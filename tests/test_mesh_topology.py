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
    CIRCLE_SOLID_LARGE,
    CIRCLE_STROKED_LARGE,
    DOT_GRID_GLYPH,
    DOT_GRID_SPACING_X,
    DOT_GRID_SPACING_Y,
    MESH_GRID_CENTER_COLUMN,
    MESH_GRID_CENTER_ROW,
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
from radio_service import NodeMetadata
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
        await pilot.press("4")
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
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accepted_send("hello from me")
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertEqual(remote_ids, set())

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
            await pilot.press("4")
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
            await pilot.press("4")
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

            # Alice no longer has any recorded interaction at all -- she
            # can no longer exist in a CLIENT-only working set.
            app._channel_states.clear()
            app.chat_store = None
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

    async def test_navigation_walks_through_relay_stages_and_back(self) -> None:
        """The exact "YOU -> RELAY -> RELAY -> ALICE and back" contract

        (spec section 16): a single 2-hop client leaves two genuinely
        interior cells (see the min_radius_by_id boost in _refresh_mesh),
        so sequential single-key presses visit both relay stages before
        the real node, and the same sequence in reverse returns to YOU --
        no wrapping, no scrolling, context text flips correctly at each
        stop.
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
            board_offset_before = view.board.styles.offset

            expected_forward = [
                stage.node_id
                for stage in sorted(view.relay_stages, key=lambda stage: stage.index)
            ] + [target_id]
            visited = []
            for _ in expected_forward:
                await pilot.press("up")
                await pilot.pause()
                visited.append(view.selected_node_id)
            self.assertEqual(visited, expected_forward)

            # No wrapping: one more "up" from the real node changes nothing.
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, target_id)
            status = str(app.query_one("#mesh-context-status").render())
            self.assertTrue(status.startswith("Target Node"))

            visited_back = []
            for _ in expected_forward:
                await pilot.press("down")
                await pilot.pause()
                visited_back.append(view.selected_node_id)
            self.assertEqual(visited_back, list(reversed(expected_forward[:-1])) + [you_id])
            self.assertEqual(str(app.query_one("#mesh-context-status").render()), "YOU")

            # Context reads exactly "RELAY" while a relay stage is selected.
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(str(app.query_one("#mesh-context-status").render()), "RELAY")

            self.assertEqual(view.board.styles.offset, board_offset_before)
            self.assertFalse(view.show_vertical_scrollbar)
            self.assertFalse(view.show_horizontal_scrollbar)

    async def test_relay_stage_selection_styling_and_context(self) -> None:
        """Stroked + DIM_BASE unselected, ACCENT + wider composite

        selected, never labeled, bottom context exactly "RELAY" (spec
        sections 6/8).
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

            _mesh_select_node(app, stage_id)
            await pilot.pause()
            relay_widget = next(w for w in app.query(MeshRelayWidget) if w.node_id == stage_id)
            self.assertEqual(
                relay_widget.render().spans[0].style.foreground, Color.parse(palette.accent)
            )
            self.assertEqual(int(relay_widget.styles.width.value), MESH_SELECTED_GLYPH_WIDTH)
            self.assertEqual(
                str(app.query_one("#mesh-context-status").render()), "RELAY"
            )

            _mesh_select_node(app, you_id)
            await pilot.pause()
            relay_widget = next(w for w in app.query(MeshRelayWidget) if w.node_id == stage_id)
            self.assertEqual(
                relay_widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            self.assertEqual(int(relay_widget.styles.width.value), 1)

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
            title = str(app.query_one("#mesh-title").render())
            self.assertEqual(title, "> MESH · ACTIVE 4")

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
            title = str(app.query_one("#mesh-title").render())
            self.assertEqual(title, "> MESH · ACTIVE 1")

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
            await pilot.press("4")
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
