"""Meshtastic private-channel PSK semantics and stable channel identity.

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
- A private channel's STABLE identity (history isolation, never keyed by
  display name alone) is its cryptographic key: two peers using the same
  PSK share the same logical private channel regardless of the differing
  local display names Meshtastic protocol semantics permit. Keying by
  sha256(psk) keeps that true while staying opaque and length-stable.
"""

from __future__ import annotations

import base64
import hashlib
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


def channel_stable_key(raw_psk: bytes, display_name: str) -> str:
    """A stable private-channel identity independent of slot index and name.

    Derived from the cryptographic PSK itself (sha256), so two peers using
    the same key join the same logical channel even with different local
    display names -- the ordering Meshtastic protocol semantics permit.
    Only if no key bytes are available (should not happen for a genuinely
    private channel) does it fall back to a name-derived key, which is
    strictly a display-name identity and not a cryptographic guarantee.
    """
    if raw_psk:
        return "psk:" + hashlib.sha256(raw_psk).hexdigest()
    normalized_name = (display_name or "").strip().lower()
    return "name:" + normalized_name
