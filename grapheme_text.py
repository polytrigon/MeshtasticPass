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


def truncate_to_cells(value: str, width: int) -> str:
    """Grapheme-safe truncate-with-ellipsis to at most `width` display cells."""
    if cell_len(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    visible = ""
    for cluster in grapheme_clusters(value):
        if cell_len(f"{visible}{cluster}…") > width:
            break
        visible += cluster
    return f"{visible}…"


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
