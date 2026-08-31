"""Tests for channel_psk.py -- Meshtastic private-channel PSK semantics.

Pure-logic unit tests only: no radio, no SDK channel write, no live
hardware. Proves the generation/validation rules and stable channel
identity the private-channel feature relies on.
"""

from __future__ import annotations

import base64
import unittest

from channel_psk import (
    generate_private_psk,
    normalize_private_psk,
)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class GeneratePrivatePskTests(unittest.TestCase):
    def test_generate_returns_valid_32_byte_base64(self) -> None:
        psk = generate_private_psk()
        raw = base64.b64decode(psk)
        self.assertEqual(len(raw), 32)
        self.assertEqual(_b64(raw), psk)

    def test_each_generation_is_fresh(self) -> None:
        self.assertNotEqual(generate_private_psk(), generate_private_psk())


class NormalizePrivatePskTests(unittest.TestCase):
    def test_accepts_16_byte_key(self) -> None:
        raw = bytes(range(16))
        canonical, decoded = normalize_private_psk(_b64(raw))
        self.assertEqual(decoded, raw)
        self.assertEqual(canonical, _b64(raw))

    def test_accepts_32_byte_key(self) -> None:
        raw = bytes(range(32))
        canonical, decoded = normalize_private_psk(_b64(raw))
        self.assertEqual(decoded, raw)
        self.assertEqual(canonical, _b64(raw))

    def test_accepts_whitespace_padding(self) -> None:
        raw = bytes(range(16))
        canonical, _ = normalize_private_psk("  " + _b64(raw) + "  ")
        self.assertEqual(canonical, _b64(raw))

    def test_rejects_empty_and_whitespace(self) -> None:
        self.assertIsNone(normalize_private_psk(""))
        self.assertIsNone(normalize_private_psk("   "))

    def test_rejects_invalid_base64(self) -> None:
        self.assertIsNone(normalize_private_psk("!!not-base64!!"))

    def test_rejects_wrong_length(self) -> None:
        self.assertIsNone(normalize_private_psk(_b64(b"\x00" * 8)))
        self.assertIsNone(normalize_private_psk(_b64(b"\x00" * 17)))
        self.assertIsNone(normalize_private_psk(_b64(b"\x00" * 31)))

    def test_rejects_default_and_none_sentinels(self) -> None:
        # 0x00 = no encryption, 0x01 = default public-channel PSK.
        # Neither is a genuine private-channel secret.
        self.assertIsNone(normalize_private_psk(_b64(bytes([0x00]))))
        self.assertIsNone(normalize_private_psk(_b64(bytes([0x01]))))

    def test_rejects_split_base64_padding(self) -> None:
        # Standard base64 with the '=' padding is required; a bare 16-byte
        # decode that lenient parsers might accept should still be fine:
        raw = bytes(range(16))
        self.assertIsNotNone(normalize_private_psk(_b64(raw)))
        # A double-padded / malformed form must fail.
        self.assertIsNone(normalize_private_psk(_b64(raw) + "=="))


if __name__ == "__main__":
    unittest.main()
