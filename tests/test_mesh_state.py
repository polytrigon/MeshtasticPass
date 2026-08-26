"""Tests for MESH's real-data working-set ranking, roles, and staleness."""

from __future__ import annotations

import unittest

from geo import GeoPosition
from mesh_state import (
    DEFAULT_MAX_REMOTE_NODES,
    MESH_STALE_THRESHOLD_SECONDS,
    MeshNodeState,
    build_mesh_working_set,
    format_mesh_context_line,
    normalize_mesh_node_id,
)
from radio_service import NodeMetadata


NOW = 1_000_000.0
YOU = NodeMetadata("!you", "Local", "ME", 0, NOW, True)
YOU_POSITION = GeoPosition(40.7128, -74.0060)


class NormalizeMeshNodeIdTests(unittest.TestCase):
    """The exact int/hex and case mismatches flagged as the navigation

    regression's likely cause: two representations of the same physical
    node must normalize to one identical string.
    """

    def test_already_canonical_form_is_unchanged(self) -> None:
        self.assertEqual(normalize_mesh_node_id("!075bcd15"), "!075bcd15")

    def test_uppercase_hex_normalizes_to_lowercase(self) -> None:
        self.assertEqual(normalize_mesh_node_id("!075BCD15"), "!075bcd15")

    def test_bare_decimal_node_number_normalizes_to_hex_form(self) -> None:
        """123456789 decimal == 0x075bcd15 -- the exact pairing called out

        as the suspected root cause.
        """
        self.assertEqual(normalize_mesh_node_id("123456789"), "!075bcd15")

    def test_whitespace_is_stripped(self) -> None:
        self.assertEqual(normalize_mesh_node_id("  !075bcd15  "), "!075bcd15")

    def test_empty_string_stays_empty(self) -> None:
        self.assertEqual(normalize_mesh_node_id(""), "")


def client_state(
    node: NodeMetadata, *, last_interaction_at: float | None, is_relay: bool = False
) -> MeshNodeState:
    return MeshNodeState(
        node=node, is_client=True, is_relay=is_relay, last_interaction_at=last_interaction_at
    )


class MeshNodeStateStalenessTests(unittest.TestCase):
    def test_at_exactly_the_threshold_is_recent(self) -> None:
        state = client_state(
            NodeMetadata("!a", "A"),
            last_interaction_at=NOW - MESH_STALE_THRESHOLD_SECONDS,
        )
        self.assertFalse(state.is_stale(now=NOW))

    def test_one_second_past_the_threshold_is_stale(self) -> None:
        state = client_state(
            NodeMetadata("!a", "A"),
            last_interaction_at=NOW - MESH_STALE_THRESHOLD_SECONDS - 1,
        )
        self.assertTrue(state.is_stale(now=NOW))

    def test_no_known_interaction_is_stale_not_fabricated_recent(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!a", "A"),
            is_client=False,
            is_relay=False,
            last_interaction_at=None,
        )
        self.assertTrue(state.is_stale(now=NOW))


class MeshNodeStateGlyphRuleTests(unittest.TestCase):
    """CLIENT appearance (solid) takes priority; only RELAY-with-no-CLIENT

    renders stroked. No current fixture/data path in this app can ever
    produce a RELAY-only state (see MeshNodeState's docstring on why
    is_relay is always False from build_mesh_working_set), so the
    RELAY-only case is proven directly against a synthetic state.
    """

    def test_client_only_is_solid(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!a", "A"),
            is_client=True,
            is_relay=False,
            last_interaction_at=NOW,
        )
        self.assertTrue(state.glyph_is_solid())

    def test_client_and_relay_is_solid(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!a", "A"),
            is_client=True,
            is_relay=True,
            last_interaction_at=NOW,
        )
        self.assertTrue(state.glyph_is_solid())

    def test_relay_only_is_stroked(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!a", "A"),
            is_client=False,
            is_relay=True,
            last_interaction_at=NOW,
        )
        self.assertFalse(state.glyph_is_solid())

    def test_neither_role_defaults_solid(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!a", "A"),
            is_client=False,
            is_relay=False,
            last_interaction_at=None,
        )
        self.assertTrue(state.glyph_is_solid())


class BuildMeshWorkingSetTests(unittest.TestCase):
    def test_you_is_always_included_when_present(self) -> None:
        result = build_mesh_working_set([YOU], last_message_at={})
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].node.is_local)

    def test_you_absent_when_local_node_unknown(self) -> None:
        result = build_mesh_working_set([], last_message_at={})
        self.assertEqual(result, ())

    def test_incoming_message_makes_a_node_client(self) -> None:
        alice = NodeMetadata("!alice", "Alice", "ALC", 1, NOW)
        result = build_mesh_working_set(
            [YOU, alice], last_message_at={"!alice": NOW - 10}
        )
        remote = next(state for state in result if not state.node.is_local)
        self.assertTrue(remote.is_client)
        self.assertFalse(remote.is_relay)
        self.assertEqual(remote.last_interaction_at, NOW - 10)

    def test_node_with_no_message_history_is_excluded(self) -> None:
        heard_only = NodeMetadata("!heard", "HeardOnly", None, None, NOW - 5)
        result = build_mesh_working_set([YOU, heard_only], last_message_at={})
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].node.is_local)

    def test_client_node_missing_from_known_nodes_still_appears(self) -> None:
        """A CLIENT node absent from the passive node database (e.g. its

        record has not synced yet) still appears, with only its node ID
        known -- CHAT history, not the node database, is authoritative
        for CLIENT.
        """
        result = build_mesh_working_set([YOU], last_message_at={"!ghost": NOW - 5})
        remote = next(state for state in result if not state.node.is_local)
        self.assertEqual(remote.node.node_id, "!ghost")
        self.assertTrue(remote.is_client)

    def test_case_insensitive_node_id_matching(self) -> None:
        alice = NodeMetadata("!AlIcE", "Alice", "ALC", 1, NOW)
        result = build_mesh_working_set(
            [YOU, alice], last_message_at={"!alice": NOW - 10}
        )
        remote = next(state for state in result if not state.node.is_local)
        self.assertEqual(remote.node.long_name, "Alice")

    def test_decimal_sender_id_still_joins_the_real_known_node(self) -> None:
        """Regression: if the CHAT-activity source reports a sender under a

        bare decimal node number ("123456789") while get_known_nodes()
        reports the SAME physical node as "!075bcd15", the two must
        resolve to one MeshNodeState carrying the real name/position --
        not a nameless "ghost" node under the decimal ID that leaves the
        real node entirely absent from the working set (which is what
        broke arrow navigation: the rendered/positioned node and the one
        navigation could find were different objects).
        """
        north_node = NodeMetadata("!075bcd15", "North Node", "NORTH", 1, NOW)
        result = build_mesh_working_set(
            [YOU, north_node], last_message_at={"123456789": NOW - 10}
        )
        self.assertEqual(len(result), 2)
        remote = next(state for state in result if not state.node.is_local)
        self.assertEqual(remote.node.node_id, "!075bcd15")
        self.assertEqual(remote.node.long_name, "North Node")

    def test_split_representations_of_one_node_merge_keeping_latest_time(
        self,
    ) -> None:
        """If a node's history was already split across both raw

        representations (e.g. an older message logged under the decimal
        form, a newer one under the hex form), the working set must
        merge them into one node, keeping the more recent timestamp --
        never silently dropping one representation's activity.
        """
        result = build_mesh_working_set(
            [YOU],
            last_message_at={"123456789": NOW - 500, "!075bcd15": NOW - 10},
        )
        self.assertEqual(len(result), 2)
        remote = next(state for state in result if not state.node.is_local)
        self.assertEqual(remote.node.node_id, "!075bcd15")
        self.assertEqual(remote.last_interaction_at, NOW - 10)

    def test_working_set_is_bounded_to_max_remote_nodes(self) -> None:
        last_message_at = {f"!n{i:04x}": NOW - i for i in range(20)}
        result = build_mesh_working_set([YOU], last_message_at=last_message_at)
        self.assertEqual(len(result) - 1, DEFAULT_MAX_REMOTE_NODES)

    def test_never_renders_the_full_historical_client_list(self) -> None:
        last_message_at = {f"!n{i:04x}": NOW - i for i in range(200)}
        result = build_mesh_working_set([YOU], last_message_at=last_message_at)
        self.assertLess(len(result), len(last_message_at))

    def test_most_recent_interaction_ranks_first(self) -> None:
        last_message_at = {"!old": NOW - 5_000, "!new": NOW - 10}
        result = build_mesh_working_set([YOU], last_message_at=last_message_at)
        remote_ids = [state.node.node_id for state in result if not state.node.is_local]
        self.assertEqual(remote_ids[0], "!new")

    def test_stale_fallback_shows_most_recent_historical_nodes(self) -> None:
        """When nothing is recent, ranking naturally surfaces the most

        recently-active stale nodes instead of an empty working set --
        there is no separate priority tier that would exclude them.
        """
        very_old = NOW - MESH_STALE_THRESHOLD_SECONDS * 10
        last_message_at = {f"!n{i:04x}": very_old - i for i in range(5)}
        result = build_mesh_working_set([YOU], last_message_at=last_message_at)
        self.assertEqual(len(result) - 1, 5)
        remotes = [state for state in result if not state.node.is_local]
        self.assertTrue(all(state.is_stale(now=NOW) for state in remotes))

    def test_ranking_is_deterministic_and_arrival_order_independent(self) -> None:
        last_message_at = {f"!n{i:04x}": NOW - i * 7 for i in range(10)}
        nodes = [YOU] + [
            NodeMetadata(f"!n{i:04x}", f"Node{i}") for i in range(10)
        ]
        forward = build_mesh_working_set(nodes, last_message_at=last_message_at)
        backward = build_mesh_working_set(
            list(reversed(nodes)), last_message_at=last_message_at
        )
        self.assertEqual(
            [state.node.node_id for state in forward],
            [state.node.node_id for state in backward],
        )

    def test_tie_break_on_node_id_is_stable(self) -> None:
        last_message_at = {"!bbb": NOW - 10, "!aaa": NOW - 10}
        result = build_mesh_working_set([YOU], last_message_at=last_message_at)
        remote_ids = [state.node.node_id for state in result if not state.node.is_local]
        self.assertEqual(remote_ids, ["!aaa", "!bbb"])


class FormatMeshContextLineTests(unittest.TestCase):
    def test_you_context_is_bare_literal_you(self) -> None:
        """compact_node_label() always returns literal "YOU" for the local

        node regardless of its configured name -- see mesh_topology.py.
        """
        state = MeshNodeState(
            node=YOU, is_client=False, is_relay=False, last_interaction_at=None
        )
        self.assertEqual(format_mesh_context_line(state, now=NOW), "YOU")

    def test_client_only_format(self) -> None:
        """Long Name / Short Name / ROLE / N HOPS / AGE / DISTANCE -- the

        full new format (spec section 21/37). No distance_miles was set
        on this state, so distance renders "? mi", never a fabricated
        figure.
        """
        state = client_state(
            NodeMetadata("!bob", "Bob Basecamp", "BOB", 1, NOW),
            last_interaction_at=NOW - 30 * 60,
        )
        self.assertEqual(
            format_mesh_context_line(state, now=NOW),
            "Bob Basecamp / BOB / CLIENT / 1 HOPS / 30m / ? mi",
        )

    def test_client_with_known_distance_format(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!bob", "Bob Basecamp", "BOB", 1, NOW),
            is_client=True,
            is_relay=False,
            last_interaction_at=NOW - 30 * 60,
            distance_miles=4.23,
        )
        self.assertEqual(
            format_mesh_context_line(state, now=NOW),
            "Bob Basecamp / BOB / CLIENT / 1 HOPS / 30m / 4.2 mi",
        )

    def test_client_and_relay_format(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!alice", "Alice Trail", "ALC", 0, NOW),
            is_client=True,
            is_relay=True,
            last_interaction_at=NOW - 30 * 60,
            distance_miles=1.8,
        )
        self.assertEqual(
            format_mesh_context_line(state, now=NOW),
            "Alice Trail / ALC / CLIENT+RELAY / 0 HOPS / 30m / 1.8 mi",
        )

    def test_relay_only_format(self) -> None:
        """No Short Name on this node -- that segment is omitted entirely,

        never rendered as a fabricated "?"/"UNKNOWN"/"NONE".
        """
        state = MeshNodeState(
            node=NodeMetadata("!r", "Relay Only", None, 2, NOW),
            is_client=False,
            is_relay=True,
            last_interaction_at=NOW - 60,
        )
        self.assertEqual(
            format_mesh_context_line(state, now=NOW),
            "Relay Only / RELAY / 2 HOPS / 1m / ? mi",
        )

    def test_short_name_omitted_when_absent(self) -> None:
        """No Short Name -> exactly 5 segments (name/role/hops/time/

        distance), never a fabricated 6th "?"/"UNKNOWN"/"NONE" segment.
        """
        state = client_state(
            NodeMetadata("!nick", "Long Only", None, 1, NOW), last_interaction_at=NOW - 10
        )
        line = format_mesh_context_line(state, now=NOW)
        self.assertTrue(line.startswith("Long Only / CLIENT"))
        self.assertEqual(len(line.split(" / ")), 5)

    def test_short_name_not_duplicated_when_identical_to_long_name(self) -> None:
        state = client_state(
            NodeMetadata("!same", "SAME", "SAME", 1, NOW), last_interaction_at=NOW - 10
        )
        line = format_mesh_context_line(state, now=NOW)
        self.assertEqual(line.count("SAME"), 1)
        self.assertTrue(line.startswith("SAME / CLIENT"))

    def test_long_name_missing_falls_back_to_short_name(self) -> None:
        state = client_state(
            NodeMetadata("!shortonly", None, "SHRT", 1, NOW), last_interaction_at=NOW - 10
        )
        line = format_mesh_context_line(state, now=NOW)
        self.assertTrue(line.startswith("SHRT / CLIENT"))

    def test_both_names_missing_falls_back_to_node_id(self) -> None:
        state = client_state(NodeMetadata("!bareid"), last_interaction_at=NOW - 10)
        line = format_mesh_context_line(state, now=NOW)
        self.assertTrue(line.startswith("!bareid / CLIENT"))

    def test_non_string_name_field_does_not_crash_and_is_ignored(self) -> None:
        """Defensive: a stray non-string value in a name field (should never

        happen from RadioService, but nothing here should assume it) is
        treated as absent, never passed to str.strip() directly.
        """
        state = client_state(
            NodeMetadata("!weird", "Weird Name", 0), last_interaction_at=NOW - 10  # type: ignore[arg-type]
        )
        line = format_mesh_context_line(state, now=NOW)
        self.assertTrue(line.startswith("Weird Name / CLIENT"))

    def test_unknown_hops_renders_question_mark_not_zero(self) -> None:
        state = client_state(
            NodeMetadata("!x", "X", None, None, NOW), last_interaction_at=NOW - 10
        )
        self.assertIn("? HOPS", format_mesh_context_line(state, now=NOW))
        self.assertNotIn("0 HOPS", format_mesh_context_line(state, now=NOW))

    def test_unknown_interaction_time_renders_question_mark(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!x", "X", None, 1, NOW),
            is_client=True,
            is_relay=False,
            last_interaction_at=None,
        )
        self.assertEqual(
            format_mesh_context_line(state, now=NOW), "X / CLIENT / 1 HOPS / ? / ? mi"
        )

    def test_you_context_never_gains_appended_segments(self) -> None:
        """Section 28: YOU stays exactly "YOU" -- never a Short Name, role,

        hop count, time, or distance, even if the local NodeMetadata
        happens to carry a position (distance would otherwise be 0 mi).
        """
        you_with_position = NodeMetadata(
            "!you", "Local", "ME", 0, NOW, True, position=YOU_POSITION
        )
        state = MeshNodeState(
            node=you_with_position, is_client=False, is_relay=False, last_interaction_at=None
        )
        self.assertEqual(format_mesh_context_line(state, now=NOW), "YOU")


if __name__ == "__main__":
    unittest.main()
