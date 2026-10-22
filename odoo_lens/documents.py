# Columns are UTF-16 code units everywhere, lines 0-based. ast/regex UTF-8 byte columns go through
# extract.common.SrcIndex (whole source) or bytecol_to_utf16 (single line).
from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from typing import Any, Iterable

# Mirrors Node's url.pathToFileURL(): chars outside this set are %-encoded as UTF-8 ('#' -> %23).
_URI_UNSAFE = re.compile(r"[^A-Za-z0-9\-._/@:;=&!$,+()*']")


def _pct(match: re.Match) -> str:
    return "".join("%%%02X" % b for b in match.group().encode("utf-8"))


def path_to_uri(path: str) -> str:
    return "file://" + _URI_UNSAFE.sub(_pct, os.path.abspath(path))


_PCT = re.compile(r"(?:%[0-9A-Fa-f]{2})+")


def _unpct(match: re.Match) -> str:
    raw = match.group()
    return bytes(int(raw[i + 1 : i + 3], 16) for i in range(0, len(raw), 3)).decode("utf-8", "surrogateescape")


def uri_to_path(uri: str) -> str:
    if not uri.startswith("file:"):
        return uri
    rest = uri[5:]
    if rest.startswith("//"):
        rest = rest[2:]
        slash = rest.find("/")
        rest = rest[slash:] if slash >= 0 else "/"
    path = _PCT.sub(_unpct, rest)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return os.path.normpath(path)


def utf16_len(s: str) -> int:
    if s.isascii():
        return len(s)
    return len(s.encode("utf-16-le")) // 2


def utf16_to_index(line: str, col16: int) -> int:
    if col16 <= 0:
        return 0
    if line.isascii():
        return min(col16, len(line))
    count = 0
    for i, ch in enumerate(line):
        if count >= col16:
            return i
        count += 2 if ord(ch) > 0xFFFF else 1
    return len(line)


def index_to_utf16(line: str, index: int) -> int:
    return utf16_len(line[:index])


def bytecol_to_utf16(line: bytes, bcol: int) -> int:
    if bcol <= 0:
        return 0
    if line.isascii():
        return bcol
    return utf16_len(line[:bcol].decode("utf-8", "replace"))


def utf16_to_bytecol(line: bytes, col16: int) -> int:
    if line.isascii():
        return min(col16, len(line))
    text = line.decode("utf-8", "replace")
    return len(text[: utf16_to_index(text, col16)].encode("utf-8"))


def convert_col(line: str, col: int, src: str, dst: str) -> int:
    if src == dst or line.isascii():
        return col
    if src == "utf-16":
        idx = utf16_to_index(line, col)
    elif src == "utf-8":
        idx = len(line.encode("utf-8")[:col].decode("utf-8", "ignore"))
    else:
        idx = min(col, len(line))
    if dst == "utf-16":
        return utf16_len(line[:idx])
    if dst == "utf-8":
        return len(line[:idx].encode("utf-8"))
    return idx


# Same line numbers as LSP for '\n' and '\r\n'; a trailing '\r' stays on the line.
def split_lines(text: str) -> list[str]:
    return text.split("\n")


def offset_at(text: str, line: int, col16: int) -> int:
    if line < 0:
        return 0
    start = 0
    for _ in range(line):
        nl = text.find("\n", start)
        if nl < 0:
            return len(text)
        start = nl + 1
    end = text.find("\n", start)
    if end < 0:
        end = len(text)
    return start + utf16_to_index(text[start:end], col16)


def apply_change(text: str, change: dict) -> str:
    rng = change.get("range")
    if not rng:
        return change["text"]
    s = offset_at(text, rng["start"]["line"], rng["start"]["character"])
    e = offset_at(text, rng["end"]["line"], rng["end"]["character"])
    if e < s:
        s, e = e, s
    return text[:s] + change["text"] + text[e:]


_REVISION = [0]


def _next_rev() -> int:
    _REVISION[0] += 1
    return _REVISION[0]


class Document:
    __slots__ = ("uri", "path", "text", "version", "language_id", "_lines", "disk_stat", "rev")

    def __init__(self, uri: str, text: str, version: int | None, language_id: str = ""):
        self.uri = uri
        self.path = uri_to_path(uri)
        self.text = text
        self.version = version
        self.language_id = language_id
        self._lines: list[str] | None = None
        self.disk_stat = _stat(self.path)
        self.rev = _next_rev()  # server-side: client versions can collide after a disk reload

    @property
    def lines(self) -> list[str]:
        if self._lines is None:
            self._lines = split_lines(self.text)
        return self._lines

    def set_text(self, text: str, version: int | None) -> None:
        self.text = text
        self.version = version
        self._lines = None
        self.disk_stat = _stat(self.path)
        self.rev = _next_rev()

    def source(self) -> bytes:
        return self.text.encode("utf-8", "surrogateescape")


class DocumentStore:
    def __init__(self, disk_cache_size: int = 96):
        self._docs: dict[str, Document] = {}
        self._disk: OrderedDict[str, tuple[int, int, str, list[str] | None]] = OrderedDict()
        self._disk_size = disk_cache_size
        self._lock = threading.Lock()

    def open(self, uri: str, text: str, version: int | None, language_id: str = "") -> Document:
        doc = Document(uri, text, version, language_id)
        self._docs[doc.path] = doc
        return doc

    def change(self, uri: str, version: int | None, changes: Iterable[dict]) -> Document | None:
        doc = self._docs.get(uri_to_path(uri))
        if doc is None:
            return None
        text = doc.text
        for change in changes:
            text = apply_change(text, change)
        doc.set_text(text, version)
        return doc

    def close(self, uri: str) -> Document | None:
        return self._docs.pop(uri_to_path(uri), None)

    # Claude Code sends no didClose/didChangeWatchedFiles: Bash/git edits would stay stale otherwise.
    def refresh_from_disk(self, path: str) -> bool:
        doc = self._docs.get(path)
        if doc is None:
            return False
        st = _stat(path)
        if st is None or st == doc.disk_stat:
            return False
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            return False
        if text != doc.text:
            doc.set_text(text, doc.version)  # only the client may advance its version
            return True
        doc.disk_stat = st
        return False

    def get(self, path: str) -> Document | None:
        return self._docs.get(path)

    def is_open(self, path: str) -> bool:
        return path in self._docs

    def open_paths(self) -> list[str]:
        return list(self._docs)

    def uri_for(self, path: str) -> str:
        doc = self._docs.get(path)
        return doc.uri if doc is not None else path_to_uri(path)

    def text(self, path: str) -> str | None:
        doc = self._docs.get(path)
        if doc is not None:
            return doc.text
        entry = self._disk_entry(path)
        return entry[2] if entry is not None else None

    def source(self, path: str) -> bytes | None:
        doc = self._docs.get(path)
        if doc is not None:
            return doc.source()
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def lines(self, path: str) -> list[str] | None:
        doc = self._docs.get(path)
        if doc is not None:
            return doc.lines
        entry = self._disk_entry(path)
        if entry is None:
            return None
        if entry[3] is None:
            lines = split_lines(entry[2])
            with self._lock:
                self._disk[path] = (entry[0], entry[1], entry[2], lines)
            return lines
        return entry[3]

    def line(self, path: str, line: int) -> str:
        lines = self.lines(path)
        if lines is None or not 0 <= line < len(lines):
            return ""
        return lines[line]

    def _disk_entry(self, path: str) -> tuple[int, int, str, list[str] | None] | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        with self._lock:
            entry = self._disk.get(path)
            if entry is not None and entry[0] == st.st_mtime_ns and entry[1] == st.st_size:
                self._disk.move_to_end(path)
                return entry
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            return None
        entry = (st.st_mtime_ns, st.st_size, text, None)
        with self._lock:
            self._disk[path] = entry
            while len(self._disk) > self._disk_size:
                self._disk.popitem(last=False)
        return entry


def _stat(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def lsp_range(line: int, col: int, end_line: int, end_col: int) -> dict[str, Any]:
    return {"start": {"line": line, "character": col}, "end": {"line": end_line, "character": end_col}}


def lsp_location(uri: str, line: int, col: int, end_line: int, end_col: int) -> dict[str, Any]:
    return {"uri": uri, "range": lsp_range(line, col, end_line, end_col)}
