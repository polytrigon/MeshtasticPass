"""Clock-safe timestamp selection for application chat entries."""

from __future__ import annotations

from math import isfinite
from typing import Any


MAX_FUTURE_SKEW_SECONDS = 5 * 60


def validate_sent_at(value: Any, received_at: float) -> float | None:
    """Return a usable packet wall-clock timestamp or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    timestamp = float(value)
    if not isfinite(timestamp) or timestamp <= 0:
        return None
    if timestamp > received_at + MAX_FUTURE_SKEW_SECONDS:
        return None
    return timestamp


def make_age_reference(
    sent_at: Any,
    received_at: float,
    monotonic_now: float,
) -> tuple[float | None, float]:
    """Convert optional wall-clock packet time to a monotonic reference."""
    valid_sent_at = validate_sent_at(sent_at, received_at)
    initial_age = (
        max(0.0, received_at - valid_sent_at)
        if valid_sent_at is not None
        else 0.0
    )
    return valid_sent_at, monotonic_now - initial_age
