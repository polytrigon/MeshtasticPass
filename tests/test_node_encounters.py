"""PASSES persistence: the record of every node this radio has met.

A pass outlives the radio's bounded NodeDB and the MESH board's bounded
working set, so this is the one place in the app where forgetting is a
bug rather than a bound. These tests are mostly about what must NOT be
lost: a name, an earlier first sighting, or the fact that a node was
once heard directly rather than only gossiped about.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_store import (  # noqa: E402
    ENCOUNTER_ORDERS,
    SCHEMA_VERSION,
    ChatStore,
    NodeEncounter,
    sort_encounters,
)

T = 1_700_000_000.0


def _encounter(node_id: str, **kwargs) -> NodeEncounter:
    fields = {
        "long_name": None,
        "short_name": None,
        "hops_away": None,
        "first_seen_at": T,
        "last_seen_at": T,
        "first_heard_at": None,
        "last_heard_at": None,
    }
    fields.update(kwargs)
    return NodeEncounter(node_id=node_id, **fields)


class SortEncountersTests(unittest.TestCase):
    """Pure ordering -- no database, no app."""

    def setUp(self) -> None:
        self.rows = (
            _encounter("!d4", short_name="DLTA", hops_away=1, last_seen_at=T + 10),
            _encounter("!a1", short_name="ALFA", hops_away=3, last_seen_at=T),
            _encounter("!c3", short_name="chrl", last_seen_at=T + 200),
            _encounter("!b2", short_name="BRVO", last_seen_at=T + 50),
        )

    def test_alpha_is_case_insensitive(self) -> None:
        """A lowercase short name must sort among the others, not after

        them -- node operators pick their own capitalisation and the list
        should read alphabetically to a human, not to ASCII.
        """
        self.assertEqual(
            [e.display_name for e in sort_encounters(self.rows, "alpha")],
            ["ALFA", "BRVO", "chrl", "DLTA"],
        )

    def test_hops_puts_unknown_depth_last(self) -> None:
        """An unknown hop count is not a small one. Sorting it as zero

        would put the least-known nodes exactly where the closest ones
        belong.
        """
        self.assertEqual(
            [e.display_name for e in sort_encounters(self.rows, "hops")],
            ["DLTA", "ALFA", "BRVO", "chrl"],
        )

    def test_recent_is_newest_first(self) -> None:
        self.assertEqual(
            [e.display_name for e in sort_encounters(self.rows, "recent")],
            ["chrl", "BRVO", "DLTA", "ALFA"],
        )

    def test_every_order_is_total(self) -> None:
        """Identical-looking rows must still have a fixed order, or the

        list reshuffles between renders of unchanged data.
        """
        twins = (
            _encounter("!zz", short_name="SAME", hops_away=2),
            _encounter("!aa", short_name="SAME", hops_away=2),
        )
        for order in ENCOUNTER_ORDERS:
            with self.subTest(order=order):
                self.assertEqual(
                    [e.node_id for e in sort_encounters(twins, order)], ["!aa", "!zz"]
                )

    def test_an_unknown_order_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            sort_encounters(self.rows, "sideways")

    def test_display_name_falls_back_rather_than_going_blank(self) -> None:
        self.assertEqual(_encounter("!x1", short_name="SHRT").display_name, "SHRT")
        self.assertEqual(_encounter("!x1", long_name="Long Name").display_name, "Long Name")
        self.assertEqual(_encounter("!x1").display_name, "!x1")
        self.assertEqual(_encounter("!x1", short_name="   ").display_name, "!x1")


class RecordEncounterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "chat.db")
        self.store = ChatStore.open(self.path)

    def _one(self, node_id: str) -> NodeEncounter:
        return next(e for e in self.store.encounters() if e.node_id == node_id)

    def test_gossip_then_heard_upgrades_and_keeps_what_was_known(self) -> None:
        self.store.record_encounter(
            "!aaaa0001", seen_at=T, short_name="ALFA", long_name="Alfa", hops_away=3
        )
        self.store.record_encounter("!aaaa0001", seen_at=T + 100, heard_directly=True)
        row = self._one("!aaaa0001")
        self.assertTrue(row.heard_directly)
        self.assertEqual((row.short_name, row.long_name, row.hops_away), ("ALFA", "Alfa", 3))

    def test_a_later_recorded_earlier_sighting_never_downgrades(self) -> None:
        """Sightings do not arrive in time order -- a NodeDB sweep can

        report an older lastHeard after a packet already arrived. Recording
        one must not undo what a direct encounter established.
        """
        self.store.record_encounter(
            "!bbbb0002", seen_at=T + 50, short_name="BRVO", heard_directly=True
        )
        self.store.record_encounter("!bbbb0002", seen_at=T - 50)
        row = self._one("!bbbb0002")
        self.assertTrue(row.heard_directly)
        self.assertEqual(row.short_name, "BRVO")
        self.assertEqual(row.first_seen_at, T - 50)
        self.assertEqual(row.last_seen_at, T + 50)

    def test_gossip_only_stays_gossip(self) -> None:
        self.store.record_encounter("!cccc0003", seen_at=T)
        self.assertFalse(self._one("!cccc0003").heard_directly)

    def test_recording_is_idempotent(self) -> None:
        for _ in range(5):
            self.store.record_encounter("!dddd0004", seen_at=T, short_name="DLTA")
        self.assertEqual(len(self.store.encounters()), 1)

    def test_a_blank_name_never_overwrites_a_known_one(self) -> None:
        self.store.record_encounter("!eeee0005", seen_at=T, short_name="ECHO")
        self.store.record_encounter("!eeee0005", seen_at=T + 1, short_name="   ")
        self.assertEqual(self._one("!eeee0005").short_name, "ECHO")

    def test_a_pass_survives_reopening_the_database(self) -> None:
        self.store.record_encounter("!ffff0006", seen_at=T, short_name="FOXT")
        reopened = ChatStore.open(self.path)
        self.assertEqual([e.short_name for e in reopened.encounters()], ["FOXT"])

    def test_an_empty_node_id_is_refused(self) -> None:
        with self.assertRaises(Exception):
            self.store.record_encounter("   ", seen_at=T)


class SchemaUpgradeTests(unittest.TestCase):
    def test_an_existing_v6_database_gains_passes_without_losing_chat(self) -> None:
        """Upgrading must be additive. Every migration before this one

        added a column; this one adds a table, which CREATE TABLE IF NOT
        EXISTS handles on an existing database -- but the CHAT history it
        sits beside has to come through untouched.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "chat.db")
            ChatStore.open(path)
            connection = sqlite3.connect(path)
            connection.execute("DROP TABLE node_encounters")
            connection.execute("UPDATE schema_version SET version = 6")
            connection.commit()
            message_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            connection.close()

            upgraded = ChatStore.open(path)

            connection = sqlite3.connect(path)
            self.addCleanup(connection.close)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone()[0],
                SCHEMA_VERSION,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                message_count,
            )
            self.assertEqual(upgraded.encounters(), ())
            upgraded.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
            self.assertEqual(len(upgraded.encounters()), 1)

    def test_a_v7_database_gains_pass_at_without_losing_its_encounters(self) -> None:
        """v7 -> v8 is the first node_encounters migration that must ALTER.

        The v6 -> v7 step got its table free from CREATE TABLE IF NOT
        EXISTS. That same statement is a NO-OP on a v7 database, so a new
        COLUMN needs an explicit ALTER -- and the rows already in the
        table have to survive it, since a pass list that resets on
        upgrade defeats the entire point of the view.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "chat.db")
            store = ChatStore.open(path)
            store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")

            # Rebuild node_encounters exactly as v7 shipped it: same
            # columns, no pass_at, carrying the row already recorded.
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE encounters_v7 (
                    node_id TEXT PRIMARY KEY,
                    long_name TEXT,
                    short_name TEXT,
                    hops_away INTEGER,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    first_heard_at REAL,
                    last_heard_at REAL
                );
                INSERT INTO encounters_v7
                    SELECT node_id, long_name, short_name, hops_away,
                           first_seen_at, last_seen_at, first_heard_at,
                           last_heard_at
                    FROM node_encounters;
                DROP TABLE node_encounters;
                ALTER TABLE encounters_v7 RENAME TO node_encounters;
                UPDATE schema_version SET version = 7;
                """
            )
            connection.commit()
            connection.close()

            upgraded = ChatStore.open(path)

            rows = upgraded.encounters()
            self.assertEqual([row.short_name for row in rows], ["ALFA"])
            self.assertIsNone(rows[0].pass_at)
            self.assertFalse(rows[0].has_pass)
            connection = sqlite3.connect(path)
            self.addCleanup(connection.close)
            self.assertEqual(
                connection.execute("SELECT version FROM schema_version").fetchone()[0],
                SCHEMA_VERSION,
            )


class ConfirmedPassTests(unittest.TestCase):
    """pass_at: the subset of encounters that actually exchanged a pass.

    Distinct from heard_directly, and deliberately harder to earn. Being
    heard is something a radio does TO us; a pass is mutual, so nothing
    the mesh broadcasts can ever set it on its own.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ChatStore.open(os.path.join(directory.name, "chat.db"))

    def _one(self, node_id: str) -> NodeEncounter:
        return next(e for e in self.store.encounters() if e.node_id == node_id)

    def test_an_ordinary_encounter_has_no_pass(self) -> None:
        """The default, and for now the only case real data produces.

        No pass protocol exists yet, so every row a live radio writes
        must come back has_pass False -- the PASSES count is honestly
        zero rather than quietly counting something else.
        """
        self.store.record_encounter("!aaaa0001", seen_at=T, heard_directly=True)
        row = self._one("!aaaa0001")
        self.assertTrue(row.heard_directly)
        self.assertIsNone(row.pass_at)
        self.assertFalse(row.has_pass)

    def test_a_recorded_pass_round_trips(self) -> None:
        self.store.record_encounter("!bbbb0002", seen_at=T, pass_at=T)
        row = self._one("!bbbb0002")
        self.assertEqual(row.pass_at, T)
        self.assertTrue(row.has_pass)

    def test_a_later_sighting_never_clears_a_pass(self) -> None:
        """Same one-way rule the rest of the row follows.

        A node that passed with us once has passed with us for ever; an
        ordinary NodeDB sweep afterwards carries no pass_at and must not
        be read as a retraction.
        """
        self.store.record_encounter("!cccc0003", seen_at=T, pass_at=T)
        self.store.record_encounter("!cccc0003", seen_at=T + 500)
        self.assertEqual(self._one("!cccc0003").pass_at, T)

    def test_a_pass_keeps_the_earliest_time(self) -> None:
        """pass_at answers "since when", so it only ever moves earlier."""
        self.store.record_encounter("!dddd0004", seen_at=T, pass_at=T + 100)
        self.store.record_encounter("!dddd0004", seen_at=T, pass_at=T + 5)
        self.store.record_encounter("!dddd0004", seen_at=T, pass_at=T + 900)
        self.assertEqual(self._one("!dddd0004").pass_at, T + 5)

    def test_a_pass_survives_reopening_the_database(self) -> None:
        self.store.record_encounter("!eeee0005", seen_at=T, pass_at=T)
        self.store.record_encounter("!ffff0006", seen_at=T)
        reopened = ChatStore.open(self.store.path)
        self.assertEqual(
            {e.node_id: e.has_pass for e in reopened.encounters()},
            {"!eeee0005": True, "!ffff0006": False},
        )


if __name__ == "__main__":
    unittest.main()
