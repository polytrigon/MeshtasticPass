"""Hardware-free tests for versioned SQLite CHAT persistence."""

from __future__ import annotations

from pathlib import Path
import sqlite3
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

    def test_out_of_order_equal_and_untimed_rows_have_stable_restart_order(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!a",
            sender_name="A",
            sender_short_name="A",
            channel_index=0,
            text="newer first",
            radio_rx_at=300.0,
            received_at=500.0,
        )
        self.store.add_incoming(
            packet_id=2,
            node_id="!b",
            sender_name="B",
            sender_short_name="B",
            channel_index=0,
            text="older second",
            radio_rx_at=100.0,
            received_at=501.0,
        )
        self.store.add_incoming(
            packet_id=3,
            node_id="!c",
            sender_name="C",
            sender_short_name="C",
            channel_index=0,
            text="equal one",
            radio_rx_at=300.0,
            received_at=502.0,
        )
        self.store.add_incoming(
            packet_id=4,
            node_id="!d",
            sender_name="D",
            sender_short_name="D",
            channel_index=0,
            text="untimed arrival",
            radio_rx_at=None,
            received_at=503.0,
        )
        self.store.add_outgoing(
            text="local send",
            channel_index=0,
            local_sent_at=400.0,
            delivery_state="SENT",
        )

        before = [message.text for message in self.store.load_recent(limit=10)]
        self.store.close()
        self.store = ChatStore.open(self.path)
        after = [message.text for message in self.store.load_recent(limit=10)]

        self.assertEqual(
            before,
            [
                "older second",
                "newer first",
                "equal one",
                "local send",
                "untimed arrival",
            ],
        )
        self.assertEqual(after, before)

    def test_version_one_database_migrates_without_losing_history(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            DROP TABLE IF EXISTS send_attempts;
            DROP TABLE IF EXISTS messages;
            DROP TABLE IF EXISTS schema_version;
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (1);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                packet_id INTEGER,
                node_id TEXT,
                sender_name TEXT,
                sender_short_name TEXT,
                channel_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                radio_rx_at REAL,
                received_at REAL NOT NULL,
                local_sent_at REAL,
                delivery_state TEXT,
                created_at REAL NOT NULL
            );
            INSERT INTO messages VALUES (
                1, 'incoming', 77, '!old', 'Old Node', 'OLD', 0,
                'preserved', 100, 101, NULL, NULL, 101
            );
            """
        )
        connection.commit()
        connection.close()

        self.store = ChatStore.open(self.path)
        messages = self.store.load_recent()
        version = self.store._connection.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]

        self.assertEqual(version, 2)
        self.assertEqual([message.text for message in messages], ["preserved"])
        self.assertIsNone(messages[0].origin_sent_at)

    def test_cursor_pages_are_bounded_and_chronological(self) -> None:
        for index in range(175):
            self.add_incoming(index + 1, f"message {index}", 100.0 + index)

        statements = []
        self.store._connection.set_trace_callback(statements.append)
        recent = self.store.load_recent_page(limit=100)
        first_older = self.store.load_older_page(recent.messages[0].id, limit=50)
        final_older = self.store.load_older_page(
            first_older.messages[0].id,
            limit=50,
        )

        self.assertEqual(len(recent.messages), 100)
        self.assertTrue(recent.has_older)
        self.assertEqual(recent.messages[0].text, "message 75")
        self.assertEqual(recent.messages[-1].text, "message 174")
        self.assertEqual(len(first_older.messages), 50)
        self.assertTrue(first_older.has_older)
        self.assertEqual(first_older.messages[0].text, "message 25")
        self.assertEqual(first_older.messages[-1].text, "message 74")
        self.assertEqual(len(final_older.messages), 25)
        self.assertFalse(final_older.has_older)
        self.assertEqual(final_older.messages[0].text, "message 0")
        self.assertEqual(final_older.messages[-1].text, "message 24")
        page_queries = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        ]
        self.assertTrue(page_queries)
        self.assertTrue(all("LIMIT" in statement.upper() for statement in page_queries))
        self.assertTrue(all("OFFSET" not in statement.upper() for statement in page_queries))

    def test_out_of_order_cursor_pages_are_duplicate_free(self) -> None:
        for index in range(175):
            self.add_incoming(index + 1, f"message {index}", 100.0 + index)
        self.store.add_incoming(
            packet_id=999,
            node_id="!late",
            sender_name="Late",
            sender_short_name="LATE",
            channel_index=0,
            text="late middle",
            radio_rx_at=148.5,
            received_at=1000.0,
        )

        page = self.store.load_recent_page(limit=100)
        all_messages = list(page.messages)
        while page.has_older:
            page = self.store.load_older_page(all_messages[0].id, limit=50)
            all_messages[0:0] = page.messages

        ids = [message.id for message in all_messages]
        self.assertEqual(len(ids), 176)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            [message.order_key for message in all_messages],
            sorted(message.order_key for message in all_messages),
        )
        self.assertIn("late middle", [message.text for message in all_messages])

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

    def test_latest_incoming_message_at_picks_newest_across_channels(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=0,
            text="early",
            radio_rx_at=100.0,
            received_at=100.0,
        )
        self.store.add_incoming(
            packet_id=2,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=1,
            text="later, different channel",
            radio_rx_at=500.0,
            received_at=500.0,
        )
        result = self.store.latest_incoming_message_at()
        self.assertEqual(result["!a11ce001"], 500.0)

    def test_latest_incoming_message_at_excludes_outgoing(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=0,
            text="hi",
            radio_rx_at=100.0,
            received_at=100.0,
        )
        self.store.add_outgoing(
            text="a much later reply",
            channel_index=0,
            local_sent_at=9_999.0,
            delivery_state="SENDING",
        )
        result = self.store.latest_incoming_message_at()
        self.assertEqual(result, {"!a11ce001": 100.0})

    def test_delayed_out_of_order_insertion_resolves_by_truthful_timestamp(
        self,
    ) -> None:
        """A message inserted LAST can still carry the OLDEST timestamp, and

        vice versa; the result must reflect true timestamp order, not
        insertion/row order.
        """
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=0,
            text="arrives first, timestamped later",
            radio_rx_at=900.0,
            received_at=900.0,
        )
        self.store.add_incoming(
            packet_id=2,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=0,
            text="arrives second, but an older delayed packet",
            radio_rx_at=200.0,
            received_at=950.0,
        )
        result = self.store.latest_incoming_message_at()
        self.assertEqual(result["!a11ce001"], 900.0)

    def test_origin_sent_at_takes_precedence_over_radio_rx_at(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=0,
            text="hi",
            radio_rx_at=100.0,
            received_at=100.0,
            origin_sent_at=50.0,
        )
        result = self.store.latest_incoming_message_at()
        self.assertEqual(result["!a11ce001"], 50.0)

    def test_untimed_incoming_messages_are_excluded_not_guessed(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!untimed001",
            sender_name="Untimed",
            sender_short_name="UT",
            channel_index=0,
            text="no trustworthy timestamp",
            radio_rx_at=None,
            received_at=100.0,
        )
        result = self.store.latest_incoming_message_at()
        self.assertNotIn("!untimed001", result)

    def test_node_id_lookup_is_case_insensitive(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!A11CE001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=0,
            text="hi",
            radio_rx_at=100.0,
            received_at=100.0,
        )
        result = self.store.latest_incoming_message_at()
        self.assertEqual(result, {"!a11ce001": 100.0})

    def test_latest_incoming_message_at_survives_reopening_the_store(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=0,
            text="a week ago",
            radio_rx_at=100.0,
            received_at=100.0,
        )
        self.store.close()
        self.store = ChatStore.open(self.path)
        result = self.store.latest_incoming_message_at()
        self.assertEqual(result, {"!a11ce001": 100.0})

    def test_latest_incoming_message_at_query_uses_index_not_a_full_scan(
        self,
    ) -> None:
        for index in range(50):
            self.store.add_incoming(
                packet_id=index,
                node_id=f"!n{index % 5:07x}",
                sender_name="X",
                sender_short_name="X",
                channel_index=index % 3,
                text="m",
                radio_rx_at=float(index),
                received_at=float(index),
            )
        # pylint: disable=protected-access
        plan = self.store._connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT LOWER(node_id) AS node_id,
                   MAX(COALESCE(origin_sent_at, radio_rx_at)) AS message_time
            FROM messages
            WHERE direction = 'incoming'
                AND node_id IS NOT NULL
                AND COALESCE(origin_sent_at, radio_rx_at) IS NOT NULL
            GROUP BY LOWER(node_id)
            """
        ).fetchall()
        detail = " ".join(str(row["detail"]) for row in plan)
        self.assertIn("USING INDEX incoming_node_message_time", detail)
        self.assertNotIn("SCAN messages", detail)


if __name__ == "__main__":
    unittest.main()
