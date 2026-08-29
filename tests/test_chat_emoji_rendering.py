"""CHAT emoji/scrollbar-corruption regression tests.

See tests/test_grapheme_text.py for the pure-function proof of the
underlying fix (grapheme_text.install_flag_pair_protection() patches
Rich's own grapheme splitter so a regional-indicator pair can never be
severed by its hard-wrap fallback -- the same fallback that already
protects ZWJ sequences, skin-tone modifiers, and variation-selector
pairs on its own). This file proves that fix is wired into CHAT's real
rendering path and that selection/navigation through Unicode-containing
messages behaves exactly like plain ASCII: no crash, no change in
scrollbar/container geometry, the message text renders completely
unmutated, and the message displays intact.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from time import time
import unittest

from textual.widgets import Input, Static

from chat_store import ChatStore
from grapheme_text import COMBINING_ENCLOSING_KEYCAP, terminal_safe_text
from app import ChatEntryWidget, ChatTranscript, MeshtasticPassApp, ThinScrollBarRender
from app_settings import AppSettings
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService


ASCII_TEXT = "hello there, this is a normal message"
SINGLE_EMOJI_TEXT = "hi 🍕 there"
EMOJI_AT_BOUNDARY_TEXT = "a very long line of plain words wrapping near here 🍕"
MULTIPLE_EMOJI_TEXT = "🍕🐔🎉🎊🥳"
ZWJ_FAMILY_TEXT = "family time 👨‍👩‍👧‍👦 today"
SKIN_TONE_TEXT = "wave hi 👋🏽 back"
FLAG_PAIR_TEXT = "go 🇺🇸🇬🇧 team"
CJK_TEXT = "你好，世界！这是一个测试消息"

KEYCAP_FIVE = "5️" + COMBINING_ENCLOSING_KEYCAP
KEYCAP_AT_START_TEXT = f"{KEYCAP_FIVE} is the floor"
KEYCAP_AT_WRAP_BOUNDARY_TEXT = (
    "a very long line of plain words wrapping near here " + KEYCAP_FIVE
)
REPEATED_KEYCAP_TEXT = KEYCAP_FIVE * 3
KEYCAP_WITH_ORDINARY_EMOJI_TEXT = f"🍕 order {KEYCAP_FIVE} for table 🎉"


class ChatEmojiRenderingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )
        self.base_time = time() - 10_000

    @staticmethod
    def radio() -> SimulatedRadioService:
        return SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )

    def message(self, packet_id: int, text: str, offset: float):
        return replace(
            SIMULATED_MESSAGES[0],
            packet_id=packet_id,
            text=text,
            radio_rx_at=self.base_time + offset,
        )

    async def _mount_message(self, pilot, app, text: str) -> ChatEntryWidget:
        app._accept_received_message(self.message(hash(text) & 0xFFFF, text, 0))
        await pilot.pause()
        return next(w for w in app.query(ChatEntryWidget) if w.entry.text == text)

    # ---- Renders without crashing, for every required category --------

    async def test_ascii_message_renders_and_selects(self) -> None:
        await self._assert_renders_and_selects(ASCII_TEXT)

    async def test_single_emoji_message_renders_and_selects(self) -> None:
        await self._assert_renders_and_selects(SINGLE_EMOJI_TEXT)

    async def test_emoji_at_wrap_boundary_renders_and_selects(self) -> None:
        await self._assert_renders_and_selects(EMOJI_AT_BOUNDARY_TEXT)

    async def test_multiple_emoji_message_renders_and_selects(self) -> None:
        await self._assert_renders_and_selects(MULTIPLE_EMOJI_TEXT)

    async def test_zwj_family_emoji_message_renders_and_selects(self) -> None:
        await self._assert_renders_and_selects(ZWJ_FAMILY_TEXT)

    async def test_skin_tone_modifier_message_renders_and_selects(self) -> None:
        await self._assert_renders_and_selects(SKIN_TONE_TEXT)

    async def test_flag_pair_message_renders_and_selects(self) -> None:
        await self._assert_renders_and_selects(FLAG_PAIR_TEXT)

    async def test_cjk_wide_character_message_renders_and_selects(self) -> None:
        await self._assert_renders_and_selects(CJK_TEXT)

    async def _assert_renders_and_selects(self, text: str) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            widget = await self._mount_message(pilot, app, text)

            # The rendered text is always exactly the stored text --
            # flag-pair wrap protection is applied at the rendering
            # boundary (see grapheme_text.install_flag_pair_protection),
            # never by mutating the string.
            rendered = str(widget.message_label.render())
            self.assertEqual(rendered, text)

            widget.focus()
            await pilot.pause()
            self.assertTrue(widget.has_class("chat-entry"))
            self.assertEqual(str(widget.selection_marker.render()), ">")

            widget.blur()
            await pilot.pause()
            self.assertEqual(str(widget.selection_marker.render()), " ")

    # ---- Arrow navigation onto/off Unicode-containing messages ---------

    async def test_arrow_navigation_through_mixed_unicode_messages(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            texts = [
                ASCII_TEXT,
                SINGLE_EMOJI_TEXT,
                ZWJ_FAMILY_TEXT,
                FLAG_PAIR_TEXT,
                CJK_TEXT,
            ]
            for index, text in enumerate(texts):
                app._accept_received_message(self.message(1000 + index, text, index))
            await pilot.pause()

            widgets = [w for w in app.query(ChatEntryWidget)]
            self.assertEqual(len(widgets), len(texts))

            widgets[0].focus()
            await pilot.pause()
            for _ in range(len(widgets) - 1):
                app._move_chat_focus(1)
                await pilot.pause()
            # Arriving at the last (most Unicode-heavy) message with no crash.
            self.assertTrue(app.focused.has_class("chat-entry"))

            for _ in range(len(widgets) - 1):
                app._move_chat_focus(-1)
                await pilot.pause()
            self.assertTrue(app.focused.has_class("chat-entry"))

    # ---- Scrollbar/right-edge geometry stability ------------------------

    async def test_scrollbar_geometry_unaffected_by_emoji_content(self) -> None:
        """The transcript's own width/scrollbar-renderer type must be

        identical whether its messages are plain ASCII or Unicode-heavy
        -- selection styling and message content never resize the
        container (see .chat-entry-row/.chat-entry-content's fixed 1fr
        width in app.py's CSS).
        """
        ascii_app = MeshtasticPassApp(self.radio(), self.settings)
        async with ascii_app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            ascii_app.show_tab("chat")
            await pilot.pause()
            for index, text in enumerate([ASCII_TEXT] * 5):
                ascii_app._accept_received_message(
                    self.message(2000 + index, text, index)
                )
            await pilot.pause()
            ascii_transcript = ascii_app.query_one("#chat-log", ChatTranscript)
            ascii_width = ascii_transcript.size.width
            ascii_renderer = ascii_transcript.vertical_scrollbar.renderer

        emoji_texts = [
            SINGLE_EMOJI_TEXT,
            MULTIPLE_EMOJI_TEXT,
            ZWJ_FAMILY_TEXT,
            SKIN_TONE_TEXT,
            FLAG_PAIR_TEXT,
        ]
        emoji_settings = AppSettings.load(
            config_path=Path(self.directory.name) / "config2.json",
            profile_path=Path(self.directory.name) / "terminal2.conf",
        )
        emoji_app = MeshtasticPassApp(self.radio(), emoji_settings)
        async with emoji_app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            emoji_app.show_tab("chat")
            await pilot.pause()
            for index, text in enumerate(emoji_texts):
                emoji_app._accept_received_message(
                    self.message(3000 + index, text, index)
                )
            await pilot.pause()
            emoji_transcript = emoji_app.query_one("#chat-log", ChatTranscript)
            emoji_width = emoji_transcript.size.width
            emoji_renderer = emoji_transcript.vertical_scrollbar.renderer

            # Selecting an emoji-containing message must not shift the
            # transcript's own width -- the right edge (where the
            # scrollbar lives) stays exactly where it was.
            widget = next(
                w for w in emoji_app.query(ChatEntryWidget) if w.entry.text == FLAG_PAIR_TEXT
            )
            widget.focus()
            await pilot.pause()
            self.assertEqual(emoji_transcript.size.width, emoji_width)

        self.assertEqual(ascii_width, emoji_width)
        self.assertIs(ascii_renderer, ThinScrollBarRender)
        self.assertIs(emoji_renderer, ThinScrollBarRender)


class KeycapDigitScrollbarSafetyTests(unittest.IsolatedAsyncioTestCase):
    """A keycap-digit emoji (e.g. boxed "5") is measured by Rich/Textual

    as one 2-cell-wide unit via a VARIATION SELECTOR-16 "promote to
    wide" rule a plain terminal font need not implement -- unlike every
    other emoji class in ChatEmojiRenderingTests above, its *accounted*
    width can disagree with what an arbitrary target terminal actually
    paints, corrupting the wrap point, this entry's height, and
    everything laid out after it (the scrollbar). The fix substitutes
    it, at the display boundary only, with the single-codepoint circled
    digit of the same number (see grapheme_text.terminal_safe_text) --
    an ordinary character with no presentation-selector interaction, so
    accounted and actual width can never disagree. This class proves
    that substitution is real (stored text is untouched, only the
    rendered label changes), applied in both CHANNEL and DM, and that
    it does not disturb transcript/scrollbar geometry the way the raw
    sequence would.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )
        self.base_time = time() - 10_000

    @staticmethod
    def radio() -> SimulatedRadioService:
        return SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )

    def message(self, packet_id: int, text: str, offset: float):
        return replace(
            SIMULATED_MESSAGES[0],
            packet_id=packet_id,
            text=text,
            radio_rx_at=self.base_time + offset,
        )

    async def _mount_message(self, pilot, app, text: str) -> ChatEntryWidget:
        app._accept_received_message(self.message(hash(text) & 0xFFFF, text, 0))
        await pilot.pause()
        return next(w for w in app.query(ChatEntryWidget) if w.entry.text == text)

    # ---- The width disagreement this fix closes is real -----------------

    def test_raw_keycap_and_substituted_form_disagree_on_accounted_width(self) -> None:
        """Not hypothetical: prove the exact numeric disagreement between

        what Rich/Textual would account for the raw sequence versus the
        substituted display form -- the gap this fix closes.
        """
        from rich.cells import cell_len

        self.assertEqual(cell_len(KEYCAP_FIVE), 2)
        self.assertEqual(cell_len(terminal_safe_text(KEYCAP_FIVE)), 1)

    # ---- Renders with the substituted glyph, original text untouched ----

    async def _assert_renders_substituted(self, text: str) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            widget = await self._mount_message(pilot, app, text)

            # entry.text (persistence, RF payload, @mention matching)
            # keeps the exact original sequence untouched.
            self.assertEqual(widget.entry.text, text)

            # Only the rendered label is display-safe.
            rendered = str(widget.message_label.render())
            self.assertEqual(rendered, terminal_safe_text(text))
            self.assertNotIn(COMBINING_ENCLOSING_KEYCAP, rendered)

            widget.focus()
            await pilot.pause()
            self.assertTrue(widget.has_class("chat-entry"))
            widget.blur()
            await pilot.pause()

    async def test_keycap_at_start_of_message(self) -> None:
        await self._assert_renders_substituted(KEYCAP_AT_START_TEXT)

    async def test_keycap_near_wrap_boundary(self) -> None:
        await self._assert_renders_substituted(KEYCAP_AT_WRAP_BOUNDARY_TEXT)

    async def test_repeated_keycaps(self) -> None:
        await self._assert_renders_substituted(REPEATED_KEYCAP_TEXT)

    async def test_keycap_alongside_ordinary_emoji(self) -> None:
        await self._assert_renders_substituted(KEYCAP_WITH_ORDINARY_EMOJI_TEXT)

    # ---- Transcript/scrollbar geometry stability, vs. plain-text control -

    async def test_transcript_and_scrollbar_geometry_unaffected_in_channel(self) -> None:
        control_app = MeshtasticPassApp(self.radio(), self.settings)
        async with control_app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            control_app.show_tab("chat")
            await pilot.pause()
            for index in range(12):
                control_app._accept_received_message(
                    self.message(4000 + index, ASCII_TEXT, index)
                )
            await pilot.pause()
            control_transcript = control_app.query_one("#chat-log", ChatTranscript)
            control_width = control_transcript.size.width
            control_virtual_height = control_transcript.virtual_size.height
            control_renderer = control_transcript.vertical_scrollbar.renderer

        keycap_settings = AppSettings.load(
            config_path=Path(self.directory.name) / "config2.json",
            profile_path=Path(self.directory.name) / "terminal2.conf",
        )
        keycap_app = MeshtasticPassApp(self.radio(), keycap_settings)
        async with keycap_app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            keycap_app.show_tab("chat")
            await pilot.pause()
            texts = [ASCII_TEXT] * 11 + [KEYCAP_AT_WRAP_BOUNDARY_TEXT]
            for index, text in enumerate(texts):
                keycap_app._accept_received_message(self.message(5000 + index, text, index))
            await pilot.pause()
            keycap_transcript = keycap_app.query_one("#chat-log", ChatTranscript)
            keycap_width = keycap_transcript.size.width
            keycap_virtual_height = keycap_transcript.virtual_size.height
            keycap_renderer = keycap_transcript.vertical_scrollbar.renderer

            # A visible scrollbar (enough history) whose track/thumb
            # renderer is unchanged, and whose right edge (transcript
            # width) is unaffected by the keycap-containing message.
            self.assertGreater(keycap_transcript.max_scroll_y, 0)
            self.assertIs(keycap_renderer, ThinScrollBarRender)

            # Scrolling through it end-to-end is stable: no crash, and
            # position stays within the legitimate range.
            keycap_transcript.scroll_end(animate=False)
            await pilot.pause()
            self.assertEqual(keycap_transcript.scroll_y, keycap_transcript.max_scroll_y)
            keycap_transcript.scroll_home(animate=False)
            await pilot.pause()
            self.assertEqual(keycap_transcript.scroll_y, 0)

        self.assertEqual(control_width, keycap_width)
        self.assertIs(control_renderer, ThinScrollBarRender)
        self.assertGreaterEqual(keycap_virtual_height, control_virtual_height)

    async def test_keycap_message_in_dm_conversation(self) -> None:
        store = ChatStore.open(Path(self.directory.name) / "chat.db")
        self.addCleanup(store.close)
        app = MeshtasticPassApp(self.radio(), self.settings, chat_store=store)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice Trail", short_name="ALCE")
            await pilot.pause()
            app.query_one("#dm-input", Input).value = KEYCAP_AT_START_TEXT
            await pilot.press("enter")
            await pilot.pause()

            widget = next(
                w
                for w in app.query(ChatEntryWidget)
                if w.entry.text == KEYCAP_AT_START_TEXT
            )
            self.assertEqual(widget.entry.text, KEYCAP_AT_START_TEXT)
            rendered = str(widget.message_label.render())
            self.assertEqual(rendered, terminal_safe_text(KEYCAP_AT_START_TEXT))
            self.assertNotIn(COMBINING_ENCLOSING_KEYCAP, rendered)

            dm_transcript = app.query_one("#dm-log", ChatTranscript)
            self.assertIs(dm_transcript.vertical_scrollbar.renderer, ThinScrollBarRender)


if __name__ == "__main__":
    unittest.main()
