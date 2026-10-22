from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens.envs import EnvManager  # noqa: E402
from odoo_lens.extract import extract_source  # noqa: E402
from odoo_lens.facts import CLASSES, F_NAME, K_FIELDS, K_MODEL, M_SUPER, MI_INHERIT, MI_NAME, RECORDS, REFS  # noqa: E402

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def ref_text(src: bytes, ref: tuple) -> str:
    line = src.decode("utf-8").split("\n")[ref[2]]
    return line.encode("utf-16-le")[2 * ref[3] : 2 * ref[4]].decode("utf-16-le")


class ExtractorFixes(unittest.TestCase):
    def test_chained_name_and_name_reference(self):
        src = b"""from odoo import models
class A(models.Model):
    _name = _description = 'test.a'
class W(models.Model):
    _name = 'ir.module.module'
    _inherit = _name
class B(models._Base[int]):
    pass
"""
        f = extract_source("/m/x.py", "py", src)
        a, w, b = f[CLASSES]
        self.assertEqual(a[K_MODEL][MI_NAME], "test.a")
        self.assertEqual(w[K_MODEL][MI_INHERIT], ["ir.module.module"])
        self.assertEqual(b[5], ("models._Base",))

    def test_super_detection_is_per_method(self):
        src = b"""from odoo import models
class A(models.Model):
    _inherit = 'x.y'
    def write(self, vals):
        # super( mentioned in a comment
        return super().create(vals)
    def unlink(self):
        return super(A, self).unlink()
"""
        methods = {m[0]: m for m in extract_source("/m/x.py", "py", src)[CLASSES][0][8]}
        self.assertFalse(methods["write"][M_SUPER])
        self.assertTrue(methods["unlink"][M_SUPER])

    def test_setattr_keyword_field(self):
        src = b"""from odoo import fields, models
class IrRule(models.Model):
    _name = 'ir.rule'
global_ = fields.Boolean(compute='_compute_global', store=True)
setattr(IrRule, 'global', global_)
"""
        fields = [x[F_NAME] for x in extract_source("/m/x.py", "py", src)[CLASSES][0][K_FIELDS]]
        self.assertIn("global", fields)

    def test_xml_multiline_and_entity_positions(self):
        src = (
            b'<odoo><record id="g" model="res.groups">\n'
            b'  <field name="implied_ids" eval="[(4, ref(\'base.group_user\')),\n'
            b"      (4, ref('base.group_system'))]\"/>\n"
            b'</record><record id="v" model="ir.ui.view"><field name="arch" type="xml">\n'
            b"<form><field name=\"a\" invisible=\"state != 'x' or amount &gt;= 1\"/></form></field></record></odoo>"
        )
        f = extract_source("/m/views/x.xml", "xml", src)
        for r in f[REFS]:
            self.assertEqual(ref_text(src, r), r[1], r)
        values = {r[1] for r in f[REFS]}
        self.assertTrue({"base.group_user", "base.group_system", "state", "amount"} <= values)

    def test_xml_domain_code_and_metadata(self):
        src = b"""<odoo noupdate="1">
<record id="rule" model="ir.rule"><field name="model_id" ref="model_sale_order"/>
<field name="domain_force">[('company_id', 'in', company_ids)]</field></record>
<record id="cron" model="ir.cron"><field name="model_id" ref="model_sale_order"/>
<field name="code">model._cron_x()</field></record>
<record id="rep" model="ir.actions.report"><field name="report_name">sale.report_x</field></record>
<record id="v" model="ir.ui.view"><field name="priority" eval="20"/><field name="arch" type="xml">
<list><field name="partner_id" domain="[('is_company', '=', True)]"/><a type="object" name="act"/></list></field></record>
<template id="t" inherit_id="web.layout" primary="True"><div/></template>
</odoo>"""
        f = extract_source("/m/data/x.xml", "xml", src)
        roles = {(r[1], r[5][0]) for r in f[REFS]}
        self.assertIn(("company_id", "record_domain"), roles)
        self.assertIn(("_cron_x", "code"), roles)
        self.assertIn(("sale.report_x", "report"), roles)
        self.assertIn(("is_company", "arch_domain"), roles)
        self.assertIn(("act", "button"), roles)
        recs = {r[0]: r for r in f[RECORDS]}
        self.assertTrue(recs["rule"][7]["noupdate"])
        self.assertEqual(recs["rule"][7]["model_id"], "model_sale_order")
        self.assertEqual(recs["v"][7]["priority"], 20)
        self.assertEqual(recs["v"][7]["type"], "list")
        self.assertEqual(recs["t"][7]["mode"], "primary")

    def test_csv_quoted_cells(self):
        src = b'id,name,model_id,group_id/id,operation,domain\r\nacc,"a, ""b""",sale.order,,r,"[(\'state\', \'=\', \'sale\')]"\r\n'
        f = extract_source("/m/security/ir.access.csv", "csv", src)
        self.assertEqual(f[RECORDS][0][7]["name"], 'a, "b"')
        self.assertIn(("field", "state"), {(r[0], r[1]) for r in f[REFS]})


class RegistryFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-review-")
        cls.root = os.path.join(cls.tmp, "mini")
        shutil.copytree(MINI, cls.root)
        os.environ["ODOO_LENS_CACHE_DIR"] = os.path.join(cls.tmp, "cache")
        mail = os.path.join(cls.root, "odoo/addons/mail/models")
        write(os.path.join(mail, "blacklist.py"), """from odoo import fields, models


class Blacklist(models.AbstractModel):
    _name = 'mail.thread.blacklist'
    _description = 'Blacklist Mixin'
    _inherit = ['mail.thread']

    is_blacklisted = fields.Boolean()


class Partner(models.Model):
    _name = 'res.partner'
    _inherit = ['mail.thread.blacklist', 'res.partner']
""")
        write(os.path.join(mail, "__init__.py"), "from . import mail_thread\nfrom . import blacklist\n")
        base = os.path.join(cls.root, "odoo/odoo/addons/base")
        write(os.path.join(base, "data/aaa_update.xml"), '<odoo><record id="main_partner" model="res.partner"><field name="email">x@y.z</field></record></odoo>\n')
        manifest = os.path.join(base, "__manifest__.py")
        with open(manifest, encoding="utf-8") as fh:
            text = fh.read()
        write(manifest, text.replace("'views/res_partner_views.xml',", "'views/res_partner_views.xml',\n        'data/aaa_update.xml',"))
        from odoo_lens.indexer import build_index

        cls.env = EnvManager().env_for_path(os.path.join(cls.root, "odoo/addons/sale/__manifest__.py"))
        cls.reg, _ = build_index(cls.env, workers=1, wait_for_cache_write=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_gc_stays_enabled(self):
        from odoo_lens.indexer import FileEntry, extract_files

        self.assertTrue(gc.isenabled())
        path = os.path.join(self.root, "odoo/addons/sale/models/sale_order.py")
        st = os.stat(path)
        extract_files([FileEntry(path, os.path.dirname(path), "x.py", "py", st.st_mtime_ns, st.st_size, "sale")], workers=1)
        self.assertTrue(gc.isenabled())

    def test_description_from_own_classes(self):
        self.assertEqual(self.reg.model_attr("res.partner", "_description"), "Contact")

    def test_mro_parent_before_own_name(self):
        mro = [(c.model, c.module) for c in self.reg.model_mro("res.partner")]
        # LastOrderedSet: the blacklist mixin comes before the older res.partner definition
        self.assertLess(mro.index(("mail.thread.blacklist", "mail")), mro.index(("res.partner", "base")))

    def test_records_in_manifest_order(self):
        recs = self.reg.records("base.main_partner")
        self.assertTrue(recs[0].path.endswith("res_company_data.xml"))

    def test_batch_update_rebuilds_once(self):
        reg = self.reg
        calls = []
        original = reg._rebuild
        reg._rebuild = lambda: (calls.append(1), original())  # type: ignore[method-assign]
        try:
            pkg = os.path.join(self.root, "custom/new_mod")
            changes = {}
            write(os.path.join(pkg, "__manifest__.py"), "{'name': 'n', 'depends': ['sale']}\n")
            write(os.path.join(pkg, "__init__.py"), "from . import models\n")
            write(os.path.join(pkg, "models/__init__.py"), "from . import a\nfrom . import b\n")
            write(os.path.join(pkg, "models/a.py"), "from odoo import models\nclass A(models.Model):\n    _name = 'n.a'\n")
            write(os.path.join(pkg, "models/b.py"), "from odoo import models\nclass B(models.Model):\n    _name = 'n.b'\n")
            for rel, kind in (("__manifest__.py", "manifest"), ("__init__.py", "py"), ("models/__init__.py", "py"), ("models/a.py", "py"), ("models/b.py", "py")):
                p = os.path.join(pkg, rel)
                with open(p, "rb") as fh:
                    changes[p] = extract_source(p, kind, fh.read())
            reg.update_files(changes, {**reg.module_dirs, "new_mod": pkg})
            self.assertEqual(len(calls), 1)
            self.assertTrue(reg.has_model("n.a") and reg.has_model("n.b"))
        finally:
            reg._rebuild = original  # type: ignore[method-assign]


class EnvFixes(unittest.TestCase):
    def test_tickets_of_different_versions_get_different_envs(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "A/user/m16/__manifest__.py"), "{'name': 'a', 'version': '16.0.1.0.0', 'depends': ['base']}")
            write(os.path.join(tmp, "B/user/m17/__manifest__.py"), "{'name': 'b', 'version': '17.0.1.0.0', 'depends': ['base']}")
            write(os.path.join(tmp, ".odoo-lens-sources"), "")
            os.environ["ODOO_LENS_SOURCES"] = os.path.join(MINI, "{version}-does-not-exist")
            try:
                m = EnvManager()
                e1 = m.env_for_path(os.path.join(tmp, "A/user/m16/__manifest__.py"))
                e2 = m.env_for_path(os.path.join(tmp, "B/user/m17/__manifest__.py"))
            finally:
                del os.environ["ODOO_LENS_SOURCES"]
            self.assertNotEqual(e1.key, e2.key)
            self.assertEqual([os.path.basename(os.path.dirname(r)) for r in e1.custom_roots], ["A"])
            self.assertEqual([os.path.basename(os.path.dirname(r)) for r in e2.custom_roots], ["B"])


class SessionHint(unittest.TestCase):
    def hint(self, folder: str) -> str:
        import argparse
        import contextlib
        import io

        from odoo_lens.cli import cmd_session_hint

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cmd_session_hint(argparse.Namespace(path=folder)), 0)
        return out.getvalue()

    def test_silent_outside_odoo(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "pkg/setup.py"), "")
            self.assertEqual(self.hint(tmp), "")
            self.assertEqual(self.hint(os.path.join(tmp, "missing")), "")

    def test_custom_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "user/m1/__manifest__.py"), "{'name': 'm1', 'version': '17.0.1.0.0'}")
            write(os.path.join(tmp, "user/m2/__manifest__.py"), "{'name': 'm2', 'version': '17.0.1.0.0'}")
            text = self.hint(tmp)
            self.assertIn("2 Odoo module(s) for Odoo 17.0", text)
            self.assertIn("odoo-lens:odoo-lens", text)
            self.assertIn(os.path.join(ROOT, "bin", "odoo-lens") + " check <module_dir>", text)

    def test_checkout(self):
        self.assertIn("checkout", self.hint(os.path.join(MINI, "odoo")))


class DiagnosticsFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from odoo_lens.cli import headless_server, load_env

        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-review-diag-")
        cls.root = os.path.join(cls.tmp, "mini")
        shutil.copytree(MINI, cls.root)
        os.environ["ODOO_LENS_CACHE_DIR"] = os.path.join(cls.tmp, "cache")
        models = os.path.join(cls.root, "custom/my_module/models")
        write(os.path.join(models, "forgotten.py"), "from odoo import fields, models\n\n\nclass F(models.Model):\n    _name = 'x.forgotten'\n    _description = 'F'\n")
        write(os.path.join(models, "gone.py"), "from odoo import fields, models\n\n\nclass G(models.Model):\n    _inherit = 'sale.subscription'\n\n    x_studio_note = fields.Char()\n    other = fields.Char(related='partner_id.x_studio_code')\n")
        write(os.path.join(models, "commented.py"), "from odoo import fields, models\n\n\nclass C(models.Model):\n    _inherit = 'sale.order'\n\n    commented_field = fields.Char()\n")
        write(os.path.join(models, "uses.py"), "from odoo import fields, models\n\n\nclass U(models.Model):\n    _inherit = 'sale.order'\n\n    mirror = fields.Char(related='commented_field')\n")
        write(os.path.join(models, "__init__.py"), "from . import sale_order\nfrom . import gone\n# from . import commented\nfrom . import uses\n")
        views = os.path.join(cls.root, "custom/my_module/views")
        bad = "<odoo><record id='v' model='ir.ui.view'><field name='model'>sale.order</field><field name='inherit_id' ref='sale.nope'/></record></odoo>\n"
        write(os.path.join(views, "disabled.xml"), bad)
        write(os.path.join(views, "fixture.xml"), bad)
        manifest = os.path.join(cls.root, "custom/my_module/__manifest__.py")
        with open(manifest, encoding="utf-8") as fh:
            text = fh.read()
        write(manifest, text.replace("'data': [", "'data': [\n        # 'views/disabled.xml',", 1))
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(models, "sale_order.py"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def diags(self, rel: str) -> list[dict]:
        from odoo_lens import features
        from odoo_lens.server import RequestContext

        path = os.path.join(self.root, rel)
        return features.diagnostics(RequestContext(self.server, self.env, self.env.registry, path))

    def test_not_imported(self):
        codes = [d["code"] for d in self.diags("custom/my_module/models/forgotten.py")]
        self.assertEqual(codes, ["not-imported"])

    def test_commented_import(self):
        ds = self.diags("custom/my_module/models/commented.py")
        self.assertEqual([d["code"] for d in ds], ["not-imported"])
        self.assertIn("its import is commented out in models/__init__.py:3", ds[0]["message"])

    def test_field_of_a_never_imported_file(self):
        ds = [d for d in self.diags("custom/my_module/models/uses.py") if d["code"] == "unknown-field"]
        self.assertEqual(len(ds), 1, ds)
        self.assertIn("declared in my_module/models/commented.py:7, a file its module never imports", ds[0]["message"])

    def test_unloaded_data_files(self):
        ds = self.diags("custom/my_module/views/disabled.xml")
        self.assertEqual([d["code"] for d in ds], ["not-loaded"])
        self.assertIn("commented out in __manifest__.py:", ds[0]["message"])
        self.assertEqual(self.diags("custom/my_module/views/fixture.xml"), [])

    def test_extension_only_model_and_studio_fields(self):
        ds = self.diags("custom/my_module/models/gone.py")
        msgs = [d["message"] for d in ds]
        self.assertTrue(any("sale.subscription" in m and "only extended" in m for m in msgs), msgs)
        self.assertFalse(any("x_studio" in m for m in msgs), msgs)


if __name__ == "__main__":
    unittest.main()
