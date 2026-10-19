"""Tests for odoo_lens.features.symbols: document symbols (file outline) and
workspace symbols (agent-oriented query grammar).

Fixture: tests/fixtures/mini (core, enterprise, a non-customer module) plus a
customer addons root tests/fixtures/symbols/addons (``.odoo-lens.json`` points to
the mini checkout), so customer-first ranking is exercised.

``MoreFixture`` generates a throwaway tree (make_more_tree): a copy of the mini
checkout whose ``odoo.api`` / ``odoo.tools`` re-export like 19.0+, and a customer
module with never-imported files, server actions / crons / rules bound through
``model_id``, ``ir.access`` rows with domains, menus without names, one xml id
spread over several data files, and files whose outline exceeds the size budget.
"""

from __future__ import annotations

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

from odoo_lens.cli import _ctx, headless_server, load_env  # noqa: E402
from odoo_lens.documents import uri_to_path  # noqa: E402
from odoo_lens.extract import extract_source  # noqa: E402
from odoo_lens.features import symbols as S  # noqa: E402
from odoo_lens.protocol import NEVER_CANCELLED  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
MINI = os.path.join(FIX, "mini")
CUSTOM = os.path.join(FIX, "symbols", "addons", "x_sale_ext")
WORKTREES = os.path.expanduser(os.environ.get("ODOO_LENS_WORKTREES", "~/odev/worktrees"))
KIND_NAMES = {1: "File", 2: "Module", 5: "Class", 6: "Method", 8: "Field", 12: "Function", 13: "Variable", 14: "Constant", 19: "Object", 20: "Key"}


def outline(symbols: list[dict], depth: int = 0) -> list[str]:
    """Render DocumentSymbols the way Claude Code prints them."""
    out = []
    for s in symbols:
        line = "  " * depth + f"{s['name']} ({KIND_NAMES.get(s['kind'], s['kind'])})"
        if s.get("detail"):
            line += " " + s["detail"]
        out.append(line + f" - Line {s['range']['start']['line'] + 1}")
        out += outline(s.get("children") or [], depth + 1)
    return out


def found(symbols: list[dict]) -> list[str]:
    """Render workspace symbols as 'name (Kind) basename:L in containerName'."""
    out = []
    for s in symbols:
        loc = s["location"]
        out.append(
            f"{s['name']} ({KIND_NAMES.get(s['kind'], s['kind'])}) {os.path.basename(uri_to_path(loc['uri']))}:"
            f"{loc['range']['start']['line'] + 1} in {s.get('containerName', '')}"
        )
    return out


def render_ws(symbols: list[dict]) -> str:
    """Workspace symbols exactly as Claude Code prints them (grouped by file)."""
    groups: dict[str, list[dict]] = {}
    for s in symbols:
        groups.setdefault(uri_to_path(s["location"]["uri"]), []).append(s)
    out = [f"Found {len(symbols)} symbols in workspace:"]
    for path, items in groups.items():
        out += ["", f"{path}:"]
        for s in items:
            out.append(f"  {s['name']} ({KIND_NAMES.get(s['kind'], s['kind'])}) - Line {s['location']['range']['start']['line'] + 1} in {s.get('containerName', '')}")
    return "\n".join(out)


def render_doc(symbols: list[dict]) -> str:
    return "\n".join(["Document symbols:"] + outline(symbols))


def pos(r: dict, end: bool = False) -> tuple[int, int]:
    p = r["end" if end else "start"]
    return (p["line"], p["character"])


class FixtureEnv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(CUSTOM, "models", "sale_order.py"))
        assert cls.env is not None and os.path.dirname(CUSTOM) in cls.env.custom_roots

    @classmethod
    def tearDownClass(cls):
        cls.env.registry = None  # let the registry go (it is gc-frozen)

    def doc(self, path: str, hierarchical: bool = True) -> list[dict]:
        return S.document_symbols(_ctx(self.server, self.env, path, 1, 1), hierarchical)

    def ws(self, query: str) -> list[dict]:
        return S.workspace_symbols(self.server, self.env, self.env.registry, query, NEVER_CANCELLED)


class DocumentSymbolsTest(FixtureEnv):
    def test_python_model_file(self):
        syms = self.doc(os.path.join(CUSTOM, "models", "sale_order.py"))
        lines = outline(syms)
        self.assertEqual(
            lines[:16],
            [
                "_logger (Variable) - Line 5",
                "X_STATES (Variable) - Line 6",
                "x_helper (Function) (value, digits=2) - Line 9",
                "SaleOrder (Class) extends sale.order - Line 13",
                "  _order (Variable) = 'name desc' - Line 15",
                '  x_reference (Field) Char "X Reference" required - Line 17',
                "  partner_id (Field) Many2one → res.partner, overrides sale - Line 18",
                '  x_note_ids (Field) One2many → x.sale.note "Notes" - Line 19',
                "  x_total (Field) Float compute=_compute_x_total stored - Line 20",
                "  _sql_constraints (Constant) [('x_reference_uniq', 'unique(x_reference)', 'Reference must be unique')] - Line 22",
                # ranges start on the def line, not on the decorator (line 24)
                "  _compute_x_total (Method) @api.depends(order_line.price_subtotal) - Line 25",
                "  action_confirm (Method) overrides sale without super() - Line 29",
                "  create (Method) @api.model_create_multi overrides sale - Line 33",
                "XSaleNote (Class) x.sale.note (defines) + mail.thread - Line 37",
                "  _description (Variable) = 'Sale Note' - Line 40",
                "  order_id (Field) Many2one → sale.order required - Line 42",
            ],
        )
        self.assertIn("XSaleNoteWizard (Class) x.sale.note.wizard (defines, transient) - Line 46", lines)
        self.assertIn("NotAModel (Class) - Line 53", lines)
        self.assertIn("  run (Method) - Line 56", lines)
        self.assertFalse(any("_inherit (" in ln or "_name (" in ln for ln in lines))  # summarized by the class detail
        cls = syms[3]
        self.assertEqual(cls["selectionRange"], {"start": {"line": 12, "character": 6}, "end": {"line": 12, "character": 15}})
        self.assertEqual(pos(cls["range"]), (12, 0))
        self.assertEqual(pos(cls["range"], end=True)[0], 33)

    def test_python_overrides_scope(self):
        # sale_stock calls super(); its scope does not contain sale_subscription
        lines = outline(self.doc(os.path.join(MINI, "odoo", "addons", "sale_stock", "models", "sale_order.py")))
        self.assertIn("  action_confirm (Method) overrides sale - Line 10", lines)
        self.assertIn("  partner_id (Field) Many2one → res.partner, overrides sale - Line 8", lines)
        lines = outline(self.doc(os.path.join(MINI, "odoo", "addons", "sale", "models", "sale_order.py")))
        self.assertIn("  create (Method) @api.model_create_multi overrides BaseModel - Line 35", lines)
        self.assertIn("  action_confirm (Method) - Line 25", lines)  # the original definition
        lines = outline(self.doc(os.path.join(MINI, "odoo", "odoo", "addons", "base", "models", "res_users.py")))
        self.assertIn("Users (Class) res.users (defines), _inherits res.partner via partner_id - Line 12", lines)

    def test_python_controller_routes(self):
        lines = outline(self.doc(os.path.join(CUSTOM, "controllers", "main.py")))
        self.assertEqual(
            lines,
            [
                "XShop (Class) (http.Controller) - Line 5",
                "  _export_route (Variable) = '/x/export' - Line 6",
                "  cart (Method) @route /shop/cart type=http auth=public - Line 9",
                "  orders (Method) @route /x/orders, /x/orders/page/<int:page> type=http auth=user - Line 13",
                "  order (Method) @route /x/orders/<int:order_id> type=jsonrpc auth=user - Line 21",
                "  export (Method) @route /x/export type=http auth=user - Line 25",
                "  helper (Method) - Line 28",
                "XShopExtended (Class) (XShop) - Line 32",
                "  cart (Method) @route (inherited path) - Line 35",
            ],
        )
        # paths from imported module constants, f-strings and route wrappers
        self.assertEqual(
            outline(self.doc(os.path.join(CUSTOM, "controllers", "portal.py"))),
            [
                "x_route (Function) (*args, **kwargs) - Line 8",
                "XPortal (Class) (BaseShop) - Line 12",
                "  webhook (Method) @route /x/webhook type=http auth=public - Line 15",
                "  shop_product (Method) @route /x/shop/<int:product_id> type=http auth=public - Line 19",
                "  orders (Method) @route (inherited path) - Line 23",
                "XPortalMore (Class) (XPortal) - Line 27",
                "  cart (Method) @route (inherited path) - Line 30",
            ],
        )

    def test_xml_records(self):
        syms = self.doc(os.path.join(CUSTOM, "views", "sale_order_views.xml"))
        self.assertEqual(
            outline(syms),
            [
                "view_order_form_x (Object) view of sale.order, inherits sale.view_order_form - Line 3",
                "view_x_sale_note_list (Object) list view of x.sale.note - Line 13",
                'action_x_sale_notes (Object) window action → x.sale.note "Order Notes" - Line 24',
                "menu_x_root (Object) menu: X Sales - Line 28",
                "  menu_x_notes (Object) menu: Order Notes → action_x_sale_notes - Line 29",
                'cart (Object) QWeb template "X Cart" - Line 31',
                "cart_inherit (Object) QWeb template, inherits x_sale_ext.cart - Line 34",
                "note_demo (Object) x.sale.note, noupdate - Line 38",
            ],
        )
        first = syms[0]
        self.assertEqual(pos(first["range"]), (2, 4))  # the '<record' tag
        self.assertEqual(pos(first["range"], end=True)[0], 11)  # '</record>'
        lines = outline(self.doc(os.path.join(MINI, "odoo", "addons", "sale", "views", "sale_order_views.xml")))
        self.assertEqual(lines[0], "view_order_form (Object) form view of sale.order - Line 3")
        self.assertIn('action_orders (Object) window action → sale.order "Sales Orders" - Line 23', lines)
        self.assertIn("menu_sale_order (Object) menu: Orders → action_orders - Line 29", lines)

    def test_owl_templates_and_js(self):
        lines = outline(self.doc(os.path.join(CUSTOM, "static", "src", "xml", "order_widget.xml")))
        # t-inherit-mode="extension" patches web.FormView in place: its t-name is no template
        self.assertEqual(lines, ["x_sale_ext.XOrderWidget (Object) owl template - Line 3", "x_sale_ext.XFormView (Object) owl template extension of web.FormView - Line 6"])
        lines = outline(self.doc(os.path.join(CUSTOM, "static", "src", "js", "order_widget.js")))
        self.assertEqual(
            lines,
            [
                "XOrderWidget (Class) extends Component, owl template: x_sale_ext.XOrderWidget - Line 6",
                "formatXAmount (Function) (value) - Line 11",
                "xOrderWidget (Variable) - Line 15",
            ],
        )

    def test_csv_rows(self):
        lines = outline(self.doc(os.path.join(CUSTOM, "security", "ir.model.access.csv")))
        self.assertEqual(
            lines,
            [
                "access_x_sale_note_user (Object) x.sale.note, group base.group_user, read/write/create - Line 2",
                "access_x_sale_note_wizard (Object) x.sale.note.wizard, no group, read - Line 3",
            ],
        )
        lines = outline(self.doc(os.path.join(MINI, "odoo", "odoo", "addons", "base", "security", "ir.model.access.csv")))
        self.assertEqual(lines[0], "access_res_partner_user (Object) res.partner, group group_user, read/write/create - Line 2")

    def test_manifest(self):
        lines = outline(self.doc(os.path.join(CUSTOM, "__manifest__.py")))
        self.assertEqual(lines[0], "name (Key) X Sale Extension - Line 2")
        self.assertIn("depends (Key) sale, mail - Line 5", lines)
        self.assertIn("  sale (Module) - Line 5", lines)
        self.assertIn("data (Key) 2 files - Line 6", lines)
        self.assertIn("  views/sale_order_views.xml (File) - Line 8", lines)
        self.assertIn("assets (Key) web.assets_backend (2) - Line 10", lines)
        self.assertIn("installable (Key) True - Line 16", lines)

    def test_flat_symbols(self):
        flat = self.doc(os.path.join(CUSTOM, "models", "sale_order.py"), hierarchical=False)
        names = {(s["name"], s.get("containerName")) for s in flat}
        self.assertIn(("SaleOrder", None), names)
        self.assertIn(("x_reference", "SaleOrder"), names)
        self.assertIn(("run", "NotAModel"), names)
        self.assertTrue(all("location" in s and "range" in s["location"] for s in flat))

    def test_ranges_contain_selections_and_children(self):
        def check(symbols, parent=None):
            for s in symbols:
                r, sel = s["range"], s["selectionRange"]
                self.assertLessEqual(pos(r), pos(sel), s["name"])
                self.assertLessEqual(pos(sel, True), pos(r, True), s["name"])
                if parent is not None:
                    self.assertLessEqual(pos(parent), pos(r), s["name"])
                    self.assertLessEqual(pos(r, True), pos(parent, True), s["name"])
                check(s.get("children") or [], r)

        for d, _, files in os.walk(CUSTOM):
            for f in files:
                check(self.doc(os.path.join(d, f)))
        check(self.doc(os.path.join(MINI, "odoo", "odoo", "models.py")))

    def test_unsupported_file(self):
        self.assertEqual(self.doc(os.path.join(FIX, "symbols", ".odoo-lens.json")), [])


class WorkspaceSymbolsTest(FixtureEnv):
    def test_model(self):
        res = found(self.ws("sale.order"))
        self.assertEqual(
            res,
            [
                "sale.order (Class) sale_order.py:5 in sale (defines)",
                "sale.order (Class) sale_order.py:4 in my_module (extends)",  # customer extensions first (load order)
                "sale.order (Class) sale_order.py:13 in x_sale_ext (extends)",
                "sale.order (Class) sale_order.py:4 in sale_stock (extends)",
                "sale.order (Class) sale_order.py:11 in sale_subscription (extends)",
                "sale.order.line (Class) sale_order_line.py:4 in sale (defines)",  # other models with the prefix
            ],
        )
        self.assertEqual(found(self.ws("model:x.sale.note")), ["x.sale.note (Class) sale_order.py:37 in x_sale_ext (defines)", "x.sale.note.wizard (Class) sale_order.py:46 in x_sale_ext (defines, transient)"])
        self.assertEqual(found(self.ws("model:note.wiz")), ["x.sale.note.wizard (Class) sale_order.py:46 in x_sale_ext (defines, transient)"])

    def test_model_member(self):
        expected = [
            "partner_id (Field) sale_order.py:12 in sale.order (sale) Many2one→res.partner",
            "partner_id (Field) sale_order.py:18 in sale.order (x_sale_ext) Many2one",
            "partner_id (Field) sale_order.py:8 in sale.order (sale_stock) Many2one",
        ]
        for q in ("sale.order.partner_id", "sale.order/partner_id", "sale.order:partner_id", "sale.order partner_id", "field:sale.order.partner_id"):
            self.assertEqual(found(self.ws(q)), expected, q)
        self.assertEqual(
            found(self.ws("method:sale.order.create")),
            [
                "create (Method) sale_order.py:35 in sale.order (sale) method",
                "create (Method) sale_order.py:33 in sale.order (x_sale_ext) method",
                "create (Method) models.py:30 in BaseModel (odoo) method",  # inherited definitions after the model's own
            ],
        )
        # field paths are walked; _inherits fields are delegated
        self.assertEqual(found(self.ws("sale.order.partner_id.country_id")), ["country_id (Field) res_partner.py:14 in res.partner (base) Many2one→res.country"])
        self.assertEqual(found(self.ws("res.users.name")), ["name (Field) res_partner.py:10 in res.partner (base) Char, via partner_id"])
        self.assertEqual(found(self.ws("sale.order.message_ids")), ["message_ids (Field) mail_thread.py:8 in mail.thread (mail) One2many→res.partner"])
        self.assertEqual(found(self.ws("sale.order.amo")), ["amount_total (Field) sale_order.py:16 in sale.order (sale) Monetary compute=_compute_amounts", "_compute_amounts (Method) sale_order.py:21 in sale.order (sale) method"])
        self.assertEqual(found(self.ws("sale.order.nosuchmember")), [])

    def test_model_members_listing(self):
        res = found(self.ws("sale.order."))
        self.assertLessEqual(len(res), S.WORKSPACE_CAP)
        self.assertEqual(res[0], "x_note (Field) sale_order.py:7 in sale.order (my_module) Text")  # customer fields first
        self.assertIn("x_reference (Field) sale_order.py:17 in sale.order (x_sale_ext) Char", res[:12])
        self.assertIn("order_line (Field) sale_order.py:15 in sale.order (sale) One2many→sale.order.line", res)
        self.assertIn("message_post (Method) mail_thread.py:10 in mail.thread (mail) method", res)
        fields = [r for r in res if "(Field)" in r]
        self.assertLess(res.index(fields[-1]), res.index("action_confirm (Method) sale_order.py:25 in sale.order (sale) method") + 100)
        members = found(self.ws("sale.order:"))
        self.assertEqual(res[: len(members)], members)
        self.assertEqual(res[len(members) :], ["sale.order.line (Class) sale_order_line.py:4 in sale (defines)"])  # sub-models when room is left

    def test_plain_member(self):
        res = found(self.ws("action_confirm"))
        # one chain (sale.order): the definition first, then the customer override, then load order
        self.assertEqual(
            res,
            [
                "action_confirm (Method) sale_order.py:25 in sale.order (sale) method",
                "action_confirm (Method) sale_order.py:17 in sale.order (my_module) method",
                "action_confirm (Method) sale_order.py:29 in sale.order (x_sale_ext) method",
                "action_confirm (Method) sale_order.py:10 in sale.order (sale_stock) method",
                "action_confirm (Method) sale_order.py:17 in sale.order (sale_subscription) method",
            ],
        )
        res = found(self.ws("partner_id"))
        self.assertEqual(res[0], "partner_id (Field) sale_order.py:12 in sale.order (sale) Many2one→res.partner")  # chain holding the customer field
        self.assertEqual(res[1], "partner_id (Field) sale_order.py:18 in sale.order (x_sale_ext) Many2one")
        self.assertIn("partner_id (Field) res_users.py:17 in res.users (base) Many2one→res.partner", res)
        self.assertEqual(found(self.ws("field:partner_id")), res)
        self.assertEqual(found(self.ws("search")), ["search (Method) models.py:43 in BaseModel (odoo) method", "search_count (Method) models.py:46 in BaseModel (odoo) method"])

    def test_records(self):
        self.assertEqual(
            found(self.ws("sale.view_order_form")),
            ["sale.view_order_form (Object) sale_order_views.xml:3 in ir.ui.view (sale) form view of sale.order"],  # exact: no fuzzy noise
        )
        res = found(self.ws("view_order_form"))  # local ids: exact first, then prefix
        self.assertEqual(res[0], "sale.view_order_form (Object) sale_order_views.xml:3 in ir.ui.view (sale) form view of sale.order")
        self.assertEqual(res[1], "my_module.view_order_form_inherit_my_module (Object) sale_order_views.xml:3 in ir.ui.view (my_module) view of sale.order, inherits sale.view_order_form")
        self.assertEqual(res[2], "x_sale_ext.view_order_form_x (Object) sale_order_views.xml:3 in ir.ui.view (x_sale_ext) view of sale.order, inherits sale.view_order_form")
        self.assertEqual(len(res), 4)
        self.assertEqual(found(self.ws("sale.model_sale_order")), ["sale.model_sale_order (Object) sale_order.py:5 in ir.model (sale) implicit xml id of model sale.order"])
        self.assertEqual(found(self.ws("xmlid:main_company")), ['base.main_company (Object) res_company_data.xml:8 in res.company (base) "My Company", noupdate'])
        self.assertEqual(
            found(self.ws("x_sale_ext.access_x_sale_note_user")),  # ACL rows say what they grant
            ['x_sale_ext.access_x_sale_note_user (Object) ir.model.access.csv:2 in ir.model.access (x_sale_ext) "x.sale.note user": x.sale.note, group base.group_user, read/write/create'],
        )

    def test_record_kinds(self):
        self.assertEqual(
            found(self.ws("view:sale.order")),
            [
                "sale.view_order_form (Object) sale_order_views.xml:3 in ir.ui.view (sale) form view of sale.order",  # primary first
                "my_module.view_order_form_inherit_my_module (Object) sale_order_views.xml:3 in ir.ui.view (my_module) view of sale.order, inherits sale.view_order_form",
                "x_sale_ext.view_order_form_x (Object) sale_order_views.xml:3 in ir.ui.view (x_sale_ext) view of sale.order, inherits sale.view_order_form",
                "sale_stock.view_order_form_inherit_sale_stock (Object) sale_order_views.xml:3 in ir.ui.view (sale_stock) view of sale.order, inherits sale.view_order_form",
            ],
        )
        self.assertEqual(found(self.ws("action:sale.order")), ['sale.action_orders (Object) sale_order_views.xml:23 in ir.actions.act_window (sale) window action → sale.order "Sales Orders"'])
        self.assertEqual(found(self.ws("action:customers")), ['base.action_partner_form (Object) res_partner_views.xml:20 in ir.actions.act_window (base) window action → res.partner "Customers"'])
        self.assertEqual(found(self.ws("menu:order notes")), ["x_sale_ext.menu_x_notes (Object) sale_order_views.xml:29 in ir.ui.menu (x_sale_ext) menu: Order Notes → x_sale_ext.action_x_sale_notes"])
        self.assertEqual(
            found(self.ws("Order Notes")),  # several words: menus and actions by display name
            [
                "x_sale_ext.menu_x_notes (Object) sale_order_views.xml:29 in ir.ui.menu (x_sale_ext) menu: Order Notes → x_sale_ext.action_x_sale_notes",
                'x_sale_ext.action_x_sale_notes (Object) sale_order_views.xml:24 in ir.actions.act_window (x_sale_ext) window action → x.sale.note "Order Notes"',
            ],
        )
        self.assertEqual(
            found(self.ws("template:cart")),
            [
                'x_sale_ext.cart (Object) sale_order_views.xml:31 in ir.ui.view (x_sale_ext) QWeb template "X Cart"',
                "x_sale_ext.cart_inherit (Object) sale_order_views.xml:34 in ir.ui.view (x_sale_ext) QWeb template, inherits x_sale_ext.cart",
            ],
        )
        self.assertEqual(found(self.ws("template:XFormView")), ["x_sale_ext.XFormView (Object) order_widget.xml:6 in owl template extension (x_sale_ext) of web.FormView"])

    def test_routes(self):
        self.assertEqual(
            found(self.ws("/shop/cart")),
            [
                "cart (Method) main.py:9 in route /shop/cart (x_sale_ext) type=http auth=public",
                "cart (Method) main.py:35 in route /shop/cart (x_sale_ext) type=http auth=public, overrides XShop.cart (x_sale_ext)",
                # @route() through an ancestor imported under an alias (XPortal(BaseShop) -> XShop)
                "cart (Method) portal.py:30 in route /shop/cart (x_sale_ext) type=http auth=public, overrides XShop.cart (x_sale_ext)",
            ],
        )
        self.assertEqual(found(self.ws("/x/webhook")), ["webhook (Method) portal.py:15 in route /x/webhook (x_sale_ext) type=http auth=public"])  # const.WEBHOOK_ROUTE
        self.assertEqual(found(self.ws("/x/shop/12")), ["shop_product (Method) portal.py:19 in route /x/shop/<int:product_id> (x_sale_ext) type=http auth=public"])  # f-string, x_route
        self.assertEqual(
            found(self.ws("/x/orders"))[:2],
            [
                "orders (Method) main.py:13 in route /x/orders (x_sale_ext) type=http auth=user",
                "orders (Method) portal.py:23 in route /x/orders (x_sale_ext) type=http auth=user, overrides XShop.orders (x_sale_ext)",
            ],
        )
        self.assertEqual(found(self.ws("/x/orders/42")), ["order (Method) main.py:21 in route /x/orders/<int:order_id> (x_sale_ext) type=jsonrpc auth=user"])  # converter pattern
        self.assertEqual(
            found(self.ws("https://example.com/x/orders?page=2")),
            [
                "orders (Method) main.py:13 in route /x/orders (x_sale_ext) type=http auth=user",  # exact, then prefix
                "orders (Method) portal.py:23 in route /x/orders (x_sale_ext) type=http auth=user, overrides XShop.orders (x_sale_ext)",
                "order (Method) main.py:21 in route /x/orders/<int:order_id> (x_sale_ext) type=jsonrpc auth=user",
            ],
        )
        self.assertEqual(found(self.ws("/x/export")), ["export (Method) main.py:25 in route /x/export (x_sale_ext) type=http auth=user"])  # class attribute
        self.assertEqual([s["name"] for s in self.ws("route:orders")], ["orders", "order", "orders"])
        self.assertEqual(self.ws("/no/such/route"), [])

    def test_python_js_modules(self):
        self.assertEqual(
            found(self.ws("class:SaleOrder"))[:3],
            [
                "SaleOrder (Class) sale_order.py:4 in odoo.addons.my_module.models.sale_order: sale.order (extends)",
                "SaleOrder (Class) sale_order.py:13 in odoo.addons.x_sale_ext.models.sale_order: sale.order (extends)",
                "SaleOrder (Class) sale_order.py:5 in odoo.addons.sale.models.sale_order: sale.order (defines)",
            ],
        )
        self.assertEqual(found(self.ws("def:x_helper")), ["x_helper (Function) sale_order.py:9 in odoo.addons.x_sale_ext.models.sale_order"])
        self.assertEqual(found(self.ws("export")), ["export (Method) main.py:25 in XShop (odoo.addons.x_sale_ext.controllers.main)"])
        self.assertEqual(found(self.ws("x_test_helper")), [])  # tests/ are not loaded by Odoo
        self.assertEqual(found(self.ws("TestXThing")), [])
        self.assertEqual(
            found(self.ws("XOrderWidget")),
            [
                "x_sale_ext.XOrderWidget (Object) order_widget.xml:3 in owl template (x_sale_ext)",
                "XOrderWidget (Class) order_widget.js:6 in @x_sale_ext/js/order_widget",
                "xOrderWidget (Variable) order_widget.js:15 in @x_sale_ext/js/order_widget",
            ],
        )
        self.assertEqual(found(self.ws("formatxamount")), ["formatXAmount (Function) order_widget.js:11 in @x_sale_ext/js/order_widget"])
        self.assertEqual(found(self.ws("odoo.tools.float_utils.float_round")), ["float_round (Function) float_utils.py:1 in odoo.tools.float_utils"])
        self.assertEqual(found(self.ws("odoo.addons.sale.models.sale_order")), ["odoo.addons.sale.models.sale_order (Module) sale_order.py:1 in odoo.addons.sale.models.sale_order"])
        self.assertEqual(
            found(self.ws("module:sale")),
            [
                'sale (Module) __manifest__.py:2 in "Sales" depends product, mail',
                'sale_stock (Module) __manifest__.py:2 in "Sales and Warehouse Management" depends sale',
                'sale_subscription (Module) __manifest__.py:2 in "Subscriptions" depends sale',
                'x_sale_ext (Module) __manifest__.py:2 in "X Sale Extension" depends sale, mail',
            ],
        )

    def test_fuzzy_and_edge_queries(self):
        res = found(self.ws("actconf"))  # an abbreviation: every run of characters starts a word
        self.assertTrue(res and all(r.endswith("[fuzzy match]") for r in res))
        self.assertIn("action_confirm (Method) sale_order.py:25 in sale.order (sale) method [fuzzy match]", res)
        for q in ("", "   ", "zzzqqq", "field:", "nosuch.model.field", "acnfrm"):  # (scattered characters: noise)
            self.assertEqual(self.ws(q), [], q)
        self.assertEqual(found(self.ws("'action_confirm()'")), found(self.ws("action_confirm")))

    def test_cap_and_determinism(self):
        ix = S._index(self.env, self.env.registry)
        search = S._Search(self.server, ix, NEVER_CANCELLED, cap=3)
        search.run("sale.order")
        res = search.symbols()
        self.assertEqual(len(res), 3)
        self.assertEqual((res[-1]["name"], res[-1]["containerName"]), ("(more results not listed)", S._NARROW))  # a truncated answer says so
        self.assertEqual(self.ws("a"), self.ws("a"))
        self.assertLessEqual(len(self.ws("a")), S.WORKSPACE_CAP)

    def test_output_size_budget(self):
        # doc-outline-over-50k: answers stop well before Claude Code's 50k chars (budget shrunk here)
        with mock.patch.object(S, "OUTPUT_BUDGET", 1500):
            res = self.ws("sale.order.")
        self.assertLess(len(render_ws(res)), 1500 + 300)
        self.assertRegex(res[-1]["name"], r"^\(\d+ more results not listed: the answer stops at ~1k chars\)$")
        self.assertGreater(len(self.ws("sale.order.")), len(res))

    def test_member_listing_keeps_every_kind(self):
        # ws-member-listing-truncated: a listing too long for the cap still shows methods
        # and inherited members, and ends with a marker counting what was left out
        ix = S._index(self.env, self.env.registry, self.server)
        search = S._Search(self.server, ix, NEVER_CANCELLED, cap=12)
        search.run("sale.order.")
        res = found(search.symbols())
        self.assertEqual(len(res), 12)
        self.assertEqual(res[0], "x_note (Field) sale_order.py:7 in sale.order (my_module) Text")  # customer members first
        self.assertTrue(any("(Method)" in r and "x_sale_ext" not in r and "my_module" not in r for r in res[:-1]), res)
        self.assertTrue(any(" in mail.thread (mail) " in r or " in BaseModel (odoo) " in r for r in res[:-1]), res)  # inherited
        self.assertRegex(
            res[-1],
            r"^\(\d+ more members not listed: \d+ fields, \d+ methods(, \d+ attributes)?\) \(Object\) sale_order.py:5 in "
            r"narrow with sale.order.<prefix> / field:sale.order.<prefix> / method:sale.order.<prefix>$",
        )
        search = S._Search(self.server, ix, NEVER_CANCELLED, cap=6)
        search.run("method:sale.order.")
        res = found(search.symbols())
        self.assertTrue(all("(Method)" in r for r in res[:-1]))
        self.assertRegex(res[-1], r"^\(\d+ more members not listed: \d+ methods\) .* in narrow with method:sale.order.<prefix>$")
        self.assertFalse(any("not listed" in r for r in found(self.ws("sale.order."))))  # fits: no marker

    def test_typos_and_abbreviations(self):
        # ws-fuzzy-flood: only when nothing else matched; one-typo-away names first; capped
        res = found(self.ws("action_confrim"))  # swapped letters
        self.assertEqual(res[0], "action_confirm (Method) sale_order.py:25 in sale.order (sale) method [fuzzy match]")
        self.assertLessEqual(len(res), S.FUZZY_CAP)
        self.assertEqual(found(self.ws("partner_idd"))[0], "partner_id (Field) sale_order.py:12 in sale.order (sale) Many2one→res.partner [fuzzy match]")
        self.assertEqual(found(self.ws("res.partnr")), ["res.partner (Class) res_partner.py:4 in base (defines) [fuzzy match]"])
        self.assertEqual(found(self.ws("sale.order.amout_total")), ["amount_total (Field) sale_order.py:16 in sale.order (sale) Monetary compute=_compute_amounts [fuzzy match]"])
        self.assertIn("amount_total (Field) sale_order.py:16 in sale.order (sale) Monetary compute=_compute_amounts [fuzzy match]", found(self.ws("sale.order.amnt")))
        self.assertFalse(any("[fuzzy match]" in r for r in found(self.ws("XOrder"))))  # a prefix matched: no guesses

    def test_model_names_any_case(self):
        # ws-case-and-implicit-ids: model names are lowercase whatever the query's case
        self.assertEqual(found(self.ws("Sale.Order")), found(self.ws("sale.order")))
        self.assertEqual(found(self.ws("Res.Partner")), found(self.ws("res.partner")))
        member = found(self.ws("sale.order.partner_id"))
        for q in ("Sale.Order.partner_id", "SALE.ORDER:partner_id", "field:Sale.Order.partner_id"):
            self.assertEqual(found(self.ws(q)), member, q)
        self.assertEqual(found(self.ws("view:Sale.Order")), found(self.ws("view:sale.order")))
        self.assertEqual(found(self.ws("action:Sale.Order")), found(self.ws("action:sale.order")))
        # bare implicit ids: the module defining the model (or field) first
        res = found(self.ws("model_sale_order"))
        self.assertEqual(res[0], "sale.model_sale_order (Object) sale_order.py:5 in ir.model (sale) implicit xml id of model sale.order")
        self.assertIn("x_sale_ext.model_sale_order (Object) sale_order.py:13 in ir.model (x_sale_ext) implicit xml id of model sale.order", res)
        self.assertEqual(
            found(self.ws("field_sale_order__partner_id"))[0],
            "sale.field_sale_order__partner_id (Object) sale_order.py:12 in ir.model.fields (sale) implicit xml id of field sale.order.partner_id",
        )
        self.assertEqual(found(self.ws("xmlid:model_sale_order"))[0], res[0])

    def test_model_spelled_with_spaces(self):
        # ws-multiword-model: "sale order" is the model, not only menus / actions labels
        classes = [r for r in found(self.ws("sale.order")) if r.startswith("sale.order (Class)")]
        self.assertEqual(found(self.ws("sale order")), classes)
        self.assertEqual(found(self.ws("Sale Order")), classes)
        self.assertEqual(found(self.ws("sale order line")), ["sale.order.line (Class) sale_order_line.py:4 in sale (defines)"])
        self.assertEqual(found(self.ws("res partner"))[0], found(self.ws("res.partner"))[0])
        self.assertEqual(len(self.ws("Order Notes")), 2)  # labels as before
        self.assertEqual(found(self.ws("sale.order partner_id")), found(self.ws("sale.order.partner_id")))  # a dotted head: a member

    def test_code_shaped_queries(self):
        # ws-code-shaped-queries: code as Claude writes it
        member = found(self.ws("sale.order.action_confirm"))
        self.assertEqual(found(self.ws("SaleOrder.action_confirm")), member)  # class name . member
        self.assertEqual(found(self.ws("self.env['sale.order']")), found(self.ws("sale.order")))
        self.assertEqual(found(self.ws('request.env["sale.order"].action_confirm()')), member)
        self.assertEqual(found(self.ws("def action_confirm(self):")), found(self.ws("def:action_confirm")))
        self.assertEqual(found(self.ws("class SaleOrder(models.Model):")), found(self.ws("class:SaleOrder")))
        plain = found(self.ws("action_confirm"))
        for q in ("action_confirm(", "action_confirm(self, force=True)", "self.action_confirm()"):
            self.assertEqual(found(self.ws(q)), plain, q)
        self.assertEqual(found(self.ws("fields.Many2one")), ["Many2one (Class) fields.py:59 in odoo.fields"])
        self.assertEqual(found(self.ws("models.Model")), ["Model (Class) models.py:89 in odoo.models"])
        self.assertEqual(found(self.ws("@api.depends('order_line')")), ["depends (Function) api.py:1 in odoo.api"])
        self.assertEqual(found(self.ws("self.env.ref('base.main_company')")), found(self.ws("base.main_company")))
        self.assertEqual(found(self.ws("@http.route('/shop/cart', type='http')")), found(self.ws("/shop/cart")))
        self.assertEqual(found(self.ws("XShop.cart")), ["cart (Method) main.py:9 in XShop (odoo.addons.x_sale_ext.controllers.main)"])
        # a method inherited from a python base class is found on it
        self.assertEqual(found(self.ws("XPortalMore.orders")), ["orders (Method) portal.py:23 in XPortal (odoo.addons.x_sale_ext.controllers.portal)"])

    def test_re_exported_python_names(self):
        # ws-python-reexports: odoo/tools/__init__.py re-exports float_compare (from .float_utils import ...)
        expected = ["float_compare (Function) float_utils.py:5 in odoo.tools.float_utils, re-exported by odoo.tools"]
        self.assertEqual(found(self.ws("odoo.tools.float_compare")), expected)
        self.assertEqual(found(self.ws("tools.float_compare")), expected)

    def test_route_language_prefix(self):
        # ws-route-lang-prefix: http_routing serves /fr/shop/cart with /shop/cart
        plain = [s["containerName"] for s in self.ws("/shop/cart")]
        for q, lang in (("/fr/shop/cart", "fr"), ("/fr_BE/shop/cart", "fr_BE"), ("https://example.com/es_419/shop/cart?x=1", "es_419"), ("/sr@latin/shop/cart", "sr@latin")):
            self.assertEqual([s["containerName"] for s in self.ws(q)], [f"{c}, lang prefix /{lang} stripped" for c in plain], q)
        self.assertEqual(found(self.ws("/fr/x/orders/42")), ["order (Method) main.py:21 in route /x/orders/<int:order_id> (x_sale_ext) type=jsonrpc auth=user, lang prefix /fr stripped"])
        for q in ("/fr/no/such/route", "/shop/fr/cart"):
            self.assertEqual(self.ws(q), [], q)

    def test_symbol_information_shape(self):
        for s in self.ws("sale.order") + self.ws("/shop/cart") + self.ws("view_order_form"):
            self.assertEqual(set(s), {"name", "kind", "location", "containerName"})
            self.assertTrue(s["location"]["uri"].startswith("file://"))
            self.assertIn("range", s["location"])


# ---------------------------------------------------------------------------
# a generated tree: a copy of the mini checkout whose ``odoo.api`` / ``odoo.tools``
# re-export like 19.0+ (``from odoo.orm.decorators import depends``, ``from .misc
# import *``) and a customer module ``x_more`` exercising the rest


def _write(root: str, rel: str, text: str) -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(text).lstrip("\n"))
    return path


def make_more_tree(root: str) -> dict[str, str]:
    shutil.copytree(os.path.join(MINI, "odoo"), os.path.join(root, "odoo"), ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(root, "odoo", "odoo", "api.py"), encoding="utf-8") as fh:
        api = fh.read()
    os.remove(os.path.join(root, "odoo", "odoo", "api.py"))
    api = api.replace("def depends(*args):\n    return lambda f: f\n", "")
    assert "def depends(" not in api, "tests/fixtures/mini/odoo/odoo/api.py changed: update make_more_tree"
    _write(root, "odoo/odoo/api/__init__.py", "from odoo.orm.decorators import depends\n\n" + api)
    _write(root, "odoo/odoo/orm/__init__.py", "")
    _write(root, "odoo/odoo/orm/decorators.py", "def depends(*args):\n    return lambda f: f\n")
    _write(root, "odoo/odoo/tools/__init__.py", "from .misc import *\nfrom . import translate\n")
    _write(root, "odoo/odoo/tools/misc.py", "def float_compare(value1, value2, precision_digits=None):\n    return 0\n")
    _write(root, ".odoo-lens.json", '{"odoo": "odoo"}\n')
    m = "addons/x_more/"
    paths = {
        "manifest": _write(
            root,
            m + "__manifest__.py",
            """
            {
                'name': 'X More',
                'version': '18.0.1.0.0',
                'depends': ['sale'],
                'data': [
                    'security/x_groups.xml',
                    'security/ir.access.csv',
                    'views/x_more_views.xml',
                    'data/a_update.xml',
                ],
                'demo': ['demo/a_demo.xml'],
                'license': 'LGPL-3',
            }
            """,
        ),
        "init": _write(root, m + "__init__.py", "from . import models\nfrom . import tools\n"),  # controllers/ never imported
        "models_init": _write(root, m + "models/__init__.py", "from . import sale_order\n# from . import dead_order\n"),
        "live": _write(
            root,
            m + "models/sale_order.py",
            """
            from odoo import fields, models


            class SaleOrder(models.Model):
                _inherit = 'sale.order'

                x_more_note = fields.Char()

                def x_more_action(self):
                    return True
            """,
        ),
        "dead": _write(
            root,
            m + "models/dead_order.py",
            """
            from odoo import fields, models


            def dead_helper():
                return 1


            class DeadOrder(models.Model):
                _inherit = 'sale.order'

                x_dead_note = fields.Char()

                def action_confirm(self):
                    return super().action_confirm()


            class DeadHelper:
                def run(self):
                    return 1
            """,
        ),
        "controllers_init": _write(root, m + "controllers/__init__.py", "from . import webhook\n"),
        "webhook": _write(
            root,
            m + "controllers/webhook.py",
            """
            from odoo import http


            class XMoreWebhook(http.Controller):
                @http.route('/x_more/webhook', type='http', auth='public', csrf=False)
                def webhook(self, **kw):
                    return ''
            """,
        ),
        "groups": _write(
            root,
            m + "security/x_groups.xml",
            """
            <?xml version="1.0" encoding="utf-8"?>
            <odoo>
                <record id="group_x" model="res.groups">
                    <field name="name">X Users</field>
                </record>
            </odoo>
            """,
        ),
        "update": _write(
            root,
            m + "data/a_update.xml",
            """
            <?xml version="1.0" encoding="utf-8"?>
            <odoo>
                <record id="group_x" model="res.groups">
                    <field name="comment">Updated by a later data file</field>
                </record>
            </odoo>
            """,
        ),
        "demo": _write(
            root,
            m + "demo/a_demo.xml",
            """
            <?xml version="1.0" encoding="utf-8"?>
            <odoo>
                <data noupdate="1">
                    <record id="group_x" model="res.groups">
                        <field name="comment">Demo</field>
                    </record>
                </data>
            </odoo>
            """,
        ),
        "access": _write(
            root,
            m + "security/ir.access.csv",
            """
            id,name,model_id,group_id/id,operation,domain
            access_x_order_user,x orders user,sale.order,base.group_user,r,"[('state', '=', 'sale')]"
            x_order_restriction,x orders restriction,sale.order,,crud,"[('company_id', 'in', company_ids)]"
            access_x_order_manager,x orders manager,sale.order,base.group_system,crud,
            """,
        ),
        "views": _write(
            root,
            m + "views/x_more_views.xml",
            """
            <?xml version="1.0" encoding="utf-8"?>
            <odoo>
                <record id="action_server_mark_x" model="ir.actions.server">
                    <field name="name">Mark as X</field>
                    <field name="model_id" ref="sale.model_sale_order"/>
                    <field name="binding_model_id" ref="sale.model_sale_order"/>
                    <field name="state">code</field>
                    <field name="code">records.x_more_action()</field>
                </record>
                <record id="cron_x" model="ir.cron">
                    <field name="name">X: refresh orders</field>
                    <field name="model_id" ref="model_sale_order"/>
                    <field name="state">code</field>
                    <field name="code">model.x_more_action()</field>
                </record>
                <record id="rule_x_order" model="ir.rule">
                    <field name="name">X orders: own company</field>
                    <field name="model_id" ref="sale.model_sale_order"/>
                    <field name="domain_force">[('company_id', 'in', company_ids)]</field>
                </record>
                <record id="action_x_orders" model="ir.actions.act_window">
                    <field name="name">X Orders</field>
                    <field name="res_model">sale.order</field>
                </record>
                <record id="action_sale_order_lines_x" model="ir.actions.act_window">
                    <field name="name">Sale Order Lines X</field>
                    <field name="res_model">sale.order.line</field>
                </record>
                <record id="action_report_x" model="ir.actions.report">
                    <field name="name">X Report</field>
                    <field name="model">sale.order</field>
                    <field name="report_name">x_more.report_x</field>
                </record>
                <menuitem id="menu_x_orders" action="action_x_orders" parent="sale.sale_menu_root"/>
                <menuitem id="menu_x_sales" action="sale.action_orders" parent="sale.sale_menu_root"/>
                <menuitem id="menu_x_report" action="action_report_x"/>
            </odoo>
            """,
        ),
        "tools_init": _write(root, m + "tools/__init__.py", "from . import big_helpers\n"),
    }
    # big files (doc-outline-over-50k): each renders far beyond 50k chars
    methods = "".join(f"    def helper_method_number_{n:04d}(self, value, other=None):\n        return value\n\n" for n in range(1500))
    paths["big_py"] = _write(root, m + "tools/big_helpers.py", "class BigHelper:\n" + methods)
    funcs = "".join(f"export function bigWidgetHelper{n:04d}(alpha, beta, gamma) {{\n    return alpha;\n}}\n" for n in range(2500))
    paths["big_js"] = _write(root, m + "static/src/js/big_widgets.js", funcs)
    records = "".join(
        f'    <record id="x_more_partner_with_a_rather_long_identifier_{n:04d}" model="res.partner">\n'
        f"        <field name=\"name\">A partner with a fairly long display name, number {n:04d}</field>\n"
        "    </record>\n"
        for n in range(900)
    )
    paths["big_xml"] = _write(root, m + "data/big_partners.xml", '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n' + records + "</odoo>\n")
    rows = "".join(f"x_more_bank_{n:04d},A bank with a long name for the outline budget test number {n:04d}\n" for n in range(1200))
    paths["big_csv"] = _write(root, m + "data/res.bank.csv", "id,name\n" + rows)
    return paths


def load_uncached(server, path: str):
    """load_env for a throwaway tree: no cache shard written for it."""
    old = os.environ.get("ODOO_LENS_NO_CACHE")
    os.environ["ODOO_LENS_NO_CACHE"] = "1"
    try:
        return load_env(server, path)
    finally:
        if old is None:
            os.environ.pop("ODOO_LENS_NO_CACHE", None)
        else:
            os.environ["ODOO_LENS_NO_CACHE"] = old


class MoreFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-symbols-")
        cls.paths = make_more_tree(cls.tmp)
        cls.server = headless_server()
        cls.env, _ = load_uncached(cls.server, cls.paths["live"])
        assert cls.env is not None and cls.env.registry.module("x_more") is not None

    @classmethod
    def tearDownClass(cls):
        cls.env.registry = None
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ws(self, query: str) -> list[dict]:
        return S.workspace_symbols(self.server, self.env, self.env.registry, query, NEVER_CANCELLED)

    def doc(self, path: str) -> list[dict]:
        return S.document_symbols(_ctx(self.server, self.env, path, 1, 1), True)


class MoreSymbolsTest(MoreFixture):
    def test_never_imported_files(self):
        # ws-dead-classes: models/dead_order.py is not imported by models/__init__.py
        self.assertEqual(self.ws("DeadOrder"), [])  # a model class Odoo never registers
        self.assertEqual(self.ws("class:DeadOrder"), [])
        self.assertEqual(self.ws("x_dead_note"), [])
        self.assertFalse(any(uri_to_path(s["location"]["uri"]) == self.paths["dead"] for s in self.ws("sale.order")))
        res = found(self.ws("SaleOrder"))
        self.assertEqual(res[0], "SaleOrder (Class) sale_order.py:4 in odoo.addons.x_more.models.sale_order: sale.order (extends)")  # customer first
        self.assertFalse(any("dead_order" in r for r in res), res)
        # other code of such files stays findable (scripts, IoT drivers...), flagged
        self.assertEqual(found(self.ws("DeadHelper")), ["DeadHelper (Class) dead_order.py:17 in odoo.addons.x_more.models.dead_order, not imported by its module"])
        self.assertEqual(found(self.ws("dead_helper")), ["dead_helper (Function) dead_order.py:4 in odoo.addons.x_more.models.dead_order, not imported by its module"])
        self.assertEqual(
            outline(self.doc(self.paths["dead"])),
            [
                "dead_helper (Function) () - Line 4",
                "DeadOrder (Class) extends sale.order, not imported: never loaded - Line 8",
                "  x_dead_note (Field) Char - Line 11",
                "  action_confirm (Method) - Line 13",
                "DeadHelper (Class) not imported by its module - Line 17",
                "  run (Method) - Line 18",
            ],
        )
        self.assertNotIn("not imported", outline(self.doc(self.paths["live"]))[0])
        # a route of a controller file never imported is not served: flagged
        self.assertEqual(
            found(self.ws("/x_more/webhook")),
            ["webhook (Method) webhook.py:6 in route /x_more/webhook (x_more) type=http auth=public, controller file not imported by its module: route never registered"],
        )
        self.assertEqual(outline(self.doc(self.paths["webhook"]))[0], "XMoreWebhook (Class) (http.Controller), not imported: never loaded - Line 4")

    def test_actions_bound_through_model_id(self):
        # ws-action-server-actions: server actions and crons name their model with model_id
        res = found(self.ws("action:sale.order"))
        self.assertIn('x_more.action_server_mark_x (Object) x_more_views.xml:3 in ir.actions.server (x_more) server action → sale.order "Mark as X", in the Action menu of sale.order', res)
        self.assertIn('x_more.cron_x (Object) x_more_views.xml:10 in ir.cron (x_more) scheduled action → sale.order "X: refresh orders"', res)
        self.assertIn('x_more.action_report_x (Object) x_more_views.xml:29 in ir.actions.report (x_more) report → sale.order "X Report"', res)
        self.assertIn('sale.action_orders (Object) sale_order_views.xml:23 in ir.actions.act_window (sale) window action → sale.order "Sales Orders"', res)
        self.assertNotIn("x_more.action_sale_order_lines_x", "".join(res))  # → sale.order.line
        self.assertEqual(found(self.ws("x_more.rule_x_order")), ['x_more.rule_x_order (Object) x_more_views.xml:16 in ir.rule (x_more) "X orders: own company" on sale.order'])
        lines = outline(self.doc(self.paths["views"]))
        self.assertIn('action_server_mark_x (Object) server action → sale.order "Mark as X", in the Action menu of sale.order - Line 3', lines)
        self.assertIn('cron_x (Object) scheduled action → sale.order "X: refresh orders" - Line 10', lines)
        self.assertIn('rule_x_order (Object) ir.rule "X orders: own company" on sale.order - Line 16', lines)

    def test_menus_labelled_by_their_action(self):
        # ws-menu-label-from-action: a menuitem without name= shows its action's name
        self.assertEqual(found(self.ws("menu:X Orders")), ["x_more.menu_x_orders (Object) x_more_views.xml:34 in ir.ui.menu (x_more) menu: X Orders (from action) → x_more.action_x_orders"])
        self.assertIn("x_more.menu_x_sales (Object) x_more_views.xml:35 in ir.ui.menu (x_more) menu: Sales Orders (from action) → sale.action_orders", found(self.ws("menu:sales orders")))
        self.assertEqual(found(self.ws("x_more.menu_x_report")), ["x_more.menu_x_report (Object) x_more_views.xml:36 in ir.ui.menu (x_more) menu → x_more.action_report_x"])  # reports do not label menus
        lines = outline(self.doc(self.paths["views"]))
        self.assertIn("menu_x_orders (Object) menu: X Orders (from action) → action_x_orders - Line 34", lines)

    def test_records_in_load_order(self):
        # ws-xmlid-definition-order: the manifest loads security/x_groups.xml first (it creates
        # the record), then data/a_update.xml, then the demo file - not path order
        expected = [
            'x_more.group_x (Object) x_groups.xml:3 in res.groups (x_more) "X Users"',
            "x_more.group_x (Object) a_update.xml:3 in res.groups (x_more)",
            "x_more.group_x (Object) a_demo.xml:4 in res.groups (x_more), noupdate",
        ]
        for q in ("x_more.group_x", "xmlid:x_more.group_x", "group_x"):
            self.assertEqual(found(self.ws(q))[:3], expected, q)

    def test_access_rules_with_domains(self):
        # doc-ir-access-domain: ir.access rows keep their domain; no group = a restriction on everybody
        self.assertEqual(
            outline(self.doc(self.paths["access"])),
            [
                "access_x_order_user (Object) sale.order, group base.group_user, operation r, domain [('state', '=', 'sale')] - Line 2",
                "x_order_restriction (Object) sale.order, restriction (all users), operation crud, domain [('company_id', 'in', company_ids)] - Line 3",
                "access_x_order_manager (Object) sale.order, group base.group_system, operation crud - Line 4",
            ],
        )
        self.assertEqual(
            found(self.ws("x_more.access_x_order_user")),
            ["x_more.access_x_order_user (Object) ir.access.csv:2 in ir.access (x_more) \"x orders user\": sale.order, group base.group_user, operation r, domain [('state', '=', 'sale')]"],
        )

    def test_model_and_labels(self):
        # ws-multiword-model: the model first, then menus / actions whose label has the words
        res = found(self.ws("sale order"))
        self.assertTrue(res[0].startswith("sale.order (Class) sale_order.py:5 in sale (defines)"), res)
        self.assertEqual(res[-1], 'x_more.action_sale_order_lines_x (Object) x_more_views.xml:25 in ir.actions.act_window (x_more) window action → sale.order.line "Sale Order Lines X"')

    def test_outlines_within_budget(self):
        # doc-outline-over-50k: every outline renders under ~40k chars and says what it left out
        for key in ("big_py", "big_js", "big_xml", "big_csv"):
            syms = self.doc(self.paths[key])
            text = render_doc(syms)
            self.assertLess(len(text), S.OUTPUT_BUDGET + 200, key)
            self.assertRegex(text.split("\n")[-1], r"^\(\d+ more symbols not listed: the outline stops at ~40k chars; read the file around this line\) \(Object\) - Line \d+$", key)
        syms = self.doc(self.paths["big_py"])  # the class is cut inside: its children stop, the marker ends the outline
        self.assertEqual(syms[0]["name"], "BigHelper")
        self.assertLess(len(syms[0]["children"]), 1500)
        flat = S.document_symbols(_ctx(self.server, self.env, self.paths["big_js"], 1, 1), False)
        self.assertLess(len(flat), 2500)

    def test_python_reexports_per_env(self):
        # ws-python-reexports (19.0+ style package re-export) and ws-env-mru: answers come from
        # the env asked, never from the other env's caches (both trees define odoo.api / odoo.tools)
        mini_env, _ = load_env(self.server, os.path.join(CUSTOM, "models", "sale_order.py"))
        try:

            def mini(q: str) -> list[str]:
                return found(S.workspace_symbols(self.server, mini_env, mini_env.registry, q, NEVER_CANCELLED))

            for _ in range(2):
                self.assertEqual(found(self.ws("odoo.api.depends")), ["depends (Function) decorators.py:1 in odoo.orm.decorators, re-exported by odoo.api"])
                self.assertEqual(mini("odoo.api.depends"), ["depends (Function) api.py:1 in odoo.api"])
                self.assertEqual(found(self.ws("odoo.tools.float_compare")), ["float_compare (Function) misc.py:1 in odoo.tools.misc, re-exported by odoo.tools"])
                self.assertEqual(mini("odoo.tools.float_compare"), ["float_compare (Function) float_utils.py:5 in odoo.tools.float_utils, re-exported by odoo.tools"])
            self.assertEqual(found(self.ws("@api.depends")), found(self.ws("odoo.api.depends")))
            self.assertEqual(found(self.ws("odoo.orm")), ["odoo.orm (Module) __init__.py:1 in odoo.orm"])
        finally:
            mini_env.registry = None


class MoreIncrementalTest(MoreFixture):
    def test_file_becoming_imported_and_data_reordered(self):
        reg = self.env.registry
        self.assertEqual(self.ws("DeadOrder"), [])
        self.assertEqual(found(self.ws("x_more.group_x"))[0], 'x_more.group_x (Object) x_groups.xml:3 in res.groups (x_more) "X Users"')
        ix = reg.__dict__[S._INDEX_ATTR]
        # importing dead_order.py rebuilds the registry (a newly reachable file, new
        # ClassRefs) although dead_order.py itself did not change: the index follows
        path = self.paths["models_init"]
        reg.update_file(path, extract_source(path, "py", b"from . import sale_order\nfrom . import dead_order\n"))
        self.assertEqual(found(self.ws("DeadOrder")), ["DeadOrder (Class) dead_order.py:8 in odoo.addons.x_more.models.dead_order: sale.order (extends)"])
        self.assertIn(self.paths["dead"], [uri_to_path(s["location"]["uri"]) for s in self.ws("sale.order")])
        self.assertEqual(found(self.ws("dead_helper")), ["dead_helper (Function) dead_order.py:4 in odoo.addons.x_more.models.dead_order"])
        self.assertEqual(outline(self.doc(self.paths["dead"]))[1], "DeadOrder (Class) extends sale.order - Line 8")
        self.assertIs(reg.__dict__[S._INDEX_ATTR], ix)
        # the manifest now loads data/a_update.xml before security/x_groups.xml
        manifest = self.paths["manifest"]
        with open(manifest, "rb") as fh:
            src = fh.read().replace(b"'security/x_groups.xml',\n", b"").replace(b"'data/a_update.xml',", b"'data/a_update.xml',\n        'security/x_groups.xml',")
        reg.update_file(manifest, extract_source(manifest, "manifest", src))
        self.assertEqual([os.path.basename(uri_to_path(s["location"]["uri"])) for s in self.ws("x_more.group_x")], ["a_update.xml", "x_groups.xml", "a_demo.xml"])


class IncrementalIndexTest(unittest.TestCase):
    """The index follows registry updates file by file (didOpen / didChange)."""

    def setUp(self):
        self.server = headless_server()
        self.env, _ = load_env(self.server, os.path.join(CUSTOM, "models", "sale_order.py"))

    def tearDown(self):
        self.env.registry = None

    def ws(self, query: str) -> list[dict]:
        return S.workspace_symbols(self.server, self.env, self.env.registry, query, NEVER_CANCELLED)

    def update(self, path: str, kind: str, old: str, new: str) -> None:
        with open(path, "rb") as fh:
            src = fh.read().replace(old.encode(), new.encode())
        self.env.registry.update_file(path, extract_source(path, kind, src))

    def test_updates(self):
        reg = self.env.registry
        self.assertEqual(len(self.ws("x_reference")), 1)
        self.assertEqual(len(self.ws("menu:order notes")), 1)
        self.assertEqual(len(self.ws("/x/export")), 1)
        ix = reg.__dict__[S._INDEX_ATTR]
        py = os.path.join(CUSTOM, "models", "sale_order.py")
        self.update(py, "py", "x_reference = ", "x_ref2 = ")
        self.assertEqual(self.ws("x_reference"), [])
        self.assertEqual(found(self.ws("sale.order.x_ref2")), ['x_ref2 (Field) sale_order.py:17 in sale.order (x_sale_ext) Char'])
        self.assertIs(reg.__dict__[S._INDEX_ATTR], ix)  # updated in place, not rebuilt
        xml = os.path.join(CUSTOM, "views", "sale_order_views.xml")
        self.update(xml, "xml", 'name="Order Notes" action', 'name="Order Memos" action')
        self.assertEqual(found(self.ws("menu:order memos")), ["x_sale_ext.menu_x_notes (Object) sale_order_views.xml:29 in ir.ui.menu (x_sale_ext) menu: Order Memos → x_sale_ext.action_x_sale_notes"])
        ctl = os.path.join(CUSTOM, "controllers", "main.py")
        self.update(ctl, "py", "'/x/export'", "'/x/export2'")
        self.assertEqual([s["containerName"] for s in self.ws("/x/export2")], ["route /x/export2 (x_sale_ext) type=http auth=user"])
        reg.remove_file(py)
        self.assertEqual(self.ws("x_helper"), [])
        self.assertEqual([s["name"] for s in self.ws("sale.order")][:2], ["sale.order", "sale.order"])
        classes = ix.families["classes"]
        reg.set_modules(dict(reg.module_dirs))  # e.g. a manifest was edited: every class is rebuilt
        self.assertEqual(len(self.ws("action_confirm")), 4)
        self.assertIsNot(ix.families["classes"], classes)


# ---------------------------------------------------------------------------
# real Odoo trees (skipped when absent)

BUDGET_MS = 300  # 2x the 150 ms budget: the test machine is shared


class RealTree:
    VERSION = ""

    @classmethod
    def setUpClass(cls):
        cls.server = headless_server()
        cls.root = os.path.join(WORKTREES, cls.VERSION)
        cls.env, _ = load_env(cls.server, os.path.join(cls.root, "odoo", "addons", "sale", "models", "sale_order.py"))
        S.workspace_symbols(cls.server, cls.env, cls.env.registry, "warm_up_every_bucket", NEVER_CANCELLED)

    @classmethod
    def tearDownClass(cls):
        cls.env.registry = None

    def ws(self, query: str) -> list[dict]:
        t = time.perf_counter()
        res = S.workspace_symbols(self.server, self.env, self.env.registry, query, NEVER_CANCELLED)
        ms = (time.perf_counter() - t) * 1000
        if not query.startswith(("/", "view:")) and not S._URL_HOST.match(query):  # route / view indexes are built on first use
            self.assertLess(ms, BUDGET_MS, f"{query!r} took {ms:.0f} ms")
        return res

    def rel(self, s: dict) -> str:
        return os.path.relpath(uri_to_path(s["location"]["uri"]), self.root)

    def test_model(self):
        res = self.ws("sale.order")
        self.assertGreater(len(res), 10)
        self.assertEqual((self.rel(res[0]), res[0]["containerName"]), ("odoo/addons/sale/models/sale_order.py", "sale (defines)"))
        self.assertTrue(all(s["name"] == "sale.order" for s in res[:10]))
        self.assertTrue(all(s["containerName"].endswith("(extends)") for s in res[1:10]))

    def test_member(self):
        res = self.ws("sale.order.partner_id")
        self.assertEqual(self.rel(res[0]), "odoo/addons/sale/models/sale_order.py")
        self.assertEqual(res[0]["containerName"], "sale.order (sale) Many2one→res.partner")
        res = self.ws("account.move.action_post")
        self.assertEqual((self.rel(res[0]), res[0]["name"], res[0]["kind"]), ("odoo/addons/account/models/account_move.py", "action_post", S.SK_METHOD))
        res = self.ws("sale.order.")
        self.assertEqual(len(res), S.WORKSPACE_CAP)
        self.assertEqual(self.rel(res[0]), "odoo/addons/sale/models/sale_order.py")

    def test_plain(self):
        res = self.ws("action_confirm")
        self.assertIn(("odoo/addons/sale/models/sale_order.py", "sale.order (sale) method"), [(self.rel(s), s["containerName"]) for s in res])
        self.assertTrue(all(s["name"] == "action_confirm" for s in res[:20]))
        self.assertLessEqual(len(self.ws("id")), S.WORKSPACE_CAP)

    def test_record(self):
        res = self.ws("sale.view_order_form")
        self.assertEqual((res[0]["name"], self.rel(res[0])), ("sale.view_order_form", "odoo/addons/sale/views/sale_order_views.xml"))
        res = self.ws("view:sale.order")
        self.assertIn("sale.view_order_form", [s["name"] for s in res])
        self.assertNotIn("inherits", res[0]["containerName"])  # primary views first

    def test_route(self):
        res = self.ws("/shop/cart")
        self.assertTrue(any(s["name"] == "cart" and s["containerName"].startswith("route /shop/cart (website_sale)") for s in res), [s["containerName"] for s in res])

    def test_document_symbols(self):
        path = os.path.join(self.root, "odoo", "addons", "sale", "models", "sale_order.py")
        ctx = _ctx(self.server, self.env, path, 1, 1)
        ctx.facts  # parsing the file is not part of the outline budget
        t = time.perf_counter()
        syms = S.document_symbols(ctx, True)
        self.assertLess((time.perf_counter() - t) * 1000, BUDGET_MS)
        cls = next(s for s in syms if s["name"] == "SaleOrder")
        self.assertTrue(cls["detail"].startswith("sale.order (defines)"))
        confirm = next(c for c in cls["children"] if c["name"] == "action_confirm")
        self.assertEqual(confirm["kind"], S.SK_METHOD)
        with open(path, encoding="utf-8") as fh:
            self.assertTrue(fh.read().split("\n")[confirm["range"]["start"]["line"]].lstrip().startswith("def action_confirm"))

    def doc(self, rel: str) -> list[dict]:
        return S.document_symbols(_ctx(self.server, self.env, os.path.join(self.root, rel), 1, 1), True)

    def test_outline_budget(self):
        # doc-outline-over-50k: o_spreadsheet.js (3k+ symbols from 18.0 on) used to render
        # 120-170k chars, big data files 50-58k
        for rel in ("odoo/addons/spreadsheet/static/src/o_spreadsheet/o_spreadsheet.js", "odoo/odoo/addons/base/data/res.country.state.csv"):
            text = render_doc(self.doc(rel))
            self.assertLess(len(text), S.OUTPUT_BUDGET + 500, rel)
        self.assertRegex(text.split("\n")[-1], r"^\(\d+ more symbols not listed: ")

    def test_typos_and_unknown_names(self):
        # ws-fuzzy-flood: a few ranked suggestions, the obvious one first; no flood for unknown names
        res = self.ws("res.partnr")
        self.assertEqual((res[0]["name"], res[0]["containerName"]), ("res.partner", "base (defines) [fuzzy match]"))
        res = self.ws("action_confrim")
        self.assertTrue(res and all(s["name"] == "action_confirm" for s in res), [s["name"] for s in res])
        self.assertEqual(self.ws("partner_idd")[0]["name"], "partner_id")
        for q in ("res.partnr", "action_confrim", "partner_idd", "zqxv_no_such_thing", "useStateQx", "nmae_gt_xyz"):
            res = self.ws(q)
            self.assertLessEqual(len(res), S.FUZZY_CAP, q)
            self.assertLess(len(render_ws(res)), 6000, q)

    def test_model_spelled_with_spaces(self):
        # ws-multiword-model
        for q, model in (("sale order", "sale.order"), ("purchase order", "purchase.order"), ("stock move", "stock.move"), ("product template", "product.template"), ("Sale Order Line", "sale.order.line")):
            res = self.ws(q)
            self.assertEqual((res[0]["name"], res[0]["containerName"].rsplit(" ", 1)[-1]), (model, "(defines)"), q)

    def test_member_listing_shows_every_kind(self):
        # ws-member-listing-truncated: account.move has ~700 fields and ~1400 methods
        res = self.ws("account.move.")
        self.assertEqual(len(res), S.WORKSPACE_CAP)
        kinds = [s["kind"] for s in res[:-1]]
        self.assertGreaterEqual(kinds.count(S.SK_METHOD), 30)
        self.assertGreaterEqual(kinds.count(S.SK_FIELD), 50)
        self.assertTrue(any(not s["containerName"].startswith("account.move ") for s in res[:-1]))  # inherited members
        self.assertRegex(res[-1]["name"], r"^\(\d+ more members not listed: \d+ fields, \d+ methods")

    def test_actions_bound_through_model_id(self):
        # ws-action-server-actions: sale's server actions bind sale.order through model_id
        containers = [s["containerName"] for s in self.ws("action:sale.order")]
        self.assertTrue(any(c.startswith("ir.actions.server (sale) server action → sale.order") for c in containers), containers)

    def test_records_in_load_order(self):
        # ws-xmlid-definition-order: base_groups.xml (manifest-first) creates base.group_user
        res = self.ws("base.group_user")
        self.assertEqual(self.rel(res[0]), "odoo/odoo/addons/base/security/base_groups.xml")
        self.assertEqual([self.rel(s) for s in self.ws("group_user")][:1], ["odoo/odoo/addons/base/security/base_groups.xml"])

    def test_menu_label_from_action(self):
        # ws-menu-label-from-action: Accounting > Customers > Invoices has no name= of its own
        res = [s for s in self.ws("menu:Invoices") if s["name"] == "account.menu_action_move_out_invoice_type"]
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0]["containerName"].startswith("ir.ui.menu (account) menu: Invoices (from action) → account.action_move_out_invoice"))

    def test_route_language_prefix(self):
        # ws-route-lang-prefix
        for q in ("/fr/shop/cart", "/fr_BE/shop/cart", "https://www.example.com/th/shop/cart?x=1"):
            res = self.ws(q)
            self.assertTrue(res and res[0]["containerName"].startswith("route /shop/cart (website_sale)") and " stripped" in res[0]["containerName"], q)

    def test_case_and_implicit_ids(self):
        # ws-case-and-implicit-ids
        self.assertEqual(self.ws("Sale.Order"), self.ws("sale.order"))
        self.assertEqual(self.ws("Res.Partner")[0]["containerName"], "base (defines)")
        res = self.ws("model_sale_order")
        self.assertEqual((res[0]["name"], self.rel(res[0])), ("sale.model_sale_order", "odoo/addons/sale/models/sale_order.py"))

    def test_code_shaped_queries(self):
        # ws-code-shaped-queries
        res = self.ws("SaleOrder.action_confirm")
        self.assertEqual((self.rel(res[0]), res[0]["containerName"]), ("odoo/addons/sale/models/sale_order.py", "sale.order (sale) method"))
        self.assertEqual(self.ws("self.env['sale.order']"), self.ws("sale.order"))
        self.assertEqual(self.ws("def action_confirm"), self.ws("def:action_confirm"))
        for q in ("fields.Many2one", "models.Model", "@api.depends", "odoo.http.route", "odoo.tools.float_compare"):
            self.assertEqual(len(self.ws(q)), 1, q)  # also ws-python-reexports (tools/__init__.py: from .float_utils import *)
        self.assertEqual(self.rel(self.ws("odoo.tools.float_compare")[0]), "odoo/odoo/tools/float_utils.py")


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "16.0")), "Odoo 16.0 checkout not available")
class Odoo16Test(RealTree, unittest.TestCase):
    VERSION = "16.0"


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "18.0")), "Odoo 18.0 checkout not available")
class Odoo18Test(RealTree, unittest.TestCase):
    VERSION = "18.0"

    def test_never_imported_controller(self):
        # ws-dead-classes: l10n_dk_nemhandel/__init__.py never imports controllers/
        res = self.ws("/nemhandel/webhook/new-message")
        self.assertEqual([self.rel(s) for s in res], ["odoo/addons/l10n_dk_nemhandel/controllers/webhooks.py"])
        self.assertTrue(res[0]["containerName"].endswith("controller file not imported by its module: route never registered"))
        lines = outline(self.doc("odoo/addons/l10n_dk_nemhandel/controllers/webhooks.py"))
        self.assertEqual(lines[0], "NemhandelWebhookController (Class) (http.Controller), not imported: never loaded - Line 5")


class AccessCsv:
    """saas-19.4+: ir.access replaces ir.model.access and ir.rule."""

    def test_ir_access_domain(self):
        # doc-ir-access-domain: a row's domain restricts what its group may do
        lines = outline(self.doc("odoo/odoo/addons/base/security/ir.access.csv"))
        self.assertIn("ir_attachment_public_rule (Object) ir.attachment, group base.group_user, operation r, domain [('public', '=', True)] - Line 4", lines)
        self.assertTrue(any(", restriction (all users), " in line for line in lines))
        res = self.ws("base.ir_attachment_public_rule")
        self.assertEqual(res[0]["containerName"], "ir.access (base) \"read public attachments\": ir.attachment, group base.group_user, operation r, domain [('public', '=', True)]")

    def test_python_reexports(self):
        # ws-python-reexports: odoo/models, fields, api (and http on 20.0) re-export odoo.orm.*
        expected = {
            "odoo.models.BaseModel": "odoo/odoo/orm/models.py",
            "odoo.models.Model": "odoo/odoo/orm/models.py",
            "odoo.fields.Many2one": "odoo/odoo/orm/fields_relational.py",
            "fields.Many2one": "odoo/odoo/orm/fields_relational.py",
            "odoo.api.depends": "odoo/odoo/orm/decorators.py",
            "@api.depends('partner_id')": "odoo/odoo/orm/decorators.py",
            "odoo.tools.format_date": "odoo/odoo/tools/misc.py",
        }
        for q, rel in expected.items():
            self.assertEqual([self.rel(s) for s in self.ws(q)], [rel], q)

    def test_never_imported_core_file(self):
        # ws-dead-classes: website_event_booth_sale/models/__init__.py does not import sale_order_line
        self.assertFalse(any("website_event_booth_sale" in self.rel(s) for s in self.ws("SaleOrderLine")))
        lines = outline(self.doc("odoo/addons/website_event_booth_sale/models/sale_order_line.py"))
        self.assertTrue(any(line.startswith("SaleOrderLine (Class) extends sale.order.line, not imported: never loaded") for line in lines), lines)


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "saas-19.4")), "Odoo saas-19.4 checkout not available")
class OdooSaas194Test(AccessCsv, RealTree, unittest.TestCase):
    VERSION = "saas-19.4"


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "20.0")), "Odoo 20.0 checkout not available")
class Odoo20Test(AccessCsv, RealTree, unittest.TestCase):
    VERSION = "20.0"

    def test_controller_route_through_keyword(self):
        res = self.ws("/shop/cart")  # @route(route="/shop/cart", ...) in website_sale/controllers/cart.py
        cart = [s for s in res if self.rel(s) == "odoo/addons/website_sale/controllers/cart.py"]
        self.assertEqual(cart[0]["containerName"], "route /shop/cart (website_sale) type=http auth=public")

    def test_http_package_reexports(self):
        # ws-python-reexports: odoo/http/__init__.py: from .routing_map import Controller, route
        self.assertEqual([self.rel(s) for s in self.ws("odoo.http.route")], ["odoo/odoo/http/routing_map.py"])
        self.assertEqual([self.rel(s) for s in self.ws("odoo.http.Controller")], ["odoo/odoo/http/routing_map.py"])


if __name__ == "__main__":
    unittest.main()
