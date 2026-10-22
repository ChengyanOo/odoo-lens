"""Tests for the command line interface (odoo_lens/cli.py): query ops, call
hierarchy contexts, symbols output limits and multi-env checks."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import weakref
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens import cli  # noqa: E402

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")
SALE = os.path.join(MINI, "odoo", "addons", "sale", "models", "sale_order.py")
SALE_STOCK = os.path.join(MINI, "odoo", "addons", "sale_stock", "models", "sale_order.py")
DISCOVERY_VARS = ("ODOO_LENS_ODOO", "ODOO_LENS_ADDONS", "ODOO_LENS_VERSION")


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def position(path: str, needle: str) -> tuple[int, int]:
    """1-based (line, col) of the first ``needle`` in ``path``, as the CLI takes it."""
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh.read().split("\n")):
            col = line.find(needle)
            if col >= 0:
                return i + 1, col + 1
    raise AssertionError(f"{needle!r} not found in {path}")


class TestQueryOps(unittest.TestCase):
    def test_targets_is_listed(self):
        self.assertIn("targets", cli.QUERY_OPS)
        self.assertIn("|calls-out|targets", cli.__doc__)

    def test_unknown_op_is_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            run("query", "nope", SALE, "1", "1")
        self.assertEqual(2, cm.exception.code)

    def test_targets_op(self):
        line, col = position(SALE_STOCK, "super().action_confirm")
        rc, out, _ = run("query", "targets", SALE_STOCK, str(line), str(col + len("super().")))
        self.assertEqual(0, rc)
        self.assertIn("action_confirm", out)


class TestCallHierarchy(unittest.TestCase):
    """calls-in / calls-out run on the item's definition, like the server."""

    def calls(self, op: str, path: str, needle: str, offset: int = 0) -> list[tuple[str, int]]:
        line, col = position(path, needle)
        rc, out, _ = run("query", op, path, str(line), str(col + offset), "--json")
        self.assertEqual(0, rc)
        key = "from" if op == "calls-in" else "to"
        return sorted((c[key]["uri"], c[key]["selectionRange"]["start"]["line"]) for c in json.loads(out))

    def test_calls_in_from_a_super_call_site(self):
        at_definition = self.calls("calls-in", SALE, "def action_confirm", len("def "))
        at_super_call = self.calls("calls-in", SALE_STOCK, "super().action_confirm", len("super()."))
        self.assertEqual(at_definition, at_super_call)
        # the super() callers (sale_stock's own override among them) are kept
        self.assertTrue(any(uri.endswith("/sale_stock/models/sale_order.py") for uri, _ in at_super_call))
        self.assertGreaterEqual(len(at_super_call), 3)

    def test_calls_out_from_a_super_call_site(self):
        at_definition = self.calls("calls-out", SALE, "def action_confirm", len("def "))
        at_super_call = self.calls("calls-out", SALE_STOCK, "super().action_confirm", len("super()."))
        self.assertEqual(at_definition, at_super_call)


class TestSymbolsLimit(unittest.TestCase):
    QUERY = "sale.order."

    def test_default_prints_the_whole_answer(self):
        rc, out, err = run("symbols", self.QUERY, "--path", MINI)
        self.assertEqual(0, rc)
        total = int(re.search(r"\((\d+) results", err).group(1))
        self.assertGreater(total, 3)
        self.assertEqual(total, len(out.splitlines()))
        self.assertNotIn("not shown", out)

    def test_limit_says_what_is_cut(self):
        rc, out, err = run("symbols", self.QUERY, "--path", MINI, "--limit", "3")
        self.assertEqual(0, rc)
        total = int(re.search(r"\((\d+) results", err).group(1))
        lines = out.splitlines()
        self.assertEqual(4, len(lines))
        self.assertEqual(f"({total - 3} more results not shown; raise --limit)", lines[-1])

    def test_limit_keeps_json_valid(self):
        rc, out, err = run("symbols", self.QUERY, "--path", MINI, "--limit", "3", "--json")
        self.assertEqual(0, rc)
        self.assertEqual(3, len(json.loads(out)))
        self.assertIn("more results not shown; raise --limit", err)


class TestCheckEnvs(unittest.TestCase):
    """``check <folder>`` checks each file against its own env."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="odoo-lens-cli-")
        cls._saved = {k: os.environ.get(k) for k in ("ODOO_LENS_CACHE_DIR", *DISCOVERY_VARS)}
        for k in DISCOVERY_VARS:
            os.environ.pop(k, None)
        # private cache: keep temp trees out of the shared (pruned) cache
        os.environ["ODOO_LENS_CACHE_DIR"] = os.path.join(cls._tmp.name, "cache")
        cls.root = os.path.join(cls._tmp.name, "tickets")
        for side in ("A", "B"):
            shutil.copytree(MINI, os.path.join(cls.root, side, "mini"))
        cls.stray = os.path.join(cls.root, "stray.py")
        with open(cls.stray, "w", encoding="utf-8") as fh:
            fh.write("print('not in any Odoo env')\n")

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls._tmp.cleanup()

    def check(self, *paths: str) -> tuple[int, list[str], str, int]:
        rc, out, err = run("check", *paths)
        n_files = int(re.search(r"diagnostics \(\d+ errors\) in (\d+) files", err).group(1))
        return rc, out.splitlines(), err, n_files

    def test_every_env_of_a_folder_is_checked(self):
        rc, lines, err, n_files = self.check(self.root)
        self.assertEqual(1, rc)
        for side in ("A", "B"):
            module = os.path.join(self.root, side, "mini", "custom", "my_module")
            self.assertTrue(any(line.startswith(module + os.sep) and "[unknown-module]" in line for line in lines), side)
        self.assertIn(f"skipped (no Odoo env): {self.stray}", err)
        # only the files actually checked are counted
        _, _, _, n_a = self.check(os.path.join(self.root, "A"))
        _, _, _, n_b = self.check(os.path.join(self.root, "B"))
        self.assertGreater(n_a, 0)
        self.assertEqual(n_a + n_b, n_files)

    def test_one_registry_at_a_time(self):
        """The registry of a checked env is released before the next env is built."""
        refs: list[weakref.ref] = []
        alive: list[list[bool]] = []
        index_env = cli.index_env

        def spy(env, workers=None):
            stats = index_env(env, workers)
            alive.append([r() is not None for r in refs])
            refs.append(weakref.ref(env.registry))
            return stats

        with mock.patch.object(cli, "index_env", spy):
            self.check(self.root)
        self.assertEqual([[], [False]], alive)

    def test_no_env_at_all(self):
        rc, out, err = run("check", self.stray)
        self.assertEqual(1, rc)
        self.assertEqual("", out)
        self.assertIn(f"skipped (no Odoo env): {self.stray}", err)
        self.assertIn("no Odoo environment detected", err)


if __name__ == "__main__":
    unittest.main()
