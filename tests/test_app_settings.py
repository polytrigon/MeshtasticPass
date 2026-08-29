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
    FONT_SIZE_CHOICES,
    RadioConfigPreset,
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
        for color in ("snow", "amber"):
            with self.subTest(color=color), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.json"
                settings = AppSettings.load(config_path=config)

                settings.set_color(color)
                settings.save()

                self.assertEqual(AppSettings.load(config_path=config).color, color)

    def test_legacy_theme_names_migrate_deterministically(self) -> None:
        """Item 32: WHITE -> SNOW, ORANGE -> AMBER (base hue preserved),

        GREEN -> SNOW (no base-hue equivalent survives -- SNOW is the
        only remaining palette that still contains that exact neon
        green at all, as its own ACCENT). Never discarded outright:
        every legacy value resolves to a valid current one.
        """
        mapping = {"white": "snow", "green": "snow", "orange": "amber"}
        for legacy, migrated in mapping.items():
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.json"
                config.write_text(json.dumps({"color": legacy}), encoding="utf-8")

                settings = AppSettings.load(config_path=config)

                self.assertEqual(settings.color, migrated)

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

    def test_existing_config_without_color_uses_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "config.json"
            config.write_text(json.dumps({"font_size": 16}), encoding="utf-8")

            settings = AppSettings.load(config_path=config)

            self.assertEqual(settings.color, DEFAULT_COLOR)

    def test_invalid_color_falls_back_to_default(self) -> None:
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

    # ---- First-run default text size = LARGE ("UI SCALE" follow-up) ------

    def test_fresh_install_with_no_config_file_defaults_to_large(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "config.json"

            settings = AppSettings.load(config_path=config)

            self.assertEqual(settings.font_size, 16)
            self.assertEqual(DEFAULT_FONT_SIZE, 16)

    def test_config_file_missing_font_size_key_defaults_to_large(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "config.json"
            config.write_text(json.dumps({"color": "green"}), encoding="utf-8")

            settings = AppSettings.load(config_path=config)

            self.assertEqual(settings.font_size, 16)

    def test_config_file_with_invalid_font_size_defaults_to_large(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "config.json"
            config.write_text(json.dumps({"font_size": 999}), encoding="utf-8")

            settings = AppSettings.load(config_path=config)

            self.assertEqual(settings.font_size, 16)

    def test_each_existing_saved_font_size_survives_reload_unreset(self) -> None:
        """A user who already saved ANY size -- including ones that are

        not the current default -- must never be silently bumped to a
        new default on their next load (e.g. the XL->LARGE default
        change made in the "UI SCALE" follow-up).
        """
        for name, size in FONT_SIZE_CHOICES:
            with self.subTest(size=name), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "config.json"
                config.write_text(
                    json.dumps({"font_size": size}), encoding="utf-8"
                )

                settings = AppSettings.load(config_path=config)

                self.assertEqual(settings.font_size, size)

    def test_saved_font_size_persists_across_restart_even_when_it_is_xl(
        self,
    ) -> None:
        """Explicitly saving XL itself must persist as a REAL saved

        preference, not be indistinguishable from "no preference was
        ever saved" on the next load.
        """
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            settings = AppSettings.load(config_path=config)
            settings.set_font_size(11)
            settings.save()
            settings.set_font_size(18)
            settings.save()

            reloaded = AppSettings.load(config_path=config)
            self.assertEqual(reloaded.font_size, 18)

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


class RadioConfigPresetPersistenceTests(unittest.TestCase):
    """ADVANCED RADIO CONFIG (UI / CHANNEL / RADIO CONFIG TUNING Part D):

    saved presets round-trip through the same JSON config file as
    every other setting, survive a restart, tolerate an older config
    file with no presets at all, and never lose unrelated settings.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_path = Path(self.directory.name) / "config.json"

    def test_existing_config_without_radio_config_presets_still_loads(self) -> None:
        self.config_path.write_text(
            json.dumps({"font_size": 18, "color": "amber"}), encoding="utf-8"
        )

        settings = AppSettings.load(config_path=self.config_path)

        self.assertEqual(settings.radio_config_presets, [])
        self.assertEqual(settings.font_size, 18)
        self.assertEqual(settings.color, "amber")

    def test_save_load_delete_round_trips_ms48_acceptance_case(self) -> None:
        """The real hardware MS48 case: MEDIUM_SLOW, slot 48, a

        legitimately BLANK channel name, and the standard AQ== key.
        """
        settings = AppSettings.load(config_path=self.config_path)
        preset = RadioConfigPreset(
            name="NYC MS48",
            modem_preset="MEDIUM_SLOW",
            frequency_slot=48,
            channel_name="",
            channel_psk_base64="AQ==",
        )

        settings.save_radio_config_preset(preset)
        settings.save()

        reloaded = AppSettings.load(config_path=self.config_path)
        self.assertEqual(reloaded.radio_config_preset_names(), ("NYC MS48",))
        restored = reloaded.get_radio_config_preset("NYC MS48")
        self.assertEqual(restored.modem_preset, "MEDIUM_SLOW")
        self.assertEqual(restored.frequency_slot, 48)
        self.assertEqual(restored.channel_name, "")
        self.assertEqual(restored.channel_psk_base64, "AQ==")

        reloaded.delete_radio_config_preset("NYC MS48")
        reloaded.save()
        final = AppSettings.load(config_path=self.config_path)
        self.assertEqual(final.radio_config_presets, [])

    def test_saving_an_existing_name_updates_rather_than_duplicates(self) -> None:
        settings = AppSettings.load(config_path=self.config_path)
        settings.save_radio_config_preset(
            RadioConfigPreset(name="Home", modem_preset="LONG_FAST", frequency_slot=0)
        )
        settings.save_radio_config_preset(
            RadioConfigPreset(name="Home", modem_preset="MEDIUM_SLOW", frequency_slot=20)
        )

        self.assertEqual(len(settings.radio_config_presets), 1)
        self.assertEqual(settings.get_radio_config_preset("Home").modem_preset, "MEDIUM_SLOW")

    def test_empty_preset_name_is_rejected(self) -> None:
        settings = AppSettings.load(config_path=self.config_path)
        with self.assertRaises(ValueError):
            settings.save_radio_config_preset(
                RadioConfigPreset(name="  ", modem_preset="LONG_FAST")
            )

    def test_malformed_preset_entries_are_skipped_not_crashed_on(self) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    "radio_config_presets": [
                        {"name": "Valid", "modem_preset": "LONG_FAST"},
                        {"name": "", "modem_preset": "LONG_FAST"},
                        {"modem_preset": "LONG_FAST"},
                        {"name": "NoPreset"},
                        "not even a dict",
                        42,
                    ]
                }
            ),
            encoding="utf-8",
        )

        settings = AppSettings.load(config_path=self.config_path)

        self.assertEqual(settings.radio_config_preset_names(), ("Valid",))

    def test_unrelated_settings_survive_a_preset_save(self) -> None:
        settings = AppSettings.load(config_path=self.config_path)
        settings.set_favorite("!a11ce001", True)
        settings.set_font_size(22)
        settings.save_radio_config_preset(
            RadioConfigPreset(name="Home", modem_preset="LONG_FAST")
        )
        settings.save()

        reloaded = AppSettings.load(config_path=self.config_path)
        self.assertTrue(reloaded.is_favorite("!a11ce001"))
        self.assertEqual(reloaded.font_size, 22)
        self.assertEqual(reloaded.radio_config_preset_names(), ("Home",))

    def test_hop_limit_has_no_place_in_the_preset_model(self) -> None:
        """HOP LIMIT stays independent -- structural proof, not just

        behavioral: RadioConfigPreset has no hop_limit field to move it
        into in the first place.
        """
        preset = RadioConfigPreset(name="Home", modem_preset="LONG_FAST")
        self.assertFalse(hasattr(preset, "hop_limit"))


if __name__ == "__main__":
    unittest.main()
