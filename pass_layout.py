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
there is room for it instead: the bar under the grid, which states the
node ID for the highlighted cell unconditionally (see format_pass_bar).

Kept pure and free of Textual so the layout can be tested exhaustively
-- including the emoji short names that are common on a real mesh and
are the usual cause of a column drifting out of alignment.
"""

from __future__ import annotations

from typing import Callable

from grapheme_text import cell_len, grapheme_clusters


# How wide a piece of text is. Defaults to cell_len -- the width Unicode
# DECLARES -- everywhere, so every caller that does not care behaves
# exactly as it always has. The app passes a MEASURED width instead (see
# terminal_width.PaintedWidths), because a terminal with no glyph for an
# emoji advances one column where Rich accounted for two, and in a grid
# that error moves every column to its right for the rest of the row.
Measure = Callable[[str], int]


# A pass cell is the node's display name and nothing else, so the widest
# useful name decides the column. Capped because one node with a very
# long long-name must not collapse the whole board to a single column --
# the full identity lives in the bottom bar, exactly as it does on MESH.
PASS_NAME_MAX_CELLS = 16

# The share of names a column is sized to fit whole. The rest are
# truncated (see PASS_TRUNCATION_MARKER) and read in full from the bar.
#
# Sizing to the WIDEST name instead lets one outlier set the stride for
# the entire board: a node with no short name falls back to its long
# name, and a single 13-cell "No Short Name" turned a thirteen-column
# board into a four-column one with enormous gaps. That is the wrong
# trade in a directory -- people come here to scan the list, not to read
# its one longest entry, and the bar under the grid already gives that
# entry in full the moment it is highlighted.
#
# Only bites once there are enough names for a distribution to mean
# anything: below roughly twenty, the quantile IS the maximum and every
# name still fits whole.
PASS_NAME_TYPICAL_QUANTILE = 0.9
# The MINIMUM blank cells between columns, and so what decides how many
# columns fit. One reads as a wrapped sentence; two leave no room for a
# glyph painted wider than the box it advances, which this board has.
# Four separates one name from another on a board whose columns are four
# or five cells wide.
#
# The gutter actually drawn is usually wider: see pass_gutter, which
# spends whatever the column count left over rather than banking it as a
# margin on the right. None of this FIXES a glyph whose advance is wrong
# -- that error accumulates along a row and no gutter removes it (see
# terminal_width) -- it only keeps names from crowding each other.
PASS_COLUMN_GUTTER = 4

# Ceiling on the distributed gutter. Past this the eye stops reading a
# row as a row and starts reading each name as its own island, which is
# the opposite of what a `dir /w` block is for.
PASS_MAX_COLUMN_GUTTER = 8

# Columns held back from the number that would fit. The rightmost column
# has no gutter after it and nothing between it and the edge of the
# screen, so it is where overspilling ink has nowhere to go and where a
# row that has drifted shows up worst. One column of names is a cheap
# price for a board that ends in air.
PASS_COLUMNS_RESERVED = 1

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


def pass_column_width(names: tuple[str, ...], measure: Measure = cell_len) -> int:
    """The width every column takes: what MOST names need, capped.

    Sized to PASS_NAME_TYPICAL_QUANTILE of the names rather than to the
    widest one, so a single long name cannot set the stride for the
    whole board (see that constant). Names above it are truncated, and
    the bar reads them out in full.

    Measured in columns the terminal really advances rather than in the
    width Unicode declares (see terminal_width), because this number's
    whole job is to be the stride between two things a person sees side
    by side.
    """
    widths = sorted(measure(name) for name in names)
    if not widths:
        return 1
    index = min(len(widths) - 1, int(len(widths) * PASS_NAME_TYPICAL_QUANTILE))
    return max(1, min(widths[index], PASS_NAME_MAX_CELLS))


def pass_column_count(
    names: tuple[str, ...], viewport_width: int, measure: Measure = cell_len
) -> int:
    """How many columns fit, never fewer than one.

    A viewport too narrow for even one full column still gets one: a
    truncated name is readable, and zero columns would render nothing at
    all and look like an empty pass list rather than a narrow window.
    The same floor applies after PASS_COLUMNS_RESERVED is taken off, so
    a narrow window loses names rather than becoming blank.
    """
    column_width = pass_column_width(names, measure)
    stride = column_width + PASS_COLUMN_GUTTER
    if viewport_width < column_width:
        return 1
    fits = (viewport_width + PASS_COLUMN_GUTTER) // stride
    return max(1, fits - PASS_COLUMNS_RESERVED)


def pass_gutter(
    names: tuple[str, ...], viewport_width: int, measure: Measure = cell_len
) -> int:
    """The gutter to actually draw: the minimum, widened to fill the width.

    The column COUNT is decided at PASS_COLUMN_GUTTER, and whatever width
    that leaves over is spent on the gaps rather than banked as one large
    margin on the right. A board of four-cell names is mostly gutter
    anyway, so this is the difference between names spread across the
    screen and names bunched at the left of it.

    Never narrower than PASS_COLUMN_GUTTER, so widening can only ever
    make a row fit less tightly than the count assumed -- it cannot push
    a column off the edge.
    """
    columns = pass_column_count(names, viewport_width, measure)
    if columns < 2:
        return PASS_COLUMN_GUTTER
    column_width = pass_column_width(names, measure)
    spare = viewport_width - columns * column_width
    return max(
        PASS_COLUMN_GUTTER,
        min(PASS_MAX_COLUMN_GUTTER, spare // (columns - 1)),
    )


def lay_out_passes(
    names: tuple[str, ...], viewport_width: int, measure: Measure = cell_len
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
    column_width = pass_column_width(names, measure)
    cells = [
        _pad_to_cells(_truncate(name, column_width, measure), column_width, measure)
        for name in names
    ]
    columns = pass_column_count(names, viewport_width, measure)
    gutter = pass_gutter(names, viewport_width, measure)
    # Cells are padded to their MEASURED width while Rich and Textual
    # crop this text by its DECLARED one, so a row holding a glyph the
    # terminal paints narrow measures wider to them than it draws. Rather
    # than reason about how much slack a particular viewport leaves, check
    # and drop a column -- what Textual would crop is the right-hand end
    # of the last name on the row.
    while columns > 1 and _widest_declared_row(cells, columns, gutter) > viewport_width:
        columns -= 1
    return tuple(
        tuple(cells[start : start + columns])
        for start in range(0, len(cells), columns)
    )


def _widest_declared_row(cells: list[str], columns: int, gutter: int) -> int:
    """The widest row's width in the units Rich crops by (cell_len)."""
    return max(
        (
            sum(cell_len(cell) for cell in cells[start : start + columns])
            + gutter * (len(cells[start : start + columns]) - 1)
            for start in range(0, len(cells), columns)
        ),
        default=0,
    )


def _truncate(value: str, width: int, measure: Measure) -> str:
    """Grapheme-safe truncate to at most `width` cells, as MEASURED.

    Same job as grapheme_text.truncate_to_cells, in the same units as
    the padding beside it. A name capped by declared width could still
    paint short and leave its cell under-filled, which is the drift this
    module exists to prevent, only smaller.
    """
    if measure(value) <= width:
        return value
    marker_width = measure(PASS_TRUNCATION_MARKER)
    if width <= marker_width:
        return PASS_TRUNCATION_MARKER[:width]
    visible = ""
    for cluster in grapheme_clusters(value):
        if measure(f"{visible}{cluster}{PASS_TRUNCATION_MARKER}") > width:
            break
        visible += cluster
    return f"{visible}{PASS_TRUNCATION_MARKER}"


def _pad_to_cells(value: str, width: int, measure: Measure = cell_len) -> str:
    """Right-pad so the terminal advances `width` columns.

    Three different widths are in play and only one of them keeps a
    column of names lined up: len() lies about wide characters, cell_len
    tells the truth about what Unicode DECLARES, and `measure` says what
    the font in front of the user actually draws. The padding is spaces,
    which every terminal agrees are one column, so the arithmetic holds.
    """
    return value + " " * max(0, width - measure(value))


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


def scroll_window_offset(
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


def scroll_window_step(
    total: int, visible: int, index: int, offset: int, direction: int
) -> tuple[int, int]:
    """Move `index` by `direction`, wrapping, and follow with the window.

    Returns (index, offset). Pure, and separate from the widget that
    calls it, because a widget cannot be constructed outside a running
    Textual app -- so state-machine rules living inside one can only be
    tested by standing up a whole app, which is slow and hides the rule
    among the scaffolding.
    """
    if total <= 0:
        return 0, 0
    index = (index + direction) % total
    return index, scroll_window_offset(total, visible, index, offset)


# The PASSES grid's own name for it, kept because that is what the view
# and its tests call. The rule is not grid-specific -- the CHAT emoji
# picker scrolls its strip by exactly the same one -- so the general
# name is the implementation and this is the alias.
pass_row_offset = scroll_window_offset
