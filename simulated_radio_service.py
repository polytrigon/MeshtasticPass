"""Deterministic Meshtastic radio simulation for development without hardware."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from queue import Empty, Queue
from threading import Event
import time
from typing import Callable, Iterator

from geo import GeoPosition
from node_activity import count_active_other_nodes
from radio_service import (
    ChannelInfo,
    ClockSyncResult,
    ConfigWriteResult,
    RadioApplyResult,
    RadioEvent,
    DeliveryState,
    DISPLAY_UNITS_METRIC,
    RadioInfo,
    RadioIdentityError,
    RadioSendError,
    RadioState,
    LinkObservation,
    NodeMetadata,
    ReceivedMessage,
    SentMessage,
    SendStatus,
    TracerouteResult,
    TracerouteState,
    TracerouteStatus,
    NodeRemoveResult,
    validate_send_request,
    validate_long_name,
    validate_short_name,
)


SIMULATED_DEVICE_PATHS = ("/dev/ttyUSB0", "/dev/ttyUSB1")
# The deterministic canonical local node ID the simulated radio reports
# as its own identity. Tests that pre-seed ChatStore history for the
# simulated radio must set_local_node_id to this before writing, so the
# seeded rows live in the SAME per-radio namespace the app activates on
# connect (CHAT state-integrity -- per-physical-radio history isolation).
SIMULATED_LOCAL_NODE_ID = "!51a00001"
SIMULATED_CHANNELS = (
    ChannelInfo(0, "LongFast", stable_key="sim-longfast"),
    ChannelInfo(1, "Hiking", stable_key="sim-hiking"),
)


class SimulatedSendOutcome(Enum):
    """Explicit deterministic result for one simulated send attempt."""

    SENT = "sent"
    HEARD = "heard"
    UNCONFIRMED = "unconfirmed"
    FAILED = "failed"


class SimulatedTracerouteOutcome(Enum):
    """Explicit deterministic result for one simulated traceroute attempt.

    NO_RESPONSE never calls the status_handler at all -- for exercising
    the app's own TRACEROUTE_TIMEOUT_SECONDS path without an artificial
    real-time wait.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NO_RESPONSE = "no_response"


@dataclass(frozen=True)
class SimulatedNode:
    """Stable fake node information for repeatable development and tests."""

    node_id: str
    long_name: str | None
    short_name: str | None
    position: GeoPosition | None
    last_heard_age_seconds: object | None
    hops_away: int | None = None
    # Fake directly-heard RF signal quality (see RadioService.
    # LinkObservation/get_link_quality) -- only meaningful, and only
    # ever surfaced by get_link_quality below, when hops_away == 0, the
    # same "zero relays traveled" honesty rule the real radio enforces
    # via traversed_hops(). Every existing node below predates MESH
    # LINK and deliberately keeps hops_away >= 1 or None, so none of
    # them gain LINK data by accident.
    rssi: int | None = None
    snr: float | None = None


SIMULATED_LOCAL_POSITION = GeoPosition(40.7128, -74.0060, 1_700_000_000.0)

SIMULATED_NODES = (
    SimulatedNode(
        "!a11ce001",
        "Alice Trail",
        "ALCE",
        GeoPosition(40.7736, -73.9566, 1_700_000_100.0),
        30.0,
        1,
    ),
    SimulatedNode("!b0b00002", "Bob Basecamp", "BOB", None, 299.0, 1),
    SimulatedNode(
        "!cafe0003",
        "Cafe Relay",
        "CAFE",
        GeoPosition(40.6501, -73.9496, 1_700_000_200.0),
        400.0,
        None,
    ),
    SimulatedNode("!bad00004", "Malformed Clock", "BAD", None, "recent"),
    SimulatedNode("!none0005", "Missing Clock", "NONE", None, None),
    SimulatedNode("!10a60006", None, "NOLN", None, 600.0, 2),
    SimulatedNode("!5a070007", "No Short Name", None, None, 600.0, None),
    SimulatedNode(
        "!d1ec7008",
        "Direct Neighbor",
        "DIRN",
        GeoPosition(40.7300, -73.9950, 1_700_000_300.0),
        45.0,
        0,
        rssi=-52,
        snr=9.5,
    ),
)

_SIMULATED_REFERENCE_TIME = time.time()


SIMULATED_MESSAGES = (
    # Channel 0 deliberately delivers a newer receiver timestamp first, then
    # an older one. This makes chronological insertion and its notice visible.
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[0].node_id,
        sender_long_name=SIMULATED_NODES[0].long_name,
        sender_short_name=SIMULATED_NODES[0].short_name,
        channel_index=0,
        text="Hello from the trail!",
        rssi=-87,
        snr=6.5,
        packet_id=350000001,
        radio_rx_at=_SIMULATED_REFERENCE_TIME - 8,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[0].position,
    ),
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[1].node_id,
        sender_long_name=SIMULATED_NODES[1].long_name,
        sender_short_name=SIMULATED_NODES[1].short_name,
        channel_index=0,
        text="Basecamp checking in.",
        rssi=-73,
        snr=8.25,
        packet_id=350000002,
        radio_rx_at=_SIMULATED_REFERENCE_TIME - 27 * 60,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[1].position,
    ),
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[2].node_id,
        sender_long_name=SIMULATED_NODES[2].long_name,
        sender_short_name=SIMULATED_NODES[2].short_name,
        channel_index=1,
        text="Relay link is online.",
        rssi=-101,
        snr=2.0,
        packet_id=350000003,
        radio_rx_at=_SIMULATED_REFERENCE_TIME - (2 * 60 + 12) * 60,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[2].position,
    ),
    # Two more channel-0 arrivals are older than the first message but newer
    # than Bob. Together these provide three deterministic older NEW entries
    # for exercising Left Arrow catch-up in simulation.
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[2].node_id,
        sender_long_name=SIMULATED_NODES[2].long_name,
        sender_short_name=SIMULATED_NODES[2].short_name,
        channel_index=0,
        text="Cafe relay heard the primary channel.",
        rssi=-96,
        snr=3.5,
        packet_id=350000004,
        radio_rx_at=_SIMULATED_REFERENCE_TIME - 20 * 60,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[2].position,
    ),
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[3].node_id,
        sender_long_name=SIMULATED_NODES[3].long_name,
        sender_short_name=SIMULATED_NODES[3].short_name,
        channel_index=0,
        text="Clock check from the ridge.",
        rssi=-91,
        snr=4.0,
        packet_id=350000005,
        radio_rx_at=_SIMULATED_REFERENCE_TIME - 15 * 60,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[3].position,
    ),
)


class SimulatedRadioService:
    """Behaves like RadioService while producing deterministic fake events."""

    def __init__(
        self,
        device_path: str = SIMULATED_DEVICE_PATHS[0],
        connect_delay: float = 0.25,
        message_interval: float = 0.75,
        scripted_messages: tuple[ReceivedMessage, ...] = SIMULATED_MESSAGES,
        send_outcomes: tuple[SimulatedSendOutcome, ...] = (),
        traceroute_outcomes: tuple[SimulatedTracerouteOutcome, ...] = (),
        traceroute_forward_route: tuple[str, ...] = (),
        traceroute_forward_snr: tuple[float | None, ...] = (),
        traceroute_return_route: tuple[str, ...] = (),
        traceroute_return_snr: tuple[float | None, ...] = (),
    ) -> None:
        if device_path not in SIMULATED_DEVICE_PATHS:
            device_path = SIMULATED_DEVICE_PATHS[0]
        self.device_path = device_path
        self.info = RadioInfo(
            device_path=self.device_path,
            node_id=SIMULATED_LOCAL_NODE_ID,
            long_name="Simulated Node",
            short_name="SIM",
            firmware_version="sim-1.0.0",
            known_nodes=len(SIMULATED_NODES) + 1,
            channels=SIMULATED_CHANNELS,
        )
        self.connect_delay = connect_delay
        self.message_interval = message_interval
        self.scripted_messages = scripted_messages
        self.send_outcomes = send_outcomes
        self.traceroute_outcomes = traceroute_outcomes
        self.traceroute_forward_route = traceroute_forward_route
        self.traceroute_forward_snr = traceroute_forward_snr
        self.traceroute_return_route = traceroute_return_route
        self.traceroute_return_snr = traceroute_return_snr
        self._traceroute_count = 0
        self._message_handlers: list[Callable[[ReceivedMessage], None]] = []
        self._state_events: Queue[RadioEvent] = Queue()
        self._stop_event = Event()
        self._online = False
        self._closed = False
        self._sent_messages: list[SentMessage] = []
        # NodeDB entries removed via remove_node -- a deterministic stand-in
        # for the radio's own NodeDB deletion. The local node is never added
        # here (self-protection) and a removed node only reappears if the
        # mesh is heard from again (see _hear_node), mirroring real
        # discovery. Lowercased canonical node IDs.
        self._removed_node_ids: set[str] = set()
        self._send_count = 0
        self._activity_reference_time: float | None = None
        self._direct_observations: dict[str, float] = {}
        # Deterministic fake RADIO-section state -- mirrors the real
        # localConfig sections RadioService.write_verified_config_field
        # writes/reads, so app.py's RADIO section behaves identically
        # under --simulate without touching real hardware.
        self._config_sections: dict[str, dict[str, object]] = {
            "display": {
                "screen_on_secs": 300,
                "units": DISPLAY_UNITS_METRIC,
                "compass_north_top": True,
                "flip_screen": False,
                "use_12h_clock": False,
            },
            "bluetooth": {"enabled": True},
            # role=0 is Config.DeviceConfig.Role.CLIENT (see
            # radio_capabilities.role_choices) -- matches this fake
            # radio's own hardware_identity() role_name="CLIENT" below.
            "device": {"tzdef": "", "role": 0},
            # lora.hop_limit -- PROTOBUF-SOURCE-VERIFIED default of 3
            # (Config.LoRaConfig.hop_limit docstring), but a fully valid
            # 0-7 range (see app.HOP_LIMIT_CHOICES) -- this fake radio's
            # own "current value" for the HOP LIMIT RADIO setting, never
            # a display-side default. use_preset/modem_preset/
            # channel_num (ADVANCED RADIO CONFIG) mirror LONG_FAST's own
            # real default (Config.LoRaConfig.ModemPreset.LONG_FAST == 0,
            # channel_num 0 == "radio auto-selects") -- these three MUST
            # already exist here, never only appear once first written,
            # since write_verified_config_field's own simulated
            # implementation (below) refuses to write a field this dict
            # doesn't already declare.
            "lora": {
                "hop_limit": 3,
                "use_preset": True,
                "modem_preset": 0,
                "channel_num": 0,
            },
            # A deterministic, always-present POSITION config section --
            # see item 9: "no current fix" is itself a real, common,
            # honest state, not an error, so the simulated snapshot
            # models it directly rather than only ever a fix.
            "position": {
                "gps_enabled": True,
                "gps_update_interval": 120,
                "gps_en_gpio": 34,
                "position_broadcast_smart_enabled": True,
            },
        }
        self._connection_generation = 0
        self._config_snapshot = None
        # Deterministic fake PRIMARY channel state (ADVANCED RADIO
        # CONFIG) -- mirrors a real freshly-flashed radio's own default
        # open "LongFast" primary channel, PSK byte 0x01 (Meshtastic's
        # own "default channel psk" sentinel -- see meshtastic.util.
        # fromPSK("default")), matching base64 "AQ==" exactly.
        self._primary_channel_name = "LongFast"
        self._primary_channel_psk = bytes([1])

    def available_device_paths(self) -> tuple[str, ...]:
        """Return fake ports without asking the host operating system."""
        return SIMULATED_DEVICE_PATHS

    def set_device_path(self, device_path: str) -> None:
        """Switch deterministic fake ports without touching real hardware."""
        if device_path not in SIMULATED_DEVICE_PATHS:
            raise ValueError("Unknown simulated USB device.")
        self.close()
        self.device_path = device_path
        self.info = RadioInfo(
            device_path=device_path,
            node_id=self.info.node_id,
            long_name=self.info.long_name,
            short_name=self.info.short_name,
            firmware_version=self.info.firmware_version,
            known_nodes=self.info.known_nodes,
            channels=self.info.channels,
        )
        self._direct_observations.clear()

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def sent_messages(self) -> tuple[SentMessage, ...]:
        """Return an immutable snapshot of deterministic send history."""
        return tuple(self._sent_messages)

    def connect(self) -> RadioInfo:
        """Bring the simulated radio online and return its local node info."""
        self._stop_event.clear()
        self._closed = False
        self._online = True
        self._activity_reference_time = time.time()
        self._connection_generation += 1
        self._rebuild_config_snapshot()
        return self.info

    def active_node_count(self, now: float | None = None) -> int | None:
        """Return deterministic passive node activity while connected."""
        if not self._online or self._activity_reference_time is None:
            return None
        current_time = time.time() if now is None else now
        local_number = int(self.info.node_id[1:], 16)
        nodes: list[tuple[int, dict[str, object]]] = [
            (
                local_number,
                {
                    "user": {"id": self.info.node_id},
                    "lastHeard": self._activity_reference_time,
                },
            )
        ]
        for index, node in enumerate(SIMULATED_NODES, start=1):
            record: dict[str, object] = {"user": {"id": node.node_id}}
            if node.last_heard_age_seconds is not None:
                age = node.last_heard_age_seconds
                record["lastHeard"] = (
                    self._activity_reference_time - age
                    if isinstance(age, (int, float))
                    else age
                )
            nodes.append((index, record))
        return count_active_other_nodes(
            nodes,
            local_node_number=local_number,
            local_node_id=self.info.node_id,
            now=current_time,
            direct_observations=self._direct_observations,
        )

    def get_node_metadata(self, node_id: str) -> NodeMetadata:
        """Return deterministic node details without radio side effects."""
        normalized = node_id.strip().lower()
        for node in SIMULATED_NODES:
            if node.node_id.lower() == normalized:
                return NodeMetadata(
                    node.node_id,
                    node.long_name,
                    node.short_name,
                    node.hops_away,
                    self._node_last_heard(node),
                    position=node.position,
                )
        return NodeMetadata(node_id.strip())

    def get_link_quality(self, node_id: str) -> LinkObservation | None:
        """Return fixture-defined directly-heard signal quality.

        Mirrors RadioService.get_link_quality's own honesty rule: only
        a node with hops_away == 0 (SIMULATED_NODES' fake analog of
        traversed_hops(...) == 0 -- a genuine zero-relay reception) can
        ever have LINK data, and only when the fixture actually
        supplies rssi/snr. observed_at is anchored to this connection's
        own activity_reference_time (falling back to the current time
        the app is already free to treat as fresh) rather than a fixed
        constant, so --simulate's LINK display ages exactly like real
        last_heard/LAST UPDATE do.
        """
        normalized = node_id.strip().lower()
        for node in SIMULATED_NODES:
            if node.node_id.lower() != normalized:
                continue
            if node.hops_away != 0:
                return None
            if node.rssi is None and node.snr is None:
                return None
            observed_at = (
                self._activity_reference_time
                if self._activity_reference_time is not None
                else time.time()
            )
            return LinkObservation(rssi=node.rssi, snr=node.snr, observed_at=observed_at)
        return None

    def get_known_nodes(self) -> tuple[NodeMetadata, ...]:
        """Return stable fake topology data without hardware or transmissions."""
        if not self._online or self._activity_reference_time is None:
            return ()
        local = NodeMetadata(
            self.info.node_id,
            self.info.long_name,
            self.info.short_name,
            0,
            self._activity_reference_time,
            True,
            position=SIMULATED_LOCAL_POSITION,
        )
        remotes = tuple(
            NodeMetadata(
                node.node_id,
                node.long_name,
                node.short_name,
                node.hops_away,
                self._node_last_heard(node),
                position=node.position,
            )
            for node in SIMULATED_NODES
            if node.node_id.strip().lower() not in self._removed_node_ids
        )
        return (local, *remotes)

    def remove_node(self, node_id: str, *, timeout: float = 15.0) -> NodeRemoveResult:
        """Deterministically remove a remote node from the simulated NodeDB.

        Mirrors RadioService.remove_node: protects the local node (never
        removable), only ever removes the ONE named node, never clears the
        whole NodeDB, and never sends a probe to force rediscovery. A node
        is not re-added until the mesh is heard from again (see
        _hear_node). Offline -> failure ("not_connected"); removing the
        local node -> "local".
        """
        normalized = node_id.strip().lower()
        if not self._online or self._stop_event.is_set():
            return NodeRemoveResult(False, node_id, "not_connected")
        if normalized == self.info.node_id.lower():
            return NodeRemoveResult(False, node_id, "local")
        self._removed_node_ids.add(normalized)
        return NodeRemoveResult(True, node_id, "")

    def _hear_node(self, node_id: str) -> None:
        """Normal mesh traffic (no probe) lets a previously-removed node
        reappear with fresh identity, mirroring real rediscovery."""
        self._removed_node_ids.discard(node_id.strip().lower())

    def _node_last_heard(self, node: SimulatedNode) -> float | None:
        reference = self._activity_reference_time
        age = node.last_heard_age_seconds
        if (
            reference is None
            or not isinstance(age, (int, float))
            or isinstance(age, bool)
            or age < 0
        ):
            return None
        return reference - float(age)

    def set_long_name(self, long_name: str) -> RadioInfo:
        """Update the deterministic local identity while simulated online."""
        normalized = validate_long_name(long_name)
        if not self._online or self._stop_event.is_set():
            raise RadioIdentityError("The simulated radio is not connected.")
        self.info = replace(self.info, long_name=normalized)
        return self.info

    def set_short_name(self, short_name: str) -> RadioInfo:
        """Update the deterministic Short Name without touching hardware."""
        normalized = validate_short_name(short_name)
        if not self._online or self._stop_event.is_set():
            raise RadioIdentityError("The simulated radio is not connected.")
        self.info = replace(self.info, short_name=normalized)
        return self.info

    def hardware_identity(self):
        """Deterministic fake identity, structurally identical to

        RadioService.hardware_identity()'s real HardwareIdentity so
        app.py's RADIO section renders identically under --simulate.
        """
        from radio_capabilities import HardwareIdentity

        node_num = int(self.info.node_id[1:], 16)
        return HardwareIdentity(
            hw_model_raw=39,
            hw_model_name="HELTEC_V3",
            hw_model_source="simulated",
            role_raw=0,
            role_name="CLIENT",
            role_source="simulated",
            firmware_version=self.info.firmware_version,
            firmware_edition=None,
            min_app_version=None,
            node_num=node_num,
            node_id=self.info.node_id,
            device_id="not configured",
            pio_env="simulated",
            macaddr="not configured",
            has_wifi=False,
            has_bluetooth=True,
            has_ethernet=False,
            has_remote_hardware=False,
            has_pkc=False,
            can_shutdown=False,
            excluded_modules=None,
        )

    def read_synced_config_field(self, section: str, field: str):
        """Deterministic fake read, mirroring RadioService's real method."""
        if not self._online:
            return None
        return self._config_sections.get(section, {}).get(field)

    def write_verified_config_field(
        self,
        section: str,
        field: str,
        value,
        *,
        timeout: float = 15.0,
    ) -> ConfigWriteResult:
        """Deterministic fake write -- always succeeds while online, since

        --simulate exists to exercise the UI, not RadioService's real
        verification/failure paths (see tests/test_radio_service.py and
        tests/test_radio_write_readback_probe.py for those).
        """
        if not self._online:
            return ConfigWriteResult(False, value, None, "not_connected")
        section_values = self._config_sections.setdefault(section, {})
        if field not in section_values:
            return ConfigWriteResult(False, value, None, "timeout")
        section_values[field] = value
        self._rebuild_config_snapshot()
        return ConfigWriteResult(True, value, value, "")

    def read_primary_channel_settings(self) -> tuple[str, bytes] | None:
        """Deterministic fake read, mirroring RadioService's real method."""
        if not self._online:
            return None
        return (self._primary_channel_name, self._primary_channel_psk)

    def write_verified_primary_channel(
        self,
        *,
        name: str,
        psk: bytes,
        timeout: float = 15.0,
    ) -> ConfigWriteResult:
        """Deterministic fake write, mirroring RadioService's real method
        (--simulate always succeeds while online -- see
        write_verified_config_field's own docstring for why).
        """
        if not self._online:
            return ConfigWriteResult(False, (name, psk), None, "not_connected")
        self._primary_channel_name = name
        self._primary_channel_psk = psk
        self._rebuild_config_snapshot()
        return ConfigWriteResult(True, (name, psk), (name, psk), "")

    def begin_settings_transaction(self) -> bool:
        """No-op stub (the simulator has no firmware transaction/reboot);

        returns True so apply_radio_config_preset exercises its
        begin/commit path identically under --simulate.
        """
        return self._online

    def commit_settings_transaction(self) -> bool:
        return self._online

    def reread_lora_and_primary_channel(self, *, timeout: float = 8.0) -> bool:
        """No-op: the simulator's _config_sections / primary-channel

        state is always live, so a "fresh re-read" is whatever
        read_synced_config_field / read_primary_channel_settings
        already return.
        """
        return self._online

    def apply_network_config(
        self,
        *,
        use_preset: bool,
        modem_preset: int,
        channel_num: int,
        channel_name: str,
        psk: bytes,
        stage_log=None,
    ) -> RadioApplyResult:
        """Deterministic fire-and-forget NETWORK write -- mirrors

        RadioService.apply_network_config's staging/logging shape
        (--simulate always succeeds while online) so ADVANCED RADIO's
        apply path behaves identically without hardware.
        """
        log = stage_log or (lambda _message: None)
        if not self._online:
            log("connect ERROR not_connected")
            return RadioApplyResult(
                False, "connect", {"connect": ConfigWriteResult(False, None, None, "not_connected")}
            )
        results: dict[str, ConfigWriteResult] = {}
        for stage in ("begin", "lora", "channel", "commit"):
            log(f"{stage} START")
            if stage == "lora":
                lora = self._config_sections.setdefault("lora", {})
                lora["use_preset"] = use_preset
                lora["modem_preset"] = modem_preset
                lora["channel_num"] = channel_num
            elif stage == "channel":
                self._primary_channel_name = channel_name
                self._primary_channel_psk = psk
            results[stage] = ConfigWriteResult(True, None, None, "")
            log(f"{stage} DONE")
        self._rebuild_config_snapshot()
        return RadioApplyResult(True, "", results)

    def config_snapshot(self):
        """The current connection's cached fake RadioConfigurationSnapshot,

        or None -- mirrors RadioService.config_snapshot() exactly (see
        its own docstring): a pure cache read, never touches anything.
        """
        return self._config_snapshot

    def refresh_config_snapshot(self):
        """Deterministic fake refresh -- mirrors RadioService's own."""
        self._rebuild_config_snapshot()
        return self._config_snapshot

    def _rebuild_config_snapshot(self) -> None:
        if not self._online:
            self._config_snapshot = None
            return
        from radio_config_snapshot import LocalPositionSnapshot, RadioConfigurationSnapshot
        from radio_capabilities import ChannelReport, ConfigSectionReport

        channels = tuple(
            ChannelReport(
                index=channel.index,
                name=channel.name,
                role="PRIMARY" if channel.index == 0 else "SECONDARY",
                psk="not configured",
            )
            for channel in self.info.channels
        )
        local_config = tuple(
            ConfigSectionReport(
                category="DEVICE CONFIG" if section == "device" else section.upper(),
                section=section,
                fields={name: str(value) for name, value in fields.items()},
            )
            for section, fields in self._config_sections.items()
        )
        position_fields = self._config_sections.get("position", {})
        position_section = next(
            (report for report in local_config if report.section == "position"), None
        )
        self._config_snapshot = RadioConfigurationSnapshot(
            connection_generation=self._connection_generation,
            node_id=self.info.node_id,
            device_path=self.device_path,
            hardware=self.hardware_identity(),
            local_config=local_config,
            module_config=(),
            channels=channels,
            position=LocalPositionSnapshot(
                gps_capable=position_section is not None,
                config=position_section,
                has_fix=False,
                latitude=None,
                longitude=None,
                altitude=None,
                location_source=None,
                last_position_time=None,
            ),
            generated_at=time.time(),
        )

    def supports_clock_sync(self) -> bool:
        """--simulate always claims support, matching the real SDK's

        current schema (see RadioService.supports_clock_sync) -- never
        used to exercise the unsupported-hardware path, which is
        covered directly against RadioService instead.
        """
        return True

    def sync_clock(self) -> ClockSyncResult:
        """Deterministic fake sync -- always succeeds while online, for

        the same reason write_verified_config_field's fake does: this
        exists to exercise the UI, not RadioService's real ack/rxTime
        corroboration paths (see tests/test_radio_clock_sync.py for
        those, against a fake Meshtastic interface).
        """
        if not self._online:
            return ClockSyncResult(False, 0, None, "not_connected")
        requested_epoch = int(time.time())
        return ClockSyncResult(True, requested_epoch, requested_epoch, "")

    def connection_events(
        self,
        retry_delay: float = 5.0,
        stop_event: Event | None = None,
        poll_interval: float = 0.25,
    ) -> Iterator[RadioEvent]:
        """Connect, emit scripted messages, and wait for simulated events."""
        if retry_delay < 0:
            raise ValueError("retry_delay cannot be negative")
        if self.connect_delay < 0 or self.message_interval < 0:
            raise ValueError("simulation delays cannot be negative")

        stopped = stop_event or Event()
        self._stop_event.clear()
        self._closed = False

        try:
            if self._is_stopped(stopped):
                return

            yield RadioEvent(RadioState.CONNECTING)
            if self._wait(self.connect_delay, stopped):
                return

            yield RadioEvent(RadioState.ONLINE, info=self.connect())

            for message in self.scripted_messages:
                if self._wait(self.message_interval, stopped):
                    return
                self.emit_message(message)

            while not self._is_stopped(stopped):
                try:
                    event = self._state_events.get(timeout=poll_interval)
                except Empty:
                    continue

                self._apply_state(event.state)
                yield event
        finally:
            self.close()

    def add_message_handler(
        self,
        handler: Callable[[ReceivedMessage], None],
    ) -> None:
        if handler not in self._message_handlers:
            self._message_handlers.append(handler)

    def remove_message_handler(
        self,
        handler: Callable[[ReceivedMessage], None],
    ) -> None:
        if handler in self._message_handlers:
            self._message_handlers.remove(handler)

    def send_text(
        self,
        text: str,
        channel_index: int = 0,
        destination_node_id: str | None = None,
        status_handler: Callable[[SendStatus], None] | None = None,
    ) -> SentMessage:
        """Record a send using the next explicitly scripted outcome."""
        message = validate_send_request(
            text,
            channel_index,
            destination_node_id,
        )
        if not self._online or self._stop_event.is_set():
            raise RadioSendError("The simulated radio is not connected.")

        outcome = (
            self.send_outcomes[self._send_count]
            if self._send_count < len(self.send_outcomes)
            else SimulatedSendOutcome.SENT
        )
        self._send_count += 1
        if outcome is SimulatedSendOutcome.FAILED:
            raise RadioSendError("Simulated definite send failure.")

        packet_id = 450000000 + self._send_count
        immediate_state = {
            SimulatedSendOutcome.SENT: DeliveryState.SENT,
            SimulatedSendOutcome.HEARD: DeliveryState.HEARD,
            SimulatedSendOutcome.UNCONFIRMED: DeliveryState.UNCONFIRMED,
        }[outcome]
        sent = SentMessage(
            message.text,
            message.channel_index,
            message.destination_node_id,
            packet_id=packet_id,
            immediate_state=immediate_state,
        )
        self._sent_messages.append(sent)
        return sent

    def send_traceroute(
        self,
        destination_node_id: str,
        status_handler: Callable[[TracerouteStatus], None],
    ) -> int:
        """Record a traceroute using the next explicitly scripted outcome.

        Calls `status_handler` synchronously, before returning, exactly
        like RadioService's own async callback eventually would -- the
        caller (MeshtasticPassApp._start_traceroute) never relies on
        that ordering: its own request_token is captured in the
        status_handler closure BEFORE this method is even called, so a
        synchronous callback here can never race anything (see
        TracerouteStatusReceived's own docstring).
        """
        if not self._online or self._stop_event.is_set():
            raise RadioSendError("The simulated radio is not connected.")
        outcome = (
            self.traceroute_outcomes[self._traceroute_count]
            if self._traceroute_count < len(self.traceroute_outcomes)
            else SimulatedTracerouteOutcome.SUCCEEDED
        )
        self._traceroute_count += 1
        packet_id = 460000000 + self._traceroute_count
        if outcome is SimulatedTracerouteOutcome.NO_RESPONSE:
            return packet_id
        if outcome is SimulatedTracerouteOutcome.FAILED:
            status_handler(
                TracerouteStatus(
                    TracerouteState.FAILED,
                    packet_id,
                    detail="Simulated routing failure.",
                )
            )
            return packet_id
        status_handler(
            TracerouteStatus(
                TracerouteState.SUCCEEDED,
                packet_id,
                result=TracerouteResult(
                    destination_node_id=destination_node_id,
                    forward_route=self.traceroute_forward_route,
                    forward_snr=self.traceroute_forward_snr,
                    return_route=self.traceroute_return_route,
                    return_snr=self.traceroute_return_snr,
                    completed_at=time.time(),
                ),
            )
        )
        return packet_id

    def emit_message(self, message: ReceivedMessage) -> None:
        """Deliver one fake message to every registered consumer."""
        if not self._online or self._stop_event.is_set():
            return

        node_id = message.sender_node_id.strip().lower()
        if node_id and node_id != "unknown":
            self._direct_observations[node_id] = time.time()

        for handler in tuple(self._message_handlers):
            try:
                handler(message)
            except Exception:
                pass

    def simulate_disconnect(self) -> None:
        self._state_events.put(
            RadioEvent(
                RadioState.OFFLINE,
                message="Simulated radio connection was lost.",
            )
        )

    def simulate_error(self, message: str = "Simulated radio error.") -> None:
        self._state_events.put(RadioEvent(RadioState.ERROR, message=message))

    def simulate_reconnect(self) -> None:
        self._state_events.put(RadioEvent(RadioState.CONNECTING))
        self._state_events.put(RadioEvent(RadioState.ONLINE, info=self.info))

    def close(self) -> None:
        """Stop the simulator. Safe to call more than once."""
        self._config_snapshot = None
        self._online = False
        self._closed = True
        self._stop_event.set()

    def _apply_state(self, state: RadioState) -> None:
        if state is RadioState.ONLINE:
            self._online = True
        elif state in (RadioState.OFFLINE, RadioState.ERROR):
            self._online = False

    def _is_stopped(self, external_stop: Event) -> bool:
        return self._stop_event.is_set() or external_stop.is_set()

    def _wait(self, seconds: float, external_stop: Event) -> bool:
        deadline = time.monotonic() + seconds
        while not self._is_stopped(external_stop):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._stop_event.wait(min(0.05, remaining))
        return True
