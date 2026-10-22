from __future__ import annotations

from typing import Any

# Import eagerly, before the defs: a lazy submodule import would rebind hover/references/diagnostics.
from . import calls as _calls
from . import diagnostics as _diagnostics
from . import hover as _hover
from . import navigation as _navigation
from . import references as _references
from . import symbols as _symbols


def definition(ctx: Any) -> list[dict]:
    return _navigation.definition(ctx)


def type_definition(ctx: Any) -> list[dict]:
    return _navigation.type_definition(ctx)


def implementation(ctx: Any) -> list[dict]:
    return _navigation.implementation(ctx)


def references(ctx: Any, include_declaration: bool = True) -> list[dict]:
    return _references.references(ctx, include_declaration)


def hover(ctx: Any) -> dict | None:
    return _hover.hover(ctx)


def document_symbols(ctx: Any, hierarchical: bool = True) -> list[dict]:
    return _symbols.document_symbols(ctx, hierarchical)


def workspace_symbols(server: Any, env: Any, registry: Any, query: str, token: Any) -> list[dict]:
    return _symbols.workspace_symbols(server, env, registry, query, token)


def prepare_call_hierarchy(ctx: Any) -> list[dict]:
    return _calls.prepare(ctx)


def incoming_calls(ctx: Any, item: dict) -> list[dict]:
    return _calls.incoming(ctx, item)


def outgoing_calls(ctx: Any, item: dict) -> list[dict]:
    return _calls.outgoing(ctx, item)


def diagnostics(ctx: Any) -> list[dict]:
    return _diagnostics.diagnostics(ctx)
