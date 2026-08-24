"""Deterministic Meshtastic radio simulation for development without hardware."""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event
import time
from typing import Callable, Iterator

from radio_service import RadioEvent, RadioInfo, RadioState, ReceivedMessage


@dataclass(frozen=True)
class SimulatedNode:
    """Stable fake node information for repeatable development and tests."""

    node_id: str
    long_name: str
    short_name: str


SIMULATED_NODES = (
    SimulatedNode("!a11ce001", "Alice Trail", "ALCE"),
    SimulatedNode("!b0b00002", "Bob Basecamp", "BOB"),
    SimulatedNode("!cafe0003", "Cafe Relay", "CAFE"),
)


SIMULATED_MESSAGES = (
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[0].node_id,
        sender_long_name=SIMULATED_NODES[0].long_name,
        sender_short_name=SIMULATED_NODES[0].short_name,
        channel_index=0,
        text="Hello from the trail!",
        rssi=-87,
        snr=6.5,
        packet_id=350000001,
    ),
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[1].node_id,
        sender_long_name=SIMULATED_NODES[1].long_name,
        sender_short_name=SIMULATED_NODES[1].short_name,
        channel_index=0,
        text="Basecamp checking in.",
        rssi=-73,
        snr=8.25,
        packet_id=350000002,
    ),
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[2].node_id,
        sender_long_name=SIMULATED_NODES[2].long_name,
        sender_short_name=SIMULATED_NODES[2].short_name,
        channel_index=1,
        text="Relay link is online.",
        rssi=-101,
        snr=2.0,
        packet_id=350000003,
    ),
)


class SimulatedRadioService:
    """Behaves like RadioService while producing deterministic fake events."""

    def __init__(
        self,
        connect_delay: float = 0.25,
        message_interval: float = 0.75,
        scripted_messages: tuple[ReceivedMessage, ...] = SIMULATED_MESSAGES,
    ) -> None:
        self.device_path = "simulated://meshtastic"
        self.info = RadioInfo(
            device_path=self.device_path,
            node_id="!51a00001",
            long_name="MeshtasticPass Simulator",
            short_name="SIM",
            firmware_version="sim-1.0.0",
            known_nodes=len(SIMULATED_NODES) + 1,
        )
        self.connect_delay = connect_delay
        self.message_interval = message_interval
        self.scripted_messages = scripted_messages
        self._message_handlers: list[Callable[[ReceivedMessage], None]] = []
        self._state_events: Queue[RadioEvent] = Queue()
        self._stop_event = Event()
        self._online = False
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    def connect(self) -> RadioInfo:
        """Bring the simulated radio online and return its local node info."""
        self._stop_event.clear()
        self._closed = False
        self._online = True
        return self.info

    def connection_events(
        self,
        retry_delay: float = 5.0,
        stop_event: Event | None = None,
        poll_interval: float = 0.25,
    ) -> Iterator[RadioEvent]:
        """Connect, emit scripted messages, and wait for simulated events."""
        if retry_delay < 0:
            raise ValueError("retry_delay cannot be negative")
        if self.connect_delay < 0 or self.message_interval < 0:
            raise ValueError("simulation delays cannot be negative")

        stopped = stop_event or Event()
        self._closed = False

        try:
            if self._is_stopped(stopped):
                return

            yield RadioEvent(RadioState.CONNECTING)
            if self._wait(self.connect_delay, stopped):
                return

            yield RadioEvent(RadioState.ONLINE, info=self.connect())

            for message in self.scripted_messages:
                if self._wait(self.message_interval, stopped):
                    return
                self.emit_message(message)

            while not self._is_stopped(stopped):
                try:
                    event = self._state_events.get(timeout=poll_interval)
                except Empty:
                    continue

                self._apply_state(event.state)
                yield event
        finally:
            self.close()

    def add_message_handler(
        self,
        handler: Callable[[ReceivedMessage], None],
    ) -> None:
        if handler not in self._message_handlers:
            self._message_handlers.append(handler)

    def remove_message_handler(
        self,
        handler: Callable[[ReceivedMessage], None],
    ) -> None:
        if handler in self._message_handlers:
            self._message_handlers.remove(handler)

    def emit_message(self, message: ReceivedMessage) -> None:
        """Deliver one fake message to every registered consumer."""
        if not self._online or self._stop_event.is_set():
            return

        for handler in tuple(self._message_handlers):
            try:
                handler(message)
            except Exception:
                pass

    def simulate_disconnect(self) -> None:
        self._state_events.put(
            RadioEvent(
                RadioState.OFFLINE,
                message="Simulated radio connection was lost.",
            )
        )

    def simulate_error(self, message: str = "Simulated radio error.") -> None:
        self._state_events.put(RadioEvent(RadioState.ERROR, message=message))

    def simulate_reconnect(self) -> None:
        self._state_events.put(RadioEvent(RadioState.CONNECTING))
        self._state_events.put(RadioEvent(RadioState.ONLINE, info=self.info))

    def close(self) -> None:
        """Stop the simulator. Safe to call more than once."""
        self._online = False
        self._closed = True
        self._stop_event.set()

    def _apply_state(self, state: RadioState) -> None:
        if state is RadioState.ONLINE:
            self._online = True
        elif state in (RadioState.OFFLINE, RadioState.ERROR):
            self._online = False

    def _is_stopped(self, external_stop: Event) -> bool:
        return self._stop_event.is_set() or external_stop.is_set()

    def _wait(self, seconds: float, external_stop: Event) -> bool:
        deadline = time.monotonic() + seconds
        while not self._is_stopped(external_stop):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._stop_event.wait(min(0.05, remaining))
        return True
