import os
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

IDENTITY_URL = "https://identity.density.io/oauth/wayfinding/exchange"
DISPLAY_ID = "dsp_956223069054042646"
DISPLAY_URL = f"https://api.density.io/app/v2/safe-display-core/displays/{DISPLAY_ID}"
TIMEOUT = 10


@dataclass
class Reading:
    count: int
    capacity: int
    observed_at: datetime


def _get_access_token() -> str:
    """Trade the long-lived share token for a short-lived access token"""
    share_token = os.environ["DENSITY_SHARE_TOKEN"]
    response = requests.post(
        IDENTITY_URL,
        headers={"Authorization": f"Bearer {share_token}"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def fetch_reading() -> Reading:
    """Fetch the current occupancy from the Density API"""
    response = requests.get(
        DISPLAY_URL,
        headers={"Authorization": f"Bearer {_get_access_token()}"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    space = response.json()["dedicated_space"]
    return Reading(
        count=space["current_count"],
        capacity=space["capacity"],
        observed_at=datetime.now(timezone.utc),
    )

def percentage(occupancy, capacity):
    """Computes percentage full"""
    return occupancy/capacity
