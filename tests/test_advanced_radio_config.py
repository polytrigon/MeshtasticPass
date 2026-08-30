"""ADVANCED RADIO: the collapsed NETWORK selector + transient NEW
NETWORK editor.

Covers three layers: radio_capabilities' schema-derived modem-preset
helpers, radio_service.apply_radio_config_preset's controlled write
sequence (against SimulatedRadioService -- no live hardware writes
here), and the full app.py UI (collapsed default, transient editor,
SAVE confirm/apply, NETWORK switch confirm/apply, CANCEL, leave-view
discard, zero-RF browsing, and HOP LIMIT independence).
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from textual.widgets import Input, Static

from app import (
    BUILTIN_LONGFAST_NETWORK,
    CancelNetworkControl,
    HopLimitSelector,
    MeshtasticPassApp,
    NetworkSelector,
    NewNetworkControl,
    RadioModeSelector,
    SaveNetworkControl,
    builtin_longfast_preset,
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
        self.assertEqual(modem_preset_enum_name(0), "LONG_FAST")
        self.assertEqual(modem_preset_enum_name(3), "MEDIUM_SLOW")
        self.assertEqual(modem_preset_friendly_label(3), "MEDIUM SLOW")

    def test_unknown_enum_value_never_raises(self) -> None:
        self.assertIsNone(modem_preset_enum_name(999))
        self.assertEqual(modem_preset_friendly_label(999), "UNKNOWN(999)")


class BuiltinLongfastNetworkTests(unittest.TestCase):
    def test_builtin_longfast_uses_meshtastic_public_defaults(self) -> None:
        preset = builtin_longfast_preset()
        self.assertEqual(preset.name, BUILTIN_LONGFAST_NETWORK)
        self.assertEqual(preset.modem_preset, "LONG_FAST")
        self.assertEqual(preset.frequency_slot, 0)
        self.assertEqual(preset.channel_name, "")
        # "AQ==" is base64 for the SDK's own default public-channel PSK
        # sentinel byte 0x01.
        self.assertEqual(preset.channel_psk_base64, "AQ==")

    def test_builtin_longfast_round_trips_through_the_apply_boundary(self) -> None:
        radio = SimulatedRadioService(connect_delay=0, message_interval=0)
        radio.connect()
        radio._online = True
        result = apply_radio_config_preset(radio, builtin_longfast_preset())
        self.assertTrue(result.applied)
        self.assertEqual(radio._config_sections["lora"]["modem_preset"], 0)
        self.assertEqual(radio.read_primary_channel_settings(), ("", bytes([1])))


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


class AdvancedRadioUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config_path = self.root / "config.json"
        self.settings = AppSettings.load(
            config_path=self.config_path, profile_path=self.root / "terminal.conf"
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

    def _editor_rows_visible(self, app) -> bool:
        return app.query_one("#advanced-radio-actions").display

    async def _open_editor(self, app, pilot) -> None:
        app.query_one(NewNetworkControl).focus()
        await pilot.press("enter")
        await pilot.pause()

    def _fill_editor(self, app, *, name, radio_mode, freq_slot, key) -> None:
        app.query_one("#network-name-input", Input).value = name
        app.query_one(RadioModeSelector).value = radio_mode
        app.query_one("#freq-slot-input", Input).value = freq_slot
        app.query_one("#key-input", Input).value = key

    # ---- Collapsed default -------------------------------------------------

    async def test_heading_is_exactly_advanced_radio(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            title = str(app.query_one("#advanced-radio-title", Static).render())
            self.assertEqual(title, "ADVANCED RADIO")

    async def test_default_view_is_collapsed_with_longfast_network(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            self.assertFalse(app._network_editor_open)
            self.assertFalse(self._editor_rows_visible(app))
            self.assertTrue(app.query_one(NewNetworkControl).display)
            selector = app.query_one(NetworkSelector)
            self.assertEqual(selector.value, BUILTIN_LONGFAST_NETWORK)
            self.assertIn(
                BUILTIN_LONGFAST_NETWORK,
                [option.value for option in selector.options],
            )

    async def test_startup_and_reconnect_do_not_auto_apply_longfast(self) -> None:
        radio = self.radio()
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

    # ---- NEW NETWORK editor ----------------------------------------------

    async def test_new_network_reveals_exactly_the_small_editor(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)

            self.assertTrue(app._network_editor_open)
            self.assertFalse(app.query_one(NewNetworkControl).display)
            self.assertTrue(app.query_one("#network-name-row").display)
            self.assertTrue(app.query_one(RadioModeSelector).display)
            self.assertTrue(app.query_one("#freq-slot-row").display)
            self.assertTrue(app.query_one("#key-row").display)
            self.assertTrue(app.query_one("#advanced-radio-actions").display)
            # No CHANNEL NAME field, no raw LoRa engineering fields.
            self.assertEqual(len(app.query("#channel-name-input")), 0)
            self.assertEqual(len(app.query("#preset-channel-input")), 0)

    async def test_cancel_discards_and_collapses_with_zero_rf(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            radio.write_verified_config_field = Mock(
                wraps=radio.write_verified_config_field
            )
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="Scratch", radio_mode="MEDIUM_SLOW", freq_slot="7", key=""
            )
            app.query_one(CancelNetworkControl).focus()
            await pilot.press("enter")
            await pilot.pause()

            self.assertFalse(app._network_editor_open)
            self.assertEqual(app.query_one("#network-name-input", Input).value, "")
            self.assertEqual(app.settings.radio_config_preset_names(), ())
            radio.write_verified_config_field.assert_not_called()

    async def test_leaving_connection_discards_unfinished_editor(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            radio.write_verified_config_field = Mock(
                wraps=radio.write_verified_config_field
            )
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="Abandoned", radio_mode="LONG_FAST", freq_slot="9", key="AQ=="
            )

            app.show_tab("mesh")
            await pilot.pause()
            app.show_tab("connection")
            await pilot.pause()

            self.assertFalse(app._network_editor_open)
            self.assertTrue(app.query_one(NewNetworkControl).display)
            self.assertEqual(app.query_one("#network-name-input", Input).value, "")
            self.assertEqual(app.query_one("#freq-slot-input", Input).value, "")
            self.assertEqual(app.query_one("#key-input", Input).value, "")
            self.assertEqual(app.settings.radio_config_preset_names(), ())
            radio.write_verified_config_field.assert_not_called()

    async def test_app_restart_does_not_resurrect_unfinished_editor(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="Ghost", radio_mode="LONG_FAST", freq_slot="3", key=""
            )
            app.show_tab("mesh")
            await pilot.pause()

        reloaded = AppSettings.load(
            config_path=self.config_path, profile_path=self.root / "terminal.conf"
        )
        app2 = MeshtasticPassApp(self.radio(), reloaded)
        async with app2.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app2)
            app2.show_tab("connection")
            await pilot.pause()
            self.assertFalse(app2._network_editor_open)
            self.assertEqual(app2.query_one("#network-name-input", Input).value, "")
            self.assertEqual(reloaded.radio_config_preset_names(), ())

    # ---- SAVE -----------------------------------------------------------

    async def test_save_requires_confirmation_then_persists_and_applies(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="NYC MS48", radio_mode="MEDIUM_SLOW", freq_slot="48", key="AQ=="
            )
            save = app.query_one(SaveNetworkControl)
            save.focus()
            await pilot.press("enter")
            await pilot.pause()

            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("PRESS SAVE AGAIN TO CONFIRM", status)
            self.assertEqual(radio._config_sections["lora"]["channel_num"], 0)

            await pilot.press("enter")
            for _ in range(12):
                await pilot.pause()

            self.assertEqual(self.settings.radio_config_preset_names(), ("NYC MS48",))
            saved = self.settings.get_radio_config_preset("NYC MS48")
            self.assertEqual(saved.channel_name, "")  # NETWORK NAME != channel name
            self.assertEqual(saved.modem_preset, "MEDIUM_SLOW")
            self.assertFalse(app._network_editor_open)
            self.assertEqual(app.query_one(NetworkSelector).value, "NYC MS48")
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("APPLIED", status)
            self.assertNotIn("NOT APPLIED", status)
            self.assertEqual(radio._config_sections["lora"]["modem_preset"], 3)
            self.assertEqual(radio._config_sections["lora"]["channel_num"], 48)

    async def test_save_rf_failure_never_falsely_reports_applied(self) -> None:
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
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="Home", radio_mode="LONG_FAST", freq_slot="12", key=""
            )
            save = app.query_one(SaveNetworkControl)
            save.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(12):
                await pilot.pause()

            # Persisted locally, but RF honestly reported as NOT applied.
            self.assertEqual(self.settings.radio_config_preset_names(), ("Home",))
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("NOT APPLIED", status)
            self.assertNotIn("Home SAVED & APPLIED", status)

    async def test_save_while_offline_persists_but_reports_not_applied(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="Later", radio_mode="LONG_FAST", freq_slot="", key=""
            )
            radio.simulate_disconnect()
            for _ in range(10):
                await pilot.pause()
                if app._radio_state is not RadioState.ONLINE:
                    break

            save = app.query_one(SaveNetworkControl)
            save.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(self.settings.radio_config_preset_names(), ("Later",))
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("NOT APPLIED", status)

    async def test_save_missing_name_is_rejected_before_confirm(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="   ", radio_mode="LONG_FAST", freq_slot="", key=""
            )
            save = app.query_one(SaveNetworkControl)
            save.focus()
            await pilot.press("enter")
            await pilot.pause()
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("NETWORK NAME REQUIRED", status)
            self.assertIsNone(app._advanced_radio_confirm)
            self.assertEqual(self.settings.radio_config_preset_names(), ())

    # ---- NETWORK switch -------------------------------------------------

    async def test_network_switch_requires_confirmation_then_applies(self) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(
                name="NYC MS48",
                modem_preset="MEDIUM_SLOW",
                frequency_slot=48,
                channel_psk_base64="AQ==",
            )
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            radio.write_verified_config_field = Mock(
                wraps=radio.write_verified_config_field
            )

            app._on_network_selected("NYC MS48")
            await pilot.pause()
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("SELECT NYC MS48 AGAIN TO CONFIRM", status)
            radio.write_verified_config_field.assert_not_called()

            app._on_network_selected("NYC MS48")
            for _ in range(12):
                await pilot.pause()

            self.assertEqual(app._selected_network, "NYC MS48")
            self.assertEqual(radio._config_sections["lora"]["modem_preset"], 3)
            self.assertEqual(radio._config_sections["lora"]["channel_num"], 48)

    async def test_network_switch_cancelled_by_timeout_causes_zero_writes(self) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48)
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            radio.write_verified_config_field = Mock(
                wraps=radio.write_verified_config_field
            )

            app._on_network_selected("NYC MS48")
            await pilot.pause()
            app._advanced_radio_confirm_expired()
            await pilot.pause()

            self.assertIsNone(app._advanced_radio_confirm)
            self.assertEqual(app._selected_network, BUILTIN_LONGFAST_NETWORK)
            self.assertEqual(app.query_one(NetworkSelector).value, BUILTIN_LONGFAST_NETWORK)
            radio.write_verified_config_field.assert_not_called()

    # ---- Persistence / independence ------------------------------------

    async def test_existing_config_without_networks_still_loads(self) -> None:
        self.config_path.write_text('{"color": "amber"}', encoding="utf-8")
        settings = AppSettings.load(
            config_path=self.config_path, profile_path=self.root / "terminal.conf"
        )
        app = MeshtasticPassApp(self.radio(), settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            self.assertEqual(
                app.query_one(NetworkSelector).value, BUILTIN_LONGFAST_NETWORK
            )

    async def test_delete_network_is_not_implemented(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            self.assertEqual(len(app.query("#advanced-radio-delete-preset")), 0)
            for widget in app.query(Static):
                self.assertNotIn("DELETE NETWORK", str(widget.render()))

    async def test_hop_limit_untouched_by_network_operations(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            hop_limit_before = app.query_one(HopLimitSelector).value

            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="Home", radio_mode="LONG_FAST", freq_slot="", key=""
            )
            save = app.query_one(SaveNetworkControl)
            save.focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(12):
                await pilot.pause()

            self.assertEqual(app.query_one(HopLimitSelector).value, hop_limit_before)

    async def test_browsing_editing_and_cancel_cause_zero_rf(self) -> None:
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

            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="Home", radio_mode="LONG_FAST", freq_slot="5", key="AQ=="
            )
            app.query_one(RadioModeSelector).value = "MEDIUM_SLOW"
            await pilot.pause()
            app.query_one(CancelNetworkControl).focus()
            await pilot.press("enter")
            await pilot.pause()

            radio.write_verified_config_field.assert_not_called()
            radio.write_verified_primary_channel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
