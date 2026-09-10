"""Deciding which arrivals are the radio's stored backlog, and when it ends.

Pure -- no Textual, no app. The rule is what these tests are about, and
a rule that can only be exercised by standing up an app is a rule
nobody re-checks.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backlog import (  # noqa: E402
    BACKLOG_CLOCK_TOLERANCE_SECONDS,
    BACKLOG_QUIET_SECONDS,
    Backlog,
    backlog_label,
    is_banked,
    record_banked,
    settle,
)

CONNECTED_AT = 1_700_000_000.0


class IsBankedTests(unittest.TestCase):
    """The radio had it before we did. That is the whole definition."""

    def test_a_message_the_radio_held_before_we_attached_is_banked(self) -> None:
        self.assertTrue(is_banked(CONNECTED_AT - 3600, CONNECTED_AT))

    def test_a_message_arriving_now_is_not(self) -> None:
        self.assertFalse(is_banked(CONNECTED_AT + 5, CONNECTED_AT))

    def test_a_radio_clock_running_slightly_slow_is_not_history(self) -> None:
        """The error that matters is calling a LIVE message history.

        A radio's clock is not the host's -- CLOCK SYNC exists because
        they drift -- so a message a second "before" connection is far
        more likely live. Labelling a conversation happening right now
        as "while you were away" is the worse mistake, so the tolerance
        leans that way.
        """
        just_before = CONNECTED_AT - (BACKLOG_CLOCK_TOLERANCE_SECONDS - 1)
        self.assertFalse(is_banked(just_before, CONNECTED_AT))

    def test_past_the_tolerance_it_is_banked(self) -> None:
        well_before = CONNECTED_AT - (BACKLOG_CLOCK_TOLERANCE_SECONDS + 1)
        self.assertTrue(is_banked(well_before, CONNECTED_AT))

    def test_no_timestamp_is_never_banked(self) -> None:
        """No evidence, so the honest default is live."""
        self.assertFalse(is_banked(None, CONNECTED_AT))
        self.assertFalse(is_banked(True, CONNECTED_AT))
        self.assertFalse(is_banked("recent", CONNECTED_AT))

    def test_nothing_is_banked_before_a_connection_exists(self) -> None:
        """There is no dividing line yet, so nothing can be on one side."""
        self.assertFalse(is_banked(CONNECTED_AT - 3600, None))


class DrainLifecycleTests(unittest.TestCase):
    def test_an_untouched_backlog_shows_nothing(self) -> None:
        self.assertFalse(Backlog().draining)

    def test_arrivals_count_and_hold_the_drain_open(self) -> None:
        backlog = record_banked(record_banked(Backlog(), 100.0), 100.5)
        self.assertEqual(backlog.count, 2)
        self.assertTrue(backlog.draining)

    def test_a_gap_shorter_than_the_quiet_window_does_not_end_it(self) -> None:
        """Two replayed packets on a slow serial link are not the end."""
        backlog = record_banked(Backlog(), 100.0)
        self.assertTrue(settle(backlog, 100.0 + BACKLOG_QUIET_SECONDS / 2).draining)

    def test_quiet_ends_the_drain(self) -> None:
        backlog = record_banked(Backlog(), 100.0)
        self.assertFalse(settle(backlog, 100.0 + BACKLOG_QUIET_SECONDS + 1).draining)

    def test_the_count_survives_the_drain_ending(self) -> None:
        """How many arrived is the interesting number, and clearing it

        on completion would throw it away at the exact moment there is
        finally something worth saying about it.
        """
        backlog = settle(record_banked(Backlog(), 100.0), 200.0)
        self.assertEqual(backlog.count, 1)

    def test_settling_an_idle_backlog_is_harmless(self) -> None:
        self.assertEqual(settle(Backlog(), 500.0), Backlog())

    def test_a_late_arrival_reopens_the_drain(self) -> None:
        """A second burst is still a burst. The indicator comes back
        rather than the count silently climbing behind a hidden widget.
        """
        backlog = settle(record_banked(Backlog(), 100.0), 200.0)
        self.assertFalse(backlog.draining)
        backlog = record_banked(backlog, 200.0)
        self.assertTrue(backlog.draining)
        self.assertEqual(backlog.count, 2)


class LabelTests(unittest.TestCase):
    def test_one_message_reads_as_one(self) -> None:
        self.assertEqual(backlog_label(1), "1 MESSAGE FROM THE RADIO")

    def test_several_read_as_several(self) -> None:
        self.assertEqual(backlog_label(11), "11 MESSAGES FROM THE RADIO")

    def test_the_radio_is_named_as_the_source(self) -> None:
        """"Receiving 11 messages" would read as ordinary live traffic,

        which is the confusion this whole feature exists to remove.
        """
        self.assertIn("RADIO", backlog_label(3))


if __name__ == "__main__":
    unittest.main()
