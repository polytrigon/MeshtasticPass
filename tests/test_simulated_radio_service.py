"""Hardware-free tests for SimulatedRadioService."""

from dataclasses import replace
import sys
from threading import Event
import unittest
from unittest.mock import Mock, patch

from node_activity import ACTIVE_WINDOW_SECONDS
from radio_service import RadioIdentityError, RadioState
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
        self.assertEqual(service.info.device_path, "/dev/ttyUSB0")
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

    def test_fake_usb_choices_switch_without_real_enumeration(self) -> None:
        service = self.make_service()
        with patch("serial_devices.discover_serial_devices") as discover:
            self.assertEqual(
                service.available_device_paths(),
                ("/dev/ttyUSB0", "/dev/ttyUSB1"),
            )
            service.set_device_path("/dev/ttyUSB1")
        discover.assert_not_called()
        self.assertEqual(service.device_path, "/dev/ttyUSB1")
        self.assertEqual(service.info.device_path, "/dev/ttyUSB1")

    def test_activity_is_deterministic_and_ages_without_messages(self) -> None:
        service = self.make_service()
        self.assertIsNone(service.active_node_count(now=1_000))

        with patch("simulated_radio_service.time.time", return_value=1_000):
            service.connect()

        # Alice(30s)/Bob(299s)/Cafe(400s) are well inside the firmware-aligned
        # ACTIVE_WINDOW_SECONDS (7200s); NOLN/No Short Name (600s each) are
        # the next to age out, exactly when the window elapses since their
        # last-heard time -- BAD's malformed "recent" value and NONE's
        # missing timestamp are never active at any `now`.
        self.assertEqual(service.active_node_count(now=1_000), 5)
        self.assertEqual(
            service.active_node_count(now=1_000 + ACTIVE_WINDOW_SECONDS - 600), 3
        )
        self.assertEqual(service.info.known_nodes, 8)
        self.assertEqual(service.sent_messages, ())

        service.close()
        self.assertIsNone(service.active_node_count(now=1_002))

    def test_known_nodes_are_stable_diverse_and_hardware_free(self) -> None:
        service = self.make_service()
        self.assertEqual(service.get_known_nodes(), ())
        with patch("simulated_radio_service.time.time", return_value=1_000):
            service.connect()

        nodes = service.get_known_nodes()
        self.assertEqual(len(nodes), 8)
        self.assertEqual(sum(node.is_local for node in nodes), 1)
        self.assertGreaterEqual(sum(node.hops_away == 1 for node in nodes), 2)
        self.assertTrue(any(node.hops_away == 2 for node in nodes))
        self.assertTrue(any(node.hops_away is None for node in nodes))
        self.assertTrue(any(node.long_name is None for node in nodes))
        self.assertTrue(any(node.short_name is None for node in nodes))
        local = next(node for node in nodes if node.is_local)
        self.assertEqual(local.position, SIMULATED_LOCAL_POSITION)
        self.assertTrue(any(node.position is not None for node in nodes if not node.is_local))
        self.assertTrue(any(node.position is None for node in nodes if not node.is_local))
        self.assertEqual(service.sent_messages, ())

    def test_direct_message_activity_and_identity_edit_are_deterministic(self) -> None:
        service = self.make_service()
        with patch("simulated_radio_service.time.time", return_value=1_000):
            service.connect()
            service.emit_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    sender_node_id="!new00006",
                    packet_id=999,
                )
            )

        # 5 NodeDB-active nodes at now=1_000 (Alice/Bob/Cafe/NOLN/No Short
        # Name, all inside the firmware-aligned ACTIVE_WINDOW_SECONDS) plus
        # the freshly direct-observed "!new00006" sender.
        self.assertEqual(service.active_node_count(now=1_000), 6)
        info = service.set_long_name("Clockwork Nomad")
        self.assertEqual(info.long_name, "Clockwork Nomad")
        self.assertEqual(service.info.long_name, "Clockwork Nomad")
        info = service.set_short_name("MHU")
        self.assertEqual(info.short_name, "MHU")
        self.assertEqual(service.info.short_name, "MHU")
        self.assertEqual(service.sent_messages, ())

        service.set_device_path("/dev/ttyUSB1")
        self.assertEqual(service._direct_observations, {})
        with self.assertRaises(RadioIdentityError):
            service.set_long_name("Offline Name")
        with self.assertRaises(RadioIdentityError):
            service.set_short_name("OFF")

    def test_no_snapshot_before_connect(self) -> None:
        service = self.make_service()
        self.assertIsNone(service.config_snapshot())

    def test_snapshot_built_once_and_cached(self) -> None:
        service = self.make_service()
        service.connect()
        first = service.config_snapshot()
        self.assertIsNotNone(first)
        self.assertEqual(first.node_id, service.info.node_id)
        for _ in range(10):
            self.assertIs(service.config_snapshot(), first)

    def test_close_invalidates_the_snapshot(self) -> None:
        service = self.make_service()
        service.connect()
        self.assertIsNotNone(service.config_snapshot())
        service.close()
        self.assertIsNone(service.config_snapshot())

    def test_reconnect_rebuilds_with_a_newer_generation(self) -> None:
        service = self.make_service()
        service.connect()
        first = service.config_snapshot()
        service.close()
        service.connect()
        second = service.config_snapshot()
        self.assertIsNot(second, first)
        self.assertGreater(second.connection_generation, first.connection_generation)

    def test_confirmed_write_rebuilds_the_snapshot(self) -> None:
        service = self.make_service()
        service.connect()
        before = service.config_snapshot()
        result = service.write_verified_config_field("display", "flip_screen", True)
        self.assertTrue(result.applied)
        after = service.config_snapshot()
        self.assertIsNot(after, before)
        display = next(s for s in after.local_config if s.section == "display")
        self.assertEqual(display.fields["flip_screen"], "True")

    def test_refresh_only_rebuilds_when_explicitly_activated(self) -> None:
        service = self.make_service()
        service.connect()
        first = service.config_snapshot()
        for _ in range(5):
            self.assertIs(service.config_snapshot(), first)
        refreshed = service.refresh_config_snapshot()
        self.assertIsNot(refreshed, first)

    def test_secrets_are_absent_from_channel_reports(self) -> None:
        service = self.make_service()
        service.connect()
        snapshot = service.config_snapshot()
        self.assertTrue(snapshot.channels)
        for channel in snapshot.channels:
            self.assertIn(channel.psk, ("configured", "not configured"))

    def test_gps_position_has_no_fix_by_default(self) -> None:
        service = self.make_service()
        service.connect()
        position = service.config_snapshot().position
        self.assertTrue(position.gps_capable)
        self.assertFalse(position.has_fix)
        self.assertIsNone(position.latitude)
        self.assertIsNone(position.longitude)


if __name__ == "__main__":
    unittest.main()
