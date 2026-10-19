"""End-to-end: drive bin/odoo-lens over stdio exactly like Claude Code 2.1.x does.

Claude Code sends the initialize params below verbatim, opens a file (didOpen
with the disk content) before any request on it, syncs edits with a full-text
didChange followed by didSave, never sends didClose/didChangeWatchedFiles and
only reads push diagnostics.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lsp_client import ROOT, LspClient, path_to_uri  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "mini")

CLAUDE_CODE_CAPS = {
    "workspace": {"configuration": False, "workspaceFolders": False, "didChangeWatchedFiles": {"dynamicRegistration": False}},
    "textDocument": {
        "synchronization": {"dynamicRegistration": False, "willSave": False, "willSaveWaitUntil": False, "didSave": True},
        "publishDiagnostics": {
            "relatedInformation": True,
            "tagSupport": {"valueSet": [1, 2]},
            "versionSupport": False,
            "codeDescriptionSupport": True,
            "dataSupport": False,
        },
        "hover": {"dynamicRegistration": False, "contentFormat": ["markdown", "plaintext"]},
        "definition": {"dynamicRegistration": False, "linkSupport": True},
        "references": {"dynamicRegistration": False},
        "documentSymbol": {"dynamicRegistration": False, "hierarchicalDocumentSymbolSupport": True},
        "callHierarchy": {"dynamicRegistration": False},
    },
    "general": {"positionEncodings": ["utf-16"]},
}


def find(path: str, needle: str, nth: int = 0) -> tuple[int, int]:
    """0-based (line, col) of the nth occurrence of ``needle`` in ``path``."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    seen = 0
    for i, line in enumerate(lines):
        col = line.find(needle)
        while col >= 0:
            if seen == nth:
                return i, col
            seen += 1
            col = line.find(needle, col + 1)
    raise AssertionError(f"{needle!r} not found in {path}")


def as_list(result):
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-e2e-")
        cls.root = os.path.join(cls.tmp, "mini")
        shutil.copytree(FIXTURE, cls.root)
        cls.cache = os.path.join(cls.tmp, "cache")
        cls.stderr = os.path.join(cls.tmp, "server.log")
        cls.client = LspClient(cwd=cls.root, env={"ODOO_LENS_CACHE_DIR": cls.cache}, stderr_path=cls.stderr)
        t = time.perf_counter()
        uri = path_to_uri(cls.root)
        cls.init = cls.client.request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "Claude Code", "version": "2.1.302"},
                "initializationOptions": {},
                "workspaceFolders": [{"uri": uri, "name": "mini"}],
                "rootPath": cls.root,
                "rootUri": uri,
                "capabilities": CLAUDE_CODE_CAPS,
            },
        )
        cls.init_ms = (time.perf_counter() - t) * 1000
        cls.client.notify("initialized", {})
        cls.sale = os.path.join(cls.root, "odoo/addons/sale/models/sale_order.py")
        cls.custom = os.path.join(cls.root, "custom/my_module/models/sale_order.py")
        cls.view = os.path.join(cls.root, "odoo/addons/sale/views/sale_order_views.xml")

    @classmethod
    def tearDownClass(cls):
        cls.exit_code = cls.client.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def request_at(self, method: str, path: str, needle: str, nth: int = 0, offset: int = 0, **extra):
        self.client.open(path)
        line, col = find(path, needle, nth)
        return self.client.at(method, path, line, col + offset, **extra)

    # -- lifecycle ----------------------------------------------------------------

    def test_initialize_fast_and_capabilities(self):
        self.assertLess(self.init_ms, 1500)
        caps = self.init["capabilities"]
        for key in ("definitionProvider", "referencesProvider", "hoverProvider", "documentSymbolProvider",
                    "workspaceSymbolProvider", "implementationProvider", "callHierarchyProvider"):
            self.assertTrue(caps.get(key), key)
        self.assertEqual(caps["positionEncoding"], "utf-16")

    # -- navigation -----------------------------------------------------------------

    def test_definition_of_inherited_model(self):
        res = as_list(self.request_at("textDocument/definition", self.custom, "'sale.order'", offset=3))
        self.assertTrue(res, "no definition")
        self.assertTrue(res[0]["uri"].endswith("odoo/addons/sale/models/sale_order.py"), res[0])

    def test_definition_of_field_through_chain(self):
        # company = self.env.ref('base.main_company'); company.partner_id.email
        res = as_list(self.request_at("textDocument/definition", self.custom, "partner_id.email", offset=12))
        self.assertTrue(any(r["uri"].endswith("base/models/res_partner.py") for r in res), res)

    def test_hover_field_is_markdown(self):
        res = self.request_at("textDocument/hover", self.sale, "partner_id = fields.Many2one", offset=2)
        self.assertIsNotNone(res)
        value = res["contents"]["value"] if isinstance(res["contents"], dict) else str(res["contents"])
        self.assertIn("res.partner", value)
        self.assertNotIn("range", res)

    def test_implementation_lists_overrides(self):
        res = as_list(self.request_at("textDocument/implementation", self.sale, "def action_confirm", offset=6))
        files = {r["uri"].rsplit("/", 4)[-4] for r in res}
        self.assertGreaterEqual(len(res), 3, res)
        self.assertTrue(any("sale_stock" in r["uri"] for r in res), files)

    def test_references_of_xmlid(self):
        res = as_list(self.request_at("textDocument/references", self.custom, "base.main_company", offset=6))
        self.assertTrue(any(r["uri"].endswith("res_company_data.xml") for r in res), res)

    def test_view_field_definition(self):
        res = as_list(self.request_at("textDocument/definition", self.view, 'name="product_id"', offset=7))
        self.assertTrue(any(r["uri"].endswith("sale_order_line.py") for r in res), res)

    def test_document_symbols_shape(self):
        self.client.open(self.sale)
        res = self.client.request("textDocument/documentSymbol", {"textDocument": {"uri": path_to_uri(self.sale)}})
        self.assertTrue(res)
        top = res[0]
        self.assertIn("range", top)
        self.assertTrue(top.get("children"))

    def test_workspace_symbol_query(self):
        res = self.client.request("workspace/symbol", {"query": "sale.order"})
        self.assertTrue(res)
        self.assertEqual(res[0]["name"], "sale.order")
        self.assertIn("range", res[0]["location"])

    # -- diagnostics policy -----------------------------------------------------------

    def test_diagnostics_published_for_custom_module_on_open(self):
        self.client.open(self.custom)
        diags = self.client.wait_diagnostics(self.custom, timeout=20)
        self.assertIsNotNone(diags, "no publishDiagnostics for a custom module file")
        codes = {d.get("code") for d in diags}
        self.assertTrue(codes & {"unknown-model", "unknown-field", "unknown-xmlid", "missing-dependency"}, diags)
        for d in diags:
            self.assertEqual(d.get("source"), "odoo-lens")
            self.assertIn(d.get("severity"), (1, 2, 3, 4))

    def test_no_diagnostics_for_navigation_into_core(self):
        core = os.path.join(self.root, "odoo/addons/sale_stock/models/sale_order.py")
        self.client.open(core)
        self.assertIsNone(self.client.wait_diagnostics(core, timeout=1.5))

    def test_edit_produces_diagnostics_and_stale_disk_is_reloaded(self):
        path = os.path.join(self.root, "odoo/addons/product/models/product.py")
        self.client.open(path)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        broken = text.replace("_inherit = ['mail.thread']", "_inherit = ['mail.threadd']")
        with open(path, "w", encoding="utf-8") as fh:  # Claude's Edit tool writes the file first...
            fh.write(broken)
        self.client.change(path, broken, 2)  # ...then sends didChange + didSave
        self.client.notify("textDocument/didSave", {"textDocument": {"uri": path_to_uri(path)}})
        diags = self.client.wait_diagnostics(path, timeout=20)
        self.assertTrue(diags and any(d.get("code") == "unknown-model" for d in diags), diags)
        # file fixed behind the server's back (git checkout): the next request must see it
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        line, col = find(path, "'mail.thread'")
        res = as_list(self.client.at("textDocument/definition", path, line, col + 3))
        self.assertTrue(any(r["uri"].endswith("mail/models/mail_thread.py") for r in res), res)


if __name__ == "__main__":
    unittest.main()
