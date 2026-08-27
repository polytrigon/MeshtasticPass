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
# XL, not MEDIUM: this is the fallback used only when no saved font_size
# preference exists at all (missing file, unreadable file, or a file
# with no valid font_size key) -- see AppSettings.load. An existing
# user's already-saved SMALL/MEDIUM/LARGE/XL/XXL choice always wins,
# since load() only ever falls back to this constant, never overwrites
# a value found in the config file.
DEFAULT_FONT_SIZE = 18
COLOR_CHOICES = (
    ("WHITE", "white"),
    ("GREEN", "green"),
    ("ORANGE", "orange"),
)
VALID_COLORS = tuple(value for _name, value in COLOR_CHOICES)
DEFAULT_COLOR = "white"
DEFAULT_DEVICE_PATH = "/dev/ttyUSB0"


def default_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


@dataclass
class AppSettings:
    """Load, validate, and save the small user-facing settings file."""

    font_size: int = DEFAULT_FONT_SIZE
    color: str = DEFAULT_COLOR
    device_path: str = DEFAULT_DEVICE_PATH
    favorite_node_ids: set[str] = field(default_factory=set)
    # A MeshtasticPass-local behavior preference, never a radio config
    # field (see RadioService.sync_clock/SyncClockControl) -- OFF until
    # the user explicitly turns it on; never silently enabled by a
    # default-on migration or an unrelated setting.
    clock_auto_sync: bool = False
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
            )
        }
        candidate = raw.get("font_size")
        if cls.is_valid_font_size(candidate):
            settings.font_size = candidate
        color_candidate = raw.get("color")
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
        auto_sync_candidate = raw.get("clock_auto_sync")
        if isinstance(auto_sync_candidate, bool):
            settings.clock_auto_sync = auto_sync_candidate
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

    def save(self) -> None:
        """Atomically save known settings while retaining unknown future keys."""
        data = dict(self._unknown)
        data["font_size"] = self.font_size
        data["color"] = self.color
        data["device_path"] = self.device_path
        data["favorite_node_ids"] = sorted(self.favorite_node_ids)
        data["clock_auto_sync"] = self.clock_auto_sync
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
