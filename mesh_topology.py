"""Pure, non-geographic layout helpers for the MESH topology board."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal
import unicodedata

from rich.cells import cell_len

from radio_service import NodeMetadata


NODE_WIDTH = 18
NODE_HEIGHT = 3
HORIZONTAL_GAP = 4
VERTICAL_GAP = 1
BOARD_MARGIN_X = 2
BOARD_MARGIN_Y = 1

Direction = Literal["up", "down", "left", "right"]


@dataclass(frozen=True)
class PositionedNode:
    """One normalized node at stable terminal-cell coordinates."""

    node: NodeMetadata
    x: int
    y: int
    layer: int | None


@dataclass(frozen=True)
class TopologyLayout:
    """A complete collision-free virtual board."""

    nodes: tuple[PositionedNode, ...]
    width: int
    height: int


def compact_node_label(node: NodeMetadata, max_cells: int = NODE_WIDTH - 2) -> str:
    """Choose a readable compact label without exposing transport records."""
    if node.is_local:
        return "YOU"
    for candidate in (node.long_name, node.short_name):
        if candidate and cell_len(candidate) <= max_cells:
            return candidate
    if node.long_name:
        return _truncate(node.long_name, max_cells)
    if node.short_name:
        return _truncate(node.short_name, max_cells)
    node_id = node.node_id.strip()
    if cell_len(node_id) <= max_cells:
        return node_id or "UNKNOWN"
    return f"…{node_id[-(max_cells - 1):]}"


def build_topology(nodes: Iterable[NodeMetadata]) -> TopologyLayout:
    """Arrange YOU centrally, then known hop layers and an outer unknown layer.

    Coordinates express topology proximity only. They never imply geography,
    routing edges, or a measured physical direction.
    """
    unique: dict[str, NodeMetadata] = {}
    for node in nodes:
        key = node.node_id.strip().lower()
        if key and key not in unique:
            unique[key] = node
    ordered = tuple(unique.values())
    local = next((node for node in ordered if node.is_local), None)
    remotes = [node for node in ordered if node is not local]
    remotes.sort(key=_node_sort_key)

    logical: list[tuple[NodeMetadata, int, int, int | None]] = []
    if local is not None:
        logical.append((local, 0, 0, 0))

    groups: list[tuple[int | None, list[NodeMetadata]]] = []
    known_hops = sorted(
        {node.hops_away for node in remotes if _valid_remote_hops(node.hops_away)}
    )
    for hops in known_hops:
        groups.append((hops, [node for node in remotes if node.hops_away == hops]))
    unknown = [node for node in remotes if not _valid_remote_hops(node.hops_away)]
    if unknown:
        groups.append((None, unknown))

    radius = 1
    for layer, group in groups:
        remaining = list(group)
        while remaining:
            positions = _square_ring(radius)
            batch, remaining = remaining[: len(positions)], remaining[len(positions) :]
            for node, (grid_x, grid_y) in zip(batch, positions):
                logical.append((node, grid_x, grid_y, layer))
            radius += 1

    if not logical:
        return TopologyLayout((), 1, 1)
    min_x = min(item[1] for item in logical)
    max_x = max(item[1] for item in logical)
    min_y = min(item[2] for item in logical)
    max_y = max(item[2] for item in logical)
    cell_width = NODE_WIDTH + HORIZONTAL_GAP
    cell_height = NODE_HEIGHT + VERTICAL_GAP
    positioned = tuple(
        PositionedNode(
            node,
            BOARD_MARGIN_X + (grid_x - min_x) * cell_width,
            BOARD_MARGIN_Y + (grid_y - min_y) * cell_height,
            layer,
        )
        for node, grid_x, grid_y, layer in logical
    )
    return TopologyLayout(
        positioned,
        BOARD_MARGIN_X * 2 + (max_x - min_x) * cell_width + NODE_WIDTH,
        BOARD_MARGIN_Y * 2 + (max_y - min_y) * cell_height + NODE_HEIGHT,
    )


def directional_target(
    current_node_id: str,
    layout: TopologyLayout,
    direction: Direction,
) -> PositionedNode | None:
    """Choose the nearest directionally sensible node without wrapping."""
    current = next(
        (item for item in layout.nodes if item.node.node_id == current_node_id),
        None,
    )
    if current is None:
        return None
    candidates: list[tuple[tuple[float, int, int, str], PositionedNode]] = []
    for candidate in layout.nodes:
        if candidate is current:
            continue
        dx = candidate.x - current.x
        dy = candidate.y - current.y
        primary, cross = _directional_distances(dx, dy, direction)
        if primary <= 0:
            continue
        score = (
            cross / primary,
            primary + cross,
            cross,
            candidate.node.node_id.lower(),
        )
        candidates.append((score, candidate))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _directional_distances(dx: int, dy: int, direction: Direction) -> tuple[int, int]:
    if direction == "up":
        return -dy, abs(dx)
    if direction == "down":
        return dy, abs(dx)
    if direction == "left":
        return -dx, abs(dy)
    return dx, abs(dy)


def _valid_remote_hops(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _node_sort_key(node: NodeMetadata) -> tuple[str, str]:
    return (compact_node_label(node).casefold(), node.node_id.casefold())


def _square_ring(radius: int) -> tuple[tuple[int, int], ...]:
    """Return stable cardinal-first points around one square ring."""
    points: list[tuple[int, int]] = []
    # Clockwise perimeter, then rotate so the four cardinal points lead.
    perimeter: list[tuple[int, int]] = []
    for x in range(-radius, radius + 1):
        perimeter.append((x, -radius))
    for y in range(-radius + 1, radius + 1):
        perimeter.append((radius, y))
    for x in range(radius - 1, -radius - 1, -1):
        perimeter.append((x, radius))
    for y in range(radius - 1, -radius, -1):
        perimeter.append((-radius, y))
    cardinal = [(0, -radius), (radius, 0), (0, radius), (-radius, 0)]
    points.extend(cardinal)
    points.extend(point for point in perimeter if point not in cardinal)
    return tuple(points)


_ZERO_WIDTH_JOINER = "‍"
_VARIATION_SELECTORS = ("︎", "️")
_EMOJI_MODIFIER_RANGE = range(0x1F3FB, 0x1F400)  # Fitzpatrick skin-tone modifiers
_REGIONAL_INDICATOR_RANGE = range(0x1F1E6, 0x1F200)  # flag letter pairs


def _attaches_to_previous(
    character: str, previous_cluster: str, join_next: bool
) -> bool:
    """Return whether `character` must stay glued to the prior grapheme cluster."""
    code_point = ord(character)
    if (
        join_next
        or character == _ZERO_WIDTH_JOINER
        or character in _VARIATION_SELECTORS
        or code_point in _EMOJI_MODIFIER_RANGE
        or unicodedata.combining(character)
    ):
        return True
    return (
        len(previous_cluster) == 1
        and ord(previous_cluster) in _REGIONAL_INDICATOR_RANGE
        and code_point in _REGIONAL_INDICATOR_RANGE
    )


def _grapheme_clusters(value: str) -> tuple[str, ...]:
    """Group codepoints so ZWJ/flag/modifier/combining sequences never split.

    Truncating raw codepoints can leave a dangling joiner, variation selector,
    skin-tone modifier, or a lone half of a flag pair. Terminals render such
    orphaned fragments unpredictably (often as an unexpectedly wide glyph),
    which is what let emoji-containing labels overflow their fixed-width node
    box and visibly corrupt neighboring layout/scrollbar rendering.
    """
    clusters: list[str] = []
    join_next = False
    for character in value:
        previous = clusters[-1] if clusters else ""
        if clusters and _attaches_to_previous(character, previous, join_next):
            clusters[-1] += character
        else:
            clusters.append(character)
        join_next = character == _ZERO_WIDTH_JOINER
    return tuple(clusters)


def _truncate(value: str, width: int) -> str:
    if cell_len(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    visible = ""
    for cluster in _grapheme_clusters(value):
        if cell_len(f"{visible}{cluster}…") > width:
            break
        visible += cluster
    return f"{visible}…"
