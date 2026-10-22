"""LSP server: lifecycle, document sync, env/index management, request routing.

Threads:
* dispatcher (main): every handler + every registry mutation/query;
* reader (protocol): parses stdin;
* indexers: one background thread per env being (re)built; they publish a fully
  built registry by assigning ``env.registry`` and setting ``env.ready`` - the
  dispatcher then applies open-document overlays lazily (see :meth:`_registry`);
* refresher: periodically re-stats indexed files in the background and posts the
  changes to the dispatcher.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from typing import Any

from . import __version__
from .documents import DocumentStore, path_to_uri, uri_to_path
from .envs import Env, EnvManager
from .extract import extract_source, kind_for_path
from .facts import CLASSES, F_NAME, K_FIELDS, K_METHODS, K_MODEL, K_NAME, KIND, KIND_MANIFEST, KIND_PY, M_NAME
from .protocol import (
    INVALID_REQUEST,
    NEVER_CANCELLED,
    SERVER_NOT_INITIALIZED,
    CancelToken,
    Endpoint,
    ResponseError,
    Transport,
)

log = logging.getLogger("odoo_lens.server")

INDEX_WAIT = float(os.environ.get("ODOO_LENS_INDEX_WAIT", "45"))  # max seconds a request waits (Claude Code times out at 60)
REFRESH_INTERVAL = float(os.environ.get("ODOO_LENS_REFRESH", "30"))  # stat sweep period (0 = off)
MAX_ENVS = int(os.environ.get("ODOO_LENS_MAX_ENVS", "3"))  # loaded registries kept in memory
DIAG_DELAY = 0.35


def _purge_caches() -> None:
    """Drop module-level analysis caches (they may reference an evicted registry)."""
    from .analysis import js as _js
    from .analysis import python as _py
    from .analysis import xml as _xml

    for mod in (_py, _xml, _js):
        try:
            mod.clear_caches()
        except Exception:
            log.exception("clearing caches of %s failed", mod.__name__)


def overlay_paths(registry: Any) -> set[str]:
    """Paths whose facts in ``registry`` come from open documents (not from disk).

    Stored on the registry itself so that a freshly built registry starts clean."""
    paths = registry.__dict__.get("overlay_paths")
    if paths is None:
        paths = registry.__dict__["overlay_paths"] = set()
    return paths


class RequestContext:
    """Everything a feature needs to answer a positional request."""

    __slots__ = ("server", "env", "registry", "path", "uri", "kind", "module", "scope", "line", "col", "token", "_facts", "_source")

    def __init__(self, server: "OdooLanguageServer", env: Env, registry: Any, path: str, line: int = 0, col: int = 0, token: CancelToken = NEVER_CANCELLED):
        self.server = server
        self.env = env
        self.registry = registry
        self.path = path
        self.uri = server.documents.uri_for(path)
        name = os.path.basename(path)
        root = env.root_of(path)
        rel = os.path.relpath(path, root) if root else name
        # module-relative path for kind detection (static/src/... for js)
        mod = registry.module_of_path(path) if registry is not None else None
        if mod and registry.module(mod) is not None:
            rel = os.path.relpath(path, registry.module(mod).path)
        self.kind = kind_for_path(name, rel)
        self.module = mod
        self.scope = registry.scope_for_path(path) if registry is not None else None
        self.line = line
        self.col = col
        self.token = token
        self._facts: dict | None = None
        self._source: bytes | None = None

    @property
    def source(self) -> bytes:
        if self._source is None:
            self._source = self.server.documents.source(self.path) or b""
        return self._source

    @property
    def text(self) -> str:
        return self.source.decode("utf-8", "replace")

    @property
    def facts(self) -> dict | None:
        """Facts of the current file reflecting the current (possibly unsaved) text."""
        if self._facts is None:
            self._facts = self.server.current_facts(self.env, self.path)
        return self._facts

    def location(self, path: str, line: int, col: int, end_col: int, end_line: int | None = None) -> dict:
        return {
            "uri": self.server.documents.uri_for(path),
            "range": {
                "start": {"line": line, "character": col},
                "end": {"line": line if end_line is None else end_line, "character": end_col},
            },
        }


def _keeps_members(old: dict, new: dict) -> bool:
    """The facts of a repaired (syntax error) Python buffer still declare every
    class, field and method of the last good ones: safe to index."""
    if old.get(KIND) != KIND_PY or new.get(KIND) != KIND_PY:
        return False  # XML / CSV after a parse error: truncated
    oc, nc = old.get(CLASSES) or (), new.get(CLASSES) or ()
    if [c[K_NAME] for c in oc] != [c[K_NAME] for c in nc]:
        return False
    for a, b in zip(oc, nc):
        if a[K_MODEL] != b[K_MODEL]:
            return False
        if not {f[F_NAME] for f in a[K_FIELDS]} <= {f[F_NAME] for f in b[K_FIELDS]}:
            return False
        if not {m[M_NAME] for m in a[K_METHODS]} <= {m[M_NAME] for m in b[K_METHODS]}:
            return False
    return True


class OdooLanguageServer:
    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint
        self.documents = DocumentStore()
        self.envs = EnvManager()
        self.initialized = False
        self.client_caps: dict = {}
        self.init_options: dict = {}
        self.workspace_folders: list[str] = []
        self.progress_supported = False
        self.markdown = True
        self.hierarchical_symbols = True
        self._doc_facts: dict[str, tuple[Any, dict]] = {}  # path -> (doc version, facts)
        self._disk_facts: dict[tuple, dict] = {}  # (path, mtime, size) -> full facts of non-open files
        self._published: dict[str, int] = {}  # uri -> number of diagnostics last published
        self._edited: set[str] = set()  # paths changed through didChange/didSave (diagnostics always published)
        self._open_hint: Env | None = None  # env of a didOpen immediately preceding a request
        self._workspace_envs: list[Env] = []
        self._index_threads: dict[str, threading.Thread] = {}
        self._refreshing = False
        self._parent_pid: int | None = None
        self._register()

    # ------------------------------------------------------------------ wiring

    def _register(self) -> None:
        ep = self.endpoint
        req = ep.on_request
        note = ep.on_notification
        req("initialize", self.on_initialize)
        note("initialized", self.on_initialized)
        note("textDocument/didOpen", self.on_did_open)
        note("textDocument/didChange", self.on_did_change)
        note("textDocument/didClose", self.on_did_close)
        note("textDocument/didSave", self.on_did_save)
        note("workspace/didChangeWatchedFiles", self.on_watched_files)
        note("workspace/didChangeWorkspaceFolders", self.on_workspace_folders)
        note("workspace/didChangeConfiguration", lambda params: None)
        note("$/setTrace", lambda params: None)
        for method, handler in (
            ("textDocument/definition", self.on_definition),
            ("textDocument/declaration", self.on_definition),
            ("textDocument/typeDefinition", self.on_type_definition),
            ("textDocument/references", self.on_references),
            ("textDocument/hover", self.on_hover),
            ("textDocument/implementation", self.on_implementation),
            ("textDocument/documentSymbol", self.on_document_symbol),
            ("workspace/symbol", self.on_workspace_symbol),
            ("textDocument/prepareCallHierarchy", self.on_prepare_call_hierarchy),
            ("callHierarchy/incomingCalls", self.on_incoming_calls),
            ("callHierarchy/outgoingCalls", self.on_outgoing_calls),
            ("textDocument/diagnostic", self.on_pull_diagnostics),
            ("odoo/status", self.on_status),
        ):
            req(method, self._guard(handler))

    def _guard(self, handler):
        def wrapped(params: Any, token: CancelToken) -> Any:
            self._hint, self._open_hint = self._open_hint, None
            if not self.initialized:
                raise ResponseError(SERVER_NOT_INITIALIZED, "Server not initialized")
            if getattr(self.endpoint, "shutdown_received", False):
                raise ResponseError(INVALID_REQUEST, "Server is shutting down")
            return handler(params or {}, token)

        return wrapped

    # ------------------------------------------------------------- lifecycle

    def on_initialize(self, params: dict, token: CancelToken) -> dict:
        params = params or {}
        self.client_caps = params.get("capabilities") or {}
        self.init_options = params.get("initializationOptions") or {}
        td = self.client_caps.get("textDocument") or {}
        window = self.client_caps.get("window") or {}
        self.progress_supported = bool(window.get("workDoneProgress"))
        hover_formats = (td.get("hover") or {}).get("contentFormat")
        self.markdown = not hover_formats or "markdown" in hover_formats
        self.hierarchical_symbols = bool((td.get("documentSymbol") or {}).get("hierarchicalDocumentSymbolSupport", True))
        folders = params.get("workspaceFolders") or []
        self.workspace_folders = [uri_to_path(f["uri"]) for f in folders if f.get("uri")]
        if not self.workspace_folders:
            root = params.get("rootUri") or params.get("rootPath")
            if root:
                self.workspace_folders = [uri_to_path(root) if "://" in root else root]
        pid = params.get("processId")
        if isinstance(pid, int) and pid > 0:
            self._parent_pid = pid
            threading.Thread(target=self._watch_parent, name="odoo-lens-parent-watch", daemon=True).start()
        self.initialized = True
        return {
            "capabilities": {
                "positionEncoding": "utf-16",
                "textDocumentSync": {"openClose": True, "change": 2, "save": {"includeText": False}},
                "definitionProvider": True,
                "declarationProvider": True,
                "typeDefinitionProvider": True,
                "referencesProvider": True,
                "hoverProvider": True,
                "implementationProvider": True,
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
                "callHierarchyProvider": True,
                "workspace": {"workspaceFolders": {"supported": True, "changeNotifications": True}},
            },
            "serverInfo": {"name": "odoo-lens", "version": __version__},
        }

    def on_initialized(self, params: Any) -> None:
        for folder in self.workspace_folders:
            for env in self.envs.envs_for_workspace(folder):
                env.last_used = time.monotonic()
                self._workspace_envs.append(env)
                self.ensure_index(env)
        if REFRESH_INTERVAL > 0:
            self.endpoint.post_later(REFRESH_INTERVAL, "refresh", self._refresh_tick)

    def _watch_parent(self) -> None:
        while not self.endpoint._stopped:
            time.sleep(5)
            try:
                os.kill(self._parent_pid, 0)  # type: ignore[arg-type]
            except ProcessLookupError:
                log.info("parent process %s is gone, exiting", self._parent_pid)
                from .indexer import terminate_workers

                terminate_workers()
                os._exit(1)
            except PermissionError:
                pass

    # ------------------------------------------------------------- logging

    def log_message(self, message: str, level: int = 3) -> None:
        log.info(message)
        try:
            self.endpoint.notify("window/logMessage", {"type": level, "message": message})
        except Exception:
            pass

    # ------------------------------------------------------------- indexing

    def ensure_index(self, env: Env) -> None:
        """Start indexing ``env`` in the background unless built or in progress."""
        if env.registry is not None or env.indexing:
            return
        env.indexing = True
        self._evict_envs(keep=env)
        t = threading.Thread(target=self._index_env, args=(env,), name=f"odoo-lens-index-{env.key}", daemon=True)
        self._index_threads[env.key] = t
        t.start()

    def _index_env(self, env: Env) -> None:
        from .indexer import build_index

        token = f"odoo-lens-index-{env.key}"
        progress = None
        if self.progress_supported:
            try:
                self.endpoint.request("window/workDoneProgress/create", {"token": token})
                self.endpoint.notify("$/progress", {"token": token, "value": {"kind": "begin", "title": f"Indexing {env.label}", "percentage": 0}})
                last = [0.0]

                def progress(stage: str, done: int, total: int) -> None:
                    now = time.monotonic()
                    if now - last[0] < 0.25 and done < total:
                        return
                    last[0] = now
                    pct = int(done * 100 / total) if total else 0
                    self.endpoint.notify("$/progress", {"token": token, "value": {"kind": "report", "message": f"{stage} {done}/{total}", "percentage": pct}})
            except Exception:
                progress = None
        try:
            registry, stats = build_index(env, progress=progress)
        except Exception as exc:
            log.exception("indexing %s failed", env.label)
            self.endpoint.post(self.log_message, f"odoo-lens: indexing {env.label} failed: {exc}", 1)
            registry = None
            stats = None
        finally:
            env.indexing = False
            if self.progress_supported and progress is not None:
                try:
                    self.endpoint.notify("$/progress", {"token": token, "value": {"kind": "end"}})
                except Exception:
                    pass
        if registry is not None:
            env.registry = registry
            env.ready.set()
            self.endpoint.post(self.log_message, f"odoo-lens: indexed {env.label}: {stats.summary()}")
            self.endpoint.post(self._after_index, env)
        else:
            env.ready.set()  # do not let requests wait forever

    def _after_index(self, env: Env) -> None:
        self._registry(env)  # applies overlays
        for path in self.documents.open_paths():
            if self.envs.env_for_path(path) is env and self.should_publish(env, path):
                self.publish_diagnostics(path)

    def _evict_envs(self, keep: Env) -> None:
        loaded = [e for e in self.envs.envs.values() if e.registry is not None and e is not keep]
        if len(loaded) + 1 <= MAX_ENVS:
            return
        loaded.sort(key=lambda e: e.last_used)
        for env in loaded[: len(loaded) + 1 - MAX_ENVS]:
            log.info("evicting index of %s", env.label)
            env.registry = None
            env.ready.clear()
        _purge_caches()
        # registries are gc.freeze()d after their build: unfreeze to let the
        # collector reclaim the evicted ones, then freeze the survivors again
        gc.unfreeze()
        gc.collect()
        gc.freeze()

    def _registry(self, env: Env, wait: bool = True) -> Any:
        """The published registry of ``env`` with open-document overlays applied."""
        env.last_used = time.monotonic()
        if env.registry is None:
            self.ensure_index(env)
            if wait:
                env.ready.wait(INDEX_WAIT)
        reg = env.registry
        if reg is None:
            return None
        overlaid = overlay_paths(reg)
        for path in self.documents.open_paths():
            if path not in overlaid and self.envs.env_for_path(path) is env:
                self._sync_doc(path)
        return reg

    def _adopt_new_module(self, env: Env, path: str) -> None:
        """A file of a module created after indexing (Claude's Write tool): register
        the module right away and index its other files in the background, instead
        of waiting for the next stat sweep."""
        reg = env.registry
        if reg is None or reg.module_of_path(path) is not None:
            return
        root = env.root_of(path)
        if root is None or root == env.framework_dir:
            return
        rel = os.path.relpath(path, root).split(os.sep)
        mod_dir = os.path.join(root, rel[0])
        if len(rel) < 2 or rel[0] in reg.module_dirs or not os.path.isfile(os.path.join(mod_dir, "__manifest__.py")):
            return
        log.info("new module %s: indexing it now", rel[0])
        reg.set_modules({**reg.module_dirs, rel[0]: mod_dir})
        if not self._refreshing:
            self._start_refresh([env])

    def _matches_index(self, reg: Any, path: str) -> bool:
        """True if the open document's text is exactly the indexed disk content
        (Claude Code opens files with their disk content: nothing to overlay, and
        overlaying would needlessly invalidate every registry cache)."""
        doc = self.documents.get(path)
        if doc is None or path not in reg.file_stats:
            return False
        try:
            st = os.stat(path)
            if reg.file_stats[path] != (st.st_mtime_ns, st.st_size):
                return False
            with open(path, "rb") as fh:
                return fh.read() == doc.source()
        except OSError:
            return False

    def current_facts(self, env: Env, path: str) -> dict | None:
        """Full facts for ``path`` (open document text, else the file on disk).

        Unlike the index (built with ``index=True``), these include the
        high-volume facts such as XML record-level ``<field name>`` refs."""
        doc = self.documents.get(path)
        if doc is None:
            try:
                st = os.stat(path)
            except OSError:
                return None
            key = (path, st.st_mtime_ns, st.st_size)
            hit = self._disk_facts.get(key)
            if hit is not None:
                return hit
            kind = self._kind(env, path)
            src = self.documents.source(path)
            if src is None or kind is None:
                return None
            facts = extract_source(path, kind, src)
            if len(self._disk_facts) > 64:
                self._disk_facts.clear()
            self._disk_facts[key] = facts
            return facts
        cached = self._doc_facts.get(path)
        if cached is not None and cached[0] == doc.rev:
            return cached[1]
        kind = self._kind(env, path)
        if kind is None:
            return None
        facts = extract_source(path, kind, doc.source())
        self._doc_facts[path] = (doc.rev, facts)
        return facts

    def _kind(self, env: Env, path: str) -> str | None:
        name = os.path.basename(path)
        reg = env.registry
        mod = reg.module_of_path(path) if reg is not None else None
        rel = name
        if mod and reg.module(mod) is not None:
            rel = os.path.relpath(path, reg.module(mod).path)
        return kind_for_path(name, rel)

    def _sync_doc(self, path: str) -> None:
        """Push an open document's current facts into its env's registry."""
        env = self.envs.env_for_path(path)
        if env is None or env.registry is None:
            return
        if self.documents.refresh_from_disk(path):  # changed on disk behind our back
            self._doc_facts.pop(path, None)
        self._adopt_new_module(env, path)
        if path not in overlay_paths(env.registry) and self._matches_index(env.registry, path):
            return
        facts = self.current_facts(env, path)
        if facts is None:
            return
        overlay_paths(env.registry).add(path)
        if facts.get("error"):
            old = env.registry.file_facts(path)
            if old is not None and not old.get("error") and not _keeps_members(old, facts):
                # mid-edit syntax error that hides definitions: keep the last good facts
                # indexed (the error itself is still reported by diagnostics, computed
                # from current_facts)
                return
        env.registry.update_file(path, facts)
        if facts.get("kind") == KIND_MANIFEST:
            env.registry.set_modules(dict(env.registry.module_dirs))

    # ----------------------------------------------------------- refresh

    def _refresh_tick(self) -> None:
        if getattr(self.endpoint, "shutdown_received", False):
            return
        if not self._refreshing:
            envs = [e for e in self.envs.envs.values() if e.registry is not None and not e.indexing]
            if envs:
                self._start_refresh(envs)
        self.endpoint.post_later(REFRESH_INTERVAL, "refresh", self._refresh_tick)

    def _start_refresh(self, envs: list[Env]) -> None:
        """Start a stat sweep of ``envs`` in the background (dispatcher thread only).

        The sweep compares against copies of each registry's ``file_stats`` and
        ``module_dirs`` taken here: the background thread never reads a published
        registry."""
        snapshots = []
        for env in envs:
            reg = env.registry
            if reg is not None:
                snapshots.append((env, reg, dict(reg.file_stats), dict(reg.module_dirs)))
        if not snapshots:
            return
        self._refreshing = True
        threading.Thread(target=self._refresh_bg, args=(snapshots,), name="odoo-lens-refresh", daemon=True).start()

    def _refresh_bg(self, snapshots: list[tuple[Env, Any, dict, dict]]) -> None:
        from .indexer import extract_files, scan

        try:
            for env, reg, file_stats, module_dirs in snapshots:
                sc = scan(env)
                seen = set()
                todo = []
                for fe in sc.all_files():
                    seen.add(fe.path)
                    if file_stats.get(fe.path) != (fe.mtime, fe.size):
                        todo.append(fe)
                removed = [p for p in file_stats if p not in seen]
                modules_changed = sc.modules != module_dirs
                if not todo and not removed and not modules_changed:
                    continue
                results = extract_files(todo)
                self.endpoint.post(self._apply_refresh, env, reg, sc, todo, results, removed)
        except Exception:
            log.exception("refresh failed")
        finally:
            self.endpoint.post(setattr, self, "_refreshing", False)

    def _apply_refresh(self, env: Env, reg: Any, sc: Any, todo: list, results: list, removed: list[str]) -> None:
        if env.registry is not reg:
            return
        from .indexer import _save_shards_async

        overlaid = overlay_paths(reg)
        changes: dict[str, dict | None] = {}
        dirty = set()
        by_path = {fe.path: fe for fe in todo}
        for path in removed:
            reg.file_stats.pop(path, None)
            root = env.root_of(path)
            if root and root in reg.shards:
                reg.shards[root].pop(os.path.relpath(path, root), None)
                dirty.add(root)
            doc = self.documents.get(path)
            if doc is not None:  # deleted behind our back (git, rm): forget the document too
                self.documents.close(doc.uri)
                self._doc_facts.pop(path, None)
            overlaid.discard(path)
            changes[path] = None
        for path, mtime, size, facts in results:
            fe = by_path.get(path)
            if fe is None:
                continue
            reg.file_stats[path] = (mtime, size)
            reg.shards.setdefault(fe.root, {})[fe.rel] = (mtime, size, facts)
            dirty.add(fe.root)
            if self.documents.is_open(path):
                # Claude Code's edits write the file: the disk is the truth for open
                # documents too (no didClose/didChangeWatchedFiles ever arrives)
                if self.documents.refresh_from_disk(path):
                    self._doc_facts.pop(path, None)
                if self._matches_index(reg, path):
                    overlaid.discard(path)
                else:  # an editor buffer with unsaved changes: keep overlaying it
                    facts = self.current_facts(env, path) or facts
            changes[path] = facts
        module_dirs = sc.modules if sc.modules != reg.module_dirs else None
        if changes or module_dirs is not None:
            reg.update_files(changes, module_dirs)
        if dirty:
            _save_shards_async({r: dict(reg.shards[r]) for r in dirty})
        if results or removed:
            log.info("refreshed %s: %d changed, %d removed", env.label, len(results), len(removed))
            for path in self.documents.open_paths():
                if self.envs.env_for_path(path) is env and self.should_publish(env, path):
                    self.publish_diagnostics(path)

    # ------------------------------------------------------------- documents

    def on_did_open(self, params: dict) -> None:
        td = params["textDocument"]
        doc = self.documents.open(td["uri"], td.get("text", ""), td.get("version"), td.get("languageId", ""))
        self._doc_facts.pop(doc.path, None)
        env = self.envs.env_for_path(doc.path)
        if env is None:
            return
        env.last_used = time.monotonic()
        # Claude Code opens the file of an LSP call right before the request: the
        # best hint of which env a following workspace/symbol is about
        self._open_hint = env
        try:  # the first Edit/Write of a file arrives as a didOpen of the new content
            if time.time() - os.stat(doc.path).st_mtime < 3:
                self._edited.add(doc.path)
        except OSError:
            pass
        self.ensure_index(env)
        self._sync_doc(doc.path)
        # Claude Code opens files when navigating into them; only report problems
        # for files outside the Odoo sources (custom modules) or files being edited.
        if self.should_publish(env, doc.path):
            self.publish_diagnostics(doc.path)

    def on_did_change(self, params: dict) -> None:
        td = params["textDocument"]
        doc = self.documents.change(td["uri"], td.get("version"), params.get("contentChanges") or [])
        if doc is None:
            return
        self._edited.add(doc.path)
        # publish right away: Claude Code collects diagnostics right after the tool batch
        self._sync_doc(doc.path)
        self.publish_diagnostics(doc.path)

    def on_did_save(self, params: dict) -> None:
        path = uri_to_path(params["textDocument"]["uri"])
        doc = self.documents.get(path)
        if doc is None:
            return
        if self.documents.refresh_from_disk(path):
            self._sync_doc(path)
            self.publish_diagnostics(path)

    def should_publish(self, env: Env, path: str) -> bool:
        return path in self._edited or self._is_custom(env, path)

    def _is_custom(self, env: Env, path: str) -> bool:
        root = env.root_of(path)
        return root is not None and root in env.custom_roots

    def on_did_close(self, params: dict) -> None:
        uri = params["textDocument"]["uri"]
        doc = self.documents.close(uri)
        if doc is None:
            return
        self._doc_facts.pop(doc.path, None)
        env = self.envs.env_for_path(doc.path)
        if env is not None and env.registry is not None:
            overlay_paths(env.registry).discard(doc.path)
            src = self.documents.source(doc.path)
            kind = self._kind(env, doc.path)
            if src is not None and kind is not None:
                env.registry.update_file(doc.path, extract_source(doc.path, kind, src))
            else:
                env.registry.remove_file(doc.path)
        if self._published.pop(uri, 0):
            self.endpoint.notify("textDocument/publishDiagnostics", {"uri": uri, "diagnostics": []})

    def on_watched_files(self, params: dict) -> None:
        for change in params.get("changes") or []:
            path = uri_to_path(change["uri"])
            if self.documents.is_open(path):
                continue
            env = self.envs.env_for_path(path)
            if env is None or env.registry is None:
                continue
            if change.get("type") == 3:
                env.registry.remove_file(path)
                env.registry.file_stats.pop(path, None)
                continue
            kind = self._kind(env, path)
            src = self.documents.source(path)
            if kind is None or src is None:
                continue
            env.registry.update_file(path, extract_source(path, kind, src))
            try:
                st = os.stat(path)
                env.registry.file_stats[path] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass

    def on_workspace_folders(self, params: dict) -> None:
        event = params.get("event") or {}
        for f in event.get("added") or []:
            folder = uri_to_path(f["uri"])
            self.workspace_folders.append(folder)
            for env in self.envs.envs_for_workspace(folder):
                self.ensure_index(env)
        for f in event.get("removed") or []:
            folder = uri_to_path(f["uri"])
            if folder in self.workspace_folders:
                self.workspace_folders.remove(folder)

    # ------------------------------------------------------------- diagnostics

    def schedule_diagnostics(self, path: str, delay: float = DIAG_DELAY) -> None:
        if not self.documents.is_open(path):
            return
        self.endpoint.post_later(delay, ("diag", path), self.publish_diagnostics, path)

    def publish_diagnostics(self, path: str) -> None:
        doc = self.documents.get(path)
        if doc is None:
            return
        env = self.envs.env_for_path(path)
        if env is None or not env.diagnostics_enabled:
            return
        reg = self._registry(env, wait=False)
        if reg is None:
            return  # published once the index is ready (_after_index)
        from . import features

        ctx = RequestContext(self, env, reg, path)
        try:
            diags = features.diagnostics(ctx)
        except Exception:
            log.exception("diagnostics failed for %s", path)
            return
        if not diags and not self._published.get(doc.uri):
            return
        self._published[doc.uri] = len(diags)
        self.endpoint.notify(
            "textDocument/publishDiagnostics", {"uri": doc.uri, "version": doc.version, "diagnostics": diags}
        )

    def on_pull_diagnostics(self, params: dict, token: CancelToken) -> dict:
        ctx = self._context(params, token, positional=False)
        if ctx is None:
            return {"kind": "full", "items": []}
        from . import features

        return {"kind": "full", "items": features.diagnostics(ctx)}

    # ------------------------------------------------------------- requests

    def _context(self, params: dict, token: CancelToken, positional: bool = True) -> RequestContext | None:
        uri = (params.get("textDocument") or {}).get("uri")
        if not uri:
            return None
        path = uri_to_path(uri)
        env = self.envs.env_for_path(path)
        if env is None:
            return None
        if self.documents.refresh_from_disk(path):  # changed on disk behind our back (git, Bash...)
            self._doc_facts.pop(path, None)
            if env.registry is not None:
                self._sync_doc(path)
        reg = self._registry(env)
        if reg is None:
            return None
        token.check()
        pos = params.get("position") or {}
        return RequestContext(self, env, reg, path, pos.get("line", 0), pos.get("character", 0), token)

    def on_definition(self, params: dict, token: CancelToken) -> Any:
        ctx = self._context(params, token)
        if ctx is None:
            return None
        from . import features

        return _one_or_many(features.definition(ctx))

    def on_type_definition(self, params: dict, token: CancelToken) -> Any:
        ctx = self._context(params, token)
        if ctx is None:
            return None
        from . import features

        return _one_or_many(features.type_definition(ctx))

    def on_references(self, params: dict, token: CancelToken) -> Any:
        ctx = self._context(params, token)
        if ctx is None:
            return None
        from . import features

        include_decl = bool((params.get("context") or {}).get("includeDeclaration", True))
        return features.references(ctx, include_decl) or None

    def on_hover(self, params: dict, token: CancelToken) -> Any:
        ctx = self._context(params, token)
        if ctx is None:
            return None
        from . import features

        return features.hover(ctx)

    def on_implementation(self, params: dict, token: CancelToken) -> Any:
        ctx = self._context(params, token)
        if ctx is None:
            return None
        from . import features

        return _one_or_many(features.implementation(ctx))

    def on_document_symbol(self, params: dict, token: CancelToken) -> Any:
        ctx = self._context(params, token, positional=False)
        if ctx is None:
            return None
        from . import features

        return features.document_symbols(ctx, hierarchical=self.hierarchical_symbols)

    def on_workspace_symbol(self, params: dict, token: CancelToken) -> Any:
        from . import features

        query = params.get("query") or ""
        # workspace/symbol carries no file: use the env of the file Claude Code just
        # opened for this call, else the workspace's env, else the most recently used
        hint = getattr(self, "_hint", None)
        if hint is not None:
            envs = [hint]
        elif len(self._workspace_envs) == 1:
            envs = list(self._workspace_envs)
        else:
            envs = sorted(self.envs.envs.values(), key=lambda e: -e.last_used)
        if not envs:
            for folder in self.workspace_folders:
                envs.extend(self.envs.envs_for_workspace(folder))
        results: list = []
        for env in envs[:1] or []:
            reg = self._registry(env)
            if reg is None:
                continue
            results.extend(features.workspace_symbols(self, env, reg, query, token))
        return results

    def on_prepare_call_hierarchy(self, params: dict, token: CancelToken) -> Any:
        ctx = self._context(params, token)
        if ctx is None:
            return None
        from . import features

        return features.prepare_call_hierarchy(ctx) or None

    def _item_context(self, params: dict, token: CancelToken) -> RequestContext | None:
        item = params.get("item") or {}
        uri = item.get("uri")
        if not uri:
            return None
        sel = (item.get("selectionRange") or item.get("range") or {}).get("start") or {}
        return self._context({"textDocument": {"uri": uri}, "position": sel}, token)

    def on_incoming_calls(self, params: dict, token: CancelToken) -> Any:
        ctx = self._item_context(params, token)
        if ctx is None:
            return None
        from . import features

        return features.incoming_calls(ctx, params["item"]) or None

    def on_outgoing_calls(self, params: dict, token: CancelToken) -> Any:
        ctx = self._item_context(params, token)
        if ctx is None:
            return None
        from . import features

        return features.outgoing_calls(ctx, params["item"]) or None

    def on_status(self, params: dict, token: CancelToken) -> Any:
        out = []
        for env in self.envs.envs.values():
            reg = env.registry
            out.append(
                {
                    "env": env.label,
                    "version": env.version.label if env.version else None,
                    "addons_roots": env.addons_roots,
                    "indexed": reg is not None,
                    "indexing": env.indexing,
                    "files": len(reg.files) if reg is not None else 0,
                }
            )
        return out


def _one_or_many(locations: list | None) -> Any:
    """A single Location rather than a 1-item array: Claude Code's gitignore
    filter only applies to arrays, so a lone definition always gets through."""
    if not locations:
        return None
    return locations[0] if len(locations) == 1 else locations


def setup_logging() -> None:
    level = os.environ.get("ODOO_LENS_LOG_LEVEL", "INFO").upper()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = os.environ.get("ODOO_LENS_LOG")
    if log_file:
        handlers.append(logging.FileHandler(os.path.expanduser(log_file)))
    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=handlers, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def serve_stdio() -> int:
    setup_logging()
    # Keep private, non-inheritable copies of the protocol pipes and point fds 0/1
    # elsewhere: no child process (indexing pool, multiprocessing's resource
    # tracker) can then write into the protocol stream or keep the pipes open.
    in_fd, out_fd = os.dup(0), os.dup(1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    stdin = open(in_fd, "rb")
    stdout = open(out_fd, "wb")
    sys.stdout = sys.stderr  # nothing but protocol messages may reach the protocol pipe
    endpoint = Endpoint(Transport(stdin, stdout))

    def hard_exit(code: int) -> None:
        from .indexer import terminate_workers

        try:
            terminate_workers()
            stdout.flush()
            logging.shutdown()
        finally:
            os._exit(code)

    endpoint.on_exit = hard_exit
    OdooLanguageServer(endpoint)  # registers its handlers on the endpoint
    endpoint.serve_forever()
    # stdin EOF (or stop): the reader thread is blocked in readline() holding the
    # buffer lock, so a regular interpreter shutdown would abort - leave directly
    hard_exit(0 if endpoint.shutdown_received else 1)
    return 0
