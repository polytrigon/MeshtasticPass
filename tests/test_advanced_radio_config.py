"""UI / CHANNEL / RADIO CONFIG TUNING Part D: ADVANCED RADIO CONFIG.

Covers three layers: radio_capabilities' schema-derived modem-preset
helpers, radio_service.apply_radio_config_preset's controlled write
sequence (against SimulatedRadioService -- no live hardware writes
here), and the full app.py UI (create/save/delete/apply, confirmation,
zero-RF browsing/editing/saving, and HOP LIMIT independence).
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from textual.widgets import Input, Static

from app import (
    ApplyPresetControl,
    CreateNewPresetControl,
    DeletePresetControl,
    HopLimitSelector,
    MeshtasticPassApp,
    ModemPresetFieldSelector,
    SavedRadioConfigSelector,
    SavePresetControl,
)
from app_settings import AppSettings, RadioConfigPreset
from radio_capabilities import (
    modem_preset_choices,
    modem_preset_enum_name,
    modem_preset_friendly_label,
)
from radio_service import ConfigWriteResult, RadioState, apply_radio_config_preset
from simulated_radio_service import SimulatedRadioService


class ModemPresetCapabilityTests(unittest.TestCase):
    def test_modem_preset_choices_are_schema_derived_and_include_ms48_case(self) -> None:
        choices = modem_preset_choices()
        names = {name for _label, name in choices}
        self.assertIn("MEDIUM_SLOW", names)
        self.assertIn("LONG_FAST", names)
        self.assertIn(("MEDIUM SLOW", "MEDIUM_SLOW"), choices)

    def test_modem_preset_enum_name_and_friendly_label_round_trip(self) -> None:
        # LONG_FAST is 0, MEDIUM_SLOW is 3 in the installed protobuf schema.
        self.assertEqual(modem_preset_enum_name(0), "LONG_FAST")
        self.assertEqual(modem_preset_enum_name(3), "MEDIUM_SLOW")
        self.assertEqual(modem_preset_friendly_label(3), "MEDIUM SLOW")

    def test_unknown_enum_value_never_raises(self) -> None:
        self.assertIsNone(modem_preset_enum_name(999))
        self.assertEqual(modem_preset_friendly_label(999), "UNKNOWN(999)")


class ApplyRadioConfigPresetServiceTests(unittest.TestCase):
    """Against SimulatedRadioService -- no live hardware writes."""

    @staticmethod
    def _radio() -> SimulatedRadioService:
        radio = SimulatedRadioService(connect_delay=0, message_interval=0)
        radio.connect()
        radio._online = True
        return radio

    def test_ms48_acceptance_case_round_trips_through_the_model(self) -> None:
        radio = self._radio()
        preset = RadioConfigPreset(
            name="NYC MS48",
            modem_preset="MEDIUM_SLOW",
            frequency_slot=48,
            channel_name="",
            channel_psk_base64="AQ==",
        )

        result = apply_radio_config_preset(radio, preset)

        self.assertTrue(result.applied)
        self.assertEqual(result.failed_step, "")
        self.assertEqual(radio._config_sections["lora"]["use_preset"], True)
        self.assertEqual(radio._config_sections["lora"]["modem_preset"], 3)
        self.assertEqual(radio._config_sections["lora"]["channel_num"], 48)
        self.assertEqual(radio.read_primary_channel_settings(), ("", bytes([1])))

    def test_blank_channel_name_is_accepted(self) -> None:
        radio = self._radio()
        preset = RadioConfigPreset(
            name="Blank", modem_preset="LONG_FAST", channel_name="", channel_psk_base64=""
        )

        result = apply_radio_config_preset(radio, preset)

        self.assertTrue(result.applied)
        self.assertEqual(radio.read_primary_channel_settings(), ("", b""))

    def test_each_write_is_called_exactly_once(self) -> None:
        radio = self._radio()
        radio.write_verified_config_field = Mock(wraps=radio.write_verified_config_field)
        radio.write_verified_primary_channel = Mock(
            wraps=radio.write_verified_primary_channel
        )
        preset = RadioConfigPreset(
            name="Home", modem_preset="LONG_FAST", frequency_slot=0, channel_name="Home"
        )

        result = apply_radio_config_preset(radio, preset)

        self.assertTrue(result.applied)
        self.assertEqual(radio.write_verified_config_field.call_count, 3)
        self.assertEqual(radio.write_verified_primary_channel.call_count, 1)

    def test_stops_at_first_failing_step_never_writes_later_fields(self) -> None:
        radio = self._radio()
        original = radio.write_verified_config_field

        def failing_write(section, field, value, **kwargs):
            if field == "modem_preset":
                return ConfigWriteResult(False, value, None, "nak")
            return original(section, field, value, **kwargs)

        radio.write_verified_config_field = failing_write
        radio.write_verified_primary_channel = Mock(
            wraps=radio.write_verified_primary_channel
        )
        preset = RadioConfigPreset(name="Home", modem_preset="LONG_FAST", frequency_slot=5)

        result = apply_radio_config_preset(radio, preset)

        self.assertFalse(result.applied)
        self.assertEqual(result.failed_step, "modem_preset")
        # channel_num/channel were never attempted.
        self.assertNotIn("frequency_slot", result.results)
        self.assertNotIn("channel", result.results)
        radio.write_verified_primary_channel.assert_not_called()

    def test_invalid_modem_preset_name_fails_before_any_write(self) -> None:
        radio = self._radio()
        radio.write_verified_config_field = Mock(wraps=radio.write_verified_config_field)
        preset = RadioConfigPreset(name="Bad", modem_preset="NOT_A_REAL_PRESET")

        result = apply_radio_config_preset(radio, preset)

        self.assertFalse(result.applied)
        self.assertEqual(result.failed_step, "modem_preset")
        radio.write_verified_config_field.assert_not_called()

    def test_not_connected_fails_cleanly(self) -> None:
        radio = SimulatedRadioService(connect_delay=0, message_interval=0)
        preset = RadioConfigPreset(name="Home", modem_preset="LONG_FAST")

        result = apply_radio_config_preset(radio, preset)

        self.assertFalse(result.applied)
        self.assertEqual(result.results["use_preset"].reason, "not_connected")


class AdvancedRadioConfigUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=root / "config.json", profile_path=root / "terminal.conf"
        )

    @staticmethod
    def radio() -> SimulatedRadioService:
        return SimulatedRadioService(connect_delay=0, message_interval=0)

    @staticmethod
    async def _wait_online(pilot, app) -> None:
        for _ in range(15):
            await pilot.pause()
            if app._radio_state is RadioState.ONLINE:
                break
        assert app._radio_state is RadioState.ONLINE

    def _fill_editor(self, app, *, name, modem_preset, freq_slot, channel, key) -> None:
        app.query_one("#preset-name-input", Input).value = name
        app.query_one(ModemPresetFieldSelector).value = modem_preset
        app.query_one("#preset-freq-slot-input", Input).value = freq_slot
        app.query_one("#preset-channel-input", Input).value = channel
        app.query_one("#preset-key-input", Input).value = key

    async def test_create_save_delete_local_preset(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()

            app.query_one(CreateNewPresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            self._fill_editor(
                app,
                name="NYC MS48",
                modem_preset="MEDIUM_SLOW",
                freq_slot="48",
                channel="",
                key="AQ==",
            )
            app.query_one(SavePresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(self.settings.radio_config_preset_names(), ("NYC MS48",))
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("SAVED", status)
            self.assertFalse(app.query_one(DeletePresetControl).disabled)

            app.query_one(DeletePresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(self.settings.radio_config_preset_names(), ())

    async def test_browsing_editing_and_saving_cause_zero_rf(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            radio.write_verified_config_field = Mock(
                wraps=radio.write_verified_config_field
            )
            radio.write_verified_primary_channel = Mock(
                wraps=radio.write_verified_primary_channel
            )

            app.query_one(CreateNewPresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            self._fill_editor(
                app,
                name="Home",
                modem_preset="LONG_FAST",
                freq_slot="",
                channel="Home",
                key="",
            )
            app.query_one(SavePresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()

            # Browse: reload the live radio's own values, then reload
            # the just-saved preset back into the editor.
            app._reset_preset_editor(prefill_from_live=True)
            saved = self.settings.get_radio_config_preset("Home")
            app._load_preset_into_editor(saved)
            await pilot.pause()

            radio.write_verified_config_field.assert_not_called()
            radio.write_verified_primary_channel.assert_not_called()

    async def test_apply_requires_confirmation_and_cancel_causes_zero_writes(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            radio.write_verified_config_field = Mock(
                wraps=radio.write_verified_config_field
            )

            app.query_one(CreateNewPresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            self._fill_editor(
                app,
                name="Home",
                modem_preset="LONG_FAST",
                freq_slot="",
                channel="Home",
                key="",
            )
            apply_btn = app.query_one(ApplyPresetControl)
            apply_btn.focus()
            await pilot.press("enter")
            await pilot.pause()

            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("PRESS APPLY AGAIN TO CONFIRM", status)
            radio.write_verified_config_field.assert_not_called()

            # Let the arm expire instead of confirming -- zero writes.
            app._advanced_radio_confirm_expired()
            await pilot.pause()
            radio.write_verified_config_field.assert_not_called()
            self.assertIsNone(app._advanced_radio_confirm)

    async def test_confirmed_apply_writes_and_reports_success(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()

            app.query_one(CreateNewPresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            self._fill_editor(
                app,
                name="NYC MS48",
                modem_preset="MEDIUM_SLOW",
                freq_slot="48",
                channel="",
                key="AQ==",
            )
            apply_btn = app.query_one(ApplyPresetControl)
            apply_btn.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()

            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("APPLIED", status)
            self.assertNotIn("NOT APPLIED", status)
            self.assertEqual(radio._config_sections["lora"]["modem_preset"], 3)
            self.assertEqual(radio._config_sections["lora"]["channel_num"], 48)

    async def test_apply_failure_never_falsely_reports_success(self) -> None:
        radio = self.radio()
        original = radio.write_verified_config_field

        def failing_write(section, field, value, **kwargs):
            if field == "channel_num":
                return ConfigWriteResult(False, value, None, "mismatch")
            return original(section, field, value, **kwargs)

        radio.write_verified_config_field = failing_write
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()

            app.query_one(CreateNewPresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            self._fill_editor(
                app,
                name="Home",
                modem_preset="LONG_FAST",
                freq_slot="12",
                channel="",
                key="",
            )
            apply_btn = app.query_one(ApplyPresetControl)
            apply_btn.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()

            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("NOT APPLIED", status)
            self.assertNotIn("Home APPLIED", status)

    async def test_reconnect_does_not_auto_apply(self) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="Home", modem_preset="MEDIUM_SLOW", frequency_slot=20)
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            radio.write_verified_config_field = Mock(
                wraps=radio.write_verified_config_field
            )
            radio.write_verified_primary_channel = Mock(
                wraps=radio.write_verified_primary_channel
            )

            radio.simulate_reconnect()
            for _ in range(15):
                await pilot.pause()
                if app._radio_state is RadioState.ONLINE:
                    break
            for _ in range(5):
                await pilot.pause()

            radio.write_verified_config_field.assert_not_called()
            radio.write_verified_primary_channel.assert_not_called()
            # The radio's own state is authoritative -- never silently
            # treated as identical to the untouched saved "Home" preset.
            self.assertEqual(radio._config_sections["lora"]["modem_preset"], 0)

    async def test_hop_limit_selector_untouched_by_preset_operations(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            hop_limit_before = app.query_one(HopLimitSelector).value

            app.query_one(CreateNewPresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            self._fill_editor(
                app,
                name="Home",
                modem_preset="LONG_FAST",
                freq_slot="",
                channel="",
                key="",
            )
            app.query_one(SavePresetControl).focus()
            await pilot.press("enter")
            await pilot.pause()

            apply_btn = app.query_one(ApplyPresetControl)
            apply_btn.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()

            self.assertEqual(app.query_one(HopLimitSelector).value, hop_limit_before)


if __name__ == "__main__":
    unittest.main()
