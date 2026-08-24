# MeshtasticPass

A Nintendo StreetPass-inspired app for the ClockworkPi uConsole. The first
milestone verifies reliable USB serial communication with a Meshtastic ESP32.

## Milestone 1: connect to the radio

Run these commands on the uConsole from `/home/mt/meshtasticPass`:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Confirm that Linux can see the radio:

```bash
ls -l /dev/ttyUSB0
```

If that command shows the device but Python reports permission denied, add the
`mt` user to the serial-device group, then log out and back in:

```bash
sudo usermod -aG dialout mt
```

With the virtual environment active, run:

```bash
python check_radio.py
```

A successful initial connection and node/config sync prints:

```text
CONNECTING: /dev/ttyUSB0
RADIO ONLINE
Device:   /dev/ttyUSB0
Node ID:  !12345678
Name:     Example Node (EXMP)
Firmware: 2.x.x
Nodes:    1 known
```

Use a different serial device when needed:

```bash
python check_radio.py --device /dev/ttyACM0
```

## Current structure

```text
check_radio.py     Small command-line connection check
radio_service.py   All Meshtastic connection and device-info logic
requirements.txt   Python dependency
```

Future UI code should call `RadioService`; it should not import or manage the
Meshtastic SDK directly.
