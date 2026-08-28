"""Tests for host_timezone -- local Linux timezone detection and IANA

-> Meshtastic device.tzdef conversion. Pure host introspection: no
RadioService, no app.py, no Meshtastic traffic anywhere in this module
or its tests.
"""

from __future__ import annotations

import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import host_timezone
from host_timezone import (
    HostTimezone,
    TZDEF_MAX_LENGTH,
    _SAFE_POSIX_TZDEF,
    _read_tzif_footer,
    _tzdef_for_iana_name,
    detect_host_timezone,
)


def _write_tzif_v2(path: Path, footer: str) -> None:
    """A minimal-but-well-formed version-2 tzfile: an empty version-1

    block, then an empty version-2 block, then the "\\nFOOTER\\n" the
    real IANA tzdata compiler (zic) appends -- exactly what
    _read_tzif_footer parses (see tzfile(5)/RFC 8536). Field values
    inside each block are all zero/empty; only the footer matters for
    these tests.
    """
    # Real v2/v3 tzfiles mark BOTH the leading v1-compatible header and
    # the v2 header that follows the v1 data block with the SAME
    # version byte ('2') -- readers rely on the FIRST header's version
    # to know a v2 block (and therefore a footer) follows at all.
    v1_header = b"TZif" + b"2" + b"\x00" * 15 + struct.pack(">6l", 0, 0, 0, 0, 0, 0)
    v2_header = b"TZif" + b"2" + b"\x00" * 15 + struct.pack(">6l", 0, 0, 0, 0, 0, 0)
    footer_block = b"\n" + footer.encode("ascii") + b"\n"
    path.write_bytes(v1_header + v2_header + footer_block)


class ReadTzifFooterTests(unittest.TestCase):
    def test_parses_a_well_formed_version_2_footer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Fake/Zone"
            path.parent.mkdir(parents=True)
            _write_tzif_v2(path, "EST5EDT,M3.2.0,M11.1.0")

            self.assertEqual(_read_tzif_footer(str(path)), "EST5EDT,M3.2.0,M11.1.0")

    def test_version_1_only_file_has_no_footer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old"
            path.write_bytes(b"TZif" + b"\x00" * 40)

            self.assertIsNone(_read_tzif_footer(str(path)))

    def test_non_tzif_data_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-a-tzfile"
            path.write_bytes(b"not a tzfile at all")

            self.assertIsNone(_read_tzif_footer(str(path)))

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(_read_tzif_footer("/nonexistent/path/to/nowhere"))

    def test_footer_without_trailing_newline_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "truncated"
            v2_header = b"TZif" + b"2" + b"\x00" * 15 + struct.pack(">6l", 0, 0, 0, 0, 0, 0)
            path.write_bytes(v2_header + b"\nEST5EDT")  # no trailing \n

            self.assertIsNone(_read_tzif_footer(str(path)))


class SafePosixTzdefShapeTests(unittest.TestCase):
    def test_accepts_known_good_forms(self) -> None:
        for tzdef in (
            "EST5EDT,M3.2.0,M11.1.0",
            "CST6CDT,M3.2.0,M11.1.0",
            "MST7MDT,M3.2.0,M11.1.0",
            "PST8PDT,M3.2.0,M11.1.0",
            "AKST9AKDT,M3.2.0,M11.1.0",
            "HST10",
            "JST-9",
            "UTC0",
            "GMT0",
            "GMT0BST,M3.5.0/1,M10.5.0",
            "IST-5:30",
            "AEST-10AEDT,M10.1.0,M4.1.0/3",
        ):
            with self.subTest(tzdef=tzdef):
                self.assertIsNotNone(_SAFE_POSIX_TZDEF.match(tzdef))

    def test_rejects_angle_bracket_numeric_names(self) -> None:
        for tzdef in ("<+14>-14", "<-03>3", "<+1030>-10:30<+11>-11,M10.1.0,M4.1.0"):
            with self.subTest(tzdef=tzdef):
                self.assertIsNone(_SAFE_POSIX_TZDEF.match(tzdef))


class TzdefForIanaNameTests(unittest.TestCase):
    real_zoneinfo_root = "/usr/share/zoneinfo"

    @unittest.skipUnless(
        os.path.isfile("/usr/share/zoneinfo/America/New_York"),
        "no system zoneinfo tree available",
    )
    def test_eastern_matches_the_real_hardware_verified_string_exactly(self) -> None:
        """The REAL-HARDWARE-VERIFIED anchor (see app.py's TIMEZONE_

        CHOICES): America/New_York's own tzdata-embedded POSIX footer
        must be exactly "EST5EDT,M3.2.0,M11.1.0" -- never altered.
        """
        tzdef, detail = _tzdef_for_iana_name("America/New_York")
        self.assertEqual(tzdef, "EST5EDT,M3.2.0,M11.1.0")
        self.assertIn("America/New_York", detail)

    @unittest.skipUnless(
        os.path.isfile("/usr/share/zoneinfo/UTC"), "no system zoneinfo tree available"
    )
    def test_utc_produces_a_safe_no_dst_tzdef(self) -> None:
        tzdef, _detail = _tzdef_for_iana_name("UTC")
        self.assertEqual(tzdef, "UTC0")

    def test_unknown_zone_name_yields_no_tzdef(self) -> None:
        tzdef, detail = _tzdef_for_iana_name("Not/AZone")
        self.assertIsNone(tzdef)
        self.assertIn("no zoneinfo file found", detail)

    def test_oversized_footer_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Fake/Huge"
            path.parent.mkdir(parents=True)
            _write_tzif_v2(path, "X" * (TZDEF_MAX_LENGTH + 1))

            with patch.object(host_timezone.zoneinfo, "TZPATH", (str(root),)):
                tzdef, detail = _tzdef_for_iana_name("Fake/Huge")

        self.assertIsNone(tzdef)
        self.assertIn("safety margin", detail)

    def test_unsafe_shaped_footer_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Fake/Weird"
            path.parent.mkdir(parents=True)
            _write_tzif_v2(path, "<+14>-14")

            with patch.object(host_timezone.zoneinfo, "TZPATH", (str(root),)):
                tzdef, detail = _tzdef_for_iana_name("Fake/Weird")

        self.assertIsNone(tzdef)
        self.assertIn("safe/verified POSIX TZ shape", detail)

    def test_version_1_only_tzfile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Fake/OldOnly"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"TZif" + b"\x00" * 40)

            with patch.object(host_timezone.zoneinfo, "TZPATH", (str(root),)):
                tzdef, detail = _tzdef_for_iana_name("Fake/OldOnly")

        self.assertIsNone(tzdef)
        self.assertIn("no readable version-2/3", detail)


class DetectIanaNameFallbackChainTests(unittest.TestCase):
    """Exercises the actual /etc/timezone -> /etc/localtime ->

    timedatectl fallback DECISION LOGIC -- see "do not depend
    exclusively on timedatectl". Real temp files/symlinks stand in for
    /etc/timezone and /etc/localtime (patched via the module's own
    ETC_TIMEZONE_PATH/ETC_LOCALTIME_PATH constants) rather than mocking
    open()/os.path.realpath directly.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.zoneinfo_root = self.root / "zoneinfo"
        (self.zoneinfo_root / "America").mkdir(parents=True)
        _write_tzif_v2(
            self.zoneinfo_root / "America/New_York", "EST5EDT,M3.2.0,M11.1.0"
        )
        (self.zoneinfo_root / "Etc").mkdir(parents=True)
        _write_tzif_v2(self.zoneinfo_root / "Etc/UTC", "UTC0")
        self.etc_timezone = self.root / "timezone"
        self.etc_localtime = self.root / "localtime"
        self.zoneinfo_patch = patch.object(
            host_timezone.zoneinfo, "TZPATH", (str(self.zoneinfo_root),)
        )
        self.zoneinfo_patch.start()
        self.addCleanup(self.zoneinfo_patch.stop)

    def _patch_paths(self):
        return (
            patch.object(host_timezone, "ETC_TIMEZONE_PATH", str(self.etc_timezone)),
            patch.object(host_timezone, "ETC_LOCALTIME_PATH", str(self.etc_localtime)),
        )

    def test_prefers_etc_timezone_when_present_and_valid(self) -> None:
        self.etc_timezone.write_text("America/New_York\n", encoding="ascii")
        self.etc_localtime.symlink_to(self.zoneinfo_root / "Etc/UTC")

        with self._patch_paths()[0], self._patch_paths()[1]:
            result = detect_host_timezone()

        self.assertEqual(result.iana_name, "America/New_York")
        self.assertEqual(result.source, "/etc/timezone")
        self.assertEqual(result.tzdef, "EST5EDT,M3.2.0,M11.1.0")

    def test_falls_back_to_etc_localtime_symlink_when_etc_timezone_missing(
        self,
    ) -> None:
        self.etc_localtime.symlink_to(self.zoneinfo_root / "America/New_York")

        with self._patch_paths()[0], self._patch_paths()[1]:
            result = detect_host_timezone()

        self.assertEqual(result.iana_name, "America/New_York")
        self.assertEqual(result.source, "/etc/localtime")
        self.assertEqual(result.tzdef, "EST5EDT,M3.2.0,M11.1.0")

    def test_falls_back_past_an_invalid_etc_timezone_value(self) -> None:
        self.etc_timezone.write_text("Not/AZone\n", encoding="ascii")
        self.etc_localtime.symlink_to(self.zoneinfo_root / "Etc/UTC")

        with self._patch_paths()[0], self._patch_paths()[1]:
            result = detect_host_timezone()

        self.assertEqual(result.iana_name, "Etc/UTC")
        self.assertEqual(result.source, "/etc/localtime")

    def test_falls_back_to_timedatectl_when_the_other_two_sources_fail(self) -> None:
        with self._patch_paths()[0], self._patch_paths()[1], patch.object(
            host_timezone.shutil, "which", return_value="/usr/bin/timedatectl"
        ), patch.object(host_timezone.subprocess, "run") as run:
            run.return_value.stdout = "America/New_York\n"
            result = detect_host_timezone()

        self.assertEqual(result.iana_name, "America/New_York")
        self.assertEqual(result.source, "timedatectl")

    def test_never_depends_exclusively_on_timedatectl(self) -> None:
        """timedatectl absent entirely must not be a hard failure by

        itself -- /etc/timezone or /etc/localtime alone are each
        sufficient (see the two tests above); this proves the
        REVERSE too: with all three genuinely unavailable, detection
        fails safely rather than raising or hanging.
        """
        with self._patch_paths()[0], self._patch_paths()[1], patch.object(
            host_timezone.shutil, "which", return_value=None
        ):
            result = detect_host_timezone()

        self.assertIsNone(result.iana_name)
        self.assertIsNone(result.tzdef)
        self.assertEqual(result.source, "none")

    def test_all_three_sources_failing_yields_no_tzdef_never_raises(self) -> None:
        with self._patch_paths()[0], self._patch_paths()[1], patch.object(
            host_timezone.shutil, "which", return_value=None
        ):
            result = detect_host_timezone()

        self.assertEqual(result, HostTimezone(None, None, "none", result.detail))
        self.assertIn("unavailable", result.detail)


class DetectHostTimezoneRealSystemSmokeTest(unittest.TestCase):
    @unittest.skipUnless(
        os.path.exists("/etc/localtime") or os.path.exists("/etc/timezone"),
        "no host timezone configuration available on this machine",
    )
    def test_detects_something_plausible_on_the_real_host(self) -> None:
        result = detect_host_timezone()
        self.assertIsNotNone(result.iana_name)
        if result.tzdef is not None:
            self.assertLessEqual(len(result.tzdef), TZDEF_MAX_LENGTH)


if __name__ == "__main__":
    unittest.main()
