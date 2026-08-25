"""Pure and headless tests for the passive MESH topology board."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from rich.cells import cell_len
from rich.color import Color as RichColor
from textual.color import Color
from textual.events import MouseScrollDown

from app import (
    CHAT_SCROLLBAR_THUMB_GLYPH,
    DOT_GRID_GLYPH,
    DOT_GRID_SPACING_X,
    DOT_GRID_SPACING_Y,
    HORIZONTAL_SCROLLBAR_THUMB_GLYPH,
    ChatEntryWidget,
    MeshDotGrid,
    MeshNodeWidget,
    MeshTopologyView,
    MeshtasticPassApp,
    ThinScrollBarRender,
    _render_dot_grid,
)
from app_settings import AppSettings
from geo import GeoPosition
from mesh_topology import (
    HORIZONTAL_GAP,
    NODE_HEIGHT,
    NODE_WIDTH,
    VERTICAL_GAP,
    build_topology,
    compact_node_label,
    directional_target,
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
        for index in range(10):
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


class MeshDotGridRenderTests(unittest.TestCase):
    def test_dot_grid_uses_regular_spacing_and_glyph(self) -> None:
        width, height = 20, 9
        text = _render_dot_grid(width, height, "#222222")
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

    def test_dot_grid_is_empty_for_a_degenerate_canvas(self) -> None:
        self.assertEqual(str(_render_dot_grid(1, 1, "#222222")), "")

    def test_dot_grid_is_non_focusable(self) -> None:
        self.assertFalse(MeshDotGrid.can_focus)


class MeshTopologyAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )

    async def test_tab_renders_simulated_nodes_and_shared_local_menu(self) -> None:
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

    async def test_activity_favorite_theme_and_chat_share_one_node_state(self) -> None:
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
            alice = next(
                widget
                for widget in app.query(MeshNodeWidget)
                if widget.node.node_id == "!a11ce001"
            )
            stale = next(
                widget
                for widget in app.query(MeshNodeWidget)
                if widget.node.node_id == "!10a60006"
            )
            self.settings.set_favorite(alice.node.node_id, True)
            self.settings.set_favorite(stale.node.node_id, True)
            app._refresh_mesh()

            palette = THEME_PALETTES["white"]
            alice_text = alice.render()
            stale_text = stale.render()
            self.assertEqual(
                alice_text.spans[0].style.foreground,
                Color.parse(palette.accent),
            )
            self.assertEqual(
                stale_text.spans[0].style.foreground,
                Color.parse(palette.dim_base),
            )

            app._activate_node_action(alice.node.node_id, "favorite")
            chat_alice = next(
                widget
                for widget in app.query(ChatEntryWidget)
                if widget.entry.node_id == alice.node.node_id
            )
            self.assertTrue(chat_alice.has_class("favorite-sender"))

            app._activate_user_action(chat_alice, "unfavorite")
            self.assertFalse(self.settings.is_favorite(alice.node.node_id))
            self.assertEqual(
                alice.render().spans[0].style.foreground,
                Color.parse(palette.base),
            )

            alice.refresh_visual(
                now=(alice.node.last_heard or 0) + 300,
                favorite=False,
                theme="white",
            )
            self.assertEqual(
                alice.render().spans[0].style.foreground,
                Color.parse(palette.dim_base),
            )

            app._activate_node_action(alice.node.node_id, "favorite")

            app._apply_color_theme("green")
            self.assertEqual(
                alice.render().spans[0].style.foreground,
                Color.parse(THEME_PALETTES["green"].accent),
            )

    async def test_large_board_scrolls_both_axes_to_selected_node(self) -> None:
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
            rightmost = max(view.layout_model.nodes, key=lambda item: item.x)
            bottommost = max(view.layout_model.nodes, key=lambda item: item.y)
            view.focus_node(rightmost.node.node_id)
            await pilot.pause()
            right_widget = next(
                widget
                for widget in app.query(MeshNodeWidget)
                if widget.node.node_id == rightmost.node.node_id
            )
            self.assertGreater(
                view.scroll_x,
                0,
                (
                    view.size,
                    view.virtual_size,
                    view.max_scroll_x,
                    view.max_scroll_y,
                    view.board.size,
                    right_widget.region,
                    right_widget.virtual_region,
                    view.layout_model.width,
                ),
            )
            view.focus_node(bottommost.node.node_id)
            await pilot.pause()
            self.assertGreater(view.scroll_y, 0)

    async def test_opening_mesh_centers_local_node_in_viewport(self) -> None:
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
            # center_node() schedules a second, deferred scroll_to via
            # call_after_refresh to handle layout that isn't settled after
            # the first refresh; give it a second cycle to land before
            # asserting, so this isn't sensitive to system/scheduler timing.
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            local_widget = next(
                widget for widget in app.query(MeshNodeWidget) if widget.node.is_local
            )
            widget_center_x = local_widget.region.x + local_widget.region.width / 2
            widget_center_y = local_widget.region.y + local_widget.region.height / 2
            viewport_center_x = view.region.x + view.region.width / 2
            viewport_center_y = view.region.y + view.region.height / 2
            # Real scrolling must have been necessary for this to be meaningful.
            self.assertGreater(view.max_scroll_x, 0)
            self.assertLessEqual(abs(widget_center_x - viewport_center_x), NODE_WIDTH)
            self.assertLessEqual(abs(widget_center_y - viewport_center_y), NODE_HEIGHT)

    async def test_center_offsets_respect_scroll_bounds(self) -> None:
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
            rightmost = max(view.layout_model.nodes, key=lambda item: item.x)
            view.center_node(rightmost.node.node_id)
            await pilot.pause()
            self.assertGreaterEqual(view.scroll_x, 0)
            self.assertLessEqual(view.scroll_x, view.max_scroll_x)
            bottommost = max(view.layout_model.nodes, key=lambda item: item.y)
            view.center_node(bottommost.node.node_id)
            await pilot.pause()
            self.assertGreaterEqual(view.scroll_y, 0)
            self.assertLessEqual(view.scroll_y, view.max_scroll_y)

    async def test_small_topology_does_not_invent_scrolling_when_centered(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
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
            await pilot.press("4")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.max_scroll_x, 0)
            self.assertEqual(view.max_scroll_y, 0)
            self.assertEqual(view.scroll_x, 0)
            self.assertEqual(view.scroll_y, 0)

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

            # A long_name-only change is a metadata change that forces
            # set_nodes() to rebuild widgets (it is part of the rebuild
            # signature), without moving any node's hop layer/grid slot.
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
            view.focus_node(top.node.node_id)
            await pilot.pause()
            focused_before = app.focused
            scroll_before = (view.scroll_x, view.scroll_y)
            await pilot.press("up")
            await pilot.pause()
            self.assertIs(app.focused, focused_before)
            self.assertEqual((view.scroll_x, view.scroll_y), scroll_before)

    async def test_focus_node_does_not_scroll_when_target_already_visible(
        self,
    ) -> None:
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
            local = next(
                item for item in view.layout_model.nodes if item.node.is_local
            )
            view.center_node(local.node.node_id)
            await pilot.pause()
            scroll_before = (view.scroll_x, view.scroll_y)
            # The node is already fully visible (just centered), so refocusing
            # it via the plain (non-centering) navigation path must not scroll.
            view.focus_node(local.node.node_id)
            await pilot.pause()
            self.assertEqual((view.scroll_x, view.scroll_y), scroll_before)

    async def test_mouse_wheel_still_scrolls_mesh_view(self) -> None:
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
            view.scroll_to(x=0, y=0, animate=False, force=True, immediate=True)
            await pilot.pause()
            view._on_mouse_scroll_down(
                MouseScrollDown(view, 1, 1, 0, 1, 0, False, False, False)
            )
            await pilot.pause()
            self.assertGreater(view.scroll_y, 0)

    async def test_container_focus_recovers_to_node_on_arrow_press(self) -> None:
        """Regression test: the ScrollableContainer must never win arrow keys.

        This reproduces the root cause of the uConsole bug: if MeshTopologyView
        itself ever holds keyboard focus (rather than a node), pressing an
        arrow key must recover focus onto a node instead of silently scrolling
        via the container's inherited ScrollableContainer bindings.
        """
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
            view.focus()
            await pilot.pause()
            self.assertIsInstance(app.focused, MeshTopologyView)
            await pilot.press("down")
            await pilot.pause()
            self.assertIsInstance(app.focused, MeshNodeWidget)

    async def test_horizontal_and_vertical_scrollbar_use_matching_thin_renderer(
        self,
    ) -> None:
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
            self.assertIs(view.vertical_scrollbar.renderer, ThinScrollBarRender)
            self.assertIs(view.horizontal_scrollbar.renderer, ThinScrollBarRender)

    async def test_mesh_scrollbar_theme_colors_match_palette_on_both_axes(
        self,
    ) -> None:
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
            for theme, palette in THEME_PALETTES.items():
                app._apply_color_theme(theme)
                await pilot.pause()
                self.assertEqual(view.styles.scrollbar_size_vertical, 1)
                self.assertEqual(view.styles.scrollbar_size_horizontal, 1)
                self.assertEqual(view.styles.scrollbar_color.hex, palette.base)
                self.assertEqual(
                    view.styles.scrollbar_background.hex, palette.dim_base
                )
                self.assertEqual(
                    view.styles.scrollbar_corner_color.hex, palette.dim_base
                )

    async def test_scrollbar_geometry_is_one_cell_on_each_axis(self) -> None:
        """Both bars must consume exactly one cell of thickness, not two."""
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
            self.assertEqual(view.vertical_scrollbar.size.width, 1)
            self.assertEqual(view.horizontal_scrollbar.size.height, 1)

    async def test_scrollbar_corner_matches_track_color_no_black_box(self) -> None:
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
            # ScrollBarCorner.render() fills itself with self.parent.styles.
            # scrollbar_corner_color, so proving that style resolves to the
            # same color as the track (rather than Textual's unrelated
            # ansi_default/#666666 fallback) proves the corner cannot render
            # as a mismatched box.
            corner = view.scrollbar_corner
            self.assertIs(corner.parent, view)
            for theme, palette in THEME_PALETTES.items():
                app._apply_color_theme(theme)
                await pilot.pause()
                self.assertEqual(
                    view.styles.scrollbar_corner_color.hex, palette.dim_base
                )
                self.assertEqual(
                    view.styles.scrollbar_corner_color.hex,
                    view.styles.scrollbar_background.hex,
                )

    async def test_dot_grid_covers_canvas_matches_theme_and_renders_below_nodes(
        self,
    ) -> None:
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
            grid = app.query_one(MeshDotGrid)
            self.assertEqual(grid.styles.width.value, view.board.styles.width.value)
            self.assertEqual(grid.styles.height.value, view.board.styles.height.value)
            self.assertEqual(grid.styles.layer, "grid")
            node = next(iter(app.query(MeshNodeWidget)))
            self.assertEqual(node.styles.layer, "nodes")
            self.assertEqual(view.board.styles.layers, ("grid", "nodes"))

            for theme, palette in THEME_PALETTES.items():
                app._apply_color_theme(theme)
                await pilot.pause()
                self.assertEqual(
                    grid.render().spans[0].style.foreground,
                    Color.parse(palette.grid_dot),
                )

    async def test_emoji_node_labels_render_without_corrupting_board_geometry(
        self,
    ) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(42, 15)) as pilot:
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

            view = app.query_one(MeshTopologyView)
            widgets = list(app.query(MeshNodeWidget))
            self.assertEqual(len(widgets), len(emoji_nodes))
            for widget in widgets:
                label_line = str(widget.render()).splitlines()[0]
                self.assertLessEqual(cell_len(label_line), NODE_WIDTH)
            self.assertGreaterEqual(view.max_scroll_x, 0)
            self.assertGreaterEqual(view.max_scroll_y, 0)
            self.assertIs(view.horizontal_scrollbar.renderer, ThinScrollBarRender)
            self.assertIs(view.vertical_scrollbar.renderer, ThinScrollBarRender)


class ThinScrollBarRenderTests(unittest.TestCase):
    def test_horizontal_thin_render_matches_vertical_glyph_style(self) -> None:
        rendered = ThinScrollBarRender.render_bar(
            size=10,
            virtual_size=30,
            window_size=10,
            position=5,
            thickness=1,
            vertical=False,
            back_color=RichColor.parse(THEME_PALETTES["white"].dim_base),
            bar_color=RichColor.parse("#39ff14"),
        )
        thumb_segments = [
            segment
            for segment in rendered.segments
            if segment.style is not None
            and segment.style.meta.get("@mouse.down") == "grab"
        ]
        track_segments = [
            segment
            for segment in rendered.segments
            if segment.text != "\n" and segment not in thumb_segments
        ]
        self.assertTrue(thumb_segments)
        self.assertTrue(track_segments)
        self.assertTrue(
            all(
                segment.text == HORIZONTAL_SCROLLBAR_THUMB_GLYPH
                for segment in thumb_segments
            )
        )
        self.assertTrue(
            all(
                segment.text == HORIZONTAL_SCROLLBAR_THUMB_GLYPH
                for segment in track_segments
            )
        )
        self.assertTrue(all(segment.cell_length == 1 for segment in thumb_segments))
        self.assertTrue(all(segment.cell_length == 1 for segment in track_segments))
        self.assertNotEqual(
            HORIZONTAL_SCROLLBAR_THUMB_GLYPH, CHAT_SCROLLBAR_THUMB_GLYPH
        )

    def test_vertical_thin_render_is_unchanged_by_horizontal_support(self) -> None:
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
