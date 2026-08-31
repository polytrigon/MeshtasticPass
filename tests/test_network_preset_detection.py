"""NETWORK startup authority: radio-authoritative PRESET detection.

Covers the CONNECTION/CONFIG NETWORK section's startup/reconnect
truthfulness rules:

- a fresh install locally defaults the PRESET selector to the built-in
  LongFast BEFORE any radio has been read, with zero radio/config
  writes (a LOCAL UI DEFAULT ONLY);
- once a radio sync succeeds, the RADIO is authoritative: the active
  PRESET is derived from the actual radio configuration via the SAME
  semantic comparison the apply verification uses (read-only);
- an unmatched radio configuration is presented honestly as
  UNMATCHED_NETWORK_LABEL ("CURRENT RADIO"), never falsely as LongFast,
  and never auto-"corrected" by writing to the radio;
- external radio changes are detected after a genuine reconnect;
- one PRESET selection produces exactly one apply transaction, and a
  reconnect never duplicates writes/commits;
- the terminal "<name> APPLIED" success line auto-dismisses after the
  fixed window, correlation-safely (a stale timer can never clear a
  newer status, and errors are never auto-cleared);
- the NETWORK/PRESET/NEW PRESET terminology and the reordered
  CONNECTION -> NETWORK -> RADIO -> STYLE keyboard navigation.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from textual.widgets import Static

import app as app_module
from app import (
    BUILTIN_LONGFAST_NETWORK,
    UNMATCHED_NETWORK_LABEL,
    MeshtasticPassApp,
    NetworkSelector,
    NewNetworkControl,
)
from app_settings import AppSettings, RadioConfigPreset
from host_timezone import HostTimezone
from radio_service import RadioState
from simulated_radio_service import SimulatedRadioService

# Config.LoRaConfig.ModemPreset enum values used by the fake radio
# (PROTOBUF-SOURCE-VERIFIED; same values the rest of the suite uses).
MEDIUM_SLOW = 3
LONG_SLOW = 1

# Every SimulatedRadioService method that can emit a radio write of any
# kind -- config, channel, transaction, or clock. Spied (never stubbed)
# so behavior is unchanged while call counts stay honest.
WRITE_PATH_METHODS = (
    "write_verified_config_field",
    "write_verified_primary_channel",
    "apply_network_config",
    "begin_settings_transaction",
    "commit_settings_transaction",
    "sync_clock",
)


def spy_write_paths(radio) -> dict[str, Mock]:
    spies: dict[str, Mock] = {}
    for name in WRITE_PATH_METHODS:
        spy = Mock(wraps=getattr(radio, name))
        setattr(radio, name, spy)
        spies[name] = spy
    return spies


def assert_zero_writes(test: unittest.TestCase, spies: dict[str, Mock]) -> None:
    for name, spy in spies.items():
        test.assertEqual(
            spy.call_count, 0, f"{name} was called {spy.call_count} times"
        )


class RadioAuthoritativePresetDetectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        for attr, value in (
            ("_NETWORK_VERIFY_SETTLE_SECONDS", 0.0),
            ("_NETWORK_VERIFY_INTERVAL_SECONDS", 0.02),
            ("_NETWORK_VERIFY_DEADLINE_SECONDS", 0.4),
        ):
            original = getattr(MeshtasticPassApp, attr)
            setattr(MeshtasticPassApp, attr, value)
            self.addCleanup(setattr, MeshtasticPassApp, attr, original)

    @staticmethod
    def radio(**kwargs) -> SimulatedRadioService:
        kwargs.setdefault("connect_delay", 0)
        kwargs.setdefault("message_interval", 0)
        return SimulatedRadioService(**kwargs)

    @staticmethod
    async def _wait_online(pilot, app) -> None:
        for _ in range(15):
            await pilot.pause()
            if app._radio_state is RadioState.ONLINE:
                break
        assert app._radio_state is RadioState.ONLINE

    @staticmethod
    async def _wait_apply_settled(pilot, app, tries: int = 120) -> None:
        for _ in range(tries):
            await pilot.pause(0.05)
            if app._network_apply is None:
                return

    @staticmethod
    async def _wait_reconnected(pilot, app) -> None:
        for _ in range(15):
            await pilot.pause()
            if app._radio_state is not RadioState.ONLINE:
                break
        for _ in range(15):
            await pilot.pause()
            if app._radio_state is RadioState.ONLINE:
                break
        assert app._radio_state is RadioState.ONLINE

    def _save_ms48_preset(self) -> None:
        self.settings.save_radio_config_preset(
            RadioConfigPreset(
                name="NYC MS48",
                modem_preset="MEDIUM_SLOW",
                frequency_slot=48,
                channel_psk_base64="AQ==",
            )
        )

    @staticmethod
    def _set_radio_to_ms48(radio) -> None:
        radio._config_sections["lora"]["modem_preset"] = MEDIUM_SLOW
        radio._config_sections["lora"]["channel_num"] = 48
        radio._primary_channel_psk = bytes([1])

    @staticmethod
    def _set_radio_to_unmatched(radio) -> None:
        radio._config_sections["lora"]["modem_preset"] = LONG_SLOW
        radio._config_sections["lora"]["channel_num"] = 7
        radio._primary_channel_psk = bytes(range(16))

    # ---- Fresh install: local default only, radio authoritative later ---

    async def test_fresh_install_presents_longfast_before_any_radio_read(
        self,
    ) -> None:
        radio = self.radio(connect_delay=60)  # never reaches ONLINE here
        spies = spy_write_paths(radio)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            for _ in range(5):
                await pilot.pause()
            self.assertIsNot(app._radio_state, RadioState.ONLINE)
            selector = app.query_one(NetworkSelector)
            self.assertEqual(selector.value, BUILTIN_LONGFAST_NETWORK)
            self.assertFalse(app._network_unmatched)
            # A LOCAL UI DEFAULT ONLY: zero radio/config writes.
            assert_zero_writes(self, spies)

    async def test_actual_longfast_radio_selects_longfast_after_sync(self) -> None:
        radio = self.radio()
        spies = spy_write_paths(radio)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            await pilot.pause()
            self.assertEqual(app._selected_network, BUILTIN_LONGFAST_NETWORK)
            self.assertFalse(app._network_unmatched)
            self.assertEqual(
                app.query_one(NetworkSelector).value, BUILTIN_LONGFAST_NETWORK
            )
            assert_zero_writes(self, spies)

    async def test_ms48_radio_selects_matching_saved_preset_after_sync(
        self,
    ) -> None:
        self._save_ms48_preset()
        radio = self.radio()
        self._set_radio_to_ms48(radio)
        spies = spy_write_paths(radio)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            await pilot.pause()
            # The radio overrides the app's local LongFast default.
            self.assertEqual(app._selected_network, "NYC MS48")
            self.assertFalse(app._network_unmatched)
            self.assertEqual(app.query_one(NetworkSelector).value, "NYC MS48")
            # Detection is READ-ONLY: the radio is never "corrected"
            # back toward the previously assumed PRESET.
            assert_zero_writes(self, spies)

    async def test_external_radio_change_is_detected_after_reconnect(self) -> None:
        self._save_ms48_preset()
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            await pilot.pause()
            self.assertEqual(app._selected_network, BUILTIN_LONGFAST_NETWORK)

            # The user changes the radio outside MeshtasticPass (e.g.
            # via the CLI) while the app is disconnected.
            self._set_radio_to_ms48(radio)
            spies = spy_write_paths(radio)
            radio.simulate_reconnect()
            await self._wait_reconnected(pilot, app)
            await pilot.pause()

            self.assertEqual(app._selected_network, "NYC MS48")
            self.assertEqual(app.query_one(NetworkSelector).value, "NYC MS48")
            # Reconnect detection performs zero config writes -- MS48 is
            # never overwritten back to LongFast.
            assert_zero_writes(self, spies)

    # ---- Unmatched radio configuration -----------------------------------

    async def test_unmatched_radio_is_not_falsely_labeled_longfast(self) -> None:
        self._save_ms48_preset()
        radio = self.radio()
        self._set_radio_to_unmatched(radio)
        spies = spy_write_paths(radio)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            await pilot.pause()
            selector = app.query_one(NetworkSelector)
            self.assertTrue(app._network_unmatched)
            self.assertEqual(selector.value, UNMATCHED_NETWORK_LABEL)
            rendered = str(selector.render())
            self.assertIn(f"[ {UNMATCHED_NETWORK_LABEL} ▾ ]", rendered)
            self.assertNotIn(f"[ {BUILTIN_LONGFAST_NETWORK} ▾ ]", rendered)
            # Display-only: never a selectable option, never persisted
            # as a PRESET, and zero writes to resolve it.
            self.assertNotIn(
                UNMATCHED_NETWORK_LABEL,
                [option.value for option in selector.options],
            )
            self.assertEqual(
                list(self.settings.radio_config_preset_names()), ["NYC MS48"]
            )
            assert_zero_writes(self, spies)

    async def test_unmatched_state_makes_any_explicit_selection_a_switch(
        self,
    ) -> None:
        radio = self.radio()
        self._set_radio_to_unmatched(radio)
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            self.assertTrue(app._network_unmatched)
            # While UNMATCHED there is no active PRESET: explicitly
            # selecting LongFast (the app's previous assumption) must be
            # a genuine apply, not an "already selected" no-op.
            spies = spy_write_paths(radio)
            app._on_network_selected(BUILTIN_LONGFAST_NETWORK)
            await self._wait_apply_settled(pilot, app)
            self.assertEqual(spies["apply_network_config"].call_count, 1)
            self.assertFalse(app._network_unmatched)
            self.assertEqual(app._selected_network, BUILTIN_LONGFAST_NETWORK)

    # ---- One selection == one transaction; reconnect never duplicates ----

    async def test_one_selection_is_exactly_one_apply_transaction(self) -> None:
        self._save_ms48_preset()
        radio = self.radio()
        app = MeshtasticPassApp(radio, self.settings)
        buffer = io.StringIO()
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            spies = spy_write_paths(radio)
            with contextlib.redirect_stdout(buffer):
                app._on_network_selected("NYC MS48")
                await self._wait_apply_settled(pilot, app)
                status = str(
                    app.query_one("#advanced-radio-status", Static).render()
                )
                self.assertIn("APPLIED", status)
                self.assertEqual(spies["apply_network_config"].call_count, 1)

                # A later reconnect must not repeat any write/commit.
                radio.simulate_reconnect()
                await self._wait_reconnected(pilot, app)
                for _ in range(5):
                    await pilot.pause()
            self.assertEqual(spies["apply_network_config"].call_count, 1)
            self.assertEqual(spies["write_verified_config_field"].call_count, 0)
            self.assertEqual(spies["write_verified_primary_channel"].call_count, 0)
            self.assertEqual(app._selected_network, "NYC MS48")
        # The staged transaction ran exactly once end to end -- one
        # BEGIN, one COMMIT -- for the one user selection, with nothing
        # repeated by the reconnect afterwards.
        printed = buffer.getvalue()
        self.assertEqual(printed.count("begin START"), 1)
        self.assertEqual(printed.count("commit START"), 1)
        self.assertEqual(printed.count("REQUEST name="), 1)

    async def test_reconnect_clock_sync_skips_tzdef_write_when_unchanged(
        self,
    ) -> None:
        # Unexpected-reboot audit: with CLOCK SYNC enabled, every new
        # connection lifecycle used to rewrite device.tzdef even when
        # the radio already had the host's value -- a redundant
        # reconnect-time config write. It must be skipped when equal.
        self.settings.clock_auto_sync = True
        radio = self.radio()
        radio._config_sections["device"]["tzdef"] = "EST5EDT,M3.2.0,M11.1.0"
        with patch(
            "app.detect_host_timezone",
            return_value=HostTimezone(
                "America/New_York",
                "EST5EDT,M3.2.0,M11.1.0",
                "test",
                "patched for test",
            ),
        ):
            app = MeshtasticPassApp(radio, self.settings)
            async with app.run_test(size=(110, 40)) as pilot:
                await self._wait_online(pilot, app)
                spies = spy_write_paths(radio)
                radio.simulate_reconnect()
                await self._wait_reconnected(pilot, app)
                for _ in range(10):
                    await pilot.pause(0.05)
                # The epoch sync still runs once per lifecycle; the
                # already-correct tzdef is NOT rewritten.
                self.assertEqual(spies["write_verified_config_field"].call_count, 0)


class NetworkSuccessAutoDismissTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.settings.save_radio_config_preset(
            RadioConfigPreset(
                name="NYC MS48",
                modem_preset="MEDIUM_SLOW",
                frequency_slot=48,
                channel_psk_base64="AQ==",
            )
        )
        for attr, value in (
            ("_NETWORK_VERIFY_SETTLE_SECONDS", 0.0),
            ("_NETWORK_VERIFY_INTERVAL_SECONDS", 0.02),
            ("_NETWORK_VERIFY_DEADLINE_SECONDS", 0.4),
        ):
            original = getattr(MeshtasticPassApp, attr)
            setattr(MeshtasticPassApp, attr, value)
            self.addCleanup(setattr, MeshtasticPassApp, attr, original)

    def _patch_dismiss_seconds(self, seconds: float) -> None:
        original = app_module.NETWORK_STATUS_SUCCESS_DISMISS_SECONDS
        app_module.NETWORK_STATUS_SUCCESS_DISMISS_SECONDS = seconds
        self.addCleanup(
            setattr,
            app_module,
            "NETWORK_STATUS_SUCCESS_DISMISS_SECONDS",
            original,
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

    @staticmethod
    async def _wait_apply_settled(pilot, app, tries: int = 120) -> None:
        for _ in range(tries):
            await pilot.pause(0.05)
            if app._network_apply is None:
                return

    @staticmethod
    def _status_text(app) -> str:
        return str(app.query_one("#advanced-radio-status", Static).render())

    async def _apply_ms48(self, pilot, app) -> None:
        app._on_network_selected("NYC MS48")
        await self._wait_apply_settled(pilot, app)

    async def test_success_message_remains_then_clears_after_the_window(
        self,
    ) -> None:
        self._patch_dismiss_seconds(0.3)
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._apply_ms48(pilot, app)
            self.assertIn("NYC MS48 APPLIED", self._status_text(app))
            # Still visible immediately after success...
            await pilot.pause(0.05)
            self.assertIn("NYC MS48 APPLIED", self._status_text(app))
            # ...and cleared once the dismiss window has passed.
            for _ in range(20):
                await pilot.pause(0.05)
                if not self._status_text(app):
                    break
            self.assertEqual(self._status_text(app), "")
            self.assertIsNone(app._network_status_dismiss_timer)

    async def test_default_dismiss_window_is_ten_seconds(self) -> None:
        self.assertEqual(app_module.NETWORK_STATUS_SUCCESS_DISMISS_SECONDS, 10.0)

    async def test_stale_success_timer_cannot_clear_a_newer_status(self) -> None:
        self._patch_dismiss_seconds(0.3)
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            await self._apply_ms48(pilot, app)
            self.assertIn("NYC MS48 APPLIED", self._status_text(app))
            self.assertIsNotNone(app._network_status_dismiss_timer)
            # A NEWER status/error arrives inside the dismiss window --
            # the pending success dismiss must be disarmed by the write
            # itself, never left to clear the newer message.
            app._set_advanced_radio_status("NEWER ERROR", "setting-error")
            self.assertIsNone(app._network_status_dismiss_timer)
            for _ in range(15):
                await pilot.pause(0.05)
            self.assertIn("NEWER ERROR", self._status_text(app))

    async def test_error_status_is_never_auto_cleared(self) -> None:
        self._patch_dismiss_seconds(0.2)
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            # A selection while the preset is unknown -> a genuine
            # error status with NO success timer armed.
            app._on_network_selected("NO SUCH PRESET")
            await pilot.pause()
            self.assertIn("UNKNOWN NETWORK", self._status_text(app))
            self.assertIsNone(app._network_status_dismiss_timer)
            for _ in range(15):
                await pilot.pause(0.05)
            self.assertIn("UNKNOWN NETWORK", self._status_text(app))


class NetworkTerminologyAndNavigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
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

    async def test_selector_label_is_preset_and_editor_keeps_network_name(
        self,
    ) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            rendered = str(app.query_one(NetworkSelector).render())
            self.assertIn("PRESET", rendered)
            self.assertNotIn("NETWORK", rendered)
            new_preset = str(app.query_one(NewNetworkControl).render())
            self.assertIn("[ NEW PRESET ]", new_preset)
            # The editor's NETWORK NAME field is deliberately NOT renamed.
            labels = [
                str(label.render())
                for label in app.query_one("#network-name-row").query(Static)
            ]
            self.assertIn("NETWORK NAME", labels)

    async def test_connection_nav_order_follows_the_reordered_sections(
        self,
    ) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            order = [type(c).__name__ for c in app._connection_nav_controls()]
            self.assertEqual(
                order,
                [
                    # CONNECTION
                    "DeviceSelector",
                    "LongNameControl",
                    "ShortNameControl",
                    # NETWORK (collapsed)
                    "NetworkSelector",
                    "NewNetworkControl",
                    # RADIO
                    "RoleSelector",
                    "BluetoothSelector",
                    "TimezoneSelector",
                    "ScreenTimeoutSelector",
                    "UnitsSelector",
                    "CompassSelector",
                    "FlipScreenSelector",
                    "Clock24HSelector",
                    "HopLimitSelector",
                    "AutoSyncSelector",
                    # STYLE
                    "FontSizeSelector",
                    "ColorSelector",
                ],
            )

    async def test_open_editor_nav_slots_between_preset_and_radio_sections(
        self,
    ) -> None:
        app = MeshtasticPassApp(self.radio(), self.settings)
        async with app.run_test(size=(110, 40)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("connection")
            await pilot.pause()
            app.query_one(NewNetworkControl).focus()
            await pilot.press("enter")
            await pilot.pause()
            order = [
                getattr(c, "id", None) or type(c).__name__
                for c in app._connection_nav_controls()
            ]
            selector_index = order.index("advanced-radio-network-selector")
            self.assertEqual(
                order[selector_index : selector_index + 6],
                [
                    "advanced-radio-network-selector",
                    "network-name-input",
                    "advanced-radio-mode-selector",
                    "freq-slot-input",
                    "key-input",
                    "advanced-radio-save",
                ],
            )
            self.assertEqual(order[selector_index + 6], "radio-role-selector")


if __name__ == "__main__":
    unittest.main()
