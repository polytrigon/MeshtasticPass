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


def _make_config_interface(node_id: str, node_number: int = 0x12345678) -> SimpleNamespace:
    """Like make_interface(), plus enough localConfig/channels shape

    for RadioConfigurationSnapshot to have something concrete to read.
    """
    from meshtastic.protobuf import localonly_pb2

    local_config = localonly_pb2.LocalConfig()
    local_config.display.screen_on_secs = 30
    return SimpleNamespace(
        myInfo=SimpleNamespace(my_node_num=node_number),
        localNode=SimpleNamespace(
            localConfig=local_config,
            moduleConfig=localonly_pb2.LocalModuleConfig(),
            channels=[],
        ),
        nodesByNum={
            node_number: {"user": {"id": node_id, "longName": "Test", "shortName": "TST"}}
        },
        metadata=SimpleNamespace(firmware_version="2.7.11"),
        close=Mock(),
    )


class RadioConfigSnapshotLifecycleTests(unittest.TestCase):
    """Item 13: the connect-time config-snapshot cache/invalidation

    lifecycle, tested entirely through RadioService's own public
    surface (connect/close/set_device_path/config_snapshot/
    refresh_config_snapshot/write_verified_config_field).
    """

    def test_no_snapshot_before_connecting(self) -> None:
        service = RadioService("/dev/ttyUSB0")
        self.assertIsNone(service.config_snapshot())

    def test_snapshot_built_once_per_successful_connect(self) -> None:
        service = RadioService("/dev/ttyUSB0")
        interface = _make_config_interface("!12345678")
        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface),
        ):
            service.connect()
        snapshot = service.config_snapshot()
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.node_id, "!12345678")
        self.assertEqual(snapshot.connection_generation, 1)
        display = next(s for s in snapshot.local_config if s.section == "display")
        self.assertEqual(display.fields["screen_on_secs"], "30")

    def test_opening_config_repeatedly_sends_zero_additional_requests(self) -> None:
        """config_snapshot() is a pure cache read -- calling it many

        times must never touch the interface again.
        """
        service = RadioService("/dev/ttyUSB0")
        interface = _make_config_interface("!12345678")
        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface),
        ):
            service.connect()
        first = service.config_snapshot()
        for _ in range(20):
            self.assertIs(service.config_snapshot(), first)

    def test_reconnect_to_same_radio_rebuilds_the_snapshot(self) -> None:
        service = RadioService("/dev/ttyUSB0")
        interface = _make_config_interface("!12345678")
        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface),
        ):
            service.connect()
            first = service.config_snapshot()
            service.close()
            self.assertIsNone(service.config_snapshot())
            service.connect()
            second = service.config_snapshot()
        self.assertIsNot(first, second)
        self.assertEqual(second.node_id, first.node_id)
        self.assertEqual(second.connection_generation, first.connection_generation + 1)

    def test_different_node_id_invalidates_the_old_snapshot(self) -> None:
        """The V3 -> V4 transition: connecting to a DIFFERENT radio must

        never show the previous radio's stale settings.
        """
        service = RadioService("/dev/ttyUSB0")
        v3_interface = _make_config_interface("!aaaaaaaa", node_number=0xAAAAAAAA)
        v4_interface = _make_config_interface("!bbbbbbbb", node_number=0xBBBBBBBB)
        v4_interface.localNode.localConfig.display.screen_on_secs = 999

        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=v3_interface),
        ):
            service.connect()
        v3_snapshot = service.config_snapshot()
        self.assertEqual(v3_snapshot.node_id, "!aaaaaaaa")

        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=v4_interface),
        ):
            service.connect()
        v4_snapshot = service.config_snapshot()

        self.assertEqual(v4_snapshot.node_id, "!bbbbbbbb")
        self.assertNotEqual(v4_snapshot.node_id, v3_snapshot.node_id)
        display = next(s for s in v4_snapshot.local_config if s.section == "display")
        self.assertEqual(display.fields["screen_on_secs"], "999")
        self.assertGreater(v4_snapshot.connection_generation, v3_snapshot.connection_generation)

    def test_device_path_is_not_used_to_infer_capability(self) -> None:
        """Item 8: ttyUSB0 vs ttyACM0 must never determine what the

        snapshot reports -- only the interface's own reported state
        does. Two connections through DIFFERENT device paths but
        IDENTICAL interface state must produce identical config.
        """
        service = RadioService("/dev/ttyUSB0")
        interface_usb = _make_config_interface("!12345678")
        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface_usb),
        ):
            service.connect()
        usb_snapshot = service.config_snapshot()

        service2 = RadioService("/dev/ttyACM0")
        interface_acm = _make_config_interface("!12345678")
        with (
            patch.object(service2, "_check_device"),
            patch.object(service2, "_open_interface", return_value=interface_acm),
        ):
            service2.connect()
        acm_snapshot = service2.config_snapshot()

        self.assertEqual(usb_snapshot.hardware.hw_model_name, acm_snapshot.hardware.hw_model_name)
        self.assertEqual(usb_snapshot.local_config, acm_snapshot.local_config)
        self.assertEqual(usb_snapshot.device_path, "/dev/ttyUSB0")
        self.assertEqual(acm_snapshot.device_path, "/dev/ttyACM0")

    def test_refresh_only_rebuilds_when_explicitly_activated(self) -> None:
        service = RadioService("/dev/ttyUSB0")
        interface = _make_config_interface("!12345678")
        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface),
        ):
            service.connect()
        first = service.config_snapshot()
        for _ in range(5):
            self.assertIs(service.config_snapshot(), first)

        interface.localNode.localConfig.display.screen_on_secs = 60
        refreshed = service.refresh_config_snapshot()
        self.assertIsNot(refreshed, first)
        display = next(s for s in refreshed.local_config if s.section == "display")
        self.assertEqual(display.fields["screen_on_secs"], "60")

    def test_unconfirmed_write_does_not_touch_the_snapshot(self) -> None:
        service = RadioService("/dev/ttyUSB0")
        interface = _make_config_interface("!12345678")
        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface),
        ):
            service.connect()
        before = service.config_snapshot()

        result = service.write_verified_config_field("display", "screen_on_secs", 999, timeout=0.1)

        self.assertFalse(result.applied)
        self.assertIs(service.config_snapshot(), before)

    def test_close_invalidates_the_snapshot(self) -> None:
        service = RadioService("/dev/ttyUSB0")
        interface = _make_config_interface("!12345678")
        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface),
        ):
            service.connect()
        self.assertIsNotNone(service.config_snapshot())
        service.close()
        self.assertIsNone(service.config_snapshot())

    def test_set_device_path_invalidates_the_snapshot(self) -> None:
        service = RadioService("/dev/ttyUSB0")
        interface = _make_config_interface("!12345678")
        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface),
        ):
            service.connect()
        self.assertIsNotNone(service.config_snapshot())
        service.set_device_path("/dev/ttyACM0")
        self.assertIsNone(service.config_snapshot())


def _make_swap_interface(node_id: str, node_number: int, others=None):
    """A fake interface whose NodeDB may ALSO carry other nodes' records

    (e.g. an old radio's own entry, still legitimately remembered by
    the new radio's NodeDB -- see MESH FOLLOW-UP item 4). Only
    `node_number` is ever the local node.
    """
    nodes_by_number = dict(others or {})
    nodes_by_number[node_number] = {
        "user": {"id": node_id, "longName": f"Radio {node_id}", "shortName": node_id[-4:]}
    }
    return SimpleNamespace(
        myInfo=SimpleNamespace(my_node_num=node_number),
        localNode=object(),
        nodesByNum=nodes_by_number,
        metadata=SimpleNamespace(firmware_version="2.7.11"),
        close=Mock(),
    )


class LocalNodeIdentityRadioSwapTests(unittest.TestCase):
    """MESH FOLLOW-UP items 2-13, 23-24: the local/YOU identity must

    always be derived from the CURRENTLY CONNECTED radio, never a
    previous physical radio's node ID -- exercised entirely through
    RadioService's own public surface (connect/close/set_device_path/
    get_known_nodes), the sole source of the is_local flag MESH's
    working set relies on.
    """

    def test_radio_swap_on_same_path_makes_new_radio_local(self) -> None:
        service = RadioService("/dev/ttyACM0")
        a_number, b_number = 0xAAAAAAAA, 0xBBBBBBBB

        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                return_value=_make_swap_interface("!aaaaaaaa", a_number),
            ),
        ):
            service.connect()
        nodes = {n.node_id: n for n in service.get_known_nodes()}
        self.assertTrue(nodes["!aaaaaaaa"].is_local)

        service.close()

        # V4's own NodeDB still legitimately remembers the old V3 node
        # (item 4) -- it must become an ordinary remote node, never YOU,
        # never hidden, never deleted.
        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                return_value=_make_swap_interface(
                    "!bbbbbbbb", b_number, others={a_number: {
                        "user": {"id": "!aaaaaaaa", "longName": "V3 Radio", "shortName": "V3"}
                    }}
                ),
            ),
        ):
            service.connect()
        nodes = {n.node_id: n for n in service.get_known_nodes()}
        self.assertTrue(nodes["!bbbbbbbb"].is_local)
        self.assertFalse(nodes["!aaaaaaaa"].is_local)
        self.assertEqual(sum(1 for n in nodes.values() if n.is_local), 1)
        # device_path was never changed -- same /dev/ttyACM0 throughout.
        self.assertEqual(service.device_path, "/dev/ttyACM0")

    def test_radio_swap_on_different_path_makes_new_radio_local(self) -> None:
        service = RadioService("/dev/ttyUSB0")
        a_number, b_number = 0xAAAAAAAA, 0xBBBBBBBB

        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                return_value=_make_swap_interface("!aaaaaaaa", a_number),
            ),
        ):
            service.connect()
        service.close()
        service.set_device_path("/dev/ttyACM0")

        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                return_value=_make_swap_interface("!bbbbbbbb", b_number),
            ),
        ):
            service.connect()
        nodes = {n.node_id: n for n in service.get_known_nodes()}
        self.assertTrue(nodes["!bbbbbbbb"].is_local)
        self.assertEqual(sum(1 for n in nodes.values() if n.is_local), 1)

    def test_close_clears_stale_local_identity_before_reconnect(self) -> None:
        """A disconnect for ANY reason invalidates the previous radio's

        local identity immediately -- get_known_nodes() already returns
        () while disconnected, so there is no "wrong YOU" to show
        during the gap, only correctly no YOU at all (item 5).
        """
        service = RadioService("/dev/ttyACM0")
        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                return_value=_make_swap_interface("!aaaaaaaa", 0xAAAAAAAA),
            ),
        ):
            service.connect()
        self.assertIsNotNone(service._activity_local_node_id)
        service.close()
        self.assertIsNone(service._activity_local_node_id)
        self.assertEqual(service.get_known_nodes(), ())

    def test_node_number_with_high_bit_set_still_matches_as_local(self) -> None:
        """Item 7: a node number with bit 31 set can surface as either a

        negative or unsigned Python int from different code paths --
        canonical masking (see radio_service._canonical_node_number)
        must make the comparison exact regardless of which sign
        representation myInfo.my_node_num happens to report.
        """
        service = RadioService("/dev/ttyACM0")
        high_bit_number = 0xF0000001  # > 2**31, ambiguous as signed/unsigned
        interface = _make_swap_interface("!f0000001", high_bit_number)
        # Simulate a code path reporting my_node_num as the SIGNED
        # 32-bit interpretation of the identical bit pattern.
        interface.myInfo.my_node_num = high_bit_number - (1 << 32)
        with (
            patch.object(service, "_check_device"),
            patch.object(service, "_open_interface", return_value=interface),
        ):
            service.connect()
        nodes = {n.node_id: n for n in service.get_known_nodes()}
        self.assertTrue(nodes["!f0000001"].is_local)

    def test_exactly_one_you_after_swap_with_extra_active_nodes(self) -> None:
        """Full item 23 scenario at the RadioService layer: V4's NodeDB

        contains the old V3 node A plus the new local B plus two other
        real nodes X and Y -- exactly one is_local afterward, and A/X/Y
        are all ordinary ('is_local=False') entries.
        """
        service = RadioService("/dev/ttyACM0")
        a_number, b_number, x_number, y_number = (
            0xAAAAAAAA,
            0xBBBBBBBB,
            0xC0000001,
            0xD0000001,
        )
        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                return_value=_make_swap_interface("!aaaaaaaa", a_number),
            ),
        ):
            service.connect()
        service.close()

        others = {
            a_number: {
                "user": {"id": "!aaaaaaaa", "longName": "V3 Radio", "shortName": "V3"}
            },
            x_number: {"user": {"id": "!c0000001", "longName": "X", "shortName": "X"}},
            y_number: {"user": {"id": "!d0000001", "longName": "Y", "shortName": "Y"}},
        }
        with (
            patch.object(service, "_check_device"),
            patch.object(
                service,
                "_open_interface",
                return_value=_make_swap_interface("!bbbbbbbb", b_number, others=others),
            ),
        ):
            service.connect()
        nodes = {n.node_id: n for n in service.get_known_nodes()}
        self.assertEqual(len(nodes), 4)
        self.assertEqual(
            {node_id for node_id, node in nodes.items() if node.is_local}, {"!bbbbbbbb"}
        )
        self.assertFalse(nodes["!aaaaaaaa"].is_local)
        self.assertFalse(nodes["!c0000001"].is_local)
        self.assertFalse(nodes["!d0000001"].is_local)


if __name__ == "__main__":
    unittest.main()
