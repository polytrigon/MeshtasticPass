"""Keep the uConsole connected to its Meshtastic radio."""

import argparse

from radio_service import RadioService, RadioState


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check a Meshtastic USB radio")
    parser.add_argument(
        "--device",
        default="/dev/ttyUSB0",
        help="serial device path (default: /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--retry-delay",
        default=5.0,
        type=float,
        help="seconds between connection attempts (default: 5)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    radio = RadioService(args.device)

    try:
        for event in radio.connection_events(args.retry_delay):
            if event.state is RadioState.CONNECTING:
                print(f"RADIO CONNECTING: {args.device}")
            elif event.state is RadioState.ONLINE and event.info is not None:
                print("RADIO ONLINE")
                print(f"Device:   {event.info.device_path}")
                print(f"Node ID:  {event.info.node_id}")
                print(
                    f"Name:     {event.info.long_name} "
                    f"({event.info.short_name})"
                )
                print(f"Firmware: {event.info.firmware_version}")
                print(f"Nodes:    {event.info.known_nodes} known")
            elif event.state is RadioState.OFFLINE:
                print("RADIO OFFLINE")
                print(event.message)
                print(f"Retrying in {args.retry_delay:g} seconds...")
    except KeyboardInterrupt:
        print("\nRadio monitor stopped.")
    finally:
        radio.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
