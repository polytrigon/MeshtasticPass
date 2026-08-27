"""Regression coverage for dropdowns, channel CHAT, and contextual navigation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from textual.widgets import Input, Static

from app import (
    ChannelSelector,
    ChatEntryWidget,
    ColorSelector,
    DeviceSelector,
    FontSizeSelector,
    MessageActionControl,
    MeshtasticPassApp,
    can_manual_resend,
)
from app_controller import received_chat_entry
from app_settings import AppSettings
from chat_store import ChatStore
from keyboard_dropdown import DropdownOption, KeyboardDropdown
from radio_service import ChannelInfo, DeliveryState
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService


class MilestoneDropdownChannelTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_reusable_dropdown_select_cancel_and_dynamic_options(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 32)) as pilot:
            font = app.query_one(FontSizeSelector)
            color = app.query_one(ColorSelector)
            device = app.query_one(DeviceSelector)

            # A fresh install's default is XL (item 4: first-run default
            # text size), not MEDIUM -- see AppSettings.DEFAULT_FONT_SIZE.
            self.assertIn("[ XL ▾ ]", str(font.render()))
            await pilot.press("enter", "down", "escape")
            self.assertFalse(font.is_open)
            self.assertEqual(font.font_size, 18)

            await pilot.press("enter", "down", "enter")
            await pilot.pause()
            self.assertEqual(font.font_size, 22)
            self.assertFalse(font.is_open)

            self.assertEqual(
                [option.value for option in device.options],
                ["/dev/ttyUSB0", "/dev/ttyUSB1"],
            )
            device.set_options(())
            self.assertEqual(device.options, ())
            self.assertIn("/dev/ttyUSB0", str(device.render()))
            self.assertIsInstance(color, KeyboardDropdown)
            self.assertEqual(color._accent_color, "#39FF14")
            app._apply_color_theme("green")
            self.assertTrue(
                all(
                    dropdown._accent_color == "#FF8C00"
                    for dropdown in app.query(KeyboardDropdown)
                )
            )

    async def test_simulated_usb_switch_persists_and_reconnects_one_radio(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            selector = app.query_one(DeviceSelector)
            selector.focus()
            await pilot.press("enter", "down", "enter")
            for _ in range(5):
                await pilot.pause()
                if radio.info.device_path == "/dev/ttyUSB1":
                    break
            self.assertEqual(radio.device_path, "/dev/ttyUSB1")
            self.assertEqual(self.settings.device_path, "/dev/ttyUSB1")
            self.assertTrue(app._monitor.is_running)

        reloaded = AppSettings.load(config_path=self.settings.config_path)
        self.assertEqual(reloaded.device_path, "/dev/ttyUSB1")

    async def test_channel_dropdown_separates_transcripts_unread_and_sends(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        channel_zero = SIMULATED_MESSAGES[0]
        channel_one = SIMULATED_MESSAGES[2]

        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app._accept_received_message(channel_zero)
            app._accept_received_message(channel_one)
            self.assertEqual(app.unread_count, 2)

            app.show_tab("chat")
            self.assertEqual(app.unread_count, 1)
            self.assertEqual([entry.channel_index for entry in app.chat_history], [0])

            chat_input = app.query_one("#chat-input", Input)
            await pilot.press("escape", "c")
            selector = app.query_one(ChannelSelector)
            self.assertTrue(selector.is_open)
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertEqual(app.current_channel_index, 1)
            self.assertEqual([entry.channel_index for entry in app.chat_history], [1])
            self.assertEqual(app.unread_count, 0)
            self.assertIn("[ Hiking ▾ ]", str(selector.render()))

            app._accept_received_message(replace(channel_one, packet_id=9991))
            self.assertTrue(app.chat_history[-1].is_new)
            chat_input.focus()
            chat_input.value = "reply on hiking"
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(all(not entry.is_new for entry in app.chat_history))
            self.assertEqual(radio.sent_messages[-1].channel_index, 1)

            await app._switch_channel(0)
            self.assertEqual(app.chat_history[0].channel_index, 0)

    async def test_channel_switch_preserves_each_channels_own_draft(self) -> None:
        """Item 27: an unfinished LongFast draft must survive inspecting

        Hiking and switching back -- never lost, and never leaked into
        Hiking's own composer.
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            chat_input.value = "unfinished longfast thought"

            await app._switch_channel(1)
            self.assertEqual(chat_input.value, "")
            chat_input.value = "a different hiking draft"

            await app._switch_channel(0)
            self.assertEqual(chat_input.value, "unfinished longfast thought")
            self.assertEqual(chat_input.cursor_position, len(chat_input.value))

            await app._switch_channel(1)
            self.assertEqual(chat_input.value, "a different hiking draft")

    async def test_duplicate_channel_display_names_remain_independent(self) -> None:
        """Item F/24: identity is by stable channel INDEX, never display

        name -- two configured channels sharing a user-facing name must
        still keep completely separate history/drafts/unread state.
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._channels = (ChannelInfo(0, "Shared"), ChannelInfo(1, "Shared"))
            app.query_one(ChannelSelector).set_options(
                (DropdownOption(c.name, c.index) for c in app._channels),
                value=0,
            )
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            chat_input.value = "on the first Shared channel"

            await app._switch_channel(1)
            self.assertEqual(chat_input.value, "")
            app._accept_received_message(replace(SIMULATED_MESSAGES[0], channel_index=1))
            self.assertEqual([e.channel_index for e in app.chat_history], [1])

            await app._switch_channel(0)
            self.assertEqual(chat_input.value, "on the first Shared channel")
            self.assertEqual(app.chat_history, [])

    async def test_arrows_navigate_whole_messages_controls_and_resend(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(60, 24)) as pilot:
            app.show_tab("chat")
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    text="a long message " * 20,
                    packet_id=8101,
                )
            )
            app._accept_received_message(
                replace(SIMULATED_MESSAGES[1], packet_id=8102)
            )
            outgoing = app._accepted_send("try again")
            entry = app.chat_history[-1]
            app._set_delivery_state(entry, DeliveryState.UNCONFIRMED)
            widgets = list(app.query(ChatEntryWidget))

            widgets[0].focus()
            await pilot.press("down")
            self.assertIs(app.focused, widgets[1])
            await pilot.press("down")
            self.assertIs(app.focused, widgets[2])
            await pilot.press("down")
            self.assertIsInstance(app.focused, MessageActionControl)

            app._rebroadcast = Mock()
            await pilot.press("enter")
            app._rebroadcast.assert_called_once_with(entry)
            widgets[-1].focus()
            await pilot.press("down")
            self.assertIsInstance(app.focused, MessageActionControl)
            app.transcript_new_count = 2
            app.query_one("#chat-log").focus()
            await pilot.press("end")
            self.assertEqual(app.transcript_new_count, 0)

            footer = str(app.query_one("#footer", Static).render())
            self.assertIn("C channel", footer)
            self.assertNotIn("ESC scroll", footer)

    async def test_resend_visibility_matches_manual_retry_states(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 28)) as pilot:
            app.show_tab("chat")
            app._accepted_send("retry policy")
            entry = app.chat_history[-1]
            widget = app.query_one(ChatEntryWidget)

            for state, expected in (
                (DeliveryState.SENDING, False),
                (DeliveryState.SENT, False),
                (DeliveryState.HEARD, False),
                (DeliveryState.UNCONFIRMED, True),
                (DeliveryState.FAILED, True),
            ):
                with self.subTest(state=state):
                    app._set_delivery_state(entry, state)
                    self.assertEqual(widget.action_control.display, expected)
                    self.assertEqual(can_manual_resend(entry), expected)

            incoming_entry = received_chat_entry(SIMULATED_MESSAGES[0])
            incoming_entry.delivery_state = DeliveryState.FAILED
            incoming_widget = ChatEntryWidget(incoming_entry)
            self.assertFalse(incoming_widget.action_control.display)
            self.assertFalse(can_manual_resend(incoming_entry))

            app._rebroadcast = Mock()
            for state in (DeliveryState.UNCONFIRMED, DeliveryState.FAILED):
                app._set_delivery_state(entry, state)
                await pilot.pause()
                widget.action_control.focus()
                await pilot.press("enter")
                await pilot.pause()
                app._rebroadcast.assert_called_with(entry)
            self.assertEqual(app._rebroadcast.call_count, 2)

    async def test_channel_switch_uses_indexed_sqlite_history_without_duplicates(self) -> None:
        store = ChatStore.open(Path(self.directory.name) / "chat.db")
        for channel, text in ((0, "primary history"), (1, "hiking history")):
            store.add_incoming(
                packet_id=9000 + channel,
                node_id="!a11ce001",
                sender_name="Alice",
                sender_short_name="ALCE",
                channel_index=channel,
                text=text,
                radio_rx_at=1000.0,
                received_at=1001.0,
            )
        app = MeshtasticPassApp(self.radio(), self.settings, chat_store=store)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            self.assertEqual(
                [entry.text for entry in app.chat_history],
                ["primary history"],
            )
            await app._switch_channel(1)
            self.assertEqual(
                [entry.text for entry in app.chat_history],
                ["hiking history"],
            )
            await app._switch_channel(0)
            self.assertEqual(
                [entry.text for entry in app.chat_history],
                ["primary history"],
            )
            self.assertEqual(len(list(app.query(ChatEntryWidget))), 1)


if __name__ == "__main__":
    unittest.main()
