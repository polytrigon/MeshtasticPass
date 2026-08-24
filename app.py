#!/usr/bin/env python3
"""Keyboard-first terminal application for MeshtasticPass."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from time import monotonic, time

from rich.color import Color
from rich.segment import Segment, Segments
from rich.style import Style
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Focus, Key
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
    RadioEvent,
    RadioInfo,
    RadioSendError,
    RadioState,
    ReceivedMessage,
    SendStatus,
    SentMessage,
)
from relative_time import format_relative_age
from simulated_radio_service import SimulatedRadioService, SimulatedSendOutcome
from terminal_cursor import TerminalCursor


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
    """Use a narrow one-cell thumb while preserving Textual scroll behavior."""

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

        thumb_style = Style(
            color=bar_color,
            bgcolor=back_color,
            meta={"@mouse.down": "grab"},
        )
        segments = [
            Segment(CHAT_SCROLLBAR_THUMB_GLYPH * thickness, thumb_style)
            if segment.style is not None
            and segment.style.meta.get("@mouse.down") == "grab"
            else segment
            for segment in rendered.segments
        ]
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
            "CHAT —",
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


class EndOfChatControl(Static):
    """Focusable navigation target for returning to the newest message."""

    can_focus = True

    class Activated(Message):
        pass

    def __init__(self) -> None:
        super().__init__(
            "[ END OF CHAT ]",
            id="end-of-chat",
            classes="chat-nav-target",
            markup=False,
        )

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.post_message(self.Activated())
            event.stop()


class ChatTranscript(VerticalScroll):
    """Focusable transcript so navigation never types into the message box."""

    can_focus = True

    def on_mount(self) -> None:
        self.vertical_scrollbar.renderer = ThinScrollBarRender

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
        elif event.key == "end":
            app._jump_to_newest()
            event.stop()


class ChatEntryWidget(Vertical):
    """One chat message whose relative timestamp can refresh in place."""

    can_focus = True

    class SelectionChanged(Message):
        pass

    def __init__(self, entry: ChatEntry, now: float | None = None) -> None:
        self.entry = entry
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
        super().__init__(
            Horizontal(
                self.selection_marker,
                Vertical(header, self.message_label, classes="chat-entry-content"),
                classes="chat-entry-row",
            ),
            self.action_control,
            classes="chat-entry new-message" if is_new else "chat-entry",
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
        self.post_message(self.SelectionChanged())

    def on_blur(self, _event: object) -> None:
        self.selection_marker.update(" ")
        self.post_message(self.SelectionChanged())


class MeshtasticPassApp(App[None]):
    """The first MeshtasticPass terminal UI shell."""

    TITLE = "MeshtasticPass"
    CSS = """
    Screen {
        background: #101010;
        color: #d8d8d8;
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
        color: #8a8a8a;
    }

    #content {
        height: 1fr;
        padding: 0 2;
    }

    .tab-page {
        height: 1fr;
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

    #connection-error, #send-error, #style-status.setting-error {
        height: auto;
        min-height: 1;
        color: #ff1744;
    }

    #chat-log {
        height: 1fr;
        scrollbar-size: 1 1;
        scrollbar-color: #d8d8d8;
        scrollbar-color-hover: #d8d8d8;
        scrollbar-color-active: #d8d8d8;
        scrollbar-background: #2c2c2c;
        scrollbar-background-hover: #2c2c2c;
        scrollbar-background-active: #2c2c2c;
    }

    Screen.theme-green #chat-log {
        scrollbar-color: #39ff14;
        scrollbar-color-hover: #39ff14;
        scrollbar-color-active: #39ff14;
        scrollbar-background: #0e5f08;
        scrollbar-background-hover: #0e5f08;
        scrollbar-background-active: #0e5f08;
    }

    Screen.theme-orange #chat-log {
        scrollbar-color: #ff8c00;
        scrollbar-color-hover: #ff8c00;
        scrollbar-color-active: #ff8c00;
        scrollbar-background: #6f3d00;
        scrollbar-background-hover: #6f3d00;
        scrollbar-background-active: #6f3d00;
    }

    #load-older, #end-of-chat, .message-action {
        width: auto;
        height: 1;
        color: #d8d8d8;
        margin-bottom: 1;
    }

    #load-older:focus, #end-of-chat:focus, .message-action:focus {
        text-style: reverse;
    }

    Screen.theme-green #load-older,
    Screen.theme-green #end-of-chat,
    Screen.theme-green .message-action {
        color: #39ff14;
    }

    Screen.theme-orange #load-older,
    Screen.theme-orange #end-of-chat,
    Screen.theme-orange .message-action {
        color: #ff8c00;
    }

    .chat-entry {
        height: auto;
        margin-bottom: 1;
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

    .chat-entry-separator, .chat-entry-delivery {
        width: auto;
        color: #8a8a8a;
    }

    .chat-entry-timestamp, .chat-entry-distance {
        width: auto;
        color: #8a8a8a;
        text-style: dim;
    }

    Screen.theme-green .chat-entry-timestamp,
    Screen.theme-green .chat-entry-distance {
        color: #168f0a;
    }

    Screen.theme-orange .chat-entry-timestamp,
    Screen.theme-orange .chat-entry-distance {
        color: #a85c00;
    }

    Screen.theme-green .chat-entry-separator,
    Screen.theme-green .chat-entry-delivery {
        color: #168f0a;
    }

    Screen.theme-orange .chat-entry-separator,
    Screen.theme-orange .chat-entry-delivery {
        color: #a85c00;
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
        color: #ff1744;
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
        border-top: solid #4a4a4a;
        color: #8a8a8a;
    }

    Screen.theme-green #tab-bar,
    Screen.theme-green #footer {
        color: #168f0a;
    }

    Screen.theme-orange #tab-bar,
    Screen.theme-orange #footer {
        color: #a85c00;
    }

    Screen.theme-green #footer {
        border-top: solid #0e5f08;
    }

    Screen.theme-orange #footer {
        border-top: solid #6f3d00;
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
        self._send_dot_count = 1
        self._has_older_history = False
        self._mounted_chat_target = DEFAULT_HISTORY_LIMIT
        self._chat_open_scroll_pending = False
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
            with Vertical(id="connection", classes="tab-page"):
                yield Static("> CONNECTION", classes="page-title")
                yield Static(id="connection-status")
                yield DeviceSelector(self.settings.device_path, devices)
                yield Static(id="connection-details")
                yield Static(id="connection-error")
                yield Static("> STYLE", id="style-title", classes="page-title")
                yield FontSizeSelector(self.settings.font_size)
                yield ColorSelector(self.settings.color)
                yield Static(id="style-status")
            with Vertical(id="chat", classes="tab-page"):
                yield ChannelSelector(self._channels, self.current_channel_index)
                with ChatTranscript(id="chat-log"):
                    yield EndOfChatControl()
                yield Static(id="chat-new-below")
                yield Static(id="send-error")
                yield Input(placeholder="> message", id="chat-input")
            with Vertical(id="profile", classes="tab-page"):
                yield Static("> PROFILE", classes="page-title")
                yield Static("Coming in a future milestone.")
            with Vertical(id="pass-map", classes="tab-page"):
                yield Static("> PASS MAP", classes="page-title")
                yield Static("Coming in a future milestone.")
        yield Static("1-4 switch tabs    Q quit", id="footer")

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
        if isinstance(self.focused, KeyboardDropdown) and self.focused.is_open:
            return
        if isinstance(self.focused, Input):
            if event.key == "escape":
                self.query_one("#chat-log", ChatTranscript).focus()
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
            if event.key == "end":
                self._jump_to_newest()
                event.stop()
                return

        if self.current_tab == "connection" and event.key in ("up", "down"):
            controls = [
                self.query_one(DeviceSelector),
                self.query_one(FontSizeSelector),
                self.query_one(ColorSelector),
            ]
            if self.focused in controls:
                current = controls.index(self.focused)
                step = -1 if event.key == "up" else 1
                controls[(current + step) % len(controls)].focus()
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
        elif event.key.lower() == "q":
            self.exit()
            event.stop()

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
            status.add_class("setting-error")
            status.update(f"SETTING NOT SAVED — {error}")
        else:
            status.remove_class("setting-error")
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
        for name, _value in COLOR_CHOICES:
            self.screen.remove_class(f"theme-{name.lower()}")
        self.screen.add_class(f"theme-{color}")
        palettes = {
            "white": ("#d8d8d8", "#39ff14", "#4a4a4a"),
            "green": ("#39ff14", "#ff8c00", "#0e5f08"),
            "orange": ("#ff8c00", "#d8d8d8", "#6f3d00"),
        }
        for dropdown in self.query(KeyboardDropdown):
            dropdown.set_palette(*palettes[color])

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
        self.chat_history.append(entry)
        self._persist_outgoing(entry)
        self._append_chat_widget(entry)
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
        self.chat_history.append(entry)
        self._persist_outgoing(entry)
        self._append_chat_widget(entry)
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
        if not self._persist_incoming(entry):
            return
        state.entries.append(entry)
        if channel_index == self.current_channel_index:
            self._append_chat_widget(entry)
        if not chat_is_visible:
            state.unread_count += 1
            self._recount_unread()
            self._update_tab_bar()

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
        self.transcript_new_count = state.transcript_new_count
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
        widgets.extend(ChatEntryWidget(entry) for entry in state.entries)
        widgets.append(EndOfChatControl())
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

    def _append_chat_widget(self, entry: ChatEntry) -> None:
        transcript = self.query_one("#chat-log", ChatTranscript)
        end_control = self.query_one(EndOfChatControl)
        if self.current_tab != "chat":
            transcript.mount(ChatEntryWidget(entry), before=end_control)
            self._trim_mounted_chat_window(transcript)
            self._chat_open_scroll_pending = True
            return
        should_follow = self._is_near_chat_bottom()
        transcript.mount(ChatEntryWidget(entry), before=end_control)
        self._trim_mounted_chat_window(transcript)
        if should_follow:
            self.call_after_refresh(self._jump_to_newest)
        else:
            self.transcript_new_count += 1
            self._update_transcript_indicator()

    def _trim_mounted_chat_window(self, transcript: ChatTranscript) -> None:
        """Bound the mounted window without hiding NEW/unread messages."""
        trimmed = False
        while len(self.chat_history) > self._mounted_chat_target:
            removable_index = next(
                (
                    index
                    for index, candidate in enumerate(self.chat_history)
                    if candidate.message_id is not None
                    and not candidate.is_new
                    and not candidate.unread
                ),
                None,
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
        if len(self.query(LoadOlderControl)):
            return
        first_widget = next(iter(self.query(ChatEntryWidget)), None)
        before = first_widget or self.query_one(EndOfChatControl)
        transcript.mount(LoadOlderControl(), before=before)

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
        selectors[0].set_suffix(f"— ACTIVE {value}")

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
        transcript.scroll_end(animate=False)
        self.transcript_new_count = 0
        self._update_transcript_indicator()

    def _chat_navigation_targets(self) -> list[Static | ChatEntryWidget]:
        targets: list[Static | ChatEntryWidget] = []
        transcript = self.query_one(ChatTranscript)
        for widget in transcript.walk_children():
            if isinstance(
                widget,
                (LoadOlderControl, ChatEntryWidget, MessageActionControl, EndOfChatControl),
            ) and widget.display:
                targets.append(widget)
        return targets

    def _move_chat_focus(self, direction: int) -> None:
        targets = self._chat_navigation_targets()
        if not targets:
            return
        try:
            index = targets.index(self.focused)
            target = targets[max(0, min(len(targets) - 1, index + direction))]
        except ValueError:
            target = targets[-1] if direction < 0 else targets[0]
        target.focus()
        target.scroll_visible(animate=False)
        self.call_after_refresh(self._clear_indicator_if_at_bottom)

    @on(EndOfChatControl.Activated)
    def end_of_chat_activated(self, _event: EndOfChatControl.Activated) -> None:
        self._jump_to_newest()
        self.query_one(EndOfChatControl).focus()

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

    def _clear_indicator_if_at_bottom(self) -> None:
        if self._is_near_chat_bottom() and self.transcript_new_count:
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
        for entry in self.chat_history:
            if entry.unread:
                entry.unread = False
        self._state_for(self.current_channel_index).unread_count = 0

    def _mark_new_messages_read(self) -> None:
        found_new = False
        for entry in self.chat_history:
            if entry.is_new:
                entry.is_new = False
                found_new = True
        if not found_new:
            return
        for widget in self.query(ChatEntryWidget):
            widget.refresh_new_message_state()

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
        for entry in state.entries:
            widgets.append(ChatEntryWidget(entry))
        if widgets:
            transcript.mount(*widgets, before=self.query_one(EndOfChatControl))

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
            self._capture_current_channel_state()
            return

        transcript = self.query_one("#chat-log", ChatTranscript)
        old_scroll_y = transcript.scroll_y
        old_virtual_height = transcript.virtual_size.height
        first_widget = next(iter(self.query(ChatEntryWidget)), None)
        entries = [stored_chat_entry(stored) for stored in page.messages]
        widgets = [ChatEntryWidget(entry) for entry in entries]
        self.chat_history[0:0] = entries
        self._mounted_chat_target += len(entries)
        await transcript.mount(*widgets, before=first_widget)
        self._has_older_history = page.has_older
        self._capture_current_channel_state()
        if not page.has_older:
            control = self.query_one("#load-older", LoadOlderControl)
            await control.remove()
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
        _event: ChatEntryWidget.SelectionChanged,
    ) -> None:
        self._update_footer()

    def _update_footer(self) -> None:
        text = "1-4 switch tabs    Q quit"
        if self.current_tab == "chat" and isinstance(self.focused, Input):
            text += "    ENTER send    ESC messages"
        elif self.current_tab == "chat":
            text += "    ↑↓ navigate    C channel    ENTER action    END newest"
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
        self._render_connection_details()
        self._render_chat_heading()
        self.query_one("#connection-error", Static).update("")

    def _advance_connection_animation(self) -> None:
        if self._radio_state is RadioState.ONLINE:
            return
        self._status_dot_count = self._status_dot_count % 3 + 1
        self._render_connection_details()

    def _render_connection_details(self) -> None:
        status = (
            "CONNECTED"
            if self._radio_state is RadioState.ONLINE
            else ANIMATED_STATUS[self._radio_state] + "." * self._status_dot_count
        )
        self.query_one("#connection-status", Static).update(
            f"{'STATUS':<12} {status}"
        )
        values = []
        if self._radio_state is RadioState.ONLINE and self._radio_info is not None:
            info = self._radio_info
            values.extend(
                [
                    ("NODE", info.node_id),
                    ("NAME", f"{info.long_name} ({info.short_name})"),
                    ("FIRMWARE", info.firmware_version),
                    ("NODES", str(info.known_nodes)),
                ]
            )
        details = "\n".join(f"{label:<10} {value}" for label, value in values)
        self.query_one("#connection-details", Static).update(details)

    def _show_send_error(self, message: str) -> None:
        self.query_one("#send-error", Static).update(message)

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
