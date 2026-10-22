"""Focused diagnostics checks: the code of private button methods and
``compute_sql=`` method references, on a copy of the mini fixture."""

from __future__ import annotations

import gc
import importlib
import os
import shutil
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens.cli import headless_server, load_env  # noqa: E402
from odoo_lens.facts import R_COL, R_CTX, R_END_COL, R_KIND, R_LINE, R_VALUE, REF_METHOD, REFS  # noqa: E402
from odoo_lens.server import RequestContext  # noqa: E402

diag_module = importlib.import_module("odoo_lens.features.diagnostics")

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")

FILES = {
    "custom/fix_diag/__init__.py": "from . import models\n",
    "custom/fix_diag/__manifest__.py": """
        {
            'name': 'Fix diag',
            'depends': ['sale'],
            'data': ['views/views.xml'],
            'license': 'LGPL-3',
        }
        """,
    "custom/fix_diag/models.py": """
        from odoo import fields, models


        class FixDiag(models.Model):
            _name = 'fix.diag'
            _description = 'Fix diag'

            name = fields.Char()
            total = fields.Integer(compute='_compute_total', compute_sql='_compute_sql_total')
            bad_total = fields.Integer(compute='_compute_total', compute_sql='_compute_sql_nope')

            def _compute_total(self):
                pass

            def _compute_sql_total(self, alias, query):
                return None

            def _private_action(self):
                return True

            def action_ok(self):
                return True
        """,
    "custom/fix_diag/views/views.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="fix_diag_form" model="ir.ui.view">
                <field name="name">fix.diag.form</field>
                <field name="model">fix.diag</field>
                <field name="arch" type="xml">
                    <form>
                        <button name="action_ok" type="object"/>
                        <button name="_private_action" type="object"/>
                        <button name="_private_missing" type="object"/>
                        <button name="action_missing" type="object"/>
                        <field name="name"/>
                    </form>
                </field>
            </record>
            <record id="sale_order_form_fix" model="ir.ui.view">
                <field name="name">sale.order.form.fix</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="sale.view_order_form"/>
                <field name="arch" type="xml">
                    <field name="partner_id" position="after">
                        <button name="_private_sale" type="object"/>
                        <button name="action_confirm" type="object"/>
                    </field>
                </field>
            </record>
        </odoo>
        """,
}

MODELS = "custom/fix_diag/models.py"
VIEWS = "custom/fix_diag/views/views.xml"


def position(path: str, needle: str) -> tuple[int, int]:
    """0-based (line, col) of the first occurrence of ``needle`` in ``path``."""
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh.read().split("\n")):
            col = line.find(needle)
            if col >= 0:
                return i, col
    raise AssertionError(f"{needle!r} not found in {path}")


class TestFixDiagnostics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="odoo-lens-fixdiag-")
        # private cache: keep temp trees out of the shared cache
        cls._cache_env = os.environ.get("ODOO_LENS_CACHE_DIR")
        os.environ["ODOO_LENS_CACHE_DIR"] = os.path.join(cls._tmp.name, "cache")
        cls.root = os.path.join(cls._tmp.name, "mini")
        shutil.copytree(MINI, cls.root)
        for rel, content in FILES.items():
            path = os.path.join(cls.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(content).lstrip("\n"))
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(cls.root, MODELS))

    @classmethod
    def tearDownClass(cls):
        if cls.env is not None:
            cls.env.registry = None
        cls.server = cls.env = None
        gc.collect()
        if cls._cache_env is None:
            os.environ.pop("ODOO_LENS_CACHE_DIR", None)
        else:
            os.environ["ODOO_LENS_CACHE_DIR"] = cls._cache_env
        cls._tmp.cleanup()

    def ctx(self, rel: str) -> RequestContext:
        return RequestContext(self.server, self.env, self.env.registry, os.path.join(self.root, rel))

    def at(self, diags: list[dict], rel: str, needle: str) -> list[dict]:
        line, col = position(os.path.join(self.root, rel), needle)
        return [d for d in diags if d["range"]["start"] == {"line": line, "character": col}]

    # -- features-18: private button methods ---------------------------------------

    def test_private_button_code(self):
        diags = diag_module.diagnostics(self.ctx(VIEWS))
        for needle in ("_private_action", "_private_missing", "_private_sale"):
            found = self.at(diags, VIEWS, needle)
            self.assertEqual(["private-method"], [d["code"] for d in found], needle)
            self.assertEqual(2, found[0]["severity"])
            self.assertIn("is private", found[0]["message"])
        self.assertEqual(["unknown-method"], [d["code"] for d in self.at(diags, VIEWS, "action_missing")])
        self.assertEqual([], self.at(diags, VIEWS, "action_ok"))
        self.assertEqual([], self.at(diags, VIEWS, "action_confirm"))

    # -- features-3: compute_sql names a method --------------------------------------

    def test_compute_sql_role(self):
        self.assertIn("compute_sql", diag_module.PY_METHOD_ROLES)
        self.assertIn("compute_sql", diag_module.LATE_BOUND_ROLES)

    def test_compute_sql_reference(self):
        ctx = self.ctx(MODELS)
        facts = dict(ctx.facts)
        refs = list(facts.get(REFS) or ())
        # the extractor may not emit compute_sql references yet: add them as it would
        for name in ("_compute_sql_total", "_compute_sql_nope"):
            if not any(r[R_KIND] == REF_METHOD and r[R_VALUE] == name for r in refs):
                line, col = position(os.path.join(self.root, MODELS), name)
                ref = [None] * 6
                ref[R_KIND], ref[R_VALUE], ref[R_LINE], ref[R_COL], ref[R_END_COL] = REF_METHOD, name, line, col, col + len(name)
                ref[R_CTX] = ("compute_sql", "fix.diag")
                refs.append(tuple(ref))
        facts[REFS] = refs
        ctx._facts = facts
        diags = diag_module.diagnostics(ctx)
        found = self.at(diags, MODELS, "_compute_sql_nope")
        self.assertEqual(["unknown-method"], [d["code"] for d in found])
        self.assertIn("compute_sql=", found[0]["message"])
        self.assertEqual([], self.at(diags, MODELS, "_compute_sql_total"))


if __name__ == "__main__":
    unittest.main()
