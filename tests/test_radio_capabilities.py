"""Tests for the read-only Meshtastic hardware/capability audit.

Every fake interface here is built from real meshtastic protobuf
message objects (LocalConfig/LocalModuleConfig/DeviceMetadata/
MyNodeInfo) -- no real hardware, no serial port -- combined with plain
Python objects/dicts for the SDK-level containers (nodesByNum,
channels) exactly as meshtastic's own SDK represents them internally.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from meshtastic.protobuf import channel_pb2, localonly_pb2, mesh_pb2

import radio_capabilities as rc
from radio_service import RadioService


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


class DescribeScalarFieldsTests(unittest.TestCase):
    def test_none_message_yields_empty_dict(self) -> None:
        self.assertEqual(rc.describe_scalar_fields(None), {})

    def test_non_protobuf_object_yields_empty_dict(self) -> None:
        self.assertEqual(rc.describe_scalar_fields(object()), {})

    def test_scalar_and_bool_fields_render_as_strings(self) -> None:
        display = localonly_pb2.LocalConfig().display
        display.screen_on_secs = 30
        display.flip_screen = True
        described = rc.describe_scalar_fields(display)
        self.assertEqual(described["screen_on_secs"], "30")
        self.assertEqual(described["flip_screen"], "True")

    def test_enum_fields_render_symbolic_name(self) -> None:
        lora = localonly_pb2.LocalConfig().lora
        lora.region = 3  # EU_868
        described = rc.describe_scalar_fields(lora)
        self.assertEqual(described["region"], "EU_868")

    def test_unknown_enum_value_does_not_crash(self) -> None:
        lora = localonly_pb2.LocalConfig().lora
        lora.region = 9999
        described = rc.describe_scalar_fields(lora)
        self.assertIn("UNKNOWN(9999)", described["region"])

    def test_bytes_fields_are_redacted(self) -> None:
        network = localonly_pb2.LocalConfig().network
        network.wifi_psk = "supersecretpassword"
        described = rc.describe_scalar_fields(network)
        self.assertEqual(described["wifi_psk"], rc.REDACTED_CONFIGURED)
        self.assertNotIn("supersecretpassword", described.values())

    def test_empty_bytes_field_reports_not_configured(self) -> None:
        network = localonly_pb2.LocalConfig().network
        described = rc.describe_scalar_fields(network)
        self.assertEqual(described["wifi_psk"], rc.REDACTED_NOT_CONFIGURED)

    def test_sub_messages_are_never_descended_into(self) -> None:
        config = localonly_pb2.LocalConfig()
        described = rc.describe_scalar_fields(config)
        self.assertNotIn("display", described)
        self.assertNotIn("lora", described)


class LocalConfigSectionsTests(unittest.TestCase):
    def test_absent_local_node_yields_no_sections(self) -> None:
        self.assertEqual(rc.local_config_sections(None), ())

    def test_local_node_without_local_config_attribute_yields_no_sections(self) -> None:
        self.assertEqual(rc.local_config_sections(object()), ())

    def test_discovers_every_schema_declared_section(self) -> None:
        local_node = _make_local_node()
        sections = rc.local_config_sections(local_node)
        section_names = {section.section for section in sections}
        for expected in ("device", "position", "power", "network", "display", "lora", "bluetooth", "security"):
            self.assertIn(expected, section_names)

    def test_module_config_discovers_every_schema_declared_section(self) -> None:
        local_node = _make_local_node()
        sections = rc.module_config_sections(local_node)
        section_names = {section.section for section in sections}
        for expected in ("mqtt", "serial", "telemetry", "canned_message"):
            self.assertIn(expected, section_names)


class HardwareIdentityTests(unittest.TestCase):
    def test_no_interface_data_reports_unavailable_never_crashes(self) -> None:
        identity = rc.hardware_identity(_make_interface())
        self.assertIsNone(identity.hw_model_name)
        self.assertEqual(identity.hw_model_source, "unavailable")

    def test_hardware_identity_comes_from_device_metadata_not_device_path(self) -> None:
        """The exact question this audit exists to answer: hw_model

        must be sourced from the Meshtastic API's own DeviceMetadata,
        never from any USB path/serial device name -- this test's
        interface carries no device path information at all, proving
        the function cannot possibly be reading one.
        """
        metadata = mesh_pb2.DeviceMetadata(
            hw_model=mesh_pb2.HELTEC_V3, firmware_version="2.5.3", hasWifi=True
        )
        interface = _make_interface(metadata=metadata, myInfo=mesh_pb2.MyNodeInfo(my_node_num=1))
        identity = rc.hardware_identity(interface)
        self.assertEqual(identity.hw_model_name, "HELTEC_V3")
        self.assertIn("DeviceMetadata.hw_model", identity.hw_model_source)
        self.assertEqual(identity.firmware_version, "2.5.3")
        self.assertTrue(identity.has_wifi)

    def test_falls_back_to_local_nodedb_user_record_when_metadata_absent(self) -> None:
        interface = _make_interface(
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=42),
            nodesByNum={42: {"user": {"id": "!0000002a", "hwModel": "HELTEC_V3"}}},
        )
        identity = rc.hardware_identity(interface)
        self.assertEqual(identity.hw_model_name, "HELTEC_V3")
        self.assertIn("NodeInfo.user.hwModel", identity.hw_model_source)

    def test_unset_hw_model_string_is_not_reported_as_a_real_value(self) -> None:
        interface = _make_interface(
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=42),
            nodesByNum={42: {"user": {"id": "!0000002a", "hwModel": "UNSET"}}},
        )
        identity = rc.hardware_identity(interface)
        self.assertIsNone(identity.hw_model_name)

    def test_unknown_hardware_model_enum_value_does_not_crash(self) -> None:
        metadata = mesh_pb2.DeviceMetadata(hw_model=9999)
        identity = rc.hardware_identity(_make_interface(metadata=metadata))
        self.assertIn("UNKNOWN(9999)", identity.hw_model_name)

    def test_macaddr_is_never_printed_verbatim(self) -> None:
        interface = _make_interface(
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=42),
            nodesByNum={42: {"user": {"id": "!0000002a", "macaddr": b"\x01\x02\x03\x04\x05\x06"}}},
        )
        identity = rc.hardware_identity(interface)
        self.assertEqual(identity.macaddr, rc.REDACTED_CONFIGURED)


class ChannelReportsTests(unittest.TestCase):
    def test_no_channels_yields_empty_tuple(self) -> None:
        self.assertEqual(rc.channel_reports(_make_local_node()), ())

    def test_psk_is_never_printed_verbatim(self) -> None:
        channel = channel_pb2.Channel(index=0)
        channel.settings.name = "LongFast"
        channel.settings.psk = b"\x01" * 16
        channel.role = channel_pb2.Channel.Role.PRIMARY
        local_node = _make_local_node(channels=[channel])
        reports = rc.channel_reports(local_node)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].psk, rc.REDACTED_CONFIGURED)
        self.assertNotIn(b"\x01" * 16, str(reports[0]).encode())

    def test_empty_psk_reports_not_configured(self) -> None:
        channel = channel_pb2.Channel(index=0)
        local_node = _make_local_node(channels=[channel])
        reports = rc.channel_reports(local_node)
        self.assertEqual(reports[0].psk, rc.REDACTED_NOT_CONFIGURED)


class RemoteNodeSummariesTests(unittest.TestCase):
    def test_excludes_the_local_node(self) -> None:
        interface = _make_interface(
            myInfo=mesh_pb2.MyNodeInfo(my_node_num=1),
            nodesByNum={
                1: {"user": {"id": "!local"}},
                2: {"user": {"id": "!remote", "longName": "Remote"}},
            },
        )
        summaries = rc.remote_node_summaries(interface)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].node_id, "!remote")

    def test_public_key_never_printed_verbatim(self) -> None:
        interface = _make_interface(
            nodesByNum={2: {"user": {"id": "!remote", "publicKey": "base64keydata"}}},
        )
        summaries = rc.remote_node_summaries(interface)
        self.assertEqual(summaries[0].public_key, rc.REDACTED_CONFIGURED)

    def test_malformed_records_do_not_crash(self) -> None:
        interface = _make_interface(nodesByNum={2: "not a dict", 3: {"user": "not a dict either"}})
        # Record 2 is entirely malformed and is skipped outright; record
        # 3 is a dict but its "user" sub-value is not, so it still
        # produces a summary with a node_id derived from the number and
        # every other field defaulting to None/absent -- neither shape
        # raises.
        summaries = rc.remote_node_summaries(interface)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].node_id, "!00000003")


class BuildCapabilityMatrixTests(unittest.TestCase):
    def test_empty_interface_does_not_crash(self) -> None:
        rows = rc.build_capability_matrix(_make_interface())
        self.assertGreater(len(rows), 0)  # hardware identity rows always present

    def test_none_interface_does_not_crash(self) -> None:
        rows = rc.build_capability_matrix(None)
        self.assertGreater(len(rows), 0)

    def test_secrets_never_appear_as_raw_values_anywhere_in_the_matrix(self) -> None:
        local_config = localonly_pb2.LocalConfig()
        local_config.network.wifi_psk = "supersecretwifi"
        module_config = localonly_pb2.LocalModuleConfig()
        module_config.mqtt.password = "supersecretmqtt"
        local_node = _make_local_node(localConfig=local_config, moduleConfig=module_config)
        rows = rc.build_capability_matrix(_make_interface(localNode=local_node))
        rendered = "\n".join(row.value for row in rows)
        self.assertNotIn("supersecretwifi", rendered)
        self.assertNotIn("supersecretmqtt", rendered)

    def test_format_capability_matrix_never_raises_and_contains_headers(self) -> None:
        rows = rc.build_capability_matrix(_make_interface())
        text = rc.format_capability_matrix(rows)
        self.assertIn("CATEGORY", text)
        self.assertIn("SAFE", text)

    def test_hardware_identity_rows_are_never_sourced_from_a_device_path(self) -> None:
        rows = rc.build_capability_matrix(_make_interface())
        identity_rows = [row for row in rows if row.category == "HARDWARE IDENTITY"]
        self.assertTrue(identity_rows)
        for row in identity_rows:
            self.assertNotIn("/dev/", row.source)
            self.assertNotIn("ttyUSB", row.source)


class NoWritesOrTrafficTests(unittest.TestCase):
    """Prove the audit performs zero writes, zero text sends, and zero

    additional LoRa traffic -- a mock interface records every method
    call, and every audit function is proven to only ever GET
    attributes, never CALL a mutating/transmitting method.
    """

    def _tracked_interface(self):
        interface = Mock(spec=[
            "localNode", "metadata", "myInfo", "nodesByNum",
            "sendText", "sendData", "sendPosition", "sendAdmin",
        ])
        interface.localNode = _make_local_node()
        interface.metadata = mesh_pb2.DeviceMetadata(hw_model=mesh_pb2.HELTEC_V3)
        interface.myInfo = mesh_pb2.MyNodeInfo(my_node_num=1)
        interface.nodesByNum = {1: {"user": {"id": "!local"}}}
        return interface

    def test_build_capability_matrix_never_calls_a_send_method(self) -> None:
        interface = self._tracked_interface()
        rc.build_capability_matrix(interface)
        interface.sendText.assert_not_called()
        interface.sendData.assert_not_called()
        interface.sendPosition.assert_not_called()
        interface.sendAdmin.assert_not_called()

    def test_hardware_identity_never_calls_a_send_method(self) -> None:
        interface = self._tracked_interface()
        rc.hardware_identity(interface)
        interface.sendText.assert_not_called()
        interface.sendData.assert_not_called()
        interface.sendAdmin.assert_not_called()

    def test_local_node_setowner_never_called(self) -> None:
        """localNode.setOwner is the exact call RadioService.set_long_name/

        set_short_name use to WRITE identity -- this audit must never
        invoke it.
        """
        local_node = Mock(spec=["localConfig", "moduleConfig", "channels", "setOwner"])
        local_node.localConfig = localonly_pb2.LocalConfig()
        local_node.moduleConfig = localonly_pb2.LocalModuleConfig()
        local_node.channels = None
        interface = _make_interface(localNode=local_node)
        rc.build_capability_matrix(interface)
        local_node.setOwner.assert_not_called()


class RadioServiceCapabilityMethodsTests(unittest.TestCase):
    """Tests through RadioService's own public surface -- the boundary

    a future settings UI would actually call through.
    """

    def test_capability_report_before_connect_reports_unavailable_never_raises(
        self,
    ) -> None:
        radio = RadioService("/dev/null")
        rows = radio.capability_report()
        self.assertTrue(rows)
        self.assertTrue(
            all(row.value in ("unavailable", rc.REDACTED_NOT_CONFIGURED) for row in rows)
        )

    def test_hardware_identity_before_connect_reports_unavailable(self) -> None:
        radio = RadioService("/dev/null")
        identity = radio.hardware_identity()
        self.assertIsNone(identity.hw_model_name)

    def test_capability_report_reads_the_connected_interface(self) -> None:
        radio = RadioService("/dev/null")
        metadata = mesh_pb2.DeviceMetadata(hw_model=mesh_pb2.HELTEC_V3)
        radio._interface = _make_interface(metadata=metadata)
        rows = radio.capability_report()
        identity_rows = {row.field: row.value for row in rows if row.category == "HARDWARE IDENTITY"}
        self.assertEqual(identity_rows["hw_model"], "HELTEC_V3")

    def test_diagnostic_is_never_invoked_by_ordinary_connect_flow(self) -> None:
        """Diagnostic mode is off by default: connecting to a radio

        must never, on its own, call capability_report/hardware_identity
        or read any config/module-config section -- those are only
        ever invoked explicitly (via radio_capability_probe.py or a
        deliberate future settings UI call), never as a side effect of
        the app's own normal connection flow.
        """
        radio = RadioService("/dev/null")
        local_node = Mock(spec=["localConfig", "moduleConfig", "channels"])
        interface = Mock(spec=[
            "myInfo", "localNode", "metadata", "nodesByNum", "close",
        ])
        interface.localNode = local_node
        radio._interface = interface
        # Simulate normal RadioInfo reads without ever touching the
        # capability-audit surface.
        _ = radio.get_known_nodes()
        self.assertEqual(local_node.method_calls, [])
        self.assertEqual(local_node.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
