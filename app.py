#!/usr/bin/env python3
"""Keyboard-first terminal application for MeshtasticPass."""

from __future__ import annotations

import argparse

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Key
from textual.message import Message
from textual.widgets import ContentSwitcher, Input, Static

from app_controller import (
    ChatEntry,
    RadioMonitor,
    create_radio_service,
    outgoing_chat_entry,
    received_chat_entry,
)
from app_settings import AppSettings, FONT_SIZE_CHOICES
from radio_service import RadioEvent, RadioInfo, RadioSendError, RadioState, ReceivedMessage


TAB_NAMES = {
    "connection": "CONNECTION/CONFIG",
    "chat": "CHAT",
    "profile": "PROFILE",
    "pass-map": "PASS MAP",
}


class FontSizeSelector(Static):
    """Focusable keyboard control for the four supported terminal font sizes."""

    can_focus = True

    class Changed(Message):
        def __init__(self, font_size: int) -> None:
            super().__init__()
            self.font_size = font_size

    def __init__(self, font_size: int) -> None:
        super().__init__(id="font-size-selector")
        self.font_size = font_size

    def on_mount(self) -> None:
        self._update_display()

    def on_key(self, event: Key) -> None:
        if event.key not in ("left", "right"):
            return

        sizes = [size for _name, size in FONT_SIZE_CHOICES]
        current_index = sizes.index(self.font_size)
        direction = -1 if event.key == "left" else 1
        self.font_size = sizes[(current_index + direction) % len(sizes)]
        self._update_display()
        self.post_message(self.Changed(self.font_size))
        event.stop()

    def _update_display(self) -> None:
        options = []
        for name, size in FONT_SIZE_CHOICES:
            label = f"\\[ {name} {size} ]"
            if size == self.font_size:
                label = f"[reverse][b]{label}[/b][/reverse]"
            options.append(label)
        self.update("FONT SIZE   " + " ".join(options))


class MeshtasticPassApp(App[None]):
    """The first MeshtasticPass terminal UI shell."""

    TITLE = "MeshtasticPass"
    CSS = """
    Screen {
        background: #101010;
        color: #d8d8d8;
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

    #font-size-selector {
        height: 2;
        color: #d8d8d8;
    }

    #font-size-selector:focus {
        color: #f2f2f2;
    }

    #style-status {
        height: 2;
        color: #8fcf8f;
    }

    #connection-error, #send-error {
        height: auto;
        min-height: 1;
        color: #ff6b6b;
    }

    #chat-log {
        height: 1fr;
        scrollbar-size: 1 1;
    }

    .chat-entry {
        height: auto;
        margin-bottom: 1;
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
    """

    def __init__(self, radio: object, settings: AppSettings | None = None) -> None:
        super().__init__()
        self.radio = radio
        self.settings = settings or AppSettings.load()
        self.current_tab = "connection"
        self.chat_history: list[ChatEntry] = []
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
        self._update_tab_bar()
        self._show_connection(RadioState.CONNECTING)
        self.query_one(FontSizeSelector).focus()
        self._monitor.start()

    def on_unmount(self) -> None:
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
        self.query_one("#content", ContentSwitcher).current = tab_id
        self._update_tab_bar()
        if tab_id == "chat":
            self.query_one("#chat-input", Input).focus()
        elif tab_id == "connection":
            self.query_one(FontSizeSelector).focus()
        else:
            self.set_focus(None)

    @on(FontSizeSelector.Changed)
    def save_font_size(self, event: FontSizeSelector.Changed) -> None:
        status = self.query_one("#style-status", Static)
        try:
            self.settings.set_font_size(event.font_size)
            self.settings.save()
            self.settings.update_lxterminal_profile()
        except (OSError, ValueError) as error:
            status.update(f"FONT SIZE NOT SAVED — {error}")
        else:
            status.update("FONT SIZE SAVED — APPLIES ON NEXT LAUNCH")

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

    def _append_chat_widget(self, entry: ChatEntry) -> None:
        transcript = self.query_one("#chat-log", VerticalScroll)
        transcript.mount(
            Static(
                f"[b]{self._escape(entry.author)}[/b]\n  {self._escape(entry.text)}",
                classes="chat-entry",
            )
        )
        transcript.scroll_end(animate=False)

    def _show_connection(
        self,
        state: RadioState,
        info: RadioInfo | None = None,
        message: str = "",
    ) -> None:
        values = [
            ("RADIO", state.value),
            ("DEVICE", getattr(self.radio, "device_path", "unknown")),
        ]
        if info is not None:
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
        self.query_one("#connection-error", Static).update(message)

    def _show_send_error(self, message: str) -> None:
        self.query_one("#send-error", Static).update(message)

    def _update_tab_bar(self) -> None:
        labels = []
        for number, (tab_id, name) in enumerate(TAB_NAMES.items(), start=1):
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
