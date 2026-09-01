"""Persistent user settings for MeshtasticPass."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
from typing import Any


FONT_SIZE_CHOICES = (
    ("SMALL", 11),
    ("MEDIUM", 13),
    ("LARGE", 16),
    ("XL", 18),
    ("XXL", 22),
)
VALID_FONT_SIZES = tuple(size for _name, size in FONT_SIZE_CHOICES)
# LARGE, not MEDIUM: this is the fallback used only when no saved
# font_size preference exists at all (missing file, unreadable file, or
# a file with no valid font_size key) -- see AppSettings.load. An
# existing user's already-saved SMALL/MEDIUM/LARGE/XL/XXL choice always
# wins, since load() only ever falls back to this constant, never
# overwrites a value found in the config file. The user-facing label
# for this setting is "UI SCALE" (see app.py's FontSizeSelector); the
# underlying "font_size" config key/JSON field name is kept as-is for
# backward compatibility with already-saved settings files.
DEFAULT_FONT_SIZE = 16
COLOR_CHOICES = (
    ("SNOW", "snow"),
    ("AMBER", "amber"),
)
VALID_COLORS = tuple(value for _name, value in COLOR_CHOICES)
DEFAULT_COLOR = "snow"
# Deterministic migration for the three retired theme names (see the
# theme-overhaul completion report for the full reasoning): "white" ->
# "snow" and "orange" -> "amber" both keep their BASE hue. "green" has
# no base-hue equivalent in the new two-theme palette -- SNOW is the
# only remaining palette that still contains that exact neon green at
# all (as its ACCENT, see theme_palette.THEME_PALETTES), so a "green"
# theme user keeps seeing that same green prominently rather than
# losing it outright under "orange"/AMBER, which shares nothing with
# it. Never discards a color preference outright -- every legacy value
# maps to a valid current one.
_LEGACY_COLOR_MIGRATION = {
    "white": "snow",
    "green": "snow",
    "orange": "amber",
}
DEFAULT_DEVICE_PATH = "/dev/ttyUSB0"


def default_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


@dataclass
class RadioConfigPreset:
    """One locally saved radio/network configuration (ADVANCED RADIO

    CONFIG). Deliberately narrow: a user-visible name, the Meshtastic
    MODEM PRESET, a frequency slot, and the PRIMARY channel's name/key
    -- exactly the fields a saved "network" identity needs. HOP LIMIT
    is intentionally excluded: it stays the existing, independent
    RADIO-section HopLimitSelector field, never folded into a saved
    network preset. Raw bandwidth/spread_factor/coding_rate are also
    excluded -- those are derived from `modem_preset` in PRESET mode
    when applying (see radio_service.apply_radio_config_preset), never
    stored or written manually here.

    This class holds untrusted, unvalidated user input as-is (mirrors
    favorite_node_ids' own "just a string" scope) -- validating
    `modem_preset` against the installed Meshtastic protobuf schema is
    the caller's job (see radio_capabilities.modem_preset_choices),
    never this module's: app_settings.py has no Meshtastic SDK
    dependency at all, by design, and this preset is equally valid
    persisted data whether or not that package happens to be
    installed right now.
    """

    name: str
    modem_preset: str
    # LoRaConfig.channel_num -- 0 means "not set" (Meshtastic's own
    # "let the radio auto-select a channel" sentinel), never a
    # fabricated default frequency slot.
    frequency_slot: int = 0
    # The primary channel's ChannelSettings.name -- "" is a legitimate,
    # real-hardware value (an unnamed/default primary channel), never
    # coerced to a placeholder string.
    channel_name: str = ""
    # Canonical representation of ChannelSettings.psk: standard base64
    # TEXT (e.g. "AQ==" for the SDK's own default-channel-psk sentinel
    # byte 0x01) -- the SAME string end-to-end from the KEY UI field to
    # this persisted value; only the actual radio-write boundary
    # (radio_service.apply_radio_config_preset) ever decodes this to
    # raw bytes. "" means "not set".
    channel_psk_base64: str = ""


def _radio_config_preset_from_dict(raw: Any) -> RadioConfigPreset | None:
    """Tolerantly parse one saved preset entry -- an unrecognized/

    malformed entry is skipped (returns None) rather than corrupting
    the whole load or crashing the app (see AppSettings.load's own
    "never destroy existing settings" contract).
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    modem_preset = raw.get("modem_preset")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(modem_preset, str) or not modem_preset.strip():
        return None
    frequency_slot = raw.get("frequency_slot", 0)
    if isinstance(frequency_slot, bool) or not isinstance(frequency_slot, int):
        frequency_slot = 0
    channel_name = raw.get("channel_name", "")
    if not isinstance(channel_name, str):
        channel_name = ""
    channel_psk_base64 = raw.get("channel_psk_base64", "")
    if not isinstance(channel_psk_base64, str):
        channel_psk_base64 = ""
    return RadioConfigPreset(
        name=name.strip(),
        modem_preset=modem_preset.strip(),
        frequency_slot=frequency_slot,
        channel_name=channel_name,
        channel_psk_base64=channel_psk_base64,
    )


@dataclass
class AppSettings:
    """Load, validate, and save the small user-facing settings file."""

    font_size: int = DEFAULT_FONT_SIZE
    color: str = DEFAULT_COLOR
    device_path: str = DEFAULT_DEVICE_PATH
    favorite_node_ids: set[str] = field(default_factory=set)
    # Canonical ChannelInfo stable identities (see RadioService
    # _channel_stable_key) of configured channels the user has locally
    # deleted/hidden from the CHAT list via CTRL+D. NEVER a slot index,
    # display name, or PSK hash alone -- only the collapsed, contiguous
    # list of stored identities with a leading "_" like "cafeface".
    hidden_channel_ids: set[str] = field(default_factory=set)
    # A MeshtasticPass-local behavior preference, never a radio config
    # field (see RadioService.sync_clock/SyncClockControl) -- OFF until
    # the user explicitly turns it on; never silently enabled by a
    # default-on migration or an unrelated setting.
    clock_auto_sync: bool = False
    # ADVANCED RADIO CONFIG: locally saved network/radio configurations
    # -- never auto-applied on load/connect (see radio_service.
    # apply_radio_config_preset's own callers, always an explicit user
    # APPLY action).
    radio_config_presets: list[RadioConfigPreset] = field(default_factory=list)
    config_path: Path = field(
        default_factory=lambda: default_config_home()
        / "meshtasticpass"
        / "config.json"
    )
    profile_path: Path = field(
        default_factory=lambda: default_config_home()
        / "lxterminal"
        / "lxterminal-meshtasticpass.conf"
    )
    _unknown: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(
        cls,
        config_path: Path | None = None,
        profile_path: Path | None = None,
    ) -> AppSettings:
        """Load valid settings, falling back safely when the file is unusable."""
        settings = cls()
        if config_path is not None:
            settings.config_path = Path(config_path)
        if profile_path is not None:
            settings.profile_path = Path(profile_path)

        try:
            raw = json.loads(settings.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return settings

        if not isinstance(raw, dict):
            return settings

        settings._unknown = {
            key: value
            for key, value in raw.items()
            if key
            not in (
                "font_size",
                "color",
                "device_path",
                "favorite_node_ids",
                "clock_auto_sync",
                "radio_config_presets",
                "hidden_channel_ids",
            )
        }
        candidate = raw.get("font_size")
        if cls.is_valid_font_size(candidate):
            settings.font_size = candidate
        color_candidate = raw.get("color")
        if isinstance(color_candidate, str):
            color_candidate = _LEGACY_COLOR_MIGRATION.get(
                color_candidate, color_candidate
            )
        if cls.is_valid_color(color_candidate):
            settings.color = color_candidate
        device_candidate = raw.get("device_path")
        if cls.is_valid_device_path(device_candidate):
            settings.device_path = device_candidate
        favorites = raw.get("favorite_node_ids")
        if isinstance(favorites, list):
            settings.favorite_node_ids = {
                value.strip().lower()
                for value in favorites
                if isinstance(value, str) and value.strip()
            }
        hidden = raw.get("hidden_channel_ids")
        if isinstance(hidden, list):
            settings.hidden_channel_ids = {
                value.strip()
                for value in hidden
                if isinstance(value, str) and value.strip()
            }
        auto_sync_candidate = raw.get("clock_auto_sync")
        if isinstance(auto_sync_candidate, bool):
            settings.clock_auto_sync = auto_sync_candidate
        presets_candidate = raw.get("radio_config_presets")
        if isinstance(presets_candidate, list):
            settings.radio_config_presets = [
                preset
                for preset in (
                    _radio_config_preset_from_dict(item) for item in presets_candidate
                )
                if preset is not None
            ]
        return settings

    @staticmethod
    def is_valid_font_size(value: Any) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value in VALID_FONT_SIZES
        )

    def set_font_size(self, font_size: int) -> None:
        if not self.is_valid_font_size(font_size):
            choices = ", ".join(str(size) for size in VALID_FONT_SIZES)
            raise ValueError(f"Font size must be one of: {choices}")
        self.font_size = font_size

    @staticmethod
    def is_valid_color(value: Any) -> bool:
        return isinstance(value, str) and value in VALID_COLORS

    def set_color(self, color: str) -> None:
        if not self.is_valid_color(color):
            choices = ", ".join(VALID_COLORS)
            raise ValueError(f"Color must be one of: {choices}")
        self.color = color

    @staticmethod
    def is_valid_device_path(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def set_device_path(self, device_path: str) -> None:
        if not self.is_valid_device_path(device_path):
            raise ValueError("USB device path cannot be empty.")
        self.device_path = device_path.strip()

    def set_clock_auto_sync(self, enabled: bool) -> None:
        """Explicit opt-in only -- see item 16: never enabled silently."""
        self.clock_auto_sync = bool(enabled)

    def is_favorite(self, node_id: str | None) -> bool:
        return (
            isinstance(node_id, str)
            and node_id.strip().lower() in self.favorite_node_ids
        )

    def set_favorite(self, node_id: str, favorite: bool) -> None:
        """Persist favorite identity by stable Meshtastic Node ID."""
        if not isinstance(node_id, str) or not node_id.strip():
            raise ValueError("Favorite Node ID cannot be empty.")
        normalized = node_id.strip().lower()
        if favorite:
            self.favorite_node_ids.add(normalized)
        else:
            self.favorite_node_ids.discard(normalized)

    def radio_config_preset_names(self) -> tuple[str, ...]:
        return tuple(preset.name for preset in self.radio_config_presets)

    def get_radio_config_preset(self, name: str) -> RadioConfigPreset | None:
        return next(
            (preset for preset in self.radio_config_presets if preset.name == name),
            None,
        )

    def save_radio_config_preset(self, preset: RadioConfigPreset) -> None:
        """Create or update-by-name -- SAVE never touches the connected

        radio (see ADVANCED RADIO CONFIG's own "browsing/editing/
        saving causes zero RF" requirement).
        """
        if not preset.name.strip():
            raise ValueError("Preset name cannot be empty.")
        self.radio_config_presets = [
            existing
            for existing in self.radio_config_presets
            if existing.name != preset.name
        ]
        self.radio_config_presets.append(preset)

    def delete_radio_config_preset(self, name: str) -> None:
        self.radio_config_presets = [
            preset for preset in self.radio_config_presets if preset.name != name
        ]

    def save(self) -> None:
        """Atomically save known settings while retaining unknown future keys."""
        data = dict(self._unknown)
        data["font_size"] = self.font_size
        data["color"] = self.color
        data["device_path"] = self.device_path
        data["favorite_node_ids"] = sorted(self.favorite_node_ids)
        data["clock_auto_sync"] = self.clock_auto_sync
        data["hidden_channel_ids"] = sorted(self.hidden_channel_ids)
        data["radio_config_presets"] = [
            {
                "name": preset.name,
                "modem_preset": preset.modem_preset,
                "frequency_slot": preset.frequency_slot,
                "channel_name": preset.channel_name,
                "channel_psk_base64": preset.channel_psk_base64,
            }
            for preset in self.radio_config_presets
        ]
        content = json.dumps(data, indent=2, sort_keys=True) + "\n"
        self._atomic_write(self.config_path, content)

    def update_lxterminal_profile(self) -> None:
        """Update only the dedicated MeshtasticPass LXTerminal profile."""
        try:
            content = self.profile_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            content = ""

        lines = content.splitlines()
        general_start = next(
            (index for index, line in enumerate(lines) if line.strip() == "[general]"),
            None,
        )
        if general_start is None:
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend(["[general]"])
            general_start = len(lines) - 1

        general_end = next(
            (
                index
                for index in range(general_start + 1, len(lines))
                if lines[index].strip().startswith("[")
                and lines[index].strip().endswith("]")
            ),
            len(lines),
        )
        desired = {
            "fontname": f"Monospace {self.font_size}",
            "hidemenubar": "true",
            "hidescrollbar": "true",
        }
        found: set[str] = set()
        for index in range(general_start + 1, general_end):
            key = lines[index].partition("=")[0].strip().lower()
            if key in desired:
                lines[index] = f"{key}={desired[key]}"
                found.add(key)

        insert_at = general_end
        for key, value in desired.items():
            if key not in found:
                lines.insert(insert_at, f"{key}={value}")
                insert_at += 1

        self._atomic_write(self.profile_path, "\n".join(lines) + "\n")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                temporary_file.write(content)
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
