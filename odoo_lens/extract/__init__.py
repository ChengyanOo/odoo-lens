# Also run in spawned pool workers: import only the stdlib, ..facts and sibling extractors.

from __future__ import annotations

from ..facts import ERROR, KIND, KIND_CSV, KIND_JS, KIND_MANIFEST, KIND_PY, KIND_XML


def kind_for_path(name: str, rel: str) -> str | None:
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
    # index=True (background indexing) drops bulky facts: XML record-level <field name> refs,
    # big data-CSV row refs, l10n /data/template/ CSVs; open/candidate files get a full re-extract.
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
    except Exception as exc:  # never take the indexer down
        return {KIND: kind, ERROR: (f"internal extractor error: {type(exc).__name__}: {exc}", 0, 0)}
