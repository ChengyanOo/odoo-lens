from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens import documents as D  # noqa: E402
from odoo_lens.envs import EnvManager, OdooVersion, guess_version, read_release_version  # noqa: E402
from odoo_lens.extract import extract_source  # noqa: E402
from odoo_lens.facts import (  # noqa: E402
    CLASSES,
    F_COMODEL,
    F_NAME,
    K_FIELDS,
    K_METHODS,
    K_MODEL,
    K_NAME,
    M_NAME,
    M_SUPER,
    MI_INHERIT,
    RECORDS,
    REFS,
)
from odoo_lens.protocol import Transport  # noqa: E402

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")


def fx(rel: str) -> str:
    return os.path.join(MINI, rel)


def read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class TransportTest(unittest.TestCase):
    def test_roundtrip_and_malformed(self):
        out = io.BytesIO()
        Transport(io.BytesIO(), out).write({"jsonrpc": "2.0", "id": 1, "result": "é"})
        data = out.getvalue()
        self.assertTrue(data.startswith(b"Content-Length: "))
        body = data.split(b"\r\n\r\n", 1)[1]
        self.assertEqual(int(data.split(b"\r\n")[0].split(b":")[1]), len(body))
        self.assertEqual(json.loads(body)["result"], "é")
        bad = b"Content-Length: 5\r\n\r\n{nope" + data
        t = Transport(io.BytesIO(bad), io.BytesIO())
        with self.assertRaises(ValueError):
            t.read()
        self.assertEqual(t.read()["id"], 1)
        self.assertIsNone(t.read())


class DocumentsTest(unittest.TestCase):
    def test_uri_roundtrip(self):
        for p in ("/home/dev/tickets/#123456/user/x.py", "/a b/é/~x/[1].xml"):
            self.assertEqual(D.uri_to_path(D.path_to_uri(p)), p)
        self.assertEqual(D.path_to_uri("/t/#1/a b.py"), "file:///t/%231/a%20b.py")

    def test_utf16(self):
        line = 'x = "é😀" + y'
        self.assertEqual(D.utf16_len(line[: line.index("y")]), 12)
        self.assertEqual(D.bytecol_to_utf16(line.encode(), line.encode().index(b"y")), 12)
        self.assertEqual(D.utf16_to_index(line, 12), line.index("y"))

    def test_incremental_change(self):
        text = "ab\ncdé😀f\ng"
        rng = {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 5}}
        self.assertEqual(D.apply_change(text, {"range": rng, "text": "X"}), "ab\ncdXf\ng")
        self.assertEqual(D.apply_change(text, {"text": "new"}), "new")

    def test_refresh_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.py")
            with open(path, "w") as fh:
                fh.write("a = 1\n")
            store = D.DocumentStore()
            store.open(D.path_to_uri(path), "a = 1\n", 1)
            self.assertFalse(store.refresh_from_disk(path))
            with open(path, "w") as fh:
                fh.write("a = 22\n")
            self.assertTrue(store.refresh_from_disk(path))
            self.assertEqual(store.text(path), "a = 22\n")


class EnvsTest(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(OdooVersion.parse("17.0.1.0.0").label, "17.0")
        self.assertEqual(OdooVersion.parse("saas~19.2").label, "saas-19.2")
        self.assertLess(OdooVersion.parse("19.0"), OdooVersion.parse("saas-19.1"))
        self.assertIsNone(OdooVersion.parse("1.0"))
        self.assertEqual(read_release_version(fx("odoo")), OdooVersion(18, 0))

    def test_discovery_side_by_side(self):
        env = EnvManager().env_for_path(fx("custom/my_module/models/sale_order.py"))
        self.assertIsNotNone(env)
        self.assertEqual(env.community, fx("odoo"))
        names = [os.path.relpath(r, MINI) for r in env.addons_roots]
        self.assertEqual(names, ["odoo/odoo/addons", "odoo/addons", "enterprise", "custom"])
        self.assertEqual(env.version, OdooVersion(18, 0))

    def test_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = os.path.join(tmp, "addons", "x_mod")
            os.makedirs(mod)
            with open(os.path.join(mod, "__manifest__.py"), "w") as fh:
                fh.write("{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['sale']}")
            with open(os.path.join(tmp, ".odoo-lens.json"), "w") as fh:
                json.dump({"odoo": fx("odoo")}, fh)
            env = EnvManager().env_for_path(os.path.join(mod, "__manifest__.py"))
            self.assertEqual(env.community, fx("odoo"))
            self.assertIn(os.path.join(tmp, "addons"), env.addons_roots)
            self.assertEqual(guess_version([mod]), OdooVersion(18, 0))


class ExtractTest(unittest.TestCase):
    def test_python_facts(self):
        path = fx("odoo/addons/sale/models/sale_order.py")
        f = extract_source(path, "py", read(path))
        self.assertIsNone(f["error"])
        cls = f[CLASSES][0]
        self.assertEqual(cls[K_NAME], "SaleOrder")
        self.assertEqual(cls[K_MODEL][MI_INHERIT], ["mail.thread"])
        fields = {x[F_NAME]: x for x in cls[K_FIELDS]}
        self.assertEqual(fields["partner_id"][F_COMODEL], "res.partner")
        self.assertEqual(fields["order_line"][7]["inverse_name"], "order_id")
        methods = {m[M_NAME]: m for m in cls[K_METHODS]}
        self.assertTrue(methods["create"][M_SUPER])
        refs = {(r[0], r[1], r[5][0]) for r in f[REFS]}
        self.assertIn(("field", "order_line.price_subtotal", "depends"), refs)
        self.assertIn(("method", "_compute_amounts", "compute"), refs)
        self.assertIn(("model", "ir.sequence", "env"), refs)
        lines = read(path).decode().split("\n")
        for r in f[REFS]:
            self.assertEqual(lines[r[2]][r[3] : r[4]], r[1].split(",")[0] if r[0] != "field" or "." not in r[1] else r[1])

    def test_syntax_error(self):
        f = extract_source("/x.py", "py", b"def broken(:\n")
        self.assertIsNotNone(f["error"])

    def test_xml_facts_subpath(self):
        path = fx("odoo/addons/sale_stock/views/sale_order_views.xml")
        f = extract_source(path, "xml", read(path))
        refs = {(r[1], r[5][0], r[5][2] if len(r[5]) > 2 else None) for r in f[REFS] if r[0] == "field"}
        self.assertIn(("product_uom_qty", "arch", ("order_line",)), refs)
        self.assertIn(("product_id", "arch", ("order_line",)), refs)
        self.assertEqual(f[RECORDS][0][7]["inherit_id"], "sale.view_order_form")

    def test_xml_index_mode_drops_record_fields(self):
        path = fx("odoo/odoo/addons/base/data/res_company_data.xml")
        full = extract_source(path, "xml", read(path))
        idx = extract_source(path, "xml", read(path), index=True)
        self.assertTrue(any(r[0] == "field" and r[5][0] == "record" for r in full[REFS]))
        self.assertFalse(any(r[0] == "field" and r[5][0] == "record" for r in idx[REFS]))
        self.assertIn(("xmlid", "main_partner"), {(r[0], r[1]) for r in idx[REFS]})

    def test_csv_both_acl_formats(self):
        path = fx("odoo/odoo/addons/base/security/ir.model.access.csv")
        f = extract_source(path, "csv", read(path))
        self.assertEqual(f["model"], "ir.model.access")
        self.assertIn(("xmlid", "model_res_partner"), {(r[0], r[1]) for r in f[REFS]})
        v20 = b"id,name,model_id,group_id/id,operation,domain\nacc_x,x,sale.order,base.group_user,r,\"[('state', '=', 'sale')]\"\n"
        f = extract_source("/m/security/ir.access.csv", "csv", v20)
        kinds = {(r[0], r[1]) for r in f[REFS]}
        self.assertIn(("model", "sale.order"), kinds)
        self.assertIn(("xmlid", "base.group_user"), kinds)
        self.assertIn(("field", "state"), kinds)

    def test_manifest(self):
        path = fx("custom/my_module/__manifest__.py")
        f = extract_source(path, "manifest", read(path))
        self.assertEqual([d[0] for d in f["depends"]], ["sale", "unknown_module"])
        self.assertEqual(f["data"][1][0], "views/missing_file.xml")


class IndexAndRegistryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-core-")
        cls.root = os.path.join(cls.tmp, "mini")
        shutil.copytree(MINI, cls.root)
        os.environ["ODOO_LENS_CACHE_DIR"] = os.path.join(cls.tmp, "cache")
        from odoo_lens.indexer import build_index

        cls.env = EnvManager().env_for_path(os.path.join(cls.root, "odoo/addons/sale/__manifest__.py"))
        cls.reg, cls.stats = build_index(cls.env, workers=1, wait_for_cache_write=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def p(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def test_warm_index_uses_cache(self):
        from odoo_lens.indexer import build_index

        _, stats = build_index(self.env, workers=1)
        self.assertEqual(stats.extracted, 0)
        self.assertEqual(stats.cached, self.stats.files)

    def test_load_order_and_scope(self):
        r = self.reg
        ranks = {m.name: m.rank for m in r.modules()}
        self.assertEqual(ranks["base"], 0)
        self.assertLess(ranks["sale"], ranks["sale_stock"])
        self.assertLess(ranks["my_module"], ranks["sale_stock"])
        scope = r.scope_for_path(self.p("custom/my_module/models/sale_order.py"))
        self.assertIn("sale", scope)
        self.assertNotIn("sale_stock", scope)

    def test_mro_and_members(self):
        r = self.reg
        mro = r.model_mro("sale.order")
        self.assertEqual(mro[-1].name, "BaseModel")
        models = [c.model for c in mro if c.model]
        self.assertLess(models.index("sale.order"), models.index("mail.thread"))
        self.assertLess(models.index("mail.thread"), models.index("base"))
        defs = r.method("sale.order", "action_confirm")
        self.assertEqual(defs[-1].module, "sale")
        self.assertEqual({d.module for d in defs}, {"sale", "sale_stock", "sale_subscription", "my_module"})
        info = r.field_info("sale.order", "partner_id")
        self.assertEqual(info["comodel"], "res.partner")
        self.assertTrue(info["args"]["tracking"])
        self.assertTrue(r.field("sale.order", "create_date"))
        self.assertTrue(r.field("res.users", "email"))
        self.assertEqual(r.field("res.users", "email")[0].delegated_via, "partner_id")
        self.assertEqual([s for s, _, d in r.walk_path("sale.order", "partner_id.country_id.code") if d], ["partner_id", "country_id", "code"])

    def test_xmlids_and_views(self):
        r = self.reg
        self.assertEqual(r.records("base.main_company")[0].model, "res.company")
        self.assertTrue(r.records("sale_stock.model_sale_order"))  # implicit: one per module extending the model
        self.assertFalse(r.records("mail.model_sale_order"))
        self.assertTrue(r.records("sale.field_sale_order__partner_id"))
        views = {v.xmlid for v in r.views_of_model("sale.order")}
        self.assertIn("sale_stock.view_order_form_inherit_sale_stock", views)
        self.assertEqual(r.refs_to_xmlid("base.group_user")[0].kind, "xmlid")
        self.assertEqual(r.py_module_file("odoo.addons.sale.models.sale_order"), self.p("odoo/addons/sale/models/sale_order.py"))

    def test_incremental_update(self):
        r = self.reg
        path = self.p("odoo/addons/product/models/product.py")
        src = read(path)
        new = src.replace(b"active = fields.Boolean(default=True)", b"active = fields.Boolean(default=True)\n    barcode = fields.Char()")
        r.update_file(path, extract_source(path, "py", new))
        self.assertTrue(r.field("product.product", "barcode"))
        self.assertIn("product.product", r.models_with_member("barcode"))
        r.update_file(path, extract_source(path, "py", src))
        self.assertFalse(r.field("product.product", "barcode"))
        self.assertNotIn("product.product", r.models_with_member("barcode"))
        self.assertEqual(len(r.model_classes("product.product")), 1)

    def test_name_rules_per_version(self):
        from odoo_lens.registry import model_class_info

        cls = ("HrEmployeeExt", 0, 0, 0, 0, (), {"name": None, "inherit": ["hr.employee"], "inherits": {}, "kind": "Model", "attrs": {}, "inherit_str": False}, [], [], [], None)
        self.assertEqual(model_class_info(cls, 18)[0], "hr.employee")
        self.assertEqual(model_class_info(cls, 19)[0], "hr.employee.ext")
        cls_str = cls[:6] + (dict(cls[6], inherit_str=True),) + cls[7:]
        self.assertEqual(model_class_info(cls_str, 19)[0], "hr.employee")


if __name__ == "__main__":
    unittest.main()
