"""Deterministic Meshtastic radio simulation for development without hardware."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from queue import Empty, Queue
from threading import Event
import time
from typing import Callable, Iterator

from geo import GeoPosition
from node_activity import count_active_other_nodes
from radio_service import (
    ChannelInfo,
    RadioEvent,
    DeliveryState,
    RadioInfo,
    RadioIdentityError,
    RadioSendError,
    RadioState,
    NodeMetadata,
    ReceivedMessage,
    SentMessage,
    SendStatus,
    validate_send_request,
    validate_long_name,
)


SIMULATED_DEVICE_PATHS = ("/dev/ttyUSB0", "/dev/ttyUSB1")
SIMULATED_CHANNELS = (
    ChannelInfo(0, "LongFast"),
    ChannelInfo(1, "Hiking"),
)


class SimulatedSendOutcome(Enum):
    """Explicit deterministic result for one simulated send attempt."""

    SENT = "sent"
    HEARD = "heard"
    UNCONFIRMED = "unconfirmed"
    FAILED = "failed"


@dataclass(frozen=True)
class SimulatedNode:
    """Stable fake node information for repeatable development and tests."""

    node_id: str
    long_name: str | None
    short_name: str | None
    position: GeoPosition | None
    last_heard_age_seconds: object | None
    hops_away: int | None = None


SIMULATED_LOCAL_POSITION = GeoPosition(40.7128, -74.0060, 1_700_000_000.0)

SIMULATED_NODES = (
    SimulatedNode(
        "!a11ce001",
        "Alice Trail",
        "ALCE",
        GeoPosition(40.7736, -73.9566, 1_700_000_100.0),
        30.0,
        1,
    ),
    SimulatedNode("!b0b00002", "Bob Basecamp", "BOB", None, 299.0, 5),
    SimulatedNode(
        "!cafe0003",
        "Cafe Relay",
        "CAFE",
        GeoPosition(40.6501, -73.9496, 1_700_000_200.0),
        400.0,
        None,
    ),
    SimulatedNode("!bad00004", "Malformed Clock", "BAD", None, "recent"),
    SimulatedNode("!none0005", "Missing Clock", "NONE", None, None),
    SimulatedNode("!10a60006", None, "NOLN", None, 600.0, 2),
    SimulatedNode("!5a070007", "No Short Name", None, None, 600.0, None),
)

_SIMULATED_REFERENCE_TIME = time.time()


SIMULATED_MESSAGES = (
    # Channel 0 deliberately delivers a newer receiver timestamp first, then
    # an older one. This makes chronological insertion and its notice visible.
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[0].node_id,
        sender_long_name=SIMULATED_NODES[0].long_name,
        sender_short_name=SIMULATED_NODES[0].short_name,
        channel_index=0,
        text="Hello from the trail!",
        rssi=-87,
        snr=6.5,
        packet_id=350000001,
        radio_rx_at=_SIMULATED_REFERENCE_TIME - 8,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[0].position,
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
        radio_rx_at=_SIMULATED_REFERENCE_TIME - 27 * 60,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[1].position,
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
        radio_rx_at=_SIMULATED_REFERENCE_TIME - (2 * 60 + 12) * 60,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[2].position,
    ),
    # Two more channel-0 arrivals are older than the first message but newer
    # than Bob. Together these provide three deterministic older NEW entries
    # for exercising Left Arrow catch-up in simulation.
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[2].node_id,
        sender_long_name=SIMULATED_NODES[2].long_name,
        sender_short_name=SIMULATED_NODES[2].short_name,
        channel_index=0,
        text="Cafe relay heard the primary channel.",
        rssi=-96,
        snr=3.5,
        packet_id=350000004,
        radio_rx_at=_SIMULATED_REFERENCE_TIME - 20 * 60,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[2].position,
    ),
    ReceivedMessage(
        sender_node_id=SIMULATED_NODES[3].node_id,
        sender_long_name=SIMULATED_NODES[3].long_name,
        sender_short_name=SIMULATED_NODES[3].short_name,
        channel_index=0,
        text="Clock check from the ridge.",
        rssi=-91,
        snr=4.0,
        packet_id=350000005,
        radio_rx_at=_SIMULATED_REFERENCE_TIME - 15 * 60,
        local_position=SIMULATED_LOCAL_POSITION,
        sender_position=SIMULATED_NODES[3].position,
    ),
)


class SimulatedRadioService:
    """Behaves like RadioService while producing deterministic fake events."""

    def __init__(
        self,
        device_path: str = SIMULATED_DEVICE_PATHS[0],
        connect_delay: float = 0.25,
        message_interval: float = 0.75,
        scripted_messages: tuple[ReceivedMessage, ...] = SIMULATED_MESSAGES,
        send_outcomes: tuple[SimulatedSendOutcome, ...] = (),
    ) -> None:
        if device_path not in SIMULATED_DEVICE_PATHS:
            device_path = SIMULATED_DEVICE_PATHS[0]
        self.device_path = device_path
        self.info = RadioInfo(
            device_path=self.device_path,
            node_id="!51a00001",
            long_name="Simulated Node",
            short_name="SIM",
            firmware_version="sim-1.0.0",
            known_nodes=len(SIMULATED_NODES) + 1,
            channels=SIMULATED_CHANNELS,
        )
        self.connect_delay = connect_delay
        self.message_interval = message_interval
        self.scripted_messages = scripted_messages
        self.send_outcomes = send_outcomes
        self._message_handlers: list[Callable[[ReceivedMessage], None]] = []
        self._state_events: Queue[RadioEvent] = Queue()
        self._stop_event = Event()
        self._online = False
        self._closed = False
        self._sent_messages: list[SentMessage] = []
        self._send_count = 0
        self._activity_reference_time: float | None = None
        self._direct_observations: dict[str, float] = {}

    def available_device_paths(self) -> tuple[str, ...]:
        """Return fake ports without asking the host operating system."""
        return SIMULATED_DEVICE_PATHS

    def set_device_path(self, device_path: str) -> None:
        """Switch deterministic fake ports without touching real hardware."""
        if device_path not in SIMULATED_DEVICE_PATHS:
            raise ValueError("Unknown simulated USB device.")
        self.close()
        self.device_path = device_path
        self.info = RadioInfo(
            device_path=device_path,
            node_id=self.info.node_id,
            long_name=self.info.long_name,
            short_name=self.info.short_name,
            firmware_version=self.info.firmware_version,
            known_nodes=self.info.known_nodes,
            channels=self.info.channels,
        )
        self._direct_observations.clear()

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def sent_messages(self) -> tuple[SentMessage, ...]:
        """Return an immutable snapshot of deterministic send history."""
        return tuple(self._sent_messages)

    def connect(self) -> RadioInfo:
        """Bring the simulated radio online and return its local node info."""
        self._stop_event.clear()
        self._closed = False
        self._online = True
        self._activity_reference_time = time.time()
        return self.info

    def active_node_count(self, now: float | None = None) -> int | None:
        """Return deterministic passive node activity while connected."""
        if not self._online or self._activity_reference_time is None:
            return None
        current_time = time.time() if now is None else now
        local_number = int(self.info.node_id[1:], 16)
        nodes: list[tuple[int, dict[str, object]]] = [
            (
                local_number,
                {
                    "user": {"id": self.info.node_id},
                    "lastHeard": self._activity_reference_time,
                },
            )
        ]
        for index, node in enumerate(SIMULATED_NODES, start=1):
            record: dict[str, object] = {"user": {"id": node.node_id}}
            if node.last_heard_age_seconds is not None:
                age = node.last_heard_age_seconds
                record["lastHeard"] = (
                    self._activity_reference_time - age
                    if isinstance(age, (int, float))
                    else age
                )
            nodes.append((index, record))
        return count_active_other_nodes(
            nodes,
            local_node_number=local_number,
            local_node_id=self.info.node_id,
            now=current_time,
            direct_observations=self._direct_observations,
        )

    def get_node_metadata(self, node_id: str) -> NodeMetadata:
        """Return deterministic node details without radio side effects."""
        normalized = node_id.strip().lower()
        for node in SIMULATED_NODES:
            if node.node_id.lower() == normalized:
                return NodeMetadata(
                    node.node_id,
                    node.long_name,
                    node.short_name,
                    node.hops_away,
                )
        return NodeMetadata(node_id.strip())

    def set_long_name(self, long_name: str) -> RadioInfo:
        """Update the deterministic local identity while simulated online."""
        normalized = validate_long_name(long_name)
        if not self._online or self._stop_event.is_set():
            raise RadioIdentityError("The simulated radio is not connected.")
        self.info = replace(self.info, long_name=normalized)
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
        self._stop_event.clear()
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

    def send_text(
        self,
        text: str,
        channel_index: int = 0,
        destination_node_id: str | None = None,
        status_handler: Callable[[SendStatus], None] | None = None,
    ) -> SentMessage:
        """Record a send using the next explicitly scripted outcome."""
        message = validate_send_request(
            text,
            channel_index,
            destination_node_id,
        )
        if not self._online or self._stop_event.is_set():
            raise RadioSendError("The simulated radio is not connected.")

        outcome = (
            self.send_outcomes[self._send_count]
            if self._send_count < len(self.send_outcomes)
            else SimulatedSendOutcome.SENT
        )
        self._send_count += 1
        if outcome is SimulatedSendOutcome.FAILED:
            raise RadioSendError("Simulated definite send failure.")

        packet_id = 450000000 + self._send_count
        immediate_state = {
            SimulatedSendOutcome.SENT: DeliveryState.SENT,
            SimulatedSendOutcome.HEARD: DeliveryState.HEARD,
            SimulatedSendOutcome.UNCONFIRMED: DeliveryState.UNCONFIRMED,
        }[outcome]
        sent = SentMessage(
            message.text,
            message.channel_index,
            message.destination_node_id,
            packet_id=packet_id,
            immediate_state=immediate_state,
        )
        self._sent_messages.append(sent)
        return sent

    def emit_message(self, message: ReceivedMessage) -> None:
        """Deliver one fake message to every registered consumer."""
        if not self._online or self._stop_event.is_set():
            return

        node_id = message.sender_node_id.strip().lower()
        if node_id and node_id != "unknown":
            self._direct_observations[node_id] = time.time()

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
