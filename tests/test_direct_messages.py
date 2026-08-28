"""App-level tests for first-class Direct Messages: a conversation model

distinct from channel CHAT (item 1), keyed by the remote party's stable
node ID (item 2), reusing the existing composer/transcript/delivery-
state/resend/delete machinery.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import time
import unittest

from textual.widgets import Input

from app import ChatEntryWidget, MeshtasticPassApp, _mesh_select_node, MeshTopologyView
from app_settings import AppSettings
from chat_store import ChatStore
from radio_service import DeliveryState, NodeMetadata, ReceivedMessage, SendStatus
from simulated_radio_service import SimulatedRadioService
from tests.test_app import ControllableSendRadioService


def _simulated_radio(**kwargs) -> SimulatedRadioService:
    return SimulatedRadioService(
        connect_delay=0, message_interval=0, scripted_messages=(), **kwargs
    )


class DirectMessageAppTestsBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )
        self.chat_db_path = self.root / "chat.db"


class DMTabAndConversationTests(DirectMessageAppTestsBase):
    async def test_dm_is_not_the_channel_selector(self) -> None:
        """Item 1: DM is a distinct tab, not another channel-dropdown

        entry -- CHAT's channel selector never gains a DM option.
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("dm")
            await pilot.pause()
            self.assertEqual(app.current_tab, "dm")
            self.assertNotEqual(app.current_tab, "chat")

    async def test_open_dm_creates_and_switches_to_conversation(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice Trail", short_name="ALCE")
            await pilot.pause()
            self.assertEqual(app.current_tab, "dm")
            self.assertEqual(app.current_dm_node_id, "!a11ce001")
            self.assertEqual(
                app.query_one("#dm-content").current, "dm-conversation"
            )

    async def test_dm_header_identifies_target_with_stable_id(self) -> None:
        """Item 11: the stable node ID must be visible even when a

        display name is also shown, so duplicate names stay
        disambiguatable.
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!4de2d072", long_name="GSI Portable", short_name="GSI")
            await pilot.pause()
            header = str(app.query_one("#dm-header").render())
            self.assertIn("DM", header)
            self.assertIn("GSI Portable", header)
            self.assertIn("!4de2d072", header)

    async def test_dm_header_falls_back_to_node_id_when_no_name_known(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!00000001")
            await pilot.pause()
            header = str(app.query_one("#dm-header").render())
            self.assertEqual(header, "DM / !00000001")

    async def test_duplicate_display_names_remain_distinct_conversations(self) -> None:
        """Item 2: two different node IDs sharing a display name must

        never merge into one conversation.
        """
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!11111111", long_name="Basecamp", short_name="BC")
            await pilot.pause()
            app.query_one("#dm-input", Input).value = "to first Basecamp"
            app.open_dm("!22222222", long_name="Basecamp", short_name="BC")
            await pilot.pause()
            self.assertEqual(app.query_one("#dm-input", Input).value, "")
            self.assertIsNot(
                app._dm_state_for("!11111111"), app._dm_state_for("!22222222")
            )

    async def test_escape_from_transcript_returns_to_conversation_list(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice", short_name="ALC")
            await pilot.pause()
            app.query_one("#dm-log").focus()
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsNone(app.current_dm_node_id)
            self.assertEqual(app.query_one("#dm-content").current, "dm-list")

    async def test_escape_from_composer_returns_to_transcript_not_list(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice", short_name="ALC")
            await pilot.pause()
            app.query_one("#dm-input", Input).focus()
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.current_dm_node_id, "!a11ce001")
            self.assertIsInstance(app.focused, type(app.query_one("#dm-log")))


class DMConversationListTests(DirectMessageAppTestsBase):
    async def test_empty_list_shows_no_conversations_placeholder(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("dm")
            await pilot.pause()
            rows = [str(c.render()) for c in app.query_one("#dm-list").children]
            self.assertEqual(rows, ["No DM conversations yet."])

    async def test_list_sorted_by_most_recent_activity(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        self.addCleanup(store.close)
        app = MeshtasticPassApp(_simulated_radio(), self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!11111111", long_name="Older Chat")
            await pilot.pause()
            app.query_one("#dm-input", Input).value = "hi"
            await pilot.press("enter")
            await pilot.pause()
            app._close_dm_conversation()
            await pilot.pause()

            app.open_dm("!22222222", long_name="Newer Chat")
            await pilot.pause()
            app.query_one("#dm-input", Input).value = "hi"
            await pilot.press("enter")
            await pilot.pause()
            app._close_dm_conversation()
            await pilot.pause()

            self.assertEqual(
                app._dm_conversation_order, ["!22222222", "!11111111"]
            )

    async def test_up_down_moves_highlight_enter_opens(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        self.addCleanup(store.close)
        app = MeshtasticPassApp(_simulated_radio(), self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for node_id, name in (("!11111111", "First"), ("!22222222", "Second")):
                app.open_dm(node_id, long_name=name)
                await pilot.pause()
                app.query_one("#dm-input", Input).value = "hi"
                await pilot.press("enter")
                await pilot.pause()
                app._close_dm_conversation()
                await pilot.pause()

            self.assertEqual(app._dm_list_highlighted_index, 0)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app._dm_list_highlighted_index, 1)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.current_dm_node_id, app._dm_conversation_order[1])


class DMSendReceiveTests(DirectMessageAppTestsBase):
    async def test_send_targets_exact_stable_destination(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            app.query_one("#dm-input", Input).value = "hello alice"
            await pilot.press("enter")
            for _ in range(5):
                await pilot.pause()
            self.assertEqual(radio.sent_texts, ["hello alice"])

    async def test_send_never_falls_back_to_broadcast(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "hello")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            self.assertEqual(len(radio.pending_status_handlers), 1)
            # The exact packet was tracked with a real destination, never None.
            self.assertIsNotNone(entry.dm_node_id)

    async def test_exactly_one_send_per_explicit_send(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            app.query_one("#dm-input", Input).value = "one message"
            await pilot.press("enter")
            for _ in range(5):
                await pilot.pause()
            self.assertEqual(len(radio.sent_texts), 1)

    async def test_destination_ack_produces_heard(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "hello")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))
            radio.fire_status(packet_id, SendStatus(DeliveryState.HEARD, packet_id))
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.HEARD)

    async def test_local_implicit_ack_produces_sent(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "hello")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))
            radio.fire_status(packet_id, SendStatus(DeliveryState.SENT, packet_id))
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.SENT)

    async def test_nak_produces_failed(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "hello")
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))
            radio.fire_status(
                packet_id, SendStatus(DeliveryState.FAILED, packet_id, detail="NAK")
            )
            await pilot.pause()
            self.assertEqual(entry.delivery_state, DeliveryState.FAILED)

    async def test_late_ack_after_delete_cannot_resurrect(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "hello")
            entry.delivery_state = DeliveryState.FAILED
            app._send_from_thread(entry, entry.send_generation)
            for _ in range(5):
                await pilot.pause()
            packet_id = next(iter(radio.pending_status_handlers))

            app._delete_dm_entry(entry)
            await pilot.pause()
            self.assertTrue(entry.deleted)

            radio.fire_status(packet_id, SendStatus(DeliveryState.HEARD, packet_id))
            await pilot.pause()
            self.assertTrue(entry.deleted)
            self.assertNotIn(entry, app._dm_state_for("!a11ce001").entries)

    async def test_incoming_dm_to_local_node_opens_correct_conversation(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            incoming = ReceivedMessage(
                sender_node_id="!a11ce001",
                sender_long_name="Alice Trail",
                sender_short_name="ALCE",
                channel_index=0,
                text="direct reply",
                rssi=-80,
                snr=5.0,
                packet_id=42,
                radio_rx_at=time.time(),
                is_direct=True,
            )
            app._accept_received_message(incoming)
            await pilot.pause()
            texts = [str(w.message_label.render()) for w in app.query(ChatEntryWidget)]
            self.assertIn("direct reply", texts)

    async def test_packet_addressed_to_someone_else_is_not_shown_as_local_dm(
        self,
    ) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            not_for_us = ReceivedMessage(
                sender_node_id="!a11ce001",
                sender_long_name="Alice Trail",
                sender_short_name="ALCE",
                channel_index=0,
                text="relayed traffic",
                rssi=-80,
                snr=5.0,
                packet_id=43,
                radio_rx_at=time.time(),
                is_direct=False,
            )
            app._accept_received_message(not_for_us)
            await pilot.pause()
            self.assertIn("relayed traffic", [e.text for e in app.chat_history])
            self.assertEqual(app._dm_states, {})


class DMDraftAndResendDeleteTests(DirectMessageAppTestsBase):
    async def test_drafts_are_isolated_per_conversation(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            app.query_one("#dm-input", Input).value = "draft for alice"

            app.open_dm("!b0b00002", long_name="Bob")
            await pilot.pause()
            self.assertEqual(app.query_one("#dm-input", Input).value, "")

            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            self.assertEqual(
                app.query_one("#dm-input", Input).value, "draft for alice"
            )

    async def test_resend_reuses_existing_pending_send_machinery(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "will fail")
            entry.delivery_state = DeliveryState.FAILED
            first_generation = entry.send_generation
            app._rebroadcast(entry)
            await pilot.pause()
            self.assertEqual(entry.send_generation, first_generation + 1)
            self.assertEqual(entry.delivery_state, DeliveryState.SENDING)

    async def test_delete_is_local_only_and_generates_zero_rf_traffic(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "to delete")
            entry.delivery_state = DeliveryState.FAILED
            await pilot.pause()
            before = list(radio.sent_texts)

            app._delete_dm_entry(entry)
            await pilot.pause()

            self.assertEqual(radio.sent_texts, before)
            self.assertTrue(entry.deleted)


class DMNodeMenuIntegrationTests(DirectMessageAppTestsBase):
    async def test_chat_sender_menu_offers_dm_for_remote_sender(self) -> None:
        from simulated_radio_service import SIMULATED_MESSAGES

        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await pilot.pause()
            widget = list(app.query(ChatEntryWidget))[-1]
            widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            values = [item.value for item in app._user_menu.items]
            self.assertIn("dm", values)

    async def test_activating_dm_from_chat_menu_opens_conversation(self) -> None:
        from simulated_radio_service import SIMULATED_MESSAGES

        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("chat")
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await pilot.pause()
            widget = list(app.query(ChatEntryWidget))[-1]
            widget.focus()
            await pilot.press("enter")
            await pilot.pause()
            dm_index = next(
                i for i, item in enumerate(app._user_menu.items) if item.value == "dm"
            )
            app._user_menu.activate(dm_index)
            await pilot.pause()
            self.assertEqual(app.current_tab, "dm")
            self.assertEqual(
                app.current_dm_node_id, SIMULATED_MESSAGES[0].sender_node_id
            )
            self.assertIsNone(app._user_menu)

    async def test_mesh_node_menu_offers_dm_for_remote_node(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            now = time.time()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata("!c0ffee01", "Target Node", "TGT", 1, last_heard=now - 5),
            )
            app.show_tab("mesh")
            await pilot.pause()
            _mesh_select_node(app, "!c0ffee01")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            values = [item.value for item in app._user_menu.items]
            self.assertIn("dm", values)

    async def test_mesh_node_menu_activating_dm_opens_conversation(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            now = time.time()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata("!c0ffee01", "Target Node", "TGT", 1, last_heard=now - 5),
            )
            app.show_tab("mesh")
            await pilot.pause()
            _mesh_select_node(app, "!c0ffee01")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            dm_index = next(
                i for i, item in enumerate(app._user_menu.items) if item.value == "dm"
            )
            app._user_menu.activate(dm_index)
            await pilot.pause()
            self.assertEqual(app.current_tab, "dm")
            self.assertEqual(app.current_dm_node_id, "!c0ffee01")

    async def test_you_has_no_dm_action_in_mesh_menu(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            values = [item.value for item in app._user_menu.items]
            self.assertNotIn("dm", values)
            self.assertTrue(all(not item.actionable for item in app._user_menu.items))

    async def test_unmessageable_node_never_offers_dm(self) -> None:
        app = MeshtasticPassApp(_simulated_radio(), self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            now = time.time()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(
                    "!unmsg001",
                    "No Message",
                    "NOM",
                    1,
                    last_heard=now - 5,
                    is_unmessagable=True,
                ),
            )
            app.show_tab("mesh")
            await pilot.pause()
            _mesh_select_node(app, "!unmsg001")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            values = [item.value for item in app._user_menu.items]
            self.assertNotIn("dm", values)
            self.assertIn("favorite", values)


class DMTrafficTests(DirectMessageAppTestsBase):
    async def test_opening_dm_list_generates_zero_rf(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.show_tab("dm")
            await pilot.pause()
            self.assertEqual(radio.sent_texts, [])

    async def test_opening_dm_history_generates_zero_rf(self) -> None:
        store = ChatStore.open(self.chat_db_path)
        self.addCleanup(store.close)
        store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice",
            sender_short_name="ALC",
            channel_index=0,
            text="hi",
            radio_rx_at=100.0,
            received_at=100.0,
            dm_node_id="!a11ce001",
        )
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings, chat_store=store)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            self.assertEqual(radio.sent_texts, [])

    async def test_delete_generates_zero_rf(self) -> None:
        radio = ControllableSendRadioService()
        app = MeshtasticPassApp(radio, self.settings)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.open_dm("!a11ce001", long_name="Alice")
            await pilot.pause()
            entry = app._start_dm_outgoing("!a11ce001", "delete me")
            entry.delivery_state = DeliveryState.FAILED
            await pilot.pause()
            app._delete_dm_entry(entry)
            await pilot.pause()
            self.assertEqual(radio.sent_texts, [])


if __name__ == "__main__":
    unittest.main()
