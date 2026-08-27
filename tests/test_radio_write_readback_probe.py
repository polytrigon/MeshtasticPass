"""Tests for radio_write_readback_probe.py's own logic.

These tests exercise the poison/request/poll freshness technique and
the scoped-write logic against FAKE local_node/interface objects --
never a real radio. A FakeLocalNode wraps a REAL
meshtastic.protobuf.localonly_pb2.LocalConfig() message so protobuf
CopyFrom/HasField semantics are genuine, while request/send/ack
behavior is fully scripted in-memory.
"""

from __future__ import annotations

import pytest

from meshtastic.protobuf import localonly_pb2

import radio_write_readback_probe as probe


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
        self.waitForAckNak_should_timeout = False
        self.waitForAckNak_calls = 0

    def waitForAckNak(self) -> None:
        self.waitForAckNak_calls += 1
        if self.waitForAckNak_should_timeout:
            raise RuntimeError("Timed out waiting for an acknowledgment")
        self._acknowledgment.receivedAck = True


class FakeLocalNode:
    def __init__(self, iface: FakeInterface) -> None:
        self.localConfig = localonly_pb2.LocalConfig()
        self.iface = iface
        iface.localNode = self
        self.requestConfig_calls: list = []
        self.sendAdmin_calls: list = []
        # When set, requestConfig() immediately writes this value into
        # localConfig.display.screen_on_secs, simulating a fresh
        # FromRadio config packet arriving synchronously.
        self.respond_with_on_request: int | None = None
        self.value_at_request_time: int | None = None

    def requestConfig(self, field_descriptor) -> None:
        self.requestConfig_calls.append(field_descriptor)
        self.value_at_request_time = self.localConfig.display.screen_on_secs
        if self.respond_with_on_request is not None:
            self.localConfig.display.screen_on_secs = self.respond_with_on_request

    def _sendAdmin(self, admin_message, onResponse=None, **_kwargs) -> None:
        self.sendAdmin_calls.append(admin_message)

    def onAckNak(self, p) -> None:
        pass


def make_node() -> FakeLocalNode:
    return FakeLocalNode(FakeInterface())


# --- describe_packet -------------------------------------------------


def test_describe_packet_non_dict():
    assert "non-dict packet" in probe.describe_packet(object())


def test_describe_packet_undecoded():
    result = probe.describe_packet({"fromId": "!abc"})
    assert "undecoded" in result
    assert "!abc" in result


def test_describe_packet_routing_app():
    packet = {
        "fromId": "!abc",
        "decoded": {
            "portnum": "ROUTING_APP",
            "requestId": 42,
            "routing": {"errorReason": "NONE"},
        },
    }
    result = probe.describe_packet(packet)
    assert "ROUTING_APP" in result
    assert "errorReason=NONE" in result
    assert "requestId=42" in result


def test_describe_packet_admin_app():
    packet = {
        "fromId": "!abc",
        "decoded": {"portnum": "ADMIN_APP", "admin": {"getConfigResponse": {}}},
    }
    result = probe.describe_packet(packet)
    assert "ADMIN_APP" in result
    assert "getConfigResponse" in result


def test_describe_packet_other_portnum():
    packet = {"fromId": "!abc", "decoded": {"portnum": "TEXT_MESSAGE_APP"}, "channel": 0}
    result = probe.describe_packet(packet)
    assert "TEXT_MESSAGE_APP" in result


# --- sentinel safety ---------------------------------------------------


def test_sentinel_value_fits_in_uint32():
    assert 0 <= probe.SENTINEL_VALUE < 2**32


def test_probe_only_targets_display_screen_on_secs():
    assert probe.SECTION_NAME == "display"
    assert probe.FIELD_NAME == "screen_on_secs"


# --- fresh_read ---------------------------------------------------------


def test_fresh_read_detects_fresh_value_from_radio():
    node = make_node()
    node.localConfig.display.screen_on_secs = 300
    node.respond_with_on_request = 300

    observer = probe.PacketObserver()
    value, timed_out = probe.fresh_read(node, observer, timeout=2.0)

    assert value == 300
    assert timed_out is False
    assert len(node.requestConfig_calls) == 1
    descriptor = node.requestConfig_calls[0]
    assert descriptor.containing_type.name == "LocalConfig"
    assert descriptor.name == "display"


def test_fresh_read_poisons_local_cache_before_requesting():
    node = make_node()
    node.localConfig.display.screen_on_secs = 300
    node.respond_with_on_request = 300

    observer = probe.PacketObserver()
    probe.fresh_read(node, observer, timeout=2.0)

    # The value visible to requestConfig() at call time must be the
    # sentinel, not the pre-existing 300 -- proving the local cache was
    # poisoned before the request went out, not just re-read as-is.
    assert node.value_at_request_time == probe.SENTINEL_VALUE


def test_fresh_read_times_out_when_radio_never_responds():
    node = make_node()
    node.localConfig.display.screen_on_secs = 300
    node.respond_with_on_request = None  # radio never answers

    observer = probe.PacketObserver()
    value, timed_out = probe.fresh_read(node, observer, timeout=0.5)

    assert timed_out is True
    assert value is None


# --- write_value ---------------------------------------------------------


def test_write_value_sets_only_the_display_section():
    node = make_node()

    probe.write_value(node, 240, probe.PacketObserver())

    assert node.localConfig.display.screen_on_secs == 240
    assert len(node.sendAdmin_calls) == 1
    admin_message = node.sendAdmin_calls[0]
    assert admin_message.set_config.HasField("display")
    for other_field in ("device", "position", "power", "network", "lora", "bluetooth", "security"):
        assert not admin_message.set_config.HasField(other_field), other_field


def test_write_value_reports_ack_signal():
    node = make_node()

    result = probe.write_value(node, 240, probe.PacketObserver())

    assert result["receivedAck"] is True
    assert result["timed_out"] is False
    assert node.iface.waitForAckNak_calls == 1


def test_write_value_reports_ack_timeout_without_raising():
    node = make_node()
    node.iface.waitForAckNak_should_timeout = True

    result = probe.write_value(node, 240, probe.PacketObserver())

    assert result["timed_out"] is True
    assert result["receivedAck"] is False


# --- PacketObserver -----------------------------------------------------


def test_packet_observer_describe_since_only_includes_new_packets():
    observer = probe.PacketObserver()
    observer._on_packet(packet={"fromId": "!a", "decoded": {"portnum": "TEXT_MESSAGE_APP"}})
    start_index = len(observer.packets)
    observer._on_packet(packet={"fromId": "!b", "decoded": {"portnum": "TEXT_MESSAGE_APP"}})

    lines = observer.describe_since(start_index)

    assert len(lines) == 1
    assert "!b" in lines[0]


def test_packet_observer_connection_lost_increments_counter():
    observer = probe.PacketObserver()
    observer._on_connection_lost()
    assert observer.connection_lost_events == 1
