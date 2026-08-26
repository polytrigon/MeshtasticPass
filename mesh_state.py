"""Application-level MESH working-set selection, observed roles, and staleness.

Kept separate from rendering (app.py) and from pure grid geometry
(mesh_topology.py) so ranking/role/staleness rules are independently
testable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from geo import distance_between
from mesh_topology import compact_node_label
from radio_service import NodeMetadata
from relative_time import format_relative_age


MESH_STALE_THRESHOLD_SECONDS = 24 * 60 * 60
DEFAULT_MAX_REMOTE_NODES = 8


def normalize_mesh_node_id(node_id: str) -> str:
    """Canonical MESH node-ID form: lowercase, "!"-prefixed 8-hex-digit.

    Two representations of the SAME physical node must compare equal
    everywhere MESH keys anything by node ID -- the working set, grid
    layout positions, rendered widgets, and directional navigation all
    join on this string. If any two of those disagreed on case, or one
    carried a bare decimal node number (e.g. "123456789") where another
    carried the standard "!075bcd15" hex form, the affected node would
    silently vanish from ONE join (typically navigation, which looks up
    an exact key) while still rendering fine via another path that
    happened to tolerate the mismatch -- exactly the failure mode this
    closes. Applied once, at the working-set boundary (see
    build_mesh_working_set), so every MeshNodeState this module ever
    produces already carries the canonical form.
    """
    candidate = node_id.strip()
    if not candidate:
        return candidate
    lowered = candidate.lower()
    if lowered.isdigit():
        try:
            return f"!{int(lowered):08x}"
        except (ValueError, OverflowError):
            return lowered
    return lowered


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
    distance_miles: float | None = None

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
    available, matched against `last_message_at`'s keys through
    normalize_mesh_node_id() (case-insensitive, and tolerant of a bare
    decimal node number where the standard "!hex" form is expected) so a
    node can never split into two silently-different identities depending
    on which source reported it; a CLIENT node absent from `nodes` (e.g.
    its passive node-database record has not synced yet) still appears,
    with only its node ID known. Every MeshNodeState this function
    returns carries that same normalized ID.
    """
    last_message_at = last_message_at or {}
    local: NodeMetadata | None = None
    known_by_id: dict[str, NodeMetadata] = {}
    for node in nodes:
        key = normalize_mesh_node_id(node.node_id)
        if not key:
            continue
        normalized_node = node if node.node_id == key else replace(node, node_id=key)
        if node.is_local:
            if local is None:
                local = normalized_node
            continue
        known_by_id.setdefault(key, normalized_node)

    # Merge by normalized ID, keeping only the most recent timestamp per
    # node -- if the bug this normalizes against already split one
    # node's history across two raw representations, this recombines it
    # rather than silently picking (and thereby truncating) just one.
    normalized_interactions: dict[str, float] = {}
    for raw_node_id, interaction_at in last_message_at.items():
        node_id = normalize_mesh_node_id(raw_node_id)
        if not node_id:
            continue
        if (
            node_id not in normalized_interactions
            or interaction_at > normalized_interactions[node_id]
        ):
            normalized_interactions[node_id] = interaction_at

    local_position = local.position if local is not None else None
    candidates: list[MeshNodeState] = []
    for node_id, interaction_at in normalized_interactions.items():
        node = known_by_id.get(node_id) or NodeMetadata(node_id)
        candidates.append(
            MeshNodeState(
                node=node,
                is_client=True,
                is_relay=False,
                last_interaction_at=interaction_at,
                # Straight-line geographic distance from YOU, derived only
                # from stored GPS coordinates -- never from grid position,
                # hop count, or relay-chain length (see RelayStage). "? mi"
                # (distance_miles=None) whenever either position is
                # unknown, never a fabricated figure.
                distance_miles=distance_between(local_position, node.position),
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


def _clean_text(value: object) -> str | None:
    """Defensive optional-string coercion for a NodeMetadata name field.

    Only a genuine, non-blank ``str`` counts -- never a stray non-string
    field value, and never a string that is blank once trimmed. Guards
    against ever calling ``.strip()`` on something that is not text.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _remote_name_segments(node: NodeMetadata) -> tuple[str, ...]:
    """Long Name first, then Short Name, per the context-line name rule.

    Short Name is included only when it is real and distinct from Long
    Name -- never fabricated as "?"/"UNKNOWN"/"NONE" when absent, and
    never duplicated when the two happen to be the same displayed value.
    If Long Name is unavailable, the sole surviving segment falls back
    to Short Name, then to the node ID itself -- never a blank segment.
    """
    long_name = _clean_text(node.long_name)
    short_name = _clean_text(node.short_name)
    if long_name and short_name and long_name != short_name:
        return (long_name, short_name)
    if long_name:
        return (long_name,)
    if short_name:
        return (short_name,)
    return (node.node_id,)


def _format_distance(distance_miles: float | None) -> str:
    return "? mi" if distance_miles is None else f"{distance_miles:.1f} mi"


def format_mesh_context_line(state: MeshNodeState, *, now: float) -> str:
    """Build the bottom-left status text for a selected REAL MESH node.

    (An anonymous relay-stage placeholder is not a MeshNodeState at all
    -- see mesh_topology.RelayStage -- and, being visual/topology-only,
    can never be selected in the first place (see
    MeshTopologyView.select_node()), so this function is never called
    for one; there is no "RELAY" special case to handle here or in any
    caller. A genuinely identified real RELAY-only node, should
    trustworthy evidence for one ever exist, is an ordinary
    MeshNodeState and renders through the normal path below with
    ROLE=RELAY.)

    YOU has no observed communication role, hop count, interaction
    time, or distance of its own, so its line is just its compact label
    ("YOU"). A remote node's line is:

        LONG NAME [/ SHORT NAME] / ROLE / N HOPS / AGE / DISTANCE

    ROLE is CLIENT, RELAY, or CLIENT+RELAY. Unknown hop count,
    interaction age, or distance each render as "?" rather than a
    fabricated value -- see _remote_name_segments, MeshNodeState.
    distance_miles, and build_mesh_working_set for how each is derived.
    """
    if state.node.is_local:
        return compact_node_label(state.node)

    segments: list[str] = list(_remote_name_segments(state.node))

    role = "+".join(
        name
        for name, present in (("CLIENT", state.is_client), ("RELAY", state.is_relay))
        if present
    ) or "?"
    segments.append(role)

    hops = state.node.hops_away
    segments.append(f"{hops} HOPS" if hops is not None else "? HOPS")

    if state.last_interaction_at is None or state.last_interaction_at > now:
        segments.append("?")
    else:
        segments.append(format_relative_age(now - state.last_interaction_at))

    segments.append(_format_distance(state.distance_miles))

    return " / ".join(segments)
