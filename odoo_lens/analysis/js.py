from __future__ import annotations

import os
import re
from collections import OrderedDict
from typing import Any

from ..documents import index_to_utf16, utf16_len
from ..facts import (
    IMPORTS,
    KIND,
    KIND_JS,
    R_COL,
    R_CTX,
    R_END_COL,
    R_KIND,
    R_LINE,
    R_VALUE,
    REF_METHOD,
    REF_MODEL,
    REF_QWEB,
    REFS,
    S_COL,
    S_END_COL,
    S_LINE,
    S_NAME,
    SYMBOLS,
)
from .targets import FILE, METHOD, MODEL, PYSYM, TEMPLATE, Target
from .xml import nearest

LIB_ALIASES = {
    "@odoo/owl": ("web", "static/lib/owl/owl.js"),
    "@odoo/hoot": ("web", "static/lib/hoot/hoot.js"),
    "@odoo/hoot-dom": ("web", "static/lib/hoot-dom/hoot-dom.js"),
    "@odoo/hoot-mock": ("web", "static/lib/hoot/hoot-mock.js"),
    "@odoo/o-spreadsheet": ("spreadsheet", "static/src/o_spreadsheet/o_spreadsheet.js"),
}
_REEXPORT_DEPTH = 4

_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_OWNER_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*$")
_IMPORT_RE = re.compile(r"""\bimport\s+([\w$\s,{}*]+?)\s*\bfrom\s*(["'])([^"'\n]+)\2""")
_EXPORT_FROM_RE = re.compile(r"""\bexport\s*(\*(?:\s*as\s+[\w$]+)?|\{[^}]*\})\s*from\s*(["'])([^"'\n]+)\2""")
_DEFAULT_RE = re.compile(r"""\bexport\s+default\s+(?:async\s+)?(?:(?:class|function\s*\*?)\s+)?([A-Za-z_$][\w$]*)?""")
_EXPORT_LIST_RE = re.compile(r"""\bexport\s*(\{[^}]*\})(?!\s*from\b)""")
_EXPORT_MAP_RE = re.compile(r"""\b([A-Za-z_$][\w$]*)\s*:\s*\(\)\s*=>\s*([A-Za-z_$][\w$]*)|\bexports\.([A-Za-z_$][\w$]*)\s*=\s*([A-Za-z_$][\w$]*)""")
_DECL_RE = re.compile(r"""^([ \t]*)(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:class|function\s*\*?|const|let|var)\s+([A-Za-z_$][\w$]*)""", re.M)
_STRING_RE = re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`""")
_KEYWORDS = frozenset(
    "await break case catch class const continue debugger default delete do else export extends false finally for "
    "from function if import in instanceof let new null of return static super switch this throw true try typeof "
    "undefined var void while with yield async get set as".split()
)


def targets_at(ctx: Any) -> list[Target]:
    facts = ctx.facts
    if not facts:
        return []
    line, col = ctx.line, ctx.col
    text = ctx.server.documents.line(ctx.path, line)
    cands: list[tuple[int, int, int, tuple]] = []
    for spec, l0, c0, c1 in facts.get(IMPORTS) or ():
        if l0 == line:
            cands.append((c0, c1, 0, ("specifier", spec, c0, c1)))
    for ref in facts.get(REFS) or ():
        if ref[R_LINE] == line:
            cands.append((ref[R_COL], ref[R_END_COL], 0, ("ref", ref)))
    bindings = _file_bindings(ctx.path, facts, ctx)
    symbols = {s[S_NAME]: s for s in facts.get(SYMBOLS) or ()}
    masked = _mask(text)
    for m in _IDENT_RE.finditer(masked):
        name = m.group()
        if name in _KEYWORDS:
            continue
        c0, c1 = index_to_utf16(text, m.start()), index_to_utf16(text, m.end())
        exact = c0 <= col <= c1
        if exact:  # on an identifier: that one or nothing, never a neighbour
            cands = [c for c in cands if c[0] <= col <= c[1]]
        item = _identifier(name, masked, m.start(), bindings, symbols)
        if item is not None:
            cands.append((c0, c1, 1, item + (c0, c1)))
        if exact:
            break
    hit = nearest(cands, col)
    if hit is None:
        return []
    what = hit[0]
    if what == "ref":
        return _ref_targets(hit[1])
    if what == "specifier":
        path = resolve_specifier(ctx.registry, hit[1], ctx.path)
        return [Target(FILE, path=path, origin=(line, hit[2], hit[3]), extra={"role": "import", "specifier": hit[1]})] if path else []
    origin = (line, hit[-2], hit[-1])
    if what == "local":
        sym = hit[1]
        return [Target(PYSYM, name=sym[S_NAME], path=ctx.path, line=sym[S_LINE], col=sym[S_COL], end_col=sym[S_END_COL], origin=origin, extra={"role": "symbol"})]
    spec, exported, local = hit[1], hit[2], hit[3]
    path = resolve_specifier(ctx.registry, spec, ctx.path)
    if path is None:
        return []
    extra = {"role": "import", "specifier": spec, "local": local}
    if exported == "*":
        return [Target(FILE, path=path, origin=origin, extra=extra)]
    loc = export_location(ctx.registry, ctx.server.documents, path, exported)
    if loc is None:
        return [Target(FILE, path=path, origin=origin, extra=extra)]
    return [Target(PYSYM, name=exported, path=loc[0], line=loc[1], col=loc[2], end_col=loc[3], origin=origin, extra=extra)]


def _ref_targets(ref: tuple) -> list[Target]:
    kind = ref[R_KIND]
    origin = (ref[R_LINE], ref[R_COL], ref[R_END_COL])
    role = ref[R_CTX][0]
    if kind == REF_QWEB:
        return [Target(TEMPLATE, xmlid=ref[R_VALUE], origin=origin, extra={"role": role})]
    if kind == REF_MODEL:
        return [Target(MODEL, model=ref[R_VALUE], origin=origin, extra={"role": role})]
    if kind == REF_METHOD and len(ref[R_CTX]) > 1:
        return [Target(METHOD, model=ref[R_CTX][1], name=ref[R_VALUE], origin=origin, extra={"role": role})]
    return []


def _identifier(name: str, masked: str, start: int, bindings: dict, symbols: dict) -> tuple | None:
    before = masked[:start].rstrip()
    if before.endswith(".") and not before.endswith("..."):
        owner = _OWNER_RE.search(before[:-1])
        binding = bindings.get(owner.group(1)) if owner else None
        if binding is not None and binding[1] == "*":
            return ("bound", binding[0], name, f"{owner.group(1)}.{name}")
        return None
    if name in bindings:
        spec, exported = bindings[name]
        return ("bound", spec, exported, name)
    if name in symbols:
        return ("local", symbols[name])
    return None


def _mask(line: str) -> str:
    out = _STRING_RE.sub(lambda m: m.group()[0] + " " * (len(m.group()) - 2) + m.group()[-1], line)
    cut = out.find("//")
    return out if cut < 0 else out[:cut] + " " * (len(out) - cut)


_BINDINGS: OrderedDict[str, tuple[dict, dict]] = OrderedDict()


def _file_bindings(path: str, facts: dict, ctx: Any) -> dict[str, tuple[str, str]]:
    hit = _BINDINGS.get(path)
    if hit is not None and hit[0] is facts:
        _BINDINGS.move_to_end(path)
        return hit[1]
    bindings = import_bindings(ctx.text)
    _BINDINGS[path] = (facts, bindings)
    while len(_BINDINGS) > _INDEX_SIZE:
        _BINDINGS.popitem(last=False)
    return bindings


# {local: (specifier, exported | "default" | "*")}, incl. names re-exported by `export {x as y} from`
def import_bindings(text: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for m in _EXPORT_FROM_RE.finditer(text):
        if m.group(1).startswith("{"):
            for alias, exported in _clause_bindings(m.group(1)):
                out[alias] = (m.group(3), exported)
    for m in _IMPORT_RE.finditer(text):
        for local, exported in _clause_bindings(m.group(1)):
            out[local] = (m.group(3), exported)
    return out


def _clause_bindings(clause: str) -> list[tuple[str, str]]:
    out = []
    clause = clause.strip()
    if clause and clause[0] not in "{*":
        default, _, clause = clause.partition(",")
        if _IDENT_RE.fullmatch(default.strip()):
            out.append((default.strip(), "default"))
        clause = clause.strip()
    if clause.startswith("*"):
        m = re.match(r"\*\s*as\s+([\w$]+)", clause)
        if m:
            out.append((m.group(1), "*"))
    elif clause.startswith("{"):
        for part in clause[1 : clause.find("}") if "}" in clause else None].split(","):
            name, _, alias = part.strip().partition(" as ")
            name, alias = name.strip(), alias.strip()
            if _IDENT_RE.fullmatch(name):
                out.append((alias or name, name))
    return out


def resolve_specifier(registry: Any, spec: str, from_path: str) -> str | None:
    lib = LIB_ALIASES.get(spec)
    if lib is not None:
        info = registry.module(lib[0])
        path = os.path.join(info.path, lib[1]) if info is not None else None
        return path if path and os.path.isfile(path) else None
    if spec.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(from_path), spec))
    elif spec.startswith("@"):
        module, _, rest = spec[1:].partition("/")
        info = registry.module(module)
        if info is None or not rest:
            return None
        # @mod/x -> static/src/x(.js|/index.js); @mod/../tests/x -> static/tests (url_to_module_path)
        base = os.path.normpath(os.path.join(info.path, "static", "src", rest))
    else:
        return None
    for cand in (base + ".js", os.path.join(base, "index.js"), base):
        if cand.endswith(".js") and os.path.isfile(cand):
            return cand
    return None


def export_location(registry: Any, documents: Any, path: str, name: str, _depth: int = 0) -> tuple[str, int, int, int] | None:
    text = documents.text(path)
    if text is None:
        return None
    facts = registry.file_facts(path)
    # bundled static/lib file: esbuild export map or CommonJS exports; the local may be renamed
    if not (facts and facts.get(KIND) == KIND_JS):
        local = _file_index(path, text)[0].get(name)
        loc = _declaration(None, path, text, local) if local is not None else None
        if loc is not None:
            return loc
    if name == "default":
        m = _DEFAULT_RE.search(text)
        if m is None:
            return None
        if m.group(1):
            return _declaration(facts, path, text, m.group(1)) or _position(path, text, m.start(1), m.group(1))
        return _position(path, text, m.start(), "export")
    loc = _declaration(facts, path, text, name)
    if loc is not None or _depth >= _REEXPORT_DEPTH:
        return loc
    for m in _EXPORT_LIST_RE.finditer(text):
        local = next((src for alias, src in _clause_bindings(m.group(1)) if alias == name), None)
        if local is not None:
            entry = re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", m.group(1))
            return _declaration(facts, path, text, local) or _position(path, text, m.start(1) + (entry.start() if entry else 0), name)
    for m in _EXPORT_FROM_RE.finditer(text):
        clause, spec = m.group(1), m.group(3)
        if clause.startswith("*"):
            source = None if "as" in clause else name
        else:
            source = next((exported for local, exported in _clause_bindings(clause) if local == name), None)
        target = resolve_specifier(registry, spec, path) if source else None
        loc = export_location(registry, documents, target, source, _depth + 1) if target and source else None
        if loc is not None:
            return loc
    return None


def _declaration(facts: dict | None, path: str, text: str, name: str) -> tuple[str, int, int, int] | None:
    if facts and facts.get(KIND) == KIND_JS:
        for sym in facts.get(SYMBOLS) or ():
            if sym[S_NAME] == name:
                return path, sym[S_LINE], sym[S_COL], sym[S_END_COL]
    hit = _file_index(path, text)[1].get(name)
    return _position(path, text, hit[1], name) if hit else None


_INDEX: OrderedDict[str, tuple[str, dict, dict]] = OrderedDict()
_INDEX_SIZE = 16


def _file_index(path: str, text: str) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
    hit = _INDEX.get(path)
    if hit is not None and hit[0] is text:
        _INDEX.move_to_end(path)
        return hit[1], hit[2]
    exports: dict[str, str] = {}
    for m in _EXPORT_MAP_RE.finditer(text):
        exports.setdefault(m.group(1) or m.group(3), m.group(2) or m.group(4))
    decls: dict[str, tuple[int, int]] = {}
    # least indented declaration wins: bundled libraries wrap their top level in a function
    for m in _DECL_RE.finditer(text):
        indent = len(m.group(1))
        if m.group(2) not in decls or indent < decls[m.group(2)][0]:
            decls[m.group(2)] = (indent, m.start(2))
    _INDEX[path] = (text, exports, decls)
    while len(_INDEX) > _INDEX_SIZE:
        _INDEX.popitem(last=False)
    return exports, decls


def _position(path: str, text: str, offset: int, token: str) -> tuple[str, int, int, int]:
    line = text.count("\n", 0, offset)
    start = text.rfind("\n", 0, offset) + 1
    col = utf16_len(text[start:offset])
    return path, line, col, col + utf16_len(token)


__all__ = ["targets_at", "resolve_specifier", "export_location", "import_bindings", "LIB_ALIASES"]


def clear_caches() -> None:
    _BINDINGS.clear()
    _INDEX.clear()
