"""Small SQLite persistence boundary for MeshtasticPass CHAT history."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from threading import RLock
from time import time


SCHEMA_VERSION = 6
DEFAULT_HISTORY_LIMIT = 100
OLDER_HISTORY_PAGE_SIZE = 50

# Separator that joins the canonical local node ID and the canonical SHORT
# NAME into a single durable profile key. The node-id half is always the
# canonical "!xxxxxxxx" form (never contains ":"), so splitting on the FIRST
# ":" unambiguously recovers the node id; the short-name half is canonicalized
# (stripped, uppercased) and may be empty (a node-only lineage). The profile
# key is the ONE place this composite is built/parsed -- never scattered as
# display strings through the app.
_PROFILE_KEY_SEPARATOR = ":"


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
    # The remote party's canonical node ID for a Direct Message row --
    # NULL for an ordinary channel/broadcast row. Set identically for
    # BOTH directions of one DM conversation (the sender for incoming,
    # the destination for outgoing), so a conversation is always keyed
    # by this single stable value, never by channel_index (see item 2:
    # DM identity must never be a display name). channel_index is still
    # populated on a DM row (0) but carries no meaning there -- every
    # DM query filters by dm_node_id, never channel_index.
    dm_node_id: str | None = None
    # This row's own CHANNEL's stable identity (ChannelInfo.stable_key)
    # at the time it was written -- NULL for a DM row (channel_key
    # carries no meaning there, mirroring how channel_index carries no
    # meaning on a DM row) and also NULL for any row written before
    # this column existed, or while the live radio's real channel
    # identity was not yet known (e.g. the pre-connection placeholder
    # channel list). A NULL channel_key is deliberately treated as
    # "matches any channel_key filter" by load_recent_page/
    # load_older_page (grandfathered) rather than hidden -- there is no
    # way to retroactively recover which physical channel an old NULL
    # row actually belonged to, so the honest choice is to keep
    # showing it rather than silently discard already-collected
    # history. See CHAT channel-history isolation (FINAL MESHTASTIC
    # POLISH pass) and this field's own precedent, dm_node_id.
    channel_key: str | None = None
    # This row's OWNING physical/local radio's canonical node ID. A
    # row belongs to exactly one local radio conversation namespace --
    # CHAT state lives under (canonical local node ID -> conversation
    # identity). Two different radios (POLY vs SOHO) running identical
    # network/channel/PSK/DM settings must never share history, so this
    # is always part of every read filter and every write stamp.
    # NULL means "written before per-radio namespacing existed" or
    # "local identity was not yet resolved when the row was written".
    # The preserve-but-hide migration policy guarantees such a NULL
    # row is NEVER returned by any read (every query filters
    # local_node_id = <resolved id>, and a NULL equality never matches),
    # so pre-namespacing history is kept on disk but never presented as
    # belonging to any radio. Writes that occur before the radio's local
    # node ID is resolved are likewise never surfaced.
    local_node_id: str | None = None
    # This row's owning LOCAL CHAT HISTORY PROFILE key -- the durable
    # composite (canonical canonical local node ID + canonical SHORT NAME)
    # that scopes ALL local CHAT state for one physical/local radio pairing.
    # A profile is the privacy boundary at the LOCAL device: local device
    # access, not a cryptographic radio fingerprint. Two profiles with the
    # SAME node id but DIFFERENT short names (POLY vs SOHO vs 1234) are
    # distinct namespaces that are never merged. NULL means "written before
    # per-profile namespacing existed" (or "local identity not yet resolved
    # when the row was written"); the preserve-but-hide policy guarantees
    # such a row is never returned by any read, so pre-profile history is
    # preserved on disk but never presented as belonging to a profile.
    profile_key: str | None = None

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


def canonical_short_name(short_name: str | None) -> str:
    """Canonicalize a local SHORT NAME for history-profile identity.

    SHORT NAME (not LONG NAME) is part of the local CHAT history profile:
    !1234 + POLY, !1234 + SOHO and !1234 + 1234 are distinct profiles even
    for the same node id. Canonicalization is strip + UPPERCASE, so
    "poly"/"POLY" are the SAME profile (short names are case-insensitive
    labels; a user renaming only the case must never splinter history).
    LONG NAME is never part of the profile -- it is presentation only
    (see canonical_profile_key).

    Returns "" ONLY for a genuinely unusable value (None, or blank/
    whitespace after trimming). A blank/whitespace SHORT NAME is UNRESOLVED
    identity -- it must NOT contribute a node-only profile, so it yields ""
    and canonical_profile_key returns None (never a "!12345678:" key).
    """
    if not isinstance(short_name, str):
        return ""
    normalized = short_name.strip().upper()
    return normalized


def canonical_profile_key(node_id: str | None, short_name: str | None) -> str | None:
    """Build the durable local CHAT history profile key for a radio pairing.

    The profile is (canonical local node ID + canonical SHORT NAME). This is
    the ONE place the composite is built -- never concatenated as display
    strings through the app. Returns None unless BOTH components are usable:

    - the node id normalizes to a canonical "!xxxxxxxx" hex id, AND
    - the SHORT NAME canonicalizes to a NON-EMPTY value.

    A missing/blank/whitespace-only SHORT NAME is unresolved identity, so
    None is returned (a node-only "!12345678:" key is NEVER created). A real
    factory/default short name such as "1234" is valid and yields
    "!12345678:1234".
    """
    normalized = normalize_profile_node_id(node_id)
    if normalized is None:
        return None
    short = canonical_short_name(short_name)
    if not short:
        return None
    return f"{normalized}{_PROFILE_KEY_SEPARATOR}{short}"


def normalize_profile_node_id(node_id: str | None) -> str | None:
    """Canonical "!"-prefixed 8-hex-digit local node id, or None.

    The LOCAL profile identity uses the actual Meshtastic node-ID wire
    representation, normalized consistently to canonical "!xxxxxxxx" (lowercase
    hex). Only the canonical hex form is accepted -- an optional "!" prefix and
    surrounding whitespace are tolerated, but a bare digit-only string is NOT
    interpreted via competing decimal/hex readings. The connected radio's own
    `RadioInfo.node_id` is already canonical, so this is a defensive guard for
    hand-constructed/tests values, never a source of ambiguity.

    Returns None when the id is not usable (blank, non-hex, not a 32-bit
    node id) -- the preserve-but-hide fallback.
    """
    if not isinstance(node_id, str):
        return None
    candidate = node_id.strip()
    had_prefix = candidate.startswith("!")
    if had_prefix:
        candidate = candidate[1:]
    # Canonical hex node-id representation. A "!"-prefixed value is
    # unambiguously a node id (parsed as hex). A BARE digit-only string (no
    # "!" and no a-f letter) is ambiguous under decimal vs hex and is
    # rejected -- one representation, never two competing readings.
    if not candidate or len(candidate) > 8:
        return None
    if not had_prefix and not any(ch in "abcdefABCDEF" for ch in candidate):
        return None
    try:
        value = int(candidate, 16)
    except ValueError:
        return None
    return f"!{value & 0xFFFFFFFF:08x}"


def split_profile_key(profile_key: str | None) -> tuple[str | None, str]:
    """Split a profile key into its (canonical node id, canonical short name).

    Inverse of canonical_profile_key. Malformed/unknown keys yield
    (None, ""). Used only where a profile must be decomposed (e.g. to know
    a rename target's node id); the app never builds profile identity from
    raw display strings.
    """
    if not profile_key:
        return (None, "")
    node_part, _, short_part = profile_key.partition(_PROFILE_KEY_SEPARATOR)
    node_normalized = normalize_profile_node_id(node_part)
    return (node_normalized, short_part)


class ChatStore:
    """Own one SQLite connection and all CHAT persistence SQL."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._connection = connection
        self.path = path
        self._lock = RLock()
        self._closed = False
        # The active LOCAL CHAT HISTORY PROFILE (canonical local node ID +
        # canonical SHORT NAME). Every write is stamped with this and every
        # read is filtered by it, so CHAT state is isolated per (node ID +
        # short name) pairing -- !1234+POLY, !1234+SOHO and !1234+1234 are
        # never merged. None means "local profile not yet resolved": reads
        # surface no history and writes are stored but later hidden (the
        # preserve-but-hide policy -- never present a row as belonging to a
        # profile we cannot prove owned it).
        self._active_profile_key: str | None = None
        # Whether the profile was EVER explicitly bound. A raw ChatStore
        # that never calls set_active_profile (older Store tests, hand-run
        # tools) is left profile-unfiltered so its write-then-read round
        # trips still work exactly as before; once the app resolves a local
        # profile it always binds, turning the profile filter on
        # permanently for the life of this store.
        self._profile_bound: bool = False

    def set_active_profile(self, profile_key: str | None) -> None:
        """Bind this store to one local CHAT history profile.

        `profile_key` is the durable composite built by
        canonical_profile_key(node_id, short_name). Call it once the local
        (node ID + SHORT NAME) profile is resolved (or resolve it to None
        while unknown). The store does not read or write any conversation
        state outside this profile. Switching profiles (a live rename of
        the SHORT NAME, or attaching a different radio) requires nothing
        more than calling this again with the new key -- every subsequent
        read/write is then scoped to the new profile; no other profile's
        rows match, and the two are never merged.
        """
        self._active_profile_key = profile_key
        self._profile_bound = True

    def set_local_profile(self, node_id: str | None, short_name: str | None = None) -> str | None:
        """Convenience: bind the store to the canonical (node ID + SHORT NAME)
        profile -- builds the durable profile key and activates it. Returns
        the profile key (None when the node id is not usable).

        Equivalent to set_active_profile(canonical_profile_key(...)); the app
        and tests use this to bind by identity rather than by an opaque key.
        """
        key = canonical_profile_key(node_id, short_name)
        self.set_active_profile(key)
        return key

    def is_namespace_bound(self) -> bool:
        """Whether this store's per-profile namespace has been set at all.

        A store the app never bound (raw unit tests / hand tools) keeps its
        pre-namespacing behavior (reads unfiltered, writes profile-less);
        once the app resolves a local profile it always binds, turning the
        profile filter on permanently for this store's lifetime.
        """
        return self._profile_bound

    def ensure_profile(self, node_id: str | None, short_name: str | None) -> str | None:
        """Record a known local (node ID + SHORT NAME) profile; return its key.

        Idempotent (INSERT OR IGNORE). Creates a durable
        local_profiles record so a pairing stays independently recoverable
        across restart/rename even before any message row is written under
        it. Returns canonical_profile_key, or None when the node id is not
        usable (no authority to create a profile).
        """
        key = canonical_profile_key(node_id, short_name)
        if key is None:
            return None
        normalized = normalize_profile_node_id(node_id)
        now = time()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO local_profiles
                    (profile_key, node_id, short_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, normalized, canonical_short_name(short_name), now, now),
            )
        return key

    def profile_exists(self, profile_key: str | None) -> bool:
        """Whether a durable profile exists for this exact pairing."""
        if not profile_key:
            return False
        try:
            with self._lock:
                self._ensure_open()
                row = self._connection.execute(
                    "SELECT 1 FROM local_profiles WHERE profile_key = ? LIMIT 1",
                    (profile_key,),
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not look up local profile: {error}") from error
        return row is not None

    def list_profile_keys(self) -> tuple[str, ...]:
        """All known (node ID + SHORT NAME) profile keys, in creation order."""
        try:
            with self._lock:
                self._ensure_open()
                rows = self._connection.execute(
                    "SELECT profile_key FROM local_profiles ORDER BY created_at ASC"
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not list local profiles: {error}") from error
        return tuple(str(row["profile_key"]) for row in rows)

    def _rekey_active_profile_rows(
        self, old_profile_key: str | None, new_profile_key: str
    ) -> None:
        """Move every row currently under `old_profile_key` to `new_profile_key`.

        A LIVE RENAME (target pairing does not exist yet): history follows
        the rename. Atomic and restart-safe (one UPDATE; if the process dies
        the old key simply has no rows). No merge, no duplicate, no delete.
        """
        with self._transaction() as connection:
            connection.execute(
                "UPDATE messages SET profile_key = ? WHERE profile_key = ?",
                (new_profile_key, old_profile_key),
            )
            connection.execute(
                "UPDATE local_profiles SET profile_key = ?, updated_at = ? WHERE profile_key = ?",
                (new_profile_key, time(), old_profile_key),
            )

    def migrate_profile_association(
        self, old_profile_key: str | None, new_profile_key: str
    ) -> None:
        """CASE 1 live rename: rekey the active profile's rows+record to a new key.

        Reused by the app when a live SHORT NAME change targets a pairing that
        does NOT yet exist: the current profile becomes the new pairing, so
        its history follows the rename (never duplicated or merged).
        """
        if old_profile_key == new_profile_key:
            return
        self._rekey_active_profile_rows(old_profile_key, new_profile_key)

    def switch_profile(self, profile_key: str | None) -> None:
        """CASE 2 live rename: switch to an ALREADY-EXISTING pairing.

        No data is migrated or merged -- the current profile's rows stay
        under their own key; the store now reads/writes the existing target
        profile. (Also the ordinary cross-radio / fresh-profile switch.)
        """
        self.set_active_profile(profile_key)

    @property
    def _ns_filter(self) -> str:
        """SQL fragment narrowing a read to the bound local profile.

        Empty (no filter) when the profile was never explicitly bound (a
        raw Store used directly without the app -- its write-then-read
        round trips must behave exactly as before namespacing). Once
        bound, every read is narrowed to profile_key = <active key>; a
        None key matches nothing, so an unresolved profile surfaces no
        state (preserve-but-hide).
        """
        return " AND profile_key = ?" if self._profile_bound else ""

    def _ns_params(self) -> tuple:
        """Parameter values paired with _ns_filter (emptied when unbound)."""
        return (self._active_profile_key,) if self._profile_bound else ()

    def _ns_value(self) -> str | None:
        """The profile key stamped on a newly-written row.

        The active local profile once bound; None otherwise (a raw Store
        never bound by the app writes profile-less rows that a later
        bound store treats as unowned and hides -- the preserve-but-hide
        compatibility policy).
        """
        return self._active_profile_key if self._profile_bound else None

    def _ns_node_id(self) -> str | None:
        """The canonical node-id half of the active profile, for tracing.

        Kept purely for backward-compatibility/audit of the legacy
        local_node_id column; the authoritative scoping key is profile_key
        (_ns_value). Returns None when unbound.
        """
        if not self._profile_bound or not self._active_profile_key:
            return None
        node_part, _short = split_profile_key(self._active_profile_key)
        return node_part

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
        dm_node_id: str | None = None,
        channel_key: str | None = None,
    ) -> InsertResult:
        """Persist one incoming packet, deduplicating stable packet identities.

        `dm_node_id` set (to the sender's own canonical ID) marks this
        row as belonging to a Direct Message conversation rather than a
        channel -- see StoredMessage.dm_node_id. `channel_key` should
        stay None for a DM row -- see StoredMessage.channel_key.
        """
        created_at = received_at
        sql = """
            INSERT OR IGNORE INTO messages (
                direction, packet_id, node_id, sender_name, sender_short_name,
                channel_index, text, origin_sent_at, radio_rx_at, received_at, local_sent_at,
                delivery_state, created_at, dm_node_id, channel_key, local_node_id, profile_key
            ) VALUES ('incoming', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?)
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
                    dm_node_id,
                    channel_key,
                    self._ns_node_id(),
                    self._ns_value(),
                ),
            )
            if cursor.rowcount:
                return InsertResult(int(cursor.lastrowid), True)
            row = connection.execute(
                f"""
                SELECT id FROM messages
                WHERE direction = 'incoming' AND node_id = ?
                    AND packet_id = ? AND channel_index = ? AND dm_node_id IS ?{self._ns_filter}
                """,
                (node_id, packet_id, channel_index, dm_node_id, *self._ns_params()),
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
        dm_node_id: str | None = None,
        channel_key: str | None = None,
    ) -> int:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    direction, packet_id, node_id, sender_name,
                    sender_short_name, channel_index, text, origin_sent_at, radio_rx_at,
                    received_at, local_sent_at, delivery_state, created_at, dm_node_id,
                    channel_key, local_node_id, profile_key
                ) VALUES ('outgoing', NULL, NULL, 'YOU', NULL, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_index,
                    text,
                    local_sent_at,
                    local_sent_at,
                    delivery_state,
                    local_sent_at,
                    dm_node_id,
                    channel_key,
                    self._ns_node_id(),
                    self._ns_value(),
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

    def update_message_chronology(self, message_id: int, local_sent_at: float) -> None:
        """Move an outgoing message's effective chronological position.

        Updates ONLY `local_sent_at` -- the field StoredMessage.
        message_time/order_key actually read for an outgoing row -- so
        the transcript re-sorts to reflect when this message actually,
        successfully re-entered the mesh (see app.py's manual-resend
        handling). `created_at` is never touched: it remains the true,
        original moment this message was first composed, regardless of
        how many times it was later retried or when a retry succeeded.
        """
        with self._transaction() as connection:
            connection.execute(
                "UPDATE messages SET local_sent_at = ? WHERE id = ? AND direction = 'outgoing'",
                (local_sent_at, message_id),
            )

    def delete_message(self, message_id: int) -> None:
        """Permanently remove one message and its send-attempt history.

        Local delete only (see app.py's DEL action) -- never sends or
        implies anything to the mesh. Deletes by the stable message_id
        primary key alone, so another message with identical text, a
        resend's own separate row, or any other message near the same
        timestamp is never touched. A no-op if the id no longer exists
        (e.g. DEL pressed twice in quick succession).
        """
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM send_attempts WHERE message_id = ?", (message_id,)
            )
            connection.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    def delete_dm_conversation(self, dm_node_id: str) -> int:
        """Permanently remove one WHOLE DM conversation and its history.

        Local delete only (see app.py's CTRL+D action) -- never sends or
        implies anything to the mesh, and never touches channel history
        (every matching row has dm_node_id set, so the WHERE clause below
        can never match an ordinary channel/broadcast row whose dm_node_id
        is NULL). Keyed by the remote party's canonical node ID -- never a
        display name -- exactly like every other DM query in this store.
        Deletes every message in BOTH directions of that one conversation,
        plus their send-attempt rows, in one transaction. A no-op (returns
        0) if no such conversation exists.

        Returns the number of `messages` rows removed (for the caller's
        selector/unread bookkeeping and for tests).
        """
        with self._transaction() as connection:
            connection.execute(
                f"""
                DELETE FROM send_attempts
                WHERE message_id IN (
                    SELECT id FROM messages WHERE dm_node_id = ?{self._ns_filter}
                )
                """,
                (dm_node_id, *self._ns_params()),
            )
            cursor = connection.execute(
                f"DELETE FROM messages WHERE dm_node_id = ?{self._ns_filter}",
                (dm_node_id, *self._ns_params()),
            )
            return cursor.rowcount

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
        channel_key: str | None = None,
    ) -> HistoryPage:
        """Load only the newest bounded page, in chronological order.

        `channel_key` (CHAT channel-history isolation) is the LIVE
        radio's current stable identity for `channel_index`. When it is
        a real value, only rows recorded under that same key -- plus
        any legacy row whose channel_key is still NULL, grandfathered
        rather than hidden (see StoredMessage.channel_key) -- are
        returned, so a same-slot radio reconfiguration (e.g. index 0
        LongFast -> MediumSlow) can no longer surface the OTHER
        channel's history under this one. When `channel_key` is None
        (identity not yet known -- e.g. this app's own pre-connection
        placeholder), no channel_key filtering is applied at all: there
        is nothing trustworthy to filter against yet, so every row for
        this index is returned exactly like before this column existed.
        """
        self._validate_history_limit(limit)
        try:
            with self._lock:
                self._ensure_open()
                rows = self._connection.execute(
                    f"""
                    SELECT id, direction, packet_id, node_id, sender_name,
                    sender_short_name, channel_index, text, origin_sent_at, radio_rx_at,
                    received_at, local_sent_at, delivery_state, created_at, dm_node_id,
                    channel_key, local_node_id, profile_key
                    FROM (
                        SELECT * FROM messages
                        WHERE channel_index = ? AND dm_node_id IS NULL{self._ns_filter}
                            AND (? IS NULL OR channel_key = ? OR channel_key IS NULL)
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
                    (channel_index, *self._ns_params(), channel_key, channel_key, limit + 1),
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
                    f"""
                    SELECT
                        LOWER(node_id) AS node_id,
                        MAX(COALESCE(origin_sent_at, radio_rx_at)) AS message_time
                    FROM messages
                    WHERE direction = 'incoming'
                        AND node_id IS NOT NULL
                        AND dm_node_id IS NULL{self._ns_filter}
                        AND COALESCE(origin_sent_at, radio_rx_at) IS NOT NULL
                    GROUP BY LOWER(node_id)
                    """,
                    self._ns_params(),
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
        channel_key: str | None = None,
    ) -> HistoryPage:
        """Load a bounded page immediately older than a stable message ID.

        `channel_key` filters exactly like load_recent_page's own --
        see its docstring.
        """
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
                    f"""
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
                        radio_rx_at, received_at, local_sent_at, delivery_state, created_at,
                        dm_node_id, channel_key, local_node_id, profile_key
                    FROM messages CROSS JOIN cursor
                    WHERE messages.channel_index = ? AND messages.dm_node_id IS NULL{self._ns_filter}
                        AND (? IS NULL OR messages.channel_key = ? OR messages.channel_key IS NULL)
                        AND (
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
                        *self._ns_params(),
                        channel_key,
                        channel_key,
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

    def load_recent_dm_page(
        self,
        dm_node_id: str,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> HistoryPage:
        """Load the newest bounded page of ONE DM conversation, keyed

        entirely by the remote party's stable node ID -- never
        channel_index, which carries no meaning on a DM row (see
        StoredMessage.dm_node_id). Mirrors load_recent_page's own
        ordering/paging contract exactly.
        """
        self._validate_history_limit(limit)
        try:
            with self._lock:
                self._ensure_open()
                rows = self._connection.execute(
                    f"""
                    SELECT id, direction, packet_id, node_id, sender_name,
                    sender_short_name, channel_index, text, origin_sent_at, radio_rx_at,
                    received_at, local_sent_at, delivery_state, created_at, dm_node_id,
                    local_node_id, profile_key
                    FROM (
                        SELECT * FROM messages
                        WHERE dm_node_id = ?{self._ns_filter}
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
                    (dm_node_id, *self._ns_params(), limit + 1),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not load DM history: {error}") from error
        has_older = len(rows) > limit
        selected = rows[-limit:]
        return HistoryPage(
            tuple(StoredMessage(**dict(row)) for row in selected),
            has_older,
        )

    def load_older_dm_page(
        self,
        before_message_id: int,
        dm_node_id: str,
        limit: int = OLDER_HISTORY_PAGE_SIZE,
    ) -> HistoryPage:
        """Load a bounded page immediately older than a stable message ID,

        within ONE DM conversation. Mirrors load_older_page's own cursor
        contract, keyed by dm_node_id instead of channel_index.
        """
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
                    f"""
                    WITH cursor AS (
                        SELECT
                            COALESCE(
                                origin_sent_at, radio_rx_at, local_sent_at, received_at
                            ) AS order_time,
                            received_at AS cursor_received_at,
                            id AS cursor_id
                        FROM messages
                        WHERE id = ? AND dm_node_id = ?
                    )
                    SELECT messages.id, direction, packet_id, node_id, sender_name,
                        sender_short_name, messages.channel_index, text, origin_sent_at,
                        radio_rx_at, received_at, local_sent_at, delivery_state, created_at,
                        dm_node_id, local_node_id, profile_key
                    FROM messages CROSS JOIN cursor
                    WHERE messages.dm_node_id = ?{self._ns_filter} AND (
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
                        dm_node_id,
                        dm_node_id,
                        *self._ns_params(),
                        limit + 1,
                    ),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not load older DM history: {error}") from error
        has_older = len(rows) > limit
        selected = list(reversed(rows[:limit]))
        return HistoryPage(
            tuple(StoredMessage(**dict(row)) for row in selected),
            has_older,
        )

    def list_dm_conversations(self) -> list[tuple[str, float]]:
        """Return (dm_node_id, latest_message_time) for every DM

        conversation with at least one persisted message, sorted by
        most-recent activity descending (item 8's own preferred sort).
        `latest_message_time` matches each row's own StoredMessage.
        message_time precedence (outgoing: local_sent_at; incoming:
        origin_sent_at, else radio_rx_at, else received_at as a last
        resort) taken as a MAX over the whole conversation -- never
        loads message rows into memory.
        """
        try:
            with self._lock:
                self._ensure_open()
                rows = self._connection.execute(
                    f"""
                    SELECT
                        dm_node_id,
                        MAX(
                            CASE
                                WHEN direction = 'outgoing' THEN
                                    COALESCE(local_sent_at, received_at)
                                ELSE
                                    COALESCE(origin_sent_at, radio_rx_at, received_at)
                            END
                        ) AS latest_message_time
                    FROM messages
                    WHERE dm_node_id IS NOT NULL{self._ns_filter}
                    GROUP BY dm_node_id
                    ORDER BY latest_message_time DESC
                    """,
                    self._ns_params(),
                ).fetchall()
        except sqlite3.DatabaseError as error:
            raise ChatStoreError(f"Could not list DM conversations: {error}") from error
        return [
            (str(row["dm_node_id"]), float(row["latest_message_time"]))
            for row in rows
            if str(row["dm_node_id"]).strip()
        ]

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
                            created_at, dm_node_id, channel_key, local_node_id, profile_key
                        FROM messages
                        WHERE channel_index = ?
                            AND direction = 'incoming'{self._ns_filter}
                            AND id IN ({placeholders})
                        ORDER BY
                            COALESCE(
                                origin_sent_at, radio_rx_at, local_sent_at, received_at
                            ) ASC,
                            received_at ASC,
                            id ASC
                        LIMIT 1
                        """,
                        (channel_index, *self._ns_params(), *chunk),
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
                        created_at REAL NOT NULL,
                        dm_node_id TEXT,
                        channel_key TEXT,
                        local_node_id TEXT,
                        profile_key TEXT
                    );

                    CREATE TABLE IF NOT EXISTS local_profiles (
                        profile_key TEXT PRIMARY KEY,
                        node_id TEXT NOT NULL,
                        short_name TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );

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
                    # A brand-new database: the executescript CREATE TABLE
                    # above already has every column/index this version
                    # needs, so there is nothing to migrate.
                    connection.execute(
                        "INSERT INTO schema_version(version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
                else:
                    current_version = int(row["version"])
                    # Any version from 1 up to (and including) the current
                    # SCHEMA_VERSION is a supported database that must be
                    # migrated in place (each historical bump below adds its
                    # own column); only a database claiming a FUTURE version
                    # is genuinely unsupported. A literal tuple like
                    # "(1, 2, 3, SCHEMA_VERSION)" silently breaks the moment
                    # a bump is applied without updating it -- e.g. the v4 ->
                    # v5 per-radio bump left "4" out, so an existing valid
                    # v4 database raised "Unsupported CHAT schema version 4"
                    # instead of migrating. Ranging over every prior version
                    # makes the migration path correct and restart-safe no
                    # matter how many times the version is advanced.
                    if not (1 <= current_version <= SCHEMA_VERSION):
                        raise ChatStoreError(
                            f"Unsupported CHAT schema version {current_version}."
                        )
                    columns = {
                        column["name"]
                        for column in connection.execute(
                            "PRAGMA table_info(messages)"
                        ).fetchall()
                    }
                    if current_version <= 1 and "origin_sent_at" not in columns:
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN origin_sent_at REAL"
                        )
                    if current_version <= 2 and "dm_node_id" not in columns:
                        # v2 -> v3: add DM conversation identity. Existing
                        # rows get dm_node_id = NULL, which is exactly
                        # "this is a channel message" -- every existing
                        # channel history row is preserved unchanged and
                        # stays correctly excluded from every DM query.
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN dm_node_id TEXT"
                        )
                    if current_version <= 3 and "channel_key" not in columns:
                        # v3 -> v4: add stable CHANNEL identity (CHAT
                        # channel-history isolation), mirroring
                        # dm_node_id's own precedent. Existing rows get
                        # channel_key = NULL -- "written before this
                        # column existed" -- which load_recent_page/
                        # load_older_page treat as matching any live
                        # channel_key rather than hiding already-
                        # collected history that never had a chance to
                        # record which physical channel it belonged to.
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN channel_key TEXT"
                        )
                    if current_version <= 4 and "local_node_id" not in columns:
                        # v4 -> v5: add per-physical/local-radio CHAT
                        # namespace (CHAT state-integrity). Unlike
                        # channel_key, a NULL local_node_id is NEVER
                        # grandfathered into queries -- the preserve-
                        # but-hide migration policy keeps pre-namespacing
                        # rows on disk but never returns them, so a
                        # radio can never surface history it did not
                        # actually own. Rows are only ever stamped with
                        # a resolved local node ID going forward.
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN local_node_id TEXT"
                        )
                    if current_version <= 5 and "profile_key" not in columns:
                        # v5 -> v6: add the LOCAL CHAT HISTORY PROFILE key
                        # (canonical local node ID + canonical SHORT NAME).
                        # Existing rows get profile_key = NULL -- they predate
                        # profile keying and cannot be tied to a trustworthy
                        # profile (no short name was recorded), so the
                        # preserve-but-hide policy keeps them on disk but
                        # never returns them, exactly like the v4 -> v5
                        # local_node_id NULL rows. They are never arbitrarily
                        # assigned to whichever profile connects first, and
                        # never merged. local_node_id (node half) is retained
                        # for audit; profile_key is the authoritative scope.
                        connection.execute(
                            "ALTER TABLE messages ADD COLUMN profile_key TEXT"
                        )
                    if current_version != SCHEMA_VERSION:
                        connection.execute(
                            "UPDATE schema_version SET version = ?",
                            (SCHEMA_VERSION,),
                        )
                # Only after any migration above is origin_sent_at/
                # dm_node_id guaranteed to exist on every database this
                # app has ever created are these indexes (which reference
                # them) created -- last, rather than in the main script
                # above, and the dedup index is unconditionally dropped
                # and rebuilt every open() (cheap, one-time per process)
                # since CREATE INDEX IF NOT EXISTS alone would never widen
                # an index already existing under this name from a prior
                # schema version's narrower column list.
                #
                # COALESCE(dm_node_id, '') rather than the bare column:
                # standard SQL (and SQLite) UNIQUE constraints treat every
                # NULL as distinct from every other NULL, so a bare
                # dm_node_id column here would silently stop deduplicating
                # ALL channel messages (whose dm_node_id is always NULL)
                # the moment this column was introduced. The column
                # itself stays genuinely NULL for a channel row -- every
                # `dm_node_id IS NULL` filter elsewhere is unaffected --
                # only this index's own notion of "equal" is coerced.
                #
                # profile_key is included so the SAME remote packet arriving
                # under two different local profiles (same node id, different
                # SHORT NAME) is treated as two distinct observations -- a
                # profile never deduplicates a packet it has not itself seen.
                # COALESCE(profile_key, '') mirrors the dm_node_id reasoning:
                # a pre-profile NULL row must not collide with every other
                # NULL row.
                connection.execute("DROP INDEX IF EXISTS incoming_packet_identity")
                connection.execute(
                    """
                    CREATE UNIQUE INDEX incoming_packet_identity
                    ON messages(
                        node_id,
                        packet_id,
                        channel_index,
                        COALESCE(dm_node_id, ''),
                        COALESCE(local_node_id, ''),
                        COALESCE(profile_key, '')
                    )
                    WHERE direction = 'incoming' AND packet_id IS NOT NULL
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS incoming_node_message_time
                    ON messages(node_id, origin_sent_at, radio_rx_at)
                    WHERE direction = 'incoming'
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS dm_conversation_lookup
                    ON messages(dm_node_id)
                    WHERE dm_node_id IS NOT NULL
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
