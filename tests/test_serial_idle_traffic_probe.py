"""Tests for serial_idle_traffic_probe.py's own logic -- never real hardware."""

from __future__ import annotations

import sys

from meshtastic.protobuf import mesh_pb2

import serial_idle_traffic_probe as probe


def test_describe_to_radio_names_heartbeat():
    to_radio = mesh_pb2.ToRadio()
    to_radio.heartbeat.CopyFrom(mesh_pb2.Heartbeat())

    assert probe.describe_to_radio(to_radio) == "heartbeat"


def test_describe_to_radio_names_want_config_id():
    to_radio = mesh_pb2.ToRadio()
    to_radio.want_config_id = 42

    assert probe.describe_to_radio(to_radio) == "want_config_id"


def test_describe_to_radio_handles_unrecognized_input_without_raising():
    assert probe.describe_to_radio(object()) == "unknown"
    assert probe.describe_to_radio(None) == "unknown"


class FakeInterface:
    def __init__(self):
        self.sent = []

    def _sendToRadioImpl(self, to_radio):
        self.sent.append(to_radio)


class FakeRadioService:
    """Mimics RadioService's connect()/close()/._interface just enough

    for main() to run end-to-end without any real hardware.
    """

    def __init__(self, device_path):
        self.device_path = device_path
        self._interface = FakeInterface()
        self.closed = False

    def connect(self):
        from radio_service import RadioInfo

        return RadioInfo(
            device_path=self.device_path,
            node_id="!fake0001",
            long_name="Fake",
            short_name="FAKE",
            firmware_version="fake",
            known_nodes=0,
        )

    def close(self):
        self.closed = True


def test_main_wraps_send_calls_through_and_restores_on_exit(monkeypatch):
    """main() must call straight through to the real _sendToRadioImpl

    (introducing no new outbound traffic itself, only observing what was
    already about to be sent) and must restore the original method and
    close the connection on exit, rather than leaving the interface
    patched or the connection open.
    """
    services = []

    def fake_radio_service(device_path):
        service = FakeRadioService(device_path)
        services.append(service)
        return service

    monkeypatch.setattr(probe, "RadioService", fake_radio_service)
    monkeypatch.setattr(sys, "argv", ["serial_idle_traffic_probe.py", "--device", "/dev/fake"])

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise KeyboardInterrupt  # stop the idle-wait loop on the first tick

    monkeypatch.setattr(probe.time, "sleep", fake_sleep)

    exit_code = probe.main()

    assert exit_code == 0
    assert len(sleep_calls) == 1
    service = services[0]
    assert service.closed is True
    # Restored to the real bound method -- no logging wrapper left attached.
    assert service._interface._sendToRadioImpl.__func__ is FakeInterface._sendToRadioImpl

    to_radio = mesh_pb2.ToRadio()
    to_radio.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
    service._interface._sendToRadioImpl(to_radio)
    assert service._interface.sent == [to_radio]
