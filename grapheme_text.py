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


def protect_flag_pairs_from_wrap_severing(value: str) -> str:
    """Prevent a hard-wrap fallback from severing a regional-indicator

    flag pair.

    Word-wrap algorithms only break on whitespace; a run of non-
    whitespace text wider than the available width falls back to a raw
    per-codepoint hard wrap. Verified directly against Rich's own
    wrapping engine (rich._wrap.chop_cells -> rich.cells.
    split_graphemes): it ALREADY keeps a ZWJ sequence (e.g. a family
    emoji), a skin-tone-modified emoji, and a base character plus its
    variation selector intact through that fallback on its own -- no
    intervention needed, and none is applied here for those. The one
    confirmed gap is two adjacent regional-indicator symbols (a flag,
    e.g. "US" -> \ud83c\uddfa\ud83c\uddf8): they are NOT recognized as one unit by that
    fallback and can be split apart, leaving one line with the first
    letter and the next with the second -- an orphaned regional-
    indicator glyph renders unpredictably in a terminal and corrupts
    everything drawn after it (in CHAT, the scrollbar).

    The fix: insert a zero-width joiner between the two codepoints of
    each flag pair. Rich's own grapheme splitter explicitly keeps
    whatever follows a ZWJ attached to what came before it, so the pair
    can never be cut apart by the hard-wrap fallback again. Trade-off,
    and the reason this transform is applied ONLY to flag pairs and
    nothing else: Rich's ZWJ handling assumes -- correctly, for a real
    ZWJ emoji sequence -- that the combined glyph renders at the same
    width as the first character alone, so a protected pair's *reported*
    cell width for wrapping purposes drops from 2 to 1. A guaranteed-
    safe pairing is worth that trade: a severed flag is a certain,
    visually severe corruption; an occasional single-cell wrap
    misjudgment for a message containing flag emoji is a rare, minor
    one. Every other cluster kind (already handled correctly by Rich,
    per above) is left completely untouched.
    """
    pieces: list[str] = []
    for cluster in grapheme_clusters(value):
        if len(cluster) == 2 and all(
            ord(codepoint) in REGIONAL_INDICATOR_RANGE for codepoint in cluster
        ):
            pieces.append(cluster[0] + ZERO_WIDTH_JOINER + cluster[1])
        else:
            pieces.append(cluster)
    return "".join(pieces)
