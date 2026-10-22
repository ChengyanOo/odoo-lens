from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from . import __version__


class NullEndpoint:
    _stopped = False

    def notify(self, method: str, params: Any = None) -> None:
        pass

    def request(self, method: str, params: Any = None) -> Any:
        from concurrent.futures import Future

        fut: Future = Future()
        fut.set_result(None)
        return fut

    def post(self, fn: Any, *args: Any) -> None:
        fn(*args)

    def post_later(self, delay: float, key: Any, fn: Any, *args: Any) -> None:
        pass

    def on_request(self, method: str, handler: Any) -> None:
        pass

    def on_notification(self, method: str, handler: Any) -> None:
        pass

    def stop(self) -> None:
        pass


def headless_server():
    from .server import OdooLanguageServer

    server = OdooLanguageServer(NullEndpoint())  # type: ignore[arg-type]
    server.initialized = True
    return server


def load_env(server: Any, path: str, workers: int | None = None, use_cache: bool = True):
    if not use_cache:
        os.environ["ODOO_LENS_NO_CACHE"] = "1"
    path = os.path.abspath(path)
    env = server.envs.env_for_path(path)
    if env is None and os.path.isdir(path):
        envs = server.envs.envs_for_workspace(path)
        env = envs[0] if envs else None
    if env is None:
        return None, None
    return env, index_env(env, workers)


def index_env(env: Any, workers: int | None = None):
    from .indexer import build_index

    registry, stats = build_index(env, workers=workers, wait_for_cache_write=True)
    env.registry = registry
    env.ready.set()
    env.last_used = time.monotonic()
    return stats


def _ctx(server: Any, env: Any, path: str, line1: int, col1: int):
    from .documents import utf16_len
    from .server import RequestContext

    text = server.documents.text(path) or ""
    lines = text.split("\n")
    line0 = max(0, line1 - 1)
    # CLI columns are 1-based code points (like Claude's) -> 0-based UTF-16
    line_text = lines[line0] if line0 < len(lines) else ""
    col16 = utf16_len(line_text[: max(0, col1 - 1)])
    return RequestContext(server, env, env.registry, path, line0, col16)


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
        return
    from .documents import uri_to_path

    def loc(l: dict) -> str:
        uri = l.get("uri") or l.get("targetUri")
        rng = l.get("range") or l.get("targetSelectionRange") or {}
        s = rng.get("start", {})
        return f"{uri_to_path(uri)}:{s.get('line', 0) + 1}:{s.get('character', 0) + 1}"

    if obj is None:
        print("(no result)")
    elif isinstance(obj, list):
        if not obj:
            print("(no result)")
        for item in obj:
            if isinstance(item, dict) and ("uri" in item or "targetUri" in item) and "name" not in item:
                print(loc(item))
            elif isinstance(item, dict) and "location" in item:
                print(f"{item.get('name')}  [{item.get('containerName', '')}]  {loc(item['location'])}")
            elif isinstance(item, dict) and "name" in item:
                print(f"{item['name']}  {item.get('detail', '')}  {loc(item) if 'uri' in item else ''}")
            else:
                print(json.dumps(item, ensure_ascii=False))
    elif isinstance(obj, dict) and "contents" in obj:
        c = obj["contents"]
        print(c.get("value") if isinstance(c, dict) else c)
    else:
        print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_env(args: argparse.Namespace) -> int:
    server = headless_server()
    path = os.path.abspath(args.path)
    env = server.envs.env_for_path(path)
    if env is None and os.path.isdir(path):
        envs = server.envs.envs_for_workspace(path)
        env = envs[0] if envs else None
    if env is None:
        print("no Odoo environment detected")
        return 1
    print(env.label)
    print("version:  ", env.version.label if env.version else "?")
    print("community:", env.community)
    for r in env.addons_roots:
        print("addons:   ", r)
    if env.config_path:
        print("config:   ", env.config_path)
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    server = headless_server()
    env, stats = load_env(server, args.path, args.workers, not args.no_cache)
    if env is None:
        print("no Odoo environment detected", file=sys.stderr)
        return 1
    reg = env.registry
    print(env.label)
    print(stats.summary())
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(f"max RSS: {rss / (1 << 20) if sys.platform == 'darwin' else rss / 1024:.0f} MB")
    except Exception:
        pass
    try:
        print(f"models: {len(list(reg.model_names()))}  xml ids: {len(list(reg.xmlids()))}")
    except Exception:
        pass
    return 0


QUERY_OPS = ("definition", "references", "hover", "implementation", "typedef", "symbols", "calls-in", "calls-out", "targets")


def cmd_query(args: argparse.Namespace) -> int:
    from . import features

    server = headless_server()
    path = os.path.abspath(args.file)
    env, _ = load_env(server, path, args.workers)
    if env is None:
        print("no Odoo environment detected", file=sys.stderr)
        return 1
    ctx = _ctx(server, env, path, args.line, args.col)
    t = time.perf_counter()
    op = args.op
    if op == "definition":
        res = features.definition(ctx)
    elif op == "references":
        res = features.references(ctx, True)
    elif op == "hover":
        res = features.hover(ctx)
    elif op == "implementation":
        res = features.implementation(ctx)
    elif op == "typedef":
        res = features.type_definition(ctx)
    elif op == "symbols":
        res = features.document_symbols(ctx, True)
    elif op in ("calls-in", "calls-out"):
        from .documents import uri_to_path
        from .server import RequestContext

        calls = features.incoming_calls if op == "calls-in" else features.outgoing_calls
        res = []
        for item in features.prepare_call_hierarchy(ctx) or []:
            # context on the item, not the cursor (as server._item_context); selectionRange is 0-based UTF-16
            sel = (item.get("selectionRange") or item.get("range") or {}).get("start") or {}
            ictx = RequestContext(server, env, env.registry, uri_to_path(item["uri"]), sel.get("line", 0), sel.get("character", 0))
            res += calls(ictx, item) or []
    elif op == "targets":
        from .analysis import targets_at

        res = [repr(t) for t in targets_at(ctx)]
    else:
        print(f"unknown op {op}", file=sys.stderr)
        return 2
    dt = time.perf_counter() - t
    _print(res, args.json)
    print(f"({dt * 1000:.1f} ms)", file=sys.stderr)
    return 0


def cmd_symbols(args: argparse.Namespace) -> int:
    from . import features
    from .protocol import NEVER_CANCELLED

    server = headless_server()
    env, _ = load_env(server, args.path or os.getcwd(), args.workers)
    if env is None:
        print("no Odoo environment detected", file=sys.stderr)
        return 1
    t = time.perf_counter()
    res = features.workspace_symbols(server, env, env.registry, args.query, NEVER_CANCELLED)
    dt = time.perf_counter() - t
    # no default limit: the server already caps the answer and ends a cut one with a "not listed" marker
    shown = res if args.limit is None else res[: max(0, args.limit)]
    _print(shown, args.json)
    if len(shown) < len(res):
        print(f"({len(res) - len(shown)} more results not shown; raise --limit)", file=sys.stderr if args.json else sys.stdout)
    print(f"({len(res)} results, {dt * 1000:.1f} ms)", file=sys.stderr)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import gc

    from . import features
    from .server import RequestContext, _purge_caches

    server = headless_server()
    targets: list[str] = []
    for p in args.paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            for d, dirs, files in os.walk(p):
                dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "__pycache__", "static")]
                targets += [os.path.join(d, f) for f in files if f.endswith((".py", ".xml", ".csv"))]
        else:
            targets.append(p)
    if not targets:
        return 0
    groups: dict[str, tuple[Any, list[str]]] = {}
    unresolved: list[str] = []
    for path in sorted(set(targets)):
        env = server.envs.env_for_path(path)
        if env is None:
            unresolved.append(path)
        else:
            groups.setdefault(env.key, (env, []))[1].append(path)
    for path in unresolved:  # retry: an env discovered later (from a module file) may contain it
        env = server.envs.env_for_path(path)
        if env is None:
            print(f"skipped (no Odoo env): {path}", file=sys.stderr)
        else:
            groups.setdefault(env.key, (env, []))[1].append(path)
    if not groups:
        print("no Odoo environment detected", file=sys.stderr)
        return 1
    sev_names = {1: "error", 2: "warning", 3: "info", 4: "hint"}
    n_err = 0
    n_total = 0
    n_files = 0
    for env, paths in groups.values():
        index_env(env, args.workers)
        for path in sorted(paths):
            ctx = RequestContext(server, env, env.registry, path)
            if ctx.kind is None:
                continue
            n_files += 1
            for d in features.diagnostics(ctx):
                s = d["range"]["start"]
                sev = d.get("severity", 1)
                n_total += 1
                n_err += sev == 1
                if args.json:
                    print(json.dumps({"path": path, **d}, ensure_ascii=False))
                else:
                    code = f" [{d['code']}]" if d.get("code") else ""
                    print(f"{path}:{s['line'] + 1}:{s['character'] + 1}: {sev_names.get(sev, sev)}{code}: {d['message']}")
        if len(groups) > 1:
            # one registry at a time, released as in server._evict_envs; next build's gc.collect() frees it
            ctx = None
            env.registry = None
            env.ready.clear()
            _purge_caches()
            gc.unfreeze()
    print(f"{n_total} diagnostics ({n_err} errors) in {n_files} files", file=sys.stderr)
    return 1 if n_err else 0


MAX_HINT_MODULES = 300


def cmd_session_hint(args: argparse.Namespace) -> int:
    try:
        from .envs import find_community_root, guess_version, is_community_root, iter_module_dirs, read_release_version

        folder = os.path.abspath(args.path or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        launcher = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "odoo-lens")
        community = find_community_root(folder)
        if community:
            v = read_release_version(community)
            what = f"an Odoo {v.label if v else ''} checkout".replace("  ", " ")
        else:
            try:
                children = [e.path for e in os.scandir(folder) if e.is_dir() and not e.name.startswith(".")]
            except OSError:
                children = []
            checkouts = [c for c in children for cand in (c, os.path.join(c, "odoo")) if is_community_root(cand)]
            if checkouts:
                what = f"{len(checkouts)} Odoo checkouts (each file is analysed against its own version)"
            else:
                mods = []
                for m in iter_module_dirs(folder, max_depth=3):
                    mods.append(m)
                    if len(mods) >= MAX_HINT_MODULES:
                        break
                if not mods:
                    return 0
                v = guess_version(mods)
                count = f"{len(mods)}+" if len(mods) >= MAX_HINT_MODULES else str(len(mods))
                what = f"{count} Odoo module(s)" + (f" for Odoo {v.label}" if v else "")
        print(
            f"This project contains {what}. The odoo-lens plugin indexes it together with the matching Odoo "
            "sources: before grepping or reading Odoo code, load the `odoo-lens:odoo-lens` skill (LSP tool: "
            "hover / goToDefinition / findReferences / goToImplementation / workspaceSymbol give exact answers on "
            "models, fields, methods, override order, xml ids and views). To find what is broken in a module "
            "(unknown fields / models / methods / xml ids, missing dependencies, files never imported, syntax "
            f"removed in this Odoo version), run first: {launcher} check <module_dir>"
        )
    except Exception:  # SessionStart hook: must never fail
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("serve", "--stdio") or (argv[0].startswith("--") and argv[0] not in ("-h", "--help", "--version")):
        from .server import serve_stdio

        return serve_stdio()
    parser = argparse.ArgumentParser(prog="odoo-lens", description="Odoo language server")
    parser.add_argument("--version", action="version", version=f"odoo-lens {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("env", help="show the detected Odoo environment")
    p.add_argument("path")
    p.set_defaults(func=cmd_env)
    p = sub.add_parser("index", help="build the index and print timings")
    p.add_argument("path")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--workers", type=int)
    p.set_defaults(func=cmd_index)
    p = sub.add_parser("query", help="run a feature at FILE:LINE:COL (1-based)")
    p.add_argument("op", choices=QUERY_OPS)
    p.add_argument("file")
    p.add_argument("line", type=int)
    p.add_argument("col", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--workers", type=int)
    p.set_defaults(func=cmd_query)
    p = sub.add_parser("symbols", help="workspace symbol search")
    p.add_argument("query")
    p.add_argument("--path")
    p.add_argument("--limit", type=int, help="print at most N results (default: the whole answer)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--workers", type=int)
    p.set_defaults(func=cmd_symbols)
    p = sub.add_parser("check", help="print diagnostics")
    p.add_argument("paths", nargs="+")
    p.add_argument("--json", action="store_true")
    p.add_argument("--workers", type=int)
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("session-hint", help="SessionStart hook note when the project holds Odoo code")
    p.add_argument("path", nargs="?")
    p.set_defaults(func=cmd_session_hint)
    args = parser.parse_args(argv)
    return args.func(args)
