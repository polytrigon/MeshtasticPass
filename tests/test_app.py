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

from rich.color import Color
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.events import MouseScrollDown
from textual.widgets import Input, Static

from app import (
    ConnectionPage,
    ChatTranscript,
    ChatEntryWidget,
    ColorSelector,
    DeviceSelector,
    EndOfChatHistoryMarker,
    FontSizeSelector,
    LoadOlderControl,
    LongNameControl,
    MeshtasticPassApp,
    ThinScrollBarRender,
)
from app_controller import received_chat_entry
from app_settings import AppSettings
from chat_store import ChatStore
from geo import format_distance_miles
from radio_service import (
    DeliveryState,
    RadioEvent,
    RadioInfo,
    RadioIdentityError,
    RadioService,
    RadioState,
)
from simulated_radio_service import (
    SIMULATED_MESSAGES,
    SimulatedRadioService,
    SimulatedSendOutcome,
)
from theme_palette import THEME_PALETTES


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
            sendText=Mock(),
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
            expected = sorted(
                primary_messages,
                key=lambda message: message.radio_rx_at or 0,
            )
            self.assertEqual(app.chat_history[0].author, "Bob Basecamp")
            self.assertEqual(
                [entry.text for entry in app.chat_history],
                [message.text for message in expected],
            )
            self.assertEqual(
                [entry.radio_rx_at for entry in app.chat_history],
                [message.radio_rx_at for message in expected],
            )
            self.assertTrue(
                all(
                    entry.radio_rx_at != entry.app_received_at
                    for entry in app.chat_history
                )
            )
            self.assertEqual(app.unread_count, len(SIMULATED_MESSAGES))
            self.assertIn(
                f"CHAT({len(SIMULATED_MESSAGES)})",
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

            await pilot.press("enter", "down", "enter")
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
            status = app.query_one("#connection-status", Static)
            unavailable = app.query_one(
                "#identity-long-name-unavailable", Static
            )
            connecting = str(status.render())
            self.assertIn("STATUS       CONNECTING.", connecting)
            self.assertEqual(str(unavailable.render()), "...")
            self.assertIn("NODES        ...", str(details.render()))
            self.assertIn("SHORT NAME   ...", str(app.query_one("#identity-values", Static).render()))
            self.assertIn(
                "/dev/ttyUSB0",
                str(app.query_one("#device-selector", Static).render()),
            )

            app._show_connection(RadioState.ONLINE, info)
            online = str(details.render())
            self.assertIn("STATUS       CONNECTED", str(status.render()))
            self.assertIn("NODES        100", online)
            identity = str(app.query_one("#identity-values", Static).render())
            self.assertIn("SHORT NAME   9a3c", identity)
            self.assertIn("NODE ID      !433a9a3c", identity)
            self.assertEqual(
                app.query_one("#long-name-input", Input).value,
                "@Polytrigon",
            )
            self.assertFalse(unavailable.display)

            app._show_connection(RadioState.OFFLINE, info)
            offline = str(details.render())
            self.assertIn("STATUS       OFFLINE — RETRYING.", str(status.render()))
            self.assertNotIn("!433a9a3c", offline)
            self.assertNotIn("FIRMWARE", offline)
            self.assertEqual(str(unavailable.render()), "—")
            self.assertIn("NODE ID      —", str(app.query_one("#identity-values", Static).render()))

            app._show_connection(RadioState.ERROR, info, "raw SDK exception")
            error = str(details.render())
            self.assertIn(
                "STATUS       CONNECTION ERROR — RETRYING.",
                str(status.render()),
            )
            self.assertNotIn("!433a9a3c", error)
            self.assertNotIn(
                "raw SDK exception",
                str(app.query_one("#connection-error", Static).render()),
            )
            self.assertEqual(str(unavailable.render()), "—")

            app._show_connection(RadioState.CONNECTING, info)
            self.assertEqual(str(unavailable.render()), "...")
            self.assertNotIn(
                "@Polytrigon",
                str(app.query_one("#identity-values", Static).render()),
            )
            for name, palette in THEME_PALETTES.items():
                app._apply_color_theme(name)
                self.assertEqual(
                    unavailable.visual_style.foreground.hex6,
                    palette.dim_base,
                )

            radio.set_long_name = Mock()
            app.save_long_name(SimpleNamespace(value="stale name"))
            radio.set_long_name.assert_not_called()
            self.assertIn(
                "RADIO NOT CONNECTED",
                str(app.query_one("#identity-status", Static).render()),
            )

    async def test_identity_edit_success_failure_and_protocol_validation(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            editor = app.query_one("#long-name-input", Input)
            control = app.query_one(LongNameControl)
            identity = app.query_one("#identity-values", Static)
            status = app.query_one("#identity-status", Static)
            self.assertEqual(editor.value, "Simulated Node")
            self.assertTrue(editor.disabled)
            self.assertFalse(control.disabled)
            self.assertIn("SHORT NAME   SIM", str(identity.render()))
            self.assertIn("NODE ID      !51a00001", str(identity.render()))

            control.focus()
            await pilot.press("enter")
            self.assertTrue(control.editing)
            self.assertIs(app.focused, editor)
            self.assertFalse(editor.disabled)
            editor.value = "Clockwork Nomad"
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
                if "NAME SAVED" in str(status.render()):
                    break
            self.assertEqual(radio.info.long_name, "Clockwork Nomad")
            self.assertEqual(editor.value, "Clockwork Nomad")
            self.assertFalse(control.editing)
            self.assertTrue(editor.disabled)
            self.assertIs(app.focused, control)
            self.assertIn("NAME SAVED", str(status.render()))
            self.assertEqual(status.visual_style.foreground.hex6, "#39FF14")

            radio.set_long_name = Mock(
                side_effect=RadioIdentityError("simulated identity failure")
            )
            await pilot.press("enter")
            editor.value = "Broken Name"
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
                if "simulated identity failure" in str(status.render()):
                    break
            self.assertIn("simulated identity failure", str(status.render()))
            self.assertEqual(status.visual_style.foreground.hex6, "#FF1744")
            self.assertEqual(editor.value, "Clockwork Nomad")
            self.assertIs(app.focused, control)

            await pilot.press("enter")
            editor.value = "🚲" * 10
            radio.set_long_name = SimulatedRadioService.set_long_name.__get__(radio)
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
                if "39 UTF-8 bytes" in str(status.render()):
                    break
            self.assertIn("39 UTF-8 bytes", str(status.render()))
            self.assertEqual(editor.value, "Clockwork Nomad")
            self.assertIs(app.focused, control)

            app._show_connection(RadioState.OFFLINE)
            self.assertTrue(editor.disabled)
            self.assertEqual(editor.value, "")
            self.assertIn("NODE ID      —", str(identity.render()))

    async def test_long_name_navigation_edit_cancel_save_and_arrow_ownership(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(62, 18)) as pilot:
            await pilot.pause()
            control = app.query_one(LongNameControl)
            editor = control.editor
            font = app.query_one(FontSizeSelector)

            control.focus()
            original = editor.value
            original_cursor = editor.cursor_position
            await pilot.press("left", "right")
            self.assertIs(app.focused, control)
            self.assertEqual(editor.cursor_position, original_cursor)

            await pilot.press("down")
            self.assertIs(app.focused, font)
            await pilot.press("up", "enter")
            self.assertIs(app.focused, editor)
            self.assertTrue(control.editing)
            self.assertFalse(editor.disabled)

            editor.value = "Cancel Me"
            editor.cursor_position = len(editor.value)
            await pilot.press("left")
            self.assertEqual(editor.cursor_position, len("Cancel Me") - 1)
            await pilot.press("down")
            self.assertIs(app.focused, editor)

            await pilot.press("escape")
            self.assertFalse(control.editing)
            self.assertTrue(editor.disabled)
            self.assertIs(app.focused, control)
            self.assertEqual(editor.value, original)

            await pilot.press("enter")
            editor.value = "Saved Name"
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
                if radio.info.long_name == "Saved Name":
                    break
            self.assertEqual(radio.info.long_name, "Saved Name")
            self.assertEqual(editor.value, "Saved Name")
            self.assertFalse(control.editing)
            self.assertIs(app.focused, control)

    async def test_long_name_field_resizes_without_horizontal_page_scroll(self) -> None:
        app = MeshtasticPassApp(
            SimulatedRadioService(
                connect_delay=0,
                message_interval=0,
                scripted_messages=(),
            ),
            self.settings,
        )
        async with app.run_test(size=(35, 18)) as pilot:
            await pilot.pause()
            page = app.query_one(ConnectionPage)
            control = app.query_one(LongNameControl)
            editor = control.editor
            control.focus()
            await pilot.press("enter")

            editor.value = "A"
            await pilot.pause()
            minimum_width = editor.region.width
            self.assertGreaterEqual(minimum_width, 8)

            editor.value = "A" * 39
            await pilot.pause()
            self.assertGreater(editor.region.width, minimum_width)
            self.assertLessEqual(editor.region.width, 39)
            self.assertLessEqual(editor.region.right, page.region.right)
            self.assertFalse(page.allow_horizontal_scroll)

    async def test_connection_status_animation_reuses_one_timer(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=10,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        info = radio.info

        async with app.run_test(size=(100, 30)) as pilot:
            details = app.query_one("#connection-status", Static)
            timer = app._connection_animation_timer
            self.assertIsNotNone(timer)

            self.assertIn("STATUS       CONNECTING.", str(details.render()))
            await pilot.pause(0.5)
            self.assertIn("STATUS       CONNECTING..", str(details.render()))
            app._advance_connection_animation()
            self.assertIn("STATUS       CONNECTING...", str(details.render()))
            app._advance_connection_animation()
            self.assertIn("STATUS       CONNECTING.", str(details.render()))

            app._show_connection(RadioState.OFFLINE)
            app._advance_connection_animation()
            self.assertIn("STATUS       OFFLINE — RETRYING..", str(details.render()))

            app._show_connection(RadioState.ERROR)
            app._advance_connection_animation()
            self.assertIn(
                "STATUS       CONNECTION ERROR — RETRYING..",
                str(details.render()),
            )

            app._show_connection(RadioState.ONLINE, info)
            online = str(details.render())
            app._advance_connection_animation()
            self.assertEqual(str(details.render()), online)
            self.assertIn("STATUS       CONNECTED", online)
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
            await pilot.press("enter", "down", "enter")
            await pilot.pause()

            self.assertEqual(color_selector.color, "green")
            self.assertEqual(self.settings.color, "green")
            self.assertTrue(app.screen.has_class("theme-green"))
            self.assertFalse(app.screen.has_class("theme-white"))

            await pilot.press("enter", "down", "enter")
            await pilot.pause()
            self.assertEqual(color_selector.color, "orange")
            self.assertTrue(app.screen.has_class("theme-orange"))

            await pilot.press("up", "enter", "down", "enter")
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

            await pilot.press("down", "enter", "down", "enter")
            await pilot.pause()
            self.assertIn("COLOR SAVED", str(status.render()))
            self.assertEqual(status.visual_style.foreground.hex6, "#39FF14")

            await pilot.press("enter", "down", "enter")
            await pilot.pause()
            self.assertEqual(status.visual_style.foreground.hex6, "#FF8C00")

            await pilot.press("enter", "down", "enter")
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
            app.show_tab("chat")
            timestamp_timer = app._chat_timestamp_timer
            self.assertIsNotNone(timestamp_timer)
            self.assertEqual(timestamp_timer._interval, 1.0)

            before_incoming = time()
            app._accept_received_message(incoming)
            incoming_entry = app.chat_history[-1]
            self.assertGreaterEqual(
                incoming_entry.app_received_at,
                before_incoming,
            )
            self.assertEqual(
                incoming_entry.radio_rx_at,
                incoming.radio_rx_at,
            )
            self.assertIsNone(incoming_entry.local_sent_at)

            before_outgoing = time()
            app._accepted_send("local timestamp")
            outgoing_entry = app.chat_history[-1]
            self.assertGreaterEqual(
                outgoing_entry.app_received_at,
                before_outgoing,
            )
            self.assertIsNone(outgoing_entry.radio_rx_at)
            self.assertEqual(
                outgoing_entry.local_sent_at,
                outgoing_entry.app_received_at,
            )
            self.assertTrue(outgoing_entry.outgoing)

            await pilot.pause()
            widgets = list(app.query(ChatEntryWidget))
            self.assertEqual(len(widgets), 2)
            self.assertIs(app._chat_timestamp_timer, timestamp_timer)

            incoming_entry.age_reference = 1_000.0 - 8
            original_widget = widgets[0]
            for now, expected in (
                (1_000.0, "RX 8s"),
                (1_001.0, "RX 9s"),
                (1_002.0, "RX 10s"),
            ):
                app._refresh_chat_timestamps(now)
                await pilot.pause()
                self.assertEqual(
                    str(original_widget.timestamp_label.render()),
                    expected,
                )
                self.assertIs(list(app.query(ChatEntryWidget))[0], original_widget)

            app._refresh_chat_timestamps(incoming_entry.age_reference + 63 * 60)
            await pilot.pause()
            incoming_timestamp = widgets[0].query_one(
                ".chat-entry-timestamp", Static
            )
            self.assertEqual(str(incoming_timestamp.render()), "RX 1h 3m")
            self.assertGreaterEqual(incoming_timestamp.region.width, len("RX 1h 3m"))

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
            app._accepted_send("ordinary outgoing timestamp")
            app.show_tab("connection")
            await pilot.pause()
            timestamps = list(app.query(".chat-entry-timestamp"))
            error = app.query_one("#send-error", Static)

            expected_timestamp_colors = {
                name: palette.dim_base
                for name, palette in THEME_PALETTES.items()
            }
            for color, expected in expected_timestamp_colors.items():
                app._apply_color_theme(color)
                await pilot.pause()
                self.assertEqual(len(timestamps), 2)
                for timestamp in timestamps:
                    self.assertEqual(
                        timestamp.visual_style.foreground.hex6,
                        expected,
                    )
                self.assertEqual(error.visual_style.foreground.hex6, "#FF1744")

    async def test_truthful_origin_and_receive_age_labels(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            receive_only = replace(
                SIMULATED_MESSAGES[0],
                origin_sent_at=None,
                radio_rx_at=1_700_000_000.0 - 8,
            )
            receive_entry = received_chat_entry(
                receive_only,
                app_received_at=1_700_000_000.0,
                monotonic_now=1_000.0,
            )
            receive_widget = ChatEntryWidget(receive_entry, now=1_000.0)
            self.assertEqual(str(receive_widget.timestamp_label.render()), "RX 8s")
            self.assertNotIn("DELAYED", str(receive_widget.timestamp_label.render()))

            origin_entry = received_chat_entry(
                replace(
                    receive_only,
                    origin_sent_at=1_700_000_000.0 - 24 * 60 * 60,
                    radio_rx_at=1_700_000_000.0 - 2,
                ),
                app_received_at=1_700_000_000.0,
                monotonic_now=1_000.0,
            )
            origin_widget = ChatEntryWidget(origin_entry, now=1_000.0)
            self.assertEqual(str(origin_widget.timestamp_label.render()), "1d")
            self.assertNotIn("RX", str(origin_widget.timestamp_label.render()))

            app._accepted_send("outgoing age")
            await pilot.pause()
            outgoing = list(app.query(ChatEntryWidget))[-1]
            self.assertEqual(str(outgoing.timestamp_label.render()), "0s")

    async def test_scrollbar_follows_active_theme(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(80, 18)) as pilot:
            transcript = app.query_one("#chat-log", ChatTranscript)
            connection = app.query_one("#connection", ConnectionPage)
            palettes = {
                name: (palette.base, palette.dim_base)
                for name, palette in THEME_PALETTES.items()
            }
            for theme, (thumb, track) in palettes.items():
                app._apply_color_theme(theme)
                await pilot.pause()
                self.assertEqual(transcript.styles.scrollbar_size_vertical, 1)
                self.assertEqual(transcript.styles.scrollbar_color.hex, thumb)
                self.assertEqual(
                    transcript.styles.scrollbar_background.hex,
                    track,
                )
                self.assertEqual(connection.styles.scrollbar_color.hex, thumb)
                self.assertEqual(
                    connection.styles.scrollbar_background.hex,
                    track,
                )
            self.assertIs(transcript.vertical_scrollbar.renderer, ThinScrollBarRender)
            self.assertIs(connection.vertical_scrollbar.renderer, ThinScrollBarRender)

    async def test_connection_page_scrolls_and_controls_work_at_short_height(self) -> None:
        app = MeshtasticPassApp(
            SimulatedRadioService(
                connect_delay=0,
                message_interval=0,
                scripted_messages=(),
            ),
            self.settings,
        )
        async with app.run_test(size=(62, 10)) as pilot:
            await pilot.pause()
            page = app.query_one("#connection", ConnectionPage)
            self.assertGreater(page.max_scroll_y, 0)
            self.assertTrue(page.allow_vertical_scroll)

            font = app.query_one(FontSizeSelector)
            color = app.query_one(ColorSelector)
            font.focus()
            await pilot.press("down")
            await pilot.pause()
            self.assertIs(app.focused, color)
            self.assertGreater(page.scroll_y, 0)
            await pilot.press("enter", "down", "enter")
            self.assertEqual(color.color, "green")

            page.scroll_to(y=0, animate=False)
            await pilot.pause()
            page._on_mouse_scroll_down(
                MouseScrollDown(page, 1, 1, 0, 1, 0, False, False, False)
            )
            await pilot.pause()
            self.assertGreater(page.scroll_y, 0)

            editor = app.query_one("#long-name-input", Input)
            control = app.query_one(LongNameControl)
            self.assertTrue(editor.disabled)
            control.focus()
            control.scroll_visible(animate=False)
            await pilot.pause()
            self.assertGreaterEqual(control.region.y, page.region.y)
            self.assertLess(control.region.y, page.region.bottom)

    def test_thin_scrollbar_uses_one_cell_mouse_draggable_thumb(self) -> None:
        rendered = ThinScrollBarRender.render_bar(
            size=10,
            virtual_size=30,
            window_size=10,
            position=5,
            thickness=1,
            vertical=True,
            back_color=Color.parse(THEME_PALETTES["white"].dim_base),
            bar_color=Color.parse("#39ff14"),
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
        self.assertTrue(all(segment.text == "▕" for segment in thumb_segments))
        self.assertTrue(all(segment.text == "▕" for segment in track_segments))
        self.assertTrue(all(segment.cell_length == 1 for segment in thumb_segments))
        self.assertTrue(all(segment.cell_length == 1 for segment in track_segments))
        self.assertTrue(
            all(segment.style.color.name == "#39ff14" for segment in thumb_segments)
        )
        self.assertTrue(
            all(
                segment.style.color.name
                == THEME_PALETTES["white"].dim_base.lower()
                for segment in track_segments
            )
        )

    async def test_chat_content_has_gutter_before_far_right_scrollbar(self) -> None:
        app = MeshtasticPassApp(
            SimulatedRadioService(connect_delay=0, message_interval=0, scripted_messages=()),
            self.settings,
        )
        async with app.run_test(size=(42, 18)) as pilot:
            app.show_tab("chat")
            app._accept_received_message(
                replace(SIMULATED_MESSAGES[0], text="wrapped message " * 10)
            )
            await pilot.pause()
            transcript = app.query_one(ChatTranscript)
            widget = app.query_one(ChatEntryWidget)
            content = widget.query_one(".chat-entry-content")
            scrollbar = transcript.vertical_scrollbar
            self.assertEqual(widget.styles.padding.right, 1)
            self.assertEqual(transcript.styles.scrollbar_size_vertical, 1)
            self.assertLess(content.region.right, transcript.region.right)
            self.assertGreater(widget.region.height, 2)

    async def test_middle_dot_heading_no_end_control_and_f4_quit_last(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)
        exit_mock = Mock()
        app.exit = exit_mock

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            heading = str(app.query_one("#chat-title", Static).render())
            self.assertIn("CHAT · [ LongFast ▾ ] · ACTIVE", heading)
            self.assertNotIn("CHAT —", heading)
            with self.assertRaises(NoMatches):
                app.query_one("#end-of-chat")

            await pilot.press("q")
            exit_mock.assert_not_called()
            footer = str(app.query_one("#footer", Static).render())
            self.assertTrue(footer.endswith("F4 quit"))
            self.assertNotIn("Q quit", footer)

            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            await pilot.press("q")
            self.assertEqual(chat_input.value, "q")
            app.query_one(ChatTranscript).focus()
            await pilot.pause()
            app._update_footer()
            footer = str(app.query_one("#footer", Static).render())
            self.assertTrue(footer.endswith("F4 quit"))
            self.assertNotIn("END newest", footer)
            self.assertNotIn("RIGHT newest", footer)
            self.assertNotIn("→ newest", footer)

            app.show_tab("connection")
            device = app.query_one(DeviceSelector)
            device.focus()
            await pilot.press("enter", "f4")
            exit_mock.assert_called_once_with()

    async def test_f4_exit_runs_normal_cursor_cleanup(self) -> None:
        cursor = Mock()
        app = MeshtasticPassApp(
            SimulatedRadioService(connect_delay=0, message_interval=0, scripted_messages=()),
            self.settings,
            terminal_cursor=cursor,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("f4")
            await pilot.pause()

        cursor.hide.assert_called_once_with()
        cursor.restore.assert_called_once_with()

    async def test_active_heading_ages_and_nodes_remain_total_database_count(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            reference = radio._activity_reference_time
            self.assertIsNotNone(reference)
            app._refresh_chat_timestamps(wall_now=reference)
            self.assertIn(
                "CHAT · [ LongFast ▾ ] · ACTIVE 2",
                str(app.query_one("#chat-title", Static).render()),
            )
            self.assertIn(
                "NODES        8",
                str(app.query_one("#connection-details", Static).render()),
            )

            app._refresh_chat_timestamps(wall_now=reference + 1)
            self.assertIn(
                "ACTIVE 1",
                str(app.query_one("#chat-title", Static).render()),
            )
            self.assertEqual(radio.sent_messages, ())

            app._refresh_chat_timestamps(wall_now=reference + 400)
            self.assertIn(
                "ACTIVE 0",
                str(app.query_one("#chat-title", Static).render()),
            )

            app._show_connection(RadioState.OFFLINE)
            self.assertIn(
                "ACTIVE —",
                str(app.query_one("#chat-title", Static).render()),
            )

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
            distance = widget.query_one(".chat-entry-distance", Static)

            self.assertIn("CHAT · [ LongFast ▾ ]", str(heading.render()))
            self.assertNotIn("—", str(heading.render()))
            self.assertIs(author.parent, timestamp.parent)
            self.assertIs(author.parent, distance.parent)
            self.assertIsInstance(author.parent, Horizontal)
            self.assertEqual(author.region.y, timestamp.region.y)
            self.assertEqual(author.region.y, distance.region.y)
            self.assertEqual(
                str(distance.render()),
                format_distance_miles(app.chat_history[0].distance_miles),
            )
            header_text = [
                str(part.render())
                for part in widget.query(".chat-entry-header Static")
            ]
            self.assertEqual(header_text[0], "Alice Trail")
            self.assertEqual(header_text[1], " / ")
            self.assertEqual(header_text[3], " / ")
            self.assertTrue(header_text[4].endswith("miles"))
            self.assertTrue(author.visual_style.bold)
            self.assertFalse(bool(timestamp.visual_style.bold))
            self.assertTrue(timestamp.visual_style.dim)
            self.assertTrue(distance.visual_style.dim)

            app._accept_received_message(SIMULATED_MESSAGES[1])
            await pilot.pause()
            without_position = next(
                widget
                for widget in app.query(ChatEntryWidget)
                if widget.entry.node_id == SIMULATED_MESSAGES[1].sender_node_id
            )
            self.assertIsNone(without_position.distance_label)
            self.assertEqual(
                len(list(without_position.query(".chat-entry-header Static"))),
                3,
            )

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
            unread_distance = unread_widget.distance_label
            unread_body = unread_widget.message_label
            self.assertIsNotNone(unread_distance)
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
                for part in (
                    unread_author,
                    unread_timestamp,
                    unread_distance,
                    unread_body,
                ):
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
            self.assertEqual(
                unread_timestamp.visual_style.foreground.hex6,
                THEME_PALETTES["orange"].dim_base,
            )
            self.assertEqual(
                unread_distance.visual_style.foreground.hex6,
                THEME_PALETTES["orange"].dim_base,
            )
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
            self.assertIsNone(outgoing_widget.distance_label)

            app.show_tab("connection")
            self.assertFalse(app.chat_history[-2].is_new)

    async def test_visible_delivery_states_and_theme_colors(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            app._accepted_send("visible states")
            entry = app.chat_history[-1]
            widget = list(app.query(ChatEntryWidget))[-1]
            self.assertIs(entry.delivery_state, DeliveryState.SENT)
            self.assertIn(DeliveryState.SENT, DeliveryState)

            palettes = {
                "white": ("#39FF14", "#D8D8D8"),
                "green": ("#FF8C00", "#39FF14"),
                "orange": ("#D8D8D8", "#FF8C00"),
            }
            for theme, (accent, base) in palettes.items():
                app._apply_color_theme(theme)

                entry.delivery_state = DeliveryState.SENDING
                widget.refresh_delivery_state(1)
                await pilot.pause()
                self.assertIn(
                    str(widget.delivery_label.render()),
                    ("SENDING.", "SENDING..", "SENDING..."),
                )
                self.assertEqual(widget.delivery_label.visual_style.foreground.hex6, accent)

                entry.delivery_state = DeliveryState.SENT
                widget.refresh_delivery_state(2)
                await pilot.pause()
                self.assertIn(
                    str(widget.delivery_label.render()),
                    ("SENDING.", "SENDING..", "SENDING..."),
                )
                self.assertTrue(widget.has_class("delivery-sending"))
                self.assertFalse(widget.has_class("delivery-sent"))
                self.assertEqual(widget.delivery_label.visual_style.foreground.hex6, accent)

                entry.delivery_state = DeliveryState.HEARD
                widget.refresh_delivery_state(1)
                await pilot.pause()
                self.assertEqual(str(widget.delivery_label.render()), "HEARD")
                self.assertEqual(widget.delivery_label.visual_style.foreground.hex6, base)

                entry.delivery_state = DeliveryState.UNCONFIRMED
                widget.refresh_delivery_state(1)
                await pilot.pause()
                self.assertEqual(
                    str(widget.delivery_label.render()),
                    "UNCONFIRMED",
                )
                self.assertEqual(widget.delivery_label.visual_style.foreground.hex6, accent)

                entry.delivery_state = DeliveryState.FAILED
                widget.refresh_delivery_state(1)
                await pilot.pause()
                self.assertEqual(str(widget.delivery_label.render()), "FAILED")
                self.assertEqual(
                    widget.delivery_label.visual_style.foreground.hex6,
                    "#FF1744",
                )

            entry.delivery_state = DeliveryState.SENT
            widget.refresh_delivery_state(1)
            app._advance_delivery_states()
            self.assertIn(
                str(widget.delivery_label.render()),
                ("SENDING.", "SENDING..", "SENDING..."),
            )
            self.assertIsNone(widget.distance_label)

    async def test_delivery_label_reflows_between_visible_states(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings)

        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            app._accepted_send("delivery layout")
            entry = app.chat_history[-1]
            widget = list(app.query(ChatEntryWidget))[-1]
            label = widget.delivery_label
            self.assertIsNotNone(label)

            transitions = (
                (DeliveryState.SENDING, 3, "SENDING..."),
                (DeliveryState.HEARD, 1, "HEARD"),
                (DeliveryState.FAILED, 1, "FAILED"),
                (DeliveryState.UNCONFIRMED, 1, "UNCONFIRMED"),
            )
            widths = []
            for state, dot_count, expected in transitions:
                entry.delivery_state = state
                widget.refresh_delivery_state(dot_count)
                await pilot.pause()

                rendered = str(label.render())
                self.assertEqual(rendered, expected)
                self.assertEqual(label.region.width, len(expected))
                self.assertNotIn(" ", rendered)
                widths.append(label.region.width)

            self.assertLess(widths[1], widths[0])
            self.assertGreater(widths[2], widths[1])
            self.assertGreater(widths[3], widths[2])

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
            self.assertNotEqual(entry.radio_rx_at, entry.app_received_at)
            self.assertIsNone(entry.origin_sent_at)
            self.assertIsNone(entry.local_sent_at)
            self.assertEqual(str(widget.timestamp_label.render()), "RX 2h")
            self.assertNotIn("DELAYED", str(widget.timestamp_label.render()))
            self.assertIn(
                "ACTIVE 1",
                str(app.query_one("#chat-title", Static).render()),
            )
            radio.interface.sendText.assert_not_called()
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

    async def test_history_pagination_is_bounded_and_preserves_ui_state(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        for index in range(225):
            store.add_incoming(
                packet_id=50_000 + index,
                node_id="!a11ce001",
                sender_name="Alice Trail",
                sender_short_name="ALCE",
                channel_index=0,
                text=f"history {index}",
                radio_rx_at=1_700_000_000.0 + index,
                received_at=1_700_000_002.0 + index,
            )
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)

        async with app.run_test(size=(80, 18)) as pilot:
            app.show_tab("chat")
            await pilot.pause()
            transcript = app.query_one("#chat-log", ChatTranscript)
            control = app.query_one("#load-older", LoadOlderControl)
            self.assertEqual(len(app.query(EndOfChatHistoryMarker)), 0)
            self.assertTrue(control.can_focus)
            self.assertEqual(len(app.chat_history), 100)
            self.assertEqual(app.chat_history[0].text, "history 125")
            self.assertEqual(app.chat_history[-1].text, "history 224")
            self.assertEqual(transcript.scroll_y, transcript.max_scroll_y)

            transcript.scroll_to(y=20, animate=False)
            await pilot.pause()
            anchor = list(app.query(ChatEntryWidget))[20]
            anchor_y = anchor.region.y
            unread_before = app.unread_count
            below_before = app.transcript_new_count

            await app.load_older_chat_history(LoadOlderControl.Activated())
            for _ in range(5):
                await pilot.pause()
                if abs(anchor.region.y - anchor_y) <= 1:
                    break
            self.assertEqual(len(app.chat_history), 150)
            self.assertEqual(app.chat_history[0].text, "history 75")
            self.assertAlmostEqual(anchor.region.y, anchor_y, delta=1)
            self.assertTrue(all(not entry.unread and not entry.is_new for entry in app.chat_history[:50]))
            self.assertEqual(app.unread_count, unread_before)
            self.assertEqual(app.transcript_new_count, below_before)

            control.focus()
            await pilot.press("enter")
            for _ in range(5):
                await pilot.pause()
                if len(app.chat_history) == 200:
                    break
            self.assertEqual(len(app.chat_history), 200)
            self.assertEqual(app.chat_history[0].text, "history 25")
            self.assertIs(app.query_one("#load-older", LoadOlderControl), control)

            transcript.scroll_to(y=30, animate=False)
            await pilot.pause()
            final_anchor = list(app.query(ChatEntryWidget))[20]
            final_anchor_y = final_anchor.region.y
            await app.load_older_chat_history(LoadOlderControl.Activated())
            for _ in range(5):
                await pilot.pause()
                if abs(final_anchor.region.y - final_anchor_y) <= 1:
                    break
            self.assertEqual(len(app.chat_history), 225)
            self.assertEqual(app.chat_history[0].text, "history 0")
            self.assertEqual(len(app.query(LoadOlderControl)), 0)
            marker = app.query_one(EndOfChatHistoryMarker)
            self.assertFalse(marker.can_focus)
            self.assertEqual(str(marker.render()), "END OF CHAT HISTORY")
            self.assertAlmostEqual(final_anchor.region.y, final_anchor_y, delta=1)
            self.assertNotIn(marker, app._chat_navigation_targets())

    async def test_current_session_new_messages_are_not_capped(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)

        async with app.run_test(size=(80, 18)) as pilot:
            app.show_tab("profile")
            for index in range(105):
                app._accept_received_message(
                    replace(
                        SIMULATED_MESSAGES[0],
                        packet_id=80_000 + index,
                        text=f"session new {index}",
                    )
                )
            await pilot.pause()
            self.assertEqual(len(app.chat_history), 105)
            self.assertEqual(len(app.query(ChatEntryWidget)), 105)
            self.assertEqual(app.unread_count, 105)
            self.assertTrue(all(entry.is_new for entry in app.chat_history))

    async def test_one_new_message_keeps_default_window_at_100(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        for index in range(100):
            store.add_incoming(
                packet_id=90_000 + index,
                node_id="!a11ce001",
                sender_name="Alice Trail",
                sender_short_name="ALCE",
                channel_index=0,
                text=f"history {index}",
                radio_rx_at=1_700_000_000.0 + index,
                received_at=1_700_000_002.0 + index,
            )
        app = MeshtasticPassApp(
            SimulatedRadioService(
                connect_delay=0,
                message_interval=0,
                scripted_messages=(),
            ),
            self.settings,
            chat_store=store,
        )

        async with app.run_test(size=(80, 18)) as pilot:
            app.show_tab("profile")
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    packet_id=91_000,
                    text="new arrival",
                )
            )
            await pilot.pause()

            self.assertEqual(len(app.chat_history), 100)
            self.assertEqual(len(app.query(ChatEntryWidget)), 100)
            self.assertEqual(app.chat_history[0].text, "history 1")
            self.assertEqual(app.chat_history[-1].text, "new arrival")
            self.assertTrue(app.chat_history[-1].is_new)
            self.assertTrue(app.chat_history[-1].unread)
            self.assertEqual(app.unread_count, 1)
            self.assertEqual(len(store.load_recent(limit=200)), 101)
            self.assertEqual(len(app.query(LoadOlderControl)), 1)

    async def test_forty_new_messages_replace_oldest_mounted_history(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        for index in range(150):
            store.add_incoming(
                packet_id=92_000 + index,
                node_id="!a11ce001",
                sender_name="Alice Trail",
                sender_short_name="ALCE",
                channel_index=0,
                text=f"history {index}",
                radio_rx_at=1_700_000_000.0 + index,
                received_at=1_700_000_002.0 + index,
            )
        app = MeshtasticPassApp(
            SimulatedRadioService(
                connect_delay=0,
                message_interval=0,
                scripted_messages=(),
            ),
            self.settings,
            chat_store=store,
        )

        async with app.run_test(size=(80, 18)) as pilot:
            app.show_tab("profile")
            for index in range(40):
                app._accept_received_message(
                    replace(
                        SIMULATED_MESSAGES[0],
                        packet_id=93_000 + index,
                        text=f"session new {index}",
                    )
                )
            await pilot.pause()

            self.assertEqual(len(app.chat_history), 100)
            self.assertEqual(len(app.query(ChatEntryWidget)), 100)
            self.assertEqual(
                [entry.text for entry in app.chat_history[:60]],
                [f"history {index}" for index in range(90, 150)],
            )
            self.assertEqual(
                [entry.text for entry in app.chat_history[60:]],
                [f"session new {index}" for index in range(40)],
            )
            self.assertTrue(all(entry.is_new for entry in app.chat_history[60:]))
            self.assertTrue(all(entry.unread for entry in app.chat_history[60:]))
            self.assertEqual(app.unread_count, 40)
            self.assertEqual(len(store.load_recent(limit=250)), 190)

            await app.load_older_chat_history(LoadOlderControl.Activated())
            await pilot.pause()
            self.assertEqual(len(app.chat_history), 150)
            message_ids = [entry.message_id for entry in app.chat_history]
            self.assertEqual(len(message_ids), len(set(message_ids)))
            self.assertEqual(app.chat_history[0].text, "history 40")
            self.assertEqual(len(app.query(LoadOlderControl)), 1)
            self.assertEqual(app.unread_count, 40)
            self.assertTrue(all(entry.is_new for entry in app.chat_history[-40:]))

            await app.load_older_chat_history(LoadOlderControl.Activated())
            await pilot.pause()
            self.assertEqual(len(app.chat_history), 190)
            message_ids = [entry.message_id for entry in app.chat_history]
            self.assertEqual(len(message_ids), len(set(message_ids)))
            self.assertEqual(app.chat_history[0].text, "history 0")
            self.assertEqual(len(app.query(LoadOlderControl)), 0)
            self.assertEqual(len(app.query(EndOfChatHistoryMarker)), 1)

    async def test_passive_history_marker_is_dim_and_empty_chat_omits_it(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        store.add_incoming(
            packet_id=94_000,
            node_id="!a11ce001",
            sender_name="Alice Trail",
            sender_short_name="ALCE",
            channel_index=0,
            text="the only stored message",
            radio_rx_at=1_700_000_000.0,
            received_at=1_700_000_002.0,
        )
        app = MeshtasticPassApp(
            SimulatedRadioService(
                connect_delay=0,
                message_interval=0,
                scripted_messages=(),
            ),
            self.settings,
            chat_store=store,
        )
        async with app.run_test(size=(80, 18)) as pilot:
            await pilot.pause()
            marker = app.query_one(EndOfChatHistoryMarker)
            self.assertFalse(marker.can_focus)
            for name, palette in THEME_PALETTES.items():
                app._apply_color_theme(name)
                await pilot.pause()
                self.assertEqual(
                    marker.visual_style.foreground.hex6,
                    palette.dim_base,
                )
            targets = app._chat_navigation_targets()
            self.assertNotIn(marker, targets)
            self.assertTrue(all(target.can_focus for target in targets))

        empty_app = MeshtasticPassApp(
            SimulatedRadioService(
                connect_delay=0,
                message_interval=0,
                scripted_messages=(),
            ),
            self.settings,
            chat_store=ChatStore.open(Path(self.temporary_directory.name) / "empty.db"),
        )
        async with empty_app.run_test(size=(80, 18)) as pilot:
            await pilot.pause()
            self.assertEqual(len(empty_app.query(EndOfChatHistoryMarker)), 0)

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
            widget.focus()
            await pilot.press("down", "enter")
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
