"""Tests for the connect-time RadioConfigurationSnapshot builder.

Fake interfaces are built the same way tests/test_radio_capabilities.py
builds them: real meshtastic protobuf message objects wrapped in
SimpleNamespace, never hand-rolled dict shapes -- so a passing test
here proves behavior against the actual installed schema.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from meshtastic.protobuf import localonly_pb2, mesh_pb2

import radio_capabilities as rc
from radio_config_snapshot import build_radio_configuration_snapshot


def _make_local_node(*, localConfig=None, moduleConfig=None, channels=None):
    return SimpleNamespace(
        localConfig=localConfig if localConfig is not None else localonly_pb2.LocalConfig(),
        moduleConfig=moduleConfig if moduleConfig is not None else localonly_pb2.LocalModuleConfig(),
        channels=channels,
    )


def _make_interface(**kwargs):
    defaults = dict(
        localNode=_make_local_node(),
        metadata=None,
        myInfo=None,
        nodesByNum={},
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class BuildRadioConfigurationSnapshotTests(unittest.TestCase):
    def test_absent_interface_yields_a_safe_empty_snapshot(self) -> None:
        snapshot = build_radio_configuration_snapshot(
            None, device_path="/dev/ttyUSB0", connection_generation=1, generated_at=100.0
        )
        self.assertIsNone(snapshot.node_id)
        self.assertEqual(snapshot.local_config, ())
        self.assertEqual(snapshot.module_config, ())
        self.assertEqual(snapshot.channels, ())
        self.assertFalse(snapshot.position.has_fix)
        self.assertEqual(snapshot.connection_generation, 1)
        self.assertEqual(snapshot.device_path, "/dev/ttyUSB0")

    def test_session_identity_fields_are_carried_through(self) -> None:
        interface = _make_interface(
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=0x1234),
            nodesByNum={0x1234: {"user": {"id": "!00001234"}}},
        )
        snapshot = build_radio_configuration_snapshot(
            interface, device_path="/dev/ttyACM0", connection_generation=7, generated_at=200.0
        )
        self.assertEqual(snapshot.node_id, "!00001234")
        self.assertEqual(snapshot.connection_generation, 7)
        self.assertEqual(snapshot.device_path, "/dev/ttyACM0")
        self.assertEqual(snapshot.generated_at, 200.0)

    def test_local_and_module_config_sections_are_populated(self) -> None:
        local_config = localonly_pb2.LocalConfig()
        local_config.display.screen_on_secs = 30
        module_config = localonly_pb2.LocalModuleConfig()
        module_config.mqtt.enabled = True
        interface = _make_interface(
            localNode=_make_local_node(localConfig=local_config, moduleConfig=module_config)
        )
        snapshot = build_radio_configuration_snapshot(
            interface, device_path="/dev/ttyUSB0", connection_generation=1, generated_at=0.0
        )
        display = next(s for s in snapshot.local_config if s.section == "display")
        self.assertEqual(display.fields["screen_on_secs"], "30")
        mqtt = next(s for s in snapshot.module_config if s.section == "mqtt")
        self.assertEqual(mqtt.fields["enabled"], "True")

    def test_secrets_are_never_present_anywhere_in_the_snapshot(self) -> None:
        """Item 12: prove it end-to-end through the assembled snapshot,

        not just at describe_scalar_fields' own layer.
        """
        local_config = localonly_pb2.LocalConfig()
        local_config.network.wifi_psk = "supersecretwifi"
        local_config.security.private_key = b"supersecretprivatekey"
        module_config = localonly_pb2.LocalModuleConfig()
        module_config.mqtt.password = "supersecretmqtt"
        channel = SimpleNamespace(
            index=0,
            role=1,
            settings=SimpleNamespace(name="LongFast", psk=b"supersecretchannelpsk"),
        )
        interface = _make_interface(
            localNode=_make_local_node(
                localConfig=local_config,
                moduleConfig=module_config,
                channels=[channel],
            )
        )
        snapshot = build_radio_configuration_snapshot(
            interface, device_path="/dev/ttyUSB0", connection_generation=1, generated_at=0.0
        )

        secrets = (
            "supersecretwifi",
            "supersecretmqtt",
            "supersecretprivatekey",
            "supersecretchannelpsk",
        )
        haystacks = [repr(snapshot)]
        for section in (*snapshot.local_config, *snapshot.module_config):
            haystacks.extend(section.fields.values())
        for channel_report in snapshot.channels:
            haystacks.append(channel_report.psk)
        blob = " ".join(str(item) for item in haystacks)
        for secret in secrets:
            self.assertNotIn(secret, blob)

    def test_channel_psk_reported_as_configured_not_configured(self) -> None:
        channel = SimpleNamespace(
            index=0, role=1, settings=SimpleNamespace(name="LongFast", psk=b"secret")
        )
        interface = _make_interface(
            localNode=_make_local_node(channels=[channel])
        )
        snapshot = build_radio_configuration_snapshot(
            interface, device_path="/dev/ttyUSB0", connection_generation=1, generated_at=0.0
        )
        self.assertEqual(snapshot.channels[0].psk, rc.REDACTED_CONFIGURED)

    def test_gps_capability_present_with_no_current_fix(self) -> None:
        """Item 9: no lat/lon must render as an honest no-fix state,

        not an error and not a crash -- the exact real-hardware
        scenario this item documents.
        """
        local_config = localonly_pb2.LocalConfig()
        local_config.position.gps_enabled = True
        local_config.position.gps_update_interval = 120
        interface = _make_interface(
            localNode=_make_local_node(localConfig=local_config),
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=0x99),
            nodesByNum={0x99: {"user": {"id": "!00000099"}}},
        )
        snapshot = build_radio_configuration_snapshot(
            interface, device_path="/dev/ttyUSB0", connection_generation=1, generated_at=0.0
        )
        self.assertTrue(snapshot.position.gps_capable)
        self.assertIsNotNone(snapshot.position.config)
        self.assertEqual(snapshot.position.config.fields["gps_enabled"], "True")
        self.assertFalse(snapshot.position.has_fix)
        self.assertIsNone(snapshot.position.latitude)
        self.assertIsNone(snapshot.position.longitude)

    def test_gps_fix_present_reports_latitude_longitude_and_altitude(self) -> None:
        interface = _make_interface(
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=0x99),
            nodesByNum={
                0x99: {
                    "user": {"id": "!00000099"},
                    "position": {
                        "latitudeI": 407128000,
                        "longitudeI": -740060000,
                        "altitude": 42,
                        "time": 1_700_000_000,
                    },
                }
            },
        )
        snapshot = build_radio_configuration_snapshot(
            interface, device_path="/dev/ttyUSB0", connection_generation=1, generated_at=0.0
        )
        self.assertTrue(snapshot.position.has_fix)
        self.assertAlmostEqual(snapshot.position.latitude, 40.7128, places=4)
        self.assertAlmostEqual(snapshot.position.longitude, -74.0060, places=4)
        self.assertEqual(snapshot.position.altitude, 42)
        self.assertEqual(snapshot.position.last_position_time, 1_700_000_000.0)

    def test_malformed_position_record_never_crashes(self) -> None:
        interface = _make_interface(
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=0x99),
            nodesByNum={0x99: {"user": {"id": "!x"}, "position": "not-a-dict"}},
        )
        snapshot = build_radio_configuration_snapshot(
            interface, device_path="/dev/ttyUSB0", connection_generation=1, generated_at=0.0
        )
        self.assertFalse(snapshot.position.has_fix)

    def test_missing_channels_attribute_yields_empty_channels(self) -> None:
        interface = _make_interface(localNode=_make_local_node(channels=None))
        snapshot = build_radio_configuration_snapshot(
            interface, device_path="/dev/ttyUSB0", connection_generation=1, generated_at=0.0
        )
        self.assertEqual(snapshot.channels, ())

    def test_unsupported_schema_fields_render_safely_not_crash(self) -> None:
        """A local_node missing localConfig/moduleConfig entirely (an

        object() with no attributes, the same defensive shape
        radio_capabilities' own tests exercise) must yield an empty,
        not a crashing, snapshot.
        """
        interface = _make_interface(localNode=object())
        snapshot = build_radio_configuration_snapshot(
            interface, device_path="/dev/ttyUSB0", connection_generation=1, generated_at=0.0
        )
        self.assertEqual(snapshot.local_config, ())
        self.assertEqual(snapshot.module_config, ())
        self.assertFalse(snapshot.position.gps_capable)


if __name__ == "__main__":
    unittest.main()
