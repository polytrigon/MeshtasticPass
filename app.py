#!/usr/bin/env python3
"""Keyboard-first terminal application for MeshtasticPass."""

from __future__ import annotations

import argparse
from time import monotonic, time

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Blur, Focus, Key
from textual.message import Message
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
from chat_store import ChatStore, ChatStoreError
from radio_service import (
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


class StyleSelector(Static):
    """One focusable row in the keyboard-driven STYLE section."""

    can_focus = True

    class Changed(Message):
        def __init__(self, setting: str, value: object) -> None:
            super().__init__()
            self.setting = setting
            self.value = value

    def __init__(
        self,
        setting: str,
        label: str,
        choices: tuple[tuple[str, object], ...],
        value: object,
        widget_id: str,
    ) -> None:
        super().__init__(id=widget_id, classes="style-selector")
        self.setting = setting
        self.label = label
        self.choices = choices
        self.value = value
        self._active = False

    def on_mount(self) -> None:
        self._update_display()

    def on_key(self, event: Key) -> None:
        if event.key in ("up", "down"):
            selectors = list(self.screen.query(StyleSelector))
            current_index = selectors.index(self)
            direction = -1 if event.key == "up" else 1
            selectors[(current_index + direction) % len(selectors)].focus()
            event.stop()
            return
        if event.key not in ("left", "right"):
            return

        values = [value for _name, value in self.choices]
        current_index = values.index(self.value)
        direction = -1 if event.key == "left" else 1
        self.value = values[(current_index + direction) % len(values)]
        self._update_display()
        self.post_message(self.Changed(self.setting, self.value))
        event.stop()

    def on_focus(self, _event: Focus) -> None:
        self._active = True
        self._update_display()

    def on_blur(self, _event: Blur) -> None:
        self._active = False
        self._update_display()

    def _update_display(self) -> None:
        options = []
        for name, value in self.choices:
            suffix = f" {value}" if self.setting == "font_size" else ""
            label = f"\\[ {name}{suffix} ]"
            if value == self.value:
                label = f"[reverse][b]{label}[/b][/reverse]"
            options.append(label)
        focus_marker = ">" if self._active else " "
        self.update(f"{focus_marker} {self.label:<10}" + " ".join(options))


class FontSizeSelector(StyleSelector):
    def __init__(self, font_size: int) -> None:
        super().__init__(
            "font_size",
            "FONT SIZE",
            FONT_SIZE_CHOICES,
            font_size,
            "font-size-selector",
        )

    @property
    def font_size(self) -> int:
        return int(self.value)


class ColorSelector(StyleSelector):
    def __init__(self, color: str) -> None:
        super().__init__("color", "COLOR", COLOR_CHOICES, color, "color-selector")

    @property
    def color(self) -> str:
        return str(self.value)


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


class ChatTranscript(VerticalScroll):
    """Focusable transcript so navigation never types into the message box."""

    can_focus = True

    def on_key(self, event: Key) -> None:
        """Keep transcript navigation deterministic when this widget has focus."""
        app = self.app
        if event.key in ("up", "down"):
            self.scroll_relative(y=-1 if event.key == "up" else 1)
            app.call_after_refresh(app._clear_indicator_if_at_bottom)
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
            format_relative_age(initial_now - entry.age_reference),
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
        header = Horizontal(*header_parts, classes="chat-entry-header")
        self.message_label = Static(
            self.entry.text,
            classes="chat-entry-text",
            markup=False,
        )
        super().__init__(
            header,
            self.message_label,
            classes="chat-entry new-message" if is_new else "chat-entry",
        )
        self.refresh_delivery_state(1)

    def refresh_timestamp(self, now: float) -> None:
        """Update only the existing timestamp child for this entry."""
        self.timestamp_label.update(
            format_relative_age(now - self.entry.age_reference),
            layout=False,
        )

    def refresh_new_message_state(self) -> None:
        """Apply the entry's persistent new/read presentation state."""
        self.set_class(self.entry.is_new and not self.entry.outgoing, "new-message")

    def refresh_delivery_state(self, dot_count: int) -> None:
        if self.delivery_label is None:
            return
        state = self.entry.delivery_state or DeliveryState.SENT
        text = state.value
        if state is DeliveryState.SENDING:
            text += "." * dot_count
        self.delivery_label.update(text, layout=False)
        for name in DeliveryState:
            self.set_class(name is state, f"delivery-{name.value.lower()}")

    def on_focus(self, _event: Focus) -> None:
        self.post_message(self.SelectionChanged())

    def on_blur(self, _event: Blur) -> None:
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

    #connection-details {
        height: auto;
    }

    #style-title {
        margin-top: 1;
    }

    .style-selector {
        height: 2;
        color: #d8d8d8;
    }

    .style-selector:focus {
        color: #f2f2f2;
    }

    Screen.theme-green .style-selector,
    Screen.theme-green #chat-input {
        color: #39ff14;
    }

    Screen.theme-orange .style-selector,
    Screen.theme-orange #chat-input {
        color: #ff8c00;
    }

    Screen.theme-green .style-selector:focus,
    Screen.theme-green .page-title {
        color: #7cff6b;
    }

    Screen.theme-orange .style-selector:focus,
    Screen.theme-orange .page-title {
        color: #ffb000;
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
    }

    .chat-entry {
        height: auto;
        margin-bottom: 1;
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

    .chat-entry-timestamp {
        width: auto;
        color: #8a8a8a;
        text-style: dim;
    }

    Screen.theme-green .chat-entry-timestamp {
        color: #168f0a;
    }

    Screen.theme-orange .chat-entry-timestamp {
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

    .chat-entry.delivery-sending .chat-entry-delivery {
        color: #f2f2f2;
    }

    Screen.theme-green .chat-entry.delivery-sending .chat-entry-delivery {
        color: #39ff14;
    }

    Screen.theme-orange .chat-entry.delivery-sending .chat-entry-delivery {
        color: #ff8c00;
    }

    .chat-entry.delivery-unconfirmed .chat-entry-delivery {
        color: #ffb000;
    }

    .chat-entry.delivery-failed .chat-entry-delivery {
        color: #ff1744;
    }

    .chat-entry-text {
        height: auto;
    }

    Screen.theme-white .chat-entry.new-message .chat-entry-author,
    Screen.theme-white .chat-entry.new-message .chat-entry-timestamp,
    Screen.theme-white .chat-entry.new-message .chat-entry-text {
        color: #39ff14;
    }

    Screen.theme-green .chat-entry.new-message .chat-entry-author,
    Screen.theme-green .chat-entry.new-message .chat-entry-timestamp,
    Screen.theme-green .chat-entry.new-message .chat-entry-text {
        color: #ff8c00;
    }

    Screen.theme-orange .chat-entry.new-message .chat-entry-author,
    Screen.theme-orange .chat-entry.new-message .chat-entry-timestamp,
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
        self.chat_history: list[ChatEntry] = []
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
        self._terminal_cursor = terminal_cursor or TerminalCursor()
        self._monitor = RadioMonitor(
            radio,
            self._radio_event_from_thread,
            self._message_from_thread,
        )

    def compose(self) -> ComposeResult:
        yield Static(id="tab-bar")
        with ContentSwitcher(initial="connection", id="content"):
            with Vertical(id="connection", classes="tab-page"):
                yield Static("> CONNECTION", classes="page-title")
                yield Static(id="connection-details")
                yield Static(id="connection-error")
                yield Static("> STYLE", id="style-title", classes="page-title")
                yield FontSizeSelector(self.settings.font_size)
                yield ColorSelector(self.settings.color)
                yield Static(id="style-status")
            with Vertical(id="chat", classes="tab-page"):
                yield Static("> CHAT — PRIMARY", id="chat-title", classes="page-title")
                yield ChatTranscript(id="chat-log")
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
            12.0,
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
        if isinstance(self.focused, Input):
            if event.key == "escape":
                self.query_one("#chat-log", ChatTranscript).focus()
                event.stop()
            return

        if self.current_tab == "chat":
            transcript = self.query_one("#chat-log", ChatTranscript)
            if event.key in ("up", "down"):
                transcript.scroll_relative(y=-1 if event.key == "up" else 1)
                self.call_after_refresh(self._clear_indicator_if_at_bottom)
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
            if event.key == "end":
                self._jump_to_newest()
                event.stop()
                return
            if event.key.lower() == "r" and isinstance(
                self.focused, ChatEntryWidget
            ):
                if self.focused.entry.delivery_state is DeliveryState.UNCONFIRMED:
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
            self.unread_count = 0
            self._refresh_chat_timestamps()
        self.query_one("#content", ContentSwitcher).current = tab_id
        self._update_tab_bar()
        if tab_id == "chat":
            self.query_one("#chat-input", Input).focus()
        elif tab_id == "connection":
            self.query_one(FontSizeSelector).focus()
        else:
            self.set_focus(None)
        self._update_footer()

    @on(StyleSelector.Changed)
    def save_style_setting(self, event: StyleSelector.Changed) -> None:
        status = self.query_one("#style-status", Static)
        try:
            if event.setting == "font_size":
                self.settings.set_font_size(int(event.value))
                self.settings.save()
                self.settings.update_lxterminal_profile()
            elif event.setting == "color":
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
                if event.setting == "font_size"
                else "COLOR SAVED"
            )
            status.update(message)

    def _apply_color_theme(self, color: str) -> None:
        for name, _value in COLOR_CHOICES:
            self.screen.remove_class(f"theme-{name.lower()}")
        self.screen.add_class(f"theme-{color}")

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
        entry = outgoing_chat_entry(text, delivery_state=DeliveryState.SENDING)
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
        entry = outgoing_chat_entry(text, delivery_state=DeliveryState.SENT)
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
        if (message.channel_index or 0) != 0:
            return
        received_at = time()
        monotonic_now = monotonic()
        chat_is_visible = self.current_tab == "chat"
        entry = received_chat_entry(
            message,
            received_at=received_at,
            monotonic_now=monotonic_now,
            unread=not chat_is_visible,
            is_new=True,
        )
        if not self._persist_incoming(entry):
            return
        self.chat_history.append(entry)
        self._append_chat_widget(entry)
        if not chat_is_visible:
            self.unread_count += 1
            self._update_tab_bar()

    def _append_chat_widget(self, entry: ChatEntry) -> None:
        transcript = self.query_one("#chat-log", ChatTranscript)
        should_follow = self._is_near_chat_bottom()
        transcript.mount(ChatEntryWidget(entry))
        if should_follow:
            self.call_after_refresh(self._jump_to_newest)
        else:
            self.transcript_new_count += 1
            self._update_transcript_indicator()

    def _refresh_chat_timestamps(self, now: float | None = None) -> None:
        current_time = monotonic() if now is None else now
        for widget in self.query(ChatEntryWidget):
            widget.refresh_timestamp(current_time)

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

    def _load_chat_history(self) -> None:
        if self.chat_store is None:
            return
        try:
            stored_messages = self.chat_store.load_recent(channel_index=0)
        except ChatStoreError as error:
            self._show_send_error(str(error))
            return
        transcript = self.query_one("#chat-log", ChatTranscript)
        for stored in stored_messages:
            entry = stored_chat_entry(stored)
            self.chat_history.append(entry)
            transcript.mount(ChatEntryWidget(entry))
        if stored_messages:
            self.call_after_refresh(self._jump_to_newest)

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
                received_at=entry.received_at,
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
                local_sent_at=entry.local_sent_at or entry.received_at,
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
        if entry.delivery_state is not DeliveryState.UNCONFIRMED:
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
        if (
            isinstance(self.focused, ChatEntryWidget)
            and self.focused.entry.delivery_state is DeliveryState.UNCONFIRMED
        ):
            text += "    [R] REBROADCAST"
        elif self.current_tab == "chat":
            text += "    ESC scroll    END newest"
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
        self._status_dot_count = 1
        if self._connection_animation_timer is not None:
            if state is RadioState.ONLINE:
                self._connection_animation_timer.pause()
            else:
                self._connection_animation_timer.resume()
        self._render_connection_details()
        self.query_one("#connection-error", Static).update("")

    def _advance_connection_animation(self) -> None:
        if self._radio_state is RadioState.ONLINE:
            return
        self._status_dot_count = self._status_dot_count % 3 + 1
        self._render_connection_details()

    def _render_connection_details(self) -> None:
        status = (
            "ONLINE"
            if self._radio_state is RadioState.ONLINE
            else ANIMATED_STATUS[self._radio_state] + "." * self._status_dot_count
        )
        values = [
            ("STATUS", status),
            ("DEVICE", getattr(self.radio, "device_path", "unknown")),
        ]
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
    parser.add_argument("--device", default="/dev/ttyUSB0", help="serial device")
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
    if args.simulate and args.simulate_send_outcome:
        radio = SimulatedRadioService(
            send_outcomes=tuple(
                SimulatedSendOutcome(value)
                for value in args.simulate_send_outcome
            )
        )
    else:
        radio = create_radio_service(args.simulate, args.device)
    history_error = ""
    try:
        chat_store = ChatStore.open()
    except ChatStoreError as error:
        chat_store = None
        history_error = str(error)
    app = MeshtasticPassApp(
        radio,
        AppSettings.load(),
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
