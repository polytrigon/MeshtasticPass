"""Shared grapheme-cluster-safe text helpers for terminal display width.

Used wherever text of unknown/untrusted Unicode composition -- ZWJ
sequences, skin-tone modifiers, regional-indicator flag pairs, variation
selectors, CJK/other wide characters -- must be measured, truncated, or
wrapped without a terminal ever rendering a severed half-glyph. A severed
cluster renders unpredictably (often unexpectedly wide) and can corrupt
whatever is drawn immediately to its right: a fixed-width label's
neighboring grid cell in MESH, or a scrollbar in CHAT.

Originally developed for MESH's board labels (mesh_topology.py, which
re-exports these names for backward compatibility) and now also used by
CHAT message rendering (app.py) -- kept here as the one shared
implementation rather than two independent copies of the same algorithm.
"""

from __future__ import annotations

import unicodedata

import rich.cells
from rich.cells import cell_len


ZERO_WIDTH_JOINER = "‍"
VARIATION_SELECTORS = ("︎", "️")
EMOJI_MODIFIER_RANGE = range(0x1F3FB, 0x1F400)  # Fitzpatrick skin-tone modifiers
REGIONAL_INDICATOR_RANGE = range(0x1F1E6, 0x1F200)  # flag letter pairs


# Unicode general categories that never stand alone: non-spacing marks,
# ENCLOSING marks, and spacing combining marks.
#
# unicodedata.combining() alone is not enough, and the gap is not
# theoretical: U+20E3 COMBINING ENCLOSING KEYCAP -- the third codepoint
# of every keycap emoji -- is category Me with a canonical combining
# class of ZERO, so a combining()-only test reported it as a standalone
# grapheme and split "5\ufe0f\u20e3" into two clusters. That is exactly
# the severed cluster this module exists to prevent: it can be truncated
# apart, and it made the sequence unmeasurable as a unit.
COMBINING_CATEGORIES = frozenset({"Mn", "Me", "Mc"})


def attaches_to_previous(
    character: str, previous_cluster: str, join_next: bool
) -> bool:
    """Return whether `character` must stay glued to the prior grapheme cluster."""
    code_point = ord(character)
    if (
        join_next
        or character == ZERO_WIDTH_JOINER
        or character in VARIATION_SELECTORS
        or code_point in EMOJI_MODIFIER_RANGE
        or unicodedata.combining(character)
        or unicodedata.category(character) in COMBINING_CATEGORIES
    ):
        return True
    return (
        len(previous_cluster) == 1
        and ord(previous_cluster) in REGIONAL_INDICATOR_RANGE
        and code_point in REGIONAL_INDICATOR_RANGE
    )


def grapheme_clusters(value: str) -> tuple[str, ...]:
    """Group codepoints so ZWJ/flag/modifier/combining sequences never split.

    Truncating or wrapping raw codepoints can leave a dangling joiner,
    variation selector, skin-tone modifier, or a lone half of a flag
    pair. Terminals render such orphaned fragments unpredictably (often
    as an unexpectedly wide glyph), which is what lets emoji-containing
    text overflow its intended width and visibly corrupt neighboring
    layout/scrollbar rendering.
    """
    clusters: list[str] = []
    join_next = False
    for character in value:
        previous = clusters[-1] if clusters else ""
        if clusters and attaches_to_previous(character, previous, join_next):
            clusters[-1] += character
        else:
            clusters.append(character)
        join_next = character == ZERO_WIDTH_JOINER
    return tuple(clusters)


def truncate_to_cells(value: str, width: int, marker: str = "…") -> str:
    """Grapheme-safe truncate-with-marker to at most `width` display cells.

    `marker` exists for callers laying text into a FIXED-WIDTH GRID. The
    default "…" is East_Asian_Width=AMBIGUOUS: Rich counts it as one
    cell, and a terminal configured (or fonted) to treat ambiguous-width
    characters as wide paints it as two. In flowing text that costs a
    column at the end of a line and nobody notices. In a grid it moves
    every column to its right. Such callers pass an ASCII marker, whose
    width no terminal disagrees about.
    """
    if cell_len(value) <= width:
        return value
    marker_width = cell_len(marker)
    if width <= marker_width:
        return marker[:width]
    visible = ""
    for cluster in grapheme_clusters(value):
        if cell_len(f"{visible}{cluster}{marker}") > width:
            break
        visible += cluster
    return f"{visible}{marker}"


def _merge_regional_indicator_spans(
    spans: list[tuple[int, int, int]], text: str
) -> list[tuple[int, int, int]]:
    """Combine two adjacent single-codepoint regional-indicator spans

    (a flag pair, e.g. U+1F1FA U+1F1F8) into one span covering both.

    Mirrors how Rich's own split_graphemes already merges a ZWJ
    sequence or a variation-selector pair into a single span -- the
    same treatment, extended to the one cluster kind Rich's own
    implementation does not yet cover. The merged span's cell length is
    the sum of the two constituent spans', so total cell_len() output
    for the string is unchanged; only the span boundary between the two
    codepoints is removed, so a hard-wrap fold can no longer land
    between them.
    """
    merged: list[tuple[int, int, int]] = []
    index = 0
    total = len(spans)
    while index < total:
        start, end, cell_length = spans[index]
        if (
            index + 1 < total
            and end - start == 1
            and ord(text[start]) in REGIONAL_INDICATOR_RANGE
        ):
            next_start, next_end, next_cell_length = spans[index + 1]
            if next_end - next_start == 1 and ord(text[next_start]) in REGIONAL_INDICATOR_RANGE:
                merged.append((start, next_end, cell_length + next_cell_length))
                index += 2
                continue
        merged.append((start, end, cell_length))
        index += 1
    return merged


def install_flag_pair_protection() -> None:
    """Teach Rich's grapheme splitter that a regional-indicator flag

    pair is one indivisible unit, so its own hard-wrap fallback (used
    for a run of non-whitespace text wider than the available render
    width) can never sever the two codepoints of a flag onto separate
    lines. An orphaned regional-indicator glyph renders unpredictably
    in a terminal and corrupts whatever is drawn to its right -- in
    CHAT, the scrollbar.

    Verified directly against Rich's real wrapping engine
    (rich._wrap.divide_line -> rich.cells.chop_cells ->
    rich.cells.split_graphemes, the same path Textual's Static/Content
    rendering uses): split_graphemes already merges a ZWJ sequence
    (e.g. a family emoji), a skin-tone modifier, and a base character
    plus its variation selector into one non-severable span on its own
    -- no help needed, and none is applied here for those. Its one
    confirmed gap is two adjacent regional-indicator symbols. This
    patches that single gap, at the one place both Rich's own wrapping
    and Textual's widget rendering resolve split_graphemes from
    (chop_cells looks it up as a bare name in the rich.cells module
    namespace at call time, so patching the module attribute here
    reaches every caller).

    This changes ONLY where wrap breaks may fall -- never the text
    itself. No caller's string is mutated, no synthetic character is
    ever inserted, and the reported cell_len() of any string is
    unchanged (cell_len() itself is not patched; the merge preserves
    the original spans' combined width). Idempotent: calling this more
    than once (e.g. across multiple app instances in tests) re-wraps
    the same underlying original only once.
    """
    current = rich.cells.split_graphemes
    if getattr(current, "_flag_pair_protected", False):
        return
    original = current

    def _split_graphemes_with_flag_pairs(
        text: str, unicode_version: str = "auto"
    ) -> "tuple[list[tuple[int, int, int]], int]":
        spans, total_width = original(text, unicode_version)
        return _merge_regional_indicator_spans(spans, text), total_width

    _split_graphemes_with_flag_pairs._flag_pair_protected = True  # type: ignore[attr-defined]
    rich.cells.split_graphemes = _split_graphemes_with_flag_pairs


COMBINING_ENCLOSING_KEYCAP = "⃣"

# A "keycap" emoji (e.g. the boxed/keycap-style "5" in a message: digit
# "5" + VARIATION SELECTOR-16 + COMBINING ENCLOSING KEYCAP) is one of the
# few emoji sequences whose *declared* cell width depends on a rule most
# minimal terminal fonts do not implement. Confirmed directly against
# Rich's own width table (rich.cells._cell_len, the function Textual's
# Strip -- and so every wrap/height/virtual-size calculation -- resolves
# its cell counts from): a bare digit is narrow (1 cell) and the
# COMBINING ENCLOSING KEYCAP mark is zero-width on its own, but the
# presence of VARIATION SELECTOR-16 (U+FE0F) triggers a specific
# "narrow_to_wide" bump that promotes the preceding digit to 2 cells --
# i.e. Rich models the fully-qualified sequence as one indivisible,
# self-consistently-measured 2-cell-wide unit, per the Unicode emoji
# variation-sequence convention. That convention assumes a terminal font
# with a real combined keycap glyph. A plain monospace terminal font
# without one typically falls back to rendering one or more of the
# individual codepoints as its own visible placeholder glyph -- so the
# number of columns the terminal actually paints can diverge from the 2
# cells Textual accounted for. Textual's own layout (wrap points, this
# entry's rendered height, the transcript's virtual size, the
# scrollbar's thumb/track math) all trust the accounted width, so any
# such divergence corrupts everything drawn after the mismatch, not just
# the emoji glyph itself -- in CHAT, that means the scrollbar.
#
# This USED to be fixed by substituting the single-codepoint CIRCLED
# DIGIT at the display boundary (terminal_safe_text()). That is gone.
#
# The app now MEASURES its own terminal at startup and corrects Rich's
# width table to match what the font actually paints (terminal_width.py),
# so the accounted width no longer disagrees and there is nothing left to
# substitute around. Keeping the substitution cost correctness: real
# traffic on this mesh is full of keycaps -- people count off with them --
# and every one rendered as the wrong glyph, so a message reading "2" in
# a box arrived looking like a circled 2. It was not even width-neutral,
# since CIRCLED DIGIT is itself East_Asian_Width=AMBIGUOUS.
#
# COMBINING_ENCLOSING_KEYCAP above is kept as the named codepoint these
# notes and the tests refer to. Nothing here matches on it any more:
# attaches_to_previous() keeps a keycap whole across a wrap boundary via
# the mark's Unicode CATEGORY (Me, in COMBINING_CATEGORIES), which is
# the general rule and needs no per-emoji special case.
