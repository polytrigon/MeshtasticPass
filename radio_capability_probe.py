"""Read-only Meshtastic hardware/configuration capability probe.

Connects to a real radio exactly like check_radio.py does (a single
connection attempt, no retry loop), then prints:

- authoritative hardware/firmware identity (never inferred from the
  serial device path)
- the full sanitized capability matrix (see radio_capabilities.py)

This performs NO configuration writes, sends NO text, and generates
NO additional LoRa traffic -- every value printed was already read
during the radio's own normal initial configuration exchange. Secrets
(WiFi/MQTT passwords, channel PSKs, public/private/admin keys) are
never printed; only "configured"/"not configured" is shown for them.

Usage:

    python radio_capability_probe.py --device /dev/ttyUSB0
"""

import argparse

from radio_capabilities import format_capability_matrix
from radio_service import RadioConnectionError, RadioService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only capability/configuration probe for a connected Meshtastic radio"
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
        print(f"Could not connect to {args.device}: {error}")
        return 1

    try:
        print(f"Connected: {info.device_path}  node={info.node_id}")
        print()

        identity = radio.hardware_identity()
        print("=== HARDWARE IDENTITY (from the radio/API itself) ===")
        print(f"hw_model:          {identity.hw_model_name!r} (raw={identity.hw_model_raw})")
        print(f"  source:          {identity.hw_model_source}")
        print(f"role:              {identity.role_name!r} (raw={identity.role_raw})")
        print(f"  source:          {identity.role_source}")
        print(f"firmware_version:  {identity.firmware_version}")
        print(f"firmware_edition:  {identity.firmware_edition}")
        print(f"min_app_version:   {identity.min_app_version}")
        print(f"node_num:          {identity.node_num}")
        print(f"node_id:           {identity.node_id}")
        print(f"device_id:         {identity.device_id}")
        print(f"pio_env:           {identity.pio_env}")
        print(f"macaddr:           {identity.macaddr}")
        print(f"has_wifi:          {identity.has_wifi}")
        print(f"has_bluetooth:     {identity.has_bluetooth}")
        print(f"has_ethernet:      {identity.has_ethernet}")
        print(f"has_remote_hw:     {identity.has_remote_hardware}")
        print(f"has_pkc:           {identity.has_pkc}")
        print(f"can_shutdown:      {identity.can_shutdown}")
        print(f"excluded_modules:  {identity.excluded_modules}")
        print()

        rows = radio.capability_report()
        print(f"=== CAPABILITY MATRIX ({len(rows)} rows) ===")
        print(format_capability_matrix(rows))
    finally:
        radio.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
