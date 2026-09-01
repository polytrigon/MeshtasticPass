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
from simulated_radio_service import (
    SIMULATED_LOCAL_NODE_ID,
    SIMULATED_SHORT_NAME,
    SIMULATED_MESSAGES,
    SimulatedRadioService,
)
from theme_palette import THEME_PALETTES
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

    async def test_live_delayed_packet_reorders_and_notice_tracks_pending_review(self) -> None:
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
            for theme in ("snow", "amber"):
                accent = THEME_PALETTES[theme].accent.upper()
                app._apply_color_theme(theme)
                await pilot.pause()
                self.assertEqual(status.visual_style.foreground.hex6, accent)
            self.assertFalse(
                any(
                    timer.name == "chat-older-message-notice"
                    for timer in app._timers
                )
            )

            app.show_tab("chat")
            self.assertEqual(app.unread_count, 0)
            self.assertTrue(all(entry.is_new for entry in app.chat_history))
            first_widget = list(app.query(ChatEntryWidget))[0]
            first_widget.focus()
            await pilot.pause()
            self.assertFalse(first_widget.entry.is_new)
            self.assertFalse(first_widget.has_class("new-message"))
            self.assertTrue(first_widget.has_class("favorite-sender"))
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(app.query(ViewportMenu)), 1)

    async def test_normal_aged_packet_dedupe_and_history_load_do_not_notify(self) -> None:
        store = ChatStore.open(self.db_path)
        store.set_local_profile(SIMULATED_LOCAL_NODE_ID, SIMULATED_SHORT_NAME)
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
            await pilot.pause()
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

            app._accept_received_message(
                self.message(4598, "older above", self.base_time + 0.5)
            )
            await pilot.pause()
            above = next(
                widget
                for widget in app.query(ChatEntryWidget)
                if widget.entry.text == "older above"
            )
            above.focus()
            await pilot.pause()
            self.assertEqual(app.transcript_new_count, 1)

            below = next(
                widget
                for widget in app.query(ChatEntryWidget)
                if widget.entry.text == "older but below"
            )
            below.focus()
            await pilot.pause()
            self.assertEqual(app.transcript_new_count, 0)
            self.assertEqual(
                str(app.query_one("#chat-new-below", Static).render()),
                "",
            )

    async def test_delayed_insert_respects_default_mounted_cap(self) -> None:
        store = ChatStore.open(self.db_path)
        store.set_local_profile(SIMULATED_LOCAL_NODE_ID, SIMULATED_SHORT_NAME)
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
        store.set_local_profile(SIMULATED_LOCAL_NODE_ID, SIMULATED_SHORT_NAME)
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
            self.assertEqual(
                str(app.query_one("#send-error", Static).render()),
                "1 OLDER MESSAGE RECEIVED",
            )
            ids = [entry.message_id for entry in app.chat_history]
            self.assertEqual(len(ids), len(set(ids)))

            loaded_widget = next(
                widget
                for widget in app.query(ChatEntryWidget)
                if widget.entry is loaded
            )
            loaded_widget.focus()
            await pilot.pause()
            self.assertFalse(loaded.is_new)
            self.assertEqual(str(app.query_one("#send-error", Static).render()), "")

    async def test_keyboard_selection_acknowledges_only_selected_older_messages(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            for packet_id, text, offset in (
                (7001, "newest", 400),
                (7002, "older one", 300),
                (7003, "older two", 200),
                (7004, "older three", 100),
            ):
                app._accept_received_message(
                    self.message(packet_id, text, self.base_time + offset)
                )
            app.show_tab("chat")
            await pilot.pause()
            status = app.query_one("#send-error", Static)
            self.assertEqual(str(status.render()), "3 OLDER MESSAGES RECEIVED")
            widgets = list(app.query(ChatEntryWidget))

            widgets[0].focus()
            await pilot.pause()
            self.assertFalse(widgets[0].entry.is_new)
            self.assertTrue(all(widget.entry.is_new for widget in widgets[1:]))
            self.assertEqual(str(status.render()), "2 OLDER MESSAGES RECEIVED")
            self.assertEqual(
                widgets[0].timestamp_label.visual_style.foreground.hex6,
                THEME_PALETTES["snow"].dim_base,
            )
            self.assertEqual(
                widgets[0].query_one(".chat-entry-author", Static)
                .visual_style.foreground.hex6,
                THEME_PALETTES["snow"].base.upper(),
            )

            widgets[1].focus()
            await pilot.pause()
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")
            widgets[2].focus()
            await pilot.pause()
            self.assertEqual(str(status.render()), "")
            self.assertTrue(widgets[3].entry.is_new)

            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(app.query(ViewportMenu)), 1)

    async def test_reselecting_an_already_acknowledged_message_does_nothing(
        self,
    ) -> None:
        """Extends test_keyboard_selection_acknowledges_only_selected_

        older_messages with the one case it doesn't cover: navigating
        away from an already-acknowledged OLDER message and back must
        not decrement the (already-authoritative, already-zeroed-out-
        for-that-message) count a second time -- this reuses the
        existing seen/BASE selection machinery (_mark_message_read /
        pending_older_ids.discard, which is naturally idempotent on an
        identity no longer present in the set) rather than a new,
        separately-tracked acknowledgement flag.
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            for packet_id, text, offset in (
                (7201, "newest", 400),
                (7202, "older one", 300),
                (7203, "older two", 200),
            ):
                app._accept_received_message(
                    self.message(packet_id, text, self.base_time + offset)
                )
            app.show_tab("chat")
            await pilot.pause()
            status = app.query_one("#send-error", Static)
            self.assertEqual(str(status.render()), "2 OLDER MESSAGES RECEIVED")
            widgets = list(app.query(ChatEntryWidget))

            widgets[0].focus()
            await pilot.pause()
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")

            # Navigate away, then back onto the same, already-acknowledged
            # message -- the count must not move at all.
            widgets[1].focus()
            await pilot.pause()
            self.assertEqual(str(status.render()), "")
            widgets[0].focus()
            await pilot.pause()
            self.assertEqual(str(status.render()), "")

            # Re-focusing it directly, repeatedly, is equally inert.
            for _ in range(3):
                widgets[0].blur()
                await pilot.pause()
                widgets[0].focus()
                await pilot.pause()
                self.assertEqual(str(status.render()), "")

    async def test_old_messages_count_derives_from_authoritative_state_not_a_counter(
        self,
    ) -> None:
        """The displayed count is len(state.pending_older_ids), recomputed

        fresh by _render_chat_status() every time -- proven here by
        mutating that authoritative set directly (as the app's own
        machinery does) and confirming the displayed text tracks it
        exactly, with no separate counter that could drift.
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            for packet_id, text, offset in (
                (7301, "newest", 400),
                (7302, "older one", 300),
                (7303, "older two", 200),
                (7304, "older three", 100),
            ):
                app._accept_received_message(
                    self.message(packet_id, text, self.base_time + offset)
                )
            app.show_tab("chat")
            await pilot.pause()
            status = app.query_one("#send-error", Static)
            state = app._state_for(0)
            self.assertEqual(len(state.pending_older_ids), 3)
            self.assertEqual(str(status.render()), "3 OLDER MESSAGES RECEIVED")

            identity = next(iter(state.pending_older_ids))
            state.pending_older_ids.discard(identity)
            app._render_chat_status()
            self.assertEqual(str(status.render()), "2 OLDER MESSAGES RECEIVED")

            state.pending_older_ids.clear()
            app._render_chat_status()
            self.assertEqual(str(status.render()), "")

    async def test_mouse_selection_matches_keyboard_without_scroll_acknowledgement(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 18)) as pilot:
            app.show_tab("chat")
            for index in range(20):
                app._accept_received_message(
                    self.message(
                        7100 + index,
                        f"message {index}",
                        self.base_time + index,
                    )
                )
            await pilot.pause()
            transcript = app.query_one(ChatTranscript)
            transcript.scroll_to(y=0, animate=False)
            await pilot.pause()
            transcript.scroll_to(y=4, animate=False)
            await pilot.pause()
            target = next(
                widget
                for widget in app.query(ChatEntryWidget)
                if widget.region.y >= transcript.region.y
                and widget.region.bottom <= transcript.region.bottom
            )
            self.assertTrue(target.entry.is_new)
            self.assertTrue(target.has_class("new-message"))

            clicked = await pilot.click(target.message_label)
            await pilot.pause()
            self.assertTrue(clicked)
            self.assertFalse(target.entry.is_new)
            self.assertFalse(target.has_class("new-message"))

    async def test_unrelated_selection_and_dedup_do_not_change_older_pending(self) -> None:
        store = ChatStore.open(self.db_path)
        store.set_local_profile(SIMULATED_LOCAL_NODE_ID, SIMULATED_SHORT_NAME)
        store.add_incoming(
            packet_id=7199,
            node_id="!history",
            sender_name="History",
            sender_short_name="HIST",
            channel_index=0,
            text="historical",
            radio_rx_at=self.base_time - 100,
            received_at=self.base_time - 99,
        )
        app = MeshtasticPassApp(
            self.radio(),
            self.settings,
            chat_store=store,
        )
        async with app.run_test(size=(80, 22)) as pilot:
            app._accept_received_message(
                self.message(7201, "newer", self.base_time + 200)
            )
            app._accept_received_message(
                self.message(7202, "older", self.base_time + 100)
            )
            app._accept_received_message(
                self.message(7202, "older duplicate", self.base_time + 100)
            )
            app.show_tab("chat")
            await pilot.pause()
            status = app.query_one("#send-error", Static)
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")
            widgets = list(app.query(ChatEntryWidget))

            historical = next(
                widget for widget in widgets if widget.entry.text == "historical"
            )
            historical.focus()
            await pilot.pause()
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")

            newest = next(widget for widget in widgets if widget.entry.text == "newer")
            newest.focus()
            await pilot.pause()
            self.assertFalse(newest.entry.is_new)
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")

            app._accepted_send("local message")
            await pilot.pause()
            outgoing = list(app.query(ChatEntryWidget))[-1]
            app._accept_received_message(
                self.message(7203, "another older", self.base_time + 50)
            )
            await pilot.pause()
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")
            outgoing.focus()
            await pilot.pause()
            self.assertEqual(str(status.render()), "1 OLDER MESSAGE RECEIVED")

    async def test_bulk_read_clears_pending_older_review(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 22)) as pilot:
            app._accept_received_message(
                self.message(7301, "newer", self.base_time + 200)
            )
            app._accept_received_message(
                self.message(7302, "older", self.base_time + 100)
            )
            app.show_tab("chat")
            await pilot.pause()
            self.assertEqual(
                str(app.query_one("#send-error", Static).render()),
                "1 OLDER MESSAGE RECEIVED",
            )

            app.show_tab("connection")
            await pilot.pause()
            self.assertFalse(any(entry.is_new for entry in app.chat_history))
            self.assertFalse(app._state_for(0).pending_older_ids)
            self.assertEqual(str(app.query_one("#send-error", Static).render()), "")

    async def test_delayed_notice_and_order_are_isolated_per_channel(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 22)) as pilot:
            await pilot.pause()
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
            self.assertEqual(
                str(app.query_one("#send-error", Static).render()),
                "1 OLDER MESSAGE RECEIVED",
            )
            await app._switch_channel(0)
            self.assertEqual(str(app.query_one("#send-error", Static).render()), "")
            await app._switch_channel(1)
            self.assertEqual(
                str(app.query_one("#send-error", Static).render()),
                "1 OLDER MESSAGE RECEIVED",
            )


if __name__ == "__main__":
    unittest.main()
