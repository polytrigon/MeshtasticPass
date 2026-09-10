"""Re-asking for config when the device never answers the first request.

Measured on hardware: the first connect attempt after process start
receives exactly ONE unsolicited node_info in 33 seconds and nothing
else. The port is open and the device is transmitting -- it simply never
answers `want_config_id`. The attempt six seconds later pulls 95
node_info and the whole config in three. Waiting two minutes between
runs changes nothing, so it is not the port settling.

That is the signature of the request write being lost on a freshly
opened CDC endpoint. The repair is to ask again inside the same attempt,
which is what these tests pin.

`_traced_interface` builds its subclass over whatever class it is given,
so the retry can be exercised against a stand-in with no radio, no
serial port and no SDK -- the logic is what is being tested, not the
Meshtastic package.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_service import (  # noqa: E402
    CONFIG_REQUEST_INTERVAL_SECONDS,
    _traced_interface,
)


class Timeout(Exception):
    """Stands in for the SDK's own timeout error."""


class FakeInterface:
    """The two methods the retry drives, and nothing else.

    `answers_on` is which ask finally gets a reply: 1 means the device
    answered the original request, 2 means it took one re-ask, and None
    means it never answers at all.
    """

    def __init__(self, answers_on: int | None = 2) -> None:
        self.answers_on = answers_on
        self.asks = 1  # _startConfig has already run once by now
        self.waits: list[float] = []
        self.failure = None

    def _waitConnected(self, timeout: float = 30.0):
        self.waits.append(timeout)
        if self.answers_on is not None and self.asks >= self.answers_on:
            return "connected"
        raise Timeout("Timed out waiting for connection completion")

    def _startConfig(self) -> None:
        self.asks += 1


def build(answers_on: int | None, budget: float) -> FakeInterface:
    traced = _traced_interface(FakeInterface, budget)
    interface = traced.__new__(traced)
    FakeInterface.__init__(interface, answers_on)
    return interface


class ConfigRequestRetryTests(unittest.TestCase):
    # ---- the repair ---------------------------------------------------

    def test_an_unanswered_request_is_asked_again(self) -> None:
        interface = build(answers_on=2, budget=8.0)

        self.assertEqual(interface._waitConnected(), "connected")
        self.assertEqual(interface.asks, 2)

    def test_it_keeps_asking_within_the_budget(self) -> None:
        """One re-ask is not a rule, it is just what today needed."""
        interface = build(answers_on=4, budget=30.0)

        self.assertEqual(interface._waitConnected(), "connected")
        self.assertEqual(interface.asks, 4)

    def test_an_answered_request_is_never_repeated(self) -> None:
        """A working connect must not be disturbed.

        _startConfig clears myInfo and the node maps, so an unnecessary
        re-ask would throw away a download that was already arriving.
        """
        interface = build(answers_on=1, budget=8.0)

        self.assertEqual(interface._waitConnected(), "connected")
        self.assertEqual(interface.asks, 1)

    # ---- the budget still bounds it ------------------------------------

    def test_it_gives_up_at_the_deadline(self) -> None:
        """A device that never answers must still fail, and on time."""
        interface = build(answers_on=None, budget=1.0)

        with self.assertRaises(Timeout):
            interface._waitConnected()

    def test_no_single_wait_exceeds_the_ask_interval(self) -> None:
        """Otherwise a lost request is stared at for the whole budget."""
        interface = build(answers_on=None, budget=2.0)

        with self.assertRaises(Timeout):
            interface._waitConnected()

        self.assertTrue(interface.waits)
        for wait in interface.waits:
            self.assertLessEqual(wait, CONFIG_REQUEST_INTERVAL_SECONDS)

    def test_a_short_budget_still_gets_one_ask(self) -> None:
        """Never zero attempts, however tight the budget."""
        interface = build(answers_on=1, budget=0.0)

        self.assertEqual(interface._waitConnected(), "connected")
        self.assertEqual(len(interface.waits), 1)

    # ---- a real failure is not a lost request ---------------------------

    def test_a_reported_device_failure_is_not_retried(self) -> None:
        """Re-asking would bury the real error under a timeout.

        The SDK sets `failure` when the device itself reported a
        problem. That is not a dropped write, and hammering it with
        fresh config requests would replace a useful message with a
        generic timeout at the end of the budget.
        """
        interface = build(answers_on=None, budget=30.0)
        interface.failure = RuntimeError("device said no")

        with self.assertRaises(Timeout):
            interface._waitConnected()

        self.assertEqual(interface.asks, 1, "must not re-ask a real failure")
        self.assertEqual(len(interface.waits), 1)


if __name__ == "__main__":
    unittest.main()
