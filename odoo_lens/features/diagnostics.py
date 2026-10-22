from __future__ import annotations

import difflib
import os
import re
import xml.parsers.expat as expat
from typing import Any, Iterable

from ..documents import utf16_len, utf16_to_index
from ..extract.common import SrcIndex
from ..facts import (
    A_COL,
    A_END_COL,
    A_KIND,
    A_LINE,
    A_NAME,
    A_VALUE,
    CLASSES,
    CSV_MODEL,
    ERROR,
    F_ARGS,
    F_COL,
    F_COMODEL,
    F_END_COL,
    F_END_LINE,
    F_LINE,
    F_NAME,
    F_TYPE,
    K_ATTRS,
    K_BASES,
    K_COL,
    K_END_COL,
    K_END_LINE,
    K_FIELDS,
    K_LINE,
    K_METHODS,
    K_MODEL,
    MANIFEST_DATA,
    MANIFEST_DEMO,
    MANIFEST_DEPENDS,
    M_COL,
    M_DECORATORS,
    M_END_COL,
    M_END_LINE,
    M_LINE,
    M_NAME,
    MI_INHERIT,
    MI_INHERIT_STR,
    MI_INHERITS,
    MI_ATTRS,
    MI_KIND,
    MI_NAME,
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
    REF_XMLID,
    REFS,
    ROOT,
    S_END_LINE,
    S_KIND,
    S_LINE,
    SYMBOLS,
    X_END_LINE,
    X_ID,
    X_INFO,
    X_LINE,
    X_MODEL,
    X_TAG,
)
from ..registry import LOG_ACCESS_FIELDS, UNLOADED_DIRS, model_class_info

SOURCE = "odoo-lens"
ERROR_, WARNING, INFO, HINT = 1, 2, 3, 4
# Claude Code shows <=10 per file, basename only: every message is a self-explanatory one-liner
MAX_DIAGNOSTICS = 50

# other bases are Python mixins the registry cannot see (model not closed)
MODEL_BASES = frozenset({"Model", "AbstractModel", "TransientModel", "BaseModel", "CachedModel", "object"})
NON_RELATIONAL = frozenset(
    {"Char", "Text", "Html", "Integer", "Float", "Monetary", "Boolean", "Date", "Datetime", "Selection", "Binary", "Image", "Json", "Id"}
)
VIEW_TYPES = frozenset(
    {"form", "list", "tree", "kanban", "search", "graph", "pivot", "calendar", "gantt", "activity", "cohort", "map", "grid", "hierarchy", "card"}
)
# Odoo's ir_ui_view._validate_tag_field list: narrower than xmlfacts.SUBVIEW_TAGS on purpose
SUBVIEW_TAGS = frozenset({"form", "list", "tree", "graph", "kanban", "calendar"})
# on every model: 16-18 synthesized by MetaModel (not in facts), 19+ partly declared on BaseModel
MAGIC_FIELDS = {
    "id": ("Id", None),
    "display_name": ("Char", None),
    "create_uid": ("Many2one", "res.users"),
    "create_date": ("Datetime", None),
    "write_uid": ("Many2one", "res.users"),
    "write_date": ("Datetime", None),
    "__last_update": ("Datetime", None),
}

# severity mirrors Odoo: raises at setup or search (_order), only logs for constrains/onchange
PY_FIELD_ROLES = {
    "related": (ERROR_, "related"),
    "depends": (ERROR_, "@api.depends"),
    "constrains": (WARNING, "@api.constrains"),
    "onchange": (WARNING, "@api.onchange"),
    "currency_field": (ERROR_, "currency_field"),
    "inverse_name": (ERROR_, "inverse_name"),
    "_rec_name": (ERROR_, "_rec_name"),
    "_order": (ERROR_, "_order"),
    "_rec_names_search": (ERROR_, "_rec_names_search"),
    "domain": (WARNING, "domain"),
}
PY_METHOD_ROLES = frozenset({"compute", "inverse", "search", "group_expand", "compute_sql", "selection", "default"})
# an abstract mixin may name members only its inheritors define: accepted if one of them resolves it
LATE_BOUND_ROLES = frozenset({"depends", "constrains", "onchange", "_order", "_rec_names_search"}) | PY_METHOD_ROLES
SIMPLE_NAME_ROLES = frozenset({"constrains", "onchange", "currency_field", "inverse_name", "_rec_name"})
# resolved by the loader at install: a missing target fails the install
XML_STRICT_ROLES = frozenset({"ref", "inherit_id", "parent", "action"})
XML_ROLE_LABELS = {
    "ref": "ref",
    "eval": "ref() in eval",
    "inherit_id": "inherit_id",
    "parent": "menu parent",
    "action": "action",
    "groups": "groups",
    "percent": "%(...)d",
    "tcall": "t-call",
    "view_ref": "*_view_ref context key",
}
FIELD_RENAMES_19 = {"groups_id": "group_ids", "users": "user_ids", "category_id": "privilege_id"}
MODEL_RENAMES = {"ir.model.access": ((19, 4), "ir.access"), "ir.rule": ((19, 4), "ir.access")}
REMOVED_DECORATORS = {
    "api.multi": ((13, 0), "@api.multi was removed in Odoo 13.0: drop the decorator"),
    "api.one": ((13, 0), "@api.one was removed in Odoo 13.0: drop the decorator and loop over self"),
    "api.returns": ((19, 0), "@api.returns was removed in Odoo 19.0: drop the decorator"),
    "api.model_create_single": ((19, 0), "@api.model_create_single was removed in Odoo 19.0: use @api.model_create_multi"),
}
FIELD_KWARG_DEPRECATIONS = (
    ("states", (17, 0), "Field parameter 'states' is ignored since Odoo 17.0: use readonly/required/invisible expressions in the views"),
    ("group_operator", (18, 0), "Field parameter 'group_operator' was renamed 'aggregator' in Odoo 18.0"),
    ("auto_join", (19, 0), "Field parameter 'auto_join' was renamed 'bypass_search_access' in Odoo 19.0 (auto_join is ignored)"),
)
SOFT_LOOKUPS = frozenset({"_xmlid_to_res_id", "_xmlid_to_res_model_res_id"})

_XMLID_KEY_RE = re.compile(r"""['"]xml_id['"]\s*:\s*['"]([\w.\-]+)['"]""")
_TAG_RE = re.compile(rb"""<[^>"']*(?:(?:"[^"]*"|'[^']*')[^>"']*)*>""")
_XPATH_FIELD_RE = re.compile(r"""field\[@name\s*=\s*(['"])([A-Za-z0-9_]+)\1\]""")
_XPATH_TREE_RE = re.compile(r"""(?:^|/)tree(?=$|[/\[\s])""")
_WORD_TREE_RE = re.compile(r"""\btree\b""")
_XPATH_LIST_RE = re.compile(r"""(?:^|/)list(?:\[|/|$)""")
_TREE_VIEW_REF_RE = re.compile(r"""\btree_view_ref\b""")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUOTED_NAME_RE = re.compile(r"""^['"]([\w.]+)['"]$""")
_DEPENDS_DECORATOR_RE = re.compile(r"""\s*@\s*(?:api\.)?depends\(""")
_DYNAMIC_NAME_RE = re.compile(rb"""(?m)^\s+_name\s*=\s*(?:[a-z_]\w*\(|f['"]|['"][^'"\n]*['"]\s*%)""")
_XMLID_SQL_RE = re.compile(rb"""\bir_model_data\b|_update_xmlids\(""")
_CALL_BEFORE_RE = re.compile(r"""([\w\])]*)\s*\.\s*(\w+)\(\s*$""")
_NOT_FOUND_FALSE_RE = re.compile(r"""raise_if_not_found\s*=\s*False|^\s*['"]?\s*,\s*False\b""")
_USER_HAS_GROUPS_RE = re.compile(rb"""\.user_has_groups\(""")
_OPTIONAL_GUARD_RE = re.compile(
    r"""['"][\w.]+['"]\s+(?:not\s+)?in\s+[\w.\[\]'"()]*(?:env|_fields|registry|pool)\b"""
    r"""|ir\.module\.module|base\.module_\w|_is_installed|module_installed|install_modules\(|button_immediate_install"""
    r"""|\bhasattr\(|\bfields_get\("""
)


def diagnostics(ctx: Any) -> list[dict]:
    kind = ctx.kind
    if kind not in ("py", "xml", "csv", "manifest"):
        return []
    facts = ctx.facts
    if not facts:
        return []
    report = _Report()
    if facts.get(ERROR):
        if _loaded_file(ctx):
            _syntax_error(facts, report)
    elif ctx.registry is not None and ctx.module is not None:
        res = _Resolver(ctx)
        if kind == "py":
            if _not_imported(ctx, facts, report):
                return report.result()  # its models never exist: other checks would mislead
            _PythonChecker(ctx, res, facts, report).run()
        elif kind in ("xml", "csv") and not _loaded_file(ctx):
            _not_loaded(ctx, report)
        elif kind == "xml":
            _XmlChecker(ctx, res, facts, report).run()
        elif kind == "csv":
            _CsvChecker(ctx, res, facts, report).run()
        elif ctx.path == res.reg.module(ctx.module).manifest:
            _check_manifest(ctx, res, facts, report)
    return report.result()


def _not_imported(ctx: Any, facts: dict, report: "_Report") -> bool:
    reg = ctx.registry
    if not hasattr(reg, "is_loaded") or reg.is_loaded(ctx.path):
        return False
    rel = _module_rel(reg, ctx.module, ctx.path) or ""
    parts = rel.split("/")
    if any(p in ("tests", "migrations", "upgrades", "upgrade", "static") for p in parts[:-1]):
        return False
    models = [c for c in facts.get(CLASSES) or () if c[K_MODEL] and c[K_MODEL].get("kind") != "framework"]
    if not models:
        return False
    pkg = "/".join(parts[:-1]) or "."
    name = parts[-1][:-3]
    init = os.path.join(os.path.dirname(ctx.path), "__init__.py")
    commented = _commented_import(ctx, init, name)
    if commented is not None:
        how = f"its import is commented out in {pkg}/__init__.py:{commented + 1}"
    elif not os.path.isfile(init):
        how = f"{pkg}/ has no __init__.py: create it with 'from . import {name}' and import the package"
    else:
        how = f"add 'from . import {name}' to {pkg}/__init__.py"
    msg = f"This file is not imported by module '{ctx.module}' ({how}): Odoo never loads the models defined here"
    first = models[0]
    report.add(WARNING, "not-imported", first[K_LINE], first[K_COL], first[K_END_COL], msg)
    return True


def _not_loaded(ctx: Any, report: "_Report") -> None:
    info = ctx.registry.module(ctx.module)
    rel = _module_rel(ctx.registry, ctx.module, ctx.path)
    if info is None or not rel:
        return
    lines = _file_lines(ctx, info.manifest) or []
    quoted = re.compile(r"['\"](?:\./)?" + re.escape(rel) + r"['\"]")
    if any(quoted.search(text.split("#", 1)[0]) for text in lines):
        return
    # only commented-out entries: unlisted files (fixtures, samples) are left alone
    rx = re.compile(r"#.*" + quoted.pattern)
    for i, text in enumerate(lines):
        if rx.search(text):
            msg = f"This file is not loaded: its entry is commented out in __manifest__.py:{i + 1} (Odoo never reads its records)"
            report.add(WARNING, "not-loaded", 0, 0, 1, msg)
            return


def _file_lines(ctx: Any, path: str) -> list[str] | None:
    lines = ctx.server.documents.lines(path) if hasattr(ctx.server, "documents") else None
    if lines is None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
        except OSError:
            return None
    return lines


_COMMENTED_IMPORT = re.compile(r"^\s*#+\s*from\s+\.\s+import\s+(.+)$")


def _commented_import(ctx: Any, init: str, name: str) -> int | None:
    for i, text in enumerate(_file_lines(ctx, init) or ()):
        m = _COMMENTED_IMPORT.match(text)
        if m and name in [n.strip().split(" as ")[0].strip() for n in m.group(1).split(",")]:
            return i
    return None


def _loaded_file(ctx: Any) -> bool:
    if ctx.kind in ("py", "manifest"):
        return True
    rel = _module_rel(ctx.registry, ctx.module, ctx.path)
    return rel is not None and (rel.startswith("static/src/") or rel in _manifest_files(ctx.registry, ctx.module))


def _module_rel(reg: Any, module: str | None, path: str) -> str | None:
    info = reg.module(module) if reg is not None and module else None
    return os.path.relpath(path, info.path).replace(os.sep, "/") if info is not None else None


def _manifest_files(reg: Any, module: str) -> set[str]:
    info = reg.module(module)
    manifest = reg.file_facts(info.manifest) if info is not None else None
    entries = [e for key in (MANIFEST_DATA, MANIFEST_DEMO) for e in (manifest or {}).get(key) or ()]
    return {os.path.normpath(e[0]).replace(os.sep, "/") for e in entries}


class _Report:
    def __init__(self) -> None:
        self.items: list[tuple] = []
        self._seen: set[tuple] = set()

    def add(self, severity: int, code: str, line: int, col: int, end_col: int, message: str) -> None:
        key = (line, col, end_col, code, message)
        if key in self._seen:
            return
        self._seen.add(key)
        self.items.append((severity, line, col, end_col, code, message))

    def result(self) -> list[dict]:
        self.items.sort(key=lambda d: (d[0], d[1], d[2]))
        return [
            {
                "range": {"start": {"line": line, "character": col}, "end": {"line": line, "character": end}},
                "severity": sev,
                "code": code,
                "source": SOURCE,
                "message": msg,
            }
            for sev, line, col, end, code, msg in self.items[:MAX_DIAGNOSTICS]
        ]


def _syntax_error(facts: dict, report: _Report) -> None:
    err = facts.get(ERROR)
    if not err:
        return
    msg, line, col = err
    if msg.startswith(("internal extractor error", "unsupported file kind")):
        return
    report.add(ERROR_, "syntax-error", line, col, col + 1, f"Syntax error: {msg}")


def _unique(items: Iterable[str | None]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def _quote_list(names: list[str], limit: int = 3) -> str:
    shown = ", ".join(f"'{n}'" for n in names[:limit])
    return shown + (f" (+{len(names) - limit} more)" if len(names) > limit else "")


def _suggest(name: str, candidates: Iterable[str]) -> str:
    match = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.8)
    return f"; did you mean '{match[0]}'?" if match else ""


def _vlabel(version: tuple[int, int]) -> str:
    return f"{version[0]}.0" if version[1] == 0 else f"saas-{version[0]}.{version[1]}"


def _segment_range(value: str, col: int, end: int, index: int, sep: str = ".") -> tuple[int, int]:
    segs = value.split(sep)
    if end - col != len(value) or index >= len(segs):
        return col, end
    start = col + sum(len(s) + len(sep) for s in segs[:index])
    return start, start + len(segs[index])


# non-literal _name ('x.%s' % y): no MI_NAME, so the registry guesses the model (usually _inherit[0])
def _misattributed(cls: Any) -> bool:
    return not cls.model_info.get(MI_NAME) and any(a[A_NAME] == "_name" for a in cls.facts[K_ATTRS])


def _registry_cache(reg: Any) -> dict:
    cache = reg.__dict__.get("diagnostics_cache")
    if cache is None or cache.get("generation") != reg.generation:
        cache = reg.__dict__["diagnostics_cache"] = {"generation": reg.generation}
    return cache


class _Resolver:
    def __init__(self, ctx: Any):
        reg = ctx.registry
        self.env = ctx.env
        self.reg = reg
        self.documents = ctx.server.documents
        self.module: str = ctx.module
        ver = ctx.env.version
        self.version: tuple[int, int] = ver.key() if ver is not None else (0, 0)
        self.label = ver.label if ver is not None else "?"
        self.cache = _registry_cache(reg)
        closure = ctx.scope or frozenset((self.module,))
        # closure + auto_install bridges: runtime = all of them, scope = those loaded before this module
        self.scope, self.runtime = self._effective_scopes(closure)
        info = reg.module(self.module)
        manifest = reg.file_facts(info.manifest) if info is not None else None
        self.deps_known = bool(manifest) and not manifest.get(ERROR)
        self.missing = sorted(m for m in closure if reg.module(m) is None)
        self.has_base = reg.module("base") is not None
        # a partial base (no ir.model) cannot tell which ir.* models exist
        self.full_base = reg.has_model("ir.model")
        self._xmlids: dict[tuple, tuple[str, list[str]]] = {}

    def _effective_scopes(self, closure: frozenset[str]) -> tuple[frozenset[str], frozenset[str]]:
        key = ("scopes", self.module)
        hit = self.cache.get(key)
        if hit is None:
            reg = self.reg
            auto = []
            for info in reg.modules():
                flag = info.values.get("auto_install")
                if flag:
                    auto.append((info.name, set(flag) if isinstance(flag, (list, tuple)) else set(info.depends)))
            runtime = set(closure)
            changed = True
            while changed:
                changed = False
                for name, triggers in auto:
                    if name not in runtime and triggers <= runtime:
                        runtime |= reg.closure(name)
                        changed = True
            me = reg.module(self.module)
            rank = me.rank if me is not None else -1
            load = set(closure)
            for name in runtime - closure:
                info = reg.module(name)
                if info is not None and info.rank < rank:
                    load.add(name)
            hit = self.cache[key] = (frozenset(load), frozenset(runtime))
        return hit

    def since(self, version: tuple[int, int]) -> bool:
        return self.version >= version

    def adjust(self, severity: int, message: str) -> tuple[int, str]:
        if not self.missing:
            return severity, message
        return min(severity + 1, HINT), f"{message} (unverified: dependency {_quote_list(self.missing)} is not in the addons path)"

    def module_rel(self, path: str) -> str | None:
        return _module_rel(self.reg, self.module, path)

    def loaded_by_manifest(self, path: str) -> bool:
        return self.module_rel(path) in _manifest_files(self.reg, self.module)

    def ignore_dependencies(self) -> None:
        self.deps_known = False

    def _within(self, modules: Iterable[str | None], runtime: bool) -> bool:
        scope = self.runtime if runtime else self.scope
        return any(m in scope for m in modules)

    def model_status(self, model: str, runtime: bool = False) -> tuple[str, list[str]]:
        if not self.has_base or model.startswith("x_") or (model.startswith("ir.") and not self.full_base):
            return "skip", []
        classes = self.reg.model_classes(model)
        if not classes:
            return ("skip" if model in self._unextracted_models() or self.dynamic_models(self.module) else "unknown"), []
        defs = [c.module for c in classes if c.is_definition]
        if not defs:
            return ("skip" if model in self._unextracted_models() or self.dynamic_models(self.module) else "unknown"), []
        if not self.deps_known or self._within(defs, runtime):
            return "ok", []
        return ("skip" if self.dynamic_models(self.module) else "outside"), _unique(defs)

    # _name built at runtime ('x.%s' % y, f-string, call): the module's model refs cannot be checked
    def dynamic_models(self, module: str) -> bool:
        key = ("dynamic_models", module)
        hit = self.cache.get(key)
        if hit is None:
            info = self.reg.module(module)
            prefix = info.path + os.sep if info is not None else "\0"
            hit = self.cache[key] = any(
                _DYNAMIC_NAME_RE.search(self.documents.source(path) or b"") is not None
                for path, _facts in self.reg.iter_files("py")
                if path.startswith(prefix)
            )
        return hit

    def _unextracted_models(self) -> set[str]:
        hit = self.cache.get("unextracted_models")
        if hit is None:
            hit = set()
            for _path, facts in self.reg.iter_files("py"):
                for cls in facts.get(CLASSES) or ():
                    if cls[K_MODEL] is None or not cls[K_MODEL].get(MI_NAME):
                        for a in cls[K_ATTRS]:
                            m = _QUOTED_NAME_RE.match(a[A_VALUE]) if a[A_NAME] == "_name" else None
                            if m:
                                hit.add(m.group(1))
            self.cache["unextracted_models"] = hit
        return hit

    # all parents in scope, no Python mixin base: a member it lacks is really missing
    def model_closed(self, model: str) -> bool:
        key = ("closed", model, self.scope)
        hit = self.cache.get(key)
        if hit is None:
            hit = self.cache[key] = self._closed(model)
        return hit

    def _closed(self, model: str) -> bool:
        reg = self.reg
        classes = []
        seen: set[str] = set()
        stack = [model, "base"]
        while stack:
            m = stack.pop()
            if m in seen:
                continue
            seen.add(m)
            own = reg.model_classes(m, self.scope)
            if not own:
                return False
            for c in own:
                classes.append(c)
                stack.extend(c.parents())
                stack.extend((c.model_info.get(MI_INHERITS) or {}).keys())
                stack.extend(f[F_COMODEL] for f in c.facts[K_FIELDS] if f[F_COMODEL] and (f[F_ARGS] or {}).get("delegate") is True)
        names = {c.name for c in classes}
        return not any(
            base.rsplit(".", 1)[-1] not in MODEL_BASES and base.rsplit(".", 1)[-1] not in names for c in classes for base in c.facts[K_BASES]
        )

    def is_abstract(self, model: str) -> bool:
        return model == "base" or self.reg.model_kind(model) == "AbstractModel"

    def descendants(self, model: str) -> list[str]:
        key = ("descendants", model)
        hit = self.cache.get(key)
        if hit is None:
            seen: dict[str, None] = {}
            stack = [model]
            while stack and len(seen) < 3000:
                for child in self.reg.model_children(stack.pop()):
                    if child not in seen:
                        seen[child] = None
                        stack.append(child)
            hit = self.cache[key] = list(seen)
        return hit

    def suggest_model(self, model: str) -> str:
        defined = [m for m in self.reg.model_names() if m != model and any(c.is_definition for c in self.reg.model_classes(m))]
        return _suggest(model, defined)

    # a member exists if any class of model/parents/base/framework declares it: union, not MRO
    def _union(self, model: str, scope: frozenset[str] | None, delegated: bool) -> list:
        key = ("union", model, scope, delegated)
        hit = self.cache.get(key)
        if hit is None:
            reg = self.reg
            hit = []
            seen: set[str] = set()
            stack = [model, "base"]
            while stack:
                m = stack.pop()
                if m in seen:
                    continue
                seen.add(m)
                for c in reg.model_classes(m, scope):
                    if _misattributed(c):
                        continue
                    hit.append(c)
                    stack.extend(c.parents())
                    if delegated:
                        stack.extend((c.model_info.get(MI_INHERITS) or {}).keys())
                        stack.extend(f[F_COMODEL] for f in c.facts[K_FIELDS] if f[F_COMODEL] and (f[F_ARGS] or {}).get("delegate") is True)
            hit.extend(reg.framework_classes())
            self.cache[key] = hit
        return hit

    def members(self, model: str, which: int, scope: frozenset[str] | None) -> dict[str, tuple]:
        key = ("members", model, which, scope)
        hit = self.cache.get(key)
        if hit is None:
            hit = {}
            for c in self._union(model, scope, which == K_FIELDS):
                for member in c.facts[which]:
                    prev = hit.get(member[F_NAME])
                    if prev is None or (which == K_FIELDS and not prev[F_COMODEL] and member[F_COMODEL]):
                        hit[member[F_NAME]] = member
            if which == K_FIELDS:
                if not self.since((19, 0)):  # 16-18 create the _inherits many2one automatically
                    for parent, link in self.reg.model_inherits(model, scope).items():
                        hit.setdefault(link, (link, 0, 0, 0, 0, "Many2one", parent, {}))
                for name, (ftype, comodel) in MAGIC_FIELDS.items():
                    if name == "__last_update" and self.since((17, 0)):
                        continue
                    if name not in hit and (name not in LOG_ACCESS_FIELDS or self._log_access(model)):
                        hit[name] = (name, 0, 0, 0, 0, ftype, comodel, {})
            self.cache[key] = hit
        return hit

    def computes(self, model: str) -> set[str]:
        key = ("computes", model, self.scope)
        hit = self.cache.get(key)
        if hit is None:
            hit = set()
            for c in self._union(model, self.scope, False):
                for f in c.facts[K_FIELDS]:
                    value = (f[F_ARGS] or {}).get("compute")
                    if isinstance(value, str):
                        hit.add(value)
            self.cache[key] = hit
        return hit

    def declares(self, model: str, name: str, which: int, module: str | None = None) -> bool:
        classes = self._union(model, self.scope if module is None else None, False)
        return any((module is None or c.module == module) and any(m[F_NAME] == name for m in c.facts[which]) for c in classes)

    def field_status(self, model: str, name: str, runtime: bool = False) -> tuple[str, list[str], tuple | None]:
        fdef = self.members(model, K_FIELDS, self.runtime if runtime else self.scope).get(name)
        if fdef is not None:
            return "ok", [], fdef
        fdef = self.members(model, K_FIELDS, None).get(name)
        if fdef is None:
            return "unknown", [], None
        if not self.deps_known:
            return "ok", [], fdef
        modules = self._providers(model, name, K_FIELDS)
        return ("outside" if modules else "unknown"), modules, fdef

    def method_status(self, model: str, name: str, runtime: bool = False) -> tuple[str, list[str]]:
        if name in self.members(model, K_METHODS, self.runtime if runtime else self.scope):
            return "ok", []
        if name not in self.members(model, K_METHODS, None):
            return "unknown", []
        if not self.deps_known:
            return "ok", []
        modules = self._providers(model, name, K_METHODS)
        return ("outside" if modules else "unknown"), modules

    def member_of_any(self, models: Iterable[str], name: str, which: int) -> bool:
        return any(name in self.members(m, which, None) for m in models)

    def _providers(self, model: str, name: str, which: int) -> list[str]:
        out = []
        for c in self._union(model, None, which == K_FIELDS):
            if c.module is None or c.module in self.scope:
                continue
            if any(m[F_NAME] == name for m in c.facts[which]) or any(name in self.members(p, which, None) for p in c.parents()):
                out.append(c.module)
        return _unique(out)

    # Odoo's default (_auto, False on abstract models); registry._log_access defaults to True
    def _log_access(self, model: str) -> bool:
        classes = self.reg.model_classes(model)
        auto = None
        for c in reversed(classes):
            attrs = c.model_info.get(MI_ATTRS) or {}
            if "_auto" in attrs:
                auto = bool(attrs["_auto"])
                break
        log = auto if auto is not None else self.reg.model_kind(model) not in ("AbstractModel", "BaseModel")
        for c in classes:
            attrs = c.model_info.get(MI_ATTRS) or {}
            if "_log_access" in attrs:
                log = bool(attrs["_log_access"])
        return log

    # vals pseudo-fields in create/write (account.account 'prefix'), setattr fields (ir.rule 'global')
    def mentioned_in_code(self, model: str, name: str) -> bool:
        key = ("mentioned", model, name)
        hit = self.cache.get(key)
        if hit is None:
            q = rf"""['"]{re.escape(name)}['"]"""
            rx = re.compile(
                rf"""(?:\b\w*val\w*\s*(?:\.\s*(?:get|pop|setdefault)\s*\(\s*|\[\s*){q})"""
                rf"""|(?:{q}\s+(?:not\s+)?in\s+\w*val)"""
                rf"""|(?:setattr\s*\([^,]+,\s*{q})|(?:locals\s*\(\s*\)\s*\[\s*{q})"""
            )
            paths = _unique(c.path for c in self.reg.model_classes(model))
            hit = self.cache[key] = any(rx.search(self.documents.text(p) or "") for p in paths)
        return hit

    def comodel(self, model: str, name: str) -> str | None:
        fdef = self.members(model, K_FIELDS, self.scope).get(name) or self.members(model, K_FIELDS, None).get(name)
        if fdef is None:
            return None
        comodel = fdef[F_COMODEL] or (fdef[F_ARGS] or {}).get("comodel_name")
        return comodel if isinstance(comodel, str) else None

    # None: resolves or unjudgeable; else (segment index, 'unknown'|'outside'|'nonrel', model, detail)
    def walk(self, model: str, path: str, transient_only: bool = False) -> tuple[int, str, str, Any] | None:
        cur = model
        segs = path.split(".")
        for i, seg in enumerate(segs):
            if not seg or (transient_only and i and self.reg.model_kind(cur) != "TransientModel"):
                return None
            status, modules, fdef = self.field_status(cur, seg)
            if status != "ok" and not self.model_closed(cur):
                return None
            if status == "unknown":
                if seg.startswith("x_"):
                    return None
                return i, "unknown", cur, None
            if status == "outside":
                return i, "outside", cur, modules
            if i == len(segs) - 1 or fdef is None:
                return None
            nxt = self.comodel(cur, seg)
            if nxt is None:
                return (i + 1, "nonrel", cur, (seg, fdef[F_TYPE])) if fdef[F_TYPE] in NON_RELATIONAL else None
            if self.model_status(nxt)[0] != "ok":
                return None
            cur = nxt
        return None

    def walk_steps(self, model: str | None, steps: tuple) -> str | None:
        for name in steps:
            if model is None:
                return None
            model = self.comodel(model, name)
        return model

    def suggest_field(self, model: str, name: str) -> str:
        return self.unimported_member(model, name, K_FIELDS) or _suggest(name, self.members(model, K_FIELDS, self.scope))

    def suggest_method(self, model: str, name: str) -> str:
        return self.unimported_member(model, name, K_METHODS) or _suggest(name, self.members(model, K_METHODS, self.scope))

    def unimported_member(self, model: str, name: str, index: int) -> str:
        hit = self.cache.get("unimported_members")
        if hit is None:
            hit = {}
            reg = self.reg
            if hasattr(reg, "is_loaded"):
                major = getattr(getattr(self.env, "version", None), "major", None)
                for path, facts in reg.iter_files("py"):
                    if reg.module_of_path(path) is None or reg.is_loaded(path) or f"{os.sep}tests{os.sep}" in path:
                        continue
                    for c in facts.get(CLASSES) or ():
                        owner = model_class_info(c, major)[0] if c[K_MODEL] else None
                        if not owner:
                            continue
                        root = self.env.root_of(path)
                        rel = os.path.relpath(path, root) if root else path
                        for idx in (K_FIELDS, K_METHODS):
                            for item in c[idx]:
                                hit.setdefault((owner, idx, item[F_NAME]), (rel, item[F_LINE] + 1))
            self.cache["unimported_members"] = hit
        found = hit.get((model, index, name))
        if not found:
            return ""
        return f"; it is declared in {found[0]}:{found[1]}, a file its module never imports (add it to the package's __init__.py)"

    def xmlid_status(self, xmlid: str, runtime: bool = False) -> tuple[str, list[str]]:
        key = (xmlid, runtime, self.deps_known)
        hit = self._xmlids.get(key)
        if hit is None:
            hit = self._xmlids[key] = self._xmlid_status(xmlid, runtime)
        return hit

    def _xmlid_status(self, xmlid: str, runtime: bool) -> tuple[str, list[str]]:
        module, _, local = xmlid.partition(".")
        if not local or not self.has_base or self.reg.module(module) is None:
            return "skip", []
        recs = self.reg.records(xmlid)
        if not recs and not self._implicit_exists(module, local):
            if xmlid in self._dynamic_ids() or self._creates_ids(module, xmlid):
                return "skip", []
            return "unknown", [module]
        if not self.deps_known or self._within([module] + [r.module for r in recs], runtime):
            return "ok", []
        return "outside", [module]

    def suggest_xmlid(self, xmlid: str) -> str:
        module, _, local = xmlid.partition(".")
        if local.startswith("model_"):
            match = difflib.get_close_matches(local[6:], list(self._underscored()), n=1, cutoff=0.8)
            if match:
                model = self._underscored()[match[0]][0]
                modules = [c.module for c in self.reg.model_classes(model) if c.module]
                owner = module if module in modules else next((c.module for c in self.reg.model_classes(model) if c.is_definition), None)
                if owner:
                    return f"; did you mean '{owner}.model_{match[0]}'?"
        # built once per registry generation: rescanning ~200k ids per unknown id cost 150-300 ms
        by_module, by_local = self._xmlid_tables()
        match = difflib.get_close_matches(local, by_module.get(module, ()), n=1, cutoff=0.8)
        if match:
            return f"; did you mean '{module}.{match[0]}'?"
        moved = by_local.get(local)
        return f"; did you mean '{moved}'?" if moved else ""

    def _xmlid_tables(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        hit = self.cache.get("xmlid_tables")
        if hit is None:
            by_module: dict[str, list[str]] = {}
            by_local: dict[str, str] = {}
            for x in self.reg.xmlids():
                mod, _, loc = x.partition(".")
                by_module.setdefault(mod, []).append(loc)
                by_local.setdefault(loc, x)
            hit = self.cache["xmlid_tables"] = (by_module, by_local)
        return hit

    # QWeb t-call resolves view keys (<field name="key">) before xml ids
    def template_key_exists(self, key: str) -> bool:
        keys = self.cache.get("template_keys")
        if keys is None:
            keys = set()
            for _path, facts in self.reg.iter_files("xml"):
                for rec in facts.get(RECORDS) or ():
                    k = (rec[X_INFO] or {}).get("key")
                    if k:
                        keys.add(k)
            self.cache["template_keys"] = keys
        return key in keys

    # ids Odoo creates itself: model_/field_/selection__/constraint_, base.module_*, _inherits parents
    def _implicit_exists(self, module: str, local: str) -> bool:
        if local.startswith(("selection__", "constraint_")) or (module == "base" and local.startswith("module_")):
            return True
        if local.startswith(("model_", "field_")) and (
            self.dynamic_models(module)
            or any(local[6:] == m.replace(".", "_") or local[6:].startswith(m.replace(".", "_") + "__") for m in self._unextracted_models())
        ):
            return True
        reg = self.reg
        if local.startswith("field_") and "__" in local:
            mpart, _, fname = local[6:].rpartition("__")
            for model in self._underscored().get(mpart, ()):
                if fname in self.members(model, K_FIELDS, None) and (
                    any(c.module == module for c in reg.model_classes(model)) or self.declares(model, fname, K_FIELDS, module)
                ):
                    return True
            return False
        for suffix in self._inherits_suffixes():
            if local.endswith(suffix) and len(local) > len(suffix) and reg.records(f"{module}.{local[: -len(suffix)]}"):
                return True
        return False

    def _underscored(self) -> dict[str, list[str]]:
        hit = self.cache.get("underscored")
        if hit is None:
            hit = {}
            for model in self.reg.model_names():
                hit.setdefault(model.replace(".", "_"), []).append(model)
            self.cache["underscored"] = hit
        return hit

    def _inherits_suffixes(self) -> list[str]:
        hit = self.cache.get("inherits_suffixes")
        if hit is None:
            parents: set[str] = set()
            for model in self.reg.model_names():
                for c in self.reg.model_classes(model):
                    parents.update((c.model_info.get(MI_INHERITS) or {}).keys())
                    for f in c.facts[K_FIELDS]:
                        if f[F_COMODEL] and (f[F_ARGS] or {}).get("delegate") is True:
                            parents.add(f[F_COMODEL])
            hit = self.cache["inherits_suffixes"] = ["_" + p.replace(".", "_") for p in sorted(parents)]
        return hit

    def _dynamic_ids(self) -> set[str]:
        hit = self.cache.get("dynamic_ids")
        if hit is None:
            hit = set()
            for ref in self.reg.refs_by_name(REF_METHOD, "_update_xmlids"):
                if ref.path.endswith(".xml"):
                    hit.update(_XMLID_KEY_RE.findall(self.documents.text(ref.path) or ""))
            self.cache["dynamic_ids"] = hit
        return hit

    # Python-created ids ('xml_id' keys) or opaque ones (SQL, ir.model.data create); base's are implicit
    def _creates_ids(self, module: str, xmlid: str) -> bool:
        key = ("creates_ids", module)
        hit = self.cache.get(key)
        if hit is None:
            ids: set[str] = set()
            opaque = False
            info = self.reg.module(module)
            prefix = info.path + os.sep if info is not None else "\0"
            for path, _facts in self.reg.iter_files("py"):
                if not path.startswith(prefix) or os.sep + "tests" + os.sep in path[len(prefix) - 1 :]:
                    continue
                src = self.documents.source(path) or b""
                if b"xml_id" in src:
                    ids.update(_XMLID_KEY_RE.findall(src.decode("utf-8", "replace")))
                opaque = opaque or (module != "base" and _XMLID_SQL_RE.search(src) is not None)
            if not opaque and module != "base":
                for ref in self.reg.refs_to_model("ir.model.data"):
                    if ref.module == module and ref.path.endswith(".py") and ".create(" in self.documents.line(ref.path, ref.line):
                        opaque = True
                        break
            hit = self.cache[key] = (frozenset(ids), opaque)
        return hit[1] or xmlid in hit[0]

    def unimported_model_file(self, model: str) -> str | None:
        hit = self.cache.get("unimported_models")
        if hit is None:
            hit = {}
            reg = self.reg
            if hasattr(reg, "is_loaded"):
                for path, facts in reg.iter_files("py"):
                    if reg.module_of_path(path) is None or reg.is_loaded(path) or f"{os.sep}tests{os.sep}" in path:
                        continue
                    for c in facts.get(CLASSES) or ():
                        name = (c[K_MODEL] or {}).get(MI_NAME)
                        if name:
                            root = self.env.root_of(path)
                            hit.setdefault(name, os.path.relpath(path, root) if root else path)
            self.cache["unimported_models"] = hit
        return hit.get(model)

    def manual_fields(self, module: str) -> bool:
        key = ("manual_fields", module)
        hit = self.cache.get(key)
        if hit is None:
            info = self.reg.module(module)
            prefix = info.path + os.sep if info is not None else "\0"
            hit = any(
                path.startswith(prefix) and any(r[X_MODEL] == "ir.model.fields" for r in facts.get(RECORDS) or ())
                for path, facts in self.reg.iter_files("xml")
            )
            self.cache[key] = hit
        return hit


def _report_model(
    report: _Report, res: _Resolver, model: str, pos: tuple[int, int, int], severity: int, context: str, inherit: bool = False, runtime: bool = False
) -> None:
    status, modules = res.model_status(model, runtime)
    line, col, end = pos
    if status == "unknown":
        rename = MODEL_RENAMES.get(model)
        if rename and res.since(rename[0]) and res.reg.has_model(rename[1]):
            msg = f"Model '{model}' ({context}) no longer exists since Odoo {_vlabel(rename[0])}: use '{rename[1]}'"
        elif res.reg.model_classes(model):
            extenders = _unique(c.module for c in res.reg.model_classes(model))
            msg = (
                f"Model '{model}' ({context}) is defined nowhere in this Odoo {res.label} env, only extended "
                f"(by {_quote_list(extenders)}): removed or renamed in this version?{res.suggest_model(model)}"
            )
        elif res.unimported_model_file(model):
            msg = (
                f"Model '{model}' ({context}) is defined in {res.unimported_model_file(model)}, "
                f"a file its module never imports (add it to the package's __init__.py)"
            )
        else:
            msg = f"Model '{model}' ({context}) does not exist in this Odoo {res.label} env{res.suggest_model(model)}"
        sev, msg = res.adjust(severity, msg)
        report.add(sev, "unknown-model", line, col, end, msg)
    elif status == "outside":
        tail = f"; loading '{res.module}' fails without it" if inherit else ""
        _report_missing_dep(report, res, f"Model '{model}' ({context})", modules, pos, tail)


def _report_missing_dep(report: _Report, res: _Resolver, what: str, modules: list[str], pos: tuple[int, int, int], tail: str = "") -> None:
    first = modules[0] if modules else "?"
    msg = f"{what} is defined in module {_quote_list(modules)}, which is not in the dependencies of '{res.module}' (add '{first}' to depends{tail})"
    sev, msg = res.adjust(WARNING, msg)
    report.add(sev, "missing-dependency", pos[0], pos[1], pos[2], msg)


def _report_xmlid(
    report: _Report, res: _Resolver, xmlid: str, pos: tuple[int, int, int], severity: int, context: str, runtime: bool = False, dependency: bool = True
) -> None:
    status, modules = res.xmlid_status(xmlid, runtime)
    if status == "unknown":
        module, _, local = xmlid.partition(".")
        msg = f"External ID '{xmlid}' ({context}) does not exist: module '{module}' defines no record '{local}'{res.suggest_xmlid(xmlid)}"
        report.add(severity, "unknown-xmlid", pos[0], pos[1], pos[2], msg)
    elif status == "outside" and dependency:
        _report_missing_dep(report, res, f"External ID '{xmlid}' ({context})", modules, pos)


class _ClassCtx:
    __slots__ = ("facts", "model", "registered", "abstract")

    def __init__(self, facts: tuple, model: str, registered: bool, abstract: bool):
        self.facts = facts
        self.model = model
        self.registered = registered
        self.abstract = abstract

    @property
    def start(self) -> int:
        return self.facts[K_LINE]

    @property
    def end(self) -> int:
        return self.facts[K_END_LINE]


class _PythonChecker:
    def __init__(self, ctx: Any, res: _Resolver, facts: dict, report: _Report):
        self.ctx = ctx
        self.res = res
        self.reg = res.reg
        self.facts = facts
        self.report = report
        dirs = (res.module_rel(ctx.path) or "").split("/")[:-1]
        self.skip = bool(UNLOADED_DIRS.intersection(dirs) - {"tests"})
        self.tests = "tests" in dirs or "populate" in dirs
        self.text = ctx.text
        self._lines: list[str] | None = None
        self._guards: dict[str, bool] = {}
        self.classes = self._classes()
        self.local_models = {c.model for c in self.classes if c.facts[K_MODEL].get(MI_NAME) == c.model and c.model not in c.facts[K_MODEL].get(MI_INHERIT, ())}

    def _classes(self) -> list[_ClassCtx]:
        out = []
        major = self.res.version[0] or None
        for idx, cls in enumerate(self.facts.get(CLASSES) or ()):
            mi = cls[K_MODEL]
            if not mi or mi.get(MI_KIND) == "framework":
                continue
            model = model_class_info(cls, major)[0]
            if not model:
                continue
            registered = any(c.path == self.ctx.path and c.index == idx for c in self.reg.model_classes(model))
            out.append(_ClassCtx(cls, model, registered, registered and self.res.is_abstract(model)))
        return out

    @property
    def lines(self) -> list[str]:
        if self._lines is None:
            self._lines = self.text.split("\n")
        return self._lines

    def _line(self, n: int) -> str:
        lines = self.lines
        return lines[n] if 0 <= n < len(lines) else ""

    def _find(self, first: int, last: int, pattern: re.Pattern, group: int = 0) -> tuple[int, int, int] | None:
        found = None
        for n in range(max(0, first), last + 1):
            line = self._line(n)
            for m in pattern.finditer(line):
                found = (n, utf16_len(line[: m.start(group)]), utf16_len(line[: m.end(group)]))
        return found

    # literal inside a comment or a larger string (generated code)
    def _inert(self, line: int, col: int) -> bool:
        text = self._line(line)[: max(0, utf16_to_index(self._line(line), col) - 1)]
        quote = None
        for ch in text:
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "#":
                return True
        return quote is not None

    def class_at(self, line: int) -> _ClassCtx | None:
        for c in self.classes:
            if c.start <= line <= c.end:
                return c
        return None

    def _field_def(self, cls: _ClassCtx, line: int) -> tuple | None:
        for f in cls.facts[K_FIELDS]:
            if f[F_LINE] <= line <= f[F_END_LINE]:
                return f
        return None

    def field_at(self, cls: _ClassCtx, line: int) -> str | None:
        f = self._field_def(cls, line)
        return f[F_NAME] if f is not None else None

    def run(self) -> None:
        if self.skip:
            return
        for c in self.classes:
            self._check_class(c)
        if self.res.since((18, 0)):
            self._user_has_groups()
        if self.tests:
            return  # tests may target optional or removed records behind skip()s
        for ref in self.facts.get(REFS) or ():
            kind = ref[R_KIND]
            role = ref[R_CTX][0]
            if kind in (REF_MODEL, REF_XMLID) and role in ("env", "res_model", "ref", "groups") and self._inert(ref[R_LINE], ref[R_COL]):
                continue
            if kind == REF_MODEL:
                self._model_ref(ref, role)
            elif kind == REF_FIELD and role in PY_FIELD_ROLES:
                self._field_ref(ref, role)
            elif kind == REF_METHOD and role in PY_METHOD_ROLES:
                self._method_ref(ref, role)
            elif kind == REF_XMLID and role in ("ref", "groups"):
                self._xmlid_ref(ref, role)

    def _model_ref(self, ref: tuple, role: str) -> None:
        model = ref[R_VALUE]
        if model in self.local_models or role == "_name":
            return
        pos = (ref[R_LINE], ref[R_COL], ref[R_END_COL])
        if role in ("_inherit", "_inherits"):
            _report_model(self.report, self.res, model, pos, ERROR_, role, inherit=True)
        elif role == "comodel":
            cls = self.class_at(ref[R_LINE])
            field = self._field_def(cls, ref[R_LINE]) if cls is not None else None
            if field is not None and "related" in (field[F_ARGS] or {}):
                return
            name = field[F_NAME] if field is not None else None
            _report_model(self.report, self.res, model, pos, ERROR_, f"comodel of field '{name}'" if name else "comodel")
        elif role == "env" or role == "res_model":
            status = self.res.model_status(model, runtime=True)[0]
            # a res_model string is only data until the client opens it
            if (status == "unknown" or (status == "outside" and role == "env")) and not self._guarded(model, ref[R_LINE]):
                _report_model(self.report, self.res, model, pos, WARNING, "env[...]" if role == "env" else "res_model", runtime=True)

    def _guarded(self, model: str, line: int) -> bool:
        hit = self._guards.get(model)
        if hit is None:
            q = re.escape(model)
            text = self.text
            hit = self._guards[model] = bool(
                re.search(rf"""{q}['"]\s+(?:not\s+)?in\s+[\w.]*(?:env|registry|models|pool)\b""", text)
                or re.search(rf"""\.get\(\s*['"]{q}['"]""", text)
            )
        return hit or self._method_guarded(line, model)

    def _method_guarded(self, line: int, model: str | None = None) -> bool:
        span = self._method_span(line)
        if span is None:
            return False
        body = "\n".join(self.lines[span[0] : span[1] + 1])
        if model is not None and re.search(rf"""==\s*['"]{re.escape(model)}['"]""", body):
            return True
        return _OPTIONAL_GUARD_RE.search(body) is not None

    def _method_span(self, line: int) -> tuple[int, int] | None:
        for cls in self.facts.get(CLASSES) or ():
            if cls[K_LINE] <= line <= cls[K_END_LINE]:
                for m in cls[K_METHODS]:
                    if m[M_LINE] <= line <= m[M_END_LINE]:
                        return m[M_LINE], m[M_END_LINE]
        for sym in self.facts.get(SYMBOLS) or ():
            if sym[S_KIND] == "def" and sym[S_LINE] <= line <= sym[S_END_LINE]:
                return sym[S_LINE], sym[S_END_LINE]
        return None

    def _field_ref(self, ref: tuple, role: str) -> None:
        severity, label = PY_FIELD_ROLES[role]
        value = ref[R_VALUE]
        line, col, end = ref[R_LINE], ref[R_COL], ref[R_END_COL]
        cls = self.class_at(line)
        if role in ("inverse_name", "domain"):
            model = ref[R_CTX][1]
            if not model or self.res.model_status(model)[0] != "ok":
                return
        else:
            if cls is None or not cls.registered:
                return
            model = cls.model
            if self.res.model_status(model)[0] == "unknown":
                return
        path = value
        if role == "_order":
            path = value.strip('"').split(".", 1)[0].split(":", 1)[0]
            if value.startswith('"'):
                col, end = col + 1, col + 1 + len(path)
        resolved = True
        if role == "depends" and cls is not None:
            state = self._depends_state(cls, line)
            if state == "replaced":
                return
            resolved = state == "compute"
        if role in SIMPLE_NAME_ROLES and "." in path:
            if role in ("constrains", "onchange"):
                msg = f"{label}('{value}') on model '{model}': dotted paths are not supported, only field names (Odoo ignores it)"
                self.report.add(WARNING, "unknown-field", line, col, end, msg)
            return
        # a wizard's dependencies on regular models are not resolved (Field.resolve_depends)
        transient = role == "depends" and self.reg.model_kind(model) == "TransientModel"
        problem = self.res.walk(model, path, transient)
        if problem is None:
            return
        index, kind, owner, detail = problem
        if role in LATE_BOUND_ROLES and cls is not None and cls.abstract and model == cls.model and self._late_bound_ok(model, path):
            return
        if not resolved:
            if kind != "unknown":
                return
            severity = WARNING
        segs = path.split(".")
        seg = segs[index] if index < len(segs) else path
        c0, c1 = _segment_range(path, col, end, index) if path == value else (col, end)
        where = f"{label} '{value}'" if "." in value else label
        if kind == "unknown":
            msg = f"Field '{seg}' does not exist on model '{owner}' ({where}){self.res.suggest_field(owner, seg)}"
        elif kind == "outside":
            _report_missing_dep(self.report, self.res, f"Field '{owner}.{seg}' ({where})", detail, (line, c0, c1))
            return
        else:
            prev, ftype = detail
            msg = f"'{seg}' cannot follow '{prev}' ({ftype} field of model '{owner}', not relational) in {where}"
        sev, msg = self.res.adjust(severity, msg)
        self.report.add(sev, "unknown-field", line, c0, c1, msg)

    # 'compute': Odoo resolves it; 'replaced': an outer @api.depends wins; 'other': never resolved
    def _depends_state(self, cls: _ClassCtx, line: int) -> str:
        methods = cls.facts[K_METHODS]
        index = next((i for i, m in enumerate(methods) if m[M_LINE] >= line), None)
        if index is None:
            return "other"
        method = methods[index]
        first = methods[index - 1][M_END_LINE] + 1 if index else cls.start
        starts = [n for n in range(first, method[M_LINE]) if _DEPENDS_DECORATOR_RE.match(self._line(n))]
        if len(starts) > 1 and line >= starts[1]:
            return "replaced"
        return "compute" if method[M_NAME] in self.res.computes(cls.model) else "other"

    def _late_bound_ok(self, model: str, path: str) -> bool:
        if model == "base":
            return bool(self.reg.models_with_member(path.split(".", 1)[0]))
        return any(self.res.walk(d, path) is None for d in self.res.descendants(model))

    def _method_ref(self, ref: tuple, role: str) -> None:
        name = ref[R_VALUE]
        line, col, end = ref[R_LINE], ref[R_COL], ref[R_END_COL]
        cls = self.class_at(line)
        if cls is None or not cls.registered:
            return
        model = cls.model
        field = self.field_at(cls, line) or "?"
        status, modules = self.res.method_status(model, name)
        if status == "ok":
            if role == "default":
                msg = f"default='{name}' of field '{field}' is a plain string value, not a method call: use default=lambda self: self.{name}()"
                self.report.add(WARNING, "invalid-default", line, col, end, msg)
            return
        if not self.res.model_closed(model):
            return
        if cls.abstract and (model == "base" or self.res.member_of_any(self.res.descendants(model), name, K_METHODS)):
            return
        if status == "outside":
            _report_missing_dep(self.report, self.res, f"Method '{model}.{name}' ({role}= of field '{field}')", modules, (line, col, end))
            return
        msg = f"Method '{name}' ({role}= of field '{field}') is not defined on model '{model}'{self.res.suggest_method(model, name)}"
        sev, msg = self.res.adjust(ERROR_, msg)
        self.report.add(sev, "unknown-method", line, col, end, msg)

    def _xmlid_ref(self, ref: tuple, role: str) -> None:
        if role == "groups":
            # has_group() and field groups= tolerate groups of modules not installed
            _report_xmlid(self.report, self.res, ref[R_VALUE], (ref[R_LINE], ref[R_COL], ref[R_END_COL]), WARNING, "groups", dependency=False)
            return
        call, soft = self._lookup_call(ref)
        if soft:
            return
        guarded = self._method_guarded(ref[R_LINE])
        context = "env.ref()" if call == "ref" else f"{call}()"
        _report_xmlid(self.report, self.res, ref[R_VALUE], (ref[R_LINE], ref[R_COL], ref[R_END_COL]), WARNING, context, runtime=True, dependency=not guarded)

    def _lookup_call(self, ref: tuple) -> tuple[str, bool]:
        line = self._line(ref[R_LINE])
        start = utf16_to_index(line, ref[R_COL])
        stop = utf16_to_index(line, ref[R_END_COL])
        m = _CALL_BEFORE_RE.search(line[: max(0, start - 1)])
        call = m.group(2) if m else "ref"
        tail = line[stop:]
        if call == "ref" and (m is None or m.group(1) not in ("env", "self", "cls")):
            return call, True  # e.g. account.chart.template.ref(): template ids, not xml ids
        if re.match(r"""['"]\s*(?:[+%]|\.format\()""", tail):
            return call, True
        n = ref[R_LINE]
        while ")" not in tail and n < ref[R_LINE] + 4:
            n += 1
            tail += " " + self._line(n)
        tail = tail.split(")", 1)[0]
        if call in SOFT_LOOKUPS:
            return call, "raise_if_not_found=True" not in tail.replace(" ", "")
        return call, _NOT_FOUND_FALSE_RE.search(tail) is not None

    def _check_class(self, c: _ClassCtx) -> None:
        cls = c.facts
        mi = cls[K_MODEL]
        res = self.res
        if res.since((19, 0)) and not mi.get(MI_NAME) and mi.get(MI_INHERIT) and not mi.get(MI_INHERIT_STR):
            target = mi[MI_INHERIT][0]
            if c.model != target:
                pos = self._find(c.start, c.end, re.compile(r"\b_inherit\b")) or (cls[K_LINE], cls[K_COL], cls[K_END_COL])
                msg = (
                    f"Since Odoo 19.0 a list _inherit without _name defines a new model '{c.model}' (derived from the class name) "
                    f"instead of extending '{target}': write _inherit = '{target}'"
                )
                self.report.add(WARNING, "deprecated", *pos, msg)
        for m in cls[K_METHODS]:
            if m[M_NAME] == "name_get" and res.since((17, 0)):
                state = "removed in 18.0" if res.since((18, 0)) else "deprecated"
                msg = f"name_get() is no longer used for display names since Odoo 17.0 ({state}): override _compute_display_name() on '{c.model}'"
                self.report.add(WARNING, "deprecated", m[M_LINE], m[M_COL], m[M_END_COL], msg)
            for dec, _args in m[M_DECORATORS]:
                key = ".".join(dec.split(".")[-2:])
                removed = REMOVED_DECORATORS.get(key)
                if removed and res.since(removed[0]):
                    rx = re.compile(r"@\s*(?:odoo\.)?" + re.escape(key) + r"\b")
                    pos = self._find(m[M_LINE] - 12, m[M_LINE], rx) or (m[M_LINE], m[M_COL], m[M_END_COL])
                    self.report.add(ERROR_, "deprecated", *pos, removed[1])
        if res.since((19, 0)):
            for a in cls[K_ATTRS]:
                if a[A_KIND] == "sql_constraints":
                    msg = f"Since Odoo 19.0 _sql_constraints is ignored on '{c.model}': declare models.Constraint('...', '...') class attributes"
                    self.report.add(WARNING, "deprecated", a[A_LINE], a[A_COL], a[A_END_COL], msg)
        for f in cls[K_FIELDS]:
            self._field_kwargs(f)
        if res.since((19, 0)) and c.registered:
            self._inherits_links(c)

    def _field_kwargs(self, f: tuple) -> None:
        args = f[F_ARGS] or {}
        for kw, since, msg in FIELD_KWARG_DEPRECATIONS:
            if kw not in args or not self.res.since(since):
                continue
            severity = WARNING
            if kw == "group_operator" and not self.res.since((19, 1)):
                severity, msg = INFO, msg + " (deprecated alias)"
            pos = self._find(f[F_LINE], f[F_END_LINE], re.compile(r"\b" + kw + r"\s*=")) or (f[F_LINE], f[F_COL], f[F_END_COL])
            self.report.add(severity, "deprecated", pos[0], pos[1], pos[1] + len(kw), f"{msg} (field '{f[F_NAME]}')")

    # 19.0+: the _inherits many2one is no longer created automatically
    def _inherits_links(self, c: _ClassCtx) -> None:
        inherits = c.facts[K_MODEL].get(MI_INHERITS) or {}
        if not inherits:
            return
        for parent, fname in inherits.items():
            if self.res.declares(c.model, fname, K_FIELDS):
                continue
            pos = None
            for ref in self.facts.get(REFS) or ():
                if ref[R_KIND] == REF_FIELD and ref[R_CTX][0] == "_inherits" and ref[R_VALUE] == fname and c.start <= ref[R_LINE] <= c.end:
                    pos = (ref[R_LINE], ref[R_COL], ref[R_END_COL])
                    break
            pos = pos or (c.facts[K_LINE], c.facts[K_COL], c.facts[K_END_COL])
            msg = (
                f"Since Odoo 19.0 the _inherits link field '{fname}' must be declared on '{c.model}': "
                f"{fname} = fields.Many2one('{parent}', required=True, ondelete='cascade')"
            )
            self.report.add(ERROR_, "unknown-field", *pos, msg)

    def _user_has_groups(self) -> None:
        if self.reg.models_with_member("user_has_groups"):
            return
        src = self.ctx.source
        if b"user_has_groups(" not in src:
            return
        idx = SrcIndex(src)
        for m in _USER_HAS_GROUPS_RE.finditer(src):
            line, c0, c1 = idx.span(m.start() + 1, m.end() - 1)
            msg = "user_has_groups() was removed in Odoo 18.0: use self.env.user.has_group('module.group') (or has_groups('a,!b'))"
            self.report.add(ERROR_, "deprecated", line, c0, c1, msg)


class _View:
    __slots__ = ("xmlid", "model", "inherit", "vtype", "root", "items")

    def __init__(self, xmlid: str | None):
        self.xmlid = xmlid
        self.model: str | None = None
        self.inherit: str | None = None
        self.vtype: str | None = None
        self.root: str | None = None
        # (kind, name, byte span | None, steps: x2many/groupby fields from arch root or spec to element)
        self.items: list[tuple] = []


class _Frame:
    __slots__ = ("view", "record", "record_model", "arch", "arch_field", "attributes", "steps", "field", "groupby", "capture", "text_start", "opaque")

    def __init__(self) -> None:
        self.view: _View | None = None
        self.record = False
        self.record_model: str | None = None
        self.arch = False
        self.arch_field = False
        self.attributes = False
        self.steps: tuple = ()
        self.field: str | None = None
        self.groupby: str | None = None
        self.capture: str | None = None
        self.text_start = -1
        self.opaque = False


# expat pass for what facts lack: arch element model context, attribute spans, record field texts
class _XmlScan:
    def __init__(self, src: bytes):
        self.src = src
        self.views: list[_View] = []
        self.texts: list[tuple[str | None, str, int, int]] = []
        self._stack: list[_Frame] = []
        self._rx: dict[tuple[str, bool], re.Pattern] = {}
        self._p = parser = expat.ParserCreate()
        parser.buffer_text = True
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        try:
            parser.Parse(src, True)
        except expat.ExpatError:
            pass  # reported from the facts

    def attr_span(self, ts: int, te: int, name: str, value: bool = True) -> tuple[int, int] | None:
        rx = self._rx.get((name, value))
        if rx is None:
            rx = self._rx[(name, value)] = re.compile(rb"""(?:^|[\s<])(""" + re.escape(name.encode()) + rb""")\s*=\s*(["'])(.*?)\2""", re.S)
        m = rx.search(self.src, ts, te)
        if m is None:
            return None
        return (m.start(3), m.end(3)) if value else (m.start(1), m.end(1))

    def _start(self, tag: str, attrs: dict) -> None:
        ts = self._p.CurrentByteIndex
        m = _TAG_RE.match(self.src, ts)
        te = m.end() if m else ts + 1
        parent = self._stack[-1] if self._stack else None
        f = _Frame()
        if parent is not None:
            if parent.arch:
                self._arch(f, parent, tag, attrs, ts, te)
            elif parent.opaque or tag == "template":
                f.opaque = True
            elif tag == "record":
                self._record(f, attrs)
            elif tag == "field" and parent.record:
                self._record_field(f, parent, attrs, te)
        self._stack.append(f)

    def _end(self, tag: str) -> None:
        f = self._stack.pop()
        if not f.capture:
            return
        end = self._p.CurrentByteIndex
        raw = self.src[f.text_start : end] if end > f.text_start else b""
        if b"<" in raw:
            return
        if f.view is not None:
            value = raw.decode("utf-8", "replace").strip()
            if f.capture == "model":
                f.view.model = value
            elif f.capture == "type":
                f.view.vtype = value
        self.texts.append((f.record_model, f.capture, f.text_start, max(end, f.text_start)))

    def _record(self, f: _Frame, attrs: dict) -> None:
        f.record = True
        f.record_model = attrs.get("model")
        if f.record_model == "ir.ui.view":
            f.view = _View(attrs.get("id"))
            self.views.append(f.view)

    def _record_field(self, f: _Frame, parent: _Frame, attrs: dict, te: int) -> None:
        name = attrs.get("name", "")
        view = parent.view
        f.record_model = parent.record_model
        if view is not None and name in ("arch", "arch_db", "arch_base"):
            f.arch = f.arch_field = True
            f.view = view
        elif attrs.get("type") in ("xml", "html"):
            f.opaque = True
        else:
            if view is not None and name == "inherit_id" and attrs.get("ref"):
                view.inherit = attrs["ref"].strip()
            if name in ("model", "type", "view_mode", "context") and "ref" not in attrs and "eval" not in attrs:
                f.capture = name
                f.text_start = te
                f.view = view

    def _arch(self, f: _Frame, parent: _Frame, tag: str, attrs: dict, ts: int, te: int) -> None:
        view = parent.view
        assert view is not None
        f.arch = True
        f.view = view
        if parent.attributes:
            f.attributes = True
            if tag == "attribute" and attrs.get("name") in ("attrs", "states"):
                view.items.append(("attrs", attrs["name"], self.attr_span(ts, te, "name"), ()))
            return
        f.attributes = attrs.get("position") == "attributes"
        if parent.arch_field:
            view.root = view.root or tag
            steps: tuple = ()
        elif parent.groupby:
            steps = parent.steps + (parent.groupby,)
        elif parent.field and tag in SUBVIEW_TAGS:
            steps = parent.steps + (parent.field,)
        else:
            steps = parent.steps
        f.steps = steps
        items = view.items
        name = attrs.get("name")
        # a locator's children go next to the located node, not inside it
        inside = attrs.get("position", "inside") == "inside"
        if tag == "field" and name:
            f.field = name if inside else None
            items.append(("field", name, self.attr_span(ts, te, "name"), steps))
        elif tag == "groupby" and name:
            f.groupby = name if inside else None
            items.append(("field", name, self.attr_span(ts, te, "name"), steps))
        elif tag == "button" and name and attrs.get("type") == "object" and not attrs.get("special"):
            items.append(("button", name, self.attr_span(ts, te, "name"), steps))
        elif tag == "xpath" and attrs.get("expr"):
            self._xpath(items, attrs["expr"], ts, te)
        elif tag == "tree":
            items.append(("tree", tag, (ts + 1, ts + 5), steps))
        elif tag == "list":
            items.append(("list", tag, (ts + 1, ts + 5), steps))
        for attr in ("attrs", "states"):
            if attr in attrs:
                items.append(("attrs", attr, self.attr_span(ts, te, attr, value=False), steps))
        mode = attrs.get("mode")
        if tag == "field" and mode and "tree" in mode.replace(" ", "").split(","):
            items.append(("tree-mode", mode, self.attr_span(ts, te, "mode"), steps))
        context = attrs.get("context")
        if context and "tree_view_ref" in context:
            span = self.attr_span(ts, te, "context")
            if span is not None:
                i = self.src.find(b"tree_view_ref", span[0], span[1])
                items.append(("tree-ref", "tree_view_ref", (i, i + 13) if i >= 0 else span, steps))

    def _xpath(self, items: list, expr: str, ts: int, te: int) -> None:
        span = self.attr_span(ts, te, "expr")
        exact = span is not None and self.src[span[0] : span[1]].decode("utf-8", "replace") == expr
        for m in _XPATH_FIELD_RE.finditer(expr):
            if exact:
                start = span[0] + len(expr[: m.start(2)].encode())  # type: ignore[index]
                items.append(("field", m.group(2), (start, start + len(m.group(2))), ()))
            else:
                items.append(("field", m.group(2), span, ()))
        if _XPATH_TREE_RE.search(expr):
            items.append(("xpath-tree", expr, span, ()))
        if _XPATH_LIST_RE.search(expr):
            items.append(("xpath-list", expr, span, ()))


_XML_MODEL_CONTEXT = {"record": "<record model>", "field_model": "model name field", "function": "<function model>", "delete": "<delete model>"}


class _XmlChecker:
    def __init__(self, ctx: Any, res: _Resolver, facts: dict, report: _Report):
        self.ctx = ctx
        self.res = res
        self.reg = res.reg
        self.facts = facts
        self.report = report
        dirs = (res.module_rel(ctx.path) or "").split("/")[:-1]
        self.skip = bool(UNLOADED_DIRS.intersection(dirs))
        if not res.loaded_by_manifest(ctx.path):
            res.ignore_dependencies()
        self._index: SrcIndex | None = None
        self._lines: list[str] | None = None

    @property
    def index(self) -> SrcIndex:
        if self._index is None:
            self._index = SrcIndex(self.ctx.source)
        return self._index

    def _pos(self, span: tuple[int, int]) -> tuple[int, int, int]:
        start, end = span
        nl = self.ctx.source.find(b"\n", start, end)
        return self.index.span(start, end if nl < 0 else nl)

    def run(self) -> None:
        if self.skip or self.facts.get(ROOT) == "templates":
            return
        self._refs()
        src = self.ctx.source
        if b"ir.ui.view" in src or (self.res.since((18, 0)) and (b"view_mode" in src or b"tree_view_ref" in src)):
            scan = _XmlScan(src)
            for view in scan.views:
                self._view(view)
            if self.res.since((18, 0)):
                self._texts(scan)

    def _refs(self) -> None:
        records = self.facts.get(RECORDS) or ()
        for ref in self.facts.get(REFS) or ():
            kind = ref[R_KIND]
            role = ref[R_CTX][0]
            pos = (ref[R_LINE], ref[R_COL], ref[R_END_COL])
            if kind == REF_MODEL and role in _XML_MODEL_CONTEXT:
                _report_model(self.report, self.res, ref[R_VALUE], pos, ERROR_, _XML_MODEL_CONTEXT[role])
            elif kind == REF_XMLID and role in XML_ROLE_LABELS:
                self._xmlid(ref, role, pos)
            elif kind == REF_FIELD and role == "record":
                self._record_field(ref, records, pos)
            elif kind == REF_METHOD and role == "function":
                self._function(ref, pos)

    def _xmlid(self, ref: tuple, role: str, pos: tuple[int, int, int]) -> None:
        value = ref[R_VALUE]
        if not value or "{" in value or " " in value or (role in ("tcall", "view_ref") and "." not in value):
            return
        line = self._line(ref[R_LINE])
        if role == "eval" and _NOT_FOUND_FALSE_RE.search(line[utf16_to_index(line, ref[R_END_COL]) :].split(")", 1)[0]):
            return
        xmlid = self.reg.qualify_xmlid(value, self.res.module)
        runtime = self._runtime_ref(role, line, ref[R_COL])
        if role == "tcall" and self.res.xmlid_status(xmlid)[0] == "unknown" and self.res.template_key_exists(xmlid):
            return
        # missing *_view_ref targets and QWeb groups are ignored at runtime
        soft = role in ("view_ref", "tcall") or (role == "groups" and self._in_qweb(ref[R_LINE]))
        severity = ERROR_ if role in XML_STRICT_ROLES and not runtime else WARNING
        _report_xmlid(self.report, self.res, xmlid, pos, severity, XML_ROLE_LABELS[role], runtime, dependency=not soft)

    def _in_qweb(self, line: int) -> bool:
        for rec in self.facts.get(RECORDS) or ():
            if rec[X_LINE] <= line <= rec[X_END_LINE]:
                return rec[X_TAG] == "template" or (rec[X_INFO] or {}).get("view_type") == "qweb"
        return False

    # resolved when the view is used (t-call, *_view_ref, arch on_create...), not at data load
    @staticmethod
    def _runtime_ref(role: str, line: str, col: int) -> bool:
        if role in ("tcall", "view_ref"):
            return True
        if role not in ("ref", "action"):
            return False
        m = re.search(r"""([\w:.-]+)\s*=\s*["']\s*$""", line[: utf16_to_index(line, col)])
        attr = m.group(1) if m else None
        return attr not in ("ref", "view_id", "action", None)

    def _line(self, n: int) -> str:
        if self._lines is None:
            self._lines = self.ctx.text.split("\n")
        return self._lines[n] if 0 <= n < len(self._lines) else ""

    def _record_field(self, ref: tuple, records: tuple | list, pos: tuple[int, int, int]) -> None:
        idx = ref[R_CTX][1]
        if not 0 <= idx < len(records):
            return
        rec = records[idx]
        model = rec[X_MODEL]
        name = ref[R_VALUE].split("@", 1)[0]  # 19.0+ name="field@lang" translations
        if not model or not _IDENT_RE.match(name):
            return
        res = self.res
        if res.model_status(model)[0] != "ok":
            return
        status, modules, _fdef = res.field_status(model, name)
        if status == "ok" or not res.model_closed(model):
            return
        where = f"record '{rec[X_ID]}'" if rec[X_ID] else f"<record model=\"{model}\">"
        if status == "outside":
            _report_missing_dep(self.report, res, f"Field '{model}.{name}' ({where})", modules, pos)
            return
        if name.startswith("x_") or res.mentioned_in_code(model, name):
            return
        new = FIELD_RENAMES_19.get(name)
        if new and res.since((19, 0)) and new in res.members(model, K_FIELDS, res.scope):
            msg = f"Field '{name}' of model '{model}' was renamed '{new}' in Odoo 19.0 ({where})"
        else:
            msg = f"Field '{name}' does not exist on model '{model}' ({where}){res.suggest_field(model, name)}"
        sev, msg = res.adjust(ERROR_, msg)
        self.report.add(sev, "unknown-field", *pos, msg)

    def _function(self, ref: tuple, pos: tuple[int, int, int]) -> None:
        model = ref[R_CTX][1]
        name = ref[R_VALUE]
        res = self.res
        if not model or not _IDENT_RE.match(name) or res.model_status(model)[0] != "ok":
            return
        status, modules = res.method_status(model, name)
        if status != "ok" and not res.model_closed(model):
            return
        if status == "outside":
            _report_missing_dep(self.report, res, f"Method '{model}.{name}' (<function>)", modules, pos)
        elif status == "unknown":
            sev, msg = res.adjust(WARNING, f"Method '{name}' called by <function> is not defined on model '{model}'{res.suggest_method(model, name)}")
            self.report.add(sev, "unknown-method", *pos, msg)

    def _view(self, view: _View) -> None:
        if view.vtype == "qweb":
            return
        if view.inherit:
            spec = self._spec_context(view)
            if spec is None:
                return
            self._view_deprecations(view)
            self._spec_items(view, *spec)
        elif view.root in VIEW_TYPES:
            self._view_deprecations(view)
            if view.model and self.res.model_status(view.model)[0] == "ok":
                self._primary_items(view, view.model)

    def _spec_context(self, view: _View) -> tuple[str, set[str]] | None:
        reg = self.reg
        cur: str | None = reg.qualify_xmlid(view.inherit, self.res.module)
        chain = []
        while cur and len(chain) < 40:
            recs = reg.records(cur)
            if not recs:
                return None
            rec = next((r for r in recs if r.info.get("inherit_id")), recs[0])
            if rec.tag != "record" or rec.model != "ir.ui.view" or rec.info.get("view_type") == "qweb" or rec in chain:
                return None
            chain.append(rec)
            parent = rec.info.get("inherit_id")
            cur = reg.qualify_xmlid(parent, rec.module) if parent else None
        if not chain or not reg.view_model(chain[-1]):
            return None
        own = view.model or reg.view_model(chain[0])
        if not own or self.res.model_status(own)[0] != "ok":
            return None
        models = self._tree_models(chain[-1])
        models.add(own)
        return own, models

    # a spec can locate any element of the inherited view tree: models of all of them + comodels
    def _tree_models(self, root: Any) -> set[str]:
        key = ("view_tree_models", root.xmlid)
        hit = self.res.cache.get(key)
        if hit is not None:
            return set(hit)
        reg = self.reg
        res = self.res
        views = []
        seen: set[tuple[str, int]] = set()
        queue = [root]
        while queue and len(views) < 500:
            r = queue.pop()
            if (r.path, r.index) in seen:
                continue
            seen.add((r.path, r.index))
            views.append(r)
            queue.extend(reg.children_views(r.xmlid))
        by_path: dict[str, dict[int, str]] = {}
        models: set[str] = set()
        for r in views:
            vm = reg.view_model(r)
            if vm:
                models.add(vm)
                by_path.setdefault(r.path, {})[r.index] = vm
        for path, index_model in by_path.items():
            for ref in (reg.file_facts(path) or {}).get(REFS) or ():
                rctx = ref[R_CTX]
                if ref[R_KIND] == REF_FIELD and rctx[0] == "arch" and rctx[1] in index_model:
                    model = res.walk_steps(index_model[rctx[1]], rctx[2])
                    if model:
                        models.add(model)
                        comodel = res.comodel(model, ref[R_VALUE])
                        if comodel:
                            models.add(comodel)
        self.res.cache[key] = frozenset(models)
        return set(models)

    def _spec_items(self, view: _View, own: str, candidates: set[str]) -> None:
        res = self.res
        reg = self.reg
        for kind, name, span, steps in view.items:
            if kind not in ("field", "button") or span is None or not _IDENT_RE.match(name) or name.startswith("__"):
                continue
            models = candidates if not steps else {m for m in (res.walk_steps(c, steps) for c in candidates) if m}
            if not models:
                continue
            if not (kind == "button" and name.startswith("_")) and res.member_of_any(models, name, K_FIELDS if kind == "field" else K_METHODS):
                continue
            if not all(res.model_closed(m) for m in models if reg.has_model(m, res.scope)):
                continue
            pos = self._pos(span)
            if kind == "field":
                if name.startswith("x_"):
                    continue
                msg = f"Field '{name}' does not exist on model '{own}' nor in the views it inherits ('{view.inherit}'){res.suggest_field(own, name)}"
                sev, msg = res.adjust(ERROR_, msg)
                self.report.add(sev, "unknown-field", *pos, msg)
            elif name.startswith("_"):
                self.report.add(WARNING, "private-method", *pos, f"Button method '{name}' is private: Odoo refuses to call '_'-prefixed methods from buttons")
            else:
                msg = f"Button method '{name}' is not defined on model '{own}' nor on the models of the views it inherits{res.suggest_method(own, name)}"
                sev, msg = res.adjust(WARNING, msg)
                self.report.add(sev, "unknown-method", *pos, msg)

    def _primary_items(self, view: _View, model: str) -> None:
        res = self.res
        where = f"view '{view.xmlid}'" if view.xmlid else "view"
        for kind, name, span, steps in view.items:
            if kind not in ("field", "button") or span is None or not _IDENT_RE.match(name) or name.startswith("__"):
                continue
            target = res.walk_steps(model, steps) if steps else model
            if target is None:
                continue
            pos = self._pos(span)
            if kind == "field":
                status, modules, _fdef = res.field_status(target, name)
                if status != "ok" and not res.model_closed(target):
                    continue
                if status == "outside":
                    _report_missing_dep(self.report, res, f"Field '{target}.{name}' ({where})", modules, pos)
                elif status == "unknown" and not name.startswith("x_") and not res.mentioned_in_code(target, name):
                    sev, msg = res.adjust(ERROR_, f"Field '{name}' does not exist on model '{target}' ({where}){res.suggest_field(target, name)}")
                    self.report.add(sev, "unknown-field", *pos, msg)
            elif name.startswith("_"):
                self.report.add(WARNING, "private-method", *pos, f"Button method '{name}' is private: Odoo refuses to call '_'-prefixed methods from buttons")
            else:
                status, modules = res.method_status(target, name)
                if status != "ok" and not res.model_closed(target):
                    continue
                if status == "outside":
                    _report_missing_dep(self.report, res, f"Method '{target}.{name}' (button in {where})", modules, pos)
                elif status == "unknown":
                    sev, msg = res.adjust(WARNING, f"Button method '{name}' is not defined on model '{target}' ({where}){res.suggest_method(target, name)}")
                    self.report.add(sev, "unknown-method", *pos, msg)

    def _view_deprecations(self, view: _View) -> None:
        res = self.res
        for kind, name, span, _steps in view.items:
            if span is None:
                continue
            if kind == "attrs" and res.since((17, 0)):
                if name == "attrs":
                    msg = "Since Odoo 17.0 the 'attrs' attribute is no longer supported: use invisible/readonly/required/column_invisible Python expressions"
                else:
                    msg = "Since Odoo 17.0 the 'states' attribute is no longer supported: use e.g. invisible=\"state not in ('draft', 'sent')\""
                self.report.add(ERROR_, "deprecated-attrs", *self._pos(span), msg)
            elif kind in ("list", "xpath-list") and not res.since((18, 0)):
                msg = (
                    f"<list> views only exist since Odoo 18.0: use <tree> in Odoo {res.label}"
                    if kind == "list"
                    else f"<list> views only exist since Odoo 18.0: this xpath cannot match in Odoo {res.label} (use tree)"
                )
                self.report.add(ERROR_, "unknown-view-type", *self._pos(span), msg)
            elif kind in ("tree", "tree-mode", "tree-ref", "xpath-tree") and res.since((18, 0)):
                msg = {
                    "tree": "Since Odoo 18.0 <tree> views are <list>: rename the element",
                    "tree-mode": "Since Odoo 18.0 the 'tree' view mode is 'list' (x2many mode attribute)",
                    "tree-ref": "Since Odoo 18.0 'tree_view_ref' is 'list_view_ref'",
                    "xpath-tree": "Since Odoo 18.0 <tree> views are <list>: this xpath cannot match",
                }[kind]
                self.report.add(ERROR_, "deprecated-tree", *self._pos(span), msg)

    def _texts(self, scan: _XmlScan) -> None:
        src = self.ctx.source
        for model, name, start, end in scan.texts:
            raw = src[start:end].decode("utf-8", "replace")
            if name == "view_mode":
                rx, msg = _WORD_TREE_RE, "Since Odoo 18.0 the 'tree' view mode is 'list' (view_mode)"
            elif name == "type" and model == "ir.ui.view" and raw.strip() == "tree":
                rx, msg = _WORD_TREE_RE, "Since Odoo 18.0 the 'tree' view type is 'list'"
            elif name == "context":
                rx, msg = _TREE_VIEW_REF_RE, "Since Odoo 18.0 'tree_view_ref' is 'list_view_ref'"
            else:
                continue
            for m in rx.finditer(raw):
                b0 = start + len(raw[: m.start()].encode())
                self.report.add(ERROR_, "deprecated-tree", *self._pos((b0, b0 + len(m.group()))), msg)


class _CsvChecker:
    def __init__(self, ctx: Any, res: _Resolver, facts: dict, report: _Report):
        self.ctx = ctx
        self.res = res
        self.facts = facts
        self.report = report

    def run(self) -> None:
        if not self.res.loaded_by_manifest(self.ctx.path):
            return  # the loader derives the model from the name of listed files only
        res = self.res
        model = self.facts.get(CSV_MODEL) or ""
        header = self.ctx.text.split("\n", 1)[0]
        status = res.model_status(model)[0]
        if status != "ok":
            context = f"model of {os.path.basename(self.ctx.path)}"
            _report_model(self.report, res, model, (0, 0, len(header.encode("utf-16-le")) // 2), ERROR_, context)
            return
        for ref in self.facts.get(REFS) or ():
            kind = ref[R_KIND]
            role = ref[R_CTX][0]
            pos = (ref[R_LINE], ref[R_COL], ref[R_END_COL])
            if kind == REF_FIELD and role == "csv_header":
                self._header(ref[R_VALUE], model, pos)
            elif kind == REF_XMLID:
                xmlid = res.reg.qualify_xmlid(ref[R_VALUE], res.module)
                _report_xmlid(self.report, res, xmlid, pos, ERROR_, f"column {role}")
            elif kind == REF_MODEL and role == "csv":
                _report_model(self.report, res, ref[R_VALUE], pos, ERROR_, "model_id column")

    def _header(self, name: str, model: str, pos: tuple[int, int, int]) -> None:
        res = self.res
        status, modules, _fdef = res.field_status(model, name)
        if status != "ok" and not res.model_closed(model):
            return
        if status == "outside":
            _report_missing_dep(self.report, res, f"Field '{model}.{name}' (CSV column)", modules, pos)
        elif status == "unknown" and not name.startswith("x_") and not res.mentioned_in_code(model, name):
            sev, msg = res.adjust(ERROR_, f"Column '{name}' is not a field of model '{model}'{res.suggest_field(model, name)}")
            self.report.add(sev, "unknown-field", *pos, msg)


def _check_manifest(ctx: Any, res: _Resolver, facts: dict, report: _Report) -> None:
    reg = res.reg
    if res.has_base:
        names: list[str] | None = None
        for name, line, col, end in facts.get(MANIFEST_DEPENDS) or ():
            if reg.module(name) is None:
                names = names if names is not None else [m.name for m in reg.modules()]
                msg = f"Module '{name}' (in depends) is not in the addons path of this Odoo {res.label} env{_suggest(name, names)}"
                report.add(ERROR_, "unknown-module", line, col, end, msg)
    base = os.path.dirname(ctx.path)
    for key, entries in (("data", facts.get(MANIFEST_DATA)), ("demo", facts.get(MANIFEST_DEMO))):
        for rel, line, col, end in entries or ():
            if not rel or any(ch in rel for ch in "*?[") or os.path.isfile(os.path.join(base, os.path.normpath(rel))):
                continue
            msg = f"File '{rel}' listed in '{key}' does not exist in module '{res.module}'{_suggest_file(base, rel)}"
            report.add(ERROR_, "missing-file", line, col, end, msg)


def _suggest_file(base: str, rel: str) -> str:
    folder = os.path.dirname(rel)
    try:
        names = os.listdir(os.path.join(base, folder))
    except OSError:
        return ""
    match = difflib.get_close_matches(os.path.basename(rel), names, n=1, cutoff=0.75)
    return f"; did you mean '{folder + '/' if folder else ''}{match[0]}'?" if match else ""
