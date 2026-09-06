"""Connection-target discovery kept outside the terminal UI.

A "connection target" is either a serial device path (/dev/ttyUSB0) or a
TCP address written as tcp://host[:port]. Both are things the Meshtastic
Python SDK can open; which one a given radio needs is a property of how
it is wired, not of this application.

The TCP form exists for Linux-native radios -- notably the HackerGadgets
uConsole AIO boards, whose SX1262 hangs off the Pi's SPI1 with GPIO
IRQ/Busy/Reset lines and is driven by meshtasticd running on the host.
There is no USB serial port to open on those; meshtasticd owns the radio
and republishes the same Meshtastic API on TCP 4403, which is how its own
web client talks to it.
"""

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


MESHTASTICD_TCP_PORT = 4403
MESHTASTICD_LOCAL_TARGET = f"tcp://localhost:{MESHTASTICD_TCP_PORT}"
_MESHTASTICD_PROBE_TIMEOUT_SECONDS = 0.15


def meshtasticd_is_listening(
    host: str = "localhost",
    port: int = MESHTASTICD_TCP_PORT,
    *,
    timeout: float = _MESHTASTICD_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """Whether something is accepting TCP connections on the meshtasticd port.

    Opens and immediately closes a socket: no Meshtastic protocol is
    spoken, nothing is written, and no LoRa traffic is generated -- the
    same passivity rule the rest of the connection page follows. The
    timeout is deliberately short because this runs while the user is
    looking at the CONNECTION list; a host that is not listening refuses
    at once, and one that black-holes the port must not stall the UI.

    A true answer means "a socket answered here", not "meshtasticd is
    healthy". Proving the latter means speaking the protocol, which is
    what actually connecting does -- so this only decides whether to
    OFFER the target, never whether a connection will succeed.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_connection_targets() -> tuple[str, ...]:
    """Serial devices, plus the local meshtasticd target when one answers."""
    targets = list(discover_serial_devices())
    if meshtasticd_is_listening():
        targets.append(MESHTASTICD_LOCAL_TARGET)
    return tuple(targets)


TCP_TARGET_SCHEME = "tcp://"


class ConnectionTargetError(ValueError):
    """A connection target that cannot be parsed into a transport."""


def parse_connection_target(target: object) -> tuple[str, str, int]:
    """Resolve a saved connection target into (kind, location, port).

    Returns ("serial", device_path, 0) or ("tcp", host, port). The port
    defaults to MESHTASTICD_TCP_PORT when the target omits one, so
    "tcp://localhost" and "tcp://localhost:4403" are the same target.

    Bare IPv6 literals are not supported: the host/port split is a
    rightmost-colon split, which a bare "::1" would defeat. meshtasticd
    on localhost is what this exists for, and "localhost" resolves
    either family, so the gap costs nothing real -- but it is a gap, not
    a subtlety to rediscover later.

    Anything without the tcp:// scheme is a serial device path and is
    returned untouched -- this must never try to be clever about what
    looks like a path, because every previously saved config holds a
    bare /dev/... string and has to keep meaning exactly what it meant.
    """
    if not isinstance(target, str) or not target.strip():
        raise ConnectionTargetError("Connection target cannot be empty.")
    target = target.strip()
    if not target.lower().startswith(TCP_TARGET_SCHEME):
        return ("serial", target, 0)

    remainder = target[len(TCP_TARGET_SCHEME):].strip()
    if not remainder:
        raise ConnectionTargetError(
            f"{target}: a tcp:// target needs a host, e.g. {MESHTASTICD_LOCAL_TARGET}"
        )
    host, separator, port_text = remainder.rpartition(":")
    if not separator:
        return ("tcp", remainder, MESHTASTICD_TCP_PORT)
    if not host:
        raise ConnectionTargetError(
            f"{target}: a tcp:// target needs a host, e.g. {MESHTASTICD_LOCAL_TARGET}"
        )
    try:
        port = int(port_text)
    except ValueError:
        raise ConnectionTargetError(
            f"{target}: {port_text!r} is not a port number."
        ) from None
    if not 1 <= port <= 65535:
        raise ConnectionTargetError(f"{target}: port {port} is out of range.")
    return ("tcp", host, port)


def describe_connection_target(target: object) -> str:
    """A short human label for a target, for status and error text."""
    try:
        kind, location, port = parse_connection_target(target)
    except ConnectionTargetError:
        return str(target)
    if kind == "serial":
        return location
    if location in ("localhost", "127.0.0.1"):
        return f"meshtasticd ({location}:{port})"
    return f"{location}:{port}"
