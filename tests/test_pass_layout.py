"""PASSES column layout: the DOS `dir /w` flow, measured in display cells.

The failure this file exists to prevent is a column that drifts out of
alignment because a name was measured with len() instead of its terminal
width -- which on a real mesh happens the moment somebody sets their
short name to an emoji.
"""

from __future__ import annotations

import os
import sys
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grapheme_text import cell_len  # noqa: E402
from pass_layout import (  # noqa: E402
    PASS_COLUMN_GUTTER,
    PASS_NAME_MAX_CELLS,
    PASS_TRUNCATION_MARKER,
    format_pass_bar,
    pass_row_offset,
    lay_out_passes,
    pass_column_count,
    pass_column_width,
)


class ColumnFlowTests(unittest.TestCase):
    def test_names_flow_left_to_right_then_wrap(self) -> None:
        """MS-DOS `dir /w` order, which is what the reference screenshot

        shows: reading across a row is alphabetical, not down a column.
        """
        rows = lay_out_passes(("AAA", "BBB", "CCC", "DDD", "EEE"), viewport_width=20)
        self.assertEqual([cell.strip() for cell in rows[0]], ["AAA", "BBB", "CCC", "DDD"])
        self.assertEqual([cell.strip() for cell in rows[1]], ["EEE"])

    def test_the_last_row_is_short_not_padded_with_blanks(self) -> None:
        rows = lay_out_passes(("AAA", "BBB", "CCC"), viewport_width=20)
        self.assertEqual(len(rows[-1]), 3)

    def test_an_empty_list_lays_out_to_nothing(self) -> None:
        self.assertEqual(lay_out_passes((), viewport_width=60), ())


class ColumnWidthTests(unittest.TestCase):
    def test_the_widest_name_sets_the_column(self) -> None:
        self.assertEqual(pass_column_width(("AB", "ABCDE", "A")), 5)

    def test_one_very_long_name_cannot_collapse_the_board(self) -> None:
        """Without the cap, a single long long-name would take the whole

        width and leave one column -- the full identity belongs in the
        bottom bar, as it does on MESH.
        """
        self.assertEqual(
            pass_column_width(("A", "B" * 80)), PASS_NAME_MAX_CELLS
        )

    def test_a_viewport_narrower_than_one_column_still_gets_one(self) -> None:
        """Zero columns would render nothing and read as an empty pass

        list rather than a narrow window.
        """
        self.assertEqual(pass_column_count(("ABCDEFGH",), viewport_width=3), 1)

    def test_column_count_accounts_for_the_gutter(self) -> None:
        names = ("ABCD",)  # 4 cells + 2 gutter = 6 per column, last needs no gutter
        self.assertEqual(pass_column_count(names, viewport_width=4), 1)
        self.assertEqual(pass_column_count(names, viewport_width=10), 2)
        self.assertEqual(pass_column_count(names, viewport_width=16), 3)


class DisplayWidthTests(unittest.TestCase):
    """Every cell must be equal in TERMINAL width, not in len()."""

    def test_emoji_names_produce_equal_width_cells(self) -> None:
        names = ("ALFA", "🐍", "👍🏽", "🇺🇸", "BRAVO")
        rows = lay_out_passes(names, viewport_width=60)
        widths = {cell_len(cell) for row in rows for cell in row}
        self.assertEqual(len(widths), 1, f"ragged columns: {widths}")

    def test_a_wide_glyph_is_not_padded_as_if_it_were_narrow(self) -> None:
        """The specific bug: len('🐍') is 1, cell_len is 2. Padding by

        len() would make that column one cell too wide and shunt every
        column to its right.
        """
        rows = lay_out_passes(("🐍", "AB"), viewport_width=60)
        self.assertEqual(cell_len(rows[0][0]), cell_len(rows[0][1]))

    def test_an_over_long_name_is_truncated_not_left_to_overflow(self) -> None:
        rows = lay_out_passes(("A" * 40, "BB"), viewport_width=60)
        self.assertEqual(cell_len(rows[0][0]), PASS_NAME_MAX_CELLS)

    def test_truncation_never_severs_a_grapheme_cluster(self) -> None:
        """A severed ZWJ sequence or flag half renders unpredictably wide

        and corrupts whatever is drawn to its right (see grapheme_text).
        """
        for name in ("👨‍👩‍👧‍👦" * 6, "🇺🇸" * 12, "👍🏽" * 12, "é" * 30):
            with self.subTest(name=name[:8]):
                rows = lay_out_passes((name,), viewport_width=60)
                self.assertLessEqual(cell_len(rows[0][0]), PASS_NAME_MAX_CELLS)

    def test_every_row_fits_the_viewport_it_was_laid_out_for(self) -> None:
        names = tuple(f"NODE{n:02d}" for n in range(37))
        for width in range(8, 121, 7):
            with self.subTest(width=width):
                rows = lay_out_passes(names, width)
                for row in rows:
                    painted = sum(cell_len(cell) for cell in row) + PASS_COLUMN_GUTTER * (
                        len(row) - 1
                    )
                    self.assertLessEqual(painted, max(width, pass_column_width(names)))


class DuplicateNameTests(unittest.TestCase):
    """Meshtastic short names are not unique and never were.

    They are shown anyway. A version of this view appended each
    colliding node's ID tail so no two cells could look alike; it cost
    four cells of width on every such name, and on a terminal whose
    emoji glyphs overpaint the character beside them the separator was
    not even visible. Identity moved to the bar and the ENTER menu,
    which have room to print a node ID. What the grid owes the reader is
    that duplicates still LAY OUT correctly -- two identical names must
    occupy two cells of equal width, not collapse or drift.
    """

    def test_identical_names_produce_identical_cells(self) -> None:
        grid = lay_out_passes(("BIG", "BIG", "ALFA"), viewport_width=60)
        self.assertEqual(grid[0][0], grid[0][1])

    def test_duplicates_are_not_collapsed(self) -> None:
        """Three radios called SAME are three rows, not one.

        The list is a record of nodes, and a node is not a name.
        """
        grid = lay_out_passes(("SAME", "SAME", "SAME"), viewport_width=60)
        self.assertEqual(sum(len(row) for row in grid), 3)

    def test_duplicate_emoji_names_keep_equal_width_cells(self) -> None:
        """The real case: two radios sharing one emoji short name."""
        names = ("\U0001f43b", "\U0001f43b", "ALFA")
        grid = lay_out_passes(names, viewport_width=60)
        widths = {cell_len(cell) for row in grid for cell in row}
        self.assertEqual(len(widths), 1, f"ragged columns: {widths}")

    def test_a_bare_emoji_name_costs_no_more_width_than_it_is(self) -> None:
        """What removing the ID tail bought.

        A board of emoji names now sets its column from the emoji, not
        from the emoji plus a five-character suffix, which is several
        more columns of names on an 80-column screen.
        """
        self.assertEqual(
            pass_column_width(("\U0001f43b", "\U0001f43b")), 2
        )


class RowOffsetTests(unittest.TestCase):
    """Scrolling the pass grid: minimal movement, never overscrolling."""

    def test_a_selection_already_visible_does_not_scroll(self) -> None:
        self.assertEqual(pass_row_offset(20, 5, 2, 0), 0)
        self.assertEqual(pass_row_offset(20, 5, 7, 5), 5)

    def test_stepping_past_the_fold_moves_by_one_row(self) -> None:
        """Not a recentre. Every cell looks like every other cell here,

        so a list that jumps the selection to the middle of the screen
        makes it hard to keep your place.
        """
        self.assertEqual(pass_row_offset(20, 5, 5, 0), 1)

    def test_moving_above_the_window_scrolls_up_to_it(self) -> None:
        self.assertEqual(pass_row_offset(20, 5, 3, 5), 3)

    def test_the_last_row_cannot_scroll_past_the_end(self) -> None:
        """The bottom of the list is the bottom of the list -- never a

        screen of empty space below it.
        """
        self.assertEqual(pass_row_offset(20, 5, 19, 10), 15)

    def test_a_list_shorter_than_the_viewport_never_scrolls(self) -> None:
        self.assertEqual(pass_row_offset(3, 10, 2, 0), 0)

    def test_degenerate_sizes_are_survivable(self) -> None:
        """Called during layout, when the widget may not have a size."""
        self.assertEqual(pass_row_offset(20, 0, 5, 3), 0)
        self.assertEqual(pass_row_offset(0, 5, 0, 0), 0)

    def test_walking_the_whole_list_keeps_the_selection_visible(self) -> None:
        total, viewport, offset = 37, 6, 0
        for row in range(total):
            offset = pass_row_offset(total, viewport, row, offset)
            self.assertTrue(
                offset <= row < offset + viewport,
                f"row {row} not visible at offset {offset}",
            )
            self.assertLessEqual(offset, total - viewport)


class AmbiguousWidthTests(unittest.TestCase):
    """Nothing the LAYOUT adds to a cell may have a negotiable width.

    East_Asian_Width=AMBIGUOUS characters -- U+00B7 MIDDLE DOT, U+2026
    HORIZONTAL ELLIPSIS and a large family besides -- are one cell to
    Rich and two cells to a terminal whose font or configuration treats
    that class as wide. In flowing text the disagreement costs a column
    at the end of a line. In a fixed-width grid it moves every column to
    the right of it, for the whole row, and the app cannot detect that
    it happened.

    Node names come off the mesh and are taken as they are. Everything
    the layout puts AROUND them is a choice, and this pins the choice:
    ASCII, whose width no terminal disagrees about. The value of the
    test is that the next person to reach for a nicer-looking separator
    finds out here rather than on a uConsole.
    """

    def _assert_unambiguous(self, value: str) -> None:
        for character in value:
            width = unicodedata.east_asian_width(character)
            self.assertNotEqual(
                width,
                "A",
                f"U+{ord(character):04X} is East_Asian_Width=AMBIGUOUS",
            )
            self.assertTrue(
                character.isascii(),
                f"U+{ord(character):04X} is not ASCII",
            )

    def test_the_truncation_marker_has_an_undisputed_width(self) -> None:
        self._assert_unambiguous(PASS_TRUNCATION_MARKER)

    def test_a_truncated_name_is_not_marked_with_an_ellipsis(self) -> None:
        """truncate_to_cells defaults to "…", which is AMBIGUOUS.

        The grid has to override that default, and this fails if the
        override is ever dropped -- a regression that would only show up
        on names long enough to be cut, which is exactly the kind of
        rare case nobody notices going wrong.
        """
        rows = lay_out_passes(("X" * 40,), viewport_width=60)
        self.assertNotIn("\u2026", rows[0][0])
        self.assertIn(PASS_TRUNCATION_MARKER, rows[0][0])

    def test_everything_the_layout_adds_to_a_cell_is_unambiguous(self) -> None:
        """The general rule, checked against real output.

        Whatever appears in a rendered cell that was NOT in the node's
        own name got there from this module, and must be safe.
        """
        names = ("\U0001f43b", "\U0001f43b", "Q" * 40)
        supplied = set("".join(names))
        for row in lay_out_passes(names, viewport_width=60):
            for cell in row:
                self._assert_unambiguous(
                    "".join(c for c in cell if c not in supplied)
                )


class PassBarTests(unittest.TestCase):
    """The one line under the grid: identity first, then how they reached us."""

    @staticmethod
    def _encounter(**kwargs):
        from chat_store import NodeEncounter

        fields = {
            "node_id": "!e5deef81",
            "long_name": None,
            "short_name": "ALFA",
            "hops_away": None,
            "first_seen_at": 1_700_000_000.0,
            "last_seen_at": 1_700_000_000.0,
            "first_heard_at": None,
            "last_heard_at": None,
        }
        fields.update(kwargs)
        return NodeEncounter(**fields)

    def test_a_node_only_gossiped_about_says_via_mesh(self) -> None:
        bar = format_pass_bar(self._encounter(), now=1_700_000_000.0)
        self.assertIn("VIA MESH", bar)
        self.assertNotIn("HEARD DIRECTLY", bar)

    def test_a_node_we_heard_ourselves_says_heard_not_met(self) -> None:
        """HEARD, never MET: unrelayed packets prove range, not a meeting.

        MET is reserved for a pass, so the two words cannot be read as
        the same claim by someone glancing at the bar.
        """
        bar = format_pass_bar(
            self._encounter(first_heard_at=1_699_000_000.0), now=1_700_000_000.0
        )
        self.assertIn("HEARD DIRECTLY", bar)
        self.assertNotIn("MET", bar)

    def test_heard_directly_and_a_hop_count_coexist(self) -> None:
        """Not a contradiction, and must not be suppressed as one.

        first_heard_at is a past-tense fact that never expires; hops_away
        is where the node is now. A node heard in the room last week and
        five hops away today is both.
        """
        bar = format_pass_bar(
            self._encounter(first_heard_at=1_699_000_000.0, hops_away=5),
            now=1_700_000_000.0,
        )
        self.assertIn("HEARD DIRECTLY", bar)
        self.assertIn("HOPS 5", bar)

    def test_no_pass_field_appears_without_a_pass(self) -> None:
        """Every real row today. An absent pass is absent, not "PASS: no"."""
        bar = format_pass_bar(
            self._encounter(first_heard_at=1_699_000_000.0), now=1_700_000_000.0
        )
        self.assertNotIn("PASS", bar)

    def test_a_confirmed_pass_is_named_on_the_bar(self) -> None:
        bar = format_pass_bar(
            self._encounter(pass_at=1_699_000_000.0), now=1_700_000_000.0
        )
        self.assertIn("PASS", bar)

    def test_the_node_id_is_always_printed(self) -> None:
        """The grid can show two identical-looking cells; this cannot."""
        for encounter in (
            self._encounter(),
            self._encounter(short_name=None, long_name=None),
            self._encounter(pass_at=1_699_000_000.0, hops_away=0),
        ):
            self.assertIn("!e5deef81", format_pass_bar(encounter, now=1_700_000_000.0))


if __name__ == "__main__":
    unittest.main()
