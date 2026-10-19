"""Go to definition, type definition and implementation.

The entry points resolve the targets under the cursor (``analysis.targets_at``)
and turn each :class:`~odoo_lens.analysis.targets.Target` into LSP locations
with :func:`locations_for_target`, :func:`type_locations_for_target` and
:func:`implementations_for_target`, which also work on hand-built targets.

Order is part of the answer (Claude reads the first entries first):

* definitions go base-most first - the original declaration, then the overrides
  in load order - except for ``super().m()`` whose first location is the
  definition the call actually reaches;
* lookups are scoped to the dependency closure of the current module and fall
  back to the whole env only when the scoped lookup finds nothing;
* implementations list the target model's own classes first, then models
  inheriting it, then what it inherits.

Results are deduplicated and capped; ambiguous targets (``model`` None with
candidate ``models``) yield the locations of each candidate, best first.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Iterator

from ..analysis import targets_at
from ..analysis.targets import ATTR, FIELD, FILE, METHOD, MODEL, MODULE, PYSYM, TEMPLATE, XMLID, Target
from ..facts import (
    CLASSES,
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
    K_NAME,
    KIND_PY,
    M_COL,
    M_END_COL,
    M_LINE,
    M_NAME,
    MI_INHERITS,
    REF_QWEB,
    S_KIND,
    S_NAME,
    SYMBOLS,
)
from ..registry import ClassRef, MemberDef, RecordRef, RefHit

MAX_DEFINITIONS = 50
MAX_IMPLEMENTATIONS = 100
MAX_CANDIDATE_MODELS = 8
MAX_VIEW_DEPTH = 10
MAX_SUBCLASS_DEPTH = 4

Spot = tuple  # (path, line, col, end_col) - a location before URI conversion


# ---------------------------------------------------------------------------
# entry points


def definition(ctx: Any) -> list[dict]:
    return _collect(ctx, locations_for_target, MAX_DEFINITIONS)


def type_definition(ctx: Any) -> list[dict]:
    return _collect(ctx, type_locations_for_target, MAX_DEFINITIONS)


def implementation(ctx: Any) -> list[dict]:
    return _collect(ctx, implementations_for_target, MAX_IMPLEMENTATIONS)


def locations_for_target(ctx: Any, target: Target) -> list[dict]:
    return _locations(ctx, _definition_spots(ctx, target), MAX_DEFINITIONS)


def type_locations_for_target(ctx: Any, target: Target) -> list[dict]:
    return _locations(ctx, _type_spots(ctx, target), MAX_DEFINITIONS)


def implementations_for_target(ctx: Any, target: Target) -> list[dict]:
    return _locations(ctx, _implementation_spots(ctx, target), MAX_IMPLEMENTATIONS)


def _collect(ctx: Any, per_target: Callable[[Any, Target], list[dict]], cap: int) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for target in targets_at(ctx):
        for loc in per_target(ctx, target):
            start = loc["range"]["start"]
            key = (loc["uri"], start["line"], start["character"])
            if key in seen:
                continue
            seen.add(key)
            out.append(loc)
            if len(out) >= cap:
                return out
    return out


def _locations(ctx: Any, spots: Iterable[Spot], cap: int) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple] = set()
    for path, line, col, end_col in spots:
        key = (path, line, col)
        if key in seen:
            continue
        seen.add(key)
        out.append(ctx.location(path, line, col, end_col))
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# shared helpers (also used by hover and calls)


def class_spot(c: ClassRef) -> Spot:
    f = c.facts
    return (c.path, f[K_LINE], f[K_COL], f[K_END_COL])


def member_spot(d: MemberDef) -> Spot:
    return (d.path, d.line, d.col, d.end_col)


def record_spot(r: RecordRef) -> Spot:
    return (r.path, r.line, r.col, r.end_col)


def module_rank(registry: Any, module: str | None) -> int:
    info = registry.module(module)
    return info.rank if info is not None else -1


def candidate_models(target: Target) -> list[str]:
    """The target's model, else its candidate models (ambiguous receiver), best first."""
    if target.model:
        return [target.model]
    return list(dict.fromkeys(target.models or ()))[:MAX_CANDIDATE_MODELS]


def scopes(scope: frozenset[str] | None) -> tuple:
    """Lookup scopes in fallback order: the dependency closure, then the whole env."""
    return (scope, None) if scope is not None else (None,)


def scoped_lookup(lookup: Callable, model: str, name: str, scope: frozenset[str] | None) -> list[MemberDef]:
    """``lookup(model, name, scope)`` falling back to the unscoped lookup when empty."""
    for sc in scopes(scope):
        defs = lookup(model, name, sc)
        if defs:
            return defs
    return []


def model_classes_ordered(registry: Any, model: str, scope: frozenset[str] | None) -> list[ClassRef]:
    """Classes of ``model``: the defining class(es) first, then the extensions in
    load order. The definition is shown even when it lives outside ``scope``
    (module with a missing dependency)."""
    classes: list[ClassRef] = []
    for sc in scopes(scope):
        classes = registry.model_classes(model, sc)
        if classes:
            break
    defs = [c for c in classes if c.is_definition]
    if not defs and scope is not None:
        defs = registry.model_definitions(model, None)
    return defs + [c for c in classes if not c.is_definition]


def defining_classes(registry: Any, model: str, scope: frozenset[str] | None) -> list[ClassRef]:
    for sc in scopes(scope):
        defs = registry.model_definitions(model, sc)
        if defs:
            return defs
    return registry.model_classes(model, None)[:1]


def field_comodel(registry: Any, model: str, name: str, scope: frozenset[str] | None, _depth: int = 0) -> str | None:
    """Comodel of a relational field, following ``related`` paths (a related
    many2one usually does not repeat its comodel)."""
    for sc in scopes(scope):
        info = registry.field_info(model, name, sc)
        if info is None:
            continue
        if info["comodel"]:
            return info["comodel"]
        related = info["args"].get("related")
        if isinstance(related, str) and _depth < 5:
            seg, owner, defs = registry.walk_path(model, related, sc)[-1]
            if defs and owner:
                return field_comodel(registry, owner, seg, sc, _depth + 1)
        return None
    return None


def class_containing(classes: Iterable[ClassRef], path: str, line: int) -> ClassRef | None:
    for c in classes:
        if c.path == path and c.facts[K_LINE] <= line <= c.facts[K_END_LINE]:
            return c
    return None


def super_definitions(ctx: Any, model: str, name: str, line: int) -> list[MemberDef]:
    """Definitions ``super().name()`` written at ``line`` of ``ctx.path`` can
    reach: the ones below the current class in the MRO of ``model``, the one the
    call reaches first."""
    registry = ctx.registry
    for sc in scopes(ctx.scope):
        mro = registry.model_mro(model, sc)
        # methods first; a property may override a class attribute or a field
        defs = registry.method(model, name, sc) + registry.attr(model, name, sc) + registry.field(model, name, sc)
        current = class_containing(mro, ctx.path, line)
        if current is None:
            below = [d for d in defs if not (d.path == ctx.path and d.cls.facts[K_LINE] <= line <= d.cls.facts[K_END_LINE])]
        else:
            rank = {id(c): i for i, c in enumerate(mro)}
            start = rank[id(current)]
            below = sorted((d for d in defs if rank.get(id(d.cls), -1) > start), key=lambda d: rank[id(d.cls)])
        if below:
            return below
    return []


def method_definitions(ctx: Any, target: Target, model: str) -> list[MemberDef]:
    """Definitions of a METHOD target on ``model`` in navigation order."""
    if target.extra.get("super"):
        line = target.origin[0] if target.origin else ctx.line
        return super_definitions(ctx, model, target.name or "", line)
    return scoped_lookup(ctx.registry.method, model, target.name or "", ctx.scope)[::-1]


# ---------------------------------------------------------------------------
# definition


def _definition_spots(ctx: Any, target: Target) -> Iterator[Spot]:
    registry, scope, kind = ctx.registry, ctx.scope, target.kind
    if kind == MODEL:
        for model in candidate_models(target):
            yield from map(class_spot, model_classes_ordered(registry, model, scope))
    elif kind in (FIELD, ATTR) and target.extra.get("super"):
        line = target.origin[0] if target.origin else ctx.line
        for model in candidate_models(target):
            yield from map(member_spot, super_definitions(ctx, model, target.name or "", line))
    elif kind == FIELD:
        for model in candidate_models(target):
            yield from map(member_spot, scoped_lookup(registry.field, model, target.name or "", scope)[::-1])
    elif kind == METHOD:
        for model in candidate_models(target):
            yield from map(member_spot, method_definitions(ctx, target, model))
    elif kind == ATTR:
        for model in candidate_models(target):
            yield from map(member_spot, scoped_lookup(registry.attr, model, target.name or "", scope)[::-1])
    elif kind == XMLID:
        yield from map(record_spot, registry.records(target.xmlid or ""))
    elif kind == TEMPLATE:
        name = target.xmlid or target.name or ""
        yield from map(record_spot, registry.templates(name) or registry.records(name))
    elif kind == MODULE:
        info = registry.module(target.name)
        if info is not None:
            yield (info.manifest, 0, 0, 0)
    elif kind == FILE:
        if target.path:
            yield (target.path, 0, 0, 0)
    elif kind == PYSYM:
        if target.path:
            yield (target.path, target.line, target.col, target.end_col)


# ---------------------------------------------------------------------------
# type definition


def _type_spots(ctx: Any, target: Target) -> Iterator[Spot]:
    registry, scope, kind = ctx.registry, ctx.scope, target.kind
    if kind == FIELD:
        name = target.name or ""
        for model in candidate_models(target):
            comodel = field_comodel(registry, model, name, scope)
            if comodel:
                yield from map(class_spot, defining_classes(registry, comodel, scope))
            else:
                defs = scoped_lookup(registry.field, model, name, scope)
                if defs:
                    yield member_spot(defs[-1])
    elif kind in (MODEL, METHOD, ATTR):
        for model in candidate_models(target):
            yield from map(class_spot, defining_classes(registry, model, scope))
    elif kind == XMLID:
        for rec in registry.records(target.xmlid or "")[:1]:
            if rec.model:
                yield from map(class_spot, defining_classes(registry, rec.model, scope))


# ---------------------------------------------------------------------------
# implementation


def _implementation_spots(ctx: Any, target: Target) -> Iterator[Spot]:
    registry, kind = ctx.registry, target.kind
    name = target.name or ""
    if kind == METHOD:
        for model in candidate_models(target):
            yield from map(member_spot, method_implementations(registry, model, name))
    elif kind == FIELD:
        for model in candidate_models(target):
            yield from map(member_spot, field_implementations(registry, model, name))
    elif kind == ATTR:
        for model in candidate_models(target):
            yield from map(member_spot, registry.attr(model, name)[::-1] + _descendant_members(registry, model, name, K_ATTRS, "attr"))
    elif kind == MODEL:
        for model in candidate_models(target):
            yield from map(class_spot, model_implementations(registry, model))
    elif kind == XMLID:
        yield from map(record_spot, view_implementations(registry, target.xmlid or ""))
    elif kind == TEMPLATE:
        key = target.xmlid or name
        yield from map(record_spot, view_implementations(registry, key))
        yield from map(record_spot, registry.templates(key)[1:])
        for hit in template_extensions(registry, key):
            yield (hit.path, hit.line, hit.col, hit.end_col)
    elif kind == MODULE:
        yield from module_dependent_spots(registry, name)
    elif kind == PYSYM and target.path:
        yield from python_implementation_spots(registry, target.path, target.line, name)


def _own_members(registry: Any, model: str, name: str, index: int, kind: str) -> list[MemberDef]:
    """Definitions of ``name`` in the classes of ``model`` itself (all modules, load order)."""
    return [MemberDef(kind, name, c, m) for c in registry.model_classes(model) for m in c.facts[index] if m[0] == name]


def descendants(registry: Any, model: str) -> set[str]:
    """Models inheriting ``model`` as a parent, recursively (every model for ``base``)."""
    if model == "base":
        return set(registry.model_names()) - {"base"}
    out: set[str] = set()
    stack = [model]
    while stack:
        for child in registry.model_children(stack.pop()):
            if child not in out and child != model:
                out.add(child)
                stack.append(child)
    return out


def _descendant_members(registry: Any, model: str, name: str, index: int, kind: str, delegation: bool = False) -> list[MemberDef]:
    """(Re)definitions of ``name`` in models inheriting ``model`` (``delegation``:
    also in models delegating to it through ``_inherits``), load order."""
    candidates = [m for m in registry.models_with_member(name) if m and m != model]
    if not candidates:
        return []
    inheriting = descendants(registry, model)
    out = []
    for m in candidates:
        classes = registry.model_classes(m)
        if m not in inheriting and not (delegation and any(model in (c.model_info.get(MI_INHERITS) or {}) for c in classes)):
            continue
        out.extend(MemberDef(kind, name, c, x) for c in classes for x in c.facts[index] if x[0] == name)
    out.sort(key=lambda d: (d.cls.rank, d.path, d.line))
    return out


def method_implementations(registry: Any, model: str, name: str) -> list[MemberDef]:
    """Every definition of method ``name`` that can run for ``model``, in any
    module: the model's own classes (load order), overrides in models inheriting
    it (e.g. ``mail.thread.message_post`` overridden by ``sale.order``), then the
    implementations it inherits (MRO order)."""
    own = _own_members(registry, model, name, K_METHODS, "method")
    inherited = [d for d in registry.method(model, name) if d.cls.model != model]
    return own + _descendant_members(registry, model, name, K_METHODS, "method") + inherited


def field_implementations(registry: Any, model: str, name: str) -> list[MemberDef]:
    """Every (re)definition of field ``name`` of ``model`` in any module, base-most
    first, then redefinitions in inheriting / delegating models."""
    return registry.field(model, name)[::-1] + _descendant_members(registry, model, name, K_FIELDS, "field", delegation=True)


def model_implementations(registry: Any, model: str) -> list[ClassRef]:
    """All classes of ``model`` (definition first) and the classes of other models
    listing it in their ``_inherit``."""
    classes = registry.model_classes(model)
    own = [c for c in classes if c.is_definition] + [c for c in classes if not c.is_definition]
    kids = [c for child in registry.model_children(model) for c in registry.model_classes(child) if model in c.parents()]
    kids.sort(key=lambda c: (c.rank, c.path, c.line))
    return own + kids


def view_implementations(registry: Any, key: str) -> list[RecordRef]:
    """Views / templates inheriting ``key`` (breadth first, load order per level),
    then the other definitions of the same xml id (updates from other modules)."""
    out: list[RecordRef] = []
    seen = {key}
    level = [key]
    for _ in range(MAX_VIEW_DEPTH):
        nxt = []
        for k in level:
            for r in sorted(registry.children_views(k), key=lambda r: (module_rank(registry, r.module), r.path, r.line)):
                if r.xmlid in seen:
                    continue
                seen.add(r.xmlid)
                out.append(r)
                nxt.append(r.xmlid)
        if not nxt:
            break
        level = nxt
    return out + registry.records(key)[1:]


def template_extensions(registry: Any, name: str) -> list[RefHit]:
    """``t-inherit`` references to client template ``name`` from templates
    without a t-name of their own (``t-inherit-mode="extension"``); primary
    inheritors are records, listed by ``children_views``."""
    primary = [(r.path, r.line) for r in registry.children_views(name)]
    return [
        h
        for h in registry.refs_to_template(name)
        if h.kind == REF_QWEB and h.ctx and h.ctx[0] == "tinherit"
        and not any(p == h.path and abs(line - h.line) <= 3 for p, line in primary)  # same start tag as a t-name
    ]


def module_dependent_spots(registry: Any, name: str) -> list[Spot]:
    """Manifests of the modules depending on ``name``: direct dependents first
    (at their ``depends`` entry), then the transitive ones."""
    dependents = registry.dependents(name)
    if not dependents:
        return []
    entries = {h.path: h for h in registry.refs_to_module(name) if h.ctx and h.ctx[0] == "depends"}
    infos = sorted((registry.module(m) for m in dependents), key=lambda i: i.rank)
    direct = [i for i in infos if name in i.depends]
    out = []
    for info in direct:
        hit = entries.get(info.manifest)
        out.append((info.manifest, hit.line, hit.col, hit.end_col) if hit else (info.manifest, 0, 0, 0))
    out.extend((i.manifest, 0, 0, 0) for i in infos if name not in i.depends)
    return out


# ---------------------------------------------------------------------------
# plain Python classes (controllers, helpers...): subclasses and overrides


def python_implementation_spots(registry: Any, path: str, line: int, name: str) -> list[Spot]:
    """Subclasses of the class defined at ``path:line``, or for a method of a
    class: the method then its overrides in the subclasses (e.g. a controller
    route overridden by other modules)."""
    for cls in (registry.file_facts(path) or {}).get(CLASSES) or ():
        if cls[K_LINE] == line and cls[K_NAME] == name:
            return [(p, c[K_LINE], c[K_COL], c[K_END_COL]) for p, c in python_subclasses(registry, path, name)]
        for m in cls[K_METHODS]:
            if m[M_LINE] == line and m[M_NAME] == name:
                out = [(path, m[M_LINE], m[M_COL], m[M_END_COL])]
                for p, sub in python_subclasses(registry, path, cls[K_NAME]):
                    out.extend((p, x[M_LINE], x[M_COL], x[M_END_COL]) for x in sub[K_METHODS] if x[M_NAME] == name)
                return out
    return []


def python_subclasses(registry: Any, path: str, name: str) -> list[tuple[str, tuple]]:
    """``(path, Class tuple)`` of the classes deriving from class ``name`` of
    ``path``, breadth first: a base matches when it resolves, through the
    imports of its file, to the same dotted name as the class."""
    origin = registry.py_dotted_name(path)
    if not origin:
        return []
    frontier = {f"{origin}.{name}"}
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, tuple]] = []
    for _ in range(MAX_SUBCLASS_DEPTH):
        names = {d.rpartition(".")[2] for d in frontier}
        nxt: set[str] = set()
        level: list[tuple[str, tuple]] = []
        for p, facts in registry.iter_files(KIND_PY):
            for cls in facts.get(CLASSES) or ():
                if (p, cls[K_LINE]) in seen:
                    continue
                for base in cls[K_BASES]:
                    if base.rpartition(".")[2] in names and canonical_class(registry, base_dotted(registry, p, facts, base)) in frontier:
                        seen.add((p, cls[K_LINE]))
                        level.append((p, cls))
                        dotted = registry.py_dotted_name(p)
                        if dotted:
                            nxt.add(f"{dotted}.{cls[K_NAME]}")
                        break
        level.sort(key=lambda pc: (module_rank(registry, registry.module_of_path(pc[0])), pc[0], pc[1][K_LINE]))
        out.extend(level)
        if not nxt:
            break
        frontier = nxt
    return out


def absolute_module(registry: Any, path: str, module: str, level: int) -> str:
    """Dotted module of an import statement of ``path`` (resolves relative imports)."""
    if not level:
        return module
    parts = (registry.py_dotted_name(path) or "").split(".")
    keep = len(parts) - level + (1 if path.endswith("__init__.py") else 0)
    return ".".join(parts[: max(keep, 0)] + ([module] if module else []))


def base_dotted(registry: Any, path: str, facts: dict, base: str) -> str | None:
    """Dotted name a base-class expression of ``path`` refers to (``main.WebsiteSale``
    with ``from odoo.addons.website_sale.controllers import main`` ->
    ``odoo.addons.website_sale.controllers.main.WebsiteSale``)."""
    head, _, rest = base.partition(".")
    for imp in facts.get(IMPORTS) or ():
        if imp[I_NAME] == "*" or (imp[I_ASNAME] or imp[I_NAME] or imp[I_MODULE].split(".")[0]) != head:
            continue
        if imp[I_NAME]:
            full = f"{absolute_module(registry, path, imp[I_MODULE], imp[I_LEVEL])}.{imp[I_NAME]}"
        else:
            full = imp[I_MODULE] if imp[I_ASNAME] else head
        return f"{full}.{rest}" if rest else full
    own = registry.py_dotted_name(path)
    return f"{own}.{base}" if own else None


def canonical_class(registry: Any, dotted: str | None, hops: int = 3) -> str | None:
    """Follow re-exports (``from .main import WebsiteSale`` in a package) to the
    module that defines the class."""
    for _ in range(hops):
        if not dotted:
            return dotted
        module, _, name = dotted.rpartition(".")
        path = registry.py_module_file(module)
        facts = registry.file_facts(path) if path else None
        if not facts or any(s[S_NAME] == name and s[S_KIND] == "class" for s in facts.get(SYMBOLS) or ()):
            return dotted
        imp = next((i for i in facts.get(IMPORTS) or () if i[I_NAME] and (i[I_ASNAME] or i[I_NAME]) == name), None)
        if imp is None:
            return dotted
        dotted = f"{absolute_module(registry, path, imp[I_MODULE], imp[I_LEVEL])}.{imp[I_NAME]}"
    return dotted

