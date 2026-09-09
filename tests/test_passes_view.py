"""PASSES: what the grid says, and what it spends its accent on.

The view draws two different numbers and one colour, and each is a claim
about the mesh that has to stay true:

  NODES   everyone this radio has ever encountered.
  PASSES  the subset that exchanged a pass with us -- which only another
          MeshtasticPass install can do, so today it is honestly zero.

An earlier version coloured "heard directly" and counted it as MET
DIRECTLY, conflating being in radio range with having met someone. Most
of a real board qualifies, so the accent stopped meaning anything. These
tests pin the replacement: one colour for everyone, and the accent spent
only on a confirmed pass.

Every test here runs against a radio that never reaches ONLINE. That is
deliberate twice over: a pass list is a record, not a live view, so it
must render with no radio attached -- and an ONLINE simulated radio
would sweep its own NodeDB into the store (see _record_pass_encounters)
and make these counts depend on a fixture that has nothing to do with
what is being tested.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from textual.widgets import Static

from app import MeshtasticPassApp, PassesView
from app_settings import AppSettings
from chat_store import ChatStore
from simulated_radio_service import SimulatedRadioService
from theme_palette import THEME_PALETTES


T = 1_700_000_000.0


def _cells(view: PassesView) -> dict[str, object]:
    """Every rendered cell as {name: colour}.

    render() appends each cell with its own style and appends the
    gutters and newlines unstyled, so the Text's spans ARE the cells --
    reading them back reads what the terminal is told to paint, rather
    than re-deriving it from the encounter list and testing nothing.
    """
    text = view.render()
    return {
        text.plain[span.start : span.end].rstrip(): span.style.color
        for span in text.spans
    }


class PassesViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=root / "meshtasticpass" / "config.json",
            profile_path=root / "terminal.conf",
        )
        self.store = ChatStore.open(str(root / "chat.db"))

    def _app(self) -> MeshtasticPassApp:
        # connect_delay=60: never reaches ONLINE here (see the module
        # docstring).
        radio = SimulatedRadioService(connect_delay=60, message_interval=0)
        app = MeshtasticPassApp(radio, self.settings, chat_store=self.store)
        app._clock = lambda: T
        return app

    async def _open_passes(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    def _count_text(self, app: MeshtasticPassApp) -> str:
        return str(app.query_one("#passes-count", Static).render())

    # ---- the two numbers ---------------------------------------------

    async def test_the_count_names_nodes_and_passes(self) -> None:
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        self.store.record_encounter("!bbbb0002", seen_at=T, short_name="BRVO")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)

            self.assertIn("2 NODES", self._count_text(app))

    async def test_hearing_a_node_directly_is_not_a_pass(self) -> None:
        """The whole point of the rename.

        Both of these were heard by this radio with nothing relaying, and
        neither has exchanged a pass. The old line called that "2 MET
        DIRECTLY"; the count must now read zero passes.
        """
        self.store.record_encounter(
            "!aaaa0001", seen_at=T, short_name="ALFA", heard_directly=True
        )
        self.store.record_encounter(
            "!bbbb0002", seen_at=T, short_name="BRVO", heard_directly=True
        )
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)

            line = self._count_text(app)
            self.assertIn("2 NODES", line)
            self.assertIn("0 PASSES", line)
            self.assertNotIn("MET DIRECTLY", line)

    async def test_zero_passes_is_printed_rather_than_omitted(self) -> None:
        """"0 PASSES" on a full board IS the information.

        A field that vanishes at zero reads as a field that does not
        exist, leaving no way to tell "nobody out there is running this
        yet" from "this app does not count that".
        """
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)

            self.assertIn("0 PASSES", self._count_text(app))

    async def test_a_confirmed_pass_is_counted(self) -> None:
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        self.store.record_encounter(
            "!bbbb0002", seen_at=T, short_name="BRVO", pass_at=T
        )
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)

            line = self._count_text(app)
            self.assertIn("2 NODES", line)
            self.assertIn("1 PASSES", line)

    # ---- the one colour ----------------------------------------------

    async def test_every_node_without_a_pass_shares_one_colour(self) -> None:
        """Heard or gossiped, near or far: all the same.

        Encountering a node is what a mesh does all day. Colouring the
        ordinary case spends the reader's attention on nothing.
        """
        self.store.record_encounter(
            "!aaaa0001", seen_at=T, short_name="ALFA", heard_directly=True
        )
        self.store.record_encounter(
            "!bbbb0002", seen_at=T, short_name="BRVO", hops_away=5
        )
        self.store.record_encounter("!cccc0003", seen_at=T, short_name="CHRL")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)

            colours = set(_cells(view).values())
            self.assertEqual(len(colours), 1, f"more than one colour: {colours}")

    async def test_a_pass_is_the_only_thing_drawn_in_accent(self) -> None:
        self.store.record_encounter(
            "!aaaa0001", seen_at=T, short_name="ALFA", heard_directly=True
        )
        self.store.record_encounter(
            "!bbbb0002", seen_at=T, short_name="BRVO", pass_at=T
        )
        self.store.record_encounter("!cccc0003", seen_at=T, short_name="CHRL")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)
            palette = THEME_PALETTES[app._current_theme]
            self.assertNotEqual(palette.accent, palette.base)

            by_name = _cells(view)
            self.assertEqual(set(by_name), {"ALFA", "BRVO", "CHRL"})
            self.assertEqual(by_name["BRVO"].name, palette.accent)
            for name in ("ALFA", "CHRL"):
                self.assertEqual(by_name[name].name, palette.base)


if __name__ == "__main__":
    unittest.main()
