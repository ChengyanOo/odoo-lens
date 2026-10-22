"""Hover: compact markdown cards explaining the Odoo symbol under the cursor.

Hover is Claude's richest channel (rendered verbatim, never filtered), so a card
packs what would otherwise take several file reads: the merged attributes of a
field over all its definitions, the override chain of a method with its
``super()`` calls, the composition of a model, the details of a record / view,
module metadata. Locations are absolute ``path:line`` (1-based) so they can be
opened directly. Lookups use the dependency closure of the current module;
definitions outside it are reported separately (they only apply when those
modules are installed). Cards stay under ~40 lines: lists are truncated with
"+N more".
"""

from __future__ import annotations

import ast
import csv
import os
import re
import textwrap
from collections import Counter
from typing import Any, Callable

from ..analysis import targets_at
from ..analysis.targets import ATTR, FIELD, FILE, METHOD, MODEL, MODULE, PYSYM, TEMPLATE, XMLID, Target
from ..facts import (
    A_KIND,
    A_VALUE,
    CLASSES,
    F_ARGS,
    F_END_LINE,
    F_LINE,
    I_ASNAME,
    I_LEVEL,
    I_MODULE,
    I_NAME,
    IMPORTS,
    K_DOC,
    K_FIELDS,
    K_LINE,
    K_METHODS,
    KIND,
    KIND_CSV,
    KIND_XML,
    M_DECORATORS,
    M_DOC,
    M_LINE,
    M_PARAMS,
    M_RETURNS,
    M_SUPER,
    MI_ATTRS,
    RECORDS,
    REF_MODEL,
    S_END_LINE,
    S_KIND,
    S_LINE,
    S_NAME,
    SYMBOLS,
    X_END_LINE,
    X_ID,
    X_LINE,
    X_MODEL,
    X_TAG,
)
from ..registry import MemberDef, RecordRef
from .navigation import (
    MEMBER_NAME,
    absolute_module,
    candidate_models,
    field_comodel,
    module_rank,
    super_definitions,
    template_extensions,
    view_implementations,
)

MAX_OVERRIDES = 8
MAX_CHAIN = 12
MAX_FULL_CHAIN = 25
MAX_LIST = 12
MAX_SELECTION = 12
SNIPPET_LINES = 6
VALUE_WIDTH = 100

# field arguments rendered as flags, in this order
FLAG_ARGS = (
    "required", "readonly", "store", "index", "translate", "company_dependent", "tracking", "copy",
    "groups", "precompute", "compute_sudo", "recursive", "delegate", "ondelete", "check_company",
    "auto_join", "bypass_search_access", "aggregator", "group_operator", "size", "digits",
    "currency_field", "sanitize", "attachment", "change_default",
)
# field arguments rendered as their own "key: value" line, in this order
DETAIL_ARGS = ("related", "compute", "inverse", "search", "default", "domain", "context", "inverse_name", "relation", "group_expand", "compute_sql")
METHOD_NAME_ARGS = frozenset({"compute", "inverse", "search", "group_expand", "compute_sql"})
# arguments that only document a definition (never shown as "changed" in overrides)
SILENT_ARGS = frozenset({"comodel_name", "automatic"})
VIEW_TYPE_RE = re.compile(r"<\s*([A-Za-z_][\w\-]*)")
ARCH_RE = re.compile(r"""<field\b[^>]*\bname\s*=\s*["']arch(?:_db)?["']""")
JS_DECL_RE = re.compile(r"""\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(class|function|const|let|var)\b\s*\*?\s*([A-Za-z_$][\w$]*)""")


def hover(ctx: Any) -> dict | None:
    targets = targets_at(ctx)
    md = None
    while targets and not md:
        md = hover_for_target(ctx, targets.pop(0))
    if not md:
        return None
    others = [s for s in (_one_liner(ctx, t) for t in targets[:3]) if s]
    if others:
        md += "\n\n**Other candidates:**\n" + "\n".join(f"- {s}" for s in others)
    return {"contents": {"kind": "markdown" if ctx.server.markdown else "plaintext", "value": md}}


def hover_for_target(ctx: Any, target: Target) -> str | None:
    """Markdown card for one target (None when there is nothing to say)."""
    kind = target.kind
    if kind in (FIELD, METHOD, ATTR) and not target.model:
        return _ambiguous_card(ctx, target)
    guess = target.extra.get("guess") if kind in (FIELD, METHOD, ATTR) else None
    if guess:
        card = _member_card(ctx, target)
        how = "the other members used on it" if guess == "duck-typing" else "the member name"
        what = "View model" if (getattr(ctx, "path", "") or "").endswith(".xml") else "Receiver type"
        note = f"*{what} not inferred: `{target.model}` guessed from {how}.*"
        return f"{note}\n\n{card}" if card else None
    return _member_card(ctx, target) if kind in (FIELD, METHOD, ATTR) else _other_card(ctx, target)


def _member_card(ctx: Any, target: Target) -> str | None:
    kind = target.kind
    if kind == FIELD:
        return field_card(ctx, target.model, target.name or "") or _missing(ctx, target)
    if kind == METHOD:
        return method_card(ctx, target.model, target.name or "", target) or _missing(ctx, target)
    if kind == ATTR:
        return attr_card(ctx, target.model, target.name or "") or _missing(ctx, target)
    return None


def _other_card(ctx: Any, target: Target) -> str | None:
    kind = target.kind
    if kind == MODEL:
        return model_card(ctx, target.model or "") or _missing(ctx, target)
    if kind == XMLID:
        return xmlid_card(ctx, target.xmlid or "") or _missing(ctx, target)
    if kind == TEMPLATE:
        return template_card(ctx, target.xmlid or target.name or "") or _missing(ctx, target)
    if kind == MODULE:
        return module_card(ctx, target.name or "")
    if kind == FILE:
        return file_card(ctx, target.path or "")
    if kind == PYSYM:
        return pysym_card(ctx, target)
    return None


def _missing(ctx: Any, target: Target) -> str:
    """Card for a symbol the env does not define (typo, missing module)."""
    if target.kind == MODEL:
        return f"**{target.model}** · ⚠ model not found in this environment"
    if target.kind in (XMLID, TEMPLATE):
        return f"**{target.xmlid}** · ⚠ {'xml id' if target.kind == XMLID else 'template'} not found in this environment"
    what = {FIELD: "field", METHOD: "method", ATTR: "attribute"}[target.kind]
    if not ctx.registry.has_model(target.model):
        return f"**{target.model}.{target.name}** · ⚠ model `{target.model}` not found in this environment"
    return f"**{target.model}.{target.name}** · ⚠ no {what} `{target.name}` on {target.model} in this environment"


# ---------------------------------------------------------------------------
# formatting helpers


def _at(path: str, line: int) -> str:
    return f"{path}:{line + 1}"


def _mod(module: str | None) -> str:
    return module or "odoo"  # framework classes (BaseModel...) belong to no module


def _clip(text: str, width: int = VALUE_WIDTH) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _value(v: Any, width: int = VALUE_WIDTH) -> str:
    """Source-like rendering of a facts literal (``("=", src)`` holds an expression)."""
    return _clip(v[1] if _is_expr(v) else repr(v), width)


def _more(items: list[str], limit: int, sep: str = ", ") -> str:
    shown = sep.join(items[:limit])
    return shown + (f"{sep}+{len(items) - limit} more" if len(items) > limit else "")


def _snippet(ctx: Any, path: str, start: int, end: int, name: str | None = None) -> list[str]:
    """Up to SNIPPET_LINES source lines as a fenced block; with ``name``, only if
    the first line really assigns/defines it (synthesized members have none)."""
    lines = ctx.server.documents.lines(path)
    if not lines or not 0 <= start < len(lines):
        return []
    chunk = lines[start : min(end, start + SNIPPET_LINES - 1) + 1]
    if name is not None and not re.match(rf"\s*(?:def\s+|async\s+def\s+)?{re.escape(name)}\b", chunk[0]):
        return []
    body = textwrap.dedent("\n".join(l.rstrip("\r") for l in chunk)).rstrip()
    if end > start + SNIPPET_LINES - 1:
        body += "\n    ..."
    return ["```python", body, "```"]


def _modules_of(defs: list) -> list[str]:
    return list(dict.fromkeys(_mod(d.module) for d in defs))


def _not_visible(modules: list[str]) -> str:
    return f"⚠ defined only in modules outside this module's dependencies: {_more(modules, MAX_LIST)}"


def _visible(lookup: Callable, model: str, name: str, scope: frozenset[str] | None) -> tuple[list, bool]:
    """Definitions visible from ``scope``; when there are none, those of the
    whole env, flagged False (defined only outside the dependency closure)."""
    defs = lookup(model, name, scope)
    if defs or scope is None:
        return defs, True
    return lookup(model, name, None), False


def _outside_overrides(registry: Any, model: str, name: str, scope: frozenset[str] | None, index: int, kind: str) -> list[MemberDef]:
    """Definitions of ``name`` in classes of ``model`` itself contributed by
    modules outside ``scope`` (load order) - what applies only if they are
    installed."""
    if scope is None:
        return []
    key = MEMBER_NAME[index]
    return [MemberDef(kind, name, c, m) for c in registry.model_classes(model) if c.module not in scope for m in c.facts[index] if m[key] == name]


# ---------------------------------------------------------------------------
# fields


def field_card(ctx: Any, model: str | None, name: str) -> str | None:
    registry, scope = ctx.registry, ctx.scope
    if not model:
        return None
    defs, visible = _visible(registry.field, model, name, scope)
    if not defs:
        return None
    info = registry.field_info(model, name, scope if visible else None)
    args: dict = info["args"]
    out = []
    head = f"**{model}.{name}** · {info['type']}"
    comodel = info["comodel"] or field_comodel(registry, model, name, scope)
    if comodel:
        head += f" → {comodel}"
    out.append(head)
    if not visible:
        out.append(_not_visible(_modules_of(defs)))
    flags = _field_flags(args)
    if flags:
        out.append(" · ".join(flags))
    label = args.get("string")
    help_text = args.get("help")
    if isinstance(label, str) or isinstance(help_text, str):
        bits = [f'"{_clip(label, 80)}"'] if isinstance(label, str) else []
        if isinstance(help_text, str):
            bits.append(_clip(help_text, 200))
        out.append(" — ".join(bits))
    owner = defs[-1].model if defs[-1].delegated_via else model  # methods / paths of delegated fields live on the parent
    for key in DETAIL_ARGS:
        if args.get(key) is not None:
            out.append(f"{key}: {_detail(ctx, owner, key, args[key], scope if visible else None)}")
    selection = _selection_line(ctx, defs)
    if selection:
        out.append(selection)
    out.extend(_field_origin(model, defs, args))
    out.extend(_override_lines(model, defs[-2::-1], MAX_OVERRIDES))
    outside = _outside_overrides(registry, model, name, scope, K_FIELDS, "field") if visible else []
    if outside:
        refs = [f"{_owner(d, model)} ({_at(d.path, d.line)})" + (f" `{changed}`" if changed else "") for d, changed in ((d, _changed_args(d)) for d in outside)]
        out.append(f"Also overridden outside this module's dependencies (applies only when installed): {_more(refs, 3, '; ')}")
    origin = defs[-1]
    out.extend(_snippet(ctx, origin.path, origin.facts[F_LINE], origin.facts[F_END_LINE], name))
    return "\n".join(out)


def _field_flags(args: dict) -> list[str]:
    flags = []
    for key in FLAG_ARGS:
        if key not in args:
            continue
        v = args[key]
        if v is True:
            flags.append(key)
        elif v is False or v is None:
            flags.append(f"{key}={v}")
        else:
            flags.append(f"{key}={v if isinstance(v, str) else _value(v, 60)}")
    if args.get("automatic"):
        flags.append("automatic")
    return flags


def _detail(ctx: Any, model: str, key: str, v: Any, scope: frozenset[str] | None) -> str:
    """A field argument value, with where it leads (method definition, related field)."""
    registry = ctx.registry
    if not isinstance(v, str) or key == "default":
        return f"`{_value(v)}`"
    text = f"`{_clip(v)}`"
    if key in METHOD_NAME_ARGS:
        defs = registry.method(model, v, scope) or registry.method(model, v, None)
        if defs:
            text += f" — {_at(defs[0].path, defs[0].line)}"
    elif key == "related":
        steps = registry.walk_path(model, v, scope) or []
        if steps and steps[-1][2]:
            seg, owner, defs = steps[-1]
            info = registry.field_info(owner, seg, scope) or {}
            text += f" → **{owner}.{seg}**" + (f" ({info['type']})" if info.get("type") else "")
        elif steps:
            text += f" ⚠ `{steps[-1][0]}` not found on {steps[-1][1] or '?'}"
    return text


def _selection_line(ctx: Any, defs: list[MemberDef]) -> str | None:
    """Selection values merged like Odoo does: the base selection plus every
    ``selection_add`` of the overrides (base-most first). A selection given as a
    module-level constant is resolved from the source; one that cannot be
    (method name, expression) is shown as written, after the known values."""
    values: list[tuple[str, str | None]] = []
    other: tuple[Any, MemberDef] | None = None  # last unresolved selection and its definition
    for d in reversed(defs):
        a = d.facts[F_ARGS] or {}
        sel = a.get("selection")
        if _is_expr(sel) and re.fullmatch(r"[A-Za-z_]\w*", sel[1]):
            const = _constant(ctx, d.path, sel[1])
            if isinstance(const, list):
                sel = const
        if isinstance(sel, list):
            values, other = _pairs(sel), None
        elif sel is not None:
            other = (sel, d)
        add = a.get("selection_add")
        if isinstance(add, list):
            known = {k for k, _ in values}
            for k, label in _pairs(add):
                if k not in known:
                    values.append((k, label))
                    known.add(k)
    parts = []
    if values:
        parts.append(_more([f"`{k}` ({_clip(v, 40)})" if v else f"`{k}`" for k, v in values], MAX_SELECTION))
    if other is not None:
        sel, d = other
        parts.append(f"`{_clip(sel) if isinstance(sel, str) else _value(sel)}`" + (f" (redefined in **{_mod(d.module)}**)" if values else ""))
    return "selection: " + " · ".join(parts) if parts else None


def _is_expr(v: Any) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 2 and v[0] == "=" and isinstance(v[1], str)


def _pairs(items: list) -> list[tuple[str, str | None]]:
    return [(str(x[0]), str(x[1]) if len(x) > 1 else None) for x in items if isinstance(x, (list, tuple)) and x]


def _constant(ctx: Any, path: str, name: str, hops: int = 1) -> Any:
    """Literal value of the module-level constant ``name`` used in ``path``:
    assigned in that file or imported from another module (one hop)."""
    registry = ctx.registry
    facts = registry.file_facts(path) or {}
    for sym in facts.get(SYMBOLS) or ():
        if sym[S_NAME] == name and sym[S_KIND] == "var":
            return _literal_assignment(ctx, path, sym[S_LINE], sym[S_END_LINE])
    if hops <= 0:
        return None
    for imp in facts.get(IMPORTS) or ():
        if (imp[I_ASNAME] or imp[I_NAME]) != name or not imp[I_NAME]:
            continue
        target = registry.py_module_file(absolute_module(registry, path, imp[I_MODULE], imp[I_LEVEL]))
        if target:
            return _constant(ctx, target, imp[I_NAME], hops - 1)
    return None


def _literal_assignment(ctx: Any, path: str, line: int, end_line: int) -> Any:
    lines = ctx.server.documents.lines(path) or []
    if end_line - line > 80:
        return None
    try:
        node = ast.parse(textwrap.dedent("\n".join(lines[line : end_line + 1]))).body[0]
        return ast.literal_eval(node.value) if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value else None
    except (SyntaxError, ValueError, TypeError, IndexError, MemoryError, RecursionError):
        return None


def _field_origin(model: str, defs: list[MemberDef], args: dict) -> list[str]:
    origin = defs[-1]
    if args.get("automatic") and origin.module is None:
        return [f"Automatic field added by the ORM to every model — {_at(origin.path, origin.line)}"]
    out = []
    if origin.delegated_via:
        out.append(f"Delegated from **{origin.model}** via `_inherits` through `{origin.delegated_via}` (related `{origin.delegated_via}.{origin.name}`)")
    out.append(f"Defined in {_owner(origin, model)} — {_at(origin.path, origin.line)}")
    return out


def _owner(d: MemberDef, model: str) -> str:
    """``**module**``, plus the class's model when it is not ``model`` (mixin,
    ``_inherits`` parent) or its name for framework classes."""
    if not d.cls.model:
        return f"**{_mod(d.module)}** ({d.cls.name})"
    if d.cls.model != model:
        return f"**{_mod(d.module)}** ({d.cls.model})"
    return f"**{_mod(d.module)}**"


def _changed_args(d: MemberDef) -> str:
    args = {k: v for k, v in (d.facts[F_ARGS] or {}).items() if k not in SILENT_ARGS}
    return _clip(", ".join(f"{k}={_value(v, 40)}" for k, v in args.items()), 120)


def _override_lines(model: str, overrides: list[MemberDef], limit: int) -> list[str]:
    """Overrides (base-most first) as a bullet list with their changed arguments."""
    if not overrides:
        return []
    out = ["Overridden in:"]
    for d in overrides[:limit]:
        changed = _changed_args(d)
        out.append(f"- {_owner(d, model)} — {_at(d.path, d.line)}" + (f" `{changed}`" if changed else ""))
    if len(overrides) > limit:
        out.append(f"- +{len(overrides) - limit} more")
    return out


# ---------------------------------------------------------------------------
# methods


def method_card(ctx: Any, model: str | None, name: str, target: Target | None = None) -> str | None:
    registry, scope = ctx.registry, ctx.scope
    if not model:
        return None
    chain, visible = _visible(registry.method, model, name, scope)
    if not chain:
        return None
    top = chain[0]
    out = [f"**{model}.{name}**({_clip(top.facts[M_PARAMS] or '', 160)})"]
    if not visible:
        out.append(_not_visible(_modules_of(chain)))
    decorators = next((d.facts[M_DECORATORS] for d in chain if d.facts[M_DECORATORS]), ())
    if decorators:
        out.append(" ".join(f"`@{_decorator(dec)}`" for dec in decorators))
    returns = next((d.facts[M_RETURNS] for d in chain if d.facts[M_RETURNS]), None)
    if returns:
        out.append(f"→ `{returns}`")
    doc = next((d.facts[M_DOC] for d in reversed(chain) if d.facts[M_DOC]), None)
    if doc:
        out.append(f"*{_clip(doc, 200)}*")
    if target is not None and target.extra.get("super"):
        line = target.origin[0] if target.origin else ctx.line
        reached = super_definitions(ctx, model, name, line)
        if reached:
            out.append(f"`super()` here reaches **{_mod(reached[0].module)}** — {_at(reached[0].path, reached[0].line)}")
    out.append(f"**Override chain** ({len(chain)}, most derived first):")
    for i, d in enumerate(chain[:MAX_CHAIN], 1):
        out.append(f"{i}. {_chain_entry(model, d, last=i == len(chain))}")
    if len(chain) > MAX_CHAIN:
        out.append(f"… +{len(chain) - MAX_CHAIN} more (root: **{_mod(chain[-1].module)}** — {_at(chain[-1].path, chain[-1].line)})")
    outside = _outside_overrides(registry, model, name, scope, K_METHODS, "method") if visible else []
    full = registry.method(model, name, None) if outside else []
    if len(full) > len(chain):
        # what runs in a database where every module overriding it is installed
        out.append(f"**Call order with every module installed** ({len(full)}, load order reversed; each `super()` continues on the next line):")
        for i, d in enumerate(full[:MAX_FULL_CHAIN], 1):
            entry = _chain_entry(model, d, last=i == len(full))
            if d.module and scope is not None and d.module not in scope:
                entry += " *(not a dependency)*"
            out.append(f"{i}. {entry}")
        if len(full) > MAX_FULL_CHAIN:
            out.append(f"… +{len(full) - MAX_FULL_CHAIN} more (goToImplementation lists every override)")
    elif outside:
        out.append(f"+{len(outside)} override(s) outside this module's dependencies (only when installed): {_more(_modules_of(outside), MAX_LIST)}")
    return "\n".join(out)


def _decorator(dec: tuple) -> str:
    name, args = dec[0], dec[1]
    if not args:
        return name
    return f"{name}({', '.join('…' if a == '?' else repr(a) for a in args)})"


def _chain_entry(model: str, d: MemberDef, last: bool) -> str:
    s = f"{_owner(d, model)} — {_at(d.path, d.line)}"
    if d.facts[M_SUPER]:
        s += " · super()"
    elif not last:
        s += " · ⚠ no super(): the definitions below are skipped"
    return s


# ---------------------------------------------------------------------------
# class attributes


def attr_card(ctx: Any, model: str | None, name: str) -> str | None:
    registry, scope = ctx.registry, ctx.scope
    if not model:
        return None
    defs, visible = _visible(registry.attr, model, name, scope)
    if not defs:
        return None
    top = defs[0]
    out = [f"**{model}.{name}** = `{_clip(top.facts[A_VALUE], 160)}`" + (f" ({top.facts[A_KIND]})" if top.facts[A_KIND] != "attr" else "")]
    if not visible:
        out.append(_not_visible(_modules_of(defs)))
    out.append("Set in (most derived first):")
    for d in defs[:MAX_OVERRIDES]:
        out.append(f"- {_owner(d, model)} — {_at(d.path, d.line)} `{_clip(d.facts[A_VALUE], 80)}`")
    if len(defs) > MAX_OVERRIDES:
        out.append(f"- +{len(defs) - MAX_OVERRIDES} more")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# models


def model_card(ctx: Any, model: str) -> str | None:
    registry, scope = ctx.registry, ctx.scope
    every = registry.model_classes(model, None)
    if not every:
        return None
    classes = registry.model_classes(model, scope) if scope is not None else every
    use_scope = scope if classes else None
    defs = [c for c in every if c.is_definition]
    origin = defs[0] if defs else every[0]
    desc = _own_model_attr(registry, model, "_description", use_scope)
    out = [f"**{model}**" + (f" — {_clip(desc, 120)}" if isinstance(desc, str) else "")]
    out.append(" · ".join(_model_facts(registry, model, use_scope)))
    if not classes:
        out.append(_not_visible(_modules_of(every)))
    elif scope is not None and not any(c.is_definition for c in classes):
        out.append(f"⚠ the module defining it (**{_mod(origin.module)}**) is not a dependency of this module")
    for c in defs[:3] or [origin]:
        out.append(f"Defined in **{_mod(c.module)}** — {_at(c.path, c.line)}")
    parents = registry.model_parents(model, use_scope)
    if parents:
        out.append(f"Inherits: {_more(parents, MAX_LIST)}")
    inherits = registry.model_inherits(model, use_scope)
    if inherits:
        out.append("Delegates (`_inherits`): " + ", ".join(f"{p} via `{f}`" for p, f in inherits.items()))
    ext = [c for c in (classes or every) if not c.is_definition]
    ext_modules = list(dict.fromkeys(_mod(c.module) for c in ext))
    if ext_modules:
        out.append(f"Extended in {len(ext_modules)} module(s): {_more(ext_modules, MAX_LIST)}")
    if classes:
        hidden = list(dict.fromkeys(_mod(c.module) for c in every if c not in classes and not c.is_definition))
        if hidden:
            out.append(f"+{len(hidden)} extending module(s) outside this module's dependencies: {_more(hidden, 6)}")
    fields = registry.fields(model, use_scope)
    methods = [n for n, ds in registry.methods(model, use_scope).items() if any(d.cls.model and d.cls.model != "base" for d in ds)]
    out.append(f"{len(fields)} fields · {len(methods)} model methods (excluding base/ORM)" + (" visible from this module" if use_scope is not None else ""))
    children = registry.model_children(model)
    if children:
        out.append(f"Inherited by {len(children)} model(s): {_more(children, 10)}")
    views = _views_line(registry, model)
    if views:
        out.append(views)
    out.extend(_access_lines(ctx, model))
    return "\n".join(out)


def _views_line(registry: Any, model: str) -> str | None:
    """Primary views of ``model`` (load order; form, list and search views first
    within a module, guessed from the xml id) and the number of extension views."""
    views = registry.views_of_model(model)
    if not views:
        return None
    primary = [v for v in views if not v.info.get("inherit_id") or v.info.get("mode") == "primary"]
    primary.sort(key=lambda v: (module_rank(registry, v.module), _view_kind_rank(v.xmlid), v.path, v.line))
    return f"Views: {len(primary)} primary ({_more([v.xmlid for v in primary], 6)}) · {len(views) - len(primary)} extension(s)"


def _view_kind_rank(xmlid: str) -> int:
    for rank, words in enumerate((("form",), ("tree", "list"), ("search", "filter"))):
        if any(w in xmlid for w in words):
            return rank
    return 3


def _own_model_attr(registry: Any, model: str, attr: str, scope: frozenset[str] | None) -> Any:
    """Effective value of a literal model attribute, ignoring framework defaults."""
    for c in registry.model_mro(model, scope):
        if not c.model:
            break
        attrs = c.model_info.get(MI_ATTRS) or {}
        if attr in attrs:
            return attrs[attr]
    return None


def _model_facts(registry: Any, model: str, scope: frozenset[str] | None) -> list[str]:
    bits = [registry.model_kind(model) or "Model"]
    for attr in ("_rec_name", "_order", "_table", "_parent_name"):
        v = _own_model_attr(registry, model, attr, scope)
        if v not in (None, ""):
            bits.append(f"{attr} `{_clip(v, 80)}`")
    for attr, label in (("_auto", "_auto"), ("_log_access", "_log_access"), ("_parent_store", "_parent_store"), ("_check_company_auto", "_check_company_auto")):
        v = _own_model_attr(registry, model, attr, scope)
        if v is not None and not (attr == "_auto" and v is True):
            bits.append(f"{label}={v}")
    return bits


def _access_lines(ctx: Any, model: str) -> list[str]:
    """Summary of the access rights rows (CSV ACL / ir.access) and record rules on ``model``."""
    registry = ctx.registry
    local = "model_" + model.replace(".", "_")
    hits = []
    for m in dict.fromkeys(c.module for c in registry.model_classes(model) if c.module):
        hits.extend(registry.refs_to_xmlid(f"{m}.{local}"))
    hits.extend(h for h in registry.refs_by_name(REF_MODEL, model) if h.ctx and h.ctx[0] == "csv")
    rows: list[tuple] = []
    rules: list[tuple] = []
    seen: set[tuple] = set()
    scope = ctx.scope
    for h in hits:
        if (h.path, h.line) in seen:
            continue
        seen.add((h.path, h.line))
        # rows of the file's dependency closure first; others flagged (installed only maybe)
        outside = scope is not None and h.module not in scope
        key = (outside, registry.module(h.module).rank if registry.module(h.module) else 0, h.path, h.line)
        mark = " *(not a dependency)*" if outside else ""
        if h.path.endswith(".csv"):
            row = _csv_row(ctx, h.path, h.line)
            if row is not None:
                rows.append((key, _acl_text(registry, row, h.module) + mark))
        elif h.path.endswith(".xml"):
            rec = _record_around(registry, h.path, h.line)
            if rec is not None and rec[X_MODEL] in ("ir.rule", "ir.access"):
                rules.append((key, f"`{rec[X_ID]}` ({_mod(h.module)}){mark}"))
    rows = [r for _k, r in sorted(rows)]  # type: ignore[assignment]
    rules = [r for _k, r in sorted(rules)]  # type: ignore[assignment]
    out = []
    if rows:
        out.append("Access rights (c=create r=read u=write d=unlink):")
        out.extend(f"- {r}" for r in rows[:6])
        if len(rows) > 6:
            out.append(f"- +{len(rows) - 6} more")
    if rules:
        out.append(f"Record rules ({len(rules)}): {_more(rules, 6)}")
    return out


def _csv_row(ctx: Any, path: str, line: int) -> dict[str, str] | None:
    lines = ctx.server.documents.lines(path)
    if not lines or not 0 < line < len(lines):
        return None
    try:
        header = next(csv.reader([lines[0].lstrip("\ufeff")]))
        values = next(csv.reader([lines[line]]))
    except (csv.Error, StopIteration):
        return None
    return {h.strip(): v.strip() for h, v in zip(header, values)}


def _acl_text(registry: Any, row: dict[str, str], module: str | None) -> str:
    group = row.get("group_id:id") or row.get("group_id/id") or row.get("group_id") or ""
    group = registry.qualify_xmlid(group, module) if group else ""
    if "operation" in row:
        perms = row.get("operation") or "-"
        kind = "restriction" if not group else "permission"
        extra = f" domain `{_clip(row['domain'], 80)}`" if row.get("domain") else ""
        return f"`{row.get('id', '?')}` {group or 'all users'}: {perms} [{kind}]{extra} ({_mod(module)})"
    perms = "".join(letter for letter, col in (("c", "perm_create"), ("r", "perm_read"), ("u", "perm_write"), ("d", "perm_unlink")) if row.get(col) in ("1", "True", "true"))
    return f"`{row.get('id', '?')}` {group or 'all users'}: {perms or '-'} ({_mod(module)})"


def _record_around(registry: Any, path: str, line: int) -> tuple | None:
    facts = registry.file_facts(path) or {}
    for rec in facts.get(RECORDS) or ():
        if rec[X_LINE] <= line <= rec[X_END_LINE]:
            return rec
    return None


# ---------------------------------------------------------------------------
# records, templates


def xmlid_card(ctx: Any, xmlid: str) -> str | None:
    registry = ctx.registry
    recs = registry.records(xmlid)
    if not recs:
        return None
    rec = recs[0]
    if rec.tag == "implicit":
        return _implicit_card(ctx, rec)
    info = rec.info
    out = [f"**{xmlid}** · {rec.model or rec.tag}" + (f" ({rec.tag})" if rec.tag not in ("record", "csv") else "")]
    if info.get("name"):
        out.append(f'"{_clip(info["name"], 120)}"')
    out.append(f"Defined in **{_mod(rec.module)}** — {_at(rec.path, rec.line)}" + (" · noupdate" if info.get("noupdate") else ""))
    if rec.model == "ir.ui.view" or rec.tag == "template":
        out.extend(_view_lines(ctx, rec))
    elif rec.model == "ir.ui.menu":
        for key in ("parent", "action", "groups", "sequence"):
            if info.get(key):
                out.append(f"{key}: `{info[key]}`")
    elif info.get("res_model"):
        out.append(f"Model: **{info['res_model']}**")
    elif info.get("model_id") and rec.tag != "csv":  # server action, cron, record rule
        model = _model_of_xmlid(registry, info["model_id"], rec.module)
        if model:
            out.append(f"Model: **{model}**")
    if info.get("binding_model_id") and rec.tag != "csv":
        model = _model_of_xmlid(registry, info["binding_model_id"], rec.module)
        if model:
            menu = "Print" if rec.model == "ir.actions.report" else "Action"
            out.append(f"Listed in the {menu} menu of **{model}**")
    if rec.tag == "csv":
        row = _csv_row(ctx, rec.path, rec.line) if rec.model in ("ir.model.access", "ir.access") else None
        if row is not None:
            out.append("Access (c=create r=read u=write d=unlink): " + _acl_text(registry, row, rec.module))
        out.extend(f"{key}: `{_clip(v, 80)}`" for key, v in info.items() if key not in ("name", "res_model") and not (row and key == "group_id"))
    if len(recs) > 1:
        out.append("Also defined/updated in: " + _more([f"**{_mod(r.module)}** ({_at(r.path, r.line)})" for r in recs[1:]], 4, "; "))
    n = len(registry.refs_to_xmlid(xmlid))
    if n:
        out.append(f"Referenced in {n} place(s) (findReferences lists them)")
    return "\n".join(out)


def _model_of_xmlid(registry: Any, xid: str, module: str | None) -> str | None:
    """'sale.model_sale_order' -> 'sale.order' (implicit ir.model xml ids)."""
    for r in registry.records(registry.qualify_xmlid(xid, module)):
        if r.tag == "implicit" and r.model == "ir.model":
            return r.info.get("model")
    return None


def _view_lines(ctx: Any, rec: RecordRef) -> list[str]:
    registry = ctx.registry
    info = rec.info
    parent = info.get("inherit_id")
    mode = info.get("mode") or ("extension" if parent else "primary")
    bits = []
    vm = registry.view_model(rec)
    if vm:
        bits.append(f"view of **{vm}**")
    vtype = _view_type(ctx, rec, 0)
    if vtype:
        bits.append(vtype)
    bits.append(mode)
    bits.append(f"priority {info.get('priority', 16)}")
    out = [" · ".join(bits)]
    if parent:
        full = registry.qualify_xmlid(parent, rec.module)
        prec = registry.records(full)
        out.append(f"Inherits **{full}**" + (f" — {_at(prec[0].path, prec[0].line)}" if prec else " (not found)"))
    kids = view_implementations(registry, rec.xmlid)
    kids = [k for k in kids if k.xmlid != rec.xmlid]
    if kids:
        out.append(f"Inherited by {len(kids)} view(s) (recursively):")
        out.extend(f"- {k.xmlid} ({_mod(k.module)}) — {_at(k.path, k.line)}" for k in kids[:6])
        if len(kids) > 6:
            out.append(f"- +{len(kids) - 6} more (goToImplementation lists them)")
    return out


def _view_type(ctx: Any, rec: RecordRef, depth: int) -> str | None:
    """View type: the ``type`` field (``qweb`` for templates), else the arch
    root tag, or the parent's type for extension views."""
    info = rec.info
    vtype = info.get("view_type") or info.get("type")
    if vtype:
        return vtype
    lines = ctx.server.documents.lines(rec.path) or []
    end = min(len(lines), (rec.facts[X_END_LINE] or rec.line) + 1)
    for i in range(rec.line, end):
        m = ARCH_RE.search(lines[i])
        if not m:
            continue
        tail = lines[i][m.end() :] + "\n" + "\n".join(lines[i + 1 : min(end, i + 6)])
        for tm in VIEW_TYPE_RE.finditer(tail.split(">", 1)[1] if ">" in tail else ""):
            tag = tm.group(1)
            if tag not in ("xpath", "field", "data") and not info.get("inherit_id"):
                return tag
            break
        break
    parent = info.get("inherit_id")
    if parent and depth < 10:
        for p in ctx.registry.records(ctx.registry.qualify_xmlid(parent, rec.module))[:1]:
            return _view_type(ctx, p, depth + 1)
    return None


def _implicit_card(ctx: Any, rec: RecordRef) -> str:
    info = rec.info
    model = info.get("model", "?")
    if "field" in info:
        out = [f"**{rec.xmlid}** · ir.model.fields (implicit xml id)",
               f"Created by Odoo for field **{model}.{info['field']}** of module **{_mod(rec.module)}** (`field_<model>__<field>`)",
               f"Field defined at {_at(rec.path, rec.line)}"]
    else:
        out = [f"**{rec.xmlid}** · ir.model (implicit xml id)",
               f"Created by Odoo for model **{model}** in every module defining or extending it (`model_<model>`); here: **{_mod(rec.module)}**",
               f"Class at {_at(rec.path, rec.line)}"]
    n = len(ctx.registry.refs_to_xmlid(rec.xmlid))
    if n:
        out.append(f"Referenced in {n} place(s)")
    return "\n".join(out)


def template_card(ctx: Any, name: str) -> str | None:
    registry = ctx.registry
    recs = registry.templates(name)
    if not recs:
        return xmlid_card(ctx, name)
    rec = recs[0]
    out = [f"**{name}** · client (owl/QWeb) template", f"Defined in **{_mod(rec.module)}** — {_at(rec.path, rec.line)}"]
    inherit = rec.info.get("inherit")
    if inherit:
        out.append(f"t-inherit (primary): **{inherit}**")
    kids = registry.children_views(name)
    exts = template_extensions(registry, name)
    if kids or exts:
        items = [f"- {k.xmlid} ({_mod(k.module)}, primary) — {_at(k.path, k.line)}" for k in kids]
        items += [f"- {_mod(h.module)} (extension) — {_at(h.path, h.line)}" for h in exts]
        out.append(f"Inherited by {len(items)} template(s):")
        out.extend(items[:8])
        if len(items) > 8:
            out.append(f"- +{len(items) - 8} more (goToImplementation lists them)")
    calls = [h for h in registry.refs_to_template(name) if not (h.ctx and h.ctx[0] == "tinherit")]
    if calls:
        out.append(f"Used in {len(calls)} place(s) (t-call / JS template)")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# modules, files, python symbols


def module_card(ctx: Any, name: str) -> str | None:
    registry = ctx.registry
    info = registry.module(name)
    if info is None:
        return f"**{name}** · ⚠ module not found in this environment's addons paths"
    v = info.values
    head = f"**{name}**"
    if v.get("name"):
        head += f" — {_clip(v['name'], 80)}"
    if v.get("version"):
        head += f" · version {v['version']}"
    out = [head]
    if v.get("summary"):
        out.append(f"*{_clip(v['summary'], 200)}*")
    bits = [f"{k}: {_clip(v[k], 60)}" for k in ("category", "license") if v.get(k)]
    for k in ("application", "auto_install"):
        if v.get(k):
            bits.append(f"{k}: {_clip(v[k], 80)}")
    if bits:
        out.append(" · ".join(bits))
    closure = registry.closure(name)
    out.append(f"Depends: {_more(list(info.depends), MAX_LIST) or '—'} ({len(closure) - 1} modules in the dependency closure)")
    out.append(f"Load order: depth {info.depth} (longest dependency path to base) — modules load by depth, then name (#{info.rank + 1} of the {len(registry.module_dirs)} in this environment)")
    missing = [d for d in info.depends if registry.module(d) is None]
    if missing:
        out.append(f"⚠ dependencies not found in this environment: {', '.join(missing)}")
    dependents = registry.dependents(name)
    direct = sorted(m for m in dependents if name in registry.module(m).depends)
    if dependents:
        out.append(f"Required by {len(dependents)} module(s) ({len(direct)} directly): {_more(direct, 10)}")
    out.append(f"Path: {info.path}")
    return "\n".join(out)


def file_card(ctx: Any, path: str) -> str | None:
    if not path:
        return None
    registry = ctx.registry
    module = registry.module_of_path(path)
    info = registry.module(module)
    rel = os.path.relpath(path, info.path) if info is not None else os.path.basename(path)
    out = [f"**{rel}**" + (f" · module **{module}**" if module else ""), path]
    if not os.path.exists(path):
        out.append("⚠ file does not exist")
        return "\n".join(out)
    facts = registry.file_facts(path) or {}
    if facts.get(KIND) in (KIND_XML, KIND_CSV):
        counts = Counter(rec[X_MODEL] or rec[X_TAG] for rec in facts.get(RECORDS) or ())
        if counts:
            out.append(f"{sum(counts.values())} record(s): " + _more([f"{m} ×{n}" for m, n in counts.most_common()], 8))
    return "\n".join(out)


def pysym_card(ctx: Any, target: Target) -> str | None:
    path = target.path
    if not path:
        return None
    lines = ctx.server.documents.lines(path) or []
    if not 0 <= target.line < len(lines):
        return None
    if path.endswith(".js"):
        return _js_symbol_card(ctx, target, lines)
    dotted = ctx.registry.py_dotted_name(path)
    where = f"{dotted + ' — ' if dotted else ''}{_at(path, target.line)}"
    if target.name and not re.match(rf"\s*(?:async\s+def|def|class)\s+{re.escape(target.name)}\b", lines[target.line]):
        # a variable, parameter or imported name: show the line binding it (and its inferred type)
        head = f"`{target.name}`"
        models = target.extra.get("value_models")
        if models:
            head += " → recordset of " + " | ".join(f"**{m}**" for m in models)
        elif target.extra.get("vals_model"):
            head += f" → field values of **{target.extra['vals_model']}**"
        return "\n".join([head, "```python", lines[target.line].strip(), "```", where])
    out = ["```python", _signature(lines, target.line), "```"]
    doc = _pysym_doc(ctx, path, target.line, lines)
    if doc:
        out.append(f"*{_clip(doc, 200)}*")
    out.append(where)
    return "\n".join(out)


def _js_symbol_card(ctx: Any, target: Target, lines: list[str]) -> str:
    """Card of a JS symbol: its declaration header (or the line exporting it),
    the first line of its JSDoc and its ``@module/path`` specifier."""
    path, line = target.path or "", target.line
    spec = _js_module(ctx.registry, path)
    where = f"{spec + ' — ' if spec else ''}{_at(path, line)}"
    m = JS_DECL_RE.match(lines[line])
    if m is None or (m.group(2) != target.name and target.name != "default"):
        label = f"`{target.name}`" if target.name != "default" else f"`{target.extra.get('local') or 'default'}` (default export)"
        return "\n".join([label, "```js", lines[line].strip(), "```", where])
    out = ["```js", _js_signature(lines, line, m.group(1)), "```"]
    doc = _jsdoc(lines, line)
    if doc:
        out.append(f"*{_clip(doc, 200)}*")
    out.append(where)
    return "\n".join(out)


def _js_signature(lines: list[str], line: int, keyword: str) -> str:
    """Declaration header at ``line``: a function up to its parameter list, a
    class up to its body, other declarations as their first line."""
    first = lines[line].rstrip()
    if keyword == "class":
        return textwrap.dedent(first.split("{", 1)[0].rstrip())
    if keyword != "function":
        return textwrap.dedent(first)
    text = "\n".join(l.rstrip() for l in lines[line : line + 4])
    start = text.find("(")
    depth = 0
    for i in range(max(start, 0), len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return textwrap.dedent(text[: i + 1])
    return textwrap.dedent(first)


def _jsdoc(lines: list[str], line: int) -> str | None:
    """First text line of the ``/** ... */`` block right above ``line``."""
    if line == 0 or not lines[line - 1].strip().endswith("*/"):
        return None
    for start in range(line - 1, max(-1, line - 40), -1):
        if "/*" in lines[start]:  # the comment's opening line
            if "/**" not in lines[start]:
                return None  # a plain block comment, not a JSDoc
            for text in lines[start:line]:
                text = text.strip().removeprefix("/**").removesuffix("*/").strip().lstrip("*").strip()
                if text and not text.startswith("@"):
                    return text
            return None
    return None


def _js_module(registry: Any, path: str) -> str | None:
    """``@module/path`` specifier of a file under ``static/src``."""
    module = registry.module_of_path(path)
    info = registry.module(module)
    if info is None:
        return None
    rel = os.path.relpath(path, info.path).replace(os.sep, "/")
    if not rel.startswith("static/src/"):
        return None
    return f"@{module}/{rel[len('static/src/') :].removesuffix('.js')}"


def _signature(lines: list[str], line: int) -> str:
    """The definition header starting at ``line`` (up to its colon, <= 4 lines)."""
    out = []
    for i in range(line, min(len(lines), line + 4)):
        text = lines[i].rstrip()
        out.append(text)
        if text.endswith(":") or not re.match(r"\s*(async\s+def|def|class)\b", lines[line]):
            break
    return textwrap.dedent("\n".join(out))


def _pysym_doc(ctx: Any, path: str, line: int, lines: list[str]) -> str | None:
    facts = (ctx.facts if path == ctx.path else None) or ctx.registry.file_facts(path) or {}
    for cls in facts.get(CLASSES) or ():
        if cls[K_LINE] == line:
            return cls[K_DOC]
        for m in cls[K_METHODS]:
            if m[M_LINE] == line:
                return m[M_DOC]
    # module-level function: first line of a docstring right after the header
    for i in range(line + 1, min(len(lines), line + 12)):
        text = lines[i].strip()
        if not text:
            continue
        m = re.match(r"""[rRuU]?(\"\"\"|''')\s*(.*?)\s*(?:\1)?$""", text)
        if m:
            return m.group(2) or (lines[i + 1].strip() if i + 1 < len(lines) else None)
        if text.endswith(":") and i < line + 4:
            continue
        break
    return None


# ---------------------------------------------------------------------------
# ambiguous targets


def _ambiguous_card(ctx: Any, target: Target) -> str | None:
    models = candidate_models(target)
    if not models:
        return None
    out = [f"**{target.name}** — receiver type not inferred; defined on {len(models)} candidate model(s):"]
    for model in models:
        line = _candidate_line(ctx, target.kind, model, target.name or "")
        if line:
            out.append(f"- {line}")
    return "\n".join(out) if len(out) > 1 else None


def _candidate_line(ctx: Any, kind: str, model: str, name: str) -> str | None:
    registry, scope = ctx.registry, ctx.scope
    if kind == FIELD:
        info = registry.field_info(model, name, scope) or registry.field_info(model, name, None)
        if not info:
            return None
        d = info["defs"][-1]
        typ = info["type"] + (f" → {info['comodel']}" if info["comodel"] else "")
        return f"**{model}**.{name} · {typ} — **{_mod(d.module)}** {_at(d.path, d.line)}"
    lookup = registry.method if kind == METHOD else registry.attr
    defs = lookup(model, name, scope) or lookup(model, name, None)
    if not defs:
        return None
    d = defs[-1]
    params = f"({_clip(defs[0].facts[M_PARAMS] or '', 60)})" if kind == METHOD else ""
    return f"**{model}**.{name}{params} — **{_mod(d.module)}** {_at(d.path, d.line)}"


def _one_liner(ctx: Any, target: Target) -> str | None:
    """Short description of an alternative target."""
    kind = target.kind
    if kind in (FIELD, METHOD, ATTR):
        if target.model:
            return _candidate_line(ctx, kind, target.model, target.name or "")
        return f"{kind} `{target.name}` on {', '.join(candidate_models(target))}"
    if kind == MODEL:
        return f"model **{target.model}**"
    if kind in (XMLID, TEMPLATE):
        return f"{kind} `{target.xmlid}`"
    if kind == MODULE:
        return f"module `{target.name}`"
    if kind in (FILE, PYSYM):
        return f"{kind} {target.path}:{target.line + 1}"
    return None
