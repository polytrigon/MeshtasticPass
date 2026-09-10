"""Detecting the radio's stored backlog draining into CHAT.

A Meshtastic radio keeps receiving while nothing is attached to it, and
hands what it collected to the next client that connects: the SDK sends
`want_config_id`, the device replays, then `config_complete_id`. So the
first thing a person sees after opening the app on a radio left running
overnight is a burst of messages, arriving faster than anyone can read,
with no indication that this is history rather than a very busy mesh.

This decides -- as pure functions, so the rule can be tested without an
app -- which arrivals are that backlog, and when it has finished.

WHAT MAKES A MESSAGE BANKED

Its `radio_rx_at` (the radio's own rxTime for the packet) is earlier
than the moment this session attached. The radio had it before we did;
that is the whole definition, and it needs no burst-detection
heuristics or guesses about timing.

Two deliberate softenings:

`BACKLOG_CLOCK_TOLERANCE_SECONDS` -- a radio's clock is not the host's,
and CLOCK SYNC exists precisely because they drift. A message a second
or two "before" connection is far more likely to be a live one seen
through a slightly slow radio clock than a banked one, and calling a
live message history is the worse error: it puts a "while you were
away" label on a conversation happening right now.

A missing `radio_rx_at` is never banked. Without a timestamp there is
no evidence, and the honest default is to treat it as live.

WHEN IT IS OVER

After `BACKLOG_QUIET_SECONDS` with no further banked arrival. There is
no end-of-backlog marker in the protocol that reaches this layer, and
waiting for a quiet moment is what a person watching the screen would
do anyway. Live messages arriving during the drain do not extend it:
they are not what is being waited for.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


# See the module docstring: how far before the connection instant a
# message may claim to have arrived and still count as live.
BACKLOG_CLOCK_TOLERANCE_SECONDS = 5.0

# How long without a banked arrival before the drain is called finished.
# Long enough to bridge the gap between two replayed packets on a slow
# serial link; short enough that the indicator does not linger over a
# transcript that has already settled.
BACKLOG_QUIET_SECONDS = 2.0


@dataclass(frozen=True)
class Backlog:
    """How much history has arrived, and whether more is still coming."""

    count: int = 0
    last_arrival: float | None = None

    @property
    def draining(self) -> bool:
        """Whether an indicator should currently be on screen."""
        return self.count > 0 and self.last_arrival is not None


def is_banked(
    radio_rx_at: float | None,
    connected_at: float | None,
    tolerance: float = BACKLOG_CLOCK_TOLERANCE_SECONDS,
) -> bool:
    """Whether the radio was already holding this message when we attached."""
    if connected_at is None:
        return False
    if not isinstance(radio_rx_at, (int, float)) or isinstance(radio_rx_at, bool):
        return False
    return float(radio_rx_at) < connected_at - tolerance


def record_banked(backlog: Backlog, arrived_at: float) -> Backlog:
    """Count one banked message and hold the drain open."""
    return replace(backlog, count=backlog.count + 1, last_arrival=arrived_at)


def settle(
    backlog: Backlog, now: float, quiet: float = BACKLOG_QUIET_SECONDS
) -> Backlog:
    """End the drain once it has been quiet long enough.

    Returns a FINISHED backlog rather than an empty one: the count is
    what the caller reports, and clearing it here would leave nothing to
    say at the moment there is finally something worth saying.
    """
    if backlog.last_arrival is None:
        return backlog
    if now - backlog.last_arrival < quiet:
        return backlog
    return replace(backlog, last_arrival=None)


def backlog_label(count: int) -> str:
    """What the indicator says. Singular is worth the branch here.

    Names the RADIO as the source, because the distinction being drawn
    is that these were collected while the app was closed -- "receiving"
    alone would read as ordinary live traffic, which is exactly the
    confusion this exists to remove.
    """
    if count == 1:
        return "1 MESSAGE FROM THE RADIO"
    return f"{count} MESSAGES FROM THE RADIO"
