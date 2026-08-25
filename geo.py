"""Application-level geographic position and distance helpers."""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, atan2, cos, degrees, isfinite, radians, sin, sqrt
from typing import Any


EARTH_RADIUS_MILES = 3958.7613


@dataclass(frozen=True)
class GeoPosition:
    """An application-level coordinate with an optional source timestamp."""

    latitude: float
    longitude: float
    updated_at: float | None = None


def make_geo_position(
    latitude: Any,
    longitude: Any,
    updated_at: Any = None,
) -> GeoPosition | None:
    """Return a valid position, or ``None`` for incomplete/malformed data."""
    latitude_value = _finite_number(latitude)
    longitude_value = _finite_number(longitude)
    if latitude_value is None or longitude_value is None:
        return None
    if not -90 <= latitude_value <= 90:
        return None
    if not -180 <= longitude_value <= 180:
        return None

    timestamp = _finite_number(updated_at)
    if timestamp is not None and timestamp <= 0:
        timestamp = None
    return GeoPosition(latitude_value, longitude_value, timestamp)


def haversine_miles(first: GeoPosition, second: GeoPosition) -> float:
    """Calculate straight-line great-circle distance between two positions."""
    latitude_delta = radians(second.latitude - first.latitude)
    longitude_delta = radians(second.longitude - first.longitude)
    first_latitude = radians(first.latitude)
    second_latitude = radians(second.latitude)

    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(first_latitude)
        * cos(second_latitude)
        * sin(longitude_delta / 2) ** 2
    )
    central_angle = 2 * asin(min(1.0, sqrt(haversine)))
    return EARTH_RADIUS_MILES * central_angle


def bearing_degrees(origin: GeoPosition, target: GeoPosition) -> float:
    """Return the initial great-circle bearing from origin to target.

    0 = north, 90 = east, 180 = south, 270 = west.
    """
    origin_latitude = radians(origin.latitude)
    target_latitude = radians(target.latitude)
    longitude_delta = radians(target.longitude - origin.longitude)

    x = sin(longitude_delta) * cos(target_latitude)
    y = cos(origin_latitude) * sin(target_latitude) - sin(origin_latitude) * cos(
        target_latitude
    ) * cos(longitude_delta)
    return degrees(atan2(x, y)) % 360.0


def bearing_and_distance(
    origin: GeoPosition | None,
    target: GeoPosition | None,
) -> tuple[float, float] | None:
    """Return (bearing degrees, distance miles), only when both positions exist."""
    if origin is None or target is None:
        return None
    return bearing_degrees(origin, target), haversine_miles(origin, target)


def distance_between(
    local_position: GeoPosition | None,
    sender_position: GeoPosition | None,
) -> float | None:
    """Return distance only when both application-level positions exist."""
    if local_position is None or sender_position is None:
        return None
    return haversine_miles(local_position, sender_position)


def format_distance_miles(distance: float) -> str:
    """Format a non-negative mile distance for a compact CHAT header."""
    if not isfinite(distance) or distance < 0:
        raise ValueError("Distance must be a finite non-negative number.")
    if distance < 0.1:
        return "<0.1miles"
    if distance < 10:
        rounded = round(distance, 1)
        if rounded < 10:
            return f"{rounded:.1f}miles"
        distance = rounded
    return f"{int(distance + 0.5)}miles"


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if isfinite(result) else None
