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
accepts broadcast text on channel 0. While the chat input is focused, normal
text and number keys remain input. Press `Escape` to focus the transcript,
then use Up/Down, Page Up/Page Down, or End. New messages follow the bottom
only when the transcript is already near the bottom. If you are reading older
messages, `↓ n NEW` counts messages below the viewport; End jumps to the newest
entry and clears that indicator. Press `q` outside the input to quit. Radio
monitoring, CHAT storage, and the radio service close during shutdown.

### Persistent CHAT history

CHAT history is stored in SQLite at
`~/.local/share/meshtasticpass/chat.db`, or under `$XDG_DATA_HOME` when that
variable is set. The app creates the parent directory and version-1 schema on
first launch. The TUI mounts only the newest 100 PRIMARY-channel messages in
chronological order. When older rows exist, focus `[ LOAD OLDER ]` at the top
of CHAT and press Enter to prepend the next 50. SQLite uses the oldest loaded
message ID as a stable cursor, never loads the whole archive for Python-side
slicing, and removes the control after the final partial page. Prepending
preserves the current visual reading position and does not affect `CHAT(n)` or
the transcript's `↓ n NEW` counter. Historical messages load as read/normal;
only messages received during the current process participate in NEW styling
and `CHAT(n)`. Current-session NEW messages are never removed to enforce the
100-row startup window.

The `messages` table stores logical incoming/outgoing entries, packet and node
identity, channel, text, receiver-side `radio_rx_at`, local acceptance/send
times, and the last known delivery state. The `send_attempts` table stores each
intentional transmission attempt separately. Incoming packets with an ID are
deduplicated by `(node_id, packet_id, channel_index)`; packets without an ID
are retained because they have no safe stable deduplication key. Monotonic
values are never persisted. Runtime age references are reconstructed from the
stored wall clock values at startup.

If the database cannot be opened or one history write fails, the app reports a
CHAT history error and keeps the radio/UI running. It never deletes or silently
replaces a malformed database.

### CHAT age and distance metadata

Relative time uses compact `s`, `m`, `h`, and `d` units and refreshes in place
once per second through one shared Textual timer. There is no per-message
timer or widget remount. Outgoing entries show local acceptance age, such as
`8s` or `1h 3m`.

Meshtastic Python SDK 2.7.11 and its generated `MeshPacket` protobuf define
`rxTime` as the time the connected ESP32 received the packet. The protobuf
explicitly says this field is never sent over the radio link; the receiving
device adds it for its phone/API client. Ordinary `TEXT_MESSAGE_APP` data is
only the text payload and has no trustworthy sender-origin wall-clock field.
Forwarding therefore cannot turn `rxTime` into original send time. The
Store-and-Forward protocol is a separate `STORE_FORWARD_APP` payload and its
replayed text field likewise supplies no sender-origin timestamp to the
ordinary text callback used here.

MeshtasticPass keeps `origin_sent_at`, `radio_rx_at`, and local application
acceptance time (`app_received_at`) as separate concepts. `origin_sent_at` remains unset for
ordinary text, so incoming age is labeled honestly as receiver age:

```text
Alice Trail / RX 5m / 5.1miles
```

If a future protocol provides a trustworthy origin time, the application
model can display that age without `RX`; no current packet field is promoted
to origin time. No speculative `DELAYED` marker is shown, and no SQLite schema
migration was added because there is no real origin timestamp to persist.

When both nodes have usable positions, an incoming header also shows the
straight-line Haversine distance in miles:

```text
Alice Trail / RX 5m / 5.1miles
```

`RadioService` reads the latest local and sender positions from the SDK 2.7.11
node database. The SDK normally supplies decimal `latitude` and `longitude`
after converting `latitudeI` and `longitudeI`; both forms are accepted safely.
The application-level position model also retains the GPS-solution `timestamp`,
or the received-position `time` when available, so freshness policy can be
added later. This milestone does not reject an otherwise valid coordinate by
age.

Distances below 0.1 mile show `<0.1miles`, distances below 10 miles use one
decimal place, and distances of 10 miles or more use whole miles. Distance is
omitted when either coordinate is unavailable or malformed. It is never
estimated from RSSI, SNR, hops, or routing data, and it reflects last-known
positions rather than guaranteed current locations. Outgoing `YOU` entries do
not show distance. Simulated Alice has deterministic coordinates; simulated
Bob intentionally has no position to exercise the omitted case.

### Outgoing state meanings

MeshtasticPass is tested against the pinned Meshtastic Python SDK 2.7.11.
That SDK's `sendText` returns a packet with an ID after the packet is accepted
for local submission. With `wantAck=True`, its response handler receives a
matching `ROUTING_APP` packet whose `requestId` is the outbound packet ID.
SDK 2.7.11 converts the routing protobuf with default `MessageToDict` behavior:
known enum values are symbolic strings, while the optional `NONE` field may be
omitted entirely or appear as the string `NONE` when explicitly present. Both
forms are ACK evidence. Known symbolic routing errors are definite NAKs.
Unknown future enum numbers remain numeric and are ignored rather than being
misreported as success or failure.
The SDK describes a broadcast ACK from the local node as an implicit ACK:
the packet likely propagated, but delivery cannot be guaranteed. The Python
SDK does not expose a human read receipt, and its response-handler table does
not implement its own expiry.

The internal state and persistence model keeps all five meanings:

- `SENDING`: MeshtasticPass has started the SDK call.
- `SENT`: SDK 2.7.11 returned a packet ID after accepting local submission;
  this is not remote delivery.
- `HEARD`: a matching routing ACK with `errorReason=NONE` arrived. For the
  PRIMARY broadcast this is implicit mesh evidence, not a human read receipt.
- `UNCONFIRMED`: no matching ACK/NAK arrived during the SDK's default
  300-second response window. Transmission may still have occurred.
- `FAILED`: a definite local SDK exception or routing NAK occurred.

CHAT intentionally presents both internal `SENDING` and `SENT` as the same
animated `SENDING...` state. `SENT` remains internal because it drives the
ACK timeout, SQLite history, send attempts, and diagnostics, but local SDK
acceptance is not useful as a separate user-facing delivery claim. The visible
resolved states are `HEARD`, `UNCONFIRMED`, and `FAILED`.

Delivery colors use the theme's semantic base/accent pairing: WHITE uses GREEN
for `SENDING...` and `UNCONFIRMED`, GREEN uses ORANGE, and ORANGE uses WHITE.
`HEARD` uses the selected base color, while `FAILED` always uses electric red
`#FF1744`. The CHAT scrollbar follows the same live theme: its thumb uses the
WHITE/GREEN/ORANGE base and its track uses the corresponding subdued gray,
dark green, or dark orange.

`SENDING` uses one shared UI animation timer. No state triggers an automatic
application-level resend. To rebroadcast an `UNCONFIRMED` entry, press Escape,
focus that exact outgoing entry with Tab/Shift+Tab, then press `R`
when `[R] REBROADCAST` appears. This records another send attempt against the
same visible message rather than duplicating the transcript entry.

Simulation outcomes are explicit and repeatable. The default is successful
local submission. To exercise an unconfirmed attempt followed by a successful
manual rebroadcast, run:

```bash
python app.py --simulate \
  --simulate-send-outcome unconfirmed \
  --simulate-send-outcome sent
```

Use `heard` or `failed` for the other deterministic outcomes. Simulation does
not claim a real LoRa acknowledgement.

Known limitation: this milestone does not queue sends while the radio is
offline, recover in-flight callbacks after restart, or implement
store-and-forward. Those behaviors are intentionally reserved for the next
milestone.

The first tab is **CONNECTION/CONFIG**. Its CONNECTION section immediately shows
CONNECTING, then ONLINE and live radio metadata after the initial sync. If the
radio disappears or a recoverable connection error occurs, the status clearly
says that automatic retry is active and stale node metadata is hidden.

The STYLE section has FONT SIZE and COLOR rows. Use Up/Down to move between the
rows and Left/Right to select a value. Font choices are SMALL (11), MEDIUM (13),
LARGE (16), and XL (18). Color choices are WHITE (the default), GREEN, and
ORANGE. Both values save immediately to
`~/.config/meshtasticpass/config.json`. Color changes the running Textual app
immediately. Font size is also written only to the dedicated MeshtasticPass
LXTerminal profile and applies on the next menu launch.

### Install the uConsole menu launcher

From the project directory on the uConsole, run:

```bash
chmod +x install-launcher.sh
./install-launcher.sh
```

Running the installer again safely updates the same launcher. The menu entry
remains at **Startup / Accessories / MeshtasticPass** and starts the real-radio
path (`python app.py`), not simulation mode.

The installer creates a dedicated lxterminal profile with a generic
monospaced font, hides lxterminal's menu and scrollbar, and uses `--no-remote`
so these settings do not alter or inherit from ordinary terminal windows. It
uses the saved MeshtasticPass font size, or the default size 13 when no setting
exists, and never resets an existing selection.
Because lxterminal has no fullscreen command-line option, the installer adds a
title-specific MeshtasticPass window rule to the user's labwc `rc.xml` and
reloads labwc when it is running. Existing labwc settings are preserved. This
assumes the uConsole session uses labwc and lxterminal; on another window
manager the app will still launch, but fullscreen behavior is not guaranteed.

To launch manually without the menu or launcher-specific presentation:

```bash
cd /home/mt/meshtasticPass
source .venv/bin/activate
python app.py
```

## Current structure

```text
check_radio.py     Small command-line connection check
receive_messages.py  Text-message receive monitor
send_message.py    One-shot real or simulated text sender
app.py             Keyboard-first Textual application
app_controller.py  Non-visual chat state and radio monitor
chat_store.py      Versioned SQLite CHAT history and send attempts
geo.py             Position validation, Haversine distance, and mile formatting
app_settings.py    Persistent user settings and LXTerminal profile updates
install-launcher.sh  Reproducible uConsole menu/fullscreen setup
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
