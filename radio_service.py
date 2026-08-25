"""Meshtastic radio access for the StreetPass app."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
import os
from pathlib import Path
from threading import Event
import time
from typing import Any, Callable, Iterator

from geo import GeoPosition, make_geo_position
from node_activity import count_active_other_nodes
from serial_devices import discover_serial_devices


class RadioState(Enum):
    """Connection states that callers can display without knowing SDK details."""

    CONNECTING = "CONNECTING"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    ERROR = "ERROR"


class RadioConnectionError(Exception):
    """Raised when the Meshtastic radio cannot be used."""

    def __init__(
        self,
        message: str,
        state: RadioState = RadioState.ERROR,
    ) -> None:
        super().__init__(message)
        self.state = state


class RadioSendError(Exception):
    """Raised when an application text message cannot be accepted for sending."""


class RadioIdentityError(Exception):
    """Raised when the connected radio identity cannot be updated."""


LONG_NAME_MAX_UTF8_BYTES = 39
SHORT_NAME_MAX_UTF8_BYTES = 4


def validate_long_name(long_name: str) -> str:
    """Apply the Meshtastic User.long_name nanopb string constraints."""
    if not isinstance(long_name, str):
        raise RadioIdentityError("Long Name must be text.")
    normalized = long_name.strip()
    if not normalized:
        raise RadioIdentityError("Long Name cannot be empty.")
    if len(normalized.encode("utf-8")) > LONG_NAME_MAX_UTF8_BYTES:
        raise RadioIdentityError(
            f"Long Name must be at most {LONG_NAME_MAX_UTF8_BYTES} UTF-8 bytes."
        )
    return normalized


def validate_short_name(short_name: str) -> str:
    """Apply the Meshtastic User.short_name nanopb string constraints.

    Meshtastic 2.7.11's protobuf declares ``max_size: 5`` for this field:
    four UTF-8 payload bytes plus nanopb's terminating null byte.
    """
    if not isinstance(short_name, str):
        raise RadioIdentityError("Short Name must be text.")
    normalized = short_name.strip()
    if not normalized:
        raise RadioIdentityError("Short Name cannot be empty.")
    if len(normalized.encode("utf-8")) > SHORT_NAME_MAX_UTF8_BYTES:
        raise RadioIdentityError(
            f"Short Name must be at most {SHORT_NAME_MAX_UTF8_BYTES} UTF-8 bytes."
        )
    return normalized


class DeliveryState(Enum):
    """Truthful application-level knowledge about one outgoing message."""

    SENDING = "SENDING"
    SENT = "SENT"
    HEARD = "HEARD"
    UNCONFIRMED = "UNCONFIRMED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class SendStatus:
    """An asynchronous ACK/NAK result with no SDK packet structures."""

    state: DeliveryState
    packet_id: int | None
    detail: str = ""


@dataclass(frozen=True)
class ChannelInfo:
    """One enabled application-facing Meshtastic channel."""

    index: int
    name: str


@dataclass(frozen=True)
class NodeMetadata:
    """Trustworthy application-level node details from the synced database."""

    node_id: str
    long_name: str | None = None
    short_name: str | None = None
    hops_away: int | None = None
    last_heard: float | None = None
    is_local: bool = False


@dataclass(frozen=True)
class RadioInfo:
    """Small, UI-friendly summary of the connected radio."""

    device_path: str
    node_id: str
    long_name: str
    short_name: str
    firmware_version: str
    known_nodes: int
    channels: tuple[ChannelInfo, ...] = ()


@dataclass(frozen=True)
class RadioEvent:
    """A connection state change emitted by RadioService."""

    state: RadioState
    info: RadioInfo | None = None
    message: str = ""


@dataclass(frozen=True)
class ReceivedMessage:
    """A decoded text message with no Meshtastic SDK-specific structures.

    ``origin_sent_at`` is reserved for a trustworthy protocol-provided origin
    time. Ordinary Meshtastic text packets do not have one, so RadioService
    leaves it unset. ``radio_rx_at`` carries the connected receiver's ``rxTime``.
    """

    sender_node_id: str
    sender_long_name: str | None
    sender_short_name: str | None
    channel_index: int | None
    text: str
    rssi: int | None
    snr: float | None
    packet_id: int | None
    origin_sent_at: float | None = None
    radio_rx_at: float | None = None
    local_position: GeoPosition | None = None
    sender_position: GeoPosition | None = None


@dataclass(frozen=True)
class SentMessage:
    """An application-level record accepted by the local send API."""

    text: str
    channel_index: int
    destination_node_id: str | None
    packet_id: int | None = None
    immediate_state: DeliveryState | None = None


def validate_send_request(
    text: str,
    channel_index: int,
    destination_node_id: str | None,
) -> SentMessage:
    """Validate and normalize values shared by real and simulated radios."""
    if not isinstance(text, str) or not text.strip():
        raise RadioSendError("Message text cannot be empty or whitespace only.")
    if (
        isinstance(channel_index, bool)
        or not isinstance(channel_index, int)
        or not 0 <= channel_index <= 7
    ):
        raise RadioSendError("Channel index must be an integer from 0 through 7.")
    if destination_node_id is not None:
        if (
            not isinstance(destination_node_id, str)
            or not destination_node_id.strip()
        ):
            raise RadioSendError("Destination node ID cannot be empty.")
        destination_node_id = destination_node_id.strip()

    return SentMessage(text, channel_index, destination_node_id)


class RadioService:
    """Owns the connection between the app and a Meshtastic radio."""

    def __init__(self, device_path: str = "/dev/ttyUSB0") -> None:
        self.device_path = device_path
        self._interface: Any | None = None
        self._connection_lost = Event()
        self._pub: Any | None = None
        self._message_handlers: list[Callable[[ReceivedMessage], None]] = []
        self._direct_observations: dict[str, float] = {}
        self._activity_local_node_id: str | None = None

    def connect(self) -> RadioInfo:
        """Connect, wait for the SDK's initial sync, and return local node info."""
        self._connection_lost.clear()
        self._check_device()

        try:
            self._interface = self._open_interface()
            info = self._read_radio_info()
            if (
                self._activity_local_node_id is not None
                and self._activity_local_node_id.lower() != info.node_id.lower()
            ):
                self._direct_observations.clear()
            self._activity_local_node_id = info.node_id
            return info
        except RadioConnectionError:
            self.close()
            raise
        except ImportError as error:
            self.close()
            raise RadioConnectionError(
                "The Meshtastic Python package is not installed. "
                "Activate .venv and run: pip install -r requirements.txt"
            ) from error
        except Exception as error:
            self.close()
            message = str(error).strip() or error.__class__.__name__
            raise RadioConnectionError(
                f"Could not connect to the radio on {self.device_path}: {message}"
            ) from error

    def available_device_paths(self) -> tuple[str, ...]:
        """Return serial devices currently reported by pyserial."""
        return discover_serial_devices()

    def set_device_path(self, device_path: str) -> None:
        """Change ports only after closing the current serial interface."""
        if not isinstance(device_path, str) or not device_path.strip():
            raise ValueError("USB device path cannot be empty.")
        self.close()
        self.device_path = device_path.strip()
        self._direct_observations.clear()
        self._activity_local_node_id = None

    def connection_events(
        self,
        retry_delay: float = 5.0,
        stop_event: Event | None = None,
        poll_interval: float = 0.25,
    ) -> Iterator[RadioEvent]:
        """Keep the radio connected and emit state changes until stopped."""
        if retry_delay < 0:
            raise ValueError("retry_delay cannot be negative")

        stopped = stop_event or Event()

        try:
            while not stopped.is_set():
                yield RadioEvent(RadioState.CONNECTING)

                try:
                    info = self.connect()
                except RadioConnectionError as error:
                    yield RadioEvent(error.state, message=str(error))
                else:
                    yield RadioEvent(RadioState.ONLINE, info=info)

                    while not stopped.is_set():
                        if self._connection_lost.wait(poll_interval):
                            break
                        if not self._device_exists():
                            break

                    if stopped.is_set():
                        break

                    self.close()
                    yield RadioEvent(
                        RadioState.OFFLINE,
                        message=f"Connection to {self.device_path} was lost.",
                    )

                if stopped.wait(retry_delay):
                    break
        finally:
            self.close()
            self._unsubscribe_from_events()

    def add_message_handler(
        self,
        handler: Callable[[ReceivedMessage], None],
    ) -> None:
        """Call handler for each valid received text message."""
        if handler not in self._message_handlers:
            self._message_handlers.append(handler)

    def active_node_count(self, now: float | None = None) -> int | None:
        """Return recently heard other nodes without transmitting anything."""
        interface = self._interface
        if interface is None:
            return None
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_number, dict):
            try:
                nodes = tuple(nodes_by_number.items())
            except RuntimeError:
                # The SDK may be updating the node database from its receive thread.
                # Direct observations remain trustworthy during that brief race.
                nodes = ()
        else:
            nodes = ()

        my_info = getattr(interface, "myInfo", None)
        local_number = getattr(my_info, "my_node_num", None)
        if isinstance(local_number, bool) or not isinstance(local_number, int):
            local_number = None
        local_record = (
            nodes_by_number.get(local_number, {})
            if isinstance(nodes_by_number, dict) and local_number is not None
            else {}
        )
        local_user = self._user_from_record(local_record)
        local_id = self._optional_string(local_user.get("id"))
        if local_id is None:
            local_id = self._activity_local_node_id
        try:
            observations = dict(self._direct_observations)
        except RuntimeError:
            observations = {}
        return count_active_other_nodes(
            nodes,
            local_node_number=local_number,
            local_node_id=local_id,
            now=time.time() if now is None else now,
            direct_observations=observations,
        )

    def get_node_metadata(self, node_id: str) -> NodeMetadata:
        """Read normalized node metadata without transmitting radio traffic."""
        normalized = node_id.strip() if isinstance(node_id, str) else ""
        if not normalized:
            return NodeMetadata("")
        try:
            node_number = int(normalized.removeprefix("!"), 16)
        except ValueError:
            node_number = None
        record = self._lookup_node_record(
            self._interface,
            node_number,
            normalized,
        )
        user = self._user_from_record(record)
        hops = self._optional_int(record.get("hopsAway"))
        if hops is not None and hops < 0:
            hops = None
        last_heard = self._valid_timestamp(record.get("lastHeard"))
        observed_at = self._direct_observations.get(normalized.lower())
        if self._valid_timestamp(observed_at) is not None:
            last_heard = max(last_heard or 0.0, float(observed_at))
        return NodeMetadata(
            node_id=normalized,
            long_name=self._optional_string(user.get("longName")),
            short_name=self._optional_string(user.get("shortName")),
            hops_away=hops,
            last_heard=last_heard,
            is_local=self._is_local_node(normalized, node_number),
        )

    def get_known_nodes(self) -> tuple[NodeMetadata, ...]:
        """Return the synced node database as normalized, read-only app values."""
        interface = self._interface
        if interface is None:
            return ()
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if not isinstance(nodes_by_number, dict):
            return ()
        try:
            records = tuple(nodes_by_number.items())
            observations = dict(self._direct_observations)
        except RuntimeError:
            return ()

        result: list[NodeMetadata] = []
        seen: set[str] = set()
        for raw_number, record in records:
            if not isinstance(record, dict):
                continue
            number = (
                raw_number
                if isinstance(raw_number, int) and not isinstance(raw_number, bool)
                else None
            )
            user = self._user_from_record(record)
            node_id = self._optional_string(user.get("id"))
            if node_id is None and number is not None:
                node_id = f"!{number:08x}"
            if node_id is None or node_id.lower() in seen:
                continue
            seen.add(node_id.lower())
            hops = self._optional_int(record.get("hopsAway"))
            if hops is not None and hops < 0:
                hops = None
            last_heard = self._valid_timestamp(record.get("lastHeard"))
            observed_at = self._valid_timestamp(observations.get(node_id.lower()))
            if observed_at is not None:
                last_heard = max(last_heard or 0.0, observed_at)
            result.append(
                NodeMetadata(
                    node_id=node_id,
                    long_name=self._optional_string(user.get("longName")),
                    short_name=self._optional_string(user.get("shortName")),
                    hops_away=hops,
                    last_heard=last_heard,
                    is_local=self._is_local_node(node_id, number),
                )
            )

        for node_id, raw_observed_at in observations.items():
            if node_id in seen or (
                self._activity_local_node_id
                and node_id == self._activity_local_node_id.lower()
            ):
                continue
            observed_at = self._valid_timestamp(raw_observed_at)
            if observed_at is not None:
                seen.add(node_id)
                result.append(NodeMetadata(node_id, last_heard=observed_at))

        local_id = self._activity_local_node_id
        if local_id and local_id.lower() not in seen:
            result.append(NodeMetadata(local_id, is_local=True))
        return tuple(result)

    def _is_local_node(self, node_id: str, node_number: int | None) -> bool:
        interface = self._interface
        my_info = getattr(interface, "myInfo", None)
        local_number = getattr(my_info, "my_node_num", None)
        return bool(
            (node_number is not None and node_number == local_number)
            or (
                self._activity_local_node_id
                and node_id.lower() == self._activity_local_node_id.lower()
            )
        )

    @staticmethod
    def _valid_timestamp(value: Any) -> float | None:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not isfinite(value)
            or value <= 0
        ):
            return None
        return float(value)

    def set_long_name(self, long_name: str) -> RadioInfo:
        """Update the local Meshtastic owner's advertised Long Name."""
        normalized = validate_long_name(long_name)
        interface = self._interface
        if interface is None:
            raise RadioIdentityError("The radio is not connected.")
        local_node = getattr(interface, "localNode", None)
        if local_node is None:
            raise RadioIdentityError("The connected radio identity is unavailable.")

        try:
            local_node.setOwner(long_name=normalized)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise RadioIdentityError(f"Could not save Long Name: {detail}") from error

        my_info = getattr(interface, "myInfo", None)
        local_number = getattr(my_info, "my_node_num", None)
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_number, dict) and local_number in nodes_by_number:
            record = nodes_by_number.get(local_number)
            if isinstance(record, dict):
                user = record.setdefault("user", {})
                if isinstance(user, dict):
                    user["longName"] = normalized
        return replace(self._read_radio_info(), long_name=normalized)

    def set_short_name(self, short_name: str) -> RadioInfo:
        """Update the local Meshtastic owner's advertised Short Name."""
        normalized = validate_short_name(short_name)
        interface = self._interface
        if interface is None:
            raise RadioIdentityError("The radio is not connected.")
        local_node = getattr(interface, "localNode", None)
        if local_node is None:
            raise RadioIdentityError("The connected radio identity is unavailable.")

        try:
            local_node.setOwner(short_name=normalized)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise RadioIdentityError(f"Could not save Short Name: {detail}") from error

        my_info = getattr(interface, "myInfo", None)
        local_number = getattr(my_info, "my_node_num", None)
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_number, dict) and local_number in nodes_by_number:
            record = nodes_by_number.get(local_number)
            if isinstance(record, dict):
                user = record.setdefault("user", {})
                if isinstance(user, dict):
                    user["shortName"] = normalized
        return replace(self._read_radio_info(), short_name=normalized)

    def remove_message_handler(
        self,
        handler: Callable[[ReceivedMessage], None],
    ) -> None:
        """Stop calling a previously registered message handler."""
        if handler in self._message_handlers:
            self._message_handlers.remove(handler)

    def send_text(
        self,
        text: str,
        channel_index: int = 0,
        destination_node_id: str | None = None,
        status_handler: Callable[[SendStatus], None] | None = None,
    ) -> SentMessage:
        """Submit text and optionally report matching routing ACK/NAK events.

        A successful return means the Python SDK accepted the packet for local
        radio submission. It does not mean another node received or read it.
        """
        message = validate_send_request(
            text,
            channel_index,
            destination_node_id,
        )
        if self._interface is None:
            raise RadioSendError("The radio is not connected.")

        sdk_arguments: dict[str, Any] = {
            "text": message.text,
            "channelIndex": message.channel_index,
        }
        if message.destination_node_id is not None:
            sdk_arguments["destinationId"] = message.destination_node_id

        if status_handler is not None:
            # Meshtastic 2.7.x only permits ordinary ACK packets through a
            # sendText response callback when its name is exactly onAckNak.
            def onAckNak(packet: dict[str, Any]) -> None:
                status = self._parse_send_response(packet)
                if status is not None:
                    status_handler(status)

            sdk_arguments.update(wantAck=True, onResponse=onAckNak)

        try:
            sdk_packet = self._interface.sendText(**sdk_arguments)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise RadioSendError(f"Could not send text message: {detail}") from error

        packet_id = self._optional_int(getattr(sdk_packet, "id", None))
        return SentMessage(
            message.text,
            message.channel_index,
            message.destination_node_id,
            packet_id=packet_id,
        )

    def _parse_send_response(self, packet: Any) -> SendStatus | None:
        """Convert a Meshtastic routing ACK/NAK into application state."""
        if not isinstance(packet, dict):
            return None
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict) or decoded.get("portnum") != "ROUTING_APP":
            return None
        routing = decoded.get("routing")
        if not isinstance(routing, dict):
            return None
        reason = self._normalize_routing_error(routing)
        if reason is None:
            return None
        packet_id = self._optional_int(decoded.get("requestId"))
        if reason == "NONE":
            return SendStatus(DeliveryState.HEARD, packet_id)
        return SendStatus(
            DeliveryState.FAILED,
            packet_id,
            detail=f"Meshtastic routing failure: {reason}",
        )

    @staticmethod
    def _normalize_routing_error(routing: dict[str, Any]) -> str | None:
        """Normalize the enum shape emitted by Meshtastic SDK 2.7.11.

        The SDK uses protobuf ``MessageToDict`` with its default enum behavior:
        known values are symbolic strings, while an unset optional NONE field
        is omitted. Unknown future enum numbers remain integers and must not be
        interpreted as a definite ACK or NAK.
        """
        if "errorReason" not in routing:
            return "NONE"

        reason = routing["errorReason"]
        if not isinstance(reason, str):
            return None

        try:
            from meshtastic.protobuf import mesh_pb2

            mesh_pb2.Routing.Error.Value(reason)
        except (AttributeError, ImportError, ValueError):
            return None
        return reason

    def close(self) -> None:
        """Close the serial connection if it is open."""
        if self._interface is not None:
            interface = self._interface
            self._interface = None
            try:
                interface.close()
            except Exception:
                # An unplugged serial device can fail while it is being closed.
                pass

    def _open_interface(self) -> Any:
        # Import here so a missing dependency becomes a friendly runtime error.
        from meshtastic.serial_interface import SerialInterface
        from pubsub import pub

        self._subscribe_to_events(pub)

        return SerialInterface(devPath=self.device_path)

    def _subscribe_to_events(self, pub: Any) -> None:
        if self._pub is not None:
            return

        pub.subscribe(
            self._on_connection_lost,
            "meshtastic.connection.lost",
        )
        try:
            pub.subscribe(
                self._on_text_received,
                "meshtastic.receive.text",
            )
        except Exception:
            pub.unsubscribe(
                self._on_connection_lost,
                "meshtastic.connection.lost",
            )
            raise

        self._pub = pub

    def _on_connection_lost(self, interface: Any, **_kwargs: Any) -> None:
        if interface is self._interface:
            self._connection_lost.set()

    def _on_text_received(
        self,
        packet: Any = None,
        interface: Any = None,
        **_kwargs: Any,
    ) -> None:
        if interface is not None and interface is not self._interface:
            return

        message = self._parse_text_packet(packet, self._interface)
        if message is None:
            return

        self._record_direct_observation(message.sender_node_id)

        for handler in tuple(self._message_handlers):
            try:
                handler(message)
            except Exception:
                # One consumer should not stop radio packet processing.
                pass

    def _record_direct_observation(
        self,
        node_id: str,
        observed_at: float | None = None,
    ) -> None:
        """Remember a valid directly received packet without transmitting."""
        if not isinstance(node_id, str):
            return
        normalized = node_id.strip().lower()
        if not normalized or normalized == "unknown":
            return
        timestamp = time.time() if observed_at is None else observed_at
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            return
        self._direct_observations[normalized] = float(timestamp)

    def _unsubscribe_from_events(self) -> None:
        if self._pub is not None:
            subscriptions = (
                (self._on_connection_lost, "meshtastic.connection.lost"),
                (self._on_text_received, "meshtastic.receive.text"),
            )
            for callback, topic in subscriptions:
                try:
                    self._pub.unsubscribe(callback, topic)
                except Exception:
                    pass
            self._pub = None

    def _check_device(self) -> None:
        path = Path(self.device_path)
        if not self._device_exists():
            raise RadioConnectionError(
                f"Serial device {self.device_path} was not found. "
                "Check the USB cable and the selected device path.",
                state=RadioState.OFFLINE,
            )

        if not os.access(path, os.R_OK | os.W_OK):
            raise RadioConnectionError(
                f"Permission denied for {self.device_path}. "
                "Add your current user to the serial-device access group "
                "(commonly dialout), then log out and back in."
            )

    def _device_exists(self) -> bool:
        return Path(self.device_path).exists()

    def _parse_text_packet(
        self,
        packet: Any,
        interface: Any,
    ) -> ReceivedMessage | None:
        if not isinstance(packet, dict):
            return None

        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            return None
        if decoded.get("portnum") not in ("TEXT_MESSAGE_APP", 1):
            return None

        text = self._decode_text(decoded)
        if text is None or text == "":
            return None

        sender_number = self._optional_int(packet.get("from"))
        sender_id = packet.get("fromId")
        if not isinstance(sender_id, str) or not sender_id:
            sender_id = (
                f"!{sender_number:08x}"
                if sender_number is not None
                else "unknown"
            )

        sender_record = self._lookup_node_record(
            interface,
            sender_number,
            sender_id,
        )
        user = self._user_from_record(sender_record)

        channel_value = packet.get("channel", 0)
        return ReceivedMessage(
            sender_node_id=sender_id,
            sender_long_name=self._optional_string(user.get("longName")),
            sender_short_name=self._optional_string(user.get("shortName")),
            channel_index=self._optional_int(channel_value),
            text=text,
            rssi=self._optional_int(packet.get("rxRssi")),
            snr=self._optional_float(packet.get("rxSnr")),
            packet_id=self._optional_int(packet.get("id")),
            # Ordinary TEXT_MESSAGE_APP packets do not carry a trustworthy
            # sender-origin timestamp. Meshtastic rxTime is receiver-side.
            origin_sent_at=None,
            radio_rx_at=self._optional_float(packet.get("rxTime")),
            local_position=self._local_position(interface),
            sender_position=self._position_from_record(sender_record),
        )

    @staticmethod
    def _decode_text(decoded: dict[str, Any]) -> str | None:
        value = decoded.get("text")
        if value is None:
            value = decoded.get("payload")

        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return None

    @staticmethod
    def _lookup_sender_user(
        interface: Any,
        sender_number: int | None,
        sender_id: str,
    ) -> dict[str, Any]:
        record = RadioService._lookup_node_record(
            interface,
            sender_number,
            sender_id,
        )
        return RadioService._user_from_record(record)

    @staticmethod
    def _lookup_node_record(
        interface: Any,
        node_number: int | None,
        node_id: str,
    ) -> dict[str, Any]:
        if interface is None:
            return {}

        record: Any = None
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_number, dict) and node_number is not None:
            record = nodes_by_number.get(node_number)

        if not isinstance(record, dict):
            nodes_by_id = getattr(interface, "nodes", None)
            if isinstance(nodes_by_id, dict):
                record = nodes_by_id.get(node_id)

        return record if isinstance(record, dict) else {}

    @staticmethod
    def _user_from_record(record: dict[str, Any]) -> dict[str, Any]:
        user = record.get("user")
        return user if isinstance(user, dict) else {}

    @staticmethod
    def _local_position(interface: Any) -> GeoPosition | None:
        if interface is None:
            return None
        my_info = getattr(interface, "myInfo", None)
        node_number = getattr(my_info, "my_node_num", None)
        record = RadioService._lookup_node_record(interface, node_number, "")
        return RadioService._position_from_record(record)

    @staticmethod
    def _position_from_record(record: Any) -> GeoPosition | None:
        """Extract the node-position shape used by Meshtastic SDK 2.7.11."""
        if not isinstance(record, dict):
            return None
        position = record.get("position")
        if not isinstance(position, dict):
            return None

        latitude = position.get("latitude")
        longitude = position.get("longitude")
        if latitude is None and "latitudeI" in position:
            value = RadioService._optional_float(position.get("latitudeI"))
            latitude = value * 1e-7 if value is not None else None
        if longitude is None and "longitudeI" in position:
            value = RadioService._optional_float(position.get("longitudeI"))
            longitude = value * 1e-7 if value is not None else None

        updated_at = RadioService._optional_position_time(position)
        return make_geo_position(latitude, longitude, updated_at)

    @staticmethod
    def _optional_position_time(position: dict[str, Any]) -> float | None:
        """Prefer the GPS solution time, then the SDK's received-position time."""
        for field in ("timestamp", "time"):
            value = RadioService._optional_float(position.get(field))
            if value is not None and value > 0:
                return value
        return None

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def _read_radio_info(self) -> RadioInfo:
        if self._interface is None:
            raise RadioConnectionError("The radio is not connected.")

        my_info = self._interface.myInfo
        local_node = self._interface.localNode
        if my_info is None or local_node is None:
            raise RadioConnectionError(
                "The serial port opened, but the initial Meshtastic sync did not complete."
            )

        node_number = getattr(my_info, "my_node_num", None)
        local_record = self._interface.nodesByNum.get(node_number, {})
        user = local_record.get("user", {})
        metadata = self._interface.metadata

        node_id = user.get("id") or (
            f"!{node_number:08x}" if isinstance(node_number, int) else "unknown"
        )

        return RadioInfo(
            device_path=self.device_path,
            node_id=node_id,
            long_name=user.get("longName", "unknown"),
            short_name=user.get("shortName", "unknown"),
            firmware_version=getattr(metadata, "firmware_version", "unknown"),
            known_nodes=len(self._interface.nodesByNum),
            channels=self._read_channel_info(local_node),
        )

    @staticmethod
    def _read_channel_info(local_node: Any) -> tuple[ChannelInfo, ...]:
        """Convert enabled SDK channel protobufs into stable app values."""
        channels = getattr(local_node, "channels", None)
        if not channels:
            return ()
        try:
            from meshtastic.protobuf import channel_pb2

            disabled_role = channel_pb2.Channel.Role.DISABLED
        except (ImportError, AttributeError):
            disabled_role = 0

        result: list[ChannelInfo] = []
        seen_indexes: set[int] = set()
        for fallback_index, channel in enumerate(channels):
            role = getattr(channel, "role", disabled_role)
            if role == disabled_role or role == "DISABLED":
                continue
            raw_index = getattr(channel, "index", fallback_index)
            index = RadioService._optional_int(raw_index)
            if index is None or not 0 <= index <= 7 or index in seen_indexes:
                continue
            settings = getattr(channel, "settings", None)
            raw_name = getattr(settings, "name", "")
            name = raw_name.strip() if isinstance(raw_name, str) else ""
            if not name and index == 0:
                name = RadioService._primary_channel_default_name(local_node)
            result.append(ChannelInfo(index, name or f"Channel {index + 1}"))
            seen_indexes.add(index)
        return tuple(sorted(result, key=lambda channel: channel.index))

    @staticmethod
    def _primary_channel_default_name(local_node: Any) -> str:
        """Derive the unnamed primary channel label from the radio preset."""
        try:
            from meshtastic.protobuf import config_pb2

            lora = local_node.localConfig.lora
            if not lora.use_preset:
                return ""
            enum_name = config_pb2.Config.LoRaConfig.ModemPreset.Name(
                lora.modem_preset
            )
        except (ImportError, AttributeError, KeyError, TypeError, ValueError):
            return ""
        return "".join(part.title() for part in enum_name.split("_"))
