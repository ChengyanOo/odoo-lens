"""Tests for find references (odoo_lens/features/references.py).

Python usages come from ``analysis.python.member_usages``; most tests replace it
with a table-driven fake so that the receiver filtering is tested on its own.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens.analysis.targets import ATTR, FIELD, FILE, METHOD, MODEL, MODULE, PYSYM, TEMPLATE, XMLID, Target  # noqa: E402
from odoo_lens.cli import headless_server, load_env  # noqa: E402
from odoo_lens.documents import uri_to_path  # noqa: E402
from odoo_lens.extract import extract_source  # noqa: E402
from odoo_lens.server import RequestContext  # noqa: E402

# the package attribute ``odoo_lens.features.references`` is the entry point function
R = importlib.import_module("odoo_lens.features.references")

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")
WORKTREES = os.path.expanduser(os.environ.get("ODOO_LENS_WORKTREES", "~/odev/worktrees"))
SALE = "odoo/addons/sale/models/sale_order.py"
SALE_VIEWS = "odoo/addons/sale/views/sale_order_views.xml"
SALE_STOCK = "odoo/addons/sale_stock/models/sale_order.py"
SUBSCRIPTION = "enterprise/sale_subscription/models/sale_order.py"
MY = "custom/my_module/models/sale_order.py"
MY_VIEWS = "custom/my_module/views/sale_order_views.xml"
PARTNER = "odoo/odoo/addons/base/models/res_partner.py"
COMPANY = "odoo/odoo/addons/base/models/res_company.py"
COMPANY_DATA = "odoo/odoo/addons/base/data/res_company_data.xml"
FLOAT_UTILS = "odoo/odoo/tools/float_utils.py"
TOOLS_INIT = "odoo/odoo/tools/__init__.py"


def fx(rel: str) -> str:
    return os.path.join(MINI, rel)


def line_of(rel: str, line1: int) -> str:
    with open(fx(rel), encoding="utf-8") as fh:
        return fh.read().split("\n")[line1 - 1]


def spot(rel: str, line1: int, token: str, nth: int = 0) -> tuple[str, int, int]:
    """(rel, 0-based line, col) of the ``nth`` occurrence of ``token`` on 1-based ``line1``."""
    text = line_of(rel, line1)
    col = -1
    for _ in range(nth + 1):
        col = text.index(token, col + 1)
    return (rel, line1 - 1, col)


def usage(rel: str, line1: int, name: str, receivers: list[str] | None, kind: str, nth: int = 0) -> tuple:
    _, line, col = spot(rel, line1, name, nth)
    return (line, col, col + len(name), receivers, kind)


class FakeUsages:
    """Stand-in for analysis.python.member_usages: {(rel, name): [usage]}."""

    def __init__(self, table: dict | None = None):
        self.table = {(fx(rel), name): v for (rel, name), v in (table or {}).items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, registry, path, source, name):
        self.calls.append((path, name))
        return list(self.table.get((path, name), ()))


def rel_spots(res: list[dict], root: str = MINI) -> list[tuple[str, int, int]]:
    out = []
    for loc in res:
        start = loc["range"]["start"]
        out.append((os.path.relpath(uri_to_path(loc["uri"]), root), start["line"], start["character"]))
    return out


class _FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, fx(SALE))
        cls.reg = cls.env.registry

    def setUp(self):
        R._INDEXES.clear()  # per-registry caches (incl. cached usages of the fake)

    def ctx(self, rel: str, line: int = 0, col: int = 0) -> RequestContext:
        return RequestContext(self.server, self.env, self.reg, fx(rel), line, col)

    def refs(self, target: Target, rel: str = SALE, include: bool = True, usages: dict | None = None, finder=None) -> list[tuple[str, int, int]]:
        finder = finder or FakeUsages(usages)
        with mock.patch.object(R, "_usage_finder", lambda: finder):
            return rel_spots(R.references_for_target(self.ctx(rel), target, include))


class FieldReferencesTest(_FixtureCase):
    USAGES = {
        (SALE, "partner_id"): [usage(SALE, 30, "partner_id", ["sale.order"], "attr"), usage(SALE, 43, "partner_id", ["sale.order"], "attr")],
        (MY, "partner_id"): [
            usage(MY, 15, "partner_id", ["sale.order"], "attr"),
            usage(MY, 21, "partner_id", ["res.company"], "attr"),  # another model's partner_id
            usage(MY, 8, "partner_id", ["sale.order"], "string"),  # same spot as the related= index ref
            usage(MY, 15, "partner_id", None, "attr"),  # unknown receiver, common name: dropped
        ],
    }

    def test_sale_order_partner_id(self):
        res = self.refs(Target(FIELD, model="sale.order", name="partner_id"), usages=self.USAGES)
        expected = [
            spot(SALE, 12, "partner_id"),  # definition
            spot(SALE_STOCK, 8, "partner_id"),  # redefinition in an extension
            spot(SALE, 17, "partner_id"),  # related='partner_id.country_id.code'
            spot(MY, 8, "partner_id"),  # related in a customer module
            spot(MY, 12, "partner_id"),  # @api.depends('partner_id.namee')
            spot(SALE_VIEWS, 11, "partner_id"),  # view arch
            spot(MY_VIEWS, 8, "partner_id"),  # inherited view spec (model via inherit_id)
            spot(SALE, 30, "partner_id"),  # usages with an inferred receiver
            spot(SALE, 43, "partner_id"),
            spot(MY, 15, "partner_id"),
        ]
        for s in expected:
            self.assertIn(s, res)
        self.assertEqual(len(res), len(set(res)))
        self.assertEqual(len(res), len(expected))
        self.assertNotIn(spot(MY, 21, "partner_id"), res)  # company.partner_id
        self.assertNotIn(spot(COMPANY, 8, "partner_id"), res)  # res.company's related path
        self.assertEqual(res[0][0], SALE)  # current file first
        # declarations only when asked
        res = self.refs(Target(FIELD, model="sale.order", name="partner_id"), include=False, usages=self.USAGES)
        self.assertNotIn(spot(SALE, 12, "partner_id"), res)
        self.assertNotIn(spot(SALE_STOCK, 8, "partner_id"), res)
        self.assertIn(spot(SALE_VIEWS, 11, "partner_id"), res)

    def test_segment_ranges(self):
        res = R.references_for_target(self.ctx(SALE), Target(FIELD, model="res.partner", name="country_id"), True)
        texts = {}
        for loc in res:
            rel = os.path.relpath(uri_to_path(loc["uri"]), MINI)
            start, end = loc["range"]["start"], loc["range"]["end"]
            texts[(rel, start["line"])] = line_of(rel, start["line"] + 1)[start["character"] : end["character"]]
        self.assertEqual(set(texts.values()), {"country_id"})  # the segment, not the whole path string
        self.assertIn((SALE, 16), texts)  # 'partner_id.country_id.code': second segment, owned by res.partner
        self.assertIn((MY, 7), texts)

    def test_unknown_receivers_only_for_unique_names(self):
        table = {(MY, "x_note"): [usage(MY, 15, "x_note", None, "attr")]}
        res = self.refs(Target(FIELD, model="sale.order", name="x_note"), usages=table)
        self.assertIn(spot(MY, 15, "x_note"), res)
        self.assertIn(spot(MY, 7, "x_note"), res)  # declaration

    def test_record_fields_and_secondary_rank(self):
        table = {(MY, "is_company"): [usage(MY, 19, "is_company", ["res.partner"], "string")]}
        res = self.refs(Target(FIELD, model="res.partner", name="is_company"), rel=PARTNER, usages=table)
        decl = spot(PARTNER, 17, "is_company")
        view = spot("odoo/odoo/addons/base/views/res_partner_views.xml", 9, "is_company")  # invisible="is_company"
        domain = spot(MY, 19, "is_company")
        record = spot(COMPANY_DATA, 6, "is_company")  # <field name="is_company" eval="True"/>
        # current file, then customer modules (custom/ is a customer addons root), then core;
        # data records rank after code
        self.assertEqual(res, [decl, domain, view, record])

    def test_record_fields_filtered_by_record_model(self):
        res = self.refs(Target(FIELD, model="res.company", name="partner_id"), rel=COMPANY)
        self.assertIn(spot(COMPANY_DATA, 10, "partner_id"), res)  # record model="res.company"
        self.assertIn(spot(COMPANY, 8, "partner_id"), res)  # related='partner_id.name'
        self.assertNotIn(spot(SALE_VIEWS, 11, "partner_id"), res)
        res = self.refs(Target(FIELD, model="sale.order", name="partner_id"))
        self.assertNotIn(spot(COMPANY_DATA, 10, "partner_id"), res)

    def test_family(self):
        search = R._Search(self.ctx(SALE), True, R.MAX_REFERENCES)
        # res.users delegates the partner fields it does not define (_inherits)
        self.assertEqual(search.family(["res.partner"], "email", "field"), {"res.partner", "res.users"})
        self.assertEqual(search.family(["res.users"], "email", "field"), {"res.partner", "res.users"})
        self.assertEqual(search.family(["res.users"], "login", "field"), {"res.users"})
        self.assertEqual(search.family(["res.partner"], "_get_country_code", "method"), {"res.partner"})  # methods are not delegated
        # mixins having the member, models inheriting the target, base for framework members
        self.assertEqual(search.family(["sale.order"], "message_ids", "field"), {"sale.order", "mail.thread"})
        self.assertEqual(search.family(["mail.thread"], "message_post", "method"), {"mail.thread", "sale.order", "product.product"})
        self.assertIn("base", search.family(["sale.order"], "write", "method"))
        self.assertIsNone(search.family(["base"], "write", "method"))

    def test_delegated_field_usages(self):
        table = {(MY, "email"): [usage(MY, 21, "email", ["res.partner"], "attr"), usage(MY, 21, "email", ["res.users"], "attr", nth=1)]}
        res = self.refs(Target(FIELD, model="res.partner", name="email"), usages=table)
        self.assertIn(spot(MY, 21, "email"), res)
        self.assertIn(spot(MY, 21, "email", 1), res)
        res = self.refs(Target(FIELD, model="res.users", name="login"), usages={(MY, "login"): [usage(MY, 21, "email", ["res.partner"], "attr")]})
        self.assertEqual(res, [spot("odoo/odoo/addons/base/models/res_users.py", 18, "login")])

    def test_csv_headers(self):
        res = self.refs(Target(FIELD, model="ir.model.access", name="group_id"))
        for rel in ("odoo/odoo/addons/base/security/ir.model.access.csv", "odoo/addons/sale/security/ir.model.access.csv"):
            self.assertIn(spot(rel, 1, "group_id"), res)

    def test_ambiguous_target_unions_models(self):
        res = self.refs(Target(FIELD, models=["sale.order", "res.company"], name="partner_id"))
        self.assertIn(spot(SALE_VIEWS, 11, "partner_id"), res)
        self.assertIn(spot(COMPANY, 8, "partner_id"), res)
        self.assertIn(spot(COMPANY_DATA, 10, "partner_id"), res)

    def test_xml_cursor_entry_point(self):
        line, col = spot(SALE_VIEWS, 11, "partner_id")[1:]
        with mock.patch.object(R, "_usage_finder", lambda: FakeUsages()):
            res = rel_spots(R.references(self.ctx(SALE_VIEWS, line, col + 2), True))
        self.assertEqual(res[0], spot(SALE_VIEWS, 11, "partner_id"))  # current file first
        self.assertIn(spot(SALE, 12, "partner_id"), res)
        self.assertIn(spot(MY_VIEWS, 8, "partner_id"), res)


class MethodReferencesTest(_FixtureCase):
    def test_action_confirm(self):
        table = {
            (SALE_STOCK, "action_confirm"): [usage(SALE_STOCK, 11, "action_confirm", ["sale.order"], "super")],
            (SUBSCRIPTION, "action_confirm"): [usage(SUBSCRIPTION, 18, "action_confirm", ["sale.order"], "super")],
            (MY, "action_confirm"): [usage(MY, 23, "action_confirm", ["sale.order"], "super"), usage(MY, 17, "action_confirm", ["sale.order"], "string")],
        }
        res = self.refs(Target(METHOD, model="sale.order", name="action_confirm"), usages=table)
        expected = [
            spot(SALE, 25, "action_confirm"),
            spot(SALE_STOCK, 10, "action_confirm"),
            spot(SUBSCRIPTION, 17, "action_confirm"),
            spot(MY, 17, "action_confirm"),
            spot(SALE_VIEWS, 9, "action_confirm"),  # <button name="action_confirm" type="object">
            spot(SALE_STOCK, 11, "action_confirm"),  # super() calls
            spot(SUBSCRIPTION, 18, "action_confirm"),
            spot(MY, 23, "action_confirm"),
        ]
        self.assertEqual(sorted(res), sorted(expected))  # the "string" usage kind is not a method use
        res = self.refs(Target(METHOD, model="sale.order", name="action_confirm"), include=False, usages=table)
        self.assertEqual(sorted(res), sorted(expected[4:]))

    def test_compute_string(self):
        res = self.refs(Target(METHOD, model="sale.order", name="_compute_amounts"))
        self.assertIn(spot(SALE, 16, "_compute_amounts"), res)  # compute='_compute_amounts'
        self.assertIn(spot(SALE, 21, "_compute_amounts"), res)

    def test_receiver_filter(self):
        name = "message_post"
        here = spot(SALE, 30, name)

        def found(target_model: str, receivers: list[str]) -> bool:
            table = {(SALE, name): [usage(SALE, 30, name, receivers, "call")]}
            return here in self.refs(Target(METHOD, model=target_model, name=name), usages=table)

        self.assertTrue(found("mail.thread", ["sale.order"]))  # a model inheriting the mixin
        self.assertTrue(found("sale.order", ["mail.thread"]))  # the mixin defining the method
        self.assertTrue(found("sale.order", ["res.users", "sale.order"]))  # ambiguous receiver, one matches
        self.assertFalse(found("sale.order", ["product.product"]))  # sibling: another model's method
        self.assertFalse(found("mail.thread", ["res.partner"]))  # not a thread model in the fixture

    def test_class_attribute(self):
        finder = FakeUsages({(SALE, "_description"): [usage(SALE, 8, "_description", ["sale.order"], "attr")]})
        res = self.refs(Target(ATTR, model="sale.order", name="_description"), finder=finder)
        self.assertIn(spot(SALE, 8, "_description"), res)  # the model's own value
        self.assertIn(spot("odoo/addons/mail/models/mail_thread.py", 6, "_description"), res)  # the mixin's, overridden
        self.assertIn((fx(SALE), "_description"), finder.calls)

    def test_time_box_keeps_index_results(self):
        finder = FakeUsages()
        with mock.patch.object(R, "BUDGET", 0.0):
            res = self.refs(Target(METHOD, model="sale.order", name="action_confirm"), finder=finder)
        self.assertIn(spot(SALE_VIEWS, 9, "action_confirm"), res)
        self.assertIn(spot(MY, 17, "action_confirm"), res)
        self.assertLessEqual({p for p, _ in finder.calls}, {fx(SALE)})  # only the current file got analysed


class OtherReferencesTest(_FixtureCase):
    def test_model(self):
        res = self.refs(Target(MODEL, model="res.partner"), rel=PARTNER)
        expected = [
            spot(PARTNER, 4, "Partner"),  # class declaration
            spot(PARTNER, 5, "res.partner"),  # _name
            spot(PARTNER, 12, "res.partner"),  # comodel
            spot(SALE, 43, "res.partner"),  # env['res.partner']
            spot(MY, 19, "res.partner"),
            spot("odoo/odoo/addons/base/security/ir.model.access.csv", 2, "model_res_partner"),
            spot("odoo/odoo/addons/base/views/res_partner_views.xml", 5, "res.partner"),  # <field name="model">
            spot("odoo/odoo/addons/base/views/res_partner_views.xml", 22, "res.partner"),  # res_model
            spot("odoo/odoo/addons/base/models/res_users.py", 15, "res.partner"),  # _inherits
        ]
        for s in expected:
            self.assertIn(s, res)
        self.assertEqual(res[0][0], PARTNER)
        record = spot(COMPANY_DATA, 4, "res.partner")  # <record model="res.partner"> of a data file
        self.assertEqual(res[-1], record)

    def test_xmlid(self):
        res = self.refs(Target(XMLID, xmlid="base.main_company"))
        self.assertEqual(
            sorted(res),
            sorted([spot(COMPANY_DATA, 8, "main_company"), spot(SALE_VIEWS, 26, "base.main_company"), spot(MY, 18, "base.main_company")]),
        )

    def test_module(self):
        res = self.refs(Target(MODULE, name="sale"))
        self.assertEqual(res[0], spot("custom/my_module/__manifest__.py", 4, "sale"))  # customer modules first
        self.assertIn(("odoo/addons/sale/__manifest__.py", 0, 0), res)
        for rel in ("custom/my_module/__manifest__.py", "odoo/addons/sale_stock/__manifest__.py", "enterprise/sale_subscription/__manifest__.py"):
            self.assertIn(spot(rel, 4, "sale"), res)

    def test_module_python_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "helper.py")
            src = b"from odoo.addons.sale.models.sale_order import SaleOrder\nfrom odoo.addons import sale\nimport odoo.addons.sale_stock\n"
            with open(path, "wb") as fh:
                fh.write(src)
            self.reg.update_file(path, extract_source(path, "py", src))
            try:
                res = R.references_for_target(self.ctx(SALE), Target(MODULE, name="sale"), True)
            finally:
                self.reg.remove_file(path)
        spots = [(uri_to_path(loc["uri"]), loc["range"]["start"]["line"], loc["range"]["start"]["character"], loc["range"]["end"]["character"]) for loc in res]
        self.assertIn((path, 0, 17, 21), spots)  # odoo.addons.<sale>.models...
        self.assertIn((path, 1, 24, 28), spots)  # from odoo.addons import <sale>
        self.assertNotIn(1, [s[1] for s in spots if s[0] == path and s[2] == 0])
        self.assertEqual(len([s for s in spots if s[0] == path]), 2)  # sale_stock is another module

    def test_pysym_function_through_reexport(self):
        line, col = spot(FLOAT_UTILS, 5, "float_compare")[1:]
        target = Target(PYSYM, path=fx(FLOAT_UTILS), line=line, col=col, end_col=col + 13, name="float_compare")
        res = self.refs(target)
        self.assertEqual(
            sorted(res),
            sorted([spot(FLOAT_UTILS, 5, "float_compare"), spot(TOOLS_INIT, 1, "float_compare"), spot(SALE, 2, "float_compare"), spot(SALE, 28, "float_compare")]),
        )
        self.assertEqual(res[0][0], SALE)  # current file first
        self.assertNotIn(spot(FLOAT_UTILS, 5, "float_compare"), self.refs(target, include=False))

    def test_pysym_local_variable(self):
        line, col = spot(SALE, 42, "lines")[1:]
        res = self.refs(Target(PYSYM, path=fx(SALE), line=line, col=col, end_col=col + 5, name="lines"))
        self.assertEqual(res, [spot(SALE, 42, "lines"), spot(SALE, 44, "lines")])

    def test_file_targets(self):
        res = self.refs(Target(FILE, path=fx(FLOAT_UTILS)))
        self.assertIn(spot(TOOLS_INIT, 1, "float_utils"), res)
        res = self.refs(Target(FILE, path=fx(SALE_VIEWS)))
        self.assertIn(spot("odoo/addons/sale/__manifest__.py", 7, "views/sale_order_views.xml"), res)

    def test_package_entry_point_called_twice(self):
        from odoo_lens import features

        line, col = spot(SALE_VIEWS, 11, "partner_id")[1:]
        for _ in range(2):  # importing the submodule rebinds the package attribute to it
            res = rel_spots(features.references(self.ctx(SALE_VIEWS, line, col), True))
            self.assertIn(spot(SALE, 12, "partner_id"), res)

    def test_custom_roots_rank_first(self):
        saved = self.env.custom_roots
        self.env.custom_roots = [fx("custom")]
        R._INDEXES.clear()
        try:
            res = self.refs(Target(MODEL, model="sale.order"), rel="odoo/addons/sale_stock/models/sale_order.py")
        finally:
            self.env.custom_roots = saved
            R._INDEXES.clear()
        files = list(dict.fromkeys(r[0] for r in res))
        self.assertEqual(files[0], "odoo/addons/sale_stock/models/sale_order.py")
        self.assertTrue(files[1].startswith("custom/"), files)
        self.assertTrue(all(not f.startswith("custom/") for f in files[3:]), files)


class LiveUsagesTest(_FixtureCase):
    """With the real analysis.python.member_usages (skipped until it exists)."""

    def setUp(self):
        super().setUp()
        if R._usage_finder() is None:
            self.skipTest("analysis.python.member_usages not available")

    def test_sale_order_partner_id(self):
        res = rel_spots(R.references_for_target(self.ctx(SALE), Target(FIELD, model="sale.order", name="partner_id"), True))
        self.assertIn(spot(SALE, 30, "partner_id"), res)
        self.assertIn(spot(MY, 15, "partner_id"), res)
        self.assertNotIn(spot(MY, 21, "partner_id"), res)  # company.partner_id

    def test_action_confirm_super_calls(self):
        res = rel_spots(R.references_for_target(self.ctx(SALE), Target(METHOD, model="sale.order", name="action_confirm"), True))
        for s in (spot(SALE_STOCK, 11, "action_confirm"), spot(SUBSCRIPTION, 18, "action_confirm"), spot(MY, 23, "action_confirm")):
            self.assertIn(s, res)


# ---------------------------------------------------------------------------
# real checkouts


class _RealTree:
    VERSION = ""
    BUDGET = 3.5  # seconds (server budget: < 3 s on a quiet machine)

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(WORKTREES, cls.VERSION)
        if not os.path.isdir(cls.root):
            raise unittest.SkipTest(f"{cls.root} not available")
        cls.server = headless_server()
        cls.sale = os.path.join(cls.root, "odoo/addons/sale/models/sale_order.py")
        cls.env, _ = load_env(cls.server, cls.sale)
        cls.reg = cls.env.registry

    @classmethod
    def tearDownClass(cls):
        cls.env.registry = None  # one real registry in memory at a time
        cls.reg = None

    def run_refs(self, target: Target, path: str | None = None) -> tuple[list[dict], float]:
        ctx = RequestContext(self.server, self.env, self.reg, path or self.sale, 0, 0)
        t = time.perf_counter()
        res = R.references_for_target(ctx, target, True)
        return res, time.perf_counter() - t

    def texts(self, res: list[dict]) -> list[tuple[str, int, str]]:
        out = []
        for loc in res:
            path = uri_to_path(loc["uri"])
            s, e = loc["range"]["start"], loc["range"]["end"]
            line = self.server.documents.line(path, s["line"])
            out.append((os.path.relpath(path, self.root), s["line"], line[s["character"] : e["character"]]))
        return out

    def test_model_and_xmlid(self):
        res, dt = self.run_refs(Target(MODEL, model="sale.order"))
        texts = self.texts(res)
        self.assertLess(dt, 0.5)
        self.assertLessEqual(len(res), R.MAX_REFERENCES)
        # model names, implicit xml ids model_sale_order, class names of the declarations
        self.assertTrue(all(t == "sale.order" or t.endswith("model_sale_order") or t.isidentifier() for _, _, t in texts), texts[:20])
        self.assertTrue(any(p == "odoo/addons/sale/models/sale_order.py" and t == "sale.order" for p, _, t in texts))
        res, dt = self.run_refs(Target(XMLID, xmlid="base.main_company"))
        self.assertLess(dt, 0.5)
        self.assertIn(("odoo/odoo/addons/base/data/res_company_data.xml", "main_company"), {(p, t) for p, _, t in self.texts(res)})

    def test_field(self):
        res, dt = self.run_refs(Target(FIELD, model="sale.order", name="partner_id"))
        texts = self.texts(res)
        self.assertLess(dt, self.BUDGET)
        self.assertEqual({t for _, _, t in texts}, {"partner_id"})
        files = {p for p, _, _ in texts}
        self.assertIn("odoo/addons/sale/models/sale_order.py", files)
        self.assertIn("odoo/addons/sale/views/sale_order_views.xml", files)
        self.assertNotIn("odoo/odoo/addons/base/models/res_company.py", files)
        res, dt = self.run_refs(Target(FIELD, model="res.partner", name="name"))
        self.assertLess(dt, self.BUDGET)
        self.assertEqual(len(res), R.MAX_REFERENCES)
        self.assertEqual({t for _, _, t in self.texts(res)}, {"name"})

    def test_method(self):
        res, dt = self.run_refs(Target(METHOD, model="account.move", name="action_post"))
        texts = self.texts(res)
        self.assertLess(dt, self.BUDGET)
        self.assertEqual({t for _, _, t in texts}, {"action_post"})
        files = {p for p, _, _ in texts}
        self.assertIn("odoo/addons/account/models/account_move.py", files)
        self.assertIn("odoo/addons/account/views/account_move_views.xml", files)

    def test_module(self):
        res, dt = self.run_refs(Target(MODULE, name="sale"))
        self.assertLess(dt, 0.5)
        self.assertIn(("odoo/addons/sale_stock/__manifest__.py", "sale"), {(p, t) for p, _, t in self.texts(res)})

    def test_owl_template(self):
        res, dt = self.run_refs(Target(TEMPLATE, xmlid="web.FormView"))
        texts = self.texts(res)
        self.assertLess(dt, 0.5)
        self.assertEqual({t for _, _, t in texts}, {"web.FormView"})
        files = {p for p, _, _ in texts}
        self.assertIn("odoo/addons/web/static/src/views/form/form_controller.xml", files)  # t-name
        self.assertTrue(any(p.endswith(".js") for p in files), files)  # static template = "web.FormView"


class RealTree16Test(_RealTree, unittest.TestCase):
    VERSION = "16.0"


class RealTree18Test(_RealTree, unittest.TestCase):
    VERSION = "18.0"


class RealTree20Test(_RealTree, unittest.TestCase):
    VERSION = "20.0"


if __name__ == "__main__":
    unittest.main()
