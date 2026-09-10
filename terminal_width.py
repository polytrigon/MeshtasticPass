"""What THIS terminal actually paints, measured rather than assumed.

Every fixed-width layout in this app pads text using rich.cells.cell_len,
which reports the width Unicode DECLARES a character to be. A terminal
advances the width its FONT actually has. Those agree for ASCII, for CJK,
and for any emoji the terminal has a real glyph for. They disagree when
the terminal has NO glyph and falls back to a text font: Rich accounts
for two columns, the terminal advances one, and every character after it
on that line is painted one column left of where the app put it. The
error accumulates along the row, so by the last column a board can be
several cells out.

Nothing inside the app can detect that. Rich is self-consistent, so a
test that measures with cell_len and pads with cell_len passes while the
screen is visibly wrong -- which is why this went unfixed through several
rounds of reasoning about it. The only way to know is to ask the terminal.

So this asks. It prints one grapheme at column 1 and issues ESC[6n, the
standard Device Status Report; the column the terminal reports back is
how many cells it really advanced. That has to happen BEFORE Textual
takes the screen, so it runs once at startup (see app.main) against the
graphemes actually about to be displayed.

Everything here degrades to today's behaviour rather than to an error: a
terminal that does not answer, a non-tty (a test, a pipe), a
platform without termios -- each yields no measurements at all, and
PaintedWidths then reports exactly what cell_len does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from grapheme_text import cell_len, grapheme_clusters


# U+FE0F. Rich does not measure a sequence ending in this by looking the
# sequence up; it measures the BASE character and then adds one if that
# base is in the cell table's `narrow_to_wide` set (see rich.cells.
# _cell_len). So a terminal that ignores the promotion is corrected by
# removing the base from that set, never by a per-character width.
VARIATION_SELECTOR_16 = "\ufe0f"


# Per-grapheme wait for the terminal's reply. Generous for a local
# terminal answering from its own event loop, small enough that a
# terminal which never answers costs one of these and not one per
# grapheme -- measurement stops at the first silence (see below).
REPLY_TIMEOUT_SECONDS = 0.35

# Ceiling on how many distinct graphemes one startup will measure. A
# board of a few hundred nodes rarely holds more than a couple of dozen
# distinct emoji, and an unbounded loop here would be a startup delay
# nobody asked for.
MEASUREMENT_LIMIT = 96


class PaintedWidths:
    """Display widths, measured where known and declared where not.

    The empty instance is the identity: width() is then cell_len exactly,
    which is what every caller gets when measurement was impossible. So a
    caller can use this unconditionally and never branch on whether the
    terminal cooperated.
    """

    def __init__(self, measured: Mapping[str, int] | None = None) -> None:
        self._measured = {
            grapheme: width
            for grapheme, width in (measured or {}).items()
            if isinstance(width, int) and width >= 0
        }

    def width(self, text: str) -> int:
        """Columns this terminal will advance for `text`."""
        total = 0
        for cluster in grapheme_clusters(text):
            measured = self._measured.get(cluster)
            total += cell_len(cluster) if measured is None else measured
        return total

    @property
    def measured(self) -> dict[str, int]:
        """Everything measured, agreements included.

        `corrections` is the interesting subset; this is what a cache
        should keep, so an agreeing glyph is not re-probed every launch.
        """
        return dict(self._measured)

    @property
    def corrections(self) -> dict[str, int]:
        """Only the graphemes the terminal DISAGREES with Rich about.

        The interesting set, and usually a short one: it is what a bug
        report should carry, and an empty dict means this terminal paints
        everything at its declared width.
        """
        return {
            grapheme: width
            for grapheme, width in self._measured.items()
            if width != cell_len(grapheme)
        }

    def __bool__(self) -> bool:
        return bool(self._measured)


def distinct_graphemes(names: Iterable[str], limit: int = MEASUREMENT_LIMIT) -> tuple[str, ...]:
    """Non-ASCII grapheme clusters worth measuring, in first-seen order.

    ASCII is skipped because no terminal disagrees about it, and because
    measuring it would spend the whole budget before reaching the emoji
    that are the entire reason for doing this.
    """
    seen: dict[str, None] = {}
    for name in names:
        if not name:
            continue
        for cluster in grapheme_clusters(name):
            if cluster.isascii() or cluster in seen:
                continue
            seen[cluster] = None
            if len(seen) >= limit:
                return tuple(seen)
    return tuple(seen)


def measure_painted_widths(
    graphemes: Iterable[str],
    *,
    write: Callable[[str], None],
    read_reply: Callable[[], str | None],
) -> dict[str, int]:
    """Measure each grapheme by asking the terminal where the cursor went.

    `write` emits to the terminal and `read_reply` returns one complete
    ESC[...R Device Status Report (or None on timeout). Both are injected
    so this can be tested against a scripted terminal -- the arithmetic
    and the give-up rule are the parts that can be wrong, and neither
    needs a real tty to exercise.

    Stops at the FIRST unanswered query. A terminal that does not support
    DSR will not start supporting it on the fourth try, and the
    alternative is one timeout per grapheme on every startup.
    """
    measured: dict[str, int] = {}
    for grapheme in graphemes:
        # Carriage return rather than an absolute move, so this is
        # correct wherever on the screen it happens to run.
        write(f"\r{grapheme}\x1b[6n")
        reply = read_reply()
        if reply is None:
            break
        column = _column_from_report(reply)
        write("\r\x1b[K")
        if column is None:
            continue
        # ESC [ row ; col R is 1-based, so a grapheme that advanced the
        # cursor by N leaves it at column N + 1.
        measured[grapheme] = max(0, column - 1)
    return measured


def _column_from_report(reply: str) -> int | None:
    """The column out of an ESC[row;colR report, or None if it is not one."""
    if not reply.endswith("R") or ";" not in reply:
        return None
    try:
        return int(reply[:-1].split(";")[-1])
    except ValueError:
        return None


def cache_path() -> "Path":
    """Where measurements are remembered between launches.

    Beside the CHAT database rather than in the settings file: this is a
    fact about the hardware, not a preference the user chose, and it
    should not travel if a config is copied to another machine.
    """
    from pathlib import Path

    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local/share"
    return root / "meshtasticpass" / "terminal_widths.json"


def _terminal_key() -> str:
    return os.environ.get("TERM", "?")


def load_cached_widths() -> dict[str, int]:
    """Previously measured widths for this TERM, or nothing.

    Keyed by TERM, which is a proxy rather than a guarantee: the same
    TERM with a different FONT has different glyph coverage. Deleting
    this file, or running terminal_width_probe.py, re-measures.
    """
    import json

    try:
        with open(cache_path(), encoding="utf-8") as handle:
            stored = json.load(handle)
        widths = stored.get(_terminal_key(), {})
        return {
            grapheme: width
            for grapheme, width in widths.items()
            if isinstance(width, int) and width >= 0
        }
    except Exception:
        return {}


def save_cached_widths(widths: Mapping[str, int]) -> None:
    """Remember measurements for this TERM. Best effort, never raises."""
    import json

    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except Exception:
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored[_terminal_key()] = dict(widths)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(stored, handle, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception:
        return


def measure_terminal(
    names: Iterable[str], *, use_cache: bool = True
) -> PaintedWidths:
    """Measure this process's real terminal, or return the empty result.

    Measures only what is NOT already known for this terminal. Probing
    paints each glyph on the screen for as long as the reply takes, so a
    board of emoji names visibly flickered on every launch; remembering
    the answers makes that a first-run cost, and a new node name costs
    only its own new glyphs.

    Every failure path lands on an empty PaintedWidths, which behaves
    exactly like cell_len -- so a caller can call this at startup and use
    the result without ever asking whether it worked. Never raises.
    """
    graphemes = distinct_graphemes(names)
    if not graphemes:
        return PaintedWidths()
    known = load_cached_widths() if use_cache else {}
    missing = tuple(g for g in graphemes if g not in known)
    if missing:
        try:
            fresh = _measure_on_tty(missing)
        except Exception:
            fresh = {}
        if fresh:
            known = {**known, **fresh}
            save_cached_widths(known)
    return PaintedWidths({g: known[g] for g in graphemes if g in known})


def _measure_on_tty(graphemes: tuple[str, ...]) -> dict[str, int]:
    import select
    import sys
    import termios
    import tty

    stdin = sys.__stdin__
    stdout = sys.__stdout__
    if stdin is None or stdout is None:
        return {}
    if not stdin.isatty() or not stdout.isatty():
        return {}

    def write(text: str) -> None:
        stdout.write(text)
        stdout.flush()

    def read_reply() -> str | None:
        reply = ""
        while not reply.endswith("R"):
            ready, _, _ = select.select([stdin], [], [], REPLY_TIMEOUT_SECONDS)
            if not ready:
                return None
            reply += os.read(stdin.fileno(), 32).decode("utf-8", "replace")
            if len(reply) > 64:
                return None
        return reply[reply.rfind("\x1b[") + 2 :] if "\x1b[" in reply else reply

    saved = termios.tcgetattr(stdin)
    # On the ALTERNATE SCREEN, with the cursor hidden. Probing has to
    # paint each glyph to find out how wide it is, and doing that on the
    # user's own shell line meant watching a stream of emoji flicker
    # past on every launch. The alternate screen is discarded on exit
    # and the shell scrollback is restored untouched -- and Textual is
    # about to switch to it anyway, so there is nothing to see.
    write("\x1b[?1049h\x1b[?25l")
    try:
        tty.setraw(stdin.fileno())
        # A stray keypress still in the buffer would be read as part of
        # the first reply and turn one measurement into nonsense.
        termios.tcflush(stdin, termios.TCIFLUSH)
        return measure_painted_widths(graphemes, write=write, read_reply=read_reply)
    finally:
        termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
        write("\r\x1b[K\x1b[?1049l\x1b[?25h")


@dataclass(frozen=True)
class WidthCorrections:
    """What this terminal disagrees with Rich about, in Rich's own terms.

    Rich computes every width through two mechanisms and they need
    different corrections, which is why this is not simply a dict:

    `per_character` is for characters measured directly -- the ordinary
    case, and the one that covers an emoji the font has no glyph for.

    `unpromoted_bases` is for VARIATION SELECTOR-16 sequences. Rich never
    looks such a sequence up as a unit: it measures the base character
    and adds one if that base is in the cell table's narrow_to_wide set.
    A terminal that does not honour the promotion is corrected by
    removing the base from the set.

    `unexpressible` records measured disagreements that neither
    mechanism can carry -- a ZWJ sequence the font renders at an
    unexpected width, say. They are left alone rather than approximated,
    and named so a bug report can say so.
    """

    per_character: Mapping[int, int]
    unpromoted_bases: frozenset[str]
    unexpressible: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.per_character or self.unpromoted_bases)


def plan_corrections(painted: PaintedWidths) -> WidthCorrections:
    """Translate measurements into the corrections Rich can accept.

    MUST run before install_terminal_widths: it compares each
    measurement against what Rich currently believes, and once Rich has
    been corrected there is nothing left to disagree with.
    """
    per_character: dict[int, int] = {}
    unpromoted: set[str] = set()
    unexpressible: list[str] = []
    for grapheme, measured in sorted(painted.corrections.items()):
        if len(grapheme) == 1:
            per_character[ord(grapheme)] = measured
        elif VARIATION_SELECTOR_16 in grapheme and measured < cell_len(grapheme):
            # The promotion is what this terminal is not doing.
            unpromoted.add(grapheme[0])
        else:
            unexpressible.append(grapheme)
    return WidthCorrections(per_character, frozenset(unpromoted), tuple(unexpressible))


def install_terminal_widths(corrections: WidthCorrections) -> bool:
    """Make Rich itself measure the way this terminal paints.

    Everything Textual lays out -- where CHAT wraps a message, how tall
    the transcript thinks it is, where the scrollbar's thumb goes, how
    wide a panel border is drawn -- is computed from rich.cells.cell_len.
    Correcting our own layout code only fixes our own grids; correcting
    Rich fixes all of it, for every glyph the terminal disagrees about,
    including ones nobody has thought to hard-code a workaround for.

    Both patches go on rich.cells module attributes rather than on the
    functions callers hold. That is deliberate and it is what makes this
    work at all: a dozen Rich modules do `from .cells import cell_len` at
    import time, so patching cell_len itself would miss them -- but
    cell_len's implementation resolves BOTH get_character_cell_size and
    load_cell_table as bare names in rich.cells' own globals at CALL
    time, so patching those reaches every caller no matter how it
    imported anything. Verified against Rich's real source, including
    rich.text's by-value copy.

    Idempotent, and a no-op when there is nothing to correct -- so an
    unmeasurable terminal leaves Rich exactly as it found it.
    """
    if not corrections:
        return False
    try:
        import functools

        import rich.cells as cells
    except Exception:
        return False
    if getattr(cells.get_character_cell_size, "_meshtasticpass_corrected", False):
        return False
    try:
        original_size = cells.get_character_cell_size
        original_load = cells.load_cell_table
        per_character = dict(corrections.per_character)
        unpromoted = corrections.unpromoted_bases

        @functools.lru_cache(maxsize=4096)
        def corrected_size(character: str, unicode_version: str = "auto") -> int:
            width = per_character.get(ord(character))
            if width is None:
                return original_size(character, unicode_version)
            return width

        corrected_size._meshtasticpass_corrected = True  # type: ignore[attr-defined]

        @functools.lru_cache(maxsize=32)
        def corrected_load(unicode_version: str = "auto"):
            table = original_load(unicode_version)
            if not unpromoted:
                return table
            return table._replace(
                narrow_to_wide=table.narrow_to_wide - unpromoted
            )

        cells.get_character_cell_size = corrected_size
        cells.load_cell_table = corrected_load
        # Rich memoises both the per-character size and whole-string
        # lengths. Anything measured before this point is now wrong.
        original_size.cache_clear()
        cells.cached_cell_len.cache_clear()
    except Exception:
        return False
    return True
