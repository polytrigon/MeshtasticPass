"""Tests for RadioService connection state changes."""

from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import Mock, patch

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
        service = RadioService()
        interface = make_interface()
        local_number = interface.myInfo.my_node_num
        interface.nodesByNum[local_number]["lastHeard"] = 1_000
        interface.nodesByNum[2] = {
            "user": {"id": "!00000002"},
            "lastHeard": 990,
        }
        interface.nodesByNum[3] = {
            "user": {"id": "!00000003"},
            "lastHeard": 600,
        }
        interface.sendText = Mock()
        service._interface = interface

        self.assertEqual(service.active_node_count(now=1_000), 1)
        interface.sendText.assert_not_called()

        interface.nodesByNum[4] = {
            "user": {"id": "!00000004"},
            "lastHeard": 999,
        }
        self.assertEqual(service.active_node_count(now=1_000), 2)
        interface.sendText.assert_not_called()

    def test_active_node_count_is_unavailable_while_disconnected(self) -> None:
        self.assertIsNone(RadioService().active_node_count(now=1_000))

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
        self.assertEqual(service.active_node_count(now=1_300), 0)
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


if __name__ == "__main__":
    unittest.main()
