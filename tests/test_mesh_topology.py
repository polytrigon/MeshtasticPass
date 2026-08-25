"""Pure and headless tests for MESH: pure grid geometry plus the fixed

three-node (YOU/ALICE/BOB) interaction fixture that is the current,
deliberately minimal MESH board. See app.py's module comment above
MESH_GRID_ROWS for what this fixture intentionally does not yet do
(ranking, staleness, favorites, real relay/message classification,
dynamic geography, scrolling).
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
    MESH_FIXTURE_CONNECTORS,
    MESH_FIXTURE_CONTEXT_LABELS,
    MESH_FIXTURE_NODES,
    MESH_GRID_CENTER_COLUMN,
    MESH_GRID_CENTER_ROW,
    MESH_GRID_COLUMNS,
    MESH_GRID_ROWS,
    MeshCanvas,
    MeshFixtureNode,
    MeshNodeLabelWidget,
    MeshNodeWidget,
    MeshTopologyView,
    MeshtasticPassApp,
    ThinScrollBarRender,
    _mesh_fixture_directional_target,
    _mesh_grid_pixel,
    _mesh_translated_positions,
    _render_mesh_canvas,
)
from app_settings import AppSettings
from geo import GeoPosition
from mesh_topology import (
    HORIZONTAL_GAP,
    NODE_HEIGHT,
    NODE_WIDTH,
    build_topology,
    compact_node_label,
    directional_target,
    route_connector,
)
import mesh_topology as mesh_topology_module
from radio_service import NodeMetadata
from simulated_radio_service import SimulatedRadioService
from theme_palette import THEME_PALETTES


LOCAL_GEO = GeoPosition(0.0, 0.0)
_MILES_PER_DEGREE_AT_EQUATOR = 69.0

YOU_ID = "!you"
ALICE_ID = "!alice"
BOB_ID = "!bob"


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


def expected_widget_offset(row: int, column: int, label: str) -> tuple[int, int]:
    """The widget-box offset that centers `label` over the grid point,

    mirroring MeshTopologyView.render_fixture's own placement formula.
    """
    grid_x, grid_y = _mesh_grid_pixel(row, column)
    width = max(1, cell_len(label))
    return (grid_x - width // 2, grid_y - 1)


def glyph_screen_position(widget) -> tuple[int, int]:
    """The glyph widget's own on-screen (x, y).

    MeshNodeWidget (the glyph) is always a 1x1 widget offset directly to
    its grid coordinate -- independent of the separately positioned
    MeshNodeLabelWidget, so this is simply its own offset.
    """
    return offset_xy(widget)


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


class MeshFixtureModelTests(unittest.TestCase):
    """Pure tests of the fixed 8x21 grid and its three-node fixture, with no

    running app: grid dimensions, logical positions, and the whole-mesh
    translation/recentering math from the exact worked examples in spec.
    """

    def test_grid_is_exactly_eight_rows_by_twenty_one_columns(self) -> None:
        self.assertEqual(MESH_GRID_ROWS, 8)
        self.assertEqual(MESH_GRID_COLUMNS, 21)

    def test_center_position_is_row_five_column_eleven(self) -> None:
        self.assertEqual(MESH_GRID_CENTER_ROW, 5)
        self.assertEqual(MESH_GRID_CENTER_COLUMN, 11)

    def test_you_fixed_logical_position_is_row_five_column_eleven(self) -> None:
        you = next(node for node in MESH_FIXTURE_NODES if node.node_id == YOU_ID)
        self.assertEqual((you.row, you.column), (5, 11))
        self.assertTrue(you.is_local)

    def test_alice_fixed_logical_position_is_row_four_column_ten(self) -> None:
        alice = next(node for node in MESH_FIXTURE_NODES if node.node_id == ALICE_ID)
        self.assertEqual((alice.row, alice.column), (4, 10))
        self.assertFalse(alice.is_local)

    def test_bob_is_two_columns_left_and_one_row_down_from_alice(self) -> None:
        alice = next(node for node in MESH_FIXTURE_NODES if node.node_id == ALICE_ID)
        bob = next(node for node in MESH_FIXTURE_NODES if node.node_id == BOB_ID)
        self.assertEqual(bob.row, alice.row + 1)
        self.assertEqual(bob.column, alice.column - 2)
        self.assertEqual((bob.row, bob.column), (5, 8))
        self.assertFalse(bob.is_local)

    def test_exactly_three_fixture_nodes_exist(self) -> None:
        self.assertEqual(len(MESH_FIXTURE_NODES), 3)

    def test_you_and_bob_use_solid_glyph_alice_uses_stroked_glyph(self) -> None:
        by_id = {node.node_id: node for node in MESH_FIXTURE_NODES}
        self.assertTrue(by_id[YOU_ID].solid)
        self.assertTrue(by_id[BOB_ID].solid)
        self.assertFalse(by_id[ALICE_ID].solid)

    def test_fixture_labels_are_you_alice_bob(self) -> None:
        by_id = {node.node_id: node for node in MESH_FIXTURE_NODES}
        self.assertEqual(by_id[YOU_ID].label, "YOU")
        self.assertEqual(by_id[ALICE_ID].label, "ALICE")
        self.assertEqual(by_id[BOB_ID].label, "Bob")

    def test_context_labels_are_you_alice_bob(self) -> None:
        self.assertEqual(MESH_FIXTURE_CONTEXT_LABELS[YOU_ID], "YOU")
        self.assertEqual(MESH_FIXTURE_CONTEXT_LABELS[ALICE_ID], "ALICE")
        self.assertEqual(MESH_FIXTURE_CONTEXT_LABELS[BOB_ID], "BOB")

    def test_you_selected_leaves_positions_untranslated(self) -> None:
        positions = _mesh_translated_positions(YOU_ID)
        self.assertEqual(positions[YOU_ID], (5, 11))
        self.assertEqual(positions[ALICE_ID], (4, 10))
        self.assertEqual(positions[BOB_ID], (5, 8))

    def test_alice_selected_recenters_alice_and_shifts_others_by_the_same_delta(
        self,
    ) -> None:
        """The exact worked example: ALICE -> (5,11), YOU -> (6,12)."""
        positions = _mesh_translated_positions(ALICE_ID)
        self.assertEqual(positions[ALICE_ID], (5, 11))
        self.assertEqual(positions[YOU_ID], (6, 12))
        self.assertEqual(positions[BOB_ID], (6, 9))

    def test_translation_is_a_pure_whole_mesh_shift(self) -> None:
        """Every node moves by the identical row/column delta -- relative

        geometry between all three nodes never changes, regardless of
        which node is selected.
        """
        for reference_id in (YOU_ID, ALICE_ID, BOB_ID):
            you_selected = _mesh_translated_positions(YOU_ID)
            other_selected = _mesh_translated_positions(reference_id)
            you_row, you_col = you_selected[YOU_ID]
            ref_row, ref_col = you_selected[reference_id]
            you_row2, you_col2 = other_selected[YOU_ID]
            ref_row2, ref_col2 = other_selected[reference_id]
            with self.subTest(selected=reference_id):
                self.assertEqual(
                    (you_row - ref_row, you_col - ref_col),
                    (you_row2 - ref_row2, you_col2 - ref_col2),
                )

    def test_directional_target_from_you_moving_left_is_bob(self) -> None:
        """BOB sits on YOU's own row (5,8 vs 5,11), directly left with zero

        vertical deviation, while ALICE is diagonal (4,10) -- the existing
        nearest-node rule (mesh_topology.directional_target) picks the more
        directionally-pure candidate over the merely-closer one, so LEFT
        from YOU reaches BOB. ALICE is reached via UP instead (see below).
        This is the current spatial rule applied faithfully to this
        fixture's geometry, not a hardcoded key map.
        """
        self.assertEqual(_mesh_fixture_directional_target(YOU_ID, "left"), BOB_ID)

    def test_directional_target_from_you_moving_up_is_alice(self) -> None:
        self.assertEqual(_mesh_fixture_directional_target(YOU_ID, "up"), ALICE_ID)

    def test_directional_target_from_alice_moving_left_is_bob(self) -> None:
        self.assertEqual(_mesh_fixture_directional_target(ALICE_ID, "left"), BOB_ID)

    def test_directional_target_from_alice_moving_down_is_you(self) -> None:
        self.assertEqual(_mesh_fixture_directional_target(ALICE_ID, "down"), YOU_ID)

    def test_directional_target_from_bob_moving_right_is_you(self) -> None:
        self.assertEqual(_mesh_fixture_directional_target(BOB_ID, "right"), YOU_ID)

    def test_directional_target_from_bob_moving_up_is_alice(self) -> None:
        self.assertEqual(_mesh_fixture_directional_target(BOB_ID, "up"), ALICE_ID)

    def test_directional_target_with_no_candidate_is_none(self) -> None:
        self.assertIsNone(_mesh_fixture_directional_target(YOU_ID, "down"))
        self.assertIsNone(_mesh_fixture_directional_target(BOB_ID, "left"))

    def test_background_grid_renders_a_dot_at_every_logical_position(self) -> None:
        """Every one of the 8x21 logical grid positions must correspond to

        exactly one visible background dot -- proven directly against the
        procedural canvas renderer, independent of any node overlay.
        """
        width = MESH_GRID_COLUMNS * DOT_GRID_SPACING_X
        height = MESH_GRID_ROWS * DOT_GRID_SPACING_Y
        rows = str(_render_mesh_canvas(width, height, (), "#222222")).split("\n")
        self.assertEqual(len(rows), height)
        for row in range(1, MESH_GRID_ROWS + 1):
            for column in range(1, MESH_GRID_COLUMNS + 1):
                x, y = _mesh_grid_pixel(row, column)
                with self.subTest(row=row, column=column):
                    self.assertEqual(rows[y][x], DOT_GRID_GLYPH)

    def test_background_dot_exists_at_you_grid_position(self) -> None:
        width = MESH_GRID_COLUMNS * DOT_GRID_SPACING_X
        height = MESH_GRID_ROWS * DOT_GRID_SPACING_Y
        rows = str(_render_mesh_canvas(width, height, (), "#222222")).split("\n")
        x, y = _mesh_grid_pixel(5, 11)
        self.assertEqual(rows[y][x], DOT_GRID_GLYPH)

    def test_background_dot_exists_at_alice_grid_position(self) -> None:
        width = MESH_GRID_COLUMNS * DOT_GRID_SPACING_X
        height = MESH_GRID_ROWS * DOT_GRID_SPACING_Y
        rows = str(_render_mesh_canvas(width, height, (), "#222222")).split("\n")
        x, y = _mesh_grid_pixel(4, 10)
        self.assertEqual(rows[y][x], DOT_GRID_GLYPH)

    def test_background_dot_exists_at_bob_grid_position(self) -> None:
        width = MESH_GRID_COLUMNS * DOT_GRID_SPACING_X
        height = MESH_GRID_ROWS * DOT_GRID_SPACING_Y
        rows = str(_render_mesh_canvas(width, height, (), "#222222")).split("\n")
        x, y = _mesh_grid_pixel(5, 8)
        self.assertEqual(rows[y][x], DOT_GRID_GLYPH)

    def test_connectors_are_you_alice_and_alice_bob_only(self) -> None:
        self.assertEqual(
            MESH_FIXTURE_CONNECTORS, (("!you", "!alice"), ("!alice", "!bob"))
        )

    def test_alice_bob_connector_route_moves_left_then_down(self) -> None:
        """2 columns left, 1 row down -- route_connector always draws its

        horizontal segment first (along ALICE's row) then its vertical
        segment second (along BOB's column), so the corner cell must sit
        at (bob_x, alice_y), every horizontal cell must share ALICE's row,
        and every vertical cell must share BOB's column.
        """
        alice_x, alice_y = _mesh_grid_pixel(4, 10)
        bob_x, bob_y = _mesh_grid_pixel(5, 8)
        self.assertLess(bob_x, alice_x)
        self.assertGreater(bob_y, alice_y)
        route = route_connector(alice_x, alice_y, bob_x, bob_y)
        self.assertTrue(route)

        corner_index = next(
            index for index, (x, y, _glyph) in enumerate(route) if (x, y) == (bob_x, alice_y)
        )
        for x, y, _glyph in route[:corner_index]:
            self.assertEqual(y, alice_y)
        for x, y, _glyph in route[corner_index:]:
            self.assertEqual(x, bob_x)
        last_x, last_y, _glyph = route[-1]
        self.assertEqual((last_x, last_y), (bob_x, bob_y - 1))


class MeshFixtureAppTests(unittest.IsolatedAsyncioTestCase):
    """Headless app tests of the fixed YOU+ALICE+BOB MESH board: default

    selection, ACCENT/BASE styling, labels, the recenter-on-select
    interaction, connectors, the bottom-left context label, horizontal
    centering, and the absence of any selection background/focus rectangle
    or scrollbars.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        # render_fixture defers its centering offset by one refresh cycle
        # the very first time this view is laid out (self.size is 0x0
        # before that); give it a chance to land before asserting on it.
        await pilot.pause()

    def _widget(self, app, node_id: str) -> MeshNodeWidget:
        return next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)

    def _label_widget(self, app, node_id: str) -> MeshNodeLabelWidget:
        return next(w for w in app.query(MeshNodeLabelWidget) if w.node_id == node_id)

    async def test_you_is_selected_by_default(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, YOU_ID)

    async def test_default_styling_you_solid_accent_alice_stroked_base_bob_solid_base(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            palette = THEME_PALETTES[app._current_theme]
            you, alice, bob = (
                self._widget(app, YOU_ID),
                self._widget(app, ALICE_ID),
                self._widget(app, BOB_ID),
            )
            you_rendered, alice_rendered, bob_rendered = (
                you.render(),
                alice.render(),
                bob.render(),
            )
            self.assertEqual(
                you_rendered.spans[0].style.foreground, Color.parse(palette.accent)
            )
            self.assertEqual(
                alice_rendered.spans[0].style.foreground, Color.parse(palette.base)
            )
            self.assertEqual(
                bob_rendered.spans[0].style.foreground, Color.parse(palette.base)
            )
            self.assertEqual(
                str(you_rendered).splitlines()[-1].strip(), CIRCLE_SOLID_LARGE
            )
            self.assertEqual(
                str(alice_rendered).splitlines()[-1].strip(), CIRCLE_STROKED_LARGE
            )
            self.assertEqual(
                str(bob_rendered).splitlines()[-1].strip(), CIRCLE_SOLID_LARGE
            )

    async def test_node_labels_render_you_alice_bob(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            you, alice, bob = (
                self._label_widget(app, YOU_ID),
                self._label_widget(app, ALICE_ID),
                self._label_widget(app, BOB_ID),
            )
            self.assertEqual(str(you.render()).strip(), "YOU")
            self.assertEqual(str(alice.render()).strip(), "ALICE")
            self.assertEqual(str(bob.render()).strip(), "Bob")

    async def test_you_glyph_stays_exactly_on_its_grid_coordinate_with_label_present(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            self.assertEqual(
                glyph_screen_position(self._widget(app, YOU_ID)),
                _mesh_grid_pixel(5, 11),
            )

    async def test_alice_glyph_stays_exactly_on_its_grid_coordinate_with_label_present(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            self.assertEqual(
                glyph_screen_position(self._widget(app, ALICE_ID)),
                _mesh_grid_pixel(4, 10),
            )

    async def test_bob_glyph_stays_exactly_on_its_grid_coordinate_with_label_present(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            self.assertEqual(
                glyph_screen_position(self._widget(app, BOB_ID)), _mesh_grid_pixel(5, 8)
            )

    async def test_node_glyph_stays_exactly_on_its_grid_point_regardless_of_label_width(
        self,
    ) -> None:
        """Label width must never move the glyph off its grid coordinate --

        YOU (3 cells), ALICE (5 cells), and Bob (3 cells) have different
        label widths, but every glyph still lands exactly on its own
        (grid_x, grid_y), and the label widget's own offset -- entirely
        separate from the glyph widget -- centers it over that same point.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            positions = {YOU_ID: (5, 11), ALICE_ID: (4, 10), BOB_ID: (5, 8)}
            for node_id, (row, column) in positions.items():
                glyph_widget = self._widget(app, node_id)
                label_widget = self._label_widget(app, node_id)
                with self.subTest(node=node_id):
                    self.assertEqual(
                        glyph_screen_position(glyph_widget),
                        _mesh_grid_pixel(row, column),
                    )
                    self.assertEqual(
                        offset_xy(label_widget),
                        expected_widget_offset(row, column, label_widget.fixture.label),
                    )

    async def test_labels_are_centered_over_their_fixed_glyph_coordinates(self) -> None:
        """Equal left/right margin between each label's own box and its

        glyph's screen column -- proof of centering independent of the
        offset formula under test elsewhere.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            for node_id in (YOU_ID, ALICE_ID, BOB_ID):
                # Both regions are in the same (absolute, on-screen)
                # coordinate space -- unlike glyph_screen_position(), which
                # is board-local, so they can be compared directly here.
                glyph_x = self._widget(app, node_id).region.x
                label_region = self._label_widget(app, node_id).region
                left_margin = glyph_x - label_region.x
                right_margin = (label_region.x + label_region.width) - glyph_x - 1
                with self.subTest(node=node_id):
                    self.assertEqual(left_margin, right_margin)

    async def test_wide_emoji_label_does_not_move_the_glyph(self) -> None:
        """A label far wider (in terminal cells) than its character count

        must still leave the glyph exactly on its grid coordinate -- the
        positioning formula uses cell_len(), not Python len(), so a label
        this wide is deliberately provocative: len() would undercount it.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            you = self._widget(app, YOU_ID)
            you_label = self._label_widget(app, YOU_ID)
            original_glyph_position = glyph_screen_position(you)

            emoji_label = "🐔🍕👨‍👩‍👧‍👦"
            self.assertNotEqual(cell_len(emoji_label), len(emoji_label))
            you.fixture = MeshFixtureNode(
                you.fixture.node_id,
                you.fixture.row,
                you.fixture.column,
                you.fixture.is_local,
                emoji_label,
                you.fixture.solid,
            )
            you_label.fixture = you.fixture

            view = app.query_one(MeshTopologyView)
            view.render_fixture(theme=app._current_theme)
            await pilot.pause()

            self.assertEqual(glyph_screen_position(you), original_glyph_position)
            self.assertEqual(
                offset_xy(you_label),
                expected_widget_offset(5, 11, emoji_label),
            )

    async def test_connector_endpoints_stay_attached_to_rendered_glyph_coordinates(
        self,
    ) -> None:
        """The live connector cells (drawn on the canvas) must terminate

        exactly one step short of each glyph's own actual rendered screen
        position -- ties the connector geometry to the real widget, not
        just to the pure _mesh_grid_pixel arithmetic both share.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            you_glyph = offset_xy(self._widget(app, YOU_ID))
            alice_glyph = offset_xy(self._widget(app, ALICE_ID))
            bob_glyph = offset_xy(self._widget(app, BOB_ID))

            canvas = app.query_one(MeshCanvas)
            connector_points = {(x, y) for x, y, _glyph, _color in canvas._signature[2]}

            for near, far in ((you_glyph, alice_glyph), (alice_glyph, bob_glyph)):
                # Recomputed from the real, live widget offsets (not the
                # static _mesh_grid_pixel formula both this and app.py
                # share) -- proves the drawn connector actually tracks
                # wherever the glyph widgets really ended up on screen.
                expected_route = {
                    (x, y) for x, y, _glyph in route_connector(*near, *far)
                }
                with self.subTest(near=near, far=far):
                    self.assertTrue(expected_route)
                    self.assertTrue(expected_route <= connector_points)

    async def test_no_selection_background_rectangle_in_css_or_at_runtime(self) -> None:
        self.assertNotIn(".mesh-node:focus", MeshtasticPassApp.CSS)
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            for node_id in (YOU_ID, ALICE_ID, BOB_ID):
                widget = self._widget(app, node_id)
                # Selected or not, no widget's background may be a filled
                # focus box -- it must stay fully transparent.
                with self.subTest(node=node_id):
                    self.assertEqual(widget.styles.background.a, 0)

    async def test_board_is_horizontally_centered_in_the_mesh_viewport(self) -> None:
        """No right-heavy extra padding: the board's left and right margins

        inside the MESH viewport must be equal, applied as a single
        board-level offset rather than by moving any node.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            left_margin = view.board.region.x - view.region.x
            right_margin = (view.region.x + view.region.width) - (
                view.board.region.x + view.board.region.width
            )
            self.assertEqual(left_margin, right_margin)

    async def test_left_from_you_selects_bob_and_recenters_the_whole_mesh(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            await pilot.press("left")
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, BOB_ID)

            you, alice, bob = (
                self._widget(app, YOU_ID),
                self._widget(app, ALICE_ID),
                self._widget(app, BOB_ID),
            )
            self.assertEqual(glyph_screen_position(bob), _mesh_grid_pixel(5, 11))
            self.assertEqual(glyph_screen_position(you), _mesh_grid_pixel(5, 14))
            self.assertEqual(glyph_screen_position(alice), _mesh_grid_pixel(4, 13))

    async def test_up_from_you_selects_alice_and_recenters_the_whole_mesh(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            await pilot.press("up")
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, ALICE_ID)

            you, alice, bob = (
                self._widget(app, YOU_ID),
                self._widget(app, ALICE_ID),
                self._widget(app, BOB_ID),
            )
            self.assertEqual(glyph_screen_position(alice), _mesh_grid_pixel(5, 11))
            self.assertEqual(glyph_screen_position(you), _mesh_grid_pixel(6, 12))
            self.assertEqual(glyph_screen_position(bob), _mesh_grid_pixel(6, 9))

    async def test_left_from_you_makes_bob_accent_you_base_alice_base(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            await pilot.press("left")
            await pilot.pause()

            palette = THEME_PALETTES[app._current_theme]
            you, alice, bob = (
                self._widget(app, YOU_ID),
                self._widget(app, ALICE_ID),
                self._widget(app, BOB_ID),
            )
            self.assertEqual(
                bob.render().spans[0].style.foreground, Color.parse(palette.accent)
            )
            self.assertEqual(
                you.render().spans[0].style.foreground, Color.parse(palette.base)
            )
            self.assertEqual(
                alice.render().spans[0].style.foreground, Color.parse(palette.base)
            )

    async def test_left_from_you_turns_alice_bob_connector_accent_you_alice_dim(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            palette = THEME_PALETTES[app._current_theme]

            canvas = app.query_one(MeshCanvas)
            before_colors = {color for *_pos, color in canvas._signature[2]}
            self.assertEqual(before_colors, {palette.dim_base})

            await pilot.press("left")
            await pilot.pause()
            after_colors = {color for *_pos, color in canvas._signature[2]}
            self.assertEqual(after_colors, {palette.dim_base, palette.accent})

    async def test_up_from_you_turns_you_alice_connector_accent(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            palette = THEME_PALETTES[app._current_theme]
            await pilot.press("up")
            await pilot.pause()
            canvas = app.query_one(MeshCanvas)
            colors = {color for *_pos, color in canvas._signature[2]}
            self.assertEqual(colors, {palette.dim_base, palette.accent})

    async def test_right_from_bob_reselects_you_and_restores_original_positions(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            you, alice, bob = (
                self._widget(app, YOU_ID),
                self._widget(app, ALICE_ID),
                self._widget(app, BOB_ID),
            )
            original = {
                YOU_ID: glyph_screen_position(you),
                ALICE_ID: glyph_screen_position(alice),
                BOB_ID: glyph_screen_position(bob),
            }

            await pilot.press("left")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, YOU_ID)
            self.assertEqual(glyph_screen_position(you), original[YOU_ID])
            self.assertEqual(glyph_screen_position(alice), original[ALICE_ID])
            self.assertEqual(glyph_screen_position(bob), original[BOB_ID])

    async def test_right_from_bob_returns_connectors_to_dim_base(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            await pilot.press("left")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()

            palette = THEME_PALETTES[app._current_theme]
            canvas = app.query_one(MeshCanvas)
            colors = {color for *_pos, color in canvas._signature[2]}
            self.assertEqual(colors, {palette.dim_base})

    async def test_context_status_line_updates_for_you_alice_bob(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(status, "YOU")

            await pilot.press("up")
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(status, "ALICE")

            await pilot.press("down")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(status, "BOB")

            await pilot.press("right")
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(status, "YOU")

    async def test_navigation_never_moves_the_board_or_creates_scrollbars(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertNotIsInstance(view, ScrollableContainer)
            self.assertFalse(view.show_vertical_scrollbar)
            self.assertFalse(view.show_horizontal_scrollbar)
            self.assertEqual(view.styles.overflow_x, "hidden")
            self.assertEqual(view.styles.overflow_y, "hidden")

            board_offset_before = view.board.styles.offset
            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(view.board.styles.offset, board_offset_before)
            self.assertFalse(view.show_vertical_scrollbar)
            self.assertFalse(view.show_horizontal_scrollbar)

    async def test_arrow_with_no_candidate_is_a_noop(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, YOU_ID)

    async def test_bob_is_reachable_via_spatial_arrow_navigation_from_you(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, BOB_ID)

    async def test_grid_dots_nodes_and_connectors_fit_inside_the_uconsole_viewport(
        self,
    ) -> None:
        """At the actual small/uConsole-like viewport (90x28, the size used

        by every other MESH test in this module and the stated supported
        terminal size), the fixed 8x21 board must never clip: all 168
        background dots, all three node glyphs, and both connectors must
        render entirely inside the visible MESH region -- MESH has no
        scrolling to fall back on if the board is too big for the viewport.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)

            view = app.query_one(MeshTopologyView)
            viewport = view.region
            board = view.board
            canvas = app.query_one(MeshCanvas)

            # The board container and its background canvas (dots plus
            # connectors) must both render fully inside the visible region.
            self.assertTrue(viewport.contains_region(board.region))
            self.assertTrue(viewport.contains_region(canvas.region))

            # All 168 (8x21) logical grid-dot positions, individually.
            dot_count = 0
            for row in range(1, MESH_GRID_ROWS + 1):
                for column in range(1, MESH_GRID_COLUMNS + 1):
                    x, y = _mesh_grid_pixel(row, column)
                    screen_x = board.region.x + x
                    screen_y = board.region.y + y
                    with self.subTest(row=row, column=column):
                        self.assertTrue(viewport.contains_point((screen_x, screen_y)))
                    dot_count += 1
            self.assertEqual(dot_count, 8 * 21)

            # YOU, ALICE, and BOB.
            for widget in app.query(MeshNodeWidget):
                with self.subTest(node=widget.node_id):
                    self.assertTrue(viewport.contains_region(widget.region))

            # Both connectors, cell by cell: YOU-ALICE and ALICE-BOB.
            fixed_pixel = {
                YOU_ID: _mesh_grid_pixel(5, 11),
                ALICE_ID: _mesh_grid_pixel(4, 10),
                BOB_ID: _mesh_grid_pixel(5, 8),
            }
            for near_id, far_id in MESH_FIXTURE_CONNECTORS:
                route = route_connector(*fixed_pixel[near_id], *fixed_pixel[far_id])
                self.assertTrue(route)
                for x, y, _glyph in route:
                    screen_x = board.region.x + x
                    screen_y = board.region.y + y
                    with self.subTest(near=near_id, far=far_id, cell=(x, y)):
                        self.assertTrue(viewport.contains_point((screen_x, screen_y)))


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
