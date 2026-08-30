"""Hardware-free tests for application-level TRACEROUTE_APP sending

(MESHTASTICPASS TRACE ROUTE Part C) -- mirrors tests/test_send_text.py's
own RealRadioSendTests structure (a Mock() interface standing in for
the Meshtastic SDK's MeshInterface) for the equivalent traceroute path.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from meshtastic.protobuf import mesh_pb2, portnums_pb2

from radio_service import (
    RadioSendError,
    RadioService,
    TracerouteState,
)
from simulated_radio_service import SimulatedRadioService, SimulatedTracerouteOutcome


LOCAL_NODE_NUMBER = 0x12345678
UNK_SNR = -128


def routing_failure_response(request_id: int, error_reason: int) -> dict:
    """The shape a ROUTING_APP NAK takes after SDK 2.7.11 conversion."""
    routing_message = mesh_pb2.Routing()
    routing_message.error_reason = error_reason
    return {
        "decoded": {
            "portnum": "ROUTING_APP",
            "requestId": request_id,
            "routing": {"errorReason": mesh_pb2.Routing.Error.Name(error_reason)},
        }
    }


def traceroute_success_response(
    request_id: int,
    *,
    route: tuple[int, ...] = (),
    snr_towards: tuple[int, ...] = (),
    route_back: tuple[int, ...] = (),
    snr_back: tuple[int, ...] = (),
) -> dict:
    """The shape a real TRACEROUTE_APP reply takes after the SDK's own

    generic decode step (see meshtastic/__init__.py's KnownProtocol
    registration for TRACEROUTE_APP -> mesh_pb2.RouteDiscovery, and
    mesh_interface.py's _handleFromRadio, which stashes the parsed
    protobuf itself under decoded["traceroute"]["raw"]).
    """
    discovery = mesh_pb2.RouteDiscovery()
    discovery.route.extend(route)
    discovery.snr_towards.extend(snr_towards)
    discovery.route_back.extend(route_back)
    discovery.snr_back.extend(snr_back)
    return {
        "decoded": {
            "portnum": "TRACEROUTE_APP",
            "requestId": request_id,
            "traceroute": {"raw": discovery},
        }
    }


class RealRadioTracerouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = RadioService()
        self.interface = Mock()
        self.interface.myInfo = SimpleNamespace(my_node_num=LOCAL_NODE_NUMBER)
        self.service._interface = self.interface

    def test_requires_an_active_connection(self) -> None:
        self.service._interface = None

        with self.assertRaisesRegex(RadioSendError, "not connected"):
            self.service.send_traceroute("!a11ce001", lambda status: None)

    def test_sends_a_route_discovery_to_the_traceroute_port(self) -> None:
        self.interface.sendData.return_value = SimpleNamespace(id=99)

        packet_id = self.service.send_traceroute("!a11ce001", lambda status: None)

        self.assertEqual(packet_id, 99)
        kwargs = self.interface.sendData.call_args.kwargs
        self.assertIsInstance(self.interface.sendData.call_args.args[0], mesh_pb2.RouteDiscovery)
        self.assertEqual(kwargs["destinationId"], "!a11ce001")
        self.assertEqual(kwargs["portNum"], portnums_pb2.PortNum.TRACEROUTE_APP)
        self.assertTrue(kwargs["wantResponse"])

    def test_hop_limit_reuses_the_apps_own_synced_lora_setting(self) -> None:
        """Never a second, independent hop-limit concept -- the SAME

        zero-RF read_synced_config_field the HOP LIMIT radio setting
        already uses (see app.HopLimitSelector).
        """
        self.interface.sendData.return_value = SimpleNamespace(id=1)
        self.interface.localNode.localConfig.lora.hop_limit = 5

        self.service.send_traceroute("!a11ce001", lambda status: None)

        self.assertEqual(self.interface.sendData.call_args.kwargs["hopLimit"], 5)

    def test_sdk_failure_becomes_application_error(self) -> None:
        self.interface.sendData.side_effect = OSError("serial link failed")

        with self.assertRaises(RadioSendError) as caught:
            self.service.send_traceroute("!a11ce001", lambda status: None)

        self.assertEqual(
            str(caught.exception), "Could not send traceroute: serial link failed"
        )

    def test_no_packet_id_is_a_send_error(self) -> None:
        self.interface.sendData.return_value = SimpleNamespace(id=None)

        with self.assertRaises(RadioSendError):
            self.service.send_traceroute("!a11ce001", lambda status: None)

    def test_routing_nak_resolves_as_failed(self) -> None:
        statuses = []
        self.interface.sendData.return_value = SimpleNamespace(id=42)
        self.service.send_traceroute("!a11ce001", statuses.append)
        on_response = self.interface.sendData.call_args.kwargs["onResponse"]

        on_response(routing_failure_response(42, mesh_pb2.Routing.Error.NO_ROUTE))

        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].state, TracerouteState.FAILED)
        self.assertEqual(statuses[0].packet_id, 42)
        self.assertIn("NO_ROUTE", statuses[0].detail)

    def test_clean_routing_ack_alone_is_not_yet_a_result(self) -> None:
        """A bare ACK on ROUTING_APP is not itself a completed traceroute

        -- the real RouteDiscovery reply is always a separate, later
        packet (see _parse_traceroute_response's own docstring).
        """
        statuses = []
        self.interface.sendData.return_value = SimpleNamespace(id=43)
        self.service.send_traceroute("!a11ce001", statuses.append)
        on_response = self.interface.sendData.call_args.kwargs["onResponse"]

        on_response(routing_failure_response(43, mesh_pb2.Routing.Error.NONE))

        self.assertEqual(statuses, [])

    def test_successful_route_discovery_reports_the_real_route(self) -> None:
        statuses = []
        self.interface.sendData.return_value = SimpleNamespace(id=44)
        self.service.send_traceroute("!a11ce001", statuses.append)
        on_response = self.interface.sendData.call_args.kwargs["onResponse"]

        on_response(
            traceroute_success_response(
                44,
                route=(0xAAAA0001, 0xBBBB0002),
                snr_towards=(40, 20, UNK_SNR),
                route_back=(0xBBBB0002,),
                snr_back=(12, 8),
            )
        )

        self.assertEqual(len(statuses), 1)
        status = statuses[0]
        self.assertEqual(status.state, TracerouteState.SUCCEEDED)
        self.assertEqual(status.packet_id, 44)
        result = status.result
        self.assertIsNotNone(result)
        self.assertEqual(result.destination_node_id, "!a11ce001")
        self.assertEqual(result.forward_route, ("!aaaa0001", "!bbbb0002"))
        self.assertEqual(result.forward_snr, (10.0, 5.0, None))
        self.assertEqual(result.return_route, ("!bbbb0002",))
        self.assertEqual(result.return_snr, (3.0, 2.0))

    def test_direct_connection_has_empty_routes_not_fabricated_hops(self) -> None:
        """A genuinely direct (zero-relay) connection reports EMPTY

        route tuples -- never an invented intermediate node.
        """
        statuses = []
        self.interface.sendData.return_value = SimpleNamespace(id=45)
        self.service.send_traceroute("!a11ce001", statuses.append)
        on_response = self.interface.sendData.call_args.kwargs["onResponse"]

        on_response(
            traceroute_success_response(45, snr_towards=(30,), snr_back=(30,))
        )

        result = statuses[0].result
        self.assertEqual(result.forward_route, ())
        self.assertEqual(result.return_route, ())

    def test_repeated_successful_trace_never_invents_missing_data(self) -> None:
        """No return-route evidence at all (no routeBack/snrBack) is

        reported honestly as empty, never fabricated from the forward
        route.
        """
        statuses = []
        self.interface.sendData.return_value = SimpleNamespace(id=46)
        self.service.send_traceroute("!a11ce001", statuses.append)
        on_response = self.interface.sendData.call_args.kwargs["onResponse"]

        on_response(traceroute_success_response(46, route=(0xCCCC0003,), snr_towards=(20, 20)))

        result = statuses[0].result
        self.assertEqual(result.return_route, ())
        self.assertEqual(result.return_snr, ())

    def test_unrelated_portnum_under_the_same_request_id_is_ignored(self) -> None:
        statuses = []
        self.interface.sendData.return_value = SimpleNamespace(id=47)
        self.service.send_traceroute("!a11ce001", statuses.append)
        on_response = self.interface.sendData.call_args.kwargs["onResponse"]

        on_response({"decoded": {"portnum": "TEXT_MESSAGE_APP", "requestId": 47}})

        self.assertEqual(statuses, [])


class SimulatedTracerouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SimulatedRadioService(connect_delay=0)
        self.service.connect()
        self.addCleanup(self.service.close)

    def test_requires_an_active_connection(self) -> None:
        service = SimulatedRadioService()
        with self.assertRaisesRegex(RadioSendError, "not connected"):
            service.send_traceroute("!a11ce001", lambda status: None)

    def test_default_outcome_is_a_deterministic_success(self) -> None:
        statuses = []
        packet_id = self.service.send_traceroute("!a11ce001", statuses.append)

        self.assertIsInstance(packet_id, int)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].state, TracerouteState.SUCCEEDED)
        self.assertEqual(statuses[0].result.destination_node_id, "!a11ce001")

    def test_explicit_outcomes_are_deterministic(self) -> None:
        service = SimulatedRadioService(
            connect_delay=0,
            traceroute_outcomes=(
                SimulatedTracerouteOutcome.SUCCEEDED,
                SimulatedTracerouteOutcome.FAILED,
                SimulatedTracerouteOutcome.NO_RESPONSE,
            ),
        )
        service.connect()
        self.addCleanup(service.close)
        statuses = []

        service.send_traceroute("!a", statuses.append)
        service.send_traceroute("!b", statuses.append)
        service.send_traceroute("!c", statuses.append)

        self.assertEqual(
            [status.state for status in statuses],
            [TracerouteState.SUCCEEDED, TracerouteState.FAILED],
        )

    def test_scripted_route_is_reported_verbatim(self) -> None:
        service = SimulatedRadioService(
            connect_delay=0,
            traceroute_forward_route=("!aaaa0001",),
            traceroute_forward_snr=(10.0, None),
            traceroute_return_route=(),
            traceroute_return_snr=(5.0,),
        )
        service.connect()
        self.addCleanup(service.close)
        statuses = []

        service.send_traceroute("!a11ce001", statuses.append)

        result = statuses[0].result
        self.assertEqual(result.forward_route, ("!aaaa0001",))
        self.assertEqual(result.forward_snr, (10.0, None))
        self.assertEqual(result.return_route, ())
        self.assertEqual(result.return_snr, (5.0,))


if __name__ == "__main__":
    unittest.main()
