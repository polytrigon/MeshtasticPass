"""Viewport popup, node menu, favorites, and cleanup milestone tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from textual.geometry import Region
from textual.widgets import Input, Static

from app import (
    ChannelSelector,
    ChatEntryWidget,
    ChatTranscript,
    ColorSelector,
    ConnectionPage,
    FontSizeSelector,
    MeshtasticPassApp,
)
from app_settings import AppSettings
from chat_store import ChatStore
from radio_service import NodeMetadata, RadioService, RadioState
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService
from theme_palette import THEME_PALETTES
from viewport_menu import (
    PopupMenuRow,
    ViewportMenu,
    calculate_popup_placement,
)


class PopupPlacementTests(unittest.TestCase):
    def test_prefers_down_then_up_and_constrains_to_larger_side(self) -> None:
        viewport = Region(0, 0, 80, 24)
        down = calculate_popup_placement(Region(4, 2, 20, 1), viewport, 24, 7)
        self.assertEqual(down.direction, "down")
        self.assertFalse(down.constrained)

        up = calculate_popup_placement(Region(4, 21, 20, 1), viewport, 24, 7)
        self.assertEqual(up.direction, "up")
        self.assertFalse(up.constrained)

        constrained = calculate_popup_placement(
            Region(4, 4, 20, 1), Region(0, 0, 40, 9), 24, 12
        )
        self.assertTrue(constrained.constrained)
        self.assertEqual(constrained.height, 4)
        self.assertGreaterEqual(constrained.y, 0)
        self.assertLessEqual(constrained.y + constrained.height, 9)


class ViewportContextFavoriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )

    @staticmethod
    def radio() -> SimulatedRadioService:
        return SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )

    async def test_dropdowns_use_visible_viewport_and_keep_xxl_visible(self) -> None:
        for configured_size in (18, 22):
            with self.subTest(font_size=configured_size):
                self.settings.font_size = configured_size
                app = MeshtasticPassApp(self.radio(), self.settings)
                async with app.run_test(size=(52, 9)) as pilot:
                    await pilot.pause()
                    page = app.query_one(ConnectionPage)
                    font = app.query_one(FontSizeSelector)
                    color = app.query_one(ColorSelector)

                    font.focus()
                    font.scroll_visible(animate=False)
                    await pilot.pause()
                    await pilot.press("enter")
                    popup = font.popup
                    self.assertIsNotNone(popup)
                    assert popup is not None and popup.placement is not None
                    self.assertTrue(popup.placement.constrained)
                    self.assertLessEqual(popup.placement.y + popup.placement.height, 9)
                    for _ in range(5):
                        if font.options[font._highlighted_index].label == "XXL":
                            break
                        await pilot.press("down")
                    await pilot.pause()
                    self.assertEqual(
                        font.options[font._highlighted_index].label,
                        "XXL",
                    )
                    row = list(popup.query(PopupMenuRow))[font._highlighted_index]
                    self.assertGreaterEqual(row.region.y, popup.region.y)
                    self.assertLess(row.region.y, popup.region.bottom)
                    await pilot.press("escape")

                    color.focus()
                    color.scroll_visible(animate=False)
                    await pilot.pause()
                    await pilot.press("enter")
                    assert color.popup is not None
                    self.assertEqual(color.popup.placement.direction, "up")
                    self.assertGreater(page.max_scroll_y, 0)
                    self.assertFalse(page.allow_horizontal_scroll)

    async def test_top_and_chat_channel_dropdown_open_downward(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            selector = app.query_one(ChannelSelector)
            selector.focus()
            await pilot.press("enter")
            assert selector.popup is not None
            self.assertEqual(selector.popup.placement.direction, "down")
            self.assertTrue(callable(PopupMenuRow.clicked))
            rows = list(selector.popup.query(PopupMenuRow))
            self.assertGreaterEqual(len(rows), 2)
            expected_value = selector.options[1].value
            rows[1].clicked()
            await pilot.pause()
            self.assertEqual(selector.value, expected_value)
            self.assertFalse(selector.is_open)

    async def test_context_menu_metadata_escape_and_outgoing_exclusion(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(58, 14)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(SIMULATED_MESSAGES[0])
            widget = list(app.query(ChatEntryWidget))[-1]
            widget.focus()
            transcript = app.query_one(ChatTranscript)
            await pilot.pause()
            old_scroll = transcript.scroll_y
            await pilot.press("enter")
            await pilot.pause()

            menu = app.query_one("#node-context-menu", ViewportMenu)
            labels = [item.label for item in menu.items]
            self.assertEqual(
                labels,
                ["Alice Trail", "ALCE", "1 HOP AWAY", "REPLY", "DIRECT MSG", "HIGHLIGHT", "REMOVE NODE"],
            )
            self.assertEqual(
                [item.actionable for item in menu.items],
                [False, False, False, True, True, True, True],
            )
            await pilot.press("up", "down")
            self.assertEqual(menu.highlighted_index, 5)
            self.assertIsNotNone(menu.placement)
            assert menu.placement is not None
            self.assertLessEqual(menu.placement.y + menu.placement.height, 14)

            await pilot.press("escape")
            await pilot.pause()
            self.assertIs(app.focused, widget)
            self.assertEqual(transcript.scroll_y, old_scroll)

            app._accepted_send("local message")
            await pilot.pause()
            outgoing_widget = list(app.query(ChatEntryWidget))[-1]
            self.assertTrue(outgoing_widget.entry.outgoing)
            outgoing_widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(app.query("#node-context-menu")), 0)

    async def test_favorite_is_persistent_immediate_and_zero_radio_traffic(self) -> None:
        radio = self.radio()
        radio.send_text = Mock(wraps=radio.send_text)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(SIMULATED_MESSAGES[0])
            app._accept_received_message(SIMULATED_MESSAGES[1])
            first = next(
                widget
                for widget in app.query(ChatEntryWidget)
                if widget.entry.node_id == "!a11ce001"
            )
            unrelated = next(
                widget
                for widget in app.query(ChatEntryWidget)
                if widget.entry.node_id != "!a11ce001"
            )
            first.focus()
            await pilot.press("enter", "enter")
            await pilot.pause()

            self.assertTrue(self.settings.is_favorite("!a11ce001"))
            self.assertTrue(first.has_class("favorite-sender"))
            self.assertFalse(unrelated.has_class("favorite-sender"))
            for name, palette in THEME_PALETTES.items():
                app._apply_color_theme(name)
                await pilot.pause()
                self.assertEqual(
                    first.query_one(
                        ".chat-entry-author", Static
                    ).visual_style.foreground.hex6,
                    palette.accent2,
                )
            radio.send_text.assert_not_called()

            app.show_tab("connection")
            app.show_tab("chat")
            await pilot.pause()
            for name, palette in THEME_PALETTES.items():
                app._apply_color_theme(name)
                await pilot.pause()
                self.assertEqual(
                    first.timestamp_label.visual_style.foreground.hex6,
                    palette.dim_base,
                )

            renamed = replace(
                SIMULATED_MESSAGES[0],
                sender_long_name="Alice Renamed",
                packet_id=987654,
            )
            app._accept_received_message(renamed)
            renamed_widget = list(app.query(ChatEntryWidget))[-1]
            self.assertTrue(renamed_widget.has_class("favorite-sender"))

        reloaded = AppSettings.load(config_path=self.settings.config_path)
        self.assertTrue(reloaded.is_favorite("!a11ce001"))

    async def test_duplicate_display_names_remain_distinct_favorites(self) -> None:
        """FAVORITE is keyed to stable node ID, never display name -- two

        different nodes both calling themselves ALICE must never share
        or cross-contaminate favorite state.
        """
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            first_alice = replace(
                SIMULATED_MESSAGES[0],
                sender_node_id="!a11ce001",
                sender_long_name="ALICE",
                packet_id=700001,
            )
            second_alice = replace(
                SIMULATED_MESSAGES[0],
                sender_node_id="!a11ce002",
                sender_long_name="ALICE",
                packet_id=700002,
            )
            app._accept_received_message(first_alice)
            app._accept_received_message(second_alice)
            first_widget = next(
                w for w in app.query(ChatEntryWidget) if w.entry.node_id == "!a11ce001"
            )
            second_widget = next(
                w for w in app.query(ChatEntryWidget) if w.entry.node_id == "!a11ce002"
            )

            first_widget.focus()
            await pilot.press("enter", "enter")
            await pilot.pause()

            self.assertTrue(self.settings.is_favorite("!a11ce001"))
            self.assertFalse(self.settings.is_favorite("!a11ce002"))
            self.assertTrue(first_widget.has_class("favorite-sender"))
            self.assertFalse(second_widget.has_class("favorite-sender"))

    async def test_unfavorite_updates_all_mounted_entries(self) -> None:
        self.settings.set_favorite("!a11ce001", True)
        self.settings.save()
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(SIMULATED_MESSAGES[0])
            app._accept_received_message(
                replace(SIMULATED_MESSAGES[0], packet_id=9911, text="again")
            )
            widgets = list(app.query(ChatEntryWidget))
            self.assertTrue(all(widget.has_class("favorite-sender") for widget in widgets))
            widgets[-1].focus()
            await pilot.press("enter")
            await pilot.pause()
            menu = app.query_one("#node-context-menu", ViewportMenu)
            highlight_item = next(
                item for item in menu.items if item.value in ("favorite", "unfavorite")
            )
            self.assertEqual(
                highlight_item.label,
                "UNHIGHLIGHT",
            )
            await pilot.press("enter")
            await pilot.pause()
            self.assertFalse(self.settings.is_favorite("!a11ce001"))
            self.assertTrue(
                all(not widget.has_class("favorite-sender") for widget in widgets)
            )

    async def test_missing_metadata_is_omitted_and_unknown_hops_not_fabricated(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(70, 18)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            missing_long = replace(
                SIMULATED_MESSAGES[0],
                sender_node_id="!10a60006",
                sender_long_name=None,
                sender_short_name="NOLN",
                packet_id=6006,
            )
            app._accept_received_message(missing_long)
            widget = list(app.query(ChatEntryWidget))[-1]
            widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            labels = [item.label for item in app._user_menu.items]
            self.assertEqual(labels, ["NOLN", "2 HOPS AWAY", "REPLY", "DIRECT MSG", "HIGHLIGHT", "REMOVE NODE"])
            await pilot.press("escape")

            missing_short_unknown_hops = replace(
                SIMULATED_MESSAGES[0],
                sender_node_id="!5a070007",
                sender_long_name="No Short Name",
                sender_short_name=None,
                packet_id=7007,
            )
            app._accept_received_message(missing_short_unknown_hops)
            widget = list(app.query(ChatEntryWidget))[-1]
            widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            labels = [item.label for item in app._user_menu.items]
            self.assertEqual(labels, ["No Short Name", "REPLY", "DIRECT MSG", "HIGHLIGHT", "REMOVE NODE"])

    async def test_right_and_end_jump_newest_without_footer_hint(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(60, 16)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            for packet_id in range(20):
                app._accept_received_message(
                    replace(SIMULATED_MESSAGES[0], packet_id=8000 + packet_id)
                )
            transcript = app.query_one(ChatTranscript)
            transcript.scroll_to(y=0, animate=False)
            app.transcript_new_count = 3
            app._update_transcript_indicator()
            transcript.focus()
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.transcript_new_count, 0)
            self.assertTrue(transcript.is_vertical_scroll_end)
            self.assertIs(app.focused, app.query_one("#chat-input", Input))

            transcript.scroll_to(y=0, animate=False)
            app.transcript_new_count = 2
            transcript.focus()
            await pilot.press("end")
            await pilot.pause()
            self.assertEqual(app.transcript_new_count, 0)
            self.assertIs(app.focused, transcript)
            footer = str(app.query_one("#footer", Static).render())
            self.assertNotIn("END", footer.upper())
            self.assertNotIn("RIGHT", footer.upper())
            self.assertNotIn("→", footer)

    async def test_connection_layout_is_flat_truthful_and_scrollable(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(52, 9)) as pilot:
            await pilot.pause()
            page = app.query_one(ConnectionPage)
            titles = [str(item.render()) for item in page.query(".page-title")]
            self.assertEqual(
                titles, ["CONNECTION", "NETWORK", "RADIO", "STYLE"]
            )
            self.assertEqual(len(page.query("#identity-title")), 0)
            self.assertEqual(len(page.query("#identity-waiting")), 0)
            self.assertGreater(page.max_scroll_y, 0)
            self.assertFalse(page.allow_horizontal_scroll)

            app._show_connection(RadioState.CONNECTING)
            self.assertIn("NODES        ...", str(app.query_one("#connection-details", Static).render()))
            self.assertEqual(
                str(app.query_one("#identity-short-name-unavailable", Static).render()),
                "...",
            )
            app._show_connection(RadioState.ONLINE, radio.info)
            self.assertIn("NODES        9", str(app.query_one("#connection-details", Static).render()))
            self.assertEqual(app.query_one("#short-name-input", Input).value, "SIM")
            app._show_connection(RadioState.OFFLINE)
            self.assertIn("NODES        —", str(app.query_one("#connection-details", Static).render()))
            self.assertIn("NODE ID      —", str(app.query_one("#identity-values", Static).render()))


class NodeMetadataTests(unittest.TestCase):
    def test_real_service_uses_only_synced_optional_hops_away(self) -> None:
        service = RadioService()
        interface = Mock()
        interface.nodesByNum = {
            0xA11CE001: {
                "user": {
                    "id": "!a11ce001",
                    "longName": "Alice Trail",
                    "shortName": "ALCE",
                },
                "hopsAway": 1,
                "hopLimit": 7,
            }
        }
        interface.nodes = {}
        service._interface = interface
        self.assertEqual(
            service.get_node_metadata("!a11ce001"),
            NodeMetadata("!a11ce001", "Alice Trail", "ALCE", 1),
        )
        interface.nodesByNum[0xA11CE001].pop("hopsAway")
        self.assertIsNone(service.get_node_metadata("!a11ce001").hops_away)


class NodeRemoveTests(unittest.IsolatedAsyncioTestCase):
    """REMOVE NODE: real radio-side NodeDB removal via the shared node menu.

    CHAT and MESH both surface it from the SAME _open_node_menu, keyed only
    by the canonical node ID. Two-step confirm, self-node protection, no
    whole-NodeDB clear, and stored CHAT/DM/channel history preserved.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )

    def _make_app(self, radio=None, chat_store=None):
        return MeshtasticPassApp(radio or self._radio(), self.settings, chat_store=chat_store)

    @staticmethod
    def _radio() -> SimulatedRadioService:
        return SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )

    async def test_chat_menu_includes_remove_node(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(58, 16)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(SIMULATED_MESSAGES[0])
            widget = list(app.query(ChatEntryWidget))[-1]
            widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            labels = [item.label for item in app._user_menu.items]
            self.assertIn("REMOVE NODE", labels)
            index = labels.index("REMOVE NODE")
            self.assertTrue(app._user_menu.items[index].actionable)
            self.assertEqual(app._user_menu.items[index].value, "remove")

    async def test_remove_node_is_two_step_and_calls_radio_with_canonical_id(
        self,
    ) -> None:
        radio = self._radio()
        radio.remove_node = Mock(wraps=radio.remove_node)
        app = self._make_app(radio)
        async with app.run_test(size=(58, 16)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(SIMULATED_MESSAGES[0])
            node_id = SIMULATED_MESSAGES[0].sender_node_id
            widget = list(app.query(ChatEntryWidget))[-1]
            widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            # First activation only arms confirmation -- no radio call yet.
            app._activate_menu_item(
                NodeMetadata(node_id, "Alice", "ALCE"), "remove"
            )
            await pilot.pause()
            radio.remove_node.assert_not_called()
            # Second activation performs the removal.
            app._activate_menu_item(
                NodeMetadata(node_id, "Alice", "ALCE"), "remove"
            )
            await pilot.pause()
            radio.remove_node.assert_called_once()
            self.assertEqual(radio.remove_node.call_args[0][0], node_id)

    async def test_self_node_cannot_be_removed(self) -> None:
        radio = self._radio()
        radio.remove_node = Mock(wraps=radio.remove_node)
        app = self._make_app(radio)
        async with app.run_test(size=(58, 16)) as pilot:
            await pilot.pause()
            # Confirm the radio boundary refuses the local node's own id.
            result = radio.remove_node(radio.info.node_id)
            self.assertFalse(result.removed)
            self.assertEqual(result.reason, "local")

    async def test_removal_preserves_channel_history(self) -> None:
        from chat_store import ChatStore

        store = ChatStore.open(Path(self.directory.name) / "chat.db")
        radio = self._radio()
        store.add_incoming(
            packet_id=1, node_id="!a11ce001", sender_name="Alice Trail",
            sender_short_name="ALCE", channel_index=0, text="keep me",
            radio_rx_at=100.0, received_at=100.0,
        )
        app = self._make_app(radio, chat_store=store)
        async with app.run_test(size=(58, 16)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            self.assertIn("keep me", [e.text for e in app.chat_history])
            # Removing the node's NodeDB entry must NOT delete history.
            app._request_node_remove(NodeMetadata("!a11ce001", "Alice", "ALCE"))
            await pilot.pause()
            app._request_node_remove(NodeMetadata("!a11ce001", "Alice", "ALCE"))
            for _ in range(5):
                await pilot.pause()
            self.assertIn("keep me", [e.text for e in app.chat_history])

    async def test_unsupported_radio_reports_error_without_fake_success(self) -> None:
        radio = self._radio()
        radio.remove_node = None  # no supported admin boundary
        app = self._make_app(radio)
        async with app.run_test(size=(58, 16)) as pilot:
            await pilot.pause()
            notifications = []
            app.notify = lambda message, **kwargs: notifications.append(message)
            app._request_node_remove(NodeMetadata("!a11ce001", "Alice", "ALCE"))
            await pilot.pause()
            app._request_node_remove(NodeMetadata("!a11ce001", "Alice", "ALCE"))
            await pilot.pause()
            self.assertIn("not supported", " ".join(notifications))
            # No radio state was cleared/faked: the node is still present.
            self.assertNotIn(
                "!a11ce001", getattr(app.radio, "_removed_node_ids", set())
            )

    async def test_removed_node_reappears_on_normal_discovery(self) -> None:
        radio = self._radio()
        app = self._make_app(radio)
        async with app.run_test(size=(58, 16)) as pilot:
            await pilot.pause()
            # Remove a remote node via the radio boundary.
            radio.remove_node("!a11ce001")
            self.assertIn("!a11ce001", radio._removed_node_ids)
            self.assertNotIn(
                "!a11ce001",
                {m.node_id for m in radio.get_known_nodes()},
            )
            # Normal mesh traffic (no probe) lets it reappear fresh.
            radio._hear_node("!a11ce001")
            self.assertNotIn("!a11ce001", radio._removed_node_ids)
            self.assertIn(
                "!a11ce001", {m.node_id for m in radio.get_known_nodes()}
            )


if __name__ == "__main__":
    unittest.main()
