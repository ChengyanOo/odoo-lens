"""JS facts (static/src/**/*.js): module imports, top-level symbols, and the
Odoo-specific strings worth navigating from: owl ``template`` names, models and
methods called through the ORM service (``orm.call("res.partner", "x")``)."""

from __future__ import annotations

import re

from ..facts import ERROR, IMPORTS, KIND, KIND_JS, REF_METHOD, REF_MODEL, REF_QWEB, REFS, SYMBOLS
from .common import SrcIndex

IMPORT_RE = re.compile(rb"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\(\s*)(["'])(@[\w\-]+/[^"'\s]*|\.{1,2}/[^"'\s]*)\1""")
SYMBOL_RE = re.compile(rb"""^(?:export\s+(?:default\s+)?)?(?:(class)\s+([A-Za-z_$][\w$]*)|(?:async\s+)?(function)\s*\*?\s*([A-Za-z_$][\w$]*)|(const|let|var)\s+([A-Za-z_$][\w$]*)\s*=)""", re.M)
TEMPLATE_RE = re.compile(rb"""\btemplate\s*[:=]\s*(["'`])([\w.\-]+)\1""")
ORM_MODEL_RE = re.compile(
    rb"""\borm\.(?:silent\.)?(?:call|read|searchRead|search|searchCount|create|write|unlink|webSearchRead|webRead|readGroup|webReadGroup|nameGet|formattedReadGroup)\(\s*(["'])([a-z0-9_]+(?:\.[a-z0-9_]+)+)\1(?:\s*,\s*(["'])([A-Za-z_]\w*)\3)?"""
)
RESMODEL_RE = re.compile(rb"""\b(?:resModel|res_model)\s*:\s*(["'])([a-z0-9_]+(?:\.[a-z0-9_]+)+)\1""")


def extract(path: str, src: bytes) -> dict:
    facts: dict = {KIND: KIND_JS, ERROR: None, IMPORTS: [], SYMBOLS: [], REFS: []}
    s = SrcIndex(src)
    for m in IMPORT_RE.finditer(src):
        line, c0, c1 = s.span(m.start(2), m.end(2))
        facts[IMPORTS].append((m.group(2).decode("utf-8", "replace"), line, c0, c1))
    for m in SYMBOL_RE.finditer(src):
        if m.group(2):
            kind, g = "class", 2
        elif m.group(4):
            kind, g = "def", 4
        else:
            kind, g = "var", 6
        line, c0, c1 = s.span(m.start(g), m.end(g))
        facts[SYMBOLS].append((m.group(g).decode(), kind, line, c0, c1, line))
    refs = facts[REFS]
    if b"template" in src:
        for m in TEMPLATE_RE.finditer(src):
            if b"." in m.group(2):
                line, c0, c1 = s.span(m.start(2), m.end(2))
                refs.append((REF_QWEB, m.group(2).decode(), line, c0, c1, ("template",)))
    if b"orm." in src:
        for m in ORM_MODEL_RE.finditer(src):
            model = m.group(2).decode()
            line, c0, c1 = s.span(m.start(2), m.end(2))
            refs.append((REF_MODEL, model, line, c0, c1, ("js",)))
            if m.group(4):
                line, c0, c1 = s.span(m.start(4), m.end(4))
                refs.append((REF_METHOD, m.group(4).decode(), line, c0, c1, ("js", model)))
    if b"esModel" in src or b"res_model" in src:
        for m in RESMODEL_RE.finditer(src):
            line, c0, c1 = s.span(m.start(2), m.end(2))
            refs.append((REF_MODEL, m.group(2).decode(), line, c0, c1, ("js",)))
    return facts
