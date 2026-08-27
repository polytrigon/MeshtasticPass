"""Tests for RadioService connection state changes."""

from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import Mock, patch

from geo import GeoPosition
from node_activity import ACTIVE_WINDOW_SECONDS
from radio_service import (
    ChannelInfo,
    RadioConnectionError,
    RadioIdentityError,
    RadioInfo,
    RadioService,
    RadioState,
)


def make_interface() -> SimpleNamespace:
    """Create the small part of a Meshtastic interface used by RadioService."""
    node_number = 0x12345678
    return SimpleNamespace(
        myInfo=SimpleNamespace(my_node_num=node_number),
        localNode=object(),
        nodesByNum={
            node_number: {
                "user": {
                    "id": "!12345678",
                    "longName": "Test Node",
                    "shortName": "TEST",
                }
            }
        },
        metadata=SimpleNamespace(firmware_version="2.7.0"),
        close=Mock(),
    )


class RadioServiceTests(unittest.TestCase):
    def test_discovers_serial_devices_and_closes_before_switching(self) -> None:
        service = RadioService("/dev/ttyUSB0")
        service.close = Mock()
        with patch("radio_service.discover_serial_devices", return_value=("/dev/a",)):
            self.assertEqual(service.available_device_paths(), ("/dev/a",))

        service.set_device_path("/dev/ttyACM0")
        service.close.assert_called_once_with()
        self.assertEqual(service.device_path, "/dev/ttyACM0")

    def test_device_errors_do_not_assume_one_path_or_username(self) -> None:
        service = RadioService("/dev/ttyACM7")
        with patch.object(service, "_device_exists", return_value=False):
            with self.assertRaises(RadioConnectionError) as missing:
                service._check_device()
        self.assertIn("/dev/ttyACM7", str(missing.exception))
        self.assertNotIn("/dev/ttyUSB0", str(missing.exception))

        with (
            patch.object(service, "_device_exists", return_value=True),
            patch("radio_service.os.access", return_value=False),
        ):
            with self.assertRaises(RadioConnectionError) as denied:
                service._check_device()
        self.assertIn("current user", str(denied.exception))
        self.assertNotIn("sudo usermod", str(denied.exception))

    def test_reads_enabled_channel_names_and_configured_primary_preset(self) -> None:
        channels = [
            SimpleNamespace(
                index=0,
                role=1,
                settings=SimpleNamespace(name=""),
            ),
            SimpleNamespace(
                index=1,
                role=2,
                settings=SimpleNamespace(name="Hiking"),
            ),
            SimpleNamespace(
                index=2,
                role=0,
                settings=SimpleNamespace(name="Disabled"),
            ),
        ]
        local_node = SimpleNamespace(
            channels=channels,
            localConfig=SimpleNamespace(
                lora=SimpleNamespace(use_preset=True, modem_preset=0)
            ),
        )

        self.assertEqual(
            RadioService._read_channel_info(local_node),
            (ChannelInfo(0, "LongFast"), ChannelInfo(1, "Hiking")),
        )

    def test_close_does_not_crash_after_unplug(self) -> None:
        service = RadioService()
        interface = make_interface()
        interface.close.side_effect = OSError("device disappeared")
        service._interface = interface

        service.close()

        interface.close.assert_called_once()
        self.assertIsNone(service._interface)

    def test_reconnects_after_connection_loss(self) -> None:
        service = RadioService()
        interface = make_interface()
        stopped = Event()

        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                side_effect=[interface, interface],
            ) as open_interface,
        ):
            events = service.connection_events(
                retry_delay=0,
                stop_event=stopped,
                poll_interval=0.001,
            )

            self.assertEqual(next(events).state, RadioState.CONNECTING)
            self.assertEqual(next(events).state, RadioState.ONLINE)

            service._on_connection_lost(interface)
            self.assertEqual(next(events).state, RadioState.OFFLINE)
            self.assertEqual(next(events).state, RadioState.CONNECTING)
            self.assertEqual(next(events).state, RadioState.ONLINE)

            stopped.set()
            with self.assertRaises(StopIteration):
                next(events)

        self.assertEqual(open_interface.call_count, 2)
        self.assertGreaterEqual(interface.close.call_count, 2)

    def test_receive_callback_delivers_messages_across_a_reconnect(self) -> None:
        """_subscribe_to_events() only subscribes once per RadioService

        (self._pub guards against re-subscribing on every reconnect,
        since the underlying pubsub dispatch is a process-wide
        singleton independent of any one interface object) -- but
        _on_text_received must still correctly route messages to
        _message_handlers for a SECOND interface generation, not only
        the first, and must still ignore a stray callback tagged with
        an interface this service no longer owns.
        """
        service = RadioService()
        first_interface = make_interface()
        second_interface = make_interface()
        stopped = Event()
        received: list[str] = []
        service.add_message_handler(lambda message: received.append(message.text))

        def text_packet(text: str) -> dict:
            return {
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
                "from": 0x12345678,
                "fromId": "!12345678",
                "channel": 0,
            }

        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                side_effect=[first_interface, second_interface],
            ),
        ):
            events = service.connection_events(
                retry_delay=0,
                stop_event=stopped,
                poll_interval=0.001,
            )

            self.assertEqual(next(events).state, RadioState.CONNECTING)
            self.assertEqual(next(events).state, RadioState.ONLINE)

            service._on_text_received(
                packet=text_packet("first generation"), interface=first_interface
            )
            self.assertEqual(received, ["first generation"])

            service._on_connection_lost(first_interface)
            self.assertEqual(next(events).state, RadioState.OFFLINE)
            self.assertEqual(next(events).state, RadioState.CONNECTING)
            self.assertEqual(next(events).state, RadioState.ONLINE)

            # A callback still tagged with the now-superseded interface
            # (e.g. a message the SDK was already dispatching at the
            # moment of disconnect) must not be attributed as live.
            service._on_text_received(
                packet=text_packet("stale first-generation packet"),
                interface=first_interface,
            )
            self.assertEqual(received, ["first generation"])

            # The receive callback must still be live for the NEW
            # interface generation -- this is the actual "does receive
            # survive a reconnect" proof.
            service._on_text_received(
                packet=text_packet("second generation"), interface=second_interface
            )
            self.assertEqual(received, ["first generation", "second generation"])

            stopped.set()
            with self.assertRaises(StopIteration):
                next(events)

    def test_subscribing_after_a_looser_optional_interface_listener_does_not_raise(
        self,
    ) -> None:
        """Regression test for a real-hardware failure: pypubsub rejects a

        REQUIRED-arg listener joining a topic whose message-data-spec was
        already established as optional by an earlier subscriber (e.g. a
        standalone diagnostic script's own connection-lost observer,
        which reasonably declares `interface=None` since it doesn't
        control what publishes). RadioService._on_connection_lost used to
        declare `interface` with no default -- inconsistent with its own
        sibling listeners _on_text_received/_on_any_packet_for_debug --
        which raised exactly this pypubsub error the moment the real
        radio tried to connect after radio_write_readback_probe.py's
        PacketObserver had already subscribed to the same topic.
        """
        from pubsub import pub

        def looser_listener(interface=None, **_kwargs):
            pass

        lost_topic = "meshtastic.connection.lost"
        text_topic = "meshtastic.receive.text"
        service = RadioService()
        pub.subscribe(looser_listener, lost_topic)
        try:
            service._subscribe_to_events(pub)
        except Exception as error:
            self.fail(
                "RadioService._on_connection_lost must tolerate a topic "
                f"whose message spec was already established as optional: {error}"
            )
        finally:
            for callback, topic in (
                (looser_listener, lost_topic),
                (service._on_connection_lost, lost_topic),
                (service._on_text_received, text_topic),
            ):
                try:
                    pub.unsubscribe(callback, topic)
                except Exception:
                    pass
            pub.getDefaultTopicMgr().delTopic(lost_topic)
            pub.getDefaultTopicMgr().delTopic(text_topic)

    def test_retries_after_initial_connection_failure(self) -> None:
        service = RadioService()
        interface = make_interface()
        stopped = Event()

        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                side_effect=[
                    RadioConnectionError(
                        "radio missing",
                        state=RadioState.OFFLINE,
                    ),
                    interface,
                ],
            ),
        ):
            events = service.connection_events(
                retry_delay=0,
                stop_event=stopped,
            )

            self.assertEqual(next(events).state, RadioState.CONNECTING)
            offline = next(events)
            self.assertEqual(offline.state, RadioState.OFFLINE)
            self.assertEqual(offline.message, "radio missing")
            self.assertEqual(next(events).state, RadioState.CONNECTING)
            self.assertEqual(next(events).state, RadioState.ONLINE)

            stopped.set()
            with self.assertRaises(StopIteration):
                next(events)

    def test_detects_when_serial_device_disappears(self) -> None:
        service = RadioService()
        interface = make_interface()
        stopped = Event()

        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface),
            patch.object(service, "_device_exists", return_value=False),
        ):
            events = service.connection_events(
                retry_delay=0,
                stop_event=stopped,
                poll_interval=0.001,
            )

            self.assertEqual(next(events).state, RadioState.CONNECTING)
            self.assertEqual(next(events).state, RadioState.ONLINE)
            offline = next(events)
            self.assertEqual(offline.state, RadioState.OFFLINE)
            self.assertIn("was lost", offline.message)

            stopped.set()
            with self.assertRaises(StopIteration):
                next(events)

        interface.close.assert_called()

    def test_reports_error_and_keeps_retrying(self) -> None:
        service = RadioService()
        info = RadioInfo(
            device_path="/dev/ttyUSB0",
            node_id="!12345678",
            long_name="Test Node",
            short_name="TEST",
            firmware_version="2.7.0",
            known_nodes=1,
        )
        stopped = Event()

        with patch.object(
            service,
            "connect",
            side_effect=[RadioConnectionError("SDK failure"), info],
        ):
            events = service.connection_events(
                retry_delay=0,
                stop_event=stopped,
            )

            self.assertEqual(next(events).state, RadioState.CONNECTING)
            error = next(events)
            self.assertEqual(error.state, RadioState.ERROR)
            self.assertEqual(error.message, "SDK failure")
            self.assertEqual(next(events).state, RadioState.CONNECTING)
            online = next(events)
            self.assertEqual(online.state, RadioState.ONLINE)
            self.assertEqual(online.info, info)

            stopped.set()
            with self.assertRaises(StopIteration):
                next(events)

    def test_active_node_count_reads_node_database_without_sending(self) -> None:
        # now=10_000 (not the old 1_000): ACTIVE_WINDOW_SECONDS is now
        # 7200 (2h, firmware-aligned -- see node_activity.py), so node 3's
        # deliberately-excluded age must exceed that, not the old 300s.
        service = RadioService()
        interface = make_interface()
        local_number = interface.myInfo.my_node_num
        interface.nodesByNum[local_number]["lastHeard"] = 10_000
        interface.nodesByNum[2] = {
            "user": {"id": "!00000002"},
            "lastHeard": 9_990,
        }
        interface.nodesByNum[3] = {
            "user": {"id": "!00000003"},
            "lastHeard": 2_000,  # age 8_000s > ACTIVE_WINDOW_SECONDS (7_200)
        }
        interface.sendText = Mock()
        service._interface = interface

        self.assertEqual(service.active_node_count(now=10_000), 1)
        interface.sendText.assert_not_called()

        interface.nodesByNum[4] = {
            "user": {"id": "!00000004"},
            "lastHeard": 9_999,
        }
        self.assertEqual(service.active_node_count(now=10_000), 2)
        interface.sendText.assert_not_called()

    def test_active_node_count_is_unavailable_while_disconnected(self) -> None:
        self.assertIsNone(RadioService().active_node_count(now=1_000))

    def test_known_nodes_are_normalized_passively_and_include_local(self) -> None:
        service = RadioService()
        interface = make_interface()
        interface.nodesByNum[0xABC12345] = {
            "user": {
                "id": "!abc12345",
                "longName": "Alice Trail",
                "shortName": "ALCE",
            },
            "hopsAway": 1,
            "lastHeard": 900.0,
            "position": {
                "latitude": 40.7736,
                "longitude": -73.9566,
                "time": 1_700_000_100.0,
            },
        }
        interface.nodesByNum[0xB0B00002] = {
            "user": {"shortName": "BOB"},
            "hopsAway": -1,
            "lastHeard": "malformed",
        }
        interface.sendText = Mock()
        service._interface = interface
        service._activity_local_node_id = "!12345678"
        service._direct_observations["!abc12345"] = 950.0
        service._direct_observations["!new00004"] = 975.0

        nodes = service.get_known_nodes()

        self.assertEqual(len(nodes), 4)
        local = next(node for node in nodes if node.is_local)
        self.assertEqual(local.node_id, "!12345678")
        alice = next(node for node in nodes if node.node_id == "!abc12345")
        self.assertEqual(alice.long_name, "Alice Trail")
        self.assertEqual(alice.hops_away, 1)
        self.assertEqual(alice.last_heard, 950.0)
        self.assertEqual(
            alice.position, GeoPosition(40.7736, -73.9566, 1_700_000_100.0)
        )
        bob = next(node for node in nodes if node.short_name == "BOB")
        self.assertEqual(bob.node_id, "!b0b00002")
        self.assertIsNone(bob.hops_away)
        self.assertIsNone(bob.last_heard)
        self.assertIsNone(bob.position)
        observed = next(node for node in nodes if node.node_id == "!new00004")
        self.assertIsNone(observed.hops_away)
        self.assertEqual(observed.last_heard, 975.0)
        interface.sendText.assert_not_called()

        service._interface = None
        self.assertEqual(service.get_known_nodes(), ())

    def test_received_text_updates_passive_activity_without_transmitting(self) -> None:
        service = RadioService()
        interface = make_interface()
        interface.nodesByNum[2] = {
            "user": {"id": "!00000002", "longName": "Alice"},
            "lastHeard": 100,
        }
        interface.sendText = Mock()
        service._interface = interface
        service._activity_local_node_id = "!12345678"
        packet = {
            "from": 2,
            "fromId": "!00000002",
            "id": 77,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }

        with patch("radio_service.time.time", return_value=1_000):
            service._on_text_received(packet, interface)
            service._on_text_received(packet, interface)

        self.assertEqual(service.active_node_count(now=1_000), 1)
        self.assertEqual(
            service.active_node_count(now=1_000 + ACTIVE_WINDOW_SECONDS), 0
        )
        interface.sendText.assert_not_called()

    def test_direct_activity_excludes_self_and_clears_on_device_switch(self) -> None:
        service = RadioService()
        interface = make_interface()
        service._interface = interface
        service._activity_local_node_id = "!12345678"
        service._record_direct_observation("!12345678", 1_000)
        service._record_direct_observation("!00000002", 1_000)

        self.assertEqual(service.active_node_count(now=1_000), 1)
        service.set_device_path("/dev/ttyUSB1")
        self.assertEqual(service._direct_observations, {})

    def test_set_long_name_uses_sdk_local_node_and_refreshes_info(self) -> None:
        service = RadioService()
        interface = make_interface()
        interface.localNode = SimpleNamespace(channels=(), setOwner=Mock())
        service._interface = interface

        info = service.set_long_name("  Clockwork Nomad  ")

        interface.localNode.setOwner.assert_called_once_with(
            long_name="Clockwork Nomad"
        )
        self.assertEqual(info.long_name, "Clockwork Nomad")
        self.assertEqual(
            interface.nodesByNum[interface.myInfo.my_node_num]["user"]["longName"],
            "Clockwork Nomad",
        )

    def test_long_name_validation_blocks_empty_oversize_and_disconnected(self) -> None:
        service = RadioService()
        with self.assertRaises(RadioIdentityError):
            service.set_long_name("Offline")

        interface = make_interface()
        interface.localNode = SimpleNamespace(channels=(), setOwner=Mock())
        service._interface = interface
        for invalid in ("   ", "x" * 40, "🚲" * 10):
            with self.subTest(invalid=invalid), self.assertRaises(RadioIdentityError):
                service.set_long_name(invalid)
        interface.localNode.setOwner.assert_not_called()

    def test_set_short_name_uses_sdk_and_enforces_four_utf8_bytes(self) -> None:
        service = RadioService()
        interface = make_interface()
        interface.localNode = SimpleNamespace(channels=(), setOwner=Mock())
        service._interface = interface

        info = service.set_short_name("  MHU  ")

        interface.localNode.setOwner.assert_called_once_with(short_name="MHU")
        self.assertEqual(info.short_name, "MHU")
        self.assertEqual(
            interface.nodesByNum[interface.myInfo.my_node_num]["user"]["shortName"],
            "MHU",
        )

        interface.localNode.setOwner.reset_mock()
        for invalid in ("", "   ", "ABCDE", "ééé", "🚲A"):
            with self.subTest(invalid=invalid), self.assertRaises(RadioIdentityError):
                service.set_short_name(invalid)
        interface.localNode.setOwner.assert_not_called()

        # Four payload bytes are accepted even when they are not four glyphs.
        self.assertEqual(service.set_short_name("éé").short_name, "éé")
        interface.localNode.setOwner.assert_called_once_with(short_name="éé")


if __name__ == "__main__":
    unittest.main()
