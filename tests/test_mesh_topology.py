"""Pure and headless tests for the passive MESH topology board."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from textual.color import Color

from app import ChatEntryWidget, MeshNodeWidget, MeshTopologyView, MeshtasticPassApp
from app_settings import AppSettings
from mesh_topology import (
    HORIZONTAL_GAP,
    NODE_HEIGHT,
    NODE_WIDTH,
    VERTICAL_GAP,
    build_topology,
    compact_node_label,
    directional_target,
)
from radio_service import NodeMetadata, RadioState
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService
from theme_palette import THEME_PALETTES
from viewport_menu import ViewportMenu


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
    def test_you_is_central_and_hop_layers_are_ordered(self) -> None:
        layout = build_topology(sample_nodes())
        by_id = {item.node.node_id: item for item in layout.nodes}
        local = by_id["!00000000"]
        one_hop = [item for item in layout.nodes if item.layer == 1]
        two_hop = [item for item in layout.nodes if item.layer == 2]
        unknown = [item for item in layout.nodes if item.layer is None]

        def radius(item):
            return max(
                abs(item.x - local.x) // (NODE_WIDTH + HORIZONTAL_GAP),
                abs(item.y - local.y) // (NODE_HEIGHT + VERTICAL_GAP),
            )

        self.assertTrue(one_hop and two_hop and unknown)
        self.assertLess(max(map(radius, one_hop)), min(map(radius, two_hop)))
        self.assertLess(max(map(radius, two_hop)), min(map(radius, unknown)))

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
        layout = build_topology(sample_nodes())
        local = next(item for item in layout.nodes if item.node.is_local)
        target = directional_target(local.node.node_id, layout, "up")
        self.assertIsNotNone(target)
        assert target is not None
        self.assertLess(target.y, local.y)
        top = min(layout.nodes, key=lambda item: item.y)
        self.assertIsNone(directional_target(top.node.node_id, layout, "up"))


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
            await pilot.press("5")
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            self.assertIn("[5] MESH", str(app.query_one("#tab-bar").render()))
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
            await pilot.press("5")
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
            await pilot.press("5")
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
            await pilot.press("5")
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
