"""The receive trace has to survive being run under the TUI.

rx_debug_log started as a bare print(), which is fine for
receive_messages.py in a plain terminal and useless for the one session
whose decisions actually matter: the real app. Textual owns the
terminal and redirects stdout, so a packet the running app quietly
classified as a duplicate left no evidence anywhere.

These tests pin the file sink that fixes that, and -- just as
importantly -- pin that a diagnostic can never take the radio down:
an unwritable path is a silent no-op, not an exception thrown from
inside the receive callback.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from radio_service import (  # noqa: E402
    RX_DEBUG_FILE_ENV_VAR,
    rx_debug_log,
)


@contextlib.contextmanager
def _sink(path: str | None):
    """Point the trace at `path` (or nowhere) for the duration."""
    previous = os.environ.get(RX_DEBUG_FILE_ENV_VAR)
    if path is None:
        os.environ.pop(RX_DEBUG_FILE_ENV_VAR, None)
    else:
        os.environ[RX_DEBUG_FILE_ENV_VAR] = path
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(RX_DEBUG_FILE_ENV_VAR, None)
        else:
            os.environ[RX_DEBUG_FILE_ENV_VAR] = previous


@contextlib.contextmanager
def _captured_stdout():
    buffer = io.StringIO()
    original = sys.stdout
    sys.stdout = buffer
    try:
        yield buffer
    finally:
        sys.stdout = original


class RxDebugLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    # ---- the terminal behaviour that already existed ------------------

    def test_printing_is_unchanged_when_no_file_is_named(self) -> None:
        """receive_messages.py in a plain terminal must keep working."""
        with _sink(None), _captured_stdout() as out:
            rx_debug_log("!abcd1234 TEXT_MESSAGE_APP accepted")

        self.assertEqual(
            out.getvalue().strip(), "[RX] !abcd1234 TEXT_MESSAGE_APP accepted"
        )

    def test_naming_a_file_does_not_stop_the_printing(self) -> None:
        """The two sinks are additive; one must not replace the other."""
        target = self.root / "rx.log"
        with _sink(str(target)), _captured_stdout() as out:
            rx_debug_log("hello")

        self.assertIn("[RX] hello", out.getvalue())

    # ---- the file sink ------------------------------------------------

    def test_nothing_is_written_when_no_file_is_named(self) -> None:
        """The default has to stay zero-cost and zero-footprint."""
        stray = self.root / "rx.log"
        with _sink(None), _captured_stdout():
            rx_debug_log("hello")

        self.assertFalse(stray.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_the_named_file_receives_the_line(self) -> None:
        target = self.root / "rx.log"
        with _sink(str(target)), _captured_stdout():
            rx_debug_log("CHAT STORE id=7 channel=0 inserted")

        self.assertIn("[RX] CHAT STORE id=7 channel=0 inserted", target.read_text())

    def test_lines_accumulate_rather_than_overwrite(self) -> None:
        """A trace kept for hours is the whole point.

        Opening with "w" instead of "a" would leave exactly one line --
        and it would look like a working log right up until the moment
        it was needed.
        """
        target = self.root / "rx.log"
        with _sink(str(target)), _captured_stdout():
            rx_debug_log("first")
            rx_debug_log("second")
            rx_debug_log("third")

        lines = target.read_text().strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].endswith("[RX] first"))
        self.assertTrue(lines[2].endswith("[RX] third"))

    def test_each_line_carries_a_timestamp(self) -> None:
        """Without a clock the trace cannot be aligned to the LCD."""
        target = self.root / "rx.log"
        with _sink(str(target)), _captured_stdout():
            rx_debug_log("hello")

        stamp = target.read_text().split(" ", 1)[0]
        hours, minutes, seconds = stamp.split(":")
        self.assertEqual((len(hours), len(minutes), len(seconds)), (2, 2, 2))
        self.assertTrue(0 <= int(hours) <= 23)
        self.assertTrue(0 <= int(minutes) <= 59)
        self.assertTrue(0 <= int(seconds) <= 60)

    def test_an_empty_or_blank_variable_means_no_file(self) -> None:
        """`MESHTASTICPASS_RX_DEBUG_FILE=` must not create a file named ''."""
        for value in ("", "   "):
            with self.subTest(value=repr(value)):
                with _sink(value), _captured_stdout():
                    rx_debug_log("hello")

                self.assertEqual(list(self.root.iterdir()), [])

    def test_surrounding_whitespace_in_the_path_is_ignored(self) -> None:
        target = self.root / "rx.log"
        with _sink(f"  {target}  "), _captured_stdout():
            rx_debug_log("hello")

        self.assertIn("[RX] hello", target.read_text())

    # ---- the diagnostic must never be able to break the radio ---------

    def test_an_unwritable_path_is_survived_silently(self) -> None:
        """This runs inside the pubsub receive callback.

        RadioService swallows handler exceptions, but rx_debug_log is
        called *above* that guard in _on_text_received -- an OSError
        here would abort processing of the very packet being traced.
        Turning the diagnostic on must never change what the app
        receives.
        """
        unwritable = self.root / "no-such-directory" / "rx.log"
        with _sink(str(unwritable)), _captured_stdout() as out:
            rx_debug_log("hello")  # must not raise

        self.assertIn("[RX] hello", out.getvalue())
        self.assertFalse(unwritable.exists())

    def test_a_directory_given_as_the_path_is_survived_silently(self) -> None:
        directory = self.root / "somewhere"
        directory.mkdir()
        with _sink(str(directory)), _captured_stdout() as out:
            rx_debug_log("hello")  # must not raise

        self.assertIn("[RX] hello", out.getvalue())

    def test_non_ascii_message_text_is_written(self) -> None:
        """Real traffic on this mesh is full of emoji."""
        target = self.root / "rx.log"
        with _sink(str(target)), _captured_stdout():
            rx_debug_log("TEXT 😭 from !8fa71544")

        self.assertIn("😭", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
