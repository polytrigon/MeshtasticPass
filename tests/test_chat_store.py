"""Hardware-free tests for versioned SQLite CHAT persistence."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from chat_store import (
    DEFAULT_HISTORY_LIMIT,
    ChatStore,
    ChatStoreError,
    default_chat_db_path,
)


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

        self.assertEqual(version, 3)
        self.assertEqual([message.text for message in messages], ["preserved"])
        self.assertIsNone(messages[0].origin_sent_at)
        self.assertIsNone(messages[0].dm_node_id)

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

    def test_delete_message_removes_the_message_and_its_send_attempts(self) -> None:
        message_id = self.store.add_outgoing(
            text="delete me",
            channel_index=0,
            local_sent_at=300.0,
            delivery_state="FAILED",
        )
        self.store.add_send_attempt(message_id, 300.0, "FAILED")

        self.store.delete_message(message_id)

        self.assertEqual(
            [m.id for m in self.store.load_recent()], []
        )
        self.assertEqual(self.store.load_send_attempts(message_id), [])
        raw = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            row = raw.execute(
                "SELECT COUNT(*) FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            self.assertEqual(row[0], 0)
            row = raw.execute(
                "SELECT COUNT(*) FROM send_attempts WHERE message_id = ?", (message_id,)
            ).fetchone()
            self.assertEqual(row[0], 0)
        finally:
            raw.close()

    def test_delete_message_never_touches_another_message(self) -> None:
        keep_id = self.store.add_outgoing(
            text="identical text",
            channel_index=0,
            local_sent_at=301.0,
            delivery_state="SENT",
        )
        delete_id = self.store.add_outgoing(
            text="identical text",
            channel_index=0,
            local_sent_at=302.0,
            delivery_state="FAILED",
        )
        self.store.add_send_attempt(keep_id, 301.0, "SENT")
        self.store.add_send_attempt(delete_id, 302.0, "FAILED")

        self.store.delete_message(delete_id)

        remaining_ids = [m.id for m in self.store.load_recent()]
        self.assertEqual(remaining_ids, [keep_id])
        self.assertEqual(len(self.store.load_send_attempts(keep_id)), 1)

    def test_delete_message_is_a_no_op_for_an_unknown_id(self) -> None:
        message_id = self.store.add_outgoing(
            text="unrelated",
            channel_index=0,
            local_sent_at=303.0,
            delivery_state="SENT",
        )
        self.store.delete_message(message_id + 999)  # never existed
        self.assertEqual([m.id for m in self.store.load_recent()], [message_id])

    def test_delete_message_persists_across_restart(self) -> None:
        message_id = self.store.add_outgoing(
            text="gone for good",
            channel_index=0,
            local_sent_at=304.0,
            delivery_state="FAILED",
        )
        self.store.delete_message(message_id)
        self.store.close()  # close() is idempotent -- tearDown's own close is harmless

        reopened = ChatStore.open(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual([m.id for m in reopened.load_recent()], [])

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


class ReconcileAbandonedSendingTests(unittest.TestCase):
    """ChatStore.open() must repair every abandoned SENDING row itself,

    directly in SQLite, before any caller can hydrate history -- not
    only the rows a particular app session happens to load into
    memory. Each test opens a store, seeds rows, closes it, then opens
    a COMPLETELY FRESH ChatStore on the same file (never reusing the
    seeding instance) to reproduce the real scenario: a database
    already on disk from a previous process, being opened for the
    first time by a new one.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "chat.db"

    def _seed_outgoing(
        self,
        store: ChatStore,
        text: str,
        *,
        channel_index: int = 0,
        local_sent_at: float = 100.0,
        delivery_state: str = "SENDING",
    ) -> int:
        message_id = store.add_outgoing(
            text=text,
            channel_index=channel_index,
            local_sent_at=local_sent_at,
            delivery_state=delivery_state,
        )
        store.add_send_attempt(message_id, local_sent_at, delivery_state)
        return message_id

    def _raw_delivery_state(self, message_id: int) -> str:
        # A completely independent read-only connection, never the
        # ChatStore instance under test -- proves the value is really
        # in SQLite, not just what one particular Python object reports.
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT delivery_state FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        finally:
            connection.close()
        return row[0]

    def _raw_send_attempt_state(self, message_id: int) -> str:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT state FROM send_attempts WHERE message_id = ?", (message_id,)
            ).fetchone()
        finally:
            connection.close()
        return row[0]

    def test_preexisting_sending_row_becomes_interrupted_on_open(self) -> None:
        """The closest reproduction of the real uConsole report: a

        SENDING row already on disk from an earlier process, repaired
        the moment a brand-new store opens it -- before history is
        hydrated, without that process ever having sent anything.
        """
        seeding_store = ChatStore.open(self.path)
        message_id = self._seed_outgoing(seeding_store, "still on the way?")
        seeding_store.close()

        fresh_store = ChatStore.open(self.path)
        self.addCleanup(fresh_store.close)

        self.assertEqual(self._raw_delivery_state(message_id), "INTERRUPTED")
        stored = fresh_store.load_recent()[0]
        self.assertEqual(stored.delivery_state, "INTERRUPTED")

    def test_reconcile_abandoned_sending_returns_count_of_rewritten_rows(
        self,
    ) -> None:
        """Exercises the method directly (not via open()'s automatic

        call) so its return value can be observed in isolation: two
        genuinely-abandoned outgoing rows plus one incoming row whose
        delivery_state has been force-corrupted to SENDING (incoming
        rows never legitimately have one) -- only the two outgoing
        rows may be counted or rewritten.
        """
        store = ChatStore.open(self.path)  # reconciles the empty db: a no-op
        self.addCleanup(store.close)
        first_id = self._seed_outgoing(store, "one")
        second_id = self._seed_outgoing(store, "two")
        incoming_id = store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=0,
            text="not outgoing, must not count",
            radio_rx_at=100.0,
            received_at=100.0,
        ).message_id
        # pylint: disable=protected-access
        store._connection.execute(
            "UPDATE messages SET delivery_state = 'SENDING' WHERE id = ?",
            (incoming_id,),
        )
        store._connection.commit()

        self.assertEqual(store.reconcile_abandoned_sending(), 2)
        stored_by_id = {message.id: message for message in store.load_recent()}
        self.assertEqual(stored_by_id[first_id].delivery_state, "INTERRUPTED")
        self.assertEqual(stored_by_id[second_id].delivery_state, "INTERRUPTED")
        self.assertEqual(stored_by_id[incoming_id].delivery_state, "SENDING")

    def test_multiple_channels_and_ages_all_reconciled_in_one_pass(self) -> None:
        seeding_store = ChatStore.open(self.path)
        current_channel_id = self._seed_outgoing(
            seeding_store, "recent, current channel", channel_index=0, local_sent_at=900.0
        )
        other_channel_id = self._seed_outgoing(
            seeding_store, "recent, other channel", channel_index=1, local_sent_at=900.0
        )
        very_old_id = self._seed_outgoing(
            seeding_store, "ancient, outside any initial page", channel_index=0, local_sent_at=1.0
        )
        # Enough intervening incoming history that the old row would
        # only surface via OLD MESSAGES pagination in the real app.
        for index in range(DEFAULT_HISTORY_LIMIT + 20):
            seeding_store.add_incoming(
                packet_id=1000 + index,
                node_id="!a11ce001",
                sender_name="Alice",
                sender_short_name="ALCE",
                channel_index=0,
                text=f"filler {index}",
                radio_rx_at=10.0 + index,
                received_at=10.0 + index,
            )
        seeding_store.close()

        fresh_store = ChatStore.open(self.path)
        self.addCleanup(fresh_store.close)

        for message_id in (current_channel_id, other_channel_id, very_old_id):
            with self.subTest(message_id=message_id):
                self.assertEqual(self._raw_delivery_state(message_id), "INTERRUPTED")

        # Confirm the old row is genuinely reachable only via pagination,
        # and still reads INTERRUPTED once actually loaded that way.
        page = fresh_store.load_recent_page(channel_index=0, limit=DEFAULT_HISTORY_LIMIT)
        self.assertTrue(page.has_older)
        self.assertNotIn(very_old_id, [message.id for message in page.messages])
        older_page = fresh_store.load_older_page(
            page.messages[0].id, channel_index=0, limit=DEFAULT_HISTORY_LIMIT
        )
        oldest_loaded = next(
            message for message in older_page.messages if message.id == very_old_id
        )
        self.assertEqual(oldest_loaded.delivery_state, "INTERRUPTED")

    def test_incoming_rows_are_never_touched(self) -> None:
        seeding_store = ChatStore.open(self.path)
        result = seeding_store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALCE",
            channel_index=0,
            text="hello",
            radio_rx_at=100.0,
            received_at=100.0,
        )
        seeding_store.close()

        fresh_store = ChatStore.open(self.path)
        self.addCleanup(fresh_store.close)
        stored = fresh_store.load_recent()[0]
        self.assertEqual(stored.id, result.message_id)
        self.assertIsNone(stored.delivery_state)

    def test_terminal_outgoing_states_are_never_touched(self) -> None:
        for state in ("SENT", "HEARD", "UNCONFIRMED", "FAILED", "INTERRUPTED"):
            with self.subTest(state=state):
                path = Path(self.temporary_directory.name) / f"{state}.db"
                seeding_store = ChatStore.open(path)
                message_id = self._seed_outgoing(
                    seeding_store, f"already {state}", delivery_state=state
                )
                seeding_store.close()

                fresh_store = ChatStore.open(path)
                try:
                    self.assertEqual(self._raw_delivery_state_at(path, message_id), state)
                    stored = fresh_store.load_recent()[0]
                    self.assertEqual(stored.delivery_state, state)
                finally:
                    fresh_store.close()

    def _raw_delivery_state_at(self, path: Path, message_id: int) -> str:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT delivery_state FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        finally:
            connection.close()
        return row[0]

    def test_send_attempts_rows_are_also_reconciled(self) -> None:
        seeding_store = ChatStore.open(self.path)
        message_id = self._seed_outgoing(seeding_store, "attempt row too")
        seeding_store.close()

        fresh_store = ChatStore.open(self.path)
        self.addCleanup(fresh_store.close)
        self.assertEqual(self._raw_send_attempt_state(message_id), "INTERRUPTED")

    def test_new_send_after_open_can_still_persist_sending(self) -> None:
        """Startup reconciliation is a one-time pass, not a write

        trigger: a message legitimately sent later in the SAME open
        store must still persist as SENDING.
        """
        seeding_store = ChatStore.open(self.path)
        self._seed_outgoing(seeding_store, "old abandoned one")
        seeding_store.close()

        store = ChatStore.open(self.path)  # runs reconciliation once, here
        self.addCleanup(store.close)
        new_message_id = store.add_outgoing(
            text="brand new, sent by this very process",
            channel_index=0,
            local_sent_at=999.0,
            delivery_state="SENDING",
        )
        self.assertEqual(self._raw_delivery_state(new_message_id), "SENDING")
        stored = [m for m in store.load_recent() if m.id == new_message_id][0]
        self.assertEqual(stored.delivery_state, "SENDING")

    def test_second_restart_remains_interrupted(self) -> None:
        seeding_store = ChatStore.open(self.path)
        message_id = self._seed_outgoing(seeding_store, "restart me twice")
        seeding_store.close()

        first_restart = ChatStore.open(self.path)
        self.assertEqual(self._raw_delivery_state(message_id), "INTERRUPTED")
        first_restart.close()

        second_restart = ChatStore.open(self.path)
        self.addCleanup(second_restart.close)
        self.assertEqual(self._raw_delivery_state(message_id), "INTERRUPTED")
        stored = second_restart.load_recent()[0]
        self.assertEqual(stored.delivery_state, "INTERRUPTED")


class DirectMessagePersistenceTests(unittest.TestCase):
    """DM history is keyed by the remote party's stable node ID, kept

    entirely separate from channel history in the SAME database (item
    12: extend the existing store, never a parallel one).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "chat.db"
        self.store = ChatStore.open(self.path)
        self.addCleanup(self.store.close)

    def test_dm_row_never_appears_in_channel_history(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="a channel message",
            radio_rx_at=100.0,
            received_at=100.0,
        )
        self.store.add_incoming(
            packet_id=2,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="a DM",
            radio_rx_at=101.0,
            received_at=101.0,
            dm_node_id="!a11ce001",
        )
        channel_texts = [m.text for m in self.store.load_recent(channel_index=0)]
        self.assertEqual(channel_texts, ["a channel message"])
        dm_texts = [m.text for m in self.store.load_recent_dm_page("!a11ce001").messages]
        self.assertEqual(dm_texts, ["a DM"])

    def test_alice_and_bob_dm_histories_are_isolated(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="from alice",
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id="!a11ce001",
        )
        self.store.add_incoming(
            packet_id=2,
            node_id="!b0b00002",
            sender_name="Bob",
            sender_short_name="BOB",
            channel_index=0,
            text="from bob",
            radio_rx_at=101.0,
            received_at=101.0,
            dm_node_id="!b0b00002",
        )
        alice = [m.text for m in self.store.load_recent_dm_page("!a11ce001").messages]
        bob = [m.text for m in self.store.load_recent_dm_page("!b0b00002").messages]
        self.assertEqual(alice, ["from alice"])
        self.assertEqual(bob, ["from bob"])

    def test_outgoing_dm_records_the_destination(self) -> None:
        message_id = self.store.add_outgoing(
            text="hi alice",
            channel_index=0,
            local_sent_at=100.0,
            delivery_state="SENDING",
            dm_node_id="!a11ce001",
        )
        stored = self.store.load_recent_dm_page("!a11ce001").messages
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].id, message_id)
        self.assertEqual(stored[0].dm_node_id, "!a11ce001")
        self.assertTrue(stored[0].direction == "outgoing")

    def test_incoming_dm_deduplicates_by_packet_id_like_channel_messages(self) -> None:
        first = self.store.add_incoming(
            packet_id=42,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="hello",
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id="!a11ce001",
        )
        duplicate = self.store.add_incoming(
            packet_id=42,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="hello again",
            radio_rx_at=101.0,
            received_at=101.0,
            dm_node_id="!a11ce001",
        )
        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.message_id, first.message_id)

    def test_same_packet_id_never_collides_between_channel_and_dm(self) -> None:
        """A packet_id landing in BOTH a channel row and a DM row (same

        sender, same packet_id, coincidentally) must never be treated
        as one duplicate of the other -- proves the COALESCE(dm_node_id,
        '') dedup index correctly distinguishes them (see _create_schema).
        """
        channel_result = self.store.add_incoming(
            packet_id=7,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="channel version",
            radio_rx_at=100.0,
            received_at=100.0,
        )
        dm_result = self.store.add_incoming(
            packet_id=7,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="dm version",
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id="!a11ce001",
        )
        self.assertTrue(channel_result.inserted)
        self.assertTrue(dm_result.inserted)
        self.assertNotEqual(channel_result.message_id, dm_result.message_id)

    def test_list_dm_conversations_sorted_by_most_recent_activity(self) -> None:
        self.store.add_outgoing(
            text="old to alice",
            channel_index=0,
            local_sent_at=100.0,
            delivery_state="SENT",
            dm_node_id="!a11ce001",
        )
        self.store.add_outgoing(
            text="newer to bob",
            channel_index=0,
            local_sent_at=200.0,
            delivery_state="SENT",
            dm_node_id="!b0b00002",
        )
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="alice replies, now newest",
            radio_rx_at=300.0,
            received_at=300.0,
            dm_node_id="!a11ce001",
        )
        conversations = self.store.list_dm_conversations()
        self.assertEqual(
            [node_id for node_id, _time in conversations],
            ["!a11ce001", "!b0b00002"],
        )

    def test_list_dm_conversations_excludes_channel_only_nodes(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="channel only, never DMed",
            radio_rx_at=100.0,
            received_at=100.0,
        )
        self.assertEqual(self.store.list_dm_conversations(), [])

    def test_older_dm_page_pages_correctly(self) -> None:
        for index in range(5):
            self.store.add_incoming(
                packet_id=index + 1,
                node_id="!a11ce001",
                sender_name="Alice",
                sender_short_name="ALC",
                channel_index=0,
                text=f"dm {index}",
                radio_rx_at=100.0 + index,
                received_at=100.0 + index,
                dm_node_id="!a11ce001",
            )
        recent = self.store.load_recent_dm_page("!a11ce001", limit=2)
        self.assertEqual([m.text for m in recent.messages], ["dm 3", "dm 4"])
        self.assertTrue(recent.has_older)
        older = self.store.load_older_dm_page(
            recent.messages[0].id, "!a11ce001", limit=2
        )
        self.assertEqual([m.text for m in older.messages], ["dm 1", "dm 2"])

    def test_dm_does_not_affect_latest_incoming_message_at(self) -> None:
        """MESH's own last-interaction signal must stay channel-only --

        this pass makes no MESH changes, so a DM must never feed it.
        """
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="a DM",
            radio_rx_at=100.0,
            received_at=100.0,
            origin_sent_at=100.0,
            dm_node_id="!a11ce001",
        )
        self.assertEqual(self.store.latest_incoming_message_at(), {})

    def test_v2_database_without_dm_column_migrates_cleanly(self) -> None:
        """A v2 database (post origin_sent_at, pre dm_node_id) gains the

        column and the widened dedup index without losing history.
        """
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            DROP TABLE IF EXISTS send_attempts;
            DROP TABLE IF EXISTS messages;
            DROP TABLE IF EXISTS schema_version;
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (2);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                packet_id INTEGER,
                node_id TEXT,
                sender_name TEXT,
                sender_short_name TEXT,
                channel_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                origin_sent_at REAL,
                radio_rx_at REAL,
                received_at REAL NOT NULL,
                local_sent_at REAL,
                delivery_state TEXT,
                created_at REAL NOT NULL
            );
            INSERT INTO messages VALUES (
                1, 'incoming', 88, '!v2node', 'V2 Node', 'V2', 0,
                'v2 preserved', 200, 201, 201, NULL, NULL, 201
            );
            """
        )
        connection.commit()
        connection.close()

        reopened = ChatStore.open(self.path)
        self.addCleanup(reopened.close)
        version = reopened._connection.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        self.assertEqual(version, 3)
        messages = reopened.load_recent()
        self.assertEqual([m.text for m in messages], ["v2 preserved"])
        self.assertIsNone(messages[0].dm_node_id)
        # The migrated store must correctly support new DM traffic too.
        reopened.add_incoming(
            packet_id=1,
            node_id="!newdm01",
            sender_name="New DM",
            sender_short_name="NDM",
            channel_index=0,
            text="works after migration",
            radio_rx_at=300.0,
            received_at=300.0,
            dm_node_id="!newdm01",
        )
        dm_messages = reopened.load_recent_dm_page("!newdm01").messages
        self.assertEqual([m.text for m in dm_messages], ["works after migration"])


if __name__ == "__main__":
    unittest.main()
