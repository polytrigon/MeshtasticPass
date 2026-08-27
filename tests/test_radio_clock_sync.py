"""Tests for RadioService.sync_clock/supports_clock_sync, against a FAKE

Meshtastic interface -- never a real radio (see
tests/test_radio_config_write.py, whose FakeInterface/FakeLocalNode
scaffolding this reuses and extends for AdminMessage.set_time_only,
which has no get-time-equivalent RPC to simulate a full readback with;
see ClockSyncResult's own docstring for why "applied" here settles for
a best-effort rxTime cross-check rather than a verified readback).
"""

from __future__ import annotations

import unittest

from radio_service import ClockSyncResult, RadioService
from tests.test_radio_config_write import FakeInterface, FakeLocalNode


class FakeClockLocalNode(FakeLocalNode):
    def __init__(self, iface: FakeInterface) -> None:
        super().__init__(iface)
        self.drop_time_response = False
        self.rx_time_offset = 0

    def _sendAdmin(self, admin_message, onResponse=None, **kwargs):
        self.sendAdmin_calls.append(admin_message)
        if admin_message.HasField("set_time_only"):
            if self.drop_time_response:
                return
            packet = {
                "decoded": {
                    "portnum": "ROUTING_APP",
                    "routing": {
                        "errorReason": "MAX_RETRANSMIT"
                        if self.simulate_nak
                        else "NONE"
                    },
                    "requestId": 1,
                },
                "rxTime": admin_message.set_time_only + self.rx_time_offset,
            }
            if onResponse is not None:
                onResponse(packet)
            return
        return super()._sendAdmin(admin_message, onResponse=onResponse, **kwargs)


def make_clock_service() -> tuple[RadioService, FakeClockLocalNode]:
    service = RadioService("/dev/test-radio")
    interface = FakeInterface()
    node = FakeClockLocalNode(interface)
    service._interface = interface
    return service, node


class SupportsClockSyncTests(unittest.TestCase):
    def test_current_sdk_schema_supports_clock_sync(self) -> None:
        """SDK-SOURCE-VERIFIED against the installed meshtastic==2.7.11

        package: AdminMessage.set_time_only exists.
        """
        service, _node = make_clock_service()
        self.assertTrue(service.supports_clock_sync())


class SyncClockTests(unittest.TestCase):
    def test_matching_rx_time_reports_applied(self) -> None:
        service, node = make_clock_service()
        node.rx_time_offset = 1  # one second of round-trip latency

        result = service.sync_clock()

        self.assertTrue(result.applied)
        self.assertEqual(result.reason, "")
        self.assertEqual(result.observed_rx_time, result.requested_epoch + 1)

    def test_rx_time_far_outside_tolerance_is_unconfirmed(self) -> None:
        service, node = make_clock_service()
        node.rx_time_offset = 999  # nowhere near the requested epoch

        result = service.sync_clock()

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "unconfirmed")
        self.assertEqual(result.observed_rx_time, result.requested_epoch + 999)

    def test_nak_fails_fast(self) -> None:
        service, node = make_clock_service()
        node.simulate_nak = True

        result = service.sync_clock()

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "nak")

    def test_no_response_at_all_is_timeout(self) -> None:
        """The write is accepted by the fake but the radio never answers

        -- distinct from "unconfirmed" (a response arrived but didn't
        corroborate the write): here nothing at all came back.
        """
        service, node = make_clock_service()
        node.drop_time_response = True

        result = service.sync_clock()

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "timeout")
        self.assertIsNone(result.observed_rx_time)

    def test_not_connected_when_no_interface(self) -> None:
        service = RadioService("/dev/test-radio")
        result = service.sync_clock()
        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "not_connected")

    def test_connection_lost_during_write_is_disconnected(self) -> None:
        service, node = make_clock_service()

        def _sendAdmin_then_disconnect(admin_message, onResponse=None, **kwargs):
            node.sendAdmin_calls.append(admin_message)
            if admin_message.HasField("set_time_only"):
                service._connection_lost.set()

        node._sendAdmin = _sendAdmin_then_disconnect

        result = service.sync_clock()

        self.assertFalse(result.applied)
        self.assertEqual(result.reason, "disconnected")

    def test_never_reads_or_writes_any_config_section(self) -> None:
        """sync_clock is a completely separate admin field from

        set_config/get_config -- confirms it never accidentally touches
        localConfig (e.g. tzdef) as a side effect.
        """
        service, node = make_clock_service()
        service.sync_clock()
        for call in node.sendAdmin_calls:
            self.assertFalse(call.HasField("set_config"))
            self.assertFalse(call.HasField("get_config_request"))

    def test_host_epoch_is_captured_at_write_time_not_earlier(self) -> None:
        import time

        service, node = make_clock_service()
        before = int(time.time())
        result = service.sync_clock()
        after = int(time.time())
        self.assertGreaterEqual(result.requested_epoch, before)
        self.assertLessEqual(result.requested_epoch, after)


if __name__ == "__main__":
    unittest.main()
