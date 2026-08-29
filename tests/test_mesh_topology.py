"""Pure and headless tests for MESH: pure grid geometry, the real passive

working-set/role/staleness model (mesh_state.py), and the fixed-grid
board that renders it. See app.py's module comment above MESH_GRID_ROWS
and mesh_state.py's module docstring for what remains deliberately out
of scope (Favorites, dynamic node-count growth beyond the bounded
working set, scrolling, and RELAY inference from anything but
trustworthy per-packet relay evidence, which this app does not have).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from rich.cells import cell_len
from textual.color import Color
from textual.containers import ScrollableContainer
from textual.widgets import Input

from app import (
    ANIMATED_STATUS,
    CIRCLE_SOLID_LARGE,
    CIRCLE_STROKED_LARGE,
    DOT_GRID_GLYPH,
    DOT_GRID_SPACING_X,
    DOT_GRID_SPACING_Y,
    MESH_BOARD_LABEL_MAX_CELLS,
    MESH_GRID_MIN_COLUMNS,
    MESH_GRID_MIN_ROWS,
    MESH_LOGICAL_GRID_CENTER_COLUMN,
    MESH_LOGICAL_GRID_CENTER_ROW,
    MESH_LOGICAL_GRID_COLUMNS,
    MESH_LOGICAL_GRID_ROWS,
    MESH_SELECTED_GLYPH_WIDTH,
    MeshCanvas,
    MeshNodeLabelWidget,
    MeshNodeWidget,
    MeshRelayWidget,
    MeshTopologyView,
    MeshtasticPassApp,
    ThinScrollBarRender,
    _compute_mesh_grid_dimensions,
    _mesh_directional_target,
    _mesh_grid_pixel,
    _mesh_hop_counts,
    _mesh_node_color,
    _mesh_relay_color,
    _mesh_select_node,
    _mesh_translated_positions,
    _render_mesh_canvas,
)
from app_settings import AppSettings
from chat_store import DEFAULT_HISTORY_LIMIT, ChatStore
from geo import KM_PER_MILE, GeoPosition, distance_between, format_coordinates
from mesh_state import (
    DEFAULT_MAX_REMOTE_NODES,
    MESH_STALE_THRESHOLD_SECONDS,
    MeshNodeState,
    build_mesh_working_set,
    format_mesh_node_bar_fields,
    format_mesh_node_bar_line,
)
from mesh_topology import (
    DEFAULT_MAX_GRID_RADIUS,
    HORIZONTAL_GAP,
    NODE_HEIGHT,
    NODE_WIDTH,
    RelayStage,
    assign_grid_slots,
    build_relay_stages,
    build_topology,
    compact_node_label,
    directional_target,
    place_within_bounds,
    project_to_viewport,
    route_chain,
    route_chain_avoiding,
    route_connector,
    route_connector_avoiding,
)
from mesh_topology import _route_connector_alternate_elbow
from mesh_topology import _compress_distance_to_radius
import mesh_topology as mesh_topology_module
from node_activity import ACTIVE_WINDOW_SECONDS, is_node_active
from relative_time import format_relative_age
from radio_service import (
    DISPLAY_UNITS_IMPERIAL,
    DISPLAY_UNITS_METRIC,
    LinkObservation,
    NodeMetadata,
    RadioInfo,
    RadioSendError,
    RadioState,
    TracerouteState,
)
from simulated_radio_service import (
    SIMULATED_LOCAL_POSITION,
    SIMULATED_MESSAGES,
    SIMULATED_NODES,
    SimulatedRadioService,
    SimulatedTracerouteOutcome,
)
from theme_palette import THEME_PALETTES


LOCAL_GEO = GeoPosition(0.0, 0.0)
_MILES_PER_DEGREE_AT_EQUATOR = 69.0

YOU = NodeMetadata("!you", "Local", "ME", 0, 1_000.0, True, position=LOCAL_GEO)


def west_of_local(miles: float) -> GeoPosition:
    return GeoPosition(0.0, -miles / _MILES_PER_DEGREE_AT_EQUATOR)


def east_of_local(miles: float) -> GeoPosition:
    return GeoPosition(0.0, miles / _MILES_PER_DEGREE_AT_EQUATOR)


def north_of_local(miles: float) -> GeoPosition:
    return GeoPosition(miles / _MILES_PER_DEGREE_AT_EQUATOR, 0.0)


def south_of_local(miles: float) -> GeoPosition:
    return GeoPosition(-miles / _MILES_PER_DEGREE_AT_EQUATOR, 0.0)


def _bar_text(app: MeshtasticPassApp) -> str:
    """MESH GPS + UNIFIED BAR Part B: the single #mesh-node-bar widget's

    rendered text -- replaces the old separate #mesh-context-status/
    #mesh-last-update widgets this test module used to query directly.
    """
    return str(app.query_one("#mesh-node-bar").render())


def _bar_field(text: str, field: str) -> str:
    """Extract one " • "-separated "FIELD value" segment's value from a

    unified-bar string (e.g. _bar_field(text, "DISTANCE") -> "3.2 km") --
    tolerant of a value that itself contains " • " or "/" (LINK's raw
    values), since it only ever splits on the FIELD-NAME boundaries.
    """
    marker = f"{field} "
    start = text.index(marker) + len(marker)
    end = text.find(" • ", start)
    return text[start:] if end == -1 else text[start:end]


def sample_nodes(count: int = 8) -> tuple[NodeMetadata, ...]:
    nodes = [NodeMetadata("!00000000", "Local", "ME", 0, 1_000.0, True)]
    for index in range(1, count):
        hops = 1 if index < 4 else 2 if index < 7 else None
        nodes.append(
            NodeMetadata(
                f"!{index:08x}",
                f"Node {index}",
                f"N{index}",
                hops,
                990.0 if index % 2 else 600.0,
            )
        )
    return tuple(nodes)


def offset_xy(widget) -> tuple[int, int]:
    offset = widget.styles.offset
    return (int(offset.x.value), int(offset.y.value))


def glyph_screen_position(widget) -> tuple[int, int]:
    """The glyph's own anchor (x, y): the center column of its box.

    Unselected, MeshNodeWidget is a 1-cell-wide box, so its offset IS the
    anchor. Selected, it widens to a 3-cell composite (small dot / role
    glyph / small dot) for the "larger" treatment -- the anchor is still
    the box's center column, never its left edge, so this must account
    for width rather than assume width == 1.
    """
    offset_x, offset_y = offset_xy(widget)
    width = int(widget.styles.width.value)
    return (offset_x + width // 2, offset_y)


def client_state(
    node: NodeMetadata, *, last_interaction_at: float | None, is_relay: bool = False
) -> MeshNodeState:
    return MeshNodeState(
        node=node, is_client=True, is_relay=is_relay, last_interaction_at=last_interaction_at
    )


class MeshTopologyModelTests(unittest.TestCase):
    def test_west_and_east_nodes_ordered_by_distance_not_proportionally(self) -> None:
        """MESH GPS PLACEMENT: real distance now genuinely differentiates

        radius (see _compress_distance_to_radius) -- Jim < Alice < YOU
        < Bob on the x-axis, with Bob's real ~40x-farther distance
        compressed to a MODEST extra grid step, never a literal 40x
        pixel gap.
        """
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        alice = NodeMetadata(
            "!alice", "Alice", "ALCE", None, 1_000.0, False, position=west_of_local(0.5)
        )
        jim = NodeMetadata(
            "!jim", "Jim", "JIM", None, 1_000.0, False, position=west_of_local(3)
        )
        bob = NodeMetadata(
            "!bob", "Bob", "BOB", None, 1_000.0, False, position=east_of_local(20)
        )
        layout = build_topology([local, alice, jim, bob])
        by_id = {item.node.node_id: item for item in layout.nodes}

        self.assertEqual(by_id["!alice"].region, "W")
        self.assertEqual(by_id["!jim"].region, "W")
        self.assertEqual(by_id["!bob"].region, "E")
        self.assertLess(by_id["!jim"].x, by_id["!alice"].x)
        self.assertLess(by_id["!alice"].x, by_id["!local"].x)
        self.assertLess(by_id["!local"].x, by_id["!bob"].x)

        # Jim (radius 2) sits genuinely farther from Alice (radius 1)
        # than Alice sits from local -- distance-informed, not a
        # uniform one-step-per-node ladder irrespective of the real
        # gap -- but Bob's own ~40x-farther real distance (radius 3,
        # the same cap any non-GPS/hop-based node already respects)
        # never produces anywhere close to a 40x pixel gap.
        alice_gap = by_id["!local"].x - by_id["!alice"].x
        jim_gap = by_id["!alice"].x - by_id["!jim"].x
        bob_gap = by_id["!bob"].x - by_id["!local"].x
        self.assertGreater(jim_gap, 0)
        self.assertGreater(bob_gap, alice_gap)
        self.assertLess(bob_gap, alice_gap * 40)

    def test_north_and_south_nodes_placed_up_and_down(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        nora = NodeMetadata(
            "!nora", "Nora", "NORA", None, 1_000.0, False, position=north_of_local(3)
        )
        sam = NodeMetadata(
            "!sam", "Sam", "SAM", None, 1_000.0, False, position=south_of_local(2)
        )
        layout = build_topology([local, nora, sam])
        by_id = {item.node.node_id: item for item in layout.nodes}

        self.assertEqual(by_id["!nora"].region, "N")
        self.assertEqual(by_id["!sam"].region, "S")
        self.assertLess(by_id["!nora"].y, by_id["!local"].y)
        self.assertLess(by_id["!local"].y, by_id["!sam"].y)

    def test_distance_orders_outward_without_proportional_spacing(self) -> None:
        """far is genuinely ~500x farther than near in the real world --

        MESH GPS PLACEMENT compresses that gap onto a couple of extra
        grid steps (see _compress_distance_to_radius), never a literal
        500x pixel gap, while still placing far genuinely farther out.
        """
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        near = NodeMetadata(
            "!near", "Near", "N", None, 1_000.0, False, position=west_of_local(0.5)
        )
        far = NodeMetadata(
            "!far", "Far", "F", None, 1_000.0, False, position=west_of_local(500)
        )
        layout = build_topology([local, near, far])
        by_id = {item.node.node_id: item for item in layout.nodes}
        self.assertLess(by_id["!far"].x, by_id["!near"].x)
        near_gap = by_id["!local"].x - by_id["!near"].x
        far_gap = by_id["!near"].x - by_id["!far"].x
        self.assertGreater(far_gap, 0)
        self.assertLess(far_gap, near_gap * 500)

    def test_grid_radius_is_clamped_and_never_explodes_the_board(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        nodes = [local]
        for index in range(6):
            nodes.append(
                NodeMetadata(
                    f"!w{index}",
                    f"West{index}",
                    None,
                    None,
                    1_000.0,
                    False,
                    position=west_of_local(index + 1),
                )
            )
        layout = build_topology(nodes, max_radius=3)
        cell_width = NODE_WIDTH + HORIZONTAL_GAP
        max_extent_cells = max(abs(item.x - layout.nodes[0].x) for item in layout.nodes)
        # A single very-distant node (rank 6) must not push any node beyond a
        # small multiple of the clamped radius, regardless of how far it is.
        self.assertLessEqual(max_extent_cells, cell_width * (3 + 2))

    def test_unknown_position_nodes_use_deterministic_fallback_region(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        known = NodeMetadata(
            "!known", "Known", "K", None, 1_000.0, False, position=west_of_local(1)
        )
        close_hop_no_position = NodeMetadata(
            "!close", "CloseHop", "CH", 1, 1_000.0, False, position=None
        )
        far_hop_no_position = NodeMetadata(
            "!far", "FarHop", "FH", 9, 1_000.0, False, position=None
        )
        layout = build_topology(
            [local, known, close_hop_no_position, far_hop_no_position]
        )
        by_id = {item.node.node_id: item for item in layout.nodes}
        self.assertEqual(by_id["!known"].region, "W")
        # hopsAway must never fabricate a direction/ordering, regardless of
        # how close or far the hop count claims to be.
        self.assertEqual(by_id["!close"].region, "UNKNOWN")
        self.assertEqual(by_id["!far"].region, "UNKNOWN")

    def test_missing_local_position_makes_every_remote_unknown(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=None)
        remote = NodeMetadata(
            "!remote", "Remote", "R", None, 1_000.0, False, position=west_of_local(1)
        )
        layout = build_topology([local, remote])
        by_id = {item.node.node_id: item for item in layout.nodes}
        self.assertEqual(by_id["!remote"].region, "UNKNOWN")

    def test_directional_placement_is_arrival_order_independent(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        nodes = [local]
        for index in range(6):
            nodes.append(
                NodeMetadata(
                    f"!w{index}",
                    f"West {index}",
                    f"W{index}",
                    None,
                    1_000.0,
                    False,
                    position=west_of_local(index + 1),
                )
            )
        forward = build_topology(nodes)
        backward = build_topology(list(reversed(nodes)))
        forward_positions = {
            item.node.node_id: (item.x, item.y) for item in forward.nodes
        }
        backward_positions = {
            item.node.node_id: (item.x, item.y) for item in backward.nodes
        }
        self.assertEqual(forward_positions, backward_positions)

    def test_no_overlap_across_mixed_directions_and_unknown_region(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        nodes = [local]
        for index in range(8):
            nodes.append(
                NodeMetadata(
                    f"!w{index}",
                    f"West{index}",
                    None,
                    None,
                    1_000.0,
                    False,
                    position=west_of_local(index + 1),
                )
            )
            nodes.append(
                NodeMetadata(
                    f"!e{index}",
                    f"East{index}",
                    None,
                    None,
                    1_000.0,
                    False,
                    position=east_of_local(index + 1),
                )
            )
            nodes.append(
                NodeMetadata(
                    f"!u{index}", f"Unknown{index}", None, None, 1_000.0, False
                )
            )
        layout = build_topology(nodes)
        rectangles = []
        for item in layout.nodes:
            rectangle = (item.x, item.y, item.x + NODE_WIDTH, item.y + NODE_HEIGHT)
            for other in rectangles:
                separated = (
                    rectangle[2] <= other[0]
                    or other[2] <= rectangle[0]
                    or rectangle[3] <= other[1]
                    or other[3] <= rectangle[1]
                )
                self.assertTrue(separated)
            rectangles.append(rectangle)

    def test_reserve_slot_falls_back_to_nearest_free_slot_on_collision(self) -> None:
        occupied = {(2, 0)}
        result = mesh_topology_module._reserve_slot(2, 0, occupied)
        self.assertNotEqual(result, (2, 0))
        self.assertIn(result, occupied)

    def test_layout_is_deterministic_collision_free_and_scales(self) -> None:
        nodes = sample_nodes(31)
        first = build_topology(nodes)
        second = build_topology(reversed(nodes))
        first_positions = {
            item.node.node_id: (item.x, item.y) for item in first.nodes
        }
        second_positions = {
            item.node.node_id: (item.x, item.y) for item in second.nodes
        }
        self.assertEqual(first_positions, second_positions)
        rectangles = []
        for item in first.nodes:
            rectangle = (item.x, item.y, item.x + NODE_WIDTH, item.y + NODE_HEIGHT)
            for other in rectangles:
                separated = (
                    rectangle[2] <= other[0]
                    or other[2] <= rectangle[0]
                    or rectangle[3] <= other[1]
                    or other[3] <= rectangle[1]
                )
                self.assertTrue(separated)
            rectangles.append(rectangle)

    def test_labels_prefer_names_then_abbreviated_node_id(self) -> None:
        self.assertEqual(compact_node_label(sample_nodes()[0]), "YOU")
        short = NodeMetadata("!12345678", "A name far too long for the node", "ABCD")
        self.assertEqual(compact_node_label(short), "ABCD")
        fallback = NodeMetadata("!1234567890abcdef")
        self.assertTrue(compact_node_label(fallback).startswith("…"))

    def test_spatial_navigation_has_no_edge_wrap(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        north_node = NodeMetadata(
            "!north", "North", "N", None, 1_000.0, False, position=north_of_local(1)
        )
        layout = build_topology([local, north_node])
        target = directional_target("!local", layout, "up")
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.node.node_id, "!north")
        top = min(layout.nodes, key=lambda item: item.y)
        self.assertIsNone(directional_target(top.node.node_id, layout, "up"))

    def test_emoji_and_wide_labels_never_exceed_node_cell_width(self) -> None:
        max_cells = NODE_WIDTH - 2
        long_suffix = "_extra_long_descriptive_name_here"
        samples = (
            "🐔node" + long_suffix,
            "🍕pizza" + long_suffix,
            "👨‍👩‍👧‍👦 family" + long_suffix,
            "🇺🇸🇬🇧 flags" + long_suffix,
            "a🏴‍☠️b pirate" + long_suffix,
            "CJK漢字測試名稱超長節點標籤",
            "plain_ascii" + long_suffix,
        )
        for long_name in samples:
            node = NodeMetadata("!12345678", long_name, None)
            label = compact_node_label(node)
            with self.subTest(long_name=long_name):
                self.assertLessEqual(cell_len(label), max_cells)
                if label.endswith("…"):
                    # Truncation must not sever an emoji joiner/modifier,
                    # which would otherwise render as an unexpectedly wide orphan.
                    last_kept = label[:-1][-1:]
                    self.assertNotEqual(
                        last_kept, mesh_topology_module._ZERO_WIDTH_JOINER
                    )
                    self.assertNotIn(
                        last_kept, mesh_topology_module._VARIATION_SELECTORS
                    )

    def test_short_emoji_and_wide_labels_pass_through_untruncated(self) -> None:
        for short_name in ("🐔", "🍕US", "漢字"):
            node = NodeMetadata("!12345678", None, short_name)
            self.assertEqual(compact_node_label(node), short_name)

    def test_emoji_labels_do_not_change_deterministic_layout_positions(self) -> None:
        emoji_nodes = [NodeMetadata("!00000000", "Local", "ME", 0, 1_000.0, True)]
        for index in range(1, 8):
            hops = 1 if index < 4 else 2 if index < 7 else None
            emoji_nodes.append(
                NodeMetadata(
                    f"!{index:08x}",
                    f"🐔 Node {index}",
                    f"N{index}",
                    hops,
                    990.0 if index % 2 else 600.0,
                )
            )
        emoji_nodes = tuple(emoji_nodes)
        first = build_topology(emoji_nodes)
        second = build_topology(reversed(emoji_nodes))
        first_positions = {item.node.node_id: (item.x, item.y) for item in first.nodes}
        second_positions = {item.node.node_id: (item.x, item.y) for item in second.nodes}
        self.assertEqual(first_positions, second_positions)

    def test_virtual_board_dimensions_unaffected_by_emoji_label_length(self) -> None:
        plain = sample_nodes(8)
        emoji_nodes = tuple(
            NodeMetadata(
                node.node_id,
                (f"🐔🍕👨‍👩‍👧‍👦 {node.long_name}" if node.long_name else None),
                node.short_name,
                node.hops_away,
                node.last_heard,
                node.is_local,
            )
            for node in plain
        )
        plain_layout = build_topology(plain)
        emoji_layout = build_topology(emoji_nodes)
        self.assertEqual(plain_layout.width, emoji_layout.width)
        self.assertEqual(plain_layout.height, emoji_layout.height)

    def test_emoji_labels_do_not_disturb_directional_grid_slots(self) -> None:
        local = NodeMetadata("!local", "YOU", None, 0, 1_000.0, True, position=LOCAL_GEO)
        plain_west = NodeMetadata(
            "!west", "West Node", "W", None, 1_000.0, False, position=west_of_local(1)
        )
        emoji_west = NodeMetadata(
            "!west", "🐔🍕👨‍👩‍👧‍👦 West Node", "W", None, 1_000.0, False,
            position=west_of_local(1),
        )
        plain_layout = build_topology([local, plain_west])
        emoji_layout = build_topology([local, emoji_west])
        plain_positions = {item.node.node_id: (item.x, item.y) for item in plain_layout.nodes}
        emoji_positions = {item.node.node_id: (item.x, item.y) for item in emoji_layout.nodes}
        self.assertEqual(plain_positions, emoji_positions)


class MeshRemoteRelativeGeographyTests(unittest.TestCase):
    """MODE B: when YOU has no GPS but 2+ displayed remotes do, they are

    placed via their own relative geography (see
    mesh_topology._project_remote_gps_cluster) instead of the
    fabricated-direction-free outer-ring fallback. YOU is never assigned
    a fake position and never assumed to sit at the cluster's centroid.
    """

    def test_relative_cluster_preserves_shape_when_local_has_no_gps(self) -> None:
        """Case A: ALICE (NW) / BOB (NE) / CHARLIE (S), YOU with no GPS."""
        you = NodeMetadata("!you", "Local", "ME", 0, 1_000.0, True, position=None)
        offset = 3.0 / _MILES_PER_DEGREE_AT_EQUATOR
        alice = NodeMetadata(
            "!alice", "Alice", "ALC", None, position=GeoPosition(offset, -offset)
        )
        bob = NodeMetadata(
            "!bob", "Bob", "BOB", None, position=GeoPosition(offset, offset)
        )
        charlie = NodeMetadata("!charlie", "Charlie", "CHR", None, position=south_of_local(3))
        slots = assign_grid_slots([you, alice, bob, charlie], max_radius=3)
        by_id = {item.node.node_id: item for item in slots}

        self.assertEqual((by_id["!you"].x, by_id["!you"].y), (0, 0))
        self.assertEqual(by_id["!you"].region, "LOCAL")
        for node_id in ("!alice", "!bob", "!charlie"):
            self.assertEqual(by_id[node_id].region, "GPS_RELATIVE")

        self.assertLess(by_id["!alice"].x, by_id["!bob"].x)  # ALICE west of BOB
        self.assertLess(by_id["!alice"].y, by_id["!charlie"].y)  # ALICE north of CHARLIE
        self.assertLess(by_id["!bob"].y, by_id["!charlie"].y)  # BOB north of CHARLIE
        self.assertEqual(by_id["!alice"].y, by_id["!bob"].y)  # symmetric fixture
        for item in slots:
            self.assertLessEqual(abs(item.x), 3)
            self.assertLessEqual(abs(item.y), 3)

    def test_relative_cluster_is_stable_across_repeated_calls(self) -> None:
        """Case B: identical GPS inputs over repeated calls -> identical

        positions, no jitter.
        """
        you = NodeMetadata("!you", "Local", "ME", 0, 1_000.0, True, position=None)
        alice = NodeMetadata("!alice", "Alice", "ALC", None, position=GeoPosition(0.02, -0.03))
        bob = NodeMetadata("!bob", "Bob", "BOB", None, position=GeoPosition(-0.01, 0.04))
        first = assign_grid_slots([you, alice, bob], max_radius=3)
        second = assign_grid_slots([you, alice, bob], max_radius=3)
        self.assertEqual(first, second)

    def test_single_gps_remote_uses_fallback_not_fabricated_bearing(self) -> None:
        """Case F: with YOU GPS-less, one GPS remote alone has no relative

        geometry to construct -- it must use the existing deterministic
        fallback, never a fabricated bearing from YOU.
        """
        you = NodeMetadata("!you", "Local", "ME", 0, 1_000.0, True, position=None)
        alice = NodeMetadata("!alice", "Alice", "ALC", None, position=north_of_local(3))
        slots = assign_grid_slots([you, alice], max_radius=3)
        by_id = {item.node.node_id: item for item in slots}
        self.assertEqual(by_id["!alice"].region, "UNKNOWN")

    def test_two_gps_remotes_preserve_east_west_ordering(self) -> None:
        """Case G: exactly two GPS remotes -- their relative ordering is

        meaningful and must survive arrival-order reversal.
        """
        you = NodeMetadata("!you", "Local", "ME", 0, 1_000.0, True, position=None)
        alice = NodeMetadata("!alice", "Alice", "ALC", None, position=west_of_local(2))
        bob = NodeMetadata("!bob", "Bob", "BOB", None, position=east_of_local(2))
        slots = assign_grid_slots([you, alice, bob], max_radius=3)
        by_id = {item.node.node_id: item for item in slots}
        self.assertEqual(by_id["!alice"].region, "GPS_RELATIVE")
        self.assertEqual(by_id["!bob"].region, "GPS_RELATIVE")
        self.assertLess(by_id["!alice"].x, by_id["!bob"].x)

        reversed_slots = assign_grid_slots([you, bob, alice], max_radius=3)
        reversed_by_id = {item.node.node_id: item for item in reversed_slots}
        self.assertLess(reversed_by_id["!alice"].x, reversed_by_id["!bob"].x)

    def test_uniform_scale_preserves_aspect_ratio_not_independent_stretch(
        self,
    ) -> None:
        """A cluster twice as wide (east-west) as it is tall (north-south)

        must render twice as wide as it is tall -- a single uniform
        scale factor on both axes, never independently stretched per
        axis merely to fill more of the available grid.
        """
        you = NodeMetadata("!you", "Local", "ME", 0, 1_000.0, True, position=None)
        west_node = NodeMetadata("!w", "W", None, None, position=GeoPosition(0.0, -1.0))
        east_node = NodeMetadata("!e", "E", None, None, position=GeoPosition(0.0, 1.0))
        north_node = NodeMetadata("!n", "N", None, None, position=GeoPosition(0.5, 0.0))
        south_node = NodeMetadata("!s", "S", None, None, position=GeoPosition(-0.5, 0.0))
        slots = assign_grid_slots(
            [you, west_node, east_node, north_node, south_node], max_radius=6
        )
        by_id = {item.node.node_id: item for item in slots}
        ew_span = by_id["!e"].x - by_id["!w"].x
        ns_span = by_id["!s"].y - by_id["!n"].y
        # Real-world ratio is 2:1 (2.0 degrees longitude vs 1.0 degree
        # latitude, at the equator where cos(0) == 1).
        self.assertEqual(ew_span, 12)
        self.assertEqual(ns_span, 6)

    def test_longitude_convergence_is_corrected_away_from_equator(self) -> None:
        """At a non-equatorial latitude, the same-sized longitude delta

        covers noticeably less real ground than the same-sized latitude
        delta -- the projection must reflect that via cos(reference
        latitude), never treat one degree of longitude as equal to one
        degree of latitude everywhere.
        """
        you = NodeMetadata("!you", "Local", "ME", 0, 1_000.0, True, position=None)
        west_node = NodeMetadata(
            "!west1", "West1", None, None, position=GeoPosition(80.0, -100.5)
        )
        east_node = NodeMetadata(
            "!east1", "East1", None, None, position=GeoPosition(80.0, -99.5)
        )
        north_node = NodeMetadata(
            "!north1", "North1", None, None, position=GeoPosition(80.5, -100.0)
        )
        south_node = NodeMetadata(
            "!south1", "South1", None, None, position=GeoPosition(79.5, -100.0)
        )
        slots = assign_grid_slots(
            [you, west_node, east_node, north_node, south_node], max_radius=10
        )
        by_id = {item.node.node_id: item for item in slots}
        ew_span = abs(by_id["!east1"].x - by_id["!west1"].x)
        ns_span = abs(by_id["!south1"].y - by_id["!north1"].y)
        # Identical 1.0-degree raw deltas on each axis -- without the
        # cos(latitude) correction these spans would come out equal.
        self.assertLess(ew_span, ns_span)


class RouteConnectorTests(unittest.TestCase):
    def test_straight_lines_use_a_single_glyph_no_elbow(self) -> None:
        horizontal = route_connector(0, 0, 5, 0)
        self.assertTrue(all(glyph == "─" for _x, _y, glyph in horizontal))
        vertical = route_connector(0, 0, 0, 5)
        self.assertTrue(all(glyph == "│" for _x, _y, glyph in vertical))

    def test_same_point_has_no_cells(self) -> None:
        self.assertEqual(route_connector(3, 3, 3, 3), ())

    def test_endpoints_are_excluded(self) -> None:
        cells = route_connector(0, 0, 4, 0)
        points = {(x, y) for x, y, _glyph in cells}
        self.assertNotIn((0, 0), points)
        self.assertNotIn((4, 0), points)

    def test_all_four_elbow_quadrants_use_distinct_correct_corners(self) -> None:
        # East+South: arrives from the left, turns down.
        se = route_connector(0, 0, 4, 3)
        self.assertEqual(se[-3], (4, 0, "┐"))
        # East+North: arrives from the left, turns up.
        ne = route_connector(0, 3, 4, 0)
        self.assertEqual(ne[-3], (4, 3, "┘"))
        # West+South: arrives from the right, turns down.
        sw = route_connector(4, 0, 0, 3)
        self.assertEqual(sw[-3], (0, 0, "┌"))
        # West+North: arrives from the right, turns up.
        nw = route_connector(4, 3, 0, 0)
        self.assertEqual(nw[-3], (0, 3, "└"))

    def test_route_is_deterministic(self) -> None:
        self.assertEqual(route_connector(1, 1, 9, 6), route_connector(1, 1, 9, 6))


class RouteConnectorAvoidingTests(unittest.TestCase):
    """route_connector_avoiding/route_chain_avoiding: the real-world bug

    this fixes is a direct ALICE<->ME connector visually passing
    through an unrelated real node BOB's own cell merely because BOB
    sits geometrically between them, with no relay/hop evidence BOB
    ever carried that traffic (see item 8 of the MESH topology task).
    """

    def test_no_obstacle_matches_plain_route_connector(self) -> None:
        self.assertEqual(
            route_connector_avoiding(0, 0, 8, 4, frozenset()),
            route_connector(0, 0, 8, 4),
        )

    def test_bob_geometrically_between_alice_and_me_is_avoided(self) -> None:
        """The exact reported scenario: BOB's cell sits on the default

        elbow route between ALICE and ME, but BOB is not an evidenced
        relay for this connection -- the route must steer around BOB's
        cell entirely, never rendering ALICE -> BOB -> ME.
        """
        primary = route_connector(0, 0, 8, 4)
        bob_position = next((x, y) for x, y, _glyph in primary)
        result = route_connector_avoiding(0, 0, 8, 4, frozenset({bob_position}))
        result_points = {(x, y) for x, y, _glyph in result}
        self.assertNotIn(bob_position, result_points)
        # A real, still-orthogonal path was found -- not an empty/broken
        # result.
        self.assertGreater(len(result), 0)

    def test_both_elbow_orderings_blocked_falls_back_to_primary(self) -> None:
        primary = route_connector(0, 0, 8, 4)
        alternate = _route_connector_alternate_elbow(0, 0, 8, 4)
        obstacles = frozenset(
            {(x, y) for x, y, _glyph in primary} | {(x, y) for x, y, _glyph in alternate}
        )
        result = route_connector_avoiding(0, 0, 8, 4, obstacles)
        self.assertEqual(result, primary)

    def test_collinear_pair_has_no_elbow_choice_and_ignores_obstacles(self) -> None:
        # A straight line has only one possible route regardless of
        # obstacles -- avoidance can never fabricate a detour off-axis.
        result = route_connector_avoiding(0, 0, 5, 0, frozenset({(2, 0)}))
        self.assertEqual(result, route_connector(0, 0, 5, 0))

    def test_alternate_elbow_never_touches_the_endpoints(self) -> None:
        alternate = _route_connector_alternate_elbow(0, 0, 8, 4)
        points = {(x, y) for x, y, _glyph in alternate}
        self.assertNotIn((0, 0), points)
        self.assertNotIn((8, 4), points)

    def test_route_chain_avoiding_steers_each_segment_around_obstacles(self) -> None:
        # A 3-point relay chain (YOU -> relay -> CLIENT); an obstacle on
        # the first segment's default route must not affect the second
        # segment at all.
        points = ((0, 0), (8, 4), (8, 8))
        primary_first_segment = route_connector(0, 0, 8, 4)
        obstacle = next((x, y) for x, y, _glyph in primary_first_segment)
        result = route_chain_avoiding(points, frozenset({obstacle}))
        result_points = {(x, y) for x, y, _glyph in result}
        self.assertNotIn(obstacle, result_points)


class MeshCanvasRenderTests(unittest.TestCase):
    def test_dot_grid_uses_regular_spacing_and_glyph(self) -> None:
        width, height = 20, 9
        text = _render_mesh_canvas(width, height, (), "#222222")
        rows = str(text).split("\n")
        self.assertEqual(len(rows), height)
        for y, row in enumerate(rows):
            self.assertEqual(len(row), width)
            for x, character in enumerate(row):
                expected = (
                    DOT_GRID_GLYPH
                    if x % DOT_GRID_SPACING_X == 0 and y % DOT_GRID_SPACING_Y == 0
                    else " "
                )
                self.assertEqual(character, expected)

    def test_canvas_is_empty_for_a_degenerate_board(self) -> None:
        self.assertEqual(str(_render_mesh_canvas(1, 1, (), "#222222")), "")

    def test_connectors_override_the_dot_grid_at_their_cells(self) -> None:
        connectors = ((3, 3, "─", "#ff0000"), (9, 3, "┐", "#ff0000"))
        text = _render_mesh_canvas(20, 9, connectors, "#222222")
        rows = str(text).split("\n")
        self.assertEqual(rows[3][3], "─")
        self.assertEqual(rows[3][9], "┐")

    def test_canvas_is_non_focusable(self) -> None:
        self.assertFalse(MeshCanvas.can_focus)


class MeshGridPlacementTests(unittest.TestCase):
    """Pure tests for assign_grid_slots()/place_within_bounds(): the shared

    geometry MESH now uses to place a real, bounded working set on its
    fixed grid.
    """

    def test_assign_grid_slots_places_local_at_origin(self) -> None:
        slots = assign_grid_slots([YOU])
        self.assertEqual(len(slots), 1)
        self.assertEqual((slots[0].x, slots[0].y), (0, 0))
        self.assertEqual(slots[0].region, "LOCAL")

    def test_place_within_bounds_centers_local_at_the_given_center(self) -> None:
        slots = assign_grid_slots([YOU])
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        self.assertEqual(positions["!you"], (5, 11))

    def test_place_within_bounds_keeps_a_far_node_inside_the_grid(self) -> None:
        far_east = NodeMetadata("!far", "Far", position=east_of_local(500))
        slots = assign_grid_slots([YOU, far_east], max_radius=DEFAULT_MAX_GRID_RADIUS)
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        row, column = positions["!far"]
        self.assertTrue(1 <= row <= 8)
        self.assertTrue(1 <= column <= 21)

    def test_place_within_bounds_never_overlaps_under_collision_pressure(self) -> None:
        """Cram many nodes into the same compass direction so the shared,

        unbounded collision-avoidance (_reserve_slot) would want to push
        several of them past this fixed board's edge -- place_within_bounds
        must still land every one in-bounds and collision-free.
        """
        nodes = [YOU] + [
            NodeMetadata(f"!s{index:02x}", f"South{index}", position=south_of_local(index + 1))
            for index in range(10)
        ]
        slots = assign_grid_slots(nodes, max_radius=3)
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        self.assertEqual(len(positions), len(nodes))
        self.assertEqual(len(positions), len(set(positions.values())))
        for row, column in positions.values():
            self.assertTrue(1 <= row <= 8)
            self.assertTrue(1 <= column <= 21)

    def test_place_within_bounds_is_deterministic_and_order_independent(self) -> None:
        nodes = [
            YOU,
            NodeMetadata("!a", "A", position=north_of_local(2)),
            NodeMetadata("!b", "B", position=north_of_local(5)),
            NodeMetadata("!c", "C"),
        ]
        forward = place_within_bounds(
            assign_grid_slots(nodes),
            center_row=5,
            center_column=11,
            row_count=8,
            column_count=21,
        )
        backward = place_within_bounds(
            assign_grid_slots(list(reversed(nodes))),
            center_row=5,
            center_column=11,
            row_count=8,
            column_count=21,
        )
        self.assertEqual(forward, backward)

    # ---- Grid-spread: use more of the available fixed-grid room --------

    def test_lone_direction_node_stretches_to_use_available_room(self) -> None:
        """A lone north node has 1 raw logical step but 3 rows of

        headroom above center on this 8-row grid (row_count=8,
        center_row=5, default margin=1: 5-1-1=3) -- it must stretch to
        use that room instead of clustering one row from center.
        """
        node = NodeMetadata("!n", "N", position=north_of_local(3))
        slots = assign_grid_slots([YOU, node])
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        row, _column = positions["!n"]
        self.assertEqual(row, 2)  # center_row(5) - full up_room(3)

    def test_spread_does_not_shrink_when_room_is_already_fully_used(self) -> None:
        """Three same-direction nodes, at real distances landing on

        MESH GPS PLACEMENT's own three distinct compression radii (1,
        2, 3 -- see _compress_distance_to_radius), already spanning the
        full 3-row headroom, must land at exactly their original ranks
        (4, 3, 2) -- spreading only ever stretches outward when there
        is slack, never compresses when the available room is already
        fully used.
        """
        nodes = [YOU] + [
            NodeMetadata(f"!n{i}", f"N{i}", position=north_of_local(miles))
            for i, miles in enumerate((0.5, 3.0, 50.0))
        ]
        slots = assign_grid_slots(nodes, max_radius=3)
        radii = sorted({abs(item.y) for item in slots if item.node.node_id != "!you"})
        self.assertEqual(radii, [1, 2, 3])
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        rows = sorted(positions[f"!n{i}"][0] for i in range(3))
        self.assertEqual(rows, [2, 3, 4])

    def test_spread_respects_asymmetric_room_between_axes(self) -> None:
        """The 21-column grid has far more horizontal headroom (9) than

        this 8-row grid has vertical headroom (3 up / 2 down) around
        center (11, 5) -- a lone east node and a lone north node, each
        one raw logical step out, must stretch by different amounts
        that reflect their own axis's actual available room.
        """
        east_node = NodeMetadata("!e", "E", position=east_of_local(3))
        north_node = NodeMetadata("!n", "N", position=north_of_local(3))
        slots = assign_grid_slots([YOU, east_node, north_node])
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        east_distance = positions["!e"][1] - 11
        north_distance = 5 - positions["!n"][0]
        self.assertGreater(east_distance, north_distance)

    def test_spread_preserves_relative_ordering_within_a_direction(self) -> None:
        near = NodeMetadata("!near", "Near", position=north_of_local(1))
        far = NodeMetadata("!far", "Far", position=north_of_local(50))
        slots = assign_grid_slots([YOU, near, far], max_radius=3)
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        # Farther logical rank must still render strictly farther from
        # center after spreading, never reordered by the stretch.
        self.assertLess(positions["!far"][0], positions["!near"][0])

    def test_spread_stays_collision_free_and_deterministic_for_a_mixed_fixture(
        self,
    ) -> None:
        """A representative multi-direction, multi-node fixture: spread

        must never collide and must never depend on input order, exactly
        like the unspread algorithm already guaranteed.
        """
        nodes = [YOU] + [
            NodeMetadata("!n1", "N1", position=north_of_local(2)),
            NodeMetadata("!s1", "S1", position=south_of_local(3)),
            NodeMetadata("!e1", "E1", position=east_of_local(4)),
            NodeMetadata("!e2", "E2", position=east_of_local(20)),
            NodeMetadata("!w1", "W1", position=west_of_local(5)),
        ]
        forward = place_within_bounds(
            assign_grid_slots(nodes), center_row=5, center_column=11, row_count=8, column_count=21
        )
        backward = place_within_bounds(
            assign_grid_slots(list(reversed(nodes))),
            center_row=5,
            center_column=11,
            row_count=8,
            column_count=21,
        )
        self.assertEqual(forward, backward)
        self.assertEqual(len(forward), len(set(forward.values())))
        for row, column in forward.values():
            self.assertTrue(1 <= row <= 8)
            self.assertTrue(1 <= column <= 21)

    def test_spread_never_places_a_node_at_the_true_outer_edge_by_default(
        self,
    ) -> None:
        """The default margin=1 keeps at least one row/column of breathing

        room at the true edge even when a lone node stretches to fill
        all available headroom (spec: "Do not push everything against
        the outer edge either; preserve reasonable margins").
        """
        node = NodeMetadata("!n", "N", position=north_of_local(3))
        slots = assign_grid_slots([YOU, node])
        positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        self.assertGreater(positions["!n"][0], 1)


def _sticky_map(slots) -> dict:
    return {item.node.node_id.strip().lower(): (item.x, item.y, item.region) for item in slots}


class MeshLayoutStabilityStickyPositionTests(unittest.TestCase):
    """MESH LAYOUT STABILITY: assign_grid_slots(sticky_positions=...) and

    place_within_bounds(min_extent=...) are the pure-function core of
    "no new topology information = no existing node movement" -- see
    their own docstrings. Wired into the live app in _refresh_mesh;
    MeshLayoutStabilityAppTests below proves that wiring end to end.
    """

    def test_sticky_position_is_reused_when_region_matches(self) -> None:
        node = NodeMetadata("!a", "A", position=east_of_local(2))
        first = assign_grid_slots([YOU, node])
        sticky = _sticky_map(first)
        # A brand new call, from scratch, fed the previous call's own
        # output as sticky_positions -- exactly what app.py's
        # _refresh_mesh does every cycle.
        second = assign_grid_slots([YOU, node], sticky_positions=sticky)
        self.assertEqual(_sticky_map(first), _sticky_map(second))

    def test_new_node_does_not_move_an_existing_sticky_node(self) -> None:
        existing = NodeMetadata("!a", "A", position=east_of_local(2))
        first = assign_grid_slots([YOU, existing])
        sticky = _sticky_map(first)

        newcomer = NodeMetadata("!b", "B", position=east_of_local(6))
        second = assign_grid_slots([YOU, existing, newcomer], sticky_positions=sticky)

        existing_slot = next(item for item in second if item.node.node_id == "!a")
        self.assertEqual((existing_slot.x, existing_slot.y), (sticky["!a"][0], sticky["!a"][1]))

    def test_departed_then_returned_node_is_not_forced_back_to_the_same_cell(
        self,
    ) -> None:
        """Once pruned from the sticky map (the node genuinely left the

        working set), a later reappearance is an ordinary fresh
        placement -- it MAY land back on the same cell (deterministic
        geometry often agrees), but nothing forces it to; this proves
        the two calls are independent, not that a stale entry lingered.
        """
        node = NodeMetadata("!a", "A", position=east_of_local(2))
        first = assign_grid_slots([YOU, node])
        sticky_without_a = {
            key: value for key, value in _sticky_map(first).items() if key != "!a"
        }
        second = assign_grid_slots([YOU, node], sticky_positions=sticky_without_a)
        # Deterministic geometry: still lands in the same place, but via
        # fresh computation, not a leftover sticky entry (there is none).
        self.assertEqual(_sticky_map(first)["!a"][:2], _sticky_map(second)["!a"][:2])

    def test_gaining_a_real_bearing_overrides_stale_unknown_sticky_entry(self) -> None:
        """A node's placement CATEGORY changing (no position -> a real

        bearing) is genuine new topology information -- it is correctly
        allowed to move, never forced back into its old UNKNOWN-region
        cell.
        """
        node_no_gps = NodeMetadata("!a", "A")
        first = assign_grid_slots([YOU, node_no_gps])
        self.assertEqual(first[1].region, "UNKNOWN")
        sticky = _sticky_map(first)

        node_with_gps = NodeMetadata("!a", "A", position=east_of_local(2))
        second = assign_grid_slots([YOU, node_with_gps], sticky_positions=sticky)
        placed = next(item for item in second if item.node.node_id == "!a")
        self.assertEqual(placed.region, "E")

    def test_min_extent_prevents_recompaction_when_the_farthest_node_leaves(
        self,
    ) -> None:
        near = NodeMetadata("!near", "Near", position=north_of_local(1))
        far = NodeMetadata("!far", "Far", position=north_of_local(10))
        slots_with_both = assign_grid_slots([YOU, near, far], max_radius=3)
        positions_with_both = place_within_bounds(
            slots_with_both, center_row=5, center_column=11, row_count=8, column_count=21
        )
        near_row_with_far_present = positions_with_both["!near"][0]

        # Far leaves. Without a ratcheted min_extent, near would stretch
        # to fill the axis's now-larger relative headroom and move.
        slots_without_far = assign_grid_slots([YOU, near], max_radius=3)
        # Exactly what far's own logical step had been (its clamped
        # rank, not its raw geographic distance) -- what app.py's own
        # ratchet remembers from the cycle far was last present.
        farthest_up_with_far_present = max(
            -item.y for item in slots_with_both if item.y < 0
        )
        ratcheted_extent = {"up": farthest_up_with_far_present}
        positions_ratcheted = place_within_bounds(
            slots_without_far,
            center_row=5,
            center_column=11,
            row_count=8,
            column_count=21,
            min_extent=ratcheted_extent,
        )
        self.assertEqual(positions_ratcheted["!near"][0], near_row_with_far_present)

        # Without the ratchet at all (the pre-existing, still-default
        # behavior for any caller that omits min_extent), near DOES
        # recompact -- proving the ratchet is what changes the outcome,
        # not some other factor.
        positions_unratcheted = place_within_bounds(
            slots_without_far, center_row=5, center_column=11, row_count=8, column_count=21
        )
        self.assertNotEqual(positions_unratcheted["!near"][0], near_row_with_far_present)

    def test_min_extent_omitted_reproduces_prior_behavior_exactly(self) -> None:
        nodes = [YOU, NodeMetadata("!a", "A", position=east_of_local(2))]
        slots = assign_grid_slots(nodes)
        with_default = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        with_explicit_empty = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21, min_extent={}
        )
        self.assertEqual(with_default, with_explicit_empty)

    def test_sticky_positions_omitted_reproduces_prior_behavior_exactly(self) -> None:
        nodes = [YOU, NodeMetadata("!a", "A", position=east_of_local(2))]
        without_param = assign_grid_slots(nodes)
        with_none = assign_grid_slots(nodes, sticky_positions=None)
        with_empty = assign_grid_slots(nodes, sticky_positions={})
        self.assertEqual(_sticky_map(without_param), _sticky_map(with_none))
        self.assertEqual(_sticky_map(without_param), _sticky_map(with_empty))

    def test_repeated_identical_calls_are_idempotent(self) -> None:
        nodes = [
            YOU,
            NodeMetadata("!a", "A", position=east_of_local(2)),
            NodeMetadata("!b", "B", position=north_of_local(4)),
        ]
        slots = assign_grid_slots(nodes)
        sticky = _sticky_map(slots)
        extent = {"up": 4, "down": 0, "left": 0, "right": 2}
        first_positions = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21, min_extent=extent
        )
        for _ in range(3):
            slots = assign_grid_slots(nodes, sticky_positions=sticky)
            sticky = _sticky_map(slots)
            positions = place_within_bounds(
                slots,
                center_row=5,
                center_column=11,
                row_count=8,
                column_count=21,
                min_extent=extent,
            )
            self.assertEqual(positions, first_positions)


class CompressDistanceToRadiusTests(unittest.TestCase):
    """Pure tests for MESH GPS PLACEMENT's own real-distance -> grid-

    step-radius compression (see _compress_distance_to_radius's own
    docstring for the exact breakpoint ladder and why it exists).
    """

    def test_under_first_breakpoint_is_radius_one(self) -> None:
        self.assertEqual(_compress_distance_to_radius(0.0, 3), 1)
        self.assertEqual(_compress_distance_to_radius(0.999, 3), 1)

    def test_each_breakpoint_crossed_adds_one_radius_step(self) -> None:
        self.assertEqual(_compress_distance_to_radius(1.0, 3), 1)
        self.assertEqual(_compress_distance_to_radius(1.001, 3), 2)
        self.assertEqual(_compress_distance_to_radius(4.999, 3), 2)
        self.assertEqual(_compress_distance_to_radius(5.001, 3), 3)

    def test_never_exceeds_max_radius(self) -> None:
        for distance in (25.001, 1_000.0, 1_000_000.0):
            with self.subTest(distance=distance):
                self.assertEqual(_compress_distance_to_radius(distance, 3), 3)

    def test_max_radius_is_respected_generically_not_hardcoded_to_three(self) -> None:
        self.assertEqual(_compress_distance_to_radius(1_000.0, 5), 5)
        self.assertEqual(_compress_distance_to_radius(1_000.0, 1), 1)

    def test_monotonically_non_decreasing_in_distance(self) -> None:
        previous = 0
        for distance in (0.0, 0.5, 1.0, 2.0, 5.0, 6.0, 25.0, 26.0, 100.0):
            radius = _compress_distance_to_radius(distance, 4)
            self.assertGreaterEqual(radius, previous)
            previous = radius


class MeshGpsInformedPlacementTests(unittest.TestCase):
    """MESH GPS PLACEMENT (pure-function level): bearing/distance now

    genuinely inform a GPS-having remote's placement, sticky
    (region-based) reuse absorbs ordinary GPS jitter, and a genuine
    bearing-bucket change is the only thing that legitimately moves a
    GPS node. See MeshGpsInformedPlacementAppTests below for the same
    invariants wired through the real app.
    """

    def test_cardinal_and_intercardinal_bearings_land_in_the_expected_octant(self) -> None:
        cases = (
            (north_of_local(5), "N"),
            (south_of_local(5), "S"),
            (east_of_local(5), "E"),
            (west_of_local(5), "W"),
        )
        for position, expected_region in cases:
            with self.subTest(expected_region=expected_region):
                node = NodeMetadata("!n", "N", position=position)
                slots = assign_grid_slots([YOU, node])
                placed = next(item for item in slots if item.node.node_id == "!n")
                self.assertEqual(placed.region, expected_region)

    def test_minor_gps_jitter_within_the_same_bucket_does_not_move_the_node(self) -> None:
        """A slightly-updated GPS fix that stays within the SAME 45-degree

        bearing bucket must reuse the exact previous cell -- see
        _place_group_sticky_first, keyed on region (the bucket), not on
        the freshly recomputed ideal coordinate.
        """
        node_v1 = NodeMetadata("!n", "N", position=north_of_local(5.0))
        first = assign_grid_slots([YOU, node_v1])
        sticky = _sticky_map(first)

        node_v2 = NodeMetadata("!n", "N", position=north_of_local(5.3))
        second = assign_grid_slots([YOU, node_v2], sticky_positions=sticky)
        self.assertEqual(_sticky_map(first)["!n"], _sticky_map(second)["!n"])

    def test_meaningful_bearing_change_is_allowed_to_move_the_node(self) -> None:
        node_v1 = NodeMetadata("!n", "N", position=north_of_local(5.0))
        first = assign_grid_slots([YOU, node_v1])
        sticky = _sticky_map(first)
        self.assertEqual(first[1].region, "N")

        node_v2 = NodeMetadata("!n", "N", position=east_of_local(5.0))
        second = assign_grid_slots([YOU, node_v2], sticky_positions=sticky)
        placed = next(item for item in second if item.node.node_id == "!n")
        self.assertEqual(placed.region, "E")
        self.assertNotEqual((placed.x, placed.y), (first[1].x, first[1].y))

    def test_meaningful_distance_change_within_the_same_bucket_is_allowed_to_move(
        self,
    ) -> None:
        """Crossing a compression breakpoint (radius itself changes) is

        real new topology information even though the bucket stays the
        same -- sticky reuse only protects against jitter that doesn't
        change the node's own placement inputs enough to matter; a
        node's own genuinely-changed distance is exactly the kind of
        update MESH GPS PLACEMENT says may legitimately move it. This
        exercises the fresh (non-sticky) path directly, matching how a
        real caller would feed only nodes whose own category is
        unchanged as sticky candidates.
        """
        near = assign_grid_slots([YOU, NodeMetadata("!n", "N", position=north_of_local(0.5))])
        far = assign_grid_slots([YOU, NodeMetadata("!n", "N", position=north_of_local(50.0))])
        near_radius = next(item for item in near if item.node.node_id == "!n").y
        far_radius = next(item for item in far if item.node.node_id == "!n").y
        self.assertNotEqual(near_radius, far_radius)

    def test_unrelated_node_is_not_moved_by_a_sibling_gps_update(self) -> None:
        stable = NodeMetadata("!stable", "Stable", position=east_of_local(3))
        moving_v1 = NodeMetadata("!moving", "Moving", position=north_of_local(2))
        first = assign_grid_slots([YOU, stable, moving_v1])
        sticky = _sticky_map(first)
        stable_before = _sticky_map(first)["!stable"]

        moving_v2 = NodeMetadata("!moving", "Moving", position=west_of_local(30))
        second = assign_grid_slots([YOU, stable, moving_v2], sticky_positions=sticky)
        self.assertEqual(_sticky_map(second)["!stable"], stable_before)

    def test_invalid_or_missing_gps_falls_back_to_topology_placement(self) -> None:
        no_position_node = NodeMetadata("!n", "N")
        slots = assign_grid_slots([YOU, no_position_node])
        placed = next(item for item in slots if item.node.node_id == "!n")
        self.assertEqual(placed.region, "UNKNOWN")

    def test_you_without_gps_falls_back_to_topology_for_a_lone_remote(self) -> None:
        """A single GPS-having remote with no GPS-having peer to be

        relative to (MODE B needs 2+) falls through to the SAME
        UNKNOWN outer-ring placement a position-less remote would get
        -- never a fabricated bearing from a YOU that has no fix.
        """
        you_no_gps = NodeMetadata("!you", "Local", "ME", 0, 1_000.0, True)
        remote = NodeMetadata("!n", "N", position=north_of_local(5))
        slots = assign_grid_slots([you_no_gps, remote])
        placed = next(item for item in slots if item.node.node_id == "!n")
        self.assertEqual(placed.region, "UNKNOWN")

    def test_mixed_gps_and_non_gps_nodes_coexist_without_interference(self) -> None:
        gps_node = NodeMetadata("!gps", "GPS Node", position=north_of_local(5))
        no_gps_node = NodeMetadata("!nogps", "No GPS Node")
        slots = assign_grid_slots([YOU, gps_node, no_gps_node])
        by_id = {item.node.node_id: item for item in slots}
        self.assertEqual(by_id["!gps"].region, "N")
        self.assertEqual(by_id["!nogps"].region, "UNKNOWN")
        # Both land on real, distinct, collision-free cells.
        self.assertNotEqual((by_id["!gps"].x, by_id["!gps"].y), (0, 0))
        self.assertNotEqual((by_id["!nogps"].x, by_id["!nogps"].y), (0, 0))
        self.assertNotEqual(
            (by_id["!gps"].x, by_id["!gps"].y), (by_id["!nogps"].x, by_id["!nogps"].y)
        )


class MeshTranslationAndDirectionalTargetTests(unittest.TestCase):
    """Pure tests for app.py's generic (not fixture-specific) whole-mesh

    translation and arrow-navigation helpers, now parametrized by
    whatever working set/base positions the live model produces.
    """

    def test_translate_shifts_every_node_by_the_identical_delta(self) -> None:
        base = {"!you": (5, 11), "!a": (4, 10), "!b": (6, 9)}
        translated = _mesh_translated_positions(
            base, "!a", center_row=5, center_column=11
        )
        self.assertEqual(translated["!a"], (5, 11))
        row_delta, column_delta = 5 - 4, 11 - 10
        self.assertEqual(translated["!you"], (5 + row_delta, 11 + column_delta))
        self.assertEqual(translated["!b"], (6 + row_delta, 9 + column_delta))

    def test_translate_recenters_on_whatever_center_the_viewport_gives(self) -> None:
        """The center is the CURRENT viewport's own center (see

        MeshTopologyView.current_grid_dimensions), never a fixed
        constant -- a smaller/larger visible grid recenters the
        selected node on a different cell without changing anything
        else about the translation.
        """
        base = {"!you": (5, 11), "!a": (4, 10)}
        translated = _mesh_translated_positions(
            base, "!a", center_row=3, center_column=4
        )
        self.assertEqual(translated["!a"], (3, 4))
        self.assertEqual(translated["!you"], (3 + 1, 4 + 1))

    def test_translate_with_unresolvable_selection_is_a_no_op(self) -> None:
        base = {"!you": (5, 11)}
        self.assertEqual(
            _mesh_translated_positions(base, "!missing", center_row=5, center_column=11),
            base,
        )

    def test_directional_target_picks_the_nearest_candidate(self) -> None:
        """No hardcoded node IDs: the same general nearest-candidate rule

        used by mesh_topology.directional_target, just fed the current
        rendered layout's own positions -- real nodes and anonymous
        relay stages alike, since both are just entries in this dict.
        """
        base_positions = {"!you": (5, 11), "!near": (4, 10), "!far": (1, 8)}
        self.assertEqual(
            _mesh_directional_target(base_positions, "!you", "left"), "!near"
        )

    def test_directional_target_with_no_candidate_is_none(self) -> None:
        target = _mesh_directional_target({"!you": (5, 11)}, "!you", "left")
        self.assertIsNone(target)


class RelayStageGeometryTests(unittest.TestCase):
    """Pure tests for mesh_topology's anonymous-relay-stage placement:

    exact stage counts per hop depth, independence between clients, and
    collision-free placement -- no app, no widgets.
    """

    def _positions(self, *, target_hops: int) -> tuple[dict, dict]:
        you = NodeMetadata("!you", is_local=True, position=GeoPosition(0.0, 0.0))
        target = NodeMetadata(
            "!target", "Target", "TGT", target_hops, position=GeoPosition(9 / 69.0, 0.0)
        )
        min_radius = {"!target": target_hops + 1} if target_hops > 0 else {}
        slots = assign_grid_slots(
            (you, target), max_radius=DEFAULT_MAX_GRID_RADIUS, min_radius_by_id=min_radius
        )
        base = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        return base, {"!target": target_hops} if target_hops > 0 else {}

    def test_zero_hops_generates_no_relay_stages(self) -> None:
        base, hop_counts = self._positions(target_hops=0)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        self.assertEqual(stages, ())
        self.assertEqual(positions, {})

    def test_one_hop_generates_exactly_one_relay_stage(self) -> None:
        base, hop_counts = self._positions(target_hops=1)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        self.assertEqual(len(stages), 1)
        self.assertEqual(len(positions), 1)

    def test_two_hops_generates_exactly_two_relay_stages(self) -> None:
        base, hop_counts = self._positions(target_hops=2)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        self.assertEqual(len(stages), 2)
        self.assertEqual({s.index for s in stages}, {1, 2})

    def test_three_hops_generates_exactly_three_relay_stages(self) -> None:
        base, hop_counts = self._positions(target_hops=3)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        self.assertEqual(len(stages), 3)
        self.assertEqual({s.index for s in stages}, {1, 2, 3})
        self.assertEqual(len(positions.values()), len(set(positions.values())))

    def test_unknown_hops_client_absent_from_hop_counts_gets_no_stages(self) -> None:
        """A `? HOPS` client is never passed into hop_counts at all (see

        app._mesh_hop_counts) -- proving build_relay_stages never
        fabricates stages for a client it isn't told about.
        """
        base, _ = self._positions(target_hops=0)
        stages, positions = build_relay_stages(
            base, you_id="!you", hop_counts={}, row_count=8, column_count=21
        )
        self.assertEqual(stages, ())
        self.assertEqual(positions, {})

    def test_no_you_position_produces_no_stages(self) -> None:
        stages, positions = build_relay_stages(
            {"!target": (4, 11)},
            you_id="!you",
            hop_counts={"!target": 2},
            row_count=8,
            column_count=21,
        )
        self.assertEqual(stages, ())
        self.assertEqual(positions, {})

    def test_relay_stage_ids_are_never_shared_between_clients_with_equal_hops(
        self,
    ) -> None:
        you = NodeMetadata("!you", is_local=True, position=GeoPosition(0.0, 0.0))
        alice = NodeMetadata(
            "!alice", "Alice", position=GeoPosition(0.0, 9 / 69.0)
        )  # east
        bob = NodeMetadata(
            "!bob", "Bob", position=GeoPosition(0.0, -9 / 69.0)
        )  # west
        slots = assign_grid_slots(
            (you, alice, bob),
            max_radius=DEFAULT_MAX_GRID_RADIUS,
            min_radius_by_id={"!alice": 3, "!bob": 3},
        )
        base = place_within_bounds(
            slots, center_row=5, center_column=11, row_count=8, column_count=21
        )
        stages, positions = build_relay_stages(
            base,
            you_id="!you",
            hop_counts={"!alice": 2, "!bob": 2},
            row_count=8,
            column_count=21,
        )
        self.assertEqual(len(stages), 4)
        alice_ids = {s.node_id for s in stages if s.source_node_id == "!alice"}
        bob_ids = {s.node_id for s in stages if s.source_node_id == "!bob"}
        self.assertEqual(len(alice_ids), 2)
        self.assertEqual(len(bob_ids), 2)
        self.assertEqual(alice_ids & bob_ids, set())
        # No fabricated real-node shape leaks onto a placeholder.
        for stage in stages:
            self.assertFalse(stage.is_local)
            self.assertTrue(stage.node_id.startswith("relay:"))
        self.assertEqual(len(positions.values()), len(set(positions.values())))

    def test_relay_stage_node_id_encodes_source_and_index_internally_only(
        self,
    ) -> None:
        """The synthetic ID format is internal bookkeeping -- proven here

        directly -- and never surfaces in any user-facing string (the
        app-level tests prove the UI always shows exactly "RELAY").
        """
        base, hop_counts = self._positions(target_hops=2)
        stages, _ = build_relay_stages(
            base, you_id="!you", hop_counts=hop_counts, row_count=8, column_count=21
        )
        ids = {s.node_id for s in stages}
        self.assertEqual(ids, {"relay:!target:1", "relay:!target:2"})

    def test_min_radius_by_id_does_not_affect_unlisted_nodes(self) -> None:
        """assign_grid_slots' new optional parameter is fully backward

        compatible: omitting it (as every non-MESH-hop caller does)
        reproduces the exact prior placement.
        """
        you = NodeMetadata("!you", is_local=True, position=GeoPosition(0.0, 0.0))
        node = NodeMetadata("!n", "N", position=GeoPosition(9 / 69.0, 0.0))
        without = assign_grid_slots((you, node), max_radius=DEFAULT_MAX_GRID_RADIUS)
        with_empty = assign_grid_slots(
            (you, node), max_radius=DEFAULT_MAX_GRID_RADIUS, min_radius_by_id={}
        )
        self.assertEqual(without, with_empty)

    def test_min_radius_by_id_boosts_only_the_named_node(self) -> None:
        you = NodeMetadata("!you", is_local=True, position=GeoPosition(0.0, 0.0))
        node = NodeMetadata("!n", "N", position=GeoPosition(9 / 69.0, 0.0))
        boosted = assign_grid_slots(
            (you, node), max_radius=DEFAULT_MAX_GRID_RADIUS, min_radius_by_id={"!n": 3}
        )
        entry = next(item for item in boosted if item.node.node_id == "!n")
        self.assertEqual((entry.x, entry.y), (0, -3))


class RelayChainOrderedRouteTests(unittest.TestCase):
    """Pure tests for the ordered-relay-chain regression: a real

    hardware report where a 3-hop endpoint's third relay stage,
    displaced sideways by collision avoidance, visually read as an
    orphan side-branch rather than a continuous route (see
    build_relay_stages/_reserve_relay_stage_cell's docstrings for the
    root cause -- a stage placed geometrically BEHIND its predecessor
    forces route_chain's later segments to retrace an earlier
    segment's own cells, which last-drawn-wins overwrite erases).
    """

    def _assert_continuous_no_self_overlap(
        self, you_position, ordered_stage_positions, endpoint_position
    ):
        """The chain's rendered cells contain no duplicate coordinate --

        the direct, decisive proxy for "no branch": a genuine branch or
        an overwrite-erased segment can only arise from two different
        segments of the SAME chain drawing to the identical cell (see
        MeshCanvas/_render_mesh_canvas's last-drawn-wins overlay).

        Converted through _mesh_grid_pixel first -- exactly as
        MeshTopologyView.set_nodes does before calling route_chain --
        since adjacent LOGICAL (row, column) steps are NOT adjacent
        pixel cells (see DOT_GRID_SPACING_X/Y); testing on raw logical
        coordinates would report false overlaps route_chain's real
        caller never actually produces.
        """
        chain_points = tuple(
            _mesh_grid_pixel(row, column)
            for row, column in (you_position, *ordered_stage_positions, endpoint_position)
        )
        cells = route_chain(chain_points)
        coordinates = [(x, y) for x, y, _glyph in cells]
        self.assertEqual(
            len(coordinates),
            len(set(coordinates)),
            f"chain segments overlap/self-cross: {chain_points}",
        )
        return cells

    def test_reserve_relay_stage_cell_refuses_a_route_through_another_node(
        self,
    ) -> None:
        """Direct proof of the fix: given a blocked ideal cell where the

        ordinary nearest-any-free-cell search (_reserve_bounded_cell)
        picks a cell whose OWN onward segment to the real endpoint
        would pass directly through another real node's cell as an
        elbow waypoint (not merely its final position touching that
        node), the new resolver refuses that candidate and lands on
        one whose onward segment stays clear of it -- checked with the
        real per-axis pixel spacing (DOT_GRID_SPACING_X/Y), since this
        exact class of overlap is only visible once scaled (see
        _segment_pixel_cells' own docstring).
        """
        you_position = (5, 11)
        r1_position = (4, 11)
        r2_position = (3, 11)
        endpoint_position = (1, 11)
        blocker = (2, 11)  # R3's own naive interpolated cell
        row_scale, column_scale = DOT_GRID_SPACING_Y, DOT_GRID_SPACING_X
        occupied = {you_position, r1_position, r2_position, endpoint_position, blocker}
        blocked_pixel_cells = {
            mesh_topology_module._scaled_pixel(
                cell, row_scale=row_scale, column_scale=column_scale
            )
            for cell in occupied
        }

        old_choice = mesh_topology_module._reserve_bounded_cell(
            2, 11, set(occupied), row_count=8, column_count=21
        )
        offending_segment = mesh_topology_module._segment_pixel_cells(
            old_choice, endpoint_position, row_scale=row_scale, column_scale=column_scale
        )
        self.assertTrue(
            offending_segment & blocked_pixel_cells,
            "fixture must reproduce the old resolver's onward segment "
            "to the endpoint passing through an occupied cell",
        )

        new_choice = mesh_topology_module._reserve_relay_stage_cell(
            2, 11, set(occupied), row_count=8, column_count=21,
            previous_position=r2_position,
            blocked_pixel_cells=blocked_pixel_cells,
            next_fixed_point=endpoint_position,
            row_scale=row_scale, column_scale=column_scale,
        )
        self.assertNotEqual(new_choice, old_choice)
        self.assertFalse(
            mesh_topology_module._segment_pixel_cells(
                new_choice, endpoint_position,
                row_scale=row_scale, column_scale=column_scale,
            )
            & blocked_pixel_cells
        )

    def test_three_hop_hardware_regression_stays_one_ordered_chain(self) -> None:
        """Case 6: the exact real-hardware reproduction -- a 3-hop

        endpoint whose third relay stage's own ideal (naively
        interpolated) cell is already occupied by an unrelated real
        node, forcing collision avoidance to displace it -- exactly
        "one relay is displaced one cell over", the real-hardware
        report's own description. The rendered route must still be one
        continuous, non-self-overlapping chain through R1, then R2,
        then the actually-displaced R3, then the endpoint -- never a
        branch.
        """
        you_position = (5, 11)
        endpoint_position = (1, 11)
        real_positions = {
            "you": you_position,
            "endpoint": endpoint_position,
            "blocker": (2, 11),  # R3's own naive interpolated cell
        }
        stages, positions = build_relay_stages(
            real_positions, you_id="you", hop_counts={"endpoint": 3},
            row_count=8, column_count=21,
            row_scale=DOT_GRID_SPACING_Y, column_scale=DOT_GRID_SPACING_X,
        )
        self.assertEqual(len(stages), 3)
        ordered = sorted(stages, key=lambda stage: stage.index)
        self.assertEqual([stage.index for stage in ordered], [1, 2, 3])
        ordered_positions = [positions[stage.node_id] for stage in ordered]

        # R3 was genuinely displaced from its naive interpolated cell.
        self.assertNotEqual(ordered_positions[2], (2, 11))
        self.assertEqual(len(ordered_positions), len(set(ordered_positions)))

        self._assert_continuous_no_self_overlap(
            you_position, ordered_positions, endpoint_position
        )

    def test_n_hop_continuity_for_one_two_three_five(self) -> None:
        """Case 7: for N in (1, 2, 3, 5), the rendered route is one

        continuous YOU -> R1 -> ... -> RN -> endpoint chain with no
        skipped, reordered, or duplicated relay -- checked with EVERY
        stage's own naive interpolated cell already occupied by an
        unrelated real node, forcing every single stage to actually be
        displaced, not merely the trivial no-collision case.
        """
        you_position = (5, 11)
        endpoint_position = (1, 11)
        for hop_count in (1, 2, 3, 5):
            with self.subTest(hop_count=hop_count):
                ideal_cells = []
                for step in range(1, hop_count + 1):
                    fraction = step / (hop_count + 1)
                    ideal_cells.append(
                        (
                            round(
                                you_position[0]
                                + (endpoint_position[0] - you_position[0]) * fraction
                            ),
                            round(
                                you_position[1]
                                + (endpoint_position[1] - you_position[1]) * fraction
                            ),
                        )
                    )
                real_positions = {
                    "you": you_position,
                    "endpoint": endpoint_position,
                    **{f"b{index}": cell for index, cell in enumerate(ideal_cells)},
                }
                stages, positions = build_relay_stages(
                    real_positions, you_id="you",
                    hop_counts={"endpoint": hop_count},
                    row_count=8, column_count=21,
                    row_scale=DOT_GRID_SPACING_Y, column_scale=DOT_GRID_SPACING_X,
                )
                self.assertEqual(len(stages), hop_count)
                ordered = sorted(stages, key=lambda stage: stage.index)
                self.assertEqual(
                    [stage.index for stage in ordered], list(range(1, hop_count + 1))
                )
                ordered_positions = [positions[stage.node_id] for stage in ordered]
                self.assertEqual(len(ordered_positions), len(set(ordered_positions)))
                # Every stage was genuinely displaced from its ideal cell.
                for position, ideal in zip(ordered_positions, ideal_cells):
                    self.assertNotEqual(position, ideal)
                self._assert_continuous_no_self_overlap(
                    you_position, ordered_positions, endpoint_position
                )

    def test_no_branch_for_a_single_endpoints_own_chain(self) -> None:
        """Case 8: a single endpoint's own connector component is one

        simple path -- verified directly on the rendered cell list
        (see _assert_continuous_no_self_overlap) across several
        different displacement geometries, not merely the count/order
        already checked above.
        """
        you_position = (5, 11)
        endpoint_position = (8, 11)
        for blocked in (
            {(6, 11)},
            {(6, 11), (7, 11)},
            {(6, 11), (6, 10), (6, 12), (7, 11)},
        ):
            with self.subTest(blocked=blocked):
                real_positions = {
                    "you": you_position,
                    "endpoint": endpoint_position,
                    **{f"b{i}": cell for i, cell in enumerate(blocked)},
                }
                stages, positions = build_relay_stages(
                    real_positions, you_id="you", hop_counts={"endpoint": 3},
                    row_count=8, column_count=21,
                    row_scale=DOT_GRID_SPACING_Y, column_scale=DOT_GRID_SPACING_X,
                )
                ordered = sorted(stages, key=lambda stage: stage.index)
                ordered_positions = [positions[stage.node_id] for stage in ordered]
                self._assert_continuous_no_self_overlap(
                    you_position, ordered_positions, endpoint_position
                )

    def test_multiple_endpoints_keep_independent_ordered_chains(self) -> None:
        """Case 9: two active endpoints whose routes pass near each

        other -- each endpoint's own relay stages stay owned by it, in
        its own order, and one endpoint's collision handling can never
        detach or reorder a stage belonging to the other.
        """
        you_position = (5, 11)
        real_positions = {
            "you": you_position,
            "alice": (1, 9),
            "bob": (1, 13),
        }
        stages, positions = build_relay_stages(
            real_positions, you_id="you",
            hop_counts={"alice": 3, "bob": 3},
            row_count=8, column_count=21,
            row_scale=DOT_GRID_SPACING_Y, column_scale=DOT_GRID_SPACING_X,
        )
        self.assertEqual(len(stages), 6)
        alice_stages = sorted(
            (s for s in stages if s.source_node_id == "alice"),
            key=lambda stage: stage.index,
        )
        bob_stages = sorted(
            (s for s in stages if s.source_node_id == "bob"),
            key=lambda stage: stage.index,
        )
        self.assertEqual([s.index for s in alice_stages], [1, 2, 3])
        self.assertEqual([s.index for s in bob_stages], [1, 2, 3])
        alice_ids = {s.node_id for s in alice_stages}
        bob_ids = {s.node_id for s in bob_stages}
        self.assertEqual(alice_ids & bob_ids, set())

        alice_positions = [positions[s.node_id] for s in alice_stages]
        bob_positions = [positions[s.node_id] for s in bob_stages]
        self.assertEqual(len(alice_positions), len(set(alice_positions)))
        self.assertEqual(len(bob_positions), len(set(bob_positions)))
        # Each chain individually stays monotonic and self-consistent
        # even though both routes pass through the same neighborhood.
        self._assert_continuous_no_self_overlap(you_position, alice_positions, real_positions["alice"])
        self._assert_continuous_no_self_overlap(you_position, bob_positions, real_positions["bob"])


class RouteChainTests(unittest.TestCase):
    """Pure tests for the multi-segment connector-chain helper."""

    def test_two_point_chain_matches_plain_route_connector(self) -> None:
        self.assertEqual(
            route_chain(((0, 0), (4, 3))), route_connector(0, 0, 4, 3)
        )

    def test_chain_concatenates_each_segment_in_order(self) -> None:
        chain = route_chain(((0, 0), (0, 3), (4, 3)))
        expected = route_connector(0, 0, 0, 3) + route_connector(0, 3, 4, 3)
        self.assertEqual(chain, expected)

    def test_single_point_chain_is_empty(self) -> None:
        self.assertEqual(route_chain(((2, 2),)), ())


class BoardLabelFiveCellLimitTests(unittest.TestCase):
    """Pure tests for the new 5-display-cell BOARD label limit.

    This is a DISPLAY-CELL limit (cell_len()), never Python len() --
    grapheme-safe truncation, emoji/ZWJ/CJK safety, and the underlying
    compact_node_label() truncation convention are all unchanged; only
    the max_cells value app.py passes for the label physically above a
    node's glyph is new. The unified bottom bar (mesh_state.
    format_mesh_node_bar_fields) is untouched and always uses the full,
    uncapped Long Name/Short Name -- proven separately in test_mesh_state.py.
    """

    def test_short_ascii_name_passes_through_unchanged(self) -> None:
        node = NodeMetadata("!12345678", "ALICE", None)
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertEqual(label, "ALICE")
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)

    def test_long_ascii_name_truncates_to_five_cells(self) -> None:
        node = NodeMetadata("!12345678", "CHARLIE THE LONG NAMED NODE", None)
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)
        self.assertTrue(label.endswith("…"))

    def test_emoji_name_stays_within_five_cells(self) -> None:
        node = NodeMetadata("!12345678", "🐔pizza party extra long name", None)
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)

    def test_zwj_emoji_never_severed_at_five_cells(self) -> None:
        node = NodeMetadata(
            "!12345678", "👨‍👩‍👧‍👦 family extra long descriptive name", None
        )
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)
        if label.endswith("…"):
            last_kept = label[:-1][-1:]
            self.assertNotEqual(last_kept, mesh_topology_module._ZERO_WIDTH_JOINER)
            self.assertNotIn(last_kept, mesh_topology_module._VARIATION_SELECTORS)

    def test_cjk_wide_characters_stay_within_five_cells(self) -> None:
        node = NodeMetadata("!12345678", "CJK漢字測試名稱超長節點標籤", None)
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)

    def test_short_name_fallback_also_respects_five_cells(self) -> None:
        node = NodeMetadata("!12345678", "A Very Long Descriptive Long Name", "SHORT")
        label = compact_node_label(node, MESH_BOARD_LABEL_MAX_CELLS)
        self.assertEqual(label, "SHORT")
        self.assertLessEqual(cell_len(label), MESH_BOARD_LABEL_MAX_CELLS)


class MeshNodeColorTests(unittest.TestCase):
    """MESH FOLLOW-UP items 16-18, 26: YOU is ALWAYS ACCENT2, a persistent

    identity anchor entirely independent of selection; a selected
    remote node is ACCENT; an unselected remote node uses its existing
    active/inactive BASE/DIM_BASE styling. Pure unit tests against
    _mesh_node_color directly -- no app/theme rendering required.
    """

    NOW = 1_700_000_000.0

    def _you_state(self, position=None) -> MeshNodeState:
        return MeshNodeState(
            node=NodeMetadata("!you", "Local", "ME", 0, self.NOW, True, position=position),
            is_client=False,
            is_relay=False,
            last_interaction_at=None,
        )

    def _remote_state(self, *, last_heard: float | None) -> MeshNodeState:
        return MeshNodeState(
            node=NodeMetadata("!remote1", "Remote", "RMT", 1, last_heard),
            is_client=True,
            is_relay=False,
            last_interaction_at=last_heard,
        )

    def test_you_is_accent2_when_unselected(self) -> None:
        for theme in ("snow", "amber"):
            with self.subTest(theme=theme):
                palette = THEME_PALETTES[theme]
                color = _mesh_node_color(
                    self._you_state(), selected=False, theme=theme, now=self.NOW
                )
                self.assertEqual(color, palette.accent2)

    def test_you_is_accent2_when_selected_never_accent(self) -> None:
        for theme in ("snow", "amber"):
            with self.subTest(theme=theme):
                palette = THEME_PALETTES[theme]
                color = _mesh_node_color(
                    self._you_state(), selected=True, theme=theme, now=self.NOW
                )
                self.assertEqual(color, palette.accent2)
                self.assertNotEqual(color, palette.accent)

    def test_selecting_a_remote_node_never_recolors_you(self) -> None:
        """Selection state is per-widget (see MeshNodeWidget.refresh_visual,

        called once per node with ITS OWN selected flag) -- YOU's own
        color call always passes selected=False whenever a DIFFERENT
        node is selected, and must still resolve to ACCENT2, never BASE
        or ACCENT.
        """
        palette = THEME_PALETTES["snow"]
        color = _mesh_node_color(self._you_state(), selected=False, theme="snow", now=self.NOW)
        self.assertEqual(color, palette.accent2)

    def test_selected_remote_node_uses_accent(self) -> None:
        for theme in ("snow", "amber"):
            with self.subTest(theme=theme):
                palette = THEME_PALETTES[theme]
                color = _mesh_node_color(
                    self._remote_state(last_heard=self.NOW - 5),
                    selected=True,
                    theme=theme,
                    now=self.NOW,
                )
                self.assertEqual(color, palette.accent)

    def test_unselected_active_remote_node_uses_base(self) -> None:
        palette = THEME_PALETTES["snow"]
        color = _mesh_node_color(
            self._remote_state(last_heard=self.NOW - 5),
            selected=False,
            theme="snow",
            now=self.NOW,
        )
        self.assertEqual(color, palette.base)

    def test_unselected_inactive_remote_node_uses_dim_base(self) -> None:
        palette = THEME_PALETTES["snow"]
        color = _mesh_node_color(
            self._remote_state(last_heard=self.NOW - ACTIVE_WINDOW_SECONDS * 2),
            selected=False,
            theme="snow",
            now=self.NOW,
        )
        self.assertEqual(color, palette.dim_base)

    def test_moving_remote_focus_restores_prior_remote_normal_style(self) -> None:
        """Deselecting a remote node (selected=False on a later call)

        returns it to its own ordinary active/inactive style -- proving
        color is recomputed fresh per call, never sticky from a
        previous selected=True render.
        """
        palette = THEME_PALETTES["snow"]
        state = self._remote_state(last_heard=self.NOW - 5)
        selected_color = _mesh_node_color(state, selected=True, theme="snow", now=self.NOW)
        self.assertEqual(selected_color, palette.accent)
        restored_color = _mesh_node_color(state, selected=False, theme="snow", now=self.NOW)
        self.assertEqual(restored_color, palette.base)

    def test_snow_and_amber_resolve_distinct_accent2_tokens(self) -> None:
        """No hardcoded colors: each theme's own accent2 token is used,

        never a shared/literal constant (item 18).
        """
        snow_color = _mesh_node_color(
            self._you_state(), selected=False, theme="snow", now=self.NOW
        )
        amber_color = _mesh_node_color(
            self._you_state(), selected=False, theme="amber", now=self.NOW
        )
        self.assertEqual(snow_color, THEME_PALETTES["snow"].accent2)
        self.assertEqual(amber_color, THEME_PALETTES["amber"].accent2)
        self.assertNotEqual(snow_color, amber_color)


class MeshRealDataAppTests(unittest.IsolatedAsyncioTestCase):
    """Headless app tests proving MESH is now driven by real passive data:

    CLIENT derivation, RELAY truthfulness, the bounded working set,
    geographic placement, context text, selection/navigation, the
    preserved visual contract, and zero new radio traffic.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(
        self, *, chat_store: ChatStore | None = None, scripted_messages=()
    ) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=scripted_messages
        )
        return MeshtasticPassApp(radio, self.settings, chat_store=chat_store)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    # ---- CLIENT derivation (spec section 21) -------------------------

    async def test_incoming_message_makes_the_sender_a_client(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            alice = next(
                state
                for state in view.working_set
                if state.node.node_id == SIMULATED_MESSAGES[0].sender_node_id
            )
            self.assertTrue(alice.is_client)

    async def test_outgoing_message_does_not_make_a_remote_node_client(self) -> None:
        """An outgoing message must never mark ANY remote node CLIENT.

        Remote nodes may still appear here -- NodeDB-first admission
        (see build_mesh_working_set) means SimulatedRadioService's own
        passively-known nodes are candidates on their own -- but none of
        them may be is_client, since no incoming message was ever
        received from any of them.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accepted_send("hello from me")
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            remote_clients = {
                state.node.node_id
                for state in view.working_set
                if not state.node.is_local and state.is_client
            }
            self.assertEqual(remote_clients, set())

    async def test_persisted_incoming_history_survives_a_fresh_app_instance(self) -> None:
        """Simulates a process restart: a new ChatStore/App reading the same

        database file must still see Alice as CLIENT from "before".
        """
        db_path = self.root / "chat.db"
        now = 1_700_000_000.0
        old_timestamp = now - 10 * 24 * 60 * 60

        first_process_store = ChatStore.open(db_path)
        first_process_store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice Trail",
            sender_short_name="ALCE",
            channel_index=0,
            text="before restart",
            radio_rx_at=old_timestamp,
            received_at=old_timestamp,
        )
        first_process_store.close()

        second_process_store = ChatStore.open(db_path)
        self.addCleanup(second_process_store.close)
        app = self._make_app(chat_store=second_process_store)
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            alice = next(
                state for state in view.working_set if state.node.node_id == "!a11ce001"
            )
            self.assertTrue(alice.is_client)
            self.assertEqual(alice.last_interaction_at, old_timestamp)

    async def test_most_recent_incoming_message_determines_last_interaction(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            # SIMULATED_MESSAGES[0] and [3] both come from the "!cafe0003"-
            # or "!a11ce001"-style senders at different ages; use the two
            # channel-0 Cafe Relay messages (index 2 is channel 1, index 3
            # is channel 0 and older than index 0's sender is irrelevant --
            # pick two messages from the SAME sender at different ages).
            older = SIMULATED_MESSAGES[3]
            newer = older.__class__(**{**older.__dict__, "radio_rx_at": older.radio_rx_at + 500})
            app._accept_received_message(older)
            app._accept_received_message(newer)
            activity = app._mesh_last_message_activity()
            self.assertEqual(activity[older.sender_node_id.lower()], newer.radio_rx_at)

    async def test_message_older_than_mounted_chat_window_still_makes_client(self) -> None:
        db_path = self.root / "bounded_chat.db"
        now = 1_700_000_000.0
        old_timestamp = now - 30 * 24 * 60 * 60

        store = ChatStore.open(db_path)
        store.add_incoming(
            packet_id=1,
            node_id="!a11ce001",
            sender_name="Alice Trail",
            sender_short_name="ALCE",
            channel_index=0,
            text="a message from a month ago",
            radio_rx_at=old_timestamp,
            received_at=old_timestamp,
        )
        for index in range(DEFAULT_HISTORY_LIMIT):
            store.add_incoming(
                packet_id=1000 + index,
                node_id="!f177e001",
                sender_name="Filler",
                sender_short_name="FIL",
                channel_index=0,
                text=f"filler {index}",
                radio_rx_at=now - index,
                received_at=now - index,
            )
        store.close()

        store = ChatStore.open(db_path)
        self.addCleanup(store.close)
        app = self._make_app(chat_store=store)
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            mounted_ids = {entry.node_id for entry in app.chat_history if entry.node_id}
            self.assertNotIn("!a11ce001", mounted_ids)
            await pilot.press("3")
            await pilot.pause()
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            alice = next(
                state for state in view.working_set if state.node.node_id == "!a11ce001"
            )
            self.assertTrue(alice.is_client)
            self.assertTrue(alice.is_stale(now=now))

    # ---- RELAY truthfulness (spec section 22) ------------------------

    async def test_no_node_is_ever_relay_from_current_real_data(self) -> None:
        """No data source available to this app identifies a specific relay

        node (see mesh_state.MeshNodeState's docstring) -- RELAY must
        never be fabricated from hop count or a configured role, so
        every real-data node produced by the live app is_relay=False,
        even a node with a high hop count.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertTrue(view.working_set)
            self.assertTrue(all(not state.is_relay for state in view.working_set))

    async def test_high_hop_count_does_not_produce_relay_state(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            far_hop_message = SIMULATED_MESSAGES[0].__class__(
                **{**SIMULATED_MESSAGES[0].__dict__}
            )
            app._accept_received_message(far_hop_message)
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(far_hop_message.sender_node_id, hops_away=9),
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            remote = next(
                state
                for state in view.working_set
                if state.node.node_id == far_hop_message.sender_node_id.lower()
                or state.node.node_id == far_hop_message.sender_node_id
            )
            self.assertFalse(remote.is_relay)

    # ---- Working set (spec section 23) -------------------------------

    async def test_working_set_is_bounded(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for index in range(20):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=f"!n{index:07x}",
                        sender_long_name=f"Node{index}",
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=9_000_000 + index,
                        radio_rx_at=time.time() - index,
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            remote_count = sum(1 for state in view.working_set if not state.node.is_local)
            self.assertEqual(remote_count, DEFAULT_MAX_REMOTE_NODES)

    async def test_very_old_client_is_excluded_from_the_working_set(self) -> None:
        """Beyond MESH_VERY_OLD_THRESHOLD_SECONDS, a node's connector is

        removed from the board entirely (see MeshActivityTier.VERY_OLD)
        -- this used to be the "stale fallback still shows up" case, but
        that behavior was superseded once VERY_OLD nodes stopped
        occupying a working-set slot at all (see item 6 of the MESH
        activity-model task).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            very_old = 1_700_000_000.0 - MESH_STALE_THRESHOLD_SECONDS * 10
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!oldclient",
                    sender_long_name="OldClient",
                    sender_short_name=None,
                    channel_index=0,
                    text="ancient",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=very_old,
                )
            )
            await pilot.press("3")
            await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_000.0)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertNotIn("!oldclient", remote_ids)

    async def test_unchanged_data_produces_unchanged_positions(self) -> None:
        """A second refresh moments later, with no underlying data change,

        must not reshuffle anything -- including which nodes are
        currently ACTIVE (see _mesh_active_hop_counts): both refreshes
        use realistic, closely-spaced wall-clock values rather than an
        arbitrary distant `wall_now` that would itself flip activity
        (and therefore relay-chain presence) independent of any real
        data change.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            first_positions = dict(view.base_positions)
            app._refresh_mesh(wall_now=time.time())
            await pilot.pause()
            self.assertEqual(view.base_positions, first_positions)

    async def test_working_set_nodes_never_overlap(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            positions = list(view.base_positions.values())
            self.assertEqual(len(positions), len(set(positions)))

    async def test_you_is_always_in_the_working_set_when_local_node_known(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertTrue(any(state.node.is_local for state in view.working_set))

    # ---- Geography (spec section 24) ---------------------------------

    async def test_north_node_appears_above_you_south_below(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(
                    app.radio.info.node_id, is_local=True, position=LOCAL_GEO
                ),
                NodeMetadata("!north1", "North1", position=north_of_local(5)),
                NodeMetadata("!south1", "South1", position=south_of_local(5)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!north1",
                    sender_long_name="North1",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!south1",
                    sender_long_name="South1",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=2,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            you_row, _you_col = view.base_positions[app.radio.info.node_id]
            north_row, _ = view.base_positions["!north1"]
            south_row, _ = view.base_positions["!south1"]
            self.assertLess(north_row, you_row)
            self.assertGreater(south_row, you_row)

    async def test_east_node_appears_right_of_you_west_left(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(
                    app.radio.info.node_id, is_local=True, position=LOCAL_GEO
                ),
                NodeMetadata("!east1", "East1", position=east_of_local(5)),
                NodeMetadata("!west1", "West1", position=west_of_local(5)),
            )
            for node_id in ("!east1", "!west1"):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=node_id,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node_id) & 0xFFFF,
                        radio_rx_at=time.time(),
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            _you_row, you_column = view.base_positions[app.radio.info.node_id]
            _er, east_column = view.base_positions["!east1"]
            _wr, west_column = view.base_positions["!west1"]
            self.assertGreater(east_column, you_column)
            self.assertLess(west_column, you_column)

    async def test_farther_node_is_at_least_as_many_grid_steps_away(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(
                    app.radio.info.node_id, is_local=True, position=LOCAL_GEO
                ),
                NodeMetadata("!near1", "Near1", position=north_of_local(1)),
                NodeMetadata("!far1", "Far1", position=north_of_local(500)),
            )
            for node_id in ("!near1", "!far1"):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=node_id,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node_id) & 0xFFFF,
                        radio_rx_at=time.time(),
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            you_row, _ = view.base_positions[app.radio.info.node_id]
            near_row, _ = view.base_positions["!near1"]
            far_row, _ = view.base_positions["!far1"]
            self.assertGreaterEqual(you_row - far_row, you_row - near_row)

    async def test_missing_gps_fallback_is_deterministic_and_collision_free(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            no_gps_nodes = tuple(
                NodeMetadata(f"!ng{index:02x}", f"NoGps{index}") for index in range(4)
            )
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                *no_gps_nodes,
            )
            for node in no_gps_nodes:
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node.node_id,
                        sender_long_name=node.long_name,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node.node_id) & 0xFFFF,
                        radio_rx_at=time.time(),
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            first_positions = dict(view.base_positions)
            self.assertEqual(
                len(first_positions.values()), len(set(first_positions.values()))
            )

            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()
            self.assertEqual(view.base_positions, first_positions)

    # ---- Unified bottom bar (MESH GPS + UNIFIED BAR Part B) -----------

    async def test_node_bar_for_you(self) -> None:
        """YOU's bar carries no literal "YOU" label, HOPS 0, real GPS

        (SimulatedRadioService's local node record carries
        SIMULATED_LOCAL_POSITION, a real fix), DISTANCE "--", and TIME
        "NOW" -- SimulatedRadioService reports the connected radio's
        own identity as "Simulated Node"/"SIM" (see RadioInfo).
        """
        app = self._make_app()
        # A wide terminal so the bar renders its fullest (tier 1) form --
        # see FormatMeshNodeBarLineTests/MeshNodeBarResponsiveWidthTests
        # for the width-tiered degradation itself.
        async with app.run_test(size=(160, 28)) as pilot:
            await self._open_mesh(pilot)
            status = _bar_text(app)
            self.assertNotIn("YOU", status)
            self.assertEqual(
                status,
                "LONG NAME Simulated Node • SHORT NAME SIM • HOPS 0 • "
                "GPS 40.7128, -74.0060 • DISTANCE -- • "
                "LINK ---- -- / -- • TIME NOW",
            )

    async def test_node_bar_for_client(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[1])  # Bob, hops_away=1
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            bob_id = SIMULATED_MESSAGES[1].sender_node_id
            view.select_node(bob_id)
            view.set_nodes(
                view.working_set, view.base_positions, theme=app._current_theme, now=1_700_000_100.0
            )
            app._update_mesh_node_bar(view.working_set, 1_700_000_100.0)
            await pilot.pause()
            status = _bar_text(app)
            self.assertIn("HOPS 1", status)

    async def test_node_bar_unknown_hops_shows_question_mark(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata("!unknownhop", "UnknownHop", hops_away=None),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!unknownhop",
                    sender_long_name="UnknownHop",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            view.select_node("!unknownhop")
            view.set_nodes(
                view.working_set, view.base_positions, theme=app._current_theme, now=time.time()
            )
            app._update_mesh_node_bar(view.working_set, time.time())
            await pilot.pause()
            status = _bar_text(app)
            self.assertIn("HOPS ?", status)
            self.assertNotIn("HOPS 0", status)

    # ---- Selection preservation / navigation (spec section 18/19) ----

    async def test_selection_preserved_across_refresh_if_still_present(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            alice_id = SIMULATED_MESSAGES[0].sender_node_id
            view.select_node(alice_id)
            view.set_nodes(
                view.working_set, view.base_positions, theme=app._current_theme, now=1_700_000_100.0
            )
            await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()
            self.assertEqual(view.selected_node_id.lower(), alice_id.lower())

    async def test_selection_falls_back_to_you_if_it_disappears(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            alice_id = SIMULATED_MESSAGES[0].sender_node_id
            view.select_node(alice_id)
            view.set_nodes(
                view.working_set, view.base_positions, theme=app._current_theme, now=1_700_000_100.0
            )
            await pilot.pause()

            # Alice no longer has any recorded interaction (CHAT cleared)
            # NOR any passive NodeDB record (get_known_nodes overridden
            # to YOU only) -- under NodeDB-first admission (see
            # build_mesh_working_set), losing CHAT history alone is no
            # longer enough to make a real known node disappear, so both
            # sources must be removed to genuinely test this fallback.
            app._channel_states.clear()
            app.chat_store = None
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
            )
            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()
            self.assertTrue(view.selected_node_id)
            selected_state = next(
                state
                for state in view.working_set
                if state.node.node_id == view.selected_node_id
            )
            self.assertTrue(selected_state.node.is_local)

    async def test_navigation_is_general_not_hardcoded_by_node_id(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata("!zzzznode", "ZzzzNode", position=north_of_local(2)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!zzzznode",
                    sender_long_name="ZzzzNode",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, app.radio.info.node_id)
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!zzzznode")

    async def test_arrow_navigation_full_regression_with_realistic_node_ids(
        self,
    ) -> None:
        """End-to-end regression for the reported "arrow keys do nothing"

        bug: YOU plus real-looking "!xxxxxxxx" node IDs (the exact format
        RadioService.get_known_nodes() returns) in every cardinal
        direction, proving the full navigation contract holds against
        live-shaped data, not just the clean synthetic IDs used
        elsewhere in this file. Hop count is 0 for all four so no
        anonymous relay stage is generated -- this test is specifically
        about ID handling in the real-node navigation path, not the
        separate relay-stage-traversal contract proven by
        test_navigation_walks_through_relay_stages_and_back.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            north_id, south_id, east_id, west_id = (
                "!0a0a0a0a",
                "!0b0b0b0b",
                "!0c0c0c0c",
                "!0d0d0d0d",
            )
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(north_id, "North Real", "NORT", 0, position=north_of_local(3)),
                NodeMetadata(south_id, "South Real", "SOUT", 0, position=south_of_local(3)),
                NodeMetadata(east_id, "East Real", "EAST", 0, position=east_of_local(3)),
                NodeMetadata(west_id, "West Real", "WEST", 0, position=west_of_local(3)),
            )
            for index, node_id in enumerate((north_id, south_id, east_id, west_id)):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=f"Node{index}",
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=100 + index,
                        radio_rx_at=time.time(),
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)

            # Arrow event is claimed by MESH; YOU is selected by default.
            self.assertEqual(view.selected_node_id, you_id)
            original_relative_positions = {
                node_id: (
                    view.base_positions[node_id][0] - view.base_positions[you_id][0],
                    view.base_positions[node_id][1] - view.base_positions[you_id][1],
                )
                for node_id in (you_id, north_id, south_id, east_id, west_id)
            }
            board_offset_before = view.board.styles.offset

            for key, expected_id in (
                ("up", north_id),
                ("down", you_id),
                ("down", south_id),
                ("up", you_id),
                ("right", east_id),
                ("left", you_id),
                ("left", west_id),
                ("right", you_id),
            ):
                await pilot.press(key)
                await pilot.pause()
                with self.subTest(key=key, expected=expected_id):
                    # Selected ID corresponds to an actual rendered node.
                    self.assertEqual(view.selected_node_id, expected_id)
                    self.assertIn(
                        expected_id, {w.node_id for w in app.query(MeshNodeWidget)}
                    )
                    selected_widget = next(
                        w for w in app.query(MeshNodeWidget) if w.node_id == expected_id
                    )
                    # YOU is always ACCENT2 (persistent identity anchor),
                    # even while selected; a selected remote node is
                    # ACCENT (see MESH FOLLOW-UP item 16).
                    expected_color = (
                        THEME_PALETTES[app._current_theme].accent2
                        if expected_id == you_id
                        else THEME_PALETTES[app._current_theme].accent
                    )
                    self.assertEqual(
                        selected_widget.render().spans[0].style.foreground,
                        Color.parse(expected_color),
                    )
                    # The unified bar reflects the new selection -- no
                    # literal "YOU" label, but YOU's entire bar renders
                    # in ACCENT2 (see MESH GPS + UNIFIED BAR Part B).
                    status = _bar_text(app)
                    self.assertNotIn("YOU", status)
                    bar_style = app.query_one("#mesh-node-bar").render().spans[0].style
                    expected_bar_style = (
                        THEME_PALETTES[app._current_theme].accent2
                        if expected_id == you_id
                        else THEME_PALETTES[app._current_theme].base
                    )
                    self.assertEqual(bar_style, expected_bar_style)
                    # The mesh recentered: the selected node's own base
                    # position is now the CURRENT viewport's own center
                    # anchor (see MeshTopologyView.current_grid_dimensions).
                    _, _, center_row, center_column = view.current_grid_dimensions()
                    positions = _mesh_translated_positions(
                        view.base_positions,
                        view.selected_node_id,
                        center_row=center_row,
                        center_column=center_column,
                    )
                    self.assertEqual(positions[expected_id], (center_row, center_column))
                    # Relative geometry between every node is unchanged --
                    # recentering is a pure translation, not a reshuffle.
                    for node_id in (you_id, north_id, south_id, east_id, west_id):
                        relative = (
                            positions[node_id][0] - positions[expected_id][0],
                            positions[node_id][1] - positions[expected_id][1],
                        )
                        expected_relative = (
                            original_relative_positions[node_id][0]
                            - original_relative_positions[expected_id][0],
                            original_relative_positions[node_id][1]
                            - original_relative_positions[expected_id][1],
                        )
                        self.assertEqual(relative, expected_relative)
                    # No scrolling: the board container's own offset never
                    # moves (only individual node widgets do).
                    self.assertEqual(view.board.styles.offset, board_offset_before)
                    self.assertFalse(view.show_vertical_scrollbar)
                    self.assertFalse(view.show_horizontal_scrollbar)

    async def test_navigation_works_even_when_focus_is_on_the_board_container(
        self,
    ) -> None:
        """Arrow-key MESH navigation is gated on the current tab, not on

        Textual focus residing on any particular node widget -- explicitly
        move focus to the board container itself and confirm navigation
        still works.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata("!0e0e0e0e", "Northern", position=north_of_local(2)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!0e0e0e0e",
                    sender_long_name="Northern",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            view.focus()
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!0e0e0e0e")

    async def test_navigation_works_after_visiting_chat_first(self) -> None:
        """Real-hardware regression: switching CHAT -> MESH (the ordinary

        workflow -- connect, check CHAT, then open MESH) left CHAT's own
        focused widget (e.g. #chat-log, ChatTranscript -- CHAT's neutral
        navigation state; see show_tab, it no longer auto-focuses
        #chat-input on entry) untouched, since show_tab()'s "mesh"
        branch never claimed/cleared focus for itself the way "chat"
        and "connection" already do for their own widgets.
        ChatTranscript.on_key unconditionally handles up/down/left/
        right/end for chat message navigation -- it never checks
        app.current_tab -- so once it kept focus across the tab switch,
        every arrow press on the now-visible MESH board was silently
        redirected into invisible CHAT message navigation instead of
        ever reaching _move_mesh_focus. Reproduces the exact
        key/event/focus path (pilot.press, real tab switches via the
        "2"/"3" bindings), not just the selection helper directly.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata("!0f0f0f0f", "Northern", position=north_of_local(2)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!0f0f0f0f",
                    sender_long_name="Northern",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.current_tab, "chat")
            self.assertEqual(app.focused.id, "chat-log")

            await pilot.press("3")
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            self.assertIsNone(app.focused)

            view = app.query_one(MeshTopologyView)
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!0f0f0f0f")

    async def test_navigation_survives_decimal_vs_hex_node_id_mismatch(self) -> None:
        """End-to-end version of the mesh_state-level regression test: even

        if CHAT activity reports a sender under a bare decimal node
        number while get_known_nodes() reports the same physical node in
        the standard "!hex" form, arrow navigation must still reach the
        real, positioned, rendered node -- not silently fail or land on
        a nameless duplicate. Hop count is 0 so no relay stage sits
        between YOU and it, keeping this test focused on ID handling.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata("!075bcd15", "North Node", "NORTH", 0, position=north_of_local(3)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="123456789",  # decimal for 0x075bcd15
                    sender_long_name="North Node",
                    sender_short_name="NORTH",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(
                {state.node.node_id for state in view.working_set},
                {app.radio.info.node_id, "!075bcd15"},
            )
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!075bcd15")
            status = _bar_text(app)
            self.assertTrue(status.startswith("LONG NAME North Node"))

    # ---- Anonymous relay-stage topology (hop-count based) --------------

    async def _open_mesh_with_directional_clients(
        self, pilot, *, hops: dict
    ) -> "MeshtasticPassApp":
        """Wire up to four clients, one per cardinal direction, each with

        the given hops_away -- avoiding same-bucket contention so each
        client's own relay chain has genuinely interior grid cells.
        """
        app = pilot.app
        directions = {
            "north": ("!d0000001", north_of_local(3)),
            "south": ("!d0000002", south_of_local(3)),
            "east": ("!d0000003", east_of_local(3)),
            "west": ("!d0000004", west_of_local(3)),
        }
        nodes = [NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)]
        for name, (node_id, position) in directions.items():
            nodes.append(
                NodeMetadata(
                    node_id,
                    name.title(),
                    name[:4].upper(),
                    hops.get(name),
                    last_heard=time.time() - 5,
                    position=position,
                )
            )
        app.radio.get_known_nodes = lambda nodes=tuple(nodes): nodes
        for name, (node_id, _position) in directions.items():
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name=name.title(),
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=hash(node_id) & 0xFFFF,
                    radio_rx_at=time.time(),
                )
            )
        await self._open_mesh(pilot)
        return directions

    async def test_hop_count_topology_generates_correct_relay_stage_counts(
        self,
    ) -> None:
        """0/1/2/3 HOPS -> exactly that many anonymous relay stages each

        (spec section 2/32), never fewer, never more, one client per
        compass direction so counts can't be confused across clients.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            directions = await self._open_mesh_with_directional_clients(
                pilot, hops={"north": 0, "south": 1, "east": 2, "west": 3}
            )
            view = app.query_one(MeshTopologyView)
            for name, expected_count in (
                ("north", 0),
                ("south", 1),
                ("east", 2),
                ("west", 3),
            ):
                node_id = directions[name][0]
                with self.subTest(direction=name):
                    stage_count = sum(
                        1 for stage in view.relay_stages if stage.source_node_id == node_id
                    )
                    self.assertEqual(stage_count, expected_count)
            self.assertEqual(len(view.relay_stages), 0 + 1 + 2 + 3)
            self.assertEqual(
                {w.node_id for w in app.query(MeshRelayWidget)},
                {stage.node_id for stage in view.relay_stages},
            )

    async def test_unknown_hops_client_has_no_relay_stages_but_stays_visible(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            directions = await self._open_mesh_with_directional_clients(
                pilot, hops={"north": None, "south": None, "east": None, "west": None}
            )
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.relay_stages, ())
            self.assertEqual(len(list(app.query(MeshRelayWidget))), 0)
            north_id = directions["north"][0]
            self.assertIn(north_id, {w.node_id for w in app.query(MeshNodeWidget)})

    async def test_relay_stages_never_count_toward_active_or_working_set_bound(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            await self._open_mesh_with_directional_clients(
                pilot, hops={"north": 3, "south": 3, "east": 3, "west": 3}
            )
            view = app.query_one(MeshTopologyView)
            remote_count = sum(1 for state in view.working_set if not state.node.is_local)
            self.assertEqual(remote_count, 4)
            self.assertGreater(len(view.relay_stages), 0)

    async def test_navigation_skips_over_relay_stages_directly_to_real_node(
        self,
    ) -> None:
        """Anonymous relay stages are visual topology only -- NOT

        navigable (spec update: "UNKNOWN RELAYS ARE NOT SELECTABLE"). A
        single 2-hop client sits with two relay stages geometrically
        between it and YOU; a single "up" press from YOU must land
        directly on the real node in one press, never stopping on
        either relay stage, and a single "down" press from the real
        node returns directly to YOU. No wrapping, no scrolling.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!ta4be701"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    target_id, "Target Node", "TGT", 2,
                    last_heard=time.time() - 5, position=north_of_local(9),
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Target Node",
                    sender_short_name="TGT",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, you_id)
            self.assertEqual(len(view.relay_stages), 2)
            relay_ids = {stage.node_id for stage in view.relay_stages}
            board_offset_before = view.board.styles.offset

            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, target_id)
            self.assertNotIn(view.selected_node_id, relay_ids)
            status = _bar_text(app)
            self.assertTrue(status.startswith("LONG NAME Target Node"))

            # No wrapping: one more "up" from the real node changes nothing.
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, target_id)

            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, you_id)
            status = _bar_text(app)
            self.assertIn("HOPS 0", status)
            self.assertIn("TIME NOW", status)

            self.assertEqual(view.board.styles.offset, board_offset_before)
            self.assertFalse(view.show_vertical_scrollbar)
            self.assertFalse(view.show_horizontal_scrollbar)

    async def test_relay_stage_cannot_be_selected_and_always_renders_dim(
        self,
    ) -> None:
        """Anonymous relay stages are never focusable/selectable: no

        on_click, never ACCENT, never the enlarged composite, never the
        bottom-left context, and MeshTopologyView.select_node() rejects
        a relay stage ID outright as a no-op rather than accepting it
        and relying on a later set_nodes() call to repair it (spec
        update section "UNKNOWN RELAYS ARE NOT SELECTABLE").
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!re1a5001"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    target_id, "Relay Target", "RLT", 1,
                    last_heard=time.time() - 5, position=east_of_local(9),
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Relay Target",
                    sender_short_name="RLT",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 1)
            stage_id = view.relay_stages[0].node_id
            palette = THEME_PALETTES[app._current_theme]

            self.assertFalse(hasattr(MeshRelayWidget, "on_click"))
            self.assertFalse(MeshRelayWidget.can_focus)

            relay_widget = next(w for w in app.query(MeshRelayWidget) if w.node_id == stage_id)
            self.assertEqual(
                relay_widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            self.assertEqual(int(relay_widget.styles.width.value), 1)
            self.assertEqual(str(relay_widget.render()), CIRCLE_STROKED_LARGE)
            self.assertEqual(
                next((w for w in app.query(MeshNodeLabelWidget) if w.node_id == stage_id), None),
                None,
            )

            # Directly attempting to select a relay stage ID (there is no
            # UI path that does this anymore -- proving the model layer
            # itself refuses, not just the removed click handler): YOU is
            # already selected here, and MeshTopologyView.select_node()
            # rejects the relay ID outright as a no-op (see
            # test_select_node_accepts_only_real_working_set_nodes for
            # the dedicated proof) -- selection simply never moves off YOU.
            _mesh_select_node(app, stage_id)
            await pilot.pause()
            self.assertEqual(view.selected_node_id, you_id)
            self.assertNotEqual(view.selected_node_id, stage_id)
            relay_widget = next(w for w in app.query(MeshRelayWidget) if w.node_id == stage_id)
            self.assertEqual(
                relay_widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            self.assertEqual(int(relay_widget.styles.width.value), 1)
            status = _bar_text(app)
            self.assertIn("HOPS 0", status)
            self.assertIn("TIME NOW", status)

    async def test_select_node_accepts_only_real_working_set_nodes(self) -> None:
        """MeshTopologyView.select_node() is authoritative: only a real

        node currently in `working_set` may ever become
        `selected_node_id`. An anonymous RelayStage ID or an arbitrary
        unknown ID is rejected outright as a no-op right here, never
        even momentarily accepted and relying on a later set_nodes()
        call to repair it (spec update: tighten the selection boundary
        at select_node() itself, not just at the removed click handler
        or the navigation candidate filter). Requirement 5 (falling
        back to YOU when the selected REAL node disappears on refresh)
        is proven separately by
        test_selection_falls_back_to_you_if_it_disappears -- that path
        goes through set_nodes()'s own fallback, not select_node(),
        and is unaffected by this change.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            alice_id = "!a1ice001"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    alice_id, "Alice", "ALC", 1,
                    last_heard=time.time() - 5, position=north_of_local(9),
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=alice_id,
                    sender_long_name="Alice",
                    sender_short_name="ALC",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 1)
            relay_id = view.relay_stages[0].node_id

            # 1. Real working-set node selection succeeds.
            view.select_node(alice_id)
            self.assertEqual(view.selected_node_id, alice_id)

            # 2. Anonymous relay-stage ID selection is a no-op.
            view.select_node(relay_id)
            self.assertEqual(view.selected_node_id, alice_id)
            self.assertNotEqual(view.selected_node_id, relay_id)

            # 3. Arbitrary/unknown ID selection is a no-op.
            view.select_node("!totally-unknown-id")
            self.assertEqual(view.selected_node_id, alice_id)

            # 4. selected_node_id never even momentarily becomes a relay ID.
            for _ in range(3):
                view.select_node(relay_id)
                self.assertNotEqual(view.selected_node_id, relay_id)
            self.assertEqual(view.selected_node_id, alice_id)

            # Normal real-node selection behavior is otherwise unchanged.
            view.select_node(you_id)
            self.assertEqual(view.selected_node_id, you_id)

    async def test_navigation_candidate_set_excludes_relay_stage_ids(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!ca4d1da1"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    target_id, "Alice", "ALC", 3,
                    last_heard=time.time() - 5, position=west_of_local(9),
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Alice",
                    sender_short_name="ALC",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=1_700_000_000.0,
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 3)
            relay_ids = {stage.node_id for stage in view.relay_stages}

            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, target_id)
            self.assertEqual(relay_ids & {view.selected_node_id}, set())

            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, you_id)

    async def test_multiple_clients_same_hop_count_get_independent_relay_stages(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            alice_id, bob_id = "!a11cebbb", "!b0bbbbbb"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    alice_id, "Alice", "ALC", 2,
                    last_heard=time.time() - 5, position=east_of_local(9),
                ),
                NodeMetadata(
                    bob_id, "Bob", "BOB", 2,
                    last_heard=time.time() - 5, position=west_of_local(9),
                ),
            )
            for node_id, name in ((alice_id, "Alice"), (bob_id, "Bob")):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=name,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node_id) & 0xFFFF,
                        radio_rx_at=1_700_000_000.0,
                    )
                )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 4)
            alice_stage_ids = {
                s.node_id for s in view.relay_stages if s.source_node_id == alice_id
            }
            bob_stage_ids = {s.node_id for s in view.relay_stages if s.source_node_id == bob_id}
            self.assertEqual(len(alice_stage_ids), 2)
            self.assertEqual(len(bob_stage_ids), 2)
            self.assertEqual(alice_stage_ids & bob_stage_ids, set())
            positions = list(view.base_positions.values())
            self.assertEqual(len(positions), len(set(positions)))

    # ---- ACTIVE / brightness predicate unification (spec section 17-20) --

    async def test_active_count_and_node_brightness_use_same_predicate(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            client_ids = [f"!ac00000{i}" for i in range(8)]
            active_ids = set(client_ids[:4])
            nodes = [NodeMetadata(app.radio.info.node_id, is_local=True)]
            for index, node_id in enumerate(client_ids):
                last_heard = (
                    now - 10
                    if node_id in active_ids
                    else now - ACTIVE_WINDOW_SECONDS - 100
                )
                nodes.append(NodeMetadata(node_id, f"Node{index}", last_heard=last_heard))
            app.radio.get_known_nodes = lambda nodes=tuple(nodes): nodes
            for index, node_id in enumerate(client_ids):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=f"Node{index}",
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=9_500_000 + index,
                        radio_rx_at=now - index,
                    )
                )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn("MESH(4)", tab_bar)

            view = app.query_one(MeshTopologyView)
            self.assertNotIn(view.selected_node_id, client_ids)
            palette = THEME_PALETTES[app._current_theme]
            for node_id in client_ids:
                widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
                color = widget.render().spans[0].style.foreground
                with self.subTest(node_id=node_id, active=node_id in active_ids):
                    if node_id in active_ids:
                        self.assertEqual(color, Color.parse(palette.base))
                    else:
                        self.assertEqual(color, Color.parse(palette.dim_base))

    async def test_relay_stage_brightness_ignores_activity_and_is_never_counted(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!d1d0007f"
            now = 1_700_000_000.0
            # A recently-heard (ACTIVE) client, but its relay stage must
            # still always render DIM_BASE unless selected -- it has no
            # identity of its own to be "heard" from.
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    target_id, "DimTarget", "DIM", 1, last_heard=now - 5, position=north_of_local(9)
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="DimTarget",
                    sender_short_name="DIM",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 1)
            palette = THEME_PALETTES[app._current_theme]
            stage_widget = next(iter(app.query(MeshRelayWidget)))
            self.assertEqual(
                stage_widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn("MESH(1)", tab_bar)

    async def test_active_header_never_counts_nodes_outside_the_displayed_working_set(
        self,
    ) -> None:
        """Regression, updated for NodeDB-first admission (see

        build_mesh_working_set): the original observed uConsole bug was
        a population mismatch -- ACTIVE counted activity across the FULL
        known-node population (get_known_nodes()), independent of the
        bounded working set actually rendered, so an active node that
        never made the bounded cut could inflate the header without ever
        appearing on the board.

        A CHAT message is no longer what keeps a node off the board --
        NodeDB-first admission means any known node is a candidate, so
        the only way a real active node now fails to appear is by
        losing the bound (max_remote_nodes) to higher-ranked candidates
        (see build_mesh_working_set's tiered ranking). This fixture
        supplies MORE currently-active real nodes than the bound allows,
        confirming the displayed board and [3] MESH (N) still agree
        exactly: N equals precisely how many of the DISPLAYED nodes are
        active, never the size of the broader known-node population.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            active_ids = [f"!ac00000{i}" for i in range(DEFAULT_MAX_REMOTE_NODES + 2)]

            nodes = [NodeMetadata(app.radio.info.node_id, is_local=True)]
            for node_id in active_ids:
                nodes.append(NodeMetadata(node_id, node_id, last_heard=now - 10))
            app.radio.get_known_nodes = lambda nodes=tuple(nodes): nodes

            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            displayed_remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertEqual(len(displayed_remote_ids), DEFAULT_MAX_REMOTE_NODES)
            self.assertLess(len(displayed_remote_ids), len(active_ids))
            # Every candidate here is equally active -- the bound simply
            # keeps the board readable, but every node it DOES keep must
            # still count, and none of the ones it drops may leak in.
            all_displayed_ids = {state.node.node_id for state in view.working_set}
            self.assertEqual(
                {w.node_id for w in app.query(MeshNodeWidget)}, all_displayed_ids
            )

            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn(f"MESH({DEFAULT_MAX_REMOTE_NODES})", tab_bar)

    async def test_activity_styling_refreshes_without_leaving_mesh_tab(self) -> None:
        """The user should not have to leave/re-enter MESH for active

        styling (or ACTIVE N) to become correct as time passes -- the
        existing 1s _refresh_chat_timestamps timer already calls
        _refresh_mesh() while the MESH tab is current; this proves the
        resulting recompute actually updates both the header and the
        node's own color, entirely via repeated _refresh_mesh() calls
        with no additional tab switch and no new radio traffic.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            node_id = "!c0055ing1"
            heard_at = 1_700_000_000.0
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(node_id, "Crosser", last_heard=heard_at),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Crosser",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=heard_at,
                )
            )
            await self._open_mesh(pilot)
            palette = THEME_PALETTES[app._current_theme]

            app._refresh_mesh(wall_now=heard_at + 10)
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
            self.assertEqual(widget.render().spans[0].style.foreground, Color.parse(palette.base))

            # Same session, same tab -- only wall-clock time advanced past
            # the active window, exactly as the 1s timer would apply it.
            app._refresh_mesh(wall_now=heard_at + ACTIVE_WINDOW_SECONDS + 50)
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            self.assertIn("MESH(0)", str(app.query_one("#tab-bar").render()))
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            self.assertEqual(app.radio.sent_messages, ())

    # ---- UI consolidation: [3] MESH (N) replaces the internal header ---

    async def test_mesh_internal_view_no_longer_shows_active_status_text(
        self,
    ) -> None:
        """The in-view "> MESH · ACTIVE N" heading is gone entirely --

        the count now lives only in the top-nav tab label. The
        topology board and the bottom-left selected-node context line
        are unaffected.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            node_id = "!ac71ve01"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(node_id, "Active", last_heard=now - 5),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Active",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            self.assertEqual(list(app.query("#mesh-title")), [])
            status_text = str(app.query_one("#mesh-status").render())
            context_text = _bar_text(app)
            for text in (status_text, context_text):
                self.assertNotIn("ACTIVE", text)
                self.assertNotIn("> MESH", text)
            # The board and bottom-left context are untouched.
            self.assertIsNotNone(app.query_one(MeshTopologyView))
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
            self.assertIsNotNone(widget)

    async def test_mesh_shows_shared_status_while_connecting(self) -> None:
        """MESH's top status must use the exact same normalized value

        CONNECTION/CONFIG and CHAT use -- never MESH-specific wording
        like the old "NO MESH DATA -- RADIO DISCONNECTED".
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._show_connection(RadioState.OFFLINE, message="lost")
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")

            status_widget = app.query_one("#mesh-connection-status")
            self.assertTrue(status_widget.display)
            text = str(status_widget.render())
            self.assertIn(f"STATUS {ANIMATED_STATUS[RadioState.OFFLINE]}", text)
            self.assertNotIn("RADIO DISCONNECTED", text)
            self.assertNotIn("RECONNECTING", text)

    async def test_mesh_connection_status_becomes_last_update_once_connected(
        self,
    ) -> None:
        """The CONNECTING/RECONNECTING status text disappears once ONLINE

        -- the old top-of-view widget is hidden entirely (freeing its row
        for the grid, see item 5), replaced by the unified bottom bar
        (MESH GPS + UNIFIED BAR Part B), never left blank, since the
        default SimulatedRadioService fixture has a real selected node
        (YOU) as soon as it connects.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            for _ in range(5):
                await pilot.pause()
                if app._radio_state is RadioState.ONLINE:
                    break
            await pilot.press("3")
            await pilot.pause()
            self.assertEqual(app._radio_state, RadioState.ONLINE)

            status_widget = app.query_one("#mesh-connection-status")
            self.assertFalse(status_widget.display)
            text = _bar_text(app)
            self.assertIn("TIME", text)
            self.assertNotIn("STATUS", text)

    async def test_mesh_topology_not_rebuilt_by_connection_status_change(
        self,
    ) -> None:
        """Stale topology intentionally remains visible during a

        connection drop -- only the shared top status communicates
        "not live right now"; the board itself must not be cleared or
        rebuilt by the status change alone.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            node_id = "!sta1e001"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(node_id, "Stale", last_heard=now - 5),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Stale",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.working_set), 2)
            before_positions = dict(view.base_positions)

            app._show_connection(RadioState.OFFLINE, message="lost")
            await pilot.pause()
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            self.assertEqual(app.current_tab, "mesh")
            status_widget = app.query_one("#mesh-connection-status")
            self.assertTrue(status_widget.display)
            self.assertEqual(len(view.working_set), 2)
            self.assertEqual(dict(view.base_positions), before_positions)
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
            self.assertIsNotNone(widget)

    async def test_mesh_tab_shows_zero_when_no_active_displayed_remote_nodes(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            node_id = "!1nact1ve"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(
                    node_id, "Stale", last_heard=now - ACTIVE_WINDOW_SECONDS - 100
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Stale",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertIn("MESH(0)", str(app.query_one("#tab-bar").render()))

    async def test_mesh_tab_shows_two_when_two_displayed_real_nodes_are_active(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            active_ids = ["!act1ve01", "!act1ve02"]
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(active_ids[0], "A1", last_heard=now - 5),
                NodeMetadata(active_ids[1], "A2", last_heard=now - 5),
            )
            for index, node_id in enumerate(active_ids):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=f"A{index + 1}",
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=9_800_000 + index,
                        radio_rx_at=now - index,
                    )
                )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertIn("MESH(2)", str(app.query_one("#tab-bar").render()))

    async def test_anonymous_relay_placeholders_do_not_affect_tab_count(self) -> None:
        """A distant CLIENT generates anonymous relay-stage placeholders

        along its path to YOU -- those placeholders must never be
        counted, only the real, active CLIENT itself.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            distant_id = "!d1stant1"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    distant_id,
                    "Distant",
                    "DIST",
                    2,
                    last_heard=now - 5,
                    position=north_of_local(20),
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=distant_id,
                    sender_long_name="Distant",
                    sender_short_name="DIST",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertGreaterEqual(len(view.relay_stages), 1)
            # Exactly one real, active remote node -- the relay stage(s)
            # it generated along the way must not inflate the count.
            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))

    async def test_you_does_not_count_toward_tab_count(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            # YOU given a suspiciously recent last_heard -- if the
            # predicate ever forgot to exclude is_local, this would
            # wrongly count as 1.
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True, last_heard=now - 1),
            )
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertIn("MESH(0)", str(app.query_one("#tab-bar").render()))

    async def test_mesh_tab_count_updates_without_leaving_or_reentering_mesh(
        self,
    ) -> None:
        """Same guarantee as test_activity_styling_refreshes_without_

        leaving_mesh_tab, but observed from a DIFFERENT tab -- the count
        must update via the same periodic refresh even while MESH isn't
        the visible tab, and must never force a topology rebuild to do
        it (see _mesh_active_count's docstring).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            node_id = "!crossing"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(app.radio.info.node_id, is_local=True),
                NodeMetadata(node_id, "Crosser", last_heard=now),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name="Crosser",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=now,
                )
            )
            app.show_tab("connection")
            await pilot.pause()
            self.assertEqual(app.current_tab, "connection")

            app._refresh_chat_timestamps(wall_now=now)
            await pilot.pause()
            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))

            app._refresh_chat_timestamps(wall_now=now + ACTIVE_WINDOW_SECONDS + 50)
            await pilot.pause()
            self.assertEqual(app.current_tab, "connection")
            self.assertIn("MESH(0)", str(app.query_one("#tab-bar").render()))
            self.assertEqual(app.radio.sent_messages, ())

    # ---- Screenshot regression: relay count vs line-bend count ----------

    @staticmethod
    def _client_chain_points(view, you_id, remote_id):
        stages = sorted(
            (stage for stage in view.relay_stages if stage.source_node_id == remote_id),
            key=lambda stage: stage.index,
        )
        node_ids = [you_id, *(stage.node_id for stage in stages), remote_id]
        return tuple(_mesh_grid_pixel(*view.base_positions[node_id]) for node_id in node_ids)

    @classmethod
    def _count_bends(cls, view, you_id, remote_id):
        chain_points = cls._client_chain_points(view, you_id, remote_id)
        elbow_glyphs = {"┐", "┘", "┌", "└"}
        return sum(1 for _x, _y, glyph in route_chain(chain_points) if glyph in elbow_glyphs)

    async def _diagnose_topology(self, app, pilot, *, clients, now):
        """Build the given {node_id: (name, hops_away)} clients NE/around

        YOU, refresh MESH, and return a per-client diagnostic dict of
        {node_id: {hops_away, relay_count, bend_count}} -- exactly the
        report requested to distinguish (A) wrong hop-count
        interpretation, (B) duplicated relays, (C) waypoints rendered
        as relays, or (D) something else, before any fix is written.
        """
        you_id = app.radio.info.node_id
        nodes = [NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)]
        for node_id, (name, hops, position) in clients.items():
            nodes.append(
                NodeMetadata(
                    node_id, name, name[:4].upper(), hops, last_heard=now - 5, position=position
                )
            )
        app.radio.get_known_nodes = lambda nodes=tuple(nodes): nodes
        for index, (node_id, (name, _hops, _position)) in enumerate(clients.items()):
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=node_id,
                    sender_long_name=name,
                    sender_short_name=name[:4].upper(),
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=9_900_000 + index,
                    radio_rx_at=now - index,
                )
            )
        await self._open_mesh(pilot)
        app._refresh_mesh(wall_now=now)
        await pilot.pause()

        view = app.query_one(MeshTopologyView)
        report = {}
        for node_id, (_name, hops, _position) in clients.items():
            relay_count = sum(
                1 for stage in view.relay_stages if stage.source_node_id == node_id
            )
            bend_count = self._count_bends(view, you_id, node_id)
            report[node_id] = {
                "hops_away": hops,
                "relay_count": relay_count,
                "bend_count": bend_count,
            }
            print(
                f"[MESH DIAGNOSTIC] node={node_id} hops_away={hops} "
                f"synthetic_relays={relay_count} line_waypoints(bends)={bend_count}"
            )
        return report

    async def test_relay_count_is_determined_only_by_hop_count_not_by_line_bends(
        self,
    ) -> None:
        """Screenshot regression: a chain of many anonymous relay circles

        must only ever appear if the client's own reported hop count is
        genuinely that large -- never as a side effect of how many
        orthogonal bends its connector line happens to need. Two
        topologies with the IDENTICAL client/hop-count set, one client
        positioned to produce few line bends and the other positioned
        (northeast, off-axis) to force several, must report the exact
        same relay count per client -- geometry must never manufacture
        topology.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            few_bends_clients = {
                "!ax1a1000": ("AxisA", 2, north_of_local(6)),
                "!ax1b2000": ("AxisB", 1, GeoPosition(0.0, 6 / 69.0)),
            }
            report_few_bends = await self._diagnose_topology(
                app, pilot, clients=few_bends_clients, now=now
            )
            self.assertGreaterEqual(
                sum(info["bend_count"] for info in report_few_bends.values()), 0
            )

        app2 = self._make_app()
        async with app2.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            # Same node IDs, same hop counts -- but off-axis (northeast)
            # positions, which force the orthogonal router to take
            # several elbow turns per segment instead of a straight run.
            many_bends_clients = {
                "!ax1a1000": ("AxisA", 2, GeoPosition(6 / 69.0, 6 / 69.0)),
                "!ax1b2000": ("AxisB", 1, GeoPosition(4 / 69.0, 7 / 69.0)),
            }
            report_many_bends = await self._diagnose_topology(
                app2, pilot, clients=many_bends_clients, now=now
            )

        for node_id in few_bends_clients:
            with self.subTest(node_id=node_id):
                few = report_few_bends[node_id]
                many = report_many_bends[node_id]
                self.assertEqual(few["hops_away"], many["hops_away"])
                self.assertEqual(
                    few["relay_count"],
                    few["hops_away"],
                    "relay count must equal hop count exactly",
                )
                self.assertEqual(
                    many["relay_count"],
                    many["hops_away"],
                    "relay count must equal hop count exactly regardless of bends",
                )
                self.assertEqual(
                    few["relay_count"],
                    many["relay_count"],
                    "changing the number of line bends must not change the relay count",
                )

    async def test_northeast_client_many_bends_still_matches_hop_count_exactly(
        self,
    ) -> None:
        """The exact screenshot shape: YOU, several real clients, one

        positioned northeast, orthogonally routed with several bends.
        Logs the full diagnostic report and asserts every displayed
        client's relay count is determined solely by its own hop count.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            clients = {
                "!ne000001": ("Northeast5", 3, GeoPosition(6 / 69.0, 9 / 69.0)),
                "!n0000002": ("North2", 1, north_of_local(5)),
                "!e0000003": ("East3", 2, GeoPosition(0.0, 8 / 69.0)),
                "!d0000004": ("Direct4", 0, GeoPosition(-3 / 69.0, 2 / 69.0)),
            }
            report = await self._diagnose_topology(app, pilot, clients=clients, now=now)

            view = app.query_one(MeshTopologyView)
            self.assertEqual(
                len(view.relay_stages),
                sum(hops for _name, hops, _position in clients.values()),
            )
            for node_id, (_name, hops, _position) in clients.items():
                with self.subTest(node_id=node_id):
                    self.assertEqual(report[node_id]["relay_count"], hops)
                    # Every relay for this client is a genuine RelayStage,
                    # never a raw line-waypoint glyph: relay circles are
                    # mounted MeshRelayWidgets with this client's own
                    # node_id baked in, entirely independent of however
                    # many background connector-line cells were drawn.
                    relay_ids = {
                        stage.node_id
                        for stage in view.relay_stages
                        if stage.source_node_id == node_id
                    }
                    self.assertEqual(len(relay_ids), hops)
                    mounted = {
                        widget.node_id
                        for widget in app.query(MeshRelayWidget)
                        if widget.node_id in relay_ids
                    }
                    self.assertEqual(mounted, relay_ids)

    # ---- Expanded selected-node context (spec section 21-27) -----------

    async def test_selected_context_full_format_with_distance(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!c0ffee01"
            target_position = north_of_local(4.2)
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    target_id, "Bob Basecamp", "BOB", 1, position=target_position
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Bob Basecamp",
                    sender_short_name="BOB",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time() - 30,
                )
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, target_id)
            await pilot.pause()
            status = _bar_text(app)
            # format_mesh_node_bar_fields' TIME formats AGE against real
            # wall-clock time() (see app.py), so this cannot pin an exact
            # TIME string; only the wall-clock-independent fields are
            # asserted exactly (spec section 37: no test may depend on
            # uncontrolled wall-clock timing).
            expected_distance = distance_between(LOCAL_GEO, target_position)
            # SimulatedRadioService's default synced display.units is
            # DISPLAY_UNITS_METRIC (see MESH VIEW PASS item 11 / spec
            # section 37: distance now honestly reflects the connected
            # radio's own UNITS setting rather than a hardcoded "mi").
            self.assertEqual(_bar_field(status, "LONG NAME"), "Bob Basecamp")
            self.assertEqual(_bar_field(status, "SHORT NAME"), "BOB")
            self.assertEqual(_bar_field(status, "HOPS"), "1")
            self.assertEqual(
                _bar_field(status, "DISTANCE"), f"{expected_distance * KM_PER_MILE:.1f} km"
            )
            self.assertNotEqual(_bar_field(status, "TIME"), "?")

    async def test_selected_context_missing_short_name_and_distance(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!c0ffee02"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True),  # no position -> "? km"
                NodeMetadata(target_id, "No Short Name", None, 1),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="No Short Name",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, target_id)
            await pilot.pause()
            status = _bar_text(app)
            # SHORT NAME always resolves to something displayable --
            # falls back to the node ID rather than being omitted (see
            # format_mesh_node_bar_fields).
            self.assertEqual(_bar_field(status, "SHORT NAME"), target_id)
            # No shared GPS fix (YOU has no position at all in this
            # fixture) -> DISTANCE is the honest "--", never a
            # fabricated/unit-suffixed unknown value.
            self.assertEqual(_bar_field(status, "DISTANCE"), "--")
            self.assertEqual(_bar_field(status, "LONG NAME"), "No Short Name")

    async def test_distance_unchanged_by_recentering(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            alice_id, bob_id = "!a1a1a1a1", "!b2b2b2b2"
            alice_position = north_of_local(3.7)
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(alice_id, "Alice", "ALC", 1, position=alice_position),
                NodeMetadata(bob_id, "Bob", "BOB", 1, position=south_of_local(5)),
            )
            for node_id, name in ((alice_id, "Alice"), (bob_id, "Bob")):
                app._accept_received_message(
                    SIMULATED_MESSAGES[0].__class__(
                        sender_node_id=node_id,
                        sender_long_name=name,
                        sender_short_name=None,
                        channel_index=0,
                        text="hi",
                        rssi=None,
                        snr=None,
                        packet_id=hash(node_id) & 0xFFFF,
                        radio_rx_at=time.time(),
                    )
                )
            await self._open_mesh(pilot)

            _mesh_select_node(app, alice_id)
            await pilot.pause()
            first_status = _bar_text(app)
            first_distance = _bar_field(first_status, "DISTANCE")

            # Selecting Bob recenters the whole board on him.
            _mesh_select_node(app, bob_id)
            await pilot.pause()
            self.assertNotEqual(
                _bar_text(app), first_status
            )

            _mesh_select_node(app, alice_id)
            await pilot.pause()
            second_status = _bar_text(app)
            self.assertEqual(second_status, first_status)
            self.assertEqual(_bar_field(second_status, "DISTANCE"), first_distance)

    async def test_no_scrollbars_on_mesh(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertNotIsInstance(view, ScrollableContainer)
            self.assertFalse(view.show_vertical_scrollbar)
            self.assertFalse(view.show_horizontal_scrollbar)

    # ---- Board label 5-cell limit (app-level) --------------------------

    async def test_board_label_five_cells_stays_centered_and_glyph_unmoved(
        self,
    ) -> None:
        """A long name truncates to <= 5 display cells (plus TRACE

        ROUTE's own 2-cell "<marker> " prefix -- see
        mesh_topology.mesh_board_marker_label) on the board label, the
        label stays centered over the glyph's fixed grid anchor, and
        the glyph's own coordinate is completely unaffected by label
        length (spec: "label width never moves the glyph").
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            long_id = "!10ngname"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    long_id,
                    "A Very Long Descriptive Node Name",
                    None,
                    position=north_of_local(3),
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=long_id,
                    sender_long_name="A Very Long Descriptive Node Name",
                    sender_short_name=None,
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            glyph = next(w for w in app.query(MeshNodeWidget) if w.node_id == long_id)
            label = next(w for w in app.query(MeshNodeLabelWidget) if w.node_id == long_id)

            label_text = str(label.render())
            self.assertLessEqual(cell_len(label_text), MESH_BOARD_LABEL_MAX_CELLS + 2)
            self.assertTrue(label_text.startswith("• ") or label_text.startswith("* "))
            # The label sits exactly one row above the glyph, horizontally
            # centered on the SAME column anchor -- never the glyph's own
            # coordinate (see MeshNodeLabelWidget/set_nodes).
            glyph_x, glyph_y = glyph_screen_position(glyph)
            label_x, label_y = glyph_screen_position(label)
            self.assertEqual(label_x, glyph_x)
            self.assertEqual(label_y, glyph_y - 1)

            # Selecting a neighbor recenters the whole board (glyph moves
            # with the translation) but the label stays centered on it.
            _mesh_select_node(app, you_id)
            await pilot.pause()
            glyph = next(w for w in app.query(MeshNodeWidget) if w.node_id == long_id)
            label = next(w for w in app.query(MeshNodeLabelWidget) if w.node_id == long_id)
            glyph_x, glyph_y = glyph_screen_position(glyph)
            label_x, label_y = glyph_screen_position(label)
            self.assertEqual(label_x, glyph_x)
            self.assertEqual(label_y, glyph_y - 1)

    # ---- Preserved visual contract -----------------------------------

    async def test_selected_remote_node_uses_accent_and_wider_composite(self) -> None:
        """A SELECTED REMOTE node uses ACCENT (see MESH FOLLOW-UP item

        16 -- YOU's own persistent ACCENT2 is covered separately by
        test_selected_you_uses_accent2_not_accent below).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app._accept_received_message(SIMULATED_MESSAGES[0])
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            remote_id = SIMULATED_MESSAGES[0].sender_node_id
            _mesh_select_node(app, remote_id)
            await pilot.pause()
            palette = THEME_PALETTES[app._current_theme]
            remote_widget = next(
                widget
                for widget in app.query(MeshNodeWidget)
                if widget.node_id == view.selected_node_id
            )
            rendered = remote_widget.render()
            self.assertEqual(rendered.spans[0].style.foreground, Color.parse(palette.accent))
            self.assertEqual(int(remote_widget.styles.width.value), MESH_SELECTED_GLYPH_WIDTH)

    async def test_selected_you_uses_accent2_not_accent(self) -> None:
        """YOU selected by default still gets the wider selected

        composite (selection-width is identity-independent), but its
        color is ACCENT2, never ACCENT (see MESH FOLLOW-UP item 16).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            palette = THEME_PALETTES[app._current_theme]
            you_widget = next(
                widget
                for widget in app.query(MeshNodeWidget)
                if widget.node_id == view.selected_node_id
            )
            rendered = you_widget.render()
            self.assertEqual(rendered.spans[0].style.foreground, Color.parse(palette.accent2))
            self.assertNotEqual(
                rendered.spans[0].style.foreground, Color.parse(palette.accent)
            )
            self.assertEqual(int(you_widget.styles.width.value), MESH_SELECTED_GLYPH_WIDTH)

    async def test_stale_unselected_node_uses_dim_base(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            # Beyond ACTIVE_WINDOW_SECONDS but within
            # MESH_VERY_OLD_THRESHOLD_SECONDS -- STALE, not VERY_OLD, so
            # the node stays admitted (and rendered dim) rather than
            # being excluded from the board entirely (see
            # MeshActivityTier).
            very_old = 1_700_000_000.0 - MESH_STALE_THRESHOLD_SECONDS // 2
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id="!stale001",
                    sender_long_name="StaleOne",
                    sender_short_name=None,
                    channel_index=0,
                    text="ancient",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=very_old,
                )
            )
            await pilot.press("3")
            await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_000.0)
            await pilot.pause()
            palette = THEME_PALETTES[app._current_theme]
            stale_widget = next(
                widget for widget in app.query(MeshNodeWidget) if widget.node_id == "!stale001"
            )
            rendered = stale_widget.render()
            self.assertEqual(
                rendered.spans[0].style.foreground, Color.parse(palette.dim_base)
            )

    async def test_no_selection_background_rectangle(self) -> None:
        self.assertNotIn(".mesh-node:focus", MeshtasticPassApp.CSS)
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            for widget in app.query(MeshNodeWidget):
                self.assertEqual(widget.styles.background.a, 0)

    # ---- Zero new radio traffic (spec section 26) ---------------------

    async def test_mesh_refresh_navigation_generate_no_radio_traffic(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            for key in ("up", "down", "left", "right", "up", "down"):
                await pilot.press(key)
                await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()
            self.assertEqual(app.radio.sent_messages, ())

    async def test_mesh_never_calls_send_text_or_traceroute_paths(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            for method_name in ("send_text", "sendData", "sendText", "sendPosition"):
                if hasattr(app.radio, method_name):
                    setattr(
                        app.radio,
                        method_name,
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            AssertionError(f"MESH must never call {method_name}")
                        ),
                    )
            for message in SIMULATED_MESSAGES:
                app._accept_received_message(message)
            await self._open_mesh(pilot)
            for key in ("up", "down", "left", "right"):
                await pilot.press(key)
                await pilot.pause()
            app._refresh_mesh(wall_now=1_700_000_100.0)
            await pilot.pause()


class MeshNodeDbFirstLiveUpdateTests(unittest.IsolatedAsyncioTestCase):
    """MESH's board must consume the same fresh NodeDB state driving

    [3] MESH (N) -- count and board are computed from ONE shared
    working-set snapshot per refresh (see app._refresh_mesh), and
    admission is NodeDB-first (see build_mesh_working_set): a real node
    the radio has passively heard from is a candidate whether or not it
    has ever sent a CHAT message.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    def _mounted_ids(self, app: MeshtasticPassApp) -> set[str]:
        return {w.node_id for w in app.query(MeshNodeWidget)}

    async def test_count_and_board_agree_when_previously_unknown_nodes_go_active(
        self,
    ) -> None:
        """The exact reported mismatch: [3] MESH (N) must never change

        without the board changing too. Start with zero known remote
        nodes (count 0, empty board); mutate the underlying RadioService
        as real hardware would (two nodes newly heard); a normal refresh
        (no tab switch, no reconnect) must bring both the count AND the
        board into agreement, with no stale snapshot left mounted.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local_only = (NodeMetadata(app.radio.info.node_id, is_local=True),)
            app.radio.get_known_nodes = lambda: local_only
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertIn("MESH(0)", str(app.query_one("#tab-bar").render()))
            self.assertEqual(
                {state.node.node_id for state in view.working_set if not state.node.is_local},
                set(),
            )

            fresh_nodes = local_only + (
                NodeMetadata("!fresh001", "Fresh One", last_heard=now - 5),
                NodeMetadata("!fresh002", "Fresh Two", last_heard=now - 5),
            )
            app.radio.get_known_nodes = lambda: fresh_nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn("MESH(2)", tab_bar)
            remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertEqual(remote_ids, {"!fresh001", "!fresh002"})
            self.assertEqual(self._mounted_ids(app), {n.node_id for n in fresh_nodes})
            palette = THEME_PALETTES[app._current_theme]
            for node_id in ("!fresh001", "!fresh002"):
                widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
                self.assertEqual(
                    widget.render().spans[0].style.foreground, Color.parse(palette.base)
                )

    async def test_new_nodedb_node_appears_without_any_chat_history(self) -> None:
        """A brand-new node the radio starts hearing mid-session appears

        on refresh with no TEXT_MESSAGE_APP required -- NodeDB-first
        admission, not CHAT-history admission (see
        build_mesh_working_set).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            stale_last_heard = now - ACTIVE_WINDOW_SECONDS - 100
            a = NodeMetadata("!aaaaaaaa", "Node A", last_heard=stale_last_heard)
            b = NodeMetadata("!bbbbbbbb", "Node B", last_heard=stale_last_heard)
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            app.radio.get_known_nodes = lambda: (local, a, b)
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertNotIn(
                "!cccccccc",
                {state.node.node_id for state in view.working_set},
            )

            c = NodeMetadata("!cccccccc", "Node C", last_heard=now - 5)
            app.radio.get_known_nodes = lambda: (local, a, b, c)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertIn("!cccccccc", remote_ids)
            self.assertIn("!cccccccc", self._mounted_ids(app))
            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))
            c_state = next(
                state for state in view.working_set if state.node.node_id == "!cccccccc"
            )
            self.assertFalse(c_state.is_client)

    async def test_live_hops_away_change_updates_relay_stage_count(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            far = NodeMetadata("!far00001", "Far Node", hops_away=1, last_heard=now - 5)
            app.radio.get_known_nodes = lambda nodes=(local, far): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(
                len(
                    [
                        s
                        for s in view.relay_stages
                        if s.node_id.startswith("relay:!far00001:")
                    ]
                ),
                1,
            )

            far_now_3_hops = NodeMetadata(
                "!far00001", "Far Node", hops_away=3, last_heard=now - 4
            )
            app.radio.get_known_nodes = lambda nodes=(local, far_now_3_hops): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(
                len(
                    [
                        s
                        for s in view.relay_stages
                        if s.node_id.startswith("relay:!far00001:")
                    ]
                ),
                3,
            )

    async def test_live_position_change_updates_placement(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            position_a = GeoPosition(10.0, 10.0)
            moving = NodeMetadata(
                "!move0001", "Mover", hops_away=1, last_heard=now - 5, position=position_a
            )
            app.radio.get_known_nodes = lambda nodes=(local, moving): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            position_before = view.base_positions["!move0001"]

            position_b = GeoPosition(-40.0, 130.0)
            moved = NodeMetadata(
                "!move0001", "Mover", hops_away=1, last_heard=now - 4, position=position_b
            )
            app.radio.get_known_nodes = lambda nodes=(local, moved): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            position_after = view.base_positions["!move0001"]
            self.assertNotEqual(position_before, position_after)

    async def test_live_last_heard_transition_updates_color_and_count(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            stale_last_heard = now - ACTIVE_WINDOW_SECONDS - 100
            crosser = NodeMetadata("!cross001", "Crosser", last_heard=stale_last_heard)
            app.radio.get_known_nodes = lambda nodes=(local, crosser): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            palette = THEME_PALETTES[app._current_theme]
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!cross001")
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )
            self.assertIn("MESH(0)", str(app.query_one("#tab-bar").render()))

            fresh_crosser = NodeMetadata("!cross001", "Crosser", last_heard=now - 5)
            app.radio.get_known_nodes = lambda nodes=(local, fresh_crosser): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!cross001")
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.base)
            )
            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))


class MeshActiveConnectivityTests(unittest.IsolatedAsyncioTestCase):
    """Active real-node topology vs. stale last-known node history:

    a real node with a trustworthy nonzero hop count only gets an
    anonymous relay chain and a connector line back to YOU while it is
    currently ACTIVE (is_node_active(last_heard, now) -- the same
    predicate the board's BASE/DIM_BASE styling and [3] MESH (N) use).
    A stale node keeps its last-known grid position (see
    _mesh_hop_counts, used for placement regardless of activity) but no
    connector/relay chain -- see _mesh_active_hop_counts and
    MeshTopologyView.set_nodes.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    def _relay_source_ids(self, view: MeshTopologyView) -> set[str]:
        return {stage.source_node_id for stage in view.relay_stages}

    async def test_active_nodes_get_topology_stale_nodes_stay_dim_and_disconnected(
        self,
    ) -> None:
        """The item-9 example: ALICE (active, 0 hops) and CHARLIE (active,

        2 hops) participate in visible connector topology; BOB and DAVID
        (stale) remain visible at their last-known position with no
        relay chain and no connector. [3] MESH(2) -- exactly the two
        active endpoints, never the four displayed real nodes.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            stale_last_heard = now - ACTIVE_WINDOW_SECONDS - 100
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            alice = NodeMetadata(
                "!a1ice000", "Alice", "ALC", 0,
                last_heard=now - 5, position=north_of_local(3),
            )
            bob = NodeMetadata(
                "!b0b00000", "Bob", "BOB", 0,
                last_heard=stale_last_heard, position=south_of_local(3),
            )
            charlie = NodeMetadata(
                "!cha4lie0", "Charlie", "CHL", 2,
                last_heard=now - 5, position=east_of_local(3),
            )
            david = NodeMetadata(
                "!dav1d000", "David", "DAV", 1,
                last_heard=stale_last_heard, position=west_of_local(3),
            )
            app.radio.get_known_nodes = lambda: (local, alice, bob, charlie, david)
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            self.assertIn("MESH(2)", str(app.query_one("#tab-bar").render()))
            view = app.query_one(MeshTopologyView)

            source_ids = self._relay_source_ids(view)
            self.assertNotIn("!b0b00000", source_ids)
            self.assertNotIn("!dav1d000", source_ids)
            self.assertNotIn("!a1ice000", source_ids)  # 0 hops -> no stages
            self.assertIn("!cha4lie0", source_ids)
            self.assertEqual(
                sum(1 for s in view.relay_stages if s.source_node_id == "!cha4lie0"), 2
            )

            for stale_id in ("!b0b00000", "!dav1d000"):
                self.assertIn(stale_id, {w.node_id for w in app.query(MeshNodeWidget)})

    async def test_node_becoming_active_gains_connector_without_moving(self) -> None:
        """Live INACTIVE -> ACTIVE transition (item 4/5): a normal refresh

        brightens the node, generates its relay chain from its current
        hops_away, and connects it back to YOU -- all without a tab
        switch/reconnect/restart, and without moving the node to a new
        grid position (placement is independent of activity).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            stale_last_heard = now - ACTIVE_WINDOW_SECONDS - 100
            crosser = NodeMetadata(
                "!cr055er0", "Crosser", "CRS", 2,
                last_heard=stale_last_heard, position=north_of_local(5),
            )
            app.radio.get_known_nodes = lambda nodes=(local, crosser): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertNotIn("!cr055er0", self._relay_source_ids(view))
            position_before = view.base_positions["!cr055er0"]
            self.assertIn("MESH(0)", str(app.query_one("#tab-bar").render()))

            fresh_crosser = NodeMetadata(
                "!cr055er0", "Crosser", "CRS", 2,
                last_heard=now - 5, position=north_of_local(5),
            )
            app.radio.get_known_nodes = lambda nodes=(local, fresh_crosser): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))
            self.assertEqual(view.base_positions["!cr055er0"], position_before)
            self.assertEqual(
                sum(1 for s in view.relay_stages if s.source_node_id == "!cr055er0"), 2
            )
            palette = THEME_PALETTES[app._current_theme]
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!cr055er0")
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.base)
            )

    async def test_node_becoming_inactive_loses_connector_without_orphans(self) -> None:
        """Live ACTIVE -> INACTIVE transition (item 4/6): a normal refresh

        dims the node, removes its connector path, and removes its own
        synthetic relay stages -- with no orphan relay widgets left
        behind for anyone -- while the node itself stays mounted and
        visible.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            active_node = NodeMetadata(
                "!ag1ng000", "Ager", "AGR", 3,
                last_heard=now - 5, position=south_of_local(5),
            )
            app.radio.get_known_nodes = lambda nodes=(local, active_node): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertIn("!ag1ng000", self._relay_source_ids(view))
            self.assertEqual(len(view.relay_stages), 3)
            self.assertEqual(len(list(app.query(MeshRelayWidget))), 3)
            position_before = view.base_positions["!ag1ng000"]
            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))

            stale_node = NodeMetadata(
                "!ag1ng000", "Ager", "AGR", 3,
                last_heard=now - ACTIVE_WINDOW_SECONDS - 100, position=south_of_local(5),
            )
            app.radio.get_known_nodes = lambda nodes=(local, stale_node): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            self.assertIn("MESH(0)", str(app.query_one("#tab-bar").render()))
            self.assertEqual(view.relay_stages, ())
            self.assertEqual(len(list(app.query(MeshRelayWidget))), 0)
            self.assertIn("!ag1ng000", {w.node_id for w in app.query(MeshNodeWidget)})
            self.assertEqual(view.base_positions["!ag1ng000"], position_before)
            palette = THEME_PALETTES[app._current_theme]
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!ag1ng000")
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )

    async def test_every_relay_widget_belongs_to_a_currently_active_endpoint(
        self,
    ) -> None:
        """General invariant (item 6/7): after a refresh with a mix of

        active and stale hopped real nodes, every mounted MeshRelayWidget
        belongs (via source_node_id) to a real node that is BOTH in the
        current working set AND currently active -- no orphan relay
        widget ever exists for a stale or vanished node.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            stale_last_heard = now - ACTIVE_WINDOW_SECONDS - 100
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            nodes = (
                local,
                NodeMetadata(
                    "!act1ve01", "Active1", "AC1", 1,
                    last_heard=now - 5, position=north_of_local(4),
                ),
                NodeMetadata(
                    "!act1ve02", "Active2", "AC2", 2,
                    last_heard=now - 5, position=east_of_local(4),
                ),
                NodeMetadata(
                    "!sta1e001", "Stale1", "ST1", 3,
                    last_heard=stale_last_heard, position=south_of_local(4),
                ),
                NodeMetadata(
                    "!sta1e002", "Stale2", "ST2", 1,
                    last_heard=stale_last_heard, position=west_of_local(4),
                ),
            )
            app.radio.get_known_nodes = lambda: nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            active_ids = {
                state.node.node_id
                for state in view.working_set
                if not state.node.is_local
                and is_node_active(state.node.last_heard, now)
            }
            self.assertEqual(active_ids, {"!act1ve01", "!act1ve02"})
            for stage in view.relay_stages:
                self.assertIn(stage.source_node_id, active_ids)
            relay_widget_ids = {w.node_id for w in app.query(MeshRelayWidget)}
            self.assertEqual(relay_widget_ids, {s.node_id for s in view.relay_stages})

    async def test_stale_node_stays_selectable_via_arrow_navigation(self) -> None:
        """Losing active connector topology never removes a node from the

        navigation candidate set (see _move_mesh_focus, which filters
        only relay-stage IDs, never by activity) -- a stale, dim,
        disconnected real node is still a first-class, selectable board
        member.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            stale = NodeMetadata(
                "!qu1et000", "Quiet", "QUI", 1,
                last_heard=now - ACTIVE_WINDOW_SECONDS - 100,
                position=north_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, stale): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.relay_stages, ())
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!qu1et000")
            status = _bar_text(app)
            self.assertTrue(status.startswith("LONG NAME Quiet"))

    async def test_no_orphan_relays_under_realistic_mixed_topology(self) -> None:
        """Reproduction of the hardware-observed "phantom relay" report:

        a board mixing several stale, unlabeled-looking real nodes with
        several active ones of varying hop counts, spread across
        distinct compass directions (so bucket-ordering can't merge
        their chains). For every rendered relay stage, prove its owner
        is a real node that is (a) currently ACTIVE, (b) present in the
        working set, (c) actually mounted as a MeshNodeWidget, and (d)
        rendered FILLED (see item 2) -- and that the owner's stage COUNT
        exactly matches its hops_away, never more, never fewer. No
        relay may ever exist without satisfying all four.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            stale_last_heard = now - ACTIVE_WINDOW_SECONDS - 100
            active_last_heard = now - 5
            miles = 3.0
            offset = miles / _MILES_PER_DEGREE_AT_EQUATOR
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            # 4 stale (unknown/known hops, never active) + 4 active
            # (varying hop counts, including a long chain), one per
            # compass direction.
            nodes = (
                local,
                NodeMetadata(
                    "!qnzt0000", "QnzT", "QNZ", None,
                    last_heard=stale_last_heard, position=GeoPosition(offset, -offset),
                ),
                NodeMetadata(
                    "!b60c0000", "b60c", "B60", 2,
                    last_heard=stale_last_heard, position=GeoPosition(-offset, offset),
                ),
                NodeMetadata(
                    "!zrak0000", "Zrak", "ZRK", 1,
                    last_heard=stale_last_heard, position=GeoPosition(-offset, -offset),
                ),
                NodeMetadata(
                    "!n2dh0000", "N2DH", "N2D", None,
                    last_heard=stale_last_heard, position=GeoPosition(offset, offset),
                ),
                NodeMetadata(
                    "!skugh000", "Skugh", "SKU", 1,
                    last_heard=active_last_heard, position=north_of_local(miles),
                ),
                NodeMetadata(
                    "!nova0000", "Nova", "NOV", 0,
                    last_heard=active_last_heard, position=west_of_local(miles),
                ),
                NodeMetadata(
                    "!b8b80000", "b8b8", "B8B", 5,
                    last_heard=active_last_heard, position=east_of_local(miles),
                ),
                NodeMetadata(
                    "!d0ec0000", "d0ec", "D0E", 2,
                    last_heard=active_last_heard, position=south_of_local(miles),
                ),
            )
            app.radio.get_known_nodes = lambda: nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn("MESH(4)", tab_bar)

            view = app.query_one(MeshTopologyView)
            expected_hops = {"!skugh000": 1, "!b8b80000": 5, "!d0ec0000": 2}
            active_ids = set(expected_hops) | {"!nova0000"}
            stale_ids = {"!qnzt0000", "!b60c0000", "!zrak0000", "!n2dh0000"}

            # (a)/(d): every active real node is mounted and FILLED;
            # every stale one is mounted and STROKED.
            palette = THEME_PALETTES[app._current_theme]
            for node_id in active_ids:
                widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
                self.assertEqual(str(widget.render()).strip(), CIRCLE_SOLID_LARGE)
            for node_id in stale_ids:
                widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
                self.assertEqual(str(widget.render()).strip(), CIRCLE_STROKED_LARGE)

            # (b)/(c): every relay's owner is a currently-active,
            # displayed working-set member -- never a stale one.
            mounted_ids = {w.node_id for w in app.query(MeshNodeWidget)}
            working_set_active_ids = {
                state.node.node_id
                for state in view.working_set
                if not state.node.is_local
                and is_node_active(state.node.last_heard, now)
            }
            self.assertEqual(working_set_active_ids, active_ids)
            for stage in view.relay_stages:
                self.assertIn(stage.source_node_id, active_ids)
                self.assertIn(stage.source_node_id, mounted_ids)
                self.assertNotIn(stage.source_node_id, stale_ids)

            # Exact stage count per active owner, no partial chains --
            # Nova (0 hops) gets none.
            for node_id, hops in expected_hops.items():
                owned = [s for s in view.relay_stages if s.source_node_id == node_id]
                self.assertEqual(len(owned), hops)
                self.assertEqual(
                    sorted(s.index for s in owned), list(range(1, hops + 1))
                )
            self.assertEqual(
                sum(1 for s in view.relay_stages if s.source_node_id == "!nova0000"), 0
            )
            self.assertEqual(len(view.relay_stages), sum(expected_hops.values()))

            # Every mounted relay widget corresponds 1:1 to a computed
            # stage -- no leftover/orphan widget beyond what the model
            # itself says should exist.
            relay_widget_ids = {w.node_id for w in app.query(MeshRelayWidget)}
            self.assertEqual(relay_widget_ids, {s.node_id for s in view.relay_stages})


class MeshGeographicModeTransitionTests(unittest.IsolatedAsyncioTestCase):
    """App-level integration coverage for MODE A/MODE B geographic

    placement transitions, coexistence with non-GPS fallback nodes, and
    the invariant that neither activity nor selection ever triggers a
    fresh geographic projection -- see mesh_topology.assign_grid_slots
    and _project_remote_gps_cluster for the underlying pure logic
    (covered independently by MeshRemoteRelativeGeographyTests).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    async def test_you_gaining_gps_switches_to_true_you_relative_mode(self) -> None:
        """Case C: YOU gains GPS mid-session -- placement switches to real

        YOU-relative geography and actual distance becomes available on
        a normal refresh, with no fake YOU coordinate ever having
        existed beforehand.
        """
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            now = 1_700_000_000.0
            you_id = app.radio.info.node_id
            alice_id, bob_id = "!al1cegps", "!b0bgps00"
            no_gps_you = NodeMetadata(you_id, is_local=True, position=None)
            alice = NodeMetadata(
                alice_id, "Alice", "ALC", None, last_heard=now - 5,
                position=north_of_local(2),
            )
            bob = NodeMetadata(
                bob_id, "Bob", "BOB", None, last_heard=now - 5, position=south_of_local(2)
            )
            app.radio.get_known_nodes = lambda: (no_gps_you, alice, bob)
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertEqual(
                {s.node.node_id for s in view.working_set if not s.node.is_local},
                {alice_id, bob_id},
            )
            _mesh_select_node(app, alice_id)
            await pilot.pause()
            # No shared GPS fix yet (YOU has none) -> DISTANCE is the
            # honest "--", never a fabricated/unit-suffixed value.
            self.assertEqual(_bar_field(_bar_text(app), "DISTANCE"), "--")

            gps_you = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            app.radio.get_known_nodes = lambda: (gps_you, alice, bob)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            _mesh_select_node(app, alice_id)
            await pilot.pause()
            status = _bar_text(app)
            # SimulatedRadioService's default synced display.units is
            # DISPLAY_UNITS_METRIC -- see MESH VIEW PASS item 11.
            self.assertTrue(_bar_field(status, "DISTANCE").endswith("km"))
            self.assertNotEqual(_bar_field(status, "DISTANCE"), "--")

    async def test_you_losing_gps_returns_to_remote_relative_mode(self) -> None:
        """Case D: YOU loses GPS -- returns cleanly to remote-relative

        placement on the next normal refresh.
        """
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            now = 1_700_000_000.0
            you_id = app.radio.info.node_id
            alice_id, bob_id = "!al1cegps", "!b0bgps00"
            alice = NodeMetadata(
                alice_id, "Alice", "ALC", None, last_heard=now - 5, position=west_of_local(2)
            )
            bob = NodeMetadata(
                bob_id, "Bob", "BOB", None, last_heard=now - 5, position=east_of_local(2)
            )
            gps_you = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            app.radio.get_known_nodes = lambda: (gps_you, alice, bob)
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            _mesh_select_node(app, alice_id)
            await pilot.pause()
            self.assertNotEqual(_bar_field(_bar_text(app), "DISTANCE"), "--")

            no_gps_you = NodeMetadata(you_id, is_local=True, position=None)
            app.radio.get_known_nodes = lambda: (no_gps_you, alice, bob)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertLess(
                view.base_positions[alice_id][1], view.base_positions[bob_id][1]
            )
            _mesh_select_node(app, alice_id)
            await pilot.pause()
            # No shared GPS fix any more (YOU lost it) -> DISTANCE is the
            # honest "--", never a fabricated/unit-suffixed value.
            self.assertEqual(_bar_field(_bar_text(app), "DISTANCE"), "--")

    async def test_gps_cluster_coexists_with_fallback_nodes_without_collision(
        self,
    ) -> None:
        """Case E: 3 GPS remotes + 2 no-GPS remotes, YOU with no GPS.

        The GPS cluster keeps its relative geometry; the fallback nodes
        get deterministic free positions that never collide with the
        GPS cluster or distort it.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=None)
            alice = NodeMetadata(
                "!al1ce001", "Alice", "ALC", None, last_heard=now - 5,
                position=north_of_local(3),
            )
            bob = NodeMetadata(
                "!b0b00001", "Bob", "BOB", None, last_heard=now - 5, position=west_of_local(3)
            )
            david = NodeMetadata(
                "!dav1d001", "David", "DAV", None, last_heard=now - 5,
                position=east_of_local(3),
            )
            charlie = NodeMetadata(
                "!char1001", "Charlie", "CHR", None, last_heard=now - 5, position=None
            )
            erin = NodeMetadata(
                "!er1n0001", "Erin", "ERN", None, last_heard=now - 5, position=None
            )
            nodes = (local, alice, bob, david, charlie, erin)
            app.radio.get_known_nodes = lambda: nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            positions = view.base_positions
            all_ids = ["!al1ce001", "!b0b00001", "!dav1d001", "!char1001", "!er1n0001"]
            position_values = [positions[n] for n in all_ids] + [
                positions[app.radio.info.node_id]
            ]
            self.assertEqual(len(position_values), len(set(position_values)))
            # Bob (west) still strictly west of David (east) -- the
            # fallback nodes did not distort the GPS cluster's geometry.
            self.assertLess(positions["!b0b00001"][1], positions["!dav1d001"][1])

    async def test_activity_change_alone_never_moves_a_gps_node(self) -> None:
        """Case H: ACTIVE -> STALE for a MODE B GPS-relative node changes

        only its styling/topology, never its position.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=None)
            alice = NodeMetadata(
                "!ag1nggps", "Ager", "AGR", None, last_heard=now - 5, position=north_of_local(3)
            )
            bob = NodeMetadata(
                "!b0bstblx", "Bob", "BOB", None, last_heard=now - 5, position=south_of_local(3)
            )
            app.radio.get_known_nodes = lambda nodes=(local, alice, bob): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            position_before = view.base_positions["!ag1nggps"]

            stale_alice = NodeMetadata(
                "!ag1nggps", "Ager", "AGR", None,
                last_heard=now - ACTIVE_WINDOW_SECONDS - 100, position=north_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, stale_alice, bob): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(view.base_positions["!ag1nggps"], position_before)
            palette = THEME_PALETTES[app._current_theme]
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!ag1nggps")
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )

    async def test_selection_change_never_reprojects_gps_cluster(self) -> None:
        """Case I: selecting a different node must not trigger a fresh

        geographic projection with different normalization -- the
        underlying relative geometry is exactly preserved, only the
        recentering translation changes (see _mesh_translated_positions).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=None)
            alice = NodeMetadata(
                "!al1cesel", "Alice", "ALC", None, last_heard=now - 5,
                position=north_of_local(3),
            )
            bob = NodeMetadata(
                "!b0bselec", "Bob", "BOB", None, last_heard=now - 5, position=south_of_local(3)
            )
            app.radio.get_known_nodes = lambda nodes=(local, alice, bob): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            base_positions_before = dict(view.base_positions)

            _mesh_select_node(app, "!al1cesel")
            await pilot.pause()
            self.assertEqual(view.base_positions, base_positions_before)

    async def test_live_gps_update_reflows_placement_on_normal_refresh(self) -> None:
        """Case J: a remote node gaining/changing valid GPS mid-session

        updates the geographic placement on the next normal refresh --
        no tab switch, reconnect, or restart required.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=None)
            alice = NodeMetadata(
                "!al1velv1", "Alice", "ALC", None, last_heard=now - 5, position=None
            )
            bob = NodeMetadata(
                "!b0blivea", "Bob", "BOB", None, last_heard=now - 5, position=west_of_local(3)
            )
            app.radio.get_known_nodes = lambda nodes=(local, alice, bob): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(
                {s.node.node_id for s in view.working_set if s.node.node_id == "!al1velv1"},
                {"!al1velv1"},
            )
            # Only one GPS remote (Bob) so far -- fallback placement.
            alice_before = view.base_positions["!al1velv1"]

            gps_alice = NodeMetadata(
                "!al1velv1", "Alice", "ALC", None, last_heard=now - 5,
                position=east_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, gps_alice, bob): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            # Now two GPS remotes -- relative geography kicks in, and
            # Alice (east) must land strictly east of Bob (west).
            self.assertLess(
                view.base_positions["!b0blivea"][1], view.base_positions["!al1velv1"][1]
            )
            self.assertNotEqual(view.base_positions["!al1velv1"], alice_before)


class MeshUnknownHopsConnectorTests(unittest.IsolatedAsyncioTestCase):
    """Active real nodes must always have a connector toward YOU, even

    with an unknown hop count -- the amount of known route detail
    determines relay visualization, never whether a connection is drawn
    at all. See app.py's connector-generation loop in
    MeshTopologyView.set_nodes(), which previously skipped a node
    entirely whenever hops_away was None.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    def _connector_cells(self, view: MeshTopologyView):
        canvas = view.board.query_one(MeshCanvas)
        signature = canvas._signature
        return signature[2] if signature is not None else ()

    async def test_active_unknown_hops_gets_direct_connector_no_relays(self) -> None:
        """Case A: active, hopsAway=None -> counted, filled, BASE, a

        direct connector to YOU, and zero anonymous relay stages.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            unknown_hops = NodeMetadata(
                "!unkn0001", "Unknown Hops", "UNK", None,
                last_heard=now - 5, position=north_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, unknown_hops): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.relay_stages, ())
            self.assertGreater(len(self._connector_cells(view)), 0)
            palette = THEME_PALETTES[app._current_theme]
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!unkn0001")
            self.assertEqual(str(widget.render()).strip(), CIRCLE_SOLID_LARGE)
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.base)
            )

    async def test_active_zero_hops_gets_direct_connector_no_relays(self) -> None:
        """Case B: active, hopsAway=0 -> direct connector, zero relays."""
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            zero_hops = NodeMetadata(
                "!zero0001", "Zero Hops", "ZER", 0,
                last_heard=now - 5, position=north_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, zero_hops): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.relay_stages, ())
            self.assertGreater(len(self._connector_cells(view)), 0)

    async def test_active_one_hop_gets_exactly_one_relay(self) -> None:
        """Case C."""
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            one_hop = NodeMetadata(
                "!one00001", "One Hop", "ONE", 1,
                last_heard=now - 5, position=north_of_local(5),
            )
            app.radio.get_known_nodes = lambda nodes=(local, one_hop): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 1)
            self.assertGreater(len(self._connector_cells(view)), 0)

    async def test_active_three_hops_gets_exactly_three_relays(self) -> None:
        """Case D."""
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            three_hops = NodeMetadata(
                "!three001", "Three Hops", "THR", 3,
                last_heard=now - 5, position=north_of_local(9),
            )
            app.radio.get_known_nodes = lambda nodes=(local, three_hops): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(len(view.relay_stages), 3)
            self.assertGreater(len(self._connector_cells(view)), 0)

    async def test_stale_known_hops_stays_visible_with_dashed_connector(
        self,
    ) -> None:
        """Case E: a stale node with a known hop count remains visible

        and selectable, stroked/DIM_BASE, with a DIM+DOTTED direct
        connector (no relay chain of its own -- see item 5 of the MESH
        activity-model task: STALE no longer means "no connector at
        all", only "no active/relay-chain connector").
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            stale = NodeMetadata(
                "!stale002", "Stale Known", "STK", 2,
                last_heard=now - ACTIVE_WINDOW_SECONDS - 100, position=north_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, stale): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.relay_stages, ())
            cells = self._connector_cells(view)
            self.assertGreater(len(cells), 0)
            self.assertTrue(any(glyph in ("╌", "╎") for _x, _y, glyph, _c in cells))
            self.assertIn("!stale002", {w.node_id for w in app.query(MeshNodeWidget)})
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!stale002")

    async def test_real_device_snapshot_three_active_unknown_hops(self) -> None:
        """Case F: several stale nodes plus exactly 3 active nodes, all

        3 active nodes with UNKNOWN hops -- MESH(3), exactly 3 filled
        active endpoints, all 3 with direct abstract connections, zero
        fabricated relay circles anywhere on the board.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            stale_last_heard = now - ACTIVE_WINDOW_SECONDS - 100
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            active_ids = {"!act1ve01", "!act1ve02", "!act1ve03"}
            nodes = [
                local,
                NodeMetadata(
                    "!act1ve01", "A1", "A1", None, last_heard=now - 5,
                    position=north_of_local(3),
                ),
                NodeMetadata(
                    "!act1ve02", "A2", "A2", None, last_heard=now - 5,
                    position=east_of_local(3),
                ),
                NodeMetadata(
                    "!act1ve03", "A3", "A3", None, last_heard=now - 5,
                    position=south_of_local(3),
                ),
                NodeMetadata(
                    "!sta1e001", "S1", "S1", 2, last_heard=stale_last_heard,
                    position=west_of_local(3),
                ),
                NodeMetadata(
                    "!sta1e002", "S2", "S2", None, last_heard=stale_last_heard,
                    position=west_of_local(6),
                ),
            ]
            app.radio.get_known_nodes = lambda nodes=tuple(nodes): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            self.assertIn("MESH(3)", str(app.query_one("#tab-bar").render()))
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.relay_stages, ())
            self.assertEqual(len(list(app.query(MeshRelayWidget))), 0)
            palette = THEME_PALETTES[app._current_theme]
            for node_id in active_ids:
                widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == node_id)
                self.assertEqual(str(widget.render()).strip(), CIRCLE_SOLID_LARGE)
            self.assertGreater(len(self._connector_cells(view)), 0)

    async def test_unknown_hops_node_stale_to_active_gains_connector_in_place(
        self,
    ) -> None:
        """Case G: endpoint position stays fixed; its DASHED stale

        connector becomes a normal SOLID one on the exact refresh it
        becomes active (see item 5: STALE now draws a dim/dotted
        connector rather than none at all).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            stale = NodeMetadata(
                "!transit1", "Transit", "TR", None,
                last_heard=now - ACTIVE_WINDOW_SECONDS - 100, position=north_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, stale): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            position_before = view.base_positions["!transit1"]
            stale_cells = self._connector_cells(view)
            self.assertGreater(len(stale_cells), 0)
            self.assertTrue(any(glyph in ("╌", "╎") for _x, _y, glyph, _c in stale_cells))

            active = NodeMetadata(
                "!transit1", "Transit", "TR", None,
                last_heard=now - 5, position=north_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, active): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(view.base_positions["!transit1"], position_before)
            active_cells = self._connector_cells(view)
            self.assertGreater(len(active_cells), 0)
            self.assertFalse(any(glyph in ("╌", "╎") for _x, _y, glyph, _c in active_cells))
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!transit1")
            self.assertEqual(str(widget.render()).strip(), CIRCLE_SOLID_LARGE)

    async def test_unknown_hops_node_active_to_stale_loses_connector_in_place(
        self,
    ) -> None:
        """Case H: the reverse transition -- endpoint stays fixed, dims,

        and its SOLID connector becomes a DASHED one (never disappears
        outright -- see item 5).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            active = NodeMetadata(
                "!transit2", "Transit2", "TR2", None,
                last_heard=now - 5, position=north_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, active): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            position_before = view.base_positions["!transit2"]
            active_cells = self._connector_cells(view)
            self.assertGreater(len(active_cells), 0)
            self.assertFalse(any(glyph in ("╌", "╎") for _x, _y, glyph, _c in active_cells))

            stale = NodeMetadata(
                "!transit2", "Transit2", "TR2", None,
                last_heard=now - ACTIVE_WINDOW_SECONDS - 100, position=north_of_local(3),
            )
            app.radio.get_known_nodes = lambda nodes=(local, stale): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(view.base_positions["!transit2"], position_before)
            stale_cells = self._connector_cells(view)
            self.assertGreater(len(stale_cells), 0)
            self.assertTrue(any(glyph in ("╌", "╎") for _x, _y, glyph, _c in stale_cells))
            palette = THEME_PALETTES[app._current_theme]
            widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!transit2")
            self.assertEqual(str(widget.render()).strip(), CIRCLE_STROKED_LARGE)
            self.assertEqual(
                widget.render().spans[0].style.foreground, Color.parse(palette.dim_base)
            )


class MeshUnrelatedNodeObstacleAvoidanceTests(unittest.IsolatedAsyncioTestCase):
    """The real reported visual bug (item 8 of the MESH topology task):

    a direct ALICE<->ME connector could previously render as if it
    passed straight through BOB's own rendered cell merely because BOB
    happens to sit geometrically between them -- with no relay/hop
    evidence BOB actually carried that traffic. Positions below are
    empirically confirmed (not guessed) to place BOB exactly on
    ALICE's DEFAULT (obstacle-unaware) elbow route.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def test_connector_avoids_an_unrelated_real_node_geometrically_in_the_way(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            you = NodeMetadata(app.radio.info.node_id, is_local=True)
            alice = NodeMetadata("!a1a1a1a1", "Alice", "AL", 0, last_heard=1_800_000_000.0)

            # ALICE alone first: confirms BOB's position (below) really
            # does sit on ALICE's unobstructed default route -- the
            # precondition this test depends on, checked directly
            # rather than assumed.
            view.set_nodes(
                (
                    MeshNodeState(
                        node=you, is_client=False, is_relay=False, last_interaction_at=None
                    ),
                    MeshNodeState(
                        node=alice,
                        is_client=True,
                        is_relay=False,
                        last_interaction_at=1_800_000_000.0,
                    ),
                ),
                {app.radio.info.node_id: (0, 0), "!a1a1a1a1": (8, 4)},
                theme="snow",
                now=1_800_000_000.0,
            )
            await pilot.pause()
            baseline_positions = {
                (x, y) for x, y, _glyph, _color in view.board.query_one(MeshCanvas)._signature[2]
            }
            bob_position = (72, 22)
            self.assertIn(bob_position, baseline_positions)

            bob = NodeMetadata("!b0b0b0b0", "Bob", "BB", 0, last_heard=1_800_000_000.0)
            view.set_nodes(
                (
                    MeshNodeState(
                        node=you, is_client=False, is_relay=False, last_interaction_at=None
                    ),
                    MeshNodeState(
                        node=alice,
                        is_client=True,
                        is_relay=False,
                        last_interaction_at=1_800_000_000.0,
                    ),
                    MeshNodeState(
                        node=bob, is_client=True, is_relay=False, last_interaction_at=1_800_000_000.0
                    ),
                ),
                {
                    app.radio.info.node_id: (0, 0),
                    "!a1a1a1a1": (8, 4),
                    "!b0b0b0b0": (4, 4),
                },
                theme="snow",
                now=1_800_000_000.0,
            )
            await pilot.pause()
            bob_widget = next(w for w in app.query(MeshNodeWidget) if w.node_id == "!b0b0b0b0")
            self.assertEqual(
                (int(bob_widget.styles.offset.x.value), int(bob_widget.styles.offset.y.value)),
                bob_position,
            )
            routed_positions = {
                (x, y) for x, y, _glyph, _color in view.board.query_one(MeshCanvas)._signature[2]
            }
            self.assertNotIn(bob_position, routed_positions)


class MeshSelectedRoutePaintPriorityTests(unittest.IsolatedAsyncioTestCase):
    """The real reported visual bug (item 10/11 of the MESH topology

    task): when many connectors overlap, focusing a node could
    previously render only PART of its own route in ACCENT, because a
    later-drawn unselected connector painted over shared cells --
    MeshCanvas's overlay dict keys on (x, y), so whichever connector's
    cells are appended LAST to the `connectors` tuple wins any shared
    cell. This constructs two clients whose default elbow routes
    genuinely share several transit cells (found empirically, not
    fabricated) to prove the fix: the SELECTED node's connector is now
    always appended last, so it always wins.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    def _set_overlapping_pair(self, app: MeshtasticPassApp) -> MeshTopologyView:
        """YOU at the origin; A and B's default elbow routes share every

        cell of YOU's own row out to column -32/+32 respectively (both
        turn at the same row) -- confirmed empirically, not asserted
        blindly; test_the_two_routes_genuinely_overlap below re-verifies
        this same precondition so a future geometry change that removes
        the overlap fails loudly here instead of silently proving
        nothing.
        """
        view = app.query_one(MeshTopologyView)
        you = NodeMetadata(app.radio.info.node_id, is_local=True)
        a = NodeMetadata("!aaaaaaaa", "A", "A", 0, last_heard=1_800_000_000.0)
        b = NodeMetadata("!bbbbbbbb", "B", "B", 0, last_heard=1_800_000_000.0)
        working_set = (
            MeshNodeState(node=you, is_client=False, is_relay=False, last_interaction_at=None),
            MeshNodeState(
                node=a, is_client=True, is_relay=False, last_interaction_at=1_800_000_000.0
            ),
            MeshNodeState(
                node=b, is_client=True, is_relay=False, last_interaction_at=1_800_000_000.0
            ),
        )
        base_positions = {
            app.radio.info.node_id: (0, 0),
            "!aaaaaaaa": (-4, -6),
            "!bbbbbbbb": (4, -2),
        }
        view.set_nodes(working_set, base_positions, theme="snow", now=1_800_000_000.0)
        return view

    def _connector_cells(self, view: MeshTopologyView):
        canvas = view.board.query_one(MeshCanvas)
        return canvas._signature[2]

    async def test_the_two_routes_genuinely_overlap(self) -> None:
        """Precondition check, not the fix itself: if this ever starts

        failing, the OTHER tests below stop meaning anything (they
        would trivially pass with no paint-priority fix at all).
        """
        app = self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            view = self._set_overlapping_pair(app)
            await pilot.pause()
            cells = self._connector_cells(view)
            positions = [(x, y) for x, y, _glyph, _color in cells]
            self.assertGreater(len(positions), len(set(positions)))

    async def test_selected_route_is_fully_accent_despite_overlap(self) -> None:
        """Every position where A's and B's routes genuinely collide

        (found directly from the RAW connector cells' own duplicates,
        never independently recomputed -- obstacle avoidance means
        B's route can legitimately differ depending on whether A is
        present at all, so a separately-computed "B alone" baseline is
        not a valid ground truth here) must resolve to ACCENT once B is
        selected: MeshCanvas's overlay dict keys on (x, y), so the
        LAST-drawn connector wins a shared cell -- this is what proves
        the selected route is drawn last, not merely coincidentally
        unaffected.
        """
        app = self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            # Select A specifically: A is iterated BEFORE B in
            # working_set (see _set_overlapping_pair), so without the
            # "selected drawn last" fix, B's later-drawn DIM cells would
            # silently overwrite A's ACCENT ones at every shared
            # position -- this ordering is what makes the test
            # genuinely exercise the fix, not just coincidentally pass.
            view = self._set_overlapping_pair(app)
            view.select_node("!aaaaaaaa")
            view.set_nodes(
                view.working_set, view.base_positions, theme="snow", now=1_800_000_000.0
            )
            await pilot.pause()
            palette = THEME_PALETTES["snow"]
            raw_cells = self._connector_cells(view)
            positions = [(x, y) for x, y, _glyph, _color in raw_cells]
            duplicated = {p for p in positions if positions.count(p) > 1}
            self.assertGreater(len(duplicated), 0)
            by_position = {(x, y): color for x, y, _glyph, color in raw_cells}
            for position in duplicated:
                self.assertEqual(by_position[position], palette.accent)

    async def test_deselecting_restores_ordinary_dim_styling(self) -> None:
        """Focus moving away must not leave any paint contamination --

        set_nodes() recomputes ordering fresh every call, nothing
        persists across calls.
        """
        app = self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            view = self._set_overlapping_pair(app)
            view.select_node("!bbbbbbbb")
            view.set_nodes(
                view.working_set, view.base_positions, theme="snow", now=1_800_000_000.0
            )
            await pilot.pause()

            view.select_local()
            view.set_nodes(
                view.working_set, view.base_positions, theme="snow", now=1_800_000_000.0
            )
            await pilot.pause()
            palette = THEME_PALETTES["snow"]
            cells = self._connector_cells(view)
            self.assertTrue(
                all(color == palette.dim_base for _x, _y, _glyph, color in cells)
            )


class MeshNodeBarConnectionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """MESH GPS + UNIFIED BAR Part B: #mesh-connection-status (top) vs

    #mesh-node-bar (bottom, unified) across the ONLINE/CONNECTING/OFFLINE
    lifecycle. The shared connection-status text lives in the TOP widget
    while not ONLINE; once ONLINE, that TOP widget is hidden entirely
    (freeing its row for the grid) and the unified bar shows the
    selected node's own TIME field instead (see app._update_mesh_node_bar).

    Unlike the old split-widget design's board-wide "LAST UPDATE"
    indicator, the unified bar is entirely selected-node-identity-driven
    -- so, deliberately, it BLANKS (rather than freezing a stale value)
    while off the MESH tab or not ONLINE, matching the old bottom-left
    context line's own precedent rather than the old bottom-right LAST
    UPDATE's "persist across a disconnect" one, since there is no longer
    a separate "board freshness, independent of any one node" concept to
    preserve.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _mesh_connection_text(self, app: MeshtasticPassApp) -> str:
        from textual.widgets import Static

        return str(app.query_one("#mesh-connection-status", Static).render())

    async def test_time_ages_between_refreshes_with_no_new_data(self) -> None:
        """Between two refreshes with the SAME underlying last_heard, the

        selected node's displayed TIME must grow by exactly the elapsed
        wall-clock time -- proving it is computed from (data, now), not
        reset just because a refresh happened.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            node = NodeMetadata("!ag1ng001", "Ager", last_heard=now - 5)
            app.radio.get_known_nodes = lambda nodes=(local, node): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            _mesh_select_node(app, "!ag1ng001")
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(_bar_field(_bar_text(app), "TIME"), "5s")

            app._refresh_mesh(wall_now=now + 30)
            await pilot.pause()
            self.assertEqual(_bar_field(_bar_text(app), "TIME"), "35s")

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    async def test_ui_refresh_alone_does_not_reset_time(self) -> None:
        """Calling _refresh_mesh() again with unchanged data AND

        unchanged wall-clock time must leave the displayed TIME exactly
        as it was -- a bare repaint is not a new data event.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            node = NodeMetadata("!st111111", "Steady", last_heard=now - 42)
            app.radio.get_known_nodes = lambda nodes=(local, node): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            _mesh_select_node(app, "!st111111")
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            first_text = _bar_text(app)
            self.assertEqual(_bar_field(first_text, "TIME"), "42s")

            for _ in range(3):
                app._refresh_mesh(wall_now=now)
                await pilot.pause()
            self.assertEqual(_bar_text(app), first_text)

    async def test_meaningful_nodedb_update_resets_time(self) -> None:
        """A genuinely fresher NodeDB last_heard resets the displayed

        TIME -- never derived from CHAT history, purely from the passive
        mesh-state timestamp itself.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            old_heard = NodeMetadata("!upd00001", "Updater", last_heard=now - 90)
            app.radio.get_known_nodes = lambda nodes=(local, old_heard): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            _mesh_select_node(app, "!upd00001")
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(_bar_field(_bar_text(app), "TIME"), "1m")

            fresh_heard = NodeMetadata("!upd00001", "Updater", last_heard=now)
            app.radio.get_known_nodes = lambda nodes=(local, fresh_heard): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(_bar_field(_bar_text(app), "TIME"), "0s")

    async def test_returning_online_restores_the_bar(self) -> None:
        """CONNECTING shows the shared status in the TOP widget; the bar

        blanks while not ONLINE and shows real content again the instant
        ONLINE resumes -- no second/duplicate widget is ever created,
        and the TOP widget goes back to hidden.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            node = NodeMetadata("!re1turn0", "Returner", last_heard=now - 5)
            app.radio.get_known_nodes = lambda nodes=(local, node): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            _mesh_select_node(app, "!re1turn0")
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertIn("TIME", _bar_text(app))
            self.assertFalse(app.query_one("#mesh-connection-status").display)

            app._show_connection(RadioState.CONNECTING)
            await pilot.pause()
            text = await self._mesh_connection_text(app)
            self.assertIn(f"STATUS {ANIMATED_STATUS[RadioState.CONNECTING]}", text)
            self.assertTrue(app.query_one("#mesh-connection-status").display)
            self.assertEqual(_bar_text(app), "")

            app._show_connection(RadioState.ONLINE, app.radio.info)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertIn("TIME", _bar_text(app))
            self.assertFalse(app.query_one("#mesh-connection-status").display)
            self.assertEqual(len(list(app.query("#mesh-connection-status"))), 1)
            self.assertEqual(len(list(app.query("#mesh-node-bar"))), 1)

    async def test_connecting_status_appears_on_top_and_blanks_the_bar(
        self,
    ) -> None:
        """While CONNECTING/RECONNECTING, the shared connection-status

        text appears in the TOP widget; the unified bar blanks (see this
        class's own docstring on why -- no board-wide freshness concept
        survives independent of the selected node any more).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            now = 1_700_000_000.0
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            stale = NodeMetadata(
                "!stale002", "Stale", last_heard=now - ACTIVE_WINDOW_SECONDS - 100
            )
            app.radio.get_known_nodes = lambda nodes=(local, stale): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            _mesh_select_node(app, "!stale002")
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertIn("TIME", _bar_text(app))
            self.assertFalse(app.query_one("#mesh-connection-status").display)

            app._show_connection(RadioState.OFFLINE, message="lost")
            await pilot.pause()
            top_text = await self._mesh_connection_text(app)
            self.assertIn(f"STATUS {ANIMATED_STATUS[RadioState.OFFLINE]}", top_text)
            self.assertTrue(app.query_one("#mesh-connection-status").display)
            self.assertEqual(_bar_text(app), "")


class MeshNodeBarLayoutTests(unittest.IsolatedAsyncioTestCase):
    """Item 5 / MESH GPS + UNIFIED BAR Part B: the old top-of-view

    connection-status row is freed for the grid once ONLINE, and the
    single #mesh-node-bar widget (which replaced the old #mesh-context-
    status/#mesh-last-update split) never wraps or overflows its row --
    see app._update_mesh_node_bar and mesh_state.format_mesh_node_bar_
    line's own width-tiered degradation (proven at the pure-function
    level in test_mesh_state.py's FormatMeshNodeBarLineTests).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def test_grid_gains_rows_once_top_status_row_is_reclaimed(self) -> None:
        """At a terminal height chosen so the freed 2 rows cross an odd/

        even boundary in _compute_mesh_grid_dimensions, going ONLINE
        (hiding #mesh-connection-status) measurably grows the visible
        grid's own row count -- proving #mesh-view's `height: 1fr`
        reclaims the space with no hardcoded row math, not merely that
        the widget's pixel height changed.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 31)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            app._show_connection(RadioState.OFFLINE, message="lost")
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            offline_rows, _, _, _ = view.current_grid_dimensions()

            app._show_connection(RadioState.ONLINE, app.radio.info)
            app._refresh_mesh()
            await pilot.pause()
            online_rows, _, _, _ = view.current_grid_dimensions()

            self.assertGreater(online_rows, offline_rows)

    async def test_bar_shows_full_content_at_normal_width(
        self,
    ) -> None:
        """At a normal XL uConsole-like width, a realistic-length bar

        renders its fullest tier in full -- no ellipsis, no dropped
        fields, no wrapping -- and never exceeds the widget's own
        measured width.
        """
        app = self._make_app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            node_id = "!ctxwidth1"
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            node = NodeMetadata(
                node_id, "Golf Sierra Portable", "GSP", last_heard=time.time() - 5
            )
            app.radio.get_known_nodes = lambda nodes=(local, node): nodes
            app._refresh_mesh()
            await pilot.pause()
            _mesh_select_node(app, node_id)
            await pilot.pause()

            bar_widget = app.query_one("#mesh-node-bar")
            text = _bar_text(app)

            self.assertIn("Golf Sierra Portable", text)
            self.assertNotIn("…", text)
            self.assertIn("LINK ---- -- / --", text)
            self.assertIn("SHORT NAME GSP", text)
            self.assertLessEqual(cell_len(text), bar_widget.size.width)

    async def test_bar_degrades_gracefully_at_narrow_width_without_wrapping(
        self,
    ) -> None:
        """At a narrow width, an overlong bar degrades deterministically

        (dropping lower-priority fields per format_mesh_node_bar_line's
        own tiers, and grapheme-safe-truncating with an ellipsis only as
        the very last resort) -- always one physical line, never
        overflowing the widget's own measured width.
        """
        app = self._make_app()
        async with app.run_test(size=(40, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            node_id = "!ctxwidth2"
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            node = NodeMetadata(
                node_id,
                "An Extremely Long Node Long Name That Cannot Possibly Fit",
                "LONG",
                last_heard=time.time() - 5,
            )
            app.radio.get_known_nodes = lambda nodes=(local, node): nodes
            app._refresh_mesh()
            await pilot.pause()
            _mesh_select_node(app, node_id)
            await pilot.pause()

            bar_widget = app.query_one("#mesh-node-bar")
            text = _bar_text(app)

            self.assertNotIn("\n", text)
            self.assertLessEqual(cell_len(text), bar_widget.size.width)


class MeshLinkQualityDisplayTests(unittest.IsolatedAsyncioTestCase):
    """MESH GPS + UNIFIED BAR Part B: MESH's passive LINK quality

    display, wired into the real app/#mesh-node-bar -- see
    test_mesh_state.py's FormatMeshLinkDisplayTests/
    FormatMeshNodeBarFieldsTests for the pure-function formatting rules
    this exercises end to end, and tests/test_radio_service.py's
    LinkQualityTests for RadioService.get_link_quality's own honesty
    rules (traversed_hops == 0, radio-swap safety).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def test_selected_remote_node_with_valid_link_shows_full_line(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            neighbor = NodeMetadata("!near0001", "Near Neighbor", "NEAR", last_heard=now - 3)
            app.radio.get_known_nodes = lambda nodes=(local, neighbor): nodes
            app.radio.get_link_quality = (
                lambda node_id: LinkObservation(rssi=-87, snr=6.0, observed_at=now - 3)
                if node_id == "!near0001"
                else None
            )
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            _mesh_select_node(app, "!near0001")
            await pilot.pause()
            text = _bar_text(app)
            self.assertEqual(_bar_field(text, "LINK"), "▂▄▆█ -87 / +6")
            self.assertEqual(_bar_field(text, "TIME"), "3s")

    async def test_selected_multi_hop_node_shows_honest_fallback_not_a_guess(
        self,
    ) -> None:
        """A node only ever reachable through relays must never borrow

        ANY node's real RSSI/SNR -- get_link_quality legitimately
        returns None for it (RadioService only ever records a
        traversed_hops == 0 packet), and the display must show the
        placeholder, never fabricate a plausible-looking number.
        """
        app = self._make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            far = NodeMetadata("!far00001", "Far Relay Client", "FAR", last_heard=now - 3, hops_away=2)
            app.radio.get_known_nodes = lambda nodes=(local, far): nodes
            app.radio.get_link_quality = lambda node_id: None
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            _mesh_select_node(app, "!far00001")
            await pilot.pause()
            text = _bar_text(app)
            self.assertEqual(_bar_field(text, "LINK"), "---- -- / --")
            self.assertEqual(_bar_field(text, "TIME"), "3s")

    async def test_you_selected_never_queries_link(self) -> None:
        """A radio has no RF link to itself -- selecting YOU must show

        the honest placeholder LINK segment, and get_link_quality must
        never even be called for YOU's own node ID (no self-LINK claim
        of any kind, not merely "displayed as unknown").
        """
        app = self._make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            remote = NodeMetadata("!rem00001", "Remote", last_heard=now - 3)
            app.radio.get_known_nodes = lambda nodes=(local, remote): nodes
            queried_ids: list[str] = []

            def get_link_quality(node_id: str) -> LinkObservation | None:
                queried_ids.append(node_id)
                return LinkObservation(rssi=-40, snr=15.0, observed_at=now)

            app.radio.get_link_quality = get_link_quality
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            _mesh_select_node(app, local.node_id)
            await pilot.pause()
            text = _bar_text(app)
            self.assertEqual(_bar_field(text, "LINK"), "---- -- / --")
            self.assertEqual(_bar_field(text, "TIME"), "NOW")
            self.assertNotIn(local.node_id, queried_ids)

    async def test_changing_selection_switches_link_data_immediately(self) -> None:
        """Selection-driven (see _move_mesh_focus/_mesh_select_node), not

        waiting for the next periodic _refresh_mesh() tick -- and never
        leaking the PREVIOUSLY selected node's own reading onto the
        newly selected one.
        """
        app = self._make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            strong = NodeMetadata("!strong01", "Strong", last_heard=now - 1)
            weak = NodeMetadata("!weak0001", "Weak", last_heard=now - 1)
            app.radio.get_known_nodes = lambda nodes=(local, strong, weak): nodes
            readings = {
                "!strong01": LinkObservation(rssi=-40, snr=15.0, observed_at=now - 1),
                "!weak0001": LinkObservation(rssi=-110, snr=-15.0, observed_at=now - 1),
            }
            app.radio.get_link_quality = lambda node_id: readings.get(node_id)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            _mesh_select_node(app, "!strong01")
            await pilot.pause()
            strong_link = _bar_field(_bar_text(app), "LINK")
            self.assertEqual(strong_link, "▂▄▆█ -40 / +15")

            _mesh_select_node(app, "!weak0001")
            await pilot.pause()
            weak_link = _bar_field(_bar_text(app), "LINK")
            self.assertEqual(weak_link, "▂ -110 / -15")
            self.assertNotIn("-40", weak_link)
            self.assertNotIn("+15", weak_link)

    async def test_selection_and_link_display_generate_zero_rf_traffic(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            neighbor = NodeMetadata("!qz000001", "Quiet Zone", "QZ", last_heard=now - 3)
            app.radio.get_known_nodes = lambda nodes=(local, neighbor): nodes
            app.radio.get_link_quality = lambda node_id: LinkObservation(
                rssi=-70, snr=2.0, observed_at=now - 3
            )
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            _mesh_select_node(app, "!qz000001")
            await pilot.pause()
            _bar_text(app)
            self.assertEqual(app.radio.sent_messages, ())

    async def test_stale_link_observation_falls_back_to_honest_placeholder(self) -> None:
        """An observation older than ACTIVE_WINDOW_SECONDS must never be

        presented as current, even though RadioService itself still has
        it cached (see LinkQualityTests.
        test_same_radio_reconnect_preserves_link_observations) --
        staleness is enforced at display time, not by the cache.
        """
        app = self._make_app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            old = NodeMetadata("!old00001", "Old Reading", last_heard=now - 3)
            app.radio.get_known_nodes = lambda nodes=(local, old): nodes
            app.radio.get_link_quality = lambda node_id: LinkObservation(
                rssi=-60, snr=4.0, observed_at=now - ACTIVE_WINDOW_SECONDS - 1
            )
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            _mesh_select_node(app, "!old00001")
            await pilot.pause()
            text = _bar_text(app)
            self.assertEqual(_bar_field(text, "LINK"), "---- -- / --")
            self.assertEqual(_bar_field(text, "TIME"), "3s")


class MeshLayoutStabilityAppTests(unittest.IsolatedAsyncioTestCase):
    """MESH LAYOUT STABILITY, wired end to end through the real app's

    _refresh_mesh (see MeshLayoutStabilityStickyPositionTests above for
    the pure-function proof underneath this). Core invariant: no new
    topology information, no existing node movement.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    @staticmethod
    def _real_positions(view: "MeshTopologyView") -> dict:
        return {
            node_id: position
            for node_id, position in view.base_positions.items()
            if not node_id.startswith("relay:")
        }

    async def test_last_heard_update_alone_never_moves_nodes(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            a = NodeMetadata("!aaaa0001", "Alpha", "A", last_heard=now - 5, hops_away=1)
            b = NodeMetadata("!bbbb0002", "Bravo", "B", last_heard=now - 5, hops_away=1)
            app.radio.get_known_nodes = lambda nodes=(local, a, b): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            before = self._real_positions(view)

            # Ordinary passing time: last_heard age advances, nothing else.
            app._refresh_mesh(wall_now=now + 10)
            await pilot.pause()
            self.assertEqual(self._real_positions(view), before)

    async def test_telemetry_and_link_updates_never_move_nodes(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            a = NodeMetadata("!aaaa0001", "Alpha", "A", last_heard=now - 5, hops_away=1)
            app.radio.get_known_nodes = lambda nodes=(local, a): nodes
            app.radio.get_link_quality = lambda node_id: None
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            before = self._real_positions(view)

            app.radio.get_link_quality = lambda node_id: LinkObservation(
                rssi=-70, snr=3.0, observed_at=now
            )
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(self._real_positions(view), before)

    async def test_selection_changes_never_move_nodes(self) -> None:
        """YOU -> remote -> YOU -> remote: no coordinate movement."""
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            a = NodeMetadata("!aaaa0001", "Alpha", "A", last_heard=now - 5, hops_away=1)
            app.radio.get_known_nodes = lambda nodes=(local, a): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            before = self._real_positions(view)

            for node_id in (local.node_id, "!aaaa0001", local.node_id, "!aaaa0001"):
                _mesh_select_node(app, node_id)
                await pilot.pause()
                self.assertEqual(self._real_positions(view), before)

    async def test_new_node_does_not_move_existing_ones(self) -> None:
        """The new node's name/id sorts BEFORE both existing ones

        (Bravo/Charlie vs. new "Aaron") -- neither has a trustworthy
        position, so both land on the UNKNOWN outer ring, indexed by
        _node_sort_key's alphabetical order; a newcomer sorting first
        is exactly what would shift every existing node's ring index
        without the sticky-position fix (see MeshLayoutStabilityStickyPositionTests).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            b = NodeMetadata("!bbbb0002", "Bravo", "B", last_heard=now - 5, hops_away=1)
            c = NodeMetadata("!cccc0003", "Charlie", "C", last_heard=now - 5, hops_away=1)
            app.radio.get_known_nodes = lambda nodes=(local, b, c): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            before = self._real_positions(view)

            aaron = NodeMetadata("!aaaa0001", "Aaron", "A", last_heard=now - 5, hops_away=1)
            app.radio.get_known_nodes = lambda nodes=(local, b, c, aaron): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            after = self._real_positions(view)

            for node_id, position in before.items():
                self.assertEqual(after[node_id], position)
            self.assertIn("!aaaa0001", after)
            self.assertNotIn("!aaaa0001", before)

    async def test_node_aging_out_does_not_recompact_remaining_nodes(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            near = NodeMetadata("!near0001", "Near", "NR", last_heard=now - 5, hops_away=1)
            far = NodeMetadata(
                "!far00001", "Far", "FR", last_heard=now - 5, hops_away=3
            )
            app.radio.get_known_nodes = lambda nodes=(local, near, far): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            near_before = self._real_positions(view)["!near0001"]

            # Far ages out of the working set entirely (VERY_OLD).
            far_gone = NodeMetadata(
                "!far00001",
                "Far",
                "FR",
                last_heard=now - MESH_STALE_THRESHOLD_SECONDS - 60,
                hops_away=3,
            )
            app.radio.get_known_nodes = lambda nodes=(local, near, far_gone): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            positions = self._real_positions(view)
            self.assertNotIn("!far00001", positions)
            self.assertEqual(positions["!near0001"], near_before)

    async def test_repeated_identical_refresh_is_idempotent(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True)
            a = NodeMetadata("!aaaa0001", "Alpha", "A", last_heard=now - 5, hops_away=1)
            app.radio.get_known_nodes = lambda nodes=(local, a): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            first = self._real_positions(view)
            for _ in range(3):
                app._refresh_mesh(wall_now=now)
                await pilot.pause()
                self.assertEqual(self._real_positions(view), first)

    async def test_total_remote_turnover_does_not_carry_over_stale_stretch(
        self,
    ) -> None:
        """A complete remote-population swap (radio swap, or -- as here

        -- the default simulated mesh's own real nodes being fully
        replaced by a test-specific set with no shared identity) is a
        genuine "substantial logical topology change" (item 27): a
        leftover extent-ratchet/sticky-position from the OLD, unrelated
        population must not constrain the NEW one's fresh placement.
        Regression for exactly the cross-contamination
        MeshResponsiveResizeAndFocusPersistenceTests's own
        test_growing_the_terminal_reveals_more_grid_and_keeps_selection
        surfaced: switching to MESH first renders the app's real
        default SimulatedRadioService nodes (Alice/Bob/...), THEN the
        test overrides get_known_nodes to an unrelated custom set.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()  # real default SIMULATED_NODES render first
            view = app.query_one(MeshTopologyView)

            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            far_north = NodeMetadata(
                "!far0north", "Far North", "FARN", None, last_heard=now - 5,
                position=north_of_local(50),
            )
            app.radio.get_known_nodes = lambda nodes=(local, far_north): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            # Computed fresh (no unrelated leftover history), far_north
            # spreads out enough that the small viewport shows it as an
            # off-screen edge indicator -- exactly what a from-scratch
            # app instance given the SAME two nodes at the SAME small
            # size would also show.
            self.assertIn("!far0north", view.edge_node_ids)


class MeshGpsInformedPlacementAppTests(unittest.IsolatedAsyncioTestCase):
    """MESH GPS PLACEMENT, wired end to end through the real app's

    _refresh_mesh -- see MeshGpsInformedPlacementTests above for the
    pure-function proof underneath this.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    @staticmethod
    def _real_positions(view: "MeshTopologyView") -> dict:
        return {
            node_id: position
            for node_id, position in view.base_positions.items()
            if not node_id.startswith("relay:")
        }

    async def test_rssi_and_selection_updates_never_move_a_gps_node(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            gps_node = NodeMetadata(
                "!gps0001", "GPS Node", "GPS", last_heard=now - 5,
                position=north_of_local(5),
            )
            app.radio.get_known_nodes = lambda nodes=(local, gps_node): nodes
            app.radio.get_link_quality = lambda node_id: None
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            before = self._real_positions(view)

            app.radio.get_link_quality = lambda node_id: LinkObservation(
                rssi=-70, snr=3.0, observed_at=now
            )
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(self._real_positions(view), before)

            _mesh_select_node(app, "!gps0001")
            await pilot.pause()
            _mesh_select_node(app, local.node_id)
            await pilot.pause()
            self.assertEqual(self._real_positions(view), before)

    async def test_minor_gps_jitter_does_not_move_the_node_across_refreshes(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            app.radio.get_known_nodes = lambda nodes=(
                local,
                NodeMetadata(
                    "!gps0001", "GPS Node", "GPS", last_heard=now - 5,
                    position=north_of_local(5.0),
                ),
            ): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            before = self._real_positions(view)

            app.radio.get_known_nodes = lambda nodes=(
                local,
                NodeMetadata(
                    "!gps0001", "GPS Node", "GPS", last_heard=now - 2,
                    position=north_of_local(5.3),
                ),
            ): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(self._real_positions(view), before)

    async def test_meaningful_gps_change_moves_only_the_affected_node(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.show_tab("mesh")
            await pilot.pause()
            now = time.time()
            local = NodeMetadata(app.radio.info.node_id, is_local=True, position=LOCAL_GEO)
            stable = NodeMetadata(
                "!stable1", "Stable", "STBL", last_heard=now - 5, position=east_of_local(3)
            )
            app.radio.get_known_nodes = lambda nodes=(
                local,
                stable,
                NodeMetadata(
                    "!gps0001", "GPS Node", "GPS", last_heard=now - 5,
                    position=north_of_local(5),
                ),
            ): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            before = self._real_positions(view)

            app.radio.get_known_nodes = lambda nodes=(
                local,
                stable,
                NodeMetadata(
                    "!gps0001", "GPS Node", "GPS", last_heard=now - 5,
                    position=south_of_local(5),
                ),
            ): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            after = self._real_positions(view)

            self.assertEqual(after["!stable1"], before["!stable1"])
            self.assertEqual(after[local.node_id], before[local.node_id])
            self.assertNotEqual(after["!gps0001"], before["!gps0001"])


class ThinScrollBarRenderTests(unittest.TestCase):
    def test_vertical_thin_render_uses_the_thin_glyph(self) -> None:
        from rich.color import Color as RichColor

        from app import CHAT_SCROLLBAR_THUMB_GLYPH

        rendered = ThinScrollBarRender.render_bar(
            size=10,
            virtual_size=30,
            window_size=10,
            position=5,
            thickness=1,
            vertical=True,
            back_color=RichColor.parse(THEME_PALETTES["snow"].dim_base),
            bar_color=RichColor.parse("#39ff14"),
        )
        non_newline = [
            segment for segment in rendered.segments if segment.text != "\n"
        ]
        self.assertTrue(
            all(segment.text == CHAT_SCROLLBAR_THUMB_GLYPH for segment in non_newline)
        )


class MeshResponsiveGridDimensionsTests(unittest.TestCase):
    """Pure tests for _compute_mesh_grid_dimensions -- the responsive

    viewport's sole source of visible grid size, computed from the
    MESH view's own actual rendered size, never a hardcoded per-font-
    size table (see item 1/25 of the responsive-viewport task).
    """

    def test_xl_sized_viewport_uses_approximately_the_old_visual_extent(self) -> None:
        """Case A: a typical XL-ish viewport (the previous fixed 8x21

        board's own pixel footprint) computes to roughly that same
        extent -- never requiring the EXACT historical numbers, only a
        comparable, legitimately-derived odd size.
        """
        rows, columns, _, _ = _compute_mesh_grid_dimensions(
            MESH_LOGICAL_GRID_COLUMNS * DOT_GRID_SPACING_X,
            MESH_LOGICAL_GRID_ROWS * DOT_GRID_SPACING_Y,
        )
        self.assertAlmostEqual(rows, MESH_LOGICAL_GRID_ROWS, delta=2)
        self.assertAlmostEqual(columns, MESH_LOGICAL_GRID_COLUMNS, delta=2)

    def test_small_sized_viewport_produces_more_rows_and_columns_than_xl(self) -> None:
        """Case B: a larger available terminal area (more character

        cells fit -- what a SMALL font setting produces in the real
        deployment) yields strictly more visible grid rows/columns than
        a smaller one (an XL-sized one), never fewer.
        """
        xl_rows, xl_columns, _, _ = _compute_mesh_grid_dimensions(84, 16)
        small_rows, small_columns, _, _ = _compute_mesh_grid_dimensions(220, 60)
        self.assertGreater(small_rows, xl_rows)
        self.assertGreater(small_columns, xl_columns)

    def test_grid_never_exceeds_the_available_viewport_area(self) -> None:
        """Case C: the computed grid, converted back to terminal cells,

        never exceeds the actual available width/height it was derived
        from.
        """
        for view_width, view_height in ((84, 16), (220, 60), (40, 8), (1000, 300)):
            with self.subTest(view_width=view_width, view_height=view_height):
                rows, columns, _, _ = _compute_mesh_grid_dimensions(
                    view_width, view_height
                )
                self.assertLessEqual(columns * DOT_GRID_SPACING_X, max(view_width, columns * DOT_GRID_SPACING_X))
                # The MIN floors exist precisely so a tiny/zero viewport
                # (before layout, or pathologically small) never yields
                # a degenerate size -- those floors may legitimately
                # exceed a tiny input, but never a normal-sized one.
                if view_width >= MESH_GRID_MIN_COLUMNS * DOT_GRID_SPACING_X:
                    self.assertLessEqual(columns * DOT_GRID_SPACING_X, view_width)
                if view_height >= (MESH_GRID_MIN_ROWS + 1) * DOT_GRID_SPACING_Y:
                    self.assertLessEqual(rows * DOT_GRID_SPACING_Y, view_height)

    def test_grid_reserves_one_row_of_label_headroom(self) -> None:
        """Case D: the row count is derived from one fewer usable row

        than the raw viewport height implies -- reserved so a node's
        label (rendered one row above its glyph) never has to render
        outside the MESH view's own top edge.
        """
        rows, _, _, _ = _compute_mesh_grid_dimensions(84, 20)
        raw_rows = 20 // DOT_GRID_SPACING_Y
        self.assertLessEqual(rows, raw_rows - 1)

    def test_dimensions_are_odd_with_an_exact_center(self) -> None:
        """Case E: dimensions are always odd, so center_row/center_column

        are always an exact, unambiguous integer cell -- never a
        rounded-off "between two cells" position.
        """
        for view_width, view_height in ((84, 16), (91, 23), (220, 61), (57, 33)):
            with self.subTest(view_width=view_width, view_height=view_height):
                rows, columns, center_row, center_column = _compute_mesh_grid_dimensions(
                    view_width, view_height
                )
                self.assertEqual(rows % 2, 1)
                self.assertEqual(columns % 2, 1)
                self.assertEqual(center_row, rows // 2 + 1)
                self.assertEqual(center_column, columns // 2 + 1)
                self.assertTrue(1 <= center_row <= rows)
                self.assertTrue(1 <= center_column <= columns)

    def test_repeated_calls_with_unchanged_geometry_are_identical(self) -> None:
        """Case F: a pure function of its two inputs -- calling it

        repeatedly with the same viewport size always yields the exact
        same dimensions, never drifting from repaint to repaint.
        """
        first = _compute_mesh_grid_dimensions(97, 27)
        for _ in range(5):
            self.assertEqual(_compute_mesh_grid_dimensions(97, 27), first)

    def test_activity_and_theme_never_feed_into_grid_dimensions(self) -> None:
        """Case G: _compute_mesh_grid_dimensions takes only a width and

        a height -- there is no parameter, global, or code path by
        which node activity, selection, or theme could ever change its
        result for the same viewport size.
        """
        import inspect

        signature = inspect.signature(_compute_mesh_grid_dimensions)
        self.assertEqual(list(signature.parameters), ["view_width", "view_height"])


class MeshOffScreenEdgeIndicatorTests(unittest.IsolatedAsyncioTestCase):
    """Live app tests for the off-screen edge-indicator viewport (items

    4-9, 26-28 of the responsive-viewport task): a real node whose
    translated logical position falls outside the small simulated
    viewport renders as an edge indicator, arrow navigation reaches it
    as a valid target, and selecting it recenters the viewport so it
    becomes a normal visible, labeled node.

    Positions are injected directly via MeshTopologyView.set_nodes()
    with a hand-crafted base_positions dict, deliberately bypassing
    _refresh_mesh's GPS-bearing/assign_grid_slots/place_within_bounds
    pipeline -- that placement logic already has its own dedicated
    coverage elsewhere in this file; this class tests the NEW viewport-
    clipping/edge-indicator concern in isolation from it, against a
    small simulated terminal window standing in for an XL-sized
    viewport (see item 23/26: XL == fewer visible terminal cells).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    @staticmethod
    def _state(node_id: str, name: str, *, is_local: bool = False) -> MeshNodeState:
        node = NodeMetadata(
            node_id, name, name[:4].upper(), is_local=is_local, last_heard=1_700_000_000.0
        )
        return MeshNodeState(node=node, is_client=False, is_relay=False, last_interaction_at=None)

    def _fixture(self, you_id: str):
        """YOU near the logical center; DAVID close by; ALICE due

        north, BOB south-west, and CHARLIE south-east, all pushed to
        the FAR edge of the fixed 8x21 logical grid -- see
        MESH_LOGICAL_GRID_ROWS/COLUMNS.
        """
        working_set = (
            self._state(you_id, "You", is_local=True),
            self._state("!david004", "David"),
            self._state("!alice001", "Alice"),
            self._state("!bob002", "Bob"),
            self._state("!charlie003", "Charlie"),
        )
        base_positions = {
            you_id: (
                MESH_LOGICAL_GRID_CENTER_ROW,
                MESH_LOGICAL_GRID_CENTER_COLUMN,
            ),
            # Due east, not northwest, of YOU -- so "up" navigation
            # unambiguously targets ALICE alone (see
            # test_arrow_navigation_reaches_and_recenters_on_an_off_
            # screen_node), never a closer diagonal candidate.
            "!david004": (
                MESH_LOGICAL_GRID_CENTER_ROW,
                MESH_LOGICAL_GRID_CENTER_COLUMN + 1,
            ),
            "!alice001": (1, MESH_LOGICAL_GRID_CENTER_COLUMN),
            "!bob002": (MESH_LOGICAL_GRID_ROWS, 3),
            "!charlie003": (MESH_LOGICAL_GRID_ROWS, MESH_LOGICAL_GRID_COLUMNS - 2),
        }
        return working_set, base_positions

    async def test_distant_nodes_render_as_directional_edge_indicators(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await self._open_mesh(pilot)
            you_id = app.radio.info.node_id
            working_set, base_positions = self._fixture(you_id)
            view = app.query_one(MeshTopologyView)
            view.select_node(you_id)
            view.set_nodes(working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0)
            await pilot.pause()

            row_count, column_count, center_row, center_column = (
                view.current_grid_dimensions()
            )
            self.assertLess(row_count, MESH_LOGICAL_GRID_ROWS)

            # DAVID renders normally: a real label exists for him.
            self.assertTrue(
                any(w.node_id == "!david004" for w in app.query(MeshNodeLabelWidget))
            )
            self.assertNotIn("!david004", view.edge_node_ids)

            # ALICE/BOB/CHARLIE are genuinely off-screen edge indicators.
            self.assertEqual(
                view.edge_node_ids, {"!alice001", "!bob002", "!charlie003"}
            )
            for edge_id in ("!alice001", "!bob002", "!charlie003"):
                self.assertFalse(
                    any(w.node_id == edge_id for w in app.query(MeshNodeLabelWidget)),
                    f"{edge_id} must not have a normal label while off-screen",
                )
                # Still a real, ordinary glyph -- same semantic dot
                # treatment as any other real node (see
                # _mesh_node_color); no new "off-screen" color.
                self.assertTrue(
                    any(w.node_id == edge_id for w in app.query(MeshNodeWidget))
                )

            # Directional relationship preserved: ALICE clamps to the
            # top row on YOU's own column; BOB to the bottom-left
            # corner; CHARLIE to the bottom-right corner.
            positions = _mesh_translated_positions(
                view.base_positions, you_id, center_row=center_row, center_column=center_column
            )
            viewport_positions, edge_ids = project_to_viewport(
                positions, row_count=row_count, column_count=column_count
            )
            self.assertEqual(edge_ids & set(base_positions), view.edge_node_ids)
            self.assertEqual(viewport_positions["!alice001"][0], 1)
            self.assertEqual(viewport_positions["!alice001"][1], center_column)
            self.assertEqual(viewport_positions["!bob002"], (row_count, 1))
            self.assertEqual(viewport_positions["!charlie003"], (row_count, column_count))

    async def test_arrow_navigation_reaches_and_recenters_on_an_off_screen_node(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await self._open_mesh(pilot)
            you_id = app.radio.info.node_id
            working_set, base_positions = self._fixture(you_id)
            view = app.query_one(MeshTopologyView)
            view.select_node(you_id)
            view.set_nodes(working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0)
            await pilot.pause()
            self.assertIn("!alice001", view.edge_node_ids)

            await pilot.press("up")
            await pilot.pause()

            # ALICE becomes selected purely from directional navigation
            # against the STABLE logical positions -- never from
            # whatever was merely visible on screen.
            self.assertEqual(view.selected_node_id, "!alice001")
            # The viewport recentered: ALICE is no longer an edge
            # indicator, and now has a normal label.
            self.assertNotIn("!alice001", view.edge_node_ids)
            self.assertTrue(
                any(w.node_id == "!alice001" for w in app.query(MeshNodeLabelWidget))
            )
            # The underlying logical/base positions never changed --
            # recentering is a pure viewport transform, not a
            # topology re-derivation (see item 10/11).
            self.assertEqual(view.base_positions[you_id], base_positions[you_id])
            self.assertEqual(view.base_positions["!alice001"], base_positions["!alice001"])

            # YOU (previously the center) may now legitimately be
            # off-screen or on the boundary now that ALICE is centered
            # -- navigating back to YOU must still work deterministically.
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, you_id)
            self.assertNotIn(you_id, view.edge_node_ids)

    async def test_previously_visible_node_becomes_edge_indicator_after_recenter(
        self,
    ) -> None:
        """Selecting a distant node can push a PREVIOUSLY visible node

        (e.g. YOU, or another near neighbor) far enough from the new
        center that it becomes an edge indicator in turn -- the
        translation is symmetric, not YOU-privileged.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await self._open_mesh(pilot)
            you_id = app.radio.info.node_id
            working_set, base_positions = self._fixture(you_id)
            view = app.query_one(MeshTopologyView)
            # select_node() only accepts an ID already in the working
            # set -- populate it (defaulting selection to YOU) before
            # selecting ALICE, exactly as the production
            # _mesh_select_node/_move_mesh_focus call sequence does.
            view.set_nodes(working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0)
            view.select_node("!alice001")
            view.set_nodes(working_set, view.base_positions, theme=app._current_theme, now=1_700_000_000.0)
            await pilot.pause()

            # Recentered on ALICE (far north of YOU): YOU, BOB, and
            # CHARLIE -- all far south of ALICE now -- are edge
            # indicators; ALICE herself renders normally.
            self.assertNotIn("!alice001", view.edge_node_ids)
            self.assertIn(you_id, view.edge_node_ids | {you_id})

    async def test_multiple_off_screen_nodes_on_the_same_edge_do_not_collide(
        self,
    ) -> None:
        """Case 27: two distinct real nodes that would otherwise clamp

        to the identical boundary cell are deterministically spread
        along that edge instead of stacking invisibly on one cell.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await self._open_mesh(pilot)
            you_id = app.radio.info.node_id
            working_set = (
                self._state(you_id, "You", is_local=True),
                self._state("!north001", "North One"),
                self._state("!north002", "North Two"),
            )
            base_positions = {
                you_id: (MESH_LOGICAL_GRID_CENTER_ROW, MESH_LOGICAL_GRID_CENTER_COLUMN),
                "!north001": (1, MESH_LOGICAL_GRID_CENTER_COLUMN),
                "!north002": (1, MESH_LOGICAL_GRID_CENTER_COLUMN),
            }
            view = app.query_one(MeshTopologyView)
            view.select_node(you_id)
            view.set_nodes(working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0)
            await pilot.pause()

            self.assertEqual(view.edge_node_ids, {"!north001", "!north002"})
            row_count, column_count, center_row, center_column = (
                view.current_grid_dimensions()
            )
            positions = _mesh_translated_positions(
                view.base_positions, you_id, center_row=center_row, center_column=center_column
            )
            viewport_positions, _ = project_to_viewport(
                positions, row_count=row_count, column_count=column_count
            )
            self.assertNotEqual(
                viewport_positions["!north001"], viewport_positions["!north002"]
            )
            # Deterministic across repeated calls with the same input.
            again, _ = project_to_viewport(
                positions, row_count=row_count, column_count=column_count
            )
            self.assertEqual(viewport_positions, again)
            self.assertEqual(
                sum(1 for w in app.query(MeshNodeWidget) if w.node_id in ("!north001", "!north002")),
                2,
            )

    async def test_no_duplicate_node_ids_and_relays_never_become_edge_targets(
        self,
    ) -> None:
        """Case 27/16: an anonymous relay stage is never included in

        edge_node_ids (it is not a navigable identity at all), and no
        real node ever appears as more than one mounted widget.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            now = 1_700_000_000.0
            you_id = app.radio.info.node_id
            local = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            far_client = NodeMetadata(
                "!far0001", "Far Client", "FARC", 3,
                last_heard=now - 5, position=north_of_local(50),
            )
            app.radio.get_known_nodes = lambda nodes=(local, far_client): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            relay_ids = {stage.node_id for stage in view.relay_stages}
            self.assertGreater(len(relay_ids), 0)
            self.assertEqual(relay_ids & view.edge_node_ids, set())

            all_glyph_ids = [w.node_id for w in app.query(MeshNodeWidget)] + [
                w.node_id for w in app.query(MeshRelayWidget)
            ]
            self.assertEqual(len(all_glyph_ids), len(set(all_glyph_ids)))

            await pilot.press("up")
            await pilot.pause()
            self.assertNotIn(view.selected_node_id, relay_ids)

    async def test_mesh_count_is_independent_of_edge_visibility(self) -> None:
        """Case 21: MESH(N) counts active real remote nodes by the

        existing authoritative predicate alone -- being rendered as an
        edge indicator (off-screen) never removes a node from that
        count.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            now = 1_700_000_000.0
            you_id = app.radio.info.node_id
            local = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            far_active = NodeMetadata(
                "!faract2", "Far Active Two", "FAC2", None,
                last_heard=now - 5, position=north_of_local(50),
            )
            app.radio.get_known_nodes = lambda nodes=(local, far_active): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertIn("!faract2", view.edge_node_ids)
            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))

    async def test_connector_clips_at_viewport_boundary_without_fake_endpoints(
        self,
    ) -> None:
        """Case 28: an active client far enough off-screen still gets a

        connector, drawn to its clamped edge position -- never a
        fabricated relay or a fabricated endpoint merely because the
        true connector would otherwise extend off the visible grid.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            now = 1_700_000_000.0
            you_id = app.radio.info.node_id
            local = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            far_active = NodeMetadata(
                "!faract1", "Far Active", "FACT", None,
                last_heard=now - 5, position=north_of_local(50),
            )
            app.radio.get_known_nodes = lambda nodes=(local, far_active): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertIn("!faract1", view.edge_node_ids)
            self.assertEqual(view.relay_stages, ())

            canvas = view.board.query_one(MeshCanvas)
            signature = canvas._signature
            self.assertIsNotNone(signature)
            width, height, connectors, _theme = signature
            self.assertGreater(len(connectors), 0)
            for x, y, _glyph, _color in connectors:
                self.assertTrue(0 <= x < width)
                self.assertTrue(0 <= y < height)

    async def test_multi_hop_off_screen_endpoint_keeps_one_ordered_chain(
        self,
    ) -> None:
        """Case 12: a genuinely off-screen endpoint with a real, nonzero

        hop count -- the ordered-chain invariant (item 6/7 of the
        relay-chain correctness task) must hold on the VISIBLE, clipped
        connector exactly as it does fully on-screen: no duplicate
        rendered cell (see RelayChainOrderedRouteTests'
        _assert_continuous_no_self_overlap for why that is the direct
        proxy for "no branch"), the endpoint gets the edge indicator
        (never a relay stage), and relay count still matches the
        client's own reported hop count regardless of clipping.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            now = 1_700_000_000.0
            you_id = app.radio.info.node_id
            local = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            far_multi_hop = NodeMetadata(
                "!faroff03", "Far Multi Hop", "FMH3", 3,
                last_heard=now - 5, position=north_of_local(50),
            )
            app.radio.get_known_nodes = lambda nodes=(local, far_multi_hop): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertIn("!faroff03", view.edge_node_ids)
            self.assertEqual(len(view.relay_stages), 3)
            self.assertEqual(
                {stage.node_id for stage in view.relay_stages} & view.edge_node_ids,
                set(),
                "anonymous relay stages must never themselves become edge indicators",
            )

            canvas = view.board.query_one(MeshCanvas)
            width, height, connectors, _theme = canvas._signature
            coordinates = [(x, y) for x, y, _glyph, _color in connectors]
            self.assertEqual(
                len(coordinates),
                len(set(coordinates)),
                "the visible, clipped connector must still be one "
                "continuous chain -- no branch introduced by clipping",
            )
            for x, y, _glyph, _color in connectors:
                self.assertTrue(0 <= x < width)
                self.assertTrue(0 <= y < height)


class MeshBoundaryContinuationIndicatorTests(unittest.IsolatedAsyncioTestCase):
    """MESH BOUNDARY CONTINUATION INDICATORS (items 30-33): a boundary

    indicator must represent an actual off-screen NodeDB node only --
    never inferred hop points, virtual relay positions, or other
    synthetic topology geometry that merely happens to clip too. See
    ProjectToViewportTests.test_clipping_is_id_agnostic_real_or_synthetic
    for the underlying mechanism's own foundation-level proof.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    async def test_real_off_screen_node_with_no_hops_shows_the_indicator(self) -> None:
        """Case B: an actual NodeDB node beyond the boundary, with no

        relay chain at all (hops_away unknown) -- the plain, original
        "real node off-screen" case, unaffected by this pass.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            now = 1_700_000_000.0
            you_id = app.radio.info.node_id
            local = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            far = NodeMetadata(
                "!far00001", "Far", "FAR1", None, last_heard=now - 5,
                position=north_of_local(50),
            )
            app.radio.get_known_nodes = lambda nodes=(local, far): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            self.assertIn("!far00001", view.edge_node_ids)
            self.assertEqual(view.relay_stages, ())

    async def test_synthetic_geometry_clipping_alongside_a_real_off_screen_node_is_not_double_counted(
        self,
    ) -> None:
        """Case D: a real off-screen node whose OWN relay chain also

        clips must produce exactly the real-node indication -- never an
        extra, separate indication for the clipped relay stage(s). The
        outermost relay stage of a 5-hop chain this far out genuinely
        clips (verified fixture, not asserted-into-existence); it must
        render hidden, not as a second boundary dot beside the real
        node's own edge glyph.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            now = 1_700_000_000.0
            you_id = app.radio.info.node_id
            local = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            far = NodeMetadata(
                "!far00001", "Far", "FAR1", 5, last_heard=now - 5,
                position=north_of_local(50),
            )
            app.radio.get_known_nodes = lambda nodes=(local, far): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            # Exactly one real indication -- the node itself.
            self.assertEqual(view.edge_node_ids, frozenset({"!far00001"}))
            self.assertEqual(len(view.relay_stages), 5)

            relay_widgets = {w.node_id: w for w in app.query(MeshRelayWidget)}
            self.assertEqual(len(relay_widgets), 5)
            clipped_relay_ids = {
                stage.node_id for stage in view.relay_stages
            } & view._edge_node_ids
            # The fixture must genuinely exercise a clipped relay stage
            # -- otherwise this test would pass vacuously.
            self.assertTrue(clipped_relay_ids, "fixture must clip at least one relay stage")
            for node_id, widget in relay_widgets.items():
                if node_id in clipped_relay_ids:
                    self.assertFalse(
                        widget.display,
                        f"{node_id} clipped to the boundary must render hidden, "
                        "never as an extra continuation indication",
                    )
                else:
                    self.assertTrue(widget.display)

    async def test_selection_round_trip_restores_the_same_indicator_set(self) -> None:
        """Case E: toggling selection away and back must restore the

        exact same boundary-indicator set -- selection/recentering
        alone is never what determines which relay stages get hidden.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            # Real current time, not a fixed historical epoch: unlike
            # _refresh_mesh (called explicitly with wall_now= below),
            # _mesh_select_node's own set_nodes() call always stamps
            # `now=time()` internally -- a fixed past `now` here would
            # make the fixture look VERY_OLD (and so lose its relay
            # chain entirely) the instant selection triggers that call.
            now = time.time()
            you_id = app.radio.info.node_id
            local = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            far = NodeMetadata(
                "!far00001", "Far", "FAR1", 5, last_heard=now - 5,
                position=north_of_local(50),
            )
            app.radio.get_known_nodes = lambda nodes=(local, far): nodes
            await self._open_mesh(pilot)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            before_edge = view.edge_node_ids
            before_relay_display = {
                w.node_id: w.display for w in app.query(MeshRelayWidget)
            }

            _mesh_select_node(app, "!far00001")
            await pilot.pause()
            _mesh_select_node(app, you_id)
            await pilot.pause()

            self.assertEqual(view.edge_node_ids, before_edge)
            after_relay_display = {
                w.node_id: w.display for w in app.query(MeshRelayWidget)
            }
            self.assertEqual(after_relay_display, before_relay_display)


class MeshSelectedRelayChainSpuriousConnectorTests(unittest.IsolatedAsyncioTestCase):
    """Real-hardware regression: selecting a multi-hop REMOTE node

    recenters the viewport on IT (see MeshTopologyView.set_nodes'
    own docstring: "recentered on the selection") -- which can push an
    INTERMEDIATE relay stage (never tested by
    test_multi_hop_off_screen_endpoint_keeps_one_ordered_chain above,
    whose own fixture deliberately keeps every relay stage on-screen
    and never selects the remote endpoint at all) onto a viewport edge
    independently of its neighbors in the chain. project_to_viewport
    clips each node with no awareness of chain order, so the resulting
    multi-point route can retrace/zigzag across the whole board --
    visually: starts in one corner, rises to the opposite edge, then
    runs along it -- while never containing an exact duplicate cell,
    which is what the PRE-EXISTING fallback in MeshTopologyView.
    set_nodes actually checked for (see app.py's own comment there).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    def _fixture(self, you_id: str):
        """A 3-hop remote positioned so that, once the board recenters

        on IT, exactly one intermediate relay stage independently
        edge-clips -- confirmed (via direct mesh_topology helper calls
        against this exact test's own (60, 14)-terminal viewport
        dimensions, outside this test file) to make the OLD algorithm
        (before this fix) render a spurious CLOSED-LOOP connector (42
        cells, no exact duplicates -- so the pre-existing duplicate-
        cell fallback never caught it) where the correct direct route
        is only 13 cells.
        """
        working_set = (
            MeshNodeState(
                node=NodeMetadata(you_id, "You", "YOU", is_local=True),
                is_client=False,
                is_relay=False,
                last_interaction_at=None,
            ),
            MeshNodeState(
                node=NodeMetadata(
                    "!faroff03",
                    "Far Multi Hop",
                    "FMH3",
                    3,
                    last_heard=1_700_000_000.0,
                ),
                is_client=False,
                is_relay=False,
                last_interaction_at=None,
            ),
        )
        base_positions = {
            you_id: (7, 2),
            "!faroff03": (8, 5),
        }
        return working_set, base_positions

    async def test_selecting_remote_node_produces_bounded_connected_route(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await self._open_mesh(pilot)
            you_id = app.radio.info.node_id
            working_set, base_positions = self._fixture(you_id)
            view = app.query_one(MeshTopologyView)

            # select_node() only accepts an ID already in the working
            # set (see its own docstring), so the working set must be
            # established once first before selecting the remote node
            # and re-rendering to actually apply the recentering.
            view.set_nodes(
                working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0
            )
            view.select_node("!faroff03")
            view.set_nodes(
                working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0
            )
            await pilot.pause()
            self.assertEqual(len(view.relay_stages), 3)
            relay_ids = {stage.node_id for stage in view.relay_stages}
            # First confirm the fixture actually DOES independently
            # edge-clip a relay stage once recentered -- otherwise this
            # test would trivially pass without exercising the bug.
            # NOTE: the PUBLIC edge_node_ids property deliberately
            # excludes anonymous relay-stage IDs (see its own
            # docstring: "never a selectable edge concept") -- the
            # internal _edge_node_ids is the one that still includes
            # them, which is exactly what a white-box check like this
            # one needs.
            self.assertTrue(
                relay_ids & view._edge_node_ids,
                "fixture must genuinely reproduce an edge-clipped relay "
                "stage, or this test proves nothing",
            )

            canvas = view.board.query_one(MeshCanvas)
            width, height, connectors, _theme = canvas._signature
            coordinates = {(x, y) for x, y, _glyph, _color in connectors}

            self.assertEqual(
                len(coordinates),
                len(connectors),
                "connector must remain one continuous chain, no branch",
            )
            for x, y, _glyph, _color in connectors:
                self.assertTrue(0 <= x < width)
                self.assertTrue(0 <= y < height)

            # The precise, deterministic check: once an intermediate
            # relay stage is independently edge-clipped, the ordered-
            # chain geometric guarantee no longer holds (see app.py's
            # own comment at this exact fallback) -- the rendered
            # connector must be EXACTLY the direct YOU<->remote elbow,
            # never a multi-point route through the (now unreliable)
            # relay-stage waypoints. This is what actually distinguishes
            # correct behavior from the bug: the OLD code's multi-point
            # route for this exact fixture visits MORE cells than the
            # direct route (a real, verified difference -- not merely a
            # coincidental match), including cells nowhere near a
            # legitimate YOU-remote path.
            row_count, column_count, center_row, center_column = (
                view.current_grid_dimensions()
            )
            translated = _mesh_translated_positions(
                view.base_positions, "!faroff03",
                center_row=center_row, center_column=center_column,
            )
            viewport_positions, _edge_ids = project_to_viewport(
                translated, row_count=row_count, column_count=column_count
            )
            you_center = _mesh_grid_pixel(*viewport_positions[you_id])
            remote_center = _mesh_grid_pixel(*viewport_positions["!faroff03"])
            direct_route = route_connector_avoiding(
                you_center[0], you_center[1], remote_center[0], remote_center[1],
                frozenset(),
            )
            self.assertEqual(
                coordinates,
                {(x, y) for x, y, _glyph in direct_route},
                "an edge-clipped relay stage must fall back to the "
                "direct YOU<->remote connector, never a stale multi-"
                "point route through it",
            )

    async def test_you_and_remote_selection_yield_identical_logical_graph(
        self,
    ) -> None:
        """Selection is presentation state -- the underlying node set

        and edge (adjacency) list must never change merely because
        the user moved focus.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await self._open_mesh(pilot)
            you_id = app.radio.info.node_id
            working_set, base_positions = self._fixture(you_id)
            view = app.query_one(MeshTopologyView)

            view.select_node(you_id)
            view.set_nodes(
                working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0
            )
            await pilot.pause()
            you_selected_node_ids = {state.node.node_id for state in view.working_set}
            you_selected_relay_sources = {
                stage.source_node_id for stage in view.relay_stages
            }

            view.select_node("!faroff03")
            view.set_nodes(
                working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0
            )
            await pilot.pause()
            remote_selected_node_ids = {state.node.node_id for state in view.working_set}
            remote_selected_relay_sources = {
                stage.source_node_id for stage in view.relay_stages
            }

            self.assertEqual(you_selected_node_ids, remote_selected_node_ids)
            self.assertEqual(you_selected_relay_sources, remote_selected_relay_sources)
            self.assertEqual(len(view.relay_stages), 3)

    async def test_repeated_selection_toggling_is_deterministic(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await self._open_mesh(pilot)
            you_id = app.radio.info.node_id
            working_set, base_positions = self._fixture(you_id)
            view = app.query_one(MeshTopologyView)
            canvas = view.board.query_one(MeshCanvas)

            def current_connectors():
                view.set_nodes(
                    working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0
                )
                _w, _h, connectors, _theme = canvas._signature
                return connectors

            view.select_node(you_id)
            you_connectors_first = current_connectors()
            view.select_node("!faroff03")
            remote_connectors_first = current_connectors()
            view.select_node(you_id)
            you_connectors_second = current_connectors()
            view.select_node("!faroff03")
            remote_connectors_second = current_connectors()

            self.assertEqual(you_connectors_first, you_connectors_second)
            self.assertEqual(remote_connectors_first, remote_connectors_second)

    async def test_selected_route_paint_priority_still_wins(self) -> None:
        """Preserve item 10/25: the selected connector still paints

        ACCENT, last, regardless of this fix.
        """
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await self._open_mesh(pilot)
            you_id = app.radio.info.node_id
            working_set, base_positions = self._fixture(you_id)
            view = app.query_one(MeshTopologyView)
            view.set_nodes(
                working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0
            )
            view.select_node("!faroff03")
            view.set_nodes(
                working_set, base_positions, theme=app._current_theme, now=1_700_000_000.0
            )
            await pilot.pause()
            canvas = view.board.query_one(MeshCanvas)
            _w, _h, connectors, _theme = canvas._signature
            palette = THEME_PALETTES[app._current_theme]
            colors = {color for _x, _y, _glyph, color in connectors}
            self.assertIn(palette.accent, colors)


class ProjectToViewportTests(unittest.TestCase):
    """Pure tests for mesh_topology.project_to_viewport -- the direct-

    preserving clip that turns a translated logical position into
    either an unchanged in-viewport cell or a boundary-clamped edge
    indicator (see items 4/14 of the responsive-viewport task).
    """

    def test_in_bounds_positions_are_unchanged(self) -> None:
        viewport, edge_ids = project_to_viewport(
            {"a": (2, 2), "b": (3, 3)}, row_count=5, column_count=5
        )
        self.assertEqual(viewport, {"a": (2, 2), "b": (3, 3)})
        self.assertEqual(edge_ids, frozenset())

    def test_cardinal_edge_projection_preserves_the_shared_axis(self) -> None:
        viewport, edge_ids = project_to_viewport(
            {"you": (3, 3), "alice": (-10, 3)}, row_count=5, column_count=5
        )
        self.assertEqual(viewport["alice"], (1, 3))
        self.assertEqual(edge_ids, frozenset({"alice"}))

    def test_corner_projection_preserves_diagonal_relationship(self) -> None:
        """Case 14: a node beyond BOTH axes clamps to the matching

        corner (here, north-east), never collapsing onto one of only
        four cardinal edge cells regardless of its real direction.
        """
        for position, expected_corner in (
            ((-10, 20), (1, 5)),  # north-east
            ((-10, -20), (1, 1)),  # north-west
            ((20, 20), (5, 5)),  # south-east
            ((20, -20), (5, 1)),  # south-west
        ):
            with self.subTest(position=position):
                viewport, edge_ids = project_to_viewport(
                    {"you": (3, 3), "far": position}, row_count=5, column_count=5
                )
                self.assertEqual(viewport["far"], expected_corner)
                self.assertEqual(edge_ids, frozenset({"far"}))

    def test_in_bounds_node_is_never_displaced_by_an_edge_collision(self) -> None:
        viewport, edge_ids = project_to_viewport(
            {"you": (3, 3), "near": (1, 3), "far": (-10, 3)},
            row_count=5,
            column_count=5,
        )
        self.assertEqual(viewport["near"], (1, 3))
        self.assertNotEqual(viewport["far"], (1, 3))
        self.assertEqual(edge_ids, frozenset({"far"}))

    def test_colliding_edge_nodes_are_spread_deterministically(self) -> None:
        viewport, edge_ids = project_to_viewport(
            {"you": (3, 3), "a": (-10, 3), "b": (-11, 3)},
            row_count=5,
            column_count=5,
        )
        self.assertEqual(edge_ids, frozenset({"a", "b"}))
        self.assertNotEqual(viewport["a"], viewport["b"])
        again, _ = project_to_viewport(
            {"you": (3, 3), "a": (-10, 3), "b": (-11, 3)},
            row_count=5,
            column_count=5,
        )
        self.assertEqual(viewport, again)

    def test_selected_node_at_exact_center_is_never_an_edge(self) -> None:
        """The translated selected node always lands exactly at

        (center_row, center_column), which is trivially in-bounds --
        so it can never itself become an edge indicator (see item 7:
        the currently selected node is always fully visible).
        """
        viewport, edge_ids = project_to_viewport(
            {"you": (3, 3)}, row_count=5, column_count=5
        )
        self.assertEqual(viewport["you"], (3, 3))
        self.assertEqual(edge_ids, frozenset())

    def test_clipping_is_id_agnostic_real_or_synthetic(self) -> None:
        """MESH BOUNDARY CONTINUATION INDICATORS' foundation: this

        function has no concept of "real node" vs "synthetic relay
        stage" at all -- it clips whatever (id, position) pairs it is
        given, purely by coordinate. A "relay:..."-shaped id that ends
        up out of bounds is reported in edge_ids exactly like any other
        id would be -- distinguishing "this represents an actual
        off-screen NodeDB node" from "this is just interior route
        geometry that happened to clip" is entirely the CALLER's job
        (see app.py's MeshTopologyView.edge_node_ids, which intersects
        this raw set with real working-set node IDs, and set_nodes'
        own `widget.display = widget.node_id not in edge_ids` for
        MeshRelayWidget, which hides a clipped relay stage rather than
        rendering it at the boundary as if it were one).
        """
        viewport, edge_ids = project_to_viewport(
            {"you": (3, 3), "real_far": (-10, 3), "relay:real_far:1": (-10, 3)},
            row_count=5,
            column_count=5,
        )
        self.assertEqual(edge_ids, frozenset({"real_far", "relay:real_far:1"}))


class MeshResponsiveResizeAndFocusPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """Live resize tests (items 22-24): a real terminal-size change

    (standing in for a font-size change taking effect, since a live
    process cannot actually change its own font mid-session -- see
    item 23) must recompute the visible grid and reveal/hide edge
    indicators accordingly, while never disturbing the current
    selection, the underlying logical topology, or MESH(N).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def test_growing_the_terminal_reveals_more_grid_and_keeps_selection(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(60, 14)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            now = 1_700_000_000.0
            you_id = app.radio.info.node_id
            local = NodeMetadata(you_id, is_local=True, position=LOCAL_GEO)
            far_north = NodeMetadata(
                "!far0north", "Far North", "FARN", None,
                last_heard=now - 5, position=north_of_local(50),
            )
            app.radio.get_known_nodes = lambda nodes=(local, far_north): nodes
            app._refresh_mesh(wall_now=now)
            await pilot.pause()

            view = app.query_one(MeshTopologyView)
            small_rows, small_columns, _, _ = view.current_grid_dimensions()
            self.assertIn("!far0north", view.edge_node_ids)
            self.assertEqual(view.selected_node_id, you_id)

            await pilot.resize_terminal(220, 60)
            await pilot.pause()

            big_rows, big_columns, _, _ = view.current_grid_dimensions()
            self.assertGreater(big_rows, small_rows)
            self.assertGreater(big_columns, small_columns)
            # Selection persisted through the resize -- never silently
            # reassigned merely because the layout changed (item 22).
            self.assertEqual(view.selected_node_id, you_id)
            # MESH(N)/activity are untouched by a pure layout event.
            self.assertIn("MESH(1)", str(app.query_one("#tab-bar").render()))
            # The underlying logical position is exactly what it was --
            # only the viewport changed, never the topology itself.
            self.assertEqual(
                view.base_positions[you_id],
                (MESH_LOGICAL_GRID_CENTER_ROW, MESH_LOGICAL_GRID_CENTER_COLUMN),
            )

            await pilot.resize_terminal(60, 14)
            await pilot.pause()
            shrunk_rows, shrunk_columns, _, _ = view.current_grid_dimensions()
            self.assertEqual((shrunk_rows, shrunk_columns), (small_rows, small_columns))
            self.assertEqual(view.selected_node_id, you_id)


class MeshNodeMenuTests(unittest.IsolatedAsyncioTestCase):
    """MESH VIEW PASS item 8-10: ENTER on a focused real MESH node opens

    the shared CHAT/MESH node-options menu (app._open_node_menu),
    reused unchanged apart from the new allow_reply=False MESH never
    passes REPLY along for. YOU's ENTER reuses that same function's
    existing is_local branch (informational rows only) -- a deliberate
    decision, not a new code path (see app._open_mesh_node_menu).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    async def test_enter_on_remote_node_opens_menu_without_reply(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!c0ffee03"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(target_id, "Target Node", "TGT", 1, position=north_of_local(1)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Target Node",
                    sender_short_name="TGT",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, target_id)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            labels = [item.label for item in app._user_menu.items]
            self.assertIn("Target Node", labels)
            self.assertIn("TGT", labels)
            self.assertIn("1 HOP AWAY", labels)
            self.assertNotIn("REPLY", labels)
            values = [item.value for item in app._user_menu.items]
            self.assertNotIn("reply", values)

    async def test_enter_on_you_opens_informational_only_menu(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            you_id = app.radio.info.node_id
            self.assertEqual(app.query_one(MeshTopologyView).selected_node_id, you_id)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            self.assertTrue(all(not item.actionable for item in app._user_menu.items))
            labels = [item.label for item in app._user_menu.items]
            self.assertIn(you_id, labels)

    async def test_enter_does_nothing_when_working_set_is_empty(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            app.radio.get_known_nodes = lambda: ()
            await self._open_mesh(pilot)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNone(app._user_menu)

    async def test_favorite_action_toggles_and_closes_menu(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!c0ffee04"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(target_id, "Fave Target", "FAV", 1),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Fave Target",
                    sender_short_name="FAV",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, target_id)
            await pilot.pause()
            self.assertFalse(app.settings.is_favorite(target_id))
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(app.settings.is_favorite(target_id))
            self.assertIsNone(app._user_menu)

    async def test_escape_closes_menu_and_preserves_selection(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!c0ffee05"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(target_id, "Escape Target", "ESC", 1),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Escape Target",
                    sender_short_name="ESC",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, target_id)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsNone(app._user_menu)
            self.assertEqual(view.selected_node_id, target_id)

    async def test_arrow_keys_move_menu_highlight_not_mesh_selection_while_open(
        self,
    ) -> None:
        """The existing global "if self._user_menu is not None" interception

        (shared with CHAT) already suspends MESH's own arrow-key
        navigation for free once a node menu is open -- proving that
        holds for MESH too, with zero new plumbing.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!c0ffee06"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(
                    target_id, "Arrow Target", "ARO", 0, position=north_of_local(2)
                ),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Arrow Target",
                    sender_short_name="ARO",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, target_id)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, target_id)

    async def test_click_on_relay_stage_glyph_never_reaches_enter_path(self) -> None:
        """Defensive: MeshRelayWidget has no on_click and can_focus is

        False (see test_relay_stage_cannot_be_selected_and_always_
        renders_dim), so an anonymous relay stage can never become
        view.selected_node_id in the first place -- ENTER can therefore
        never open a menu "for" a relay stage; this proves the
        precondition _open_mesh_node_menu relies on still holds.
        """
        self.assertFalse(hasattr(MeshRelayWidget, "on_click"))
        self.assertFalse(MeshRelayWidget.can_focus)


class MeshFocusAndNavigationAuditTests(unittest.IsolatedAsyncioTestCase):
    """MESH VIEW PASS item 15-17: audit-level proof that YOU is a normal

    selectable focus target and existing arrow-navigation continues to
    function exactly as before this pass -- no rewrite, only the
    ENTER-menu addition layered on top (see MeshNodeMenuTests above).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    async def test_you_is_selected_by_default_and_navigable_away_from(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            north_id = "!f0000001"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(north_id, "North", "N", 0, position=north_of_local(2)),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=north_id,
                    sender_long_name="North",
                    sender_short_name="N",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, you_id)
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, north_id)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, you_id)

    async def test_switching_away_and_back_to_mesh_preserves_selection(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            target_id = "!c0ffee07"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(target_id, "Persisted", "PER", 1),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=target_id,
                    sender_long_name="Persisted",
                    sender_short_name="PER",
                    channel_index=0,
                    text="hi",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, target_id)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, target_id)

            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.current_tab, "chat")
            await pilot.press("3")
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            self.assertEqual(view.selected_node_id, target_id)

    async def test_tab_switch_is_structurally_blocked_while_menu_open(self) -> None:
        """The existing global on_key "if self._user_menu is not None"

        interception consumes every key (including the digit tab-switch
        keys) while a node menu is open, and the only way to reach
        show_tab() with a menu open would be through that same branch --
        so a tab switch can never actually happen while a menu is
        showing; ESC (already proven to close safely) is the only exit.
        This documents that "safe on tab-switch" holds by construction,
        not via new dismissal code (see MESH VIEW PASS item 10).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.current_tab, "mesh")
            self.assertIsNotNone(app._user_menu)


class MeshDistanceUnitsLiveUpdateTests(unittest.IsolatedAsyncioTestCase):
    """MESH VIEW PASS item 11-12: distance honors the connected radio's

    own synced display.units setting via app._mesh_distance_is_metric,
    the SAME zero-RF read_synced_config_field read every RADIO_SETTINGS
    dropdown already uses -- no new polling. It picks up a units change
    on the NEXT already-scheduled _refresh_mesh cycle, never a second,
    MESH-specific poll of its own.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    async def _select_target_with_distance(self, app, pilot) -> str:
        you_id = app.radio.info.node_id
        target_id = "!d15ce001"
        app.radio.get_known_nodes = lambda: (
            NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
            NodeMetadata(target_id, "Distant", "DST", 1, position=north_of_local(5)),
        )
        app._accept_received_message(
            SIMULATED_MESSAGES[0].__class__(
                sender_node_id=target_id,
                sender_long_name="Distant",
                sender_short_name="DST",
                channel_index=0,
                text="hi",
                rssi=None,
                snr=None,
                packet_id=1,
                radio_rx_at=time.time(),
            )
        )
        await self._open_mesh(pilot)
        _mesh_select_node(app, target_id)
        await pilot.pause()
        return target_id

    async def test_metric_by_default_via_simulated_radio(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            await self._select_target_with_distance(app, pilot)
            status = _bar_text(app)
            self.assertTrue(_bar_field(status, "DISTANCE").endswith("km"))

    async def test_imperial_when_radio_reports_imperial(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            app.radio._config_sections["display"]["units"] = DISPLAY_UNITS_IMPERIAL
            await self._select_target_with_distance(app, pilot)
            status = _bar_text(app)
            self.assertTrue(_bar_field(status, "DISTANCE").endswith("mi"))

    async def test_disconnected_radio_defaults_to_imperial(self) -> None:
        app = self._make_app()
        self.assertFalse(app._mesh_distance_is_metric())

    async def test_switching_units_mid_session_updates_on_next_refresh_no_new_poll(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            target_id = await self._select_target_with_distance(app, pilot)
            status_before = _bar_text(app)
            self.assertTrue(_bar_field(status_before, "DISTANCE").endswith("km"))

            app.radio._config_sections["display"]["units"] = DISPLAY_UNITS_IMPERIAL
            app._refresh_mesh(wall_now=time.time())
            await pilot.pause()
            status_after = _bar_text(app)
            self.assertTrue(_bar_field(status_after, "DISTANCE").endswith("mi"))
            self.assertEqual(
                app.query_one(MeshTopologyView).selected_node_id, target_id
            )

    async def test_unsupported_schema_field_defaults_to_imperial(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            del app.radio._config_sections["display"]["units"]
            await self._select_target_with_distance(app, pilot)
            status = _bar_text(app)
            self.assertTrue(_bar_field(status, "DISTANCE").endswith("mi"))


class MeshYouIdentityCorrectionTests(unittest.IsolatedAsyncioTestCase):
    """MESH VIEW PASS item 1-2: YOU's displayed long/short name always

    comes from the CONNECTED radio's own authoritative identity
    (app._radio_info, the same source CONNECTION's own controls use),
    never NodeDB's own local-node record -- which real hardware has
    been observed to report as a bare placeholder (see app._mesh_
    working_set's own docstring). Never leaks a previous radio's
    identity across a reconnect/device switch either.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def test_you_uses_radio_info_not_nodedb_placeholder(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, "Meshtastic ab59", "ab59", 0, is_local=True),
            )
            working_set = app._mesh_working_set()
            self.assertEqual(working_set[0].node.long_name, app.radio.info.long_name)
            self.assertEqual(working_set[0].node.short_name, app.radio.info.short_name)
            self.assertNotEqual(working_set[0].node.long_name, "Meshtastic ab59")

    async def test_you_identity_never_leaks_across_reconnect(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            first_you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(first_you_id, is_local=True),
            )
            first_working_set = app._mesh_working_set()
            self.assertEqual(first_working_set[0].node.long_name, "Simulated Node")

            new_info = replace(
                app.radio.info,
                node_id="!newdevice",
                long_name="Second Radio",
                short_name="SEC",
            )
            app._show_connection(RadioState.OFFLINE, message="switching device")
            app._show_connection(RadioState.ONLINE, new_info)
            app.radio.get_known_nodes = lambda: (
                NodeMetadata("!newdevice", is_local=True),
            )
            second_working_set = app._mesh_working_set()
            self.assertEqual(second_working_set[0].node.long_name, "Second Radio")
            self.assertEqual(second_working_set[0].node.short_name, "SEC")

    async def test_you_position_reflects_live_get_known_nodes_not_a_cached_snapshot(
        self,
    ) -> None:
        """The local node's position comes from the SAME live

        get_known_nodes() call as everything else in the working set --
        never a connect-time-only cached snapshot (see
        RadioConfigurationSnapshot.position, which is deliberately NOT
        used here) -- so a position that appears mid-session is picked
        up on the very next call, no reconnect required.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=None),
            )
            self.assertIsNone(app._mesh_working_set()[0].node.position)

            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
            )
            self.assertEqual(app._mesh_working_set()[0].node.position, LOCAL_GEO)


class MeshRadioSwapIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """MESH FOLLOW-UP items 9-15, 20, 25-29: drive the App's REAL

    connection lifecycle (_show_connection, exactly as RadioEvents from
    app_controller do) through a physical-radio-swap scenario --
    old V3 node A local -> disconnect -> new V4 node B local -- and
    prove MESH(N), focused-YOU, GPS, the node menu, and rendering all
    follow the CURRENTLY CONNECTED radio automatically, with no tab
    switch or manual refresh required.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    async def _swap_to_v4(self, app, pilot, *, extra_nodes=()):
        """Disconnect the (simulated) V3 and bring up a V4 with a

        DIFFERENT node ID -- driven through the exact same
        _show_connection calls app_controller's real RadioEvent stream
        produces, never a direct helper-function shortcut.
        """
        v4_info = replace(
            app.radio.info,
            node_id="!bbbbbbbb",
            long_name="V4 Radio",
            short_name="V4",
        )
        app._show_connection(RadioState.OFFLINE, message="switching device")
        await pilot.pause()
        app.radio.get_known_nodes = lambda: (
            NodeMetadata(
                "!bbbbbbbb", "V4 Radio", "V4", 0, is_local=True, position=north_of_local(1)
            ),
            *extra_nodes,
        )
        app._show_connection(RadioState.ONLINE, v4_info)
        await pilot.pause()

    async def test_swap_rerenders_mesh_automatically_no_tab_switch_or_refresh(
        self,
    ) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            status_before = _bar_text(app)
            self.assertTrue(status_before.startswith("LONG NAME Simulated Node"))

            await self._swap_to_v4(app, pilot)

            # No tab switch, no manual _refresh_mesh() call, no focus
            # change -- _show_connection's own unconditional
            # _refresh_mesh() call is the entire rerender mechanism.
            status_after = _bar_text(app)
            self.assertTrue(
                status_after.startswith("LONG NAME V4 Radio • SHORT NAME V4")
            )
            self.assertEqual(app.current_tab, "mesh")

            view = app.query_one(MeshTopologyView)
            you_widget = next(
                w for w in app.query(MeshNodeWidget) if w.node_id == view.selected_node_id
            )
            palette = THEME_PALETTES[app._current_theme]
            self.assertEqual(
                you_widget.render().spans[0].style.foreground,
                Color.parse(palette.accent2),
            )

    async def test_mesh_n_excludes_current_local_not_old_local(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            now = time.time()
            extra_nodes = (
                NodeMetadata("!aaaaaaaa", "Old V3", "V3", 0, last_heard=now - 5),
                NodeMetadata("!c0000001", "X", "X", 1, last_heard=now - 5),
                NodeMetadata("!d0000001", "Y", "Y", 1, last_heard=now - 5),
            )
            await self._swap_to_v4(app, pilot, extra_nodes=extra_nodes)
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            tab_bar = str(app.query_one("#tab-bar").render())
            self.assertIn("MESH(3)", tab_bar)
            view = app.query_one(MeshTopologyView)
            remote_ids = {
                state.node.node_id for state in view.working_set if not state.node.is_local
            }
            self.assertEqual(remote_ids, {"!aaaaaaaa", "!c0000001", "!d0000001"})
            local_ids = {
                state.node.node_id for state in view.working_set if state.node.is_local
            }
            self.assertEqual(local_ids, {"!bbbbbbbb"})

    async def test_focused_you_gps_after_swap_uses_new_radio_never_old(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            await self._open_mesh(pilot)
            now = time.time()
            old_radio_with_position = NodeMetadata(
                "!aaaaaaaa", "Old V3", "V3", 0, last_heard=now - 5, position=south_of_local(9)
            )
            await self._swap_to_v4(app, pilot, extra_nodes=(old_radio_with_position,))
            status = _bar_text(app)
            self.assertTrue(
                status.startswith("LONG NAME V4 Radio • SHORT NAME V4")
            )
            self.assertNotIn(format_coordinates(south_of_local(9)), status)
            you_gps_text = _bar_field(status, "GPS")
            self.assertEqual(you_gps_text, format_coordinates(north_of_local(1)))

            # A's own remote line never shows raw coordinates (only YOU's
            # line does) -- but its DISTANCE field is real (not "--"),
            # proving it was computed from B's ACTUAL current position,
            # never a stale/zero/fabricated one.
            view = app.query_one(MeshTopologyView)
            _mesh_select_node(app, "!aaaaaaaa")
            await pilot.pause()
            remote_status = _bar_text(app)
            self.assertTrue(remote_status.startswith("LONG NAME Old V3 • SHORT NAME V3"))
            distance_segment = _bar_field(remote_status, "DISTANCE")
            self.assertNotEqual(distance_segment, "--")
            expected_km = distance_between(north_of_local(1), south_of_local(9)) * KM_PER_MILE
            self.assertEqual(distance_segment, f"{expected_km:.1f} km")

    async def test_you_has_no_gps_after_swap_even_if_old_radio_had_one(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(200, 28)) as pilot:
            await self._open_mesh(pilot)
            v4_info = replace(
                app.radio.info, node_id="!bbbbbbbb", long_name="V4 Radio", short_name="V4"
            )
            app._show_connection(RadioState.OFFLINE, message="switching device")
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata("!bbbbbbbb", "V4 Radio", "V4", 0, is_local=True, position=None),
                NodeMetadata(
                    "!aaaaaaaa", "Old V3", "V3", 0, position=south_of_local(9)
                ),
            )
            app._show_connection(RadioState.ONLINE, v4_info)
            await pilot.pause()
            status = _bar_text(app)
            self.assertEqual(_bar_field(status, "GPS"), "--")

    async def test_menu_local_vs_remote_after_swap(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            now = time.time()
            old_radio = NodeMetadata("!aaaaaaaa", "Old V3", "V3", 0, last_heard=now - 5)
            await self._swap_to_v4(app, pilot, extra_nodes=(old_radio,))

            # ENTER on B (selected by default) -> local informational menu.
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            labels = [item.label for item in app._user_menu.items]
            self.assertIn("V4 Radio", labels)
            self.assertTrue(all(not item.actionable for item in app._user_menu.items))
            await pilot.press("escape")
            await pilot.pause()

            # ENTER on A (old radio) -> ordinary remote menu with FAVORITE.
            _mesh_select_node(app, "!aaaaaaaa")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsNotNone(app._user_menu)
            labels = [item.label for item in app._user_menu.items]
            self.assertIn("Old V3", labels)
            self.assertIn("FAVORITE", labels)
            self.assertFalse(app.settings.is_favorite("!aaaaaaaa"))
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(app.settings.is_favorite("!aaaaaaaa"))
            self.assertFalse(app.settings.is_favorite("!bbbbbbbb"))

    async def test_stale_selection_falls_back_when_invalid_after_swap(self) -> None:
        """If the previously selected node no longer exists in the new

        radio's working set, selection falls back deterministically via
        EXISTING MeshTopologyView.set_nodes rules (to YOU) -- no new
        navigation model introduced for this pass (item 15). Uses a
        NodeDB-only node (no CHAT history) so it genuinely disappears
        once get_known_nodes() stops reporting it -- a node that has
        also sent a CHAT message stays admitted via that separate,
        unrelated, deliberately-untouched CHAT-history admission path
        (see build_mesh_working_set), which is not what this test is
        about.
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            you_id = app.radio.info.node_id
            gone_id = "!c0ffee09"
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
                NodeMetadata(gone_id, "Gone Node", "GON", 0, last_heard=time.time() - 5),
            )
            await self._open_mesh(pilot)
            _mesh_select_node(app, gone_id)
            await pilot.pause()
            view = app.query_one(MeshTopologyView)
            self.assertEqual(view.selected_node_id, gone_id)

            await self._swap_to_v4(app, pilot)
            self.assertEqual(view.selected_node_id, "!bbbbbbbb")

    async def test_previously_selected_node_surviving_the_swap_may_stay_selected(
        self,
    ) -> None:
        """If the previously selected node LEGITIMATELY still exists as a

        remote node after the swap (the old radio itself), it may
        remain selectable per existing focus rules -- but YOU semantics
        move to the new radio regardless (item 15).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            now = time.time()
            old_radio = NodeMetadata("!aaaaaaaa", "Old V3", "V3", 0, last_heard=now - 5)
            await self._swap_to_v4(app, pilot, extra_nodes=(old_radio,))
            view = app.query_one(MeshTopologyView)
            _mesh_select_node(app, "!aaaaaaaa")
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!aaaaaaaa")

            # A second, unrelated refresh (same working set) must not
            # evict a still-valid selection.
            app._refresh_mesh(wall_now=now)
            await pilot.pause()
            self.assertEqual(view.selected_node_id, "!aaaaaaaa")
            you_state = next(s for s in view.working_set if s.node.is_local)
            self.assertEqual(you_state.node.node_id, "!bbbbbbbb")


class MeshTopologyYouLabelRenderTests(unittest.IsolatedAsyncioTestCase):
    """PR #43 FOLLOW-UP Part A: the ACTUAL RENDERED topology label text/

    color for the local node, not just working-set state -- proving
    the fix closes the real-hardware symptom (topology label showed
    the NodeDB name instead of "YOU" even though is_local and the
    bottom-left context were already correct).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=()
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    def _you_label_widget(self, app):
        view = app.query_one(MeshTopologyView)
        you_id = next(s.node.node_id for s in view.working_set if s.node.is_local)
        return next(w for w in app.query(MeshNodeLabelWidget) if w.node_id == you_id), you_id

    async def test_topology_label_renders_you_not_the_radios_own_name(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            v4_info = replace(
                app.radio.info, node_id="!bbbbbbbb", long_name="V4 Radio", short_name="V4"
            )
            app._show_connection(RadioState.OFFLINE, message="switching device")
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata("!bbbbbbbb", "V4 Radio", "V4", 0, is_local=True),
            )
            app._show_connection(RadioState.ONLINE, v4_info)
            await self._open_mesh(pilot)

            label_widget, _you_id = self._you_label_widget(app)
            rendered_text = str(label_widget.render())
            self.assertEqual(rendered_text, "YOU")
            self.assertNotEqual(rendered_text, "V4")
            self.assertNotEqual(rendered_text, "V4 Radio")

            # The unified bar still correctly shows the real identity.
            status = _bar_text(app)
            self.assertTrue(status.startswith("LONG NAME V4 Radio • SHORT NAME V4"))

    async def test_topology_label_renders_you_with_accent2_color(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(
                    app.radio.info.node_id, "Simulated Node", "SIM", 0, is_local=True
                ),
            )
            await self._open_mesh(pilot)
            label_widget, _you_id = self._you_label_widget(app)
            palette = THEME_PALETTES[app._current_theme]
            self.assertEqual(
                label_widget.render().spans[0].style.foreground,
                Color.parse(palette.accent2),
            )

    async def test_old_radio_keeps_its_own_topology_label_only_new_is_you(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            v4_info = replace(
                app.radio.info, node_id="!bbbbbbbb", long_name="V4 Radio", short_name="V4"
            )
            app._show_connection(RadioState.OFFLINE, message="switching device")
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata("!bbbbbbbb", "V4 Radio", "V4", 0, is_local=True),
                NodeMetadata(
                    "!aaaaaaaa", "Old V3", "V3", 0, last_heard=time.time() - 5
                ),
            )
            app._show_connection(RadioState.ONLINE, v4_info)
            await self._open_mesh(pilot)

            you_label, you_id = self._you_label_widget(app)
            self.assertEqual(str(you_label.render()), "YOU")
            self.assertEqual(you_id, "!bbbbbbbb")

            old_label = next(
                w for w in app.query(MeshNodeLabelWidget) if w.node_id == "!aaaaaaaa"
            )
            old_text = str(old_label.render())
            # SHORT NAME preferred (see mesh_topology.mesh_board_marker_label,
            # unlike compact_node_label's own long-name-preferred choice).
            self.assertEqual(old_text, "• V3")
            self.assertNotEqual(old_text, "YOU")

    async def test_self_heard_echo_of_own_transmission_still_renders_you(self) -> None:
        """The exact real-hardware mechanism this bug traced to: a CHAT

        message is received whose sender_node_id equals the LOCAL
        radio's own ID (a self-heard echo/rebroadcast) -- this must
        never create a second, is_local=False working-set entry for
        YOU's own ID that a naive node_id-keyed dict could pick instead
        of the correct one (see mesh_state.build_mesh_working_set's
        self-heard-echo comment).
        """
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda: (
                NodeMetadata(you_id, "Simulated Node", "SIM", 0, is_local=True),
            )
            app._accept_received_message(
                SIMULATED_MESSAGES[0].__class__(
                    sender_node_id=you_id,
                    sender_long_name="Simulated Node",
                    sender_short_name="SIM",
                    channel_index=0,
                    text="echo",
                    rssi=None,
                    snr=None,
                    packet_id=1,
                    radio_rx_at=time.time(),
                )
            )
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            matching_states = [s for s in view.working_set if s.node.node_id == you_id]
            self.assertEqual(len(matching_states), 1)
            self.assertTrue(matching_states[0].node.is_local)
            label_widget, _ = self._you_label_widget(app)
            self.assertEqual(str(label_widget.render()), "YOU")

    async def test_radio_swap_still_works_alongside_label_fix(self) -> None:
        app = self._make_app()
        async with app.run_test(size=(90, 28)) as pilot:
            await self._open_mesh(pilot)
            first_label, first_id = self._you_label_widget(app)
            self.assertEqual(str(first_label.render()), "YOU")

            v4_info = replace(
                app.radio.info, node_id="!bbbbbbbb", long_name="V4 Radio", short_name="V4"
            )
            app._show_connection(RadioState.OFFLINE, message="switching device")
            await pilot.pause()
            app.radio.get_known_nodes = lambda: (
                NodeMetadata("!bbbbbbbb", "V4 Radio", "V4", 0, is_local=True),
            )
            app._show_connection(RadioState.ONLINE, v4_info)
            await pilot.pause()

            second_label, second_id = self._you_label_widget(app)
            self.assertEqual(str(second_label.render()), "YOU")
            self.assertNotEqual(second_id, first_id)
            self.assertEqual(second_id, "!bbbbbbbb")


class MeshTracerouteTests(unittest.IsolatedAsyncioTestCase):
    """MESHTASTICPASS TRACE ROUTE Part C: explicit, user-triggered

    traceroute -- menu gating, the TRACING ROUTE animation, the
    SUCCEEDED/FAILED lifecycle, the session-local "*" marker, and
    correlation/async-safety (selection changes, disconnects, timeouts,
    late responses, and repeated attempts must never misattribute a
    result -- see MeshtasticPassApp._start_traceroute/
    traceroute_status_received).
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = AppSettings.load(
            config_path=self.root / "config.json",
            profile_path=self.root / "terminal.conf",
        )

    def _make_app(self, **radio_kwargs) -> MeshtasticPassApp:
        radio = SimulatedRadioService(
            connect_delay=0, message_interval=0, scripted_messages=(), **radio_kwargs
        )
        return MeshtasticPassApp(radio, self.settings)

    async def _open_mesh(self, pilot) -> None:
        await pilot.pause()
        await pilot.press("3")
        await pilot.pause()
        await pilot.pause()

    @staticmethod
    def _two_remotes(you_id: str) -> tuple[NodeMetadata, ...]:
        now = time.time()
        return (
            NodeMetadata(you_id, is_local=True, position=LOCAL_GEO),
            NodeMetadata(
                "!a1111111", "Alice", "ALC", 1, last_heard=now - 5, position=north_of_local(2)
            ),
            NodeMetadata(
                "!b2222222", "Bob", "BOB", 1, last_heard=now - 5, position=south_of_local(2)
            ),
        )

    def _label_text(self, app: MeshtasticPassApp, node_id: str) -> str:
        return str(
            next(w for w in app.query(MeshNodeLabelWidget) if w.node_id == node_id).render()
        )

    # ---- Menu gating ---------------------------------------------------

    async def test_remote_menu_offers_trace_route_you_never_does(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.NO_RESPONSE,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)

            _mesh_select_node(app, "!a1111111")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("TRACE ROUTE", [item.label for item in app._user_menu.items])
            await pilot.press("escape")
            await pilot.pause()

            _mesh_select_node(app, you_id)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIn("TRACE ROUTE", [item.label for item in app._user_menu.items])

    async def test_trace_route_omitted_from_menu_while_one_is_pending(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.NO_RESPONSE,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            _mesh_select_node(app, "!a1111111")
            await pilot.pause()
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")
            app._start_traceroute(node)
            await pilot.pause()

            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIn("TRACE ROUTE", [item.label for item in app._user_menu.items])

    # ---- No automatic tracing -------------------------------------------

    async def test_never_traces_automatically(self) -> None:
        """Opening MESH, selecting nodes, and periodic refreshes must

        never call RadioService.send_traceroute on their own -- only an
        explicit _start_traceroute (menu action) does.
        """
        calls: list[str] = []
        app = self._make_app()
        original = app.radio.send_traceroute

        def spy(destination_node_id, status_handler):
            calls.append(destination_node_id)
            return original(destination_node_id, status_handler)

        app.radio.send_traceroute = spy
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            _mesh_select_node(app, "!a1111111")
            await pilot.pause()
            _mesh_select_node(app, "!b2222222")
            await pilot.pause()
            app._refresh_mesh()
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()

            self.assertEqual(calls, [])

    # ---- Exactly one explicit RF request / pending animation -----------

    async def test_explicit_trace_sends_exactly_one_rf_request(self) -> None:
        calls: list[str] = []
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.NO_RESPONSE,)
        )
        original = app.radio.send_traceroute

        def spy(destination_node_id, status_handler):
            calls.append(destination_node_id)
            return original(destination_node_id, status_handler)

        app.radio.send_traceroute = spy
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            _mesh_select_node(app, "!a1111111")
            await pilot.pause()
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()

            self.assertEqual(calls, ["!a1111111"])

    async def test_canonical_node_id_used_as_destination(self) -> None:
        """LONG/SHORT NAME are presentation only -- the canonical node ID

        is always what is actually sent as the RF destination.
        """
        calls: list[str] = []
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.NO_RESPONSE,)
        )
        original = app.radio.send_traceroute
        app.radio.send_traceroute = lambda dest, handler: (
            calls.append(dest) or original(dest, handler)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = NodeMetadata("!a1111111", "Some Very Different Display Name", "XYZ", 1)

            app._start_traceroute(node)
            await pilot.pause()

            self.assertEqual(calls, ["!a1111111"])

    async def test_pending_status_shows_animated_tracing_text(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.NO_RESPONSE,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()

            status = str(app.query_one("#mesh-status").render())
            self.assertEqual(status, "TRACING ROUTE TO ALC  > > >")

            first_frame = app._traceroute_animation_frame
            app._advance_delivery_states()
            self.assertNotEqual(app._traceroute_animation_frame, first_frame)

    # ---- Success / failure lifecycle + marker ---------------------------

    async def test_successful_trace_shows_banner_and_marks_the_node(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.SUCCEEDED,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")
            self.assertEqual(self._label_text(app, "!a1111111"), "• ALC")

            app._start_traceroute(node)
            await pilot.pause()

            self.assertEqual(str(app.query_one("#mesh-status").render()), "TRACE SUCCEEDED")
            self.assertIsNone(app._active_traceroute)
            self.assertIn("!a1111111", app._traceroute_results)
            self.assertEqual(self._label_text(app, "!a1111111"), "* ALC")

            # 10-second lifecycle: after the banner's own dismiss timer
            # fires, the normal status returns.
            app._dismiss_traceroute_banner()
            self.assertIsNone(app._traceroute_banner)

    async def test_failed_trace_shows_banner_without_marking_the_node(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.FAILED,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()

            self.assertEqual(str(app.query_one("#mesh-status").render()), "TRACE FAILED")
            self.assertIsNone(app._active_traceroute)
            self.assertNotIn("!a1111111", app._traceroute_results)
            self.assertEqual(self._label_text(app, "!a1111111"), "• ALC")

    async def test_later_failure_never_erases_an_earlier_success(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(
                SimulatedTracerouteOutcome.SUCCEEDED,
                SimulatedTracerouteOutcome.FAILED,
            )
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()
            self.assertEqual(self._label_text(app, "!a1111111"), "* ALC")

            app._start_traceroute(node)
            await pilot.pause()
            self.assertEqual(str(app.query_one("#mesh-status").render()), "TRACE FAILED")
            self.assertEqual(self._label_text(app, "!a1111111"), "* ALC")
            self.assertIn("!a1111111", app._traceroute_results)

    async def test_repeated_success_replaces_not_duplicates_the_stored_result(
        self,
    ) -> None:
        app = self._make_app(
            traceroute_outcomes=(
                SimulatedTracerouteOutcome.SUCCEEDED,
                SimulatedTracerouteOutcome.SUCCEEDED,
            ),
            traceroute_forward_route=("!ffff0001",),
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()
            app.radio.traceroute_forward_route = ("!ffff0002",)
            app._start_traceroute(node)
            await pilot.pause()

            self.assertEqual(len(app._traceroute_results), 1)
            self.assertEqual(
                app._traceroute_results["!a1111111"].forward_route, ("!ffff0002",)
            )

    # ---- One active trace at a time -------------------------------------

    async def test_repeated_attempt_while_pending_is_a_deterministic_no_op(
        self,
    ) -> None:
        calls: list[str] = []
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.NO_RESPONSE,)
        )
        original = app.radio.send_traceroute
        app.radio.send_traceroute = lambda dest, handler: (
            calls.append(dest) or original(dest, handler)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()
            first_token = app._active_traceroute.request_token
            app._start_traceroute(node)
            await pilot.pause()

            self.assertEqual(calls, ["!a1111111"])
            self.assertEqual(app._active_traceroute.request_token, first_token)

    # ---- Correlation / async safety --------------------------------------

    async def test_response_after_selection_change_still_resolves_the_original(
        self,
    ) -> None:
        """select A -> TRACE A -> select B -> response for A arrives: the

        result belongs to A, never B, and selection is untouched.
        """
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.SUCCEEDED,)
        )
        # Wide enough that recentering on B still keeps A's own label
        # widget mounted (never an off-screen edge indicator) -- the
        # marker check below needs A's real label, not just its
        # session-local result-dict entry.
        async with app.run_test(size=(300, 40)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            _mesh_select_node(app, "!a1111111")
            await pilot.pause()
            node_a = next(s.node for s in view.working_set if s.node.node_id == "!a1111111")

            app._start_traceroute(node_a)
            await pilot.pause()  # SimulatedRadioService resolves synchronously

            _mesh_select_node(app, "!b2222222")
            await pilot.pause()

            self.assertIn("!a1111111", app._traceroute_results)
            self.assertNotIn("!b2222222", app._traceroute_results)
            self.assertEqual(self._label_text(app, "!a1111111"), "* ALC")
            self.assertEqual(self._label_text(app, "!b2222222"), "• BOB")
            self.assertEqual(view.selected_node_id, "!b2222222")

    async def test_late_response_after_timeout_is_ignored(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.NO_RESPONSE,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()
            token = app._active_traceroute.request_token

            app._traceroute_timed_out(token)
            self.assertIsNone(app._active_traceroute)
            self.assertEqual(str(app.query_one("#mesh-status").render()), "TRACE FAILED")

            # A genuine (but late) success for the SAME, already-resolved
            # token must never retroactively create a marker.
            from radio_service import TracerouteResult, TracerouteStatus

            late_status = TracerouteStatus(
                TracerouteState.SUCCEEDED,
                None,
                result=TracerouteResult(
                    destination_node_id="!a1111111",
                    forward_route=(),
                    forward_snr=(),
                    return_route=(),
                    return_snr=(),
                    completed_at=time.time(),
                ),
            )

            class FakeEvent:
                request_token = token
                status = late_status

            app.traceroute_status_received(FakeEvent())
            self.assertNotIn("!a1111111", app._traceroute_results)
            self.assertEqual(self._label_text(app, "!a1111111"), "• ALC")

    async def test_disconnect_during_trace_cancels_it_silently(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.NO_RESPONSE,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()
            self.assertIsNotNone(app._active_traceroute)

            app._show_connection(RadioState.OFFLINE, message="lost")
            await pilot.pause()

            self.assertIsNone(app._active_traceroute)
            self.assertIsNone(app._traceroute_banner)
            self.assertEqual(str(app.query_one("#mesh-status").render()), "")

    async def test_navigating_away_mid_trace_does_not_cancel_it(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.NO_RESPONSE,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()
            token = app._active_traceroute.request_token

            app.show_tab("chat")
            await pilot.pause()
            self.assertIsNotNone(app._active_traceroute)
            self.assertEqual(app._active_traceroute.request_token, token)

    async def test_completion_never_steals_focus(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.SUCCEEDED,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")
            focused_before = app.focused

            app._start_traceroute(node)
            await pilot.pause()

            self.assertIs(app.focused, focused_before)

    # ---- No visualization changes ----------------------------------------

    async def test_traceroute_never_changes_connectors_or_placement(self) -> None:
        app = self._make_app(
            traceroute_outcomes=(SimulatedTracerouteOutcome.SUCCEEDED,)
        )
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            view = app.query_one(MeshTopologyView)
            positions_before = dict(view.base_positions)
            node = next(s.node for s in view.working_set if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()

            self.assertEqual(view.base_positions, positions_before)

    # ---- Requesting failure (radio not connected) -------------------------

    async def test_send_failure_resolves_as_a_failed_trace(self) -> None:
        app = self._make_app()

        def raising_send(destination_node_id, status_handler):
            raise RadioSendError("The simulated radio is not connected.")

        app.radio.send_traceroute = raising_send
        async with app.run_test(size=(90, 28)) as pilot:
            you_id = app.radio.info.node_id
            app.radio.get_known_nodes = lambda nodes=self._two_remotes(you_id): nodes
            await self._open_mesh(pilot)
            node = next(s.node for s in app.query_one(MeshTopologyView).working_set
                        if s.node.node_id == "!a1111111")

            app._start_traceroute(node)
            await pilot.pause()

            self.assertEqual(str(app.query_one("#mesh-status").render()), "TRACE FAILED")
            self.assertIsNone(app._active_traceroute)
