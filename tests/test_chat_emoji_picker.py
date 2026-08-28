"""Tests for the CHAT composer's Ctrl+E emoji picker."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unicodedata
import unittest

from textual.widgets import Input, Static

from rich.cells import cell_len

from app import (
    EMOJI_PICKER_BORDER_CELLS,
    EMOJI_PICKER_CHOICES,
    EMOJI_PICKER_PADDING_CELLS,
    ChatEntryWidget,
    ChatTranscript,
    ColorSelector,
    EmojiPicker,
    MeshtasticPassApp,
    emoji_picker_content_width,
    emoji_picker_total_width,
)
from app_settings import AppSettings
from simulated_radio_service import SIMULATED_MESSAGES, SimulatedRadioService
from theme_palette import THEME_PALETTES


class EmojiPickerTests(unittest.IsolatedAsyncioTestCase):
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

    def make_app(self) -> MeshtasticPassApp:
        return MeshtasticPassApp(self.radio(), self.settings)

    async def test_exactly_thirteen_emoji_in_order(self) -> None:
        self.assertEqual(len(EMOJI_PICKER_CHOICES), 13)
        self.assertEqual(
            EMOJI_PICKER_CHOICES,
            (
                "😀",
                "😂",
                "❤️",
                "👍",
                "👎",
                "😭",
                "😮",
                "😡",
                "🎉",
                "🔥",
                "👋",
                "✨",
                "📡",
            ),
        )

    async def test_final_set_swaps_and_additions(self) -> None:
        self.assertNotIn("😢", EMOJI_PICKER_CHOICES)
        for emoji in ("😭", "👋", "✨", "📡"):
            self.assertIn(emoji, EMOJI_PICKER_CHOICES)

    async def test_ctrl_e_opens_only_from_the_composer(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")

            # Neutral CHAT focus (the transcript, not the composer).
            app.query_one("#chat-log", ChatTranscript).focus()
            await pilot.press("ctrl+e")
            await pilot.pause()
            self.assertIsNone(app._emoji_picker)

            # A CHAT message.
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await pilot.pause()
            widget = list(app.query(ChatEntryWidget))[-1]
            widget.focus()
            await pilot.press("ctrl+e")
            await pilot.pause()
            self.assertIsNone(app._emoji_picker)

            # A dropdown (CONNECTION/CONFIG).
            app.show_tab("connection")
            app.query_one(ColorSelector).focus()
            await pilot.press("ctrl+e")
            await pilot.pause()
            self.assertIsNone(app._emoji_picker)

            # The composer itself: this is the only context it opens from.
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e")
            await pilot.pause()
            self.assertIsNotNone(app._emoji_picker)
            self.assertIsInstance(app.screen.query_one(EmojiPicker), EmojiPicker)

    async def test_left_right_cycles_selection(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e")
            picker = app._emoji_picker
            self.assertEqual(picker.highlighted_index, 0)

            await pilot.press("right")
            self.assertEqual(picker.highlighted_index, 1)
            await pilot.press("left")
            self.assertEqual(picker.highlighted_index, 0)
            # Deterministic wrap-around at the edges.
            await pilot.press("left")
            self.assertEqual(picker.highlighted_index, len(EMOJI_PICKER_CHOICES) - 1)

    async def test_right_reaches_every_one_of_the_thirteen_emoji(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e")
            picker = app._emoji_picker

            seen = [picker.selected_emoji]
            for _ in range(len(EMOJI_PICKER_CHOICES) - 1):
                await pilot.press("right")
                seen.append(picker.selected_emoji)
            self.assertEqual(tuple(seen), EMOJI_PICKER_CHOICES)

            # One more RIGHT wraps back around to the first emoji.
            await pilot.press("right")
            self.assertEqual(picker.highlighted_index, 0)

    async def test_enter_inserts_at_cursor_and_closes_picker(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "hello there"
            chat_input.cursor_position = len("hello ")
            chat_input.focus()
            await pilot.press("ctrl+e")
            picker = app._emoji_picker
            await pilot.press("right", "right", "right")  # 👍 at index 3
            selected = picker.selected_emoji
            self.assertEqual(selected, "👍")

            await pilot.press("enter")
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertEqual(chat_input.value, "hello 👍there")
            self.assertEqual(chat_input.cursor_position, len("hello 👍"))
            self.assertTrue(chat_input.has_focus)

    async def test_enter_does_not_send_while_picker_is_open(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "draft text"
            chat_input.focus()
            await pilot.press("ctrl+e")
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()

            # ENTER selected the emoji, not sent anything -- the draft
            # (now with an emoji inserted) is still present, not cleared
            # as a send would leave it.
            self.assertNotEqual(chat_input.value, "")
            self.assertEqual(len(list(app.query(ChatEntryWidget))), 0)

    async def test_escape_is_lossless(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "hello there"
            chat_input.cursor_position = len("hello ")
            chat_input.focus()
            await pilot.press("ctrl+e")
            await pilot.press("right", "right")

            await pilot.press("escape")
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertEqual(chat_input.value, "hello there")
            self.assertEqual(chat_input.cursor_position, len("hello "))
            self.assertTrue(chat_input.has_focus)

    async def test_up_dismisses_picker_then_leaves_composer_normally(self) -> None:
        """UP must not be swallowed merely to close the picker -- the

        SAME keypress both dismisses it AND performs the normal "UP
        leaves the composer for CHAT message navigation" behavior.
        """
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await pilot.pause()
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "hello there"
            chat_input.cursor_position = len("hello ")
            chat_input.focus()
            await pilot.press("ctrl+e")
            self.assertIsNotNone(app._emoji_picker)

            await pilot.press("up")
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertFalse(chat_input.has_focus)
            self.assertEqual(chat_input.value, "hello there")
            self.assertEqual(chat_input.cursor_position, len("hello "))

    async def test_up_dismisses_picker_even_when_composer_keeps_focus(self) -> None:
        """With an EMPTY chat history, _move_chat_focus(-1) is a no-op

        (nothing to navigate to) and the composer never loses focus, so
        ChatMessageInput.on_blur never fires. This is the one case that
        proves ChatMessageInput.on_key's own explicit UP-handling is
        actually load-bearing, not merely redundant with blur-based
        dismissal.
        """
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "draft"
            chat_input.focus()
            await pilot.press("ctrl+e")
            self.assertIsNotNone(app._emoji_picker)

            await pilot.press("up")
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertTrue(chat_input.has_focus)
            self.assertEqual(chat_input.value, "draft")

    async def test_tab_switch_dismisses_picker_without_mutating_draft(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "hello there"
            chat_input.cursor_position = len("hello ")
            chat_input.focus()
            await pilot.press("ctrl+e")
            self.assertIsNotNone(app._emoji_picker)

            app.show_tab("connection")
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertEqual(chat_input.value, "hello there")

    async def test_generic_composer_blur_dismisses_picker(self) -> None:
        """ANY normal focus transfer away from the composer -- not just

        ESC/UP/tab-switch specifically -- must dismiss the picker (see
        ChatMessageInput.on_blur).
        """
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.value = "draft"
            chat_input.focus()
            await pilot.press("ctrl+e")
            self.assertIsNotNone(app._emoji_picker)

            app.query_one("#chat-log", ChatTranscript).focus()
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertEqual(chat_input.value, "draft")

    async def test_reopening_composer_does_not_reopen_picker(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e")
            self.assertIsNotNone(app._emoji_picker)

            app.query_one("#chat-log", ChatTranscript).focus()
            await pilot.pause()
            self.assertIsNone(app._emoji_picker)

            chat_input.focus()
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)

    async def test_heart_emoji_sequence_stays_intact_and_editable(self) -> None:
        """❤️ is a multi-codepoint sequence (heart + variation selector) --

        it must survive insertion, and the composer must remain
        editable afterward without corrupting it.
        """
        heart_index = EMOJI_PICKER_CHOICES.index("❤️")
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e")
            for _ in range(heart_index):
                await pilot.press("right")
            self.assertEqual(app._emoji_picker.selected_emoji, "❤️")
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(chat_input.value, "❤️")
            # Still editable: typing continues to work normally afterward.
            await pilot.press("h", "i")
            self.assertEqual(chat_input.value, "❤️hi")

    async def test_new_emoji_insert_correctly_without_breaking_cursor(self) -> None:
        """😭 / 👋 / ✨ / 📡 are the newly added/swapped-in emoji -- each

        must insert cleanly at the cursor and leave the composer
        correctly positioned and editable, exactly like the
        already-verified ❤️ case.
        """
        for emoji in ("😭", "👋", "✨", "📡"):
            with self.subTest(emoji=emoji):
                app = self.make_app()
                async with app.run_test(size=(90, 24)) as pilot:
                    await pilot.pause()
                    app.show_tab("chat")
                    chat_input = app.query_one("#chat-input", Input)
                    chat_input.value = "hello there"
                    chat_input.cursor_position = len("hello ")
                    chat_input.focus()
                    await pilot.press("ctrl+e")
                    index = EMOJI_PICKER_CHOICES.index(emoji)
                    for _ in range(index):
                        await pilot.press("right")
                    self.assertEqual(app._emoji_picker.selected_emoji, emoji)
                    await pilot.press("enter")
                    await pilot.pause()

                    self.assertEqual(chat_input.value, f"hello {emoji}there")
                    self.assertEqual(
                        chat_input.cursor_position, len(f"hello {emoji}")
                    )
                    await pilot.press("!")
                    self.assertEqual(chat_input.value, f"hello {emoji}!there")

    async def test_picker_width_hugs_content_not_composer_width(self) -> None:
        """At XL (a wide composer), the picker must NOT stretch to the

        composer's full width -- it hugs its own rendered content plus
        the CSS's own border/padding only.
        """
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.pause()
            self.assertGreater(chat_input.region.width, emoji_picker_total_width() + 10)

            await pilot.press("ctrl+e")
            picker = app._emoji_picker
            await pilot.pause()

            self.assertEqual(picker.region.width, emoji_picker_total_width())
            self.assertLess(picker.region.width, chat_input.region.width)

    async def test_picker_right_border_geometry_at_xl(self) -> None:
        """A real widget/render-geometry regression, not only the pure

        emoji_picker_total_width() unit test: reads Textual's ACTUAL
        computed gutter (border + padding) and content_region for the
        mounted widget, and proves the outer region's right edge sits
        EXACTLY at content_region.right + the right gutter -- no
        one-cell drift, no phantom trailing column, regardless of what
        the pure width helper claims in isolation. Also proves the
        content area Textual actually allocated matches
        emoji_picker_content_width() exactly, so a future CSS change to
        border/padding without updating EMOJI_PICKER_BORDER_CELLS/
        EMOJI_PICKER_PADDING_CELLS would be caught here.
        """
        app = self.make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app.query_one("#chat-input", Input).focus()
            await pilot.press("ctrl+e")
            picker = app._emoji_picker
            await pilot.pause()

            gutter = picker.styles.gutter
            content_region = picker.content_region

            # The CSS's actual border+padding, read from Textual itself --
            # not assumed from the EMOJI_PICKER_BORDER_CELLS/PADDING_CELLS
            # constants. Right is deliberately 1 cell wider than left (see
            # EMOJI_PICKER_PADDING_CELLS): a spare, unambiguous column that
            # absorbs a real terminal rendering an ambiguous-width emoji
            # (like the heart) narrower than Python assumes.
            self.assertEqual(gutter.left, 2)
            self.assertEqual(gutter.right, 3)
            self.assertEqual(content_region.width, emoji_picker_content_width())

            # The fundamental invariant: outer width = content width +
            # both gutters, exactly -- no drift in either direction.
            self.assertEqual(
                picker.region.width, content_region.width + gutter.left + gutter.right
            )
            # The right border terminates immediately after the content
            # area plus its own padding -- never leaving a phantom blank
            # column, never clipping the last emoji.
            self.assertEqual(picker.region.right, content_region.right + gutter.right)
            self.assertEqual(picker.region.x + gutter.left, content_region.x)

    def test_picker_padding_absorbs_worst_case_ambiguous_width_undercount(self) -> None:
        """The real-hardware regression from PR #39: the right border was

        still visibly misaligned on an actual uConsole even after the
        calculate_popup_placement() screen-edge-clamp fix and even
        though test_picker_right_border_geometry_at_xl (a real widget-
        geometry read, not just the pure width helper) already passed.
        Neither of those catches the true root cause: "❤️"'s BASE
        codepoint (U+2764) has Unicode East Asian Width "Narrow" --
        unlike every other EMOJI_PICKER_CHOICES entry, which is "Wide"
        -- so a terminal that honors that raw property (rather than
        overriding to a wide emoji-presentation glyph the way this
        Python environment's own Rich cell_len() does) renders it in
        only 1 column, not the 2 emoji_picker_content_width() assumes.
        Since a picker row is emitted as one contiguous run of
        characters, that 1-column shortfall silently shifts everything
        drawn afterward in the SAME row -- including the picker's own
        right border -- left by exactly that much, relative to the
        pure-ASCII, unambiguous top/bottom border rows.

        This is computed independently from Unicode's own East Asian
        Width data, never from cell_len() (the exact function the
        insufficient earlier test relied on for both the width helper
        AND the geometry check), so it fails for the real defect even
        though every cell_len()-based number stays internally
        consistent.
        """

        def worst_case_width(emoji: str) -> int:
            base_width = unicodedata.east_asian_width(emoji[0])
            return cell_len(emoji) if base_width in ("W", "F") else 1

        optimistic_total = emoji_picker_content_width()
        worst_case_total = sum(
            1 + worst_case_width(emoji) + 1 for emoji in EMOJI_PICKER_CHOICES
        ) + max(0, len(EMOJI_PICKER_CHOICES) - 1)
        worst_case_shortfall = optimistic_total - worst_case_total

        # Sanity check on the premise itself: EMOJI_PICKER_CHOICES must
        # still contain a genuinely ambiguous-width glyph for this
        # regression to mean anything. If this ever fails because ❤️
        # was removed/replaced, the padding safety margin below may no
        # longer be needed -- that's a real finding, not a bug in the
        # test.
        self.assertGreater(worst_case_shortfall, 0)

        # The baseline (symmetric, 1 cell each side) padding is 2; any
        # amount beyond that is pure safety margin and must fully cover
        # the worst case, on the RIGHT side specifically (a shortfall
        # earlier in the row only ever pushes things further right,
        # never left, so only the right edge is at risk).
        self.assertGreaterEqual(EMOJI_PICKER_PADDING_CELLS - 2, worst_case_shortfall)

    async def test_picker_content_width_uses_rendered_cell_width_not_len(self) -> None:
        """❤️ is 5 Python characters (heart, U+FE0F variation selector

        counts as 1 char) but renders as 2 terminal cells -- the width
        calculation must use cell_len, never len().
        """
        naive_len_total = sum(1 + len(emoji) + 1 for emoji in EMOJI_PICKER_CHOICES) + (
            len(EMOJI_PICKER_CHOICES) - 1
        )
        correct_total = sum(1 + cell_len(emoji) + 1 for emoji in EMOJI_PICKER_CHOICES) + (
            len(EMOJI_PICKER_CHOICES) - 1
        )
        self.assertEqual(emoji_picker_content_width(), correct_total)
        self.assertEqual(
            emoji_picker_total_width(),
            correct_total + EMOJI_PICKER_BORDER_CELLS + EMOJI_PICKER_PADDING_CELLS,
        )
        # Every emoji here is a single Python character except ❤️ (heart
        # + variation selector, 2 characters) but ALL of them are wide
        # (2-cell) glyphs -- a naive len()-based sum undercounts the 12
        # single-character ones, so it comes out narrower than the
        # correct, cell-width-based total. Using len() here would size
        # the picker too small and clip content.
        self.assertLess(naive_len_total, correct_total)

    async def test_picker_width_deterministic_across_repeated_opens(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()

            widths = []
            for _ in range(3):
                await pilot.press("ctrl+e")
                await pilot.pause()
                widths.append(app._emoji_picker.region.width)
                await pilot.press("escape")
                await pilot.pause()

            self.assertEqual(widths, [emoji_picker_total_width()] * 3)

    async def test_picker_degrades_gracefully_in_a_narrow_viewport(self) -> None:
        """A composer narrower than the picker's natural content width

        must clamp the picker to stay inside the viewport -- never
        overflow off-screen, never corrupt layout.
        """
        app = self.make_app()
        async with app.run_test(size=(30, 20)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.pause()
            self.assertLess(chat_input.region.width, emoji_picker_total_width())

            await pilot.press("ctrl+e")
            picker = app._emoji_picker
            await pilot.pause()

            # Clamped against the real screen viewport (the same proven
            # calculate_popup_placement() the sender-action menu already
            # uses) -- never against the narrower composer width alone,
            # which is what actually matters: the picker must never
            # overflow off-screen, even if that means it ends up wider
            # than the composer itself in a very narrow layout.
            self.assertLessEqual(picker.region.width, app.screen.region.width)
            self.assertGreater(picker.region.width, 0)
            self.assertGreaterEqual(picker.region.x, app.screen.region.x)
            self.assertLessEqual(picker.region.right, app.screen.region.right)

    async def test_reply_then_emoji_workflow(self) -> None:
        """The full documented workflow: REPLY inserts the mention, then

        Ctrl+E inserts an emoji at the (now-advanced) cursor position,
        without disturbing the typed text in between.
        """
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    sender_node_id="!a11ce999",
                    sender_long_name="ALICE",
                    packet_id=12345,
                )
            )
            widget = next(
                w for w in app.query(ChatEntryWidget) if w.entry.node_id == "!a11ce999"
            )
            widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            menu = app._user_menu
            reply_index = next(
                i for i, item in enumerate(menu.items) if item.value == "reply"
            )
            for _ in range(len(menu.items) + 1):
                if menu.highlighted_index == reply_index:
                    break
                await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause()

            chat_input = app.query_one("#chat-input", Input)
            self.assertEqual(chat_input.value, "@ALICE ")

            chat_input.value = "@ALICE that's really cool "
            chat_input.cursor_position = len(chat_input.value)

            await pilot.press("ctrl+e")
            fire_index = EMOJI_PICKER_CHOICES.index("🔥")
            for _ in range(fire_index):
                await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(chat_input.value, "@ALICE that's really cool 🔥")
            self.assertTrue(chat_input.has_focus)

    async def test_theme_switching_updates_picker_palette(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app.query_one("#chat-input", Input).focus()
            await pilot.press("ctrl+e")
            picker = app._emoji_picker

            width_before = picker.region.width
            app._apply_color_theme("amber")
            await pilot.pause()
            palette = THEME_PALETTES["amber"]
            self.assertEqual(picker._base_color, palette.base)
            self.assertEqual(picker._accent_color, palette.accent)
            self.assertEqual(picker.region.width, width_before)
            self.assertEqual(picker.region.width, emoji_picker_total_width())

    async def test_tab_switch_closes_the_picker(self) -> None:
        app = self.make_app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app.query_one("#chat-input", Input).focus()
            await pilot.press("ctrl+e")
            self.assertIsNotNone(app._emoji_picker)

            app.show_tab("mesh")
            await pilot.pause()

            self.assertIsNone(app._emoji_picker)
            self.assertEqual(len(app.screen.query(EmojiPicker)), 0)

    async def test_emoji_insertion_generates_no_radio_traffic(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            await pilot.press("ctrl+e", "enter")
            await pilot.pause()

            self.assertEqual(radio.sent_messages, ())


if __name__ == "__main__":
    unittest.main()
