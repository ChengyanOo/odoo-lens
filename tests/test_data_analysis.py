"""targets_at for XML / CSV / manifest / JS files, and the view-arch model
helpers (arch_ref_model / arch_ref_candidates).

Run: ODOO_LENS_CACHE_DIR=... python3.12 -m unittest tests.test_data_analysis -v
"""

from __future__ import annotations

import os
import shutil
import tempfile
import textwrap
import time
import unittest

from odoo_lens.analysis import targets_at
from odoo_lens.analysis.targets import FIELD, FILE, METHOD, MODEL, MODULE, PYSYM, TEMPLATE, XMLID
from odoo_lens.analysis.xml import arch_ref_candidates, arch_ref_model
from odoo_lens.cli import _ctx, headless_server, load_env
from odoo_lens.extract import extract_source
from odoo_lens.facts import R_CTX, R_KIND, R_VALUE, RECORDS, REF_FIELD, REFS, X_INFO

HERE = os.path.dirname(os.path.abspath(__file__))
MINI = os.path.join(HERE, "fixtures", "mini")
WORKTREES = os.path.expanduser("~/odev/worktrees")


def find(path: str, needle: str, nth: int = 0, offset: int = 0) -> tuple[int, int]:
    """1-based (line, column) of the ``nth`` occurrence of ``needle`` (+ offset)."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    seen = 0
    for i, line in enumerate(lines):
        start = 0
        while True:
            j = line.find(needle, start)
            if j < 0:
                break
            if seen == nth:
                return i + 1, j + 1 + offset
            seen += 1
            start = j + 1
    raise AssertionError(f"{needle!r} not found in {path}")


class _Base(unittest.TestCase):
    root = MINI
    server = None
    env = None

    @classmethod
    def load(cls, rel: str) -> None:
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(cls.root, rel))
        assert cls.env is not None, f"no env for {rel}"

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.env is not None:
            cls.env.registry = None  # free the index before the next class loads its own

    def at(self, rel: str, needle: str, nth: int = 0, offset: int = 0) -> list:
        path = os.path.join(self.root, rel)
        line, col = find(path, needle, nth, offset)
        return targets_at(_ctx(self.server, self.env, path, line, col))

    def one(self, rel: str, needle: str, nth: int = 0, offset: int = 0):
        res = self.at(rel, needle, nth, offset)
        self.assertEqual(len(res), 1, f"{needle!r}: {res}")
        return res[0]

    def assertTarget(self, target, kind: str, **attrs) -> None:
        self.assertEqual(target.kind, kind, target)
        for k, v in attrs.items():
            self.assertEqual(getattr(target, k), v, f"{k} of {target}")

    def facts(self, rel: str) -> tuple[str, dict]:
        path = os.path.join(self.root, rel)
        with open(path, "rb") as fh:
            src = fh.read()
        kind = "csv" if path.endswith(".csv") else "xml"
        return path, extract_source(path, kind, src)

    def ref(self, facts: dict, value: str, nth: int = 0) -> tuple:
        refs = [r for r in facts[REFS] if r[R_VALUE] == value]
        self.assertGreater(len(refs), nth, value)
        return refs[nth]


# ---------------------------------------------------------------------------
# tests/fixtures/mini


class MiniXmlTests(_Base):
    SALE = "odoo/addons/sale/views/sale_order_views.xml"
    STOCK = "odoo/addons/sale_stock/views/sale_order_views.xml"
    MINE = "custom/my_module/views/sale_order_views.xml"

    @classmethod
    def setUpClass(cls) -> None:
        cls.load(cls.SALE)

    def test_view_field(self):
        self.assertTarget(self.one(self.SALE, 'name="partner_id"', offset=7), FIELD, model="sale.order", name="partner_id")

    def test_subview_field(self):
        t = self.one(self.SALE, 'name="product_id"', offset=7)
        self.assertTarget(t, FIELD, model="sale.order.line", name="product_id")
        self.assertEqual(t.extra["role"], "arch")

    def test_button_method(self):
        self.assertTarget(self.one(self.SALE, 'name="action_confirm"', offset=8), METHOD, model="sale.order", name="action_confirm")

    def test_expression_field(self):
        self.assertTarget(self.one(self.SALE, "state != 'draft'", offset=1), FIELD, model="sale.order", name="state")

    def test_eval_ref_in_record_field(self):
        t = self.one(self.SALE, "base.main_company", offset=3)
        self.assertTarget(t, XMLID, xmlid="base.main_company")
        self.assertEqual(t.extra["role"], "eval")

    def test_menu_refs(self):
        self.assertTarget(self.one(self.SALE, 'parent="sale_menu_root"', offset=9), XMLID, xmlid="sale.sale_menu_root")
        self.assertTarget(self.one(self.SALE, 'action="action_orders"', offset=9), XMLID, xmlid="sale.action_orders")
        self.assertTarget(self.one(self.SALE, 'groups="base.group_user"', offset=9), XMLID, xmlid="base.group_user")

    def test_record_ids(self):
        t = self.one(self.SALE, 'id="view_order_form"', offset=5)
        self.assertTarget(t, XMLID, xmlid="sale.view_order_form")
        self.assertEqual(t.extra["role"], "id")
        self.assertTarget(self.one(self.SALE, 'id="sale_menu_root"', offset=5), XMLID, xmlid="sale.sale_menu_root")

    def test_model_refs(self):
        self.assertTarget(self.one(self.SALE, 'model="ir.ui.view"', offset=8), MODEL, model="ir.ui.view")
        self.assertTarget(self.one(self.SALE, ">sale.order<", offset=3), MODEL, model="sale.order")
        self.assertTarget(self.one(self.SALE, ">sale.order<", nth=1, offset=3), MODEL, model="sale.order")

    def test_record_level_fields(self):
        self.assertTarget(self.one(self.SALE, '<field name="name">', offset=14), FIELD, model="ir.ui.view", name="name")
        self.assertTarget(self.one(self.SALE, '<field name="res_model">', offset=14), FIELD, model="ir.actions.act_window", name="res_model")

    def test_lenient_columns(self):
        # on "<field" a few columns before the name, and just after the closing quote
        self.assertTarget(self.one(self.SALE, '<field name="partner_id"'), FIELD, name="partner_id")
        self.assertTarget(self.one(self.SALE, 'partner_id"/>', offset=12), FIELD, name="partner_id")
        # nothing meaningful within reach
        self.assertEqual(self.at(self.SALE, "<odoo>"), [])

    def test_inherit_id(self):
        t = self.one(self.STOCK, 'ref="sale.view_order_form"', offset=6)
        self.assertTarget(t, XMLID, xmlid="sale.view_order_form")
        self.assertEqual(t.extra["role"], "ref")

    def test_inherited_view_fields(self):
        self.assertTarget(self.one(self.STOCK, 'name="date_order"', offset=7), FIELD, model="sale.order", name="date_order")
        self.assertTarget(self.one(self.STOCK, 'name="picking_policy"', offset=7), FIELD, model="sale.order", name="picking_policy")
        self.assertTarget(self.one(self.STOCK, "@name='order_line'", offset=8), FIELD, model="sale.order", name="order_line")
        self.assertTarget(self.one(self.STOCK, "@name='product_id'", offset=8), FIELD, model="sale.order.line", name="product_id")
        self.assertTarget(self.one(self.STOCK, 'name="product_uom_qty"', offset=7), FIELD, model="sale.order.line", name="product_uom_qty")

    def test_unknown_field_keeps_view_model(self):
        self.assertTarget(self.one(self.MINE, 'name="no_such_field"', offset=7), FIELD, model="sale.order", name="no_such_field")
        self.assertTarget(self.one(self.MINE, 'name="x_note"', offset=7), FIELD, model="sale.order", name="x_note")

    def test_arch_ref_model(self):
        reg = self.env.registry
        path, facts = self.facts(self.SALE)
        self.assertEqual(arch_ref_model(reg, path, facts, self.ref(facts, "partner_id")), "sale.order")
        self.assertEqual(arch_ref_model(reg, path, facts, self.ref(facts, "product_id")), "sale.order.line")
        self.assertEqual(arch_ref_model(reg, path, facts, self.ref(facts, "action_confirm")), "sale.order")
        record_ref = next(r for r in facts[REFS] if r[R_CTX][0] == "record" and r[R_VALUE] == "res_model")
        self.assertEqual(arch_ref_model(reg, path, facts, record_ref), "ir.actions.act_window")
        self.assertIsNone(arch_ref_model(reg, path, facts, self.ref(facts, "base.main_company")))

    def test_arch_ref_model_unsaved_text(self):
        # facts of an edited (unsaved) text: the record is built from these facts
        reg = self.env.registry
        path = os.path.join(self.root, self.STOCK)
        with open(path, encoding="utf-8") as fh:
            text = fh.read().replace('<field name="picking_policy"/>', '<field name="picking_policy"/>\n<field name="amount_total"/>')
        facts = extract_source(path, "xml", text.encode())
        ref = self.ref(facts, "amount_total")
        self.assertEqual(arch_ref_model(reg, path, facts, ref, text), "sale.order")
        self.assertEqual(arch_ref_candidates(reg, path, facts, ref, text), ["sale.order"])

    def test_arch_ref_candidates(self):
        reg = self.env.registry
        path, facts = self.facts(self.STOCK)
        self.assertEqual(arch_ref_candidates(reg, path, facts, self.ref(facts, "product_uom_qty"))[0], "sale.order.line")
        self.assertEqual(arch_ref_candidates(reg, path, facts, self.ref(facts, "picking_policy")), ["sale.order"])
        path, facts = self.facts(self.MINE)
        self.assertEqual(arch_ref_candidates(reg, path, facts, self.ref(facts, "no_such_field")), ["sale.order"])

    def test_arch_ref_candidates_from_parent_arch(self):
        # a spec naming a field that only exists in a sub-view of the parent arch
        reg = self.env.registry
        path = os.path.join(self.root, self.MINE)
        with open(path, encoding="utf-8") as fh:
            text = fh.read().replace('<field name="partner_id" position="after">', '<field name="price_subtotal" position="after">')
        facts = extract_source(path, "xml", text.encode())
        cands = arch_ref_candidates(reg, path, facts, self.ref(facts, "price_subtotal"), text)
        self.assertEqual(cands[0], "sale.order.line")
        # the content inserted next to it follows the located node
        self.assertEqual(arch_ref_model(reg, path, facts, self.ref(facts, "x_note"), text), "sale.order.line")


class MiniCsvManifestTests(_Base):
    ACL = "odoo/odoo/addons/base/security/ir.model.access.csv"
    MANIFEST = "custom/my_module/__manifest__.py"

    @classmethod
    def setUpClass(cls) -> None:
        cls.load("odoo/addons/sale/__manifest__.py")

    def test_acl_xmlid_columns(self):
        t = self.one(self.ACL, "model_res_partner", offset=2)
        self.assertTarget(t, XMLID, xmlid="base.model_res_partner")
        self.assertEqual(t.extra["role"], "model_id:id")
        self.assertTrue(self.env.registry.records(t.xmlid))  # implicit model xml id
        self.assertTarget(self.one(self.ACL, ",group_user,", offset=3), XMLID, xmlid="base.group_user")
        self.assertTarget(self.one(self.ACL, "base.group_user", offset=6), XMLID, xmlid="base.group_user")

    def test_acl_header_and_ids(self):
        self.assertTarget(self.one(self.ACL, "perm_read", offset=2), FIELD, model="ir.model.access", name="perm_read")
        self.assertTarget(self.one(self.ACL, "model_id:id", offset=2), FIELD, model="ir.model.access", name="model_id")
        self.assertTarget(self.one(self.ACL, "access_res_partner_user", offset=4), XMLID, xmlid="base.access_res_partner_user")

    def test_manifest_depends(self):
        self.assertTarget(self.one(self.MANIFEST, "'sale'", offset=1), MODULE, name="sale")
        self.assertTarget(self.one(self.MANIFEST, "'unknown_module'", offset=3), MODULE, name="unknown_module")

    def test_manifest_data_files(self):
        t = self.one(self.MANIFEST, "views/sale_order_views.xml", offset=3)
        self.assertTarget(t, FILE, path=os.path.join(self.root, "custom/my_module/views/sale_order_views.xml"))
        self.assertTrue(t.extra["exists"])
        t = self.one(self.MANIFEST, "views/missing_file.xml", offset=3)
        self.assertTarget(t, FILE, path=os.path.join(self.root, "custom/my_module/views/missing_file.xml"))
        self.assertFalse(t.extra["exists"])
        t = self.one("odoo/odoo/addons/base/__manifest__.py", "security/ir.model.access.csv", offset=12)
        self.assertTarget(t, FILE, path=os.path.join(self.root, "odoo/odoo/addons/base/security/ir.model.access.csv"))

    def test_manifest_keys_are_not_targets(self):
        self.assertEqual(self.at(self.MANIFEST, "'license'", offset=2), [])


# ---------------------------------------------------------------------------
# synthetic tree: a copy of mini + modules exercising specs, JS, owl, assets


EXTRA_FILES = {
    "custom/my_views/__init__.py": "from . import models\n",
    "custom/my_views/__manifest__.py": """
        {
            'name': 'My views',
            'version': '18.0.1.0.0',
            'depends': ['sale_stock', 'web'],
            'data': [
                'views/sale_order_views.xml',
                'views/templates.xml',
                'security/ir.access.csv',
                'security/ir.rule.csv',
            ],
            'assets': {
                'web.assets_backend': [
                    'my_views/static/src/js/widget.js',
                    'my_views/static/src/**/*.xml',
                    ('include', 'web._assets_helpers'),
                    ('remove', 'web/static/src/core/registry.js'),
                    '/web/static/src/core/utils/hooks.js',
                    'web/static/src/missing/**/*.js',
                ],
            },
            'license': 'LGPL-3',
        }
        """,
    "custom/my_views/models/__init__.py": "from . import sale_order_line\n",
    "custom/my_views/models/sale_order_line.py": """
        from odoo import fields, models


        class SaleOrderLine(models.Model):
            _inherit = 'sale.order.line'

            sibling_line_ids = fields.One2many(related='order_id.order_line')
        """,
    "custom/my_views/views/sale_order_views.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="view_order_form_specs" model="ir.ui.view">
                <field name="name">sale.order.form.specs</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="sale.view_order_form"/>
                <field name="arch" type="xml">
                    <field name="product_id" position="after">
                        <field name="order_id"/>
                        <field name="display_name"/>
                    </field>
                    <xpath expr="//field[@name='order_line']//field[@name='price_subtotal']" position="before">
                        <field name="price_unit"/>
                    </xpath>
                    <xpath expr="//field[@name='order_line']" position="attributes">
                        <field name="bogus"/>
                        <attribute name="readonly">state != 'draft'</attribute>
                    </xpath>
                    <field name="product_uom_qty" position="after">
                        <field name="create_date"/>
                    </field>
                    <button name="action_confirm" position="after">
                        <button name="no_such_method" type="object"/>
                        <label for="action_confirm"/>
                    </button>
                    <button name="locate_only" position="replace"/>
                </field>
            </record>
            <record id="view_order_line_form" model="ir.ui.view">
                <field name="name">sale.order.line.form</field>
                <field name="model">sale.order.line</field>
                <field name="arch" type="xml">
                    <form>
                        <field name="sibling_line_ids">
                            <list>
                                <field name="product_id" attrs="{'invisible': [('parent.product_uom_qty', '=', 0)]}"/>
                            </list>
                        </field>
                    </form>
                </field>
            </record>
        </odoo>
        """,
    "custom/my_views/views/templates.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <template id="portal_layout" name="Portal layout">
                <div class="o_portal"><t t-out="0"/></div>
            </template>
            <template id="portal_page" name="Portal page">
                <t t-call="my_views.portal_layout"><span>hello</span></t>
                <t t-call="portal_layout"/>
            </template>
        </odoo>
        """,
    "custom/my_views/security/ir.access.csv": """
        id,name,model_id,group_id/id,operation,domain
        access_sale_order_x,sale.order x,sale.order,base.group_user,r,"[('partner_id.country_id', '=', False)]"
        """,
    "custom/my_views/security/ir.rule.csv": """
        id,name,model_id:id,domain_force
        rule_sale_order_x,sale.order rule,sale.model_sale_order,"[('partner_id', '!=', False)]"
        """,
    "custom/my_views/static/src/js/widget.js": """
        import { Component, useState } from "@odoo/owl";
        import { useService as useSvc } from "@web/core/utils/hooks";
        import { registry } from "@web/core/registry";
        import { _t } from "@web/core/l10n";
        import * as utils from "./utils";
        import Helper from "./helper";
        import { total } from "./utils";

        export class SaleWidget extends Component {
            static template = "my_views.SaleWidget";
            setup() {
                this.orm = useSvc("orm");
                this.state = useState({ label: _t("Label") });
                utils.formatAmount(total);
                new Helper();
            }
            async load() {
                const unknownLocal = 1;
                await this.orm.call("sale.order", "action_confirm", [[unknownLocal]]);
            }
        }

        registry.category("fields").add("sale_widget", SaleWidget);
        """,
    "custom/my_views/static/src/js/utils.js": """
        export function formatAmount(amount) {
            return amount;
        }
        const internalTotal = 0;
        export { internalTotal as total };
        """,
    "custom/my_views/static/src/js/helper.js": """
        export default class Helper {}
        """,
    "custom/my_views/static/src/js/widget.xml": """
        <?xml version="1.0" encoding="UTF-8"?>
        <templates xml:space="preserve">
            <t t-name="my_views.SaleWidget">
                <t t-call="web.Helper"/>
            </t>
            <t t-name="my_views.SaleWidget2" t-inherit="my_views.SaleWidget" t-inherit-mode="primary"/>
        </templates>
        """,
    "odoo/addons/web/__init__.py": "",
    "odoo/addons/web/__manifest__.py": """
        {
            'name': 'Web',
            'version': '1.0',
            'depends': ['base'],
            'license': 'LGPL-3',
        }
        """,
    "odoo/addons/web/static/src/core/utils/hooks.js": """
        export function useService(serviceName) {
            return serviceName;
        }
        """,
    "odoo/addons/web/static/src/core/registry.js": """
        class Registry {}
        export const registry = new Registry();
        """,
    "odoo/addons/web/static/src/core/l10n/index.js": """
        export * from "./translation";
        """,
    "odoo/addons/web/static/src/core/l10n/translation.js": """
        export function _t(source) {
            return source;
        }
        """,
    "odoo/addons/web/static/src/core/helper.xml": """
        <?xml version="1.0" encoding="UTF-8"?>
        <templates xml:space="preserve">
            <t t-name="web.Helper"><div/></t>
        </templates>
        """,
    "odoo/addons/web/static/lib/owl/owl.js": """
        var owl = (() => {
          var index_exports = {};
          __export(index_exports, {
            Component: () => Component,
            useState: () => useState
          });
          var Component = class {
          };
          function useState(state) {
            return state;
          }
          return index_exports;
        })();
        """,
}


class SyntheticTreeTests(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-data-")
        cls.root = os.path.join(cls.tmp, "mini")
        shutil.copytree(MINI, cls.root)
        for rel, content in EXTRA_FILES.items():
            path = os.path.join(cls.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(content).lstrip("\n"))
        old = os.environ.get("ODOO_LENS_NO_CACHE")
        os.environ["ODOO_LENS_NO_CACHE"] = "1"  # temporary roots: keep them out of the shared cache
        try:
            cls.load("custom/my_views/__manifest__.py")
        finally:
            if old is None:
                os.environ.pop("ODOO_LENS_NO_CACHE", None)
            else:
                os.environ["ODOO_LENS_NO_CACHE"] = old

    @classmethod
    def tearDownClass(cls) -> None:
        super().tearDownClass()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    VIEWS = "custom/my_views/views/sale_order_views.xml"
    JS = "custom/my_views/static/src/js/widget.js"

    # -- inheritance specs ---------------------------------------------------

    def test_spec_located_in_subview(self):
        # <field name="product_id" position="after">: product_id only exists in the order_line list
        self.assertTarget(self.one(self.VIEWS, 'name="product_id"', offset=7), FIELD, model="sale.order.line", name="product_id")
        self.assertTarget(self.one(self.VIEWS, 'name="order_id"', offset=7), FIELD, model="sale.order.line", name="order_id")
        # display_name exists on both models: the located node decides
        t = self.one(self.VIEWS, 'name="display_name"', offset=7)
        self.assertTarget(t, FIELD, model="sale.order.line", name="display_name")

    def test_xpath_descendant_steps(self):
        self.assertTarget(self.one(self.VIEWS, "@name='price_subtotal'", offset=8), FIELD, model="sale.order.line", name="price_subtotal")
        self.assertTarget(self.one(self.VIEWS, "@name='order_line'", offset=8), FIELD, model="sale.order", name="order_line")
        self.assertTarget(self.one(self.VIEWS, 'name="price_unit"', offset=7), FIELD, model="sale.order.line", name="price_unit")

    def test_attributes_spec_content_is_ignored(self):
        # children of a position="attributes" spec are not view nodes: no ref at all
        path, facts = self.facts(self.VIEWS)
        self.assertFalse([r for r in facts["refs"] if r[1] == "bogus"])
        self.assertEqual(self.at(self.VIEWS, 'name="bogus"', offset=7), [])

    def test_anchor_added_by_sibling_extension(self):
        # product_uom_qty is added inside the order_line list by sale_stock's extension
        self.assertTarget(self.one(self.VIEWS, 'name="product_uom_qty"', offset=7), FIELD, model="sale.order.line")
        self.assertTarget(self.one(self.VIEWS, 'name="create_date"', offset=7), FIELD, model="sale.order.line", name="create_date")

    def test_button_specs(self):
        self.assertTarget(self.one(self.VIEWS, 'name="action_confirm"', offset=7), METHOD, model="sale.order", name="action_confirm")
        self.assertTarget(self.one(self.VIEWS, 'name="no_such_method"', offset=7), METHOD, model="sale.order", name="no_such_method")

    def test_unlocatable_button_spec(self):
        # locates a button the parent arch does not show as a method call (type="action", special...)
        path, facts = self.facts(self.VIEWS)
        self.assertEqual(arch_ref_candidates(self.env.registry, path, facts, self.ref(facts, "locate_only")), [])
        self.assertEqual(self.at(self.VIEWS, 'name="locate_only"', offset=7), [])

    def test_label_for_button(self):
        self.assertTarget(self.one(self.VIEWS, 'for="action_confirm"', offset=6), METHOD, model="sale.order", name="action_confirm")

    def test_related_x2many_subview(self):
        self.assertTarget(self.one(self.VIEWS, 'name="product_id" attrs', offset=7), FIELD, model="sale.order.line", name="product_id")

    def test_attrs_parent_field(self):
        t = self.one(self.VIEWS, "parent.product_uom_qty", offset=9)
        self.assertTarget(t, FIELD, model="sale.order.line", name="product_uom_qty")
        self.assertTarget(self.one(self.VIEWS, "parent.product_uom_qty", offset=1), MODEL, model="sale.order.line")
        path, facts = self.facts(self.VIEWS)
        ref = self.ref(facts, "parent.product_uom_qty")
        self.assertEqual(arch_ref_model(self.env.registry, path, facts, ref), "sale.order.line")

    # -- QWeb / owl ----------------------------------------------------------

    def test_server_qweb_tcall(self):
        rel = "custom/my_views/views/templates.xml"
        self.assertTarget(self.one(rel, 't-call="my_views.portal_layout"', offset=9), XMLID, xmlid="my_views.portal_layout")
        self.assertTarget(self.one(rel, 't-call="portal_layout"', offset=9), XMLID, xmlid="my_views.portal_layout")
        self.assertTarget(self.one(rel, 'id="portal_page"', offset=5), XMLID, xmlid="my_views.portal_page")

    def test_owl_templates(self):
        rel = "custom/my_views/static/src/js/widget.xml"
        self.assertTarget(self.one(rel, 't-name="my_views.SaleWidget"', offset=9), TEMPLATE, xmlid="my_views.SaleWidget")
        self.assertTarget(self.one(rel, 't-call="web.Helper"', offset=9), TEMPLATE, xmlid="web.Helper")
        self.assertTarget(self.one(rel, 't-inherit="my_views.SaleWidget"', offset=12), TEMPLATE, xmlid="my_views.SaleWidget")

    # -- CSV -------------------------------------------------------------------

    def test_ir_access_csv(self):
        rel = "custom/my_views/security/ir.access.csv"
        self.assertTarget(self.one(rel, ",sale.order,", offset=2), MODEL, model="sale.order")
        self.assertTarget(self.one(rel, "base.group_user", offset=2), XMLID, xmlid="base.group_user")
        self.assertTarget(self.one(rel, "operation", offset=2), FIELD, model="ir.access", name="operation")
        self.assertTarget(self.one(rel, "partner_id.country_id", offset=2), FIELD, model="sale.order", name="partner_id")
        t = self.one(rel, "partner_id.country_id", offset=14)
        self.assertTarget(t, FIELD, model="res.partner", name="country_id")
        self.assertEqual(t.origin[2] - t.origin[1], len("country_id"))
        self.assertTarget(self.one(rel, "access_sale_order_x", offset=2), XMLID, xmlid="my_views.access_sale_order_x")

    def test_domain_on_implicit_model_xmlid(self):
        rel = "custom/my_views/security/ir.rule.csv"
        self.assertTarget(self.one(rel, "'partner_id'", offset=3), FIELD, model="sale.order", name="partner_id")
        self.assertTarget(self.one(rel, "sale.model_sale_order", offset=6), XMLID, xmlid="sale.model_sale_order")

    # -- manifest assets -------------------------------------------------------

    def test_manifest_assets(self):
        rel = "custom/my_views/__manifest__.py"
        web = os.path.join(self.root, "odoo/addons/web")
        mine = os.path.join(self.root, "custom/my_views")
        self.assertTarget(self.one(rel, "my_views/static/src/js/widget.js", offset=5), FILE, path=os.path.join(mine, "static/src/js/widget.js"))
        self.assertTarget(self.one(rel, "my_views/static/src/**/*.xml", offset=5), FILE, path=os.path.join(mine, "static/src"))
        self.assertTarget(self.one(rel, "web/static/src/core/registry.js", offset=5), FILE, path=os.path.join(web, "static/src/core/registry.js"))
        self.assertTarget(self.one(rel, "/web/static/src/core/utils/hooks.js", offset=5), FILE, path=os.path.join(web, "static/src/core/utils/hooks.js"))
        self.assertTarget(self.one(rel, "web/static/src/missing/**/*.js", offset=5), FILE, path=os.path.join(web, "static/src"))
        self.assertEqual(self.at(rel, "web._assets_helpers", offset=5), [])
        self.assertTarget(self.one(rel, "'sale_stock'", offset=2), MODULE, name="sale_stock")

    # -- JS ------------------------------------------------------------------

    def test_js_import_specifiers(self):
        web = os.path.join(self.root, "odoo/addons/web")
        self.assertTarget(self.one(self.JS, '"@web/core/utils/hooks"', offset=5), FILE, path=os.path.join(web, "static/src/core/utils/hooks.js"))
        self.assertTarget(self.one(self.JS, '"@odoo/owl"', offset=3), FILE, path=os.path.join(web, "static/lib/owl/owl.js"))
        self.assertTarget(self.one(self.JS, '"@web/core/l10n"', offset=5), FILE, path=os.path.join(web, "static/src/core/l10n/index.js"))
        self.assertTarget(self.one(self.JS, '"./utils"', offset=3), FILE, path=os.path.join(self.root, "custom/my_views/static/src/js/utils.js"))

    def test_js_imported_names(self):
        web = os.path.join(self.root, "odoo/addons/web")
        t = self.one(self.JS, "useSvc(", offset=2)  # renamed import, at a call site
        self.assertTarget(t, PYSYM, name="useService", path=os.path.join(web, "static/src/core/utils/hooks.js"), line=0)
        self.assertEqual((t.col, t.end_col), (16, 26))
        self.assertTarget(self.one(self.JS, "registry.category", offset=3), PYSYM, name="registry", path=os.path.join(web, "static/src/core/registry.js"), line=1)
        self.assertTarget(self.one(self.JS, "_t(", offset=1), PYSYM, name="_t", path=os.path.join(web, "static/src/core/l10n/translation.js"))
        t = self.one(self.JS, "extends Component", offset=10)
        self.assertTarget(t, PYSYM, name="Component", path=os.path.join(web, "static/lib/owl/owl.js"), line=6)
        self.assertTarget(self.one(self.JS, "useState(", nth=0, offset=2), PYSYM, name="useState", line=8)
        mine = os.path.join(self.root, "custom/my_views/static/src/js")
        self.assertTarget(self.one(self.JS, "utils.formatAmount", offset=8), PYSYM, name="formatAmount", path=os.path.join(mine, "utils.js"))
        self.assertTarget(self.one(self.JS, "new Helper", offset=6), PYSYM, name="default", path=os.path.join(mine, "helper.js"))
        t = self.one(self.JS, "formatAmount(total)", offset=14)  # export { internalTotal as total }
        self.assertTarget(t, PYSYM, name="total", path=os.path.join(mine, "utils.js"), line=3, col=6)

    def test_js_local_symbols_and_refs(self):
        self.assertTarget(self.one(self.JS, "sale_widget\", SaleWidget", offset=16), PYSYM, name="SaleWidget", line=8)
        self.assertTarget(self.one(self.JS, '"my_views.SaleWidget"', offset=3), TEMPLATE, xmlid="my_views.SaleWidget")
        self.assertTarget(self.one(self.JS, '"sale.order"', offset=3), MODEL, model="sale.order")
        self.assertTarget(self.one(self.JS, '"action_confirm"', offset=3), METHOD, model="sale.order", name="action_confirm")

    def test_js_unresolved_identifier_is_not_snapped(self):
        # exactly on a local variable: nothing, not the neighbouring import
        self.assertEqual(self.at(self.JS, "unknownLocal]]", offset=2), [])
        self.assertEqual(self.at(self.JS, "this.orm", offset=6), [])


# ---------------------------------------------------------------------------
# real Odoo trees


def _tree(version: str) -> str:
    return os.path.join(WORKTREES, version)


class _RealTree(_Base):
    version = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = _tree(cls.version)
        cls.load("odoo/addons/sale/__manifest__.py")

    def timed(self, rel: str, needle: str, nth: int = 0, offset: int = 0):
        t0 = time.perf_counter()
        res = self.at(rel, needle, nth, offset)
        return res, (time.perf_counter() - t0) * 1000

    def arch_precision(self, max_ratio: float) -> None:
        """Inherited-view <field name> refs whose candidates own the field."""
        reg = self.env.registry
        total = bad = 0
        for path, facts in reg.iter_files("xml"):
            recs = facts.get(RECORDS) or ()
            scope = reg.scope_for_path(path)
            for ref in facts.get(REFS) or ():
                rctx = ref[R_CTX]
                if rctx[0] != "arch" or ref[R_KIND] != REF_FIELD or not (recs[rctx[1]][X_INFO] or {}).get("inherit_id"):
                    continue
                cands = arch_ref_candidates(reg, path, facts, ref)
                if not cands:
                    continue
                total += 1
                if not any(reg.field(m, ref[R_VALUE], scope) or reg.field(m, ref[R_VALUE]) for m in cands):
                    bad += 1
        self.assertGreater(total, 1000)
        self.assertLessEqual(bad / total, max_ratio, f"{bad}/{total} inherited arch fields without an owning candidate")


@unittest.skipUnless(os.path.isdir(_tree("16.0")), "Odoo 16.0 sources not available")
class Real16Tests(_RealTree):
    version = "16.0"
    VIEWS = "odoo/addons/sale/views/sale_order_views.xml"

    def test_tree_subview_and_attrs(self):
        res, ms = self.timed(self.VIEWS, "<field name=\"product_uom_qty\"/>", offset=13)
        self.assertTarget(res[0], FIELD, model="sale.order.line", name="product_uom_qty")
        self.assertLess(ms, 100)
        self.assertTarget(self.one(self.VIEWS, "attrs=\"{'invisible': [('state', 'not in', ['sent'])]}\"", offset=17), FIELD, model="sale.order", name="state")
        self.assertTarget(self.one(self.VIEWS, "parent.state", offset=9), FIELD, model="sale.order", name="state")

    def test_model_access_csv(self):
        rel = "odoo/addons/sale/security/ir.model.access.csv"
        t = self.one(rel, "account.model_account_account", offset=10)
        self.assertTarget(t, XMLID, xmlid="account.model_account_account")
        self.assertTrue(self.env.registry.records(t.xmlid))
        self.assertTarget(self.one(rel, "perm_read", offset=2), FIELD, model="ir.model.access", name="perm_read")

    def test_inherited_arch_precision(self):
        self.arch_precision(0.002)


@unittest.skipUnless(os.path.isdir(_tree("18.0")), "Odoo 18.0 sources not available")
class Real18Tests(_RealTree):
    version = "18.0"

    def test_list_subview(self):
        rel = "odoo/addons/sale/views/sale_order_views.xml"
        res, ms = self.timed(rel, 'name="product_uom_qty"', nth=1, offset=7)
        self.assertTarget(res[0], FIELD, model="sale.order.line", name="product_uom_qty")
        self.assertLess(ms, 100)

    def test_inherited_view(self):
        rel = "odoo/addons/sale_stock/views/sale_order_views.xml"
        self.assertTarget(self.one(rel, 'ref="sale.view_order_form"', offset=6), XMLID, xmlid="sale.view_order_form")

    def test_inherited_arch_precision(self):
        self.arch_precision(0.002)


@unittest.skipUnless(os.path.isdir(_tree("20.0")), "Odoo 20.0 sources not available")
class Real20Tests(_RealTree):
    version = "20.0"
    JS = "odoo/addons/web/static/src/views/list/list_renderer.js"

    def test_ir_access_csv(self):
        rel = "odoo/addons/sales_team/security/ir.access.csv"
        self.assertTarget(self.one(rel, "crm.team,base.group_user", offset=2), MODEL, model="crm.team")
        self.assertTarget(self.one(rel, "base.group_user", offset=3), XMLID, xmlid="base.group_user")
        self.assertTarget(self.one(rel, "'company_id'", offset=3), FIELD, model="crm.team", name="company_id")
        self.assertTarget(self.one(rel, "model_id", offset=2), FIELD, model="ir.access", name="model_id")

    def test_owl_templates(self):
        rel = "odoo/addons/web/static/src/webclient/settings_form_view/settings_form_view.xml"
        self.assertTarget(self.one(rel, 't-inherit="web.FormView"', offset=12), TEMPLATE, xmlid="web.FormView")
        self.assertTarget(self.one(rel, 't-name="web.SettingsFormView"', offset=9), TEMPLATE, xmlid="web.SettingsFormView")
        self.assertTarget(self.one("odoo/addons/web/static/src/webclient/navbar/navbar.xml", 't-call="web.NavBar.AppsMenu"', offset=9), TEMPLATE, xmlid="web.NavBar.AppsMenu")

    def test_website_tcall(self):
        rel = "odoo/addons/website/views/website_templates.xml"
        t = self.one(rel, 't-call="website.layout"', offset=9)
        self.assertTarget(t, XMLID, xmlid="website.layout")
        self.assertTrue(self.env.registry.records("website.layout"))

    def test_js_imports(self):
        web = os.path.join(self.root, "odoo/addons/web")
        res, ms = self.timed(self.JS, '"@web/core/utils/hooks"', offset=3)
        self.assertTarget(res[0], FILE, path=os.path.join(web, "static/src/core/utils/hooks.js"))
        self.assertLess(ms, 100)
        res, ms = self.timed(self.JS, "useService }", offset=2)
        self.assertTarget(res[0], PYSYM, name="useService", path=os.path.join(web, "static/src/core/utils/hooks.js"))
        self.assertLess(ms, 100)
        self.assertTarget(self.one(self.JS, "Component,", offset=2), PYSYM, name="Component", path=os.path.join(web, "static/lib/owl/owl.js"))
        self.assertTarget(self.one(self.JS, '"web.ListRenderer"', offset=3), TEMPLATE, xmlid="web.ListRenderer")
        # re-exported name: export { evaluate } from "./py_interpreter"
        t = self.one("odoo/addons/web/static/src/core/py_js/py.js", "export { evaluate }", offset=10)
        self.assertTarget(t, PYSYM, name="evaluate", path=os.path.join(web, "static/src/core/py_js/py_interpreter.js"))

    def test_manifest(self):
        rel = "odoo/addons/sale/__manifest__.py"
        self.assertTarget(self.one(rel, '"sales_team"', offset=2), MODULE, name="sales_team")
        self.assertTarget(self.one(rel, "sale/static/src/js/badge_extra_price/*", offset=10), FILE, path=os.path.join(self.root, "odoo/addons/sale/static/src/js/badge_extra_price"))

    def test_inherited_arch_precision(self):
        self.arch_precision(0.002)


@unittest.skipUnless(os.path.isdir(_tree("saas-19.4")), "Odoo saas-19.4 sources not available")
class RealSaas194Tests(_RealTree):
    version = "saas-19.4"

    def test_ir_access_csv(self):
        rel = "odoo/addons/sales_team/security/ir.access.csv"
        self.assertTarget(self.one(rel, "crm.team,base.group_user", offset=2), MODEL, model="crm.team")


if __name__ == "__main__":
    unittest.main()
