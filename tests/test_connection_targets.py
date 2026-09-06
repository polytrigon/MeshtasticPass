"""Connection-target parsing: the one place transport choice is decided.

Everything above RadioService._open_interface is transport-agnostic --
the node database, sends, connection events, hardware_identity() and the
whole capability matrix read a MeshInterface, and SerialInterface and
TCPInterface are both MeshInterface subclasses. So these tests cover the
only code that can send a radio to the wrong transport, and they need no
hardware and no meshtastic package to run.
"""

from __future__ import annotations

import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serial_devices import (  # noqa: E402
    MESHTASTICD_LOCAL_TARGET,
    MESHTASTICD_TCP_PORT,
    ConnectionTargetError,
    describe_connection_target,
    discover_connection_targets,
    meshtasticd_is_listening,
    parse_connection_target,
)


class ParseConnectionTargetTests(unittest.TestCase):
    def test_a_device_path_is_returned_untouched(self) -> None:
        """Every settings file already written holds a bare serial path.

        A saved preference must keep meaning exactly what it meant, so
        anything without the tcp:// scheme is passed through rather than
        inspected for path-shaped-ness.
        """
        for path in ("/dev/ttyUSB0", "/dev/ttyACM0", "/dev/cu.usbserial-10"):
            self.assertEqual(parse_connection_target(path), ("serial", path, 0))

    def test_tcp_target_defaults_to_the_meshtasticd_port(self) -> None:
        self.assertEqual(
            parse_connection_target("tcp://localhost"),
            ("tcp", "localhost", MESHTASTICD_TCP_PORT),
        )

    def test_tcp_target_honours_an_explicit_port(self) -> None:
        self.assertEqual(
            parse_connection_target("tcp://192.168.1.50:4404"),
            ("tcp", "192.168.1.50", 4404),
        )

    def test_scheme_is_case_insensitive_and_padding_is_ignored(self) -> None:
        self.assertEqual(
            parse_connection_target("  TCP://Meshy.local:4403  "),
            ("tcp", "Meshy.local", 4403),
        )

    def test_the_advertised_local_target_parses_to_localhost(self) -> None:
        """The constant offered in the connection list must be one this

        parser accepts -- otherwise the app offers a target it cannot open.
        """
        self.assertEqual(
            parse_connection_target(MESHTASTICD_LOCAL_TARGET),
            ("tcp", "localhost", MESHTASTICD_TCP_PORT),
        )

    def test_unusable_targets_are_rejected_not_guessed_at(self) -> None:
        for value in (
            "",
            "   ",
            None,
            5,
            "tcp://",
            "tcp://:4403",
            "tcp://host:abc",
            "tcp://host:0",
            "tcp://host:70000",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ConnectionTargetError):
                    parse_connection_target(value)

    def test_rejection_is_a_valueerror(self) -> None:
        """Existing callers catch ValueError around target changes; the

        more specific type must not slip past them.
        """
        self.assertTrue(issubclass(ConnectionTargetError, ValueError))


class DescribeConnectionTargetTests(unittest.TestCase):
    def test_a_serial_path_describes_as_itself(self) -> None:
        self.assertEqual(describe_connection_target("/dev/ttyUSB0"), "/dev/ttyUSB0")

    def test_a_local_tcp_target_names_meshtasticd(self) -> None:
        self.assertEqual(
            describe_connection_target(MESHTASTICD_LOCAL_TARGET),
            f"meshtasticd (localhost:{MESHTASTICD_TCP_PORT})",
        )

    def test_a_remote_tcp_target_shows_host_and_port(self) -> None:
        self.assertEqual(
            describe_connection_target("tcp://192.168.1.50:4403"),
            "192.168.1.50:4403",
        )

    def test_an_unparseable_target_still_describes_rather_than_raising(self) -> None:
        """This runs inside error messages and dropdown labels; it must

        never be the thing that raises while reporting a problem.
        """
        self.assertEqual(describe_connection_target("tcp://"), "tcp://")


class MeshtasticdProbeTests(unittest.TestCase):
    def test_a_listening_socket_is_detected(self) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            self.assertTrue(meshtasticd_is_listening("127.0.0.1", port))

    def test_a_closed_port_is_reported_closed(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.assertFalse(meshtasticd_is_listening("127.0.0.1", port))

    def test_a_listening_daemon_is_offered_as_a_target(self) -> None:
        """Discovery must offer exactly the constant the parser accepts --

        otherwise the connection list shows a target the app cannot open.
        """
        with mock.patch("serial_devices.meshtasticd_is_listening", return_value=True):
            targets = discover_connection_targets()
        self.assertIn(MESHTASTICD_LOCAL_TARGET, targets)
        self.assertEqual(
            parse_connection_target(targets[-1]),
            ("tcp", "localhost", MESHTASTICD_TCP_PORT),
        )

    def test_nothing_listening_means_nothing_extra_offered(self) -> None:
        with mock.patch("serial_devices.meshtasticd_is_listening", return_value=False):
            targets = discover_connection_targets()
        self.assertNotIn(MESHTASTICD_LOCAL_TARGET, targets)

    def test_serial_devices_are_still_offered_alongside(self) -> None:
        with mock.patch(
            "serial_devices.discover_serial_devices",
            return_value=("/dev/ttyUSB0",),
        ), mock.patch("serial_devices.meshtasticd_is_listening", return_value=True):
            targets = discover_connection_targets()
        self.assertEqual(targets, ("/dev/ttyUSB0", MESHTASTICD_LOCAL_TARGET))


if __name__ == "__main__":
    unittest.main()
