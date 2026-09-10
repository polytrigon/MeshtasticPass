"""The banked-message window: packets that arrive mid-connect.

When no client is attached, the firmware queues packets bound for one
(`MeshService::toPhoneQueue`, 8/16/32 slots by board). On reconnect it
replays them right after `config_complete_id` -- which is the moment
`SerialInterface.__init__` returns, so they land on the SDK's reader
thread while `connect()` is still blocked in the constructor and
`self._interface` has not been assigned.

`_on_text_received`'s stale-interface guard used to discard exactly
those. Live traffic (arriving seconds later, after the assignment) was
unaffected, so the app looked healthy while silently dropping every
message the user was away for. Whether the guard won is a pure race:
the field database shows two successful 8-hour drains, then ten days
and ~1,470 messages with a maximum receive lag of 31 seconds -- not one
banked message.

These tests hold that window open. The first fails on the code that
shipped for those ten days.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_service import (  # noqa: E402
    RadioState,
    CONNECT_TIMEOUT_SECONDS,
    FIRST_CONNECT_TIMEOUT_SECONDS,
    MAX_CONNECT_ARRIVALS,
    RadioService,
)

NODE_NUMBER = 0x12345678
SENDER_NUMBER = 0x9DAFA5EB


def make_interface() -> SimpleNamespace:
    """The small part of a Meshtastic interface RadioService touches."""
    return SimpleNamespace(
        myInfo=SimpleNamespace(my_node_num=NODE_NUMBER),
        localNode=object(),
        nodesByNum={
            NODE_NUMBER: {
                "user": {
                    "id": "!12345678",
                    "longName": "Test Node",
                    "shortName": "TEST",
                }
            },
            SENDER_NUMBER: {
                "user": {
                    "id": "!9dafa5eb",
                    "longName": "englshmffns",
                    "shortName": "mffn",
                }
            },
        },
        metadata=SimpleNamespace(firmware_version="2.7.0"),
        close=Mock(),
    )


def banked_packet(text: str, *, rx_time: float = 1_700_000_000.0) -> dict:
    """A replayed TEXT_MESSAGE_APP packet, carrying its ORIGINAL rxTime.

    rxTime older than the connection is what makes a packet identifiable
    as banked rather than live (see backlog.is_banked), so it has to
    survive the trip.
    """
    return {
        "from": SENDER_NUMBER,
        "fromId": "!9dafa5eb",
        "to": 0xFFFFFFFF,
        "id": 1236669885,
        "channel": 0,
        "rxTime": rx_time,
        "rxSnr": 7.25,
        "rxRssi": -47,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
    }


class ConnectWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RadioService("/dev/ttyUSB0")
        self.received = []
        self.service.add_message_handler(self.received.append)

    def _connect_delivering(self, packets, *, interface=None):
        """Connect, with `packets` published DURING the constructor.

        This is the whole point: the packets are handed to
        _on_text_received before _open_interface() returns, so
        self._interface is necessarily still unassigned -- reproducing
        the real race deterministically instead of hoping to lose it.
        """
        opened = make_interface() if interface is None else interface

        def open_interface():
            self.assertIsNone(
                self.service._interface,
                "precondition: the interface must not be assigned yet",
            )
            for packet in packets:
                self.service._on_text_received(packet=packet, interface=opened)
            return opened

        with (
            patch.object(self.service, "_check_device"),
            patch.object(self.service, "_open_interface", side_effect=open_interface),
        ):
            self.service.connect()
        return opened

    # ---- the regression ----------------------------------------------

    def test_a_packet_arriving_before_the_interface_is_assigned_survives(self) -> None:
        self._connect_delivering([banked_packet("Oooh whatcha got?")])

        self.assertEqual([m.text for m in self.received], ["Oooh whatcha got?"])

    def test_a_whole_backlog_drain_survives_in_order(self) -> None:
        """A real drain is a burst, not one packet.

        The observed 31 August drain delivered fourteen messages in four
        seconds; order is what makes a transcript readable.
        """
        texts = [f"message {index}" for index in range(14)]
        self._connect_delivering([banked_packet(text) for text in texts])

        self.assertEqual([m.text for m in self.received], texts)

    def test_the_original_receive_time_survives_the_hold(self) -> None:
        """Without rxTime the app cannot tell banked from live."""
        self._connect_delivering([banked_packet("hello", rx_time=1_699_999_000.0)])

        self.assertEqual(self.received[0].radio_rx_at, 1_699_999_000.0)

    def test_sender_identity_is_resolved_against_the_opened_interface(self) -> None:
        """Held packets must be parsed against the NEW node database.

        Resolving a held packet against a previous connection's nodes
        would attribute it to the wrong person, which is worse than
        dropping it.
        """
        self._connect_delivering([banked_packet("hello")])

        message = self.received[0]
        self.assertEqual(message.sender_node_id, "!9dafa5eb")
        self.assertEqual(message.sender_short_name, "mffn")

    # ---- the guard still has to guard --------------------------------

    def test_everything_held_during_a_connect_is_attributed_to_the_winner(self) -> None:
        """A held packet is judged by the connection that succeeded.

        Deliberate, and a change from the first version of this fix. The
        SDK's serial path waits 30s for config_complete; on a slow host
        with a large node DB the FIRST attempt times out while the radio
        is already replaying its backlog into it. Those packets arrive
        on an interface that is about to die. Insisting they match the
        final interface would discard exactly the backlog this exists to
        rescue -- and since delivery drains the radio destructively,
        nothing else holds a copy.
        """
        doomed = make_interface()
        opened = make_interface()

        def open_interface():
            self.service._on_text_received(
                packet=banked_packet("held by the attempt that timed out"),
                interface=doomed,
            )
            self.service._on_text_received(
                packet=banked_packet("held by the one that worked"), interface=opened
            )
            return opened

        with (
            patch.object(self.service, "_check_device"),
            patch.object(self.service, "_open_interface", side_effect=open_interface),
        ):
            self.service.connect()

        self.assertEqual(
            [m.text for m in self.received],
            ["held by the attempt that timed out", "held by the one that worked"],
        )

    def test_packets_held_by_a_failed_attempt_survive_into_the_next(self) -> None:
        """The real observed failure, end to end.

        12:10:39 LINK connecting / 12:11:12 LINK failed (timed out) /
        12:11:18 LINK connecting / 12:11:21 LINK online. The radio
        drained into the attempt that died. Before this, `connect()`
        raised before the interface was ever assigned, so the drain was
        never replayed and the retry met an empty queue.
        """
        opened = make_interface()
        attempts = []

        def open_interface():
            attempts.append(1)
            if len(attempts) == 1:
                self.service._on_text_received(
                    packet=banked_packet("banked overnight"), interface=make_interface()
                )
                raise OSError("Timed out waiting for connection completion")
            return opened

        with (
            patch.object(self.service, "_check_device"),
            patch.object(self.service, "_open_interface", side_effect=open_interface),
        ):
            with self.assertRaises(Exception):
                self.service.connect()
            self.assertEqual(self.received, [], "not deliverable until a connect wins")
            self.service.connect()

        self.assertEqual([m.text for m in self.received], ["banked overnight"])

    def test_after_connecting_a_stale_packet_is_discarded_as_before(self) -> None:
        """Outside the window, nothing about the old behaviour changes."""
        opened = self._connect_delivering([])
        self.assertEqual(self.received, [])

        self.service._on_text_received(
            packet=banked_packet("live"), interface=opened
        )
        self.service._on_text_received(
            packet=banked_packet("stale"), interface=make_interface()
        )

        self.assertEqual([m.text for m in self.received], ["live"])

    # ---- the hold must not become a leak -----------------------------

    def test_the_hold_is_emptied_by_the_replay(self) -> None:
        self._connect_delivering([banked_packet("hello")])

        self.assertEqual(self.service._connect_arrivals, [])
        self.assertFalse(self.service._connect_in_progress)

    def test_the_window_closes_even_if_opening_the_interface_fails(self) -> None:
        """A failed connect must not leave the service holding packets.

        If _connect_in_progress stuck on, every later stale packet would
        be silently accumulated instead of rejected -- a slow leak, and
        a much subtler bug than the one being fixed.
        """
        with (
            patch.object(self.service, "_check_device"),
            patch.object(
                self.service, "_open_interface", side_effect=OSError("no such port")
            ),
        ):
            with self.assertRaises(Exception):
                self.service.connect()

        self.assertFalse(self.service._connect_in_progress)
        self.assertEqual(
            self.service._connect_arrivals,
            [],
            "nothing arrived, so nothing should be carried",
        )

    def test_the_hold_is_bounded(self) -> None:
        """A pathological connect must not grow the list without limit."""
        flood = [banked_packet(f"m{index}") for index in range(MAX_CONNECT_ARRIVALS + 25)]
        self._connect_delivering(flood)

        self.assertEqual(len(self.received), MAX_CONNECT_ARRIVALS)
        self.assertEqual(self.service._connect_arrivals, [])

    def test_a_second_connect_does_not_replay_the_first_ones_packets(self) -> None:
        """Each connect starts with an empty hold.

        Otherwise a reconnect would re-deliver the previous drain, and
        the dedupe index would be the only thing standing between the
        user and a duplicated transcript.
        """
        self._connect_delivering([banked_packet("first connect")])
        self.received.clear()
        self.service._interface = None

        self._connect_delivering([banked_packet("second connect")])

        self.assertEqual([m.text for m in self.received], ["second connect"])


class FirstAttemptTimeoutTests(unittest.TestCase):
    """The first open after a close is the one that does not take.

    Measured: attempt one ran 33s and received a single node_info;
    attempt two pulled 95 node_info and the whole config in three
    seconds, and a two-minute wait between runs changed nothing. So the
    first attempt is dead rather than slow, and staring at it for 30s
    only widens the window in which it can consume the radio's queue.
    """

    def setUp(self) -> None:
        self.service = RadioService("/dev/ttyUSB0")

    def test_the_first_attempt_fails_fast(self) -> None:
        self.assertEqual(
            self.service._connect_timeout(), FIRST_CONNECT_TIMEOUT_SECONDS
        )

    def test_the_short_timeout_is_shorter(self) -> None:
        """Guards the constants against being edited into nonsense."""
        self.assertLess(FIRST_CONNECT_TIMEOUT_SECONDS, CONNECT_TIMEOUT_SECONDS)
        self.assertGreater(FIRST_CONNECT_TIMEOUT_SECONDS, 0)

    def test_later_attempts_get_the_full_timeout(self) -> None:
        """Once a connect has worked, a slow one is worth waiting for."""
        opened = make_interface()
        with (
            patch.object(self.service, "_check_device"),
            patch.object(self.service, "_open_interface", return_value=opened),
        ):
            self.service.connect()

        self.assertEqual(self.service._connect_timeout(), CONNECT_TIMEOUT_SECONDS)

    def test_closing_makes_the_next_open_a_first_open_again(self) -> None:
        """Otherwise every reconnect after the first wastes 30s.

        A reconnect opens the port afresh, so it hits exactly the same
        dead first attempt as process start does.
        """
        opened = make_interface()
        with (
            patch.object(self.service, "_check_device"),
            patch.object(self.service, "_open_interface", return_value=opened),
        ):
            self.service.connect()
        self.service.close()

        self.assertEqual(
            self.service._connect_timeout(), FIRST_CONNECT_TIMEOUT_SECONDS
        )

    def test_a_failed_connect_does_not_promote_the_timeout(self) -> None:
        """Nothing succeeded, so the next attempt is still a first one."""
        with (
            patch.object(self.service, "_check_device"),
            patch.object(
                self.service, "_open_interface", side_effect=OSError("timed out")
            ),
        ):
            with self.assertRaises(Exception):
                self.service.connect()

        self.assertEqual(
            self.service._connect_timeout(), FIRST_CONNECT_TIMEOUT_SECONDS
        )


class HeldDeliveryOrderingTests(unittest.TestCase):
    """Held packets must not be delivered before the app is ready.

    The app binds its CHAT history profile while handling the ONLINE
    event. Every read is narrowed by `AND profile_key = ?`, so a
    message persisted before that binding lands with profile_key NULL
    and is preserved-but-hidden forever.

    Seen in the field exactly once, which is what these tests exist to
    prevent recurring: id 2034 was stored in the same second as LINK
    online and never appeared, while id 2035 arrived two seconds later
    and rendered normally.
    """

    def setUp(self) -> None:
        self.service = RadioService("/dev/ttyUSB0")
        self.received = []
        self.service.add_message_handler(self.received.append)
        self.interface = make_interface()

    def _open_holding(self):
        def open_interface():
            self.service._on_text_received(
                packet=banked_packet("banked"), interface=self.interface
            )
            return self.interface

        return open_interface

    def test_defer_held_keeps_them_queued(self) -> None:
        with (
            patch.object(self.service, "_check_device"),
            patch.object(
                self.service, "_open_interface", side_effect=self._open_holding()
            ),
        ):
            self.service.connect(defer_held=True)

        self.assertEqual(self.received, [], "held until the caller says when")
        self.assertEqual(len(self.service._connect_arrivals), 1)

    def test_connect_without_the_flag_still_delivers(self) -> None:
        """Direct callers (tools, tests) keep the simple behaviour."""
        with (
            patch.object(self.service, "_check_device"),
            patch.object(
                self.service, "_open_interface", side_effect=self._open_holding()
            ),
        ):
            self.service.connect()

        self.assertEqual([m.text for m in self.received], ["banked"])

    def test_connection_events_delivers_only_after_online_is_handled(self) -> None:
        """The ordering that actually matters.

        A generator resumes on the consumer's NEXT iteration, so nothing
        placed after the ONLINE yield can run until the consumer has
        finished handling it -- which for the app is where the profile
        gets bound.
        """
        from threading import Event as ThreadEvent

        stopped = ThreadEvent()
        with (
            patch.object(self.service, "_check_device"),
            patch.object(
                self.service, "_open_interface", side_effect=self._open_holding()
            ),
        ):
            events = self.service.connection_events(
                retry_delay=0, stop_event=stopped, poll_interval=0.001
            )
            self.assertEqual(next(events).state, RadioState.CONNECTING)
            self.assertEqual(next(events).state, RadioState.ONLINE)

            # Standing exactly where the app binds its profile.
            self.assertEqual(
                self.received, [], "delivered before the consumer could bind"
            )

            stopped.set()
            for _ in events:
                pass

        self.assertEqual([m.text for m in self.received], ["banked"])


if __name__ == "__main__":
    unittest.main()
