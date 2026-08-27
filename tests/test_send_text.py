"""Hardware-free tests for application-level text sending."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from google.protobuf.json_format import MessageToDict
from meshtastic.protobuf import mesh_pb2

from radio_service import (
    DeliveryState,
    RadioSendError,
    RadioService,
    SentMessage,
)
from simulated_radio_service import SimulatedRadioService, SimulatedSendOutcome


class SimulatedSendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SimulatedRadioService()
        self.service.connect()

    def tearDown(self) -> None:
        self.service.close()

    def test_broadcast_send(self) -> None:
        sent = self.service.send_text("hello", channel_index=1)

        self.assertEqual(sent.text, "hello")
        self.assertEqual(sent.channel_index, 1)
        self.assertEqual(sent.immediate_state, DeliveryState.SENT)
        self.assertIsNotNone(sent.packet_id)

    def test_direct_send(self) -> None:
        sent = self.service.send_text(
            "private hello",
            destination_node_id="!a11ce001",
        )

        self.assertEqual(sent.destination_node_id, "!a11ce001")

    def test_explicit_outcomes_are_deterministic(self) -> None:
        service = SimulatedRadioService(
            send_outcomes=(
                SimulatedSendOutcome.HEARD,
                SimulatedSendOutcome.UNCONFIRMED,
                SimulatedSendOutcome.FAILED,
            )
        )
        service.connect()
        self.addCleanup(service.close)

        self.assertEqual(
            service.send_text("heard").immediate_state,
            DeliveryState.HEARD,
        )
        self.assertEqual(
            service.send_text("uncertain").immediate_state,
            DeliveryState.UNCONFIRMED,
        )
        with self.assertRaisesRegex(RadioSendError, "definite"):
            service.send_text("failed")

    def test_sent_history_is_ordered_and_read_only(self) -> None:
        first = self.service.send_text("one")
        second = self.service.send_text("two", channel_index=2)

        self.assertEqual(self.service.sent_messages, (first, second))
        self.assertIsInstance(self.service.sent_messages, tuple)

    def test_rejects_invalid_input(self) -> None:
        invalid_requests = (
            {"text": ""},
            {"text": "   "},
            {"text": "hello", "channel_index": -1},
            {"text": "hello", "channel_index": 8},
            {"text": "hello", "channel_index": True},
            {"text": "hello", "destination_node_id": "  "},
        )

        for arguments in invalid_requests:
            with self.subTest(arguments=arguments):
                with self.assertRaises(RadioSendError):
                    self.service.send_text(**arguments)

        self.assertEqual(self.service.sent_messages, ())


LOCAL_NODE_NUMBER = 0x12345678
REMOTE_NODE_NUMBER = 0xABCDEF01


class RealRadioSendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RadioService()
        self.interface = Mock()
        self.interface.myInfo = SimpleNamespace(my_node_num=LOCAL_NODE_NUMBER)
        self.service._interface = self.interface

    @staticmethod
    def routing_response(
        request_id: int,
        error_reason: int | None = None,
        from_number: int | None = LOCAL_NODE_NUMBER,
    ) -> dict:
        """Build the callback shape produced by SDK 2.7.11 conversion.

        `from_number` defaults to the LOCAL node -- matching the SDK's
        own "implicit ack" shape (see node.py's onAckNak/receivedImplAck):
        a clean routing response whose "from" is our own node number is
        real evidence the local radio/routing layer handled the packet,
        but never proof any other node received it. Pass
        REMOTE_NODE_NUMBER (or any other value) to build the shape of a
        genuinely different node's own routing response -- e.g. a DM
        destination's explicit ack.
        """
        routing_message = mesh_pb2.Routing()
        if error_reason is not None:
            routing_message.error_reason = error_reason
        routing = MessageToDict(routing_message)
        routing["raw"] = routing_message
        packet = {
            "decoded": {
                "portnum": "ROUTING_APP",
                "requestId": request_id,
                "routing": routing,
            }
        }
        if from_number is not None:
            packet["from"] = from_number
        return packet

    def test_broadcast_calls_sdk_without_destination_override(self) -> None:
        sent = self.service.send_text("mesh hello", channel_index=1)

        self.interface.sendText.assert_called_once_with(
            text="mesh hello",
            channelIndex=1,
        )
        self.assertEqual(sent, SentMessage("mesh hello", 1, None))

    def test_direct_send_maps_to_sdk_destination(self) -> None:
        self.service.send_text(
            "direct hello",
            channel_index=2,
            destination_node_id="!a11ce001",
        )

        self.interface.sendText.assert_called_once_with(
            text="direct hello",
            channelIndex=2,
            destinationId="!a11ce001",
        )

    def test_sdk_failure_becomes_application_error(self) -> None:
        self.interface.sendText.side_effect = OSError("serial link failed")

        with self.assertRaises(RadioSendError) as caught:
            self.service.send_text("hello")

        self.assertEqual(
            str(caught.exception),
            "Could not send text message: serial link failed",
        )

    def test_requires_an_active_connection(self) -> None:
        self.service._interface = None

        with self.assertRaisesRegex(RadioSendError, "not connected"):
            self.service.send_text("hello")

    def test_sdk_ack_callback_accepts_omitted_none_from_real_conversion(self) -> None:
        """A clean ack whose "from" is OUR OWN node -- the SDK's own

        implicit-ack shape -- is real evidence the local radio/routing
        layer handled the packet, but is NOT proof any other node
        received it, so it must report SENT (checkmark), never HEARD
        (double-checkmark). See test_ack_from_a_different_node_is_heard
        for the case that DOES produce HEARD.
        """
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=123456)

        sent = self.service.send_text("hello", status_handler=statuses.append)

        kwargs = self.interface.sendText.call_args.kwargs
        self.assertEqual(kwargs["text"], "hello")
        self.assertEqual(kwargs["channelIndex"], 0)
        self.assertTrue(kwargs["wantAck"])
        self.assertEqual(kwargs["onResponse"].__name__, "onAckNak")
        self.assertEqual(sent.packet_id, 123456)

        response = self.routing_response(123456)
        self.assertNotIn("errorReason", response["decoded"]["routing"])
        kwargs["onResponse"](response)
        self.assertEqual(statuses[0].state, DeliveryState.SENT)
        self.assertEqual(statuses[0].packet_id, 123456)

    def test_sdk_ack_callback_accepts_explicit_symbolic_none(self) -> None:
        response = self.routing_response(7, mesh_pb2.Routing.Error.NONE)

        self.assertEqual(response["decoded"]["routing"]["errorReason"], "NONE")
        status = self.service._parse_send_response(response)

        self.assertEqual(status.state, DeliveryState.SENT)
        self.assertEqual(status.packet_id, 7)

    def test_ack_from_a_different_node_is_heard(self) -> None:
        """The strong signal: a routing response whose "from" names a

        DIFFERENT node than our own -- a DM destination's own explicit
        ack, or any other node's routing response genuinely reaching us.
        """
        response = self.routing_response(8, from_number=REMOTE_NODE_NUMBER)

        status = self.service._parse_send_response(response)

        self.assertEqual(status.state, DeliveryState.HEARD)
        self.assertEqual(status.packet_id, 8)

    def test_local_cache_alone_cannot_produce_heard(self) -> None:
        """Calling send_text() and having it return successfully must

        never, by itself, produce anything stronger than SENT -- no ack
        of any kind has even been parsed yet.
        """
        self.interface.sendText.return_value = SimpleNamespace(id=42)

        sent = self.service.send_text("hello")

        self.assertIsNone(sent.immediate_state)  # RadioService never fabricates one

    def test_a_missing_from_field_is_treated_as_local_not_heard(self) -> None:
        """An ambiguous packet (no "from" at all) must never be

        interpreted as genuine remote evidence -- truthfulness over
        always reaching the stronger state.
        """
        response = self.routing_response(9, from_number=None)

        status = self.service._parse_send_response(response)

        self.assertEqual(status.state, DeliveryState.SENT)

    def test_sdk_routing_nak_is_definite_failure(self) -> None:
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=99)
        self.service.send_text("hello", status_handler=statuses.append)
        callback = self.interface.sendText.call_args.kwargs["onResponse"]

        response = self.routing_response(
            99,
            mesh_pb2.Routing.Error.MAX_RETRANSMIT,
        )
        self.assertEqual(
            response["decoded"]["routing"]["errorReason"],
            "MAX_RETRANSMIT",
        )
        callback(response)

        self.assertEqual(statuses[0].state, DeliveryState.FAILED)
        self.assertIn("MAX_RETRANSMIT", statuses[0].detail)

    def test_unknown_numeric_or_symbolic_routing_errors_are_ignored(self) -> None:
        numeric_response = self.routing_response(20, 123)
        self.assertEqual(
            numeric_response["decoded"]["routing"]["errorReason"],
            123,
        )

        symbolic_response = self.routing_response(21)
        symbolic_response["decoded"]["routing"]["errorReason"] = "FUTURE_ERROR"

        self.assertIsNone(self.service._parse_send_response(numeric_response))
        self.assertIsNone(self.service._parse_send_response(symbolic_response))


class GenericRoutingResponseTrackingTests(unittest.TestCase):
    """RadioService._on_routing_response / _pending_sends -- the

    generic, repeatable observation path that exists because the SDK's
    own one-shot onResponse callback discards its handler after the
    FIRST matching packet (see node.py/mesh_interface.py), which would
    otherwise silently drop a genuinely stronger confirmation (e.g. a
    DM destination's own ack) arriving after an earlier weaker one.
    """

    def setUp(self) -> None:
        self.service = RadioService()
        self.interface = Mock()
        self.interface.myInfo = SimpleNamespace(my_node_num=LOCAL_NODE_NUMBER)
        self.service._interface = self.interface

    def routing_response(self, request_id, error_reason=None, from_number=LOCAL_NODE_NUMBER):
        return RealRadioSendTests.routing_response(request_id, error_reason, from_number)

    def test_a_later_genuinely_remote_ack_upgrades_an_earlier_local_one(self) -> None:
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=1)
        self.service.send_text("hello", status_handler=statuses.append)

        self.service._on_routing_response(
            packet=self.routing_response(1, from_number=LOCAL_NODE_NUMBER),
            interface=self.interface,
        )
        self.service._on_routing_response(
            packet=self.routing_response(1, from_number=REMOTE_NODE_NUMBER),
            interface=self.interface,
        )

        self.assertEqual([status.state for status in statuses], [DeliveryState.SENT, DeliveryState.HEARD])

    def test_unrelated_ack_cannot_confirm_another_message(self) -> None:
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=1)
        self.service.send_text("hello", status_handler=statuses.append)

        self.service._on_routing_response(
            packet=self.routing_response(999, from_number=REMOTE_NODE_NUMBER),
            interface=self.interface,
        )

        self.assertEqual(statuses, [])

    def test_duplicate_confirmation_is_harmless(self) -> None:
        """Once a packet ID has resolved to a terminal state (HEARD), it

        is removed from tracking -- a duplicate/late repeat of the SAME
        packet must not re-fire the handler at all (never mind produce
        a contradictory result). Harmless means "silently ignored", not
        "delivered twice".
        """
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=1)
        self.service.send_text("hello", status_handler=statuses.append)
        response = self.routing_response(1, from_number=REMOTE_NODE_NUMBER)

        self.service._on_routing_response(packet=response, interface=self.interface)
        self.service._on_routing_response(packet=response, interface=self.interface)

        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].state, DeliveryState.HEARD)
        self.assertNotIn(1, self.service._pending_sends)

    def test_pending_send_is_cleared_after_a_terminal_state(self) -> None:
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=2)
        self.service.send_text("hello", status_handler=statuses.append)
        self.assertIn(2, self.service._pending_sends)

        self.service._on_routing_response(
            packet=self.routing_response(2, from_number=REMOTE_NODE_NUMBER),
            interface=self.interface,
        )

        self.assertNotIn(2, self.service._pending_sends)

    def test_pending_send_survives_a_non_terminal_local_ack(self) -> None:
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=3)
        self.service.send_text("hello", status_handler=statuses.append)

        self.service._on_routing_response(
            packet=self.routing_response(3, from_number=LOCAL_NODE_NUMBER),
            interface=self.interface,
        )

        self.assertIn(3, self.service._pending_sends)

    def test_stale_interface_response_is_ignored(self) -> None:
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=4)
        self.service.send_text("hello", status_handler=statuses.append)
        stale_interface = Mock()

        self.service._on_routing_response(
            packet=self.routing_response(4, from_number=REMOTE_NODE_NUMBER),
            interface=stale_interface,
        )

        self.assertEqual(statuses, [])

    def test_pending_sends_are_bounded(self) -> None:
        self.interface.sendText.side_effect = [
            SimpleNamespace(id=index) for index in range(RadioService._MAX_PENDING_SENDS + 5)
        ]
        for _ in range(RadioService._MAX_PENDING_SENDS + 5):
            self.service.send_text("hello", status_handler=lambda status: None)

        self.assertLessEqual(len(self.service._pending_sends), RadioService._MAX_PENDING_SENDS)
        self.assertNotIn(0, self.service._pending_sends)  # oldest evicted first


if __name__ == "__main__":
    unittest.main()
