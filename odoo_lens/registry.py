"""The in-memory semantic index of one env, built from facts.

Terminology
-----------
* *scope*: a ``frozenset`` of module names, normally ``closure(module)`` of the
  module the user's file belongs to (the module and all its transitive
  dependencies) - i.e. what is guaranteed to be loaded when that code runs.
  ``None`` means "everything in the env" (used for files outside any module,
  e.g. the framework, and as a fallback when a scoped lookup finds nothing).
* *load order*: Odoo loads modules level by level of dependency depth, by name
  within a level; every module gets a ``rank`` in that order and contributions
  are sorted by ``(rank, path, line)``.

Odoo class-composition semantics replicated here (``BaseModel._build_model``):
* a class with ``_name = X`` whose ``_inherit`` does not contain X *defines* X;
  the other ``_inherit`` entries are parents (mixins);
* a class without ``_name`` and with ``_inherit = X`` (str or list: first item),
  or with ``_name = X`` and ``X in _inherit``, *extends* X;
* every model except ``base`` implicitly has ``base`` as last parent, so classes
  extending ``base`` extend every model;
* ``_inherits = {parent: m2o}`` delegates the fields (not the methods) of parent;
* the framework classes (``BaseModel`` & co in odoo/models.py or
  odoo/orm/models.py) close every MRO and provide magic fields and ORM methods;
* MRO(X) = X's classes most-derived first, then each parent's MRO in order of
  introduction, then ``base``, then the framework classes; duplicates keep their
  LAST occurrence (Odoo's LastOrderedSet / Python C3 behaviour).

Thread-safety: only the server's dispatcher thread touches a published registry.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any, Iterable, Iterator

from .facts import (
    CLASSES,
    IMPORTS,
    F_ARGS,
    F_COMODEL,
    F_NAME,
    F_TYPE,
    K_ATTRS,
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
    MANIFEST_DATA,
    MANIFEST_DEMO,
    MANIFEST_DEPENDS,
    MANIFEST_VALUES,
    MI_ATTRS,
    MI_INHERIT,
    MI_INHERIT_STR,
    MI_INHERITS,
    MI_KIND,
    MI_NAME,
    R_KIND,
    R_VALUE,
    RECORDS,
    REF_FIELD,
    REF_METHOD,
    REF_MODEL,
    REF_MODULE,
    REF_QWEB,
    REF_XMLID,
    REFS,
    X_ID,
    X_INFO,
    X_MODEL,
    X_TAG,
)

# Fields every model has (16-18: synthesized by MetaModel; 19+: id/display_name
# declared on BaseModel, log-access fields still synthesized).
MAGIC_FIELDS = {
    "id": ("Id", None, {"string": "ID", "readonly": True}),
    "display_name": ("Char", None, {"string": "Display Name", "compute": "_compute_display_name"}),
    "create_uid": ("Many2one", "res.users", {"string": "Created by", "readonly": True}),
    "create_date": ("Datetime", None, {"string": "Created on", "readonly": True}),
    "write_uid": ("Many2one", "res.users", {"string": "Last Updated by", "readonly": True}),
    "write_date": ("Datetime", None, {"string": "Last Updated on", "readonly": True}),
    "__last_update": ("Datetime", None, {"string": "Last Modified on", "compute": "_compute_concurrency_field"}),
}
LOG_ACCESS_FIELDS = ("create_uid", "create_date", "write_uid", "write_date")
OWN_CLASS_ATTRS = frozenset({"_description", "_table", "_auto", "_log_access"})
# module sub-directories whose Python files are never imported when Odoo loads a module
UNLOADED_DIRS = frozenset({"tests", "migrations", "upgrades", "upgrade", "static"})
_CAMEL = re.compile(r"(?<=[^_])([A-Z])")
_MAGIC_ADD = re.compile(rb"\badd(?:_default)?\(\s*(?:'(\w+)'|(self\.CONCURRENCY_CHECK_FIELD))")


def _magic_positions(path: str) -> dict[str, tuple[int, int, int]]:
    """(line, col, end_col) of the name of each magic field added by MetaModel in
    ``path`` (the framework file defining BaseModel)."""
    try:
        with open(path, "rb") as fh:
            src = fh.read()
    except OSError:
        return {}
    out: dict[str, tuple[int, int, int]] = {}
    for m in _MAGIC_ADD.finditer(src):
        g = 1 if m.group(1) else 2
        name = m.group(1).decode() if g == 1 else "__last_update"
        if name in out:
            continue
        start = m.start(g)
        bol = src.rfind(b"\n", 0, start) + 1
        line = src.count(b"\n", 0, start)
        out[name] = (line, start - bol, m.end(g) - bol)
    return out

FRAMEWORK_CHAINS = {
    "Model": ("Model", "AbstractModel", "BaseModel"),
    "CachedModel": ("CachedModel", "Model", "AbstractModel", "BaseModel"),
    "TransientModel": ("TransientModel", "Model", "AbstractModel", "BaseModel"),
    "AbstractModel": ("AbstractModel", "BaseModel"),
    "BaseModel": ("BaseModel",),
    None: ("Model", "AbstractModel", "BaseModel"),
}


class ModuleInfo:
    """A module (addon) of the env."""

    __slots__ = ("name", "path", "root", "manifest", "depends", "depth", "rank", "values")

    def __init__(self, name: str, path: str, root: str):
        self.name = name
        self.path = path  # module directory
        self.root = root  # addons root containing it
        self.manifest = os.path.join(path, "__manifest__.py")
        self.depends: tuple[str, ...] = ()  # direct dependencies (manifest 'depends'; base implied)
        self.depth = 0  # 0 for base, 1 + max(depth of deps) otherwise
        self.rank = 0  # position in load order
        self.values: dict = {}  # literal manifest values (name, version, summary, ...)

    def __repr__(self) -> str:
        return f"<Module {self.name} rank={self.rank}>"


class ClassRef:
    """One Python class contributing to a model (or a framework model class)."""

    __slots__ = ("path", "index", "module", "facts", "model", "is_definition", "rank")

    def __init__(self, path: str, index: int, module: str | None, facts: tuple, model: str, is_definition: bool, rank: int):
        self.path = path
        self.index = index  # index in the file's CLASSES list
        self.module = module  # None for framework classes
        self.facts = facts  # the Class tuple (K_* constants)
        self.model = model  # model name this class contributes to ('' for framework classes)
        self.is_definition = is_definition  # True if it introduces the model (_name, not an extension)
        self.rank = rank  # module load rank (framework classes: -1)

    @property
    def model_info(self) -> dict:
        return self.facts[K_MODEL] or {}

    @property
    def name(self) -> str:
        return self.facts[K_NAME]

    @property
    def line(self) -> int:
        return self.facts[K_LINE]

    def parents(self) -> list[str]:
        return [p for p in self.model_info.get(MI_INHERIT, ()) if p != self.model]

    def __repr__(self) -> str:
        return f"<ClassRef {self.model or self.name} {self.module} {os.path.basename(self.path)}:{self.line + 1}>"


class MemberDef:
    """A field / method / class attribute definition found in a ClassRef."""

    __slots__ = ("kind", "name", "cls", "facts", "delegated_via")

    def __init__(self, kind: str, name: str, cls: ClassRef, facts: tuple, delegated_via: str | None = None):
        self.kind = kind  # "field" | "method" | "attr"
        self.name = name
        self.cls = cls
        self.facts = facts  # Field / Method / ClassAttr tuple
        self.delegated_via = delegated_via  # for _inherits: the m2o field name on the child model

    @property
    def path(self) -> str:
        return self.cls.path

    @property
    def module(self) -> str | None:
        return self.cls.module

    @property
    def model(self) -> str:
        return self.cls.model

    @property
    def line(self) -> int:
        return self.facts[1]

    @property
    def col(self) -> int:
        return self.facts[2]

    @property
    def end_col(self) -> int:
        return self.facts[3]

    def __repr__(self) -> str:
        return f"<{self.kind} {self.cls.model}.{self.name} {self.module} {os.path.basename(self.path)}:{self.line + 1}>"


class RecordRef:
    """An XML/CSV record (or owl template) definition."""

    __slots__ = ("xmlid", "path", "index", "module", "facts")

    def __init__(self, xmlid: str, path: str, index: int, module: str | None, facts: tuple):
        self.xmlid = xmlid  # fully qualified 'module.name' (t-name for owl templates)
        self.path = path
        self.index = index  # index in the file's RECORDS list (-1 for implicit records)
        self.module = module
        self.facts = facts  # Record tuple (X_* constants)

    @property
    def model(self) -> str | None:
        return self.facts[X_MODEL]

    @property
    def tag(self) -> str:
        return self.facts[X_TAG]

    @property
    def info(self) -> dict:
        return self.facts[X_INFO] or {}

    @property
    def line(self) -> int:
        return self.facts[3]

    @property
    def col(self) -> int:
        return self.facts[4]

    @property
    def end_col(self) -> int:
        return self.facts[5]

    def __repr__(self) -> str:
        return f"<Record {self.xmlid} {self.model} {os.path.basename(self.path)}:{self.line + 1}>"


class RefHit:
    """A reference (Ref tuple) located in a file."""

    __slots__ = ("path", "module", "facts")

    def __init__(self, path: str, module: str | None, facts: tuple):
        self.path = path
        self.module = module
        self.facts = facts  # Ref tuple (R_* constants)

    @property
    def kind(self) -> str:
        return self.facts[0]

    @property
    def value(self) -> str:
        return self.facts[1]

    @property
    def line(self) -> int:
        return self.facts[2]

    @property
    def col(self) -> int:
        return self.facts[3]

    @property
    def end_col(self) -> int:
        return self.facts[4]

    @property
    def ctx(self) -> tuple:
        return self.facts[5]

    def __repr__(self) -> str:
        return f"<Ref {self.kind} {self.value} {os.path.basename(self.path)}:{self.line + 1}>"


def _import_key(facts: dict) -> list:
    return [i[:4] for i in facts.get(IMPORTS) or ()]


def _load_keys(modules: dict[str, ModuleInfo], test_rule: bool = False) -> dict[str, tuple[int, str]]:
    """Load-order key ``(depth, order_name)`` of every module: depth is 0 for
    base, else 1 + max over known deps (iteratively, cycles count as depth 0);
    modules load sorted by it. With ``test_rule`` (19+ ``module_graph``), a
    ``test_*`` module loads right after its last loaded dependency: its depth,
    and its name prefixed by that dependency's order name."""
    keys: dict[str, tuple[int, str]] = {}
    for start in modules:
        if start in keys:
            continue
        on_stack = {start}
        stack = [(start, iter(modules[start].depends))]
        while stack:
            name, deps = stack[-1]
            dep = next(deps, None)
            if dep is None:
                stack.pop()
                on_stack.discard(name)
                known = [d for d in modules[name].depends if d in keys and d in modules]
                if test_rule and name.startswith("test_") and known:
                    last = keys[max(known, key=lambda d: keys[d])]
                    keys[name] = (last[0], f"{last[1]} {name}")
                else:
                    keys[name] = (max((keys[d][0] + 1 for d in known), default=0), name)
                continue
            if dep in modules and dep not in keys and dep not in on_stack:
                on_stack.add(dep)
                stack.append((dep, iter(modules[dep].depends)))
    return keys


def model_class_info(facts: tuple, major: int | None = None) -> tuple[str | None, bool, list[str]]:
    """(model, is_definition, parents) for a Class tuple with model info.

    ``_name`` defaulting (MetaModel.__new__): a str ``_inherit`` implies
    ``_name``; for a list ``_inherit`` without ``_name``, 16-18 use the single
    parent (the class name verbatim with several parents) and 19+ derive the
    name from the class name (``HrEmployee`` -> ``hr.employee``)."""
    mi = facts[K_MODEL]
    if not mi:
        return None, False, []
    name = mi.get(MI_NAME)
    inherit = list(mi.get(MI_INHERIT) or ())
    if not name and inherit:
        if mi.get(MI_INHERIT_STR) or major is None:
            name = inherit[0]
        elif major >= 19:
            name = _CAMEL.sub(r".\1", facts[K_NAME]).lower()
        else:
            name = inherit[0] if len(inherit) == 1 else facts[K_NAME]
    if not name:
        return None, False, []
    return name, name not in inherit, [p for p in inherit if p != name]


class Registry:
    """Semantic index of one env. Built once from all facts, then updated per file."""

    def __init__(self, env: Any, files: dict[str, dict], module_dirs: dict[str, str]):
        self.env = env
        self.files = files  # abs path -> facts
        self.module_dirs: dict[str, str] = dict(module_dirs)  # module name -> dir
        self.file_stats: dict[str, tuple[int, int]] = {}  # set by the indexer
        self.shards: dict[str, dict] = {}  # set by the indexer
        self.generation = 0
        self._cache: dict = {}
        self._path_module: dict[str, str | None] = {}
        version = getattr(env, "version", None)
        self._major: int | None = version.major if version is not None else None
        self._set_modules(self.module_dirs)
        self._rebuild()

    def _rebuild(self) -> None:
        self._model_classes: dict[str, list[ClassRef]] = defaultdict(list)
        self._dirty_models: set[str] = set()
        self._framework: dict[str, ClassRef] = {}
        self._children: dict[str, set[str]] = defaultdict(set)  # parent model -> models having it as parent
        self._members: dict[str, set[str]] = defaultdict(set)  # member name -> models ('' = framework)
        self._xmlids: dict[str, list[RecordRef]] = defaultdict(list)
        self._templates: dict[str, list[RecordRef]] = defaultdict(list)
        self._view_children: dict[str, list[RecordRef]] = defaultdict(list)
        self._refs: dict[tuple[str, str], list[tuple[str, tuple]]] = defaultdict(list)  # (kind, key) -> [(path, ref)]
        self._py_modules: dict[str, str] = {}  # dotted -> path
        self._py_dotted: dict[str, str] = {}  # path -> dotted
        self._contrib: dict[str, tuple] = {}  # path -> (classes, records) the file added
        self._loaded = self._compute_loaded()
        for path, facts in self.files.items():
            self._add_file(path, facts)

    def _import_targets(self, path: str) -> list[str]:
        """Python files imported (at module level) by ``path``, in source order."""
        facts = self.files.get(path)
        if not facts or facts.get(KIND) != KIND_PY:
            return []
        files = self.files
        here = os.path.dirname(path)
        out: list[str] = []
        for imp in facts.get(IMPORTS) or ():
            mod, name, level = imp[0], imp[1], imp[3]
            targets = []
            if level:
                d = here
                for _ in range(level - 1):
                    d = os.path.dirname(d)
                sub = os.path.join(d, *mod.split(".")) if mod else d
                targets.append(sub)
                if name and name != "*":
                    targets.append(os.path.join(sub, name))
            elif mod.startswith("odoo.addons."):
                parts = mod.split(".")[2:]
                other = self._modules.get(parts[0]) if parts else None
                if other is not None:
                    sub = os.path.join(other.path, *parts[1:])
                    targets.append(sub)
                    if name and name != "*":
                        targets.append(os.path.join(sub, name))
            for t in targets:
                for cand in (t + ".py", os.path.join(t, "__init__.py")):
                    if cand in files and cand not in out:
                        out.append(cand)
        return out

    def _compute_loaded(self) -> dict[str, int]:
        """Python files Odoo actually imports when loading each module (everything
        reachable through imports from the module's ``__init__.py``), mapped to
        their execution order: a post-order walk of the imports in source order,
        i.e. the order in which their classes get defined (and registered).
        Model classes in other files (forgotten imports, scripts) never register."""
        order: dict[str, int] = {}
        counter = 0
        for info in self._modules.values():
            init = os.path.join(info.path, "__init__.py")
            if init not in self.files or init in order:
                continue
            visiting = {init}
            stack = [(init, iter(self._import_targets(init)))]
            while stack:
                f, children = stack[-1]
                nxt = next(children, None)
                if nxt is None:
                    stack.pop()
                    order[f] = counter
                    counter += 1
                    continue
                if nxt in order or nxt in visiting:
                    continue
                visiting.add(nxt)
                stack.append((nxt, iter(self._import_targets(nxt))))
        return order

    def is_loaded(self, path: str) -> bool:
        """False for a module's .py file that no import chain from its __init__.py reaches."""
        module = self.module_of_path(path)
        if module is None:
            return True
        return path in self._loaded

    # =====================================================================
    # maintenance

    def _bump(self) -> None:
        self.generation += 1
        self._cache.clear()

    def update_file(self, path: str, facts: dict) -> None:
        """Replace the facts of one file (new or changed) and update every index."""
        self.update_files({path: facts})

    def remove_file(self, path: str) -> None:
        self.update_files({path: None})

    def set_modules(self, module_dirs: dict[str, str]) -> None:
        """Module set changed (module added/removed): recompute everything."""
        self.update_files({}, module_dirs, force=True)

    def update_files(self, changes: dict[str, dict | None], module_dirs: dict[str, str] | None = None, force: bool = False) -> None:
        """Apply many file changes at once (facts, or None for a removed file),
        optionally with a new module set.

        Incremental per file unless the module set or the set of files reachable
        from the modules' ``__init__.py`` changes - then ONE full rebuild covers
        the whole batch (a git checkout adding modules must not rebuild per file)."""
        full = force or (module_dirs is not None and module_dirs != self.module_dirs)
        check_loaded = False
        manifests = False
        for path, facts in changes.items():
            old = self.files.get(path)
            kinds = {(old or {}).get(KIND), (facts or {}).get(KIND)}
            if KIND_MANIFEST in kinds:
                manifests = True
            if KIND_PY in kinds and self.module_of_path(path) is not None:
                if old is None or facts is None or _import_key(old) != _import_key(facts):
                    check_loaded = True
        if not full:
            for path in changes:
                if path in self._contrib:
                    self._remove_file(path)  # needs the old facts, still in self.files
        for path, facts in changes.items():
            if facts is None:
                self.files.pop(path, None)
            else:
                self.files[path] = facts
        if not full and check_loaded and self._compute_loaded() != self._loaded:
            full = True  # a file became (un)reachable: re-register everything
        if full:
            if module_dirs is not None:
                self.module_dirs = dict(module_dirs)
            self._path_module.clear()
            self._set_modules(self.module_dirs)
            self._rebuild()
            self._bump()
            return
        if manifests:
            self._compute_graph()
            for lst in self._model_classes.values():  # ranks may have changed
                for c in lst:
                    c.rank = self._rank(c.module)
            self._dirty_models.update(self._model_classes)
        for path, facts in changes.items():
            if facts is not None:
                self._add_file(path, facts)
        self._bump()

    def _set_modules(self, module_dirs: dict[str, str]) -> None:
        self._modules: dict[str, ModuleInfo] = {}
        self._dir_module: dict[str, str] = {}
        self._path_module.clear()
        for name, d in module_dirs.items():
            info = ModuleInfo(name, d, os.path.dirname(d))
            self._modules[name] = info
            self._dir_module[d] = name
        self._compute_graph()

    def _compute_graph(self) -> None:
        for name, info in self._modules.items():
            facts = self.files.get(info.manifest)
            values: dict = {}
            deps: list[str] = []
            if facts and facts.get(KIND) == KIND_MANIFEST:
                values = facts.get(MANIFEST_VALUES) or {}
                deps = [d[0] for d in facts.get(MANIFEST_DEPENDS) or ()]
            info.values = values
            if not deps and name != "base":
                deps = ["base"]
            info.depends = tuple(dict.fromkeys(deps))
        modules = self._modules
        keys = _load_keys(modules, test_rule=(self._major or 0) >= 19)
        order = sorted(modules, key=keys.__getitem__)
        for rank, name in enumerate(order):
            info = modules[name]
            info.depth = keys[name][0]
            info.rank = rank
        # load order of each module's data files: manifest 'data' then 'demo'
        self._data_order: dict[str, int] = {}
        for info in modules.values():
            facts = self.files.get(info.manifest)
            if not facts or facts.get(KIND) != KIND_MANIFEST:
                continue
            entries = list(facts.get(MANIFEST_DATA) or ()) + list(facts.get(MANIFEST_DEMO) or ())
            for i, entry in enumerate(entries):
                self._data_order.setdefault(os.path.normpath(os.path.join(info.path, entry[0])), i)
        self._cache.clear()

    def _ref_keys(self, facts: dict, module: str | None) -> Iterator[tuple[str, str]]:
        qualify = self.qualify_xmlid
        for ref in facts.get(REFS) or ():
            rk = ref[0]
            if rk == REF_FIELD or rk == REF_METHOD:
                role = ref[5][0]
                if role == "record" or role == "csv_header":
                    continue  # high-volume: searched on demand (see refs_in_records)
                v = ref[1]
                if "." in v:
                    for seg in v.split("."):
                        yield (rk, seg), ref
                else:
                    yield (rk, v), ref
            elif rk == REF_XMLID:
                yield (rk, qualify(ref[1], module)), ref
            elif rk == REF_MODEL or rk == REF_QWEB or rk == REF_MODULE:
                yield (rk, ref[1]), ref

    def _add_file(self, path: str, facts: dict) -> None:
        kind = facts.get(KIND)
        module = self.module_of_path(path)
        info = self._modules.get(module) if module else None
        rank = info.rank if info is not None else -1
        classes: list[ClassRef] = []
        records: list[RecordRef] = []
        if kind == KIND_PY:
            fw = self.env.framework_dir if self.env is not None else None
            in_fw = bool(fw) and path.startswith(fw + os.sep) and module is None
            members = self._members
            loaded = info is None or (
                path in self._loaded
                and not UNLOADED_DIRS.intersection(os.path.relpath(os.path.dirname(path), info.path).split(os.sep))
            )
            major = self._major
            for idx, cls in enumerate(facts.get(CLASSES) or ()):
                mi = cls[K_MODEL]
                if not mi:
                    continue
                if not loaded and mi.get(MI_KIND) != "framework":
                    continue  # tests/migrations: never part of the registry
                if (mi.get(MI_ATTRS) or {}).get("_register") is False and mi.get(MI_KIND) != "framework":
                    continue
                if mi.get(MI_KIND) == "framework":
                    if in_fw or module is None:
                        ref = ClassRef(path, idx, None, cls, "", False, -1)
                        self._framework[cls[K_NAME]] = ref
                        classes.append(ref)
                        for f in cls[K_FIELDS]:
                            members[f[0]].add("")
                        for m in cls[K_METHODS]:
                            members[m[0]].add("")
                        for a in cls[K_ATTRS]:
                            members[a[0]].add("")
                    continue
                model, is_def, parents = model_class_info(cls, major)
                if not model:
                    continue
                ref = ClassRef(path, idx, module, cls, model, is_def, rank)
                self._model_classes[model].append(ref)
                self._dirty_models.add(model)
                classes.append(ref)
                for p in parents:
                    self._children[p].add(model)
                for f in cls[K_FIELDS]:
                    members[f[0]].add(model)
                for m in cls[K_METHODS]:
                    members[m[0]].add(model)
                for a in cls[K_ATTRS]:
                    members[a[0]].add(model)
            dotted = self._dotted_for(path, module)
            if dotted:
                self._py_modules[dotted] = path
                self._py_dotted[path] = dotted
        elif kind == KIND_XML or kind == KIND_CSV:
            qualify = self.qualify_xmlid
            for idx, rec in enumerate(facts.get(RECORDS) or ()):
                xid = rec[0]
                if not xid:
                    continue
                if rec[2] == "qweb":
                    r = RecordRef(xid, path, idx, module, rec)
                    if (rec[7] or {}).get("mode") != "extension":  # an extension only patches its parent
                        self._templates[xid].append(r)
                    records.append(r)
                    inherit = (rec[7] or {}).get("inherit")
                    if inherit:
                        self._view_children["qweb:" + inherit].append(r)
                    continue
                r = RecordRef(qualify(xid, module), path, idx, module, rec)
                self._xmlids[r.xmlid].append(r)
                records.append(r)
                parent = (rec[7] or {}).get("inherit_id")
                if parent:
                    self._view_children[qualify(parent, module)].append(r)
        refs = self._refs
        for key, ref in self._ref_keys(facts, module):
            refs[key].append((path, ref))
        if classes or records or facts.get(REFS):
            self._contrib[path] = (classes, records)

    def _remove_file(self, path: str) -> None:
        classes, records = self._contrib.pop(path)
        old = self.files.get(path) or {}
        module = self.module_of_path(path)
        for ref in classes:
            if not ref.model:
                if self._framework.get(ref.name) is ref:
                    del self._framework[ref.name]
                continue
            lst = self._model_classes.get(ref.model)
            if lst is not None:
                try:
                    lst.remove(ref)
                except ValueError:
                    pass
                if not lst:
                    del self._model_classes[ref.model]
            self._dirty_models.add(ref.model)
            for p in ref.parents():
                kids = self._children.get(p)
                if kids is not None and not any(p in c.parents() for c in self._model_classes.get(ref.model, ())):
                    kids.discard(ref.model)
        for ref in classes:
            remaining = self._model_classes.get(ref.model, ()) if ref.model else self._framework.values()
            names = [f[0] for f in ref.facts[K_FIELDS]] + [m[0] for m in ref.facts[K_METHODS]] + [a[0] for a in ref.facts[K_ATTRS]]
            for name in names:
                if not any(
                    any(x[0] == name for x in c.facts[K_FIELDS])
                    or any(x[0] == name for x in c.facts[K_METHODS])
                    or any(x[0] == name for x in c.facts[K_ATTRS])
                    for c in remaining
                ):
                    bucket = self._members.get(name)
                    if bucket is not None:
                        bucket.discard(ref.model)
        for r in records:
            rec = r.facts
            info = rec[7] or {}
            if rec[2] == "qweb":
                buckets = [self._templates.get(r.xmlid)]
                if info.get("inherit"):
                    buckets.append(self._view_children.get("qweb:" + info["inherit"]))
            else:
                buckets = [self._xmlids.get(r.xmlid)]
                if info.get("inherit_id"):
                    buckets.append(self._view_children.get(self.qualify_xmlid(info["inherit_id"], r.module)))
            for bucket in buckets:
                if bucket:
                    try:
                        bucket.remove(r)
                    except ValueError:
                        pass
        for key in {k for k, _ in self._ref_keys(old, module)}:
            bucket = self._refs.get(key)
            if bucket:
                bucket[:] = [h for h in bucket if h[0] != path]
                if not bucket:
                    del self._refs[key]
        dotted = self._py_dotted.pop(path, None)
        if dotted and self._py_modules.get(dotted) == path:
            del self._py_modules[dotted]

    # =====================================================================
    # modules

    def module(self, name: str | None) -> ModuleInfo | None:
        return self._modules.get(name) if name else None

    def modules(self) -> Iterable[ModuleInfo]:
        return self._modules.values()

    def module_of_path(self, path: str) -> str | None:
        """Name of the module containing ``path`` (None for framework / foreign files)."""
        try:
            return self._path_module[path]
        except KeyError:
            pass
        d = os.path.dirname(path)
        found = None
        seen = []
        for _ in range(12):
            if d in self._dir_module:
                found = self._dir_module[d]
                break
            seen.append(d)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        self._path_module[path] = found
        return found

    def closure(self, module: str) -> frozenset[str]:
        """``module`` + all transitive dependencies (cached)."""
        key = ("closure", module)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out: set[str] = set()
        stack = [module]
        while stack:
            m = stack.pop()
            if m in out:
                continue
            out.add(m)
            info = self._modules.get(m)
            if info is not None:
                stack.extend(info.depends)
        res = frozenset(out)
        self._cache[key] = res
        return res

    def dependents(self, module: str) -> frozenset[str]:
        """All modules depending (transitively) on ``module`` (cached)."""
        key = ("dependents", module)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        rev: dict[str, list[str]] = self._cache.get("revdeps")  # type: ignore[assignment]
        if rev is None:
            rev = defaultdict(list)
            for info in self._modules.values():
                for d in info.depends:
                    rev[d].append(info.name)
            self._cache["revdeps"] = rev
        out: set[str] = set()
        stack = list(rev.get(module, ()))
        while stack:
            m = stack.pop()
            if m in out:
                continue
            out.add(m)
            stack.extend(rev.get(m, ()))
        res = frozenset(out)
        self._cache[key] = res
        return res

    def scope_for_path(self, path: str) -> frozenset[str] | None:
        """``closure(module_of_path(path))`` or None outside modules."""
        mod = self.module_of_path(path)
        return self.closure(mod) if mod else None

    # =====================================================================
    # models

    def _sorted_classes(self, model: str) -> list[ClassRef]:
        lst = self._model_classes.get(model)
        if lst is None:
            return []
        if model in self._dirty_models:
            order = self._loaded
            lst.sort(key=lambda c: (c.rank, order.get(c.path, 1 << 30), c.path, c.line))
            self._dirty_models.discard(model)
        return lst

    def model_names(self) -> Iterable[str]:
        return list(self._model_classes)

    def has_model(self, model: str, scope: frozenset[str] | None = None) -> bool:
        lst = self._model_classes.get(model)
        if not lst:
            return False
        if scope is None:
            return True
        return any(c.module in scope for c in lst)

    def model_classes(self, model: str, scope: frozenset[str] | None = None) -> list[ClassRef]:
        """Classes contributing directly to ``model`` (definition + extensions), load order."""
        lst = self._sorted_classes(model)
        if scope is None:
            return list(lst)
        return [c for c in lst if c.module in scope]

    def model_definitions(self, model: str, scope: frozenset[str] | None = None) -> list[ClassRef]:
        return [c for c in self.model_classes(model, scope) if c.is_definition]

    def model_parents(self, model: str, scope: frozenset[str] | None = None) -> list[str]:
        """Parent models (mixins) of ``model`` in order of introduction, without ``base``."""
        out: list[str] = []
        for c in self.model_classes(model, scope):
            for p in c.parents():
                if p not in out and p != "base":
                    out.append(p)
        return out

    def model_inherits(self, model: str, scope: frozenset[str] | None = None) -> dict[str, str]:
        """Merged ``_inherits`` {parent_model: m2o_field} (including inherited ones)."""
        key = ("inherits", model, scope)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out: dict[str, str] = {}
        for c in self.model_mro(model, scope):
            if not c.model:
                continue
            for parent, fname in (c.model_info.get(MI_INHERITS) or {}).items():
                out.setdefault(parent, fname)
            for f in c.facts[K_FIELDS]:  # Many2one(..., delegate=True) is reflected into _inherits
                if f[F_COMODEL] and (f[F_ARGS] or {}).get("delegate") is True:
                    out.setdefault(f[F_COMODEL], f[F_NAME])
        self._cache[key] = out
        return out

    def model_children(self, model: str) -> list[str]:
        """Models listing ``model`` among their parents (e.g. mail.thread -> sale.order, ...)."""
        return sorted(m for m in self._children.get(model, ()) if m in self._model_classes)

    def _bases(self, model: str, scope: frozenset[str] | None) -> list[tuple[str, Any]]:
        """Bases of the registry class of ``model`` as Odoo builds them
        (``add_to_registry``/``_build_model``): for each contributing class in load
        order, ``LastOrderedSet([cls])`` extended with, for every ``_inherit`` entry
        as written (+ 'base'), the previous bases when the entry is the model itself,
        else the parent's registry class. Entries: ("c", ClassRef) | ("m", parent)."""
        key = ("bases", model, scope)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        bases: dict[tuple, tuple[str, Any]] = {}
        for c in self.model_classes(model, scope):
            new: dict[tuple, tuple[str, Any]] = {("c", id(c)): ("c", c)}  # LastOrderedSet: re-adding moves to the end
            parents = list(c.model_info.get(MI_INHERIT) or ())
            if model != "base":
                parents.append("base")
            for p in parents:
                if p == model:
                    for k, e in bases.items():
                        new.pop(k, None)
                        new[k] = e
                else:
                    k = ("m", p)
                    new.pop(k, None)
                    new[k] = ("m", p)
            bases = new
        res = list(bases.values())
        self._cache[key] = res
        return res

    def model_mro(self, model: str, scope: frozenset[str] | None = None) -> list[ClassRef]:
        """Full linearization, most-derived first, ending with the framework classes."""
        key = ("mro", model, scope)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out: list[ClassRef] = []
        roots = [model] if model == "base" or self._model_classes.get(model) else [model, "base"]
        for root in roots:
            # depth-first expansion of the base lists, iterative (no recursive closure:
            # it would form a garbage cycle holding the registry)
            visiting = {root}
            stack = [(root, iter(self._bases(root, scope)))]
            while stack:
                m, it = stack[-1]
                entry = next(it, None)
                if entry is None:
                    stack.pop()
                    visiting.discard(m)
                    continue
                if entry[0] == "c":
                    out.append(entry[1])
                elif entry[1] not in visiting:
                    visiting.add(entry[1])
                    stack.append((entry[1], iter(self._bases(entry[1], scope))))
        seen: set[int] = set()
        res: list[ClassRef] = []
        for c in reversed(out):
            if id(c) in seen:
                continue
            seen.add(id(c))
            res.append(c)
        res.reverse()
        res.extend(self._framework_chain(model, scope))
        self._cache[key] = res
        return res

    def _framework_chain(self, model: str, scope: frozenset[str] | None) -> list[ClassRef]:
        kind = self.model_kind(model)
        out = []
        for name in FRAMEWORK_CHAINS.get(kind, FRAMEWORK_CHAINS[None]):
            ref = self._framework.get(name)
            if ref is not None:
                out.append(ref)
        return out

    def framework_classes(self) -> list[ClassRef]:
        """BaseModel & friends (root of every MRO)."""
        return list(self._framework.values())

    def inherits_from(self, model: str, ancestor: str, scope: frozenset[str] | None = None) -> bool:
        """True if ``ancestor`` is ``model`` or one of its (recursive) parents / _inherits."""
        if model == ancestor or ancestor == "base":
            return True
        key = ("ancestors", model, scope)
        anc = self._cache.get(key)
        if anc is None:
            anc = set()
            stack = [model]
            while stack:
                m = stack.pop()
                for p in self.model_parents(m, scope) + list(self.model_inherits(m, scope)):
                    if p not in anc:
                        anc.add(p)
                        stack.append(p)
            self._cache[key] = anc
        return ancestor in anc

    def model_attr(self, model: str, attr: str, scope: frozenset[str] | None = None) -> Any:
        """Effective literal model attribute (``_description``, ``_order``, ``_rec_name``...).

        ``_description``, ``_table``, ``_auto`` and ``_log_access`` only come from
        the model's own classes (Odoo ignores mixins/parents for them); other
        attributes follow the Python MRO."""
        classes = reversed(self.model_classes(model, scope)) if attr in OWN_CLASS_ATTRS else self.model_mro(model, scope)
        for c in classes:
            attrs = c.model_info.get(MI_ATTRS) or {}
            if attr in attrs:
                return attrs[attr]
        return None

    def model_kind(self, model: str) -> str | None:
        """"Model" | "AbstractModel" | "TransientModel" (from the defining class)."""
        lst = self._sorted_classes(model)
        for c in lst:
            if c.is_definition and c.model_info.get(MI_KIND):
                return c.model_info.get(MI_KIND)
        for c in lst:
            if c.model_info.get(MI_KIND):
                return c.model_info.get(MI_KIND)
        return None

    # =====================================================================
    # members

    def _members_of(self, model: str, scope: frozenset[str] | None, which: str) -> dict[str, list[MemberDef]]:
        key = (which, model, scope)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out: dict[str, list[MemberDef]] = {}
        idx = {"field": K_FIELDS, "method": K_METHODS, "attr": K_ATTRS}[which]
        for c in self.model_mro(model, scope):
            for m in c.facts[idx]:
                out.setdefault(m[0], []).append(MemberDef(which, m[0], c, m))
        if which == "field":
            self._add_magic_fields(model, scope, out)
            for parent, m2o in self.model_inherits(model, scope).items():
                if parent == model:
                    continue
                if m2o not in out and (self._major or 0) < 19:  # 16-18 create the _inherits many2one automatically
                    owner = self._class_declaring(model, scope, "_inherits")
                    if owner is not None:
                        a = next((x for x in owner.facts[K_ATTRS] if x[0] == "_inherits"), None)
                        pos = (a[1], a[2], a[3]) if a else (owner.facts[1], owner.facts[2], owner.facts[3])
                        args = {"comodel_name": parent, "required": True, "ondelete": "cascade", "string": f"Automatically created field to link to parent {parent}"}
                        out[m2o] = [MemberDef("field", m2o, owner, (m2o, pos[0], pos[1], pos[2], pos[0], "Many2one", parent, args))]
                for name, defs in self._members_of(parent, scope, "field").items():
                    if name in out:
                        continue
                    out[name] = [MemberDef("field", name, d.cls, d.facts, delegated_via=m2o) for d in defs]
        self._cache[key] = out
        return out

    def _class_declaring(self, model: str, scope: frozenset[str] | None, attr: str) -> ClassRef | None:
        for c in self.model_mro(model, scope):
            if attr in (c.model_info.get(MI_ATTRS) or {}) or (attr == "_inherits" and c.model_info.get(MI_INHERITS)):
                return c
        return None

    def _log_access(self, model: str, scope: frozenset[str] | None) -> bool:
        """``_log_access`` (default: ``_auto``) as set by the model's OWN classes -
        Odoo ignores the values of mixins/parents for these attributes."""
        auto = None
        for c in reversed(self.model_classes(model, scope)):
            attrs = c.model_info.get(MI_ATTRS) or {}
            if "_log_access" in attrs:
                return bool(attrs["_log_access"])
            if auto is None and "_auto" in attrs:
                auto = bool(attrs["_auto"])
        return True if auto is None else auto

    def _add_magic_fields(self, model: str, scope: frozenset[str] | None, out: dict) -> None:
        base = self._framework.get("BaseModel")
        if base is None:
            return
        anchor = base
        meta_pos = self._cache.get("metamodel_pos")
        if meta_pos is None:
            meta_pos = (base.facts[1], base.facts[2], base.facts[3])
            for cls in (self.files.get(base.path) or {}).get(CLASSES) or ():
                if cls[K_NAME] == "MetaModel":
                    meta_pos = (cls[1], cls[2], cls[3])
                    break
            self._cache["metamodel_pos"] = meta_pos
        magic_pos = self._cache.get("magic_pos")
        if magic_pos is None:  # the ``add('id', ...)`` / ``add_default('create_uid', ...)`` lines
            magic_pos = self._cache["magic_pos"] = _magic_positions(base.path)
        log_access = None
        for name, (ftype, comodel, args) in MAGIC_FIELDS.items():
            if name in out:
                continue
            if name == "__last_update" and self._major != 16:
                continue
            if name in LOG_ACCESS_FIELDS:
                if log_access is None:
                    log_access = self._log_access(model, scope)
                if not log_access:
                    continue
            pos = magic_pos.get(name) or (meta_pos if name not in ("id", "display_name") or (self._major or 0) < 19 else (base.facts[1], base.facts[2], base.facts[3]))
            fargs = dict(args, automatic=True)
            if comodel:
                fargs["comodel_name"] = comodel
            out[name] = [MemberDef("field", name, anchor, (name, pos[0], pos[1], pos[2], pos[0], ftype, comodel, fargs))]

    def fields(self, model: str, scope: frozenset[str] | None = None) -> dict[str, list[MemberDef]]:
        """All fields of ``model`` (name -> definitions, most-derived first)."""
        return self._members_of(model, scope, "field")

    def methods(self, model: str, scope: frozenset[str] | None = None) -> dict[str, list[MemberDef]]:
        return self._members_of(model, scope, "method")

    def attrs(self, model: str, scope: frozenset[str] | None = None) -> dict[str, list[MemberDef]]:
        return self._members_of(model, scope, "attr")

    def field(self, model: str, name: str, scope: frozenset[str] | None = None) -> list[MemberDef]:
        """All definitions of field ``name`` visible on ``model``: own MRO first
        (most-derived first), then fields delegated through ``_inherits``."""
        return self._members_of(model, scope, "field").get(name, [])

    def method(self, model: str, name: str, scope: frozenset[str] | None = None) -> list[MemberDef]:
        """All definitions of method ``name`` in the MRO of ``model``, most-derived first."""
        return self._members_of(model, scope, "method").get(name, [])

    def attr(self, model: str, name: str, scope: frozenset[str] | None = None) -> list[MemberDef]:
        return self._members_of(model, scope, "attr").get(name, [])

    def member(self, model: str, name: str, scope: frozenset[str] | None = None) -> list[MemberDef]:
        """field(), else method(), else attr() - whichever is non-empty first."""
        return self.field(model, name, scope) or self.method(model, name, scope) or self.attr(model, name, scope)

    def field_info(self, model: str, name: str, scope: frozenset[str] | None = None) -> dict | None:
        """Effective field: {"type", "comodel", "args" (merged, base-most first,
        overridden by more derived definitions), "defs": [MemberDef]}."""
        defs = self.field(model, name, scope)
        if not defs:
            return None
        args: dict = {}
        for d in reversed(defs):
            args.update(d.facts[F_ARGS] or {})
        return {"type": defs[0].facts[F_TYPE], "comodel": self.comodel(model, name, scope), "args": args, "defs": defs}

    def comodel(self, model: str, name: str, scope: frozenset[str] | None = None, _depth: int = 0) -> str | None:
        """Comodel of a relational field; follows ``related=`` when the field
        declares no comodel (``company_id = fields.Many2one(related='order_id.company_id')``)."""
        defs = self.field(model, name, scope)
        for d in defs:
            if d.facts[F_COMODEL]:
                return d.facts[F_COMODEL]
        if not defs or _depth > 6:
            return None
        args: dict = {}
        for d in reversed(defs):
            args.update(d.facts[F_ARGS] or {})
        cm = args.get("comodel_name")
        if isinstance(cm, str):
            return cm
        related = args.get("related")
        if isinstance(related, str) and related:
            steps = self.walk_path(model, related, scope)
            if steps and steps[-1][2] and len(steps) == len(related.split(".")):
                return self.comodel(steps[-1][1], steps[-1][0], scope, _depth + 1)
        return None

    def walk_path(self, model: str, path: str, scope: frozenset[str] | None = None) -> list[tuple[str, str | None, list[MemberDef]]]:
        """Resolve a dotted field path segment by segment.

        Returns [(segment, model_owning_segment, definitions)]; resolution stops
        after the first unresolved segment (empty definitions)."""
        out = []
        cur: str | None = model
        for seg in path.split("."):
            if cur is None:
                out.append((seg, None, []))
                break
            defs = self.field(cur, seg, scope)
            out.append((seg, cur, defs))
            if not defs:
                break
            cur = self.comodel(cur, seg, scope)
        return out

    def models_with_member(self, name: str) -> list[str]:
        """Models (and '' for framework classes) defining a field/method/attr named ``name``."""
        return sorted(self._members.get(name, ()))

    # =====================================================================
    # XML ids / records

    def qualify_xmlid(self, raw: str, module: str | None) -> str:
        """'x' -> '<module>.x'; 'm.x' unchanged."""
        if "." in raw or not module:
            return raw
        return f"{module}.{raw}"

    def _implicit_index(self) -> dict[str, list[str]]:
        hit = self._cache.get("implicit")
        if hit is None:
            hit = defaultdict(list)
            for model in self._model_classes:
                hit[model.replace(".", "_")].append(model)
            self._cache["implicit"] = hit
        return hit

    def implicit_record(self, xmlid: str) -> list[RecordRef]:
        """Records Odoo creates implicitly: <module>.model_<model_> and
        <module>.field_<model_>__<field> (module = a module contributing to it)."""
        module, _, local = xmlid.partition(".")
        if not local:
            return []
        out = []
        if local.startswith("model_"):
            for model in self._implicit_index().get(local[6:], ()):
                for c in self._sorted_classes(model):
                    if c.module == module:
                        facts = (local, "ir.model", "implicit", c.facts[1], c.facts[2], c.facts[3], c.facts[4], {"model": model})
                        out.append(RecordRef(xmlid, c.path, -1, module, facts))
                        break
        elif local.startswith("field_") and "__" in local:
            mpart, _, fname = local[6:].rpartition("__")
            for model in self._implicit_index().get(mpart, ()):
                for c in self._sorted_classes(model):
                    if c.module != module:
                        continue
                    for f in c.facts[K_FIELDS]:
                        if f[F_NAME] == fname:
                            facts = (local, "ir.model.fields", "implicit", f[1], f[2], f[3], f[4], {"model": model, "field": fname})
                            out.append(RecordRef(xmlid, c.path, -1, module, facts))
        return out

    def records(self, xmlid: str) -> list[RecordRef]:
        """Definitions of a fully-qualified xml id (load order), implicit ones last."""
        lst = self._xmlids.get(xmlid)
        order = self._data_order
        explicit = sorted(lst, key=lambda r: (self._rank(r.module), order.get(r.path, 1 << 20), r.path, r.line)) if lst else []
        if explicit:
            return explicit
        return self.implicit_record(xmlid)

    def _rank(self, module: str | None) -> int:
        info = self._modules.get(module) if module else None
        return info.rank if info else -1

    def record_at(self, path: str, index: int) -> RecordRef | None:
        facts = self.files.get(path)
        if not facts:
            return None
        recs = facts.get(RECORDS) or ()
        if not 0 <= index < len(recs):
            return None
        rec = recs[index]
        if not rec[X_ID]:
            module = self.module_of_path(path)
            return RecordRef("", path, index, module, rec)
        module = self.module_of_path(path)
        full = rec[X_ID] if rec[X_TAG] == "qweb" else self.qualify_xmlid(rec[X_ID], module)
        return RecordRef(full, path, index, module, rec)

    def xmlids(self) -> Iterable[str]:
        return list(self._xmlids)

    def template_names(self) -> Iterable[str]:
        return list(self._templates)

    def templates(self, name: str) -> list[RecordRef]:
        """Owl/QWeb client templates by t-name."""
        return list(self._templates.get(name, ()))

    def view_model(self, record: RecordRef, _depth: int = 0) -> str | None:
        """Model of an ir.ui.view record (follows inherit_id when no model is set)."""
        info = record.info
        vm = info.get("view_model")
        if vm:
            return vm
        parent = info.get("inherit_id")
        if not parent or _depth > 25:
            return None
        for p in self.records(self.qualify_xmlid(parent, record.module)):
            vm = self.view_model(p, _depth + 1)
            if vm:
                return vm
        return None

    def views_of_model(self, model: str) -> list[RecordRef]:
        idx = self._cache.get("views_by_model")
        if idx is None:
            idx = defaultdict(list)
            for recs in self._xmlids.values():
                for r in recs:
                    if r.model == "ir.ui.view" or r.tag == "template":
                        vm = self.view_model(r)
                        if vm:
                            idx[vm].append(r)
            self._cache["views_by_model"] = idx
        return list(idx.get(model, ()))

    def children_views(self, xmlid: str) -> list[RecordRef]:
        """Views/templates whose inherit_id is ``xmlid`` (or owl templates t-inheriting it)."""
        out = list(self._view_children.get(xmlid, ()))
        out += self._view_children.get("qweb:" + xmlid, ())
        return out

    def model_xmlid(self, model: str) -> str | None:
        """Implicit xml id of a model: '<defining module>.model_<model with _>'."""
        for c in self._sorted_classes(model):
            if c.is_definition and c.module:
                return f"{c.module}.model_{model.replace('.', '_')}"
        return None

    # =====================================================================
    # references

    def _hits(self, kind: str, key: str) -> list[RefHit]:
        return [RefHit(p, self.module_of_path(p), r) for p, r in self._refs.get((kind, key), ())]

    def refs_to_model(self, model: str) -> list[RefHit]:
        out = self._hits(REF_MODEL, model)
        # implicit xml ids model_<model_> used in csv/xml
        underscored = "model_" + model.replace(".", "_")
        for (kind, key), hits in self._refs.items():
            if kind == REF_XMLID and key.endswith(underscored) and key.split(".", 1)[-1] == underscored:
                out.extend(RefHit(p, self.module_of_path(p), r) for p, r in hits)
        return out

    def refs_to_xmlid(self, xmlid: str) -> list[RefHit]:
        return self._hits(REF_XMLID, xmlid)

    def refs_to_template(self, name: str) -> list[RefHit]:
        return self._hits(REF_QWEB, name) + self._hits(REF_XMLID, name)

    def refs_by_name(self, kind: str, name: str) -> list[RefHit]:
        """REF_FIELD / REF_METHOD refs having ``name`` as one of their path segments."""
        return self._hits(kind, name)

    def refs_to_module(self, module: str) -> list[RefHit]:
        return self._hits(REF_MODULE, module)

    # =====================================================================
    # python modules

    def _dotted_for(self, path: str, module: str | None) -> str | None:
        if not path.endswith(".py"):
            return None
        fw = self.env.framework_dir if self.env is not None else None
        if module and module in self._modules:
            rel = os.path.relpath(path, self._modules[module].path)
            parts = ["odoo", "addons", module] + rel[:-3].split(os.sep)
        elif fw and path.startswith(fw + os.sep):
            rel = os.path.relpath(path, os.path.dirname(fw))
            parts = rel[:-3].split(os.sep)
        else:
            return None
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def py_module_file(self, dotted: str) -> str | None:
        """'odoo.addons.sale.models.sale_order' -> file path (module.py or pkg/__init__.py)."""
        return self._py_modules.get(dotted)

    def py_dotted_name(self, path: str) -> str | None:
        return self._py_dotted.get(path)

    def file_facts(self, path: str) -> dict | None:
        return self.files.get(path)

    def iter_files(self, kind: str | None = None) -> Iterator[tuple[str, dict]]:
        for path, facts in self.files.items():
            if kind is None or facts.get(KIND) == kind:
                yield path, facts

    # =====================================================================
    # misc

    def stats(self) -> dict:
        return {
            "files": len(self.files),
            "modules": len(self._modules),
            "models": len(self._model_classes),
            "xmlids": len(self._xmlids),
            "templates": len(self._templates),
            "ref_keys": len(self._refs),
        }


__all__ = ["Registry", "ModuleInfo", "ClassRef", "MemberDef", "RecordRef", "RefHit", "model_class_info", "KIND_JS"]
