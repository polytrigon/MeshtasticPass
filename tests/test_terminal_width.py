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
    install_terminal_widths,
    measure_painted_widths,
    measure_terminal,
    plan_corrections,
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


HEART = "\u2764\ufe0f"        # HEAVY BLACK HEART + VARIATION SELECTOR-16
KEYCAP = "5\ufe0f\u20e3"       # DIGIT FIVE + VS16 + COMBINING ENCLOSING KEYCAP
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f466"


class CorrectionPlanTests(unittest.TestCase):
    """Translating measurements into terms Rich can actually accept.

    Rich computes widths two different ways and they need different
    corrections, which is the whole reason this is a plan rather than a
    dict. Getting the classification wrong means a correction that
    silently does nothing.
    """

    @staticmethod
    def _plan(measured):
        return plan_corrections(PaintedWidths(measured))

    def test_a_single_codepoint_becomes_a_per_character_width(self) -> None:
        plan = self._plan({BEAR: 1})
        self.assertEqual(plan.per_character, {ord(BEAR): 1})
        self.assertEqual(plan.unpromoted_bases, frozenset())

    def test_a_variation_selector_sequence_unpromotes_its_base(self) -> None:
        """Rich never looks such a sequence up as a unit.

        It measures the BASE and adds one if the base is in the cell
        table's narrow_to_wide set, so a per-character width for the
        whole sequence would be a correction Rich never consults.
        """
        plan = self._plan({HEART: 1})
        self.assertEqual(plan.unpromoted_bases, frozenset({"\u2764"}))
        self.assertEqual(plan.per_character, {})

    def test_a_keycap_unpromotes_its_digit(self) -> None:
        """Three codepoints, same mechanism -- and the reason CHAT

        currently substitutes circled digits for keycaps at the display
        boundary.
        """
        plan = self._plan({KEYCAP: 1})
        self.assertEqual(plan.unpromoted_bases, frozenset({"5"}))

    def test_a_zwj_sequence_is_reported_rather_than_approximated(self) -> None:
        """Neither mechanism can carry it, so it is named and left alone.

        Guessing at a correction Rich cannot express would be worse than
        the disagreement: it would move widths for every OTHER sequence
        sharing that base.
        """
        plan = self._plan({FAMILY: 4})
        self.assertEqual(plan.unexpressible, (FAMILY,))
        self.assertEqual(plan.per_character, {})
        self.assertEqual(plan.unpromoted_bases, frozenset())

    def test_agreement_produces_no_plan_at_all(self) -> None:
        """A terminal that paints what Rich expects must not be patched."""
        plan = self._plan({BEAR: cell_len(BEAR), "A": 1})
        self.assertFalse(plan)


class InstallTests(unittest.TestCase):
    """Correcting Rich itself.

    Every wrap point, virtual size and scrollbar position Textual
    computes comes from rich.cells.cell_len, so this is what makes the
    measurement reach CHAT rather than only this app's own grids.

    These tests patch a THIRD-PARTY MODULE GLOBALLY. Every one of them
    restores it, because a leak would silently change the widths every
    later test in the run measures with.
    """

    def setUp(self) -> None:
        import rich.cells as cells

        self.cells = cells
        original_size = cells.get_character_cell_size
        original_load = cells.load_cell_table

        def restore() -> None:
            cells.get_character_cell_size = original_size
            cells.load_cell_table = original_load
            original_size.cache_clear()
            cells.cached_cell_len.cache_clear()

        self.addCleanup(restore)

    def test_a_correction_reaches_rich_and_everything_built_on_it(self) -> None:
        """Including the modules that imported cell_len BY VALUE.

        A dozen Rich modules do `from .cells import cell_len` at import
        time, so patching cell_len itself would miss them. cell_len's
        implementation resolves get_character_cell_size as a bare name
        in rich.cells' globals at CALL time, which is why patching that
        reaches them anyway -- and is the single fact this whole
        approach rests on.
        """
        from rich.text import Text

        self.assertEqual(self.cells.cell_len(BEAR), 2)
        self.assertTrue(install_terminal_widths(plan_corrections(PaintedWidths({BEAR: 1}))))

        self.assertEqual(self.cells.cell_len(BEAR), 1)
        self.assertEqual(Text(f"{BEAR}70fa").cell_len, 5)
        self.assertEqual(sys.modules["rich.text"].cell_len(BEAR), 1)

    def test_an_unpromoted_base_reaches_rich(self) -> None:
        self.assertEqual(self.cells.cell_len(HEART), 2)
        install_terminal_widths(plan_corrections(PaintedWidths({HEART: 1})))
        self.assertEqual(self.cells.cell_len(HEART), 1)

    def test_uncorrected_characters_are_untouched(self) -> None:
        """The blast radius has to stay exactly as wide as the evidence."""
        install_terminal_widths(plan_corrections(PaintedWidths({BEAR: 1})))
        self.assertEqual(self.cells.cell_len("ALFA"), 4)
        self.assertEqual(self.cells.cell_len("\u4e2d"), 2)
        self.assertEqual(self.cells.cell_len(CORN), 2)

    def test_nothing_to_correct_patches_nothing(self) -> None:
        """An unmeasurable terminal must leave Rich exactly as found."""
        before = self.cells.get_character_cell_size
        self.assertFalse(install_terminal_widths(plan_corrections(PaintedWidths())))
        self.assertIs(self.cells.get_character_cell_size, before)

    def test_installing_twice_is_a_no_op(self) -> None:
        plan = plan_corrections(PaintedWidths({BEAR: 1}))
        self.assertTrue(install_terminal_widths(plan))
        self.assertFalse(install_terminal_widths(plan))
        self.assertEqual(self.cells.cell_len(BEAR), 1)

    def test_rich_caches_are_cleared(self) -> None:
        """Rich memoises widths. Anything measured before the patch is

        now wrong, and an uncleared cache would serve it for the rest of
        the process -- which on this app means for the whole session.
        """
        self.assertEqual(self.cells.cell_len(f"{BEAR} hello"), 8)
        install_terminal_widths(plan_corrections(PaintedWidths({BEAR: 1})))
        self.assertEqual(self.cells.cell_len(f"{BEAR} hello"), 7)


if __name__ == "__main__":
    unittest.main()
