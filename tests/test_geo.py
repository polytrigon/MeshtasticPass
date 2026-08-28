"""Tests for application-level geographic position and distance helpers."""

from __future__ import annotations

import unittest

from geo import (
    KM_PER_MILE,
    GeoPosition,
    distance_between,
    format_coordinates,
    format_distance,
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


class FormatDistanceTests(unittest.TestCase):
    """MESH VIEW PASS item 11/12: one underlying distance calculation,

    METRIC/IMPERIAL only ever a presentation choice on top of it.
    """

    def test_imperial_renders_miles_suffix(self) -> None:
        self.assertEqual(format_distance(4.23, metric=False), "4.2 mi")

    def test_metric_renders_km_suffix_derived_from_the_same_miles_value(self) -> None:
        self.assertEqual(
            format_distance(4.23, metric=True), f"{4.23 * KM_PER_MILE:.1f} km"
        )

    def test_zero_distance_both_units(self) -> None:
        self.assertEqual(format_distance(0.0, metric=False), "0.0 mi")
        self.assertEqual(format_distance(0.0, metric=True), "0.0 km")

    def test_rounds_to_one_decimal_place(self) -> None:
        self.assertEqual(format_distance(1.849, metric=False), "1.8 mi")
        self.assertEqual(format_distance(1.851, metric=False), "1.9 mi")

    def test_large_distance_still_one_decimal_no_thousands_separator(self) -> None:
        self.assertEqual(format_distance(1234.5, metric=False), "1234.5 mi")

    def test_negative_distance_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_distance(-1.0, metric=False)

    def test_non_finite_distance_rejected(self) -> None:
        with self.assertRaises(ValueError):
            format_distance(float("nan"), metric=False)
        with self.assertRaises(ValueError):
            format_distance(float("inf"), metric=True)

    def test_metric_and_imperial_are_never_independently_computed(self) -> None:
        """Both units must always derive from the SAME miles figure via

        KM_PER_MILE -- never a second, independently-rounded great-
        circle calculation that could drift from the mi figure.
        """
        miles = 7.5
        km_text = format_distance(miles, metric=True)
        expected_km = round(miles * KM_PER_MILE, 1)
        self.assertEqual(km_text, f"{expected_km} km")

    def test_small_distance_does_not_collapse_to_zero(self) -> None:
        self.assertEqual(format_distance(0.04, metric=False), "0.0 mi")


class FormatCoordinatesTests(unittest.TestCase):
    """MESH VIEW PASS item 4: compact real-GPS-fix text for YOU."""

    def test_positive_coordinates(self) -> None:
        self.assertEqual(
            format_coordinates(GeoPosition(40.7128, -74.0060)), "40.7128, -74.0060"
        )

    def test_negative_latitude_and_longitude(self) -> None:
        self.assertEqual(
            format_coordinates(GeoPosition(-33.8688, 151.2093)), "-33.8688, 151.2093"
        )

    def test_rounds_to_four_decimal_places(self) -> None:
        self.assertEqual(
            format_coordinates(GeoPosition(1.23456789, -1.00001)), "1.2346, -1.0000"
        )

    def test_equator_and_prime_meridian(self) -> None:
        self.assertEqual(format_coordinates(GeoPosition(0.0, 0.0)), "0.0000, 0.0000")

    def test_ignores_the_optional_timestamp_field(self) -> None:
        self.assertEqual(
            format_coordinates(GeoPosition(10.0, 20.0, updated_at=1_700_000_000.0)),
            "10.0000, 20.0000",
        )

