"""Regression tests of the analysis fixes: framework tables shared with the
extractor (CachedModel), field-declaration calls recognised like the facts
extractor, no name-based model pinned for BaseModel members in XML, and the
x2many sub-view tags shared with the XML extractor.

Run: ODOO_LENS_CACHE_DIR=... python3.12 -m unittest tests.test_fix_analysis -v
"""

from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from odoo_lens import features  # noqa: E402
from odoo_lens.analysis import python as pa  # noqa: E402
from odoo_lens.analysis import targets_at  # noqa: E402
from odoo_lens.analysis import xml as xa  # noqa: E402
from odoo_lens.analysis.targets import FIELD, METHOD  # noqa: E402
from odoo_lens.cli import _ctx, headless_server, load_env  # noqa: E402
from odoo_lens.extract import pyfacts, xmlfacts  # noqa: E402

MINI = os.path.join(HERE, "fixtures", "mini")
WORKTREES = os.path.expanduser(os.environ.get("ODOO_LENS_WORKTREES", "~/odev/worktrees"))

EXTRA_FILES = {
    "custom/my_fix/__manifest__.py": """
        {
            'name': 'My Fix',
            'version': '18.0.1.0.0',
            'depends': ['sale'],
            'data': ['views/views.xml'],
            'license': 'LGPL-3',
        }
    """,
    "custom/my_fix/__init__.py": """
        from . import models
    """,
    "custom/my_fix/models/__init__.py": """
        from . import probe
    """,
    "custom/my_fix/models/probe.py": """
        from odoo import fields, models
        from odoo.fields import Many2one
        from odoo.fields import Many2one as M2O


        class Probe(models.Model):
            _inherit = 'sale.order'

            x_bare_id = Many2one('res.partner', domain=lambda self: [('name', '=', 'bare')])
            x_alias_id = M2O(comodel_name='res.partner', domain=lambda self: [('name', '=', 'alias')])
            x_attr_id = fields.Many2one('res.partner', domain=lambda self: [('name', '=', 'attr')])
    """,
    "custom/my_fix/views/views.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="view_unknown_parent" model="ir.ui.view">
                <field name="name">unknown parent</field>
                <field name="inherit_id" ref="my_fix.no_such_view"/>
                <field name="arch" type="xml">
                    <xpath expr="//sheet" position="inside">
                        <button name="create" type="object"/>
                        <button name="action_confirm" type="object"/>
                        <field name="id"/>
                    </xpath>
                </field>
            </record>
        </odoo>
    """,
}


def find(path: str, needle: str, offset: int = 0) -> tuple[int, int]:
    """1-based (line, column) of the first occurrence of ``needle`` (+ offset)."""
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh.read().split("\n")):
            j = line.find(needle)
            if j >= 0:
                return i + 1, j + 1 + offset
    raise AssertionError(f"{needle!r} not found in {path}")


class SharedTablesTest(unittest.TestCase):
    def test_framework_tables_from_extractor(self):
        self.assertIs(pa.FRAMEWORK_CLASSES, pyfacts.FRAMEWORK_CLASSES)
        self.assertIs(pa.MODEL_BASES, pyfacts.MODEL_BASES)
        self.assertIn("CachedModel", pa.FRAMEWORK_CLASSES)

    def test_cached_model_class(self):
        tree = ast.parse("class CachedModel(Model):\n    pass\n\nclass Cache(models.CachedModel):\n    _name = 'x.cache'\n")
        self.assertEqual(pa._class_model(tree.body[0], 19, True), ("base", True))
        self.assertEqual(pa._class_model(tree.body[1], 19, False), ("x.cache", True))

    def test_subview_tags_from_extractor(self):
        self.assertIs(xa.SUBVIEW_TAGS, xmlfacts.SUBVIEW_TAGS)
        self.assertIn("card", xa.SUBVIEW_TAGS)


class FieldCallTest(unittest.TestCase):
    SRC = textwrap.dedent(
        """
        import odoo.fields
        import openerp.fields
        from odoo import fields
        from odoo.fields import Many2one
        from odoo.fields import Many2one as M2O
        from .fields_misc import Id
        from odoo.models import Model
        from odoo.tools import Helper

        a = fields.Many2one('res.partner')
        b = odoo.fields.Many2one('res.partner')
        c = openerp.fields.Many2one('res.partner')
        d = Many2one('res.partner')
        e = M2O('res.partner')
        f = Id()
        g = Model()
        h = Helper()
        i = Unknown()
        j = fields.first(x)
        """
    ).encode()
    EXPECTED = {
        "a": "Many2one",
        "b": "Many2one",
        "c": "Many2one",
        "d": "Many2one",
        "e": "Many2one",
        "f": "Id",
        "g": None,
        "h": None,
        "i": None,
        "j": None,
    }

    def calls(self, f: pa._File) -> dict[str, ast.Call]:
        return {n.targets[0].id: n.value for n in f.tree.body if isinstance(n, ast.Assign)}

    def test_parsed_module_scope(self):
        f = pa._File("/tmp/x/models/probe.py", self.SRC)
        got = {k: pa._field_class(call, f.module_scope) for k, call in self.calls(f).items()}
        self.assertEqual(got, self.EXPECTED)

    def test_facts_module_scope(self):
        # partial parses: the module scope is built from the IMPORTS facts
        f = pa._File("/tmp/x/models/probe.py", self.SRC)
        f.facts_module_scope(pyfacts.extract("/tmp/x/models/probe.py", self.SRC))
        got = {k: pa._field_class(call, f.module_scope) for k, call in self.calls(f).items()}
        self.assertEqual(got, self.EXPECTED)

    def test_comodel(self):
        tree = ast.parse("M2O('res.partner')\nM2O(comodel_name='res.users')\nChar('x')\n")
        calls = [n.value for n in tree.body]
        self.assertEqual(pa._field_comodel(calls[0], "Many2one"), "res.partner")
        self.assertEqual(pa._field_comodel(calls[1], "Many2one"), "res.users")
        self.assertIsNone(pa._field_comodel(calls[2], "Char"))


class MiniTreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-fix-analysis-")
        cls.root = os.path.join(cls.tmp, "mini")
        shutil.copytree(MINI, cls.root)
        for rel, content in EXTRA_FILES.items():
            path = os.path.join(cls.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(content).lstrip("\n"))
        old = os.environ.get("ODOO_LENS_NO_CACHE")
        os.environ["ODOO_LENS_NO_CACHE"] = "1"  # temporary root: keep it out of the shared cache
        try:
            cls.server = headless_server()
            cls.env, _ = load_env(cls.server, os.path.join(cls.root, "custom/my_fix/__manifest__.py"))
        finally:
            if old is None:
                os.environ.pop("ODOO_LENS_NO_CACHE", None)
            else:
                os.environ["ODOO_LENS_NO_CACHE"] = old
        assert cls.env is not None

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.registry = None
        cls.server = cls.env = None
        pa._FILES.clear()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ctx(self, rel: str, needle: str, offset: int = 0):
        path = os.path.join(self.root, rel)
        line, col = find(path, needle, offset)
        return _ctx(self.server, self.env, path, line, col)

    def at(self, rel: str, needle: str, offset: int = 0) -> list:
        return targets_at(self.ctx(rel, needle, offset))

    # -- field declarations by bare imported class name ------------------------

    PROBE = "custom/my_fix/models/probe.py"

    def test_lambda_domain_of_bare_field_class(self):
        for needle in ("('name', '=', 'bare')", "('name', '=', 'alias')", "('name', '=', 'attr')"):
            res = self.at(self.PROBE, needle, 3)
            self.assertEqual([(t.kind, t.model, t.name) for t in res], [(FIELD, "res.partner", "name")], needle)

    # -- XML: unknown view model ----------------------------------------------

    VIEWS = "custom/my_fix/views/views.xml"

    def test_base_member_not_pinned(self):
        # create is a BaseModel method (overridden by sale.order): no basis to pick a model
        self.assertEqual(self.at(self.VIEWS, 'name="create"', 7), [])

    def test_magic_field_not_pinned(self):
        self.assertEqual(self.at(self.VIEWS, 'name="id"', 7), [])

    def test_single_name_candidate_is_flagged(self):
        res = self.at(self.VIEWS, 'name="action_confirm"', 7)
        self.assertEqual([(t.kind, t.model, t.name) for t in res], [(METHOD, "sale.order", "action_confirm")])
        self.assertEqual(res[0].extra.get("guess"), "member-name")
        md = features.hover(self.ctx(self.VIEWS, 'name="action_confirm"', 7))["contents"]["value"]
        self.assertIn("guessed", md)


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "saas-19.4", "odoo", "odoo", "orm")), "saas-19.4 checkout not available")
class CachedModelRealTest(unittest.TestCase):
    def test_cached_model_is_analysed_as_base(self):
        root = os.path.join(WORKTREES, "saas-19.4")
        server = headless_server()
        env, _ = load_env(server, os.path.join(root, "odoo/addons/sale/models/sale_order.py"))
        try:
            path = os.path.join(root, "odoo/odoo/orm/models_cached.py")
            line, col = find(path, "self.check_field_access", 6)
            res = targets_at(_ctx(server, env, path, line, col))
            self.assertEqual([(t.kind, t.model, t.name) for t in res], [(METHOD, "base", "check_field_access")])
        finally:
            env.registry = None
            pa._FILES.clear()


if __name__ == "__main__":
    unittest.main()
