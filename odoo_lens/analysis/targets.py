"""``Target``: a resolved symbol under the cursor - the contract between the
per-language analysis (which produces targets) and the features (which turn
targets into definitions, references, hovers, implementations...).

``targets_at(ctx)`` dispatches on the file kind of ``ctx`` (a
:class:`odoo_lens.server.RequestContext`) and returns the targets for the token at
``(ctx.line, ctx.col)`` - usually one; several when ambiguous (e.g. an attribute
whose receiver could not be inferred and that a few models define). The first
target is the best guess.

Analyses are lenient about the cursor position: Claude-supplied columns are often
off by a few characters, so when the exact position is not on a meaningful token
the nearest meaningful token on the same line is used (preferring Odoo-specific
tokens: model names, field names, xml ids).
"""

from __future__ import annotations

from typing import Any

# Target kinds
MODEL = "model"  # model:   model
FIELD = "field"  # field:   model, name        (model None + models=[...] when ambiguous)
METHOD = "method"  # method:  model, name      (model None + models=[...] when ambiguous)
ATTR = "attr"  # class attribute of a model:  model, name
XMLID = "xmlid"  # record:  xmlid (fully qualified)
TEMPLATE = "template"  # owl/QWeb client template: xmlid holds the t-name
MODULE = "module"  # module:  name
FILE = "file"  # a file:  path (e.g. manifest 'data' entry, JS import specifier)
PYSYM = "pysym"  # generic Python symbol already resolved to a location: path, line, col, end_col, name


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
        self.origin = origin  # (line, col, end_col) of the token under the cursor
        self.models = models  # candidate models when ``model`` could not be pinned down
        self.extra = extra or {}
        # Well-known ``extra`` keys:
        #   "super": True        -> super().name(...): definitions should start
        #                           *below* the current class in the MRO
        #   "role": str          -> how the token was used (e.g. "compute", "_inherit",
        #                           "env", "related", "button", "ref", "inherit_id"...)
        #   "receiver": str      -> textual receiver expression of an attribute
        #   "inferred": bool     -> model was inferred (True) or taken from a literal (False)
        #   "class_index": int   -> for targets inside a model class: index in CLASSES

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


def targets_at(ctx: Any) -> list[Target]:
    """Targets under the cursor of ``ctx`` (RequestContext), best first."""
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
