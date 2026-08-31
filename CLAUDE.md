# CLAUDE.md

Guidance for contributors and AI coding assistants working in this repository.
`README.md` is the detailed design record; `CONTRIBUTING.md`, `PRIVACY.md`, and
`SECURITY.md` are binding policy. Where the README and the code disagree, the
code is the current truth (see "Gotchas" below).

## What this project is

MeshtasticPass is a Nintendo StreetPass-inspired, keyboard-first Meshtastic
companion TUI for the ClockworkPi uConsole. It talks to a Meshtastic ESP32
radio over USB serial through the official Python SDK and renders a Textual
terminal UI (CONNECTION/CONFIG, CHAT, MESH, and DM). It is early-stage,
independent of the Meshtastic project, and **has no software license yet** --
`CONTRIBUTING.md` explicitly grants no permission to reuse or redistribute.

Python 3.11. Exactly two runtime dependencies, intentionally pinned in
`requirements.txt`: `meshtastic==2.7.11` and `textual==8.2.8`. Dependency
changes must be separate, justified pull requests, never incidental cleanup.

## Setup and commands

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

There is no linter, formatter, type checker, or CI configured. Tests use the
standard library `unittest` only -- do not introduce pytest.

Useful environment variables:

- `MESHTASTICPASS_RX_DEBUG=1` -- enables receive-pipeline debug tracing.
- `XDG_CONFIG_HOME` / `XDG_DATA_HOME` -- honored for all app data. Settings
  live at `~/.config/meshtasticpass/config.json`, CHAT history at
  `~/.local/share/meshtasticpass/chat.db`. See `PRIVACY.md` for every path.

## Hard rules

These are project doctrine, and several are enforced by tests:

1. **Truthful data only.** Never fabricate hop counts, delivery confirmations,
   presence, acknowledgements, bearings, distances, or origin timestamps that
   the available radio data cannot support. `hopsAway` is a proximity count,
   not a route; `rxTime` is receiver-side, never a send time. When data is
   missing, show it as missing -- do not estimate from RSSI, SNR, arrival
   order, or node ID.
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
mesh_topology.py            MESH grid layout and arrow navigation (pure, no I/O)
geo.py                      Position validation, Haversine distance, bearing, mile formatting
grapheme_text.py            Grapheme-cluster-safe width, truncation, wrapping
node_activity.py            Five-minute activity semantics (firmware-source-verified threshold)
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
  either service via `create_radio_service(...)` and never branches on
  "is this simulation?" elsewhere. Simulation is deterministic by design and
  deliberately encodes edge cases (an ACTIVE count that ages at an exact
  boundary, an out-of-order packet pair, a node with no position). When adding
  a feature to `RadioService`, add the matching deterministic behavior to the
  simulator so the feature is reviewable without hardware.
- **Pure logic lives outside `app.py`.** `app.py` is a single large file;
  the project's answer to that is to push rules and computation into small,
  independently tested modules (`mesh_state`, `mesh_topology`, `geo`,
  `grapheme_text`, `node_activity`, `message_time`, `relative_time`). Follow
  that split for new logic -- `app.py` should render and route input.
- **`chat_store.py` owns SQLite.** Versioned schema with in-place migrations;
  it never deletes or silently replaces a malformed database, and history
  paging uses stable ID cursors, never OFFSET. A failed history write surfaces
  a CHAT error while the radio/UI keeps running.
- **`theme_palette.py` is the single color source.** Widgets consume semantic
  tokens (BASE, ACCENT, DIM, ERROR, ...); never hard-code a theme-specific
  literal color, and never alias one semantic token to another.

## Code conventions

- `from __future__ import annotations` at the top of every module; full type
  annotations on function signatures (including `-> None` on tests).
- `@dataclass(frozen=True)` for value types; `Enum` for state machines;
  `snake_case` functions, `CamelCase` classes, `SCREAMING_CASE` constants,
  leading `_` for private helpers.
- Every file starts with a module docstring whose first line is one sentence.
  Docstrings record *why* -- firmware/SDK citations, reproduced bugs, and
  rejected alternatives are welcome and expected.
- Use `--` for dashes in Python source, not the Unicode em dash character.
- Tests: `tests/test_<module>.py`, classes named `<Thing>Tests(unittest.TestCase)`,
  ending with `if __name__ == "__main__": unittest.main()`. UI behavior is
  tested with Textual's pilot harness (`IsolatedAsyncioTestCase` +
  `app.run_test()`). Tests are behavior-focused and thorough; a change to
  user-visible behavior needs a test that describes that behavior.

## Firmware/SDK facts to respect (pinned 2.7.11)

- Long Name allows 39 UTF-8 payload bytes; Short Name allows 4 (nanopb
  `max_size: 5`). Validate by encoded byte size app-side before the SDK can
  truncate.
- Delivery states are `SENDING / SENT / HEARD / UNCONFIRMED / FAILED`;
  `SENDING` and `SENT` intentionally display the same. `HEARD` on broadcast is
  implicit mesh evidence, not a read receipt.
- `hop_limit` protocol range is 0-7.
- Firmware has no reliable persisted inbox; the phone queue is small,
  volatile, and drained through the normal receive callback with no replay
  marker.

## Gotchas

- **README staleness.** The README documents milestones as they were built;
  some sections lag the code (e.g., theme names are now SNOW/AMBER with a
  legacy WHITE/GREEN/ORANGE migration, and DM exists as a mode inside CHAT).
  Verify current behavior in the code before relying on a README claim.
- **`inspect_chat_store.py` bypasses `ChatStore.open()` on purpose** --
  `open()` rewrites abandoned `SENDING` rows as a side effect, which would
  destroy the evidence the tool exists to display. Keep it read-only.
- **The `*_probe.py` scripts are labeled TEMPORARY diagnostics.** They are run
  by hand, are not imported by `app.py`, and have strict self-documented
  limits on what they may touch.
- **`config.json` keys are backward-compatible.** For example the `font_size`
  key persists even though the UI now calls it UI SCALE; defaults are
  fallbacks, never overwrites of an existing user choice.
- The launcher assumes labwc + lxterminal and edits `rc.xml` only inside its
  marked block; `tests/test_launcher_installer.py` runs it twice against a
  temporary HOME to prove idempotence.

## Git workflow

- Default branch: `main`. Work on kebab-case topic branches; keep unrelated
  changes out of the branch. Integration is via pull request.
- Commit subjects are imperative and specific; bodies explain the symptom,
  the mechanism, and the tests added (many commits also record the full-suite
  result).
- Pull requests must explain user-visible behavior, tests, hardware
  assumptions, and any radio traffic the change can generate.
