"""``__manifest__.py`` extractor: literal values plus positions of the entries
that reference other things (dependencies, data files, assets)."""

from __future__ import annotations

import ast
import warnings

from ..facts import (
    ERROR,
    KIND,
    KIND_MANIFEST,
    MANIFEST_ASSETS,
    MANIFEST_DATA,
    MANIFEST_DEMO,
    MANIFEST_DEPENDS,
    MANIFEST_KEYS,
    MANIFEST_VALUES,
    REF_FILE,
    REF_MODULE,
    REFS,
)
from .common import SrcIndex


def _pos(s: SrcIndex, node: ast.Constant) -> tuple[int, int, int] | None:
    """Content range of a single-line string literal."""
    if node.lineno != node.end_lineno:
        return None
    l0 = node.lineno - 1
    raw = s.lines[l0][node.col_offset : node.end_col_offset]
    q = 0
    while q < len(raw) and raw[q : q + 1] not in (b"'", b'"'):
        q += 1
    quote = raw[q : q + 3] if raw[q : q + 3] in (b"'''", b'"""') else raw[q : q + 1]
    if not quote or not raw.endswith(quote):
        return None
    return l0, s.col(l0, node.col_offset + q + len(quote)), s.col(l0, node.end_col_offset - len(quote))


def _strings(s: SrcIndex, node: ast.AST) -> list[tuple]:
    out = []
    for e in getattr(node, "elts", []):
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            p = _pos(s, e)
            if p is not None:
                out.append((e.value, p[0], p[1], p[2]))
        elif isinstance(e, (ast.Tuple, ast.List)):  # assets directives: ('include', 'web._assets_x')
            for sub in e.elts:
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    p = _pos(s, sub)
                    if p is not None:
                        out.append((sub.value, p[0], p[1], p[2]))
    return out


def extract(path: str, src: bytes) -> dict:
    facts: dict = {
        KIND: KIND_MANIFEST,
        ERROR: None,
        MANIFEST_VALUES: {},
        MANIFEST_DEPENDS: [],
        MANIFEST_DATA: [],
        MANIFEST_DEMO: [],
        MANIFEST_ASSETS: {},
        MANIFEST_KEYS: {},
        REFS: [],
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        facts[ERROR] = (exc.msg or "invalid syntax", max(0, (exc.lineno or 1) - 1), max(0, (exc.offset or 1) - 1))
        return facts
    except Exception as exc:
        facts[ERROR] = (str(exc), 0, 0)
        return facts
    node = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
            node = stmt.value
            break
    if node is None:
        facts[ERROR] = ("manifest is not a dict literal", 0, 0)
        return facts
    s = SrcIndex(src)
    values: dict = {}
    module = None
    parts = path.replace("\\", "/").split("/")
    if len(parts) >= 2:
        module = parts[-2]
    for k, v in zip(node.keys, node.values):
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
            continue
        key = k.value
        p = _pos(s, k)
        if p is not None:
            facts[MANIFEST_KEYS][key] = p
        if key == "depends":
            facts[MANIFEST_DEPENDS] = _strings(s, v)
            for name, l0, c0, c1 in facts[MANIFEST_DEPENDS]:
                facts[REFS].append((REF_MODULE, name, l0, c0, c1, ("depends",)))
            values[key] = [d[0] for d in facts[MANIFEST_DEPENDS]]
            continue
        if key in ("data", "demo", "init_xml", "update_xml", "demo_xml", "qweb"):
            entries = _strings(s, v)
            target = MANIFEST_DEMO if key in ("demo", "demo_xml") else MANIFEST_DATA
            facts[target] = facts[target] + entries
            for rel, l0, c0, c1 in entries:
                facts[REFS].append((REF_FILE, rel, l0, c0, c1, (key, module)))
            continue
        if key == "assets" and isinstance(v, ast.Dict):
            assets = {}
            for bk, bv in zip(v.keys, v.values):
                if isinstance(bk, ast.Constant) and isinstance(bk.value, str):
                    entries = _strings(s, bv)
                    assets[bk.value] = entries
                    for rel, l0, c0, c1 in entries:
                        if "/" in rel or rel.endswith((".js", ".scss", ".css", ".xml")):
                            facts[REFS].append((REF_FILE, rel, l0, c0, c1, ("assets", module)))
            facts[MANIFEST_ASSETS] = assets
            continue
        try:
            values[key] = ast.literal_eval(v)
        except Exception:
            pass
    facts[MANIFEST_VALUES] = values
    return facts
