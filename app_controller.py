"""Non-visual state and radio monitoring for the MeshtasticPass TUI."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from time import monotonic, time
from typing import Any, Callable

from chat_store import StoredMessage
from geo import distance_between
from message_time import make_age_reference
from radio_service import (
    DeliveryState,
    RadioEvent,
    ReceivedMessage,
    RadioService,
)
from simulated_radio_service import SimulatedRadioService


@dataclass
class ChatEntry:
    """One application-level line item ready for the chat transcript."""

    author: str
    text: str
    radio_rx_at: float | None
    local_sent_at: float | None
    received_at: float
    age_reference: float
    distance_miles: float | None = None
    outgoing: bool = False
    unread: bool = False
    is_new: bool = False
    message_id: int | None = None
    packet_id: int | None = None
    node_id: str | None = None
    sender_name: str | None = None
    sender_short_name: str | None = None
    channel_index: int = 0
    delivery_state: DeliveryState | None = None
    active_attempt_id: int | None = None
    confirmation_deadline: float | None = None
    send_generation: int = 0


def received_chat_entry(
    message: ReceivedMessage,
    received_at: float | None = None,
    monotonic_now: float | None = None,
    unread: bool = False,
    is_new: bool = False,
) -> ChatEntry:
    """Convert an application message to display state without SDK details."""
    local_received_at = time() if received_at is None else received_at
    local_monotonic = monotonic() if monotonic_now is None else monotonic_now
    valid_radio_rx_at, age_reference = make_age_reference(
        message.radio_rx_at,
        local_received_at,
        local_monotonic,
    )
    author = (
        message.sender_long_name
        or message.sender_short_name
        or message.sender_node_id
    )
    return ChatEntry(
        author=author,
        text=message.text,
        radio_rx_at=valid_radio_rx_at,
        local_sent_at=None,
        received_at=local_received_at,
        age_reference=age_reference,
        distance_miles=distance_between(
            message.local_position,
            message.sender_position,
        ),
        unread=unread,
        is_new=is_new,
        packet_id=message.packet_id,
        node_id=message.sender_node_id,
        sender_name=message.sender_long_name,
        sender_short_name=message.sender_short_name,
        channel_index=message.channel_index or 0,
    )


def outgoing_chat_entry(
    text: str,
    received_at: float | None = None,
    monotonic_now: float | None = None,
    channel_index: int = 0,
    delivery_state: DeliveryState = DeliveryState.SENDING,
) -> ChatEntry:
    """Create a local outgoing transcript entry before SDK completion."""
    local_received_at = time() if received_at is None else received_at
    local_monotonic = monotonic() if monotonic_now is None else monotonic_now
    return ChatEntry(
        author="YOU",
        text=text,
        radio_rx_at=None,
        local_sent_at=local_received_at,
        received_at=local_received_at,
        age_reference=local_monotonic,
        outgoing=True,
        channel_index=channel_index,
        delivery_state=delivery_state,
    )


def stored_chat_entry(
    stored: StoredMessage,
    wall_now: float | None = None,
    monotonic_now: float | None = None,
) -> ChatEntry:
    """Rebuild process-local age state from persisted wall-clock fields."""
    current_wall = time() if wall_now is None else wall_now
    current_monotonic = monotonic() if monotonic_now is None else monotonic_now
    outgoing = stored.direction == "outgoing"
    if outgoing:
        age_time = stored.local_sent_at or stored.received_at
    else:
        age_time = stored.radio_rx_at or stored.received_at
    initial_age = max(0.0, current_wall - age_time)
    state = None
    if outgoing and stored.delivery_state:
        try:
            state = DeliveryState(stored.delivery_state)
        except ValueError:
            state = DeliveryState.FAILED
    return ChatEntry(
        author=(
            "YOU"
            if outgoing
            else stored.sender_name
            or stored.sender_short_name
            or stored.node_id
            or "unknown"
        ),
        text=stored.text,
        radio_rx_at=stored.radio_rx_at,
        local_sent_at=stored.local_sent_at,
        received_at=stored.received_at,
        age_reference=current_monotonic - initial_age,
        outgoing=outgoing,
        unread=False,
        is_new=False,
        message_id=stored.id,
        packet_id=stored.packet_id,
        node_id=stored.node_id,
        sender_name=stored.sender_name,
        sender_short_name=stored.sender_short_name,
        channel_index=stored.channel_index,
        delivery_state=state,
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
