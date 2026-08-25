"""Tests for MESH's bounded working-set ranking and freshness classification."""

from __future__ import annotations

import unittest

from mesh_state import (
    DEFAULT_MAX_REMOTE_NODES,
    RECENT_ACTIVITY_SECONDS,
    VERY_STALE_SECONDS,
    build_display_nodes,
    classify_recency,
)
from radio_service import NodeMetadata


NOW = 1_000_000.0


class ClassifyRecencyTests(unittest.TestCase):
    def test_recent_boundary_is_inclusive(self) -> None:
        self.assertEqual(classify_recency(NOW - RECENT_ACTIVITY_SECONDS, NOW), "recent")
        self.assertEqual(
            classify_recency(NOW - RECENT_ACTIVITY_SECONDS - 1, NOW), "stale"
        )

    def test_very_stale_boundary_is_inclusive_for_stale(self) -> None:
        self.assertEqual(classify_recency(NOW - VERY_STALE_SECONDS, NOW), "stale")
        self.assertEqual(
            classify_recency(NOW - VERY_STALE_SECONDS - 1, NOW), "very_stale"
        )

    def test_missing_or_malformed_activity_is_unknown(self) -> None:
        self.assertEqual(classify_recency(None, NOW), "unknown")
        self.assertEqual(classify_recency("not a number", NOW), "unknown")
        self.assertEqual(classify_recency(True, NOW), "unknown")

    def test_future_timestamp_is_unknown_not_fabricated_as_fresh(self) -> None:
        self.assertEqual(classify_recency(NOW + 60, NOW), "unknown")


class BuildDisplayNodesTests(unittest.TestCase):
    def test_working_set_is_bounded_to_max_remote_nodes(self) -> None:
        nodes = [NodeMetadata("!you", "YOU", None, 0, NOW, True)]
        for index in range(20):
            nodes.append(
                NodeMetadata(f"!n{index:04x}", f"Node{index}", None, None, NOW - index)
            )
        result = build_display_nodes(nodes, now=NOW, is_favorite=lambda _n: False)
        self.assertEqual(len(result) - 1, DEFAULT_MAX_REMOTE_NODES)

    def test_never_renders_the_full_historical_node_database(self) -> None:
        nodes = [NodeMetadata("!you", "YOU", None, 0, NOW, True)]
        nodes.extend(
            NodeMetadata(f"!n{i:04x}", f"Node{i}", None, None, NOW - i)
            for i in range(200)
        )
        result = build_display_nodes(nodes, now=NOW, is_favorite=lambda _n: False)
        self.assertLess(len(result), len(nodes))

    def test_recent_message_activity_ranks_above_last_heard_only(self) -> None:
        nodes = [
            NodeMetadata("!you", "YOU", None, 0, NOW, True),
            NodeMetadata("!heard_recent", "HeardOnly", None, None, NOW - 10),
            NodeMetadata("!messaged_old", "MessagedOlder", None, None, NOW - 5_000),
        ]
        result = build_display_nodes(
            nodes,
            now=NOW,
            is_favorite=lambda _n: False,
            last_message_at={"!messaged_old": NOW - 5_000},
        )
        remote_ids = [display.node.node_id for display in result if not display.node.is_local]
        self.assertEqual(remote_ids[0], "!messaged_old")

    def test_favorite_beats_plain_recency_when_no_recent_message(self) -> None:
        nodes = [NodeMetadata("!you", "YOU", None, 0, NOW, True)]
        for index in range(10):
            nodes.append(
                NodeMetadata(
                    f"!n{index:04x}", f"Node{index}", None, None, NOW - (index + 1) * 100_000
                )
            )
        oldest_id = "!n0009"
        result = build_display_nodes(
            nodes,
            now=NOW,
            is_favorite=lambda node_id: node_id.lower() == oldest_id,
            max_remote_nodes=8,
        )
        remote_ids = {display.node.node_id for display in result if not display.node.is_local}
        self.assertIn(oldest_id, remote_ids)
        remote_display = next(d for d in result if d.node.node_id == oldest_id)
        self.assertTrue(remote_display.favorite)

    def test_stale_fallback_shows_something_useful_when_nothing_recent(self) -> None:
        nodes = [NodeMetadata("!you", "YOU", None, 0, NOW, True)]
        for index in range(5):
            nodes.append(
                NodeMetadata(
                    f"!n{index:04x}",
                    f"Node{index}",
                    None,
                    None,
                    NOW - VERY_STALE_SECONDS * 2,
                )
            )
        result = build_display_nodes(nodes, now=NOW, is_favorite=lambda _n: False)
        # Board is not empty just because nothing recent happened.
        self.assertEqual(len(result) - 1, 5)
        self.assertTrue(all(d.recency_bucket == "very_stale" for d in result[1:]))

    def test_stale_node_is_never_classified_as_active(self) -> None:
        nodes = [
            NodeMetadata("!you", "YOU", None, 0, NOW, True),
            NodeMetadata("!old", "Old", None, None, NOW - VERY_STALE_SECONDS - 1),
        ]
        result = build_display_nodes(nodes, now=NOW, is_favorite=lambda _n: False)
        remote = next(d for d in result if not d.node.is_local)
        self.assertNotEqual(remote.recency_bucket, "recent")

    def test_relationship_kind_direct_only_from_trustworthy_message_activity(
        self,
    ) -> None:
        nodes = [
            NodeMetadata("!you", "YOU", None, 0, NOW, True),
            NodeMetadata("!messaged", "Messaged", None, None, NOW - 10),
            NodeMetadata("!context_only", "ContextOnly", None, None, NOW - 10),
        ]
        result = build_display_nodes(
            nodes,
            now=NOW,
            is_favorite=lambda _n: False,
            last_message_at={"!messaged": NOW - 10},
        )
        by_id = {d.node.node_id: d for d in result}
        self.assertEqual(by_id["!messaged"].relationship_kind, "direct")
        self.assertEqual(by_id["!context_only"].relationship_kind, "context")
        # "relay" is reserved for a future trustworthy data source; nothing
        # in this application can produce it today (see module docstring).
        self.assertTrue(
            all(d.relationship_kind != "relay" for d in result)
        )

    def test_ranking_is_deterministic_and_arrival_order_independent(self) -> None:
        nodes = [NodeMetadata("!you", "YOU", None, 0, NOW, True)]
        for index in range(10):
            nodes.append(
                NodeMetadata(f"!n{index:04x}", f"Node{index}", None, None, NOW - index * 7)
            )
        forward = build_display_nodes(nodes, now=NOW, is_favorite=lambda _n: False)
        backward = build_display_nodes(
            list(reversed(nodes)), now=NOW, is_favorite=lambda _n: False
        )
        self.assertEqual(
            [d.node.node_id for d in forward], [d.node.node_id for d in backward]
        )

    def test_duplicate_node_ids_are_deduplicated(self) -> None:
        nodes = [
            NodeMetadata("!you", "YOU", None, 0, NOW, True),
            NodeMetadata("!DUP0001", "First", None, None, NOW),
            NodeMetadata("!dup0001", "Second", None, None, NOW),
        ]
        result = build_display_nodes(nodes, now=NOW, is_favorite=lambda _n: False)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
