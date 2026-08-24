"""Tests for application-level received Meshtastic messages."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call

from radio_service import ReceivedMessage, RadioService


class ReceivedMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RadioService()

    def test_parses_normal_text_packet(self) -> None:
        packet = {
            "from": 0xABC12345,
            "fromId": "!abc12345",
            "channel": 2,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "text": "hello from another node",
            },
            "rxRssi": -87,
            "rxSnr": 6.5,
            "id": 123456789,
        }

        message = self.service._parse_text_packet(packet, None)

        self.assertEqual(
            message,
            ReceivedMessage(
                sender_node_id="!abc12345",
                sender_long_name=None,
                sender_short_name=None,
                channel_index=2,
                text="hello from another node",
                rssi=-87,
                snr=6.5,
                packet_id=123456789,
            ),
        )

    def test_ignores_non_text_packet(self) -> None:
        packet = {
            "decoded": {
                "portnum": "TELEMETRY_APP",
                "telemetry": {"batteryLevel": 90},
            }
        }

        self.assertIsNone(self.service._parse_text_packet(packet, None))

    def test_handles_malformed_and_missing_fields(self) -> None:
        malformed = {
            "from": 7,
            "channel": "not-a-number",
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": b"hello \xffworld",
            },
            "rxRssi": object(),
            "rxSnr": "not-a-number",
            "id": None,
        }

        message = self.service._parse_text_packet(malformed, None)

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.sender_node_id, "!00000007")
        self.assertEqual(message.text, "hello \ufffdworld")
        self.assertIsNone(message.channel_index)
        self.assertIsNone(message.rssi)
        self.assertIsNone(message.snr)
        self.assertIsNone(message.packet_id)
        self.assertIsNone(self.service._parse_text_packet({}, None))
        self.assertIsNone(self.service._parse_text_packet(None, None))

    def test_looks_up_sender_names_from_node_database(self) -> None:
        sender_number = 0xABC12345
        interface = SimpleNamespace(
            nodesByNum={
                sender_number: {
                    "user": {
                        "longName": "Some Node",
                        "shortName": "NODE",
                    }
                }
            },
            nodes={},
        )
        packet = {
            "from": sender_number,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "text": "hello",
            },
        }

        message = self.service._parse_text_packet(packet, interface)

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.sender_long_name, "Some Node")
        self.assertEqual(message.sender_short_name, "NODE")

    def test_does_not_duplicate_subscriptions_across_reconnects(self) -> None:
        pub = Mock()

        self.service._subscribe_to_events(pub)
        self.service._subscribe_to_events(pub)

        self.assertEqual(pub.subscribe.call_count, 2)
        self.assertEqual(
            pub.subscribe.call_args_list,
            [
                call(
                    self.service._on_connection_lost,
                    "meshtastic.connection.lost",
                ),
                call(
                    self.service._on_text_received,
                    "meshtastic.receive.text",
                ),
            ],
        )

        self.service._unsubscribe_from_events()
        self.assertEqual(pub.unsubscribe.call_count, 2)

    def test_delivers_one_clean_message_to_each_handler(self) -> None:
        handler = Mock()
        interface = SimpleNamespace(nodesByNum={}, nodes={})
        packet = {
            "fromId": "!abc12345",
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "text": "hello",
            },
        }
        self.service._interface = interface
        self.service.add_message_handler(handler)
        self.service.add_message_handler(handler)

        self.service._on_text_received(packet=packet, interface=interface)

        handler.assert_called_once()
        message = handler.call_args.args[0]
        self.assertIsInstance(message, ReceivedMessage)
        self.assertEqual(message.text, "hello")


if __name__ == "__main__":
    unittest.main()
