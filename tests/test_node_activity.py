"""Tests for passive ACTIVE-node policy."""

import math
import unittest

from node_activity import ACTIVE_WINDOW_SECONDS, count_active_other_nodes


class NodeActivityTests(unittest.TestCase):
    def test_counts_unique_recent_other_nodes_and_excludes_self(self) -> None:
        now = 1_000.0
        nodes = (
            (1, {"user": {"id": "!self0001"}, "lastHeard": now}),
            (2, {"user": {"id": "!other002"}, "lastHeard": now - 10}),
            (3, {"user": {"id": "!OTHER002"}, "lastHeard": now - 20}),
            (4, {"user": {"id": "!other004"}, "lastHeard": now - 299}),
        )

        self.assertEqual(
            count_active_other_nodes(
                nodes,
                local_node_number=1,
                local_node_id="!self0001",
                now=now,
            ),
            2,
        )

    def test_exact_active_window_boundary_is_inactive(self) -> None:
        now = ACTIVE_WINDOW_SECONDS + 1_000.0
        nodes = ((2, {"lastHeard": now - ACTIVE_WINDOW_SECONDS}),)

        self.assertEqual(
            count_active_other_nodes(
                nodes,
                local_node_number=1,
                local_node_id=None,
                now=now,
            ),
            0,
        )

    def test_excludes_self_by_node_id_when_numeric_key_differs(self) -> None:
        self.assertEqual(
            count_active_other_nodes(
                ((99, {"user": {"id": "!self0001"}, "lastHeard": 999}),),
                local_node_number=1,
                local_node_id="!SELF0001",
                now=1_000,
            ),
            0,
        )

    def test_ignores_missing_malformed_future_and_impossible_times(self) -> None:
        nodes = (
            (2, {}),
            (3, {"lastHeard": "999"}),
            (4, {"lastHeard": True}),
            (5, {"lastHeard": math.nan}),
            (6, {"lastHeard": math.inf}),
            (7, {"lastHeard": 0}),
            (8, {"lastHeard": 1_001}),
            ("bad-key", {"lastHeard": 999}),
            (9, "not a record"),
        )

        self.assertEqual(
            count_active_other_nodes(
                nodes,
                local_node_number=1,
                local_node_id=None,
                now=1_000,
            ),
            0,
        )

    def test_uses_numeric_node_key_when_user_id_is_missing(self) -> None:
        self.assertEqual(
            count_active_other_nodes(
                ((0xABC12345, {"lastHeard": 990}),),
                local_node_number=1,
                local_node_id="!00000001",
                now=1_000,
            ),
            1,
        )

    def test_combines_direct_observations_with_database_by_unique_node(self) -> None:
        now = 1_000.0
        nodes = (
            (2, {"user": {"id": "!alice001"}, "lastHeard": now - 900}),
            (3, {"user": {"id": "!bob00003"}, "lastHeard": now - 10}),
        )
        observations = {
            "!ALICE001": now,
            "!bob00003": now - 900,
            "!self0001": now,
        }

        self.assertEqual(
            count_active_other_nodes(
                nodes,
                local_node_number=1,
                local_node_id="!self0001",
                now=now,
                direct_observations=observations,
            ),
            2,
        )

    def test_direct_observation_ages_out_at_exact_boundary(self) -> None:
        now = ACTIVE_WINDOW_SECONDS + 1_000
        self.assertEqual(
            count_active_other_nodes(
                (),
                local_node_number=1,
                local_node_id="!self0001",
                now=now,
                direct_observations={"!alice001": now - ACTIVE_WINDOW_SECONDS},
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
