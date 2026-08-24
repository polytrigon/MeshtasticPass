"""Headless interaction tests for the Textual app shell."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Event
from time import monotonic, time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from textual.containers import Horizontal
from textual.widgets import Input, Static

from app import (
    ChatTranscript,
    ChatEntryWidget,
    ColorSelector,
    FontSizeSelector,
    MeshtasticPassApp,
)
from app_settings import AppSettings
from chat_store import ChatStore
from radio_service import (
    DeliveryState,
    RadioEvent,
    RadioInfo,
    RadioService,
    RadioState,
)
from simulated_radio_service import (
    SIMULATED_MESSAGES,
    SimulatedRadioService,
    SimulatedSendOutcome,
)


class CallbackRadioService(RadioService):
    """Real packet parser with hardware-free connection events for UI tests."""

    def __init__(self) -> None:
        super().__init__("/dev/test-radio")
        self.interface = SimpleNamespace(
            nodesByNum={
                0xABC12345: {
                    "user": {"longName": "Alice Trail", "shortName": "ALCE"}
                }
            },
            nodes={},
        )
        self.info = RadioInfo(
            device_path=self.device_path,
            node_id="!51a00001",
            long_name="Test Radio",
            short_name="TEST",
            firmware_version="test",
            known_nodes=2,
        )

    def connection_events(
        self,
        retry_delay: float = 5.0,
        stop_event: Event | None = None,
        poll_interval: float = 0.25,
    ):
        stopped = stop_event or Event()
        self._interface = self.interface
        yield RadioEvent(RadioState.CONNECTING)
        yield RadioEvent(RadioState.ONLINE, info=self.info)
        stopped.wait()


class MeshtasticPassAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=root / "meshtasticpass" / "config.json",
            profile_path=root / "lxterminal" / "lxterminal-meshtasticpass.conf",
        )
        self.chat_db_path = root / "data" / "chat.db"

    async def test_connection_tabs_input_and_send(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.current_tab, "connection")

            await pilot.press("2")
            self.assertEqual(app.current_tab, "chat")
            chat_input = app.query_one("#chat-input", Input)

            await pilot.press("1")
            self.assertEqual(app.current_tab, "chat")
            self.assertEqual(chat_input.value, "1")

            chat_input.value = "hello from test"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.chat_history[-1].author, "YOU")
            self.assertEqual(app.chat_history[-1].text, "hello from test")
            self.assertEqual(chat_input.value, "")

            await pilot.press("escape", "3")
            self.assertEqual(app.current_tab, "profile")
            await pilot.press("4")
            self.assertEqual(app.current_tab, "pass-map")

        self.assertTrue(radio.is_closed)

    async def test_simulated_messages_reach_chat_history_once(self) -> None:
        radio = SimulatedRadioService(connect_delay=0, message_interval=0)
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            primary_messages = [
                message
                for message in SIMULATED_MESSAGES
                if (message.channel_index or 0) == 0
            ]
            for _ in range(5):
                await pilot.pause()
                if len(app.chat_history) == len(primary_messages):
                    break

            self.assertEqual(len(app.chat_history), len(primary_messages))
            self.assertEqual(app.chat_history[0].author, "Alice Trail")
            self.assertEqual(
                [entry.text for entry in app.chat_history],
                [message.text for message in primary_messages],
            )
            self.assertEqual(
                [entry.radio_rx_at for entry in app.chat_history],
                [message.radio_rx_at for message in primary_messages],
            )
            self.assertTrue(
                all(
                    entry.radio_rx_at != entry.received_at
                    for entry in app.chat_history
                )
            )
            self.assertEqual(app.unread_count, len(primary_messages))
            self.assertIn(
                f"CHAT({len(primary_messages)})",
                str(app.query_one("#tab-bar", Static).render()),
            )

    async def test_font_size_selection_saves_setting_and_profile(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            selector = app.query_one(FontSizeSelector)
            self.assertEqual(selector.font_size, 13)
            self.assertIn(
                "CONNECTION/CONFIG",
                str(app.query_one("#tab-bar", Static).render()),
            )

            await pilot.press("right")
            await pilot.pause()

            self.assertEqual(selector.font_size, 16)
            self.assertEqual(self.settings.font_size, 16)
            self.assertIn(
                "APPLIES ON NEXT LAUNCH",
                str(app.query_one("#style-status", Static).render()),
            )

        reloaded = AppSettings.load(
            config_path=self.settings.config_path,
            profile_path=self.settings.profile_path,
        )
        self.assertEqual(reloaded.font_size, 16)
        self.assertIn(
            "fontname=Monospace 16",
            self.settings.profile_path.read_text(encoding="utf-8"),
        )

    async def test_connection_state_feedback_clears_stale_metadata(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=10,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        info = RadioInfo(
            device_path=radio.device_path,
            node_id="!433a9a3c",
            long_name="@Polytrigon",
            short_name="9a3c",
            firmware_version="2.5.3.a70d5ee",
            known_nodes=100,
        )

        async with app.run_test(size=(100, 30)):
            details = app.query_one("#connection-details", Static)
            connecting = str(details.render())
            self.assertIn("STATUS     CONNECTING.", connecting)
            self.assertIn("DEVICE     simulated://meshtastic", connecting)

            app._show_connection(RadioState.ONLINE, info)
            online = str(details.render())
            self.assertIn("STATUS     ONLINE", online)
            self.assertIn("NODE       !433a9a3c", online)
            self.assertIn("NAME       @Polytrigon (9a3c)", online)

            app._show_connection(RadioState.OFFLINE, info)
            offline = str(details.render())
            self.assertIn("STATUS     OFFLINE — RETRYING.", offline)
            self.assertNotIn("!433a9a3c", offline)
            self.assertNotIn("FIRMWARE", offline)

            app._show_connection(RadioState.ERROR, info, "raw SDK exception")
            error = str(details.render())
            self.assertIn("STATUS     CONNECTION ERROR — RETRYING.", error)
            self.assertNotIn("!433a9a3c", error)
            self.assertNotIn(
                "raw SDK exception",
                str(app.query_one("#connection-error", Static).render()),
            )

    async def test_connection_status_animation_reuses_one_timer(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=10,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        info = radio.info

        async with app.run_test(size=(100, 30)) as pilot:
            details = app.query_one("#connection-details", Static)
            timer = app._connection_animation_timer
            self.assertIsNotNone(timer)

            self.assertIn("STATUS     CONNECTING.", str(details.render()))
            await pilot.pause(0.5)
            self.assertIn("STATUS     CONNECTING..", str(details.render()))
            app._advance_connection_animation()
            self.assertIn("STATUS     CONNECTING...", str(details.render()))
            app._advance_connection_animation()
            self.assertIn("STATUS     CONNECTING.", str(details.render()))

            app._show_connection(RadioState.OFFLINE)
            app._advance_connection_animation()
            self.assertIn("STATUS     OFFLINE — RETRYING..", str(details.render()))

            app._show_connection(RadioState.ERROR)
            app._advance_connection_animation()
            self.assertIn(
                "STATUS     CONNECTION ERROR — RETRYING..",
                str(details.render()),
            )

            app._show_connection(RadioState.ONLINE, info)
            online = str(details.render())
            app._advance_connection_animation()
            self.assertEqual(str(details.render()), online)
            self.assertIn("STATUS     ONLINE", online)
            self.assertNotIn("ONLINE.", online)

            for state in (
                RadioState.CONNECTING,
                RadioState.OFFLINE,
                RadioState.ERROR,
                RadioState.ONLINE,
            ):
                app._show_connection(state, info)
                self.assertIs(app._connection_animation_timer, timer)

        self.assertIsNone(timer._task)  # Timer is explicitly stopped on unmount.

    async def test_style_keyboard_navigation_and_live_color(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            font_selector = app.query_one(FontSizeSelector)
            color_selector = app.query_one(ColorSelector)
            self.assertTrue(font_selector.has_focus)
            self.assertEqual(color_selector.color, "white")
            self.assertTrue(app.screen.has_class("theme-white"))

            await pilot.press("down")
            self.assertTrue(color_selector.has_focus)
            self.assertTrue(str(color_selector.render()).startswith("> COLOR"))
            self.assertTrue(str(font_selector.render()).startswith("  FONT SIZE"))
            await pilot.press("right")
            await pilot.pause()

            self.assertEqual(color_selector.color, "green")
            self.assertEqual(self.settings.color, "green")
            self.assertTrue(app.screen.has_class("theme-green"))
            self.assertFalse(app.screen.has_class("theme-white"))

            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(color_selector.color, "orange")
            self.assertTrue(app.screen.has_class("theme-orange"))

            await pilot.press("up", "right")
            await pilot.pause()
            self.assertTrue(font_selector.has_focus)
            self.assertEqual(font_selector.font_size, 16)
            self.assertEqual(color_selector.color, "orange")

        reloaded = AppSettings.load(config_path=self.settings.config_path)
        self.assertEqual(reloaded.color, "orange")

    async def test_style_status_inherits_white_green_and_orange(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            status = app.query_one("#style-status", Static)
            self.assertEqual(status.visual_style.foreground.hex6, "#D8D8D8")

            await pilot.press("down", "right")
            await pilot.pause()
            self.assertIn("COLOR SAVED", str(status.render()))
            self.assertEqual(status.visual_style.foreground.hex6, "#39FF14")

            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(status.visual_style.foreground.hex6, "#FF8C00")

            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(status.visual_style.foreground.hex6, "#D8D8D8")

    async def test_chat_unread_counter_tracks_only_hidden_incoming_messages(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        incoming = SIMULATED_MESSAGES[0]

        async with app.run_test(size=(100, 30)):
            tab_bar = app.query_one("#tab-bar", Static)

            app._accept_received_message(incoming)
            self.assertEqual(app.unread_count, 1)
            self.assertIn("CHAT(1)", str(tab_bar.render()))

            app._accept_received_message(incoming)
            rendered = str(tab_bar.render())
            self.assertEqual(app.unread_count, 2)
            self.assertIn("CHAT(2)", rendered)
            self.assertNotIn("CHAT (2)", rendered)

            app.show_tab("chat")
            self.assertEqual(app.unread_count, 0)
            self.assertNotIn("CHAT(", str(tab_bar.render()))

            app._accept_received_message(incoming)
            self.assertEqual(app.unread_count, 0)

            app.show_tab("connection")
            app._accepted_send("local message")
            self.assertEqual(app.unread_count, 0)
            self.assertNotIn("CHAT(", str(tab_bar.render()))

            app.show_tab("profile")
            self.assertEqual(app.unread_count, 0)
            app._accept_received_message(incoming)
            self.assertEqual(app.unread_count, 1)
            self.assertIn("CHAT(1)", str(tab_bar.render()))

            app.show_tab("chat")
            self.assertEqual(app.unread_count, 0)
            self.assertNotIn("CHAT(", str(tab_bar.render()))

    async def test_chat_entries_timestamp_and_refresh_with_one_shared_timer(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        incoming = SIMULATED_MESSAGES[0]

        async with app.run_test(size=(100, 30)) as pilot:
            timestamp_timer = app._chat_timestamp_timer
            self.assertIsNotNone(timestamp_timer)

            before_incoming = time()
            app._accept_received_message(incoming)
            incoming_entry = app.chat_history[-1]
            self.assertGreaterEqual(incoming_entry.received_at, before_incoming)
            self.assertEqual(
                incoming_entry.radio_rx_at,
                incoming.radio_rx_at,
            )
            self.assertIsNone(incoming_entry.local_sent_at)

            before_outgoing = time()
            app._accepted_send("local timestamp")
            outgoing_entry = app.chat_history[-1]
            self.assertGreaterEqual(outgoing_entry.received_at, before_outgoing)
            self.assertIsNone(outgoing_entry.radio_rx_at)
            self.assertEqual(
                outgoing_entry.local_sent_at,
                outgoing_entry.received_at,
            )
            self.assertTrue(outgoing_entry.outgoing)

            await pilot.pause()
            widgets = list(app.query(ChatEntryWidget))
            self.assertEqual(len(widgets), 2)
            self.assertIs(app._chat_timestamp_timer, timestamp_timer)

            app._refresh_chat_timestamps(incoming_entry.age_reference + 63 * 60)
            await pilot.pause()
            incoming_timestamp = widgets[0].query_one(
                ".chat-entry-timestamp", Static
            )
            self.assertEqual(str(incoming_timestamp.render()), "1h 3min")

        self.assertIsNone(timestamp_timer._task)

    async def test_timestamp_theme_colors_and_electric_errors(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            app._accept_received_message(SIMULATED_MESSAGES[0])
            app.show_tab("chat")
            app.show_tab("connection")
            await pilot.pause()
            timestamp = app.query_one(".chat-entry-timestamp", Static)
            error = app.query_one("#send-error", Static)

            expected_timestamp_colors = {
                "white": "#8A8A8A",
                "green": "#168F0A",
                "orange": "#A85C00",
            }
            for color, expected in expected_timestamp_colors.items():
                app._apply_color_theme(color)
                await pilot.pause()
                self.assertEqual(timestamp.visual_style.foreground.hex6, expected)
                self.assertEqual(error.visual_style.foreground.hex6, "#FF1744")

    async def test_chat_primary_heading_and_timestamp_hierarchy(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await pilot.pause()

            heading = app.query_one("#chat-title", Static)
            widget = app.query_one(ChatEntryWidget)
            author = widget.query_one(".chat-entry-author", Static)
            timestamp = widget.query_one(".chat-entry-timestamp", Static)

            self.assertIn("CHAT — PRIMARY", str(heading.render()))
            self.assertIs(author.parent, timestamp.parent)
            self.assertIsInstance(author.parent, Horizontal)
            self.assertEqual(author.region.y, timestamp.region.y)
            self.assertTrue(author.visual_style.bold)
            self.assertFalse(bool(timestamp.visual_style.bold))
            self.assertTrue(timestamp.visual_style.dim)

    async def test_new_message_header_lifecycle_and_themes(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        incoming = SIMULATED_MESSAGES[0]

        async with app.run_test(size=(100, 30)) as pilot:
            self.assertFalse(hasattr(app, "_new_message_timer"))

            app._accept_received_message(incoming)
            unread_entry = app.chat_history[-1]
            self.assertTrue(unread_entry.unread)
            self.assertTrue(unread_entry.is_new)
            self.assertEqual(app.unread_count, 1)

            app.show_tab("chat")
            await pilot.pause()
            unread_widget = list(app.query(ChatEntryWidget))[-1]
            unread_author = unread_widget.query_one(".chat-entry-author", Static)
            unread_timestamp = unread_widget.timestamp_label
            unread_body = unread_widget.message_label
            self.assertFalse(unread_entry.unread)
            self.assertTrue(unread_entry.is_new)
            self.assertTrue(unread_widget.has_class("new-message"))
            self.assertEqual(app.unread_count, 0)

            expected_highlight_colors = {
                "white": "#39FF14",
                "green": "#FF8C00",
                "orange": "#D8D8D8",
            }
            for theme, expected in expected_highlight_colors.items():
                app._apply_color_theme(theme)
                await pilot.pause()
                for part in (unread_author, unread_timestamp, unread_body):
                    self.assertEqual(part.visual_style.foreground.hex6, expected)

            app._refresh_chat_timestamps(
                unread_entry.age_reference + 24 * 60 * 60
            )
            await pilot.pause()
            self.assertTrue(unread_entry.is_new)
            self.assertTrue(unread_widget.has_class("new-message"))

            app.show_tab("profile")
            await pilot.pause()
            self.assertFalse(unread_entry.is_new)
            self.assertFalse(unread_widget.has_class("new-message"))

            app.show_tab("chat")
            await pilot.pause()
            self.assertFalse(unread_widget.has_class("new-message"))
            self.assertEqual(unread_author.visual_style.foreground.hex6, "#FF8C00")
            self.assertEqual(unread_timestamp.visual_style.foreground.hex6, "#A85C00")
            self.assertEqual(unread_body.visual_style.foreground.hex6, "#FF8C00")

            app._accept_received_message(incoming)
            visible_widget = list(app.query(ChatEntryWidget))[-1]
            self.assertEqual(app.unread_count, 0)
            self.assertFalse(app.chat_history[-1].unread)
            self.assertTrue(app.chat_history[-1].is_new)
            self.assertTrue(visible_widget.has_class("new-message"))

            app._accepted_send("local message")
            outgoing_widget = list(app.query(ChatEntryWidget))[-1]
            self.assertTrue(app.chat_history[-1].outgoing)
            self.assertFalse(app.chat_history[-1].is_new)
            self.assertFalse(outgoing_widget.has_class("new-message"))

            app.show_tab("connection")
            self.assertFalse(app.chat_history[-2].is_new)

    async def test_terminal_cursor_guard_runs_for_app_lifecycle(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        cursor = Mock()
        app = MeshtasticPassApp(radio, self.settings, terminal_cursor=cursor)

        async with app.run_test(size=(100, 30)):
            cursor.hide.assert_called_once_with()
            cursor.restore.assert_not_called()

        cursor.restore.assert_called_once_with()

    async def test_real_callback_uses_new_state_and_radio_receive_time(self) -> None:
        radio = CallbackRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        packet = {
            "from": 0xABC12345,
            "fromId": "!abc12345",
            "channel": 0,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "text": "hello from real callback",
            },
            "rxTime": time() - 2 * 60 * 60,
            "rxRssi": -87,
            "rxSnr": 6.5,
            "id": 123456789,
        }

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._apply_color_theme("green")

            # Call the actual RadioService pubsub callback on the app thread.
            # The UI bridge must work from either SDK callback context.
            radio._on_text_received(packet=packet, interface=radio.interface)
            await pilot.pause()

            entry = app.chat_history[-1]
            widget = list(app.query(ChatEntryWidget))[-1]
            parts = (
                widget.query_one(".chat-entry-author", Static),
                widget.timestamp_label,
                widget.message_label,
            )
            self.assertTrue(entry.is_new)
            self.assertFalse(entry.unread)
            self.assertEqual(app.unread_count, 0)
            self.assertNotEqual(entry.radio_rx_at, entry.received_at)
            self.assertIsNone(entry.local_sent_at)
            self.assertEqual(str(widget.timestamp_label.render()), "2h")
            for part in parts:
                self.assertEqual(part.visual_style.foreground.hex6, "#FF8C00")

            expected_new_colors = {
                "white": "#39FF14",
                "green": "#FF8C00",
                "orange": "#D8D8D8",
            }
            for theme, expected in expected_new_colors.items():
                app._apply_color_theme(theme)
                await pilot.pause()
                for part in parts:
                    self.assertEqual(part.visual_style.foreground.hex6, expected)

            app.show_tab("profile")
            self.assertFalse(entry.is_new)
            app.show_tab("chat")
            await pilot.pause()
            self.assertFalse(widget.has_class("new-message"))

            app.show_tab("profile")
            radio._on_text_received(packet=packet, interface=radio.interface)
            await pilot.pause()
            self.assertTrue(app.chat_history[-1].is_new)
            self.assertTrue(app.chat_history[-1].unread)
            self.assertEqual(app.unread_count, 1)

    async def test_persisted_history_reloads_read_with_delivery_state(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        store = ChatStore.open(self.chat_db_path)
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)

        async with app.run_test(size=(100, 30)) as pilot:
            app._accept_received_message(SIMULATED_MESSAGES[0])
            app._accepted_send("persist me")
            await pilot.pause()

        reopened = ChatStore.open(self.chat_db_path)
        second_radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        second_app = MeshtasticPassApp(
            second_radio,
            self.settings,
            chat_store=reopened,
        )
        async with second_app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(
                [entry.text for entry in second_app.chat_history],
                [SIMULATED_MESSAGES[0].text, "persist me"],
            )
            self.assertTrue(
                all(
                    not entry.unread and not entry.is_new
                    for entry in second_app.chat_history
                )
            )
            self.assertEqual(
                second_app.chat_history[-1].delivery_state,
                DeliveryState.SENT,
            )
            self.assertEqual(second_app.unread_count, 0)

    async def test_smart_scroll_and_end_newest_indicator(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(80, 18)) as pilot:
            app.show_tab("chat")
            for index in range(20):
                app._accept_received_message(
                    replace(
                        SIMULATED_MESSAGES[0],
                        packet_id=9000 + index,
                        text=f"scroll message {index}",
                    )
                )
            await pilot.pause()
            transcript = app.query_one("#chat-log", ChatTranscript)
            self.assertEqual(transcript.scroll_y, transcript.max_scroll_y)

            transcript.scroll_to(y=0, animate=False)
            await pilot.pause()
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    packet_id=9999,
                    text="below viewport",
                )
            )
            await pilot.pause()
            self.assertLess(transcript.scroll_y, transcript.max_scroll_y)
            self.assertEqual(app.transcript_new_count, 1)
            self.assertEqual(
                str(app.query_one("#chat-new-below", Static).render()),
                "↓ 1 NEW",
            )
            self.assertEqual(app.unread_count, 0)

            await pilot.press("escape", "end")
            await pilot.pause()
            self.assertEqual(transcript.scroll_y, transcript.max_scroll_y)
            self.assertEqual(app.transcript_new_count, 0)

    async def test_outgoing_states_timeout_failure_and_manual_rebroadcast(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
            send_outcomes=(
                SimulatedSendOutcome.UNCONFIRMED,
                SimulatedSendOutcome.SENT,
                SimulatedSendOutcome.FAILED,
            ),
        )
        store = ChatStore.open(self.chat_db_path)
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)

        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            immediate = app._start_outgoing("manual retry")
            self.assertEqual(immediate.delivery_state, DeliveryState.SENDING)
            immediate_widget = list(app.query(ChatEntryWidget))[-1]
            await pilot.pause()
            self.assertEqual(
                str(immediate_widget.delivery_label.render()),
                "SENDING.",
            )
            self.assertEqual(
                [
                    str(child.render())
                    for child in immediate_widget.query(".chat-entry-header Static")
                ],
                ["YOU", " / ", "SENDING.", " / ", "0s"],
            )
            app._advance_delivery_states()
            self.assertEqual(
                str(immediate_widget.delivery_label.render()),
                "SENDING..",
            )
            self.assertIsNotNone(app._delivery_timer)
            self.assertEqual(
                len([timer for timer in app._timers if timer.name == "chat-delivery-states"]),
                1,
            )
            app.run_worker(lambda: app._send_from_thread(immediate), thread=True)
            for _ in range(5):
                await pilot.pause()
                if immediate.delivery_state is DeliveryState.UNCONFIRMED:
                    break
            self.assertEqual(immediate.delivery_state, DeliveryState.UNCONFIRMED)

            widget = list(app.query(ChatEntryWidget))[-1]
            await pilot.press("shift+tab")
            self.assertIs(app.focused, widget)
            await pilot.press("r")
            for _ in range(5):
                await pilot.pause()
                if immediate.delivery_state is DeliveryState.SENT:
                    break
            self.assertEqual(immediate.delivery_state, DeliveryState.SENT)
            self.assertEqual(len(radio.sent_messages), 2)
            self.assertEqual(len(app.chat_history), 1)

            failed = app._start_outgoing("definite failure")
            app.run_worker(lambda: app._send_from_thread(failed), thread=True)
            for _ in range(5):
                await pilot.pause()
                if failed.delivery_state is DeliveryState.FAILED:
                    break
            self.assertEqual(failed.delivery_state, DeliveryState.FAILED)

            immediate.delivery_state = DeliveryState.SENT
            immediate.confirmation_deadline = monotonic() - 1
            before = len(radio.sent_messages)
            app._advance_delivery_states()
            self.assertEqual(immediate.delivery_state, DeliveryState.UNCONFIRMED)
            self.assertEqual(len(radio.sent_messages), before)

        reopened = ChatStore.open(self.chat_db_path)
        self.addCleanup(reopened.close)
        stored_messages = reopened.load_recent()
        self.assertEqual(len(stored_messages), 2)
        self.assertEqual(stored_messages[0].delivery_state, "UNCONFIRMED")
        self.assertEqual(stored_messages[1].delivery_state, "FAILED")
        self.assertEqual(
            len(reopened.load_send_attempts(stored_messages[0].id)),
            2,
        )


if __name__ == "__main__":
    unittest.main()
