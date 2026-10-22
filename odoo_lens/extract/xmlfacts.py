# expat reports only tag starts (CurrentByteIndex): attribute value ranges come from the raw tag

from __future__ import annotations

import re
import sys
import xml.parsers.expat as expat

from ..facts import (
    ERROR,
    KIND,
    KIND_XML,
    RECORDS,
    REF_FIELD,
    REF_METHOD,
    REF_MODEL,
    REF_QWEB,
    REF_XMLID,
    REFS,
    ROOT,
    X_COL,
    X_END_COL,
    X_END_LINE,
    X_INFO,
    X_LINE,
    X_MODEL,
)
from .common import SrcIndex

TAG_RE = re.compile(rb"""<[^>"']*(?:(?:"[^"]*"|'[^']*')[^>"']*)*>""")
REF_CALL_RE = re.compile(r"""\bref\(\s*(['"])([A-Za-z0-9_.\-]+)\1""")
PERCENT_RE = re.compile(r"""%\(([A-Za-z0-9_.\-]+)\)[ds]""")
XPATH_FIELD_RE = re.compile(r"""field\[@name\s*=\s*(['"])([A-Za-z0-9_]+)\1\]""")
XPATH_STEP_RE = re.compile(r"""/+([A-Za-z_][\w\-]*)""")
DOMAIN_FIELD_RE = re.compile(r"""[(\[]\s*(['"])([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*)\1\s*,""")
# leaf only: ('path', 'op', ...); value tuples like ('done', 'cancel') must not match
DOMAIN_LEAF_RE = re.compile(
    r"""[(\[]\s*(['"])([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*)\1\s*,\s*(['"])"""
    r"""(?:=|!=|<>|<|>|<=|>=|=\?|=like|=ilike|like|not like|ilike|not ilike|in|not in|child_of|parent_of|any|not any|any!|not any!)\3"""
)
PERCENT_MASK_RE = re.compile(r"%\([^)]*\)[ds]")
_OPS_B = rb"=|!=|<>|<|>|<=|>=|&lt;|&gt;|&lt;=|&gt;=|&lt;&gt;|=\?|=like|=ilike|like|not like|ilike|not ilike|in|not in|child_of|parent_of|any|not any|any!|not any!"
DOMAIN_LEAF_B = re.compile(rb"""[(\[]\s*(['"])([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*)\1\s*,\s*(['"])(?:""" + _OPS_B + rb""")\3""")
CODE_CALL_B = re.compile(rb"""\b(?:model|records|record)\s*\.\s*([A-Za-z_]\w*)\s*\(""")
_ENTITIES = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}
ENTITY_MASK_RE = re.compile(r"&#?\w+;")
ATTR_EXPR_NAMES = frozenset({"invisible", "readonly", "required", "column_invisible", "attrs"})
XPATH_BUTTON_RE = re.compile(r"""button\[@name\s*=\s*(['"])([A-Za-z_][A-Za-z0-9_]*)\1\]""")
XPATH_LABEL_RE = re.compile(r"""label\[@for\s*=\s*(['"])([A-Za-z_][A-Za-z0-9_]*)\1\]""")
IDENT_RE = re.compile(r"""[A-Za-z_][A-Za-z0-9_]*""")
STRING_RE = re.compile(r"""'[^']*'|"[^"]*\"""")
MODEL_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
VIEW_MODEL_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*$")  # views may target dotless models (website, base)
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SUBVIEW_TAGS = frozenset(
    {"form", "list", "tree", "kanban", "graph", "pivot", "calendar", "gantt", "activity", "cohort", "map", "grid", "hierarchy", "search", "card"}
)
EXPR_ATTRS = ("invisible", "readonly", "required", "column_invisible")
REF_ATTRS = ("context", "domain", "filter_domain", "options", "eval", "attrs")
MODEL_TEXT_FIELDS = frozenset({"model", "res_model", "src_model", "model_name", "binding_model"})
VIEW_REF_RE = re.compile(r"""\w+_view_ref['"]\s*:\s*(['"])([A-Za-z0-9_\-]+\.[A-Za-z0-9_.\-]+)\1""")
XMLID_ATTRS = ("quick_create_view", "on_create", "t-snippet", "t-snippet-call")
EXPR_SKIP = frozenset(
    {
        "and", "or", "not", "in", "is", "if", "else", "for", "lambda", "True", "False", "None", "true", "false",
        "context", "uid", "user", "parent", "current_company_id", "allowed_company_ids", "companies",
        "today", "now", "time", "datetime", "relativedelta", "context_today", "len", "bool", "str", "int",
        "float", "abs", "min", "max", "sum", "any", "all", "set", "list", "dict", "tuple", "id_list", "self",
        "current_date", "unicode",
    }
)
DATA_ROOTS = frozenset({"odoo", "openerp", "data"})


class _Frame:
    __slots__ = ("tag", "rec", "arch", "qweb", "subpath", "field_name", "capture", "text_start", "menu_id", "created", "arch_field", "attrs_spec")

    def __init__(self, tag: str):
        self.tag = tag
        self.rec = -1
        self.arch = False
        self.qweb = False
        self.subpath: tuple = ()  # x2many path to the model of this element's children
        self.field_name: str | None = None
        self.capture: str | None = None
        self.text_start = -1
        self.menu_id: str | None = None
        self.created = False
        self.arch_field = False
        self.attrs_spec = False


_intern = sys.intern


class _XmlExtractor:
    def __init__(self, path: str, src: bytes, index: bool = False):
        self.index = index
        self.path = path.replace("\\", "/")
        self.src = src
        self.s = SrcIndex(src)
        self.records: list[list] = []
        self.refs: list[tuple] = []
        self.stack: list[_Frame] = []
        self.root: str | None = None
        self.noupdate = 0
        self.static = "/static/" in self.path
        self._attr_rx: dict[str, re.Pattern] = {}
        self.p = expat.ParserCreate()
        self.p.buffer_text = True
        self.p.StartElementHandler = self.start
        self.p.EndElementHandler = self.end

    def tag_span(self, idx: int) -> tuple[int, int]:
        m = TAG_RE.match(self.src, idx)
        return (idx, m.end()) if m else (idx, idx + 1)

    def attr_span(self, tag_start: int, tag_end: int, name: str) -> tuple[int, int] | None:
        rx = self._attr_rx.get(name)
        if rx is None:
            rx = self._attr_rx[name] = re.compile(rb"""(?:^|[\s<])""" + re.escape(name.encode()) + rb"""\s*=\s*(["'])(.*?)\1""", re.S)
        m = rx.search(self.src, tag_start, tag_end)
        return (m.start(2), m.end(2)) if m else None

    def add_ref(self, kind: str, value: str, start: int, end: int, ctx: tuple) -> None:
        line, c0, c1 = self.s.span(start, end)
        self.refs.append((kind, _intern(value), line, c0, c1, ctx))

    # source byte offset per char of expat's decoded value, then the end; None: same bytes or unmappable
    def value_map(self, span: tuple[int, int], value: str) -> list[int] | None:
        raw = self.src[span[0] : span[1]]
        if raw.isascii() and b"&" not in raw and b"\n" not in raw and b"\t" not in raw and b"\r" not in raw:
            return None
        out: list[int] = []
        chars: list[str] = []
        i, n, base = 0, len(raw), span[0]
        while i < n:
            b = raw[i]
            if b == 0x26:
                j = raw.find(b";", i, i + 12)
                ent = raw[i + 1 : j].decode("ascii", "replace") if j > 0 else ""
                ch = _ENTITIES.get(ent)
                if ch is None and ent.startswith("#"):
                    try:
                        ch = chr(int(ent[2:], 16) if ent[1:2] in ("x", "X") else int(ent[1:]))
                    except ValueError:
                        ch = None
                if ch is None:
                    return None
                out.append(base + i)
                chars.append(ch)
                i = j + 1
                continue
            if b == 0x0D and raw[i + 1 : i + 2] == b"\n":
                out.append(base + i)
                chars.append(" ")
                i += 2
                continue
            if b in (0x0A, 0x0D, 0x09):
                out.append(base + i)
                chars.append(" ")
                i += 1
                continue
            size = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
            out.append(base + i)
            chars.append(raw[i : i + size].decode("utf-8", "replace"))
            i += size
        if "".join(chars) != value:
            return None
        out.append(span[1])
        return out

    def add_value_ref(self, kind: str, item: str, span: tuple[int, int], vmap: list[int] | None, value: str, i: int, ctx: tuple) -> None:
        j = i + len(item)
        if vmap is None:
            start = span[0] + len(value[:i].encode())
            self.add_ref(kind, item, start, start + len(item.encode()), ctx)
        else:
            self.add_ref(kind, item, vmap[i], vmap[j], ctx)

    def ref_attr(self, kind: str, ts: int, te: int, attr: str, value: str, ctx: tuple) -> None:
        span = self.attr_span(ts, te, attr)
        if span is None:
            return
        stripped = value.strip()
        if stripped:
            self.add_value_ref(kind, stripped, span, self.value_map(span, value), value, len(value) - len(value.lstrip()), ctx)

    # "!x" negates a group; menuitem groups="-x" unlinks one
    def ref_list_attr(self, kind: str, ts: int, te: int, attr: str, value: str, ctx: tuple, strip: str = "!-") -> None:
        span = self.attr_span(ts, te, attr)
        if span is None:
            return
        vmap = self.value_map(span, value)
        pos = 0
        for part in value.split(","):
            item = part.strip()
            lead = len(part) - len(part.lstrip())
            if item[:1] in strip and item:
                item = item[1:]
                lead += 1
            if item:
                self.add_value_ref(kind, item, span, vmap, value, pos + lead, ctx)
            pos += len(part) + 1

    def scan_attr(self, rx: re.Pattern, group: int, kind: str, ts: int, te: int, attr: str, value: str, ctx: tuple) -> None:
        span = self.attr_span(ts, te, attr)
        if span is None:
            return
        vmap = self.value_map(span, value)
        for m in rx.finditer(value):
            self.add_value_ref(kind, m.group(group), span, vmap, value, m.start(group), ctx)

    def expr_fields(self, ts: int, te: int, attr: str, value: str, ctx: tuple) -> None:
        span = self.attr_span(ts, te, attr)
        if span is None:
            return
        vmap = self.value_map(span, value)
        masked = STRING_RE.sub(lambda m: " " * len(m.group()), value)
        masked = PERCENT_MASK_RE.sub(lambda m: " " * len(m.group()), masked)
        for m in IDENT_RE.finditer(masked):
            name = m.group()
            if name in EXPR_SKIP or name[0].isupper():
                continue
            before = masked[: m.start()].rstrip()
            after = masked[m.end() :].lstrip()
            if before.endswith(".") or after.startswith("(") or after.startswith("."):
                continue
            self.add_value_ref(REF_FIELD, name, span, vmap, value, m.start(), ctx)

    # <attribute name="invisible">expr</attribute>; 16.0 name="attrs" holds a dict of domains
    def text_expr_refs(self, start: int, end: int, attr: str, ctx: tuple) -> None:
        raw = self.src[start:end]
        if attr == "attrs":
            for m in DOMAIN_LEAF_B.finditer(raw):
                self.add_ref(REF_FIELD, m.group(2).decode(), start + m.start(2), start + m.end(2), ctx)
            return
        text = raw.decode("utf-8", "replace")
        masked = STRING_RE.sub(lambda m: " " * len(m.group()), text)
        masked = ENTITY_MASK_RE.sub(lambda m: " " * len(m.group()), masked)
        masked = PERCENT_MASK_RE.sub(lambda m: " " * len(m.group()), masked)
        for m in IDENT_RE.finditer(masked):
            name = m.group()
            if name in EXPR_SKIP or name[0].isupper():
                continue
            before = masked[: m.start()].rstrip()
            after = masked[m.end() :].lstrip()
            if before.endswith(".") or after.startswith("(") or after.startswith("."):
                continue
            b0 = start + len(text[: m.start()].encode())
            self.add_ref(REF_FIELD, name, b0, b0 + len(name.encode()), ctx)

    def text_refs(self, start: int, end: int, rec: int, field: str) -> None:
        raw = self.src[start:end]
        if field == "code":
            for m in CODE_CALL_B.finditer(raw):
                self.add_ref(REF_METHOD, m.group(1).decode(), start + m.start(1), start + m.end(1), ("code", rec))
        else:
            for m in DOMAIN_LEAF_B.finditer(raw):
                self.add_ref(REF_FIELD, m.group(2).decode(), start + m.start(2), start + m.end(2), ("record_domain", rec))

    def new_record(self, xid: str | None, model: str | None, tag: str, ts: int, te: int, info: dict) -> int:
        span = self.attr_span(ts, te, "id") if xid else None
        if span is None and tag == "qweb":
            span = self.attr_span(ts, te, "t-name")
        if span is not None:
            line, c0, c1 = self.s.span(span[0], span[1])
        else:
            line, c0, c1 = self.s.span(ts + 1, ts + 1 + len(tag))
        if self.noupdate:
            info["noupdate"] = True
        self.records.append([_intern(xid) if xid else xid, _intern(model) if model else model, tag, line, c0, c1, line, info])
        return len(self.records) - 1

    def start(self, tag: str, a: dict) -> None:
        ts, te = self.tag_span(self.p.CurrentByteIndex)
        parent = self.stack[-1] if self.stack else None
        f = _Frame(tag)
        if parent is None:
            self.root = tag
        else:
            f.rec = parent.rec
            f.arch = parent.arch
            f.qweb = parent.qweb
            f.subpath = parent.subpath
            f.menu_id = parent.menu_id
            f.attrs_spec = parent.attrs_spec
        if tag in DATA_ROOTS and a.get("noupdate") in ("1", "True", "true"):
            self.noupdate += 1
            f.capture = "__noupdate__"
        if f.arch:
            self.arch_element(f, parent, tag, a, ts, te)
        elif self.root == "templates" or (self.static and not (parent and parent.rec >= 0)):
            self.owl_element(f, tag, a, ts, te)
        else:
            self.data_element(f, parent, tag, a, ts, te)
        f.created = f.rec >= 0 and (parent is None or parent.rec != f.rec)
        self.stack.append(f)

    def end(self, tag: str) -> None:
        f = self.stack.pop()
        idx = self.p.CurrentByteIndex
        if f.capture == "__noupdate__":
            self.noupdate -= 1
        elif f.capture and f.capture.startswith("attribute:") and f.text_start >= 0 and idx > f.text_start:
            self.text_expr_refs(f.text_start, idx, f.capture[10:], ("expr", f.rec, self.stack[-1].subpath if self.stack else ()))
        elif f.capture and f.text_start >= 0 and f.rec >= 0:
            raw = self.src[f.text_start : idx] if idx >= f.text_start else b""
            text = raw.decode("utf-8", "replace")
            value = text.strip()
            rec = self.records[f.rec]
            if value and f.capture in ("domain_force", "domain", "code") and "<" not in value:
                self.text_refs(f.text_start, idx, f.rec, f.capture)
            if value and "ref(" in value and "<" not in value:
                for m in REF_CALL_RE.finditer(text):
                    start = f.text_start + len(text[: m.start(2)].encode())
                    self.add_ref(REF_XMLID, m.group(2), start, start + len(m.group(2).encode()), ("eval",))
            if value and "<" not in value:
                lead = len(text) - len(text.lstrip())
                start = f.text_start + len(text[:lead].encode())
                end = start + len(value.encode())
                name = f.capture
                if name in MODEL_TEXT_FIELDS and (
                    MODEL_RE.match(value) or (name == "model" and rec[X_MODEL] == "ir.ui.view" and VIEW_MODEL_RE.match(value))
                ):
                    self.add_ref(REF_MODEL, value, start, end, ("field_model",))
                    if name == "model" and rec[X_MODEL] == "ir.ui.view":
                        rec[X_INFO]["view_model"] = value
                    elif name in ("res_model", "model"):
                        rec[X_INFO].setdefault("res_model", value)
                elif name == "name":
                    rec[X_INFO].setdefault("name", value[:200])
                elif name == "type":
                    rec[X_INFO].setdefault("view_type", value)
                elif name == "mode":
                    rec[X_INFO]["mode"] = value
                elif name == "priority":
                    try:
                        rec[X_INFO]["priority"] = int(value)
                    except ValueError:
                        pass
                elif name in ("report_name", "report_file") and rec[X_MODEL] == "ir.actions.report" and "." in value and " " not in value:
                    if name == "report_name":
                        self.add_ref(REF_XMLID, value, start, end, ("report",))
                elif name == "key":
                    rec[X_INFO]["key"] = value
        if f.created and idx >= 0:
            self.records[f.rec][X_END_LINE] = self.s.pos(idx)[0]

    def data_element(self, f: _Frame, parent: _Frame | None, tag: str, a: dict, ts: int, te: int) -> None:
        in_record = parent is not None and parent.tag == "record" and parent.rec >= 0
        if tag == "record":
            model = a.get("model")
            f.rec = self.new_record(a.get("id"), model, "record", ts, te, {})
            if model:
                self.ref_attr(REF_MODEL, ts, te, "model", model, ("record",))
            if "context" in a:
                self.scan_attr(REF_CALL_RE, 2, REF_XMLID, ts, te, "context", a["context"], ("eval",))
            return
        if tag == "field" and in_record:
            name = a.get("name", "")
            rec = self.records[parent.rec]  # type: ignore[union-attr]
            if name and not self.index:
                self.ref_attr(REF_FIELD, ts, te, "name", name, ("record", parent.rec))  # type: ignore[union-attr]
            if "ref" in a:
                self.ref_attr(REF_XMLID, ts, te, "ref", a["ref"], ("ref",))
                if name == "model_id":
                    rec[X_INFO]["model_id"] = a["ref"].strip()
                elif name == "binding_model_id":
                    rec[X_INFO]["binding_model_id"] = a["ref"].strip()
                if name == "inherit_id":
                    rec[X_INFO]["inherit_id"] = a["ref"].strip()
                elif name in ("parent_id",) and rec[X_MODEL] == "ir.ui.menu":
                    rec[X_INFO]["parent"] = a["ref"].strip()
                elif name in ("action",) and rec[X_MODEL] == "ir.ui.menu":
                    rec[X_INFO]["action"] = a["ref"].strip()
            if "eval" in a:
                self.scan_attr(REF_CALL_RE, 2, REF_XMLID, ts, te, "eval", a["eval"], ("eval",))
                if name == "priority" and a["eval"].strip().isdigit():
                    rec[X_INFO]["priority"] = int(a["eval"].strip())
                if name in ("domain_force", "domain"):
                    self.scan_attr(DOMAIN_LEAF_RE, 2, REF_FIELD, ts, te, "eval", a["eval"], ("record_domain", parent.rec))  # type: ignore[union-attr]
            if "model" in a:
                self.ref_attr(REF_MODEL, ts, te, "model", a["model"], ("field_model",))
            if name in ("arch", "arch_db") and a.get("type") == "xml":
                f.arch = True
                f.arch_field = True
                f.qweb = rec[X_INFO].get("view_type") == "qweb"
                f.rec = parent.rec  # type: ignore[union-attr]
                f.subpath = ()
                return
            if a.get("type") == "html" or name in ("body_html", "arch_base"):
                f.arch = True
                f.qweb = True
                f.rec = parent.rec  # type: ignore[union-attr]
                return
            if "ref" not in a and "eval" not in a and name and name != "arch":
                f.capture = name
                f.text_start = te
            return
        if tag == "template":
            if not a.get("id") and a.get("t-name"):
                a = dict(a, id=a["t-name"])
            info: dict = {}
            for k in ("name", "active", "key"):
                if k in a:
                    info[k] = a[k]
            if a.get("priority", "").strip().isdigit():
                info["priority"] = int(a["priority"])
            if a.get("primary") in ("True", "true", "1") and a.get("inherit_id"):
                info["mode"] = "primary"
            if a.get("inherit_id"):
                info["inherit_id"] = a["inherit_id"].strip()
                self.ref_attr(REF_XMLID, ts, te, "inherit_id", a["inherit_id"], ("inherit_id",))
            info["view_type"] = "qweb"
            f.rec = self.new_record(a.get("id"), "ir.ui.view", "template", ts, te, info)
            if not self.attr_span(ts, te, "id") and a.get("id"):
                rec = self.records[f.rec]
                span = self.attr_span(ts, te, "t-name")
                if span:
                    rec[X_LINE], rec[X_COL], rec[X_END_COL] = self.s.span(span[0], span[1])
            if "groups" in a:
                self.ref_list_attr(REF_XMLID, ts, te, "groups", a["groups"], ("groups",))
            f.arch = True
            f.qweb = True
            f.subpath = ()
            return
        if tag == "menuitem":
            info = {}
            for k in ("name", "sequence", "web_icon"):
                if k in a:
                    info[k] = a[k]
            if a.get("parent"):
                info["parent"] = a["parent"].strip()
                self.ref_attr(REF_XMLID, ts, te, "parent", a["parent"], ("parent",))
            elif parent is not None and parent.tag == "menuitem" and parent.menu_id:
                info["parent"] = parent.menu_id
            if a.get("action"):
                info["action"] = a["action"].strip()
                self.ref_attr(REF_XMLID, ts, te, "action", a["action"], ("action",))
            if a.get("groups"):
                info["groups"] = a["groups"]
                self.ref_list_attr(REF_XMLID, ts, te, "groups", a["groups"], ("groups",))
            f.rec = self.new_record(a.get("id"), "ir.ui.menu", "menuitem", ts, te, info)
            f.menu_id = a.get("id")
            return
        if tag in ("report", "act_window"):
            model = "ir.actions.report" if tag == "report" else "ir.actions.act_window"
            info = {k: a[k] for k in ("name", "string", "report_name", "report_type") if k in a}
            for attr in ("model", "res_model", "src_model", "binding_model"):
                if attr in a:
                    self.ref_attr(REF_MODEL, ts, te, attr, a[attr], ("field_model",))
                    info.setdefault("res_model", a[attr])
            if "view_id" in a:
                self.ref_attr(REF_XMLID, ts, te, "view_id", a["view_id"], ("ref",))
            if "groups" in a:
                self.ref_list_attr(REF_XMLID, ts, te, "groups", a["groups"], ("groups",))
            f.rec = self.new_record(a.get("id"), model, tag, ts, te, info)
            return
        if tag == "function":
            model = a.get("model")
            if model:
                self.ref_attr(REF_MODEL, ts, te, "model", model, ("function",))
                if a.get("name"):
                    self.ref_attr(REF_METHOD, ts, te, "name", a["name"], ("function", model))
            if "eval" in a:
                self.scan_attr(REF_CALL_RE, 2, REF_XMLID, ts, te, "eval", a["eval"], ("eval",))
            return
        if tag == "value":
            if "eval" in a:
                self.scan_attr(REF_CALL_RE, 2, REF_XMLID, ts, te, "eval", a["eval"], ("eval",))
            if "model" in a:
                self.ref_attr(REF_MODEL, ts, te, "model", a["model"], ("function",))
            return
        if tag == "delete":
            if a.get("model"):
                self.ref_attr(REF_MODEL, ts, te, "model", a["model"], ("delete",))
            if a.get("id"):
                self.ref_attr(REF_XMLID, ts, te, "id", a["id"], ("delete",))
            return
        if tag in DATA_ROOTS or parent is None:
            return
        if a.get("id") and parent.tag in DATA_ROOTS:
            model = "ir.asset" if tag == "asset" else None
            f.rec = self.new_record(a.get("id"), model, tag, ts, te, {k: v for k, v in a.items() if k in ("name", "bundle")})

    def arch_element(self, f: _Frame, parent: _Frame | None, tag: str, a: dict, ts: int, te: int) -> None:
        rec = f.rec
        sub = parent.subpath if parent is not None else ()
        if parent is not None and tag in SUBVIEW_TAGS and parent.field_name and not f.qweb:
            f.subpath = sub + (parent.field_name,)
        else:
            f.subpath = sub
        if parent is not None and parent.arch_field and rec >= 0 and tag in SUBVIEW_TAGS:
            self.records[rec][X_INFO].setdefault("type", tag)
        if f.attrs_spec:
            # <attribute> children are values, not view nodes; their expressions name located-element fields
            if tag == "attribute" and a.get("name") in ATTR_EXPR_NAMES and rec >= 0 and not f.qweb:
                f.capture = "attribute:" + a["name"]
                f.text_start = te
            return
        if a.get("position") == "attributes":
            f.attrs_spec = True
        if "groups" in a:
            self.ref_list_attr(REF_XMLID, ts, te, "groups", a["groups"], ("groups",))
        for attr in XMLID_ATTRS:
            v = a.get(attr)
            if v and "." in v and "{" not in v and v != "quick_create":
                self.ref_attr(REF_XMLID, ts, te, attr, v, ("ref",))
        if f.qweb:
            if "t-call" in a and a["t-call"] and "{" not in a["t-call"]:
                self.ref_attr(REF_XMLID, ts, te, "t-call", a["t-call"], ("tcall",))
        else:
            if tag == "field" and a.get("name") and NAME_RE.match(a["name"]):
                self.ref_attr(REF_FIELD, ts, te, "name", a["name"], ("arch", rec, sub))
                f.field_name = a["name"]
            elif tag == "label" and a.get("for") and NAME_RE.match(a["for"]):
                self.ref_attr(REF_FIELD, ts, te, "for", a["for"], ("label", rec, sub))
            elif tag == "groupby" and a.get("name") and NAME_RE.match(a["name"]):
                # <groupby name="m2o"> in a list is a sub-view on the m2o's comodel
                self.ref_attr(REF_FIELD, ts, te, "name", a["name"], ("arch", rec, sub))
                if "position" not in a:  # a locator's children go beside the groupby, not in it
                    f.subpath = sub + (a["name"],)
            elif (tag == "button" or a.get("type") == "object") and a.get("name") and "special" not in a:
                name = a["name"]
                btype = a.get("type")
                if "%(" in name:
                    self.scan_attr(PERCENT_RE, 1, REF_XMLID, ts, te, "name", name, ("percent",))
                elif (btype == "object" or (btype is None and "position" in a)) and NAME_RE.match(name):
                    # object buttons + untyped locators in inherited views (analysis resolves them in the parent arch)
                    self.ref_attr(REF_METHOD, ts, te, "name", name, ("button", rec, sub))
                elif btype == "action" and "." in name:
                    self.ref_attr(REF_XMLID, ts, te, "name", name, ("action",))
            elif tag == "xpath" and a.get("expr"):
                self.xpath(f, a["expr"], ts, te, rec, sub)
        for attr in REF_ATTRS:
            v = a.get(attr)
            if not v:
                continue
            if "ref(" in v:
                self.scan_attr(REF_CALL_RE, 2, REF_XMLID, ts, te, attr, v, ("eval",))
            if "%(" in v:
                self.scan_attr(PERCENT_RE, 1, REF_XMLID, ts, te, attr, v, ("percent",))
            if "_view_ref" in v:
                self.scan_attr(VIEW_REF_RE, 2, REF_XMLID, ts, te, attr, v, ("view_ref",))
            if attr == "attrs" and not f.qweb:
                self.scan_attr(DOMAIN_LEAF_RE, 2, REF_FIELD, ts, te, attr, v, ("expr", rec, f.subpath if tag != "field" else sub))
            elif attr in ("domain", "filter_domain") and not f.qweb:
                # <field domain> filters the comodel; <filter domain>, <field filter_domain> the view's model
                on_field = a.get("name") if tag == "field" and attr == "domain" and NAME_RE.match(a.get("name") or "") else None
                self.scan_attr(DOMAIN_LEAF_RE, 2, REF_FIELD, ts, te, attr, v, ("arch_domain", rec, sub, on_field))
        if not f.qweb:
            for attr in EXPR_ATTRS:
                v = a.get(attr)
                if v and v not in ("1", "0", "True", "False", "true", "false"):
                    self.expr_fields(ts, te, attr, v, ("expr", rec, sub))
        if "name" in a and "%(" in a["name"] and tag != "button":
            self.scan_attr(PERCENT_RE, 1, REF_XMLID, ts, te, "name", a["name"], ("percent",))

    def xpath(self, f: _Frame, expr: str, ts: int, te: int, rec: int, sub: tuple) -> None:
        span = self.attr_span(ts, te, "expr")
        raw = self.src[span[0] : span[1]].decode("utf-8", "replace") if span else None
        path = list(sub)
        steps = [(m.start(), m.group(1)) for m in XPATH_STEP_RE.finditer(expr)]
        for m in XPATH_FIELD_RE.finditer(expr):
            name = m.group(2)
            if span and raw == expr:
                start = span[0] + len(expr[: m.start(2)].encode())
                self.add_ref(REF_FIELD, name, start, start + len(name.encode()), ("arch", rec, tuple(path)))
            # a following sub-view or field step descends into the comodel: field[@name='x']//field[@name='y']
            nxt = [t for (pos, t) in steps if pos >= m.end()]
            if nxt and (nxt[0] in SUBVIEW_TAGS or nxt[0] == "field"):
                path.append(name)
        f.subpath = tuple(path)
        if span:
            vmap = self.value_map(span, expr)
            for m in XPATH_BUTTON_RE.finditer(expr):
                self.add_value_ref(REF_METHOD, m.group(2), span, vmap, expr, m.start(2), ("button", rec, tuple(path)))
            for m in XPATH_LABEL_RE.finditer(expr):
                self.add_value_ref(REF_FIELD, m.group(2), span, vmap, expr, m.start(2), ("label", rec, tuple(path)))

    def owl_element(self, f: _Frame, tag: str, a: dict, ts: int, te: int) -> None:
        if a.get("t-name"):
            info: dict = {}
            if a.get("t-inherit"):
                info["inherit"] = a["t-inherit"]
                info["mode"] = "extension" if a.get("t-inherit-mode") == "extension" else "primary"
            f.rec = self.new_record(a["t-name"].strip(), None, "qweb", ts, te, info)
        if a.get("t-inherit"):
            self.ref_attr(REF_QWEB, ts, te, "t-inherit", a["t-inherit"], ("tinherit",))
        if a.get("t-call") and "{" not in a["t-call"]:
            self.ref_attr(REF_QWEB, ts, te, "t-call", a["t-call"], ("tcall",))


def extract(path: str, src: bytes, index: bool = False) -> dict:
    facts: dict = {KIND: KIND_XML, ERROR: None, ROOT: None, RECORDS: [], REFS: []}
    ex = _XmlExtractor(path, src, index)
    try:
        ex.p.Parse(src, True)
    except expat.ExpatError as exc:
        facts[ERROR] = (expat.errors.messages.get(exc.code, str(exc)) if hasattr(expat.errors, "messages") else str(exc), max(0, exc.lineno - 1), exc.offset)
    except Exception as exc:  # pragma: no cover
        facts[ERROR] = (f"{type(exc).__name__}: {exc}", 0, 0)
    facts[ROOT] = ex.root
    facts[RECORDS] = [tuple(r) for r in ex.records]
    facts[REFS] = ex.refs
    return facts
