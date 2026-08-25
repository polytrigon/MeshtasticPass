# MeshtasticPass

A Nintendo StreetPass-inspired, keyboard-first Meshtastic companion for the
ClockworkPi uConsole. It currently supports a Meshtastic ESP32 over selectable
USB serial devices using the official Python SDK; `/dev/ttyUSB0` is the default,
not the only supported device. Automatic reconnect, channel CHAT, local history,
delivery-state feedback, node favorites, and a deterministic hardware-free
simulation mode are implemented.

MeshtasticPass is early-stage software. The current hardware path is a directly
attached USB serial radio; `meshtasticd` and HackerGadgets integration are not
implemented. MeshtasticPass is an independent project and is not affiliated
with or endorsed by Meshtastic.

Local settings are stored under `${XDG_CONFIG_HOME:-$HOME/.config}` and CHAT
data under `${XDG_DATA_HOME:-$HOME/.local/share}`. See [Privacy](PRIVACY.md) for
exact paths and data details, [Security](SECURITY.md) for private-reporting
guidance, and [Contributing](CONTRIBUTING.md) before preparing a change.

## Milestone 1: connect to the radio

Run these commands from your MeshtasticPass checkout on the uConsole:

```bash
cd /path/to/MeshtasticPass
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Confirm that Linux can see the radio:

```bash
ls -l /dev/ttyUSB0
```

If that command shows the device but Python reports permission denied, add your
current user to the serial-device group, then log out and back in:

```bash
sudo usermod -aG dialout "$USER"
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

Press `1` through `5` to open CONNECTION, CHAT, PROFILE, PASS MAP, or MESH. CHAT
shows and sends on the currently selected configured broadcast channel. Press
`C` outside the input to open the channel dropdown; use Up/Down and Enter to
select, or Escape to cancel. While the chat input is focused, normal text and
number keys remain input. Press `Escape` to focus the transcript, then use
Up/Down to move one whole message or contextual control at a time. Page
Up/Page Down still move by a viewport. Right Arrow jumps to the newest edge;
the physical End key remains an unadvertised equivalent. New
messages follow the bottom
only when the transcript is already near the bottom. If you are reading older
messages, `↓ n NEW` counts messages below the viewport; either newest shortcut
clears that indicator. Neither shortcut is advertised in the deliberately
minimal CHAT footer. There is no visible END OF CHAT control;
Up from the single-line composer enters transcript navigation at its final
message/action target. Up/Down explore sequential targets and Down past the
final target returns to the composer. In transcript navigation, Left selects the
oldest actual NEW incoming message in the current channel, while Right returns
to newest and resumes the preserved draft. Left/Right continue editing the text
caret while the composer is focused. End remains an unadvertised newest-only
shortcut. Press `F4` to quit from
any normal app context;
`q` is ordinary input and is not a quit shortcut. Radio
monitoring, CHAT storage, and the radio service close during shutdown.

Incoming NEW messages stay accented until they are read. Selecting one message
with Up/Down or the mouse acknowledges only that entry; scrolling, mounting,
and LOAD OLDER do not. A Favorite sender keeps its Favorite author accent after
the message's NEW-only styling is removed.

The selected channel heading uses the format
`CHAT · [ configured channel ▾ ] · ACTIVE N`. ACTIVE is a passive count of
unique *other* nodes whose freshest trustworthy node-database `lastHeard` or
packet directly observed by MeshtasticPass is less than five minutes old.
Exactly five minutes old is inactive. Missing, malformed, future, and otherwise
impossible timestamps are ignored, and the local node is excluded. The shared
one-second CHAT refresh ages this count even when no packets arrive. `ACTIVE —`
means the radio or activity data is unavailable; connected with no recent nodes
shows `ACTIVE 0`. Direct observations are held only in memory, deduplicated by
node ID, and cleared when the USB/radio selection changes. The receive callback
and node-database read call no transmission API, so ACTIVE sends no LoRa traffic.
The CONNECTION page's `NODES` value remains the total known node-database size,
so it is intentionally different from ACTIVE.

Simulation includes the local node, two recent nodes, one stale node, and nodes
with malformed or missing activity timestamps. It initially displays
`ACTIVE 2`; one node reaches the exact five-minute boundary after one second,
leaving `ACTIVE 1`. This deterministic transition makes activity aging testable
without a second radio. The primary-channel message script also deliberately
delivers a newer receiver-timestamped packet before an older one. CHAT inserts
the second packet into chronological position and shows
`1 OLDER MESSAGE RECEIVED`, so delayed ordering is visible without hardware.

### Passive MESH topology board

Press `5` to open MESH. The board places `YOU` at its center, arranges nodes
with trustworthy Meshtastic `hopsAway` values in deterministic hop layers, and
puts unknown-hop nodes in an outer layer. This is a topology view, not a map:
positions do not imply GPS location or physical direction, and the app draws no
link or route unless a future trustworthy data source can support one.

Active nodes use the current theme's BASE color. Nodes not heard within the
same exact five-minute window used by CHAT's ACTIVE count use DIM_BASE. An
active Favorite uses ACCENT for its label; stale Favorites stay dim so activity
truth remains visible. Long Name is preferred when it fits, then Short Name,
then an abbreviated Node ID. Missing metadata is omitted rather than invented.

Use all four arrow keys for spatial selection. Navigation does not wrap at the
board edges, and the two-axis viewport scrolls only inside MESH to keep the
selected node visible. Mouse selection uses the same focus state. Enter opens
the shared node context menu used by CHAT; remote nodes show available identity,
hop, receiver-age, and Favorite controls. `YOU` shows local identity only and
cannot be favorited. Opening MESH and its menu only read the SDK's already
synced node database—they do not request positions, probe nodes, or transmit
LoRa packets. While disconnected, the board says
`NO MESH DATA — RADIO DISCONNECTED` instead of displaying stale topology as
current.

For a hardware-free review, run `python app.py --simulate`, press `5`, navigate
the stable one-hop, two-hop, unknown-hop, active, stale, and missing-name nodes,
and verify Favorite changes are also visible in CHAT.

CHAT keeps its one-cell scrollbar allocation. Both the darker track and BASE
thumb use the same right-aligned narrow Unicode `▕` glyph through Textual's
supported per-scrollbar renderer hook, while retaining Textual's sizing, theme
colors, mouse metadata, wheel scrolling, and drag behavior. Message content has
one column of right padding before that far-right scrollbar, including when
messages wrap at narrow terminal widths.

Press Enter on a selected incoming CHAT entry to open its contextual node menu.
The menu shows only synchronized metadata the selected radio service actually
knows: Long Name, Short Name, and the optional Meshtastic node-database
`hopsAway` value. Missing fields are omitted; hop count is never estimated from
distance, packet age, or `hopLimit`, and opening the menu sends no radio traffic.
Informational rows are skipped by Up/Down action navigation. Press Enter on
FAVORITE or UNFAVORITE, or Escape to close and restore the message focus and
reading position. Outgoing YOU entries do not expose this menu. Direct Message
is intentionally not implemented yet.

Favorites are stored by stable lower-case Node ID in the existing
`~/.config/meshtasticpass/config.json` settings file. Favoriting immediately
recolors every mounted entry from that node and also applies when older history
is mounted later. Only the sender name receives persistent ACCENT styling;
ordinary timestamps remain DIM_BASE and message/delivery styling keeps its
existing semantics. The preference survives name changes and app restarts and
is never written to the radio.

### Stock firmware and messages received while the app is absent

Meshtastic firmware 2.7.11 does not provide a reliable persisted inbox for
ordinary text messages. Its normal phone/API path has a finite in-memory
`toPhoneQueue`: the classic ESP32 build allows 8 queued packets and other
supported configurations at that release allow 32. The queue is shared with
other packet types, can overflow, and is lost on reboot or power loss. When the
Python client reconnects, firmware drains surviving entries through the normal
receive callback automatically. There is no separate inbox-query API and no
live-versus-replayed marker. See the exact 2.7.11 firmware sources for
[`MAX_RX_TOPHONE`](https://github.com/meshtastic/firmware/blob/v2.7.11.ee68575/src/mesh/mesh-pb-constants.h),
[`MeshService::sendToPhone`](https://github.com/meshtastic/firmware/blob/v2.7.11.ee68575/src/mesh/MeshService.cpp),
and the [`PhoneAPI` queue drain](https://github.com/meshtastic/firmware/blob/v2.7.11.ee68575/src/mesh/PhoneAPI.cpp).

Queued packets retain their normal sender, packet ID, channel, and receiver-side
`rxTime`. MeshtasticPass's existing callback therefore accepts any surviving
queued text exactly like live text. SQLite's `(node_id, packet_id,
channel_index)` key deduplicates packets with IDs; packets lacking an ID cannot
be deduplicated safely. Because the callback exposes no replay marker, the UI
cannot truthfully label a packet as recovered rather than live.

The Python SDK's internal queue is for transmit flow control, not a received
message archive. SDK 2.7.11 decodes `STORE_FORWARD_APP`, but it has no high-level
reliable-inbox/history API. Firmware's optional
[`StoreForwardModule`](https://github.com/meshtastic/firmware/blob/v2.7.11.ee68575/src/modules/StoreForwardModule.cpp)
requires explicit configuration, suitable ESP32/Portduino hardware, server role
configuration, and PSRAM. Its history is also volatile, and remote history
requests transmit over LoRa. It is therefore not enabled or queried by
MeshtasticPass for this milestone. Short app absences may recover a small
best-effort set through the stock RAM queue, but the application does not claim
offline delivery guarantees. A reliable offline inbox would need a separately
designed, explicitly configured feature rather than hidden traffic or custom
firmware behavior.

### Persistent CHAT history

CHAT history is stored in SQLite at
`~/.local/share/meshtasticpass/chat.db`, or under `$XDG_DATA_HOME` when that
variable is set. The app creates the parent directory and version-2 schema on
first launch. Each channel transcript mounts only its newest 100 messages in
chronological order. When older rows exist, focus `[ LOAD OLDER ]` at the top
of CHAT and press Enter to prepend the next 50. SQLite uses the oldest loaded
message ID as a stable cursor, resolves that row's chronological key, and never
loads the whole archive for Python-side slicing or uses OFFSET. After the final
page, the top control is replaced by a passive,
non-focusable `END OF CHAT HISTORY` marker. The marker appears only when at
least one message exists and SQLite confirms that no older row remains; it is
not an action and arrow navigation skips it. Prepending
preserves the current visual reading position and does not affect `CHAT(n)` or
the transcript's `↓ n NEW` counter. Historical messages load as read/normal;
only messages received during the current process participate in NEW styling
and `CHAT(n)`. The default mounted window remains at 100 as new traffic
arrives by trimming its oldest persisted read entries; those rows stay in
SQLite and return through `[ LOAD OLDER ]`. Current-session NEW/unread entries
are never hidden, so the mounted window may exceed 100 when more than 100 such
entries exist. Explicitly loading an older page expands the mounted target by
that page's size.

The `messages` table stores logical incoming/outgoing entries, packet and node
identity, channel, text, optional trustworthy sender-origin time,
receiver-side `radio_rx_at`, local acceptance/send times, and the last known
delivery state. Version-1 databases are migrated in place without deleting
history. The `send_attempts` table stores each
intentional transmission attempt separately. Incoming packets with an ID are
deduplicated by `(node_id, packet_id, channel_index)`; packets without an ID
are retained because they have no safe stable deduplication key. Monotonic
values are never persisted. Runtime age references are reconstructed from the
stored wall clock values at startup.

Within each channel, CHAT orders rows by trustworthy message time, then local
acceptance time, then stable SQLite message ID. For outgoing entries, message
time is the local accepted-send time. For incoming entries, a genuine protocol
origin time is preferred when available; ordinary Meshtastic text instead uses
the receiver-side `rxTime` described below. An incoming packet with neither
time remains untimed: its local acceptance time is used only as a deterministic
arrival-order fallback and is not presented as an invented send time. Equal
timestamps therefore remain stable across restarts.

When a newly received timed packet sorts before the current chronological tail,
CHAT inserts it in place and shows `1 OLDER MESSAGE RECEIVED` (or the plural
count when several arrive). The session-only count tracks those specific older
arrivals until each is explicitly reviewed, decrementing one-by-one as they are
selected. It is isolated per channel, remains visible until its count reaches
zero, and yields to send/history errors without losing pending review state.
Startup loading, LOAD OLDER paging, duplicates, and normally ordered packets do
not create pending older-review state; channel switching preserves each
channel's live count. Existing broader read actions, such as leaving CHAT or
sending, clear the affected pending entries too.
Delayed entries keep normal NEW/unread semantics. An entry older than the
mounted window remains safely in SQLite until LOAD OLDER reaches it; the app
does not expand the whole archive or jump away from the user's reading position.
Here, NEW means newly received by this MeshtasticPass client, not necessarily
newly sent across the mesh; delayed and out-of-order delivery is expected.

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
to origin time. No speculative `DELAYED` marker is shown. The optional persisted
origin field exists for a future protocol that can supply one truthfully; it is
not populated from ordinary Meshtastic `rxTime`.

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
  broadcast CHAT this is implicit mesh evidence, not a human read receipt.
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
`#FF1744`. Ordinary read timestamps use the selected theme's semantic
`DIM_BASE`, while NEW incoming timestamps retain the alternate accent. The
single source of truth derives `DIM_BASE` as `BASE` at 35% opacity over the
`#101010` application background using Textual's RGB blend, resolving to
`#565656` (WHITE), `#1E6311` (GREEN), and `#633B0A` (ORANGE) for opaque terminal
rendering. The old manually assigned dark theme colors are no longer semantic
palette inputs. CHAT and CONNECTION/CONFIG scrollbars follow the same live
theme: the thin `▕` thumb uses BASE and its thin `▕` track uses DIM_BASE.

`SENDING` uses one shared UI animation timer. No state triggers an automatic
application-level resend. To rebroadcast an `UNCONFIRMED` or `FAILED` entry,
select it with Up/Down, move to its `[ RESEND ]` action, and press Enter. This records
another send attempt against the same visible message rather than duplicating
the transcript entry.

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

The first tab is **CONNECTION/CONFIG**. Its flat CONNECTION section immediately
shows CONNECTING, then CONNECTED and live radio metadata after the initial sync.
It has no heading caret or separate IDENTITY heading: Long Name, Short Name, and
Node ID sit directly below the connection rows. If the
radio disappears or a recoverable connection error occurs, the status clearly
says that automatic retry is active and stale node metadata is hidden. The full
CONNECTION / STYLE surface scrolls vertically on short terminals;
keyboard controls, dropdown operation, identity editing, mouse-wheel scrolling,
and the one-cell themed scrollbar remain available instead of compressing the
sections.

CONNECTION also has a USB DEVICE dropdown populated from currently discovered
serial ports. Selecting a different port saves it, closes the old connection,
and immediately starts the normal reconnect flow. Simulation offers the stable
fake choices `/dev/ttyUSB0` and `/dev/ttyUSB1` without enumerating host ports.

Long Name and Short Name are two-state keyboard controls: use Up/Down to reach a
bracketed value, press Enter to edit, then press Enter to validate/save or Escape
to restore the value from before editing. Arrow keys belong to the text caret
only while edit mode is active. USB DEVICE, LONG NAME, SHORT NAME, FONT SIZE,
and COLOR use Textual focus as their single navigation-selection source, so
exactly one row shows both the `>` caret and the same subtle selected-row
background used in CHAT. Passive STATUS, NODES, and NODE ID rows reserve the
same two-cell gutter, and all CONNECTION values share a compact aligned column.
The fields follow their content without creating horizontal page scrolling.

Saved identity values use Meshtastic Python SDK 2.7.11's supported
`interface.localNode.setOwner(long_name=...)` and
`interface.localNode.setOwner(short_name=...)` paths; they are not local-only
preferences. The Long Name protobuf buffer permits 39 UTF-8 payload bytes plus
its terminator. The Short Name field declares nanopb `max_size: 5`, permitting
four UTF-8 payload bytes plus its terminator. MeshtasticPass rejects empty or
oversized values before the SDK can truncate them, including multibyte values
that fit by character count but not by encoded size. Node ID remains read-only.
Identity fields are unavailable while disconnected, and the simulator provides
the same deterministic edit behavior without touching hardware. These supported
SDK operations may cause the firmware's normal identity propagation; the app
does not add a separate announcement packet. During an active CONNECTING state,
NODES and the three identity values use dimmed `...`.
Offline/error retry intervals use unavailable dashes instead of leaking the
previous radio identity; the next active attempt returns to the connecting state.
The STATUS value uses semantic colors: CONNECTING and OFFLINE/retry use ACCENT,
CONNECTED uses BASE, and ERROR uses the theme-independent electric ERROR red.
The STATUS label itself remains BASE.

The STYLE section has FONT SIZE and COLOR dropdowns. Focus a dropdown and press
Enter, then use Up/Down and Enter to select or Escape to cancel. Font choices
are SMALL (11), MEDIUM (13), LARGE (16), XL (18), and XXL (22). Color choices
are WHITE (the default), GREEN, and ORANGE. All settings save to
`~/.config/meshtasticpass/config.json`. Color changes the running Textual app
immediately. Font size is also written only to the dedicated MeshtasticPass
LXTerminal profile and applies on the next menu launch. Successful FONT SIZE
and COLOR confirmations use the active theme's ACCENT; COLOR applies its new
theme before rendering the confirmation. Save errors remain semantic ERROR red.

USB DEVICE, FONT SIZE, COLOR, CHAT channel, and contextual node menus share one
viewport-aware popup implementation. A popup prefers to open downward, opens
upward when that is the complete visible fit, and otherwise uses the larger
visible side with internal scrolling. Placement uses the terminal's real screen
region rather than the virtual height of CONNECTION's scrollable content.
Up/Down always keeps the highlighted option visible, including XXL at short
terminal heights; mouse selection remains supported.

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
cd /path/to/MeshtasticPass
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
mesh_topology.py   Pure deterministic MESH layout and arrow navigation
geo.py             Position validation, Haversine distance, and mile formatting
app_settings.py    Persistent user settings and LXTerminal profile updates
install-launcher.sh  Reproducible uConsole menu/fullscreen setup
radio_service.py   All Meshtastic connection and device-info logic
simulated_radio_service.py  Deterministic radio simulator
requirements.txt   Intentionally pinned supported Python dependencies
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
