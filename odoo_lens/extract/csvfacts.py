"""CSV data files (``ir.model.access.csv``, Odoo 20 ``ir.access.csv``, any
``<model>.csv`` data file): one record per row with an ``id`` column, xml id
references from ``<field>:id`` / ``<field>/id`` columns, model names from
``model_id`` columns holding technical names (Odoo 20), field paths of
``domain`` columns, and header cells as field references of the file's model."""

from __future__ import annotations

import os
import re
import sys

from ..facts import (
    CSV_MODEL,
    ERROR,
    KIND,
    KIND_CSV,
    RECORDS,
    REF_FIELD,
    REF_MODEL,
    REF_XMLID,
    REFS,
)
from .common import SrcIndex

MODEL_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
DOMAIN_FIELD_RE = re.compile(r"""[(\[]\s*(['"])([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*)\1\s*,""")


def _quoted_row(src: bytes, i: int) -> tuple[list[tuple[str, int, int]], int]:
    """One CSV row starting at ``i`` with RFC 4180 quoting (cells may span lines).
    Returns the cells as (value, byte_start, byte_end) of the content, and the
    offset of the next row."""
    n = len(src)
    row: list[tuple[str, int, int]] = []
    while True:
        if src[i : i + 1] == b'"':
            j = i + 1
            buf = bytearray()
            while j < n:
                c = src[j : j + 1]
                if c == b'"':
                    if src[j + 1 : j + 2] == b'"':
                        buf += b'"'
                        j += 2
                        continue
                    break
                buf += c
                j += 1
            row.append((bytes(buf).decode("utf-8", "replace"), i + 1, j))
            i = j + 1
            while i < n and src[i : i + 1] not in (b",", b"\n"):  # junk after the closing quote
                i += 1
        else:
            j = i
            while j < n and src[j : j + 1] not in (b",", b"\n"):
                j += 1
            end = j - 1 if j > i and src[j - 1 : j] == b"\r" else j
            row.append((src[i:end].decode("utf-8", "replace"), i, end))
            i = j
        if i < n and src[i : i + 1] == b",":
            i += 1
            continue
        return row, (i + 1 if i < n else n)


def _cells(src: bytes):
    """Yield rows as lists of (value, byte_start, byte_end) of each cell content.

    Lines without any double quote (nearly all of them) are split with C-speed
    bytes.split; rows with quotes go through the RFC 4180 state machine."""
    n = len(src)
    i = 0
    while i < n:
        nl = src.find(b"\n", i)
        end = n if nl < 0 else nl
        if src.find(b'"', i, end) < 0:
            stop = end - 1 if end > i and src[end - 1 : end] == b"\r" else end
            row = []
            pos = i
            for part in src[i:stop].split(b","):
                row.append((part.decode("utf-8", "replace"), pos, pos + len(part)))
                pos += len(part) + 1
            yield row
            i = end + 1
        else:
            row, i = _quoted_row(src, i)
            yield row


BIG_CSV_ROWS = 2000
ACL_MODELS = ("ir.model.access", "ir.access", "ir.rule")


def extract(path: str, src: bytes, index: bool = False) -> dict:
    model = os.path.basename(path)[:-4].split("-", 1)[0]  # "res.country.state-xx.csv" -> res.country.state
    facts: dict = {KIND: KIND_CSV, ERROR: None, CSV_MODEL: model, RECORDS: [], REFS: []}
    norm = path.replace("\\", "/")
    if index and "/data/template/" in norm:
        return facts  # l10n chart templates (17.0+): not real xml ids, huge
    big = index and model not in ACL_MODELS and src.count(b"\n") > BIG_CSV_ROWS
    if src.startswith(b"\xef\xbb\xbf"):
        src = b"   " + src[3:]  # keep offsets, drop the BOM
    s = SrcIndex(src)
    rows = _cells(src)
    try:
        header = next(rows)
    except StopIteration:
        return facts
    cols = [h[0].strip() for h in header]
    refs = facts[REFS]
    records = facts[RECORDS]
    # header cells: field names of the file's model
    for (value, b0, b1), name in zip(header, cols):
        fname = re.split(r"[:/]", name, 1)[0]
        if fname and fname not in ("id",) and re.match(r"^[a-z_][a-z0-9_]*$", fname):
            line, c0, c1 = s.span(b0, b0 + len(fname.encode()))
            refs.append((REF_FIELD, fname, line, c0, c1, ("csv_header", model)))
    id_idx = cols.index("id") if "id" in cols else -1
    xid_cols = {i: c for i, c in enumerate(cols) if c.endswith((":id", "/id")) and c not in (":id", "/id")}
    model_name_cols = {i for i, c in enumerate(cols) if c in ("model_id", "model", "res_model")}
    domain_cols = {i for i, c in enumerate(cols) if c in ("domain", "domain_force")}
    name_idx = cols.index("name") if "name" in cols else -1
    intern = sys.intern
    for row in rows:
        if not row or all(not v.strip() for v, _, _ in row):
            continue
        if big:  # huge data file (cities, counties...): ids only
            if 0 <= id_idx < len(row) and row[id_idx][0].strip():
                value, b0, b1 = row[id_idx]
                line, c0, c1 = s.span(b0, b1)
                records.append((intern(value.strip()), model, "csv", line, c0, c1, line, None))
            continue
        row_model = None
        for i in model_name_cols:
            if i < len(row) and MODEL_RE.match(row[i][0].strip()):
                row_model = row[i][0].strip()
                line, c0, c1 = s.span(row[i][1], row[i][2])
                refs.append((REF_MODEL, row_model, line, c0, c1, ("csv",)))
        for i, col in xid_cols.items():
            if i >= len(row):
                continue
            value, b0, b1 = row[i]
            pos = 0
            for part in value.split(","):
                item = part.strip()
                if item:
                    lead = len(part) - len(part.lstrip())
                    start = b0 + len(value[: pos + lead].encode())
                    line, c0, c1 = s.span(start, start + len(item.encode()))
                    refs.append((REF_XMLID, intern(item), line, c0, c1, (col,)))
                    if row_model is None and col.startswith("model_id") and item.split(".")[-1].startswith("model_"):
                        pass
                pos += len(part) + 1
        for i in domain_cols:
            if i >= len(row):
                continue
            value, b0, b1 = row[i]
            raw = src[b0:b1].decode("utf-8", "replace")
            if raw != value:
                continue  # escaped quotes inside: positions would be wrong
            for m in DOMAIN_FIELD_RE.finditer(value):
                start = b0 + len(value[: m.start(2)].encode())
                line, c0, c1 = s.span(start, start + len(m.group(2).encode()))
                refs.append((REF_FIELD, m.group(2), line, c0, c1, ("domain", row_model)))
        if 0 <= id_idx < len(row):
            value, b0, b1 = row[id_idx]
            xid = value.strip()
            if xid:
                line, c0, c1 = s.span(b0, b1)
                info = {}
                if 0 <= name_idx < len(row):
                    info["name"] = row[name_idx][0]
                if row_model:
                    info["res_model"] = row_model
                for i, col in xid_cols.items():
                    if i < len(row) and row[i][0].strip():
                        info[col.split(":")[0].split("/")[0]] = row[i][0].strip()
                records.append((intern(xid), model, "csv", line, c0, c1, line, info))
    return facts
