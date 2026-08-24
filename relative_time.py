"""Compact relative-time formatting for the MeshtasticPass UI."""

from __future__ import annotations


def format_relative_age(age_seconds: float) -> str:
    """Format a non-negative elapsed duration as a compact chat age."""
    seconds = max(0, int(age_seconds))
    if seconds < 60:
        return f"{seconds}s"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"

    hours = minutes // 60
    remaining_minutes = minutes % 60
    if hours < 24:
        suffix = f" {remaining_minutes}m" if remaining_minutes else ""
        return f"{hours}h{suffix}"

    days = hours // 24
    remaining_hours = hours % 24
    suffix = f" {remaining_hours}h" if remaining_hours else ""
    return f"{days}d{suffix}"
