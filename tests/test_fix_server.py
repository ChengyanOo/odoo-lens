from __future__ import annotations

import io
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens import indexer  # noqa: E402
from odoo_lens.cli import NullEndpoint, load_env  # noqa: E402
from odoo_lens.envs import EnvManager  # noqa: E402
from odoo_lens.extract import extract_source  # noqa: E402
from odoo_lens.protocol import INVALID_REQUEST, Endpoint, Transport  # noqa: E402
from odoo_lens.server import OdooLanguageServer, _keeps_members  # noqa: E402

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")


def frame(*msgs: dict) -> bytes:
    out = b""
    for msg in msgs:
        body = json.dumps(msg).encode("utf-8")
        out += b"Content-Length: %d\r\n\r\n" % len(body) + body
    return out


def unframe(data: bytes) -> list[dict]:
    t = Transport(io.BytesIO(data), io.BytesIO())
    out = []
    while True:
        msg = t.read()
        if msg is None:
            return out
        out.append(msg)


class ShutdownTest(unittest.TestCase):
    def test_single_shutdown_flag(self):
        stdin = io.BytesIO(
            frame(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}},
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "shutdown"},
                {"jsonrpc": "2.0", "id": 3, "method": "workspace/symbol", "params": {"query": "x"}},
            )
        )
        stdout = io.BytesIO()
        endpoint = Endpoint(Transport(stdin, stdout))
        server = OdooLanguageServer(endpoint)
        self.assertNotIn("shutdown", endpoint._requests)
        self.assertNotIn("exit", endpoint._notifications)
        self.assertFalse(hasattr(server, "shutdown_requested"))
        endpoint.serve_forever()
        self.assertTrue(endpoint.shutdown_received)
        by_id = {m["id"]: m for m in unframe(stdout.getvalue()) if "id" in m}
        self.assertIsNone(by_id[2]["result"])
        self.assertEqual(by_id[3]["error"]["code"], INVALID_REQUEST)
        server._refresh_tick()
        try:
            self.assertEqual(endpoint._timers, {})
        finally:
            for timer in endpoint._timers.values():
                timer.cancel()

    def test_refresh_tick_without_protocol_endpoint(self):
        class Recording(NullEndpoint):
            def __init__(self):
                self.later = []

            def post_later(self, delay, key, fn, *args):
                self.later.append(key)

        endpoint = Recording()
        server = OdooLanguageServer(endpoint)  # type: ignore[arg-type]
        server._refresh_tick()
        self.assertEqual(endpoint.later, ["refresh"])


class QueueEndpoint(NullEndpoint):
    def __init__(self):
        self.posted: queue.Queue = queue.Queue()

    def post(self, fn, *args):
        self.posted.put((fn, args))


class Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"refresher read registry.{name}")


class RefreshSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-fix-server-")
        cls.root = os.path.join(cls.tmp, "mini")
        shutil.copytree(MINI, cls.root)
        cls._cache_env = os.environ.get("ODOO_LENS_CACHE_DIR")
        os.environ["ODOO_LENS_CACHE_DIR"] = os.path.join(cls.tmp, "cache")

    @classmethod
    def tearDownClass(cls):
        # shards are saved on a background thread: join it before restoring ODOO_LENS_CACHE_DIR
        for t in threading.enumerate():
            if t.name == "odoo-lens-cache-writer":
                t.join(30)
        if cls._cache_env is None:
            os.environ.pop("ODOO_LENS_CACHE_DIR", None)
        else:
            os.environ["ODOO_LENS_CACHE_DIR"] = cls._cache_env
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def p(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def drain(self, endpoint: QueueEndpoint, server: OdooLanguageServer) -> list:
        calls = []
        deadline = time.monotonic() + 30
        while server._refreshing:
            fn, args = endpoint.posted.get(timeout=max(0.1, deadline - time.monotonic()))
            calls.append((fn, args))
            fn(*args)
        return calls

    def test_sweep_uses_snapshots_and_applies_changes(self):
        endpoint = QueueEndpoint()
        server = OdooLanguageServer(endpoint)  # type: ignore[arg-type]
        server.initialized = True
        env, _ = load_env(server, self.p("odoo/addons/sale/__manifest__.py"), workers=1)
        reg = env.registry
        changed = self.p("custom/my_module/models/sale_order.py")
        gone = self.p("custom/my_module/views/sale_order_views.xml")
        self.assertIn(changed, reg.file_stats)
        self.assertIn(gone, reg.file_stats)
        with open(changed, "a") as fh:
            fh.write("\n\nclass Extra(models.Model):\n    _name = 'x.extra'\n")
        os.remove(gone)

        server._start_refresh([env])
        self.assertTrue(server._refreshing)
        ghost = self.p("custom/my_module/ghost.py")
        reg.file_stats[ghost] = (1, 1)
        calls = self.drain(endpoint, server)

        applies = [args for fn, args in calls if getattr(fn, "__name__", "") == "_apply_refresh"]
        self.assertEqual(len(applies), 1)
        _, applied_reg, _, todo, _, removed = applies[0]
        self.assertIs(applied_reg, reg)
        self.assertEqual([fe.path for fe in todo], [changed])
        self.assertEqual(removed, [gone])
        self.assertNotIn(gone, reg.file_stats)
        st = os.stat(changed)
        self.assertEqual(reg.file_stats[changed], (st.st_mtime_ns, st.st_size))
        self.assertTrue(reg.has_model("x.extra"))

    def test_background_thread_never_reads_registry(self):
        endpoint = QueueEndpoint()
        server = OdooLanguageServer(endpoint)  # type: ignore[arg-type]
        env = EnvManager().env_for_path(self.p("odoo/addons/sale/__manifest__.py"))
        sc = indexer.scan(env)
        stats = {fe.path: (fe.mtime, fe.size) for fe in sc.all_files()}
        stale = self.p("odoo/addons/sale/__manifest__.py")
        stats[stale] = (0, 0)
        stats[self.p("custom/my_module/old.py")] = (1, 1)
        server._refresh_bg([(env, Untouchable(), stats, dict(sc.modules))])
        posted = []
        while not endpoint.posted.empty():
            posted.append(endpoint.posted.get_nowait())
        applies = [args for fn, args in posted if getattr(fn, "__name__", "") == "_apply_refresh"]
        self.assertEqual(len(applies), 1)
        _, _, _, todo, _, removed = applies[0]
        self.assertEqual([fe.path for fe in todo], [stale])
        self.assertEqual(removed, [self.p("custom/my_module/old.py")])


class RemovedApisTest(unittest.TestCase):
    def test_refresh_index_removed(self):
        self.assertFalse(hasattr(indexer, "refresh_index"))

    def test_python_paths_key_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = os.path.join(tmp, "addons", "x_mod")
            os.makedirs(mod)
            with open(os.path.join(mod, "__manifest__.py"), "w") as fh:
                fh.write("{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['sale']}")
            with open(os.path.join(tmp, ".odoo-lens.json"), "w") as fh:
                json.dump({"odoo": os.path.join(MINI, "odoo"), "python_paths": ["lib"]}, fh)
            env = EnvManager().env_for_path(os.path.join(mod, "__manifest__.py"))
            self.assertIsNotNone(env)
            self.assertEqual(env.community, os.path.join(MINI, "odoo"))
            self.assertFalse(hasattr(env, "python_paths"))


class KeepsMembersTest(unittest.TestCase):
    def test_field_and_method_names(self):
        src = b"from odoo import fields, models\n\nclass A(models.Model):\n    _name = 'a.a'\n    f = fields.Char()\n\n    def m(self):\n        pass\n"
        old = extract_source("/x/a.py", "py", src)
        self.assertTrue(_keeps_members(old, extract_source("/x/a.py", "py", src + b"    g = fields.Char()\n")))
        self.assertFalse(_keeps_members(old, extract_source("/x/a.py", "py", src.replace(b"    f = fields.Char()\n", b""))))
        self.assertFalse(_keeps_members(old, extract_source("/x/a.py", "py", src.replace(b"def m(", b"def n("))))


if __name__ == "__main__":
    unittest.main()
