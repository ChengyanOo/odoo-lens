"""Tests for features/hover.py: the markdown cards, on the mini fixture and real checkouts."""

from __future__ import annotations

import gc
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens.analysis.targets import ATTR, FIELD, FILE, METHOD, MODEL, MODULE, PYSYM, TEMPLATE, XMLID, Target  # noqa: E402
from odoo_lens.cli import _ctx, headless_server, load_env  # noqa: E402
from odoo_lens.features.hover import hover, hover_for_target  # noqa: E402

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")
WORKTREES = os.path.expanduser(os.environ.get("ODOO_LENS_WORKTREES", "~/odev/worktrees"))

SALE = os.path.join(MINI, "odoo/addons/sale/models/sale_order.py")
SALE_STOCK = os.path.join(MINI, "odoo/addons/sale_stock/models/sale_order.py")
MY = os.path.join(MINI, "custom/my_module/models/sale_order.py")


def line_of(path: str, needle: str) -> int:
    """1-based line of the first line of ``path`` containing ``needle``."""
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if needle in line:
                return i
    raise AssertionError(f"{needle!r} not in {path}")


class FixtureHoverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, MY)

    def card(self, target: Target, path: str = MY, line: int = 1, col: int = 1) -> str:
        md = hover_for_target(_ctx(self.server, self.env, path, line, col), target)
        self.assertIsNotNone(md, target)
        self.assertLessEqual(len(md.splitlines()), 40)
        return md

    def test_field_card(self):
        md = self.card(Target(FIELD, model="sale.order", name="partner_id"))
        lines = md.splitlines()
        self.assertEqual(lines[0], "**sale.order.partner_id** · Many2one → res.partner")
        self.assertEqual(lines[1], "required · index")
        self.assertIn('"Customer"', md)
        self.assertIn(f"Defined in **sale** — {SALE}:{line_of(SALE, 'partner_id =')}", md)
        # sale_stock is not a dependency of my_module: reported apart, with its arguments
        self.assertIn(f"outside this module's dependencies (applies only when installed): **sale_stock** ({SALE_STOCK}:{line_of(SALE_STOCK, 'partner_id =')}) `tracking=True`", md)
        self.assertIn("```python\npartner_id = fields.Many2one('res.partner', string='Customer', required=True, index=True)\n```", md)

    def test_field_card_overrides_in_scope(self):
        md = self.card(Target(FIELD, model="sale.order", name="partner_id"), SALE_STOCK)
        self.assertIn("required · index · tracking", md)
        self.assertIn(f"Overridden in:\n- **sale_stock** — {SALE_STOCK}:{line_of(SALE_STOCK, 'partner_id =')} `tracking=True`", md)
        self.assertNotIn("outside", md)

    def test_field_card_details(self):
        state = self.card(Target(FIELD, model="sale.order", name="state"))
        self.assertIn("selection: `draft` (Quotation), `sale` (Sales Order), `cancel` (Cancelled)", state)
        self.assertIn("default: `'draft'`", state)
        amount = self.card(Target(FIELD, model="sale.order", name="amount_total"))
        self.assertIn(f"compute: `_compute_amounts` — {SALE}:{line_of(SALE, 'def _compute_amounts')}", amount)
        self.assertIn("store", amount.splitlines()[1])
        related = self.card(Target(FIELD, model="sale.order", name="country_code"))
        self.assertIn("related: `partner_id.country_id.code` → **res.country.code** (Char)", related)

    def test_delegated_magic_and_hidden_fields(self):
        delegated = self.card(Target(FIELD, model="res.users", name="name"))
        self.assertIn("Delegated from **res.partner** via `_inherits` through `partner_id`", delegated)
        magic = self.card(Target(FIELD, model="sale.order", name="create_uid"))
        self.assertTrue(magic.startswith("**sale.order.create_uid** · Many2one → res.users"))
        self.assertIn("Automatic field added by the ORM to every model", magic)
        hidden = self.card(Target(FIELD, model="sale.order", name="picking_policy"))
        self.assertIn("⚠ defined only in modules outside this module's dependencies: sale_stock", hidden)
        self.assertEqual(self.card(Target(FIELD, model="sale.order", name="nope")), "**sale.order.nope** · ⚠ no field `nope` on sale.order in this environment")
        self.assertEqual(self.card(Target(MODEL, model="res.partnr")), "**res.partnr** · ⚠ model not found in this environment")
        self.assertIn("xml id not found", self.card(Target(XMLID, xmlid="base.no_such_record")))

    def test_method_card(self):
        md = self.card(Target(METHOD, model="sale.order", name="action_confirm"))
        lines = md.splitlines()
        self.assertEqual(lines[0], "**sale.order.action_confirm**(self)")
        self.assertIn("*Confirm the quotation into a sales order.*", md)
        self.assertIn(f"1. **my_module** — {MY}:{line_of(MY, 'def action_confirm')} · super()", md)
        self.assertIn(f"2. **sale** — {SALE}:{line_of(SALE, 'def action_confirm')}", md)
        # the order every override runs in when all modules overriding it are installed
        full = md.split("**Call order with every module installed** (4, load order reversed; each `super()` continues on the next line):\n", 1)[1]
        self.assertEqual([line.split("**")[1] for line in full.splitlines()], ["sale_subscription", "sale_stock", "my_module", "sale"])
        self.assertIn("**sale_stock** — ", full)
        self.assertIn("· super() *(not a dependency)*", full)

    def test_method_card_super_and_framework(self):
        sup = Target(METHOD, model="sale.order", name="action_confirm", origin=(line_of(MY, "super().action_confirm") - 1, 23, 37), extra={"super": True})
        md = self.card(sup)
        self.assertIn(f"`super()` here reaches **sale** — {SALE}:{line_of(SALE, 'def action_confirm')}", md)
        create = self.card(Target(METHOD, model="sale.order", name="create"), SALE)
        self.assertIn("`@api.model_create_multi`", create)
        self.assertIn("**odoo** (BaseModel) — ", create)
        self.assertIn("*Create new records for the model.*", create)

    def test_model_card(self):
        md = self.card(Target(MODEL, model="sale.order"))
        lines = md.splitlines()
        self.assertEqual(lines[0], "**sale.order** — Sales Order")
        self.assertEqual(lines[1], "Model · _order `date_order desc, id desc`")
        self.assertIn(f"Defined in **sale** — {SALE}:{line_of(SALE, 'class SaleOrder')}", md)
        self.assertIn("Inherits: mail.thread", md)
        self.assertIn("Extended in 1 module(s): my_module", md)
        self.assertIn("+2 extending module(s) outside this module's dependencies: sale_stock, sale_subscription", md)
        self.assertIn("- `access_sale_order` base.group_user: crud (sale)", md)
        self.assertIn("Views: 1 primary (sale.view_order_form) · 2 extension(s)", md)
        partner = self.card(Target(MODEL, model="res.partner"))
        self.assertIn("_rec_name `complete_name`", partner)
        self.assertIn("- `access_res_partner_user` base.group_user: cru (base)", partner)  # bare group id qualified
        mixin = self.card(Target(MODEL, model="mail.thread"))
        self.assertIn("AbstractModel", mixin)
        self.assertIn("Inherited by 2 model(s): product.product, sale.order", mixin)

    def test_record_cards(self):
        view = self.card(Target(XMLID, xmlid="sale.view_order_form"))
        self.assertIn("view of **sale.order** · form · primary · priority 16", view)
        self.assertIn("Inherited by 2 view(s) (recursively):", view)
        self.assertIn("- my_module.view_order_form_inherit_my_module (my_module)", view)
        ext = self.card(Target(XMLID, xmlid="sale_stock.view_order_form_inherit_sale_stock"))
        self.assertIn("view of **sale.order** · form · extension · priority 16", ext)
        self.assertIn("Inherits **sale.view_order_form**", ext)
        action = self.card(Target(XMLID, xmlid="sale.action_orders"))
        self.assertIn("**sale.action_orders** · ir.actions.act_window", action)
        self.assertIn("Model: **sale.order**", action)
        menu = self.card(Target(XMLID, xmlid="sale.menu_sale_order"))
        self.assertIn("action: `action_orders`", menu)
        company = self.card(Target(XMLID, xmlid="base.main_company"))
        self.assertIn("· noupdate", company)
        implicit = self.card(Target(XMLID, xmlid="sale.model_sale_order"))
        self.assertIn("ir.model (implicit xml id)", implicit)
        self.assertIn("model **sale.order**", implicit)
        field = self.card(Target(XMLID, xmlid="sale.field_sale_order__partner_id"))
        self.assertIn("ir.model.fields (implicit xml id)", field)
        self.assertIn("field **sale.order.partner_id**", field)
        acl = self.card(Target(XMLID, xmlid="base.access_res_partner_user"))
        self.assertIn("Access (c=create r=read u=write d=unlink): `access_res_partner_user` base.group_user: cru (base)", acl)

    def test_module_file_symbol_attr_cards(self):
        sale = self.card(Target(MODULE, name="sale"))
        self.assertTrue(sale.startswith("**sale** — Sales · version 1.2"))
        self.assertIn("Depends: product, mail (3 modules in the dependency closure)", sale)
        self.assertIn("Required by 3 module(s) (3 directly): my_module, sale_stock, sale_subscription", sale)
        my = self.card(Target(MODULE, name="my_module"))
        self.assertIn("⚠ dependencies not found in this environment: unknown_module", my)
        self.assertIn("not found", self.card(Target(MODULE, name="unknown_module")))
        views = os.path.join(MINI, "odoo/addons/sale/views/sale_order_views.xml")
        self.assertIn("4 record(s): ir.ui.menu ×2, ir.ui.view ×1, ir.actions.act_window ×1", self.card(Target(FILE, path=views)))
        self.assertIn("⚠ file does not exist", self.card(Target(FILE, path=os.path.join(MINI, "custom/my_module/views/missing_file.xml"))))
        utils = os.path.join(MINI, "odoo/odoo/tools/float_utils.py")
        sym = self.card(Target(PYSYM, name="float_compare", path=utils, line=line_of(utils, "def float_compare") - 1, col=4, end_col=17))
        self.assertIn("def float_compare(value1, value2, precision_digits=None):", sym)
        self.assertIn("*Compare two floats at the given precision.*", sym)
        self.assertIn("odoo.tools.float_utils — ", sym)
        attr = self.card(Target(ATTR, model="sale.order", name="_order"))
        self.assertTrue(attr.startswith("**sale.order._order** = `'date_order desc, id desc'`"))

    def test_ambiguous_card(self):
        md = self.card(Target(FIELD, name="name", models=["res.partner", "sale.order"]))
        self.assertTrue(md.startswith("**name** — receiver type not inferred; defined on 2 candidate model(s):"))
        self.assertIn("- **res.partner**.name · Char — **base**", md)
        self.assertIn("- **sale.order**.name · Char — **sale**", md)

    def test_entry_point_xml(self):
        views = os.path.join(MINI, "odoo/addons/sale/views/sale_order_views.xml")
        line = line_of(views, '"action_confirm"')
        with open(views, encoding="utf-8") as fh:
            col = fh.read().split("\n")[line - 1].index("action_confirm") + 3
        res = hover(_ctx(self.server, self.env, views, line, col))
        self.assertEqual(set(res), {"contents"})
        self.assertEqual(res["contents"]["kind"], "markdown")
        self.assertTrue(res["contents"]["value"].startswith("**sale.order.action_confirm**(self)"))

    def test_package_entry_point_stays_callable(self):
        # importing features/hover.py rebinds odoo_lens.features.hover to the submodule
        from odoo_lens import features

        views = os.path.join(MINI, "odoo/addons/sale/views/sale_order_views.xml")
        ctx = _ctx(self.server, self.env, views, line_of(views, '"action_confirm"'), 40)
        self.assertEqual(features.hover(ctx), features.hover(ctx))
        self.assertIsNotNone(features.hover(ctx))

    def test_entry_point_python(self):
        line = line_of(MY, "super().action_confirm")
        res = hover(_ctx(self.server, self.env, MY, line, 26))
        self.assertIn("`super()` here reaches **sale**", res["contents"]["value"])


class RealTreeHoverCase(unittest.TestCase):
    VERSION = ""

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(WORKTREES, cls.VERSION)
        cls.server = headless_server()
        cls.path = os.path.join(cls.root, "odoo/addons/sale/models/sale_order.py")
        cls.env, _ = load_env(cls.server, cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.server = cls.env = None
        gc.collect()

    def card(self, target: Target) -> str:
        md = hover_for_target(_ctx(self.server, self.env, self.path, 1, 1), target)
        self.assertIsNotNone(md, target)
        self.assertLessEqual(len(md.splitlines()), 40)
        return md

    def test_res_partner_name(self):
        md = self.card(Target(FIELD, model="res.partner", name="name"))
        self.assertTrue(md.startswith("**res.partner.name** · Char"))
        self.assertIn(f"Defined in **base** — {self.root}/odoo/odoo/addons/base/models/res_partner.py:", md)
        self.assertIn("```python\nname = fields.Char(", md)

    def test_sale_order_cards(self):
        write = self.card(Target(METHOD, model="sale.order", name="write"))
        self.assertTrue(write.startswith("**sale.order.write**(self, "))
        self.assertIn("**odoo** (BaseModel)", write)
        self.assertIn("(mail.thread)", write)
        model = self.card(Target(MODEL, model="sale.order"))
        self.assertIn("Defined in **sale**", model)
        self.assertIn("mail.thread", model)
        self.assertIn("Access rights", model)
        state = self.card(Target(FIELD, model="sale.order", name="state"))
        self.assertIn("`draft`", state)  # selection resolved even when given as a module constant
        self.assertIn("`sale`", state)
        view = self.card(Target(XMLID, xmlid="sale.view_order_form"))
        self.assertIn("view of **sale.order** · form · primary", view)

    def test_client_template_card(self):
        md = self.card(Target(TEMPLATE, xmlid="web.ListRenderer"))
        self.assertTrue(md.startswith("**web.ListRenderer** · client (owl/QWeb) template"))
        self.assertIn(f"Defined in **web** — {self.root}/odoo/addons/web/static/src/views/list/list_renderer.xml:", md)
        self.assertIn("Inherited by", md)

    def test_latency(self):
        targets = [
            Target(FIELD, model="res.partner", name="name"),
            Target(FIELD, model="sale.order", name="partner_id"),
            Target(FIELD, model="sale.order", name="state"),
            Target(METHOD, model="sale.order", name="write"),
            Target(METHOD, model="mail.thread", name="message_post"),
            Target(MODEL, model="res.partner"),
            Target(MODEL, model="mail.thread"),
            Target(XMLID, xmlid="sale.view_order_form"),
            Target(XMLID, xmlid="base.group_user"),
            Target(MODULE, name="base"),
        ]
        ctx = _ctx(self.server, self.env, self.path, 1, 1)
        worst = 0.0
        for target in targets:
            t = time.perf_counter()
            hover_for_target(ctx, target)
            worst = max(worst, time.perf_counter() - t)
        self.assertLess(worst, 0.05)


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "16.0")), "no 16.0 checkout")
class RealTree16HoverTest(RealTreeHoverCase):
    VERSION = "16.0"


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "18.0")), "no 18.0 checkout")
class RealTree18HoverTest(RealTreeHoverCase):
    VERSION = "18.0"


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "20.0")), "no 20.0 checkout")
class RealTree20HoverTest(RealTreeHoverCase):
    VERSION = "20.0"


del RealTreeHoverCase  # abstract: only the per-version subclasses run

if __name__ == "__main__":
    unittest.main()
