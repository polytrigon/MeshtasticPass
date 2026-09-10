#!/usr/bin/env python3
"""Report what THIS terminal paints, for diagnosis.

The app measures its own terminal at startup now (see terminal_width),
so nothing depends on anyone running this. It exists to SHOW the numbers
when a board still looks wrong: which glyphs this font disagrees with
Rich about, and by how much.

MUST BE RUN IN THE UCONSOLE'S OWN TERMINAL, not over SSH. The painting is
done by whichever terminal emulator and font draws the pixels, so a run
inside an SSH session reports the machine you connected FROM -- which is
how an earlier round of this concluded, wrongly, that nothing was
diverging at all.

Usage:
    .venv/bin/python terminal_width_probe.py [output-file]

Run it from the virtual environment, not system Python -- it needs the
same rich this app runs against, and a bare `python3` typically has no
rich at all.

Prints a table and writes the same table to `output-file` (default
~/terminal_width_probe.txt) so it can be read back over SSH.
"""

from __future__ import annotations

import os
import sys


# A standard battery: two anchors whose answers are known, so a terminal
# that reports nonsense is obvious, then the categories that diverge.
STANDARD_PROBES: tuple[tuple[str, str], ...] = (
    ("A", "plain ASCII (anchor: must be 1)"),
    ("中", "CJK ideograph (anchor: usually 2)"),
    ("·", "MIDDLE DOT (East_Asian_Width=AMBIGUOUS)"),
    ("…", "HORIZONTAL ELLIPSIS (AMBIGUOUS)"),
    ("∴", "THEREFORE (AMBIGUOUS; seen in a real node name)"),
    ("\U0001f33d", "EAR OF CORN (older emoji, usually covered)"),
    ("\U0001f9a6", "OTTER (Unicode 11)"),
    ("\U0001f9cc", "TROLL (Unicode 14)"),
    ("\U0001fac8", "unassigned/newer plane-1 codepoint"),
    ("❤️", "HEAVY BLACK HEART + VS16"),
    ("5️⃣", "KEYCAP DIGIT FIVE (already substituted in CHAT)"),
    ("\U0001f1fa\U0001f1f8", "REGIONAL INDICATOR PAIR (flag)"),
    ("\U0001f468‍\U0001f469‍\U0001f466", "ZWJ family sequence"),
    ("\U0001f44d\U0001f3fd", "THUMBS UP + skin-tone modifier"),
)


def names_in_use(limit: int = 40) -> list[tuple[str, str]]:
    """Whole PASSES cells from the local database, with their node IDs.

    The per-glyph table says which glyph is wrong. This says which ROW
    drifts, which is what is visible on screen: a cell's painted width is
    the stride to the next column, so any name whose painted width
    differs from its declared width moves everything to its right.
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


def _table(
    heading: str, rows: list[tuple[str, int, int | None, str]], label_width: int
) -> list[str]:
    lines = [
        "",
        heading,
        "",
        f"{'what':<{label_width}}{'rich':>5}{'painted':>9}  note",
        "-" * 78,
    ]
    divergent = 0
    for shown, declared, painted, note in rows:
        answer = "no answer" if painted is None else str(painted)
        flag = ""
        if painted is not None and painted != declared:
            flag = f"  <-- DIVERGES {painted - declared:+d}"
            divergent += 1
        lines.append(f"{shown:<{label_width}}{declared:>5}{answer:>9}  {note}{flag}")
    lines.append("-" * 78)
    lines.append(f"{divergent} of {len(rows)} diverge.")
    return lines


def main() -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Not a terminal. Run this IN the uConsole's terminal, not piped.")
        return 2

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from grapheme_text import cell_len
        from terminal_width import measure_terminal
    except Exception:
        print(
            "Could not import rich. Run this with .venv/bin/python from the "
            "MeshtasticPass checkout, not system python3."
        )
        return 2

    names = names_in_use()
    # One measurement pass over everything, through the SAME code the app
    # runs at startup -- a probe with its own private implementation
    # could agree with itself and disagree with the app.
    subjects = [grapheme for grapheme, _ in STANDARD_PROBES] + [
        name for name, _ in names
    ]
    widths = measure_terminal(subjects)

    def measured(text: str) -> int | None:
        painted = widths.width(text)
        return painted if widths else None

    lines = [
        f"TERM={os.environ.get('TERM', '?')}  "
        f"COLORTERM={os.environ.get('COLORTERM', '?')}",
        "",
        "If TERM says ghostty/iterm/vte and you reached this over SSH, this",
        "report is about the machine you connected FROM, not the uConsole.",
    ]
    lines += _table(
        "GLYPHS",
        [
            (" ".join(f"U+{ord(c):04X}" for c in grapheme), cell_len(grapheme),
             measured(grapheme), note)
            for grapheme, note in STANDARD_PROBES
        ],
        label_width=34,
    )
    if names:
        lines += _table(
            "PASSES CELLS -- a name whose two widths differ moves every column\n"
            "to its right on that row.",
            [(name, cell_len(name), measured(name), node_id) for name, node_id in names],
            label_width=24,
        )
    corrections = widths.corrections
    lines += ["", f"Corrections the app will apply on this terminal: {len(corrections)}"]
    for grapheme, painted in corrections.items():
        codepoints = " ".join(f"U+{ord(c):04X}" for c in grapheme)
        lines.append(f"  {codepoints}  rich {cell_len(grapheme)} -> painted {painted}")

    report = "\n".join(lines)
    print(report)
    destination = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.expanduser("~/terminal_width_probe.txt")
    )
    try:
        with open(destination, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
        print(f"\nWritten to {destination}")
    except OSError as error:
        print(f"\nCould not write {destination}: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
