"""Focused tests for the simulator-only CHAT NEW CHANNEL private-channel UI.

Proves the CHAT-local editor state machine, the pending (not-yet-radio-
configured) channel model, PSK generation/validation, and that the whole
workflow performs zero radio/config writes. No live radio; the radio-write
boundary is deliberately out of scope (see channel_psk / radio_service).
"""

from __future__ import annotations

import base64
from pathlib import Path
import tempfile
import unittest

from app import (
    ChannelSelector,
    MeshtasticPassApp,
    NEW_CHANNEL_ACTION_VALUE,
)
from app_settings import AppSettings
from channel_psk import generate_private_psk, normalize_private_psk
from chat_store import ChatStore
from simulated_radio_service import SimulatedRadioService
from tests.test_app import ControllableSendRadioService


def _simulated_radio(**kwargs) -> SimulatedRadioService:
    return SimulatedRadioService(
        connect_delay=0, message_interval=0, scripted_messages=(), **kwargs
    )


class PrivateChannelUiBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.store = ChatStore.open(self.root / "chat.db")
        self.addCleanup(self.store.close)

    def _make_app(self, radio=None):
        return MeshtasticPassApp(
            radio or _simulated_radio(), self.settings, chat_store=self.store
        )


class NewChannelActionTests(PrivateChannelUiBase):
    async def test_new_channel_is_an_action_row_never_a_real_channel(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            selector = app.query_one(ChannelSelector)
            selector.focus()
            await pilot.press("enter")
            await pilot.pause()
            options = list(selector.options)
            self.assertGreater(len(options), 0)
            self.assertEqual(options[-1].value, NEW_CHANNEL_ACTION_VALUE)
            self.assertEqual(options[-1].label, "NEW CHANNEL")
            # The closed selector still shows a real channel index, never the
            # sentinel (NEW CHANNEL is not a selectable configured channel).
            self.assertNotEqual(selector.value, NEW_CHANNEL_ACTION_VALUE)
            # Selecting NEW CHANNEL opens the editor (action, not a channel).
            selector._activate_popup_item(len(options) - 1, None)
            await pilot.pause()
            self.assertTrue(app._new_channel_editor_open)
            self.assertIsNone(app._pending_channel)


class PrivateChannelStateMachineTests(PrivateChannelUiBase):
    async def test_start_cancel_toggle_editor(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._start_new_channel()
            self.assertTrue(app._new_channel_editor_open)
            self.assertIsNone(app._pending_channel)
            app._cancel_new_channel()
            self.assertFalse(app._new_channel_editor_open)
            self.assertIsNone(app._pending_channel)

    async def test_save_blank_key_generates_a_32_byte_psk(self) -> None:
        radio = ControllableSendRadioService()
        app = self._make_app(radio)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._start_new_channel()
            pending = app._save_new_channel("Secret Society", "")
            self.assertIsNotNone(pending)
            self.assertEqual(pending.name, "Secret Society")
            self.assertTrue(pending.generated)
            self.assertEqual(len(base64.b64decode(pending.psk_base64)), 32)
            # Zero RF / zero writes.
            self.assertEqual(radio.sent_texts, [])

    async def test_save_supplied_valid_key_normalizes(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._start_new_channel()
            raw = bytes(range(16))
            b64 = base64.b64encode(raw).decode("ascii")
            pending = app._save_new_channel("Society", b64)
            self.assertIsNotNone(pending)
            self.assertFalse(pending.generated)
            self.assertEqual(pending.raw_psk, raw)
            self.assertEqual(pending.psk_base64, b64)

    async def test_save_invalid_key_keeps_editor_with_error_and_zero_writes(self) -> None:
        radio = ControllableSendRadioService()
        app = self._make_app(radio)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._start_new_channel()
            pending = app._save_new_channel("Society", "!!!not-base64!!!")
            self.assertIsNone(pending)
            self.assertTrue(app._new_channel_editor_open)
            self.assertEqual(app._new_channel_error, "INVALID KEY")
            # Pending draft is NOT represented as radio-configured.
            self.assertIsNone(app._pending_channel)
            self.assertEqual([c.name for c in app._channels], ["Channel 1"])
            self.assertEqual(radio.sent_texts, [])

    async def test_save_empty_name_keeps_editor(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._start_new_channel()
            pending = app._save_new_channel("   ", "")
            self.assertIsNone(pending)
            self.assertTrue(app._new_channel_editor_open)
            self.assertEqual(app._new_channel_error, "CHANNEL NAME REQUIRED")

    async def test_pending_never_reaches_configured_channels(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._start_new_channel()
            app._save_new_channel("Secret Society", "")
            # The pending draft is app-side only -- `_channels` (the radio's
            # configured channels) is unchanged and does NOT gain a slot for
            # the pending private channel. No radio-authoritative ChannelInfo
            # was fabricated for it.
            self.assertNotIn(
                "Secret Society", [c.name for c in app._channels]
            )
            self.assertIsNotNone(app._pending_channel)

    async def test_escape_cancels_editor_before_save(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            app._start_new_channel()
            self.assertTrue(app._new_channel_editor_open)
            self.assertIsNone(app._pending_channel)
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app._new_channel_editor_open)
            self.assertIsNone(app._pending_channel)


class PskHelperTests(unittest.TestCase):
    def test_generate_private_psk_is_32_byte_base64(self) -> None:
        psk = generate_private_psk()
        self.assertEqual(len(base64.b64decode(psk)), 32)

    def test_normalize_rejects_nonsense(self) -> None:
        self.assertIsNone(normalize_private_psk("!!!"))


if __name__ == "__main__":
    unittest.main()
