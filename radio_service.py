"""Meshtastic radio access for the StreetPass app."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any


class RadioConnectionError(Exception):
    """Raised when the Meshtastic radio cannot be used."""


@dataclass(frozen=True)
class RadioInfo:
    """Small, UI-friendly summary of the connected radio."""

    device_path: str
    node_id: str
    long_name: str
    short_name: str
    firmware_version: str
    known_nodes: int


class RadioService:
    """Owns the connection between the app and a Meshtastic radio."""

    def __init__(self, device_path: str = "/dev/ttyUSB0") -> None:
        self.device_path = device_path
        self._interface: Any | None = None

    def connect(self) -> RadioInfo:
        """Connect, wait for the SDK's initial sync, and return local node info."""
        self._check_device()

        try:
            # Import here so a missing dependency becomes a friendly runtime error.
            from meshtastic.serial_interface import SerialInterface

            self._interface = SerialInterface(devPath=self.device_path)
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

    def close(self) -> None:
        """Close the serial connection if it is open."""
        if self._interface is not None:
            try:
                self._interface.close()
            finally:
                self._interface = None

    def _check_device(self) -> None:
        path = Path(self.device_path)
        if not path.exists():
            raise RadioConnectionError(
                f"Serial device {self.device_path} was not found. "
                "Check the USB cable and run: ls -l /dev/ttyUSB0"
            )

        if not os.access(path, os.R_OK | os.W_OK):
            raise RadioConnectionError(
                f"Permission denied for {self.device_path}. "
                "Add user 'mt' to the dialout group, then log out and back in: "
                "sudo usermod -aG dialout mt"
            )

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
