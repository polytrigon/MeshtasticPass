#!/usr/bin/env python3
"""Measure what THIS terminal actually paints, versus what Rich accounts for.

MUST BE RUN IN THE UCONSOLE'S OWN TERMINAL, not over SSH. The painting is
done by whichever terminal emulator and font is drawing the pixels, so a
run inside an SSH session measures the machine you SSH'd FROM.

Why this exists
---------------
Every fixed-width layout in this app -- the PASSES grid, MESH's board,
CHAT's scrollbar -- pads text using rich.cells.cell_len, which reports the
width Unicode DECLARES a character to be. A terminal paints the width its
FONT actually has. When those disagree, everything drawn after the
character on that line slides sideways by the difference, and no amount of
careful padding inside the app can fix it: the app is padding in units the
terminal is not using.

There is no way to fix a divergence you have not measured, and no way to
measure it from inside a running Textual app -- Textual owns the screen.
So: this probe prints one grapheme at a time at column 1, asks the
terminal where the cursor ended up (the standard ESC[6n Device Status
Report), and records the answer. The number that comes back is ground
truth for this hardware.

Usage
-----
    python3 terminal_width_probe.py [output-file]

Prints a table and writes the same table to `output-file`
(default ~/terminal_width_probe.txt) so it can be read back over SSH.

By default it probes a standard battery PLUS every distinct grapheme in
the short/long names already recorded in the local CHAT database -- the
names that are actually on screen when the columns drift, rather than a
guess about which emoji people chose.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty


PROBE_TIMEOUT_SECONDS = 0.5

# A standard battery: one known-narrow and one known-wide anchor so a
# terminal that answers nonsense is obvious, then the categories that
# actually diverge in practice.
STANDARD_PROBES: tuple[tuple[str, str], ...] = (
    ("A", "plain ASCII (anchor: must be 1)"),
    ("中", "CJK ideograph (anchor: usually 2)"),
    ("·", "MIDDLE DOT (the PASSES disambiguator)"),
    ("…", "HORIZONTAL ELLIPSIS (the truncation marker)"),
    ("\U0001f3c3", "RUNNER emoji"),
    ("\U0001f43b", "BEAR emoji"),
    ("\U0001f33d", "EAR OF CORN emoji"),
    ("\U0001f600", "GRINNING FACE emoji"),
    ("❤️", "HEAVY BLACK HEART + VS16"),
    ("❤", "HEAVY BLACK HEART, unqualified"),
    ("5️⃣", "KEYCAP DIGIT FIVE (already substituted in CHAT)"),
    ("\U0001f1fa\U0001f1f8", "REGIONAL INDICATOR PAIR (flag)"),
    ("\U0001f468‍\U0001f469‍\U0001f466", "ZWJ family sequence"),
    ("\U0001f44d\U0001f3fd", "THUMBS UP + skin-tone modifier"),
)


def measure_painted_width(grapheme: str) -> int | None:
    """Paint `grapheme` at column 1 and ask the terminal where we ended up.

    Returns the number of columns the terminal ADVANCED, or None if it did
    not answer (not a real terminal, or one without DSR support).

    The cursor is homed with a carriage return rather than an absolute
    move so this works the same whatever line of the screen we are on, and
    the line is erased afterwards so the probe leaves no residue that a
    later measurement could be read against.
    """
    sys.stdout.write("\r")
    sys.stdout.write(grapheme)
    sys.stdout.write("\x1b[6n")
    sys.stdout.flush()

    response = ""
    while not response.endswith("R"):
        ready, _, _ = select.select([sys.stdin], [], [], PROBE_TIMEOUT_SECONDS)
        if not ready:
            return None
        response += os.read(sys.stdin.fileno(), 32).decode("utf-8", "replace")
        if len(response) > 64:
            return None

    # ESC [ rows ; cols R -- the column is 1-based, so a character that
    # advanced the cursor by N leaves it at column N+1.
    try:
        column = int(response.rstrip("R").split(";")[-1])
    except ValueError:
        return None
    sys.stdout.write("\r\x1b[K")
    sys.stdout.flush()
    return column - 1


def graphemes_in_use(limit: int = 40) -> list[tuple[str, str]]:
    """Distinct graphemes from names already in the local CHAT database.

    The point of probing what is on record rather than a fixed list: the
    columns drift because of the names this radio has actually met, and
    those are sitting in node_encounters right now.
    """
    try:
        from chat_store import ChatStore
        from grapheme_text import grapheme_clusters
    except Exception:
        return []
    try:
        # ChatStore.open() with no argument resolves the same default path
        # the app itself uses (see chat_store.default_chat_db_path), so
        # this probes the real database rather than a guessed location.
        rows = ChatStore.open().encounters()
    except Exception:
        return []
    seen: dict[str, str] = {}
    for row in rows:
        for name in (row.short_name, row.long_name):
            if not name:
                continue
            for cluster in grapheme_clusters(name):
                if cluster.isascii() or cluster in seen:
                    continue
                seen[cluster] = f"from {row.node_id} ({name!r})"
                if len(seen) >= limit:
                    return list(seen.items())
    return list(seen.items())


def names_in_use(limit: int = 40) -> list[tuple[str, str]]:
    """Whole PASSES cell names, exactly as the grid would lay them out.

    The per-grapheme table says which glyph is wrong. This says which
    ROW drifts, which is what is actually visible on screen: a cell's
    painted width is the stride to the next column, so any name whose
    painted width differs from its declared width moves everything to
    its right by that difference -- and a name whose two widths agree
    cannot move anything, however many emoji it contains.

    Uses display_name, which is exactly what a cell holds -- short name,
    then long name, then the node ID.
    """
    try:
        from chat_store import ChatStore
    except Exception:
        return []
    try:
        rows = ChatStore.open().encounters()
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for encounter in rows:
        name = encounter.display_name
        if name.isascii():
            continue
        out.append((name, encounter.node_id))
        if len(out) >= limit:
            break
    return out


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Not a terminal. Run this IN the uConsole's terminal, not piped.")
        return 2

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from rich.cells import cell_len
    except Exception:
        print("rich is not importable; run this from the MeshtasticPass checkout.")
        return 2

    probes = list(STANDARD_PROBES) + graphemes_in_use()
    names = names_in_use()
    destination = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
        "~/terminal_width_probe.txt"
    )

    settings = termios.tcgetattr(sys.stdin)
    results: list[tuple[str, int, int | None, str]] = []
    name_results: list[tuple[str, int, int | None, str]] = []
    try:
        tty.setraw(sys.stdin.fileno())
        # Anything already sitting in the input buffer (a stray keypress,
        # the newline that started this script) would be read as part of
        # the first DSR reply and turn one measurement into nonsense.
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
        for grapheme, label in probes:
            results.append(
                (grapheme, cell_len(grapheme), measure_painted_width(grapheme), label)
            )
        name_results = [
            (name, cell_len(name), measure_painted_width(name), node_id)
            for name, node_id in names
        ]
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, settings)
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()

    lines = [
        f"TERM={os.environ.get('TERM', '?')}  "
        f"COLORTERM={os.environ.get('COLORTERM', '?')}",
        "",
        f"{'codepoints':<34}{'rich':>5}{'painted':>9}  what it is",
        "-" * 78,
    ]
    divergent = 0
    for grapheme, declared, painted, label in results:
        codepoints = " ".join(f"U+{ord(c):04X}" for c in grapheme)
        shown = "no answer" if painted is None else str(painted)
        flag = ""
        if painted is not None and painted != declared:
            flag = "  <-- DIVERGES"
            divergent += 1
        lines.append(f"{codepoints:<34}{declared:>5}{shown:>9}  {label}{flag}")
    lines.append("-" * 78)
    lines.append(
        f"{divergent} of {len(results)} graphemes paint at a width Rich did not account for."
    )

    if name_results:
        lines += [
            "",
            "PASSES CELLS -- a name whose two widths differ moves every column",
            "to its right on that row. One whose widths agree cannot, no matter",
            "how many emoji it holds.",
            "",
            f"{'name':<24}{'rich':>5}{'painted':>9}  node",
            "-" * 78,
        ]
        drifting = 0
        for name, declared, painted, node_id in name_results:
            shown = "no answer" if painted is None else str(painted)
            flag = ""
            if painted is not None and painted != declared:
                flag = f"  <-- DRIFTS {painted - declared:+d}"
                drifting += 1
            lines.append(f"{name:<24}{declared:>5}{shown:>9}  {node_id}{flag}")
        lines.append("-" * 78)
        lines.append(f"{drifting} of {len(name_results)} names drift.")

    report = "\n".join(lines)
    print(report)
    try:
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
        print(f"\nWritten to {destination}")
    except OSError as error:
        print(f"\nCould not write {destination}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
