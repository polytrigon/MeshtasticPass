"""Pure column layout for the PASSES directory.

PASSES lists every node this radio has encountered as a DOS `dir /w`
style block: names flow LEFT TO RIGHT and then wrap to the next row,
which is what makes a long list scannable in a fixed-width terminal
without a scrollbar per column.

Names are shown exactly as their operators set them, INCLUDING when two
nodes share one -- a mesh full of duplicate short names is the normal
state, not a data problem to paper over. An earlier version appended
each colliding node's ID tail to tell them apart, which cost every such
cell four cells of width and still could not be read on a terminal whose
emoji glyphs overpaint the character beside them. Identity lives where
there is room for it instead: the bar under the grid, and the ENTER
menu, both of which state the node ID outright.

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

# Marks a name cut short at the column cap. ASCII, and deliberately not
# "…" (U+2026), which is East_Asian_Width=AMBIGUOUS: Rich counts it as
# one cell and a terminal whose font treats that class as wide paints it
# as two, which in a fixed-width grid moves every column to its right.
#
# The general rule: node names come off the mesh and are taken as they
# are, but a character the LAYOUT adds to a cell must have a width that
# is a fact rather than a setting, and ASCII is the only width every
# terminal agrees on. Enforced by test_pass_layout.AmbiguousWidthTests.
#
# "~" is also what DOS itself used for a name that would not fit, and
# this view is imitating DOS.
PASS_TRUNCATION_MARKER = "~"


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
        _pad_to_cells(
            truncate_to_cells(name, column_width, PASS_TRUNCATION_MARKER),
            column_width,
        )
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


DEFAULT_PASS_ORDER = "recent"

# One line describing the highlighted pass, in MESH's node-bar grammar:
# fields separated by " - ", and a field with no data OMITTED rather
# than printed as a placeholder (see mesh_state's own bar, which stopped
# printing empty fields for exactly this reason -- a placeholder costs
# the same width as real information and tells you nothing).
PASS_BAR_SEPARATOR = " · "


def format_pass_bar(encounter, now: float) -> str:
    """Describe one pass on a single line.

    Says HOW the node reached us before anything else measurable,
    because that is the one thing a reader cannot recover from the grid
    alone once a cell is highlighted.
    """
    from relative_time import format_relative_age

    fields = [encounter.display_name]
    if encounter.long_name and encounter.long_name != encounter.display_name:
        fields.append(encounter.long_name)
    # The node ID always, never conditionally. Short names collide
    # constantly on a real mesh -- two radios sharing one emoji, or a
    # pair running the same default name -- and when the grid shows two
    # cells that look identical this line is the only place a reader can
    # find out whether they are one node or two.
    fields.append(encounter.node_id)
    # "MET" is reserved for a pass -- a mutual exchange between two
    # MeshtasticPass installs. What a bare radio can tell us is only
    # whether its packets reached us unrelayed, which is a fact about
    # range, so this field says HEARD, not MET. Printed as a past-tense
    # fact: a node heard directly last week and five hops away today is
    # both of those at once, not a contradiction.
    fields.append("HEARD DIRECTLY" if encounter.heard_directly else "VIA MESH")
    if encounter.has_pass:
        fields.append("PASS")
    if encounter.hops_away is not None:
        fields.append(f"HOPS {encounter.hops_away}")
    first_age = _age_or_none(encounter.first_seen_at, now, format_relative_age)
    if first_age is not None:
        fields.append(f"FIRST {first_age}")
    last_age = _age_or_none(encounter.last_seen_at, now, format_relative_age)
    if last_age is not None:
        fields.append(f"LAST {last_age}")
    return PASS_BAR_SEPARATOR.join(fields)


def _age_or_none(when, now: float, formatter):
    """Format an age, or nothing at all for an impossible timestamp.

    A future or malformed timestamp is not rendered as "0s" -- that
    would state something the data does not support. The field is simply
    absent, the same way the MESH bar omits a field it has no data for.
    """
    if not isinstance(when, (int, float)) or isinstance(when, bool):
        return None
    age = now - float(when)
    if age < 0:
        return None
    return formatter(age)


def pass_row_offset(
    total_rows: int, viewport_rows: int, selected_row: int, offset: int
) -> int:
    """The smallest scroll that keeps `selected_row` visible.

    Deliberately minimal rather than recentering: moving down one row
    should move the list by one row, not jump the selection to the
    middle of the screen. A list that re-centres under the cursor makes
    it hard to keep your place, which matters more here than elsewhere
    because every cell looks like every other cell.

    Clamped so the last screenful cannot scroll past the end into empty
    space -- the bottom of the list is the bottom of the list.
    """
    if viewport_rows <= 0 or total_rows <= 0:
        return 0
    highest = max(0, total_rows - viewport_rows)
    offset = max(0, min(offset, highest))
    if selected_row < offset:
        return selected_row
    if selected_row >= offset + viewport_rows:
        return min(selected_row - viewport_rows + 1, highest)
    return offset
