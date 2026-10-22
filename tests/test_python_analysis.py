from __future__ import annotations

import gc
import os
import statistics
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens.analysis import python as pa  # noqa: E402
from odoo_lens.analysis.targets import ATTR, FIELD, FILE, METHOD, MODEL, MODULE, PYSYM, XMLID  # noqa: E402
from odoo_lens.cli import headless_server, load_env  # noqa: E402
from odoo_lens.documents import path_to_uri  # noqa: E402
from odoo_lens.server import RequestContext  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
MINI = os.path.join(HERE, "fixtures", "mini")
CUSTOM = os.path.join(MINI, "custom", "my_module", "models", "sale_order.py")
SALE = os.path.join(MINI, "odoo", "addons", "sale", "models", "sale_order.py")
SALE_STOCK = os.path.join(MINI, "odoo", "addons", "sale_stock", "models", "sale_order.py")
FLOAT_UTILS = os.path.join(MINI, "odoo", "odoo", "tools", "float_utils.py")
WORKTREES = os.path.expanduser(os.environ.get("ODOO_LENS_WORKTREES", "~/odev/worktrees"))


class _Base(unittest.TestCase):
    server = None
    env = None

    def ctx(self, path: str, line: int, col: int) -> RequestContext:
        return RequestContext(self.server, self.env, self.env.registry, path, line, col)

    def targets(self, path: str, line: int, col: int) -> list:
        return pa.targets_at(self.ctx(path, line, col))

    def col(self, path: str, line: int, needle: str, offset: int = 0, nth: int = 0) -> int:
        text = self.server.documents.line(path, line)
        idx = -1
        for _ in range(nth + 1):
            idx = text.index(needle, idx + 1)
        return idx + offset

    def at(self, path: str, line: int, needle: str, offset: int = 0, nth: int = 0) -> list:
        return self.targets(path, line, self.col(path, line, needle, offset, nth))

    def one(self, path: str, line: int, needle: str, offset: int = 0, nth: int = 0):
        res = self.at(path, line, needle, offset, nth)
        self.assertEqual(len(res), 1, res)
        return res[0]


class TestFixture(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, CUSTOM)
        cls.reg = cls.env.registry
        cls.opened: list[str] = []

    @classmethod
    def tearDownClass(cls) -> None:
        for path in cls.opened:
            cls.server.documents.close(path_to_uri(path))
        cls.server = cls.env = cls.reg = None
        gc.collect()

    def virtual(self, name: str, text: str) -> str:
        path = os.path.join(MINI, "custom", "my_module", "models", name)
        self.server.documents.open(path_to_uri(path), text, 1, "python")
        self.opened.append(path)
        return path

    def test_env_ref_xmlid(self):
        t = self.one(CUSTOM, 17, "base.main_company", 3)
        self.assertEqual((t.kind, t.xmlid), (XMLID, "base.main_company"))
        self.assertEqual(t.origin, (17, 32, 49))

    def test_inherit_string_model(self):
        t = self.one(CUSTOM, 4, "sale.order")
        self.assertEqual((t.kind, t.model), (MODEL, "sale.order"))

    def test_related_path_segments(self):
        t = self.one(CUSTOM, 7, "country_id", 2)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.partner", "country_id"))
        t = self.one(CUSTOM, 7, "code'", 1)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.country", "code"))

    def test_depends_second_segment(self):
        t = self.one(CUSTOM, 11, "namee", 2)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.partner", "namee"))
        self.assertTrue(t.extra.get("missing"))
        self.assertEqual(t.origin, (11, 29, 34))

    def test_order_items(self):
        t = self.one(SALE, 8, "date_order")
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "sale.order", "date_order"))

    def test_chain_from_env_ref(self):
        t = self.one(CUSTOM, 20, "partner_id")
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.company", "partner_id"))
        t = self.one(CUSTOM, 20, "email", 1, nth=1)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.partner", "email"))
        self.assertTrue(t.extra.get("inferred"))

    def test_loop_variable_over_search(self):
        t = self.one(CUSTOM, 20, "email", 1)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.partner", "email"))
        t = self.one(CUSTOM, 20, "partner.")
        self.assertEqual(t.kind, PYSYM)
        self.assertEqual((t.path, t.line), (CUSTOM, 19))
        self.assertEqual(t.extra.get("value_models"), ["res.partner"])

    def test_super_call(self):
        t = self.one(CUSTOM, 22, "action_confirm")
        self.assertEqual((t.kind, t.model, t.name), (METHOD, "sale.order", "action_confirm"))
        self.assertTrue(t.extra.get("super"))
        self.assertEqual(t.extra.get("class_index"), 0)
        t = self.one(SALE_STOCK, 10, "action_confirm")
        self.assertEqual((t.kind, t.model, t.name, t.extra.get("super")), (METHOD, "sale.order", "action_confirm", True))

    def test_definition_sites(self):
        t = self.one(CUSTOM, 6, "x_note", 1)
        self.assertEqual((t.kind, t.model, t.name, t.extra.get("definition")), (FIELD, "sale.order", "x_note", True))
        t = self.one(CUSTOM, 12, "_compute_x", 2)
        self.assertEqual((t.kind, t.model, t.name), (METHOD, "sale.order", "_compute_x"))
        t = self.one(CUSTOM, 3, "SaleOrder", 1)
        self.assertEqual((t.kind, t.model), (MODEL, "sale.order"))
        t = self.one(CUSTOM, 4, "_inherit", 1)
        self.assertEqual((t.kind, t.model), (MODEL, "sale.order"))
        t = self.one(SALE, 7, "_description", 1)
        self.assertEqual((t.kind, t.model, t.name), (ATTR, "sale.order", "_description"))

    def test_mapped_string(self):
        t = self.one(SALE, 22, "price_subtotal", 2)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "sale.order.line", "price_subtotal"))
        self.assertEqual(t.extra.get("role"), "mapped")
        t = self.one(SALE_STOCK, 16, "product_id", 1)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "sale.order.line", "product_id"))

    def test_lambda_parameter(self):
        t = self.one(SALE, 41, "active")
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "product.product", "active"))
        t = self.one(SALE, 41, "product_id")
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "sale.order.line", "product_id"))

    def test_browse_result(self):
        t = self.one(SALE, 43, "country_id")
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.partner", "country_id"))

    def test_super_create_result(self):
        t = self.one(SALE, 37, "name", 1)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "sale.order", "name"))

    def test_vals_key(self):
        t = self.one(SALE, 30, "state", 1)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "sale.order", "state"))

    def test_domain_leaf(self):
        t = self.one(CUSTOM, 18, "is_company", 1)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.partner", "is_company"))
        t = self.one(CUSTOM, 18, "search", 1)
        self.assertEqual((t.kind, t.model, t.name), (METHOD, "res.partner", "search"))

    def test_field_default_lambda_self(self):
        t = self.one(SALE, 17, "company)", 1)
        self.assertEqual(t.kind, PYSYM)
        self.assertTrue(t.path.endswith("odoo/odoo/api.py"))
        self.assertEqual(t.extra.get("value_models"), ["res.company"])

    def test_imported_function(self):
        t = self.one(SALE, 27, "float_compare", 2)
        self.assertEqual((t.kind, t.path, t.name, t.line), (PYSYM, FLOAT_UTILS, "float_compare", 4))
        t = self.one(SALE, 1, "float_compare", 2)
        self.assertEqual((t.kind, t.path, t.line), (PYSYM, FLOAT_UTILS, 4))

    def test_import_module_path(self):
        t = self.one(SALE, 1, "tools", 1)
        self.assertEqual(t.kind, FILE)
        self.assertTrue(t.path.endswith("odoo/odoo/tools/__init__.py"))
        t = self.one(SALE, 0, "fields", 1)
        self.assertEqual(t.kind, FILE)
        self.assertTrue(t.path.endswith("odoo/odoo/fields.py"))

    def test_module_attribute(self):
        t = self.one(SALE, 11, "Many2one", 1)
        self.assertEqual((t.kind, t.name), (PYSYM, "Many2one"))
        self.assertTrue(t.path.endswith("odoo/odoo/fields.py"))
        t = self.one(SALE, 4, "Model)", 1)
        self.assertEqual((t.kind, t.name), (PYSYM, "Model"))
        self.assertTrue(t.path.endswith("odoo/odoo/models.py"))
        t = self.one(SALE, 19, "depends", 1)
        self.assertEqual((t.kind, t.name), (PYSYM, "depends"))
        self.assertTrue(t.path.endswith("odoo/odoo/api.py"))

    def test_local_and_param(self):
        t = self.one(SALE, 36, "orders", 1)
        self.assertEqual((t.kind, t.line, t.extra.get("symbol_kind")), (PYSYM, 35, "local"))
        t = self.one(SALE, 35, "vals_list)", 1)
        self.assertEqual((t.kind, t.line, t.extra.get("symbol_kind")), (PYSYM, 34, "param"))

    def test_leniency(self):
        for col in (self.col(CUSTOM, 17, "'base"), self.col(CUSTOM, 17, "')"), self.col(CUSTOM, 17, "')") + 1):
            res = self.targets(CUSTOM, 17, col)
            self.assertEqual([(t.kind, t.xmlid) for t in res], [(XMLID, "base.main_company")], col)
        res = self.at(CUSTOM, 17, "('base")
        self.assertEqual([(t.kind, t.name) for t in res], [(PYSYM, "ref")])
        res = self.at(CUSTOM, 22, "()", 0, nth=1)
        self.assertEqual([(t.kind, t.name) for t in res], [(METHOD, "action_confirm")])
        res = self.at(CUSTOM, 22, "return", 2)
        self.assertEqual([(t.kind, t.name) for t in res], [(METHOD, "action_confirm")])
        res = self.at(CUSTOM, 20, ".email", 0, nth=1)
        self.assertEqual([(t.kind, t.model, t.name) for t in res], [(FIELD, "res.partner", "email")])

    def test_nothing_on_blank_or_past_end(self):
        self.assertEqual(self.targets(CUSTOM, 15, 0), [])
        self.assertEqual(self.targets(CUSTOM, 999, 0), [])

    def test_comment_and_keywords(self):
        path = self.virtual("t_comment.py", (
            "from odoo import models\n"
            "\n"
            "\n"
            "class SaleOrder(models.Model):\n"
            "    _inherit = 'sale.order'\n"
            "\n"
            "    def f(self):\n"
            "        x = self.partner_id  # see self.order_line\n"
            "        return x\n"
        ))
        self.assertEqual(self.at(path, 7, "order_line"), [])
        self.assertEqual(self.at(path, 7, "see"), [])
        t = self.one(path, 7, "partner_id", 2)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "sale.order", "partner_id"))

    def test_strings_in_code(self):
        path = self.virtual("t_strings.py", (
            "from odoo import Command, models\n"
            "\n"
            "\n"
            "class SaleOrder(models.Model):\n"
            "    _inherit = 'sale.order'\n"
            "\n"
            "    def f(self):\n"
            "        domain = [('partner_id.email', '=', False)]\n"
            "        orders = self.env['sale.order'].search(domain, order='date_order desc')\n"
            "        self.write({'order_line': [(0, 0, {'product_id': 1}), Command.create({'price_unit': 2.0})]})\n"
            "        self.with_context(default_partner_id=1).create({})\n"
            "        name = self._fields['partner_id'].string\n"
            "        rows = orders.read(['date_order'])\n"
            "        for partner, count in self.env['sale.order']._read_group([], ['partner_id'], ['__count']):\n"
            "            partner.email\n"
            "        for partner, orders in self.grouped('partner_id').items():\n"
            "            partner.email\n"
            "        return self.sorted('date_order').filtered_domain([('state', '=', 'sale')])\n"
            "\n"
            "    def create(self, vals_list):\n"
            "        for vals in vals_list:\n"
            "            if 'partner_id' in vals or vals.get('note'):\n"
            "                vals['date_order'] = False\n"
            "        return super().create(vals_list)\n"
        ))
        cases = [
            (7, "email", (FIELD, "res.partner", "email")),
            (7, "partner_id", (FIELD, "sale.order", "partner_id")),
            (8, "date_order", (FIELD, "sale.order", "date_order")),
            (9, "order_line", (FIELD, "sale.order", "order_line")),
            (9, "product_id", (FIELD, "sale.order.line", "product_id")),
            (9, "price_unit", (FIELD, "sale.order.line", "price_unit")),
            (10, "partner_id", (FIELD, "sale.order", "partner_id")),
            (11, "partner_id", (FIELD, "sale.order", "partner_id")),
            (12, "date_order", (FIELD, "sale.order", "date_order")),
            (13, "partner_id", (FIELD, "sale.order", "partner_id")),
            (14, "email", (FIELD, "res.partner", "email")),
            (16, "email", (FIELD, "res.partner", "email")),
            (17, "date_order", (FIELD, "sale.order", "date_order")),
            (17, "state", (FIELD, "sale.order", "state")),
            (21, "partner_id", (FIELD, "sale.order", "partner_id")),
            (22, "date_order", (FIELD, "sale.order", "date_order")),
        ]
        for line, needle, expected in cases:
            res = self.at(path, line, needle, 1)
            self.assertEqual([(t.kind, t.model, t.name) for t in res], [expected], (line, needle))
        t = self.one(path, 10, "default_partner_id", 1)
        self.assertEqual(t.origin[1], self.col(path, 10, "partner_id"))

    def test_unknown_receivers(self):
        path = self.virtual("t_unknown.py", (
            "from odoo import models\n"
            "\n"
            "\n"
            "class SaleOrder(models.Model):\n"
            "    _inherit = 'sale.order'\n"
            "\n"
            "    def f(self, line, rec, thing):\n"
            "        line.price_subtotal\n"
            "        rec.company_id\n"
            "        thing.upper()\n"
            "        return line.order_id\n"
        ))
        t = self.one(path, 7, "price_subtotal", 1)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "sale.order.line", "price_subtotal"))
        self.assertFalse(t.extra.get("inferred"))
        t = self.one(path, 8, "company_id", 1)
        self.assertEqual((t.kind, t.model, t.name), (FIELD, None, "company_id"))
        self.assertEqual(t.models[0], "sale.order")
        self.assertIn("res.partner", t.models)
        self.assertEqual(self.at(path, 9, "upper"), [])

    def test_reaching_definitions(self):
        path = self.virtual("t_reaching.py", (
            "from odoo import models\n"
            "\n"
            "\n"
            "class SaleOrder(models.Model):\n"
            "    _inherit = 'sale.order'\n"
            "\n"
            "    def f(self):\n"
            "        partners = set()\n"
            "        partners.add(self.partner_id.id)\n"
            "        partners = self.env['res.partner'].browse(partners)\n"
            "        prev = None\n"
            "        for order in self:\n"
            "            if prev:\n"
            "                prev.email\n"
            "            prev = order.partner_id\n"
            "        return partners.email\n"
        ))
        self.assertEqual(self.at(path, 8, "add"), [])
        t = self.one(path, 15, "email")
        self.assertEqual((t.kind, t.model), (FIELD, "res.partner"))
        t = self.one(path, 13, "email")
        self.assertEqual((t.kind, t.model), (FIELD, "res.partner"))

    def test_python_classes_and_request(self):
        path = self.virtual("t_python.py", (
            "from odoo.http import request\n"
            "\n"
            "\n"
            "class Helper:\n"
            "    def __init__(self, env):\n"
            "        self.env = env\n"
            "        self.partner = env['res.partner']\n"
            "\n"
            "    def run(self, partner: 'res.partner'):\n"
            "        self.partner.email\n"
            "        partner.country_id\n"
            "        request.env['res.partner'].email\n"
            "        return self.helper()\n"
            "\n"
            "    def helper(self):\n"
            "        return self.partner\n"
        ))
        t = self.one(path, 9, "email")
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.partner", "email"))
        t = self.one(path, 9, "partner")
        self.assertEqual((t.kind, t.line), (PYSYM, 6))
        t = self.one(path, 10, "country_id")
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.partner", "country_id"))
        t = self.one(path, 11, "email")
        self.assertEqual((t.kind, t.model, t.name), (FIELD, "res.partner", "email"))
        t = self.one(path, 12, "helper")
        self.assertEqual((t.kind, t.line, t.name), (PYSYM, 14, "helper"))

    def test_domain_objects_and_copy_default(self):
        path = self.virtual("t_domain.py", (
            "from odoo import models\n"
            "from odoo.fields import Domain\n"
            "\n"
            "\n"
            "class SaleOrder(models.Model):\n"
            "    _inherit = 'sale.order'\n"
            "\n"
            "    def f(self):\n"
            "        domain = Domain('email', '=', False) & Domain([('is_company', '=', True)])\n"
            "        partners = self.env['res.partner'].search(domain)\n"
            "        user = self.env.user\n"
            "        self.copy(default={'date_order': False})\n"
            "        return user.partner_id.email, self.env.company.partner_id\n"
        ))
        cases = [
            (8, "email", (FIELD, "res.partner", "email")),
            (8, "is_company", (FIELD, "res.partner", "is_company")),
            (11, "date_order", (FIELD, "sale.order", "date_order")),
            (12, "email", (FIELD, "res.partner", "email")),  # env.user -> res.users -> _inherits partner_id
            (12, "partner_id", (FIELD, "res.users", "partner_id")),
        ]
        for line, needle, expected in cases:
            res = self.at(path, line, needle, 1)
            self.assertEqual([(t.kind, t.model, t.name) for t in res], [expected], (line, needle))
        self.assertEqual(self.at(path, 12, "partner_id", 1, nth=1)[0].model, "res.company")

    def test_big_file_partial_parse(self):
        filler = "".join(f"    def m{i}(self):\n        return self.name\n\n" for i in range(900))
        text = (
            "from odoo import models\n"
            "\n"
            "\n"
            "class SaleOrder(models.Model):\n"
            "    _inherit = 'sale.order'\n"
            "\n"
            + filler
            + "    def target(self):\n"
            "        for line in self.order_line:\n"
            "            line.product_id.active\n"
            "        return super().action_confirm()\n"
        )
        path = self.virtual("t_big.py", text)
        line0 = text.count("\n") - 2
        pa._FILES.clear()
        ctx = self.ctx(path, line0, self.col(path, line0, "active") + 1)
        res = pa.targets_at(ctx)
        self.assertEqual([(t.kind, t.model, t.name) for t in res], [(FIELD, "product.product", "active")])
        self.assertTrue(any(len(k) == 4 for k in pa._FILES), "big file analysed through a partial parse")
        self.assertFalse(any(len(k) == 3 and k[0] == path for k in pa._FILES))
        res = self.targets(path, line0 + 1, self.col(path, line0 + 1, "action_confirm") + 1)
        self.assertEqual([(t.kind, t.extra.get("super"), t.extra.get("class_index")) for t in res], [(METHOD, True, 0)])
        src = text.encode()
        enc = pa.enclosing_function(self.reg, path, src, line0)
        self.assertEqual((enc["name"], enc["model"]), ("target", "sale.order"))
        for i in range(pa.PARTIAL_MAX_USES + 1):
            self.targets(path, 7 + 3 * i, 12)
        self.assertTrue(any(len(k) == 3 and k[0] == path for k in pa._FILES))
        res = self.targets(path, line0, self.col(path, line0, "active") + 1)
        self.assertEqual([(t.kind, t.model, t.name) for t in res], [(FIELD, "product.product", "active")])

    def source(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    def test_member_usages(self):
        src = self.source(SALE)
        self.assertEqual(pa.member_usages(self.reg, SALE, src, "partner_id"), [
            (29, 18, 28, ["sale.order"], "attr"),
            (42, 54, 64, ["sale.order"], "attr"),
        ])
        self.assertEqual(pa.member_usages(self.reg, SALE, src, "action_confirm"), [])
        self.assertEqual(pa.member_usages(self.reg, SALE, src, "message_post"), [(29, 29, 41, ["res.partner"], "call")])
        self.assertEqual(pa.member_usages(self.reg, SALE, src, "price_subtotal"), [(22, 62, 76, ["sale.order.line"], "string")])
        self.assertEqual(pa.member_usages(self.reg, SALE, src, "state"), [(30, 21, 26, ["sale.order"], "key")])
        self.assertEqual(pa.member_usages(self.reg, SALE, src, "create"), [(35, 25, 31, ["sale.order"], "super")])
        self.assertEqual(pa.member_usages(self.reg, SALE, src, "no_such_name"), [])
        self.assertEqual(pa.member_usages(self.reg, CUSTOM, self.source(CUSTOM), "action_confirm"), [(22, 23, 37, ["sale.order"], "super")])
        usages = pa.member_usages(self.reg, CUSTOM, self.source(CUSTOM), "email")
        self.assertEqual(usages, [(20, 20, 25, ["res.partner"], "attr"), (20, 47, 52, ["res.partner"], "attr")])

    def test_member_usages_strings_and_keys(self):
        src = (
            b"from odoo import models\n"
            b"\n"
            b"\n"
            b"class SaleOrder(models.Model):\n"
            b"    _inherit = 'sale.order'\n"
            b"\n"
            b"    def f(self):\n"
            b"        self.mapped('order_line.product_id')\n"
            b"        self.with_context(default_partner_id=1).write({'partner_id': 2})\n"
            b"        return self.env['res.partner'].search([('parent_id.email', '!=', False)])\n"
        )
        path = os.path.join(MINI, "custom", "my_module", "models", "t_usages.py")
        self.assertEqual(pa.member_usages(self.reg, path, src, "product_id"), [(7, 32, 42, ["sale.order.line"], "string")])
        self.assertEqual(pa.member_usages(self.reg, path, src, "partner_id"), [
            (8, 34, 44, ["sale.order"], "key"),
            (8, 56, 66, ["sale.order"], "key"),
        ])
        self.assertEqual(pa.member_usages(self.reg, path, src, "email"), [(9, 59, 64, ["res.partner"], "string")])

    def test_calls_in_function(self):
        src = self.source(SALE)
        self.assertEqual(pa.calls_in_function(self.reg, SALE, src, 26), [
            ("message_post", 29, 29, 41, ["res.partner"]),
            ("write", 30, 13, 18, ["sale.order"]),
        ])
        calls = pa.calls_in_function(self.reg, CUSTOM, self.source(CUSTOM), 16)
        self.assertEqual(calls, [
            ("search", 18, 43, 49, ["res.partner"]),
            ("action_confirm", 22, 23, 37, ["sale.order"]),
        ])
        self.assertEqual(pa.calls_in_function(self.reg, SALE, src, 4), [])

    def test_enclosing_function(self):
        src = self.source(SALE)
        self.assertEqual(pa.enclosing_function(self.reg, SALE, src, 27), {
            "model": "sale.order", "class": "SaleOrder", "name": "action_confirm",
            "line": 24, "col": 8, "end_col": 22, "end_line": 31,
        })
        enc = pa.enclosing_function(self.reg, SALE, src, 33)
        self.assertEqual((enc["name"], enc["line"]), ("create", 34))
        self.assertIsNone(pa.enclosing_function(self.reg, SALE, src, 4))
        self.assertIsNone(pa.enclosing_function(self.reg, SALE, src, 17))

    def test_cache_and_bytes_prefilter(self):
        src = self.source(SALE)
        t = time.perf_counter()
        for _ in range(50):
            pa.member_usages(self.reg, SALE, src, "zzz_not_there")
        self.assertLess(time.perf_counter() - t, 0.05)
        pa.member_usages(self.reg, SALE, src, "partner_id")
        key = (SALE, len(src), hash(src))
        self.assertIn(key, pa._FILES)


def _find(path: str, needle: str) -> tuple[int, int]:
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            j = line.find(needle)
            if j >= 0:
                return i, j
    raise AssertionError(f"{needle!r} not found in {path}")


class _RealTree(_Base):
    version = ""

    @classmethod
    def setUpClass(cls) -> None:
        root = os.path.join(WORKTREES, cls.version)
        cls.root = root
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(root, "odoo", "addons", "sale", "models", "sale_order.py"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server = cls.env = None
        pa._FILES.clear()
        pa._SNIPPETS.clear()
        pa._RET_CACHE.clear()
        pa._SYM_CACHE.clear()
        gc.collect()

    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def target_at_needle(self, rel: str, needle: str, offset: int = 0) -> list:
        path = self.path(rel)
        line, col = _find(path, needle)
        return self.targets(path, line, col + offset)

    def check_many2one(self, expected_suffix: str):
        res = self.target_at_needle("odoo/addons/sale/models/sale_order.py", "fields.Many2one(", 8)
        self.assertEqual([(t.kind, t.name) for t in res], [(PYSYM, "Many2one")])
        self.assertTrue(res[0].path.endswith(expected_suffix), res[0].path)

    def check_sale_order(self):
        res = self.target_at_needle("odoo/addons/sale/models/sale_order.py", "self.partner_id", 6)
        self.assertEqual([(t.kind, t.model, t.name) for t in res], [(FIELD, "sale.order", "partner_id")])
        res = self.target_at_needle("odoo/addons/sale_stock/models/sale_order.py", "super(SaleOrder, self)._action_confirm()", 24)
        self.assertEqual([(t.kind, t.model, t.name, t.extra.get("super")) for t in res], [(METHOD, "sale.order", "_action_confirm", True)])
        res = self.target_at_needle("odoo/addons/sale/models/sale_order.py", "self.env.company", 10)
        self.assertEqual([t.kind for t in res], [PYSYM])
        self.assertEqual(res[0].extra.get("value_models"), ["res.company"])

    def check_latency(self):
        path = self.path("odoo/addons/account/models/account_move.py")
        lines = self.server.documents.lines(path)
        positions = [(i, len(text) - len(text.lstrip()) + 1) for i, text in enumerate(lines) if ".partner_id" in text][:40]
        times = []
        found = 0
        for line, _ in positions:
            col = lines[line].index(".partner_id") + 2
            t = time.perf_counter()
            res = self.targets(path, line, col)
            times.append((time.perf_counter() - t) * 1000)
            found += bool(res)
        first, median = times[0], statistics.median(times)
        print(f"\n{self.version} account_move.py targets_at: first {first:.1f} ms, median {median:.2f} ms, max {max(times):.1f} ms ({found}/{len(times)} resolved)")
        self.assertLess(median, 50)
        self.assertGreater(found, len(times) * 0.8)

    def check_member_usages(self):
        path = self.path("odoo/addons/sale/models/sale_order.py")
        with open(path, "rb") as fh:
            src = fh.read()
        pa._FILES.clear()
        t = time.perf_counter()
        usages = pa.member_usages(self.env.registry, path, src, "partner_id")
        dt = (time.perf_counter() - t) * 1000
        print(f"\n{self.version} member_usages(sale_order.py, 'partner_id'): {len(usages)} usages, {dt:.1f} ms")
        self.assertGreater(len(usages), 10)
        self.assertTrue(all(kind in ("attr", "call", "super", "string", "key") for *_, kind in usages))
        self.assertIn(["sale.order"], [u[3] for u in usages])


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "16.0")), "16.0 checkout not available")
class TestReal16(_RealTree):
    version = "16.0"

    def test_many2one_symbol(self):
        self.check_many2one("odoo/odoo/fields.py")

    def test_sale_order(self):
        self.check_sale_order()

    def test_member_usages(self):
        self.check_member_usages()


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "18.0")), "18.0 checkout not available")
class TestReal18(_RealTree):
    version = "18.0"

    def test_many2one_symbol(self):
        self.check_many2one("odoo/odoo/fields.py")

    def test_sale_order(self):
        self.check_sale_order()

    def test_test_class_attributes(self):
        rel = "odoo/addons/account/tests/test_account_move_out_invoice.py"
        res = self.target_at_needle(rel, "self.partner_a.id", 5)
        self.assertEqual([(t.kind, t.name) for t in res], [(PYSYM, "partner_a")])
        self.assertTrue(res[0].path.endswith("account/tests/common.py"))
        res = self.target_at_needle(rel, "self.partner_a.id", 15)
        self.assertEqual([(t.kind, t.model, t.name) for t in res], [(FIELD, "res.partner", "id")])

    def test_related_comodel(self):
        res = self.target_at_needle("odoo/addons/sale/models/sale_order_line.py", "self.company_id.sale_discount_product_id", 16)
        self.assertEqual([(t.kind, t.model, t.name) for t in res], [(FIELD, "res.company", "sale_discount_product_id")])


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "20.0")), "20.0 checkout not available")
class TestReal20(_RealTree):
    version = "20.0"

    def test_many2one_symbol(self):
        self.check_many2one("odoo/odoo/orm/fields_relational.py")

    def test_sale_order(self):
        self.check_sale_order()

    def test_odoo_namespace_names(self):
        # 19+: odoo/__init__.py is gone, odoo.Command / odoo._ are set by odoo/init.py
        res = self.target_at_needle("odoo/addons/sale/models/sale_order.py", "from odoo import", 0)
        line, _ = _find(self.path("odoo/addons/sale/models/sale_order.py"), "from odoo import")
        text = self.server.documents.line(self.path("odoo/addons/sale/models/sale_order.py"), line)
        for name, suffix in (("Command", "odoo/odoo/orm/commands.py"), ("_", "odoo/odoo/tools/translate.py")):
            if f" {name}," not in text and f" {name}\n" not in text + "\n":
                continue
            col = text.index(f" {name}") + 1
            res = self.targets(self.path("odoo/addons/sale/models/sale_order.py"), line, col)
            self.assertEqual([t.kind for t in res], [PYSYM], name)
            self.assertTrue(res[0].path.endswith(suffix), res[0].path)

    def test_odoo_module_path(self):
        path = self.path("odoo/addons/sale_stock/models/sale_order.py")
        res = self.targets(path, *_find(path, "from odoo"))
        self.assertIn(res[0].kind if res else None, (None, FILE, MODULE))

    def test_latency(self):
        self.check_latency()

    def test_member_usages(self):
        self.check_member_usages()


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "saas-19.2")), "saas-19.2 checkout not available")
class TestRealSaas19(_RealTree):
    version = "saas-19.2"

    def test_many2one_symbol(self):
        self.check_many2one("odoo/odoo/orm/fields_relational.py")

    def test_sale_order(self):
        self.check_sale_order()


if __name__ == "__main__":
    unittest.main()
