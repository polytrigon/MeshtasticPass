"""TEMPORARY, standalone diagnostic: observe ALL outbound (host -> radio)

serial traffic while MeshtasticPass is connected but otherwise completely
idle -- no text sends, no config writes, no user interaction.

This exists to answer one question empirically, on real hardware: does
this application's OWN code generate any periodic traffic while idle, or
does the traffic (if any) come entirely from the Meshtastic Python SDK's
own internal heartbeat? (See mesh_interface.py's _startHeartbeat(),
which -- once the connection completes its initial sync -- automatically
sends a ToRadio.heartbeat every 300 seconds for as long as the interface
object exists, with no RadioService/app.py code ever calling it.)

Introduces NO new outbound traffic of its own: it only wraps the SDK's
existing interface._sendToRadioImpl (the single funnel every outbound
ToRadio message -- heartbeat, admin, mesh packets -- already passes
through; see mesh_interface.py's _sendToRadio) to log what was ALREADY
about to be sent, then calls straight through to the real
implementation unchanged.

While this runs, watch the physical radio's display. Per the firmware's
PowerFSM (src/PowerFSM.cpp on github.com/meshtastic/firmware): the mere
presence of an open serial API connection transitions the power state
machine into a dedicated SERIAL state that unconditionally suppresses
sleep, for as long as the connection doesn't go SERIAL_CONNECTION_TIMEOUT
(15 minutes, see src/SerialConsole.cpp) without any inbound ToRadio
traffic. This script's own printout shows exactly what traffic (if any)
keeps that 15-minute window from ever elapsing.

Usage:

    python serial_idle_traffic_probe.py --device /dev/ttyUSB0

Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from radio_service import RadioConnectionError, RadioService


def log(message: str) -> None:
    print(f"[IDLE-TRAFFIC] {message}", flush=True)


def describe_to_radio(to_radio: Any) -> str:
    """Which ToRadio oneof field this outbound message carries -- e.g.

    "heartbeat", "packet", "want_config_id" -- or "unknown" for a shape
    this diagnostic doesn't recognize. Never raises on an unexpected
    input; an honest "unknown" beats a crashed diagnostic.
    """
    which_oneof = getattr(to_radio, "WhichOneof", None)
    if which_oneof is None:
        return "unknown"
    try:
        field = which_oneof("payload_variant")
    except Exception:
        return "unknown"
    return field or "unknown"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "TEMPORARY diagnostic: log every outbound ToRadio message while "
            "idle, to distinguish this app's own traffic from the Meshtastic "
            "SDK's automatic heartbeat. Sends nothing of its own."
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

    try:
        info = radio.connect()
    except RadioConnectionError as error:
        log(f"Could not connect to {args.device}: {error}")
        return 1

    interface = radio._interface
    log(f"Connected: node={info.node_id}. Watching outbound ToRadio traffic.")
    log(
        "This script sends nothing itself -- do not touch the radio or any "
        "other MeshtasticPass window while observing. Ctrl+C to stop."
    )

    original_send = interface._sendToRadioImpl
    start = time.monotonic()

    def logging_send(to_radio: Any) -> Any:
        elapsed = time.monotonic() - start
        log(f"t+{elapsed:8.1f}s  OUTBOUND ToRadio: {describe_to_radio(to_radio)}")
        return original_send(to_radio)

    interface._sendToRadioImpl = logging_send

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log("Stopped.")
    finally:
        interface._sendToRadioImpl = original_send
        radio.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
