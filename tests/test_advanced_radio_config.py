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

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from textual.containers import Horizontal
from textual.widgets import Input, Static

from app import (
    BUILTIN_LONGFAST_NETWORK,
    CONNECTION_VALUE_COLUMN_INDENT,
    CancelNetworkControl,
    ConnectionPage,
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
from radio_service import (
    ConfigWriteResult,
    RadioApplyResult,
    RadioState,
    apply_radio_config_preset,
    psk_matches_request,
    verify_radio_config_preset,
)
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

    def test_apply_delegates_to_apply_network_config_fire_and_forget(self) -> None:
        radio = self._radio()
        captured: dict = {}
        stages: list[str] = []
        original = radio.apply_network_config

        def spy(**kwargs):
            captured.update(kwargs)
            log = kwargs.get("stage_log")

            def tap(message):
                stages.append(message)
                if log:
                    log(message)

            kwargs["stage_log"] = tap
            return original(**kwargs)

        radio.apply_network_config = spy
        preset = RadioConfigPreset(
            name="NYC MS48",
            modem_preset="MEDIUM_SLOW",
            frequency_slot=48,
            channel_name="",
            channel_psk_base64="AQ==",
        )

        result = apply_radio_config_preset(radio, preset)

        self.assertTrue(result.applied)
        self.assertEqual(captured["modem_preset"], 3)  # MEDIUM_SLOW enum
        self.assertEqual(captured["channel_num"], 48)
        self.assertEqual(captured["channel_name"], "")  # NETWORK NAME not written
        self.assertEqual(captured["psk"], bytes([1]))  # AQ== decoded ONCE
        # begin -> lora -> channel -> commit, each START'd once, in order.
        self.assertEqual(
            [s.split()[0] for s in stages if s.endswith("START")],
            ["begin", "lora", "channel", "commit"],
        )
        self.assertIn("lora DONE", stages)
        self.assertIn("commit DONE", stages)

    def test_apply_reports_the_failed_stage(self) -> None:
        radio = self._radio()
        radio.apply_network_config = lambda **k: RadioApplyResult(
            False, "lora", {"lora": ConfigWriteResult(False, None, None, "error: X")}
        )
        result = apply_radio_config_preset(
            radio, RadioConfigPreset(name="Home", modem_preset="LONG_FAST")
        )
        self.assertFalse(result.applied)
        self.assertEqual(result.failed_step, "lora")

    def test_invalid_modem_preset_name_fails_before_any_rf(self) -> None:
        radio = self._radio()
        radio.apply_network_config = Mock()
        preset = RadioConfigPreset(name="Bad", modem_preset="NOT_A_REAL_PRESET")

        result = apply_radio_config_preset(radio, preset)

        self.assertFalse(result.applied)
        self.assertEqual(result.failed_step, "modem_preset")
        radio.apply_network_config.assert_not_called()

    def test_invalid_psk_fails_before_any_rf(self) -> None:
        radio = self._radio()
        radio.apply_network_config = Mock()
        preset = RadioConfigPreset(
            name="Bad", modem_preset="LONG_FAST", channel_psk_base64="not!base64!"
        )

        result = apply_radio_config_preset(radio, preset)

        self.assertFalse(result.applied)
        self.assertEqual(result.failed_step, "channel")
        radio.apply_network_config.assert_not_called()

    def test_not_connected_fails_cleanly(self) -> None:
        radio = SimulatedRadioService(connect_delay=0, message_interval=0)
        preset = RadioConfigPreset(name="Home", modem_preset="LONG_FAST")

        result = apply_radio_config_preset(radio, preset)

        self.assertFalse(result.applied)
        self.assertEqual(result.failed_step, "connect")
        self.assertEqual(result.results["connect"].reason, "not_connected")


class VerifyRadioConfigPresetTests(unittest.TestCase):
    @staticmethod
    def _radio() -> SimulatedRadioService:
        radio = SimulatedRadioService(connect_delay=0, message_interval=0)
        radio.connect()
        radio._online = True
        return radio

    def test_psk_matches_request_semantics(self) -> None:
        default_key = __import__("meshtastic.util", fromlist=["DEFAULT_KEY"]).DEFAULT_KEY
        self.assertTrue(psk_matches_request(bytes([1]), bytes([1])))
        self.assertTrue(psk_matches_request(bytes([1]), default_key))  # expanded
        self.assertTrue(psk_matches_request(default_key, bytes([1])))
        self.assertTrue(psk_matches_request(b"", b""))
        self.assertTrue(psk_matches_request(b"", bytes([0])))  # "no encryption"
        self.assertFalse(psk_matches_request(bytes([1]), b"random-16-bytes!"))

    def test_longfast_channel_num_zero_is_semantically_verified(self) -> None:
        radio = self._radio()
        apply_radio_config_preset(
            radio,
            RadioConfigPreset(
                name="LongFast",
                modem_preset="LONG_FAST",
                frequency_slot=0,
                channel_psk_base64="AQ==",
            ),
        )
        # channel_num is the auto-select sentinel 0.
        radio._config_sections["lora"]["channel_num"] = 0
        v = verify_radio_config_preset(
            radio,
            RadioConfigPreset(
                name="LongFast",
                modem_preset="LONG_FAST",
                frequency_slot=0,
                channel_psk_base64="AQ==",
            ),
        )
        self.assertTrue(v.ok)
        self.assertEqual(v.mismatched_field, "")
        # And an unavailable channel_num readback still verifies for a
        # requested 0 (never a false literal-equality failure).
        radio.read_synced_config_field = (
            lambda s, f: None if (s, f) == ("lora", "channel_num")
            else SimulatedRadioService.read_synced_config_field(radio, s, f)
        )
        v2 = verify_radio_config_preset(
            radio, RadioConfigPreset(name="LF", modem_preset="LONG_FAST", frequency_slot=0)
        )
        slot_check = next(c for c in v2.checks if c.field == "frequency_slot")
        self.assertTrue(slot_check.match)

    def test_ms48_slot_48_verification_is_strict(self) -> None:
        radio = self._radio()
        preset = RadioConfigPreset(
            name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48
        )
        apply_radio_config_preset(radio, preset)
        radio._config_sections["lora"]["channel_num"] = 20  # wrong slot
        v = verify_radio_config_preset(radio, preset)
        self.assertFalse(v.ok)
        self.assertEqual(v.mismatched_field, "frequency_slot")

    def test_verification_identifies_the_specific_mismatched_field(self) -> None:
        radio = self._radio()
        preset = RadioConfigPreset(
            name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48
        )
        apply_radio_config_preset(radio, preset)
        radio._config_sections["lora"]["modem_preset"] = 0  # reverted to LONG_FAST
        v = verify_radio_config_preset(radio, preset)
        self.assertEqual(v.mismatched_field, "modem_preset")
        modem_check = next(c for c in v.checks if c.field == "modem_preset")
        self.assertEqual(modem_check.requested, "MEDIUM_SLOW")
        self.assertEqual(modem_check.actual, "LONG_FAST")

    def test_verification_strings_never_contain_psk_bytes(self) -> None:
        radio = self._radio()
        preset = RadioConfigPreset(
            name="Keyed",
            modem_preset="LONG_FAST",
            channel_psk_base64="AQ==",
        )
        apply_radio_config_preset(radio, preset)
        v = verify_radio_config_preset(radio, preset)
        psk_check = next(c for c in v.checks if c.field == "channel_psk")
        self.assertEqual(psk_check.requested, "len=1")
        self.assertTrue(psk_check.actual.startswith("len="))
        self.assertNotIn("AQ==", str(v))
        self.assertNotIn("\\x01", repr(v))

    def test_network_name_is_never_the_channel_name_note(self) -> None:
        radio = self._radio()
        preset = RadioConfigPreset(
            name="NYC MS48",  # local-only network name
            modem_preset="LONG_FAST",
            channel_name="",  # channel name stays blank
        )
        apply_radio_config_preset(radio, preset)
        v = verify_radio_config_preset(radio, preset)
        self.assertNotIn("NYC MS48", v.channel_name_note)
        self.assertIn("blank", v.channel_name_note)


class AdvancedRadioUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config_path = self.root / "config.json"
        self.settings = AppSettings.load(
            config_path=self.config_path, profile_path=self.root / "terminal.conf"
        )
        # The post-write readback-converge poll has a real settle/retry
        # cadence for hardware; keep it near-instant under --simulate/tests.
        for attr, value in (
            ("_NETWORK_VERIFY_SETTLE_SECONDS", 0.0),
            ("_NETWORK_VERIFY_INTERVAL_SECONDS", 0.02),
            ("_NETWORK_VERIFY_DEADLINE_SECONDS", 0.4),
        ):
            original = getattr(MeshtasticPassApp, attr)
            setattr(MeshtasticPassApp, attr, value)
            self.addCleanup(setattr, MeshtasticPassApp, attr, original)

    @staticmethod
    async def _wait_apply_settled(pilot, app, tries: int = 120) -> None:
        for _ in range(tries):
            await pilot.pause(0.05)
            if app._network_apply is None:
                return

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

    @staticmethod
    def _apply_commits_wrong_lora(radio, **overrides) -> None:
        """Wrap apply_network_config so the writes 'succeed' but the

        radio ends up with DIFFERENT lora values -- so the post-write
        readback verify legitimately fails (radio still connected).
        """
        original = radio.apply_network_config

        def wrapper(**kwargs):
            result = original(**kwargs)
            radio._config_sections.setdefault("lora", {}).update(overrides)
            radio._rebuild_config_snapshot()
            return result

        radio.apply_network_config = wrapper

    @staticmethod
    def _apply_reports_disconnected(radio) -> None:
        """apply_network_config returns a 'disconnected' stage -- i.e.

        the interface dropped mid-write (a genuine observed disconnect,
        the ONLY thing that may enter the reconnect path).
        """
        radio.apply_network_config = lambda **k: RadioApplyResult(
            False, "lora", {"lora": ConfigWriteResult(False, None, None, "disconnected")}
        )

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
        # Writes go out, radio stays connected, but ends up on the wrong
        # slot -> the fresh readback verify legitimately fails.
        self._apply_commits_wrong_lora(radio, channel_num=99)
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

    async def test_network_switch_applies_immediately_on_one_selection(self) -> None:
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

            statuses: list[tuple[str, object]] = []
            orig_status = app._set_advanced_radio_status
            app._set_advanced_radio_status = (
                lambda text, cls: statuses.append((text, cls)) or orig_status(text, cls)
            )
            begin_calls: list[tuple[str, bool]] = []
            orig_begin = app._begin_network_apply
            app._begin_network_apply = (
                lambda name, preset, *, saved: begin_calls.append((name, saved))
                or orig_begin(name, preset, saved=saved)
            )

            # ONE selection is the whole commitment -- no confirm state,
            # no second ENTER.
            app._on_network_selected("NYC MS48")
            self.assertEqual(begin_calls, [("NYC MS48", False)])
            self.assertIsNone(app._advanced_radio_confirm)
            # Initial status makes NO premature reboot claim.
            self.assertIn(("SWITCHING TO NYC MS48...", "setting-accent"), statuses)
            joined = " ".join(text for text, _ in statuses)
            self.assertNotIn("AGAIN TO CONFIRM", joined)
            self.assertNotIn("SWITCHING TO NYC MS48... REBOOTING", joined)

            for _ in range(12):
                await pilot.pause()

            self.assertEqual(app._selected_network, "NYC MS48")
            self.assertEqual(radio._config_sections["lora"]["modem_preset"], 3)
            self.assertEqual(radio._config_sections["lora"]["channel_num"], 48)

    async def test_network_dropdown_browsing_and_current_selection_zero_writes(self) -> None:
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
            selector = app.query_one(NetworkSelector)

            # Open + navigate + ESC: KeyboardDropdown never posts
            # Selected for any of this.
            selector.focus()
            await pilot.press("enter")   # open
            await pilot.press("down")
            await pilot.press("up")
            await pilot.press("escape")  # cancel
            await pilot.pause()
            self.assertIsNone(app._network_apply)
            radio.write_verified_config_field.assert_not_called()

            # Selecting the currently-active NETWORK is a no-op.
            app._on_network_selected(BUILTIN_LONGFAST_NETWORK)
            await pilot.pause()
            self.assertIsNone(app._network_apply)
            radio.write_verified_config_field.assert_not_called()

    async def test_failed_switch_verification_does_not_claim_target_active(self) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48)
        )
        self._apply_reports_disconnected(radio)  # interface dropped mid-write
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()

            app._on_network_selected("NYC MS48")
            for _ in range(12):
                await pilot.pause()
            # Radio comes back STILL on the old config.
            radio.simulate_reconnect()
            await self._wait_apply_settled(pilot, app)

            self.assertIsNone(app._network_apply)
            self.assertEqual(app._selected_network, BUILTIN_LONGFAST_NETWORK)
            self.assertEqual(app.query_one(NetworkSelector).value, BUILTIN_LONGFAST_NETWORK)
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("NOT APPLIED", status)

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

    # ---- SAVE/APPLY state machine (the hardware stall regression) --------

    async def test_save_writes_psk_bytes_and_leaves_channel_name_blank(self) -> None:
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
            hop_before = radio._config_sections["lora"]["hop_limit"]
            app.query_one(SaveNetworkControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(12):
                await pilot.pause()

            # AQ== -> single byte 0x01 (decoded, not literal ASCII), and
            # the NETWORK NAME is never written as the channel name.
            self.assertEqual(radio.read_primary_channel_settings(), ("", bytes([1])))
            self.assertEqual(radio._config_sections["lora"]["modem_preset"], 3)
            self.assertEqual(radio._config_sections["lora"]["channel_num"], 48)
            self.assertEqual(radio._config_sections["lora"]["hop_limit"], hop_before)
            self.assertIsNone(app._network_apply)

    async def test_expected_reboot_reconnect_does_not_strand_the_apply(self) -> None:
        radio = self.radio()
        # The rare case: the interface actually drops during the write.
        self._apply_reports_disconnected(radio)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="NYC MS48", radio_mode="MEDIUM_SLOW", freq_slot="48", key="AQ=="
            )
            app.query_one(SaveNetworkControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(12):
                await pilot.pause()

            # Not stranded in "SAVING & APPLYING": it moved to the
            # awaiting-reconnect phase, still non-terminal but honest.
            self.assertIsNotNone(app._network_apply)
            self.assertTrue(app._network_apply.awaiting_reconnect)
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("WAITING FOR RECONNECT", status)

            # The radio comes back with the new config already applied.
            radio._config_sections["lora"]["modem_preset"] = 3
            radio._config_sections["lora"]["channel_num"] = 48
            radio._primary_channel_psk = bytes([1])
            radio.simulate_reconnect()
            await self._wait_apply_settled(pilot, app)

            self.assertIsNone(app._network_apply)  # terminal
            self.assertEqual(app._selected_network, "NYC MS48")
            self.assertEqual(app.query_one(NetworkSelector).value, "NYC MS48")
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("APPLIED", status)
            self.assertNotIn("NOT APPLIED", status)

    async def test_reconnect_with_wrong_config_reports_error_not_success(self) -> None:
        radio = self.radio()
        self._apply_reports_disconnected(radio)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="NYC MS48", radio_mode="MEDIUM_SLOW", freq_slot="48", key=""
            )
            app.query_one(SaveNetworkControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(12):
                await pilot.pause()

            # Radio reconnects but STILL on the old config.
            radio.simulate_reconnect()
            await self._wait_apply_settled(pilot, app)

            self.assertIsNone(app._network_apply)
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("NOT APPLIED", status)

    async def test_apply_timeout_reaches_terminal_error(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="NYC MS48", radio_mode="MEDIUM_SLOW", freq_slot="48", key=""
            )
            # Freeze the worker so no RadioConfigPresetApplied ever posts.
            app._apply_network_from_thread = lambda *a, **k: None
            app.query_one(SaveNetworkControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            self.assertIsNotNone(app._network_apply)
            token = app._network_apply.token
            app._network_apply_timed_out(token)
            await pilot.pause()

            self.assertIsNone(app._network_apply)
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("NOT APPLIED", status)
            self.assertIn("timed out", status)

    async def test_apply_diagnostics_never_print_key_material(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        buffer = io.StringIO()
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="NYC MS48", radio_mode="MEDIUM_SLOW", freq_slot="48",
                key="AQ==",
            )
            with contextlib.redirect_stdout(buffer):
                app.query_one(SaveNetworkControl).focus()
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")
                for _ in range(12):
                    await pilot.pause()

        printed = buffer.getvalue()
        self.assertIn("[NETCFG]", printed)
        self.assertIn("decoded_len=1", printed)
        self.assertIn("verify[", printed)  # field-by-field verification logged
        self.assertNotIn("AQ==", printed)
        self.assertNotIn("\\x01", printed)
        self.assertNotIn("1PG7OiAp", printed)  # never the expanded default key

    async def test_no_reboot_apply_completes_via_fresh_readback(self) -> None:
        """PATH B: the write + commit succeed and the connection stays

        up -- the operation resolves from a fresh readback immediately,
        never waiting for a reboot that is not coming.
        """
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(
                name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48
            )
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            statuses: list[str] = []
            orig = app._set_advanced_radio_status
            app._set_advanced_radio_status = (
                lambda t, c: statuses.append(t) or orig(t, c)
            )

            app._on_network_selected("NYC MS48")
            await self._wait_apply_settled(pilot, app)

            self.assertIsNone(app._network_apply)  # terminal, no reboot wait
            self.assertNotIn(
                "WAITING FOR RECONNECT", " ".join(statuses)
            )
            self.assertEqual(app._selected_network, "NYC MS48")
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("APPLIED", status)
            self.assertNotIn("NOT APPLIED", status)

    async def test_no_reboot_apply_with_wrong_readback_names_the_field(self) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(
                name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48
            )
        )
        # Writes go out, radio stays connected, but the slot ends up
        # wrong -> the fresh readback verify names FREQ. SLOT.
        self._apply_commits_wrong_lora(radio, channel_num=20)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()

            app._on_network_selected("NYC MS48")
            await self._wait_apply_settled(pilot, app)

            self.assertIsNone(app._network_apply)
            self.assertEqual(app._selected_network, BUILTIN_LONGFAST_NETWORK)  # reverted
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("FREQ. SLOT MISMATCH", status)
            self.assertNotIn("NYC MS48 APPLIED", status)

    # ---- Post-commit convergence ---------------------------------------

    @staticmethod
    def _readback_converges_on(radio, attempt, *, field="channel_num", stale=0):
        """The lora readback returns `stale` for `field` until the Nth

        verify call, then the real committed value -- models delayed
        SDK/radio state propagation after a fire-and-forget commit.
        Spies reread_lora_and_primary_channel so a test can assert each
        convergence attempt does a genuinely fresh readback.
        """
        radio.reread_lora_and_primary_channel = Mock(
            wraps=radio.reread_lora_and_primary_channel
        )
        real = radio.read_synced_config_field
        counter = {"n": 0}

        def fake(section, f):
            if (section, f) == ("lora", field):
                counter["n"] += 1
                if counter["n"] < attempt:
                    return stale
            return real(section, f)

        radio.read_synced_config_field = fake

    async def test_first_post_commit_mismatch_does_not_fail_then_converges(
        self,
    ) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48)
        )
        # channel_num reads stale (0) on attempt 1, correct (48) from 2.
        self._readback_converges_on(radio, 2, field="channel_num", stale=0)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()

            app._on_network_selected("NYC MS48")
            await self._wait_apply_settled(pilot, app)

            self.assertIsNone(app._network_apply)
            self.assertEqual(app._selected_network, "NYC MS48")
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("APPLIED", status)
            self.assertNotIn("NOT APPLIED", status)
            # Each attempt re-read the radio (>= 2 attempts here).
            self.assertGreaterEqual(radio.reread_lora_and_primary_channel.call_count, 2)

    async def test_convergence_stops_immediately_on_match(self) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48)
        )
        self._readback_converges_on(radio, 2, field="channel_num", stale=0)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            app._on_network_selected("NYC MS48")
            await self._wait_apply_settled(pilot, app)
            # Converged on attempt 2 -> a small, bounded number of
            # rereads, never the whole window.
            self.assertLessEqual(radio.reread_lora_and_primary_channel.call_count, 4)

    async def test_persistent_mismatch_reaches_terminal_error_naming_the_field(
        self,
    ) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48)
        )
        self._apply_commits_wrong_lora(radio, channel_num=99)  # never converges
        radio.reread_lora_and_primary_channel = Mock(
            wraps=radio.reread_lora_and_primary_channel
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            app._on_network_selected("NYC MS48")
            await self._wait_apply_settled(pilot, app)

            self.assertIsNone(app._network_apply)
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("FREQ. SLOT MISMATCH", status)
            self.assertEqual(app._selected_network, BUILTIN_LONGFAST_NETWORK)
            # It RETRIED (more than one readback) before giving up.
            self.assertGreaterEqual(radio.reread_lora_and_primary_channel.call_count, 2)

    async def test_genuine_write_error_fails_immediately_without_convergence(
        self,
    ) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48)
        )
        radio.apply_network_config = lambda **k: RadioApplyResult(
            False, "commit", {"commit": ConfigWriteResult(False, None, None, "error: X")}
        )
        radio.reread_lora_and_primary_channel = Mock(
            wraps=radio.reread_lora_and_primary_channel
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            app._on_network_selected("NYC MS48")
            await self._wait_apply_settled(pilot, app)

            self.assertIsNone(app._network_apply)
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("NOT APPLIED", status)
            self.assertIn("COMMIT", status)
            # No convergence poll for a write that never went out.
            radio.reread_lora_and_primary_channel.assert_not_called()

    async def test_convergence_retries_perform_zero_config_writes(self) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48)
        )
        self._readback_converges_on(radio, 3, field="channel_num", stale=0)
        radio.apply_network_config = Mock(wraps=radio.apply_network_config)
        radio.write_verified_config_field = Mock(wraps=radio.write_verified_config_field)
        radio.write_verified_primary_channel = Mock(
            wraps=radio.write_verified_primary_channel
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            app._on_network_selected("NYC MS48")
            await self._wait_apply_settled(pilot, app)

            self.assertEqual(radio.apply_network_config.call_count, 1)  # ONE write
            radio.write_verified_config_field.assert_not_called()
            radio.write_verified_primary_channel.assert_not_called()

    async def test_disconnect_during_convergence_enters_reconnect_path(self) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48)
        )
        self._apply_commits_wrong_lora(radio, channel_num=99)  # would never converge
        app = MeshtasticPassApp(radio, self.settings)
        # widen the window so the disconnect lands mid-convergence
        app._NETWORK_VERIFY_DEADLINE_SECONDS = 3.0
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            app._on_network_selected("NYC MS48")
            await pilot.pause(0.1)
            radio.simulate_disconnect()
            for _ in range(40):
                await pilot.pause(0.05)
                if app._network_apply and app._network_apply.awaiting_reconnect:
                    break

            self.assertIsNotNone(app._network_apply)
            self.assertTrue(app._network_apply.awaiting_reconnect)
            status = str(app.query_one("#advanced-radio-status", Static).render())
            self.assertIn("WAITING FOR RECONNECT", status)

    async def test_switch_status_scrolls_into_view_when_below_the_fold(self) -> None:
        radio = self.radio()
        self.settings.save_radio_config_preset(
            RadioConfigPreset(name="NYC MS48", modem_preset="MEDIUM_SLOW", frequency_slot=48)
        )
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(60, 12)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            page = app.query_one(ConnectionPage)
            page.scroll_home(animate=False)  # status is far below the fold
            await pilot.pause()

            status_widget = app.query_one("#advanced-radio-status", Static)
            app._on_network_selected("NYC MS48")
            for _ in range(8):
                await pilot.pause()

            region = status_widget.region
            viewport = page.scrollable_content_region
            self.assertTrue(
                viewport.contains_region(region) or region.y < viewport.bottom,
                f"status region {region} not brought into viewport {viewport}",
            )

    # ---- UI polish -----------------------------------------------------

    async def test_new_network_aligns_with_the_network_control(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            rendered = str(app.query_one(NewNetworkControl).render())
            self.assertTrue(rendered.startswith(CONNECTION_VALUE_COLUMN_INDENT))
            self.assertIn("[ NEW NETWORK ]", rendered)

    async def test_save_and_cancel_share_one_row(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            actions = app.query_one("#advanced-radio-actions", Horizontal)
            self.assertEqual(actions.styles.height.value, 1)
            save = app.query_one(SaveNetworkControl)
            cancel = app.query_one(CancelNetworkControl)
            self.assertEqual(save.region.y, cancel.region.y)
            self.assertLess(save.region.x, cancel.region.x)

    async def test_save_cancel_left_right_navigation(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            save = app.query_one(SaveNetworkControl)
            cancel = app.query_one(CancelNetworkControl)

            save.focus()
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertIs(app.focused, cancel)

            await pilot.press("left")
            await pilot.pause()
            self.assertIs(app.focused, save)

            # DOWN through the form lands on SAVE (not CANCEL); UP from
            # the action row returns to the KEY field.
            app.query_one("#key-input", Input).focus()
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            self.assertIs(app.focused, save)
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(app.focused.id, "key-input")

            # ENTER still activates each control.
            cancel.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertFalse(app._network_editor_open)  # CANCEL fired

    async def test_opening_editor_focuses_name_without_jumping_to_top(self) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(60, 12)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            page = app.query_one(ConnectionPage)
            page.scroll_end(animate=False)
            await pilot.pause()
            app.query_one(NewNetworkControl).focus()
            await pilot.press("enter")
            for _ in range(6):
                await pilot.pause()

            self.assertTrue(app._network_editor_open)
            self.assertIsInstance(app.focused, Input)
            self.assertEqual(app.focused.id, "network-name-input")
            self.assertGreater(page.scroll_offset.y, 0)  # did not jump to top

    async def test_status_line_is_indented_to_the_control_column(self) -> None:
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._open_editor(app, pilot)
            self._fill_editor(
                app, name="NYC MS48", radio_mode="MEDIUM_SLOW", freq_slot="48", key=""
            )
            app.query_one(SaveNetworkControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            status_widget = app.query_one("#advanced-radio-status", Static)
            rendered = str(status_widget.render())
            self.assertTrue(rendered.startswith(CONNECTION_VALUE_COLUMN_INDENT))
            self.assertNotIn("setting-error", status_widget.classes)


if __name__ == "__main__":
    unittest.main()
