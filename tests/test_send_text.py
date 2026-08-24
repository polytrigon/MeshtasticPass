"""Hardware-free tests for application-level text sending."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

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


class RealRadioSendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RadioService()
        self.interface = Mock()
        self.service._interface = self.interface

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

    def test_sdk_ack_callback_uses_real_2711_signature(self) -> None:
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=123456)

        sent = self.service.send_text("hello", status_handler=statuses.append)

        kwargs = self.interface.sendText.call_args.kwargs
        self.assertEqual(kwargs["text"], "hello")
        self.assertEqual(kwargs["channelIndex"], 0)
        self.assertTrue(kwargs["wantAck"])
        self.assertEqual(kwargs["onResponse"].__name__, "onAckNak")
        self.assertEqual(sent.packet_id, 123456)

        kwargs["onResponse"](
            {
                "decoded": {
                    "portnum": "ROUTING_APP",
                    "requestId": 123456,
                    "routing": {"errorReason": "NONE"},
                }
            }
        )
        self.assertEqual(statuses[0].state, DeliveryState.HEARD)
        self.assertEqual(statuses[0].packet_id, 123456)

    def test_sdk_routing_nak_is_definite_failure(self) -> None:
        statuses = []
        self.interface.sendText.return_value = SimpleNamespace(id=99)
        self.service.send_text("hello", status_handler=statuses.append)
        callback = self.interface.sendText.call_args.kwargs["onResponse"]

        callback(
            {
                "decoded": {
                    "portnum": "ROUTING_APP",
                    "requestId": 99,
                    "routing": {"errorReason": "MAX_RETRANSMIT"},
                }
            }
        )

        self.assertEqual(statuses[0].state, DeliveryState.FAILED)
        self.assertIn("MAX_RETRANSMIT", statuses[0].detail)


if __name__ == "__main__":
    unittest.main()
