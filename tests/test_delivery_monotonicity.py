"""Delivery-state monotonicity: once a send attempt reaches SENT, HEARD,

or FAILED, a later confirmation-timeout tick must never downgrade it to
UNCONFIRMED (PART A of the DELIVERY MONOTONICITY + HOPS AUDIT +
HOP LIMIT payload). Also covers PART B (explicit RESEND always creates
a new logical packet ID) and the DM regression required by PART C
item 10.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from time import monotonic
import unittest

from app import ChatEntryWidget, MeshtasticPassApp
from app_settings import AppSettings
from chat_store import ChatStore
from radio_service import DeliveryState, SendStatus
from simulated_radio_service import SimulatedRadioService, SimulatedSendOutcome
from tests.test_app import ControllableSendRadioService


class DeliveryMonotonicityTestsBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.chat_db_path = self.root / "chat.db"


class ChannelDeliveryMonotonicityTests(DeliveryMonotonicityTestsBase):
    """Scenarios A-F of the delivery test matrix, against channel CHAT."""

    async def test_a_no_response_before_timeout_becomes_unconfirmed(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            entry = app._start_outgoing("no response")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.SENDING)

            entry.confirmation_deadline = monotonic() - 1
            app._advance_delivery_states()
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.UNCONFIRMED)

    async def test_b_local_implicit_confirmation_stays_sent_past_timeout(
        self,
    ) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            entry = app._start_outgoing("implicit ack")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))
            radio.fire_status(packet_id, SendStatus(DeliveryState.SENT, packet_id))
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.SENT)
            self.assertIsNone(entry.confirmation_deadline)

            entry.confirmation_deadline = monotonic() - 1
            app._advance_delivery_states()
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.SENT)

    async def test_c_remote_ack_stays_heard_past_timeout(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            entry = app._start_outgoing("remote ack")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))
            radio.fire_status(packet_id, SendStatus(DeliveryState.SENT, packet_id))
            await pilot.pause()
            radio.fire_status(packet_id, SendStatus(DeliveryState.HEARD, packet_id))
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.HEARD)
            self.assertIsNone(entry.confirmation_deadline)

            entry.confirmation_deadline = monotonic() - 1
            app._advance_delivery_states()
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.HEARD)

    async def test_d_nak_stays_failed_past_timeout(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            entry = app._start_outgoing("nak")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))
            radio.fire_status(
                packet_id, SendStatus(DeliveryState.FAILED, packet_id, detail="NAK")
            )
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.FAILED)
            self.assertIsNone(entry.confirmation_deadline)

            entry.confirmation_deadline = monotonic() - 1
            app._advance_delivery_states()
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.FAILED)

    async def test_e_late_ack_after_unconfirmed_promotes_it(self) -> None:
        """Chosen rule (see completion report item 4): a genuinely LATE

        but real ack still promotes an already-UNCONFIRMED entry to
        SENT/HEARD -- real evidence is never discarded merely because
        it arrived after the timeout already fired. This was already
        the existing behavior of delivery_status_received (it only
        refuses to move an entry OUT of HEARD/FAILED, never out of
        UNCONFIRMED) -- this test makes it explicit and deterministic.
        """
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            entry = app._start_outgoing("late ack")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))

            entry.confirmation_deadline = monotonic() - 1
            app._advance_delivery_states()
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.UNCONFIRMED)

            radio.fire_status(packet_id, SendStatus(DeliveryState.HEARD, packet_id))
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.HEARD)

    async def test_f_duplicate_ack_after_sent_is_harmless(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            entry = app._start_outgoing("duplicate ack")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))
            radio.fire_status(packet_id, SendStatus(DeliveryState.SENT, packet_id))
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.SENT)

            # A duplicate/late repeat of the SAME status must never
            # crash or produce a contradictory transition.
            radio.fire_status(packet_id, SendStatus(DeliveryState.SENT, packet_id))
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.SENT)


class ResendPacketIdentityTests(DeliveryMonotonicityTestsBase):
    """PART B: explicit user RESEND always creates a NEW logical send."""

    async def test_g_resend_produces_distinct_packet_ids(self) -> None:
        """UNCONFIRMED (not FAILED) is used for the first two attempts

        because a FAILED simulated send raises before a packet_id is
        even recorded (see SimulatedRadioService.send_text) -- UNCONFIRMED
        is still one of the MANUAL_RESEND_STATES, so [RESEND] remains
        available after each attempt, and every attempt's packet_id is
        genuinely recorded for comparison.
        """
        radio = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
            send_outcomes=(
                SimulatedSendOutcome.UNCONFIRMED,
                SimulatedSendOutcome.UNCONFIRMED,
                SimulatedSendOutcome.SENT,
            ),
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            entry = app._start_outgoing("hi all")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(10):
                await pilot.pause()
                if len(radio.sent_messages) >= 1:
                    break
            self.assertEqual(entry.delivery_state, DeliveryState.UNCONFIRMED)

            app._rebroadcast(entry)
            for _ in range(10):
                await pilot.pause()
                if len(radio.sent_messages) >= 2:
                    break
            first_generation = entry.send_generation
            self.assertEqual(len(radio.sent_messages), 2)
            self.assertEqual(entry.delivery_state, DeliveryState.UNCONFIRMED)

            app._rebroadcast(entry)
            for _ in range(10):
                await pilot.pause()
                if len(radio.sent_messages) >= 3:
                    break
            second_generation = entry.send_generation
            self.assertEqual(len(radio.sent_messages), 3)
            self.assertEqual(entry.delivery_state, DeliveryState.SENT)

            packet_a, packet_b, packet_c = (
                message.packet_id for message in radio.sent_messages
            )
            self.assertNotEqual(packet_a, packet_b)
            self.assertNotEqual(packet_b, packet_c)
            self.assertNotEqual(packet_a, packet_c)
            self.assertEqual(first_generation, 2)
            self.assertEqual(second_generation, 3)

    async def test_h_identical_text_sends_never_share_pending_state(self) -> None:
        """Six sends of the identical text are six separate logical

        sends -- message identity is never derived from text content
        (item 8).
        """
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            entries = []
            for _ in range(6):
                entry = app._start_outgoing("hi all")
                app._send_from_thread(entry, entry.send_generation)
                await pilot.pause()
                entries.append(entry)

            packet_ids = [entry.packet_id for entry in entries]
            self.assertEqual(len(packet_ids), len(set(packet_ids)))
            self.assertEqual(len(radio.pending_status_handlers), 6)

            # Resolving ONE of the six as HEARD must never affect the
            # other five identical-text entries.
            radio.fire_status(
                packet_ids[2], SendStatus(DeliveryState.HEARD, packet_ids[2])
            )
            await pilot.pause()
            for index, entry in enumerate(entries):
                expected = (
                    DeliveryState.HEARD if index == 2 else DeliveryState.SENDING
                )
                self.assertEqual(entry.delivery_state, expected)


class DMDeliveryMonotonicityRegressionTests(DeliveryMonotonicityTestsBase):
    """PART C item 10: monotonicity and destination-ACK correlation

    apply identically to DM entries, never regressed by this pass.
    """

    async def test_dm_sent_never_downgrades_to_unconfirmed(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "hello")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))
            radio.fire_status(packet_id, SendStatus(DeliveryState.SENT, packet_id))
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.SENT)

            entry.confirmation_deadline = monotonic() - 1
            app._advance_delivery_states()
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.SENT)

    async def test_dm_destination_ack_heard_never_downgrades(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "hello")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))
            radio.fire_status(packet_id, SendStatus(DeliveryState.HEARD, packet_id))
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.HEARD)

            entry.confirmation_deadline = monotonic() - 1
            app._advance_delivery_states()
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.HEARD)

    async def test_dm_times_out_to_unconfirmed_even_while_a_different_dm_is_open(
        self,
    ) -> None:
        """Regression for _advance_delivery_states' generalized iteration:

        a SENDING DM entry must time out correctly even when it is not
        the currently-open conversation.
        """
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "will time out")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.SENDING)

            app.open_dm("!b0b00002", long_name="Bob")
            await pilot.pause()

            entry.confirmation_deadline = monotonic() - 1
            app._advance_delivery_states()
            await pilot.pause()

            self.assertEqual(entry.delivery_state, DeliveryState.UNCONFIRMED)

    async def test_dm_unrelated_ack_never_produces_heard_and_never_downgrades(
        self,
    ) -> None:
        """Reconfirms MESHTASTICPASS DM item 6 (destination correlation)

        still holds unmodified alongside the new monotonicity fix.
        """
        from unittest.mock import Mock
        from types import SimpleNamespace
        from radio_service import RadioService

        service = RadioService()
        interface = Mock()
        interface.myInfo = SimpleNamespace(my_node_num=0x11111111)
        service._interface = interface
        statuses = []
        interface.sendText.return_value = SimpleNamespace(id=1)
        service.send_text("hi", destination_node_id="!22222222", status_handler=statuses.append)

        def routing_response(request_id, from_number):
            return {
                "decoded": {"portnum": "ROUTING_APP", "requestId": request_id, "routing": {}},
                "from": from_number,
            }

        service._on_routing_response(
            packet=routing_response(1, 0x33333333), interface=interface
        )
        self.assertEqual(statuses, [])

        service._on_routing_response(
            packet=routing_response(1, 0x22222222), interface=interface
        )
        self.assertEqual([status.state for status in statuses], [DeliveryState.HEARD])


if __name__ == "__main__":
    unittest.main()
