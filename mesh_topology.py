"""Pure, non-geographic layout helpers for the MESH topology board."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal
import unicodedata

from rich.cells import cell_len

from geo import bearing_and_distance
from radio_service import NodeMetadata


NODE_WIDTH = 18
NODE_HEIGHT = 3
HORIZONTAL_GAP = 4
VERTICAL_GAP = 1
BOARD_MARGIN_X = 2
BOARD_MARGIN_Y = 1
DEFAULT_MAX_GRID_RADIUS = 3

Direction = Literal["up", "down", "left", "right"]

# Compass bucket -> unit vector in logical grid space. y increases downward
# (terminal convention), so north is negative y.
_DIRECTION_VECTORS: dict[str, tuple[int, int]] = {
    "N": (0, -1),
    "NE": (1, -1),
    "E": (1, 0),
    "SE": (1, 1),
    "S": (0, 1),
    "SW": (-1, 1),
    "W": (-1, 0),
    "NW": (-1, -1),
}
_DIRECTION_ORDER = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


@dataclass(frozen=True)
class PositionedNode:
    """One normalized node at stable terminal-cell coordinates.

    `region` records why the node landed where it did: "LOCAL" for YOU, one
    of the eight compass buckets when trustworthy position data placed it
    directionally, or "UNKNOWN" when no trustworthy bearing was available.
    """

    node: NodeMetadata
    x: int
    y: int
    region: str


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


def build_topology(
    nodes: Iterable[NodeMetadata],
    *,
    max_radius: int = DEFAULT_MAX_GRID_RADIUS,
) -> TopologyLayout:
    """Arrange YOU centrally, then a bounded relative spatial grid outward.

    A remote node is placed by compass direction only when both YOU and that
    node have trustworthy position data; distance only orders nodes outward
    within their direction bucket as a discrete grid-step rank, clamped to
    `max_radius` so one very distant node can never explode the board -- it
    is never proportional to real distance. Nodes without a resolvable
    bearing are placed deterministically around the outer ring instead of
    being mixed into the directional grid with a fabricated direction.
    Coordinates never imply literal geography, routing edges, or exact scale.
    The result is bounded by construction (the caller is expected to pass a
    working set of a handful of nodes, not the full node database), so there
    is no scrolling: the whole board is meant to fit one viewport.
    """
    max_radius = max(1, max_radius)
    unique: dict[str, NodeMetadata] = {}
    for node in nodes:
        key = node.node_id.strip().lower()
        if key and key not in unique:
            unique[key] = node
    ordered = tuple(unique.values())
    local = next((node for node in ordered if node.is_local), None)
    remotes = [node for node in ordered if node is not local]

    logical: list[tuple[NodeMetadata, int, int, str]] = []
    occupied: set[tuple[int, int]] = set()
    if local is not None:
        logical.append((local, 0, 0, "LOCAL"))
        occupied.add((0, 0))

    local_position = local.position if local is not None else None
    buckets: dict[str, list[tuple[float, NodeMetadata]]] = {}
    unknown: list[NodeMetadata] = []
    for node in remotes:
        resolved = bearing_and_distance(local_position, node.position)
        if resolved is None:
            unknown.append(node)
            continue
        bearing, distance = resolved
        buckets.setdefault(_direction_bucket(bearing), []).append((distance, node))

    for direction in _DIRECTION_ORDER:
        entries = buckets.get(direction)
        if not entries:
            continue
        entries.sort(key=lambda item: (item[0], item[1].node_id.casefold()))
        dx, dy = _DIRECTION_VECTORS[direction]
        for rank, (_distance, node) in enumerate(entries, start=1):
            clamped_rank = min(rank, max_radius)
            slot_x, slot_y = _reserve_slot(dx * clamped_rank, dy * clamped_rank, occupied)
            logical.append((node, slot_x, slot_y, direction))

    unknown.sort(key=_node_sort_key)
    if unknown:
        outer_ring = _square_ring(max_radius)
        for index, node in enumerate(unknown):
            grid_x, grid_y = outer_ring[index % len(outer_ring)]
            slot_x, slot_y = _reserve_slot(grid_x, grid_y, occupied)
            logical.append((node, slot_x, slot_y, "UNKNOWN"))

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
            region,
        )
        for node, grid_x, grid_y, region in logical
    )
    return TopologyLayout(
        positioned,
        BOARD_MARGIN_X * 2 + (max_x - min_x) * cell_width + NODE_WIDTH,
        BOARD_MARGIN_Y * 2 + (max_y - min_y) * cell_height + NODE_HEIGHT,
    )


def _direction_bucket(bearing: float) -> str:
    """Map a compass bearing to one of eight 45-degree direction buckets."""
    index = int(((bearing % 360.0) + 22.5) // 45.0) % 8
    return _DIRECTION_ORDER[index]


def _reserve_slot(
    x: int, y: int, occupied: set[tuple[int, int]]
) -> tuple[int, int]:
    """Claim (x, y), or the nearest unclaimed slot if it is already taken."""
    if (x, y) not in occupied:
        occupied.add((x, y))
        return x, y
    radius = 1
    while radius < 4096:
        for dx, dy in _square_ring(radius):
            candidate = (x + dx, y + dy)
            if candidate not in occupied:
                occupied.add(candidate)
                return candidate
        radius += 1
    occupied.add((x, y))  # pragma: no cover - unreachable in practice
    return x, y


def route_connector(
    from_x: int, from_y: int, to_x: int, to_y: int
) -> tuple[tuple[int, int, str], ...]:
    """Return (x, y, glyph) cells for a simple orthogonal YOU-to-node connector.

    A horizontal run along the origin's row, then a vertical run along the
    target's column -- one elbow at most -- degenerating to a straight
    segment when the two points already share a row or column. Endpoints
    are excluded: a node widget renders on top of and occludes its own
    cell regardless of what the background layer drew underneath it.
    """
    if from_x == to_x and from_y == to_y:
        return ()
    cells: list[tuple[int, int, str]] = []
    step_x = 1 if to_x > from_x else -1
    step_y = 1 if to_y > from_y else -1

    if from_y == to_y:
        return tuple((x, from_y, "─") for x in range(from_x + step_x, to_x, step_x))
    if from_x == to_x:
        return tuple((from_x, y, "│") for y in range(from_y + step_y, to_y, step_y))

    for x in range(from_x + step_x, to_x, step_x):
        cells.append((x, from_y, "─"))
    cells.append((to_x, from_y, _elbow_glyph(step_x, step_y)))
    for y in range(from_y + step_y, to_y, step_y):
        cells.append((to_x, y, "│"))
    return tuple(cells)


def _elbow_glyph(step_x: int, step_y: int) -> str:
    """Box-drawing corner for a horizontal run turning into a vertical one."""
    if step_x > 0:
        return "┐" if step_y > 0 else "┘"
    return "┌" if step_y > 0 else "└"


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
