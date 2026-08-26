"""CHAT emoji/scrollbar-corruption regression tests.

See tests/test_grapheme_text.py for the pure-function proof of the
underlying fix (Rich's own wrap engine already protects ZWJ sequences,
skin-tone modifiers, and variation-selector pairs on its own; the one
confirmed gap -- a regional-indicator flag pair -- is what
grapheme_text.protect_flag_pairs_from_wrap_severing() actually changes).
This file proves that fix is wired into CHAT's real rendering path and
that selection/navigation through Unicode-containing messages behaves
exactly like plain ASCII: no crash, no change in scrollbar/container
geometry, and the message displays intact.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
from time import time
import unittest

from textual.widgets import Static

from app import ChatEntryWidget, ChatTranscript, MeshtasticPassApp, ThinScrollBarRender
from app_settings import AppSettings
from grapheme_text import protect_flag_pairs_from_wrap_severing
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService


ASCII_TEXT = "hello there, this is a normal message"
SINGLE_EMOJI_TEXT = "hi 🍕 there"
EMOJI_AT_BOUNDARY_TEXT = "a very long line of plain words wrapping near here 🍕"
MULTIPLE_EMOJI_TEXT = "🍕🐔🎉🎊🥳"
ZWJ_FAMILY_TEXT = "family time 👨‍👩‍👧‍👦 today"
SKIN_TONE_TEXT = "wave hi 👋🏽 back"
FLAG_PAIR_TEXT = "go 🇺🇸🇬🇧 team"
CJK_TEXT = "你好，世界！这是一个测试消息"


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

            # Exactly what protect_flag_pairs_from_wrap_severing() would
            # produce -- identical to the original text for every
            # category except a flag pair, which deliberately gains an
            # internal joiner (see tests/test_grapheme_text.py).
            rendered = str(widget.message_label.render())
            self.assertEqual(rendered, protect_flag_pairs_from_wrap_severing(text))

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


if __name__ == "__main__":
    unittest.main()
