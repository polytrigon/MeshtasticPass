"""Pure and headless tests for MESH: pure grid geometry, the real passive

working-set/role/staleness model (mesh_state.py), and the fixed-grid
board that renders it. See app.py's module comment above MESH_GRID_ROWS
and mesh_state.py's module docstring for what remains deliberately out
of scope (Favorites, dynamic node-count growth beyond the bounded
working set, scrolling, and RELAY inference from anything but
trustworthy per-packet relay evidence, which this app does not have).
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from rich.cells import cell_len
from textual.color import Color
from textual.containers import ScrollableContainer

from app import (
    ANIMATED_STATUS,
    CIRCLE_SOLID_LARGE,
    CIRCLE_STROKED_LARGE,
    DOT_GRID_GLYPH,
    DOT_GRID_SPACING_X,
    DOT_GRID_SPACING_Y,
    MESH_GRID_CENTER_COLUMN,
    MESH_GRID_CENTER_ROW,
    MESH_BOARD_LABEL_MAX_CELLS,
    MESH_GRID_COLUMNS,
    MESH_GRID_ROWS,
    MESH_SELECTED_GLYPH_WIDTH,
    MeshCanvas,
    MeshNodeLabelWidget,
    MeshNodeWidget,
    MeshRelayWidget,
    MeshTopologyView,
    MeshtasticPassApp,
    ThinScrollBarRender,
    _mesh_directional_target,
    _mesh_grid_pixel,
    _mesh_hop_counts,
    _mesh_node_color,
    _mesh_relay_color,
    _mesh_select_node,
    _mesh_translated_positions,
    _render_mesh_canvas,
)
from app_settings import AppSettings
from chat_store import DEFAULT_HISTORY_LIMIT, ChatStore
from geo import GeoPosition, distance_between
from mesh_state import (
    DEFAULT_MAX_REMOTE_NODES,
    MESH_STALE_THRESHOLD_SECONDS,
    MeshNodeState,
    build_mesh_working_set,
    format_mesh_context_line,
)
from mesh_topology import (
    DEFAULT_MAX_GRID_RADIUS,
    HORIZONTAL_GAP,
    NODE_HEIGHT,
    NODE_WIDTH,
    RelayStage,
    assign_grid_slots,
    build_relay_stages,
    build_topology,
    compact_node_label,
    directional_target,
    place_within_bounds,
    route_chain,
    route_connector,
)
import mesh_topology as mesh_topology_module
from node_activity import ACTIVE_WINDOW_SECONDS, is_node_active
from radio_service import NodeMetadata, RadioState
from simulated_radio_service import (
    SIMULATED_LOCAL_POSITION,
    SIMULATED_MESSAGES,
    SIMULATED_NODES,
    SimulatedRadioService,
)
from theme_palette import THEME_PALETTES


LOCAL_GEO = GeoPosition(0.0, 0.0)
_MILES_PER_DEGREE_AT_EQUATOR = 69.0

YOU = NodeMetadata("!you", "Local", "ME", 0, 1_000.0, True, position=LOCAL_GEO)


def west_of_local(miles: float) -> GeoPosition:
    return GeoPosition(0.0, -miles / _MILES_PER_DEGREE_AT_EQUATOR)


def east_of_local(miles: float) -> GeoPosition:
    return GeoPosition(0.0, miles / _MILES_PER_DEGREE_AT_EQUATOR)


def north_of_local(miles: float) -> GeoPosition:
    return GeoPosition(miles / _MILES_PER_DEGREE_AT_EQUATOR, 0.0)


def south_of_local(miles: float) -> GeoPosition:
    return GeoPosition(-miles / _MILES_PER_DEGREE_AT_EQUATOR, 0.0)


def sample_nodes(count: int = 8) -> tuple[NodeMetadata, ...]:
    nodes = [NodeMetadata("!00000000", "Local", "ME", 0, 1_000.0, True)]
    for index in range(1, count):
        hops = 1 if index < 4 else 2 if index < 7 else None
        nodes.append(
            NodeMetadata(
                f"!{index:08x}",
                f"Node {index}",
                f"N{index}",
                hops,
                990.0 if index % 2 else 600.0,
            )
        )
    return tuple(nodes)


def offset_xy(widget) -> tuple[int, int]:
    offset = widget.styles.offset
    return (int(offset.x.value), int(offset.y.value))


def glyph_screen_position(widget) -> tuple[int, int]:
    """The glyph's own anchor (x, y): the center column of its box.

    Unselected, MeshNodeWidget is a 1-cell-wide box, so its offset IS the
    anchor. Selected, it widens to a 3-cell composite (small dot / role
    glyph / small dot) for the "larger" treatment -- the anchor is still
    the box's center column, never its left edge, so this must account
    for width rather than assume width == 1.
    """
    offset_x, offset_y = offset_xy(widget)
    width = int(widget.styles.width.value)
    return (offset_x + width // 2, offset_y)


def client_state(
    node: NodeMetadata, *, last_interaction_at: float | None, is_relay: bool = False
) -> MeshNodeState:
    return MeshNodeState(
        node=node, is_client=True, is_relay=is_relay, last_interaction_at=last_interaction_at
    )


class MeshTopologyModelTests(unittest.TestCase):
    def test_west_and_east_nodes_ordered_by_distance_not_proportionally(self) -> None:
        """The exact worked example: Jim < Alice < YOU < Bob on the x-axis."""
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        alice = NodeMetadata(
            "!alice", "Alice", "ALCE", None, 1_000.0, False, position=west_of_local(1)
        )
        jim = NodeMetadata(
            "!jim", "Jim", "JIM", None, 1_000.0, False, position=west_of_local(2)
        )
        bob = NodeMetadata(
            "!bob", "Bob", "BOB", None, 1_000.0, False, position=east_of_local(4)
        )
        layout = build_topology([local, alice, jim, bob])
        by_id = {item.node.node_id: item for item in layout.nodes}

        self.assertEqual(by_id["!alice"].region, "W")
        self.assertEqual(by_id["!jim"].region, "W")
        self.assertEqual(by_id["!bob"].region, "E")
        self.assertLess(by_id["!jim"].x, by_id["!alice"].x)
        self.assertLess(by_id["!alice"].x, by_id["!local"].x)
        self.assertLess(by_id["!local"].x, by_id["!bob"].x)

        # Bob is 4x farther than Alice but must not sit 4x farther visually:
        # one grid step per rank, regardless of the real-world mile gap.
        alice_gap = by_id["!local"].x - by_id["!alice"].x
        jim_gap = by_id["!alice"].x - by_id["!jim"].x
        self.assertEqual(alice_gap, jim_gap)

    def test_north_and_south_nodes_placed_up_and_down(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        nora = NodeMetadata(
            "!nora", "Nora", "NORA", None, 1_000.0, False, position=north_of_local(3)
        )
        sam = NodeMetadata(
            "!sam", "Sam", "SAM", None, 1_000.0, False, position=south_of_local(2)
        )
        layout = build_topology([local, nora, sam])
        by_id = {item.node.node_id: item for item in layout.nodes}

        self.assertEqual(by_id["!nora"].region, "N")
        self.assertEqual(by_id["!sam"].region, "S")
        self.assertLess(by_id["!nora"].y, by_id["!local"].y)
        self.assertLess(by_id["!local"].y, by_id["!sam"].y)

    def test_distance_orders_outward_without_proportional_spacing(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        near = NodeMetadata(
            "!near", "Near", "N", None, 1_000.0, False, position=west_of_local(1)
        )
        far = NodeMetadata(
            "!far", "Far", "F", None, 1_000.0, False, position=west_of_local(500)
        )
        layout = build_topology([local, near, far])
        by_id = {item.node.node_id: item for item in layout.nodes}
        self.assertLess(by_id["!far"].x, by_id["!near"].x)
        near_gap = by_id["!local"].x - by_id["!near"].x
        far_gap = by_id["!near"].x - by_id["!far"].x
        self.assertEqual(near_gap, far_gap)

    def test_grid_radius_is_clamped_and_never_explodes_the_board(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        nodes = [local]
        for index in range(6):
            nodes.append(
                NodeMetadata(
                    f"!w{index}",
                    f"West{index}",
                    None,
                    None,
                    1_000.0,
                    False,
                    position=west_of_local(index + 1),
                )
            )
        layout = build_topology(nodes, max_radius=3)
        cell_width = NODE_WIDTH + HORIZONTAL_GAP
        max_extent_cells = max(abs(item.x - layout.nodes[0].x) for item in layout.nodes)
        # A single very-distant node (rank 6) must not push any node beyond a
        # small multiple of the clamped radius, regardless of how far it is.
        self.assertLessEqual(max_extent_cells, cell_width * (3 + 2))

    def test_unknown_position_nodes_use_deterministic_fallback_region(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        known = NodeMetadata(
            "!known", "Known", "K", None, 1_000.0, False, position=west_of_local(1)
        )
        close_hop_no_position = NodeMetadata(
            "!close", "CloseHop", "CH", 1, 1_000.0, False, position=None
        )
        far_hop_no_position = NodeMetadata(
            "!far", "FarHop", "FH", 9, 1_000.0, False, position=None
        )
        layout = build_topology(
            [local, known, close_hop_no_position, far_hop_no_position]
        )
        by_id = {item.node.node_id: item for item in layout.nodes}
        self.assertEqual(by_id["!known"].region, "W")
        # hopsAway must never fabricate a direction/ordering, regardless of
        # how close or far the hop count claims to be.
        self.assertEqual(by_id["!close"].region, "UNKNOWN")
        self.assertEqual(by_id["!far"].region, "UNKNOWN")

    def test_missing_local_position_makes_every_remote_unknown(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=None)
        remote = NodeMetadata(
            "!remote", "Remote", "R", None, 1_000.0, False, position=west_of_local(1)
        )
        layout = build_topology([local, remote])
        by_id = {item.node.node_id: item for item in layout.nodes}
        self.assertEqual(by_id["!remote"].region, "UNKNOWN")

    def test_directional_placement_is_arrival_order_independent(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        nodes = [local]
        for index in range(6):
            nodes.append(
                NodeMetadata(
                    f"!w{index}",
                    f"West {index}",
                    f"W{index}",
                    None,
                    1_000.0,
                    False,
                    position=west_of_local(index + 1),
                )
            )
        forward = build_topology(nodes)
        backward = build_topology(list(reversed(nodes)))
        forward_positions = {
            item.node.node_id: (item.x, item.y) for item in forward.nodes
        }
        backward_positions = {
            item.node.node_id: (item.x, item.y) for item in backward.nodes
        }
        self.assertEqual(forward_positions, backward_positions)

    def test_no_overlap_across_mixed_directions_and_unknown_region(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        nodes = [local]
        for index in range(8):
            nodes.append(
                NodeMetadata(
                    f"!w{index}",
                    f"West{index}",
                    None,
                    None,
                    1_000.0,
                    False,
                    position=west_of_local(index + 1),
                )
            )
            nodes.append(
                NodeMetadata(
                    f"!e{index}",
                    f"East{index}",
                    None,
                    None,
                    1_000.0,
                    False,
                    position=east_of_local(index + 1),
                )
            )
            nodes.append(
                NodeMetadata(
                    f"!u{index}", f"Unknown{index}", None, None, 1_000.0, False
                )
            )
        layout = build_topology(nodes)
        rectangles = []
        for item in layout.nodes:
            rectangle = (item.x, item.y, item.x + NODE_WIDTH, item.y + NODE_HEIGHT)
            for other in rectangles:
                separated = (
                    rectangle[2] <= other[0]
                    or other[2] <= rectangle[0]
                    or rectangle[3] <= other[1]
                    or other[3] <= rectangle[1]
                )
                self.assertTrue(separated)
            rectangles.append(rectangle)

    def test_reserve_slot_falls_back_to_nearest_free_slot_on_collision(self) -> None:
        occupied = {(2, 0)}
        result = mesh_topology_module._reserve_slot(2, 0, occupied)
        self.assertNotEqual(result, (2, 0))
        self.assertIn(result, occupied)

    def test_layout_is_deterministic_collision_free_and_scales(self) -> None:
        nodes = sample_nodes(31)
        first = build_topology(nodes)
        second = build_topology(reversed(nodes))
        first_positions = {
            item.node.node_id: (item.x, item.y) for item in first.nodes
        }
        second_positions = {
            item.node.node_id: (item.x, item.y) for item in second.nodes
        }
        self.assertEqual(first_positions, second_positions)
        rectangles = []
        for item in first.nodes:
            rectangle = (item.x, item.y, item.x + NODE_WIDTH, item.y + NODE_HEIGHT)
            for other in rectangles:
                separated = (
                    rectangle[2] <= other[0]
                    or other[2] <= rectangle[0]
                    or rectangle[3] <= other[1]
                    or other[3] <= rectangle[1]
                )
                self.assertTrue(separated)
            rectangles.append(rectangle)

    def test_labels_prefer_names_then_abbreviated_node_id(self) -> None:
        self.assertEqual(compact_node_label(sample_nodes()[0]), "YOU")
        short = NodeMetadata("!12345678", "A name far too long for the node", "ABCD")
        self.assertEqual(compact_node_label(short), "ABCD")
        fallback = NodeMetadata("!1234567890abcdef")
        self.assertTrue(compact_node_label(fallback).startswith("…"))

    def test_spatial_navigation_has_no_edge_wrap(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        north_node = NodeMetadata(
            "!north", "North", "N", None, 1_000.0, False, position=north_of_local(1)
        )
        layout = build_topology([local, north_node])
        target = directional_target("!local", layout, "up")
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.node.node_id, "!north")
        top = min(layout.nodes, key=lambda item: item.y)
        self.assertIsNone(directional_target(top.node.node_id, layout, "up"))

    def test_emoji_and_wide_labels_never_exceed_node_cell_width(self) -> None:
        max_cells = NODE_WIDTH - 2
        long_suffix = "_extra_long_descriptive_name_here"
        samples = (
            "🐔node" + long_suffix,
            "🍕pizza" + long_suffix,
            "👨‍👩‍👧‍👦 family" + long_suffix,
            "🇺🇸🇬🇧 flags" + long_suffix,
            "a🏴‍☠️b pirate" + long_suffix,
            "CJK漢字測試名稱超長節點標籤",
            "plain_ascii" + long_suffix,
        )
        for long_name in samples:
            node = NodeMetadata("!12345678", long_name, None)
            label = compact_node_label(node)
            with self.subTest(long_name=long_name):
                self.assertLessEqual(cell_len(label), max_cells)
                if label.endswith("…"):
                    # Truncation must not sever an emoji joiner/modifier,
                    # which would otherwise render as an unexpectedly wide orphan.
                    last_kept = label[:-1][-1:]
                    self.assertNotEqual(
                        last_kept, mesh_topology_module._ZERO_WIDTH_JOINER
                    )
                    self.assertNotIn(
                        last_kept, mesh_topology_module._VARIATION_SELECTORS
                    )

    def test_short_emoji_and_wide_labels_pass_through_untruncated(self) -> None:
        for short_name in ("🐔", "🍕US", "漢字"):
            node = NodeMetadata("!12345678", None, short_name)
            self.assertEqual(compact_node_label(node), short_name)

    def test_emoji_labels_do_not_change_deterministic_layout_positions(self) -> None:
        emoji_nodes = [NodeMetadata("!00000000", "Local", "ME", 0, 1_000.0, True)]
        for index in range(1, 8):
            hops = 1 if index < 4 else 2 if index < 7 else None
            emoji_nodes.append(
                NodeMetadata(
                    f"!{index:08x}",
                    f"🐔 Node {index}",
                    f"N{index}",
                    hops,
                    990.0 if index % 2 else 600.0,
                )
            )
        emoji_nodes = tuple(emoji_nodes)
        first = build_topology(emoji_nodes)
        second = build_topology(reversed(emoji_nodes))
        first_positions = {item.node.node_id: (item.x, item.y) for item in first.nodes}
        second_positions = {item.node.node_id: (item.x, item.y) for item in second.nodes}
        self.assertEqual(first_positions, second_positions)

    def test_virtual_board_dimensions_unaffected_by_emoji_label_length(self) -> None:
        plain = sample_nodes(8)
        emoji_nodes = tuple(
            NodeMetadata(
                node.node_id,
                (f"🐔🍕👨‍👩‍👧‍👦 {node.long_name}" if node.long_name else None),
                node.short_name,
                node.hops_away,
                node.last_heard,
                node.is_local,
            )
            for node in plain
        )
        plain_layout = build_topology(plain)
        emoji_layout = build_topology(emoji_nodes)
        self.assertEqual(plain_layout.width, emoji_layout.width)
        self.assertEqual(plain_layout.height, emoji_layout.height)

    def test_emoji_labels_do_not_disturb_directional_grid_slots(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        plain_west = NodeMetadata(
            "!west", "West Node", "W", None, 1_000.0, False, position=west_of_local(1)
        )
        emoji_west = NodeMetadata(
            "!west", "🐔🍕👨‍👩‍👧‍👦 West Node", "W", None, 1_000.0, False,
            position=west_of_local(1),
        )
        plain_layout = build_topology([local, plain_west])
        emoji_layout = build_topology([local, emoji_west])
        plain_positions = {item.node.node_id: (item.x, item.y) for item in plain_layout.nodes}
        emoji_positions = {item.node.node_id: (item.x, item.y) for item in emoji_layout.nodes}
        self.assertEqual(plain_positions, emoji_positions)


class RouteConnectorTests(unittest.TestCase):
    def test_straight_lines_use_a_single_glyph_no_elbow(self) -> None:
        horizontal = route_connector(0, 0, 5, 0)
        self.assertTrue(all(glyph == "─" for _x, _y, glyph in horizontal))
        vertical = route_connector(0, 0, 0, 5)
        self.assertTrue(all(glyph == "│" for _x, _y, glyph in vertical))

    def test_same_point_has_no_cells(self) -> None:
        self.assertEqual(route_connector(3, 3, 3, 3), ())

    def test_endpoints_are_excluded(self) -> None:
        cells = route_connector(0, 0, 4, 0)
        points = {(x, y) for x, y, _glyph in cells}
        self.assertNotIn((0, 0), points)
        self.assertNotIn((4, 0), points)

    def test_all_four_elbow_quadrants_use_distinct_correct_corners(self) -> None:
        # East+South: arrives from the left, turns down.
        se = route_connector(0, 0, 4, 3)
        self.assertEqual(se[-3], (4, 0, "┐"))
        # East+North: arrives from the left, turns up.
        ne = route_connector(0, 3, 4, 0)
        self.assertEqual(ne[-3], (4, 3, "┘"))
        # West+South: arrives from the right, turns down.
        sw = route_connector(4, 0, 0, 3)
        self.assertEqual(sw[-3], (0, 0, "┌"))
        # West+North: arrives from the right, turns up.
        nw = route_connector(4, 3, 0, 0)
        self.assertEqual(nw[-3], (0, 3, "└"))

    def test_route_is_deterministic(self) -> None:
        self.assertEqual(route_connector(1, 1, 9, 6), route_connector(1, 1, 9, 6))


class MeshCanvasRenderTests(unittest.TestCase):
    def test_dot_grid_uses_regular_spacing_and_glyph(self) -> None:
        width, height = 20, 9
        text = _render_mesh_canvas(width, height, (), "#222222")
        rows = str(text).split("\n")
        self.assertEqual(len(rows), height)
        for y, row in enumerate(rows):
            self.assertEqual(len(row), width)
            for x, character in enumerate(row):
                expected = (
                    DOT_GRID_GLYPH
                    if x % DOT_GRID_SPACING_X == 0 and y % DOT_GRID_SPACING_Y == 0
                    else " "
                )
                self.assertEqual(character, expected)

    def test_canvas_is_empty_for_a_degenerate_board(self) -> None:
        self.assertEqual(str(_render_mesh_canvas(1, 1, (), "#222222")), "")

    def test_connectors_override_the_dot_grid_at_their_cells(self) -> None:
        connectors = ((3, 3, "─", "#ff0000"), (9, 3, "┐", "#ff0000"))
        text = _render_mesh_canvas(20, 9, connectors, "#222222")
        rows = str(text).split("\n")
        self.assertEqual(rows[3][3], "─")
        self.assertEqual(rows[3][9], "┐")

    def test_canvas_is_non_focusable(self) -> None:
        self.assertFalse(MeshCanvas.can_focus)


class MeshGridPlacementTests(unittest.TestCase):
    """Pure tests for assign_grid_slots()/place_within_bounds(): the shared

    geometry MESH now uses to place a real, bounded working set on its
    fixed grid.
    """

    def test_assign_grid_slots_places_local_at_origin(self) -> None:
        slots = assign_grid_slots([YOU])
        self.assertEqual(len(slots), 1)
        self.assertEqual((slots[0].x, slots[0].y), (0, 0))
        self.assertEqual(slots[0].region, "LOCAL")

    def test_place_within_bounds_centers_local_at_the_given_center(self) -> None:
        slots = assign_grid_slots([YOU])
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        self.assertEqual(positions["!you"], (5, 11))

    def test_place_within_bounds_keeps_a_far_node_inside_the_grid(self) -> None:
        far_east = NodeMetadata("!far", "Far", position=east_of_local(500))
        slots = assign_grid_slots([YOU, far_east], max_radius=DEFAULT_MAX_GRID_RADIUS)
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        row, column = positions["!far"]
        self.assertTrue(1 <= row <= 8)
        self.assertTrue(1 <= column <= 21)

    def test_place_within_bounds_never_overlaps_under_collision_pressure(self) -> None:
        """Cram many nodes into the same compass direction so the shared,

        unbounded collision-avoidance (_reserve_slot) would want to push
        several of them past this fixed board's edge -- place_within_bounds
        must still land every one in-bounds and collision-free.
        """
        nodes = [YOU] + [
            NodeMetadata(f"!s{index:02x}", f"South{index}", position=south_of_local(index + 1))
            for index in range(10)
        ]
        slots = assign_grid_slots(nodes, max_radius=3)
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        self.assertEqual(len(positions), len(nodes))
        self.assertEqual(len(positions), len(set(positions.values())))
        for row, column in positions.values():
            self.assertTrue(1 <= row <= 8)
            self.assertTrue(1 <= column <= 21)

    def test_place_within_bounds_is_deterministic_and_order_independent(self) -> None:
        nodes = [
            YOU,
            NodeMetadata("!a", "A", position=north_of_local(2)),
            NodeMetadata("!b", "B", position=north_of_local(5)),
            NodeMetadata("!c", "C"),
        ]
        forward = place_within_bounds(
            assign_grid_slots(nodes),
            center_row=5,
            center_column=11,
            row_count=8,
            column_count=21,
        )
        backward = place_within_bounds(
            assign_grid_slots(list(reversed(nodes))),
            center_row=5,
            center_column=11,
            row_count=8,
            column_count=21,
        )
        self.assertEqual(forward, backward)

    # ---- Grid-spread: use more of the available fixed-grid room --------

    def test_lone_direction_node_stretches_to_use_available_room(self) -> None:
        """A lone north node has 1 raw logical step but 3 rows of

        headroom above center on this 8-row grid (row_count=8,
        center_row=5, default margin=1: 5-1-1=3) -- it must stretch to
        use that room instead of clustering one row from center.
        """
        node = NodeMetadata("!n", "N", position=north_of_local(3))
        slots = assign_grid_slots([YOU, node])
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        row, _column = positions["!n"]
        self.assertEqual(row, 2)  # center_row(5) - full up_room(3)

    def test_spread_does_not_shrink_when_room_is_already_fully_used(self) -> None:
        """Three same-direction nodes already spanning the full 3-row

        headroom must land at exactly their original ranks (4, 3, 2) --
        spreading only ever stretches outward when there is slack, never
        compresses when the available room is already fully used.
        """
        nodes = [YOU] + [
            NodeMetadata(f"!n{i}", f"N{i}", position=north_of_local(i + 1)) for i in range(3)
        ]
        slots = assign_grid_slots(nodes, max_radius=3)
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        rows = sorted(positions[f"!n{i}"][0] for i in range(3))
        self.assertEqual(rows, [2, 3, 4])

    def test_spread_respects_asymmetric_room_between_axes(self) -> None:
        """The 21-column grid has far more horizontal headroom (9) than

        this 8-row grid has vertical headroom (3 up / 2 down) around
        center (11, 5) -- a lone east node and a lone north node, each
        one raw logical step out, must stretch by different amounts
        that reflect their own axis's actual available room.
        """
        east_node = NodeMetadata("!e", "E", position=east_of_local(3))
        north_node = NodeMetadata("!n", "N", position=north_of_local(3))
        slots = assign_grid_slots([YOU, east_node, north_node])
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        east_distance = positions["!e"][1] - 11
        north_distance = 5 - positions["!n"][0]
        self.assertGreater(east_distance, north_distance)

    def test_spread_preserves_relative_ordering_within_a_direction(self) -> None:
        near = NodeMetadata("!near", "Near", position=north_of_local(1))
        far = NodeMetadata("!far", "Far", position=north_of_local(50))
        slots = assign_grid_slots([YOU, near, far], max_radius=3)
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        # Farther logical rank must still render strictly farther from
        # center after spreading, never reordered by the stretch.
        self.assertLess(positions["!far"][0], positions["!near"][0])

    def test_spread_stays_collision_free_and_deterministic_for_a_mixed_fixture(
        self,
    ) -> None:
        """A representative multi-direction, multi-node fixture: spread

        must never collide and must never depend on input order, exactly
        like the unspread algorithm already guaranteed.
        """
        nodes = [YOU] + [
            NodeMetadata("!n1", "N1", position=north_of_local(2)),
            NodeMetadata("!s1", "S1", position=south_of_local(3)),
            NodeMetadata("!e1", "E1", position=east_of_local(4)),
            NodeMetadata("!e2", "E2", position=east_of_local(20)),
            NodeMetadata("!w1", "W1", position=west_of_local(5)),
        ]
        forward = place_within_bounds(
            assign_grid_slots(nodes), center_row=5, center_column=11, row_count=8, column_count=21
        )
        backward = place_within_bounds(
            assign_grid_slots(list(reversed(nodes))),
            center_row=5,
            center_column=11,
            row_count=8,
            column_count=21,
        )
        self.assertEqual(forward, backward)
        self.assertEqual(len(forward), len(set(forward.values())))
        for row, column in forward.values():
            self.assertTrue(1 <= row <= 8)
            self.assertTrue(1 <= column <= 21)

    def test_spread_never_places_a_node_at_the_true_outer_edge_by_default(
        self,
    ) -> None:
        """The default margin=1 keeps at least one row/column of breathing

        room at the true edge even when a lone node stretches to fill
        all available headroom (spec: "Do not push everything against
        the outer edge either; preserve reasonable margins").
        """
        node = NodeMetadata("!n", "N", position=north_of_local(3))
        slots = assign_grid_slots([YOU, node])
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        self.assertGreater(positions["!n"][0], 1)


class MeshTranslationAndDirectionalTargetTests(unittest.TestCase):
    """Pure tests for app.py's generic (not fixture-specific) whole-mesh

    translation and arrow-navigation helpers, now parametrized by
    whatever working set/base positions the live model produces.
    """

    def test_translate_shifts_every_node_by_the_identical_delta(self) -> None:
        base = {"!you": (5, 11), "!a": (4, 10), "!b": (6, 9)}
        translated = _mesh_translated_positions(base, "!a")
        self.assertEqual(translated["!a"], (5, 11))
        row_delta, column_delta = 5 - 4, 11 - 10
        self.assertEqual(translated["!you"], (5 + row_delta, 11 + column_delta))
        self.assertEqual(translated["!b"], (6 + row_delta, 9 + column_delta))

    def test_translate_with_unresolvable_selection_is_a_no_op(self) -> None:
        base = {"!you": (5, 11)}
        self.assertEqual(_mesh_translated_positions(base, "!missing"), base)

    def test_directional_target_picks_the_nearest_candidate(self) -> None:
        """No hardcoded node IDs: the same general nearest-candidate rule

        used by mesh_topology.directional_target, just fed the current
        rendered layout's own positions -- real nodes and anonymous
        relay stages alike, since both are just entries in this dict.
        """
        base_positions = {"!you": (5, 11), "!near": (4, 10), "!far": (1, 8)}
        self.assertEqual(
            _mesh_directional_target(base_positions, "!you", "left"), "!near"
        )

    def test_directional_target_with_no_candidate_is_none(self) -> None:
        target = _mesh_directional_target({"!you": (5, 11)}, "!you", "left")
        self.assertIsNone(target)


class RelayStageGeometryTests(unittest.TestCase):
    """Pure tests for mesh_topology's anonymous-relay-stage placement:

    exact stage counts per hop depth, independence between clients, and
    collision-free placement -- no app, no widgets.
    """

    def _positions(self, *, target_hops: int) -> tuple[dict, dict]:
        you = NodeMetadata("!you", is_local=True, position=GeoPosition(0.0, 0.0))
        target = NodeMetadata(
            "!target", "Target", "TGT", target_hops, position=GeoPosition(9 / 69.0, 0.0)
        )
        min_radius = {"!target": target_hops + 1} if target_hops > 0 else {}
        slots = assign_grid_slots(
            (you, target), max_radius=DEFAULT_MAX_GRID_RADIUS, min_radius_by_id=min_radius
        )
        base = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        return base, {"!target": target_hops} if target_hops > 0 else {}

    def test_zero_hops_generates_no_relay_stages(self) -> None:
        base, hop_counts = self._positions(target_hops=0)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        self.assertEqual(stages, ())
        self.assertEqual(positions, {})

    def test_one_hop_generates_exactly_one_relay_stage(self) -> None:
        base, hop_counts = self._positions(target_hops=1)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        self.assertEqual(len(stages), 1)
        self.assertEqual(len(positions), 1)

    def test_two_hops_generates_exactly_two_relay_stages(self) -> None:
        base, hop_counts = self._positions(target_hops=2)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        self.assertEqual(len(stages), 2)
        self.assertEqual({s.index for s in stages}, {1, 2})

    def test_three_hops_generates_exactly_three_relay_stages(self) -> None:
        base, hop_counts = self._positions(target_hops=3)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        self.assertEqual(len(stages), 3)
        self.assertEqual({s.index for s in stages}, {1, 2, 3})
        self.assertEqual(len(positions.values()), len(set(positions.values())))

    def test_unknown_hops_client_absent_from_hop_counts_gets_no_stages(self) -> None:
        """A `? HOPS` client is never passed into hop_counts at all (see

        app._mesh_hop_counts) -- proving build_relay_stages never
        fabricates stages for a client it isn't told about.
        """
        base, _ = self._positions(target_hops=0)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts={}, row_count=8, column_count=21
        )
        self.assertEqual(stages, ())
        self.assertEqual(positions, {})

    def test_no_you_position_produces_no_stages(self) -> None:
        stages, positions = build_relay_stages(
            {"!target": (4, 11)},
            you_id="!you",
            hop_counts={"!target": 2},
            row_count=8,
            column_count=21,
        )
        self.assertEqual(stages, ())
        self.assertEqual(positions, {})

    def test_relay_stage_ids_are_never_shared_between_clients_with_equal_hops(
        self,
    ) -> None:
        you = NodeMetadata("!you", is_local=True, position=GeoPosition(0.0, 0.0))
        alice = NodeMetadata(
            "!alice", "Alice", position=GeoPosition(0.0, 9 / 69.0)
        )  # east
        bob = NodeMetadata(
            "!bob", "Bob", position=GeoPosition(0.0, -9 / 69.0)
        )  # west
        slots = assign_grid_slots(
            (you, alice, bob),
            max_radius=DEFAULT_MAX_GRID_RADIUS,
            min_radius_by_id={"!alice": 3, "!bob": 3},
        )
        base = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        stages, positions = build_relay_stages(
            base,
            you_id="!you",
            hop_counts={"!alice": 2, "!bob": 2},
            row_count=8,
            column_count=21,
        )
        self.assertEqual(len(stages), 4)
        alice_ids = {s.node_id for s in stages if s.source_node_id == "!alice"}
        bob_ids = {s.node_id for s in stages if s.source_node_id == "!bob"}
        self.assertEqual(len(alice_ids), 2)
        self.assertEqual(len(bob_ids), 2)
        self.assertEqual(alice_ids & bob_ids, set())
        # No fabricated real-node shape leaks onto a placeholder.
        for stage in stages:
            self.assertFalse(stage.is_local)
            self.assertTrue(stage.node_id.startswith("relay:"))
        self.assertEqual(len(positions.values()), len(set(positions.values())))

    def test_relay_stage_node_id_encodes_source_and_index_internally_only(
        self,
    ) -> None:
        """The synthetic ID format is internal bookkeeping -- proven here

        directly -- and never surfaces in any user-facing string (the
        app-level tests prove the UI always shows exactly "RELAY").
        """
        base, hop_counts = self._positions(target_hops=2)
        stages, _ = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        ids = {s.node_id for s in stages}
        self.assertEqual(ids, {"relay:!target:1", "relay:!target:2"})

    def test_min_radius_by_id_does_not_affect_unlisted_nodes(self) -> None:
        """assign_grid_slots' new optional parameter is fully backward

        compatible: omitting it (as every non-MESH-hop caller does)
        reproduces the exact prior placement.
        """
        you = NodeMetadata("!you", is_local=True, position=GeoPosition(0.0, 0.0))
        node = NodeMetadata("!n", "N", position=GeoPosition(9 / 69.0, 0.0))
        without = assign_grid_slots((you, node), max_radius=DEFAULT_MAX_GRID_RADIUS)
        with_empty = assign_grid_slots(
            (you, node), max_radius=DEFAULT_MAX_GRID_RADIUS, min_radius_by_id={}
        )
        self.assertEqual(without, with_empty)

    def test_min_radius_by_id_boosts_only_the_named_node(self) -> None:
        you = NodeMetadata("!you", is_local=True, position=GeoPosition(0.0, 0.0))
        node = NodeMetadata("!n", "N", position=GeoPosition(9 / 69.0, 0.0))
        boosted = assign_grid_slots(
            (you, node), max_radius=DEFAULT_MAX_GRID_RADIUS, min_radius_by_id={"!n": 3}
        )
        entry = next(item for item in boosted if item.node.node_id == "!n")
        self.assertEqual((entry.x, entry.y), (0, -3))


class RouteChainTests(unittest.TestCase):
    """Pure tests for the multi-segment connector-chain helper."""

    def test_two_point_chain_matches_plain_route_connector(self) -> None:
        self.assertEqual(
            route_chain(((0, 0), (4, 3))), route_connector(0, 0, 4, 3)
        )

    def test_chain_concatenates_each_segment_in_order(self) -> None:
        chain = route_chain(((0, 0), (0, 3), (4, 3)))
        expected = route_connector(0, 0, 0, 3) + route_connector(0, 3, 4, 3)
        self.assertEqual(chain, expected)

    def test_single_point_chain_is_empty(self) -> None:
        self.assertEqual(route_chain(((2, 2),)), ())


class BoardLabelFiveCellLimitTests(unittest.TestCase):
    """Pure tests for the new 5-display-cell BOARD label limit.

    This is a DISPLAY-CELL limit (cell_len()), never Python len() --
    grapheme-safe truncation, emoji/ZWJ/CJK safety, and the underlying
    compact_node_label() truncation convention are all unchanged; only
    the max_cells value app.py passes for the label physically above a
    node's glyph is new. The rich bottom-left context (mesh_state.
    format_mesh_context_line) is untouched and always uses the full,
    uncapped Long Name/Short Name -- proven separately in test_mesh_state.py.
    """

    def test_short_ascii_name_passes_through_unchanged(self) -> None:
        node = NodeMetadata("!12345678", "ALICE", None)
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertEqual(label, "ALICE")
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)

    def test_long_ascii_name_truncates_to_five_cells(self) -> None:
        node = NodeMetadata("!12345678", "CHARLIE THE LONG NAMED NODE", None)
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)
        self.assertTrue(label.endswith("…"))

    def test_emoji_name_stays_within_five_cells(self) -> None:
        node = NodeMetadata("!12345678", "🐔pizza party extra long name", None)
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)

    def test_zwj_emoji_never_severed_at_five_cells(self) -> None:
        node = NodeMetadata(
            "!12345678", "👨‍👩‍👧‍👦 family extra long descriptive name", None
        )
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)
        if label.endswith("…"):
            last_kept = label[:-1][-1:]
            self.assertNotEqual(last_kept, mesh_topology_module._ZERO_WIDTH_JOINER)
            self.assertNotIn(last_kept, mesh_topology_module._VARIATION_SELECTORS)

    def test_cjk_wide_characters_stay_within_five_cells(self) -> None:
        node = NodeMetadata("!12345678", "CJK漢字測試名稱超長節點標籤", None)
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)

    def test_short_name_fallback_also_respects_five_cells(self) -> None:
        node = NodeMetadata("!12345678", "A Very Long Descriptive Long Name", "SHORT")
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertEqual(label, "SHORT")
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)


class MeshRealDataAppTests(unittest.IsolatedAsyncioTestCase):
    """Headless app tests proving MESH is now driven by real passive data:

    CLIENT derivation, RELAY truthfulness, the bounded working set,
    geographic placement, context text, selection/navigation, the
    preserved visual contract, and zero new radio traffic.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(
        self, *, chat_store: ChatStore | None = None, scripted_messages=()
    ) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=scripted_messages
        )
        return MeshtasticPassApp(radio, self.settings, chat_store=chat_store)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    # ---- CLIENT derivation (spec section 21) -------------------------

    async def test_incoming_message_makes_the_sender_a_client(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            alice = next(
                state
                for state in view.working_set
                if state.node.node_id == SIMULATED_MESSAGES[0].sender_node_id
            )
            self.assertTrue(alice.is_client)

    async def test_outgoing_message_does_not_make_a_remote_node_client(self) -> None:
        """An outgoing message must never mark ANY remote node CLIENT.

        Remote nodes may still appear here -- NodeDB-first admission
        (see build_mesh_working_set) means SimulatedRadioService's own
        passively-known nodes are candidates on their own -- but none of
        them may be is_client, since no incoming message was ever
        received from any of them.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accepted_send("hello from me")
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            remote_clients = {
                state.node.node_id
                for state in view.working_set
                if not state.node.is_local and state.is_client
            }
            self.assertEqual(remote_clients, set())

    async def test_persisted_incoming_history_survives_a_fresh_app_instance(self) -> None:
        """Simulates a process restart: a new ChatStore/App reading the same

        database file must still see Alice as CLIENT from "before".
        """
        db_path = self.root / "chat.db"
        now = 1_700_000_000.0
        old_timestamp = now - 10 * 24 * 60 * 60

        first_process_store = ChatStore.open(db_path)
        first_process_store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice Trail",
            sender_short_name="ALCE",
            channel_index=0,
            text="before restart",
            radio_rx_at=old_timestamp,
            received_at=old_timestamp,
        )
        first_process_store.close()

        second_process_store = ChatStore.open(db_path)
        self.addCleanup(second_process_store.close)
        app = self._make_app(chat_store=second_process_store)
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            alice = next(
                state for state in view.working_set if state.node.node_id == "!a11ce001"
            )
            self.assertTrue(alice.is_client)
            self.assertEqual(alice.last_interaction_at, old_timestamp)

    async def test_most_recent_incoming_message_determines_last_interaction(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            # SIMULATED_MESSAGES[0] and [3] both come from the "!cafe0003"-
            # or "!a11ce001"-style senders at different ages; use the two
            # channel-0 Cafe Relay messages (index 2 is channel 1, index 3
            # is channel 0 and older than index 0's sender is irrelevant --
            # pick two messages from the SAME sender at different ages).
            older = SIMULATED_MESSAGES[3]
            newer = older.__class__(**{**older.__dict__, "radio_rx_at": older.radio_rx_at + 500})
            app._accept_received_message(older)
            app._accept_received_message(newer)
            activity = app._mesh_last_message_activity()
            self.assertEqual(activity[older.sender_node_id.lower()], newer.radio_rx_at)

    async def test_message_older_than_mounted_chat_window_still_makes_client(self) -> None:
        db_path = self.root / "bounded_chat.db"
        now = 1_700_000_000.0
        old_timestamp = now - 30 * 24 * 60 * 60

        store = ChatStore.open(db_path)
        store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice Trail",
            sender_short_name="ALCE",
            channel_index=0,
            text="a message from a month ago",
            radio_rx_at=old_timestamp,
            received_at=old_timestamp,
        )
        for index in range(DEFAULT_HISTORY_LIMIT):
            store.add_incoming(
                packet_id=1000 + index,
                node_id="!f177e001",
                sender_name="Filler",
                sender_short_name="FIL",
                channel_index=0,
                text=f"filler {index}",
                radio_rx_at=now - index,
                received_at=now - index,
            )
        store.close()

        store = ChatStore.open(db_path)
        self.addCleanup(store.close)
        app = self._make_app(chat_store=store)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            mounted_ids = {entry.node_id for entry in app.chat_history if entry.node_id}
            self.assertNotIn("!a11ce001", mounted_ids)
            await pilot.press("3")
            await pilot.pause()
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            alice = next(
                state for state in view.working_set if state.node.node_id == "!a11ce001"
            )
            self.assertTrue(alice.is_client)
            self.assertTrue(alice.is_stale(now=now))

    # ---- RELAY truthfulness (spec section 22) ------------------------

    async def test_no_node_is_ever_relay_from_current_real_data(self) -> None:
        """No data source available to this app identifies a specific relay

        node (see mesh_state.MeshNodeState's docstring) -- RELAY must
        never be fabricated from hop count or a configured role, so
        every real-data node produced by the live app is_relay=False,
        even a node with a high hop count.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertTrue(view.working_set)
            self.assertTrue(all(not state.is_relay for state in view.working_set))

    async def test_high_hop_count_does_not_produce_relay_state(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            far_hop_message = SIMULATED_MESSAGES[0].__class__(
                **{**SIMULATED_MESSAGES[0].__dict__}
            )
            app._accept_received_message(far_hop_message)
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(far_hop_message.sender_node_id, hops_away=9),
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            remote = next(
                state
                for state in view.working_set
                if state.node.node_id == far_hop_message.sender_node_id.lower()
                or state.node.node_id == far_hop_message.sender_node_id
            )
            self.assertFalse(remote.is_relay)

    # ---- Working set (spec section 23) -------------------------------

    async def test_working_set_is_bounded(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for index in range(20):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=f"!n{index:07x}",
                        sender_long_name=f"Node{index}",
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=9_000_000 + index,
                        radio_rx_at=1_700_000_000.0 - index,
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            remote_count = sum(1 for state in view.working_set if not state.node.is_local)
            self.assertEqual(remote_count, DEFAULT_MAX_REMOTE_NODES)

    async def test_stale_fallback_appears_when_nothing_is_recent(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            very_old = 1_700_000_000.0 - MESH_STALE_THRESHOLD_SECONDS * 10
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!oldclient",
                    sender_long_name="OldClient",
                    sender_short_name=None,
                    channel_index=0,
                    text="ancient",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=very_old,
                )
            )
            await pilot.press("3")
            await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_000.0)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertIn("!oldclient", remote_ids)

    async def test_unchanged_data_produces_unchanged_positions(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            first_positions = dict(view.base_positions)
            app._refresh_mesh(wall_now=1_700_000_500.0)
            await pilot.pause()
            self.assertEqual(view.base_positions, first_positions)

    async def test_working_set_nodes_never_overlap(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            positions = list(view.base_positions.values())
            self.assertEqual(len(positions), len(set(positions)))

    async def test_you_is_always_in_the_working_set_when_local_node_known(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertTrue(any(state.node.is_local for state in view.working_set))

    # ---- Geography (spec section 24) ---------------------------------

    async def test_north_node_appears_above_you_south_below(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(
                    app.radio.info.node_id, is_local=True, position=LOCAL_GEO
                ),
                NodeMetadata("!north1", "North1", position=north_of_local(5)),
                NodeMetadata("!south1", "South1", position=south_of_local(5)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!north1",
                    sender_long_name="North1",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!south1",
                    sender_long_name="South1",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=2,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            you_row, _you_col = view.base_positions[app.radio.info.node_id]
            north_row, _ = view.base_positions["!north1"]
            south_row, _ = view.base_positions["!south1"]
            self.assertLess(north_row, you_row)
            self.assertGreater(south_row, you_row)

    async def test_east_node_appears_right_of_you_west_left(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(
                    app.radio.info.node_id, is_local=True, position=LOCAL_GEO
                ),
                NodeMetadata("!east1", "East1", position=east_of_local(5)),
                NodeMetadata("!west1", "West1", position=west_of_local(5)),
            )
            for node_id in ("!east1", "!west1"):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=node_id,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node_id) & 0xFFFF,
                        radio_rx_at=1_700_000_000.0,
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            _you_row, you_column = view.base_positions[app.radio.info.node_id]
            _er, east_column = view.base_positions["!east1"]
            _wr, west_column = view.base_positions["!west1"]
            self.assertGreater(east_column, you_column)
            self.assertLess(west_column, you_column)

    async def test_farther_node_is_at_least_as_many_grid_steps_away(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(
                    app.radio.info.node_id, is_local=True, position=LOCAL_GEO
                ),
                NodeMetadata("!near1", "Near1", position=north_of_local(1)),
                NodeMetadata("!far1", "Far1", position=north_of_local(500)),
            )
            for node_id in ("!near1", "!far1"):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=node_id,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node_id) & 0xFFFF,
                        radio_rx_at=1_700_000_000.0,
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            you_row, _ = view.base_positions[app.radio.info.node_id]
            near_row, _ = view.base_positions["!near1"]
            far_row, _ = view.base_positions["!far1"]
            self.assertGreaterEqual(you_row - far_row, you_row - near_row)

    async def test_missing_gps_fallback_is_deterministic_and_collision_free(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            no_gps_nodes = tuple(
                NodeMetadata(f"!ng{index:02x}", f"NoGps{index}") for index in range(4)
            )
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                *no_gps_nodes,
            )
            for node in no_gps_nodes:
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node.node_id,
                        sender_long_name=node.long_name,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node.node_id) & 0xFFFF,
                        radio_rx_at=1_700_000_000.0,
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            first_positions = dict(view.base_positions)
            self.assertEqual(
                len(first_positions.values()), len(set(first_positions.values()))
            )

            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()
            self.assertEqual(view.base_positions, first_positions)

    # ---- Context (spec section 25) -----------------------------------

    async def test_context_line_for_you(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(status, "YOU")

    async def test_context_line_for_client(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[1])  # Bob, hops_away=1
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            bob_id = SIMULATED_MESSAGES[1].sender_node_id
            view.select_node(bob_id)
            view.set_nodes(
                view.working_set, view.base_positions, theme=app._current_theme, now=1_700_000_100.0
            )
            app._update_mesh_context_status()
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            self.assertIn("CLIENT", status)
            self.assertIn("1 HOPS", status)
            self.assertNotIn("CLIENT+RELAY", status)

    async def test_context_line_unknown_hops_shows_question_mark(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata("!unknownhop", "UnknownHop", hops_away=None),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!unknownhop",
                    sender_long_name="UnknownHop",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            view.select_node("!unknownhop")
            view.set_nodes(
                view.working_set, view.base_positions, theme=app._current_theme, now=1_700_000_000.0
            )
            app._update_mesh_context_status()
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            self.assertIn("? HOPS", status)
            self.assertNotIn("0 HOPS", status)

    def test_context_line_supports_client_and_relay_and_relay_only_format(self) -> None:
        """Formatting-level proof (see mesh_state tests): the live app's

        role-to-glyph and context-line logic supports CLIENT+RELAY and
        RELAY-only correctly even though no current real-data path can
        produce them (see MeshNodeState's docstring on why is_relay is
        always False today) -- so the code is truthfully ready without
        the live app ever fabricating either state.
        """
        client_and_relay = MeshNodeState(
            node=NodeMetadata("!cr", "ClientRelay", 0),
            is_client=True,
            is_relay=True,
            last_interaction_at=1_700_000_000.0,
        )
        relay_only = MeshNodeState(
            node=NodeMetadata("!ro", "RelayOnly", 2),
            is_client=False,
            is_relay=True,
            last_interaction_at=1_700_000_000.0,
        )
        self.assertIn(
            "CLIENT+RELAY", format_mesh_context_line(client_and_relay, now=1_700_000_100.0)
        )
        self.assertIn("RELAY", format_mesh_context_line(relay_only, now=1_700_000_100.0))
        self.assertNotIn("CLIENT", format_mesh_context_line(relay_only, now=1_700_000_100.0))

    # ---- Selection preservation / navigation (spec section 18/19) ----

    async def test_selection_preserved_across_refresh_if_still_present(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            alice_id = SIMULATED_MESSAGES[0].sender_node_id
            view.select_node(alice_id)
            view.set_nodes(
                view.working_set, view.base_positions, theme=app._current_theme, now=1_700_000_100.0
            )
            await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()
            self.assertEqual(view.selected_node_id.lower(), alice_id.lower())

    async def test_selection_falls_back_to_you_if_it_disappears(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            alice_id = SIMULATED_MESSAGES[0].sender_node_id
            view.select_node(alice_id)
            view.set_nodes(
                view.working_set, view.base_positions, theme=app._current_theme, now=1_700_000_100.0
            )
            await pilot.pause()

            # Alice no longer has any recorded interaction (CHAT cleared)
            # NOR any passive NodeDB record (get_known_nodes overridden
            # to YOU only) -- under NodeDB-first admission (see
            # build_mesh_working_set), losing CHAT history alone is no
            # longer enough to make a real known node disappear, so both
            # sources must be removed to genuinely test this fallback.
            app._channel_states.clear()
            app.chat_store = None
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
            )
            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()
            self.assertTrue(view.selected_node_id)
            selected_state = next(
                state
                for state in view.working_set
                if state.node.node_id == view.selected_node_id
            )
            self.assertTrue(selected_state.node.is_local)

    async def test_navigation_is_general_not_hardcoded_by_node_id(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata("!zzzznode", "ZzzzNode", position=north_of_local(2)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!zzzznode",
                    sender_long_name="ZzzzNode",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, app.radio.info.node_id)
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!zzzznode")

    async def test_arrow_navigation_full_regression_with_realistic_node_ids(
        self,
    ) -> None:
        """End-to-end regression for the reported "arrow keys do nothing"

        bug: YOU plus real-looking "!xxxxxxxx" node IDs (the exact format
        RadioService.get_known_nodes() returns) in every cardinal
        direction, proving the full navigation contract holds against
        live-shaped data, not just the clean synthetic IDs used
        elsewhere in this file. Hop count is 0 for all four so no
        anonymous relay stage is generated -- this test is specifically
        about ID handling in the real-node navigation path, not the
        separate relay-stage-traversal contract proven by
        test_navigation_walks_through_relay_stages_and_back.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            north_id, south_id, east_id, west_id = (
                "!0a0a0a0a",
                "!0b0b0b0b",
                "!0c0c0c0c",
                "!0d0d0d0d",
            )
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(north_id, "North Real", "NORT", 0, position=north_of_local(3)),
                NodeMetadata(south_id, "South Real", "SOUT", 0, position=south_of_local(3)),
                NodeMetadata(east_id, "East Real", "EAST", 0, position=east_of_local(3)),
                NodeMetadata(west_id, "West Real", "WEST", 0, position=west_of_local(3)),
            )
            for index, node_id in enumerate((north_id, south_id, east_id, west_id)):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=f"Node{index}",
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=100 + index,
                        radio_rx_at=1_700_000_000.0,
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)

            # Arrow event is claimed by MESH; YOU is selected by default.
            self.assertEqual(view.selected_node_id, you_id)
            original_relative_positions = {
                node_id: (
                    view.base_positions[node_id][0] - view.base_positions[you_id][0],
                    view.base_positions[node_id][1] - view.base_positions[you_id][1],
                )
                for node_id in (you_id, north_id, south_id, east_id, west_id)
            }
            board_offset_before = view.board.styles.offset

            for key, expected_id in (
                ("up", north_id),
                ("down", you_id),
                ("down", south_id),
                ("up", you_id),
                ("right", east_id),
                ("left", you_id),
                ("left", west_id),
                ("right", you_id),
            ):
                await pilot.press(key)
                await pilot.pause()
                with self.subTest(key=key, expected=expected_id):
                    # Selected ID corresponds to an actual rendered node.
                    self.assertEqual(view.selected_node_id, expected_id)
                    self.assertIn(
                        expected_id, {w.node_id for w in app.query(MeshNodeWidget)}
                    )
                    selected_widget = next(
                        w for w in app.query(MeshNodeWidget) if w.node_id == expected_id
                    )
                    self.assertEqual(
                        selected_widget.render().spans[0].style.foreground,
                        Color.parse(THEME_PALETTES[app._current_theme].accent),
                    )
                    # Bottom-left context reflects the new selection.
                    status = str(app.query_one("#mesh-context-status").render())
                    if expected_id == you_id:
                        self.assertEqual(status, "YOU")
                    else:
                        self.assertNotEqual(status, "YOU")
                    # The mesh recentered: the selected node's own base
                    # position is now the center anchor.
                    positions = _mesh_translated_positions(
                        view.base_positions, view.selected_node_id
                    )
                    self.assertEqual(
                        positions[expected_id], (MESH_GRID_CENTER_ROW, MESH_GRID_CENTER_COLUMN)
                    )
                    # Relative geometry between every node is unchanged --
                    # recentering is a pure translation, not a reshuffle.
                    for node_id in (you_id, north_id, south_id, east_id, west_id):
                        relative = (
                            positions[node_id][0] - positions[expected_id][0],
                            positions[node_id][1] - positions[expected_id][1],
                        )
                        expected_relative = (
                            original_relative_positions[node_id][0]
                            - original_relative_positions[expected_id][0],
                            original_relative_positions[node_id][1]
                            - original_relative_positions[expected_id][1],
                        )
                        self.assertEqual(relative, expected_relative)
                    # No scrolling: the board container's own offset never
                    # moves (only individual node widgets do).
                    self.assertEqual(view.board.styles.offset, board_offset_before)
                    self.assertFalse(view.show_vertical_scrollbar)
                    self.assertFalse(view.show_horizontal_scrollbar)

    async def test_navigation_works_even_when_focus_is_on_the_board_container(
        self,
    ) -> None:
        """Arrow-key MESH navigation is gated on the current tab, not on

        Textual focus residing on any particular node widget -- explicitly
        move focus to the board container itself and confirm navigation
        still works.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata("!0e0e0e0e", "Northern", position=north_of_local(2)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!0e0e0e0e",
                    sender_long_name="Northern",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            view.focus()
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!0e0e0e0e")

    async def test_navigation_survives_decimal_vs_hex_node_id_mismatch(self) -> None:
        """End-to-end version of the mesh_state-level regression test: even

        if CHAT activity reports a sender under a bare decimal node
        number while get_known_nodes() reports the same physical node in
        the standard "!hex" form, arrow navigation must still reach the
        real, positioned, rendered node -- not silently fail or land on
        a nameless duplicate. Hop count is 0 so no relay stage sits
        between YOU and it, keeping this test focused on ID handling.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata("!075bcd15", "North Node", "NORTH", 0, position=north_of_local(3)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="123456789",  # decimal for 0x075bcd15
                    sender_long_name="North Node",
                    sender_short_name="NORTH",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(
                {state.node.node_id for state in view.working_set},
                {app.radio.info.node_id, "!075bcd15"},
            )
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!075bcd15")
            status = str(app.query_one("#mesh-context-status").render())
            self.assertTrue(status.startswith("North Node"))

    # ---- Anonymous relay-stage topology (hop-count based) --------------

    async def _open_mesh_with_directional_clients(
        self, pilot, *, hops: dict
    ) -> "MeshtasticPassApp":
        """Wire up to four clients, one per cardinal direction, each with

        the given hops_away -- avoiding same-bucket contention so each
        client's own relay chain has genuinely interior grid cells.
        """
        app = pilot.app
        directions = {
            "north": ("!d0000001", north_of_local(3)),
            "south": ("!d0000002", south_of_local(3)),
            "east": ("!d0000003", east_of_local(3)),
            "west": ("!d0000004", west_of_local(3)),
        }
        nodes = [NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)]
        for name, (node_id, position) in directions.items():
            nodes.append(
                NodeMetadata(node_id, name.title(), name[:4].upper(), hops.get(name), position=position)
            )
        app.radio.get_known_nodes = lambda nodes=tuple(nodes): nodes
        for name, (node_id, _position) in directions.items():
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name=name.title(),
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=hash(node_id) & 0xFFFF,
                    radio_rx_at=1_700_000_000.0,
                )
            )
        await self._open_mesh(pilot)
        return directions

    async def test_hop_count_topology_generates_correct_relay_stage_counts(
        self,
    ) -> None:
        """0/1/2/3 HOPS -> exactly that many anonymous relay stages each

        (spec section 2/32), never fewer, never more, one client per
        compass direction so counts can't be confused across clients.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            directions = await self._open_mesh_with_directional_clients(
                pilot, hops={"north": 0, "south": 1, "east": 2, "west": 3}
            )
            view = app.query_one(MeshTopologyView)
            for name, expected_count in (
                ("north", 0),
                ("south", 1),
                ("east", 2),
                ("west", 3),
            ):
                node_id = directions[name][0]
                with self.subTest(direction=name):
                    stage_count = sum(
                        1 for stage in view.relay_stages if stage.source_node_id == node_id
                    )
                    self.assertEqual(stage_count, expected_count)
            self.assertEqual(len(view.relay_stages), 0 + 1 + 2 + 3)
            self.assertEqual(
                {w.node_id for w in app.query(MeshRelayWidget)},
                {stage.node_id for stage in view.relay_stages},
            )

    async def test_unknown_hops_client_has_no_relay_stages_but_stays_visible(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            directions = await self._open_mesh_with_directional_clients(
                pilot, hops={"north": None, "south": None, "east": None, "west": None}
            )
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.relay_stages, ())
            self.assertEqual(len(list(app.query(MeshRelayWidget))), 0)
            north_id = directions["north"][0]
            self.assertIn(north_id, {w.node_id for w in app.query(MeshNodeWidget)})

    async def test_relay_stages_never_count_toward_active_or_working_set_bound(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await self._open_mesh_with_directional_clients(
                pilot, hops={"north": 3, "south": 3, "east": 3, "west": 3}
            )
            view = app.query_one(MeshTopologyView)
            remote_count = sum(1 for state in view.working_set if not state.node.is_local)
            self.assertEqual(remote_count, 4)
            self.assertGreater(len(view.relay_stages), 0)

    async def test_navigation_skips_over_relay_stages_directly_to_real_node(
        self,
    ) -> None:
        """Anonymous relay stages are visual topology only -- NOT

        navigable (spec update: "UNKNOWN RELAYS ARE NOT SELECTABLE"). A
        single 2-hop client sits with two relay stages geometrically
        between it and YOU; a single "up" press from YOU must land
        directly on the real node in one press, never stopping on
        either relay stage, and a single "down" press from the real
        node returns directly to YOU. No wrapping, no scrolling.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!ta4be701"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(target_id, "Target Node", "TGT", 2, position=north_of_local(9)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Target Node",
                    sender_short_name="TGT",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, you_id)
            self.assertEqual(len(view.relay_stages), 2)
            relay_ids = {stage.node_id for stage in view.relay_stages}
            board_offset_before = view.board.styles.offset

            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, target_id)
            self.assertNotIn(view.selected_node_id, relay_ids)
            status = str(app.query_one("#mesh-context-status").render())
            self.assertTrue(status.startswith("Target Node"))

            # No wrapping: one more "up" from the real node changes nothing.
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, target_id)

            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, you_id)
            self.assertEqual(str(app.query_one("#mesh-context-status").render()), "YOU")

            self.assertEqual(view.board.styles.offset, board_offset_before)
            self.assertFalse(view.show_vertical_scrollbar)
            self.assertFalse(view.show_horizontal_scrollbar)

    async def test_relay_stage_cannot_be_selected_and_always_renders_dim(
        self,
    ) -> None:
        """Anonymous relay stages are never focusable/selectable: no

        on_click, never ACCENT, never the enlarged composite, never the
        bottom-left context, and MeshTopologyView.select_node() rejects
        a relay stage ID outright as a no-op rather than accepting it
        and relying on a later set_nodes() call to repair it (spec
        update section "UNKNOWN RELAYS ARE NOT SELECTABLE").
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!re1a5001"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(target_id, "Relay Target", "RLT", 1, position=east_of_local(9)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Relay Target",
                    sender_short_name="RLT",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 1)
            stage_id = view.relay_stages[0].node_id
            palette = THEME_PALETTES[app._current_theme]

            self.assertFalse(hasattr(MeshRelayWidget, "on_click"))
            self.assertFalse(MeshRelayWidget.can_focus)

            relay_widget = next(w for w in app.query(MeshRelayWidget) if w.node_id == stage_id)
            self.assertEqual(
                relay_widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            self.assertEqual(int(relay_widget.styles.width.value), 1)
            self.assertEqual(str(relay_widget.render()), CIRCLE_STROKED_LARGE)
            self.assertEqual(
                next((w for w in app.query(MeshNodeLabelWidget) if w.node_id == stage_id), None),
                None,
            )

            # Directly attempting to select a relay stage ID (there is no
            # UI path that does this anymore -- proving the model layer
            # itself refuses, not just the removed click handler): YOU is
            # already selected here, and MeshTopologyView.select_node()
            # rejects the relay ID outright as a no-op (see
            # test_select_node_accepts_only_real_working_set_nodes for
            # the dedicated proof) -- selection simply never moves off YOU.
            _mesh_select_node(app, stage_id)
            await pilot.pause()
            self.assertEqual(view.selected_node_id, you_id)
            self.assertNotEqual(view.selected_node_id, stage_id)
            relay_widget = next(w for w in app.query(MeshRelayWidget) if w.node_id == stage_id)
            self.assertEqual(
                relay_widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            self.assertEqual(int(relay_widget.styles.width.value), 1)
            self.assertEqual(str(app.query_one("#mesh-context-status").render()), "YOU")

    async def test_select_node_accepts_only_real_working_set_nodes(self) -> None:
        """MeshTopologyView.select_node() is authoritative: only a real

        node currently in `working_set` may ever become
        `selected_node_id`. An anonymous RelayStage ID or an arbitrary
        unknown ID is rejected outright as a no-op right here, never
        even momentarily accepted and relying on a later set_nodes()
        call to repair it (spec update: tighten the selection boundary
        at select_node() itself, not just at the removed click handler
        or the navigation candidate filter). Requirement 5 (falling
        back to YOU when the selected REAL node disappears on refresh)
        is proven separately by
        test_selection_falls_back_to_you_if_it_disappears -- that path
        goes through set_nodes()'s own fallback, not select_node(),
        and is unaffected by this change.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            alice_id = "!a1ice001"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(alice_id, "Alice", "ALC", 1, position=north_of_local(9)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=alice_id,
                    sender_long_name="Alice",
                    sender_short_name="ALC",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 1)
            relay_id = view.relay_stages[0].node_id

            # 1. Real working-set node selection succeeds.
            view.select_node(alice_id)
            self.assertEqual(view.selected_node_id, alice_id)

            # 2. Anonymous relay-stage ID selection is a no-op.
            view.select_node(relay_id)
            self.assertEqual(view.selected_node_id, alice_id)
            self.assertNotEqual(view.selected_node_id, relay_id)

            # 3. Arbitrary/unknown ID selection is a no-op.
            view.select_node("!totally-unknown-id")
            self.assertEqual(view.selected_node_id, alice_id)

            # 4. selected_node_id never even momentarily becomes a relay ID.
            for _ in range(3):
                view.select_node(relay_id)
                self.assertNotEqual(view.selected_node_id, relay_id)
            self.assertEqual(view.selected_node_id, alice_id)

            # Normal real-node selection behavior is otherwise unchanged.
            view.select_node(you_id)
            self.assertEqual(view.selected_node_id, you_id)

    async def test_navigation_candidate_set_excludes_relay_stage_ids(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!ca4d1da1"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(target_id, "Alice", "ALC", 3, position=west_of_local(9)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Alice",
                    sender_short_name="ALC",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 3)
            relay_ids = {stage.node_id for stage in view.relay_stages}

            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, target_id)
            self.assertEqual(relay_ids & {view.selected_node_id}, set())

            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, you_id)

    async def test_multiple_clients_same_hop_count_get_independent_relay_stages(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            alice_id, bob_id = "!a11cebbb", "!b0bbbbbb"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(alice_id, "Alice", "ALC", 2, position=east_of_local(9)),
                NodeMetadata(bob_id, "Bob", "BOB", 2, position=west_of_local(9)),
            )
            for node_id, name in ((alice_id, "Alice"), (bob_id, "Bob")):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=name,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node_id) & 0xFFFF,
                        radio_rx_at=1_700_000_000.0,
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 4)
            alice_stage_ids = {
                s.node_id for s in view.relay_stages if s.source_node_id == alice_id
            }
            bob_stage_ids = {s.node_id for s in view.relay_stages if s.source_node_id == bob_id}
            self.assertEqual(len(alice_stage_ids), 2)
            self.assertEqual(len(bob_stage_ids), 2)
            self.assertEqual(alice_stage_ids & bob_stage_ids, set())
            positions = list(view.base_positions.values())
            self.assertEqual(len(positions), len(set(positions)))

    # ---- ACTIVE / brightness predicate unification (spec section 17-20) --

    async def test_active_count_and_node_brightness_use_same_predicate(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            client_ids = [f"!ac00000{i}" for i in range(8)]
            active_ids = set(client_ids[:4])
            nodes = [NodeMetadata(app.radio.info.node_id, is_local=True)]
            for index, node_id in enumerate(client_ids):
                last_heard = (
                    now - 10
                    if node_id in active_ids
                    else now - ACTIVE_WINDOW_SECONDS - 100
                )
                nodes.append(NodeMetadata(node_id, f"Node{index}", last_heard=last_heard))
            app.radio.get_known_nodes = lambda nodes=tuple(nodes): nodes
            for index, node_id in enumerate(client_ids):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=f"Node{index}",
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=9_500_000 + index,
                        radio_rx_at=now - index,
                    )
                )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn("MESH (4)", tab_bar)

            view = app.query_one(MeshTopologyView)
            self.assertNotIn(view.selected_node_id, client_ids)
            palette = THEME_PALETTES[app._current_theme]
            for node_id in client_ids:
                widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
                color = widget.render().spans[0].style.foreground
                with self.subTest(node_id=node_id, active=node_id in active_ids):
                    if node_id in active_ids:
                        self.assertEqual(color, Color.parse(palette.base))
                    else:
                        self.assertEqual(color, Color.parse(palette.dim_base))

    async def test_relay_stage_brightness_ignores_activity_and_is_never_counted(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!d1d0007f"
            now = 1_700_000_000.0
            # A recently-heard (ACTIVE) client, but its relay stage must
            # still always render DIM_BASE unless selected -- it has no
            # identity of its own to be "heard" from.
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    target_id, "DimTarget", "DIM", 1, last_heard=now - 5, position=north_of_local(9)
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="DimTarget",
                    sender_short_name="DIM",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 1)
            palette = THEME_PALETTES[app._current_theme]
            stage_widget = next(iter(app.query(MeshRelayWidget)))
            self.assertEqual(
                stage_widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn("MESH (1)", tab_bar)

    async def test_active_header_never_counts_nodes_outside_the_displayed_working_set(
        self,
    ) -> None:
        """Regression, updated for NodeDB-first admission (see

        build_mesh_working_set): the original observed uConsole bug was
        a population mismatch -- ACTIVE counted activity across the FULL
        known-node population (get_known_nodes()), independent of the
        bounded working set actually rendered, so an active node that
        never made the bounded cut could inflate the header without ever
        appearing on the board.

        A CHAT message is no longer what keeps a node off the board --
        NodeDB-first admission means any known node is a candidate, so
        the only way a real active node now fails to appear is by
        losing the bound (max_remote_nodes) to higher-ranked candidates
        (see build_mesh_working_set's tiered ranking). This fixture
        supplies MORE currently-active real nodes than the bound allows,
        confirming the displayed board and [3] MESH (N) still agree
        exactly: N equals precisely how many of the DISPLAYED nodes are
        active, never the size of the broader known-node population.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            active_ids = [f"!ac00000{i}" for i in range(DEFAULT_MAX_REMOTE_NODES + 2)]

            nodes = [NodeMetadata(app.radio.info.node_id, is_local=True)]
            for node_id in active_ids:
                nodes.append(NodeMetadata(node_id, node_id, last_heard=now - 10))
            app.radio.get_known_nodes = lambda nodes=tuple(nodes): nodes

            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            displayed_remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertEqual(len(displayed_remote_ids), DEFAULT_MAX_REMOTE_NODES)
            self.assertLess(len(displayed_remote_ids), len(active_ids))
            # Every candidate here is equally active -- the bound simply
            # keeps the board readable, but every node it DOES keep must
            # still count, and none of the ones it drops may leak in.
            all_displayed_ids = {state.node.node_id for state in view.working_set}
            self.assertEqual(
                {w.node_id for w in app.query(MeshNodeWidget)}, all_displayed_ids
            )

            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn(f"MESH ({DEFAULT_MAX_REMOTE_NODES})", tab_bar)

    async def test_activity_styling_refreshes_without_leaving_mesh_tab(self) -> None:
        """The user should not have to leave/re-enter MESH for active

        styling (or ACTIVE N) to become correct as time passes -- the
        existing 1s _refresh_chat_timestamps timer already calls
        _refresh_mesh() while the MESH tab is current; this proves the
        resulting recompute actually updates both the header and the
        node's own color, entirely via repeated _refresh_mesh() calls
        with no additional tab switch and no new radio traffic.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            node_id = "!c0055ing1"
            heard_at = 1_700_000_000.0
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(node_id, "Crosser", last_heard=heard_at),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Crosser",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=heard_at,
                )
            )
            await self._open_mesh(pilot)
            palette = THEME_PALETTES[app._current_theme]

            app._refresh_mesh(wall_now=heard_at + 10)
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            self.assertIn("MESH (1)", str(app.query_one("#tab-bar").render()))
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
            self.assertEqual(widget.render().spans[0].style.foreground, Color.parse(palette.base))

            # Same session, same tab -- only wall-clock time advanced past
            # the active window, exactly as the 1s timer would apply it.
            app._refresh_mesh(wall_now=heard_at + ACTIVE_WINDOW_SECONDS + 50)
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            self.assertIn("MESH (0)", str(app.query_one("#tab-bar").render()))
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            self.assertEqual(app.radio.sent_messages, ())

    # ---- UI consolidation: [3] MESH (N) replaces the internal header ---

    async def test_mesh_internal_view_no_longer_shows_active_status_text(
        self,
    ) -> None:
        """The in-view "> MESH · ACTIVE N" heading is gone entirely --

        the count now lives only in the top-nav tab label. The
        topology board and the bottom-left selected-node context line
        are unaffected.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            node_id = "!ac71ve01"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(node_id, "Active", last_heard=now - 5),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Active",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            self.assertEqual(list(app.query("#mesh-title")), [])
            status_text = str(app.query_one("#mesh-status").render())
            context_text = str(app.query_one("#mesh-context-status").render())
            for text in (status_text, context_text):
                self.assertNotIn("ACTIVE", text)
                self.assertNotIn("> MESH", text)
            # The board and bottom-left context are untouched.
            self.assertIsNotNone(app.query_one(MeshTopologyView))
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
            self.assertIsNotNone(widget)

    async def test_mesh_shows_shared_status_while_connecting(self) -> None:
        """MESH's top status must use the exact same normalized value

        CONNECTION/CONFIG and CHAT use -- never MESH-specific wording
        like the old "NO MESH DATA -- RADIO DISCONNECTED".
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._show_connection(RadioState.OFFLINE, message="lost")
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")

            status_widget = app.query_one("#mesh-connection-status")
            self.assertTrue(status_widget.display)
            text = str(status_widget.render())
            self.assertIn(f"STATUS {ANIMATED_STATUS[RadioState.OFFLINE]}", text)
            self.assertNotIn("RADIO DISCONNECTED", text)
            self.assertNotIn("RECONNECTING", text)

    async def test_mesh_connection_status_disappears_once_connected(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            for _ in range(5):
                await pilot.pause()
                if app._radio_state is RadioState.ONLINE:
                    break
            await pilot.press("3")
            await pilot.pause()
            self.assertEqual(app._radio_state, RadioState.ONLINE)

            status_widget = app.query_one("#mesh-connection-status")
            self.assertFalse(status_widget.display)
            self.assertEqual(str(status_widget.render()), "")

    async def test_mesh_topology_not_rebuilt_by_connection_status_change(
        self,
    ) -> None:
        """Stale topology intentionally remains visible during a

        connection drop -- only the shared top status communicates
        "not live right now"; the board itself must not be cleared or
        rebuilt by the status change alone.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            node_id = "!sta1e001"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(node_id, "Stale", last_heard=now - 5),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Stale",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.working_set), 2)
            before_positions = dict(view.base_positions)

            app._show_connection(RadioState.OFFLINE, message="lost")
            await pilot.pause()
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            self.assertEqual(app.current_tab, "mesh")
            status_widget = app.query_one("#mesh-connection-status")
            self.assertTrue(status_widget.display)
            self.assertEqual(len(view.working_set), 2)
            self.assertEqual(dict(view.base_positions), before_positions)
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
            self.assertIsNotNone(widget)

    async def test_mesh_tab_shows_zero_when_no_active_displayed_remote_nodes(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            node_id = "!1nact1ve"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(
                    node_id, "Stale", last_heard=now - ACTIVE_WINDOW_SECONDS - 100
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Stale",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertIn("MESH (0)", str(app.query_one("#tab-bar").render()))

    async def test_mesh_tab_shows_two_when_two_displayed_real_nodes_are_active(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            active_ids = ["!act1ve01", "!act1ve02"]
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(active_ids[0], "A1", last_heard=now - 5),
                NodeMetadata(active_ids[1], "A2", last_heard=now - 5),
            )
            for index, node_id in enumerate(active_ids):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=f"A{index + 1}",
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=9_800_000 + index,
                        radio_rx_at=now - index,
                    )
                )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertIn("MESH (2)", str(app.query_one("#tab-bar").render()))

    async def test_anonymous_relay_placeholders_do_not_affect_tab_count(self) -> None:
        """A distant CLIENT generates anonymous relay-stage placeholders

        along its path to YOU -- those placeholders must never be
        counted, only the real, active CLIENT itself.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            distant_id = "!d1stant1"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    distant_id,
                    "Distant",
                    "DIST",
                    2,
                    last_heard=now - 5,
                    position=north_of_local(20),
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=distant_id,
                    sender_long_name="Distant",
                    sender_short_name="DIST",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertGreaterEqual(len(view.relay_stages), 1)
            # Exactly one real, active remote node -- the relay stage(s)
            # it generated along the way must not inflate the count.
            self.assertIn("MESH (1)", str(app.query_one("#tab-bar").render()))

    async def test_you_does_not_count_toward_tab_count(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            # YOU given a suspiciously recent last_heard -- if the
            # predicate ever forgot to exclude is_local, this would
            # wrongly count as 1.
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, last_heard=now - 1),
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertIn("MESH (0)", str(app.query_one("#tab-bar").render()))

    async def test_mesh_tab_count_updates_without_leaving_or_reentering_mesh(
        self,
    ) -> None:
        """Same guarantee as test_activity_styling_refreshes_without_

        leaving_mesh_tab, but observed from a DIFFERENT tab -- the count
        must update via the same periodic refresh even while MESH isn't
        the visible tab, and must never force a topology rebuild to do
        it (see _mesh_active_count's docstring).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            node_id = "!crossing"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(node_id, "Crosser", last_heard=now),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Crosser",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            app.show_tab("connection")
            await pilot.pause()
            self.assertEqual(app.current_tab, "connection")

            app._refresh_chat_timestamps(wall_now=now)
            await pilot.pause()
            self.assertIn("MESH (1)", str(app.query_one("#tab-bar").render()))

            app._refresh_chat_timestamps(wall_now=now + ACTIVE_WINDOW_SECONDS + 50)
            await pilot.pause()
            self.assertEqual(app.current_tab, "connection")
            self.assertIn("MESH (0)", str(app.query_one("#tab-bar").render()))
            self.assertEqual(app.radio.sent_messages, ())

    # ---- Screenshot regression: relay count vs line-bend count ----------

    @staticmethod
    def _client_chain_points(view, you_id, remote_id):
        stages = sorted(
            (stage for stage in view.relay_stages if stage.source_node_id == remote_id),
            key=lambda stage: stage.index,
        )
        node_ids = [you_id, *(stage.node_id for stage in stages), remote_id]
        return tuple(_mesh_grid_pixel(*view.base_positions[node_id]) for node_id in node_ids)

    @classmethod
    def _count_bends(cls, view, you_id, remote_id):
        chain_points = cls._client_chain_points(view, you_id, remote_id)
        elbow_glyphs = {"┐", "┘", "┌", "└"}
        return sum(1 for _x, _y, glyph in route_chain(chain_points) if glyph in elbow_glyphs)

    async def _diagnose_topology(self, app, pilot, *, clients, now):
        """Build the given {node_id: (name, hops_away)} clients NE/around

        YOU, refresh MESH, and return a per-client diagnostic dict of
        {node_id: {hops_away, relay_count, bend_count}} -- exactly the
        report requested to distinguish (A) wrong hop-count
        interpretation, (B) duplicated relays, (C) waypoints rendered
        as relays, or (D) something else, before any fix is written.
        """
        you_id = app.radio.info.node_id
        nodes = [NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)]
        for node_id, (name, hops, position) in clients.items():
            nodes.append(
                NodeMetadata(
                    node_id, name, name[:4].upper(), hops, last_heard=now - 5, position=position
                )
            )
        app.radio.get_known_nodes = lambda nodes=tuple(nodes): nodes
        for index, (node_id, (name, _hops, _position)) in enumerate(clients.items()):
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name=name,
                    sender_short_name=name[:4].upper(),
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=9_900_000 + index,
                    radio_rx_at=now - index,
                )
            )
        await self._open_mesh(pilot)
        app._refresh_mesh(wall_now=now)
        await pilot.pause()

        view = app.query_one(MeshTopologyView)
        report = {}
        for node_id, (_name, hops, _position) in clients.items():
            relay_count = sum(
                1 for stage in view.relay_stages if stage.source_node_id == node_id
            )
            bend_count = self._count_bends(view, you_id, node_id)
            report[node_id] = {
                "hops_away": hops,
                "relay_count": relay_count,
                "bend_count": bend_count,
            }
            print(
                f"[MESH DIAGNOSTIC] node={node_id} hops_away={hops} "
                f"synthetic_relays={relay_count} line_waypoints(bends)={bend_count}"
            )
        return report

    async def test_relay_count_is_determined_only_by_hop_count_not_by_line_bends(
        self,
    ) -> None:
        """Screenshot regression: a chain of many anonymous relay circles

        must only ever appear if the client's own reported hop count is
        genuinely that large -- never as a side effect of how many
        orthogonal bends its connector line happens to need. Two
        topologies with the IDENTICAL client/hop-count set, one client
        positioned to produce few line bends and the other positioned
        (northeast, off-axis) to force several, must report the exact
        same relay count per client -- geometry must never manufacture
        topology.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            few_bends_clients = {
                "!ax1a1000": ("AxisA", 2, north_of_local(6)),
                "!ax1b2000": ("AxisB", 1, GeoPosition(0.0, 6 / 69.0)),
            }
            report_few_bends = await self._diagnose_topology(
                app, pilot, clients=few_bends_clients, now=now
            )
            self.assertGreaterEqual(
                sum(info["bend_count"] for info in report_few_bends.values()), 0
            )

        app2 = self._make_app()
        async with app2.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            # Same node IDs, same hop counts -- but off-axis (northeast)
            # positions, which force the orthogonal router to take
            # several elbow turns per segment instead of a straight run.
            many_bends_clients = {
                "!ax1a1000": ("AxisA", 2, GeoPosition(6 / 69.0, 6 / 69.0)),
                "!ax1b2000": ("AxisB", 1, GeoPosition(4 / 69.0, 7 / 69.0)),
            }
            report_many_bends = await self._diagnose_topology(
                app2, pilot, clients=many_bends_clients, now=now
            )

        for node_id in few_bends_clients:
            with self.subTest(node_id=node_id):
                few = report_few_bends[node_id]
                many = report_many_bends[node_id]
                self.assertEqual(few["hops_away"], many["hops_away"])
                self.assertEqual(
                    few["relay_count"],
                    few["hops_away"],
                    "relay count must equal hop count exactly",
                )
                self.assertEqual(
                    many["relay_count"],
                    many["hops_away"],
                    "relay count must equal hop count exactly regardless of bends",
                )
                self.assertEqual(
                    few["relay_count"],
                    many["relay_count"],
                    "changing the number of line bends must not change the relay count",
                )

    async def test_northeast_client_many_bends_still_matches_hop_count_exactly(
        self,
    ) -> None:
        """The exact screenshot shape: YOU, several real clients, one

        positioned northeast, orthogonally routed with several bends.
        Logs the full diagnostic report and asserts every displayed
        client's relay count is determined solely by its own hop count.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            clients = {
                "!ne000001": ("Northeast5", 3, GeoPosition(6 / 69.0, 9 / 69.0)),
                "!n0000002": ("North2", 1, north_of_local(5)),
                "!e0000003": ("East3", 2, GeoPosition(0.0, 8 / 69.0)),
                "!d0000004": ("Direct4", 0, GeoPosition(-3 / 69.0, 2 / 69.0)),
            }
            report = await self._diagnose_topology(app, pilot, clients=clients, now=now)

            view = app.query_one(MeshTopologyView)
            self.assertEqual(
                len(view.relay_stages),
                sum(hops for _name, hops, _position in clients.values()),
            )
            for node_id, (_name, hops, _position) in clients.items():
                with self.subTest(node_id=node_id):
                    self.assertEqual(report[node_id]["relay_count"], hops)
                    # Every relay for this client is a genuine RelayStage,
                    # never a raw line-waypoint glyph: relay circles are
                    # mounted MeshRelayWidgets with this client's own
                    # node_id baked in, entirely independent of however
                    # many background connector-line cells were drawn.
                    relay_ids = {
                        stage.node_id
                        for stage in view.relay_stages
                        if stage.source_node_id == node_id
                    }
                    self.assertEqual(len(relay_ids), hops)
                    mounted = {
                        widget.node_id
                        for widget in app.query(MeshRelayWidget)
                        if widget.node_id in relay_ids
                    }
                    self.assertEqual(mounted, relay_ids)

    # ---- Expanded selected-node context (spec section 21-27) -----------

    async def test_selected_context_full_format_with_distance(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!c0ffee01"
            target_position = north_of_local(4.2)
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    target_id, "Bob Basecamp", "BOB", 1, position=target_position
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Bob Basecamp",
                    sender_short_name="BOB",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_699_999_970.0,
                )
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, target_id)
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            # _update_mesh_context_status() formats AGE against real
            # wall-clock time() (see app.py), so -- like the pre-existing
            # test_context_line_for_client -- this cannot pin an exact
            # AGE string; only the name/short-name/role/hops/distance
            # segments, which are wall-clock-independent, are asserted
            # exactly (spec section 37: no test may depend on
            # uncontrolled wall-clock timing).
            segments = status.split(" / ")
            self.assertEqual(len(segments), 6)
            expected_distance = distance_between(LOCAL_GEO, target_position)
            self.assertEqual(
                [segments[0], segments[1], segments[2], segments[3], segments[5]],
                ["Bob Basecamp", "BOB", "CLIENT", "1 HOPS", f"{expected_distance:.1f} mi"],
            )
            self.assertNotEqual(segments[4], "?")

    async def test_selected_context_missing_short_name_and_distance(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!c0ffee02"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True),  # no position -> "? mi"
                NodeMetadata(target_id, "No Short Name", None, 1),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="No Short Name",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, target_id)
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(len(status.split(" / ")), 5)  # no Short Name segment
            self.assertTrue(status.endswith("? mi"))
            self.assertTrue(status.startswith("No Short Name / CLIENT"))

    async def test_distance_unchanged_by_recentering(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            alice_id, bob_id = "!a1a1a1a1", "!b2b2b2b2"
            alice_position = north_of_local(3.7)
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(alice_id, "Alice", "ALC", 1, position=alice_position),
                NodeMetadata(bob_id, "Bob", "BOB", 1, position=south_of_local(5)),
            )
            for node_id, name in ((alice_id, "Alice"), (bob_id, "Bob")):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=name,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node_id) & 0xFFFF,
                        radio_rx_at=1_700_000_000.0,
                    )
                )
            await self._open_mesh(pilot)

            _mesh_select_node(app, alice_id)
            await pilot.pause()
            first_status = str(app.query_one("#mesh-context-status").render())
            first_distance = first_status.rsplit(" / ", 1)[-1]

            # Selecting Bob recenters the whole board on him.
            _mesh_select_node(app, bob_id)
            await pilot.pause()
            self.assertNotEqual(
                str(app.query_one("#mesh-context-status").render()), first_status
            )

            _mesh_select_node(app, alice_id)
            await pilot.pause()
            second_status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(second_status, first_status)
            self.assertEqual(second_status.rsplit(" / ", 1)[-1], first_distance)

    async def test_no_scrollbars_on_mesh(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertNotIsInstance(view, ScrollableContainer)
            self.assertFalse(view.show_vertical_scrollbar)
            self.assertFalse(view.show_horizontal_scrollbar)

    # ---- Board label 5-cell limit (app-level) --------------------------

    async def test_board_label_five_cells_stays_centered_and_glyph_unmoved(
        self,
    ) -> None:
        """A long name truncates to <= 5 display cells on the board label,

        the label stays centered over the glyph's fixed grid anchor, and
        the glyph's own coordinate is completely unaffected by label
        length (spec: "label width never moves the glyph").
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            long_id = "!10ngname"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    long_id,
                    "A Very Long Descriptive Node Name",
                    None,
                    position=north_of_local(3),
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=long_id,
                    sender_long_name="A Very Long Descriptive Node Name",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            glyph = next(w for w in app.query(MeshNodeWidget) if w.node_id == long_id)
            label = next(w for w in app.query(MeshNodeLabelWidget) if w.node_id == long_id)

            label_text = str(label.render())
            self.assertLessEqual(cell_len(label_text), MESH_BOARD_LABEL_MAX_CELLS)
            # The label sits exactly one row above the glyph, horizontally
            # centered on the SAME column anchor -- never the glyph's own
            # coordinate (see MeshNodeLabelWidget/set_nodes).
            glyph_x, glyph_y = glyph_screen_position(glyph)
            label_x, label_y = glyph_screen_position(label)
            self.assertEqual(label_x, glyph_x)
            self.assertEqual(label_y, glyph_y - 1)

            # Selecting a neighbor recenters the whole board (glyph moves
            # with the translation) but the label stays centered on it.
            _mesh_select_node(app, you_id)
            await pilot.pause()
            glyph = next(w for w in app.query(MeshNodeWidget) if w.node_id == long_id)
            label = next(w for w in app.query(MeshNodeLabelWidget) if w.node_id == long_id)
            glyph_x, glyph_y = glyph_screen_position(glyph)
            label_x, label_y = glyph_screen_position(label)
            self.assertEqual(label_x, glyph_x)
            self.assertEqual(label_y, glyph_y - 1)

    # ---- Preserved visual contract -----------------------------------

    async def test_selected_node_uses_accent_and_wider_composite(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            palette = THEME_PALETTES[app._current_theme]
            you_widget = next(
                widget
                for widget in app.query(MeshNodeWidget)
                if widget.node_id == view.selected_node_id
            )
            rendered = you_widget.render()
            self.assertEqual(rendered.spans[0].style.foreground, Color.parse(palette.accent))
            self.assertEqual(int(you_widget.styles.width.value), MESH_SELECTED_GLYPH_WIDTH)

    async def test_stale_unselected_node_uses_dim_base(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            very_old = 1_700_000_000.0 - MESH_STALE_THRESHOLD_SECONDS * 5
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!stale001",
                    sender_long_name="StaleOne",
                    sender_short_name=None,
                    channel_index=0,
                    text="ancient",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=very_old,
                )
            )
            await pilot.press("3")
            await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_000.0)
            await pilot.pause()
            palette = THEME_PALETTES[app._current_theme]
            stale_widget = next(
                widget for widget in app.query(MeshNodeWidget) if widget.node_id == "!stale001"
            )
            rendered = stale_widget.render()
            self.assertEqual(
                rendered.spans[0].style.foreground, Color.parse(palette.dim_base)
            )

    async def test_no_selection_background_rectangle(self) -> None:
        self.assertNotIn(".mesh-node:focus", MeshtasticPassApp.CSS)
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            for widget in app.query(MeshNodeWidget):
                self.assertEqual(widget.styles.background.a, 0)

    # ---- Zero new radio traffic (spec section 26) ---------------------

    async def test_mesh_refresh_navigation_generate_no_radio_traffic(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            for key in ("up", "down", "left", "right", "up", "down"):
                await pilot.press(key)
                await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()
            self.assertEqual(app.radio.sent_messages, ())

    async def test_mesh_never_calls_send_text_or_traceroute_paths(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for method_name in ("send_text", "sendData", "sendText", "sendPosition"):
                if hasattr(app.radio, method_name):
                    setattr(
                        app.radio,
                        method_name,
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError(f"MESH must never call {method_name}")
                        ),
                    )
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            for key in ("up", "down", "left", "right"):
                await pilot.press(key)
                await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()


class MeshNodeDbFirstLiveUpdateTests(unittest.IsolatedAsyncioTestCase):
    """MESH's board must consume the same fresh NodeDB state driving

    [3] MESH (N) -- count and board are computed from ONE shared
    working-set snapshot per refresh (see app._refresh_mesh), and
    admission is NodeDB-first (see build_mesh_working_set): a real node
    the radio has passively heard from is a candidate whether or not it
    has ever sent a CHAT message.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    def _mounted_ids(self, app: MeshtasticPassApp) -> set[str]:
        return {w.node_id for w in app.query(MeshNodeWidget)}

    async def test_count_and_board_agree_when_previously_unknown_nodes_go_active(
        self,
    ) -> None:
        """The exact reported mismatch: [3] MESH (N) must never change

        without the board changing too. Start with zero known remote
        nodes (count 0, empty board); mutate the underlying RadioService
        as real hardware would (two nodes newly heard); a normal refresh
        (no tab switch, no reconnect) must bring both the count AND the
        board into agreement, with no stale snapshot left mounted.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local_only = (NodeMetadata(app.radio.info.node_id, is_local=True),)
            app.radio.get_known_nodes = lambda: local_only
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertIn("MESH (0)", str(app.query_one("#tab-bar").render()))
            self.assertEqual(
                {state.node.node_id for state in view.working_set if not state.node.is_local},
                set(),
            )

            fresh_nodes = local_only + (
                NodeMetadata("!fresh001", "Fresh One", last_heard=now - 5),
                NodeMetadata("!fresh002", "Fresh Two", last_heard=now - 5),
            )
            app.radio.get_known_nodes = lambda: fresh_nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn("MESH (2)", tab_bar)
            remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertEqual(remote_ids, {"!fresh001", "!fresh002"})
            self.assertEqual(self._mounted_ids(app), {n.node_id for n in fresh_nodes})
            palette = THEME_PALETTES[app._current_theme]
            for node_id in ("!fresh001", "!fresh002"):
                widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
                self.assertEqual(
                    widget.render().spans[0].style.foreground, Color.parse(palette.base)
                )

    async def test_new_nodedb_node_appears_without_any_chat_history(self) -> None:
        """A brand-new node the radio starts hearing mid-session appears

        on refresh with no TEXT_MESSAGE_APP required -- NodeDB-first
        admission, not CHAT-history admission (see
        build_mesh_working_set).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            stale_last_heard = now - ACTIVE_WINDOW_SECONDS - 100
            a = NodeMetadata("!aaaaaaaa", "Node A", last_heard=stale_last_heard)
            b = NodeMetadata("!bbbbbbbb", "Node B", last_heard=stale_last_heard)
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            app.radio.get_known_nodes = lambda: (local, a, b)
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertNotIn(
                "!cccccccc",
                {state.node.node_id for state in view.working_set},
            )

            c = NodeMetadata("!cccccccc", "Node C", last_heard=now - 5)
            app.radio.get_known_nodes = lambda: (local, a, b, c)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertIn("!cccccccc", remote_ids)
            self.assertIn("!cccccccc", self._mounted_ids(app))
            self.assertIn("MESH (1)", str(app.query_one("#tab-bar").render()))
            c_state = next(
                state for state in view.working_set if state.node.node_id == "!cccccccc"
            )
            self.assertFalse(c_state.is_client)

    async def test_live_hops_away_change_updates_relay_stage_count(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            far = NodeMetadata("!far00001", "Far Node", hops_away=1, last_heard=now - 5)
            app.radio.get_known_nodes = lambda nodes=(local, far): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(
                len(
                    [
                        s
                        for s in view.relay_stages
                        if s.node_id.startswith("relay:!far00001:")
                    ]
                ),
                1,
            )

            far_now_3_hops = NodeMetadata(
                "!far00001", "Far Node", hops_away=3, last_heard=now - 4
            )
            app.radio.get_known_nodes = lambda nodes=(local, far_now_3_hops): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(
                len(
                    [
                        s
                        for s in view.relay_stages
                        if s.node_id.startswith("relay:!far00001:")
                    ]
                ),
                3,
            )

    async def test_live_position_change_updates_placement(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            position_a = GeoPosition(10.0, 10.0)
            moving = NodeMetadata(
                "!move0001", "Mover", hops_away=1, last_heard=now - 5, position=position_a
            )
            app.radio.get_known_nodes = lambda nodes=(local, moving): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            position_before = view.base_positions["!move0001"]

            position_b = GeoPosition(-40.0, 130.0)
            moved = NodeMetadata(
                "!move0001", "Mover", hops_away=1, last_heard=now - 4, position=position_b
            )
            app.radio.get_known_nodes = lambda nodes=(local, moved): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            position_after = view.base_positions["!move0001"]
            self.assertNotEqual(position_before, position_after)

    async def test_live_last_heard_transition_updates_color_and_count(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            stale_last_heard = now - ACTIVE_WINDOW_SECONDS - 100
            crosser = NodeMetadata("!cross001", "Crosser", last_heard=stale_last_heard)
            app.radio.get_known_nodes = lambda nodes=(local, crosser): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            palette = THEME_PALETTES[app._current_theme]
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!cross001")
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            self.assertIn("MESH (0)", str(app.query_one("#tab-bar").render()))

            fresh_crosser = NodeMetadata("!cross001", "Crosser", last_heard=now - 5)
            app.radio.get_known_nodes = lambda nodes=(local, fresh_crosser): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!cross001")
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.base)
            )
            self.assertIn("MESH (1)", str(app.query_one("#tab-bar").render()))


class MeshLastUpdateStatusLineTests(unittest.IsolatedAsyncioTestCase):
    """The shared #mesh-connection-status widget's fallback priority:

    connection status text first, then "LAST UPDATE <age>" once ONLINE
    but stale, else nothing -- see app._update_mesh_status_line.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _mesh_status_text(self, app: MeshtasticPassApp) -> str:
        from textual.widgets import Static

        return str(app.query_one("#mesh-connection-status", Static).render())

    async def test_stale_board_shows_last_update_age(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            stale = NodeMetadata(
                "!stale001", "Stale", last_heard=now - ACTIVE_WINDOW_SECONDS - 3 * 60
            )
            app.radio.get_known_nodes = lambda nodes=(local, stale): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            text = await self._mesh_status_text(app)
            self.assertIn("LAST UPDATE", text)
            self.assertIn("8m", text)

    async def test_fresh_board_hides_last_update_indicator(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            fresh = NodeMetadata("!fresh999", "Fresh", last_heard=now - 5)
            app.radio.get_known_nodes = lambda nodes=(local, fresh): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            text = await self._mesh_status_text(app)
            self.assertNotIn("LAST UPDATE", text)
            self.assertEqual(text, "")
            self.assertFalse(app.query_one("#mesh-connection-status").display)

    async def test_connecting_status_overrides_stale_last_update(self) -> None:
        """While CONNECTING/RECONNECTING, the shared connection-status

        text always wins over "LAST UPDATE" -- see
        _update_mesh_status_line's priority order.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            stale = NodeMetadata(
                "!stale002", "Stale", last_heard=now - ACTIVE_WINDOW_SECONDS - 100
            )
            app.radio.get_known_nodes = lambda nodes=(local, stale): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            text = await self._mesh_status_text(app)
            self.assertIn("LAST UPDATE", text)

            app._show_connection(RadioState.OFFLINE, message="lost")
            await pilot.pause()
            text = await self._mesh_status_text(app)
            self.assertIn(f"STATUS {ANIMATED_STATUS[RadioState.OFFLINE]}", text)
            self.assertNotIn("LAST UPDATE", text)


class ThinScrollBarRenderTests(unittest.TestCase):
    def test_vertical_thin_render_uses_the_thin_glyph(self) -> None:
        from rich.color import Color as RichColor

        from app import CHAT_SCROLLBAR_THUMB_GLYPH

        rendered = ThinScrollBarRender.render_bar(
            size=10,
            virtual_size=30,
            window_size=10,
            position=5,
            thickness=1,
            vertical=True,
            back_color=RichColor.parse(THEME_PALETTES["white"].dim_base),
            bar_color=RichColor.parse("#39ff14"),
        )
        non_newline = [
            segment for segment in rendered.segments if segment.text != "\n"
        ]
        self.assertTrue(
            all(segment.text == CHAT_SCROLLBAR_THUMB_GLYPH for segment in non_newline)
        )
