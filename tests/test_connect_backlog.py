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

    def test_a_packet_from_a_different_interface_is_still_discarded(self) -> None:
        """The stale-interface guard exists for a reason; keep it.

        A leftover packet from a previous connection must not be
        smuggled in by the hold -- being replayed is not the same as
        being trusted.
        """
        stale = make_interface()
        opened = make_interface()

        def open_interface():
            self.service._on_text_received(
                packet=banked_packet("from the dead connection"), interface=stale
            )
            self.service._on_text_received(
                packet=banked_packet("from the live one"), interface=opened
            )
            return opened

        with (
            patch.object(self.service, "_check_device"),
            patch.object(self.service, "_open_interface", side_effect=open_interface),
        ):
            self.service.connect()

        self.assertEqual([m.text for m in self.received], ["from the live one"])

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


if __name__ == "__main__":
    unittest.main()
