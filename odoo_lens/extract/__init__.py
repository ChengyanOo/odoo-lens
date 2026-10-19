"""Facts extractors: pure ``extract(path, source: bytes) -> dict`` functions.

They only depend on the standard library and :mod:`odoo_lens.facts`, never raise,
and run inside worker processes during parallel indexing.
"""

from __future__ import annotations

from ..facts import ERROR, KIND, KIND_CSV, KIND_JS, KIND_MANIFEST, KIND_PY, KIND_XML


def kind_for_path(name: str, rel: str) -> str | None:
    """Facts kind for a file ``name`` at module-relative path ``rel`` (None = skip)."""
    if name.endswith(".py"):
        return KIND_MANIFEST if name == "__manifest__.py" else KIND_PY
    if name.endswith(".xml"):
        return KIND_XML
    if name.endswith(".csv"):
        return KIND_CSV
    if name.endswith(".js"):
        rel = rel.replace("\\", "/")
        return KIND_JS if rel.startswith("static/src/") else None
    return None


def extract_source(path: str, kind: str, src: bytes, index: bool = False) -> dict:
    """Facts of one file. ``index=True`` (background indexing) leaves out
    high-volume facts that are only needed for the file being edited (XML
    record-level ``<field name>`` refs, refs of big data CSVs); queries that need
    them re-extract the few candidate files."""
    try:
        if kind == KIND_PY:
            from .pyfacts import extract
        elif kind == KIND_XML:
            from .xmlfacts import extract as xml_extract

            return xml_extract(path, src, index)
        elif kind == KIND_MANIFEST:
            from .manifestfacts import extract
        elif kind == KIND_CSV:
            from .csvfacts import extract as csv_extract

            return csv_extract(path, src, index)
        elif kind == KIND_JS:
            from .jsfacts import extract
        else:
            return {KIND: kind, ERROR: ("unsupported file kind", 0, 0)}
        return extract(path, src)
    except Exception as exc:  # extractors must never take the indexer down
        return {KIND: kind, ERROR: (f"internal extractor error: {type(exc).__name__}: {exc}", 0, 0)}
