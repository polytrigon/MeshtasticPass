"""Hardware-free tests for the non-visual TUI controller."""

from __future__ import annotations

from threading import Event
import unittest
from unittest.mock import Mock, patch

from app_controller import (
    RadioMonitor,
    create_radio_service,
    outgoing_chat_entry,
    received_chat_entry,
)
from radio_service import RadioEvent, RadioState, ReceivedMessage


class AppControllerTests(unittest.TestCase):
    def test_selects_simulated_service(self) -> None:
        with patch("app_controller.SimulatedRadioService") as simulator:
            selected = create_radio_service(True, "/dev/test")

        self.assertIs(selected, simulator.return_value)
        simulator.assert_called_once_with()

    def test_selects_real_service_and_device(self) -> None:
        with patch("app_controller.RadioService") as real_service:
            selected = create_radio_service(False, "/dev/ttyACM0")

        self.assertIs(selected, real_service.return_value)
        real_service.assert_called_once_with(device_path="/dev/ttyACM0")

    def test_received_message_prefers_name_then_node_id(self) -> None:
        named = self.make_message(long_name="Alice Trail", short_name="ALCE")
        fallback = self.make_message(long_name=None, short_name=None)

        entry = received_chat_entry(named, accepted_at=123.0)
        self.assertEqual(entry.author, "Alice Trail")
        self.assertEqual(entry.accepted_at, 123.0)
        self.assertEqual(received_chat_entry(fallback).author, "!a11ce001")
        self.assertFalse(entry.outgoing)

    def test_outgoing_entry_uses_you_marker(self) -> None:
        entry = outgoing_chat_entry("hello", accepted_at=456.0)

        self.assertEqual(entry.author, "YOU")
        self.assertEqual(entry.text, "hello")
        self.assertEqual(entry.accepted_at, 456.0)
        self.assertTrue(entry.outgoing)

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
