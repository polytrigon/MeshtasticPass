"""Complete directional CHAT navigation regressions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from time import time
import unittest
from unittest.mock import Mock

from textual.widgets import Input, Static

from app import (
    ChatEntryWidget,
    ChatTranscript,
    MessageActionControl,
    MeshtasticPassApp,
)
from app_settings import AppSettings
from chat_store import ChatStore, OLDER_HISTORY_PAGE_SIZE
from radio_service import DeliveryState
from simulated_radio_service import (
    SIMULATED_LOCAL_NODE_ID,
    SIMULATED_MESSAGES,
    SimulatedRadioService,
)


class ChatArrowNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )
        self.db_path = root / "chat.db"
        self.base_time = time() - 20_000

    @staticmethod
    def radio() -> SimulatedRadioService:
        return SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )

    def message(
        self,
        packet_id: int,
        text: str,
        offset: float,
        *,
        channel_index: int = 0,
    ):
        return replace(
            SIMULATED_MESSAGES[0],
            packet_id=packet_id,
            text=text,
            radio_rx_at=self.base_time + offset,
            channel_index=channel_index,
        )

    async def test_left_reviews_oldest_new_in_order_and_uses_read_transition(self) -> None:
        self.settings.set_favorite("!a11ce001", True)
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 22)) as pilot:
            for packet_id, text, offset in (
                (10_001, "newest", 400),
                (10_002, "older one", 300),
                (10_003, "older two", 200),
                (10_004, "older three", 100),
            ):
                app._accept_received_message(self.message(packet_id, text, offset))
            app._accept_received_message(
                self.message(10_101, "other channel", 50, channel_index=1)
            )
            app.show_tab("chat")
            await pilot.pause()
            transcript = app.query_one(ChatTranscript)
            status = app.query_one("#send-error", Static)
            transcript.focus()

            expected = (
                ("older three", "2 OLDER MESSAGES RECEIVED"),
                ("older two", "1 OLDER MESSAGE RECEIVED"),
                ("older one", ""),
                ("newest", ""),
            )
            for text, notice in expected:
                await pilot.press("left")
                await pilot.pause()
                self.assertIsInstance(app.focused, ChatEntryWidget)
                self.assertEqual(app.focused.entry.text, text)
                self.assertFalse(app.focused.entry.is_new)
                self.assertTrue(app.focused.has_class("favorite-sender"))
                self.assertEqual(str(status.render()), notice)

            final_focus = app.focused
            final_scroll = transcript.scroll_y
            await pilot.press("left")
            await pilot.pause()
            self.assertIs(app.focused, final_focus)
            self.assertEqual(transcript.scroll_y, final_scroll)
            self.assertTrue(
                any(entry.is_new for entry in app._state_for(1).entries)
            )

            # ACCENT-like states are not navigation identities.
            app._accepted_send("outgoing")
            outgoing = app.chat_history[-1]
            outgoing.is_new = True
            outgoing_widget = list(app.query(ChatEntryWidget))[-1]
            outgoing_widget.focus()
            await pilot.press("left")
            self.assertIs(app.focused, outgoing_widget)

    async def test_right_returns_to_present_and_end_remains_newest_only(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 18)) as pilot:
            app.show_tab("chat")
            for index in range(5):
                app._accept_received_message(
                    self.message(11_000 + index, f"message {index}", index)
                )
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            transcript = app.query_one(ChatTranscript)
            chat_input.value = "meet at the ridge"
            widgets = list(app.query(ChatEntryWidget))
            widgets[0].focus()
            await pilot.pause()
            remaining_new = sum(entry.is_new for entry in app.chat_history)
            state = app._state_for(0)
            state.new_below_ids = {
                app._entry_review_identity(entry)
                for entry in app.chat_history
                if entry.is_new
            }
            app.transcript_new_count = len(state.new_below_ids)
            app._update_transcript_indicator()

            await pilot.press("right")
            self.assertIs(app.focused, chat_input)
            self.assertEqual(chat_input.value, "meet at the ridge")
            self.assertEqual(chat_input.cursor_position, len(chat_input.value))
            self.assertEqual(sum(entry.is_new for entry in app.chat_history), remaining_new)
            self.assertEqual(app.transcript_new_count, 0)
            self.assertTrue(transcript.is_vertical_scroll_end)
            await pilot.press("!")
            self.assertEqual(chat_input.value, "meet at the ridge!")

            await pilot.press("escape")
            targets = app._chat_navigation_targets()
            targets[1].focus()
            await pilot.pause()
            self.assertIs(app.focused, targets[1])
            await pilot.press("right")
            self.assertIs(app.focused, chat_input)

            await pilot.press("escape", "up")
            self.assertIsInstance(app.focused, ChatEntryWidget)
            await pilot.press("end")
            self.assertIsNot(app.focused, chat_input)
            self.assertTrue(transcript.is_vertical_scroll_end)
            footer = str(app.query_one("#footer", Static).render())
            for forbidden in ("LEFT", "RIGHT", "END", "←", "→"):
                self.assertNotIn(forbidden, footer.upper())

    async def test_composer_up_down_loop_preserves_draft_and_editing_arrows(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 22)) as pilot:
            app._accept_received_message(self.message(12_001, "newest", 200))
            app._accept_received_message(self.message(12_002, "older", 100))
            app.show_tab("chat")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            chat_input.value = "meet at the ridge"
            chat_input.cursor_position = 5

            await pilot.press("left")
            self.assertEqual(chat_input.cursor_position, 4)
            await pilot.press("right")
            self.assertEqual(chat_input.cursor_position, 5)

            await pilot.press("up")
            self.assertIsInstance(app.focused, ChatEntryWidget)
            self.assertEqual(app.focused.entry.text, "newest")
            self.assertFalse(app.focused.entry.is_new)
            older = next(entry for entry in app.chat_history if entry.text == "older")
            self.assertTrue(older.is_new)
            self.assertEqual(
                str(app.query_one("#send-error", Static).render()),
                "1 OLDER MESSAGE RECEIVED",
            )
            self.assertEqual(chat_input.value, "meet at the ridge")

            await pilot.press("down")
            self.assertIs(app.focused, chat_input)
            self.assertEqual(chat_input.value, "meet at the ridge")
            self.assertEqual(chat_input.cursor_position, len(chat_input.value))

            # The loop remains stable and never leaves duplicate selection carets.
            for _ in range(2):
                await pilot.press("up")
                self.assertIsInstance(app.focused, ChatEntryWidget)
                await pilot.press("down")
                self.assertIs(app.focused, chat_input)
            selected = [
                widget
                for widget in app.query(ChatEntryWidget)
                if str(widget.selection_marker.render()) == ">"
            ]
            self.assertEqual(selected, [])

            await pilot.press("escape", "left")
            self.assertEqual(app.focused.entry.text, "older")
            await pilot.press("right")
            self.assertIs(app.focused, chat_input)
            self.assertEqual(chat_input.value, "meet at the ridge")

    async def test_composer_up_uses_final_resend_and_empty_chat_noops(self) -> None:
        """UP from the composer must land on the newest actionable

        message's RESEND control (⟲), never DEL directly (item 5) --
        DEL is reachable only by an explicit RIGHT from there (item 6).
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 22)) as pilot:
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            chat_input.value = "draft"
            await pilot.press("up")
            self.assertIs(app.focused, chat_input)

            app._accepted_send("retry")
            entry = app.chat_history[-1]
            app._set_delivery_state(entry, DeliveryState.FAILED)
            chat_input.focus()
            chat_input.value = "draft"
            await pilot.press("up")
            self.assertIsInstance(app.focused, MessageActionControl)
            self.assertEqual(app.focused.action, "resend")
            await pilot.press("right")
            self.assertEqual(app.focused.action, "delete")
            await pilot.press("left")
            self.assertEqual(app.focused.action, "resend")
            await pilot.press("down")
            self.assertIs(app.focused, chat_input)
            self.assertEqual(chat_input.value, "draft")

    async def test_left_reaches_unmounted_new_with_bounded_duplicate_free_pages(self) -> None:
        store = ChatStore.open(self.db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        for index in range(1, 261):
            store.add_incoming(
                packet_id=20_000 + index,
                node_id="!history",
                sender_name="History",
                sender_short_name="HIST",
                channel_index=0,
                text=f"history {index}",
                radio_rx_at=self.base_time + index,
                received_at=self.base_time + index,
            )
        original_loader = store.load_older_page
        store.load_older_page = Mock(wraps=original_loader)  # type: ignore[method-assign]
        app = MeshtasticPassApp(self.radio(), self.settings, chat_store=store)
        async with app.run_test(size=(80, 22)) as pilot:
            target = self.message(29_999, "unmounted new", 99.5)
            app._accept_received_message(target)
            target_id = next(iter(app._state_for(0).new_message_ids))
            self.assertNotIn(target_id, [entry.message_id for entry in app.chat_history])
            self.assertEqual(len(app.query(ChatEntryWidget)), 100)

            app.show_tab("chat")
            await pilot.press("escape", "left")
            for _ in range(20):
                await pilot.pause()
                if (
                    isinstance(app.focused, ChatEntryWidget)
                    and app.focused.entry.message_id == target_id
                ):
                    break

            self.assertIsInstance(app.focused, ChatEntryWidget)
            self.assertEqual(app.focused.entry.message_id, target_id)
            self.assertFalse(app.focused.entry.is_new)
            self.assertEqual(store.load_older_page.call_count, 2)
            for call in store.load_older_page.call_args_list:
                self.assertEqual(call.kwargs["limit"], OLDER_HISTORY_PAGE_SIZE)
            mounted_ids = [
                widget.entry.message_id for widget in app.query(ChatEntryWidget)
            ]
            self.assertEqual(len(mounted_ids), len(set(mounted_ids)))
            self.assertLess(len(mounted_ids), 261)
            self.assertEqual(
                str(app.query_one("#send-error", Static).render()),
                "",
            )


if __name__ == "__main__":
    unittest.main()
