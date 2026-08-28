"""Tests for CHAT/DM/MENTION UX: DM folded into CHAT as a mode, the

CHAT header's peer selectors, C/D hotkeys, DM(N) unread tracking,
REPLY's new SHORTNAME-first fallback, @mention highlighting, the new
SNOW ACCENT2 neon-purple value, and CONNECTED rendering in ACCENT.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from textual.widgets import Input, Static

from app import (
    ChannelSelector,
    ChatEntryWidget,
    DMModeSelector,
    MeshtasticPassApp,
    message_mentions_short_name,
)
from app_settings import AppSettings
from chat_store import ChatStore
from radio_service import NodeMetadata, ReceivedMessage
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService
from tests.test_app import ControllableSendRadioService
from theme_palette import THEME_PALETTES
from viewport_menu import ViewportMenu


def _simulated_radio(**kwargs) -> SimulatedRadioService:
    return SimulatedRadioService(
        connect_delay=0, message_interval=0, scripted_messages=(), **kwargs
    )


class ChatDmMentionAppTestsBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.chat_db_path = self.root / "chat.db"


# ---- Theme (Part G) ---------------------------------------------------


class SnowAccent2ColorTests(unittest.TestCase):
    def test_snow_accent2_is_the_exact_new_neon_purple(self) -> None:
        self.assertEqual(THEME_PALETTES["snow"].accent2, "#9D00FF")

    def test_amber_accent2_is_unchanged(self) -> None:
        self.assertEqual(THEME_PALETTES["amber"].accent2, "#40C4FF")

    def test_other_snow_tokens_are_unchanged(self) -> None:
        snow = THEME_PALETTES["snow"]
        self.assertEqual(snow.base, "#F2F2F2")
        self.assertEqual(snow.accent, "#39FF14")
        self.assertEqual(snow.confirm, "#39FF14")


# ---- Top-level navigation (Part A) -------------------------------------


class TopLevelNavigationTests(ChatDmMentionAppTestsBase):
    async def test_top_nav_has_no_dm_entry(self) -> None:
        from app import TAB_NAMES

        self.assertEqual(list(TAB_NAMES), ["connection", "chat", "mesh"])

    async def test_no_stale_dm_tab_footer_reference(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            footer_text = str(app.query_one("#footer", Static).render())
            self.assertNotIn("DM", footer_text.split("F4")[0].upper() or "")
            self.assertNotIn("1-4", footer_text)


# ---- CHAT header (Part B) ----------------------------------------------


class ChatHeaderTests(ChatDmMentionAppTestsBase):
    async def test_both_peer_selectors_present(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            self.assertEqual(len(app.query(ChannelSelector)), 1)
            self.assertEqual(len(app.query(DMModeSelector)), 1)

    async def test_bullet_separator_is_not_focusable(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            bullet = app.query_one("#chat-header-bullet")
            self.assertFalse(bullet.can_focus)

    async def test_dm_selector_shows_unread_count(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            self.assertIn("DM(0)", str(app.query_one(DMModeSelector).render()))

    async def test_activating_dm_selector_switches_to_dms_mode(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app.query_one(DMModeSelector).focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app._chat_mode, "dms")

    async def test_selecting_a_channel_switches_back_to_channel_mode(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._switch_chat_mode("dms")
            await pilot.pause()
            self.assertEqual(app._chat_mode, "dms")
            await app._switch_channel(app.current_channel_index)
            await pilot.pause()
            self.assertEqual(app._chat_mode, "channel")


# ---- C/D hotkeys (Part C) -----------------------------------------------


class ChatHotkeyTests(ChatDmMentionAppTestsBase):
    async def test_c_switches_to_channel_mode(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._switch_chat_mode("dms")
            await pilot.pause()
            app.query_one("#dm-list").focus()
            await pilot.press("c")
            await pilot.pause()
            self.assertEqual(app._chat_mode, "channel")

    async def test_d_switches_to_dms_mode(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app.query_one("#chat-log").focus()
            await pilot.press("d")
            await pilot.pause()
            self.assertEqual(app._chat_mode, "dms")

    async def test_c_does_not_reset_channel_draft(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            chat_input.value = "unfinished thought"
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            self.assertEqual(app._chat_mode, "dms")
            await pilot.press("c")
            await pilot.pause()
            self.assertEqual(app._chat_mode, "channel")
            self.assertEqual(app.query_one("#chat-input", Input).value, "unfinished thought")

    async def test_typing_c_and_d_inside_composer_types_text_not_hotkeys(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("c", "d")
            await pilot.pause()
            self.assertEqual(chat_input.value, "cd")
            self.assertEqual(app._chat_mode, "channel")

    async def test_c_and_d_do_not_conflict_with_numeric_tab_navigation(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app.query_one("#chat-log").focus()
            await pilot.press("3")
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.current_tab, "chat")


# ---- DM navigation (Part D) ---------------------------------------------


class DmNavigationEntryPointTests(ChatDmMentionAppTestsBase):
    async def test_open_dm_opens_exact_conversation_not_generic_list(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice Trail", short_name="ALCE")
            await pilot.pause()
            self.assertEqual(app.current_tab, "chat")
            self.assertEqual(app._chat_mode, "dms")
            self.assertEqual(app.current_dm_node_id, "!a11ce001")


# ---- DM unread model (Part E) --------------------------------------------


class DmUnreadModelTests(ChatDmMentionAppTestsBase):
    async def test_no_unread_shows_dm_zero(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.dm_unread_count, 0)

    async def test_incoming_dm_increments_unread(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            message = ReceivedMessage(
                sender_node_id="!b0b00002",
                sender_long_name="Bob",
                sender_short_name="BOB",
                channel_index=0,
                text="hi",
                rssi=None,
                snr=None,
                packet_id=1,
                is_direct=True,
            )
            app._accept_received_message(message)
            await pilot.pause()
            self.assertEqual(app.dm_unread_count, 1)
            self.assertIn("DM(1)", str(app.query_one(DMModeSelector).render()))

    async def test_multiple_conversations_aggregate_unread(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for node_id, packet_id in (("!b0b00002", 1), ("!c0ffee01", 2)):
                app._accept_received_message(
                    ReceivedMessage(
                        sender_node_id=node_id,
                        sender_long_name="X",
                        sender_short_name="X",
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=packet_id,
                        is_direct=True,
                    )
                )
            await pilot.pause()
            self.assertEqual(app.dm_unread_count, 2)

    async def test_outgoing_dm_never_increments_unread(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!b0b00002", long_name="Bob")
            await pilot.pause()
            entry = app._start_dm_outgoing("!b0b00002", "hello")
            app._send_from_thread(entry, entry.send_generation)
            await pilot.pause()
            self.assertEqual(app.dm_unread_count, 0)

    async def test_channel_message_never_increments_dm_unread(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._accept_received_message(
                replace(SIMULATED_MESSAGES[0], packet_id=999001)
            )
            await pilot.pause()
            self.assertEqual(app.dm_unread_count, 0)

    async def test_actively_open_conversation_does_not_increment(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!b0b00002", long_name="Bob")
            await pilot.pause()
            message = ReceivedMessage(
                sender_node_id="!b0b00002",
                sender_long_name="Bob",
                sender_short_name="BOB",
                channel_index=0,
                text="hi again",
                rssi=None,
                snr=None,
                packet_id=1,
                is_direct=True,
            )
            app._accept_received_message(message)
            await pilot.pause()
            self.assertEqual(app.dm_unread_count, 0)

    async def test_opening_conversation_clears_its_own_unread(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._accept_received_message(
                ReceivedMessage(
                    sender_node_id="!b0b00002",
                    sender_long_name="Bob",
                    sender_short_name="BOB",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    is_direct=True,
                )
            )
            await pilot.pause()
            self.assertEqual(app.dm_unread_count, 1)
            app.open_dm("!b0b00002", long_name="Bob")
            await pilot.pause()
            self.assertEqual(app.dm_unread_count, 0)

    async def test_reading_one_conversation_does_not_clear_another(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for node_id, packet_id in (("!b0b00002", 1), ("!c0ffee01", 2)):
                app._accept_received_message(
                    ReceivedMessage(
                        sender_node_id=node_id,
                        sender_long_name="X",
                        sender_short_name="X",
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=packet_id,
                        is_direct=True,
                    )
                )
            await pilot.pause()
            app.open_dm("!b0b00002", long_name="X")
            await pilot.pause()
            self.assertEqual(app.dm_unread_count, 1)

    async def test_opening_dm_selector_generates_zero_rf(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one(DMModeSelector).focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(radio.sent_texts, [])


# ---- REPLY (Part F) -------------------------------------------------------


class ReplyDuplicateLongNameTests(ChatDmMentionAppTestsBase):
    async def _reply_mention_for(self, pilot, app, message) -> str:
        app._accept_received_message(message)
        await pilot.pause()
        widget = next(w for w in app.query(ChatEntryWidget) if w.entry.text == message.text)
        widget.focus()
        await pilot.press("enter")
        await pilot.pause()
        menu = app.query_one(ViewportMenu)
        reply_index = next(i for i, item in enumerate(menu.items) if item.value == "reply")
        for _ in range(len(menu.items) + 1):
            if menu.highlighted_index == reply_index:
                break
            await pilot.press("down")
            await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        value = app.query_one("#chat-input", Input).value
        app.query_one("#chat-input", Input).value = ""
        return value

    async def test_duplicate_long_names_resolve_by_stable_node_id(self) -> None:
        """Item 20: Node A "!aaaaaaaa" Rover/RVA, Node B "!bbbbbbbb"

        Rover/RVB -- REPLY to each must resolve to that node's OWN
        SHORTNAME, never conflated by their shared Long Name.
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            message_a = replace(
                SIMULATED_MESSAGES[0],
                packet_id=70001,
                sender_node_id="!aaaaaaaa",
                sender_long_name="Rover",
                sender_short_name="RVA",
                text="from rover A",
            )
            message_b = replace(
                SIMULATED_MESSAGES[0],
                packet_id=70002,
                sender_node_id="!bbbbbbbb",
                sender_long_name="Rover",
                sender_short_name="RVB",
                text="from rover B",
            )
            mention_a = await self._reply_mention_for(pilot, app, message_a)
            mention_b = await self._reply_mention_for(pilot, app, message_b)
            self.assertEqual(mention_a, "@RVA ")
            self.assertEqual(mention_b, "@RVB ")


# ---- Mention highlighting (Part H) ---------------------------------------


class MentionTokenMatchingTests(unittest.TestCase):
    def test_matches(self) -> None:
        for text in (
            "@POLY",
            "@poly",
            "hello @POLY",
            "(@POLY)",
            "@POLY,",
            "@POLY!",
            "@POLY?",
        ):
            with self.subTest(text=text):
                self.assertTrue(message_mentions_short_name(text, "POLY"))

    def test_no_matches(self) -> None:
        for text in ("POLY", "POLY@", "@POLYGON", "foo@POLYbar", "@POLY123"):
            with self.subTest(text=text):
                self.assertFalse(message_mentions_short_name(text, "POLY"))

    def test_no_local_short_name_disables_matching(self) -> None:
        self.assertFalse(message_mentions_short_name("hey @POLY", None))
        self.assertFalse(message_mentions_short_name("hey @POLY", ""))
        self.assertFalse(message_mentions_short_name("hey @POLY", "   "))


class MentionHighlightAppTests(ChatDmMentionAppTestsBase):
    async def test_entire_incoming_channel_block_gets_mention_class(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            local_short = app.chat_local_short_name
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    packet_id=80001,
                    text=f"hey @{local_short} check this",
                )
            )
            await pilot.pause()
            widget = list(app.query(ChatEntryWidget))[-1]
            self.assertTrue(widget.has_class("mention"))

    async def test_unrelated_message_stays_normal(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._accept_received_message(
                replace(SIMULATED_MESSAGES[0], packet_id=80002, text="just chatting")
            )
            await pilot.pause()
            widget = list(app.query(ChatEntryWidget))[-1]
            self.assertFalse(widget.has_class("mention"))

    async def test_dm_never_auto_mention_highlighted(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            local_short = app.chat_local_short_name
            app.open_dm("!b0b00002", long_name="Bob")
            await pilot.pause()
            app._accept_received_message(
                ReceivedMessage(
                    sender_node_id="!b0b00002",
                    sender_long_name="Bob",
                    sender_short_name="BOB",
                    channel_index=0,
                    text=f"hey @{local_short} are you there",
                    rssi=None,
                    snr=None,
                    packet_id=80003,
                    is_direct=True,
                )
            )
            await pilot.pause()
            widget = next(
                w for w in app.query(ChatEntryWidget) if "are you there" in w.entry.text
            )
            self.assertFalse(widget.has_class("mention"))

    async def test_no_usable_local_short_name_disables_highlighting(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._radio_info = None
            app._accept_received_message(
                replace(SIMULATED_MESSAGES[0], packet_id=80004, text="hey @anything here")
            )
            await pilot.pause()
            widget = list(app.query(ChatEntryWidget))[-1]
            self.assertFalse(widget.has_class("mention"))

    async def test_radio_swap_updates_already_mounted_messages_immediately(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            old_short = app.chat_local_short_name
            app._accept_received_message(
                replace(SIMULATED_MESSAGES[0], packet_id=80005, text=f"hey @{old_short}")
            )
            await pilot.pause()
            widget = list(app.query(ChatEntryWidget))[-1]
            self.assertTrue(widget.has_class("mention"))

            from radio_service import RadioInfo, RadioState

            app._show_connection(
                RadioState.ONLINE,
                RadioInfo(
                    device_path=app.radio.device_path,
                    node_id="!newradio1",
                    long_name="New Radio",
                    short_name="NEW1",
                    firmware_version="test",
                    known_nodes=0,
                ),
            )
            await pilot.pause()
            self.assertFalse(widget.has_class("mention"))

    async def test_outgoing_message_never_mention_highlighted(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            local_short = app.chat_local_short_name
            entry = app._start_outgoing(f"hey @{local_short}")
            app._send_from_thread(entry, entry.send_generation)
            await pilot.pause()
            widget = next(w for w in app.query(ChatEntryWidget) if w.entry is entry)
            self.assertFalse(widget.has_class("mention"))

    async def test_mention_detection_generates_zero_rf(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            local_short = app.chat_local_short_name
            app._accept_received_message(
                replace(SIMULATED_MESSAGES[0], packet_id=80006, text=f"hey @{local_short}")
            )
            await pilot.pause()
            self.assertEqual(radio.sent_texts, [])


# ---- Connection status (Part I) -------------------------------------------


class ConnectionStatusAccentTests(ChatDmMentionAppTestsBase):
    async def test_connected_renders_in_accent_both_themes(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)):
            for theme in ("snow", "amber"):
                app._apply_color_theme(theme)
                app._show_connection(app._radio_state.__class__.ONLINE, app.radio.info)
                rendered = app.query_one("#connection-status", Static).render()
                self.assertEqual(
                    rendered.spans[-1].style.foreground.hex.upper(),
                    THEME_PALETTES[theme].accent.upper(),
                )


if __name__ == "__main__":
    unittest.main()
