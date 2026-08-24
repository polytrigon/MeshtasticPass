"""Serial-device discovery kept outside the terminal UI."""

from __future__ import annotations


def discover_serial_devices() -> tuple[str, ...]:
    """Return currently available serial device paths in stable order."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return ()

    try:
        ports = list_ports.comports()
    except Exception:
        return ()
    devices = {
        port.device
        for port in ports
        if isinstance(getattr(port, "device", None), str) and port.device
    }
    return tuple(sorted(devices))
