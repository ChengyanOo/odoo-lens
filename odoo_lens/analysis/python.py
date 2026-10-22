"""Odoo-aware Python analysis: the targets under the cursor, the usages of a
member in a file, the method calls of a function.

One file is analysed on demand from its current source (``ast``) on top of the
env's :class:`~odoo_lens.registry.Registry`. Parsed files and their analyses live
in a small LRU keyed by ``(path, len(source), hash(source))``.

Receiver inference is local to a function and essentially flow-insensitive: the
type of a name is the union of the types of its bindings (assignments, loop and
comprehension targets, parameters), narrowed to the bindings that can reach the
use (straight-line order, loop-carried ones included; a dominating last binding
wins). Expressions are typed lazily, memoized and depth-limited - no fixpoints.
Method calls are typed from ``-> ...`` annotations or the ``return`` statements
of the called definition (parsing just that method). Type values are tuples:

* ``("rs", models)``: recordset of a model (several models = candidates)
* ``("env",)``: an ``odoo.api.Environment``; ``("req",)``: ``odoo.http.request``
* ``("super", class_info)``: ``super()`` inside a class
* ``("vals", model)``: a dict of field values of ``model`` (create/write...)
* ``("list", elem)``, ``("tuple", items)``, ``("dict", key, value)``: containers
* ``("mod", dotted)``, ``("cls" | "inst" | "func", path, name)``: python objects
* ``("val", what)``: a scalar; ``("obj",)``: any other non-record object
* ``None``: unknown (could be a recordset of any model)
"""

from __future__ import annotations

import ast
import builtins
import os
import re
import weakref
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from typing import Any, Iterator

from ..facts import (
    A_COL,
    A_END_COL,
    A_LINE,
    A_NAME,
    CLASSES,
    F_ARGS,
    F_COL,
    F_COMODEL,
    F_END_COL,
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
    K_FIELDS,
    K_LINE,
    K_METHODS,
    K_MODEL,
    K_NAME,
    M_COL,
    M_END_COL,
    M_END_LINE,
    M_LINE,
    M_NAME,
    M_RETURNS,
    MI_INHERIT,
    MI_INHERIT_STR,
    MI_INHERITS,
    MI_NAME,
    R_COL,
    R_CTX,
    R_END_COL,
    R_KIND,
    R_LINE,
    R_VALUE,
    REF_FIELD,
    REF_METHOD,
    REF_MODEL,
    REF_MODULE,
    REF_QWEB,
    REF_XMLID,
    REFS,
    S_COL,
    S_END_COL,
    S_KIND,
    S_LINE,
    S_NAME,
    SYMBOLS,
)
from ..documents import bytecol_to_utf16
from ..extract.pyfacts import FRAMEWORK_CLASSES, MODEL_BASES, parse_tolerant
from ..registry import MAGIC_FIELDS, model_class_info
from .targets import ATTR, FIELD, FILE, METHOD, MODEL, MODULE, PYSYM, TEMPLATE, XMLID, Target

# ---------------------------------------------------------------------------
# Odoo knowledge

# methods returning a recordset of the receiver's model (``-> Self`` in 18+)
PRESERVING = frozenset({
    "browse", "search", "create", "copy", "sudo", "with_context", "with_user", "with_company",
    "with_env", "with_prefetch", "exists", "filtered", "filtered_domain", "sorted", "ensure_one",
    "new", "union", "concat", "search_fetch", "try_lock_for_update", "_filtered_access",
    "_fetch_query", "_browse",
})
# lambdas passed to these recordset methods receive a record of the receiver
LAMBDA_METHODS = frozenset({"filtered", "sorted", "mapped", "grouped"})
# recordset methods whose first argument is a field path string
PATH_METHODS = frozenset({"mapped", "filtered", "grouped", "sorted"})
# domain consumers: method -> positional index of the domain argument
DOMAIN_ARGS = {
    "search": 0, "search_count": 0, "search_read": 0, "search_fetch": 0, "_search": 0,
    "filtered_domain": 0, "read_group": 0, "_read_group": 0, "formatted_read_group": 0,
    "web_read_group": 0, "web_search_read": 0, "_where_calc": 0, "search_any": 0,
    "_read_group_select": 0, "name_search": 1, "_name_search": 1, "web_name_search": 1,
    "read_progress_bar": 0, "_read_progress_bar": 0, "search_panel_select_range": -1,
    "search_panel_select_multi_range": -1,
}
DOMAIN_KWARGS = frozenset({"domain", "args"})
# operators of a domain leaf ``(path, operator, value)``, all versions
LEAF_OPERATORS = frozenset({
    "=", "!=", "<>", "==", "<", ">", "<=", ">=", "=?", "like", "ilike", "=like", "=ilike",
    "not like", "not ilike", "not =like", "not =ilike", "in", "not in", "child_of", "parent_of",
    "any", "not any", "any!", "not any!",
})
# field-name arguments (strings or lists of strings): method -> (positions, keywords)
FIELD_ARGS = {
    "read": ((0,), ("fields",)),
    "search_read": ((1,), ("fields",)),
    "search_fetch": ((1,), ("field_names",)),
    "fetch": ((0,), ("field_names",)),
    "flush_model": ((0,), ("fnames",)),
    "flush_recordset": ((0,), ("fnames",)),
    "invalidate_model": ((0,), ("fnames",)),
    "invalidate_recordset": ((0,), ("fnames",)),
    "modified": ((0,), ("fnames",)),
    "fields_get": ((0,), ("allfields",)),
    "default_get": ((0,), ("fields_list", "fields")),
    "read_group": ((1, 2), ("fields", "groupby", "aggregates")),
    "_read_group": ((1, 2), ("fields", "groupby", "aggregates")),
    "formatted_read_group": ((1, 2), ("groupby", "aggregates")),
    "web_read_group": ((1, 2), ("fields", "groupby", "aggregates")),
}
ORDER_KWARGS = frozenset({"order", "orderby"})
# methods taking a dict of field values: method -> (position, keywords)
VALS_ARGS = {
    "create": (0, ("vals_list", "vals")),
    "write": (0, ("vals",)),
    "update": (0, ("values",)),
    "new": (0, ("values",)),
    "copy": (0, ("default",)),
    "copy_data": (0, ("default",)),
    "_add_missing_default_values": (0, ("values",)),
}
# Command.create(vals) / Command.update(id, vals): position of the values dict
COMMAND_VALS = {"create": 0, "update": 1}
# containers / combinators a domain flows through unchanged
DOMAIN_WRAPPERS = frozenset({
    "AND", "OR", "normalize_domain", "Domain", "distribute_not", "list", "optimize",
    "optimize_full", "_optimize", "TRUE", "FALSE", "domain_combine_anies",
})
# container mutations giving the element type of an initially empty list / set
FILL_METHODS = frozenset({"append", "add", "extend", "insert"})
# parameters holding field values in ORM method overrides: method -> parameter index
VALS_PARAMS = {"create": 1, "write": 1, "copy": 1, "copy_data": 1, "new": 1, "update": 1, "_add_missing_default_values": 1}
ENV_MODELS = {"user": "res.users", "company": "res.company", "companies": "res.company", "website": "website"}
RELATIONAL = frozenset({"Many2one", "One2many", "Many2many"})
FIELDS_MODULES = ("fields", "odoo.fields", "openerp.fields")  # receivers of fields.X(...) declarations
# recordset attributes that exist on every model without being declared in facts
RS_SLOTS = frozenset({"env", "_ids", "_prefetch_ids", "_fields", "pool"})
FRAMEWORK_FILES = ("odoo/models.py", "odoo/orm/models.py", "odoo/orm/models_transient.py", "odoo/orm/models_cached.py")
# odoo 19+: names assigned on the ``odoo`` namespace package by odoo/init.py
ODOO_INIT_NAMES = {
    "_": ("odoo.tools.translate", "_"),
    "_lt": ("odoo.tools.translate", "_lt"),
    "Command": ("odoo.orm.commands", "Command"),
    "SUPERUSER_ID": ("odoo.orm.utils", "SUPERUSER_ID"),
}
BUILTINS = frozenset(dir(builtins))
# attribute names of builtin containers, strings, dates, files, loggers, cursors...:
# an unknown receiver accessing them is most likely not a recordset
NON_MODEL_ATTRS = frozenset(
    {n for t in (dict, list, str, set, bytes, int, float, tuple, frozenset) for n in dir(t) if not n.startswith("_") and n != "title"}
    | {
        "date", "time", "strftime", "isoformat", "year", "month", "day", "hour", "minute", "second",
        "weekday", "isoweekday", "total_seconds", "days", "seconds", "microseconds", "tzinfo", "timestamp",
        "read", "readline", "readlines", "close", "seek", "tell", "flush", "info", "warning", "error",
        "debug", "exception", "critical", "execute", "executemany", "fetchall", "fetchone", "dictfetchall",
        "dictfetchone", "commit", "rollback", "savepoint", "mogrify", "rowcount", "group", "groups",
        "groupdict", "span", "start", "end", "match", "fullmatch", "sub", "text", "tag", "attrib",
        "find", "findall", "iterfind", "xpath", "getparent", "getchildren", "getroottree", "addnext",
        "addprevious", "iter", "itertext", "tail", "json", "status_code", "content", "headers",
        "raise_for_status", "cookies", "url", "args", "form", "files", "values", "environ", "method",
    }
)
MAX_NAME_HOPS = 8
MAX_DEPTH = 60
MAX_STEPS = 50000  # inference steps per public call (safety net for pathological code)
MAX_DECLARING = 40  # unknown receivers: members declared by more models are not guessed
LENIENCY = 25  # columns searched around an imprecise cursor
PARTIAL_MIN_LINES = 2500  # bigger files are analysed one method at a time when possible...
PARTIAL_MAX_USES = 4  # ... for their first requests, then parsed entirely
MAX_CANDIDATES = 8  # ambiguous receivers: at most this many candidate models

_FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMP = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_SCOPE_KINDS = {
    ast.FunctionDef: "function", ast.AsyncFunctionDef: "function", ast.Lambda: "lambda", ast.ClassDef: "class",
    ast.ListComp: "comp", ast.SetComp: "comp", ast.DictComp: "comp", ast.GeneratorExp: "comp",
}
_TRY = tuple(t for t in (ast.Try, getattr(ast, "TryStar", None)) if t is not None)
_MATCH_NAMES = tuple(t for t in (getattr(ast, "MatchAs", None), getattr(ast, "MatchStar", None)) if t is not None)
_MODEL_RX = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+")
_CAMEL = re.compile(r"(?<=[^_])([A-Z])")
_FROM_RX = re.compile(rb"^\s*from\s+(\.*)\s*([\w.]*)\s+import\b")

ENV = ("env",)
REQ = ("req",)
OBJ = ("obj",)
VAL = ("val", None)
INDEXED = ("<indexed>",)  # domain of a field definition: covered by the facts refs


# ---------------------------------------------------------------------------
# type helpers


def _rs(models: Any) -> tuple | None:
    models = tuple(dict.fromkeys(m for m in models if m))
    return ("rs", models) if models else None


def _union(types: Any) -> tuple | None:
    """Flow-insensitive merge: recordsets win over anything else; unknowns are ignored."""
    models: list[str] = []
    other = None
    for t in types:
        if t is None:
            continue
        if t[0] == "rs":
            models.extend(t[1])
        elif other is None:
            other = t
        elif other != t:
            if other[0] == t[0] == "list":
                other = ("list", _union((other[1], t[1])))
            elif other[0] == t[0] == "dict":
                other = ("dict", _union((other[1], t[1])), _union((other[2], t[2])), other[3] or t[3])
            else:
                other = OBJ
    if models:
        return ("rs", tuple(dict.fromkeys(models)))
    return other


def _homogeneous(types: Any, strict: bool = False) -> tuple | None:
    """Element type of a literal container: recordsets mixed with other known
    values (``{'move': self, 'lines': []}``) give no element type; ``strict``:
    neither do unknown values."""
    types = list(types)
    if strict and any(t is None for t in types):
        return None
    types = [t for t in types if t is not None]
    if any(t[0] == "rs" for t in types) and any(t[0] != "rs" for t in types):
        return None
    return _union(types)


def _subst(t: tuple | None, src: str, dst: str) -> tuple | None:
    """Replace recordsets of ``src`` by recordsets of ``dst`` (the type of values
    computed by a method of model ``src`` called on a model ``dst`` inheriting it)."""
    if t is None:
        return None
    k = t[0]
    if k == "rs":
        return ("rs", tuple(dict.fromkeys(dst if m == src else m for m in t[1])))
    if k == "list":
        return ("list", _subst(t[1], src, dst))
    if k == "tuple":
        return ("tuple", tuple(_subst(x, src, dst) for x in t[1]))
    if k == "dict":
        e = t[3]
        if e is not None:
            e = (e[0], e[1], e[2], e[3] + ((src, dst),))
        return ("dict", _subst(t[1], src, dst), _subst(t[2], src, dst), e)
    if k == "vals" and t[1] == src:
        return ("vals", dst)
    return t


def _entry(t: tuple, key: str) -> tuple | None:
    """Type of ``d[key]`` for a dict type built from a literal."""
    an, eu, entries, substs = t[3]
    if key not in entries:
        return None
    v = an.infer(entries[key], eu)
    for src, dst in substs:
        v = _subst(v, src, dst)
    return v


def _elem(t: tuple | None) -> tuple | None:
    """Type of the items obtained by iterating over a value of type ``t``."""
    if t is None:
        return None
    k = t[0]
    if k == "rs" or k == "vals":
        return t
    if k == "list":
        return t[1]
    if k == "dict":
        return t[1]
    if k == "tuple":
        return _union(t[1])
    return None


def _item(t: tuple | None, i: int) -> tuple | None:
    """Type of the i-th item when unpacking a value of type ``t``."""
    if t is None:
        return None
    k = t[0]
    if k == "tuple":
        return t[1][i] if -len(t[1]) <= i < len(t[1]) else None
    if k == "rs":
        return t
    if k == "list":
        return t[1]
    return None


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _start(node: ast.AST) -> int:
    """First line (1-based) of a statement, decorators included."""
    decs = getattr(node, "decorator_list", None)
    if decs:
        return min(node.lineno, decs[0].lineno)  # type: ignore[attr-defined]
    return node.lineno  # type: ignore[attr-defined]


def _str_const(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_super_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super"


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _target_names(target: ast.AST) -> Iterator[ast.Name]:
    if isinstance(target, ast.Name):
        yield target
    elif isinstance(target, (ast.Tuple, ast.List)):
        for e in target.elts:
            yield from _target_names(e)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _bound_name(alias: ast.alias, stmt: ast.AST) -> str:
    if alias.asname:
        return alias.asname
    if isinstance(stmt, ast.Import):
        return alias.name.split(".")[0]
    return alias.name


def _parse(source: bytes) -> ast.Module | None:
    """AST of ``source``, of its repaired text on a syntax error (see :func:`parse_tolerant`)."""
    return parse_tolerant(source)[0]


# ---------------------------------------------------------------------------
# per-file structures


class _ClassInfo:
    """A class of the analysed file (top-level ones in facts CLASSES order)."""

    __slots__ = ("node", "index", "name", "model", "is_model", "facts")

    def __init__(self, node: ast.ClassDef, index: int, facts: tuple | None = None):
        self.node = node
        self.index = index  # index in facts CLASSES (-1 for nested classes)
        self.name = facts[K_NAME] if facts is not None else node.name
        self.model: str | None = None
        self.is_model = False
        self.facts = facts  # Class tuple when only part of the class body is parsed


class _Scope:
    """A python scope (module, class body, function, lambda, comprehension)
    with the bindings of its names: ``name -> [binder]``.

    Binders: ("param", arg, index, fn) | ("expr", value, pos) |
    ("unpack", value, path, pos) | ("for", iterable, path, pos) |
    ("aug", value, op, pos) | ("ann", annotation, value, pos) |
    ("import", alias, stmt) | ("def", node) | ("gvar", stmt, pos) |
    ("cvar", stmt, pos) | ("except", handler) | ("global", stmt) |
    ("with", expr, pos) | ("unknown", node)."""

    __slots__ = ("node", "kind", "parent", "binders")

    def __init__(self, node: ast.AST | None, kind: str, parent: _Scope | None):
        self.node = node
        self.kind = kind
        self.parent = parent
        self.binders: dict[str, list[tuple]] = {}

    def add(self, name: str, binder: tuple) -> None:
        lst = self.binders.get(name)
        if lst is None:
            self.binders[name] = [binder]
        else:
            lst.append(binder)


class _Unit:
    """The analysis unit of a line: the outermost function (method) containing
    it, or the module / class-level statement. Owns a parent map and the
    scopes created inside it, and memoizes types."""

    __slots__ = ("root", "cls", "header", "base", "parents", "scopes", "memo", "vars", "attrs", "stores", "keys", "loads", "fills")

    def __init__(self, root: ast.AST, cls: _ClassInfo | None, header: bool, base: _Scope):
        self.root = root
        self.cls = cls  # class context of the root (None at module level)
        self.header = header  # class header only (decorators, bases, keywords)
        self.base = base  # scope the root statement lives in
        self.parents: dict[ast.AST, ast.AST] = {}
        self.scopes: dict[ast.AST, _Scope] = {}
        self.memo: dict[int, Any] = {}
        self.vars: dict[tuple, Any] = {}
        self.attrs: list[ast.Attribute] = []
        self.stores: dict[tuple[str, str], list[ast.AST]] = {}  # (name, key) -> values of name['key'] = value
        self.keys: dict[str, list[ast.AST]] = {}  # name -> computed keys k of name[k] = v / name[k].append(v)
        self.loads: dict[str, list[ast.Name]] = {}  # name -> Name nodes reading it
        self.fills: dict[str, list[ast.Call]] = {}  # name -> name.append/add/extend/insert(...) calls


class _File:
    """A parsed python source and its registry-independent structure."""

    def __init__(self, path: str, source: bytes, tree: ast.Module | None = None, lines: list[bytes] | None = None, partial: bool = False):
        self.path = path
        self.source = source
        self.lines = lines if lines is not None else source.split(b"\n")
        self.tree = tree if tree is not None else _parse(source)
        self.partial = partial  # only one method parsed (module scope from the facts)
        self.classes: list[_ClassInfo] = []
        self.class_by_node: dict[ast.AST, _ClassInfo] = {}
        self.module_scope = _Scope(self.tree, "module", None)
        self.class_scopes: dict[ast.AST, _Scope] = {}
        self.units: dict[tuple, _Unit] = {}
        self.framework = path.replace("\\", "/").endswith(FRAMEWORK_FILES)
        self.analyzer: _Analyzer | None = None
        self._nl: list[int] | None = None
        self._assigns: dict[ast.AST, dict[str, list[tuple[ast.AST, ast.AST | None, ast.Attribute]]]] = {}
        self.starts: dict[int, list[int]] = {}  # id(statement list) -> start lines (see _stmt_at)
        if self.tree is not None:
            self._collect_module(self.tree.body)

    # -- structure ------------------------------------------------------------

    def _collect_module(self, body: list[ast.stmt]) -> None:
        scope = self.module_scope
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name != "*":
                        scope.add(_bound_name(a, node), ("import", a, node))
            elif isinstance(node, ast.ClassDef):
                ci = _ClassInfo(node, len(self.classes))
                self.classes.append(ci)
                self.class_by_node[node] = ci
                scope.add(node.name, ("def", node))
            elif isinstance(node, _FUNC):
                scope.add(node.name, ("def", node))
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    for n in _target_names(t):
                        scope.add(n.id, ("gvar", node, n))
            elif isinstance(node, (ast.If,) + _TRY):
                self._collect_module(node.body)
                for h in getattr(node, "handlers", ()):
                    self._collect_module(h.body)
                self._collect_module(node.orelse)
                self._collect_module(getattr(node, "finalbody", []))

    def set_major(self, major: int | None) -> None:
        for ci in self.classes:
            if ci.facts is None:
                ci.model, ci.is_model = _class_model(ci.node, major, self.framework)
            elif self.framework and ci.name in FRAMEWORK_CLASSES:
                ci.model, ci.is_model = "base", True
            else:
                ci.model = model_class_info(ci.facts, major)[0] if ci.facts[K_MODEL] else None
                ci.is_model = bool(ci.facts[K_MODEL])

    def facts_module_scope(self, facts: dict) -> None:
        """Module scope from the facts (partial parses)."""
        scope = self.module_scope = _Scope(self.tree, "module", None)
        for imp in facts.get(IMPORTS) or ():
            if imp[I_NAME] != "*":
                scope.add(imp[I_ASNAME] or imp[I_NAME] or imp[I_MODULE].split(".")[0], ("fimport", imp))
        for sym in facts.get(SYMBOLS) or ():
            scope.add(sym[S_NAME], ("fsym", sym))

    def class_scope(self, ci: _ClassInfo) -> _Scope:
        s = self.class_scopes.get(ci.node)
        if s is None:
            s = _Scope(ci.node, "class", self.module_scope)
            for stmt in ci.node.body:
                if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                    for t in targets:
                        for n in _target_names(t):
                            s.add(n.id, ("cvar", stmt, n))
                elif isinstance(stmt, _FUNC + (ast.ClassDef,)):
                    s.add(stmt.name, ("def", stmt))
                elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    for a in stmt.names:
                        if a.name != "*":
                            s.add(_bound_name(a, stmt), ("import", a, stmt))
            self.class_scopes[ci.node] = s
        return s

    def instance_assigns(self, ci: _ClassInfo) -> dict[str, list[tuple[ast.AST, ast.AST | None, ast.Attribute]]]:
        """``attr -> [(method, value, target)]`` for ``self.attr = value`` /
        ``cls.attr = value`` statements in the methods of a class (value None
        when assigned by tuple unpacking)."""
        out = self._assigns.get(ci.node)
        if out is not None:
            return out
        out = {}
        for stmt in ci.node.body:
            if not isinstance(stmt, _FUNC):
                continue
            params = stmt.args.posonlyargs + stmt.args.args
            if not params:
                continue
            # ``__new__`` builds the instance itself: ``self = object.__new__(cls)``
            firsts = {params[0].arg, "self"} if stmt.name == "__new__" else {params[0].arg}
            for n in ast.walk(stmt):
                if isinstance(n, ast.Assign):
                    targets, value = n.targets, n.value
                elif isinstance(n, ast.AnnAssign) and n.value is not None:
                    targets, value = [n.target], n.value
                else:
                    continue
                for t in targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id in firsts:
                        out.setdefault(t.attr, []).append((stmt, value, t))
                    elif isinstance(t, (ast.Tuple, ast.List)):  # self.a, self.b = ...
                        for e in t.elts:
                            if isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name) and e.value.id in firsts:
                                out.setdefault(e.attr, []).append((stmt, None, e))
        self._assigns[ci.node] = out
        return out

    def line_of_offset(self, offset: int) -> int:
        if self._nl is None:
            self._nl = [i for i, b in enumerate(self.source) if b == 10] if len(self.source) < 4096 else [
                m.start() for m in re.finditer(b"\n", self.source)
            ]
        return bisect_left(self._nl, offset)

    def u16(self, line0: int, bcol: int) -> int:
        """UTF-8 byte column -> UTF-16 column on ``line0``."""
        try:
            b = self.lines[line0]
        except IndexError:
            return max(bcol, 0)
        return bytecol_to_utf16(b, bcol)

    # -- units ------------------------------------------------------------------

    def locate(self, line0: int) -> tuple[ast.AST | None, _ClassInfo | None, bool]:
        """(unit root, class context, header-only) for a 0-based line."""
        if self.tree is None:
            return None, None, False
        lineno = line0 + 1
        body: list[ast.stmt] = self.tree.body
        while True:
            stmt = _stmt_at(body, lineno, self.starts)
            if stmt is None:
                return None, None, False
            if isinstance(stmt, (ast.If,) + _TRY):
                inner = []
                for sub in [stmt.body, stmt.orelse, getattr(stmt, "finalbody", [])] + [h.body for h in getattr(stmt, "handlers", ())]:
                    if _stmt_at(sub, lineno, self.starts) is not None:
                        inner = sub
                        break
                if not inner:
                    return stmt, None, False
                body = inner
                continue
            if isinstance(stmt, ast.ClassDef):
                ci = self.class_by_node.get(stmt)
                if ci is None:
                    return stmt, None, False
                member = _stmt_at(stmt.body, lineno, self.starts)
                if member is None:
                    return stmt, ci, True
                return member, ci, False
            return stmt, None, False

    def unit_at(self, line0: int) -> _Unit | None:
        root, ci, header = self.locate(line0)
        if root is None:
            return None
        return self.unit(root, ci, header)

    def unit(self, root: ast.AST, ci: _ClassInfo | None, header: bool = False) -> _Unit:
        key = (id(root), header)
        u = self.units.get(key)
        if u is None:
            u = self._build_unit(root, ci, header)
            self.units[key] = u
        return u

    def _build_unit(self, root: ast.AST, ci: _ClassInfo | None, header: bool) -> _Unit:
        """One walk over the unit: parent map, scopes and their bindings, plus
        indexes of attributes, loaded names, subscript stores and container fills."""
        if ci is not None and not header:
            base = self.class_scope(ci)
        else:
            base = self.module_scope
        u = _Unit(root, ci, header, base)
        parents = u.parents
        scopes = u.scopes
        attrs = u.attrs
        loads = u.loads
        start = base
        if isinstance(root, _FUNC):
            start = _Scope(root, "function", base)
            scopes[root] = start
            _bind_params(start, root)
        stack: list[tuple[ast.AST, _Scope]] = []
        if header and isinstance(root, ast.ClassDef):
            for child in list(root.decorator_list) + list(root.bases) + list(root.keywords):
                parents[child] = root
                stack.append((child, start))
        else:
            stack.append((root, start))
        AST = ast.AST
        Name, Attribute, Call, Load = ast.Name, ast.Attribute, ast.Call, ast.Load
        while stack:
            node, scope = stack.pop()
            t = type(node)
            if t is Attribute:
                attrs.append(node)
            elif t is Name:
                if type(node.ctx) is Load:  # type: ignore[attr-defined]
                    loads.setdefault(node.id, []).append(node)  # type: ignore[attr-defined]
            elif t is Call:
                _index_fill(u, node)  # type: ignore[arg-type]
            elif t in _BINDING_TYPES:
                if scope is not base:
                    _bind_node(node, scope)
                if t is ast.Assign or t is ast.AugAssign:
                    for tg in node.targets if t is ast.Assign else [node.target]:  # type: ignore[attr-defined]
                        if type(tg) is ast.Subscript and type(tg.value) is Name:
                            key = _str_const(tg.slice)  # type: ignore[attr-defined]
                            u.stores.setdefault((tg.value.id, key), []).append(node.value)  # type: ignore[attr-defined]
                            if key is None:
                                u.keys.setdefault(tg.value.id, []).append(tg.slice)  # type: ignore[attr-defined]
            for field in node._fields:
                if field == "ctx":
                    continue
                v = getattr(node, field, None)
                if v is None:
                    continue
                if type(v) is list:
                    children = v
                elif isinstance(v, AST):
                    children = (v,)
                else:
                    continue
                for child in children:
                    if not isinstance(child, AST):
                        continue
                    parents[child] = node
                    kind = _SCOPE_KINDS.get(type(child))
                    if kind is None:
                        stack.append((child, scope))
                        continue
                    cs = _Scope(child, kind, scope)
                    scopes[child] = cs
                    if kind == "function" or kind == "lambda":
                        if kind == "function" and scope is not base:
                            scope.add(child.name, ("def", child))  # type: ignore[attr-defined]
                        _bind_params(cs, child)
                    elif kind == "comp":
                        for gen in child.generators:  # type: ignore[attr-defined]
                            _bind_for(cs, gen.target, gen.iter, ())
                    elif scope is not base:  # nested class
                        scope.add(child.name, ("def", child))  # type: ignore[attr-defined]
                    stack.append((child, cs))
        return u


def _index_fill(u: _Unit, node: ast.Call) -> None:
    """Record ``x.append(...)`` / ``d[k].append(...)`` / ``d.setdefault(k, []).append(...)``."""
    func = node.func
    if type(func) is not ast.Attribute or func.attr not in FILL_METHODS:
        return
    target = func.value
    if isinstance(target, ast.Name):
        u.fills.setdefault(target.id, []).append(node)
    elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
        u.fills.setdefault(target.value.id + "[]", []).append(node)
        if _str_const(target.slice) is None:
            u.keys.setdefault(target.value.id, []).append(target.slice)
    elif isinstance(target, ast.Call) and _call_name(target.func) == "setdefault" and isinstance(target.func.value, ast.Name):  # type: ignore[attr-defined]
        u.fills.setdefault(target.func.value.id + "[]", []).append(node)  # type: ignore[attr-defined]
        if target.args and _str_const(target.args[0]) is None:
            u.keys.setdefault(target.func.value.id, []).append(target.args[0])  # type: ignore[attr-defined]


def _stmt_at(body: list[ast.stmt], lineno: int, starts: dict[int, list[int]]) -> ast.stmt | None:
    """The statement of ``body`` spanning 1-based ``lineno`` (decorators included);
    ``starts`` caches the start lines of long bodies (by id: they belong to the file)."""
    if len(body) < 16:
        for stmt in body:
            if _start(stmt) <= lineno <= (stmt.end_lineno or stmt.lineno):
                return stmt
        return None
    lines = starts.get(id(body))
    if lines is None:
        lines = starts[id(body)] = [_start(stmt) for stmt in body]
    i = bisect_right(lines, lineno) - 1
    if i >= 0 and lineno <= (body[i].end_lineno or body[i].lineno):
        return body[i]
    return None


def _class_model(node: ast.ClassDef, major: int | None, framework: bool) -> tuple[str | None, bool]:
    """(model, is_model_class) like the facts extractor + ``model_class_info``.
    The framework classes (BaseModel & co) are analysed as the ``base`` model,
    whose MRO ends with them."""
    if framework and node.name in FRAMEWORK_CLASSES:
        return "base", True
    meta: dict[str, Any] = {}
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            target, value = stmt.targets[0].id, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            target, value = stmt.target.id, stmt.value
        else:
            continue
        if target in ("_name", "_inherit"):
            if isinstance(value, ast.Constant):
                meta[target] = value.value
            elif isinstance(value, (ast.List, ast.Tuple)):
                meta[target] = [e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    kind = None
    for b in node.bases:
        last = (_dotted(b) or "").rsplit(".", 1)[-1]
        if last in MODEL_BASES:
            kind = last
            break
    if "_name" not in meta and "_inherit" not in meta and kind is None:
        return None, False
    name = meta.get("_name") if isinstance(meta.get("_name"), str) else None
    inherit = meta.get("_inherit")
    inherit_str = isinstance(inherit, str)
    inherit = [inherit] if inherit_str else (inherit if isinstance(inherit, list) else [])
    fake = (node.name, 0, 0, 0, 0, (), {MI_NAME: name, MI_INHERIT: inherit, MI_INHERIT_STR: inherit_str})
    model = model_class_info(fake, major)[0]
    return model, True


def _bind_params(scope: _Scope, fn: ast.AST) -> None:
    args = fn.args  # type: ignore[attr-defined]
    for i, a in enumerate(list(args.posonlyargs) + list(args.args)):
        scope.add(a.arg, ("param", a, i, fn))
    for a in (args.vararg, args.kwarg):
        if a is not None:
            scope.add(a.arg, ("param", a, -1, fn))
    for a in args.kwonlyargs:
        scope.add(a.arg, ("param", a, -2, fn))


def _bind_target(scope: _Scope, target: ast.AST, value: ast.AST | None, path: tuple) -> None:
    if isinstance(target, ast.Name):
        if value is None:
            scope.add(target.id, ("unknown", target))
        elif path:
            scope.add(target.id, ("unpack", value, path, target))
        else:
            scope.add(target.id, ("expr", value, target))
    elif isinstance(target, (ast.Tuple, ast.List)):
        elts = target.elts
        if (
            not path
            and isinstance(value, (ast.Tuple, ast.List))
            and len(value.elts) == len(elts)
            and not any(isinstance(e, ast.Starred) for e in elts + value.elts)
        ):
            for t, v in zip(elts, value.elts):
                _bind_target(scope, t, v, ())
        else:
            star = next((i for i, t in enumerate(elts) if isinstance(t, ast.Starred)), None)
            for i, t in enumerate(elts):
                if isinstance(t, ast.Starred):
                    _bind_target(scope, t.value, None, ())
                else:  # items after a starred target are counted from the end
                    _bind_target(scope, t, value, path + (i if star is None or i < star else i - len(elts),))
    elif isinstance(target, ast.Starred):
        _bind_target(scope, target.value, None, ())


def _bind_for(scope: _Scope, target: ast.AST, iterable: ast.AST, path: tuple) -> None:
    if isinstance(target, ast.Name):
        scope.add(target.id, ("for", iterable, path, target))
    elif isinstance(target, (ast.Tuple, ast.List)):
        elts = target.elts
        star = next((i for i, t in enumerate(elts) if isinstance(t, ast.Starred)), None)
        for i, t in enumerate(elts):
            if isinstance(t, ast.Starred):
                _bind_target(scope, t.value, None, ())
            else:
                _bind_for(scope, t, iterable, path + (i if star is None or i < star else i - len(elts),))


_BINDING_TYPES = frozenset(
    (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith, ast.NamedExpr,
     ast.Import, ast.ImportFrom, ast.ExceptHandler, ast.Global, ast.Nonlocal) + _MATCH_NAMES
)


def _bind_node(node: ast.AST, scope: _Scope) -> None:
    """Record the name bindings made by ``node`` (a statement or expression) in ``scope``."""
    if isinstance(node, ast.Assign):
        for t in node.targets:
            _bind_target(scope, t, node.value, ())
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            scope.add(node.target.id, ("ann", node.annotation, node.value, node.target))
    elif isinstance(node, ast.AugAssign):
        if isinstance(node.target, ast.Name):
            scope.add(node.target.id, ("aug", node.value, node.op, node.target))
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        _bind_for(scope, node.target, node.iter, ())
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                for n in _target_names(item.optional_vars):
                    scope.add(n.id, ("with", item.context_expr, n))
    elif isinstance(node, ast.NamedExpr):
        s = scope
        while s.kind == "comp" and s.parent is not None:
            s = s.parent
        if isinstance(node.target, ast.Name):
            s.add(node.target.id, ("expr", node.value, node.target))
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            if a.name != "*":
                scope.add(_bound_name(a, node), ("import", a, node))
    elif isinstance(node, ast.ExceptHandler):
        if node.name:
            scope.add(node.name, ("except", node))
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        for n in node.names:
            scope.add(n, ("global", node))
    elif _MATCH_NAMES and isinstance(node, _MATCH_NAMES):
        if getattr(node, "name", None):
            scope.add(node.name, ("unknown", node))  # type: ignore[attr-defined]


def _same_frame(scope: _Scope, s: _Scope) -> bool:
    """``scope`` is ``s`` or nested in it through lambdas / comprehensions only."""
    x: _Scope | None = scope
    while x is not None and x is not s:
        if x.kind not in ("comp", "lambda"):
            return False
        x = x.parent
    return x is s


def _binder_node(b: tuple) -> ast.AST | None:
    k = b[0]
    if k in ("param", "def", "except", "unknown"):
        return b[1]
    if k in ("import", "gvar", "cvar", "global"):
        return b[2] if len(b) > 2 else b[1]
    return b[-1]


def _binder_order(b: tuple) -> tuple[int, int]:
    if b[0] == "param":
        return (0, 0)
    n = _binder_node(b)
    return (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))


def _binder_stmt(b: tuple, parents: dict) -> ast.AST | None:
    """The statement (or walrus) performing the binding."""
    k = b[0]
    if k == "param":
        return None
    if k == "def":
        return b[1]
    if k == "import":
        return b[2]
    n = _binder_node(b)
    while n is not None and not isinstance(n, ast.stmt):
        if isinstance(n, ast.NamedExpr):
            return n
        n = parents.get(n)
    return n


def _reaches(b: tuple, stmt: ast.AST | None, use: ast.AST, upos: tuple, parents: dict) -> bool:
    """The binding happens before ``use`` (straight-line order)."""
    if b[0] == "param" or stmt is None:
        return True
    if upos > (stmt.end_lineno, stmt.end_col_offset):  # type: ignore[attr-defined]
        return True
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
        n: ast.AST | None = use
        while n is not None:
            p = parents.get(n)
            if p is stmt:  # in the body, not in the iterable / context expression
                return isinstance(n, ast.stmt)
            n = p
    return False


def _inside(node: ast.AST, ancestor: ast.AST, parents: dict) -> bool:
    n: ast.AST | None = node
    while n is not None:
        if n is ancestor:
            return True
        n = parents.get(n)
    return False


def _enclosing_loops(node: ast.AST, parents: dict) -> list[ast.AST]:
    out = []
    n = parents.get(node)
    while n is not None and not isinstance(n, _FUNC + (ast.Lambda, ast.ClassDef)):
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            out.append(n)
        n = parents.get(n)
    return out


def _dominates(stmt: ast.AST, use: ast.AST, parents: dict) -> bool:
    """``stmt`` runs before ``use`` on every path: ``use`` is inside it or after
    it in the same block."""
    if isinstance(stmt, ast.NamedExpr):
        n = parents.get(stmt)
        while n is not None and not isinstance(n, ast.stmt):
            n = parents.get(n)
        if n is None:
            return False
        stmt = n
    bp = parents.get(stmt)
    block = None
    if bp is not None:
        for field in ("body", "orelse", "finalbody"):
            lst = getattr(bp, field, None)
            if isinstance(lst, list) and any(x is stmt for x in lst):
                block = lst
                break
    n: ast.AST | None = use
    while n is not None:
        if n is stmt:
            return True
        p = parents.get(n)
        if block is not None and p is bp and any(x is n for x in block):
            return _index(block, n) > _index(block, stmt)
        n = p
    return False


def _index(lst: list, node: ast.AST) -> int:
    for i, x in enumerate(lst):
        if x is node:
            return i
    return -1


def _lookup(scope: _Scope | None, name: str) -> tuple[_Scope | None, list[tuple] | None]:
    """Python name resolution (LEGB without builtins): class bodies are only
    visible from the class body itself."""
    s = scope
    first = True
    while s is not None:
        if s.kind == "class" and not first:
            s = s.parent
            continue
        b = s.binders.get(name)
        if b:
            if b[0][0] == "global":
                m = s
                while m.parent is not None:
                    m = m.parent
                b2 = m.binders.get(name)
                return (m, b2) if b2 else (None, None)
            return s, b
        first = False
        s = s.parent
    return None, None


# ---------------------------------------------------------------------------
# file cache

_LRU_SIZE = 32
_FILES: OrderedDict[tuple, _File] = OrderedDict()
_PARTIAL_USES: dict[tuple, int] = {}


def _get_file(path: str, source: bytes) -> _File:
    key = (path, len(source), hash(source))
    f = _FILES.get(key)
    if f is not None:
        _FILES.move_to_end(key)
        return f
    f = _File(path, source)
    _FILES[key] = f
    while len(_FILES) > _LRU_SIZE:
        _drop(_FILES.popitem(last=False)[1])
    return f


def _drop(f: _File) -> None:
    """Break the _File <-> _Analyzer cycle of an evicted entry so that refcounting
    frees its AST right away (and it can never pin a registry)."""
    f.analyzer = None


def clear_caches() -> None:
    """Forget every parsed file and derived cache (e.g. when a registry is evicted)."""
    for f in _FILES.values():
        _drop(f)
    _FILES.clear()
    _PARTIAL_USES.clear()
    _SYM_CACHE.clear()
    _RET_CACHE.clear()
    _SNIPPETS.clear()
    _LINES.clear()


def _file_for_path(path: str) -> _File | None:
    try:
        with open(path, "rb") as fh:
            source = fh.read()
    except OSError:
        return None
    return _get_file(path, source)


def _partial_file(path: str, source: bytes, facts: dict | None, line0: int) -> _File | None:
    """For a big file, a _File holding only the method (of a model class) that
    contains ``line0``, delimited with the facts; None when not applicable."""
    if not facts or source.count(b"\n") < PARTIAL_MIN_LINES:
        return None
    full = (path, len(source), hash(source))
    if full in _FILES:
        return None  # already fully parsed
    _PARTIAL_USES[full] = n = _PARTIAL_USES.get(full, 0) + 1
    if n > PARTIAL_MAX_USES:
        _PARTIAL_USES.pop(full, None)
        return None  # used a lot: parse it all once
    if len(_PARTIAL_USES) > 256:
        _PARTIAL_USES.clear()
    for idx, cls in enumerate(facts.get(CLASSES) or ()):
        if not cls[K_LINE] <= line0 <= cls[4]:
            continue
        if not cls[K_MODEL]:
            return None
        ends = sorted([f[4] for f in cls[K_FIELDS]] + [m[M_END_LINE] for m in cls[K_METHODS]] + [a[A_LINE] for a in cls[K_ATTRS]])
        for m in cls[K_METHODS]:
            prev = [e for e in ends if e < m[M_LINE]]
            start = (prev[-1] if prev else cls[K_LINE]) + 1
            if start <= line0 <= m[M_END_LINE]:
                key = (path, len(source), hash(source), start)
                f = _FILES.get(key)
                if f is not None:
                    _FILES.move_to_end(key)
                    return f
                f = _method_file(path, source, facts, idx, cls, m, start)
                if f is not None:
                    _FILES[key] = f
                    while len(_FILES) > _LRU_SIZE:
                        _drop(_FILES.popitem(last=False)[1])
                return f
        return None
    return None


def _method_file(path: str, source: bytes, facts: dict, idx: int, cls: tuple, m: tuple, start: int) -> _File | None:
    lines = source.split(b"\n")
    if not lines[m[M_LINE]][:1] in (b" ", b"\t"):
        return None
    tree = _parse(b"class _S:\n" + b"\n".join(lines[start : m[M_END_LINE] + 1]))
    if tree is None or len(tree.body) != 1 or not isinstance(tree.body[0], ast.ClassDef):
        return None
    ast.increment_lineno(tree, start - 1)
    fake = tree.body[0]
    fn = fake.body[-1] if fake.body else None
    if not isinstance(fn, _FUNC) or fn.name != m[M_NAME] or fn.lineno - 1 != m[M_LINE]:
        return None  # facts out of sync with the source
    f = _File(path, source, tree, lines, partial=True)
    f.facts_module_scope(facts)
    ci = _ClassInfo(fake, idx, cls)
    f.classes = [ci]
    f.class_by_node = {fake: ci}
    return f


_CURRENT: list = [None, -1]  # weak reference to the registry the caches belong to, its generation


def _use_registry(registry: Any) -> None:
    """Drop every registry-derived result when the registry (or its generation)
    changes: caches never keep a replaced registry alive. Parsed files and their
    units are registry-independent and kept."""
    ref = _CURRENT[0]
    if ref is not None and ref() is registry and _CURRENT[1] == registry.generation:
        return
    _SYM_CACHE.clear()
    _RET_CACHE.clear()
    _SNIPPETS.clear()
    for f in _FILES.values():
        if f.analyzer is not None:
            f.analyzer = None
            for u in f.units.values():
                u.memo.clear()
                u.vars.clear()
    _CURRENT[0] = weakref.ref(registry)
    _CURRENT[1] = registry.generation


def _analyzer(registry: Any, path: str, source: bytes, facts: dict | None = None, line0: int | None = None) -> _Analyzer:
    """Analyzer of ``source``; with facts and a line, possibly over a partial
    parse of the method containing the line (big files)."""
    f = _partial_file(path, source, facts, line0) if line0 is not None else None
    if f is None:
        f = _get_file(path, source)
    an = f.analyzer
    if an is None or an.reg is not registry or an.generation != registry.generation:
        if an is not None:
            for u in f.units.values():
                u.memo.clear()
                u.vars.clear()
        an = _Analyzer(f, registry)
        f.analyzer = an
    return an


# ---------------------------------------------------------------------------
# python symbols (imports, re-exports) - memoized for the current registry

_SYM_CACHE: dict[tuple, Any] = {}
MAX_HOPS = 6


def _abs_module(reg: Any, path: str, module: str, level: int) -> str | None:
    """Absolute dotted name of ``from <level dots><module> import`` in ``path``."""
    if not level:
        return module
    dotted = reg.py_dotted_name(path)
    if not dotted:
        return None
    pkg = dotted if path.endswith("__init__.py") else dotted.rpartition(".")[0]
    for _ in range(level - 1):
        pkg = pkg.rpartition(".")[0]
    if not pkg:
        return None
    return f"{pkg}.{module}" if module else pkg


def _module_sym(reg: Any, dotted: str) -> tuple | None:
    path = reg.py_module_file(dotted)
    return ("module", dotted, path)


def _resolve_symbol(reg: Any, dotted: str, name: str, depth: int = 0) -> tuple | None:
    """Where ``name`` imported from python module ``dotted`` is defined, following
    re-exports. ("sym", path, name, kind, line, col, end_col) | ("module", dotted, path)."""
    key = (dotted, name)
    if key in _SYM_CACHE:
        return _SYM_CACHE[key]
    if len(_SYM_CACHE) > 20000:
        _SYM_CACHE.clear()
    _SYM_CACHE[key] = None  # cycle guard
    res = _resolve_symbol_uncached(reg, dotted, name, depth)
    _SYM_CACHE[key] = res
    return res


def _resolve_symbol_uncached(reg: Any, dotted: str, name: str, depth: int) -> tuple | None:
    if depth > MAX_HOPS:
        return None
    path = reg.py_module_file(dotted)
    sub = f"{dotted}.{name}"
    if path is None:
        if dotted == "odoo" and name in ODOO_INIT_NAMES:
            mod, real = ODOO_INIT_NAMES[name]
            return _resolve_symbol(reg, mod, real, depth + 1)
        if reg.py_module_file(sub):
            return _module_sym(reg, sub)
        return None
    facts = reg.file_facts(path) or {}
    var = None
    for s in facts.get(SYMBOLS) or ():
        if s[S_NAME] == name:
            if s[S_KIND] != "var":
                return ("sym", path, name, s[S_KIND], s[S_LINE], s[S_COL], s[S_END_COL])
            if var is None:
                var = ("sym", path, name, "var", s[S_LINE], s[S_COL], s[S_END_COL])
    if path.endswith("__init__.py") and reg.py_module_file(sub):
        return _module_sym(reg, sub)
    stars = []
    for imp in facts.get(IMPORTS) or ():
        iname = imp[I_NAME]
        if iname == "*":
            stars.append(imp)
            continue
        if iname is None:  # import a.b.c [as x]
            if imp[I_ASNAME] == name:
                return _module_sym(reg, imp[I_MODULE])
            if not imp[I_ASNAME] and imp[I_MODULE].split(".")[0] == name:
                return _module_sym(reg, name)
            continue
        if (imp[I_ASNAME] or iname) != name:
            continue
        mod = _abs_module(reg, path, imp[I_MODULE], imp[I_LEVEL])
        if mod is None:
            continue
        if reg.py_module_file(f"{mod}.{iname}") and not _defines(reg, mod, iname):
            return _module_sym(reg, f"{mod}.{iname}")
        res = _resolve_symbol(reg, mod, iname, depth + 1)
        if res is not None:
            return res
        return var or ("sym", path, name, "import", imp[4], imp[5], imp[6])
    if var is not None:
        return var
    for imp in stars:
        mod = _abs_module(reg, path, imp[I_MODULE], imp[I_LEVEL])
        if mod:
            res = _resolve_symbol(reg, mod, name, depth + 1)
            if res is not None:
                return res
    if reg.py_module_file(sub):
        return _module_sym(reg, sub)
    return None


def _defines(reg: Any, dotted: str, name: str) -> bool:
    path = reg.py_module_file(dotted)
    facts = reg.file_facts(path) if path else None
    return bool(facts) and any(s[S_NAME] == name and s[S_KIND] != "var" for s in facts.get(SYMBOLS) or ())


def _class_facts(reg: Any, path: str, name: str) -> tuple | None:
    for cls in (reg.file_facts(path) or {}).get(CLASSES) or ():
        if cls[K_NAME] == name:
            return cls
    return None


def _resolve_in_file(reg: Any, path: str, dotted: str, depth: int = 0) -> tuple | None:
    """Resolve a dotted expression (e.g. a base class ``portal.CustomerPortal``)
    written at module level in ``path``."""
    first, _, rest = dotted.partition(".")
    dotted_mod = reg.py_dotted_name(path)
    sym = _resolve_symbol(reg, dotted_mod, first, depth + 1) if dotted_mod else None
    for part in rest.split(".") if rest else ():
        if sym is None:
            return None
        if sym[0] == "module":
            sym = _resolve_symbol(reg, sym[1], part, depth + 1)
        elif sym[0] == "sym" and sym[3] == "class":
            sym = _class_member(reg, sym[1], sym[2], part, depth + 1)
        else:
            return None
    return sym


def _class_member(reg: Any, path: str, clsname: str, attr: str, depth: int = 0) -> tuple | None:
    """A method / class attribute of python class ``clsname`` defined in ``path``,
    searched through its python bases."""
    if depth > MAX_HOPS:
        return None
    key = ("member", path, clsname, attr)
    if key in _SYM_CACHE:
        return _SYM_CACHE[key]
    _SYM_CACHE[key] = None
    res = None
    cls = _class_facts(reg, path, clsname)
    if cls is not None:
        for m in cls[K_METHODS]:
            if m[M_NAME] == attr:
                res = ("sym", path, attr, "def", m[M_LINE], m[M_COL], m[M_END_COL])
                break
        if res is None:
            for a in cls[K_ATTRS]:
                if a[A_NAME] == attr:
                    res = ("sym", path, attr, "var", a[A_LINE], a[A_COL], a[A_END_COL])
                    break
        if res is None:
            for fd in cls[K_FIELDS]:
                if fd[F_NAME] == attr:
                    res = ("sym", path, attr, "var", fd[F_LINE], fd[F_COL], fd[F_END_COL])
                    break
        if res is None:
            for base in cls[K_BASES]:
                if base == "?" or base == clsname:
                    continue
                bsym = _resolve_in_file(reg, path, base, depth + 1)
                if bsym is not None and bsym[0] == "sym" and bsym[3] == "class":
                    res = _class_member(reg, bsym[1], bsym[2], attr, depth + 1)
                    if res is not None:
                        break
    _SYM_CACHE[key] = res
    return res


# ---------------------------------------------------------------------------
# the analyzer


class _Analyzer:
    """Inference over one :class:`_File` with one registry (generation)."""

    def __init__(self, f: _File, reg: Any):
        self.f = f
        self.reg = reg
        self.generation = reg.generation
        self.path = f.path
        version = getattr(getattr(reg, "env", None), "version", None)
        self.major: int | None = version.major if version is not None else None
        self.scope = reg.scope_for_path(f.path)
        self.module = reg.module_of_path(f.path)
        self.dotted = reg.py_dotted_name(f.path)
        self.minor = version.minor if version is not None else 0
        f.set_major(self.major)
        self._members: dict[tuple, Any] = {}  # (model, name) -> (kind, definitions)
        self._comodels: dict[tuple, str | None] = {}
        self._lineages: dict[str, set[str]] = {}
        self._insts: dict[tuple, Any] = {}  # (path, class, attr) -> type
        self._gvars: dict[tuple, Any] = {}
        self._depth = 0
        self._hops = 0
        self._cuts = 0  # bumped whenever a depth limit truncates an inference
        self._steps = 0
        self._rank_info: tuple | None = None
        self._search_methods: dict[str, frozenset[str]] = {}

    def reset_budget(self) -> None:
        self._steps = 0

    # -- registry helpers ---------------------------------------------------

    def member(self, model: str, name: str) -> tuple[str | None, list]:
        """(kind, definitions) of member ``name`` of ``model``: scoped lookup first,
        then the whole env."""
        key = (model, name)
        hit = self._members.get(key)
        if hit is not None:
            return hit
        reg = self.reg
        res: tuple[str | None, list] = (None, [])
        for scope in ((self.scope, None) if self.scope is not None else (None,)):
            d = reg.field(model, name, scope)
            if d:
                res = ("field", d)
                break
            d = reg.method(model, name, scope)
            if d:
                res = ("method", d)
                break
            d = reg.attr(model, name, scope)
            if d:
                res = ("attr", d)
                break
        self._members[key] = res
        return res

    def comodel(self, model: str, name: str, _depth: int = 0) -> str | None:
        """Comodel of a relational field, following ``related=`` paths when the
        field does not declare one (``company_id = fields.Many2one(related=...)``)."""
        key = (model, name)
        if key in self._comodels:
            return self._comodels[key]
        self._comodels[key] = None
        kind, defs = self.member(model, name)
        cm = None
        if kind == "field":
            cm = next((d.facts[F_COMODEL] for d in defs if d.facts[F_COMODEL]), None)
            if cm is None and defs[0].facts[F_TYPE] in RELATIONAL:
                args: dict = {}
                for d in reversed(defs):  # merged like Odoo: base-most first
                    args.update(d.facts[F_ARGS] or {})
                cm = args.get("comodel_name") if isinstance(args.get("comodel_name"), str) else None
                related = args.get("related")
                if cm is None and isinstance(related, str) and _depth < 5:
                    cur: str | None = model
                    for seg in related.split("."):
                        cur = self.comodel(cur, seg, _depth + 1) if cur else None
                    cm = cur
        self._comodels[key] = cm
        return cm

    def has_model(self, model: str) -> bool:
        return self.reg.has_model(model, self.scope) or (self.scope is not None and self.reg.has_model(model))

    def field_type(self, model: str, name: str) -> tuple | None:
        """Type of ``record.name`` for a field (None when not a field)."""
        kind, defs = self.member(model, name)
        if kind != "field":
            return None
        ftype = defs[0].facts[F_TYPE]
        if ftype in RELATIONAL:
            cm = self.comodel(model, name)
            return ("rs", (cm,)) if cm else None
        if ftype == "Reference":
            return None
        return ("val", ftype)

    def walk(self, model: str, path: str) -> list[tuple[str, str | None, list]]:
        """Like ``registry.walk_path`` (segment, owner, definitions), each segment
        looked up scoped then unscoped, comodels following ``related=``."""
        out: list[tuple[str, str | None, list]] = []
        cur: str | None = model
        for seg in path.split("."):
            if cur is None:
                out.append((seg, None, []))
                break
            kind, defs = self.member(cur, seg)
            defs = defs if kind == "field" else []
            out.append((seg, cur, defs))
            if not defs:
                break
            cur = self.comodel(cur, seg)
        return out

    def path_type(self, models: tuple, path: str) -> tuple | None:
        """Type of ``records.mapped(path)``."""
        out: list[str] = []
        for m in models:
            res = self.walk(m, path)
            if len(res) == len(path.split(".")) and res[-1][2]:
                seg, owner, defs = res[-1]
                if defs[0].facts[F_TYPE] in RELATIONAL and owner:
                    cm = self.comodel(owner, seg)
                    if cm:
                        out.append(cm)
                        continue
                return ("list", ("val", defs[0].facts[F_TYPE]))
        return _rs(out) if out else None

    def xmlid_model(self, raw: str) -> str | None:
        xmlid = self.reg.qualify_xmlid(raw, self.module)
        recs = self.reg.records(xmlid)
        return recs[0].model if recs else None

    # -- scopes ---------------------------------------------------------------

    def scope_of(self, node: ast.AST, u: _Unit) -> _Scope:
        parents = u.parents
        scopes = u.scopes
        n = parents.get(node)
        while n is not None:
            s = scopes.get(n)
            if s is not None:
                return s
            n = parents.get(n)
        s = scopes.get(u.root)
        return s if s is not None else u.base

    def class_of_function(self, fn: ast.AST, u: _Unit) -> _ClassInfo | None:
        """Class whose method ``fn`` is (None for plain functions / lambdas)."""
        if fn is u.root:
            return u.cls
        parent = u.parents.get(fn)
        if isinstance(parent, ast.ClassDef):
            ci = self.f.class_by_node.get(parent)
            if ci is None:
                ci = _ClassInfo(parent, -1)
                self.f.class_by_node[parent] = ci
            return ci
        return None

    def enclosing_method(self, node: ast.AST, u: _Unit) -> tuple[ast.AST | None, _ClassInfo | None]:
        """Innermost def containing ``node`` that is a method, with its class."""
        n: ast.AST | None = node
        while n is not None:
            if isinstance(n, _FUNC):
                ci = self.class_of_function(n, u)
                if ci is not None:
                    return n, ci
            n = u.parents.get(n)
        return None, u.cls

    # -- inference --------------------------------------------------------------

    def infer(self, node: ast.AST | None, u: _Unit) -> tuple | None:
        if node is None:
            return None
        memo = u.memo
        key = id(node)
        if key in memo:
            return memo[key]
        self._steps += 1
        if self._depth > MAX_DEPTH or self._steps > MAX_STEPS:
            self._cuts += 1
            return None
        memo[key] = None  # cycle guard
        self._depth += 1
        cuts = self._cuts
        try:
            t = self._infer(node, u)
        finally:
            self._depth -= 1
        if self._cuts != cuts:
            del memo[key]  # truncated by a depth limit: may be complete from a shallower start
        else:
            memo[key] = t
        return t

    def _infer(self, node: ast.AST, u: _Unit) -> tuple | None:
        if isinstance(node, ast.Name):
            return self.name_type(node, u)
        if isinstance(node, ast.Attribute):
            return self.attr_type(node, u)
        if isinstance(node, ast.Call):
            return self.call_type(node, u)
        if isinstance(node, ast.Subscript):
            return self.subscript_type(node, u)
        if isinstance(node, ast.Constant):
            v = node.value
            return None if v is None or v is False else ("val", type(v).__name__)
        if isinstance(node, ast.BinOp):
            lt = self.infer(node.left, u)
            rt = self.infer(node.right, u)
            if isinstance(node.op, (ast.BitOr, ast.BitAnd, ast.Sub, ast.Add)):
                if (lt and lt[0] == "rs") or (rt and rt[0] == "rs"):
                    return _union((lt if lt and lt[0] == "rs" else None, rt if rt and rt[0] == "rs" else None))
                if isinstance(node.op, ast.Add) and lt and lt[0] == "list":
                    return _union((lt, rt))
            kinds = {t[1] for t in (lt, rt) if t is not None and t[0] == "val"}
            if "str" in kinds or isinstance(node.op, ast.Mod) and lt == ("val", "str"):
                return ("val", "str")
            if kinds == {"int"}:
                return ("val", "int")
            return VAL if lt is not None or rt is not None else None
        if isinstance(node, ast.BoolOp):
            types = [self.infer(v, u) for v in node.values]
            if isinstance(node.op, ast.And):
                return types[-1]
            return _union(types)
        if isinstance(node, ast.IfExp):
            return _union((self.infer(node.body, u), self.infer(node.orelse, u)))
        if isinstance(node, ast.NamedExpr):
            return self.infer(node.value, u)
        if isinstance(node, ast.JoinedStr):
            return ("val", "str")
        if isinstance(node, ast.Compare) or isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return ("val", "bool")
        if isinstance(node, ast.UnaryOp):
            t = self.infer(node.operand, u)
            return t if t is not None and t[0] == "val" else VAL
        if isinstance(node, (ast.List, ast.Set)):
            return ("list", _homogeneous(self.infer(e, u) for e in node.elts))
        if isinstance(node, ast.Tuple):
            return ("tuple", tuple(self.infer(e, u) for e in node.elts))
        if isinstance(node, ast.Dict):
            vals = self.vals_models(node, u)
            if vals:
                return ("vals", vals[0])
            entries = {}
            for k, v in zip(node.keys, node.values):
                key = _str_const(k)
                if key is not None:
                    entries[key] = v
            keys = _homogeneous(self.infer(k, u) for k in node.keys if k is not None)
            values = _homogeneous((self.infer(v, u) for v in node.values), strict=bool(entries))
            # per-key values, evaluated lazily by this analyzer in this unit
            return ("dict", keys, values, (self, u, entries, ()) if entries else None)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return ("list", self.infer(node.elt, u))
        if isinstance(node, ast.DictComp):
            return ("dict", self.infer(node.key, u), self.infer(node.value, u), None)
        if isinstance(node, ast.Lambda):
            return OBJ
        if isinstance(node, ast.Starred):
            return self.infer(node.value, u)
        return None

    def name_type(self, node: ast.Name, u: _Unit) -> tuple | None:
        scope = self.scope_of(node, u)
        s, binders = _lookup(scope, node.id)
        if binders is None or s is None:
            return ("builtin", node.id) if node.id in BUILTINS else None
        if len(binders) > 1 and s.kind == "function" and _same_frame(scope, s):
            binders = self.reaching(binders, node, u)
        return _union([self.binder_type_cached(b, s, u) for b in binders])

    def binder_type_cached(self, b: tuple, s: _Scope, u: _Unit) -> tuple | None:
        local = s.node is not None and u.scopes.get(s.node) is s
        cache = u.vars if local else self._gvars
        key = id(b)
        if key in cache:
            return cache[key]
        if self._hops >= MAX_NAME_HOPS:
            self._cuts += 1
            return None
        cache[key] = None  # cycle guard
        self._hops += 1
        cuts = self._cuts
        try:
            t = self.binder_type(b, s, u)
        finally:
            self._hops -= 1
        if self._cuts != cuts:
            del cache[key]
        else:
            cache[key] = t
        return t

    def reaching(self, binders: list[tuple], use: ast.AST, u: _Unit) -> list[tuple]:
        """Binders of a function local that may reach ``use``: those before it
        (the last one alone when it dominates the use) plus loop-carried ones;
        all of them when none precedes the use."""
        parents = u.parents
        upos = (use.lineno, use.col_offset)  # type: ignore[attr-defined]
        loops = _enclosing_loops(use, parents)
        before: list[tuple] = []
        carried: list[tuple] = []
        for b in binders:
            stmt = _binder_stmt(b, parents)
            if _reaches(b, stmt, use, upos, parents):
                before.append(b)
            elif loops and stmt is not None and any(_inside(stmt, lp, parents) for lp in loops):
                carried.append(b)
        if not before and not carried:
            return binders
        if before:
            last = max(before, key=_binder_order)
            stmt = _binder_stmt(last, parents)
            if last[0] == "param" or (stmt is not None and _dominates(stmt, use, parents)):
                if not carried or (stmt is not None and _inside(stmt, loops[0], parents)):
                    return [last]
        return before + carried

    def binder_type(self, b: tuple, s: _Scope, u: _Unit) -> tuple | None:
        k = b[0]
        if k == "expr":
            if _is_empty_container(b[1]):
                return self.filled_type(b[1], b[2].id, u)
            return self.infer(b[1], u)
        if k == "for":
            t = _elem(self.infer(b[1], u))
            for i in b[2]:
                t = _item(t, i)
            return t
        if k == "unpack":
            t = self.infer(b[1], u)
            for i in b[2]:
                t = _item(t, i)
            return t
        if k == "param":
            return self.param_type(b[1], b[2], b[3], s, u)
        if k == "aug":
            if not isinstance(b[2], (ast.BitOr, ast.BitAnd, ast.Sub, ast.Add)):
                return None
            target = b[3]
            ps, prev = _lookup(self.scope_of(target, u), target.id)
            types = [self.infer(b[1], u)]
            if prev and ps is not None:
                types += [self.binder_type_cached(x, ps, u) for x in self.reaching(prev, target, u) if x is not b]
            rs = [t for t in types if t is not None and t[0] == "rs"]
            return _union(rs) if rs else None
        if k == "ann":
            t = self.annotation_type(b[1], u)
            return t if t is not None else self.infer(b[2], u)
        if k == "import":
            return self.sym_type(self.import_sym(b[1], b[2]))
        if k == "def":
            node = b[1]
            if isinstance(node, ast.ClassDef):
                ci = self.f.class_by_node.get(node)
                if ci is not None and ci.model:
                    return ("rs", (ci.model,))
                return ("cls", self.path, node.name)
            return ("func", self.path, node.name)
        if k == "gvar":
            name = b[2].id
            if name == "request" and self.dotted == "odoo.http":
                return REQ
            return OBJ
        if k == "fimport":
            return self.sym_type(self.facts_import_sym(b[1]))
        if k == "fsym":
            sym = b[1]
            return self.sym_type(("sym", self.path, sym[S_NAME], sym[S_KIND], sym[S_LINE], sym[S_COL], sym[S_END_COL]))
        if k == "with":  # with Form(rec) as f / with f.line_ids.new() as line / .edit(0)
            expr = b[1]
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "edit":
                t = self.infer(expr.func.value, u)
            else:
                t = self.infer(expr, u)
            return t if t is not None and t[0] == "rs" else OBJ
        if k in ("cvar", "except"):
            return OBJ
        return None

    def filled_type(self, value: ast.AST, name: str, u: _Unit) -> tuple | None:
        """Type of a variable initialised with an empty container, from what the
        function puts into it (``x.append(rec)``, ``x.add((a, b))``, ``d[k] = rec``)."""
        if isinstance(value, ast.Dict) or _call_name(getattr(value, "func", None)) in ("dict", "defaultdict"):
            stored = [self.infer(v, u) for (n, key), vals in u.stores.items() if n == name and key is None for v in vals]
            if name + "[]" in u.fills:  # d[k].append(x) / d.setdefault(k, []).append(x)
                stored.append(self.filled_type(ast.List(elts=[]), name + "[]", u))
            keys = _homogeneous((self.infer(k, u) for k in u.keys.get(name, ())), strict=True)
            if isinstance(value, ast.Call) and value.args and not stored:  # defaultdict(<factory>)
                t = self.infer(value, u)
                return ("dict", keys, t[2], t[3]) if keys is not None and t is not None and t[0] == "dict" else t
            return ("dict", keys, _homogeneous(stored), None)
        elems = []
        for call in u.fills.get(name, ()):
            meth, args = call.func.attr, call.args  # type: ignore[attr-defined]
            if meth in ("append", "add") and args:
                elems.append(self.infer(args[0], u))
            elif meth == "extend" and args:
                elems.append(_elem(self.infer(args[0], u)))
            elif meth == "insert" and len(args) > 1:
                elems.append(self.infer(args[1], u))
        return ("list", _homogeneous(elems))

    def param_type(self, arg: ast.arg, idx: int, fn: ast.AST, s: _Scope, u: _Unit) -> tuple | None:
        if arg.annotation is not None:
            t = self.annotation_type(arg.annotation, u)
            if t is not None:
                return t
        if isinstance(fn, ast.Lambda):
            return self.lambda_param_type(fn, idx, s, u)
        if idx >= 0:
            ci = self.class_of_function(fn, u)
            if ci is not None:
                decs = {_dotted(d) for d in fn.decorator_list}  # type: ignore[attr-defined]
                if "staticmethod" in decs:
                    idx += 1
                if idx == 0:
                    if ci.model:
                        return ("rs", (ci.model,))
                    if ci.is_model:
                        return None
                    return ("cls" if "classmethod" in decs else "inst", self.path, ci.name)
                if ci.model and VALS_PARAMS.get(fn.name) == idx:  # type: ignore[attr-defined]
                    return ("vals", ci.model)  # create(self, vals_list) / write(self, vals)...
        if arg.arg == "env":
            return ENV  # hooks, migrations, helpers: an Environment by convention
        return None

    def lambda_param_type(self, lam: ast.Lambda, idx: int, s: _Scope, u: _Unit) -> tuple | None:
        if idx != 0:
            return None
        parent = u.parents.get(lam)
        kw = None
        call = None
        if isinstance(parent, ast.keyword):
            kw = parent.arg
            call = u.parents.get(parent)
        elif isinstance(parent, ast.Call):
            call = parent
        if isinstance(call, ast.Call):
            func = call.func
            if isinstance(func, ast.Attribute) and func.attr in LAMBDA_METHODS:
                t = self.infer(func.value, u)
                if t is not None and t[0] == "rs":
                    return t
                if t is not None and t[0] == "super":
                    return ("rs", (t[1].model,)) if t[1].model else None
            name = _call_name(func)
            args = call.args
            if name in ("sorted", "max", "min") and kw == "key" and args:
                return _elem(self.infer(args[0], u))
            if name in ("filter", "map") and len(args) > 1 and args[0] is lam:
                return _elem(self.infer(args[1], u))
            if name == "groupby" and args and (kw == "key" or (len(args) > 1 and args[1] is lam)):
                return _elem(self.infer(args[0], u))
        if s.parent is not None and s.parent.kind == "class" and u.cls is not None and u.cls.model:
            return ("rs", (u.cls.model,))  # field default=/compute=/domain= lambdas
        return None

    def annotation_type(self, ann: ast.AST, u: _Unit, _depth: int = 0) -> tuple | None:
        if _depth > 4:
            return None
        if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
            text = ann.value.strip()
            if _MODEL_RX.fullmatch(text):
                return ("rs", (text,)) if self.has_model(text) else None
            try:
                expr = ast.parse(text, mode="eval").body
            except SyntaxError:
                return None
            if isinstance(expr, ast.Name):
                return self.class_name_type(expr.id, u)
            return self.annotation_type(expr, u, _depth + 1) if not isinstance(expr, ast.Constant) else None
        if isinstance(ann, (ast.Name, ast.Attribute)):
            last = ann.id if isinstance(ann, ast.Name) else ann.attr
            if last == "Self":
                return ("rs", (u.cls.model,)) if u.cls is not None and u.cls.model else None
            t = self.infer(ann, u) if ann in u.parents or ann is u.root else None
            if t is not None and t[0] == "rs":
                return t
            if t is not None and t[0] == "cls":
                return ("inst", t[1], t[2])
            if isinstance(ann, ast.Name) and t is None:
                return self.class_name_type(ann.id, u)
            return None
        if isinstance(ann, ast.Subscript):
            base = _call_name(ann.value)
            inner = ann.slice
            if base == "Optional":
                return self.annotation_type(inner, u, _depth + 1)
            if base in ("list", "List", "Iterable", "Iterator", "Sequence", "set", "Set", "Collection"):
                return ("list", self.annotation_type(inner, u, _depth + 1))
            return None
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            return _union((self.annotation_type(ann.left, u, _depth + 1), self.annotation_type(ann.right, u, _depth + 1)))
        return None

    def class_name_type(self, name: str, u: _Unit) -> tuple | None:
        """A class name in an annotation: a model class (imported or 19+ naming rule)."""
        s, binders = _lookup(u.base, name)
        if binders:
            t = _union([self.binder_type(b, s, u) for b in binders])  # type: ignore[arg-type]
            if t is not None and t[0] in ("rs", "inst"):
                return t
            if t is not None and t[0] == "cls":
                return ("inst", t[1], t[2])
        if (self.major or 0) >= 19 and name[:1].isupper():
            model = _CAMEL.sub(r".\1", name).lower()
            if self.has_model(model):
                return ("rs", (model,))
        return None

    def attr_type(self, node: ast.Attribute, u: _Unit) -> tuple | None:
        attr = node.attr
        if attr == "env":
            rt = self.infer(node.value, u)
            if rt is not None and rt[0] == "mod":
                return self.sym_type(_resolve_symbol(self.reg, rt[1], attr))
            return ENV
        rt = self.infer(node.value, u)
        if rt is None:
            return None
        k = rt[0]
        if k == "rs":
            return self.rs_attr_type(rt[1], attr)
        if k == "env":
            model = ENV_MODELS.get(attr)
            if model and (model != "website" or ((self.major or 0, self.minor) >= (19, 4) and self.has_model(model))):
                return ("rs", (model,))
            if attr == "ref":
                return ("envref",)
            return OBJ
        if k == "req":
            if attr == "website" and self.has_model("website"):
                return ("rs", ("website",))
            return OBJ
        if k == "super":
            ci = rt[1]
            if ci.model:
                return ("method", (ci.model,), attr)
            return OBJ
        if k == "mod":
            return self.sym_type(_resolve_symbol(self.reg, rt[1], attr))
        if k in ("cls", "inst"):
            return self.inst_attr_type(rt[1], rt[2], attr)
        return OBJ

    def inst_attr_type(self, path: str, clsname: str, attr: str) -> tuple | None:
        """``obj.attr`` on an instance / class of a plain python class: methods are
        objects, attributes assigned as ``self.attr = ...`` / ``cls.attr = ...`` in
        its methods (or its bases') have the type of the assigned values."""
        key = (path, clsname, attr)
        if key in self._insts:
            return self._insts[key]
        self._insts[key] = None
        res = _inst_attr_type(self, path, clsname, attr, 0)
        self._insts[key] = res
        return res

    def rs_attr_type(self, models: tuple, attr: str) -> tuple | None:
        if attr == "_origin":
            return ("rs", models)
        if attr in ("ids", "_ids"):
            return ("list", VAL)
        if attr == "_name":
            return ("model_name", models[0]) if len(models) == 1 else VAL
        if attr in RS_SLOTS or attr in ("_context", "_cr", "_uid"):
            return OBJ
        types = []
        method = None
        for m in models:
            kind, defs = self.member(m, attr)
            if kind == "field":
                types.append(self.field_type(m, attr))
            elif kind == "method":
                method = method or ("method", models, attr)
            elif kind == "attr":
                types.append(VAL)
        if types:
            return _union(types) or None
        return method

    def call_type(self, node: ast.Call, u: _Unit) -> tuple | None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "super":
            _fn, ci = self.enclosing_method(node, u)
            return ("super", ci) if ci is not None else None
        if isinstance(func, ast.Attribute):
            rt = self.infer(func.value, u) if func.attr != "env" else None
            if rt is not None:
                k = rt[0]
                meth = func.attr
                if k == "rs":
                    return self.rs_call_type(rt[1], meth, node, u)
                if k == "super":
                    ci = rt[1]
                    if ci.model:
                        return self.rs_call_type((ci.model,), meth, node, u)
                    return None
                if k == "env":
                    if meth == "ref" and node.args:
                        raw = _str_const(node.args[0])
                        model = self.xmlid_model(raw) if raw else None
                        return ("rs", (model,)) if model else None
                    return OBJ
                if k in ("dict", "vals", "list", "tuple"):
                    return self.container_call_type(rt, meth, node, u)
                if k == "mod":
                    sym = _resolve_symbol(self.reg, rt[1], meth)
                    return self.special_call(meth, node, u) or self.sym_call_type(sym)
                if k in ("inst", "cls"):
                    # helpers of addon classes (tests, controllers) often return records
                    return None if self.reg.module_of_path(rt[1]) else OBJ
                if k in ("val", "obj", "builtin", "model_name", "envref", "req", "func"):
                    return OBJ
        ft = self.infer(func, u)
        if ft is None:
            return self.special_call(_call_name(func), node, u)
        k = ft[0]
        if k == "method":
            return self.rs_call_type(ft[1], ft[2], node, u)
        if k == "envref":
            raw = _str_const(node.args[0]) if node.args else None
            model = self.xmlid_model(raw) if raw else None
            return ("rs", (model,)) if model else None
        if k == "env":
            return ENV
        if k == "builtin":
            return self.builtin_call(ft[1], node, u)
        if k in ("cls", "func"):
            if k == "cls" and ft[2] == "Form" and node.args and ft[1].replace("\\", "/").endswith(("odoo/tests/form.py", "odoo/tests/common.py")):
                t = self.infer(node.args[0], u)  # a form proxies the fields of its record
                return t if t is not None and t[0] == "rs" else None
            special = self.special_call(_call_name(func), node, u)
            if special is not None:
                return special
            if k == "cls":
                if ft[2] == "Environment":
                    return ENV
                return ("inst", ft[1], ft[2])
            return None if self.reg.module_of_path(ft[1]) else OBJ
        if k == "rs":
            return None  # a model class called (rare)
        return OBJ

    def special_call(self, name: str | None, node: ast.Call, u: _Unit) -> tuple | None:
        """Library helpers with a known result shape."""
        args = node.args
        if name == "defaultdict":
            if args and isinstance(args[0], ast.Lambda):
                return ("dict", None, self.infer(args[0].body, u), None)
            return ("dict", None, ("list", None) if args and isinstance(args[0], ast.Name) and args[0].id == "list" else None, None)
        if name == "groupby" and args:
            return ("list", ("tuple", (None, ("list", _elem(self.infer(args[0], u))))))
        if name in ("OrderedSet", "unique") and args:
            return ("list", _elem(self.infer(args[0], u)))
        if name == "Environment":
            return ENV
        return None

    def sym_call_type(self, sym: tuple | None) -> tuple | None:
        if sym is None:
            return OBJ
        if sym[0] == "sym":
            if sym[3] == "class":
                if sym[2] == "Environment":
                    return ENV
                return ("inst", sym[1], sym[2])
            if sym[3] == "def":
                return None if self.reg.module_of_path(sym[1]) else OBJ
        return OBJ

    def builtin_call(self, name: str, node: ast.Call, u: _Unit) -> tuple | None:
        args = node.args
        if name in ("sorted", "reversed", "list", "tuple", "set", "frozenset", "iter", "filter"):
            if not args:
                return ("list", None)
            src = args[1] if name == "filter" and len(args) > 1 else args[0]
            return ("list", _elem(self.infer(src, u)))
        if name in ("max", "min", "next") and args:
            if name == "next" or len(args) == 1:
                return _elem(self.infer(args[0], u))
            return None
        if name == "zip":
            return ("list", ("tuple", tuple(_elem(self.infer(a, u)) for a in args)))
        if name == "enumerate" and args:
            return ("list", ("tuple", (VAL, _elem(self.infer(args[0], u)))))
        if name == "dict":
            return ("dict", None, None, None)
        if name == "getattr" and len(args) >= 2:
            attr = _str_const(args[1])
            rt = self.infer(args[0], u)
            if attr and rt is not None and rt[0] == "rs":
                return self.rs_attr_type(rt[1], attr)
            return None
        if name in ("super", "type", "vars", "object", "property", "classmethod", "staticmethod"):
            return OBJ
        return OBJ if name != "map" else ("list", None)

    def container_call_type(self, rt: tuple, meth: str, node: ast.Call, u: _Unit) -> tuple | None:
        k = rt[0]
        if k == "dict":
            if meth == "items":
                return ("list", ("tuple", (rt[1], rt[2])))
            if meth == "values":
                return ("list", rt[2])
            if meth == "keys":
                return ("list", rt[1])
            if meth in ("get", "pop", "setdefault"):
                key = _str_const(node.args[0]) if node.args else None
                if key is not None and rt[3] is not None:
                    return _entry(rt, key)
                return rt[2]
            if meth == "copy":
                return rt
            return OBJ
        if k == "vals":
            if meth == "copy":
                return rt
            if meth in ("items",):
                return ("list", ("tuple", (VAL, None)))
            return None if meth in ("get", "pop", "setdefault") else OBJ
        if k == "list":
            if meth == "pop":
                return rt[1]
            if meth == "copy":
                return rt
            return OBJ
        return OBJ

    def rs_call_type(self, models: tuple, meth: str, node: ast.Call, u: _Unit) -> tuple | None:
        if meth in PRESERVING:
            return ("rs", models)
        args = node.args
        if meth == "mapped" and args:
            path = _str_const(args[0])
            if path:
                return self.path_type(models, path)
            if isinstance(args[0], ast.Lambda):
                body = self.infer(args[0].body, u)
                return body if body is not None and body[0] == "rs" else None
            return None
        if meth == "grouped" and args:
            path = _str_const(args[0])
            key = self.path_type(models, path) if path else None
            if key is not None and key[0] == "list":
                key = key[1]
            return ("dict", key, ("rs", models), None)
        if meth in ("_read_group", "read_group"):
            return self.read_group_type(models, meth, node)
        if meth in ("read", "search_read", "web_search_read", "formatted_read_group", "web_read_group"):
            return ("list", OBJ)  # dicts
        if meth in ("search_count", "name_get", "fields_get", "check_access", "has_access"):
            return OBJ
        if meth == "default_get":
            return ("vals", models[0])
        if meth == "copy_data":
            return ("list", ("vals", models[0]))
        return self.returns_type(models, meth)

    def read_group_type(self, models: tuple, meth: str, node: ast.Call) -> tuple | None:
        major = self.major or 0
        if major < 17 or (meth == "read_group" and major < 20):
            return ("list", OBJ)  # list of dicts (old API)
        model = models[0]

        def specs(pos: int, kw: str) -> list | None:
            arg = node.args[pos] if len(node.args) > pos else next((k.value for k in node.keywords if k.arg == kw), None)
            if arg is None:
                return []
            if isinstance(arg, (ast.List, ast.Tuple)):
                out = [_str_const(e) for e in arg.elts]
                return out if all(out) else None
            return None

        groupby = specs(1, "groupby")
        aggregates = specs(2, "aggregates")
        if groupby is None or aggregates is None:
            return ("list", None)
        items: list = []
        for spec in groupby:
            fname, _, gran = spec.partition(":")
            t = None if gran else self.path_type(models, fname)
            items.append(t if t is not None and t[0] == "rs" else VAL)
        for spec in aggregates:
            fname, _, agg = spec.partition(":")
            if agg == "recordset":
                items.append(("rs", (model,)) if fname == "id" else self.path_type(models, fname))
            else:
                items.append(VAL)
        return ("list", ("tuple", tuple(items)))

    def returns_type(self, models: tuple, meth: str) -> tuple | None:
        """Return type of a method: its ``-> ...`` annotation, else the types of
        the ``return`` statements of its most derived definition(s)."""
        out: list = []
        for m in models:
            kind, defs = self.member(m, meth)
            if kind != "method":
                continue
            t = None
            for d in defs:
                ret = d.facts[M_RETURNS] if len(d.facts) > M_RETURNS else None
                if ret:
                    t = self.returns_text_type(ret, m, d.path)
                    break
            if t is None:
                t = self.body_returns(m, meth, defs)
            if t is not None:
                out.append(t)
        return _union(out)

    def body_returns(self, model: str, meth: str, defs: list) -> tuple | None:
        key = (self.scope, model, meth)
        if key in _RET_CACHE:
            return _RET_CACHE[key]
        if _CROSS[0] > 2:
            return None
        if len(_RET_CACHE) > 20000:
            _RET_CACHE.clear()
        _RET_CACHE[key] = None  # recursion guard (super() chains, mutual calls)
        res = None
        _CROSS[0] += 1
        try:
            for d in defs[:6]:
                t = _def_returns(self, d)
                if t is not None:
                    src = d.model or "base"  # framework classes are analysed as ``base``
                    if src != model:
                        t = _subst(t, src, model)  # e.g. ``return self`` in a mixin
                    res = t
                    break
        finally:
            _CROSS[0] -= 1
        _RET_CACHE[key] = res
        return res

    def returns_text_type(self, text: str, model: str, path: str) -> tuple | None:
        text = text.strip()
        if text in ("Self", "typing.Self", "Self | None"):
            return ("rs", (model,))
        unq = text.strip("'\"")
        if _MODEL_RX.fullmatch(unq):
            return ("rs", (unq,)) if self.has_model(unq) else None
        m = re.fullmatch(r"(?:list|Iterator|Iterable|Sequence)\[(?:typing\.)?Self\]", text)
        if m:
            return ("list", ("rs", (model,)))
        if re.fullmatch(r"(?:[A-Za-z_]\w*\.)*[A-Z]\w*", unq):
            sym = _resolve_in_file(self.reg, path, unq) if self.reg.py_dotted_name(path) else None
            if sym is not None and sym[0] == "sym" and sym[3] == "class":
                cls = _class_facts(self.reg, sym[1], sym[2])
                if cls is not None and cls[K_MODEL]:
                    name = model_class_info(cls, self.major)[0]
                    if name:
                        return ("rs", (name,))
            if (self.major or 0) >= 19:
                derived = _CAMEL.sub(r".\1", unq.rsplit(".", 1)[-1]).lower()
                if self.has_model(derived):
                    return ("rs", (derived,))
        return None

    def subscript_type(self, node: ast.Subscript, u: _Unit) -> tuple | None:
        vt = self.infer(node.value, u)
        if vt is None:
            return None
        k = vt[0]
        sl = node.slice
        if k == "env":
            name = _str_const(sl)
            if name:
                return ("rs", (name,))
            st = self.infer(sl, u)
            if st is not None and st[0] == "model_name":
                return ("rs", (st[1],))
            return None
        if k == "rs":
            name = _str_const(sl)
            if name is not None:
                types = [self.field_type(m, name) for m in vt[1]]
                return _union(types)
            if isinstance(sl, (ast.Constant, ast.Slice, ast.UnaryOp)):
                return vt
            st = self.infer(sl, u)
            if st is None:
                # records[i] keeps the model, records[fname] is a field value
                dyn = _dotted(sl) or ""
                return None if any(w in dyn.lower() for w in ("field", "fname", "name", "key", "attr", "col")) else vt
            return vt if st[0] == "val" and st[1] in ("int", "bool") else None
        if k == "list":
            return vt if isinstance(sl, ast.Slice) else vt[1]
        if k == "tuple":
            if isinstance(sl, ast.Constant) and isinstance(sl.value, int):
                return _item(vt, sl.value)
            return None
        if k == "dict":
            key = _str_const(sl)
            if key is not None:
                types = []
                if vt[3] is not None:
                    types.append(_entry(vt, key))
                if isinstance(node.value, ast.Name):
                    types.extend(self.infer(v, u) for v in u.stores.get((node.value.id, key), ()))
                if types or vt[3] is not None:
                    return _union(types)
            return vt[2]
        if k == "vals":
            return None
        if k == "fields":
            return OBJ
        return OBJ

    # -- python symbols ---------------------------------------------------------

    def import_sym(self, alias: ast.alias, stmt: ast.AST) -> tuple | None:
        reg = self.reg
        if isinstance(stmt, ast.Import):
            if alias.asname:
                return _module_sym(reg, alias.name)
            return _module_sym(reg, alias.name.split(".")[0])
        module = self.abs_module(stmt.module or "", stmt.level or 0)  # type: ignore[attr-defined]
        if module is None:
            return None
        if reg.py_module_file(f"{module}.{alias.name}") and not _defines(reg, module, alias.name):
            return _module_sym(reg, f"{module}.{alias.name}")
        return _resolve_symbol(reg, module, alias.name)

    def facts_import_sym(self, imp: tuple) -> tuple | None:
        """``import_sym`` for an Import tuple of the facts."""
        reg = self.reg
        if imp[I_NAME] is None:
            return _module_sym(reg, imp[I_MODULE] if imp[I_ASNAME] else imp[I_MODULE].split(".")[0])
        module = self.abs_module(imp[I_MODULE], imp[I_LEVEL])
        if module is None:
            return None
        if reg.py_module_file(f"{module}.{imp[I_NAME]}") and not _defines(reg, module, imp[I_NAME]):
            return _module_sym(reg, f"{module}.{imp[I_NAME]}")
        return _resolve_symbol(reg, module, imp[I_NAME])

    def abs_module(self, module: str, level: int) -> str | None:
        if not level:
            return module
        dotted = self.dotted
        if not dotted:
            return None
        pkg = dotted if self.path.endswith("__init__.py") else dotted.rpartition(".")[0]
        for _ in range(level - 1):
            pkg = pkg.rpartition(".")[0]
        if not pkg:
            return None
        return f"{pkg}.{module}" if module else pkg

    def sym_type(self, sym: tuple | None) -> tuple | None:
        if sym is None:
            return OBJ
        if sym[0] == "module":
            return ("mod", sym[1])
        kind = sym[3]
        if kind == "class":
            cls = _class_facts(self.reg, sym[1], sym[2])
            if cls is not None and cls[K_MODEL]:
                model = model_class_info(cls, self.major)[0]
                if model:
                    return ("rs", (model,))
            return ("cls", sym[1], sym[2])
        if kind == "def":
            return ("func", sym[1], sym[2])
        if sym[2] == "request" and self.reg.py_dotted_name(sym[1]) == "odoo.http":
            return REQ
        return OBJ

    # -- strings with an Odoo meaning ------------------------------------------------

    def classify_string(self, node: ast.Constant, u: _Unit) -> tuple | None:
        """Odoo role of a str literal in code: (mode, models, role) where mode is
        "path" (dotted field path), "name" (field name, maybe ``f:agg``),
        "order" (``"f desc, g"``), "key" (field-values key), "default"
        (``default_<field>`` context key), "model" or "xmlid"; models is the
        tuple of candidate base models (None when unknown)."""
        parents = u.parents
        p = parents.get(node)
        if p is None:
            return None
        if isinstance(p, ast.Dict):
            if not any(k is node for k in p.keys):
                return None
            models = self.vals_models(p, u)
            if models:
                return ("key", models, "vals")
            if node.value.startswith("default_"):
                models = self.context_models(p, u)
                if models:
                    return ("default", models, "context")
            return None
        if isinstance(p, ast.keyword):
            call = parents.get(p)
            return self.call_string(call, node, None, p.arg, False, u) if isinstance(call, ast.Call) else None
        if isinstance(p, ast.Call):
            if node in p.args:
                return self.call_string(p, node, p.args.index(node), None, False, u)
            return None
        if isinstance(p, (ast.List, ast.Tuple)):
            elts = p.elts
            if len(elts) == 3 and elts[0] is node:
                op = elts[1]
                if isinstance(op, ast.Constant):
                    leaf = isinstance(op.value, str) and op.value.lower() in LEAF_OPERATORS and not _is_leaf_value(p, parents)
                else:  # ``(path, operator, value)`` in a ``_search_x`` method
                    leaf = isinstance(op, (ast.Name, ast.Attribute, ast.Subscript, ast.Call))
                if leaf:
                    models = self.domain_models(p, u)
                    if models is INDEXED or (models is None and not isinstance(op, ast.Constant)):
                        return None
                    return ("path", models, "domain")
            if node.value in ("&", "|", "!"):
                return None
            pp = parents.get(p)
            if isinstance(pp, ast.keyword):
                call = parents.get(pp)
                if isinstance(call, ast.Call):
                    return self.call_string(call, node, None, pp.arg, True, u)
            elif isinstance(pp, ast.Call) and p in pp.args:
                return self.call_string(pp, node, pp.args.index(p), None, True, u)
            return None
        if isinstance(p, ast.Subscript) and p.slice is node:
            vt = self.infer(p.value, u)
            if vt is None:
                return None
            if vt[0] == "env":
                return ("model", None, "env")
            if vt[0] == "rs":
                return ("name", vt[1], "subscript")
            if vt[0] == "vals":
                return ("key", (vt[1],), "vals")
            if vt[0] == "dict" and isinstance(p.value, ast.Name):  # res['x'] = ...; create(res)
                models = self.var_vals_models(p.value, u, 0)
                if models:
                    return ("key", models, "vals")
            if isinstance(p.value, ast.Attribute) and p.value.attr == "_fields":
                owner = self.infer(p.value.value, u)
                if owner is not None and owner[0] == "rs":
                    return ("name", owner[1], "_fields")
            return None
        if isinstance(p, ast.Compare) and p.left is node and len(p.ops) == 1 and isinstance(p.ops[0], (ast.In, ast.NotIn)):
            vt = self.infer(p.comparators[0], u)
            if vt is not None and vt[0] == "vals":
                return ("key", (vt[1],), "vals")
        return None

    def call_string(self, call: ast.Call, node: ast.AST, idx: int | None, kw: str | None, in_list: bool, u: _Unit) -> tuple | None:
        func = call.func
        name = _call_name(func)
        if name == "Domain" and idx == 0 and not in_list:
            models = self.domain_models(call, u)
            return None if models is INDEXED else ("path", models, "domain")
        if not isinstance(func, ast.Attribute):
            if name == "getattr" and idx == 1 and call.args:
                t = self.infer(call.args[0], u)
                if t is not None and t[0] == "rs":
                    return ("name", t[1], "getattr")
            return None
        meth = func.attr
        rt = self.infer(func.value, u)
        if rt is not None and rt[0] == "super":
            rt = ("rs", (rt[1].model,)) if rt[1].model else None
        rk = rt[0] if rt is not None else None
        if rk == "env":
            if meth == "ref" and idx == 0 and not in_list:
                return ("xmlid", None, "ref")
            return None
        if rk == "vals":
            if meth in ("get", "pop", "setdefault") and idx == 0 and not in_list:
                return ("key", (rt[1],), "vals")  # type: ignore[index]
            return None
        if rk is not None and rk != "rs":
            return None
        models = rt[1] if rt is not None else None
        if meth in PATH_METHODS and not in_list and (idx == 0 or (meth == "sorted" and kw == "key")):
            return ("order" if meth == "sorted" else "path", models, meth)
        spec = FIELD_ARGS.get(meth)
        if spec is not None and ((idx is not None and idx in spec[0]) or (kw is not None and kw in spec[1])):
            return ("name", models, meth)
        if kw in ORDER_KWARGS and not in_list and (meth in DOMAIN_ARGS or meth in FIELD_ARGS):
            return ("order", models, meth)
        return None

    # -- domains / values / contexts: from a literal to the model consuming it -------

    def domain_models(self, node: ast.AST, u: _Unit, depth: int = 0) -> tuple | None:
        """Models a domain literal (or ``Domain(...)``) is applied to, found by
        following it to its consumer (search & co, a variable passed to one,
        an action dict with ``res_model``, a field ``domain=``, a nested
        ``any`` leaf...)."""
        if depth > 3:
            return None
        parents = u.parents
        cur = node
        while True:
            p = parents.get(cur)
            if p is None:
                return None
            if isinstance(p, (ast.List, ast.Tuple)):
                elts = p.elts
                if len(elts) == 3 and elts[2] is cur and isinstance(elts[1], ast.Constant) and elts[1].value in ("any", "not any", "any!", "not any!"):
                    path = _str_const(elts[0])
                    outer = self.domain_models(p, u, depth + 1)
                    if not path or not outer or outer is INDEXED:
                        return outer if outer is INDEXED else None
                    t = self.path_type(outer, path)
                    return t[1] if t is not None and t[0] == "rs" else None
                cur = p
                continue
            if isinstance(p, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.IfExp, ast.Starred)):
                cur = p
                continue
            if isinstance(p, ast.keyword):
                call = parents.get(p)
                if not isinstance(call, ast.Call):
                    return None
                if p.arg in DOMAIN_KWARGS:
                    # only ORM domain consumers filter their receiver: a ``domain=``
                    # of another method (``_stage_find(domain=...)``) may target any model
                    if _call_name(call.func) in DOMAIN_ARGS:
                        return self.consumer_models(call, u)
                    if p.arg == "domain" and _field_class(call, self.scope_of(call, u)):
                        return INDEXED
                return None
            if isinstance(p, ast.Call):
                name = _call_name(p.func)
                if name in DOMAIN_WRAPPERS:
                    cur = p
                    continue
                if isinstance(p.func, ast.Attribute) and name in ("append", "extend", "insert", "__iadd__") and isinstance(p.func.value, ast.Name):
                    return self.var_domain_models(p.func.value, u, depth)
                if isinstance(p.func, ast.Attribute) and name in DOMAIN_ARGS and cur in p.args and p.args.index(cur) == DOMAIN_ARGS[name]:
                    return self.consumer_models(p, u)
                return None
            if isinstance(p, ast.Dict):
                for k, v in zip(p.keys, p.values):
                    if v is cur and _str_const(k) == "domain":
                        return self.action_model(p)
                return None
            if isinstance(p, ast.Lambda):
                lp = parents.get(p)
                if isinstance(lp, ast.keyword) and lp.arg == "domain":
                    call = parents.get(lp)
                    ftype = _field_class(call, self.scope_of(call, u)) if isinstance(call, ast.Call) else None
                    if ftype:
                        cm = _field_comodel(call, ftype)
                        return (cm,) if cm else None
                return None
            if isinstance(p, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets = p.targets if isinstance(p, ast.Assign) else [p.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        return self.var_domain_models(t, u, depth)
                    if isinstance(t, ast.Subscript) and _str_const(t.slice) == "domain":
                        return None
                return None
            if isinstance(p, ast.Return):
                return self.search_method_models(u)
            return None

    def var_domain_models(self, name: ast.Name, u: _Unit, depth: int) -> tuple | None:
        out: list[str] = []
        for use in self.uses_of(name, u):
            if isinstance(u.parents.get(use), ast.Return):
                models = self.search_method_models(u)
            else:
                models = self.domain_models(use, u, depth + 1)
            if models and models is not INDEXED:
                out.extend(models)
        return tuple(dict.fromkeys(out)) or None

    def search_method_models(self, u: _Unit) -> tuple | None:
        """The model of the unit when its method is a field ``search=`` method
        (it returns a domain on its own model)."""
        fn, ci = u.root, u.cls
        if not isinstance(fn, _FUNC) or ci is None or not ci.model:
            return None
        model = ci.model
        names = self._search_methods.get(model)
        if names is None:
            names = set()
            for defs in self.reg.fields(model, self.scope).values():
                for d in defs:
                    search = (d.facts[F_ARGS] or {}).get("search")
                    if isinstance(search, str):
                        names.add(search)
            names = self._search_methods[model] = frozenset(names)
        return (model,) if fn.name in names or fn.name.startswith("_search_") else None

    def consumer_models(self, call: ast.Call, u: _Unit) -> tuple | None:
        func = call.func
        if not isinstance(func, ast.Attribute):
            return None
        t = self.infer(func.value, u)
        if t is not None and t[0] == "super":
            return (t[1].model,) if t[1].model else None
        return t[1] if t is not None and t[0] == "rs" else None

    def action_model(self, d: ast.Dict) -> tuple | None:
        for k, v in zip(d.keys, d.values):
            if _str_const(k) == "res_model":
                model = _str_const(v)
                return (model,) if model else None
        return None

    def uses_of(self, name: ast.Name, u: _Unit) -> list[ast.Name]:
        """Other loads of the variable bound by ``name`` in the same unit."""
        s = _lookup(self.scope_of(name, u), name.id)[0]
        return [n for n in u.loads.get(name.id, ()) if n is not name and _lookup(self.scope_of(n, u), n.id)[0] is s]

    def vals_models(self, d: ast.Dict, u: _Unit, depth: int = 0) -> tuple | None:
        """Models a dict literal holds field values of (create/write/... argument,
        ``Command.create`` / ``(0, 0, vals)`` of an x2many key, a variable passed
        to one of those, ``vals.update({...})``)."""
        if depth > 3:
            return None
        parents = u.parents
        cur: ast.AST = d
        while True:
            p = parents.get(cur)
            if p is None:
                return None
            if isinstance(p, ast.List) or (isinstance(p, ast.Tuple) and not _is_command_tuple(p)):
                cur = p
                continue
            if isinstance(p, ast.Tuple):  # (0, 0, vals) / (1, id, vals)
                if p.elts[2] is not cur:
                    return None
                return self.x2many_models(p, u, depth)
            if isinstance(p, (ast.Starred, ast.IfExp, ast.BoolOp)) or _is_comp_elt(p, cur) or _is_seq_call(p, cur):
                cur = p
                continue
            if isinstance(p, ast.keyword):
                call = parents.get(p)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                    spec = VALS_ARGS.get(call.func.attr)
                    if spec and p.arg in spec[1]:
                        return self.consumer_models(call, u) or self.vals_receiver(call, u)
                return None
            if isinstance(p, ast.Call):
                func = p.func
                name = _call_name(func)
                if isinstance(func, ast.Attribute):
                    if name in COMMAND_VALS and _dotted(func.value) in ("Command", "fields.Command", "odoo.Command"):
                        if p.args and len(p.args) > COMMAND_VALS[name] and p.args[COMMAND_VALS[name]] is cur:
                            return self.x2many_models(p, u, depth)
                        return None
                    spec = VALS_ARGS.get(name)  # type: ignore[arg-type]
                    if spec and cur in p.args and p.args.index(cur) == spec[0]:
                        return self.consumer_models(p, u) or self.vals_receiver(p, u)
                    if name == "append" and isinstance(func.value, ast.Name):
                        return self.var_vals_models(func.value, u, depth)
                return None
            if isinstance(p, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = p.targets if isinstance(p, ast.Assign) else [p.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        return self.var_vals_models(t, u, depth)
                return None
            return None

    def vals_receiver(self, call: ast.Call, u: _Unit) -> tuple | None:
        """``vals.update({...})`` on a field-values dict."""
        if isinstance(call.func, ast.Attribute) and call.func.attr == "update":
            t = self.infer(call.func.value, u)
            if t is not None and t[0] == "vals":
                return (t[1],)
        return None

    def var_vals_models(self, name: ast.Name, u: _Unit, depth: int) -> tuple | None:
        out: list[str] = []
        for use in self.uses_of(name, u):
            models = self.vals_models_of_use(use, u, depth + 1)
            if models:
                out.extend(models)
        return tuple(dict.fromkeys(out)) or None

    def vals_models_of_use(self, use: ast.Name, u: _Unit, depth: int) -> tuple | None:
        p = u.parents.get(use)
        if isinstance(p, ast.Call) or isinstance(p, ast.keyword) or isinstance(p, (ast.List, ast.Tuple, ast.Starred)):
            return self.vals_models(use, u, depth)  # type: ignore[arg-type]
        if isinstance(p, ast.Attribute) and p.attr == "update":
            return None
        return None

    def x2many_models(self, cmd: ast.AST, u: _Unit, depth: int) -> tuple | None:
        """Comodels of the x2many key whose value holds the command ``cmd``."""
        parents = u.parents
        cur = cmd
        while True:
            p = parents.get(cur)
            if p is None:
                return None
            if isinstance(p, (ast.List, ast.Tuple, ast.Starred, ast.BinOp)) and not (isinstance(p, ast.Tuple) and _is_command_tuple(p)):
                cur = p
                continue
            if isinstance(p, (ast.IfExp, ast.BoolOp)) or _is_comp_elt(p, cur) or _is_seq_call(p, cur):
                cur = p
                continue
            if isinstance(p, ast.Dict):
                for k, v in zip(p.keys, p.values):
                    if v is cur:
                        key = _str_const(k)
                        outer = self.vals_models(p, u, depth + 1)
                        if not key or not outer:
                            return None
                        out = [self.comodel(m, key) for m in outer]
                        return tuple(dict.fromkeys(m for m in out if m)) or None
                return None
            if isinstance(p, ast.Call) and _call_name(p.func) == "append" and isinstance(p.func, ast.Attribute) and isinstance(p.func.value, ast.Name):
                return self.var_x2many_models(p.func.value, u, depth)
            if isinstance(p, (ast.Assign, ast.AugAssign)):
                targets = p.targets if isinstance(p, ast.Assign) else [p.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        return self.var_x2many_models(t, u, depth)
                    if isinstance(t, ast.Subscript):
                        key = _str_const(t.slice)
                        vt = self.infer(t.value, u)
                        if key and vt is not None and vt[0] == "vals":
                            cm = self.comodel(vt[1], key)
                            return (cm,) if cm else None
                    if isinstance(t, ast.Attribute):  # rec.line_ids = [Command.create({...})]
                        vt = self.infer(t.value, u)
                        if vt is not None and vt[0] == "rs":
                            out = [self.comodel(m, t.attr) for m in vt[1]]
                            return tuple(dict.fromkeys(m for m in out if m)) or None
                return None
            return None

    def var_x2many_models(self, name: ast.Name, u: _Unit, depth: int) -> tuple | None:
        if depth > 3:
            return None
        out: list[str] = []
        for use in self.uses_of(name, u):
            models = self.x2many_models(use, u, depth + 1)
            if models:
                out.extend(models)
        return tuple(dict.fromkeys(out)) or None

    def context_models(self, d: ast.AST, u: _Unit, depth: int = 0) -> tuple | None:
        """Models a context dict applies ``default_*`` keys to: ``with_context({...})``
        receivers, the ``res_model`` of an action dict holding it as ``context``."""
        parents = u.parents
        p = parents.get(d)
        if isinstance(p, ast.Call) and isinstance(p.func, ast.Attribute) and p.func.attr == "with_context" and d in p.args:
            return self.consumer_models(p, u)
        if isinstance(p, ast.Dict):
            for k, v in zip(p.keys, p.values):
                if v is d and _str_const(k) == "context":
                    return self.action_model(p)
        if isinstance(p, ast.keyword) and p.arg is None:  # with_context(**{...})
            call = parents.get(p)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "with_context":
                return self.consumer_models(call, u)
        if isinstance(p, ast.Assign) and depth < 2:  # ctx = {...}; ...with_context(ctx) / 'context': ctx
            out: list[str] = []
            for t in p.targets:
                if isinstance(t, ast.Name):
                    for use in self.uses_of(t, u):
                        out.extend(self.context_models(use, u, depth + 1) or ())  # type: ignore[arg-type]
            return tuple(dict.fromkeys(out)) or None
        return None

    # -- unknown receivers ---------------------------------------------------------------

    def candidates(self, attr: str, node: ast.Attribute | None, u: _Unit | None) -> tuple[list[str], str]:
        """Models that may own member ``attr`` of a receiver of unknown type:
        visible models declaring it, narrowed by the other members accessed on
        the same variable (duck typing), collapsed to the most general ones."""
        if attr in NON_MODEL_ATTRS or attr.startswith("__") or attr in MAGIC_FIELDS or attr in RS_SLOTS:
            return [], ""
        if self.scope is None:
            return [], ""  # framework / standalone code: model-agnostic, nothing to guess from
        reg = self.reg
        declared = reg.models_with_member(attr)
        if not declared or "" in declared:
            return [], ""  # a BaseModel member exists on every model: no basis to pick one
        cands = [m for m in declared if reg.has_model(m, self.scope)]
        if not cands or len(cands) > MAX_DECLARING:
            return [], ""
        how = "member-name"
        if node is not None and u is not None and isinstance(node.value, ast.Name) and len(cands) > 1:
            others = self.sibling_attrs(node, u)
            if others:
                owners = [set(reg.models_with_member(a)) for a in others]
                narrowed = [m for m in cands if all(o.intersection(self.lineage(m)) for o in owners)]
                if narrowed:
                    cands = narrowed
                    how = "duck-typing"
        if len(cands) > 1:
            cands = [m for m in cands if not any(o != m and o in self.lineage(m) for o in cands)]
        return cands, how

    def lineage(self, model: str) -> set[str]:
        """``model`` and its ancestors (``_inherit`` parents, ``_inherits``
        parents, recursively) from the class declarations only - cheap, unlike
        a full MRO."""
        hit = self._lineages.get(model)
        if hit is not None:
            return hit
        out = {model}
        stack = [model]
        reg = self.reg
        while stack:
            m = stack.pop()
            for c in reg.model_classes(m, None):
                info = c.model_info
                for p in list(info.get(MI_INHERIT) or ()) + list((info.get(MI_INHERITS) or {}).keys()):
                    if p not in out:
                        out.add(p)
                        stack.append(p)
        self._lineages[model] = out
        return out

    def sibling_attrs(self, node: ast.Attribute, u: _Unit) -> set[str]:
        """Model-specific attributes accessed on the same variable as ``node`` in the unit."""
        name = node.value.id  # type: ignore[attr-defined]
        target_scope = _lookup(self.scope_of(node.value, u), name)[0]
        out = set()
        for a in u.attrs:
            if a is node or a.attr == node.attr or not isinstance(a.value, ast.Name) or a.value.id != name:
                continue
            if a.attr in NON_MODEL_ATTRS or a.attr in RS_SLOTS:
                continue
            if _lookup(self.scope_of(a.value, u), name)[0] is not target_scope:
                continue
            owners = self.reg.models_with_member(a.attr)
            if owners and "" not in owners:
                out.add(a.attr)
        return out

    def rank(self, models: list[str], u: _Unit | None) -> list[str]:
        """Order candidate models by relatedness to the file (best first)."""
        cur = u.cls.model if u is not None and u.cls is not None else None
        if self._rank_info is None or self._rank_info[0] != cur:
            comodels: set[str] = set()
            if cur:
                for defs in self.reg.fields(cur, self.scope).values():
                    cm = defs[0].facts[F_COMODEL] if defs else None
                    if cm:
                        comodels.add(cm)
            referenced = set()
            facts = self.reg.file_facts(self.path) or {}
            for ref in facts.get(REFS) or ():
                if ref[R_KIND] == REF_MODEL:
                    referenced.add(ref[R_VALUE])
            self._rank_info = (cur, comodels, referenced)
        _cur, comodels, referenced = self._rank_info

        def score(m: str) -> tuple:
            if m == cur:
                s = 0
            elif cur and (m in self.lineage(cur) or cur in self.lineage(m)):
                s = 1
            elif m in comodels:
                s = 2
            elif m in referenced:
                s = 3
            else:
                s = 4
            return (s, m)

        return sorted(models, key=score)

    # -- targets ------------------------------------------------------------------

    def member_targets(self, models: tuple, attr: str, is_call: bool, origin: tuple, extra: dict, u: _Unit | None) -> list[Target]:
        """FIELD / METHOD / ATTR target(s) for ``attr`` on recordsets of ``models``."""
        found: list[tuple[str, str]] = []
        for m in models:
            kind, _defs = self.member(m, attr)
            if kind:
                found.append((m, kind))
        if found:
            kinds = [k for _m, k in found]
            kind = "method" if is_call and "method" in kinds else kinds[0]
            tkind = {"field": FIELD, "method": METHOD, "attr": ATTR}[kind]
            owners = [m for m, k in found if k == kind]
            if len(owners) == 1:
                return [Target(tkind, model=owners[0], name=attr, origin=origin, extra=dict(extra, inferred=True))]
            return [Target(tkind, model=None, models=owners, name=attr, origin=origin, extra=dict(extra, inferred=True))]
        if attr in NON_MODEL_ATTRS:
            return []  # e.g. a list / set variable also assigned a recordset elsewhere
        # not declared on the model: an abstract mixin using members of its inheritors?
        heirs = [c for c in self.candidates(attr, None, None)[0] if any(m in self.lineage(c) for m in models)]
        tkind = METHOD if is_call else FIELD
        if 1 <= len(heirs) <= MAX_CANDIDATES:
            heirs = self.rank(heirs, u)
            if len(heirs) == 1:
                return [Target(tkind, model=heirs[0], name=attr, origin=origin, extra=dict(extra, inferred=False, via=models[0]))]
            return [Target(tkind, model=None, models=heirs, name=attr, origin=origin, extra=dict(extra, inferred=False, via=models[0]))]
        return [Target(tkind, model=models[0], name=attr, origin=origin, extra=dict(extra, inferred=True, missing=True))]

    def fallback_targets(self, attr: str, is_call: bool, origin: tuple, extra: dict, node: ast.Attribute | None, u: _Unit | None) -> list[Target]:
        cands, how = self.candidates(attr, node, u)
        if not cands or len(cands) > MAX_CANDIDATES:
            return []
        cands = self.rank(cands, u)
        kinds = [self.member(m, attr)[0] for m in cands]
        if is_call and "method" in kinds:
            kind = "method"
        elif "field" in kinds:
            kind = "field"
        else:
            kind = next((k for k in kinds if k), "method")
        tkind = {"field": FIELD, "method": METHOD, "attr": ATTR}[kind]
        extra = dict(extra, inferred=False, guess=how)
        if len(cands) == 1:
            return [Target(tkind, model=cands[0], name=attr, origin=origin, extra=extra)]
        return [Target(tkind, model=None, models=cands, name=attr, origin=origin, extra=extra)]

    def sym_targets(self, sym: tuple | None, origin: tuple, extra: dict | None = None) -> list[Target]:
        if sym is None:
            return []
        if sym[0] == "module":
            if not sym[2]:
                return []
            return [Target(FILE, path=sym[2], name=sym[1], origin=origin, extra=dict(extra or {}))]
        _k, path, name, kind, line, col, end_col = sym
        ex = dict(extra or {}, symbol_kind=kind)
        if kind == "class":
            cls = _class_facts(self.reg, path, name)
            if cls is not None and cls[K_MODEL]:
                model = model_class_info(cls, self.major)[0]
                if model:
                    ex["model"] = model
        return [Target(PYSYM, name=name, path=path, line=line, col=col, end_col=end_col, origin=origin, extra=ex)]


def _is_empty_container(node: ast.AST) -> bool:
    """``[]``, ``{}``, ``set()``, ``list()``, ``dict()``, ``OrderedSet()``, ``defaultdict(list)``..."""
    if isinstance(node, (ast.List, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name in ("list", "set", "dict", "OrderedSet") and not node.args and not node.keywords:
            return True
        return name == "defaultdict"
    return False


def _is_leaf_value(seq: ast.AST, parents: dict) -> bool:
    """``seq`` is the value of an enclosing domain leaf (``('state', 'in', seq)``)."""
    p = parents.get(seq)
    if not isinstance(p, (ast.List, ast.Tuple)) or len(p.elts) != 3 or p.elts[2] is not seq:
        return False
    op = p.elts[1]
    return isinstance(op, ast.Constant) and isinstance(op.value, str) and op.value.lower() in LEAF_OPERATORS and op.value not in ("any", "not any", "any!", "not any!")


def _is_comp_elt(p: ast.AST, cur: ast.AST) -> bool:
    """``cur`` is the element of comprehension ``p`` (``[{...} for x in xs]``)."""
    return isinstance(p, (ast.ListComp, ast.SetComp, ast.GeneratorExp)) and p.elt is cur


def _is_seq_call(p: ast.AST, cur: ast.AST) -> bool:
    """``list(cur)`` / ``tuple(cur)``."""
    return isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id in ("list", "tuple") and p.args[:1] == [cur]


def _field_class(call: ast.Call, scope: _Scope) -> str | None:
    """Field class of a field declaration call, the forms of the facts extractor:
    ``fields.X(...)`` (also ``odoo.fields`` / ``openerp.fields``) or ``X(...)``
    with ``X`` imported by name from a ``*field*`` module (``from odoo.fields
    import Many2one``, 19+ ``from .fields_misc import Id``)."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr if _dotted(func.value) in FIELDS_MODULES and func.attr[:1].isupper() else None
    if not isinstance(func, ast.Name):
        return None
    for b in _lookup(scope, func.id)[1] or ():
        if b[0] == "import" and isinstance(b[2], ast.ImportFrom):
            module, name = b[2].module or "", b[1].name
        elif b[0] == "fimport" and b[1][I_NAME]:
            module, name = b[1][I_MODULE], b[1][I_NAME]
        else:
            continue
        if name[:1].isupper() and "field" in module.rsplit(".", 1)[-1]:
            return name
    return None


def _field_comodel(call: ast.Call, ftype: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == "comodel_name":
            return _str_const(kw.value)
    if call.args and ftype in RELATIONAL:
        return _str_const(call.args[0])
    return None


def _is_command_tuple(t: ast.Tuple) -> bool:
    """``(0, 0, vals)`` / ``(1, id, vals)`` x2many command tuples."""
    elts = t.elts
    return len(elts) == 3 and isinstance(elts[0], ast.Constant) and elts[0].value in (0, 1) and isinstance(elts[2], (ast.Dict, ast.Name, ast.Call))


def _python_bases(an: _Analyzer, ci: _ClassInfo) -> list[tuple[str, str]]:
    """(path, class name) of the python base classes of a class of ``an``'s file."""
    out = []
    for b in ci.node.bases:
        dotted = _dotted(b)
        if not dotted:
            continue
        first, _, rest = dotted.partition(".")
        _s, binders = _lookup(an.f.module_scope, first)
        if not binders:
            continue
        b0 = binders[0]
        sym = None
        if b0[0] == "def" and isinstance(b0[1], ast.ClassDef):
            if not rest and b0[1].name != ci.name:
                out.append((an.path, b0[1].name))
            continue
        if b0[0] == "import":
            sym = an.import_sym(b0[1], b0[2])
        for part in rest.split(".") if rest else ():
            if sym is None or sym[0] != "module":
                sym = None
                break
            sym = _resolve_symbol(an.reg, sym[1], part)
        if sym is not None and sym[0] == "sym" and sym[3] == "class":
            out.append((sym[1], sym[2]))
    return out


_CROSS = [0]  # nesting of cross-file inferences
_RET_CACHE: dict[tuple, Any] = {}
_SNIPPETS: OrderedDict[tuple, Any] = OrderedDict()
_LINES: OrderedDict[str, tuple] = OrderedDict()


def _file_lines(path: str) -> list[bytes] | None:
    """Lines of a file on disk (small cache validated by mtime / size)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    hit = _LINES.get(path)
    if hit is not None and hit[0] == (st.st_mtime_ns, st.st_size):
        _LINES.move_to_end(path)
        return hit[1]
    try:
        with open(path, "rb") as fh:
            lines = fh.read().split(b"\n")
    except OSError:
        return None
    _LINES[path] = ((st.st_mtime_ns, st.st_size), lines)
    while len(_LINES) > 64:
        _LINES.popitem(last=False)
    return lines


def _method_snippet(reg: Any, d: Any) -> tuple[_Analyzer, _Unit] | None:
    """Analyzer + unit for one method definition of another file, parsing only
    the method's source (within a stand-in class carrying its model); the
    module scope comes from the file's facts."""
    key = (d.path, d.line)
    if key in _SNIPPETS:
        _SNIPPETS.move_to_end(key)
        return _SNIPPETS[key]
    _SNIPPETS[key] = None
    while len(_SNIPPETS) > 256:
        _SNIPPETS.popitem(last=False)
    lines = _file_lines(d.path)
    facts = reg.file_facts(d.path)
    if lines is None or not facts or len(d.facts) <= M_END_LINE or d.line >= len(lines):
        return None
    start, end = d.line, d.facts[M_END_LINE]
    first = lines[start]
    if not first.lstrip().startswith((b"def ", b"async ")):
        return None
    body = b"\n".join(lines[start : end + 1])
    indented = first[:1] in (b" ", b"\t")
    tree = _parse(b"class _S:\n" + body if indented else body)
    if tree is None or not tree.body:
        return None
    ast.increment_lineno(tree, start - 1 if indented else start)
    f = _File(d.path, b"", tree, lines, partial=True)
    f.facts_module_scope(facts)
    ci = None
    fn = tree.body[0]
    if indented and isinstance(fn, ast.ClassDef) and fn.body:
        ci = _ClassInfo(fn, -1)
        ci.model = d.model or "base"
        ci.is_model = True
        f.class_by_node[fn] = ci
        fn = fn.body[0]
    if not isinstance(fn, _FUNC):
        return None
    an = _Analyzer(f, reg)
    f.analyzer = an
    res = (an, f.unit(fn, ci))
    _SNIPPETS[key] = res
    return res


def _def_returns(an: _Analyzer, d: Any) -> tuple | None:
    """Union of the types of the ``return`` values of a method definition
    (a registry MemberDef); None when unknown."""
    if d.path == an.path and not an.f.partial:
        other = an
        u = an.f.unit_at(d.line)
    else:
        snip = _method_snippet(an.reg, d)
        if snip is None:
            return None
        other, u = snip
    if u is None or not isinstance(u.root, _FUNC) or u.root.name != d.name:  # type: ignore[attr-defined]
        return None
    types = []
    stack: list[ast.AST] = list(u.root.body)  # type: ignore[attr-defined]
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Return):
            if n.value is not None:
                types.append(other.infer(n.value, u))
            continue
        if isinstance(n, _FUNC + (ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))
    known = [t for t in types if t is not None]
    return _union(known) if known else None


def _inst_attr_type(an: _Analyzer, path: str, clsname: str, attr: str, depth: int) -> tuple | None:
    if depth > 3 or _CROSS[0] > 3:
        return None
    f = an.f if path == an.path else _file_for_path(path)
    if f is None or f.tree is None:
        return None
    ci = next((c for c in f.classes if c.name == clsname), None)
    if ci is None:
        return None
    other = an if f is an.f else _analyzer(an.reg, path, f.source)
    for stmt in ci.node.body:
        if isinstance(stmt, _FUNC + (ast.ClassDef,)) and stmt.name == attr:
            return OBJ
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if any(n.id == attr for t in targets for n in _target_names(t)):
                return OBJ
    assigns = f.instance_assigns(ci).get(attr)
    if assigns:
        _CROSS[0] += 1
        try:
            return _union([other.infer(value, f.unit(meth, ci)) for meth, value, _t in assigns])
        finally:
            _CROSS[0] -= 1
    for bpath, bname in _python_bases(other, ci):
        t = _inst_attr_type(an, bpath, bname, attr, depth + 1)
        if t is not None:
            return t
    return None


# ---------------------------------------------------------------------------
# tokens: what the cursor can designate on a line


class _Tok:
    __slots__ = ("c0", "c1", "prio", "kind", "data")

    def __init__(self, c0: int, c1: int, prio: int, kind: str, data: Any):
        self.c0 = c0  # UTF-16 columns of the token on the line
        self.c1 = c1
        self.prio = prio  # 0 Odoo strings / definitions, 1 attributes & def names, 2 plain names
        self.kind = kind
        self.data = data


def _str_content(f: _File, node: ast.Constant) -> tuple[int, int, int, bool] | None:
    """(line0, start byte, end byte, exact) of the content of a single-line str
    literal; ``exact`` when byte offsets inside map 1:1 to the value."""
    if node.lineno != node.end_lineno:
        return None
    l0 = node.lineno - 1
    raw = f.lines[l0][node.col_offset : node.end_col_offset]
    q = 0
    while q < len(raw) and raw[q] not in (34, 39):
        q += 1
    if q >= len(raw) or q > 3:
        return None
    quote = raw[q : q + 3] if raw[q : q + 3] in (b"'''", b'"""') else raw[q : q + 1]
    if len(raw) < q + 2 * len(quote) or not raw.endswith(quote):
        return None
    start = node.col_offset + q + len(quote)
    end = node.end_col_offset - len(quote)
    content = f.lines[l0][start:end]
    exact = content == node.value.encode("utf-8")
    if not exact and b"\\" not in content:
        return None  # implicit concatenation
    return l0, start, end, exact


_MODEL_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
_AGG_SPEC = re.compile(r"\s*\w+\s*:\s*\w+\s*\(\s*([\w.]+)\s*\)\s*$")


def _spec_field(value: str) -> tuple[str, int]:
    """(field path, offset in ``value``) of a field-name argument: ``'f'``,
    ``'f:sum'``, ``'date:month'`` or a 16.0 read_group alias ``'total:sum(amount)'``."""
    m = _AGG_SPEC.match(value)
    if m:
        return m.group(1), m.start(1)
    return value.split(":")[0], 0


def _segments(value: str, sep: str, c0: int, c1: int, exact: bool) -> list[tuple[int, str, int, int]]:
    """(index, item, byte start, byte end) of the ``sep``-separated items of a
    string whose content spans bytes ``[c0, c1)`` (whole span if not exact)."""
    out = []
    pos = 0
    for i, part in enumerate(value.split(sep)):
        item = part.strip()
        lead = len(part) - len(part.lstrip())
        if exact and item:
            b0 = c0 + len(value[: pos + lead].encode("utf-8"))
            out.append((i, item, b0, b0 + len(item.encode("utf-8"))))
        elif item:
            out.append((i, item, c0, c1))
        pos += len(part) + len(sep)
    return out


def _walk_lines(root: ast.AST, lo: int, hi: int, header: bool = False) -> Iterator[ast.AST]:
    """Nodes under ``root`` that may intersect 1-based lines ``[lo, hi]``."""
    if header and isinstance(root, ast.ClassDef):
        stack: list[ast.AST] = list(root.decorator_list) + list(root.bases) + list(root.keywords)
    else:
        stack = [root]
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            ln = getattr(c, "lineno", None)
            if ln is not None and (_start(c) > hi or (getattr(c, "end_lineno", None) or ln) < lo):
                continue
            stack.append(c)


class _LineTokens:
    """Collects the tokens of one line of the analysed file."""

    def __init__(self, an: _Analyzer, line0: int, facts: dict | None):
        self.an = an
        self.f = an.f
        self.line0 = line0
        self.facts = facts
        self.toks: list[_Tok] = []
        self.strings: list[tuple[int, int]] = []  # UTF-16 spans of string literals (comment detection)
        self.unit: _Unit | None = None

    def add(self, c0: int, c1: int, prio: int, kind: str, data: Any) -> None:
        if c1 > c0:
            self.toks.append(_Tok(c0, c1, prio, kind, data))

    def collect(self) -> list[_Tok]:
        self.facts_tokens()
        u = self.f.unit_at(self.line0)
        self.unit = u
        if u is not None:
            self.ast_tokens(u)
        # same range from several sources: keep the first (facts) one
        seen = set()
        out = []
        for t in self.toks:
            key = (t.c0, t.c1)
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    # -- from facts ----------------------------------------------------------------

    def facts_tokens(self) -> None:
        facts = self.facts
        if not facts:
            return
        line0 = self.line0
        for ref in facts.get(REFS) or ():
            if ref[R_LINE] != line0:
                continue
            kind = ref[R_KIND]
            c0, c1 = ref[R_COL], ref[R_END_COL]
            value = ref[R_VALUE]
            if kind == REF_FIELD and "." in value:
                exact = (c1 - c0) == len(value)
                pos = 0
                for i, seg in enumerate(value.split(".")):
                    s0 = c0 + pos if exact else c0
                    self.add(s0, s0 + len(seg) if exact else c1, 0, "ref", (ref, i))
                    pos += len(seg) + 1
            else:
                self.add(c0, c1, 0, "ref", (ref, None))
        major = self.an.major
        for idx, cls in enumerate(facts.get(CLASSES) or ()):
            if not cls[K_LINE] <= line0 <= cls[4]:
                continue
            model = model_class_info(cls, major)[0] if cls[K_MODEL] else None
            if self.f.framework and cls[K_NAME] in FRAMEWORK_CLASSES:
                model = "base"
            if cls[K_LINE] == line0:
                self.add(cls[K_COL], cls[K_END_COL], 1, "classdef", (idx, cls, model))
            for fd in cls[K_FIELDS]:
                if fd[F_LINE] == line0:
                    self.add(fd[F_COL], fd[F_END_COL], 1, "fielddef", (idx, fd, model))
            for m in cls[K_METHODS]:
                if m[M_LINE] == line0:
                    self.add(m[M_COL], m[M_END_COL], 1, "methoddef", (idx, m, model))
            for a in cls[K_ATTRS]:
                if a[A_LINE] == line0:
                    self.add(a[A_COL], a[A_END_COL], 1, "attrdef", (idx, a, model, cls))

    # -- from the AST -------------------------------------------------------------------

    def ast_tokens(self, u: _Unit) -> None:
        f = self.f
        an = self.an
        lineno = self.line0 + 1
        line0 = self.line0
        for node in _walk_lines(u.root, lineno, lineno, u.header):
            if isinstance(node, ast.Constant):
                if isinstance(node.value, str) and node.lineno <= lineno <= (node.end_lineno or node.lineno):
                    self.string_tokens(node, u)
            elif isinstance(node, ast.JoinedStr):
                if node.lineno == lineno == node.end_lineno:
                    self.strings.append((f.u16(line0, node.col_offset), f.u16(line0, node.end_col_offset)))
                elif node.lineno <= lineno <= (node.end_lineno or node.lineno):
                    self.strings.append((0, 1 << 30))
            elif isinstance(node, ast.Attribute):
                if (node.end_lineno or node.lineno) == lineno:
                    b1 = node.end_col_offset or 0
                    b0 = b1 - len(node.attr.encode("utf-8"))
                    self.add(f.u16(line0, b0), f.u16(line0, b1), 1, "attr", node)
            elif isinstance(node, ast.Name):
                if node.lineno == lineno:
                    c0, c1 = f.u16(line0, node.col_offset), f.u16(line0, node.end_col_offset or 0)
                    if node.id not in BUILTINS or _lookup(an.scope_of(node, u), node.id)[1]:
                        self.add(c0, c1, 2, "name", node)
                    elif node.id == "super" and isinstance(u.parents.get(node), ast.Call):
                        sup = u.parents[node]
                        attr = u.parents.get(sup)
                        if isinstance(attr, ast.Attribute) and attr.value is sup:
                            self.add(c0, c1, 2, "super", attr)  # resolves like the attribute it reaches
                        else:
                            self.add(c0, c1, 3, "inert", node)
                    else:
                        self.add(c0, c1, 3, "inert", node)  # a builtin: nothing, but no snapping either
            elif isinstance(node, ast.keyword):
                if node.arg and node.lineno == lineno:
                    b0 = node.col_offset
                    c0, c1 = f.u16(line0, b0), f.u16(line0, b0 + len(node.arg.encode("utf-8")))
                    call = u.parents.get(node)
                    if node.arg.startswith("default_") and isinstance(call, ast.Call) and _call_name(call.func) == "with_context":
                        self.add(c0, c1, 0, "ctxkw", node)
                    elif isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "dict":
                        self.add(c0, c1, 0, "dictkw", (node, call))  # create(dict(name=...))
                    else:  # a keyword-argument name: its value's Odoo string (compute='_x'), else nothing
                        self.add(c0, c1, 3, "kwarg", node)
            elif isinstance(node, _FUNC + (ast.ClassDef,)):
                if node.lineno == lineno:
                    pos = _def_name_pos(f, node)
                    if pos is not None:
                        self.add(pos[1], pos[2], 1, "def", node)
            elif isinstance(node, ast.arg):
                if node.lineno == lineno:
                    b0 = node.col_offset
                    self.add(f.u16(line0, b0), f.u16(line0, b0 + len(node.arg.encode("utf-8"))), 2, "arg", node)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self.import_tokens(node)

    def string_tokens(self, node: ast.Constant, u: _Unit) -> None:
        f = self.f
        span = _str_content(f, node)
        if span is None:
            if node.lineno == node.end_lineno:
                self.strings.append((f.u16(self.line0, node.col_offset), f.u16(self.line0, node.end_col_offset or 0)))
            else:
                self.strings.append((0, 1 << 30))
            return
        l0, b0, b1, exact = span
        self.strings.append((f.u16(l0, node.col_offset), f.u16(l0, node.end_col_offset or 0)))
        role = self.an.classify_string(node, u)
        if role is None:
            # a literal naming a known model ('sale.order' passed to any helper)
            if exact and _MODEL_NAME.match(node.value) and self.an.has_model(node.value):
                self.add(f.u16(l0, b0), f.u16(l0, b1), 0, "str", (node, ("model", None, "literal"), 0, node.value, b0, b1))
            return
        mode = role[0]
        value = node.value
        if mode in ("model", "xmlid", "key", "default"):
            self.add(f.u16(l0, b0), f.u16(l0, b1), 0, "str", (node, role, 0, value, b0, b1))
            return
        if mode == "order":
            for _i, item, s0, s1 in _segments(value, ",", b0, b1, exact):
                fname = item.split()[0].split(":")[0]  # 'date:min desc' (read_group orderby)
                pos = 0
                for j, seg in enumerate(fname.split(".")):
                    e0 = s0 + pos if exact else s0
                    e1 = e0 + len(seg.encode("utf-8")) if exact else s1
                    self.add(f.u16(l0, e0), f.u16(l0, e1), 0, "str", (node, role, j, fname, e0, e1))
                    pos += len(seg.encode("utf-8")) + 1
            return
        if mode == "name":  # 'f', 'f:agg', 'date:month', 'alias:agg(f)'
            path, k = _spec_field(value)
            if exact:
                b0 += len(value[:k].encode("utf-8"))
                b1 = b0 + len(path.encode("utf-8"))
        else:
            path = value
        for j, seg, s0, s1 in _segments(path, ".", b0, b1, exact):
            self.add(f.u16(l0, s0), f.u16(l0, s1), 0, "str", (node, role, j, path, s0, s1))

    def import_tokens(self, node: ast.AST) -> None:
        f = self.f
        line0 = self.line0
        lineno = line0 + 1
        if isinstance(node, ast.ImportFrom):
            if node.lineno == lineno:
                m = _FROM_RX.match(f.lines[line0])
                if m:
                    dots = len(m.group(1))
                    module = m.group(2).decode("utf-8", "replace")
                    base = m.start(2)
                    pos = 0
                    parts = module.split(".") if module else []
                    for i, part in enumerate(parts):
                        b0 = base + pos
                        prefix = ".".join(parts[: i + 1])
                        self.add(f.u16(line0, b0), f.u16(line0, b0 + len(part)), 2, "modpath", (prefix, dots))
                        pos += len(part) + 1
        for a in node.names:  # type: ignore[attr-defined]
            if getattr(a, "lineno", None) != lineno:
                continue
            b0, b1 = a.col_offset, a.end_col_offset
            if isinstance(node, ast.Import):
                parts = a.name.split(".")
                pos = 0
                for i, part in enumerate(parts):
                    s0 = b0 + pos
                    self.add(f.u16(line0, s0), f.u16(line0, s0 + len(part)), 1, "modpath", (".".join(parts[: i + 1]), 0))
                    pos += len(part) + 1
                if a.asname:
                    self.add(f.u16(line0, b1 - len(a.asname)), f.u16(line0, b1), 1, "alias", (a, node))
            else:
                self.add(f.u16(line0, b0), f.u16(line0, b0 + len(a.name.encode("utf-8"))), 1, "alias", (a, node))
                if a.asname:
                    self.add(f.u16(line0, b1 - len(a.asname)), f.u16(line0, b1), 1, "alias", (a, node))


def _def_name_pos(f: _File, node: ast.AST) -> tuple[int, int, int] | None:
    """(line0, col16, end_col16) of the name of a def / class statement."""
    l0 = node.lineno - 1  # type: ignore[attr-defined]
    line = f.lines[l0]
    kw = b"class" if isinstance(node, ast.ClassDef) else b"def"
    name = node.name.encode("utf-8")  # type: ignore[attr-defined]
    i = line.find(kw, node.col_offset)  # type: ignore[attr-defined]
    j = line.find(name, i + len(kw)) if i >= 0 else -1
    if j < 0:
        return None
    return l0, f.u16(l0, j), f.u16(l0, j + len(name))


def _comment_start(f: _File, line0: int, strings: list[tuple[int, int]]) -> int | None:
    """UTF-16 column of a ``#`` comment on the line (outside string literals)."""
    raw = f.lines[line0] if line0 < len(f.lines) else b""
    i = raw.find(b"#")
    while i >= 0:
        c = f.u16(line0, i)
        if not any(s0 <= c < s1 for s0, s1 in strings):
            return c
        i = raw.find(b"#", i + 1)
    return None


def _pick(toks: list[_Tok], col: int) -> _Tok | None:
    """The token under (or nearest to) the cursor: containing tokens first
    (innermost), else the closest within LENIENCY columns, weighing Odoo
    strings and attribute names over plain names; ties go to the right."""
    inside = [t for t in toks if t.c0 <= col < t.c1]
    if inside:
        return min(inside, key=lambda t: (t.c1 - t.c0, t.prio))
    best = None
    best_key = None
    for t in toks:
        dist = t.c0 - col if col < t.c0 else col - t.c1 + 1
        if dist > LENIENCY:
            continue
        key = (2 * dist + t.prio, 0 if t.c0 > col else 1)
        if best_key is None or key < best_key:
            best, best_key = t, key
    return best


# ---------------------------------------------------------------------------
# token -> targets


def _resolve(an: _Analyzer, lt: _LineTokens, tok: _Tok) -> list[Target]:
    kind = tok.kind
    u = lt.unit
    line0 = lt.line0
    origin = (line0, tok.c0, tok.c1)
    if kind == "ref":
        return _ref_targets(an, lt, tok)
    if kind == "str":
        return _string_targets(an, tok, origin)
    if kind == "classdef":
        idx, cls, model = tok.data
        if model:
            return [Target(MODEL, model=model, origin=origin, extra={"class_index": idx, "definition": True, "inferred": False})]
        return [Target(PYSYM, name=cls[K_NAME], path=an.path, line=cls[K_LINE], col=cls[K_COL], end_col=cls[K_END_COL], origin=origin, extra={"symbol_kind": "class"})]
    if kind in ("fielddef", "methoddef"):
        idx, m, model = tok.data
        if model:
            return [Target(FIELD if kind == "fielddef" else METHOD, model=model, name=m[0], origin=origin, extra={"class_index": idx, "definition": True, "inferred": False})]
        return [Target(PYSYM, name=m[0], path=an.path, line=m[1], col=m[2], end_col=m[3], origin=origin, extra={"symbol_kind": "def" if kind == "methoddef" else "var"})]
    if kind == "attrdef":
        idx, a, model, cls = tok.data
        if model and a[A_NAME] in ("_name", "_inherit"):
            return [Target(MODEL, model=model, origin=origin, extra={"class_index": idx, "role": a[A_NAME], "inferred": False})]
        if model:
            return [Target(ATTR, model=model, name=a[A_NAME], origin=origin, extra={"class_index": idx, "definition": True, "inferred": False})]
        return [Target(PYSYM, name=a[A_NAME], path=an.path, line=a[A_LINE], col=a[A_COL], end_col=a[A_END_COL], origin=origin, extra={"symbol_kind": "var"})]
    if u is None:
        return []
    if kind == "attr":
        return _attr_targets(an, tok.data, u, origin)
    if kind == "super":
        return _attr_targets(an, tok.data, u, origin)
    if kind == "name":
        return _name_targets(an, tok.data, u, origin)
    if kind == "kwarg":
        return _kwarg_targets(an, lt, tok.data, line0)
    if kind == "dictkw":
        node, call = tok.data
        models = an.vals_models(call, u)  # type: ignore[arg-type]
        return _field_name_targets(an, models, node.arg, origin, {"role": "vals"}, u) if models else []
    if kind == "ctxkw":
        node = tok.data
        call = u.parents.get(node)
        models = an.consumer_models(call, u) if isinstance(call, ast.Call) else None
        fname = node.arg[8:]
        o = (line0, tok.c0 + 8, tok.c1)
        return _field_name_targets(an, models, fname, o, {"role": "default"}, u)
    if kind == "def":
        node = tok.data
        ci = an.class_of_function(node, u) if isinstance(node, _FUNC) else an.f.class_by_node.get(node)
        if isinstance(node, _FUNC) and ci is not None and ci.model:
            return [Target(METHOD, model=ci.model, name=node.name, origin=origin, extra={"class_index": ci.index, "definition": True, "inferred": False})]
        if isinstance(node, ast.ClassDef) and ci is not None and ci.model:
            return [Target(MODEL, model=ci.model, origin=origin, extra={"class_index": ci.index, "definition": True, "inferred": False})]
        return [Target(PYSYM, name=node.name, path=an.path, line=line0, col=tok.c0, end_col=tok.c1, origin=origin, extra={"symbol_kind": "class" if isinstance(node, ast.ClassDef) else "def"})]
    if kind == "arg":
        node = tok.data
        t = _arg_value_type(an, node, u)
        extra = {"symbol_kind": "param"}
        if t is not None and t[0] == "rs":
            extra["value_models"] = list(t[1])
        elif t is not None and t[0] == "vals" and t[1]:
            extra["vals_model"] = t[1]
        return [Target(PYSYM, name=node.arg, path=an.path, line=line0, col=tok.c0, end_col=tok.c1, origin=origin, extra=extra)]
    if kind == "alias":
        a, stmt = tok.data
        return an.sym_targets(an.import_sym(a, stmt), origin, {"role": "import"})
    if kind == "modpath":
        prefix, dots = tok.data
        dotted = an.abs_module(prefix, dots) if dots else prefix
        if not dotted:
            return []
        parts = dotted.split(".")
        if len(parts) == 3 and parts[:2] == ["odoo", "addons"] and an.reg.module(parts[2]) is not None:
            return [Target(MODULE, name=parts[2], origin=origin, extra={"role": "import"})]
        path = an.reg.py_module_file(dotted)
        return [Target(FILE, path=path, name=dotted, origin=origin, extra={"role": "import"})] if path else []
    return []


def _kwarg_targets(an: _Analyzer, lt: _LineTokens, kw: ast.keyword, line0: int) -> list[Target]:
    """A keyword-argument name: the targets of the Odoo string token (facts ref or
    role string) starting its value on the same line, e.g. ``compute='_compute_x'``;
    nothing for other values (``string="Customer"``, ``body=...``)."""
    v = kw.value
    if v.lineno - 1 != line0:
        return []
    f = an.f
    v0 = f.u16(line0, v.col_offset)
    v1 = f.u16(line0, v.end_col_offset or 0) if (v.end_lineno or v.lineno) - 1 == line0 else 1 << 30
    inside = [t for t in lt.toks if t.kind in ("ref", "str") and v0 <= t.c0 and t.c1 <= v1]
    if not inside:
        return []
    return _resolve(an, lt, min(inside, key=lambda t: t.c0))


def _arg_value_type(an: _Analyzer, node: ast.arg, u: _Unit) -> tuple | None:
    fn = u.parents.get(node)
    while fn is not None and not isinstance(fn, _FUNC + (ast.Lambda,)):
        fn = u.parents.get(fn)
    if fn is None:
        return None
    s = u.scopes.get(fn)
    if s is None:
        return None
    for b in s.binders.get(node.arg, ()):
        if b[0] == "param" and b[1] is node:
            return an.param_type(node, b[2], fn, s, u)
    return None


def _ref_targets(an: _Analyzer, lt: _LineTokens, tok: _Tok) -> list[Target]:
    ref, seg = tok.data
    kind = ref[R_KIND]
    value = ref[R_VALUE]
    rctx = ref[R_CTX] or ()
    role = rctx[0] if rctx else None
    origin = (lt.line0, tok.c0, tok.c1)
    extra = {"role": role, "inferred": False}
    if kind == REF_MODEL:
        return [Target(MODEL, model=value, origin=origin, extra=extra)]
    if kind == REF_XMLID:
        return [Target(XMLID, xmlid=an.reg.qualify_xmlid(value, an.module), origin=origin, extra=extra)]
    if kind == REF_QWEB:
        return [Target(TEMPLATE, xmlid=value, origin=origin, extra=extra)]
    if kind == REF_MODULE:
        return [Target(MODULE, name=value, origin=origin, extra=extra)]
    model = rctx[1] if len(rctx) > 1 and isinstance(rctx[1], str) else None
    models: tuple | None = (model,) if model else None
    if models is None and lt.unit is not None:
        node = _const_at(lt, ref)
        role_info = an.classify_string(node, lt.unit) if node is not None else None
        models = role_info[1] if role_info else None
    if kind == REF_METHOD:
        if models:
            return an.member_targets(models, value, True, origin, {"role": role}, lt.unit)
        return an.fallback_targets(value, True, origin, {"role": role}, None, lt.unit)
    if kind == REF_FIELD:
        return _path_segment_targets(an, models, value, seg or 0, origin, {"role": role, "path": value}, lt.unit)
    return []


def _const_at(lt: _LineTokens, ref: tuple) -> ast.Constant | None:
    u = lt.unit
    if u is None:
        return None
    lineno = lt.line0 + 1
    for node in _walk_lines(u.root, lineno, lineno, u.header):
        if isinstance(node, ast.Constant) and node.value == ref[R_VALUE] and node.lineno == lineno:
            return node
    return None


def _path_segment_targets(an: _Analyzer, models: tuple | None, path: str, seg: int, origin: tuple, extra: dict, u: _Unit | None) -> list[Target]:
    """FIELD target for segment ``seg`` of field path ``path`` walked from ``models``."""
    segs = path.split(".")
    if seg >= len(segs):
        return []
    name = segs[seg]
    if not models:
        if seg:
            return []
        return an.fallback_targets(name, False, origin, extra, None, u)
    owners: list[str] = []
    for m in models:
        res = an.walk(m, path)
        if seg < len(res):
            owner = res[seg][1]
            if owner and all(r[2] for r in res[:seg]):
                owners.append(owner)
    owners = list(dict.fromkeys(owners))
    if not owners:
        return []
    found = [o for o in owners if an.member(o, name)[0] == "field"]
    if not found:
        return [Target(FIELD, model=owners[0], name=name, origin=origin, extra=dict(extra, inferred=True, missing=True))]
    if len(found) == 1:
        return [Target(FIELD, model=found[0], name=name, origin=origin, extra=dict(extra, inferred=True))]
    return [Target(FIELD, model=None, models=found, name=name, origin=origin, extra=dict(extra, inferred=True))]


def _field_name_targets(an: _Analyzer, models: tuple | None, name: str, origin: tuple, extra: dict, u: _Unit | None) -> list[Target]:
    """A field name used as a values / context key."""
    if models:
        found = [m for m in models if an.member(m, name)[0] == "field"]
        if len(found) == 1:
            return [Target(FIELD, model=found[0], name=name, origin=origin, extra=dict(extra, inferred=True))]
        if found:
            return [Target(FIELD, model=None, models=found, name=name, origin=origin, extra=dict(extra, inferred=True))]
        if extra.get("role") == "default":
            return []  # not a field of the consumer: a plain context key
        return [Target(FIELD, model=models[0], name=name, origin=origin, extra=dict(extra, inferred=True, missing=True))]
    return an.fallback_targets(name, False, origin, extra, None, u)


def _string_targets(an: _Analyzer, tok: _Tok, origin: tuple) -> list[Target]:
    node, role, seg, path, _b0, _b1 = tok.data
    mode, models, rname = role
    u = an.f.unit_at(node.lineno - 1)
    extra = {"role": rname}
    if mode == "model":
        return [Target(MODEL, model=node.value, origin=origin, extra=dict(extra, inferred=False))]
    if mode == "xmlid":
        return [Target(XMLID, xmlid=an.reg.qualify_xmlid(node.value, an.module), origin=origin, extra=dict(extra, inferred=False))]
    if mode == "key":
        return _field_name_targets(an, models, node.value, origin, extra, u)
    if mode == "default":
        name = node.value[8:]
        o = (origin[0], origin[1] + 8, origin[2])
        return _field_name_targets(an, models, name, o, dict(extra, role="default"), u)
    if mode == "name" and path == "__count":
        return []
    if rname == "getattr" and models:  # getattr(rec, 'x'): a field, method or attribute
        return an.member_targets(models, node.value, False, origin, extra, u)
    return _path_segment_targets(an, models, path, seg, origin, dict(extra, path=path), u)


def _attr_targets(an: _Analyzer, node: ast.Attribute, u: _Unit, origin: tuple) -> list[Target]:
    attr = node.attr
    parent = u.parents.get(node)
    is_call = isinstance(parent, ast.Call) and parent.func is node
    extra: dict = {"receiver": _text(an.f, node.value)}
    if _is_super_call(node.value):
        _fn, ci = an.enclosing_method(node, u)
        if ci is not None and ci.model:
            return [Target(_super_kind(an, ci.model, attr, is_call), model=ci.model, name=attr, origin=origin, extra=dict(extra, super=True, class_index=ci.index, inferred=True))]
        if ci is not None:
            return an.sym_targets(_py_super_member(an, ci, attr), origin, extra)
        return []
    rt = an.infer(node.value, u)
    if rt is None:
        if attr == "env":
            return _env_class_targets(an, None, origin, extra)
        return an.fallback_targets(attr, is_call, origin, extra, node, u)
    k = rt[0]
    if k == "rs":
        if attr == "env":
            return _env_class_targets(an, None, origin, extra)
        if attr in RS_SLOTS:
            return []
        return an.member_targets(rt[1], attr, is_call, origin, extra, u)
    if k == "super":
        ci = rt[1]
        if ci.model:
            return [Target(_super_kind(an, ci.model, attr, is_call), model=ci.model, name=attr, origin=origin, extra=dict(extra, super=True, class_index=ci.index, inferred=True))]
        return an.sym_targets(_py_super_member(an, ci, attr), origin, extra)
    if k == "env":
        t = an.attr_type(node, u)
        if t is not None and t[0] == "rs":
            extra["value_models"] = list(t[1])
        return _env_class_targets(an, attr, origin, extra)
    if k == "req":
        sym = _resolve_symbol(an.reg, "odoo.http", "Request")
        found = _python_member(an, sym[1], sym[2], attr) if sym is not None and sym[0] == "sym" else None
        if found is None and attr == "website" and an.has_model("website"):
            # set on the request by the website module (16-19)
            return [Target(MODEL, model="website", origin=origin, extra=dict(extra, inferred=True))]
        return an.sym_targets(found, origin, extra)
    if k == "mod":
        return an.sym_targets(_resolve_symbol(an.reg, rt[1], attr), origin, extra)
    if k in ("cls", "inst"):
        return an.sym_targets(_python_member(an, rt[1], rt[2], attr), origin, extra)
    return []


def _super_kind(an: _Analyzer, model: str, attr: str, is_call: bool) -> str:
    """Target kind of ``super().attr`` on ``model``: a method unless the member is
    only a field or a class attribute (e.g. a property overriding a mixin's
    class attribute reaches that attribute)."""
    if is_call:
        return METHOD
    kind = an.member(model, attr)[0]
    if kind == "attr":
        return ATTR
    if kind == "field":
        return FIELD
    return METHOD


def _python_member(an: _Analyzer, path: str, clsname: str, attr: str) -> tuple | None:
    """Definition of ``attr`` on python class ``clsname`` of file ``path``."""
    if path == an.path:
        other: _Analyzer | None = an
    else:
        bf = _file_for_path(path)
        other = _analyzer(an.reg, path, bf.source) if bf is not None and bf.tree is not None else None
    sym = _class_member_def(other, clsname, attr) if other is not None else None
    return sym or _class_member(an.reg, path, clsname, attr)


def _env_class_targets(an: _Analyzer, attr: str | None, origin: tuple, extra: dict) -> list[Target]:
    """``env`` itself -> the Environment class; ``env.<attr>`` -> its member."""
    sym = _resolve_symbol(an.reg, "odoo.api", "Environment")
    if sym is None or sym[0] != "sym":
        return []
    if attr is None:
        return an.sym_targets(sym, origin, extra)
    # _python_member also finds annotations (``cr: BaseCursor``) and ``self.x = ...``
    return an.sym_targets(_python_member(an, sym[1], sym[2], attr), origin, extra)


def _py_super_member(an: _Analyzer, ci: _ClassInfo, attr: str) -> tuple | None:
    """``super().attr`` in a plain python class: the member in its bases."""
    for bpath, bname in _python_bases(an, ci):
        res = _python_member(an, bpath, bname, attr)
        if res is not None:
            return res
    return None


def _class_member_def(an: _Analyzer, clsname: str, attr: str, depth: int = 0) -> tuple | None:
    """Definition of ``attr`` on python class ``clsname`` of ``an``'s file: a def or
    class attribute, else the first ``self.<attr> = ...`` / ``cls.<attr> = ...``
    assignment in its methods; searched through its python bases (any file)."""
    if depth > 4:
        return None
    f = an.f
    ci = next((c for c in f.classes if c.name == clsname), None)
    if ci is None or f.partial:
        return None
    for stmt in ci.node.body:
        if isinstance(stmt, _FUNC + (ast.ClassDef,)) and stmt.name == attr:
            pos = _def_name_pos(f, stmt)
            if pos:
                return ("sym", an.path, attr, "def", pos[0], pos[1], pos[2])
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                for n in _target_names(t):
                    if n.id == attr:
                        l0 = n.lineno - 1
                        return ("sym", an.path, attr, "var", l0, f.u16(l0, n.col_offset), f.u16(l0, n.end_col_offset or 0))
    assigns = f.instance_assigns(ci).get(attr)
    if assigns:
        t = min((a[2] for a in assigns), key=lambda n: (n.end_lineno, n.end_col_offset))
        l0 = (t.end_lineno or t.lineno) - 1
        b1 = t.end_col_offset or 0
        return ("sym", an.path, attr, "var", l0, f.u16(l0, b1 - len(attr.encode("utf-8"))), f.u16(l0, b1))
    for bpath, bname in _python_bases(an, ci):
        if bpath == an.path:
            other: _Analyzer | None = an
        else:
            bf = _file_for_path(bpath)
            other = _analyzer(an.reg, bpath, bf.source) if bf is not None and bf.tree is not None else None
        res = _class_member_def(other, bname, attr, depth + 1) if other is not None else None
        if res is None:
            res = _class_member(an.reg, bpath, bname, attr)
        if res is not None:
            return res
    return None


def _name_targets(an: _Analyzer, node: ast.Name, u: _Unit, origin: tuple) -> list[Target]:
    scope = an.scope_of(node, u)
    s, binders = _lookup(scope, node.id)
    if not binders or s is None:
        return []
    f = an.f
    extra: dict = {}
    t = an.infer(node, u)
    if t is not None and t[0] == "rs":
        extra["value_models"] = list(t[1])
    elif t is not None and t[0] == "vals" and t[1]:
        extra["vals_model"] = t[1]
    if s.kind == "module" or s.kind == "class":
        for b in binders:
            if b[0] == "import":
                return an.sym_targets(an.import_sym(b[1], b[2]), origin, dict(extra, role="import"))
            if b[0] == "fimport":
                return an.sym_targets(an.facts_import_sym(b[1]), origin, dict(extra, role="import"))
        b = binders[0]
        if b[0] == "fsym":
            sym = b[1]
            return an.sym_targets(("sym", an.path, sym[S_NAME], sym[S_KIND], sym[S_LINE], sym[S_COL], sym[S_END_COL]), origin, extra)
        if b[0] == "def":
            pos = _def_name_pos(f, b[1])
            if pos is None:
                return []
            kind = "class" if isinstance(b[1], ast.ClassDef) else "def"
            ci = f.class_by_node.get(b[1])
            if ci is not None and ci.model:
                extra["model"] = ci.model
            return [Target(PYSYM, name=node.id, path=an.path, line=pos[0], col=pos[1], end_col=pos[2], origin=origin, extra=dict(extra, symbol_kind=kind))]
        pos_node = b[2] if b[0] in ("gvar", "cvar") else None
        if pos_node is None:
            return []
        l0 = pos_node.lineno - 1
        return [Target(PYSYM, name=node.id, path=an.path, line=l0, col=f.u16(l0, pos_node.col_offset), end_col=f.u16(l0, pos_node.end_col_offset or 0), origin=origin, extra=dict(extra, symbol_kind="var"))]
    best = None
    for b in binders:
        pos = _binder_pos(f, b)
        if pos is not None and (best is None or pos < best[0]):
            best = (pos, b)
    if best is None:
        return []
    (l0, c0, c1), b = best
    if b[0] == "import":
        return an.sym_targets(an.import_sym(b[1], b[2]), origin, dict(extra, role="import"))
    kind = "param" if b[0] == "param" else "def" if b[0] == "def" else "local"
    return [Target(PYSYM, name=node.id, path=an.path, line=l0, col=c0, end_col=c1, origin=origin, extra=dict(extra, symbol_kind=kind))]


def _binder_pos(f: _File, b: tuple) -> tuple[int, int, int] | None:
    k = b[0]
    if k == "param":
        a = b[1]
        l0 = a.lineno - 1
        return l0, f.u16(l0, a.col_offset), f.u16(l0, a.col_offset + len(a.arg.encode("utf-8")))
    if k in ("expr", "unpack", "for", "aug", "ann", "with"):
        n = b[-1]
        l0 = n.lineno - 1
        return l0, f.u16(l0, n.col_offset), f.u16(l0, n.end_col_offset or 0)
    if k == "def":
        return _def_name_pos(f, b[1])
    if k == "import":
        a = b[1]
        if getattr(a, "lineno", None) is None:
            return None
        l0 = a.lineno - 1
        name = a.asname or a.name.split(".")[0]
        if a.asname:
            return l0, f.u16(l0, a.end_col_offset - len(name)), f.u16(l0, a.end_col_offset)
        return l0, f.u16(l0, a.col_offset), f.u16(l0, a.col_offset + len(name))
    if k == "except":
        h = b[1]
        l0 = h.lineno - 1
        line = f.lines[l0]
        i = line.find(b" as ", h.col_offset)
        j = line.find(h.name.encode("utf-8"), i + 4) if i >= 0 else -1
        if j < 0:
            return None
        return l0, f.u16(l0, j), f.u16(l0, j + len(h.name))
    return None


def _text(f: _File, node: ast.AST) -> str | None:
    if getattr(node, "lineno", None) is None or node.lineno != node.end_lineno:  # type: ignore[attr-defined]
        return None
    raw = f.lines[node.lineno - 1][node.col_offset : node.end_col_offset]  # type: ignore[attr-defined]
    text = raw.decode("utf-8", "replace")
    return text if len(text) <= 80 else text[:79] + "…"


# ---------------------------------------------------------------------------
# public API


def targets_at(ctx: Any) -> list[Target]:
    """Targets for the token at ``(ctx.line, ctx.col)`` of a Python file, best first."""
    reg = ctx.registry
    if reg is None:
        return []
    _use_registry(reg)
    source = ctx.source
    facts = ctx.facts
    line0 = ctx.line
    an = _analyzer(reg, ctx.path, source, facts, line0)
    an.reset_budget()
    f = an.f
    if not 0 <= line0 < len(f.lines):
        return []
    lt = _LineTokens(an, line0, facts)
    toks = lt.collect()
    comment = _comment_start(f, line0, lt.strings)
    col = ctx.col
    if comment is not None:
        if col >= comment:
            return []
        toks = [t for t in toks if t.c0 < comment]
    tok = _pick(toks, col)
    if tok is None:
        return []
    for s0, s1 in lt.strings:  # in a string without a role: nothing, never a neighbour
        if s0 <= col < s1 and not (s0 <= tok.c0 and tok.c1 <= s1):
            return []
    return _resolve(an, lt, tok)


def member_usages(registry: Any, path: str, source: bytes, name: str) -> list[tuple[int, int, int, list[str] | None, str]]:
    """All usages of attribute / method / field ``name`` in a Python file, sorted:
    ``(line, col, end_col, receiver_models, kind)``; receiver_models are the
    models the receiver was inferred to be (None: unknown). Kinds:

    * "attr": ``x.name`` read or written; "call": ``x.name(...)``;
      "super": ``super().name(...)`` (receiver: the current model);
    * "string": a segment equal to ``name`` of a field path / field name /
      order string in code (``mapped``, ``filtered``, ``sorted``, ``grouped``,
      domains, ``read``, ``_read_group`` specs, ``order=``...); the range covers
      that segment and the receiver is the model owning it;
    * "key": a key of field values (``create``/``write``/``update``/
      ``copy(default=)``/``new``, ``Command.create``, ``(0, 0, vals)``,
      ``vals['name']``, ``'name' in vals``...) or a ``default_<name>`` context
      key / ``with_context`` keyword (range: the ``name`` part).

    Strings already in the facts refs (``related=``, ``@api.depends``, field
    ``domain=``...) are not repeated. Returns at once when ``name`` is not in
    ``source``."""
    bname = name.encode("utf-8")
    if not name or bname not in source:
        return []
    _use_registry(registry)
    an = _analyzer(registry, path, source)
    an.reset_budget()
    f = an.f
    if f.tree is None:
        return []
    lines = sorted({f.line_of_offset(m.start()) for m in re.finditer(re.escape(bname), source)})
    units: dict[int, _Unit] = {}
    for l0 in lines:
        u = f.unit_at(l0)
        if u is not None:
            units.setdefault(id(u), u)
    out: list[tuple[int, int, int, list[str] | None, str]] = []
    cand = [l0 + 1 for l0 in lines]
    for u in units.values():
        for node in _walk_candidate_lines(u, cand):
            if isinstance(node, ast.Attribute):
                if node.attr == name:
                    hit = _attr_usage(an, node, u)
                    if hit is not None:
                        out.append(hit)
            elif isinstance(node, ast.Constant):
                if isinstance(node.value, str) and name in node.value:
                    out.extend(_string_usages(an, node, u, name))
            elif isinstance(node, ast.keyword):
                if node.arg == "default_" + name:
                    call = u.parents.get(node)
                    if isinstance(call, ast.Call) and _call_name(call.func) == "with_context":
                        models = an.consumer_models(call, u)
                        l0 = node.lineno - 1
                        b0 = node.col_offset + 8
                        out.append((l0, f.u16(l0, b0), f.u16(l0, b0 + len(bname)), list(models) if models else None, "key"))
    out.sort(key=lambda h: (h[0], h[1]))
    return out


def _walk_candidate_lines(u: _Unit, cand: list[int]) -> Iterator[ast.AST]:
    """Nodes of the unit that may lie on one of the (sorted, 1-based) lines."""
    def hit(lo: int, hi: int) -> bool:
        i = bisect_left(cand, lo)
        return i < len(cand) and cand[i] <= hi

    if u.header and isinstance(u.root, ast.ClassDef):
        stack: list[ast.AST] = list(u.root.decorator_list) + list(u.root.bases) + list(u.root.keywords)
    else:
        stack = [u.root]
    while stack:
        n = stack.pop()
        yield n
        for c in ast.iter_child_nodes(n):
            ln = getattr(c, "lineno", None)
            if ln is not None and not hit(_start(c), getattr(c, "end_lineno", None) or ln):
                continue
            stack.append(c)


def _attr_usage(an: _Analyzer, node: ast.Attribute, u: _Unit) -> tuple | None:
    f = an.f
    l0 = (node.end_lineno or node.lineno) - 1
    b1 = node.end_col_offset or 0
    c0, c1 = f.u16(l0, b1 - len(node.attr.encode("utf-8"))), f.u16(l0, b1)
    parent = u.parents.get(node)
    is_call = isinstance(parent, ast.Call) and parent.func is node
    if _is_super_call(node.value):
        _fn, ci = an.enclosing_method(node, u)
        return (l0, c0, c1, [ci.model] if ci is not None and ci.model else None, "super")
    rt = an.infer(node.value, u)
    if rt is None:
        models = None
    elif rt[0] == "rs":
        models = list(rt[1])
    elif rt[0] == "super":
        models = [rt[1].model] if rt[1].model else None
        return (l0, c0, c1, models, "super")
    else:
        return None  # not a recordset (module, env, scalar, dict...)
    return (l0, c0, c1, models, "call" if is_call else "attr")


def _string_usages(an: _Analyzer, node: ast.Constant, u: _Unit, name: str) -> list[tuple]:
    role = an.classify_string(node, u)
    if role is None:
        return []
    span = _str_content(an.f, node)
    if span is None:
        return []
    f = an.f
    l0, b0, b1, exact = span
    mode, models, _rname = role
    value = node.value
    out = []
    if mode in ("key", "default"):
        key = value[8:] if mode == "default" else value
        if key == name:
            s0 = b0 + (8 if mode == "default" else 0)
            owners = [m for m in models if an.member(m, name)[0] == "field"] if models else []
            out.append((l0, f.u16(l0, s0), f.u16(l0, b1), owners or (list(models) if models else None), "key"))
        return out
    if mode not in ("path", "name", "order"):
        return out
    if mode == "order":
        items = _segments(value, ",", b0, b1, exact)
    elif mode == "name":
        spec, k = _spec_field(value)
        s0 = b0 + len(value[:k].encode("utf-8")) if exact else b0
        items = [(0, spec, s0, s0 + len(spec.encode("utf-8")) if exact else b1)]
    else:
        items = [(0, value, b0, b1)]
    for _i, item, s0, s1 in items:
        path = item.split()[0].split(":")[0] if mode == "order" else item
        if name not in path.split("."):
            continue
        pos = 0
        for j, seg in enumerate(path.split(".")):
            e0 = s0 + pos if exact else s0
            e1 = e0 + len(seg.encode("utf-8")) if exact else s1
            pos += len(seg.encode("utf-8")) + 1
            if seg != name:
                continue
            owners = None
            if models:
                found = []
                for m in models:
                    res = an.walk(m, path)
                    if j < len(res) and res[j][1] and all(r[2] for r in res[:j]):
                        found.append(res[j][1])
                owners = list(dict.fromkeys(found)) or None
            out.append((l0, f.u16(l0, e0), f.u16(l0, e1), owners, "string"))
    return out


def _function_at(an: _Analyzer, line0: int) -> tuple[ast.AST | None, _Unit | None]:
    """Innermost def containing 0-based ``line0`` (decorator lines included)."""
    u = an.f.unit_at(line0)
    if u is None:
        return None, None
    lineno = line0 + 1
    best = None
    for node in _walk_lines(u.root, lineno, lineno, u.header):
        if isinstance(node, _FUNC) and _start(node) <= lineno <= (node.end_lineno or node.lineno):
            if best is None or _start(node) >= _start(best):
                best = node
    return best, u


def calls_in_function(registry: Any, path: str, source: bytes, line: int) -> list[tuple[str, int, int, int, list[str] | None]]:
    """Method calls inside the function enclosing 0-based ``line``:
    ``(method_name, line, col, end_col, receiver_models or None)``; calls on
    receivers known not to be recordsets are left out."""
    _use_registry(registry)
    an = _analyzer(registry, path, source, registry.file_facts(path), line)
    an.reset_budget()
    fn, u = _function_at(an, line)
    if fn is None or u is None:
        return []
    f = an.f
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        l0 = (func.end_lineno or func.lineno) - 1
        b1 = func.end_col_offset or 0
        c0, c1 = f.u16(l0, b1 - len(func.attr.encode("utf-8"))), f.u16(l0, b1)
        if _is_super_call(func.value):
            _m, ci = an.enclosing_method(func, u)
            out.append((func.attr, l0, c0, c1, [ci.model] if ci is not None and ci.model else None))
            continue
        rt = an.infer(func.value, u)
        if rt is None:
            if func.attr in NON_MODEL_ATTRS:
                continue
            out.append((func.attr, l0, c0, c1, None))
        elif rt[0] == "rs":
            out.append((func.attr, l0, c0, c1, list(rt[1])))
        elif rt[0] == "super":
            out.append((func.attr, l0, c0, c1, [rt[1].model] if rt[1].model else None))
    out.sort(key=lambda c: (c[1], c[2]))
    return out


def enclosing_function(registry: Any, path: str, source: bytes, line: int) -> dict | None:
    """The def enclosing 0-based ``line``: {"model", "class", "name", "line",
    "col", "end_col", "end_line"} (col/end_col = the def name token)."""
    _use_registry(registry)
    an = _analyzer(registry, path, source, registry.file_facts(path), line)
    fn, u = _function_at(an, line)
    if fn is None or u is None:
        return None
    pos = _def_name_pos(an.f, fn)
    if pos is None:
        return None
    _m, ci = an.enclosing_method(fn, u)
    if ci is None:
        n = u.parents.get(fn)
        while n is not None and not isinstance(n, ast.ClassDef):
            n = u.parents.get(n)
        ci = an.f.class_by_node.get(n) if n is not None else u.cls
    return {
        "model": ci.model if ci is not None else None,
        "class": ci.name if ci is not None else None,
        "name": fn.name,  # type: ignore[attr-defined]
        "line": pos[0],
        "col": pos[1],
        "end_col": pos[2],
        "end_line": (fn.end_lineno or fn.lineno) - 1,  # type: ignore[attr-defined]
    }


__all__ = ["targets_at", "member_usages", "calls_in_function", "enclosing_function"]
