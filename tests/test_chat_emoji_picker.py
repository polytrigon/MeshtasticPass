"""Tests for the CHAT composer's Ctrl+E emoji picker."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from textual.widgets import Input, Static

from app import (
    EMOJI_PICKER_CHOICES,
    ChatEntryWidget,
    ChatTranscript,
    ColorSelector,
    EmojiPicker,
    MeshtasticPassApp,
)
from app_settings import AppSettings
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService
from theme_palette import THEME_PALETTES


class EmojiPickerTests(unittest.IsolatedAsyncioTestCase):
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
            connect_delay=0, message_interval=0, scripted_messages=()
        )

    def make_app(self) -> MeshtasticPassApp:
        return MeshtasticPassApp(self.radio(), self.settings)

    async def test_exactly_ten_emoji(self) -> None:
        self.assertEqual(len(EMOJI_PICKER_CHOICES), 10)

    async def test_ctrl_e_opens_only_from_the_composer(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")

            # Neutral CHAT focus (the transcript, not the composer).
            app.query_one("#chat-log", ChatTranscript).focus()
            await pilot.press("ctrl+e")
            await pilot.pause()
            self.assertIsNone(app._emoji_picker)

            # A CHAT message.
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await pilot.pause()
            widget = list(app.query(ChatEntryWidget))[-1]
            widget.focus()
            await pilot.press("ctrl+e")
            await pilot.pause()
            self.assertIsNone(app._emoji_picker)

            # A dropdown (CONNECTION/CONFIG).
            app.show_tab("connection")
            app.query_one(ColorSelector).focus()
            await pilot.press("ctrl+e")
            await pilot.pause()
            self.assertIsNone(app._emoji_picker)

            # The composer itself: this is the only context it opens from.
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e")
            await pilot.pause()
            self.assertIsNotNone(app._emoji_picker)
            self.assertIsInstance(app.screen.query_one(EmojiPicker), EmojiPicker)

    async def test_left_right_cycles_selection(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e")
            picker = app._emoji_picker
            self.assertEqual(picker.highlighted_index, 0)

            await pilot.press("right")
            self.assertEqual(picker.highlighted_index, 1)
            await pilot.press("left")
            self.assertEqual(picker.highlighted_index, 0)
            # Deterministic wrap-around at the edges.
            await pilot.press("left")
            self.assertEqual(picker.highlighted_index, len(EMOJI_PICKER_CHOICES) - 1)

    async def test_enter_inserts_at_cursor_and_closes_picker(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "hello there"
            chat_input.cursor_position = len("hello ")
            chat_input.focus()
            await pilot.press("ctrl+e")
            picker = app._emoji_picker
            await pilot.press("right", "right", "right")  # 👍 at index 3
            selected = picker.selected_emoji
            self.assertEqual(selected, "👍")

            await pilot.press("enter")
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertEqual(chat_input.value, "hello 👍there")
            self.assertEqual(chat_input.cursor_position, len("hello 👍"))
            self.assertTrue(chat_input.has_focus)

    async def test_enter_does_not_send_while_picker_is_open(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "draft text"
            chat_input.focus()
            await pilot.press("ctrl+e")
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()

            # ENTER selected the emoji, not sent anything -- the draft
            # (now with an emoji inserted) is still present, not cleared
            # as a send would leave it.
            self.assertNotEqual(chat_input.value, "")
            self.assertEqual(len(list(app.query(ChatEntryWidget))), 0)

    async def test_escape_is_lossless(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "hello there"
            chat_input.cursor_position = len("hello ")
            chat_input.focus()
            await pilot.press("ctrl+e")
            await pilot.press("right", "right")

            await pilot.press("escape")
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertEqual(chat_input.value, "hello there")
            self.assertEqual(chat_input.cursor_position, len("hello "))
            self.assertTrue(chat_input.has_focus)

    async def test_heart_emoji_sequence_stays_intact_and_editable(self) -> None:
        """❤️ is a multi-codepoint sequence (heart + variation selector) --

        it must survive insertion, and the composer must remain
        editable afterward without corrupting it.
        """
        heart_index = EMOJI_PICKER_CHOICES.index("❤️")
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e")
            for _ in range(heart_index):
                await pilot.press("right")
            self.assertEqual(app._emoji_picker.selected_emoji, "❤️")
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(chat_input.value, "❤️")
            # Still editable: typing continues to work normally afterward.
            await pilot.press("h", "i")
            self.assertEqual(chat_input.value, "❤️hi")

    async def test_reply_then_emoji_workflow(self) -> None:
        """The full documented workflow: REPLY inserts the mention, then

        Ctrl+E inserts an emoji at the (now-advanced) cursor position,
        without disturbing the typed text in between.
        """
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    sender_node_id="!a11ce999",
                    sender_long_name="ALICE",
                    packet_id=12345,
                )
            )
            widget = next(
                w for w in app.query(ChatEntryWidget) if w.entry.node_id == "!a11ce999"
            )
            widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            menu = app._user_menu
            reply_index = next(
                i for i, item in enumerate(menu.items) if item.value == "reply"
            )
            for _ in range(len(menu.items) + 1):
                if menu.highlighted_index == reply_index:
                    break
                await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()

            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(chat_input.value, "@ALICE ")

            chat_input.value = "@ALICE that's really cool "
            chat_input.cursor_position = len(chat_input.value)

            await pilot.press("ctrl+e")
            fire_index = EMOJI_PICKER_CHOICES.index("🔥")
            for _ in range(fire_index):
                await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(chat_input.value, "@ALICE that's really cool 🔥")
            self.assertTrue(chat_input.has_focus)

    async def test_theme_switching_updates_picker_palette(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app.query_one("#chat-input", Input).focus()
            await pilot.press("ctrl+e")
            picker = app._emoji_picker

            app._apply_color_theme("green")
            await pilot.pause()
            palette = THEME_PALETTES["green"]
            self.assertEqual(picker._base_color, palette.base)
            self.assertEqual(picker._accent_color, palette.accent)

    async def test_tab_switch_closes_the_picker(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app.query_one("#chat-input", Input).focus()
            await pilot.press("ctrl+e")
            self.assertIsNotNone(app._emoji_picker)

            app.show_tab("mesh")
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertEqual(len(app.screen.query(EmojiPicker)), 0)

    async def test_emoji_insertion_generates_no_radio_traffic(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e", "enter")
            await pilot.pause()

            self.assertEqual(radio.sent_messages, ())


if __name__ == "__main__":
    unittest.main()
