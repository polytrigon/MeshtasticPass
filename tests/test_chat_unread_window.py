"""The mounted CHAT window stays bounded even with nobody watching.

The bug: the window trimmed only messages that had been READ, with no
upper bound. Every arriving message is unread until somebody looks, so
an unattended radio never trimmed anything -- it mounted a widget per
message all night, and the 1s relative-timestamp timer walked every one
of them by morning. It went sluggish specifically when left alone, which
is the detail that identifies it, because that is exactly when nothing
gets read.

What must NOT regress in fixing it: an unread message may leave the
mounted window, but it may never leave the app. The text stays in
chat_store, the entry comes back by scrolling up, and CHAT(N) keeps
counting it.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app import (
    MOUNTED_CHAT_UNREAD_CEILING,
    ChatEntryWidget,
    MeshtasticPassApp,
)
from app_settings import AppSettings
from chat_store import ChatStore
from simulated_radio_service import SimulatedRadioService


class MountedWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.settings = AppSettings.load(
            config_path=root / "meshtasticpass" / "config.json",
            profile_path=root / "terminal.conf",
        )
        self.store = ChatStore.open(str(root / "chat.db"))

    def _app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(connect_delay=60, message_interval=0)
        return MeshtasticPassApp(radio, self.settings, chat_store=self.store)

    @staticmethod
    async def _receive(app, pilot, count: int) -> None:
        """Messages arriving with nobody on the CHAT tab -- all unread."""
        from radio_service import ReceivedMessage

        for index in range(count):
            app._accept_received_message(
                ReceivedMessage(
                    text=f"message {index}",
                    sender_node_id="!aaaa0001",
                    sender_long_name="Alfa Trail",
                    sender_short_name="ALFA",
                    channel_index=0,
                )
            )
        await pilot.pause()

    async def test_an_unattended_night_does_not_grow_without_bound(self) -> None:
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            arrived = app._mounted_chat_target + MOUNTED_CHAT_UNREAD_CEILING + 150
            await self._receive(app, pilot, arrived)

            mounted = len(app.query(ChatEntryWidget))
            self.assertLessEqual(
                mounted,
                app._mounted_chat_target + MOUNTED_CHAT_UNREAD_CEILING,
                f"{arrived} unread messages left {mounted} widgets mounted",
            )

    async def test_unread_messages_are_protected_below_the_ceiling(self) -> None:
        """The protection still does its job at ordinary volumes.

        A handful of unread messages must not scroll out of the window
        before anyone has had a chance to see them.
        """
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await self._receive(app, pilot, app._mounted_chat_target + 10)

            self.assertEqual(
                len(app.chat_history), app._mounted_chat_target + 10
            )

    async def test_trimming_never_loses_the_unread_count(self) -> None:
        """CHAT(N) counts arrivals, not mounted widgets.

        If it were derived from what happens to be mounted, this fix
        would silently under-report a night of traffic -- which is worse
        than the sluggishness it replaces.
        """
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            arrived = app._mounted_chat_target + MOUNTED_CHAT_UNREAD_CEILING + 150
            await self._receive(app, pilot, arrived)

            self.assertEqual(app._channel_states[0].unread_count, arrived)

    async def test_trimmed_messages_are_still_in_the_store(self) -> None:
        """Out of the window is not out of the app."""
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            arrived = app._mounted_chat_target + MOUNTED_CHAT_UNREAD_CEILING + 150
            await self._receive(app, pilot, arrived)

            stored = app.chat_store.load_recent(0, limit=arrived)
            self.assertGreater(len(stored), arrived - 5)

    async def test_an_unsent_message_is_never_trimmed(self) -> None:
        """It is not in the store, so the widget is the only copy.

        Absolute, unlike the unread protection: no ceiling releases it.
        """
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            outgoing = app._start_outgoing("still sending")
            outgoing.message_id = None
            await self._receive(
                app,
                pilot,
                app._mounted_chat_target + MOUNTED_CHAT_UNREAD_CEILING + 150,
            )

            self.assertIn(outgoing, app.chat_history)


if __name__ == "__main__":
    unittest.main()
