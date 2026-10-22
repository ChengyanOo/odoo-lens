from __future__ import annotations

import gc
import logging
import marshal
import multiprocessing
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Callable, Iterable

from . import cache
from .envs import MANIFEST, Env
from .extract import extract_source, kind_for_path

log = logging.getLogger("odoo_lens.indexer")

PRUNE = frozenset({"__pycache__", "node_modules", ".git", ".hg", ".svn", "i18n", "i18n_extra", ".tox", ".venv", "venv"})
PRUNE_REL = frozenset(
    {"static/lib", "static/description", "static/img", "static/fonts", "static/src/img", "static/src/fonts", "static/src/libs"}
)

PARALLEL_THRESHOLD = 300


class FileEntry:
    __slots__ = ("path", "root", "rel", "kind", "mtime", "size", "module")

    def __init__(self, path: str, root: str, rel: str, kind: str, mtime: int, size: int, module: str | None):
        self.path = path
        self.root = root
        self.rel = rel
        self.kind = kind
        self.mtime = mtime
        self.size = size
        self.module = module


class Scan:
    def __init__(self) -> None:
        self.files: dict[str, list[FileEntry]] = {}
        self.modules: dict[str, str] = {}
        self.shadowed: list[str] = []

    def all_files(self) -> Iterable[FileEntry]:
        for entries in self.files.values():
            yield from entries


def _walk_module(module_dir: str, root: str, module: str, out: list[FileEntry]) -> None:
    stack = [module_dir]
    plen = len(module_dir) + 1
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                name = e.name
                if name[0] == ".":
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if name in PRUNE:
                            continue
                        if e.path[plen:].replace(os.sep, "/") in PRUNE_REL:
                            continue
                        stack.append(e.path)
                        continue
                    kind = kind_for_path(name, e.path[plen:])
                    if kind is None:
                        continue
                    st = e.stat()
                except OSError:
                    continue
                out.append(
                    FileEntry(e.path, root, os.path.relpath(e.path, root), kind, st.st_mtime_ns, st.st_size, module)
                )


def _walk_framework(fw: str, out: list[FileEntry]) -> None:
    stack = [fw]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                name = e.name
                if name[0] == ".":
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        if name in PRUNE or (d == fw and name in ("addons", "static")):
                            continue
                        stack.append(e.path)
                        continue
                    if not name.endswith(".py"):
                        continue
                    st = e.stat()
                except OSError:
                    continue
                out.append(FileEntry(e.path, fw, os.path.relpath(e.path, fw), "py", st.st_mtime_ns, st.st_size, None))


def scan(env: Env) -> Scan:
    result = Scan()
    for root in env.addons_roots:
        entries: list[FileEntry] = []
        try:
            children = sorted(os.scandir(root), key=lambda e: e.name)
        except OSError:
            children = []
        for child in children:
            if child.name.startswith(".") or not child.is_dir():
                continue
            if not os.path.isfile(os.path.join(child.path, MANIFEST)):
                continue
            if child.name in result.modules:
                result.shadowed.append(child.path)
                continue
            result.modules[child.name] = child.path
            _walk_module(child.path, root, child.name, entries)
        result.files[root] = entries
    fw = env.framework_dir
    if fw and os.path.isdir(fw):
        entries = []
        _walk_framework(fw, entries)
        result.files[fw] = entries
    return result


# pool workers: stdout -> stderr (protocol pipe), no cyclic GC (ASTs are acyclic), exit with the server
def _worker_init(parent: int | None = None) -> None:
    try:
        os.dup2(2, 1)
    except OSError:
        pass
    gc.disable()
    if parent:

        def watch() -> None:
            while True:
                time.sleep(2)
                if os.getppid() != parent:
                    os._exit(0)

        threading.Thread(target=watch, name="odoo-lens-worker-watch", daemon=True).start()


_POOLS: set = set()


def terminate_workers() -> None:
    for pool in list(_POOLS):
        for proc in list(getattr(pool, "_processes", {}).values()):
            try:
                proc.kill()
            except Exception:
                pass


# one marshal blob per batch (facts are builtin types): cheap transfer back from pool workers
def _extract_batch(batch: list[tuple[str, str]]) -> bytes:
    out = []
    for path, kind in batch:
        try:
            st = os.stat(path)
            with open(path, "rb") as fh:
                src = fh.read()
        except OSError:
            continue
        out.append((path, st.st_mtime_ns, st.st_size, extract_source(path, kind, src, index=True)))
    return marshal.dumps(out)


def _batches(items: list[tuple[str, str, int]], n: int) -> list[list[tuple[str, str]]]:
    items = sorted(items, key=lambda t: -t[2])
    batches: list[list[tuple[str, str]]] = [[] for _ in range(n)]
    loads = [0] * n
    for path, kind, size in items:
        i = loads.index(min(loads)) if n <= 64 else len(batches[0]) % n
        batches[i].append((path, kind))
        loads[i] += size + 2048
    return [b for b in batches if b]


def default_workers() -> int:
    explicit = os.environ.get("ODOO_LENS_WORKERS")
    if explicit and explicit.isdigit():
        return max(1, int(explicit))
    return max(1, min(12, (os.cpu_count() or 2) - 1))


def extract_files(
    todo: list[FileEntry],
    workers: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[tuple[str, int, int, dict]]:
    if not todo:
        return []
    workers = workers or default_workers()
    total = len(todo)
    if total < PARALLEL_THRESHOLD or workers <= 1:
        results = []
        for i, fe in enumerate(todo):
            results.extend(marshal.loads(_extract_batch([(fe.path, fe.kind)])))
            if progress and i % 200 == 0:
                progress(i, total)
        return results
    batches = _batches([(fe.path, fe.kind, fe.size) for fe in todo], workers * 4)
    results: list[tuple[str, int, int, dict]] = []
    ctx = multiprocessing.get_context("spawn")
    done = 0
    pool = ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_worker_init, initargs=(os.getpid(),))
    _POOLS.add(pool)
    try:
        for blob, batch in zip(pool.map(_extract_batch, batches), batches):
            results.extend(marshal.loads(blob))
            done += len(batch)
            if progress:
                progress(done, total)
    finally:
        _POOLS.discard(pool)
        pool.shutdown(wait=True, cancel_futures=True)
    return results


class IndexStats:
    def __init__(self) -> None:
        self.files = 0
        self.extracted = 0
        self.cached = 0
        self.modules = 0
        self.t_scan = 0.0
        self.t_cache = 0.0
        self.t_extract = 0.0
        self.t_registry = 0.0
        self.t_total = 0.0

    def summary(self) -> str:
        return (
            f"{self.modules} modules, {self.files} files ({self.cached} cached, {self.extracted} parsed) in "
            f"{self.t_total:.2f}s [scan {self.t_scan:.2f}s, cache {self.t_cache:.2f}s, "
            f"extract {self.t_extract:.2f}s, registry {self.t_registry:.2f}s]"
        )


def _save_shards_async(shards: dict[str, dict]) -> threading.Thread:
    def run() -> None:
        for root, files in shards.items():
            cache.save_shard(root, files)

    t = threading.Thread(target=run, name="odoo-lens-cache-writer", daemon=True)
    t.start()
    return t


def build_index(
    env: Env,
    workers: int | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    wait_for_cache_write: bool = False,
):
    from .registry import Registry

    stats = IndexStats()
    t0 = time.perf_counter()
    # GC off during build (it dominated build time), result gc.freeze()d; collect first: no cycle frozen
    with _BUILD_LOCK:
        gc.collect()
        gc_was_enabled = gc.isenabled()
        gc.disable()
        try:
            return _build_index(env, workers, progress, wait_for_cache_write, stats, t0)
        finally:
            if gc_was_enabled:
                gc.enable()
            gc.freeze()


_BUILD_LOCK = threading.Lock()  # one build at a time: they toggle process-wide GC state


def _build_index(env, workers, progress, wait_for_cache_write, stats, t0):
    from .registry import Registry

    sc = scan(env)
    stats.t_scan = time.perf_counter() - t0
    stats.modules = len(sc.modules)

    t1 = time.perf_counter()
    facts: dict[str, dict] = {}
    file_stats: dict[str, tuple[int, int]] = {}
    shards: dict[str, dict] = {}
    dirty: set[str] = set()
    todo: list[FileEntry] = []
    for root, entries in sc.files.items():
        old = cache.load_shard(root)
        new: dict = {}
        for fe in entries:
            hit = old.get(fe.rel)
            if hit is not None and hit[0] == fe.mtime and hit[1] == fe.size:
                new[fe.rel] = hit
                facts[fe.path] = hit[2]
                file_stats[fe.path] = (fe.mtime, fe.size)
            else:
                todo.append(fe)
        if len(new) != len(old):
            dirty.add(root)
        shards[root] = new
    stats.t_cache = time.perf_counter() - t1
    stats.files = sum(len(v) for v in sc.files.values())
    stats.cached = stats.files - len(todo)
    stats.extracted = len(todo)

    t2 = time.perf_counter()
    if todo:
        by_path = {fe.path: fe for fe in todo}
        for path, mtime, size, f in extract_files(
            todo, workers, (lambda d, t: progress("Parsing", d, t)) if progress else None
        ):
            fe = by_path.get(path)
            if fe is None:
                continue
            facts[path] = f
            file_stats[path] = (mtime, size)
            shards[fe.root][fe.rel] = (mtime, size, f)
            dirty.add(fe.root)
    stats.t_extract = time.perf_counter() - t2

    writer = _save_shards_async({r: shards[r] for r in dirty}) if dirty else None

    t3 = time.perf_counter()
    if progress:
        progress("Building registry", 0, 1)
    registry = Registry(env, facts, sc.modules)
    registry.file_stats = file_stats
    registry.shards = shards
    stats.t_registry = time.perf_counter() - t3
    stats.t_total = time.perf_counter() - t0
    if writer is not None and wait_for_cache_write:
        writer.join()
    log.info("indexed %s: %s", env.label, stats.summary())
    return registry, stats
