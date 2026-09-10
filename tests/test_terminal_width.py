"""Measuring what the terminal paints, against a scripted terminal.

The bug this exists to close is invisible to every other test in this
repo: Rich is self-consistent, so padding measured with cell_len and
asserted with cell_len passes while the screen is visibly wrong. A
terminal with no glyph for an emoji advances ONE column where Rich
accounted for two, and in a fixed-width grid that error moves every
column to its right for the rest of the row.

The real measurement needs a tty, which a test suite does not have. What
CAN be wrong without one is the arithmetic (the report is 1-based, the
advance is not), the give-up rule, and the promise that every failure
path degrades to today's behaviour rather than to an exception. Those
are what a scripted terminal exercises here.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grapheme_text import cell_len  # noqa: E402
from terminal_width import (  # noqa: E402
    PaintedWidths,
    distinct_graphemes,
    measure_painted_widths,
    measure_terminal,
)


BEAR = "\U0001f43b"
CORN = "\U0001f33d"


class ScriptedTerminal:
    """A terminal that answers DSR with whatever widths it is told to.

    `advances` maps a grapheme to the number of columns this pretend
    terminal moves the cursor. Anything not listed gets Rich's declared
    width, so a test only states the disagreements it cares about.
    """

    def __init__(self, advances: dict[str, int] | None = None, mute: bool = False):
        self.advances = advances or {}
        self.mute = mute
        self.written: list[str] = []
        self._pending: int | None = None

    def write(self, text: str) -> None:
        self.written.append(text)
        if not text.endswith("\x1b[6n"):
            return
        painted = text[1:-4]
        self._pending = self.advances.get(painted, cell_len(painted))

    def read_reply(self) -> str | None:
        if self.mute:
            return None
        column, self._pending = self._pending, None
        return None if column is None else f"1;{column + 1}R"


class MeasurementTests(unittest.TestCase):
    def test_a_narrow_glyph_is_measured_narrow(self) -> None:
        """The actual bug. Rich says 2; this terminal advances 1."""
        terminal = ScriptedTerminal({BEAR: 1})
        measured = measure_painted_widths(
            (BEAR, CORN), write=terminal.write, read_reply=terminal.read_reply
        )
        self.assertEqual(measured, {BEAR: 1, CORN: 2})

    def test_the_report_column_is_one_based(self) -> None:
        """A grapheme that advanced N leaves the cursor at column N+1.

        Off by one here and every emoji on the board gains a cell, which
        would break the alignment this is supposed to fix.
        """
        terminal = ScriptedTerminal()
        measured = measure_painted_widths(
            ("A",), write=terminal.write, read_reply=terminal.read_reply
        )
        self.assertEqual(measured["A"], 1)

    def test_a_silent_terminal_stops_the_whole_measurement(self) -> None:
        """One timeout, not one per grapheme.

        A terminal without DSR will not acquire it on the fourth try, and
        sixty-four timeouts is a startup delay a person would notice and
        blame on the radio.
        """
        terminal = ScriptedTerminal(mute=True)
        measured = measure_painted_widths(
            (BEAR, CORN, "A"), write=terminal.write, read_reply=terminal.read_reply
        )
        self.assertEqual(measured, {})
        self.assertEqual(
            sum(1 for text in terminal.written if text.endswith("\x1b[6n")), 1
        )

    def test_the_line_is_cleared_after_each_probe(self) -> None:
        """This runs on the user's shell line before Textual starts.

        Leaving the printed graphemes behind would put a row of stray
        emoji above the app on every launch.
        """
        terminal = ScriptedTerminal()
        measure_painted_widths(
            (BEAR,), write=terminal.write, read_reply=terminal.read_reply
        )
        self.assertIn("\r\x1b[K", terminal.written)

    def test_a_malformed_report_is_skipped_not_fatal(self) -> None:
        terminal = ScriptedTerminal()
        terminal.read_reply = lambda: "garbage"
        measured = measure_painted_widths(
            (BEAR,), write=terminal.write, read_reply=terminal.read_reply
        )
        self.assertEqual(measured, {})


class PaintedWidthsTests(unittest.TestCase):
    def test_no_measurements_is_exactly_cell_len(self) -> None:
        """The identity case, and the one every test and pipe gets.

        Callers use PaintedWidths unconditionally and never branch on
        whether the terminal cooperated, which only works if the empty
        instance is indistinguishable from cell_len.
        """
        widths = PaintedWidths()
        for text in ("ALFA", BEAR, f"{BEAR}70fa", "", "中文"):
            self.assertEqual(widths.width(text), cell_len(text))

    def test_a_measured_grapheme_overrides_the_declared_width(self) -> None:
        widths = PaintedWidths({BEAR: 1})
        self.assertEqual(widths.width(BEAR), 1)
        self.assertEqual(widths.width(f"{BEAR}70fa"), 5)
        self.assertEqual(cell_len(f"{BEAR}70fa"), 6)

    def test_an_unmeasured_grapheme_beside_a_measured_one_is_unaffected(self) -> None:
        widths = PaintedWidths({BEAR: 1})
        self.assertEqual(widths.width(CORN), cell_len(CORN))

    def test_corrections_lists_only_real_disagreements(self) -> None:
        """What a bug report should carry, and usually a short list."""
        widths = PaintedWidths({BEAR: 1, CORN: 2, "A": 1})
        self.assertEqual(widths.corrections, {BEAR: 1})

    def test_a_nonsense_measurement_is_ignored(self) -> None:
        widths = PaintedWidths({BEAR: -3, CORN: None})
        self.assertEqual(widths.width(BEAR), cell_len(BEAR))
        self.assertEqual(widths.width(CORN), cell_len(CORN))


class GraphemeSelectionTests(unittest.TestCase):
    def test_ascii_is_never_measured(self) -> None:
        """No terminal disagrees about ASCII, and measuring it would

        spend the whole budget before reaching the emoji that are the
        entire reason for doing this.
        """
        self.assertEqual(distinct_graphemes(("ALFA", "BRVO")), ())

    def test_each_grapheme_is_measured_once(self) -> None:
        names = (BEAR, f"{BEAR}70fa", CORN, BEAR)
        self.assertEqual(distinct_graphemes(names), (BEAR, CORN))

    def test_the_budget_is_respected(self) -> None:
        names = tuple(chr(0x1F300 + index) for index in range(50))
        self.assertEqual(len(distinct_graphemes(names, limit=8)), 8)

    def test_a_zwj_sequence_is_one_grapheme(self) -> None:
        """Measured whole, because it is drawn whole. Probing half of a

        family emoji would measure a fragment no terminal will ever be
        asked to paint.
        """
        family = "\U0001f468‍\U0001f469‍\U0001f466"
        self.assertEqual(distinct_graphemes((family,)), (family,))


class MeasureTerminalTests(unittest.TestCase):
    def test_measuring_without_a_terminal_yields_the_declared_widths(self) -> None:
        """The test-suite and piped-output case. Must not raise."""
        widths = measure_terminal((BEAR, CORN))
        self.assertEqual(widths.width(BEAR), cell_len(BEAR))

    def test_all_ascii_names_skip_the_terminal_entirely(self) -> None:
        """Nothing to disagree about means no reason to touch stdout."""
        self.assertFalse(measure_terminal(("ALFA", "BRVO")))


if __name__ == "__main__":
    unittest.main()
