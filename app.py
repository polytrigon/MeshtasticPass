#!/usr/bin/env python3
"""Keyboard-first terminal application for MeshtasticPass."""

from __future__ import annotations

import argparse
import base64
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from threading import Thread
from time import monotonic, sleep, time
from typing import Any, Callable

from rich.cells import cell_len
from rich.color import Color
from rich.segment import Segment, Segments
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import (
    Container,
    Horizontal,
    ScrollableContainer,
    Vertical,
    VerticalScroll,
)
from textual.events import Blur, Click, Focus, Key
from textual.message import Message
from textual.scrollbar import ScrollBarRender
from textual.timer import Timer
from textual.widgets import ContentSwitcher, Input, Static
from textual.widget import Widget

from app_controller import (
    ChatEntry,
    RadioMonitor,
    create_radio_service,
    outgoing_chat_entry,
    received_chat_entry,
    stored_chat_entry,
)
from app_settings import AppSettings, COLOR_CHOICES, FONT_SIZE_CHOICES, RadioConfigPreset
from channel_psk import generate_private_psk, normalize_private_psk
from chat_store import (
    DEFAULT_HISTORY_LIMIT,
    OLDER_HISTORY_PAGE_SIZE,
    ChatStore,
    ChatStoreError,
)
from geo import format_distance_miles
from host_timezone import detect_host_timezone
from grapheme_text import (
    install_flag_pair_protection,
    terminal_safe_text,
    truncate_to_cells,
)
from keyboard_dropdown import DropdownOption, KeyboardDropdown
from mesh_state import (
    MeshActivityTier,
    MeshNodeState,
    _clean_text,
    build_mesh_working_set,
    format_mesh_node_bar_fields,
    format_mesh_node_bar_line,
)
from mesh_topology import (
    DEFAULT_MAX_GRID_RADIUS,
    PositionedNode,
    RelayStage,
    TopologyLayout,
    assign_grid_slots,
    build_relay_stages,
    directional_target,
    mesh_board_marker_label,
    place_within_bounds,
    project_to_viewport,
    route_chain_avoiding,
)
from node_activity import is_node_active
from radio_capabilities import (
    format_hw_model_name,
    modem_preset_choices,
    role_choices,
)
from radio_service import (
    ChannelInfo,
    ClockSyncResult,
    ConfigWriteResult,
    PrivateChannelApplyResult,
    DeliveryState,
    DISPLAY_UNITS_IMPERIAL,
    DISPLAY_UNITS_METRIC,
    LONG_NAME_MAX_UTF8_BYTES,
    SCREEN_ON_SECS_ALWAYS_ON,
    SHORT_NAME_MAX_UTF8_BYTES,
    NETWORK_FIELD_LABELS,
    NETWORK_STAGE_LABELS,
    RadioApplyResult,
    RadioConfigVerification,
    RadioEvent,
    RadioIdentityError,
    RadioInfo,
    NodeMetadata,
    RadioSendError,
    apply_radio_config_preset,
    verify_radio_config_preset,
    RadioState,
    ReceivedMessage,
    SendStatus,
    SentMessage,
    TracerouteResult,
    TracerouteState,
    TracerouteStatus,
    rx_debug_enabled,
    rx_debug_log,
    validate_long_name,
    validate_short_name,
)
from relative_time import format_relative_age
from simulated_radio_service import SimulatedRadioService, SimulatedSendOutcome
from terminal_cursor import TerminalCursor
from theme_palette import ERROR, THEME_PALETTES
from viewport_menu import PopupItem, ViewportMenu, calculate_popup_placement


# Patches Rich's grapheme splitter (used by Static/Content's word-wrap
# fallback) so a regional-indicator flag pair can never be severed
# across a wrap boundary -- see grapheme_text.install_flag_pair_protection
# for the full rationale. Installed once at import time, process-wide,
# rather than per-message, since it corrects Rich's own wrap engine
# rather than any particular string.
install_flag_pair_protection()


# PROFILE is intentionally omitted here: it remains fully implemented
# (composed inside the "#content" ContentSwitcher, its widgets/settings/
# models/tests all untouched) but is hidden from the visible top nav and
# unreachable via any digit key -- see tab_for_key below. This is a
# navigation/UI-only change, not a deletion; restoring PROFILE to the
# nav later only requires re-adding it to this dict and to tab_for_key.
#
# DM is likewise intentionally absent as its own top-level entry (CHAT/
# DM/MENTION UX Part A): it is no longer a fourth top-level view -- it
# is now a MODE inside CHAT (see MeshtasticPassApp._chat_mode/
# _switch_chat_mode), reached via the header's DM(N) peer selector or
# the D hotkey. The DM feature itself (persistence/identity/delivery/
# resend/delete/draft architecture) is completely unchanged -- only how
# the user NAVIGATES to it changed.
TAB_NAMES = {
    "connection": "CONNECTION/CONFIG",
    "chat": "CHAT",
    "mesh": "MESH",
}

ANIMATED_STATUS = {
    RadioState.CONNECTING: "CONNECTING",
    RadioState.OFFLINE: "OFFLINE — RETRYING",
    RadioState.ERROR: "CONNECTION ERROR — RETRYING",
}

CONNECTION_LABEL_WIDTH = 12
CONNECTION_ROW_PREFIX = "  "
# Where a KeyboardDropdown's own "[ VALUE ▾ ]" begins on any RADIO/
# STYLE row built with label_width=CONNECTION_LABEL_WIDTH -- its own
# heading is "{marker} {label:<CONNECTION_LABEL_WIDTH}} [ ... ]", and
# marker+space is exactly len(CONNECTION_ROW_PREFIX) wide, so this
# stays correct automatically if either constant ever changes, rather
# than hardcoding a column number. Reused by any status row that must
# align under the control/value column instead of the label column
# (see _set_font_size_status).
CONNECTION_VALUE_COLUMN_INDENT = " " * (len(CONNECTION_ROW_PREFIX) + CONNECTION_LABEL_WIDTH + 1)


CHAT_CONFIRMATION_TIMEOUT_SECONDS = 300.0
SEND_ERROR_AUTO_DISMISS_SECONDS = 10.0
# Same lifecycle again for LONG NAME SAVED/SHORT NAME SAVED (see
# _set_long_name_status/_set_short_name_status).
IDENTITY_STATUS_AUTO_DISMISS_SECONDS = 10.0
# ADVANCED RADIO: how long a SAVE / NETWORK-switch confirmation stays
# armed after the first press before auto-disarming -- long enough for a
# deliberate second press, short enough that an armed-but-abandoned
# confirmation never lingers as a trap for a later, unrelated ENTER.
ADVANCED_RADIO_CONFIRM_SECONDS = 6.0
# Hard upper bound on ONE NETWORK apply (SAVE or switch). Writing LoRa
# modem_preset / channel_num / primary-channel PSK reboots real
# Meshtastic firmware, so this must comfortably cover the write +
# reboot + serial reconnect + config re-sync + readback. If no terminal
# result (success or failure) is reached within this window the
# operation is force-resolved to an honest ERROR -- there is never a
# permanent "SAVING & APPLYING..." state (see _network_apply_timed_out).
NETWORK_APPLY_TIMEOUT_SECONDS = 90.0
# NETWORK: how long a terminal "<name> APPLIED" success line stays on
# #advanced-radio-status before clearing itself. Success only -- a
# genuine error line never auto-dismisses, and any NEWER status write
# (via _set_advanced_radio_status, the single funnel) disarms a pending
# dismiss first, so a stale timer can never clear a newer message.
NETWORK_STATUS_SUCCESS_DISMISS_SECONDS = 10.0
# U+2713 CHECK MARK -- a plain, Narrow-width Unicode symbol (never an
# emoji-presentation glyph, so it never unexpectedly renders double-
# width). SENT/checkmark meaning: the strongest truthful evidence of a
# successful local send WITHOUT stronger remote confirmation (see
# RadioService._parse_send_response). HEARD/double-checkmark meaning:
# a genuinely different node's own routing response reached us -- see
# the same method's "from" comparison. FAILED/U+2715 MULTIPLICATION X:
# same reasoning as the checkmark -- a plain, Narrow-width text glyph
# (never "❌", an emoji-presentation glyph that would render double-
# width), replacing the literal word "FAILED" as pure presentation;
# the ERROR-colored CSS rule for this state (.delivery-failed) is
# already theme-driven and untouched by this change. Does not change
# what causes DeliveryState.FAILED, nor its meaning: a definitive
# routing failure/NAK.
# UNCONFIRMED/U+27D0 WHITE DIAMOND WITH CENTRED DOT: same reasoning as
# the checkmarks/✕ above -- a plain, Narrow-width text glyph, never an
# emoji-presentation one -- replacing the literal word "UNCONFIRMED" as
# pure presentation. Means exactly what it always meant: no conclusive
# positive or negative routing outcome arrived before the confirmation
# timeout (see can_manual_resend/MANUAL_RESEND_STATES, unchanged).
DELIVERY_CHECKMARKS: dict[DeliveryState, str] = {
    DeliveryState.SENT: "✓",
    DeliveryState.HEARD: "✓✓",
    DeliveryState.UNCONFIRMED: "⟐",
    DeliveryState.FAILED: "✕",
}
# A single narrow glyph (U+25B7 WHITE RIGHT-POINTING TRIANGLE -- plain,
# non-emoji, never double-width; confirmed via grapheme_text.cell_len
# the same way every other delivery glyph in this module was). SENDING
# always renders TWO of these, "▷ ▷", never moving horizontally --
# only which one carries ACCENT (the "active" arrow) versus the
# 25%-BASE-over-background "inactive" arrow alternates, at exactly
# SENDING_ARROW_FRAMES' own pre-existing cadence (see
# _sending_arrows_text/refresh_delivery_state). Reads as "in flight /
# still being resolved", never as success -- unlike the checkmarks
# above, SENDING covers every outgoing message for which a NAK is
# still a legitimate possible outcome (see send_was_submitted).
SENDING_ARROW_GLYPH = "▷"
SENDING_ARROW_FRAMES = (1, 2)


def _sending_arrows_text(animation_frame: int, theme: str) -> Text:
    """Two permanently visible "▷ ▷" arrows -- COLOR alternates between

    them each frame, never horizontal position/width (see MESH FOLLOW-
    UP item 6-8): frame 1 is DIM25/ACCENT, frame 2 is ACCENT/DIM25,
    matching the SAME 1/2 toggle _advance_delivery_states already
    drives (see SENDING_ARROW_FRAMES) -- no second timer. DIM25 (25%
    BASE-over-background -- deliberately weaker than the ordinary 50%
    DIM token) is theme_palette.dim_base_quarter, derived the exact
    same way DIM itself already is, so this needs no theme-specific
    literal color and resolves correctly for both SNOW and AMBER.
    """
    palette = THEME_PALETTES[theme]
    left_is_active = animation_frame == 2
    left_color = palette.accent if left_is_active else palette.dim_quarter
    right_color = palette.dim_quarter if left_is_active else palette.accent
    text = Text()
    text.append(SENDING_ARROW_GLYPH, style=Style(color=left_color))
    text.append(" ")
    text.append(SENDING_ARROW_GLYPH, style=Style(color=right_color))
    return text
CHAT_SCROLLBAR_THUMB_GLYPH = "▕"
MANUAL_RESEND_STATES = frozenset(
    (DeliveryState.UNCONFIRMED, DeliveryState.FAILED, DeliveryState.INTERRUPTED)
)


def can_manual_resend(entry: ChatEntry) -> bool:
    """Return whether the existing explicit rebroadcast action is valid."""
    return entry.outgoing and entry.delivery_state in MANUAL_RESEND_STATES


def _reply_mention_name(metadata: NodeMetadata) -> str:
    """SHORTNAME -> LONG NAME -> canonical node ID (CHAT/DM MENTION UX

    item 21) -- the OPPOSITE precedence from MESH's own _name_segments
    (Long Name first, used for on-screen display elsewhere): an inline
    @mention exists to compactly and unambiguously address one sender
    in running composer text, where a real SHORTNAME is short by
    firmware convention while a Long Name can be arbitrarily long/
    spaced. Resolved from the sender's stable node ID via already-
    synced NodeDB metadata -- never from display-name equality (item
    20): two different nodes that happen to share a Long Name still
    resolve to their own distinct SHORTNAMEs (or node IDs).
    """
    short_name = _clean_text(metadata.short_name)
    if short_name:
        return short_name
    long_name = _clean_text(metadata.long_name)
    if long_name:
        return long_name
    return metadata.node_id


# @SHORTNAME token boundary (CHAT/DM/MENTION UX Part H item 29):
# "@" not itself preceded by "@" or a word character (rejects
# "foo@POLYbar" -- an embedded/email-like "@", never a real mention
# start), followed by one-or-more word characters captured greedily
# (so "@POLYGON" captures the WHOLE word "POLYGON", which then simply
# fails the exact-match comparison below rather than partially
# matching "POLY"), with an implicit \b boundary from \w+ naturally
# stopping at the next non-word character (so "@POLY," / "@POLY!" /
# "(@POLY)" all correctly capture just "POLY"). Case-insensitive
# comparison is applied by the caller, not baked into this pattern.
_MENTION_TOKEN_PATTERN = re.compile(r"(?<![@\w])@(\w+)")


def message_mentions_short_name(text: str, short_name: str | None) -> bool:
    """Whether `text` contains an explicit @SHORTNAME token addressing

    `short_name` (item 26/29) -- case-insensitive, word-bounded. A
    missing/blank `short_name` (item 27: no usable current local
    identity) always returns False rather than guessing.
    """
    normalized_target = (short_name or "").strip().lower()
    if not normalized_target:
        return False
    return any(
        match.group(1).lower() == normalized_target
        for match in _MENTION_TOKEN_PATTERN.finditer(text)
    )


def _dm_dropdown_label(
    node_id: str, long_name: str | None, short_name: str | None
) -> str:
    """"LONG NAME / SHORT NAME" presentation for one DM dropdown row

    (PR #46 follow-up Part B item 9), falling back to whichever single
    name is known, then the canonical node ID -- names are presentation
    only, never conversation identity (that is always the dropdown
    option's own `value`, the node_id itself).
    """
    if long_name and short_name and long_name != short_name:
        return f"{long_name} / {short_name}"
    return long_name or short_name or node_id


_NODE_ID_HEX = frozenset("0123456789abcdef")


def canonical_entered_node_id(raw: str) -> str | None:
    """Validate and canonicalize a hand-typed Meshtastic node ID.

    Accepts the standard "!"-prefixed 8-hex-digit form (case-insensitive,
    e.g. "!A11Ce001") or a bare 8-hex-digit form, and returns the app's
    canonical lowercase "!xxxxxxxx" form -- the SAME stable node identity
    every other part of the app keys DMs/favorites/MESH by (see
    mesh_state.normalize_mesh_node_id). Returns None for anything else
    (empty, wrong length, non-hex), so a caller can reject the input
    without creating a conversation or sending any RF.
    """
    candidate = raw.strip().lower()
    if candidate.startswith("!"):
        candidate = candidate[1:]
    if len(candidate) != 8 or any(char not in _NODE_ID_HEX for char in candidate):
        return None
    return f"!{candidate}"


# The DM dropdown's synthetic "NEW DM" action row value -- never a real
# node ID, so it can never collide with a stored conversation.
NEW_DM_ACTION_VALUE = "__new_dm__"
# The CHAT channel selector's synthetic "NEW CHANNEL" action row value --
# never a real ChannelInfo index (so it can never be mistaken for a
# configured channel), never persisted, never given history.
NEW_CHANNEL_ACTION_VALUE = "__new_channel__"


@dataclass(frozen=True)
class PendingChannelConfig:
    """A CHAT-local, NOT-yet-radio-configured private channel draft.

    Distinguishes the truthfully PENDING channel the NEW CHANNEL editor is
    building from a radio-authoritative ChannelInfo (which describes a
    slot the connected radio actually has configured). This value holds the
    validated name and canonical Base64 PSK the future radio-write boundary
    will need; it deliberately carries NO slot/index and is never presented
    as "the radio is configured on this channel". Dropped on cancel/ESC.
    """

    name: str
    # Canonical Base64 (16- or 32-byte decoded) PSK -- the normalized form
    # normalized_private_psk/generate_private_psk produce. Never logged.
    psk_base64: str
    raw_psk: bytes
    # True when a key was generated (blank KEY), False when supplied.
    generated: bool




class ThinScrollBarRender(ScrollBarRender):
    """Use one aligned narrow glyph for both track and draggable thumb.

    Vertical only: CHAT and CONNECTION are the only remaining scrollable
    views. MESH is a bounded, non-scrolling board with no scrollbars.
    """

    @classmethod
    def render_bar(
        cls,
        size: int = 25,
        virtual_size: float = 50,
        window_size: float = 20,
        position: float = 0,
        thickness: int = 1,
        vertical: bool = True,
        back_color: Color = Color.parse("#555555"),
        bar_color: Color = Color.parse("bright_magenta"),
    ) -> Segments:
        rendered = super().render_bar(
            size=size,
            virtual_size=virtual_size,
            window_size=window_size,
            position=position,
            thickness=thickness,
            vertical=vertical,
            back_color=back_color,
            bar_color=bar_color,
        )
        if not vertical:
            return rendered

        segments = []
        for segment in rendered.segments:
            if segment.text == "\n":
                segments.append(segment)
                continue
            metadata = segment.style.meta if segment.style is not None else {}
            is_thumb = metadata.get("@mouse.down") == "grab"
            segments.append(
                Segment(
                    CHAT_SCROLLBAR_THUMB_GLYPH * thickness,
                    Style(color=bar_color if is_thumb else back_color, meta=metadata),
                )
            )
        return Segments(segments, new_lines=rendered.new_lines)


class FontSizeSelector(KeyboardDropdown):
    """User-facing label is "UI SCALE" -- the setting_name ("font_size"),

    underlying AppSettings field/JSON key, and widget id are all kept
    as "font_size"/"font-size-*" for compatibility with already-saved
    settings files and existing internal call sites; only the DISPLAYED
    text changed.
    """

    def __init__(self, font_size: int) -> None:
        super().__init__(
            "font_size",
            "UI SCALE",
            (DropdownOption(name, value) for name, value in FONT_SIZE_CHOICES),
            font_size,
            widget_id="font-size-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )

    @property
    def font_size(self) -> int:
        return int(self.value)


class ColorSelector(KeyboardDropdown):
    def __init__(self, color: str) -> None:
        super().__init__(
            "color",
            "COLOR",
            (DropdownOption(name, value) for name, value in COLOR_CHOICES),
            color,
            widget_id="color-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )

    @property
    def color(self) -> str:
        return str(self.value)


class DeviceSelector(KeyboardDropdown):
    def __init__(self, device_path: str, options: tuple[str, ...]) -> None:
        super().__init__(
            "device_path",
            "USB DEVICE",
            (DropdownOption(path, path) for path in options),
            device_path,
            widget_id="device-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


SCREEN_TIMEOUT_CHOICES = (
    ("15 SEC", 15),
    ("30 SEC", 30),
    ("1 MIN", 60),
    ("2 MIN", 120),
    ("5 MIN", 300),
    ("10 MIN", 600),
    ("ALWAYS ON", SCREEN_ON_SECS_ALWAYS_ON),
)

UNITS_DISPLAY_CHOICES = (
    ("METRIC", DISPLAY_UNITS_METRIC),
    ("IMPERIAL", DISPLAY_UNITS_IMPERIAL),
)

COMPASS_CHOICES = (
    ("NORTH UP", True),
    ("HEADING UP", False),
)

FLIP_SCREEN_CHOICES = (
    ("OFF", False),
    ("ON", True),
)

# bluetooth.enabled -- the CONNECTED RADIO's own Bluetooth, never the
# host uConsole's. A plain boolean localConfig field, so this reuses
# the exact same verified write/readback/RADIO_SETTINGS machinery as
# every other row here.
BLUETOOTH_CHOICES = (
    ("ON", True),
    ("OFF", False),
)

CLOCK_24H_CHOICES = (
    ("ON", True),
    ("OFF", False),
)

# lora.hop_limit -- PROTOBUF-SOURCE-VERIFIED (Config.LoRaConfig.hop_limit
# docstring, config_pb2.pyi): "Maximum number of hops. This can't be
# greater than 7. Default of 3." hop_start/hop_limit are also
# PROTOBUF-SOURCE-VERIFIED as a 3-bit wire field (mesh_pb2.pyi), so 0-7
# is the full range the protocol itself can represent -- never an
# app-invented cap below 7, and 7 is never treated as invalid/excessive.
HOP_LIMIT_CHOICES = tuple((str(value), value) for value in range(8))

# OFF first: this is a MeshtasticPass-local behavior preference (see
# AppSettings.clock_auto_sync), never a radio config field, and must
# default to and visually lead with OFF -- explicit opt-in only (item 16).
AUTO_SYNC_CHOICES = (
    ("OFF", False),
    ("ON", True),
)

# device.tzdef -- a POSIX TZ environment-variable string, applied by
# firmware via setenv("TZ", ...)/tzset() (confirmed against meshtastic/
# firmware's src/main.cpp), so any valid POSIX TZ string is accepted;
# these are only the friendly names this pass exposes. Classification,
# reported honestly (see the completion report's own 5-way scheme):
# NOT SET is the empty string firmware itself falls back to GMT0 for.
# UTC="GMT0" is FIRMWARE-SOURCE-VERIFIED -- that exact fallback string,
# confirmed in src/main.cpp. EASTERN="EST5EDT,M3.2.0,M11.1.0" is
# REAL-HARDWARE-VERIFIED: observed correct on the user's own HELTEC_V4,
# firmware 2.7.x, and this exact string must never be altered. The
# other four US zones reuse that same already-verified M3.2.0/M11.1.0
# DST transition rule (the standardized post-2007 US rule, not
# Meshtastic-specific) with each zone's own standard textbook POSIX
# offset/name -- INFERENCE, not independently hardware- or firmware-
# doc-verified. No Olson-name mapping ships in the installed
# meshtastic==2.7.11 package (grepped for "tzdef"/"timezone" across
# it -- none found), ruling out an SDK-SOURCE-VERIFIED label for any
# of these.
TIMEZONE_CHOICES = (
    ("NOT SET", ""),
    ("UTC", "GMT0"),
    ("EASTERN", "EST5EDT,M3.2.0,M11.1.0"),
    ("CENTRAL", "CST6CDT,M3.2.0,M11.1.0"),
    ("MOUNTAIN", "MST7MDT,M3.2.0,M11.1.0"),
    ("PACIFIC", "PST8PDT,M3.2.0,M11.1.0"),
    ("ALASKA", "AKST9AKDT,M3.2.0,M11.1.0"),
    ("HAWAII", "HST10"),
)


@dataclass(frozen=True)
class RadioSettingSpec:
    """Maps one RADIO-section dropdown to its localConfig field.

    `to_schema_value`/`from_schema_value` exist ONLY for clock_24h,
    whose user-facing "24 HOUR TIME" label is the logical NEGATION of
    the schema's own use_12h_clock field -- every other row's dropdown
    value already IS the schema value, so the default identity mapping
    applies unchanged.
    """

    section: str
    field: str
    to_schema_value: Callable[[Any], Any] = lambda value: value
    from_schema_value: Callable[[Any], Any] = lambda value: value


RADIO_SETTINGS: dict[str, RadioSettingSpec] = {
    "role": RadioSettingSpec("device", "role"),
    "bluetooth": RadioSettingSpec("bluetooth", "enabled"),
    "screen_timeout": RadioSettingSpec("display", "screen_on_secs"),
    "units": RadioSettingSpec("display", "units"),
    "compass": RadioSettingSpec("display", "compass_north_top"),
    "flip_screen": RadioSettingSpec("display", "flip_screen"),
    "clock_24h": RadioSettingSpec(
        "display",
        "use_12h_clock",
        to_schema_value=lambda is_24h: not is_24h,
        from_schema_value=lambda use_12h_clock: not use_12h_clock,
    ),
    "timezone": RadioSettingSpec("device", "tzdef"),
    "hop_limit": RadioSettingSpec("lora", "hop_limit"),
}


class RoleSelector(KeyboardDropdown):
    """ROLE -- backed by device.role. Options are built once from

    role_choices(), itself derived from the installed protobuf
    schema's own enum, never a hardcoded role list -- see that
    function's own docstring. Never pre-selects or infers a role: the
    initial `role` value always comes from the connected radio's own
    already-synced config. A live value this installed schema doesn't
    recognize (e.g. a newer firmware's role not yet in this SDK
    version) falls back to KeyboardDropdown.selected_label's own safe
    "show the raw number" behavior rather than crashing or silently
    coercing it to a known role.
    """

    def __init__(self, role: int) -> None:
        super().__init__(
            "role",
            "ROLE",
            (DropdownOption(name, value) for name, value in role_choices()),
            role,
            widget_id="radio-role-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class BluetoothSelector(KeyboardDropdown):
    """BLUETOOTH -- backed by bluetooth.enabled on the CONNECTED RADIO,

    never the host uConsole's own Bluetooth.
    """

    def __init__(self, enabled: bool) -> None:
        super().__init__(
            "bluetooth",
            "BLUETOOTH",
            (DropdownOption(name, value) for name, value in BLUETOOTH_CHOICES),
            enabled,
            widget_id="radio-bluetooth-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class TimezoneSelector(KeyboardDropdown):
    """TIMEZONE -- backed by device.tzdef (see TIMEZONE_CHOICES for the

    exact per-option POSIX TZ string and its sourcing). A CUSTOM option
    is injected dynamically by _render_radio_settings (see
    _timezone_options_for) whenever the connected radio's own tzdef
    doesn't match any known mapping -- never a real, persisted choice
    of its own, and the raw string is never otherwise shown in this
    normal UI.
    """

    def __init__(self, tzdef: str) -> None:
        super().__init__(
            "timezone",
            "TIMEZONE",
            (DropdownOption(name, value) for name, value in TIMEZONE_CHOICES),
            tzdef,
            widget_id="radio-timezone-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class ScreenTimeoutSelector(KeyboardDropdown):
    def __init__(self, screen_on_secs: int) -> None:
        super().__init__(
            "screen_timeout",
            "SCREEN SLP",
            (DropdownOption(name, value) for name, value in SCREEN_TIMEOUT_CHOICES),
            screen_on_secs,
            widget_id="radio-screen-timeout-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class UnitsSelector(KeyboardDropdown):
    def __init__(self, units: int) -> None:
        super().__init__(
            "units",
            "UNITS",
            (DropdownOption(name, value) for name, value in UNITS_DISPLAY_CHOICES),
            units,
            widget_id="radio-units-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class CompassSelector(KeyboardDropdown):
    def __init__(self, compass_north_top: bool) -> None:
        super().__init__(
            "compass",
            "COMPASS",
            (DropdownOption(name, value) for name, value in COMPASS_CHOICES),
            compass_north_top,
            widget_id="radio-compass-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class FlipScreenSelector(KeyboardDropdown):
    def __init__(self, flip_screen: bool) -> None:
        super().__init__(
            "flip_screen",
            "FLIP SCREEN",
            (DropdownOption(name, value) for name, value in FLIP_SCREEN_CHOICES),
            flip_screen,
            widget_id="radio-flip-screen-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class Clock24HSelector(KeyboardDropdown):
    def __init__(self, is_24h: bool) -> None:
        super().__init__(
            "clock_24h",
            "24 HOUR TIME",
            (DropdownOption(name, value) for name, value in CLOCK_24H_CHOICES),
            is_24h,
            widget_id="radio-clock-24h-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class HopLimitSelector(KeyboardDropdown):
    """HOP LIMIT -- backed by lora.hop_limit (see HOP_LIMIT_CHOICES for

    sourcing). The initial value is always the CONNECTED radio's own
    synced value (never defaulted to 3 here -- see _render_radio_settings,
    which reads it live via read_synced_config_field exactly like every
    other RADIO_SETTINGS dropdown); the 3 passed to __init__ below is
    only compose()'s placeholder before the first connection.
    """

    def __init__(self, hop_limit: int) -> None:
        super().__init__(
            "hop_limit",
            "HOP LIMIT",
            (DropdownOption(name, value) for name, value in HOP_LIMIT_CHOICES),
            hop_limit,
            widget_id="radio-hop-limit-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class AutoSyncSelector(KeyboardDropdown):
    """User-facing label is "CLOCK SYNC" -- the setting_name

    ("clock_auto_sync"), AppSettings field/JSON key, class name, and
    widget id are all kept as "clock_auto_sync"/"auto-sync"/
    "AutoSyncSelector" for persistence/internal compatibility; only the
    DISPLAYED text changed. A MeshtasticPass-local preference, never
    written to the radio -- see AppSettings.clock_auto_sync/
    _maybe_auto_sync_clock. Uses the same dropdown grammar as every
    other RADIO-section control even though nothing here is a
    localConfig field.
    """

    def __init__(self, enabled: bool) -> None:
        super().__init__(
            "clock_auto_sync",
            "CLOCK SYNC",
            (DropdownOption(name, value) for name, value in AUTO_SYNC_CHOICES),
            enabled,
            widget_id="radio-auto-sync-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


# ADVANCED RADIO: the built-in "LongFast" NETWORK. Always available in
# the NETWORK selector without the user creating it, and the default
# selection. It represents the normal public LongFast configuration
# using Meshtastic's own defaults: the LONG_FAST modem preset, no
# explicit frequency slot (0 == "let the radio auto-select"), a blank
# primary-channel name, and the SDK's own default public-channel PSK
# sentinel byte 0x01 (base64 "AQ=="). Selecting it is never applied to
# the radio automatically -- only an explicit, confirmed NETWORK switch
# or SAVE ever writes RF/config (see _apply_network_from_thread).
BUILTIN_LONGFAST_NETWORK = "LongFast"
_LONGFAST_DEFAULT_PSK_BASE64 = "AQ=="
# NETWORK: what the PRESET selector shows when a successful radio sync
# does not semantically match ANY saved PRESET (built-in LongFast
# included) -- an honest "the radio is on a configuration this app has
# no PRESET for" placeholder. Display-only: it is NEVER added to the
# dropdown's options (KeyboardDropdown.selected_label falls back to
# str(value) for a value with no option), never persisted, and never
# creates a PRESET. Resolving it requires an explicit user selection;
# detection itself performs zero writes (see
# _detect_active_network_from_radio).
UNMATCHED_NETWORK_LABEL = "CURRENT RADIO"


def builtin_longfast_preset() -> "RadioConfigPreset":
    return RadioConfigPreset(
        name=BUILTIN_LONGFAST_NETWORK,
        modem_preset="LONG_FAST",
        frequency_slot=0,
        channel_name="",
        channel_psk_base64=_LONGFAST_DEFAULT_PSK_BASE64,
    )


class NetworkSelector(KeyboardDropdown):
    """The NETWORK section's "PRESET [ ... ]" dropdown -- the mechanism

    for switching between complete saved Meshtastic network
    configurations (RadioConfigPreset.name) plus the built-in
    BUILTIN_LONGFAST_NETWORK entry. Merely focusing it, opening it, or
    navigating its choices is zero RF/config; only re-selecting a
    different PRESET to confirm it ever applies anything (see
    MeshtasticPassApp.dropdown_selected's "network" branch).
    """

    def __init__(self, options: Iterable[DropdownOption]) -> None:
        super().__init__(
            "network",
            "PRESET",
            options,
            BUILTIN_LONGFAST_NETWORK,
            widget_id="advanced-radio-network-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class RadioModeSelector(KeyboardDropdown):
    """The NEW NETWORK editor's "RADIO MODE [ ... ]" dropdown -- a

    friendly UI label ("LONG FAST", "MEDIUM SLOW", ...) over Meshtastic's
    actual modem_preset enum NAME (see radio_capabilities.
    modem_preset_choices), which is what is stored and applied.
    """

    def __init__(self, modem_preset_name: str) -> None:
        super().__init__(
            "radio_mode",
            "RADIO MODE",
            (DropdownOption(label, name) for label, name in modem_preset_choices()),
            modem_preset_name,
            widget_id="advanced-radio-mode-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row advanced-radio-editor",
        )


class NewNetworkControl(Static):
    """[ NEW PRESET ] -- reveals the transient NEW PRESET editor.

    Zero RF, zero persistence by itself: it only shows the editor rows
    (see MeshtasticPassApp._set_network_editor_open). The user must
    still explicitly SAVE, and abandoning the editor discards it.
    """

    can_focus = True

    class Activated(Message):
        pass

    def __init__(self) -> None:
        # Indented to the same control/value column the PRESET selector's
        # "[ LongFast ▾ ]" starts at (CONNECTION_VALUE_COLUMN_INDENT),
        # derived from CONNECTION_LABEL_WIDTH so it tracks UI scale rather
        # than a hardcoded gap.
        super().__init__(
            f"{CONNECTION_VALUE_COLUMN_INDENT}[ NEW PRESET ]",
            id="advanced-radio-new-network",
            classes="connection-action-row",
            markup=False,
        )

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Activated())
            event.stop()


class NewChannelApply(Static):
    """[ APPLY ] -- write the validated pending private channel to the radio.

    Asynchronous, radio-touching (see MeshtasticPassApp._apply_pending_channel):
    writes exactly one logical channel slot, then readbacks/verifies before any
    promotion. Never claims success before a matching readback.
    """

    can_focus = True

    class Activated(Message):
        pass

    def __init__(self) -> None:
        super().__init__(
            "[ APPLY ]",
            id="new-channel-apply",
            classes="connection-action-row",
            markup=False,
        )

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Activated())
            event.stop()
        elif event.key == "escape":
            self.app._cancel_new_channel()
            event.stop()


class NewChannelSave(Static):
    """[ SAVE ] -- validate the CHAT NEW CHANNEL editor (see
    MeshtasticPassApp._save_new_channel). Production of a pending draft only,
    zero radio writes; never claims the radio was configured.
    """

    can_focus = True

    class Activated(Message):
        pass

    def __init__(self) -> None:
        super().__init__(
            "[ SAVE ]",
            id="new-channel-save",
            classes="connection-action-row",
            markup=False,
        )

    def on_key(self, event: Key) -> None:
        if event.key in ("enter",):
            self.post_message(self.Activated())
            event.stop()
        elif event.key == "escape":
            self.app._cancel_new_channel()
            event.stop()


class NewChannelCancel(Static):
    """[ CANCEL ] -- discard the CHAT NEW CHANNEL editor. Zero writes/RF."""

    can_focus = True

    class Activated(Message):
        pass

    def __init__(self) -> None:
        super().__init__(
            "[ CANCEL ]",
            id="new-channel-cancel",
            classes="connection-action-row",
            markup=False,
        )

    def on_key(self, event: Key) -> None:
        if event.key in ("enter",):
            self.post_message(self.Activated())
            event.stop()
        elif event.key == "escape":
            self.app._cancel_new_channel()
            event.stop()


class SaveNetworkControl(Static):
    """[ SAVE ] -- validate the NEW NETWORK editor, then (after a

    press-again-to-confirm cycle, because RF/config will change) persist
    the NETWORK locally and apply it through radio_service.
    apply_radio_config_preset. There is no separate APPLY button.
    """

    can_focus = True

    class Activated(Message):
        pass

    def __init__(self) -> None:
        super().__init__(
            "[ SAVE ]",
            id="advanced-radio-save",
            classes="connection-action-row",
            markup=False,
        )

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Activated())
            event.stop()


class CancelNetworkControl(Static):
    """[ CANCEL ] -- discard the NEW NETWORK editor's unsaved contents,

    collapse it, and restore [ NEW NETWORK ]. Zero RF/config, no
    confirmation, leaves the currently selected NETWORK unchanged.
    """

    can_focus = True

    class Activated(Message):
        pass

    def __init__(self) -> None:
        super().__init__(
            "[ CANCEL ]",
            id="advanced-radio-cancel",
            classes="connection-action-row",
            markup=False,
        )

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Activated())
            event.stop()


class NetworkFieldInput(Horizontal):
    """One labeled text-entry row inside the transient NEW NETWORK

    editor (NETWORK NAME / FREQ. SLOT / KEY) -- a plain, always-enabled
    LOCAL DRAFT field. Never itself written anywhere by being edited:
    SAVE is the only action that persists/applies the editor's values,
    so no two-state nav/edit toggle is needed here.
    """

    can_focus = False

    def __init__(
        self,
        *,
        label: str,
        widget_id: str,
        input_id: str,
        max_length: int | None = None,
        collapsible: bool = True,
    ) -> None:
        # `collapsible` is False for the CHAT NEW CHANNEL form, which shares
        # this same layout primitives but has no collapse toggle (it is always
        # shown while the CHAT editor view is active). NEW PRESET keeps it True
        # (its editor rows collapse until [ NEW PRESET ] is pressed).
        classes = "connection-action-row"
        if collapsible:
            classes += " advanced-radio-editor"
        super().__init__(id=widget_id, classes=classes)
        self._label = label
        self._input_id = input_id
        self._max_length = max_length

    def compose(self) -> ComposeResult:
        yield Static(" ", classes="connection-selection-gutter", markup=False)
        yield Static(self._label, classes="connection-label", markup=False)
        yield Static("[ ", classes="identity-bracket", markup=False)
        yield Input(id=self._input_id, max_length=self._max_length)
        yield Static(" ]", classes="identity-bracket", markup=False)


class ChannelSelector(KeyboardDropdown):
    """CHAT's LEFT peer selector: [ channel ▾ ].

    The CLOSED heading is the current channel; the OPEN dropdown shows
    every configured channel plus ONE trailing synthetic "NEW CHANNEL"
    action row (see NEW_CHANNEL_ACTION_VALUE). NEW CHANNEL is never a real
    channel -- it carries no slot/index, is never persisted and never gets
    history -- it simply opens the CHAT-local NEW CHANNEL editor (see
    MeshtasticPassApp._start_new_channel). Configuring an existing channel
    via the dropdown is unchanged.
    """

    def __init__(self, channels: tuple[ChannelInfo, ...], value: int) -> None:
        super().__init__(
            "channel_index",
            "",
            (DropdownOption(channel.name, channel.index) for channel in channels),
            value,
            widget_id="chat-title",
            prefix="",
        )
        self._action_items: tuple[DropdownOption, ...] = ()
        # Snapshot of the configured-channel options captured fresh at
        # open_menu time and restored by close_menu -- never the init-time
        # list, so later set_options updates survive an open/close cycle.
        self._open_snapshot: tuple[DropdownOption, ...] | None = None

    def open_menu(self) -> None:
        # Rebuild the popup rows: the configured channels first, then the
        # NEW CHANNEL action, so index-for-index navigation over
        # `_action_items` matches KeyboardDropdown.on_key's `% len(self.
        # options)` math (restored to the configured channels by close_menu).
        self._open_snapshot = tuple(self.options)
        self._action_items = self._open_snapshot + (
            DropdownOption("NEW CHANNEL", NEW_CHANNEL_ACTION_VALUE),
        )
        self.options = self._action_items
        self.is_open = True
        self._highlighted_index = self._selected_index()
        self.add_class("open")
        items = tuple(
            PopupItem(option.label, option.value, actionable=option.value is not None)
            for option in self._action_items
        )
        self.popup = ViewportMenu(
            items,
            highlighted_index=self._highlighted_index,
            on_activate=self._activate_popup_item,
        )
        width = max(len(option.label) for option in self._action_items) + 4
        self.screen.mount(self.popup)
        self.popup.place(self.region, self.screen.region, width)
        self._render_dropdown()

    def close_menu(self) -> None:
        super().close_menu()
        if self._open_snapshot is not None:
            self.options = self._open_snapshot
            self._open_snapshot = None
        self._action_items = ()

    def _activate_popup_item(self, index: int, _item: PopupItem) -> None:
        if not 0 <= index < len(self._action_items):
            self.close_menu()
            return
        option = self._action_items[index]
        value = option.value
        self.close_menu()
        if value == NEW_CHANNEL_ACTION_VALUE:
            self.app._start_new_channel()
            return
        self._highlighted_index = index
        self.value = value
        self.post_message(self.Selected(self, value))


class DMModeSelector(KeyboardDropdown):
    """CHAT's RIGHT peer selector: [ DM(N) ▾ ] -- N is the unread DM

    count (see MeshtasticPassApp._recount_dm_unread). A TRUE dropdown,
    peer to ChannelSelector (PR #46 follow-up Part B): opening it
    (ENTER, click, or the D hotkey) shows existing DM conversations
    directly, most-recent-activity first -- never immediately
    switching into DMS mode/the full conversation list the way the
    original PR #46 pass did. Opening this dropdown alone is zero-RF
    and never clears DM(N) -- only actually opening a conversation
    does (see MeshtasticPassApp._open_dm_conversation).

    `self.options`/`self.value` hold exactly one entry -- the CLOSED
    heading's own DropdownOption("DM(N)", "dms") -- and are restored
    after every close (see close_menu); they are NOT what the open
    popup shows. The open popup's real conversation rows live in
    `self._conversation_items`, a completely separate list rebuilt
    fresh on every open_menu() call (never cached/persisted), because
    KeyboardDropdown's own on_key navigation math (`% len(self.
    options)`) would otherwise misbehave against a "DM(N)"-only
    options list of length 1.
    """

    def __init__(self, unread_count: int) -> None:
        super().__init__(
            "chat_dm_mode",
            "",
            (DropdownOption(f"DM({unread_count})", "dms"),),
            "dms",
            widget_id="chat-dm-selector",
            prefix="",
        )
        self._closed_options = self.options
        self._conversation_items: tuple[DropdownOption, ...] = ()

    def set_unread_count(self, unread_count: int) -> None:
        option = DropdownOption(f"DM({unread_count})", "dms")
        self._closed_options = (option,)
        if not self.is_open:
            self.set_options((option,), value="dms")

    def open_menu(self) -> None:
        conversations = self.app.dm_dropdown_conversations()
        # The popup rows are the real stored conversations (or a single
        # non-actionable "NO DMS" placeholder when none exist) plus ONE
        # synthetic trailing "NEW DM" action row. `_conversation_items`
        # must mirror the popup rows index-for-index (KeyboardDropdown.
        # on_key's own `% len(self.options)` math and _activate_popup_item
        # both index into it), so the NEW DM row is included here too --
        # its value is the NEW_DM_ACTION_VALUE sentinel, never a real node
        # ID, so it can never be mistaken for a stored conversation.
        items_list = list(conversations) if conversations else [DropdownOption("NO DMS", None)]
        items_list.append(DropdownOption("NEW DM", NEW_DM_ACTION_VALUE))
        self._conversation_items = tuple(items_list)
        # Swapped in only so KeyboardDropdown's own on_key navigation
        # math (`% len(self.options)`) operates against the REAL
        # conversation + NEW DM row count while open -- restored by
        # close_menu.
        self.options = self._conversation_items
        self.value = self._conversation_items[0].value
        self.is_open = True
        self._highlighted_index = 0
        self.add_class("open")
        items = tuple(
            PopupItem(option.label, option.value, actionable=option.value is not None)
            for option in self._conversation_items
        )
        self.popup = ViewportMenu(
            items,
            highlighted_index=self._highlighted_index,
            on_activate=self._activate_popup_item,
        )
        width = max(len(option.label) for option in self._conversation_items) + 4
        self.screen.mount(self.popup)
        self.popup.place(self.region, self.screen.region, width)
        self._render_dropdown()

    def close_menu(self) -> None:
        super().close_menu()
        self.options = self._closed_options
        self.value = "dms"
        self._render_dropdown()

    def _activate_popup_item(self, index: int, _item: PopupItem) -> None:
        if not 0 <= index < len(self._conversation_items):
            self.close_menu()
            return
        option = self._conversation_items[index]
        node_id = option.value
        self.close_menu()
        if node_id is None:
            # "NO DMS" -- item 14: safely closes without opening or
            # fabricating a conversation, whether reached by mouse
            # click (ViewportMenu.activate's own actionable=False
            # guard already no-ops it) or by keyboard ENTER
            # (KeyboardDropdown.on_key calls _activate_popup_item
            # directly, bypassing that actionable check -- so this
            # explicit guard is the one that actually matters there).
            return
        if node_id == NEW_DM_ACTION_VALUE:
            # NEW DM: open the transient node-ID entry surface, never a
            # stored conversation. Zero RF; no conversation is created
            # until a valid node ID is actually entered.
            self.app._start_new_dm()
            return
        self.post_message(self.Selected(self, node_id))


# The first-pass emoji set -- see the CHAT delivery/menu/emoji task.
# Centralized here for easy future expansion; nothing else in this
# module hardcodes this list or its length.
EMOJI_PICKER_CHOICES: tuple[str, ...] = (
    "😀",
    "😂",
    "❤️",
    "👍",
    "👎",
    "😭",
    "😮",
    "😡",
    "🎉",
    "🔥",
    "👋",
    "✨",
    "📡",
)
# Must match the ".emoji-picker { height: ... }" CSS rule below.
EMOJI_PICKER_HEIGHT = 3
# What the ".emoji-picker" CSS rule below actually costs in columns:
# "border: solid ..." is 1 column each side (EMOJI_PICKER_BORDER_CELLS),
# "padding: 0 2 0 1" is 1 column on the left but 2 on the right
# (EMOJI_PICKER_PADDING_CELLS) -- see emoji_picker_total_width().
#
# The extra right-side cell is a real-hardware fix, not cosmetic: "❤️"
# (U+2764 HEAVY BLACK HEART + U+FE0F variation selector) is the only
# choice in EMOJI_PICKER_CHOICES whose BASE codepoint has Unicode East
# Asian Width "Narrow" -- every other emoji here is "Wide" and renders
# at a consistent, undisputed 2 terminal columns everywhere. Rich's own
# cell_len() (used by emoji_picker_content_width() below) resolves the
# heart+VS16 sequence to 2 columns, matching how most GUI terminals
# render it, but a terminal that instead honors the base character's
# raw Narrow property -- observed on real uConsole hardware -- draws it
# in only 1 column. Since Textual emits a whole picker row as one
# contiguous run of characters (content, then padding, then the right
# border), any single glyph rendering narrower than Python assumed
# shifts every character drawn after it in that same row -- including
# the picker's own right border -- left by the shortfall, making it
# look displaced relative to the (pure-ASCII, unambiguous) top/bottom
# border rows. A real terminal's cursor advances by however many
# columns IT decides a character occupies; nothing in Python can
# correct that after the fact. The fix is to always leave 1 spare,
# unambiguous (plain-space) column between the content and the right
# border: on a terminal that renders the heart at the expected 2
# columns, that space is just a harmless extra sliver of padding: on
# one that renders it at only 1, the same space is what silently
# absorbs the 1-column shortfall, so the border still lands exactly
# where it should either way. See
# test_picker_padding_absorbs_worst_case_ambiguous_width_undercount for
# the regression that encodes this reasoning directly (not just a
# width-helper arithmetic check, which alone cannot catch this: the
# pure Python math above is self-consistent regardless of what a real
# terminal decides to do with an ambiguous-width character).
EMOJI_PICKER_BORDER_CELLS = 2
EMOJI_PICKER_PADDING_CELLS = 3


def emoji_picker_content_width() -> int:
    """Exact rendered terminal-cell width of the picker's emoji row.

    Never len(text): each item is a 1-cell bracket/space, the emoji's
    own RENDERED cell width (cell_len -- a wide emoji is 2 cells even
    when, like an intact heart+variation-selector sequence, it is more
    than one Python character), and a closing 1-cell bracket/space,
    plus a 1-cell separator between items. Derived from
    EMOJI_PICKER_CHOICES itself, so the picker never needs a manual
    width update if the set changes.
    """
    per_item_width = sum(1 + cell_len(emoji) + 1 for emoji in EMOJI_PICKER_CHOICES)
    separator_width = max(0, len(EMOJI_PICKER_CHOICES) - 1)
    return per_item_width + separator_width


def emoji_picker_total_width() -> int:
    """Content width plus the border/padding the CSS rule actually

    applies -- the picker's bounding box should hug this exactly, with
    only the CSS's own intentional padding, never a stretched-to-fit
    container (see item 12 of the follow-up task).
    """
    return (
        emoji_picker_content_width()
        + EMOJI_PICKER_BORDER_CELLS
        + EMOJI_PICKER_PADDING_CELLS
    )


class EmojiPicker(Static):
    """Compact horizontal emoji strip for the CHAT composer (Ctrl+E).

    Never focusable -- like the existing sender-action ViewportMenu,
    this is an overlay the App's own on_key intercepts LEFT/RIGHT/
    ENTER/ESC for while it is open; the composer Input keeps real
    Textual focus throughout (see item 22: "composer remains focused").
    """

    can_focus = False

    def __init__(self) -> None:
        super().__init__(classes="emoji-picker", markup=False)
        self.highlighted_index = 0
        default_palette = THEME_PALETTES["snow"]
        self._base_color = default_palette.base
        self._accent_color = default_palette.accent

    def on_mount(self) -> None:
        self._render_picker()

    def set_palette(self, base: str, accent: str) -> None:
        self._base_color = base
        self._accent_color = accent
        self._render_picker()

    def move_highlight(self, direction: int) -> None:
        self.highlighted_index = (self.highlighted_index + direction) % len(
            EMOJI_PICKER_CHOICES
        )
        self._render_picker()

    @property
    def selected_emoji(self) -> str:
        return EMOJI_PICKER_CHOICES[self.highlighted_index]

    def _render_picker(self) -> None:
        text = Text()
        for index, emoji in enumerate(EMOJI_PICKER_CHOICES):
            if index:
                text.append(" ", style=self._base_color)
            selected = index == self.highlighted_index
            text.append("[" if selected else " ", style=self._base_color)
            text.append(emoji, style=self._accent_color if selected else self._base_color)
            text.append("]" if selected else " ", style=self._base_color)
        self.update(text)


@dataclass
class ChannelChatState:
    entries: list[ChatEntry] = field(default_factory=list)
    # An unfinished composer draft is preserved per channel (item 27):
    # switching CHANNELS -- LongFast to Local, say -- must not lose an
    # in-progress message on the channel the user is leaving, and must
    # not leak that text into the channel being switched to either.
    draft: str = ""
    unread_count: int = 0
    transcript_new_count: int = 0
    has_older_history: bool = False
    mounted_target: int = DEFAULT_HISTORY_LIMIT
    open_scroll_pending: bool = False
    loaded: bool = False
    # The channel_key (ChannelInfo.stable_key) `entries` was actually
    # queried with (FINAL MESHTASTIC POLISH -- CHAT channel-history
    # isolation) -- None means "loaded before the radio's real channel
    # identity was known" (this app's own pre-connection placeholder).
    # _ensure_channel_loaded compares this against the CURRENT stable
    # key on every access, never trusting `loaded` alone: a same-index
    # radio reconfiguration (e.g. slot 0 LongFast -> MediumSlow between
    # runs) must invalidate an already-"loaded" cache the instant the
    # real identity resolves to something different than what it was
    # last queried with, even though the index itself never changed.
    loaded_key: str | None = None
    new_message_ids: set[int] = field(default_factory=set)
    unread_message_ids: set[int] = field(default_factory=set)
    pending_older_ids: set[tuple[str, int]] = field(default_factory=set)
    new_below_ids: set[tuple[str, int]] = field(default_factory=set)


class RadioMessageReceived(Message):
    """Thread-safe bridge from either radio service into the UI queue."""

    def __init__(self, message: ReceivedMessage) -> None:
        super().__init__()
        self.message = message


class SendSubmitted(Message):
    def __init__(
        self,
        entry: ChatEntry,
        sent: SentMessage,
        generation: int,
    ) -> None:
        super().__init__()
        self.entry = entry
        self.sent = sent
        self.generation = generation


class PrivateChannelApplyResultMessage(Message):
    """A verified (or failed) private-channel apply, posted from the worker."""

    def __init__(self, result: object, pending: object) -> None:
        super().__init__()
        self.result = result
        self.pending = pending


class PrivateChannelApplyFailed(Message):
    def __init__(self, detail: str, pending: object) -> None:
        super().__init__()
        self.detail = detail
        self.pending = pending


class SendFailed(Message):
    def __init__(self, entry: ChatEntry, detail: str, generation: int) -> None:
        super().__init__()
        self.entry = entry
        self.detail = detail
        self.generation = generation


class DeliveryStatusReceived(Message):
    def __init__(
        self,
        entry: ChatEntry,
        status: SendStatus,
        generation: int,
    ) -> None:
        super().__init__()
        self.entry = entry
        self.status = status
        self.generation = generation


class TracerouteStatusReceived(Message):
    """A real traceroute outcome arrived (TRACE ROUTE Part C).

    `request_token` (an app-local sequence number, assigned BEFORE the
    RF request is even sent -- see MeshtasticPassApp._start_traceroute)
    is what the handler correlates against _active_traceroute, never
    RadioService's own SDK packet_id: the token is known synchronously,
    before the background worker even starts, so it can never race a
    same-thread/synchronous status_handler call (e.g. SimulatedRadioService)
    that fires before the worker's own return value would otherwise be
    recorded.
    """

    def __init__(self, request_token: int, status: TracerouteStatus) -> None:
        super().__init__()
        self.request_token = request_token
        self.status = status


class TracerouteRequestFailed(Message):
    """send_traceroute() itself raised (e.g. the radio disconnected

    between the menu press and the worker thread actually running) --
    never a routing NAK/timeout, which arrive as TracerouteStatusReceived
    instead.
    """

    def __init__(self, request_token: int, detail: str) -> None:
        super().__init__()
        self.request_token = request_token
        self.detail = detail


class IdentitySaved(Message):
    """The radio accepted an advertised identity-name update."""

    def __init__(self, info: RadioInfo, field_label: str) -> None:
        super().__init__()
        self.info = info
        self.field_label = field_label


class IdentitySaveFailed(Message):
    """An identity-name update failed validation or radio submission."""

    def __init__(self, detail: str, field_label: str) -> None:
        super().__init__()
        self.detail = detail
        self.field_label = field_label


class RadioSettingApplied(Message):
    """A RADIO-section verified config write finished (success or not).

    Carries the dropdown itself (never mutated off the UI thread --
    only referenced, then acted on here once this message is handled)
    so the handler can show/revert that exact row without re-querying.
    """

    def __init__(
        self,
        dropdown: "KeyboardDropdown",
        setting_name: str,
        result: ConfigWriteResult,
    ) -> None:
        super().__init__()
        self.dropdown = dropdown
        self.setting_name = setting_name
        self.result = result


class RadioConfigPresetApplied(Message):
    """One ADVANCED RADIO NETWORK apply-worker (radio_service.

    apply_radio_config_preset, then -- while still on the worker thread
    -- a fresh readback via reread_lora_and_primary_channel +
    verify_radio_config_preset) RETURNED. The write may have completed
    cleanly with no reboot (Path B: `verification` is populated and
    authoritative), or the radio may have rebooted to commit LoRa
    config (Path A: `verification` is None / the radio is no longer
    ONLINE, and the operation now waits for the reconnect full-sync).
    `token` correlates this against the ONE outstanding _network_apply.
    `saved` distinguishes a SAVE from a bare NETWORK switch.
    """

    def __init__(
        self,
        token: int,
        preset_name: str,
        result: RadioApplyResult,
        saved: bool = False,
        verification: RadioConfigVerification | None = None,
    ) -> None:
        super().__init__()
        self.token = token
        self.preset_name = preset_name
        self.result = result
        self.saved = saved
        self.verification = verification


@dataclass
class NetworkApply:
    """The ONE outstanding ADVANCED RADIO NETWORK apply (SAVE or switch).

    Held on MeshtasticPassApp._network_apply from the moment SAVE/switch
    is confirmed until a terminal SUCCESS or ERROR is reached. `token`
    correlates the worker return and the timeout timer; `saved` picks
    the "SAVED & APPLIED" vs "APPLIED" wording and whether a failure
    reverts the NETWORK selector; `awaiting_reconnect` means the apply
    write already landed the radio in a reboot/reconnect and the
    operation is now waiting for the interface to come back so it can
    read the new config back (see _resolve_network_apply_after_reconnect).
    """

    token: int
    name: str
    preset: RadioConfigPreset
    saved: bool
    awaiting_reconnect: bool = False


class ClockSyncApplied(Message):
    """One AUTO SYNC RadioService.sync_clock() call finished (see

    _apply_sync_clock_from_thread). `generation` identifies WHICH
    attempt this is -- see clock_sync_applied's own guard: a completion
    whose generation no longer matches the app's current one is stale
    (a newer attempt started, or a disconnect/reconnect superseded it
    -- see _reset_clock_sync_state) and is safely ignored.
    """

    def __init__(self, result: ClockSyncResult, generation: int) -> None:
        super().__init__()
        self.result = result
        self.generation = generation


class LoadOlderControl(Static):
    """Focusable, keyboard-only control for one bounded history page."""

    can_focus = True

    class Activated(Message):
        pass

    def __init__(self) -> None:
        super().__init__("[ LOAD OLDER ]", id="load-older", classes="chat-nav-target", markup=False)

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Activated())
            event.stop()


class EndOfChatHistoryMarker(Static):
    """Passive proof that the oldest stored row is currently mounted."""

    can_focus = False

    def __init__(self) -> None:
        super().__init__(
            "END OF CHAT HISTORY",
            id="end-of-chat-history",
            markup=False,
        )


class StartOfChannelHistoryMarker(Static):
    """Informational-only proof a channel has zero stored messages yet.

    (FINAL MESHTASTIC POLISH -- CHAT channel-history isolation.) Purely
    a rendered Static line, exactly like EndOfChatHistoryMarker: never
    inserted into chat_history/state.entries, never given a message_id,
    never counted toward unread/new, never a resend/delete/reply
    target -- there is simply no ChatEntry for it to be. Removed
    automatically (see _insert_chat_widget) the instant this channel's
    first real message is inserted, and only ever mounted for a
    CHANNEL (see _initial_chat_widgets) -- DM conversations use their
    own separate empty-state handling, if any, untouched here.
    """

    can_focus = False

    def __init__(self, channel_label: str) -> None:
        super().__init__(
            f"This is the start of {channel_label} channel history",
            id="start-of-channel-history",
            markup=False,
        )


# U+27F2 ANTICLOCKWISE GAPPED CIRCLE ARROW -- a plain, Narrow-width
# text glyph (never emoji-presentation), replacing the literal word
# "RESEND". Bracketed exactly like DEL's own "[ DEL ]" -- the same
# action-control grammar for both, "[ ⟲ ]" -- not a bare glyph.
RESEND_GLYPH = "⟲"
MESSAGE_ACTION_LABELS: dict[str, str] = {
    "resend": f"[ {RESEND_GLYPH} ]",
    "delete": "[ DEL ]",
}


class MessageActionControl(Static):
    """A contextual action beneath a message; ready for future action kinds.

    Vertical CHAT navigation only ever stops on the "resend" control
    (see MeshtasticPassApp._chat_navigation_targets) -- "delete" is
    reachable only by an explicit horizontal move (see on_key below),
    never a vertical one, so UP/DOWN can never land on DEL directly.
    `paired_control` links the two action controls for one message to
    each other (set once, right after both are constructed -- see
    ChatEntryWidget.__init__), letting each one move focus to its
    sibling with no separate lookup/query needed.
    """

    can_focus = True

    class Activated(Message):
        def __init__(self, control: "MessageActionControl") -> None:
            super().__init__()
            self.action_control = control

    def __init__(self, entry: ChatEntry, action: str = "resend") -> None:
        self.entry = entry
        self.action = action
        self.paired_control: "MessageActionControl | None" = None
        super().__init__(
            MESSAGE_ACTION_LABELS[action],
            classes="message-action chat-nav-target",
            markup=False,
        )

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Activated(self))
            event.stop()
        elif event.key == "right" and self.action == "resend":
            # RIGHT from RESEND -> DEL, one deliberate move away --
            # never triggered by navigation alone (see item 6). Stops
            # here so the transcript's own RIGHT ("jump to present and
            # type") never fires for this specific focus target.
            if self.paired_control is not None:
                self.paired_control.focus()
            event.stop()
        elif event.key == "left" and self.action == "delete":
            if self.paired_control is not None:
                self.paired_control.focus()
            event.stop()


class ChatTranscript(VerticalScroll):
    """Focusable transcript so navigation never types into the message box."""

    can_focus = True

    def on_mount(self) -> None:
        self.vertical_scrollbar.renderer = ThinScrollBarRender
        self.anchor()

    def on_key(self, event: Key) -> None:
        """Keep transcript navigation deterministic when this widget has focus."""
        app = self.app
        if getattr(app, "_user_menu", None) is not None:
            # A node-context menu (e.g. opened via Enter on this exact
            # widget) is showing -- up/down must move ITS highlight
            # (handled by the app-level on_key this event still bubbles
            # to), not the chat message selection underneath it. Focus
            # never actually moves off this ChatEntryWidget/transcript
            # while the menu is open (see _open_node_menu), so without
            # this guard up/down would always be captured here first.
            return
        if event.key in ("up", "down"):
            app._move_chat_focus(-1 if event.key == "up" else 1)
            event.stop()
        elif event.key in ("pageup", "pagedown"):
            step = max(1, self.region.height - 2)
            self.scroll_relative(y=-step if event.key == "pageup" else step)
            app.call_after_refresh(app._clear_indicator_if_at_bottom)
            event.stop()
        elif event.key == "left":
            app._focus_oldest_new_message()
            event.stop()
        elif event.key == "right":
            app._return_to_present_and_type()
            event.stop()
        elif event.key == "end":
            app._jump_to_newest()
            event.stop()


class ChatMessageInput(Input):
    """The CHAT draft input; posts Left so the app can react to focus

    loss without needing to know every possible reason it happened
    (Escape, a tab switch, a mouse click elsewhere) -- see the
    empty-message error's auto-dismiss-on-focus-loss requirement.
    """

    class Left(Message):
        pass

    def on_focus(self, _event: Focus) -> None:
        self.app._update_footer()

    def on_blur(self, _event: Blur) -> None:
        self.post_message(self.Left())
        # The emoji picker is subordinate to composer focus -- ANY way
        # this widget loses focus (tab switch, another control taking
        # focus, etc.) must dismiss it. Re-focusing the composer later
        # never reopens it on its own; Ctrl+E is required again.
        picker = getattr(self.app, "_emoji_picker", None)
        if picker is not None:
            self.app._close_emoji_picker()

    def on_key(self, event: Key) -> None:
        """Intercept the emoji picker's keys (and Ctrl+E to open it)

        directly on this widget -- NOT in the App's own on_key. Input
        already binds "enter" (submit) and "ctrl+e"/"left"/"right"
        (end-of-line/cursor movement) itself; Textual checks a focused
        widget's own on_key BEFORE its inherited key bindings, so this
        is the only place that can reliably preempt those defaults
        while this widget has focus (see items 18 and 23).
        """
        app = self.app
        picker = getattr(app, "_emoji_picker", None)
        if picker is not None:
            if event.key == "left":
                picker.move_highlight(-1)
                event.stop()
            elif event.key == "right":
                picker.move_highlight(1)
                event.stop()
            elif event.key == "enter":
                emoji = picker.selected_emoji
                app._close_emoji_picker()
                app._insert_emoji_at_cursor(emoji)
                event.stop()
            elif event.key == "escape":
                app._close_emoji_picker()
                event.stop()
            elif event.key == "up":
                # Dismiss, then let the SAME keypress continue on to the
                # App's own "up leaves the composer" handling below --
                # never swallow UP merely to close the picker.
                app._close_emoji_picker()
            return
        if event.key == "ctrl+e":
            app._open_emoji_picker()
            event.stop()


class ConnectionPage(VerticalScroll):
    """Scrollable settings surface for short uConsole terminal windows."""

    def on_mount(self) -> None:
        self.vertical_scrollbar.renderer = ThinScrollBarRender


CIRCLE_SOLID_LARGE = "●"
CIRCLE_STROKED_LARGE = "○"

# --- MESH: real passive-data visualization on a responsive viewport --------
#
# A viewport into a logical topology that may be LARGER than the
# currently visible terminal grid, driven entirely by real, passively
# observed Meshtastic data -- no LoRa traffic is ever generated to
# populate or refresh it (see _refresh_mesh). Real nodes are placed by
# coarse compass direction/distance ranking when GPS is available
# (never exact, proportional geography), spread to use more of the
# visible grid's available room, and a real CLIENT's truthful nonzero
# hop count renders as that many anonymous relay-stage placeholders
# along its path to YOU (see mesh_topology.RelayStage) -- visual/
# topology decoration only, never selectable, focusable, or a
# navigation candidate. The visible grid's own row/column count is
# computed fresh from the MESH view's actual rendered size every
# refresh (see _compute_mesh_grid_dimensions), never a hardcoded
# per-font-size table -- a node whose logical position falls outside
# that grid renders as an edge indicator instead of being force-fit
# inside it (see mesh_topology.project_to_viewport). Intentionally out
# of scope: Favorites, dynamic node-count growth beyond the bounded
# working set, and free-form scrolling (navigation is always node-to-
# node via the arrow keys, never a scrollable viewport). See
# mesh_state.py for the working-set/role/staleness/distance model and
# mesh_topology.py for the pure grid geometry (assign_grid_slots(),
# place_within_bounds(), project_to_viewport(), directional_target(),
# build_relay_stages(), route_chain()) reused here.
MESH_GRID_MIN_ROWS = 5
MESH_GRID_MIN_COLUMNS = 9
# A node's label renders one terminal row ABOVE its glyph (see
# set_nodes) -- reserving this one row of headroom at the top of the
# computed grid keeps a glyph from ever being placed so close to
# #mesh-view's own top edge that its label would render outside this
# container entirely.
MESH_GRID_LABEL_MARGIN_ROWS = 1
# The LOGICAL topology's own bounded extent (assign_grid_slots()'s
# `max_radius`/min_radius_by_id-boosted grid steps, mapped onto real
# row/column coordinates by place_within_bounds() in _refresh_mesh) --
# a FIXED coordinate space, entirely independent of the current
# VIEWPORT's own row/column count (see
# MeshTopologyView.current_grid_dimensions/on_resize, which never
# touch these). A live viewport resize can only ever clip/reveal more
# or less of this SAME stable layout afterward (see
# mesh_topology.project_to_viewport) -- never re-derive it from a
# different origin (see item 11/12 of the responsive-viewport task).
#
# Deliberately NOT dramatically larger than a typical viewport:
# place_within_bounds() actively STRETCHES the lone farthest node on
# each half-axis to use all available room up to this bound (see its
# own docstring) -- an enormous bound here would stretch even a single
# nearby remote node far out into empty space for no rendering
# benefit, making nearly everything an edge indicator regardless of
# viewport size. Kept at the same size that comfortably filled the
# previous fixed 8x21 board -- still genuinely independent of the
# viewport (a SMALL-font viewport, computed larger, can now reveal
# this entire logical extent at once with room to spare; an XL-sized
# one, computed smaller, or a client legitimately boosted farther out
# by a large truthful hop count, can genuinely exceed it and need edge
# indicators -- see item 26's tests).
MESH_LOGICAL_GRID_ROWS = 8
MESH_LOGICAL_GRID_COLUMNS = 21
MESH_LOGICAL_GRID_CENTER_ROW = 5
MESH_LOGICAL_GRID_CENTER_COLUMN = 11
# Selected-node "visually larger" treatment: a 3-cell-wide composite
# (small dot + role glyph + small dot) replacing the ordinary 1-cell
# glyph -- see MeshNodeWidget.refresh_visual for why bold alone wasn't
# enough and why this stays a reliable-width text composite rather than
# an ambiguous-width "big circle" Unicode glyph.
MESH_SELECTED_GLYPH_WIDTH = 3
MESH_SELECTED_HALO_GLYPH = "·"
# The label physically above a node's glyph is a compact hint, not the
# full identity -- that lives in the unified bottom bar (see
# mesh_state.format_mesh_node_bar_fields, which always has the full Long
# Name/Short Name, uncapped). Capped in DISPLAY CELLS (cell_len()), not
# Python len(), so wide/CJK/emoji glyphs are counted by their actual
# terminal width -- see mesh_topology.mesh_board_marker_label/_truncate
# for the grapheme-safe truncation this limit is applied through (this
# is the NAME portion's own budget -- TRACE ROUTE's "<marker> " prefix
# adds two more cells on top, never shrinking the name itself).
MESH_BOARD_LABEL_MAX_CELLS = 5


@dataclass(frozen=True)
class ActiveTraceroute:
    """TRACE ROUTE Part C: the ONE currently in-flight traceroute (v1

    allows exactly one at a time). `request_token` -- an app-local
    sequence number assigned synchronously in _start_traceroute, BEFORE
    any RF request is even sent -- is the sole correlation key: it is
    known before the background worker starts, so a response (or a
    same-thread/synchronous simulated one) can never race its own
    assignment. `destination_short_name` is captured once, at request
    time, for the "TRACING ROUTE TO <name>" status text -- never a live
    re-lookup, so it can never go stale/blank if the destination's own
    NodeDB record changes or the node temporarily drops out of the
    working set mid-trace.
    """

    request_token: int
    destination_node_id: str
    destination_short_name: str


@dataclass(frozen=True)
class TracerouteBanner:
    """The terminal TRACE SUCCEEDED/TRACE FAILED status text, shown in

    #mesh-status for TRACEROUTE_BANNER_SECONDS before the normal status
    (NO MESH DATA / blank) automatically returns -- see
    MeshtasticPassApp._show_traceroute_banner/_dismiss_traceroute_banner.
    """

    text: str
    style_kind: str  # "accent" (TRACE SUCCEEDED) or "error" (TRACE FAILED)


# TRACE ROUTE's "TRACING ROUTE TO SHN  > > >" animation reuses the
# EXACT visual language of CHAT's own SENDING arrows (see
# SENDING_ARROW_FRAMES/_sending_arrows_text above): active = ACCENT,
# inactive = the SAME dim_quarter token SENDING's own inactive arrow
# uses. Three positions (not two, unlike SENDING) -- one active arrow
# cycles 0 -> 1 -> 2 -> 0, advanced by the SAME pre-existing 0.45s
# _delivery_timer/_advance_delivery_states tick SENDING already uses,
# never a new timer (see _advance_delivery_states' own extension).
TRACEROUTE_ARROW_GLYPH = ">"
TRACEROUTE_ARROW_POSITIONS = 3
# A UI-appropriate hard ceiling -- deliberately NOT the Meshtastic SDK's
# own sendTraceRoute/waitForTraceRoute helper's blocking timeout (up to
# hopLimit+1 multiples of a 300s base -- see RadioService.
# send_traceroute's own docstring), which is designed for a CLI script
# willing to wait indefinitely, not an interactive TUI. Long enough for
# a real multi-hop LoRa round trip, short enough that "TRACING ROUTE"
# never appears stuck.
TRACEROUTE_TIMEOUT_SECONDS = 30.0
# "Only an actual successful traceroute response counts as success... in
# ACCENT for 10 seconds, then restore the normal top-left status" -- the
# literal duration the spec gives, for both TRACE SUCCEEDED and TRACE
# FAILED.
TRACEROUTE_BANNER_SECONDS = 10.0


def _mesh_node_color(state: MeshNodeState, *, selected: bool, theme: str, now: float) -> str:
    """YOU is ALWAYS ACCENT2 -- a persistent identity anchor, entirely

    independent of selection (see MESH FOLLOW-UP items 16-18): selecting
    YOU never recolors it to ACCENT, and selecting a remote node never
    recolors YOU either. Checked FIRST, before selection, so it can
    never be overridden.

    For a remote node, selection (ACCENT) overrides active/inactive
    styling. A remote node's brightness otherwise uses the EXACT SAME
    predicate as the MESH header's "ACTIVE N" count --
    node_activity.is_node_active, keyed on RadioService's passive
    last_heard -- so the two always visually agree: if the header says
    ACTIVE 4, exactly the working set's real remote nodes satisfying
    this same predicate render BASE. This is deliberately a different
    concept from MeshNodeState.is_stale() (>24h since the last CHAT
    interaction), which decides which nodes are worth ranking into the
    working set at all (see mesh_state.build_mesh_working_set) -- not
    how bright an already-displayed node looks. A node can therefore
    show a merely-old interaction time ("2h") while still rendering
    dim, and that is expected.

    Node identity color and selected-ROUTE color (see
    MeshTopologyView's connector-painting logic) are deliberately
    independent: YOU's own connector may still paint ACCENT when YOU is
    the current selection, while the YOU glyph/label themselves stay
    ACCENT2 throughout -- ACCENT2 is a persistent identity anchor,
    ACCENT is current selection/route emphasis, and the two are allowed
    to differ on the very same node.
    """
    palette = THEME_PALETTES[theme]
    if state.node.is_local:
        return palette.accent2
    if selected:
        return palette.accent
    if not is_node_active(state.node.last_heard, now):
        return palette.dim_base
    return palette.base


def _mesh_relay_color(theme: str) -> str:
    """An anonymous relay-stage placeholder is visual topology only: it

    has no identity to be "heard" from, is never active/inactive, and
    (unlike a real node) is never selectable, so it is always DIM_BASE
    -- never ACCENT, regardless of theme, activity, or selection state
    anywhere else on the board.
    """
    return THEME_PALETTES[theme].dim_base


# A STALE connector's straight runs render dashed (see item 5 of the
# MESH activity-model task: "STALE connection: DIM + DOTTED"), reusing
# the SAME box-drawing weight (LIGHT) as the solid glyphs they replace
# so the dash pattern reads as "the same kind of line, aged" rather
# than a visually unrelated style. Elbow corners are left as their
# solid glyph unchanged -- a "dashed corner" has no single-cell
# box-drawing equivalent, and one corner cell alone does not read as
# meaningfully dotted either way.
_MESH_DASHED_CONNECTOR_GLYPHS = {"─": "╌", "│": "╎"}


def _mesh_dashed_glyph(glyph: str) -> str:
    return _MESH_DASHED_CONNECTOR_GLYPHS.get(glyph, glyph)


def _mesh_grid_pixel(row: int, column: int) -> tuple[int, int]:
    """Convert a 1-indexed logical grid position to a pixel coordinate,

    aligned exactly to a dot-grid intersection (DOT_GRID_SPACING_X/Y below).
    """
    return (column - 1) * DOT_GRID_SPACING_X, (row - 1) * DOT_GRID_SPACING_Y


def _mesh_translated_positions(
    base_positions: Mapping[str, tuple[int, int]],
    selected_node_id: str,
    *,
    center_row: int,
    center_column: int,
) -> dict[str, tuple[int, int]]:
    """Translate the whole current layout so the selected node sits at the

    given center grid position (the CURRENT viewport's own center --
    see MeshTopologyView.current_grid_dimensions, computed fresh from
    the view's actual rendered size, never a fixed constant). This is a
    pure whole-mesh translation: every node shifts by the same row/
    column delta, so relative geometry between nodes never changes --
    it is never an independent per-node recomputation, and it never
    touches `base_positions` (the working set's fixed geographic/
    fallback layout, itself independent of the viewport entirely) --
    only a later clip into the visible grid (see
    mesh_topology.project_to_viewport) can move a node off this exact
    translated position, never this function.
    """
    selected = base_positions.get(selected_node_id)
    if selected is None:
        return dict(base_positions)
    row_delta = center_row - selected[0]
    column_delta = center_column - selected[1]
    return {
        node_id: (row + row_delta, column + column_delta)
        for node_id, (row, column) in base_positions.items()
    }


def _mesh_directional_target(
    base_positions: Mapping[str, tuple[int, int]],
    current_node_id: str,
    direction: str,
) -> str | None:
    """Pick the ID reached by an arrow press, reusing the shared

    spatial-navigation rule (mesh_topology.directional_target) against
    the CURRENT fixed, untranslated LOGICAL (row, column) positions --
    not rendered pixel positions. DOT_GRID_SPACING_X/Y (4x2) make one
    logical row-step visually shorter than one column-step on screen, a
    purely cosmetic choice; ranking by pixel distance would let that
    asymmetry distort "sensible direction". Direction is a property of
    the mesh's logical geometry, not of wherever the selection happens
    to be recentered on screen.

    Candidates come directly from whatever `base_positions` the caller
    passes -- e.g. _move_mesh_focus deliberately excludes anonymous
    relay-stage IDs before calling this, since a relay stage is visual
    topology only and must never become a navigation target -- rather
    than from any node-role data baked into this function itself. No
    node IDs are hardcoded: this is the same general nearest-candidate
    rule for whatever position set the caller provides.
    """
    layout = TopologyLayout(
        tuple(
            PositionedNode(node=NodeMetadata(node_id), x=column, y=row, region="UNKNOWN")
            for node_id, (row, column) in base_positions.items()
        ),
        width=MESH_LOGICAL_GRID_COLUMNS,
        height=MESH_LOGICAL_GRID_ROWS,
    )
    target = directional_target(current_node_id, layout, direction)  # type: ignore[arg-type]
    return target.node.node_id if target is not None else None


def _mesh_hop_counts(working_set: tuple[MeshNodeState, ...]) -> dict[str, int]:
    """Real remote CLIENTs with a trustworthy, nonzero hop count -- used

    ONLY for grid PLACEMENT (see _refresh_mesh's min_radius_by_id):
    every such node, active or stale, reserves enough outward room for
    its potential relay chain, so a node's position never needs to jump
    outward the moment it later becomes active (see item 5 -- placement
    stability is independent of current activity). An unknown hop count
    (hops_away is None) is deliberately absent here and below: it must
    never be treated as zero or imply any specific path depth.

    NOT used for deciding which nodes actually render a relay chain or
    connector -- see _mesh_active_hop_counts for that.
    """
    return {
        state.node.node_id: state.node.hops_away
        for state in working_set
        if not state.node.is_local
        and state.node.hops_away is not None
        and state.node.hops_away > 0
    }


def _mesh_active_hop_counts(
    working_set: tuple[MeshNodeState, ...], *, now: float
) -> dict[str, int]:
    """Real remote CLIENTs with a trustworthy, nonzero hop count AND

    currently active (is_node_active(last_heard, now) -- the same
    predicate the board's BASE/DIM_BASE styling and [3] MESH (N) use).

    This -- not _mesh_hop_counts -- is what decides which nodes get an
    anonymous relay chain and a connector line drawn back to YOU (see
    MeshTopologyView.set_nodes): a stale node remains visible at its
    last-known position as useful historical context, but the board
    should never look like it is CURRENTLY connected to something it
    hasn't heard from recently. Active connectivity is a rendering-only
    decision, made fresh every refresh -- it never affects grid
    placement (see _mesh_hop_counts), so a node's position stays stable
    across an activity transition.
    """
    return {
        state.node.node_id: state.node.hops_away
        for state in working_set
        if not state.node.is_local
        and state.node.hops_away is not None
        and state.node.hops_away > 0
        and is_node_active(state.node.last_heard, now)
    }


class MeshNodeWidget(Static):
    """The node's glyph: a single cell, anchored exactly on its grid

    coordinate. The glyph's own screen position must never depend on its
    label's width -- see MeshNodeLabelWidget for the label, a separately
    positioned overlay above this glyph, never the other way around.

    (A single two-line Text with justify="center" was tried here first,
    relying on Rich's own per-line centering to align the 1-cell glyph
    line under the wider label line. That does not hold once Textual
    composites the Static's content into the screen buffer -- the short
    line renders flush against the box's left edge instead of centered,
    silently shifting the glyph off its grid coordinate. Two independently
    positioned single-line widgets sidesteps that entirely: neither box is
    ever wider than its own content, so there is no centering decision left
    for Textual's renderer to get wrong.)

    Selection state lives on MeshTopologyView, not Textual's focus system:
    rendering always reflects the current selection synchronously, so there
    is no second, focus-driven source of visual selection state to keep in
    sync. Textual focus is not used here at all -- mouse clicks are routed
    by position, not focus, and keyboard arrow routing is handled entirely
    at the App level against MeshTopologyView.selected_node_id.
    """

    def __init__(self, state: MeshNodeState) -> None:
        self.state = state
        self.node_id = state.node.node_id
        super().__init__(classes="mesh-node", markup=False)

    def refresh_visual(
        self, *, selected: bool, theme: str, now: float, traced: bool = False
    ) -> None:
        color = _mesh_node_color(self.state, selected=selected, theme=theme, now=now)
        # The glyph shape itself is never altered by selection -- ACTIVE
        # (solid) vs stale (stroked) stays the authoritative visual state
        # regardless of selection. This is the EXACT SAME predicate as
        # _mesh_node_color's BASE/DIM_BASE split and [3] MESH (N)'s count
        # (is_node_active), not CLIENT/is_client -- a real node admitted
        # purely from passive NodeDB data (never having sent a CHAT
        # message) is otherwise indistinguishable in shape from an
        # anonymous relay stage while active, which is exactly what made
        # a genuinely active endpoint look like a relay chain dead-end
        # (see MeshRelayWidget, always stroked/unlabeled). YOU has no
        # activity concept and is always solid.
        #
        # UI / CHANNEL / RADIO CONFIG TUNING Part A: session-local
        # successful-traceroute evidence (see MeshTopologyView.
        # mark_traced) replaces this ENTIRE glyph -- shape and color --
        # with the plain "*" in existing successful-trace/ACCENT color,
        # taking priority over the ACTIVE/stale distinction (a
        # successful trace is itself stronger, more recent evidence
        # than passive last-heard staleness). This never moves the
        # glyph's own (grid_x, grid_y) anchor or its selected-composite
        # width -- only the single character/color drawn there.
        if traced:
            glyph = "*"
            color = THEME_PALETTES[theme].accent
        else:
            glyph = (
                CIRCLE_SOLID_LARGE
                if self.state.node.is_local or is_node_active(self.state.node.last_heard, now)
                else CIRCLE_STROKED_LARGE
            )
        style = Style(color=color, bold=selected)
        if selected:
            # Bold alone reads as barely-different on many terminals, so
            # the anchor cell's role glyph is flanked by a small dot in
            # each immediately neighboring cell on the same row -- a real,
            # ~3x wider visual footprint, not a font-weight trick. The
            # widget's own width/offset (set in MeshTopologyView.set_nodes) keep the
            # center *column* of this 3-cell composite exactly on the
            # glyph's (grid_x, grid_y) anchor, so growing it can never
            # move that coordinate.
            content = Text(justify="center")
            content.append(MESH_SELECTED_HALO_GLYPH, style=style)
            content.append(glyph, style=style)
            content.append(MESH_SELECTED_HALO_GLYPH, style=style)
        else:
            content = Text(glyph, style=style)
        self.update(content)

    def on_click(self, _event: Click) -> None:
        _mesh_select_node(self.app, self.node_id)


class MeshNodeLabelWidget(Static):
    """The node's label: a separately positioned overlay above its glyph.

    Always exactly cell_len(label) cells wide -- its own box is never wider
    than its content, so it needs no internal centering, only a computed
    offset (see MeshTopologyView.set_nodes). Never influences the
    glyph's own coordinate; see MeshNodeWidget.
    """

    def __init__(self, state: MeshNodeState) -> None:
        self.state = state
        self.node_id = state.node.node_id
        super().__init__(classes="mesh-node", markup=False)

    def refresh_visual(
        self, *, selected: bool, theme: str, now: float, highlighted: bool = False
    ) -> None:
        # UI / CHANNEL / RADIO CONFIG TUNING Part A: the label is now
        # always the bare name in this node's ordinary ACTIVE/STALE/
        # selected color -- no marker prefix, no traced-specific
        # branch. Successful-traceroute evidence is shown entirely on
        # the GRID GLYPH one cell below (see MeshNodeWidget.
        # refresh_visual) instead, so there is no second color/glyph
        # decision to make here any more. A HIGHLIGHTED (local favorite)
        # node's NAME renders in ACCENT2 -- presentation only, applied
        # to the label, never the glyph/topology geometry below.
        color = (
            THEME_PALETTES[theme].accent2
            if highlighted
            else _mesh_node_color(self.state, selected=selected, theme=theme, now=now)
        )
        label = mesh_board_marker_label(
            self.state.node, max_name_cells=MESH_BOARD_LABEL_MAX_CELLS
        )
        self.update(Text(label, style=Style(color=color)))

    def on_click(self, _event: Click) -> None:
        _mesh_select_node(self.app, self.node_id)


class MeshRelayWidget(Static):
    """An anonymous relay-stage placeholder glyph: visual topology only.

    Always a stroked, DIM_BASE, unlabeled 1-cell glyph -- see
    mesh_topology.RelayStage. Deliberately NOT interactive: no on_click
    (a click here does nothing), can_focus is False, and it is excluded
    from the arrow-navigation candidate set entirely (see
    MeshtasticPassApp._move_mesh_focus) -- it can never become
    selected_node_id, never shows ACCENT or the enlarged selected
    composite, and never appears in the bottom-left context. A hollow
    dot here means only "an unidentified relay stage exists between two
    real, inspectable nodes" -- never a thing to inspect itself.
    """

    can_focus = False

    def __init__(self, stage: RelayStage) -> None:
        self.stage = stage
        self.node_id = stage.node_id
        super().__init__(classes="mesh-node", markup=False)

    def refresh_visual(self, *, theme: str) -> None:
        self.update(Text(CIRCLE_STROKED_LARGE, style=Style(color=_mesh_relay_color(theme))))


def _mesh_select_node(app: MeshtasticPassApp, node_id: str) -> None:
    view = app.query_one(MeshTopologyView)
    view.select_node(node_id)
    view.set_nodes(view.working_set, view.base_positions, theme=app._current_theme, now=time())
    app._update_mesh_node_bar(view.working_set, time())


DOT_GRID_GLYPH = "·"
# 4x2 keeps a 2:1 x:y ratio (so the grid reads as roughly square against
# typical terminal cell proportions) between one logical grid step and
# its rendered terminal-cell size -- this ratio itself never changes
# with font size or viewport size (see item 3 of the responsive-
# viewport task: glyphs are never scaled to fill space; the GRID gains
# or loses cells instead).
DOT_GRID_SPACING_X = 4
DOT_GRID_SPACING_Y = 2


def _compute_mesh_grid_dimensions(
    view_width: int, view_height: int
) -> tuple[int, int, int, int]:
    """Derive (rows, columns, center_row, center_column) for the

    visible MESH grid from the MESH viewport's OWN actual rendered
    size -- never a hardcoded per-font-size table (see
    MeshTopologyView.current_grid_dimensions, this function's only
    caller). #mesh-view already flexes to fill whatever space remains
    once the top connection/status line and the bottom selected-node
    context line (both separate sibling widgets outside this
    container -- see MeshtasticPassApp.compose) take their own rows,
    so `view_width`/`view_height` -- that container's own self.size --
    already IS "the available MESH content area", with no further
    reservation needed for those two specifically.

    MESH_GRID_LABEL_MARGIN_ROWS is reserved from the row count for
    label headroom (see set_nodes). Dimensions are forced ODD so there
    is always an exact, unambiguous center cell for YOU to occupy when
    the viewport is YOU-centered, floored at MESH_GRID_MIN_ROWS/
    MESH_GRID_MIN_COLUMNS so a not-yet-laid-out or pathologically small
    container never produces a degenerate 0- or 1-cell grid. A pure
    function of its two inputs: unchanged geometry always yields
    identical dimensions, and nothing about node activity, selection,
    or connectors ever feeds into it.
    """
    columns = max(MESH_GRID_MIN_COLUMNS, view_width // DOT_GRID_SPACING_X)
    if columns % 2 == 0:
        columns -= 1
    usable_rows = (
        max(1, view_height // DOT_GRID_SPACING_Y) - MESH_GRID_LABEL_MARGIN_ROWS
    )
    rows = max(MESH_GRID_MIN_ROWS, usable_rows)
    if rows % 2 == 0:
        rows -= 1
    center_row = rows // 2 + 1
    center_column = columns // 2 + 1
    return rows, columns, center_row, center_column


def _render_mesh_canvas(
    width: int,
    height: int,
    connectors: tuple[tuple[int, int, str, str], ...],
    dot_color: str,
) -> Text:
    """Build one procedural Text for the whole canvas: dot grid plus lines.

    Never one widget per dot or per line segment. `connectors` is a list of
    (x, y, glyph, color) cells that override the dot grid at that position,
    e.g. a YOU-to-node relationship line drawn by mesh_topology.route_connector.
    """
    if width <= 1 and height <= 1:
        return Text("")
    overlay = {(x, y): (glyph, color) for x, y, glyph, color in connectors}
    text = Text()
    for y in range(height):
        if y:
            text.append("\n")
        x = 0
        while x < width:
            if (x, y) in overlay:
                glyph, color = overlay[(x, y)]
                text.append(glyph, style=Style(color=color))
                x += 1
                continue
            run_end = x
            while run_end < width and (run_end, y) not in overlay:
                run_end += 1
            segment = "".join(
                DOT_GRID_GLYPH
                if column % DOT_GRID_SPACING_X == 0 and y % DOT_GRID_SPACING_Y == 0
                else " "
                for column in range(x, run_end)
            )
            text.append(segment, style=Style(color=dot_color))
            x = run_end
    return text


class MeshCanvas(Static):
    """Static, non-interactive procedural background: dot grid plus connectors.

    One widget covers the entire bounded board (never one widget per dot or
    per line segment), so it stays cheap regardless of topology size.
    """

    can_focus = False

    def __init__(self) -> None:
        super().__init__(classes="mesh-canvas", markup=False)
        self._signature: tuple[object, ...] | None = None

    def render_scene(
        self,
        width: int,
        height: int,
        connectors: tuple[tuple[int, int, str, str], ...],
        theme: str,
    ) -> None:
        signature = (width, height, connectors, theme)
        if signature == self._signature:
            return
        self._signature = signature
        self.styles.width = width
        self.styles.height = height
        self.update(
            _render_mesh_canvas(width, height, connectors, THEME_PALETTES[theme].grid_dot)
        )


class MeshTopologyView(Container):
    """A responsive VIEWPORT into a MESH working set that may be larger

    than the currently visible terminal grid (see
    current_grid_dimensions/mesh_topology.project_to_viewport). No
    scrolling, no scrollbars -- navigation is always node-to-node via
    the arrow keys (see MeshtasticPassApp._move_mesh_focus); a node
    whose translated position falls outside the visible grid renders
    as an edge indicator instead. Node identity, positions
    (base_positions, from mesh_topology.assign_grid_slots() +
    place_within_bounds() -- STABLE, viewport-independent logical
    coordinates) and roles all come from the current working set passed
    to set_nodes(); this view only lays out, clips, and renders them.
    """

    can_focus = True

    def __init__(self) -> None:
        super().__init__(
            Container(MeshCanvas(), id="mesh-board"),
            id="mesh-view",
        )
        self._selected_node_id = ""
        self._working_set: tuple[MeshNodeState, ...] = ()
        self._base_positions: dict[str, tuple[int, int]] = {}
        self._relay_stages: tuple[RelayStage, ...] = ()
        self._edge_node_ids: frozenset[str] = frozenset()
        self._last_now: float = 0.0
        # TRACE ROUTE (Part C): canonical node IDs with successful
        # traceroute evidence during THIS app session -- persistent
        # view-level state, exactly like _selected_node_id, so it
        # survives every ordinary set_nodes() refresh and every tab
        # switch, and is never cleared except by an app restart (never
        # erased by a later FAILED trace -- see MeshtasticPassApp.
        # _finish_traceroute_success, this set's only writer).
        self._traced_node_ids: frozenset[str] = frozenset()

    @property
    def board(self) -> Container:
        return self.query_one("#mesh-board", Container)

    @property
    def selected_node_id(self) -> str:
        return self._selected_node_id

    @property
    def working_set(self) -> tuple[MeshNodeState, ...]:
        return self._working_set

    @property
    def base_positions(self) -> dict[str, tuple[int, int]]:
        """Real-node AND anonymous-relay-stage STABLE logical (row,

        column) positions, merged -- the fixed layout produced by
        assign_grid_slots()/place_within_bounds()/build_relay_stages(),
        entirely independent of the current viewport/selection (see
        mesh_topology.project_to_viewport, applied only at render time
        in set_nodes -- never here). This is NOT the arrow-navigation
        candidate set: a RelayStage's ID is included here (its glyph
        must be positioned and translated exactly like a real node's)
        but is never itself navigable -- see
        MeshtasticPassApp._move_mesh_focus, which explicitly filters
        every RelayStage ID out of this dict before calling
        _mesh_directional_target, so only real working-set nodes are
        ever navigation candidates.
        """
        return self._base_positions

    @property
    def relay_stages(self) -> tuple[RelayStage, ...]:
        return self._relay_stages

    @property
    def edge_node_ids(self) -> frozenset[str]:
        """Real working-set node IDs currently rendered as an edge

        indicator rather than normally -- i.e. their translated
        position fell outside the visible viewport this render and was
        clipped onto its boundary (see mesh_topology.
        project_to_viewport). Never includes anonymous relay-stage IDs:
        those are excluded from this set even when their own position
        was likewise clipped, since a relay stage is never a selectable
        "edge" concept, just clipped visual topology (see set_nodes).
        """
        return self._edge_node_ids & {state.node.node_id for state in self._working_set}

    def current_grid_dimensions(self) -> tuple[int, int, int, int]:
        """(rows, columns, center_row, center_column) for the visible

        grid, computed fresh from this container's OWN actual rendered
        size (see _compute_mesh_grid_dimensions) -- never a hardcoded
        per-font-size table. Calling this repeatedly with no layout
        change in between always returns the identical result: it is a
        pure function of self.size, never of activity, selection, or
        connectors.
        """
        return _compute_mesh_grid_dimensions(self.size.width, self.size.height)

    def on_resize(self) -> None:
        """The MESH viewport's available terminal-cell area just

        changed (a real terminal resize, or -- in the real deployment
        -- reopening after a font-size change reconfigured how many
        cells fit; see item 24 of the responsive-viewport task) --
        relayout immediately against the new size rather than waiting
        for the next unrelated refresh.

        Re-renders the SAME already-known working set/positions against
        the new size, reusing the exact "now" set_nodes was last called
        with (`_last_now`) rather than sampling a fresh, unsynchronized
        time() here: a resize is a pure LAYOUT event and must only ever
        change screen coordinates, never re-derive activity/counts from
        a different "now" than the rest of the current refresh cycle
        used (see _refresh_mesh's own "computed exactly ONCE per cycle"
        principle -- this must not become a second, independent source
        of "now"). A no-op before the working set has ever been
        populated (nothing to relayout yet, and no "now" to reuse).
        """
        if self._working_set:
            self.set_nodes(
                self._working_set,
                self._base_positions,
                theme=self.app._current_theme,
                now=self._last_now,
            )

    def set_nodes(
        self,
        working_set: tuple[MeshNodeState, ...],
        base_positions: Mapping[str, tuple[int, int]],
        *,
        theme: str,
        now: float,
    ) -> None:
        """Render the current working set, recentered on the selection.

        Preserves the current selection if it is still in the working
        set; otherwise falls back to YOU (or the first node, if somehow
        there is no local node). Widgets are added/removed only for
        nodes that actually entered/left the working set -- unchanged
        nodes keep their existing widget, so unchanged data never
        reshuffles or remounts anything.

        The visible grid's own dimensions are recomputed fresh from
        this container's actual current size every call (see
        current_grid_dimensions) -- a resize (real terminal resize, or
        a later call after a font-size-driven relaunch) is picked up
        automatically the next time this runs, with no separate
        per-font-size table anywhere.
        """
        self._last_now = now
        board = self.board
        row_count, column_count, center_row, center_column = (
            self.current_grid_dimensions()
        )
        board_width = column_count * DOT_GRID_SPACING_X
        board_height = row_count * DOT_GRID_SPACING_Y
        board.styles.width = board_width
        board.styles.height = board_height
        # Horizontally center the whole board as one rigid block inside the
        # available MESH region -- a board-level offset, not a per-node one,
        # so it can never desync node-to-grid coordinates. On the very first
        # render (before this view has ever been laid out), self.size is
        # not resolved yet (0x0); re-run once after the next refresh, when
        # it is, rather than leaving the board visibly left-anchored.
        view_width = self.size.width
        if view_width:
            board.styles.offset = (max(0, (view_width - board_width) // 2), 0)
        else:
            self.app.call_after_refresh(
                lambda: self.set_nodes(working_set, base_positions, theme=theme, now=now)
            )

        self._working_set = working_set
        current_ids = {state.node.node_id for state in working_set}
        you_id = next(
            (state.node.node_id for state in working_set if state.node.is_local),
            None,
        )
        # `base_positions` is expected to carry only real-node positions
        # (exactly what place_within_bounds() produces), but is filtered
        # against `current_ids` regardless -- so passing back a previous
        # call's already-merged view.base_positions (which also contains
        # relay-stage entries) is harmless: those extra keys are simply
        # ignored here and relay stages are recomputed fresh below, never
        # accumulated across calls.
        real_positions = {
            node_id: position
            for node_id, position in base_positions.items()
            if node_id in current_ids
        }
        # Relay chains render ONLY for currently ACTIVE clients (see
        # _mesh_active_hop_counts) -- a stale node keeps its last-known
        # position (see _mesh_hop_counts, used for placement only, above
        # in _refresh_mesh) but gets no anonymous relay chain: the board
        # answers "what does my radio currently believe is active" via
        # rendered connectors, and "what else does my radio remember"
        # via dim, disconnected real nodes.
        active_hop_counts = _mesh_active_hop_counts(working_set, now=now)
        # Relay-stage interpolation happens in the STABLE logical
        # coordinate space (MESH_LOGICAL_GRID_*), never the current
        # viewport's dynamic row/column count -- a relay chain's
        # placement must not shift merely because the visible grid grew
        # or shrank (see item 12 of the responsive-viewport task); only
        # the later viewport-projection step below may move it on
        # screen.
        relay_stages, relay_positions = build_relay_stages(
            real_positions,
            you_id=you_id or "",
            hop_counts=active_hop_counts,
            row_count=MESH_LOGICAL_GRID_ROWS,
            column_count=MESH_LOGICAL_GRID_COLUMNS,
            # The real per-axis pixel spacing this view actually renders
            # with -- required for build_relay_stages' own self-overlap
            # avoidance to correctly predict what will land on screen
            # (DOT_GRID_SPACING_X/Y are uneven per axis; see
            # mesh_topology.build_relay_stages' own docstring for why
            # that unevenness matters here).
            row_scale=DOT_GRID_SPACING_Y,
            column_scale=DOT_GRID_SPACING_X,
        )
        self._relay_stages = relay_stages
        self._base_positions = {**real_positions, **relay_positions}
        relay_ids = {stage.node_id for stage in relay_stages}
        # Anonymous relay stages are visual topology only -- an anonymous
        # relay ID is never a valid selection, so it falls back to YOU
        # exactly like any other ID that isn't a real working-set member.
        if self._selected_node_id not in current_ids:
            local_id = you_id
            self._selected_node_id = local_id or (
                working_set[0].node.node_id if working_set else ""
            )

        # A real working-set node whose translated position falls
        # outside the current viewport renders as an edge indicator:
        # its glyph still gets the ordinary real-node treatment (see
        # MeshNodeWidget.refresh_visual -- FILLED/BASE or STROKED/
        # DIM_BASE per the same is_node_active predicate as always,
        # never a new "off-screen" color), but it gets no label -- a
        # normal label always implies a normal in-viewport node.
        positions = _mesh_translated_positions(
            self._base_positions,
            self._selected_node_id,
            center_row=center_row,
            center_column=center_column,
        )
        viewport_positions, edge_ids = project_to_viewport(
            positions, row_count=row_count, column_count=column_count
        )
        self._edge_node_ids = edge_ids
        labelable_ids = current_ids - edge_ids

        states_by_id = {state.node.node_id: state for state in working_set}
        stages_by_id = {stage.node_id: stage for stage in relay_stages}
        # Widget.remove() only schedules removal -- it does not take effect
        # before the next refresh -- so every query below must keep
        # filtering by `current_ids` itself rather than assume a removed
        # widget is already gone from self.query().
        for widget in list(self.query(MeshNodeWidget)):
            if widget.node_id not in current_ids:
                widget.remove()
        for widget in list(self.query(MeshNodeLabelWidget)):
            if widget.node_id not in labelable_ids:
                widget.remove()
        for widget in list(self.query(MeshRelayWidget)):
            if widget.node_id not in relay_ids:
                widget.remove()
        existing_glyph_ids = {
            widget.node_id
            for widget in self.query(MeshNodeWidget)
            if widget.node_id in current_ids
        }
        existing_label_ids = {
            widget.node_id
            for widget in self.query(MeshNodeLabelWidget)
            if widget.node_id in labelable_ids
        }
        existing_relay_ids = {
            widget.node_id
            for widget in self.query(MeshRelayWidget)
            if widget.node_id in relay_ids
        }
        new_glyphs = [
            MeshNodeWidget(states_by_id[node_id])
            for node_id in states_by_id
            if node_id not in existing_glyph_ids
        ]
        new_labels = [
            MeshNodeLabelWidget(states_by_id[node_id])
            for node_id in labelable_ids
            if node_id not in existing_label_ids
        ]
        # Anonymous relay-stage placeholders are never labeled -- no
        # MeshRelayLabelWidget counterpart exists (see RelayStage).
        new_relays = [
            MeshRelayWidget(stages_by_id[node_id])
            for node_id in stages_by_id
            if node_id not in existing_relay_ids
        ]
        if new_glyphs:
            board.mount_all(new_glyphs)
        if new_labels:
            board.mount_all(new_labels)
        if new_relays:
            board.mount_all(new_relays)
        glyph_widgets = [
            widget for widget in self.query(MeshNodeWidget) if widget.node_id in current_ids
        ]
        label_widgets = [
            widget
            for widget in self.query(MeshNodeLabelWidget)
            if widget.node_id in labelable_ids
        ]
        relay_widgets = [
            widget for widget in self.query(MeshRelayWidget) if widget.node_id in relay_ids
        ]
        for widget in glyph_widgets:
            widget.state = states_by_id[widget.node_id]
        for widget in label_widgets:
            widget.state = states_by_id[widget.node_id]
        for widget in relay_widgets:
            widget.stage = stages_by_id[widget.node_id]

        centers: dict[str, tuple[int, int]] = {}
        # The glyph is the sole coordinate authority: it is always a 1x1
        # (or, selected, 3x1) widget centered exactly on (grid_x, grid_y),
        # so label width can never influence it. Positioned first so
        # `centers` (used for connector endpoints below) only ever
        # reflects glyph coordinates.
        for widget in glyph_widgets:
            row, column = viewport_positions[widget.node_id]
            grid_x, grid_y = _mesh_grid_pixel(row, column)
            selected = widget.node_id == self._selected_node_id
            # Selected nodes render a 3-cell-wide composite (see
            # MeshNodeWidget.refresh_visual); centering that wider box on
            # grid_x -- the same formula used for the label -- keeps its
            # middle column, not just its left edge, on the anchor.
            width = MESH_SELECTED_GLYPH_WIDTH if selected else 1
            widget.styles.width = width
            widget.styles.height = 1
            widget.styles.offset = (grid_x - width // 2, grid_y)
            centers[widget.node_id] = (grid_x, grid_y)
            traced = widget.node_id in self._traced_node_ids
            widget.refresh_visual(selected=selected, theme=theme, now=now, traced=traced)

        # Relay-stage placeholders share the same glyph anchor formula as
        # a real node's glyph (see MeshNodeWidget above) but never the
        # enlarged selected composite -- a relay stage is never selected
        # (see MeshRelayWidget), so it is always exactly 1 cell wide.
        for widget in relay_widgets:
            row, column = viewport_positions[widget.node_id]
            grid_x, grid_y = _mesh_grid_pixel(row, column)
            widget.styles.width = 1
            widget.styles.height = 1
            widget.styles.offset = (grid_x, grid_y)
            centers[widget.node_id] = (grid_x, grid_y)
            widget.refresh_visual(theme=theme)
            # Visibility is decided LATER, after the connector loop has
            # actually routed each chain (see the loop over
            # relay_widgets below render-time routing): a relay stage
            # is only ever shown when the drawn connector genuinely
            # visits it. Positioning/refresh here stays unconditional
            # so a stage that IS shown lands on exactly the same anchor
            # formula as before.

        # The label is a separate, independently positioned overlay: its
        # own box is exactly cell_len(label) wide (never wider), centered
        # over the glyph's fixed (grid_x, grid_y) by offsetting the whole
        # label widget -- never by resizing or repositioning the glyph.
        for widget in label_widgets:
            grid_x, grid_y = centers[widget.node_id]
            label_width = max(
                1,
                cell_len(
                    mesh_board_marker_label(
                        widget.state.node,
                        max_name_cells=MESH_BOARD_LABEL_MAX_CELLS,
                    )
                ),
            )
            widget.styles.width = label_width
            widget.styles.height = 1
            widget.styles.offset = (grid_x - label_width // 2, grid_y - 1)
            selected = widget.node_id == self._selected_node_id
            highlighted = self.app.settings.is_favorite(widget.node_id)
            widget.refresh_visual(
                selected=selected, theme=theme, now=now, highlighted=highlighted
            )

        # Connector semantics: a YOU-to-node path means "we currently
        # believe this node is active in the mesh" -- CLIENT history
        # alone is not enough; see _mesh_active_hop_counts. A stale node
        # keeps its last-known position but no path back to YOU: it
        # answers "what else does my radio remember", not "what's active
        # right now". The amount of known route detail determines relay
        # visualization, never whether a connection exists at all: a
        # known nonzero hop count draws through exactly that many
        # anonymous relay stages (see RelayStage/build_relay_stages),
        # representing observed path DEPTH, never discovered relay
        # identity ("Alice is 3 relay stages away", never "these are
        # three identified radios"); a known zero hop count, or an
        # UNKNOWN hop count, draws a direct line with zero relay stages
        # -- "we know this real node is currently participating, but we
        # do not know its intermediate route" is not the same claim as
        # "draw an isolated active dot with no connection at all", and
        # treating it that way previously left active nodes with unknown
        # hops rendered with no connector whatsoever. The selected-node
        # unified bottom bar's own "HOPS ?" still reports the hop count
        # honestly (see mesh_state.format_mesh_node_bar_fields) -- this
        # only concerns whether a connector renders, never fabricates a
        # hop count.
        palette = THEME_PALETTES[theme]
        stages_by_client: dict[str, list[RelayStage]] = {}
        for stage in relay_stages:
            stages_by_client.setdefault(stage.source_node_id, []).append(stage)
        for stages in stages_by_client.values():
            stages.sort(key=lambda stage: stage.index)

        # STALE nodes (see MeshNodeState.activity_tier) now ALSO draw a
        # connector -- DIM + DOTTED, direct only (no relay chain: a
        # stale node keeps its last-known position but no known-active
        # route, same as any other non-active node) -- rather than
        # vanishing outright the moment they fall out of the ACTIVE
        # window (see item 5 of the MESH activity-model task).
        # VERY_OLD nodes never reach this loop at all: build_mesh_
        # working_set already filters them out of `working_set` itself.
        connector_cells: list[tuple[int, int, str, str]] = []
        selected_connector_cells: list[tuple[int, int, str, str]] = []
        # ORPHAN TOPOLOGY CIRCLE AUDIT: the synthetic relay stages whose
        # chain the connector loop below ACTUALLY routed through. Only
        # these stages are displayed (see the visibility loop after this
        # one) -- every fallback to a direct YOU-to-endpoint line, and
        # every skipped/undrawn chain, leaves its stages out, so an
        # anonymous hollow circle can never stand on the board without
        # its connector chain. Starts empty so "no connectors at all"
        # (YOU missing from centers) also shows zero relay markers.
        connected_relay_ids: set[str] = set()
        if you_id is not None and you_id in centers:
            for state in working_set:
                remote_id = state.node.node_id
                if state.node.is_local or remote_id not in centers:
                    continue
                tier = state.activity_tier(now=now)
                if tier is MeshActivityTier.VERY_OLD:
                    continue
                is_stale = tier is MeshActivityTier.STALE
                chain_stages = () if is_stale else stages_by_client.get(remote_id, ())
                chain_ids = {remote_id, *(stage.node_id for stage in chain_stages)}
                is_selected = self._selected_node_id in chain_ids
                chain_points = (
                    centers[you_id],
                    *(
                        centers[stage.node_id]
                        for stage in chain_stages
                        if stage.node_id in centers
                    ),
                    centers[remote_id],
                )
                # Obstacles: every OTHER real node/relay-stage's own
                # occupied cell -- never this chain's own endpoints or
                # relay stages (see route_chain_avoiding/item 8: real
                # node cells are obstacles unless evidence-supported for
                # THIS connection). Selection state and staleness never
                # change the geometry, only the color/glyph below.
                obstacles = frozenset(
                    position
                    for node_id, position in centers.items()
                    if node_id not in chain_ids
                )
                # build_relay_stages already guarantees an ordered,
                # non-self-overlapping chain in LOGICAL space (see its
                # own docstring), but project_to_viewport clips each
                # node's position independently, with no awareness of
                # chain order -- once the current selection recenters
                # the board, an INTERMEDIATE relay stage (never the
                # real you_id/remote_id endpoints, whose own off-screen
                # clipping is the intended "edge indicator" case) can
                # independently clip onto a viewport edge far from its
                # true interpolated position, breaking the straight-
                # line ordering the chain's geometry otherwise
                # guarantees. Detected directly against the same
                # edge_ids project_to_viewport already computed --
                # real-hardware regression: this used to be detected
                # only indirectly, by checking for a DUPLICATE cell in
                # the resulting route, which caught some but not all
                # such cases (a chain can retrace across itself and
                # visibly zigzag across the whole board -- "start in
                # the lower topology, rise to the top, then run
                # horizontally across it" -- entirely through CELLS
                # that never individually repeat). Falling back to a
                # direct YOU-to-endpoint line replaces the connector's
                # PATH -- and, since the line then visits no stage, the
                # chain's relay dots are withheld from
                # connected_relay_ids so they are hidden along with it
                # (orphan-circle audit): an intermediate marker whose
                # connector no longer visits it would otherwise stand
                # on the board as an unexplained standalone circle.
                relay_stage_ids_in_chain = {
                    stage.node_id for stage in chain_stages if stage.node_id in centers
                }
                if relay_stage_ids_in_chain & self._edge_node_ids:
                    route_cells = route_chain_avoiding(
                        (centers[you_id], centers[remote_id]), obstacles
                    )
                else:
                    route_cells = route_chain_avoiding(chain_points, obstacles)
                    if len({(x, y) for x, y, _glyph in route_cells}) != len(route_cells):
                        # Belt-and-suspenders: a chain with every stage
                        # genuinely on-screen could still self-overlap
                        # via obstacle-avoidance detours alone (see
                        # route_connector_avoiding) -- same fallback,
                        # different trigger.
                        route_cells = route_chain_avoiding(
                            (centers[you_id], centers[remote_id]), obstacles
                        )
                    else:
                        # The drawn connector genuinely visits every
                        # stage of this chain -- these markers have a
                        # visible line through them and may render.
                        connected_relay_ids |= relay_stage_ids_in_chain
                if is_selected:
                    color = palette.accent
                elif is_stale:
                    color = palette.dim_base
                    route_cells = tuple(
                        (x, y, _mesh_dashed_glyph(glyph)) for x, y, glyph in route_cells
                    )
                else:
                    color = palette.dim_base
                target = selected_connector_cells if is_selected else connector_cells
                target.extend((x, y, glyph, color) for x, y, glyph in route_cells)
        # ORPHAN TOPOLOGY CIRCLE AUDIT invariant: every visible
        # synthetic hollow-circle relay marker participates in a
        # currently drawn connector chain. Decided HERE -- after the
        # routing above settled which chains are actually drawn through
        # their stages -- never before, which is exactly the ordering
        # bug that produced unexplained standalone circles (the marker
        # was shown in the placement loop, then the route fell back to
        # a direct line that no longer visits it). This also subsumes
        # the earlier edge-clip rule (MESH BOUNDARY CONTINUATION items
        # 30-32: an anonymous dim dot clipped to the viewport edge must
        # never impersonate a real off-screen node's boundary
        # indicator): a chain containing an edge-clipped stage always
        # takes the direct-route fallback, so none of its stages are
        # connected, and none render. Display-only for SYNTHETIC
        # markers -- real MeshNodeWidget nodes (their glyphs, labels,
        # edge indicators, traceroute '*') are never touched here.
        for widget in relay_widgets:
            widget.display = widget.node_id in connected_relay_ids
        # Selected route drawn LAST: MeshCanvas's own overlay dict keys
        # on (x, y), so whichever connector's cells are appended last
        # wins any shared cell -- painting the focused node's full
        # route after every other connector (rather than in working-set
        # order, where an unselected connector drawn later could paint
        # over part of an earlier-drawn selected one) is what guarantees
        # it stays fully ACCENT wherever it is drawable, regardless of
        # how many other connectors happen to overlap it (see item 10).
        # Moving focus away naturally restores ordinary styling on the
        # very next set_nodes() call -- nothing here persists paint
        # state across calls.
        self.board.query_one(MeshCanvas).render_scene(
            board_width,
            board_height,
            tuple(connector_cells) + tuple(selected_connector_cells),
            theme,
        )

    def clear_nodes(self) -> None:
        self.board.remove_children(MeshNodeWidget)
        self.board.remove_children(MeshNodeLabelWidget)
        self.board.remove_children(MeshRelayWidget)
        self._working_set = ()
        self._base_positions = {}
        self._relay_stages = ()
        self._selected_node_id = ""
        self._edge_node_ids = frozenset()
        self.board.query_one(MeshCanvas).render_scene(1, 1, (), "snow")

    def select_node(self, node_id: str) -> None:
        """Select a real working-set node by ID; anything else is a no-op.

        `_selected_node_id` may only ever hold a real node currently in
        `working_set` -- an anonymous RelayStage's synthetic ID, or any
        other stale/unknown ID, is rejected outright rather than
        accepted here and relying on a later set_nodes() call to
        sanitize it back out. Relay stages are internal rendering/
        topology bookkeeping only (see mesh_topology.RelayStage) and can
        never be selected, focused, navigated to, or recentered on.
        """
        if any(state.node.node_id == node_id for state in self._working_set):
            self._selected_node_id = node_id

    def select_local(self) -> None:
        local_id = next(
            (state.node.node_id for state in self._working_set if state.node.is_local),
            None,
        )
        if local_id is not None:
            self.select_node(local_id)

    @property
    def traced_node_ids(self) -> frozenset[str]:
        return self._traced_node_ids

    def mark_traced(self, node_id: str) -> None:
        """Record session-local successful-traceroute evidence for

        `node_id` (TRACE ROUTE Part C) -- additive only (a later failed
        trace against a DIFFERENT node, or a later failed retry of THIS
        SAME node, never removes a prior success; see
        MeshtasticPassApp._finish_traceroute_failure, which never calls
        this). Takes effect the next time a label is rendered
        (set_nodes/refresh_visual), not immediately.
        """
        self._traced_node_ids = self._traced_node_ids | {node_id}


class IdentityNameControl(Horizontal):
    """Shared two-state identity field for navigation and text editing."""

    can_focus = True
    MIN_FIELD_WIDTH = 8
    MAX_UTF8_BYTES = LONG_NAME_MAX_UTF8_BYTES
    LABEL = "NAME"
    INPUT_ID = "identity-name-input"
    UNAVAILABLE_ID = "identity-name-unavailable"
    FIXED_ROW_WIDTH = 19  # two-cell gutter, 13-cell label, two brackets.

    def __init__(self, *, widget_id: str) -> None:
        super().__init__(
            id=widget_id,
            classes="identity-name-control connection-action-row",
        )
        self.editing = False
        self._pre_edit_value = ""

    def compose(self) -> ComposeResult:
        yield Static(" ", classes="connection-selection-gutter", markup=False)
        yield Static(self.LABEL, classes="connection-label", markup=False)
        yield Static("[ ", classes="identity-bracket", markup=False)
        yield Input(
            id=self.INPUT_ID,
            max_length=self.MAX_UTF8_BYTES,
            disabled=True,
        )
        yield Static(" ]", classes="identity-bracket", markup=False)
        yield Static(
            "...",
            id=self.UNAVAILABLE_ID,
            classes="identity-name-unavailable",
            markup=False,
        )

    @property
    def editor(self) -> Input:
        return self.query_one(f"#{self.INPUT_ID}", Input)

    def set_available(self, value: str, *, force_value: bool = False) -> None:
        """Show a confirmed value while keeping navigation mode arrow-safe."""
        self.disabled = False
        self.editor.display = True
        self.query_one(f"#{self.UNAVAILABLE_ID}", Static).display = False
        for bracket in self.query(".identity-bracket"):
            bracket.display = True
        if force_value or not self.editing:
            self.editor.value = value
        if not self.editing:
            self.editor.disabled = True
        self._resize_field()

    def set_unavailable(self, placeholder: str) -> None:
        """Leave editing and show a truthful unavailable value."""
        if self.editing:
            self.cancel_edit()
        self.editor.value = ""
        self.editor.disabled = True
        self.editor.display = False
        for bracket in self.query(".identity-bracket"):
            bracket.display = False
        unavailable = self.query_one(f"#{self.UNAVAILABLE_ID}", Static)
        unavailable.update(placeholder)
        unavailable.display = True
        self.disabled = True
        self._update_label()

    def begin_edit(self) -> None:
        """Enable and focus the real text input for one edit session."""
        if self.disabled or self.editing:
            return
        self._pre_edit_value = self.editor.value
        self.editing = True
        self.add_class("editing")
        self.editor.disabled = False
        self._update_label()
        self.editor.focus()
        self.editor.cursor_position = len(self.editor.value)
        self._resize_field()

    def finish_edit(self, value: str) -> None:
        """Commit the displayed value and return to navigation mode."""
        self.editor.value = value
        self.editing = False
        self.remove_class("editing")
        self.editor.disabled = True
        self._update_label()
        self.focus()
        self._resize_field()

    def cancel_edit(self) -> None:
        """Restore the pre-edit value and return to navigation mode."""
        if self.editing:
            self.editor.value = self._pre_edit_value
        self.editing = False
        self.remove_class("editing")
        self.editor.disabled = True
        self._update_label()
        if not self.disabled:
            self.focus()
        self._resize_field()

    def _update_label(self, *, focused: bool | None = None) -> None:
        navigation_focus = self.has_focus if focused is None else focused
        marker = ">" if (navigation_focus or self.editing) and not self.disabled else " "
        self.query_one(".connection-selection-gutter", Static).update(marker)

    def _resize_field(self) -> None:
        if not self.is_mounted:
            return
        desired = max(self.MIN_FIELD_WIDTH, cell_len(self.editor.value))
        desired = min(desired, self.MAX_UTF8_BYTES)
        available = max(1, self.size.width - self.FIXED_ROW_WIDTH)
        self.editor.styles.width = min(desired, available)

    def on_focus(self, _event: Focus) -> None:
        self._update_label(focused=True)

    def on_blur(self) -> None:
        self._update_label(focused=False)

    def on_resize(self) -> None:
        self._resize_field()

    @on(Input.Changed)
    def resize_for_value(self) -> None:
        self._resize_field()

    def on_key(self, event: Key) -> None:
        if event.key == "enter" and not self.editing and not self.disabled:
            self.begin_edit()
            event.stop()

    @on(Click)
    def clicked(self) -> None:
        if not self.editing and not self.disabled:
            self.focus()


class LongNameControl(IdentityNameControl):
    """Meshtastic Long Name navigation/edit control."""

    LABEL = "LONG NAME"
    INPUT_ID = "long-name-input"
    UNAVAILABLE_ID = "identity-long-name-unavailable"

    def __init__(self) -> None:
        super().__init__(widget_id="identity-long-name")


class ShortNameControl(IdentityNameControl):
    """Meshtastic Short Name navigation/edit control."""

    LABEL = "SHORT NAME"
    INPUT_ID = "short-name-input"
    UNAVAILABLE_ID = "identity-short-name-unavailable"
    MIN_FIELD_WIDTH = 4
    MAX_UTF8_BYTES = SHORT_NAME_MAX_UTF8_BYTES

    def __init__(self) -> None:
        super().__init__(widget_id="identity-short-name")


class ChatEntryWidget(Vertical):
    """One chat message whose relative timestamp can refresh in place."""

    can_focus = True

    class SelectionChanged(Message):
        def __init__(self, widget: "ChatEntryWidget", selected: bool) -> None:
            super().__init__()
            self.widget = widget
            self.selected = selected

    class UserMenuRequested(Message):
        def __init__(self, widget: "ChatEntryWidget") -> None:
            super().__init__()
            self.widget = widget

    def __init__(
        self,
        entry: ChatEntry,
        now: float | None = None,
        favorite: bool = False,
        mention: bool = False,
    ) -> None:
        self.entry = entry
        self.favorite = favorite and not entry.outgoing
        # @mention highlighting (Part H) is CHANNEL-incoming only
        # (item 32/31) -- never DM, never an outgoing entry's own
        # delivery glyph semantics.
        self.mention = mention and not entry.outgoing and entry.dm_node_id is None
        initial_now = monotonic() if now is None else now
        is_new = self.entry.is_new and not self.entry.outgoing
        self.timestamp_label = Static(
            self._timestamp_text(initial_now),
            classes="chat-entry-timestamp",
            markup=False,
        )
        header_parts: list[Static] = [
            Static(self.entry.author, classes="chat-entry-author", markup=False),
            Static(" / ", classes="chat-entry-separator", markup=False),
        ]
        self.delivery_label: Static | None = None
        if self.entry.outgoing:
            self.delivery_label = Static(
                "",
                classes="chat-entry-delivery",
                markup=False,
            )
            header_parts.extend(
                [
                    self.delivery_label,
                    Static(" / ", classes="chat-entry-separator", markup=False),
                ]
            )
        header_parts.append(self.timestamp_label)
        self.distance_label: Static | None = None
        if not self.entry.outgoing and self.entry.distance_miles is not None:
            self.distance_label = Static(
                format_distance_miles(self.entry.distance_miles),
                classes="chat-entry-distance",
                markup=False,
            )
            header_parts.extend(
                [
                    Static(" / ", classes="chat-entry-separator", markup=False),
                    self.distance_label,
                ]
            )
        header = Horizontal(*header_parts, classes="chat-entry-header")
        # Flag-pair wrap-severing (a regional-indicator pair split
        # across a wrap boundary, corrupting whatever renders to its
        # right -- here, the transcript's scrollbar) is fixed at the
        # rendering boundary via grapheme_text.install_flag_pair_protection(),
        # called once at module import time -- the text here is never
        # touched. Selection styling likewise never touches this text
        # or its width -- see ChatEntryWidget.on_focus/on_blur, which
        # only ever update the separate, fixed-width selection_marker.
        #
        # terminal_safe_text() additionally substitutes keycap-digit
        # emoji (e.g. a boxed/keycap-style "5") with the equivalent
        # single-codepoint circled digit -- see grapheme_text.py for
        # why that specific sequence's Rich/Textual-accounted width can
        # disagree with what a plain terminal font actually paints.
        # Display-only: self.entry.text itself, chat_store persistence,
        # the outgoing RF payload, and @mention matching all still use
        # the original, untouched text.
        self.message_label = Static(
            terminal_safe_text(self.entry.text),
            classes="chat-entry-text",
            markup=False,
        )
        self.selection_marker = Static(" ", classes="chat-selection-marker", markup=False)
        self.action_control = MessageActionControl(entry, action="resend")
        self.action_control.display = False
        self.delete_control = MessageActionControl(entry, action="delete")
        self.delete_control.display = False
        self.action_control.paired_control = self.delete_control
        self.delete_control.paired_control = self.action_control
        classes = "chat-entry new-message" if is_new else "chat-entry"
        if self.favorite:
            classes += " favorite-sender"
        if self.mention:
            classes += " mention"
        super().__init__(
            Horizontal(
                self.selection_marker,
                Vertical(header, self.message_label, classes="chat-entry-content"),
                classes="chat-entry-row",
            ),
            Horizontal(
                self.action_control,
                self.delete_control,
                classes="message-action-row",
            ),
            classes=classes,
        )
        self.refresh_delivery_state(1)

    def refresh_timestamp(self, now: float) -> None:
        """Update only the existing timestamp child for this entry."""
        self.timestamp_label.update(self._timestamp_text(now))

    def _timestamp_text(self, now: float) -> str:
        age = format_relative_age(now - self.entry.age_reference)
        return f"RX {age}" if self.entry.age_is_receive_time else age

    def refresh_new_message_state(self) -> None:
        """Apply the entry's persistent new/read presentation state."""
        self.set_class(self.entry.is_new and not self.entry.outgoing, "new-message")

    def set_favorite(self, favorite: bool) -> None:
        self.favorite = favorite and not self.entry.outgoing
        self.set_class(self.favorite, "favorite-sender")

    def set_mention(self, mention: bool) -> None:
        self.mention = mention and not self.entry.outgoing and self.entry.dm_node_id is None
        self.set_class(self.mention, "mention")

    def refresh_delivery_state(self, animation_frame: int) -> None:
        if self.delivery_label is None:
            return
        internal_state = self.entry.delivery_state or DeliveryState.SENT
        visible_state = internal_state
        if visible_state is DeliveryState.SENDING:
            self.delivery_label.update(
                _sending_arrows_text(animation_frame, self.app._current_theme)
            )
        else:
            text = DELIVERY_CHECKMARKS.get(visible_state, visible_state.value)
            self.delivery_label.update(text)
        for name in DeliveryState:
            self.set_class(
                name is visible_state,
                f"delivery-{name.value.lower()}",
            )
        actionable = can_manual_resend(self.entry)
        self.action_control.display = actionable
        self.delete_control.display = actionable

    def on_focus(self, _event: Focus) -> None:
        self.selection_marker.update(">")
        self.post_message(self.SelectionChanged(self, True))

    def on_blur(self, _event: object) -> None:
        self.selection_marker.update(" ")
        self.post_message(self.SelectionChanged(self, False))

    def on_click(self, _event: Click) -> None:
        """Give mouse selection the same acknowledgement semantics as focus."""
        self.focus()

    def on_key(self, event: Key) -> None:
        if event.key == "enter" and not self.entry.outgoing:
            if getattr(self.app, "_user_menu", None) is None:
                self.post_message(self.UserMenuRequested(self))
                event.stop()


class MeshtasticPassApp(App[None]):
    """The first MeshtasticPass terminal UI shell."""

    TITLE = "MeshtasticPass"
    CSS = f"""
    $snow_base: {THEME_PALETTES["snow"].base};
    $snow_accent: {THEME_PALETTES["snow"].accent};
    $snow_accent2: {THEME_PALETTES["snow"].accent2};
    $snow_dim: {THEME_PALETTES["snow"].dim};
    $snow_confirm: {THEME_PALETTES["snow"].confirm};
    $amber_base: {THEME_PALETTES["amber"].base};
    $amber_accent: {THEME_PALETTES["amber"].accent};
    $amber_accent2: {THEME_PALETTES["amber"].accent2};
    $amber_dim: {THEME_PALETTES["amber"].dim};
    $amber_confirm: {THEME_PALETTES["amber"].confirm};
    $error: {ERROR};
    $selection_background: #181818;
    """ + """
    Screen {
        background: #101010;
        color: $snow_base;
        layers: base popup;
    }

    Screen.theme-amber {
        color: $amber_base;
    }

    #tab-bar {
        height: 3;
        padding: 1 1 0 1;
        background: #101010;
        color: $snow_dim;
    }

    #content {
        height: 1fr;
        padding: 0 2;
    }

    .tab-page {
        height: 1fr;
    }

    #connection {
        overflow-x: hidden;
        scrollbar-size: 1 1;
        scrollbar-color: $snow_base;
        scrollbar-color-hover: $snow_base;
        scrollbar-color-active: $snow_base;
        scrollbar-background: $snow_dim;
        scrollbar-background-hover: $snow_dim;
        scrollbar-background-active: $snow_dim;
    }

    .page-title {
        height: 2;
        color: $snow_accent;
        text-style: bold;
    }

    /* CONNECTION/NETWORK/RADIO/STYLE's own section headers use DIM (the
       theme's 50%-BASE-over-background token), never ACCENT/ACCENT2 --
       ID-scoped so "PROFILE" and #mesh-connection-status (which
       reuse .page-title for its own layout/weight, not this coloring)
       are entirely unaffected. */
    #connection-title, #style-title, #radio-title, #advanced-radio-title {
        color: $snow_dim;
    }

    Screen.theme-amber #connection-title,
    Screen.theme-amber #style-title,
    Screen.theme-amber #radio-title,
    Screen.theme-amber #advanced-radio-title {
        color: $amber_dim;
    }

    #connection-status, #connection-details, #identity-values, #radio-info {
        height: auto;
        min-height: 1;
        overflow-x: hidden;
    }

    /* CONNECTION -> NETWORK keeps exactly one blank line of separation
       above it; RADIO and STYLE follow directly with no extra blank line
       (the NETWORK/RADIO and RADIO/STYLE boundaries read as tighter,
       related groups). Focus order is unaffected. */
    #advanced-radio-title {
        margin-top: 1;
    }

    #radio-title, #style-title {
        margin-top: 0;
    }

    .identity-name-control {
        height: 1;
        width: 1fr;
        overflow-x: hidden;
    }

    .connection-selection-gutter {
        width: 2;
        height: 1;
    }

    .connection-label {
        width: 13;
        height: 1;
    }

    .identity-bracket {
        width: auto;
        height: 1;
    }

    .identity-name-unavailable {
        width: auto;
        height: 1;
        color: $snow_dim;
    }

    Screen.theme-amber .identity-name-unavailable {
        color: $amber_dim;
    }

    #long-name-input, #short-name-input,
    #network-name-input, #freq-slot-input, #key-input,
    #new-channel-name, #new-channel-key {
        width: 16;
        height: 1;
        border: none;
        padding: 0;
        background: #101010;
        color: $snow_base;
    }

    #long-name-input, #short-name-input {
        width: 8;
    }

    Screen.theme-amber #long-name-input,
    Screen.theme-amber #short-name-input,
    Screen.theme-amber #network-name-input,
    Screen.theme-amber #freq-slot-input,
    Screen.theme-amber #key-input {
        color: $amber_base;
    }

    .advanced-radio-editor {
        /* Collapsed by default (spec B): the NEW NETWORK editor rows
           are revealed only by _set_network_editor_open, which flips
           each widget's inline display to override this. */
        display: none;
    }

    #long-name-input:disabled, #short-name-input:disabled {
        color: $snow_dim;
        opacity: 1;
    }

    Screen.theme-amber #long-name-input:disabled,
    Screen.theme-amber #short-name-input:disabled {
        color: $amber_dim;
    }

    #long-name-status, #short-name-status, #timezone-status, #role-status,
    #font-size-status, #color-status {
        /* No min-height: display is toggled False when empty (see
           _set_long_name_status/_set_short_name_status/_set_timezone_
           status/_set_role_status/_set_font_size_status/
           _set_color_status), so the row collapses to zero height
           instead of reserving a permanent blank line. */
        height: auto;
    }

    #long-name-status, #short-name-status, #timezone-status,
    #role-status, #font-size-status {
        color: $snow_confirm;
    }

    Screen.theme-amber #long-name-status,
    Screen.theme-amber #short-name-status,
    Screen.theme-amber #timezone-status,
    Screen.theme-amber #role-status,
    Screen.theme-amber #font-size-status {
        color: $amber_confirm;
    }

    #long-name-status.setting-error, #short-name-status.setting-error,
    #timezone-status.setting-error, #role-status.setting-error,
    #font-size-status.setting-error, #color-status.setting-error {
        color: $error;
    }

    /* Textual's own CSS specificity (id, class, type) otherwise lets
       the theme-scoped CONFIRM override above win under AMBER, since
       "Screen.theme-amber #widget" carries one more type-selector
       component than "#widget.setting-error" -- these repeat the
       error color with that SAME extra Screen.theme-amber qualifier
       so ERROR always wins regardless of the active theme. $error is
       already theme-independent (NEON_RED in both palettes); only the
       selector's specificity needs raising here, not its value. */
    Screen.theme-amber #long-name-status.setting-error,
    Screen.theme-amber #short-name-status.setting-error,
    Screen.theme-amber #timezone-status.setting-error,
    Screen.theme-amber #role-status.setting-error,
    Screen.theme-amber #font-size-status.setting-error,
    Screen.theme-amber #color-status.setting-error {
        color: $error;
    }

    .keyboard-dropdown {
        height: auto;
        min-height: 2;
        color: $snow_base;
    }

    Screen.theme-amber .keyboard-dropdown {
        color: $amber_base;
    }

    .keyboard-dropdown:focus {
        color: $snow_accent;
    }

    Screen.theme-amber .keyboard-dropdown:focus {
        color: $amber_accent;
    }

    #connection .connection-action-row,
    .editor-form .connection-action-row {
        height: 1;
        min-height: 1;
        width: 1fr;
        overflow-x: hidden;
    }

    .chat-entry:focus,
    #connection .connection-action-row:focus,
    .editor-form .connection-action-row:focus,
    #connection .identity-name-control.editing {
        background: $selection_background;
    }

    Screen.theme-amber #chat-input {
        color: $amber_accent2;
    }

    /* "> message" is Textual's OWN Input.placeholder, styled via the
       separate "input--placeholder" component class (Textual's
       get_component_rich_style machinery, DEFAULT_CSS: "color:
       $text-disabled") -- a completely different mechanism from the
       plain `color` property above, which only ever affected TYPED
       text. Real hardware exposed that the prompt stayed Textual's
       own built-in disabled-grey under AMBER; this targets that exact
       component class so the prompt shares the same AMBER ACCENT2
       identity as typed text. */
    Screen.theme-amber #chat-input > .input--placeholder {
        color: $amber_accent2;
    }

    Screen.theme-amber .page-title {
        color: $amber_accent;
    }

    #chat-header {
        height: auto;
        min-height: 2;
    }

    #chat-title, #chat-dm-selector {
        width: auto;
        max-width: 70%;
        height: auto;
        min-height: 2;
        text-style: bold;
        text-overflow: ellipsis;
    }

    #chat-header-bullet {
        width: 3;
        height: auto;
        min-height: 2;
        content-align: center middle;
        color: $snow_dim;
    }

    Screen.theme-amber #chat-header-bullet {
        color: $amber_dim;
    }

    #chat-content, #chat-channel, #chat-dms {
        height: 1fr;
    }

    #radio-status {
        height: 2;
    }

    #advanced-radio-title {
        margin-top: 1;
    }

    #advanced-radio-status {
        /* auto, not a fixed 2 like #radio-status: the press-again-to-
           confirm SAVE/switch message is long enough to wrap across
           several lines at typical terminal widths, and must never be
           clipped. */
        height: auto;
        min-height: 0;
    }

    .editor-actions {
        height: 1;
        /* CONNECTION_VALUE_COLUMN_INDENT (2 row-prefix + 12 label + 1)
           minus each button's own margin-left:2 -- so [ SAVE ] lands in
           the same column the form controls' "[ ... ]" start at, and
           [ CANCEL ] follows on the SAME row after a 2-cell gap. Shared
           by NEW PRESET and NEW CHANNEL so both action rows align
           identically. */
        padding-left: 13;
    }

    .editor-actions .connection-action-row {
        width: auto;
        margin-left: 2;
    }

    .editor-hint {
        height: 1;
        min-height: 1;
        /* Aligned to the form's value column: gutter(2) + label(13). */
        padding-left: 13;
    }

    /* Spec D/J: pending ("SAVING & APPLYING...") and normal success
       ("... APPLIED") both use ACCENT; a genuine failure uses ERROR and
       never ACCENT. */
    #advanced-radio-status.setting-accent {
        color: $snow_accent;
    }

    Screen.theme-amber #advanced-radio-status.setting-accent {
        color: $amber_accent;
    }

    #advanced-radio-status.setting-error {
        color: $error;
    }

    .setting-success {
        color: $snow_confirm;
    }

    Screen.theme-amber .setting-success {
        color: $amber_confirm;
    }

    #connection-error, #send-error, #radio-status.setting-error {
        height: auto;
        min-height: 1;
        color: $error;
    }

    #send-error.older-message-notice {
        color: $snow_accent;
    }

    Screen.theme-amber #send-error.older-message-notice {
        color: $amber_accent;
    }

    #chat-log {
        height: 1fr;
        scrollbar-size: 1 1;
        scrollbar-color: $snow_base;
        scrollbar-color-hover: $snow_base;
        scrollbar-color-active: $snow_base;
        scrollbar-background: $snow_dim;
        scrollbar-background-hover: $snow_dim;
        scrollbar-background-active: $snow_dim;
    }

    Screen.theme-amber #chat-log, Screen.theme-amber #connection {
        scrollbar-color: $amber_base;
        scrollbar-color-hover: $amber_base;
        scrollbar-color-active: $amber_base;
        scrollbar-background: $amber_dim;
        scrollbar-background-hover: $amber_dim;
        scrollbar-background-active: $amber_dim;
    }

    #mesh-status, #mesh-node-bar {
        height: 1;
    }

    #mesh-status {
        color: $snow_dim;
    }

    Screen.theme-amber #mesh-status {
        color: $amber_dim;
    }

    #mesh-node-bar {
        width: 1fr;
    }

    #dm-content {
        height: 1fr;
    }

    #dm-list {
        height: 1fr;
        scrollbar-size: 1 1;
    }

    .dm-list-row {
        height: 1;
        width: 1fr;
        color: $snow_base;
    }

    Screen.theme-amber .dm-list-row {
        color: $amber_base;
    }

    .dm-list-row.highlighted {
        color: $snow_accent;
        text-style: bold;
    }

    Screen.theme-amber .dm-list-row.highlighted {
        color: $amber_accent;
    }

    .dm-list-empty {
        color: $snow_dim;
        height: 1;
    }

    Screen.theme-amber .dm-list-empty {
        color: $amber_dim;
    }

    #dm-header {
        height: auto;
        min-height: 1;
        text-style: bold;
        color: $snow_base;
    }

    Screen.theme-amber #dm-header {
        color: $amber_base;
    }

    #dm-log {
        height: 1fr;
        scrollbar-size: 1 1;
        scrollbar-color: $snow_base;
        scrollbar-color-hover: $snow_base;
        scrollbar-color-active: $snow_base;
        scrollbar-background: $snow_dim;
        scrollbar-background-hover: $snow_dim;
        scrollbar-background-active: $snow_dim;
    }

    Screen.theme-amber #dm-log {
        scrollbar-color: $amber_base;
        scrollbar-color-hover: $amber_base;
        scrollbar-color-active: $amber_base;
        scrollbar-background: $amber_dim;
        scrollbar-background-hover: $amber_dim;
        scrollbar-background-active: $amber_dim;
    }

    #dm-send-error {
        height: auto;
        min-height: 1;
        color: $error;
    }

    Screen.theme-amber #dm-input {
        color: $amber_accent2;
    }

    Screen.theme-amber #dm-input > .input--placeholder {
        color: $amber_accent2;
    }

    #mesh-view {
        height: 1fr;
        /* Horizontal centering is applied explicitly in code as a
           board-level offset (see MeshTopologyView.set_nodes), not
           via CSS align, so it stays exact regardless of board width
           parity; vertical centering is still fine left to CSS. */
        align: left middle;
    }

    #mesh-board {
        position: relative;
        min-width: 1;
        min-height: 1;
        layers: canvas nodes;
    }

    .mesh-canvas {
        layer: canvas;
        position: absolute;
        offset: 0 0;
    }

    .mesh-node {
        layer: nodes;
        position: absolute;
        content-align: center middle;
    }

    .viewport-menu {
        layer: popup;
        position: absolute;
        background: #101010;
        border: solid $snow_dim;
        padding: 0;
        scrollbar-size: 1 1;
        scrollbar-color: $snow_base;
        scrollbar-background: $snow_dim;
    }

    .viewport-menu-row {
        height: 1;
        padding: 0 1;
        color: $snow_base;
    }

    .viewport-menu-row.highlighted {
        color: $snow_accent;
        text-style: bold reverse;
    }

    .viewport-menu-row.informational {
        color: $snow_dim;
    }

    Screen.theme-amber .viewport-menu {
        border: solid $amber_dim;
        scrollbar-color: $amber_base;
        scrollbar-background: $amber_dim;
    }

    Screen.theme-amber .viewport-menu-row {
        color: $amber_base;
    }

    Screen.theme-amber .viewport-menu-row.highlighted {
        color: $amber_accent;
    }

    Screen.theme-amber .viewport-menu-row.informational {
        color: $amber_dim;
    }

    .emoji-picker {
        layer: popup;
        position: absolute;
        background: #101010;
        border: solid $snow_dim;
        height: 3;
        /* 1 cell left, 2 cells right -- see EMOJI_PICKER_PADDING_CELLS
           for why the right side carries an extra safety cell. */
        padding: 0 2 0 1;
    }

    Screen.theme-amber .emoji-picker {
        border: solid $amber_dim;
    }

    #load-older, .message-action {
        width: auto;
        height: 1;
        color: $snow_base;
        margin-bottom: 1;
    }

    Screen.theme-amber #load-older,
    Screen.theme-amber .message-action {
        color: $amber_base;
    }

    .message-action-row {
        width: auto;
        height: auto;
    }

    #end-of-chat-history {
        width: 1fr;
        height: 1;
        margin-bottom: 1;
        color: $snow_dim;
        text-align: center;
    }

    Screen.theme-amber #end-of-chat-history {
        color: $amber_dim;
    }

    #start-of-channel-history {
        width: 1fr;
        height: 1;
        margin-bottom: 1;
        color: $snow_dim;
        text-align: center;
    }

    Screen.theme-amber #start-of-channel-history {
        color: $amber_dim;
    }

    #load-older:focus, .message-action:focus {
        text-style: reverse;
    }

    .chat-entry {
        height: auto;
        margin-bottom: 1;
        padding-right: 1;
    }

    .chat-entry-row, .chat-entry-content {
        width: 1fr;
        height: auto;
    }

    .chat-selection-marker {
        width: 2;
        height: 1;
    }

    .message-action {
        margin-left: 2;
    }

    .chat-entry-header {
        height: 1;
    }

    .chat-entry-author {
        width: auto;
        text-style: bold;
    }

    .chat-entry.favorite-sender .chat-entry-author {
        color: $snow_accent2;
    }

    Screen.theme-amber .chat-entry.favorite-sender .chat-entry-author {
        color: $amber_accent2;
    }

    .chat-entry-separator, .chat-entry-delivery {
        width: auto;
        color: $snow_dim;
    }

    .chat-entry-timestamp, .chat-entry-distance {
        width: auto;
        color: $snow_dim;
        text-style: dim;
    }

    Screen.theme-amber .chat-entry-timestamp,
    Screen.theme-amber .chat-entry-distance {
        color: $amber_dim;
    }

    Screen.theme-amber .chat-entry-separator,
    Screen.theme-amber .chat-entry-delivery {
        color: $amber_dim;
    }

    /* Delivery color grammar (item 9/28): ✓✓ HEARD = ACCENT,
       ✓ SENT = BASE, ⟐ UNCONFIRMED = ACCENT2, ✕ FAILED/INTERRUPTED =
       ERROR. Semantics (DeliveryState) are unchanged -- only which
       token each visible glyph resolves to. SENDING's own two "▷ ▷"
       arrows are explicitly two-toned (ACCENT/DIM25, alternating -- see
       app._sending_arrows_text), rendered as Rich Text spans that
       override this single-color CSS rule for the actual glyphs; this
       selector's own ACCENT is kept as delivery-sending's base/fallback
       widget color only (e.g. before the very first refresh_delivery_
       state call paints the spans). */
    .chat-entry.delivery-sending .chat-entry-delivery,
    .chat-entry.delivery-heard .chat-entry-delivery {
        color: $snow_accent;
    }

    Screen.theme-amber .chat-entry.delivery-sending .chat-entry-delivery,
    Screen.theme-amber .chat-entry.delivery-heard .chat-entry-delivery {
        color: $amber_accent;
    }

    .chat-entry.delivery-sent .chat-entry-delivery {
        color: $snow_base;
    }

    Screen.theme-amber .chat-entry.delivery-sent .chat-entry-delivery {
        color: $amber_base;
    }

    .chat-entry.delivery-unconfirmed .chat-entry-delivery {
        color: $snow_accent2;
    }

    Screen.theme-amber .chat-entry.delivery-unconfirmed .chat-entry-delivery {
        color: $amber_accent2;
    }

    .chat-entry.delivery-failed .chat-entry-delivery,
    .chat-entry.delivery-interrupted .chat-entry-delivery {
        color: $error;
    }

    .chat-entry-text {
        height: auto;
    }

    /* @mention highlighting (CHAT/DM/MENTION UX Part H): the ENTIRE
       incoming CHANNEL CHAT block, not merely the "@SHORTNAME"
       characters (item 30) -- a background wash in ACCENT2, so
       stronger nested semantics (ERROR text, delivery glyphs,
       resend/delete controls) keep their own explicit color
       unaffected. Textual CSS has no :not() pseudo-class, so
       ".chat-entry.mention:focus" below is an explicit, HIGHER-
       specificity (3 selectors vs. this rule's 2, and vs.
       ".chat-entry:focus" alone) override that guarantees the
       existing focus/selection background always wins outright when
       both apply, regardless of declaration order (item 31) -- never
       a real ambiguous tie. */
    .chat-entry.mention {
        background: $snow_accent2 20%;
    }

    Screen.theme-amber .chat-entry.mention {
        background: $amber_accent2 20%;
    }

    .chat-entry.mention:focus {
        background: $selection_background;
    }

    .chat-entry.new-message .chat-entry-author,
    .chat-entry.new-message .chat-entry-timestamp,
    .chat-entry.new-message .chat-entry-distance,
    .chat-entry.new-message .chat-entry-text {
        color: $snow_accent;
    }

    Screen.theme-amber .chat-entry.new-message .chat-entry-author,
    Screen.theme-amber .chat-entry.new-message .chat-entry-timestamp,
    Screen.theme-amber .chat-entry.new-message .chat-entry-distance,
    Screen.theme-amber .chat-entry.new-message .chat-entry-text {
        color: $amber_accent;
    }

    #chat-input {
        height: 1;
        border: none;
        padding: 0;
        background: #101010;
        color: $snow_base;
    }

    #chat-input:focus {
        border: none;
    }

    #dm-input {
        height: 1;
        border: none;
        padding: 0;
        background: #101010;
        color: $snow_base;
    }

    #dm-input:focus {
        border: none;
    }

    #chat-new-below {
        height: 1;
        color: $snow_accent;
        text-align: right;
    }

    Screen.theme-amber #chat-new-below {
        color: $amber_accent;
    }

    #footer {
        height: 2;
        padding: 0 1;
        border-top: solid $snow_dim;
        color: $snow_dim;
    }

    Screen.theme-amber #tab-bar,
    Screen.theme-amber #footer {
        color: $amber_dim;
    }

    Screen.theme-amber #footer {
        border-top: solid $amber_dim;
    }
    """

    def __init__(
        self,
        radio: object,
        settings: AppSettings | None = None,
        terminal_cursor: TerminalCursor | None = None,
        chat_store: ChatStore | None = None,
        history_error: str = "",
    ) -> None:
        super().__init__()
        self.radio = radio
        self.settings = settings or AppSettings.load()
        self._current_theme = self.settings.color
        self.current_tab = "connection"
        self._channels: tuple[ChannelInfo, ...] = (ChannelInfo(0, "Channel 1"),)
        self.current_channel_index = 0
        self._channel_states: dict[int, ChannelChatState] = {
            0: ChannelChatState()
        }
        self.chat_history = self._channel_states[0].entries
        self.unread_count = 0
        self.transcript_new_count = 0
        # Direct Messages are a DISTINCT conversation model from channel
        # CHAT (item 1): a separate per-conversation state dict, keyed
        # by the remote party's canonical node ID (never a display
        # name -- item 2), each reusing the SAME ChannelChatState shape
        # (entries/draft/etc.) channel CHAT already uses. current_dm_
        # node_id is None while CHAT's DMS mode shows its conversation
        # LIST; set to a specific conversation's node ID once opened.
        self._dm_states: dict[str, ChannelChatState] = {}
        self.current_dm_node_id: str | None = None
        self._dm_conversation_order: list[str] = []
        self._dm_list_highlighted_index = 0
        # NEW DM: transient "create a DM by typing a node ID" state. While
        # True, CHAT's DMS mode shows the temporary DM / NEW entry surface
        # (header "DM / NEW", instruction text, and the composer repurposed
        # as a node-ID field) instead of a real conversation. Never a
        # persisted conversation: cleared the moment a valid ID opens a
        # real DM, on ESC, or on any mode/tab leave. Zero RF.
        self._new_dm_mode = False
        # NEW CHANNEL (private-channel UI): the truthfully-pending draft the
        # CHAT-local editor is building, plus any compact validation error.
        # Never a radio-authoritative ChannelInfo -- this is NOT "the radio
        # is configured on this channel", only "the user is configuring it".
        # Zero writes/RF until the (future, hardware) radio-write boundary.
        self._pending_channel: PendingChannelConfig | None = None
        self._new_channel_error = ""
        self._new_channel_editor_open = False
        # Guards against duplicate APPLY writes while an async apply is live.
        self._pending_apply_active = False
        # CHAT/DM/MENTION UX Part A: DM is no longer its own top-level
        # tab -- it is a MODE inside CHAT, alongside "channel" (the
        # default). current_tab stays "chat" for both; only this and
        # the inner #chat-content ContentSwitcher change (see
        # _switch_chat_mode). dm_unread_count is the DM(N) header
        # badge's count -- see _recount_dm_unread's own docstring for
        # the exact chosen unread model.
        self._chat_mode = "channel"
        self.dm_unread_count = 0
        self.chat_store = chat_store
        self._history_error = history_error
        self._radio_state = RadioState.CONNECTING
        self._radio_info: RadioInfo | None = None
        # Local wall-clock moment AUTO SYNC last completed a clock-set
        # successfully in THIS session -- never the radio's own time
        # (see RadioService.sync_clock: AdminMessage has no get-time
        # RPC to read that back with, see ClockSyncResult). None until
        # the first successful sync. Diagnostic only -- never rendered.
        self._last_clock_sync_at: float | None = None
        # Whether an AUTO SYNC write is currently in flight -- guards
        # against a reconnect loop launching an overlapping second
        # write (see _maybe_auto_sync_clock).
        self._clock_sync_in_progress = False
        # Identifies the CURRENT AUTO SYNC attempt -- see
        # ClockSyncApplied/_reset_clock_sync_state: a completion for a
        # stale (superseded, e.g. by a disconnect/reconnect) generation
        # is ignored, so a late completion from an abandoned connection
        # can never corrupt the new one's bookkeeping.
        self._clock_sync_generation = 0
        # Whether AUTO SYNC has already run once for the CURRENT
        # connection lifecycle (see _maybe_auto_sync_clock) -- reset
        # only when _show_connection sees a genuine non-ONLINE ->
        # ONLINE transition, so a reconnect loop can never trigger more
        # than one sync per lifecycle.
        self._clock_auto_sync_done_this_connection = False
        # One entry per named radio-write operation currently in flight
        # (see _run_radio_worker) -- lets a new call in the SAME group
        # be refused outright while the previous one is still running,
        # without depending on Textual's own worker exclusivity (which
        # cannot actually interrupt a blocking thread either way).
        self._radio_workers: dict[str, Thread] = {}
        # NETWORK: the currently selected PRESET name. Starts on the
        # built-in LongFast -- a LOCAL UI DEFAULT ONLY (zero writes)
        # until a radio has actually been read. Once a successful radio
        # sync completes, the RADIO is authoritative: _detect_active_
        # network_from_radio derives the active PRESET from the actual
        # radio configuration (read-only) on every genuine connect/
        # reconnect. Also changes on a confirmed PRESET switch or a
        # confirmed SAVE. Never auto-applied.
        self._selected_network: str = BUILTIN_LONGFAST_NETWORK
        # NETWORK: True when the last successful radio sync did not
        # semantically match ANY saved PRESET -- the selector then shows
        # the honest UNMATCHED_NETWORK_LABEL placeholder instead of
        # falsely claiming some PRESET (e.g. LongFast) is active. While
        # True there IS no active PRESET, so any explicit selection is a
        # genuine switch (no "already selected" no-op). Cleared by a
        # detection match, a verified apply, or a SAVE.
        self._network_unmatched = False
        # Whether the transient NEW NETWORK editor is currently revealed
        # (see _set_network_editor_open). Purely local UI state -- never
        # persisted, discarded on leaving CONNECTION (see show_tab).
        self._network_editor_open = False
        # Press-again-to-confirm arming (see _arm_advanced_radio_confirm/
        # _advanced_radio_confirm_expired) -- "save" or "switch:<name>"
        # while armed, else None. Auto-disarms after
        # ADVANCED_RADIO_CONFIRM_SECONDS via a Timer, and is explicitly
        # disarmed by ANY other editor action (editing a field, CANCEL,
        # leaving the view) so a stale arm can never survive into an
        # unrelated confirmation.
        self._advanced_radio_confirm: str | None = None
        self._advanced_radio_confirm_timer: Timer | None = None
        # The ONE outstanding NETWORK apply (SAVE or switch), its
        # monotonic correlation token source, and its hard-timeout
        # Timer. Cleared the instant a terminal SUCCESS/ERROR is
        # reached, so "SAVING & APPLYING..." can never persist (see
        # NetworkApply / _network_apply_timed_out).
        self._network_apply: NetworkApply | None = None
        self._network_apply_seq = 0
        self._network_apply_timer: Timer | None = None
        # NETWORK: the one-shot auto-dismiss for a terminal "<name>
        # APPLIED" success line (NETWORK_STATUS_SUCCESS_DISMISS_SECONDS).
        # Correlation-safe by construction: EVERY status write goes
        # through _set_advanced_radio_status, which stops any pending
        # dismiss first, so a stale timer can never clear a newer
        # status/error; only the success path ever re-arms it.
        self._network_status_dismiss_timer: Timer | None = None
        self._status_dot_count = 1
        self._connection_animation_timer: Timer | None = None
        self._chat_timestamp_timer: Timer | None = None
        self._delivery_timer: Timer | None = None
        self._send_error_message = ""
        self._send_error_dismiss_timer: Timer | None = None
        self._long_name_status_dismiss_timer: Timer | None = None
        self._short_name_status_dismiss_timer: Timer | None = None
        self._timezone_status_dismiss_timer: Timer | None = None
        self._role_status_dismiss_timer: Timer | None = None
        self._arrival_sequence = 0
        self._send_animation_frame = 1
        self._has_older_history = False
        self._mounted_chat_target = DEFAULT_HISTORY_LIMIT
        self._chat_open_scroll_pending = False
        self._user_menu: ViewportMenu | None = None
        self._user_menu_origin: Widget | None = None
        self._user_menu_scroll_target: ScrollableContainer | None = None
        self._user_menu_scroll_x: float | None = None
        self._user_menu_scroll_y: float | None = None
        self._emoji_picker: EmojiPicker | None = None
        # MESH LAYOUT STABILITY: sticky logical positions (node_id ->
        # (x, y, region), assign_grid_slots' own PositionedNode shape)
        # and a monotonic per-axis extent ratchet for place_within_
        # bounds -- both fed back in as each subsequent _refresh_mesh()
        # call's own input, so a routine update (last_heard, telemetry,
        # LINK, selection, a node aging/appearing elsewhere) never
        # moves an already-placed node purely because rank/index
        # bookkeeping shifted. See _refresh_mesh and mesh_topology.
        # assign_grid_slots/place_within_bounds' own docstrings.
        self._mesh_sticky_positions: dict[str, tuple[int, int, str]] = {}
        self._mesh_extent_ratchet: dict[str, int] = {
            "up": 0,
            "down": 0,
            "left": 0,
            "right": 0,
        }
        # TRACE ROUTE (Part C): the ONE currently in-flight explicit
        # traceroute (v1 -- see _start_traceroute's own one-active-at-a-
        # time guard), the session-local ledger of successful results
        # (keyed by canonical destination node ID, replaced not
        # duplicated on a repeat trace), the terminal TRACE SUCCEEDED/
        # TRACE FAILED banner (if one is currently showing), and the
        # 3-position arrow-animation frame (see TRACEROUTE_ARROW_
        # POSITIONS/_advance_delivery_states). None of this is ever
        # persisted to disk -- an app restart clears it completely.
        self._active_traceroute: ActiveTraceroute | None = None
        self._traceroute_results: dict[str, TracerouteResult] = {}
        self._traceroute_banner: TracerouteBanner | None = None
        self._traceroute_banner_timer: Timer | None = None
        self._traceroute_timeout_timer: Timer | None = None
        self._traceroute_animation_frame = 0
        self._traceroute_request_seq = 0
        self._terminal_cursor = terminal_cursor or TerminalCursor()
        self._monitor = RadioMonitor(
            radio,
            self._radio_event_from_thread,
            self._message_from_thread,
        )

    def compose(self) -> ComposeResult:
        try:
            devices = tuple(self.radio.available_device_paths())
        except Exception:
            devices = ()
        yield Static(id="tab-bar")
        with ContentSwitcher(initial="connection", id="content"):
            with ConnectionPage(id="connection", classes="tab-page"):
                yield Static("CONNECTION", id="connection-title", classes="page-title")
                yield DeviceSelector(self.settings.device_path, devices)
                yield Static(id="connection-status")
                yield Static(id="connection-details")
                yield Static(id="connection-error")
                yield LongNameControl()
                yield Static(id="long-name-status", markup=False)
                yield ShortNameControl()
                yield Static(id="short-name-status", markup=False)
                yield Static(id="identity-values", markup=False)
                # Conceptual section order: CONNECTION -> NETWORK ->
                # RADIO -> STYLE. NETWORK is the saved-PRESET section
                # (PRESET selector + NEW PRESET editor); RADIO holds the
                # device/radio controls that are NOT part of a saved
                # PRESET (HOP LIMIT included -- never folded into a
                # PRESET; see RadioConfigPreset's own docstring); STYLE
                # is appearance only. NETWORK is collapsed by default:
                # only the PRESET selector and [ NEW PRESET ] are
                # visible; the .advanced-radio-editor rows below are
                # hidden until NEW PRESET is activated and discarded on
                # leaving the view (see _set_network_editor_open /
                # show_tab).
                yield Static(
                    "NETWORK",
                    id="advanced-radio-title",
                    classes="page-title",
                )
                yield NetworkSelector(
                    (
                        DropdownOption(
                            BUILTIN_LONGFAST_NETWORK, BUILTIN_LONGFAST_NETWORK
                        ),
                    ),
                )
                yield NewNetworkControl()
                # The one blank row that separates the NETWORK selector
                # from the revealed editor (spec D).
                yield Static(
                    " ",
                    id="advanced-radio-editor-spacer",
                    classes="connection-action-row advanced-radio-editor",
                    markup=False,
                )
                yield NetworkFieldInput(
                    label="NETWORK NAME",
                    widget_id="network-name-row",
                    input_id="network-name-input",
                )
                yield RadioModeSelector("LONG_FAST")
                yield NetworkFieldInput(
                    label="FREQ. SLOT",
                    widget_id="freq-slot-row",
                    input_id="freq-slot-input",
                    max_length=3,
                )
                yield NetworkFieldInput(
                    label="KEY",
                    widget_id="key-row",
                    input_id="key-input",
                )
                with Horizontal(
                    id="advanced-radio-actions",
                    classes="advanced-radio-editor editor-actions",
                ):
                    yield SaveNetworkControl()
                    yield CancelNetworkControl()
                yield Static(id="advanced-radio-status", markup=False)
                yield Static("RADIO", id="radio-title", classes="page-title")
                yield Static(id="radio-info", markup=False)
                yield RoleSelector(0)
                yield Static(id="role-status", markup=False)
                yield BluetoothSelector(True)
                yield TimezoneSelector("")
                yield Static(id="timezone-status", markup=False)
                yield ScreenTimeoutSelector(300)
                yield UnitsSelector(DISPLAY_UNITS_METRIC)
                yield CompassSelector(True)
                yield FlipScreenSelector(False)
                yield Clock24HSelector(True)
                yield HopLimitSelector(3)
                yield AutoSyncSelector(self.settings.clock_auto_sync)
                yield Static(id="radio-status")
                yield Static("STYLE", id="style-title", classes="page-title")
                yield FontSizeSelector(self.settings.font_size)
                yield Static(id="font-size-status", markup=False)
                yield ColorSelector(self.settings.color)
                yield Static(id="color-status", markup=False)
            with Vertical(id="chat", classes="tab-page"):
                # Peer selectors (CHAT/DM/MENTION UX Part B): LEFT is the
                # configured Meshtastic channel selector (unchanged
                # ChannelSelector); RIGHT is the DM(N) mode selector --
                # opening it switches CHAT into DMS mode instead of
                # picking from a normal dropdown popup (see
                # DMModeSelector.open_menu). The bullet between them is
                # a plain, non-focusable Static -- purely a visual
                # separator (item 6).
                with Horizontal(id="chat-header"):
                    yield ChannelSelector(self._channels, self.current_channel_index)
                    yield Static(
                        "•",
                        id="chat-header-bullet",
                        classes="chat-header-bullet",
                        markup=False,
                    )
                    yield DMModeSelector(0)
                with ContentSwitcher(initial="chat-channel", id="chat-content"):
                    with Vertical(id="chat-channel"):
                        with ContentSwitcher(
                            initial="chat-conversation", id="chat-channel-content"
                        ):
                            with Vertical(id="chat-conversation"):
                                yield Static(id="new-channel-pending", markup=False)
                                yield ChatTranscript(id="chat-log")
                                yield Static(id="chat-new-below")
                                yield Static(id="send-error")
                                yield ChatMessageInput(
                                    placeholder="> message",
                                    id="chat-input",
                                    select_on_focus=False,
                                )
                            with Vertical(
                                id="new-channel-editor",
                                classes="new-channel-editor editor-form",
                            ):
                                yield Static(
                                    "NEW CHANNEL",
                                    classes="page-title",
                                    markup=False,
                                )
                                yield NetworkFieldInput(
                                    label="CHANNEL NAME",
                                    widget_id="new-channel-name-row",
                                    input_id="new-channel-name",
                                    collapsible=False,
                                )
                                yield NetworkFieldInput(
                                    label="CHANNEL KEY",
                                    widget_id="new-channel-key-row",
                                    input_id="new-channel-key",
                                    collapsible=False,
                                )
                                with Horizontal(
                                    id="new-channel-actions",
                                    classes="editor-actions",
                                    markup=False,
                                ):
                                    yield NewChannelCancel()
                                    yield NewChannelSave()
                                    yield NewChannelApply()
                                yield Static(
                                    "LEAVING KEY BLANK WILL CREATE A NEW CHANNEL",
                                    classes="editor-hint",
                                    markup=False,
                                )
                                yield Static(id="new-channel-error", markup=False)
                    with Vertical(id="chat-dms"):
                        yield Static(
                            id="dm-connection-status",
                            classes="page-title",
                            markup=False,
                        )
                        with ContentSwitcher(initial="dm-list", id="dm-content"):
                            yield VerticalScroll(id="dm-list")
                            with Vertical(id="dm-conversation"):
                                yield Static(id="dm-header", markup=False)
                                yield Static(
                                    id="dm-new-instruction",
                                    markup=False,
                                    classes="dm-new-instruction",
                                )
                                yield ChatTranscript(id="dm-log")
                                yield Static(id="dm-send-error")
                                yield ChatMessageInput(
                                    placeholder="> message",
                                    id="dm-input",
                                    select_on_focus=False,
                                )
            with Vertical(id="profile", classes="tab-page"):
                yield Static("> PROFILE", classes="page-title")
                yield Static("Coming in a future milestone.")
            with Vertical(id="mesh", classes="tab-page"):
                # Shown/hidden and populated by _update_chat_connection_state()
                # with the exact same _connection_status_rich_text() CHAT's
                # heading uses -- never MESH-specific terminology. Not the
                # removed permanent "> MESH · ACTIVE N" heading: this
                # exists ONLY while a connection state needs to be
                # communicated, and collapses to nothing otherwise.
                yield Static(id="mesh-connection-status", classes="page-title", markup=False)
                yield Static(id="mesh-status", markup=False)
                yield MeshTopologyView()
                # MESH GPS + UNIFIED BAR Part B: one single physical line
                # for the selected node (LONG NAME - SHORT NAME - HOPS -
                # GPS - DISTANCE - LINK - TIME), replacing the previous
                # separate bottom-left context line and bottom-right
                # LINK/LAST UPDATE line -- see _update_mesh_node_bar.
                yield Static(id="mesh-node-bar", markup=False)
        yield Static("1-3 switch tabs    F4 quit", id="footer")

    def on_mount(self) -> None:
        self._terminal_cursor.hide()
        self._apply_color_theme(self.settings.color)
        self._update_tab_bar()
        # The NEW DM instruction line is only visible during the transient
        # node-ID entry surface (see _start_new_dm), never in a normal DM
        # conversation or the DM list.
        self.query_one("#dm-new-instruction", Static).display = False
        # NEW CHANNEL editor panel + error line start hidden (only shown by
        # _start_new_channel / _save_new_channel / _cancel_new_channel).
        self._refresh_new_channel_editor()
        # UI SCALE/COLOR are local settings, independent of the radio
        # connection lifecycle -- collapsed here once at startup, unlike
        # the RADIO-section per-field rows _show_connection resets on
        # every connection-state transition.
        self._set_font_size_status("", None)
        self._set_color_status("", None)
        self._show_connection(RadioState.CONNECTING)
        self._connection_animation_timer = self.set_interval(
            0.45,
            self._advance_connection_animation,
            name="connection-status-animation",
        )
        self._chat_timestamp_timer = self.set_interval(
            1.0,
            self._refresh_chat_timestamps,
            name="chat-relative-timestamps",
        )
        self._delivery_timer = self.set_interval(
            0.45,
            self._advance_delivery_states,
            name="chat-delivery-states",
        )
        self._load_chat_history()
        if self._history_error:
            self._show_send_error(self._history_error)
        # Land on the first focus stop of the reordered CONNECTION ->
        # NETWORK -> RADIO -> STYLE page (STYLE now sits at the bottom,
        # so focusing it here would scroll the view away from the top).
        self.query_one(DeviceSelector).focus()
        self._monitor.start()

    def on_unmount(self) -> None:
        if self._connection_animation_timer is not None:
            self._connection_animation_timer.stop()
        if self._chat_timestamp_timer is not None:
            self._chat_timestamp_timer.stop()
        if self._delivery_timer is not None:
            self._delivery_timer.stop()
        if self._send_error_dismiss_timer is not None:
            self._send_error_dismiss_timer.stop()
        if self._network_apply_timer is not None:
            self._network_apply_timer.stop()
        if self._advanced_radio_confirm_timer is not None:
            self._advanced_radio_confirm_timer.stop()
        if self._network_status_dismiss_timer is not None:
            self._network_status_dismiss_timer.stop()
        self.restore_terminal_cursor()
        self._monitor.stop()
        self._reconcile_interrupted_sends_before_shutdown()
        if self.chat_store is not None:
            self.chat_store.close()

    def _reconcile_interrupted_sends_before_shutdown(self) -> None:
        """Persist INTERRUPTED for every still-SENDING outgoing message

        this process owns, before the store closes.

        This is additional cleanup, not the authoritative fix:
        correctness no longer depends on it. ChatStore.open() now
        rewrites any abandoned SENDING row to INTERRUPTED directly in
        SQLite the moment a store is opened -- covering every row in
        the database regardless of channel, pagination, or whether
        anything is currently loaded into memory (see
        ChatStore.reconcile_abandoned_sending()), which is what actually
        repairs a row this process never even loads. What this method
        adds on top: while this process is still running, catching a
        send up front (before the NEXT process's startup reconciliation
        would) means a row this process gives up on shows INTERRUPTED
        immediately rather than sitting as SENDING until the next
        restart. Runs after _monitor.stop() so no late radio callback
        can race a write in after this pass runs. Every channel's
        entries are checked, not just the currently displayed one -- a
        send can be left in flight in a channel the user has since
        switched away from.
        """
        if self.chat_store is None:
            return
        for state in (*self._channel_states.values(), *self._dm_states.values()):
            for entry in state.entries:
                if not entry.outgoing or entry.delivery_state is not DeliveryState.SENDING:
                    continue
                entry.delivery_state = DeliveryState.INTERRUPTED
                if entry.message_id is None:
                    continue
                try:
                    self.chat_store.update_delivery_state(
                        entry.message_id,
                        DeliveryState.INTERRUPTED.value,
                        attempt_id=entry.active_attempt_id,
                    )
                except ChatStoreError:
                    # Shutting down regardless; there is no user-facing
                    # surface left to report this failure to.
                    pass

    def on_key(self, event: Key) -> None:
        """Handle global keys only while the chat input is not focused."""
        if event.key == "f4":
            self.exit()
            event.stop()
            return
        if self._user_menu is not None:
            # Real-hardware regression (PR #46 follow-up Part A): the
            # node/user menu never steals Textual focus (ViewportMenu.
            # can_focus = False, by design -- see its own docstring),
            # so the ORIGINATING ChatEntryWidget/#chat-log stays
            # focused the entire time the menu is open. event.stop()
            # alone only blocks this event from bubbling PAST the App
            # -- it does NOT stop Textual's own separate, always-
            # present App._on_key handler (inherited from the base App
            # class) from ALSO running in the SAME dispatch pass. That
            # handler independently walks the bindings of every widget
            # from the still-focused ChatEntryWidget up to the Screen,
            # finds ChatTranscript's OWN inherited ScrollableContainer
            # bindings ("up"->scroll_up, "down"->scroll_down), and
            # fires them regardless -- silently scrolling the
            # transcript underneath the menu on every arrow press. In
            # ordinary (menu-closed) navigation this same double-fire
            # happens too, but _move_chat_focus's own scroll_visible()
            # call immediately re-settles the correct position
            # afterward, masking it; nothing re-settles it while the
            # menu owns navigation, which is what actually exposed the
            # bug. event.prevent_default() is the one call that
            # actually suppresses that second, independent handler
            # (see Message._no_default_action / _get_dispatch_methods
            # in Textual's own message_pump.py) -- event.stop() is not
            # enough on its own here.
            if event.key in ("up", "down"):
                self._user_menu.move_highlight(-1 if event.key == "up" else 1)
                event.stop()
                event.prevent_default()
            elif event.key == "enter":
                self._user_menu.activate()
                event.stop()
                event.prevent_default()
            elif event.key == "escape":
                self._close_user_menu()
                event.stop()
                event.prevent_default()
            return
        if (
            event.key == "ctrl+d"
            and self.current_tab == "chat"
            and self._chat_mode == "dms"
            and self.current_dm_node_id is not None
        ):
            # CTRL+D deletes the DM conversation currently being viewed.
            # Handled here (before the Input branch below) so it works
            # whether the composer or the transcript has focus. Zero RF,
            # and never offered in NEW DM mode or the DM list (both have
            # current_dm_node_id None). CTRL+D has no Textual/Input
            # editing meaning, so there is no default to conflict with.
            self._delete_current_dm()
            event.stop()
            return
        if self._new_channel_editor_open and self.current_tab == "chat":
            # NEW CHANNEL editor is active: UP/DOWN navigate the editor
            # fields, ESC cancels. Printable characters and ENTER are left
            # to the focused editor field/control so typing works like a
            # normal input; nothing here leaks into hidden CHANNEL/DM widgets.
            if event.key == "escape":
                self._cancel_new_channel()
                self.query_one("#chat-log", ChatTranscript).focus()
                event.stop()
                return
            if event.key in ("up", "down"):
                self._move_new_channel_focus(-1 if event.key == "up" else 1)
                event.stop()
                return
        if isinstance(self.focused, KeyboardDropdown) and self.focused.is_open:
            return
        if isinstance(self.focused, Input):
            if self.focused.id == "chat-input" and event.key == "escape":
                self.query_one("#chat-log", ChatTranscript).focus()
                event.stop()
            elif self.focused.id == "chat-input" and event.key == "up":
                self._move_chat_focus(-1)
                event.stop()
            elif self.focused.id == "dm-input" and event.key == "escape":
                if self._new_dm_mode:
                    self._cancel_new_dm()
                else:
                    self.query_one("#dm-log", ChatTranscript).focus()
                event.stop()
            elif self.focused.id == "dm-input" and event.key == "up":
                self._move_dm_focus(-1)
                event.stop()
            elif self.focused.id == "long-name-input" and event.key == "escape":
                self.query_one(LongNameControl).cancel_edit()
                event.stop()
            elif self.focused.id == "short-name-input" and event.key == "escape":
                self.query_one(ShortNameControl).cancel_edit()
                event.stop()
            elif (
                self.current_tab == "connection"
                and self.focused.id
                in (
                    "network-name-input",
                    "freq-slot-input",
                    "key-input",
                )
                and event.key in ("up", "down")
            ):
                # ADVANCED RADIO's plain editor Input fields join
                # the ordinary CONNECTION row up/down order (see
                # _move_connection_focus) -- otherwise this whole
                # isinstance(Input) branch's own unconditional `return`
                # below would swallow up/down for them entirely.
                self._move_connection_focus(-1 if event.key == "up" else 1)
                event.stop()
            return

        # RECONNECT DELIVERY + CHAT HEADER FIX item 16: while not
        # ONLINE, C/D must not open the channel/DM dropdowns their
        # header selectors normally do -- those selectors are
        # themselves hidden/disabled while not ONLINE (see
        # _update_chat_connection_state), so this applies uniformly
        # before any of the three chat_mode-specific branches below
        # (channel-neutral, DMS list, DMS conversation) ever dispatch
        # on "c"/"d", rather than repeating the same check three times.
        # Every other hotkey (arrows, ENTER, ESC, etc.) is unaffected --
        # this pass does not change existing offline/disabled behavior
        # for anything except these two.
        if (
            self.current_tab == "chat"
            and self._radio_state is not RadioState.ONLINE
            and event.key.lower() in ("c", "d")
        ):
            event.stop()
            return

        if self.current_tab == "chat" and self._chat_mode == "channel":
            transcript = self.query_one("#chat-log", ChatTranscript)
            if self._new_channel_editor_open and event.key == "escape":
                # ESC discards a pending NEW CHANNEL draft (zero writes/RF).
                self._cancel_new_channel()
                event.stop()
                return
            if event.key in ("up", "down"):
                self._move_chat_focus(-1 if event.key == "up" else 1)
                event.stop()
                return
            if event.key in ("pageup", "pagedown"):
                step = max(1, transcript.region.height - 2)
                transcript.scroll_relative(
                    y=-step if event.key == "pageup" else step
                )
                self.call_after_refresh(self._clear_indicator_if_at_bottom)
                event.stop()
                return
            if event.key.lower() == "c":
                selector = self.query_one(ChannelSelector)
                selector.focus()
                selector.open_menu()
                event.stop()
                return
            if event.key.lower() == "d":
                # PR #46 follow-up Part B item 7: D mirrors C -- it
                # opens the DM DROPDOWN, never immediately switches
                # into DMS mode/the full conversation list. The user
                # picks a conversation from the dropdown to actually
                # enter DMS mode (see DMModeSelector.open_menu/
                # dropdown_selected's "chat_dm_mode" handling).
                selector = self.query_one(DMModeSelector)
                selector.focus()
                selector.open_menu()
                event.stop()
                return
            if event.key == "left":
                self._focus_oldest_new_message()
                event.stop()
                return
            if event.key == "right":
                self._return_to_present_and_type()
                event.stop()
                return
            if event.key == "end":
                self._jump_to_newest()
                event.stop()
                return
            if (
                event.is_printable
                and event.character
                # "1"/"2"/"3" must still reach the tab-switch dispatch
                # below even from CHAT's neutral state -- see
                # tab_for_key -- or the keyboard could never leave CHAT
                # once on it (they only ever type into the composer when
                # it is ALREADY focused, via the isinstance(self.focused,
                # Input) branch above, which returns before this code
                # even runs). "c"/"d" are excluded for the identical
                # reason -- they are CHANNEL/DMS mode hotkeys handled
                # above, never typed into the composer from this neutral
                # state (typing them while the composer IS already
                # focused goes through the isinstance(self.focused,
                # Input) branch instead, unaffected by this exclusion).
                and event.key not in ("1", "2", "3", "c", "d")
            ):
                # Any other printable character begins composing: focus
                # the input and insert exactly what was typed, appending
                # after whatever draft already exists. A silent no-op
                # while the composer is disabled (radio not ONLINE).
                # Checked directly on the widget, not via self.focused
                # afterward -- Textual's Widget.focus() defers the
                # actual focus change via call_later, so self.focused
                # would still report the OLD widget synchronously within
                # this same handler.
                chat_input = self.query_one("#chat-input", Input)
                if not chat_input.disabled:
                    self._focus_chat_composer()
                    chat_input.insert_text_at_cursor(event.character)
                    event.stop()
                    return

        if self.current_tab == "mesh" and event.key in (
            "up",
            "down",
            "left",
            "right",
        ):
            self._move_mesh_focus(event.key)
            event.stop()
            return

        if self.current_tab == "mesh" and event.key == "enter":
            self._open_mesh_node_menu()
            event.stop()
            return

        if self.current_tab == "chat" and self._chat_mode == "dms":
            if self._new_dm_mode:
                # NEW DM entry: only ESC (cancel) is handled here; typing
                # and ENTER are owned by the composer's Input branch above
                # (focus is on #dm-input) and dm_input_submitted.
                if event.key == "escape":
                    self._cancel_new_dm()
                    event.stop()
                return
            if self.current_dm_node_id is None:
                # Conversation LIST mode: UP/DOWN move the highlight,
                # ENTER opens the highlighted conversation. Any other
                # key (notably the "1"-"3" tab-switch digits) falls
                # through unhandled, exactly like CHANNEL's own neutral
                # state does, so the keyboard is never trapped here.
                if event.key in ("up", "down"):
                    self._move_dm_list_highlight(-1 if event.key == "up" else 1)
                    event.stop()
                    return
                if event.key == "enter":
                    self._activate_dm_list_selection()
                    event.stop()
                    return
                if event.key.lower() == "c":
                    self._switch_chat_mode("channel")
                    self._focus_chat_mode("channel", open_dropdown=True)
                    event.stop()
                    return
                if event.key.lower() == "d":
                    selector = self.query_one(DMModeSelector)
                    selector.focus()
                    selector.open_menu()
                    event.stop()
                    return
            else:
                if event.key == "escape":
                    self._close_dm_conversation()
                    event.stop()
                    return
                transcript = self.query_one("#dm-log", ChatTranscript)
                if event.key in ("up", "down"):
                    self._move_dm_focus(-1 if event.key == "up" else 1)
                    event.stop()
                    return
                if event.key in ("pageup", "pagedown"):
                    step = max(1, transcript.region.height - 2)
                    transcript.scroll_relative(
                        y=-step if event.key == "pageup" else step
                    )
                    event.stop()
                    return
                if event.key.lower() == "c":
                    self._switch_chat_mode("channel")
                    self._focus_chat_mode("channel", open_dropdown=True)
                    event.stop()
                    return
                if event.key.lower() == "d":
                    selector = self.query_one(DMModeSelector)
                    selector.focus()
                    selector.open_menu()
                    event.stop()
                    return
                if (
                    event.is_printable
                    and event.character
                    and event.key not in ("1", "2", "3", "c", "d")
                ):
                    dm_input = self.query_one("#dm-input", Input)
                    if not dm_input.disabled:
                        self._focus_dm_composer()
                        dm_input.insert_text_at_cursor(event.character)
                        event.stop()
                        return

        if (
            self.current_tab == "connection"
            and self._network_editor_open
            and event.key in ("left", "right")
        ):
            # [ SAVE ] [ CANCEL ] share one row -- RIGHT/LEFT moves
            # within the pair so the user never has to press DOWN to
            # reach CANCEL (see _connection_nav_controls).
            save = self.query_one(SaveNetworkControl)
            cancel = self.query_one(CancelNetworkControl)
            if self.focused is save and event.key == "right":
                cancel.focus()
                event.stop()
                return
            if self.focused is cancel and event.key == "left":
                save.focus()
                event.stop()
                return

        if self.current_tab == "connection" and event.key in ("up", "down"):
            step = -1 if event.key == "up" else 1
            if isinstance(self.focused, CancelNetworkControl):
                # CANCEL is not its own vertical stop: vertical nav from
                # it behaves exactly as from its SAVE sibling.
                self._move_connection_focus(
                    step, origin=self.query_one(SaveNetworkControl)
                )
                event.stop()
                return
            if self._move_connection_focus(step):
                event.stop()
                return
            if event.key.lower() == "r" and isinstance(
                self.focused, ChatEntryWidget
            ):
                if can_manual_resend(self.focused.entry):
                    self._rebroadcast(self.focused.entry)
                    event.stop()
                return

        # PROFILE is intentionally absent: hidden from the visible top
        # nav (see TAB_NAMES), so no digit key may reach it. DM is
        # likewise absent here -- it is a MODE inside CHAT now (see
        # TAB_NAMES/_switch_chat_mode), reached via "2" + the D hotkey
        # or the header's DM(N) selector, never its own digit key.
        tab_for_key = {
            "1": "connection",
            "2": "chat",
            "3": "mesh",
        }
        if event.key in tab_for_key:
            self.show_tab(tab_for_key[event.key])
            event.stop()

    def _set_long_name_status(self, text: str, css_class: str | None) -> None:
        """LONG NAME's own status row -- see item ("RADIO — LONG NAME /

        SHORT NAME STATUS LAYOUT"): aligned to the LONG NAME label's
        own x-start (CONNECTION_ROW_PREFIX -- never the value/input
        column), collapses to zero height when empty instead of
        reserving a permanent blank row, and auto-dismisses a SAVED
        confirmation after IDENTITY_STATUS_AUTO_DISMISS_SECONDS -- any
        earlier pending dismiss is always stopped first, exactly like
        _set_clock_status's own guard, so a stale timer can never
        clear a newer status.
        """
        if self._long_name_status_dismiss_timer is not None:
            self._long_name_status_dismiss_timer.stop()
            self._long_name_status_dismiss_timer = None
        status = self.query_one("#long-name-status", Static)
        status.remove_class("setting-success")
        status.remove_class("setting-error")
        if css_class is not None:
            status.add_class(css_class)
        status.display = bool(text)
        status.update(f"{CONNECTION_ROW_PREFIX}{text}" if text else "")
        if css_class == "setting-success":
            self._long_name_status_dismiss_timer = self.set_timer(
                IDENTITY_STATUS_AUTO_DISMISS_SECONDS, self._dismiss_long_name_status
            )

    def _dismiss_long_name_status(self) -> None:
        self._long_name_status_dismiss_timer = None
        self._set_long_name_status("", None)

    def _set_short_name_status(self, text: str, css_class: str | None) -> None:
        """SHORT NAME's own status row -- see _set_long_name_status,

        which this exactly mirrors for the other identity field.
        """
        if self._short_name_status_dismiss_timer is not None:
            self._short_name_status_dismiss_timer.stop()
            self._short_name_status_dismiss_timer = None
        status = self.query_one("#short-name-status", Static)
        status.remove_class("setting-success")
        status.remove_class("setting-error")
        if css_class is not None:
            status.add_class(css_class)
        status.display = bool(text)
        status.update(f"{CONNECTION_ROW_PREFIX}{text}" if text else "")
        if css_class == "setting-success":
            self._short_name_status_dismiss_timer = self.set_timer(
                IDENTITY_STATUS_AUTO_DISMISS_SECONDS, self._dismiss_short_name_status
            )

    def _dismiss_short_name_status(self) -> None:
        self._short_name_status_dismiss_timer = None
        self._set_short_name_status("", None)

    def _set_timezone_status(self, text: str, css_class: str | None) -> None:
        """TIMEZONE's own status row -- see _set_long_name_status, which

        this exactly mirrors for the RADIO-section TIMEZONE control.
        """
        if self._timezone_status_dismiss_timer is not None:
            self._timezone_status_dismiss_timer.stop()
            self._timezone_status_dismiss_timer = None
        status = self.query_one("#timezone-status", Static)
        status.remove_class("setting-success")
        status.remove_class("setting-error")
        if css_class is not None:
            status.add_class(css_class)
        status.display = bool(text)
        status.update(f"{CONNECTION_ROW_PREFIX}{text}" if text else "")
        if css_class == "setting-success":
            self._timezone_status_dismiss_timer = self.set_timer(
                IDENTITY_STATUS_AUTO_DISMISS_SECONDS, self._dismiss_timezone_status
            )

    def _dismiss_timezone_status(self) -> None:
        self._timezone_status_dismiss_timer = None
        self._set_timezone_status("", None)

    def _set_role_status(self, text: str, css_class: str | None) -> None:
        """ROLE's own status row -- see _set_long_name_status, which

        this exactly mirrors for the RADIO-section ROLE control.
        """
        if self._role_status_dismiss_timer is not None:
            self._role_status_dismiss_timer.stop()
            self._role_status_dismiss_timer = None
        status = self.query_one("#role-status", Static)
        status.remove_class("setting-success")
        status.remove_class("setting-error")
        if css_class is not None:
            status.add_class(css_class)
        status.display = bool(text)
        status.update(f"{CONNECTION_ROW_PREFIX}{text}" if text else "")
        if css_class == "setting-success":
            self._role_status_dismiss_timer = self.set_timer(
                IDENTITY_STATUS_AUTO_DISMISS_SECONDS, self._dismiss_role_status
            )

    def _dismiss_role_status(self) -> None:
        self._role_status_dismiss_timer = None
        self._set_role_status("", None)

    def _set_font_size_status(self, text: str, css_class: str | None) -> None:
        """UI SCALE's own status row -- aligned under UI SCALE's own

        control/value column (CONNECTION_VALUE_COLUMN_INDENT), not the
        left label column, and collapses to zero height when empty. No
        auto-dismiss timer: preserves this row's existing lifetime --
        the confirmation stays until the user changes UI SCALE again,
        exactly as before this row was split out of the shared STYLE
        status.
        """
        status = self.query_one("#font-size-status", Static)
        status.remove_class("setting-success")
        status.remove_class("setting-error")
        if css_class is not None:
            status.add_class(css_class)
        status.display = bool(text)
        status.update(f"{CONNECTION_VALUE_COLUMN_INDENT}{text}" if text else "")

    def _set_color_status(self, text: str, css_class: str | None) -> None:
        """COLOR's own status row -- ERROR only. A successful color

        change needs no confirmation beyond the visible theme switch
        and the dropdown's own new value, so this is only ever called
        with text="" on success (see dropdown_selected's own "color"
        branch) -- collapses to zero height, leaving no empty row or
        extra spacing behind.
        """
        status = self.query_one("#color-status", Static)
        status.remove_class("setting-success")
        status.remove_class("setting-error")
        if css_class is not None:
            status.add_class(css_class)
        status.display = bool(text)
        status.update(f"{CONNECTION_VALUE_COLUMN_INDENT}{text}" if text else "")

    @staticmethod
    def _timezone_options_for(tzdef: str) -> tuple[DropdownOption, ...]:
        """TIMEZONE_CHOICES, plus a synthetic CUSTOM entry when `tzdef`

        is a non-empty string that doesn't match any known mapping --
        see TimezoneSelector's own docstring. Never mutates
        TIMEZONE_CHOICES itself.
        """
        options = tuple(DropdownOption(name, value) for name, value in TIMEZONE_CHOICES)
        known_values = {value for _label, value in TIMEZONE_CHOICES}
        if tzdef and tzdef not in known_values:
            return options + (DropdownOption("CUSTOM", tzdef),)
        return options

    def _run_radio_worker(self, group: str, target: Callable[[], None]) -> None:
        """Run one radio admin-write/save/sync call on a dedicated daemon

        thread, never through Textual's run_worker(thread=True).

        Textual's thread-mode workers are ultimately dispatched via
        asyncio's DEFAULT executor (loop.run_in_executor(None, ...) --
        see Worker._run_threaded), and Python's own asyncio.run() --
        which is exactly how Textual's own App.run() drives the event
        loop -- unconditionally waits, with NO timeout, for every job
        that executor has ever accepted before the interpreter may
        exit at all (see BaseEventLoop.shutdown_default_executor(),
        called from asyncio.run()'s own `finally` block). A genuinely
        stalled SDK call -- a real, previously-documented failure mode:
        a serial-layer stall can occur BEFORE the SDK's own
        waitForAckNak timeout even starts ticking -- would therefore
        hang the ENTIRE process at exit no matter what MeshtasticPass's
        own shutdown code does. A plain daemon thread is never tracked
        by that executor, so it can never block process termination:
        Python simply abandons it once every non-daemon thread has
        finished, exactly like RadioMonitor's own monitoring thread
        already does. Correctness-neutral for every caller here: each
        already treats its own result as best-effort, reacting only to
        the Message it later posts (post_message is safe to call, and
        simply returns False, even after this app has fully closed --
        see MessagePump.post_message) -- never by blocking on this
        thread's outcome directly.

        `group` allows at most one in-flight thread per named
        operation -- a second call for the SAME group while the first
        is still running is refused outright, mirroring (and actually
        strengthening) the exclusivity Textual's own
        group=.../exclusive=True previously provided: that mechanism
        could never truly stop an already-running blocking call either,
        so two overlapping SDK calls could already race in practice.
        """
        existing = self._radio_workers.get(group)
        if existing is not None and existing.is_alive():
            return
        thread = Thread(target=target, name=group, daemon=True)
        self._radio_workers[group] = thread
        thread.start()

    @on(Input.Submitted, "#long-name-input")
    def save_long_name(self, event: Input.Submitted) -> None:
        """Apply an identity edit through the active radio service."""
        control = self.query_one(LongNameControl)
        if self._radio_state is not RadioState.ONLINE or self._radio_info is None:
            control.cancel_edit()
            self._set_long_name_status(
                "LONG NAME UNAVAILABLE — RADIO NOT CONNECTED", "setting-error"
            )
            return
        try:
            long_name = validate_long_name(event.value)
        except RadioIdentityError as error:
            control.cancel_edit()
            self._set_long_name_status(str(error), "setting-error")
            return
        control.finish_edit(long_name)
        self._set_long_name_status("SAVING NAME...", None)
        self._run_radio_worker(
            "save-radio-long-name", lambda: self._save_long_name_from_thread(long_name)
        )

    def _save_long_name_from_thread(self, long_name: str) -> None:
        try:
            info = self.radio.set_long_name(long_name)
        except (RadioIdentityError, AttributeError) as error:
            detail = str(error).strip() or "The radio identity could not be saved."
            self.post_message(IdentitySaveFailed(detail, "LONG NAME"))
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            self.post_message(
                IdentitySaveFailed(f"Could not save Long Name: {detail}", "LONG NAME")
            )
        else:
            self.post_message(IdentitySaved(info, "LONG NAME"))

    @on(Input.Submitted, "#short-name-input")
    def save_short_name(self, event: Input.Submitted) -> None:
        """Apply a Short Name edit through the active radio service."""
        control = self.query_one(ShortNameControl)
        if self._radio_state is not RadioState.ONLINE or self._radio_info is None:
            control.cancel_edit()
            self._set_short_name_status(
                "SHORT NAME UNAVAILABLE — RADIO NOT CONNECTED", "setting-error"
            )
            return
        try:
            short_name = validate_short_name(event.value)
        except RadioIdentityError as error:
            control.cancel_edit()
            self._set_short_name_status(str(error), "setting-error")
            return
        control.finish_edit(short_name)
        self._set_short_name_status("SAVING SHORT NAME...", None)
        self._run_radio_worker(
            "save-radio-short-name", lambda: self._save_short_name_from_thread(short_name)
        )

    def _save_short_name_from_thread(self, short_name: str) -> None:
        try:
            info = self.radio.set_short_name(short_name)
        except (RadioIdentityError, AttributeError) as error:
            detail = str(error).strip() or "The radio identity could not be saved."
            self.post_message(IdentitySaveFailed(detail, "SHORT NAME"))
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            self.post_message(
                IdentitySaveFailed(f"Could not save Short Name: {detail}", "SHORT NAME")
            )
        else:
            self.post_message(IdentitySaved(info, "SHORT NAME"))

    @on(IdentitySaved)
    def identity_saved(self, event: IdentitySaved) -> None:
        self._radio_info = event.info
        self._render_identity(force_value=True)
        self._refresh_mesh()
        self._refresh_chat_mentions()
        if event.field_label == "LONG NAME":
            self._set_long_name_status("LONG NAME SAVED", "setting-success")
        else:
            self._set_short_name_status("SHORT NAME SAVED", "setting-success")

    @on(IdentitySaveFailed)
    def identity_save_failed(self, event: IdentitySaveFailed) -> None:
        self._render_identity(force_value=True)
        if event.field_label == "LONG NAME":
            self._set_long_name_status(event.detail, "setting-error")
        else:
            self._set_short_name_status(event.detail, "setting-error")

    def show_tab(self, tab_id: str) -> None:
        # DM is a MODE inside CHAT now, not its own tab (see
        # _switch_chat_mode) -- leaving "chat" for a different
        # top-level tab must still run whichever of CHANNEL's or DMS's
        # own "I was actively being viewed" bookkeeping applies,
        # exactly matching what leaving the old standalone "chat"/"dm"
        # tabs used to do individually.
        if self.current_tab == "chat" and tab_id != "chat":
            if self._chat_mode == "channel":
                self._mark_new_messages_read()
            else:
                self._capture_current_dm_state()
            if self._new_dm_mode:
                self._exit_new_dm_mode()
        if self.current_tab == "connection" and tab_id != "connection":
            # ADVANCED RADIO (spec E): an unfinished NEW NETWORK editor
            # is transient UI state -- leaving the view by ANY path
            # discards its unsaved contents, collapses it, restores
            # [ NEW NETWORK ], and shows no unsaved-changes prompt.
            # Zero RF/config, creates no NETWORK. A no-op when the
            # editor was never opened. Any pending SAVE/switch confirm
            # is disarmed here too (via _collapse_network_editor).
            if self._network_editor_open:
                self._collapse_network_editor()
                self._set_advanced_radio_status("", None)
            else:
                self._disarm_advanced_radio_confirm()
        if self._emoji_picker is not None:
            # Reachable only from the composer of whichever of CHAT/DM
            # is currently active (Ctrl+E requires that Input to be
            # focused) -- so this is correct and sufficient for both,
            # with no tab-specific branching needed.
            self._close_emoji_picker()
        self.current_tab = tab_id
        self.query_one("#content", ContentSwitcher).current = tab_id
        self._update_tab_bar()
        if tab_id == "chat":
            if self._chat_mode == "channel":
                # Neutral navigation state -- NOT the message composer.
                # The user must explicitly begin composing, either by
                # pressing a printable character (focuses #chat-input
                # and inserts it -- see on_key's chat branch) or DOWN
                # (focuses it without inserting anything -- see
                # _move_chat_focus's fallback). #chat-log is the same
                # neutral destination Escape already returns to from
                # the composer, so entering CHAT and escaping out of a
                # draft land in the identical state.
                self._mark_unread_messages_viewed()
                self._recount_unread()
                self._refresh_chat_timestamps()
                self.query_one("#chat-log", ChatTranscript).focus()
                if self._chat_open_scroll_pending:
                    self._chat_open_scroll_pending = False
                    self.call_after_refresh(self._jump_to_newest)
            else:
                if self.current_dm_node_id is None:
                    self._refresh_dm_list()
                    self.query_one("#dm-list", VerticalScroll).focus()
                else:
                    self.query_one("#dm-content", ContentSwitcher).current = "dm-conversation"
                    dm_input = self.query_one("#dm-input", Input)
                    if not dm_input.disabled:
                        dm_input.focus()
                    else:
                        self.query_one("#dm-log", ChatTranscript).focus()
        elif tab_id == "connection":
            self._refresh_device_options()
            self.query_one(DeviceSelector).focus()
        elif tab_id == "mesh":
            self._refresh_mesh()
            # MESH's own arrow-key navigation is handled by the App's
            # on_key -- gated on current_tab, not on any particular
            # widget holding focus (see _move_mesh_focus and
            # test_navigation_works_even_when_focus_is_on_the_board_
            # container) -- EXCEPT that on_key returns early whenever
            # self.focused is an Input, before current_tab is even
            # checked (e.g. mid-edit on a CONNECTION name field, or a
            # focused CHAT message widget from before the switch).
            # Clearing focus here, exactly like the "profile" fallback
            # below, is what CONNECTION and CHAT already do for
            # themselves by claiming their own widget's focus.
            self.set_focus(None)
        else:
            self.set_focus(None)
        self._update_footer()

    def _switch_chat_mode(self, mode: str) -> None:
        """Switch CHAT between its CHANNEL and DMS peer modes (CHAT/DM/

        MENTION UX Part B/C) -- the header's peer selectors and the
        C/D hotkeys all funnel through here. current_tab stays "chat"
        throughout; only the inner #chat-content ContentSwitcher and
        the leave/enter bookkeeping below change. A no-op if already in
        the requested mode, matching every other selector's own
        already-selected short-circuit -- callers that also want to
        (re)focus the mode's own widget do so separately via
        _focus_chat_mode, so re-focusing still works even when the
        mode itself doesn't change.
        """
        if self._chat_mode == mode:
            return
        if self._chat_mode == "channel":
            self._mark_new_messages_read()
        else:
            self._capture_current_dm_state()
        # Leaving DMS (or a transient NEW DM entry surface) clears the
        # NEW DM state so it can never linger into channel CHAT or a
        # later re-entry.
        if self._new_dm_mode:
            self._exit_new_dm_mode()
        self._chat_mode = mode
        self.query_one("#chat-content", ContentSwitcher).current = (
            "chat-channel" if mode == "channel" else "chat-dms"
        )
        if mode == "channel":
            self._mark_unread_messages_viewed()
            self._recount_unread()
            self._refresh_chat_timestamps()
        elif self.current_dm_node_id is None:
            self._refresh_dm_list()
        self._update_tab_bar()
        self._update_footer()

    def _focus_chat_mode(self, mode: str, *, open_dropdown: bool = False) -> None:
        """Focus the appropriate widget for CHAT's given mode -- shared

        by the C/D hotkeys, the header selectors, and show_tab("chat")'s
        own entry behavior (which calls this only implicitly, via the
        equivalent inline logic above, since it must also run the
        mode's read/list-refresh bookkeeping every time the tab is
        re-entered, not only when the mode actually changes).
        """
        if mode == "channel":
            if open_dropdown:
                selector = self.query_one(ChannelSelector)
                selector.focus()
                selector.open_menu()
            else:
                self.query_one("#chat-log", ChatTranscript).focus()
        elif self.current_dm_node_id is None:
            self.query_one("#dm-list", VerticalScroll).focus()
        else:
            self.query_one("#dm-content", ContentSwitcher).current = "dm-conversation"
            dm_input = self.query_one("#dm-input", Input)
            if not dm_input.disabled:
                dm_input.focus()
            else:
                self.query_one("#dm-log", ChatTranscript).focus()

    @on(KeyboardDropdown.Selected)
    async def dropdown_selected(self, event: KeyboardDropdown.Selected) -> None:
        if event.setting_name == "channel_index":
            await self._switch_channel(int(event.value))
            return
        if event.setting_name == "chat_dm_mode":
            # DMModeSelector's own Selected event ALWAYS carries a real
            # conversation node_id here (never "dms" -- see its
            # _activate_popup_item, which never posts Selected for the
            # non-actionable "NO DMS" placeholder) -- PR #46 follow-up
            # Part B item 11: ENTER on a highlighted conversation opens
            # that EXACT conversation directly, the dropdown's whole
            # point being to skip the generic list.
            node_id = str(event.value)
            self._switch_chat_mode("dms")
            long_name, short_name = self._dm_identity(node_id)
            self._open_dm_conversation(node_id, long_name=long_name, short_name=short_name)
            return
        if event.setting_name == "device_path":
            await self._switch_device(str(event.value))
            return
        if event.setting_name in RADIO_SETTINGS:
            self._apply_radio_setting(event.dropdown, event.setting_name, event.value)
            return
        if event.setting_name == "clock_auto_sync":
            # Item 6 of "RADIO -- SIMPLIFY CLOCK SYNC UX": the changed
            # dropdown value itself is sufficient confirmation -- no
            # separate "AUTO SYNC ENABLED/DISABLED" status line.
            self.settings.set_clock_auto_sync(bool(event.value))
            self.settings.save()
            return
        if event.setting_name == "network":
            self._on_network_selected(str(event.value))
            return
        if event.setting_name == "radio_mode":
            # Editing the RADIO MODE draft disarms any pending SAVE
            # confirm, exactly like the plain editor fields do.
            self._disarm_advanced_radio_confirm()
            return

        if event.setting_name == "font_size":
            try:
                self.settings.set_font_size(int(event.value))
                self.settings.save()
                self.settings.update_lxterminal_profile()
            except (OSError, ValueError) as error:
                self._set_font_size_status(f"UI SCALE NOT SAVED — {error}", "setting-error")
            else:
                self._set_font_size_status(
                    "UI SCALE SAVED - RELAUNCH TO APPLY", "setting-success"
                )
            return
        if event.setting_name == "color":
            # Item ("RADIO POLISH -- REMOVE COLOR SAVED"): the visible
            # theme switch and the selected COLOR value are themselves
            # sufficient confirmation -- no success status is ever
            # shown here, only a genuine persistence failure.
            try:
                self.settings.set_color(str(event.value))
                self.settings.save()
                self._apply_color_theme(self.settings.color)
            except (OSError, ValueError) as error:
                self._set_color_status(f"COLOR NOT SAVED — {error}", "setting-error")
            else:
                self._set_color_status("", None)
            return

    async def _switch_device(self, device_path: str) -> None:
        if device_path == getattr(self.radio, "device_path", None):
            return
        status = self.query_one("#connection-error", Static)
        try:
            self.settings.set_device_path(device_path)
            self.settings.save()
            self._monitor.stop()
            self.radio.set_device_path(device_path)
            self._monitor = RadioMonitor(
                self.radio,
                self._radio_event_from_thread,
                self._message_from_thread,
            )
            self._show_connection(RadioState.CONNECTING)
            self._monitor.start()
        except (OSError, ValueError) as error:
            status.update(f"USB DEVICE NOT CHANGED — {error}")

    def _connection_nav_controls(self) -> list[Widget]:
        """The explicit, ordered CONNECTION/CONFIG up/down focus list --

        shared by the plain up/down dispatch (for non-Input rows) and
        the isinstance(self.focused, Input) branch above (for the
        ADVANCED RADIO NEW NETWORK editor's own plain Input fields,
        which that branch's own early-return would otherwise swallow
        up/down for) -- one definition, never two independently
        maintained copies of this order.

        Focus order follows the visual section order (CONNECTION ->
        NETWORK -> RADIO -> STYLE). Only ONE of the NETWORK-section
        tails is ever included: the collapsed [ NEW PRESET ] row, or --
        when the transient editor is open -- NETWORK NAME/RADIO MODE/
        FREQ. SLOT/KEY then SAVE. The hidden side is left out entirely
        so a `display: none` row never becomes an unreachable focus
        stop. CANCEL is deliberately NOT a vertical stop -- [ SAVE ]
        [ CANCEL ] share one row and CANCEL is reached from SAVE with
        RIGHT (see on_key); vertical nav from CANCEL is delegated to
        its SAVE sibling.
        """
        head: tuple[Widget, ...] = (
            self.query_one(DeviceSelector),
            self.query_one(LongNameControl),
            self.query_one(ShortNameControl),
            self.query_one(NetworkSelector),
        )
        if self._network_editor_open:
            network_tail: tuple[Widget, ...] = (
                self.query_one("#network-name-input", Input),
                self.query_one(RadioModeSelector),
                self.query_one("#freq-slot-input", Input),
                self.query_one("#key-input", Input),
                self.query_one(SaveNetworkControl),
            )
        else:
            network_tail = (self.query_one(NewNetworkControl),)
        rest: tuple[Widget, ...] = (
            self.query_one(RoleSelector),
            self.query_one(BluetoothSelector),
            self.query_one(TimezoneSelector),
            self.query_one(ScreenTimeoutSelector),
            self.query_one(UnitsSelector),
            self.query_one(CompassSelector),
            self.query_one(FlipScreenSelector),
            self.query_one(Clock24HSelector),
            self.query_one(HopLimitSelector),
            self.query_one(AutoSyncSelector),
            self.query_one(FontSizeSelector),
            self.query_one(ColorSelector),
        )
        return [
            control
            for control in (*head, *network_tail, *rest)
            if not getattr(control, "disabled", False)
        ]

    def _move_connection_focus(self, step: int, *, origin: Widget | None = None) -> bool:
        """Move focus to the previous/next CONNECTION row; True if it did

        (the caller's own responsibility to check self.current_tab ==
        "connection" first and event.stop() on a True return). `origin`
        overrides "which row we're moving FROM" -- used for CANCEL,
        whose vertical movement is measured from its SAVE sibling since
        CANCEL itself is not in the vertical list.
        """
        controls = self._connection_nav_controls()
        current_widget = origin if origin is not None else self.focused
        if current_widget not in controls:
            return False
        current = controls.index(current_widget)
        target = controls[(current + step) % len(controls)]
        target.focus()
        target.scroll_visible(animate=False)
        return True

    def _refresh_device_options(self) -> None:
        try:
            devices = tuple(self.radio.available_device_paths())
        except Exception:
            devices = ()
        selector = self.query_one(DeviceSelector)
        selector.set_options(
            (DropdownOption(path, path) for path in devices),
            value=getattr(self.radio, "device_path", self.settings.device_path),
        )

    def _apply_radio_setting(
        self,
        dropdown: KeyboardDropdown,
        setting_name: str,
        dropdown_value: Any,
    ) -> None:
        """Begin a verified RADIO-section config write -- see

        RadioService.write_verified_config_field for what "verified"
        means. Shows APPLYING... immediately; radio_setting_applied()
        shows APPLIED/reverts the row once the worker thread's result
        arrives. Never claims APPLIED merely because this call returned.
        """
        spec = RADIO_SETTINGS[setting_name]
        # TIMEZONE/ROLE get their own dedicated, aligned, auto-
        # dismissing status row (see _set_timezone_status/
        # _set_role_status) instead of the shared #radio-status used by
        # every other RADIO_SETTINGS dropdown -- matching the LONG
        # NAME/SHORT NAME per-field layout.
        dedicated = self._dedicated_status_setter(setting_name)
        if dedicated is not None:
            if self._radio_state is not RadioState.ONLINE:
                dedicated(f"{dropdown.label} UNAVAILABLE — RADIO NOT CONNECTED", "setting-error")
                return
            dedicated(f"SAVING {dropdown.label}...", None)
        else:
            status = self.query_one("#radio-status", Static)
            if self._radio_state is not RadioState.ONLINE:
                status.remove_class("setting-success")
                status.add_class("setting-error")
                status.update(f"{dropdown.label} UNAVAILABLE — RADIO NOT CONNECTED")
                return
            status.remove_class("setting-error")
            status.remove_class("setting-success")
            status.update(f"APPLYING {dropdown.label}...")
        schema_value = spec.to_schema_value(dropdown_value)
        self._run_radio_worker(
            "apply-radio-setting",
            lambda: self._apply_radio_setting_from_thread(
                dropdown, spec, setting_name, schema_value
            ),
        )

    def _apply_radio_setting_from_thread(
        self,
        dropdown: KeyboardDropdown,
        spec: RadioSettingSpec,
        setting_name: str,
        schema_value: Any,
    ) -> None:
        try:
            result = self.radio.write_verified_config_field(spec.section, spec.field, schema_value)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            result = ConfigWriteResult(False, schema_value, None, f"error: {detail}")
        self.post_message(RadioSettingApplied(dropdown, setting_name, result))

    _RADIO_FAILURE_REASONS = {
        "not_connected": "RADIO NOT CONNECTED",
        "disconnected": "CONNECTION LOST",
        "nak": "REJECTED BY RADIO",
        "timeout": "TIMED OUT",
        "mismatch": "READBACK MISMATCH",
    }

    # BLUETOOTH's changed value is already sufficient confirmation on
    # its own (see RoleSelector/BluetoothSelector's own docstrings) --
    # no redundant "BLUETOOTH APPLIED" success line, matching the same
    # reasoning already applied to COLOR/AUTO SYNC. A genuine failure
    # still surfaces through the shared #radio-status normally. HOP
    # LIMIT joins this set per its own explicit requirement (PART E):
    # the visibly changed dropdown value is sufficient confirmation --
    # no "HOP LIMIT SAVED" success noise -- while a real failure still
    # surfaces an ERROR through the exact same shared #radio-status path.
    _SILENT_SUCCESS_SETTINGS = {"bluetooth", "hop_limit"}

    def _dedicated_status_setter(
        self, setting_name: str
    ) -> Callable[[str, str | None], None] | None:
        """TIMEZONE/ROLE each have their own aligned, auto-dismissing

        status row instead of the shared #radio-status -- see
        _set_timezone_status/_set_role_status.
        """
        if setting_name == "timezone":
            return self._set_timezone_status
        if setting_name == "role":
            return self._set_role_status
        return None

    def _dropdown_options_for_write(self, setting_name: str, schema_value: Any) -> Any:
        """TIMEZONE's options must be recomputed (CUSTOM injection) for

        whatever value a write/readback just settled on -- see
        _timezone_options_for. Every other dropdown's own `.options` is
        already the complete, static choice list.
        """
        if setting_name == "timezone":
            return self._timezone_options_for(schema_value)
        return None

    @on(RadioSettingApplied)
    def radio_setting_applied(self, event: RadioSettingApplied) -> None:
        spec = RADIO_SETTINGS[event.setting_name]
        dedicated = self._dedicated_status_setter(event.setting_name)
        if event.result.applied:
            if dedicated is not None:
                dedicated(f"{event.dropdown.label} SAVED", "setting-success")
            elif event.setting_name not in self._SILENT_SUCCESS_SETTINGS:
                status = self.query_one("#radio-status", Static)
                status.remove_class("setting-error")
                status.add_class("setting-success")
                status.update(f"{event.dropdown.label} APPLIED")
            else:
                status = self.query_one("#radio-status", Static)
                status.remove_class("setting-error")
                status.remove_class("setting-success")
                status.update("")
            options = self._dropdown_options_for_write(
                event.setting_name, event.result.readback_value
            )
            event.dropdown.set_options(
                event.dropdown.options if options is None else options,
                value=spec.from_schema_value(event.result.readback_value),
            )
        else:
            reason = self._RADIO_FAILURE_REASONS.get(
                event.result.reason, event.result.reason.upper()
            )
            if dedicated is not None:
                dedicated(f"{event.dropdown.label} NOT SAVED — {reason}", "setting-error")
            else:
                status = self.query_one("#radio-status", Static)
                status.remove_class("setting-success")
                status.add_class("setting-error")
                status.update(f"{event.dropdown.label} NOT APPLIED — {reason}")
            # Return the row to the authoritative radio value rather than
            # leaving the user's rejected selection displayed as if it
            # had taken effect.
            authoritative = self.radio.read_synced_config_field(spec.section, spec.field)
            if authoritative is not None:
                options = self._dropdown_options_for_write(event.setting_name, authoritative)
                event.dropdown.set_options(
                    event.dropdown.options if options is None else options,
                    value=spec.from_schema_value(authoritative),
                )

    # ---- ADVANCED RADIO (NETWORK selector + transient NEW NETWORK editor)

    def _network_preset(self, name: str) -> RadioConfigPreset | None:
        """The RadioConfigPreset for a NETWORK name -- a user-saved one

        if it exists, else the built-in LongFast, else None. A user
        NETWORK saved under the name "LongFast" deliberately shadows the
        built-in (spec M: the built-in only has to remain *available*).
        """
        saved = self.settings.get_radio_config_preset(name)
        if saved is not None:
            return saved
        if name == BUILTIN_LONGFAST_NETWORK:
            return builtin_longfast_preset()
        return None

    def _network_options(self) -> list[DropdownOption]:
        names = list(self.settings.radio_config_preset_names())
        options: list[DropdownOption] = []
        if BUILTIN_LONGFAST_NETWORK not in names:
            options.append(
                DropdownOption(BUILTIN_LONGFAST_NETWORK, BUILTIN_LONGFAST_NETWORK)
            )
        options.extend(DropdownOption(name, name) for name in names)
        return options

    def _refresh_network_options(self) -> None:
        """Rebuild the PRESET dropdown from the built-in LongFast plus

        the saved PRESET list, and re-assert the current selection as
        the shown value -- never auto-applied. When the last successful
        radio sync matched no saved PRESET (_network_unmatched), the
        shown value is the honest UNMATCHED_NETWORK_LABEL placeholder
        instead: display-only, never added to the options, so the open
        menu still lists only real PRESETs. Called on mount, on every
        connection-state change (see _render_radio_settings), and after
        a SAVE/switch, so it is never left stale relative to the saved
        list or a pending-then-abandoned selector choice.
        """
        selector = self.query_one(NetworkSelector)
        options = self._network_options()
        valid = {option.value for option in options}
        if self._selected_network not in valid:
            self._selected_network = BUILTIN_LONGFAST_NETWORK
        shown = (
            UNMATCHED_NETWORK_LABEL
            if self._network_unmatched
            else self._selected_network
        )
        selector.set_options(options, value=shown)

    def _detect_active_network_from_radio(self) -> None:
        """Derive the ACTIVE PRESET from the actual radio configuration

        after a successful config sync -- the radio is authoritative,
        never the app's previous assumption (fresh install, restart,
        the built-in LongFast default, or whatever was selected before
        the radio was changed externally). Called from _show_connection
        on every genuine non-ONLINE -> ONLINE transition, once the
        SDK's full config sync has already completed inside connect(),
        and skipped while a NETWORK apply is in flight (its own
        verification owns the outcome there).

        STRICTLY READ-ONLY: each candidate comparison reuses
        verify_radio_config_preset -- the SAME semantic rules the apply
        verification uses (modem preset, frequency slot with LongFast
        channel_num=0 semantics, PSK default-key equivalence) -- which
        only reads the SDK's already-synced cache. Zero RF, zero config
        writes, and NEVER an automatic "correction" of the radio toward
        whichever PRESET the app previously believed was active.

        Exactly one match -> that PRESET is selected/displayed. More
        than one semantically identical match -> the current selection
        wins if it is among them, else the first in the selector's own
        order (built-in LongFast first, then saved order). No match ->
        the honest UNMATCHED_NETWORK_LABEL placeholder (see
        _refresh_network_options); no PRESET is created or persisted.
        """
        matches: list[str] = []
        for option in self._network_options():
            name = str(option.value)
            preset = self._network_preset(name)
            if preset is None:
                continue
            try:
                verification = verify_radio_config_preset(self.radio, preset)
            except Exception as error:
                self._log_netcfg(
                    f"detect: verify {name!r} ERROR {error.__class__.__name__}"
                )
                continue
            if verification.ok:
                matches.append(name)
        if matches:
            if not self._network_unmatched and self._selected_network in matches:
                chosen = self._selected_network
            else:
                chosen = matches[0]
            self._log_netcfg(
                f"detect: radio matches {matches!r} -> active PRESET {chosen!r}"
            )
            self._selected_network = chosen
            self._network_unmatched = False
        else:
            self._log_netcfg(
                "detect: radio config matches no saved PRESET -> "
                f"{UNMATCHED_NETWORK_LABEL!r} (read-only, nothing written)"
            )
            self._network_unmatched = True
        self._refresh_network_options()

    def _network_editor_fields(
        self,
    ) -> tuple[Input, RadioModeSelector, Input, Input]:
        return (
            self.query_one("#network-name-input", Input),
            self.query_one(RadioModeSelector),
            self.query_one("#freq-slot-input", Input),
            self.query_one("#key-input", Input),
        )

    def _network_editor_rows(self) -> list[Widget]:
        return [
            self.query_one("#advanced-radio-editor-spacer", Static),
            self.query_one("#network-name-row", NetworkFieldInput),
            self.query_one(RadioModeSelector),
            self.query_one("#freq-slot-row", NetworkFieldInput),
            self.query_one("#key-row", NetworkFieldInput),
            self.query_one("#advanced-radio-actions", Horizontal),
        ]

    def _set_network_editor_open(self, is_open: bool) -> None:
        """Reveal/hide the transient NEW NETWORK editor rows. Toggles

        each widget's inline `display` (overriding the
        .advanced-radio-editor `display: none` default), and mirrors
        the [ NEW NETWORK ] control the opposite way, so exactly one of
        the two is ever visible/focusable.
        """
        self._network_editor_open = is_open
        for row in self._network_editor_rows():
            row.display = is_open
        self.query_one(NewNetworkControl).display = not is_open

    def _reset_network_editor_fields(self) -> None:
        name_input, mode_selector, freq_input, key_input = self._network_editor_fields()
        name_input.value = ""
        freq_input.value = ""
        key_input.value = ""
        mode_selector.set_options(
            (DropdownOption(label, value) for label, value in modem_preset_choices()),
            value="LONG_FAST",
        )

    def _collapse_network_editor(self) -> None:
        """Discard the NEW NETWORK editor's unsaved contents and hide it.

        Zero RF, zero persistence, creates no NETWORK, shows no
        unsaved-changes confirmation -- see spec E/F. Safe to call when
        the editor is already collapsed (an idempotent no-op).
        """
        self._disarm_advanced_radio_confirm()
        self._reset_network_editor_fields()
        self._set_network_editor_open(False)

    def _set_advanced_radio_status(self, text: str, css_class: str | None) -> None:
        """#advanced-radio-status. Non-empty text is indented to the same

        control/value column the form controls' "[ ... ]" start at
        (CONNECTION_VALUE_COLUMN_INDENT), never the far-left label
        column (spec J). `css_class`: "setting-accent" for pending AND
        normal success (spec D/J both say ACCENT), "setting-error" for a
        genuine failure (never ACCENT), None for a neutral confirm
        prompt.

        Non-empty status is scrolled just into view (minimal scroll, no
        jump to the top) so the user can always see the result of the
        NETWORK action they just triggered -- SWITCHING..., WAITING FOR
        RECONNECT..., APPLIED, NOT APPLIED... (spec item 6).

        Every status write disarms any pending success auto-dismiss
        FIRST -- the single-funnel guarantee that a stale 10s timer can
        never clear a newer message (only _finish_network_apply's
        success branch ever re-arms it, for exactly the line it just
        wrote).
        """
        if self._network_status_dismiss_timer is not None:
            self._network_status_dismiss_timer.stop()
            self._network_status_dismiss_timer = None
        status = self.query_one("#advanced-radio-status", Static)
        status.remove_class("setting-error")
        status.remove_class("setting-success")
        status.remove_class("setting-accent")
        if css_class:
            status.add_class(css_class)
        status.update(f"{CONNECTION_VALUE_COLUMN_INDENT}{text}" if text else "")
        if text and self.current_tab == "connection":
            self.call_after_refresh(self._scroll_advanced_radio_status_into_view)

    def _scroll_advanced_radio_status_into_view(self) -> None:
        try:
            self.query_one("#advanced-radio-status").scroll_visible(animate=False)
        except Exception:
            pass

    def _dismiss_network_apply_success(self) -> None:
        # The 10s success auto-dismiss fired with no newer status having
        # been written in the meantime (any newer write would have
        # stopped this timer via _set_advanced_radio_status) -- clear
        # the "<name> APPLIED" line.
        self._network_status_dismiss_timer = None
        self._set_advanced_radio_status("", None)

    def _arm_advanced_radio_confirm(self, action: str) -> None:
        self._advanced_radio_confirm = action
        if self._advanced_radio_confirm_timer is not None:
            self._advanced_radio_confirm_timer.stop()
        self._advanced_radio_confirm_timer = self.set_timer(
            ADVANCED_RADIO_CONFIRM_SECONDS, self._advanced_radio_confirm_expired
        )

    def _disarm_advanced_radio_confirm(self) -> None:
        self._advanced_radio_confirm = None
        if self._advanced_radio_confirm_timer is not None:
            self._advanced_radio_confirm_timer.stop()
            self._advanced_radio_confirm_timer = None

    def _advanced_radio_confirm_expired(self) -> None:
        # Only the NEW NETWORK [ SAVE ] press-again-to-confirm is ever
        # armed now -- switching between saved NETWORKs is a single
        # ENTER with no confirmation (see _on_network_selected).
        self._advanced_radio_confirm_timer = None
        if self._advanced_radio_confirm is not None:
            self._advanced_radio_confirm = None
            self._set_advanced_radio_status("", None)

    @on(NewNetworkControl.Activated)
    def open_new_network(self, _event: NewNetworkControl.Activated) -> None:
        """[ NEW NETWORK ] -- reveal a blank transient editor. Zero RF.

        Brings the newly revealed editor into view and focuses NETWORK
        NAME -- scrolling only as much as needed, never jumping the
        CONNECTION view back to the top (spec H).
        """
        self._disarm_advanced_radio_confirm()
        self._reset_network_editor_fields()
        self._set_network_editor_open(True)
        self._set_advanced_radio_status("", None)
        self._reveal_network_editor()

    def _reveal_network_editor(self) -> None:
        def _do() -> None:
            try:
                name_input = self.query_one("#network-name-input", Input)
            except Exception:
                return
            # Expose the whole revealed form, scrolling the minimum
            # amount: the SAVE/CANCEL row (its bottom edge) first, then
            # settle on NETWORK NAME so the first field and its cursor
            # are what the user actually lands on.
            self.query_one("#advanced-radio-actions").scroll_visible(animate=False)
            name_input.focus()
            name_input.scroll_visible(animate=False)

        self.call_after_refresh(_do)

    @on(CancelNetworkControl.Activated)
    def cancel_new_network(self, _event: CancelNetworkControl.Activated) -> None:
        """[ CANCEL ] -- discard + collapse the editor. Zero RF, no

        confirmation, selected NETWORK unchanged (spec F).
        """
        self._collapse_network_editor()
        self._set_advanced_radio_status("", None)
        self.call_after_refresh(lambda: self.query_one(NewNetworkControl).focus())

    @on(Input.Changed, "#network-name-input")
    def network_name_changed(self, _event: Input.Changed) -> None:
        self._disarm_advanced_radio_confirm()

    @on(Input.Changed, "#freq-slot-input")
    def network_freq_slot_changed(self, _event: Input.Changed) -> None:
        self._disarm_advanced_radio_confirm()

    @on(Input.Changed, "#key-input")
    def network_key_changed(self, _event: Input.Changed) -> None:
        self._disarm_advanced_radio_confirm()

    def _validated_network_from_editor(self) -> RadioConfigPreset | None:
        """Build a RadioConfigPreset from the editor, or set an error

        status and return None. NETWORK NAME is never written as the
        Meshtastic primary-channel name (channel_name stays "").
        """
        name_input, mode_selector, freq_input, key_input = self._network_editor_fields()
        name = name_input.value.strip()
        if not name:
            self._set_advanced_radio_status(
                "NOT SAVED — NETWORK NAME REQUIRED", "setting-error"
            )
            return None
        freq_text = freq_input.value.strip()
        try:
            frequency_slot = int(freq_text) if freq_text else 0
            if frequency_slot < 0:
                raise ValueError
        except ValueError:
            self._set_advanced_radio_status(
                "NOT SAVED — INVALID FREQ. SLOT", "setting-error"
            )
            return None
        key_text = key_input.value.strip()
        if key_text:
            try:
                base64.b64decode(key_text, validate=True)
            except Exception:
                self._set_advanced_radio_status(
                    "NOT SAVED — INVALID KEY (MUST BE BASE64)", "setting-error"
                )
                return None
        return RadioConfigPreset(
            name=name,
            modem_preset=str(mode_selector.value),
            frequency_slot=frequency_slot,
            channel_name="",
            channel_psk_base64=key_text,
        )

    @staticmethod
    def _log_netcfg(line: str) -> None:
        """One concise NETWORK diagnostic line -- rare (a deliberate,

        confirmed SAVE/switch, or one read-only detection pass per
        genuine (re)connect), never a continuous stream, and never
        secret key material (see _begin_network_apply's PSK line:
        length only). Plain print, matching rx_debug_log's own
        "lightweight terminal trace, not logging.*" precedent.
        """
        print(f"[NETCFG] {line}", flush=True)

    @on(SaveNetworkControl.Activated)
    def save_network(self, _event: SaveNetworkControl.Activated) -> None:
        """[ SAVE ] -- validate, confirm (RF/config WILL change), persist

        the NETWORK locally, then apply it through the radio-service
        boundary. There is no separate APPLY button (spec I).
        """
        preset = self._validated_network_from_editor()
        if preset is None:
            return
        if self._network_apply is not None:
            self._set_advanced_radio_status(
                f"APPLY IN PROGRESS — {self._network_apply.name}", None
            )
            return
        if self._advanced_radio_confirm != "save":
            self._arm_advanced_radio_confirm("save")
            self._set_advanced_radio_status(
                "PRESS SAVE AGAIN TO CONFIRM — SAVING WILL CHANGE THE CONNECTED "
                "RADIO'S NETWORK/RF CONFIGURATION AND MAY MOVE IT OFF ITS "
                "CURRENT NETWORK",
                None,
            )
            return
        self._disarm_advanced_radio_confirm()
        try:
            self.settings.save_radio_config_preset(preset)
            self.settings.save()
        except (OSError, ValueError) as error:
            # Local persistence failed -- keep the editor open so the
            # user can retry; nothing was applied.
            self._set_advanced_radio_status(f"NOT SAVED — {error}", "setting-error")
            return
        # Persisted. Select it, collapse the editor, restore NEW NETWORK
        # (spec I steps 6-8) -- the RF result is reported honestly in
        # the status line, never by falsely claiming it was applied.
        self._selected_network = preset.name
        self._network_unmatched = False
        self._refresh_network_options()
        self._collapse_network_editor()
        if self._radio_state is not RadioState.ONLINE:
            self._set_advanced_radio_status(
                f"{preset.name} SAVED — RADIO OFFLINE, NOT APPLIED", "setting-error"
            )
            return
        self._begin_network_apply(preset.name, preset, saved=True)

    def _on_network_selected(self, value: str) -> None:
        """NETWORK dropdown ENTER-selection. Opening the dropdown and

        moving through choices never reaches here (KeyboardDropdown only
        posts Selected on ENTER), and ESC just closes it -- both are
        zero RF. Selecting a DIFFERENT saved NETWORK is itself the
        commitment: it immediately begins the reboot-aware apply
        lifecycle with NO second ENTER and NO confirmation prompt. The
        NETWORK is not treated as active until post-reconnect readback
        verification succeeds (see _resolve_network_apply_after_reconnect).
        """
        if value == self._selected_network and not self._network_unmatched:
            # Re-selecting the active PRESET is a zero-write no-op.
            # While the radio is on an UNMATCHED configuration there is
            # no active PRESET, so every explicit selection -- the
            # previously believed name included -- is a genuine switch.
            if self._network_apply is None:
                self._set_advanced_radio_status("", None)
            return
        if self._network_apply is not None:
            # An apply is already in flight -- ignore the stray
            # selection and put the selector back where it was.
            self._refresh_network_options()
            return
        # Selecting a NETWORK abandons any unfinished NEW NETWORK editor
        # (spec K) -- same transient discard rules, never an accidental
        # SAVE of half-entered values.
        if self._network_editor_open:
            self._collapse_network_editor()
        self._disarm_advanced_radio_confirm()
        preset = self._network_preset(value)
        if preset is None:
            self._set_advanced_radio_status(
                f"UNKNOWN NETWORK — {value}", "setting-error"
            )
            self._refresh_network_options()
            return
        if self._radio_state is not RadioState.ONLINE:
            self._set_advanced_radio_status(
                "NOT APPLIED — RADIO NOT CONNECTED", "setting-error"
            )
            self._refresh_network_options()
            return
        self._begin_network_apply(value, preset, saved=False)

    # ---- NETWORK apply state machine (SAVE + switch share this) ----------

    def _begin_network_apply(
        self, name: str, preset: RadioConfigPreset, *, saved: bool
    ) -> None:
        """Start the ONE outstanding NETWORK apply. Writing LoRa

        modem_preset / channel_num / PSK reboots real Meshtastic
        firmware, so this operation is explicitly bounded: a
        NETWORK_APPLY_TIMEOUT_SECONDS timer guarantees a terminal
        SUCCESS/ERROR even if the SDK call never returns, and an
        expected mid-apply disconnect is folded into the lifecycle (see
        _on_connection_state_for_network_apply) rather than treated as
        failure.
        """
        self._network_apply_seq += 1
        token = self._network_apply_seq
        self._network_apply = NetworkApply(token, name, preset, saved)
        try:
            decoded_len = (
                len(base64.b64decode(preset.channel_psk_base64))
                if preset.channel_psk_base64
                else 0
            )
        except Exception:
            decoded_len = -1
        self._log_netcfg(
            f"apply#{token} REQUEST name={name!r} saved={saved} "
            f"modem_preset={preset.modem_preset} freq_slot={preset.frequency_slot} "
            f"psk={'present' if preset.channel_psk_base64 else 'absent'} "
            f"decoded_len={decoded_len}"
        )
        if self._network_apply_timer is not None:
            self._network_apply_timer.stop()
        self._network_apply_timer = self.set_timer(
            NETWORK_APPLY_TIMEOUT_SECONDS,
            lambda: self._network_apply_timed_out(token),
        )
        opening = (
            f"SAVING & APPLYING {name}..." if saved else f"SWITCHING TO {name}..."
        )
        self._set_advanced_radio_status(opening, "setting-accent")
        self._run_radio_worker(
            f"network-apply-{token}",
            lambda: self._apply_network_from_thread(token, name, preset, saved),
        )

    def _apply_network_from_thread(
        self, token: int, name: str, preset: RadioConfigPreset, saved: bool
    ) -> None:
        stage_log = lambda message: self._log_netcfg(f"apply#{token} {message}")
        try:
            result = apply_radio_config_preset(self.radio, preset, stage_log=stage_log)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            result = RadioApplyResult(
                False,
                "error",
                {"error": ConfigWriteResult(False, None, None, f"error: {detail}")},
            )
        self._log_netcfg(
            f"apply#{token} write returned applied={result.applied} "
            f"failed_step={result.failed_step or '-'}"
        )
        # A genuine write/commit/API stage error fails immediately --
        # no point verifying a write that did not go out. Only a write
        # that RETURNED applied gets the post-commit convergence poll.
        verification: RadioConfigVerification | None = None
        if result.applied:
            verification = self._verify_network_apply_converge(token, preset)
        self.post_message(
            RadioConfigPresetApplied(token, name, result, saved, verification)
        )

    # A fire-and-forget set_config + commit takes a moment to propagate
    # into the SDK's synced state on real hardware, so the FIRST fresh
    # readback can still show the OLD config -- which is what made a
    # switch "work only every few tries". After a write that returned
    # applied, poll a genuinely fresh readback ~once/second until it
    # converges on the requested NETWORK, or the bounded window passes.
    # Never a single big sleep; never blocks the UI thread (runs on the
    # apply worker). Well within the 90s overall safety timeout.
    _NETWORK_VERIFY_SETTLE_SECONDS = 0.5
    _NETWORK_VERIFY_DEADLINE_SECONDS = 12.0
    _NETWORK_VERIFY_INTERVAL_SECONDS = 1.0
    _NETWORK_VERIFY_REREAD_TIMEOUT = 3.0

    def _verify_network_apply_converge(
        self, token: int, preset: RadioConfigPreset
    ) -> RadioConfigVerification | None:
        """Post-commit convergence poll (apply worker thread).

        Returns the matching RadioConfigVerification the moment the
        radio's fresh readback matches the requested NETWORK; None if
        the radio dropped mid-window (the reconnect path then owns the
        verify -- see _on_connection_state_for_network_apply); or, at
        the deadline, the last mismatch (-> terminal field-named error)
        or None if the readback never answered (-> VERIFY READBACK
        TIMEOUT). Every attempt asks the radio for CURRENT values
        (reread_lora_and_primary_channel = fresh get_config +
        get_channel), never a re-check of one cached object. Zero
        config writes -- readback only.
        """
        sleep(self._NETWORK_VERIFY_SETTLE_SECONDS)
        started = monotonic()
        deadline = started + self._NETWORK_VERIFY_DEADLINE_SECONDS
        reread = getattr(self.radio, "reread_lora_and_primary_channel", None)
        last: RadioConfigVerification | None = None
        attempt = 0
        while True:
            attempt += 1
            current = self._network_apply
            if current is None or current.token != token:
                # This apply was superseded by a newer one, cancelled,
                # or already resolved -- stop polling the radio.
                self._log_netcfg(f"apply#{token} verify cancelled (superseded)")
                return None
            if self._radio_state is not RadioState.ONLINE:
                self._log_netcfg(
                    f"apply#{token} verify attempt={attempt} radio not ONLINE "
                    "-> reconnect path owns verify"
                )
                return None
            answered = True
            try:
                if callable(reread):
                    answered = bool(
                        reread(timeout=self._NETWORK_VERIFY_REREAD_TIMEOUT)
                    )
            except Exception as error:
                self._log_netcfg(
                    f"apply#{token} verify attempt={attempt} reread ERROR "
                    f"{error.__class__.__name__}"
                )
                answered = False
            if answered:
                last = verify_radio_config_preset(self.radio, preset)
                for check in last.checks:
                    self._log_netcfg(
                        f"apply#{token} verify attempt={attempt} {check.field} "
                        f"requested={check.requested} actual={check.actual} "
                        f"match={check.match}"
                    )
                if last.ok:
                    self._log_netcfg(
                        f"apply#{token} converged attempt={attempt} "
                        f"elapsed={monotonic() - started:.1f}s"
                    )
                    return last
            else:
                self._log_netcfg(
                    f"apply#{token} verify attempt={attempt} readback did not answer"
                )
            if monotonic() >= deadline:
                if last is not None:
                    unconverged = [c.field for c in last.checks if not c.match]
                    actual = ", ".join(f"{c.field}={c.actual}" for c in last.checks)
                    self._log_netcfg(
                        f"apply#{token} did NOT converge after {attempt} attempts "
                        f"{monotonic() - started:.1f}s -- unconverged={unconverged} "
                        f"final actual: {actual}"
                    )
                else:
                    self._log_netcfg(
                        f"apply#{token} did NOT converge -- readback never answered "
                        f"in {monotonic() - started:.1f}s"
                    )
                return last
            sleep(self._NETWORK_VERIFY_INTERVAL_SECONDS)

    @on(RadioConfigPresetApplied)
    def radio_config_preset_applied(self, event: RadioConfigPresetApplied) -> None:
        apply = self._network_apply
        if apply is None or apply.token != event.token:
            # A late return from a superseded / already-timed-out
            # attempt: never misattributed to whatever is current now.
            return
        if apply.awaiting_reconnect:
            # A disconnect was already observed while the worker ran
            # (commit rebooted the radio). Its verification -- if any --
            # is pre-reboot and stale; the reconnect full-sync + verify
            # is authoritative (see _resolve_network_apply_after_reconnect).
            self._log_netcfg(
                f"apply#{apply.token} worker returned but disconnect already "
                "observed -- reconnect owns the verify"
            )
            return
        result = event.result
        step_result = (
            result.results.get(result.failed_step) if not result.applied else None
        )
        raw_reason = step_result.reason if step_result is not None else ""
        # WAITING FOR RECONNECT is entered ONLY on a real observed
        # ONLINE -> non-ONLINE transition correlated with this apply --
        # never merely because a write/ACK/readback timed out. A generic
        # "timeout"/"not_connected" service string is NOT evidence the
        # radio rebooted (hardware has shown these writes do NOT reboot
        # it).
        if self._radio_state is not RadioState.ONLINE or raw_reason == "disconnected":
            apply.awaiting_reconnect = True
            self._log_netcfg(
                f"apply#{apply.token} radio down after "
                f"{result.failed_step or 'commit'} ({raw_reason or '-'}) "
                "-> awaiting reboot/reconnect"
            )
            self._set_advanced_radio_status(
                f"APPLYING {apply.name} — RADIO REBOOTED, WAITING FOR RECONNECT...",
                "setting-accent",
            )
            return
        # The radio is still connected. A write STAGE that failed is a
        # terminal error naming that stage.
        if not result.applied:
            label = NETWORK_STAGE_LABELS.get(
                result.failed_step, (result.failed_step or "WRITE").upper()
            )
            self._finish_network_apply(
                success=False, detail=f"{label} {(raw_reason or 'FAILED').upper()}"
            )
            return
        # Writes went out; verify from the fresh readback. A readback
        # that never answered while still connected is itself a terminal
        # failure -- NOT a reboot, NOT an indefinite wait.
        if event.verification is None:
            self._log_netcfg(
                f"apply#{apply.token} connected but readback did not answer"
            )
            self._finish_network_apply(
                success=False, detail="VERIFY READBACK TIMEOUT"
            )
            return
        self._finish_network_apply_from_verification(
            event.verification, source="post-write"
        )

    def _finish_network_apply_from_verification(
        self, verification: RadioConfigVerification, *, source: str
    ) -> None:
        apply = self._network_apply
        if apply is None:
            return
        # The post-write convergence poll already logged every attempt's
        # per-field lines; only the reconnect path needs them here.
        if source != "post-write":
            for check in verification.checks:
                self._log_netcfg(
                    f"apply#{apply.token} verify[{source}] {check.field} "
                    f"requested={check.requested} actual={check.actual} "
                    f"match={check.match}"
                )
        self._log_netcfg(
            f"apply#{apply.token} verify[{source}] {verification.channel_name_note}"
        )
        if verification.ok:
            self._finish_network_apply(
                success=True, detail=f"readback-verified ({source})"
            )
            return
        label = NETWORK_FIELD_LABELS.get(
            verification.mismatched_field,
            f"{verification.mismatched_field.upper() or 'READBACK'} MISMATCH",
        )
        self._finish_network_apply(success=False, detail=label)

    def _on_connection_state_for_network_apply(
        self, state: RadioState, was_online: bool
    ) -> None:
        """Fold an EXPECTED mid-apply disconnect/reconnect into the

        NETWORK apply lifecycle -- called from _show_connection on every
        radio-state transition. A disconnect while an apply is
        outstanding is the LoRa-config reboot, not a failure; the
        following reconnect is where the new config is read back and the
        operation finally resolves. Scoped entirely to a live
        _network_apply -- ordinary connects/reconnects with nothing
        outstanding are untouched.
        """
        apply = self._network_apply
        if apply is None:
            return
        if state is not RadioState.ONLINE:
            if not apply.awaiting_reconnect:
                apply.awaiting_reconnect = True
                self._log_netcfg(
                    f"apply#{apply.token} disconnect observed during apply "
                    "-> awaiting reconnect"
                )
            self._set_advanced_radio_status(
                f"APPLYING {apply.name} — RADIO REBOOTED, WAITING FOR RECONNECT...",
                "setting-accent",
            )
            return
        # ONLINE again. Only a genuine reconnect after the expected
        # reboot triggers readback -- never a redundant "still ONLINE".
        if apply.awaiting_reconnect and not was_online:
            self._resolve_network_apply_after_reconnect()

    def _resolve_network_apply_after_reconnect(self) -> None:
        apply = self._network_apply
        if apply is None:
            return
        # The reconnect already ran a full config sync, so the SDK's
        # already-synced cache is fresh -- verify_radio_config_preset
        # only reads it (zero RF), safe on the UI thread.
        verification = verify_radio_config_preset(self.radio, apply.preset)
        self._finish_network_apply_from_verification(verification, source="reconnect")

    def _network_apply_timed_out(self, token: int) -> None:
        if self._network_apply is None or self._network_apply.token != token:
            return
        self._log_netcfg(f"apply#{token} timed out after {NETWORK_APPLY_TIMEOUT_SECONDS}s")
        self._finish_network_apply(success=False, detail="timed out")

    def _finish_network_apply(self, *, success: bool, detail: str) -> None:
        apply = self._network_apply
        if apply is None:
            return
        if self._network_apply_timer is not None:
            self._network_apply_timer.stop()
            self._network_apply_timer = None
        self._network_apply = None
        self._log_netcfg(
            f"apply#{apply.token} FINAL success={success} ({detail})"
        )
        if success:
            self._selected_network = apply.name
            self._network_unmatched = False
            self._refresh_network_options()
            prefix = "SAVED & " if apply.saved else ""
            # Spec D/J: normal success uses ACCENT here, not the
            # confirm-green .setting-success other CONNECTION rows use.
            self._set_advanced_radio_status(
                f"{apply.name} {prefix}APPLIED", "setting-accent"
            )
            # Success only: leave "<name> APPLIED" visible for a fixed
            # window, then clear it. _set_advanced_radio_status (just
            # called) stopped any previously pending dismiss, so this
            # timer is always correlated with exactly the line above;
            # any NEWER status/error written during the window disarms
            # it again before it can fire.
            self._network_status_dismiss_timer = self.set_timer(
                NETWORK_STATUS_SUCCESS_DISMISS_SECONDS,
                self._dismiss_network_apply_success,
            )
            return
        # Failure. A SAVE stays saved+selected locally (honest: "SAVED
        # -- NOT APPLIED"); a bare switch reverts the selector to the
        # actual current NETWORK. Never claims the failed NETWORK is
        # active.
        if not apply.saved:
            self._refresh_network_options()
        prefix = "SAVED — " if apply.saved else ""
        self._set_advanced_radio_status(
            f"{apply.name} {prefix}NOT APPLIED: {detail}", "setting-error"
        )

    def _reset_clock_sync_state(self) -> None:
        """Invalidate whatever the OLD connection's in-flight AUTO SYNC

        write was doing -- called by _show_connection on every genuine
        connection-state transition (disconnect, error, a fresh
        (re)connect), never on a redundant "still ONLINE" event (see
        its own call site). Bumping the generation makes a late
        completion from an abandoned connection's worker thread safely
        stale (see clock_sync_applied's own guard); clearing "in
        progress" means a genuinely stuck old write can never block the
        NEW connection's own one-time sync (see _maybe_auto_sync_clock).
        """
        self._clock_sync_in_progress = False
        self._clock_sync_generation += 1

    def _maybe_auto_sync_clock(self) -> None:
        """CLOCK SYNC's only trigger -- at most once per qualifying

        connection lifecycle (see _show_connection, the only caller),
        never on tab/view/focus/render/config-snapshot activity, never
        repeated mid-lifecycle. Entirely silent (see "FINAL CLOCK UI
        SIMPLIFICATION"): no UI reflects this call either way -- it
        performs RadioService.sync_clock() (host epoch) AND a best-
        effort host-timezone -> device.tzdef sync (see
        _sync_host_timezone_from_thread) during connection
        establishment. A real failure in either is only ever a silent
        internal fact: it must never block app connection/startup (see
        _apply_sync_clock_from_thread's own try/except).

        No trustworthy get-time/RTC-validity signal exists to sync
        "only when needed" instead (see ClockSyncResult's own
        docstring: AdminMessage has no get-time RPC -- confirmed
        against the installed meshtastic==2.7.11 schema), and
        RadioService's connection state machine has no separate "this
        reconnect followed a reboot" signal either -- a dropped
        connection always re-enters RadioState.CONNECTING identically
        (see _connection_status_rich_text's own docstring). Given the
        real-hardware finding that this device's clock does NOT
        reliably survive a reboot, one sync per NEW connection
        lifecycle is the documented, audit-sanctioned fallback:
        harmless if the clock already survived, corrective if it
        didn't.
        """
        if not self.settings.clock_auto_sync or self._clock_auto_sync_done_this_connection:
            return
        self._clock_auto_sync_done_this_connection = True
        if self._clock_sync_in_progress:
            return
        self._clock_sync_generation += 1
        generation = self._clock_sync_generation
        self._clock_sync_in_progress = True
        self._run_radio_worker(
            "sync-clock", lambda: self._apply_sync_clock_from_thread(generation)
        )

    def _apply_sync_clock_from_thread(self, generation: int) -> None:
        try:
            result = self.radio.sync_clock()
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            result = ClockSyncResult(False, 0, None, f"error: {detail}")
        self._sync_host_timezone_from_thread()
        self.post_message(ClockSyncApplied(result, generation))

    def _sync_host_timezone_from_thread(self) -> None:
        """Best-effort host-timezone -> device.tzdef sync, run from the

        SAME background daemon thread as the epoch sync it always
        follows (see _apply_sync_clock_from_thread) -- entirely silent
        to the user (CLOCK SYNC's own contract), only ever a plain
        diagnostic print (see rx_debug_log's own "not logging.*, a
        lightweight opt-in-free terminal trace" precedent).

        Detecting/converting the host timezone (see host_timezone.
        detect_host_timezone) touches only the local filesystem --
        zero Meshtastic traffic by itself, regardless of outcome. A
        write is only ATTEMPTED when detection produced a confidently-
        derived tzdef (never a guess from the host's current UTC
        offset alone -- see detect_host_timezone's own docstring) AND
        the connected schema actually declares device.tzdef
        (read_synced_config_field is the SAME zero-RF cached
        capability probe the manual TIMEZONE control's own render-time
        check already uses). Detection/conversion failing, or the
        field being unsupported, always leaves device.tzdef untouched
        -- the epoch sync above still already happened regardless.

        Reuses write_verified_config_field's existing verified write
        -> readback -> snapshot-rebuild pipeline unchanged, so a
        confirmed write here updates the SAME cached
        RadioConfigurationSnapshot the TIMEZONE dropdown reads --
        which is why a successful write also asks the main thread to
        re-render RADIO settings below, so TIMEZONE reflects the
        actual confirmed radio state promptly rather than only after
        some unrelated future reconnect/redraw.
        """
        host = detect_host_timezone()
        if host.tzdef is None:
            print(f"[CLOCK SYNC] host timezone not synced: {host.detail}", flush=True)
            return
        current_tzdef = self.radio.read_synced_config_field("device", "tzdef")
        if current_tzdef is None:
            print(
                "[CLOCK SYNC] host timezone not synced: "
                "device.tzdef unsupported by this schema",
                flush=True,
            )
            return
        if current_tzdef == host.tzdef:
            # Already correct on the radio (the common steady state on
            # every reconnect) -- write NOTHING. A device set_config
            # admin write is a real config transaction the firmware
            # persists; repeating it once per connection lifecycle when
            # it changes nothing is exactly the kind of redundant
            # reconnect-time write the unexpected-reboot audit exists
            # to rule out.
            return
        try:
            result = self.radio.write_verified_config_field("device", "tzdef", host.tzdef)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            print(f"[CLOCK SYNC] host timezone write failed: error: {detail}", flush=True)
            return
        if not result.applied:
            print(f"[CLOCK SYNC] host timezone write not applied: {result.reason}", flush=True)
            return
        print(
            f"[CLOCK SYNC] host timezone synced to {host.iana_name} ({host.tzdef})",
            flush=True,
        )
        try:
            self.call_from_thread(self._render_radio_settings)
        except RuntimeError:
            # The UI may already be stopping while this worker finishes.
            pass

    @on(ClockSyncApplied)
    def clock_sync_applied(self, event: ClockSyncApplied) -> None:
        """One AUTO SYNC RadioService.sync_clock() call finished --

        purely internal bookkeeping now (see "FINAL CLOCK UI
        SIMPLIFICATION"): no UI surfaces the outcome either way,
        success or failure. A stale generation means a newer attempt
        (or a disconnect/reconnect -- see _reset_clock_sync_state) has
        since superseded this one, so it is safely ignored rather than
        e.g. resurrecting an "in progress" flag the new connection no
        longer cares about.
        """
        if event.generation != self._clock_sync_generation:
            return
        self._clock_sync_in_progress = False
        if event.result.applied:
            self._last_clock_sync_at = time()

    @staticmethod
    def _snapshot_config_field(snapshot, section: str, field: str) -> str | None:
        """One localConfig.<section>.<field> value out of an already-

        built RadioConfigurationSnapshot's local_config sections
        (already-stringified by radio_capabilities.describe_scalar_
        fields -- secrets already redacted at that source, never read
        here). None if the section/field isn't present on this
        installed schema, exactly like RadioService.
        read_synced_config_field's own "unavailable, never crash"
        contract.
        """
        for report in snapshot.local_config:
            if report.section == section:
                return report.fields.get(field)
        return None

    def _render_radio_settings(self) -> None:
        """Populate RADIO from the SDK's already-synced state (or mark

        it unavailable) -- called by _show_connection on every
        connect/reconnect/disconnect, exactly like _render_identity.
        Never issues a fresh config request itself (see item 17 of the
        RADIO-section task): a fresh read only ever happens inside
        write_verified_config_field, to verify a write this session
        just made.
        """
        self._refresh_network_options()
        info_widget = self.query_one("#radio-info", Static)
        timezone_dropdown = self.query_one(TimezoneSelector)
        dropdowns: tuple[tuple[KeyboardDropdown, RadioSettingSpec], ...] = (
            (self.query_one(RoleSelector), RADIO_SETTINGS["role"]),
            (self.query_one(BluetoothSelector), RADIO_SETTINGS["bluetooth"]),
            (self.query_one(ScreenTimeoutSelector), RADIO_SETTINGS["screen_timeout"]),
            (self.query_one(UnitsSelector), RADIO_SETTINGS["units"]),
            (self.query_one(CompassSelector), RADIO_SETTINGS["compass"]),
            (self.query_one(FlipScreenSelector), RADIO_SETTINGS["flip_screen"]),
            (self.query_one(Clock24HSelector), RADIO_SETTINGS["clock_24h"]),
            (self.query_one(HopLimitSelector), RADIO_SETTINGS["hop_limit"]),
        )
        palette = THEME_PALETTES[self._current_theme]
        online = self._radio_state is RadioState.ONLINE and self._radio_info is not None

        if not online:
            connecting = self._radio_state is RadioState.CONNECTING
            placeholder = "..." if connecting else "—"
            text = Text()
            for index, label in enumerate(("HARDWARE", "FIRMWARE")):
                if index:
                    text.append("\n")
                text.append(
                    f"{CONNECTION_ROW_PREFIX}{label:<{CONNECTION_LABEL_WIDTH}}",
                    style=palette.base,
                )
                text.append(" ", style=palette.base)
                text.append(placeholder, style=palette.dim_base)
            info_widget.update(text)
            for dropdown, _spec in dropdowns + ((timezone_dropdown, RADIO_SETTINGS["timezone"]),):
                override = Text()
                override.append(
                    f"{CONNECTION_ROW_PREFIX}{dropdown.label:<{CONNECTION_LABEL_WIDTH}}",
                    style=palette.base,
                )
                override.append(" ", style=palette.base)
                override.append(placeholder, style=palette.dim_base)
                dropdown.set_status_override(override)
            return

        # Item 4: HARDWARE/FIRMWARE read the cached RadioConfiguration
        # Snapshot -- built once at connect time (see RadioService.
        # connect/_rebuild_config_snapshot), never re-derived per
        # render -- rather than calling hardware_identity() fresh here.
        # Both are pure zero-RF reads of already-synced local objects
        # either way, so this is an architecture choice (one cached,
        # explicitly-invalidated source of truth), not a traffic fix.
        # TIMEZONE reads from this SAME snapshot (see below) for the
        # same reason; ROLE/BLUETOOTH are editable (see dropdowns
        # above) and read live via read_synced_config_field instead,
        # exactly like every other RADIO_SETTINGS dropdown.
        snapshot = getattr(self.radio, "config_snapshot", lambda: None)()
        if snapshot is None:
            # Item 11: a real, if normally brief (see item 3 -- the
            # real RadioService builds this synchronously inside
            # connect(), before ONLINE is ever announced), transient
            # state -- never a permanent one, and never a crash.
            text = Text()
            text.append(
                f"{CONNECTION_ROW_PREFIX}LOADING RADIO CONFIG...",
                style=palette.dim_base,
            )
            info_widget.update(text)
            tzdef = None
        else:
            tzdef = self._snapshot_config_field(snapshot, "device", "tzdef")
            text = Text()
            rows = (
                ("HARDWARE", format_hw_model_name(snapshot.hardware.hw_model_name)),
                ("FIRMWARE", snapshot.hardware.firmware_version or "—"),
            )
            for index, (label, value) in enumerate(rows):
                if index:
                    text.append("\n")
                text.append(
                    f"{CONNECTION_ROW_PREFIX}{label:<{CONNECTION_LABEL_WIDTH}}",
                    style=palette.base,
                )
                text.append(" ", style=palette.base)
                text.append(value, style=palette.base)
            info_widget.update(text)

        if tzdef is None:
            override = Text()
            override.append(
                f"{CONNECTION_ROW_PREFIX}{timezone_dropdown.label:<{CONNECTION_LABEL_WIDTH}}",
                style=palette.base,
            )
            override.append(" ", style=palette.base)
            override.append(
                "LOADING..." if snapshot is None else "UNSUPPORTED", style=palette.dim_base
            )
            timezone_dropdown.set_status_override(override)
        else:
            timezone_dropdown.set_status_override(None)
            timezone_dropdown.set_options(self._timezone_options_for(tzdef), value=tzdef)

        for dropdown, spec in dropdowns:
            dropdown.set_status_override(None)
            schema_value = self.radio.read_synced_config_field(spec.section, spec.field)
            if schema_value is None:
                override = Text()
                override.append(
                    f"{CONNECTION_ROW_PREFIX}{dropdown.label:<{CONNECTION_LABEL_WIDTH}}",
                    style=palette.base,
                )
                override.append(" ", style=palette.base)
                override.append("UNSUPPORTED", style=palette.dim_base)
                dropdown.set_status_override(override)
                continue
            dropdown.set_options(
                dropdown.options,
                value=spec.from_schema_value(schema_value),
            )

    def _apply_color_theme(self, color: str) -> None:
        self._current_theme = color
        for name, _value in COLOR_CHOICES:
            self.screen.remove_class(f"theme-{name.lower()}")
        self.screen.add_class(f"theme-{color}")
        palette = THEME_PALETTES[color]
        for dropdown in self.query(KeyboardDropdown):
            dropdown.set_palette(
                palette.base,
                palette.accent,
                palette.dim_base,
            )
        if self._emoji_picker is not None:
            self._emoji_picker.set_palette(palette.base, palette.accent)
        if len(self.query("#identity-values")):
            self._render_connection_details()
            self._render_identity()
            self._render_radio_settings()
        if len(self.query("#mesh-view")):
            self._refresh_mesh()
        # set_palette() above only refreshes a KeyboardDropdown's BASE
        # color -- CHAT's connection-status override (see
        # _update_chat_connection_state/_connection_status_color) is a
        # separately stored color, computed once when the override was
        # set, that set_palette() has no way to know needs recomputing
        # for the new theme. Recomputing it here keeps CHAT (and via the
        # same call, MESH's own status line) from briefly showing the
        # PREVIOUS theme's accent/error color until the next ~0.45s
        # animation tick naturally refreshed it.
        self._update_chat_connection_state()

    @on(Input.Submitted, "#chat-input")
    def send_chat_message(self, event: Input.Submitted) -> None:
        # Belt-and-suspenders: a disabled Input should already refuse to
        # focus/submit, but this guarantees sending stays refused even
        # if focus was already on the input at the moment reconnecting
        # began (see _update_chat_connection_state).
        if self._radio_state is not RadioState.ONLINE:
            return
        text = event.value
        if not text.strip():
            self._show_send_error("Message text cannot be empty.")
            return
        entry = self._start_outgoing(text)
        generation = entry.send_generation
        self.run_worker(
            lambda: self._send_from_thread(entry, generation),
            thread=True,
        )
        # UI / CHANNEL / RADIO CONFIG TUNING Part B: a successful send
        # (text was accepted -- both guard clauses above already
        # returned early without reaching here for empty text/offline
        # radio, so no typed text is ever discarded by this) returns
        # CHAT to its neutral navigation state -- the composer already
        # cleared its own value (_start_outgoing) and now loses focus
        # too, so the very next ordinary printable keypress starts a
        # NEW message from scratch via on_key's own unfocused-hotkey/
        # printable-character dispatch, rather than continuing to type
        # into an already-focused, already-empty composer.
        self.query_one("#chat-log", ChatTranscript).focus()

    @on(Input.Changed, "#chat-input")
    def chat_input_changed(self, _event: Input.Changed) -> None:
        """Typing dismisses the empty-message error immediately."""
        if self._send_error_message:
            self._show_send_error("")

    @on(ChatMessageInput.Left)
    def chat_input_lost_focus(self, _event: ChatMessageInput.Left) -> None:
        """Leaving the message-entry box dismisses the empty-message

        error immediately -- this also covers switching to a different
        top-level tab, since show_tab() moves focus away from here.
        """
        if self._send_error_message:
            self._show_send_error("")
        self._update_footer()

    def _send_from_thread(
        self,
        entry: ChatEntry,
        generation: int | None = None,
    ) -> None:
        attempt_generation = entry.send_generation if generation is None else generation

        def status_handler(status: SendStatus) -> None:
            self.post_message(
                DeliveryStatusReceived(entry, status, attempt_generation)
            )

        try:
            # entry.dm_node_id is None for every ordinary channel entry
            # (its existing, unchanged behavior) -- set only for a DM
            # entry (item 4: "Use the SDK's existing explicit-
            # destination send path"), where RadioService.send_text
            # forwards it as sendText's own destinationId, and also
            # uses it to require a DM's explicit ack come specifically
            # from THIS destination (item 6; see RadioService.
            # _parse_send_response). Never a custom RF protocol.
            sent = self.radio.send_text(
                entry.text,
                channel_index=entry.channel_index,
                destination_node_id=entry.dm_node_id,
                status_handler=status_handler,
            )
        except RadioSendError as error:
            self.post_message(SendFailed(entry, str(error), attempt_generation))
        else:
            self.post_message(SendSubmitted(entry, sent, attempt_generation))

    def _start_outgoing(self, text: str) -> ChatEntry:
        self._mark_current_channel_read_for_send()
        entry = outgoing_chat_entry(
            text,
            channel_index=self.current_channel_index,
            delivery_state=DeliveryState.SENDING,
        )
        entry.send_generation = 1
        self._assign_arrival_order(entry)
        self._persist_outgoing(entry)
        insert_index = self._insert_entry_in_order(self.chat_history, entry)
        self._insert_chat_widget(entry, insert_index, older=False)
        chat_input = self.query_one("#chat-input", Input)
        chat_input.value = ""
        self._show_send_error("")
        return entry

    def _accepted_send(self, text: str) -> None:
        """Compatibility helper for a locally accepted outgoing entry."""
        self._mark_current_channel_read_for_send()
        entry = outgoing_chat_entry(
            text,
            channel_index=self.current_channel_index,
            delivery_state=DeliveryState.SENT,
        )
        self._assign_arrival_order(entry)
        self._persist_outgoing(entry)
        insert_index = self._insert_entry_in_order(self.chat_history, entry)
        self._insert_chat_widget(entry, insert_index, older=False)
        chat_input = self.query_one("#chat-input", Input)
        chat_input.value = ""
        self._show_send_error("")

    @on(SendSubmitted)
    def send_was_submitted(self, event: SendSubmitted) -> None:
        """send_text() returning successfully is a purely LOCAL event --

        the SDK accepted the packet for local submission, nothing more.
        It must NOT by itself produce SENT (✓): a routing failure (NAK)
        remains a legitimate, still-open outcome for this exact packet
        until the first real routing response arrives (see
        RadioService._parse_send_response/_on_routing_response). The
        entry stays in SENDING (rendered as the animated arrow, see
        ChatEntryWidget.refresh_delivery_state) until that first
        response resolves it to SENT, HEARD, or FAILED.

        SimulatedRadioService's `immediate_state` is the one exception:
        it exists specifically so tests can force a deterministic
        outcome without simulating a real ack round-trip.
        """
        entry = event.entry
        if entry.deleted or event.generation != entry.send_generation:
            return
        entry.packet_id = event.sent.packet_id
        state = (
            entry.delivery_state
            if entry.delivery_state in (DeliveryState.HEARD, DeliveryState.FAILED)
            else event.sent.immediate_state or DeliveryState.SENDING
        )
        # Delivery-state monotonicity: a confirmation TIMEOUT exists only
        # to answer "nothing conclusive arrived" -- SENT is already a
        # genuine positive routing confirmation (see this method's own
        # docstring above), so it must never be scheduled for later
        # downgrade to UNCONFIRMED. Only genuine SENDING (still fully
        # open) gets a deadline; _set_delivery_state's own retirement
        # logic agrees (belt-and-suspenders -- see its docstring).
        entry.confirmation_deadline = (
            monotonic() + CHAT_CONFIRMATION_TIMEOUT_SECONDS
            if state is DeliveryState.SENDING
            else None
        )
        self._set_delivery_state(entry, state, packet_id=event.sent.packet_id)

    @on(SendFailed)
    def send_failed(self, event: SendFailed) -> None:
        if event.entry.deleted or event.generation != event.entry.send_generation:
            return
        self._set_delivery_state(
            event.entry,
            DeliveryState.FAILED,
            detail=event.detail,
        )
        if event.entry.dm_node_id is not None:
            self._show_dm_send_error(event.detail)
        else:
            self._show_send_error(event.detail)

    @on(DeliveryStatusReceived)
    def delivery_status_received(self, event: DeliveryStatusReceived) -> None:
        if event.entry.deleted or event.generation != event.entry.send_generation:
            return
        if event.entry.packet_id not in (None, event.status.packet_id):
            return
        # RadioService can now observe more than one routing response for
        # the same outgoing packet (a later, genuinely stronger
        # confirmation must not be silently dropped -- see
        # RadioService._on_routing_response). Once a message has reached
        # its strongest reachable state (HEARD) or a terminal FAILED, a
        # duplicate or weaker later update must never overwrite it.
        if event.entry.delivery_state in (DeliveryState.HEARD, DeliveryState.FAILED):
            return
        self._set_delivery_state(
            event.entry,
            event.status.state,
            packet_id=event.status.packet_id,
            detail=event.status.detail,
        )

    def _radio_event_from_thread(self, event: RadioEvent) -> None:
        try:
            self.call_from_thread(self._apply_radio_event, event)
        except RuntimeError:
            # The UI may already be stopping while the monitor exits.
            pass

    def _message_from_thread(self, message: ReceivedMessage) -> None:
        self.post_message(RadioMessageReceived(message))

    @on(RadioMessageReceived)
    def accept_radio_message(self, event: RadioMessageReceived) -> None:
        self._accept_received_message(event.message)

    def _apply_radio_event(self, event: RadioEvent) -> None:
        self._show_connection(event.state, event.info, event.message)

    def _accept_received_message(self, message: ReceivedMessage) -> None:
        try:
            self._refresh_mesh()
        except Exception:
            # A passive-topology refresh problem must never be able to
            # silently swallow a legitimate incoming CHAT message --
            # persistence below still has to run regardless.
            pass
        if message.is_direct:
            # RadioService's own packet-destination classification
            # (never a heuristic based on sender name/channel index --
            # item 3) already decided this; route it to its own DM
            # conversation, never mingled into channel history merely
            # because packet.channel happens to be present (item 12).
            self._accept_received_dm(message)
            return
        channel_index = message.channel_index or 0
        state = self._ensure_channel_loaded(channel_index)
        app_received_at = time()
        monotonic_now = monotonic()
        chat_is_visible = (
            self.current_tab == "chat"
            and self._chat_mode == "channel"
            and channel_index == self.current_channel_index
        )
        entry = received_chat_entry(
            message,
            app_received_at=app_received_at,
            monotonic_now=monotonic_now,
            unread=not chat_is_visible,
            is_new=True,
        )
        self._assign_arrival_order(entry)
        inserted = self._persist_incoming(entry)
        if rx_debug_enabled():
            if entry.message_id is not None:
                rx_debug_log(
                    f"CHAT STORE id={entry.message_id} channel={channel_index} "
                    + ("inserted" if inserted else "duplicate, ignored")
                )
            else:
                rx_debug_log(
                    f"CHAT STORE not persisted channel={channel_index} "
                    "reason=no_chat_store_attached"
                )
        if not inserted:
            return
        tail_key = state.entries[-1].order_key if state.entries else None
        is_older = (
            entry.message_time is not None
            and tail_key is not None
            and entry.order_key < tail_key
        )
        insert_index = self._ordered_insert_index(state.entries, entry)
        outside_mounted_window = (
            bool(state.entries)
            and insert_index == 0
            and entry.order_key < state.entries[0].order_key
            and (
                state.has_older_history
                or len(state.entries) >= state.mounted_target
            )
        )
        if entry.message_id is not None:
            state.new_message_ids.add(entry.message_id)
            if not chat_is_visible:
                state.unread_message_ids.add(entry.message_id)
        delivered_to_ui = False
        if not outside_mounted_window:
            state.entries.insert(insert_index, entry)
            if channel_index == self.current_channel_index:
                self._insert_chat_widget(entry, insert_index, older=is_older)
                delivered_to_ui = True
        else:
            state.has_older_history = True
            if channel_index == self.current_channel_index:
                self._has_older_history = True
                self._ensure_load_older_control(
                    self.query_one("#chat-log", ChatTranscript)
                )
                self._capture_current_channel_state()
        if rx_debug_enabled():
            if delivered_to_ui:
                rx_debug_log(f"CHAT UI id={entry.message_id} delivered")
            elif channel_index != self.current_channel_index:
                rx_debug_log(
                    f"CHAT UI id={entry.message_id} not displayed "
                    f"reason=current_channel={self.current_channel_index}"
                    f",message_channel={channel_index}"
                )
            else:
                rx_debug_log(
                    f"CHAT UI id={entry.message_id} not displayed "
                    "reason=outside_mounted_history_window"
                )
        if is_older:
            self._show_older_message_notice(channel_index, entry)
        if not chat_is_visible:
            state.unread_count += 1
            self._recount_unread()
        self._update_tab_bar()

    def _assign_arrival_order(self, entry: ChatEntry) -> None:
        self._arrival_sequence += 1
        entry.arrival_order = self._arrival_sequence

    @staticmethod
    def _ordered_insert_index(entries: list[ChatEntry], entry: ChatEntry) -> int:
        """Return a stable right-biased insertion point for equal clocks."""
        index = len(entries)
        while index and entry.order_key < entries[index - 1].order_key:
            index -= 1
        return index

    def _insert_entry_in_order(
        self,
        entries: list[ChatEntry],
        entry: ChatEntry,
    ) -> int:
        index = self._ordered_insert_index(entries, entry)
        entries.insert(index, entry)
        return index

    def _state_for(self, channel_index: int) -> ChannelChatState:
        return self._channel_states.setdefault(channel_index, ChannelChatState())

    def _channel_key_for(self, channel_index: int) -> str | None:
        """The live radio's own stable identity for this channel slot.

        None means "unknown" (index not found, or the radio hasn't
        reported a real ChannelSettings.id/psk for it yet -- e.g. the
        pre-connection placeholder channel list) -- see
        ChannelInfo.stable_key and ChannelChatState.loaded_key.
        """
        for channel in self._channels:
            if channel.index == channel_index:
                return channel.stable_key or None
        return None

    def _channel_label(self, channel_index: int) -> str:
        """The current channel selector's own presentation label.

        Falls back exactly like RadioService._read_channel_info's own
        "Channel {index + 1}" convention when the index isn't in the
        live list (e.g. not yet connected) -- so the empty-history
        marker (StartOfChannelHistoryMarker) always shows something
        sensible rather than a blank/placeholder name.
        """
        for channel in self._channels:
            if channel.index == channel_index:
                return channel.name
        return f"Channel {channel_index + 1}"

    def _capture_current_channel_state(self) -> None:
        state = self._state_for(self.current_channel_index)
        state.entries = self.chat_history
        state.transcript_new_count = self.transcript_new_count
        state.has_older_history = self._has_older_history
        state.mounted_target = self._mounted_chat_target
        state.open_scroll_pending = self._chat_open_scroll_pending
        chat_inputs = list(self.query("#chat-input"))
        if chat_inputs:
            state.draft = chat_inputs[0].value

    def _restore_channel_state(self, channel_index: int) -> ChannelChatState:
        state = self._ensure_channel_loaded(channel_index)
        self.current_channel_index = channel_index
        self.chat_history = state.entries
        self.transcript_new_count = len(state.new_below_ids)
        state.transcript_new_count = self.transcript_new_count
        self._has_older_history = state.has_older_history
        self._mounted_chat_target = state.mounted_target
        self._chat_open_scroll_pending = state.open_scroll_pending
        return state

    def _ensure_channel_loaded(self, channel_index: int) -> ChannelChatState:
        """Load `channel_index` from the store the FIRST time it is

        touched this app run, and never again after that (`state.loaded`
        latches permanently true) -- this method deliberately never
        re-queries or replaces `state.entries` for an already-loaded
        state: doing so here (rather than through
        _reconcile_current_channel_identity's own careful compare-then-
        remount) would silently desync self.chat_history's objects from
        whatever ChatEntryWidgets are already mounted for the CURRENT
        channel (identity comparisons like _trim_mounted_chat_window's
        `widget.entry is entry` would then never match). A same-slot
        identity change discovered later is handled entirely by
        _show_connection's own channel-sync block instead -- see
        _reconcile_current_channel_identity (current channel) and
        _invalidate_reassigned_channel_caches (every other channel).
        """
        state = self._state_for(channel_index)
        if state.loaded:
            return state
        state.loaded = True
        key = self._channel_key_for(channel_index)
        state.loaded_key = key
        if self.chat_store is None:
            return state
        try:
            page = self.chat_store.load_recent_page(
                channel_index=channel_index,
                limit=DEFAULT_HISTORY_LIMIT,
                channel_key=key,
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))
            return state
        state.entries.extend(stored_chat_entry(stored) for stored in page.messages)
        state.has_older_history = page.has_older
        state.open_scroll_pending = bool(page.messages)
        return state

    def _invalidate_reassigned_channel_caches(
        self, new_channels: tuple[ChannelInfo, ...]
    ) -> None:
        """Drop the cached state for any NON-CURRENT channel whose live

        identity changed since it was last loaded (CHAT channel-history
        isolation). Safe to simply discard entirely: unlike the
        CURRENTLY-displayed channel there is no already-mounted
        transcript here to keep in sync (see
        _reconcile_current_channel_identity, which handles that harder,
        remount-aware case for the current channel instead) -- the next
        access just reloads from scratch, exactly like a channel
        touched for the first time this run.
        """
        live_keys = {channel.index: channel.stable_key or None for channel in new_channels}
        for index in list(self._channel_states):
            if index == self.current_channel_index:
                continue
            state = self._channel_states[index]
            if not state.loaded or state.loaded_key is None:
                continue
            live_key = live_keys.get(index)
            if live_key is not None and live_key != state.loaded_key:
                del self._channel_states[index]

    def _initial_chat_widgets(
        self, channel_index: int, state: ChannelChatState
    ) -> list[Static | ChatEntryWidget]:
        """The widget list a freshly (re)mounted transcript starts with.

        Shared by _load_chat_history (startup), _switch_channel (user-
        driven switch), and _show_connection's post-reconnect identity
        reload -- one place decides between LOAD OLDER, END OF CHAT
        HISTORY, and the new empty-channel marker (CHAT channel-history
        isolation), so the three call sites can never drift apart.
        """
        widgets: list[Static | ChatEntryWidget] = []
        if state.has_older_history:
            widgets.append(LoadOlderControl())
        elif state.entries and self.chat_store is not None:
            widgets.append(EndOfChatHistoryMarker())
        elif self.chat_store is not None:
            widgets.append(
                StartOfChannelHistoryMarker(self._channel_label(channel_index))
            )
        widgets.extend(self._chat_entry_widget(entry) for entry in state.entries)
        return widgets

    async def _switch_channel(self, channel_index: int) -> None:
        # Selecting a channel always switches CHAT into CHANNEL mode
        # (CHAT/DM/MENTION UX item 4) -- even when the target index is
        # already the current one (the user may be sitting in DMS mode
        # and re-picking the already-selected channel purely to get
        # back to it), so this mode switch runs unconditionally, before
        # the early-return below.
        if self.current_tab == "chat":
            self._switch_chat_mode("channel")
        if channel_index == self.current_channel_index:
            return
        if self.current_tab == "chat" and self._chat_mode == "channel":
            self._mark_new_messages_read()
        self._capture_current_channel_state()
        state = self._restore_channel_state(channel_index)
        await self._mount_channel_transcript(channel_index, state)

    async def _mount_channel_transcript(
        self, channel_index: int, state: ChannelChatState
    ) -> None:
        """Rebuild the mounted transcript to match `state` for `channel_index`.

        The shared tail of a user-driven _switch_channel AND a
        background identity-driven refresh of the ALREADY-displayed
        channel (see _reconcile_current_channel_identity) -- both need
        the exact same "clear, remount from state" behavior.
        """
        chat_inputs = list(self.query("#chat-input"))
        if chat_inputs:
            chat_inputs[0].value = state.draft
            chat_inputs[0].cursor_position = len(state.draft)
        transcript = self.query_one("#chat-log", ChatTranscript)
        await transcript.remove_children()
        widgets = self._initial_chat_widgets(channel_index, state)
        if widgets:
            await transcript.mount(*widgets)
        if self.current_tab == "chat" and self._chat_mode == "channel":
            self._mark_unread_messages_viewed()
            self._recount_unread()
        selector = self.query_one(ChannelSelector)
        selector.value = channel_index
        selector.close_menu()
        self._update_tab_bar()
        self._update_transcript_indicator()
        self.call_after_refresh(self._jump_to_newest)

        self._render_chat_status()

    async def _reconcile_current_channel_identity(self) -> None:
        """Refresh the CURRENTLY-displayed channel if its live identity

        changed since it was loaded (CHAT channel-history isolation).
        Handles the one case _show_connection's own channel-sync block
        cannot: a same-INDEX radio reconfiguration (e.g. slot 0
        LongFast -> MediumSlow, whether discovered between app runs or
        mid-session) never trips "selected_index != current_channel_
        index", since the index itself never changes -- only what it
        now MEANS does.

        A no-op the overwhelming majority of the time (ordinary
        reconnect, nothing reconfigured): only issues a second query
        when the live channel_key actually differs from what is
        currently mounted, and only remounts the transcript when that
        query's result set is genuinely different content -- never a
        visible refresh on every ordinary reconnect.
        """
        channel_index = self.current_channel_index
        state = self._state_for(channel_index)
        key = self._channel_key_for(channel_index)
        if state.loaded_key == key:
            return
        if self.chat_store is None:
            state.loaded_key = key
            return
        try:
            page = self.chat_store.load_recent_page(
                channel_index=channel_index,
                limit=DEFAULT_HISTORY_LIMIT,
                channel_key=key,
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))
            return
        fresh_ids = tuple(stored.id for stored in page.messages)
        current_ids = tuple(entry.message_id for entry in state.entries)
        # loaded_key is recorded regardless of whether content changed --
        # this key has now been validated against the store either way,
        # so the next reconcile has nothing left to re-check until the
        # live identity changes again.
        state.loaded_key = key
        if fresh_ids == current_ids:
            return
        # A GENUINE difference: `state.entries`/self.chat_history are only
        # ever replaced with fresh objects in THIS branch, never on the
        # equal-content path above -- replacing them unconditionally would
        # desync already-mounted ChatEntryWidgets (which hold references
        # to the OLD entry objects) from self.chat_history's new objects,
        # breaking every `widget.entry is entry` identity comparison
        # elsewhere (e.g. _trim_mounted_chat_window) even though nothing
        # actually needed to change.
        state.entries = [stored_chat_entry(stored) for stored in page.messages]
        state.has_older_history = page.has_older
        state.open_scroll_pending = bool(page.messages)
        state.loaded = True
        self.chat_history = state.entries
        self._has_older_history = state.has_older_history
        await self._mount_channel_transcript(channel_index, state)

    def _append_chat_widget(self, entry: ChatEntry) -> None:
        """Compatibility wrapper for callers that already appended an entry."""
        self._insert_chat_widget(entry, self.chat_history.index(entry), older=False)

    def _insert_chat_widget(
        self,
        entry: ChatEntry,
        insert_index: int,
        *,
        older: bool,
    ) -> None:
        transcript = self.query_one("#chat-log", ChatTranscript)
        # The empty-channel marker (StartOfChannelHistoryMarker) is only
        # ever mounted when a channel has zero entries -- the first real
        # entry inserted anywhere in the transcript makes it stale by
        # definition, so it is removed unconditionally here rather than
        # left for some later refresh to notice.
        for marker in transcript.query(StartOfChannelHistoryMarker):
            marker.remove()
        if not self._has_older_history and self.chat_store is not None:
            self._ensure_end_history_marker(transcript)
        if self.current_tab != "chat" or self._chat_mode != "channel":
            following = self._following_chat_widget(insert_index)
            transcript.mount(self._chat_entry_widget(entry), before=following)
            self._trim_mounted_chat_window(transcript)
            self._chat_open_scroll_pending = True
            return
        should_follow = self._is_near_chat_bottom()
        if should_follow:
            transcript.anchor()
        old_scroll_y = transcript.scroll_y
        old_virtual_height = transcript.virtual_size.height
        following = self._following_chat_widget(insert_index)
        inserted_above_view = (
            older
            and following is not None
            and following.region.y <= transcript.region.y
        )
        inserted_below_view = (
            older
            and following is not None
            and following.region.y >= transcript.region.bottom
        )
        transcript.mount(self._chat_entry_widget(entry), before=following)
        self._trim_mounted_chat_window(transcript)
        if should_follow:
            self.call_after_refresh(self._jump_to_newest)
        elif inserted_above_view:
            self.call_after_refresh(
                self._restore_scroll_after_prepend,
                transcript,
                old_scroll_y,
                old_virtual_height,
            )
        elif (not older or inserted_below_view) and entry.is_new and not entry.outgoing:
            state = self._state_for(self.current_channel_index)
            state.new_below_ids.add(self._entry_review_identity(entry))
            self.transcript_new_count = len(state.new_below_ids)
            self._update_transcript_indicator()

    def _following_chat_widget(self, insert_index: int) -> ChatEntryWidget | None:
        if insert_index + 1 >= len(self.chat_history):
            return None
        following_entry = self.chat_history[insert_index + 1]
        return next(
            (
                widget
                for widget in self.query(ChatEntryWidget)
                if widget.entry is following_entry
            ),
            None,
        )

    def _trim_mounted_chat_window(self, transcript: ChatTranscript) -> None:
        """Bound the mounted window without hiding NEW/unread messages."""
        trimmed = False
        while len(self.chat_history) > self._mounted_chat_target:
            oldest = self.chat_history[0]
            removable_index = (
                0
                if oldest.message_id is not None
                and not oldest.is_new
                and not oldest.unread
                else None
            )
            if removable_index is None:
                break
            removed = self.chat_history.pop(removable_index)
            widget = next(
                (
                    candidate
                    for candidate in self.query(ChatEntryWidget)
                    if candidate.entry is removed
                ),
                None,
            )
            if widget is not None:
                widget.remove()
            trimmed = True

        if trimmed:
            self._has_older_history = True
            self._ensure_load_older_control(transcript)
        self._capture_current_channel_state()

    def _ensure_load_older_control(self, transcript: ChatTranscript) -> None:
        for marker in transcript.query(EndOfChatHistoryMarker):
            marker.remove()
        if len(transcript.query(LoadOlderControl)):
            return
        first_widget = next(iter(self.query(ChatEntryWidget)), None)
        if first_widget is None:
            transcript.mount(LoadOlderControl())
        else:
            transcript.mount(LoadOlderControl(), before=first_widget)

    def _ensure_end_history_marker(self, transcript: ChatTranscript) -> None:
        """Show the passive boundary only when at least one entry exists."""
        if (
            self.chat_store is None
            or not self.chat_history
            or self._has_older_history
        ):
            return
        if len(transcript.query(EndOfChatHistoryMarker)):
            return
        first_widget = next(iter(transcript.query(ChatEntryWidget)), None)
        if first_widget is None:
            transcript.mount(EndOfChatHistoryMarker())
        else:
            transcript.mount(EndOfChatHistoryMarker(), before=first_widget)

    def _refresh_chat_timestamps(
        self,
        now: float | None = None,
        wall_now: float | None = None,
    ) -> None:
        current_time = monotonic() if now is None else now
        for widget in self.query(ChatEntryWidget):
            widget.refresh_timestamp(current_time)
        self._refresh_mesh(wall_now)

    def _mesh_last_message_activity(self) -> dict[str, float]:
        """Most recent trustworthy incoming-message timestamp per node.

        Distinct from RadioService's `lastHeard` (passive node-database
        sync): this is specifically CHAT receive activity, the source of
        truth for MESH's CLIENT role and last-interaction time.

        Persisted CHAT history is authoritative, not whatever bounded page
        happens to be mounted in memory: a node's last message may be far
        older than the currently loaded window, or from before the app was
        last restarted, and must still count. In-memory channel state is
        merged on top of the persisted baseline -- both sources only ever
        contribute a per-node maximum, never a sum, so merging cannot
        double-count -- because a just-received message reaches this method
        (via _refresh_mesh) before its own persistence write completes; see
        _accept_received_message, which refreshes MESH before persisting.
        """
        activity: dict[str, float] = {}
        if self.chat_store is not None:
            try:
                activity.update(self.chat_store.latest_incoming_message_at())
            except ChatStoreError:
                pass
        for state in self._channel_states.values():
            for entry in state.entries:
                if entry.outgoing or not entry.node_id:
                    continue
                key = entry.node_id.strip().lower()
                if not key:
                    continue
                # Matches StoredMessage.message_time's incoming precedence:
                # no receipt-time fallback, so in-memory and persisted
                # activity resolve by the same truthful message clock.
                timestamp = entry.origin_sent_at or entry.radio_rx_at
                if timestamp is None:
                    continue
                if key not in activity or timestamp > activity[key]:
                    activity[key] = timestamp
        return activity

    def _mesh_working_set(self, wall_now: float | None = None) -> tuple[MeshNodeState, ...]:
        """Build MESH's displayed real-node set without touching the board.

        The single place that turns known nodes + CHAT activity into
        MESH's displayed working set. NodeDB-first: `RadioService.
        get_known_nodes()` (the radio's own passively-learned node
        database) is the primary admission source -- a node the radio
        already knows about from passive mesh traffic is a candidate on
        its own, no CHAT message required. `_mesh_last_message_activity()`
        enriches a candidate already admitted (marking it CLIENT) rather
        than gating admission; see build_mesh_working_set.

        Callers that need the count and the rendered board to describe
        the exact same moment (see _refresh_mesh) must compute this
        ONCE per cycle and pass the SAME `wall_now` -- and ideally the
        same resulting tuple -- to every consumer that cycle, rather
        than calling this again and risking a second, independent read
        of live, thread-mutable radio state.
        """
        if self._radio_state is not RadioState.ONLINE:
            return ()
        getter = getattr(self.radio, "get_known_nodes", None)
        try:
            nodes = tuple(getter()) if callable(getter) else ()
        except Exception:
            nodes = ()
        if not all(isinstance(node, NodeMetadata) for node in nodes):
            nodes = ()
        current_time = time() if wall_now is None else wall_now
        working_set = build_mesh_working_set(
            nodes,
            now=current_time,
            last_message_at=self._mesh_last_message_activity(),
            favorite_ids=self.settings.favorite_node_ids,
        )
        # YOU's long_name/short_name from get_known_nodes() are NOT
        # reliable (that NodeDB record is frequently a bare synthesized
        # placeholder -- see RadioService.get_known_nodes' own trailing
        # "local node never otherwise seen" fallback). self._radio_info
        # is the SAME authoritative source CONNECTION's own identity
        # controls already use, and is unconditionally cleared to None
        # on every non-ONLINE transition (see _show_connection), so
        # this can never leak a previous radio's stale identity across
        # a reconnect/device switch. Corrects only the display fields;
        # node_id/is_local/position are left exactly as the working set
        # already computed them.
        if working_set and working_set[0].node.is_local and self._radio_info is not None:
            corrected_node = replace(
                working_set[0].node,
                long_name=self._radio_info.long_name,
                short_name=self._radio_info.short_name,
            )
            working_set = (
                replace(working_set[0], node=corrected_node),
                *working_set[1:],
            )
        return working_set

    def _mesh_active_count(
        self,
        wall_now: float | None = None,
        working_set: tuple[MeshNodeState, ...] | None = None,
    ) -> int:
        """Authoritative count of active REAL remote nodes MESH displays.

        Describes the real remote nodes CURRENTLY DISPLAYED on MESH --
        the same bounded working set the board renders, not the full
        known-node population (which may include nodes MESH never shows
        at all). Uses the EXACT SAME predicate (is_node_active) that
        _mesh_node_color() uses for each node's own BASE/DIM_BASE
        styling, so the [3] MESH (N) tab label and the board can never
        disagree about which/how many displayed nodes are active.
        Anonymous relay stages are never part of the working set and so
        never affect this count either way (see mesh_topology.
        RelayStage); YOU is excluded via is_local.

        `working_set`, when supplied by a caller that already computed
        one this cycle (see _refresh_mesh), is used as-is instead of
        triggering a second independent _mesh_working_set() read of live
        radio state -- this is what keeps the count and the rendered
        board from ever describing two different snapshots. Safe to
        call regardless of the current tab -- unlike _refresh_mesh(),
        this never touches the topology board/widget, only computes a
        number, so refreshing it periodically (see
        _refresh_chat_timestamps) to catch a node aging out while MESH
        isn't even the visible tab can never "reshuffle" anything.
        """
        current_time = time() if wall_now is None else wall_now
        if working_set is None:
            working_set = self._mesh_working_set(current_time)
        return sum(
            1
            for state in working_set
            if not state.node.is_local and is_node_active(state.node.last_heard, current_time)
        )

    def _refresh_mesh(self, wall_now: float | None = None) -> None:
        """Refresh passive topology data without causing Meshtastic traffic."""
        current_time = time() if wall_now is None else wall_now
        # Computed exactly ONCE per cycle and threaded into every
        # consumer below -- the count ([3] MESH (N), via _update_tab_bar)
        # and the rendered board must describe the SAME snapshot of live
        # NodeDB state, never two independent reads of it (that mismatch
        # -- count updating live while the board stayed visually stale
        # -- was the exact bug this closes).
        working_set = self._mesh_working_set(current_time)
        # [3] MESH (N) must stay live wherever _refresh_mesh() is called
        # from, regardless of whether MESH is the tab currently showing
        # (see _mesh_active_count -- it never touches the board/widget,
        # so this can never "reshuffle" the topology).
        self._update_tab_bar(current_time, mesh_working_set=working_set)
        self._update_mesh_node_bar(working_set, current_time)
        views = list(self.query(MeshTopologyView))
        statuses = list(self.query("#mesh-status"))
        if not views or not statuses:
            return
        if self.current_tab != "mesh":
            return
        if self._radio_state is not RadioState.ONLINE:
            # Stale topology intentionally stays visible while
            # connecting/reconnecting -- the shared top-of-view
            # connection status (see _update_chat_connection_state/
            # _connection_status_rich_text) already communicates "not live
            # right now"; clearing or rebuilding the board here would be
            # a needless reshuffle of useful stale data over a
            # connection-status change alone. Never MESH-specific
            # wording like the old "RADIO DISCONNECTED" either -- that
            # was exactly the kind of independent reinterpretation this
            # is meant to eliminate.
            return
        view = views[0]
        status = statuses[0]
        # TRACE ROUTE (Part C): a pending trace's animated "TRACING
        # ROUTE TO SHN > > >" status, or a just-finished TRACE
        # SUCCEEDED/TRACE FAILED banner, takes over this SAME top-left
        # widget -- never a second one -- so it must win over whatever
        # this method would otherwise show here, in EITHER branch below.
        override = self._mesh_status_override_text()
        if not working_set:
            view.clear_nodes()
            status.update("NO MESH DATA" if override is None else override)
            self._update_mesh_node_bar(working_set, current_time)
            return
        status.update("" if override is None else override)
        # A real CLIENT with a truthful nonzero hop count is placed at
        # least hops_away+1 grid steps out -- never closer -- so its own
        # anonymous relay-stage placeholders have genuinely interior
        # cells to occupy along the YOU-to-client line (see RelayStage/
        # build_relay_stages); geography alone would otherwise often
        # place it immediately adjacent to YOU, leaving no room at all.
        min_radius_by_id = {
            node_id: hops + 1 for node_id, hops in _mesh_hop_counts(working_set).items()
        }
        # A total remote-population turnover (radio swap, or a NodeDB
        # reset -- not one remote node's ID mismatch, but the entire
        # current remote set sharing nothing with what was previously
        # remembered) is exactly the "substantial logical topology
        # change" stickiness is not meant to apply across: without this
        # reset, unrelated leftover positions/extents from a completely
        # different node population would otherwise still constrain
        # this one's fresh placement. Never triggers on ordinary churn
        # (one node joining/leaving among others that persist), only a
        # clean break -- see MeshLayoutStabilityAppTests.
        current_remote_ids = {
            state.node.node_id.strip().lower()
            for state in working_set
            if not state.node.is_local
        }
        local_id = next(
            (state.node.node_id.strip().lower() for state in working_set if state.node.is_local),
            "",
        )
        previous_remote_ids = set(self._mesh_sticky_positions) - {local_id}
        if previous_remote_ids and current_remote_ids and not (
            current_remote_ids & previous_remote_ids
        ):
            self._mesh_sticky_positions = {}
            self._mesh_extent_ratchet = {"up": 0, "down": 0, "left": 0, "right": 0}
        slots = assign_grid_slots(
            tuple(state.node for state in working_set),
            max_radius=DEFAULT_MAX_GRID_RADIUS,
            min_radius_by_id=min_radius_by_id,
            sticky_positions=self._mesh_sticky_positions,
        )
        # MESH LAYOUT STABILITY: remember this cycle's own positions,
        # pruned to only currently-present nodes (a departed node's
        # vacated cell simply becomes free for whoever's placed next --
        # it never itself displaces anyone), so the NEXT call's sticky
        # lookup reflects reality, not indefinitely-accumulating history.
        self._mesh_sticky_positions = {
            item.node.node_id.strip().lower(): (item.x, item.y, item.region)
            for item in slots
        }
        # Ratchet (monotonic max, never shrinks) each axis's own
        # farthest-logical-step extent -- see place_within_bounds'
        # min_extent docstring: without this, a node aging out (making
        # the axis's current farthest node closer) would immediately
        # re-compact every other node sharing that axis, and a new
        # farther node appearing would stretch (and so move) them
        # outward -- both routine events, neither genuine topology
        # information about any node OTHER than the one that
        # appeared/departed.
        self._mesh_extent_ratchet = {
            "up": max(
                self._mesh_extent_ratchet["up"],
                max((-item.y for item in slots if item.y < 0), default=0),
            ),
            "down": max(
                self._mesh_extent_ratchet["down"],
                max((item.y for item in slots if item.y > 0), default=0),
            ),
            "left": max(
                self._mesh_extent_ratchet["left"],
                max((-item.x for item in slots if item.x < 0), default=0),
            ),
            "right": max(
                self._mesh_extent_ratchet["right"],
                max((item.x for item in slots if item.x > 0), default=0),
            ),
        }
        # Placed in the STABLE logical coordinate space, entirely
        # independent of the current viewport's own row/column count
        # (see MESH_LOGICAL_GRID_ROWS) -- MeshTopologyView.set_nodes is
        # the only place that later translates/clips this same layout
        # into whatever is currently visible.
        base_positions = place_within_bounds(
            slots,
            center_row=MESH_LOGICAL_GRID_CENTER_ROW,
            center_column=MESH_LOGICAL_GRID_CENTER_COLUMN,
            row_count=MESH_LOGICAL_GRID_ROWS,
            column_count=MESH_LOGICAL_GRID_COLUMNS,
            min_extent=self._mesh_extent_ratchet,
        )
        view.set_nodes(working_set, base_positions, theme=self._current_theme, now=current_time)
        # Called again here (the earlier call above only ever sees LAST
        # cycle's selected_node_id, since set_nodes -- which can fix up
        # selection, e.g. when the previously selected node just
        # disappeared -- has not run yet at that point this cycle): this
        # second call is what makes the bar's content reflect THIS
        # cycle's actual selection, never a one-refresh-stale one.
        self._update_mesh_node_bar(working_set, current_time)

    def _move_mesh_focus(self, direction: str) -> None:
        view = self.query_one(MeshTopologyView)
        # Anonymous relay-stage placeholders are visual topology only --
        # excluded from the navigation candidate set entirely, so an
        # arrow press always lands on the nearest sensible REAL node
        # (YOU, or a real CLIENT/CLIENT+RELAY/RELAY), skipping over any
        # relay stage that happens to sit geometrically between them.
        relay_ids = {stage.node_id for stage in view.relay_stages}
        navigable_positions = {
            node_id: position
            for node_id, position in view.base_positions.items()
            if node_id not in relay_ids
        }
        target_id = _mesh_directional_target(
            navigable_positions, view.selected_node_id, direction
        )
        if target_id is not None:
            view.select_node(target_id)
            view.set_nodes(
                view.working_set,
                view.base_positions,
                theme=self._current_theme,
                now=time(),
            )
            # The unified bar is selected-node-specific -- it must switch
            # to the newly selected node's own data immediately, not wait
            # for the next periodic _refresh_mesh() tick (up to ~1s later).
            self._update_mesh_node_bar(view.working_set, time())

    def _open_mesh_node_menu(self) -> None:
        """ENTER on the currently focused MESH node opens the shared

        CHAT/MESH node-options menu (see _open_node_menu) against that
        SAME node -- never a fresh NodeDB lookup, so YOU's identity
        fields stay whatever _mesh_working_set already corrected them
        to. A no-op when there is nothing real to inspect: an empty
        working set (view.selected_node_id == ""), or -- defensively,
        though _move_mesh_focus's own relay_ids filtering means this
        should not normally arise -- a selected_node_id that no longer
        resolves to a working-set member or a mounted glyph widget
        (anonymous relay stages are never selectable at all; see
        MeshRelayWidget). allow_reply=False: MESH never gains
        REPLY/@mention-into-CHAT-composer -- but DM (open/create that
        node's own conversation, item 16) IS offered here, since
        _open_node_menu's allow_dm defaults to True and MESH does not
        override it. allow_traceroute=True: TRACE ROUTE (Part C) is
        offered ONLY from this MESH menu, never CHAT's own node-details
        popup (open_user_menu's call site does not pass it, so it keeps
        its own default of False).
        """
        view = self.query_one(MeshTopologyView)
        node_id = view.selected_node_id
        if not node_id:
            return
        state = next(
            (
                candidate
                for candidate in view.working_set
                if candidate.node.node_id == node_id
            ),
            None,
        )
        if state is None:
            return
        origin = next(
            (widget for widget in view.query(MeshNodeWidget) if widget.node_id == node_id),
            None,
        )
        if origin is None:
            return
        self._open_node_menu(
            state.node, origin, None, allow_reply=False, allow_traceroute=True
        )

    def _mesh_distance_is_metric(self) -> bool:
        """Whether MESH's own geographic distance display should read as

        km (METRIC) rather than mi (IMPERIAL) -- read directly off the
        connected radio's own already-synced display.units config field
        (read_synced_config_field is the SAME zero-RF cached read the
        UNITS dropdown's own display already uses -- no new config
        traffic, and MeshtasticPass never maintains a second, app-local
        units preference of its own -- see MESH VIEW PASS item 11).
        Unsupported schema/not connected/anything other than the exact
        METRIC enum value all fall back to IMPERIAL, matching this
        app's existing miles-only behavior before this pass.
        """
        return (
            self.radio.read_synced_config_field("display", "units") == DISPLAY_UNITS_METRIC
        )

    def _update_mesh_node_bar(
        self, working_set: tuple[MeshNodeState, ...], now: float
    ) -> None:
        """#mesh-node-bar: ONE physical line describing the currently

        selected MESH node (MESH GPS + UNIFIED BAR Part B) -- LONG NAME -
        SHORT NAME - HOPS - GPS - DISTANCE - LINK - TIME, replacing the
        previous separate bottom-left context line and bottom-right
        LINK/LAST UPDATE line entirely (see mesh_state.
        format_mesh_node_bar_fields/format_mesh_node_bar_line).

        Also force-hides #mesh-connection-status (the OLD top-of-view
        location) every ONLINE cycle, regardless of the current tab --
        that widget is exclusively owned by _update_chat_connection_state()
        while NOT ONLINE (see that method's own docstring), so once
        ONLINE, nothing else would ever clear its stale text/display=True
        from the last disconnected state. This mirrors the old
        _update_mesh_status_line's identical unconditional-while-ONLINE
        behavior.

        The bar's own content, unlike that force-hide, IS gated on both
        being on the MESH tab and being ONLINE -- matching the old
        _update_mesh_context_status's precedent (blank rather than freeze
        stale content while off-tab/disconnected), since this bar is now
        entirely selected-node-identity-driven with no separate "board
        freshness, independent of any one node" concept left to preserve
        across a disconnect.

        LINK is computed here (RadioService.get_link_quality -- zero RF
        traffic) and passed into format_mesh_node_bar_fields; never
        queried for YOU or when nothing is selected (a radio has no RF
        link to itself).

        Because this is now the ONLY widget in its row (no sibling to
        share width with any more), `widget.size.width` is never
        circularly dependent on another widget's content -- so, unlike
        the old two-widget split, this can run synchronously with no
        call_after_refresh layout-ordering workaround needed.
        """
        if self._radio_state is RadioState.ONLINE:
            connection_widgets = list(self.query("#mesh-connection-status"))
            if connection_widgets:
                connection_widgets[0].display = False
        widgets = list(self.query("#mesh-node-bar"))
        if not widgets:
            return
        widget = widgets[0]
        if self.current_tab != "mesh" or self._radio_state is not RadioState.ONLINE:
            widget.update("")
            return
        views = list(self.query(MeshTopologyView))
        if not views:
            widget.update("")
            return
        view = views[0]
        # An anonymous relay-stage placeholder can never be selected (see
        # MeshRelayWidget/_move_mesh_focus), so `state` is None here only
        # for a genuinely stale/invalid ID, never a relay stage.
        state = next(
            (
                candidate
                for candidate in working_set
                if candidate.node.node_id == view.selected_node_id
            ),
            None,
        )
        if state is None:
            widget.update("")
            return
        link = (
            self.radio.get_link_quality(state.node.node_id)
            if not state.node.is_local
            else None
        )
        fields = format_mesh_node_bar_fields(
            state, now=now, metric=self._mesh_distance_is_metric(), link=link
        )
        text = format_mesh_node_bar_line(fields, available_width=widget.size.width)
        palette = THEME_PALETTES[self._current_theme]
        widget.update(Text(text, style=palette.accent2 if fields.accent2 else palette.base))

    def _mesh_status_override_text(self) -> Text | None:
        """TRACE ROUTE (Part C): whatever must override the ordinary

        #mesh-status content (NO MESH DATA / blank) this cycle, or None
        if nothing does. A just-expired banner is cleared here (lazily,
        on the next read) rather than needing its own extra timer beyond
        the one that already schedules _dismiss_traceroute_banner --
        this only matters for a caller that reads this directly without
        going through that timer (none currently do, but it keeps this
        function correct standalone).
        """
        if self._traceroute_banner is not None:
            palette = THEME_PALETTES[self._current_theme]
            color = (
                palette.accent
                if self._traceroute_banner.style_kind == "accent"
                else palette.error
            )
            return Text(self._traceroute_banner.text, style=Style(color=color))
        if self._active_traceroute is not None:
            return self._traceroute_status_text()
        return None

    def _traceroute_status_text(self) -> Text:
        """"TRACING ROUTE TO SHN  > > >", one active ACCENT arrow cycling

        across TRACEROUTE_ARROW_POSITIONS positions -- reusing the exact
        CHAT SENDING visual language (active=ACCENT, inactive=dim_quarter,
        advanced by the SAME 0.45s _delivery_timer tick; see
        _advance_delivery_states/SENDING_ARROW_FRAMES).
        """
        active = self._active_traceroute
        assert active is not None
        palette = THEME_PALETTES[self._current_theme]
        text = Text(f"TRACING ROUTE TO {active.destination_short_name}  ", style=palette.dim)
        for index in range(TRACEROUTE_ARROW_POSITIONS):
            if index:
                text.append(" ")
            color = (
                palette.accent
                if index == self._traceroute_animation_frame
                else palette.dim_quarter
            )
            text.append(TRACEROUTE_ARROW_GLYPH, style=Style(color=color))
        return text

    def _render_mesh_top_status(self) -> None:
        """Force an immediate #mesh-status repaint for TRACE ROUTE state

        changes that must appear right away -- never waiting for the
        next periodic _refresh_mesh() tick (up to ~1s later). Safe to
        call regardless of the current tab (the widget simply repaints
        invisibly off-tab, exactly like [3] MESH (N) already does) and
        regardless of ONLINE state (harmless to update a currently-
        hidden widget; _refresh_mesh's own ONLINE gate is what actually
        controls this widget's meaningful visibility).
        """
        widgets = list(self.query("#mesh-status"))
        if not widgets:
            return
        override = self._mesh_status_override_text()
        if override is not None:
            widgets[0].update(override)

    def _start_traceroute(self, node: NodeMetadata) -> None:
        """TRACE ROUTE menu action (MESH's selected REMOTE node ENTER

        menu only -- never offered/attempted for YOU). Explicit,
        user-triggered RF traffic ONLY -- never called from MESH open,
        selection, refresh, a timer, or a topology update.

        V1 allows exactly one active trace at a time: a repeated
        attempt while one is already pending is a deterministic no-op
        (silently ignored), never queued or stacked.
        """
        if node.is_local or self._radio_state is not RadioState.ONLINE:
            return
        if self._active_traceroute is not None:
            return
        if self._traceroute_banner_timer is not None:
            self._traceroute_banner_timer.stop()
            self._traceroute_banner_timer = None
        self._traceroute_banner = None
        self._traceroute_request_seq += 1
        request_token = self._traceroute_request_seq
        destination_node_id = node.node_id
        self._active_traceroute = ActiveTraceroute(
            request_token=request_token,
            destination_node_id=destination_node_id,
            destination_short_name=_reply_mention_name(node),
        )
        self._traceroute_animation_frame = 0
        self._render_mesh_top_status()
        self._traceroute_timeout_timer = self.set_timer(
            TRACEROUTE_TIMEOUT_SECONDS,
            lambda: self._traceroute_timed_out(request_token),
        )

        def status_handler(status: TracerouteStatus) -> None:
            self.post_message(TracerouteStatusReceived(request_token, status))

        def send_from_thread() -> None:
            try:
                self.radio.send_traceroute(destination_node_id, status_handler)
            except RadioSendError as error:
                self.post_message(TracerouteRequestFailed(request_token, str(error)))

        self.run_worker(send_from_thread, thread=True)

    @on(TracerouteStatusReceived)
    def traceroute_status_received(self, event: TracerouteStatusReceived) -> None:
        active = self._active_traceroute
        if active is None or active.request_token != event.request_token:
            # Stale/late (this attempt already timed out, was superseded,
            # or -- impossible in v1's one-at-a-time model, but checked
            # anyway -- belongs to a different attempt entirely): ignored
            # deterministically, never misattributed to whatever IS
            # currently active/selected.
            return
        status = event.status
        if status.state is TracerouteState.SUCCEEDED and status.result is not None:
            self._finish_traceroute_success(status.result)
        else:
            self._finish_traceroute_failure(status.detail or "Traceroute failed.")

    @on(TracerouteRequestFailed)
    def traceroute_request_failed(self, event: TracerouteRequestFailed) -> None:
        active = self._active_traceroute
        if active is None or active.request_token != event.request_token:
            return
        self._finish_traceroute_failure(event.detail)

    def _traceroute_timed_out(self, request_token: int) -> None:
        active = self._active_traceroute
        if active is None or active.request_token != request_token:
            # Already resolved (success/failure/a disconnect) by the
            # time this fires -- never overwrite a real outcome with a
            # stale timeout (see _clear_active_traceroute, which always
            # stops this SAME timer the instant a real outcome lands).
            return
        self._finish_traceroute_failure("Traceroute timed out.")

    def _clear_active_traceroute(self) -> None:
        if self._traceroute_timeout_timer is not None:
            self._traceroute_timeout_timer.stop()
            self._traceroute_timeout_timer = None
        self._active_traceroute = None

    def _finish_traceroute_success(self, result: TracerouteResult) -> None:
        """Real protocol evidence only -- never "request sent" alone.

        Stores the result keyed by canonical destination (replacing,
        never duplicating, an earlier result for the SAME destination),
        marks that node's board label with the ACCENT "*" (session
        evidence only -- never a claim about currently-rendered
        connectors; see mesh_topology.mesh_board_marker_label), and
        never steals focus.
        """
        destination = result.destination_node_id
        self._clear_active_traceroute()
        self._traceroute_results[destination] = result
        self.query_one(MeshTopologyView).mark_traced(destination)
        self._show_traceroute_banner("TRACE SUCCEEDED", "accent")
        # Repaints the board (for the new "*" marker) AND the top status
        # (for the banner) in one pass -- see _mesh_status_override_text.
        self._refresh_mesh()

    def _finish_traceroute_failure(self, _detail: str) -> None:
        """A failed/timed-out trace never creates a "*" marker and never

        erases an EARLIER successful one for the same (or any other)
        destination -- this never touches _traceroute_results/
        mark_traced at all.
        """
        self._clear_active_traceroute()
        self._show_traceroute_banner("TRACE FAILED", "error")
        self._render_mesh_top_status()

    def _show_traceroute_banner(self, text: str, style_kind: str) -> None:
        self._traceroute_banner = TracerouteBanner(text, style_kind)
        self._traceroute_banner_timer = self.set_timer(
            TRACEROUTE_BANNER_SECONDS, self._dismiss_traceroute_banner
        )

    def _dismiss_traceroute_banner(self) -> None:
        self._traceroute_banner = None
        self._traceroute_banner_timer = None
        self._render_mesh_top_status()

    def _advance_delivery_states(self) -> None:
        self._send_animation_frame = self._send_animation_frame % len(
            SENDING_ARROW_FRAMES
        ) + 1
        now = monotonic()
        # Every channel's AND every DM conversation's entries, not just
        # the currently-viewed one -- a send left in flight on a channel
        # or DM the user has since switched away from must still
        # correctly resolve to UNCONFIRMED on its own timeline, exactly
        # as if it were still visible. Only genuine SENDING is eligible
        # for the timeout at all (delivery-state monotonicity: SENT/
        # HEARD/FAILED already retired their own deadline to None the
        # instant they were reached -- see _set_delivery_state -- so
        # this check is itself the second, redundant confirmation of
        # that invariant, never the ONLY thing enforcing it).
        for state in (*self._channel_states.values(), *self._dm_states.values()):
            for entry in state.entries:
                if (
                    entry.delivery_state is DeliveryState.SENDING
                    and entry.confirmation_deadline is not None
                    and now >= entry.confirmation_deadline
                ):
                    self._set_delivery_state(entry, DeliveryState.UNCONFIRMED)
        for widget in self.query(ChatEntryWidget):
            widget.refresh_delivery_state(self._send_animation_frame)
        # TRACE ROUTE (Part C): reuses this SAME pre-existing 0.45s tick
        # for its own one-active-arrow-of-three animation -- never a
        # second timer (see TRACEROUTE_ARROW_POSITIONS). A no-op
        # (neither branch below runs) whenever no trace is pending, so
        # this adds no work at all outside an active trace.
        if self._active_traceroute is not None:
            self._traceroute_animation_frame = (
                self._traceroute_animation_frame + 1
            ) % TRACEROUTE_ARROW_POSITIONS
            if self._radio_state is RadioState.ONLINE:
                self._render_mesh_top_status()

    def _is_near_chat_bottom(self) -> bool:
        transcript = self.query_one("#chat-log", ChatTranscript)
        return transcript.max_scroll_y - transcript.scroll_y <= 1

    def _jump_to_newest(self) -> None:
        transcript = self.query_one("#chat-log", ChatTranscript)
        transcript.anchor()
        transcript.scroll_end(animate=False)
        self._state_for(self.current_channel_index).new_below_ids.clear()
        self.transcript_new_count = 0
        self._update_transcript_indicator()

    def _return_to_present_and_type(self) -> None:
        """Jump to the chronological edge and resume the preserved draft."""
        self._jump_to_newest()
        self._focus_chat_composer()

    def _chat_entry_widget(self, entry: ChatEntry) -> ChatEntryWidget:
        return ChatEntryWidget(
            entry,
            favorite=self.settings.is_favorite(entry.node_id),
            mention=(
                entry.dm_node_id is None
                and not entry.outgoing
                and message_mentions_short_name(entry.text, self.chat_local_short_name)
            ),
        )

    @property
    def chat_local_short_name(self) -> str | None:
        """The CONNECTED radio's own current SHORTNAME, zero-RF (item

        27/33) -- self._radio_info is the exact same radio-swap-safe
        cached identity source already used elsewhere (see YOU/MESH):
        it is set fresh from event.info on every ONLINE transition and
        cleared to None on every non-ONLINE state (see
        _radio_event_from_thread), so a reconnect with a DIFFERENT
        radio can never leak the PREVIOUS radio's SHORTNAME here, and
        "not genuinely connected" naturally returns None -- callers
        must then disable mention highlighting entirely rather than
        guess (see message_mentions_short_name).
        """
        if self._radio_info is None:
            return None
        short_name = (self._radio_info.short_name or "").strip()
        return short_name or None

    def _refresh_chat_mentions(self) -> None:
        """Recompute @mention highlighting for every currently-mounted

        CHAT entry against whatever local SHORTNAME is now current
        (item 28) -- called whenever the connected radio's own
        identity changes, so a swap (e.g. OLD1 -> NEW1) updates
        already-visible messages immediately rather than waiting for
        the user to leave and re-enter the channel.
        """
        short_name = self.chat_local_short_name
        for widget in self.query(ChatEntryWidget):
            entry = widget.entry
            widget.set_mention(
                entry.dm_node_id is None
                and not entry.outgoing
                and message_mentions_short_name(entry.text, short_name)
            )

    def _chat_navigation_targets(self) -> list[Static | ChatEntryWidget]:
        """Vertical CHAT stops: every message is exactly ONE stop.

        An actionable (FAILED/UNCONFIRMED) message's stop is its
        RESEND control (⟲) -- the message's own ChatEntryWidget is
        excluded so the message doesn't ALSO appear as a separate
        stop, and DEL is excluded unconditionally (see item 5: "only
        ⟲ participates in normal vertical message traversal"). An
        ordinary message keeps its ChatEntryWidget as the stop, same
        as always.
        """
        targets: list[Static | ChatEntryWidget] = []
        transcript = self.query_one("#chat-log", ChatTranscript)
        for widget in transcript.walk_children():
            if isinstance(widget, MessageActionControl):
                if widget.display and widget.action == "resend":
                    targets.append(widget)
                continue
            if (
                isinstance(widget, (LoadOlderControl, ChatEntryWidget))
                and widget.display
                and not (
                    isinstance(widget, ChatEntryWidget)
                    and can_manual_resend(widget.entry)
                )
            ):
                targets.append(widget)
        return targets

    def _move_chat_focus(self, direction: int) -> None:
        """DOWN (direction > 0) moves toward the composer boundary --

        reached once forward navigation runs out of messages (existing
        behavior, unchanged), or immediately when there are no messages
        to navigate at all (`if not targets`, also unchanged) -- e.g. a
        freshly opened, empty CHAT, satisfying "DOWN begins composing"
        (see show_tab's CHAT branch) without disturbing the ordinary
        walk-through-messages-then-reach-composer flow once focus is
        already on a specific message.

        The NEUTRAL state -- focus is on the transcript itself or
        anything else not among `targets`, e.g. right after opening
        CHAT or pressing Escape (see show_tab/on_key) -- is treated as
        "positioned at the latest message" for BOTH directions, not
        merely for UP: DOWN goes straight to the composer, the same as
        if one more message existed past the newest and DOWN had
        stepped past it, with no need to first walk forward through
        every older message. UP still lands on the newest visible
        message itself, exactly as it always has.
        """
        targets = self._chat_navigation_targets()
        if not targets:
            if direction > 0:
                self._focus_chat_composer()
            return
        try:
            index = targets.index(self.focused)
            next_index = index + direction
            if direction > 0 and next_index >= len(targets):
                self._focus_chat_composer()
                return
            target = targets[max(0, min(len(targets) - 1, next_index))]
        except ValueError:
            if direction > 0:
                self._focus_chat_composer()
                return
            target = targets[-1]
        target.focus()
        target.scroll_visible(animate=False)
        self.call_after_refresh(self._clear_indicator_if_at_bottom)

    def _oldest_new_entry(self) -> ChatEntry | None:
        """Resolve the current channel's oldest actual NEW incoming entry."""
        state = self._state_for(self.current_channel_index)
        mounted_by_id = {
            entry.message_id: entry
            for entry in state.entries
            if entry.message_id is not None
        }
        candidates = [
            entry
            for entry in state.entries
            if entry.is_new and not entry.outgoing
        ]
        unmounted_ids = state.new_message_ids.difference(mounted_by_id)
        if state.new_message_ids and self.chat_store is not None:
            try:
                stored = self.chat_store.load_oldest_incoming_by_ids(
                    state.new_message_ids,
                    channel_index=self.current_channel_index,
                )
            except ChatStoreError:
                # A mounted candidate is only truthful if no older NEW identity
                # is known to exist outside the bounded window.
                if unmounted_ids:
                    return None
            else:
                if stored is not None:
                    candidate = mounted_by_id.get(stored.id)
                    if candidate is None:
                        candidate = stored_chat_entry(stored)
                        candidate.is_new = True
                        candidate.unread = stored.id in state.unread_message_ids
                    candidates.append(candidate)
        if not candidates:
            return None
        return min(candidates, key=lambda entry: entry.order_key)

    def _focus_oldest_new_message(self) -> None:
        """Select the oldest NEW incoming message in the current channel."""
        entry = self._oldest_new_entry()
        if entry is None:
            return
        identity = self._entry_review_identity(entry)
        widget = next(
            (
                candidate
                for candidate in self.query(ChatEntryWidget)
                if self._entry_review_identity(candidate.entry) == identity
            ),
            None,
        )
        if widget is not None:
            self._focus_chat_widget(widget)
            return
        if entry.message_id is None or self.chat_store is None:
            return
        self.run_worker(
            self._page_to_new_message(entry.message_id, self.current_channel_index),
            name="chat-unread-navigation",
            group="chat-unread-navigation",
            exclusive=True,
        )

    async def _page_to_new_message(
        self,
        message_id: int,
        channel_index: int,
    ) -> None:
        """Load bounded 50-row pages only until one known NEW row is mounted."""
        while (
            self.current_tab == "chat"
            and self.current_channel_index == channel_index
            and self._has_older_history
        ):
            before = len(self.chat_history)
            await self.load_older_chat_history(LoadOlderControl.Activated())
            widget = next(
                (
                    candidate
                    for candidate in self.query(ChatEntryWidget)
                    if candidate.entry.message_id == message_id
                ),
                None,
            )
            if widget is not None:
                self._focus_chat_widget(widget)
                return
            if len(self.chat_history) == before:
                return

    def _focus_chat_widget(self, widget: ChatEntryWidget) -> None:
        """Focus one entry and bring it fully into the visible transcript."""
        widget.focus()
        widget.scroll_visible(animate=False)
        # A preceding bounded prepend may have queued scroll restoration; run
        # this once more afterward so the selected target is not edge-clipped.
        self.call_after_refresh(widget.scroll_visible, animate=False)

    def _focus_chat_composer(self) -> None:
        """Return to typing without changing the current draft or read state.

        A no-op while the composer is disabled (radio not ONLINE -- see
        _update_chat_connection_state) -- never focuses a control the
        user cannot actually type into, regardless of which caller
        (DOWN-navigation, a printable keypress, RIGHT/jump-to-present)
        asked for it.
        """
        chat_input = self.query_one("#chat-input", Input)
        if chat_input.disabled:
            return
        chat_input.focus()
        chat_input.cursor_position = len(chat_input.value)

    # --- Direct Messages -------------------------------------------------
    #
    # A DM conversation is a DISTINCT model from channel CHAT (item 1),
    # keyed entirely by the remote party's stable node ID (item 2) --
    # never a display name. Deliberately reuses ChatEntryWidget/
    # ChatTranscript/ChatMessageInput/the delivery-state pipeline/
    # _rebroadcast (all already generic over a ChatEntry) rather than a
    # second, divergent widget tree; the conversation LIST/transcript
    # navigation below is its own, simpler model (no "load older"
    # pagination or unread-count badges in this first pass -- see item 8
    # and the completion report).

    def _dm_state_for(self, node_id: str) -> ChannelChatState:
        return self._dm_states.setdefault(node_id, ChannelChatState())

    def _ensure_dm_loaded(self, node_id: str) -> ChannelChatState:
        state = self._dm_state_for(node_id)
        if state.loaded:
            return state
        state.loaded = True
        if self.chat_store is None:
            return state
        try:
            page = self.chat_store.load_recent_dm_page(
                node_id, limit=DEFAULT_HISTORY_LIMIT
            )
        except ChatStoreError as error:
            self._show_dm_send_error(str(error))
            return state
        state.entries.extend(stored_chat_entry(stored) for stored in page.messages)
        return state

    def _capture_current_dm_state(self) -> None:
        if self.current_dm_node_id is None:
            return
        state = self._dm_state_for(self.current_dm_node_id)
        dm_inputs = list(self.query("#dm-input"))
        if dm_inputs:
            state.draft = dm_inputs[0].value

    def _dm_display_name(
        self, node_id: str, long_name: str | None, short_name: str | None
    ) -> str:
        if long_name and short_name and long_name != short_name:
            return f"{long_name} ({short_name})"
        return long_name or short_name or node_id

    def _dm_identity(self, node_id: str) -> tuple[str | None, str | None]:
        """Best-known LONG NAME/SHORT NAME for a DM party -- the SAME

        zero-RF NodeDB lookup open_user_menu already uses, falling back
        to whatever name this conversation's own most recent message
        carried (real evidence too, just possibly older) when NodeDB
        currently has nothing.
        """
        long_name = short_name = None
        getter = getattr(self.radio, "get_node_metadata", None)
        if callable(getter):
            try:
                metadata = getter(node_id)
            except Exception:
                metadata = None
            if isinstance(metadata, NodeMetadata):
                long_name = metadata.long_name
                short_name = metadata.short_name
        if not long_name and not short_name:
            state = self._dm_states.get(node_id)
            if state is not None:
                for entry in reversed(state.entries):
                    if not entry.outgoing:
                        long_name = entry.sender_name
                        short_name = entry.sender_short_name
                        break
        return long_name, short_name

    def dm_dropdown_conversations(self) -> tuple[DropdownOption, ...]:
        """Real DM conversations for the DM dropdown (PR #46 follow-up

        Part B), most-recent-activity first -- the SAME ordering
        _refresh_dm_list already derives from ChatStore.list_dm_
        conversations() (item 10: no independent ordering system).
        Zero RF: a pure SQLite read plus the same cached-NodeDB lookup
        _dm_identity already uses. Each option's own value is the
        canonical node_id -- never a display name or list position
        (item 8/30), so duplicate presentation names never collapse
        distinct conversations.
        """
        try:
            conversations = (
                self.chat_store.list_dm_conversations()
                if self.chat_store is not None
                else []
            )
        except ChatStoreError as error:
            self._show_dm_send_error(str(error))
            conversations = []
        options = []
        for node_id, _time in conversations:
            long_name, short_name = self._dm_identity(node_id)
            options.append(
                DropdownOption(_dm_dropdown_label(node_id, long_name, short_name), node_id)
            )
        return tuple(options)

    def _refresh_dm_list(self) -> None:
        """Rebuild the conversation list from persisted history -- zero

        RF traffic (get_node_metadata is a cached NodeDB read; see
        RadioService.get_node_metadata). Sorted by most-recent activity
        (item 8), never by name.
        """
        try:
            conversations = (
                self.chat_store.list_dm_conversations()
                if self.chat_store is not None
                else []
            )
        except ChatStoreError as error:
            self._show_dm_send_error(str(error))
            conversations = []
        self._dm_conversation_order = [node_id for node_id, _time in conversations]
        self._dm_list_highlighted_index = min(
            self._dm_list_highlighted_index, max(0, len(self._dm_conversation_order) - 1)
        )
        list_container = self.query_one("#dm-list", VerticalScroll)
        list_container.remove_children()
        if not self._dm_conversation_order:
            list_container.mount(
                Static("No DM conversations yet.", classes="dm-list-empty", markup=False)
            )
            return
        rows = []
        for index, node_id in enumerate(self._dm_conversation_order):
            long_name, short_name = self._dm_identity(node_id)
            label = self._dm_display_name(node_id, long_name, short_name)
            row = Static(f"{label}  {node_id}", classes="dm-list-row", markup=False)
            row.set_class(index == self._dm_list_highlighted_index, "highlighted")
            rows.append(row)
        list_container.mount(*rows)

    def _move_dm_list_highlight(self, direction: int) -> None:
        if not self._dm_conversation_order:
            return
        self._dm_list_highlighted_index = (
            self._dm_list_highlighted_index + direction
        ) % len(self._dm_conversation_order)
        rows = list(self.query("#dm-list .dm-list-row"))
        for index, row in enumerate(rows):
            row.set_class(index == self._dm_list_highlighted_index, "highlighted")
        if 0 <= self._dm_list_highlighted_index < len(rows):
            rows[self._dm_list_highlighted_index].scroll_visible(animate=False)

    def _activate_dm_list_selection(self) -> None:
        if not self._dm_conversation_order:
            return
        index = min(self._dm_list_highlighted_index, len(self._dm_conversation_order) - 1)
        node_id = self._dm_conversation_order[index]
        long_name, short_name = self._dm_identity(node_id)
        self._open_dm_conversation(node_id, long_name=long_name, short_name=short_name)

    def open_dm(
        self,
        node_id: str,
        *,
        long_name: str | None = None,
        short_name: str | None = None,
    ) -> None:
        """Public DM entry point: open/create this node's conversation and

        switch CHAT into DMS mode (CHAT/DM/MENTION UX item 7/16/14) --
        used by the CHAT sender menu and the MESH node menu, and
        directly by tests. Opens the EXACT conversation directly, never
        merely the generic DM list.
        """
        self.show_tab("chat")
        self._switch_chat_mode("dms")
        self._open_dm_conversation(node_id, long_name=long_name, short_name=short_name)

    def _open_dm_conversation(
        self,
        node_id: str,
        *,
        long_name: str | None,
        short_name: str | None,
    ) -> None:
        if self.current_dm_node_id == node_id:
            self.query_one("#dm-content", ContentSwitcher).current = "dm-conversation"
            self._mark_dm_conversation_read(node_id)
            return
        self._capture_current_dm_state()
        state = self._ensure_dm_loaded(node_id)
        self.current_dm_node_id = node_id
        self.query_one("#dm-header", Static).update(
            self._dm_header_text(node_id, long_name, short_name)
        )
        transcript = self.query_one("#dm-log", ChatTranscript)
        transcript.remove_children()
        widgets = [self._chat_entry_widget(entry) for entry in state.entries]
        if widgets:
            transcript.mount(*widgets)
        dm_input = self.query_one("#dm-input", Input)
        dm_input.value = state.draft
        dm_input.cursor_position = len(state.draft)
        self.query_one("#dm-content", ContentSwitcher).current = "dm-conversation"
        self._mark_dm_conversation_read(node_id)
        self._update_tab_bar()
        self.call_after_refresh(transcript.scroll_end, animate=False)
        if not dm_input.disabled:
            self.call_after_refresh(dm_input.focus)

    def _dm_header_text(
        self, node_id: str, long_name: str | None, short_name: str | None
    ) -> str:
        display_name = self._dm_display_name(node_id, long_name, short_name)
        if display_name == node_id:
            return f"DM / {node_id}"
        return f"DM / {display_name} / {node_id}"

    def _refresh_dm_header(self, node_id: str) -> None:
        """Re-resolve a DM conversation's best-known identity and re-render

        its header. Zero RF (a cached NodeDB read, exactly like
        _dm_identity). Used to pick up a node's newly-learned/renamed name
        while its conversation is open; the conversation's canonical node
        ID identity never changes.
        """
        long_name, short_name = self._dm_identity(node_id)
        self.query_one("#dm-header", Static).update(
            self._dm_header_text(node_id, long_name, short_name)
        )

    def _close_dm_conversation(self) -> None:
        self._exit_new_dm_mode()
        self._capture_current_dm_state()
        self.current_dm_node_id = None
        self.query_one("#dm-content", ContentSwitcher).current = "dm-list"
        self._refresh_dm_list()
        list_container = self.query_one("#dm-list", VerticalScroll)
        list_container.focus()
        self._update_tab_bar()

    def _exit_new_dm_mode(self) -> None:
        """Clear the transient NEW DM entry surface (if it is showing).

        Only ever touches presentation state: hides the instruction line
        and resets the flag. Zero RF, creates no conversation. Safe to
        call when NEW DM was never opened.
        """
        self._new_dm_mode = False
        widgets = list(self.query("#dm-new-instruction"))
        if widgets:
            widgets[0].display = False

    def _start_new_dm(self) -> None:
        """Open the transient NEW DM node-ID entry surface (see DMModeSelector).

        Shows "DM / NEW" in the header and the instruction text, empties
        the transcript, and repurposes the normal DM composer as a node-ID
        field. Zero RF: nothing is created or sent until a valid node ID
        is actually entered.
        """
        self._switch_chat_mode("dms")
        self._capture_current_dm_state()
        self.current_dm_node_id = None
        self._new_dm_mode = True
        self.query_one("#dm-content", ContentSwitcher).current = "dm-conversation"
        self.query_one("#dm-header", Static).update("DM / NEW")
        self.query_one("#dm-new-instruction", Static).update(
            "TYPE THE NODE ID AND HIT ENTER TO CREATE DM"
        )
        self.query_one("#dm-new-instruction", Static).display = True
        transcript = self.query_one("#dm-log", ChatTranscript)
        transcript.remove_children()
        self._show_dm_send_error("")
        dm_input = self.query_one("#dm-input", Input)
        dm_input.value = ""
        dm_input.cursor_position = 0
        self._update_tab_bar()
        if not dm_input.disabled:
            self.call_after_refresh(dm_input.focus)

    def _cancel_new_dm(self) -> None:
        """ESC from the NEW DM surface: return to the DM list, zero RF."""
        self._close_dm_conversation()

    def _submit_new_dm_node_id(self, raw: str) -> None:
        """ENTER in NEW DM mode: validate + canonicalize the node ID.

        A valid ID opens (or creates) the DM conversation locally, keyed
        by the canonical node ID -- never a display name, never a probe/
        traceroute/NodeInfo request, never any RF. An invalid ID stays in
        NEW DM mode and shows a compact ERROR, creating nothing.
        """
        canonical = canonical_entered_node_id(raw)
        if canonical is None:
            # Keep the typed text (the user may want to fix it) and just
            # show a compact error while remaining in NEW DM mode. Clearing
            # the field here would fire Input.Changed, whose own handler
            # clears the error we are about to show.
            self._show_dm_send_error("INVALID NODE ID")
            self.query_one("#dm-input", Input).focus()
            return
        self._exit_new_dm_mode()
        long_name, short_name = self._dm_identity(canonical)
        self._open_dm_conversation(canonical, long_name=long_name, short_name=short_name)

    def _delete_current_dm(self) -> None:
        """CTRL+D: permanently delete the DM conversation currently viewed.

        Only acts while CHAT is showing an actual DM conversation (a real
        current_dm_node_id) -- never in NEW DM mode or the DM list. Deletes
        the persisted history for that one conversation (keyed by canonical
        node ID, never display name), drops its session-local state/unread,
        then opens the NEXT remaining DM in the selector's order (wrapping)
        so the user is never stranded; if none remain it returns to CHAT's
        default DM state (the conversation list). Never opens NEW DM. Zero
        RF; nothing is deleted from the radio/NodeDB; channel history is
        untouched; a future message with that node naturally recreates the
        conversation.
        """
        node_id = self.current_dm_node_id
        if node_id is None:
            return
        # Capture the selector order BEFORE deletion so "next" is relative
        # to the deleted conversation's own position in that ordering.
        prior_order = list(self._dm_conversation_order)
        if self.chat_store is not None:
            try:
                self.chat_store.delete_dm_conversation(node_id)
            except ChatStoreError as error:
                self._show_dm_send_error(str(error))
                return
        self._dm_states.pop(node_id, None)
        self.current_dm_node_id = None
        self._exit_new_dm_mode()
        self._recount_dm_unread()

        remaining = (
            [
                conversation_id
                for conversation_id, _time in self.chat_store.list_dm_conversations()
            ]
            if self.chat_store is not None
            else []
        )
        if remaining:
            next_id = self._next_dm_after(prior_order, node_id, remaining)
            long_name, short_name = self._dm_identity(next_id)
            self._open_dm_conversation(
                next_id, long_name=long_name, short_name=short_name
            )
        else:
            # No DMs left: CHAT's default DM state (the conversation list).
            self.query_one("#dm-content", ContentSwitcher).current = "dm-list"
            self._refresh_dm_list()
            self.query_one("#dm-list", VerticalScroll).focus()
        self._update_tab_bar()

    @staticmethod
    def _next_dm_after(
        prior_order: list[str], deleted_id: str, remaining: list[str]
    ) -> str:
        """Pick the DM immediately after `deleted_id` in the selector order.

        Walks the PRE-delete order (most-recent-activity first) starting one
        position past the deleted conversation and wrapping, choosing the
        first ID still present in `remaining`. Falls back to the first
        remaining conversation when `prior_order` is stale or no longer
        references the deleted ID.
        """
        if not prior_order or deleted_id not in prior_order:
            return remaining[0]
        index = prior_order.index(deleted_id)
        count = len(prior_order)
        for offset in range(1, count + 1):
            candidate = prior_order[(index + offset) % count]
            if candidate in remaining:
                return candidate
        return remaining[0]

    # --- NEW CHANNEL (private-channel CHAT-local editor state machine) ----
    #
    # Simulator/UI only: this builds a truthfully-pending PendingChannelConfig
    # (validated name + canonical Base64 PSK) and represents it app-side. It
    # NEVER writes the channel to the radio, never fabricates a radio-
    # authoritative ChannelInfo/slot, and never touches PRESET/LoRa.

    def _start_new_channel(self) -> None:
        """Open the CHAT-local NEW CHANNEL editor. Zero writes/RF.

        `_new_channel_editor_open` is a distinct CHAT state from the
        configured-channel selector: it holds no slot/index, is never part
        of `self._channels`, and is dropped on cancel/ESC. The editor input
        values (CHANNEL NAME / KEY) live in the CHAT-local editor widgets;
        the validated result is captured in `_pending_channel`.
        """
        if self.current_tab != "chat":
            self.show_tab("chat")
        self._switch_chat_mode("channel")
        self._pending_channel = None
        self._new_channel_error = ""
        self._new_channel_editor_open = True
        self._clear_new_channel_inputs()
        self._refresh_new_channel_editor()
        self._focus_new_channel_field("name")
        self._update_footer()

    def _cancel_new_channel(self) -> None:
        """Discard the pending draft and editor inputs. Zero writes/RF."""
        self._pending_channel = None
        self._new_channel_error = ""
        self._new_channel_editor_open = False
        self._clear_new_channel_inputs()
        self._refresh_new_channel_editor()
        self._update_footer()

    def _clear_new_channel_inputs(self) -> None:
        for selector in ("#new-channel-name", "#new-channel-key"):
            widgets = list(self.query(selector))
            if widgets:
                widgets[0].value = ""

    def _save_new_channel(self, name: str, key: str) -> PendingChannelConfig | None:
        """Validate and build the pending private-channel config.

        Blank KEY -> generate_private_psk (a fresh secure PSK); supplied KEY
        -> normalize_private_psk. Either way the result is ONLY a pending
        draft: no radio write, no ChannelInfo/slot, no PRESET/LoRa change.
        Invalid input leaves the editor open with `_new_channel_error` set
        and the editor's entered values preserved (callers must not clear
        the inputs). Returns the pending config, or None on validation error.
        """
        name = name.strip()
        if not name:
            self._new_channel_error = "CHANNEL NAME REQUIRED"
            self._new_channel_editor_open = True
            self._refresh_new_channel_editor()
            return None
        key = key.strip()
        if key:
            normalized = normalize_private_psk(key)
            if normalized is None:
                self._new_channel_error = "INVALID KEY"
                self._new_channel_editor_open = True
                self._refresh_new_channel_editor()
                return None
            base64_text, raw_psk = normalized
            generated = False
        else:
            base64_text = generate_private_psk()
            raw_psk = base64.b64decode(base64_text)
            generated = True
        self._pending_channel = PendingChannelConfig(
            name=name,
            psk_base64=base64_text,
            raw_psk=raw_psk,
            generated=generated,
        )
        self._new_channel_error = ""
        self._new_channel_editor_open = False
        self._refresh_new_channel_editor()
        return self._pending_channel

    def _refresh_new_channel_editor(self) -> None:
        """Render / hide the CHAT-local NEW CHANNEL editor.

        Reflects `_new_channel_editor_open` + `_pending_channel` +
        `_new_channel_error`. Produces no radio traffic and no ChatStore
        writes. NAME/KEY Input values are only ever set here on open/cancel
        (never cleared on validation error, so the user's entries survive).

        When a pending config exists (SAVE succeeded), the editor closes and
        the "NOT YET APPLIED TO RADIO + PSK" strip shows in the normal CHAT
        view -- clearly not a radio-configured channel, never added to the
        configured-channel selector, never given CHAT history.
        """
        switcher_widgets = list(self.query("#chat-channel-content"))
        if switcher_widgets:
            switcher_widgets[0].current = (
                "new-channel-editor"
                if self._new_channel_editor_open
                else "chat-conversation"
            )

        pending = self._pending_channel
        pending_widgets = list(self.query("#new-channel-pending"))
        if pending_widgets:
            pending_widgets[0].display = pending is not None
            if pending is not None:
                pending_widgets[0].update(
                    "NOT YET APPLIED TO RADIO\nPSK  " + pending.psk_base64
                )
            else:
                pending_widgets[0].update("")

        error_widgets = list(self.query("#new-channel-error"))
        if error_widgets:
            error_widgets[0].display = bool(self._new_channel_error)
            error_widgets[0].update(self._new_channel_error)

    def _activate_new_channel_save(self) -> bool:
        """Validate the current editor fields; returns True on success.

        Reads #new-channel-name / #new-channel-key and delegates to
        _save_new_channel. On success leaves the compact pending strip
        visible and focuses the CHAT transcript; on failure keeps the editor
        fields (values preserved) focused.
        """
        name_inputs = list(self.query("#new-channel-name"))
        key_inputs = list(self.query("#new-channel-key"))
        name = name_inputs[0].value if name_inputs else ""
        key = key_inputs[0].value if key_inputs else ""
        return self._save_new_channel(name, key) is not None

    def _activate_new_channel_apply(self) -> None:
        """APPLY the pending private channel to the radio (async worker).

        Guarded: offline before APPLY, or an apply already active, is a no-op
        (zero writes). On a verified success the pending state is dropped, the
        channel list is refreshed from radio-authoritative state, CHAT switches
        to the promoted channel, and its PSK metadata displays normally. On any
        failure the pending config is preserved for retry/correction and a
        compact error is shown -- nothing is promoted before a matching
        readback, and a stale completion from an obsolete radio can never
        mutate current state (apply_private_channel already guards the session).
        """
        pending = self._pending_channel
        if pending is None or self._pending_apply_active:
            return
        if self._radio_state is not RadioState.ONLINE:
            self._new_channel_error = "RADIO NOT ONLINE"
            self._refresh_new_channel_editor()
            return
        self._pending_apply_active = True
        self._new_channel_error = ""
        self._refresh_new_channel_editor()

        def worker() -> None:
            result = None
            try:
                result = self.radio.apply_private_channel(
                    name=pending.name, psk=pending.raw_psk
                )
            except Exception as error:
                self.post_message(
                    PrivateChannelApplyFailed(str(error), pending)
                )
                return
            self.post_message(PrivateChannelApplyResultMessage(result, pending))

        self.run_worker(worker, thread=True)

    @on(NewChannelApply.Activated)
    def new_channel_apply(self, _event: NewChannelApply.Activated) -> None:
        self._activate_new_channel_apply()

    @on(PrivateChannelApplyResultMessage)
    def private_channel_apply_result(
        self, event: "PrivateChannelApplyResultMessage"
    ) -> None:
        self._pending_apply_active = False
        result = event.result
        if result is None or not getattr(result, "ok", False):
            self._new_channel_error = (
                getattr(result, "error", "APPLY FAILED") or "APPLY FAILED"
            )
            self._new_channel_editor_open = True
            self._refresh_new_channel_editor()
            return
        # Verified success: promote ONLY from radio-authoritative state.
        slot = getattr(result, "slot", None)
        channels = ()
        getter = getattr(self.radio, "get_config_channels", None)
        if callable(getter):
            try:
                channels = getter()
            except Exception:
                channels = ()
        promoted = next(
            (c for c in channels if c.index == slot), None
        )
        if promoted is None:
            self._new_channel_error = "CHANNEL NOT FOUND AFTER APPLY"
            self._new_channel_editor_open = True
            self._refresh_new_channel_editor()
            return
        self._channels = channels
        self._pending_channel = None
        self._new_channel_error = ""
        self._new_channel_editor_open = False
        self._clear_new_channel_inputs()
        self._refresh_new_channel_editor()
        # Switch CHAT to the new configured channel (local, zero-write/RF).
        self._switch_chat_mode("channel")
        self.run_worker(self._switch_channel(promoted.index), name="switch-to-private-channel")
        self._update_tab_bar()

    @on(PrivateChannelApplyFailed)
    def private_channel_apply_failed(self, event: "PrivateChannelApplyFailed") -> None:
        self._pending_apply_active = False
        self._new_channel_error = event.detail
        self._new_channel_editor_open = True
        self._refresh_new_channel_editor()

    def _focus_new_channel_field(self, field: str) -> None:
        """Focus one editor field/control: name | key | save | cancel."""
        widget_ids = {
            "name": "#new-channel-name",
            "key": "#new-channel-key",
            "save": "#new-channel-save",
            "cancel": "#new-channel-cancel",
        }
        widgets = list(self.query(widget_ids[field]))
        if widgets:
            widgets[0].focus()
            if isinstance(widgets[0], Input):
                widgets[0].cursor_position = len(widgets[0].value)

    _NEW_CHANNEL_FIELD_ORDER = ("name", "key", "cancel", "save")

    def _new_channel_focused_field(self) -> str:
        focused = self.focused
        focused_id = getattr(focused, "id", None)
        for field, selector in (
            ("name", "#new-channel-name"),
            ("key", "#new-channel-key"),
            ("save", "#new-channel-save"),
            ("cancel", "#new-channel-cancel"),
        ):
            if focused_id == selector.lstrip("#"):
                return field
        return "name"

    def _move_new_channel_focus(self, direction: int) -> None:
        order = self._NEW_CHANNEL_FIELD_ORDER
        current = self._new_channel_focused_field()
        try:
            index = order.index(current)
        except ValueError:
            index = 0
        target = order[(index + direction) % len(order)]
        self._focus_new_channel_field(target)

    @on(Input.Submitted, "#new-channel-name")
    def new_channel_name_submitted(self, _event: Input.Submitted) -> None:
        if self._new_channel_editor_open:
            self._focus_new_channel_field("key")

    @on(Input.Submitted, "#new-channel-key")
    def new_channel_key_submitted(self, _event: Input.Submitted) -> None:
        if self._new_channel_editor_open:
            self._focus_new_channel_field("key" if self._new_channel_error else "save")

    @on(NewChannelSave.Activated)
    def new_channel_save(self, _event: NewChannelSave.Activated) -> None:
        if not self._new_channel_editor_open:
            return
        self._activate_new_channel_save()
        if self._new_channel_editor_open:
            self._refresh_new_channel_editor()
            self._focus_new_channel_field("key" if self._new_channel_error else "name")
        else:
            self.query_one("#chat-log", ChatTranscript).focus()

    @on(NewChannelCancel.Activated)
    def new_channel_cancel(self, _event: NewChannelCancel.Activated) -> None:
        if not self._new_channel_editor_open:
            return
        self._cancel_new_channel()
        self.query_one("#chat-log", ChatTranscript).focus()

    def _pending_channel_psk_summary(self) -> str:
        """The generated/normalized key text for the pending editor, or "".

        Presentation only (UI metadata), never logged; used so the user can
        retain/share a freshly generated key before any radio write.
        """
        if self._pending_channel is None:
            return ""
        return self._pending_channel.psk_base64

    def _channel_psk_metadata_text(self) -> str:
        """PSK metadata line for the CURRENT configured channel, or "".

        UI metadata only -- never a ChatStore message, never logged. Returns
        "" for the public/default channel (no PSK clutter), so a private
        channel's normalized Base64 key is the only thing shown, read from
        the radio-authoritative channel settings (zero RF).
        """
        for channel in self._channels:
            if channel.index != self.current_channel_index:
                continue
            getter = getattr(self.radio, "channel_psk_text", None)
            if not callable(getter):
                return ""
            try:
                psk = getter(channel.index)
            except Exception:
                return ""
            if not psk:
                return ""
            return f"PSK  {psk}"
        return ""

    def _dm_navigation_targets(self) -> list[Static | ChatEntryWidget]:
        targets: list[Static | ChatEntryWidget] = []
        transcript = self.query_one("#dm-log", ChatTranscript)
        for widget in transcript.walk_children():
            if isinstance(widget, MessageActionControl):
                if widget.display and widget.action == "resend":
                    targets.append(widget)
                continue
            if (
                isinstance(widget, ChatEntryWidget)
                and widget.display
                and not can_manual_resend(widget.entry)
            ):
                targets.append(widget)
        return targets

    def _move_dm_focus(self, direction: int) -> None:
        targets = self._dm_navigation_targets()
        if not targets:
            if direction > 0:
                self._focus_dm_composer()
            return
        try:
            index = targets.index(self.focused)
            next_index = index + direction
            if direction > 0 and next_index >= len(targets):
                self._focus_dm_composer()
                return
            target = targets[max(0, min(len(targets) - 1, next_index))]
        except ValueError:
            if direction > 0:
                self._focus_dm_composer()
                return
            target = targets[-1]
        target.focus()
        target.scroll_visible(animate=False)

    def _focus_dm_composer(self) -> None:
        dm_input = self.query_one("#dm-input", Input)
        if dm_input.disabled:
            return
        dm_input.focus()
        dm_input.cursor_position = len(dm_input.value)

    def _show_dm_send_error(self, message: str) -> None:
        widgets = list(self.query("#dm-send-error"))
        if widgets:
            widgets[0].update(message)

    def _start_dm_outgoing(self, node_id: str, text: str) -> ChatEntry:
        state = self._dm_state_for(node_id)
        entry = outgoing_chat_entry(
            text,
            dm_node_id=node_id,
            delivery_state=DeliveryState.SENDING,
        )
        entry.send_generation = 1
        self._assign_arrival_order(entry)
        self._persist_outgoing(entry)
        state.entries.append(entry)
        if self.current_dm_node_id == node_id:
            transcript = self.query_one("#dm-log", ChatTranscript)
            transcript.mount(self._chat_entry_widget(entry))
            transcript.scroll_end(animate=False)
            dm_input = self.query_one("#dm-input", Input)
            dm_input.value = ""
            self._show_dm_send_error("")
        return entry

    @on(Input.Submitted, "#dm-input")
    def dm_input_submitted(self, event: Input.Submitted) -> None:
        if self._new_dm_mode:
            self._submit_new_dm_node_id(event.value)
            return
        if self.current_dm_node_id is None or self._radio_state is not RadioState.ONLINE:
            return
        text = event.value
        if not text.strip():
            self._show_dm_send_error("Message text cannot be empty.")
            return
        entry = self._start_dm_outgoing(self.current_dm_node_id, text)
        generation = entry.send_generation
        self.run_worker(
            lambda: self._send_from_thread(entry, generation),
            thread=True,
        )
        # Mirrors send_chat_message's own neutral-focus return -- see
        # its comment for why this never discards typed text.
        self.query_one("#dm-log", ChatTranscript).focus()

    @on(Input.Changed, "#dm-input")
    def dm_input_changed(self, _event: Input.Changed) -> None:
        self._show_dm_send_error("")

    def _accept_received_dm(self, message: ReceivedMessage) -> None:
        """Route an incoming DM to its own conversation -- NEVER mingled

        into channel history merely because packet.channel is present
        (item 12): this is only reached when RadioService's own
        packet-destination classification (message.is_direct) already
        said so (see _accept_received_message). Unread model (item
        15/16): an incoming DM increments DM(N) unless this EXACT
        conversation is actively visible right now (CHAT tab, DMS
        mode, this node_id open) -- see _recount_dm_unread's own
        docstring for the full chosen model, including why it is
        deliberately session-local, never persisted.
        """
        node_id = message.sender_node_id
        state = self._ensure_dm_loaded(node_id)
        dm_visible = (
            self.current_tab == "chat"
            and self._chat_mode == "dms"
            and self.current_dm_node_id == node_id
        )
        entry = received_chat_entry(
            message,
            app_received_at=time(),
            monotonic_now=monotonic(),
            unread=not dm_visible,
            is_new=True,
        )
        self._assign_arrival_order(entry)
        inserted = self._persist_incoming(entry)
        if not inserted:
            return
        state.entries.append(entry)
        if entry.message_id is not None and not dm_visible:
            state.unread_message_ids.add(entry.message_id)
        if not dm_visible:
            state.unread_count += 1
            self._recount_dm_unread()
        if dm_visible:
            transcript = self.query_one("#dm-log", ChatTranscript)
            transcript.mount(self._chat_entry_widget(entry))
            transcript.scroll_end(animate=False)
            # A node whose identity was unknown when its DM was created
            # (or was since renamed) updates its header presentation as
            # soon as a real message from it arrives -- the conversation
            # identity (canonical node ID) never changes, only the name.
            self._refresh_dm_header(node_id)
        if (
            self.current_tab == "chat"
            and self._chat_mode == "dms"
            and self.current_dm_node_id is None
        ):
            self._refresh_dm_list()
        self._update_tab_bar()

    def _mark_dm_conversation_read(self, node_id: str) -> None:
        """Clear unread state for exactly this DM conversation (item

        15/16) -- mirrors _mark_unread_messages_viewed's channel
        equivalent, purely in-memory (see _recount_dm_unread).
        """
        state = self._dm_state_for(node_id)
        for entry in state.entries:
            if entry.unread:
                entry.unread = False
                if entry.message_id is not None:
                    state.unread_message_ids.discard(entry.message_id)
        state.unread_count = 0
        self._recount_dm_unread()

    def _recount_dm_unread(self) -> None:
        """Sum every DM conversation's own unread_count into DM(N)'s

        badge (item 15). Chosen unread model, documented per the
        task's own request to decide and report it: reuses the EXACT
        same in-memory, session-local, non-persisted mechanism channel
        CHAT's own unread_count/unread_message_ids already use (see
        _accept_received_message/_recount_unread) -- deliberately NOT
        backed by a chat.db column/migration. Two reasons: (1) channel
        CHAT's own unread state is ALREADY not persisted today (it
        resets to 0 every restart, rebuilt only from messages that
        arrive DURING the running session -- see _accept_received_
        message's chat_is_visible/unread wiring), so a persisted DM
        model would be a second, divergent unread architecture living
        alongside a non-persisted one for the exact same conceptual
        feature -- precisely the "fragile parallel state system" this
        task's own Part E/item 17 warns against; (2) restart behavior
        is still fully deterministic under this model: every unread
        count -- channel AND DM alike -- resets to 0 on every restart,
        consistently, not silently/accidentally.
        """
        self.dm_unread_count = sum(
            state.unread_count for state in self._dm_states.values()
        )
        selectors = list(self.query(DMModeSelector))
        if selectors:
            selectors[0].set_options(
                (DropdownOption(f"DM({self.dm_unread_count})", "dms"),),
                value="dms",
            )

    def _reposition_dm_entry(self, entry: ChatEntry) -> None:
        if entry.dm_node_id is None:
            return
        state = self._dm_states.get(entry.dm_node_id)
        if state is None or entry not in state.entries:
            return
        state.entries.remove(entry)
        self._insert_entry_in_order(state.entries, entry)
        if (
            self.current_dm_node_id != entry.dm_node_id
            or self.current_tab != "chat"
            or self._chat_mode != "dms"
        ):
            return
        widget = next(
            (candidate for candidate in self.query(ChatEntryWidget) if candidate.entry is entry),
            None,
        )
        if widget is not None:
            transcript = self.query_one("#dm-log", ChatTranscript)
            others = [
                candidate
                for candidate in transcript.query(ChatEntryWidget)
                if candidate is not widget
            ]
            if others:
                transcript.move_child(widget, after=others[-1])

    def _delete_dm_entry(self, entry: ChatEntry) -> None:
        """DEL for a DM message: local history deletion only, zero RF

        traffic -- mirrors _delete_chat_entry's own guarantees, simpler
        focus restoration (DM's own navigation model has no vertical
        LEFT/RIGHT power-navigation to preserve).
        """
        if not can_manual_resend(entry) or entry.dm_node_id is None:
            return
        state = self._dm_states.get(entry.dm_node_id)
        entry.deleted = True
        if entry.packet_id is not None:
            pending_sends = getattr(self.radio, "_pending_sends", None)
            if isinstance(pending_sends, dict):
                pending_sends.pop(entry.packet_id, None)
        if state is not None and entry in state.entries:
            state.entries.remove(entry)
        if entry.message_id is not None and self.chat_store is not None:
            try:
                self.chat_store.delete_message(entry.message_id)
            except ChatStoreError as error:
                self._show_dm_send_error(str(error))
        for widget in list(self.query(ChatEntryWidget)):
            if widget.entry is entry:
                widget.remove()
                break
        self._focus_dm_composer()

    @on(MessageActionControl.Activated)
    def message_action_activated(
        self,
        event: MessageActionControl.Activated,
    ) -> None:
        entry = event.action_control.entry
        if event.action_control.action == "resend":
            self._rebroadcast(entry)
            for widget in self.query(ChatEntryWidget):
                if widget.entry is entry:
                    widget.focus()
                    break
        elif event.action_control.action == "delete":
            if entry.dm_node_id is not None:
                self._delete_dm_entry(entry)
            else:
                self._delete_chat_entry(entry)

    def _delete_chat_entry(self, entry: ChatEntry) -> None:
        """DEL: permanently remove one LOCAL outgoing entry -- never the

        mesh. A packet already handed to the radio for transmission
        cannot be pulled back out of the air, so this only ever touches
        local application state: the UI, self.chat_history (and its
        backing ChannelChatState -- the same list object, see
        _state_for), and chat_store's persisted row. Generates zero
        RF/admin traffic on its own.

        Only ever offered where RESEND already is (can_manual_resend),
        so `entry` is always a member of the CURRENT channel's
        self.chat_history, exactly like _reposition_chat_entry assumes
        for RESEND -- never another channel's message, and a RESEND's
        own copy is a SEPARATE ChatEntry object (see _rebroadcast,
        which mutates the SAME entry in place rather than creating a
        new one), so deleting one can never affect the other.

        entry.deleted is set FIRST, before anything else: it is the
        actual guard send_was_submitted/send_failed/
        delivery_status_received check before touching a message again,
        so a response already sitting in the app's own message queue
        (posted before this ran, processed after) is still caught, not
        just responses that arrive later. Retiring the radio-layer
        correlation (_pending_sends) additionally stops a real
        RadioService from even invoking that status_handler at all for
        a NOT-yet-queued late response -- belt and suspenders, since
        _pending_sends is a RadioService-specific implementation detail
        that not every radio backend (e.g. SimulatedRadioService) has.
        """
        if not can_manual_resend(entry):
            return
        # Item 8: focus must never point at a destroyed widget, and
        # should prefer landing on whatever remaining target now
        # occupies DEL's own vertical slot (the next-older message, or
        # the composer if this was the last one) over a generic
        # "reset to the transcript" fallback. Captured BEFORE removal:
        # DEL's own slot is the deleted message's RESEND control's
        # position (delete_control is never itself a vertical target
        # -- see _chat_navigation_targets), one past it.
        targets_before = self._chat_navigation_targets()
        focus_index = next(
            (
                index
                for index, target in enumerate(targets_before)
                if isinstance(target, MessageActionControl) and target.entry is entry
            ),
            None,
        )
        entry.deleted = True
        if entry.packet_id is not None:
            pending_sends = getattr(self.radio, "_pending_sends", None)
            if isinstance(pending_sends, dict):
                pending_sends.pop(entry.packet_id, None)
        if entry in self.chat_history:
            self.chat_history.remove(entry)
        for widget in list(self.query(ChatEntryWidget)):
            if widget.entry is entry:
                widget.remove()
                break
        if self.chat_store is not None and entry.message_id is not None:
            try:
                self.chat_store.delete_message(entry.message_id)
            except ChatStoreError as error:
                self._show_send_error(str(error))
        if focus_index is not None:
            remaining = self._chat_navigation_targets()
            if remaining:
                target = remaining[min(focus_index, len(remaining) - 1)]
                target.focus()
                target.scroll_visible(animate=False)
                return
            self._focus_chat_composer()
            return
        self.query_one("#chat-log", ChatTranscript).focus()

    @on(ChatEntryWidget.UserMenuRequested)
    def open_user_menu(self, event: ChatEntryWidget.UserMenuRequested) -> None:
        """Open node details/actions without changing transcript layout.

        The NAME row and REPLY's @mention must use the SAME name the
        user is actually looking at on this message -- entry.author,
        computed once at receipt time (see
        app_controller.received_chat_entry: sender_long_name ->
        sender_short_name -> sender_node_id, never empty) -- not
        whatever a FRESH NodeDB lookup happens to report right now.

        This matters because the two can genuinely diverge: the live
        NodeDB record for a node can be overwritten by a LATER, more
        generic broadcast (e.g. a bare auto-generated "Meshtastic ab59"
        default before that node re-announces its real chosen name),
        while this specific message's OWN packet already carried the
        real name at the time it arrived. A fresh lookup is still used
        for hops_away/last_heard/is_local, which are legitimately
        "as of right now" facts a point-in-time snapshot can't provide.
        """
        entry = event.widget.entry
        if entry.outgoing or not entry.node_id:
            return
        if entry.dm_node_id is not None:
            # Inside a DM's own transcript, the person you're already
            # 1:1 with is unambiguous (see the DM header) -- REPLY's
            # @mention exists to disambiguate a sender in a multi-person
            # CHANNEL, which does not apply here, so this menu is simply
            # not offered on a DM message (item 9: "reply insertion if
            # it makes sense inside a 1:1 conversation" -- it does not).
            return
        metadata = NodeMetadata(
            entry.node_id,
            entry.author,
            entry.sender_short_name,
        )
        getter = getattr(self.radio, "get_node_metadata", None)
        if callable(getter):
            try:
                current = getter(entry.node_id)
            except Exception:
                current = None
            if isinstance(current, NodeMetadata):
                metadata = NodeMetadata(
                    entry.node_id,
                    metadata.long_name or current.long_name,
                    metadata.short_name or current.short_name,
                    current.hops_away,
                    current.last_heard,
                    current.is_local,
                    is_unmessagable=current.is_unmessagable,
                )

        self._open_node_menu(
            metadata,
            event.widget,
            self.query_one("#chat-log", ChatTranscript),
        )

    def _open_node_menu(
        self,
        metadata: NodeMetadata,
        origin: Widget,
        scroll_target: ScrollableContainer | None,
        *,
        include_rx_age: bool = False,
        allow_reply: bool = True,
        allow_dm: bool = True,
        allow_traceroute: bool = False,
    ) -> None:
        """Open the shared CHAT/MESH node-details menu.

        allow_reply defaults to True, preserving CHAT's existing
        REPLY-then-@mention gesture unchanged. MESH's own call site
        passes allow_reply=False: within MESH, the node-options menu
        deliberately never gains a REPLY-into-CHAT-composer action --
        everything else about this shared menu (name/hops rows,
        FAVORITE toggle, highlight/placement behavior) stays exactly
        as CHAT already uses it.

        allow_dm defaults to True; DM (open/create that node's Direct
        Message conversation) is offered for any remote node UNLESS the
        caller passes allow_dm=False or the node's own NodeDB metadata
        says it cannot receive messages (metadata.is_unmessagable --
        see RadioService/NodeMetadata). Never offered for YOU (the
        is_local branch below has no actionable rows at all).

        allow_traceroute defaults to False; TRACE ROUTE (Part C) is
        explicit, user-triggered RF traffic offered ONLY from MESH's own
        ENTER menu (see _open_mesh_node_menu), never CHAT's. Omitted
        entirely (not merely shown-and-ignored) while a trace is already
        pending -- v1 allows exactly one active trace at a time, and an
        absent option is clearer feedback than a silent no-op.
        """

        items: list[PopupItem] = []
        long_name = metadata.long_name.strip() if metadata.long_name else None
        short_name = metadata.short_name.strip() if metadata.short_name else None
        if long_name:
            items.append(PopupItem(long_name, actionable=False))
        # Never repeat Short Name as its own row when it's the same
        # displayed value as Long Name -- never a blank/fake "?" row
        # either; an unavailable name is simply omitted.
        if short_name and short_name != long_name:
            items.append(PopupItem(short_name, actionable=False))
        if metadata.hops_away is not None:
            unit = "HOP" if metadata.hops_away == 1 else "HOPS"
            items.append(
                PopupItem(
                    f"{metadata.hops_away} {unit} AWAY",
                    actionable=False,
                )
            )
        if (
            include_rx_age
            and metadata.last_heard is not None
            and metadata.last_heard <= time()
        ):
            items.append(
                PopupItem(
                    f"RX {format_relative_age(time() - metadata.last_heard)}",
                    actionable=False,
                )
            )
        if metadata.is_local:
            items.append(PopupItem(metadata.node_id, actionable=False))
            highlighted = 0
        else:
            if allow_reply:
                items.append(PopupItem("REPLY", "reply", actionable=True))
            if allow_dm and not metadata.is_unmessagable:
                items.append(PopupItem("DIRECT MSG", "dm", actionable=True))
            favorite = self.settings.is_favorite(metadata.node_id)
            action = "unfavorite" if favorite else "favorite"
            label = "UNHIGHLIGHT" if favorite else "HIGHLIGHT"
            items.append(PopupItem(label, action, actionable=True))
            # Preserve the existing HIGHLIGHT/UNHIGHLIGHT default highlight
            # (the established Enter-Enter toggle gesture) -- REPLY is a
            # newly added item, reached with one extra arrow press, not
            # a change to what pressing Enter immediately does.
            highlighted = len(items) - 1
            if allow_traceroute and self._active_traceroute is None:
                items.append(PopupItem("TRACEROUTE", "traceroute", actionable=True))
        menu = ViewportMenu(
            items,
            highlighted_index=highlighted,
            on_activate=lambda _index, item: self._activate_menu_item(
                metadata, str(item.value)
            ),
            menu_id="node-context-menu",
        )
        self._close_user_menu(restore_focus=False)
        self._user_menu = menu
        self._user_menu_origin = origin
        self._user_menu_scroll_target = scroll_target
        self._user_menu_scroll_x = (
            scroll_target.scroll_x if scroll_target is not None else None
        )
        self._user_menu_scroll_y = (
            scroll_target.scroll_y if scroll_target is not None else None
        )
        width = max(cell_len(item.label) for item in items) + 4
        self.screen.mount(menu)
        menu.place(origin.region, self.screen.region, width)

    def _activate_menu_item(self, metadata: NodeMetadata, action: str) -> None:
        """Dispatch a node-context-menu action -- REPLY needs the node's

        name fields (for the @mention), so it is handled separately
        from the existing node_id-only favorite/unfavorite path.
        """
        if action == "reply":
            self._activate_reply(metadata)
            return
        if action == "traceroute":
            self._close_user_menu(restore_focus=False)
            self._start_traceroute(metadata)
            return
        if action == "dm":
            self._close_user_menu(restore_focus=False)
            self.open_dm(
                metadata.node_id,
                long_name=metadata.long_name,
                short_name=metadata.short_name,
            )
            return
        self._activate_node_action(metadata.node_id, action)

    def _activate_reply(self, metadata: NodeMetadata) -> None:
        """Insert an @mention for this node at the composer's CURRENT

        CURSOR POSITION (an empty composer is the trivial case: the
        mention becomes the whole draft so far), then hand focus back
        to the input for continued typing. Existing draft text before
        and after the cursor is always preserved untouched. A text-
        entry convenience only -- never changes Meshtastic addressing/
        routing based on the textual @mention. Relies on Textual's own
        Input.cursor_position/value indexing rather than reimplementing
        cursor math, so this stays correct for multi-codepoint grapheme
        clusters already in the draft.
        """
        self._close_user_menu(restore_focus=False)
        mention = f"@{_reply_mention_name(metadata)}"
        chat_input = self.query_one("#chat-input", Input)
        draft = chat_input.value
        cursor = chat_input.cursor_position
        insertion = f"{mention} "
        new_value = draft[:cursor] + insertion + draft[cursor:]
        new_cursor = cursor + len(insertion)
        chat_input.value = new_value
        if self.current_tab != "chat":
            self.show_tab("chat")
        if self._chat_mode != "channel":
            self._switch_chat_mode("channel")
        chat_input.focus()
        chat_input.cursor_position = new_cursor

    def _activate_node_action(self, node_id: str, action: str) -> None:
        if not node_id or action not in ("favorite", "unfavorite"):
            return
        self.settings.set_favorite(node_id, action == "favorite")
        try:
            self.settings.save()
        except OSError as error:
            self._show_send_error(f"Could not save favorite: {error}")
            return
        for widget in self.query(ChatEntryWidget):
            if widget.entry.node_id and widget.entry.node_id.lower() == node_id.lower():
                widget.set_favorite(self.settings.is_favorite(node_id))
        self._refresh_mesh()
        self._close_user_menu()

    def _activate_user_action(
        self,
        origin: ChatEntryWidget,
        action: str,
    ) -> None:
        """Compatibility wrapper for the established CHAT action path."""
        node_id = origin.entry.node_id
        if node_id:
            self._activate_node_action(node_id, action)

    def _close_user_menu(self, *, restore_focus: bool = True) -> None:
        menu = self._user_menu
        origin = self._user_menu_origin
        scroll_target = self._user_menu_scroll_target
        scroll_x = self._user_menu_scroll_x
        scroll_y = self._user_menu_scroll_y
        self._user_menu = None
        self._user_menu_origin = None
        self._user_menu_scroll_target = None
        self._user_menu_scroll_x = None
        self._user_menu_scroll_y = None
        if menu is not None:
            menu.remove()
        if restore_focus and origin is not None and origin.is_mounted:
            origin.focus(scroll_visible=not isinstance(origin, MeshNodeWidget))
            if scroll_target is not None and scroll_y is not None:
                self.call_after_refresh(
                    scroll_target.scroll_to,
                    x=scroll_x,
                    y=scroll_y,
                    animate=False,
                    force=True,
                )

    def _open_emoji_picker(self) -> None:
        """Mount the emoji strip near the composer, sized to HUG its own

        content (see emoji_picker_total_width() -- computed from
        rendered cell widths, never len()), not stretched to the
        composer's full width. Positioned via the SAME
        calculate_popup_placement() the sender-action ViewportMenu
        already uses (see _open_node_menu) -- not hand-rolled
        arithmetic -- specifically because that function clamps BOTH
        axes against the real screen region: an earlier version of
        this method only clamped the WIDTH to the composer's own
        width, never the X-OFFSET against the screen's right edge,
        which could still let the picker's right border extend past
        the visible terminal on a real narrow/XL-font layout even
        though the width math itself was correct in isolation.
        Never moves Textual focus off the composer (see item 22):
        ChatMessageInput.on_key intercepts LEFT/RIGHT/ENTER/ESC
        directly on the still-focused composer while this is open (see
        that method's own docstring for why it -- not the App's
        on_key -- has to be the one to do this).
        """
        composer = self._active_chat_input()
        picker = EmojiPicker()
        self._emoji_picker = picker
        self.screen.mount(picker)
        palette = THEME_PALETTES[self._current_theme]
        picker.set_palette(palette.base, palette.accent)
        placement = calculate_popup_placement(
            composer.region,
            self.screen.region,
            emoji_picker_total_width(),
            EMOJI_PICKER_HEIGHT,
        )
        picker.styles.width = placement.width
        picker.styles.offset = (placement.x, placement.y)

    def _close_emoji_picker(self) -> None:
        picker = self._emoji_picker
        self._emoji_picker = None
        if picker is not None:
            picker.remove()

    def _insert_emoji_at_cursor(self, emoji: str) -> None:
        """Insert at the composer's CURRENT CURSOR POSITION -- draft

        text before and after is always preserved (see item 22).
        Relies on Textual's own Input.cursor_position/value indexing
        rather than reimplementing cursor math (see item 25).
        """
        composer = self._active_chat_input()
        draft = composer.value
        cursor = composer.cursor_position
        new_value = draft[:cursor] + emoji + draft[cursor:]
        new_cursor = cursor + len(emoji)
        composer.value = new_value
        composer.focus()
        composer.cursor_position = new_cursor

    def _active_chat_input(self) -> Input:
        """The Input widget currently acting as the chat/DM composer.

        CHANNEL CHAT uses #chat-input; a DM conversation (or the transient
        NEW DM entry surface, which also types into #dm-input) uses
        #dm-input. Choosing the WRONG one is exactly the bug that made the
        emoji menu opened from a DM position at / edit the channel CHAT
        composer instead -- the active conversation's identity is derived
        here from the current _chat_mode, never hardcoded.
        """
        if self._chat_mode == "dms":
            return self.query_one("#dm-input", Input)
        return self.query_one("#chat-input", Input)

    def _clear_indicator_if_at_bottom(self) -> None:
        if self._is_near_chat_bottom() and self.transcript_new_count:
            self._state_for(self.current_channel_index).new_below_ids.clear()
            self.transcript_new_count = 0
            self._update_transcript_indicator()

    def _update_transcript_indicator(self) -> None:
        label = (
            f"↓ {self.transcript_new_count} NEW"
            if self.transcript_new_count
            else ""
        )
        self.query_one("#chat-new-below", Static).update(label)

    def _mark_unread_messages_viewed(self) -> None:
        state = self._state_for(self.current_channel_index)
        for entry in self.chat_history:
            if entry.unread:
                entry.unread = False
                if entry.message_id is not None:
                    state.unread_message_ids.discard(entry.message_id)
        state.unread_count = 0

    def _mark_new_messages_read(self) -> None:
        self._mark_messages_read(
            entry for entry in self.chat_history if entry.is_new
        )

    @staticmethod
    def _entry_review_identity(entry: ChatEntry) -> tuple[str, int]:
        """Return a stable session identity, preferring the persisted row ID."""
        if entry.message_id is not None:
            return ("message", entry.message_id)
        return ("arrival", entry.arrival_order)

    def _clear_message_read_state(self, entry: ChatEntry) -> bool:
        """Apply the model portion of one read transition without rendering."""
        state = self._state_for(entry.channel_index)
        identity = self._entry_review_identity(entry)
        changed = entry.is_new or entry.unread or identity in state.pending_older_ids
        was_unread = entry.unread
        entry.is_new = False
        entry.unread = False
        if entry.message_id is not None:
            state.new_message_ids.discard(entry.message_id)
            state.unread_message_ids.discard(entry.message_id)
        state.pending_older_ids.discard(identity)
        state.new_below_ids.discard(identity)
        if was_unread and state.unread_count:
            state.unread_count -= 1
        return changed

    def _finish_read_transition(self, changed_entries: list[ChatEntry]) -> None:
        if not changed_entries:
            return
        changed_identity = {id(entry) for entry in changed_entries}
        if any(
            entry.channel_index == self.current_channel_index
            for entry in changed_entries
        ):
            for widget in self.query(ChatEntryWidget):
                if id(widget.entry) in changed_identity:
                    widget.refresh_new_message_state()
            state = self._state_for(self.current_channel_index)
            self.transcript_new_count = len(state.new_below_ids)
            state.transcript_new_count = self.transcript_new_count
            self._update_transcript_indicator()
            self._render_chat_status()
        self._recount_unread()
        self._update_tab_bar()

    def _mark_message_read(self, entry: ChatEntry) -> None:
        """Acknowledge exactly one entry selected by the user."""
        changed = self._clear_message_read_state(entry)
        self._finish_read_transition([entry] if changed else [])

    def _mark_messages_read(self, entries: Iterable[ChatEntry]) -> None:
        """Apply the same read transition to an existing broader read flow."""
        changed = [
            entry
            for entry in entries
            if self._clear_message_read_state(entry)
        ]
        self._finish_read_transition(changed)

    def _mark_current_channel_read_for_send(self) -> None:
        """Sending proves the current conversation was actively viewed."""
        self._mark_unread_messages_viewed()
        self._mark_new_messages_read()
        self._recount_unread()
        self._update_tab_bar()

    def _recount_unread(self) -> None:
        self.unread_count = sum(
            state.unread_count for state in self._channel_states.values()
        )

    def _load_chat_history(self) -> None:
        state = self._restore_channel_state(self.current_channel_index)
        transcript = self.query_one("#chat-log", ChatTranscript)
        widgets = self._initial_chat_widgets(self.current_channel_index, state)
        if widgets:
            transcript.mount(*widgets)

    @on(LoadOlderControl.Activated)
    async def load_older_chat_history(
        self,
        _event: LoadOlderControl.Activated,
    ) -> None:
        if self.chat_store is None or not self._has_older_history:
            return
        oldest_id = self._oldest_persisted_message_id()
        if oldest_id is None:
            return
        try:
            page = self.chat_store.load_older_page(
                oldest_id,
                channel_index=self.current_channel_index,
                limit=OLDER_HISTORY_PAGE_SIZE,
                channel_key=self._channel_key_for(self.current_channel_index),
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))
            return
        if not page.messages:
            self._has_older_history = False
            control = self.query_one("#load-older", LoadOlderControl)
            await control.remove()
            transcript = self.query_one("#chat-log", ChatTranscript)
            first_widget = next(iter(transcript.query(ChatEntryWidget)), None)
            if first_widget is not None:
                await transcript.mount(EndOfChatHistoryMarker(), before=first_widget)
            self._capture_current_channel_state()
            return

        transcript = self.query_one("#chat-log", ChatTranscript)
        old_scroll_y = transcript.scroll_y
        old_virtual_height = transcript.virtual_size.height
        first_widget = next(iter(self.query(ChatEntryWidget)), None)
        entries = [stored_chat_entry(stored) for stored in page.messages]
        state = self._state_for(self.current_channel_index)
        for entry in entries:
            if entry.message_id in state.new_message_ids:
                entry.is_new = True
            if entry.message_id in state.unread_message_ids:
                entry.unread = True
        widgets = [self._chat_entry_widget(entry) for entry in entries]
        self.chat_history[0:0] = entries
        self._mounted_chat_target += len(entries)
        await transcript.mount(*widgets, before=first_widget)
        self._has_older_history = page.has_older
        if not page.has_older:
            control = self.query_one("#load-older", LoadOlderControl)
            await control.remove()
            first_loaded_widget = widgets[0]
            await transcript.mount(
                EndOfChatHistoryMarker(),
                before=first_loaded_widget,
            )
        self._capture_current_channel_state()
        self.call_after_refresh(
            self._restore_scroll_after_prepend,
            transcript,
            old_scroll_y,
            old_virtual_height,
        )

    @staticmethod
    def _restore_scroll_after_prepend(
        transcript: ChatTranscript,
        old_scroll_y: float,
        old_virtual_height: int,
    ) -> None:
        added_height = transcript.virtual_size.height - old_virtual_height
        transcript.scroll_to(
            y=max(0, old_scroll_y + added_height),
            animate=False,
            force=True,
        )

    def _oldest_persisted_message_id(self) -> int | None:
        return next(
            (
                entry.message_id
                for entry in self.chat_history
                if entry.message_id is not None
            ),
            None,
        )

    def _persist_incoming(self, entry: ChatEntry) -> bool:
        if self.chat_store is None:
            return True
        try:
            result = self.chat_store.add_incoming(
                packet_id=entry.packet_id,
                node_id=entry.node_id or "unknown",
                sender_name=entry.sender_name,
                sender_short_name=entry.sender_short_name,
                channel_index=entry.channel_index,
                text=entry.text,
                origin_sent_at=entry.origin_sent_at,
                radio_rx_at=entry.radio_rx_at,
                received_at=entry.app_received_at,
                dm_node_id=entry.dm_node_id,
                channel_key=(
                    None
                    if entry.dm_node_id
                    else self._channel_key_for(entry.channel_index)
                ),
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))
            return True
        entry.message_id = result.message_id
        return result.inserted

    def _persist_outgoing(self, entry: ChatEntry) -> None:
        if self.chat_store is None:
            return
        try:
            entry.message_id = self.chat_store.add_outgoing(
                text=entry.text,
                channel_index=entry.channel_index,
                local_sent_at=entry.local_sent_at or entry.app_received_at,
                delivery_state=(entry.delivery_state or DeliveryState.SENDING).value,
                dm_node_id=entry.dm_node_id,
                channel_key=(
                    None
                    if entry.dm_node_id
                    else self._channel_key_for(entry.channel_index)
                ),
            )
            entry.active_attempt_id = self.chat_store.add_send_attempt(
                entry.message_id,
                time(),
                (entry.delivery_state or DeliveryState.SENDING).value,
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))

    def _persist_new_attempt(self, entry: ChatEntry) -> None:
        if self.chat_store is None or entry.message_id is None:
            entry.active_attempt_id = None
            return
        try:
            entry.active_attempt_id = self.chat_store.add_send_attempt(
                entry.message_id,
                time(),
            )
            self.chat_store.update_delivery_state(
                entry.message_id,
                DeliveryState.SENDING.value,
                attempt_id=entry.active_attempt_id,
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))

    def _set_delivery_state(
        self,
        entry: ChatEntry,
        state: DeliveryState,
        *,
        packet_id: int | None = None,
        detail: str = "",
    ) -> None:
        previous_state = entry.delivery_state
        entry.delivery_state = state
        # A manual resend (send_generation > 1) that actually,
        # successfully re-enters the mesh moves the message to its new
        # chronological position -- see _update_effective_transmission_
        # time. Gated on the OLD state being SENDING specifically so
        # this fires exactly once per resend generation, at the
        # SENDING -> SENT/HEARD transition only: a later SENT -> HEARD
        # acknowledgement (previous_state is already SENT here, not
        # SENDING) never re-triggers it, and an ordinary first attempt
        # (send_generation == 1) is never affected at all.
        if (
            entry.send_generation > 1
            and previous_state is DeliveryState.SENDING
            and state in (DeliveryState.SENT, DeliveryState.HEARD)
        ):
            self._update_effective_transmission_time(entry)
        if packet_id is not None:
            entry.packet_id = packet_id
        # Delivery-state monotonicity (see _advance_delivery_states):
        # the confirmation-timeout deadline exists ONLY to eventually
        # produce UNCONFIRMED for a send that never got ANY conclusive
        # evidence. The instant a send reaches SENT/HEARD/FAILED, it
        # already IS conclusive -- the deadline is retired immediately
        # so a late-firing timeout tick can never downgrade it. A later,
        # genuinely STRONGER ack (SENT -> HEARD, or a real ack promoting
        # an already-retired UNCONFIRMED -- see delivery_status_received)
        # is a completely separate path from this timeout and is never
        # blocked by retiring the deadline here.
        if state is not DeliveryState.SENDING:
            entry.confirmation_deadline = None
        completed_at = (
            time()
            if state in (
                DeliveryState.HEARD,
                DeliveryState.UNCONFIRMED,
                DeliveryState.FAILED,
            )
            else None
        )
        if self.chat_store is not None and entry.message_id is not None:
            try:
                self.chat_store.update_delivery_state(
                    entry.message_id,
                    state.value,
                    attempt_id=entry.active_attempt_id,
                    packet_id=entry.packet_id,
                    error=detail or None,
                    completed_at=completed_at,
                )
            except ChatStoreError as error:
                self._show_send_error(str(error))
        for widget in self.query(ChatEntryWidget):
            if widget.entry is entry:
                widget.refresh_delivery_state(self._send_animation_frame)
                break
        self._update_footer()

    def _update_effective_transmission_time(self, entry: ChatEntry) -> None:
        """Move `entry` to reflect WHEN it actually, successfully

        retransmitted -- not when the user originally composed it (see
        item 3, MANUAL RESEND SHOULD UPDATE CHAT CHRONOLOGY). Only
        `local_sent_at` (ChatEntry.message_time/order_key's source for
        an outgoing entry, and the persisted column of the same name)
        changes; the message keeps its identity, message_id, and every
        send_attempt row exactly as before -- nothing is deleted,
        recreated, or duplicated. `created_at` (persisted separately,
        never touched here) still truthfully answers "when was this
        message first composed", regardless of how many attempts it
        took to actually land.

        `age_reference` is updated too so the DISPLAYED elapsed age
        reflects the same new moment. It is never itself persisted (see
        ChatEntry.age_reference / stored_chat_entry) -- a fresh restart
        recomputes it correctly from the persisted local_sent_at alone,
        so this in-memory update only matters for the current session.
        """
        wall_now = time()
        entry.local_sent_at = wall_now
        entry.age_reference = monotonic()
        if self.chat_store is not None and entry.message_id is not None:
            try:
                self.chat_store.update_message_chronology(entry.message_id, wall_now)
            except ChatStoreError as error:
                self._show_send_error(str(error))
        if entry.dm_node_id is not None:
            self._reposition_dm_entry(entry)
        else:
            self._reposition_chat_entry(entry)

    def _reposition_chat_entry(self, entry: ChatEntry) -> None:
        """Move `entry` (and its mounted widget, if any) to the list

        position its current order_key now implies, without destroying
        or recreating the widget -- so any live selection/focus on it
        (see Widget.move_child) survives the move exactly. Manual resend
        is only ever offered on an already-visible, currently-mounted
        message (see can_manual_resend/_rebroadcast's callers), so
        `entry` is always a member of the CURRENT channel's
        self.chat_history when this runs.
        """
        if entry not in self.chat_history:
            return
        self.chat_history.remove(entry)
        new_index = self._insert_entry_in_order(self.chat_history, entry)
        if self.current_tab != "chat" or self._chat_mode != "channel":
            return
        widget = next(
            (candidate for candidate in self.query(ChatEntryWidget) if candidate.entry is entry),
            None,
        )
        if widget is None:
            return
        transcript = self.query_one("#chat-log", ChatTranscript)
        following = self._following_chat_widget(new_index)
        if following is widget:
            return
        if following is not None:
            transcript.move_child(widget, before=following)
            return
        others = [
            candidate
            for candidate in transcript.query(ChatEntryWidget)
            if candidate is not widget
        ]
        if others:
            transcript.move_child(widget, after=others[-1])

    def _rebroadcast(self, entry: ChatEntry) -> None:
        if not can_manual_resend(entry):
            return
        entry.delivery_state = DeliveryState.SENDING
        entry.send_generation += 1
        entry.packet_id = None
        entry.confirmation_deadline = None
        self._persist_new_attempt(entry)
        self._set_delivery_state(entry, DeliveryState.SENDING)
        self._show_send_error("")
        generation = entry.send_generation
        self.run_worker(
            lambda: self._send_from_thread(entry, generation),
            thread=True,
        )

    @on(ChatEntryWidget.SelectionChanged)
    def chat_selection_changed(
        self,
        event: ChatEntryWidget.SelectionChanged,
    ) -> None:
        if event.selected:
            self._mark_message_read(event.widget.entry)
        self._update_footer()

    def _update_footer(self) -> None:
        if self.current_tab == "chat" and isinstance(self.focused, Input):
            # Matches the UPPERCASE-HOTKEY + lowercase-descriptor
            # grammar every other footer line below already uses.
            # CTRL+E (not a bare "E") is the real emoji-picker binding
            # (see on_key's ctrl+e handling) -- printable "e" must stay
            # typeable while the composer is focused, so the label
            # reflects the actual binding rather than a shorter one
            # that would collide with typing. #chat-input and #dm-input
            # are both plain Input widgets on this tab, so this single
            # branch already covers CHANNEL and DM identically.
            text = "CTRL+E emojis    ESC cancel    ENTER send"
        elif self.current_tab == "chat" and self._chat_mode == "channel":
            text = "↑↓ navigate    C channel    D dms    ENTER action    F4 quit"
        elif self.current_tab == "chat" and self.current_dm_node_id is None:
            text = "↑↓ select    ENTER open    C channel    1-3 tabs    F4 quit"
        elif self.current_tab == "chat":
            text = "↑↓ navigate    C channel    ENTER action    CTRL+D delete    ESC back    F4 quit"
        else:
            text = (
                "↑↓←→ select    1-3 tabs    F4 quit"
                if self.current_tab == "mesh"
                else "1-3 switch tabs    F4 quit"
            )
        self.query_one("#footer", Static).update(text)

    def restore_terminal_cursor(self) -> None:
        """Restore the host terminal cursor; safe to call repeatedly."""
        self._terminal_cursor.restore()

    def _show_connection(
        self,
        state: RadioState,
        info: RadioInfo | None = None,
        message: str = "",
    ) -> None:
        # Captured before overwriting self._radio_state -- the ONLY
        # signal AUTO SYNC uses to detect "a NEW connection lifecycle
        # just began" (see _maybe_auto_sync_clock/item 17): an
        # already-ONLINE radio calling this again (e.g. a redundant
        # event) must never re-trigger a sync.
        was_online = self._radio_state is RadioState.ONLINE
        self._radio_state = state
        self._radio_info = info if state is RadioState.ONLINE else None
        # TRACE ROUTE (Part C): a disconnect (for any reason -- dropped
        # connection, explicit device-path change, radio swap) leaves
        # NOTHING behind to genuinely resolve a pending trace against
        # (the interface object generating any real response is gone) --
        # cancelled immediately rather than left to eventually expire via
        # its own TRACEROUTE_TIMEOUT_SECONDS timer, so "TRACING ROUTE"
        # never lingers stale through a visible reconnect. Silent (no
        # TRACE FAILED banner): the disconnect itself is already
        # communicated elsewhere (#mesh-connection-status).
        if state is not RadioState.ONLINE and self._active_traceroute is not None:
            self._clear_active_traceroute()
            # Explicit blank (never left showing the just-cancelled
            # "TRACING ROUTE..." text): _refresh_mesh's own status.update()
            # calls are unreachable while not ONLINE (see its own
            # early-return), so nothing else would ever clear this stale
            # text otherwise. _mesh_status_override_text() is still
            # consulted -- if a banner somehow ALSO exists, it wins
            # (never true in practice: starting a trace already clears
            # any banner -- see _start_traceroute -- so this branch
            # never actually observes both set at once).
            widgets = list(self.query("#mesh-status"))
            if widgets:
                override = self._mesh_status_override_text()
                widgets[0].update(override if override is not None else "")
        # Radio-swap safety (item 28): a reconnect (or a drop) that
        # changes -- or clears -- the current local SHORTNAME must
        # update already-mounted CHAT entries' mention highlighting
        # immediately, never leave a PREVIOUS radio's stale identity
        # highlighted or a NEWLY-matching one dark.
        self._refresh_chat_mentions()
        # A genuine connection-state transition (anything except a
        # redundant "still ONLINE" event) must never leave an AUTO
        # SYNC write in flight/attributed to the OLD connection for the
        # NEW one to inherit -- see _reset_clock_sync_state's own
        # docstring.
        if not (state is RadioState.ONLINE and was_online):
            self._reset_clock_sync_state()
        if state is RadioState.ONLINE and info is not None and info.channels:
            self._invalidate_reassigned_channel_caches(info.channels)
            self._channels = info.channels
            selector = self.query_one(ChannelSelector)
            available_indexes = {channel.index for channel in self._channels}
            selected_index = self.current_channel_index
            if selected_index not in available_indexes:
                selected_index = self._channels[0].index
            selector.set_options(
                (
                    DropdownOption(channel.name, channel.index)
                    for channel in self._channels
                ),
                value=selected_index,
            )
            if selected_index != self.current_channel_index:
                self.run_worker(
                    self._switch_channel(selected_index),
                    name="select-available-channel",
                )
            else:
                # selected_index == current_channel_index tells us
                # nothing about whether the CHANNEL at that index is
                # still the same one -- a same-slot reconfiguration
                # (see CHAT channel-history isolation) needs its own
                # check here.
                self.run_worker(
                    self._reconcile_current_channel_identity(),
                    name="reconcile-current-channel-identity",
                )
        self._refresh_device_options()
        self._status_dot_count = 1
        if self._connection_animation_timer is not None:
            if state is RadioState.ONLINE:
                self._connection_animation_timer.pause()
            else:
                self._connection_animation_timer.resume()
        self._set_long_name_status("", None)
        self._set_short_name_status("", None)
        self._set_timezone_status("", None)
        self._set_role_status("", None)
        self._render_connection_details()
        self._render_identity(force_value=True)
        self._render_radio_settings()
        self._on_connection_state_for_network_apply(state, was_online)
        # RADIO-AUTHORITATIVE PRESET DETECTION: after every genuine
        # (re)connect whose full config sync just completed, derive the
        # active PRESET from the actual radio state (read-only, zero
        # writes) -- including right after a NETWORK apply resolved on
        # this very reconnect (the call above may have finished it), so
        # the selector always reflects the radio, not an assumption.
        # Skipped while an apply is STILL in flight: its own
        # verification lifecycle owns the selector then.
        if (
            state is RadioState.ONLINE
            and not was_online
            and self._network_apply is None
        ):
            self._detect_active_network_from_radio()
        self._refresh_mesh()
        self.query_one("#connection-error", Static).update("")
        self._update_chat_connection_state()
        if state is RadioState.ONLINE and not was_online:
            self._clock_auto_sync_done_this_connection = False
            self._maybe_auto_sync_clock()

    def _connection_status_color(self) -> str:
        """The semantic color for the current non-ONLINE radio state --

        the SAME mapping CONNECTION/CONFIG's own status row uses (see
        _render_connection_details, which calls this too rather than
        duplicating the ternary): ERROR for RadioState.ERROR, ACCENT for
        every other non-ONLINE state (CONNECTING, OFFLINE). CHAT (see
        _update_chat_connection_state) and MESH (same function, which
        writes #mesh-connection-status) both reuse this exact value via
        _connection_status_rich_text(), so all three surfaces always
        agree on what a given radio state visually means -- one
        authoritative color decision, never three independent ones.
        Uses the current theme's own semantic tokens (never a hardcoded
        literal color), so it stays correct across WHITE/GREEN/ORANGE.
        Meaningless while ONLINE; callers only use this alongside
        non-empty status text, which _connection_status_rich_text()
        only ever produces when not ONLINE.
        """
        palette = THEME_PALETTES[self._current_theme]
        return palette.error if self._radio_state is RadioState.ERROR else palette.accent

    def _connection_status_rich_text(self) -> Text | None:
        """The single authoritative connection-status line CHAT and MESH

        show at the top of their view while the radio isn't ONLINE, styled
        component-level: the "STATUS" word in BASE, the animated state
        word (CONNECTING..., OFFLINE -- RETRYING..., ...) in its own
        semantic color from _connection_status_color() -- the same
        label-in-BASE/value-in-its-own-style grammar
        _render_connection_details() already uses for CONNECTION/
        CONFIG's own status row, reused verbatim here so CHAT's heading
        and MESH's status line always agree with it at the SPAN level,
        not merely "some color somewhere in the string" (see
        _update_chat_connection_state, this function's only caller).
        Built from the EXACT SAME ANIMATED_STATUS mapping and
        _status_dot_count animation counter CONNECTION/CONFIG's own
        status row already uses -- never a tab-specific reinterpretation.
        There is no separate RECONNECTING state in RadioState -- a
        dropped connection re-enters RadioState.CONNECTING exactly like
        the first attempt (see radio_service.connection_events()), so
        both report identically here too. Returns None when ONLINE
        (nothing to show; callers should hide/restore their normal
        presentation).
        """
        if self._radio_state is RadioState.ONLINE:
            return None
        palette = THEME_PALETTES[self._current_theme]
        text = Text()
        text.append("STATUS ", style=palette.base)
        text.append(
            ANIMATED_STATUS[self._radio_state] + "." * self._status_dot_count,
            style=self._connection_status_color(),
        )
        return text

    def _update_chat_connection_state(self) -> None:
        """Keep CHAT's connection-status presentation in sync with the

        authoritative self._radio_state (see _show_connection) --
        purely observational, never triggers a new connection attempt
        or any other radio traffic on its own.

        Disables CHAT message entry/sending while the radio isn't
        ONLINE (Textual's `disabled` only affects focus/interaction, so
        a draft typed before a drop survives completely untouched and
        is exactly what the user sees once connection is restored).
        Also overrides CHAT's channel heading, and -- while the radio
        isn't ONLINE -- writes that exact same text to MESH's own status
        line (#mesh-connection-status) in this SAME call, so the two can
        never show different animation-dot phases (see
        _advance_connection_animation, which calls this on a fixed
        ~0.45s cadence). Once ONLINE (status_text == ""), this leaves
        that widget untouched -- _update_mesh_node_bar (called from
        _refresh_mesh) force-hides it instead, unconditionally, since it
        has no ONLINE-state purpose any more (the unified bottom bar now
        lives in the separate #mesh-node-bar widget). The two never fight
        over ownership: this function only ever writes while NOT ONLINE,
        that one only ever touches the widget while ONLINE.
        """
        chat_inputs = list(self.query("#chat-input"))
        if chat_inputs:
            chat_inputs[0].disabled = self._radio_state is not RadioState.ONLINE
        dm_inputs = list(self.query("#dm-input"))
        if dm_inputs:
            dm_inputs[0].disabled = self._radio_state is not RadioState.ONLINE
        self._render_chat_status()

        selectors = list(self.query(ChannelSelector))
        if selectors:
            selectors[0].set_status_override(self._connection_status_rich_text())

        status_rich_text = self._connection_status_rich_text()
        online = status_rich_text is None
        # RECONNECT DELIVERY + CHAT HEADER FIX item 11: while not
        # ONLINE, CHAT's header must show ONLY the connection-status
        # text above (already handled by ChannelSelector's own
        # set_status_override) -- the separator bullet and DM selector
        # must not remain visible/interactive alongside it. Both are
        # pure presentation toggles: value/options/unread count are
        # never touched, so the normal header reappears exactly as it
        # was the instant ONLINE returns (item 12/13), with no tab
        # switch/manual refresh required -- this method already runs on
        # every connection-state transition (see _show_connection) and
        # every ~0.45s while not ONLINE (see
        # _advance_connection_animation).
        bullets = list(self.query("#chat-header-bullet"))
        if bullets:
            bullets[0].display = online
        dm_selectors = list(self.query(DMModeSelector))
        if dm_selectors:
            dm_selector = dm_selectors[0]
            was_focused = self.focused is dm_selector
            if not online and dm_selector.is_open:
                dm_selector.close_menu()
            # disabled (not just display=False) so Textual's own
            # dropdown/key handling can never treat it as interactive
            # even if something reaches it directly (belt-and-suspenders
            # alongside the "d" hotkey's own ONLINE check below, item
            # 16) -- and so a stray Tab press cannot land focus back on
            # a hidden widget while not ONLINE.
            dm_selector.disabled = not online
            dm_selector.display = online
            if not online and was_focused:
                # Item 15: never leave focus stranded on a now-hidden
                # widget -- land on the SAME neutral per-mode target
                # C/D/ESC already use elsewhere, never a dropdown.
                self._focus_chat_mode(self._chat_mode)
        dm_status_widgets = list(self.query("#dm-connection-status"))
        if status_rich_text is not None:
            mesh_status_widgets = list(self.query("#mesh-connection-status"))
            if mesh_status_widgets:
                widget = mesh_status_widgets[0]
                widget.update(status_rich_text)
                widget.display = True
            if dm_status_widgets:
                widget = dm_status_widgets[0]
                widget.update(status_rich_text)
                widget.display = True
        elif dm_status_widgets:
            dm_status_widgets[0].display = False

    def _advance_connection_animation(self) -> None:
        if self._radio_state is RadioState.ONLINE:
            return
        self._status_dot_count = self._status_dot_count % 3 + 1
        self._render_connection_details()
        self._update_chat_connection_state()

    def _render_connection_details(self) -> None:
        statuses = list(self.query("#connection-status"))
        detail_widgets = list(self.query("#connection-details"))
        if not statuses or not detail_widgets:
            # A final timer tick may race with Textual dismantling the screen.
            return
        status = (
            "CONNECTED"
            if self._radio_state is RadioState.ONLINE
            else ANIMATED_STATUS[self._radio_state] + "." * self._status_dot_count
        )
        palette = THEME_PALETTES[self._current_theme]
        status_style = (
            palette.accent
            if self._radio_state is RadioState.ONLINE
            else self._connection_status_color()
        )
        status_text = Text()
        status_text.append(
            f"{CONNECTION_ROW_PREFIX}{'STATUS':<{CONNECTION_LABEL_WIDTH}}",
            style=palette.base,
        )
        status_text.append(" ", style=palette.base)
        status_text.append(status, style=status_style)
        statuses[0].update(status_text)
        values = []
        if self._radio_state is RadioState.ONLINE and self._radio_info is not None:
            info = self._radio_info
            values.extend(
                [
                    ("NODES", str(info.known_nodes)),
                ]
            )
        if values:
            details = Text()
            for index, (label, value) in enumerate(values):
                if index:
                    details.append("\n")
                details.append(
                    f"{CONNECTION_ROW_PREFIX}{label:<{CONNECTION_LABEL_WIDTH}}",
                    style=palette.base,
                )
                details.append(" ", style=palette.base)
                details.append(value, style=palette.base)
            detail_widgets[0].update(details)
        else:
            placeholder = "..." if self._radio_state is RadioState.CONNECTING else "—"
            details = Text()
            details.append(
                f"{CONNECTION_ROW_PREFIX}{'NODES':<{CONNECTION_LABEL_WIDTH}}",
                style=palette.base,
            )
            details.append(" ", style=palette.base)
            details.append(placeholder, style=palette.dim_base)
            detail_widgets[0].update(details)

    def _render_identity(self, force_value: bool = False) -> None:
        long_name = self.query_one(LongNameControl)
        short_name = self.query_one(ShortNameControl)
        values = self.query_one("#identity-values", Static)
        online = self._radio_state is RadioState.ONLINE and self._radio_info is not None
        palette = THEME_PALETTES[self._current_theme]
        if online:
            info = self._radio_info
            assert info is not None
            long_name.set_available(info.long_name, force_value=force_value)
            short_name.set_available(info.short_name, force_value=force_value)
            identity_text = Text()
            identity_text.append(
                f"{CONNECTION_ROW_PREFIX}{'NODE ID':<{CONNECTION_LABEL_WIDTH}}",
                style=palette.base,
            )
            identity_text.append(" ", style=palette.base)
            identity_text.append(info.node_id, style=palette.base)
            values.update(identity_text)
        else:
            connecting = self._radio_state is RadioState.CONNECTING
            placeholder = "..." if connecting else "—"
            long_name.set_unavailable(placeholder)
            short_name.set_unavailable(placeholder)
            identity_text = Text()
            identity_text.append(
                f"{CONNECTION_ROW_PREFIX}{'NODE ID':<{CONNECTION_LABEL_WIDTH}}",
                style=palette.base,
            )
            identity_text.append(" ", style=palette.base)
            identity_text.append(placeholder, style=palette.dim_base)
            values.update(identity_text)

    def _show_send_error(self, message: str) -> None:
        self._send_error_message = message
        # Replace, never accumulate, the auto-dismiss timer: any earlier
        # attempt's callback is stopped outright before a new one (if
        # any) is scheduled, so a stale timer can never later clear a
        # newer, unrelated status -- there is only ever at most one
        # pending dismissal for this widget at a time.
        if self._send_error_dismiss_timer is not None:
            self._send_error_dismiss_timer.stop()
            self._send_error_dismiss_timer = None
        if message:
            self._send_error_dismiss_timer = self.set_timer(
                SEND_ERROR_AUTO_DISMISS_SECONDS, self._auto_dismiss_send_error
            )
        self._render_chat_status()

    def _auto_dismiss_send_error(self) -> None:
        self._send_error_dismiss_timer = None
        self._show_send_error("")

    def _show_older_message_notice(
        self,
        channel_index: int,
        entry: ChatEntry,
    ) -> None:
        state = self._state_for(channel_index)
        state.pending_older_ids.add(self._entry_review_identity(entry))
        if channel_index == self.current_channel_index:
            self._render_chat_status()

    def _clear_older_message_notice(self, channel_index: int) -> None:
        self._state_for(channel_index).pending_older_ids.clear()
        self._render_chat_status()

    def _render_chat_status(self) -> None:
        """Send-error / older-message notice only -- connection status

        lives solely in CHAT's top heading now (see
        _update_chat_connection_state/_connection_status_rich_text), never
        duplicated here. Message entry is disabled while not ONLINE
        (see _update_chat_connection_state), so this renders whatever
        it normally would regardless of connection state -- there is
        nothing connection-specific left for it to say.
        """
        widgets = list(self.query("#send-error"))
        if not widgets:
            return
        widget = widgets[0]
        state = self._state_for(self.current_channel_index)
        count = len(state.pending_older_ids)
        show_notice = not self._send_error_message and count > 0
        widget.set_class(show_notice, "older-message-notice")
        if self._send_error_message:
            widget.update(self._send_error_message)
        elif show_notice:
            noun = "MESSAGE" if count == 1 else "MESSAGES"
            widget.update(f"{count} OLDER {noun} RECEIVED")
        else:
            widget.update("")

    def _update_tab_bar(
        self,
        wall_now: float | None = None,
        *,
        mesh_working_set: tuple[MeshNodeState, ...] | None = None,
    ) -> None:
        tab_bars = list(self.query("#tab-bar"))
        if not tab_bars:
            # A final timer tick may race with Textual dismantling the screen.
            return
        labels = []
        for number, (tab_id, name) in enumerate(TAB_NAMES.items(), start=1):
            if tab_id == "chat" and self.unread_count:
                name = f"{name}({self.unread_count})"
            elif tab_id == "mesh":
                count = self._mesh_active_count(wall_now, working_set=mesh_working_set)
                name = f"{name}({count})"
            label = f"[{number}] {name}"
            labels.append(f"[reverse]{label}[/reverse]" if tab_id == self.current_tab else label)
        tab_bars[0].update("   ".join(labels))

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("[", "\\[")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the MeshtasticPass terminal UI")
    parser.add_argument("--device", default=None, help="override saved serial device")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use the deterministic radio simulator",
    )
    parser.add_argument(
        "--simulate-send-outcome",
        action="append",
        choices=[outcome.value for outcome in SimulatedSendOutcome],
        default=[],
        help="script each simulated send result (may be repeated)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = AppSettings.load()
    device_path = args.device or settings.device_path
    if args.simulate and args.simulate_send_outcome:
        radio = SimulatedRadioService(
            device_path=device_path,
            send_outcomes=tuple(
                SimulatedSendOutcome(value)
                for value in args.simulate_send_outcome
            )
        )
    else:
        radio = create_radio_service(args.simulate, device_path)
    history_error = ""
    try:
        chat_store = ChatStore.open()
    except ChatStoreError as error:
        chat_store = None
        history_error = str(error)
    app = MeshtasticPassApp(
        radio,
        settings,
        chat_store=chat_store,
        history_error=history_error,
    )
    try:
        app.run()
    finally:
        app.restore_terminal_cursor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
