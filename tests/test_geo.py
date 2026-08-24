"""Tests for application-level geographic position and distance helpers."""

from __future__ import annotations

import unittest

from geo import (
    GeoPosition,
    distance_between,
    format_distance_miles,
    haversine_miles,
    make_geo_position,
)


class GeographicDistanceTests(unittest.TestCase):
    def test_haversine_distance_for_valid_coordinates(self) -> None:
        equator_origin = GeoPosition(0.0, 0.0)
        one_degree_east = GeoPosition(0.0, 1.0)

        self.assertAlmostEqual(
            haversine_miles(equator_origin, one_degree_east),
            69.09,
            places=2,
        )

    def test_missing_position_omits_distance(self) -> None:
        position = GeoPosition(40.7128, -74.0060)

        self.assertIsNone(distance_between(None, position))
        self.assertIsNone(distance_between(position, None))

    def test_malformed_or_incomplete_position_is_rejected(self) -> None:
        malformed = (
            (None, -74.0),
            (40.0, None),
            ("40.0", -74.0),
            (91.0, -74.0),
            (40.0, -181.0),
            (float("nan"), -74.0),
        )

        for latitude, longitude in malformed:
            with self.subTest(latitude=latitude, longitude=longitude):
                self.assertIsNone(make_geo_position(latitude, longitude))

    def test_distance_formatting(self) -> None:
        cases = (
            (0.0, "<0.1miles"),
            (0.09, "<0.1miles"),
            (2.44, "2.4miles"),
            (9.94, "9.9miles"),
            (9.96, "10miles"),
            (12.4, "12miles"),
            (12.5, "13miles"),
        )

        for distance, expected in cases:
            with self.subTest(distance=distance):
                self.assertEqual(format_distance_miles(distance), expected)

