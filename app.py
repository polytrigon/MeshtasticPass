#!/usr/bin/env python3
"""Keyboard-first terminal application for MeshtasticPass."""

from __future__ import annotations

import argparse
from time import monotonic

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
)
from app_settings import AppSettings, COLOR_CHOICES, FONT_SIZE_CHOICES
from radio_service import RadioEvent, RadioInfo, RadioSendError, RadioState, ReceivedMessage
from relative_time import format_relative_age


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


class ChatEntryWidget(Vertical):
    """One chat message whose relative timestamp can refresh in place."""

    def __init__(self, entry: ChatEntry, now: float | None = None) -> None:
        self.entry = entry
        initial_now = monotonic() if now is None else now
        self.timestamp_label = Static(
            format_relative_age(initial_now - entry.accepted_at),
            classes="chat-entry-timestamp",
            markup=False,
        )
        header = Horizontal(
            Static(
                self.entry.author,
                classes="chat-entry-author",
                markup=False,
            ),
            self.timestamp_label,
            classes="chat-entry-header",
        )
        super().__init__(
            header,
            Static(self.entry.text, classes="chat-entry-text", markup=False),
            classes="chat-entry",
        )

    def refresh_timestamp(self, now: float) -> None:
        """Update only the existing timestamp child for this entry."""
        self.timestamp_label.update(
            format_relative_age(now - self.entry.accepted_at),
            layout=False,
        )


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

    .chat-entry-header {
        height: 1;
    }

    .chat-entry-author {
        width: auto;
        margin-right: 2;
        text-style: bold;
    }

    .chat-entry-timestamp {
        width: auto;
        color: #8a8a8a;
    }

    Screen.theme-green .chat-entry-timestamp {
        color: #168f0a;
    }

    Screen.theme-orange .chat-entry-timestamp {
        color: #a85c00;
    }

    .chat-entry-text {
        height: auto;
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

    def __init__(self, radio: object, settings: AppSettings | None = None) -> None:
        super().__init__()
        self.radio = radio
        self.settings = settings or AppSettings.load()
        self.current_tab = "connection"
        self.chat_history: list[ChatEntry] = []
        self.unread_count = 0
        self._radio_state = RadioState.CONNECTING
        self._radio_info: RadioInfo | None = None
        self._status_dot_count = 1
        self._connection_animation_timer: Timer | None = None
        self._chat_timestamp_timer: Timer | None = None
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
                yield Static("> CHAT", classes="page-title")
                yield VerticalScroll(id="chat-log")
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
        self.query_one(FontSizeSelector).focus()
        self._monitor.start()

    def on_unmount(self) -> None:
        if self._connection_animation_timer is not None:
            self._connection_animation_timer.stop()
        if self._chat_timestamp_timer is not None:
            self._chat_timestamp_timer.stop()
        self._monitor.stop()

    def on_key(self, event: Key) -> None:
        """Handle global keys only while the chat input is not focused."""
        if isinstance(self.focused, Input):
            if event.key == "escape":
                self.set_focus(None)
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
        self.current_tab = tab_id
        if tab_id == "chat":
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
        self.run_worker(
            lambda: self._send_from_thread(text),
            thread=True,
            exclusive=True,
        )

    def _send_from_thread(self, text: str) -> None:
        try:
            self.radio.send_text(text, channel_index=0)
        except RadioSendError as error:
            self.call_from_thread(self._show_send_error, str(error))
        else:
            self.call_from_thread(self._accepted_send, text)

    def _accepted_send(self, text: str) -> None:
        entry = outgoing_chat_entry(text)
        self.chat_history.append(entry)
        self._append_chat_widget(entry)
        chat_input = self.query_one("#chat-input", Input)
        chat_input.value = ""
        self._show_send_error("")

    def _radio_event_from_thread(self, event: RadioEvent) -> None:
        try:
            self.call_from_thread(self._apply_radio_event, event)
        except RuntimeError:
            # The UI may already be stopping while the monitor exits.
            pass

    def _message_from_thread(self, message: ReceivedMessage) -> None:
        try:
            self.call_from_thread(self._accept_received_message, message)
        except RuntimeError:
            pass

    def _apply_radio_event(self, event: RadioEvent) -> None:
        self._show_connection(event.state, event.info, event.message)

    def _accept_received_message(self, message: ReceivedMessage) -> None:
        entry = received_chat_entry(message)
        self.chat_history.append(entry)
        self._append_chat_widget(entry)
        if self.current_tab != "chat":
            self.unread_count += 1
            self._update_tab_bar()

    def _append_chat_widget(self, entry: ChatEntry) -> None:
        transcript = self.query_one("#chat-log", VerticalScroll)
        transcript.mount(ChatEntryWidget(entry))
        transcript.scroll_end(animate=False)

    def _refresh_chat_timestamps(self, now: float | None = None) -> None:
        current_time = monotonic() if now is None else now
        for widget in self.query(ChatEntryWidget):
            widget.refresh_timestamp(current_time)

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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    radio = create_radio_service(args.simulate, args.device)
    MeshtasticPassApp(radio, AppSettings.load()).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
