"""App-level tests for DM management: CTRL+D delete and NEW DM by node ID.

Covers the "NEW DM" dropdown action and the transient node-ID entry
surface, plus deleting the DM conversation currently being viewed -- all
keyed by canonical node ID, never display name, and zero-RF throughout.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from textual.color import Color
from textual.widgets import Static

from app import (
    DMModeSelector,
    MeshtasticPassApp,
    NEW_DM_ACTION_VALUE,
    MeshNodeLabelWidget,
    MeshTopologyView,
    _mesh_select_node,
    canonical_entered_node_id,
)
from app_settings import AppSettings
from chat_store import ChatStore
from mesh_state import MeshNodeState
from radio_service import NodeMetadata, ReceivedMessage
from simulated_radio_service import SimulatedRadioService
from tests.test_app import ControllableSendRadioService
from theme_palette import THEME_PALETTES


def _simulated_radio(**kwargs) -> SimulatedRadioService:
    return SimulatedRadioService(
        connect_delay=0, message_interval=0, scripted_messages=(), **kwargs
    )


class CanonicalNodeIdTests(unittest.TestCase):
    def test_accepts_bang_prefixed_hex_case_insensitive(self) -> None:
        self.assertEqual(canonical_entered_node_id("!a11ce001"), "!a11ce001")
        self.assertEqual(canonical_entered_node_id("!A11CE001"), "!a11ce001")

    def test_accepts_bare_eight_hex_digits(self) -> None:
        self.assertEqual(canonical_entered_node_id("a11ce001"), "!a11ce001")

    def test_rejects_invalid(self) -> None:
        for bad in ("", "   ", "!123", "123456789", "!zzzzzzzz", "!1234abcd!", "hello"):
            self.assertIsNone(canonical_entered_node_id(bad), bad)


class DmManageAppTestsBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.chat_db_path = self.root / "chat.db"
        # A single store instance shared by the app and the assertions so
        # the test always observes the SAME database the app writes to
        # (the app otherwise opens the XDG-honored user path).
        self.store = ChatStore.open(self.chat_db_path)
        self.addCleanup(self.store.close)

    def _make_app(self, radio=None):
        return MeshtasticPassApp(
            radio or _simulated_radio(),
            self.settings,
            chat_store=self.store,
        )


class NewDmTests(DmManageAppTestsBase):
    async def test_new_dm_row_is_present_but_not_a_persisted_conversation(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.press("d")
            await pilot.pause()
            selector = app.query_one(DMModeSelector)
            labels = [item.label for item in selector.popup.items]
            self.assertIn("NEW DM", labels)
            self.assertEqual(labels[-1], "NEW DM")
            self.assertIsNone(app.current_dm_node_id)
            self.assertFalse(app._new_dm_mode)

    async def test_selecting_new_dm_opens_entry_state_with_instruction(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.press("d")
            await pilot.pause()
            # Move the highlight to the NEW DM row (last) and activate it.
            selector = app.query_one(DMModeSelector)
            for _ in range(len(selector.popup.items) - 1):
                await pilot.press("down")
                await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            self.assertTrue(app._new_dm_mode)
            self.assertEqual(app._chat_mode, "dms")
            self.assertIsNone(app.current_dm_node_id)
            header = str(app.query_one("#dm-header").render())
            self.assertEqual(header, "DM / NEW")
            instruction = app.query_one("#dm-new-instruction", Static)
            self.assertTrue(instruction.display)
            self.assertEqual(
                str(instruction.render()),
                "TYPE THE NODE ID AND HIT ENTER TO CREATE DM",
            )

    async def test_valid_node_id_creates_local_dm_with_zero_rf(self) -> None:
        radio = ControllableSendRadioService()
        app = self._make_app(radio)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._start_new_dm()
            await pilot.pause()
            dm_input = app.query_one("#dm-input")
            dm_input.focus()
            await pilot.pause()
            await pilot.press("!", "1", "2", "3", "4", "a", "b", "c", "d")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            self.assertFalse(app._new_dm_mode)
            self.assertEqual(app.current_dm_node_id, "!1234abcd")
            self.assertEqual(app._chat_mode, "dms")
            self.assertEqual(radio.sent_texts, [])
            # A freshly created DM with no message yet is in-memory only --
            # nothing to persist until a real message is sent or received.
            self.assertEqual(self.store.list_dm_conversations(), [])

    async def test_invalid_node_id_shows_error_and_creates_nothing(self) -> None:
        radio = ControllableSendRadioService()
        app = self._make_app(radio)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._start_new_dm()
            await pilot.pause()
            dm_input = app.query_one("#dm-input")
            dm_input.focus()
            await pilot.pause()
            await pilot.press("n", "o", "p", "e")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            self.assertTrue(app._new_dm_mode)
            self.assertIsNone(app.current_dm_node_id)
            self.assertEqual(radio.sent_texts, [])
            self.assertEqual(
                str(app.query_one("#dm-send-error").render()), "INVALID NODE ID"
            )

    async def test_known_node_resolves_long_name_immediately(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._start_new_dm()
            await pilot.pause()
            dm_input = app.query_one("#dm-input")
            dm_input.focus()
            await pilot.pause()
            # SIMULATED_NODES[0] is !a11ce001 = "Alice Trail"/"ALCE".
            await pilot.press("!", "a", "1", "1", "c", "e", "0", "0", "1")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(app.current_dm_node_id, "!a11ce001")
            header = str(app.query_one("#dm-header").render())
            self.assertIn("Alice", header)
            self.assertIn("!a11ce001", header)

    async def test_unknown_node_falls_back_to_node_id(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._start_new_dm()
            await pilot.pause()
            dm_input = app.query_one("#dm-input")
            dm_input.focus()
            await pilot.pause()
            await pilot.press("!", "d", "e", "a", "d", "b", "e", "e", "f")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            self.assertEqual(app.current_dm_node_id, "!deadbeef")
            header = str(app.query_one("#dm-header").render())
            self.assertEqual(header, "DM / !deadbeef")

    async def test_esc_cancels_new_dm_with_zero_rf(self) -> None:
        radio = ControllableSendRadioService()
        app = self._make_app(radio)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._start_new_dm()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            self.assertFalse(app._new_dm_mode)
            self.assertIsNone(app.current_dm_node_id)
            self.assertEqual(app._chat_mode, "dms")
            self.assertEqual(radio.sent_texts, [])

    async def test_empty_dm_list_still_exposes_new_dm(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.press("d")
            await pilot.pause()
            selector = app.query_one(DMModeSelector)
            labels = [item.label for item in selector.popup.items]
            self.assertEqual(labels, ["NO DMS", "NEW DM"])


class DeleteDmTests(DmManageAppTestsBase):
    def _seed(self, node_id: str, text: str = "hi", packet_id: int = 1) -> None:
        self.store.add_incoming(
            packet_id=packet_id,
            node_id=node_id,
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text=text,
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id=node_id,
        )

    async def test_ctrl_d_deletes_current_dm_and_persists(self) -> None:
        self._seed("!a11ce001")
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app.open_dm("!a11ce001", long_name="Alice", short_name="ALC")
            await pilot.pause()
            self.assertEqual(app.current_dm_node_id, "!a11ce001")

            await pilot.press("ctrl+d")
            await pilot.pause()

            self.assertIsNone(app.current_dm_node_id)
            self.assertFalse(app._new_dm_mode)
            self.assertEqual(self.store.list_dm_conversations(), [])

    async def test_ctrl_d_is_a_no_op_outside_dm_conversation(self) -> None:
        self._seed("!a11ce001")
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            await pilot.press("ctrl+d")
            await pilot.pause()
            self.assertIsNone(app.current_dm_node_id)
            self.assertEqual(
                [node_id for node_id, _t in self.store.list_dm_conversations()],
                ["!a11ce001"],
            )

    async def test_delete_does_not_touch_channel_history(self) -> None:
        self.store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="channel message",
            radio_rx_at=100.0,
            received_at=100.0,
        )
        self.store.add_incoming(
            packet_id=2,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="dm message",
            radio_rx_at=200.0,
            received_at=200.0,
            dm_node_id="!a11ce001",
        )
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app.open_dm("!a11ce001", long_name="Alice", short_name="ALC")
            await pilot.pause()
            await pilot.press("ctrl+d")
            await pilot.pause()

            self.assertEqual(
                [m.text for m in self.store.load_recent_page(channel_index=0).messages],
                ["channel message"],
            )

    async def test_future_traffic_recreates_deleted_dm(self) -> None:
        self._seed("!a11ce001")
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app.open_dm("!a11ce001", long_name="Alice", short_name="ALC")
            await pilot.pause()
            await pilot.press("ctrl+d")
            await pilot.pause()

            app._accept_received_dm(
                ReceivedMessage(
                    sender_node_id="!a11ce001",
                    sender_long_name="Alice",
                    sender_short_name="ALC",
                    channel_index=0,
                    text="hello again",
                    rssi=None,
                    snr=None,
                    packet_id=99,
                    radio_rx_at=300.0,
                    is_direct=True,
                )
            )
            await pilot.pause()
            self.assertEqual(
                [node_id for node_id, _t in self.store.list_dm_conversations()],
                ["!a11ce001"],
            )


class ConnectionSectionSpacingTests(DmManageAppTestsBase):
    async def test_every_section_has_exactly_one_blank_line_above_it(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            for title_id in ("advanced-radio-title", "radio-title", "style-title"):
                title = app.query_one(f"#{title_id}")
                self.assertEqual(
                    title.styles.margin.top,
                    1,
                    f"{title_id} must have exactly one blank line of separation",
                )


class MeshHighlightLabelTests(DmManageAppTestsBase):
    async def test_highlighted_mesh_node_label_uses_accent2(self) -> None:
        import time as _time

        self.settings.set_favorite("!a11ce001", True)
        app = self._make_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            you_id = app.radio.info.node_id
            now = _time.time()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True),
                NodeMetadata("!a11ce001", "Alice Trail", "ALCE", 1, last_heard=now - 5),
            )
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            label = next(
                w for w in app.query(MeshNodeLabelWidget) if w.node_id == "!a11ce001"
            )
            self.assertEqual(
                label.render().spans[0].style.foreground,
                Color.parse(THEME_PALETTES[app._current_theme].accent2),
            )


if __name__ == "__main__":
    unittest.main()