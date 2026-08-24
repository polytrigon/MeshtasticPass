"""Meshtastic radio access for the StreetPass app."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterator


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


@dataclass(frozen=True)
class RadioInfo:
    """Small, UI-friendly summary of the connected radio."""

    device_path: str
    node_id: str
    long_name: str
    short_name: str
    firmware_version: str
    known_nodes: int


@dataclass(frozen=True)
class RadioEvent:
    """A connection state change emitted by RadioService."""

    state: RadioState
    info: RadioInfo | None = None
    message: str = ""


@dataclass(frozen=True)
class ReceivedMessage:
    """A decoded text message with no Meshtastic SDK-specific structures.

    ``radio_rx_at`` carries Meshtastic's receiver-stamped ``rxTime`` when present.
    Ordinary text packets do not carry a true sender-origin timestamp.
    """

    sender_node_id: str
    sender_long_name: str | None
    sender_short_name: str | None
    channel_index: int | None
    text: str
    rssi: int | None
    snr: float | None
    packet_id: int | None
    radio_rx_at: float | None = None


@dataclass(frozen=True)
class SentMessage:
    """An application-level record of a text message accepted for sending."""

    text: str
    channel_index: int
    destination_node_id: str | None


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

    def connect(self) -> RadioInfo:
        """Connect, wait for the SDK's initial sync, and return local node info."""
        self._connection_lost.clear()
        self._check_device()

        try:
            self._interface = self._open_interface()
            return self._read_radio_info()
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
    ) -> SentMessage:
        """Send text through Meshtastic without exposing SDK details to callers."""
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

        try:
            self._interface.sendText(**sdk_arguments)
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise RadioSendError(f"Could not send text message: {detail}") from error

        return message

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

        for handler in tuple(self._message_handlers):
            try:
                handler(message)
            except Exception:
                # One consumer should not stop radio packet processing.
                pass

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
                "Check the USB cable and run: ls -l /dev/ttyUSB0",
                state=RadioState.OFFLINE,
            )

        if not os.access(path, os.R_OK | os.W_OK):
            raise RadioConnectionError(
                f"Permission denied for {self.device_path}. "
                "Add user 'mt' to the dialout group, then log out and back in: "
                "sudo usermod -aG dialout mt"
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

        user = self._lookup_sender_user(interface, sender_number, sender_id)

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
            radio_rx_at=self._optional_float(packet.get("rxTime")),
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
        if interface is None:
            return {}

        record: Any = None
        nodes_by_number = getattr(interface, "nodesByNum", None)
        if isinstance(nodes_by_number, dict) and sender_number is not None:
            record = nodes_by_number.get(sender_number)

        if not isinstance(record, dict):
            nodes_by_id = getattr(interface, "nodes", None)
            if isinstance(nodes_by_id, dict):
                record = nodes_by_id.get(sender_id)

        if not isinstance(record, dict):
            return {}

        user = record.get("user")
        return user if isinstance(user, dict) else {}

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
        )
