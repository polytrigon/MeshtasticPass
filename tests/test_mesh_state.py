"""Tests for MESH's real-data working-set ranking, roles, and staleness."""

from __future__ import annotations

import unittest

from rich.cells import cell_len

from geo import GeoPosition
from mesh_state import (
    DEFAULT_MAX_REMOTE_NODES,
    MESH_LINK_METER_UNKNOWN,
    MESH_LINK_SNR_EXCELLENT_DB,
    MESH_LINK_SNR_GOOD_DB,
    MESH_LINK_SNR_WEAK_DB,
    MESH_STALE_THRESHOLD_SECONDS,
    MeshActivityTier,
    MeshLinkDisplay,
    MeshNodeBarFields,
    MeshNodeState,
    build_mesh_working_set,
    format_mesh_link_display,
    format_mesh_node_bar_fields,
    format_mesh_node_bar_line,
    normalize_mesh_node_id,
)
from node_activity import ACTIVE_WINDOW_SECONDS, is_node_active
from radio_service import LinkObservation, NodeMetadata
from relative_time import format_relative_age


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

    def test_neither_role_renders_stroked_not_fabricated_client(self) -> None:
        """A passively-known-only node (NodeDB-first admission, never a

        CLIENT message observed) is genuinely reachable now -- see
        BuildMeshWorkingSetTests.test_nodedb_only_node_is_not_fabricated_
        as_client -- and must not render as if it had earned the CLIENT
        glyph merely by existing.
        """
        state = MeshNodeState(
            node=NodeMetadata("!a", "A"),
            is_client=False,
            is_relay=False,
            last_interaction_at=None,
        )
        self.assertFalse(state.glyph_is_solid())


class BuildMeshWorkingSetTests(unittest.TestCase):
    def test_you_is_always_included_when_present(self) -> None:
        result = build_mesh_working_set([YOU], now=NOW, last_message_at={})
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].node.is_local)

    def test_you_absent_when_local_node_unknown(self) -> None:
        result = build_mesh_working_set([], now=NOW, last_message_at={})
        self.assertEqual(result, ())

    def test_radio_swap_new_local_node_becomes_you_old_becomes_remote(self) -> None:
        """MESH FOLLOW-UP item 8/23: NodeDB carries BOTH the old radio's

        node (still is_local=False now that RadioService itself only
        ever flags the CURRENT radio) and the new one -- exactly one
        YOU, and the old radio is an ordinary remote candidate, never
        hidden or deleted.
        """
        old_radio = NodeMetadata("!aaaaaaaa", "Old V3", "V3", 0, NOW - 30, is_local=False)
        new_radio = NodeMetadata("!bbbbbbbb", "New V4", "V4", 0, NOW - 5, is_local=True)
        result = build_mesh_working_set(
            [new_radio, old_radio], now=NOW, last_message_at={}
        )
        local_ids = {state.node.node_id for state in result if state.node.is_local}
        self.assertEqual(local_ids, {"!bbbbbbbb"})
        remote = next(state for state in result if state.node.node_id == "!aaaaaaaa")
        self.assertFalse(remote.node.is_local)

    def test_self_heard_echo_never_admits_you_as_a_duplicate_remote(self) -> None:
        """PR #43 FOLLOW-UP Part A: a self-heard echo of YOU's own

        transmission (another node rebroadcasts it, or the SDK reports
        a locally-originated packet as "received") must never leave
        YOU's own ID ALSO sitting in the working set as a SECOND,
        is_local=False entry -- that duplicate key is exactly what let
        the MESH topology label widget pick the wrong (remote-shaped)
        MeshNodeState via last-write-wins dict construction while the
        bottom-left context (first-match via next()) kept showing the
        correct one. Exactly one entry for YOU's ID, and it is local.
        """
        you = NodeMetadata("!bbbbbbbb", "V4 Radio", "V4", 0, NOW - 5, is_local=True)
        remote = NodeMetadata("!c0ffee01", "Real Remote", "RMT", 1, NOW - 5)
        result = build_mesh_working_set(
            [you, remote],
            now=NOW,
            # Simulates a CHAT-received message whose sender_node_id is
            # YOU's own ID -- a self-heard echo, never a real remote
            # interaction.
            last_message_at={"!bbbbbbbb": NOW - 2, "!c0ffee01": NOW - 2},
        )
        matches = [state for state in result if state.node.node_id == "!bbbbbbbb"]
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0].node.is_local)
        self.assertEqual(matches[0].node.long_name, "V4 Radio")

    def test_ambiguous_multiple_is_local_flags_prefer_no_you_over_guessing(self) -> None:
        """Defense-in-depth (item 8): if the upstream data ever reports

        MORE than one is_local=True node at once -- a data-consistency
        violation that should not occur given RadioService's own single
        -authoritative-source fix, but this function must never blindly
        trust that -- neither becomes YOU. Both still surface as
        ordinary remote candidates rather than one being arbitrarily
        chosen or both vanishing.
        """
        first = NodeMetadata("!first000", "First", "FST", 0, NOW - 5, is_local=True)
        second = NodeMetadata("!second00", "Second", "SND", 0, NOW - 5, is_local=True)
        result = build_mesh_working_set([first, second], now=NOW, last_message_at={})
        self.assertFalse(any(state.node.is_local for state in result))
        remote_ids = {state.node.node_id for state in result}
        self.assertEqual(remote_ids, {"!first000", "!second00"})

    def test_incoming_message_makes_a_node_client(self) -> None:
        alice = NodeMetadata("!alice", "Alice", "ALC", 1, NOW - 500)
        result = build_mesh_working_set(
            [YOU, alice], now=NOW, last_message_at={"!alice": NOW - 10}
        )
        remote = next(state for state in result if not state.node.is_local)
        self.assertTrue(remote.is_client)
        self.assertFalse(remote.is_relay)
        self.assertEqual(remote.last_interaction_at, NOW - 10)

    def test_identified_relay_admitted_as_relay_and_never_very_old(self) -> None:
        """A successful traceroute's forward hop is a real canonical node ID
        that NodeDB may not have synced yet. It must be admitted as a bare
        minimal node (is_relay=True, no name/timestamp fabricated), never
        VERY_OLD'd off the board for lacking timing, and later NodeDB data
        enriches that SAME node rather than creating a duplicate.
        """
        alice = NodeMetadata("!alice", "Alice", "ALC", 1, NOW - 500)
        result = build_mesh_working_set(
            [YOU, alice],
            now=NOW,
            last_message_at={},
            identified_relay_ids=("!ffff0001",),
        )
        remote_ids = {state.node.node_id for state in result if not state.node.is_local}
        self.assertIn("!ffff0001", remote_ids)
        relay = next(state for state in result if state.node.node_id == "!ffff0001")
        self.assertTrue(relay.is_relay)
        self.assertFalse(relay.is_client)
        self.assertIsNone(relay.last_interaction_at)
        self.assertIsNone(relay.node.last_heard)
        # No timing info, but route evidence keeps it on the board (ACTIVE),
        # never VERY_OLD.
        self.assertNotEqual(relay.activity_tier(now=NOW), MeshActivityTier.VERY_OLD)

    def test_identified_relay_enriched_by_nodedb_not_duplicated(self) -> None:
        """When NodeDB also knows an identified relay, the SAME canonical
        node is used (carrying its real name), not a second bare entry.
        """
        known_relay = NodeMetadata("!ffff0001", "RelayX", "RLX", 2, NOW - 5)
        result = build_mesh_working_set(
            [YOU, known_relay],
            now=NOW,
            last_message_at={},
            identified_relay_ids=("!ffff0001",),
        )
        relays = [s for s in result if s.node.node_id == "!ffff0001"]
        self.assertEqual(len(relays), 1)
        self.assertTrue(relays[0].is_relay)
        self.assertEqual(relays[0].node.long_name, "RelayX")

    def test_identified_relay_survives_capacity_bounding(self) -> None:
        """An identified traceroute relay MUST stay admitted even when the
        normal NodeDB/CHAT population already fills max_remote_nodes: a relay
        named in a successful RouteDiscovery forward chain has to render, or
        the explicit route would silently compress into a fabricated
        YOU->A->TARGET (see app.py's route_chain_node_ids). The capacity cap
        is readability, not a license to drop route evidence.
        """
        # max_remote_nodes full competitors, each newer than the last, with
        # NO timing anywhere for the relay (it is bare, canonical-ID-only).
        last_message_at = {
            f"!n{i:04x}": NOW - i for i in range(DEFAULT_MAX_REMOTE_NODES)
        }
        result = build_mesh_working_set(
            [YOU],
            now=NOW,
            last_message_at=last_message_at,
            identified_relay_ids=("!ffff0001", "!ffff0002"),
        )
        relay_ids = {
            state.node.node_id
            for state in result
            if state.node.node_id in ("!ffff0001", "!ffff0002")
        }
        self.assertEqual(relay_ids, {"!ffff0001", "!ffff0002"})
        # The bounded cap is still honoured for the general pool but the
        # route-required relays are retained on top of it (they rank last, so
        # no higher-ranked real node is displaced).
        self.assertGreaterEqual(len(result) - 1, DEFAULT_MAX_REMOTE_NODES)

    def test_nodedb_only_node_with_no_chat_history_still_appears(self) -> None:
        """The core NodeDB-first behavior: a node the radio has passively

        heard from -- with no CHAT message ever observed for it -- is a
        MESH candidate on its own. A CHAT message must never be required
        for a real node to appear on the board. last_interaction_at
        stays specifically CHAT interaction time (None here, since there
        is none) -- unchanged meaning from before; NodeDB last_heard is
        a separate signal used for ranking only (see
        test_currently_active_nodes_outrank_more_recent_stale_ones).
        """
        heard_only = NodeMetadata("!heard", "HeardOnly", None, None, NOW - 5)
        result = build_mesh_working_set([YOU, heard_only], now=NOW, last_message_at={})
        self.assertEqual(len(result), 2)
        remote = next(state for state in result if not state.node.is_local)
        self.assertEqual(remote.node.node_id, "!heard")
        self.assertIsNone(remote.last_interaction_at)

    def test_nodedb_only_node_is_not_fabricated_as_client(self) -> None:
        """A node admitted purely from passive NodeDB data has never

        originated a message we received, so it must not be fabricated
        as CLIENT merely for existing -- see MeshNodeState.
        glyph_is_solid; the unified bottom bar (mesh_state.
        format_mesh_node_bar_fields) no longer renders a ROLE field at
        all (MESH GPS + UNIFIED BAR Part B).
        """
        heard_only = NodeMetadata("!heard", "HeardOnly", None, None, NOW - 5)
        result = build_mesh_working_set([YOU, heard_only], now=NOW, last_message_at={})
        remote = next(state for state in result if not state.node.is_local)
        self.assertFalse(remote.is_client)
        self.assertFalse(remote.is_relay)

    def test_client_node_missing_from_known_nodes_still_appears(self) -> None:
        """A CLIENT node absent from the passive node database (e.g. its

        record has not synced yet) still appears, with only its node ID
        known -- CHAT history, not the node database, is authoritative
        for CLIENT.
        """
        result = build_mesh_working_set(
            [YOU], now=NOW, last_message_at={"!ghost": NOW - 5}
        )
        remote = next(state for state in result if not state.node.is_local)
        self.assertEqual(remote.node.node_id, "!ghost")
        self.assertTrue(remote.is_client)

    def test_case_insensitive_node_id_matching(self) -> None:
        alice = NodeMetadata("!AlIcE", "Alice", "ALC", 1, NOW)
        result = build_mesh_working_set(
            [YOU, alice], now=NOW, last_message_at={"!alice": NOW - 10}
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
            [YOU, north_node], now=NOW, last_message_at={"123456789": NOW - 10}
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
            now=NOW,
            last_message_at={"123456789": NOW - 500, "!075bcd15": NOW - 10},
        )
        self.assertEqual(len(result), 2)
        remote = next(state for state in result if not state.node.is_local)
        self.assertEqual(remote.node.node_id, "!075bcd15")
        self.assertEqual(remote.last_interaction_at, NOW - 10)

    def test_working_set_is_bounded_to_max_remote_nodes(self) -> None:
        last_message_at = {f"!n{i:04x}": NOW - i for i in range(20)}
        result = build_mesh_working_set(
            [YOU], now=NOW, last_message_at=last_message_at
        )
        self.assertEqual(len(result) - 1, DEFAULT_MAX_REMOTE_NODES)

    def test_never_renders_the_full_historical_client_list(self) -> None:
        last_message_at = {f"!n{i:04x}": NOW - i for i in range(200)}
        result = build_mesh_working_set(
            [YOU], now=NOW, last_message_at=last_message_at
        )
        self.assertLess(len(result), len(last_message_at))

    def test_most_recent_interaction_ranks_first(self) -> None:
        last_message_at = {"!old": NOW - 5_000, "!new": NOW - 10}
        result = build_mesh_working_set(
            [YOU], now=NOW, last_message_at=last_message_at
        )
        remote_ids = [state.node.node_id for state in result if not state.node.is_local]
        self.assertEqual(remote_ids[0], "!new")

    def test_currently_active_nodes_outrank_more_recent_stale_ones(self) -> None:
        """Tier 1 (currently active, per the shared is_node_active

        predicate) outranks tier 3 (most-recently-interacted) even when
        the stale-but-more-recent node's raw timestamp is closer to now
        -- activity is a coarser, more meaningful signal than raw
        recency once a node has fallen out of the active window.
        """
        active = NodeMetadata("!active", "Active", None, None, NOW - 60)
        barely_stale = NodeMetadata(
            "!stale", "Stale", None, None, NOW - ACTIVE_WINDOW_SECONDS - 1
        )
        result = build_mesh_working_set([YOU, barely_stale, active], now=NOW)
        remote_ids = [state.node.node_id for state in result if not state.node.is_local]
        self.assertEqual(remote_ids, ["!active", "!stale"])

    def test_favorite_outranks_non_favorite_among_non_active_nodes(self) -> None:
        # Both beyond ACTIVE_WINDOW_SECONDS (now 2h, firmware-aligned) but
        # within MESH_VERY_OLD_THRESHOLD_SECONDS (24h), so both are
        # STALE -- admitted, non-active, and ranked on favorite status.
        favorite = NodeMetadata("!fav", "Favorite", None, None, NOW - 20_000)
        plain = NodeMetadata("!plain", "Plain", None, None, NOW - 10_000)
        result = build_mesh_working_set(
            [YOU, plain, favorite], now=NOW, favorite_ids={"!fav"}
        )
        remote_ids = [state.node.node_id for state in result if not state.node.is_local]
        self.assertEqual(remote_ids, ["!fav", "!plain"])

    def test_very_old_nodes_are_excluded_from_the_working_set(self) -> None:
        """Beyond MESH_VERY_OLD_THRESHOLD_SECONDS, a node's connector is

        removed from the current board entirely (see
        MeshActivityTier.VERY_OLD) -- never merely ranked last. Nothing
        here deletes NodeDB/CHAT history; a node heard again later is
        admitted normally on the very next refresh.
        """
        very_old = NOW - MESH_STALE_THRESHOLD_SECONDS * 10
        last_message_at = {f"!n{i:04x}": very_old - i for i in range(5)}
        result = build_mesh_working_set(
            [YOU], now=NOW, last_message_at=last_message_at
        )
        self.assertEqual(len(result), 1)  # YOU only -- all 5 filtered out
        self.assertTrue(result[0].node.is_local)

    def test_very_old_node_reappears_once_heard_again(self) -> None:
        """No persistent "removed" state: the working set is recomputed

        fresh from live last_heard/last_message_at every call, so a
        node that was VERY_OLD a moment ago is admitted completely
        normally the instant fresher evidence exists -- no special
        "restore" step anywhere.
        """
        node = NodeMetadata("!revive01", "Revived", "RVV")
        very_old_at = NOW - MESH_STALE_THRESHOLD_SECONDS * 10
        excluded = build_mesh_working_set(
            [YOU, node], now=NOW, last_message_at={"!revive01": very_old_at}
        )
        self.assertEqual(
            [state.node.node_id for state in excluded if not state.node.is_local], []
        )

        heard_again = build_mesh_working_set(
            [YOU, node], now=NOW, last_message_at={"!revive01": NOW - 5}
        )
        remote_ids = [
            state.node.node_id for state in heard_again if not state.node.is_local
        ]
        self.assertEqual(remote_ids, ["!revive01"])

    def test_ranking_is_deterministic_and_arrival_order_independent(self) -> None:
        last_message_at = {f"!n{i:04x}": NOW - i * 7 for i in range(10)}
        nodes = [YOU] + [
            NodeMetadata(f"!n{i:04x}", f"Node{i}") for i in range(10)
        ]
        forward = build_mesh_working_set(
            nodes, now=NOW, last_message_at=last_message_at
        )
        backward = build_mesh_working_set(
            list(reversed(nodes)), now=NOW, last_message_at=last_message_at
        )
        self.assertEqual(
            [state.node.node_id for state in forward],
            [state.node.node_id for state in backward],
        )

    def test_tie_break_on_node_id_is_stable(self) -> None:
        last_message_at = {"!bbb": NOW - 10, "!aaa": NOW - 10}
        result = build_mesh_working_set(
            [YOU], now=NOW, last_message_at=last_message_at
        )
        remote_ids = [state.node.node_id for state in result if not state.node.is_local]
        self.assertEqual(remote_ids, ["!aaa", "!bbb"])


class FormatMeshNodeBarFieldsTests(unittest.TestCase):
    """MESH GPS + UNIFIED BAR Part B: per-field resolution for the single

    unified bottom bar (long name / short name / HOPS / GPS / DISTANCE /
    LINK / ELAPSE), replacing the old format_mesh_context_line.
    """

    def test_you_fields_have_no_you_label_and_no_self_link(self) -> None:
        """YOU gets no literal "YOU" anywhere, HOPS "0", DISTANCE "--",

        ELAPSE "NOW", accent2=True, and the honest no-link placeholder --
        never a fabricated self-observation.
        """
        state = MeshNodeState(
            node=YOU, is_client=False, is_relay=False, last_interaction_at=None
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.long_name, "Local")
        self.assertEqual(fields.short_name, "ME")
        self.assertNotIn("YOU", fields.long_name)
        self.assertNotIn("YOU", fields.short_name)
        self.assertEqual(fields.hops_text, "0")
        self.assertEqual(fields.distance_text, "--")
        self.assertEqual(fields.elapse_text, "NOW")
        self.assertTrue(fields.accent2)
        self.assertEqual(fields.link_meter, MESH_LINK_METER_UNKNOWN)
        self.assertEqual(fields.link_rssi_text, "--")
        self.assertEqual(fields.link_snr_text, "--")
        self.assertEqual(fields.gps_text, "--")

    def test_you_with_real_gps_shows_coordinates(self) -> None:
        you_with_position = NodeMetadata(
            "!you", "Local", "ME", 0, NOW, True, position=YOU_POSITION
        )
        state = MeshNodeState(
            node=you_with_position, is_client=False, is_relay=False, last_interaction_at=None
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.gps_text, "40.7128, -74.0060")
        self.assertEqual(fields.gps_text_compact, "40.71,-74.01")
        # DISTANCE from YOU to itself is always "--", GPS or not.
        self.assertEqual(fields.distance_text, "--")

    def test_you_falls_back_to_node_id_when_no_names_at_all(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!bareyou", is_local=True),
            is_client=False,
            is_relay=False,
            last_interaction_at=None,
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.long_name, "!bareyou")
        self.assertEqual(fields.short_name, "!bareyou")

    def test_remote_client_full_fields(self) -> None:
        state = client_state(
            NodeMetadata("!bob", "Bob Basecamp", "BOB", 1),
            last_interaction_at=NOW - 30 * 60,
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.long_name, "Bob Basecamp")
        self.assertEqual(fields.short_name, "BOB")
        self.assertEqual(fields.hops_text, "1")
        self.assertEqual(fields.distance_text, "--")
        self.assertEqual(fields.elapse_text, "30m")
        self.assertFalse(fields.accent2)

    def test_remote_with_known_distance(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!bob", "Bob Basecamp", "BOB", 1),
            is_client=True,
            is_relay=False,
            last_interaction_at=NOW - 30 * 60,
            distance_miles=4.23,
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.distance_text, "4.2 mi")

    def test_distance_uses_metric_when_requested(self) -> None:
        state = MeshNodeState(
            node=NodeMetadata("!bob", "Bob", "BOB", 1),
            is_client=True,
            is_relay=False,
            last_interaction_at=NOW - 30 * 60,
            distance_miles=4.23,
        )
        fields = format_mesh_node_bar_fields(state, now=NOW, metric=True)
        self.assertTrue(fields.distance_text.endswith("km"))

    def test_distance_is_dash_when_no_shared_gps_fix_exists(self) -> None:
        """DISTANCE is state.distance_miles, which build_mesh_working_set

        only ever populates when BOTH YOU and the remote carry a real
        GPS fix -- never fabricated from hop count or any other proxy.
        """
        state = MeshNodeState(
            node=NodeMetadata("!bob", "Bob", "BOB", 1),
            is_client=True,
            is_relay=False,
            last_interaction_at=NOW - 30 * 60,
            distance_miles=None,
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.distance_text, "--")

    def test_gps_is_dash_when_node_has_no_position(self) -> None:
        state = client_state(NodeMetadata("!bob", "Bob", "BOB", 1), last_interaction_at=NOW - 10)
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.gps_text, "--")
        self.assertEqual(fields.gps_text_compact, "--")

    def test_gps_shows_real_coordinates_when_present(self) -> None:
        node = NodeMetadata(
            "!bob", "Bob", "BOB", 1, position=GeoPosition(40.7634, -73.9508)
        )
        state = client_state(node, last_interaction_at=NOW - 10)
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.gps_text, "40.7634, -73.9508")
        self.assertEqual(fields.gps_text_compact, "40.76,-73.95")

    def test_unknown_hops_renders_question_mark_not_zero(self) -> None:
        state = client_state(
            NodeMetadata("!x", "X", None, None, NOW), last_interaction_at=NOW - 10
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.hops_text, "?")

    def test_link_uses_the_passed_observation(self) -> None:
        observation = LinkObservation(rssi=-87, snr=6.0, observed_at=NOW - 3)
        state = client_state(
            NodeMetadata("!near", "Near", "NEAR", 1), last_interaction_at=NOW - 3
        )
        fields = format_mesh_node_bar_fields(state, now=NOW, link=observation)
        self.assertEqual(fields.link_rssi_text, "-87")
        self.assertEqual(fields.link_snr_text, "+6")
        self.assertNotEqual(fields.link_meter, MESH_LINK_METER_UNKNOWN)

    def test_missing_link_shows_the_honest_placeholder(self) -> None:
        """LINK preserves the passive direct-only semantics: no direct

        observation for this node -> the honest placeholder, never a
        misattributed relayed reading.
        """
        state = client_state(
            NodeMetadata("!near", "Near", "NEAR", 1), last_interaction_at=NOW - 3
        )
        fields = format_mesh_node_bar_fields(state, now=NOW, link=None)
        self.assertEqual(fields.link_meter, MESH_LINK_METER_UNKNOWN)
        self.assertEqual(fields.link_rssi_text, "--")
        self.assertEqual(fields.link_snr_text, "--")


class FormatMeshNodeBarFieldsTimeTests(unittest.TestCase):
    """ELAPSE (renamed from TIME in the FINAL MESHTASTIC POLISH pass --

    same underlying source/formatting) is the selected NODE's own
    freshness/last-heard age -- NOT LINK-observation age (see
    format_mesh_node_bar_fields' elapse_text computation, ported
    verbatim from the old format_mesh_context_line).
    """

    def test_active_nodedb_only_node_shows_concrete_time(self) -> None:
        node = NodeMetadata("!heard0001", "Hairy 9874", "SHN", 3, last_heard=NOW - 120)
        state = MeshNodeState(
            node=node, is_client=False, is_relay=False, last_interaction_at=None
        )
        self.assertTrue(is_node_active(node.last_heard, NOW))
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.elapse_text, "2m")

    def test_stale_nodedb_only_node_shows_concrete_older_time(self) -> None:
        stale_heard = NOW - ACTIVE_WINDOW_SECONDS - 3 * 60
        node = NodeMetadata("!stale0001", "Stale Node", "STL", 1, last_heard=stale_heard)
        state = MeshNodeState(
            node=node, is_client=False, is_relay=False, last_interaction_at=None
        )
        self.assertFalse(is_node_active(node.last_heard, NOW))
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.elapse_text, format_relative_age(ACTIVE_WINDOW_SECONDS + 3 * 60))

    def test_missing_last_heard_and_chat_history_stays_question_mark(self) -> None:
        node = NodeMetadata("!nodata001", "No Data", "ND", 1, last_heard=None)
        state = MeshNodeState(
            node=node, is_client=False, is_relay=False, last_interaction_at=None
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.elapse_text, "?")

    def test_chat_history_does_not_substitute_when_last_heard_is_fresher(self) -> None:
        node = NodeMetadata("!fresh0001", "Fresher", "FR", 1, last_heard=NOW - 10)
        state = MeshNodeState(
            node=node, is_client=True, is_relay=False, last_interaction_at=NOW - 5000
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.elapse_text, "10s")

    def test_last_heard_does_not_substitute_when_chat_is_fresher(self) -> None:
        node = NodeMetadata("!fresh0002", "ChatFresh", "CF", 1, last_heard=NOW - 5000)
        state = MeshNodeState(
            node=node, is_client=True, is_relay=False, last_interaction_at=NOW - 20
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        self.assertEqual(fields.elapse_text, "20s")

    def test_wall_time_advancing_ages_time_without_new_data(self) -> None:
        node = NodeMetadata("!aging0001", "Ager", "AG", 1, last_heard=NOW - 5)
        state = MeshNodeState(
            node=node, is_client=False, is_relay=False, last_interaction_at=None
        )
        self.assertEqual(format_mesh_node_bar_fields(state, now=NOW).elapse_text, "5s")
        self.assertEqual(format_mesh_node_bar_fields(state, now=NOW + 30).elapse_text, "35s")

    def test_link_observation_age_never_substitutes_for_node_time(self) -> None:
        """A stale/irrelevant LINK observation must never influence TIME --

        TIME is purely the node's own last_interaction_at/last_heard,
        never LINK's observed_at.
        """
        node = NodeMetadata("!linkage1", "LinkAge", "LA", 1, last_heard=NOW - 5)
        state = MeshNodeState(
            node=node, is_client=False, is_relay=False, last_interaction_at=None
        )
        stale_link = LinkObservation(rssi=-90, snr=-5, observed_at=NOW - 9999)
        fields = format_mesh_node_bar_fields(state, now=NOW, link=stale_link)
        self.assertEqual(fields.elapse_text, "5s")

    def test_you_time_is_always_now_regardless_of_last_heard(self) -> None:
        you_with_stale_last_heard = NodeMetadata("!you", "Local", "ME", 0, NOW - 99999, True)
        state = MeshNodeState(
            node=you_with_stale_last_heard,
            is_client=False,
            is_relay=False,
            last_interaction_at=None,
        )
        self.assertEqual(format_mesh_node_bar_fields(state, now=NOW).elapse_text, "NOW")


class FormatMeshLinkDisplayTests(unittest.TestCase):
    """UI POLISH Part C: passive LINK quality for the selected node.

    format_mesh_link_display never fabricates a value -- see its own
    docstring -- and _mesh_link_meter's SNR->bar mapping is exercised
    indirectly through it, at each threshold boundary.
    """

    def test_no_observation_is_the_honest_placeholder(self) -> None:
        display = format_mesh_link_display(None, now=NOW)
        self.assertEqual(display, MeshLinkDisplay(MESH_LINK_METER_UNKNOWN, "--", "--"))

    def test_fresh_direct_observation_is_shown(self) -> None:
        observation = LinkObservation(rssi=-52, snr=9.5, observed_at=NOW - 5)
        display = format_mesh_link_display(observation, now=NOW)
        self.assertEqual(display.rssi_text, "-52")
        self.assertEqual(display.snr_text, "+10")  # round-half-to-even(9.5) == 10
        self.assertEqual(display.meter, "▂▄▆█")

    def test_observation_at_exactly_active_window_is_stale(self) -> None:
        """Reuses the SAME ACTIVE_WINDOW_SECONDS boundary MESH's own

        activity tier already treats as "no longer active" (see
        is_node_active's own `age < ACTIVE_WINDOW_SECONDS`).
        """
        observation = LinkObservation(
            rssi=-52, snr=9.5, observed_at=NOW - ACTIVE_WINDOW_SECONDS
        )
        display = format_mesh_link_display(observation, now=NOW)
        self.assertEqual(display, MeshLinkDisplay(MESH_LINK_METER_UNKNOWN, "--", "--"))

    def test_observation_one_second_inside_window_is_fresh(self) -> None:
        observation = LinkObservation(
            rssi=-52, snr=9.5, observed_at=NOW - ACTIVE_WINDOW_SECONDS + 1
        )
        display = format_mesh_link_display(observation, now=NOW)
        self.assertNotEqual(display.meter, MESH_LINK_METER_UNKNOWN)

    def test_negative_age_is_treated_as_the_honest_placeholder(self) -> None:
        observation = LinkObservation(rssi=-52, snr=9.5, observed_at=NOW + 5)
        display = format_mesh_link_display(observation, now=NOW)
        self.assertEqual(display, MeshLinkDisplay(MESH_LINK_METER_UNKNOWN, "--", "--"))

    def test_missing_rssi_alone_shows_dash_for_rssi_only(self) -> None:
        observation = LinkObservation(rssi=None, snr=6.0, observed_at=NOW)
        display = format_mesh_link_display(observation, now=NOW)
        self.assertEqual(display.rssi_text, "--")
        self.assertEqual(display.snr_text, "+6")

    def test_missing_snr_alone_shows_dash_for_snr_and_unknown_meter(self) -> None:
        observation = LinkObservation(rssi=-52, snr=None, observed_at=NOW)
        display = format_mesh_link_display(observation, now=NOW)
        self.assertEqual(display.snr_text, "--")
        self.assertEqual(display.meter, MESH_LINK_METER_UNKNOWN)

    def test_meter_thresholds_are_deterministic_boundaries(self) -> None:
        cases = [
            (MESH_LINK_SNR_EXCELLENT_DB, "▂▄▆█"),
            (MESH_LINK_SNR_EXCELLENT_DB - 0.1, "▂▄▆"),
            (MESH_LINK_SNR_GOOD_DB, "▂▄▆"),
            (MESH_LINK_SNR_GOOD_DB - 0.1, "▂▄"),
            (MESH_LINK_SNR_WEAK_DB, "▂▄"),
            (MESH_LINK_SNR_WEAK_DB - 0.1, "▂"),
            (-20.0, "▂"),  # LoRa can remain viable at strongly negative SNR
        ]
        for snr, expected_meter in cases:
            with self.subTest(snr=snr):
                observation = LinkObservation(rssi=-90, snr=snr, observed_at=NOW)
                self.assertEqual(
                    format_mesh_link_display(observation, now=NOW).meter, expected_meter
                )

    def test_same_reading_always_yields_the_same_meter(self) -> None:
        """Deterministic: no animation/randomization/smoothing."""
        observation = LinkObservation(rssi=-52, snr=9.5, observed_at=NOW)
        results = {
            format_mesh_link_display(observation, now=NOW).meter for _ in range(20)
        }
        self.assertEqual(len(results), 1)


class FormatMeshNodeBarLineTests(unittest.TestCase):
    """MESH GPS + UNIFIED BAR Part B: assembling MeshNodeBarFields into

    ONE physical line, degrading deterministically (compact precision
    first, then drop lower-priority fields) rather than wrapping.
    """

    REMOTE_FIELDS = MeshNodeBarFields(
        long_name="SomeNode",
        short_name="NODE",
        hops_text="2",
        gps_text="40.7634, -73.9508",
        gps_text_compact="40.76,-73.95",
        distance_text="3.2 km",
        link_meter="▂▄▆█",
        link_rssi_text="-87",
        link_snr_text="+6",
        elapse_text="25s",
        accent2=False,
    )

    def test_full_width_shows_the_complete_tier(self) -> None:
        text = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=200)
        self.assertEqual(
            text,
            "SomeNode • NODE • HOPS 2 • "
            "GPS 40.7634, -73.9508 • DISTANCE 3.2 km • "
            "LINK ▂▄▆█ -87 / +6 • ELAPSE 25s",
        )

    def test_unmeasured_width_shows_the_fullest_tier_untouched(self) -> None:
        """available_width <= 0 (not yet laid out) must never truncate."""
        text = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=0)
        self.assertEqual(
            text, format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=1000)
        )

    def test_medium_width_compacts_gps_precision_and_link_spacing(self) -> None:
        full = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=1000)
        compact = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=len(full) - 1)
        self.assertIn("GPS 40.76,-73.95", compact)
        self.assertIn("LINK ▂▄▆█ -87/+6", compact)
        self.assertLess(cell_len(compact), cell_len(full))

    def test_dropping_gps_only_keeps_the_other_fields(self) -> None:
        tier3 = (
            "SomeNode • NODE • HOPS 2 • DISTANCE 3.2 km "
            "• LINK ▂▄▆█ -87/+6 • ELAPSE 25s"
        )
        text = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=len(tier3))
        self.assertEqual(text, tier3)
        self.assertNotIn("GPS", text)
        self.assertIn("DISTANCE", text)

    def test_narrow_width_drops_gps_and_distance(self) -> None:
        tier4 = "SomeNode • NODE • HOPS 2 • LINK ▂▄▆█ -87/+6 • ELAPSE 25s"
        text = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=len(tier4))
        self.assertEqual(text, tier4)
        self.assertNotIn("GPS", text)
        self.assertNotIn("DISTANCE", text)

    def test_very_narrow_width_drops_short_name_too(self) -> None:
        tier5 = "SomeNode • HOPS 2 • LINK ▂▄▆█ -87/+6 • ELAPSE 25s"
        text = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=len(tier5))
        self.assertEqual(text, tier5)
        self.assertNotIn("NODE", text)

    def test_narrowest_useful_width_drops_link_too(self) -> None:
        tier6 = "SomeNode • HOPS 2 • ELAPSE 25s"
        text = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=len(tier6))
        self.assertEqual(text, tier6)
        self.assertNotIn("LINK", text)

    def test_absurdly_narrow_width_still_grapheme_safe_truncates(self) -> None:
        text = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=5)
        self.assertLessEqual(cell_len(text), 5)

    def test_no_long_name_or_short_name_descriptor_labels(self) -> None:
        """FINAL MESHTASTIC POLISH: the "LONG NAME"/"SHORT NAME" descriptor

        words are gone -- only the bare values remain, long name first,
        short name second. HOPS/GPS/DISTANCE/LINK keep their own
        descriptors unchanged.
        """
        text = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=200)
        self.assertNotIn("LONG NAME", text)
        self.assertNotIn("SHORT NAME", text)
        self.assertTrue(text.startswith("SomeNode • NODE • HOPS"))

    def test_you_bar_omits_distance_link_and_elapse_entirely(self) -> None:
        """YOU carries no distance-from-self, no self-link, and no

        meaningful ELAPSE ("NOW"), so those three fields are dropped
        outright -- never shown as placeholders. Only long name / short
        name / HOPS / GPS remain.
        """
        node = NodeMetadata("!you", "Polytrigon", "POLY", 0, NOW, True, position=YOU_POSITION)
        state = MeshNodeState(
            node=node, is_client=False, is_relay=False, last_interaction_at=None
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        text = format_mesh_node_bar_line(fields, available_width=200)
        self.assertEqual(
            text, "Polytrigon • POLY • HOPS 0 • GPS 40.7128, -74.0060"
        )
        self.assertNotIn("YOU", text)
        self.assertNotIn("DISTANCE", text)
        self.assertNotIn("LINK", text)
        self.assertNotIn("ELAPSE", text)
        self.assertTrue(fields.accent2)

    def test_you_bar_degrades_to_hops_and_gps_then_drops_gps(self) -> None:
        node = NodeMetadata("!you", "Polytrigon", "POLY", 0, NOW, True, position=YOU_POSITION)
        state = MeshNodeState(
            node=node, is_client=False, is_relay=False, last_interaction_at=None
        )
        fields = format_mesh_node_bar_fields(state, now=NOW)
        full = format_mesh_node_bar_line(fields, available_width=200)
        narrower = format_mesh_node_bar_line(fields, available_width=len(full) - 1)
        self.assertIn("GPS 40.71,-74.01", narrower)  # 2dp compaction
        keep_short = "Polytrigon • POLY • HOPS 0"
        no_gps = format_mesh_node_bar_line(fields, available_width=len(keep_short))
        self.assertEqual(no_gps, keep_short)
        drop_short = format_mesh_node_bar_line(fields, available_width=len(keep_short) - 1)
        self.assertEqual(drop_short, "Polytrigon • HOPS 0")

    def test_remote_bar_still_includes_distance_link_and_elapse(self) -> None:
        text = format_mesh_node_bar_line(self.REMOTE_FIELDS, available_width=200)
        self.assertIn("DISTANCE 3.2 km", text)
        self.assertIn("LINK ▂▄▆█ -87 / +6", text)
        self.assertIn("ELAPSE 25s", text)

    def test_link_meter_glyphs_are_single_cell_narrow(self) -> None:
        """The bar glyphs are ordinary block-drawing characters -- never

        the kind of combining-mark/variation-selector sequence UI
        POLISH Part A investigates -- so they can never trigger that
        same class of terminal-cell-width disagreement.
        """
        for meter in ("▂▄▆█", "▂▄▆", "▂▄", "▂", MESH_LINK_METER_UNKNOWN):
            with self.subTest(meter=meter):
                self.assertEqual(cell_len(meter), len(meter))


if __name__ == "__main__":
    unittest.main()
