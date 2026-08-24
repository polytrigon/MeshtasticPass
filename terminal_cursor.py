"""Tightly scoped terminal cursor visibility for the application lifecycle."""

from __future__ import annotations

import sys
from typing import TextIO


class TerminalCursor:
    """Hide and safely restore the host terminal cursor."""

    HIDE = "\x1b[?25l"
    SHOW = "\x1b[?25h"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.__stdout__
        self._hidden = False

    def hide(self) -> None:
        if self._hidden:
            return
        self._hidden = True
        self._write(self.HIDE)

    def restore(self) -> None:
        if not self._hidden:
            return
        self._write(self.SHOW)
        self._hidden = False

    def _write(self, sequence: str) -> None:
        if not self._stream.isatty():
            return
        self._stream.write(sequence)
        self._stream.flush()
