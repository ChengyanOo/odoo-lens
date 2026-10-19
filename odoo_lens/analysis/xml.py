"""XML analysis: targets under the cursor in data files, views, server QWeb and
owl templates, plus the model context of view-arch references.

The cursor is matched against the refs and record ids of ``ctx.facts`` (a full
extraction of the current text); XML is never re-parsed here.

Model of a view-arch reference (``arch_ref_model`` / ``arch_ref_candidates``,
also used by references and diagnostics):

* the root model is the view model of the record (``<field name="model">``,
  else the ``inherit_id`` chain), walked through the x2many fields of the ref's
  *subpath* (sub-views ``<field name="line_ids"><list>...``);
* inheritance specs (elements with ``position=``) of inherited views: Odoo
  locates ``<field name="X">`` on the FIRST field X anywhere in the combined
  parent arch - possibly inside an x2many sub-view - and an xpath on its path
  steps; the content inserted around the located node belongs to that node's
  model. When the extractor's guess (spec at the arch root, ``field[@name]``
  xpath steps descending only into explicit sub-view steps) does not own the
  anchor, the anchor is looked up in the arch of the parent view chain, then of
  the sibling extensions in scope, and the spec content is re-rooted on the
  model found there. ``a//b`` and ``field[@name='x']/list`` xpath steps descend
  into the comodel of ``x``; a sub-view step not reached through a field step
  makes the content model unknown (candidates then include the sub-view models
  of the parent arch). Odoo ignores everything but ``<attribute>`` inside a
  ``position="attributes"`` spec, and a ``<button name>`` spec may locate an
  action/special button: such refs get no model.

The element nesting needed to find the spec enclosing a ref is not part of the
facts: it is recovered with a cheap tag scan of the record's lines (the current
text, else the file on disk when it still matches the facts).
"""

from __future__ import annotations

import os
import re
import weakref
from collections import OrderedDict
from typing import Any, Iterable

from ..documents import utf16_to_index
from ..facts import (
    R_COL,
    R_CTX,
    R_END_COL,
    R_KIND,
    R_LINE,
    R_VALUE,
    RECORDS,
    REF_FIELD,
    REF_METHOD,
    REF_MODEL,
    REF_QWEB,
    REF_XMLID,
    REFS,
    X_COL,
    X_END_COL,
    X_END_LINE,
    X_ID,
    X_INFO,
    X_LINE,
    X_MODEL,
    X_TAG,
)
from ..registry import RecordRef
from .targets import FIELD, METHOD, MODEL, TEMPLATE, XMLID, Target

NEAR = 25  # max distance (UTF-16 columns) between the cursor and a token picked leniently
ARCH_ROLES = frozenset({"arch", "label", "expr", "button"})
# refs whose model comes from their record (ir.rule domains, cron code) or from a view
MODEL_ROLES = frozenset({"record_domain", "code", "arch_domain"})
SUBVIEW_TAGS = frozenset(
    {"form", "list", "tree", "kanban", "graph", "pivot", "calendar", "gantt", "activity", "cohort", "map", "grid", "hierarchy", "search"}
)
MAX_NAME_CANDIDATES = 8  # name-based fallback when the view model is unknown
_CHAIN_DEPTH = 12  # inherit_id hops followed
_NESTING = 3  # parent refs resolved recursively (their own specs may need their parents)

# tie-break priority of tokens at the same distance from the cursor (lower wins)
_PRIO = {REF_MODEL: 0, REF_FIELD: 1, REF_METHOD: 1, REF_XMLID: 2, REF_QWEB: 2}
_RECORD_PRIO = 3  # refs win over record ids
_PARENT = "parent."  # attrs domains (16.0): field of the record of the enclosing x2many level

_TAG_RE = re.compile(
    r"""<!--.*?-->|<!\[CDATA\[.*?\]\]>|<[?!][^>]*>|<(/?)([A-Za-z_][\w:.\-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>""",
    re.S,
)
_ATTR_RE = re.compile(r"""([\w:.\-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
# same patterns as the extractor (refs of xpath field steps are emitted per match)
_XPATH_FIELD_RE = re.compile(r"""field\[@name\s*=\s*(['"])([A-Za-z0-9_]+)\1\]""")
_XPATH_EXT_STEP_RE = re.compile(r"""/+([A-Za-z_][\w\-]*)""")
_XPATH_STEP_RE = re.compile(r"""(/+)(\.\.?|\*|[A-Za-z_][\w\-]*)""")


# ---------------------------------------------------------------------------
# cursor -> token


def nearest(cands: Iterable[tuple[int, int, int, Any]], col: int, near: int = NEAR) -> Any:
    """Payload of the candidate ``(col, end_col, priority, payload)`` under or
    nearest to ``col`` (within ``near`` columns); ties go to the lowest priority."""
    best = None
    best_key: tuple | None = None
    for c0, c1, prio, payload in cands:
        if c0 <= col <= c1:
            d = 0
        elif col < c0:
            d = c0 - col
        else:
            d = col - c1
        if d > near:
            continue
        key = (d, prio, c1 - c0)
        if best_key is None or key < best_key:
            best, best_key = payload, key
    return best


def path_segment(value: str, col: int, cursor: int) -> tuple[int, int, int]:
    """(index, start col, end col) of the segment of dotted ``value`` (starting at
    ``col``) under ``cursor``; a cursor outside the value picks the nearest end."""
    start = col
    segs = value.split(".")
    for i, seg in enumerate(segs):
        end = start + len(seg)
        if cursor <= end or i == len(segs) - 1:
            return i, start, end
        start = end + 1
    return 0, col, col + len(value)


def segment_target(registry: Any, model: str | None, ref: tuple, cursor: int, scope: frozenset | None, extra: dict) -> Target | None:
    """FIELD target for the segment of the dotted field path of ``ref`` (first
    segment on ``model``) under ``cursor``; None when the segment's model is
    unknown (an earlier segment does not resolve)."""
    value = ref[R_VALUE]
    i, c0, c1 = path_segment(value, ref[R_COL], cursor)
    segs = value.split(".")
    owner = _walk(registry, scope, model, segs[:i]) if model else None
    if owner is None:
        return None
    if i:
        extra = dict(extra, path=value)
    return Target(FIELD, model=owner, name=segs[i], origin=(ref[R_LINE], c0, c1), extra=extra)


def _pick(facts: dict, line: int, col: int, text: str = "") -> tuple[str, Any] | None:
    cands = []
    for ref in facts.get(REFS) or ():
        if ref[R_LINE] == line:
            cands.append((ref[R_COL], ref[R_END_COL], _PRIO.get(ref[R_KIND], 4), ("ref", ref)))
    for i, rec in enumerate(facts.get(RECORDS) or ()):
        if rec[X_LINE] == line and rec[X_ID]:
            cands.append((rec[X_COL], rec[X_END_COL], _RECORD_PRIO, ("record", i)))
    if text and not any(c0 <= col <= c1 for c0, c1, _p, _x in cands) and on_unref_identifier(text, col):
        return None  # an identifier no ref covers (domain, code, expression): never trade it for a neighbour
    return nearest(cands, col)


_SYNTAX_WORDS = frozenset(
    {"invisible", "readonly", "required", "column_invisible", "and", "or", "not", "in", "is", "True", "False", "None", "parent", "context", "uid"}
)


def on_unref_identifier(text: str, col16: int) -> bool:
    """True if the cursor is on an identifier inside an attribute value or an
    element's text (where tokens are code: domains, expressions, cron code).
    A cursor on tag or attribute names keeps the lenient snapping (Claude often
    lands on ``name`` of ``name="partner_id"``)."""
    idx = utf16_to_index(text, col16)
    ch = text[idx : idx + 1]
    prev = text[idx - 1 : idx] if idx else ""
    if not ((ch and (ch.isalnum() or ch == "_")) or (prev and (prev.isalnum() or prev == "_"))):
        return False
    lo = idx
    while lo > 0 and (text[lo - 1].isalnum() or text[lo - 1] == "_"):
        lo -= 1
    hi = idx
    while hi < len(text) and (text[hi].isalnum() or text[hi] == "_"):
        hi += 1
    if text[lo:hi] in _SYNTAX_WORDS:
        return False  # attrs keys, operators, constants: snapping to the nearby field is fine
    in_tag = False
    quote = ""
    for c in text[:idx]:
        if quote:
            if c == quote:
                quote = ""
        elif in_tag:
            if c in "\"'":
                quote = c
            elif c == ">":
                in_tag = False
        elif c == "<":
            in_tag = True
    return bool(quote) or not in_tag


# ---------------------------------------------------------------------------
# targets


def targets_at(ctx: Any) -> list[Target]:
    facts = ctx.facts
    if not facts:
        return []
    hit = _pick(facts, ctx.line, ctx.col, ctx.server.documents.line(ctx.path, ctx.line))
    if hit is None:
        return []
    what, item = hit
    if what == "record":
        return _record_targets(ctx, facts[RECORDS][item])
    return _ref_targets(ctx, facts, item)


def _record_targets(ctx: Any, rec: tuple) -> list[Target]:
    origin = (rec[X_LINE], rec[X_COL], rec[X_END_COL])
    if rec[X_TAG] == "qweb":
        return [Target(TEMPLATE, xmlid=rec[X_ID], origin=origin, extra={"role": "t-name"})]
    xmlid = ctx.registry.qualify_xmlid(rec[X_ID], ctx.module)
    return [Target(XMLID, xmlid=xmlid, origin=origin, extra={"role": "id", "model": rec[X_MODEL]})]


def _ref_targets(ctx: Any, facts: dict, ref: tuple) -> list[Target]:
    kind = ref[R_KIND]
    value = ref[R_VALUE]
    role = ref[R_CTX][0]
    origin = (ref[R_LINE], ref[R_COL], ref[R_END_COL])
    if kind == REF_MODEL:
        return [Target(MODEL, model=value, origin=origin, extra={"role": role})]
    if kind == REF_XMLID:
        xmlid = ctx.registry.qualify_xmlid(value, ctx.module)
        return [Target(XMLID, xmlid=xmlid, origin=origin, extra={"role": role, "raw": value})]
    if kind == REF_QWEB:
        return [Target(TEMPLATE, xmlid=value, origin=origin, extra={"role": role})]
    if kind == REF_FIELD or kind == REF_METHOD:
        return _member_targets(ctx, facts, ref)
    return []


def _member_targets(ctx: Any, facts: dict, ref: tuple) -> list[Target]:
    registry = ctx.registry
    kind = ref[R_KIND]
    role = ref[R_CTX][0]
    name = ref_name(ref)
    origin = (ref[R_LINE], ref[R_COL], ref[R_END_COL])
    extra: dict[str, Any] = {"role": role}
    scope = ctx.scope
    if role in ARCH_ROLES and ref[R_VALUE].startswith(_PARENT):
        model = _parent_level(registry, ctx.path, facts, ref)
        n = len(_PARENT)
        if model is None:
            return []
        if ctx.col < ref[R_COL] + n:  # on "parent": the record of the enclosing level
            return [Target(MODEL, model=model, origin=(ref[R_LINE], ref[R_COL], ref[R_COL] + n - 1), extra={"role": "parent"})]
        rest = (ref[R_KIND], ref[R_VALUE][n:], ref[R_LINE], ref[R_COL] + n, ref[R_END_COL], ref[R_CTX])
        target = segment_target(registry, model, rest, ctx.col, scope, dict(extra, path=ref[R_VALUE]))
        return [target] if target is not None else []
    if role in ARCH_ROLES:
        cands = arch_ref_candidates(registry, ctx.path, facts, ref, ctx.source)
        model = _first_with_member(registry, cands, name, kind, scope)
        if model is None and role == "label":  # <label for> may also name a button
            owner = _first_with_member(registry, cands, name, REF_METHOD, scope)
            if owner is not None:
                return [Target(METHOD, model=owner, name=name, origin=origin, extra=extra)]
        if model is None and cands:
            model = cands[0]
        if len(cands) > 1:
            extra["candidates"] = cands
        if model is None:
            if view_root_model(registry, ctx.path, facts, ref[R_CTX][1]) is not None:
                return []  # ignored spec content, or a spec on a button we cannot locate
            models = _name_candidates(registry, name, kind, scope)
            if not models:
                return []
            if len(models) > 1:
                return [Target(FIELD if kind == REF_FIELD else METHOD, name=name, models=models, origin=origin, extra=extra)]
            model = models[0]
    else:
        model = arch_ref_model(registry, ctx.path, facts, ref)
        if model is None:
            return []
    if kind == REF_METHOD:
        return [Target(METHOD, model=model, name=name, origin=origin, extra=extra)]
    if "." in ref[R_VALUE]:
        target = segment_target(registry, model, ref, ctx.col, scope, extra)
        return [target] if target is not None else []
    return [Target(FIELD, model=model, name=name, origin=origin, extra=extra)]


def ref_name(ref: tuple) -> str:
    """Member name of a field/method ref: first path segment (after ``parent.``
    in 16.0 ``attrs`` domains), without ``@lang``."""
    value = ref[R_VALUE]
    if value.startswith(_PARENT):
        value = value[len(_PARENT) :]
    return value.split(".", 1)[0].split("@", 1)[0]


# ---------------------------------------------------------------------------
# view model context: public API


def arch_ref_model(registry: Any, path: str, facts: dict, ref: tuple, source: bytes | str | None = None) -> str | None:
    """Model owning the field/method named by ``ref``.

    ``("record", i)``: the record's model. ``("arch"|"label"|"expr"|"button", i,
    subpath)``: the first of ``arch_ref_candidates`` that has the member, else
    the best guess (so that callers can report an unknown field on it): the view
    model of record ``i`` of ``facts`` walked through ``subpath``, re-rooted for
    the content of inheritance specs located elsewhere (see the module
    docstring). ``source``: the text ``facts`` were extracted from (default: the
    file on disk, when it still matches). Also answers ``("function", model)``,
    CSV ``("csv_header", model)`` and ``("domain", model)``. None when unknown."""
    rctx = ref[R_CTX]
    role = rctx[0]
    if role in ARCH_ROLES:
        if ref[R_VALUE].startswith(_PARENT):
            return _parent_level(registry, path, facts, ref)
        cands = arch_ref_candidates(registry, path, facts, ref, source)
        if not cands:
            return None
        return _first_with_member(registry, cands, ref_name(ref), ref[R_KIND], registry.scope_for_path(path)) or cands[0]
    if role == "record":
        rec = _record(facts, rctx[1])
        return rec[X_MODEL] if rec is not None else None
    if role in ("function", "csv_header", "domain") and len(rctx) > 1:
        return rctx[1]
    if role in ("record_domain", "code"):
        rec = _record(facts, rctx[1])
        return record_target_model(registry, path, rec) if rec is not None else None
    if role == "arch_domain":
        scope = registry.scope_for_path(path)
        model = view_root_model(registry, path, facts, rctx[1])
        if model and rctx[2]:
            model = _walk(registry, scope, model, list(rctx[2]))
        if model and rctx[3]:  # <field name="x" domain=...>: the domain filters x's comodel
            model = comodel(registry, model, rctx[3], scope)
        return model
    return None


def record_target_model(registry: Any, path: str, rec: tuple) -> str | None:
    """Model a data record works on: ``res_model`` (actions) or the model of its
    ``model_id`` (ir.rule, ir.cron, ir.actions.server, ir.access)."""
    info = rec[X_INFO] or {}
    if info.get("res_model"):
        return info["res_model"]
    mid = info.get("model_id")
    if not mid:
        return None
    for r in registry.records(registry.qualify_xmlid(mid, registry.module_of_path(path))):
        model = (r.info or {}).get("model")
        if model:
            return model
    return None


def arch_ref_candidates(registry: Any, path: str, facts: dict, ref: tuple, source: bytes | str | None = None) -> list[str]:
    """Candidate models of an arch ``ref``, best guess first.

    For refs of inherited views whose name is not a member of the best guess,
    adds the models under which a field (a button for methods) with that name
    appears in the arch of the parent view chain (then of sibling extensions),
    the models where the enclosing spec's anchor appears, and - when the spec
    targets a sub-view of an unknown x2many - the sub-view models of the
    parent arch."""
    rctx = ref[R_CTX]
    if rctx[0] not in ARCH_ROLES or ref[R_VALUE].startswith(_PARENT):
        model = arch_ref_model(registry, path, facts, ref, source)
        return [model] if model else []
    best, _raw, root, spec = _resolve(registry, path, facts, ref, source, 0)
    out = [best] if best else []  # a re-rooted spec content is not on the extractor's model
    rec = _record(facts, rctx[1])
    if root is None or rec is None or not (rec[X_INFO] or {}).get("inherit_id") or best is None:
        return out
    kind = ref[R_KIND]
    name = ref_name(ref)
    scope = registry.scope_for_path(path)
    if _has_member(registry, best, name, kind, scope):
        return out
    more = list(_occurrences(registry, path, facts, rctx[1], kind, name, 0))
    if spec is not None:
        if spec.steps and (spec.kind, spec.steps[-1][0]) != (kind, name):
            anchor = spec.steps[-1][0]
            for m in _occurrences(registry, path, facts, rctx[1], spec.kind, anchor, 0):
                more.append(comodel(registry, m, anchor, scope) if spec.descends else m)
        if spec.unknown:
            more += _subview_models(registry, path, facts, rctx[1])
    for m in more:
        if m and m not in out:
            out.append(m)
    return out


def view_root_model(registry: Any, path: str, facts: dict, rec_index: int) -> str | None:
    """View model of record ``rec_index`` of ``facts`` (follows ``inherit_id``;
    for an override of another module's view, that view's model)."""
    memo = _memo(registry)
    key = ("root", path, rec_index)
    hit = memo.get(key)
    if hit is not None and hit[0] is facts:
        return hit[1]
    rec = _record(facts, rec_index)
    model = None
    if rec is not None:
        module = registry.module_of_path(path)
        xmlid = registry.qualify_xmlid(rec[X_ID], module) if rec[X_ID] else ""
        model = registry.view_model(RecordRef(xmlid, path, rec_index, module, rec))
        if model is None and xmlid:
            for other in registry.records(xmlid):
                if other.path != path or other.index != rec_index:
                    model = registry.view_model(other)
                    if model:
                        break
    memo[key] = (facts, model)
    return model


def comodel(registry: Any, model: str, name: str, scope: frozenset | None, _depth: int = 0) -> str | None:
    """Comodel of field ``model.name`` (scoped, then unscoped), following the
    ``related=`` path of related relational fields that declare no comodel."""
    co = registry.comodel(model, name, scope) if scope is not None else None
    co = co or registry.comodel(model, name)
    if co is not None or _depth > 3:
        return co
    info = (registry.field_info(model, name, scope) if scope is not None else None) or registry.field_info(model, name)
    related = info["args"].get("related") if info else None
    if not isinstance(related, str) or not related:
        return None
    cur: str | None = model
    for seg in related.split("."):
        cur = comodel(registry, cur, seg, scope, _depth + 1) if cur else None
    return cur


# ---------------------------------------------------------------------------
# helpers


def _record(facts: dict, index: int) -> tuple | None:
    recs = facts.get(RECORDS) or ()
    return recs[index] if 0 <= index < len(recs) else None


def _walk(registry: Any, scope: frozenset | None, model: str | None, subpath: Iterable[str]) -> str | None:
    for name in subpath:
        if model is None:
            return None
        model = comodel(registry, model, name, scope)
    return model


def _has_member(registry: Any, model: str, name: str, kind: str, scope: frozenset | None) -> bool:
    """``model`` has a field (method for REF_METHOD) ``name`` visible in ``scope``."""
    lookup = registry.method if kind == REF_METHOD else registry.field
    return bool(lookup(model, name, scope))


def _first_with_member(registry: Any, models: Iterable[str], name: str, kind: str, scope: frozenset | None) -> str | None:
    """First of ``models`` having the member in scope, else (a module outside the
    dependencies defines it) in the whole env."""
    models = [m for m in models if m]
    for m in models:
        if _has_member(registry, m, name, kind, scope):
            return m
    if scope is not None:
        for m in models:
            if _has_member(registry, m, name, kind, None):
                return m
    return None


def _name_candidates(registry: Any, name: str, kind: str, scope: frozenset | None) -> list[str]:
    """Models (in scope) having a member ``name``, when the view model is unknown."""
    models = [m for m in registry.models_with_member(name) if m]
    if not models or len(models) > 4 * MAX_NAME_CANDIDATES:
        return []
    lookup = registry.method if kind == REF_METHOD else registry.field
    found = [m for m in models if (scope is None or registry.has_model(m, scope)) and lookup(m, name, scope)]
    return found if len(found) <= MAX_NAME_CANDIDATES else []


# ---------------------------------------------------------------------------
# resolution of arch refs


def _parent_level(registry: Any, path: str, facts: dict, ref: tuple) -> str | None:
    """Model of the enclosing x2many level of a ``parent.x`` ref (16.0 attrs)."""
    rctx = ref[R_CTX]
    subpath = rctx[2] if len(rctx) > 2 else ()
    if not subpath:
        return None
    root = view_root_model(registry, path, facts, rctx[1])
    return _walk(registry, registry.scope_for_path(path), root, subpath[:-1]) if root else None


def _resolve(registry: Any, path: str, facts: dict, ref: tuple, source: bytes | str | None, depth: int) -> tuple:
    """(best model, extractor's model, view root model, enclosing spec or None)."""
    rctx = ref[R_CTX]
    rec_index = rctx[1]
    subpath = rctx[2] if len(rctx) > 2 else ()
    root = view_root_model(registry, path, facts, rec_index)
    if root is None:
        return None, None, None, None
    scope = registry.scope_for_path(path)
    raw = _walk(registry, scope, root, subpath)
    rec = _record(facts, rec_index)
    if rec is None or not (rec[X_INFO] or {}).get("inherit_id"):
        return raw, raw, root, None
    memo = _memo(registry)
    key = ("resolve", path, ref[R_LINE], ref[R_COL], ref[R_KIND], depth > 0)
    hit = memo.get(key)
    if hit is not None and hit[0] is facts:
        return hit[1]
    best = raw
    spec, pos = _enclosing_spec(registry, path, facts, rec_index, ref, source)
    if spec is not None:
        if spec.tstart <= pos < spec.tend:
            if ref in spec.step_refs:
                best = _step_owner(registry, path, facts, rec_index, root, scope, spec, spec.step_refs.index(ref), depth)
                if best is None and spec.kind == REF_METHOD:
                    raw = None  # it may locate an action/special button: not a method call
                best = best or raw
        elif spec.position == "attributes":
            best = raw = None  # only <attribute> children are applied: other content is ignored by Odoo
        else:
            located = _content_model(registry, path, facts, rec_index, root, scope, spec, depth)
            prefix = spec.base + spec.ext_sub
            if located is not None and tuple(subpath[: len(prefix)]) == prefix:
                best = _walk(registry, scope, located, subpath[len(prefix) :]) or raw
    res = (best, raw, root, spec)
    memo[key] = (facts, res)
    return res


def _step_owner(registry: Any, path: str, facts: dict, rec_index: int, root: str, scope: frozenset | None, spec: "_Spec", i: int, depth: int) -> str | None:
    """Model owning the node named by step ``i`` of a spec (located node for the last)."""
    name, owner_sub, unknown = spec.steps[i]
    kind = spec.kind if i == len(spec.steps) - 1 else REF_FIELD
    model = None if unknown else _walk(registry, scope, root, spec.base + owner_sub)
    if model and _has_member(registry, model, name, kind, scope):
        return model
    occ = _occurrences(registry, path, facts, rec_index, kind, name, depth)
    if unknown:  # inside a sub-view of an unknown x2many: prefer non-root occurrences
        occ = [m for m in occ if m != root] + [m for m in occ if m == root]
    return _first_with_member(registry, ([model] if model else []) + occ, name, kind, scope)


def _content_model(registry: Any, path: str, facts: dict, rec_index: int, root: str, scope: frozenset | None, spec: "_Spec", depth: int) -> str | None:
    """Model of the nodes inserted by a spec (None when unknown)."""
    if spec.content_unknown:
        return None
    if not spec.steps:
        return _walk(registry, scope, root, spec.base)
    owner = _step_owner(registry, path, facts, rec_index, root, scope, spec, len(spec.steps) - 1, depth)
    if owner is not None and spec.descends:
        return comodel(registry, owner, spec.steps[-1][0], scope)
    return owner


# ---------------------------------------------------------------------------
# parent view chain and sibling extensions


def _occurrences(registry: Any, path: str, facts: dict, rec_index: int, kind: str, name: str, depth: int) -> list[str]:
    """Models of the nodes named ``name`` (fields, or buttons for methods) in
    the arch of the parent views of a record, nearest parent first; when none
    owns ``name``, of the other extensions of those views (top-level requests)."""
    memo = _memo(registry)
    key = ("occ", path, rec_index, kind, name, depth > 0)
    hit = memo.get(key)
    if hit is not None and hit[0] is facts:
        return hit[1]
    out: list[str] = []
    memo[key] = (facts, out)  # recursion guard: a cyclic chain sees a partial result
    if depth >= _NESTING:
        return out
    scope = registry.scope_for_path(path)
    root = view_root_model(registry, path, facts, rec_index)
    chain = _parent_views(registry, path, facts, rec_index)
    _collect(registry, chain, kind, name, root, depth, out)
    if depth == 0 and _first_with_member(registry, out, name, kind, scope) is None:
        _collect(registry, _siblings(registry, path, facts, rec_index, chain), kind, name, root, depth, out)
    return out


def _collect(registry: Any, views: list[tuple[RecordRef, dict]], kind: str, name: str, root: str | None, depth: int, out: list[str]) -> None:
    for prec, pfacts in views:
        for pref in _arch_names(registry, prec.path, pfacts).get(prec.index, {}).get((kind, name), ()):
            model = _resolve(registry, prec.path, pfacts, pref, None, depth + 1)[0]
            if model is None:
                continue
            if model == view_root_model(registry, prec.path, pfacts, prec.index) and root and root != model and root not in out:
                out.append(root)  # a primary view on another model: its root is ours
            if model not in out:
                out.append(model)


def _parent_views(registry: Any, path: str, facts: dict, rec_index: int) -> list[tuple[RecordRef, dict]]:
    """(record, facts) of the views up the ``inherit_id`` chain of a record."""
    memo = _memo(registry)
    key = ("chain", path, rec_index)
    hit = memo.get(key)
    if hit is not None and hit[0] is facts:
        return hit[1]
    out: list[tuple[RecordRef, dict]] = []
    rec = _record(facts, rec_index)
    seen = {(path, rec_index)}
    parent = (rec[X_INFO] or {}).get("inherit_id") if rec is not None else None
    module = registry.module_of_path(path)
    for _ in range(_CHAIN_DEPTH):
        if not parent:
            break
        nxt = None
        for p in registry.records(registry.qualify_xmlid(parent, module)):
            if p.index < 0 or (p.path, p.index) in seen:
                continue
            pfacts = registry.file_facts(p.path)
            if not pfacts:
                continue
            seen.add((p.path, p.index))
            out.append((p, pfacts))
            if nxt is None:
                nxt = (p.info.get("inherit_id"), p.module)
        if nxt is None:
            break
        parent, module = nxt
    memo[key] = (facts, out)
    return out


def _siblings(registry: Any, path: str, facts: dict, rec_index: int, chain: list[tuple[RecordRef, dict]]) -> list[tuple[RecordRef, dict]]:
    """Other extensions (in scope) of the views of the chain: their nodes are
    part of the combined arch a spec is applied to."""
    scope = registry.scope_for_path(path)
    seen = {(path, rec_index)} | {(p.path, p.index) for p, _ in chain}
    out = []
    for prec, _ in chain:
        for child in registry.children_views(prec.xmlid):
            if child.index < 0 or child.tag == "qweb" or (child.path, child.index) in seen:
                continue
            if scope is not None and child.module not in scope:
                continue
            cfacts = registry.file_facts(child.path)
            if cfacts:
                seen.add((child.path, child.index))
                out.append((child, cfacts))
    return out


def _arch_names(registry: Any, path: str, facts: dict) -> dict[int, dict[tuple[str, str], list[tuple]]]:
    """{record index: {(ref kind, name): [refs]}} of the field/button nodes of
    the view archs of a file."""
    return _arch_refs(registry, path, facts)[1]


def _arch_refs(registry: Any, path: str, facts: dict) -> tuple[dict[int, list[tuple]], dict[int, dict[tuple[str, str], list[tuple]]]]:
    """Field/button node refs of the view archs of a file, by record: in
    document order, and by (kind, name)."""
    memo = _memo(registry)
    key = ("arch_refs", path)
    hit = memo.get(key)
    if hit is not None and hit[0] is facts:
        return hit[1]
    ordered: dict[int, list[tuple]] = {}
    named: dict[int, dict[tuple[str, str], list[tuple]]] = {}
    for ref in facts.get(REFS) or ():
        rctx = ref[R_CTX]
        if rctx[0] == "arch" or rctx[0] == "button":
            ordered.setdefault(rctx[1], []).append(ref)
            named.setdefault(rctx[1], {}).setdefault((ref[R_KIND], ref[R_VALUE]), []).append(ref)
    memo[key] = (facts, (ordered, named))
    return ordered, named


def _subview_models(registry: Any, path: str, facts: dict, rec_index: int) -> list[str]:
    """Models of the x2many sub-views found in the arch of the parent views."""
    memo = _memo(registry)
    key = ("subviews", path, rec_index)
    hit = memo.get(key)
    if hit is not None and hit[0] is facts:
        return hit[1]
    scope = registry.scope_for_path(path)
    root = view_root_model(registry, path, facts, rec_index)
    subs: dict[tuple, None] = {}
    for prec, pfacts in _parent_views(registry, path, facts, rec_index):
        for names in (_arch_names(registry, prec.path, pfacts).get(prec.index) or {}).values():
            for r in names:
                if r[R_CTX][2]:
                    subs[r[R_CTX][2]] = None
    out: list[str] = []
    for sub in subs:
        model = _walk(registry, scope, root, sub)
        if model and model not in out:
            out.append(model)
    memo[key] = (facts, out)
    return out


# ---------------------------------------------------------------------------
# inheritance specs (element nesting recovered from the record's text)


class _Spec:
    """An element with ``position=`` in an inherited view arch.

    ``steps``: the nodes it names - ``[(name, owner subpath, unknown context)]``,
    one for ``<field>``/``<button>``, one per ``field[@name]`` step of an xpath
    (the last one is the located node); ``step_refs``: the refs of those names
    (empty when they cannot be paired); ``base``: subpath of the spec's context;
    ``ext_sub``: x2many fields the extractor descended into for the content."""

    __slots__ = ("start", "end", "tstart", "tend", "position", "kind", "steps", "step_refs", "base", "ext_sub", "descends", "unknown", "content_unknown")

    def __init__(self, start: int, end: int, tstart: int, tend: int, position: str):
        self.start = start
        self.end = end
        self.tstart = tstart
        self.tend = tend
        self.position = position
        self.kind = REF_FIELD
        self.steps: list[tuple[str, tuple, bool]] = []
        self.step_refs: list[tuple] = []
        self.base: tuple = ()
        self.ext_sub: tuple = ()
        self.descends = False  # the content goes inside a sub-view of the located field
        self.unknown = False  # the located node may sit in a sub-view of an unknown x2many
        self.content_unknown = False  # ... and the content model cannot be derived at all


def _enclosing_spec(registry: Any, path: str, facts: dict, rec_index: int, ref: tuple, source: bytes | str | None) -> tuple["_Spec | None", int]:
    """Innermost spec containing ``ref`` and the ref's offset in the record text."""
    info = _record_specs(registry, path, facts, rec_index, source)
    if info is None:
        return None, -1
    lines, first, offsets, specs = info
    pos = _offset(lines, first, offsets, ref[R_LINE], ref[R_COL])
    if pos is None:
        return None, -1
    best = None
    for spec in specs:
        if spec.start <= pos < spec.end and (best is None or spec.start > best.start):
            best = spec
    return best, pos


def _record_specs(registry: Any, path: str, facts: dict, rec_index: int, source: bytes | str | None) -> tuple | None:
    """(lines, first line, line offsets, specs) of a record (offsets are str
    indexes in the record's lines joined by newlines)."""
    memo = _memo(registry)
    key = ("specs", path, rec_index)
    hit = memo.get(key)
    if hit is not None and hit[0] is facts:
        return hit[1]
    res = None
    rec = _record(facts, rec_index)
    lines = _source_lines(path, source, rec) if rec is not None else None
    if lines is not None and rec is not None:
        first, last = rec[X_LINE], min(rec[X_END_LINE], len(lines) - 1)
        offsets = []
        off = 0
        for i in range(first, last + 1):
            offsets.append(off)
            off += len(lines[i]) + 1
        raw_specs = _scan_specs("\n".join(lines[first : last + 1]))
        if raw_specs:
            refs = [(_offset(lines, first, offsets, r[R_LINE], r[R_COL]), r) for r in _arch_refs(registry, path, facts)[0].get(rec_index, ())]
            res = (lines, first, offsets, [_make_spec(s, refs) for s in raw_specs])
    memo[key] = (facts, res)
    return res


def _scan_specs(text: str) -> list[tuple]:
    """Elements carrying ``position=``: (start, end, tag start, tag end, tag, attrs)."""
    out = []
    stack: list[list] = []
    for m in _TAG_RE.finditer(text):
        tag = m.group(2)
        if tag is None:
            continue  # comment, CDATA, processing instruction
        if m.group(1):
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    for el in stack[i:]:
                        if el[3] is not None:
                            out.append((el[1], m.end(), el[1], el[2], el[0], el[3]))
                    del stack[i:]
                    break
            continue
        body = m.group(3)
        attrs = None
        if "position" in body:
            parsed = {a.group(1): a.group(2) if a.group(2) is not None else a.group(3) for a in _ATTR_RE.finditer(body)}
            if "position" in parsed:
                attrs = parsed
        if body.rstrip().endswith("/"):
            if attrs is not None:
                out.append((m.start(), m.end(), m.start(), m.end(), tag, attrs))
        else:
            stack.append([tag, m.start(), m.end(), attrs])
    return out


def _make_spec(raw: tuple, refs: list[tuple]) -> _Spec:
    start, end, tstart, tend, tag, attrs = raw
    spec = _Spec(start, end, tstart, tend, attrs.get("position") or "")
    in_tag = [r for pos, r in refs if pos is not None and tstart <= pos < tend]
    if tag == "field" or tag == "button":
        name = attrs.get("name")
        spec.kind = REF_FIELD if tag == "field" else REF_METHOD
        role = "arch" if tag == "field" else "button"
        if name:
            spec.steps = [(name, (), False)]
            ref = next((r for r in in_tag if r[R_KIND] == spec.kind and r[R_CTX][0] == role and r[R_VALUE] == name), None)
            if ref is not None:
                spec.step_refs = [ref]
                spec.base = tuple(ref[R_CTX][2])
    elif tag == "xpath":
        _xpath_spec(spec, attrs.get("expr") or "", [r for r in in_tag if r[R_KIND] == REF_FIELD and r[R_CTX][0] == "arch"])
    elif tag != "form":
        spec.unknown = True  # <page>, <group>, <column>, <list>...: the first match may be in a sub-view
    return spec


def _xpath_spec(spec: _Spec, expr: str, refs: list[tuple]) -> None:
    fields = list(_XPATH_FIELD_RE.finditer(expr))
    if refs and len(refs) == len(fields):
        spec.step_refs = refs
        spec.base = tuple(refs[0][R_CTX][2])
    steps = [(m.start(1), m.group(1), m.start(2), m.group(2)) for m in _XPATH_STEP_RE.finditer(expr)]
    ext_steps = [(m.start(), m.group(1)) for m in _XPATH_EXT_STEP_RE.finditer(expr)]
    field_starts = {m.start() for m in fields}
    unknown_from = None  # position of a sub-view step not reached through its x2many field
    prev_field = False
    for k, (_, _sep, tpos, tag) in enumerate(steps):
        if tpos in field_starts:
            prev_field = True
            continue
        if unknown_from is None and tag in SUBVIEW_TAGS and not prev_field and not (k == 0 and tag == "form"):
            unknown_from = tpos  # (a leading //form is the arch root)
        prev_field = False
    sub: list[str] = []
    ext: list[str] = []
    for m in fields:
        name = m.group(2)
        spec.steps.append((name, tuple(sub), unknown_from is not None and unknown_from < m.start()))
        nxt = next(((sep, tag) for (spos, sep, _t, tag) in steps if spos >= m.end()), None)
        # a node below a field node is inside one of its sub-views
        spec.descends = nxt is not None and (nxt[1] in SUBVIEW_TAGS or (nxt[0] == "//" and nxt[1] not in (".", "..")))
        if spec.descends:
            sub.append(name)
        if next((t for (pos, t) in ext_steps if pos >= m.end()), None) in SUBVIEW_TAGS:
            ext.append(name)  # what the extractor did
    spec.ext_sub = tuple(ext)
    spec.unknown = unknown_from is not None
    spec.content_unknown = spec.unknown and not (spec.steps and spec.steps[-1][2])


def _offset(lines: list[str], first: int, offsets: list[int], line: int, col: int) -> int | None:
    i = line - first
    if not 0 <= i < len(offsets):
        return None
    return offsets[i] + utf16_to_index(lines[line], col)


_DISK: OrderedDict[str, tuple[int, int, list[str]]] = OrderedDict()
_DISK_SIZE = 32


def _source_lines(path: str, source: bytes | str | None, rec: tuple) -> list[str] | None:
    """Lines of the text the facts were extracted from: ``source``, else the file
    on disk when it still matches the facts (record id at its position)."""
    if source is not None:
        text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
        return text.split("\n")
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _DISK.get(path)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        _DISK.move_to_end(path)
        lines = hit[2]
    else:
        try:
            with open(path, "rb") as fh:
                lines = fh.read().decode("utf-8", "replace").split("\n")
        except OSError:
            return None
        _DISK[path] = (st.st_mtime_ns, st.st_size, lines)
        while len(_DISK) > _DISK_SIZE:
            _DISK.popitem(last=False)
    if rec[X_ID] and rec[X_LINE] < len(lines):
        line = lines[rec[X_LINE]]
        if line[utf16_to_index(line, rec[X_COL]) : utf16_to_index(line, rec[X_END_COL])] != rec[X_ID]:
            return None  # the file changed since the facts were extracted
    return lines


# ---------------------------------------------------------------------------
# memo: per registry generation, entries validated by the identity of the facts


class _MemoState:
    __slots__ = ("registry", "generation", "data")

    def __init__(self) -> None:
        self.registry: Any = None
        self.generation = -1
        self.data: dict = {}


_STATE = _MemoState()
_MEMO_SIZE = 20000


def _memo(registry: Any) -> dict:
    st = _STATE
    reg = st.registry() if st.registry is not None else None
    if reg is not registry or st.generation != registry.generation or len(st.data) > _MEMO_SIZE:
        st.registry = weakref.ref(registry)
        st.generation = registry.generation
        st.data = {}
    return st.data


def clear_caches() -> None:
    _DISK.clear()
    _STATE.registry = None
    _STATE.generation = -1
    _STATE.data = {}


__all__ = [
    "targets_at",
    "arch_ref_model",
    "arch_ref_candidates",
    "view_root_model",
    "comodel",
    "nearest",
    "path_segment",
    "segment_target",
    "ref_name",
]
