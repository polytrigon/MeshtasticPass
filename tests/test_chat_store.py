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

        self.assertEqual(version, 6)
        self.assertIsNone(messages[0].local_node_id)
        self.assertEqual([message.text for message in messages], ["preserved"])
        self.assertIsNone(messages[0].origin_sent_at)
        self.assertIsNone(messages[0].dm_node_id)
        self.assertIsNone(messages[0].channel_key)

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
        # Newer SQLite reports the same plan as "USING COVERING INDEX";
        # either phrasing proves the named index is used -- the actual
        # requirement -- so accept both rather than pinning one
        # SQLite version's wording.
        self.assertRegex(
            detail, r"USING (?:COVERING )?INDEX incoming_node_message_time"
        )
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
        self.assertEqual(version, 6)
        messages = reopened.load_recent()
        self.assertEqual([m.text for m in messages], ["v2 preserved"])
        self.assertIsNone(messages[0].dm_node_id)
        self.assertIsNone(messages[0].channel_key)
        self.assertIsNone(messages[0].local_node_id)
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


class ChannelKeyIsolationTests(unittest.TestCase):
    """CHAT channel-history isolation (FINAL MESHTASTIC POLISH pass):

    a same-slot radio reconfiguration (e.g. index 0 LongFast ->
    MediumSlow) must not resurface the OTHER identity's history under
    the new one, and grandfathered legacy (NULL channel_key) rows must
    stay visible rather than silently vanish.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "chat.db"
        self.store = ChatStore.open(self.path)
        self.addCleanup(self.store.close)

    def test_v3_database_without_channel_key_column_migrates_cleanly(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            DROP TABLE IF EXISTS send_attempts;
            DROP TABLE IF EXISTS messages;
            DROP TABLE IF EXISTS schema_version;
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (3);
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
                created_at REAL NOT NULL,
                dm_node_id TEXT
            );
            INSERT INTO messages VALUES (
                1, 'incoming', 88, '!v3node', 'V3 Node', 'V3', 0,
                'v3 preserved', 200, 201, 201, NULL, NULL, 201, NULL
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
        self.assertEqual(version, 6)
        messages = reopened.load_recent()
        self.assertEqual([m.text for m in messages], ["v3 preserved"])
        self.assertIsNone(messages[0].channel_key)
        self.assertIsNone(messages[0].local_node_id)

    def test_v4_database_migrates_cleanly_and_preserves_legacy_rows(self) -> None:
        """A valid schema-v4 database must open and migrate deterministically
        to v5 (per-radio local_node_id namespace), not raise "Unsupported
        CHAT schema version 4".

        Regression: the v4 -> v5 bump changed SCHEMA_VERSION to 5 but the
        supported-version guard -- a literal "(1, 2, 3, SCHEMA_VERSION)"
        tuple -- was not updated to include 4, so a real v4 database raised
        "Unsupported CHAT schema version 4" before the v4 -> v5 migration
        could run. PRESERVE BUT HIDE: legacy rows stay physically present
        with local_node_id = NULL and are NOT attributed to any radio.
        """
        self.store.close()
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            DROP TABLE IF EXISTS send_attempts;
            DROP TABLE IF EXISTS messages;
            DROP TABLE IF EXISTS schema_version;
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (4);
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
                created_at REAL NOT NULL,
                dm_node_id TEXT,
                channel_key TEXT
            );
            CREATE TABLE send_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id),
                packet_id INTEGER,
                state TEXT NOT NULL,
                started_at REAL NOT NULL,
                completed_at REAL,
                error TEXT
            );
            INSERT INTO messages VALUES (
                1, 'incoming', 88, '!v4node', 'V4 Node', 'V4', 0,
                'v4 channel preserved', 200, 201, 201, NULL, NULL, 201, NULL, NULL
            );
            INSERT INTO messages VALUES (
                2, 'outgoing', NULL, NULL, 'YOU', NULL, 0,
                'v4 dm preserved', NULL, NULL, 202, 202, 'SENT', 202, '!a11ce001', NULL
            );
            INSERT INTO send_attempts VALUES (
                1, 2, 838484544, 'SENT', 202, NULL, NULL
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
        self.assertEqual(version, 6)
        # local_node_id column now exists.
        columns = {
            column["name"]
            for column in reopened._connection.execute(
                "PRAGMA table_info(messages)"
            ).fetchall()
        }
        self.assertIn("local_node_id", columns)
        # Legacy rows are physically present and NOT attributed to a radio.
        rows = reopened._connection.execute(
            "SELECT text, local_node_id FROM messages ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(row["text"], row["local_node_id"]) for row in rows],
            [("v4 channel preserved", None), ("v4 dm preserved", None)],
        )
        # Legacy rows are hidden from a bound current-radio PROFILE.
        reopened.set_active_profile("!deadbeef")
        page = reopened.load_recent_page(channel_index=0, channel_key=None)
        self.assertEqual([m.text for m in page.messages], [])
        # No row was silently assigned to the connected radio/profile; both
        # legacy namespace columns stay NULL (preserved-but-hide).
        self.assertIsNone(
            reopened._connection.execute(
                "SELECT local_node_id FROM messages LIMIT 1"
            ).fetchone()["local_node_id"]
        )
        self.assertIsNone(
            reopened._connection.execute(
                "SELECT profile_key FROM messages LIMIT 1"
            ).fetchone()["profile_key"]
        )
        # Reopening again (a second initialization) does not corrupt it.
        reopened.close()
        second = ChatStore.open(self.path)
        self.addCleanup(second.close)
        self.assertEqual(
            second._connection.execute("SELECT version FROM schema_version").fetchone()[0],
            6,
        )
        versions = {
            row["version"]
            for row in second._connection.execute("SELECT version FROM schema_version").fetchall()
        }
        self.assertEqual(versions, {6})
        remaining = second._connection.execute(
            "SELECT text FROM messages ORDER BY id"
        ).fetchall()
        self.assertEqual(len(remaining), 2)

    def test_channel_key_none_returns_every_row_unfiltered(self) -> None:
        """Identity not yet known -- e.g. before the radio connects --

        must show whatever is already there, exactly like before this
        column existed.
        """
        self.store.add_incoming(
            packet_id=1,
            node_id="!node001",
            sender_name="Node",
            sender_short_name="NODE",
            channel_index=0,
            text="key A message",
            radio_rx_at=100.0,
            received_at=100.0,
            channel_key="id:aaa",
        )
        self.store.add_incoming(
            packet_id=2,
            node_id="!node002",
            sender_name="Node",
            sender_short_name="NODE",
            channel_index=0,
            text="key B message",
            radio_rx_at=101.0,
            received_at=101.0,
            channel_key="id:bbb",
        )

        page = self.store.load_recent_page(channel_index=0, channel_key=None)

        self.assertEqual(
            [m.text for m in page.messages],
            ["key A message", "key B message"],
        )

    def test_channel_key_mismatch_excludes_the_other_identity(self) -> None:
        """A same-slot reconfiguration's OLD history stays out of view

        once the NEW identity's key is known -- the actual repro this
        column exists to fix.
        """
        self.store.add_incoming(
            packet_id=1,
            node_id="!longfast1",
            sender_name="LongFast Node",
            sender_short_name="LF",
            channel_index=0,
            text="longfast history",
            radio_rx_at=100.0,
            received_at=100.0,
            channel_key="id:longfast",
        )

        page = self.store.load_recent_page(
            channel_index=0, channel_key="id:mediumslow"
        )

        self.assertEqual(page.messages, ())

    def test_channel_key_match_includes_only_that_identity(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!longfast1",
            sender_name="LongFast Node",
            sender_short_name="LF",
            channel_index=0,
            text="longfast history",
            radio_rx_at=100.0,
            received_at=100.0,
            channel_key="id:longfast",
        )
        self.store.add_incoming(
            packet_id=2,
            node_id="!mediumslow1",
            sender_name="MediumSlow Node",
            sender_short_name="MS",
            channel_index=0,
            text="mediumslow history",
            radio_rx_at=101.0,
            received_at=101.0,
            channel_key="id:mediumslow",
        )

        page = self.store.load_recent_page(
            channel_index=0, channel_key="id:mediumslow"
        )

        self.assertEqual(
            [m.text for m in page.messages], ["mediumslow history"]
        )

    def test_legacy_null_channel_key_rows_are_grandfathered_in(self) -> None:
        """A row written before this column existed has no recorded

        identity to compare against -- it must stay visible rather than
        be silently hidden by a later, unrelated channel_key filter.
        """
        self.store.add_incoming(
            packet_id=1,
            node_id="!legacy001",
            sender_name="Legacy Node",
            sender_short_name="LEG",
            channel_index=0,
            text="legacy row",
            radio_rx_at=100.0,
            received_at=100.0,
        )

        page = self.store.load_recent_page(
            channel_index=0, channel_key="id:mediumslow"
        )

        self.assertEqual([m.text for m in page.messages], ["legacy row"])

    def test_duplicate_display_names_stay_isolated_by_channel_key(self) -> None:
        """Two channels sharing the same display NAME (e.g. both named

        "LongFast" but with different PSKs) are still kept apart --
        identity here is never the display name, only the stable key.
        """
        self.store.add_incoming(
            packet_id=1,
            node_id="!alice",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="first LongFast",
            radio_rx_at=100.0,
            received_at=100.0,
            channel_key="id:longfast-A",
        )
        self.store.add_incoming(
            packet_id=1,
            node_id="!bob",
            sender_name="Bob",
            sender_short_name="BOB",
            channel_index=1,
            text="second LongFast",
            radio_rx_at=101.0,
            received_at=101.0,
            channel_key="id:longfast-B",
        )

        first = self.store.load_recent_page(channel_index=0, channel_key="id:longfast-A")
        second = self.store.load_recent_page(channel_index=1, channel_key="id:longfast-B")

        self.assertEqual([m.text for m in first.messages], ["first LongFast"])
        self.assertEqual([m.text for m in second.messages], ["second LongFast"])

    def test_outgoing_channel_key_round_trips(self) -> None:
        self.store.add_outgoing(
            text="outgoing on mediumslow",
            channel_index=0,
            local_sent_at=200.0,
            delivery_state="SENT",
            channel_key="id:mediumslow",
        )

        matching = self.store.load_recent_page(channel_index=0, channel_key="id:mediumslow")
        mismatched = self.store.load_recent_page(channel_index=0, channel_key="id:longfast")

        self.assertEqual(
            [m.text for m in matching.messages], ["outgoing on mediumslow"]
        )
        self.assertEqual(mismatched.messages, ())

    def test_load_older_page_also_respects_channel_key(self) -> None:
        for index in range(60):
            self.store.add_incoming(
                packet_id=index + 1,
                node_id="!node",
                sender_name="Node",
                sender_short_name="NODE",
                channel_index=0,
                text=f"mediumslow {index}",
                radio_rx_at=100.0 + index,
                received_at=100.0 + index,
                channel_key="id:mediumslow",
            )
        self.store.add_incoming(
            packet_id=9999,
            node_id="!oldnode",
            sender_name="Old Node",
            sender_short_name="OLD",
            channel_index=0,
            text="stale longfast",
            radio_rx_at=1.0,
            received_at=1.0,
            channel_key="id:longfast",
        )

        recent = self.store.load_recent_page(
            channel_index=0, channel_key="id:mediumslow", limit=50
        )
        older = self.store.load_older_page(
            recent.messages[0].id,
            channel_index=0,
            channel_key="id:mediumslow",
            limit=50,
        )

        self.assertNotIn("stale longfast", [m.text for m in older.messages])


class LocalProfileTests(unittest.TestCase):
    """CHAT history is scoped by an explicit LOCAL HISTORY PROFILE.

    A profile is (canonical local node ID + canonical SHORT NAME). The same
    node id under different short names (POLY vs SOHO vs 1234) is a distinct
    namespace -- histories are NEVER merged. Legacy/unowned rows (profile_key
    NULL) are preserved but never returned (preserve-but-hide). LIVE renames
    either rekey the active profile's history (target pairing new) or switch
    to the existing pairing (target known) -- never merge.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "chat.db"
        self.store = ChatStore.open(self.path)
        self.addCleanup(self.store.close)

    def _write_channel(self, text: str, key: str = "id:primary") -> None:
        self.store.add_incoming(
            packet_id=hash(text) % 1_000_000,
            node_id="!a11ce001",
            sender_name="Alice Trail",
            sender_short_name="ALCE",
            channel_index=0,
            text=text,
            radio_rx_at=100.0,
            received_at=100.0,
            channel_key=key,
        )

    def _write_dm(self, text: str, dm: str = "!a11ce001") -> None:
        self.store.add_incoming(
            packet_id=hash(text) % 1_000_000,
            node_id=dm,
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text=text,
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id=dm,
        )

    def test_profile_key_canonicalizes_node_and_short_name(self) -> None:
        from chat_store import canonical_profile_key, canonical_short_name, split_profile_key
        # node id canonicalized; short name stripped + uppercased.
        self.assertEqual(canonical_profile_key("!12345678", "  poly "), "!12345678:POLY")
        # A hex string that contains an a-f letter is unambiguously hex.
        self.assertEqual(canonical_profile_key("a11ce001", "poly"), "!a11ce001:POLY")
        self.assertEqual(canonical_short_name("poly"), "POLY")
        self.assertEqual(canonical_short_name(None), "")
        # no usable node id -> None (no authority).
        self.assertIsNone(canonical_profile_key(None, "POLY"))
        # split round-trips the node half.
        node, short = split_profile_key("!12345678:POLY")
        self.assertEqual(node, "!12345678")
        self.assertEqual(short, "POLY")

    def test_blank_or_missing_short_name_never_yields_a_node_only_profile(self) -> None:
        from chat_store import canonical_profile_key
        # blank / whitespace-only / None SHORT NAME is unresolved identity
        # and must NOT build a node-only "!12345678:" profile.
        self.assertIsNone(canonical_profile_key("!12345678", ""))
        self.assertIsNone(canonical_profile_key("!12345678", "   "))
        self.assertIsNone(canonical_profile_key("!12345678", None))
        # A real factory/default short name ("1234") is valid and distinct.
        self.assertEqual(canonical_profile_key("!12345678", "1234"), "!12345678:1234")

    def test_node_id_normalization_has_no_decimal_hex_ambiguity(self) -> None:
        from chat_store import canonical_profile_key, normalize_profile_node_id
        # Canonical "!xxxxxxxx" hex form is accepted (even if all digits, the
        # "!" prefix makes it unambiguously a node id).
        self.assertEqual(normalize_profile_node_id("!a11ce001"), "!a11ce001")
        self.assertEqual(normalize_profile_node_id("!12345678"), "!12345678")
        # A BARE digit-only string is ambiguous -> rejected (no two readings).
        self.assertIsNone(normalize_profile_node_id("12345678"))
        # A hex string with an a-f letter (no prefix) is unambiguously hex.
        self.assertEqual(normalize_profile_node_id("a11ce001"), "!a11ce001")
        # non-hex garbage rejected.
        self.assertIsNone(normalize_profile_node_id("ZZZZZZZZ"))
        self.assertIsNone(canonical_profile_key("not-a-node", "POLY"))

    def test_profiles_are_isolated_by_short_name_not_node_id_alone(self) -> None:
        # !aaaa + POLY vs !aaaa + SOHO vs !aaaa + 1234 -> three namespaces.
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self._write_channel("POLY history")
        self.store.set_active_profile("!aaaaaaaa:SOHO")
        self._write_channel("SOHO history")
        self.store.set_active_profile("!aaaaaaaa:1234")
        self._write_channel("1234 history")

        self.store.set_active_profile("!aaaaaaaa:POLY")
        self.assertEqual(
            [m.text for m in self.store.load_recent_page(channel_index=0, channel_key="id:primary").messages],
            ["POLY history"],
        )
        self.store.set_active_profile("!aaaaaaaa:SOHO")
        self.assertEqual(
            [m.text for m in self.store.load_recent_page(channel_index=0, channel_key="id:primary").messages],
            ["SOHO history"],
        )
        self.store.set_active_profile("!aaaaaaaa:1234")
        self.assertEqual(
            [m.text for m in self.store.load_recent_page(channel_index=0, channel_key="id:primary").messages],
            ["1234 history"],
        )

    def test_same_short_name_case_is_same_profile(self) -> None:
        # The APP canonicalizes the short name (strip + uppercase) before
        # binding, so "poly" and "POLY" resolve to the SAME profile key.
        from chat_store import canonical_profile_key
        low = canonical_profile_key("!aaaaaaaa", "poly")
        self.assertEqual(low, "!aaaaaaaa:POLY")
        self.store.set_active_profile(low)
        self._write_channel("lower history")
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self.assertIn(
            "lower history",
            [m.text for m in self.store.load_recent_page(channel_index=0, channel_key="id:primary").messages],
        )

    def test_dm_history_isolated_per_profile(self) -> None:
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self._write_dm("poly dm")
        self.store.set_active_profile("!aaaaaaaa:SOHO")
        self._write_dm("soho dm")
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self.assertEqual(
            [m.text for m in self.store.load_recent_dm_page("!a11ce001").messages],
            ["poly dm"],
        )
        self.store.set_active_profile("!aaaaaaaa:SOHO")
        self.assertEqual(
            [m.text for m in self.store.load_recent_dm_page("!a11ce001").messages],
            ["soho dm"],
        )

    def test_dm_conversation_list_is_per_profile(self) -> None:
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self._write_dm("poly dm", dm="!a11ce001")
        self.store.set_active_profile("!aaaaaaaa:SOHO")
        self.assertEqual(self.store.list_dm_conversations(), [])
        self._write_dm("soho dm", dm="!b1111111")
        self.assertEqual(
            [c[0] for c in self.store.list_dm_conversations()],
            ["!b1111111"],
        )

    def test_legacy_null_rows_are_preserved_but_hidden(self) -> None:
        # Write before any profile bound -> profile_key stays NULL.
        self.store.add_incoming(
            packet_id=99, node_id="!old", sender_name="Old",
            sender_short_name="OLD", channel_index=0, text="legacy unowned",
            radio_rx_at=100.0, received_at=100.0, channel_key="id:primary",
        )
        self.store.set_active_profile("!aaaaaaaa:POLY")
        page = self.store.load_recent_page(channel_index=0, channel_key="id:primary")
        self.assertEqual([m.text for m in page.messages], [])
        # The legacy row is still physically present, unowned.
        self.assertEqual(
            self.store._connection.execute(
                "SELECT local_node_id, profile_key FROM messages"
            ).fetchone()["profile_key"],
            None,
        )

    def test_unknown_unresolved_profile_reads_nothing(self) -> None:
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self._write_channel("poly")
        self.store.set_active_profile(None)
        page = self.store.load_recent_page(channel_index=0, channel_key="id:primary")
        self.assertTrue(not page.messages)

    def test_new_packets_are_written_into_the_active_profile(self) -> None:
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self._write_channel("new packet")
        self.assertEqual(self.store._connection.execute(
            "SELECT profile_key FROM messages"
        ).fetchone()["profile_key"], "!aaaaaaaa:POLY")

    def test_dedup_is_profile_aware(self) -> None:
        # The SAME remote packet id under two different profiles are two rows.
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self.store.add_incoming(
            packet_id=777, node_id="!bbbb", sender_name="B", sender_short_name="B",
            channel_index=0, text="poly copy", radio_rx_at=100.0, received_at=100.0,
            channel_key="id:primary",
        )
        self.store.set_active_profile("!aaaaaaaa:SOHO")
        r = self.store.add_incoming(
            packet_id=777, node_id="!bbbb", sender_name="B", sender_short_name="B",
            channel_index=0, text="soho copy", radio_rx_at=100.0, received_at=100.0,
            channel_key="id:primary",
        )
        self.assertTrue(r.inserted)

    def test_live_rename_new_target_rekeys_history(self) -> None:
        # CASE 1: rename POLY -> JUNK when JUNK is unknown migrates the rows.
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self._write_channel("history follows")
        self.store.ensure_profile("!aaaaaaaa", "POLY")
        self.store.migrate_profile_association("!aaaaaaaa:POLY", "!aaaaaaaa:JUNK")
        self.store.set_active_profile("!aaaaaaaa:JUNK")
        self.assertEqual(
            [m.text for m in self.store.load_recent_page(channel_index=0, channel_key="id:primary").messages],
            ["history follows"],
        )
        # Original POLY pairing is now empty (never merged/duplicated).
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self.assertEqual(
            [m.text for m in self.store.load_recent_page(channel_index=0, channel_key="id:primary").messages],
            [],
        )

    def test_live_rename_to_existing_profile_switches_not_migrates(self) -> None:
        # CASE 2: rename SOHO -> POLY; POLY already exists with History A.
        self.store.set_active_profile("!aaaaaaaa:POLY")
        self._write_channel("A history")
        self.store.ensure_profile("!aaaaaaaa", "POLY")
        self.store.set_active_profile("!aaaaaaaa:SOHO")
        self._write_channel("B history")
        self.store.ensure_profile("!aaaaaaaa", "SOHO")
        # Live rename SOHO -> POLY: switch (do not migrate B into A).
        self.store.switch_profile("!aaaaaaaa:POLY")
        self.assertEqual(
            [m.text for m in self.store.load_recent_page(channel_index=0, channel_key="id:primary").messages],
            ["A history"],
        )
        # B stays stored under SOHO.
        self.store.set_active_profile("!aaaaaaaa:SOHO")
        self.assertEqual(
            [m.text for m in self.store.load_recent_page(channel_index=0, channel_key="id:primary").messages],
            ["B history"],
        )

    def test_profiles_registry_persists_across_reopen(self) -> None:
        self.store.ensure_profile("!aaaaaaaa", "POLY")
        self.store.ensure_profile("!aaaaaaaa", "SOHO")
        keys = set(self.store.list_profile_keys())
        self.assertEqual(keys, {"!aaaaaaaa:POLY", "!aaaaaaaa:SOHO"})
        self.store.close()
        reopened = ChatStore.open(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(
            set(reopened.list_profile_keys()),
            {"!aaaaaaaa:POLY", "!aaaaaaaa:SOHO"},
        )


if __name__ == "__main__":
    unittest.main()
