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
from radio_service import NodeMetadata, RadioState, ReceivedMessage
from simulated_radio_service import (
    SIMULATED_LOCAL_NODE_ID,
    SIMULATED_MESSAGES,
    SimulatedRadioService,
)
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
        """PR #46 follow-up Part G: lightened from #9D00FF to #B84DFF

        after real-hardware feedback that the original was too dark.
        """
        self.assertEqual(THEME_PALETTES["snow"].accent2, "#B84DFF")

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

    async def test_activating_dm_selector_opens_dropdown_not_dms_mode(self) -> None:
        """PR #46 follow-up Part B item 12: activating the header's

        DM(N) control opens the DROPDOWN, exactly like pressing D --
        it must NOT immediately switch CHAT into DMS mode on its own.
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            selector = app.query_one(DMModeSelector)
            selector.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(selector.is_open)
            self.assertEqual(app._chat_mode, "channel")

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

    async def test_d_opens_dm_dropdown_not_dms_mode_directly(self) -> None:
        """PR #46 follow-up Part B item 7: D now mirrors C -- it opens

        the DM dropdown; it must NOT immediately switch CHAT into DMS
        mode/the full conversation list the way the original PR #46
        pass did.
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app.query_one("#chat-log").focus()
            await pilot.press("d")
            await pilot.pause()
            self.assertTrue(app.query_one(DMModeSelector).is_open)
            self.assertEqual(app._chat_mode, "channel")

    async def test_selecting_from_dm_dropdown_enters_dms_mode(self) -> None:
        """The DM dropdown reads from persisted conversations (the SAME

        ChatStore.list_dm_conversations() source _refresh_dm_list
        already uses -- item 10), so this test attaches a real
        ChatStore rather than relying on in-memory-only state.
        """
        store = ChatStore.open(self.chat_db_path)
        self.addCleanup(store.close)
        app = MeshtasticPassApp(_simulated_radio(), self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
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
            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app._chat_mode, "dms")
            self.assertEqual(app.current_dm_node_id, "!b0b00002")

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
            app.open_dm("!b0b00002", long_name="Bob")
            await pilot.pause()
            self.assertEqual(app._chat_mode, "dms")
            await pilot.press("escape")
            await pilot.pause()
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


# ---- Overlay key ownership (PR #46 follow-up Part A/F) --------------------


class NodeMenuKeyOwnershipTests(ChatDmMentionAppTestsBase):
    """Real-hardware regression: opening the CHAT node/user menu left

    the originating ChatEntryWidget focused (ViewportMenu.can_focus is
    False by design), so UP/DOWN pressed to navigate the menu ALSO
    reached Textual's own built-in App._on_key handler (co-resident on
    the App class alongside our custom on_key -- see event.
    prevent_default's docstring in on_key), which independently
    re-walked the focused widget's ancestor bindings and fired
    ChatTranscript's OWN inherited ScrollableContainer "up"/"down"
    scroll bindings -- scrolling the transcript underneath the menu on
    every arrow press, through the actual widget/app event path (not
    an isolated helper).
    """

    async def test_node_menu_navigation_does_not_move_underlying_chat(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 20)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            for i in range(40):
                app._accept_received_message(
                    replace(
                        SIMULATED_MESSAGES[0],
                        packet_id=900000 + i,
                        text=f"message {i} with enough padding text to force scrolling",
                    )
                )
            for _ in range(5):
                await pilot.pause()
            widgets = list(app.query(ChatEntryWidget))
            target = widgets[20]
            target.focus()
            for _ in range(3):
                await pilot.pause()
            transcript = app.query_one("#chat-log")
            scroll_before = transcript.scroll_y

            await pilot.press("enter")
            for _ in range(3):
                await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            menu = app._user_menu
            highlight_before = menu.highlighted_index

            await pilot.press("up")
            for _ in range(3):
                await pilot.pause()
            self.assertNotEqual(menu.highlighted_index, highlight_before)
            self.assertIs(app.focused, target)
            self.assertEqual(transcript.scroll_y, scroll_before)

            await pilot.press("down")
            for _ in range(3):
                await pilot.pause()
            self.assertIs(app.focused, target)
            self.assertEqual(transcript.scroll_y, scroll_before)

            await pilot.press("escape")
            await pilot.pause()
            self.assertIsNone(app._user_menu)

            await pilot.press("up")
            for _ in range(3):
                await pilot.pause()
            self.assertIsNot(app.focused, target)

    async def test_mesh_node_menu_does_not_move_mesh_board(self) -> None:
        """Same shared fix, MESH's own node menu (item 4: audit other

        overlays using the same _user_menu path).
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            focused_before = app.focused
            await pilot.press("up", "down")
            await pilot.pause()
            self.assertIs(app.focused, focused_before)


class ChannelDropdownOwnershipTests(ChatDmMentionAppTestsBase):
    async def test_channel_dropdown_navigation_does_not_move_chat(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            transcript = app.query_one("#chat-log")
            scroll_before = transcript.scroll_y
            await pilot.press("c")
            await pilot.pause()
            selector = app.query_one(ChannelSelector)
            self.assertTrue(selector.is_open)
            await pilot.press("down", "down")
            await pilot.pause()
            self.assertEqual(transcript.scroll_y, scroll_before)
            self.assertTrue(selector.is_open)
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(selector.is_open)
            self.assertEqual(app.current_tab, "chat")
            self.assertEqual(app._chat_mode, "channel")


class DmDropdownTests(ChatDmMentionAppTestsBase):
    async def test_d_opens_dropdown_with_real_conversations(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self.addCleanup(store.close)
        store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="hi",
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id="!a11ce001",
        )
        app = MeshtasticPassApp(_simulated_radio(), self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            transcript = app.query_one("#chat-log")
            scroll_before = transcript.scroll_y
            await pilot.press("d")
            await pilot.pause()
            selector = app.query_one(DMModeSelector)
            self.assertTrue(selector.is_open)
            self.assertEqual(app._chat_mode, "channel")
            # NodeDB (SIMULATED_NODES) already knows !a11ce001 as
            # "Alice Trail"/"ALCE", which _dm_identity prefers over
            # whatever this specific stored message happened to carry
            # ("Alice"/"ALC") -- matching open_user_menu's own,
            # already-established precedence.
            self.assertEqual(
                [item.label for item in selector.popup.items], ["Alice Trail / ALCE", "NEW DM"]
            )
            self.assertEqual(transcript.scroll_y, scroll_before)

            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(selector.is_open)
            self.assertEqual(app._chat_mode, "channel")
            self.assertIsNone(app.current_dm_node_id)

    async def test_dm_dropdown_orders_by_most_recent_activity(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self.addCleanup(store.close)
        store.add_incoming(
            packet_id=1,
            node_id="!11111111",
            sender_name="Older Chat",
            sender_short_name="OLD",
            channel_index=0,
            text="hi",
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id="!11111111",
        )
        store.add_incoming(
            packet_id=2,
            node_id="!22222222",
            sender_name="Newer Chat",
            sender_short_name="NEW",
            channel_index=0,
            text="hi",
            radio_rx_at=200.0,
            received_at=200.0,
            dm_node_id="!22222222",
        )
        app = MeshtasticPassApp(_simulated_radio(), self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            options = app.dm_dropdown_conversations()
            self.assertEqual([option.value for option in options], ["!22222222", "!11111111"])

    async def test_dm_dropdown_duplicate_long_names_stay_distinct(self) -> None:
        """Item 40: Node A "!aaaaaaaa" Rover/RVA, Node B "!bbbbbbbb"

        Rover/RVB must appear as two distinct dropdown rows, each
        selecting its own exact node_id.
        """
        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self.addCleanup(store.close)
        store.add_incoming(
            packet_id=1,
            node_id="!aaaaaaaa",
            sender_name="Rover",
            sender_short_name="RVA",
            channel_index=0,
            text="hi from a",
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id="!aaaaaaaa",
        )
        store.add_incoming(
            packet_id=2,
            node_id="!bbbbbbbb",
            sender_name="Rover",
            sender_short_name="RVB",
            channel_index=0,
            text="hi from b",
            radio_rx_at=200.0,
            received_at=200.0,
            dm_node_id="!bbbbbbbb",
        )
        app = MeshtasticPassApp(_simulated_radio(), self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            options = app.dm_dropdown_conversations()
            self.assertEqual(len(options), 2)
            values = {option.value for option in options}
            self.assertEqual(values, {"!aaaaaaaa", "!bbbbbbbb"})

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.current_dm_node_id, "!bbbbbbbb")

    async def test_empty_dm_dropdown_is_safe(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.press("d")
            await pilot.pause()
            selector = app.query_one(DMModeSelector)
            self.assertTrue(selector.is_open)
            self.assertEqual([item.label for item in selector.popup.items], ["NO DMS", "NEW DM"])
            await pilot.press("up", "down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNone(app.current_dm_node_id)
            self.assertEqual(app._chat_mode, "channel")
            self.assertEqual(radio.sent_texts, [])

            await pilot.press("d")
            await pilot.pause()
            self.assertTrue(app.query_one(DMModeSelector).is_open)
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.query_one(DMModeSelector).is_open)

    async def test_direct_dm_entry_points_bypass_dropdown(self) -> None:
        """Item 18/42: explicit CHAT/MESH node-menu DM actions still

        open the exact conversation directly -- never routed through
        the dropdown, never requiring a second selection.
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!c0ffee01", long_name="Direct Target", short_name="DT")
            await pilot.pause()
            self.assertEqual(app._chat_mode, "dms")
            self.assertEqual(app.current_dm_node_id, "!c0ffee01")
            self.assertFalse(app.query_one(DMModeSelector).is_open)


# ---- AMBER CHAT composer = ACCENT2 (PR #46 final follow-up Part B) -------


class AmberComposerColorTests(ChatDmMentionAppTestsBase):
    async def test_amber_channel_composer_uses_accent2(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._apply_color_theme("amber")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(
                chat_input.styles.color.hex.upper(),
                THEME_PALETTES["amber"].accent2.upper(),
            )

    async def test_amber_dm_composer_uses_accent2(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._switch_chat_mode("dms")
            await pilot.pause()
            app._apply_color_theme("amber")
            await pilot.pause()
            dm_input = app.query_one("#dm-input", Input)
            self.assertEqual(
                dm_input.styles.color.hex.upper(),
                THEME_PALETTES["amber"].accent2.upper(),
            )

    async def test_amber_accent2_is_unchanged_value(self) -> None:
        self.assertEqual(THEME_PALETTES["amber"].accent2, "#40C4FF")

    async def test_no_hardcoded_hex_in_composer_css(self) -> None:
        """Item 10: the composer rule must reference the semantic

        ACCENT2 token, never a literal #40C4FF baked in specifically
        for the input widgets (the token itself is still allowed to
        appear once, in the CSS variable definition).
        """
        css = MeshtasticPassApp.CSS
        chat_rule_start = css.index("Screen.theme-amber #chat-input")
        chat_rule = css[chat_rule_start : chat_rule_start + 80]
        self.assertNotIn("#40C4FF", chat_rule.upper())
        self.assertIn("$amber_accent2", chat_rule)
        dm_rule_start = css.index("Screen.theme-amber #dm-input")
        dm_rule = css[dm_rule_start : dm_rule_start + 80]
        self.assertNotIn("#40C4FF", dm_rule.upper())
        self.assertIn("$amber_accent2", dm_rule)

    async def test_snow_composer_appearance_unchanged(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(
                chat_input.styles.color.hex.upper(),
                THEME_PALETTES["snow"].base.upper(),
            )

    async def test_live_snow_to_amber_switch_updates_composer(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(
                chat_input.styles.color.hex.upper(),
                THEME_PALETTES["snow"].base.upper(),
            )
            app._apply_color_theme("amber")
            await pilot.pause()
            self.assertEqual(
                chat_input.styles.color.hex.upper(),
                THEME_PALETTES["amber"].accent2.upper(),
            )

    async def test_live_amber_to_snow_switch_restores_snow_appearance(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._apply_color_theme("amber")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(
                chat_input.styles.color.hex.upper(),
                THEME_PALETTES["amber"].accent2.upper(),
            )
            app._apply_color_theme("snow")
            await pilot.pause()
            self.assertEqual(
                chat_input.styles.color.hex.upper(),
                THEME_PALETTES["snow"].base.upper(),
            )

    async def test_amber_composer_keyboard_and_send_behavior_unchanged(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._apply_color_theme("amber")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("h", "i")
            await pilot.pause()
            self.assertEqual(chat_input.value, "hi")
            await pilot.press("enter")
            for _ in range(5):
                await pilot.pause()
            self.assertEqual(radio.sent_texts, ["hi"])

    async def test_amber_theme_switch_generates_zero_rf(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._apply_color_theme("amber")
            await pilot.pause()
            app._apply_color_theme("snow")
            await pilot.pause()
            self.assertEqual(radio.sent_texts, [])


# ---- AMBER "> message" prompt (PR #46 hardware acceptance follow-up) ----


class AmberComposerPromptColorTests(ChatDmMentionAppTestsBase):
    """Real hardware exposed a SEPARATE bug from the typed-text color

    fix above: "> message" is Textual's own Input.placeholder, styled
    through the distinct "input--placeholder" component class
    (Input.DEFAULT_CSS: "color: $text-disabled") -- a completely
    different mechanism from the plain `color` property, which only
    ever affected typed text. These tests target the placeholder
    itself, not styles.color.
    """

    @staticmethod
    def _placeholder_color(widget: Input) -> str:
        color = widget.get_component_rich_style("input--placeholder").color
        return color.get_truecolor().hex.upper()

    async def test_amber_channel_prompt_uses_accent2(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._apply_color_theme("amber")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(
                self._placeholder_color(chat_input),
                THEME_PALETTES["amber"].accent2.upper(),
            )

    async def test_amber_dm_prompt_uses_accent2(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._switch_chat_mode("dms")
            await pilot.pause()
            app._apply_color_theme("amber")
            await pilot.pause()
            dm_input = app.query_one("#dm-input", Input)
            self.assertEqual(
                self._placeholder_color(dm_input),
                THEME_PALETTES["amber"].accent2.upper(),
            )

    async def test_amber_typed_text_still_matches_prompt(self) -> None:
        """Both must visibly share the same AMBER ACCENT2 identity."""
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._apply_color_theme("amber")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(
                self._placeholder_color(chat_input),
                chat_input.styles.color.hex.upper(),
            )

    async def test_snow_prompt_unchanged(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            self.assertNotEqual(
                self._placeholder_color(chat_input),
                THEME_PALETTES["snow"].accent2.upper(),
            )
            self.assertNotEqual(
                self._placeholder_color(chat_input),
                THEME_PALETTES["amber"].accent2.upper(),
            )

    async def test_live_snow_to_amber_switch_updates_prompt(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            before = self._placeholder_color(chat_input)
            app._apply_color_theme("amber")
            await pilot.pause()
            after = self._placeholder_color(chat_input)
            self.assertNotEqual(before, after)
            self.assertEqual(after, THEME_PALETTES["amber"].accent2.upper())

    async def test_live_amber_to_snow_switch_restores_prompt(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            original = self._placeholder_color(chat_input)
            app._apply_color_theme("amber")
            await pilot.pause()
            app._apply_color_theme("snow")
            await pilot.pause()
            self.assertEqual(self._placeholder_color(chat_input), original)

    async def test_no_hardcoded_hex_in_placeholder_css(self) -> None:
        css = MeshtasticPassApp.CSS
        chat_rule_start = css.index("Screen.theme-amber #chat-input > .input--placeholder")
        chat_rule = css[chat_rule_start : chat_rule_start + 100]
        self.assertNotIn("#40C4FF", chat_rule.upper())
        self.assertIn("$amber_accent2", chat_rule)
        dm_rule_start = css.index("Screen.theme-amber #dm-input > .input--placeholder")
        dm_rule = css[dm_rule_start : dm_rule_start + 100]
        self.assertNotIn("#40C4FF", dm_rule.upper())
        self.assertIn("$amber_accent2", dm_rule)

    async def test_amber_prompt_theme_switch_generates_zero_rf(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._apply_color_theme("amber")
            await pilot.pause()
            app._apply_color_theme("snow")
            await pilot.pause()
            self.assertEqual(radio.sent_texts, [])


# ---- RECONNECT DELIVERY + CHAT HEADER FIX Part B -----------------------


class ChatConnectingHeaderTests(ChatDmMentionAppTestsBase):
    """While CONNECTING (or any other not-ONLINE state), CHAT's header

    must show only the connection-status text -- never the separator
    bullet or the DM selector alongside it (real-hardware report: both
    remained visible, e.g. "STATUS CONNECTING... • [ DM(0) ▾ ]").
    """

    def _header_widgets(self, app: MeshtasticPassApp):
        bullet = app.query_one("#chat-header-bullet")
        dm_selector = app.query_one(DMModeSelector)
        channel_selector = app.query_one(ChannelSelector)
        return channel_selector, bullet, dm_selector

    async def test_connecting_hides_bullet_and_dm_selector(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            channel_selector, bullet, dm_selector = self._header_widgets(app)
            self.assertTrue(bullet.display)
            self.assertTrue(dm_selector.display)

            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()

            self.assertFalse(bullet.display)
            self.assertFalse(dm_selector.display)
            self.assertTrue(dm_selector.disabled)
            header_text = str(channel_selector.render())
            self.assertIn("CONNECTING", header_text)
            self.assertNotIn("DM", header_text)

    async def test_offline_also_hides_bullet_and_dm_selector(self) -> None:
        """Item 14: the same principle applies to every not-ONLINE

        connection-status presentation, not CONNECTING specifically.
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._show_connection(RadioState.OFFLINE, message="lost")
            await pilot.pause()
            _, bullet, dm_selector = self._header_widgets(app)
            self.assertFalse(bullet.display)
            self.assertFalse(dm_selector.display)

    async def test_online_restores_normal_header_automatically(self) -> None:
        """Item 12: no tab switch, manual refresh, or focus movement

        required -- ONLINE alone restores it, using whatever
        channel/DM state the user already had selected.
        """
        radio = _simulated_radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()
            channel_selector, bullet, dm_selector = self._header_widgets(app)
            self.assertFalse(bullet.display)
            self.assertFalse(dm_selector.display)

            app._show_connection(RadioState.ONLINE, radio.info)
            await pilot.pause()

            self.assertTrue(bullet.display)
            self.assertTrue(dm_selector.display)
            self.assertFalse(dm_selector.disabled)
            header_text = str(channel_selector.render())
            self.assertNotIn("CONNECTING", header_text)
            self.assertNotIn("STATUS", header_text)

    async def test_hiding_does_not_auto_open_a_dropdown_on_restore(self) -> None:
        radio = _simulated_radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()
            app._show_connection(RadioState.ONLINE, radio.info)
            await pilot.pause()
            _, _, dm_selector = self._header_widgets(app)
            self.assertFalse(dm_selector.is_open)
            self.assertFalse(app.query_one(ChannelSelector).is_open)

    async def test_focus_is_not_stranded_on_hidden_dm_selector(self) -> None:
        """Item 15: focus must move to a visible, usable target the

        instant the DM selector is hidden -- never remain on it.
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            dm_selector = app.query_one(DMModeSelector)
            dm_selector.focus()
            await pilot.pause()
            self.assertIs(app.focused, dm_selector)

            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()

            self.assertIsNot(app.focused, dm_selector)
            self.assertTrue(app.query_one("#chat-log").has_focus)

    async def test_dm_selector_popup_closes_when_hidden(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            dm_selector = app.query_one(DMModeSelector)
            dm_selector.focus()
            dm_selector.open_menu()
            await pilot.pause()
            self.assertTrue(dm_selector.is_open)

            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()

            self.assertFalse(dm_selector.is_open)

    async def test_c_and_d_are_inert_while_connecting(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app.query_one("#chat-log").focus()
            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()

            await pilot.press("d")
            await pilot.pause()
            self.assertFalse(app.query_one(DMModeSelector).is_open)

            await pilot.press("c")
            await pilot.pause()
            self.assertFalse(app.query_one(ChannelSelector).is_open)

    async def test_c_and_d_resume_normal_behavior_once_online(self) -> None:
        radio = _simulated_radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()
            app._show_connection(RadioState.ONLINE, radio.info)
            await pilot.pause()
            app.query_one("#chat-log").focus()

            await pilot.press("d")
            await pilot.pause()
            self.assertTrue(app.query_one(DMModeSelector).is_open)
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("c")
            await pilot.pause()
            self.assertTrue(app.query_one(ChannelSelector).is_open)

    async def test_hiding_header_preserves_dm_state_and_unread_count(self) -> None:
        """Item 13: presentation only -- selected channel, active DM

        conversation, DM unread count, drafts, transcript history, and
        channel state must all survive the hide/show cycle untouched.
        """
        store = ChatStore.open(self.chat_db_path)
        self.addCleanup(store.close)
        store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="hi",
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id="!a11ce001",
        )
        radio = _simulated_radio()
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            unread_before = app.query_one(DMModeSelector).selected_label
            app.query_one("#chat-input", Input).value = "unsent draft"

            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()
            app._show_connection(RadioState.ONLINE, radio.info)
            await pilot.pause()

            self.assertEqual(
                app.query_one(DMModeSelector).selected_label, unread_before
            )
            self.assertEqual(
                app.query_one("#chat-input", Input).value, "unsent draft"
            )
            self.assertEqual(app.current_channel_index, 0)

    async def test_header_visibility_generates_zero_rf_traffic(self) -> None:
        radio = _simulated_radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()
            app._show_connection(RadioState.ONLINE, radio.info)
            await pilot.pause()
            self.assertEqual(radio.sent_messages, ())


if __name__ == "__main__":
    unittest.main()
