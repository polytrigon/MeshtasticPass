"""Hardware-free tests for versioned SQLite CHAT persistence."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chat_store import ChatStore, ChatStoreError, default_chat_db_path


class ChatStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "nested" / "chat.db"
        self.store = ChatStore.open(self.path)
        self.addCleanup(self.store.close)

    def add_incoming(self, packet_id: int | None, text: str, when: float):
        return self.store.add_incoming(
            packet_id=packet_id,
            node_id="!a11ce001",
            sender_name="Alice Trail",
            sender_short_name="ALCE",
            channel_index=0,
            text=text,
            radio_rx_at=when - 2,
            received_at=when,
        )

    def test_first_open_creates_parent_database_and_schema(self) -> None:
        self.assertTrue(self.path.is_file())
        self.assertEqual(self.store.load_recent(), [])

    def test_xdg_default_location(self) -> None:
        with patch.dict(
            "os.environ",
            {"XDG_DATA_HOME": str(self.path.parent)},
        ):
            self.assertEqual(
                default_chat_db_path(),
                self.path.parent / "meshtasticpass" / "chat.db",
            )

    def test_incoming_deduplication_and_missing_packet_ids(self) -> None:
        first = self.add_incoming(123, "hello", 100.0)
        duplicate = self.add_incoming(123, "hello again", 101.0)
        missing_one = self.add_incoming(None, "no id one", 102.0)
        missing_two = self.add_incoming(None, "no id two", 103.0)

        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.message_id, first.message_id)
        self.assertTrue(missing_one.inserted)
        self.assertTrue(missing_two.inserted)
        self.assertEqual(len(self.store.load_recent()), 3)

    def test_history_survives_recreation_in_order_with_limit(self) -> None:
        for index in range(5):
            self.add_incoming(index + 1, f"message {index}", 100.0 + index)
        self.store.close()
        self.store = ChatStore.open(self.path)

        recent = self.store.load_recent(limit=3)
        self.assertEqual(
            [message.text for message in recent],
            ["message 2", "message 3", "message 4"],
        )

    def test_outgoing_attempt_and_delivery_state_persist(self) -> None:
        message_id = self.store.add_outgoing(
            text="mesh hello",
            channel_index=0,
            local_sent_at=200.0,
            delivery_state="SENDING",
        )
        attempt_id = self.store.add_send_attempt(
            message_id,
            200.0,
            "SENT",
        )
        self.store.update_delivery_state(
            message_id,
            "HEARD",
            attempt_id=attempt_id,
            packet_id=987,
            completed_at=205.0,
        )
        self.store.close()
        self.store = ChatStore.open(self.path)

        stored = self.store.load_recent()[0]
        self.assertEqual(stored.direction, "outgoing")
        self.assertEqual(stored.packet_id, 987)
        self.assertEqual(stored.delivery_state, "HEARD")
        self.assertEqual(stored.local_sent_at, 200.0)
        attempts = self.store.load_send_attempts(message_id)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].state, "HEARD")

    def test_malformed_database_is_reported_without_deletion(self) -> None:
        self.store.close()
        original = b"not a sqlite database"
        self.path.write_bytes(original)

        with self.assertRaises(ChatStoreError):
            ChatStore.open(self.path)

        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
