"""Scheduling preferences: the schema, and the bounds it does not enforce.

Preferences are set through the chat, which calls set_preferences with typed
arguments. This module owns what those arguments mean and what counts as a
sensible value -- a schema guarantees the shape of what a model produces, not
its sense, and `session_minutes: 600` is schema-valid nonsense.

The model sits at the edge. It turns language into these numbers once; every
decision downstream -- which windows are eligible, how they rank, what gets
booked -- reads the numbers and never calls a model.
"""

import os
import re

from pydantic import BaseModel, Field, field_validator

# Bounds are enforced here, not left to the model. A schema guarantees the
# shape of what comes back; it guarantees nothing about whether the numbers
# make sense, and "session_minutes: 600" is schema-valid nonsense.
SESSION_RANGE = (20, 180)
BUFFER_RANGE = (0, 120)
SESSIONS_PER_WEEK_RANGE = (1, 14)


class Preferences(BaseModel):
    """What the user asked for, as parameters the policy already understands.

    Field order is load-bearing. Structured output is generated left to right,
    so `clauses` is first on purpose: it makes the model enumerate everything
    stated before it commits to any value. Without it, extraction was
    order-dependent -- "four times a week, and never more than half full" gave
    the frequency and silently dropped the ceiling, while the same two clauses
    reversed gave both.
    """

    clauses: list[str] = Field(
        default_factory=list,
        description=(
            "Every distinct preference stated, one per item, in the order said."
            " Fill this in first, before any other field, and include a clause"
            " even when you are unsure it maps to a field below."
        ),
    )
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
            if field not in ("summary", "clauses")
        )


def _clamp(value, low, high):
    if value is None:
        return None
    return max(low, min(high, value))


def available() -> bool:
    """Whether the feature can run at all, so the UI can hide it if not."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
