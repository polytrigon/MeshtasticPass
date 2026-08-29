"""Tests for application-level received Meshtastic messages."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call

from geo import GeoPosition
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
            "rxTime": 1_700_000_000,
            # Neither an arbitrary wall-clock-looking key nor the deprecated
            # delayed enum is a trustworthy text origin timestamp.
            "timestamp": 1_600_000_000,
            "delayed": "DELAYED_BROADCAST",
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
                origin_sent_at=None,
                radio_rx_at=1_700_000_000.0,
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
            "rxTime": "not-a-time",
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
        self.assertIsNone(message.origin_sent_at)
        self.assertIsNone(message.radio_rx_at)
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

    def test_extracts_actual_sdk_position_shapes_and_timestamps(self) -> None:
        local_number = 0x51A00001
        sender_number = 0xABC12345
        interface = SimpleNamespace(
            myInfo=SimpleNamespace(my_node_num=local_number),
            nodesByNum={
                local_number: {
                    "position": {
                        "latitude": 40.7128,
                        "longitude": -74.0060,
                        "timestamp": 1_700_000_000,
                    }
                },
                sender_number: {
                    "position": {
                        "latitudeI": 407736000,
                        "longitudeI": -739566000,
                        "time": 1_700_000_100,
                    }
                },
            },
            nodes={},
        )
        packet = {
            "from": sender_number,
            "fromId": "!abc12345",
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }

        message = self.service._parse_text_packet(packet, interface)

        self.assertEqual(
            message.local_position,
            GeoPosition(40.7128, -74.0060, 1_700_000_000.0),
        )
        self.assertAlmostEqual(message.sender_position.latitude, 40.7736)
        self.assertAlmostEqual(message.sender_position.longitude, -73.9566)
        self.assertEqual(message.sender_position.updated_at, 1_700_000_100.0)

    def test_missing_or_malformed_node_positions_are_omitted(self) -> None:
        sender_number = 0xABC12345
        packet = {
            "from": sender_number,
            "fromId": "!abc12345",
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }
        cases = (
            SimpleNamespace(
                myInfo=None,
                nodesByNum={sender_number: {"position": {"latitude": 40.0}}},
                nodes={},
            ),
            SimpleNamespace(
                myInfo=SimpleNamespace(my_node_num=1),
                nodesByNum={
                    1: {"position": {"latitude": 40.0, "longitude": -74.0}},
                    sender_number: {"position": {"latitude": "bad", "longitude": -73.0}},
                },
                nodes={},
            ),
        )

        for interface in cases:
            with self.subTest(interface=interface):
                message = self.service._parse_text_packet(packet, interface)
                self.assertIsNone(message.sender_position)

        self.assertIsNone(
            self.service._parse_text_packet(packet, cases[0]).local_position
        )

    def test_position_time_falls_back_when_timestamp_is_malformed(self) -> None:
        position = self.service._position_from_record(
            {
                "position": {
                    "latitude": 40.0,
                    "longitude": -74.0,
                    "timestamp": "malformed",
                    "time": 1_700_000_100,
                }
            }
        )

        self.assertEqual(position.updated_at, 1_700_000_100.0)

    def test_does_not_duplicate_subscriptions_across_reconnects(self) -> None:
        pub = Mock()

        self.service._subscribe_to_events(pub)
        self.service._subscribe_to_events(pub)

        self.assertEqual(pub.subscribe.call_count, 4)
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
                call(
                    self.service._on_routing_response,
                    "meshtastic.receive.routing",
                ),
                call(
                    self.service._on_any_packet_for_link_quality,
                    "meshtastic.receive",
                ),
            ],
        )

        self.service._unsubscribe_from_events()
        # Always attempts to unsubscribe the RX-debug diagnostic topic
        # too (see rx_debug_enabled()), even when it was never actually
        # subscribed -- each attempt is independently wrapped so an
        # unsubscribe for a topic that was never joined is harmless.
        self.assertEqual(pub.unsubscribe.call_count, 5)

    def test_broadcast_packet_is_never_direct(self) -> None:
        """PART A item 3: SDK-source-verified check (__main__.py's own

        DM-detection pattern) -- a broadcast packet's "to" is
        BROADCAST_NUM (0xFFFFFFFF), never a real node number.
        """
        interface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=0x51A00001))
        packet = {
            "from": 0xABC12345,
            "to": 0xFFFFFFFF,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi all"},
        }

        message = self.service._parse_text_packet(packet, interface)

        self.assertFalse(message.is_direct)

    def test_packet_addressed_to_local_node_is_direct(self) -> None:
        local_number = 0x51A00001
        interface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=local_number))
        packet = {
            "from": 0xABC12345,
            "to": local_number,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "just for you"},
        }

        message = self.service._parse_text_packet(packet, interface)

        self.assertTrue(message.is_direct)

    def test_packet_addressed_to_someone_else_is_not_direct(self) -> None:
        """A packet destined for a DIFFERENT node than us (e.g. sniffed

        while relaying) must never be treated as a local DM.
        """
        local_number = 0x51A00001
        interface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=local_number))
        packet = {
            "from": 0xABC12345,
            "to": 0xDEADBEEF,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "not for you"},
        }

        message = self.service._parse_text_packet(packet, interface)

        self.assertFalse(message.is_direct)

    def test_missing_destination_field_is_not_direct(self) -> None:
        interface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=0x51A00001))
        packet = {
            "from": 0xABC12345,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "no to field"},
        }

        message = self.service._parse_text_packet(packet, interface)

        self.assertFalse(message.is_direct)

    def test_destination_classification_survives_signed_unsigned_mismatch(self) -> None:
        """Item 7 of the MESH FOLLOW-UP applies equally here: a node

        number with bit 31 set can surface as a negative Python int
        from one source and unsigned from another -- canonical masking
        must still correctly recognize the match.
        """
        high_bit_number = 0xF0000001
        interface = SimpleNamespace(
            myInfo=SimpleNamespace(my_node_num=high_bit_number - (1 << 32))
        )
        packet = {
            "from": 0xABC12345,
            "to": high_bit_number,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "signed mismatch"},
        }

        message = self.service._parse_text_packet(packet, interface)

        self.assertTrue(message.is_direct)

    def test_dm_detection_never_uses_channel_index_as_a_heuristic(self) -> None:
        """A packet on channel 0 (or any index) addressed to someone

        else is still never a DM -- and one addressed to us is a DM
        REGARDLESS of its channel index (item 3: never a heuristic).
        """
        local_number = 0x51A00001
        interface = SimpleNamespace(myInfo=SimpleNamespace(my_node_num=local_number))
        not_a_dm = self.service._parse_text_packet(
            {
                "from": 1,
                "to": 0xFFFFFFFF,
                "channel": 0,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "broadcast ch0"},
            },
            interface,
        )
        is_a_dm = self.service._parse_text_packet(
            {
                "from": 1,
                "to": local_number,
                "channel": 3,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "dm ch3"},
            },
            interface,
        )
        self.assertFalse(not_a_dm.is_direct)
        self.assertTrue(is_a_dm.is_direct)

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
