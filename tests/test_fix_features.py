from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens.analysis.targets import METHOD, PYSYM, XMLID, Target  # noqa: E402
from odoo_lens.cli import _ctx, headless_server, load_env  # noqa: E402
from odoo_lens.documents import uri_to_path  # noqa: E402
from odoo_lens.facts import REF_METHOD  # noqa: E402
from odoo_lens.features import calls  # noqa: E402
from odoo_lens.features.hover import hover, hover_for_target  # noqa: E402
from odoo_lens.features.navigation import MAX_UNKNOWN_RECEIVER_MODELS, trust_unknown_receiver  # noqa: E402
from odoo_lens.registry import RefHit  # noqa: E402

# features.references / .hover are the entry functions, not the submodules
R = importlib.import_module("odoo_lens.features.references")

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")
SALE = "odoo/addons/sale/models/sale_order.py"
MY = "custom/my_module/models/sale_order.py"
UTILS = "odoo/addons/sale/static/src/js/utils.js"
INDEX = "odoo/addons/sale/static/src/js/index.js"
WIDGET = "custom/cron_ext/static/src/js/widget.js"
OTHER = "custom/cron_ext/static/src/js/other.js"
BARREL = "odoo/addons/sale/static/src/js/barrel.js"
FMT = "odoo/addons/sale/static/src/js/fmt.js"
RENAMED = "odoo/addons/sale/static/src/js/renamed.js"
DEFAULT_EXPR = "odoo/addons/sale/static/src/js/default_expr.js"
USER = "custom/cron_ext/static/src/js/user.js"
CRON = "custom/cron_ext/data/cron.xml"
MIGRATION = "custom/cron_ext/migrations/18.0.1.1/post-migrate.py"
EXT_MODELS = "custom/cron_ext/models/models.py"

EXTRA = {
    UTILS: textwrap.dedent(
        """\
        /** @odoo-module **/

        /**
         * Build a unique key.
         * @param {string} prefix
         */
        export function makeKey(prefix = "", { sep } = {}) {
            return prefix + makeKey.next++;
        }
        makeKey.next = 0;

        export default class Helper {
            run() {
                return makeKey("h");
            }
        }
        """
    ),
    INDEX: 'export { makeKey } from "./utils";\n',
    FMT: textwrap.dedent(
        """\
        /** @odoo-module **/

        /** Doc of another symbol. */
        export const SEP = ", ";
        /* plain comment */
        export function formatAll(list) {
            return list.join(SEP);
        }
        """
    ),
    BARREL: 'export * from "@sale/js/fmt";\n',
    RENAMED: 'export { makeKey as mk } from "./utils";\n',
    DEFAULT_EXPR: textwrap.dedent(
        """\
        import { registry } from "@sale/js/utils";

        registry.add("x");
        export default registry.child;
        """
    ),
    USER: textwrap.dedent(
        """\
        /** @odoo-module **/
        import { formatAll } from "@sale/js/barrel";
        import { mk } from "@sale/js/renamed";
        import { makeKey } from "@sale/js/utils";

        export const all = formatAll([mk("u"), makeKey("v")]);
        """
    ),
    "custom/cron_ext/__init__.py": "from . import models\n",
    "custom/cron_ext/__manifest__.py": "{'name': 'Cron ext', 'version': '18.0.1.1', 'depends': ['sale'], 'data': ['data/cron.xml', 'views/views.xml'], 'license': 'LGPL-3'}\n",
    "custom/cron_ext/models/__init__.py": "from . import models\n",
    EXT_MODELS: textwrap.dedent(
        """\
        from odoo import fields, models


        class SaleOrder(models.Model):
            _inherit = 'sale.order'

            x_flag = fields.Boolean()


        class ResPartner(models.Model):
            _inherit = 'res.partner'

            def action_twice(self):
                return True

            def x_flag(self):
                return True


        class ResCompany(models.Model):
            _inherit = 'res.company'

            def action_twice(self):
                return True
        """
    ),
    CRON: textwrap.dedent(
        """\
        <odoo>
            <record id="cron_confirm" model="ir.cron">
                <field name="name">Confirm orders</field>
                <field name="model_id" ref="sale.model_sale_order"/>
                <field name="state">code</field>
                <field name="code">model.action_confirm()</field>
            </record>
            <record id="action_confirm_server" model="ir.actions.server">
                <field name="name">Confirm</field>
                <field name="model_id" ref="sale.model_sale_order"/>
                <field name="state">code</field>
                <field name="code">records.action_confirm()</field>
            </record>
        </odoo>
        """
    ),
    "custom/cron_ext/views/views.xml": textwrap.dedent(
        """\
        <odoo>
            <record id="qweb_form" model="ir.ui.view">
                <field name="name">qweb.form</field>
                <field name="type">qweb</field>
                <field name="arch" type="xml">
                    <form><div/></form>
                </field>
            </record>
        </odoo>
        """
    ),
    MIGRATION: textwrap.dedent(
        """\
        from odoo import SUPERUSER_ID, api


        def migrate(cr, version):
            env = api.Environment(cr, SUPERUSER_ID, {})
            env['sale.order'].search([]).action_confirm()
        """
    ),
    WIDGET: textwrap.dedent(
        """\
        /** @odoo-module **/
        import { makeKey as key } from "@sale/js/utils";
        import Helper from "@sale/js/utils";
        import * as utils from "@sale/js/utils";

        export function widget() {
            // makeKey and key in a comment
            const label = "makeKey";
            return key("w") + utils.makeKey("x") + new Helper().run() + label;
        }
        """
    ),
    OTHER: textwrap.dedent(
        """\
        /** @odoo-module **/
        import { makeKey } from "@sale/js/index";

        export const other = makeKey("o");
        """
    ),
}


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


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-fix-features-")
        cls.root = os.path.join(cls.tmp, "mini")
        shutil.copytree(MINI, cls.root)
        for rel, text in EXTRA.items():
            os.makedirs(os.path.dirname(os.path.join(cls.root, rel)), exist_ok=True)
            with open(os.path.join(cls.root, rel), "w", encoding="utf-8") as fh:
                fh.write(text)
        cls.server = headless_server()
        with mock.patch.dict(os.environ, {"ODOO_LENS_CACHE_DIR": os.path.join(cls.tmp, "cache")}):
            cls.env, _ = load_env(cls.server, os.path.join(cls.root, MY))
        cls.reg = cls.env.registry

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        R._INDEXES.clear()

    def ctx(self, rel: str, needle: str | None = None, nth: int = 0, offset: int = 0):
        line, col = at(self.root, rel, needle, nth) if needle else (1, 1)
        return _ctx(self.server, self.env, os.path.join(self.root, rel), line, col + offset)

    def spot(self, rel: str, needle: str, nth: int = 0) -> tuple[str, int, int]:
        return (rel, *at(self.root, rel, needle, nth))

    def spots(self, locs: list[dict]) -> list[tuple[str, int, int]]:
        return [
            (os.path.relpath(uri_to_path(loc["uri"]), self.root), loc["range"]["start"]["line"] + 1, loc["range"]["start"]["character"] + 1)
            for loc in locs
        ]


class JsSymbolTest(_Case):
    def test_hover_js_fence_and_declaration(self):
        md = hover(self.ctx(WIDGET, 'key("w")'))["contents"]["value"]
        self.assertIn("```js\nexport function makeKey(prefix = \"\", { sep } = {})\n```", md)
        self.assertNotIn("```python", md)
        self.assertIn("*Build a unique key.*", md)
        self.assertIn(f"@sale/js/utils — {os.path.join(self.root, UTILS)}:{at(self.root, UTILS, 'makeKey(prefix')[0]}", md)

    def test_hover_default_export_class(self):
        md = hover(self.ctx(WIDGET, "new Helper", offset=5))["contents"]["value"]
        self.assertIn("```js\nexport default class Helper\n```", md)

    def test_references_follow_js_imports(self):
        res = self.spots(R.references(self.ctx(WIDGET, 'key("w")'), True))
        expected = {
            self.spot(UTILS, "makeKey(prefix"),
            self.spot(UTILS, "makeKey.next++"),
            self.spot(UTILS, "makeKey.next = 0"),
            self.spot(UTILS, 'makeKey("h")'),
            self.spot(WIDGET, "key }"),
            self.spot(WIDGET, 'key("w")'),
            self.spot(WIDGET, 'makeKey("x")'),
            self.spot(INDEX, "makeKey"),
            self.spot(OTHER, "makeKey }"),
            self.spot(OTHER, 'makeKey("o")'),
            self.spot(USER, "makeKey }"),
            self.spot(USER, 'makeKey("v")'),
            self.spot(RENAMED, "mk }"),
            self.spot(USER, "mk }"),
            self.spot(USER, 'mk("u")'),
        }
        self.assertEqual(set(res), expected)
        self.assertEqual(res[0][0], WIDGET)

    def test_references_default_export(self):
        res = set(self.spots(R.references(self.ctx(WIDGET, "new Helper", offset=5), True)))
        self.assertEqual(res, {self.spot(UTILS, "Helper {"), self.spot(WIDGET, "Helper from"), self.spot(WIDGET, "Helper()")})

    def test_references_from_declaration(self):
        line, col = at(self.root, UTILS, "makeKey(prefix")
        target = Target(PYSYM, name="makeKey", path=os.path.join(self.root, UTILS), line=line - 1, col=col - 1, end_col=col + 6)
        res = set(self.spots(R.references_for_target(self.ctx(UTILS), target, False)))
        self.assertNotIn(self.spot(UTILS, "makeKey(prefix"), res)
        self.assertIn(self.spot(OTHER, 'makeKey("o")'), res)

    def test_references_through_star_reexport(self):
        res = set(self.spots(R.references(self.ctx(FMT, "formatAll"), True)))
        self.assertIn(self.spot(USER, "formatAll }"), res)
        self.assertIn(self.spot(USER, "formatAll(["), res)

    def test_references_through_renamed_reexport(self):
        res = set(self.spots(R.references(self.ctx(USER, 'mk("u")'), True)))
        self.assertIn(self.spot(USER, 'mk("u")'), res)
        self.assertIn(self.spot(RENAMED, "mk }"), res)
        self.assertIn(self.spot(UTILS, "makeKey(prefix"), res)
        self.assertIn(self.spot(OTHER, 'makeKey("o")'), res)
        self.assertIn(self.spot(USER, 'makeKey("v")'), res)

    def test_hover_skips_plain_comment(self):
        md = hover(self.ctx(FMT, "formatAll"))["contents"]["value"]
        self.assertIn("```js\nexport function formatAll(list)\n```", md)
        self.assertNotIn("Doc of another symbol", md)

    def test_default_export_expression_has_no_local(self):
        line = at(self.root, DEFAULT_EXPR, "registry.child")[0] - 1
        with open(os.path.join(self.root, DEFAULT_EXPR), encoding="utf-8") as fh:
            code = R._PyCode(fh.read(), R._JS_SKIP, False)
        self.assertIsNone(R._js_declared(code, line, len("export default ")))
        self.assertEqual(R._js_declared(code, 0, len("import { ")), "registry")

    def test_file_references_through_js_specifiers(self):
        target = Target("file", path=os.path.join(self.root, UTILS))
        res = set(self.spots(R.references_for_target(self.ctx(WIDGET), target, False)))
        self.assertIn(self.spot(WIDGET, "@sale/js/utils"), res)
        self.assertIn(self.spot(INDEX, "./utils"), res)


class CallHierarchyFixTest(_Case):
    def incoming_for(self, rel: str, needle: str) -> dict[str, dict]:
        item = calls.prepare(self.ctx(rel, needle, offset=4))[0]
        start = item["selectionRange"]["start"]
        from odoo_lens.server import RequestContext

        ictx = RequestContext(self.server, self.env, self.reg, uri_to_path(item["uri"]), start["line"], start["character"])
        return {c["from"]["name"]: c for c in calls.incoming(ictx, item)}

    def test_cron_and_server_action_code(self):
        callers = self.incoming_for(SALE, "def action_confirm")
        cron = callers["cron_ext.cron_confirm"]
        self.assertEqual(cron["from"]["detail"], "cron code")
        self.assertEqual(os.path.relpath(uri_to_path(cron["from"]["uri"]), self.root), CRON)
        line, col = at(self.root, CRON, "action_confirm()")
        self.assertEqual(cron["fromRanges"][0]["start"], {"line": line - 1, "character": col - 1})
        self.assertEqual(callers["cron_ext.action_confirm_server"]["from"]["detail"], "server action code")
        self.assertIn("migrate", callers)

    def test_compute_sql_role(self):
        line, col = at(self.root, SALE, "country_code =")
        fake = RefHit(os.path.join(self.root, SALE), "sale", (REF_METHOD, "_compute_amounts", line - 1, col - 1, col + 15, ("compute_sql", "sale.order")))
        original = self.reg.refs_by_name

        def refs_by_name(kind, name):
            return original(kind, name) + ([fake] if kind == REF_METHOD and name == "_compute_amounts" else [])

        with mock.patch.object(self.reg, "refs_by_name", refs_by_name):
            callers = self.incoming_for(SALE, "def _compute_amounts")
        self.assertEqual(callers["sale.order.country_code"]["from"]["detail"], "compute_sql=")

    def test_prepare_marks_uninferred_receivers(self):
        ctx = self.ctx(SALE, "def action_confirm", offset=4)
        cases = [
            (Target(METHOD, models=["sale.order", "res.partner"], name="action_confirm"), "sale · receiver not inferred (candidates: sale.order, res.partner)"),
            (Target(METHOD, model="sale.order", name="action_confirm", extra={"guess": "name"}), "sale · receiver not inferred (candidates: sale.order)"),
            (Target(METHOD, model="sale.order", name="action_confirm"), "sale"),
        ]
        for target, detail in cases:
            with mock.patch.object(calls, "targets_at", lambda _ctx, t=target: [t]):
                item = calls.prepare(ctx)[0]
            self.assertEqual((item["name"], item["detail"]), ("sale.order.action_confirm", detail))


class UnknownReceiverPolicyTest(_Case):
    def test_one_rule_for_references_and_calls(self):
        reg = self.reg
        self.assertEqual(MAX_UNKNOWN_RECEIVER_MODELS, 1)
        self.assertTrue(trust_unknown_receiver(reg, "action_confirm"))
        self.assertFalse(trust_unknown_receiver(reg, "action_twice"))
        self.assertIn("", reg.models_with_member("write"))
        self.assertFalse(trust_unknown_receiver(reg, "write"))
        for name in ("action_confirm", "action_twice", "write"):
            self.assertEqual(calls._receiver_filter(reg, "sale.order", name)(None), trust_unknown_receiver(reg, name), name)

    def test_references_drop_unknown_receivers_of_shared_names(self):
        rel = EXT_MODELS
        partner, company = at(self.root, rel, "action_twice"), at(self.root, rel, "action_twice", 1)

        def finder(registry, path, source, name):
            if path == os.path.join(self.root, rel):
                return [(partner[0] - 1, partner[1] - 1, partner[1] + 11, ["res.partner"], "call"), (company[0] - 1, company[1] - 1, company[1] + 11, None, "call")]
            return []

        with mock.patch.object(R, "_usage_finder", lambda: finder):
            res = self.spots(R.references_for_target(self.ctx(rel), Target(METHOD, model="res.partner", name="action_twice"), False))
        self.assertIn((rel, *partner), res)
        self.assertNotIn((rel, *company), res)


class NeverLoadedFilesTest(_Case):
    def test_migration_scripts_ranked_last(self):
        res = self.spots(R.references_for_target(self.ctx(SALE), Target(METHOD, model="sale.order", name="action_confirm"), True))
        self.assertEqual(res[-1], self.spot(MIGRATION, "action_confirm"))
        self.assertEqual(sum(1 for r in res if r[0] == MIGRATION), 1)


class HoverViewTypeTest(_Case):
    def test_explicit_view_type_wins(self):
        rec = self.reg.records("cron_ext.qweb_form")[0]
        self.assertEqual((rec.info.get("view_type"), rec.info.get("type")), ("qweb", "form"))
        md = hover_for_target(self.ctx(MY), Target(XMLID, xmlid="cron_ext.qweb_form"))
        self.assertEqual(md.split("\n")[-1], "qweb · primary · priority 16")


if __name__ == "__main__":
    unittest.main()
