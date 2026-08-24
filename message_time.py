"""Clock-safe, semantically honest timestamp selection for CHAT entries."""

from __future__ import annotations

from math import isfinite
from typing import Any


MAX_FUTURE_SKEW_SECONDS = 5 * 60


def validate_radio_rx_at(value: Any, app_received_at: float) -> float | None:
    """Return a usable receiver-stamped wall-clock time or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    timestamp = float(value)
    if not isfinite(timestamp) or timestamp <= 0:
        return None
    if timestamp > app_received_at + MAX_FUTURE_SKEW_SECONDS:
        return None
    return timestamp


def validate_origin_sent_at(value: Any, app_received_at: float) -> float | None:
    """Validate an origin time already established by a trustworthy protocol."""
    return validate_radio_rx_at(value, app_received_at)


def make_age_reference(
    radio_rx_at: Any,
    app_received_at: float,
    monotonic_now: float,
) -> tuple[float | None, float]:
    """Convert optional radio receive time to a monotonic age reference."""
    valid_radio_rx_at = validate_radio_rx_at(radio_rx_at, app_received_at)
    initial_age = (
        max(0.0, app_received_at - valid_radio_rx_at)
        if valid_radio_rx_at is not None
        else 0.0
    )
    return valid_radio_rx_at, monotonic_now - initial_age


def make_incoming_age_reference(
    origin_sent_at: Any,
    radio_rx_at: Any,
    app_received_at: float,
    monotonic_now: float,
) -> tuple[float | None, float | None, float, bool]:
    """Choose true origin age when supplied, otherwise explicit receive age.

    Ordinary Meshtastic text packets do not supply ``origin_sent_at``.  The
    parameter exists as a separate application concept so a future protocol
    with authenticated origin time cannot be confused with receiver ``rxTime``.
    """
    valid_origin_sent_at = validate_origin_sent_at(
        origin_sent_at,
        app_received_at,
    )
    valid_radio_rx_at = validate_radio_rx_at(radio_rx_at, app_received_at)
    age_time = valid_origin_sent_at or valid_radio_rx_at or app_received_at
    initial_age = max(0.0, app_received_at - age_time)
    return (
        valid_origin_sent_at,
        valid_radio_rx_at,
        monotonic_now - initial_age,
        valid_origin_sent_at is None,
    )
