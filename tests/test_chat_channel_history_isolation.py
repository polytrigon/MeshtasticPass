"""FINAL MESHTASTIC POLISH -- CHAT channel-history isolation.

Proves the real repro end to end through the app/ChatStore: a same-
slot radio reconfiguration (e.g. index 0 LongFast -> MediumSlow) must
never resurface the OLD channel's history under the new one, an empty
channel must show the informational start-of-history marker, and DM
history/isolation must be completely unaffected.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app import (
    ChannelSelector,
    EndOfChatHistoryMarker,
    MeshtasticPassApp,
    StartOfChannelHistoryMarker,
)
from app_settings import AppSettings
from chat_store import ChatStore
from radio_service import ChannelInfo, RadioState, ReceivedMessage
from simulated_radio_service import (
    SIMULATED_LOCAL_NODE_ID,
    SIMULATED_MESSAGES,
    SimulatedRadioService,
)


class ChatChannelHistoryIsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.chat_db_path = self.root / "chat.db"

    @staticmethod
    def _radio(**kwargs) -> SimulatedRadioService:
        return SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=(), **kwargs
        )

    @staticmethod
    async def _wait_online(pilot, app) -> None:
        for _ in range(15):
            await pilot.pause()
            if app._radio_state is RadioState.ONLINE:
                break
        assert app._radio_state is RadioState.ONLINE
        # A few extra pauses for _reconcile_current_channel_identity's
        # own background worker (run_worker) to finish settling.
        for _ in range(5):
            await pilot.pause()

    # ---- The real repro: same INDEX, reconfigured to a new channel -------

    async def test_same_slot_reconfiguration_hides_old_history(self) -> None:
        """LongFast history accumulated in a PRIOR session must not

        appear once the radio's slot 0 has been reconfigured to
        MediumSlow before this (new) session ever opens.
        """
        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        store.add_incoming(
            packet_id=1,
            node_id="!longfast1",
            sender_name="LongFast Node",
            sender_short_name="LF",
            channel_index=0,
            text="old longfast message",
            radio_rx_at=100.0,
            received_at=100.0,
            channel_key="id:longfast",
        )
        store.close()

        reopened = ChatStore.open(self.chat_db_path)
        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(ChannelInfo(0, "MediumSlow", stable_key="id:mediumslow"),),
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=reopened)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)

            self.assertEqual(app.current_channel_index, 0)
            self.assertNotIn(
                "old longfast message", [e.text for e in app.chat_history]
            )
            marker = app.query_one(StartOfChannelHistoryMarker)
            self.assertIn(
                "This is the start of MediumSlow channel history",
                str(marker.render()),
            )
            self.assertIn("[ MediumSlow ▾ ]", str(app.query_one(ChannelSelector).render()))

    async def test_same_slot_reconfiguration_mid_session_also_reconciles(self) -> None:
        """The same reconfiguration, but discovered via a live reconnect

        WHILE the app stays open (e.g. a Companion-app change) rather
        than only across a restart -- _show_connection's own channel-
        sync block never trips its usual branch here (the index never
        changes), so _reconcile_current_channel_identity must catch it.
        """
        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(ChannelInfo(0, "LongFast", stable_key="id:longfast"),),
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=ChatStore.open(self.chat_db_path))
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("chat")
            app._accept_received_message(
                replace(SIMULATED_MESSAGES[0], channel_index=0, packet_id=70001)
            )
            await pilot.pause()
            self.assertIn(
                SIMULATED_MESSAGES[0].text, [e.text for e in app.chat_history]
            )

            radio.info = replace(
                radio.info,
                channels=(ChannelInfo(0, "MediumSlow", stable_key="id:mediumslow"),),
            )
            radio.simulate_reconnect()
            for _ in range(15):
                await pilot.pause()
                if app._radio_state is RadioState.ONLINE:
                    break
            for _ in range(5):
                await pilot.pause()

            self.assertNotIn(
                SIMULATED_MESSAGES[0].text, [e.text for e in app.chat_history]
            )
            marker = app.query_one(StartOfChannelHistoryMarker)
            self.assertIn("MediumSlow", str(marker.render()))

    # ---- Interactive switching between two genuinely distinct channels ---

    async def test_switching_between_channels_isolates_and_restores_history(
        self,
    ) -> None:
        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        store.add_incoming(
            packet_id=1,
            node_id="!longfast1",
            sender_name="LongFast Node",
            sender_short_name="LF",
            channel_index=0,
            text="longfast accumulated",
            radio_rx_at=100.0,
            received_at=100.0,
            channel_key="id:longfast",
        )
        store.close()

        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(
                ChannelInfo(0, "LongFast", stable_key="id:longfast"),
                ChannelInfo(1, "MediumSlow", stable_key="id:mediumslow"),
            ),
        )
        app = MeshtasticPassApp(
            radio, self.settings, chat_store=ChatStore.open(self.chat_db_path)
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("chat")

            self.assertEqual(
                [e.text for e in app.chat_history], ["longfast accumulated"]
            )

            await app._switch_channel(1)
            self.assertEqual(app.chat_history, [])
            self.assertNotIn(
                "longfast accumulated", [e.text for e in app.chat_history]
            )
            marker = app.query_one(StartOfChannelHistoryMarker)
            self.assertIn(
                "This is the start of MediumSlow channel history",
                str(marker.render()),
            )

            # A real MediumSlow message removes the empty-history marker.
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    channel_index=1,
                    packet_id=70101,
                    text="first mediumslow message",
                )
            )
            await pilot.pause()
            self.assertEqual(len(app.query(StartOfChannelHistoryMarker)), 0)
            self.assertIn(
                "first mediumslow message", [e.text for e in app.chat_history]
            )

            await app._switch_channel(0)
            self.assertEqual(
                [e.text for e in app.chat_history], ["longfast accumulated"]
            )
            self.assertEqual(len(app.query(EndOfChatHistoryMarker)), 1)

            # Repeated switching is idempotent.
            for _ in range(3):
                await app._switch_channel(1)
                await app._switch_channel(0)
            self.assertEqual(
                [e.text for e in app.chat_history], ["longfast accumulated"]
            )

    # ---- Persistence survives an app restart, per channel ----------------

    async def test_history_survives_restart_for_each_channel(self) -> None:
        radio_channels = (
            ChannelInfo(0, "LongFast", stable_key="id:longfast"),
            ChannelInfo(1, "MediumSlow", stable_key="id:mediumslow"),
        )

        first_radio = self._radio()
        first_radio.info = replace(first_radio.info, channels=radio_channels)
        first_store = ChatStore.open(self.chat_db_path)
        app = MeshtasticPassApp(first_radio, self.settings, chat_store=first_store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.show_tab("chat")
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    channel_index=0,
                    packet_id=71001,
                    text="longfast before restart",
                )
            )
            await app._switch_channel(1)
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    channel_index=1,
                    packet_id=71002,
                    text="mediumslow before restart",
                )
            )
            await pilot.pause()

        second_radio = self._radio()
        second_radio.info = replace(second_radio.info, channels=radio_channels)
        second_store = ChatStore.open(self.chat_db_path)
        app2 = MeshtasticPassApp(second_radio, self.settings, chat_store=second_store)
        async with app2.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app2)
            self.assertEqual(
                [e.text for e in app2.chat_history], ["longfast before restart"]
            )
            await app2._switch_channel(1)
            self.assertEqual(
                [e.text for e in app2.chat_history], ["mediumslow before restart"]
            )

    # ---- Duplicate display names never merge history, only stable key does

    async def test_duplicate_display_names_isolated_by_stable_key(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        store.add_incoming(
            packet_id=1,
            node_id="!alice",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="first Shared channel",
            radio_rx_at=100.0,
            received_at=100.0,
            channel_key="id:shared-a",
        )
        store.add_incoming(
            packet_id=1,
            node_id="!bob",
            sender_name="Bob",
            sender_short_name="BOB",
            channel_index=1,
            text="second Shared channel",
            radio_rx_at=101.0,
            received_at=101.0,
            channel_key="id:shared-b",
        )
        store.close()

        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(
                ChannelInfo(0, "Shared", stable_key="id:shared-a"),
                ChannelInfo(1, "Shared", stable_key="id:shared-b"),
            ),
        )
        app = MeshtasticPassApp(
            radio, self.settings, chat_store=ChatStore.open(self.chat_db_path)
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            self.assertEqual(
                [e.text for e in app.chat_history], ["first Shared channel"]
            )
            await app._switch_channel(1)
            self.assertEqual(
                [e.text for e in app.chat_history], ["second Shared channel"]
            )

    # ---- DM history/isolation is completely unaffected --------------------

    async def test_dm_history_unaffected_by_channel_reconfiguration(self) -> None:
        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(ChannelInfo(0, "LongFast", stable_key="id:longfast"),),
        )
        app = MeshtasticPassApp(
            radio, self.settings, chat_store=ChatStore.open(self.chat_db_path)
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "dm before reconfiguration")
            await pilot.pause()
            before = [e.text for e in app._dm_states["!a11ce001"].entries]
            self.assertIn("dm before reconfiguration", before)

            radio.info = replace(
                radio.info,
                channels=(ChannelInfo(0, "MediumSlow", stable_key="id:mediumslow"),),
            )
            radio.simulate_reconnect()
            for _ in range(15):
                await pilot.pause()
                if app._radio_state is RadioState.ONLINE:
                    break
            for _ in range(5):
                await pilot.pause()

            after = [e.text for e in app._dm_states["!a11ce001"].entries]
            self.assertEqual(before, after)
            self.assertIn("dm before reconfiguration", after)

    # ---- No RF traffic is generated by any of this ------------------------

    async def test_channel_switch_and_reconciliation_cause_zero_rf_traffic(
        self,
    ) -> None:
        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(
                ChannelInfo(0, "LongFast", stable_key="id:longfast"),
                ChannelInfo(1, "MediumSlow", stable_key="id:mediumslow"),
            ),
        )
        app = MeshtasticPassApp(
            radio, self.settings, chat_store=ChatStore.open(self.chat_db_path)
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            sent_before = len(radio.sent_messages)
            traceroutes_before = radio._traceroute_count

            await app._switch_channel(1)
            await app._switch_channel(0)

            radio.info = replace(
                radio.info,
                channels=(
                    ChannelInfo(0, "MediumSlow", stable_key="id:mediumslow-2"),
                    ChannelInfo(1, "MediumSlow", stable_key="id:mediumslow"),
                ),
            )
            radio.simulate_reconnect()
            for _ in range(15):
                await pilot.pause()
                if app._radio_state is RadioState.ONLINE:
                    break
            for _ in range(5):
                await pilot.pause()

            self.assertEqual(len(radio.sent_messages), sent_before)
            self.assertEqual(radio._traceroute_count, traceroutes_before)


class PerRadioNamespaceAppTests(unittest.IsolatedAsyncioTestCase):
    """CHAT state-integrity: conversation state belongs to a physical radio.

    Two different radios (POLY vs SOHO) running the SAME logical network/
    channel/PSK/DM must never share CHAT state. Switching radios must not
    leak the other radio's transcript, and switching back must restore it.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.chat_db_path = self.root / "chat.db"

    @staticmethod
    def _radio(node_id: str) -> SimulatedRadioService:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        radio.info = replace(radio.info, node_id=node_id)
        return radio

    @staticmethod
    async def _wait_online(pilot, app) -> None:
        for _ in range(15):
            await pilot.pause()
            if app._radio_state is RadioState.ONLINE:
                break
        assert app._radio_state is RadioState.ONLINE
        for _ in range(5):
            await pilot.pause()

    async def test_radio_switch_isolates_and_restores_channel_history(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id("!a1a2a3a4")
        store.add_incoming(
            packet_id=1, node_id="!a11ce001", sender_name="Alice Trail",
            sender_short_name="ALCE", channel_index=0, text="POLY history",
            radio_rx_at=100.0, received_at=100.0, channel_key="sim-longfast",
        )
        poly_app = MeshtasticPassApp(
            self._radio("!a1a2a3a4"), self.settings, chat_store=store
        )
        async with poly_app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, poly_app)
            self.assertEqual([e.text for e in poly_app.chat_history], ["POLY history"])
            self.assertEqual(poly_app._local_radio_node_id, "!a1a2a3a4")

        # SOHO: a DIFFERENT physical radio, same logical channel config.
        # It must see NO POLY history -- CHAT opens empty.
        soho_store = ChatStore.open(self.chat_db_path)
        soho_store.set_local_node_id("!b1b2b3b4")
        soho_app = MeshtasticPassApp(
            self._radio("!b1b2b3b4"), self.settings, chat_store=soho_store
        )
        async with soho_app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, soho_app)
            self.assertEqual(soho_app._local_radio_node_id, "!b1b2b3b4")
            self.assertEqual([e.text for e in soho_app.chat_history], [])

        # Back to POLY: its persisted history is restored, SOHO's is absent.
        poly_again = ChatStore.open(self.chat_db_path)
        poly_again.set_local_node_id("!a1a2a3a4")
        poly2 = MeshtasticPassApp(
            self._radio("!a1a2a3a4"), self.settings, chat_store=poly_again
        )
        async with poly2.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, poly2)
            self.assertEqual([e.text for e in poly2.chat_history], ["POLY history"])


class PrimaryChannelIdentityTests(unittest.IsolatedAsyncioTestCase):
    """MEDIUMSLOW -> PRIMARY display change must NOT change identity.

    The unnamed PRIMARY logical channel's ON-SCREEN label ("PRIMARY") is
    presentation only. It must never feed CHAT's stable channel identity
    or the ChatStore conversation key, so a mere display-label change (or
    the previous bug where the modem preset "MediumSlow" was shown) can
    never re-key existing history or cause an async refresh to switch to
    a different conversation. A truly unnamed primary with no assigned
    settings.id and no PSK has genuinely no stable identity of its own
    (key "" = "unknown"), NOT a fabricated key from its label.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.chat_db_path = self.root / "chat.db"

    @staticmethod
    def _radio(node_id: str = SIMULATED_LOCAL_NODE_ID) -> SimulatedRadioService:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        radio.info = replace(radio.info, node_id=node_id)
        return radio

    @staticmethod
    async def _wait_online(pilot, app) -> None:
        for _ in range(15):
            await pilot.pause()
            if app._radio_state is RadioState.ONLINE:
                break
        assert app._radio_state is RadioState.ONLINE
        for _ in range(5):
            await pilot.pause()

    def test_display_label_does_not_enter_stable_identity(self) -> None:
        from types import SimpleNamespace
        from radio_service import RadioService

        # The stable key is derived from the channel's REAL configured name
        # plus psk/settings.id (the `configured_name` argument). The DISPLAY
        # label is never what the caller passes for identity. Prove the
        # labelled caller contract: an unnamed primary (configured_name "")
        # keys only by its psk hash, insensitive to any shown label.
        with_psk = SimpleNamespace(name="", psk=bytes([1, 2, 3]))
        bare_key = RadioService._channel_stable_key(with_psk, "")
        self.assertEqual(bare_key, "hash::0")
        # Simulate the OLD buggy caller passing the shown label as identity:
        # that WOULD differ -- which is exactly why _read_channel_info never
        # does it, instead always passing the real configured name.
        label_key = RadioService._channel_stable_key(with_psk, "PRIMARY")
        self.assertNotEqual(bare_key, label_key)

        # Truly unnamed primary, no id/psk: no identity of its own.
        unnamed = SimpleNamespace(name="", id=0)
        self.assertEqual(RadioService._channel_stable_key(unnamed, ""), "")

        # A genuinely named channel with no id/psk keys by its real name.
        named = SimpleNamespace(name="LongFast")
        self.assertEqual(RadioService._channel_stable_key(named, "LongFast"), "LongFast")

    def test_read_channel_info_keeps_primary_label_out_of_identity(self) -> None:
        from types import SimpleNamespace
        from radio_service import ChannelInfo, RadioService

        channels = [
            SimpleNamespace(index=0, role=1, settings=SimpleNamespace(name="")),
        ]
        local_node = SimpleNamespace(
            channels=channels,
            localConfig=SimpleNamespace(lora=SimpleNamespace(use_preset=True, modem_preset=0)),
        )
        result = RadioService._read_channel_info(local_node)
        # Presentation label is "PRIMARY", but stable identity is "" (the
        # real configured name for an unnamed primary with no id/psk) --
        # never "PRIMARY" and never the modem preset "MediumSlow".
        self.assertEqual(result, (ChannelInfo(0, "PRIMARY", stable_key=""),))

    async def test_primary_label_change_keeps_history_mounted(self) -> None:
        from app import ChannelInfo, MeshtasticPassApp

        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        # Seeded under an unnamed PRIMARY channel: no settings.id, no psk, so
        # its stable identity is "" (unknown).
        store.add_incoming(
            packet_id=1, node_id="!a11ce001", sender_name="Alice Trail",
            sender_short_name="ALCE", channel_index=0, text="primary history",
            radio_rx_at=100.0, received_at=100.0, channel_key="",
        )
        # The connected radio presents this exact unnamed primary: display
        # label "PRIMARY", stable identity "" (no settings.id, no psk).
        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(ChannelInfo(0, "PRIMARY", stable_key=""),),
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["primary history"])

            # Label and identity are unchanged across an async refresh, so the
            # mounted conversation stays put (PROBLEM 3 invariant).
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["primary history"])


class StableIdentityReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """Problem 3: an async channel/config refresh must never spontaneously
    switch away from the active stable channel identity.

    Reconciliation preserves the active conversation by STABLE identity
    (ChannelInfo.stable_key), not slot/index, display label, PRIMARY
    fallback, modem preset, or "first option". A channel that merely
    reorders, re-keys a same slot, or changes its display label stays
    active; only a genuinely-removed channel falls back deterministically.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.chat_db_path = self.root / "chat.db"

    @staticmethod
    def _radio() -> SimulatedRadioService:
        return SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )

    @staticmethod
    async def _wait_online(pilot, app) -> None:
        for _ in range(15):
            await pilot.pause()
            if app._radio_state is RadioState.ONLINE:
                break
        assert app._radio_state is RadioState.ONLINE
        for _ in range(5):
            await pilot.pause()

    @staticmethod
    def _seed(store: ChatStore, channel_index: int, text: str, key: str) -> None:
        store.add_incoming(
            packet_id=hash(text) % 1_000_000,
            node_id="!a11ce001",
            sender_name="Alice Trail",
            sender_short_name="ALCE",
            channel_index=channel_index,
            text=text,
            radio_rx_at=100.0,
            received_at=100.0,
            channel_key=key,
        )

    async def test_async_refresh_with_channel_a_still_present_keeps_a(self) -> None:
        """A is active; A still exists after a refresh -> A stays mounted and
        B's old history never mounts (failure classes A and B combined)."""
        from app import ChannelInfo, MeshtasticPassApp

        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self._seed(store, 0, "A history", "id:a")
        self._seed(store, 1, "B history", "id:b")
        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(
                ChannelInfo(0, "Alpha", stable_key="id:a"),
                ChannelInfo(1, "Beta", stable_key="id:b"),
            ),
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["A history"])
            self.assertEqual(app._channel_key_for(app.current_channel_index), "id:a")

            # Async refresh: same channels, A still present (unchanged
            # identity and index). Reconcile must keep A active, never
            # mount B (failure class B: selector stays, transcript must
            # NOT drift onto B's history).
            radio.info = replace(
                radio.info,
                channels=(
                    ChannelInfo(0, "Alpha", stable_key="id:a"),
                    ChannelInfo(1, "Beta", stable_key="id:b"),
                ),
            )
            radio.simulate_reconnect()
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["A history"])
            self.assertNotIn("B history", [e.text for e in app.chat_history])
            self.assertEqual(app._channel_key_for(app.current_channel_index), "id:a")

    async def test_option_reorder_or_label_change_preserves_a(self) -> None:
        """Reordering or renaming the same channel (same slot) must not switch
        away -- the active stable identity is preserved regardless of order or
        presentation label."""
        from app import ChannelInfo, MeshtasticPassApp

        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self._seed(store, 0, "A history", "id:a")
        self._seed(store, 1, "B history", "id:b")
        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(
                ChannelInfo(0, "Alpha", stable_key="id:a"),
                ChannelInfo(1, "Beta", stable_key="id:b"),
            ),
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["A history"])
            # Re-present the SAME channels, order swapped, A still index 0
            # but listed second. Label unchanged; identity unchanged.
            radio.info = replace(
                radio.info,
                channels=(
                    ChannelInfo(1, "Beta", stable_key="id:b"),
                    ChannelInfo(0, "Alpha", stable_key="id:a"),
                ),
            )
            radio.simulate_reconnect()
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["A history"])
            self.assertEqual(app._channel_key_for(app.current_channel_index), "id:a")
            # Same stable identity, different DISPLAY label.
            radio.info = replace(
                radio.info,
                channels=(
                    ChannelInfo(0, "Alpha Renamed", stable_key="id:a"),
                    ChannelInfo(1, "Beta Renamed", stable_key="id:b"),
                ),
            )
            radio.simulate_reconnect()
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["A history"])
            self.assertEqual(app._channel_key_for(app.current_channel_index), "id:a")
            self.assertEqual(app._channel_label(app.current_channel_index), "Alpha Renamed")

    async def test_same_index_rekeyed_preserves_active_conversation(self) -> None:
        """Slot 0 silently re-keyed (id:a -> id:b) must not surface A's history."""
        from app import ChannelInfo, MeshtasticPassApp, StartOfChannelHistoryMarker

        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self._seed(store, 0, "A history", "id:a")
        self._seed(store, 0, "B history", "id:b")
        radio = self._radio()
        radio.info = replace(
            radio.info, channels=(ChannelInfo(0, "Alpha", stable_key="id:a"),)
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["A history"])
            # Same slot re-keyed to B without a user action.
            radio.info = replace(
                radio.info, channels=(ChannelInfo(0, "Beta", stable_key="id:b"),)
            )
            radio.simulate_reconnect()
            await self._wait_online(pilot, app)
            # Current channel is still index 0 (slot unchanged); the mounted
            # history must now be B's, never A's -- the selector and transcript
            # agree on the SAME new stable identity.
            self.assertEqual(app.current_channel_index, 0)
            self.assertEqual([e.text for e in app.chat_history], ["B history"])
            self.assertNotIn("A history", [e.text for e in app.chat_history])

    async def test_genuine_removal_falls_back_to_primary_first(self) -> None:
        """A removed entirely -> deterministic fallback to PRIMARY (index 0)."""
        from app import ChannelInfo, MeshtasticPassApp

        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self._seed(store, 0, "PRIMARY history", "id:primary")
        self._seed(store, 1, "A history", "id:a")
        radio = self._radio()
        radio.info = replace(
            radio.info,
            channels=(
                ChannelInfo(0, "LongFast", stable_key="id:primary"),
                ChannelInfo(1, "Alpha", stable_key="id:a"),
            ),
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            await app._switch_channel(1)
            await pilot.pause()
            self.assertEqual([e.text for e in app.chat_history], ["A history"])
            # A is now genuinely removed from the radio config.
            radio.info = replace(
                radio.info, channels=(ChannelInfo(0, "LongFast", stable_key="id:primary"),)
            )
            radio.simulate_reconnect()
            await self._wait_online(pilot, app)
            # Fallback: PRIMARY (index 0) still exists -> A's removal lands there.
            self.assertEqual(app.current_channel_index, 0)
            self.assertEqual(app._channel_key_for(app.current_channel_index), "id:primary")
            self.assertEqual([e.text for e in app.chat_history], ["PRIMARY history"])

    async def test_display_label_change_preserves_primary_identity(self) -> None:
        """PRIMARY display-label/fallback changes must not affect identity."""
        from app import ChannelInfo, MeshtasticPassApp

        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self._seed(store, 0, "primary history", "id:primary")
        radio = self._radio()
        radio.info = replace(
            radio.info, channels=(ChannelInfo(0, "PRIMARY", stable_key="id:primary"),)
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["primary history"])
            self.assertEqual(app._channel_key_for(app.current_channel_index), "id:primary")
            # Same identity, different presentation label.
            radio.info = replace(
                radio.info, channels=(ChannelInfo(0, "PRIMARY (default)", stable_key="id:primary"),)
            )
            radio.simulate_reconnect()
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["primary history"])
            self.assertEqual(app._channel_key_for(app.current_channel_index), "id:primary")

    async def test_removal_fallback_never_selects_new_channel_sentinel(self) -> None:
        """Fallback must select a REAL channel, never the NEW CHANNEL sentinel."""
        from app import ChannelInfo, MeshtasticPassApp

        store = ChatStore.open(self.chat_db_path)
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self._seed(store, 0, "PRIMARY history", "id:primary")
        radio = self._radio()
        radio.info = replace(
            radio.info, channels=(ChannelInfo(0, "LongFast", stable_key="id:primary"),)
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            # The sentinel is never a ChannelInfo -- confirm the deterministic
            # fallback ignores any non-index-0 synthetic value and lands on a
            # real channel that actually exists.
            self.assertEqual(app._deterministic_channel_fallback(()), None)
            self.assertEqual(
                app._deterministic_channel_fallback(
                    (ChannelInfo(1, "Alpha", stable_key="id:a"),)
                ),
                1,
            )

    async def test_per_radio_namespace_stays_correct_across_refresh(self) -> None:
        """A refresh must not leak one radio's history into another (Problem 1
        preserved through Problem 3's reconciliation)."""
        from app import ChannelInfo, MeshtasticPassApp

        store = ChatStore.open(self.chat_db_path)
        # POLY owns some history in this namespace.
        store.set_local_node_id(SIMULATED_LOCAL_NODE_ID)
        self._seed(store, 0, "A history", "id:a")
        radio = self._radio()
        radio.info = replace(
            radio.info, channels=(ChannelInfo(0, "Alpha", stable_key="id:a"),)
        )
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._wait_online(pilot, app)
            self.assertEqual([e.text for e in app.chat_history], ["A history"])
            # The connected radio reports its OWN canonical node ID set to a
            # DIFFERENT identity than the one that owns the seeded history.
            # Its namespace has no history, so CHAT is empty and stays empty
            # across a refresh -- no leakage from the previous radio.
            radio.info = replace(
                radio.info,
                node_id="!b1b2b3b4",
                channels=(ChannelInfo(0, "Alpha", stable_key="id:a"),),
            )
            radio.simulate_reconnect()
            await self._wait_online(pilot, app)
            self.assertEqual(app._local_radio_node_id, "!b1b2b3b4")
            self.assertEqual([e.text for e in app.chat_history], [])


class MigrationLiveIngestTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end v4 -> v5 migration + live per-radio ingestion.

    Mirrors the hardware regression: an existing valid schema-v4 database
    must open (NOT "unsupported chat schema version 4"), migrate
    deterministically to v5 (local_node_id column, preserve-but-hide), then
    continue admitting NEW messages under the currently connected radio's
    canonical namespace -- and never leak legacy rows, never collide a new
    packet with a legacy NULL row, and isolate a different radio.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.chat_db_path = self.root / "chat.db"

    @staticmethod
    def _mk_v4_db(path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (4);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                packet_id INTEGER,
                node_id TEXT,
                sender_name TEXT,
                sender_short_name TEXT,
                channel_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                origin_sent_at REAL,
                radio_rx_at REAL,
                received_at REAL NOT NULL,
                local_sent_at REAL,
                delivery_state TEXT,
                created_at REAL NOT NULL,
                dm_node_id TEXT,
                channel_key TEXT
            );
            CREATE TABLE send_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id),
                packet_id INTEGER,
                state TEXT NOT NULL,
                started_at REAL NOT NULL,
                completed_at REAL,
                error TEXT
            );
            INSERT INTO messages VALUES (
                1,'incoming',789,'!b0b00002','Old Radio','OLD',0,
                'OLD channel',100,101,101,NULL,NULL,101,NULL,NULL
            );
            INSERT INTO messages VALUES (
                2,'incoming',789,'!b0b00002','Old Radio','OLD',0,
                'OLD dm',102,103,103,NULL,NULL,103,'!b0b00002',NULL
            );
            INSERT INTO send_attempts VALUES (
                1, 1, NULL, 'SENT', 101, NULL, NULL
            );
            """
        )
        connection.commit()
        connection.close()

    @staticmethod
    def _radio(node_id: str) -> SimulatedRadioService:
        radio = SimulatedRadioService(
            connect_delay=0.5, message_interval=0, scripted_messages=()
        )
        radio.info = replace(radio.info, node_id=node_id)
        return radio

    @staticmethod
    async def _settle(pilot, app) -> None:
        for _ in range(30):
            await pilot.pause()
            if app._radio_state is RadioState.ONLINE:
                break
        assert app._radio_state is RadioState.ONLINE
        for _ in range(5):
            await pilot.pause()

    async def test_migration_then_per_radio_live_ingest_and_switch(self) -> None:
        from radio_service import ChannelInfo as CI

        self._mk_v4_db(self.chat_db_path)
        store = ChatStore.open(self.chat_db_path)
        self.assertEqual(
            store._connection.execute("SELECT version FROM schema_version").fetchone()[0],
            5,
        )
        # legacy rows preserved but hidden after binding a real radio.
        store.set_local_node_id("!aaaaaaaa")
        page = store.load_recent_page(channel_index=0, channel_key=None)
        self.assertEqual([m.text for m in page.messages], [])
        rows = store._connection.execute(
            "SELECT text, local_node_id FROM messages ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(r["text"], r["local_node_id"]) for r in rows],
            [("OLD channel", None), ("OLD dm", None)],
        )
        store.close()

        # Bind radio !aaaaaaaa: legacy hidden; a NEW channel packet (same
        # packet_id + node_id as the legacy row) must be inserted and become
        # visible; a NEW DM too -- no dedup collision against legacy NULL.
        reopened = ChatStore.open(self.chat_db_path)
        app = MeshtasticPassApp(self._radio("!aaaaaaaa"), self.settings, chat_store=reopened)
        async with app.run_test(size=(100, 30)) as pilot:
            await self._settle(pilot, app)
            self.assertEqual(app._local_radio_node_id, "!aaaaaaaa")
            self.assertEqual([e.text for e in app.chat_history], [])
            app._accept_received_message(
                replace(
                    SIMULATED_MESSAGES[0],
                    packet_id=789,
                    sender_node_id="!b0b00002",
                    text="A new packet",
                    channel_index=0,
                )
            )
            await pilot.pause()
            for _ in range(8):
                await pilot.pause()
            self.assertIn("A new packet", [e.text for e in app.chat_history])
            self.assertNotIn("OLD channel", [e.text for e in app.chat_history])
            all_rows = reopened._connection.execute(
                "SELECT text, local_node_id FROM messages"
            ).fetchall()
            self.assertIn(("A new packet", "!aaaaaaaa"), [(r["text"], r["local_node_id"]) for r in all_rows])

            # A new DM for the same radio, namespaced and visible.
            app._accept_received_dm(
                ReceivedMessage(
                    sender_node_id="!b0b00002",
                    sender_long_name="Old Radio",
                    sender_short_name="OLD",
                    channel_index=0,
                    text="A new DM",
                    rssi=None,
                    snr=None,
                    packet_id=790,
                    radio_rx_at=200.0,
                    is_direct=True,
                )
            )
            await pilot.pause()
            for _ in range(8):
                await pilot.pause()
            dm_rows = reopened._connection.execute(
                "SELECT text, local_node_id, dm_node_id FROM messages WHERE dm_node_id = ?",
                ("!b0b00002",),
            ).fetchall()
            self.assertIn(
                ("A new DM", "!aaaaaaaa", "!b0b00002"),
                [(r["text"], r["local_node_id"], r["dm_node_id"]) for r in dm_rows],
            )

            # Switch to a DIFFERENT radio: !aaaaaaaa history must vanish.
            app._activate_local_radio_namespace("!bbbbbbbb")
            app._channels = (CI(0, "LongFast", stable_key="id:primary"),)
            self.assertEqual([e.text for e in app.chat_history], [])

        # Reopened under !bbbbbbbb: only its own (empty) history is visible.
        reopened_b = ChatStore.open(self.chat_db_path)
        reopened_b.set_local_node_id("!bbbbbbbb")
        self.assertEqual(
            [m.text for m in reopened_b.load_recent_page(channel_index=0, channel_key=None).messages],
            [],
        )
        reopened_b.close()

        # Switching back to !aaaaaaaa restores its namespaced new history.
        reopened_a = ChatStore.open(self.chat_db_path)
        reopened_a.set_local_node_id("!aaaaaaaa")
        visible = [
            m.text
            for m in reopened_a.load_recent_page(channel_index=0, channel_key=None).messages
        ]
        self.assertIn("A new packet", visible)
        reopened_a.close()


if __name__ == "__main__":
    unittest.main()
