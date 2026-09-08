"""Google sign-in and Calendar free/busy.

Hand-rolled against `requests` rather than Google's client libraries: the
flow is three HTTP calls, and it mirrors the token exchange already written
for Density. Avoids ~10MB of transitive dependencies for that.

Scope is deliberately `calendar.freebusy` -- busy start/end times only, no
titles, attendees or locations. A leaked token reveals that the user was busy
2-3pm, not what they were doing. `openid` and `email` are only there so the
app knows which account signed in, for the allowlist.
"""

import json
import os
import secrets
from base64 import urlsafe_b64decode
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FREEBUSY_URL = "https://www.googleapis.com/calendar/v3/freeBusy"
CALENDARS_URL = "https://www.googleapis.com/calendar/v3/calendars"
CALENDAR_NAME = "RSF Dashboard"
# app.created is non-sensitive and sandboxed: it can create its own secondary
# calendars and manage events on them, but cannot see or touch the user's
# existing calendars. Read and write therefore stay cleanly separated --
# freebusy sees when you are busy and nothing else; app.created writes only
# into its own space.
SCOPES = (
    "openid email"
    " https://www.googleapis.com/auth/calendar.freebusy"
    " https://www.googleapis.com/auth/calendar.app.created"
)
TIMEOUT = 15

class NeedsReauth(Exception):
    """The refresh token no longer works -- revoked, or six months idle.

    Raised so the caller can prompt a fresh sign-in rather than failing
    mysteriously, which is how invalid_grant usually presents.
    """


def client_id() -> str:
    return os.environ["GOOGLE_CLIENT_ID"]


def _client_secret() -> str:
    return os.environ["GOOGLE_CLIENT_SECRET"]


def _cipher() -> Fernet:
    """Refresh tokens are encrypted at rest.

    The volume is private, but it is also snapshotted, and a long-lived
    credential to someone's calendar should not sit in plaintext in a file
    that gets copied around.
    """
    return Fernet(os.environ["TOKEN_ENCRYPTION_KEY"].encode())


def allowed_emails() -> set[str]:
    """Who may sign in. Empty means nobody, which is the safe default for a
    misconfigured deployment."""
    raw = os.environ.get("ALLOWED_EMAILS", "")
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


def authorize_url(redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            # offline + consent is what actually returns a refresh token;
            # without them Google issues an access token only
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )


def new_state() -> str:
    return secrets.token_urlsafe(24)


def _post_token(payload: dict) -> dict:
    response = requests.post(TOKEN_URL, data=payload, timeout=TIMEOUT)
    if response.status_code == 400 and "invalid_grant" in response.text:
        raise NeedsReauth(response.text[:200])
    response.raise_for_status()
    return response.json()


def exchange_code(code: str, redirect_uri: str) -> dict:
    return _post_token(
        {
            "code": code,
            "client_id": client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )


def access_token_from(refresh_token: str) -> str:
    return _post_token(
        {
            "refresh_token": refresh_token,
            "client_id": client_id(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
        }
    )["access_token"]


def email_from_id_token(id_token: str) -> str | None:
    """The email claim, without verifying the signature.

    Safe here because the token came straight from Google's token endpoint
    over TLS in response to our own request -- it was not supplied by a
    browser. Never trust an id_token this way if it arrives from a client.
    """
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(urlsafe_b64decode(payload)).get("email", "").lower() or None
    except (IndexError, ValueError):
        return None


def save_refresh_token(connection, email: str, refresh_token: str) -> None:
    connection.execute(
        """INSERT INTO google_tokens (email, refresh_token, linked_at)
           VALUES (%s, %s, NOW())
           ON CONFLICT (email) DO UPDATE SET
               refresh_token = EXCLUDED.refresh_token, linked_at = EXCLUDED.linked_at""",
        (email.lower(), _cipher().encrypt(refresh_token.encode()).decode()),
    )
    connection.commit()


def load_refresh_token(connection, email: str) -> str | None:
    row = connection.execute(
        "SELECT refresh_token FROM google_tokens WHERE email = %s", (email.lower(),)
    ).fetchone()
    if not row:
        return None
    try:
        return _cipher().decrypt(row["refresh_token"].encode()).decode()
    except InvalidToken:
        # the encryption key changed; the stored token is unrecoverable
        return None


def forget(connection, email: str) -> None:
    connection.execute("DELETE FROM google_tokens WHERE email = %s", (email.lower(),))
    connection.commit()


def busy_intervals(access_token: str, start: datetime, end: datetime):
    """[(start, end)] of busy blocks. Times only -- the scope returns nothing else."""
    response = requests.post(
        FREEBUSY_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "timeMin": start.astimezone(timezone.utc).isoformat(),
            "timeMax": end.astimezone(timezone.utc).isoformat(),
            "items": [{"id": "primary"}],
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    calendars = response.json().get("calendars", {})
    return [
        (datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"]))
        for block in calendars.get("primary", {}).get("busy", [])
    ]


def create_calendar(access_token: str, name: str = CALENDAR_NAME) -> str:
    """Create the app's own secondary calendar and return its id.

    app.created can only write to calendars this app made, so everything the
    agent books lives here rather than on the user's primary calendar. It
    still overlays their day view, and removing the whole calendar removes
    everything the agent ever created.
    """
    response = requests.post(
        CALENDARS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"summary": name, "timeZone": "America/Los_Angeles"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["id"]


def create_event(access_token: str, calendar_id: str, start, end,
                 summary: str, description: str = "") -> str:
    response = requests.post(
        f"{CALENDARS_URL}/{calendar_id}/events",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["id"]


def delete_event(access_token: str, calendar_id: str, event_id: str) -> None:
    """Removing an already-gone event is not an error -- the user may have
    deleted it in Google Calendar, which is a perfectly normal way to cancel."""
    response = requests.delete(
        f"{CALENDARS_URL}/{calendar_id}/events/{event_id}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=TIMEOUT,
    )
    if response.status_code not in (200, 204, 404, 410):
        response.raise_for_status()
