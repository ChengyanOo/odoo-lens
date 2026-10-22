from __future__ import annotations

import hashlib
import logging
import marshal
import os
import sys
import tempfile
import time

from .facts import FACTS_VERSION

log = logging.getLogger("odoo_lens.cache")

MAX_SHARDS = 64


def cache_dir() -> str:
    explicit = os.environ.get("ODOO_LENS_CACHE_DIR")
    if explicit:
        return os.path.expanduser(explicit)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "odoo-lens")


# One shard per root (shared across envs); marshal is Python-version specific, hence in the name.
def shard_path(root: str) -> str:
    ident = f"{FACTS_VERSION}|{sys.version_info[0]}.{sys.version_info[1]}|{os.path.realpath(root)}"
    digest = hashlib.sha1(ident.encode("utf-8", "surrogateescape")).hexdigest()[:20]
    name = os.path.basename(root.rstrip(os.sep)) or "root"
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:40]
    return os.path.join(cache_dir(), f"{safe}-{digest}.marshal")


Shard = dict  # relpath -> (mtime_ns, size, facts); indexer reuses facts only while both match


def load_shard(root: str) -> Shard:
    if os.environ.get("ODOO_LENS_NO_CACHE"):
        return {}
    path = shard_path(root)
    try:
        with open(path, "rb") as fh:
            data = marshal.loads(fh.read())
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log.warning("discarding unreadable cache %s: %s", path, exc)
        return {}
    if not isinstance(data, dict) or data.get("v") != FACTS_VERSION or data.get("root") != root:
        return {}
    try:
        os.utime(path)  # mtime = last use, for prune()
    except OSError:
        pass
    return data.get("files") or {}


def save_shard(root: str, files: Shard) -> None:
    if os.environ.get("ODOO_LENS_NO_CACHE"):
        return
    path = shard_path(root)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        blob = marshal.dumps({"v": FACTS_VERSION, "root": root, "files": files})
        # temp file + rename: concurrent servers never read a torn shard (last writer wins)
        fd, tmp = tempfile.mkstemp(prefix=".shard-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        log.warning("could not write cache %s: %s", path, exc)
        return
    prune()


def prune(max_shards: int = MAX_SHARDS) -> None:
    directory = cache_dir()
    try:
        entries = [e for e in os.scandir(directory) if e.name.endswith(".marshal")]
    except OSError:
        return
    if len(entries) <= max_shards:
        return
    entries.sort(key=lambda e: e.stat().st_mtime)
    for e in entries[: len(entries) - max_shards]:
        try:
            os.unlink(e.path)
        except OSError:
            pass
    now = time.time()
    try:
        for e in os.scandir(directory):
            if e.name.startswith(".shard-") and now - e.stat().st_mtime > 3600:
                os.unlink(e.path)
    except OSError:
        pass
