#!/usr/bin/env python3
"""Send one application-level text message through a real or simulated radio."""

from __future__ import annotations

import argparse

from radio_service import RadioConnectionError, RadioSendError, RadioService
from simulated_radio_service import SimulatedRadioService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a Meshtastic text message")
    parser.add_argument("text", help="text message to send")
    parser.add_argument("--channel", type=int, default=0, help="channel index (0-7)")
    parser.add_argument("--to", dest="destination", help="destination node ID")
    parser.add_argument("--device", default="/dev/ttyUSB0", help="serial device")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use the deterministic simulator instead of hardware",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    radio = (
        SimulatedRadioService()
        if args.simulate
        else RadioService(device_path=args.device)
    )

    try:
        radio.connect()
        message = radio.send_text(
            args.text,
            channel_index=args.channel,
            destination_node_id=args.destination,
        )
    except (RadioConnectionError, RadioSendError) as error:
        print(f"SEND ERROR: {error}")
        return 1
    finally:
        radio.close()

    status = "SIMULATED MESSAGE ACCEPTED" if args.simulate else "MESSAGE SENT"
    destination = message.destination_node_id or "broadcast"
    print(status)
    print(f"TO        {destination}")
    print(f"CHANNEL   {message.channel_index}")
    print(f"TEXT      {message.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
