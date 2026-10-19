"""Benchmark the real server over stdio, the way Claude Code drives it.

    python3.12 scripts/bench.py [--versions 16.0,18.0,20.0] [--worktrees ~/odev/worktrees] [--cold]

For each version: spawn bin/odoo-lens with the checkout as workspace, measure
initialize, the first request (includes loading/building the index), then the
latency of each operation on well-known positions, and the server RSS.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from lsp_client import LspClient, path_to_uri  # noqa: E402

# (label, operation, relative file, needle, column offset inside the needle)
PROBES = [
    ("hover field", "textDocument/hover", "odoo/addons/sale/models/sale_order.py", "partner_id = fields.Many2one", 2),
    ("definition _inherit", "textDocument/definition", "odoo/addons/sale_stock/models/sale_order.py", "_inherit = 'sale.order'", 14),
    ("definition self.field", "textDocument/definition", "odoo/addons/sale/models/sale_order.py", "self.partner_id", 7),
    ("implementation method", "textDocument/implementation", "odoo/addons/sale/models/sale_order.py", "def action_confirm", 6),
    ("references method", "textDocument/references", "odoo/addons/sale/models/sale_order.py", "def action_confirm", 6),
    ("references field", "textDocument/references", "odoo/addons/sale/models/sale_order.py", "partner_id = fields.Many2one", 2),
    ("references model", "textDocument/references", "odoo/addons/sale_stock/models/sale_order.py", "_inherit = 'sale.order'", 14),
    ("documentSymbol", "textDocument/documentSymbol", "odoo/addons/sale/models/sale_order.py", None, 0),
    ("hover view field", "textDocument/hover", "odoo/addons/sale/views/sale_order_views.xml", 'name="partner_id"', 7),
]
QUERIES = ["sale.order", "sale.order.partner_id", "action_confirm", "sale.view_order_form", "account.move."]


def find(path: str, needle: str) -> tuple[int, int]:
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    for variant in (needle, needle.replace("'", '"')):
        for i, line in enumerate(lines):
            col = line.find(variant)
            if col >= 0:
                return i, col
    raise ValueError(f"{needle!r} not in {path}")


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 if out else 0.0


def count(result) -> int:
    if result is None:
        return 0
    return len(result) if isinstance(result, list) else 1


def bench(version: str, worktrees: str, cold: bool) -> None:
    root = os.path.join(worktrees, version)
    env = {}
    if cold:
        env["ODOO_LENS_CACHE_DIR"] = tempfile.mkdtemp(prefix="odoo-lens-bench-")
    client = LspClient(cwd=root, env=env)
    t = time.perf_counter()
    client.initialize(root)
    print(f"\n== {version}  initialize {1000 * (time.perf_counter() - t):.0f} ms")
    first = True
    for label, method, rel, needle, offset in PROBES:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            print(f"  {label:24s} (missing {rel})")
            continue
        client.open(path)
        t = time.perf_counter()
        if needle is None:
            res = client.request(method, {"textDocument": {"uri": path_to_uri(path)}})
        else:
            try:
                line, col = find(path, needle)
            except ValueError:
                print(f"  {label:24s} (needle not found)")
                continue
            extra = {"context": {"includeDeclaration": True}} if method.endswith("references") else {}
            res = client.at(method, path, line, col + offset, **extra)
        dt = 1000 * (time.perf_counter() - t)
        tag = " (incl. index load)" if first else ""
        first = False
        print(f"  {label:24s} {dt:8.1f} ms  results={count(res)}{tag}")
    for q in QUERIES:
        t = time.perf_counter()
        res = client.request("workspace/symbol", {"query": q})
        print(f"  {'symbol ' + q:24s} {1000 * (time.perf_counter() - t):8.1f} ms  results={count(res)}")
    print(f"  server RSS {rss_mb(client.proc.pid):.0f} MB")
    client.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", default="16.0,18.0,20.0")
    ap.add_argument("--worktrees", default=os.path.expanduser("~/odev/worktrees"))
    ap.add_argument("--cold", action="store_true", help="use an empty cache")
    args = ap.parse_args()
    for v in args.versions.split(","):
        bench(v.strip(), args.worktrees, args.cold)


if __name__ == "__main__":
    main()
