"""Turning a sentence about your schedule into policy parameters.

The model sits at the edge of the system, not inside it. It runs once, when
you describe what you want, and its only job is to turn ambiguous language
into a small set of numbers. Everything downstream -- which windows are
eligible, how they rank, what gets booked -- stays deterministic and
testable, and never calls a model.

That boundary is deliberate. Ambiguous language is what a model is good at.
"Is 2pm inside the open window" is not a judgement call, and putting a model
anywhere near it would make the system slower, costlier, and impossible to
test.
"""

import os
import re

import anthropic
from pydantic import BaseModel, Field, field_validator

MODEL = "claude-opus-5"
MAX_TOKENS = 1024

# Bounds are enforced here, not left to the model. A schema guarantees the
# shape of what comes back; it guarantees nothing about whether the numbers
# make sense, and "session_minutes: 600" is schema-valid nonsense.
SESSION_RANGE = (20, 180)
BUFFER_RANGE = (0, 120)
SESSIONS_PER_WEEK_RANGE = (1, 14)


class Preferences(BaseModel):
    """What the user asked for, as parameters the policy already understands."""

    session_minutes: int | None = Field(
        None, description="How long a workout should be, in minutes."
    )
    earliest_hour: int | None = Field(
        None, description="Earliest hour of the day they would go, 0-23 local time."
    )
    latest_hour: int | None = Field(
        None, description="Latest hour a session may *start*, 0-23 local time."
    )
    max_crowding_pct: float | None = Field(
        None, description="Busiest they will tolerate, 0-1. 0.6 means 60% full."
    )
    travel_buffer_minutes: int | None = Field(
        None, description="Clearance needed either side of a calendar commitment."
    )
    sessions_per_week: int | None = Field(
        None, description="How many workouts a week they are aiming for."
    )
    summary: str = Field(
        description="One short sentence saying what was understood, addressed to the user."
    )

    @field_validator("summary")
    @classmethod
    def _clean_summary(cls, value: str) -> str:
        """Strip generation artifacts before this reaches a person.

        Observed once in production: a summary ending in a leaked
        "summary_end_placeholder" token. Rare, but it is user-facing text, and
        a schema cannot police the contents of a free string.
        """
        cleaned = re.sub(r"\s*\w*_?placeholder\w*\s*$", "", value.strip())
        return cleaned[:200].strip()

    @field_validator("earliest_hour", "latest_hour")
    @classmethod
    def _hour_in_range(cls, value):
        return None if value is None or not 0 <= value <= 23 else value

    @field_validator("session_minutes")
    @classmethod
    def _sensible_session(cls, value):
        return _clamp(value, *SESSION_RANGE)

    @field_validator("travel_buffer_minutes")
    @classmethod
    def _sensible_buffer(cls, value):
        return _clamp(value, *BUFFER_RANGE)

    @field_validator("sessions_per_week")
    @classmethod
    def _sensible_frequency(cls, value):
        return _clamp(value, *SESSIONS_PER_WEEK_RANGE)

    @field_validator("max_crowding_pct")
    @classmethod
    def _fraction(cls, value):
        if value is None:
            return None
        # "60% full" said as 60 rather than 0.6 is the obvious mistake to absorb
        if 1 < value <= 100:
            value = value / 100
        return None if not 0 < value <= 1 else value

    def is_empty(self) -> bool:
        """True when nothing usable was extracted, whatever the summary claims."""
        return all(
            getattr(self, field) is None
            for field in self.model_fields
            if field != "summary"
        )


def _clamp(value, low, high):
    if value is None:
        return None
    return max(low, min(high, value))


SYSTEM = """You turn a sentence about someone's gym schedule into structured \
preferences for a scheduling tool.

Rules:
- Only fill a field the person actually expressed. Leave everything else null.
  Inventing a preference is worse than returning nothing.
- Hours are local wall-clock, 0-23. "mornings" is not an hour; only set
  earliest_hour or latest_hour if they named or clearly implied a boundary.
- latest_hour is the latest a session may START.
- max_crowding_pct is a fraction. A stated proportion counts as expressed,
  whether written as a number or in words: "under 50%" and "more than half
  full" are both 0.5; "a quarter full" is 0.25. Only vague words with no
  proportion in them -- "not too busy", "when it's quiet" -- are too imprecise
  to use, and those stay null.
- summary is one short sentence in second person saying what you understood,
  so they can see whether you got it right."""


def parse(text: str, client=None) -> Preferences | None:
    """Free text to validated preferences. None if it could not be read.

    Returns None rather than raising: a misread sentence should leave the
    user's existing settings alone, not break the page.
    """
    if not text or not text.strip():
        return None
    try:
        client = client or anthropic.Anthropic()
        response = client.messages.parse(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": text.strip()}],
            output_format=Preferences,
        )
        parsed = response.parsed_output
    except Exception:
        return None
    if parsed is None or parsed.is_empty():
        return None
    return parsed


def available() -> bool:
    """Whether the feature can run at all, so the UI can hide it if not."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
