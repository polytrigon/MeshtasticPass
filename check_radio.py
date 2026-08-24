"""First milestone: verify the uConsole can communicate with its radio."""

import argparse

from radio_service import RadioConnectionError, RadioService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check a Meshtastic USB radio")
    parser.add_argument(
        "--device",
        default="/dev/ttyUSB0",
        help="serial device path (default: /dev/ttyUSB0)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    radio = RadioService(args.device)

    print(f"CONNECTING: {args.device}")
    try:
        info = radio.connect()
        print("RADIO ONLINE")
        print(f"Device:   {info.device_path}")
        print(f"Node ID:  {info.node_id}")
        print(f"Name:     {info.long_name} ({info.short_name})")
        print(f"Firmware: {info.firmware_version}")
        print(f"Nodes:    {info.known_nodes} known")
        return 0
    except RadioConnectionError as error:
        print("RADIO ERROR")
        print(error)
        return 1
    except KeyboardInterrupt:
        print("\nConnection cancelled.")
        return 130
    finally:
        radio.close()


if __name__ == "__main__":
    raise SystemExit(main())
