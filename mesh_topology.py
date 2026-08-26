"""Pure, non-geographic layout helpers for the MESH topology board."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Iterable, Literal, Mapping

from rich.cells import cell_len

from geo import GeoPosition, bearing_and_distance, project_local_plane
from grapheme_text import (
    EMOJI_MODIFIER_RANGE as _EMOJI_MODIFIER_RANGE,
    REGIONAL_INDICATOR_RANGE as _REGIONAL_INDICATOR_RANGE,
    VARIATION_SELECTORS as _VARIATION_SELECTORS,
    ZERO_WIDTH_JOINER as _ZERO_WIDTH_JOINER,
    attaches_to_previous as _attaches_to_previous,
    grapheme_clusters as _grapheme_clusters,
    truncate_to_cells as _truncate,
)
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


@dataclass(frozen=True)
class RelayStage:
    """An anonymous, unidentified intermediate relay hop on one real
    client's observed path back to YOU -- see build_relay_stages().

    Meshtastic gives this app a hop COUNT for a remote node, never the
    identities of the nodes that actually relayed its traffic (see
    mesh_state.MeshNodeState's docstring). A RelayStage represents
    exactly "an intermediate relay stage existed here" -- never "we
    discovered another Meshtastic node" -- so it deliberately carries no
    name, no ID resembling a real node's, no position, no activity, and
    no role.

    `node_id` is an internal-only synthetic identifier
    ("relay:<source_node_id>:<index>") used purely for rendering,
    topology, and connector bookkeeping; it must never be shown to the
    user. Anonymous relay stages are visual topology only -- they
    cannot be selected, focused, or navigated to (see
    MeshTopologyView.select_node() and app.py's _move_mesh_focus, which
    excludes every RelayStage ID from the navigation candidate set), so
    this ID never appears as `selected_node_id` and never drives the
    bottom-left context line; a genuinely identified real RELAY-only
    node (should trustworthy evidence for one ever exist) would instead
    use the standard remote-node context line with ROLE=RELAY. `is_local`
    is always False, present only so a RelayStage can be duck-typed
    alongside NodeMetadata (both expose node_id/is_local) by generic,
    node-kind-agnostic rendering/connector code. `source_node_id` is the
    real client this stage belongs to;
    `index` is this stage's 1-indexed position counting outward from
    YOU (1 = nearest YOU, N = nearest the client for an N-hop client).
    Together, source_node_id and index guarantee that two different
    clients -- even ones truthfully reporting the identical hop count --
    always get numerically independent stages that are never merged or
    treated as the same physical relay (see build_relay_stages and
    "MULTIPLE CLIENT PATHS" in the MESH spec).
    """

    node_id: str
    source_node_id: str
    index: int
    is_local: bool = False


def build_relay_stages(
    real_positions: Mapping[str, tuple[int, int]],
    *,
    you_id: str,
    hop_counts: Mapping[str, int],
    row_count: int,
    column_count: int,
) -> tuple[tuple[RelayStage, ...], dict[str, tuple[int, int]]]:
    """Generate anonymous relay-stage placeholders for every real CLIENT

    with a trustworthy, nonzero hop count, and place them on interior
    grid cells along the straight line between YOU and that client.

    Exactly `hop_counts[client_id]` stages are generated per client --
    never fewer, never more (a `? HOPS` client -- absent from
    `hop_counts` entirely -- gets none: an unknown hop count must never
    be treated as zero or imply any specific path depth). A client's
    stages always carry that client's own node_id baked into their
    synthetic node_id, so two clients reporting the same hop count are
    never merged into shared stages even if a placement collision seats
    them on the same or an adjacent cell.

    Placement is a straight-line interpolation between YOU's and the
    client's already-fixed grid cell, rounded to the nearest integer
    grid step -- geography sets the rough direction the chain extends
    in, never its exact route. Collisions with real nodes, YOU, or
    another client's stages are resolved with the same bounded
    ring-search-then-full-grid-scan fallback place_within_bounds uses
    (_reserve_bounded_cell), so a stage is never silently dropped as
    long as the fixed grid has any free cell left. Clients are processed
    in deterministic node_id order so placement never depends on
    working-set iteration/arrival order.
    """
    you_position = real_positions.get(you_id)
    if you_position is None:
        return (), {}
    occupied: set[tuple[int, int]] = set(real_positions.values())
    stages: list[RelayStage] = []
    positions: dict[str, tuple[int, int]] = {}
    for client_id in sorted(hop_counts):
        count = hop_counts[client_id]
        client_position = real_positions.get(client_id)
        if count <= 0 or client_position is None:
            continue
        for step in range(1, count + 1):
            fraction = step / (count + 1)
            row = round(
                you_position[0] + (client_position[0] - you_position[0]) * fraction
            )
            column = round(
                you_position[1] + (client_position[1] - you_position[1]) * fraction
            )
            row, column = _reserve_bounded_cell(
                _clamp(row, 1, row_count),
                _clamp(column, 1, column_count),
                occupied,
                row_count=row_count,
                column_count=column_count,
            )
            occupied.add((row, column))
            node_id = f"relay:{client_id}:{step}"
            stages.append(RelayStage(node_id, client_id, step))
            positions[node_id] = (row, column)
    return tuple(stages), positions


def route_chain(
    points: tuple[tuple[int, int], ...]
) -> tuple[tuple[int, int, str], ...]:
    """Concatenate route_connector() across consecutive (x, y) pixel points.

    Draws a multi-segment YOU -> relay -> relay -> ... -> node connector
    chain as one continuous path, each segment routed by the same
    single-elbow orthogonal rule as a direct two-point connector. A
    two-point chain (a 0-hop client, or any direct link) degenerates to
    exactly what route_connector() alone would draw.
    """
    cells: list[tuple[int, int, str]] = []
    for start, end in zip(points, points[1:]):
        cells.extend(route_connector(start[0], start[1], end[0], end[1]))
    return tuple(cells)


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


def _project_remote_gps_cluster(
    nodes: Iterable[NodeMetadata], *, max_radius: int
) -> dict[str, tuple[int, int]]:
    """MODE B: when YOU has no GPS, project 2+ GPS-equipped remote nodes

    into shared local grid-step coordinates that preserve their
    geography RELATIVE TO EACH OTHER -- north/south, east/west, and
    approximate relative separation -- since that is real, known
    information independent of anything about YOU.

    This NEVER assigns YOU a position and NEVER assumes YOU sits at the
    cluster's centroid -- it is not triangulation of YOU's location.
    YOU remains an independently placed visual/topology origin (see
    assign_grid_slots, which always places YOU at logical (0, 0)
    regardless of this projection's results). Returns {} for fewer than
    2 positioned nodes: a single GPS point has no relative geometry of
    its own to preserve, so that caller falls through to the ordinary
    deterministic fallback instead (see MODE B's single-remote rule).

    The reference point for geo.project_local_plane() is this cluster's
    own mean latitude/longitude -- a representative anchor for the
    projection math, again never a stand-in for YOU's position. The
    projected cluster is then normalized with a SINGLE uniform scale
    factor on both axes (never independently stretched per axis, which
    would distort real shape/aspect ratio just to fill more of the
    grid), chosen so that the single POINT FARTHEST from the shared
    local origin -- not merely half the bounding box's raw span, which
    understates the true maximum offset whenever the cluster isn't
    symmetric around its own mean -- lands at exactly `max_radius` grid
    steps out; every other point is guaranteed to land at or within
    that same radius. Coordinates are then rounded to integer grid
    steps. Collision avoidance and any hop-based radius boost are the
    caller's job, exactly as for every other coordinate this module
    produces.
    """
    positioned = [(node, node.position) for node in nodes if node.position is not None]
    if len(positioned) < 2:
        return {}
    reference = GeoPosition(
        sum(position.latitude for _, position in positioned) / len(positioned),
        sum(position.longitude for _, position in positioned) / len(positioned),
    )
    local_xy = {
        node.node_id: project_local_plane(reference, position)
        for node, position in positioned
    }
    max_abs_offset = max(
        max(abs(x), abs(y)) for x, y in local_xy.values()
    )
    if max_abs_offset <= 0:
        # Degenerate cluster (all effectively coincident) -- no
        # meaningful spread to preserve; every node starts at the
        # shared origin and the caller's ordinary collision avoidance
        # separates them exactly as it would for any other coincident
        # placement.
        return {node_id: (0, 0) for node_id in local_xy}
    scale = max_radius / max_abs_offset
    return {
        # North (higher latitude) is negative grid-y; east (higher
        # longitude) is positive grid-x -- matching _DIRECTION_VECTORS'
        # existing convention ("y increases downward").
        node_id: (round(x * scale), round(-y * scale))
        for node_id, (x, y) in local_xy.items()
    }


def _apply_min_radius(x: int, y: int, min_radius: int) -> tuple[int, int]:
    """Push (x, y) outward to at least `min_radius` from the origin,

    preserving its exact direction/angle -- never rotating a node or
    reassigning which side of the board it falls on, only extending its
    distance when relay-chain room genuinely requires it (see
    assign_grid_slots' min_radius_by_id). Geography (or, in MODE B,
    relative geography) sets the direction; hop depth may only push a
    node farther out along that same direction, never redefine it.
    """
    if min_radius <= 0:
        return x, y
    current = sqrt(x * x + y * y)
    if current >= min_radius:
        return x, y
    if current == 0:
        return (0, -min_radius)
    scale = min_radius / current
    return (round(x * scale), round(y * scale))


def assign_grid_slots(
    nodes: Iterable[NodeMetadata],
    *,
    max_radius: int = DEFAULT_MAX_GRID_RADIUS,
    min_radius_by_id: Mapping[str, int] | None = None,
) -> tuple[PositionedNode, ...]:
    """Place YOU at (0, 0), then a bounded relative spatial grid outward.

    Pure logical grid-STEP placement (not pixel/cell coordinates): a
    remote node is placed by compass direction only when both YOU and
    that node have trustworthy position data; distance only orders nodes
    outward within their direction bucket as a discrete grid-step rank,
    clamped to `max_radius` so one very distant node can never explode
    the board -- it is never proportional to real distance. Coordinates
    never imply literal geography, routing edges, or exact scale.

    MODE B: when YOU has no trustworthy position but 2+ remotes do,
    those remotes are placed instead via _project_remote_gps_cluster(),
    preserving their geography RELATIVE TO EACH OTHER without assuming
    or fabricating a position for YOU (see that function's docstring --
    this is never triangulation of YOU's location). A lone GPS remote
    with no other positioned remote to be relative to, and every node
    with no position at all, falls back to the deterministic outer-ring
    placement below instead of being mixed into either directional
    scheme with a fabricated direction.

    `min_radius_by_id` (default: none) raises a specific node's distance
    from the origin to at least the given value, overriding `max_radius`
    for that node only -- everything else about its placement (compass
    direction or, in MODE B, relative-geography direction) is unchanged;
    see _apply_min_radius. Every other caller, and every node not named
    in the mapping, sees identical behavior to before this parameter
    existed. MESH's real-data pipeline uses this so a real client with a
    truthful nonzero hop count is placed far enough from YOU to leave
    room for its own anonymous relay-stage placeholders as genuinely
    interior cells (see build_relay_stages) -- hop depth, not just
    geography, legitimately extends how far out a node's chain (and so
    the node itself) renders, without changing its actual direction.

    This is the shared core reused both by build_topology() (which scales
    these steps into pixel positions for its own auto-sized board) and by
    any caller mapping them directly onto a different, e.g. fixed-size,
    grid.
    """
    max_radius = max(1, max_radius)
    min_radius_by_id = min_radius_by_id or {}
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
    # MODE B: YOU has no trustworthy position -- if 2+ remotes do, place
    # them by their own relative geography instead of falling back to
    # the fabricated-direction-free outer ring (see
    # _project_remote_gps_cluster). A single positioned remote has no
    # relative geometry to preserve on its own, so it (and every
    # unpositioned remote) still falls through to that same outer ring,
    # exactly as before this mode existed.
    gps_cluster_positions: dict[str, tuple[int, int]] = {}
    if local_position is None:
        gps_remotes = [node for node in remotes if node.position is not None]
        if len(gps_remotes) >= 2:
            gps_cluster_positions = _project_remote_gps_cluster(
                gps_remotes, max_radius=max_radius
            )

    buckets: dict[str, list[tuple[float, NodeMetadata]]] = {}
    unknown: list[NodeMetadata] = []
    for node in remotes:
        if node.node_id in gps_cluster_positions:
            continue
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
            clamped_rank = max(
                min(rank, max_radius), min_radius_by_id.get(node.node_id, 0)
            )
            slot_x, slot_y = _reserve_slot(dx * clamped_rank, dy * clamped_rank, occupied)
            logical.append((node, slot_x, slot_y, direction))

    # MODE B placement -- reserved BEFORE the unpositioned-fallback loop
    # below, so those nodes route around the GPS cluster rather than the
    # other way around (GPS positions are the higher-priority
    # reservation; see _project_remote_gps_cluster). Deterministic
    # node_id order keeps this arrival-order independent, matching every
    # other placement loop in this function.
    for node in sorted(
        (node for node in remotes if node.node_id in gps_cluster_positions),
        key=lambda node: node.node_id.casefold(),
    ):
        raw_x, raw_y = gps_cluster_positions[node.node_id]
        boosted_x, boosted_y = _apply_min_radius(
            raw_x, raw_y, min_radius_by_id.get(node.node_id, 0)
        )
        slot_x, slot_y = _reserve_slot(boosted_x, boosted_y, occupied)
        logical.append((node, slot_x, slot_y, "GPS_RELATIVE"))

    unknown.sort(key=_node_sort_key)
    if unknown:
        outer_ring = _square_ring(max_radius)
        for index, node in enumerate(unknown):
            grid_x, grid_y = outer_ring[index % len(outer_ring)]
            slot_x, slot_y = _reserve_slot(grid_x, grid_y, occupied)
            logical.append((node, slot_x, slot_y, "UNKNOWN"))

    return tuple(
        PositionedNode(node, grid_x, grid_y, region)
        for node, grid_x, grid_y, region in logical
    )


def build_topology(
    nodes: Iterable[NodeMetadata],
    *,
    max_radius: int = DEFAULT_MAX_GRID_RADIUS,
) -> TopologyLayout:
    """Arrange YOU centrally, then a bounded relative spatial grid outward,

    scaled into pixel/cell coordinates for an auto-sized board. See
    assign_grid_slots() for the pure logical placement this scales.
    The result is bounded by construction (the caller is expected to pass
    a working set of a handful of nodes, not the full node database), so
    there is no scrolling: the whole board is meant to fit one viewport.
    """
    slots = assign_grid_slots(nodes, max_radius=max_radius)
    if not slots:
        return TopologyLayout((), 1, 1)
    min_x = min(item.x for item in slots)
    max_x = max(item.x for item in slots)
    min_y = min(item.y for item in slots)
    max_y = max(item.y for item in slots)
    cell_width = NODE_WIDTH + HORIZONTAL_GAP
    cell_height = NODE_HEIGHT + VERTICAL_GAP
    positioned = tuple(
        PositionedNode(
            item.node,
            BOARD_MARGIN_X + (item.x - min_x) * cell_width,
            BOARD_MARGIN_Y + (item.y - min_y) * cell_height,
            item.region,
        )
        for item in slots
    )
    return TopologyLayout(
        positioned,
        BOARD_MARGIN_X * 2 + (max_x - min_x) * cell_width + NODE_WIDTH,
        BOARD_MARGIN_Y * 2 + (max_y - min_y) * cell_height + NODE_HEIGHT,
    )


def place_within_bounds(
    slots: tuple[PositionedNode, ...],
    *,
    center_row: int,
    center_column: int,
    row_count: int,
    column_count: int,
    margin: int = 1,
) -> dict[str, tuple[int, int]]:
    """Map assign_grid_slots()'s unbounded logical (x, y) steps from LOCAL

    onto a fixed row_count x column_count grid (1-indexed row, column),
    guaranteeing every node lands in-bounds and collision-free.

    Each of the four half-axes (up/down/left/right from center) is
    independently stretched by the largest factor that still fits its
    farthest-out node within `margin` cells of that edge -- e.g. a lone
    node one logical step north of LOCAL, on a board with three rows of
    headroom above center, renders three rows out rather than one,
    genuinely using the board's free space rather than clustering
    everything near center regardless of how much room is available.
    The factor is 1.0 (no change) whenever the axis is already using at
    least as much room as is available (many same-direction nodes, or a
    ring-search overflow from heavy collision pressure) -- this only
    ever stretches outward, never compresses inward. Every node on one
    half-axis is scaled by the identical factor, so relative order
    (farther logical rank stays farther) and any node's own compass
    direction are both preserved exactly; only the actual grid distance
    changes. A hop-bearing client's own already-boosted logical rank
    (see assign_grid_slots' min_radius_by_id) starts from a REAL node's
    position that's already been pushed out for chain room; this spread
    only pushes it (and its interior relay-stage cells) out further
    when the board has headroom to give.

    assign_grid_slots()'s own collision avoidance (_reserve_slot) assumes
    an auto-growing board: if several nodes cluster in the same compass
    direction, its outward spiral search can place a node farther from
    LOCAL than max_radius. On a FIXED board that can push a node past the
    edge. Nodes closest to LOCAL claim their intended cell first (any
    necessary nudging falls on whichever node was already placed
    farthest out); a node whose clamped cell is taken searches an
    expanding ring within bounds, falling back to the first free cell in
    the grid in the extreme case (guaranteed to exist for any working set
    much smaller than the grid's total cell count).
    """
    up_scale = _spread_scale(
        max((-item.y for item in slots if item.y < 0), default=0),
        max(0, center_row - 1 - margin),
    )
    down_scale = _spread_scale(
        max((item.y for item in slots if item.y > 0), default=0),
        max(0, row_count - center_row - margin),
    )
    left_scale = _spread_scale(
        max((-item.x for item in slots if item.x < 0), default=0),
        max(0, center_column - 1 - margin),
    )
    right_scale = _spread_scale(
        max((item.x for item in slots if item.x > 0), default=0),
        max(0, column_count - center_column - margin),
    )

    occupied: set[tuple[int, int]] = set()
    result: dict[str, tuple[int, int]] = {}
    ordered = sorted(
        slots,
        key=lambda item: (
            item.x * item.x + item.y * item.y,
            item.node.node_id.casefold(),
        ),
    )
    for item in ordered:
        if item.y < 0:
            row = center_row - round(-item.y * up_scale)
        elif item.y > 0:
            row = center_row + round(item.y * down_scale)
        else:
            row = center_row
        if item.x < 0:
            column = center_column - round(-item.x * left_scale)
        elif item.x > 0:
            column = center_column + round(item.x * right_scale)
        else:
            column = center_column
        row = _clamp(row, 1, row_count)
        column = _clamp(column, 1, column_count)
        row, column = _reserve_bounded_cell(
            row, column, occupied, row_count=row_count, column_count=column_count
        )
        occupied.add((row, column))
        result[item.node.node_id] = (row, column)
    return result


def project_to_viewport(
    positions: Mapping[str, tuple[int, int]],
    *,
    row_count: int,
    column_count: int,
) -> tuple[dict[str, tuple[int, int]], frozenset[str]]:
    """Clip a set of already-translated (row, column) positions into a

    visible row_count x column_count viewport, returning (a) every
    node's final on-screen cell and (b) the subset that had to be
    clipped to get there -- the real off-screen "edge indicator" set
    (see app.py's MeshTopologyView.set_nodes, the only caller).

    A node already inside [1, row_count] x [1, column_count] is left
    exactly where it is -- this never moves an already-visible node,
    only ever pulls a genuinely off-screen one onto the nearest in-
    bounds cell. Clamping each axis independently toward the boundary
    is exactly the direction-preserving projection this needs: a point
    beyond only one axis lands on the matching cardinal edge; a point
    beyond both axes lands on the corresponding corner, preserving its
    NE/NW/SE/SW relationship rather than collapsing every off-screen
    node onto one of four cardinal cells regardless of its real
    direction.

    In-bounds nodes are placed first and reserve their cells, so an
    off-screen node's clamped landing point can never displace an
    already-correctly-positioned visible one. Multiple off-screen nodes
    that clamp to the identical cell are then deterministically spread
    along the boundary ring outward from that cell (see
    _reserve_edge_cell), processed in node_id order so the outcome is
    stable across repeated refreshes with the same input -- never a
    different arrangement from run to run, and never a random or
    arrival-order-dependent choice.
    """
    row_count = max(1, row_count)
    column_count = max(1, column_count)
    in_bounds: list[tuple[str, int, int]] = []
    out_of_bounds: list[tuple[str, int, int]] = []
    for node_id, (row, column) in sorted(
        positions.items(), key=lambda item: item[0].casefold()
    ):
        if 1 <= row <= row_count and 1 <= column <= column_count:
            in_bounds.append((node_id, row, column))
        else:
            out_of_bounds.append((node_id, row, column))

    occupied: set[tuple[int, int]] = set()
    viewport: dict[str, tuple[int, int]] = {}
    for node_id, row, column in in_bounds:
        viewport[node_id] = (row, column)
        occupied.add((row, column))

    edge_ids: set[str] = set()
    for node_id, row, column in out_of_bounds:
        clamped_row = _clamp(row, 1, row_count)
        clamped_column = _clamp(column, 1, column_count)
        clamped_row, clamped_column = _reserve_edge_cell(
            clamped_row,
            clamped_column,
            occupied,
            row_count=row_count,
            column_count=column_count,
        )
        occupied.add((clamped_row, clamped_column))
        viewport[node_id] = (clamped_row, clamped_column)
        edge_ids.add(node_id)
    return viewport, frozenset(edge_ids)


def _reserve_edge_cell(
    row: int,
    column: int,
    occupied: set[tuple[int, int]],
    *,
    row_count: int,
    column_count: int,
) -> tuple[int, int]:
    """Claim (row, column) for an off-screen node, or the nearest free

    cell that is ALSO still on the grid's outer boundary ring -- so a
    nudged-aside edge indicator never drifts into the interior and
    starts looking like an ordinary in-viewport node. Falls back to
    _reserve_bounded_cell's any-free-cell search (which may leave the
    boundary) only in the degenerate case where the entire boundary
    ring within reach is already occupied -- exactly as rare, and
    handled exactly as deterministically, as that function's own
    full-grid-scan fallback.
    """
    if (row, column) not in occupied:
        return row, column
    radius = 1
    max_span = row_count + column_count
    while radius <= max_span:
        for delta_row, delta_column in _square_ring(radius):
            candidate_row = row + delta_row
            candidate_column = column + delta_column
            if not (
                1 <= candidate_row <= row_count
                and 1 <= candidate_column <= column_count
            ):
                continue
            candidate = (candidate_row, candidate_column)
            if candidate in occupied:
                continue
            on_boundary = (
                candidate_row in (1, row_count)
                or candidate_column in (1, column_count)
            )
            if on_boundary:
                return candidate
        radius += 1
    return _reserve_bounded_cell(
        row, column, occupied, row_count=row_count, column_count=column_count
    )


def _spread_scale(farthest_logical_step: int, available_room: int) -> float:
    """Stretch factor for one half-axis: only ever >= 1.0 (never shrinks).

    farthest_logical_step is the largest raw step assign_grid_slots
    assigned on this half-axis (0 if nothing is placed there at all --
    the factor is irrelevant in that case). available_room is how many
    grid cells that half-axis has between center and its margin-
    respecting edge. Stretching to fill exactly that room is intentional:
    place_within_bounds' own clamp+collision-reservation still guarantees
    an in-bounds, collision-free result even if downstream nudging pushes
    a cell slightly past this target.
    """
    if farthest_logical_step <= 0 or available_room <= farthest_logical_step:
        return 1.0
    return available_room / farthest_logical_step


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _reserve_bounded_cell(
    row: int,
    column: int,
    occupied: set[tuple[int, int]],
    *,
    row_count: int,
    column_count: int,
) -> tuple[int, int]:
    if (row, column) not in occupied:
        return row, column
    radius = 1
    max_span = row_count + column_count
    while radius <= max_span:
        for delta_row, delta_column in _square_ring(radius):
            candidate = (row + delta_row, column + delta_column)
            if (
                1 <= candidate[0] <= row_count
                and 1 <= candidate[1] <= column_count
                and candidate not in occupied
            ):
                return candidate
        radius += 1
    for candidate_row in range(1, row_count + 1):  # pragma: no cover - grid full
        for candidate_column in range(1, column_count + 1):
            if (candidate_row, candidate_column) not in occupied:
                return (candidate_row, candidate_column)
    return (row, column)  # pragma: no cover - unreachable, grid full


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
    """Choose the nearest directionally sensible node without wrapping.

    Ranked primarily by actual Euclidean distance (in the candidate's own
    direction, i.e. `primary > 0`), not by direction-purity alone: a much
    closer node that is somewhat off-axis is preferred over a far node
    that happens to have zero perpendicular offset. Direction-purity
    (`cross / primary`) only breaks a tie between equidistant candidates.
    """
    current = next(
        (item for item in layout.nodes if item.node.node_id == current_node_id),
        None,
    )
    if current is None:
        return None
    candidates: list[tuple[tuple[float, float, str], PositionedNode]] = []
    for candidate in layout.nodes:
        if candidate is current:
            continue
        dx = candidate.x - current.x
        dy = candidate.y - current.y
        primary, cross = _directional_distances(dx, dy, direction)
        if primary <= 0:
            continue
        score = (
            primary * primary + cross * cross,
            cross / primary,
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


# Grapheme-cluster-safe truncation (_ZERO_WIDTH_JOINER, _VARIATION_SELECTORS,
# _EMOJI_MODIFIER_RANGE, _REGIONAL_INDICATOR_RANGE, _attaches_to_previous,
# _grapheme_clusters, _truncate) lives in grapheme_text.py, imported above
# and re-exported under these names for backward compatibility with
# existing callers/tests in this module.
