"""Read-only Meshtastic hardware/config capability audit.

Every function here only READS state an already-connected Meshtastic
interface object already holds from its normal initial configuration
exchange (myInfo, metadata, localNode.localConfig/moduleConfig/
channels, nodesByNum) -- see radio_service.RadioService._open_interface
for where that exchange happens. Nothing in this module sends
configuration, transmits text, requests a remote node's metadata, or
otherwise generates new radio/RF traffic; it is pure introspection of
already-synced Python objects (see radio_capability_probe.py for the
CLI entry point, and RadioService.capability_report()/
hardware_identity() for the two read-only methods that expose this).

Two kinds of information are deliberately kept separate throughout:

- LIVE VALUES, read directly off the connected interface.
- STATIC JUDGMENT (writable?/reboot behavior/hardware-dependent?/safe
  to expose?), which is this codebase's own analysis of the
  Meshtastic protobuf schema -- never derived from the live radio, and
  attached only for the fields explicitly named in the capability-
  audit task. A field with no curated judgment gets "unknown"/
  "ADVANCED" defaults rather than a fabricated opinion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REDACTED_CONFIGURED = "configured"
REDACTED_NOT_CONFIGURED = "not configured"

# Matched as a case-insensitive substring of a protobuf field's own
# declared name -- covers every secret-shaped field this audit found
# in the installed meshtastic package's schema (WiFi/MQTT passwords,
# channel/admin/security keys, lock PINs) without needing an exhaustive
# exact-name list that would silently miss a renamed or newly added
# field in a future meshtastic release. Bytes-typed fields are ALSO
# always redacted regardless of name (see describe_scalar_fields) --
# this list exists for the STRING-typed secrets bytes-detection alone
# would miss.
_SENSITIVE_NAME_MARKERS = (
    "psk",
    "password",
    "pin",
    "public_key",
    "private_key",
    "admin_key",
    "root",  # NetworkConfig.rsyslog root cert / MQTT root cert path
)


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SENSITIVE_NAME_MARKERS)


def _enum_name(field_descriptor: Any, raw_value: Any) -> str:
    """The symbolic enum name for a raw protobuf enum integer, or an

    explicit "UNKNOWN(n)" marker for a value this installed schema
    does not recognize -- never a raised exception. A future firmware
    reporting a newer enum value than this app's installed meshtastic
    package knows about must render as an honest "unknown", not crash
    the whole probe.
    """
    try:
        return field_descriptor.enum_type.values_by_number[raw_value].name
    except (KeyError, AttributeError, TypeError):
        return f"UNKNOWN({raw_value!r})"


def role_choices() -> tuple[tuple[str, int], ...]:
    """Every device.role value the installed protobuf schema declares,

    as (friendly_label, enum_number) pairs -- discovered from the
    schema itself, never a hardcoded role list, so a future meshtastic
    release adding a new role appears automatically with no code
    change here. Order matches the schema's own declaration order;
    this never ranks or recommends any particular role. Returns an
    empty tuple if the installed package or this exact field is
    unavailable for any reason, rather than raising -- the caller
    (RoleSelector) treats that the same as any other unsupported field.
    """
    try:
        from meshtastic.protobuf import config_pb2
    except ImportError:
        return ()
    role_field = config_pb2.Config.DeviceConfig.DESCRIPTOR.fields_by_name.get("role")
    if role_field is None or role_field.enum_type is None:
        return ()
    return tuple(
        (value.name.replace("_", " "), value.number)
        for value in role_field.enum_type.values
    )


def modem_preset_choices() -> tuple[tuple[str, str], ...]:
    """Every Config.LoRaConfig.ModemPreset value the installed protobuf

    schema declares, as (friendly_label, enum_name) pairs -- discovered
    from the schema itself (mirrors role_choices exactly), never a
    hardcoded preset list. The enum NAME (e.g. "MEDIUM_SLOW"), not its
    number, is the value here and the one persisted in
    RadioConfigPreset.modem_preset -- names are stable across a
    protobuf schema's own internal renumbering in a way raw numbers are
    not, and read naturally in a saved-config file. Converted to the
    actual enum number only at the radio-write boundary (see
    radio_service.apply_radio_config_preset). Returns an empty tuple if
    the installed package or this exact field is unavailable, rather
    than raising.
    """
    try:
        from meshtastic.protobuf import config_pb2
    except ImportError:
        return ()
    preset_field = config_pb2.Config.LoRaConfig.DESCRIPTOR.fields_by_name.get(
        "modem_preset"
    )
    if preset_field is None or preset_field.enum_type is None:
        return ()
    return tuple(
        (value.name.replace("_", " "), value.name)
        for value in preset_field.enum_type.values
    )


def modem_preset_enum_name(raw_value: Any) -> str | None:
    """The raw LoRaConfig.modem_preset enum NUMBER's own symbolic NAME

    (e.g. "MEDIUM_SLOW", read via RadioService.read_synced_config_field
    ("lora", "modem_preset")) -- None if this installed schema does not
    recognize it, never a raised exception.
    """
    try:
        from meshtastic.protobuf import config_pb2

        return config_pb2.Config.LoRaConfig.ModemPreset.Name(raw_value)
    except Exception:
        return None


def modem_preset_friendly_label(raw_value: Any) -> str:
    """Convert a raw modem_preset enum NUMBER into the same friendly

    label modem_preset_choices() produces for it -- "UNKNOWN(n)" for a
    value this installed schema does not recognize, matching
    _enum_name's own never-raise convention.
    """
    name = modem_preset_enum_name(raw_value)
    if name is None:
        return f"UNKNOWN({raw_value!r})"
    return name.replace("_", " ")


def describe_scalar_fields(message: Any) -> dict[str, str]:
    """Flatten one protobuf message's own scalar fields (never

    descending into embedded sub-messages -- callers walk those
    explicitly, one section at a time) into name -> printable value.

    Bytes-typed fields and any field whose name matches a known-
    sensitive marker are reported only as "configured"/"not
    configured" (see module docstring) -- their actual content is
    never read into the result. Enum fields render as their symbolic
    name (or an explicit UNKNOWN(n) marker for an unrecognized value).
    A message with no DESCRIPTOR (None, a fake test double missing
    protobuf shape, or any other unexpected input) contributes an
    empty dict rather than raising -- this is a diagnostic, not a
    strict validator, and a genuinely absent/unsynced config section
    must never crash the whole report.
    """
    if message is None:
        return {}
    descriptor = getattr(message, "DESCRIPTOR", None)
    if descriptor is None:
        return {}
    result: dict[str, str] = {}
    for field in descriptor.fields:
        if field.type == field.TYPE_MESSAGE:
            continue
        try:
            raw = getattr(message, field.name)
        except Exception:
            continue
        if field.type == field.TYPE_BYTES or _is_sensitive_name(field.name):
            result[field.name] = (
                REDACTED_CONFIGURED if raw else REDACTED_NOT_CONFIGURED
            )
        elif field.type == field.TYPE_ENUM:
            result[field.name] = _enum_name(field, raw)
        else:
            result[field.name] = str(raw)
    return result


@dataclass(frozen=True)
class ConfigSectionReport:
    """One LocalConfig/LocalModuleConfig section's own scalar fields."""

    category: str
    section: str
    fields: dict[str, str]


def local_config_sections(local_node: Any) -> tuple[ConfigSectionReport, ...]:
    """Every section of localNode.localConfig this installed package's

    LocalConfig schema declares -- device, position, power, network,
    display, lora, bluetooth, security, and whatever else a future
    meshtastic release adds, discovered from the schema itself rather
    than a hardcoded list (see item 18 of the capability-audit task:
    capability/config-driven, not hardcoded-per-model). A radio whose
    initial sync has not populated localConfig yet (or a fake test
    double missing the attribute entirely) yields no sections, never
    a crash.
    """
    local_config = getattr(local_node, "localConfig", None)
    if local_config is None:
        return ()
    descriptor = getattr(local_config, "DESCRIPTOR", None)
    if descriptor is None:
        return ()
    sections = []
    for field in descriptor.fields:
        if field.type != field.TYPE_MESSAGE:
            continue
        try:
            section_message = getattr(local_config, field.name)
        except Exception:
            continue
        sections.append(
            ConfigSectionReport(
                category="DEVICE CONFIG" if field.name == "device" else field.name.upper(),
                section=field.name,
                fields=describe_scalar_fields(section_message),
            )
        )
    return tuple(sections)


def module_config_sections(local_node: Any) -> tuple[ConfigSectionReport, ...]:
    """Every section of localNode.moduleConfig this installed package's

    LocalModuleConfig schema declares (mqtt, serial, telemetry, ...) --
    discovered from the schema itself, never a hardcoded list (see
    local_config_sections' own docstring for why).
    """
    module_config = getattr(local_node, "moduleConfig", None)
    if module_config is None:
        return ()
    descriptor = getattr(module_config, "DESCRIPTOR", None)
    if descriptor is None:
        return ()
    sections = []
    for field in descriptor.fields:
        if field.type != field.TYPE_MESSAGE:
            continue
        try:
            section_message = getattr(module_config, field.name)
        except Exception:
            continue
        sections.append(
            ConfigSectionReport(
                category="MODULE CONFIG",
                section=field.name,
                fields=describe_scalar_fields(section_message),
            )
        )
    return tuple(sections)


@dataclass(frozen=True)
class HardwareIdentity:
    """Authoritative hardware/firmware identity -- every field here

    comes directly from the radio/Meshtastic API itself (DeviceMetadata,
    exchanged as part of the normal initial sync, and/or the local
    node's own User record already present in the synced NodeDB) --
    NEVER inferred from the USB device path, serial device name, or
    any other host-side heuristic. `hw_model_source`/`role_source`
    name exactly which of those two independent sources produced the
    value, or "unavailable" if the connected firmware supplied
    neither.
    """

    hw_model_raw: int | None
    hw_model_name: str | None
    hw_model_source: str
    role_raw: int | None
    role_name: str | None
    role_source: str
    firmware_version: str | None
    firmware_edition: str | None
    min_app_version: int | None
    node_num: int | None
    node_id: str | None
    device_id: str | None
    pio_env: str | None
    macaddr: str
    has_wifi: bool | None
    has_bluetooth: bool | None
    has_ethernet: bool | None
    has_remote_hardware: bool | None
    has_pkc: bool | None
    can_shutdown: bool | None
    excluded_modules: str | None


def hardware_identity(interface: Any) -> HardwareIdentity:
    """Report what the connected radio authoritatively says about its

    own hardware -- see the module docstring's "two kinds of
    information" note: every field here is a live read, never a
    heuristic. macaddr is reported as "configured"/"not configured"
    like any other device-identity byte string (see
    describe_scalar_fields) -- a MAC address is still a persistent
    device identifier worth knowing IS present without printing it
    verbatim in a diagnostic dump.
    """
    metadata = getattr(interface, "metadata", None)
    my_info = getattr(interface, "myInfo", None)
    node_num = getattr(my_info, "my_node_num", None)
    if isinstance(node_num, bool) or not isinstance(node_num, int):
        node_num = None

    hw_model_raw: int | None = None
    hw_model_name: str | None = None
    hw_model_source = "unavailable"
    role_raw: int | None = None
    role_name: str | None = None
    role_source = "unavailable"
    firmware_version: str | None = None
    has_wifi = has_bluetooth = has_ethernet = None
    has_remote_hardware = has_pkc = can_shutdown = None
    excluded_modules: str | None = None

    metadata_descriptor = getattr(metadata, "DESCRIPTOR", None)
    if metadata is not None and metadata_descriptor is not None:
        fields_by_name = metadata_descriptor.fields_by_name
        if "hw_model" in fields_by_name:
            raw = getattr(metadata, "hw_model", None)
            if raw:
                hw_model_raw = raw
                hw_model_name = _enum_name(fields_by_name["hw_model"], raw)
                hw_model_source = "DeviceMetadata.hw_model (device metadata exchange)"
        if "role" in fields_by_name:
            raw_role = getattr(metadata, "role", None)
            if raw_role is not None:
                role_raw = raw_role
                role_name = _enum_name(fields_by_name["role"], raw_role)
                role_source = "DeviceMetadata.role (device metadata exchange)"
        firmware_version = getattr(metadata, "firmware_version", "") or None
        has_wifi = bool(getattr(metadata, "hasWifi", False))
        has_bluetooth = bool(getattr(metadata, "hasBluetooth", False))
        has_ethernet = bool(getattr(metadata, "hasEthernet", False))
        has_remote_hardware = bool(getattr(metadata, "hasRemoteHardware", False))
        has_pkc = bool(getattr(metadata, "hasPKC", False))
        can_shutdown = bool(getattr(metadata, "canShutdown", False))
        excluded_modules = str(getattr(metadata, "excluded_modules", "")) or None

    # A SEPARATE authoritative source: the local node's own NodeInfo/
    # User record in the synced NodeDB (mesh_pb2.User.hw_model/role),
    # normalized by the SDK into a camelCase dict exactly like every
    # other node's record (see radio_service.py's own
    # _user_from_record) -- only consulted when DeviceMetadata didn't
    # already answer, never overriding it.
    nodes_by_number = getattr(interface, "nodesByNum", None)
    local_record = None
    if isinstance(nodes_by_number, dict) and node_num is not None:
        local_record = nodes_by_number.get(node_num)
    local_user = local_record.get("user") if isinstance(local_record, dict) else None
    if isinstance(local_user, dict):
        if hw_model_name is None:
            raw_name = local_user.get("hwModel")
            if isinstance(raw_name, str) and raw_name and raw_name != "UNSET":
                hw_model_name = raw_name
                hw_model_source = "NodeInfo.user.hwModel (local node's own NodeDB record)"
        if role_name is None:
            raw_role_name = local_user.get("role")
            if isinstance(raw_role_name, str) and raw_role_name:
                role_name = raw_role_name
                role_source = "NodeInfo.user.role (local node's own NodeDB record)"

    node_id = local_user.get("id") if isinstance(local_user, dict) else None
    if not isinstance(node_id, str) or not node_id:
        node_id = f"!{node_num:08x}" if isinstance(node_num, int) else None
    macaddr_raw = local_user.get("macaddr") if isinstance(local_user, dict) else None
    macaddr = REDACTED_CONFIGURED if macaddr_raw else REDACTED_NOT_CONFIGURED

    return HardwareIdentity(
        hw_model_raw=hw_model_raw,
        hw_model_name=hw_model_name,
        hw_model_source=hw_model_source,
        role_raw=role_raw,
        role_name=role_name,
        role_source=role_source,
        firmware_version=firmware_version,
        firmware_edition=_optional_enum(my_info, "firmware_edition"),
        min_app_version=_optional_int(my_info, "min_app_version"),
        node_num=node_num,
        node_id=node_id,
        device_id=(REDACTED_CONFIGURED if _optional_bytes(my_info, "device_id") else REDACTED_NOT_CONFIGURED)
        if my_info is not None
        else None,
        pio_env=_optional_str(my_info, "pio_env"),
        macaddr=macaddr,
        has_wifi=has_wifi,
        has_bluetooth=has_bluetooth,
        has_ethernet=has_ethernet,
        has_remote_hardware=has_remote_hardware,
        has_pkc=has_pkc,
        can_shutdown=can_shutdown,
        excluded_modules=excluded_modules,
    )


def format_hw_model_name(hw_model_name: str | None) -> str:
    """Human-readable form of an authoritative hw_model enum name, e.g.

    "HELTEC_V3" -> "HELTEC V3". Purely cosmetic (underscores to spaces)
    -- never infers or guesses a model; None stays an explicit "—" so a
    disconnected/unavailable radio can never be mistaken for one that
    reported an empty model name.
    """
    if not hw_model_name:
        return "—"
    return hw_model_name.replace("_", " ")


def _optional_str(message: Any, name: str) -> str | None:
    if message is None:
        return None
    value = getattr(message, name, None)
    return value if isinstance(value, str) and value else None


def _optional_int(message: Any, name: str) -> int | None:
    if message is None:
        return None
    value = getattr(message, name, None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_bytes(message: Any, name: str) -> bytes | None:
    if message is None:
        return None
    value = getattr(message, name, None)
    return value if isinstance(value, (bytes, bytearray)) and value else None


def _optional_enum(message: Any, name: str) -> str | None:
    if message is None:
        return None
    descriptor = getattr(message, "DESCRIPTOR", None)
    if descriptor is None or name not in descriptor.fields_by_name:
        return None
    raw = getattr(message, name, None)
    if raw is None:
        return None
    return _enum_name(descriptor.fields_by_name[name], raw)


@dataclass(frozen=True)
class ChannelReport:
    """One configured channel slot, PSK reported only as configured/not."""

    index: int
    name: str
    role: str
    psk: str


def channel_reports(local_node: Any) -> tuple[ChannelReport, ...]:
    """Every channel slot the SDK already holds from the initial sync --

    including a DISABLED slot (unlike RadioService._read_channel_info,
    which the app's own CHAT channel selector deliberately filters to
    enabled channels only; this audit reports the raw slot set instead,
    since "what channel slots exist at all" is itself a capability
    question). PSK content is never read -- only whether the slot's
    settings carry a nonempty one.
    """
    channels = getattr(local_node, "channels", None)
    if not channels:
        return ()
    reports = []
    for fallback_index, channel in enumerate(channels):
        descriptor = getattr(channel, "DESCRIPTOR", None)
        role_field = descriptor.fields_by_name.get("role") if descriptor else None
        raw_role = getattr(channel, "role", None)
        role_name = (
            _enum_name(role_field, raw_role) if role_field is not None else "UNKNOWN"
        )
        settings = getattr(channel, "settings", None)
        raw_name = getattr(settings, "name", "") if settings is not None else ""
        raw_psk = getattr(settings, "psk", b"") if settings is not None else b""
        index = getattr(channel, "index", fallback_index)
        reports.append(
            ChannelReport(
                index=index if isinstance(index, int) else fallback_index,
                name=raw_name if isinstance(raw_name, str) else "",
                role=role_name,
                psk=REDACTED_CONFIGURED if raw_psk else REDACTED_NOT_CONFIGURED,
            )
        )
    return tuple(reports)


@dataclass(frozen=True)
class RemoteNodeSummary:
    """One remote NodeDB entry's passively-known state -- REMOTE

    information, never local radio configuration (see item 13 of the
    capability-audit task's explicit LOCAL-vs-REMOTE distinction).
    """

    node_id: str
    long_name: str | None
    short_name: str | None
    hw_model: str | None
    firmware_version: str | None
    role: str | None
    hops_away: int | None
    last_heard: int | None
    snr: float | None
    via_mqtt: bool | None
    is_favorite: bool | None
    public_key: str


def remote_node_summaries(interface: Any) -> tuple[RemoteNodeSummary, ...]:
    """Passive NodeDB entries only -- never a new radio request; this

    is exactly the same nodesByNum snapshot RadioService.
    get_known_nodes() already reads for MESH, just with more of the
    already-synced fields surfaced (SNR, viaMqtt, favorite, public key
    presence) since this is a capability-inventory audit, not the
    application's own bounded working-set view.
    """
    nodes_by_number = getattr(interface, "nodesByNum", None)
    if not isinstance(nodes_by_number, dict):
        return ()
    my_info = getattr(interface, "myInfo", None)
    local_number = getattr(my_info, "my_node_num", None)
    summaries = []
    try:
        items = tuple(nodes_by_number.items())
    except RuntimeError:
        return ()
    for number, record in items:
        if not isinstance(record, dict) or number == local_number:
            continue
        user = record.get("user") if isinstance(record.get("user"), dict) else {}
        node_id = user.get("id") or (
            f"!{number:08x}" if isinstance(number, int) else "unknown"
        )
        public_key = user.get("publicKey")
        summaries.append(
            RemoteNodeSummary(
                node_id=node_id,
                long_name=user.get("longName"),
                short_name=user.get("shortName"),
                hw_model=user.get("hwModel"),
                firmware_version=record.get("firmwareVersion"),
                role=user.get("role"),
                hops_away=record.get("hopsAway"),
                last_heard=record.get("lastHeard"),
                snr=record.get("snr"),
                via_mqtt=record.get("viaMqtt"),
                is_favorite=record.get("isFavorite"),
                public_key=REDACTED_CONFIGURED if public_key else REDACTED_NOT_CONFIGURED,
            )
        )
    return tuple(summaries)


# ---- Section-level judgment classification (this audit's own analysis) ----
#
# NEVER derived from the live radio -- see the module docstring. Keyed by
# (section, field); a field with no entry here falls back to the
# conservative defaults in classify_field() below rather than a fabricated
# opinion.

WRITABLE_UNKNOWN = "unknown"
REBOOT_UNKNOWN = "unknown"
REBOOT_NONE = "applies immediately, no reboot indicated"
REBOOT_LIKELY = "likely requires interface reconnect/reboot"
REBOOT_DOCUMENTED = "SDK/CLI code documents a reboot for this write"
SAFE_HIGH_VALUE = "SAFE/HIGH-VALUE"
SAFE_ADVANCED = "ADVANCED"
SAFE_DANGEROUS = "DANGEROUS"
SAFE_DO_NOT_EXPOSE = "DO-NOT-EXPOSE"

_FIELD_CLASSIFICATION: dict[tuple[str, str], dict[str, str]] = {
    ("display", "screen_on_secs"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_NONE,
        "hardware_dependent": "no",
        "safe": SAFE_HIGH_VALUE,
        "notes": (
            "A screen-ON-DURATION timeout, not a persistent display-"
            "disable flag or an immediate runtime command -- see item 5's "
            "A/B/C/D distinction. Setting this very low is the closest "
            "thing to \"turn the OLED off\" this schema exposes."
        ),
    },
    ("display", "oled"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_LIKELY,
        "hardware_dependent": "yes",
        "safe": SAFE_DANGEROUS,
        "notes": "OLED CONTROLLER CHIP type (SSD1306/SH1106/...), not on/off.",
    },
    ("display", "flip_screen"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "yes",
        "safe": SAFE_HIGH_VALUE,
    },
    ("display", "compass_north_top"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_NONE,
        "hardware_dependent": "no",
        "safe": SAFE_HIGH_VALUE,
    },
    ("display", "units"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_NONE,
        "hardware_dependent": "no",
        "safe": SAFE_HIGH_VALUE,
    },
    ("display", "wake_on_tap_or_motion"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "yes (needs an accelerometer)",
        "safe": SAFE_ADVANCED,
    },
    ("device", "role"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_LIKELY,
        "hardware_dependent": "no",
        "safe": SAFE_DANGEROUS,
        "notes": "Changes mesh behavior radio-wide (e.g. CLIENT vs ROUTER).",
    },
    ("device", "rebroadcast_mode"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "no",
        "safe": SAFE_ADVANCED,
    },
    ("device", "node_info_broadcast_secs"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_NONE,
        "hardware_dependent": "no",
        "safe": SAFE_ADVANCED,
    },
    ("device", "buzzer_mode"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "yes (needs a buzzer)",
        "safe": SAFE_HIGH_VALUE,
    },
    ("device", "tzdef"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "no",
        "safe": SAFE_HIGH_VALUE,
    },
    ("lora", "region"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_DOCUMENTED,
        "hardware_dependent": "yes (legal/RF-compliance dependent)",
        "safe": SAFE_DANGEROUS,
        "notes": "Wrong region can violate local RF regulations.",
    },
    ("lora", "modem_preset"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_LIKELY,
        "hardware_dependent": "no",
        "safe": SAFE_DANGEROUS,
        "notes": "Breaks compatibility with any peer not using the same preset.",
    },
    ("lora", "hop_limit"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "no",
        "safe": SAFE_ADVANCED,
    },
    ("lora", "tx_power"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "yes (PA/antenna dependent)",
        "safe": SAFE_DANGEROUS,
        "notes": "Can violate local RF power limits or damage a mismatched PA.",
    },
    ("lora", "override_frequency"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_LIKELY,
        "hardware_dependent": "yes",
        "safe": SAFE_DO_NOT_EXPOSE,
        "notes": "Can take the radio off the legal/interoperable band entirely.",
    },
    ("lora", "bandwidth"): {"writable": "yes", "reboot": REBOOT_LIKELY, "hardware_dependent": "yes", "safe": SAFE_DANGEROUS},
    ("lora", "spread_factor"): {"writable": "yes", "reboot": REBOOT_LIKELY, "hardware_dependent": "yes", "safe": SAFE_DANGEROUS},
    ("lora", "coding_rate"): {"writable": "yes", "reboot": REBOOT_LIKELY, "hardware_dependent": "yes", "safe": SAFE_DANGEROUS},
    ("lora", "ignore_mqtt"): {"writable": "yes", "reboot": REBOOT_NONE, "hardware_dependent": "no", "safe": SAFE_ADVANCED},
    ("lora", "config_ok_to_mqtt"): {"writable": "yes", "reboot": REBOOT_NONE, "hardware_dependent": "no", "safe": SAFE_ADVANCED},
    ("position", "gps_enabled"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "yes -- schema presence never implies GPS hardware is present",
        "safe": SAFE_ADVANCED,
    },
    ("position", "gps_mode"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "yes",
        "safe": SAFE_HIGH_VALUE,
        "notes": "Includes a NOT_PRESENT value -- the closest this schema comes to firmware self-reporting no GPS hardware.",
    },
    ("position", "position_broadcast_secs"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_NONE,
        "hardware_dependent": "no",
        "safe": SAFE_HIGH_VALUE,
    },
    ("position", "fixed_position"): {
        "writable": "yes (admin RPC: set_fixed_position/remove_fixed_position)",
        "reboot": REBOOT_NONE,
        "hardware_dependent": "no",
        "safe": SAFE_ADVANCED,
    },
    ("power", "is_power_saving"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_LIKELY,
        "hardware_dependent": "yes",
        "safe": SAFE_ADVANCED,
        "notes": "Can make the radio unresponsive/hard to reach over serial while asleep.",
    },
    ("power", "sds_secs"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_LIKELY,
        "hardware_dependent": "yes",
        "safe": SAFE_DANGEROUS,
        "notes": "Deep sleep timer -- can make the radio unreachable until it wakes.",
    },
    ("power", "ls_secs"): {"writable": "yes", "reboot": REBOOT_UNKNOWN, "hardware_dependent": "yes", "safe": SAFE_DANGEROUS},
    ("power", "wait_bluetooth_secs"): {"writable": "yes", "reboot": REBOOT_NONE, "hardware_dependent": "yes (needs Bluetooth)", "safe": SAFE_ADVANCED},
    ("bluetooth", "enabled"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_LIKELY,
        "hardware_dependent": "yes",
        "safe": SAFE_ADVANCED,
    },
    ("bluetooth", "mode"): {"writable": "yes", "reboot": REBOOT_UNKNOWN, "hardware_dependent": "yes", "safe": SAFE_ADVANCED},
    ("bluetooth", "fixed_pin"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "yes",
        "safe": SAFE_DO_NOT_EXPOSE,
        "notes": "A pairing PIN -- treat as sensitive even though the schema types it as a plain integer, not bytes.",
    },
    ("network", "wifi_enabled"): {"writable": "yes", "reboot": REBOOT_LIKELY, "hardware_dependent": "yes", "safe": SAFE_ADVANCED},
    ("network", "wifi_ssid"): {"writable": "yes", "reboot": REBOOT_LIKELY, "hardware_dependent": "yes", "safe": SAFE_ADVANCED},
    ("network", "wifi_psk"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_LIKELY,
        "hardware_dependent": "yes",
        "safe": SAFE_DO_NOT_EXPOSE,
        "notes": "Never display; write-only field in any future UI.",
    },
    ("network", "ntp_server"): {"writable": "yes", "reboot": REBOOT_NONE, "hardware_dependent": "no", "safe": SAFE_HIGH_VALUE},
    ("security", "public_key"): {
        "writable": "no (device-generated identity key)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "no",
        "safe": SAFE_DO_NOT_EXPOSE,
        "notes": "Reported presence-only; never the key bytes themselves.",
    },
    ("security", "admin_channel_enabled"): {"writable": "yes", "reboot": REBOOT_UNKNOWN, "hardware_dependent": "no", "safe": SAFE_DANGEROUS},
    ("security", "debug_log_api_enabled"): {"writable": "yes", "reboot": REBOOT_UNKNOWN, "hardware_dependent": "no", "safe": SAFE_ADVANCED},
    ("mqtt", "enabled"): {"writable": "yes", "reboot": REBOOT_NONE, "hardware_dependent": "no (needs WiFi or a MQTT-capable link)", "safe": SAFE_ADVANCED},
    ("mqtt", "password"): {
        "writable": "yes (config write)",
        "reboot": REBOOT_UNKNOWN,
        "hardware_dependent": "no",
        "safe": SAFE_DO_NOT_EXPOSE,
        "notes": "Never display; write-only field in any future UI.",
    },
    ("telemetry", "device_update_interval"): {"writable": "yes", "reboot": REBOOT_NONE, "hardware_dependent": "no", "safe": SAFE_HIGH_VALUE},
    ("canned_message", "enabled"): {"writable": "yes", "reboot": REBOOT_NONE, "hardware_dependent": "yes (needs an input device)", "safe": SAFE_ADVANCED},
}


def classify_field(section: str, field_name: str) -> dict[str, str]:
    """This audit's own writable/reboot/hardware-dependent/safety

    judgment for one config field -- see the module-level
    _FIELD_CLASSIFICATION table and its docstring note above for why
    this is never derived from the live radio. Falls back to
    conservative "unknown"/ADVANCED defaults for any field this task's
    payload did not specifically discuss, rather than a fabricated
    opinion.
    """
    found = _FIELD_CLASSIFICATION.get((section, field_name))
    if found is None:
        return {
            "writable": WRITABLE_UNKNOWN,
            "reboot": REBOOT_UNKNOWN,
            "hardware_dependent": WRITABLE_UNKNOWN,
            "safe": SAFE_ADVANCED,
            "notes": "not individually classified by this audit",
        }
    return {
        "writable": found.get("writable", WRITABLE_UNKNOWN),
        "reboot": found.get("reboot", REBOOT_UNKNOWN),
        "hardware_dependent": found.get("hardware_dependent", WRITABLE_UNKNOWN),
        "safe": found.get("safe", SAFE_ADVANCED),
        "notes": found.get("notes", ""),
    }


@dataclass(frozen=True)
class CapabilityRow:
    """One row of the sanitized capability matrix (see item 19 of the

    capability-audit task): a live value plus this audit's own static
    judgment columns.
    """

    category: str
    field: str
    source: str
    value: str
    writable: str
    reboot: str
    hardware_dependent: str
    safe_to_expose: str
    notes: str


def build_capability_matrix(interface: Any) -> tuple[CapabilityRow, ...]:
    """The full sanitized capability matrix for an already-connected

    interface -- local config sections, module config sections,
    channels, and hardware identity, each row combining a live value
    with this audit's own judgment (see classify_field). Pure read;
    see the module docstring for the full no-write/no-RF guarantee.
    """
    rows: list[CapabilityRow] = []
    local_node = getattr(interface, "localNode", None)

    for section_report in local_config_sections(local_node):
        for field_name, value in section_report.fields.items():
            judgment = classify_field(section_report.section, field_name)
            rows.append(
                CapabilityRow(
                    category=f"DEVICE CONFIG: {section_report.section}",
                    field=field_name,
                    source=f"localConfig.{section_report.section}.{field_name}",
                    value=value,
                    writable=judgment["writable"],
                    reboot=judgment["reboot"],
                    hardware_dependent=judgment["hardware_dependent"],
                    safe_to_expose=judgment["safe"],
                    notes=judgment["notes"],
                )
            )

    for section_report in module_config_sections(local_node):
        for field_name, value in section_report.fields.items():
            judgment = classify_field(section_report.section, field_name)
            rows.append(
                CapabilityRow(
                    category=f"MODULE CONFIG: {section_report.section}",
                    field=field_name,
                    source=f"moduleConfig.{section_report.section}.{field_name}",
                    value=value,
                    writable=judgment["writable"],
                    reboot=judgment["reboot"],
                    hardware_dependent=judgment["hardware_dependent"],
                    safe_to_expose=judgment["safe"],
                    notes=judgment["notes"],
                )
            )

    for channel in channel_reports(local_node):
        rows.append(
            CapabilityRow(
                category="CHANNEL",
                field=f"channel[{channel.index}]",
                source=f"localNode.channels[{channel.index}]",
                value=f"name={channel.name!r} role={channel.role} psk={channel.psk}",
                writable="yes (admin RPC: set_channel)",
                reboot=REBOOT_NONE,
                hardware_dependent="no",
                safe_to_expose=SAFE_HIGH_VALUE,
                notes="PSK reported as configured/not configured only.",
            )
        )

    identity = hardware_identity(interface)
    identity_fields = (
        ("hw_model", identity.hw_model_name, identity.hw_model_source, SAFE_HIGH_VALUE),
        ("role", identity.role_name, identity.role_source, SAFE_HIGH_VALUE),
        ("firmware_version", identity.firmware_version, "DeviceMetadata.firmware_version", SAFE_HIGH_VALUE),
        ("node_id", identity.node_id, "NodeInfo.user.id / MyNodeInfo.my_node_num", SAFE_HIGH_VALUE),
        ("macaddr", identity.macaddr, "NodeInfo.user.macaddr", SAFE_DO_NOT_EXPOSE),
    )
    for field_name, value, source, safety in identity_fields:
        rows.append(
            CapabilityRow(
                category="HARDWARE IDENTITY",
                field=field_name,
                source=source,
                value="unavailable" if value is None else str(value),
                writable="no (device-reported)",
                reboot=REBOOT_NONE,
                hardware_dependent="yes",
                safe_to_expose=safety,
                notes="",
            )
        )

    return tuple(rows)


def format_capability_matrix(rows: tuple[CapabilityRow, ...]) -> str:
    """Render the capability matrix as a plain, fixed-width text table.

    Only SOURCE (a fully-qualified dotted path, rarely useful past a
    modest width) is capped; every other column -- including NOTES,
    which carries this audit's actual judgment prose -- renders in
    full rather than truncating mid-word.
    """
    headers = (
        "CATEGORY", "FIELD", "SOURCE", "VALUE", "WRITABLE",
        "REBOOT", "HW-DEP", "SAFE", "NOTES",
    )
    table_rows = [headers] + [
        (
            row.category, row.field, row.source, row.value, row.writable,
            row.reboot, row.hardware_dependent, row.safe_to_expose, row.notes,
        )
        for row in rows
    ]
    caps = {2: 48}  # SOURCE column index -> max width
    widths = [
        min(max(len(str(row[column])) for row in table_rows), caps.get(column, 200))
        for column in range(len(headers))
    ]

    def format_row(values: tuple[str, ...]) -> str:
        return " | ".join(
            str(value)[: widths[index]].ljust(widths[index])
            for index, value in enumerate(values)
        )

    lines = [format_row(headers), "-+-".join("-" * width for width in widths)]
    lines.extend(format_row(row) for row in table_rows[1:])
    return "\n".join(lines)
