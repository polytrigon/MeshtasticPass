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

from typing import Callable, Iterable, Mapping

from grapheme_text import cell_len, grapheme_clusters


# Per-grapheme wait for the terminal's reply. Generous for a local
# terminal answering from its own event loop, small enough that a
# terminal which never answers costs one of these and not one per
# grapheme -- measurement stops at the first silence (see below).
REPLY_TIMEOUT_SECONDS = 0.35

# Ceiling on how many distinct graphemes one startup will measure. A
# board of a few hundred nodes rarely holds more than a couple of dozen
# distinct emoji, and an unbounded loop here would be a startup delay
# nobody asked for.
MEASUREMENT_LIMIT = 64


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


def measure_terminal(names: Iterable[str]) -> PaintedWidths:
    """Measure this process's real terminal, or return the empty result.

    Every failure path lands on an empty PaintedWidths, which behaves
    exactly like cell_len -- so a caller can call this at startup and use
    the result without ever asking whether it worked. Never raises.
    """
    graphemes = distinct_graphemes(names)
    if not graphemes:
        return PaintedWidths()
    try:
        return PaintedWidths(_measure_on_tty(graphemes))
    except Exception:
        return PaintedWidths()


def _measure_on_tty(graphemes: tuple[str, ...]) -> dict[str, int]:
    import os
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
    # Hidden throughout: this paints real characters on the user's shell
    # line for a few milliseconds before Textual takes the screen, and a
    # cursor jumping around them is the part that would be noticed.
    write("\x1b[?25l")
    try:
        tty.setraw(stdin.fileno())
        # A stray keypress still in the buffer would be read as part of
        # the first reply and turn one measurement into nonsense.
        termios.tcflush(stdin, termios.TCIFLUSH)
        return measure_painted_widths(graphemes, write=write, read_reply=read_reply)
    finally:
        termios.tcsetattr(stdin, termios.TCSADRAIN, saved)
        write("\r\x1b[K\x1b[?25h")
