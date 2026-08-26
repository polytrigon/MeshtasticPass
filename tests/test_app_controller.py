"""Hardware-free tests for the non-visual TUI controller."""

from __future__ import annotations

from dataclasses import replace
from threading import Event
import unittest
from unittest.mock import Mock, patch

from chat_store import StoredMessage
from geo import GeoPosition
from app_controller import (
    RadioMonitor,
    create_radio_service,
    outgoing_chat_entry,
    received_chat_entry,
    stored_chat_entry,
)
from radio_service import DeliveryState, RadioEvent, RadioState, ReceivedMessage


def make_stored_message(**overrides: object) -> StoredMessage:
    defaults: dict[str, object] = dict(
        id=1,
        direction="outgoing",
        packet_id=None,
        node_id=None,
        sender_name=None,
        sender_short_name=None,
        channel_index=0,
        text="hello",
        origin_sent_at=None,
        radio_rx_at=None,
        received_at=1_700_000_000.0,
        local_sent_at=1_700_000_000.0,
        delivery_state="SENDING",
        created_at=1_700_000_000.0,
    )
    defaults.update(overrides)
    return StoredMessage(**defaults)


class AppControllerTests(unittest.TestCase):
    def test_selects_simulated_service(self) -> None:
        with patch("app_controller.SimulatedRadioService") as simulator:
            selected = create_radio_service(True, "/dev/test")

        self.assertIs(selected, simulator.return_value)
        simulator.assert_called_once_with(device_path="/dev/test")

    def test_selects_real_service_and_device(self) -> None:
        with patch("app_controller.RadioService") as real_service:
            selected = create_radio_service(False, "/dev/ttyACM0")

        self.assertIs(selected, real_service.return_value)
        real_service.assert_called_once_with(device_path="/dev/ttyACM0")

    def test_received_message_prefers_name_then_node_id(self) -> None:
        named = self.make_message(long_name="Alice Trail", short_name="ALCE")
        fallback = self.make_message(long_name=None, short_name=None)

        entry = received_chat_entry(
            named,
            app_received_at=1_700_000_000.0,
            monotonic_now=123.0,
            unread=True,
            is_new=True,
        )
        self.assertEqual(entry.author, "Alice Trail")
        self.assertIsNone(entry.origin_sent_at)
        self.assertIsNone(entry.radio_rx_at)
        self.assertIsNone(entry.local_sent_at)
        self.assertEqual(entry.app_received_at, 1_700_000_000.0)
        self.assertEqual(entry.age_reference, 123.0)
        self.assertTrue(entry.age_is_receive_time)
        self.assertTrue(entry.unread)
        self.assertTrue(entry.is_new)
        self.assertIsNone(entry.distance_miles)
        self.assertEqual(received_chat_entry(fallback).author, "!a11ce001")
        self.assertFalse(entry.outgoing)

    def test_outgoing_entry_uses_you_marker(self) -> None:
        entry = outgoing_chat_entry(
            "hello",
            app_received_at=1_700_000_000.0,
            monotonic_now=456.0,
        )

        self.assertEqual(entry.author, "YOU")
        self.assertEqual(entry.text, "hello")
        self.assertIsNone(entry.origin_sent_at)
        self.assertIsNone(entry.radio_rx_at)
        self.assertEqual(entry.local_sent_at, 1_700_000_000.0)
        self.assertEqual(entry.app_received_at, 1_700_000_000.0)
        self.assertEqual(entry.age_reference, 456.0)
        self.assertFalse(entry.age_is_receive_time)
        self.assertTrue(entry.outgoing)
        self.assertFalse(entry.unread)
        self.assertFalse(entry.is_new)
        self.assertIsNone(entry.distance_miles)

    def test_received_entry_calculates_sender_distance(self) -> None:
        message = self.make_message(long_name="Alice", short_name="ALCE")
        message = replace(
            message,
            local_position=GeoPosition(0.0, 0.0),
            sender_position=GeoPosition(0.0, 1.0),
        )

        entry = received_chat_entry(message)

        self.assertAlmostEqual(entry.distance_miles, 69.09, places=2)

    # ---- Interrupted SENDING reconciliation on reload ------------------

    def test_persisted_sending_message_reconciles_to_interrupted_on_load(
        self,
    ) -> None:
        """A message left SENDING by a process that no longer exists must

        not reload as SENDING forever -- see radio_service.DeliveryState.
        INTERRUPTED and app_controller.stored_chat_entry.
        """
        stored = make_stored_message(delivery_state="SENDING")

        entry = stored_chat_entry(stored, wall_now=1_700_000_100.0)

        self.assertEqual(entry.delivery_state, DeliveryState.INTERRUPTED)
        self.assertNotEqual(entry.delivery_state, DeliveryState.SENDING)

    def test_interrupted_reconciliation_never_claims_sent(self) -> None:
        stored = make_stored_message(delivery_state="SENDING")
        entry = stored_chat_entry(stored, wall_now=1_700_000_100.0)
        self.assertNotIn(
            entry.delivery_state, (DeliveryState.SENT, DeliveryState.HEARD)
        )

    def test_reconciliation_does_not_fabricate_a_new_send_attempt(self) -> None:
        """Reconciliation is a pure display-state transform: it must not

        touch active_attempt_id/confirmation_deadline/send_generation,
        which is what would be required to actually resubmit a send --
        proving no new transmission is implied by loading this entry.
        """
        stored = make_stored_message(delivery_state="SENDING")
        entry = stored_chat_entry(stored, wall_now=1_700_000_100.0)
        self.assertIsNone(entry.active_attempt_id)
        self.assertIsNone(entry.confirmation_deadline)
        self.assertEqual(entry.send_generation, 0)

    def test_already_terminal_states_are_not_reconciled(self) -> None:
        """SENT is deliberately excluded here -- see the dedicated

        SENT->UNCONFIRMED tests below; every other terminal state must
        reload completely unchanged.
        """
        for raw_state, expected in (
            ("HEARD", DeliveryState.HEARD),
            ("UNCONFIRMED", DeliveryState.UNCONFIRMED),
            ("FAILED", DeliveryState.FAILED),
            ("INTERRUPTED", DeliveryState.INTERRUPTED),
        ):
            with self.subTest(raw_state=raw_state):
                stored = make_stored_message(delivery_state=raw_state)
                entry = stored_chat_entry(stored, wall_now=1_700_000_100.0)
                self.assertEqual(entry.delivery_state, expected)

    # ---- Unconfirmable SENT reconciliation on reload --------------------

    def test_persisted_sent_message_reconciles_to_unconfirmed_on_load(self) -> None:
        """Reproduces the real-device report exactly: message_id 81's

        real dump had messages.delivery_state = SENT and a terminal
        SENT send_attempt (completed_at NULL, which is expected -- see
        _set_delivery_state, SENT is deliberately excluded from the
        completed_at stamp since it isn't a final outcome). A SENT row
        has no persisted confirmation_deadline, and nothing can ever
        move it out of SENT again after a restart (only a live
        process's own radio callback can ever deliver HEARD, and
        _advance_delivery_states() only promotes SENT->UNCONFIRMED once
        a confirmation_deadline it doesn't have elapses) -- so the
        widget's deliberate SENT-renders-as-SENDING display (see
        ChatEntryWidget.refresh_delivery_state) would otherwise show
        "SENDING..." forever, with no resend affordance either (SENT is
        not in MANUAL_RESEND_STATES). UNCONFIRMED is the honest,
        already-existing state for exactly this situation.
        """
        stored = make_stored_message(delivery_state="SENT")

        entry = stored_chat_entry(stored, wall_now=1_700_000_100.0)

        self.assertEqual(entry.delivery_state, DeliveryState.UNCONFIRMED)
        self.assertNotEqual(entry.delivery_state, DeliveryState.SENT)

    def test_sent_reconciliation_never_claims_heard(self) -> None:
        stored = make_stored_message(delivery_state="SENT")
        entry = stored_chat_entry(stored, wall_now=1_700_000_100.0)
        self.assertNotIn(
            entry.delivery_state, (DeliveryState.SENT, DeliveryState.HEARD)
        )

    def test_sent_reconciliation_unlocks_manual_resend(self) -> None:
        from app import can_manual_resend

        stored = make_stored_message(delivery_state="SENT")
        entry = stored_chat_entry(stored, wall_now=1_700_000_100.0)
        self.assertTrue(can_manual_resend(entry))

    def test_sent_reconciliation_does_not_fabricate_a_new_send_attempt(self) -> None:
        stored = make_stored_message(delivery_state="SENT")
        entry = stored_chat_entry(stored, wall_now=1_700_000_100.0)
        self.assertIsNone(entry.active_attempt_id)
        self.assertIsNone(entry.confirmation_deadline)
        self.assertEqual(entry.send_generation, 0)

    def test_incoming_messages_are_never_touched_by_reconciliation(self) -> None:
        """The SENDING->INTERRUPTED rule only ever applies to outgoing

        entries -- an incoming message never carries a delivery_state at
        all, and stored_chat_entry must not invent one.
        """
        stored = make_stored_message(
            direction="incoming",
            delivery_state=None,
            node_id="!a11ce001",
            sender_name="Alice",
            local_sent_at=None,
            origin_sent_at=1_700_000_000.0,
        )
        entry = stored_chat_entry(stored, wall_now=1_700_000_100.0)
        self.assertIsNone(entry.delivery_state)
        self.assertFalse(entry.outgoing)

    def test_monitor_stops_and_closes_service_cleanly(self) -> None:
        radio = FakeRadio()
        events = []
        messages = []
        monitor = RadioMonitor(radio, events.append, messages.append)

        monitor.start()
        self.assertTrue(radio.started.wait(1))
        monitor.stop()
        monitor.stop()

        self.assertFalse(monitor.is_running)
        self.assertEqual(radio.close.call_count, 1)
        self.assertEqual(radio.removed_handlers, [messages.append])
        self.assertEqual([event.state for event in events], [RadioState.CONNECTING])

    @staticmethod
    def make_message(
        long_name: str | None,
        short_name: str | None,
    ) -> ReceivedMessage:
        return ReceivedMessage(
            sender_node_id="!a11ce001",
            sender_long_name=long_name,
            sender_short_name=short_name,
            channel_index=0,
            text="hello",
            rssi=-80,
            snr=5.0,
            packet_id=1,
        )


class FakeRadio:
    def __init__(self) -> None:
        self.started = Event()
        self.close = Mock()
        self.added_handlers = []
        self.removed_handlers = []

    def add_message_handler(self, handler: object) -> None:
        self.added_handlers.append(handler)

    def remove_message_handler(self, handler: object) -> None:
        self.removed_handlers.append(handler)

    def connection_events(self, stop_event: Event):
        self.started.set()
        yield RadioEvent(RadioState.CONNECTING)
        stop_event.wait(1)


if __name__ == "__main__":
    unittest.main()
