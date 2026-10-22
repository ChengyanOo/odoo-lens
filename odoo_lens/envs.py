from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from collections import Counter
from typing import Any, Iterable

log = logging.getLogger("odoo_lens.envs")

CONFIG_NAMES = (".odoo-lens.json", "odoo-lens.json")
MANIFEST = "__manifest__.py"

DEFAULT_SOURCE_TEMPLATES = (
    "~/odev/worktrees/{version}/odoo",
    "~/odoo/worktrees/{version}/odoo",
    "~/odoo/{version}/odoo",
    "~/odoo/{version}",
    "~/src/odoo/{version}/odoo",
    "~/src/odoo/{version}",
    "~/src/{version}/odoo",
    "~/work/odoo/{version}",
    "~/odoo-{version}",
    "~/odoo{version}",
)

SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".idea", ".vscode", "static"}
)


class OdooVersion:
    __slots__ = ("major", "minor")

    def __init__(self, major: int, minor: int = 0):
        self.major = major
        self.minor = minor

    @classmethod
    def parse(cls, text: str | None) -> "OdooVersion | None":
        if not text:
            return None
        m = re.match(r"^\s*(?:saas[~-])?(\d{2})\.(\d+)", str(text))
        if not m:
            return None
        major = int(m.group(1))
        if not 8 <= major <= 40:
            return None
        return cls(major, int(m.group(2)))

    @property
    def label(self) -> str:
        return f"{self.major}.0" if self.minor == 0 else f"saas-{self.major}.{self.minor}"

    def key(self) -> tuple[int, int]:
        return (self.major, self.minor)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OdooVersion) and self.key() == other.key()

    def __hash__(self) -> int:
        return hash(self.key())

    def __lt__(self, other: "OdooVersion") -> bool:
        return self.key() < other.key()

    def __le__(self, other: "OdooVersion") -> bool:
        return self.key() <= other.key()

    def __repr__(self) -> str:
        return f"OdooVersion({self.label})"

    def __str__(self) -> str:
        return self.label


_RELEASE_RE = re.compile(r"^version_info\s*=\s*\(\s*([^,]+),\s*(\d+)", re.M)


def read_release_version(community_root: str) -> OdooVersion | None:
    try:
        with open(os.path.join(community_root, "odoo", "release.py"), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    m = _RELEASE_RE.search(text)
    if not m:
        return None
    first, minor = m.group(1).strip().strip("'\""), int(m.group(2))
    mm = re.match(r"(?:saas~)?(\d+)", first)
    if not mm:
        return None
    major = int(mm.group(1))
    # version_info = (18, 0, ...) or ('saas~19', 2, ...)
    return OdooVersion(major, minor if first.startswith("saas") else 0)


def is_community_root(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "odoo", "release.py")) and (
        os.path.isfile(os.path.join(path, "odoo-bin")) or os.path.isdir(os.path.join(path, "odoo", "addons", "base"))
    )


def is_module_dir(path: str) -> bool:
    return os.path.isfile(os.path.join(path, MANIFEST))


def is_addons_root(path: str, limit: int = 4000) -> bool:
    try:
        with os.scandir(path) as it:
            for i, entry in enumerate(it):
                if i >= limit:
                    break
                if entry.is_dir() and not entry.name.startswith(".") and is_module_dir(entry.path):
                    return True
    except OSError:
        pass
    return False


def module_root_of(path: str) -> str | None:
    d = path if os.path.isdir(path) else os.path.dirname(path)
    home = os.path.expanduser("~")
    while True:
        if is_module_dir(d):
            return d
        parent = os.path.dirname(d)
        if parent == d or d == home:
            return None
        d = parent


def community_addons_roots(community: str) -> list[str]:
    roots = [os.path.join(community, "odoo", "addons"), os.path.join(community, "addons")]
    return [r for r in roots if os.path.isdir(r)]


def sibling_addons_roots(community: str) -> list[str]:
    container = os.path.dirname(community)
    found = []
    try:
        names = sorted(os.listdir(container))
    except OSError:
        return found
    names.sort(key=lambda n: (n != "enterprise", n))
    for name in names:
        p = os.path.join(container, name)
        if p == community or name.startswith(".") or not os.path.isdir(p):
            continue
        if is_community_root(p):
            continue
        if is_addons_root(p):
            found.append(p)
    return found


def find_community_root(path: str) -> str | None:
    d = path if os.path.isdir(path) else os.path.dirname(path)
    home = os.path.expanduser("~")
    while True:
        if is_community_root(d):
            return d
        child = os.path.join(d, "odoo")
        if is_community_root(child):
            return child
        sib = os.path.join(os.path.dirname(d), "odoo")
        if sib != d and is_community_root(sib) and is_addons_root(d):
            return sib
        parent = os.path.dirname(d)
        if parent == d or d == home:
            return None
        d = parent


def find_config(path: str) -> tuple[str, dict] | None:
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while True:
        for name in CONFIG_NAMES:
            cfg = os.path.join(d, name)
            if os.path.isfile(cfg):
                try:
                    with open(cfg, encoding="utf-8") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict):
                        return cfg, data
                except (OSError, ValueError) as exc:
                    log.warning("ignoring invalid %s: %s", cfg, exc)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def manifest_version(module_dir: str) -> OdooVersion | None:
    try:
        with open(os.path.join(module_dir, MANIFEST), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r"""['"]version['"]\s*:\s*['"]([^'"]+)['"]""", text)
    if not m:
        return None
    raw = m.group(1)
    # "17.0.1.0.0" names its Odoo version; a short "1.0" / "0.1" does not
    if raw.count(".") < 2 and not raw.startswith("saas"):
        return None
    return OdooVersion.parse(raw)


def iter_module_dirs(root: str, max_depth: int = 3) -> Iterable[str]:
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        if is_module_dir(d):
            yield d
            continue
        if depth >= max_depth or (depth > 0 and is_community_root(d)):
            continue
        try:
            with os.scandir(d) as it:
                subs = [e.path for e in it if e.is_dir() and not e.name.startswith(".") and e.name not in SKIP_DIRS]
        except OSError:
            continue
        stack.extend((s, depth + 1) for s in sorted(subs, reverse=True))


def guess_version(module_dirs: Iterable[str]) -> OdooVersion | None:
    votes = Counter(v for v in (manifest_version(m) for m in module_dirs) if v is not None)
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: (kv[1], kv[0].key()))[0]


def source_templates() -> list[str]:
    extra = os.environ.get("ODOO_LENS_SOURCES")
    templates = list(DEFAULT_SOURCE_TEMPLATES)
    if extra:
        templates = [t for t in extra.split(os.pathsep) if t] + templates
    return templates


def locate_sources(version: OdooVersion) -> str | None:
    labels = [version.label]
    if version.minor:
        labels.append(f"saas~{version.major}.{version.minor}")
    for template in source_templates():
        for label in labels:
            cand = os.path.expanduser(template.format(version=label))
            if is_community_root(cand):
                found = read_release_version(cand)
                if found is None or found == version:
                    return os.path.normpath(cand)
    return None


def available_versions() -> list[tuple[OdooVersion, str]]:
    seen: dict[OdooVersion, str] = {}
    for template in source_templates():
        if "{version}" not in template:
            continue
        pattern = os.path.expanduser(template)
        base, _, rest = pattern.partition("{version}")
        parent = os.path.dirname(base) if not base.endswith(os.sep) else base.rstrip(os.sep)
        prefix = os.path.basename(base) if not base.endswith(os.sep) else ""
        try:
            names = os.listdir(parent)
        except OSError:
            continue
        for name in names:
            if prefix and not name.startswith(prefix):
                continue
            label = name[len(prefix) :]
            ver = OdooVersion.parse(label)
            if ver is None or ver in seen:
                continue
            cand = os.path.normpath(os.path.join(parent, name) + rest)
            if is_community_root(cand):
                seen[ver] = cand
    return sorted(seen.items(), key=lambda kv: kv[0].key(), reverse=True)


class Env:
    def __init__(
        self,
        community: str | None,
        addons_roots: list[str],
        version: OdooVersion | None,
        custom_roots: Iterable[str] = (),
        config: dict | None = None,
        config_path: str | None = None,
    ):
        self.community = os.path.normpath(community) if community else None
        roots: list[str] = []
        for r in addons_roots:
            r = os.path.normpath(r)
            if r not in roots:
                roots.append(r)
        self.addons_roots = roots
        self.version = version
        self.custom_roots = [os.path.normpath(r) for r in custom_roots]
        self.config = config or {}
        self.config_path = config_path
        ident = "\0".join([self.community or ""] + self.addons_roots)
        self.key = hashlib.sha1(ident.encode("utf-8", "surrogateescape")).hexdigest()[:16]
        self.registry: Any = None
        self.ready = threading.Event()
        self.indexing = False
        self.last_used = 0.0

    @property
    def framework_dir(self) -> str | None:
        return os.path.join(self.community, "odoo") if self.community else None

    @property
    def label(self) -> str:
        ver = self.version.label if self.version else "unknown version"
        where = self.community or (self.addons_roots[0] if self.addons_roots else "?")
        return f"Odoo {ver} ({where})"

    @property
    def diagnostics_enabled(self) -> bool:
        return bool(self.config.get("diagnostics", True))

    def contains(self, path: str) -> bool:
        return self.root_of(path) is not None

    def root_of(self, path: str) -> str | None:
        best = None
        for r in self.addons_roots + ([self.framework_dir] if self.framework_dir else []):
            if path == r or path.startswith(r + os.sep):
                if best is None or len(r) > len(best):
                    best = r
        return best

    def __repr__(self) -> str:
        return f"<Env {self.label} roots={len(self.addons_roots)}>"


def _cfg_paths(cfg_path: str, value: Any) -> list[str]:
    base = os.path.dirname(cfg_path)
    vals = value if isinstance(value, list) else [value] if value else []
    return [os.path.normpath(os.path.join(base, os.path.expanduser(str(v)))) for v in vals]


# sibling/extra addons roots with these names are Odoo sources; others are custom (diagnostics on open)
ODOO_SOURCE_NAMES = frozenset({"enterprise", "design-themes", "themes", "industry", "upgrade", "upgrade-util", "odoo"})


def env_from_parts(
    community: str | None,
    custom_roots: list[str],
    version: OdooVersion | None,
    extra_roots: list[str] = (),  # type: ignore[assignment]
    config: dict | None = None,
    config_path: str | None = None,
) -> Env:
    roots: list[str] = []
    custom_roots = list(custom_roots)
    if community:
        roots += community_addons_roots(community)
        siblings = sibling_addons_roots(community)
        roots += siblings
        custom_roots += [r for r in siblings if os.path.basename(r.rstrip(os.sep)) not in ODOO_SOURCE_NAMES]
        if version is None:
            version = read_release_version(community)
    roots += list(extra_roots)
    custom_roots += [r for r in extra_roots if os.path.basename(r.rstrip(os.sep)) not in ODOO_SOURCE_NAMES]
    roots += [r for r in custom_roots if r not in roots]
    return Env(community, roots, version, dict.fromkeys(custom_roots), config, config_path)


# envs are identity-stable per key (the server compares them with "is")
class EnvManager:
    def __init__(self) -> None:
        self.envs: dict[str, Env] = {}
        self._by_path_cache: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def _register(self, env: Env) -> Env:
        with self._lock:
            existing = self.envs.get(env.key)
            if existing is not None:
                return existing
            self.envs[env.key] = env
            self._by_path_cache.clear()
            return env

    def env_for_path(self, path: str) -> Env | None:
        path = os.path.normpath(path)
        d = os.path.dirname(path)
        with self._lock:
            if d in self._by_path_cache:
                key = self._by_path_cache[d]
                return self.envs.get(key) if key else None
        best, best_rank = None, (-1, 0.0)
        for env in list(self.envs.values()):
            r = env.root_of(path)
            if r is not None and (len(r), env.last_used) > best_rank:
                best, best_rank = env, (len(r), env.last_used)
        env = best or self.discover(path)
        with self._lock:
            self._by_path_cache[d] = env.key if env else None
        return env

    def discover(self, path: str, scan_root: str | None = None) -> Env | None:
        env = self._discover(path, scan_root)
        return self._register(env) if env is not None else None

    def _discover(self, path: str, scan_root: str | None = None) -> Env | None:
        found = find_config(path)
        cfg_path, cfg = found if found else (None, {})
        ev_community = os.environ.get("ODOO_LENS_ODOO")
        ev_addons = [p for p in os.environ.get("ODOO_LENS_ADDONS", "").split(os.pathsep) if p]
        ev_version = OdooVersion.parse(os.environ.get("ODOO_LENS_VERSION"))

        community = None
        extra: list[str] = []
        version = None
        if cfg_path:
            if cfg.get("odoo"):
                community = _cfg_paths(cfg_path, cfg["odoo"])[0]
            extra += _cfg_paths(cfg_path, cfg.get("addons"))
            version = OdooVersion.parse(cfg.get("version"))
        if community is None and ev_community:
            community = os.path.expanduser(ev_community)
        extra += [os.path.expanduser(p) for p in ev_addons]
        version = version or ev_version

        inside = find_community_root(path)
        if community is None and inside is not None:
            community = inside

        custom: list[str] = []
        module = module_root_of(path)
        if inside is None:
            if scan_root:
                scan_from = scan_root
            elif cfg_path:
                scan_from = os.path.dirname(cfg_path)
            else:
                scan_from = os.path.dirname(module) if module else None
            if scan_from:
                mods = list(iter_module_dirs(scan_from))
                by_root: dict[str, list[str]] = {}
                for m in mods:
                    by_root.setdefault(os.path.dirname(m), []).append(m)
                root_versions = {r: guess_version(ms) for r, ms in by_root.items()}
                if version is None and community is None:
                    own = os.path.dirname(module) if module else None
                    version = root_versions.get(own) or guess_version(mods)
                explicit = bool(cfg_path) and bool(cfg.get("odoo") or cfg.get("version"))
                for r, rv in root_versions.items():
                    # roots of other Odoo versions (e.g. two tickets side by side) belong to other envs
                    if explicit or rv is None or version is None or rv == version:
                        if r not in custom and r not in extra:
                            custom.append(r)
            if community is None and version is not None:
                community = locate_sources(version)
                if community is None:
                    log.warning("no Odoo %s sources found; set ODOO_LENS_ODOO or .odoo-lens.json", version)
            if community is None and version is None and module is not None:
                avail = available_versions()
                if avail:
                    version, community = avail[0]
                    log.info("no Odoo version detected for %s, defaulting to %s", path, version)
        if community is None and not custom and not extra:
            return None
        if community is not None and not is_community_root(community):
            log.warning("configured Odoo root %s is not an Odoo checkout", community)
            community = None
        return env_from_parts(community, custom, version, extra, cfg, cfg_path)

    # a container of several checkouts (~/odev/worktrees) yields none: envs found lazily per file
    def envs_for_workspace(self, folder: str) -> list[Env]:
        folder = os.path.normpath(folder)
        if find_community_root(folder) or find_config(folder):
            env = self.discover(folder, scan_root=folder)
            return [env] if env else []
        checkouts = set()
        try:
            children = [e.path for e in os.scandir(folder) if e.is_dir() and not e.name.startswith(".")]
        except OSError:
            children = []
        for child in children:
            for cand in (child, os.path.join(child, "odoo")):
                if is_community_root(cand):
                    checkouts.add(cand)
        if len(checkouts) > 1:
            return []
        if len(checkouts) == 1:
            env = self.discover(next(iter(checkouts)))
            return [env] if env else []
        for m in iter_module_dirs(folder, max_depth=3):
            env = self.discover(m, scan_root=folder)
            return [env] if env else []
        return []
