# AGENTS.md

Durable, tool-agnostic engineering rules for anyone (human or coding agent)
working on MeshtasticPass. This is the canonical shared rulebook; `CLAUDE.md`
is a thin, vendor-specific pointer back here. `README.md` is the detailed
design record; `CONTRIBUTING.md`, `PRIVACY.md`, and `SECURITY.md` are binding
policy. Where the README and the code disagree, the code is the current truth.

## What this project is

MeshtasticPass is a Nintendo StreetPass-inspired, keyboard-first Meshtastic
companion TUI for the ClockworkPi uConsole. It talks to a Meshtastic ESP32
radio over USB serial through the official Python SDK and renders a Textual
terminal UI (CONNECTION/CONFIG, CHAT, MESH, and DM-as-a-mode-inside-CHAT). It
is early-stage, independent of the Meshtastic project, and **has no software
license yet** -- `CONTRIBUTING.md` explicitly grants no permission to reuse or
redistribute.

Python 3.11. Exactly two runtime dependencies, intentionally pinned in
`requirements.txt`: `meshtastic==2.7.11` and `textual==8.2.8`. Dependency
changes must be separate, justified pull requests, never incidental cleanup.

## Setup, run, and validation

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Run the app:

```bash
python app.py                 # real radio (default /dev/ttyUSB0; --device to change)
python app.py --simulate      # deterministic hardware-free mode -- use this for development
python app.py --simulate --simulate-send-outcome unconfirmed --simulate-send-outcome sent
```

Smaller CLI entry points: `check_radio.py`, `receive_messages.py`,
`send_message.py` (all support `--simulate` where applicable).
`install-launcher.sh` installs the uConsole menu launcher (assumes labwc +
lxterminal).

Before opening a pull request, run all three checks (from `CONTRIBUTING.md`):

```bash
python -m unittest discover -v
python -m compileall -q .
git diff --check
```

CI (`.github/workflows/ci.yml`) runs those same checks plus focused fast
tests, the pytest-style probe test, and two headless `--simulate` smoke tests;
see "CI and the test stack" below. There is no linter, formatter, or type
checker.

Useful environment variables:

- `MESHTASTICPASS_RX_DEBUG=1` -- enables the read-only receive-pipeline debug
  trace (observational only; never changes routing/filtering, never adds RF).
- `XDG_CONFIG_HOME` / `XDG_DATA_HOME` -- honored for all app data. Settings
  live at `~/.config/meshtasticpass/config.json`, CHAT history at
  `~/.local/share/meshtasticpass/chat.db`. See `PRIVACY.md` for every path.

## Hard rules

These are project doctrine, and several are enforced by tests:

1. **Truthful data only.** Never fabricate hop counts, delivery
   confirmations, presence, acknowledgements, bearings, distances, or origin
   timestamps that the available radio data cannot support. `hopsAway` is a
   proximity count, not a route; `rxTime` is receiver-side, never a send time.
   When data is missing, show it as missing -- do not estimate from RSSI, SNR,
   arrival order, or node ID.
2. **No unexpected LoRa/RF traffic.** Passive features (MESH board, ACTIVE
   counts, context menus, capability audits) must read already-synced SDK
   state and never transmit. Any change that can generate radio traffic must
   say so explicitly in its pull request.
3. **Never commit private data.** No CHAT databases, node logs, `.env` files,
   credentials, channel keys, certificates, or local configuration. Use
   obviously synthetic data in tests and examples (`!a11ce001`, "Alice
   Trail", "Bob"). `tests/test_repository_hygiene.py` enforces this: it pins
   the required `.gitignore` patterns, verifies default runtime paths resolve
   outside the checkout, and runs a simulated session to prove nothing is
   written into the repo tree.
4. **Zero surrogate code points (U+D800-U+DFFF) in any `.py` file** --
   including escape sequences inside docstrings and comments. A surrogate
   escape in a string literal crashed module import on the uConsole's strict
   UTF-8 Linux while working on macOS; the hygiene test now scans every file.
   Do not describe astral emoji codepoint-by-codepoint.
5. **The UI never touches the Meshtastic SDK directly.** All SDK contact goes
   through `RadioService` (see architecture below).

## Architecture

Flat layout: all modules at the repository root, tests in `tests/`.

```text
app.py                      Entire Textual UI (~10k lines): widgets, inline CSS, MeshtasticPassApp, main()
app_controller.py           Non-visual chat state and the radio monitor
radio_service.py            The ONLY Meshtastic SDK boundary
simulated_radio_service.py  Deterministic hardware-free twin of RadioService
chat_store.py               The ONLY SQLite boundary (schema versioning + migrations)
app_settings.py             Persistent settings (config.json) and LXTerminal profile writing
theme_palette.py            Single source of theme colors (semantic tokens)
mesh_state.py               MESH working-set ranking and freshness classification (pure)
mesh_topology.py            MESH grid layout, relay-stage/connector routing, arrow navigation (pure, no I/O)
geo.py                      Position validation, Haversine distance, bearing, mile formatting
grapheme_text.py            Grapheme-cluster-safe width, truncation, wrapping
node_activity.py            Two-hour activity semantics (firmware-source-verified NUM_ONLINE_SECS threshold)
radio_capabilities.py       Read-only capability/schema audit helpers (no RF)
radio_config_snapshot.py    Read-only synced radio-config snapshot model
message_time.py, relative_time.py, host_timezone.py, terminal_cursor.py,
keyboard_dropdown.py, viewport_menu.py, serial_devices.py   Small single-purpose helpers
check_radio.py, receive_messages.py, send_message.py        Small CLI entry points
*_probe.py, inspect_chat_store.py                           Standalone hand-run diagnostics (not wired into app.py)
```

Layering rules that keep this testable:

- **`radio_service.py` is the sole SDK boundary.** It exposes
  application-level types (`ReceivedMessage`, `RadioInfo`, `NodeMetadata`,
  `DeliveryState`, ...) and application-level exceptions
  (`RadioConnectionError`, `RadioSendError`, `RadioIdentityError`). SDK
  exceptions and packet dictionaries never escape it. UI and controller code
  must not `import meshtastic`.
- **`simulated_radio_service.py` implements the same interface.** Code accepts
  either service via `create_radio_service(...)` and never branches on "is
  this simulation?" elsewhere. When adding a feature to `RadioService`, add the
  matching deterministic behavior to the simulator so the feature is reviewable
  without hardware.
- **Pure logic lives outside `app.py`.** `app.py` is a single large file; the
  project's answer to that is to push rules and computation into small,
  independently tested modules (`mesh_state`, `mesh_topology`, `geo`,
  `grapheme_text`, `node_activity`, `message_time`, `relative_time`). Follow
  that split for new logic -- `app.py` should render and route input.
- **`chat_store.py` owns SQLite.** Versioned schema with in-place migrations;
  it never deletes or silently replaces a malformed database, and history
  paging uses stable ID cursors, never OFFSET. A failed history write surfaces
  a CHAT error while the radio/UI keeps running.
- **`theme_palette.py` is the single color source.** Widgets consume semantic
  tokens (BASE, ACCENT, ACCENT2, DIM, ERROR, CONFIRM, ...); never hard-code a
  theme-specific literal color, and never alias one semantic token to another.

## Canonical node identity rules

- **Stable Node ID is the only identity.** Meshtastic Node IDs are the
  canonical `"!"` + 8 lowercase hex digits form. `mesh_state.normalize_mesh_node_id`
  is the single canonicalizer: lowercase, `"!"`-prefixed, 8-hex-digit; a bare
  decimal node number is converted to its hex form. Favorites are persisted by
  lowercased Node ID (`app_settings` strips + lowercases on load/save).
- **Display names are never identity.** Long Name and Short Name are labels
  that can change or collide; never key any state (favorites, DM
  conversations, MESH joins) on them. `tests/test_viewport_context_favorites.py`
  proves two nodes with the same display name stay distinct favorites.
- **Node numbers are compared as 32-bit unsigned.** The wire format is uint32,
  but the same bit pattern can surface signed or unsigned; `_canonical_node_number`
  masks to 32 bits for the is-local comparison. String node-ID normalization is
  separate (`normalize_mesh_node_id`).

## CHANNEL vs DM identity rules

- **A DM conversation is a distinct model from channel CHAT**, keyed entirely
  by the remote party's canonical node ID (`dm_node_id`) -- never by
  `channel_index`, which carries no meaning on a DM row (always 0). Every DM
  query filters by `dm_node_id`.
- `dm_node_id` is set to the **same** value for both directions of one
  conversation: the sender for an incoming DM, the destination for an outgoing
  DM. A channel/broadcast row always has `dm_node_id IS NULL`.
- Incoming DM-ness comes from `RadioService`'s own destination-field
  classification (`message.is_direct`), never re-derived from sender name or
  channel index.
- `channel_key` (`ChannelInfo.stable_key`) is stable **channel** identity for
  channel-history isolation; it is NULL on DM rows. A NULL `channel_key` on an
  old channel row is grandfathered ("matches any filter"), never silently
  discarded.
- Never construct DM identity from a display name; the DM header/selector uses
  the stable ID (`DM / <display_name> / <node_id>`).

## Persistence expectations

- **Settings** live at `~/.config/meshtasticpass/config.json` (or
  `$XDG_CONFIG_HOME/meshtasticpass/config.json`). Saves are atomic
  (temp file + `os.replace`), retain unknown future keys, and are
  backward-compatible -- `font_size` persists even though the UI labels it
  "UI SCALE"; defaults are fallbacks, never overwrites of an existing user
  choice.
- **CHAT history** lives at `~/.local/share/meshtasticpass/chat.db` (or
  `$XDG_DATA_HOME/meshtasticpass/chat.db`). `chat_store.py` owns the versioned
  schema (currently v4) with in-place migrations; it never deletes or
  replaces a malformed database. Paging uses stable ID cursors, never OFFSET.
- **Deduplication** is by `(node_id, packet_id, channel_index,
  COALESCE(dm_node_id, ''))` for incoming packets that carry an ID; packets
  without an ID are retained (no safe dedup key).
- **Abandoned SENDING rows** are rewritten to `INTERRUPTED` once at
  `ChatStore.open()` startup (`reconcile_abandoned_sending`), before any
  history is hydrated -- never to SENT/HEARD, and never a retransmit.
- Runtime-only age references are reconstructed from stored wall-clock values
  at startup; monotonic values are never persisted. A failed history write
  surfaces a CHAT error while the radio/UI keeps running.

## Delivery-state monotonicity

- Delivery states are `SENDING / SENT / HEARD / UNCONFIRMED / FAILED`, plus
  `INTERRUPTED` for a persisted `SENDING` row left behind by a dead process.
  `SENDING` and `SENT` intentionally display the same; `SENT` is internal
  (drives the ACK timeout, persistence, and send attempts). The visible
  resolved states are `HEARD`, `UNCONFIRMED`, and `FAILED`.
- **Monotonicity**: once an attempt reaches `SENT`, `HEARD`, or `FAILED`, a
  later confirmation-timeout tick must never downgrade it (especially not to
  `UNCONFIRMED`). A reload is not evidence and must never move a resolved state.
- **A genuinely late but real ack still promotes** an already-`UNCONFIRMED`
  entry to `SENT`/`HEARD`; real evidence is never discarded merely because it
  arrived after the timeout. Duplicate/repeat statuses are harmless.
- `HEARD` on broadcast is implicit mesh evidence, not a read receipt. A
  matching routing ack whose `from` is the local node is `SENT` (implicit
  ack), never `HEARD`.
- Explicit user RESEND always creates a new logical send (new packet ID, new
  send-attempt row) against the same visible entry -- never a duplicate
  transcript entry, and never a downgrade of the prior attempt.

## RF and config safety boundaries

- **`RadioService` is the only place that may generate RF.** Passive features
  (MESH board, ACTIVE counts, context menus, capability audits, config
  snapshots, `read_synced_config_field`, `read_primary_channel_settings`) read
  already-synced SDK state and never transmit.
- **No automatic RF for UI operations.** Opening MESH or a node context menu,
  toggling a favorite, DEL (local history delete), and SAVE of a network preset
  are all zero-RF. Traceroute is the one explicit, user-triggered RF action and
  is offered only from MESH's own menu.
- **Config writes are explicit APPLY only.** `save_radio_config_preset` never
  touches the connected radio. `apply_radio_config_preset` validates (modem
  preset name, PSK) before any RF, then stages + writes fire-and-forget and
  verifies with one fresh readback (`verify_radio_config_preset`), never an
  intermediate per-field ACK wait. `sync_clock` is opt-in only. Any change that
  can generate RF must be disclosed in the PR.

## Async, correlation, and token safety

- The radio monitor runs on a dedicated daemon thread; events reach the UI via
  `on_event`/`on_message`. Shutdown closes the radio on its own bounded daemon
  thread so a serial-layer stall can never hang app exit.
- Sends are correlated by **packet ID** (`requestId`). `_pending_sends` maps
  packet ID to `(status_handler, expected_destination_number)` and is bounded
  (`_MAX_PENDING_SENDS`).
- **A DM ack must come from the exact destination node.** A clean routing
  response from any other node resolves to no status (still pending), never
  `HEARD` and never a NAK/`FAILED`.
- **Stale interfaces never resolve sends.** A routing response from an
  interface other than the current one, or after a reconnect, is ignored
  (`_on_routing_response` checks `interface is self._interface`);
  `_connection_generation` guards the config snapshot.
- Message identity is never derived from text content (six identical sends are
  six logical sends with distinct packet IDs). `send_generation` tokens and the
  `deleted` flag on a `ChatEntry` guard against stale async completions mutating
  or resurrecting an entry the user already removed.

## Reconnect lifecycle expectations

- The connection loop is `CONNECTING -> ONLINE -> OFFLINE -> retry`, with a
  default 5-second retry. It yields state changes; setup/SDK failures surface
  as an ERROR event and the loop keeps retrying instead of crashing.
- **A new connection is a new generation.** Every successful `connect()` bumps
  `_connection_generation` and rebuilds the config snapshot fresh; the old
  snapshot is replaced, never merged. `close()` discards the snapshot and
  local identity so a stale previous radio can never be shown for the next.
- Direct observations and link observations are cleared on radio identity
  change, `set_device_path`, and `close`, so a previous connection's readings
  never leak into a new one.

## MESH topology truthfulness

- MESH is a bounded working set (YOU plus up to 8 remote nodes), not an
  all-time catalog; ranking is recent message activity, then favorites, then
  last-heard recency, then Node ID.
- Bearing is computed only from trustworthy position data; it is never inferred
  from `hopsAway`, RSSI, SNR, arrival order, or node ID. A node without a
  resolvable bearing goes to a deterministic outer-ring slot, never labeled
  "UNKNOWN". MESH draws only YOU-to-node connectors, never fabricated
  node-to-node edges.
- Anonymous relay-stage markers (`mesh_topology.RelayStage`, a hollow circle
  derived from a client's `hopsAway` count) are visual topology only: never
  named, selectable, or navigable, and rendered only while they are a waypoint
  on a currently visible connector chain -- never as a standalone orphan dot.
- `is_relay` is always false today: no current SDK data identifies a specific
  relay; never set it from hop count, configured role, or position.
- `hopsAway` is a proximity count, not a route; `rxTime` is receiver-side.
  Show missing data as missing.

## Theme and style conventions

- Two user themes (SNOW, AMBER), each defining the semantic tokens
  BASE/ACCENT/ACCENT2/DIM/ERROR/CONFIRM. Widgets consume tokens, never
  theme-specific literal colors, and never alias one token to another
  (CONFIRM is always a distinct token even where it equals ACCENT). DIM is
  derived (BASE at 50% over the background), never hand-picked.
- Legacy color values migrate deterministically (`white`/`green` -> SNOW,
  `orange` -> AMBER); never discard a saved preference.
- Code conventions: `from __future__ import annotations` at the top of every
  module; full type annotations (including `-> None` on tests);
  `@dataclass(frozen=True)` for value types; `Enum` for state machines;
  `snake_case`/`CamelCase`/`SCREAMING_CASE`/leading `_`. Every file starts with
  a one-sentence module docstring; docstrings record *why* (firmware/SDK
  citations, reproduced bugs, rejected alternatives). Use `--` for dashes in
  Python source, not the Unicode em dash.

## Firmware/SDK facts to respect (pinned 2.7.11)

- Long Name allows 39 UTF-8 payload bytes; Short Name allows 4 (nanopb
  `max_size: 5`). Validate by encoded byte size app-side before the SDK can
  truncate.
- `hop_limit` protocol range is 0-7.
- Firmware has no reliable persisted inbox; the phone queue is small, volatile,
  and drained through the normal receive callback with no replay marker. The
  SDK's internal queue is transmit flow control, not a receive archive.

## Test strategy

- **`unittest` is the primary/default test suite** (`python -m unittest
  discover -v`). Tests live in `tests/test_<module>.py`, classes named
  `<Thing>Tests(unittest.TestCase)`, ending with
  `if __name__ == "__main__": unittest.main()`.
- **pytest is permitted where the repository already uses it.** Exactly one
  module, `tests/test_radio_write_readback_probe.py`, is pytest-style (20
  function tests invisible to `unittest discover`), and CI runs it separately.
  Do not migrate existing tests between frameworks incidentally, and do not
  add `pytest` to `requirements.txt` (it stays a CI-only test dependency).
- UI behavior is tested with Textual's pilot harness (`IsolatedAsyncioTestCase`
  + `app.run_test()`). Tests are behavior-focused and thorough; a change to
  user-visible behavior needs a test that describes that behavior.
- **Automated tests never touch live serial hardware.** They use
  `SimulatedRadioService` or `ControllableSendRadioService`; the simulation
  test asserts the real `_open_interface` and serial path are never reached.
  See `tests/test_simulated_radio_service.py::test_does_not_touch_sdk_or_serial_device`.
- Use obviously synthetic data only.

### CI and the test stack

`.github/workflows/ci.yml` is the PR gate and is **simulation-only** (no
serial devices, no live radio config writes, no LoRa traffic). It runs, in
order: byte-compile (`compileall`), the whitespace check against the PR base
(`git diff --check`), a focused fast-test subset, the pytest-style probe test
(`python -m pytest tests/test_radio_write_readback_probe.py -q`), the full
`unittest discover` run, and two headless `--simulate` smoke tests.

## Git, branch, and PR workflow

- Default branch: `main`. Work on kebab-case topic branches; keep unrelated
  changes out of the branch. Integration is via pull request.
- Commit subjects are imperative and specific; bodies explain the symptom, the
  mechanism, and the tests added.
- Pull requests must explain user-visible behavior, tests, hardware
  assumptions, and any radio traffic the change can generate.

## Agent merge and validation honesty

- **Do not merge your own pull request unless explicitly instructed to.** A
  coding agent may push a topic branch and open a PR, but must not merge it
  into `main` on its own initiative.
- **Hardware-only validation must be reported honestly.** If a change has only
  been validated in `--simulate`, say so; never claim a real-radio result that
  was not actually observed. When a behavior is hardware-only and was not run,
  label it as unverified rather than passing.
