"""Pure and headless tests for MESH: pure grid geometry plus the fixed

two-node (YOU/ALICE) interaction fixture that is the current, deliberately
minimal MESH board. See app.py's module comment above MESH_GRID_ROWS for
what this fixture intentionally does not yet do (ranking, staleness,
favorites, relay state, dynamic geography, scrolling).
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
    MESH_FIXTURE_NODES,
    MESH_GRID_CENTER_COLUMN,
    MESH_GRID_CENTER_ROW,
    MESH_GRID_COLUMNS,
    MESH_GRID_ROWS,
    MeshCanvas,
    MeshFixtureNode,
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
    """Pure tests of the fixed 9x21 grid and its two-node fixture, with no

    running app: grid dimensions, logical positions, and the whole-mesh
    translation/recentering math from the exact worked example in spec.
    """

    def test_grid_is_exactly_nine_rows_by_twenty_one_columns(self) -> None:
        self.assertEqual(MESH_GRID_ROWS, 9)
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

    def test_only_two_fixture_nodes_exist(self) -> None:
        self.assertEqual(len(MESH_FIXTURE_NODES), 2)

    def test_you_selected_leaves_positions_untranslated(self) -> None:
        positions = _mesh_translated_positions(YOU_ID)
        self.assertEqual(positions[YOU_ID], (5, 11))
        self.assertEqual(positions[ALICE_ID], (4, 10))

    def test_alice_selected_recenters_alice_and_shifts_you_by_the_same_delta(
        self,
    ) -> None:
        """The exact worked example: ALICE -> (5,11), YOU -> (6,12)."""
        positions = _mesh_translated_positions(ALICE_ID)
        self.assertEqual(positions[ALICE_ID], (5, 11))
        self.assertEqual(positions[YOU_ID], (6, 12))

    def test_translation_is_a_pure_whole_mesh_shift(self) -> None:
        """Every node moves by the identical row/column delta -- relative

        geometry between YOU and ALICE never changes, regardless of which
        node is selected.
        """
        you_selected = _mesh_translated_positions(YOU_ID)
        alice_selected = _mesh_translated_positions(ALICE_ID)
        you_row, you_col = you_selected[YOU_ID]
        alice_row, alice_col = you_selected[ALICE_ID]
        you_row2, you_col2 = alice_selected[YOU_ID]
        alice_row2, alice_col2 = alice_selected[ALICE_ID]
        self.assertEqual(
            (you_row - alice_row, you_col - alice_col),
            (you_row2 - alice_row2, you_col2 - alice_col2),
        )

    def test_directional_target_from_you_moving_left_is_alice(self) -> None:
        self.assertEqual(_mesh_fixture_directional_target(YOU_ID, "left"), ALICE_ID)

    def test_directional_target_from_alice_moving_right_is_you(self) -> None:
        self.assertEqual(_mesh_fixture_directional_target(ALICE_ID, "right"), YOU_ID)

    def test_directional_target_with_no_candidate_is_none(self) -> None:
        self.assertIsNone(_mesh_fixture_directional_target(YOU_ID, "down"))

    def test_background_grid_renders_a_dot_at_every_logical_position(self) -> None:
        """Every one of the 9x21 logical grid positions must correspond to

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

    def test_fixture_nodes_have_no_label_field(self) -> None:
        self.assertNotIn("label", MeshFixtureNode.__dataclass_fields__)


class MeshFixtureAppTests(unittest.IsolatedAsyncioTestCase):
    """Headless app tests of the fixed YOU+ALICE MESH board: default

    selection, ACCENT/BASE styling, the recenter-on-select interaction, the
    reverse interaction, the bottom-left context label, and the absence of
    any selection background/focus rectangle or scrollbars.
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

    async def test_you_is_selected_by_default(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, YOU_ID)

    async def test_default_styling_you_accent_alice_base(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            palette = THEME_PALETTES[app._current_theme]
            you = next(w for w in app.query(MeshNodeWidget) if w.node_id == YOU_ID)
            alice = next(w for w in app.query(MeshNodeWidget) if w.node_id == ALICE_ID)
            you_rendered = you.render()
            alice_rendered = alice.render()
            self.assertEqual(
                you_rendered.spans[0].style.foreground, Color.parse(palette.accent)
            )
            self.assertEqual(
                alice_rendered.spans[0].style.foreground, Color.parse(palette.base)
            )
            self.assertEqual(
                str(you_rendered).splitlines()[-1].strip(), CIRCLE_SOLID_LARGE
            )
            self.assertEqual(
                str(alice_rendered).splitlines()[-1].strip(), CIRCLE_STROKED_LARGE
            )

    async def test_node_widgets_render_glyph_only_no_labels(self) -> None:
        """The board renders no node-name text anywhere -- only the

        one-cell circle glyph, sized to exactly one cell wide and tall.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            for widget in app.query(MeshNodeWidget):
                expected_glyph = (
                    CIRCLE_SOLID_LARGE if widget.fixture.is_local else CIRCLE_STROKED_LARGE
                )
                rendered = widget.render()
                self.assertEqual(rendered.plain, expected_glyph)
                self.assertNotIn("YOU", rendered.plain)
                self.assertNotIn("ALICE", rendered.plain)
                self.assertEqual(int(widget.styles.width.value), 1)
                self.assertEqual(int(widget.styles.height.value), 1)

    async def test_node_glyph_centers_align_exactly_to_grid_dot_coordinates(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            you = next(w for w in app.query(MeshNodeWidget) if w.node_id == YOU_ID)
            alice = next(w for w in app.query(MeshNodeWidget) if w.node_id == ALICE_ID)
            self.assertEqual(offset_xy(you), _mesh_grid_pixel(5, 11))
            self.assertEqual(offset_xy(alice), _mesh_grid_pixel(4, 10))

    async def test_no_selection_background_rectangle_in_css_or_at_runtime(self) -> None:
        self.assertNotIn(".mesh-node:focus", MeshtasticPassApp.CSS)
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            you = next(w for w in app.query(MeshNodeWidget) if w.node_id == YOU_ID)
            # YOU is selected by default; its background must stay fully
            # transparent -- no filled focus box, selected or not.
            self.assertEqual(you.styles.background.a, 0)

    async def test_left_from_you_selects_alice_and_recenters_the_whole_mesh(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, ALICE_ID)

            you = next(w for w in app.query(MeshNodeWidget) if w.node_id == YOU_ID)
            alice = next(w for w in app.query(MeshNodeWidget) if w.node_id == ALICE_ID)

            self.assertEqual(offset_xy(alice), _mesh_grid_pixel(5, 11))
            self.assertEqual(offset_xy(you), _mesh_grid_pixel(6, 12))

    async def test_left_from_you_makes_alice_accent_and_you_base(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()

            palette = THEME_PALETTES[app._current_theme]
            you = next(w for w in app.query(MeshNodeWidget) if w.node_id == YOU_ID)
            alice = next(w for w in app.query(MeshNodeWidget) if w.node_id == ALICE_ID)
            self.assertEqual(
                alice.render().spans[0].style.foreground, Color.parse(palette.accent)
            )
            self.assertEqual(
                you.render().spans[0].style.foreground, Color.parse(palette.base)
            )

    async def test_left_from_you_turns_the_connector_accent(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            palette = THEME_PALETTES[app._current_theme]

            canvas = app.query_one(MeshCanvas)
            before_colors = {color for *_pos, color in canvas._signature[2]}
            self.assertEqual(before_colors, {palette.dim_base})

            await pilot.press("left")
            await pilot.pause()
            after_colors = {color for *_pos, color in canvas._signature[2]}
            self.assertEqual(after_colors, {palette.accent})

    async def test_right_from_alice_reselects_you_and_restores_original_positions(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            you = next(w for w in app.query(MeshNodeWidget) if w.node_id == YOU_ID)
            alice = next(w for w in app.query(MeshNodeWidget) if w.node_id == ALICE_ID)
            original_you_offset = offset_xy(you)
            original_alice_offset = offset_xy(alice)

            await pilot.press("left")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, YOU_ID)
            self.assertEqual(offset_xy(you), original_you_offset)
            self.assertEqual(offset_xy(alice), original_alice_offset)

    async def test_right_from_alice_returns_the_connector_to_dim_base(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()

            palette = THEME_PALETTES[app._current_theme]
            canvas = app.query_one(MeshCanvas)
            colors = {color for *_pos, color in canvas._signature[2]}
            self.assertEqual(colors, {palette.dim_base})

    async def test_context_status_line_updates_for_you_and_alice(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(status, "YOU")

            await pilot.press("left")
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(status, "ALICE")

            await pilot.press("right")
            await pilot.pause()
            status = str(app.query_one("#mesh-context-status").render())
            self.assertEqual(status, "YOU")

    async def test_navigation_never_moves_the_board_or_creates_scrollbars(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
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
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, YOU_ID)

    async def test_grid_dots_nodes_and_connector_fit_inside_the_uconsole_viewport(
        self,
    ) -> None:
        """At the actual small/uConsole-like viewport (90x28, the size used

        by every other MESH test in this module and the stated supported
        terminal size), the fixed 9x21 board must never clip: all 189
        background dots, both node glyphs, and the connector must render
        entirely inside the visible MESH region -- MESH has no scrolling
        to fall back on if the board is too big for the viewport.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            viewport = view.region
            board = view.board
            canvas = app.query_one(MeshCanvas)

            # The board container and its background canvas (dots plus
            # connector) must both render fully inside the visible region.
            self.assertTrue(viewport.contains_region(board.region))
            self.assertTrue(viewport.contains_region(canvas.region))

            # All 189 (9x21) logical grid-dot positions, individually.
            dot_count = 0
            for row in range(1, MESH_GRID_ROWS + 1):
                for column in range(1, MESH_GRID_COLUMNS + 1):
                    x, y = _mesh_grid_pixel(row, column)
                    screen_x = board.region.x + x
                    screen_y = board.region.y + y
                    with self.subTest(row=row, column=column):
                        self.assertTrue(viewport.contains_point((screen_x, screen_y)))
                    dot_count += 1
            self.assertEqual(dot_count, 9 * 21)

            # YOU and ALICE.
            for widget in app.query(MeshNodeWidget):
                with self.subTest(node=widget.node_id):
                    self.assertTrue(viewport.contains_region(widget.region))

            # The connector between YOU (5,11) and ALICE (4,10), cell by cell.
            you_x, you_y = _mesh_grid_pixel(5, 11)
            alice_x, alice_y = _mesh_grid_pixel(4, 10)
            connector = route_connector(you_x, you_y, alice_x, alice_y)
            self.assertTrue(connector)
            for x, y, _glyph in connector:
                screen_x = board.region.x + x
                screen_y = board.region.y + y
                with self.subTest(connector_cell=(x, y)):
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
