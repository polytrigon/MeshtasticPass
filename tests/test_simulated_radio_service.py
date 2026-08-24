"""Hardware-free tests for SimulatedRadioService."""

import sys
from threading import Event
import unittest
from unittest.mock import Mock, patch

from radio_service import RadioState
from simulated_radio_service import (
    SIMULATED_MESSAGES,
    SIMULATED_LOCAL_POSITION,
    SimulatedRadioService,
)


class SimulatedRadioServiceTests(unittest.TestCase):
    def make_service(self) -> SimulatedRadioService:
        return SimulatedRadioService(connect_delay=0, message_interval=0)

    def test_connecting_to_online_transition(self) -> None:
        service = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        events = service.connection_events(poll_interval=0.001)

        connecting = next(events)
        online = next(events)

        self.assertEqual(connecting.state, RadioState.CONNECTING)
        self.assertEqual(online.state, RadioState.ONLINE)
        self.assertEqual(online.info, service.info)
        self.assertEqual(service.info.device_path, "simulated://meshtastic")
        self.assertEqual(service.info.node_id, "!51a00001")
        service.close()
        with self.assertRaises(StopIteration):
            next(events)

    def test_messages_are_deterministic(self) -> None:
        service = self.make_service()
        stopped = Event()
        received = []

        def receive(message: object) -> None:
            received.append(message)
            if len(received) == len(SIMULATED_MESSAGES):
                stopped.set()

        service.add_message_handler(receive)
        states = list(service.connection_events(stop_event=stopped))

        self.assertEqual(received, list(SIMULATED_MESSAGES))
        self.assertTrue(
            all(message.radio_rx_at is not None for message in received)
        )
        self.assertTrue(
            all(message.origin_sent_at is None for message in received)
        )
        self.assertEqual(received[0].local_position, SIMULATED_LOCAL_POSITION)
        self.assertIsNotNone(received[0].sender_position)
        self.assertIsNone(received[1].sender_position)
        self.assertEqual(
            [event.state for event in states],
            [RadioState.CONNECTING, RadioState.ONLINE],
        )

    def test_each_handler_receives_each_message_once(self) -> None:
        service = self.make_service()
        stopped = Event()
        handler = Mock()

        def stop_after_last_message(message: object) -> None:
            if message == SIMULATED_MESSAGES[-1]:
                stopped.set()

        service.add_message_handler(handler)
        service.add_message_handler(handler)
        service.add_message_handler(stop_after_last_message)
        list(service.connection_events(stop_event=stopped))

        self.assertEqual(handler.call_count, len(SIMULATED_MESSAGES))
        self.assertEqual(
            [item.args[0] for item in handler.call_args_list],
            list(SIMULATED_MESSAGES),
        )

    def test_clean_shutdown(self) -> None:
        service = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        events = service.connection_events(poll_interval=0.001)
        next(events)
        next(events)

        service.close()

        with self.assertRaises(StopIteration):
            next(events)
        self.assertTrue(service.is_closed)

    def test_supports_scripted_connection_states(self) -> None:
        service = SimulatedRadioService(
            connect_delay=0,
            message_interval=0,
            scripted_messages=(),
        )
        events = service.connection_events(poll_interval=0.001)
        next(events)
        next(events)

        service.simulate_disconnect()
        self.assertEqual(next(events).state, RadioState.OFFLINE)
        service.simulate_error("test error")
        error = next(events)
        self.assertEqual(error.state, RadioState.ERROR)
        self.assertEqual(error.message, "test error")
        service.simulate_reconnect()
        self.assertEqual(next(events).state, RadioState.CONNECTING)
        self.assertEqual(next(events).state, RadioState.ONLINE)
        service.close()
        with self.assertRaises(StopIteration):
            next(events)

    def test_does_not_touch_sdk_or_serial_device(self) -> None:
        service = self.make_service()
        stopped = Event()

        def stop_after_last_message(message: object) -> None:
            if message == SIMULATED_MESSAGES[-1]:
                stopped.set()

        service.add_message_handler(stop_after_last_message)
        with (
            patch.dict(sys.modules, {"meshtastic": None}),
            patch(
                "radio_service.RadioService._open_interface",
                side_effect=AssertionError("real radio used"),
            ),
            patch(
                "pathlib.Path.exists",
                side_effect=AssertionError("serial path checked"),
            ),
        ):
            list(service.connection_events(stop_event=stopped))

        self.assertTrue(service.is_closed)


if __name__ == "__main__":
    unittest.main()
