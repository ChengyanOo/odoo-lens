"""CSV analysis (``ir.model.access.csv``, 19.4+ ``ir.access.csv``, ``<model>.csv``
data files): targets under the cursor.

* ``id`` cells: the record's xml id (bare ids belong to the current module);
* ``<field>:id`` / ``<field>/id`` cells (comma-separated for many2many): xml ids;
* ``model_id`` cells holding a dotted model name (19.4+ ``ir.access.csv``): models;
* header cells: fields of the file's model (``ir.model.access``, ``ir.access``...);
* ``domain`` cells: the field-path segment under the cursor, on the row's model
  (the ``model_id`` cell: a dotted name, or an implicit ``model_x`` xml id).
"""

from __future__ import annotations

from ..documents import utf16_to_index

from typing import Any

from ..facts import (
    CSV_MODEL,
    R_COL,
    R_CTX,
    R_END_COL,
    R_KIND,
    R_LINE,
    R_VALUE,
    RECORDS,
    REF_FIELD,
    REF_MODEL,
    REF_XMLID,
    REFS,
    X_COL,
    X_END_COL,
    X_ID,
    X_LINE,
    X_MODEL,
)
from .targets import FIELD, MODEL, XMLID, Target
from .xml import nearest, segment_target

_PRIO = {REF_MODEL: 0, REF_FIELD: 1, REF_XMLID: 2}
_RECORD_PRIO = 3


def targets_at(ctx: Any) -> list[Target]:
    facts = ctx.facts
    if not facts:
        return []
    line = ctx.line
    cands = []
    for ref in facts.get(REFS) or ():
        if ref[R_LINE] == line:
            cands.append((ref[R_COL], ref[R_END_COL], _PRIO.get(ref[R_KIND], 4), ("ref", ref)))
    for rec in facts.get(RECORDS) or ():
        if rec[X_LINE] == line and rec[X_ID]:
            cands.append((rec[X_COL], rec[X_END_COL], _RECORD_PRIO, ("record", rec)))
    text = ctx.server.documents.line(ctx.path, line)
    if text and not any(c0 <= ctx.col <= c1 for c0, c1, _p, _x in cands):
        idx = utf16_to_index(text, ctx.col)
        here, prev = text[idx : idx + 1], text[idx - 1 : idx] if idx else ""
        if (here.isalnum() or here == "_") or (prev.isalnum() or prev == "_"):
            return []  # a cell token no ref covers: never trade it for a neighbouring cell
    hit = nearest(cands, ctx.col)
    if hit is None:
        return []
    what, item = hit
    if what == "record":
        return [_record_target(ctx, item)]
    return _ref_targets(ctx, facts, item)


def _record_target(ctx: Any, rec: tuple) -> Target:
    xmlid = ctx.registry.qualify_xmlid(rec[X_ID], ctx.module)
    return Target(XMLID, xmlid=xmlid, origin=(rec[X_LINE], rec[X_COL], rec[X_END_COL]), extra={"role": "id", "model": rec[X_MODEL]})


def _ref_targets(ctx: Any, facts: dict, ref: tuple) -> list[Target]:
    registry = ctx.registry
    kind = ref[R_KIND]
    role = ref[R_CTX][0]
    value = ref[R_VALUE]
    origin = (ref[R_LINE], ref[R_COL], ref[R_END_COL])
    if kind == REF_XMLID:
        xmlid = registry.qualify_xmlid(value, ctx.module)
        return [Target(XMLID, xmlid=xmlid, origin=origin, extra={"role": role, "raw": value})]
    if kind == REF_MODEL:
        return [Target(MODEL, model=value, origin=origin, extra={"role": role})]
    if kind != REF_FIELD:
        return []
    if role == "csv_header":
        model = ref[R_CTX][1] if len(ref[R_CTX]) > 1 else facts.get(CSV_MODEL)
        return [Target(FIELD, model=model, name=value, origin=origin, extra={"role": role})] if model else []
    if role == "domain":
        model = ref[R_CTX][1] if len(ref[R_CTX]) > 1 and ref[R_CTX][1] else row_model(registry, ctx.module, facts, ref[R_LINE])
        if model is None:
            return []
        target = segment_target(registry, model, ref, ctx.col, ctx.scope, {"role": role})
        return [target] if target is not None else []
    return []


def row_model(registry: Any, module: str | None, facts: dict, line: int) -> str | None:
    """Model a CSV row applies to: its ``model_id`` cell (dotted name, or the
    implicit ``model_x`` xml id of a model)."""
    for ref in facts.get(REFS) or ():
        if ref[R_LINE] != line:
            continue
        if ref[R_KIND] == REF_MODEL:
            return ref[R_VALUE]
        if ref[R_KIND] == REF_XMLID and ref[R_CTX][0].startswith("model_id"):
            for rec in registry.records(registry.qualify_xmlid(ref[R_VALUE], module)):
                model = rec.info.get("model")
                if model:
                    return model
    return None


__all__ = ["targets_at", "row_model"]
