"""Regression tests of the Python-analysis review findings (py-01 .. py-14).

Positions are found by searching needles in the real sources, so the tests
survive small upstream changes. Code samples are opened as in-memory documents
(never written to disk). Real-tree tests run when
~/odev/worktrees/<version> (or $ODOO_LENS_WORKTREES/<version>) exists.
"""

from __future__ import annotations

import ast
import gc
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens import features  # noqa: E402
from odoo_lens.analysis import python as pa  # noqa: E402
from odoo_lens.analysis.targets import ATTR, FIELD, METHOD, PYSYM  # noqa: E402
from odoo_lens.cli import headless_server, load_env  # noqa: E402
from odoo_lens.documents import path_to_uri  # noqa: E402
from odoo_lens.extract.pyfacts import extract, parse_tolerant  # noqa: E402
from odoo_lens.server import RequestContext  # noqa: E402

WORKTREES = os.path.expanduser(os.environ.get("ODOO_LENS_WORKTREES", "~/odev/worktrees"))


class TolerantParseTest(unittest.TestCase):
    BODY = (
        "class A:\n"
        "    def f(self):\n"
        "        return self.x\n"
        "\n"
        "    def g(self):\n"
        "{broken}\n"
        "        return 1\n"
        "\n"
        "    def h(self):\n"
        "        return self.y\n"
    )

    def check(self, broken: str) -> ast.Module:
        src = self.BODY.format(broken=broken).encode()
        tree, error = parse_tolerant(src)
        self.assertIsNotNone(error)
        self.assertIsNotNone(tree, broken)
        cls = tree.body[0]
        names = [n.name for n in cls.body if isinstance(n, ast.FunctionDef)]
        self.assertIn("f", names)
        self.assertIn("h", names)
        h = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "h")
        self.assertEqual(h.lineno, src.split(b"\n").index(b"    def h(self):") + 1)  # line numbers preserved
        return tree

    def test_unclosed_paren(self):
        self.check("        x = (self.partner_id")

    def test_missing_colon_keeps_block(self):
        tree = self.check("        if self.partner_id")
        g = next(n for n in tree.body[0].body if isinstance(n, ast.FunctionDef) and n.name == "g")
        self.assertTrue(any(isinstance(n, ast.Return) for n in ast.walk(g)))

    def test_half_def(self):
        self.check("    def k(self")

    def test_unexpected_indent(self):
        self.check("                x = 1\n                y = 2")

    def test_valid_source_has_no_error(self):
        tree, error = parse_tolerant(b"x = 1\n")
        self.assertIsNone(error)
        self.assertIsNotNone(tree)

    def test_facts_of_a_broken_buffer(self):
        src = self.BODY.format(broken="        x = (self.partner_id").encode()
        facts = extract("/tmp/a.py", src)
        self.assertIsNotNone(facts["error"])
        self.assertEqual([m[0] for m in facts["classes"][0][8]], ["f", "g", "h"])


class LoadOrderTest(unittest.TestCase):
    def modules(self, graph: dict[str, tuple[str, ...]]) -> dict:
        from odoo_lens.registry import ModuleInfo

        out = {}
        for name, deps in graph.items():
            info = ModuleInfo(name, "/x/" + name, "/x")
            info.depends = deps
            out[name] = info
        return out

    def test_depth_then_name(self):
        from odoo_lens.registry import _load_keys

        mods = self.modules({"base": (), "a": ("base",), "b": ("a",), "c": ("base",), "test_a": ("a", "c")})
        keys = _load_keys(mods)
        self.assertEqual(sorted(mods, key=keys.__getitem__), ["base", "a", "c", "b", "test_a"])

    def test_test_modules_follow_their_last_dependency_in_19(self):
        from odoo_lens.registry import _load_keys

        mods = self.modules({"base": (), "a": ("base",), "b": ("a",), "c": ("base",), "test_a": ("a", "c"), "z": ("b",)})
        keys = _load_keys(mods, test_rule=True)
        # test_a: right after c (its last loaded dependency), before b (depth 2)
        self.assertEqual(sorted(mods, key=keys.__getitem__), ["base", "a", "c", "test_a", "b", "z"])
        self.assertEqual(keys["test_a"], (1, "c test_a"))


class _Real(unittest.TestCase):
    version = ""
    anchor = "odoo/addons/sale/models/sale_order.py"

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = os.path.join(WORKTREES, cls.version)
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(cls.root, cls.anchor))
        cls.opened: list[str] = []

    @classmethod
    def tearDownClass(cls) -> None:
        for path in cls.opened:
            cls.server.documents.close(path_to_uri(path))
        cls.server = cls.env = None
        pa._FILES.clear()
        pa._SNIPPETS.clear()
        pa._RET_CACHE.clear()
        pa._SYM_CACHE.clear()
        gc.collect()

    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def ctx(self, path: str, line: int, col: int) -> RequestContext:
        return RequestContext(self.server, self.env, self.env.registry, path, line, col)

    def find(self, path: str, needle: str, after: str | None = None) -> tuple[int, int]:
        lines = self.server.documents.lines(path)
        if not lines:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().split("\n")
        start = 0
        if after is not None:
            start = next(i for i, text in enumerate(lines) if after in text)
        for i in range(start, len(lines)):
            j = lines[i].find(needle)
            if j >= 0:
                return i, j
        raise AssertionError(f"{needle!r} not found in {path}")

    def at(self, rel: str, needle: str, offset: int = 0, after: str | None = None) -> RequestContext:
        path = rel if rel.startswith("/") else self.path(rel)
        line, col = self.find(path, needle, after)
        return self.ctx(path, line, col + offset)

    def targets(self, rel: str, needle: str, offset: int = 0, after: str | None = None) -> list:
        return pa.targets_at(self.at(rel, needle, offset, after))

    def kmn(self, targets: list) -> list[tuple]:
        return [(t.kind, t.model, t.name) for t in targets]

    def virtual(self, rel: str, text: str) -> str:
        path = self.path(rel)
        self.server.documents.open(path_to_uri(path), text, 1, "python")
        self.opened.append(path)
        return path


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "18.0")), "18.0 checkout not available")
class Review18Test(_Real):
    version = "18.0"

    # py-01: no arbitrary model for BaseModel members of unknown receivers; dict keys typed
    def test_unknown_receiver_base_member_not_pinned(self):
        path = self.virtual(
            "odoo/addons/sale/models/_probe_py01.py",
            "from odoo import models\n\n\nclass P(models.Model):\n    _inherit = 'sale.order'\n\n"
            "    def probe(self, thing, other):\n"
            "        a = thing.id\n"
            "        b = other.filtered(lambda r: r).exists()\n",
        )
        self.assertEqual(pa.targets_at(self.ctx(path, 7, 19)), [])  # thing.id
        self.assertEqual(pa.targets_at(self.ctx(path, 8, 21)), [])  # other.filtered

    def test_dict_keys_are_typed(self):
        # lines_by_company[line.company_id] += line ... for company, lines in lines_by_company.items()
        res = self.targets("odoo/addons/sale/models/sale_order_line.py", "company.id", 8, after="def _compute_tax_id")
        self.assertEqual(self.kmn(res), [(FIELD, "res.company", "id")])

    def test_magic_field_definition_line(self):
        ctx = self.at("odoo/addons/sale/models/sale_order_line.py", "company.id", 8, after="def _compute_tax_id")
        locs = features.definition(ctx)
        locs = locs if isinstance(locs, list) else [locs]
        self.assertTrue(locs[0]["uri"].endswith("/odoo/odoo/models.py"))
        with open(self.path("odoo/odoo/models.py"), encoding="utf-8") as fh:
            line = fh.read().split("\n")[locs[0]["range"]["start"]["line"]]
        self.assertIn("add('id'", line)

    def test_guess_is_flagged_in_hover(self):
        ctx = self.at("odoo/addons/crm/models/crm_lead.py", "('fold', '=', False)", 2, after="_stage_find(domain=")
        md = features.hover(ctx)["contents"]["value"]
        self.assertIn("guessed", md)
        self.assertIn("crm.stage", md)

    # py-02: domain= of a non-ORM method does not filter the receiver
    def test_domain_kwarg_of_other_method(self):
        res = self.targets("odoo/addons/crm/models/crm_lead.py", "('fold', '=', False)", 2, after="_stage_find(domain=")
        self.assertEqual(self.kmn(res), [(FIELD, "crm.stage", "fold")])

    # py-03: 3-string tuples that are not domain leaves
    def test_selection_tuple_is_not_a_leaf(self):
        res = self.targets("odoo/addons/stock/models/stock_picking.py", "('assigned', 'done', 'cancel')", 2)
        self.assertEqual(res, [])

    def test_sql_constraint_is_not_a_leaf(self):
        res = self.targets("odoo/addons/link_tracker/models/link_tracker.py", "('code', 'unique( code )'", 2)
        self.assertEqual(res, [])

    # py-04: no snapping out of strings; super resolves like the attribute
    def test_no_snap_from_unresolved_string(self):
        res = self.targets("odoo/addons/sale/models/sale_order.py", "'partner_id': self.partner_invoice_id.id", 3)
        self.assertEqual(res, [])

    def test_super_token(self):
        res = self.targets("odoo/addons/sale/models/sale_order.py", "res = super().write(vals)", 8)
        self.assertEqual([(t.kind, t.model, t.name, t.extra.get("super")) for t in res], [(METHOD, "sale.order", "write", True)])

    def test_keyword_name_is_inert(self):
        res = self.targets("odoo/addons/sale/models/sale_order.py", "required=True, change_default=True", 2)
        self.assertEqual(res, [])

    def test_keyword_name_resolves_its_odoo_string(self):
        rel = "odoo/addons/sale/models/sale_order.py"
        res = self.targets(rel, "compute='_compute_amounts'", 2, after="amount_untaxed = fields.Monetary(")
        self.assertEqual(self.kmn(res), [(METHOD, "sale.order", "_compute_amounts")])
        res = self.targets(rel, 'string="Untaxed Amount"', 2, after="amount_untaxed = fields.Monetary(")
        self.assertEqual(res, [])

    def test_literal_model_name(self):
        res = self.targets("odoo/addons/sale_stock/models/sale_order.py", "_get_model_defaults('sale.order')", 22)
        self.assertEqual(self.kmn(res), [("model", "sale.order", None)])

    # py-05: a syntax error elsewhere keeps the file analysable
    def test_broken_buffer(self):
        rel = "odoo/addons/sale/models/sale_order.py"
        path = self.path(rel)
        with open(path, encoding="utf-8") as fh:
            text = fh.read().split("\n")
        at = next(i for i, t in enumerate(text) if t.startswith("    def action_confirm"))
        self.server.documents.open(path_to_uri(path), "\n".join(text[:at] + ["        x = (self.partner_id"] + text[at:]), 1, "python")
        try:
            line, col = self.find(path, "self.partner_id", after="def action_confirm")
            res = pa.targets_at(self.ctx(path, line, col + 6))
            self.assertEqual(self.kmn(res), [(FIELD, "sale.order", "partner_id")])
            line, col = self.find(path, "partner_id = fields.Many2one(")
            res = pa.targets_at(self.ctx(path, line, col + 2))
            self.assertEqual(self.kmn(res), [(FIELD, "sale.order", "partner_id")])
        finally:
            self.server.documents.close(path_to_uri(path))

    # py-06: hover shows the inferred model of a variable
    def test_variable_hover_shows_model(self):
        ctx = self.at("odoo/addons/sale/models/sale_order.py", "for order in self:", 4)
        md = features.hover(ctx)["contents"]["value"]
        self.assertIn("recordset of **sale.order**", md)

    def test_vals_hover(self):
        ctx = self.at("odoo/addons/sale/models/sale_order.py", "def create(self, vals_list)", 17)
        md = features.hover(ctx)["contents"]["value"]
        self.assertIn("field values of **sale.order**", md)

    # py-07: search methods return domains on their own model
    def test_search_method_domain(self):
        rel = "odoo/addons/sale/models/sale_order_line.py"
        needle = "return [('product_id.product_tmpl_id', operator, value)]"
        self.assertEqual(self.kmn(self.targets(rel, needle, 10)), [(FIELD, "sale.order.line", "product_id")])
        self.assertEqual(self.kmn(self.targets(rel, needle, 22)), [(FIELD, "product.product", "product_tmpl_id")])

    # py-08: env.context / env.cr
    def test_env_members(self):
        ctx = self.at("odoo/addons/sale/models/sale_order.py", "self.env.context.get(", 10)
        locs = features.definition(ctx)
        locs = locs if isinstance(locs, list) else [locs]
        self.assertTrue(locs and locs[0]["uri"].endswith("/odoo/odoo/api.py"), locs)

    # py-09: getattr strings may name any member
    def test_getattr_attribute(self):
        res = self.targets("odoo/addons/mail/models/mail_thread.py", "getattr(DocModel, '_mail_post_access'", 19)
        self.assertEqual(self.kmn(res), [(ATTR, "mail.thread", "_mail_post_access")])

    # py-11: super() reaching a class attribute
    def test_super_attribute(self):
        ctx = self.at("odoo/addons/account/models/account_move.py", "super()._sequence_monthly_regex", 9)
        locs = features.definition(ctx)
        locs = locs if isinstance(locs, list) else [locs]
        self.assertTrue(locs and locs[0]["uri"].endswith("/account/models/sequence_mixin.py"), locs)

    # py-12: read_group order with aggregate
    def test_order_with_aggregate(self):
        res = self.targets("odoo/addons/crm/models/crm_lead.py", "order='date_deadline:min ASC", 8)
        self.assertEqual(self.kmn(res), [(FIELD, "mail.activity", "date_deadline")])

    # py-13: values keys through containers and variables
    def test_vals_through_containers(self):
        path = self.virtual(
            "odoo/addons/sale/models/_probe_py13.py",
            "from odoo import models, Command\n\n\nclass P(models.Model):\n    _inherit = 'sale.order'\n\n"
            "    def probe(self):\n"
            "        self.order_line = [Command.create({'product_uom_qty': 2})]\n"
            "        res = {}\n"
            "        res['note'] = 'x'\n"
            "        self.env['sale.order'].create(res)\n"
            "        self.env['sale.order'].create(dict(client_order_ref='x'))\n"
            "        lines = [Command.create({'product_uom_qty': p}) for p in range(3)]\n"
            "        self.write({'order_line': lines})\n"
            "        self.env['sale.order'].create([{'client_order_ref': x} for x in 'ab'])\n"
            "        ctx = {'default_partner_id': 1}\n"
            "        return self.env['sale.order'].with_context(ctx)\n",
        )
        lines = self.server.documents.lines(path)
        expect = [
            ("'product_uom_qty': 2", ("sale.order.line", "product_uom_qty")),
            ("'note'", ("sale.order", "note")),
            ("client_order_ref=", ("sale.order", "client_order_ref")),
            ("'product_uom_qty': p", ("sale.order.line", "product_uom_qty")),
            ("'client_order_ref': x", ("sale.order", "client_order_ref")),
            ("'default_partner_id'", ("sale.order", "partner_id")),
        ]
        for needle, (model, name) in expect:
            line = next(i for i, t in enumerate(lines) if needle in t)
            col = lines[line].index(needle) + (1 if needle.startswith("'") else 0) + (8 if "default_" in needle else 0)
            res = pa.targets_at(self.ctx(path, line, col))
            self.assertEqual(self.kmn(res), [(FIELD, model, name)], needle)

    # symbols follow-ups: actions listed in a model's Action / Print menu; hover of bound actions
    def test_action_menu_bindings(self):
        from odoo_lens.protocol import NEVER_CANCELLED

        res = features.workspace_symbols(self.server, self.env, self.env.registry, "action:sale.order", NEVER_CANCELLED)
        by_name = {r["name"]: r.get("containerName", "") for r in res}
        self.assertIn("sale.action_view_sale_advance_payment_inv", by_name)  # res_model sale.advance.payment.inv
        self.assertIn("Action menu of sale.order", by_name["sale.action_view_sale_advance_payment_inv"])
        self.assertIn("Print menu of sale.order", by_name["sale.action_report_saleorder"])
        md = features.hover(self.at("odoo/addons/sale/views/sale_order_views.xml", 'id="model_sale_order_action_quotation_sent"', 5))["contents"]["value"]
        self.assertIn("Model: **sale.order**", md)
        self.assertIn("Action menu of **sale.order**", md)

    # live-session gap: the call order when every overriding module is installed
    def test_full_install_call_order(self):
        ctx = self.at("odoo/addons/sale_stock/models/sale_order.py", "def _action_confirm", 6)
        md = features.hover(ctx)["contents"]["value"]
        self.assertIn("**Call order with every module installed**", md)
        section = md.split("**Call order with every module installed**", 1)[1]
        order = [line.split("**")[1] for line in section.split("\n") if line[:1].isdigit()]
        self.assertEqual(order[-1], "sale")
        # sale_stock (depth n) runs before delivery and after repair (repair depends on sale_stock)
        self.assertLess(order.index("repair"), order.index("sale_stock"))
        self.assertLess(order.index("sale_stock"), order.index("delivery"))  # same depth, name order reversed
        self.assertIn("*(not a dependency)*", section)

    # py-14: Form proxies in tests
    def test_form_proxies(self):
        rel = "odoo/addons/sale/tests/test_sale_order.py"
        res = self.targets(rel, "sol_form.product_id = ", 10, after="with Form(sol) as sol_form")
        self.assertEqual(self.kmn(res), [(FIELD, "sale.order.line", "product_id")])
        res = self.targets(rel, "line.product_id = ", 6, after="with so_form.order_line.new() as line")
        self.assertEqual(self.kmn(res), [(FIELD, "sale.order.line", "product_id")])
        res = self.targets(rel, "line.product_packaging_qty", 6, after="with so_form.order_line.edit(0) as line")
        self.assertEqual(self.kmn(res), [(FIELD, "sale.order.line", "product_packaging_qty")])


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "16.0")), "16.0 checkout not available")
class Review16Test(_Real):
    version = "16.0"

    def test_read_group_alias(self):
        rel = "odoo/addons/stock/models/stock_move.py"
        self.assertEqual(self.targets(rel, "'lot_ids:array_agg(lot_id)'", 2), [])
        res = self.targets(rel, "'lot_ids:array_agg(lot_id)'", 20)
        self.assertEqual(self.kmn(res), [(FIELD, "stock.move.line", "lot_id")])

    def test_env_members_without_annotations(self):
        # 16.0: self.cr, self.uid, self.context, self.su = ... in Environment.__new__
        ctx = self.at("odoo/addons/sale/models/sale_order.py", "self.env.cr.execute(", 10)
        locs = features.definition(ctx)
        locs = locs if isinstance(locs, list) else [locs]
        self.assertTrue(locs and locs[0]["uri"].endswith("/odoo/odoo/api.py"), locs)


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "20.0")), "20.0 checkout not available")
class Review20Test(_Real):
    version = "20.0"

    # py-10: default_<x> for a consumer without field x is a plain context key
    def test_default_key_without_field(self):
        res = self.targets("odoo/addons/sale/models/sale_order.py", "with_context(default_team_id=", 13, after='.env["crm.team"]')
        self.assertEqual(res, [])

    def test_domain_object_in_search_method(self):
        res = self.targets("odoo/addons/sale/models/sale_order_line.py", 'Domain("product_id.product_tmpl_id", operator, value)', 8)
        self.assertEqual(self.kmn(res), [(FIELD, "sale.order.line", "product_id")])

    def test_env_cursor(self):
        ctx = self.at("odoo/addons/sale/models/sale_order.py", "self.env.cr.execute(", 10)
        locs = features.definition(ctx)
        locs = locs if isinstance(locs, list) else [locs]
        self.assertTrue(locs and locs[0]["uri"].endswith("/odoo/orm/environments.py"), locs)


if __name__ == "__main__":
    unittest.main()
