"""Tests for receiver-stamped radio time and monotonic chat aging."""

from __future__ import annotations

import unittest

from app_controller import received_chat_entry
from message_time import make_age_reference, validate_radio_rx_at
from radio_service import ReceivedMessage
from relative_time import format_relative_age


class MessageTimeTests(unittest.TestCase):
    RECEIVED_AT = 1_700_000_000.0
    MONOTONIC_NOW = 10_000.0

    def make_message(self, radio_rx_at: object) -> ReceivedMessage:
        return ReceivedMessage(
            sender_node_id="!abc12345",
            sender_long_name="Alice Trail",
            sender_short_name="ALCE",
            channel_index=0,
            text="hello",
            rssi=-80,
            snr=5.0,
            packet_id=1,
            radio_rx_at=radio_rx_at,  # type: ignore[arg-type]
        )

    def test_valid_radio_receive_time_preserves_clocks_and_initial_age(self) -> None:
        radio_rx_at = self.RECEIVED_AT - 2 * 60 * 60
        entry = received_chat_entry(
            self.make_message(radio_rx_at),
            received_at=self.RECEIVED_AT,
            monotonic_now=self.MONOTONIC_NOW,
        )

        self.assertEqual(entry.radio_rx_at, radio_rx_at)
        self.assertIsNone(entry.local_sent_at)
        self.assertEqual(entry.received_at, self.RECEIVED_AT)
        self.assertNotEqual(entry.radio_rx_at, entry.received_at)
        self.assertEqual(
            format_relative_age(self.MONOTONIC_NOW - entry.age_reference),
            "2h",
        )
        self.assertEqual(
            format_relative_age(
                self.MONOTONIC_NOW + 12 * 60 - entry.age_reference
            ),
            "2h 12min",
        )

    def test_invalid_radio_receive_times_fall_back_to_local_time(self) -> None:
        invalid_values = (
            None,
            0,
            -1,
            "malformed",
            float("nan"),
            self.RECEIVED_AT + 60 * 60,
        )

        for value in invalid_values:
            with self.subTest(value=value):
                entry = received_chat_entry(
                    self.make_message(value),
                    received_at=self.RECEIVED_AT,
                    monotonic_now=self.MONOTONIC_NOW,
                )
                self.assertIsNone(entry.radio_rx_at)
                self.assertIsNone(entry.local_sent_at)
                self.assertEqual(entry.received_at, self.RECEIVED_AT)
                self.assertEqual(entry.age_reference, self.MONOTONIC_NOW)
                self.assertEqual(
                    format_relative_age(
                        self.MONOTONIC_NOW - entry.age_reference
                    ),
                    "0s",
                )

    def test_small_future_clock_skew_is_safe(self) -> None:
        radio_rx_at, reference = make_age_reference(
            self.RECEIVED_AT + 30,
            self.RECEIVED_AT,
            self.MONOTONIC_NOW,
        )

        self.assertEqual(radio_rx_at, self.RECEIVED_AT + 30)
        self.assertEqual(reference, self.MONOTONIC_NOW)
        self.assertEqual(
            validate_radio_rx_at(True, self.RECEIVED_AT),
            None,
        )
