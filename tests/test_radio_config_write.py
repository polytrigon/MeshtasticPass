"""Tests for RadioService's generic verified config-write pipeline.

These exercise RadioService.write_verified_config_field/
read_synced_config_field against a FAKE Meshtastic interface -- never a
real radio. The fake wraps a REAL meshtastic.protobuf.localonly_pb2.
LocalConfig() message and binds the REAL (unmodified)
meshtastic.node.Node.onResponseRequestSettings onto itself via
types.MethodType, exactly like tests/test_radio_write_readback_probe.py,
since this production method reuses that exact real-hardware-proven
technique.
"""

from __future__ import annotations

import types
import unittest

import google.protobuf.json_format as protobuf_json_format

from meshtastic.node import Node
from meshtastic.protobuf import admin_pb2, localonly_pb2

from radio_service import ConfigWriteResult, RadioService


def _config_response_packet(section: str, field: str, value) -> dict:
    response = admin_pb2.AdminMessage()
    setattr(getattr(response.get_config_response, section), field, value)
    admin_dict = protobuf_json_format.MessageToDict(response)
    admin_dict["raw"] = response
    return {
        "fromId": "!local",
        "decoded": {"portnum": "ADMIN_APP", "admin": admin_dict, "requestId": 1},
    }


class FakeAcknowledgment:
    def __init__(self) -> None:
        self.receivedAck = False
        self.receivedNak = False
        self.receivedImplAck = False

    def reset(self) -> None:
        self.receivedAck = False
        self.receivedNak = False
        self.receivedImplAck = False


class FakeInterface:
    def __init__(self) -> None:
        self._acknowledgment = FakeAcknowledgment()
        self.localNode = None

    def waitForAckNak(self) -> None:
        pass


class FakeLocalNode:
    def __init__(self, iface: FakeInterface) -> None:
        self.localConfig = localonly_pb2.LocalConfig()
        self.iface = iface
        iface.localNode = self
        self.onResponseRequestSettings = types.MethodType(
            Node.onResponseRequestSettings, self
        )
        self.sendAdmin_calls: list = []
        # Controls for the get_config_request response:
        self.respond_with_value: int | None = None
        self.respond_section = "display"
        self.respond_field = "screen_on_secs"
        self.drop_response_callback = False
        self.simulate_nak = False
        # When set, simulates a reconnect completing (the RadioService's
        # _interface swapping to a brand new object) right when the read
        # request is sent, before any response is processed.
        self.swap_interface_on: RadioService | None = None

    def onAckNak(self, packet) -> None:
        if self.simulate_nak:
            self.iface._acknowledgment.receivedNak = True
        else:
            self.iface._acknowledgment.receivedAck = True

    def _sendAdmin(self, admin_message, onResponse=None, **_kwargs) -> None:
        self.sendAdmin_calls.append(admin_message)
        if admin_message.HasField("set_config"):
            if onResponse is not None:
                onResponse(
                    {
                        "decoded": {
                            "portnum": "ROUTING_APP",
                            "routing": {
                                "errorReason": "MAX_RETRANSMIT"
                                if self.simulate_nak
                                else "NONE"
                            },
                            "requestId": 1,
                        }
                    }
                )
            return
        if admin_message.HasField("get_config_request"):
            if self.swap_interface_on is not None:
                self.swap_interface_on._interface = FakeInterface()
                return
            if self.drop_response_callback or self.respond_with_value is None:
                return  # the radio never answers -- timeout path
            packet = _config_response_packet(
                self.respond_section, self.respond_field, self.respond_with_value
            )
            if onResponse is not None:
                onResponse(packet)


def make_service() -> tuple[RadioService, FakeLocalNode]:
    service = RadioService("/dev/test-radio")
    interface = FakeInterface()
    node = FakeLocalNode(interface)
    service._interface = interface
    return service, node


class WriteVerifiedConfigFieldTests(unittest.TestCase):
    def test_c_matching_readback_is_applied(self) -> None:
        service, node = make_service()
        node.respond_with_value = 240

        result = service.write_verified_config_field("display", "screen_on_secs", 240)

        self.assertEqual(
            result, ConfigWriteResult(True, 240, 240, "")
        )

    def test_d_mismatching_readback_is_not_applied(self) -> None:
        service, node = make_service()
        node.respond_with_value = 999  # radio reports something else

        result = service.write_verified_config_field("display", "screen_on_secs", 240)

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "mismatch")
        self.assertEqual(result.readback_value, 999)

    def test_confirmed_write_rebuilds_the_config_snapshot(self) -> None:
        """Item 6: applied=True is the ONLY thing that ever updates the

        cached RadioConfigurationSnapshot -- never optimistically,
        before this exact verification level is reached (see
        test_mismatching_readback_does_not_rebuild_the_snapshot below).
        """
        service, node = make_service()
        service._rebuild_config_snapshot()
        before = service.config_snapshot()
        self.assertIsNotNone(before)
        node.respond_with_value = 240

        result = service.write_verified_config_field("display", "screen_on_secs", 240)

        self.assertTrue(result.applied)
        after = service.config_snapshot()
        self.assertIsNot(after, before)
        display = next(s for s in after.local_config if s.section == "display")
        self.assertEqual(display.fields["screen_on_secs"], "240")

    def test_mismatching_readback_does_not_rebuild_the_snapshot(self) -> None:
        service, node = make_service()
        service._rebuild_config_snapshot()
        before = service.config_snapshot()
        node.respond_with_value = 999

        result = service.write_verified_config_field("display", "screen_on_secs", 240)

        self.assertFalse(result.applied)
        self.assertIs(service.config_snapshot(), before)

    def test_e_no_response_times_out(self) -> None:
        service, node = make_service()
        node.respond_with_value = None

        result = service.write_verified_config_field(
            "display", "screen_on_secs", 240, timeout=0.3
        )

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "timeout")
        self.assertIsNone(result.readback_value)

    def test_a_local_cache_mutation_alone_is_not_verification(self) -> None:
        """The SDK's own write path mutates localConfig BEFORE any radio

        confirmation exists (see write_verified_config_field's own
        docstring) -- so even though the cache already shows 240 the
        instant this call begins, a write that never gets a genuine
        fresh readback must still report failure.
        """
        service, node = make_service()
        node.drop_response_callback = True  # radio never answers the read

        result = service.write_verified_config_field(
            "display", "screen_on_secs", 240, timeout=0.3
        )

        self.assertEqual(node.localConfig.display.screen_on_secs, 240)  # cache WAS mutated
        self.assertFalse(result.applied)  # but that alone must not count

    def test_b_routing_ack_alone_is_not_verification(self) -> None:
        """A routing ACK for the WRITE (not a NAK) must not, by itself,

        report success -- only a matching fresh getConfigResponse can.
        """
        service, node = make_service()
        node.simulate_nak = False  # the write gets a clean ACK
        node.drop_response_callback = True  # but the read never answers

        result = service.write_verified_config_field(
            "display", "screen_on_secs", 240, timeout=0.3
        )

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "timeout")

    def test_nak_on_write_fails_fast_without_waiting_for_readback(self) -> None:
        service, node = make_service()
        node.simulate_nak = True

        result = service.write_verified_config_field("display", "screen_on_secs", 240)

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "nak")

    def test_f_connection_lost_during_verification_fails(self) -> None:
        service, node = make_service()
        node.drop_response_callback = True

        def _sendAdmin_then_disconnect(admin_message, onResponse=None, **kwargs):
            node.sendAdmin_calls.append(admin_message)
            if admin_message.HasField("get_config_request"):
                service._connection_lost.set()

        node._sendAdmin = _sendAdmin_then_disconnect

        result = service.write_verified_config_field(
            "display", "screen_on_secs", 240, timeout=1.0
        )

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "disconnected")

    def test_g_stale_response_from_old_interface_cannot_verify_current_write(self) -> None:
        """If a reconnect completes (self._interface becomes a NEW

        object) while this call is still waiting, any response tied to
        the OLD interface/local_node must not be trusted to verify a
        write against the CURRENT interface.
        """
        service, node = make_service()
        node.swap_interface_on = service

        result = service.write_verified_config_field(
            "display", "screen_on_secs", 240, timeout=1.0
        )

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "disconnected")

    def test_h_response_for_a_different_section_does_not_verify(self) -> None:
        service, node = make_service()
        node.respond_section = "lora"
        node.respond_field = "use_preset"
        node.respond_with_value = True

        result = service.write_verified_config_field(
            "display", "screen_on_secs", 240, timeout=0.3
        )

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "timeout")

    def test_i_no_unrelated_fields_are_modified(self) -> None:
        service, node = make_service()
        node.respond_with_value = 240

        service.write_verified_config_field("display", "screen_on_secs", 240)

        display = node.localConfig.display
        self.assertEqual(display.screen_on_secs, 240)
        self.assertFalse(display.flip_screen)
        self.assertFalse(display.compass_north_top)
        self.assertEqual(display.units, 0)
        self.assertFalse(display.use_12h_clock)
        # Only the "display" section was ever put on the wire -- no
        # other top-level config section.
        write_message = node.sendAdmin_calls[0]
        self.assertTrue(write_message.set_config.HasField("display"))
        for section_name in ("device", "position", "power", "network", "lora", "bluetooth", "security"):
            self.assertFalse(write_message.set_config.HasField(section_name), section_name)

    def test_not_connected_fails_without_touching_anything(self) -> None:
        service = RadioService("/dev/test-radio")

        result = service.write_verified_config_field("display", "screen_on_secs", 240)

        self.assertEqual(result, ConfigWriteResult(False, 240, None, "not_connected"))


class ReadSyncedConfigFieldTests(unittest.TestCase):
    def test_reads_directly_from_the_synced_cache_no_new_request(self) -> None:
        service, node = make_service()
        node.localConfig.display.screen_on_secs = 300

        value = service.read_synced_config_field("display", "screen_on_secs")

        self.assertEqual(value, 300)
        self.assertEqual(node.sendAdmin_calls, [])  # no admin traffic generated

    def test_returns_none_when_not_connected(self) -> None:
        service = RadioService("/dev/test-radio")

        self.assertIsNone(service.read_synced_config_field("display", "screen_on_secs"))

    def test_returns_none_for_an_unsupported_field_without_crashing(self) -> None:
        service, _node = make_service()

        self.assertIsNone(service.read_synced_config_field("display", "not_a_real_field"))
        self.assertIsNone(service.read_synced_config_field("not_a_real_section", "x"))


class _ChannelFakeNode(FakeLocalNode):
    """FakeLocalNode that also answers get_channel_request, wired to a

    REAL meshtastic Channel message so the 1-indexing / index==0 logic
    is exercised against the real proto.
    """

    def __init__(self, iface: FakeInterface) -> None:
        super().__init__(iface)
        from meshtastic.protobuf import channel_pb2

        primary = channel_pb2.Channel()
        primary.index = 0
        primary.role = channel_pb2.Channel.Role.PRIMARY
        self.channels = [primary]
        self.get_channel_requests: list[int] = []
        self.answer_channel = True

    def _sendAdmin(self, admin_message, onResponse=None, **_kwargs) -> None:
        if admin_message.HasField("set_channel"):
            self.sendAdmin_calls.append(admin_message)
            self.channels[0].CopyFrom(admin_message.set_channel)
            if onResponse is not None:
                onResponse(
                    {
                        "decoded": {
                            "portnum": "ROUTING_APP",
                            "routing": {"errorReason": "NONE"},
                            "requestId": 1,
                        }
                    }
                )
            return
        if admin_message.HasField("get_channel_request"):
            self.get_channel_requests.append(admin_message.get_channel_request)
            self.sendAdmin_calls.append(admin_message)
            if not self.answer_channel:
                return
            # Real firmware: get_channel_request N addresses channel N-1.
            index = admin_message.get_channel_request - 1
            if index != 0:
                return  # invalid / not the primary channel -> no answer
            from meshtastic.protobuf import admin_pb2
            import google.protobuf.json_format as jf

            response = admin_pb2.AdminMessage()
            response.get_channel_response.CopyFrom(self.channels[0])
            admin_dict = jf.MessageToDict(response)
            admin_dict["raw"] = response
            if onResponse is not None:
                onResponse({"decoded": {"portnum": "ADMIN_APP", "admin": admin_dict}})
            return
        super()._sendAdmin(admin_message, onResponse=onResponse, **_kwargs)


class WriteVerifiedPrimaryChannelTests(unittest.TestCase):
    def _service(self) -> tuple[RadioService, _ChannelFakeNode]:
        service = RadioService("/dev/test-radio")
        interface = FakeInterface()
        node = _ChannelFakeNode(interface)
        service._interface = interface
        return service, node

    def test_readback_uses_one_indexed_get_channel_request(self) -> None:
        service, node = self._service()

        result = service.write_verified_primary_channel(name="", psk=bytes([1]))

        self.assertTrue(result.applied)
        # 1-indexed: primary channel (index 0) is requested as 1, never 0.
        self.assertIn(1, node.get_channel_requests)
        self.assertNotIn(0, node.get_channel_requests)

    def test_default_psk_sentinel_matches_expanded_default_key_on_readback(self) -> None:
        from meshtastic.util import DEFAULT_KEY

        service, node = self._service()
        # Firmware echoes the EXPANDED default key rather than the 0x01 sentinel.
        real_send = node._sendAdmin

        def echo_expanded(admin_message, onResponse=None, **kw):
            if admin_message.HasField("set_channel"):
                node.channels[0].CopyFrom(admin_message.set_channel)
                node.channels[0].settings.psk = DEFAULT_KEY
                if onResponse:
                    onResponse({"decoded": {"routing": {"errorReason": "NONE"}}})
                return
            return real_send(admin_message, onResponse=onResponse, **kw)

        node._sendAdmin = echo_expanded
        result = service.write_verified_primary_channel(name="", psk=bytes([1]))
        self.assertTrue(result.applied)  # semantic PSK match, not literal

    def test_unanswered_channel_readback_times_out_cleanly(self) -> None:
        service, node = self._service()
        node.answer_channel = False

        result = service.write_verified_primary_channel(
            name="", psk=bytes([1]), timeout=0.2
        )

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "timeout")


class SettingsTransactionTests(unittest.TestCase):
    def test_begin_commit_delegate_to_the_local_node_when_available(self) -> None:
        service, node = make_service()
        node.beginSettingsTransaction = lambda: node.sendAdmin_calls.append("begin")
        node.commitSettingsTransaction = lambda: node.sendAdmin_calls.append("commit")

        self.assertTrue(service.begin_settings_transaction())
        self.assertTrue(service.commit_settings_transaction())
        self.assertEqual(node.sendAdmin_calls, ["begin", "commit"])

    def test_missing_sdk_methods_are_a_safe_no_op(self) -> None:
        service, _node = make_service()
        # FakeLocalNode has no begin/commitSettingsTransaction.
        self.assertFalse(service.begin_settings_transaction())
        self.assertFalse(service.commit_settings_transaction())

    def test_not_connected_is_a_safe_no_op(self) -> None:
        service = RadioService("/dev/test-radio")
        self.assertFalse(service.begin_settings_transaction())
        self.assertFalse(service.commit_settings_transaction())


if __name__ == "__main__":
    unittest.main()
