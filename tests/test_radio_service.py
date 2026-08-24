"""Tests for RadioService connection state changes."""

from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import Mock, patch

from radio_service import RadioConnectionError, RadioInfo, RadioService, RadioState


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


if __name__ == "__main__":
    unittest.main()
