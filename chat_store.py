"""Small SQLite persistence boundary for MeshtasticPass CHAT history."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from threading import RLock


SCHEMA_VERSION = 2
DEFAULT_HISTORY_LIMIT = 100
OLDER_HISTORY_PAGE_SIZE = 50


class ChatStoreError(Exception):
    """Raised when CHAT history cannot be read or safely updated."""


@dataclass(frozen=True)
class StoredMessage:
    id: int
    direction: str
    packet_id: int | None
    node_id: str | None
    sender_name: str | None
    sender_short_name: str | None
    channel_index: int
    text: str
    origin_sent_at: float | None
    radio_rx_at: float | None
    received_at: float
    local_sent_at: float | None
    delivery_state: str | None
    created_at: float

    @property
    def message_time(self) -> float | None:
        """Return a truthful message clock, separate from local receipt time."""
        if self.direction == "outgoing":
            return self.local_sent_at
        return self.origin_sent_at or self.radio_rx_at

    @property
    def order_key(self) -> tuple[float, float, int]:
        """Stable chronology; untimed packets fall back to arrival order."""
        return (self.message_time or self.received_at, self.received_at, self.id)


@dataclass(frozen=True)
class InsertResult:
    message_id: int
    inserted: bool


@dataclass(frozen=True)
class HistoryPage:
    """One bounded chronological page plus whether older rows remain."""

    messages: tuple[StoredMessage, ...]
    has_older: bool


@dataclass(frozen=True)
class StoredSendAttempt:
    id: int
    message_id: int
    packet_id: int | None
    state: str
    started_at: float
    completed_at: float | None
    error: str | None


def default_chat_db_path() -> Path:
    """Return the XDG-aware user data path without touching the filesystem."""
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local/share"
    return root / "meshtasticpass" / "chat.db"


class ChatStore:
    """Own one SQLite connection and all CHAT persistence SQL."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._connection = connection
        self.path = path
        self._lock = RLock()
        self._closed = False

    @classmethod
    def open(cls, path: Path | str | None = None) -> "ChatStore":
        database_path = Path(path) if path is not None else default_chat_db_path()
        connection: sqlite3.Connection | None = None
        try:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(database_path, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            store = cls(connection, database_path)
            store._create_schema()
            store.reconcile_abandoned_sending()
            return store
        except (OSError, sqlite3.DatabaseError, ChatStoreError) as error:
            if connection is not None:
                connection.close()
            if isinstance(error, ChatStoreError):
                raise
            raise ChatStoreError(f"Could not open CHAT history: {error}") from error

    def reconcile_abandoned_sending(self) -> int:
        """Rewrite every persisted outgoing SENDING row to INTERRUPTED,

        directly in SQLite. Called once, automatically, by open() --
        before any caller can hydrate history from this store.

        This is the authoritative fix for a message stuck on SENDING
        forever: a SENDING row already persisted at the moment a store
        is opened cannot belong to the process now opening it (that
        process has not attempted a send yet), so it is abandoned state
        left behind by a previous process -- regardless of whether that
        process shut down gracefully, crashed, lost power, or predates
        this app version, and regardless of channel, pagination, or
        whether anything currently loads it into memory. Covers every
        row in the table in one UPDATE, not just whatever happens to be
        mounted in a widget or held in an in-memory ChatEntry.

        Never marks SENT/HEARD -- this store has no way to know the
        real send outcome, only that nothing is tracking the attempt
        anymore. Never retransmits -- this issues no radio traffic, it
        only rewrites rows. This is a one-time startup pass, not a
        write trigger: a message legitimately sent later in the same
        process, by the same open store, is untouched (reconciliation
        has already finished by the time that INSERT happens).

        Also reconciles any lingering send_attempts row for those
        messages, so the whole persisted record stays internally
        consistent even though nothing currently reads that table at
        runtime.

        Returns the number of `messages` rows rewritten (for logging
        and tests).

        Assumes at most one ChatStore is ever open against a given
        database file at a time -- true of how the app itself uses
        this (main() opens exactly one, for the process's whole
        lifetime). Opening a SECOND store against a database another
        still-running process/store already owns would incorrectly
        interrupt a send that store has legitimately in flight, since
        this method cannot distinguish "abandoned by a dead process"
        from "owned by a live one" other than by that single-open
        assumption. A read-only inspection tool (see
        inspect_chat_store.py) must never call open() on a database
        that might still be live for exactly this reason.
        """
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE messages
                SET delivery_state = 'INTERRUPTED'
                WHERE direction = 'outgoing'
                    AND delivery_state = 'SENDING'
                """
            )
            reconciled = cursor.rowcount
            connection.execute(
                """
                UPDATE send_attempts
                SET state = 'INTERRUPTED'
                WHERE state = 'SENDING'
                    AND message_id IN (
                        SELECT id FROM messages WHERE direction = 'outgoing'
                    )
                """
            )
            return reconciled

    def add_incoming(
        self,
        *,
        packet_id: int | None,
        node_id: str,
        sender_name: str | None,
        sender_short_name: str | None,
        channel_index: int,
        text: str,
        radio_rx_at: float | None,
        received_at: float,
        origin_sent_at: float | None = None,
    ) -> InsertResult:
        """Persist one incoming packet, deduplicating stable packet identities."""
        created_at = received_at
        sql = """
            INSERT OR IGNORE INTO messages (
                direction, packet_id, node_id, sender_name, sender_short_name,
                channel_index, text, origin_sent_at, radio_rx_at, received_at, local_sent_at,
                delivery_state, created_at
            ) VALUES ('incoming', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """
        with self._transaction() as connection:
            cursor = connection.execute(
                sql,
                (
                    packet_id,
                    node_id,
                    sender_name,
                    sender_short_name,
                    channel_index,
                    text,
                    origin_sent_at,
                    radio_rx_at,
                    received_at,
                    created_at,
                ),
            )
            if cursor.rowcount:
                return InsertResult(int(cursor.lastrowid), True)
            row = connection.execute(
                """
                SELECT id FROM messages
                WHERE direction = 'incoming' AND node_id = ?
                    AND packet_id = ? AND channel_index = ?
                """,
                (node_id, packet_id, channel_index),
            ).fetchone()
            if row is None:
                raise ChatStoreError("Incoming message was not stored.")
            return InsertResult(int(row["id"]), False)

    def add_outgoing(
        self,
        *,
        text: str,
        channel_index: int,
        local_sent_at: float,
        delivery_state: str,
    ) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    direction, packet_id, node_id, sender_name,
                    sender_short_name, channel_index, text, origin_sent_at, radio_rx_at,
                    received_at, local_sent_at, delivery_state, created_at
                ) VALUES ('outgoing', NULL, NULL, 'YOU', NULL, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    channel_index,
                    text,
                    local_sent_at,
                    local_sent_at,
                    delivery_state,
                    local_sent_at,
                ),
            )
            return int(cursor.lastrowid)

    def add_send_attempt(
        self,
        message_id: int,
        started_at: float,
        initial_state: str = "SENDING",
    ) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO send_attempts (
                    message_id, packet_id, state, started_at, completed_at, error
                ) VALUES (?, NULL, ?, ?, NULL, NULL)
                """,
                (message_id, initial_state, started_at),
            )
            return int(cursor.lastrowid)

    def update_delivery_state(
        self,
        message_id: int,
        state: str,
        *,
        attempt_id: int | None = None,
        packet_id: int | None = None,
        error: str | None = None,
        completed_at: float | None = None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE messages
                SET delivery_state = ?, packet_id = COALESCE(?, packet_id)
                WHERE id = ?
                """,
                (state, packet_id, message_id),
            )
            if attempt_id is not None:
                connection.execute(
                    """
                    UPDATE send_attempts
                    SET state = ?, packet_id = COALESCE(?, packet_id),
                        completed_at = ?, error = ?
                    WHERE id = ? AND message_id = ?
                    """,
                    (
                        state,
                        packet_id,
                        completed_at,
                        error,
                        attempt_id,
                        message_id,
                    ),
                )

    def load_recent(
        self,
        channel_index: int = 0,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> list[StoredMessage]:
        return list(self.load_recent_page(channel_index, limit).messages)

    def load_recent_page(
        self,
        channel_index: int = 0,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> HistoryPage:
        """Load only the newest bounded page, in chronological order."""
        self._validate_history_limit(limit)
        try:
            with self._lock:
                self._ensure_open()
                rows = self._connection.execute(
                    """
                    SELECT id, direction, packet_id, node_id, sender_name,
                    sender_short_name, channel_index, text, origin_sent_at, radio_rx_at,
                    received_at, local_sent_at, delivery_state, created_at
                    FROM (
                        SELECT * FROM messages
                        WHERE channel_index = ?
                        ORDER BY
                            COALESCE(origin_sent_at, radio_rx_at, local_sent_at, received_at) DESC,
                            received_at DESC,
                            id DESC
                        LIMIT ?
                    )
                    ORDER BY
                        COALESCE(origin_sent_at, radio_rx_at, local_sent_at, received_at) ASC,
                        received_at ASC,
                        id ASC
                    """,
                    (channel_index, limit + 1),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not load CHAT history: {error}") from error
        has_older = len(rows) > limit
        selected = rows[-limit:]
        return HistoryPage(
            tuple(StoredMessage(**dict(row)) for row in selected),
            has_older,
        )

    def latest_incoming_message_at(self) -> dict[str, float]:
        """Most recent truthful incoming-message timestamp per node ID.

        Persisted history is authoritative for this, not whatever page is
        currently mounted in memory: a node's last message may be far older
        than any bounded/loaded CHAT window. Returns one aggregated
        (node_id, MAX(timestamp)) row per node via SQL -- it never loads
        message rows into memory, so this stays cheap regardless of history
        size. Timestamp precedence matches StoredMessage.message_time for
        incoming messages (origin_sent_at, else radio_rx_at); a message with
        neither is excluded rather than guessed from local receipt time.
        Node IDs are lowercased to match the app's case-insensitive
        node-ID comparison convention.
        """
        try:
            with self._lock:
                self._ensure_open()
                rows = self._connection.execute(
                    """
                    SELECT
                        LOWER(node_id) AS node_id,
                        MAX(COALESCE(origin_sent_at, radio_rx_at)) AS message_time
                    FROM messages
                    WHERE direction = 'incoming'
                        AND node_id IS NOT NULL
                        AND COALESCE(origin_sent_at, radio_rx_at) IS NOT NULL
                    GROUP BY LOWER(node_id)
                    """
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(
                f"Could not load latest message activity: {error}"
            ) from error
        return {
            str(row["node_id"]): float(row["message_time"])
            for row in rows
            if str(row["node_id"]).strip()
        }

    def load_older_page(
        self,
        before_message_id: int,
        channel_index: int = 0,
        limit: int = OLDER_HISTORY_PAGE_SIZE,
    ) -> HistoryPage:
        """Load a bounded page immediately older than a stable message ID."""
        if (
            isinstance(before_message_id, bool)
            or not isinstance(before_message_id, int)
            or before_message_id <= 0
        ):
            raise ValueError("Message cursor must be a positive integer.")
        self._validate_history_limit(limit)
        try:
            with self._lock:
                self._ensure_open()
                rows = self._connection.execute(
                    """
                    WITH cursor AS (
                        SELECT
                            COALESCE(
                                origin_sent_at, radio_rx_at, local_sent_at, received_at
                            ) AS order_time,
                            received_at AS cursor_received_at,
                            id AS cursor_id
                        FROM messages
                        WHERE id = ? AND channel_index = ?
                    )
                    SELECT messages.id, direction, packet_id, node_id, sender_name,
                        sender_short_name, messages.channel_index, text, origin_sent_at,
                        radio_rx_at, received_at, local_sent_at, delivery_state, created_at
                    FROM messages CROSS JOIN cursor
                    WHERE messages.channel_index = ? AND (
                        COALESCE(origin_sent_at, radio_rx_at, local_sent_at, received_at)
                            < cursor.order_time
                        OR (
                            COALESCE(origin_sent_at, radio_rx_at, local_sent_at, received_at)
                                = cursor.order_time
                            AND received_at < cursor.cursor_received_at
                        )
                        OR (
                            COALESCE(origin_sent_at, radio_rx_at, local_sent_at, received_at)
                                = cursor.order_time
                            AND received_at = cursor.cursor_received_at
                            AND messages.id < cursor.cursor_id
                        )
                    )
                    ORDER BY
                        COALESCE(origin_sent_at, radio_rx_at, local_sent_at, received_at) DESC,
                        received_at DESC,
                        id DESC
                    LIMIT ?
                    """,
                    (
                        before_message_id,
                        channel_index,
                        channel_index,
                        limit + 1,
                    ),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not load older CHAT history: {error}") from error
        has_older = len(rows) > limit
        selected = list(reversed(rows[:limit]))
        return HistoryPage(
            tuple(StoredMessage(**dict(row)) for row in selected),
            has_older,
        )

    def load_oldest_incoming_by_ids(
        self,
        message_ids: set[int],
        *,
        channel_index: int,
    ) -> StoredMessage | None:
        """Return the oldest matching incoming row without loading history."""
        valid_ids = sorted(
            message_id
            for message_id in message_ids
            if isinstance(message_id, int)
            and not isinstance(message_id, bool)
            and message_id > 0
        )
        if not valid_ids:
            return None

        oldest: StoredMessage | None = None
        try:
            with self._lock:
                self._ensure_open()
                # Stay below SQLite's common host-parameter limit while allowing
                # a long-running session to accumulate more than one page of NEW.
                for offset in range(0, len(valid_ids), 500):
                    chunk = valid_ids[offset : offset + 500]
                    placeholders = ", ".join("?" for _ in chunk)
                    row = self._connection.execute(
                        f"""
                        SELECT id, direction, packet_id, node_id, sender_name,
                            sender_short_name, channel_index, text, origin_sent_at,
                            radio_rx_at, received_at, local_sent_at, delivery_state,
                            created_at
                        FROM messages
                        WHERE channel_index = ?
                            AND direction = 'incoming'
                            AND id IN ({placeholders})
                        ORDER BY
                            COALESCE(
                                origin_sent_at, radio_rx_at, local_sent_at, received_at
                            ) ASC,
                            received_at ASC,
                            id ASC
                        LIMIT 1
                        """,
                        (channel_index, *chunk),
                    ).fetchone()
                    if row is not None:
                        candidate = StoredMessage(**dict(row))
                        if oldest is None or candidate.order_key < oldest.order_key:
                            oldest = candidate
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(
                f"Could not locate unread CHAT history: {error}"
            ) from error
        return oldest

    def load_send_attempts(self, message_id: int) -> list[StoredSendAttempt]:
        """Return transmission attempts for one logical outgoing message."""
        try:
            with self._lock:
                self._ensure_open()
                rows = self._connection.execute(
                    """
                    SELECT id, message_id, packet_id, state, started_at,
                        completed_at, error
                    FROM send_attempts
                    WHERE message_id = ?
                    ORDER BY id ASC
                    """,
                    (message_id,),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not load send attempts: {error}") from error
        return [StoredSendAttempt(**dict(row)) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._connection.close()

    def _create_schema(self) -> None:
        try:
            with self._transaction() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_version (
                        version INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        direction TEXT NOT NULL CHECK (
                            direction IN ('incoming', 'outgoing')
                        ),
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

                    CREATE UNIQUE INDEX IF NOT EXISTS incoming_packet_identity
                    ON messages(node_id, packet_id, channel_index)
                    WHERE direction = 'incoming' AND packet_id IS NOT NULL;

                    CREATE TABLE IF NOT EXISTS send_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id INTEGER NOT NULL REFERENCES messages(id),
                        packet_id INTEGER,
                        state TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        completed_at REAL,
                        error TEXT
                    );
                    """
                )
                row = connection.execute(
                    "SELECT version FROM schema_version LIMIT 1"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO schema_version(version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
                elif int(row["version"]) == 1:
                    columns = {
                        column["name"]
                        for column in connection.execute(
                            "PRAGMA table_info(messages)"
                        ).fetchall()
                    }
                    if "origin_sent_at" not in columns:
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN origin_sent_at REAL"
                        )
                    connection.execute(
                        "UPDATE schema_version SET version = ?",
                        (SCHEMA_VERSION,),
                    )
                elif int(row["version"]) != SCHEMA_VERSION:
                    raise ChatStoreError(
                        f"Unsupported CHAT schema version {row['version']}."
                    )
                # Only after any v1->v2 migration above is origin_sent_at
                # guaranteed to exist on every database this app has ever
                # created, so this index (which references it) is created
                # last rather than in the main script above.
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS incoming_node_message_time
                    ON messages(node_id, origin_sent_at, radio_rx_at)
                    WHERE direction = 'incoming'
                    """
                )
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not initialize CHAT history: {error}") from error

    def _transaction(self):
        return _StoreTransaction(self)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ChatStoreError("CHAT history is closed.")

    @staticmethod
    def _validate_history_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("History limit must be a positive integer.")


class _StoreTransaction:
    def __init__(self, store: ChatStore) -> None:
        self.store = store

    def __enter__(self) -> sqlite3.Connection:
        self.store._lock.acquire()
        try:
            self.store._ensure_open()
        except Exception:
            self.store._lock.release()
            raise
        return self.store._connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is None:
                self.store._connection.commit()
            else:
                self.store._connection.rollback()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not update CHAT history: {error}") from error
        finally:
            self.store._lock.release()
        if exc_type is not None and issubclass(exc_type, sqlite3.DatabaseError):
            raise ChatStoreError(f"Could not update CHAT history: {exc}") from exc
        return False
