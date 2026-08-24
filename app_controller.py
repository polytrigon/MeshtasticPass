"""Non-visual state and radio monitoring for the MeshtasticPass TUI."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic
from typing import Any, Callable

from radio_service import RadioEvent, ReceivedMessage, RadioService
from simulated_radio_service import SimulatedRadioService


@dataclass
class ChatEntry:
    """One in-memory line item ready for the chat transcript."""

    author: str
    text: str
    accepted_at: float
    outgoing: bool = False
    unread: bool = False
    new_highlight_until: float | None = None


def received_chat_entry(
    message: ReceivedMessage,
    accepted_at: float | None = None,
    unread: bool = False,
    new_highlight_until: float | None = None,
) -> ChatEntry:
    """Convert an application message to display state without SDK details."""
    author = (
        message.sender_long_name
        or message.sender_short_name
        or message.sender_node_id
    )
    return ChatEntry(
        author=author,
        text=message.text,
        accepted_at=monotonic() if accepted_at is None else accepted_at,
        unread=unread,
        new_highlight_until=new_highlight_until,
    )


def outgoing_chat_entry(text: str, accepted_at: float | None = None) -> ChatEntry:
    """Create a local-only transcript entry for an accepted send."""
    return ChatEntry(
        author="YOU",
        text=text,
        accepted_at=monotonic() if accepted_at is None else accepted_at,
        outgoing=True,
    )


def create_radio_service(
    simulate: bool,
    device_path: str = "/dev/ttyUSB0",
) -> RadioService | SimulatedRadioService:
    """Select the real or deterministic radio behind the same app boundary."""
    if simulate:
        return SimulatedRadioService()
    return RadioService(device_path=device_path)


class RadioMonitor:
    """Run blocking radio events away from the Textual UI thread."""

    def __init__(
        self,
        radio: Any,
        on_event: Callable[[RadioEvent], None],
        on_message: Callable[[ReceivedMessage], None],
    ) -> None:
        self.radio = radio
        self.on_event = on_event
        self.on_message = on_message
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._stopped = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Register receive handling and start one monitoring thread."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._stopped = False
        self.radio.add_message_handler(self.on_message)
        self._thread = Thread(
            target=self._run,
            name="meshtastic-radio-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self, join_timeout: float = 2.0) -> None:
        """Stop monitoring and close the radio. Safe to call repeatedly."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        self.radio.remove_message_handler(self.on_message)
        self.radio.close()

        if self._thread is not None:
            self._thread.join(timeout=join_timeout)

    def _run(self) -> None:
        for event in self.radio.connection_events(stop_event=self._stop_event):
            if self._stop_event.is_set():
                break
            self.on_event(event)
