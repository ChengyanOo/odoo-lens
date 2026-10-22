from __future__ import annotations

from typing import Any

# Kinds: FIELD/METHOD/ATTR (class attribute): model + name (ambiguous: model None, models=[...]);
# XMLID: fully qualified xmlid; TEMPLATE: xmlid = owl/QWeb t-name; MODULE: name; FILE: path;
# PYSYM: resolved location (path, line, col, end_col, name), a JS symbol when path ends in .js.
MODEL = "model"
FIELD = "field"
METHOD = "method"
ATTR = "attr"
XMLID = "xmlid"
TEMPLATE = "template"
MODULE = "module"
FILE = "file"
PYSYM = "pysym"


class Target:
    __slots__ = ("kind", "model", "name", "xmlid", "path", "line", "col", "end_col", "origin", "models", "extra")

    def __init__(
        self,
        kind: str,
        model: str | None = None,
        name: str | None = None,
        xmlid: str | None = None,
        path: str | None = None,
        line: int = 0,
        col: int = 0,
        end_col: int = 0,
        origin: tuple[int, int, int] | None = None,
        models: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self.kind = kind
        self.model = model
        self.name = name
        self.xmlid = xmlid
        self.path = path
        self.line = line
        self.col = col
        self.end_col = end_col
        self.origin = origin  # the token under the cursor
        self.models = models
        self.extra = extra or {}
        # extra: "role" (token usage); "super": super().x, definitions start below this class in MRO;
        # "guess" ("member-name"|"duck-typing"): model guessed, receiver/view model unknown: hover
        # must flag it; "inferred": True = receiver type inferred, False = literal, heir ("via") or guess.

    def __repr__(self) -> str:
        bits = [self.kind]
        for k in ("model", "name", "xmlid", "path"):
            v = getattr(self, k)
            if v:
                bits.append(f"{k}={v!r}")
        if self.models:
            bits.append(f"models={self.models!r}")
        return f"Target({', '.join(bits)})"

    def key(self) -> tuple:
        return (self.kind, self.model, self.name, self.xmlid, self.path, self.line, self.col)


# Best first; several when ambiguous.
def targets_at(ctx: Any) -> list[Target]:
    kind = ctx.kind
    if kind == "py":
        from .python import targets_at as impl
    elif kind == "xml":
        from .xml import targets_at as impl
    elif kind == "csv":
        from .csv import targets_at as impl
    elif kind == "manifest":
        from .manifest import targets_at as impl
    elif kind == "js":
        from .js import targets_at as impl
    else:
        return []
    return impl(ctx)
