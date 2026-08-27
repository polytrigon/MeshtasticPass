"""TEMPORARY, standalone diagnostic: config write/readback confirmation probe.

Determines the STRONGEST confirmation level MeshtasticPass can obtain
for a configuration write against a real connected Meshtastic radio,
by actually writing and restoring exactly ONE field:
localConfig.display.screen_on_secs (chosen because the capability
audit classified it SAFE/HIGH-VALUE, writable, and not expected to
require a reboot -- see radio_capabilities.py).

This script NEVER touches any other configuration field: not LoRa
region/modem preset/frequency/TX power, not channels/PSKs, not
security keys, not WiFi/Bluetooth, not role, not GPS, not power-
saving/deep-sleep, not names. It is not wired into app.py or the
normal connection path -- this is diagnostic-only, run by hand.

THE CORE QUESTION THIS PROBE ANSWERS: does reading
`localNode.localConfig.display.screen_on_secs` after a write actually
prove the RADIO applied the change, or does it only prove the LOCAL
Python object was mutated? To answer this honestly, every "read" in
this script poisons the local field with an unmistakable sentinel
value first, then sends its own `AdminMessage.get_config_request` for
DISPLAY_CONFIG with an EXPLICIT response callback wired -- bypassing
`localNode.requestConfig()`'s convenience wrapper, which passes
`onResponse=None` for the LOCAL node specifically. That default is not
a caching quirk: it means NOTHING in the SDK ever parses the radio's
`getConfigResponse` reply or copies it into `localConfig` for a local
node's own config requests, even though the reply packet is fully
visible on the generic "meshtastic.receive" topic. A fresh value is
only accepted once a `getConfigResponse` packet's own raw protobuf
(`AdminMessage.get_config_response.WhichOneof("payload_variant")`)
names the "display" section -- never from packet arrival alone
(other sections/ACKs arrive on the same topic) and never from a bare
poll of the local cache (which this callback also updates via the
SDK's own `Node.onResponseRequestSettings`, cross-checked but not
trusted as the primary signal).

Every packet the SDK decodes during the experiment (routing ACK/NAK,
admin responses, anything else) is also logged via the same generic
"meshtastic.receive" pubsub topic RadioService's own RX_DEBUG
diagnostic already uses -- see radio_service.rx_debug_enabled -- so
this can report every confirmation signal actually available, not
just the ones a specific Python callback happens to be wired for.

Usage:

    python radio_write_readback_probe.py --device /dev/ttyUSB0

Exit codes: 0 = write verified via fresh readback, 1 = aborted before
any write (e.g. hardware/config unavailable), 2 = write attempted but
NOT verified.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from radio_service import RadioConnectionError, RadioService


SECTION_NAME = "display"
FIELD_NAME = "screen_on_secs"
TEMPORARY_VALUE = 240
# A value screen_on_secs (a firmware uint32 seconds count) can never
# plausibly hold in real use -- poisoning the LOCAL cache with this
# before every read makes a later observed change unambiguous proof
# that a genuinely fresh CopyFrom from the radio happened, never
# leftover local state.
SENTINEL_VALUE = 3_141_592_653
READBACK_TIMEOUT_SECONDS = 15.0


def log(message: str) -> None:
    print(f"[PROBE] {message}", flush=True)


class PacketObserver:
    """Subscribes to the SAME generic pubsub topics RadioService's own

    RX_DEBUG diagnostic uses, recording every packet/connection event
    the SDK decodes during the experiment window -- this is how
    "every confirmation signal the Python Meshtastic SDK exposes" gets
    captured, independent of whether a specific Python callback was
    wired for it.
    """

    def __init__(self) -> None:
        self.packets: list[Any] = []
        self.connection_lost_events = 0
        self._pub: Any = None

    def start(self) -> None:
        from pubsub import pub

        pub.subscribe(self._on_packet, "meshtastic.receive")
        pub.subscribe(self._on_connection_lost, "meshtastic.connection.lost")
        self._pub = pub

    def stop(self) -> None:
        if self._pub is None:
            return
        for callback, topic in (
            (self._on_packet, "meshtastic.receive"),
            (self._on_connection_lost, "meshtastic.connection.lost"),
        ):
            try:
                self._pub.unsubscribe(callback, topic)
            except Exception:
                pass
        self._pub = None

    def _on_packet(self, packet: Any = None, interface: Any = None, **_kwargs: Any) -> None:
        self.packets.append(packet)

    def _on_connection_lost(self, interface: Any = None, **_kwargs: Any) -> None:
        self.connection_lost_events += 1
        log("EVENT: meshtastic.connection.lost fired")

    def describe_since(self, start_index: int) -> list[str]:
        return [describe_packet(packet) for packet in self.packets[start_index:]]


def _get_config_response_section(admin: Any) -> str | None:
    """Which LocalConfig section (e.g. "display") a getConfigResponse

    names, read directly from the packet's own raw AdminMessage
    protobuf via the Config message's `payload_variant` oneof -- never
    from the decoded dict's key order, and never from any side effect
    on local_node's cache. Returns None for anything that is not a
    getConfigResponse (routing ACK/NAK, other admin replies, etc.).
    """
    if not isinstance(admin, dict):
        return None
    raw = admin.get("raw")
    if raw is None or not raw.HasField("get_config_response"):
        return None
    return raw.get_config_response.WhichOneof("payload_variant")


def _get_config_response_field(admin: Any, section: str, field: str) -> Any:
    """The value of `section.field` inside a getConfigResponse's raw

    protobuf payload -- e.g. section="display", field="screen_on_secs".
    Returns None if `admin` does not carry a getConfigResponse for
    that section.
    """
    if _get_config_response_section(admin) != section:
        return None
    section_message = getattr(admin["raw"].get_config_response, section)
    return getattr(section_message, field)


def describe_packet(packet: Any) -> str:
    """Human-readable one-liner for one decoded SDK packet -- routing

    ACK/NAK and admin-response shapes are called out explicitly since
    those are exactly the confirmation signals this probe cares about;
    anything else still gets an honest, non-crashing description.
    """
    if not isinstance(packet, dict):
        return f"<non-dict packet: {packet!r}>"
    decoded = packet.get("decoded")
    from_id = packet.get("fromId", "?")
    if not isinstance(decoded, dict):
        return f"from={from_id} <undecoded/encrypted>"
    portnum = decoded.get("portnum")
    if portnum == "ROUTING_APP":
        routing = decoded.get("routing")
        reason = routing.get("errorReason", "NONE") if isinstance(routing, dict) else "NONE"
        return f"from={from_id} ROUTING_APP errorReason={reason} requestId={decoded.get('requestId')}"
    if portnum == "ADMIN_APP":
        admin = decoded.get("admin")
        section = _get_config_response_section(admin)
        if section is not None:
            detail = f"getConfigResponse section={section}"
            if section == SECTION_NAME:
                value = _get_config_response_field(admin, SECTION_NAME, FIELD_NAME)
                detail += f" {FIELD_NAME}={value}"
            return f"from={from_id} ADMIN_APP {detail}"
        keys = [key for key in admin.keys() if key != "raw"] if isinstance(admin, dict) else []
        return f"from={from_id} ADMIN_APP fields={keys}"
    return f"from={from_id} portnum={portnum} channel={packet.get('channel', 0)}"


def fresh_read(
    local_node: Any,
    observer: PacketObserver,
    *,
    timeout: float = READBACK_TIMEOUT_SECONDS,
) -> tuple[int | None, bool]:
    """Poison the local cache, request a fresh config read from the

    radio, and wait for a getConfigResponse whose OWN raw protobuf
    content names the "display" section -- never for packet arrival
    alone (other admin traffic uses the same topic) and never by
    polling the local cache for a bare change.

    Bypasses localNode.requestConfig()'s convenience wrapper on
    purpose: for the LOCAL node it hard-codes onResponse=None, so nothing
    would ever parse the reply. This calls _sendAdmin directly with an
    explicit callback that (a) invokes the SDK's own
    Node.onResponseRequestSettings, so localConfig genuinely gets the
    same CopyFrom a remote-node request would receive, and (b)
    independently extracts the section/value straight from the
    packet's raw protobuf, which is the actual pass/fail signal.

    Returns (value_or_None, timed_out). `value_or_None` is None only
    when timed_out is True.
    """
    from meshtastic.protobuf import admin_pb2

    section = getattr(local_node.localConfig, SECTION_NAME)
    setattr(section, FIELD_NAME, SENTINEL_VALUE)

    admin_request = admin_pb2.AdminMessage()
    admin_request.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(
        f"{SECTION_NAME.upper()}_CONFIG"
    )

    found: dict[str, int] = {}

    def on_response(packet: Any) -> None:
        # Reuse the SDK's own extraction/CopyFrom logic (normally only
        # wired for remote-node requests) so localConfig is updated
        # through the real mechanism, not a hand-rolled duplicate of it.
        local_node.onResponseRequestSettings(packet)
        decoded = packet.get("decoded") if isinstance(packet, dict) else None
        admin = decoded.get("admin") if isinstance(decoded, dict) else None
        if _get_config_response_section(admin) == SECTION_NAME:
            found["value"] = _get_config_response_field(admin, SECTION_NAME, FIELD_NAME)

    before_count = len(observer.packets)
    local_node._sendAdmin(admin_request, onResponse=on_response)

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if "value" in found:
            value = found["value"]
            elapsed = time.monotonic() - start
            log(
                f"received radio-originated getConfigResponse for "
                f"section={SECTION_NAME} after {elapsed:.2f}s: {FIELD_NAME}={value}"
            )
            cached = getattr(getattr(local_node.localConfig, SECTION_NAME), FIELD_NAME)
            if cached != value:
                log(
                    f"WARNING: localConfig cache ({cached}) does not match the "
                    f"packet-extracted value ({value}) -- treating the packet, "
                    f"not the cache, as authoritative"
                )
            for line in observer.describe_since(before_count):
                log(f"  packet during readback wait: {line}")
            return value, False
        time.sleep(0.05)

    log(
        f"TIMED OUT waiting {timeout:g}s for a getConfigResponse naming "
        f"section={SECTION_NAME}"
    )
    for line in observer.describe_since(before_count):
        log(f"  packet during readback wait: {line}")
    return None, True


def write_value(local_node: Any, value: int, observer: PacketObserver) -> dict[str, Any]:
    """Write ONLY display.screen_on_secs via the SDK's own admin-write

    mechanism (the same AdminMessage.set_config path
    RadioService/node.writeConfig ultimately uses), but with an
    EXPLICIT ack callback wired -- node.writeConfig's own convenience
    wrapper passes onResponse=None for the local node specifically
    (see the completion report), so calling it as-is would silently
    discard the only confirmation signal the SDK offers for this call.
    """
    from meshtastic.protobuf import admin_pb2

    section = getattr(local_node.localConfig, SECTION_NAME)
    setattr(section, FIELD_NAME, value)

    interface = local_node.iface
    interface._acknowledgment.reset()
    admin_message = admin_pb2.AdminMessage()
    getattr(admin_message.set_config, SECTION_NAME).CopyFrom(section)

    before_count = len(observer.packets)
    local_node._sendAdmin(admin_message, onResponse=local_node.onAckNak)

    result: dict[str, Any] = {
        "receivedAck": False,
        "receivedNak": False,
        "receivedImplAck": False,
        "timed_out": False,
    }
    try:
        interface.waitForAckNak()
        result["receivedAck"] = interface._acknowledgment.receivedAck
        result["receivedNak"] = interface._acknowledgment.receivedNak
        result["receivedImplAck"] = interface._acknowledgment.receivedImplAck
    except Exception:
        result["timed_out"] = True

    for line in observer.describe_since(before_count):
        log(f"  packet during write: {line}")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "TEMPORARY diagnostic: probe write/readback confirmation for "
            "localConfig.display.screen_on_secs. Writes and restores this "
            "ONE field only."
        )
    )
    parser.add_argument(
        "--device",
        default="/dev/ttyUSB0",
        help="serial device path (default: /dev/ttyUSB0)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    radio = RadioService(args.device)
    observer = PacketObserver()
    observer.start()

    original_value: int | None = None

    try:
        try:
            info = radio.connect()
        except RadioConnectionError as error:
            log(f"Could not connect to {args.device}: {error}")
            return 1

        identity = radio.hardware_identity()
        log(
            f"Connected: node={info.node_id}  "
            f"hw_model={identity.hw_model_name!r} (source={identity.hw_model_source})"
        )
        if identity.hw_model_name is None:
            log("REFUSING to continue: the radio did not identify its own hardware.")
            return 1

        interface = radio._interface
        local_node = getattr(interface, "localNode", None)
        if local_node is None or getattr(local_node, "localConfig", None) is None:
            log("REFUSING to continue: localConfig is not available.")
            return 1

        log(f"--- Step 1: baseline fresh read of {SECTION_NAME}.{FIELD_NAME} ---")
        baseline, timed_out = fresh_read(local_node, observer)
        if timed_out:
            log("Could not obtain a fresh baseline read from the radio -- aborting before any write.")
            return 1
        original_value = baseline
        log(f"CURRENT {FIELD_NAME}: {original_value}")

        temporary_value = TEMPORARY_VALUE
        if original_value == temporary_value:
            temporary_value += 1
            log(
                f"Current value already equals the default probe value; "
                f"using {temporary_value} instead."
            )

        log(f"--- Step 2: write {SECTION_NAME}.{FIELD_NAME} = {temporary_value} ---")
        ack = write_value(local_node, temporary_value, observer)
        log(f"ack signals from write: {ack}")

        log("--- Step 3: fresh readback after write ---")
        read_back, timed_out = fresh_read(local_node, observer)
        log(f"REQUESTED: {temporary_value}")
        log(f"READ BACK: {'TIMED OUT (no fresh packet observed)' if timed_out else read_back}")
        verified = (not timed_out) and read_back == temporary_value
        log(f"VERIFIED: {'YES' if verified else 'NO'}")

        return 0 if verified else 2
    finally:
        if original_value is not None:
            try:
                interface = radio._interface
                local_node = getattr(interface, "localNode", None) if interface is not None else None
                if local_node is not None and getattr(local_node, "localConfig", None) is not None:
                    log(f"--- Cleanup: restoring {SECTION_NAME}.{FIELD_NAME} = {original_value} ---")
                    write_value(local_node, original_value, observer)
                    restored, timed_out = fresh_read(local_node, observer)
                    if timed_out or restored != original_value:
                        log(
                            f"WARNING: restoration could not be verified via fresh "
                            f"readback (got {restored!r}) -- MANUALLY CHECK the radio."
                        )
                    else:
                        log(f"Restoration verified: {FIELD_NAME} = {restored}")
            except Exception as error:
                log(
                    f"WARNING: restoration attempt raised {error!r} -- "
                    f"MANUALLY VERIFY {SECTION_NAME}.{FIELD_NAME} on the radio."
                )
        observer.stop()
        radio.close()


if __name__ == "__main__":
    raise SystemExit(main())
