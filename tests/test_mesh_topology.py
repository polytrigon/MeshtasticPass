"""Pure and headless tests for the bounded, YOU-centered MESH board."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from rich.cells import cell_len
from textual.color import Color
from textual.events import MouseScrollDown

from app import (
    CHAT_SCROLLBAR_THUMB_GLYPH,
    CIRCLE_SOLID_LARGE,
    CIRCLE_STROKED_LARGE,
    CIRCLE_STROKED_SMALL,
    DOT_GRID_GLYPH,
    DOT_GRID_SPACING_X,
    DOT_GRID_SPACING_Y,
    ChatEntryWidget,
    MeshCanvas,
    MeshNodeWidget,
    MeshTopologyView,
    MeshtasticPassApp,
    ThinScrollBarRender,
    _render_mesh_canvas,
)
from app_settings import AppSettings
from geo import GeoPosition
from mesh_state import DEFAULT_MAX_REMOTE_NODES
from mesh_topology import (
    DEFAULT_MAX_GRID_RADIUS,
    HORIZONTAL_GAP,
    NODE_HEIGHT,
    NODE_WIDTH,
    VERTICAL_GAP,
    build_topology,
    compact_node_label,
    directional_target,
    route_connector,
)
import mesh_topology as mesh_topology_module
from radio_service import NodeMetadata, RadioState
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService
from theme_palette import THEME_PALETTES
from viewport_menu import ViewportMenu


LOCAL_GEO = GeoPosition(0.0, 0.0)
_MILES_PER_DEGREE_AT_EQUATOR = 69.0


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


class MeshTopologyAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )

    async def test_tab_renders_bounded_working_set_and_shared_local_menu(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            self.assertIn("[4] MESH", str(app.query_one("#tab-bar").render()))
            nodes = list(app.query(MeshNodeWidget))
            self.assertEqual(len(nodes), 8)
            self.assertTrue(isinstance(app.focused, MeshNodeWidget))
            self.assertTrue(app.focused.node.is_local)
            self.assertEqual(radio.sent_messages, ())

            await pilot.press("enter")
            await pilot.pause()
            menu = app.query_one("#node-context-menu", ViewportMenu)
            labels = [item.label for item in menu.items]
            self.assertIn("Simulated Node", labels)
            self.assertNotIn("FAVORITE", labels)
            self.assertNotIn("UNFAVORITE", labels)
            await pilot.press("escape")

            radio.get_known_nodes = lambda: (
                NodeMetadata(
                    radio.info.node_id,
                    radio.info.long_name,
                    radio.info.short_name,
                    0,
                    1_000.0,
                    True,
                ),
            )
            app._refresh_mesh(wall_now=1_000.0)
            await pilot.pause()
            only_node = list(app.query(MeshNodeWidget))
            self.assertEqual(len(only_node), 1)
            self.assertTrue(only_node[0].node.is_local)

    async def test_working_set_is_bounded_and_never_shows_all_historical_nodes(
        self,
    ) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            many = [
                NodeMetadata(
                    radio.info.node_id,
                    radio.info.long_name,
                    radio.info.short_name,
                    0,
                    1_000.0,
                    True,
                )
            ]
            for index in range(25):
                many.append(
                    NodeMetadata(
                        f"!n{index:04x}", f"Node{index}", None, None, 1_000.0 - index
                    )
                )
            radio.get_known_nodes = lambda: tuple(many)
            await pilot.press("4")
            await pilot.pause()
            await pilot.pause()
            widgets = list(app.query(MeshNodeWidget))
            self.assertEqual(len(widgets), DEFAULT_MAX_REMOTE_NODES + 1)

    async def test_favorite_is_preserved_in_working_set_despite_being_oldest(
        self,
    ) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            many = [
                NodeMetadata(
                    radio.info.node_id,
                    radio.info.long_name,
                    radio.info.short_name,
                    0,
                    1_000.0,
                    True,
                )
            ]
            for index in range(12):
                many.append(
                    NodeMetadata(
                        f"!n{index:04x}",
                        f"Node{index}",
                        None,
                        None,
                        1_000.0 - (index + 1) * 100_000,
                    )
                )
            oldest_id = "!n000b"
            self.settings.set_favorite(oldest_id, True)
            radio.get_known_nodes = lambda: tuple(many)
            await pilot.press("4")
            await pilot.pause()
            await pilot.pause()
            ids = {widget.node.node_id for widget in app.query(MeshNodeWidget)}
            self.assertIn(oldest_id, ids)

    async def test_arrow_selection_context_favorite_and_disconnected_state(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            local = app.focused
            self.assertIsInstance(local, MeshNodeWidget)
            assert isinstance(local, MeshNodeWidget)
            await pilot.press("up")
            self.assertIsInstance(app.focused, MeshNodeWidget)
            self.assertIsNot(app.focused, local)
            remote = app.focused
            assert isinstance(remote, MeshNodeWidget)
            self.assertTrue(await pilot.click(local))
            self.assertIs(app.focused, local)
            await pilot.press("up")
            remote = app.focused
            assert isinstance(remote, MeshNodeWidget)

            await pilot.press("enter")
            await pilot.pause()
            menu = app.query_one("#node-context-menu", ViewportMenu)
            labels = [item.label for item in menu.items]
            self.assertIn("FAVORITE", labels)
            self.assertTrue(any("HOP" in label for label in labels))
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(self.settings.is_favorite(remote.node.node_id))

            app._show_connection(RadioState.OFFLINE)
            await pilot.pause()
            self.assertEqual(len(app.query(MeshNodeWidget)), 0)
            self.assertEqual(
                str(app.query_one("#mesh-status").render()),
                "NO MESH DATA — RADIO DISCONNECTED",
            )

    async def test_you_solid_base_and_accent_when_selected(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            you = app.focused
            assert isinstance(you, MeshNodeWidget)
            self.assertTrue(you.node.is_local)
            palette = THEME_PALETTES["white"]
            rendered = you.render()
            self.assertEqual(rendered.spans[0].style.foreground, Color.parse(palette.accent))
            self.assertEqual(str(rendered).splitlines()[-1].strip(), CIRCLE_SOLID_LARGE)

            # Move away, then back, to see YOU in its unselected state.
            await pilot.press("up")
            await pilot.pause()
            you.refresh_visual(selected=False, theme="white")
            rendered = you.render()
            self.assertEqual(rendered.spans[0].style.foreground, Color.parse(palette.base))
            self.assertEqual(str(rendered).splitlines()[-1].strip(), CIRCLE_SOLID_LARGE)

    async def test_recency_and_favorite_styling_states(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await pilot.press("4")
            await pilot.pause()
            await pilot.pause()
            alice = next(
                widget
                for widget in app.query(MeshNodeWidget)
                if widget.node.node_id == "!a11ce001"
            )
            self.assertEqual(alice.display_node.recency_bucket, "recent")
            palette = THEME_PALETTES["white"]

            # Recent, not favorited: BASE, large stroked circle.
            rendered = alice.render()
            self.assertEqual(rendered.spans[0].style.foreground, Color.parse(palette.base))
            self.assertEqual(str(rendered).splitlines()[-1].strip(), CIRCLE_STROKED_LARGE)

            # Recent and favorited: FAVORITE_ACCENT, distinct from selection ACCENT.
            self.settings.set_favorite(alice.node.node_id, True)
            app._refresh_mesh()
            await pilot.pause()
            alice = next(
                widget
                for widget in app.query(MeshNodeWidget)
                if widget.node.node_id == "!a11ce001"
            )
            rendered = alice.render()
            self.assertEqual(
                rendered.spans[0].style.foreground, Color.parse(palette.favorite_accent)
            )
            self.assertNotEqual(palette.favorite_accent, palette.accent)

            # Force a >48h very-stale reading: name only, no circle line.
            far_future = SIMULATED_MESSAGES[0].radio_rx_at + 3 * 24 * 60 * 60
            alice.refresh_visual(selected=False, theme="white")
            from mesh_state import MeshDisplayNode

            very_stale = MeshDisplayNode(
                node=alice.node,
                last_message_at=SIMULATED_MESSAGES[0].radio_rx_at,
                reference_activity=SIMULATED_MESSAGES[0].radio_rx_at,
                favorite=True,
                relationship_kind="direct",
                recency_bucket="very_stale",
            )
            alice.display_node = very_stale
            alice.refresh_visual(selected=False, theme="white")
            rendered = alice.render()
            # Label plus a lone trailing newline: no second-line glyph at all.
            self.assertEqual(rendered.plain, "Alice Trail\n")
            self.assertEqual(
                rendered.spans[0].style.foreground, Color.parse(palette.dim_base)
            )

            # Selection always overrides Favorite/staleness, and always shows
            # a circle even for an otherwise glyph-less very-stale node.
            alice.refresh_visual(selected=True, theme="white")
            rendered = alice.render()
            self.assertEqual(
                rendered.spans[0].style.foreground, Color.parse(palette.accent)
            )
            self.assertEqual(str(rendered).splitlines()[-1].strip(), CIRCLE_STROKED_LARGE)

    async def test_refresh_preserves_selected_remote_node_metadata_change(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            selected = app.focused
            self.assertIsInstance(selected, MeshNodeWidget)
            assert isinstance(selected, MeshNodeWidget)
            selected_id = selected.node.node_id
            self.assertFalse(selected.node.is_local)

            base_nodes = radio.get_known_nodes()
            changed_nodes = tuple(
                NodeMetadata(
                    node.node_id,
                    "Renamed Node" if node.node_id == selected_id else node.long_name,
                    node.short_name,
                    node.hops_away,
                    node.last_heard,
                    node.is_local,
                )
                for node in base_nodes
            )
            radio.get_known_nodes = lambda: changed_nodes
            app._refresh_mesh(wall_now=1_000.0)
            await pilot.pause()

            self.assertIsInstance(app.focused, MeshNodeWidget)
            assert isinstance(app.focused, MeshNodeWidget)
            self.assertEqual(app.focused.node.node_id, selected_id)
            self.assertEqual(app.focused.node.long_name, "Renamed Node")

    async def test_repeated_refresh_with_unchanged_data_keeps_same_positions(
        self,
    ) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            original_positions = {
                item.node.node_id: (item.x, item.y) for item in view.layout_model.nodes
            }
            app._refresh_mesh(wall_now=2_000.0)
            await pilot.pause()
            new_positions = {
                item.node.node_id: (item.x, item.y) for item in view.layout_model.nodes
            }
            self.assertEqual(original_positions, new_positions)

    async def test_directional_arrows_navigate_and_no_candidate_is_noop(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            local = app.focused
            assert isinstance(local, MeshNodeWidget)
            local_x, local_y = local.positioned.x, local.positioned.y

            await pilot.press("right")
            await pilot.pause()
            self.assertIsInstance(app.focused, MeshNodeWidget)
            assert isinstance(app.focused, MeshNodeWidget)
            self.assertGreater(app.focused.positioned.x, local_x)

            await pilot.press("left")
            await pilot.pause()
            self.assertIs(app.focused, local)

            await pilot.press("down")
            await pilot.pause()
            self.assertIsInstance(app.focused, MeshNodeWidget)
            assert isinstance(app.focused, MeshNodeWidget)
            self.assertGreater(app.focused.positioned.y, local_y)

            # Move to the topmost node, where "up" has no candidate.
            top = min(view.layout_model.nodes, key=lambda item: item.y)
            view.select_node(top.node.node_id)
            await pilot.pause()
            focused_before = app.focused
            await pilot.press("up")
            await pilot.pause()
            self.assertIs(app.focused, focused_before)

    async def test_mouse_wheel_does_not_pan_mesh(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(42, 15)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            board_offset_before = view.board.styles.offset
            # A plain Container has no functional scroll/overflow behavior,
            # so a wheel event is simply a no-op -- the board never moves.
            view._on_mouse_scroll_down(
                MouseScrollDown(view, 1, 1, 0, 1, 0, False, False, False)
            )
            await pilot.pause()
            self.assertEqual(view.board.styles.offset, board_offset_before)

    async def test_no_scrollbars_are_present_on_mesh(self) -> None:
        from textual.containers import ScrollableContainer

        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(42, 15)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertNotIsInstance(view, ScrollableContainer)
            self.assertFalse(view.show_vertical_scrollbar)
            self.assertFalse(view.show_horizontal_scrollbar)
            self.assertEqual(view.styles.overflow_x, "hidden")
            self.assertEqual(view.styles.overflow_y, "hidden")

    async def test_board_size_is_bounded_regardless_of_working_set(self) -> None:
        """The grid radius is fixed, so board size never grows past a fixed

        bound even with the maximum working set and no scrolling exists to
        reach anything beyond it -- see MeshTopologyView's docstring for why
        radius is fixed rather than adapted from live viewport size.
        """
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            cell_width = NODE_WIDTH + HORIZONTAL_GAP
            cell_height = NODE_HEIGHT + VERTICAL_GAP
            max_span = 2 * DEFAULT_MAX_GRID_RADIUS + 3  # radius + collision slack
            self.assertLessEqual(view.layout_model.width, cell_width * max_span)
            self.assertLessEqual(view.layout_model.height, cell_height * max_span)

    async def test_you_to_node_connectors_are_you_origin_only(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            local_item = next(
                item for item in view.layout_model.nodes if item.node.is_local
            )
            local_center = (
                local_item.x + NODE_WIDTH // 2,
                local_item.y + NODE_HEIGHT // 2,
            )
            canvas = app.query_one(MeshCanvas)
            signature = canvas._signature
            assert signature is not None
            connectors = signature[2]
            self.assertTrue(connectors)
            # Every connector must trace back to a straight/elbow path that
            # actually originates or ends at YOU's center -- never a
            # fabricated node-to-node edge.
            for item in view.layout_model.nodes:
                if item.node.is_local:
                    continue
                node_center = (item.x + NODE_WIDTH // 2, item.y + NODE_HEIGHT // 2)
                expected = route_connector(*local_center, *node_center)
                if not expected:
                    continue
                for x, y, glyph in expected:
                    self.assertTrue(
                        any(cx == x and cy == y and cg == glyph for cx, cy, cg, _c in connectors)
                    )

    async def test_selected_connector_uses_accent_others_use_dim_base(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            await pilot.pause()
            canvas = app.query_one(MeshCanvas)
            signature = canvas._signature
            assert signature is not None
            connectors = signature[2]
            palette = THEME_PALETTES["white"]
            colors = {color for _x, _y, _glyph, color in connectors}
            self.assertIn(palette.accent, colors)
            self.assertTrue(colors - {palette.accent} <= {palette.dim_base})

    async def test_context_status_line_you_and_remote(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await pilot.press("4")
            await pilot.pause()
            await pilot.pause()
            you_status = str(app.query_one("#mesh-context-status").render())
            self.assertTrue(you_status.startswith("YOU · CONNECTED TO "))
            self.assertNotIn("--", you_status)

            alice = next(
                widget
                for widget in app.query(MeshNodeWidget)
                if widget.node.node_id == "!a11ce001"
            )
            view = app.query_one(MeshTopologyView)
            view.select_node(alice.node.node_id)
            await pilot.pause()
            await pilot.pause()
            remote_status = str(app.query_one("#mesh-context-status").render())
            self.assertIn("Alice Trail", remote_status)
            self.assertIn(" · ", remote_status)
            self.assertNotIn(" - ", remote_status)
            self.assertIn("HOP", remote_status)
            self.assertIn("LAST MESSAGE", remote_status)

    async def test_emoji_node_labels_render_without_corrupting_board_geometry(
        self,
    ) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            base_nodes = radio.get_known_nodes()
            emoji_labels = (
                "🐔chicken_node_long",
                "🍕pizza_node_long_name",
                "👨‍👩‍👧‍👦family_node",
                "🇺🇸🇬🇧flags_node",
            )
            emoji_nodes = tuple(
                NodeMetadata(
                    node.node_id,
                    emoji_labels[index % len(emoji_labels)]
                    if not node.is_local
                    else node.long_name,
                    node.short_name,
                    node.hops_away,
                    node.last_heard,
                    node.is_local,
                )
                for index, node in enumerate(base_nodes)
            )
            radio.get_known_nodes = lambda: emoji_nodes
            await pilot.press("4")
            await pilot.pause()

            widgets = list(app.query(MeshNodeWidget))
            self.assertEqual(len(widgets), len(emoji_nodes))
            for widget in widgets:
                label_line = str(widget.render()).splitlines()[0]
                self.assertLessEqual(cell_len(label_line), NODE_WIDTH)


class ThinScrollBarRenderTests(unittest.TestCase):
    def test_vertical_thin_render_uses_the_thin_glyph(self) -> None:
        from rich.color import Color as RichColor

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
