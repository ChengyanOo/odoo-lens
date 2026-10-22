from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

from ..analysis import targets_at
from ..analysis.python import calls_in_function, enclosing_function, member_usages
from ..analysis.targets import METHOD, Target
from ..analysis.xml import arch_ref_model
from ..documents import lsp_range, uri_to_path, utf16_len, utf16_to_index
from ..facts import (
    CLASSES,
    F_COL,
    F_END_COL,
    F_END_LINE,
    F_LINE,
    F_NAME,
    K_FIELDS,
    K_MODEL,
    K_NAME,
    M_COL,
    M_END_COL,
    M_END_LINE,
    M_LINE,
    REF_METHOD,
    S_COL,
    S_END_COL,
    S_KIND,
    S_LINE,
    S_NAME,
    SYMBOLS,
    X_END_LINE,
)
from ..registry import UNLOADED_DIRS, MemberDef, RefHit
from .navigation import candidate_models, class_containing, scoped_lookup, super_definitions, trust_unknown_receiver

# LSP SymbolKind
KIND_FILE = 1
KIND_CLASS = 5
KIND_METHOD = 6
KIND_FIELD = 8
KIND_FUNCTION = 12
KIND_VARIABLE = 13
KIND_OBJECT = 19

MAX_CALLS = 100
TIME_BOX = 3.0  # seconds
MAX_RECEIVERS = 3
SUPER_RE = re.compile(r"super\s*\([^()]*\)\s*\.\s*$")
CALLER_ROLES = frozenset({"compute", "inverse", "search", "default", "selection", "group_expand", "compute_sql", "button", "function", "js", "code"})
# roles whose ctx holds a record index: the owner model is resolved from the record
RECORD_ROLES = frozenset({"button", "code"})
CODE_DETAILS = {"ir.cron": "cron code", "ir.actions.server": "server action code"}


def prepare(ctx: Any) -> list[dict]:
    for target in targets_at(ctx):
        if target.kind != METHOD:
            continue
        models = candidate_models(target)
        if not models:
            continue
        model = models[0]
        d = _target_definition(ctx, target, model)
        if d is not None:
            item = method_item(ctx, model, d)
            if target.model is None or target.extra.get("guess"):
                # Claude Code uses only the first item: keep the best candidate, flag the guess
                note = f"receiver not inferred (candidates: {', '.join(models)})"
                item["detail"] = f"{item['detail']} · {note}" if item["detail"] else note
            return [item]
    if ctx.kind != "py":
        return []
    enc = enclosing_function(ctx.registry, ctx.path, ctx.source, ctx.line)
    return [function_item(ctx, ctx.path, enc)] if enc else []


def _target_definition(ctx: Any, target: Target, model: str) -> MemberDef | None:
    name = target.name or ""
    if target.extra.get("super"):
        line = target.origin[0] if target.origin else ctx.line
        defs = super_definitions(ctx, model, name, line)
        return defs[0] if defs else None
    defs = scoped_lookup(ctx.registry.method, model, name, ctx.scope)
    if target.origin:
        for d in defs:
            if d.path == ctx.path and d.line == target.origin[0]:
                return d
    return defs[0] if defs else None


def _item(ctx: Any, name: str, kind: int, path: str, line: int, col: int, end_col: int, end_line: int, detail: str, data: dict) -> dict:
    lines = ctx.server.documents.lines(path) or []
    text = lines[line] if 0 <= line < len(lines) else ""
    start = min(len(text) - len(text.lstrip()), col)
    last = lines[end_line].rstrip("\r") if 0 <= end_line < len(lines) else ""
    end = utf16_len(last) if end_line > line else max(end_col, utf16_len(last))
    return {
        "name": name,
        "kind": kind,
        "detail": detail,
        "uri": ctx.server.documents.uri_for(path),
        "range": lsp_range(line, start, end_line, end),
        "selectionRange": lsp_range(line, col, line, end_col),
        "data": data,
    }


def method_item(ctx: Any, model: str, d: MemberDef) -> dict:
    f = d.facts
    detail = d.module or (d.cls.name if not d.cls.model else "")
    return _item(ctx, f"{model}.{d.name}", KIND_METHOD, d.path, f[M_LINE], f[M_COL], f[M_END_COL], f[M_END_LINE], detail, {"model": model, "name": d.name})


def function_item(ctx: Any, path: str, enc: dict) -> dict:
    model, cls, name = enc.get("model"), enc.get("class"), enc["name"]
    label = f"{model}.{name}" if model else f"{cls}.{name}" if cls else name
    kind = KIND_METHOD if model or cls else KIND_FUNCTION
    data = {"model": model, "name": name} if model else {"name": name, "class": cls}
    detail = ctx.registry.module_of_path(path) or ""
    return _item(ctx, label, kind, path, enc["line"], enc["col"], enc["end_col"], enc.get("end_line", enc["line"]), detail, data)


def _file_item(ctx: Any, path: str, line: int, col: int, end_col: int, name: str | None = None, kind: int = KIND_FILE) -> dict:
    detail = ctx.registry.module_of_path(path) or ""
    return _item(ctx, name or os.path.basename(path), kind, path, line, col, end_col, line, detail, {"name": name or os.path.basename(path)})


def _group(entries: dict, key: tuple, side: str, make_item: Callable[[], dict], line: int, col: int, end_col: int) -> None:
    entry = entries.get(key)
    if entry is None:
        entry = entries[key] = {side: make_item(), "fromRanges": []}
    entry["fromRanges"].append(lsp_range(line, col, line, end_col))


def incoming(ctx: Any, item: dict) -> list[dict]:
    data = item.get("data") or {}
    name = data.get("name") or item.get("name", "").rpartition(".")[2]
    model = data.get("model")
    if not name or not model:
        return []
    accepts = _receiver_filter(ctx.registry, model, name)
    refs = _ref_callers(ctx, model, name, accepts)
    deadline = time.monotonic() + TIME_BOX
    calls = _python_callers(ctx, model, name, accepts, deadline, MAX_CALLS - len(refs))
    return (calls + refs)[:MAX_CALLS]


def _receiver_filter(registry: Any, model: str, name: str) -> Callable[[Any], bool]:
    few = trust_unknown_receiver(registry, name)

    def accepts(receivers: Any) -> bool:
        if not receivers:
            return few
        return any(r == model or registry.inherits_from(r, model) or registry.inherits_from(model, r) for r in receivers)

    return accepts


def _candidate_files(ctx: Any, model: str) -> list[str]:
    registry = ctx.registry
    classes = registry.model_classes(model)
    owners = {c.module for c in classes if c.is_definition and c.module} or {c.module for c in classes if c.module}
    visible = set(owners)
    for m in owners:
        visible |= registry.dependents(m)
    custom = tuple(r.rstrip(os.sep) + os.sep for r in ctx.env.custom_roots or ())
    keyed = []
    for path, _ in registry.iter_files("py"):
        module = registry.module_of_path(path)
        if module not in visible:
            continue
        info = registry.module(module)
        aside = bool(UNLOADED_DIRS.intersection(os.path.relpath(os.path.dirname(path), info.path).split(os.sep)))
        customer = bool(custom) and path.startswith(custom)
        keyed.append(((aside, not customer, info.rank, path), path))
    keyed.sort()
    return [p for _, p in keyed]


def _python_callers(ctx: Any, model: str, name: str, accepts: Callable, deadline: float, cap: int) -> list[dict]:
    registry = ctx.registry
    needle = name.encode()
    entries: dict[tuple, dict] = {}
    for path in _candidate_files(ctx, model):
        if len(entries) >= cap or time.monotonic() > deadline:
            break
        ctx.token.check()
        src = ctx.server.documents.source(path)
        if not src or needle not in src:
            continue
        for line, col, end_col, receivers, kind in member_usages(registry, path, src, name):
            if kind not in ("call", "super") or not accepts(receivers):
                continue
            if kind == "super" and not _super_reaches(ctx, receivers, path, line):
                continue
            enc = enclosing_function(registry, path, src, line)
            if enc is None:
                _group(entries, (path, -1), "from", lambda: _file_item(ctx, path, line, col, end_col), line, col, end_col)
            else:
                _group(entries, (path, enc["line"]), "from", lambda: function_item(ctx, path, enc), line, col, end_col)
    return list(entries.values())[:cap]


def _super_reaches(ctx: Any, receivers: Any, path: str, line: int) -> bool:
    # ctx is on the item's definition; super() reaches only definitions below the caller in the MRO
    for model in receivers or ():
        mro = ctx.registry.model_mro(model)
        caller = class_containing(mro, path, line)
        target = class_containing(mro, ctx.path, ctx.line)
        if target is not None and (caller is None or mro.index(target) > mro.index(caller)):
            return True
    return False


def _ref_callers(ctx: Any, model: str, name: str, accepts: Callable) -> list[dict]:
    registry = ctx.registry
    entries: dict[tuple, dict] = {}
    for hit in registry.refs_by_name(REF_METHOD, name):
        role = hit.ctx[0] if hit.ctx else None
        if hit.value != name or role not in CALLER_ROLES:
            continue
        if role in RECORD_ROLES:
            owner = arch_ref_model(registry, hit.path, registry.file_facts(hit.path) or {}, hit.facts)
            if owner is None and role == "code":
                continue  # record without a known model (as findReferences)
        else:
            owner = hit.ctx[1] if len(hit.ctx) > 1 else None
        if not accepts([owner] if owner else None):
            continue
        key, make = _ref_caller(ctx, hit, owner)
        _group(entries, key, "from", make, hit.line, hit.col, hit.end_col)
        if len(entries) >= MAX_CALLS:
            break
    return list(entries.values())


def _ref_caller(ctx: Any, hit: RefHit, owner: str | None) -> tuple[tuple, Callable[[], dict]]:
    registry = ctx.registry
    path, role = hit.path, hit.ctx[0]
    facts = registry.file_facts(path) or {}
    if role in RECORD_ROLES:
        rec = registry.record_at(path, hit.ctx[1])
        if rec is not None:
            end = rec.facts[X_END_LINE] or rec.line
            label = rec.xmlid or os.path.basename(path)
            detail = "view button" if role == "button" else CODE_DETAILS.get(rec.model or "", "record code")
            return (path, rec.line), lambda: _item(ctx, label, KIND_OBJECT, path, rec.line, rec.col, rec.end_col, end, detail, {"xmlid": rec.xmlid})
    elif role == "js":
        sym = _js_symbol_before(facts, hit.line)
        if sym is not None:
            kind = KIND_CLASS if sym[S_KIND] == "class" else KIND_FUNCTION if sym[S_KIND] == "def" else KIND_VARIABLE
            return (path, sym[S_LINE]), lambda: _item(ctx, sym[S_NAME], kind, path, sym[S_LINE], sym[S_COL], sym[S_END_COL], sym[S_LINE], "orm call", {"name": sym[S_NAME]})
    elif role != "function":
        for cls in facts.get(CLASSES) or ():
            if not cls[K_MODEL]:
                continue
            for f in cls[K_FIELDS]:
                if f[F_LINE] <= hit.line <= f[F_END_LINE]:
                    label = f"{owner or cls[K_NAME]}.{f[F_NAME]}"
                    return (path, f[F_LINE]), lambda: _item(ctx, label, KIND_FIELD, path, f[F_LINE], f[F_COL], f[F_END_COL], f[F_END_LINE], f"{role}=", {"model": owner, "field": f[F_NAME]})
    label = f"<function> {owner}.{hit.value}" if role == "function" else None
    return (path, hit.line), lambda: _file_item(ctx, path, hit.line, hit.col, hit.end_col, label, KIND_FUNCTION if label else KIND_FILE)


def _js_symbol_before(facts: dict, line: int) -> tuple | None:
    best = None
    for sym in facts.get(SYMBOLS) or ():
        if sym[S_LINE] <= line and (best is None or sym[S_LINE] >= best[S_LINE]):
            best = sym
    return best


def outgoing(ctx: Any, item: dict) -> list[dict]:
    registry = ctx.registry
    path = uri_to_path(item["uri"])
    line = (item.get("selectionRange") or item["range"])["start"]["line"]
    model = (item.get("data") or {}).get("model")
    src = ctx.server.documents.source(path) or b""
    lines = ctx.server.documents.lines(path) or []
    entries: dict[tuple, dict] = {}
    for name, cl, col, end_col, receivers in calls_in_function(registry, path, src, line):
        ctx.token.check()
        text = lines[cl] if 0 <= cl < len(lines) else ""
        before = text[: utf16_to_index(text, col)]
        if SUPER_RE.search(before):
            resolved = [(model, d) for d in super_definitions(ctx, model, name, cl)[:1]] if model else []
        else:
            resolved = []
            for r in (receivers or ())[:MAX_RECEIVERS]:
                defs = scoped_lookup(registry.method, r, name, ctx.scope)
                if defs:
                    resolved.append((r, defs[0]))
        for r, d in resolved:
            _group(entries, (d.path, d.line), "to", lambda: method_item(ctx, r, d), cl, col, end_col)
        if len(entries) >= MAX_CALLS:
            break
    return list(entries.values())

