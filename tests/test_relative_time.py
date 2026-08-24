"""Tests for compact chat relative-time formatting."""

from __future__ import annotations

import unittest

from relative_time import format_relative_age


class RelativeTimeTests(unittest.TestCase):
    def test_formats_compact_ages(self) -> None:
        cases = (
            (0, "0s"),
            (12, "12s"),
            (59, "59s"),
            (60, "1min"),
            (5 * 60, "5min"),
            (27 * 60, "27min"),
            (60 * 60, "1h"),
            (63 * 60, "1h 3min"),
            ((23 * 60 + 59) * 60, "23h 59min"),
            (24 * 60 * 60, "1d"),
            ((2 * 24 + 4) * 60 * 60, "2d 4h"),
        )

        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(format_relative_age(seconds), expected)

    def test_negative_age_clamps_to_zero(self) -> None:
        self.assertEqual(format_relative_age(-10), "0s")

    def test_minutes_never_use_clipped_mi_abbreviation(self) -> None:
        for minutes in range(1, 60):
            rendered = format_relative_age(minutes * 60)
            self.assertEqual(rendered, f"{minutes}min")
            self.assertFalse(rendered.endswith("mi"))

        self.assertEqual(format_relative_age(63 * 60), "1h 3min")
