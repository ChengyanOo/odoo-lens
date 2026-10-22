from __future__ import annotations

import argparse
import ast
import os
import random
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odoo_lens.analysis import python as pa  # noqa: E402
from odoo_lens.cli import headless_server, load_env  # noqa: E402

DEFAULT_MODULES = [
    "sale", "account", "stock", "purchase", "mrp", "project", "hr", "crm", "point_of_sale", "website_sale",
    "sale_subscription", "account_accountant", "helpdesk", "planning", "documents",
]
SKIP_DIRS = {"tests", "migrations", "upgrades", "upgrade", "static", "__pycache__"}
RS_BUILTIN = pa.RS_SLOTS | {"_origin", "ids", "_ids", "_name", "_context", "_cr", "_uid"}


def module_files(path: str) -> list[str]:
    out = []
    for d, dirs, files in os.walk(path):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        out += [os.path.join(d, f) for f in files if f.endswith(".py") and f != "__manifest__.py"]
    return sorted(out)


def text(f: pa._File, node: ast.AST) -> str:
    return pa._text(f, node) or "<multi-line>"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("checkout")
    ap.add_argument("modules", nargs="*")
    ap.add_argument("--samples", type=int, default=30)
    ap.add_argument("--all", action="store_true", help="every module of the env")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    server = headless_server()
    anchor = os.path.join(args.checkout, "odoo", "odoo", "release.py")
    t0 = time.perf_counter()
    env, _ = load_env(server, anchor if os.path.exists(anchor) else args.checkout)
    if env is None:
        print("no Odoo environment detected", file=sys.stderr)
        return 1
    reg = env.registry
    print(f"{env.label}: index loaded in {time.perf_counter() - t0:.1f}s")
    if args.all:
        names = sorted(m.name for m in reg.modules())
    else:
        names = [m for m in (args.modules or DEFAULT_MODULES) if reg.module(m) is not None]
    print("modules:", " ".join(names))

    total = rs = resolved = mixin = 0
    unknown_fallback: Counter = Counter()
    unresolved: list[str] = []
    unknown: list[str] = []
    files = 0
    t1 = time.perf_counter()
    for name in names:
        for path in module_files(reg.module(name).path):
            try:
                with open(path, "rb") as fh:
                    src = fh.read()
            except OSError:
                continue
            pa._use_registry(reg)
            an = pa._analyzer(reg, path, src)
            an.reset_budget()
            f = an.f
            if f.tree is None:
                continue
            files += 1
            for ci in f.classes:
                if not ci.model:
                    continue
                for stmt in ci.node.body:
                    if not isinstance(stmt, pa._FUNC):
                        continue
                    an.reset_budget()
                    u = f.unit(stmt, ci)
                    for node in u.attrs:
                        attr = node.attr
                        if attr.startswith("__"):
                            continue
                        if pa._is_super_call(node.value):
                            total += 1
                            rs += 1
                            resolved += rs_ok(an, (ci.model,), attr)
                            continue
                        t = an.infer(node.value, u)
                        where = f"{os.path.relpath(path, args.checkout)}:{node.lineno}"
                        if t is not None and t[0] == "super":
                            t = ("rs", (t[1].model,)) if t[1].model else None
                        if t is not None and t[0] == "rs":
                            total += 1
                            rs += 1
                            if rs_ok(an, t[1], attr):
                                resolved += 1
                            elif heir_ok(an, t[1], attr):
                                mixin += 1
                            else:
                                unresolved.append(f"{where}  {text(f, node)}  receiver={list(t[1])}")
                            continue
                        if t is not None:
                            continue
                        if attr in pa.NON_MODEL_ATTRS or not [m for m in reg.models_with_member(attr) if m]:
                            continue
                        total += 1
                        cands, how = an.candidates(attr, node, u)
                        if not cands:
                            unknown_fallback["no candidate / too many"] += 1
                        elif len(cands) == 1:
                            unknown_fallback[f"1 candidate ({how})"] += 1
                        elif len(cands) <= pa.MAX_CANDIDATES:
                            unknown_fallback[f"2-{pa.MAX_CANDIDATES} candidates ({how})"] += 1
                        else:
                            unknown_fallback[f">{pa.MAX_CANDIDATES} candidates"] += 1
                        unknown.append(f"{where}  {text(f, node)}")
    dt = time.perf_counter() - t1
    print(f"{files} files analysed in {dt:.1f}s ({dt * 1000 / max(files, 1):.1f} ms/file)")
    print(f"record-attribute accesses: {total}")
    print(f"(a) receiver inferred as a recordset: {rs} = {100 * rs / max(total, 1):.1f}%")
    print(f"(b) resolved on the inferred model:   {resolved} = {100 * resolved / max(rs, 1):.2f}% of (a)")
    print(f"    + on models inheriting it (mixins): {mixin} -> {100 * (resolved + mixin) / max(rs, 1):.2f}% of (a)")
    print("unknown receivers, name-based fallback:")
    for k, v in unknown_fallback.most_common():
        print(f"    {k}: {v}")
    rnd = random.Random(args.seed)
    print(f"\n--- (b) unresolved samples ({len(unresolved)}):")
    for line in rnd.sample(unresolved, min(args.samples, len(unresolved))):
        print("   ", line)
    print(f"\n--- (a) unknown receiver samples ({len(unknown)}):")
    for line in rnd.sample(unknown, min(args.samples, len(unknown))):
        print("   ", line)
    return 0


def heir_ok(an: pa._Analyzer, models: tuple, attr: str) -> bool:
    reg = an.reg
    return any(
        m and any(base in an.lineage(m) for base in models)
        for m in reg.models_with_member(attr)
    )


def rs_ok(an: pa._Analyzer, models: tuple, attr: str) -> bool:
    if attr in RS_BUILTIN:
        return True
    return any(an.member(m, attr)[0] for m in models)


if __name__ == "__main__":
    sys.exit(main())
