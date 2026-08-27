"""Passive Meshtastic node-activity calculations."""

from __future__ import annotations

from math import isfinite
from typing import Any, Iterable, Mapping


# 2 hours -- FIRMWARE-SOURCE-VERIFIED to match Meshtastic firmware's own
# "N Online" threshold exactly: NUM_ONLINE_SECS in src/mesh/NodeDB.cpp
# (`#define NUM_ONLINE_SECS (60 * 60 * 2)`), used by
# NodeDB::getNumOnlineMeshNodes() as `sinceLastSeen(node) < NUM_ONLINE_SECS`
# -- i.e. purely last_heard recency, no hop-count/neighbor-status
# involvement, unchanged since a 2022 typo fix (5e109d9) predating both
# firmware 2.5 and 2.7. This was previously 300.0 (5 minutes), a
# MeshtasticPass-invented value with no firmware basis -- that mismatch
# (24x narrower than firmware's own window) is why MESH(N) could read
# dramatically lower than the radio's own onscreen "N ONLINE" and could
# fall from several active nodes to almost none within a few quiet
# minutes, even though firmware still considered those same nodes
# legitimately online. See mesh_state.MeshActivityTier for the
# STALE/VERY_OLD tiers firmware itself does not define (its own
# online/offline split is binary).
ACTIVE_WINDOW_SECONDS = 60 * 60 * 2


def is_node_active(last_heard: object, now: object) -> bool:
    """Return whether a trustworthy receive time is inside the active window."""
    if (
        not isinstance(now, (int, float))
        or isinstance(now, bool)
        or not isfinite(now)
        or not isinstance(last_heard, (int, float))
        or isinstance(last_heard, bool)
        or not isfinite(last_heard)
        or last_heard <= 0
    ):
        return False
    age = float(now) - float(last_heard)
    return 0 <= age < ACTIVE_WINDOW_SECONDS


def count_active_other_nodes(
    nodes: Iterable[tuple[Any, Any]],
    *,
    local_node_number: int | None,
    local_node_id: str | None,
    now: float,
    direct_observations: Mapping[str, float] | None = None,
) -> int:
    """Count unique other nodes heard within ACTIVE_WINDOW_SECONDS (2 hours,

    matching Meshtastic firmware's own "N Online" threshold).

    Node-database ``lastHeard`` values and packets directly observed by the
    application are combined by node ID. A node heard exactly
    ``ACTIVE_WINDOW_SECONDS`` ago is no longer active. Future, missing,
    malformed, and non-finite timestamps are ignored.
    """
    if not isinstance(now, (int, float)) or isinstance(now, bool) or not isfinite(now):
        return 0

    normalized_local_id = _normalize_node_id(local_node_id)
    active_ids: set[str] = set()
    for node_number, record in nodes:
        if not isinstance(record, dict):
            continue
        if (
            local_node_number is not None
            and isinstance(node_number, int)
            and not isinstance(node_number, bool)
            and node_number == local_node_number
        ):
            continue

        node_id = _node_id(node_number, record)
        if node_id is None or node_id == normalized_local_id:
            continue

        if is_node_active(record.get("lastHeard"), now):
            active_ids.add(node_id)

    for raw_node_id, observed_at in (direct_observations or {}).items():
        node_id = _normalize_node_id(raw_node_id)
        if node_id is None or node_id == normalized_local_id:
            continue
        if is_node_active(observed_at, now):
            active_ids.add(node_id)

    return len(active_ids)


def _node_id(node_number: Any, record: dict[str, Any]) -> str | None:
    user = record.get("user")
    if isinstance(user, dict):
        node_id = _normalize_node_id(user.get("id"))
        if node_id is not None:
            return node_id
    if isinstance(node_number, int) and not isinstance(node_number, bool):
        return f"!{node_number:08x}"
    return None


def _normalize_node_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()
