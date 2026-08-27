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
value first, explicitly asks the radio to resend that config section
(`localNode.requestConfig(...)`), and then polls until the SDK's own
generic FromRadio-handling code (see mesh_interface.py's
_handleFromRadio, which CopyFrom()s a fresh config packet into this
exact same object whenever one arrives) overwrites the sentinel -- or
times out. Only a value that is NOT the sentinel we just poisoned it
with counts as evidence of a genuinely fresh radio-side response.

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
        keys = list(admin.keys()) if isinstance(admin, dict) else []
        return f"from={from_id} ADMIN_APP fields={keys}"
    return f"from={from_id} portnum={portnum} channel={packet.get('channel', 0)}"


def fresh_read(
    local_node: Any,
    observer: PacketObserver,
    *,
    timeout: float = READBACK_TIMEOUT_SECONDS,
) -> tuple[int | None, bool]:
    """Poison the local cache, request a fresh config read from the

    radio, and poll until the poisoned value is overwritten by
    genuinely new data or `timeout` elapses.

    Returns (value_or_None, timed_out). `value_or_None` is None only
    when timed_out is True -- there is otherwise always a concrete
    (possibly still-sentinel-adjacent, but in practice always
    radio-sourced) integer to report.
    """
    section = getattr(local_node.localConfig, SECTION_NAME)
    setattr(section, FIELD_NAME, SENTINEL_VALUE)
    field_descriptor = local_node.localConfig.DESCRIPTOR.fields_by_name[SECTION_NAME]

    before_count = len(observer.packets)
    local_node.requestConfig(field_descriptor)

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        current = getattr(getattr(local_node.localConfig, SECTION_NAME), FIELD_NAME)
        if current != SENTINEL_VALUE:
            elapsed = time.monotonic() - start
            log(f"fresh radio-side config packet observed after {elapsed:.2f}s")
            for line in observer.describe_since(before_count):
                log(f"  packet during readback wait: {line}")
            return current, False
        time.sleep(0.2)

    log(f"TIMED OUT waiting {timeout:g}s for a fresh config packet")
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
