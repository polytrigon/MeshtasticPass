"""Meshtastic radio access for the StreetPass app."""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
import os
from pathlib import Path
from threading import Event
import time
from typing import Any, Callable, Iterator

from geo import GeoPosition, make_geo_position
from node_activity import count_active_other_nodes
from serial_devices import discover_serial_devices


RX_DEBUG_ENV_VAR = "MESHTASTICPASS_RX_DEBUG"


def rx_debug_enabled() -> bool:
    """Whether the read-only production receive-pipeline diagnostic is on.

    Enable with e.g. `MESHTASTICPASS_RX_DEBUG=1 python app.py`. Checked
    fresh each time (not cached at import) so tests can toggle it via
    os.environ without needing a process restart. Purely observational:
    gates only extra print() calls and one extra pubsub subscription to
    a topic this app never publishes to -- it never changes a routing/
    filtering decision, never polls the radio, and never adds RF
    traffic. Disabled (the default) costs nothing beyond this one
    environment lookup per check.
    """
    value = os.environ.get(RX_DEBUG_ENV_VAR, "").strip().lower()
    return value not in ("", "0", "false")


def rx_debug_log(line: str) -> None:
    """Print one concise receive-pipeline diagnostic line.

    Deliberately plain print() (not logging.*): this is a lightweight,
    opt-in terminal trace meant to run for hours in a normal foreground
    session on the uConsole, not a structured log file.
    """
    print(f"[RX] {line}", flush=True)


def _canonical_node_number(number: Any) -> int | None:
    """Canonical unsigned-32-bit form of a Meshtastic node number.

    The wire format defines node numbers as uint32, but different SDK/
    Python code paths can expose the identical bit pattern as either an
    unsigned value or a negative Python int (any node number with bit
    31 set can appear signed from one source and unsigned from
    another). Masking to 32 bits here makes every downstream comparison
    exact regardless of which representation a given caller happened to
    read -- this is the canonical local-node-identity comparison used
    throughout RadioService's is_local determination (see
    _is_local_node); it never touches string node-ID normalization,
    which mesh_state.normalize_mesh_node_id already owns.
    """
    if number is None or isinstance(number, bool) or not isinstance(number, int):
        return None
    return number & 0xFFFFFFFF


def _node_number_from_id(node_id: str | None) -> int | None:
    """Parse a canonical "!xxxxxxxx" node-ID string into its node number.

    Returns None for a broadcast/channel send (node_id is None) or an
    unparseable string -- used only to let a DM send correlate an
    incoming routing response against its OWN destination (see
    RadioService.send_text/_parse_send_response), never to construct
    an outgoing packet (the SDK's own sendText already accepts the
    "!xxxxxxxx" string form of destinationId directly).
    """
    if not isinstance(node_id, str):
        return None
    stripped = node_id.strip()
    if not stripped:
        return None
    try:
        return int(stripped.removeprefix("!"), 16)
    except ValueError:
        return None


def traversed_hops(hop_start: int | None, hop_limit: int | None) -> int | None:
    """Packet-level hops traveled so far: hop_start - hop_limit.

    PROTOBUF-SOURCE-VERIFIED (mesh_pb2.pyi): MeshPacket.hop_start is
    "Hop limit with which the original packet started ... the
    difference between hop_start and hop_limit gives how many hops it
    traveled." Both fields are a 3-bit wire value (0-7) -- there is no
    3-hop cap, and this deliberately never clamps its result.

    This is NOT wired into the live "N HOPS" MESH display: that display
    already shows the SDK/firmware-computed NodeInfo.hops_away field
    directly (see get_node_metadata/get_known_nodes), which the SDK
    never derives from hop_start/hop_limit anywhere -- the two are
    independent pieces of hop information. This function exists for
    any PACKET-level (not NodeDB-level) caller that genuinely has both
    fields from one received MeshPacket.

    Returns None -- an honest "indeterminate", never a fabricated 0 --
    whenever either input is missing or the packet's hop_limit exceeds
    its own hop_start (impossible for a single packet that only ever
    traveled forward, so not a trustworthy observation).
    """
    if hop_start is None or hop_limit is None:
        return None
    if hop_limit > hop_start:
        return None
    return hop_start - hop_limit


class RadioState(Enum):
    """Connection states that callers can display without knowing SDK details."""

    CONNECTING = "CONNECTING"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"


class RadioConnectionError(Exception):
    """Raised when the Meshtastic radio cannot be used."""

    def __init__(
        self,
        message: str,
        state: RadioState = RadioState.ERROR,
    ) -> None:
        super().__init__(message)
        self.state = state


class RadioSendError(Exception):
    """Raised when an application text message cannot be accepted for sending."""


class RadioIdentityError(Exception):
    """Raised when the connected radio identity cannot be updated."""


LONG_NAME_MAX_UTF8_BYTES = 39
SHORT_NAME_MAX_UTF8_BYTES = 4


def validate_long_name(long_name: str) -> str:
    """Apply the Meshtastic User.long_name nanopb string constraints."""
    if not isinstance(long_name, str):
        raise RadioIdentityError("Long Name must be text.")
    normalized = long_name.strip()
    if not normalized:
        raise RadioIdentityError("Long Name cannot be empty.")
    if len(normalized.encode("utf-8")) > LONG_NAME_MAX_UTF8_BYTES:
        raise RadioIdentityError(
            f"Long Name must be at most {LONG_NAME_MAX_UTF8_BYTES} UTF-8 bytes."
        )
    return normalized


def validate_short_name(short_name: str) -> str:
    """Apply the Meshtastic User.short_name nanopb string constraints.

    Meshtastic 2.7.11's protobuf declares ``max_size: 5`` for this field:
    four UTF-8 payload bytes plus nanopb's terminating null byte.
    """
    if not isinstance(short_name, str):
        raise RadioIdentityError("Short Name must be text.")
    normalized = short_name.strip()
    if not normalized:
        raise RadioIdentityError("Short Name cannot be empty.")
    if len(normalized.encode("utf-8")) > SHORT_NAME_MAX_UTF8_BYTES:
        raise RadioIdentityError(
            f"Short Name must be at most {SHORT_NAME_MAX_UTF8_BYTES} UTF-8 bytes."
        )
    return normalized


class DeliveryState(Enum):
    """Truthful application-level knowledge about one outgoing message."""

    SENDING = "SENDING"
    SENT = "SENT"
    HEARD = "HEARD"
    UNCONFIRMED = "UNCONFIRMED"
    FAILED = "FAILED"
    # A message reloaded from persistence still showing SENDING belonged
    # to a process that no longer exists -- that process's radio worker,
    # ACK callback, and confirmation timer are all gone with it, so
    # SENDING can never resolve on its own again. INTERRUPTED says
    # exactly and only what is truthfully known: submission was in
    # progress when the app stopped tracking it, and what happened next
    # (sent, heard, lost) is unknown -- never SENT (that would claim
    # knowledge the app doesn't have) and never silently left as
    # SENDING forever (see app_controller.stored_chat_entry).
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True)
class SendStatus:
    """An asynchronous ACK/NAK result with no SDK packet structures."""

    state: DeliveryState
    packet_id: int | None
    detail: str = ""


class TracerouteState(Enum):
    """Truthful, terminal outcome of one explicit TRACEROUTE_APP request.

    Only ever reported by RadioService.send_traceroute's own status_handler
    -- never inferred, never a "request sent" placeholder (see
    TracerouteStatus).
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class TracerouteResult:
    """Real protocol evidence from one successful traceroute response.

    PROTOBUF-SOURCE-VERIFIED (mesh_pb2.RouteDiscovery): `route`/
    `route_back` are the REAL intermediate node numbers a MeshPacket
    accumulates as it physically travels forward (towards the
    destination) and back (the response's own return trip) -- never
    including the two endpoints (the local radio, the destination)
    themselves, and never a fabricated/inferred hop; an empty tuple
    means a genuinely direct connection with zero relays, not "unknown".
    forward_snr/return_snr are PARALLEL, dB, one entry per hop of the
    corresponding route PLUS one extra final entry for the last hop
    into the endpoint (so len == len(route) + 1) -- None wherever the
    firmware's own UNK_SNR (-128, prescaled by 4) sentinel marks that
    one hop's reading as unavailable, never a fabricated number.
    """

    destination_node_id: str
    forward_route: tuple[str, ...]
    forward_snr: tuple[float | None, ...]
    return_route: tuple[str, ...]
    return_snr: tuple[float | None, ...]
    completed_at: float


@dataclass(frozen=True)
class TracerouteStatus:
    """An asynchronous traceroute outcome, reported once per request.

    `packet_id` is the SAME id send_traceroute() returned -- a caller
    correlates a status_handler invocation against a specific attempt
    by comparing this field, exactly like SendStatus.packet_id already
    lets send_text callers do for ordinary sends.
    """

    state: TracerouteState
    packet_id: int | None
    result: TracerouteResult | None = None
    detail: str = ""


@dataclass(frozen=True)
class ChannelInfo:
    """One enabled application-facing Meshtastic channel.

    `stable_key` (FINAL MESHTASTIC POLISH pass -- CHAT channel-history
    isolation) is this channel's own cryptographic/assigned identity --
    NEVER derived from `index` (a mutable radio-assigned SLOT NUMBER
    the user can freely reassign to a completely different channel,
    e.g. reconfiguring slot 0 from "LongFast" to "MediumSlow") and
    never from `name` alone either (a bare rename must not fabricate a
    new identity out of thin air with no cryptographic backing -- see
    _read_channel_info). Empty string means "not yet known" (e.g. this
    app's own pre-connection placeholder channel list) -- CHAT history
    isolation treats that as "unknown," never as a real identity to
    compare against.
    """

    index: int
    name: str
    stable_key: str = ""


@dataclass(frozen=True)
class LinkObservation:
    """RF signal quality for one node, from a packet directly heard from it.

    rssi and snr are always captured together, from the SAME MeshPacket,
    so they can never describe two different moments in time. Only ever
    recorded when traversed_hops(hop_start, hop_limit) == 0 for that
    packet (see RadioService._on_any_packet_for_link_quality) -- i.e.
    a packet this node itself transmitted and this radio received with
    zero intermediate relays. A packet forwarded through one or more
    relay nodes is deliberately never recorded here: rx_rssi/rx_snr on
    a relayed packet describe the RF link from the LAST RELAY to this
    radio, not from the packet's logical origin, and presenting that as
    the origin node's own link quality would be dishonest (see PROTOBUF-
    SOURCE-VERIFIED MeshPacket.rx_snr/rx_rssi docs: both are set once,
    on reception, by the immediate receiver, never carried end-to-end).
    """

    rssi: int | None
    snr: float | None
    observed_at: float


@dataclass(frozen=True)
class NodeMetadata:
    """Trustworthy application-level node details from the synced database."""

    node_id: str
    long_name: str | None = None
    short_name: str | None = None
    hops_away: int | None = None
    last_heard: float | None = None
    is_local: bool = False
    position: GeoPosition | None = None
    # User.is_unmessagable (PROTOBUF-SOURCE-VERIFIED field name/spelling
    # -- note the SDK protobuf itself drops the second 'e': "is_unmessagable",
    # doc: "Whether or not the node can be messaged") -- False (the
    # SAFE, non-suppressing default) whenever the field is absent, so
    # incomplete NodeDB metadata never hides a legitimate DM target.
    is_unmessagable: bool = False


@dataclass(frozen=True)
class RadioInfo:
    """Small, UI-friendly summary of the connected radio."""

    device_path: str
    node_id: str
    long_name: str
    short_name: str
    firmware_version: str
    known_nodes: int
    channels: tuple[ChannelInfo, ...] = ()


@dataclass(frozen=True)
class RadioEvent:
    """A connection state change emitted by RadioService."""

    state: RadioState
    info: RadioInfo | None = None
    message: str = ""


# Named localConfig.display.units enum values, so callers (the UI) never
# need to import the Meshtastic protobuf package directly to build a
# units selector -- see write_verified_config_field's module docstring
# note about RadioService being the sole SDK-facing layer.
DISPLAY_UNITS_METRIC = 0
DISPLAY_UNITS_IMPERIAL = 1

# localConfig.display.screen_on_secs is a firmware uint32; this exact
# value (2**32 - 1) is documented by the installed meshtastic package's
# own DisplayConfig.screen_on_secs field comment as "MAXUINT for always
# on" -- confirmed via the installed .pyi stub, never invented.
SCREEN_ON_SECS_ALWAYS_ON = 4294967295

# Meshtastic primary-channel PSK sentinels (see meshtastic.util.fromPSK):
# a single byte 0x01 means "use the well-known default public key", a
# single byte 0x00 means "no encryption". The 16-byte well-known default
# key itself (meshtastic.util.DEFAULT_KEY) -- some firmware/SDK paths
# echo the expanded form on readback, others keep the 0x01 sentinel, so
# PSK verification treats the two as equivalent (see psk_matches_request).
PSK_DEFAULT_SENTINEL = bytes([1])
PSK_NONE_SENTINEL = bytes([0])
DEFAULT_CHANNEL_PSK = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")


def psk_matches_request(requested: bytes, actual: bytes) -> bool:
    """Semantic PSK comparison for readback verification.

    Exact bytes match, OR both sides resolve to the same well-known
    channel key: a requested 0x01 sentinel is satisfied by an actual
    0x01 OR the expanded 16-byte DEFAULT_CHANNEL_PSK (and vice versa);
    a requested "" (leave unset) is satisfied by "" or the 0x00
    ("no encryption") sentinel. Never inspects/logs the raw key bytes
    beyond this equality decision.
    """
    if requested == actual:
        return True
    default_family = {PSK_DEFAULT_SENTINEL, DEFAULT_CHANNEL_PSK}
    if requested in default_family and actual in default_family:
        return True
    unset_family = {b"", PSK_NONE_SENTINEL}
    return requested in unset_family and actual in unset_family


@dataclass(frozen=True)
class ConfigWriteResult:
    """Outcome of one RadioService.write_verified_config_field() call.

    `applied` is True ONLY when a fresh radio-originated
    getConfigResponse for the written section named that exact field
    and its value matched what was requested -- never merely because
    the local Python config object was mutated (the SDK does that
    before any hardware confirmation exists) and never merely because
    a routing ACK/NAK arrived (that proves packet delivery, not that
    the admin operation was applied). See radio_write_readback_probe.py
    for the real-hardware investigation that established this
    verification model.

    `reason` is "" when applied is True; otherwise one of:
    "not_connected", "disconnected" (connection/interface changed
    mid-verification), "nak", "timeout", or "mismatch".
    """

    applied: bool
    requested_value: Any
    readback_value: Any | None
    reason: str = ""


@dataclass(frozen=True)
class RadioApplyResult:
    """Outcome of apply_radio_config_preset() -- one controlled,

    sequential, multi-field operation (LoRaConfig.use_preset/
    modem_preset/channel_num, then the PRIMARY channel's name/psk),
    each individually write-verified via the SAME ConfigWriteResult
    model ordinary RADIO-section fields already use.

    Stops at the FIRST failing step: `failed_step` names it
    ("use_preset"/"modem_preset"/"frequency_slot"/"channel") and
    `results` holds every ConfigWriteResult attempted so far (never a
    step that was never reached). There is no transactional multi-
    write primitive on real Meshtastic hardware to roll a partial
    apply back with, so a partial apply is reported honestly rather
    than silently retried, reordered, or hidden.
    """

    applied: bool
    failed_step: str
    results: dict[str, ConfigWriteResult]


@dataclass(frozen=True)
class ClockSyncResult:
    """Outcome of one RadioService.sync_clock() call.

    Unlike write_verified_config_field, AdminMessage exposes no
    get-time-equivalent RPC (confirmed against the installed
    meshtastic==2.7.11 protobuf schema: no get_time_request/response
    field exists anywhere on AdminMessage) -- there is no way to
    independently ask the radio to report its own current clock value
    back. `applied` is therefore True only when BOTH a clean
    mesh-routing ACK for the set_time_only admin write was received AND
    a best-effort secondary signal -- a subsequently observed packet's
    own rxTime, the RECEIVING device's own locally-stamped Unix
    timestamp (see RadioService._accept_received_message's identical
    use of rxTime for radio_rx_at) -- lands within a few seconds of the
    host epoch requested. When no such packet ever arrives, or its
    rxTime doesn't corroborate the write, this settles for
    "unconfirmed" rather than fabricating "applied" from routing-ack
    evidence alone -- exactly the same standard ConfigWriteResult
    already holds ordinary config writes to.

    `reason` is "" when applied is True; otherwise one of:
    "not_connected", "disconnected", "nak", "timeout" (no ack/nak
    response at all), or "unconfirmed" (a response arrived but did not
    corroborate the write).
    """

    applied: bool
    requested_epoch: int
    observed_rx_time: int | None
    reason: str = ""


@dataclass(frozen=True)
class ReceivedMessage:
    """A decoded text message with no Meshtastic SDK-specific structures.

    ``origin_sent_at`` is reserved for a trustworthy protocol-provided origin
    time. Ordinary Meshtastic text packets do not have one, so RadioService
    leaves it unset. ``radio_rx_at`` carries the connected receiver's ``rxTime``.
    """

    sender_node_id: str
    sender_long_name: str | None
    sender_short_name: str | None
    channel_index: int | None
    text: str
    rssi: int | None
    snr: float | None
    packet_id: int | None
    origin_sent_at: float | None = None
    radio_rx_at: float | None = None
    local_position: GeoPosition | None = None
    sender_position: GeoPosition | None = None
    # True only when the packet's own destination (MeshPacket.to) names
    # THIS radio's node number specifically -- never inferred from
    # channel index, sender name, or any other heuristic (see
    # RadioService._parse_text_packet/_is_direct_message). False for an
    # ordinary broadcast/channel message, where `to` is BROADCAST_NUM.
    is_direct: bool = False


@dataclass(frozen=True)
class SentMessage:
    """An application-level record accepted by the local send API."""

    text: str
    channel_index: int
    destination_node_id: str | None
    packet_id: int | None = None
    immediate_state: DeliveryState | None = None


def validate_send_request(
    text: str,
    channel_index: int,
    destination_node_id: str | None,
) -> SentMessage:
    """Validate and normalize values shared by real and simulated radios."""
    if not isinstance(text, str) or not text.strip():
        raise RadioSendError("Message text cannot be empty or whitespace only.")
    if (
        isinstance(channel_index, bool)
        or not isinstance(channel_index, int)
        or not 0 <= channel_index <= 7
    ):
        raise RadioSendError("Channel index must be an integer from 0 through 7.")
    if destination_node_id is not None:
        if (
            not isinstance(destination_node_id, str)
            or not destination_node_id.strip()
        ):
            raise RadioSendError("Destination node ID cannot be empty.")
        destination_node_id = destination_node_id.strip()

    return SentMessage(text, channel_index, destination_node_id)


class RadioService:
    """Owns the connection between the app and a Meshtastic radio."""

    _MAX_PENDING_SENDS = 200
    # How close a subsequent packet's own rxTime must land to the host
    # epoch we asked the radio to adopt (see sync_clock) to count as
    # corroborating evidence -- generous enough to absorb normal
    # send/serial/processing latency, never so tight that ordinary
    # round-trip time alone produces a false "unconfirmed".
    _CLOCK_SYNC_TOLERANCE_SECONDS = 10

    def __init__(self, device_path: str = "/dev/ttyUSB0") -> None:
        self.device_path = device_path
        self._interface: Any | None = None
        self._connection_lost = Event()
        self._pub: Any | None = None
        self._message_handlers: list[Callable[[ReceivedMessage], None]] = []
        self._direct_observations: dict[str, float] = {}
        # Separate from _direct_observations above (which only tracks
        # "was ANY accepted packet heard from this node recently", for
        # last_heard/activity-tier freshening -- it never checks hop
        # count). Keyed by lowercased node_id, one LinkObservation per
        # node, always the most recently directly-heard packet's own
        # rssi/snr/timestamp. Cleared on radio identity change/close
        # exactly like _direct_observations, so a previous radio's (or
        # previous connection's) readings can never be shown for a new
        # one (see connect()/set_device_path()/close()).
        self._link_observations: dict[str, LinkObservation] = {}
        self._activity_local_node_id: str | None = None
        # Tracks in-flight outgoing sends by packet ID, keyed to the
        # status_handler given to send_text() plus the EXPECTED
        # destination node number (None for a broadcast/channel send).
        # See _on_routing_response: unlike the SDK's own one-shot
        # onResponse callback (which discards its handler after the
        # FIRST matching packet), this keeps watching so a genuinely
        # stronger, later-arriving confirmation (e.g. a DM destination's
        # own ack arriving after an earlier local/implicit one) can
        # still be observed instead of silently dropped. Bounded by
        # _MAX_PENDING_SENDS. The destination is carried alongside the
        # handler so _parse_send_response can require a DM's explicit
        # ack to come specifically FROM that destination -- a clean
        # routing response from some OTHER, unrelated node must never
        # complete a DM as HEARD (see send_text/_parse_send_response).
        self._pending_sends: dict[
            int, tuple[Callable[[SendStatus], None], int | None]
        ] = {}
        # See config_snapshot()/refresh_config_snapshot(): a snapshot is
        # only ever attached to the connection GENERATION that built
        # it, so a stale snapshot from a previous (or failed) connect()
        # can never be presented as current -- config_snapshot() itself
        # is the only reader and always reflects the CURRENT connection.
        self._connection_generation = 0
        self._config_snapshot: RadioConfigurationSnapshot | None = None

    def connect(self) -> RadioInfo:
        """Connect, wait for the SDK's initial sync, and return local node info."""
        self._connection_lost.clear()
        self._check_device()

        try:
            self._interface = self._open_interface()
            info = self._read_radio_info()
            if (
                self._activity_local_node_id is not None
                and self._activity_local_node_id.lower() != info.node_id.lower()
            ):
                self._direct_observations.clear()
                self._link_observations.clear()
            self._activity_local_node_id = info.node_id
            # Item 7: a NEW connection always gets a NEW generation and
            # a freshly-built snapshot -- whether this is a reconnect to
            # the SAME radio or a genuinely different one (a different
            # node_id, hardware, and config set entirely). The OLD
            # snapshot is simply replaced, never merged with or patched
            # from, so stale V3 settings can never leak into a V4's own
            # snapshot (see item 8: capability comes from what the SDK
            # actually reports here, never from device_path).
            self._connection_generation += 1
            self._rebuild_config_snapshot()
            return info
        except RadioConnectionError:
            self.close()
            raise
        except ImportError as error:
            self.close()
            raise RadioConnectionError(
                "The Meshtastic Python package is not installed. "
                "Activate .venv and run: pip install -r requirements.txt"
            ) from error
        except Exception as error:
            self.close()
            message = str(error).strip() or error.__class__.__name__
            raise RadioConnectionError(
                f"Could not connect to the radio on {self.device_path}: {message}"
            ) from error

    def available_device_paths(self) -> tuple[str, ...]:
        """Return serial devices currently reported by pyserial."""
        return discover_serial_devices()

    def set_device_path(self, device_path: str) -> None:
        """Change ports only after closing the current serial interface."""
        if not isinstance(device_path, str) or not device_path.strip():
            raise ValueError("USB device path cannot be empty.")
        self.close()
        self.device_path = device_path.strip()
        self._direct_observations.clear()
        self._link_observations.clear()
        self._activity_local_node_id = None

    def connection_events(
        self,
        retry_delay: float = 5.0,
        stop_event: Event | None = None,
        poll_interval: float = 0.25,
    ) -> Iterator[RadioEvent]:
        """Keep the radio connected and emit state changes until stopped."""
        if retry_delay < 0:
            raise ValueError("retry_delay cannot be negative")

        stopped = stop_event or Event()

        try:
            while not stopped.is_set():
                yield RadioEvent(RadioState.CONNECTING)

                try:
                    info = self.connect()
                except RadioConnectionError as error:
                    yield RadioEvent(error.state, message=str(error))
                else:
                    yield RadioEvent(RadioState.ONLINE, info=info)

                    while not stopped.is_set():
                        if self._connection_lost.wait(poll_interval):
                            break
                        if not self._device_exists():
                            break

                    if stopped.is_set():
                        break

                    self.close()
                    yield RadioEvent(
                        RadioState.OFFLINE,
                        message=f"Connection to {self.device_path} was lost.",
                    )

                if stopped.wait(retry_delay):
                    break
        finally:
            self.close()
            self._unsubscribe_from_events()

    def add_message_handler(
        self,
        handler: Callable[[ReceivedMessage], None],
    ) -> None:
        """Call handler for each valid received text message."""
        if handler not in self._message_handlers:
            self._message_handlers.append(handler)

    def active_node_count(self, now: float | None = None) -> int | None:
        """Return recently heard other nodes without transmitting anything."""
        interface = self._interface
        if interface is None:
            return None
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_number, dict):
            try:
                nodes = tuple(nodes_by_number.items())
            except RuntimeError:
                # The SDK may be updating the node database from its receive thread.
                # Direct observations remain trustworthy during that brief race.
                nodes = ()
        else:
            nodes = ()

        my_info = getattr(interface, "myInfo", None)
        local_number = getattr(my_info, "my_node_num", None)
        if isinstance(local_number, bool) or not isinstance(local_number, int):
            local_number = None
        local_record = (
            nodes_by_number.get(local_number, {})
            if isinstance(nodes_by_number, dict) and local_number is not None
            else {}
        )
        local_user = self._user_from_record(local_record)
        local_id = self._optional_string(local_user.get("id"))
        if local_id is None:
            local_id = self._activity_local_node_id
        try:
            observations = dict(self._direct_observations)
        except RuntimeError:
            observations = {}
        return count_active_other_nodes(
            nodes,
            local_node_number=local_number,
            local_node_id=local_id,
            now=time.time() if now is None else now,
            direct_observations=observations,
        )

    def get_node_metadata(self, node_id: str) -> NodeMetadata:
        """Read normalized node metadata without transmitting radio traffic."""
        normalized = node_id.strip() if isinstance(node_id, str) else ""
        if not normalized:
            return NodeMetadata("")
        try:
            node_number = int(normalized.removeprefix("!"), 16)
        except ValueError:
            node_number = None
        record = self._lookup_node_record(
            self._interface,
            node_number,
            normalized,
        )
        user = self._user_from_record(record)
        hops = self._optional_int(record.get("hopsAway"))
        if hops is not None and hops < 0:
            hops = None
        last_heard = self._valid_timestamp(record.get("lastHeard"))
        observed_at = self._direct_observations.get(normalized.lower())
        if self._valid_timestamp(observed_at) is not None:
            last_heard = max(last_heard or 0.0, float(observed_at))
        return NodeMetadata(
            node_id=normalized,
            long_name=self._optional_string(user.get("longName")),
            short_name=self._optional_string(user.get("shortName")),
            hops_away=hops,
            last_heard=last_heard,
            is_local=self._is_local_node(normalized, node_number),
            position=self._position_from_record(record),
            is_unmessagable=bool(user.get("isUnmessagable", False)),
        )

    def get_known_nodes(self) -> tuple[NodeMetadata, ...]:
        """Return the synced node database as normalized, read-only app values."""
        interface = self._interface
        if interface is None:
            return ()
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if not isinstance(nodes_by_number, dict):
            return ()
        try:
            records = tuple(nodes_by_number.items())
            observations = dict(self._direct_observations)
        except RuntimeError:
            return ()

        result: list[NodeMetadata] = []
        seen: set[str] = set()
        for raw_number, record in records:
            if not isinstance(record, dict):
                continue
            number = (
                raw_number
                if isinstance(raw_number, int) and not isinstance(raw_number, bool)
                else None
            )
            user = self._user_from_record(record)
            node_id = self._optional_string(user.get("id"))
            if node_id is None and number is not None:
                node_id = f"!{_canonical_node_number(number):08x}"
            if node_id is None or node_id.lower() in seen:
                continue
            seen.add(node_id.lower())
            hops = self._optional_int(record.get("hopsAway"))
            if hops is not None and hops < 0:
                hops = None
            last_heard = self._valid_timestamp(record.get("lastHeard"))
            observed_at = self._valid_timestamp(observations.get(node_id.lower()))
            if observed_at is not None:
                last_heard = max(last_heard or 0.0, observed_at)
            result.append(
                NodeMetadata(
                    node_id=node_id,
                    long_name=self._optional_string(user.get("longName")),
                    short_name=self._optional_string(user.get("shortName")),
                    hops_away=hops,
                    last_heard=last_heard,
                    is_local=self._is_local_node(node_id, number),
                    position=self._position_from_record(record),
                    is_unmessagable=bool(user.get("isUnmessagable", False)),
                )
            )

        for node_id, raw_observed_at in observations.items():
            if node_id in seen or (
                self._activity_local_node_id
                and node_id == self._activity_local_node_id.lower()
            ):
                continue
            observed_at = self._valid_timestamp(raw_observed_at)
            if observed_at is not None:
                seen.add(node_id)
                result.append(NodeMetadata(node_id, last_heard=observed_at))

        local_id = self._activity_local_node_id
        if local_id and local_id.lower() not in seen:
            result.append(NodeMetadata(local_id, is_local=True))
        return tuple(result)

    def hardware_identity(self):
        """Read-only: what the connected radio authoritatively reports

        about its own hardware/firmware (see radio_capabilities.
        hardware_identity for the full field list and exactly which
        Meshtastic API object each one comes from). Never inferred
        from device_path/serial device name; returns an all-
        "unavailable" report if not yet connected, rather than raising.
        """
        from radio_capabilities import hardware_identity as _hardware_identity

        return _hardware_identity(self._interface)

    def capability_report(self):
        """Read-only capability/configuration audit of the connected

        radio (see radio_capabilities.build_capability_matrix) --
        every already-synced localConfig/moduleConfig/channel section
        this installed meshtastic package's schema declares, each row
        paired with this codebase's own writable/reboot/hardware-
        dependent/safe-to-expose judgment. Never sends a config write,
        never transmits text, never generates LoRa traffic; secrets
        (PSKs, passwords, keys) are reported only as configured/not
        configured. Before a connection exists, still returns the
        hardware-identity rows with "unavailable"/"not configured"
        values rather than raising or returning nothing.
        """
        from radio_capabilities import build_capability_matrix

        return build_capability_matrix(self._interface)

    def config_snapshot(self):
        """The current connection's cached RadioConfigurationSnapshot,

        or None if not yet connected (or the snapshot hasn't been
        built for this generation yet -- see _rebuild_config_snapshot).
        Pure cache read: never touches the interface, never sends
        anything -- see item 4/13 (opening/switching to CONFIG/RADIO,
        and repeated focus changes, must generate zero radio traffic).
        """
        return self._config_snapshot

    def refresh_config_snapshot(self):
        """Explicit REFRESH (item 5): rebuild the snapshot from the

        SDK's CURRENT already-synced local objects. Still zero new RF
        traffic -- localConfig/moduleConfig/channels/metadata are live
        Python objects the SDK keeps updated in place as its own
        background sync (or a write this session made) progresses;
        "refreshing" means re-reading them now rather than requesting
        anything new. Returns the new snapshot (or None if not
        connected). Never called automatically/periodically -- see
        app.py's own caller, which only runs this from an explicit
        user-activated refresh.
        """
        self._rebuild_config_snapshot()
        return self._config_snapshot

    def _rebuild_config_snapshot(self) -> None:
        """Shared by connect() and refresh_config_snapshot() -- see

        item 6: also the only place a write's confirmed result is
        folded back in (write_verified_config_field calls this again
        after a successful, verified write, never before).
        """
        if self._interface is None:
            self._config_snapshot = None
            return
        from radio_config_snapshot import build_radio_configuration_snapshot

        self._config_snapshot = build_radio_configuration_snapshot(
            self._interface,
            device_path=self.device_path,
            connection_generation=self._connection_generation,
            generated_at=time.time(),
        )

    def _is_local_node(self, node_id: str, node_number: int | None) -> bool:
        interface = self._interface
        my_info = getattr(interface, "myInfo", None)
        local_number = _canonical_node_number(getattr(my_info, "my_node_num", None))
        candidate_number = _canonical_node_number(node_number)
        # The live interface's own my_node_num is the SOLE authoritative
        # signal whenever both numbers are resolvable -- canonicalized
        # first (see _canonical_node_number) so two representations of
        # the identical wire node number can never mismatch merely from
        # signed/unsigned interpretation. This is checked BEFORE, and
        # instead of (not alongside) the string fallback below: a
        # numeric mismatch must never be overridden by a stale
        # _activity_local_node_id string that happens to still equal
        # this node's ID from a PREVIOUS connection (see MESH FOLLOW-UP
        # item 3 -- never determine YOU from stale connection-generation
        # state). The string fallback exists only for the genuine
        # no-number case (e.g. get_known_nodes()'s own synthetic
        # local-node fallback, which has no node_number to offer).
        if local_number is not None and candidate_number is not None:
            return candidate_number == local_number
        return bool(
            self._activity_local_node_id
            and node_id.lower() == self._activity_local_node_id.lower()
        )

    @staticmethod
    def _valid_timestamp(value: Any) -> float | None:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isfinite(value)
            or value <= 0
        ):
            return None
        return float(value)

    def set_long_name(self, long_name: str) -> RadioInfo:
        """Update the local Meshtastic owner's advertised Long Name."""
        normalized = validate_long_name(long_name)
        interface = self._interface
        if interface is None:
            raise RadioIdentityError("The radio is not connected.")
        local_node = getattr(interface, "localNode", None)
        if local_node is None:
            raise RadioIdentityError("The connected radio identity is unavailable.")

        try:
            local_node.setOwner(long_name=normalized)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise RadioIdentityError(f"Could not save Long Name: {detail}") from error

        my_info = getattr(interface, "myInfo", None)
        local_number = getattr(my_info, "my_node_num", None)
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_number, dict) and local_number in nodes_by_number:
            record = nodes_by_number.get(local_number)
            if isinstance(record, dict):
                user = record.setdefault("user", {})
                if isinstance(user, dict):
                    user["longName"] = normalized
        return replace(self._read_radio_info(), long_name=normalized)

    def set_short_name(self, short_name: str) -> RadioInfo:
        """Update the local Meshtastic owner's advertised Short Name."""
        normalized = validate_short_name(short_name)
        interface = self._interface
        if interface is None:
            raise RadioIdentityError("The radio is not connected.")
        local_node = getattr(interface, "localNode", None)
        if local_node is None:
            raise RadioIdentityError("The connected radio identity is unavailable.")

        try:
            local_node.setOwner(short_name=normalized)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise RadioIdentityError(f"Could not save Short Name: {detail}") from error

        my_info = getattr(interface, "myInfo", None)
        local_number = getattr(my_info, "my_node_num", None)
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_number, dict) and local_number in nodes_by_number:
            record = nodes_by_number.get(local_number)
            if isinstance(record, dict):
                user = record.setdefault("user", {})
                if isinstance(user, dict):
                    user["shortName"] = normalized
        return replace(self._read_radio_info(), short_name=normalized)

    def read_synced_config_field(self, section: str, field: str) -> Any | None:
        """Read localConfig.<section>.<field> from the SDK's already-

        synced local cache -- no new radio request, no RF traffic. Use
        this for initial population and reconnect refresh (see item 17
        of the RADIO-section task: normal initial sync is authoritative
        enough for display; a fresh explicit request is reserved for
        verifying a write). Returns None if not connected or if this
        installed schema does not declare the field (a future/older
        firmware's schema drifting must render as "unavailable", never
        crash the caller).
        """
        interface = self._interface
        local_node = getattr(interface, "localNode", None)
        local_config = getattr(local_node, "localConfig", None)
        section_message = getattr(local_config, section, None)
        if section_message is None:
            return None
        try:
            return getattr(section_message, field)
        except AttributeError:
            return None

    def write_verified_config_field(
        self,
        section: str,
        field: str,
        value: Any,
        *,
        timeout: float = 15.0,
    ) -> ConfigWriteResult:
        """Write ONE localConfig.<section>.<field> and verify it with a

        fresh radio-originated getConfigResponse -- the exact technique
        radio_write_readback_probe.py established on real hardware (see
        ConfigWriteResult's docstring for what "verified" means and
        does not mean). This bypasses node.writeConfig()/requestConfig()'s
        convenience wrappers on purpose: for the LOCAL node they wire no
        response callback at all, so nothing would ever confirm the
        write happened or parse the read reply.

        Never touches any field but the one named here. Detects a
        connection loss OR an interface swap (a reconnect completing
        mid-verification) during the wait and reports "disconnected"
        rather than fabricating a result from a stale interface's
        response.
        """
        interface = self._interface
        if interface is None:
            return ConfigWriteResult(False, value, None, "not_connected")
        local_node = getattr(interface, "localNode", None)
        local_config = getattr(local_node, "localConfig", None) if local_node else None
        section_message = getattr(local_config, section, None) if local_config is not None else None
        if local_node is None or section_message is None:
            return ConfigWriteResult(False, value, None, "not_connected")

        from meshtastic.protobuf import admin_pb2

        target_interface = interface
        setattr(section_message, field, value)

        write_message = admin_pb2.AdminMessage()
        getattr(write_message.set_config, section).CopyFrom(section_message)

        nak_seen = {"nak": False}

        def on_ack(packet: Any) -> None:
            local_node.onAckNak(packet)
            if interface._acknowledgment.receivedNak:
                nak_seen["nak"] = True

        try:
            local_node._sendAdmin(write_message, onResponse=on_ack)
            interface.waitForAckNak()
        except Exception:
            # A missing/weak ack must not by itself fail OR pass the
            # write -- only the fresh readback below is authoritative.
            pass

        if self._interface is not target_interface or self._connection_lost.is_set():
            return ConfigWriteResult(False, value, None, "disconnected")
        if nak_seen["nak"]:
            return ConfigWriteResult(False, value, None, "nak")

        read_request = admin_pb2.AdminMessage()
        read_request.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(
            f"{section.upper()}_CONFIG"
        )
        found: dict[str, Any] = {}

        def on_response(packet: Any) -> None:
            # Reuse the SDK's own extraction/CopyFrom logic (normally
            # only wired for remote-node requests) so localConfig is
            # updated through the real mechanism too.
            local_node.onResponseRequestSettings(packet)
            decoded = packet.get("decoded") if isinstance(packet, dict) else None
            admin = decoded.get("admin") if isinstance(decoded, dict) else None
            if not isinstance(admin, dict):
                return
            raw = admin.get("raw")
            if raw is None or not raw.HasField("get_config_response"):
                return
            if raw.get_config_response.WhichOneof("payload_variant") == section:
                found["value"] = getattr(getattr(raw.get_config_response, section), field)

        try:
            local_node._sendAdmin(read_request, onResponse=on_response)
        except Exception:
            return ConfigWriteResult(False, value, None, "timeout")

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self._interface is not target_interface or self._connection_lost.is_set():
                return ConfigWriteResult(False, value, None, "disconnected")
            if "value" in found:
                readback = found["value"]
                if readback == value:
                    # Item 6: the cached snapshot is rebuilt ONLY here,
                    # after a genuinely radio-confirmed write -- never
                    # optimistically, before this exact verification
                    # level is reached.
                    self._rebuild_config_snapshot()
                    return ConfigWriteResult(True, value, readback, "")
                return ConfigWriteResult(False, value, readback, "mismatch")
            time.sleep(0.05)

        return ConfigWriteResult(False, value, None, "timeout")

    def read_primary_channel_settings(self) -> tuple[str, bytes] | None:
        """Zero-RF read of the PRIMARY channel's (index 0) name/psk from

        the already-synced interface -- mirrors read_synced_config_field's
        own contract exactly: no new radio request, None when
        unavailable (not connected, or this installed schema/interface
        doesn't expose it).
        """
        interface = self._interface
        local_node = getattr(interface, "localNode", None)
        channels = getattr(local_node, "channels", None) if local_node else None
        if not channels:
            return None
        settings = getattr(channels[0], "settings", None)
        if settings is None:
            return None
        name = getattr(settings, "name", "") or ""
        psk = getattr(settings, "psk", b"") or b""
        return (name, bytes(psk))

    def write_verified_primary_channel(
        self,
        *,
        name: str,
        psk: bytes,
        timeout: float = 15.0,
    ) -> ConfigWriteResult:
        """Write the PRIMARY channel's (index 0) name/psk and verify it

        with a fresh radio-originated get_channel_response -- the same
        verification MODEL write_verified_config_field uses for
        localConfig fields (see its own docstring), adapted for a
        Channel message: Channel settings live in their own
        AdminMessage.set_channel/get_channel_request family, never
        localConfig, so this is a distinct write path rather than a
        call to write_verified_config_field with a different section
        name. Never touches role, channel_num, uplink/downlink_enabled,
        or any OTHER channel's settings -- only this one channel's
        name/psk.
        """
        interface = self._interface
        if interface is None:
            return ConfigWriteResult(False, (name, psk), None, "not_connected")
        local_node = getattr(interface, "localNode", None)
        channels = getattr(local_node, "channels", None) if local_node else None
        if local_node is None or not channels:
            return ConfigWriteResult(False, (name, psk), None, "not_connected")

        from meshtastic.protobuf import admin_pb2

        target_interface = interface
        primary = channels[0]
        primary.settings.name = name
        primary.settings.psk = psk

        write_message = admin_pb2.AdminMessage()
        write_message.set_channel.CopyFrom(primary)

        nak_seen = {"nak": False}

        def on_ack(packet: Any) -> None:
            local_node.onAckNak(packet)
            if interface._acknowledgment.receivedNak:
                nak_seen["nak"] = True

        try:
            local_node._sendAdmin(write_message, onResponse=on_ack)
            interface.waitForAckNak()
        except Exception:
            pass

        if self._interface is not target_interface or self._connection_lost.is_set():
            return ConfigWriteResult(False, (name, psk), None, "disconnected")
        if nak_seen["nak"]:
            return ConfigWriteResult(False, (name, psk), None, "nak")

        read_request = admin_pb2.AdminMessage()
        # AdminMessage.get_channel_request is 1-INDEXED on the wire:
        # meshtastic.node.Node._requestChannel sends `channelNum + 1`,
        # so requesting the PRIMARY channel (index 0) means a value of
        # 1. A value of 0 addresses no valid channel, the firmware never
        # answers, and this readback silently times out on real hardware
        # (the observed "NETWORK apply never completes" bug).
        read_request.get_channel_request = 1
        found: dict[str, Any] = {}

        def on_response(packet: Any) -> None:
            decoded = packet.get("decoded") if isinstance(packet, dict) else None
            admin = decoded.get("admin") if isinstance(decoded, dict) else None
            if not isinstance(admin, dict):
                return
            raw = admin.get("raw")
            if raw is None or not raw.HasField("get_channel_response"):
                return
            response_channel = raw.get_channel_response
            if getattr(response_channel, "index", 0) != 0:
                return  # not the primary channel's response
            found["name"] = response_channel.settings.name
            found["psk"] = bytes(response_channel.settings.psk)

        try:
            local_node._sendAdmin(read_request, onResponse=on_response)
        except Exception:
            return ConfigWriteResult(False, (name, psk), None, "timeout")

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self._interface is not target_interface or self._connection_lost.is_set():
                return ConfigWriteResult(False, (name, psk), None, "disconnected")
            if "name" in found:
                readback = (found["name"], found["psk"])
                if found["name"] == name and psk_matches_request(psk, found["psk"]):
                    self._rebuild_config_snapshot()
                    return ConfigWriteResult(True, (name, psk), readback, "")
                return ConfigWriteResult(False, (name, psk), readback, "mismatch")
            time.sleep(0.05)

        return ConfigWriteResult(False, (name, psk), None, "timeout")

    def begin_settings_transaction(self) -> bool:
        """Open a Meshtastic settings-edit transaction

        (AdminMessage.begin_edit_settings) -- exactly what
        `meshtastic --set` does before a MULTI-field config change (see
        meshtastic.__main__: it wraps >1 writeConfig() calls in
        begin/commitSettingsTransaction). Best-effort: returns False and
        never raises if there is no connection or the installed SDK
        predates the method. apply_radio_config_preset uses this so a
        NETWORK apply is committed the same way the CLI commits one,
        rather than as loose, individually-persisted set_config writes.
        """
        local_node = getattr(self._interface, "localNode", None)
        begin = getattr(local_node, "beginSettingsTransaction", None)
        if begin is None:
            return False
        try:
            begin()
            return True
        except Exception:
            return False

    def commit_settings_transaction(self) -> bool:
        """Commit the open settings-edit transaction

        (AdminMessage.commit_edit_settings). The firmware persists the
        batch of edits -- and reboots if any of them require it -- on
        THIS message, not on the individual set_config writes. Always
        paired with begin_settings_transaction() so a transaction is
        never left dangling on the radio. Best-effort / never raises.
        """
        local_node = getattr(self._interface, "localNode", None)
        commit = getattr(local_node, "commitSettingsTransaction", None)
        if commit is None:
            return False
        try:
            commit()
            return True
        except Exception:
            return False

    def reread_lora_and_primary_channel(self, *, timeout: float = 8.0) -> bool:
        """Ask the radio for a FRESH copy of localConfig.lora and the

        primary channel (get_config_request LORA + get_channel_request
        1) and fold it into the SDK's synced objects -- so a following
        read_synced_config_field("lora", ...) / read_primary_channel_
        settings() reflects the radio's CURRENT state, not a stale
        connect-time snapshot. Used to verify a NETWORK apply that
        completed WITHOUT an observed reboot (there was no reconnect
        full-sync to refresh the cache). Best-effort; returns True if
        both responses arrived. Never raises.
        """
        interface = self._interface
        local_node = getattr(interface, "localNode", None)
        if interface is None or local_node is None:
            return False
        try:
            from meshtastic.protobuf import admin_pb2
        except Exception:
            return False
        got = {"lora": False, "channel": False}

        def on_config(packet: Any) -> None:
            try:
                local_node.onResponseRequestSettings(packet)
            except Exception:
                pass
            got["lora"] = True

        def on_channel(packet: Any) -> None:
            decoded = packet.get("decoded") if isinstance(packet, dict) else None
            admin = decoded.get("admin") if isinstance(decoded, dict) else None
            raw = admin.get("raw") if isinstance(admin, dict) else None
            if raw is not None and raw.HasField("get_channel_response"):
                channel = raw.get_channel_response
                if getattr(channel, "index", 0) == 0 and local_node.channels:
                    local_node.channels[0].CopyFrom(channel)
            got["channel"] = True

        lora_request = admin_pb2.AdminMessage()
        lora_request.get_config_request = admin_pb2.AdminMessage.ConfigType.Value(
            "LORA_CONFIG"
        )
        channel_request = admin_pb2.AdminMessage()
        channel_request.get_channel_request = 1  # 1-indexed: primary == 0
        try:
            local_node._sendAdmin(lora_request, onResponse=on_config)
            local_node._sendAdmin(channel_request, onResponse=on_channel)
        except Exception:
            return False
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if self._connection_lost.is_set():
                return False
            if got["lora"] and got["channel"]:
                self._rebuild_config_snapshot()
                return True
            time.sleep(0.05)
        return False

    def supports_clock_sync(self) -> bool:
        """Whether the installed SDK/protobuf schema exposes

        AdminMessage.set_time_only -- a schema-driven capability check
        (see item 20: never inferred from device path or board-name
        string) that stays accurate even against a different installed
        meshtastic package version than the one this was audited
        against (2.7.11). Independent of whether a radio is actually
        connected right now -- callers combine this with connection
        state separately, exactly like every other capability check in
        this class.
        """
        from meshtastic.protobuf import admin_pb2

        return "set_time_only" in admin_pb2.AdminMessage.DESCRIPTOR.fields_by_name

    def sync_clock(self) -> ClockSyncResult:
        """Set the radio's clock from THIS host's current wall clock,

        captured as close as practical to the actual write (see
        ClockSyncResult's docstring for why "applied" is a materially
        weaker guarantee here than write_verified_config_field's:
        AdminMessage has no get-time RPC to read the value back with).

        Bypasses node.setTime()/Node's own convenience wrapper on
        purpose, for the same reason write_verified_config_field
        bypasses node.writeConfig(): for the LOCAL node those wire no
        response callback at all, so nothing would ever see the ack or
        a later packet's rxTime.

        Never called periodically or automatically -- see app.py's own
        caller, which only ever runs this from an explicit SYNC CLOCK
        activation.
        """
        interface = self._interface
        if interface is None:
            return ClockSyncResult(False, 0, None, "not_connected")
        local_node = getattr(interface, "localNode", None)
        if local_node is None:
            return ClockSyncResult(False, 0, None, "not_connected")

        from meshtastic.protobuf import admin_pb2

        target_interface = interface
        requested_epoch = int(time.time())
        write_message = admin_pb2.AdminMessage()
        write_message.set_time_only = requested_epoch

        response_seen = {"any": False, "nak": False, "rx_time": None}

        def on_ack(packet: Any) -> None:
            response_seen["any"] = True
            local_node.onAckNak(packet)
            if interface._acknowledgment.receivedNak:
                response_seen["nak"] = True
            rx_time = packet.get("rxTime") if isinstance(packet, dict) else None
            if isinstance(rx_time, (int, float)):
                response_seen["rx_time"] = int(rx_time)

        try:
            local_node._sendAdmin(write_message, onResponse=on_ack)
            interface.waitForAckNak()
        except Exception:
            pass

        if self._interface is not target_interface or self._connection_lost.is_set():
            return ClockSyncResult(False, requested_epoch, None, "disconnected")
        if response_seen["nak"]:
            return ClockSyncResult(False, requested_epoch, None, "nak")
        if not response_seen["any"]:
            return ClockSyncResult(False, requested_epoch, None, "timeout")

        rx_time = response_seen["rx_time"]
        if rx_time is not None and abs(rx_time - requested_epoch) <= self._CLOCK_SYNC_TOLERANCE_SECONDS:
            return ClockSyncResult(True, requested_epoch, rx_time, "")
        return ClockSyncResult(False, requested_epoch, rx_time, "unconfirmed")

    def remove_message_handler(
        self,
        handler: Callable[[ReceivedMessage], None],
    ) -> None:
        """Stop calling a previously registered message handler."""
        if handler in self._message_handlers:
            self._message_handlers.remove(handler)

    def send_text(
        self,
        text: str,
        channel_index: int = 0,
        destination_node_id: str | None = None,
        status_handler: Callable[[SendStatus], None] | None = None,
    ) -> SentMessage:
        """Submit text and optionally report matching routing ACK/NAK events.

        A successful return means the Python SDK accepted the packet for local
        radio submission. It does not mean another node received or read it.
        """
        message = validate_send_request(
            text,
            channel_index,
            destination_node_id,
        )
        if self._interface is None:
            raise RadioSendError("The radio is not connected.")

        sdk_arguments: dict[str, Any] = {
            "text": message.text,
            "channelIndex": message.channel_index,
        }
        # Resolved once, up front, so both the immediate onAckNak
        # callback and the independent _on_routing_response watch below
        # require an explicit DM ack to come specifically from THIS
        # destination -- never merely "some other, unrelated node" (see
        # MESHTASTICPASS DM item 6/_parse_send_response). None for a
        # broadcast/channel send, matching send_text's own existing
        # implicit-vs-remote distinction there unchanged.
        expected_destination_number = _node_number_from_id(message.destination_node_id)
        if message.destination_node_id is not None:
            sdk_arguments["destinationId"] = message.destination_node_id

        if status_handler is not None:
            # Meshtastic 2.7.x only permits ordinary ACK packets through a
            # sendText response callback when its name is exactly onAckNak.
            def onAckNak(packet: dict[str, Any]) -> None:
                status = self._parse_send_response(
                    packet, expected_destination_number=expected_destination_number
                )
                if status is not None:
                    status_handler(status)

            sdk_arguments.update(wantAck=True, onResponse=onAckNak)

        try:
            sdk_packet = self._interface.sendText(**sdk_arguments)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise RadioSendError(f"Could not send text message: {detail}") from error

        packet_id = self._optional_int(getattr(sdk_packet, "id", None))
        if status_handler is not None and packet_id is not None:
            # The SDK's own one-shot onResponse above only ever sees the
            # FIRST matching routing response (it pops its handler on
            # first use) -- keep watching independently via the generic
            # "meshtastic.receive.routing" topic so a genuinely stronger,
            # later-arriving confirmation is never silently dropped (see
            # _on_routing_response).
            self._pending_sends[packet_id] = (status_handler, expected_destination_number)
            while len(self._pending_sends) > self._MAX_PENDING_SENDS:
                self._pending_sends.pop(next(iter(self._pending_sends)), None)
        return SentMessage(
            message.text,
            message.channel_index,
            message.destination_node_id,
            packet_id=packet_id,
        )

    def _on_routing_response(
        self,
        packet: Any = None,
        interface: Any = None,
        **_kwargs: Any,
    ) -> None:
        """Watch every ROUTING_APP response for one this service is still

        tracking (see send_text/_pending_sends), independent of the
        SDK's own one-shot per-send callback. A stale interface (a
        reconnect already completed) can never resolve a send tracked
        against the PREVIOUS connection.
        """
        if interface is not None and interface is not self._interface:
            return
        decoded = packet.get("decoded") if isinstance(packet, dict) else None
        packet_id = (
            self._optional_int(decoded.get("requestId"))
            if isinstance(decoded, dict)
            else None
        )
        if packet_id is None:
            return
        pending = self._pending_sends.get(packet_id)
        if pending is None:
            return
        handler, expected_destination_number = pending
        status = self._parse_send_response(
            packet, expected_destination_number=expected_destination_number
        )
        if status is None:
            return
        handler(status)
        if status.state in (DeliveryState.HEARD, DeliveryState.FAILED):
            self._pending_sends.pop(packet_id, None)

    def _parse_send_response(
        self,
        packet: Any,
        *,
        expected_destination_number: int | None = None,
    ) -> SendStatus | None:
        """Convert a Meshtastic routing response into truthful delivery

        evidence. A clean ack whose "from" is our OWN node number is
        the SDK's own "implicit ack" (see node.py's onAckNak/
        receivedImplAck) -- real evidence the local radio/routing layer
        handled the packet, but never proof any other node received it,
        so it is reported as SENT, not HEARD. Only a clean ack whose
        "from" names a DIFFERENT node -- a DM destination's own
        explicit ack, or any other node's routing response genuinely
        reaching us -- is strong enough to report HEARD.

        `expected_destination_number` (set only for a DM send, never a
        broadcast/channel one -- see send_text) additionally REQUIRES
        that non-local "from" to equal the DM's own destination: a
        clean response from any other node must never complete a DM as
        HEARD (MESHTASTICPASS DM item 6). Such a response is not a NAK
        either -- it simply carries no conclusive evidence about THIS
        send, so it resolves to no status at all (None) rather than
        being reported as any terminal state, leaving the pending send
        exactly as it was (still eligible for a genuine ack, NAK, or
        eventual UNCONFIRMED timeout).
        """
        if not isinstance(packet, dict):
            return None
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict) or decoded.get("portnum") != "ROUTING_APP":
            return None
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            return None
        reason = self._normalize_routing_error(routing)
        if reason is None:
            return None
        packet_id = self._optional_int(decoded.get("requestId"))
        if reason != "NONE":
            return SendStatus(
                DeliveryState.FAILED,
                packet_id,
                detail=f"Meshtastic routing failure: {reason}",
            )

        from_number = self._optional_int(packet.get("from"))
        local_number = self._optional_int(
            getattr(getattr(self._interface, "myInfo", None), "my_node_num", None)
        )
        if (
            from_number is not None
            and local_number is not None
            and from_number != local_number
        ):
            if (
                expected_destination_number is not None
                and from_number != expected_destination_number
            ):
                return None
            return SendStatus(DeliveryState.HEARD, packet_id)
        return SendStatus(DeliveryState.SENT, packet_id)

    @staticmethod
    def _normalize_routing_error(routing: dict[str, Any]) -> str | None:
        """Normalize the enum shape emitted by Meshtastic SDK 2.7.11.

        The SDK uses protobuf ``MessageToDict`` with its default enum behavior:
        known values are symbolic strings, while an unset optional NONE field
        is omitted. Unknown future enum numbers remain integers and must not be
        interpreted as a definite ACK or NAK.
        """
        if "errorReason" not in routing:
            return "NONE"

        reason = routing["errorReason"]
        if not isinstance(reason, str):
            return None

        try:
            from meshtastic.protobuf import mesh_pb2

            mesh_pb2.Routing.Error.Value(reason)
        except (AttributeError, ImportError, ValueError):
            return None
        return reason

    # Node numbers on the wire are always unsigned 32-bit (route/
    # route_back are protobuf `fixed32`, never sign-extended) -- this
    # mask is purely defensive symmetry with _node_number_from_id's own
    # convention, not a correction of anything the SDK gets wrong.
    _NODE_NUMBER_MASK = 0xFFFFFFFF
    # PROTOBUF-SOURCE-VERIFIED (mesh_pb2.pyi RouteDiscovery.snr_towards/
    # snr_back): "SNR of the received packet, 1 = 0.25dB, -128 = invalid".
    _TRACEROUTE_UNKNOWN_SNR = -128

    def send_traceroute(
        self,
        destination_node_id: str,
        status_handler: Callable[[TracerouteStatus], None],
    ) -> int:
        """Send one explicit TRACEROUTE_APP request; report the outcome

        exactly once via `status_handler`, asynchronously, whenever a
        real response arrives (never for "request sent" alone -- see
        TracerouteState). This is genuine RF traffic, only ever
        triggered by an explicit caller action (never from here).

        Uses the SAME zero-RF-cost lora.hop_limit the app's own HOP
        LIMIT setting already reads/writes (read_synced_config_field) --
        never a second, independent hop-limit concept.

        Correlation is by `requestId` via the SDK's own one-shot
        sendData(onResponse=...) response-handler registry (Meshtastic
        2.7.11 mesh_interface.py) -- the SAME mechanism send_text uses
        for delivery ACKs/NAKs, reused here rather than the SDK's own
        sendTraceRoute/waitForTraceRoute helpers, which BLOCK the
        calling thread for up to `hopLimit + 1` multiples of a 300s base
        timeout (see meshtastic.util.Timeout.waitForTraceRoute) --
        entirely unsuitable for this app's async UI. This method never
        blocks; if the destination never responds at all, the SDK's own
        response-handler entry simply lingers unfired (the SAME
        characteristic already true of any unanswered wantResponse
        send) -- the caller is responsible for its own UI-appropriate
        timeout and for ignoring a callback it no longer cares about
        (see MeshtasticPassApp._active_traceroute's own packet_id guard).

        Returns the outgoing packet's own id, letting the caller
        correlate a later status_handler call against exactly this
        attempt via TracerouteStatus.packet_id -- critical because
        selection/navigation must never affect which attempt a response
        belongs to.
        """
        if self._interface is None:
            raise RadioSendError("The radio is not connected.")
        from meshtastic.protobuf import mesh_pb2, portnums_pb2

        hop_limit = self._optional_int(
            self.read_synced_config_field("lora", "hop_limit")
        )

        def onResponse(packet: dict[str, Any]) -> None:
            status = self._parse_traceroute_response(
                packet,
                destination_node_id=destination_node_id,
                now=time.time(),
            )
            if status is not None:
                status_handler(status)

        try:
            sdk_packet = self._interface.sendData(
                mesh_pb2.RouteDiscovery(),
                destinationId=destination_node_id,
                portNum=portnums_pb2.PortNum.TRACEROUTE_APP,
                wantResponse=True,
                onResponse=onResponse,
                hopLimit=hop_limit,
            )
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise RadioSendError(f"Could not send traceroute: {detail}") from error

        packet_id = self._optional_int(getattr(sdk_packet, "id", None))
        if packet_id is None:
            raise RadioSendError("Traceroute request was not assigned a packet ID.")
        return packet_id

    @staticmethod
    def _parse_traceroute_response(
        packet: Any,
        *,
        destination_node_id: str,
        now: float,
    ) -> TracerouteStatus | None:
        """Convert one raw response packet into a TracerouteStatus, or

        None when this packet carries no conclusive evidence yet (e.g.
        a bare ACK on ROUTING_APP that is not itself a NAK -- the real
        RouteDiscovery reply is always a separate, later packet).

        Two portnums legitimately arrive under the SAME requestId:
        ROUTING_APP (a NAK means the request could not be routed/
        delivered at all -- FAILED) or TRACEROUTE_APP (the destination's
        own real RouteDiscovery reply -- SUCCEEDED, carrying the actual
        route). Only these two ever resolve to a terminal status here.
        """
        if not isinstance(packet, dict):
            return None
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            return None
        packet_id = RadioService._optional_int(decoded.get("requestId"))
        portnum = decoded.get("portnum")

        if portnum == "ROUTING_APP":
            routing = decoded.get("routing")
            reason = (
                RadioService._normalize_routing_error(routing)
                if isinstance(routing, dict)
                else None
            )
            if reason is not None and reason != "NONE":
                return TracerouteStatus(
                    TracerouteState.FAILED,
                    packet_id,
                    detail=f"Meshtastic routing failure: {reason}",
                )
            return None

        if portnum != "TRACEROUTE_APP":
            return None
        traceroute_dict = decoded.get("traceroute")
        raw = traceroute_dict.get("raw") if isinstance(traceroute_dict, dict) else None
        if raw is None:
            return None

        def node_ids(numbers: Any) -> tuple[str, ...]:
            return tuple(
                f"!{number & RadioService._NODE_NUMBER_MASK:08x}" for number in numbers
            )

        def snr_values(values: Any) -> tuple[float | None, ...]:
            return tuple(
                None if value == RadioService._TRACEROUTE_UNKNOWN_SNR else value / 4.0
                for value in values
            )

        return TracerouteStatus(
            TracerouteState.SUCCEEDED,
            packet_id,
            result=TracerouteResult(
                destination_node_id=destination_node_id,
                forward_route=node_ids(raw.route),
                forward_snr=snr_values(raw.snr_towards),
                return_route=node_ids(raw.route_back),
                return_snr=snr_values(raw.snr_back),
                completed_at=now,
            ),
        )

    def close(self) -> None:
        """Close the serial connection if it is open."""
        # Item 7: a closed connection's config snapshot is never
        # current for whatever connects next -- discarded here rather
        # than left to be silently overwritten by the next connect(),
        # so a caller that queries config_snapshot() during the gap
        # (disconnected, or a failed reconnect attempt) truthfully sees
        # None instead of the PREVIOUS radio's now-stale configuration.
        self._config_snapshot = None
        # Same reasoning applies to local-node identity: a disconnect
        # for ANY reason (lost connection, failed reconnect, explicit
        # device-path change, or a physical radio swap) must not leave
        # the PREVIOUS radio's node ID sitting in _activity_local_node_id
        # for get_known_nodes()/_is_local_node() to keep comparing
        # against during the gap before the next connect() succeeds --
        # get_known_nodes() already returns () while self._interface is
        # None, so there is no "wrong YOU" to show during that gap, only
        # correctly no YOU at all (see MESH FOLLOW-UP item 5). The next
        # successful connect() always re-establishes both fresh from
        # that NEW radio's own reported identity.
        self._activity_local_node_id = None
        self._direct_observations.clear()
        self._link_observations.clear()
        if self._interface is not None:
            interface = self._interface
            self._interface = None
            try:
                interface.close()
            except Exception:
                # An unplugged serial device can fail while it is being closed.
                pass

    def _open_interface(self) -> Any:
        # Import here so a missing dependency becomes a friendly runtime error.
        from meshtastic.serial_interface import SerialInterface
        from pubsub import pub

        self._subscribe_to_events(pub)

        return SerialInterface(devPath=self.device_path)

    def _subscribe_to_events(self, pub: Any) -> None:
        if self._pub is not None:
            return

        pub.subscribe(
            self._on_connection_lost,
            "meshtastic.connection.lost",
        )
        try:
            pub.subscribe(
                self._on_text_received,
                "meshtastic.receive.text",
            )
        except Exception:
            pub.unsubscribe(
                self._on_connection_lost,
                "meshtastic.connection.lost",
            )
            raise

        try:
            # ROUTING_APP has a registered KnownProtocol("routing", ...)
            # in meshtastic/__init__.py, so its topic is
            # "meshtastic.receive.routing" -- NOT the generic
            # "meshtastic.receive.data.ROUTING_APP" pattern that applies
            # to portnums without one.
            pub.subscribe(
                self._on_routing_response,
                "meshtastic.receive.routing",
            )
        except Exception:
            pub.unsubscribe(
                self._on_connection_lost,
                "meshtastic.connection.lost",
            )
            pub.unsubscribe(
                self._on_text_received,
                "meshtastic.receive.text",
            )
            raise

        # Read-only diagnostic only: "meshtastic.receive" is the generic
        # topic the Meshtastic library publishes to for EVERY decoded
        # packet type (position, telemetry, nodeinfo, text, ...), in
        # addition to (never instead of) the type-specific topic above.
        # Subscribing here adds no RF traffic and changes no routing/
        # filtering decision this app makes -- it only lets
        # rx_debug_enabled() prove the production subscription is alive
        # for passive traffic too, which CHAT itself has no reason to
        # ever look at. Gated so a disabled diagnostic costs nothing
        # beyond the one-time environment check.
        if rx_debug_enabled():
            try:
                pub.subscribe(self._on_any_packet_for_debug, "meshtastic.receive")
            except Exception:
                pass

        # Always on (not diagnostic-gated): the same generic
        # "meshtastic.receive" topic, read-only, purely to capture
        # rx_rssi/rx_snr for MESH's passive LINK quality display (item
        # 22-ish, UI POLISH Part C). This requests nothing new from the
        # radio -- every packet here was already being decoded and
        # published by the SDK regardless of whether anything
        # subscribed; this only starts reading two fields the app
        # previously discarded. See _on_any_packet_for_link_quality.
        try:
            pub.subscribe(
                self._on_any_packet_for_link_quality, "meshtastic.receive"
            )
        except Exception:
            pass

        self._pub = pub

    def _on_connection_lost(self, interface: Any = None, **_kwargs: Any) -> None:
        if interface is self._interface:
            self._connection_lost.set()

    def _on_any_packet_for_debug(
        self,
        packet: Any = None,
        interface: Any = None,
        **_kwargs: Any,
    ) -> None:
        """Read-only diagnostic observer for every decoded packet type.

        Never touches _message_handlers, ChatStore, or any application
        state -- purely a print() for MESHTASTICPASS_RX_DEBUG=1. Skips
        TEXT_MESSAGE_APP entirely: _on_text_received already logs its
        own accept/reject decision for that portnum, and logging it
        twice here would be noise, not signal. Re-checks
        rx_debug_enabled() itself (in addition to _subscribe_to_events'
        one-time check before ever wiring this up) so this can never
        print if called directly with the diagnostic off.
        """
        if not rx_debug_enabled():
            return
        if interface is not None and interface is not self._interface:
            return
        from_id = self._format_from_id(packet)
        if not isinstance(packet, dict):
            return
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            rx_debug_log(f"{from_id} ENCRYPTED undecodable")
            return
        portnum = decoded.get("portnum")
        if portnum in ("TEXT_MESSAGE_APP", 1):
            return
        channel = packet.get("channel", 0)
        rx_debug_log(f"{from_id} {portnum} channel={channel} observed")

    def _on_any_packet_for_link_quality(
        self,
        packet: Any = None,
        interface: Any = None,
        **_kwargs: Any,
    ) -> None:
        """Passively record directly-heard signal quality, for MESH LINK.

        Fires for every decoded packet type (see _subscribe_to_events'
        comment on the generic "meshtastic.receive" topic) -- portnum-
        agnostic on purpose, since a node without any CHAT history can
        still have valid, honest LINK data from a position/telemetry/
        nodeinfo/routing-ack packet it sent.

        Records rx_rssi/rx_snr ONLY when traversed_hops(hop_start,
        hop_limit) == 0 for this specific packet -- a genuine zero-relay
        reception, so the reading describes the RF link to the packet's
        own sender and not to some intermediate relay (see
        LinkObservation's docstring). Any other case (indeterminate, or
        one-or-more hops traveled) is silently skipped: never recorded
        as if it were direct, and never used to overwrite a previous
        genuinely-direct reading with a multi-hop one.
        """
        if interface is not None and interface is not self._interface:
            return
        if not isinstance(packet, dict):
            return
        if traversed_hops(
            self._optional_int(packet.get("hopStart")),
            self._optional_int(packet.get("hopLimit")),
        ) != 0:
            return
        rssi = self._optional_int(packet.get("rxRssi"))
        snr = self._optional_float(packet.get("rxSnr"))
        if rssi is None and snr is None:
            return
        node_id = self._format_from_id(packet)
        normalized = node_id.strip().lower() if isinstance(node_id, str) else ""
        if not normalized or normalized == "!unknown":
            return
        self._link_observations[normalized] = LinkObservation(
            rssi=rssi, snr=snr, observed_at=time.time()
        )

    def get_link_quality(self, node_id: str) -> LinkObservation | None:
        """Read the most recent directly-heard signal quality for a node.

        Transmits nothing; returns None whenever nothing has been
        directly heard from this node since the current connection (or
        the current radio identity) began -- see LinkObservation's own
        docstring for what "directly heard" requires.
        """
        normalized = node_id.strip().lower() if isinstance(node_id, str) else ""
        if not normalized:
            return None
        return self._link_observations.get(normalized)

    @staticmethod
    def _format_from_id(packet: Any) -> str:
        if not isinstance(packet, dict):
            return "!unknown"
        sender_id = packet.get("fromId")
        if isinstance(sender_id, str) and sender_id:
            return sender_id
        sender_number = RadioService._optional_int(packet.get("from"))
        return f"!{sender_number:08x}" if sender_number is not None else "!unknown"

    @staticmethod
    def _text_packet_reject_reason(packet: Any) -> str:
        """Diagnostic-only: classify why _parse_text_packet returned None,

        mirroring its checks in the same order. Never used to make an
        actual filtering decision -- only to answer "why was this
        decoded text packet not accepted" in the RX debug log.
        """
        if not isinstance(packet, dict):
            return "not_a_packet"
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            return "undecoded_or_encrypted"
        portnum = decoded.get("portnum")
        if portnum not in ("TEXT_MESSAGE_APP", 1):
            return f"not_text_message_app(portnum={portnum!r})"
        text = RadioService._decode_text(decoded)
        if text is None or text == "":
            return "empty_or_undecodable_text"
        return "unknown"

    def _on_text_received(
        self,
        packet: Any = None,
        interface: Any = None,
        **_kwargs: Any,
    ) -> None:
        debug = rx_debug_enabled()
        if interface is not None and interface is not self._interface:
            if debug:
                rx_debug_log(
                    f"{self._format_from_id(packet)} TEXT_MESSAGE_APP "
                    "ignored reason=stale_interface"
                )
            return

        message = self._parse_text_packet(packet, self._interface)
        if message is None:
            if debug:
                reason = self._text_packet_reject_reason(packet)
                rx_debug_log(
                    f"{self._format_from_id(packet)} TEXT_MESSAGE_APP "
                    f"ignored reason={reason}"
                )
            return

        if debug:
            rx_debug_log(
                f"{message.sender_node_id} TEXT_MESSAGE_APP "
                f"channel={message.channel_index} accepted"
            )

        self._record_direct_observation(message.sender_node_id)

        for handler in tuple(self._message_handlers):
            try:
                handler(message)
            except Exception:
                # One consumer should not stop radio packet processing.
                pass

    def _record_direct_observation(
        self,
        node_id: str,
        observed_at: float | None = None,
    ) -> None:
        """Remember a valid directly received packet without transmitting."""
        if not isinstance(node_id, str):
            return
        normalized = node_id.strip().lower()
        if not normalized or normalized == "unknown":
            return
        timestamp = time.time() if observed_at is None else observed_at
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            return
        self._direct_observations[normalized] = float(timestamp)

    def _unsubscribe_from_events(self) -> None:
        if self._pub is not None:
            subscriptions = (
                (self._on_connection_lost, "meshtastic.connection.lost"),
                (self._on_text_received, "meshtastic.receive.text"),
                (self._on_routing_response, "meshtastic.receive.routing"),
                (self._on_any_packet_for_debug, "meshtastic.receive"),
                (self._on_any_packet_for_link_quality, "meshtastic.receive"),
            )
            for callback, topic in subscriptions:
                try:
                    self._pub.unsubscribe(callback, topic)
                except Exception:
                    pass
            self._pub = None

    def _check_device(self) -> None:
        path = Path(self.device_path)
        if not self._device_exists():
            raise RadioConnectionError(
                f"Serial device {self.device_path} was not found. "
                "Check the USB cable and the selected device path.",
                state=RadioState.OFFLINE,
            )

        if not os.access(path, os.R_OK | os.W_OK):
            raise RadioConnectionError(
                f"Permission denied for {self.device_path}. "
                "Add your current user to the serial-device access group "
                "(commonly dialout), then log out and back in."
            )

    def _device_exists(self) -> bool:
        return Path(self.device_path).exists()

    def _parse_text_packet(
        self,
        packet: Any,
        interface: Any,
    ) -> ReceivedMessage | None:
        if not isinstance(packet, dict):
            return None

        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            return None
        if decoded.get("portnum") not in ("TEXT_MESSAGE_APP", 1):
            return None

        text = self._decode_text(decoded)
        if text is None or text == "":
            return None

        sender_number = self._optional_int(packet.get("from"))
        sender_id = packet.get("fromId")
        if not isinstance(sender_id, str) or not sender_id:
            canonical_sender_number = _canonical_node_number(sender_number)
            sender_id = (
                f"!{canonical_sender_number:08x}"
                if canonical_sender_number is not None
                else "unknown"
            )

        sender_record = self._lookup_node_record(
            interface,
            sender_number,
            sender_id,
        )
        user = self._user_from_record(sender_record)

        channel_value = packet.get("channel", 0)
        return ReceivedMessage(
            sender_node_id=sender_id,
            sender_long_name=self._optional_string(user.get("longName")),
            sender_short_name=self._optional_string(user.get("shortName")),
            channel_index=self._optional_int(channel_value),
            text=text,
            rssi=self._optional_int(packet.get("rxRssi")),
            snr=self._optional_float(packet.get("rxSnr")),
            packet_id=self._optional_int(packet.get("id")),
            # Ordinary TEXT_MESSAGE_APP packets do not carry a trustworthy
            # sender-origin timestamp. Meshtastic rxTime is receiver-side.
            origin_sent_at=None,
            radio_rx_at=self._optional_float(packet.get("rxTime")),
            local_position=self._local_position(interface),
            sender_position=self._position_from_record(sender_record),
            is_direct=self._is_direct_message(packet, interface),
        )

    @staticmethod
    def _is_direct_message(packet: dict[str, Any], interface: Any) -> bool:
        """True only when MeshPacket.to names THIS radio's own node

        number specifically -- the exact destination/routing check the
        Meshtastic SDK's own bundled CLI uses (__main__.py:
        ``packet["to"] == interface.myInfo.my_node_num``;
        SDK-SOURCE-VERIFIED), never a heuristic based on sender name,
        display name, or channel index. An ordinary broadcast/channel
        packet's `to` is BROADCAST_NUM (0xFFFFFFFF, PROTOBUF-SOURCE-
        VERIFIED: MeshPacket.to's own field doc), which can never equal
        a real node number, so comparing only against the local node
        number already correctly excludes it -- no separate broadcast
        check is needed. Both sides are canonically masked to unsigned
        32-bit first (see _canonical_node_number) so a signed/unsigned
        representation mismatch can never produce a false negative.
        """
        destination_number = _canonical_node_number(
            RadioService._optional_int(packet.get("to"))
        )
        local_number = _canonical_node_number(
            getattr(getattr(interface, "myInfo", None), "my_node_num", None)
        )
        return (
            destination_number is not None
            and local_number is not None
            and destination_number == local_number
        )

    @staticmethod
    def _decode_text(decoded: dict[str, Any]) -> str | None:
        value = decoded.get("text")
        if value is None:
            value = decoded.get("payload")

        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return None

    @staticmethod
    def _lookup_sender_user(
        interface: Any,
        sender_number: int | None,
        sender_id: str,
    ) -> dict[str, Any]:
        record = RadioService._lookup_node_record(
            interface,
            sender_number,
            sender_id,
        )
        return RadioService._user_from_record(record)

    @staticmethod
    def _lookup_node_record(
        interface: Any,
        node_number: int | None,
        node_id: str,
    ) -> dict[str, Any]:
        if interface is None:
            return {}

        record: Any = None
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_number, dict) and node_number is not None:
            record = nodes_by_number.get(node_number)

        if not isinstance(record, dict):
            nodes_by_id = getattr(interface, "nodes", None)
            if isinstance(nodes_by_id, dict):
                record = nodes_by_id.get(node_id)

        return record if isinstance(record, dict) else {}

    @staticmethod
    def _user_from_record(record: dict[str, Any]) -> dict[str, Any]:
        user = record.get("user")
        return user if isinstance(user, dict) else {}

    @staticmethod
    def _local_position(interface: Any) -> GeoPosition | None:
        if interface is None:
            return None
        my_info = getattr(interface, "myInfo", None)
        node_number = getattr(my_info, "my_node_num", None)
        record = RadioService._lookup_node_record(interface, node_number, "")
        return RadioService._position_from_record(record)

    @staticmethod
    def _position_from_record(record: Any) -> GeoPosition | None:
        """Extract the node-position shape used by Meshtastic SDK 2.7.11."""
        if not isinstance(record, dict):
            return None
        position = record.get("position")
        if not isinstance(position, dict):
            return None

        latitude = position.get("latitude")
        longitude = position.get("longitude")
        if latitude is None and "latitudeI" in position:
            value = RadioService._optional_float(position.get("latitudeI"))
            latitude = value * 1e-7 if value is not None else None
        if longitude is None and "longitudeI" in position:
            value = RadioService._optional_float(position.get("longitudeI"))
            longitude = value * 1e-7 if value is not None else None

        updated_at = RadioService._optional_position_time(position)
        return make_geo_position(latitude, longitude, updated_at)

    @staticmethod
    def _optional_position_time(position: dict[str, Any]) -> float | None:
        """Prefer the GPS solution time, then the SDK's received-position time."""
        for field in ("timestamp", "time"):
            value = RadioService._optional_float(position.get(field))
            if value is not None and value > 0:
                return value
        return None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def _read_radio_info(self) -> RadioInfo:
        if self._interface is None:
            raise RadioConnectionError("The radio is not connected.")

        my_info = self._interface.myInfo
        local_node = self._interface.localNode
        if my_info is None or local_node is None:
            raise RadioConnectionError(
                "The serial port opened, but the initial Meshtastic sync did not complete."
            )

        node_number = getattr(my_info, "my_node_num", None)
        canonical_number = _canonical_node_number(node_number)
        # nodesByNum may key its entries by the canonical unsigned form
        # even when my_node_num itself surfaces as a signed Python int
        # (any node number with bit 31 set) -- looking up the RAW,
        # possibly-signed number here can silently miss the local
        # node's own record. Try the raw key first (the common case,
        # where both already agree), then the canonical form.
        local_record = self._interface.nodesByNum.get(node_number) or (
            self._interface.nodesByNum.get(canonical_number, {})
            if canonical_number is not None
            else {}
        )
        user = local_record.get("user", {})
        metadata = self._interface.metadata

        node_id = user.get("id") or (
            f"!{canonical_number:08x}" if canonical_number is not None else "unknown"
        )

        return RadioInfo(
            device_path=self.device_path,
            node_id=node_id,
            long_name=user.get("longName", "unknown"),
            short_name=user.get("shortName", "unknown"),
            firmware_version=getattr(metadata, "firmware_version", "unknown"),
            known_nodes=len(self._interface.nodesByNum),
            channels=self._read_channel_info(local_node),
        )

    @staticmethod
    def _read_channel_info(local_node: Any) -> tuple[ChannelInfo, ...]:
        """Convert enabled SDK channel protobufs into stable app values."""
        channels = getattr(local_node, "channels", None)
        if not channels:
            return ()
        try:
            from meshtastic.protobuf import channel_pb2

            disabled_role = channel_pb2.Channel.Role.DISABLED
        except (ImportError, AttributeError):
            disabled_role = 0

        result: list[ChannelInfo] = []
        seen_indexes: set[int] = set()
        for fallback_index, channel in enumerate(channels):
            role = getattr(channel, "role", disabled_role)
            if role == disabled_role or role == "DISABLED":
                continue
            raw_index = getattr(channel, "index", fallback_index)
            index = RadioService._optional_int(raw_index)
            if index is None or not 0 <= index <= 7 or index in seen_indexes:
                continue
            settings = getattr(channel, "settings", None)
            raw_name = getattr(settings, "name", "")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            if not name and index == 0:
                name = RadioService._primary_channel_default_name(local_node)
            resolved_name = name or f"Channel {index + 1}"
            result.append(
                ChannelInfo(
                    index,
                    resolved_name,
                    stable_key=RadioService._channel_stable_key(settings, resolved_name),
                )
            )
            seen_indexes.add(index)
        return tuple(sorted(result, key=lambda channel: channel.index))

    @staticmethod
    def _channel_stable_key(settings: Any, resolved_name: str) -> str:
        """This channel's own cryptographic/assigned identity (CHAT

        channel-history isolation) -- NEVER the radio-assigned slot
        index, which a user can freely reassign to a completely
        different channel (e.g. reconfiguring slot 0 from "LongFast" to
        "MediumSlow") while keeping the SAME index. Preference order:

        1. Channel.settings.id -- PROTOBUF-SOURCE-VERIFIED (channel_pb2.pyi):
           "Used to construct a globally unique channel ID... Any time
           a non wire compatible change is made to a channel, this
           field should be regenerated." The SDK's own purpose-built,
           64-bit, collision-negligible answer to exactly this question
           -- used whenever the connected firmware actually populates it
           (0 is its unset/default value, never a real assigned id).
        2. meshtastic.util.generate_channel_hash(name, psk) -- the SAME
           8-bit "channel number" hash Meshtastic's own official tooling
           (meshtastic.node) already computes for channel disambiguation,
           paired with the resolved display name here specifically to
           close the "two different names happen to hash the same" case
           (the hash alone has a real, if small, 1-in-256 collision
           floor for two independently-chosen PSKs -- not eliminated by
           this pairing, just not made worse by name-only guessing).
        3. The resolved display name alone, if `settings`/`psk` are
           unavailable for some reason -- strictly better than the bare
           index (a rename is at least a DELIBERATE, visible user
           action, unlike a same-index slot reassignment), even though
           it cannot detect a same-name-different-PSK reassignment.

        Returns "" (never a fabricated key) only if `settings` itself
        is entirely unavailable -- callers treat "" as "unknown," the
        same honest default a pre-connection placeholder channel uses.
        """
        if settings is None:
            return ""
        settings_id = RadioService._optional_int(getattr(settings, "id", None))
        if settings_id:
            return f"id:{settings_id}"
        psk = getattr(settings, "psk", None)
        if isinstance(psk, (bytes, bytearray)) and psk:
            try:
                from meshtastic.util import generate_channel_hash

                channel_hash = generate_channel_hash(resolved_name, bytes(psk))
            except Exception:
                return resolved_name
            return f"hash:{resolved_name}:{channel_hash}"
        return resolved_name

    @staticmethod
    def _primary_channel_default_name(local_node: Any) -> str:
        """Derive the unnamed primary channel label from the radio preset."""
        try:
            from meshtastic.protobuf import config_pb2

            lora = local_node.localConfig.lora
            if not lora.use_preset:
                return ""
            enum_name = config_pb2.Config.LoRaConfig.ModemPreset.Name(
                lora.modem_preset
            )
        except (ImportError, AttributeError, KeyError, TypeError, ValueError):
            return ""
        return "".join(part.title() for part in enum_name.split("_"))


def apply_radio_config_preset(radio: Any, preset: Any) -> RadioApplyResult:
    """Apply one saved radio/network configuration as a controlled,

    sequential, individually-write-verified multi-field operation --
    LoRaConfig.use_preset=True, then modem_preset, then channel_num
    (frequency slot), then the PRIMARY channel's name/psk. Stops at
    the first failing step (see RadioApplyResult's own docstring).

    `radio` is duck-typed against RadioService/SimulatedRadioService's
    identical write_verified_config_field/write_verified_primary_
    channel methods (never imported/type-checked against either class
    directly) -- this is a free function, not a RadioService method,
    specifically so ADVANCED RADIO CONFIG's APPLY behaves IDENTICALLY
    in --simulate and on real hardware, sharing this exact sequencing
    and stop-at-first-failure reporting rather than two independently
    maintained copies of it.

    `preset` is duck-typed against app_settings.RadioConfigPreset's own
    attributes (modem_preset/frequency_slot/channel_name/
    channel_psk_base64) -- never imported from app_settings, to keep
    this module's existing zero-dependency-on-app_settings boundary
    intact.

    use_preset=True (never manually inferring bandwidth/spread_factor/
    coding_rate from the chosen modem_preset) matches the task's own
    "Use preset mode" requirement -- those three raw LoRa physics
    fields are left for the radio's own preset table to fill in.

    The whole sequence is wrapped in a Meshtastic settings-edit
    transaction (begin_settings_transaction/commit_settings_transaction
    -- best-effort, a no-op on an SDK/simulator that lacks it), exactly
    as `meshtastic --set` wraps a multi-field config change: the
    firmware persists (and reboots if required) on the COMMIT, not on
    each loose set_config. The commit runs in a `finally` so a
    transaction is never left dangling on the radio even when a write
    fails partway.
    """
    try:
        from meshtastic.protobuf import config_pb2

        modem_preset_value = config_pb2.Config.LoRaConfig.ModemPreset.Value(
            preset.modem_preset
        )
    except Exception:
        return RadioApplyResult(
            False,
            "modem_preset",
            {
                "modem_preset": ConfigWriteResult(
                    False, preset.modem_preset, None, "invalid"
                )
            },
        )

    results: dict[str, ConfigWriteResult] = {}
    begin = getattr(radio, "begin_settings_transaction", None)
    commit = getattr(radio, "commit_settings_transaction", None)
    in_transaction = bool(begin()) if callable(begin) else False

    try:
        results["use_preset"] = radio.write_verified_config_field(
            "lora", "use_preset", True
        )
        if not results["use_preset"].applied:
            return RadioApplyResult(False, "use_preset", results)

        results["modem_preset"] = radio.write_verified_config_field(
            "lora", "modem_preset", modem_preset_value
        )
        if not results["modem_preset"].applied:
            return RadioApplyResult(False, "modem_preset", results)

        results["frequency_slot"] = radio.write_verified_config_field(
            "lora", "channel_num", preset.frequency_slot
        )
        if not results["frequency_slot"].applied:
            return RadioApplyResult(False, "frequency_slot", results)

        try:
            psk_bytes = (
                base64.b64decode(preset.channel_psk_base64)
                if preset.channel_psk_base64
                else b""
            )
        except Exception:
            results["channel"] = ConfigWriteResult(
                False, preset.channel_psk_base64, None, "invalid"
            )
            return RadioApplyResult(False, "channel", results)

        results["channel"] = radio.write_verified_primary_channel(
            name=preset.channel_name, psk=psk_bytes
        )
        if not results["channel"].applied:
            return RadioApplyResult(False, "channel", results)

        return RadioApplyResult(True, "", results)
    finally:
        if in_transaction and callable(commit):
            commit()


@dataclass(frozen=True)
class RadioConfigFieldCheck:
    """One field of a NETWORK-apply readback verification."""

    field: str          # "modem_preset" | "frequency_slot" | "channel_psk"
    requested: str       # already display-safe (PSK is a length note only)
    actual: str
    match: bool


@dataclass(frozen=True)
class RadioConfigVerification:
    ok: bool
    checks: tuple[RadioConfigFieldCheck, ...]
    mismatched_field: str          # "" when ok
    channel_name_note: str         # diagnostic only, never a pass/fail input


# The three LoRa/channel apply steps a mismatch can be attributed to,
# mapped to the compact UI wording the failure status uses.
NETWORK_FIELD_LABELS = {
    "modem_preset": "RADIO MODE MISMATCH",
    "frequency_slot": "FREQ. SLOT MISMATCH",
    "channel_psk": "KEY MISMATCH",
}


def verify_radio_config_preset(radio: Any, preset: Any) -> RadioConfigVerification:
    """Compare the connected radio's ACTUAL already-synced lora +

    primary-channel state against `preset`, field by field and
    SEMANTICALLY -- for the post-apply verification step (after a
    reconnect full-sync, or after RadioService.reread_lora_and_primary_
    channel for a no-reboot apply). Duck-typed against read_synced_
    config_field / read_primary_channel_settings, like
    apply_radio_config_preset, so it behaves identically in --simulate.

    channel_num: a requested 0 is the Meshtastic "auto-select" sentinel
    (the firmware derives the real frequency from region + modem preset
    + a hash of the primary channel name; the stored config field stays
    0), so requested 0 matches an actual 0 OR an unavailable/unset
    readback -- never a false literal-equality failure. An explicit
    slot (MS48 -> 48) must match exactly.

    PSK: compared with psk_matches_request (0x01 default sentinel <->
    expanded default key; "" <-> 0x00). Only a decoded LENGTH is ever
    put in the returned strings -- never key bytes.
    """
    checks: list[RadioConfigFieldCheck] = []

    raw_modem = radio.read_synced_config_field("lora", "modem_preset")
    try:
        from radio_capabilities import modem_preset_enum_name

        actual_modem = (
            modem_preset_enum_name(raw_modem) if raw_modem is not None else None
        )
    except Exception:
        actual_modem = None
    checks.append(
        RadioConfigFieldCheck(
            "modem_preset",
            str(preset.modem_preset),
            actual_modem or "unavailable",
            actual_modem == preset.modem_preset,
        )
    )

    raw_slot = radio.read_synced_config_field("lora", "channel_num")
    requested_slot = int(getattr(preset, "frequency_slot", 0) or 0)
    if raw_slot is None:
        slot_match = requested_slot == 0
        actual_slot_text = "unset"
    else:
        actual_slot = int(raw_slot)
        slot_match = actual_slot == requested_slot or (
            requested_slot == 0 and actual_slot == 0
        )
        actual_slot_text = str(actual_slot)
    checks.append(
        RadioConfigFieldCheck(
            "frequency_slot", str(requested_slot), actual_slot_text, slot_match
        )
    )

    try:
        requested_psk = (
            base64.b64decode(preset.channel_psk_base64)
            if preset.channel_psk_base64
            else b""
        )
    except Exception:
        requested_psk = b""
    primary = None
    reader = getattr(radio, "read_primary_channel_settings", None)
    if callable(reader):
        primary = reader()
    actual_name = primary[0] if primary else ""
    actual_psk = primary[1] if primary else b""
    psk_match = psk_matches_request(requested_psk, actual_psk)
    checks.append(
        RadioConfigFieldCheck(
            "channel_psk",
            f"len={len(requested_psk)}",
            f"len={len(actual_psk)}",
            psk_match,
        )
    )

    # NETWORK NAME is local-only and must never be written as the
    # Meshtastic primary-channel name -- this is a diagnostic breadcrumb
    # only (report if the channel name unexpectedly changed), never a
    # verification failure.
    requested_channel_name = getattr(preset, "channel_name", "") or ""
    if actual_name == requested_channel_name:
        channel_name_note = f"channel_name={actual_name or 'blank'} (as requested)"
    else:
        channel_name_note = (
            f"channel_name={actual_name or 'blank'} "
            f"(requested {requested_channel_name or 'blank'})"
        )

    mismatched = next((c.field for c in checks if not c.match), "")
    return RadioConfigVerification(
        ok=not mismatched,
        checks=tuple(checks),
        mismatched_field=mismatched,
        channel_name_note=channel_name_note,
    )
