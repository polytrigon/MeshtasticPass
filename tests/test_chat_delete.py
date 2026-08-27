"""Tests for the CHAT DEL action: local-only message deletion for

FAILED/UNCONFIRMED outgoing entries, and safe retirement of any
pending delivery tracking for the deleted send attempt.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from textual.widgets import Input

from app import ChatEntryWidget, MeshtasticPassApp, can_manual_resend
from app_settings import AppSettings
from chat_store import ChatStore
from radio_service import DeliveryState, SendStatus
from simulated_radio_service import SimulatedRadioService
from tests.test_app import ControllableSendRadioService


class ChatDeleteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json",
            profile_path=root / "terminal.conf",
        )
        self.chat_db_path = root / "chat.db"

    @staticmethod
    def radio() -> SimulatedRadioService:
        return SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )

    async def _navigate_to_action(
        self, pilot, app: MeshtasticPassApp, entry, action: str
    ) -> None:
        widget = next(w for w in app.query(ChatEntryWidget) if w.entry is entry)
        widget.focus()
        await pilot.pause()
        for _ in range(3):
            if getattr(app.focused, "action", None) == action:
                return
            await pilot.press("down")
            await pilot.pause()
        self.fail(f"never reached the {action!r} action control")

    async def test_del_removes_exactly_the_selected_entry(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            entry = app._start_outgoing("gone soon")
            app._set_delivery_state(entry, DeliveryState.FAILED)
            await pilot.pause()
            self.assertTrue(can_manual_resend(entry))

            await self._navigate_to_action(pilot, app, entry, "delete")
            await pilot.press("enter")
            await pilot.pause()

            self.assertNotIn(entry, app.chat_history)
            self.assertTrue(entry.deleted)
            self.assertEqual(
                [w for w in app.query(ChatEntryWidget) if w.entry is entry], []
            )

    async def test_del_persists_across_restart(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        app = MeshtasticPassApp(self.radio(), self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            entry = app._start_outgoing("delete then restart")
            app._set_delivery_state(entry, DeliveryState.UNCONFIRMED)
            await pilot.pause()
            message_id = entry.message_id
            self.assertIsNotNone(message_id)

            await self._navigate_to_action(pilot, app, entry, "delete")
            await pilot.press("enter")
            await pilot.pause()

        second_store = ChatStore.open(self.chat_db_path)
        self.addCleanup(second_store.close)
        second_radio = self.radio()
        second_app = MeshtasticPassApp(second_radio, self.settings, chat_store=second_store)
        async with second_app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(
                [e for e in second_app.chat_history if e.message_id == message_id], []
            )

    async def test_del_generates_zero_radio_traffic(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            entry = app._start_outgoing("no traffic please")
            app._set_delivery_state(entry, DeliveryState.FAILED)
            await pilot.pause()
            before = radio.sent_messages

            await self._navigate_to_action(pilot, app, entry, "delete")
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(radio.sent_messages, before)

    async def test_del_retires_pending_delivery_tracking(self) -> None:
        """A real RadioService's own _pending_sends correlation for THIS

        entry's packet is popped by DEL -- proven directly against the
        dict itself, since the test radio fixture used elsewhere
        (ControllableSendRadioService) deliberately bypasses
        _pending_sends entirely (see its own docstring) and so cannot
        exercise this specific real-radio-layer cleanup.
        """
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            entry = app._start_outgoing("retire my tracking")
            app._set_delivery_state(entry, DeliveryState.UNCONFIRMED, packet_id=4242)
            await pilot.pause()
            fake_pending = {}
            app.radio._pending_sends = fake_pending
            fake_pending[4242] = lambda status: None

            await self._navigate_to_action(pilot, app, entry, "delete")
            await pilot.press("enter")
            await pilot.pause()

            self.assertNotIn(4242, fake_pending)

    async def test_late_ack_after_del_is_ignored(self) -> None:
        """UNCONFIRMED -> DEL -> a late clean remote routing response

        must find the message already gone and do nothing: no
        resurrection, no mutation, no crash.
        """
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            chat_input.value = "late ack after delete"
            await pilot.press("enter")
            await pilot.pause()
            entry = app.chat_history[-1]
            packet_id = entry.packet_id
            self.assertIsNotNone(packet_id)
            app._set_delivery_state(entry, DeliveryState.UNCONFIRMED)
            await pilot.pause()

            await self._navigate_to_action(pilot, app, entry, "delete")
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(entry.deleted)
            history_before = list(app.chat_history)

            radio.fire_status(packet_id, SendStatus(DeliveryState.HEARD, packet_id))
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.UNCONFIRMED)
            self.assertEqual(app.chat_history, history_before)
            self.assertEqual(
                [w for w in app.query(ChatEntryWidget) if w.entry is entry], []
            )

    async def test_late_nak_after_del_is_ignored(self) -> None:
        """FAILED -> DEL -> a late routing response (any outcome) must

        also be ignored -- same guard, same result as the UNCONFIRMED
        case above.
        """
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            chat_input = app.query_one("#chat-input", Input)
            chat_input.focus()
            chat_input.value = "late nak after delete"
            await pilot.press("enter")
            await pilot.pause()
            entry = app.chat_history[-1]
            packet_id = entry.packet_id
            self.assertIsNotNone(packet_id)
            app._set_delivery_state(entry, DeliveryState.FAILED)
            await pilot.pause()

            await self._navigate_to_action(pilot, app, entry, "delete")
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(entry.deleted)

            radio.fire_status(packet_id, SendStatus(DeliveryState.FAILED, packet_id))
            await pilot.pause()

            self.assertEqual(
                [w for w in app.query(ChatEntryWidget) if w.entry is entry], []
            )
            self.assertNotIn(entry, app.chat_history)

    async def test_deleting_original_does_not_delete_its_resend(self) -> None:
        """RESEND mutates the SAME ChatEntry in place (see _rebroadcast)

        rather than creating a copy -- so this test's "resend" is
        necessarily a SEPARATE, independently-composed second outgoing
        message, proving DEL's identity-based removal (by object/
        message_id, never by text/timestamp) leaves an unrelated entry
        with similar provenance completely untouched.
        """
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            original = app._start_outgoing("shared wording")
            app._set_delivery_state(original, DeliveryState.FAILED)
            other = app._start_outgoing("shared wording")
            app._set_delivery_state(other, DeliveryState.UNCONFIRMED)
            await pilot.pause()

            await self._navigate_to_action(pilot, app, original, "delete")
            await pilot.press("enter")
            await pilot.pause()

            self.assertNotIn(original, app.chat_history)
            self.assertIn(other, app.chat_history)
            self.assertFalse(other.deleted)
            self.assertEqual(
                [w for w in app.query(ChatEntryWidget) if w.entry is other]
                != [],
                True,
            )

    async def test_deleting_the_other_message_does_not_delete_first(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            first = app._start_outgoing("shared wording")
            app._set_delivery_state(first, DeliveryState.FAILED)
            second = app._start_outgoing("shared wording")
            app._set_delivery_state(second, DeliveryState.FAILED)
            await pilot.pause()

            await self._navigate_to_action(pilot, app, second, "delete")
            await pilot.press("enter")
            await pilot.pause()

            self.assertIn(first, app.chat_history)
            self.assertFalse(first.deleted)
            self.assertNotIn(second, app.chat_history)

    async def test_esc_from_actions_deletes_nothing(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            app.show_tab("chat")
            entry = app._start_outgoing("do not delete me")
            app._set_delivery_state(entry, DeliveryState.FAILED)
            await pilot.pause()

            await self._navigate_to_action(pilot, app, entry, "delete")
            await pilot.press("escape")
            await pilot.pause()

            self.assertIn(entry, app.chat_history)
            self.assertFalse(entry.deleted)
            self.assertEqual(
                [w for w in app.query(ChatEntryWidget) if w.entry is entry] != [], True
            )


if __name__ == "__main__":
    unittest.main()
