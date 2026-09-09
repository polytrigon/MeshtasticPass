"""PASSES column layout: the DOS `dir /w` flow, measured in display cells.

The failure this file exists to prevent is a column that drifts out of
alignment because a name was measured with len() instead of its terminal
width -- which on a real mesh happens the moment somebody sets their
short name to an emoji.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grapheme_text import cell_len  # noqa: E402
from pass_layout import (  # noqa: E402
    PASS_COLUMN_GUTTER,
    PASS_NAME_MAX_CELLS,
    disambiguate_pass_names,
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


if __name__ == "__main__":
    unittest.main()


class DisambiguationTests(unittest.TestCase):
    """Meshtastic short names are not unique and never were."""

    @staticmethod
    def _encounter(node_id: str, short: str):
        from chat_store import NodeEncounter

        return NodeEncounter(node_id, None, short, None, 0.0, 0.0, None, None)

    def test_colliding_names_gain_their_node_id_tail(self) -> None:
        rows = (
            self._encounter("!e5deef81", "BIG"),
            self._encounter("!1c33b395", "BIG"),
        )
        self.assertEqual(disambiguate_pass_names(rows), ("BIG·ef81", "BIG·b395"))

    def test_unique_names_are_left_alone(self) -> None:
        """Nobody pays width for a collision they do not have."""
        rows = (
            self._encounter("!e5deef81", "ALFA"),
            self._encounter("!1c33b395", "BRVO"),
        )
        self.assertEqual(disambiguate_pass_names(rows), ("ALFA", "BRVO"))

    def test_three_way_collisions_all_disambiguate(self) -> None:
        rows = tuple(
            self._encounter(nid, "SAME") for nid in ("!aaaa1111", "!bbbb2222", "!cccc3333")
        )
        self.assertEqual(len(set(disambiguate_pass_names(rows))), 3)

    def test_emoji_names_collide_and_survive_layout(self) -> None:
        """The real case: two radios sharing one emoji short name."""
        rows = (self._encounter("!e5deef81", "\U0001f43b"), self._encounter("!1c33b395", "\U0001f43b"))
        names = disambiguate_pass_names(rows)
        self.assertNotEqual(names[0], names[1])
        grid = lay_out_passes(names, viewport_width=60)
        widths = {cell_len(cell) for row in grid for cell in row}
        self.assertEqual(len(widths), 1, f"ragged columns: {widths}")


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
