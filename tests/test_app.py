"""Headless interaction tests for the Textual app shell."""

from __future__ import annotations

import unittest

from textual.widgets import Input

from app import MeshtasticPassApp
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService


class MeshtasticPassAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_tabs_input_and_send(self) -> None:
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        app = MeshtasticPassApp(radio)

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
        app = MeshtasticPassApp(radio)

        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(5):
                await pilot.pause()
                if len(app.chat_history) == len(SIMULATED_MESSAGES):
                    break

            self.assertEqual(len(app.chat_history), len(SIMULATED_MESSAGES))
            self.assertEqual(app.chat_history[0].author, "Alice Trail")
            self.assertEqual(
                [entry.text for entry in app.chat_history],
                [message.text for message in SIMULATED_MESSAGES],
            )


if __name__ == "__main__":
    unittest.main()
