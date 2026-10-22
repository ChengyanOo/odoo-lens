"""Precision sweep: run diagnostics over every file of an Odoo checkout."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odoo_lens.cli import headless_server, load_env  # noqa: E402
from odoo_lens.features.diagnostics import diagnostics  # noqa: E402
from odoo_lens.server import RequestContext  # noqa: E402

KINDS = ("py", "xml", "csv", "manifest")
SEVERITIES = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("checkout")
    parser.add_argument("--modules", help="comma-separated module names to restrict the sweep to")
    parser.add_argument("--limit", type=int, default=0, help="check at most N files")
    parser.add_argument("--samples", type=int, default=8, help="samples printed per code")
    parser.add_argument("--codes", help="only print samples of these codes")
    parser.add_argument("--out", help="write all diagnostics as JSON lines to this file")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.checkout)
    server = headless_server()
    t0 = time.perf_counter()
    env, stats = load_env(server, root)
    if env is None:
        print(f"no Odoo environment found for {root}", file=sys.stderr)
        return 1
    reg = env.registry
    print(f"{env.label}: {stats.summary() if stats else ''} (loaded in {time.perf_counter() - t0:.1f}s)")

    wanted = set(args.modules.split(",")) if args.modules else None
    files = []
    for path, facts in reg.iter_files():
        if not path.startswith(root + os.sep):
            continue
        module = reg.module_of_path(path)
        if module is None or (wanted is not None and module not in wanted):
            continue
        files.append((path, facts))
    files.sort()
    if args.limit:
        files = files[: args.limit]

    by_code: Counter = Counter()
    by_code_sev: Counter = Counter()
    by_module: Counter = Counter()
    samples: dict[str, list[str]] = defaultdict(list)
    timings: list[tuple[float, str]] = []
    out = open(args.out, "w", encoding="utf-8") if args.out else None
    checked = 0
    t1 = time.perf_counter()
    for path, facts in files:
        ctx = RequestContext(server, env, reg, path)
        if ctx.kind not in KINDS:
            continue
        if ctx.kind in ("py", "manifest"):
            # py/manifest index facts are complete (index=True only trims XML/CSV): skip re-extraction
            ctx._facts = facts
        t = time.perf_counter()
        diags = diagnostics(ctx)
        timings.append((time.perf_counter() - t, path))
        checked += 1
        for d in diags:
            code = d["code"]
            by_code[code] += 1
            by_code_sev[(code, d["severity"])] += 1
            by_module[ctx.module] += 1
            start = d["range"]["start"]
            line = f"{os.path.relpath(path, root)}:{start['line'] + 1}:{start['character'] + 1}: {SEVERITIES.get(d['severity'])}: {d['message']}"
            samples[code].append(line)
            if out is not None:
                out.write(json.dumps({"path": path, **d}, ensure_ascii=False) + "\n")
    if out is not None:
        out.close()
    elapsed = time.perf_counter() - t1

    total = sum(by_code.values())
    print(f"\n{total} diagnostics in {checked} files ({elapsed:.1f}s, {elapsed / max(checked, 1) * 1000:.1f} ms/file)")
    for code, n in by_code.most_common():
        sev = ", ".join(f"{SEVERITIES[s]} {c}" for (k, s), c in sorted(by_code_sev.items()) if k == code)
        print(f"  {code:20s} {n:6d}  ({sev})")
    if by_module:
        print("\nnoisiest modules: " + ", ".join(f"{m} {n}" for m, n in by_module.most_common(15)))
    show = set(args.codes.split(",")) if args.codes else None
    for code, lines in sorted(samples.items()):
        if show is not None and code not in show:
            continue
        print(f"\n== {code} ({len(lines)})")
        step = max(1, len(lines) // args.samples) if args.samples else len(lines) + 1
        for line in lines[::step][: args.samples]:
            print("  " + line)
    timings.sort(reverse=True)
    print("\nslowest files: " + ", ".join(f"{os.path.relpath(p, root)} {t * 1000:.0f} ms" for t, p in timings[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
