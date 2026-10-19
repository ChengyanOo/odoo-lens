"""Python facts extractor.

Structure (imports, module symbols, classes, fields, methods, model attributes)
comes from the AST, visiting only module-level statements and class bodies -
never a full ``ast.walk`` (which would cost ~20 s of CPU on a full Odoo
checkout). References inside method bodies (``env['x']``, ``.ref('x')``,
``.mapped('a.b')``, domain tuples, ``res_model``...) are found with byte-level
regular expressions that start with a literal prefix (fast C scanning).
"""

from __future__ import annotations

import ast
import re
import sys
import warnings
from bisect import bisect_right

from ..facts import (
    CLASSES,
    ERROR,
    IMPORTS,
    KIND,
    KIND_PY,
    MI_ATTRS,
    MI_INHERIT,
    MI_INHERIT_STR,
    MI_INHERITS,
    MI_KIND,
    MI_NAME,
    REF_FIELD,
    REF_METHOD,
    REF_MODEL,
    REF_XMLID,
    REFS,
    SYMBOLS,
)

# Positional parameters of field constructors (after normalization to keywords).
POSITIONAL = {
    "Many2one": ("comodel_name", "string"),
    "One2many": ("comodel_name", "inverse_name", "string"),
    "Many2many": ("comodel_name", "relation", "column1", "column2", "string"),
    "Selection": ("selection", "string"),
    "Reference": ("selection", "string"),
    "Float": ("string", "digits"),
    "Monetary": ("string", "currency_field"),
}
DEFAULT_POSITIONAL = ("string",)
RELATIONAL = frozenset({"Many2one", "One2many", "Many2many"})
MODEL_BASES = {
    "Model": "Model",
    "AbstractModel": "AbstractModel",
    "TransientModel": "TransientModel",
    "BaseModel": "BaseModel",
    "CachedModel": "CachedModel",  # saas-19.1+
}
FRAMEWORK_CLASSES = frozenset({"BaseModel", "AbstractModel", "Model", "TransientModel", "CachedModel"})
# literal model attributes whose value names fields of the model itself
FIELD_NAME_ATTRS = ("_rec_name", "_parent_name", "_fold_name", "_active_name", "_date_name", "_rec_names_search")
METHOD_KWARGS = ("compute", "inverse", "search", "group_expand")
DEC_FIELD_ROLES = {"depends": "depends", "onchange": "onchange", "constrains": "constrains"}
MAX_TEXT = 120


class _Src:
    """Source helper: byte lines, newline offsets, UTF-16 column conversion."""

    __slots__ = ("src", "lines", "ascii", "_nl")

    def __init__(self, src: bytes):
        self.src = src
        self.lines = src.split(b"\n")
        self.ascii = src.isascii()
        self._nl: list[int] | None = None

    def col(self, line0: int, bcol: int) -> int:
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
        """Byte offset in the file -> (line0, byte col)."""
        if self._nl is None:
            self._nl = [m.start() for m in re.finditer(b"\n", self.src)]
        line = bisect_right(self._nl, offset - 1)
        start = self._nl[line - 1] + 1 if line > 0 else 0
        return line, offset - start

    def segment(self, node: ast.AST, limit: int = MAX_TEXT) -> str:
        l0 = node.lineno - 1  # type: ignore[attr-defined]
        l1 = (node.end_lineno or node.lineno) - 1  # type: ignore[attr-defined]
        c0 = node.col_offset  # type: ignore[attr-defined]
        c1 = node.end_col_offset  # type: ignore[attr-defined]
        if l0 == l1:
            raw = self.lines[l0][c0:c1]
        else:
            parts = [self.lines[l0][c0:]]
            for i in range(l0 + 1, min(l1, l0 + 8)):
                parts.append(self.lines[i])
            if l1 < l0 + 8:
                parts.append(self.lines[l1][:c1])
            raw = b" ".join(p.strip() for p in parts)
        text = raw.decode("utf-8", "replace")
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        return text

    def string_span(self, node: ast.Constant) -> tuple[int, int, int] | None:
        """(line0, col16, end_col16) of the *content* of a single-line str literal."""
        if node.lineno != node.end_lineno:
            return None
        l0 = node.lineno - 1
        raw = self.lines[l0][node.col_offset : node.end_col_offset]
        q = 0
        while q < len(raw) and raw[q : q + 1] not in (b"'", b'"'):
            q += 1
        if q >= len(raw):
            return None
        quote = raw[q : q + 3] if raw[q : q + 3] in (b"'''", b'"""') else raw[q : q + 1]
        if not raw.endswith(quote) or len(raw) < q + 2 * len(quote):
            return None  # implicit concatenation or odd literal
        start = node.col_offset + q + len(quote)
        end = node.end_col_offset - len(quote)
        return l0, self.col(l0, start), self.col(l0, end)


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _literal(node: ast.AST):
    """Fast literal conversion; raises ValueError for non-literals."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if isinstance(v, bytes):
            return v.decode("utf-8", "replace")
        raise ValueError
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_literal(e) for e in node.elts]
    if isinstance(node, ast.Dict):
        out = {}
        for k, v in zip(node.keys, node.values):
            if k is None:
                raise ValueError
            key = _literal(k)
            if not isinstance(key, (str, int, float, bool)) and key is not None:
                raise ValueError
            out[key] = _literal(v)
        return out
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
        v = node.operand.value
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return -v
    raise ValueError


def _str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _str_nodes(node: ast.AST | None) -> list[ast.Constant]:
    """str constants of a literal str / list / tuple node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


class _Extractor:
    def __init__(self, path: str, src: bytes):
        self.path = path
        self.s = _Src(src)
        self.imports: list[tuple] = []
        self.symbols: list[tuple] = []
        self.classes: list[tuple] = []
        self.refs: list[tuple] = []
        self.class_ranges: list[tuple[int, int, str | None]] = []  # (line0, end_line0, model) for regex refs
        self.field_classes: set[str] = set()  # names imported from odoo.fields
        self.is_framework_models = path.replace("\\", "/").endswith(
            ("odoo/models.py", "odoo/orm/models.py", "odoo/orm/models_transient.py", "odoo/orm/models_cached.py")
        )

    # -- helpers -----------------------------------------------------------------

    def ref_str(self, kind: str, node: ast.Constant, ctx: tuple, value: str | None = None) -> None:
        span = self.s.string_span(node)
        if span is None:
            return
        self.refs.append((kind, sys.intern(node.value if value is None else value), span[0], span[1], span[2], ctx))

    def ref_parts(self, kind: str, node: ast.Constant, ctx: tuple, sep: str, strip: str = "") -> None:
        """One ref per ``sep``-separated item of a str literal, with precise ranges."""
        span = self.s.string_span(node)
        value = node.value
        if span is None:
            return
        line, col, end = span
        exact = (end - col) == len(value)  # no escapes / no wide chars
        pos = 0
        for part in value.split(sep):
            start = pos
            pos += len(part) + len(sep)
            item = part.strip()
            lead = len(part) - len(part.lstrip())
            if strip and item[:1] in strip:
                item = item[1:]
                lead += 1
            if not item:
                continue
            if sep == "," and kind == REF_FIELD and " " in item:  # "_order" items: "date desc"
                item = item.split()[0]
            if exact:
                c0 = col + start + lead
                self.refs.append((kind, item, line, c0, c0 + len(item), ctx))
            else:
                self.refs.append((kind, item, line, col, end, ctx))

    def name_pos(self, node: ast.AST, keyword: bytes) -> tuple[int, int, int]:
        """Position of the name after 'class'/'def' in a ClassDef/FunctionDef."""
        l0 = node.lineno - 1  # type: ignore[attr-defined]
        line = self.s.lines[l0]
        name = node.name.encode("utf-8")  # type: ignore[attr-defined]
        i = line.find(keyword, node.col_offset)  # type: ignore[attr-defined]
        j = line.find(name, i + len(keyword)) if i >= 0 else -1
        if j < 0:
            c = node.col_offset  # type: ignore[attr-defined]
            return l0, self.s.col(l0, c), self.s.col(l0, c)
        return l0, self.s.col(l0, j), self.s.col(l0, j + len(name))

    def node_pos(self, node: ast.AST) -> tuple[int, int, int]:
        l0 = node.lineno - 1  # type: ignore[attr-defined]
        end = node.end_col_offset if node.end_lineno == node.lineno else len(self.s.lines[l0])  # type: ignore[attr-defined]
        return l0, self.s.col(l0, node.col_offset), self.s.col(l0, end)  # type: ignore[attr-defined]

    # -- module level --------------------------------------------------------------

    def run(self, tree: ast.Module) -> None:
        self.module_body(tree.body, top=True)
        self.setattr_fields(tree.body)
        self.regex_refs()

    def setattr_fields(self, body: list[ast.stmt]) -> None:
        """``global_ = fields.Boolean(...)`` + ``setattr(IrRule, 'global', global_)``:
        a field named by a Python keyword, attached to the class after its body."""
        if not any(isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == "setattr" for n in body):
            return
        values: dict[str, tuple[ast.stmt, ast.Call, str]] = {}
        for node in body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                ftype = self.field_type(node.value)
                if ftype is not None:
                    values[node.targets[0].id] = (node, node.value, ftype)  # type: ignore[assignment]
        index = {c[0]: i for i, c in enumerate(self.classes)}
        for node in body:
            call = node.value if isinstance(node, ast.Expr) else None
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "setattr" and len(call.args) == 3):
                continue
            cls_node, key, val = call.args
            if not (isinstance(cls_node, ast.Name) and isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(val, ast.Name)):
                continue
            i = index.get(cls_node.id)
            if i is None or val.id not in values or not self.classes[i][6]:
                continue
            stmt, fcall, ftype = values[val.id]
            cls = self.classes[i]
            model = cls[6].get(MI_NAME) or (cls[6].get(MI_INHERIT) or [None])[0]
            field = self.field(key, stmt, fcall, ftype, model)
            l0, c0, c1 = self.node_pos(stmt.targets[0])  # type: ignore[attr-defined]  # the field's own statement
            field = (field[0], l0, c0, c1) + field[4:]
            self.classes[i] = cls[:7] + (cls[7] + [field],) + cls[8:]

    def module_body(self, body: list[ast.stmt], top: bool) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    l0, c0, c1 = self.node_pos(a)
                    self.imports.append((a.name, None, a.asname, 0, l0, c0, c1))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for a in node.names:
                    l0, c0, c1 = self.node_pos(a)
                    self.imports.append((mod, a.name, a.asname, node.level or 0, l0, c0, c1))
                    # field classes imported by name: odoo.fields (any version) or the
                    # 19+ ORM modules (from .fields_misc import Id, ...)
                    if a.name != "*" and a.name[:1].isupper() and "field" in mod.rsplit(".", 1)[-1]:
                        self.field_classes.add(a.asname or a.name)
            elif isinstance(node, ast.ClassDef):
                self.class_def(node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                l0, c0, c1 = self.name_pos(node, b"def")
                self.symbols.append((node.name, "def", l0, c0, c1, (node.end_lineno or node.lineno) - 1))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    for n in ([t] if isinstance(t, ast.Name) else t.elts if isinstance(t, ast.Tuple) else []):
                        if isinstance(n, ast.Name):
                            l0, c0, c1 = self.node_pos(n)
                            self.symbols.append((n.id, "var", l0, c0, c1, (node.end_lineno or node.lineno) - 1))
            elif isinstance(node, (ast.If, ast.Try)) or (hasattr(ast, "TryStar") and isinstance(node, ast.TryStar)):
                self.module_body(node.body, top=False)
                for h in getattr(node, "handlers", []):
                    self.module_body(h.body, top=False)
                self.module_body(getattr(node, "orelse", []), top=False)
                self.module_body(getattr(node, "finalbody", []), top=False)

    # -- classes ---------------------------------------------------------------------

    def class_def(self, node: ast.ClassDef) -> None:
        l0, c0, c1 = self.name_pos(node, b"class")
        end_line = (node.end_lineno or node.lineno) - 1
        self.symbols.append((node.name, "class", l0, c0, c1, end_line))
        bases = tuple(_dotted(b.value if isinstance(b, ast.Subscript) else b) or "?" for b in node.bases)
        kind = None
        for b in bases:
            last = b.rsplit(".", 1)[-1]
            if last in MODEL_BASES:
                kind = MODEL_BASES[last]
                break
        meta: dict = {}
        meta_nodes: dict[str, ast.AST] = {}
        for stmt in node.body:
            targets: list[str] = []
            value = None
            if isinstance(stmt, ast.Assign):
                # also chained assignments: _name = _description = 'x'
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                value = stmt.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                targets, value = [stmt.target.id], stmt.value
            for target in targets:
                if target[0] != "_" or value is None:
                    continue
                if isinstance(value, ast.Name) and value.id in meta:  # _inherit = _name
                    meta[target] = meta[value.id]
                    meta_nodes[target] = meta_nodes[value.id]
                    continue
                try:
                    meta[target] = _literal(value)
                    meta_nodes[target] = value
                except ValueError:
                    pass
        is_framework = self.is_framework_models and node.name in FRAMEWORK_CLASSES
        has_meta = "_name" in meta or "_inherit" in meta
        model_info = None
        cls_model = None
        if has_meta or kind is not None or is_framework:
            name = meta.get("_name") if isinstance(meta.get("_name"), str) else None
            inherit = meta.get("_inherit")
            inherit_str = isinstance(inherit, str)
            if isinstance(inherit, str):
                inherit = [inherit]
            elif isinstance(inherit, list):
                inherit = [i for i in inherit if isinstance(i, str)]
            else:
                inherit = []
            inherits = meta.get("_inherits")
            if not isinstance(inherits, dict):
                inherits = {}
            inherits = {k: v for k, v in inherits.items() if isinstance(k, str) and isinstance(v, str)}
            attrs = {k: v for k, v in meta.items() if k not in ("_name", "_inherit", "_inherits") and _small(v)}
            model_info = {
                MI_NAME: name,
                MI_INHERIT: inherit,
                MI_INHERITS: inherits,
                MI_KIND: "framework" if is_framework else kind,
                MI_ATTRS: attrs,
                MI_INHERIT_STR: inherit_str,
            }
            cls_model = name or (inherit[0] if inherit else None)
            self.model_refs(meta_nodes, cls_model)
        self.class_ranges.append((l0, end_line, cls_model))
        fields: list[tuple] = []
        methods: list[tuple] = []
        attrs_list: list[tuple] = []
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(self.method(stmt, cls_model))
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                value = stmt.value
                if value is None:
                    continue
                if len(targets) == 1 and _is_locals_subscript(targets[0]) and model_info is not None:
                    # locals()['global'] = fields.Boolean(...): a field named by a keyword
                    ftype = self.field_type(value)
                    key = targets[0].slice  # type: ignore[attr-defined]
                    if ftype is not None:
                        fields.append(self.field(key, stmt, value, ftype, cls_model))  # type: ignore[arg-type]
                        continue
                if len(targets) == 1 and isinstance(targets[0], ast.Name):
                    tname = targets[0]
                    ftype = self.field_type(value)
                    if ftype is not None and model_info is not None:
                        fields.append(self.field(tname, stmt, value, ftype, cls_model))
                        continue
                    l0_, c0_, c1_ = self.node_pos(tname)
                    akind = "attr"
                    if isinstance(value, ast.Call):
                        fn = _dotted(value.func) or ""
                        last = fn.rsplit(".", 1)[-1]
                        if last == "Constraint":
                            akind = "constraint"
                        elif last in ("Index", "UniqueIndex"):
                            akind = "index"
                    elif tname.id == "_sql_constraints":
                        akind = "sql_constraints"
                    attrs_list.append((tname.id, l0_, c0_, c1_, akind, self.s.segment(value)))
                else:
                    for t in targets:
                        for n in ([t] if isinstance(t, ast.Name) else t.elts if isinstance(t, ast.Tuple) else []):
                            if isinstance(n, ast.Name):
                                l0_, c0_, c1_ = self.node_pos(n)
                                attrs_list.append((n.id, l0_, c0_, c1_, "attr", self.s.segment(stmt.value)))
        doc = _doc(node)
        self.classes.append((node.name, l0, c0, c1, end_line, bases, model_info, fields, methods, attrs_list, doc))

    def model_refs(self, meta_nodes: dict[str, ast.AST], cls_model: str | None) -> None:
        n = meta_nodes.get("_name")
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            self.ref_str(REF_MODEL, n, ("_name",))
        for c in _str_nodes(meta_nodes.get("_inherit")):
            self.ref_str(REF_MODEL, c, ("_inherit",))
        inh = meta_nodes.get("_inherits")
        if isinstance(inh, ast.Dict):
            for k, v in zip(inh.keys, inh.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    self.ref_str(REF_MODEL, k, ("_inherits",))
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    self.ref_str(REF_FIELD, v, ("_inherits", cls_model))
        for attr in FIELD_NAME_ATTRS:
            for c in _str_nodes(meta_nodes.get(attr)):
                self.ref_str(REF_FIELD, c, (attr, cls_model))
        order = meta_nodes.get("_order")
        if isinstance(order, ast.Constant) and isinstance(order.value, str):
            self.ref_parts(REF_FIELD, order, ("_order", cls_model), ",")

    def field_type(self, value: ast.AST) -> str | None:
        if not isinstance(value, ast.Call):
            return None
        fn = value.func
        if isinstance(fn, ast.Attribute):
            base = _dotted(fn.value)
            if base in ("fields", "odoo.fields", "openerp.fields") and fn.attr[:1].isupper():
                return fn.attr
            return None
        if isinstance(fn, ast.Name) and fn.id in self.field_classes:
            return fn.id
        return None

    def field(self, target: ast.AST, stmt: ast.stmt, call: ast.Call, ftype: str, cls_model: str | None) -> tuple:
        if isinstance(target, ast.Constant):  # locals()['name']: position of the name's content
            span = self.s.string_span(target)
            l0, c0, c1 = span if span is not None else self.node_pos(target)
            fname = target.value
        else:
            l0, c0, c1 = self.node_pos(target)
            fname = target.id  # type: ignore[attr-defined]
        args: dict = {}
        arg_nodes: dict[str, ast.AST] = {}
        names = POSITIONAL.get(ftype, DEFAULT_POSITIONAL)
        for i, a in enumerate(call.args):
            if i < len(names) and not isinstance(a, ast.Starred):
                arg_nodes[names[i]] = a
        for kw in call.keywords:
            if kw.arg:
                arg_nodes[kw.arg] = kw.value
        for k, v in arg_nodes.items():
            try:
                lit = _literal(v)
                args[k] = lit if _small(lit) else ("=", self.s.segment(v))
            except ValueError:
                args[k] = ("=", self.s.segment(v))
        comodel = None
        cm = arg_nodes.get("comodel_name")
        if ftype in RELATIONAL or cm is not None:
            comodel = _str(cm)
            if comodel and isinstance(cm, ast.Constant):
                self.ref_str(REF_MODEL, cm, ("comodel",))
        # string references held by field arguments
        rel = arg_nodes.get("related")
        if isinstance(rel, ast.Constant) and isinstance(rel.value, str):
            self.ref_str(REF_FIELD, rel, ("related", cls_model))
        for k in METHOD_KWARGS:
            v = arg_nodes.get(k)
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                self.ref_str(REF_METHOD, v, (k, cls_model))
        sel = arg_nodes.get("selection")
        if isinstance(sel, ast.Constant) and isinstance(sel.value, str):
            self.ref_str(REF_METHOD, sel, ("selection", cls_model))
        default = arg_nodes.get("default")
        if isinstance(default, ast.Constant) and isinstance(default.value, str) and default.value.startswith("_") and ftype not in ("Char", "Text", "Html", "Selection"):
            self.ref_str(REF_METHOD, default, ("default", cls_model))
        cf = arg_nodes.get("currency_field")
        if isinstance(cf, ast.Constant) and isinstance(cf.value, str):
            self.ref_str(REF_FIELD, cf, ("currency_field", cls_model))
        inv = arg_nodes.get("inverse_name")
        if isinstance(inv, ast.Constant) and isinstance(inv.value, str):
            self.ref_str(REF_FIELD, inv, ("inverse_name", comodel))
        groups = arg_nodes.get("groups")
        if isinstance(groups, ast.Constant) and isinstance(groups.value, str):
            self.ref_parts(REF_XMLID, groups, ("groups",), ",", strip="!")
        domain = arg_nodes.get("domain")
        if isinstance(domain, (ast.List, ast.Tuple)):
            self.domain_refs(domain, comodel)
        end_line = (stmt.end_lineno or stmt.lineno) - 1
        return (fname, l0, c0, c1, end_line, ftype, sys.intern(comodel) if comodel else None, args)

    def domain_refs(self, node: ast.AST, model: str | None) -> None:
        for e in getattr(node, "elts", []):
            if isinstance(e, (ast.Tuple, ast.List)) and len(e.elts) == 3:
                first = e.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    self.ref_str(REF_FIELD, first, ("domain", model))

    def method(self, node: ast.FunctionDef | ast.AsyncFunctionDef, cls_model: str | None) -> tuple:
        l0, c0, c1 = self.name_pos(node, b"def")
        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                name = _dotted(dec.func) or "?"
                args = []
                for a in list(dec.args) + [kw.value for kw in dec.keywords if kw.arg in ("route", "routes")]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        args.append(a.value)
                    elif isinstance(a, (ast.List, ast.Tuple)) and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in a.elts):
                        args.extend(e.value for e in a.elts)  # @route(['/a', '/b'])
                    else:
                        args.append("?")
                decorators.append((name, tuple(args)))
                short = name.rsplit(".", 1)[-1]
                role = DEC_FIELD_ROLES.get(short)
                if role and cls_model is not None:
                    for a in dec.args:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            self.ref_str(REF_FIELD, a, (role, cls_model))
            else:
                decorators.append((_dotted(dec) or "?", ()))
        end_line = (node.end_lineno or node.lineno) - 1
        params = self.params(node, l0)
        body_src = b"\n".join(self.s.lines[l0 : end_line + 1])
        # super() reaching THIS method (not super().other_method(), not a comment's "super(")
        calls_super = b"super(" in body_src and re.search(rb"super\s*\([^)]*\)\s*\.\s*" + re.escape(node.name.encode()) + rb"\b", body_src) is not None
        returns = self.s.segment(node.returns, 80) if node.returns is not None else None
        return (node.name, l0, c0, c1, end_line, tuple(decorators), params, calls_super, _doc(node), returns)

    def params(self, node: ast.AST, l0: int) -> str:
        """Parameter list text between the parentheses of a def header."""
        lines = self.s.lines
        last = node.body[0].lineno - 1 if node.body else l0  # type: ignore[attr-defined]
        start = lines[l0].find(b"(", node.col_offset)  # type: ignore[attr-defined]
        if start < 0:
            return ""
        chunks: list[bytes] = []
        depth = 0
        i, j = l0, start
        while i <= last and i < len(lines):
            line = lines[i]
            seg = j
            while j < len(line):
                ch = line[j]
                if ch in b"([{":
                    depth += 1
                elif ch in b")]}":
                    depth -= 1
                    if depth == 0:
                        chunks.append(line[seg:j])
                        text = b" ".join(c.strip() for c in chunks)[1:]  # drop the opening "("
                        return " ".join(text.decode("utf-8", "replace").split())[:200]
                j += 1
            chunks.append(line[seg:])
            i += 1
            j = 0
        return ""

    # -- regex refs in bodies ------------------------------------------------------------

    def class_model_at(self, line0: int) -> str | None:
        for start, end, model in self.class_ranges:
            if start <= line0 <= end:
                return model
        return None

    def add_regex_ref(self, kind: str, value: str, start: int, end: int, ctx: tuple) -> None:
        l0, b0 = self.s.pos(start)
        b1 = b0 + (end - start)
        self.refs.append((kind, sys.intern(value), l0, self.s.col(l0, b0), self.s.col(l0, b1), ctx))

    def regex_refs(self) -> None:
        src = self.s.src
        if b"env[" in src or b"env [" in src:
            for m in RX_ENV.finditer(src):
                self.add_regex_ref(REF_MODEL, m.group(2).decode(), m.start(2), m.end(2), ("env",))
        if b"res_model" in src:
            for m in RX_RES_MODEL.finditer(src):
                self.add_regex_ref(REF_MODEL, m.group(2).decode(), m.start(2), m.end(2), ("res_model",))
        for rx, role in RX_XMLIDS:
            for m in rx.finditer(src):
                value = m.group("v").decode("utf-8", "replace")
                if role == "groups" and ("," in value or "!" in value):
                    base = m.start("v")
                    pos = 0
                    for part in value.split(","):
                        item = part.strip()
                        lead = len(part) - len(part.lstrip())
                        if item.startswith("!"):
                            item = item[1:]
                            lead += 1
                        if item:
                            s = base + len(value[: pos + lead].encode())
                            self.add_regex_ref(REF_XMLID, item, s, s + len(item.encode()), (role,))
                        pos += len(part) + 1
                else:
                    self.add_regex_ref(REF_XMLID, value, m.start("v"), m.end("v"), (role,))
        # Field-path strings in method bodies (mapped('a.b'), domain tuples...) are
        # not indexed: patterns without a literal prefix cost as much as parsing.
        # Query-time analysis finds them precisely on the few candidate files.


RX_ENV = re.compile(rb"""env\s*\[\s*(['"])([a-z0-9_]+(?:\.[a-z0-9_]+)+)\1\s*\]""")
RX_RES_MODEL = re.compile(rb"""res_model['"]?\s*[:=]\s*(['"])([a-z0-9_]+(?:\.[a-z0-9_]+)+)\1""")
_XID = rb"""(['"])(?P<v>[A-Za-z0-9_]+\.[A-Za-z0-9_.\-]+)\1"""
RX_XMLIDS = [
    (re.compile(rb"""\.ref\(\s*""" + _XID), "ref"),
    (re.compile(rb"""\._xmlid_(?:to_res_id|lookup|to_res_model_res_id)\(\s*""" + _XID), "ref"),
    (re.compile(rb"""\._for_xml_id\(\s*""" + _XID), "ref"),
    (re.compile(rb"""\.(?:_render_qweb_pdf|_render_qweb_html|_render_qweb_text|_render_template|_render|render)\(\s*""" + _XID), "template"),
    (re.compile(rb"""\.has_group\(\s*(['"])(?P<v>!?[A-Za-z0-9_]+\.[A-Za-z0-9_.]+)\1"""), "groups"),
    (re.compile(rb"""\.user_has_groups\(\s*(['"])(?P<v>[A-Za-z0-9_.,! ]+)\1"""), "groups"),
]


def _is_locals_subscript(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "locals"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    )


def _small(v) -> bool:
    """Keep literal values small in facts (big selection lists are fine, blobs are not)."""
    if isinstance(v, str):
        return len(v) <= 400
    if isinstance(v, (list, dict)):
        return len(repr(v)) <= 2000
    return True


def _doc(node: ast.AST) -> str | None:
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        for line in first.value.value.strip().splitlines():
            line = line.strip()
            if line:
                return line[:200]
    return None


_BLOCK_AFTER = re.compile(r"expected an indented block after .* on line (\d+)")
MAX_REPAIRS = 12


def _strict_parse(src: bytes, path: str) -> ast.Module:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(src, filename=path)


def _indent_width(raw: bytes) -> int:
    return len(raw[: len(raw) - len(raw.lstrip(b" \t"))].expandtabs(8))


def parse_tolerant(src: bytes, path: str = "<unknown>") -> tuple[ast.Module | None, tuple | None]:
    """``(tree, error)``: the AST of ``src`` and None, or on a syntax error the
    first error ``(message, line0, col0)`` and the AST of ``src`` with the
    offending lines neutralised - replaced by ``pass`` (``if 1:`` when they head
    an indented block) or blanked, line numbers and the other lines untouched -
    so that a mid-edit buffer keeps its structure (None when beyond repair)."""
    try:
        return _strict_parse(src, path), None
    except SyntaxError as exc:
        err: SyntaxError = exc
    except (ValueError, MemoryError, RecursionError) as exc:
        return None, (str(exc), 0, 0)
    first = (err.msg or "invalid syntax", max(0, (err.lineno or 1) - 1), max(0, (err.offset or 1) - 1))
    lines = src.split(b"\n")
    tries: dict[int, int] = {}
    for _ in range(MAX_REPAIRS):
        msg = err.msg or ""
        m = _BLOCK_AFTER.match(msg)
        ln = int(m.group(1)) - 1 if m else (err.lineno or 0) - 1
        if not 0 <= ln < len(lines):
            break
        n = tries.get(ln, 0)
        tries[ln] = n + 1
        if n >= 2:
            break
        raw = lines[ln]
        width = _indent_width(raw)
        if "unexpected indent" in msg:  # blank the line and the block it starts
            lines[ln] = b""
            for j in range(ln + 1, len(lines)):
                r = lines[j]
                if r.strip():
                    if _indent_width(r) < width:
                        break
                    lines[j] = b""
        elif n == 0:
            indent = raw[: len(raw) - len(raw.lstrip(b" \t"))]
            nxt = next((r for r in lines[ln + 1 :] if r.strip() and not r.lstrip().startswith(b"#")), None)
            lines[ln] = indent + (b"if 1:" if nxt is not None and _indent_width(nxt) > width else b"pass")
        else:
            lines[ln] = b""
        try:
            return _strict_parse(b"\n".join(lines), path), first
        except SyntaxError as exc:
            err = exc
        except (ValueError, MemoryError, RecursionError):
            break
    return None, first


def extract(path: str, src: bytes) -> dict:
    facts: dict = {KIND: KIND_PY, ERROR: None, IMPORTS: [], SYMBOLS: [], CLASSES: [], REFS: []}
    tree, error = parse_tolerant(src, path)
    facts[ERROR] = error
    if tree is None:
        return facts
    ex = _Extractor(path, src)
    ex.run(tree)
    facts[IMPORTS] = ex.imports
    facts[SYMBOLS] = ex.symbols
    facts[CLASSES] = ex.classes
    facts[REFS] = ex.refs
    return facts
