"""MESH shows its board while the radio is still connecting.

The dot grid is not topology. It is the board topology lands on, so it
is drawable before the radio has said anything -- and an empty rectangle
during a handshake reads as a broken view where an empty board reads as
a waiting one.

The constraint that makes this delicate: stale topology deliberately
stays visible across a reconnect (see _refresh_mesh), so "draw the empty
grid while offline" must never mean "paint over what is already there".
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app import DOT_GRID_GLYPH, MeshCanvas, MeshTopologyView, MeshtasticPassApp
from app_settings import AppSettings
from simulated_radio_service import SimulatedRadioService


class MeshConnectingGridTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.settings = AppSettings.load(
            config_path=root / "meshtasticpass" / "config.json",
            profile_path=root / "terminal.conf",
        )

    def _app(self, *, connect_delay: float) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=connect_delay, message_interval=0
        )
        return MeshtasticPassApp(radio, self.settings)

    @staticmethod
    def _canvas_text(app: MeshtasticPassApp) -> str:
        return str(app.query_one(MeshCanvas).render())

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("4")
        await pilot.pause()
        await pilot.pause()

    async def test_the_dots_are_drawn_before_the_radio_connects(self) -> None:
        # connect_delay=60: never reaches ONLINE during this test.
        app = self._app(connect_delay=60)
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)

            self.assertIn(DOT_GRID_GLYPH, self._canvas_text(app))

    async def test_the_connecting_status_is_still_shown(self) -> None:
        """The board appearing must not read as "connected".

        The grid says where nodes will go; the status line is what says
        whether any have arrived. Drawing one must not silence the other.
        """
        app = self._app(connect_delay=60)
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)

            statuses = list(app.query("#mesh-connection-status"))
            self.assertTrue(statuses)
            self.assertTrue(statuses[0].display)

    async def test_an_empty_grid_never_paints_over_real_topology(self) -> None:
        """The reconnect case, and the reason this is not just a repaint.

        Once nodes have arrived, a drop back out of ONLINE keeps showing
        them -- stale data beats no data, and the status line already
        says it is not live. render_empty_grid is a no-op whenever a
        working set exists, so it cannot undo that.
        """
        app = self._app(connect_delay=0)
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            for _ in range(20):
                await pilot.pause()
                if app.query_one(MeshTopologyView).working_set:
                    break
            view = app.query_one(MeshTopologyView)
            self.assertTrue(view.working_set, "radio never delivered a working set")
            populated = self._canvas_text(app)

            view.render_empty_grid(app._current_theme)
            await pilot.pause()

            self.assertEqual(self._canvas_text(app), populated)


if __name__ == "__main__":
    unittest.main()
