# MeshtasticPass

A Nintendo StreetPass-inspired app for the ClockworkPi uConsole. The current
milestone maintains a resilient USB serial connection to a Meshtastic ESP32.

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

With the virtual environment active, start the radio monitor:

```bash
python check_radio.py
```

A successful initial connection and node/config sync prints:

```text
RADIO CONNECTING: /dev/ttyUSB0
RADIO ONLINE
Device:   /dev/ttyUSB0
Node ID:  !12345678
Name:     Example Node (EXMP)
Firmware: 2.x.x
Nodes:    1 known
```

Leave the monitor running and unplug the radio. It will report `RADIO OFFLINE`
and retry every five seconds. After the radio is plugged back in and its initial
sync completes, the monitor reports `RADIO ONLINE` again. Press `Ctrl+C` to
stop it. Setup or SDK failures are reported separately as `RADIO ERROR`, and
the monitor continues retrying instead of crashing.

Change the retry interval when needed:

```bash
python check_radio.py --retry-delay 3
```

Use a different serial device when needed:

```bash
python check_radio.py --device /dev/ttyACM0
```

## Milestone 5: terminal application

Install the updated dependencies, then start the real-radio terminal UI:

```bash
python -m pip install -r requirements.txt
python app.py
```

Develop and test without radio hardware:

```bash
python app.py --simulate
```

Press `1` through `4` to open CONNECTION, CHAT, PROFILE, or PASS MAP. CHAT
accepts broadcast text on channel 0 and shows accepted local sends as `YOU`;
this marker does not imply delivery. While the chat input is focused, normal
text and number keys remain input. Press `Escape` to leave the input, then `q`
to quit. Radio monitoring stops and the service closes during shutdown.

## Current structure

```text
check_radio.py     Small command-line connection check
receive_messages.py  Text-message receive monitor
send_message.py    One-shot real or simulated text sender
app.py             Keyboard-first Textual application
app_controller.py  Non-visual chat state and radio monitor
radio_service.py   All Meshtastic connection and device-info logic
simulated_radio_service.py  Deterministic radio simulator
requirements.txt   Python dependency
tests/             Hardware-free connection resilience tests
```

Future UI code should call `RadioService`; it should not import or manage the
Meshtastic SDK directly.

## Milestone 3: receive text messages

Run the message monitor with the virtual environment active:

```bash
python receive_messages.py
```

Send a normal Meshtastic text message to this radio from another node or client.
Confirm that one `RX MESSAGE` block appears with the sender, channel, text,
signal information, and packet ID. Non-text packets are ignored.

While the monitor is still running, unplug and reconnect the ESP32. Confirm the
state changes from `RADIO ONLINE` to `RADIO OFFLINE` and back to `RADIO ONLINE`,
then send another text message and confirm it is printed exactly once.

`ReceivedMessage` is the application-level message type. Future chat or UI code
should register a handler with `RadioService` instead of parsing Meshtastic SDK
packet dictionaries.

## Milestone 3.5: simulation mode

Run the same message monitor without Meshtastic hardware:

```bash
python receive_messages.py --simulate
```

The simulator transitions from `RADIO CONNECTING` to `RADIO ONLINE`, then emits
three deterministic `RX MESSAGE` blocks from stable fake nodes. It remains
running until `Ctrl+C`, just like the real-radio path.

`SimulatedRadioService` implements the same connection-event, message-handler,
and shutdown methods used by `receive_messages.py`. Future application code can
therefore accept either service without parsing fake data or adding simulation
branches throughout the application. Explicit hooks are available for later
disconnect, reconnect, error, and custom-message scenarios; no StreetPass
protocol behavior is simulated yet.

## Milestone 4: send text messages

Application code can send broadcast or direct text messages through the same
small API on `RadioService` and `SimulatedRadioService`:

```python
radio.send_text(
    "hello",
    channel_index=0,
    destination_node_id=None,
)
```

On a connected real radio, try these one-shot commands:

```bash
python send_message.py "hello"
python send_message.py "hello" --channel 1
python send_message.py "hello" --to !a11ce001
```

Test the same application path without hardware:

```bash
python send_message.py "hello" --simulate
```

The real service translates the request to Meshtastic's `sendText` method.
The simulator stores accepted sends in its read-only `sent_messages` history.
An accepted send does not claim LoRa delivery or an acknowledgement. Invalid
input, a disconnected radio, and SDK failures raise the application-level
`RadioSendError`, so future Chat code does not need to handle SDK exceptions.
