"""Monitor connection state and print received Meshtastic text messages."""

import argparse

from radio_service import ReceivedMessage, RadioService, RadioState
from simulated_radio_service import SimulatedRadioService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Receive Meshtastic text messages")
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
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use deterministic fake radio events instead of hardware",
    )
    return parser.parse_args()


def display(value: object) -> object:
    return "unknown" if value is None or value == "" else value


def print_message(message: ReceivedMessage) -> None:
    print("RX MESSAGE")
    print(f"FROM      {display(message.sender_node_id)}")
    print(
        f"NAME      {display(message.sender_long_name)} "
        f"({display(message.sender_short_name)})"
    )
    print(f"CHANNEL   {display(message.channel_index)}")
    print(f"TEXT      {message.text}")
    print(f"RSSI      {display(message.rssi)}")
    print(f"SNR       {display(message.snr)}")
    print(f"PACKET    {display(message.packet_id)}")


def main() -> int:
    args = parse_args()
    radio = SimulatedRadioService() if args.simulate else RadioService(args.device)
    radio.add_message_handler(print_message)

    try:
        for event in radio.connection_events(args.retry_delay):
            if event.state is RadioState.CONNECTING:
                print(f"RADIO CONNECTING: {radio.device_path}")
            elif event.state is RadioState.ONLINE:
                print("RADIO ONLINE")
            elif event.state is RadioState.OFFLINE:
                print("RADIO OFFLINE")
                print(event.message)
                print(f"Retrying in {args.retry_delay:g} seconds...")
            elif event.state is RadioState.ERROR:
                print("RADIO ERROR")
                print(event.message)
                print(f"Retrying in {args.retry_delay:g} seconds...")
    except KeyboardInterrupt:
        print("\nMessage monitor stopped.")
    finally:
        radio.remove_message_handler(print_message)
        radio.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
