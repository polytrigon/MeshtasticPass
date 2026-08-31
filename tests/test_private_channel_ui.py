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


class NewChannelEditorInteractionTests(PrivateChannelUiBase):
    async def _open_editor(self, pilot, app) -> None:
        app.show_tab("chat")
        await pilot.pause()
        selector = app.query_one(ChannelSelector)
        selector.focus()
        await pilot.press("enter")
        await pilot.pause()
        selector._activate_popup_item(len(selector.options) - 1, None)
        await pilot.pause()

    async def test_new_channel_editor_is_rendered_and_focuses_name(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._open_editor(pilot, app)
            editor = app.query_one("#new-channel-editor")
            self.assertTrue(editor.display)
            self.assertEqual(app.focused.id, "new-channel-name")

    async def test_type_name_key_navigate_save_creates_pending_only(self) -> None:
        radio = ControllableSendRadioService()
        app = self._make_app(radio)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._open_editor(pilot, app)
            name_input = app.query_one("#new-channel-name")
            name_input.focus()
            name_input.value = "Secret Society"
            await pilot.pause()
            await pilot.press("enter")  # name -> key
            await pilot.pause()
            self.assertEqual(app.focused.id, "new-channel-key")
            await pilot.press("down")  # key -> SAVE (not CANCEL)
            await pilot.pause()
            self.assertEqual(app.focused.id, "new-channel-save")
            await pilot.press("right")  # SAVE -> CANCEL
            await pilot.pause()
            self.assertEqual(app.focused.id, "new-channel-cancel")
            await pilot.press("left")  # CANCEL -> SAVE
            await pilot.pause()
            self.assertEqual(app.focused.id, "new-channel-save")
            await pilot.press("enter")  # save (blank key -> generated)
            await pilot.pause()
            self.assertIsNotNone(app._pending_channel)
            self.assertNotIn("Secret Society", [c.name for c in app._channels])
            # Zero RF/config.
            self.assertEqual(radio.sent_texts, [])

    async def test_escape_from_anywhere_cancels_editor(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._open_editor(pilot, app)
            app.query_one("#new-channel-name").value = "Some"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app._new_channel_editor_open)
            self.assertIsNone(app._pending_channel)
            self.assertFalse(app.query_one("#new-channel-editor").display)

    async def test_reopen_editor_starts_clean(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._open_editor(pilot, app)
            app.query_one("#new-channel-name").value = "Secret"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await self._open_editor(pilot, app)
            await pilot.pause()
            name_input = app.query_one("#new-channel-name")
            self.assertEqual(name_input.value, "")

    async def test_invalid_save_preserves_values_and_keeps_editor(self) -> None:
        radio = ControllableSendRadioService()
        app = self._make_app(radio)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._open_editor(pilot, app)
            app.query_one("#new-channel-name").value = "Society"
            await pilot.pause()
            await pilot.press("enter")  # name -> key
            await pilot.pause()
            app.query_one("#new-channel-key").value = "!badkey"
            await pilot.pause()
            await pilot.press("down", "down")  # -> save
            await pilot.pause()
            await pilot.press("enter")  # save with invalid key
            await pilot.pause()
            self.assertTrue(app._new_channel_editor_open)
            self.assertEqual(app._new_channel_error, "INVALID KEY")
            self.assertIsNone(app._pending_channel)
            self.assertEqual(app.query_one("#new-channel-name").value, "Society")
            self.assertEqual(radio.sent_texts, [])

    async def test_new_channel_shares_new_preset_form_geometry(self) -> None:
        from app import NetworkFieldInput

        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._open_editor(pilot, app)
            # NEW CHANNEL NAME/KEY are the SAME NetworkFieldInput rows NEW PRESET
            # uses, with the same 16-column field geometry.
            self.assertIsInstance(app.query_one("#new-channel-name-row"), NetworkFieldInput)
            self.assertIsInstance(app.query_one("#new-channel-key-row"), NetworkFieldInput)
            self.assertEqual(int(app.query_one("#new-channel-name").styles.width.value), 16)
            self.assertEqual(int(app.query_one("#new-channel-key").styles.width.value), 16)
            # NEW PRESET form uses the same component + geometry.
            self.assertIsInstance(app.query_one("#network-name-row"), NetworkFieldInput)
            self.assertEqual(int(app.query_one("#network-name-input").styles.width.value), 16)
            self.assertEqual(int(app.query_one("#key-input").styles.width.value), 16)
            # Label column shares the same 13-cell width.
            self.assertEqual(int(app.query_one("#new-channel-name-row .connection-label").styles.width.value), 13)
            self.assertEqual(int(app.query_one("#network-name-row .connection-label").styles.width.value), 13)

    async def test_new_channel_row_order_and_shared_form_primitives(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._open_editor(pilot, app)
            editor = app.query_one("#new-channel-editor")
            self.assertTrue(editor.has_class("editor-form"))
            children = list(editor.children)
            idx_name = next(
                i for i, w in enumerate(children) if w.id == "new-channel-name-row"
            )
            idx_key = next(
                i for i, w in enumerate(children) if w.id == "new-channel-key-row"
            )
            idx_actions = next(
                i for i, w in enumerate(children) if w.id == "new-channel-actions"
            )
            idx_hint = next(
                i for i, w in enumerate(children) if w.has_class("editor-hint")
            )
            # Correct order: NAME, then KEY (adjacent, no hidden spacer), then
            # SAVE/CANCEL, then the hint below.
            self.assertLess(idx_name, idx_key)
            self.assertEqual(idx_key, idx_name + 1)
            self.assertLess(idx_key, idx_actions)
            self.assertLess(idx_actions, idx_hint)
            # Exact labels / hint text per the layout spec.
            self.assertEqual(
                str(app.query_one("#new-channel-key-row .connection-label").render()),
                "CHANNEL KEY",
            )
            self.assertEqual(
                str(app.query_one(".editor-hint").render()),
                "LEAVING KEY BLANK WILL CREATE A NEW CHANNEL",
            )
            # SAVE / CANCEL visible; actions share NEW PRESET's editor-actions.
            self.assertTrue(app.query_one("#new-channel-save").display)
            self.assertTrue(app.query_one("#new-channel-cancel").display)
            self.assertTrue(app.query_one("#new-channel-actions").has_class("editor-actions"))
            # No APPLY control remains in the editor actions row.
            self.assertEqual(
                [c.id for c in app.query_one("#new-channel-actions").children],
                ["new-channel-save", "new-channel-cancel"],
            )
            # New PRESET uses the SAME sharing primitives and stays intact.
            app.show_tab("connection")
            await pilot.pause()
            self.assertTrue(app.query_one("#advanced-radio-actions").has_class("editor-actions"))
            self.assertTrue(app.query_one("#network-name-row").has_class("connection-action-row"))

    async def test_editor_is_dedicated_blank_view_hiding_transcript_and_composer(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._open_editor(pilot, app)
            switcher = app.query_one("#chat-channel-content")
            self.assertEqual(switcher.current, "new-channel-editor")
            self.assertFalse(app.query_one("#chat-conversation").display)

    async def test_cancel_restores_normal_chat_view(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await self._open_editor(pilot, app)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.query_one("#chat-channel-content").current, "chat-conversation")
            self.assertTrue(app.query_one("#chat-conversation").display)


class ConfiguredPskMetadataTests(PrivateChannelUiBase):
    def _radio_with_channels(self, psk: bytes) -> "object":
        from types import SimpleNamespace
        from radio_service import RadioService

        service = RadioService()
        channel = SimpleNamespace(index=0, settings=SimpleNamespace(psk=psk))
        service._interface = SimpleNamespace(
            localNode=SimpleNamespace(channels=[channel])
        )
        return service

    def test_public_sentinel_psk_is_not_metadata(self) -> None:
        # 0x01 = default public channel PSK -> no private metadata.
        service = self._radio_with_channels(b"\x01")
        self.assertIsNone(service.channel_psk_text(0))

    def test_no_encryption_sentinel_is_not_metadata(self) -> None:
        service = self._radio_with_channels(b"\x00")
        self.assertIsNone(service.channel_psk_text(0))

    def test_real_16_byte_psk_metadata_is_normalized_base64(self) -> None:
        import base64 as _b64

        raw = bytes(range(16))
        service = self._radio_with_channels(raw)
        self.assertEqual(service.channel_psk_text(0), _b64.b64encode(raw).decode("ascii"))

    async def test_private_psk_metadata_is_ui_only_and_default_channel_uncluttered(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            # The simulated radio offers only public channels (no private PSK),
            # so the metadata helper must return "" for the default channel.
            self.assertEqual(app._channel_psk_metadata_text(), "")


class PrivateChannelApplyTests(unittest.TestCase):
    """Fake-SDK boundary tests for RadioService.apply_private_channel.

    Proves find-slot → write only the intended channel → readback → verify,
    and that failures never promote. No live hardware.
    """

    def _fake_radio(self, *, roles, name="", psk=b""):
        from types import SimpleNamespace

        from meshtastic.protobuf import channel_pb2
        from radio_service import RadioService

        channels = []
        for i, role in enumerate(roles):
            ch = channel_pb2.Channel()
            ch.index = i
            ch.role = role
            if role != channel_pb2.Channel.Role.DISABLED:
                ch.settings.name = name
                ch.settings.psk = psk
            channels.append(ch)
        written = {}

        class Ack:
            receivedNak = False

        class LocalNode:
            def __init__(self):
                self.channels = channels
                self._ack = Ack()

            def onAckNak(self, _packet):
                pass

            def _sendAdmin(self, message, onResponse=None):
                if message.HasField("set_channel"):
                    written["channel"] = message.set_channel
                    onResponse({"decoded": {}})
                elif message.get_channel_request:
                    slot = message.get_channel_request - 1
                    ch = channel_pb2.Channel()
                    ch.index = slot
                    ch.settings.name = written["channel"].settings.name
                    ch.settings.psk = written["channel"].settings.psk
                    holder = SimpleNamespace(
                        HasField=lambda _f: True,
                        get_channel_response=ch,
                    )
                    onResponse(
                        {"decoded": {"portnum": "ADMIN_APP", "admin": {"raw": holder}}}
                    )

        service = RadioService()
        service._interface = SimpleNamespace(
            localNode=LocalNode(),
            _acknowledgment=Ack(),
        )
        # connection_lost Event must not be set.
        service._connection_lost = __import__("threading").Event()
        return service, written

    def test_not_connected(self) -> None:
        from radio_service import RadioService

        service = RadioService()
        result = service.apply_private_channel(name="Secret", psk=b"\x01" * 16)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_connected")

    def test_no_available_slot_is_non_destructive(self) -> None:
        from meshtastic.protobuf import channel_pb2

        service, written = self._fake_radio(
            roles=[channel_pb2.Channel.Role.PRIMARY] * 8
        )
        result = service.apply_private_channel(name="Secret", psk=b"\x01" * 16)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_channel_slot")
        self.assertEqual(written, {})

    def test_writes_first_disabled_slot_then_verifies_and_promotes(self) -> None:
        from meshtastic.protobuf import channel_pb2

        service, written = self._fake_radio(
            roles=[
                channel_pb2.Channel.Role.PRIMARY,
                channel_pb2.Channel.Role.DISABLED,
            ]
        )
        psk = bytes(range(16))
        result = service.apply_private_channel(name="Secret Society", psk=psk)
        self.assertTrue(result.ok)
        self.assertEqual(result.slot, 1)
        # Only the intended channel was written (slot 1), name/psk exact.
        wrote = written["channel"]
        self.assertEqual(wrote.index, 1)
        self.assertEqual(wrote.settings.name, "Secret Society")
        self.assertEqual(bytes(wrote.settings.psk), psk)

    def test_mismatch_does_not_promote(self) -> None:
        from meshtastic.protobuf import channel_pb2

        service, _ = self._fake_radio(
            roles=[channel_pb2.Channel.Role.PRIMARY, channel_pb2.Channel.Role.DISABLED]
        )
        service._interface.localNode._sendAdmin = lambda message, onResponse=None: None
        result = service.apply_private_channel(
            name="Secret Society", psk=bytes(range(16)), timeout=0.01
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")


class ApplyPendingChannelTests(PrivateChannelUiBase):
    def _seed_pending(self, app) -> "PendingChannelConfig":
        app._start_new_channel()
        return app._save_new_channel("Secret Society", "")

    async def test_save_online_auto_applies_and_promotes(self) -> None:
        from radio_service import ChannelInfo, PrivateChannelApplyResult, RadioState

        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._radio_state = RadioState.ONLINE
            app._channels = (ChannelInfo(0, "MEDIUMSLOW"),)
            app._start_new_channel()
            await pilot.pause()
            app.query_one("#new-channel-name").value = "SECRET"
            app.radio.apply_private_channel = lambda name, psk: PrivateChannelApplyResult(
                True, 1, ""
            )
            app.radio.get_config_channels = lambda: (
                ChannelInfo(0, "MEDIUMSLOW"),
                ChannelInfo(1, "SECRET"),
            )
            app.new_channel_save(None)
            for _ in range(20):
                await pilot.pause()
                if app._pending_channel is None:
                    break
            self.assertIsNone(app._pending_channel)
            self.assertIn("SECRET", [c.name for c in app._channels])

    async def test_apply_refreshes_selector_with_real_secret_name(self) -> None:
        from radio_service import ChannelInfo, PrivateChannelApplyResult, RadioState

        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._radio_state = RadioState.ONLINE
            app._channels = (ChannelInfo(0, "MEDIUMSLOW"),)
            app._start_new_channel()
            await pilot.pause()
            app.query_one("#new-channel-name").value = "SECRET"
            app.radio.apply_private_channel = lambda name, psk: PrivateChannelApplyResult(
                True, 1, ""
            )
            app.radio.get_config_channels = lambda: (
                ChannelInfo(0, "MEDIUMSLOW"),
                ChannelInfo(1, "SECRET"),
            )
            app.new_channel_save(None)
            for _ in range(20):
                await pilot.pause()
                if app._pending_channel is None:
                    break
            await pilot.pause()
            selector = app.query_one(ChannelSelector)
            # Selector options are the authoritative channels, never a raw index.
            self.assertEqual([o.value for o in selector.options], [0, 1])
            self.assertEqual([o.label for o in selector.options], ["MEDIUMSLOW", "SECRET"])
            # Heading shows the real channel name, never "1".
            self.assertEqual(selector.selected_label, "SECRET")
            self.assertEqual(app.current_channel_index, 1)
            # Opening the dropdown appends NEW CHANNEL after the real channels.
            selector.focus()
            await pilot.press("enter")
            await pilot.pause()
            labels = [o.label for o in selector.options]
            self.assertEqual(labels, ["MEDIUMSLOW", "SECRET", "NEW CHANNEL"])
            await pilot.press("escape")
            await pilot.pause()

    async def test_switch_secret_mediumslow_secret_works_zero_writes(self) -> None:
        from radio_service import ChannelInfo, PrivateChannelApplyResult, RadioState

        radio = ControllableSendRadioService()
        app = self._make_app(radio)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._radio_state = RadioState.ONLINE
            app._channels = (ChannelInfo(0, "MEDIUMSLOW"),)
            app._start_new_channel()
            await pilot.pause()
            app.query_one("#new-channel-name").value = "SECRET"
            app.radio.apply_private_channel = lambda name, psk: PrivateChannelApplyResult(
                True, 1, ""
            )
            app.radio.get_config_channels = lambda: (
                ChannelInfo(0, "MEDIUMSLOW"),
                ChannelInfo(1, "SECRET"),
            )
            app.new_channel_save(None)
            for _ in range(20):
                await pilot.pause()
                if app._pending_channel is None:
                    break
            self.assertEqual(app.current_channel_index, 1)
            # SECRET -> MEDIUMSLOW -> SECRET, all local, zero writes/RF.
            await app._switch_channel(0)
            await pilot.pause()
            self.assertEqual(app.current_channel_index, 0)
            self.assertIn("SECRET", [c.name for c in app._channels])
            await app._switch_channel(1)
            await pilot.pause()
            self.assertEqual(app.current_channel_index, 1)
            self.assertEqual(radio.sent_texts, [])

    async def test_save_offline_is_zero_write_and_keeps_editor(self) -> None:
        from radio_service import RadioState

        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._radio_state = RadioState.OFFLINE
            app._start_new_channel()
            await pilot.pause()
            app.query_one("#new-channel-name").value = "Secret Society"
            app.radio.apply_private_channel = lambda name, psk: (_ for _ in ()).throw(
                AssertionError("must not write while offline")
            )
            app.new_channel_save(None)
            await pilot.pause()
            self.assertEqual(app._new_channel_error, "RADIO NOT ONLINE")
            self.assertIsNone(app._pending_channel)

    async def test_apply_success_promotes_to_real_channel(self) -> None:
        from radio_service import ChannelInfo, PrivateChannelApplyResult

        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pending = self._seed_pending(app)
            self.assertIsNotNone(pending)
            app.radio.apply_private_channel = lambda name, psk: PrivateChannelApplyResult(
                True, 2, ""
            )
            app.radio.get_config_channels = lambda: (
                ChannelInfo(0, "LongFast"),
                ChannelInfo(2, "Secret Society"),
            )
            app._activate_new_channel_apply()
            for _ in range(20):
                await pilot.pause()
                if app._pending_channel is None:
                    break
            # Pending cleared; promoted real channel in the selector; CHAT on it.
            self.assertIsNone(app._pending_channel)
            self.assertIn("Secret Society", [c.name for c in app._channels])
            self.assertEqual(app.current_channel_index, 2)

    async def test_apply_failure_preserves_pending_and_shows_error(self) -> None:
        from radio_service import PrivateChannelApplyResult

        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            pending = self._seed_pending(app)
            self.assertIsNotNone(pending)
            app.radio.apply_private_channel = lambda name, psk: PrivateChannelApplyResult(
                False, 1, "timeout"
            )
            app._activate_new_channel_apply()
            for _ in range(20):
                await pilot.pause()
                if not app._pending_apply_active:
                    break
            # Pending preserved; no promotion; compact error shown.
            self.assertIsNotNone(app._pending_channel)
            self.assertEqual(app._new_channel_error, "timeout")
            self.assertNotIn("Secret Society", [c.name for c in app._channels])

    async def test_apply_offline_is_zero_write(self) -> None:
        from radio_service import RadioState

        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self._seed_pending(app)
            app._radio_state = RadioState.OFFLINE
            app.radio.apply_private_channel = lambda name, psk: (_ for _ in ()).throw(
                AssertionError("radio apply must not run while offline")
            )
            app._activate_new_channel_apply()
            await pilot.pause()
            self.assertEqual(app._new_channel_error, "RADIO NOT ONLINE")
            self.assertIsNotNone(app._pending_channel)


class PskHeaderTests(PrivateChannelUiBase):
    async def test_public_channel_omits_psk_and_no_strip(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.pause()
            self.assertEqual(str(app.query_one("#chat-channel-psk").render()), "")
            # No separate PSK strip is rendered for a configured public channel.
            self.assertFalse(app.query_one("#new-channel-pending").display)

    async def test_configured_private_channel_shows_inline_psk_in_header(self) -> None:
        import base64 as _b64

        from radio_service import ChannelInfo

        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            raw = bytes(range(16))
            app._channels = (ChannelInfo(0, "SECRET"),)
            app.current_channel_index = 0
            # Fake radio exposing a matching configured PSK for channel 0.
            from types import SimpleNamespace

            app.radio.channel_psk_text = lambda index: _b64.b64encode(raw).decode("ascii")
            app._refresh_channel_psk_header()
            await pilot.pause()
            text = str(app.query_one("#chat-channel-psk").render())
            self.assertIn("PSK", text)
            self.assertIn(_b64.b64encode(raw).decode("ascii"), text)
            # The pending strip is separate and hidden for a configured channel.
            self.assertFalse(app.query_one("#new-channel-pending").display)

    async def test_pending_psk_presentation_is_separate_from_header(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._start_new_channel()
            await pilot.pause()
            app._save_new_channel("SECRET", "")
            await pilot.pause()
            # Pending strip still shows the NOT YET APPLIED + PSK; header is
            # empty (the pending config is NOT a configured channel).
            self.assertTrue(app.query_one("#new-channel-pending").display)
            self.assertEqual(str(app.query_one("#chat-channel-psk").render()), "")


if __name__ == "__main__":
    unittest.main()
