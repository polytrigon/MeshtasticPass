"""Tests for tightly scoped host-terminal cursor visibility."""

from __future__ import annotations

from io import StringIO
import unittest

from terminal_cursor import TerminalCursor


class TtyBuffer(StringIO):
    def isatty(self) -> bool:
        return True


class TerminalCursorTests(unittest.TestCase):
    def test_hide_and_restore_are_idempotent(self) -> None:
        stream = TtyBuffer()
        cursor = TerminalCursor(stream)

        cursor.hide()
        cursor.hide()
        cursor.restore()
        cursor.restore()

        self.assertEqual(stream.getvalue(), TerminalCursor.HIDE + TerminalCursor.SHOW)

    def test_non_terminal_stream_is_safe(self) -> None:
        stream = StringIO()
        cursor = TerminalCursor(stream)

        cursor.hide()
        cursor.restore()

        self.assertEqual(stream.getvalue(), "")
