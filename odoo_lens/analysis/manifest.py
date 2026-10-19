"""``__manifest__.py`` analysis: targets under the cursor.

* ``depends`` entries: the module;
* ``data`` / ``demo`` (and legacy ``init_xml``/``update_xml``/``demo_xml``/``qweb``)
  entries: the file, relative to the module directory (also when missing, so
  that hover/diagnostics can say so);
* ``assets`` entries: ``module/static/...`` paths (a leading ``/`` is allowed)
  resolve against that module's directory; for globs, the deepest existing
  directory before the first wildcard. Bundle names (``('include', 'web.x')``)
  and directives are not files.
"""

from __future__ import annotations

import os
from typing import Any

from ..facts import MANIFEST_ASSETS, MANIFEST_DATA, MANIFEST_DEMO, MANIFEST_DEPENDS
from .targets import FILE, MODULE, Target
from .xml import nearest

_GLOB_CHARS = "*?["
_ASSET_EXTS = (".js", ".scss", ".css", ".sass", ".less", ".xml", ".woff", ".woff2")


def targets_at(ctx: Any) -> list[Target]:
    facts = ctx.facts
    if not facts:
        return []
    line = ctx.line
    cands = []
    for name, l0, c0, c1 in facts.get(MANIFEST_DEPENDS) or ():
        if l0 == line:
            cands.append((c0, c1, 0, ("depends", name, l0, c0, c1)))
    for key in (MANIFEST_DATA, MANIFEST_DEMO):
        for rel, l0, c0, c1 in facts.get(key) or ():
            if l0 == line:
                cands.append((c0, c1, 1, (key, rel, l0, c0, c1)))
    for entries in (facts.get(MANIFEST_ASSETS) or {}).values():
        for value, l0, c0, c1 in entries:
            if l0 == line and is_asset_path(value):
                cands.append((c0, c1, 1, ("assets", value, l0, c0, c1)))
    hit = nearest(cands, ctx.col)
    if hit is None:
        return []
    role, value, l0, c0, c1 = hit
    origin = (l0, c0, c1)
    if role == "depends":
        return [Target(MODULE, name=value, origin=origin, extra={"role": role})]
    if role == "assets":
        path = asset_path(ctx.registry, value)
        return [Target(FILE, path=path, origin=origin, extra={"role": role, "raw": value, "exists": os.path.exists(path)})] if path else []
    path = os.path.normpath(os.path.join(os.path.dirname(ctx.path), value))
    return [Target(FILE, path=path, origin=origin, extra={"role": role, "raw": value, "exists": os.path.isfile(path)})]


def is_asset_path(value: str) -> bool:
    """A file path or glob (not a bundle name or a directive) of an ``assets`` list."""
    return ("/" in value or value.endswith(_ASSET_EXTS)) and "://" not in value


def asset_path(registry: Any, value: str) -> str | None:
    """File (or, for a glob, deepest existing directory) of an asset path
    ``module/static/...``; None when the module is not part of the env."""
    module, _, rest = value.lstrip("/").partition("/")
    info = registry.module(module)
    if info is None or not rest:
        return None
    segs = rest.split("/")
    fixed = []
    for seg in segs:
        if any(ch in seg for ch in _GLOB_CHARS):
            break
        fixed.append(seg)
    path = os.path.normpath(os.path.join(info.path, *fixed))
    if len(fixed) == len(segs):
        return path
    while not os.path.isdir(path) and len(path) > len(info.path):
        path = os.path.dirname(path)
    return path


__all__ = ["targets_at", "asset_path", "is_asset_path"]
