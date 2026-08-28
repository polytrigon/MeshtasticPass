"""Local Linux host timezone detection and IANA -> Meshtastic

device.tzdef conversion.

Pure host introspection: nothing here touches a RadioService, sends
RF/admin traffic, or imports anything from this codebase's own
radio_*/app.py modules -- detecting (and even converting) the host's
timezone must cause zero Meshtastic config traffic by itself. See
app.py's _maybe_auto_sync_clock for the one caller, which uses this to
let CLOCK SYNC keep the radio's device.tzdef matching this host's own
timezone alongside the epoch it already syncs.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shutil
import subprocess

try:
    import zoneinfo
except ImportError:  # pragma: no cover -- zoneinfo is stdlib on 3.9+
    zoneinfo = None  # type: ignore[assignment]


# nanopb's own DeviceConfig.tzdef max_size is 65 bytes (PROTOBUF-
# SOURCE-VERIFIED against meshtastic/protobufs' config.options -- see
# app.py's own TIMEZONE_CHOICES audit). This stays comfortably under
# that, leaving margin for the field's own encoding overhead.
TZDEF_MAX_LENGTH = 48

# A deliberately conservative shape check for the POSIX TZ strings this
# module ever proposes writing to device.tzdef: STDoffset[DST[offset]]
# [,rule,rule]. Narrower than the full POSIX TZ grammar -- e.g. this
# rejects the angle-bracket "<+1030>" numeric-name form some real
# tzdata footers use (Pacific/Kiritimati, Australia/Lord_Howe) since
# firmware's own TZ-string parser has never been verified against that
# less common syntax. A zone whose footer needs it is treated the same
# as any other conversion failure: epoch still syncs, device.tzdef is
# left untouched.
_SAFE_POSIX_TZDEF = re.compile(
    r"^[A-Za-z]{3,}[+-]?\d{1,2}(:[0-5]\d){0,2}"
    r"([A-Za-z]{3,}([+-]?\d{1,2}(:[0-5]\d){0,2})?"
    r"(,M\d{1,2}\.[1-5]\.[0-6](/-?\d{1,3}(:[0-5]\d){0,2})?"
    r",M\d{1,2}\.[1-5]\.[0-6](/-?\d{1,3}(:[0-5]\d){0,2})?)?)?$"
)


@dataclass(frozen=True)
class HostTimezone:
    """Result of one detect_host_timezone() call.

    `iana_name` is the validated zone name detection settled on (or
    None if every source failed). `tzdef` is the derived POSIX TZ
    string ready to write to device.tzdef, or None if detection or
    conversion could not proceed safely -- callers must treat None as
    "do not touch device.tzdef", never fall back to guessing. `detail`
    is a human-readable diagnostic explaining the outcome either way.
    """

    iana_name: str | None
    tzdef: str | None
    source: str
    detail: str


def detect_host_timezone() -> HostTimezone:
    """Best-effort, read-only host timezone detection, followed by a

    conservative IANA -> Meshtastic device.tzdef conversion.

    Detection tries, in order (never depends on any single one -- see
    the "do not depend exclusively on timedatectl" requirement this
    exists to satisfy):

    1. /etc/timezone -- a plain-text IANA name (Debian/Ubuntu/Raspberry
       Pi OS convention, likely what a uConsole runs).
    2. /etc/localtime -- if it is a symlink into a zoneinfo tree (the
       other common convention), the IANA name is read from the
       symlink's own resolved target path, never its binary contents.
    3. `timedatectl show -p Timezone --value` (systemd) -- only if the
       binary exists on PATH, and only ever as the last resort.

    The first candidate any source yields that zoneinfo.ZoneInfo(...)
    actually accepts as a real zone is used; if none do, both
    `iana_name` and `tzdef` are None.

    Conversion reads the SAME authoritative POSIX TZ string the IANA
    tzdata compiler (zic) already embeds as the version-2/3 tzfile
    "footer" for that exact zone (see tzfile(5)/RFC 8536) -- e.g.
    /usr/share/zoneinfo/America/New_York's own footer is exactly
    "EST5EDT,M3.2.0,M11.1.0", matching the REAL-HARDWARE-VERIFIED
    string for that zone precisely. This is preferred over hand-
    deriving DST transition rules from scratch: it is the exact string
    glibc/most Linux userspace TZ parsers already treat as
    authoritative for that zone's own dates beyond its explicit
    transition table, so there is no separate guessing about DST
    rules, transition dates, or POSIX's reversed offset-sign
    convention -- see _SAFE_POSIX_TZDEF's own docstring for the one
    class of footer this still declines to use.
    """
    name, source, name_detail = _detect_iana_name()
    if name is None:
        return HostTimezone(None, None, source, name_detail)
    tzdef, tzdef_detail = _tzdef_for_iana_name(name)
    return HostTimezone(name, tzdef, source, tzdef_detail)


def _validated(candidate: str | None) -> str | None:
    if not candidate:
        return None
    candidate = candidate.strip()
    if not candidate or "\n" in candidate:
        return None
    if zoneinfo is None:
        return None
    try:
        zoneinfo.ZoneInfo(candidate)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return candidate


# Real paths by default; module-level so tests can point these at a
# temp file/symlink instead of mocking open()/os.path.realpath
# directly (see tests/test_host_timezone.py).
ETC_TIMEZONE_PATH = "/etc/timezone"
ETC_LOCALTIME_PATH = "/etc/localtime"


def _detect_iana_name() -> tuple[str | None, str, str]:
    try:
        with open(ETC_TIMEZONE_PATH, encoding="ascii") as handle:
            raw = handle.read()
    except OSError as error:
        raw = None
        etc_timezone_detail = f"{ETC_TIMEZONE_PATH} unavailable ({error})"
    else:
        etc_timezone_detail = f"{ETC_TIMEZONE_PATH} contained {raw.strip()!r}"
    candidate = _validated(raw)
    if candidate is not None:
        return candidate, "/etc/timezone", etc_timezone_detail

    try:
        real = os.path.realpath(ETC_LOCALTIME_PATH)
    except OSError as error:
        real = ""
        localtime_detail = f"{ETC_LOCALTIME_PATH} unreadable ({error})"
    else:
        marker = "zoneinfo/"
        idx = real.find(marker)
        if idx == -1:
            localtime_detail = (
                f"{ETC_LOCALTIME_PATH} resolved to {real!r}, not inside a zoneinfo tree"
            )
            real = ""
        else:
            real = real[idx + len(marker) :]
            localtime_detail = f"{ETC_LOCALTIME_PATH} resolved to zone {real!r}"
    candidate = _validated(real)
    if candidate is not None:
        return candidate, "/etc/localtime", localtime_detail

    timedatectl_path = shutil.which("timedatectl")
    if timedatectl_path is None:
        timedatectl_detail = "timedatectl not found on PATH"
        timedatectl_output = None
    else:
        try:
            completed = subprocess.run(
                [timedatectl_path, "show", "-p", "Timezone", "--value"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            timedatectl_output = completed.stdout
            timedatectl_detail = f"timedatectl reported {timedatectl_output.strip()!r}"
        except (OSError, subprocess.SubprocessError) as error:
            timedatectl_output = None
            timedatectl_detail = f"timedatectl invocation failed ({error})"
    candidate = _validated(timedatectl_output)
    if candidate is not None:
        return candidate, "timedatectl", timedatectl_detail

    return (
        None,
        "none",
        "; ".join((etc_timezone_detail, localtime_detail, timedatectl_detail)),
    )


def _read_tzif_footer(path: str) -> str | None:
    """The POSIX TZ string embedded as a version-2/3 tzfile's own

    trailing "\\n<TZ string>\\n" footer (see tzfile(5)/RFC 8536), or
    None for a version-1-only file (no footer at all) or anything that
    does not parse as a well-formed TZif file.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if not data.startswith(b"TZif") or data[4:5] not in (b"2", b"3"):
        return None
    if not data.endswith(b"\n"):
        return None
    body = data[:-1]
    newline_index = body.rfind(b"\n")
    if newline_index == -1:
        return None
    try:
        return body[newline_index + 1 :].decode("ascii")
    except UnicodeDecodeError:
        return None


def _tzdef_for_iana_name(name: str) -> tuple[str | None, str]:
    search_path = zoneinfo.TZPATH if zoneinfo is not None else ()
    for root in search_path:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        footer = _read_tzif_footer(path)
        if footer is None:
            return None, f"{path} has no readable version-2/3 POSIX TZ footer"
        if len(footer) > TZDEF_MAX_LENGTH:
            return (
                None,
                f"derived tzdef {footer!r} exceeds the {TZDEF_MAX_LENGTH}-char safety margin",
            )
        if not _SAFE_POSIX_TZDEF.match(footer):
            return (
                None,
                f"derived tzdef {footer!r} does not match the safe/verified POSIX TZ shape",
            )
        return footer, f"read from {path}'s own embedded POSIX TZ footer (tzfile v2/v3)"
    return None, f"no zoneinfo file found for {name!r} under any of {search_path!r}"
