from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens.analysis.targets import ATTR, FIELD, FILE, METHOD, MODEL, MODULE, PYSYM, TEMPLATE, XMLID, Target  # noqa: E402
from odoo_lens.cli import _ctx, headless_server, load_env  # noqa: E402
from odoo_lens.documents import uri_to_path  # noqa: E402
from odoo_lens.features.calls import incoming, outgoing, prepare  # noqa: E402
from odoo_lens.features.navigation import (  # noqa: E402
    definition,
    implementation,
    implementations_for_target,
    locations_for_target,
    type_definition,
    type_locations_for_target,
)
from odoo_lens.server import RequestContext  # noqa: E402

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")
WORKTREES = os.path.expanduser(os.environ.get("ODOO_LENS_WORKTREES", "~/odev/worktrees"))

SALE = "odoo/addons/sale/models/sale_order.py"
SALE_STOCK = "odoo/addons/sale_stock/models/sale_order.py"
SUBSCRIPTION = "enterprise/sale_subscription/models/sale_order.py"
MY = "custom/my_module/models/sale_order.py"
PARTNER = "odoo/odoo/addons/base/models/res_partner.py"
MODELS = "odoo/odoo/models.py"
SALE_VIEWS = "odoo/addons/sale/views/sale_order_views.xml"


def at(root: str, rel: str, needle: str, nth: int = 0) -> tuple[int, int]:
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    seen = 0
    for i, line in enumerate(lines):
        col = line.find(needle)
        while col >= 0:
            if seen == nth:
                return i + 1, col + 1
            seen += 1
            col = line.find(needle, col + 1)
    raise AssertionError(f"{needle!r} not found in {rel}")


def item_ctx(server, env, item: dict) -> RequestContext:
    start = item["selectionRange"]["start"]
    return RequestContext(server, env, env.registry, uri_to_path(item["uri"]), start["line"], start["character"])


def spots(locs: list[dict], root: str = MINI) -> list[tuple[str, int, int]]:
    return [
        (os.path.relpath(uri_to_path(loc["uri"]), root), loc["range"]["start"]["line"] + 1, loc["range"]["start"]["character"] + 1)
        for loc in locs
    ]


class FixtureCase(unittest.TestCase):
    root = MINI

    @classmethod
    def setUpClass(cls):
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(cls.root, MY))

    def ctx(self, rel: str, needle: str | None = None, nth: int = 0, offset: int = 0):
        line, col = at(self.root, rel, needle, nth) if needle else (1, 1)
        return _ctx(self.server, self.env, os.path.join(self.root, rel), line, col + offset)

    def spot(self, rel: str, needle: str, nth: int = 0) -> tuple[str, int, int]:
        return (rel, *at(self.root, rel, needle, nth))


class DefinitionTest(FixtureCase):
    def test_field_definition_scoped_to_dependencies(self):
        locs = locations_for_target(self.ctx(MY), Target(FIELD, model="sale.order", name="partner_id"))
        self.assertEqual(spots(locs), [self.spot(SALE, "partner_id =")])

    def test_field_definitions_base_most_first(self):
        locs = locations_for_target(self.ctx(SALE_STOCK), Target(FIELD, model="sale.order", name="partner_id"))
        self.assertEqual(spots(locs), [self.spot(SALE, "partner_id ="), self.spot(SALE_STOCK, "partner_id =")])

    def test_field_outside_dependencies_falls_back_to_env(self):
        locs = locations_for_target(self.ctx(MY), Target(FIELD, model="sale.order", name="picking_policy"))
        self.assertEqual(spots(locs), [self.spot(SALE_STOCK, "picking_policy =")])

    def test_delegated_magic_and_attr(self):
        ctx = self.ctx(MY)
        delegated = locations_for_target(ctx, Target(FIELD, model="res.users", name="name"))
        self.assertEqual(spots(delegated), [self.spot(PARTNER, "name = fields")])
        magic = locations_for_target(ctx, Target(FIELD, model="sale.order", name="id"))
        self.assertEqual(spots(magic), [self.spot(MODELS, "id =")])
        attr = locations_for_target(ctx, Target(ATTR, model="sale.order", name="_order"))
        self.assertEqual(spots(attr), [self.spot(MODELS, "_order ="), self.spot(SALE, "_order =")])

    def test_method_definitions_base_most_first(self):
        locs = locations_for_target(self.ctx(MY), Target(METHOD, model="sale.order", name="action_confirm"))
        self.assertEqual(spots(locs), [self.spot(SALE, "action_confirm(self)"), self.spot(MY, "action_confirm(self)")])

    def test_super_reaches_next_definition_below(self):
        target = Target(METHOD, model="sale.order", name="action_confirm", extra={"super": True})
        for rel in (MY, SALE_STOCK, SUBSCRIPTION):
            locs = locations_for_target(self.ctx(rel, "super().action_confirm"), target)
            self.assertEqual(spots(locs)[0], self.spot(SALE, "action_confirm(self)"), rel)
            self.assertNotIn(rel, [s[0] for s in spots(locs)])
        create = Target(METHOD, model="sale.order", name="create", extra={"super": True})
        self.assertEqual(spots(locations_for_target(self.ctx(SALE, "super().create"), create)), [self.spot(MODELS, "create(self")])

    def test_model_definition_first_then_extensions(self):
        locs = locations_for_target(self.ctx(MY), Target(MODEL, model="sale.order"))
        self.assertEqual(spots(locs), [self.spot(SALE, "SaleOrder"), self.spot(MY, "SaleOrder")])
        everywhere = locations_for_target(self.ctx(MODELS), Target(MODEL, model="sale.order"))
        self.assertEqual(len(everywhere), 4)
        self.assertEqual(spots(everywhere)[0], self.spot(SALE, "SaleOrder"))
        outside = locations_for_target(self.ctx(MY), Target(MODEL, model="sale.subscription.plan"))
        self.assertEqual(spots(outside), [self.spot(SUBSCRIPTION, "SaleSubscriptionPlan")])

    def test_records_modules_files_symbols(self):
        ctx = self.ctx(MY)
        self.assertEqual(spots(locations_for_target(ctx, Target(XMLID, xmlid="sale.view_order_form"))), [self.spot(SALE_VIEWS, "view_order_form")])
        implicit = locations_for_target(ctx, Target(XMLID, xmlid="sale.model_sale_order"))
        self.assertEqual(spots(implicit), [self.spot(SALE, "SaleOrder")])
        self.assertEqual(spots(locations_for_target(ctx, Target(MODULE, name="sale"))), [("odoo/addons/sale/__manifest__.py", 1, 1)])
        self.assertEqual(locations_for_target(ctx, Target(MODULE, name="unknown_module")), [])
        path = os.path.join(MINI, SALE_VIEWS)
        self.assertEqual(spots(locations_for_target(ctx, Target(FILE, path=path))), [(SALE_VIEWS, 1, 1)])
        sym = Target(PYSYM, name="float_compare", path=os.path.join(MINI, "odoo/odoo/tools/float_utils.py"), line=4, col=4, end_col=17)
        self.assertEqual(spots(locations_for_target(ctx, sym)), [("odoo/odoo/tools/float_utils.py", 5, 5)])
        self.assertEqual(locations_for_target(ctx, Target(TEMPLATE, xmlid="web.Missing")), [])

    def test_ambiguous_target_lists_each_candidate(self):
        locs = locations_for_target(self.ctx(MY), Target(FIELD, name="name", models=["res.partner", "sale.order"]))
        self.assertEqual(spots(locs), [self.spot(PARTNER, "name = fields"), self.spot(SALE, "name = fields")])

    def test_type_definition(self):
        ctx = self.ctx(MY)
        self.assertEqual(spots(type_locations_for_target(ctx, Target(FIELD, model="sale.order", name="partner_id"))), [self.spot(PARTNER, "Partner")])
        self.assertEqual(spots(type_locations_for_target(ctx, Target(FIELD, model="sale.order", name="x_note"))), [self.spot(MY, "x_note =")])
        self.assertEqual(spots(type_locations_for_target(ctx, Target(METHOD, model="sale.order", name="action_confirm"))), [self.spot(SALE, "SaleOrder")])
        view = type_locations_for_target(ctx, Target(XMLID, xmlid="sale.view_order_form"))
        self.assertEqual(spots(view), [self.spot("odoo/odoo/addons/base/models/ir_ui_view.py", "View")])


class ImplementationTest(FixtureCase):
    def test_method_overrides_in_all_modules(self):
        locs = spots(implementations_for_target(self.ctx(SALE), Target(METHOD, model="sale.order", name="action_confirm")))
        self.assertEqual(locs[0], self.spot(SALE, "action_confirm(self)"))
        self.assertEqual({s[0] for s in locs}, {SALE, MY, SALE_STOCK, SUBSCRIPTION})

    def test_inherited_method_implementation(self):
        locs = spots(implementations_for_target(self.ctx(SALE), Target(METHOD, model="sale.order", name="message_post")))
        self.assertEqual(locs, [self.spot("odoo/addons/mail/models/mail_thread.py", "message_post(self")])

    def test_field_redefinitions(self):
        locs = spots(implementations_for_target(self.ctx(MY), Target(FIELD, model="sale.order", name="partner_id")))
        self.assertEqual(locs, [self.spot(SALE, "partner_id ="), self.spot(SALE_STOCK, "partner_id =")])

    def test_model_classes_and_children(self):
        locs = spots(implementations_for_target(self.ctx(MY), Target(MODEL, model="mail.thread")))
        self.assertEqual(locs, [
            self.spot("odoo/addons/mail/models/mail_thread.py", "MailThread"),
            self.spot("odoo/addons/product/models/product.py", "ProductProduct"),
            self.spot(SALE, "SaleOrder"),
        ])

    def test_views_and_modules(self):
        ctx = self.ctx(MY)
        views = spots(implementations_for_target(ctx, Target(XMLID, xmlid="sale.view_order_form")))
        self.assertEqual({v[0] for v in views}, {"custom/my_module/views/sale_order_views.xml", "odoo/addons/sale_stock/views/sale_order_views.xml"})
        deps = spots(implementations_for_target(ctx, Target(MODULE, name="sale")))
        self.assertEqual([d[0] for d in deps], ["custom/my_module/__manifest__.py", "odoo/addons/sale_stock/__manifest__.py", "enterprise/sale_subscription/__manifest__.py"])
        for rel, line, col in deps:
            self.assertEqual((line, col - 1), at(MINI, rel, "'sale'"))
        self.assertEqual(len(implementations_for_target(ctx, Target(MODULE, name="base"))), 6)


class EntryPointTest(FixtureCase):
    def test_xml_view_field_and_button(self):
        my_view = "custom/my_module/views/sale_order_views.xml"
        ctx = self.ctx(my_view, 'name="partner_id"', offset=7)
        self.assertEqual(spots(definition(ctx)), [self.spot(SALE, "partner_id =")])
        self.assertEqual(spots(type_definition(ctx)), [self.spot(PARTNER, "Partner")])
        self.assertEqual(spots(implementation(ctx)), [self.spot(SALE, "partner_id ="), self.spot(SALE_STOCK, "partner_id =")])
        button = self.ctx(SALE_VIEWS, '"action_confirm"', offset=3)
        self.assertEqual(spots(definition(button)), [self.spot(SALE, "action_confirm(self)")])
        self.assertEqual(len(implementation(button)), 4)
        inherit = self.ctx(my_view, "sale.view_order_form", offset=2)
        self.assertEqual(spots(definition(inherit)), [self.spot(SALE_VIEWS, "view_order_form")])

    def test_python_super_and_fields(self):
        ctx = self.ctx(MY, "super().action_confirm", offset=10)
        self.assertEqual(spots(definition(ctx)), [self.spot(SALE, "action_confirm(self)")])
        ctx = self.ctx(MY, "order.partner_id.name", offset=8)
        self.assertEqual(spots(definition(ctx)), [self.spot(SALE, "partner_id =")])
        self.assertEqual(spots(type_definition(ctx)), [self.spot(PARTNER, "Partner")])
        ctx = self.ctx(MY, "_inherit = 'sale.order'", offset=13)
        self.assertEqual(spots(definition(ctx))[0], self.spot(SALE, "SaleOrder"))


class CallHierarchyTest(FixtureCase):
    def item_ctx(self, item: dict) -> RequestContext:
        return item_ctx(self.server, self.env, item)

    def calls(self, results: list[dict], side: str) -> list[tuple[str, str, list[tuple[int, int]]]]:
        out = []
        for c in results:
            it = c[side]
            out.append((it["name"], os.path.relpath(uri_to_path(it["uri"]), self.root), [(r["start"]["line"] + 1, r["start"]["character"] + 1) for r in c["fromRanges"]]))
        return out

    def test_prepare(self):
        items = prepare(self.ctx(MY, "def action_confirm", offset=4))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["name"], item["kind"], item["detail"]), ("sale.order.action_confirm", 6, "my_module"))
        self.assertEqual(item["data"], {"model": "sale.order", "name": "action_confirm"})
        line = at(self.root, MY, "def action_confirm")[0] - 1
        self.assertEqual(item["range"]["start"], {"line": line, "character": 4})
        self.assertEqual(item["range"]["end"]["line"], at(self.root, MY, "return super().action_confirm()")[0] - 1)
        self.assertEqual(item["selectionRange"], {"start": {"line": line, "character": 8}, "end": {"line": line, "character": 22}})
        reached = prepare(self.ctx(MY, "super().action_confirm", offset=10))[0]
        self.assertEqual(uri_to_path(reached["uri"]), os.path.join(self.root, SALE))
        enclosing = prepare(self.ctx(SALE, "return True"))[0]
        self.assertEqual(enclosing["name"], "sale.order.action_confirm")
        self.assertEqual(uri_to_path(enclosing["uri"]), os.path.join(self.root, SALE))

    def test_incoming(self):
        item = prepare(self.ctx(SALE, "def action_confirm", offset=4))[0]
        self.assertEqual(self.calls(incoming(self.item_ctx(item), item), "from"), [
            ("sale.order.action_confirm", MY, [at(self.root, MY, "action_confirm()")]),
            ("sale.order.action_confirm", SALE_STOCK, [at(self.root, SALE_STOCK, "action_confirm()")]),
            ("sale.order.action_confirm", SUBSCRIPTION, [at(self.root, SUBSCRIPTION, "action_confirm()")]),
            ("sale.view_order_form", SALE_VIEWS, [(at(self.root, SALE_VIEWS, '"action_confirm"')[0], at(self.root, SALE_VIEWS, '"action_confirm"')[1] + 1)]),
        ])
        top = prepare(self.ctx(SUBSCRIPTION, "def action_confirm", offset=4))[0]
        self.assertEqual([c[0] for c in self.calls(incoming(self.item_ctx(top), top), "from")], ["sale.view_order_form"])
        mine = prepare(self.ctx(MY, "def action_confirm", offset=4))[0]
        self.assertNotIn(MY, [c[1] for c in self.calls(incoming(self.item_ctx(mine), mine), "from")])
        compute = prepare(self.ctx(SALE, "def _compute_amounts", offset=4))[0]
        self.assertEqual(self.calls(incoming(self.item_ctx(compute), compute), "from"), [
            ("sale.order.amount_total", SALE, [(at(self.root, SALE, "'_compute_amounts'")[0], at(self.root, SALE, "'_compute_amounts'")[1] + 1)]),
        ])

    def test_outgoing(self):
        item = prepare(self.ctx(MY, "def action_confirm", offset=4))[0]
        self.assertEqual(self.calls(outgoing(self.item_ctx(item), item), "to"), [
            ("res.partner.search", MODELS, [at(self.root, MY, "search(")]),
            ("sale.order.action_confirm", SALE, [at(self.root, MY, "action_confirm()")]),
        ])


class ChildOverridesTest(FixtureCase):
    EXTRA = {
        "custom/mail_ext/__init__.py": "from . import models\n",
        "custom/mail_ext/__manifest__.py": "{'name': 'Mail ext', 'version': '18.0.1.0.0', 'depends': ['sale'], 'license': 'LGPL-3'}\n",
        "custom/mail_ext/models/__init__.py": "from . import models\n",
        "custom/mail_ext/models/models.py": textwrap.dedent(
            """\
            from odoo import fields, models


            class SaleOrder(models.Model):
                _inherit = 'sale.order'

                message_ids = fields.One2many(string='Order messages')

                def message_post(self, **kwargs):
                    return super().message_post(**kwargs)


            class ProductProduct(models.Model):
                _inherit = 'product.product'

                def message_post(self, **kwargs):
                    return super().message_post(**kwargs)

                def confirm_orders(self, orders):
                    orders.action_confirm()
                    return self.env['sale.order'].search([]).action_confirm()
            """
        ),
        "custom/mail_ext/controllers/__init__.py": "from . import main, more\n",
        "custom/mail_ext/controllers/main.py": textwrap.dedent(
            """\
            from odoo import http


            class Main(http.Controller):
                def index(self):
                    return 'main'


            class Sub(Main):
                def index(self):
                    return super().index()
            """
        ),
        "custom/mail_ext/controllers/more.py": textwrap.dedent(
            """\
            from odoo.addons.mail_ext.controllers import main
            from .main import Sub


            class More(main.Main):
                pass


            class Deeper(Sub):
                def index(self):
                    return 'deeper'
            """
        ),
    }

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-nav-")
        cls.root = os.path.join(cls.tmp, "mini")
        shutil.copytree(MINI, cls.root)
        for rel, text in cls.EXTRA.items():
            os.makedirs(os.path.dirname(os.path.join(cls.root, rel)), exist_ok=True)
            with open(os.path.join(cls.root, rel), "w", encoding="utf-8") as fh:
                fh.write(text)
        with mock.patch.dict(os.environ, {"ODOO_LENS_CACHE_DIR": os.path.join(cls.tmp, "cache")}):
            super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_mixin_method_overridden_in_children(self):
        locs = spots(implementations_for_target(self.ctx(MY), Target(METHOD, model="mail.thread", name="message_post")), self.root)
        ext = "custom/mail_ext/models/models.py"
        self.assertEqual(locs, [
            self.spot("odoo/addons/mail/models/mail_thread.py", "message_post(self"),
            self.spot(ext, "message_post(self", 0),
            self.spot(ext, "message_post(self", 1),
        ])

    def test_incoming_with_untyped_receiver(self):
        ext = "custom/mail_ext/models/models.py"
        item = prepare(self.ctx(SALE, "def action_confirm", offset=4))[0]
        callers = {c["from"]["name"]: c for c in incoming(item_ctx(self.server, self.env, item), item)}
        self.assertEqual(len(callers["product.product.confirm_orders"]["fromRanges"]), 2)
        self.assertEqual(uri_to_path(callers["product.product.confirm_orders"]["from"]["uri"]), os.path.join(self.root, ext))

    def test_python_subclasses_and_overrides(self):
        main, more = "custom/mail_ext/controllers/main.py", "custom/mail_ext/controllers/more.py"
        ctx = self.ctx(main)
        line, col = at(self.root, main, "Main(")
        cls = Target(PYSYM, name="Main", path=os.path.join(self.root, main), line=line - 1, col=col - 1, end_col=col + 3)
        self.assertEqual(spots(implementations_for_target(ctx, cls), self.root), [
            self.spot(main, "Sub("), self.spot(more, "More("), self.spot(more, "Deeper("),
        ])
        line, col = at(self.root, main, "index")
        meth = Target(PYSYM, name="index", path=os.path.join(self.root, main), line=line - 1, col=col - 1, end_col=col + 4)
        self.assertEqual(spots(implementations_for_target(ctx, meth), self.root), [
            self.spot(main, "index"), self.spot(main, "index", 1), self.spot(more, "index"),
        ])
        line, col = at(self.root, "odoo/odoo/http.py", "Controller")
        base = Target(PYSYM, name="Controller", path=os.path.join(self.root, "odoo/odoo/http.py"), line=line - 1, col=col - 1, end_col=col + 9)
        self.assertEqual(spots(implementations_for_target(ctx, base), self.root)[0], self.spot(main, "Main("))

    def test_mixin_field_redefined_in_child(self):
        locs = spots(implementations_for_target(self.ctx(MY), Target(FIELD, model="mail.thread", name="message_ids")), self.root)
        self.assertEqual(locs, [self.spot("odoo/addons/mail/models/mail_thread.py", "message_ids ="), self.spot("custom/mail_ext/models/models.py", "message_ids =")])
        own = spots(locations_for_target(self.ctx("custom/mail_ext/models/models.py"), Target(FIELD, model="sale.order", name="message_ids")), self.root)
        self.assertEqual(own, locs)


class RealTreeCase(unittest.TestCase):
    VERSION = ""

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(WORKTREES, cls.VERSION)
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(cls.root, "odoo/addons/sale/models/sale_order.py"))

    @classmethod
    def tearDownClass(cls):
        cls.server = cls.env = None
        gc.collect()

    def ctx(self, rel: str, needle: str | None = None, offset: int = 0):
        line, col = at(self.root, rel, needle) if needle else (1, 1)
        return _ctx(self.server, self.env, os.path.join(self.root, rel), line, col + offset)

    def test_action_confirm_implementations(self):
        locs = spots(implementations_for_target(self.ctx("odoo/addons/sale/models/sale_order.py"), Target(METHOD, model="sale.order", name="action_confirm")), self.root)
        self.assertEqual(locs[0][0], "odoo/addons/sale/models/sale_order.py")
        files = {s[0] for s in locs}
        for rel in ("sale_management", "sale_crm", "event_sale", "sale_loyalty"):
            self.assertIn(f"odoo/addons/{rel}/models/sale_order.py", files)
        self.assertIn("enterprise/sale_subscription/models/sale_order.py", files)
        self.assertGreaterEqual(len(locs), 10)

    def test_message_post_overrides_in_child_models(self):
        locs = spots(implementations_for_target(self.ctx("odoo/addons/mail/models/mail_thread.py"), Target(METHOD, model="mail.thread", name="message_post")), self.root)
        self.assertEqual(locs[0][0], "odoo/addons/mail/models/mail_thread.py")
        files = {s[0] for s in locs}
        self.assertIn("odoo/addons/sale/models/sale_order.py", files)
        self.assertTrue(any(f.startswith("odoo/addons/purchase/models/purchase") for f in files))
        # mail.group defines its own message_post but does not inherit mail.thread
        self.assertNotIn("odoo/addons/mail_group/models/mail_group.py", files)

    def test_super_from_extension_module(self):
        rel = "odoo/addons/sale_stock/models/sale_order.py"
        ctx = self.ctx(rel, "self)._action_confirm", offset=7)
        locs = spots(locations_for_target(ctx, Target(METHOD, model="sale.order", name="_action_confirm", extra={"super": True})), self.root)
        self.assertTrue(locs)
        self.assertNotEqual(locs[0][0], rel)
        self.assertIn(self.env.registry.module_of_path(os.path.join(self.root, locs[0][0])), ctx.scope)

    def test_client_template(self):
        ctx = self.ctx("odoo/addons/sale/models/sale_order.py")
        target = Target(TEMPLATE, xmlid="web.ListRenderer")
        self.assertEqual(spots(locations_for_target(ctx, target), self.root)[0][0], "odoo/addons/web/static/src/views/list/list_renderer.xml")
        inheritors = spots(implementations_for_target(ctx, target), self.root)
        self.assertTrue(inheritors)
        self.assertTrue(all(rel.endswith(".xml") and "/static/src/" in rel for rel, _, _ in inheritors))
        self.assertEqual(len(inheritors), len(set(inheritors)))

    def test_field_definition_and_type(self):
        ctx = self.ctx("odoo/addons/sale/models/sale_order.py")
        locs = spots(locations_for_target(ctx, Target(FIELD, model="sale.order", name="partner_id")), self.root)
        self.assertEqual(locs[0][0], "odoo/addons/sale/models/sale_order.py")
        types = spots(type_locations_for_target(ctx, Target(FIELD, model="sale.order", name="partner_id")), self.root)
        self.assertEqual(types[0][0], "odoo/odoo/addons/base/models/res_partner.py")

    def test_call_hierarchy(self):
        rel = "odoo/addons/sale/models/sale_order.py"
        item = prepare(self.ctx(rel, "def action_confirm", offset=4))[0]
        self.assertEqual(item["name"], "sale.order.action_confirm")
        ictx = item_ctx(self.server, self.env, item)
        t = time.perf_counter()
        callers = incoming(ictx, item)
        self.assertLess(time.perf_counter() - t, 4.0)
        self.assertLessEqual(len(callers), 100)
        self.assertIn("payment.transaction._check_amount_and_confirm_order", [c["from"]["name"] for c in callers])
        callees = [c["to"]["name"] for c in outgoing(ictx, item)]
        self.assertIn("sale.order._action_confirm", callees)

    def test_latency(self):
        ctx = self.ctx("odoo/addons/sale/models/sale_order.py")
        targets = [
            Target(FIELD, model="sale.order", name="partner_id"),
            Target(FIELD, model="res.partner", name="name"),
            Target(METHOD, model="sale.order", name="write"),
            Target(MODEL, model="res.partner"),
            Target(XMLID, xmlid="sale.view_order_form"),
        ]
        worst = 0.0
        for target in targets:
            t = time.perf_counter()
            locations_for_target(ctx, target)
            type_locations_for_target(ctx, target)
            worst = max(worst, time.perf_counter() - t)
        self.assertLess(worst, 0.05)


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "16.0")), "no 16.0 checkout")
class RealTree16Test(RealTreeCase):
    VERSION = "16.0"


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "18.0")), "no 18.0 checkout")
class RealTree18Test(RealTreeCase):
    VERSION = "18.0"


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "20.0")), "no 20.0 checkout")
class RealTree20Test(RealTreeCase):
    VERSION = "20.0"


del RealTreeCase  # abstract: only the per-version subclasses run

if __name__ == "__main__":
    unittest.main()
