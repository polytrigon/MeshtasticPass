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

from types import SimpleNamespace

from app import MeshtasticPassApp, PassSortSelector, PassesView
from radio_service import NodeMetadata, RadioState
from viewport_menu import ViewportMenu
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


class PassesHarness(unittest.IsolatedAsyncioTestCase):
    """Shared setup only -- no tests, so nothing here runs twice."""

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


class PassesViewTests(PassesHarness):
    """The header's two numbers, and the grid's one colour."""

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


class PassMenuTests(PassesHarness):
    """ENTER on a name opens the same node menu CHAT's sender names do.

    It used to jump straight into a DM, which made the menu's other four
    actions unreachable from this board. Which of two identical cells you
    are on is answered by the BAR, not the menu -- CHAT's menu prints no
    node ID for a remote node, and this is CHAT's menu unchanged.
    """

    async def _open_menu(self, app, pilot) -> ViewportMenu:
        await self._open_passes(pilot)
        await pilot.press("enter")
        await pilot.pause()
        return app.query_one("#node-context-menu", ViewportMenu)

    async def test_enter_opens_the_node_menu_rather_than_a_dm(self) -> None:
        self.store.record_encounter(
            "!aaaa0001", seen_at=T, short_name="ALFA", long_name="Alfa Trail"
        )
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            menu = await self._open_menu(app, pilot)

            labels = [item.label for item in menu.items]
            self.assertIn("Alfa Trail", labels)
            self.assertIn("ALFA", labels)
            self.assertIn("HIGHLIGHT", labels)
            self.assertIn("DIRECT MSG", labels)

    async def test_a_node_the_radio_does_not_know_still_opens_a_menu(self) -> None:
        """The case PASSES exists for.

        PASSES exists to outlive the radio's own bounded NodeDB, so the
        menu is built from what is ON RECORD first and the live NodeDB
        only overlays it. This radio is not even online, which is the
        strongest version of that case.
        """
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            menu = await self._open_menu(app, pilot)

            self.assertIn("ALFA", [item.label for item in menu.items])
            self.assertTrue(
                any(item.value for item in menu.items),
                "an offline node still needs actionable rows",
            )

    async def test_the_menu_opens_against_the_highlighted_node(self) -> None:
        """Not simply the first one. Arrow, then ENTER.

        The whole grid is one widget, so the selection is an index this
        code keeps rather than focus landing on a per-cell widget -- the
        kind of thing that silently opens a menu for the wrong row.
        """
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        self.store.record_encounter("!bbbb0002", seen_at=T + 10, short_name="BRVO")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)
            first = view.selected.short_name
            await pilot.press("right")
            await pilot.pause()
            moved = view.selected.short_name
            self.assertNotEqual(first, moved)

            await pilot.press("enter")
            await pilot.pause()
            menu = app.query_one("#node-context-menu", ViewportMenu)
            labels = [item.label for item in menu.items]
            self.assertIn(moved, labels)
            self.assertNotIn(first, labels)

    async def test_the_menu_is_placed_beside_the_cell_not_the_board(self) -> None:
        """PASSES has no per-cell widget to anchor to, so it computes one.

        Anchoring to the PassesView itself would put the popup at the
        edge of the whole grid, which is nowhere near the name that was
        selected.
        """
        for index in range(6):
            self.store.record_encounter(
                f"!aaaa000{index}", seen_at=T + index, short_name=f"N{index:03d}"
            )
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)
            await pilot.press("right", "right")
            await pilot.pause()

            region = view.selected_region()
            self.assertIsNotNone(region)
            self.assertGreater(region.x, view.content_region.x)
            self.assertEqual(region.height, 1)

    async def test_duplicate_names_render_as_two_identical_cells(self) -> None:
        """No ID tail any more -- and that is deliberate.

        Two radios sharing an emoji look alike on the board, which is
        the truth about this mesh. Telling them apart is the BAR's job,
        and both cells must still be separately selectable for it to
        have anything to describe.
        """
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="\U0001f43b")
        self.store.record_encounter("!bbbb0002", seen_at=T + 10, short_name="\U0001f43b")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)

            text = view.render()
            self.assertNotIn("aaaa", text.plain)
            self.assertNotIn("bbbb", text.plain)
            self.assertEqual(len(view.passes), 2)
            first = view.selected.node_id
            await pilot.press("right")
            await pilot.pause()
            self.assertNotEqual(view.selected.node_id, first)


class PassHighlightTests(PassesHarness):
    """A node highlighted anywhere is highlighted here.

    HIGHLIGHT is a mark the user puts on a node, and a mark that only
    shows up in the view you made it from is not a mark. MESH paints a
    highlighted node in ACCENT2; so does this.
    """

    async def test_a_highlighted_node_is_drawn_in_accent2(self) -> None:
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        self.store.record_encounter("!bbbb0002", seen_at=T + 10, short_name="BRVO")
        app = self._app()
        app.settings.set_favorite("!aaaa0001", True)
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)
            palette = THEME_PALETTES[app._current_theme]
            self.assertNotIn(palette.accent2, (palette.base, palette.accent))

            by_name = _cells(view)
            self.assertEqual(by_name["ALFA"].name, palette.accent2)
            self.assertEqual(by_name["BRVO"].name, palette.base)

    async def test_highlighting_from_the_menu_repaints_the_grid(self) -> None:
        """The recurring failure this project watches for.

        The setting changes, and the view keeps drawing the old answer
        because nothing told it to look again. PASSES reads is_favorite
        at render time, so it needs telling.
        """
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)
            palette = THEME_PALETTES[app._current_theme]
            self.assertEqual(_cells(view)["ALFA"].name, palette.base)

            # The real production path, not a direct settings poke: the
            # menu action is what a person actually triggers.
            app._activate_node_action("!aaaa0001", "favorite")
            await pilot.pause()

            self.assertEqual(_cells(view)["ALFA"].name, palette.accent2)


class PassMenuKeyTests(PassesHarness):
    """While the menu is open, the arrows belong to the menu."""

    async def test_arrows_move_the_menu_not_the_selection(self) -> None:
        """Focus never leaves the grid while the popup is up, so without

        an explicit hand-off the arrows would move the selection
        UNDERNEATH the menu -- quietly changing which node the open menu
        is about.
        """
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        self.store.record_encounter("!bbbb0002", seen_at=T + 10, short_name="BRVO")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)
            selected = view.selected.node_id

            await pilot.press("enter")
            await pilot.pause()
            menu = app.query_one("#node-context-menu", ViewportMenu)
            before = menu.highlighted_index

            await pilot.press("down")
            await pilot.pause()

            self.assertEqual(view.selected.node_id, selected)
            self.assertNotEqual(menu.highlighted_index, before)

    async def test_the_selection_moves_again_once_the_menu_closes(self) -> None:
        """The hand-off is for the menu's lifetime, not permanent."""
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        self.store.record_encounter("!bbbb0002", seen_at=T + 10, short_name="BRVO")
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)
            selected = view.selected.node_id

            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.query("#node-context-menu")), 0)

            await pilot.press("right")
            await pilot.pause()
            self.assertNotEqual(view.selected.node_id, selected)


class PassSortControlTests(PassesHarness):
    """Reaching the sort control at all.

    The grid owns the arrow keys -- they move a selection through several
    hundred names -- so there is no spare direction to walk up to a
    header control with. This app's answer has always been an uppercase
    letter hotkey named in the footer, the way C reaches CHAT's channel
    selector, and PASSES shipped without one: the dropdown was on screen
    and unreachable.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store.record_encounter("!aaaa0001", seen_at=T, short_name="ALFA")
        self.store.record_encounter("!bbbb0002", seen_at=T + 10, short_name="BRVO")

    async def test_s_opens_the_sort_control(self) -> None:
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            self.assertIsInstance(app.focused, PassesView)

            await pilot.press("s")
            await pilot.pause()

            selector = app.query_one(PassSortSelector)
            self.assertIs(app.focused, selector)
            self.assertTrue(selector.is_open)

    async def test_the_footer_says_so(self) -> None:
        """A hotkey nobody can discover is not a way in."""
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)

            footer = str(app.query_one("#footer", Static).render())
            self.assertIn("S sort", footer)

    async def test_choosing_an_order_re_sorts_and_returns_focus(self) -> None:
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            before = [encounter.short_name for encounter in app.query_one(PassesView).passes]

            await pilot.press("s")
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()

            self.assertIsInstance(app.focused, PassesView)
            after = [encounter.short_name for encounter in app.query_one(PassesView).passes]
            self.assertCountEqual(before, after)

    async def test_escape_does_not_strand_the_keyboard(self) -> None:
        """Leaving without choosing.

        A closed dropdown ignores the arrows, so focus parked on it after
        ESC would leave the keyboard doing nothing at all until the user
        guessed at a tab switch.
        """
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.query_one(PassSortSelector).is_open)

            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.focused, PassesView)

            selected = app.query_one(PassesView).selected.node_id
            await pilot.press("right")
            await pilot.pause()
            self.assertNotEqual(app.query_one(PassesView).selected.node_id, selected)


class PassConnectingStateTests(PassesHarness):
    """Connecting must not move the board.

    PASSES used to carry its own status row above the header. A row that
    appears and disappears takes everything under it with it, one line
    each way -- on a grid of names that is the whole board jumping every
    time the radio reconnects. CHAT solved this long ago by putting the
    status INSIDE its channel dropdown; this does the same.
    """

    def setUp(self) -> None:
        super().setUp()
        for index in range(12):
            self.store.record_encounter(
                f"!aaaa000{index:x}", seen_at=T + index, short_name=f"N{index:03d}"
            )

    async def test_the_status_replaces_the_sort_control(self) -> None:
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)

            selector = app.query_one(PassSortSelector)
            self.assertIn("CONNECTING", str(selector.render()).upper())
            self.assertNotIn("RECENT", str(selector.render()).upper())

    async def test_no_separate_status_row_exists(self) -> None:
        """Its absence IS the fix, so its absence is what to assert."""
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)

            self.assertEqual(len(app.query("#passes-connection-status")), 0)

    async def test_the_grid_does_not_move_when_the_radio_comes_online(self) -> None:
        """The actual complaint, stated as a coordinate.

        Whatever the connection state, the first row of names sits on the
        same screen line.
        """
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            view = app.query_one(PassesView)
            connecting_y = view.region.y

            app._radio_state = RadioState.ONLINE
            app._update_chat_connection_state()
            await pilot.pause()

            selector = app.query_one(PassSortSelector)
            self.assertIn("RECENT", str(selector.render()).upper())
            self.assertEqual(view.region.y, connecting_y)

    async def test_the_count_stays_visible_while_connecting(self) -> None:
        """It is a fact about the database, not about the radio.

        CHAT hides its network name while connecting because that
        describes a live radio. "12 NODES" is just as true with no radio
        attached, and hiding it would suggest the list was unavailable.
        """
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)

            self.assertIn("12 NODES", self._count_text(app))

    async def test_focus_is_not_stranded_on_the_disabled_control(self) -> None:
        """An overridden dropdown is disabled, and this board is all arrows."""
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_passes(pilot)
            app._radio_state = RadioState.ONLINE
            app._update_chat_connection_state()
            await pilot.pause()

            await pilot.press("s")
            await pilot.pause()
            self.assertIsInstance(app.focused, PassSortSelector)

            app._radio_state = RadioState.CONNECTING
            app._update_chat_connection_state()
            await pilot.pause()

            self.assertIsInstance(app.focused, PassesView)


class SimulatedRadioTests(PassesHarness):
    """A simulated radio must never write to the pass list.

    Found on real hardware: eight invented people sitting permanently in
    the user's own board, one of them a 13-cell "No Short Name" that
    took a thirteen-column grid down to four. PASSES is the one place in
    this app where a row deliberately outlives the radio that wrote it,
    so a fabricated row there is indistinguishable from a real encounter
    for ever after -- there is no later moment at which it becomes
    obviously wrong.
    """

    async def test_a_simulated_sweep_records_nothing(self) -> None:
        app = self._app()
        self.assertTrue(app.radio.is_simulated)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for _ in range(30):
                await pilot.pause()

            self.assertEqual(self.store.encounters(), ())

    async def test_a_real_radio_still_records(self) -> None:
        """The guard must be about SIMULATION, not about sweeps.

        A test double that never claims to be simulated has to keep
        working, or this quietly turns off PASSES for everyone.
        """
        app = self._app()
        app.radio.is_simulated = False
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._record_pass_encounters(
                (NodeMetadata("!aaaa0001", "Alfa Trail", "ALFA", 0),), now=T
            )

            recorded = self.store.encounters()
            self.assertEqual([row.node_id for row in recorded], ["!aaaa0001"])

    async def test_an_arriving_simulated_message_records_nothing(self) -> None:
        """The other write path into the store, guarded the same way."""
        app = self._app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._record_pass_from_message(
                SimpleNamespace(
                    sender_node_id="!bbbb0002",
                    sender_long_name="Bob Basecamp",
                    sender_short_name="BOB",
                    radio_rx_at=T,
                )
            )

            self.assertEqual(self.store.encounters(), ())


if __name__ == "__main__":
    unittest.main()
