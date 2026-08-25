"""Application-level MESH working-set selection, observed roles, and staleness.

Kept separate from rendering (app.py) and from pure grid geometry
(mesh_topology.py) so ranking/role/staleness rules are independently
testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from mesh_topology import compact_node_label
from radio_service import NodeMetadata
from relative_time import format_relative_age


MESH_STALE_THRESHOLD_SECONDS = 24 * 60 * 60
DEFAULT_MAX_REMOTE_NODES = 8


@dataclass(frozen=True)
class MeshNodeState:
    """One node's derived MESH state: OBSERVED COMMUNICATION ROLE + timing.

    `is_client` / `is_relay` are OBSERVED COMMUNICATION ROLES for MESH's
    visualization -- NOT a node's configured Meshtastic device role:

    - CLIENT: this node has originated at least one message we received.
    - RELAY: we have trustworthy evidence this node relayed a message
      that another node originated.

    Neither implies the other; a node can be CLIENT, RELAY, or both.

    No data source currently available to this app (see
    radio_service.RadioService._parse_text_packet) identifies which
    specific node relayed a given packet -- an ordinary Meshtastic text
    packet exposes only an aggregate hop COUNT (`hopsAway`), never
    forwarder identity, and this app does not send traceroutes or any
    other active discovery request to find out. `is_relay` is therefore
    always False today. The field exists, and this dataclass is shaped
    around it, so a future trustworthy source (should the SDK or a later
    packet type expose one) can populate it without a model change --
    but nothing in this codebase may set it from hop count, a configured
    role, geographic position, or any other proxy/guess.
    """

    node: NodeMetadata
    is_client: bool
    is_relay: bool
    last_interaction_at: float | None

    def is_stale(self, *, now: float) -> bool:
        """>24h since the last interaction; exactly 24h still counts as

        recent. A node with no known interaction timestamp is treated as
        stale -- there is nothing recent to point to, so "recent" would
        be fabricated, not observed.
        """
        if self.last_interaction_at is None:
            return True
        return (now - self.last_interaction_at) > MESH_STALE_THRESHOLD_SECONDS

    def glyph_is_solid(self) -> bool:
        """CLIENT appearance (solid) takes priority over RELAY-only (stroked).

        CLIENT and CLIENT+RELAY both render solid; only RELAY-with-no-
        CLIENT renders stroked. YOU (is_client=False, is_relay=False) has
        no observed role and is always solid -- callers render YOU solid
        unconditionally rather than through this method; see
        MeshNodeWidget in app.py.
        """
        if self.is_client:
            return True
        return not self.is_relay


def build_mesh_working_set(
    nodes: Iterable[NodeMetadata],
    *,
    last_message_at: Mapping[str, float] | None = None,
    max_remote_nodes: int = DEFAULT_MAX_REMOTE_NODES,
) -> tuple[MeshNodeState, ...]:
    """Rank observed CLIENT nodes and return YOU plus the top N most recent.

    Every remote MeshNodeState here is CLIENT by construction: with no
    trustworthy relay-identity source available (see MeshNodeState),
    CLIENT is the only observed communication role this app can currently
    establish, so a node is included here if and only if it appears in
    `last_message_at` -- the caller's merged view of persisted CHAT
    history plus any not-yet-persisted in-memory activity, so this
    reflects true history rather than just the current session.

    Ranking is purely by recency of that last interaction, most recent
    first. There is no separate "recent" vs "historical" priority tier:
    with only one observed role, "most recently relevant" and "most
    recently active" are the same criterion, so the same ranking
    naturally falls through to the most recent stale nodes instead of
    leaving the board empty when nothing is recent (see MeshNodeState.
    is_stale for the recent/stale boundary, applied independently at
    render/context time -- ranking never excludes a node merely for
    being stale). Ties break on node_id for determinism.

    `nodes` supplies richer NodeMetadata (name, hops_away, position) when
    available, matched case-insensitively against `last_message_at`'s
    keys; a CLIENT node absent from `nodes` (e.g. its passive node-
    database record has not synced yet) still appears, with only its
    node ID known.
    """
    last_message_at = last_message_at or {}
    local: NodeMetadata | None = None
    known_by_id: dict[str, NodeMetadata] = {}
    for node in nodes:
        key = node.node_id.strip().lower()
        if not key:
            continue
        if node.is_local:
            if local is None:
                local = node
            continue
        known_by_id.setdefault(key, node)

    candidates: list[MeshNodeState] = []
    for node_id, interaction_at in last_message_at.items():
        node = known_by_id.get(node_id) or NodeMetadata(node_id)
        candidates.append(
            MeshNodeState(
                node=node,
                is_client=True,
                is_relay=False,
                last_interaction_at=interaction_at,
            )
        )

    candidates.sort(
        key=lambda state: (
            -(state.last_interaction_at or 0.0),
            state.node.node_id.casefold(),
        )
    )
    bounded = tuple(candidates[: max(0, max_remote_nodes)])

    if local is None:
        return bounded
    you_state = MeshNodeState(
        node=local, is_client=False, is_relay=False, last_interaction_at=None
    )
    return (you_state, *bounded)


def format_mesh_context_line(state: MeshNodeState, *, now: float) -> str:
    """Build the bottom-left status text for a selected MESH node.

    YOU has no observed communication role, so its line is just its
    compact label. A remote node's line is "LABEL / ROLE / N HOPS / AGE";
    ROLE is CLIENT, RELAY, or CLIENT+RELAY. Unknown hop count or unknown
    interaction age render as "?" rather than a fabricated number.
    """
    label = compact_node_label(state.node)
    if state.node.is_local:
        return label

    role = "+".join(
        name
        for name, present in (("CLIENT", state.is_client), ("RELAY", state.is_relay))
        if present
    ) or "?"

    hops = state.node.hops_away
    hops_text = f"{hops} HOPS" if hops is not None else "? HOPS"

    if state.last_interaction_at is None or state.last_interaction_at > now:
        age_text = "?"
    else:
        age_text = format_relative_age(now - state.last_interaction_at)

    return f"{label} / {role} / {hops_text} / {age_text}"
