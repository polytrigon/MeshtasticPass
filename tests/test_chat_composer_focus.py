"""UI / CHANNEL / RADIO CONFIG TUNING Part B: CHAT composer focus.

Proves the full neutral <-> focused lifecycle: an unfocused ordinary
printable key starts composing, unfocused C/D stay hotkeys, a focused
composer treats every key as literal text, and a successful ENTER send
clears the composer and returns CHAT to its neutral navigation state
so the next ordinary key starts a fresh message rather than continuing
to type into an already-focused, already-empty composer.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from textual.widgets import Input

from app import ChannelSelector, ChatTranscript, DMModeSelector, MeshtasticPassApp
from app_settings import AppSettings
from radio_service import RadioState
from simulated_radio_service import SimulatedRadioService


class ChatComposerFocusTests(unittest.IsolatedAsyncioTestCase):
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

    @staticmethod
    async def _wait_online(pilot, app) -> None:
        for _ in range(15):
            await pilot.pause()
            if app._radio_state is RadioState.ONLINE:
                break
        assert app._radio_state is RadioState.ONLINE

    async def test_neutral_composer_starts_unfocused(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("chat")
            await pilot.pause()
            self.assertIsNot(app.focused, app.query_one("#chat-input", Input))
            self.assertIs(app.focused, app.query_one("#chat-log", ChatTranscript))

    async def test_ordinary_printable_key_focuses_and_inserts(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("chat")
            await pilot.pause()

            await pilot.press("h")
            await pilot.pause()

            chat_input = app.query_one("#chat-input", Input)
            self.assertIs(app.focused, chat_input)
            self.assertEqual(chat_input.value, "h")

            await pilot.press("e", "l", "l", "o")
            await pilot.pause()
            self.assertEqual(chat_input.value, "hello")

    async def test_unfocused_c_and_d_remain_hotkeys(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("chat")
            await pilot.pause()

            await pilot.press("c")
            await pilot.pause()
            selector = app.query_one(ChannelSelector)
            self.assertTrue(selector.is_open)
            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(chat_input.value, "")
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()
            dm_selector = app.query_one(DMModeSelector)
            self.assertTrue(dm_selector.is_open)
            self.assertEqual(chat_input.value, "")

    async def test_focused_c_and_d_are_literal_text(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("chat")
            await pilot.pause()

            await pilot.press("h")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(chat_input.value, "h")

            await pilot.press("c")
            await pilot.pause()
            self.assertEqual(chat_input.value, "hc")
            selector = app.query_one(ChannelSelector)
            self.assertFalse(selector.is_open)

            await pilot.press("d")
            await pilot.pause()
            self.assertEqual(chat_input.value, "hcd")
            dm_selector = app.query_one(DMModeSelector)
            self.assertFalse(dm_selector.is_open)

    async def test_successful_send_clears_composer_and_returns_to_neutral(
        self,
    ) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("chat")
            await pilot.pause()

            await pilot.press("h", "e", "l", "l", "o")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(chat_input.value, "hello")

            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(chat_input.value, "")
            self.assertIsNot(app.focused, chat_input)
            self.assertIs(app.focused, app.query_one("#chat-log", ChatTranscript))
            self.assertIn("hello", [e.text for e in app.chat_history])

            # The next ordinary printable key starts a fresh message.
            await pilot.press("w")
            await pilot.pause()
            self.assertIs(app.focused, chat_input)
            self.assertEqual(chat_input.value, "w")

    async def test_empty_send_does_not_discard_focus_or_text(self) -> None:
        """Sending cannot proceed on empty text -- focus/text must stay

        exactly as the user left them, never blurred merely to satisfy
        the neutral-return rule.
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("chat")
            await pilot.pause()

            await pilot.press("h")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            await pilot.press("backspace")
            await pilot.pause()
            self.assertEqual(chat_input.value, "")

            await pilot.press("enter")
            await pilot.pause()

            self.assertIs(app.focused, chat_input)
            self.assertEqual(chat_input.value, "")

    async def test_dm_send_also_returns_to_neutral(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()

            dm_input = app.query_one("#dm-input", Input)
            dm_input.focus()
            dm_input.value = "hi alice"
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(dm_input.value, "")
            self.assertIsNot(app.focused, dm_input)
            self.assertIs(app.focused, app.query_one("#dm-log", ChatTranscript))


if __name__ == "__main__":
    unittest.main()
