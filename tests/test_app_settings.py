"""Tests for persistent MeshtasticPass user settings."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app_settings import (
    AppSettings,
    DEFAULT_COLOR,
    DEFAULT_DEVICE_PATH,
    DEFAULT_FONT_SIZE,
    VALID_COLORS,
    VALID_FONT_SIZES,
)


class AppSettingsTests(unittest.TestCase):
    def test_missing_file_uses_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "missing.json"

            settings = AppSettings.load(config_path=config)

            self.assertEqual(settings.font_size, DEFAULT_FONT_SIZE)
            self.assertEqual(settings.color, DEFAULT_COLOR)
            self.assertEqual(settings.device_path, DEFAULT_DEVICE_PATH)

    def test_persists_usb_device_and_supports_xxl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            settings = AppSettings.load(config_path=config)
            settings.set_device_path(" /dev/ttyACM0 ")
            settings.set_font_size(22)
            settings.save()

            reloaded = AppSettings.load(config_path=config)
            self.assertEqual(reloaded.device_path, "/dev/ttyACM0")
            self.assertEqual(reloaded.font_size, 22)

    def test_rejects_empty_usb_device(self) -> None:
        settings = AppSettings()
        for invalid in ("", "   ", None, 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                settings.set_device_path(invalid)  # type: ignore[arg-type]

    def test_persists_font_size_and_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "font_size": 11,
                        "color": "white",
                        "future_setting": {"value": 7},
                    }
                ),
                encoding="utf-8",
            )
            settings = AppSettings.load(config_path=config)

            settings.set_font_size(18)
            settings.save()
            reloaded = AppSettings.load(config_path=config)
            saved = json.loads(config.read_text(encoding="utf-8"))

            self.assertEqual(reloaded.font_size, 18)
            self.assertEqual(saved["future_setting"], {"value": 7})

    def test_persists_supported_colors(self) -> None:
        for color in ("green", "orange"):
            with self.subTest(color=color), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.json"
                settings = AppSettings.load(config_path=config)

                settings.set_color(color)
                settings.save()

                self.assertEqual(AppSettings.load(config_path=config).color, color)

    def test_favorites_persist_by_normalized_node_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            settings = AppSettings.load(config_path=config)
            settings.set_favorite(" !A11CE001 ", True)
            settings.save()

            reloaded = AppSettings.load(config_path=config)
            self.assertTrue(reloaded.is_favorite("!a11ce001"))
            self.assertEqual(reloaded.favorite_node_ids, {"!a11ce001"})

            reloaded.set_favorite("!A11CE001", False)
            reloaded.save()
            self.assertFalse(
                AppSettings.load(config_path=config).is_favorite("!a11ce001")
            )

    def test_existing_config_without_color_uses_white(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "config.json"
            config.write_text(json.dumps({"font_size": 16}), encoding="utf-8")

            settings = AppSettings.load(config_path=config)

            self.assertEqual(settings.color, DEFAULT_COLOR)

    def test_invalid_color_falls_back_to_white(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "config.json"
            config.write_text(
                json.dumps({"font_size": 16, "color": "purple"}),
                encoding="utf-8",
            )

            settings = AppSettings.load(config_path=config)

            self.assertEqual(settings.color, DEFAULT_COLOR)
            self.assertEqual(settings.font_size, 16)

    def test_validates_supported_colors(self) -> None:
        settings = AppSettings()

        for color in VALID_COLORS:
            with self.subTest(color=color):
                settings.set_color(color)
                self.assertEqual(settings.color, color)

        for invalid in ("purple", "GREEN", "", None, 1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    settings.set_color(invalid)  # type: ignore[arg-type]

    def test_malformed_file_falls_back_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "config.json"
            config.write_text("{not valid json", encoding="utf-8")

            settings = AppSettings.load(config_path=config)

            self.assertEqual(settings.font_size, DEFAULT_FONT_SIZE)

    def test_validates_supported_font_sizes(self) -> None:
        settings = AppSettings()

        for font_size in VALID_FONT_SIZES:
            with self.subTest(font_size=font_size):
                settings.set_font_size(font_size)
                self.assertEqual(settings.font_size, font_size)

        for invalid in (10, 12, 14, 20, True, "13", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    settings.set_font_size(invalid)  # type: ignore[arg-type]

    def test_generates_and_updates_only_dedicated_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            profile = root / "lxterminal-meshtasticpass.conf"
            profile.write_text(
                "[general]\n"
                "fontname=Monospace 11\n"
                "hidemenubar=true\n"
                "custom-setting=preserved\n"
                "\n[other]\nvalue=untouched\n",
                encoding="utf-8",
            )
            settings = AppSettings(profile_path=profile)
            settings.set_font_size(16)

            settings.update_lxterminal_profile()
            updated = profile.read_text(encoding="utf-8")

            self.assertIn("fontname=Monospace 16", updated)
            self.assertIn("hidemenubar=true", updated)
            self.assertIn("hidescrollbar=true", updated)
            self.assertIn("custom-setting=preserved", updated)
            self.assertIn("[other]\nvalue=untouched", updated)


if __name__ == "__main__":
    unittest.main()
