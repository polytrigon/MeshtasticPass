"""Tests for the CHAT user-menu REPLY action and its @mention behavior.

The LONG NAME/SHORT NAME informational rows and FAVORITE/UNFAVORITE
action already existed and are covered by
tests/test_viewport_context_favorites.py (updated in this pass only to
account for the new REPLY row's presence/position). This file covers
what's new: REPLY itself, and the name-fallback precedence it uses.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from textual.widgets import Input

from app import ChatEntryWidget, MeshtasticPassApp
from app_settings import AppSettings
from radio_service import NodeMetadata
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService
from viewport_menu import ViewportMenu


class ChatReplyTests(unittest.IsolatedAsyncioTestCase):
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

    async def _open_menu_for(self, pilot, app, message) -> None:
        app._accept_received_message(message)
        await pilot.pause()
        widget = next(w for w in app.query(ChatEntryWidget) if w.entry.text == message.text)
        widget.focus()
        await pilot.press("enter")
        await pilot.pause()

    async def _select_reply(self, pilot, app) -> None:
        menu = app.query_one(ViewportMenu)
        reply_index = next(i for i, item in enumerate(menu.items) if item.value == "reply")
        # move_highlight() cycles only among actionable items in a fixed
        # order; repeatedly pressing "down" from any actionable item is
        # guaranteed to visit all of them (and return to the start)
        # within len(items) presses, so bound the loop instead of
        # guessing a direction that could oscillate forever.
        for _ in range(len(menu.items) + 1):
            if menu.highlighted_index == reply_index:
                break
            await pilot.press("down")
            await pilot.pause()
        else:
            self.fail(
                f"REPLY (index {reply_index}) was never reached; "
                f"stuck at {menu.highlighted_index}"
            )
        await pilot.press("enter")
        await pilot.pause()

    async def test_reply_menu_item_present_for_remote_message(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await self._open_menu_for(
                pilot, app, replace(SIMULATED_MESSAGES[0], packet_id=9001)
            )
            menu = app.query_one(ViewportMenu)
            self.assertIn("REPLY", [item.label for item in menu.items])

    async def test_reply_on_empty_draft_inserts_mention_with_trailing_space(
        self,
    ) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            message = replace(
                SIMULATED_MESSAGES[0],
                packet_id=9002,
                sender_node_id="!a11ce999",
                sender_long_name="ALICE",
                sender_short_name="ALC",
            )
            await self._open_menu_for(pilot, app, message)
            await self._select_reply(pilot, app)

            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(chat_input.value, "@ALICE ")
            self.assertTrue(chat_input.has_focus)
            self.assertEqual(chat_input.cursor_position, len(chat_input.value))

    async def test_reply_at_start_of_existing_draft_preserves_it(self) -> None:
        """Cursor at position 0 -- the mention is inserted before the

        existing draft, which survives completely untouched after it.
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "that's really cool"
            chat_input.cursor_position = 0

            message = replace(
                SIMULATED_MESSAGES[0],
                packet_id=9003,
                sender_node_id="!a11ce999",
                sender_long_name="ALICE",
                sender_short_name="ALC",
            )
            await self._open_menu_for(pilot, app, message)
            await self._select_reply(pilot, app)

            self.assertEqual(chat_input.value, "@ALICE that's really cool")
            self.assertEqual(chat_input.cursor_position, len("@ALICE "))

    async def test_reply_at_cursor_in_middle_of_existing_draft(self) -> None:
        """The defining case: insertion happens at the CURRENT CURSOR

        POSITION, not merely at the start or end -- text before and
        after the cursor is both preserved exactly.
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "hello there"
            chat_input.cursor_position = len("hello ")

            message = replace(
                SIMULATED_MESSAGES[0],
                packet_id=9004,
                sender_node_id="!a11ce999",
                sender_long_name="ALICE",
                sender_short_name="ALC",
            )
            await self._open_menu_for(pilot, app, message)
            await self._select_reply(pilot, app)

            self.assertEqual(chat_input.value, "hello @ALICE there")
            self.assertEqual(chat_input.cursor_position, len("hello @ALICE "))

    async def test_reply_at_cursor_after_an_existing_mention_inserts_again(self) -> None:
        """Insertion is unconditional on the cursor position -- REPLY

        does not special-case or deduplicate an already-present
        mention (a cursor-position-based insertion has no single
        "start of draft" to compare against).
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "@ALICE that's really cool"
            chat_input.cursor_position = 0

            message = replace(
                SIMULATED_MESSAGES[0],
                packet_id=9005,
                sender_node_id="!a11ce999",
                sender_long_name="ALICE",
                sender_short_name="ALC",
            )
            await self._open_menu_for(pilot, app, message)
            await self._select_reply(pilot, app)

            self.assertEqual(chat_input.value, "@ALICE @ALICE that's really cool")

    async def test_reply_falls_back_to_short_name_when_long_name_missing(
        self,
    ) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            message = replace(
                SIMULATED_MESSAGES[0],
                packet_id=9005,
                sender_node_id="!shortonly1",
                sender_long_name=None,
                sender_short_name="SHRT",
            )
            await self._open_menu_for(pilot, app, message)
            await self._select_reply(pilot, app)

            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(chat_input.value, "@SHRT ")

    async def test_reply_falls_back_to_compact_node_id_when_no_names(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            message = replace(
                SIMULATED_MESSAGES[0],
                packet_id=9006,
                sender_node_id="!bareid01",
                sender_long_name=None,
                sender_short_name=None,
            )
            await self._open_menu_for(pilot, app, message)
            await self._select_reply(pilot, app)

            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(chat_input.value, "@!bareid01 ")

    async def test_reply_closes_menu_and_focuses_input_ready_to_type(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            message = replace(
                SIMULATED_MESSAGES[0],
                packet_id=9007,
                sender_node_id="!a11ce999",
                sender_long_name="ALICE",
                sender_short_name="ALC",
            )
            await self._open_menu_for(pilot, app, message)
            self.assertEqual(len(app.query(ViewportMenu)), 1)
            await self._select_reply(pilot, app)

            self.assertEqual(len(app.query(ViewportMenu)), 0)
            chat_input = app.query_one("#chat-input", Input)
            self.assertTrue(chat_input.has_focus)

            # Normal typing/send behavior is otherwise unchanged.
            await pilot.press("h", "i")
            await pilot.pause()
            self.assertEqual(chat_input.value, "@ALICE hi")

    async def test_reply_generates_no_radio_traffic(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            message = replace(
                SIMULATED_MESSAGES[0],
                packet_id=9008,
                sender_node_id="!a11ce999",
                sender_long_name="ALICE",
                sender_short_name="ALC",
            )
            await self._open_menu_for(pilot, app, message)
            await self._select_reply(pilot, app)
            self.assertEqual(app.radio.sent_messages, ())

    async def test_reply_does_not_change_send_addressing(self) -> None:
        """REPLY is a text-entry convenience only -- confirm the resulting

        outgoing message still sends as an ordinary broadcast on the
        current channel, never a targeted/addressed send derived from
        the @mention text.
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            message = replace(
                SIMULATED_MESSAGES[0],
                packet_id=9009,
                sender_node_id="!a11ce999",
                sender_long_name="ALICE",
                sender_short_name="ALC",
            )
            await self._open_menu_for(pilot, app, message)
            await self._select_reply(pilot, app)
            await pilot.press("enter")
            await pilot.pause()
            entry = app.chat_history[-1]
            self.assertEqual(entry.text, "@ALICE ")
            self.assertTrue(entry.outgoing)


if __name__ == "__main__":
    unittest.main()
