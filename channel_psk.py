"""Meshtastic private-channel PSK semantics.

Pure logic only: no SDK, no radio, no I/O. Encodes the actual Meshtastic
PSK model this app must follow (see meshtastic.util.fromPSK/genPSK256 and
radio_service.psk_matches_request):

- A normal private-channel PSK is a 16-byte (128-bit) or 32-byte (256-bit)
  key, carried in a ChannelSettings.psk bytes field and rendered as
  standard Base64 text (the SDK's own "base64:" prefixed fromStr form).
- The 1-byte sentinels 0x00 ("no encryption") and 0x01 ("default channel
  PSK") are NOT private-channel keys -- they are how Meshtastic marks the
  public/default PSKs, so they are rejected here for a private channel
  rather than silently treated as a real secret.

CHANNEL IDENTITY IS DELIBERATELY NOT DEFINED HERE. MeshtasticPass's channel
identity (for CHAT history isolation) is radio_service.RadioService.
_channel_stable_key, which follows the actual Meshtastic protocol hierarchy:
Channel.settings.id (globally-unique ID) -> generate_channel_hash(name, psk)
(the on-air channel number the radio actually uses) -> resolved name. A
sha256(psk) key is a persistence convenience at best and is NOT the protocol
identity; deriving it here would conflict with that existing semantics, so
it is intentionally absent. wire the editor/model against the radio_service
identity, never a PSK-only hash.
"""

from __future__ import annotations

import base64
import os


def generate_private_psk() -> str:
    """Securely generate a Meshtastic-compatible private-channel PSK.

    Returns standard Base64 text of a freshly generated 32-byte (256-bit)
    key -- the same size meshtastic.util.genPSK256() (the SDK's own
    "random" PSK) produces. Never reuses or caches a key: each call is a
    fresh os.urandom draw.
    """
    return base64.b64encode(os.urandom(32)).decode("ascii")


def normalize_private_psk(value: str) -> tuple[str, bytes] | None:
    """Validate/normalize a user-supplied private-channel PSK.

    Accepts standard Base64 text that decodes to exactly 16 or 32 bytes
    (a real Meshtastic private key). Returns (canonical Base64, raw bytes)
    or None for anything else -- including empty input, invalid Base64,
    wrong length, and the 1-byte 0x00/0x01 sentinels, which must never be
    accepted as a genuine private-channel secret.
    """
    candidate = value.strip()
    if not candidate:
        return None
    try:
        raw = base64.b64decode(candidate, validate=True)
    except Exception:
        return None
    if len(raw) not in (16, 32):
        return None
    canonical = base64.b64encode(raw).decode("ascii")
    return canonical, raw
