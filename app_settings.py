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
)
VALID_FONT_SIZES = tuple(size for _name, size in FONT_SIZE_CHOICES)
DEFAULT_FONT_SIZE = 13


def default_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


@dataclass
class AppSettings:
    """Load, validate, and save the small user-facing settings file."""

    font_size: int = DEFAULT_FONT_SIZE
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
            key: value for key, value in raw.items() if key != "font_size"
        }
        candidate = raw.get("font_size")
        if cls.is_valid_font_size(candidate):
            settings.font_size = candidate
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

    def save(self) -> None:
        """Atomically save known settings while retaining unknown future keys."""
        data = dict(self._unknown)
        data["font_size"] = self.font_size
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
