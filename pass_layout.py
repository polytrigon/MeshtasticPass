"""Pure column layout for the PASSES directory.

PASSES lists every node this radio has encountered as a DOS `dir /w`
style block: names flow LEFT TO RIGHT and then wrap to the next row,
which is what makes a long list scannable in a fixed-width terminal
without a scrollbar per column.

Kept pure and free of Textual so the layout can be tested exhaustively
-- including the emoji short names that are common on a real mesh and
are the usual cause of a column drifting out of alignment.
"""

from __future__ import annotations

from grapheme_text import cell_len, truncate_to_cells


# A pass cell is the node's display name and nothing else, so the widest
# useful name decides the column. Capped because one node with a very
# long long-name must not collapse the whole board to a single column --
# the full identity lives in the bottom bar, exactly as it does on MESH.
PASS_NAME_MAX_CELLS = 16
# Blank cells between columns. Two reads as a table; one reads as a
# wrapped sentence.
PASS_COLUMN_GUTTER = 2


def pass_column_width(names: tuple[str, ...]) -> int:
    """The cell width every column takes: the widest name, capped."""
    widest = max((cell_len(name) for name in names), default=0)
    return max(1, min(widest, PASS_NAME_MAX_CELLS))


def pass_column_count(names: tuple[str, ...], viewport_width: int) -> int:
    """How many columns fit, never fewer than one.

    A viewport too narrow for even one full column still gets one: a
    truncated name is readable, and zero columns would render nothing at
    all and look like an empty pass list rather than a narrow window.
    """
    column_width = pass_column_width(names)
    stride = column_width + PASS_COLUMN_GUTTER
    if viewport_width < column_width:
        return 1
    return max(1, (viewport_width + PASS_COLUMN_GUTTER) // stride)


def lay_out_passes(
    names: tuple[str, ...], viewport_width: int
) -> tuple[tuple[str, ...], ...]:
    """Flow `names` into padded rows, left to right then wrapping.

    Every returned cell is padded to the same DISPLAY width (cell_len,
    not len()), and over-long names are truncated grapheme-safely, so a
    ZWJ sequence, flag pair or skin-tone modifier can never be severed
    into a fragment that renders wider than accounted for and shunts the
    rest of the row sideways.

    The final row is short rather than padded with empty cells: a
    trailing run of blanks is invisible on screen but would make the
    rendered text carry meaningless whitespace, and the view iterates
    cells rather than assuming a rectangle.
    """
    if not names:
        return ()
    column_width = pass_column_width(names)
    columns = pass_column_count(names, viewport_width)
    cells = [
        _pad_to_cells(truncate_to_cells(name, column_width), column_width)
        for name in names
    ]
    return tuple(
        tuple(cells[start : start + columns])
        for start in range(0, len(cells), columns)
    )


def _pad_to_cells(value: str, width: int) -> str:
    """Right-pad to `width` DISPLAY cells (never len(), which lies about

    wide and combining characters).
    """
    return value + " " * max(0, width - cell_len(value))
