#!/usr/bin/env python3
"""Keyboard-first terminal application for MeshtasticPass."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from time import monotonic, time

from rich.cells import cell_len
from rich.color import Color
from rich.segment import Segment, Segments
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import (
    Container,
    Horizontal,
    ScrollableContainer,
    Vertical,
    VerticalScroll,
)
from textual.events import Blur, Click, Focus, Key
from textual.message import Message
from textual.scrollbar import ScrollBarRender
from textual.timer import Timer
from textual.widgets import ContentSwitcher, Input, Static
from textual.widget import Widget

from app_controller import (
    ChatEntry,
    RadioMonitor,
    create_radio_service,
    outgoing_chat_entry,
    received_chat_entry,
    stored_chat_entry,
)
from app_settings import AppSettings, COLOR_CHOICES, FONT_SIZE_CHOICES
from chat_store import (
    DEFAULT_HISTORY_LIMIT,
    OLDER_HISTORY_PAGE_SIZE,
    ChatStore,
    ChatStoreError,
)
from geo import format_distance_miles
from grapheme_text import install_flag_pair_protection
from keyboard_dropdown import DropdownOption, KeyboardDropdown
from mesh_state import (
    MeshNodeState,
    _remote_name_segments,
    build_mesh_working_set,
    format_mesh_context_line,
)
from mesh_topology import (
    DEFAULT_MAX_GRID_RADIUS,
    PositionedNode,
    RelayStage,
    TopologyLayout,
    assign_grid_slots,
    build_relay_stages,
    compact_node_label,
    directional_target,
    place_within_bounds,
    route_chain,
)
from node_activity import is_node_active
from radio_service import (
    ChannelInfo,
    DeliveryState,
    LONG_NAME_MAX_UTF8_BYTES,
    SHORT_NAME_MAX_UTF8_BYTES,
    RadioEvent,
    RadioIdentityError,
    RadioInfo,
    NodeMetadata,
    RadioSendError,
    RadioState,
    ReceivedMessage,
    SendStatus,
    SentMessage,
    rx_debug_enabled,
    rx_debug_log,
    validate_long_name,
    validate_short_name,
)
from relative_time import format_relative_age
from simulated_radio_service import SimulatedRadioService, SimulatedSendOutcome
from terminal_cursor import TerminalCursor
from theme_palette import ERROR, THEME_PALETTES
from viewport_menu import PopupItem, ViewportMenu


# Patches Rich's grapheme splitter (used by Static/Content's word-wrap
# fallback) so a regional-indicator flag pair can never be severed
# across a wrap boundary -- see grapheme_text.install_flag_pair_protection
# for the full rationale. Installed once at import time, process-wide,
# rather than per-message, since it corrects Rich's own wrap engine
# rather than any particular string.
install_flag_pair_protection()


# PROFILE is intentionally omitted here: it remains fully implemented
# (composed inside the "#content" ContentSwitcher, its widgets/settings/
# models/tests all untouched) but is hidden from the visible top nav and
# unreachable via any digit key -- see tab_for_key below. This is a
# navigation/UI-only change, not a deletion; restoring PROFILE to the
# nav later only requires re-adding it to this dict and to tab_for_key.
TAB_NAMES = {
    "connection": "CONNECTION/CONFIG",
    "chat": "CHAT",
    "mesh": "MESH",
}

ANIMATED_STATUS = {
    RadioState.CONNECTING: "CONNECTING",
    RadioState.OFFLINE: "OFFLINE — RETRYING",
    RadioState.ERROR: "CONNECTION ERROR — RETRYING",
}

CONNECTION_LABEL_WIDTH = 12
CONNECTION_ROW_PREFIX = "  "

CHAT_CONFIRMATION_TIMEOUT_SECONDS = 300.0
SEND_ERROR_AUTO_DISMISS_SECONDS = 10.0
CHAT_SCROLLBAR_THUMB_GLYPH = "▕"
MANUAL_RESEND_STATES = frozenset(
    (DeliveryState.UNCONFIRMED, DeliveryState.FAILED, DeliveryState.INTERRUPTED)
)


def can_manual_resend(entry: ChatEntry) -> bool:
    """Return whether the existing explicit rebroadcast action is valid."""
    return entry.outgoing and entry.delivery_state in MANUAL_RESEND_STATES


def _reply_mention_name(metadata: NodeMetadata) -> str:
    """Long Name -> Short Name -> compact Node ID, reusing the exact same

    name-fallback precedence mesh_state.format_mesh_context_line already
    uses for a real node's display name, rather than a second,
    independently-defined fallback rule.
    """
    return _remote_name_segments(metadata)[0]


class ThinScrollBarRender(ScrollBarRender):
    """Use one aligned narrow glyph for both track and draggable thumb.

    Vertical only: CHAT and CONNECTION are the only remaining scrollable
    views. MESH is a bounded, non-scrolling board with no scrollbars.
    """

    @classmethod
    def render_bar(
        cls,
        size: int = 25,
        virtual_size: float = 50,
        window_size: float = 20,
        position: float = 0,
        thickness: int = 1,
        vertical: bool = True,
        back_color: Color = Color.parse("#555555"),
        bar_color: Color = Color.parse("bright_magenta"),
    ) -> Segments:
        rendered = super().render_bar(
            size=size,
            virtual_size=virtual_size,
            window_size=window_size,
            position=position,
            thickness=thickness,
            vertical=vertical,
            back_color=back_color,
            bar_color=bar_color,
        )
        if not vertical:
            return rendered

        segments = []
        for segment in rendered.segments:
            if segment.text == "\n":
                segments.append(segment)
                continue
            metadata = segment.style.meta if segment.style is not None else {}
            is_thumb = metadata.get("@mouse.down") == "grab"
            segments.append(
                Segment(
                    CHAT_SCROLLBAR_THUMB_GLYPH * thickness,
                    Style(color=bar_color if is_thumb else back_color, meta=metadata),
                )
            )
        return Segments(segments, new_lines=rendered.new_lines)


class FontSizeSelector(KeyboardDropdown):
    def __init__(self, font_size: int) -> None:
        super().__init__(
            "font_size",
            "FONT SIZE",
            (DropdownOption(name, value) for name, value in FONT_SIZE_CHOICES),
            font_size,
            widget_id="font-size-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )

    @property
    def font_size(self) -> int:
        return int(self.value)


class ColorSelector(KeyboardDropdown):
    def __init__(self, color: str) -> None:
        super().__init__(
            "color",
            "COLOR",
            (DropdownOption(name, value) for name, value in COLOR_CHOICES),
            color,
            widget_id="color-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )

    @property
    def color(self) -> str:
        return str(self.value)


class DeviceSelector(KeyboardDropdown):
    def __init__(self, device_path: str, options: tuple[str, ...]) -> None:
        super().__init__(
            "device_path",
            "USB DEVICE",
            (DropdownOption(path, path) for path in options),
            device_path,
            widget_id="device-selector",
            label_width=CONNECTION_LABEL_WIDTH,
            classes="keyboard-dropdown connection-action-row",
        )


class ChannelSelector(KeyboardDropdown):
    def __init__(self, channels: tuple[ChannelInfo, ...], value: int) -> None:
        super().__init__(
            "channel_index",
            "CHAT ·",
            (DropdownOption(channel.name, channel.index) for channel in channels),
            value,
            widget_id="chat-title",
            prefix="",
        )


@dataclass
class ChannelChatState:
    entries: list[ChatEntry] = field(default_factory=list)
    unread_count: int = 0
    transcript_new_count: int = 0
    has_older_history: bool = False
    mounted_target: int = DEFAULT_HISTORY_LIMIT
    open_scroll_pending: bool = False
    loaded: bool = False
    new_message_ids: set[int] = field(default_factory=set)
    unread_message_ids: set[int] = field(default_factory=set)
    pending_older_ids: set[tuple[str, int]] = field(default_factory=set)
    new_below_ids: set[tuple[str, int]] = field(default_factory=set)


class RadioMessageReceived(Message):
    """Thread-safe bridge from either radio service into the UI queue."""

    def __init__(self, message: ReceivedMessage) -> None:
        super().__init__()
        self.message = message


class SendSubmitted(Message):
    def __init__(
        self,
        entry: ChatEntry,
        sent: SentMessage,
        generation: int,
    ) -> None:
        super().__init__()
        self.entry = entry
        self.sent = sent
        self.generation = generation


class SendFailed(Message):
    def __init__(self, entry: ChatEntry, detail: str, generation: int) -> None:
        super().__init__()
        self.entry = entry
        self.detail = detail
        self.generation = generation


class DeliveryStatusReceived(Message):
    def __init__(
        self,
        entry: ChatEntry,
        status: SendStatus,
        generation: int,
    ) -> None:
        super().__init__()
        self.entry = entry
        self.status = status
        self.generation = generation


class IdentitySaved(Message):
    """The radio accepted an advertised identity-name update."""

    def __init__(self, info: RadioInfo, field_label: str) -> None:
        super().__init__()
        self.info = info
        self.field_label = field_label


class IdentitySaveFailed(Message):
    """An identity-name update failed validation or radio submission."""

    def __init__(self, detail: str) -> None:
        super().__init__()
        self.detail = detail


class LoadOlderControl(Static):
    """Focusable, keyboard-only control for one bounded history page."""

    can_focus = True

    class Activated(Message):
        pass

    def __init__(self) -> None:
        super().__init__("[ LOAD OLDER ]", id="load-older", classes="chat-nav-target", markup=False)

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Activated())
            event.stop()


class EndOfChatHistoryMarker(Static):
    """Passive proof that the oldest stored row is currently mounted."""

    can_focus = False

    def __init__(self) -> None:
        super().__init__(
            "END OF CHAT HISTORY",
            id="end-of-chat-history",
            markup=False,
        )


class MessageActionControl(Static):
    """A contextual action beneath a message; ready for future action kinds."""

    can_focus = True

    class Activated(Message):
        def __init__(self, control: "MessageActionControl") -> None:
            super().__init__()
            self.action_control = control

    def __init__(self, entry: ChatEntry, action: str = "resend") -> None:
        self.entry = entry
        self.action = action
        super().__init__(
            "[ RESEND ]",
            classes="message-action chat-nav-target",
            markup=False,
        )

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Activated(self))
            event.stop()


class ChatTranscript(VerticalScroll):
    """Focusable transcript so navigation never types into the message box."""

    can_focus = True

    def on_mount(self) -> None:
        self.vertical_scrollbar.renderer = ThinScrollBarRender
        self.anchor()

    def on_key(self, event: Key) -> None:
        """Keep transcript navigation deterministic when this widget has focus."""
        app = self.app
        if getattr(app, "_user_menu", None) is not None:
            # A node-context menu (e.g. opened via Enter on this exact
            # widget) is showing -- up/down must move ITS highlight
            # (handled by the app-level on_key this event still bubbles
            # to), not the chat message selection underneath it. Focus
            # never actually moves off this ChatEntryWidget/transcript
            # while the menu is open (see _open_node_menu), so without
            # this guard up/down would always be captured here first.
            return
        if event.key in ("up", "down"):
            app._move_chat_focus(-1 if event.key == "up" else 1)
            event.stop()
        elif event.key in ("pageup", "pagedown"):
            step = max(1, self.region.height - 2)
            self.scroll_relative(y=-step if event.key == "pageup" else step)
            app.call_after_refresh(app._clear_indicator_if_at_bottom)
            event.stop()
        elif event.key == "left":
            app._focus_oldest_new_message()
            event.stop()
        elif event.key == "right":
            app._return_to_present_and_type()
            event.stop()
        elif event.key == "end":
            app._jump_to_newest()
            event.stop()


class ChatMessageInput(Input):
    """The CHAT draft input; posts Left so the app can react to focus

    loss without needing to know every possible reason it happened
    (Escape, a tab switch, a mouse click elsewhere) -- see the
    empty-message error's auto-dismiss-on-focus-loss requirement.
    """

    class Left(Message):
        pass

    def on_blur(self, _event: Blur) -> None:
        self.post_message(self.Left())


class ConnectionPage(VerticalScroll):
    """Scrollable settings surface for short uConsole terminal windows."""

    def on_mount(self) -> None:
        self.vertical_scrollbar.renderer = ThinScrollBarRender


CIRCLE_SOLID_LARGE = "●"
CIRCLE_STROKED_LARGE = "○"

# --- MESH: real passive-data visualization on a fixed grid -----------------
#
# A fixed 21-column x 8-row board driven entirely by real, passively
# observed Meshtastic data -- no LoRa traffic is ever generated to
# populate or refresh it (see _refresh_mesh). Real nodes are placed by
# coarse compass direction/distance ranking when GPS is available
# (never exact, proportional geography), spread to use more of the
# fixed grid's available room, and a real CLIENT's truthful nonzero hop
# count renders as that many anonymous relay-stage placeholders along
# its path to YOU (see mesh_topology.RelayStage) -- visual/topology
# decoration only, never selectable, focusable, or a navigation
# candidate. Intentionally out of scope: Favorites, dynamic node-count
# growth beyond the bounded working set, and scrolling (the working set
# is bounded precisely so the whole board always fits one viewport).
# See mesh_state.py for the working-set/role/staleness/distance model
# and mesh_topology.py for the pure grid geometry (assign_grid_slots(),
# place_within_bounds(), directional_target(), build_relay_stages(),
# route_chain()) reused here.
MESH_GRID_ROWS = 8
MESH_GRID_COLUMNS = 21
MESH_GRID_CENTER_ROW = 5
MESH_GRID_CENTER_COLUMN = 11
# Selected-node "visually larger" treatment: a 3-cell-wide composite
# (small dot + role glyph + small dot) replacing the ordinary 1-cell
# glyph -- see MeshNodeWidget.refresh_visual for why bold alone wasn't
# enough and why this stays a reliable-width text composite rather than
# an ambiguous-width "big circle" Unicode glyph.
MESH_SELECTED_GLYPH_WIDTH = 3
MESH_SELECTED_HALO_GLYPH = "·"
# The label physically above a node's glyph is a compact hint, not the
# full identity -- that lives in the rich bottom-left context (see
# mesh_state.format_mesh_context_line, which always has the full Long
# Name/Short Name, uncapped). Capped in DISPLAY CELLS (cell_len()), not
# Python len(), so wide/CJK/emoji glyphs are counted by their actual
# terminal width -- see mesh_topology.compact_node_label/_truncate for
# the grapheme-safe truncation this limit is applied through.
MESH_BOARD_LABEL_MAX_CELLS = 5


def _mesh_node_color(state: MeshNodeState, *, selected: bool, theme: str, now: float) -> str:
    """Selection (ACCENT) always overrides active/inactive styling.

    A real remote node's brightness uses the EXACT SAME predicate as the
    MESH header's "ACTIVE N" count -- node_activity.is_node_active,
    keyed on RadioService's passive last_heard -- so the two always
    visually agree: if the header says ACTIVE 4, exactly the working
    set's real remote nodes satisfying this same predicate render BASE.
    This is deliberately a different concept from MeshNodeState.
    is_stale() (>24h since the last CHAT interaction), which decides
    which nodes are worth ranking into the working set at all (see
    mesh_state.build_mesh_working_set) -- not how bright an already-
    displayed node looks. A node can therefore show a merely-old
    interaction time ("2h") while still rendering dim, and that is
    expected. YOU has no activity concept and always uses ordinary
    ACCENT/BASE.
    """
    palette = THEME_PALETTES[theme]
    if selected:
        return palette.accent
    if not state.node.is_local and not is_node_active(state.node.last_heard, now):
        return palette.dim_base
    return palette.base


def _mesh_relay_color(theme: str) -> str:
    """An anonymous relay-stage placeholder is visual topology only: it

    has no identity to be "heard" from, is never active/inactive, and
    (unlike a real node) is never selectable, so it is always DIM_BASE
    -- never ACCENT, regardless of theme, activity, or selection state
    anywhere else on the board.
    """
    return THEME_PALETTES[theme].dim_base


def _mesh_grid_pixel(row: int, column: int) -> tuple[int, int]:
    """Convert a 1-indexed logical grid position to a pixel coordinate,

    aligned exactly to a dot-grid intersection (DOT_GRID_SPACING_X/Y below).
    """
    return (column - 1) * DOT_GRID_SPACING_X, (row - 1) * DOT_GRID_SPACING_Y


def _mesh_translated_positions(
    base_positions: Mapping[str, tuple[int, int]], selected_node_id: str
) -> dict[str, tuple[int, int]]:
    """Translate the whole current layout so the selected node sits at the

    center grid position. This is a pure whole-mesh translation: every
    node shifts by the same row/column delta, so relative geometry
    between nodes never changes -- it is never an independent per-node
    recomputation, and it never touches `base_positions` (the working
    set's fixed geographic/fallback layout) itself.
    """
    selected = base_positions.get(selected_node_id)
    if selected is None:
        return dict(base_positions)
    row_delta = MESH_GRID_CENTER_ROW - selected[0]
    column_delta = MESH_GRID_CENTER_COLUMN - selected[1]
    return {
        node_id: (row + row_delta, column + column_delta)
        for node_id, (row, column) in base_positions.items()
    }


def _mesh_directional_target(
    base_positions: Mapping[str, tuple[int, int]],
    current_node_id: str,
    direction: str,
) -> str | None:
    """Pick the ID reached by an arrow press, reusing the shared

    spatial-navigation rule (mesh_topology.directional_target) against
    the CURRENT fixed, untranslated LOGICAL (row, column) positions --
    not rendered pixel positions. DOT_GRID_SPACING_X/Y (4x2) make one
    logical row-step visually shorter than one column-step on screen, a
    purely cosmetic choice; ranking by pixel distance would let that
    asymmetry distort "sensible direction". Direction is a property of
    the mesh's logical geometry, not of wherever the selection happens
    to be recentered on screen.

    Candidates come directly from whatever `base_positions` the caller
    passes -- e.g. _move_mesh_focus deliberately excludes anonymous
    relay-stage IDs before calling this, since a relay stage is visual
    topology only and must never become a navigation target -- rather
    than from any node-role data baked into this function itself. No
    node IDs are hardcoded: this is the same general nearest-candidate
    rule for whatever position set the caller provides.
    """
    layout = TopologyLayout(
        tuple(
            PositionedNode(node=NodeMetadata(node_id), x=column, y=row, region="UNKNOWN")
            for node_id, (row, column) in base_positions.items()
        ),
        width=MESH_GRID_COLUMNS,
        height=MESH_GRID_ROWS,
    )
    target = directional_target(current_node_id, layout, direction)  # type: ignore[arg-type]
    return target.node.node_id if target is not None else None


def _mesh_hop_counts(working_set: tuple[MeshNodeState, ...]) -> dict[str, int]:
    """Real remote CLIENTs with a trustworthy, nonzero hop count -- used

    ONLY for grid PLACEMENT (see _refresh_mesh's min_radius_by_id):
    every such node, active or stale, reserves enough outward room for
    its potential relay chain, so a node's position never needs to jump
    outward the moment it later becomes active (see item 5 -- placement
    stability is independent of current activity). An unknown hop count
    (hops_away is None) is deliberately absent here and below: it must
    never be treated as zero or imply any specific path depth.

    NOT used for deciding which nodes actually render a relay chain or
    connector -- see _mesh_active_hop_counts for that.
    """
    return {
        state.node.node_id: state.node.hops_away
        for state in working_set
        if not state.node.is_local
        and state.node.hops_away is not None
        and state.node.hops_away > 0
    }


def _mesh_active_hop_counts(
    working_set: tuple[MeshNodeState, ...], *, now: float
) -> dict[str, int]:
    """Real remote CLIENTs with a trustworthy, nonzero hop count AND

    currently active (is_node_active(last_heard, now) -- the same
    predicate the board's BASE/DIM_BASE styling and [3] MESH (N) use).

    This -- not _mesh_hop_counts -- is what decides which nodes get an
    anonymous relay chain and a connector line drawn back to YOU (see
    MeshTopologyView.set_nodes): a stale node remains visible at its
    last-known position as useful historical context, but the board
    should never look like it is CURRENTLY connected to something it
    hasn't heard from recently. Active connectivity is a rendering-only
    decision, made fresh every refresh -- it never affects grid
    placement (see _mesh_hop_counts), so a node's position stays stable
    across an activity transition.
    """
    return {
        state.node.node_id: state.node.hops_away
        for state in working_set
        if not state.node.is_local
        and state.node.hops_away is not None
        and state.node.hops_away > 0
        and is_node_active(state.node.last_heard, now)
    }


class MeshNodeWidget(Static):
    """The node's glyph: a single cell, anchored exactly on its grid

    coordinate. The glyph's own screen position must never depend on its
    label's width -- see MeshNodeLabelWidget for the label, a separately
    positioned overlay above this glyph, never the other way around.

    (A single two-line Text with justify="center" was tried here first,
    relying on Rich's own per-line centering to align the 1-cell glyph
    line under the wider label line. That does not hold once Textual
    composites the Static's content into the screen buffer -- the short
    line renders flush against the box's left edge instead of centered,
    silently shifting the glyph off its grid coordinate. Two independently
    positioned single-line widgets sidesteps that entirely: neither box is
    ever wider than its own content, so there is no centering decision left
    for Textual's renderer to get wrong.)

    Selection state lives on MeshTopologyView, not Textual's focus system:
    rendering always reflects the current selection synchronously, so there
    is no second, focus-driven source of visual selection state to keep in
    sync. Textual focus is not used here at all -- mouse clicks are routed
    by position, not focus, and keyboard arrow routing is handled entirely
    at the App level against MeshTopologyView.selected_node_id.
    """

    def __init__(self, state: MeshNodeState) -> None:
        self.state = state
        self.node_id = state.node.node_id
        super().__init__(classes="mesh-node", markup=False)

    def refresh_visual(self, *, selected: bool, theme: str, now: float) -> None:
        color = _mesh_node_color(self.state, selected=selected, theme=theme, now=now)
        # The glyph shape itself is never altered by selection -- ACTIVE
        # (solid) vs stale (stroked) stays the authoritative visual state
        # regardless of selection. This is the EXACT SAME predicate as
        # _mesh_node_color's BASE/DIM_BASE split and [3] MESH (N)'s count
        # (is_node_active), not CLIENT/is_client -- a real node admitted
        # purely from passive NodeDB data (never having sent a CHAT
        # message) is otherwise indistinguishable in shape from an
        # anonymous relay stage while active, which is exactly what made
        # a genuinely active endpoint look like a relay chain dead-end
        # (see MeshRelayWidget, always stroked/unlabeled). YOU has no
        # activity concept and is always solid.
        glyph = (
            CIRCLE_SOLID_LARGE
            if self.state.node.is_local or is_node_active(self.state.node.last_heard, now)
            else CIRCLE_STROKED_LARGE
        )
        style = Style(color=color, bold=selected)
        if selected:
            # Bold alone reads as barely-different on many terminals, so
            # the anchor cell's role glyph is flanked by a small dot in
            # each immediately neighboring cell on the same row -- a real,
            # ~3x wider visual footprint, not a font-weight trick. The
            # widget's own width/offset (set in MeshTopologyView.set_nodes) keep the
            # center *column* of this 3-cell composite exactly on the
            # glyph's (grid_x, grid_y) anchor, so growing it can never
            # move that coordinate.
            content = Text(justify="center")
            content.append(MESH_SELECTED_HALO_GLYPH, style=style)
            content.append(glyph, style=style)
            content.append(MESH_SELECTED_HALO_GLYPH, style=style)
        else:
            content = Text(glyph, style=style)
        self.update(content)

    def on_click(self, _event: Click) -> None:
        _mesh_select_node(self.app, self.node_id)


class MeshNodeLabelWidget(Static):
    """The node's label: a separately positioned overlay above its glyph.

    Always exactly cell_len(label) cells wide -- its own box is never wider
    than its content, so it needs no internal centering, only a computed
    offset (see MeshTopologyView.set_nodes). Never influences the
    glyph's own coordinate; see MeshNodeWidget.
    """

    def __init__(self, state: MeshNodeState) -> None:
        self.state = state
        self.node_id = state.node.node_id
        super().__init__(classes="mesh-node", markup=False)

    def refresh_visual(self, *, selected: bool, theme: str, now: float) -> None:
        color = _mesh_node_color(self.state, selected=selected, theme=theme, now=now)
        label = compact_node_label(self.state.node, MESH_BOARD_LABEL_MAX_CELLS)
        self.update(Text(label, style=Style(color=color)))

    def on_click(self, _event: Click) -> None:
        _mesh_select_node(self.app, self.node_id)


class MeshRelayWidget(Static):
    """An anonymous relay-stage placeholder glyph: visual topology only.

    Always a stroked, DIM_BASE, unlabeled 1-cell glyph -- see
    mesh_topology.RelayStage. Deliberately NOT interactive: no on_click
    (a click here does nothing), can_focus is False, and it is excluded
    from the arrow-navigation candidate set entirely (see
    MeshtasticPassApp._move_mesh_focus) -- it can never become
    selected_node_id, never shows ACCENT or the enlarged selected
    composite, and never appears in the bottom-left context. A hollow
    dot here means only "an unidentified relay stage exists between two
    real, inspectable nodes" -- never a thing to inspect itself.
    """

    can_focus = False

    def __init__(self, stage: RelayStage) -> None:
        self.stage = stage
        self.node_id = stage.node_id
        super().__init__(classes="mesh-node", markup=False)

    def refresh_visual(self, *, theme: str) -> None:
        self.update(Text(CIRCLE_STROKED_LARGE, style=Style(color=_mesh_relay_color(theme))))


def _mesh_select_node(app: MeshtasticPassApp, node_id: str) -> None:
    view = app.query_one(MeshTopologyView)
    view.select_node(node_id)
    view.set_nodes(view.working_set, view.base_positions, theme=app._current_theme, now=time())
    app._update_mesh_context_status()


DOT_GRID_GLYPH = "·"
# The fixed MESH grid must fit entirely inside the MESH viewport on the
# supported uConsole/test terminal size (~90 columns) with no scrolling.
# 4x2 keeps a 2:1 x:y ratio (so the grid reads as roughly square against
# typical terminal cell proportions); at the current 9x21 grid that renders
# an 84x18 board, with margin inside that viewport.
DOT_GRID_SPACING_X = 4
DOT_GRID_SPACING_Y = 2


def _render_mesh_canvas(
    width: int,
    height: int,
    connectors: tuple[tuple[int, int, str, str], ...],
    dot_color: str,
) -> Text:
    """Build one procedural Text for the whole canvas: dot grid plus lines.

    Never one widget per dot or per line segment. `connectors` is a list of
    (x, y, glyph, color) cells that override the dot grid at that position,
    e.g. a YOU-to-node relationship line drawn by mesh_topology.route_connector.
    """
    if width <= 1 and height <= 1:
        return Text("")
    overlay = {(x, y): (glyph, color) for x, y, glyph, color in connectors}
    text = Text()
    for y in range(height):
        if y:
            text.append("\n")
        x = 0
        while x < width:
            if (x, y) in overlay:
                glyph, color = overlay[(x, y)]
                text.append(glyph, style=Style(color=color))
                x += 1
                continue
            run_end = x
            while run_end < width and (run_end, y) not in overlay:
                run_end += 1
            segment = "".join(
                DOT_GRID_GLYPH
                if column % DOT_GRID_SPACING_X == 0 and y % DOT_GRID_SPACING_Y == 0
                else " "
                for column in range(x, run_end)
            )
            text.append(segment, style=Style(color=dot_color))
            x = run_end
    return text


class MeshCanvas(Static):
    """Static, non-interactive procedural background: dot grid plus connectors.

    One widget covers the entire bounded board (never one widget per dot or
    per line segment), so it stays cheap regardless of topology size.
    """

    can_focus = False

    def __init__(self) -> None:
        super().__init__(classes="mesh-canvas", markup=False)
        self._signature: tuple[object, ...] | None = None

    def render_scene(
        self,
        width: int,
        height: int,
        connectors: tuple[tuple[int, int, str, str], ...],
        theme: str,
    ) -> None:
        signature = (width, height, connectors, theme)
        if signature == self._signature:
            return
        self._signature = signature
        self.styles.width = width
        self.styles.height = height
        self.update(
            _render_mesh_canvas(width, height, connectors, THEME_PALETTES[theme].grid_dot)
        )


class MeshTopologyView(Container):
    """A fixed 8x21 grid MESH board driven by a real, bounded MESH working set.

    No scrolling, no scrollbars -- the working set is bounded precisely
    so the whole board always fits this one fixed viewport (see
    mesh_state.build_mesh_working_set). Node identity, positions
    (base_positions, from mesh_topology.assign_grid_slots() +
    place_within_bounds()), and roles all come from the current working
    set passed to set_nodes(); this view only lays out and renders them.
    """

    can_focus = True

    def __init__(self) -> None:
        super().__init__(
            Container(MeshCanvas(), id="mesh-board"),
            id="mesh-view",
        )
        self._selected_node_id = ""
        self._working_set: tuple[MeshNodeState, ...] = ()
        self._base_positions: dict[str, tuple[int, int]] = {}
        self._relay_stages: tuple[RelayStage, ...] = ()

    @property
    def board(self) -> Container:
        return self.query_one("#mesh-board", Container)

    @property
    def selected_node_id(self) -> str:
        return self._selected_node_id

    @property
    def working_set(self) -> tuple[MeshNodeState, ...]:
        return self._working_set

    @property
    def base_positions(self) -> dict[str, tuple[int, int]]:
        """Real-node AND anonymous-relay-stage logical (row, column)

        positions, merged -- rendering and connector routing need both
        kinds of coordinate together. This is NOT the arrow-navigation
        candidate set: a RelayStage's ID is included here (its glyph
        must be positioned and translated exactly like a real node's)
        but is never itself navigable -- see
        MeshtasticPassApp._move_mesh_focus, which explicitly filters
        every RelayStage ID out of this dict before calling
        _mesh_directional_target, so only real working-set nodes are
        ever navigation candidates.
        """
        return self._base_positions

    @property
    def relay_stages(self) -> tuple[RelayStage, ...]:
        return self._relay_stages

    def set_nodes(
        self,
        working_set: tuple[MeshNodeState, ...],
        base_positions: Mapping[str, tuple[int, int]],
        *,
        theme: str,
        now: float,
    ) -> None:
        """Render the current working set, recentered on the selection.

        Preserves the current selection if it is still in the working
        set; otherwise falls back to YOU (or the first node, if somehow
        there is no local node). Widgets are added/removed only for
        nodes that actually entered/left the working set -- unchanged
        nodes keep their existing widget, so unchanged data never
        reshuffles or remounts anything.
        """
        board = self.board
        board_width = MESH_GRID_COLUMNS * DOT_GRID_SPACING_X
        board_height = MESH_GRID_ROWS * DOT_GRID_SPACING_Y
        board.styles.width = board_width
        board.styles.height = board_height
        # Horizontally center the whole board as one rigid block inside the
        # available MESH region -- a board-level offset, not a per-node one,
        # so it can never desync node-to-grid coordinates. On the very first
        # render (before this view has ever been laid out), self.size is
        # not resolved yet (0x0); re-run once after the next refresh, when
        # it is, rather than leaving the board visibly left-anchored.
        view_width = self.size.width
        if view_width:
            board.styles.offset = (max(0, (view_width - board_width) // 2), 0)
        else:
            self.app.call_after_refresh(
                lambda: self.set_nodes(working_set, base_positions, theme=theme, now=now)
            )

        self._working_set = working_set
        current_ids = {state.node.node_id for state in working_set}
        you_id = next(
            (state.node.node_id for state in working_set if state.node.is_local),
            None,
        )
        # `base_positions` is expected to carry only real-node positions
        # (exactly what place_within_bounds() produces), but is filtered
        # against `current_ids` regardless -- so passing back a previous
        # call's already-merged view.base_positions (which also contains
        # relay-stage entries) is harmless: those extra keys are simply
        # ignored here and relay stages are recomputed fresh below, never
        # accumulated across calls.
        real_positions = {
            node_id: position
            for node_id, position in base_positions.items()
            if node_id in current_ids
        }
        # Relay chains render ONLY for currently ACTIVE clients (see
        # _mesh_active_hop_counts) -- a stale node keeps its last-known
        # position (see _mesh_hop_counts, used for placement only, above
        # in _refresh_mesh) but gets no anonymous relay chain: the board
        # answers "what does my radio currently believe is active" via
        # rendered connectors, and "what else does my radio remember"
        # via dim, disconnected real nodes.
        active_hop_counts = _mesh_active_hop_counts(working_set, now=now)
        relay_stages, relay_positions = build_relay_stages(
            real_positions,
            you_id=you_id or "",
            hop_counts=active_hop_counts,
            row_count=MESH_GRID_ROWS,
            column_count=MESH_GRID_COLUMNS,
        )
        self._relay_stages = relay_stages
        self._base_positions = {**real_positions, **relay_positions}
        relay_ids = {stage.node_id for stage in relay_stages}
        # Anonymous relay stages are visual topology only -- an anonymous
        # relay ID is never a valid selection, so it falls back to YOU
        # exactly like any other ID that isn't a real working-set member.
        if self._selected_node_id not in current_ids:
            local_id = you_id
            self._selected_node_id = local_id or (
                working_set[0].node.node_id if working_set else ""
            )

        states_by_id = {state.node.node_id: state for state in working_set}
        stages_by_id = {stage.node_id: stage for stage in relay_stages}
        # Widget.remove() only schedules removal -- it does not take effect
        # before the next refresh -- so every query below must keep
        # filtering by `current_ids` itself rather than assume a removed
        # widget is already gone from self.query().
        for widget in list(self.query(MeshNodeWidget)):
            if widget.node_id not in current_ids:
                widget.remove()
        for widget in list(self.query(MeshNodeLabelWidget)):
            if widget.node_id not in current_ids:
                widget.remove()
        for widget in list(self.query(MeshRelayWidget)):
            if widget.node_id not in relay_ids:
                widget.remove()
        existing_glyph_ids = {
            widget.node_id
            for widget in self.query(MeshNodeWidget)
            if widget.node_id in current_ids
        }
        existing_label_ids = {
            widget.node_id
            for widget in self.query(MeshNodeLabelWidget)
            if widget.node_id in current_ids
        }
        existing_relay_ids = {
            widget.node_id
            for widget in self.query(MeshRelayWidget)
            if widget.node_id in relay_ids
        }
        new_glyphs = [
            MeshNodeWidget(states_by_id[node_id])
            for node_id in states_by_id
            if node_id not in existing_glyph_ids
        ]
        new_labels = [
            MeshNodeLabelWidget(states_by_id[node_id])
            for node_id in states_by_id
            if node_id not in existing_label_ids
        ]
        # Anonymous relay-stage placeholders are never labeled -- no
        # MeshRelayLabelWidget counterpart exists (see RelayStage).
        new_relays = [
            MeshRelayWidget(stages_by_id[node_id])
            for node_id in stages_by_id
            if node_id not in existing_relay_ids
        ]
        if new_glyphs:
            board.mount_all(new_glyphs)
        if new_labels:
            board.mount_all(new_labels)
        if new_relays:
            board.mount_all(new_relays)
        glyph_widgets = [
            widget for widget in self.query(MeshNodeWidget) if widget.node_id in current_ids
        ]
        label_widgets = [
            widget
            for widget in self.query(MeshNodeLabelWidget)
            if widget.node_id in current_ids
        ]
        relay_widgets = [
            widget for widget in self.query(MeshRelayWidget) if widget.node_id in relay_ids
        ]
        for widget in glyph_widgets:
            widget.state = states_by_id[widget.node_id]
        for widget in label_widgets:
            widget.state = states_by_id[widget.node_id]
        for widget in relay_widgets:
            widget.stage = stages_by_id[widget.node_id]

        positions = _mesh_translated_positions(
            self._base_positions, self._selected_node_id
        )
        centers: dict[str, tuple[int, int]] = {}
        # The glyph is the sole coordinate authority: it is always a 1x1
        # (or, selected, 3x1) widget centered exactly on (grid_x, grid_y),
        # so label width can never influence it. Positioned first so
        # `centers` (used for connector endpoints below) only ever
        # reflects glyph coordinates.
        for widget in glyph_widgets:
            row, column = positions[widget.node_id]
            grid_x, grid_y = _mesh_grid_pixel(row, column)
            selected = widget.node_id == self._selected_node_id
            # Selected nodes render a 3-cell-wide composite (see
            # MeshNodeWidget.refresh_visual); centering that wider box on
            # grid_x -- the same formula used for the label -- keeps its
            # middle column, not just its left edge, on the anchor.
            width = MESH_SELECTED_GLYPH_WIDTH if selected else 1
            widget.styles.width = width
            widget.styles.height = 1
            widget.styles.offset = (grid_x - width // 2, grid_y)
            centers[widget.node_id] = (grid_x, grid_y)
            widget.refresh_visual(selected=selected, theme=theme, now=now)

        # Relay-stage placeholders share the same glyph anchor formula as
        # a real node's glyph (see MeshNodeWidget above) but never the
        # enlarged selected composite -- a relay stage is never selected
        # (see MeshRelayWidget), so it is always exactly 1 cell wide.
        for widget in relay_widgets:
            row, column = positions[widget.node_id]
            grid_x, grid_y = _mesh_grid_pixel(row, column)
            widget.styles.width = 1
            widget.styles.height = 1
            widget.styles.offset = (grid_x, grid_y)
            centers[widget.node_id] = (grid_x, grid_y)
            widget.refresh_visual(theme=theme)

        # The label is a separate, independently positioned overlay: its
        # own box is exactly cell_len(label) wide (never wider), centered
        # over the glyph's fixed (grid_x, grid_y) by offsetting the whole
        # label widget -- never by resizing or repositioning the glyph.
        for widget in label_widgets:
            grid_x, grid_y = centers[widget.node_id]
            label_width = max(
                1, cell_len(compact_node_label(widget.state.node, MESH_BOARD_LABEL_MAX_CELLS))
            )
            widget.styles.width = label_width
            widget.styles.height = 1
            widget.styles.offset = (grid_x - label_width // 2, grid_y - 1)
            selected = widget.node_id == self._selected_node_id
            widget.refresh_visual(selected=selected, theme=theme, now=now)

        # Connector semantics: a YOU-to-node path means "we currently
        # believe this node is active in the mesh" -- CLIENT history
        # alone is not enough, and neither is a merely-known hop count;
        # see _mesh_active_hop_counts. A stale node keeps its last-known
        # position but no path back to YOU: it answers "what else does
        # my radio remember", not "what's active right now". It is drawn
        # through exactly that client's own anonymous relay stages
        # (hops_away of them, in order -- see RelayStage/
        # build_relay_stages), representing observed path DEPTH, never
        # discovered relay identity: "Alice is 3 relay stages away",
        # never "these are three identified radios". A `? HOPS` client
        # gets no path at all -- any line, direct or through relay
        # stages, would imply a specific, unverified hop depth, which is
        # never fabricated (see the MESH spec's "UNKNOWN HOPS" / "? HOPS
        # CONNECTORS" sections).
        palette = THEME_PALETTES[theme]
        stages_by_client: dict[str, list[RelayStage]] = {}
        for stage in relay_stages:
            stages_by_client.setdefault(stage.source_node_id, []).append(stage)
        for stages in stages_by_client.values():
            stages.sort(key=lambda stage: stage.index)

        connector_cells: list[tuple[int, int, str, str]] = []
        if you_id is not None and you_id in centers:
            for state in working_set:
                remote_id = state.node.node_id
                hops = state.node.hops_away
                if (
                    state.node.is_local
                    or remote_id not in centers
                    or hops is None
                    or not is_node_active(state.node.last_heard, now)
                ):
                    continue
                chain_stages = stages_by_client.get(remote_id, ())
                chain_ids = {remote_id, *(stage.node_id for stage in chain_stages)}
                chain_points = (
                    centers[you_id],
                    *(
                        centers[stage.node_id]
                        for stage in chain_stages
                        if stage.node_id in centers
                    ),
                    centers[remote_id],
                )
                color = (
                    palette.accent
                    if self._selected_node_id in chain_ids
                    else palette.dim_base
                )
                connector_cells.extend(
                    (x, y, glyph, color) for x, y, glyph in route_chain(chain_points)
                )
        self.board.query_one(MeshCanvas).render_scene(
            board_width, board_height, tuple(connector_cells), theme
        )

    def clear_nodes(self) -> None:
        self.board.remove_children(MeshNodeWidget)
        self.board.remove_children(MeshNodeLabelWidget)
        self.board.remove_children(MeshRelayWidget)
        self._working_set = ()
        self._base_positions = {}
        self._relay_stages = ()
        self._selected_node_id = ""
        self.board.query_one(MeshCanvas).render_scene(1, 1, (), "white")

    def select_node(self, node_id: str) -> None:
        """Select a real working-set node by ID; anything else is a no-op.

        `_selected_node_id` may only ever hold a real node currently in
        `working_set` -- an anonymous RelayStage's synthetic ID, or any
        other stale/unknown ID, is rejected outright rather than
        accepted here and relying on a later set_nodes() call to
        sanitize it back out. Relay stages are internal rendering/
        topology bookkeeping only (see mesh_topology.RelayStage) and can
        never be selected, focused, navigated to, or recentered on.
        """
        if any(state.node.node_id == node_id for state in self._working_set):
            self._selected_node_id = node_id

    def select_local(self) -> None:
        local_id = next(
            (state.node.node_id for state in self._working_set if state.node.is_local),
            None,
        )
        if local_id is not None:
            self.select_node(local_id)


class IdentityNameControl(Horizontal):
    """Shared two-state identity field for navigation and text editing."""

    can_focus = True
    MIN_FIELD_WIDTH = 8
    MAX_UTF8_BYTES = LONG_NAME_MAX_UTF8_BYTES
    LABEL = "NAME"
    INPUT_ID = "identity-name-input"
    UNAVAILABLE_ID = "identity-name-unavailable"
    FIXED_ROW_WIDTH = 19  # two-cell gutter, 13-cell label, two brackets.

    def __init__(self, *, widget_id: str) -> None:
        super().__init__(
            id=widget_id,
            classes="identity-name-control connection-action-row",
        )
        self.editing = False
        self._pre_edit_value = ""

    def compose(self) -> ComposeResult:
        yield Static(" ", classes="connection-selection-gutter", markup=False)
        yield Static(self.LABEL, classes="connection-label", markup=False)
        yield Static("[ ", classes="identity-bracket", markup=False)
        yield Input(
            id=self.INPUT_ID,
            max_length=self.MAX_UTF8_BYTES,
            disabled=True,
        )
        yield Static(" ]", classes="identity-bracket", markup=False)
        yield Static(
            "...",
            id=self.UNAVAILABLE_ID,
            classes="identity-name-unavailable",
            markup=False,
        )

    @property
    def editor(self) -> Input:
        return self.query_one(f"#{self.INPUT_ID}", Input)

    def set_available(self, value: str, *, force_value: bool = False) -> None:
        """Show a confirmed value while keeping navigation mode arrow-safe."""
        self.disabled = False
        self.editor.display = True
        self.query_one(f"#{self.UNAVAILABLE_ID}", Static).display = False
        for bracket in self.query(".identity-bracket"):
            bracket.display = True
        if force_value or not self.editing:
            self.editor.value = value
        if not self.editing:
            self.editor.disabled = True
        self._resize_field()

    def set_unavailable(self, placeholder: str) -> None:
        """Leave editing and show a truthful unavailable value."""
        if self.editing:
            self.cancel_edit()
        self.editor.value = ""
        self.editor.disabled = True
        self.editor.display = False
        for bracket in self.query(".identity-bracket"):
            bracket.display = False
        unavailable = self.query_one(f"#{self.UNAVAILABLE_ID}", Static)
        unavailable.update(placeholder)
        unavailable.display = True
        self.disabled = True
        self._update_label()

    def begin_edit(self) -> None:
        """Enable and focus the real text input for one edit session."""
        if self.disabled or self.editing:
            return
        self._pre_edit_value = self.editor.value
        self.editing = True
        self.add_class("editing")
        self.editor.disabled = False
        self._update_label()
        self.editor.focus()
        self.editor.cursor_position = len(self.editor.value)
        self._resize_field()

    def finish_edit(self, value: str) -> None:
        """Commit the displayed value and return to navigation mode."""
        self.editor.value = value
        self.editing = False
        self.remove_class("editing")
        self.editor.disabled = True
        self._update_label()
        self.focus()
        self._resize_field()

    def cancel_edit(self) -> None:
        """Restore the pre-edit value and return to navigation mode."""
        if self.editing:
            self.editor.value = self._pre_edit_value
        self.editing = False
        self.remove_class("editing")
        self.editor.disabled = True
        self._update_label()
        if not self.disabled:
            self.focus()
        self._resize_field()

    def _update_label(self, *, focused: bool | None = None) -> None:
        navigation_focus = self.has_focus if focused is None else focused
        marker = ">" if (navigation_focus or self.editing) and not self.disabled else " "
        self.query_one(".connection-selection-gutter", Static).update(marker)

    def _resize_field(self) -> None:
        if not self.is_mounted:
            return
        desired = max(self.MIN_FIELD_WIDTH, cell_len(self.editor.value))
        desired = min(desired, self.MAX_UTF8_BYTES)
        available = max(1, self.size.width - self.FIXED_ROW_WIDTH)
        self.editor.styles.width = min(desired, available)

    def on_focus(self, _event: Focus) -> None:
        self._update_label(focused=True)

    def on_blur(self) -> None:
        self._update_label(focused=False)

    def on_resize(self) -> None:
        self._resize_field()

    @on(Input.Changed)
    def resize_for_value(self) -> None:
        self._resize_field()

    def on_key(self, event: Key) -> None:
        if event.key == "enter" and not self.editing and not self.disabled:
            self.begin_edit()
            event.stop()

    @on(Click)
    def clicked(self) -> None:
        if not self.editing and not self.disabled:
            self.focus()


class LongNameControl(IdentityNameControl):
    """Meshtastic Long Name navigation/edit control."""

    LABEL = "LONG NAME"
    INPUT_ID = "long-name-input"
    UNAVAILABLE_ID = "identity-long-name-unavailable"

    def __init__(self) -> None:
        super().__init__(widget_id="identity-long-name")


class ShortNameControl(IdentityNameControl):
    """Meshtastic Short Name navigation/edit control."""

    LABEL = "SHORT NAME"
    INPUT_ID = "short-name-input"
    UNAVAILABLE_ID = "identity-short-name-unavailable"
    MIN_FIELD_WIDTH = 4
    MAX_UTF8_BYTES = SHORT_NAME_MAX_UTF8_BYTES

    def __init__(self) -> None:
        super().__init__(widget_id="identity-short-name")


class ChatEntryWidget(Vertical):
    """One chat message whose relative timestamp can refresh in place."""

    can_focus = True

    class SelectionChanged(Message):
        def __init__(self, widget: "ChatEntryWidget", selected: bool) -> None:
            super().__init__()
            self.widget = widget
            self.selected = selected

    class UserMenuRequested(Message):
        def __init__(self, widget: "ChatEntryWidget") -> None:
            super().__init__()
            self.widget = widget

    def __init__(
        self,
        entry: ChatEntry,
        now: float | None = None,
        favorite: bool = False,
    ) -> None:
        self.entry = entry
        self.favorite = favorite and not entry.outgoing
        initial_now = monotonic() if now is None else now
        is_new = self.entry.is_new and not self.entry.outgoing
        self.timestamp_label = Static(
            self._timestamp_text(initial_now),
            classes="chat-entry-timestamp",
            markup=False,
        )
        header_parts: list[Static] = [
            Static(self.entry.author, classes="chat-entry-author", markup=False),
            Static(" / ", classes="chat-entry-separator", markup=False),
        ]
        self.delivery_label: Static | None = None
        if self.entry.outgoing:
            self.delivery_label = Static(
                "",
                classes="chat-entry-delivery",
                markup=False,
            )
            header_parts.extend(
                [
                    self.delivery_label,
                    Static(" / ", classes="chat-entry-separator", markup=False),
                ]
            )
        header_parts.append(self.timestamp_label)
        self.distance_label: Static | None = None
        if not self.entry.outgoing and self.entry.distance_miles is not None:
            self.distance_label = Static(
                format_distance_miles(self.entry.distance_miles),
                classes="chat-entry-distance",
                markup=False,
            )
            header_parts.extend(
                [
                    Static(" / ", classes="chat-entry-separator", markup=False),
                    self.distance_label,
                ]
            )
        header = Horizontal(*header_parts, classes="chat-entry-header")
        # Flag-pair wrap-severing (a regional-indicator pair split
        # across a wrap boundary, corrupting whatever renders to its
        # right -- here, the transcript's scrollbar) is fixed at the
        # rendering boundary via grapheme_text.install_flag_pair_protection(),
        # called once at module import time -- the text here is never
        # touched. Selection styling likewise never touches this text
        # or its width -- see ChatEntryWidget.on_focus/on_blur, which
        # only ever update the separate, fixed-width selection_marker.
        self.message_label = Static(
            self.entry.text,
            classes="chat-entry-text",
            markup=False,
        )
        self.selection_marker = Static(" ", classes="chat-selection-marker", markup=False)
        self.action_control = MessageActionControl(entry)
        self.action_control.display = False
        classes = "chat-entry new-message" if is_new else "chat-entry"
        if self.favorite:
            classes += " favorite-sender"
        super().__init__(
            Horizontal(
                self.selection_marker,
                Vertical(header, self.message_label, classes="chat-entry-content"),
                classes="chat-entry-row",
            ),
            self.action_control,
            classes=classes,
        )
        self.refresh_delivery_state(1)

    def refresh_timestamp(self, now: float) -> None:
        """Update only the existing timestamp child for this entry."""
        self.timestamp_label.update(self._timestamp_text(now))

    def _timestamp_text(self, now: float) -> str:
        age = format_relative_age(now - self.entry.age_reference)
        return f"RX {age}" if self.entry.age_is_receive_time else age

    def refresh_new_message_state(self) -> None:
        """Apply the entry's persistent new/read presentation state."""
        self.set_class(self.entry.is_new and not self.entry.outgoing, "new-message")

    def set_favorite(self, favorite: bool) -> None:
        self.favorite = favorite and not self.entry.outgoing
        self.set_class(self.favorite, "favorite-sender")

    def refresh_delivery_state(self, dot_count: int) -> None:
        if self.delivery_label is None:
            return
        internal_state = self.entry.delivery_state or DeliveryState.SENT
        visible_state = (
            DeliveryState.SENDING
            if internal_state in (DeliveryState.SENDING, DeliveryState.SENT)
            else internal_state
        )
        text = visible_state.value
        if visible_state is DeliveryState.SENDING:
            text += "." * dot_count
        self.delivery_label.update(text)
        for name in DeliveryState:
            self.set_class(
                name is visible_state,
                f"delivery-{name.value.lower()}",
            )
        self.action_control.display = can_manual_resend(self.entry)

    def on_focus(self, _event: Focus) -> None:
        self.selection_marker.update(">")
        self.post_message(self.SelectionChanged(self, True))

    def on_blur(self, _event: object) -> None:
        self.selection_marker.update(" ")
        self.post_message(self.SelectionChanged(self, False))

    def on_click(self, _event: Click) -> None:
        """Give mouse selection the same acknowledgement semantics as focus."""
        self.focus()

    def on_key(self, event: Key) -> None:
        if event.key == "enter" and not self.entry.outgoing:
            if getattr(self.app, "_user_menu", None) is None:
                self.post_message(self.UserMenuRequested(self))
                event.stop()


class MeshtasticPassApp(App[None]):
    """The first MeshtasticPass terminal UI shell."""

    TITLE = "MeshtasticPass"
    CSS = f"""
    $white_dim: {THEME_PALETTES["white"].dim_base};
    $green_dim: {THEME_PALETTES["green"].dim_base};
    $orange_dim: {THEME_PALETTES["orange"].dim_base};
    $white_accent: {THEME_PALETTES["white"].accent};
    $green_accent: {THEME_PALETTES["green"].accent};
    $orange_accent: {THEME_PALETTES["orange"].accent};
    $error: {ERROR};
    $selection_background: #181818;
    """ + """
    Screen {
        background: #101010;
        color: #d8d8d8;
        layers: base popup;
    }

    Screen.theme-green {
        color: #39ff14;
    }

    Screen.theme-orange {
        color: #ff8c00;
    }

    #tab-bar {
        height: 3;
        padding: 1 1 0 1;
        background: #101010;
        color: $white_dim;
    }

    #content {
        height: 1fr;
        padding: 0 2;
    }

    .tab-page {
        height: 1fr;
    }

    #connection {
        overflow-x: hidden;
        scrollbar-size: 1 1;
        scrollbar-color: #d8d8d8;
        scrollbar-color-hover: #d8d8d8;
        scrollbar-color-active: #d8d8d8;
        scrollbar-background: $white_dim;
        scrollbar-background-hover: $white_dim;
        scrollbar-background-active: $white_dim;
    }

    .page-title {
        height: 2;
        color: #f2f2f2;
        text-style: bold;
    }

    #connection-status, #connection-details, #identity-values {
        height: auto;
        min-height: 1;
        overflow-x: hidden;
    }

    #style-title {
        margin-top: 1;
    }

    .identity-name-control {
        height: 1;
        width: 1fr;
        overflow-x: hidden;
    }

    .connection-selection-gutter {
        width: 2;
        height: 1;
    }

    .connection-label {
        width: 13;
        height: 1;
    }

    .identity-bracket {
        width: auto;
        height: 1;
    }

    .identity-name-unavailable {
        width: auto;
        height: 1;
        color: $white_dim;
    }

    #long-name-input, #short-name-input {
        width: 8;
        height: 1;
        border: none;
        padding: 0;
        background: #101010;
        color: #d8d8d8;
    }

    #long-name-input:disabled, #short-name-input:disabled {
        color: $white_dim;
        opacity: 1;
    }

    #identity-status {
        height: auto;
        min-height: 1;
    }

    #identity-status {
        color: #39ff14;
    }

    #identity-status.setting-error {
        color: $error;
    }

    Screen.theme-green #long-name-input,
    Screen.theme-green #short-name-input {
        color: #39ff14;
    }

    Screen.theme-orange #long-name-input,
    Screen.theme-orange #short-name-input {
        color: #ff8c00;
    }

    .keyboard-dropdown {
        height: auto;
        min-height: 2;
        color: #d8d8d8;
    }

    .keyboard-dropdown:focus {
        color: #f2f2f2;
    }

    #connection .connection-action-row {
        height: 1;
        min-height: 1;
        width: 1fr;
        overflow-x: hidden;
    }

    .chat-entry:focus,
    #connection .connection-action-row:focus,
    #connection .identity-name-control.editing {
        background: $selection_background;
    }

    Screen.theme-green .keyboard-dropdown,
    Screen.theme-green #chat-input {
        color: #39ff14;
    }

    Screen.theme-orange .keyboard-dropdown,
    Screen.theme-orange #chat-input {
        color: #ff8c00;
    }

    Screen.theme-green .keyboard-dropdown:focus,
    Screen.theme-green .page-title {
        color: #7cff6b;
    }

    Screen.theme-orange .keyboard-dropdown:focus,
    Screen.theme-orange .page-title {
        color: #ffb000;
    }

    #chat-title {
        height: auto;
        min-height: 2;
        text-style: bold;
    }

    #style-status {
        height: 2;
    }

    #style-status.setting-success {
        color: $white_accent;
    }

    Screen.theme-green #style-status.setting-success {
        color: $green_accent;
    }

    Screen.theme-orange #style-status.setting-success {
        color: $orange_accent;
    }

    #connection-error, #send-error, #style-status.setting-error {
        height: auto;
        min-height: 1;
        color: $error;
    }

    #send-error.older-message-notice {
        color: #39ff14;
    }

    Screen.theme-green #send-error.older-message-notice {
        color: #ff8c00;
    }

    Screen.theme-orange #send-error.older-message-notice {
        color: #d8d8d8;
    }

    #chat-log {
        height: 1fr;
        scrollbar-size: 1 1;
        scrollbar-color: #d8d8d8;
        scrollbar-color-hover: #d8d8d8;
        scrollbar-color-active: #d8d8d8;
        scrollbar-background: $white_dim;
        scrollbar-background-hover: $white_dim;
        scrollbar-background-active: $white_dim;
    }

    Screen.theme-green #chat-log, Screen.theme-green #connection {
        scrollbar-color: #39ff14;
        scrollbar-color-hover: #39ff14;
        scrollbar-color-active: #39ff14;
        scrollbar-background: $green_dim;
        scrollbar-background-hover: $green_dim;
        scrollbar-background-active: $green_dim;
    }

    Screen.theme-orange #chat-log, Screen.theme-orange #connection {
        scrollbar-color: #ff8c00;
        scrollbar-color-hover: #ff8c00;
        scrollbar-color-active: #ff8c00;
        scrollbar-background: $orange_dim;
        scrollbar-background-hover: $orange_dim;
        scrollbar-background-active: $orange_dim;
    }

    #mesh-status, #mesh-context-status {
        height: 1;
    }

    #mesh-status {
        color: $white_dim;
    }

    Screen.theme-green #mesh-status {
        color: $green_dim;
    }

    Screen.theme-orange #mesh-status {
        color: $orange_dim;
    }

    #mesh-context-status {
        color: #d8d8d8;
    }

    Screen.theme-green #mesh-context-status {
        color: #39ff14;
    }

    Screen.theme-orange #mesh-context-status {
        color: #ff8c00;
    }

    #mesh-view {
        height: 1fr;
        /* Horizontal centering is applied explicitly in code as a
           board-level offset (see MeshTopologyView.set_nodes), not
           via CSS align, so it stays exact regardless of board width
           parity; vertical centering is still fine left to CSS. */
        align: left middle;
    }

    #mesh-board {
        position: relative;
        min-width: 1;
        min-height: 1;
        layers: canvas nodes;
    }

    .mesh-canvas {
        layer: canvas;
        position: absolute;
        offset: 0 0;
    }

    .mesh-node {
        layer: nodes;
        position: absolute;
        content-align: center middle;
    }

    Screen.theme-green .identity-name-unavailable {
        color: $green_dim;
    }

    Screen.theme-orange .identity-name-unavailable {
        color: $orange_dim;
    }

    .viewport-menu {
        layer: popup;
        position: absolute;
        background: #101010;
        border: solid $white_dim;
        padding: 0;
        scrollbar-size: 1 1;
        scrollbar-color: #d8d8d8;
        scrollbar-background: $white_dim;
    }

    .viewport-menu-row {
        height: 1;
        padding: 0 1;
        color: #d8d8d8;
    }

    .viewport-menu-row.highlighted {
        color: #39ff14;
        text-style: bold reverse;
    }

    .viewport-menu-row.informational {
        color: $white_dim;
    }

    Screen.theme-green .viewport-menu {
        border: solid $green_dim;
        scrollbar-color: #39ff14;
        scrollbar-background: $green_dim;
    }

    Screen.theme-green .viewport-menu-row {
        color: #39ff14;
    }

    Screen.theme-green .viewport-menu-row.highlighted {
        color: #ff8c00;
    }

    Screen.theme-green .viewport-menu-row.informational {
        color: $green_dim;
    }

    Screen.theme-orange .viewport-menu {
        border: solid $orange_dim;
        scrollbar-color: #ff8c00;
        scrollbar-background: $orange_dim;
    }

    Screen.theme-orange .viewport-menu-row {
        color: #ff8c00;
    }

    Screen.theme-orange .viewport-menu-row.highlighted {
        color: #d8d8d8;
    }

    Screen.theme-orange .viewport-menu-row.informational {
        color: $orange_dim;
    }

    #load-older, .message-action {
        width: auto;
        height: 1;
        color: #d8d8d8;
        margin-bottom: 1;
    }

    #end-of-chat-history {
        width: 1fr;
        height: 1;
        margin-bottom: 1;
        color: $white_dim;
        text-align: center;
    }

    Screen.theme-green #end-of-chat-history {
        color: $green_dim;
    }

    Screen.theme-orange #end-of-chat-history {
        color: $orange_dim;
    }

    #load-older:focus, .message-action:focus {
        text-style: reverse;
    }

    Screen.theme-green #load-older,
    Screen.theme-green .message-action {
        color: #39ff14;
    }

    Screen.theme-orange #load-older,
    Screen.theme-orange .message-action {
        color: #ff8c00;
    }

    .chat-entry {
        height: auto;
        margin-bottom: 1;
        padding-right: 1;
    }

    .chat-entry-row, .chat-entry-content {
        width: 1fr;
        height: auto;
    }

    .chat-selection-marker {
        width: 2;
        height: 1;
    }

    .message-action {
        margin-left: 2;
    }

    .chat-entry-header {
        height: 1;
    }

    .chat-entry-author {
        width: auto;
        text-style: bold;
    }

    Screen.theme-white .chat-entry.favorite-sender .chat-entry-author {
        color: #39ff14;
    }

    Screen.theme-green .chat-entry.favorite-sender .chat-entry-author {
        color: #ff8c00;
    }

    Screen.theme-orange .chat-entry.favorite-sender .chat-entry-author {
        color: #d8d8d8;
    }

    .chat-entry-separator, .chat-entry-delivery {
        width: auto;
        color: $white_dim;
    }

    .chat-entry-timestamp, .chat-entry-distance {
        width: auto;
        color: $white_dim;
        text-style: dim;
    }

    Screen.theme-green .chat-entry-timestamp,
    Screen.theme-green .chat-entry-distance {
        color: $green_dim;
    }

    Screen.theme-orange .chat-entry-timestamp,
    Screen.theme-orange .chat-entry-distance {
        color: $orange_dim;
    }

    Screen.theme-green .chat-entry-separator,
    Screen.theme-green .chat-entry-delivery {
        color: $green_dim;
    }

    Screen.theme-orange .chat-entry-separator,
    Screen.theme-orange .chat-entry-delivery {
        color: $orange_dim;
    }

    .chat-entry.delivery-sending .chat-entry-delivery,
    .chat-entry.delivery-unconfirmed .chat-entry-delivery {
        color: #39ff14;
    }

    Screen.theme-green .chat-entry.delivery-sending .chat-entry-delivery,
    Screen.theme-green .chat-entry.delivery-unconfirmed .chat-entry-delivery {
        color: #ff8c00;
    }

    Screen.theme-orange .chat-entry.delivery-sending .chat-entry-delivery,
    Screen.theme-orange .chat-entry.delivery-unconfirmed .chat-entry-delivery {
        color: #d8d8d8;
    }

    .chat-entry.delivery-heard .chat-entry-delivery {
        color: #d8d8d8;
    }

    Screen.theme-green .chat-entry.delivery-heard .chat-entry-delivery {
        color: #39ff14;
    }

    Screen.theme-orange .chat-entry.delivery-heard .chat-entry-delivery {
        color: #ff8c00;
    }

    .chat-entry.delivery-failed .chat-entry-delivery,
    .chat-entry.delivery-interrupted .chat-entry-delivery {
        color: $error;
    }

    .chat-entry-text {
        height: auto;
    }

    Screen.theme-white .chat-entry.new-message .chat-entry-author,
    Screen.theme-white .chat-entry.new-message .chat-entry-timestamp,
    Screen.theme-white .chat-entry.new-message .chat-entry-distance,
    Screen.theme-white .chat-entry.new-message .chat-entry-text {
        color: #39ff14;
    }

    Screen.theme-green .chat-entry.new-message .chat-entry-author,
    Screen.theme-green .chat-entry.new-message .chat-entry-timestamp,
    Screen.theme-green .chat-entry.new-message .chat-entry-distance,
    Screen.theme-green .chat-entry.new-message .chat-entry-text {
        color: #ff8c00;
    }

    Screen.theme-orange .chat-entry.new-message .chat-entry-author,
    Screen.theme-orange .chat-entry.new-message .chat-entry-timestamp,
    Screen.theme-orange .chat-entry.new-message .chat-entry-distance,
    Screen.theme-orange .chat-entry.new-message .chat-entry-text {
        color: #d8d8d8;
    }

    #chat-input {
        height: 1;
        border: none;
        padding: 0;
        background: #101010;
        color: #f2f2f2;
    }

    #chat-input:focus {
        border: none;
    }

    #chat-new-below {
        height: 1;
        color: #ffb000;
        text-align: right;
    }

    #footer {
        height: 2;
        padding: 0 1;
        border-top: solid $white_dim;
        color: $white_dim;
    }

    Screen.theme-green #tab-bar,
    Screen.theme-green #footer {
        color: $green_dim;
    }

    Screen.theme-orange #tab-bar,
    Screen.theme-orange #footer {
        color: $orange_dim;
    }

    Screen.theme-green #footer {
        border-top: solid $green_dim;
    }

    Screen.theme-orange #footer {
        border-top: solid $orange_dim;
    }
    """

    def __init__(
        self,
        radio: object,
        settings: AppSettings | None = None,
        terminal_cursor: TerminalCursor | None = None,
        chat_store: ChatStore | None = None,
        history_error: str = "",
    ) -> None:
        super().__init__()
        self.radio = radio
        self.settings = settings or AppSettings.load()
        self._current_theme = self.settings.color
        self.current_tab = "connection"
        self._channels: tuple[ChannelInfo, ...] = (ChannelInfo(0, "Channel 1"),)
        self.current_channel_index = 0
        self._channel_states: dict[int, ChannelChatState] = {
            0: ChannelChatState()
        }
        self.chat_history = self._channel_states[0].entries
        self.unread_count = 0
        self.transcript_new_count = 0
        self.chat_store = chat_store
        self._history_error = history_error
        self._radio_state = RadioState.CONNECTING
        self._radio_info: RadioInfo | None = None
        self._status_dot_count = 1
        self._connection_animation_timer: Timer | None = None
        self._chat_timestamp_timer: Timer | None = None
        self._delivery_timer: Timer | None = None
        self._send_error_message = ""
        self._send_error_dismiss_timer: Timer | None = None
        self._arrival_sequence = 0
        self._send_dot_count = 1
        self._has_older_history = False
        self._mounted_chat_target = DEFAULT_HISTORY_LIMIT
        self._chat_open_scroll_pending = False
        self._user_menu: ViewportMenu | None = None
        self._user_menu_origin: Widget | None = None
        self._user_menu_scroll_target: ScrollableContainer | None = None
        self._user_menu_scroll_x: float | None = None
        self._user_menu_scroll_y: float | None = None
        self._terminal_cursor = terminal_cursor or TerminalCursor()
        self._monitor = RadioMonitor(
            radio,
            self._radio_event_from_thread,
            self._message_from_thread,
        )

    def compose(self) -> ComposeResult:
        try:
            devices = tuple(self.radio.available_device_paths())
        except Exception:
            devices = ()
        yield Static(id="tab-bar")
        with ContentSwitcher(initial="connection", id="content"):
            with ConnectionPage(id="connection", classes="tab-page"):
                yield Static("CONNECTION", classes="page-title")
                yield Static(id="connection-status")
                yield DeviceSelector(self.settings.device_path, devices)
                yield Static(id="connection-details")
                yield Static(id="connection-error")
                yield LongNameControl()
                yield ShortNameControl()
                yield Static(id="identity-values", markup=False)
                yield Static(id="identity-status", markup=False)
                yield Static("STYLE", id="style-title", classes="page-title")
                yield FontSizeSelector(self.settings.font_size)
                yield ColorSelector(self.settings.color)
                yield Static(id="style-status")
            with Vertical(id="chat", classes="tab-page"):
                yield ChannelSelector(self._channels, self.current_channel_index)
                yield ChatTranscript(id="chat-log")
                yield Static(id="chat-new-below")
                yield Static(id="send-error")
                yield ChatMessageInput(
                    placeholder="> message",
                    id="chat-input",
                    select_on_focus=False,
                )
            with Vertical(id="profile", classes="tab-page"):
                yield Static("> PROFILE", classes="page-title")
                yield Static("Coming in a future milestone.")
            with Vertical(id="mesh", classes="tab-page"):
                # Shown/hidden and populated by _update_chat_connection_state()
                # with the exact same _connection_status_text() CHAT's
                # heading uses -- never MESH-specific terminology. Not the
                # removed permanent "> MESH · ACTIVE N" heading: this
                # exists ONLY while a connection state needs to be
                # communicated, and collapses to nothing otherwise.
                yield Static(id="mesh-connection-status", classes="page-title", markup=False)
                yield Static(id="mesh-status", markup=False)
                yield MeshTopologyView()
                yield Static(id="mesh-context-status", markup=False)
        yield Static("1-3 switch tabs    F4 quit", id="footer")

    def on_mount(self) -> None:
        self._terminal_cursor.hide()
        self._apply_color_theme(self.settings.color)
        self._update_tab_bar()
        self._show_connection(RadioState.CONNECTING)
        self._connection_animation_timer = self.set_interval(
            0.45,
            self._advance_connection_animation,
            name="connection-status-animation",
        )
        self._chat_timestamp_timer = self.set_interval(
            1.0,
            self._refresh_chat_timestamps,
            name="chat-relative-timestamps",
        )
        self._delivery_timer = self.set_interval(
            0.45,
            self._advance_delivery_states,
            name="chat-delivery-states",
        )
        self._load_chat_history()
        if self._history_error:
            self._show_send_error(self._history_error)
        self.query_one(FontSizeSelector).focus()
        self._monitor.start()

    def on_unmount(self) -> None:
        if self._connection_animation_timer is not None:
            self._connection_animation_timer.stop()
        if self._chat_timestamp_timer is not None:
            self._chat_timestamp_timer.stop()
        if self._delivery_timer is not None:
            self._delivery_timer.stop()
        if self._send_error_dismiss_timer is not None:
            self._send_error_dismiss_timer.stop()
        self.restore_terminal_cursor()
        self._monitor.stop()
        self._reconcile_interrupted_sends_before_shutdown()
        if self.chat_store is not None:
            self.chat_store.close()

    def _reconcile_interrupted_sends_before_shutdown(self) -> None:
        """Persist INTERRUPTED for every still-SENDING outgoing message

        this process owns, before the store closes.

        This is additional cleanup, not the authoritative fix:
        correctness no longer depends on it. ChatStore.open() now
        rewrites any abandoned SENDING row to INTERRUPTED directly in
        SQLite the moment a store is opened -- covering every row in
        the database regardless of channel, pagination, or whether
        anything is currently loaded into memory (see
        ChatStore.reconcile_abandoned_sending()), which is what actually
        repairs a row this process never even loads. What this method
        adds on top: while this process is still running, catching a
        send up front (before the NEXT process's startup reconciliation
        would) means a row this process gives up on shows INTERRUPTED
        immediately rather than sitting as SENDING until the next
        restart. Runs after _monitor.stop() so no late radio callback
        can race a write in after this pass runs. Every channel's
        entries are checked, not just the currently displayed one -- a
        send can be left in flight in a channel the user has since
        switched away from.
        """
        if self.chat_store is None:
            return
        for state in self._channel_states.values():
            for entry in state.entries:
                if not entry.outgoing or entry.delivery_state is not DeliveryState.SENDING:
                    continue
                entry.delivery_state = DeliveryState.INTERRUPTED
                if entry.message_id is None:
                    continue
                try:
                    self.chat_store.update_delivery_state(
                        entry.message_id,
                        DeliveryState.INTERRUPTED.value,
                        attempt_id=entry.active_attempt_id,
                    )
                except ChatStoreError:
                    # Shutting down regardless; there is no user-facing
                    # surface left to report this failure to.
                    pass

    def on_key(self, event: Key) -> None:
        """Handle global keys only while the chat input is not focused."""
        if event.key == "f4":
            self.exit()
            event.stop()
            return
        if self._user_menu is not None:
            if event.key in ("up", "down"):
                self._user_menu.move_highlight(-1 if event.key == "up" else 1)
                event.stop()
            elif event.key == "enter":
                self._user_menu.activate()
                event.stop()
            elif event.key == "escape":
                self._close_user_menu()
                event.stop()
            return
        if isinstance(self.focused, KeyboardDropdown) and self.focused.is_open:
            return
        if isinstance(self.focused, Input):
            if self.focused.id == "chat-input" and event.key == "escape":
                self.query_one("#chat-log", ChatTranscript).focus()
                event.stop()
            elif self.focused.id == "chat-input" and event.key == "up":
                self._move_chat_focus(-1)
                event.stop()
            elif self.focused.id == "long-name-input" and event.key == "escape":
                self.query_one(LongNameControl).cancel_edit()
                event.stop()
            elif self.focused.id == "short-name-input" and event.key == "escape":
                self.query_one(ShortNameControl).cancel_edit()
                event.stop()
            return

        if self.current_tab == "chat":
            transcript = self.query_one("#chat-log", ChatTranscript)
            if event.key in ("up", "down"):
                self._move_chat_focus(-1 if event.key == "up" else 1)
                event.stop()
                return
            if event.key in ("pageup", "pagedown"):
                step = max(1, transcript.region.height - 2)
                transcript.scroll_relative(
                    y=-step if event.key == "pageup" else step
                )
                self.call_after_refresh(self._clear_indicator_if_at_bottom)
                event.stop()
                return
            if event.key.lower() == "c":
                selector = self.query_one(ChannelSelector)
                selector.focus()
                selector.open_menu()
                event.stop()
                return
            if event.key == "left":
                self._focus_oldest_new_message()
                event.stop()
                return
            if event.key == "right":
                self._return_to_present_and_type()
                event.stop()
                return
            if event.key == "end":
                self._jump_to_newest()
                event.stop()
                return

        if self.current_tab == "mesh" and event.key in (
            "up",
            "down",
            "left",
            "right",
        ):
            self._move_mesh_focus(event.key)
            event.stop()
            return

        if self.current_tab == "connection" and event.key in ("up", "down"):
            controls = [
                control
                for control in (
                    self.query_one(DeviceSelector),
                    self.query_one(LongNameControl),
                    self.query_one(ShortNameControl),
                    self.query_one(FontSizeSelector),
                    self.query_one(ColorSelector),
                )
                if not getattr(control, "disabled", False)
            ]
            if self.focused in controls:
                current = controls.index(self.focused)
                step = -1 if event.key == "up" else 1
                target = controls[(current + step) % len(controls)]
                target.focus()
                target.scroll_visible(animate=False)
                event.stop()
                return
            if event.key.lower() == "r" and isinstance(
                self.focused, ChatEntryWidget
            ):
                if can_manual_resend(self.focused.entry):
                    self._rebroadcast(self.focused.entry)
                    event.stop()
                return

        # PROFILE is intentionally absent: hidden from the visible top
        # nav (see TAB_NAMES), so no digit key may reach it -- key "4"
        # is simply not mapped to anything, rather than falling through
        # to the now-hidden tab.
        tab_for_key = {
            "1": "connection",
            "2": "chat",
            "3": "mesh",
        }
        if event.key in tab_for_key:
            self.show_tab(tab_for_key[event.key])
            event.stop()

    @on(Input.Submitted, "#long-name-input")
    def save_long_name(self, event: Input.Submitted) -> None:
        """Apply an identity edit through the active radio service."""
        status = self.query_one("#identity-status", Static)
        control = self.query_one(LongNameControl)
        if self._radio_state is not RadioState.ONLINE or self._radio_info is None:
            control.cancel_edit()
            status.add_class("setting-error")
            status.update("LONG NAME UNAVAILABLE — RADIO NOT CONNECTED")
            return
        try:
            long_name = validate_long_name(event.value)
        except RadioIdentityError as error:
            control.cancel_edit()
            status.add_class("setting-error")
            status.update(str(error))
            return
        control.finish_edit(long_name)
        status.remove_class("setting-error")
        status.update("SAVING NAME...")
        self.run_worker(
            lambda: self._save_long_name_from_thread(long_name),
            thread=True,
            name="save-radio-long-name",
            exclusive=True,
        )

    def _save_long_name_from_thread(self, long_name: str) -> None:
        try:
            info = self.radio.set_long_name(long_name)
        except (RadioIdentityError, AttributeError) as error:
            detail = str(error).strip() or "The radio identity could not be saved."
            self.post_message(IdentitySaveFailed(detail))
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            self.post_message(IdentitySaveFailed(f"Could not save Long Name: {detail}"))
        else:
            self.post_message(IdentitySaved(info, "LONG NAME"))

    @on(Input.Submitted, "#short-name-input")
    def save_short_name(self, event: Input.Submitted) -> None:
        """Apply a Short Name edit through the active radio service."""
        status = self.query_one("#identity-status", Static)
        control = self.query_one(ShortNameControl)
        if self._radio_state is not RadioState.ONLINE or self._radio_info is None:
            control.cancel_edit()
            status.add_class("setting-error")
            status.update("SHORT NAME UNAVAILABLE — RADIO NOT CONNECTED")
            return
        try:
            short_name = validate_short_name(event.value)
        except RadioIdentityError as error:
            control.cancel_edit()
            status.add_class("setting-error")
            status.update(str(error))
            return
        control.finish_edit(short_name)
        status.remove_class("setting-error")
        status.update("SAVING SHORT NAME...")
        self.run_worker(
            lambda: self._save_short_name_from_thread(short_name),
            thread=True,
            name="save-radio-short-name",
            exclusive=True,
        )

    def _save_short_name_from_thread(self, short_name: str) -> None:
        try:
            info = self.radio.set_short_name(short_name)
        except (RadioIdentityError, AttributeError) as error:
            detail = str(error).strip() or "The radio identity could not be saved."
            self.post_message(IdentitySaveFailed(detail))
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            self.post_message(IdentitySaveFailed(f"Could not save Short Name: {detail}"))
        else:
            self.post_message(IdentitySaved(info, "SHORT NAME"))

    @on(IdentitySaved)
    def identity_saved(self, event: IdentitySaved) -> None:
        self._radio_info = event.info
        self._render_identity(force_value=True)
        self._refresh_mesh()
        status = self.query_one("#identity-status", Static)
        status.remove_class("setting-error")
        status.update(f"{event.field_label} SAVED")

    @on(IdentitySaveFailed)
    def identity_save_failed(self, event: IdentitySaveFailed) -> None:
        self._render_identity(force_value=True)
        status = self.query_one("#identity-status", Static)
        status.add_class("setting-error")
        status.update(event.detail)

    def show_tab(self, tab_id: str) -> None:
        if self.current_tab == "chat" and tab_id != "chat":
            self._mark_new_messages_read()
        self.current_tab = tab_id
        if tab_id == "chat":
            self._mark_unread_messages_viewed()
            self._recount_unread()
            self._refresh_chat_timestamps()
        self.query_one("#content", ContentSwitcher).current = tab_id
        self._update_tab_bar()
        if tab_id == "chat":
            self.query_one("#chat-input", Input).focus()
            if self._chat_open_scroll_pending:
                self._chat_open_scroll_pending = False
                self.call_after_refresh(self._jump_to_newest)
        elif tab_id == "connection":
            self._refresh_device_options()
            self.query_one(FontSizeSelector).focus()
        elif tab_id == "mesh":
            self._refresh_mesh()
            # MESH's own arrow-key navigation is handled by the App's
            # on_key -- gated on current_tab, not on any particular
            # widget holding focus (see _move_mesh_focus and
            # test_navigation_works_even_when_focus_is_on_the_board_
            # container) -- EXCEPT that on_key returns early whenever
            # self.focused is an Input, before current_tab is even
            # checked. Arriving here from CHAT left #chat-input focused
            # (CHAT's own branch above claims it and never releases it),
            # so every arrow press on MESH was silently swallowed by
            # that Input check instead of ever reaching _move_mesh_focus.
            # Clearing focus here, exactly like the "profile" fallback
            # below, is what CONNECTION and CHAT already do for
            # themselves by claiming their own widget's focus.
            self.set_focus(None)
        else:
            self.set_focus(None)
        self._update_footer()

    @on(KeyboardDropdown.Selected)
    async def dropdown_selected(self, event: KeyboardDropdown.Selected) -> None:
        if event.setting_name == "channel_index":
            await self._switch_channel(int(event.value))
            return
        if event.setting_name == "device_path":
            await self._switch_device(str(event.value))
            return

        status = self.query_one("#style-status", Static)
        try:
            if event.setting_name == "font_size":
                self.settings.set_font_size(int(event.value))
                self.settings.save()
                self.settings.update_lxterminal_profile()
            elif event.setting_name == "color":
                self.settings.set_color(str(event.value))
                self.settings.save()
                self._apply_color_theme(self.settings.color)
        except (OSError, ValueError) as error:
            status.remove_class("setting-success")
            status.add_class("setting-error")
            status.update(f"SETTING NOT SAVED — {error}")
        else:
            status.remove_class("setting-error")
            status.add_class("setting-success")
            message = (
                "FONT SIZE SAVED — APPLIES ON NEXT LAUNCH"
                if event.setting_name == "font_size"
                else "COLOR SAVED"
            )
            status.update(message)

    async def _switch_device(self, device_path: str) -> None:
        if device_path == getattr(self.radio, "device_path", None):
            return
        status = self.query_one("#connection-error", Static)
        try:
            self.settings.set_device_path(device_path)
            self.settings.save()
            self._monitor.stop()
            self.radio.set_device_path(device_path)
            self._monitor = RadioMonitor(
                self.radio,
                self._radio_event_from_thread,
                self._message_from_thread,
            )
            self._show_connection(RadioState.CONNECTING)
            self._monitor.start()
        except (OSError, ValueError) as error:
            status.update(f"USB DEVICE NOT CHANGED — {error}")

    def _refresh_device_options(self) -> None:
        try:
            devices = tuple(self.radio.available_device_paths())
        except Exception:
            devices = ()
        selector = self.query_one(DeviceSelector)
        selector.set_options(
            (DropdownOption(path, path) for path in devices),
            value=getattr(self.radio, "device_path", self.settings.device_path),
        )

    def _apply_color_theme(self, color: str) -> None:
        self._current_theme = color
        for name, _value in COLOR_CHOICES:
            self.screen.remove_class(f"theme-{name.lower()}")
        self.screen.add_class(f"theme-{color}")
        palette = THEME_PALETTES[color]
        for dropdown in self.query(KeyboardDropdown):
            dropdown.set_palette(
                palette.base,
                palette.accent,
                palette.dim_base,
            )
        if len(self.query("#identity-values")):
            self._render_connection_details()
            self._render_identity()
        if len(self.query("#mesh-view")):
            self._refresh_mesh()

    @on(Input.Submitted, "#chat-input")
    def send_chat_message(self, event: Input.Submitted) -> None:
        # Belt-and-suspenders: a disabled Input should already refuse to
        # focus/submit, but this guarantees sending stays refused even
        # if focus was already on the input at the moment reconnecting
        # began (see _update_chat_connection_state).
        if self._radio_state is not RadioState.ONLINE:
            return
        text = event.value
        if not text.strip():
            self._show_send_error("Message text cannot be empty.")
            return
        entry = self._start_outgoing(text)
        generation = entry.send_generation
        self.run_worker(
            lambda: self._send_from_thread(entry, generation),
            thread=True,
        )

    @on(Input.Changed, "#chat-input")
    def chat_input_changed(self, _event: Input.Changed) -> None:
        """Typing dismisses the empty-message error immediately."""
        if self._send_error_message:
            self._show_send_error("")

    @on(ChatMessageInput.Left)
    def chat_input_lost_focus(self, _event: ChatMessageInput.Left) -> None:
        """Leaving the message-entry box dismisses the empty-message

        error immediately -- this also covers switching to a different
        top-level tab, since show_tab() moves focus away from here.
        """
        if self._send_error_message:
            self._show_send_error("")

    def _send_from_thread(
        self,
        entry: ChatEntry,
        generation: int | None = None,
    ) -> None:
        attempt_generation = entry.send_generation if generation is None else generation

        def status_handler(status: SendStatus) -> None:
            self.post_message(
                DeliveryStatusReceived(entry, status, attempt_generation)
            )

        try:
            sent = self.radio.send_text(
                entry.text,
                channel_index=entry.channel_index,
                status_handler=status_handler,
            )
        except RadioSendError as error:
            self.post_message(SendFailed(entry, str(error), attempt_generation))
        else:
            self.post_message(SendSubmitted(entry, sent, attempt_generation))

    def _start_outgoing(self, text: str) -> ChatEntry:
        self._mark_current_channel_read_for_send()
        entry = outgoing_chat_entry(
            text,
            channel_index=self.current_channel_index,
            delivery_state=DeliveryState.SENDING,
        )
        entry.send_generation = 1
        self._assign_arrival_order(entry)
        self._persist_outgoing(entry)
        insert_index = self._insert_entry_in_order(self.chat_history, entry)
        self._insert_chat_widget(entry, insert_index, older=False)
        chat_input = self.query_one("#chat-input", Input)
        chat_input.value = ""
        self._show_send_error("")
        return entry

    def _accepted_send(self, text: str) -> None:
        """Compatibility helper for a locally accepted outgoing entry."""
        self._mark_current_channel_read_for_send()
        entry = outgoing_chat_entry(
            text,
            channel_index=self.current_channel_index,
            delivery_state=DeliveryState.SENT,
        )
        self._assign_arrival_order(entry)
        self._persist_outgoing(entry)
        insert_index = self._insert_entry_in_order(self.chat_history, entry)
        self._insert_chat_widget(entry, insert_index, older=False)
        chat_input = self.query_one("#chat-input", Input)
        chat_input.value = ""
        self._show_send_error("")

    @on(SendSubmitted)
    def send_was_submitted(self, event: SendSubmitted) -> None:
        entry = event.entry
        if event.generation != entry.send_generation:
            return
        entry.packet_id = event.sent.packet_id
        state = (
            entry.delivery_state
            if entry.delivery_state in (DeliveryState.HEARD, DeliveryState.FAILED)
            else event.sent.immediate_state or DeliveryState.SENT
        )
        entry.confirmation_deadline = (
            monotonic() + CHAT_CONFIRMATION_TIMEOUT_SECONDS
            if state is DeliveryState.SENT
            else None
        )
        self._set_delivery_state(entry, state, packet_id=event.sent.packet_id)

    @on(SendFailed)
    def send_failed(self, event: SendFailed) -> None:
        if event.generation != event.entry.send_generation:
            return
        self._set_delivery_state(
            event.entry,
            DeliveryState.FAILED,
            detail=event.detail,
        )
        self._show_send_error(event.detail)

    @on(DeliveryStatusReceived)
    def delivery_status_received(self, event: DeliveryStatusReceived) -> None:
        if event.generation != event.entry.send_generation:
            return
        if event.entry.packet_id not in (None, event.status.packet_id):
            return
        self._set_delivery_state(
            event.entry,
            event.status.state,
            packet_id=event.status.packet_id,
            detail=event.status.detail,
        )

    def _radio_event_from_thread(self, event: RadioEvent) -> None:
        try:
            self.call_from_thread(self._apply_radio_event, event)
        except RuntimeError:
            # The UI may already be stopping while the monitor exits.
            pass

    def _message_from_thread(self, message: ReceivedMessage) -> None:
        self.post_message(RadioMessageReceived(message))

    @on(RadioMessageReceived)
    def accept_radio_message(self, event: RadioMessageReceived) -> None:
        self._accept_received_message(event.message)

    def _apply_radio_event(self, event: RadioEvent) -> None:
        self._show_connection(event.state, event.info, event.message)

    def _accept_received_message(self, message: ReceivedMessage) -> None:
        try:
            self._refresh_mesh()
        except Exception:
            # A passive-topology refresh problem must never be able to
            # silently swallow a legitimate incoming CHAT message --
            # persistence below still has to run regardless.
            pass
        channel_index = message.channel_index or 0
        state = self._ensure_channel_loaded(channel_index)
        app_received_at = time()
        monotonic_now = monotonic()
        chat_is_visible = (
            self.current_tab == "chat"
            and channel_index == self.current_channel_index
        )
        entry = received_chat_entry(
            message,
            app_received_at=app_received_at,
            monotonic_now=monotonic_now,
            unread=not chat_is_visible,
            is_new=True,
        )
        self._assign_arrival_order(entry)
        inserted = self._persist_incoming(entry)
        if rx_debug_enabled():
            if entry.message_id is not None:
                rx_debug_log(
                    f"CHAT STORE id={entry.message_id} channel={channel_index} "
                    + ("inserted" if inserted else "duplicate, ignored")
                )
            else:
                rx_debug_log(
                    f"CHAT STORE not persisted channel={channel_index} "
                    "reason=no_chat_store_attached"
                )
        if not inserted:
            return
        tail_key = state.entries[-1].order_key if state.entries else None
        is_older = (
            entry.message_time is not None
            and tail_key is not None
            and entry.order_key < tail_key
        )
        insert_index = self._ordered_insert_index(state.entries, entry)
        outside_mounted_window = (
            bool(state.entries)
            and insert_index == 0
            and entry.order_key < state.entries[0].order_key
            and (
                state.has_older_history
                or len(state.entries) >= state.mounted_target
            )
        )
        if entry.message_id is not None:
            state.new_message_ids.add(entry.message_id)
            if not chat_is_visible:
                state.unread_message_ids.add(entry.message_id)
        delivered_to_ui = False
        if not outside_mounted_window:
            state.entries.insert(insert_index, entry)
            if channel_index == self.current_channel_index:
                self._insert_chat_widget(entry, insert_index, older=is_older)
                delivered_to_ui = True
        else:
            state.has_older_history = True
            if channel_index == self.current_channel_index:
                self._has_older_history = True
                self._ensure_load_older_control(
                    self.query_one("#chat-log", ChatTranscript)
                )
                self._capture_current_channel_state()
        if rx_debug_enabled():
            if delivered_to_ui:
                rx_debug_log(f"CHAT UI id={entry.message_id} delivered")
            elif channel_index != self.current_channel_index:
                rx_debug_log(
                    f"CHAT UI id={entry.message_id} not displayed "
                    f"reason=current_channel={self.current_channel_index}"
                    f",message_channel={channel_index}"
                )
            else:
                rx_debug_log(
                    f"CHAT UI id={entry.message_id} not displayed "
                    "reason=outside_mounted_history_window"
                )
        if is_older:
            self._show_older_message_notice(channel_index, entry)
        if not chat_is_visible:
            state.unread_count += 1
            self._recount_unread()
        self._update_tab_bar()

    def _assign_arrival_order(self, entry: ChatEntry) -> None:
        self._arrival_sequence += 1
        entry.arrival_order = self._arrival_sequence

    @staticmethod
    def _ordered_insert_index(entries: list[ChatEntry], entry: ChatEntry) -> int:
        """Return a stable right-biased insertion point for equal clocks."""
        index = len(entries)
        while index and entry.order_key < entries[index - 1].order_key:
            index -= 1
        return index

    def _insert_entry_in_order(
        self,
        entries: list[ChatEntry],
        entry: ChatEntry,
    ) -> int:
        index = self._ordered_insert_index(entries, entry)
        entries.insert(index, entry)
        return index

    def _state_for(self, channel_index: int) -> ChannelChatState:
        return self._channel_states.setdefault(channel_index, ChannelChatState())

    def _capture_current_channel_state(self) -> None:
        state = self._state_for(self.current_channel_index)
        state.entries = self.chat_history
        state.transcript_new_count = self.transcript_new_count
        state.has_older_history = self._has_older_history
        state.mounted_target = self._mounted_chat_target
        state.open_scroll_pending = self._chat_open_scroll_pending

    def _restore_channel_state(self, channel_index: int) -> ChannelChatState:
        state = self._ensure_channel_loaded(channel_index)
        self.current_channel_index = channel_index
        self.chat_history = state.entries
        self.transcript_new_count = len(state.new_below_ids)
        state.transcript_new_count = self.transcript_new_count
        self._has_older_history = state.has_older_history
        self._mounted_chat_target = state.mounted_target
        self._chat_open_scroll_pending = state.open_scroll_pending
        return state

    def _ensure_channel_loaded(self, channel_index: int) -> ChannelChatState:
        state = self._state_for(channel_index)
        if state.loaded:
            return state
        state.loaded = True
        if self.chat_store is None:
            return state
        try:
            page = self.chat_store.load_recent_page(
                channel_index=channel_index,
                limit=DEFAULT_HISTORY_LIMIT,
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))
            return state
        state.entries.extend(stored_chat_entry(stored) for stored in page.messages)
        state.has_older_history = page.has_older
        state.open_scroll_pending = bool(page.messages)
        return state

    async def _switch_channel(self, channel_index: int) -> None:
        if channel_index == self.current_channel_index:
            return
        if self.current_tab == "chat":
            self._mark_new_messages_read()
        self._capture_current_channel_state()
        state = self._restore_channel_state(channel_index)
        transcript = self.query_one("#chat-log", ChatTranscript)
        await transcript.remove_children()
        widgets: list[Static | ChatEntryWidget] = []
        if state.has_older_history:
            widgets.append(LoadOlderControl())
        elif state.entries and self.chat_store is not None:
            widgets.append(EndOfChatHistoryMarker())
        widgets.extend(self._chat_entry_widget(entry) for entry in state.entries)
        if widgets:
            await transcript.mount(*widgets)
        if self.current_tab == "chat":
            self._mark_unread_messages_viewed()
            self._recount_unread()
        selector = self.query_one(ChannelSelector)
        selector.value = channel_index
        selector.close_menu()
        self._update_tab_bar()
        self._update_transcript_indicator()
        self.call_after_refresh(self._jump_to_newest)

        self._render_chat_status()

    def _append_chat_widget(self, entry: ChatEntry) -> None:
        """Compatibility wrapper for callers that already appended an entry."""
        self._insert_chat_widget(entry, self.chat_history.index(entry), older=False)

    def _insert_chat_widget(
        self,
        entry: ChatEntry,
        insert_index: int,
        *,
        older: bool,
    ) -> None:
        transcript = self.query_one("#chat-log", ChatTranscript)
        if not self._has_older_history and self.chat_store is not None:
            self._ensure_end_history_marker(transcript)
        if self.current_tab != "chat":
            following = self._following_chat_widget(insert_index)
            transcript.mount(self._chat_entry_widget(entry), before=following)
            self._trim_mounted_chat_window(transcript)
            self._chat_open_scroll_pending = True
            return
        should_follow = self._is_near_chat_bottom()
        if should_follow:
            transcript.anchor()
        old_scroll_y = transcript.scroll_y
        old_virtual_height = transcript.virtual_size.height
        following = self._following_chat_widget(insert_index)
        inserted_above_view = (
            older
            and following is not None
            and following.region.y <= transcript.region.y
        )
        inserted_below_view = (
            older
            and following is not None
            and following.region.y >= transcript.region.bottom
        )
        transcript.mount(self._chat_entry_widget(entry), before=following)
        self._trim_mounted_chat_window(transcript)
        if should_follow:
            self.call_after_refresh(self._jump_to_newest)
        elif inserted_above_view:
            self.call_after_refresh(
                self._restore_scroll_after_prepend,
                transcript,
                old_scroll_y,
                old_virtual_height,
            )
        elif (not older or inserted_below_view) and entry.is_new and not entry.outgoing:
            state = self._state_for(self.current_channel_index)
            state.new_below_ids.add(self._entry_review_identity(entry))
            self.transcript_new_count = len(state.new_below_ids)
            self._update_transcript_indicator()

    def _following_chat_widget(self, insert_index: int) -> ChatEntryWidget | None:
        if insert_index + 1 >= len(self.chat_history):
            return None
        following_entry = self.chat_history[insert_index + 1]
        return next(
            (
                widget
                for widget in self.query(ChatEntryWidget)
                if widget.entry is following_entry
            ),
            None,
        )

    def _trim_mounted_chat_window(self, transcript: ChatTranscript) -> None:
        """Bound the mounted window without hiding NEW/unread messages."""
        trimmed = False
        while len(self.chat_history) > self._mounted_chat_target:
            oldest = self.chat_history[0]
            removable_index = (
                0
                if oldest.message_id is not None
                and not oldest.is_new
                and not oldest.unread
                else None
            )
            if removable_index is None:
                break
            removed = self.chat_history.pop(removable_index)
            widget = next(
                (
                    candidate
                    for candidate in self.query(ChatEntryWidget)
                    if candidate.entry is removed
                ),
                None,
            )
            if widget is not None:
                widget.remove()
            trimmed = True

        if trimmed:
            self._has_older_history = True
            self._ensure_load_older_control(transcript)
        self._capture_current_channel_state()

    def _ensure_load_older_control(self, transcript: ChatTranscript) -> None:
        for marker in transcript.query(EndOfChatHistoryMarker):
            marker.remove()
        if len(transcript.query(LoadOlderControl)):
            return
        first_widget = next(iter(self.query(ChatEntryWidget)), None)
        if first_widget is None:
            transcript.mount(LoadOlderControl())
        else:
            transcript.mount(LoadOlderControl(), before=first_widget)

    def _ensure_end_history_marker(self, transcript: ChatTranscript) -> None:
        """Show the passive boundary only when at least one entry exists."""
        if (
            self.chat_store is None
            or not self.chat_history
            or self._has_older_history
        ):
            return
        if len(transcript.query(EndOfChatHistoryMarker)):
            return
        first_widget = next(iter(transcript.query(ChatEntryWidget)), None)
        if first_widget is None:
            transcript.mount(EndOfChatHistoryMarker())
        else:
            transcript.mount(EndOfChatHistoryMarker(), before=first_widget)

    def _refresh_chat_timestamps(
        self,
        now: float | None = None,
        wall_now: float | None = None,
    ) -> None:
        current_time = monotonic() if now is None else now
        for widget in self.query(ChatEntryWidget):
            widget.refresh_timestamp(current_time)
        self._refresh_mesh(wall_now)

    def _mesh_last_message_activity(self) -> dict[str, float]:
        """Most recent trustworthy incoming-message timestamp per node.

        Distinct from RadioService's `lastHeard` (passive node-database
        sync): this is specifically CHAT receive activity, the source of
        truth for MESH's CLIENT role and last-interaction time.

        Persisted CHAT history is authoritative, not whatever bounded page
        happens to be mounted in memory: a node's last message may be far
        older than the currently loaded window, or from before the app was
        last restarted, and must still count. In-memory channel state is
        merged on top of the persisted baseline -- both sources only ever
        contribute a per-node maximum, never a sum, so merging cannot
        double-count -- because a just-received message reaches this method
        (via _refresh_mesh) before its own persistence write completes; see
        _accept_received_message, which refreshes MESH before persisting.
        """
        activity: dict[str, float] = {}
        if self.chat_store is not None:
            try:
                activity.update(self.chat_store.latest_incoming_message_at())
            except ChatStoreError:
                pass
        for state in self._channel_states.values():
            for entry in state.entries:
                if entry.outgoing or not entry.node_id:
                    continue
                key = entry.node_id.strip().lower()
                if not key:
                    continue
                # Matches StoredMessage.message_time's incoming precedence:
                # no receipt-time fallback, so in-memory and persisted
                # activity resolve by the same truthful message clock.
                timestamp = entry.origin_sent_at or entry.radio_rx_at
                if timestamp is None:
                    continue
                if key not in activity or timestamp > activity[key]:
                    activity[key] = timestamp
        return activity

    def _mesh_working_set(self, wall_now: float | None = None) -> tuple[MeshNodeState, ...]:
        """Build MESH's displayed real-node set without touching the board.

        The single place that turns known nodes + CHAT activity into
        MESH's displayed working set. NodeDB-first: `RadioService.
        get_known_nodes()` (the radio's own passively-learned node
        database) is the primary admission source -- a node the radio
        already knows about from passive mesh traffic is a candidate on
        its own, no CHAT message required. `_mesh_last_message_activity()`
        enriches a candidate already admitted (marking it CLIENT) rather
        than gating admission; see build_mesh_working_set.

        Callers that need the count and the rendered board to describe
        the exact same moment (see _refresh_mesh) must compute this
        ONCE per cycle and pass the SAME `wall_now` -- and ideally the
        same resulting tuple -- to every consumer that cycle, rather
        than calling this again and risking a second, independent read
        of live, thread-mutable radio state.
        """
        if self._radio_state is not RadioState.ONLINE:
            return ()
        getter = getattr(self.radio, "get_known_nodes", None)
        try:
            nodes = tuple(getter()) if callable(getter) else ()
        except Exception:
            nodes = ()
        if not all(isinstance(node, NodeMetadata) for node in nodes):
            nodes = ()
        current_time = time() if wall_now is None else wall_now
        return build_mesh_working_set(
            nodes,
            now=current_time,
            last_message_at=self._mesh_last_message_activity(),
            favorite_ids=self.settings.favorite_node_ids,
        )

    def _mesh_active_count(
        self,
        wall_now: float | None = None,
        working_set: tuple[MeshNodeState, ...] | None = None,
    ) -> int:
        """Authoritative count of active REAL remote nodes MESH displays.

        Describes the real remote nodes CURRENTLY DISPLAYED on MESH --
        the same bounded working set the board renders, not the full
        known-node population (which may include nodes MESH never shows
        at all). Uses the EXACT SAME predicate (is_node_active) that
        _mesh_node_color() uses for each node's own BASE/DIM_BASE
        styling, so the [3] MESH (N) tab label and the board can never
        disagree about which/how many displayed nodes are active.
        Anonymous relay stages are never part of the working set and so
        never affect this count either way (see mesh_topology.
        RelayStage); YOU is excluded via is_local.

        `working_set`, when supplied by a caller that already computed
        one this cycle (see _refresh_mesh), is used as-is instead of
        triggering a second independent _mesh_working_set() read of live
        radio state -- this is what keeps the count and the rendered
        board from ever describing two different snapshots. Safe to
        call regardless of the current tab -- unlike _refresh_mesh(),
        this never touches the topology board/widget, only computes a
        number, so refreshing it periodically (see
        _refresh_chat_timestamps) to catch a node aging out while MESH
        isn't even the visible tab can never "reshuffle" anything.
        """
        current_time = time() if wall_now is None else wall_now
        if working_set is None:
            working_set = self._mesh_working_set(current_time)
        return sum(
            1
            for state in working_set
            if not state.node.is_local and is_node_active(state.node.last_heard, current_time)
        )

    def _refresh_mesh(self, wall_now: float | None = None) -> None:
        """Refresh passive topology data without causing Meshtastic traffic."""
        current_time = time() if wall_now is None else wall_now
        # Computed exactly ONCE per cycle and threaded into every
        # consumer below -- the count ([3] MESH (N), via _update_tab_bar)
        # and the rendered board must describe the SAME snapshot of live
        # NodeDB state, never two independent reads of it (that mismatch
        # -- count updating live while the board stayed visually stale
        # -- was the exact bug this closes).
        working_set = self._mesh_working_set(current_time)
        # [3] MESH (N) must stay live wherever _refresh_mesh() is called
        # from, regardless of whether MESH is the tab currently showing
        # (see _mesh_active_count -- it never touches the board/widget,
        # so this can never "reshuffle" the topology).
        self._update_tab_bar(current_time, mesh_working_set=working_set)
        self._update_mesh_status_line(working_set, current_time)
        views = list(self.query(MeshTopologyView))
        statuses = list(self.query("#mesh-status"))
        if not views or not statuses:
            return
        if self.current_tab != "mesh":
            return
        if self._radio_state is not RadioState.ONLINE:
            # Stale topology intentionally stays visible while
            # connecting/reconnecting -- the shared top-of-view
            # connection status (see _update_chat_connection_state/
            # _connection_status_text) already communicates "not live
            # right now"; clearing or rebuilding the board here would be
            # a needless reshuffle of useful stale data over a
            # connection-status change alone. Never MESH-specific
            # wording like the old "RADIO DISCONNECTED" either -- that
            # was exactly the kind of independent reinterpretation this
            # is meant to eliminate.
            return
        view = views[0]
        status = statuses[0]
        if not working_set:
            view.clear_nodes()
            status.update("NO MESH DATA")
            self._update_mesh_context_status()
            return
        status.update("")
        # A real CLIENT with a truthful nonzero hop count is placed at
        # least hops_away+1 grid steps out -- never closer -- so its own
        # anonymous relay-stage placeholders have genuinely interior
        # cells to occupy along the YOU-to-client line (see RelayStage/
        # build_relay_stages); geography alone would otherwise often
        # place it immediately adjacent to YOU, leaving no room at all.
        min_radius_by_id = {
            node_id: hops + 1 for node_id, hops in _mesh_hop_counts(working_set).items()
        }
        slots = assign_grid_slots(
            tuple(state.node for state in working_set),
            max_radius=DEFAULT_MAX_GRID_RADIUS,
            min_radius_by_id=min_radius_by_id,
        )
        base_positions = place_within_bounds(
            slots,
            center_row=MESH_GRID_CENTER_ROW,
            center_column=MESH_GRID_CENTER_COLUMN,
            row_count=MESH_GRID_ROWS,
            column_count=MESH_GRID_COLUMNS,
        )
        view.set_nodes(working_set, base_positions, theme=self._current_theme, now=current_time)
        self._update_mesh_context_status()

    def _move_mesh_focus(self, direction: str) -> None:
        view = self.query_one(MeshTopologyView)
        # Anonymous relay-stage placeholders are visual topology only --
        # excluded from the navigation candidate set entirely, so an
        # arrow press always lands on the nearest sensible REAL node
        # (YOU, or a real CLIENT/CLIENT+RELAY/RELAY), skipping over any
        # relay stage that happens to sit geometrically between them.
        relay_ids = {stage.node_id for stage in view.relay_stages}
        navigable_positions = {
            node_id: position
            for node_id, position in view.base_positions.items()
            if node_id not in relay_ids
        }
        target_id = _mesh_directional_target(
            navigable_positions, view.selected_node_id, direction
        )
        if target_id is not None:
            view.select_node(target_id)
            view.set_nodes(
                view.working_set,
                view.base_positions,
                theme=self._current_theme,
                now=time(),
            )
            self._update_mesh_context_status()

    def _update_mesh_context_status(self) -> None:
        """Minimal truthful summary line for the currently selected MESH node."""
        widgets = list(self.query("#mesh-context-status"))
        if not widgets:
            return
        status_widget = widgets[0]
        if self.current_tab != "mesh" or self._radio_state is not RadioState.ONLINE:
            status_widget.update("")
            return
        views = list(self.query(MeshTopologyView))
        if not views:
            status_widget.update("")
            return
        view = views[0]
        state = next(
            (
                candidate
                for candidate in view.working_set
                if candidate.node.node_id == view.selected_node_id
            ),
            None,
        )
        # An anonymous relay-stage placeholder can never be selected (see
        # MeshRelayWidget/_move_mesh_focus), so `state` is None here only
        # for a genuinely stale/invalid ID, never a relay stage -- the
        # bottom-left context is always either YOU or a real node's full
        # LONG NAME / SHORT NAME / ROLE / HOPS / TIME / DISTANCE line.
        status_widget.update(
            format_mesh_context_line(state, now=time()) if state is not None else ""
        )

    def _advance_delivery_states(self) -> None:
        self._send_dot_count = self._send_dot_count % 3 + 1
        now = monotonic()
        for entry in self.chat_history:
            if (
                entry.delivery_state is DeliveryState.SENT
                and entry.confirmation_deadline is not None
                and now >= entry.confirmation_deadline
            ):
                self._set_delivery_state(entry, DeliveryState.UNCONFIRMED)
        for widget in self.query(ChatEntryWidget):
            widget.refresh_delivery_state(self._send_dot_count)

    def _is_near_chat_bottom(self) -> bool:
        transcript = self.query_one("#chat-log", ChatTranscript)
        return transcript.max_scroll_y - transcript.scroll_y <= 1

    def _jump_to_newest(self) -> None:
        transcript = self.query_one("#chat-log", ChatTranscript)
        transcript.anchor()
        transcript.scroll_end(animate=False)
        self._state_for(self.current_channel_index).new_below_ids.clear()
        self.transcript_new_count = 0
        self._update_transcript_indicator()

    def _return_to_present_and_type(self) -> None:
        """Jump to the chronological edge and resume the preserved draft."""
        self._jump_to_newest()
        self._focus_chat_composer()

    def _chat_entry_widget(self, entry: ChatEntry) -> ChatEntryWidget:
        return ChatEntryWidget(
            entry,
            favorite=self.settings.is_favorite(entry.node_id),
        )

    def _chat_navigation_targets(self) -> list[Static | ChatEntryWidget]:
        targets: list[Static | ChatEntryWidget] = []
        transcript = self.query_one(ChatTranscript)
        for widget in transcript.walk_children():
            if isinstance(
                widget,
                (LoadOlderControl, ChatEntryWidget, MessageActionControl),
            ) and widget.display:
                targets.append(widget)
        return targets

    def _move_chat_focus(self, direction: int) -> None:
        targets = self._chat_navigation_targets()
        if not targets:
            if direction > 0:
                self._focus_chat_composer()
            return
        try:
            index = targets.index(self.focused)
            next_index = index + direction
            if direction > 0 and next_index >= len(targets):
                self._focus_chat_composer()
                return
            target = targets[max(0, min(len(targets) - 1, next_index))]
        except ValueError:
            target = targets[-1] if direction < 0 else targets[0]
        target.focus()
        target.scroll_visible(animate=False)
        self.call_after_refresh(self._clear_indicator_if_at_bottom)

    def _oldest_new_entry(self) -> ChatEntry | None:
        """Resolve the current channel's oldest actual NEW incoming entry."""
        state = self._state_for(self.current_channel_index)
        mounted_by_id = {
            entry.message_id: entry
            for entry in state.entries
            if entry.message_id is not None
        }
        candidates = [
            entry
            for entry in state.entries
            if entry.is_new and not entry.outgoing
        ]
        unmounted_ids = state.new_message_ids.difference(mounted_by_id)
        if state.new_message_ids and self.chat_store is not None:
            try:
                stored = self.chat_store.load_oldest_incoming_by_ids(
                    state.new_message_ids,
                    channel_index=self.current_channel_index,
                )
            except ChatStoreError:
                # A mounted candidate is only truthful if no older NEW identity
                # is known to exist outside the bounded window.
                if unmounted_ids:
                    return None
            else:
                if stored is not None:
                    candidate = mounted_by_id.get(stored.id)
                    if candidate is None:
                        candidate = stored_chat_entry(stored)
                        candidate.is_new = True
                        candidate.unread = stored.id in state.unread_message_ids
                    candidates.append(candidate)
        if not candidates:
            return None
        return min(candidates, key=lambda entry: entry.order_key)

    def _focus_oldest_new_message(self) -> None:
        """Select the oldest NEW incoming message in the current channel."""
        entry = self._oldest_new_entry()
        if entry is None:
            return
        identity = self._entry_review_identity(entry)
        widget = next(
            (
                candidate
                for candidate in self.query(ChatEntryWidget)
                if self._entry_review_identity(candidate.entry) == identity
            ),
            None,
        )
        if widget is not None:
            self._focus_chat_widget(widget)
            return
        if entry.message_id is None or self.chat_store is None:
            return
        self.run_worker(
            self._page_to_new_message(entry.message_id, self.current_channel_index),
            name="chat-unread-navigation",
            group="chat-unread-navigation",
            exclusive=True,
        )

    async def _page_to_new_message(
        self,
        message_id: int,
        channel_index: int,
    ) -> None:
        """Load bounded 50-row pages only until one known NEW row is mounted."""
        while (
            self.current_tab == "chat"
            and self.current_channel_index == channel_index
            and self._has_older_history
        ):
            before = len(self.chat_history)
            await self.load_older_chat_history(LoadOlderControl.Activated())
            widget = next(
                (
                    candidate
                    for candidate in self.query(ChatEntryWidget)
                    if candidate.entry.message_id == message_id
                ),
                None,
            )
            if widget is not None:
                self._focus_chat_widget(widget)
                return
            if len(self.chat_history) == before:
                return

    def _focus_chat_widget(self, widget: ChatEntryWidget) -> None:
        """Focus one entry and bring it fully into the visible transcript."""
        widget.focus()
        widget.scroll_visible(animate=False)
        # A preceding bounded prepend may have queued scroll restoration; run
        # this once more afterward so the selected target is not edge-clipped.
        self.call_after_refresh(widget.scroll_visible, animate=False)

    def _focus_chat_composer(self) -> None:
        """Return to typing without changing the current draft or read state."""
        chat_input = self.query_one("#chat-input", Input)
        chat_input.focus()
        chat_input.cursor_position = len(chat_input.value)

    @on(MessageActionControl.Activated)
    def message_action_activated(
        self,
        event: MessageActionControl.Activated,
    ) -> None:
        if event.action_control.action == "resend":
            self._rebroadcast(event.action_control.entry)
            for widget in self.query(ChatEntryWidget):
                if widget.entry is event.action_control.entry:
                    widget.focus()
                    break

    @on(ChatEntryWidget.UserMenuRequested)
    def open_user_menu(self, event: ChatEntryWidget.UserMenuRequested) -> None:
        """Open node details/actions without changing transcript layout."""
        entry = event.widget.entry
        if entry.outgoing or not entry.node_id:
            return
        getter = getattr(self.radio, "get_node_metadata", None)
        metadata = NodeMetadata(
            entry.node_id,
            entry.sender_name,
            entry.sender_short_name,
        )
        if callable(getter):
            try:
                current = getter(entry.node_id)
            except Exception:
                current = None
            if isinstance(current, NodeMetadata):
                metadata = NodeMetadata(
                    entry.node_id,
                    current.long_name or metadata.long_name,
                    current.short_name or metadata.short_name,
                    current.hops_away,
                    current.last_heard,
                    current.is_local,
                )

        self._open_node_menu(
            metadata,
            event.widget,
            self.query_one(ChatTranscript),
        )

    def _open_node_menu(
        self,
        metadata: NodeMetadata,
        origin: Widget,
        scroll_target: ScrollableContainer | None,
        *,
        include_rx_age: bool = False,
    ) -> None:
        """Open the shared CHAT/MESH node-details menu."""

        items: list[PopupItem] = []
        long_name = metadata.long_name.strip() if metadata.long_name else None
        short_name = metadata.short_name.strip() if metadata.short_name else None
        if long_name:
            items.append(PopupItem(long_name, actionable=False))
        # Never repeat Short Name as its own row when it's the same
        # displayed value as Long Name -- never a blank/fake "?" row
        # either; an unavailable name is simply omitted.
        if short_name and short_name != long_name:
            items.append(PopupItem(short_name, actionable=False))
        if metadata.hops_away is not None:
            unit = "HOP" if metadata.hops_away == 1 else "HOPS"
            items.append(
                PopupItem(
                    f"{metadata.hops_away} {unit} AWAY",
                    actionable=False,
                )
            )
        if (
            include_rx_age
            and metadata.last_heard is not None
            and metadata.last_heard <= time()
        ):
            items.append(
                PopupItem(
                    f"RX {format_relative_age(time() - metadata.last_heard)}",
                    actionable=False,
                )
            )
        if metadata.is_local:
            items.append(PopupItem(metadata.node_id, actionable=False))
            highlighted = 0
        else:
            items.append(PopupItem("REPLY", "reply", actionable=True))
            favorite = self.settings.is_favorite(metadata.node_id)
            action = "unfavorite" if favorite else "favorite"
            items.append(PopupItem(action.upper(), action, actionable=True))
            # Preserve the existing FAVORITE/UNFAVORITE default highlight
            # (the established Enter-Enter toggle gesture) -- REPLY is a
            # newly added item, reached with one extra arrow press, not
            # a change to what pressing Enter immediately does.
            highlighted = len(items) - 1
        menu = ViewportMenu(
            items,
            highlighted_index=highlighted,
            on_activate=lambda _index, item: self._activate_menu_item(
                metadata, str(item.value)
            ),
            menu_id="node-context-menu",
        )
        self._close_user_menu(restore_focus=False)
        self._user_menu = menu
        self._user_menu_origin = origin
        self._user_menu_scroll_target = scroll_target
        self._user_menu_scroll_x = (
            scroll_target.scroll_x if scroll_target is not None else None
        )
        self._user_menu_scroll_y = (
            scroll_target.scroll_y if scroll_target is not None else None
        )
        width = max(cell_len(item.label) for item in items) + 4
        self.screen.mount(menu)
        menu.place(origin.region, self.screen.region, width)

    def _activate_menu_item(self, metadata: NodeMetadata, action: str) -> None:
        """Dispatch a node-context-menu action -- REPLY needs the node's

        name fields (for the @mention), so it is handled separately
        from the existing node_id-only favorite/unfavorite path.
        """
        if action == "reply":
            self._activate_reply(metadata)
            return
        self._activate_node_action(metadata.node_id, action)

    def _activate_reply(self, metadata: NodeMetadata) -> None:
        """Insert an @mention for this node at the start of the CHAT

        draft, then hand focus back to the input for continued typing.
        A text-entry convenience only -- never changes Meshtastic
        addressing/routing based on the textual @mention.
        """
        self._close_user_menu(restore_focus=False)
        mention = f"@{_reply_mention_name(metadata)}"
        chat_input = self.query_one("#chat-input", Input)
        draft = chat_input.value
        if draft == mention or draft.startswith(f"{mention} "):
            # Already mentioned at the start of the draft -- preserve it
            # exactly, never duplicate the mention.
            new_value = draft
        elif draft:
            new_value = f"{mention} {draft}"
        else:
            new_value = f"{mention} "
        chat_input.value = new_value
        if self.current_tab != "chat":
            self.show_tab("chat")
        chat_input.focus()
        chat_input.cursor_position = len(chat_input.value)

    def _activate_node_action(self, node_id: str, action: str) -> None:
        if not node_id or action not in ("favorite", "unfavorite"):
            return
        self.settings.set_favorite(node_id, action == "favorite")
        try:
            self.settings.save()
        except OSError as error:
            self._show_send_error(f"Could not save favorite: {error}")
            return
        for widget in self.query(ChatEntryWidget):
            if widget.entry.node_id and widget.entry.node_id.lower() == node_id.lower():
                widget.set_favorite(self.settings.is_favorite(node_id))
        self._refresh_mesh()
        self._close_user_menu()

    def _activate_user_action(
        self,
        origin: ChatEntryWidget,
        action: str,
    ) -> None:
        """Compatibility wrapper for the established CHAT action path."""
        node_id = origin.entry.node_id
        if node_id:
            self._activate_node_action(node_id, action)

    def _close_user_menu(self, *, restore_focus: bool = True) -> None:
        menu = self._user_menu
        origin = self._user_menu_origin
        scroll_target = self._user_menu_scroll_target
        scroll_x = self._user_menu_scroll_x
        scroll_y = self._user_menu_scroll_y
        self._user_menu = None
        self._user_menu_origin = None
        self._user_menu_scroll_target = None
        self._user_menu_scroll_x = None
        self._user_menu_scroll_y = None
        if menu is not None:
            menu.remove()
        if restore_focus and origin is not None and origin.is_mounted:
            origin.focus(scroll_visible=not isinstance(origin, MeshNodeWidget))
            if scroll_target is not None and scroll_y is not None:
                self.call_after_refresh(
                    scroll_target.scroll_to,
                    x=scroll_x,
                    y=scroll_y,
                    animate=False,
                    force=True,
                )

    def _clear_indicator_if_at_bottom(self) -> None:
        if self._is_near_chat_bottom() and self.transcript_new_count:
            self._state_for(self.current_channel_index).new_below_ids.clear()
            self.transcript_new_count = 0
            self._update_transcript_indicator()

    def _update_transcript_indicator(self) -> None:
        label = (
            f"↓ {self.transcript_new_count} NEW"
            if self.transcript_new_count
            else ""
        )
        self.query_one("#chat-new-below", Static).update(label)

    def _mark_unread_messages_viewed(self) -> None:
        state = self._state_for(self.current_channel_index)
        for entry in self.chat_history:
            if entry.unread:
                entry.unread = False
                if entry.message_id is not None:
                    state.unread_message_ids.discard(entry.message_id)
        state.unread_count = 0

    def _mark_new_messages_read(self) -> None:
        self._mark_messages_read(
            entry for entry in self.chat_history if entry.is_new
        )

    @staticmethod
    def _entry_review_identity(entry: ChatEntry) -> tuple[str, int]:
        """Return a stable session identity, preferring the persisted row ID."""
        if entry.message_id is not None:
            return ("message", entry.message_id)
        return ("arrival", entry.arrival_order)

    def _clear_message_read_state(self, entry: ChatEntry) -> bool:
        """Apply the model portion of one read transition without rendering."""
        state = self._state_for(entry.channel_index)
        identity = self._entry_review_identity(entry)
        changed = entry.is_new or entry.unread or identity in state.pending_older_ids
        was_unread = entry.unread
        entry.is_new = False
        entry.unread = False
        if entry.message_id is not None:
            state.new_message_ids.discard(entry.message_id)
            state.unread_message_ids.discard(entry.message_id)
        state.pending_older_ids.discard(identity)
        state.new_below_ids.discard(identity)
        if was_unread and state.unread_count:
            state.unread_count -= 1
        return changed

    def _finish_read_transition(self, changed_entries: list[ChatEntry]) -> None:
        if not changed_entries:
            return
        changed_identity = {id(entry) for entry in changed_entries}
        if any(
            entry.channel_index == self.current_channel_index
            for entry in changed_entries
        ):
            for widget in self.query(ChatEntryWidget):
                if id(widget.entry) in changed_identity:
                    widget.refresh_new_message_state()
            state = self._state_for(self.current_channel_index)
            self.transcript_new_count = len(state.new_below_ids)
            state.transcript_new_count = self.transcript_new_count
            self._update_transcript_indicator()
            self._render_chat_status()
        self._recount_unread()
        self._update_tab_bar()

    def _mark_message_read(self, entry: ChatEntry) -> None:
        """Acknowledge exactly one entry selected by the user."""
        changed = self._clear_message_read_state(entry)
        self._finish_read_transition([entry] if changed else [])

    def _mark_messages_read(self, entries: Iterable[ChatEntry]) -> None:
        """Apply the same read transition to an existing broader read flow."""
        changed = [
            entry
            for entry in entries
            if self._clear_message_read_state(entry)
        ]
        self._finish_read_transition(changed)

    def _mark_current_channel_read_for_send(self) -> None:
        """Sending proves the current conversation was actively viewed."""
        self._mark_unread_messages_viewed()
        self._mark_new_messages_read()
        self._recount_unread()
        self._update_tab_bar()

    def _recount_unread(self) -> None:
        self.unread_count = sum(
            state.unread_count for state in self._channel_states.values()
        )

    def _load_chat_history(self) -> None:
        state = self._restore_channel_state(self.current_channel_index)
        transcript = self.query_one("#chat-log", ChatTranscript)
        widgets: list[Static | ChatEntryWidget] = []
        if state.has_older_history:
            widgets.append(LoadOlderControl())
        elif state.entries and self.chat_store is not None:
            widgets.append(EndOfChatHistoryMarker())
        for entry in state.entries:
            widgets.append(self._chat_entry_widget(entry))
        if widgets:
            transcript.mount(*widgets)

    @on(LoadOlderControl.Activated)
    async def load_older_chat_history(
        self,
        _event: LoadOlderControl.Activated,
    ) -> None:
        if self.chat_store is None or not self._has_older_history:
            return
        oldest_id = self._oldest_persisted_message_id()
        if oldest_id is None:
            return
        try:
            page = self.chat_store.load_older_page(
                oldest_id,
                channel_index=self.current_channel_index,
                limit=OLDER_HISTORY_PAGE_SIZE,
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))
            return
        if not page.messages:
            self._has_older_history = False
            control = self.query_one("#load-older", LoadOlderControl)
            await control.remove()
            transcript = self.query_one("#chat-log", ChatTranscript)
            first_widget = next(iter(transcript.query(ChatEntryWidget)), None)
            if first_widget is not None:
                await transcript.mount(EndOfChatHistoryMarker(), before=first_widget)
            self._capture_current_channel_state()
            return

        transcript = self.query_one("#chat-log", ChatTranscript)
        old_scroll_y = transcript.scroll_y
        old_virtual_height = transcript.virtual_size.height
        first_widget = next(iter(self.query(ChatEntryWidget)), None)
        entries = [stored_chat_entry(stored) for stored in page.messages]
        state = self._state_for(self.current_channel_index)
        for entry in entries:
            if entry.message_id in state.new_message_ids:
                entry.is_new = True
            if entry.message_id in state.unread_message_ids:
                entry.unread = True
        widgets = [self._chat_entry_widget(entry) for entry in entries]
        self.chat_history[0:0] = entries
        self._mounted_chat_target += len(entries)
        await transcript.mount(*widgets, before=first_widget)
        self._has_older_history = page.has_older
        if not page.has_older:
            control = self.query_one("#load-older", LoadOlderControl)
            await control.remove()
            first_loaded_widget = widgets[0]
            await transcript.mount(
                EndOfChatHistoryMarker(),
                before=first_loaded_widget,
            )
        self._capture_current_channel_state()
        self.call_after_refresh(
            self._restore_scroll_after_prepend,
            transcript,
            old_scroll_y,
            old_virtual_height,
        )

    @staticmethod
    def _restore_scroll_after_prepend(
        transcript: ChatTranscript,
        old_scroll_y: float,
        old_virtual_height: int,
    ) -> None:
        added_height = transcript.virtual_size.height - old_virtual_height
        transcript.scroll_to(
            y=max(0, old_scroll_y + added_height),
            animate=False,
            force=True,
        )

    def _oldest_persisted_message_id(self) -> int | None:
        return next(
            (
                entry.message_id
                for entry in self.chat_history
                if entry.message_id is not None
            ),
            None,
        )

    def _persist_incoming(self, entry: ChatEntry) -> bool:
        if self.chat_store is None:
            return True
        try:
            result = self.chat_store.add_incoming(
                packet_id=entry.packet_id,
                node_id=entry.node_id or "unknown",
                sender_name=entry.sender_name,
                sender_short_name=entry.sender_short_name,
                channel_index=entry.channel_index,
                text=entry.text,
                origin_sent_at=entry.origin_sent_at,
                radio_rx_at=entry.radio_rx_at,
                received_at=entry.app_received_at,
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))
            return True
        entry.message_id = result.message_id
        return result.inserted

    def _persist_outgoing(self, entry: ChatEntry) -> None:
        if self.chat_store is None:
            return
        try:
            entry.message_id = self.chat_store.add_outgoing(
                text=entry.text,
                channel_index=entry.channel_index,
                local_sent_at=entry.local_sent_at or entry.app_received_at,
                delivery_state=(entry.delivery_state or DeliveryState.SENDING).value,
            )
            entry.active_attempt_id = self.chat_store.add_send_attempt(
                entry.message_id,
                time(),
                (entry.delivery_state or DeliveryState.SENDING).value,
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))

    def _persist_new_attempt(self, entry: ChatEntry) -> None:
        if self.chat_store is None or entry.message_id is None:
            entry.active_attempt_id = None
            return
        try:
            entry.active_attempt_id = self.chat_store.add_send_attempt(
                entry.message_id,
                time(),
            )
            self.chat_store.update_delivery_state(
                entry.message_id,
                DeliveryState.SENDING.value,
                attempt_id=entry.active_attempt_id,
            )
        except ChatStoreError as error:
            self._show_send_error(str(error))

    def _set_delivery_state(
        self,
        entry: ChatEntry,
        state: DeliveryState,
        *,
        packet_id: int | None = None,
        detail: str = "",
    ) -> None:
        entry.delivery_state = state
        if packet_id is not None:
            entry.packet_id = packet_id
        if state is not DeliveryState.SENT:
            entry.confirmation_deadline = None
        completed_at = (
            time()
            if state in (
                DeliveryState.HEARD,
                DeliveryState.UNCONFIRMED,
                DeliveryState.FAILED,
            )
            else None
        )
        if self.chat_store is not None and entry.message_id is not None:
            try:
                self.chat_store.update_delivery_state(
                    entry.message_id,
                    state.value,
                    attempt_id=entry.active_attempt_id,
                    packet_id=entry.packet_id,
                    error=detail or None,
                    completed_at=completed_at,
                )
            except ChatStoreError as error:
                self._show_send_error(str(error))
        for widget in self.query(ChatEntryWidget):
            if widget.entry is entry:
                widget.refresh_delivery_state(self._send_dot_count)
                break
        self._update_footer()

    def _rebroadcast(self, entry: ChatEntry) -> None:
        if not can_manual_resend(entry):
            return
        entry.delivery_state = DeliveryState.SENDING
        entry.send_generation += 1
        entry.packet_id = None
        entry.confirmation_deadline = None
        self._persist_new_attempt(entry)
        self._set_delivery_state(entry, DeliveryState.SENDING)
        self._show_send_error("")
        generation = entry.send_generation
        self.run_worker(
            lambda: self._send_from_thread(entry, generation),
            thread=True,
        )

    @on(ChatEntryWidget.SelectionChanged)
    def chat_selection_changed(
        self,
        event: ChatEntryWidget.SelectionChanged,
    ) -> None:
        if event.selected:
            self._mark_message_read(event.widget.entry)
        self._update_footer()

    def _update_footer(self) -> None:
        if self.current_tab == "chat" and isinstance(self.focused, Input):
            text = "ENTER send    ESC messages    F4 quit"
        elif self.current_tab == "chat":
            text = "↑↓ navigate    C channel    ENTER action    F4 quit"
        else:
            text = (
                "↑↓←→ select    1-3 tabs    F4 quit"
                if self.current_tab == "mesh"
                else "1-3 switch tabs    F4 quit"
            )
        self.query_one("#footer", Static).update(text)

    def restore_terminal_cursor(self) -> None:
        """Restore the host terminal cursor; safe to call repeatedly."""
        self._terminal_cursor.restore()

    def _show_connection(
        self,
        state: RadioState,
        info: RadioInfo | None = None,
        message: str = "",
    ) -> None:
        self._radio_state = state
        self._radio_info = info if state is RadioState.ONLINE else None
        if state is RadioState.ONLINE and info is not None and info.channels:
            self._channels = info.channels
            selector = self.query_one(ChannelSelector)
            available_indexes = {channel.index for channel in self._channels}
            selected_index = self.current_channel_index
            if selected_index not in available_indexes:
                selected_index = self._channels[0].index
            selector.set_options(
                (
                    DropdownOption(channel.name, channel.index)
                    for channel in self._channels
                ),
                value=selected_index,
            )
            if selected_index != self.current_channel_index:
                self.run_worker(
                    self._switch_channel(selected_index),
                    name="select-available-channel",
                )
        self._refresh_device_options()
        self._status_dot_count = 1
        if self._connection_animation_timer is not None:
            if state is RadioState.ONLINE:
                self._connection_animation_timer.pause()
            else:
                self._connection_animation_timer.resume()
        identity_status = self.query_one("#identity-status", Static)
        identity_status.remove_class("setting-error")
        identity_status.update("")
        self._render_connection_details()
        self._render_identity(force_value=True)
        self._refresh_mesh()
        self.query_one("#connection-error", Static).update("")
        self._update_chat_connection_state()

    def _connection_status_text(self) -> str:
        """The single authoritative connection-status line CHAT and MESH

        show at the top of their view while the radio isn't ONLINE.

        Built from the EXACT SAME ANIMATED_STATUS mapping and
        _status_dot_count animation counter CONNECTION/CONFIG's own
        status row already uses (see _render_connection_details) --
        never a tab-specific reinterpretation. This is why CHAT could
        previously show "RECONNECTING..." while CONNECTION/CONFIG showed
        "CONNECTING...": CHAT's old _render_chat_status() hardcoded its
        own literal string instead of reading this shared value. There
        is no separate RECONNECTING state in RadioState -- a dropped
        connection re-enters RadioState.CONNECTING exactly like the
        first attempt (see radio_service.connection_events()), so both
        report identically here too. Returns "" when ONLINE (nothing to
        show; callers should hide/restore their normal presentation).
        """
        if self._radio_state is RadioState.ONLINE:
            return ""
        return f"STATUS {ANIMATED_STATUS[self._radio_state]}" + "." * self._status_dot_count

    def _update_chat_connection_state(self) -> None:
        """Keep CHAT's connection-status presentation in sync with the

        authoritative self._radio_state (see _show_connection) --
        purely observational, never triggers a new connection attempt
        or any other radio traffic on its own.

        Disables CHAT message entry/sending while the radio isn't
        ONLINE (Textual's `disabled` only affects focus/interaction, so
        a draft typed before a drop survives completely untouched and
        is exactly what the user sees once connection is restored).
        Also overrides CHAT's channel heading, and -- while the radio
        isn't ONLINE -- writes that exact same text to MESH's own status
        line (#mesh-connection-status) in this SAME call, so the two can
        never show different animation-dot phases (see
        _advance_connection_animation, which calls this on a fixed
        ~0.45s cadence). Once ONLINE (status_text == ""), this leaves
        that widget untouched -- _update_mesh_status_line (called from
        _refresh_mesh) becomes the writer for the "LAST UPDATE" fallback
        instead. The two never fight over ownership: this function only
        ever writes while NOT ONLINE, that one only ever writes while
        ONLINE.
        """
        status_text = self._connection_status_text()

        chat_inputs = list(self.query("#chat-input"))
        if chat_inputs:
            chat_inputs[0].disabled = self._radio_state is not RadioState.ONLINE
        self._render_chat_status()

        selectors = list(self.query(ChannelSelector))
        if selectors:
            selectors[0].set_status_override(status_text or None)

        if status_text:
            mesh_status_widgets = list(self.query("#mesh-connection-status"))
            if mesh_status_widgets:
                widget = mesh_status_widgets[0]
                widget.update(status_text)
                widget.display = True

    def _update_mesh_status_line(
        self, working_set: tuple[MeshNodeState, ...], now: float
    ) -> None:
        """The ONLINE half of #mesh-connection-status: a PERSISTENT

        "LAST UPDATE <age>" mesh-freshness indicator, always shown while
        the radio is ONLINE -- not a stale-only warning. Answers "how
        long ago did this radio last obtain meaningful information about
        the mesh", so it keeps aging (1s, 2s, ... 1m, ...) between
        refreshes with no new data, and only resets when a genuinely
        fresher timestamp arrives -- never merely because this method
        itself ran again (see _refresh_mesh's 1Hz periodic call).

        The age comes from the single most recent NodeDB last_heard
        among the working set's remote nodes (excluding YOU) -- never
        derived from CHAT history (see build_mesh_working_set; CHAT is
        enrichment, not the timing source of record here) and never a
        fabricated value: if no working-set remote node carries a
        trustworthy last_heard at all, this is omitted rather than
        showing a made-up age.

        Never touches the widget while the radio isn't ONLINE --
        _update_chat_connection_state() owns it then, on its own fixed
        animation cadence, so it can stay byte-for-byte in sync with
        CHAT's own status line; recomputing here too, on _refresh_mesh's
        different cadence, previously let the two drift out of phase by
        a dot. The two never fight over the widget: this function is a
        no-op while not ONLINE, and _update_chat_connection_state only
        ever writes non-empty text, which happens only while not ONLINE.
        """
        if self._radio_state is not RadioState.ONLINE:
            return
        widgets = list(self.query("#mesh-connection-status"))
        if not widgets:
            return
        widget = widgets[0]
        remote_last_heard = [
            state.node.last_heard
            for state in working_set
            if not state.node.is_local and state.node.last_heard is not None
        ]
        text = ""
        if remote_last_heard:
            age = now - max(remote_last_heard)
            if age >= 0:
                text = f"LAST UPDATE {format_relative_age(age)}"
        widget.update(text)
        widget.display = bool(text)

    def _advance_connection_animation(self) -> None:
        if self._radio_state is RadioState.ONLINE:
            return
        self._status_dot_count = self._status_dot_count % 3 + 1
        self._render_connection_details()
        self._update_chat_connection_state()

    def _render_connection_details(self) -> None:
        statuses = list(self.query("#connection-status"))
        detail_widgets = list(self.query("#connection-details"))
        if not statuses or not detail_widgets:
            # A final timer tick may race with Textual dismantling the screen.
            return
        status = (
            "CONNECTED"
            if self._radio_state is RadioState.ONLINE
            else ANIMATED_STATUS[self._radio_state] + "." * self._status_dot_count
        )
        palette = THEME_PALETTES[self._current_theme]
        status_style = (
            palette.base
            if self._radio_state is RadioState.ONLINE
            else palette.error
            if self._radio_state is RadioState.ERROR
            else palette.accent
        )
        status_text = Text()
        status_text.append(
            f"{CONNECTION_ROW_PREFIX}{'STATUS':<{CONNECTION_LABEL_WIDTH}}",
            style=palette.base,
        )
        status_text.append(" ", style=palette.base)
        status_text.append(status, style=status_style)
        statuses[0].update(status_text)
        values = []
        if self._radio_state is RadioState.ONLINE and self._radio_info is not None:
            info = self._radio_info
            values.extend(
                [
                    ("NODES", str(info.known_nodes)),
                ]
            )
        if values:
            details = Text()
            for index, (label, value) in enumerate(values):
                if index:
                    details.append("\n")
                details.append(
                    f"{CONNECTION_ROW_PREFIX}{label:<{CONNECTION_LABEL_WIDTH}}",
                    style=palette.base,
                )
                details.append(" ", style=palette.base)
                details.append(value, style=palette.base)
            detail_widgets[0].update(details)
        else:
            placeholder = "..." if self._radio_state is RadioState.CONNECTING else "—"
            details = Text()
            details.append(
                f"{CONNECTION_ROW_PREFIX}{'NODES':<{CONNECTION_LABEL_WIDTH}}",
                style=palette.base,
            )
            details.append(" ", style=palette.base)
            details.append(placeholder, style=palette.dim_base)
            detail_widgets[0].update(details)

    def _render_identity(self, force_value: bool = False) -> None:
        long_name = self.query_one(LongNameControl)
        short_name = self.query_one(ShortNameControl)
        values = self.query_one("#identity-values", Static)
        online = self._radio_state is RadioState.ONLINE and self._radio_info is not None
        palette = THEME_PALETTES[self._current_theme]
        if online:
            info = self._radio_info
            assert info is not None
            long_name.set_available(info.long_name, force_value=force_value)
            short_name.set_available(info.short_name, force_value=force_value)
            identity_text = Text()
            identity_text.append(
                f"{CONNECTION_ROW_PREFIX}{'NODE ID':<{CONNECTION_LABEL_WIDTH}}",
                style=palette.base,
            )
            identity_text.append(" ", style=palette.base)
            identity_text.append(info.node_id, style=palette.base)
            values.update(identity_text)
        else:
            connecting = self._radio_state is RadioState.CONNECTING
            placeholder = "..." if connecting else "—"
            long_name.set_unavailable(placeholder)
            short_name.set_unavailable(placeholder)
            identity_text = Text()
            identity_text.append(
                f"{CONNECTION_ROW_PREFIX}{'NODE ID':<{CONNECTION_LABEL_WIDTH}}",
                style=palette.base,
            )
            identity_text.append(" ", style=palette.base)
            identity_text.append(placeholder, style=palette.dim_base)
            values.update(identity_text)

    def _show_send_error(self, message: str) -> None:
        self._send_error_message = message
        # Replace, never accumulate, the auto-dismiss timer: any earlier
        # attempt's callback is stopped outright before a new one (if
        # any) is scheduled, so a stale timer can never later clear a
        # newer, unrelated status -- there is only ever at most one
        # pending dismissal for this widget at a time.
        if self._send_error_dismiss_timer is not None:
            self._send_error_dismiss_timer.stop()
            self._send_error_dismiss_timer = None
        if message:
            self._send_error_dismiss_timer = self.set_timer(
                SEND_ERROR_AUTO_DISMISS_SECONDS, self._auto_dismiss_send_error
            )
        self._render_chat_status()

    def _auto_dismiss_send_error(self) -> None:
        self._send_error_dismiss_timer = None
        self._show_send_error("")

    def _show_older_message_notice(
        self,
        channel_index: int,
        entry: ChatEntry,
    ) -> None:
        state = self._state_for(channel_index)
        state.pending_older_ids.add(self._entry_review_identity(entry))
        if channel_index == self.current_channel_index:
            self._render_chat_status()

    def _clear_older_message_notice(self, channel_index: int) -> None:
        self._state_for(channel_index).pending_older_ids.clear()
        self._render_chat_status()

    def _render_chat_status(self) -> None:
        """Send-error / older-message notice only -- connection status

        lives solely in CHAT's top heading now (see
        _update_chat_connection_state/_connection_status_text), never
        duplicated here. Message entry is disabled while not ONLINE
        (see _update_chat_connection_state), so this renders whatever
        it normally would regardless of connection state -- there is
        nothing connection-specific left for it to say.
        """
        widgets = list(self.query("#send-error"))
        if not widgets:
            return
        widget = widgets[0]
        state = self._state_for(self.current_channel_index)
        count = len(state.pending_older_ids)
        show_notice = not self._send_error_message and count > 0
        widget.set_class(show_notice, "older-message-notice")
        if self._send_error_message:
            widget.update(self._send_error_message)
        elif show_notice:
            noun = "MESSAGE" if count == 1 else "MESSAGES"
            widget.update(f"{count} OLDER {noun} RECEIVED")
        else:
            widget.update("")

    def _update_tab_bar(
        self,
        wall_now: float | None = None,
        *,
        mesh_working_set: tuple[MeshNodeState, ...] | None = None,
    ) -> None:
        tab_bars = list(self.query("#tab-bar"))
        if not tab_bars:
            # A final timer tick may race with Textual dismantling the screen.
            return
        labels = []
        for number, (tab_id, name) in enumerate(TAB_NAMES.items(), start=1):
            if tab_id == "chat" and self.unread_count:
                name = f"{name}({self.unread_count})"
            elif tab_id == "mesh":
                count = self._mesh_active_count(wall_now, working_set=mesh_working_set)
                name = f"{name} ({count})"
            label = f"[{number}] {name}"
            labels.append(f"[reverse]{label}[/reverse]" if tab_id == self.current_tab else label)
        tab_bars[0].update("   ".join(labels))

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("[", "\\[")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the MeshtasticPass terminal UI")
    parser.add_argument("--device", default=None, help="override saved serial device")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use the deterministic radio simulator",
    )
    parser.add_argument(
        "--simulate-send-outcome",
        action="append",
        choices=[outcome.value for outcome in SimulatedSendOutcome],
        default=[],
        help="script each simulated send result (may be repeated)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = AppSettings.load()
    device_path = args.device or settings.device_path
    if args.simulate and args.simulate_send_outcome:
        radio = SimulatedRadioService(
            device_path=device_path,
            send_outcomes=tuple(
                SimulatedSendOutcome(value)
                for value in args.simulate_send_outcome
            )
        )
    else:
        radio = create_radio_service(args.simulate, device_path)
    history_error = ""
    try:
        chat_store = ChatStore.open()
    except ChatStoreError as error:
        chat_store = None
        history_error = str(error)
    app = MeshtasticPassApp(
        radio,
        settings,
        chat_store=chat_store,
        history_error=history_error,
    )
    try:
        app.run()
    finally:
        app.restore_terminal_cursor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
