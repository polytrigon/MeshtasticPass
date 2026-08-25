"""Application-level MESH working-set selection and freshness classification.

Kept separate from rendering (app.py) and from pure grid geometry
(mesh_topology.py) so ranking and staleness rules are independently testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Literal, Mapping

from radio_service import NodeMetadata


RECENT_ACTIVITY_SECONDS = 24 * 60 * 60
VERY_STALE_SECONDS = 48 * 60 * 60
DEFAULT_MAX_REMOTE_NODES = 8

RecencyBucket = Literal["recent", "stale", "very_stale", "unknown"]
RelationshipKind = Literal["direct", "relay", "context"]


@dataclass(frozen=True)
class MeshDisplayNode:
    """One node's app-level MESH display state, independent of layout/rendering."""

    node: NodeMetadata
    last_message_at: float | None
    reference_activity: float | None
    favorite: bool
    relationship_kind: RelationshipKind
    recency_bucket: RecencyBucket


def classify_recency(reference_activity: object, now: float) -> RecencyBucket:
    """Bucket a node's freshness from its best-known trustworthy activity time."""
    if (
        not isinstance(reference_activity, (int, float))
        or isinstance(reference_activity, bool)
    ):
        return "unknown"
    age = now - reference_activity
    if age < 0:
        return "unknown"
    if age <= RECENT_ACTIVITY_SECONDS:
        return "recent"
    if age <= VERY_STALE_SECONDS:
        return "stale"
    return "very_stale"


def _display_priority(
    display: MeshDisplayNode, now: float
) -> tuple[int, int, float, str]:
    """Lower sorts first / more relevant.

    Order: recent (<=24h) received-message activity, then Favorite state,
    then last-known-activity recency, then a stable node-ID tie-break.
    Trustworthy relay/routing relevance would sit between the first two
    tiers, but no such signal exists yet -- see build_display_nodes().
    """
    has_recent_message = (
        display.last_message_at is not None
        and 0 <= now - display.last_message_at <= RECENT_ACTIVITY_SECONDS
    )
    recency_rank = (
        -display.reference_activity
        if isinstance(display.reference_activity, (int, float))
        and not isinstance(display.reference_activity, bool)
        else float("inf")
    )
    return (
        0 if has_recent_message else 1,
        0 if display.favorite else 1,
        recency_rank,
        display.node.node_id.casefold(),
    )


def build_display_nodes(
    nodes: Iterable[NodeMetadata],
    *,
    now: float,
    is_favorite: Callable[[str], bool],
    last_message_at: Mapping[str, float] = {},
    max_remote_nodes: int = DEFAULT_MAX_REMOTE_NODES,
) -> tuple[MeshDisplayNode, ...]:
    """Rank all known nodes and return YOU plus the top `max_remote_nodes`.

    MESH is a bounded, deliberate working set -- this never returns the full
    historical node database. When nothing recent has happened, ranking
    naturally falls through to Favorite state and then last-heard recency,
    so the board still shows the most recently useful known state instead
    of going blank.

    Relay/routing participation is not represented: the Meshtastic Python
    SDK integration this app uses does not expose a trustworthy per-hop
    relay path, only `hopsAway` (a proximity count) and passive receive
    timestamps. `relationship_kind` therefore only ever resolves to
    "direct" (this node sent a message we received) or "context" (known
    only via the node database) today; "relay" is reserved so a future
    trustworthy data source can populate it without a model change.
    """
    local: NodeMetadata | None = None
    displays: list[MeshDisplayNode] = []
    seen: set[str] = set()
    for node in nodes:
        key = node.node_id.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if node.is_local:
            local = node
            continue
        message_at = last_message_at.get(key)
        reference_activity = message_at if message_at is not None else node.last_heard
        displays.append(
            MeshDisplayNode(
                node=node,
                last_message_at=message_at,
                reference_activity=reference_activity,
                favorite=bool(is_favorite(node.node_id)),
                relationship_kind="direct" if message_at is not None else "context",
                recency_bucket=classify_recency(reference_activity, now),
            )
        )

    displays.sort(key=lambda display: _display_priority(display, now))
    bounded = tuple(displays[: max(0, max_remote_nodes)])

    if local is None:
        return bounded
    local_display = MeshDisplayNode(
        node=local,
        last_message_at=None,
        reference_activity=None,
        favorite=False,
        relationship_kind="direct",
        recency_bucket="recent",
    )
    return (local_display, *bounded)
