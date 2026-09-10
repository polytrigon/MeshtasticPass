"""Monitor connection state and record received Meshtastic text messages.

WHY THIS PERSISTS BY DEFAULT
----------------------------
Draining the radio is DESTRUCTIVE. The firmware holds packets bound for
a client in `MeshService::toPhoneQueue` (MAX_RX_TOPHONE: 8 on classic
ESP32/STM32WL, 16 on nRF52840, 32 on RP2040/ESP32-C3) and
`PhoneAPI::getFromRadio` calls `releasePhonePacket()` the moment one is
handed over. The serial link is single-consumer, so whoever connects
takes the only copy the radio has.

This tool used to print and nothing else. That made it a data shredder:
every minute it ran was a minute of mesh traffic that reached the LCD,
scrolled past a terminal, and was gone -- while the app, unable to hold
the port at the same time, recorded nothing. Three messages were lost
that way while hunting a bug about lost messages.

So: reading without recording is not a neutral act here, and this
records by default. `--no-store` exists for the simulated radio and for
deliberately read-only runs; it should be a conscious choice, never the
path of least resistance.

Because it persists into the same database the app reads, it doubles as
a headless collector: leave it running and the traffic is waiting in
CHAT afterwards, instead of being silently dropped when the radio's
small queue wraps.
"""

from __future__ import annotations

import argparse
import time

from chat_store import ChatStore, ChatStoreError, canonical_profile_key
from radio_service import (
    RadioInfo,
    ReceivedMessage,
    RadioService,
    RadioState,
)
from simulated_radio_service import SimulatedRadioService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Receive Meshtastic text messages")
    parser.add_argument(
        "--device",
        default="/dev/ttyUSB0",
        help="serial device path (default: /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--retry-delay",
        default=5.0,
        type=float,
        help="seconds between connection attempts (default: 5)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use deterministic fake radio events instead of hardware",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="chat database path (default: the app's own database)",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help=(
            "do NOT record what is received. Draining the radio destroys "
            "its only copy, so this discards real traffic -- use only for "
            "read-only checks."
        ),
    )
    return parser.parse_args()


def display(value: object) -> object:
    return "unknown" if value is None or value == "" else value


def print_message(message: ReceivedMessage) -> None:
    print("RX MESSAGE")
    print(f"FROM      {display(message.sender_node_id)}")
    print(
        f"NAME      {display(message.sender_long_name)} "
        f"({display(message.sender_short_name)})"
    )
    print(f"CHANNEL   {display(message.channel_index)}")
    print(f"TEXT      {message.text}")
    print(f"RSSI      {display(message.rssi)}")
    print(f"SNR       {display(message.snr)}")
    print(f"PACKET    {display(message.packet_id)}")


class MessageRecorder:
    """Write received messages into the app's own CHAT history.

    Mirrors MeshtasticPassApp._persist_incoming rather than inventing a
    second persistence path, because the app's reads are NARROWED and a
    naive insert is invisible:

    - every read is filtered by `profile_key` (ChatStore._ns_filter), so
      a row written by an unbound store (profile_key NULL) is preserved
      but hidden -- it would look exactly like the bug this tool exists
      to investigate;
    - channel rows carry `channel_key`, the channel's own stable
      identity, never its slot index.

    Both come from the ONLINE event's RadioInfo, so the profile is bound
    on every connect and rebound if the radio's SHORT NAME changed while
    we were away.
    """

    def __init__(self, store: ChatStore) -> None:
        self._store = store
        self._channel_keys: dict[int, str] = {}
        self._bound_profile: str | None = None

    def bind(self, info: RadioInfo | None) -> None:
        """Adopt the connected radio's identity as the history profile."""
        if info is None:
            return
        self._channel_keys = {
            channel.index: channel.stable_key
            for channel in info.channels
            if channel.stable_key
        }
        profile_key = canonical_profile_key(info.node_id, info.short_name)
        if profile_key is None:
            # Node id usable but SHORT NAME blank: identity unresolved.
            # Binding to a half-identity would file rows under a profile
            # the app will never look under.
            print("PROFILE   unresolved (no SHORT NAME) -- not recording")
            self._bound_profile = None
            self._store.set_active_profile(None)
            return
        self._store.ensure_profile(info.node_id, info.short_name)
        self._store.set_active_profile(profile_key)
        if profile_key != self._bound_profile:
            print(f"PROFILE   {profile_key}")
            self._bound_profile = profile_key

    def unbind(self) -> None:
        """Stop owning writes while the radio's identity is unknown.

        A reconnect can bring up a DIFFERENT radio, or the same one
        renamed. Writing into the previous profile during that window
        would attribute another radio's traffic to it.
        """
        self._bound_profile = None
        self._store.set_active_profile(None)

    def record(self, message: ReceivedMessage) -> None:
        if self._bound_profile is None:
            print("STORE     skipped (no profile bound)")
            return
        channel_index = message.channel_index or 0
        is_direct = bool(message.is_direct)
        try:
            result = self._store.add_incoming(
                packet_id=message.packet_id,
                node_id=message.sender_node_id or "unknown",
                sender_name=message.sender_long_name,
                sender_short_name=message.sender_short_name,
                channel_index=channel_index,
                text=message.text,
                origin_sent_at=message.origin_sent_at,
                radio_rx_at=message.radio_rx_at,
                received_at=time.time(),
                dm_node_id=message.sender_node_id if is_direct else None,
                channel_key=(
                    None if is_direct else self._channel_keys.get(channel_index)
                ),
            )
        except ChatStoreError as error:
            # Never let persistence take down the receive loop: a message
            # printed is still better than a message neither printed nor
            # stored.
            print(f"STORE     FAILED {error}")
            return
        print(
            f"STORE     id={result.message_id} "
            + ("inserted" if result.inserted else "duplicate, ignored")
        )


def main() -> int:
    args = parse_args()
    simulated = args.simulate
    radio = SimulatedRadioService() if simulated else RadioService(args.device)

    store: ChatStore | None = None
    recorder: MessageRecorder | None = None
    if not args.no_store and not simulated:
        try:
            store = ChatStore.open(args.db)
        except ChatStoreError as error:
            print(f"CHAT STORE UNAVAILABLE: {error}")
            print("Refusing to drain the radio without recording.")
            print("Pass --no-store to override (this discards real traffic).")
            return 1
        recorder = MessageRecorder(store)
    elif simulated and not args.no_store:
        print("SIMULATED radio: not recording (fake nodes never reach history)")

    def handle(message: ReceivedMessage) -> None:
        print_message(message)
        if recorder is not None:
            recorder.record(message)

    radio.add_message_handler(handle)

    try:
        for event in radio.connection_events(args.retry_delay):
            if event.state is RadioState.CONNECTING:
                print(f"RADIO CONNECTING: {radio.device_path}")
            elif event.state is RadioState.ONLINE:
                print("RADIO ONLINE")
                if recorder is not None:
                    recorder.bind(event.info)
            elif event.state is RadioState.OFFLINE:
                print("RADIO OFFLINE")
                print(event.message)
                print(f"Retrying in {args.retry_delay:g} seconds...")
                if recorder is not None:
                    recorder.unbind()
            elif event.state is RadioState.ERROR:
                print("RADIO ERROR")
                print(event.message)
                print(f"Retrying in {args.retry_delay:g} seconds...")
                if recorder is not None:
                    recorder.unbind()
    except KeyboardInterrupt:
        print("\nMessage monitor stopped.")
    finally:
        radio.remove_message_handler(handle)
        radio.close()
        if store is not None:
            store.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
