from __future__ import annotations

import ast
import csv
import heapq
import os
import re
import weakref
from array import array
from bisect import bisect_left, bisect_right
from collections import defaultdict
from itertools import accumulate
from operator import itemgetter
from typing import Any, Callable, Iterable, Iterator

from ..documents import path_to_uri, utf16_len, utf16_to_index
from ..facts import (
    A_COL,
    A_END_COL,
    A_KIND,
    A_LINE,
    A_NAME,
    A_VALUE,
    CLASSES,
    CSV_MODEL,
    F_ARGS,
    F_COL,
    F_COMODEL,
    F_END_COL,
    F_END_LINE,
    F_LINE,
    F_NAME,
    F_TYPE,
    I_ASNAME,
    I_LEVEL,
    I_MODULE,
    I_NAME,
    IMPORTS,
    K_ATTRS,
    K_BASES,
    K_COL,
    K_END_COL,
    K_END_LINE,
    K_FIELDS,
    K_LINE,
    K_METHODS,
    K_MODEL,
    K_NAME,
    KIND,
    KIND_CSV,
    KIND_JS,
    KIND_MANIFEST,
    KIND_PY,
    KIND_XML,
    M_COL,
    M_DECORATORS,
    M_END_COL,
    M_END_LINE,
    M_LINE,
    M_NAME,
    M_SUPER,
    MANIFEST_ASSETS,
    MANIFEST_DATA,
    MANIFEST_DEMO,
    MANIFEST_DEPENDS,
    MANIFEST_KEYS,
    MANIFEST_VALUES,
    MI_INHERITS,
    MI_KIND,
    R_CTX,
    R_KIND,
    R_LINE,
    R_VALUE,
    RECORDS,
    REF_QWEB,
    REFS,
    S_COL,
    S_END_COL,
    S_END_LINE,
    S_KIND,
    S_LINE,
    S_NAME,
    SYMBOLS,
    X_COL,
    X_END_COL,
    X_END_LINE,
    X_ID,
    X_INFO,
    X_LINE,
    X_MODEL,
    X_TAG,
)
from ..protocol import NEVER_CANCELLED
from ..registry import UNLOADED_DIRS, model_class_info

SK_FILE = 1
SK_MODULE = 2
SK_CLASS = 5
SK_METHOD = 6
SK_FIELD = 8
SK_FUNCTION = 12
SK_VARIABLE = 13
SK_CONSTANT = 14
SK_OBJECT = 19
SK_KEY = 20

WORKSPACE_CAP = 150  # Claude Code applies no cap of its own
FUZZY_CAP = 20
FUZZY_PER_NAME = 5
NEAR_MIN = 4  # shorter queries have too many one-typo neighbors
# Claude Code spills answers over 50k rendered chars to a file (2 KB preview)
OUTPUT_BUDGET = 40_000
MAX_DOC_ITEMS = 2000

EXACT, PREFIX, SUBSTRING, FUZZY = 0, 1, 2, 3
FUZZY_NOTE = " [fuzzy match]"
# kind names as Claude Code prints them (rendered-size estimates)
KIND_NAMES = {
    SK_FILE: "File",
    SK_MODULE: "Module",
    SK_CLASS: "Class",
    SK_METHOD: "Method",
    SK_FIELD: "Field",
    SK_FUNCTION: "Function",
    SK_VARIABLE: "Variable",
    SK_CONSTANT: "Constant",
    SK_OBJECT: "Object",
    SK_KEY: "Key",
}

RELATIONAL = frozenset({"Many2one", "One2many", "Many2many"})
MODEL_DECL_ATTRS = frozenset({"_name", "_inherit", "_inherits"})
CONSTANT_ATTRS = frozenset({"constraint", "index", "sql_constraints"})
TEST_DIRS = UNLOADED_DIRS - {"static"}
VIEW_TYPES = frozenset(
    {"form", "list", "tree", "kanban", "search", "graph", "pivot", "calendar", "gantt", "activity", "cohort", "map", "grid", "hierarchy", "qweb", "card"}
)
ACTION_KINDS = {
    "ir.actions.act_window": "window action",
    "ir.actions.server": "server action",
    "ir.actions.report": "report",
    "ir.actions.client": "client action",
    "ir.actions.act_url": "URL action",
    "ir.actions.act_window.view": "action view",
    "ir.cron": "scheduled action",
}
# actions whose name labels a <menuitem> without name= (odoo/tools/convert.py _tag_menuitem)
MENU_LABEL_ACTIONS = frozenset({"ir.actions.act_window", "ir.actions.act_url", "ir.actions.client", "ir.actions.server", "ir.actions.wizard"})
ACL_MODELS = frozenset({"ir.model.access", "ir.access"})
ROUTE_KWARGS = ("type", "auth", "methods", "website", "csrf", "readonly", "sitemap")
PREFIXES = frozenset({"model", "field", "method", "xmlid", "view", "action", "menu", "template", "module", "class", "def", "route"})
MEMBER_CATS = frozenset({"field", "method", "attr"})
RECORD_CATS = frozenset({"record", "template", "view", "action", "menu", "qweb"})
PLAIN_CATS = ("model", "record", "template", "module", "field", "method", "attr", "pyclass", "pyfunc", "jsclass", "jsfunc", "jsvar")
CODE_HEADS = frozenset({"fields", "models", "api", "http", "tools", "exceptions", "osv", "release"})
# share of a truncated 'model.' listing per (own vs inherited, kind)
LISTING_WEIGHTS = {(True, "field"): 60, (True, "method"): 45, (True, "attr"): 10, (False, "field"): 15, (False, "method"): 15, (False, "attr"): 4}

_JS_EXTENDS = re.compile(r"\bextends\s+([\w$.]+)")
_MEMBER_SEP = re.compile(r"^([\w.]+?)\s*(?:[/:]|\s)\s*([\w.]*)$")
_URL_HOST = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*")
# res.lang url codes (fr, fr_BE, es_419, sr@latin): http_routing retries without them
_LANG_SEGMENT = re.compile(r"^[a-z]{2,3}(?:[_-][A-Za-z0-9]{2,4})?(?:@[a-z]+)?$")
_CODE_DEF = re.compile(r"^(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)")
_CODE_ENV = re.compile(r"""\benv\s*\[\s*(['"])([\w.]+)\1\s*\]((?:\s*\.\s*\w+)*)""")
_CODE_REF = re.compile(r"""\bref\(\s*(['"])([\w.]+)\1""")
_CODE_ROUTE = re.compile(r"""^[\w.]*route\(\s*\[?\s*(['"])(/[^'"]*)\1""")
_CODE_CALL = re.compile(r"^([\w.]+)\(")
_NEAR_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_."
_NAME_LIKE = re.compile(r"[\w.$-]+")
_WORD_SEPS = "\n._/-"
_NO_MODULE = (1, -1)  # outside modules: not customer, ranked before every module
_UNLISTED = 1 << 20


def _rng(l0: int, c0: int, l1: int, c1: int) -> dict:
    return {"start": {"line": l0, "character": c0}, "end": {"line": l1, "character": c1}}


def _clip(text: Any, n: int) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _major(env: Any) -> int | None:
    version = getattr(env, "version", None)
    return version.major if version is not None else None


def _field_summary(ftype: str | None, comodel: str | None, args: dict, arrow: str = "→", label: bool = False) -> str:
    text = (ftype or "?") + (f"{arrow}{comodel}" if comodel else "")
    string = args.get("string")
    if label and isinstance(string, str) and string:
        text += f' "{_clip(string, 40)}"'
    for key in ("related", "compute"):
        value = args.get(key)
        if isinstance(value, str):
            text += f" {key}={value}"
        elif isinstance(value, tuple) and len(value) == 2 and value[0] == "=":
            text += f" {key}={_clip(value[1], 40)}"
    if label:
        if args.get("store") is True and ("compute" in args or "related" in args):
            text += " stored"
        if args.get("required") is True:
            text += " required"
    if args.get("automatic") is True:
        text += " (automatic)"
    return text


def _record_detail(
    rec: tuple, with_model: bool = True, module: str | None = None, target: str | None = None, label: str | None = None,
    binding: str | None = None,
) -> str:
    model, tag, info = rec[X_MODEL], rec[X_TAG], rec[X_INFO] or {}
    if module:
        info = {k: (f"{module}.{v}" if k in ("inherit_id", "action") and isinstance(v, str) and "." not in v else v) for k, v in info.items()}
    name = info.get("name")
    name = _clip(name, 60) if isinstance(name, str) and name.strip() else ""
    if tag == "qweb":
        if info.get("mode") == "extension":
            return f"owl template extension of {info.get('inherit') or '?'}"
        text = "owl template"
        if info.get("inherit"):
            text += f", inherits {info['inherit']}"
        return text
    if tag == "implicit":
        if info.get("field"):
            return f"implicit xml id of field {info.get('model')}.{info['field']}"
        return f"implicit xml id of model {info.get('model')}"
    # explicit <field name="type"> wins over the arch root tag
    vtype = info.get("view_type") or info.get("type")
    primary = ", primary" if info.get("mode") == "primary" and info.get("inherit_id") else ""
    if tag == "template" or (model == "ir.ui.view" and vtype == "qweb"):
        text = "QWeb template" + primary
        if info.get("inherit_id"):
            text += f", inherits {info['inherit_id']}"
        if name and tag == "template":
            text += f' "{name}"'
    elif model == "ir.ui.view":
        text = f"{vtype} view" if vtype in VIEW_TYPES else "view"
        if info.get("view_model"):
            text += f" of {info['view_model']}"
        if info.get("inherit_id"):
            text += primary + f", inherits {info['inherit_id']}"
    elif model == "ir.ui.menu":
        if name:
            text = f"menu: {name}"
        elif label:
            text = f"menu: {_clip(label, 60)} (from action)"
        else:
            text = "menu"
        if info.get("action"):
            text += f" → {info['action']}"
    elif model and (model.startswith("ir.actions.") or model == "ir.cron"):
        text = ACTION_KINDS.get(model, model[len("ir.actions.") :])
        on = info.get("res_model") or target
        if on:
            text += f" → {on}"
        if name:
            text += f' "{name}"'
        if binding:
            text += f", in the {'Print' if model == 'ir.actions.report' else 'Action'} menu of {binding}"
    else:
        text = (model or tag or "record") if with_model else ""
        if name:
            text = f'{text} "{name}"'.strip()
        if target:
            text = f"{text} on {target}".strip()
    if info.get("noupdate"):
        text += ", noupdate"
    return text


def _record_target(reg: Any, rec: tuple, module: str | None) -> str | None:
    info = rec[X_INFO] or {}
    if info.get("res_model") or not info.get("model_id") or rec[X_TAG] == "csv":
        return None  # CSV model_id columns hold the model name (ir.access) or are ACL rows
    return _model_of_xmlid(reg, info["model_id"], module)


def _binding_target(reg: Any, rec: tuple, module: str | None) -> str | None:
    info = rec[X_INFO] or {}
    if not info.get("binding_model_id") or rec[X_TAG] == "csv":
        return None
    return _model_of_xmlid(reg, info["binding_model_id"], module)


def _action_label(reg: Any, action: str | None, module: str | None) -> str | None:
    if reg is None or not action:
        return None
    for r in reg.records(reg.qualify_xmlid(action, module)):
        if r.model in MENU_LABEL_ACTIONS:
            name = r.info.get("name")
            if isinstance(name, str) and name.strip():
                return " ".join(name.split())
    return None


def _menu_label(reg: Any, rec: tuple, module: str | None) -> str | None:
    info = rec[X_INFO] or {}
    if rec[X_MODEL] != "ir.ui.menu" or info.get("name") or not info.get("action"):
        return None
    return _action_label(reg, info["action"], module)


def document_symbols(ctx: Any, hierarchical: bool = True) -> list[dict]:
    facts = ctx.facts
    if not facts:
        return []
    build = _DOCUMENT_BUILDERS.get(facts.get(KIND))
    if build is None:
        return []
    symbols, unbuilt = build(ctx, facts, ctx.text.split("\n"))
    symbols = _within_budget(symbols, unbuilt)
    if hierarchical:
        return symbols
    flat: list[dict] = []
    _flatten(ctx.uri, symbols, None, flat)
    return flat


def _rendered_size(name: str, kind: int, detail: str, depth: int, line: int) -> int:
    # '<2 spaces per depth>name (Kind) detail - Line N' as Claude Code prints it
    return 2 * depth + len(name) + len(KIND_NAMES.get(kind, "Unknown")) + 3 + (len(detail) + 1 if detail else 0) + 8 + len(str(line + 1))


def _within_budget(symbols: list[dict], unbuilt: tuple[int, int] | None = None, budget: int | None = None) -> list[dict]:
    budget = OUTPUT_BUDGET if budget is None else budget
    used = len("Document symbols:")
    dropped = 0
    first: int | None = None

    def count(items: list[dict] | None) -> int:
        return sum(1 + count(s.get("children")) for s in items or ())

    def walk(items: list[dict], depth: int) -> list[dict]:
        nonlocal used, dropped, first
        kept = []
        for s in items:
            if dropped:
                dropped += 1 + count(s.get("children"))
                continue
            used += _rendered_size(s["name"], s["kind"], s.get("detail") or "", depth, s["range"]["start"]["line"]) + 1
            if used > budget:
                dropped += 1 + count(s.get("children"))
                first = s["range"]["start"]["line"]
                continue
            if s.get("children"):
                s["children"] = walk(s["children"], depth + 1)
                if not s["children"]:
                    del s["children"]
            kept.append(s)
        return kept

    out = walk(symbols, 0)
    if unbuilt and unbuilt[1]:
        first = unbuilt[0] if first is None else first
        dropped += unbuilt[1]
    if dropped:
        out.append(_more_marker(first or 0, dropped, "symbols"))
    return out


def _flatten(uri: str, symbols: list[dict], container: str | None, out: list[dict]) -> None:
    for s in symbols:
        info = {"name": s["name"], "kind": s["kind"], "location": {"uri": uri, "range": s["range"]}}
        if container:
            info["containerName"] = container
        out.append(info)
        if s.get("children"):
            _flatten(uri, s["children"], s["name"], out)


def _doc_symbol(name: str, kind: int, detail: str, rng: dict, sel: dict, children: list | None = None) -> dict:
    sym = {"name": name or "?", "kind": kind, "range": rng, "selectionRange": sel}
    if detail:
        sym["detail"] = detail
    if children:
        sym["children"] = children
    return sym


def _line(lines: list[str], n: int) -> str:
    return lines[n].rstrip("\r") if 0 <= n < len(lines) else ""


def _spans(lines: list[str], line: int, end_line: int, col: int, end_col: int) -> tuple[dict, dict]:
    # Claude prints range.start.line: start on the class/def line, never on a decorator
    text = _line(lines, line)
    start = min(col, utf16_len(text[: len(text) - len(text.lstrip())]))
    end_line = max(end_line, line)
    end = utf16_len(_line(lines, end_line))
    if end_line == line:
        end = max(end, end_col)
    return _rng(line, start, end_line, end), _rng(line, col, line, end_col)


def _ordered(items: list[tuple[int, int, dict]]) -> list[dict]:
    items.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in items]


def _py_document(ctx: Any, facts: dict, lines: list[str]) -> tuple[list[dict], None]:
    major = _major(ctx.env)
    reg = ctx.registry
    dead = reg is not None and not reg.is_loaded(ctx.path)
    items: list[tuple[int, int, dict]] = []
    for cls in facts.get(CLASSES) or ():
        items.append((cls[K_LINE], cls[K_COL], _py_class(ctx, cls, lines, major, dead)))
    for s in facts.get(SYMBOLS) or ():
        kind = s[S_KIND]
        if kind == "class":
            continue  # CLASSES carry the same classes with their Odoo meaning
        rng, sel = _spans(lines, s[S_LINE], s[S_END_LINE], s[S_COL], s[S_END_COL])
        if kind == "def":
            items.append((s[S_LINE], s[S_COL], _doc_symbol(s[S_NAME], SK_FUNCTION, _signature(lines, s[S_LINE], s[S_END_COL]), rng, sel)))
        else:
            items.append((s[S_LINE], s[S_COL], _doc_symbol(s[S_NAME], SK_VARIABLE, "", rng, sel)))
    return _ordered(items), None


def _dead_note(cls: tuple) -> str:
    # non-model code of unimported files may still run (IoT drivers, scripts)
    mi = cls[K_MODEL]
    if (mi and mi.get(MI_KIND) != "framework") or any(_is_route(d[0]) for m in cls[K_METHODS] for d in m[M_DECORATORS]):
        return "not imported: never loaded"
    return "not imported by its module"


def _py_class(ctx: Any, cls: tuple, lines: list[str], major: int | None, dead: bool = False) -> dict:
    reg = ctx.registry
    mi = cls[K_MODEL]
    model, is_def, parents = None, False, []
    if mi and mi.get(MI_KIND) != "framework":
        model, is_def, parents = model_class_info(cls, major)
    lookups = reg is not None and bool(model)
    path, scope = ctx.path, ctx.scope
    resolver = None
    if any(_is_route(d[0]) for m in cls[K_METHODS] for d in m[M_DECORATORS]):
        resolver = _PathResolver(ctx.server, reg, path, ctx.facts, lines, {}).for_class(cls)
    children: list[tuple[int, int, dict]] = []
    for f in cls[K_FIELDS]:
        defs = reg.field(model, f[F_NAME], scope) if lookups else []
        comodel = f[F_COMODEL]
        if not comodel and f[F_TYPE] in RELATIONAL and defs:
            comodel = reg.comodel(model, f[F_NAME], scope)
        detail = _field_summary(f[F_TYPE], comodel, f[F_ARGS] or {}, arrow=" → ", label=True)
        below = _overridden(defs, path, f[F_LINE])
        if below is not None:
            detail += f", overrides {_owner_label(below, model)}"
        rng, sel = _spans(lines, f[F_LINE], f[F_END_LINE], f[F_COL], f[F_END_COL])
        children.append((f[F_LINE], f[F_COL], _doc_symbol(f[F_NAME], SK_FIELD, detail, rng, sel)))
    for m in cls[K_METHODS]:
        parts = _decorators(m, resolver)
        if lookups:
            below = _overridden(reg.method(model, m[M_NAME], scope), path, m[M_LINE])
            if below is not None:
                parts.append(f"overrides {_owner_label(below, model)}" + ("" if m[M_SUPER] else " without super()"))
        rng, sel = _spans(lines, m[M_LINE], m[M_END_LINE], m[M_COL], m[M_END_COL])
        children.append((m[M_LINE], m[M_COL], _doc_symbol(m[M_NAME], SK_METHOD, " ".join(parts), rng, sel)))
    for a in cls[K_ATTRS]:
        if model and a[A_NAME] in MODEL_DECL_ATTRS:
            continue
        constant = a[A_KIND] in CONSTANT_ATTRS
        detail = _clip(a[A_VALUE], 80) if constant else f"= {_clip(a[A_VALUE], 80)}"
        rng, sel = _spans(lines, a[A_LINE], a[A_LINE], a[A_COL], a[A_END_COL])
        children.append((a[A_LINE], a[A_COL], _doc_symbol(a[A_NAME], SK_CONSTANT if constant else SK_VARIABLE, detail, rng, sel)))
    rng, sel = _spans(lines, cls[K_LINE], cls[K_END_LINE], cls[K_COL], cls[K_END_COL])
    detail = _class_detail(cls, model, is_def, parents)
    if dead:
        detail = f"{detail}, {_dead_note(cls)}" if detail else _dead_note(cls)
    return _doc_symbol(cls[K_NAME], SK_CLASS, detail, rng, sel, _ordered(children))


def _class_detail(cls: tuple, model: str | None, is_def: bool, parents: list[str]) -> str:
    bases = ", ".join(cls[K_BASES])
    mi = cls[K_MODEL] or {}
    if not model:
        if mi.get(MI_KIND) == "framework":
            return f"ORM base class ({bases})" if bases else "ORM base class"
        return f"({bases})" if bases else ""
    if is_def:
        flavor = {"AbstractModel": ", abstract", "TransientModel": ", transient"}.get(mi.get(MI_KIND), "")
        text = f"{model} (defines{flavor})"
    else:
        text = f"extends {model}"
    if parents:
        text += " + " + " + ".join(parents)
    inherits = mi.get(MI_INHERITS) or {}
    if inherits:
        text += ", _inherits " + ", ".join(f"{p} via {f}" for p, f in inherits.items())
    return text


def _overridden(defs: list, path: str, line: int) -> Any:
    # defs: most-derived first
    for i, d in enumerate(defs):
        if d.delegated_via is None and d.line == line and d.path == path:
            for below in defs[i + 1 :]:
                if below.delegated_via is None:
                    return below
            return None
    return None


def _owner_label(d: Any, model: str | None) -> str:
    cls = d.cls
    if not cls.model:
        return cls.name
    if cls.model == model:
        return cls.module or "?"
    return f"{cls.model} ({cls.module})"


def _decorators(m: tuple, resolver: _PathResolver | None) -> list[str]:
    out = []
    routes = None
    for name, args in m[M_DECORATORS]:
        if _is_route(name) and resolver is not None:
            if routes is None:
                routes = resolver.decorators(m[M_LINE]) or []
            if routes:
                paths, kw = routes.pop(0)
                text = "@route " + (", ".join(paths) if paths else "(inherited path)")
                for k in ("type", "auth"):
                    if isinstance(kw.get(k), str):
                        text += f" {k}={kw[k]}"
                out.append(text)
                continue
        if not args:
            out.append("@" + name)
        else:
            out.append(f"@{name}({_clip(', '.join('…' if a == '?' else a for a in args), 60)})")
    return out


def _signature(lines: list[str], line: int, col: int) -> str:
    text = _line(lines, line)
    rest = text[utf16_to_index(text, col) :].lstrip()
    if not rest.startswith("("):
        return ""
    chunks: list[str] = []
    depth = 0
    for n in range(line, min(line + 15, len(lines))):
        seg = rest if n == line else _line(lines, n).strip()
        for j, ch in enumerate(seg):
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    chunks.append(seg[: j + 1])
                    sig = " ".join(" ".join(chunks).split())
                    return _clip(sig.replace("( ", "(").replace(", )", ")").replace(" )", ")"), 100)
        chunks.append(seg)
    return ""


def _xml_document(ctx: Any, facts: dict, lines: list[str]) -> tuple[list[dict], tuple[int, int] | None]:
    recs = facts.get(RECORDS) or ()
    reg, module = ctx.registry, ctx.module
    flat: list[dict] = []
    for rec in recs[:MAX_DOC_ITEMS]:
        l0, l1 = rec[X_LINE], max(rec[X_END_LINE], rec[X_LINE])
        text = _line(lines, l0)
        lt = text.rfind("<", 0, utf16_to_index(text, rec[X_COL]))
        start = utf16_len(text[:lt]) if lt >= 0 else rec[X_COL]
        end = utf16_len(_line(lines, l1))
        if l1 == l0:
            end = max(end, rec[X_END_COL])
        name = rec[X_ID] or f"{rec[X_MODEL] or rec[X_TAG]} (no id)"
        detail = _record_detail(rec, target=_record_target(reg, rec, module), label=_menu_label(reg, rec, module), binding=_binding_target(reg, rec, module))
        flat.append(_doc_symbol(name, SK_OBJECT, detail, _rng(l0, start, l1, end), _rng(l0, rec[X_COL], l0, rec[X_END_COL])))
    unbuilt = (recs[MAX_DOC_ITEMS][X_LINE], len(recs) - MAX_DOC_ITEMS) if len(recs) > MAX_DOC_ITEMS else None
    return _nest(flat), unbuilt


def _more_marker(line: int, count: int, what: str) -> dict:
    text = f"({count} more {what} not listed: the outline stops at ~{OUTPUT_BUDGET // 1000}k chars; read the file around this line)"
    return _doc_symbol(text, SK_OBJECT, "", _rng(line, 0, line, 0), _rng(line, 0, line, 0))


def _nest(symbols: list[dict]) -> list[dict]:
    # symbols in document order
    roots: list[dict] = []
    stack: list[dict] = []
    for s in symbols:
        r = s["range"]
        while stack:
            outer = stack[-1]["range"]
            if outer["start"]["line"] < r["start"]["line"] and (r["end"]["line"], r["end"]["character"]) <= (outer["end"]["line"], outer["end"]["character"]):
                break
            stack.pop()
        if stack:
            stack[-1].setdefault("children", []).append(s)
        else:
            roots.append(s)
        stack.append(s)
    return roots


def _csv_document(ctx: Any, facts: dict, lines: list[str]) -> tuple[list[dict], tuple[int, int] | None]:
    recs = facts.get(RECORDS) or ()
    file_model = facts.get(CSV_MODEL)
    acl = _AclColumns(lines) if file_model in ACL_MODELS else None
    out: list[dict] = []
    for rec in recs[:MAX_DOC_ITEMS]:
        info = rec[X_INFO] or {}
        if acl is not None:
            detail = acl.detail(ctx.registry, rec, _line(lines, rec[X_LINE]), file_model, ctx.module)
        else:
            detail = file_model or ""
            name = info.get("name")
            if isinstance(name, str) and name.strip():
                detail += f' "{_clip(name, 60)}"'
        line = rec[X_LINE]
        end = max(utf16_len(_line(lines, line)), rec[X_END_COL])
        out.append(_doc_symbol(rec[X_ID] or "?", SK_OBJECT, detail, _rng(line, 0, line, end), _rng(line, rec[X_COL], line, rec[X_END_COL])))
    unbuilt = (recs[MAX_DOC_ITEMS][X_LINE], len(recs) - MAX_DOC_ITEMS) if len(recs) > MAX_DOC_ITEMS else None
    return out, unbuilt


# ir.model.access (perm_*), or saas-19.4+ ir.access (operation, domain; groupless row = restriction)
class _AclColumns:
    __slots__ = ("perms", "op", "domain")

    def __init__(self, lines: list[str]):
        cols = [c.strip() for c in _csv_row(_line(lines, 0).lstrip("\ufeff"))]
        self.perms = [(i, c[5:]) for i, c in enumerate(cols) if c.startswith("perm_")]
        self.op = cols.index("operation") if "operation" in cols else -1
        self.domain = next((i for i, c in enumerate(cols) if c in ("domain", "domain_force")), -1)

    def detail(self, reg: Any, rec: tuple, text: str, file_model: str | None, module: str | None) -> str:
        info = rec[X_INFO] or {}
        row = _csv_row(text) if self.perms or self.op >= 0 or self.domain >= 0 else []
        model = info.get("res_model") or _model_of_xmlid(reg, info.get("model_id"), module) or info.get("model_id") or "?"
        group = info.get("group_id")
        if group:
            parts = [model, f"group {group}"]
        else:
            parts = [model, "restriction (all users)" if file_model == "ir.access" else "no group"]
        if self.perms and len(row) > self.perms[-1][0]:
            parts.append("/".join(p for i, p in self.perms if row[i].strip().lower() in ("1", "true")) or "no access")
        if 0 <= self.op < len(row) and row[self.op].strip():
            parts.append(f"operation {row[self.op].strip()}")
        if 0 <= self.domain < len(row) and row[self.domain].strip():
            parts.append(f"domain {_clip(row[self.domain], 80)}")
        return ", ".join(parts)


def _csv_row(text: str) -> list[str]:
    try:
        return next(csv.reader([text]), [])
    except csv.Error:
        return []


def _model_of_xmlid(reg: Any, xid: str | None, module: str | None) -> str | None:
    if not xid or reg is None:
        return None
    for r in reg.records(reg.qualify_xmlid(xid, module)):
        if r.tag == "implicit" and r.model == "ir.model":
            return r.info.get("model")
    return None


def _manifest_document(ctx: Any, facts: dict, lines: list[str]) -> tuple[list[dict], None]:
    keys = sorted((facts.get(MANIFEST_KEYS) or {}).items(), key=lambda kv: (kv[1][0], kv[1][1]))
    values = facts.get(MANIFEST_VALUES) or {}
    entries = [(e, SK_MODULE) for e in facts.get(MANIFEST_DEPENDS) or ()]
    entries += [(e, SK_FILE) for e in (facts.get(MANIFEST_DATA) or []) + (facts.get(MANIFEST_DEMO) or [])]
    bundles = facts.get(MANIFEST_ASSETS) or {}
    out: list[dict] = []
    for n, (key, (l0, c0, c1)) in enumerate(keys):
        nxt = keys[n + 1][1][:2] if n + 1 < len(keys) else (1 << 30, 0)
        mine = [(e, k) for e, k in entries if (l0, c0) < (e[1], e[2]) < nxt]
        children = [_doc_symbol(e[0], k, "", _rng(e[1], e[2], e[1], e[3]), _rng(e[1], e[2], e[1], e[3])) for e, k in mine]
        end_line = mine[-1][0][1] if mine else l0
        if key == "depends":
            detail = _clip(", ".join(e[0] for e, _ in mine), 120)
        elif mine:
            detail = f"{len(mine)} files"
        elif key == "assets":
            detail = _clip(", ".join(f"{b} ({len(v)})" for b, v in bundles.items()), 120)
            end_line = max([e[1] for v in bundles.values() for e in v] or [l0])
        else:
            detail = _value_text(values.get(key))
        rng = _rng(l0, max(0, c0 - 1), end_line, utf16_len(_line(lines, end_line)))
        out.append(_doc_symbol(key, SK_KEY, detail, rng, _rng(l0, c0, l0, c1), children))
    return out, None


def _value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _clip(", ".join(str(v) for v in value), 80)
    if isinstance(value, dict):
        return f"{len(value)} entries"
    return _clip(value, 80)


def _js_document(ctx: Any, facts: dict, lines: list[str]) -> tuple[list[dict], None]:
    syms = sorted(facts.get(SYMBOLS) or (), key=lambda s: (s[S_LINE], s[S_COL]))
    templates = sorted((r[R_LINE], r[R_VALUE]) for r in facts.get(REFS) or () if r[R_KIND] == REF_QWEB and r[R_CTX][:1] == ("template",))
    out = []
    for i, s in enumerate(syms):
        nxt = syms[i + 1][S_LINE] if i + 1 < len(syms) else 1 << 30
        parts = []
        if s[S_KIND] == "class":
            m = _JS_EXTENDS.search(_line(lines, s[S_LINE]))
            if m:
                parts.append(f"extends {m.group(1)}")
        elif s[S_KIND] == "def":
            parts.append(_signature(lines, s[S_LINE], s[S_END_COL]))
        names = [v for ln, v in templates if s[S_LINE] <= ln < nxt]
        if names:
            parts.append("owl template: " + ", ".join(names))
        kind = SK_CLASS if s[S_KIND] == "class" else SK_FUNCTION if s[S_KIND] == "def" else SK_VARIABLE
        rng, sel = _spans(lines, s[S_LINE], s[S_END_LINE], s[S_COL], s[S_END_COL])
        out.append(_doc_symbol(s[S_NAME], kind, ", ".join(p for p in parts if p), rng, sel))
    return out, None


# builder -> (symbols, (line, count) of items left unbuilt, or None)
_DOCUMENT_BUILDERS: dict[str, Callable[[Any, dict, list[str]], tuple[list[dict], tuple[int, int] | None]]] = {
    KIND_PY: _py_document,
    KIND_XML: _xml_document,
    KIND_CSV: _csv_document,
    KIND_MANIFEST: _manifest_document,
    KIND_JS: _js_document,
}


def _is_route(name: str) -> bool:
    last = name.rsplit(".", 1)[-1]
    return last == "route" or last.endswith("_route")


def _decorator_block(lines: list[str], def_line: int) -> str | None:
    def_text = _line(lines, def_line)
    indent = def_text[: len(def_text) - len(def_text.lstrip())]
    width = len(indent)
    top = None
    i = def_line - 1
    while i >= max(0, def_line - 300):
        raw = _line(lines, i)
        s = raw.strip()
        if s and not s.startswith("#"):
            ind = len(raw) - len(raw.lstrip())
            if ind < width:
                break
            if ind == width:
                if s.startswith("@"):
                    top = i
                elif s[0] not in ")]}":
                    break
        i -= 1
    if top is None:
        return None
    return "\n".join(raw[width:] if raw.startswith(indent) else raw.lstrip() for raw in (_line(lines, n) for n in range(top, def_line))) + "\n"


_ASSIGNMENT = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=]*)?=\s*(.+)$")


def _literal_assignments(lines: Iterable[str]) -> dict:
    out = {}
    for line in lines:
        m = _ASSIGNMENT.match(line.rstrip())
        if m:
            try:
                out[m.group(1)] = ast.literal_eval(m.group(2).strip())
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                pass
    return out


# @route args are re-parsed from source: facts keep only str literals (others are '?')
class _PathResolver:
    def __init__(self, server: Any, reg: Any, path: str, facts: dict, lines: list[str], modules: dict):
        self.server = server
        self.reg = reg
        self.lines = lines
        self.modules = modules
        self.class_attrs: dict = {}
        self._local: dict | None = None
        # local name -> (absolute dotted module, imported name; None for 'import x as y')
        self.imports: dict[str, tuple[str, str | None]] = {}
        dotted = (reg.py_dotted_name(path) if reg is not None else None) or ""
        package = dotted if os.path.basename(path) == "__init__.py" else dotted.rpartition(".")[0]
        for imp in facts.get(IMPORTS) or ():
            module, name, asname, level = imp[I_MODULE], imp[I_NAME], imp[I_ASNAME], imp[I_LEVEL]
            if level:
                base = package
                for _ in range(level - 1):
                    base = base.rpartition(".")[0]
                module = f"{base}.{module}" if module else base
            if name is None:
                if asname:
                    self.imports[asname] = (module, None)
            elif name != "*":
                self.imports[asname or name] = (module, name)

    def for_class(self, cls: tuple) -> _PathResolver:
        values = {}
        for a in cls[K_ATTRS]:
            try:
                values[a[A_NAME]] = ast.literal_eval(a[A_VALUE])
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                pass
        self.class_attrs = values
        return self

    def base_name(self, text: str) -> str:
        if "." in text:
            return text.rsplit(".", 1)[-1]
        imp = self.imports.get(text)
        return imp[1] if imp is not None and imp[1] else text

    def strings(self, node: Any) -> list[str]:
        if isinstance(node, (ast.List, ast.Tuple)):
            return [s for e in node.elts if not isinstance(e, ast.Starred) for s in self.strings(e)]
        value = self.value(node)
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [v for v in value if isinstance(v, str)]
        return []

    def value(self, node: Any) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.class_attrs:
                return self.class_attrs[node.id]
            if self._local is None:
                self._local = _literal_assignments(self.lines)
            if node.id in self._local:
                return self._local[node.id]
            imp = self.imports.get(node.id)
            return self.constants(imp[0]).get(imp[1]) if imp is not None and imp[1] else None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            imp = self.imports.get(node.value.id)
            if imp is None:
                return None
            return self.constants(f"{imp[0]}.{imp[1]}" if imp[1] else imp[0]).get(node.attr)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.FormattedValue) and v.conversion == -1 and v.format_spec is None:
                    v = v.value
                part = self.value(v)
                if not isinstance(part, str):
                    return None
                parts.append(part)
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(node.left), self.value(node.right)
            return left + right if isinstance(left, str) and isinstance(right, str) else None
        return None

    def constants(self, module: str) -> dict:
        hit = self.modules.get(module)
        if hit is None:
            path = self.reg.py_module_file(module) if self.reg is not None else None
            hit = self.modules[module] = _literal_assignments(_read_lines(self.server, path)) if path else {}
        return hit

    def decorators(self, def_line: int) -> list[tuple[list[str], dict]] | None:
        block = _decorator_block(self.lines, def_line)
        if block is None:
            return None
        try:
            tree = ast.parse(block + "def _f():\n    pass\n")
        except (SyntaxError, ValueError):
            return None
        fn = tree.body[0] if tree.body else None
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        out = []
        for dec in fn.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            func = dec.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if not _is_route(name):
                continue
            target = dec.args[0] if dec.args else next((k.value for k in dec.keywords if k.arg == "route"), None)
            kw = {}
            for k in dec.keywords:
                if k.arg in ROUTE_KWARGS:
                    value = self.value(k.value)
                    if value is not None:
                        kw[k.arg] = value
            out.append((self.strings(target) if target is not None else [], kw))
        return out


class _Route:
    __slots__ = ("path", "module", "cls", "bases", "method", "paths", "info", "parent", "dead")

    def __init__(self, path: str, module: str | None, cls: str, bases: tuple, method: tuple, paths: tuple, info: dict, dead: bool = False):
        self.path = path
        self.module = module
        self.cls = cls
        self.bases = bases
        self.method = method
        self.paths = paths
        self.info = info
        self.parent: _Route | None = None  # route inherited through @route() without paths
        self.dead = dead  # file never imported by its module: Odoo does not serve it


def _file_routes(facts: dict, resolver: _PathResolver) -> tuple[list[tuple], list[tuple[str, tuple]]]:
    routes = []
    classes = []
    for cls in facts.get(CLASSES) or ():
        bases = tuple(resolver.base_name(b) for b in cls[K_BASES])
        classes.append((cls[K_NAME], bases))
        resolver.for_class(cls)
        for m in cls[K_METHODS]:
            decs = [d for d in m[M_DECORATORS] if _is_route(d[0])]
            if not decs:
                continue
            parsed = resolver.decorators(m[M_LINE])
            if parsed is None:
                parsed = [([a for a in args if a != "?"], {}) for _, args in decs]
            paths: list[str] = []
            info: dict = {}
            for ps, kw in parsed:
                paths += [p for p in ps if p not in paths]
                for k, v in kw.items():
                    info.setdefault(k, v)
            routes.append((cls[K_NAME], bases, m, tuple(paths), info))
    return routes, classes


def _read_lines(server: Any, path: str) -> list[str]:
    doc = server.documents.get(path) if server is not None else None
    if doc is not None:
        return doc.lines
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace").split("\n")
    except OSError:
        return []


def _url_path(query: str) -> str:
    q = _URL_HOST.sub("", query.strip()) or "/"
    return q.split("?", 1)[0].split("#", 1)[0]


# entry: first five fields are the sort key (customer first, load rank, path, line, col)
# buckets sync per changed file: each overlay update (edited buffer) bumps the registry generation
E_CUSTOM, E_RANK, E_PATH, E_LINE, E_COL, E_END, E_CAT, E_NAME, E_KIND, E_DATA = range(10)
_entry_key = itemgetter(E_CUSTOM, E_RANK, E_PATH, E_LINE, E_COL)
FUZZY_SCAN = 1000


def _key(name: str) -> str:
    return name if name.islower() else name.lower()


# groups: name -> entry, or list when several share it (no list per name: 200k+ xml ids)
# local buckets: 'module.name' ids also match by their bare local part
class _Bucket:
    __slots__ = ("groups", "names", "best", "hay", "starts", "local", "_dirty", "_renamed", "_unsorted")

    def __init__(self, local: bool = False):
        self.groups: dict[str, Any] = {}
        self.names: list[str] = []
        self.best: list[tuple] = []
        self.hay = "\n"
        self.starts = array("l", [1])
        self.local = local
        self._dirty: set[str] = set()
        self._renamed = False
        self._unsorted = False

    def get(self, name: str) -> list[tuple]:
        v = self.groups.get(name)
        return [] if v is None else v if type(v) is list else [v]

    def items(self) -> Iterator[tuple[str, list[tuple]]]:
        for name, v in self.groups.items():
            yield name, v if type(v) is list else [v]

    def add(self, name: str, e: tuple) -> None:
        v = self.groups.get(name)
        if v is None:
            self.groups[name] = e
            self._renamed = True
            return
        if type(v) is list:
            v.append(e)
        else:
            self.groups[name] = [v, e]
        self._dirty.add(name)

    def discard(self, name: str, e: tuple) -> None:
        v = self.groups.get(name)
        if v is None:
            return
        if type(v) is list:
            for i, x in enumerate(v):
                if x is e:
                    del v[i]
                    break
        elif v is e:
            self.groups[name] = []  # dropped by seal() unless an entry comes back
        self._dirty.add(name)

    def seal(self) -> _Bucket:
        groups = self.groups
        if self._unsorted:
            for v in groups.values():
                if type(v) is list:
                    v.sort(key=_entry_key)
            self._unsorted = False
            self._renamed = True
        for n in self._dirty:
            v = groups.get(n)
            if type(v) is not list:
                continue
            if not v:
                del groups[n]
                self._renamed = True
            elif len(v) == 1:
                groups[n] = v[0]
            else:
                v.sort(key=_entry_key)
        if self._renamed:
            names = sorted(groups)
            self.names = names
            self.best = [v[0] if type(v) is list else v for v in map(groups.__getitem__, names)]
            self.hay = "\n" + "\n".join(names) + "\n"
            self.starts = array("l", accumulate((len(n) + 1 for n in names), initial=1))
        else:
            for n in self._dirty:
                v = groups.get(n)
                if v is not None:
                    self.best[bisect_left(self.names, n)] = v[0] if type(v) is list else v
        self._dirty.clear()
        self._renamed = False
        return self

    def exact(self, q: str) -> list[int]:
        names = self.names
        i = bisect_left(names, q)
        out = [i] if i < len(names) and names[i] == q else []
        if self.local and "." not in q:
            out += self._scan("." + q + "\n", first_dot=True)
        return out

    def prefix(self, q: str) -> list[int]:
        names = self.names
        i = bisect_left(names, q)
        out = []
        while i < len(names) and names[i].startswith(q):
            out.append(i)
            i += 1
        if self.local and "." not in q:
            out += self._scan("." + q, first_dot=True)
        return out

    def substring(self, q: str) -> list[int]:
        return self._scan(q)

    def fuzzy_scored(self, q: str) -> list[tuple[int, tuple]]:
        rx = re.compile(re.escape(q[0]) + "".join(f"[^\n{re.escape(c)}]*{re.escape(c)}" for c in q[1:]))
        grouped = _subsequence_regex(q)  # positions only on hits: groups slow a scan down
        hay, starts, best = self.hay, self.starts, self.best
        out: list[tuple[int, tuple]] = []
        pos = 0
        while len(out) < FUZZY_SCAN:
            m = rx.search(hay, pos)
            if m is None:
                break
            i = bisect_right(starts, m.start()) - 1
            orig = best[i][E_NAME]
            if len(orig) != starts[i + 1] - starts[i] - 1:
                orig = None  # key not a char-for-char lowercase of the name (labels, case folding)
            out.append((i, _subsequence_quality(grouped.match(hay, m.start()), hay, starts[i], orig, len(q))))
            pos = starts[i + 1]
        return out

    def near(self, variants: set[str]) -> list[int]:
        names = self.names
        return sorted(bisect_left(names, n) for n in self.groups.keys() & variants)

    def _scan(self, needle: str, first_dot: bool = False) -> list[int]:
        hay, starts, names = self.hay, self.starts, self.names
        out = []
        pos = hay.find(needle)
        while pos >= 0:
            i = bisect_right(starts, pos) - 1
            if not first_dot or pos - starts[i] == names[i].find("."):
                out.append(i)
            pos = hay.find(needle, starts[i + 1])
        return out


_MATCHERS: dict[int, Callable[[_Bucket, str], list[int]]] = {
    EXACT: _Bucket.exact,
    PREFIX: _Bucket.prefix,
    SUBSTRING: _Bucket.substring,
}


class _Family:
    __slots__ = ("buckets", "files")

    def __init__(self, buckets: dict[str, _Bucket]):
        self.buckets = buckets
        self.files: dict[str, list[tuple]] = {}

    def load(self, feeds: Iterable[tuple[str, list[tuple]]]) -> None:
        groups = {name: b.groups for name, b in self.buckets.items()}
        for path, entries in feeds:
            if not entries:
                continue
            self.files[path] = entries
            for e in entries:
                g = groups[e[E_CAT]]
                name = _key(e[E_NAME])
                v = g.get(name)
                if v is None:
                    g[name] = e
                elif type(v) is list:
                    v.append(e)
                else:
                    g[name] = [v, e]
        for b in self.buckets.values():
            b._unsorted = True

    def put(self, path: str, entries: list[tuple] | None) -> bool:
        old = self.files.pop(path, None)
        buckets = self.buckets
        if old:
            for e in old:
                buckets[e[E_CAT]].discard(_key(e[E_NAME]), e)
        if entries:
            self.files[path] = entries
            for e in entries:
                buckets[e[E_CAT]].add(_key(e[E_NAME]), e)
        return bool(old or entries)


class _Index:
    def __init__(self, env: Any, registry: Any):
        self.env = env
        # weak: lives in registry.__dict__; a strong ref would keep evicted frozen registries alive
        self.reg = weakref.proxy(registry)
        self.server: Any = None
        self.major = _major(env)
        self.generation: int | None = None
        self.mkeys: dict[str, tuple[int, int]] = {}
        self.module_dirs: dict | None = None
        self.loaded: Any = None
        self.data_order: Any = None
        self.snapshot: dict[str, dict] = {}
        self.families: dict[str, _Family] = {}
        self.derived: dict[str, _Bucket] = {}
        self.actions: list[tuple] = []
        self.route_cache: dict[str, tuple] = {}
        self.routes: list[_Route] | None = None
        self.route_rx: dict[str, Any] = {}
        self.implicit: tuple[int, dict[str, str]] | None = None
        self._unloaded_dirs: dict[str, bool] = {}

    def sync(self) -> None:
        reg = self.reg
        if self.generation == reg.generation:
            return
        custom = {os.path.normpath(r) for r in getattr(self.env, "custom_roots", None) or ()}
        mkeys = {m.name: (0 if m.root in custom else 1, m.rank) for m in reg.modules()}
        files = reg.files
        loaded, data_order = getattr(reg, "_loaded", None), getattr(reg, "_data_order", None)
        # set_modules / full _rebuild (import reachability, same facts) / manifest change: start over
        if mkeys != self.mkeys or reg.module_dirs is not self.module_dirs or loaded is not self.loaded or data_order is not self.data_order:
            self.mkeys = mkeys
            self.module_dirs = reg.module_dirs
            self.loaded, self.data_order = loaded, data_order
            self.families.clear()
            self.derived.clear()
            self.route_cache.clear()
            self._unloaded_dirs.clear()
        else:
            old = self.snapshot
            changed = [p for p, f in files.items() if old.get(p) is not f]
            changed += [p for p in old if p not in files]
            for name, fam in self.families.items():
                _, _, kinds, contribute, _ = _FAMILIES[name]
                touched = False
                for p in changed:
                    facts = files.get(p)
                    feed = contribute(self, p, facts) if facts is not None and facts.get(KIND) in kinds else None
                    touched = fam.put(p, feed) or touched
                if touched:
                    for cat, source in _DERIVED_FROM.items():
                        if source == name:
                            self.derived.pop(cat, None)
            self.derived.pop("module", None)
            for p in changed:
                if p not in files:
                    self.route_cache.pop(p, None)
        self.snapshot = dict(files)
        self.generation = reg.generation
        self.routes = None

    def mkey(self, module: str | None) -> tuple[int, int]:
        return self.mkeys.get(module, _NO_MODULE) if module else _NO_MODULE

    def record_rank(self, rank: int, path: str) -> int:
        # same order as Registry.records(): module rank, then manifest data + demo position
        order = self.data_order
        return rank * (_UNLISTED << 1) + (order.get(path, _UNLISTED) if order else _UNLISTED)

    def implicit_models(self) -> dict[str, str]:
        reg = self.reg
        hit = self.implicit
        if hit is None or hit[0] != reg.generation:
            out: dict[str, str] = {}
            for m in sorted(reg.model_names()):
                out.setdefault(m.replace(".", "_"), m)
            hit = self.implicit = (reg.generation, out)
        return hit[1]

    def family(self, name: str) -> _Family:
        fam = self.families.get(name)
        if fam is None:
            buckets, local, kinds, contribute, bulk = _FAMILIES[name]
            fam = _Family({b: _Bucket(b in local) for b in buckets})
            if bulk is not None:
                fam.load(bulk(self))
            else:
                fam.load((path, contribute(self, path, facts)) for path, facts in self.reg.files.items() if facts.get(KIND) in kinds)
            self.families[name] = fam
        return fam

    def bucket(self, cat: str) -> _Bucket:
        fam = _FAMILY_OF.get(cat)
        if fam is not None:
            return self.family(fam).buckets[cat].seal()
        b = self.derived.get(cat)
        if b is None:
            _DERIVE[cat](self)
            b = self.derived[cat]
        return b

    def unloaded(self, path: str, module: str | None) -> bool:
        info = self.reg.module(module)
        if info is None:
            return False
        d = os.path.dirname(path)
        hit = self._unloaded_dirs.get(d)
        if hit is None:
            hit = self._unloaded_dirs[d] = bool(TEST_DIRS.intersection(os.path.relpath(d, info.path).split(os.sep)))
        return hit

    def member_entry(self, cat: str, d: Any) -> tuple:
        c = d.cls
        kind = SK_FIELD if cat == "field" else SK_METHOD if cat == "method" else _attr_kind(d.facts)
        return (*self.mkey(c.module), c.path, d.line, d.col, d.end_col, cat, d.name, kind, (c.model, c, d.facts, d.delegated_via))

    def record_entry(self, r: Any) -> tuple:
        cu, rk = self.mkey(r.module)
        return (cu, self.record_rank(rk, r.path), r.path, r.line, r.col, r.end_col, "record", r.xmlid, SK_OBJECT, r.facts)

    def all_routes(self, server: Any) -> list[_Route]:
        if self.routes is not None:
            return self.routes
        reg = self.reg
        out: list[_Route] = []
        graph: dict[str, set[str]] = defaultdict(set)
        modules: dict[str, dict] = {}
        for path, facts in reg.files.items():
            if facts.get(KIND) != KIND_PY:
                continue
            hit = self.route_cache.get(path)
            if hit is None or hit[0] is not facts:
                found: tuple = ([], [])
                classes = facts.get(CLASSES)
                if classes and any(_is_route(d[0]) for c in classes for m in c[K_METHODS] for d in m[M_DECORATORS]):
                    if not self.unloaded(path, reg.module_of_path(path)):
                        found = _file_routes(facts, _PathResolver(server, reg, path, facts, _read_lines(server, path), modules))
                hit = self.route_cache[path] = (facts, found)
            routes, classes = hit[1]
            if routes:
                module = reg.module_of_path(path)
                dead = not reg.is_loaded(path)
                out += [_Route(path, module, cls, bases, m, paths, info, dead) for cls, bases, m, paths, info in routes]
            for name, bases in classes:
                graph[name].update(bases)
        self._inherit_routes(out, graph)
        self.routes = out
        return out

    def _inherit_routes(self, routes: list[_Route], graph: dict[str, set[str]]) -> None:
        # Odoo: a path-less @route() on an override reuses the overridden method's route
        named: dict[str, list[_Route]] = defaultdict(list)
        for r in routes:
            if r.paths:
                named[r.method[M_NAME]].append(r)
        for _ in range(4):
            changed = False
            for r in routes:
                if r.paths:
                    continue
                parent = self._overridden_route(r, named.get(r.method[M_NAME], ()), graph)
                if parent is None:
                    continue
                r.paths, r.parent = parent.paths, parent
                r.info = {**parent.info, **r.info}
                named[r.method[M_NAME]].append(r)
                changed = True
            if not changed:
                return

    def _overridden_route(self, r: _Route, candidates: Iterable[_Route], graph: dict[str, set[str]]) -> _Route | None:
        scope = self.reg.closure(r.module) if r.module else None
        candidates = [p for p in candidates if p is not r and (scope is None or p.module in scope)]
        frontier, seen = set(r.bases), set()
        while frontier and candidates:
            found = [p for p in candidates if p.cls in frontier]
            if found:
                return max(found, key=lambda p: (self.mkey(p.module)[1], p.path, p.method[M_LINE]))
            seen |= frontier
            frontier = {b for name in frontier for b in graph.get(name, ()) if b not in seen}
        return None

    def route_regex(self, url: str) -> Any:
        rx = self.route_rx.get(url)
        if rx is None:
            parts = []
            for part in re.split(r"(<[^<>]*>)", url):
                if part.startswith("<") and part.endswith(">"):
                    parts.append(".+" if part[1:].startswith("path:") else "[^/]+")
                else:
                    parts.append(re.escape(part))
            rx = self.route_rx[url] = re.compile("".join(parts) + "/?")
        return rx


def _attr_kind(attr: tuple) -> int:
    return SK_CONSTANT if attr[A_KIND] in CONSTANT_ATTRS else SK_VARIABLE


def _class_entries(ix: _Index, path: str, refs: list[tuple[str, Any]]) -> list[tuple]:
    out = []
    for model, c in refs:
        cu, rk = ix.mkey(c.module)
        k = c.facts
        if model:
            out.append((cu, rk, path, k[K_LINE], k[K_COL], k[K_END_COL], "model", model, SK_CLASS, c))
        for f in k[K_FIELDS]:
            out.append((cu, rk, path, f[F_LINE], f[F_COL], f[F_END_COL], "field", f[F_NAME], SK_FIELD, (model, c, f, None)))
        for m in k[K_METHODS]:
            out.append((cu, rk, path, m[M_LINE], m[M_COL], m[M_END_COL], "method", m[M_NAME], SK_METHOD, (model, c, m, None)))
        for a in k[K_ATTRS]:
            if a[A_NAME] not in MODEL_DECL_ATTRS:
                out.append((cu, rk, path, a[A_LINE], a[A_COL], a[A_END_COL], "attr", a[A_NAME], _attr_kind(a), (model, c, a, None)))
    return out


def _classes_bulk(ix: _Index) -> Iterable[tuple[str, list[tuple]]]:
    reg = ix.reg
    by_path: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    for model in reg.model_names():
        for c in reg.model_classes(model):
            by_path[c.path].append((model, c))
    for c in reg.framework_classes():
        by_path[c.path].append(("", c))
    for path, refs in by_path.items():
        yield path, _class_entries(ix, path, refs)


def _classes_of_file(ix: _Index, path: str, facts: dict) -> list[tuple]:
    reg = ix.reg
    models: set[str] = set()
    framework = False
    for k in facts.get(CLASSES) or ():
        mi = k[K_MODEL]
        if not mi:
            continue
        if mi.get(MI_KIND) == "framework":
            framework = True
        else:
            model = model_class_info(k, ix.major)[0]
            if model:
                models.add(model)
    refs = [(m, c) for m in sorted(models) for c in reg.model_classes(m) if c.path == path]
    if framework:
        refs += [("", c) for c in reg.framework_classes() if c.path == path]
    return _class_entries(ix, path, refs)


def _records_of_file(ix: _Index, path: str, facts: dict) -> list:
    records = facts.get(RECORDS)
    if not records:
        return []
    module = ix.reg.module_of_path(path)
    cu, rk = ix.mkey(module)
    rk = ix.record_rank(rk, path)
    prefix = f"{module}." if module else ""  # same as Registry.qualify_xmlid
    out = []
    for rec in records:
        xid = rec[X_ID]
        if not xid:
            continue
        if rec[X_TAG] == "qweb":
            out.append((cu, rk, path, rec[X_LINE], rec[X_COL], rec[X_END_COL], "template", xid, SK_OBJECT, rec))
        else:
            full = xid if "." in xid else prefix + xid
            out.append((cu, rk, path, rec[X_LINE], rec[X_COL], rec[X_END_COL], "record", full, SK_OBJECT, rec))
    return out


def _python_of_file(ix: _Index, path: str, facts: dict) -> list:
    # unimported file: model classes dropped (never registered), other code kept, flagged
    reg = ix.reg
    module = reg.module_of_path(path)
    if module and ix.unloaded(path, module):
        return []
    cu, rk = ix.mkey(module)
    dotted = reg.py_dotted_name(path) or os.path.basename(path)
    dead = module is not None and not reg.is_loaded(path)
    note = ", not imported by its module" if dead else ""
    by_line = {k[K_LINE]: k for k in facts.get(CLASSES) or ()}
    out = []
    for s in facts.get(SYMBOLS) or ():
        line, col = s[S_LINE], s[S_COL]
        if s[S_KIND] == "class":
            cls = by_line.get(line)
            if dead and cls is not None and cls[K_MODEL] and cls[K_MODEL].get(MI_KIND) != "framework":
                continue
            out.append((cu, rk, path, line, col, s[S_END_COL], "pyclass", s[S_NAME], SK_CLASS, (dotted, cls, note)))
        elif s[S_KIND] == "def":
            out.append((cu, rk, path, line, col, s[S_END_COL], "pyfunc", s[S_NAME], SK_FUNCTION, (dotted, None, note)))
    for k in by_line.values():
        if k[K_MODEL]:
            continue
        for m in k[K_METHODS]:
            out.append((cu, rk, path, m[M_LINE], m[M_COL], m[M_END_COL], "pyfunc", m[M_NAME], SK_METHOD, (dotted, k[K_NAME], note)))
    return out


def _js_of_file(ix: _Index, path: str, facts: dict) -> list:
    reg = ix.reg
    module = reg.module_of_path(path)
    cu, rk = ix.mkey(module)
    info = reg.module(module)
    jsname = os.path.basename(path)
    if info is not None:
        rel = os.path.relpath(path, info.path).replace(os.sep, "/")
        if rel.startswith("static/src/") and rel.endswith(".js"):
            jsname = f"@{module}/{rel[len('static/src/') : -3]}"
    out = []
    for s in facts.get(SYMBOLS) or ():
        cat, kind = ("jsclass", SK_CLASS) if s[S_KIND] == "class" else ("jsfunc", SK_FUNCTION) if s[S_KIND] == "def" else ("jsvar", SK_VARIABLE)
        out.append((cu, rk, path, s[S_LINE], s[S_COL], s[S_END_COL], cat, s[S_NAME], kind, jsname))
    return out


# family -> (buckets, 'module.name' buckets, facts kinds, per-file entries, bulk loader or None)
_FAMILIES: dict[str, tuple] = {
    "classes": (("model", "field", "method", "attr"), (), (KIND_PY,), _classes_of_file, _classes_bulk),
    "records": (("record", "template"), ("record", "template"), (KIND_XML, KIND_CSV), _records_of_file, None),
    "python": (("pyclass", "pyfunc"), (), (KIND_PY,), _python_of_file, None),
    "js": (("jsclass", "jsfunc", "jsvar"), (), (KIND_JS,), _js_of_file, None),
}
_FAMILY_OF = {b: name for name, spec in _FAMILIES.items() for b in spec[0]}


def _derive_heads(ix: _Index) -> None:
    heads = _Bucket()
    for name, entries in ix.bucket("model").items():
        for e in [e for e in entries if e[E_DATA].is_definition] or entries[:1]:
            heads.add(name, e)
    ix.derived["modelhead"] = heads.seal()


def _derive_record_kinds(ix: _Index) -> None:
    views, actions, menus, qweb = _Bucket(True), _Bucket(True), _Bucket(True), _Bucket(True)
    action_list = []
    reg = ix.reg
    for name, entries in ix.bucket("record").items():
        for e in entries:
            rec = e[E_DATA]
            model = rec[X_MODEL] or ""
            info = rec[X_INFO] or {}
            label = info.get("name")
            label = " ".join(label.split()).lower() if isinstance(label, str) else ""
            if model == "ir.ui.view":
                views.add(name, e)
                if rec[X_TAG] == "template" or info.get("view_type") == "qweb":
                    qweb.add(name, e)
            elif model == "ir.ui.menu":
                menus.add(name, e)
                if not label and info.get("action"):
                    label = (_action_label(reg, info["action"], reg.module_of_path(e[E_PATH])) or "").lower()
                if label:
                    menus.add(label, e)
            elif model.startswith("ir.actions.") or model == "ir.cron":
                actions.add(name, e)
                action_list.append(e)
                if label:
                    actions.add(label, e)
    for name, entries in ix.bucket("template").items():
        for e in entries:
            qweb.add(name, e)
    ix.derived.update(view=views.seal(), action=actions.seal(), menu=menus.seal(), qweb=qweb.seal())
    ix.actions = action_list


def _derive_modules(ix: _Index) -> None:
    reg = ix.reg
    b = _Bucket()
    for info in reg.modules():
        facts = reg.file_facts(info.manifest) or {}
        pos = (facts.get(MANIFEST_KEYS) or {}).get("name") or (0, 0, 0)
        b.add(info.name.lower(), (*ix.mkey(info.name), info.manifest, pos[0], pos[1], pos[2], "module", info.name, SK_MODULE, info))
    ix.derived["module"] = b.seal()


_DERIVE: dict[str, Callable[[_Index], None]] = {
    "modelhead": _derive_heads,
    "view": _derive_record_kinds,
    "action": _derive_record_kinds,
    "menu": _derive_record_kinds,
    "qweb": _derive_record_kinds,
    "module": _derive_modules,
}
_DERIVED_FROM = {"modelhead": "classes", "view": "records", "action": "records", "menu": "records", "qweb": "records"}

_INDEX_ATTR = "_odoo_lens_symbols"


def _index(env: Any, registry: Any, server: Any = None) -> _Index:
    ix = registry.__dict__.get(_INDEX_ATTR)
    if ix is None or ix.env is not env:
        ix = registry.__dict__[_INDEX_ATTR] = _Index(env, registry)
    ix.server = server
    ix.sync()
    return ix


def _module_of(ix: _Index, e: tuple) -> str:
    return ix.reg.module_of_path(e[E_PATH]) or "odoo"


def _c_model(ix: _Index, e: tuple) -> str:
    c = e[E_DATA]
    if not c.is_definition:
        return f"{c.module or 'odoo'} (extends)"
    flavor = {"AbstractModel": ", abstract", "TransientModel": ", transient"}.get(c.model_info.get(MI_KIND), "")
    return f"{c.module or 'odoo'} (defines{flavor})"


def _c_member(ix: _Index, e: tuple) -> str:
    model, cls, m, via = e[E_DATA]
    cat = e[E_CAT]
    if cat == "field":
        args = m[F_ARGS] or {}
        comodel = m[F_COMODEL] or (args.get("comodel_name") if isinstance(args.get("comodel_name"), str) else None)
        detail = _field_summary(m[F_TYPE], comodel, args)
    elif cat == "method":
        detail = "method"
    else:
        detail = "attribute" if m[A_KIND] == "attr" else m[A_KIND]
    text = f"{model or cls.name} ({cls.module or 'odoo'}) {detail}"
    return f"{text}, via {via}" if via else text


def _c_record(ix: _Index, e: tuple) -> str:
    rec = e[E_DATA]
    module = _module_of(ix, e)
    info = rec[X_INFO] or {}
    if rec[X_TAG] == "qweb":
        inherit = info.get("inherit")
        if info.get("mode") == "extension":
            return f"owl template extension ({module}) of {inherit or '?'}"
        return f"owl template ({module})" + (f", inherits {inherit}" if inherit else "")
    model = rec[X_MODEL] or rec[X_TAG]
    reg = ix.reg
    if rec[X_TAG] == "csv" and model in ACL_MODELS:
        lines = _doc_lines(ix.server, e[E_PATH])
        detail = _AclColumns(lines).detail(reg, rec, _line(lines, rec[X_LINE]), model, module)
        name = info.get("name")
        if isinstance(name, str) and name.strip():
            detail = f'"{_clip(name, 40)}": {detail}'
    else:
        detail = _record_detail(
            rec, with_model=False, module=module, target=_record_target(reg, rec, module), label=_menu_label(reg, rec, module),
            binding=_binding_target(reg, rec, module),
        )
    head = f"{model} ({module})"
    if not detail:
        return head
    return _clip(head + ("" if detail.startswith(",") else " ") + detail, 160)


def _doc_lines(server: Any, path: str) -> list[str]:
    documents = getattr(server, "documents", None)
    if documents is not None:
        return documents.lines(path) or []
    return _read_lines(None, path)


def _c_module(ix: _Index, e: tuple) -> str:
    info = e[E_DATA]
    name = info.values.get("name")
    text = f'"{_clip(name, 50)}"' if isinstance(name, str) else "module"
    deps = [d for d in info.depends if d != "base"] or list(info.depends)
    return text + (" depends " + _clip(", ".join(deps), 100) if deps else "")


def _c_pyclass(ix: _Index, e: tuple) -> str:
    dotted, cls, note = e[E_DATA]
    if cls is None:
        return dotted + note
    mi = cls[K_MODEL]
    if mi and mi.get(MI_KIND) != "framework":
        model, is_def, _ = model_class_info(cls, ix.major)
        if model:
            return f"{dotted}: {model} ({'defines' if is_def else 'extends'}){note}"
    return (f"{dotted} ({_clip(', '.join(cls[K_BASES]), 60)})" if cls[K_BASES] else dotted) + note


def _c_pyfunc(ix: _Index, e: tuple) -> str:
    dotted, cls_name, note = e[E_DATA]
    return (f"{cls_name} ({dotted})" if cls_name else dotted) + note


def _c_js(ix: _Index, e: tuple) -> str:
    return e[E_DATA]


def _c_route(ix: _Index, e: tuple) -> str:
    url, info, parent, dead = e[E_DATA]
    text = f"route {url} ({_module_of(ix, e)})"
    for k in ("type", "auth"):
        if isinstance(info.get(k), str):
            text += f" {k}={info[k]}"
    if parent is not None:
        text += f", overrides {parent.cls}.{parent.method[M_NAME]} ({parent.module or 'odoo'})"
    if dead:
        text += ", controller file not imported by its module: route never registered"
    return text


def _c_marker(ix: _Index, e: tuple) -> str:
    return e[E_DATA]


_CONTAINERS: dict[str, Callable[[_Index, tuple], str]] = {
    "model": _c_model,
    "field": _c_member,
    "method": _c_member,
    "attr": _c_member,
    "record": _c_record,
    "template": _c_record,
    "module": _c_module,
    "pyclass": _c_pyclass,
    "pyfunc": _c_pyfunc,
    "jsclass": _c_js,
    "jsfunc": _c_js,
    "jsvar": _c_js,
    "route": _c_route,
    "marker": _c_marker,
}


def workspace_symbols(server: Any, env: Any, registry: Any, query: str, token: Any) -> list[dict]:
    # query grammar: docs/ARCHITECTURE.md section 9
    if registry is None:
        return []
    q = _clean_query(query)
    if not q:
        return []
    search = _Search(server, _index(env, registry, server), token, registry=registry)
    search.run(q)
    return search.symbols()


def _clean_query(query: str | None) -> str:
    # Claude often queries code as written (def x, env['m'], @route('/x'), self.x): map it to the grammar
    q = " ".join((query or "").split()).strip("'\"`").strip()
    m = _CODE_DEF.match(q)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    m = _CODE_ENV.search(q)
    if m:
        return m.group(2) + "".join(m.group(3).split())
    m = _CODE_REF.search(q)
    if m:
        return m.group(2)
    if q.startswith("@"):
        m = _CODE_ROUTE.match(q[1:])
        if m:
            return m.group(2)
        q = q[1:]
    for head in ("self.", "cls."):
        if q.startswith(head) and len(q) > len(head):
            q = q[len(head) :]
    m = _CODE_CALL.match(q)
    if m:
        q = m.group(1)
    return q.strip()


def _tier(q: str, name: str) -> int | None:
    if name == q:
        return EXACT
    if name.startswith(q):
        return PREFIX
    if q in name:
        return SUBSTRING
    it = iter(name)
    if all(ch in it for ch in q):
        return FUZZY
    return None


def _one_typo(q: str) -> set[str]:
    out = set()
    alphabet = _NEAR_ALPHABET
    for i in range(len(q) + 1):
        a, b = q[:i], q[i:]
        for c in alphabet:
            out.add(a + c + b)
        if b:
            out.add(a + b[1:])
            for c in alphabet:
                out.add(a + c + b[1:])
            if len(b) > 1:
                out.add(a + b[1] + b[0] + b[2:])
    out.discard(q)
    return out


def _typo_rank(q: str, name: str) -> int:
    # likeliest slips first: swap, extra char, missing char, wrong char
    if len(name) < len(q):
        return 1
    if len(name) > len(q):
        return 2
    return 0 if sum(1 for a, b in zip(q, name) if a != b) == 2 else 3


def _subsequence_regex(q: str) -> Any:
    # group k = k-th char of q; leftmost match within one name (line)
    return re.compile("".join((f"[^\n{re.escape(c)}]*" if n else "") + f"({re.escape(c)})" for n, c in enumerate(q)))


def _subsequence_quality(m: Any, text: str, start: int, orig: str | None, size: int) -> tuple:
    # -> (runs not at a word start, chars skipped, name length): smaller is better
    loose = 0
    prev = -2
    for k in range(1, size + 1):
        p = m.start(k)
        if p != prev + 1:
            word = p == start or text[p - 1] in _WORD_SEPS
            if not word and orig is not None:
                at = p - start
                word = orig[at].isupper() and not orig[at - 1].isupper()
            loose += not word
        prev = p
    return (loose, m.end() - m.start() - size, len(orig) if orig is not None else 0)


def _subsequence_ok(q: str, quality: tuple) -> bool:
    # abbreviation (every run starts a word) or compact match; scattered chars are noise
    return quality[0] == 0 or quality[1] <= max(2, len(q) // 3)


def _creator_first(entries: list[tuple]) -> list[tuple]:
    # the first-loaded record creates the xml id
    if len(entries) < 2:
        return entries
    first = min(entries, key=itemgetter(E_RANK, E_PATH, E_LINE))
    return [first] + [e for e in entries if e is not first]


def _chain_of(cat: str) -> str | None:
    return "member" if cat in MEMBER_CATS else "record" if cat in RECORD_CATS else None


def _top(b: _Bucket, idxs: list[int], k: int, chain: str | None = None) -> list[tuple]:
    if not idxs or k <= 0:
        return []
    if len(idxs) > k:
        best = b.best
        idxs = heapq.nsmallest(k, idxs, key=lambda i: _entry_key(best[i]))
    out: list[tuple] = []
    for i in idxs:
        out.extend(b.get(b.names[i]))
    out.sort(key=_entry_key)
    if chain is None:
        return out
    chains: dict[Any, list[tuple]] = {}
    if chain == "member":
        for e in out:  # in key order: chains come out ordered by their best entry
            model, cls = e[E_DATA][0], e[E_DATA][1]
            chains.setdefault((e[E_NAME], model or cls.name), []).append(e)
        return [e for c in chains.values() for e in sorted(c, key=lambda e: (not e[E_DATA][1].is_definition, _entry_key(e)))]
    for e in out:
        chains.setdefault(e[E_NAME], []).append(e)
    return [e for c in chains.values() for e in _creator_first(c)]


def _quotas(sizes: list[int], weights: list[int], room: int) -> list[int]:
    quota = [0] * len(sizes)
    active = [i for i, n in enumerate(sizes) if n]
    while room > 0 and active:
        total = sum(weights[i] for i in active) or 1
        given = 0
        for i in active:
            take = min(max(1, room * weights[i] // total), sizes[i] - quota[i], room - given)
            quota[i] += take
            given += take
        room -= given
        active = [i for i in active if quota[i] < sizes[i]]
        if not given:
            break
    return quota


def _py_kind(kind: str) -> int:
    return SK_CLASS if kind == "class" else SK_FUNCTION if kind == "def" else SK_VARIABLE


def _split_member(q: str) -> str | None:
    m = _MEMBER_SEP.match(q)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return q if "." in q else None


class _Search:
    def __init__(self, server: Any, ix: _Index, token: Any, cap: int = WORKSPACE_CAP, registry: Any = None):
        self.server = server
        self.ix = ix
        self.reg = ix.reg
        self.registry = registry  # real object (ix.reg is a weak proxy) for analysis.python
        self.token = token or NEVER_CANCELLED
        self.cap = cap
        self.limit = cap + 1  # one extra entry detects truncation
        self.items: list[tuple[tuple, str]] = []
        self.closed = False
        self.seen: set[tuple] = set()
        self.done: set[tuple[str, int]] = set()

    def full(self) -> bool:
        return self.closed or len(self.items) >= self.limit

    def add(self, entries: Iterable[tuple], note: str = "") -> None:
        for e in entries:
            if self.closed or len(self.items) >= self.limit:
                return
            k = (e[E_PATH], e[E_LINE], e[E_COL], e[E_NAME])
            if k not in self.seen:
                self.seen.add(k)
                self.items.append((e, note))

    def close(self, at: tuple, name: str, container: str) -> None:
        self.items.append(((*at[:E_CAT], "marker", name, SK_OBJECT, container), ""))
        self.closed = True

    def symbols(self) -> list[dict]:
        items = self.items
        if not self.closed and len(items) > self.cap:
            at = items[self.cap - 1][0]
            items = items[: self.cap - 1]
            items.append(((*at[:E_CAT], "marker", "(more results not listed)", SK_OBJECT, _NARROW), ""))
        documents = self.server.documents if self.server is not None else None
        out: list[dict] = []
        used = len("Found 150 symbols in workspace:")
        files: set[str] = set()
        for n, (e, note) in enumerate(items):
            container = _CONTAINERS[e[E_CAT]](self.ix, e) + note
            path = e[E_PATH]
            # rendered as '<path>:' once per file, then '  name (Kind) - Line N in container'
            used += len(e[E_NAME]) + len(KIND_NAMES.get(e[E_KIND], "Unknown")) + len(container) + 24
            if path not in files:
                used += len(path) + 3
            if used > OUTPUT_BUDGET and e[E_CAT] != "marker":
                left = sum(1 for x, _ in items[n:] if x[E_CAT] != "marker")
                more = "+" if len(self.items) > self.cap or self.closed else ""
                name = f"({left}{more} more results not listed: the answer stops at ~{OUTPUT_BUDGET // 1000}k chars)"
                out.append(self.symbol(documents, (*e[:E_CAT], "marker", name, SK_OBJECT, _NARROW), _NARROW))
                break
            files.add(path)
            out.append(self.symbol(documents, e, container))
        return out

    @staticmethod
    def symbol(documents: Any, e: tuple, container: str) -> dict:
        path = e[E_PATH]
        return {
            "name": e[E_NAME],
            "kind": e[E_KIND],
            "location": {
                "uri": documents.uri_for(path) if documents is not None else path_to_uri(path),
                "range": _rng(e[E_LINE], e[E_COL], e[E_LINE], e[E_END]),
            },
            "containerName": container,
        }

    def run(self, q: str) -> None:
        head, sep, rest = q.partition(":")
        if sep and head.lower() in PREFIXES and rest.strip():
            self.prefixed(head.lower(), rest.strip())
            return
        if q.startswith("/") or _URL_HOST.match(q):
            self.routes(q)
            return
        if " " in q and "." not in q and self.reg.has_model(".".join(q.lower().split())):
            self.labels(q)
            return
        m = _MEMBER_SEP.match(q)
        if m and self.reg.has_model(m.group(1).lower()):
            self.member_query(m.group(1).lower(), m.group(2))
        elif " " in q:
            self.labels(q)
        elif "." in q:
            self.dotted(q)
        else:
            self.tiered(q.lower(), PLAIN_CATS)

    def prefixed(self, head: str, q: str) -> None:
        reg, low = self.reg, q.lower()
        if head == "route":
            self.routes(q)
        elif head == "model":
            self.tiered(low, ("model",))
        elif head in ("field", "method", "def"):
            kinds = ("method",) if head == "def" else (head,)
            member = _split_member(q)
            target = self.model_prefix(member) if member is not None else None
            if target is not None:
                self.member_query(target[0], target[1], kinds)
            else:
                self.tiered(low, ("method", "pyfunc", "jsfunc") if head == "def" else kinds)
        elif head == "xmlid":
            self.add(self.records_exact(q))
            self.tiered(low, ("record",))
        elif head == "view" and reg.has_model(low):
            self.add(self.views_of_model(low))
        elif head == "action" and reg.has_model(low):
            self.add(self.actions_of_model(low))
        else:
            cats = {"view": ("view",), "action": ("action",), "menu": ("menu",), "template": ("qweb",), "module": ("module",), "class": ("pyclass", "jsclass")}[head]
            self.tiered(low, cats)

    def dotted(self, q: str) -> None:
        low = q.lower()
        if self.reg.has_model(low):
            self.add(self.model_exact(low))
        else:
            target = self.model_prefix(q)
            if target is not None:
                self.member_query(*target)
        self.add(self.records_exact(q))
        self.add(self.ix.bucket("template").get(low))
        head, _, rest = q.partition(".")
        if head == "odoo":
            self.add(self.python_path(q))
        elif head in CODE_HEADS and self.reg.module(head) is None:
            self.add(self.python_path("odoo." + q))
        elif head[:1].isupper() and rest and not self.items:
            self.class_member(head, rest)
        self.tiered(low, ("model", "record", "template"), (PREFIX, SUBSTRING, FUZZY))

    def labels(self, q: str) -> None:
        low = q.lower()
        dotted = ".".join(low.split())
        if "." not in q and self.reg.has_model(dotted):
            self.add(self.model_exact(dotted))
        self.tiered(low, ("menu", "action"), (EXACT, PREFIX, SUBSTRING))

    def tiered(self, q: str, cats: Iterable[str], tiers: Iterable[int] = (EXACT, PREFIX, SUBSTRING, FUZZY)) -> None:
        cats = tuple(cats)
        for tier in tiers:
            if tier == FUZZY:
                if not self.items and len(q) >= 3 and _NAME_LIKE.fullmatch(q):
                    self.fuzzy(q, cats)
                return
            for cat in cats:
                if self.full():
                    return
                self.token.check()
                if cat == "model":
                    if tier == EXACT:
                        self.add(self.model_exact(q))
                        continue
                    cat = "modelhead"
                b = self.ix.bucket(cat)
                idxs = [i for i in _MATCHERS[tier](b, q) if (cat, i) not in self.done]
                found = _top(b, idxs, self.limit - len(self.items), _chain_of(cat))
                self.add(found)
                if not self.full():
                    self.done.update((cat, i) for i in idxs)
                if tier == EXACT and cat == "record" and "." not in q:
                    self.add(self.implicit_exact(q))

    def fuzzy(self, q: str, cats: tuple) -> None:
        buckets = [(n, cat, self.ix.bucket("modelhead" if cat == "model" else cat)) for n, cat in enumerate(cats)]
        picks: list[tuple] = []
        if len(q) >= NEAR_MIN:
            variants = _one_typo(q)
            for n, cat, b in buckets:
                picks += [(_typo_rank(q, b.names[i]), n, _entry_key(b.best[i]), cat, b, i) for i in b.near(variants)]
        if not picks:
            for n, cat, b in buckets:
                self.token.check()
                picks += [(quality, n, _entry_key(b.best[i]), cat, b, i) for i, quality in b.fuzzy_scored(q) if _subsequence_ok(q, quality)]
        left = FUZZY_CAP
        for _, _, _, cat, b, i in heapq.nsmallest(FUZZY_CAP, picks, key=itemgetter(0, 1, 2)):
            if left <= 0 or self.full():
                return
            found = _top(b, [i], FUZZY_PER_NAME, _chain_of(cat))[: min(FUZZY_PER_NAME, left)]
            self.add(found, FUZZY_NOTE)
            left -= len(found)

    def model_exact(self, low: str) -> list[tuple]:
        entries = self.ix.bucket("model").get(low)
        if not entries:
            return []
        return sorted(entries, key=lambda e: (not e[E_DATA].is_definition, _entry_key(e)))

    def model_prefix(self, q: str) -> tuple[str, str] | None:
        parts = q.split(".")
        for n in range(len(parts) - 1, 0, -1):
            model = ".".join(parts[:n]).lower()
            if self.reg.has_model(model):
                return model, ".".join(parts[n:])
        return None

    def views_of_model(self, model: str) -> list[tuple]:
        entries = [self.ix.record_entry(r) for r in self.reg.views_of_model(model)]

        def extension(e: tuple) -> bool:
            info = e[E_DATA][X_INFO] or {}
            return bool(info.get("inherit_id")) and info.get("mode") != "primary"

        return sorted(entries, key=lambda e: (extension(e), _entry_key(e)))

    def actions_of_model(self, model: str) -> list[tuple]:
        self.ix.bucket("action")  # builds ix.actions as a side effect
        reg = self.reg
        out = []
        for e in self.ix.actions:
            rec = e[E_DATA]
            module = reg.module_of_path(e[E_PATH])
            target = (rec[X_INFO] or {}).get("res_model") or _record_target(reg, rec, module)
            if target == model or _binding_target(reg, rec, module) == model:
                out.append(e)
        return sorted(out, key=_entry_key)

    def member_query(self, model: str, path: str, kinds: tuple = ("field", "method", "attr")) -> None:
        reg = self.reg
        owner, name = model, path
        if "." in path:
            head, _, name = path.rpartition(".")
            walk = reg.walk_path(model, head)
            if len(walk) != head.count(".") + 1 or not walk[-1][2]:
                return
            owner = reg.comodel(walk[-1][1], walk[-1][0])
            if not owner:
                return
        if name:
            entries = self.member_exact(owner, name, kinds)
            if entries:
                self.add(entries)
                return
        self.member_partial(owner, name.lower(), kinds)

    def member_exact(self, owner: str, name: str, kinds: tuple) -> list[tuple]:
        # mirrors Registry.member(): the first kind with definitions wins
        found = []
        for cat in kinds:
            if cat != "attr" or name not in MODEL_DECL_ATTRS:
                found = [(cat, d) for d in getattr(self.reg, cat)(owner, name)]
                if found:
                    break
        if not found:
            return []
        order: dict[str, int] = {}
        for _, d in found:
            if d.cls.model:
                order.setdefault(d.cls.model, len(order))
        entries = [(cat, d, self.ix.member_entry(cat, d)) for cat, d in found]

        def rank(item: tuple) -> tuple:
            _, d, e = item
            c = d.cls
            own = c.model == owner and d.delegated_via is None
            return (not own, d.delegated_via is not None, order.get(c.model, 1 << 20), not c.is_definition, _entry_key(e))

        entries.sort(key=rank)
        return [e for _, _, e in entries]

    def member_entry(self, owner: str, cat: str, defs: list) -> tuple[tuple, bool]:
        own = [(d, self.ix.member_entry(cat, d)) for d in defs if d.cls.model == owner and d.delegated_via is None]
        if own:
            return min(own, key=lambda de: (not de[0].cls.is_definition, _entry_key(de[1])))[1], True
        return self.ix.member_entry(cat, defs[0]), False

    def member_partial(self, owner: str, q: str, kinds: tuple) -> None:
        pool: dict[str, tuple[int, str, list]] = {}
        for ko, cat in enumerate(kinds):
            for name, defs in getattr(self.reg, cat + "s")(owner).items():
                if defs and name not in pool and not (cat == "attr" and name in MODEL_DECL_ATTRS):
                    pool[name] = (ko, cat, defs)
        if not q:
            self.member_listing(owner, pool, kinds)
            return
        cands = []
        for name, (ko, cat, defs) in pool.items():
            tier = _tier(q, name.lower())
            if tier is None or tier == FUZZY:
                continue
            entry, own = self.member_entry(owner, cat, defs)
            cands.append(((tier, not own, not entry[E_DATA][1].model, ko, _entry_key(entry)), entry))
        if cands:
            cands.sort(key=itemgetter(0))
            self.add(e for _, e in cands)
            return
        lowered = {name.lower(): name for name in pool}
        near = lowered.keys() & _one_typo(q) if len(q) >= NEAR_MIN else ()
        if near:
            scored = [((_typo_rank(q, low),), lowered[low]) for low in near]
        else:
            scored = []
            for name in lowered.values():
                quality = _fuzzy_quality(q, name)
                if quality is not None and _subsequence_ok(q, quality):
                    scored.append((quality, name))
        for _, name in heapq.nsmallest(FUZZY_CAP, scored, key=lambda s: (s[0], pool[s[1]][0], s[1])):
            ko, cat, defs = pool[name]
            self.add([self.member_entry(owner, cat, defs)[0]], FUZZY_NOTE)
            if self.full():
                return

    def member_listing(self, owner: str, pool: dict[str, tuple[int, str, list]], kinds: tuple) -> None:
        cands = []
        for name, (ko, cat, defs) in pool.items():
            entry, own = self.member_entry(owner, cat, defs)
            framework = not entry[E_DATA][1].model
            cands.append(((not own, framework, entry[E_CUSTOM], ko, _entry_key(entry)), own, cat, entry))
        cands.sort(key=itemgetter(0))
        room = self.cap - len(self.items)
        if len(cands) <= room or room < 2:
            self.add(c[3] for c in cands)
            return
        room -= 1
        groups: dict[tuple[bool, str], list] = defaultdict(list)
        for c in cands:
            groups[(c[1], c[2])].append(c)
        keys = list(groups)
        quota = _quotas([len(groups[k]) for k in keys], [LISTING_WEIGHTS[k] for k in keys], room)
        shown = {id(c) for k, n in zip(keys, quota) for c in groups[k][:n]}
        self.add(c[3] for c in cands if id(c) in shown)
        left: dict[str, int] = defaultdict(int)
        for c in cands:
            if id(c) not in shown:
                left[c[2]] += 1
        words = {"field": "fields", "method": "methods", "attr": "attributes"}
        what = ", ".join(f"{left[k]} {words[k]}" for k in kinds if left.get(k))
        at = (self.model_exact(owner) or [self.items[-1][0]])[0]
        hint = " / ".join(f"{p}{owner}.<prefix>" for p in ([""] if len(kinds) > 1 else []) + [f"{k}:" for k in kinds if k != "attr"])
        self.close(at, f"({sum(left.values())} more members not listed: {what})", f"narrow with {hint}")

    def class_member(self, cls_name: str, member: str) -> None:
        models: list[str] = []
        direct: list[tuple] = []
        resolve = None
        for e in self.ix.bucket("pyclass").get(cls_name.lower()):
            cls = e[E_DATA][1]
            if cls is None:
                continue
            mi = cls[K_MODEL]
            if mi and mi.get(MI_KIND) != "framework":
                model = model_class_info(cls, self.ix.major)[0]
                if model and model not in models:
                    models.append(model)
                continue
            if "." in member or self.registry is None:
                continue
            if resolve is None:
                from ..analysis.python import _class_member, _use_registry

                _use_registry(self.registry)
                resolve = _class_member
            sym = resolve(self.registry, e[E_PATH], cls[K_NAME], member)
            if sym is not None and sym[0] == "sym":
                _, where, real, kind, line, col, end = sym
                facts = self.reg.file_facts(where) or {}
                owner = next((k[K_NAME] for k in facts.get(CLASSES) or () if k[K_LINE] <= line <= k[K_END_LINE]), cls[K_NAME])
                module = self.reg.module_of_path(where)
                cu, rk = self.ix.mkey(module)
                dotted = self.reg.py_dotted_name(where) or os.path.basename(where)
                note = ", not imported by its module" if module and not self.reg.is_loaded(where) else ""
                direct.append((cu, rk, where, line, col, end, "pyfunc", real, SK_METHOD if kind == "def" else SK_VARIABLE, (dotted, owner, note)))
        for model in models:
            self.member_query(model, member)
        self.add(sorted(direct, key=_entry_key))

    def records_exact(self, q: str) -> list[tuple]:
        low = q.lower()
        entries = self.ix.bucket("record").get(low)
        if entries:
            return _creator_first(entries)
        if "." not in q:
            return []
        return [self.ix.record_entry(r) for r in (self.reg.records(q) or self.reg.records(low))]

    def implicit_exact(self, q: str) -> list[tuple]:
        reg = self.reg
        if q.startswith("model_"):
            model = self.ix.implicit_models().get(q[6:])
            if not model:
                return []
            modules = [c.module for c in sorted(reg.model_classes(model), key=lambda c: not c.is_definition)]
        elif q.startswith("field_") and "__" in q:
            mpart, _, fname = q[6:].rpartition("__")
            model = self.ix.implicit_models().get(mpart)
            if not model:
                return []
            modules = [d.cls.module for d in reversed(reg.field(model, fname)) if d.cls.model == model and d.delegated_via is None]
        else:
            return []
        out = []
        for module in dict.fromkeys(m for m in modules if m):
            out += [self.ix.record_entry(r) for r in reg.records(f"{module}.{q}")]
        return out

    def module_entry(self, dotted: str, path: str) -> tuple:
        return (*self.ix.mkey(self.reg.module_of_path(path)), path, 0, 0, 0, "pyfunc", dotted, SK_MODULE, (dotted, None, ""))

    def python_path(self, q: str) -> list[tuple]:
        reg, ix = self.reg, self.ix
        path = reg.py_module_file(q)
        if path is not None:
            return [self.module_entry(q, path)]
        head, _, name = q.rpartition(".")
        path = reg.py_module_file(head)
        if path is None or not name:
            return []
        cu, rk = ix.mkey(reg.module_of_path(path))
        out = []
        for s in (reg.file_facts(path) or {}).get(SYMBOLS) or ():
            if s[S_NAME] == name:
                out.append((cu, rk, path, s[S_LINE], s[S_COL], s[S_END_COL], "pyfunc", name, _py_kind(s[S_KIND]), (head, None, "")))
        if out or self.registry is None:
            return out
        # follow re-exports (saas-18.3+: odoo.fields.Many2one is in odoo.orm.fields_relational)
        from ..analysis.python import _resolve_symbol, _use_registry

        _use_registry(self.registry)  # caches keyed by dotted name only: never another env's answer
        sym = _resolve_symbol(self.registry, head, name)
        if sym is None:
            return []
        if sym[0] == "module":
            return [self.module_entry(sym[1], sym[2])] if sym[2] else []
        _, where, real, kind, line, col, end = sym
        dotted = reg.py_dotted_name(where) or os.path.basename(where)
        cu, rk = ix.mkey(reg.module_of_path(where))
        return [(cu, rk, where, line, col, end, "pyfunc", real, _py_kind(kind), (dotted, None, f", re-exported by {head}"))]

    def routes(self, q: str) -> None:
        url = _url_path(q) if q.startswith("/") or _URL_HOST.match(q) else q
        scored = self.score_routes(url)
        if url.startswith("/") and not any(s[0] <= 1 for s in scored):
            seg, _, rest = url[1:].partition("/")
            if rest and _LANG_SEGMENT.match(seg):
                alt = [s for s in self.score_routes("/" + rest) if s[0] <= 1]
                self.add((s[-1] for s in alt), f", lang prefix /{seg} stripped")
        self.add(s[-1] for s in scored)

    def score_routes(self, url: str) -> list[tuple]:
        # -> [(tier, dead, entry key, entry)], sorted
        ix = self.ix
        scored = []
        for r in ix.all_routes(self.server):
            best = None
            for p in r.paths:
                if p == url or (len(url) > 1 and p.rstrip("/") == url.rstrip("/")):
                    t = 0
                elif "<" in p and url.startswith("/") and ix.route_regex(p).fullmatch(url):
                    t = 1
                elif p.startswith(url):
                    t = 2
                elif url in p:
                    t = 3
                else:
                    continue
                if best is None or t < best[0]:
                    best = (t, p)
            if best is not None:
                m = r.method
                e = (*ix.mkey(r.module), r.path, m[M_LINE], m[M_COL], m[M_END_COL], "route", m[M_NAME], SK_METHOD, (best[1], r.info, r.parent, r.dead))
                scored.append((best[0], r.dead, _entry_key(e), e))
        if any(s[0] == 0 for s in scored):  # werkzeug prefers static rules over <converter> ones
            scored = [s for s in scored if s[0] != 1]
        scored.sort(key=itemgetter(0, 1, 2))
        return scored


_NARROW = "narrow the query: a longer name, model.member, or a prefix (model: field: method: xmlid: view: action: class: def: ...)"


def _fuzzy_quality(q: str, name: str) -> tuple | None:
    m = _subsequence_regex(q).search(name.lower())
    if m is None:
        return None
    return _subsequence_quality(m, name.lower(), 0, name if len(name) == len(name.lower()) else None, len(q))
