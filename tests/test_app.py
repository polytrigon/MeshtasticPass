"""Headless interaction tests for the Textual app shell."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from textual.widgets import Input, Static

from app import ColorSelector, FontSizeSelector, MeshtasticPassApp
from app_settings import AppSettings
from radio_service import RadioInfo, RadioState
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService


class MeshtasticPassAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=root / "meshtasticpass" / "config.json",
            profile_path=root / "lxterminal" / "lxterminal-meshtasticpass.conf",
        )

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


if __name__ == "__main__":
    unittest.main()
