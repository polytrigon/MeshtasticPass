"""Application-level MESH working-set selection, observed roles, and staleness.

Kept separate from rendering (app.py) and from pure grid geometry
(mesh_topology.py) so ranking/role/staleness rules are independently
testable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable, Mapping

from rich.cells import cell_len

from geo import distance_between, format_coordinates, format_distance
from grapheme_text import truncate_to_cells
from node_activity import ACTIVE_WINDOW_SECONDS, is_node_active
from radio_service import LinkObservation, NodeMetadata
from relative_time import format_relative_age


MESH_STALE_THRESHOLD_SECONDS = 24 * 60 * 60
DEFAULT_MAX_REMOTE_NODES = 8

# Beyond this, a remote node's connector is removed from the CURRENT
# board (see MeshActivityTier.VERY_OLD / build_mesh_working_set) --
# never a NodeDB/CHAT deletion, and never a permanent decision: a node
# that becomes active again is recomputed fresh into the working set on
# the very next refresh, exactly like any other node. Deliberately
# reuses the SAME 24-hour figure MESH_STALE_THRESHOLD_SECONDS already
# used (for the separate, CHAT-interaction-based is_stale() concept
# below) rather than inventing a second arbitrary number -- both agree
# that "over a day since anything real happened" is the point past
# which presenting something as a current connection would mislead.
MESH_VERY_OLD_THRESHOLD_SECONDS = MESH_STALE_THRESHOLD_SECONDS


class MeshActivityTier(Enum):
    """Board/connector rendering tier for an already-admitted remote node.

    Distinct from MeshNodeState.is_stale() below (CHAT-interaction-only,
    24h, decides nothing about rendering today -- see its own
    docstring): this tier decides how a node's CONNECTOR renders on the
    CURRENT board, from the freshest available evidence (last_heard OR
    CHAT interaction, mirroring build_mesh_working_set's own ranking
    recency -- either one is real observed traffic, see item 7 "passive
    only").

    ACTIVE: counts toward MESH(N); normal connector/glyph rendering.
    STALE: excluded from MESH(N), but the connector remains visible --
        historical topology evidence, not a live connection (see
        app._mesh_dashed_glyph for how it renders: DIM + DOTTED).
    VERY_OLD: not rendered on the current board at all (see
        build_mesh_working_set, which filters these out of its
        returned working set entirely). Nothing here ever deletes
        NodeDB or CHAT history -- becoming active again restores
        normal rendering automatically, since the working set is
        always recomputed fresh from live data, never a persisted
        "removed" flag.
    """

    ACTIVE = "active"
    STALE = "stale"
    VERY_OLD = "very_old"


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

    def activity_tier(self, *, now: float) -> "MeshActivityTier":
        """See MeshActivityTier -- board/connector rendering only, never

        NodeDB/CHAT persistence. YOU has no activity concept (mirrors
        _mesh_node_color's existing "YOU always uses ordinary
        ACCENT/BASE" rule) and is always ACTIVE.
        """
        if self.node.is_local:
            return MeshActivityTier.ACTIVE
        candidates = [
            timestamp
            for timestamp in (self.last_interaction_at, self.node.last_heard)
            if timestamp is not None
        ]
        freshest = max(candidates) if candidates else None
        if freshest is not None and is_node_active(freshest, now):
            return MeshActivityTier.ACTIVE
        if freshest is None:
            return MeshActivityTier.VERY_OLD
        age = now - freshest
        if age < MESH_VERY_OLD_THRESHOLD_SECONDS:
            return MeshActivityTier.STALE
        return MeshActivityTier.VERY_OLD

    def glyph_is_solid(self) -> bool:
        """SOLID means exclusively "confirmed CLIENT" -- never fabricated.

        A remote node admitted into the working set purely from passive
        NodeDB/mesh state (see build_mesh_working_set) that has never
        actually originated a message we received is_client=False, and
        renders STROKED, the same shape a RELAY-only node uses -- it has
        not earned the CLIENT glyph just by being known to exist. YOU
        (is_client=False, is_relay=False) has no observed role either,
        but is always solid -- callers render YOU solid unconditionally
        rather than through this method; see MeshNodeWidget in app.py.
        """
        return self.is_client


def build_mesh_working_set(
    nodes: Iterable[NodeMetadata],
    *,
    now: float,
    last_message_at: Mapping[str, float] | None = None,
    favorite_ids: Iterable[str] | None = None,
    max_remote_nodes: int = DEFAULT_MAX_REMOTE_NODES,
) -> tuple[MeshNodeState, ...]:
    """NodeDB-first working set: YOU plus a bounded, ranked set of real nodes.

    `nodes` (the radio's own passively-learned NodeDB, e.g.
    RadioService.get_known_nodes()) is the PRIMARY admission source -- a
    node the radio already knows about (last_heard, hops_away, position,
    names) is a MESH candidate on its own; a CHAT message is never
    required for a node to exist on the board. `last_message_at` (the
    caller's merged view of persisted CHAT history plus any
    not-yet-persisted in-memory activity) is an ENRICHMENT on top of
    that: it marks a candidate CLIENT (see MeshNodeState.glyph_is_solid),
    and its timestamp competes with the node's own `last_heard` for
    `last_interaction_at` (whichever is fresher wins -- either one is
    real evidence the node was recently relevant). It can also admit a
    node NodeDB has no record for yet (e.g. its passive record has not
    synced), exactly as before, but for any node NodeDB already reports
    it is enrichment, never the sole gate.

    A node that is only ever admitted from NodeDB and has never
    originated a message we received keeps is_client=False, is_relay=
    False -- the same "no observed role" shape YOU already uses -- and
    is never fabricated as CLIENT merely for existing; is_client/
    is_relay still drive glyph_is_solid's own STROKED-vs-FILLED node
    styling even though the unified bottom bar (mesh_state.
    format_mesh_node_bar_fields) no longer renders a ROLE field at all
    (MESH GPS + UNIFIED BAR Part B). `last_interaction_at` remains
    specifically CHAT interaction time (None when there is none) --
    unchanged meaning from before, still what is_stale() describes.
    format_mesh_node_bar_fields' own TIME field additionally considers
    `node.last_heard` (taking whichever of the two is fresher), so a
    NodeDB-only node that is_node_active() already counts as ACTIVE
    from last_heard alone can never display "?" there just for lacking
    CHAT history. This same `node.last_heard` recency is otherwise a
    separate signal, used below for ranking only, never folded into
    this field.

    Bounded to `max_remote_nodes` (never every known node -- readability
    over completeness), ranked by a single deterministic key so ranking
    never depends on arrival order or which source reported a node:

      1. currently ACTIVE remote nodes first -- is_node_active(
         node.last_heard, now), the exact same predicate the board's own
         BASE/DIM_BASE styling and [3] MESH (N) use; not a second
         activity definition.
      2. favorites (`favorite_ids`) among the rest.
      3. remaining nodes, most-recently-relevant first, where "relevant"
         is whichever is fresher of CHAT interaction time and NodeDB
         last_heard -- either one is real evidence a node was recently
         worth surfacing, so ranking (unlike last_interaction_at above)
         considers both.
      4. nodes with no timing information from either source, last.

    Ties within a tier break on node_id for determinism.

    `nodes` also supplies richer NodeMetadata (name, hops_away, position)
    when available, matched against `last_message_at`'s keys through
    normalize_mesh_node_id() (case-insensitive, and tolerant of a bare
    decimal node number where the standard "!hex" form is expected) so a
    node can never split into two silently-different identities depending
    on which source reported it. Every MeshNodeState this function
    returns carries that same normalized ID.
    """
    last_message_at = last_message_at or {}
    favorites = {
        normalize_mesh_node_id(favorite_id) for favorite_id in (favorite_ids or ())
    }
    local: NodeMetadata | None = None
    local_candidates: list[NodeMetadata] = []
    known_by_id: dict[str, NodeMetadata] = {}
    for node in nodes:
        key = normalize_mesh_node_id(node.node_id)
        if not key:
            continue
        normalized_node = node if node.node_id == key else replace(node, node_id=key)
        if node.is_local:
            local_candidates.append(normalized_node)
            continue
        known_by_id.setdefault(key, normalized_node)

    # Exactly one YOU: `nodes` (RadioService.get_known_nodes()) is the
    # sole source of is_local flags, and should report at most one --
    # but this function must never simply trust that and pick whichever
    # candidate happened to appear first if it is ever violated (e.g. a
    # transient inconsistency during a physical radio swap). Prefer NO
    # confident YOU over guessing which candidate is genuinely current
    # (see MESH FOLLOW-UP items 5/8): with more than one candidate,
    # none is promoted to local, but each still becomes an ordinary
    # remote candidate rather than vanishing outright -- an old radio's
    # node may legitimately remain visible as a normal remote node
    # (item 4), it just never doubles as YOU.
    if len(local_candidates) == 1:
        local = local_candidates[0]
        # A self-heard echo of YOU's own transmission (a real mesh
        # phenomenon: another node rebroadcasts a packet and it reaches
        # this radio again, or the SDK reports a locally-originated
        # packet as "received") can otherwise leave YOU's own ID ALSO
        # sitting in known_by_id/normalized_interactions below, which
        # would admit a SECOND, is_local=False MeshNodeState carrying
        # the identical node_id -- a duplicate key that a plain dict
        # keyed by node_id (see app.py's MeshTopologyView.set_nodes:
        # states_by_id) resolves by LAST-WRITE-WINS, silently handing
        # the topology label widget the wrong (remote-shaped) state
        # while callers using next()/first-match (e.g. the bottom-left
        # context) keep seeing the correct one. YOU's own ID must never
        # be admitted as a remote candidate at all, closing this at the
        # source rather than relying on every consumer to pick the
        # right duplicate.
        known_by_id.pop(local.node_id, None)
    else:
        for candidate in local_candidates:
            known_by_id.setdefault(candidate.node_id, replace(candidate, is_local=False))

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

    # Admission is the union of both sources: every node NodeDB knows
    # about, plus every node that has originated a CHAT message we
    # received even if NodeDB has no record for it (yet). Sorting and
    # bounding below keeps the OUTPUT small regardless of how large this
    # candidate pool is. YOU's own ID is excluded even if it reached
    # normalized_interactions alone (see the self-heard-echo comment
    # above) -- a bare, name-less NodeMetadata(node_id) fallback for
    # YOU's own ID would be exactly as duplicate-key-dangerous as one
    # sourced from known_by_id.
    candidate_ids = set(known_by_id) | set(normalized_interactions)
    if local is not None:
        candidate_ids.discard(local.node_id)

    local_position = local.position if local is not None else None
    candidates: list[MeshNodeState] = []
    for node_id in candidate_ids:
        node = known_by_id.get(node_id) or NodeMetadata(node_id)
        chat_interaction_at = normalized_interactions.get(node_id)
        candidates.append(
            MeshNodeState(
                node=node,
                is_client=chat_interaction_at is not None,
                is_relay=False,
                last_interaction_at=chat_interaction_at,
                # Straight-line geographic distance from YOU, derived only
                # from stored GPS coordinates -- never from grid position,
                # hop count, or relay-chain length (see RelayStage). "? mi"
                # (distance_miles=None) whenever either position is
                # unknown, never a fabricated figure.
                distance_miles=distance_between(local_position, node.position),
            )
        )

    # VERY_OLD nodes never occupy a working-set slot at all (see
    # MeshActivityTier.VERY_OLD) -- filtered here, before bounding, so a
    # VERY_OLD node can never crowd out a genuinely ACTIVE/STALE one
    # under max_remote_nodes. This is the ONLY place a node's connector
    # is kept off the current board for being too old; nothing here
    # touches NodeDB or CHAT history, and a node recomputed as active
    # again on a later refresh is admitted normally with no special
    # "restore" step needed.
    candidates = [
        state
        for state in candidates
        if state.activity_tier(now=now) is not MeshActivityTier.VERY_OLD
    ]

    def rank_key(state: MeshNodeState) -> tuple[int, int, float, str]:
        active = is_node_active(state.node.last_heard, now)
        favorite = state.node.node_id in favorites
        recency_candidates = [
            timestamp
            for timestamp in (state.last_interaction_at, state.node.last_heard)
            if timestamp is not None
        ]
        recency = max(recency_candidates) if recency_candidates else float("-inf")
        return (
            0 if active else 1,
            0 if favorite else 1,
            -recency,
            state.node.node_id.casefold(),
        )

    candidates.sort(key=rank_key)
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


# UI POLISH Part C: MESH's passive LINK quality display, for the
# currently selected REMOTE node only (see app.py's
# _update_mesh_status_line -- YOU and "nothing selected" never call
# any of this, since a radio has no RF link to itself).
#
# SNR, not RSSI, drives the meter: PROTOBUF-SOURCE-VERIFIED
# (mesh_pb2.pyi), LoRa's spreading-gain means a link can stay fully
# viable at a strongly negative SNR (unlike Wi-Fi, where RSSI alone is
# a reasonable proxy) -- RSSI alone cannot tell a strong link on a
# quiet channel from a weak one on a quiet channel. These thresholds
# are this app's own reasoned mapping from realistic LoRa SNR ranges
# (roughly +10 dB "clean, strong" down to roughly -20 dB "still
# decodable, near the radio's own sensitivity floor" for the higher
# spreading factors Meshtastic's default configs commonly use), not a
# copied third-party constant:
MESH_LINK_SNR_EXCELLENT_DB = 5.0
MESH_LINK_SNR_GOOD_DB = 0.0
MESH_LINK_SNR_WEAK_DB = -10.0

# Placeholder meter for "no honest reading available" (no observation,
# a stale one, or one with SNR missing) -- also the exact meter shown
# in the "nothing to report" fallback line, matching UI POLISH Part C's
# own example (`LINK  ----  -- / -- • LAST UPDATE 25s`).
MESH_LINK_METER_UNKNOWN = "----"


@dataclass(frozen=True)
class MeshLinkDisplay:
    """Display-ready LINK meter/raw-value text for the selected node.

    Always resolvable to *something* displayable -- meter/rssi_text/
    snr_text are each independently "--"-style placeholders when their
    underlying value is missing, never a fabricated number. Raw values
    are carried as already-formatted text (not a bare number) so a
    caller never needs its own second "-- vs formatted" branch.
    """

    meter: str
    rssi_text: str
    snr_text: str


def _mesh_link_meter(snr: float | None) -> str:
    """Deterministic SNR -> bar mapping: same SNR always yields the same

    bar, with no animation, randomization, or smoothing. The 4 glyphs
    (U+2582/2584/2586/2588, "lower N eighths/full block") are ordinary
    single-cell BMP symbols -- ASCII-adjacent block-drawing characters,
    never a combining mark, variation selector, or multi-codepoint
    emoji sequence, so they carry none of the terminal-cell-width risk
    UI POLISH Part A investigates for keycap-style emoji.
    """
    if snr is None:
        return MESH_LINK_METER_UNKNOWN
    if snr >= MESH_LINK_SNR_EXCELLENT_DB:
        return "▂▄▆█"
    if snr >= MESH_LINK_SNR_GOOD_DB:
        return "▂▄▆"
    if snr >= MESH_LINK_SNR_WEAK_DB:
        return "▂▄"
    return "▂"


def format_mesh_link_display(
    observation: LinkObservation | None, *, now: float
) -> MeshLinkDisplay:
    """Build the LINK meter/raw-value text for one directly-heard reading.

    Returns the honest "no data" placeholder (MeshLinkDisplay(
    MESH_LINK_METER_UNKNOWN, "--", "--")) whenever `observation` is
    None (never directly heard from this node -- see RadioService.
    get_link_quality/LinkObservation for what "directly heard"
    requires) OR its own age has reached ACTIVE_WINDOW_SECONDS -- the
    SAME threshold MESH already uses to decide whether a node counts as
    ACTIVE at all (see is_node_active), reused here rather than
    inventing a second, hidden freshness model, so a LINK reading is
    never shown as current once this board would already call the
    underlying evidence stale. A negative age (a clock skew/ordering
    edge case) is treated exactly like an over-age reading: honest
    placeholder, never a fabricated "very fresh" claim.
    """
    if observation is not None:
        age = now - observation.observed_at
        if 0 <= age < ACTIVE_WINDOW_SECONDS:
            return MeshLinkDisplay(
                meter=_mesh_link_meter(observation.snr),
                rssi_text=(
                    str(observation.rssi) if observation.rssi is not None else "--"
                ),
                snr_text=(
                    f"{round(observation.snr):+d}"
                    if observation.snr is not None
                    else "--"
                ),
            )
    return MeshLinkDisplay(MESH_LINK_METER_UNKNOWN, "--", "--")


@dataclass(frozen=True)
class MeshNodeBarFields:
    """Display-ready field values for MESH's single unified bottom bar

    (MESH GPS + UNIFIED BAR Part B), replacing the previous separate
    bottom-left node-context line and bottom-right LINK/LAST UPDATE
    line. Every field independently resolves to something displayable
    -- "?"/"--"/MESH_LINK_METER_UNKNOWN, never blank or fabricated --
    so format_mesh_node_bar_line never needs a second per-field
    missing-data branch.
    """

    long_name: str
    short_name: str
    hops_text: str
    gps_text: str
    gps_text_compact: str
    distance_text: str
    link_meter: str
    link_rssi_text: str
    link_snr_text: str
    time_text: str
    accent2: bool


def format_mesh_node_bar_fields(
    state: MeshNodeState,
    *,
    now: float,
    metric: bool = False,
    link: LinkObservation | None = None,
) -> MeshNodeBarFields:
    """Resolve one selected node's raw field values for the unified bar.

    YOU (state.node.is_local): HOPS is literally "0" (YOU is zero hops
    from itself by definition, regardless of whatever raw hops_away a
    NodeDB record happens to carry for the local node), DISTANCE is
    always "--" (no distance from yourself), TIME is literally "NOW",
    and `link` is expected to already be None from the caller (a radio
    has no RF link to itself -- see app.py's _update_mesh_node_bar,
    which never even calls get_link_quality for YOU); format_mesh_link_
    display(None, ...) already resolves to the same honest placeholder
    a real "never heard directly" remote gets, so no extra branch is
    needed here either way.

    A remote's TIME is the more recent of CHAT interaction time and
    NodeDB last_heard -- the SAME "freshest known signal" combination
    build_mesh_working_set's own ranking already uses -- never CHAT
    interaction time alone (which would wrongly show "?" for a NodeDB-
    only node last_heard already knows is recent), and explicitly NOT
    LINK's own observed_at (a directly-heard packet's timestamp is a
    different fact than "when did we last hear anything at all from
    this node").
    """
    node = state.node
    long_name = _clean_text(node.long_name) or _clean_text(node.short_name) or node.node_id
    short_name = _clean_text(node.short_name) or node.node_id
    gps_text = format_coordinates(node.position) if node.position is not None else "--"
    gps_text_compact = (
        f"{node.position.latitude:.2f},{node.position.longitude:.2f}"
        if node.position is not None
        else "--"
    )
    link_display = format_mesh_link_display(link, now=now)

    if node.is_local:
        hops_text = "0"
        distance_text = "--"
        time_text = "NOW"
    else:
        hops_text = f"{node.hops_away}" if node.hops_away is not None else "?"
        distance_text = (
            format_distance(state.distance_miles, metric=metric)
            if state.distance_miles is not None
            else "--"
        )
        last_seen_candidates = [
            timestamp
            for timestamp in (state.last_interaction_at, node.last_heard)
            if timestamp is not None
        ]
        last_seen_at = max(last_seen_candidates) if last_seen_candidates else None
        if last_seen_at is None or last_seen_at > now:
            time_text = "?"
        else:
            time_text = format_relative_age(now - last_seen_at)

    return MeshNodeBarFields(
        long_name=long_name,
        short_name=short_name,
        hops_text=hops_text,
        gps_text=gps_text,
        gps_text_compact=gps_text_compact,
        distance_text=distance_text,
        link_meter=link_display.meter,
        link_rssi_text=link_display.rssi_text,
        link_snr_text=link_display.snr_text,
        time_text=time_text,
        accent2=node.is_local,
    )


def format_mesh_node_bar_line(fields: MeshNodeBarFields, *, available_width: int) -> str:
    """Assemble MeshNodeBarFields into ONE physical line, degrading

    deterministically through six explicit priority tiers -- compacting
    precision first, then dropping lower-priority fields -- rather than
    ever wrapping onto a second line:

    1. FULL: LONG NAME - SHORT NAME - HOPS - GPS (4dp) - DISTANCE - LINK
       (spaced raw values) - TIME
    2. COMPACT VALUES: same fields, GPS at 2dp and LINK's raw values
       tightened (no surrounding spaces)
    3. drop GPS
    4. drop GPS and DISTANCE
    5. drop SHORT NAME too (LONG NAME - HOPS - LINK - TIME)
    6. drop LINK too (LONG NAME - HOPS - TIME) -- the bare minimum that
       still identifies the node and its two most essential facts

    `available_width <= 0` (not yet laid out) shows the fullest tier
    untouched, mirroring this MESH view's other width-aware lines
    (see mesh_state.py's own established convention). If even tier 6
    does not fit, grapheme-safe truncation is the final fallback.
    """
    link_full = f"{fields.link_meter} {fields.link_rssi_text} / {fields.link_snr_text}"
    link_compact = f"{fields.link_meter} {fields.link_rssi_text}/{fields.link_snr_text}"
    candidates = (
        " • ".join((
            f"LONG NAME {fields.long_name}",
            f"SHORT NAME {fields.short_name}",
            f"HOPS {fields.hops_text}",
            f"GPS {fields.gps_text}",
            f"DISTANCE {fields.distance_text}",
            f"LINK {link_full}",
            f"TIME {fields.time_text}",
        )),
        " • ".join((
            f"LONG NAME {fields.long_name}",
            f"SHORT NAME {fields.short_name}",
            f"HOPS {fields.hops_text}",
            f"GPS {fields.gps_text_compact}",
            f"DISTANCE {fields.distance_text}",
            f"LINK {link_compact}",
            f"TIME {fields.time_text}",
        )),
        " • ".join((
            f"LONG NAME {fields.long_name}",
            f"SHORT NAME {fields.short_name}",
            f"HOPS {fields.hops_text}",
            f"DISTANCE {fields.distance_text}",
            f"LINK {link_compact}",
            f"TIME {fields.time_text}",
        )),
        " • ".join((
            f"LONG NAME {fields.long_name}",
            f"SHORT NAME {fields.short_name}",
            f"HOPS {fields.hops_text}",
            f"LINK {link_compact}",
            f"TIME {fields.time_text}",
        )),
        " • ".join((
            f"LONG NAME {fields.long_name}",
            f"HOPS {fields.hops_text}",
            f"LINK {link_compact}",
            f"TIME {fields.time_text}",
        )),
        " • ".join((
            f"LONG NAME {fields.long_name}",
            f"HOPS {fields.hops_text}",
            f"TIME {fields.time_text}",
        )),
    )
    if available_width <= 0:
        return candidates[0]
    for candidate in candidates:
        if cell_len(candidate) <= available_width:
            return candidate
    return truncate_to_cells(candidates[-1], available_width)
