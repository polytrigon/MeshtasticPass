"""Tests for radio_write_readback_probe.py's own logic.

These tests exercise the poison/request/wait freshness technique and
the scoped-write logic against FAKE local_node/interface objects --
never a real radio. A FakeLocalNode wraps a REAL
meshtastic.protobuf.localonly_pb2.LocalConfig() message and binds the
REAL (unmodified) meshtastic.node.Node.onResponseRequestSettings onto
itself via types.MethodType, so the actual SDK extraction/CopyFrom
logic runs -- not a hand-rolled stand-in for it -- while
request/send/ack transport is fully scripted in-memory. getConfigResponse
packets are built from REAL admin_pb2.AdminMessage protobufs run through
google.protobuf.json_format.MessageToDict, matching exactly what
mesh_interface.py's _handlePacketFromRadio hands to a response callback.
"""

from __future__ import annotations

import types

import google.protobuf.json_format as protobuf_json_format
import pytest

from meshtastic.node import Node
from meshtastic.protobuf import admin_pb2, localonly_pb2

import radio_write_readback_probe as probe


def _get_config_response_packet(value: int, section: str = "display") -> dict:
    """A realistic ADMIN_APP getConfigResponse packet, matching exactly

    what mesh_interface.py's _handlePacketFromRadio hands to a
    registered response callback: a MessageToDict'd admin payload plus
    the raw protobuf under "raw".
    """
    response = admin_pb2.AdminMessage()
    getattr(response.get_config_response, section).screen_on_secs = value
    admin_dict = protobuf_json_format.MessageToDict(response)
    admin_dict["raw"] = response
    return {
        "fromId": "!local",
        "decoded": {"portnum": "ADMIN_APP", "admin": admin_dict, "requestId": 1},
    }


def _get_config_response_packet_for_other_section() -> dict:
    """A getConfigResponse for a DIFFERENT section (lora, not display) --

    proves this probe distinguishes "a getConfigResponse arrived" from
    "the getConfigResponse we asked for arrived".
    """
    response = admin_pb2.AdminMessage()
    response.get_config_response.lora.use_preset = True
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
        self.sendAdmin_calls: list = []
        self.onResponseRequestSettings = types.MethodType(
            Node.onResponseRequestSettings, self
        )
        # When set, a get_config_request is answered with a REAL
        # getConfigResponse packet carrying this value (mirroring what
        # the physical radio actually sends). None simulates the radio
        # never responding (the timeout path).
        self.respond_with_display_value: int | None = None
        # Simulates the exact bug this module fixes: a getConfigResponse
        # packet is still delivered to the packet observer (exactly as
        # the real radio does) but the response callback is never
        # invoked -- reproducing "the probe sees the packet on
        # meshtastic.receive but nothing ever recognizes it."
        self.drop_response_callback = False
        self.observer: probe.PacketObserver | None = None
        self.value_at_request_time: int | None = None

    def onAckNak(self, p) -> None:
        pass

    def _sendAdmin(self, admin_message, onResponse=None, **_kwargs) -> None:
        self.sendAdmin_calls.append(admin_message)
        if not admin_message.HasField("get_config_request"):
            return
        self.value_at_request_time = self.localConfig.display.screen_on_secs
        if self.respond_with_display_value is None:
            return  # radio never answers -- timeout path
        packet = _get_config_response_packet(self.respond_with_display_value)
        if self.observer is not None:
            self.observer._on_packet(packet=packet)
        if onResponse is not None and not self.drop_response_callback:
            onResponse(packet)


def make_node(observer: probe.PacketObserver | None = None) -> FakeLocalNode:
    node = FakeLocalNode(FakeInterface())
    node.observer = observer
    return node


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


def test_describe_packet_admin_app_names_the_config_section():
    packet = _get_config_response_packet(240)
    result = probe.describe_packet(packet)
    assert "ADMIN_APP" in result
    assert "getConfigResponse" in result
    assert "section=display" in result
    assert "screen_on_secs=240" in result


def test_describe_packet_admin_app_other_section_omits_field_value():
    # A response for some OTHER config section must be named, but this
    # probe must never assume it is display's value.
    packet = _get_config_response_packet_for_other_section()
    result = probe.describe_packet(packet)
    assert "section=lora" in result
    assert "screen_on_secs" not in result


def test_describe_packet_admin_app_non_config_response_lists_fields_without_raw():
    packet = {
        "fromId": "!abc",
        "decoded": {"portnum": "ADMIN_APP", "admin": {"sessionPasskey": "x"}},
    }
    result = probe.describe_packet(packet)
    assert "ADMIN_APP" in result
    assert "sessionPasskey" in result
    assert "raw" not in result


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
    observer = probe.PacketObserver()
    node = make_node(observer)
    node.localConfig.display.screen_on_secs = 300
    node.respond_with_display_value = 300

    value, timed_out = probe.fresh_read(node, observer, timeout=2.0)

    assert value == 300
    assert timed_out is False
    assert len(node.sendAdmin_calls) == 1
    assert node.sendAdmin_calls[0].get_config_request == admin_pb2.AdminMessage.ConfigType.Value(
        "DISPLAY_CONFIG"
    )


def test_fresh_read_updates_localconfig_through_the_real_sdk_copyfrom():
    """onResponseRequestSettings (the REAL, unmodified SDK method) must

    still run for a local-node request -- this is the actual fix: it
    used to never be wired at all for the local node.
    """
    observer = probe.PacketObserver()
    node = make_node(observer)
    node.respond_with_display_value = 240

    probe.fresh_read(node, observer, timeout=2.0)

    assert node.localConfig.display.screen_on_secs == 240


def test_fresh_read_poisons_local_cache_before_requesting():
    observer = probe.PacketObserver()
    node = make_node(observer)
    node.localConfig.display.screen_on_secs = 300
    node.respond_with_display_value = 300

    probe.fresh_read(node, observer, timeout=2.0)

    # The value visible to _sendAdmin() at call time must be the
    # sentinel, not the pre-existing 300 -- proving the local cache was
    # poisoned before the request went out, not just re-read as-is.
    assert node.value_at_request_time == probe.SENTINEL_VALUE


def test_fresh_read_times_out_when_radio_never_responds():
    observer = probe.PacketObserver()
    node = make_node(observer)
    node.localConfig.display.screen_on_secs = 300
    node.respond_with_display_value = None  # radio never answers

    value, timed_out = probe.fresh_read(node, observer, timeout=0.5)

    assert timed_out is True
    assert value is None


def test_fresh_read_ignores_a_response_for_a_different_config_section():
    """A getConfigResponse for some OTHER section (e.g. lora, perhaps

    left over from unrelated admin traffic) must never be mistaken for
    the display response this probe is actually waiting for.
    """
    observer = probe.PacketObserver()
    node = make_node(observer)

    def _sendAdmin_with_wrong_section_response(admin_message, onResponse=None, **_kwargs):
        node.sendAdmin_calls.append(admin_message)
        if onResponse is not None:
            onResponse(_get_config_response_packet_for_other_section())

    node._sendAdmin = _sendAdmin_with_wrong_section_response

    value, timed_out = probe.fresh_read(node, observer, timeout=0.5)

    assert timed_out is True
    assert value is None


def test_fresh_read_reproduces_and_fixes_the_dropped_response_callback_bug():
    """Regression test for the real-hardware failure: the radio's

    getConfigResponse packet is visibly delivered to the packet
    observer (exactly as MESHTASTICPASS_RX_DEBUG showed on real
    hardware -- "packet during readback wait: ... ADMIN_APP
    getConfigResponse ...") but the OLD code path
    (localNode.requestConfig(), which hard-codes onResponse=None for
    the local node) never had anything wired to consume it, so
    localConfig was never updated and fresh_read timed out despite the
    packet's arrival.

    drop_response_callback=True reproduces exactly that: the packet
    still reaches the observer, but the response callback is never
    invoked -- proving packet arrival alone must not (and does not)
    satisfy this probe's verification.
    """
    observer = probe.PacketObserver()
    node = make_node(observer)
    node.respond_with_display_value = 240
    node.drop_response_callback = True

    before_count = len(observer.packets)
    value, timed_out = probe.fresh_read(node, observer, timeout=0.5)

    # The packet WAS delivered to the observer...
    assert len(observer.packets) > before_count
    delivered = observer.describe_since(before_count)
    assert any("getConfigResponse" in line and "section=display" in line for line in delivered)
    # ...but with no response callback ever invoked, this must still be
    # reported as an honest failure -- never fabricated success from
    # packet arrival alone, and localConfig must still show the
    # sentinel, never the stale pre-existing value.
    assert timed_out is True
    assert value is None
    assert node.localConfig.display.screen_on_secs == probe.SENTINEL_VALUE


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
