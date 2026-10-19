"""Helpers shared by the extractors: byte offsets -> (line, UTF-16 column)."""

from __future__ import annotations

import re
from bisect import bisect_right

_NL = re.compile(b"\n")


class SrcIndex:
    """Maps byte offsets of a source to 0-based lines and UTF-16 columns."""

    __slots__ = ("src", "lines", "ascii", "_nl")

    def __init__(self, src: bytes):
        self.src = src
        self.lines = src.split(b"\n")
        self.ascii = src.isascii()
        self._nl: list[int] | None = None

    def col(self, line0: int, bcol: int) -> int:
        """Byte column on ``line0`` -> UTF-16 column."""
        if self.ascii or bcol <= 0:
            return bcol
        try:
            b = self.lines[line0]
        except IndexError:
            return bcol
        if b.isascii():
            return bcol
        return len(b[:bcol].decode("utf-8", "replace").encode("utf-16-le")) // 2

    def pos(self, offset: int) -> tuple[int, int]:
        """Byte offset -> (line0, byte column)."""
        if self._nl is None:
            self._nl = [m.start() for m in _NL.finditer(self.src)]
        line = bisect_right(self._nl, offset - 1)
        start = self._nl[line - 1] + 1 if line > 0 else 0
        return line, offset - start

    def span(self, start: int, end: int) -> tuple[int, int, int]:
        """Byte range [start, end) on one line -> (line0, col16, end_col16)."""
        line, b0 = self.pos(start)
        return line, self.col(line, b0), self.col(line, b0 + (end - start))

    def line_start(self, line0: int) -> int:
        if line0 <= 0:
            return 0
        if self._nl is None:
            self._nl = [m.start() for m in _NL.finditer(self.src)]
        return self._nl[line0 - 1] + 1 if line0 - 1 < len(self._nl) else len(self.src)
