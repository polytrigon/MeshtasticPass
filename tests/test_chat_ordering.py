"""Chronological CHAT ordering and delayed-arrival UI regressions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from time import time
import unittest

from textual.widgets import Static

from app import ChatEntryWidget, ChatTranscript, LoadOlderControl, MeshtasticPassApp
from app_settings import AppSettings
from chat_store import ChatStore
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService
from viewport_menu import ViewportMenu


class ChatOrderingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )
        self.db_path = root / "chat.db"
        self.base_time = time() - 10_000

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
        radio_rx_at: float | None,
        *,
        channel_index: int = 0,
    ):
        return replace(
            SIMULATED_MESSAGES[0],
            packet_id=packet_id,
            text=text,
            radio_rx_at=radio_rx_at,
            channel_index=channel_index,
        )

    async def test_live_delayed_packet_reorders_and_accumulates_one_notice_timer(self) -> None:
        self.settings.set_favorite("!a11ce001", True)
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 22)) as pilot:
            newer = self.message(1001, "newer", self.base_time + 200)
            older = self.message(1002, "older", self.base_time + 100)
            oldest = self.message(1003, "oldest", self.base_time + 50)
            app._accept_received_message(newer)
            app._accept_received_message(older)
            app._accept_received_message(oldest)
            await pilot.pause()

            self.assertEqual(
                [entry.text for entry in app.chat_history],
                ["oldest", "older", "newer"],
            )
            self.assertEqual(
                [widget.entry.text for widget in app.query(ChatEntryWidget)],
                ["oldest", "older", "newer"],
            )
            self.assertTrue(all(entry.is_new for entry in app.chat_history))
            self.assertTrue(all(entry.unread for entry in app.chat_history))
            self.assertTrue(
                all(widget.has_class("favorite-sender") for widget in app.query(ChatEntryWidget))
            )
            status = app.query_one("#send-error", Static)
            self.assertEqual(str(status.render()), "2 OLDER MESSAGES RECEIVED")
            self.assertTrue(status.has_class("older-message-notice"))
            for theme, accent in (
                ("white", "#39FF14"),
                ("green", "#FF8C00"),
                ("orange", "#D8D8D8"),
            ):
                app._apply_color_theme(theme)
                await pilot.pause()
                self.assertEqual(status.visual_style.foreground.hex6, accent)
            self.assertEqual(
                len(
                    [
                        timer
                        for timer in app._timers
                        if timer.name == "chat-older-message-notice"
                    ]
                ),
                1,
            )

            app.show_tab("chat")
            self.assertEqual(app.unread_count, 0)
            self.assertTrue(all(entry.is_new for entry in app.chat_history))
            first_widget = list(app.query(ChatEntryWidget))[0]
            first_widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(app.query(ViewportMenu)), 1)

    async def test_normal_aged_packet_dedupe_and_history_load_do_not_notify(self) -> None:
        store = ChatStore.open(self.db_path)
        store.add_incoming(
            packet_id=2001,
            node_id="!history",
            sender_name="History",
            sender_short_name="HIST",
            channel_index=0,
            text="startup history",
            radio_rx_at=self.base_time,
            received_at=self.base_time + 1,
        )
        app = MeshtasticPassApp(self.radio(), self.settings, chat_store=store)
        async with app.run_test(size=(80, 22)) as pilot:
            await pilot.pause()
            self.assertEqual(str(app.query_one("#send-error", Static).render()), "")

            in_order = self.message(2002, "aged but newer", self.base_time + 100)
            app._accept_received_message(in_order)
            app._accept_received_message(in_order)
            await pilot.pause()
            self.assertEqual(
                [entry.text for entry in app.chat_history],
                ["startup history", "aged but newer"],
            )
            self.assertEqual(str(app.query_one("#send-error", Static).render()), "")

    async def test_error_wins_over_notice_then_notice_clears(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 22)) as pilot:
            app._accept_received_message(
                self.message(3001, "newer", self.base_time + 200)
            )
            app._accept_received_message(
                self.message(3002, "older", self.base_time + 100)
            )
            status = app.query_one("#send-error", Static)
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")

            app._show_send_error("send failed")
            self.assertEqual(str(status.render()), "send failed")
            self.assertFalse(status.has_class("older-message-notice"))
            app._show_send_error("")
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")
            app._clear_older_message_notice(0)
            self.assertEqual(str(status.render()), "")
            await pilot.pause()

    async def test_delayed_insert_preserves_scroll_and_never_counts_as_below(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 18)) as pilot:
            app.show_tab("chat")
            for index in range(25):
                app._accept_received_message(
                    self.message(
                        4000 + index,
                        f"message {index}",
                        self.base_time + index,
                    )
                )
            await pilot.pause()
            transcript = app.query_one(ChatTranscript)
            transcript.scroll_to(y=8, animate=False)
            await pilot.pause()
            old_scroll = transcript.scroll_y

            app._accept_received_message(
                self.message(4999, "inserted older", self.base_time + 2.5)
            )
            await pilot.pause()
            self.assertEqual(app.transcript_new_count, 0)
            self.assertEqual(
                str(app.query_one("#chat-new-below", Static).render()),
                "",
            )
            self.assertGreaterEqual(transcript.scroll_y, old_scroll)
            self.assertNotEqual(app.chat_history[-1].text, "inserted older")

            app._jump_to_newest()
            await pilot.pause()
            self.assertEqual(transcript.scroll_y, transcript.max_scroll_y)
            self.assertEqual(app.chat_history[-1].text, "message 24")

    async def test_delayed_insert_below_view_uses_new_below_indicator(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 18)) as pilot:
            app.show_tab("chat")
            for index in range(25):
                app._accept_received_message(
                    self.message(
                        4500 + index,
                        f"message {index}",
                        self.base_time + index,
                    )
                )
            await pilot.pause()
            transcript = app.query_one(ChatTranscript)
            transcript.scroll_to(y=0, animate=False)
            await pilot.pause()

            app._accept_received_message(
                self.message(4599, "older but below", self.base_time + 23.5)
            )
            await pilot.pause()
            self.assertEqual(app.transcript_new_count, 1)
            self.assertEqual(
                str(app.query_one("#chat-new-below", Static).render()),
                "↓ 1 NEW",
            )

    async def test_delayed_insert_respects_default_mounted_cap(self) -> None:
        store = ChatStore.open(self.db_path)
        for index in range(100):
            store.add_incoming(
                packet_id=4700 + index,
                node_id="!history",
                sender_name="History",
                sender_short_name="HIST",
                channel_index=0,
                text=f"history {index}",
                radio_rx_at=self.base_time + index,
                received_at=self.base_time + 500 + index,
            )
        app = MeshtasticPassApp(self.radio(), self.settings, chat_store=store)
        async with app.run_test(size=(80, 18)) as pilot:
            app._accept_received_message(
                self.message(4899, "delayed middle", self.base_time + 50.5)
            )
            await pilot.pause()

            self.assertEqual(len(app.chat_history), 100)
            self.assertIn("delayed middle", [entry.text for entry in app.chat_history])
            delayed = next(
                entry for entry in app.chat_history if entry.text == "delayed middle"
            )
            self.assertTrue(delayed.is_new)
            self.assertEqual(len(store.load_recent(limit=200)), 101)
            self.assertEqual(len(app.query(LoadOlderControl)), 1)

    async def test_older_than_mounted_window_persists_until_load_older(self) -> None:
        store = ChatStore.open(self.db_path)
        for index in range(100):
            store.add_incoming(
                packet_id=5000 + index,
                node_id="!history",
                sender_name="History",
                sender_short_name="HIST",
                channel_index=0,
                text=f"history {index}",
                radio_rx_at=self.base_time + index,
                received_at=self.base_time + 500 + index,
            )
        app = MeshtasticPassApp(self.radio(), self.settings, chat_store=store)
        async with app.run_test(size=(80, 18)) as pilot:
            self.assertEqual(len(app.chat_history), 100)
            app._accept_received_message(
                self.message(5999, "outside window", self.base_time - 100)
            )
            await pilot.pause()
            self.assertEqual(len(app.chat_history), 100)
            self.assertNotIn(
                "outside window", [entry.text for entry in app.chat_history]
            )
            self.assertEqual(len(store.load_recent(limit=200)), 101)
            self.assertEqual(
                str(app.query_one("#send-error", Static).render()),
                "1 OLDER MESSAGE RECEIVED",
            )

            app.show_tab("chat")
            await app.load_older_chat_history(LoadOlderControl.Activated())
            await pilot.pause()
            self.assertIn("outside window", [entry.text for entry in app.chat_history])
            loaded = next(
                entry for entry in app.chat_history if entry.text == "outside window"
            )
            self.assertTrue(loaded.is_new)
            self.assertTrue(loaded.unread)
            ids = [entry.message_id for entry in app.chat_history]
            self.assertEqual(len(ids), len(set(ids)))

    async def test_delayed_notice_and_order_are_isolated_per_channel(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 22)) as pilot:
            app._accept_received_message(
                self.message(6001, "channel zero", self.base_time + 100)
            )
            app._accept_received_message(
                self.message(
                    6101,
                    "channel one newer",
                    self.base_time + 200,
                    channel_index=1,
                )
            )
            app._accept_received_message(
                self.message(
                    6102,
                    "channel one older",
                    self.base_time + 150,
                    channel_index=1,
                )
            )
            self.assertEqual(str(app.query_one("#send-error", Static).render()), "")

            await app._switch_channel(1)
            await pilot.pause()
            self.assertEqual(
                [entry.text for entry in app.chat_history],
                ["channel one older", "channel one newer"],
            )
            self.assertEqual(str(app.query_one("#send-error", Static).render()), "")
            await app._switch_channel(0)
            self.assertEqual(str(app.query_one("#send-error", Static).render()), "")


if __name__ == "__main__":
    unittest.main()
