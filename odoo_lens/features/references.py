"""Find references (``textDocument/references``).

The target under the cursor (``analysis.targets_at``, first target) is turned
into reference locations by :func:`references_for_target`, which also works on
hand-built targets. Sources, cheapest first:

* the registry's inverted index: model names, xml ids, templates, manifest
  ``depends``, and field paths / method names held by known string contexts
  (``related=``, ``@api.depends``, ``compute=``, view arch, buttons, JS
  ``orm.call``...). A field ref counts when the path segment naming the field
  is owned by the target model or a model related to it by inheritance;
* on-demand scans of candidate files the index cannot answer: XML record-level
  ``<field name>`` elements (re-extracted in full), CSV headers, Python
  attribute / call / string usages whose receiver model is inferred
  (``analysis.python.member_usages``), imports for modules and generic Python
  symbols, JS imports (and the local names they bind) for JS symbols.

Scans only visit modules that can see the target, prefilter file contents with
byte searches, run in relevance order (current file, customer modules, then
core files most likely to type a receiver as the target model; Python files
never loaded - migrations, upgrade scripts - last) and stop at a time box, or
as soon as the hits found fill the capped answer several times over. Results
are deduplicated, ranked - current file, never-loaded Python files last,
customer modules (``env.custom_roots``), primary before secondary references
(data records' ``<field name>`` and ``model=``, CSV headers, anything in test
files), module load order, path/line - and capped.
"""

from __future__ import annotations

import logging
import os
import re
import time
import weakref
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from typing import Any, Callable, Iterator

from ..analysis import targets_at
from ..analysis.targets import ATTR, FIELD, FILE, METHOD, MODEL, MODULE, PYSYM, TEMPLATE, XMLID, Target
from ..analysis.js import LIB_ALIASES, import_bindings
from ..analysis.xml import ARCH_ROLES, MODEL_ROLES, arch_ref_candidates, arch_ref_model
from ..documents import utf16_len, utf16_to_index
from ..extract import extract_source
from ..facts import (
    A_NAME,
    CLASSES,
    CSV_MODEL,
    F_ARGS,
    F_COMODEL,
    F_NAME,
    I_ASNAME,
    I_COL,
    I_END_COL,
    I_LEVEL,
    I_LINE,
    I_MODULE,
    I_NAME,
    IMPORTS,
    K_ATTRS,
    K_END_LINE,
    K_FIELDS,
    K_LINE,
    K_METHODS,
    K_NAME,
    KIND,
    KIND_CSV,
    KIND_JS,
    KIND_MANIFEST,
    KIND_PY,
    KIND_XML,
    M_END_LINE,
    M_LINE,
    M_NAME,
    MI_INHERITS,
    R_COL,
    R_CTX,
    R_END_COL,
    R_KIND,
    R_LINE,
    R_VALUE,
    RECORDS,
    REF_FIELD,
    REF_FILE,
    REF_METHOD,
    REFS,
    S_END_LINE,
    S_KIND,
    S_LINE,
    S_NAME,
    SYMBOLS,
    X_MODEL,
)
from ..registry import UNLOADED_DIRS, MemberDef
from .navigation import class_spot, descendants, field_implementations, member_spot, method_implementations, record_spot, trust_unknown_receiver

log = logging.getLogger("odoo_lens.references")

MAX_REFERENCES = 300
MAX_PY_REFERENCES = 100  # generic Python symbols / files: name-based, keep it short
MAX_MODELS = 3  # ambiguous member targets: union over the best candidate models
BUDGET = float(os.environ.get("ODOO_LENS_REFS_BUDGET", "2.2"))  # seconds of scanning per request (one file may overrun it by ~0.3 s)
CORE_PY_SHARE = 0.7  # share of the budget core Python scans may use before XML records get their turn
MAX_ARCH_CANDIDATES = 3
SOURCE_CACHE_BYTES = 32 << 20
USAGE_CACHE_SIZE = 50_000
REEXPORT_DEPTH = 2
SATURATION = 2  # scans stop once primary hits fill the capped answer this many times over
PRIMARY, SECONDARY = 0, 1  # ranking tiers within a file group (test files are always secondary)

_WHICH = {FIELD: "field", METHOD: "method", ATTR: "attr"}
_USAGE_KINDS = {
    "field": frozenset({"attr", "string", "key"}),
    "method": frozenset({"call", "attr", "super"}),
    "attr": frozenset({"attr", "call"}),
}
_UNLOADED = tuple(f"{os.sep}{d}{os.sep}" for d in sorted(UNLOADED_DIRS - {"tests"}))  # tests have their own tier
_TESTS = f"{os.sep}tests{os.sep}"
_IDENT = re.compile(rb"[A-Za-z_]\w*")
_QUOTED_MODEL = re.compile(rb"""['"]([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)['"]""")
_MANY_NEEDLES = 16  # above this many needles, one identifier/regex pass beats repeated bytes.find
# strings and comments of Python source (best effort: prefixes, triple quotes, escapes)
_PY_SKIP = re.compile(r'''#[^\n]*|[rRbBuUfF]{0,2}(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')''')
# strings, template literals and comments of JS source (best effort: no regex literals)
_JS_SKIP = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\[\s\S]|[^`\\])*`''')
_JS_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_JS_STAR_EXPORT = re.compile(r"""\bexport\s*\*\s*from\s*(["'])([^"'\n]+)\1""")
_JS_STAR_EXPORT_B = re.compile(rb"""\bexport\s*\*\s*from\b""")  # byte prefilter: such barrels need not name the symbol
_JS_EXPORT_LIST = re.compile(r"""\bexport\s*\{([^}]*)\}(\s*from\b)?""")
_JS_EXPORT_DEFAULT = re.compile(r"""\bexport\s+default\s+([A-Za-z_$][\w$]*)\s*;?\s*$""", re.M)


# ---------------------------------------------------------------------------
# entry points


def references(ctx: Any, include_declaration: bool = True) -> list[dict]:
    targets = targets_at(ctx)
    if not targets:
        return []
    return references_for_target(ctx, targets[0], include_declaration)


def references_for_target(ctx: Any, target: Target, include_declaration: bool = True) -> list[dict]:
    kind = target.kind
    cap = MAX_PY_REFERENCES if kind in (PYSYM, FILE) else MAX_REFERENCES
    search = _Search(ctx, include_declaration, cap)
    if kind == MODEL:
        for model in _target_models(target):
            search.model(model)
    elif kind in _WHICH:
        models = _target_models(target)
        if models and target.name:
            search.member(_WHICH[kind], models, target.name)
    elif kind == XMLID:
        if target.xmlid:
            search.xmlid(target.xmlid)
    elif kind == TEMPLATE:
        if target.xmlid or target.name:
            search.template(target.xmlid or target.name or "")
    elif kind == MODULE:
        if target.name:
            search.module(target.name)
    elif kind == PYSYM:
        search.pysym(target)
    elif kind == FILE:
        if target.path:
            search.file(target.path)
    return search.locations()


def _target_models(target: Target) -> list[str]:
    if target.model:
        return [target.model]
    return list(dict.fromkeys(target.models or ()))[:MAX_MODELS]


def _usage_finder() -> Callable | None:
    """``analysis.python.member_usages`` (None while that analysis is unavailable)."""
    try:
        from ..analysis.python import member_usages
    except ImportError:
        return None
    return member_usages


# ---------------------------------------------------------------------------
# per-registry lookup tables


class _Index:
    """Tables derived from one registry generation, built on first use."""

    __slots__ = ("generation", "custom_prefixes", "custom_modules", "_files", "_links", "_imports", "usages")

    def __init__(self, reg: Any, env: Any):
        self.generation = reg.generation
        self.custom_prefixes = tuple(os.path.join(r, "") for r in env.custom_roots)
        prefixes = self.custom_prefixes
        self.custom_modules = {m.name for m in reg.modules() if m.path.startswith(prefixes)} if prefixes else set()
        self._files: dict | None = None
        self._links: tuple | None = None
        self._imports: dict | None = None
        self.usages: dict = {}  # (path, name, stamp) -> member_usages result

    def files(self, reg: Any, env: Any) -> dict[str | None, list[tuple[str, str, bool, bool, int]]]:
        """module -> [(path, kind, is test file, never loaded, module rank)]."""
        if self._files is None:
            out: dict = defaultdict(list)
            for path, facts in reg.files.items():
                mod = reg.module_of_path(path)
                info = reg.module(mod)
                root = info.path if info is not None else env.root_of(path)
                rel = path[len(root) :] if root else path
                rank = info.rank if info is not None else -1
                out[mod].append((path, facts.get(KIND), _TESTS in rel, any(u in rel for u in _UNLOADED), rank))
            self._files = out
        return self._files

    def links(self, reg: Any) -> tuple[dict[str, set[bytes]], dict[str, set[str]]]:
        """(comodel -> names of relational fields pointing to it,
        parent -> models delegating to it through ``_inherits``)."""
        if self._links is None:
            links: dict[str, set[bytes]] = defaultdict(set)
            delegating: dict[str, set[str]] = defaultdict(set)
            for model in reg.model_names():
                for c in reg.model_classes(model):
                    for parent in c.model_info.get(MI_INHERITS) or ():
                        delegating[parent].add(model)
                    for f in c.facts[K_FIELDS]:
                        comodel = f[F_COMODEL]
                        if comodel:
                            links[comodel].add(f[F_NAME].encode())
                            if (f[F_ARGS] or {}).get("delegate") is True:
                                delegating[comodel].add(model)
            self._links = (links, delegating)
        return self._links

    def imports(self, reg: Any) -> dict[str, list[tuple[str, tuple]]]:
        """Resolved dotted module -> [(importing file, Import tuple)] (relative
        imports resolved against the importing file's package)."""
        if self._imports is None:
            out: dict[str, list[tuple[str, tuple]]] = defaultdict(list)
            for path, facts in reg.iter_files(KIND_PY):
                package: list[str] | None = None
                for imp in facts.get(IMPORTS) or ():
                    mod = imp[I_MODULE]
                    level = imp[I_LEVEL]
                    if level:
                        if package is None:
                            dotted = reg.py_dotted_name(path)
                            parts = dotted.split(".") if dotted else []
                            package = parts if path.endswith("__init__.py") else parts[:-1]
                        base = package[: len(package) - level + 1] if level > 1 else package
                        if not base:
                            continue
                        mod = ".".join(base + [mod]) if mod else ".".join(base)
                    out[mod].append((path, imp))
            self._imports = out
        return self._imports


_INDEXES: "weakref.WeakKeyDictionary[Any, _Index]" = weakref.WeakKeyDictionary()


def _index(reg: Any, env: Any) -> _Index:
    idx = _INDEXES.get(reg)
    if idx is None or idx.generation != reg.generation:
        idx = _INDEXES[reg] = _Index(reg, env)
    return idx


class _Sources:
    """Bounded LRU of file contents validated by (mtime_ns, size)."""

    def __init__(self, budget: int):
        self._data: OrderedDict[str, tuple[tuple[int, int], bytes]] = OrderedDict()
        self._size = 0
        self._budget = budget

    def get(self, path: str) -> tuple[bytes | None, tuple[int, int] | None]:
        try:
            st = os.stat(path)
        except OSError:
            return None, None
        stamp = (st.st_mtime_ns, st.st_size)
        hit = self._data.get(path)
        if hit is not None and hit[0] == stamp:
            self._data.move_to_end(path)
            return hit[1], stamp
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None, None
        if hit is not None:
            self._size -= len(hit[1])
        self._data[path] = (stamp, data)
        self._size += len(data)
        while self._size > self._budget and len(self._data) > 1:
            _, (_, old) = self._data.popitem(last=False)
            self._size -= len(old)
        return data, stamp


_SOURCES = _Sources(SOURCE_CACHE_BYTES)


# ---------------------------------------------------------------------------
# the search


class _Search:
    """Reference locations of one request: deduplicated, then ranked and capped."""

    def __init__(self, ctx: Any, include_declaration: bool, cap: int):
        self.ctx = ctx
        self.reg = ctx.registry
        self.include_declaration = include_declaration
        self.cap = cap
        self.index = _index(ctx.registry, ctx.env)
        self.started = time.monotonic()
        self.deadline = self.started + BUDGET
        self.stopped = ""  # why scanning stopped early ("time box" / "saturated")
        self.hits: dict[tuple[str, int, int], tuple[int, int]] = {}  # (path, line, col) -> (end_col, tier)
        self.primary = 0
        self._file_keys: dict[str, tuple] = {}
        self._code: dict[str, _PyCode | None] = {}

    def add(self, path: str, line: int, col: int, end_col: int, tier: int = PRIMARY) -> None:
        """Record a location; ``tier`` SECONDARY ranks it after the primary
        references (data records, CSV headers)."""
        key = (path, line, col)
        hit = self.hits.get(key)
        if hit is None or tier < hit[1]:
            if tier == PRIMARY:
                self.primary += 1
            self.hits[key] = (end_col, tier)

    def should_stop(self, deadline: float | None = None) -> bool:
        """Raises when the request is cancelled; True once the time box (or the
        earlier ``deadline`` of a phase) is reached, or when the primary hits
        found fill the capped answer several times over - files scanned later
        rank lower or only add secondary hits."""
        self.ctx.token.check()
        if self.primary >= SATURATION * self.cap:
            self.stopped = "saturated"
            return True
        now = time.monotonic()
        if now > self.deadline:
            self.stopped = "time box"
            return True
        return deadline is not None and now > deadline

    def source(self, path: str) -> tuple[bytes | None, tuple[int, int] | None]:
        """Current content of ``path`` and its disk stamp (None for unsaved text)."""
        if path == self.ctx.path:
            return self.ctx.source, None
        docs = self.ctx.server.documents
        if docs.is_open(path):
            return docs.source(path), None
        return _SOURCES.get(path)

    def code(self, path: str) -> _PyCode | None:
        """Text of a Python or JS file with its strings and comments located."""
        if path not in self._code:
            src = self.source(path)[0]
            js = path.endswith(".js")
            self._code[path] = _PyCode(src.decode("utf-8", "replace"), _JS_SKIP if js else _PY_SKIP, not js) if src is not None else None
        return self._code[path]

    def file_key(self, path: str) -> tuple:
        """Ranking of a file: current file, Python files never loaded
        (migrations...) last, customer modules, non-test files, load order of
        the module, path."""
        key = self._file_keys.get(path)
        if key is None:
            reg = self.reg
            info = reg.module(reg.module_of_path(path))
            root = info.path if info is not None else self.ctx.env.root_of(path)
            rel = path[len(root) :] if root else path
            unloaded = path.endswith(".py") and any(u in rel for u in _UNLOADED)
            key = (path != self.ctx.path, unloaded, not path.startswith(self.index.custom_prefixes), _TESTS in rel, info.rank if info else -1, path)
            self._file_keys[path] = key
        return key

    def locations(self) -> list[dict]:
        cap, file_key = self.cap, self.file_key

        def sort_key(item: tuple) -> tuple:
            (path, line, col), (_end, tier) = item
            fk = file_key(path)
            return (fk[0], fk[1], fk[2], max(fk[3], tier), fk[4], path, line, col)

        items = sorted(self.hits.items(), key=sort_key)
        if len(items) > cap or self.stopped:
            log.info(
                "references: %d found%s, returning %d (%.0f ms)",
                len(items),
                f" (scan stopped: {self.stopped})" if self.stopped else "",
                min(cap, len(items)),
                (time.monotonic() - self.started) * 1000,
            )
        return [self.ctx.location(path, line, col, end) for (path, line, col), (end, _tier) in items[:cap]]

    # ------------------------------------------------------------- simple kinds

    def model(self, model: str) -> None:
        reg = self.reg
        if self.include_declaration:
            for c in reg.model_classes(model):
                self.add(*class_spot(c))
        for h in reg.refs_to_model(model):
            self.add(h.path, h.line, h.col, h.end_col, SECONDARY if h.ctx[0] == "record" else PRIMARY)

    def xmlid(self, xmlid: str) -> None:
        reg = self.reg
        if self.include_declaration:
            for r in reg.records(xmlid):
                self.add(*record_spot(r))
        for h in reg.refs_to_xmlid(xmlid):
            self.add(h.path, h.line, h.col, h.end_col)

    def template(self, name: str) -> None:
        reg = self.reg
        if self.include_declaration:
            for r in reg.templates(name) or reg.records(name):
                self.add(*record_spot(r))
        for h in reg.refs_to_template(name):
            self.add(h.path, h.line, h.col, h.end_col)

    def module(self, name: str) -> None:
        """Manifest ``depends`` entries and absolute ``odoo.addons.<name>`` imports."""
        reg = self.reg
        info = reg.module(name)
        if self.include_declaration and info is not None:
            self.add(info.manifest, 0, 0, 0)
        for h in reg.refs_to_module(name):
            self.add(h.path, h.line, h.col, h.end_col)
        prefix = f"odoo.addons.{name}"
        rx = re.compile(r"\bodoo\.addons\.(" + re.escape(name) + r")\b")
        for mod, entries in self.index.imports(reg).items():
            if mod == "odoo.addons":
                for path, imp in entries:
                    if imp[I_NAME] == name and not imp[I_LEVEL]:
                        self.add(path, imp[I_LINE], imp[I_COL], imp[I_COL] + len(name))
            elif mod == prefix or mod.startswith(prefix + "."):
                for path, imp in entries:
                    if not imp[I_LEVEL]:
                        self.add(*self._spot_near(path, imp, rx))

    def _spot_near(self, path: str, imp: tuple, rx: re.Pattern) -> tuple[str, int, int, int]:
        """Position of ``rx``'s group 1 on the import's line or the lines above it
        (``from x import (\\n  name)``), else the imported name token."""
        lines = self.ctx.server.documents.lines(path) or []
        for n in range(min(imp[I_LINE], len(lines) - 1), max(-1, imp[I_LINE] - 8), -1):
            m = rx.search(lines[n])
            if m:
                line = lines[n]
                return (path, n, utf16_len(line[: m.start(1)]), utf16_len(line[: m.end(1)]))
        return (path, imp[I_LINE], imp[I_COL], imp[I_END_COL])

    # ------------------------------------------------------------ fields / methods

    def member(self, which: str, models: list[str], name: str) -> None:
        reg = self.reg
        family = self.family(models, name, which)
        in_family: Callable[[Any], bool] = (lambda m: True) if family is None else family.__contains__
        if self.include_declaration:
            for model in models:
                for d in _declarations(reg, model, name, which):
                    self.add(*member_spot(d))
        unresolved: set[str] = set()  # Python files holding model-less string refs to ``name``
        if which != "attr":
            self.string_refs(REF_FIELD if which == "field" else REF_METHOD, name, in_family, unresolved)
        modules = self.visible_modules(models, name, which)
        if which == "field":
            self.csv_headers(name, in_family, modules)
        self.scan(which, name, family, in_family, modules, unresolved)

    def family(self, models: list[str], name: str, which: str) -> set[str] | None:
        """Models whose member ``name`` is the target's: the targets, their
        ancestors that have the member, models inheriting them and - for fields -
        models delegating to them via ``_inherits`` without redefining the field.
        None means every model (members of ``base``)."""
        reg = self.reg
        if "base" in models:
            return None
        lookup = _lookup(reg, which)
        delegating = self.index.links(reg)[1] if which == "field" else {}
        fam: set[str] = set(models)
        for model in models:
            stack, seen = [model], {model}
            while stack:
                m = stack.pop()
                parents = list(reg.model_parents(m))
                if which == "field":
                    defs = reg.field(m, name)
                    if defs and defs[0].delegated_via:  # delegated: the _inherits parent owns it
                        parents += [p for p, via in reg.model_inherits(m).items() if via == defs[0].delegated_via]
                for p in parents:
                    if p not in seen:
                        seen.add(p)
                        if lookup(p, name):
                            fam.add(p)
                            stack.append(p)
            if lookup("base", name):
                fam.add("base")
            fam |= descendants(reg, model)
            for child in delegating.get(model, ()):
                defs = reg.field(child, name)
                if defs and defs[0].delegated_via is not None:
                    fam.add(child)
        return fam

    def visible_modules(self, models: list[str], name: str, which: str) -> set[str] | None:
        """Modules that can see the member: those defining it on the target
        models (or their ancestors) and their dependents. None: every module and
        the framework (member defined by the framework)."""
        reg = self.reg
        lookup = _lookup(reg, which)
        mods: set[str | None] = set()
        for model in models:
            defs = lookup(model, name)
            if defs:
                mods.update(d.module for d in defs)
            else:
                mods.update(c.module for c in reg.model_classes(model))
        if not mods or None in mods:
            return None
        out: set[str] = set()
        for m in mods:
            out.add(m)  # type: ignore[arg-type]
            out |= reg.dependents(m)
        return out

    def string_refs(self, kind: str, name: str, in_family: Callable[[Any], bool], unresolved: set[str]) -> None:
        """Indexed field paths / method names whose owner is in the family."""
        reg = self.reg
        for h in reg.refs_by_name(kind, name):
            rctx = h.ctx
            if rctx[0] in ARCH_ROLES:
                if self._arch_ref_matches(h, in_family):
                    self.add(h.path, h.line, h.col, h.end_col)
                continue
            if rctx[0] in MODEL_ROLES:  # record rule / action domains, cron code, view domains
                facts = reg.file_facts(h.path) or {}
                root = arch_ref_model(reg, h.path, facts, h.facts, self.ctx.source if h.path == self.ctx.path else None)
                if root is None:
                    continue
                if kind == REF_METHOD:
                    if in_family(root):
                        self.add(h.path, h.line, h.col, h.end_col)
                    continue
                for offset, owner in _segment_owners(reg, root, h.value, name):
                    if in_family(owner):
                        self._add_segment(h.path, h.facts, offset, len(name))
                continue
            model = rctx[1] if len(rctx) > 1 else None
            if not isinstance(model, str):
                if h.path.endswith(".py"):
                    unresolved.add(h.path)
                continue
            if kind == REF_METHOD:
                if in_family(model):
                    self.add(h.path, h.line, h.col, h.end_col)
                continue
            for offset, owner in _segment_owners(reg, model, h.value, name):
                if in_family(owner):
                    self._add_segment(h.path, h.facts, offset, len(name))

    def _arch_ref_matches(self, h: Any, in_family: Callable[[Any], bool]) -> bool:
        reg = self.reg
        facts = reg.file_facts(h.path) or {}
        source = self.ctx.source if h.path == self.ctx.path else None
        owner = arch_ref_model(reg, h.path, facts, h.facts, source)
        if owner is not None:
            return in_family(owner)
        cands = arch_ref_candidates(reg, h.path, facts, h.facts, source)
        return 0 < len(cands) <= MAX_ARCH_CANDIDATES and any(in_family(m) for m in cands)

    def _add_segment(self, path: str, ref: tuple, offset: int, length: int) -> None:
        """The range of one segment of a path string (whole string when escapes
        or wide characters make the offsets unreliable)."""
        col, end = ref[R_COL], ref[R_END_COL]
        if end - col == len(ref[R_VALUE]):
            self.add(path, ref[R_LINE], col + offset, col + offset + length)
        else:
            self.add(path, ref[R_LINE], col, end)

    def csv_headers(self, name: str, in_family: Callable[[Any], bool], modules: set[str] | None) -> None:
        reg = self.reg
        for mod, entries in self.index.files(reg, self.ctx.env).items():
            if not self._module_visible(mod, modules):
                continue
            for path, kind, _test, _unloaded, _rank in entries:
                if kind != KIND_CSV:
                    continue
                facts = reg.file_facts(path) or {}
                if not in_family(facts.get(CSV_MODEL)):
                    continue
                for ref in facts.get(REFS) or ():
                    if ref[R_CTX][0] != "csv_header":
                        break  # header cells come first
                    if ref[R_KIND] == REF_FIELD and ref[R_VALUE] == name:
                        self.add(path, ref[R_LINE], ref[R_COL], ref[R_END_COL], SECONDARY)

    def _module_visible(self, mod: str | None, modules: set[str] | None) -> bool:
        return modules is None or mod in modules or mod in self.index.custom_modules

    # ---------------------------------------------------------------- file scans

    def scan(self, which: str, name: str, family: set[str] | None, in_family: Callable[[Any], bool], modules: set[str] | None, unresolved: set[str]) -> None:
        """Python usages and XML record fields, most relevant files first:
        current file, customer modules, then core files grouped by how likely
        they type a receiver as a family model (their classes, the model name
        quoted, a relational field pointing to it); tests, then Python files
        never loaded (migrations...), last."""
        reg, ctx = self.reg, self.ctx
        finder = _usage_finder()
        if finder is None:
            log.debug("references: analysis.python.member_usages unavailable, Python usages skipped")
        records = which == "field"
        pyscan = _PyScan(self, finder, name, which, in_family, trust_unknown_receiver(reg, name))
        if ctx.kind == KIND_PY and finder is not None:
            pyscan.scan(ctx.path)
        elif ctx.kind == KIND_XML and records:
            self.xml_records([ctx.path], name, in_family)
        custom_py: list[tuple] = []
        core_py: list[tuple] = []
        custom_xml: list[tuple] = []
        core_xml: list[tuple] = []
        unloaded_py: list[str] = []  # migrations / upgrade scripts: scanned (and ranked) last
        for mod, entries in self.index.files(reg, ctx.env).items():
            if not self._module_visible(mod, modules):
                continue
            custom = mod in self.index.custom_modules
            for e in entries:
                if e[0] == ctx.path:
                    continue
                if e[3]:
                    if e[1] == KIND_PY and finder is not None:
                        unloaded_py.append(e[0])
                    continue
                if e[1] == KIND_PY and finder is not None:
                    (custom_py if custom else core_py).append(e)
                elif e[1] == KIND_XML and records:
                    (custom_xml if custom else core_xml).append(e)
        order = _entry_order
        custom_py.sort(key=order)
        for path in pyscan.ordered([e[0] for e in custom_py], family, unresolved, None, keep_rest=True):
            if self.should_stop():
                return
            pyscan.scan(path)
        self.xml_records([e[0] for e in sorted(custom_xml, key=order)], name, in_family)
        core_py.sort(key=order)
        py_deadline = self.started + CORE_PY_SHARE * BUDGET if records else None
        for test in (False, True):
            deadline = None if test else py_deadline
            for path in pyscan.ordered([e[0] for e in core_py if e[2] == test], family, unresolved, deadline):
                if self.should_stop(deadline):
                    break
                pyscan.scan(path)
            if not test and records:
                self.xml_records([e[0] for e in sorted(core_xml, key=order)], name, in_family)
        for path in pyscan.leftovers:
            if self.should_stop():
                return
            pyscan.scan(path)
        unloaded_py.sort(key=self.file_key)
        for path in pyscan.ordered(unloaded_py, family, unresolved, None, keep_rest=True):
            if self.should_stop():
                return
            pyscan.scan(path)

    def xml_records(self, paths: list[str], name: str, in_family: Callable[[Any], bool]) -> None:
        """Record-level ``<field name="name">`` of records whose model is in the
        family (not in the index: candidate files are re-extracted in full)."""
        reg = self.reg
        nb = name.encode()
        needles = (b'name="' + nb + b'"', b"name='" + nb + b"'")
        for path in paths:
            if self.should_stop():
                return
            current = path == self.ctx.path
            facts = self.ctx.facts if current else reg.file_facts(path)
            if not facts or not any(in_family(r[X_MODEL]) for r in facts.get(RECORDS) or ()):
                continue
            src = self.source(path)[0]
            if not src or (needles[0] not in src and needles[1] not in src):
                continue
            if not current:
                facts = extract_source(path, KIND_XML, src)
            recs = facts.get(RECORDS) or ()
            for ref in facts.get(REFS) or ():
                if ref[R_KIND] == REF_FIELD and ref[R_VALUE] == name:
                    rctx = ref[R_CTX]
                    if rctx[0] == "record" and 0 <= rctx[1] < len(recs) and in_family(recs[rctx[1]][X_MODEL]):
                        self.add(path, ref[R_LINE], ref[R_COL], ref[R_END_COL], SECONDARY)

    # ------------------------------------------------------- generic Python / files

    def pysym(self, target: Target) -> None:
        """Name-based references of a Python symbol: occurrences in its scope
        (function for locals), plus in the files importing it (following package
        re-exports) for module-level symbols and members of plain classes. JS
        symbols (a ``.js`` path) go to :meth:`jssym`."""
        path, name = target.path, target.name
        if not path or not name:
            return
        code = self.code(path)
        if path.endswith(".js"):
            self.jssym(target, code)
        elif code is not None:
            facts = self.ctx.facts if path == self.ctx.path else self.reg.file_facts(path)
            scope, lo, hi, owner = _py_scope(facts or {}, target.line, name)
            dotted = self.reg.py_dotted_name(path)
            uses: dict[str, set[re.Pattern]] = defaultdict(set)  # importing file -> patterns
            if scope == "local":
                self.scan_code(path, code, _bare_rx(name), lo, hi)
            elif scope == "member":
                attr = _attr_rx(name)
                self.scan_code(path, code, attr)
                for ipath, _mode, _alias in self.importers(dotted, owner) if dotted and owner else ():
                    uses[ipath].add(attr)
            else:
                self.scan_code(path, code, _bare_rx(name))
                for ipath, mode, alias in self.importers(dotted, name) if dotted and scope == "module" else ():
                    uses[ipath].add(_bare_rx(alias) if mode == "name" else _qualified_rx(alias, name))
            uses.pop(path, None)
            self.scan_importers(uses)
        if self.include_declaration:
            self.add(path, target.line, target.col, target.end_col)
        else:
            self.hits.pop((path, target.line, target.col), None)

    def jssym(self, target: Target, code: _PyCode | None) -> None:
        """Name-based references of a JS symbol: occurrences in its file, then
        in the JS modules importing it (named, renamed, default and namespace
        imports, following ``export ... from`` re-exports), under the local
        name each of them binds it to."""
        path, name = target.path or "", target.name or ""
        # the import under the cursor (the name it binds here, ``ns.name`` for a
        # namespace): scanned first, however long the re-export chain to ``path``
        here = target.extra.get("local") if self.ctx.path != path and self.ctx.path.endswith(".js") else None
        if here:
            ns, _, member = here.rpartition(".")
            code_here = self.code(self.ctx.path)
            if code_here is not None:
                self.scan_code(self.ctx.path, code_here, _js_member_rx(ns, member) if ns else _js_bare_rx(member))
        exported_as: list[str] = []
        if code is not None:
            local = _js_declared(code, target.line, target.col) or (name if name != "default" else None)
            if local:
                self.scan_code(path, code, _js_bare_rx(local))
                exported_as = _js_exported_as(code, target.line, local)
        # ``name`` is the name the importer used: through ``export {a as b} from``
        # re-exports it differs from the names the defining module exports
        todo = [(path, n) for n in dict.fromkeys([*exported_as, name])]
        seen = set(todo)
        importers: dict[str, tuple[set[str], list[str]]] = {}  # module -> (its specifiers, JS files importing it)
        for _depth in range(REEXPORT_DEPTH + 1):
            wanted: dict[str, list[tuple[str, str, set[str]]]] = defaultdict(list)  # importer -> [(module, name, its specifiers)]
            for mod_path, exported in todo:
                if mod_path not in importers:
                    specs = _js_specs(self.reg, mod_path)
                    importers[mod_path] = (specs, self.js_importers(mod_path, specs))
                specs, ipaths = importers[mod_path]
                for ipath in ipaths:
                    if ipath != path:
                        wanted[ipath].append((mod_path, exported, specs))
            nxt: list[tuple[str, str]] = []
            for ipath in sorted(wanted, key=self.file_key):
                if self.should_stop():
                    return
                if self.full_before(ipath):
                    break  # the next level's importers may still rank before
                src = self.source(ipath)[0]
                if not src or not (any(exported == "default" or exported.encode() in src for _m, exported, _s in wanted[ipath]) or _JS_STAR_EXPORT_B.search(src)):
                    continue
                code = self.code(ipath)
                if code is None:
                    continue
                bindings = import_bindings(code.text)
                for mod_path, exported, specs in wanted[ipath]:
                    for local, (spec, imported) in bindings.items():
                        if not _js_spec_matches(ipath, spec, mod_path, specs):
                            continue
                        if imported == exported:
                            self.scan_code(ipath, code, _js_bare_rx(local))
                            nxt.extend((ipath, alias) for alias in _js_reexports(code.text, local))
                        elif imported == "*" and exported != "default":
                            self.scan_code(ipath, code, _js_member_rx(local, exported))
                    if exported != "default" and _js_star_reexport(code.text, ipath, mod_path, specs):
                        nxt.append((ipath, exported))
            todo = [t for t in dict.fromkeys(nxt) if t not in seen]
            if not todo:
                return
            seen.update(todo)

    def js_importers(self, path: str, specs: set[str]) -> list[str]:
        """JS files with an import / re-export specifier resolving to ``path``."""
        out: list[str] = []
        for jpath, facts in self.reg.iter_files(KIND_JS):
            if any(_js_spec_matches(jpath, spec, path, specs) for spec, _line, _col, _end in facts.get(IMPORTS) or ()):
                out.append(jpath)
        return out

    def full_before(self, path: str) -> bool:
        """The capped answer is full of hits ranking before ``path``'s."""
        key = self.file_key(path)
        return sum(1 for p, _line, _col in self.hits if self.file_key(p) < key) >= self.cap

    def scan_importers(self, uses: dict[str, set[re.Pattern]]) -> None:
        """Scan importing files in ranking order; stop once the capped answer
        is full of hits ranking before the next file."""
        for path in sorted(uses, key=self.file_key):
            if self.should_stop() or self.full_before(path):
                return
            code = self.code(path)
            for rx in uses[path] if code is not None else ():
                self.scan_code(path, code, rx)  # type: ignore[arg-type]

    def importers(self, dotted: str, name: str) -> list[tuple[str, str, str]]:
        """Files importing ``name`` from module ``dotted``, as (path, mode, alias):
        mode "name" (used bare as ``alias``) or "module" (used as ``alias.name``).
        Re-exports through package ``__init__`` files are followed."""
        imports = self.index.imports(self.reg)
        out: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        todo = [(dotted, name, 0)]
        while todo:
            mod, sym, depth = todo.pop()
            if (mod, sym) in seen:
                continue
            seen.add((mod, sym))
            for path, imp in imports.get(mod, ()):
                imported = imp[I_NAME]
                if imported == sym or imported == "*":
                    alias = (imp[I_ASNAME] or sym) if imported == sym else sym
                    out.append((path, "name", alias))
                    if depth < REEXPORT_DEPTH and path.endswith("__init__.py"):
                        package = self.reg.py_dotted_name(path)
                        if package and package != mod:
                            todo.append((package, alias, depth + 1))
                elif imported is None and imp[I_MODULE] == mod:  # import a.b.c [as x]
                    out.append((path, "module", imp[I_ASNAME] or mod))
            parent, _, last = mod.rpartition(".")
            for path, imp in imports.get(parent, ()) if parent else ():
                if imp[I_NAME] == last:  # from package import module [as x]
                    out.append((path, "module", imp[I_ASNAME] or last))
        return out

    def scan_code(self, path: str, code: _PyCode, rx: re.Pattern, lo: int | None = None, hi: int | None = None) -> None:
        """Matches of ``rx``'s group 1 outside strings and comments (between
        lines ``lo`` and ``hi`` when given), skipping ``name=`` keyword arguments."""
        text = code.text
        start = code.offset(lo) if lo is not None else 0
        end = code.offset(hi + 1) if hi is not None else len(text)
        for m in rx.finditer(text, start, end):
            s, e = m.span(1)
            if code.skipped(s) or (code.kwargs and text[e : e + 1] == "=" and text[e + 1 : e + 2] != "="):
                continue
            self.add(path, *code.position(s, e))

    def file(self, path: str) -> None:
        """Imports of a Python module, JS imports of a JS module, manifest entries."""
        reg = self.reg
        if self.include_declaration:
            self.add(path, 0, 0, 0)
        if path.endswith(".py"):
            dotted = reg.py_dotted_name(path)
            if dotted:
                self._module_imports(dotted)
        elif path.endswith(".js"):
            self._js_imports(path)
        self._manifest_entries(path)

    def _module_imports(self, dotted: str) -> None:
        imports = self.index.imports(self.reg)
        parent, _, last = dotted.rpartition(".")
        rx = re.compile(r"(?<![\w])(" + re.escape(last) + r")\b")
        for path, imp in imports.get(dotted, ()):
            if imp[I_NAME] is None:  # import a.b.c
                self.add(path, imp[I_LINE], imp[I_COL], imp[I_COL] + len(imp[I_MODULE]))
            else:  # from a.b.c import x: point at the module part of the statement
                self.add(*self._spot_near(path, imp, rx))
        for path, imp in imports.get(parent, ()) if parent else ():
            if imp[I_NAME] == last:
                self.add(path, imp[I_LINE], imp[I_COL], imp[I_COL] + len(last))

    def _js_imports(self, path: str) -> None:
        specs = _js_specs(self.reg, path)
        for jpath, facts in self.reg.iter_files(KIND_JS):
            for spec, line, col, end in facts.get(IMPORTS) or ():
                if _js_spec_matches(jpath, spec, path, specs):
                    self.add(jpath, line, col, end)

    def _manifest_entries(self, path: str) -> None:
        reg = self.reg
        mod = reg.module_of_path(path)
        info = reg.module(mod)
        if info is None:
            return
        rel = os.path.relpath(path, info.path).replace(os.sep, "/")
        qualified = f"{mod}/{rel}"
        for mpath, facts in reg.iter_files(KIND_MANIFEST):
            own = mpath == info.manifest
            for ref in facts.get(REFS) or ():
                if ref[R_KIND] != REF_FILE:
                    continue
                value = ref[R_VALUE].lstrip("/")
                if ref[R_CTX][0] == "assets":
                    hit = value == qualified
                else:  # data / demo entries are relative to the module of the manifest
                    hit = own and value == rel
                if hit:
                    self.add(mpath, ref[R_LINE], ref[R_COL], ref[R_END_COL])


# ---------------------------------------------------------------------------
# Python usages of a member


class _PyScan:
    """member_usages over candidate Python files, filtered by receiver model."""

    def __init__(self, search: _Search, finder: Callable | None, name: str, which: str, in_family: Callable[[Any], bool], accept_unknown: bool):
        self.search = search
        self.finder = finder
        self.name = name
        self.kinds = _USAGE_KINDS[which]
        self.in_family = in_family
        self.accept_unknown = accept_unknown  # receiver unknown: count only for (nearly) unique names
        nb = re.escape(name.encode())
        # the name as a whole word (attribute, path segment, dict key, order item) or
        # after "default_" (context keys); the literal comes first for a fast search
        self.pattern = re.compile(nb + rb"\b(?:(?<!\w" + nb + rb")|(?<=default_" + nb + rb"))")
        self.leftovers: list[str] = []  # candidates with no hint of the family (unknown receivers only)
        self.failures = 0

    def scan(self, path: str) -> None:
        search = self.search
        src, stamp = search.source(path)
        if not src or not self.pattern.search(src):
            return
        cache = search.index.usages
        key = (path, self.name, stamp)
        usages = cache.get(key) if stamp is not None else None
        if usages is None:
            try:
                usages = self.finder(search.reg, path, src, self.name)  # type: ignore[misc]
            except Exception:  # one unanalysable file must not sink the whole search
                self.failures += 1
                if self.failures == 1:
                    log.exception("references: usages of %r in %s failed", self.name, path)
                return
            if stamp is not None:
                if len(cache) > USAGE_CACHE_SIZE:
                    cache.clear()
                cache[key] = usages
        for line, col, end_col, receivers, kind in usages:
            if kind not in self.kinds:
                continue
            if receivers is None:
                if self.accept_unknown:
                    search.add(path, line, col, end_col)
            elif any(self.in_family(m) for m in ((receivers,) if isinstance(receivers, str) else receivers)):
                search.add(path, line, col, end_col)

    def ordered(self, paths: list[str], family: set[str] | None, unresolved: set[str], deadline: float | None, keep_rest: bool = False) -> Iterator[str]:
        """``paths`` worth scanning, most likely to type a receiver as a family
        model first: files with family classes or model-less string refs, then
        files quoting a family model name, then files naming a relational field
        pointing to one (order kept within each group). Files with no such hint
        come last with ``keep_rest``, else they are kept in ``leftovers`` when
        unknown receivers count. Contents are read lazily so that a time box
        spends its budget on analysis."""
        search, reg = self.search, self.search.reg
        if family is None:  # members of base: every receiver is related
            yield from paths
            return
        own = {c.path for m in family for c in reg.model_classes(m)}
        own |= unresolved
        rest = [p for p in paths if p not in own]
        yield from (p for p in paths if p in own)
        quoted = {q + m.encode() + q for m in family for q in (b"'", b'"')}
        links = set().union(*(search.index.links(reg)[0].get(m, ()) for m in family))
        later = []
        for i, path in enumerate(rest):
            if i % 128 == 0 and search.should_stop(deadline):
                return
            src = search.source(path)[0]
            if not src or not self.pattern.search(src):
                continue
            if _quotes_model(src, family, quoted):
                yield path
            else:
                later.append(path)
        rest_later = []
        for path in later:
            if _has_name(search.source(path)[0] or b"", links):
                yield path
            elif keep_rest:
                rest_later.append(path)
            elif self.accept_unknown:
                self.leftovers.append(path)
        yield from rest_later


# ---------------------------------------------------------------------------
# helpers


def _lookup(reg: Any, which: str) -> Callable[..., list[MemberDef]]:
    return {"field": reg.field, "method": reg.method, "attr": reg.attr}[which]


def _declarations(reg: Any, model: str, name: str, which: str) -> list[MemberDef]:
    """Every (re)definition of the member, in any module, incl. inheriting models."""
    if which == "field":
        return field_implementations(reg, model, name)
    if which == "method":
        return method_implementations(reg, model, name)
    own = [MemberDef("attr", name, c, a) for m in descendants(reg, model) for c in reg.model_classes(m) for a in c.facts[K_ATTRS] if a[A_NAME] == name]
    return reg.attr(model, name)[::-1] + own


def _segment_owners(reg: Any, model: str, path: str, name: str) -> Iterator[tuple[int, str]]:
    """(offset in ``path``, model owning the segment) of each segment of the
    field path equal to ``name``, ``path`` starting on ``model``. Comodels are
    resolved only up to the last such segment (none when it is the first)."""
    segs = path.split(".")
    last = max((i for i, seg in enumerate(segs) if seg == name), default=-1)
    cur: str | None = model
    offset = 0
    for i in range(last + 1):
        if segs[i] == name:
            yield offset, cur  # type: ignore[misc]
        if i < last:
            cur = reg.comodel(cur, segs[i])
            if cur is None:
                return
        offset += len(segs[i]) + 1


def _quotes_model(src: bytes, family: set[str], quoted: set[bytes]) -> bool:
    """``src`` holds a family model name as a string literal (``quoted``: the
    names with their quotes)."""
    if len(quoted) <= _MANY_NEEDLES:
        return any(q in src for q in quoted)
    return not family.isdisjoint(m.decode() for m in _QUOTED_MODEL.findall(src))


def _has_name(src: bytes, names: set[bytes]) -> bool:
    """``src`` contains one of the identifiers ``names`` (plain substring test
    when there are few of them)."""
    if len(names) <= _MANY_NEEDLES:
        return any(n in src for n in names)
    return not names.isdisjoint(_IDENT.findall(src))


def _entry_order(entry: tuple) -> tuple:
    return (entry[2], entry[4], entry[0])  # non-test first, then load order, then path


def _py_scope(facts: dict, line: int, name: str) -> tuple[str, int, int, str | None]:
    """Where a Python name defined at ``line`` lives: ("module"|"member"|"local"|
    "file", first line, last line, owner class)."""
    for s in facts.get(SYMBOLS) or ():
        if s[S_LINE] == line and s[S_NAME] == name:
            return ("module", 0, 0, None)
    for c in facts.get(CLASSES) or ():
        if c[K_LINE] <= line <= c[K_END_LINE]:
            for m in c[K_METHODS]:
                if m[M_LINE] <= line <= m[M_END_LINE]:
                    if line == m[M_LINE] and m[M_NAME] == name:
                        return ("member", 0, 0, c[K_NAME])
                    return ("local", m[M_LINE], m[M_END_LINE], None)
            return ("member", 0, 0, c[K_NAME])
    for s in facts.get(SYMBOLS) or ():
        if s[S_KIND] == "def" and s[S_LINE] <= line <= s[S_END_LINE]:
            return ("local", s[S_LINE], s[S_END_LINE], None)
    return ("file", 0, 0, None)


def _bare_rx(name: str) -> re.Pattern:
    return re.compile(r"(?<![\w.])(" + re.escape(name) + r")\b")


def _attr_rx(name: str) -> re.Pattern:
    return re.compile(r"\.\s*(" + re.escape(name) + r")\b")


def _qualified_rx(alias: str, name: str) -> re.Pattern:
    return re.compile(r"(?<![\w.])" + re.escape(alias) + r"\s*\.\s*(" + re.escape(name) + r")\b")


def _js_specs(reg: Any, path: str) -> set[str]:
    """Non-relative specifiers resolving to the JS file ``path`` (the inverse
    of ``analysis.js.resolve_specifier``)."""
    mod = reg.module_of_path(path)
    info = reg.module(mod)
    if info is None:
        return set()
    rel = os.path.relpath(path, info.path).replace(os.sep, "/")
    specs = {alias for alias, (lib_mod, lib_rel) in LIB_ALIASES.items() if lib_mod == mod and lib_rel == rel}
    for prefix, base in (("static/src/", ""), ("static/tests/", "../tests/"), ("static/lib/", "../lib/")):
        if rel.startswith(prefix) and rel.endswith(".js"):
            spec = f"@{mod}/{base}{rel[len(prefix) : -3]}"
            specs |= {spec, spec + ".js"}
            if spec.endswith("/index"):
                specs.add(spec[: -len("/index")])
    return specs


def _js_spec_matches(importer: str, spec: str, path: str, specs: set[str]) -> bool:
    """Specifier ``spec`` written in ``importer`` resolves to ``path`` (``specs``:
    its non-relative specifiers)."""
    if not spec.startswith("."):
        return spec in specs
    target = os.path.normpath(os.path.join(os.path.dirname(importer), spec))
    return path in (target, target + ".js", os.path.join(target, "index.js"))


def _js_declared(code: _PyCode, line: int, col: int) -> str | None:
    """Identifier declared at ``line:col`` of a JS file (None for an anonymous
    ``export default``)."""
    start = code.offset(line)
    end = code.text.find("\n", start)
    text = code.text[start : end if end >= 0 else len(code.text)]
    m = _JS_IDENT.match(text, utf16_to_index(text, col))
    if m is None or m.group() in ("export", "default") or text[m.end() :].lstrip().startswith("."):
        return None  # anonymous default export, or ``export default expr.member``: no local binding
    return m.group()


def _js_exported_as(code: _PyCode, line: int, local: str) -> list[str]:
    """Names under which a JS module exports its declaration ``local`` on
    ``line``: ``export [default] function local``, ``export {local as x}``,
    ``export default local``."""
    start = code.offset(line)
    m = re.match(r"[ \t]*export\s+(default\b)?", code.text[start : start + 80])
    out = (["default" if m.group(1) else local] if m else []) + _js_reexports(code.text, local)
    return list(dict.fromkeys(out))


def _js_star_reexport(text: str, importer: str, path: str, specs: set[str]) -> bool:
    """``text`` re-exports every name of ``path`` (``export * from``)."""
    if "export" not in text:
        return False
    return any(_js_spec_matches(importer, m.group(2), path, specs) for m in _JS_STAR_EXPORT.finditer(text))


def _js_reexports(text: str, local: str) -> list[str]:
    """Names under which a module exports its binding ``local`` (``export {local
    as x}``, ``export {x as local} from``, ``export default local``)."""
    if "export" not in text:
        return []
    out = []
    for m in _JS_EXPORT_LIST.finditer(text):
        for part in m.group(1).split(","):
            name, _, alias = part.strip().partition(" as ")
            name, alias = name.strip(), alias.strip()
            if m.group(2):  # export {name as alias} from: ``alias`` is the local binding
                if (alias or name) == local:
                    out.append(local)
            elif name == local:
                out.append(alias or name)
    out.extend("default" for m in _JS_EXPORT_DEFAULT.finditer(text) if m.group(1) == local)
    return out


def _js_bare_rx(name: str) -> re.Pattern:
    return re.compile(r"(?:(?<=\.\.\.)|(?<![\w$.]))(" + re.escape(name) + r")(?![\w$])")


def _js_member_rx(namespace: str, name: str) -> re.Pattern:
    return re.compile(r"(?<![\w$.])" + re.escape(namespace) + r"\s*\.\s*(" + re.escape(name) + r")(?![\w$])")



class _PyCode:
    """Python (or JS) text with its string/comment spans and line offsets."""

    __slots__ = ("text", "starts", "ends", "lines", "kwargs")

    def __init__(self, text: str, skip: re.Pattern = _PY_SKIP, kwargs: bool = True):
        self.text = text
        self.kwargs = kwargs  # ``name=`` is a keyword argument (Python), not a use
        spans = [m.span() for m in skip.finditer(text)]
        self.starts = [s for s, _ in spans]
        self.ends = [e for _, e in spans]
        self.lines = [0] + [m.end() for m in re.finditer("\n", text)]

    def skipped(self, pos: int) -> bool:
        i = bisect_right(self.starts, pos) - 1
        return i >= 0 and pos < self.ends[i]

    def offset(self, line: int) -> int:
        return self.lines[line] if 0 <= line < len(self.lines) else len(self.text)

    def position(self, start: int, end: int) -> tuple[int, int, int]:
        line = bisect_right(self.lines, start) - 1
        base = self.lines[line]
        col = utf16_len(self.text[base:start])
        return line, col, col + utf16_len(self.text[start:end])

__all__ = ["references", "references_for_target", "MAX_REFERENCES"]
