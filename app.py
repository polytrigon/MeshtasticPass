#!/usr/bin/env python3
"""Keyboard-first terminal application for MeshtasticPass."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass, field
from time import monotonic, time

from rich.cells import cell_len
from rich.color import Color
from rich.segment import Segment, Segments
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click, Focus, Key
from textual.message import Message
from textual.scrollbar import ScrollBarRender
from textual.timer import Timer
from textual.widgets import ContentSwitcher, Input, Static

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
from keyboard_dropdown import DropdownOption, KeyboardDropdown
from radio_service import (
    ChannelInfo,
    DeliveryState,
    LONG_NAME_MAX_UTF8_BYTES,
    RadioEvent,
    RadioIdentityError,
    RadioInfo,
    NodeMetadata,
    RadioSendError,
    RadioState,
    ReceivedMessage,
    SendStatus,
    SentMessage,
    validate_long_name,
)
from relative_time import format_relative_age
from simulated_radio_service import SimulatedRadioService, SimulatedSendOutcome
from terminal_cursor import TerminalCursor
from theme_palette import ERROR, THEME_PALETTES
from viewport_menu import PopupItem, ViewportMenu


TAB_NAMES = {
    "connection": "CONNECTION/CONFIG",
    "chat": "CHAT",
    "profile": "PROFILE",
    "pass-map": "PASS MAP",
}

ANIMATED_STATUS = {
    RadioState.CONNECTING: "CONNECTING",
    RadioState.OFFLINE: "OFFLINE — RETRYING",
    RadioState.ERROR: "CONNECTION ERROR — RETRYING",
}

CHAT_CONFIRMATION_TIMEOUT_SECONDS = 300.0
CHAT_SCROLLBAR_THUMB_GLYPH = "▕"
MANUAL_RESEND_STATES = frozenset(
    (DeliveryState.UNCONFIRMED, DeliveryState.FAILED)
)


def can_manual_resend(entry: ChatEntry) -> bool:
    """Return whether the existing explicit rebroadcast action is valid."""
    return entry.outgoing and entry.delivery_state in MANUAL_RESEND_STATES


class ThinScrollBarRender(ScrollBarRender):
    """Use one aligned narrow glyph for both track and draggable thumb."""

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
    """The radio accepted an advertised Long Name update."""

    def __init__(self, info: RadioInfo) -> None:
        super().__init__()
        self.info = info


class IdentitySaveFailed(Message):
    """A Long Name update failed validation or radio submission."""

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
        if event.key in ("up", "down"):
            app._move_chat_focus(-1 if event.key == "up" else 1)
            event.stop()
        elif event.key in ("pageup", "pagedown"):
            step = max(1, self.region.height - 2)
            self.scroll_relative(y=-step if event.key == "pageup" else step)
            app.call_after_refresh(app._clear_indicator_if_at_bottom)
            event.stop()
        elif event.key in ("end", "right"):
            app._jump_to_newest()
            event.stop()


class ConnectionPage(VerticalScroll):
    """Scrollable settings surface for short uConsole terminal windows."""

    def on_mount(self) -> None:
        self.vertical_scrollbar.renderer = ThinScrollBarRender


class LongNameControl(Horizontal):
    """Two-state Long Name control for keyboard navigation and editing."""

    can_focus = True
    MIN_FIELD_WIDTH = 8
    FIXED_ROW_WIDTH = 17  # 13-cell label plus the two bracket widgets.

    def __init__(self) -> None:
        super().__init__(id="identity-long-name")
        self.editing = False
        self._pre_edit_value = ""

    def compose(self) -> ComposeResult:
        yield Static("  LONG NAME", classes="identity-label", markup=False)
        yield Static("[ ", classes="identity-bracket", markup=False)
        yield Input(
            id="long-name-input",
            max_length=LONG_NAME_MAX_UTF8_BYTES,
            disabled=True,
        )
        yield Static(" ]", classes="identity-bracket", markup=False)
        yield Static(
            "...",
            id="identity-long-name-unavailable",
            markup=False,
        )

    @property
    def editor(self) -> Input:
        return self.query_one("#long-name-input", Input)

    def set_available(self, value: str, *, force_value: bool = False) -> None:
        """Show a confirmed value while keeping navigation mode arrow-safe."""
        self.disabled = False
        self.editor.display = True
        self.query_one("#identity-long-name-unavailable", Static).display = False
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
        unavailable = self.query_one("#identity-long-name-unavailable", Static)
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
        self.editor.disabled = False
        self._update_label()
        self.editor.focus()
        self.editor.cursor_position = len(self.editor.value)
        self._resize_field()

    def finish_edit(self, value: str) -> None:
        """Commit the displayed value and return to navigation mode."""
        self.editor.value = value
        self.editing = False
        self.editor.disabled = True
        self._update_label()
        self.focus()
        self._resize_field()

    def cancel_edit(self) -> None:
        """Restore the pre-edit value and return to navigation mode."""
        if self.editing:
            self.editor.value = self._pre_edit_value
        self.editing = False
        self.editor.disabled = True
        self._update_label()
        if not self.disabled:
            self.focus()
        self._resize_field()

    def _update_label(self, *, focused: bool | None = None) -> None:
        navigation_focus = self.has_focus if focused is None else focused
        marker = ">" if (navigation_focus or self.editing) and not self.disabled else " "
        self.query_one(".identity-label", Static).update(f"{marker} LONG NAME")

    def _resize_field(self) -> None:
        if not self.is_mounted:
            return
        desired = max(self.MIN_FIELD_WIDTH, cell_len(self.editor.value))
        desired = min(desired, LONG_NAME_MAX_UTF8_BYTES)
        available = max(1, self.size.width - self.FIXED_ROW_WIDTH)
        self.editor.styles.width = min(desired, available)

    def on_focus(self, _event: Focus) -> None:
        self._update_label(focused=True)

    def on_blur(self) -> None:
        self._update_label(focused=False)

    def on_resize(self) -> None:
        self._resize_field()

    @on(Input.Changed, "#long-name-input")
    def resize_for_value(self) -> None:
        self._resize_field()

    def on_key(self, event: Key) -> None:
        if event.key == "enter" and not self.editing and not self.disabled:
            self.begin_edit()
            event.stop()


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

    #connection-status, #connection-details {
        height: auto;
    }

    #style-title {
        margin-top: 1;
    }

    #identity-long-name {
        height: 1;
        width: 1fr;
        overflow-x: hidden;
    }

    .identity-label {
        width: 13;
        height: 1;
    }

    .identity-bracket {
        width: auto;
        height: 1;
    }

    #identity-long-name-unavailable {
        width: auto;
        height: 1;
        color: $white_dim;
    }

    #long-name-input {
        width: 8;
        height: 1;
        border: none;
        padding: 0;
        background: #101010;
        color: #d8d8d8;
    }

    #long-name-input:disabled {
        color: $white_dim;
        opacity: 1;
    }

    #identity-values, #identity-status {
        height: auto;
        min-height: 1;
    }

    #identity-status {
        color: #39ff14;
    }

    #identity-status.setting-error {
        color: $error;
    }

    Screen.theme-green #long-name-input {
        color: #39ff14;
    }

    Screen.theme-orange #long-name-input {
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

    Screen.theme-green #identity-long-name-unavailable {
        color: $green_dim;
    }

    Screen.theme-orange #identity-long-name-unavailable {
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

    .chat-entry:focus {
        background: #181818;
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

    .chat-entry.delivery-failed .chat-entry-delivery {
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
        self._arrival_sequence = 0
        self._send_dot_count = 1
        self._has_older_history = False
        self._mounted_chat_target = DEFAULT_HISTORY_LIMIT
        self._chat_open_scroll_pending = False
        self._user_menu: ViewportMenu | None = None
        self._user_menu_origin: ChatEntryWidget | None = None
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
                yield Input(
                    placeholder="> message",
                    id="chat-input",
                    select_on_focus=False,
                )
            with Vertical(id="profile", classes="tab-page"):
                yield Static("> PROFILE", classes="page-title")
                yield Static("Coming in a future milestone.")
            with Vertical(id="pass-map", classes="tab-page"):
                yield Static("> PASS MAP", classes="page-title")
                yield Static("Coming in a future milestone.")
        yield Static("1-4 switch tabs    F4 quit", id="footer")

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
        self.restore_terminal_cursor()
        self._monitor.stop()
        if self.chat_store is not None:
            self.chat_store.close()

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
            elif self.focused.id == "long-name-input" and event.key == "escape":
                self.query_one(LongNameControl).cancel_edit()
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
            if event.key in ("end", "right"):
                self._jump_to_newest()
                event.stop()
                return

        if self.current_tab == "connection" and event.key in ("up", "down"):
            controls = [
                control
                for control in (
                    self.query_one(DeviceSelector),
                    self.query_one(LongNameControl),
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

        tab_for_key = {
            "1": "connection",
            "2": "chat",
            "3": "profile",
            "4": "pass-map",
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
            self.post_message(IdentitySaved(info))

    @on(IdentitySaved)
    def identity_saved(self, event: IdentitySaved) -> None:
        self._radio_info = event.info
        self._render_identity(force_value=True)
        status = self.query_one("#identity-status", Static)
        status.remove_class("setting-error")
        status.update("NAME SAVED")

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
            self._render_identity()

    @on(Input.Submitted, "#chat-input")
    def send_chat_message(self, event: Input.Submitted) -> None:
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
        if not self._persist_incoming(entry):
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
        if not outside_mounted_window:
            state.entries.insert(insert_index, entry)
            if channel_index == self.current_channel_index:
                self._insert_chat_widget(entry, insert_index, older=is_older)
        else:
            state.has_older_history = True
            if channel_index == self.current_channel_index:
                self._has_older_history = True
                self._ensure_load_older_control(
                    self.query_one("#chat-log", ChatTranscript)
                )
                self._capture_current_channel_state()
        if is_older:
            self._show_older_message_notice(channel_index, entry)
        if not chat_is_visible:
            state.unread_count += 1
            self._recount_unread()
            self._update_tab_bar()
        self._render_chat_heading()

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
        self._render_chat_heading()
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
        self._render_chat_heading(wall_now)

    def _render_chat_heading(self, wall_now: float | None = None) -> None:
        selectors = list(self.query(ChannelSelector))
        if not selectors:
            # A final timer tick may race with Textual dismantling the screen.
            return
        active: int | None = None
        if self._radio_state is RadioState.ONLINE:
            counter = getattr(self.radio, "active_node_count", None)
            if callable(counter):
                try:
                    active = counter(now=time() if wall_now is None else wall_now)
                except Exception:
                    active = None
        value = "—" if active is None else str(active)
        selectors[0].set_suffix(f"· ACTIVE {value}")

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
                )

        items: list[PopupItem] = []
        if metadata.long_name:
            items.append(PopupItem(metadata.long_name, actionable=False))
        if metadata.short_name:
            items.append(PopupItem(metadata.short_name, actionable=False))
        if metadata.hops_away is not None:
            unit = "HOP" if metadata.hops_away == 1 else "HOPS"
            items.append(
                PopupItem(
                    f"{metadata.hops_away} {unit} AWAY",
                    actionable=False,
                )
            )
        favorite = self.settings.is_favorite(entry.node_id)
        action = "unfavorite" if favorite else "favorite"
        items.append(PopupItem(action.upper(), action, actionable=True))
        highlighted = len(items) - 1
        menu = ViewportMenu(
            items,
            highlighted_index=highlighted,
            on_activate=lambda _index, item: self._activate_user_action(
                event.widget,
                str(item.value),
            ),
            menu_id="node-context-menu",
        )
        self._close_user_menu(restore_focus=False)
        self._user_menu = menu
        self._user_menu_origin = event.widget
        self._user_menu_scroll_y = self.query_one(ChatTranscript).scroll_y
        width = max(len(item.label) for item in items) + 4
        self.screen.mount(menu)
        menu.place(event.widget.region, self.screen.region, width)

    def _activate_user_action(
        self,
        origin: ChatEntryWidget,
        action: str,
    ) -> None:
        node_id = origin.entry.node_id
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
        self._close_user_menu()

    def _close_user_menu(self, *, restore_focus: bool = True) -> None:
        menu = self._user_menu
        origin = self._user_menu_origin
        scroll_y = self._user_menu_scroll_y
        self._user_menu = None
        self._user_menu_origin = None
        self._user_menu_scroll_y = None
        if menu is not None:
            menu.remove()
        if restore_focus and origin is not None and origin.is_mounted:
            origin.focus()
            if scroll_y is not None:
                transcript = self.query_one(ChatTranscript)
                self.call_after_refresh(
                    transcript.scroll_to,
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
            text = "1-4 switch tabs    F4 quit"
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
        self._render_chat_heading()
        self.query_one("#connection-error", Static).update("")

    def _advance_connection_animation(self) -> None:
        if self._radio_state is RadioState.ONLINE:
            return
        self._status_dot_count = self._status_dot_count % 3 + 1
        self._render_connection_details()

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
        statuses[0].update(f"{'STATUS':<12} {status}")
        values = []
        if self._radio_state is RadioState.ONLINE and self._radio_info is not None:
            info = self._radio_info
            values.extend(
                [
                    ("NODES", str(info.known_nodes)),
                ]
            )
        if values:
            details = "\n".join(f"{label:<12} {value}" for label, value in values)
            detail_widgets[0].update(details)
        else:
            placeholder = "..." if self._radio_state is RadioState.CONNECTING else "—"
            palette = THEME_PALETTES[self._current_theme]
            details = Text()
            details.append(f"{'NODES':<12} ")
            details.append(placeholder, style=palette.dim_base)
            detail_widgets[0].update(details)

    def _render_identity(self, force_value: bool = False) -> None:
        control = self.query_one(LongNameControl)
        values = self.query_one("#identity-values", Static)
        online = self._radio_state is RadioState.ONLINE and self._radio_info is not None
        if online:
            info = self._radio_info
            assert info is not None
            control.set_available(info.long_name, force_value=force_value)
            values.update(
                f"{'SHORT NAME':<12} {info.short_name}\n"
                f"{'NODE ID':<12} {info.node_id}"
            )
        else:
            connecting = self._radio_state is RadioState.CONNECTING
            placeholder = "..." if connecting else "—"
            control.set_unavailable(placeholder)
            palette = THEME_PALETTES[self._current_theme]
            identity_text = Text()
            identity_text.append(f"{'SHORT NAME':<12} ")
            identity_text.append(placeholder, style=palette.dim_base)
            identity_text.append(f"\n{'NODE ID':<12} ")
            identity_text.append(placeholder, style=palette.dim_base)
            values.update(identity_text)

    def _show_send_error(self, message: str) -> None:
        self._send_error_message = message
        self._render_chat_status()

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

    def _update_tab_bar(self) -> None:
        labels = []
        for number, (tab_id, name) in enumerate(TAB_NAMES.items(), start=1):
            if tab_id == "chat" and self.unread_count:
                name = f"{name}({self.unread_count})"
            label = f"[{number}] {name}"
            labels.append(f"[reverse]{label}[/reverse]" if tab_id == self.current_tab else label)
        self.query_one("#tab-bar", Static).update("   ".join(labels))

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
