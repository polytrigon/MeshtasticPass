"""One normalized, cached configuration snapshot for the CONFIG/RADIO UI.

Assembled ENTIRELY from state the Meshtastic Python SDK already holds
after its normal initial connection sync (see radio_capabilities.py,
which every field here is read through) -- building a snapshot never
sends a config request, an admin RPC, or any other new radio/RF
traffic. See RadioService.connect()/config_snapshot()/
refresh_config_snapshot() for the caching lifecycle: built once per
successful connection, invalidated on disconnect/reconnect/device
change, and rebuilt in place after a write this session confirmed.

Secrets (PSKs, passwords, keys) are never read into this model in the
first place -- radio_capabilities.describe_scalar_fields already
redacts them to "configured"/"not configured" at the source, so there
is no secret-shaped value anywhere in a RadioConfigurationSnapshot to
accidentally log or render.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from radio_capabilities import (
    ChannelReport,
    ConfigSectionReport,
    HardwareIdentity,
    channel_reports,
    hardware_identity,
    local_config_sections,
    module_config_sections,
)


@dataclass(frozen=True)
class LocalPositionSnapshot:
    """GPS capability/state -- see item 9: represented cleanly even with

    no current fix (a real, common, honest state -- see has_fix), so a
    future GPS UI needs no further architecture change to consume it.

    `gps_capable` is whether localConfig.position exists at all on
    this installed schema (it always does on 2.7.11, but a schema-
    driven check costs nothing and stays correct if that ever
    changes). The exact enabled/mode FIELD NAME differs across
    firmware/schema generations (older schemas expose a plain
    `gps_enabled` bool; newer ones replace it with a `gps_mode` enum)
    -- `config` below already carries whichever this installed
    package's schema actually declares (see
    radio_capabilities.local_config_sections, which is schema-driven,
    never a hardcoded field-name list), so no assumption about which
    one exists is baked in here.
    """

    gps_capable: bool
    config: ConfigSectionReport | None
    has_fix: bool
    latitude: float | None
    longitude: float | None
    altitude: int | None
    location_source: str | None
    last_position_time: float | None


def _local_node_record(interface: Any) -> dict[str, Any] | None:
    my_info = getattr(interface, "myInfo", None)
    node_num = getattr(my_info, "my_node_num", None)
    nodes_by_number = getattr(interface, "nodesByNum", None)
    if not isinstance(nodes_by_number, dict) or node_num is None:
        return None
    record = nodes_by_number.get(node_num)
    return record if isinstance(record, dict) else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _build_local_position(local_node: Any, interface: Any) -> LocalPositionSnapshot:
    position_section = next(
        (
            section
            for section in local_config_sections(local_node)
            if section.section == "position"
        ),
        None,
    )
    record = _local_node_record(interface)
    position = record.get("position") if isinstance(record, dict) else None
    if not isinstance(position, dict):
        position = {}

    latitude = _optional_float(position.get("latitude"))
    if latitude is None and "latitudeI" in position:
        raw = _optional_float(position.get("latitudeI"))
        latitude = raw * 1e-7 if raw is not None else None
    longitude = _optional_float(position.get("longitude"))
    if longitude is None and "longitudeI" in position:
        raw = _optional_float(position.get("longitudeI"))
        longitude = raw * 1e-7 if raw is not None else None

    last_position_time = None
    for field in ("timestamp", "time"):
        candidate = _optional_float(position.get(field))
        if candidate is not None and candidate > 0:
            last_position_time = candidate
            break

    location_source = position.get("locationSource")
    if not isinstance(location_source, str) or not location_source:
        location_source = None

    has_fix = latitude is not None and longitude is not None
    return LocalPositionSnapshot(
        gps_capable=position_section is not None,
        config=position_section,
        has_fix=has_fix,
        latitude=latitude if has_fix else None,
        longitude=longitude if has_fix else None,
        altitude=_optional_int(position.get("altitude")),
        location_source=location_source,
        last_position_time=last_position_time,
    )


@dataclass(frozen=True)
class RadioConfigurationSnapshot:
    """One immutable, point-in-time view of the connected radio's

    configuration -- see the module docstring for the caching/
    invalidation contract (RadioService owns that; this class is pure
    data). `connection_generation` and `node_id` together key this
    snapshot to one specific connection/session (item 7): a snapshot
    from a stale generation, or a mismatched node_id, must never be
    presented as current -- RadioService.config_snapshot() is the
    only accessor, and it always returns the CURRENT generation's
    snapshot (or None), never a stale one, so callers do not need to
    re-check these fields themselves in the common case; they exist
    for tests and defensive assertions.
    """

    connection_generation: int
    node_id: str | None
    device_path: str
    hardware: HardwareIdentity
    local_config: tuple[ConfigSectionReport, ...]
    module_config: tuple[ConfigSectionReport, ...]
    channels: tuple[ChannelReport, ...]
    position: LocalPositionSnapshot
    generated_at: float


def build_radio_configuration_snapshot(
    interface: Any,
    *,
    device_path: str,
    connection_generation: int,
    generated_at: float,
) -> RadioConfigurationSnapshot:
    """Assemble one snapshot from an already-connected interface's

    already-synced state. Never sends anything -- see the module
    docstring. Safe to call at any time (returns a snapshot with
    "unavailable"/empty fields, never raises, if `interface` is None
    or missing expected attributes -- the same defensive contract
    every radio_capabilities function already holds).
    """
    local_node = getattr(interface, "localNode", None)
    identity = hardware_identity(interface)
    return RadioConfigurationSnapshot(
        connection_generation=connection_generation,
        node_id=identity.node_id,
        device_path=device_path,
        hardware=identity,
        local_config=local_config_sections(local_node),
        module_config=module_config_sections(local_node),
        channels=channel_reports(local_node),
        position=_build_local_position(local_node, interface),
        generated_at=generated_at,
    )
