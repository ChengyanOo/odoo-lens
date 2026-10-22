from __future__ import annotations

import gc
import importlib
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens import features  # noqa: E402
from odoo_lens.cli import headless_server, load_env  # noqa: E402
from odoo_lens.extract import extract_source  # noqa: E402
from odoo_lens.server import RequestContext  # noqa: E402

diag_module = importlib.import_module("odoo_lens.features.diagnostics")
diagnostics = diag_module.diagnostics

MINI = os.path.join(ROOT, "tests", "fixtures", "mini")
WORKTREES = os.path.expanduser(os.environ.get("ODOO_LENS_WORKTREES", "~/odev/worktrees"))
CHECKED = (".py", ".xml", ".csv")


def position(path: str, needle: str, nth: int = 0) -> tuple[int, int]:
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    seen = 0
    for i, line in enumerate(lines):
        col = line.find(needle)
        while col >= 0:
            if seen == nth:
                return i, col
            seen += 1
            col = line.find(needle, col + 1)
    raise AssertionError(f"{needle!r} not found in {path}")


class DiagnosticsCase(unittest.TestCase):
    server = None
    env = None
    root = ""

    @classmethod
    def release(cls) -> None:
        if cls.env is not None:
            cls.env.registry = None
        cls.server = cls.env = None
        gc.collect()

    def diags(self, rel: str) -> list[dict]:
        path = os.path.join(self.root, rel)
        return diagnostics(RequestContext(self.server, self.env, self.env.registry, path))

    def find(self, rel: str, code: str, needle: str, nth: int = 0) -> dict:
        line, col = position(os.path.join(self.root, rel), needle, nth)
        found = self.diags(rel)
        for d in found:
            start = d["range"]["start"]
            if d["code"] == code and start["line"] == line and start["character"] == col:
                return d
        self.fail(f"no {code} at {rel}:{line + 1}:{col + 1} ({needle!r}); got {[(d['code'], d['range']['start'], d['message']) for d in found]}")

    def codes(self, rel: str) -> list[str]:
        return sorted(d["code"] for d in self.diags(rel))

    def assert_none(self, rel: str, code: str | None = None) -> None:
        found = [d for d in self.diags(rel) if code is None or d["code"] == code]
        self.assertEqual([], [(d["code"], d["range"]["start"]["line"] + 1, d["message"]) for d in found], rel)

    def module_files(self, module_dir: str) -> list[str]:
        out = []
        for d, dirs, files in os.walk(module_dir):
            dirs[:] = [x for x in dirs if x not in ("static", "__pycache__")]
            out += [os.path.join(d, f) for f in files if f.endswith(CHECKED)]
        return sorted(out)


class TestFixture(DiagnosticsCase):
    MY = "custom/my_module"

    @classmethod
    def setUpClass(cls):
        cls.root = MINI
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(MINI, cls.MY, "models", "sale_order.py"))

    @classmethod
    def tearDownClass(cls):
        cls.release()

    def test_custom_model_file(self):
        rel = f"{self.MY}/models/sale_order.py"
        d = self.find(rel, "unknown-model", "res.partnr")
        self.assertIn("did you mean 'res.partner'", d["message"])
        self.assertIn("comodel of field 'x_bad'", d["message"])
        d = self.find(rel, "unknown-field", "namee")
        self.assertIn("model 'res.partner'", d["message"])
        self.assertEqual(d["range"]["end"]["character"] - d["range"]["start"]["character"], len("namee"))
        d = self.find(rel, "missing-dependency", "sale.subscription.plan")
        self.assertIn("'sale_subscription'", d["message"])
        self.assertIn("add 'sale_subscription' to depends", d["message"])
        d = self.find(rel, "unknown-xmlid", "base.no_such_record")
        self.assertIn("module 'base' defines no record 'no_such_record'", d["message"])
        self.assertEqual(4, len(self.diags(rel)))

    def test_uncertain_when_a_dependency_is_missing(self):
        d = self.find(f"{self.MY}/models/sale_order.py", "unknown-model", "res.partnr")
        self.assertEqual(2, d["severity"])
        self.assertIn("'unknown_module' is not in the addons path", d["message"])
        d = self.find(f"{self.MY}/models/sale_order.py", "unknown-xmlid", "base.no_such_record")
        self.assertNotIn("unverified", d["message"])

    def test_custom_view(self):
        rel = f"{self.MY}/views/sale_order_views.xml"
        d = self.find(rel, "unknown-field", "no_such_field")
        self.assertIn("'sale.order'", d["message"])
        self.assertEqual(["unknown-field"], self.codes(rel))

    def test_custom_manifest(self):
        rel = f"{self.MY}/__manifest__.py"
        self.assertEqual(1, self.find(rel, "unknown-module", "unknown_module")["severity"])
        d = self.find(rel, "missing-file", "views/missing_file.xml")
        self.assertIn("module 'my_module'", d["message"])
        self.assertEqual(["missing-file", "unknown-module"], self.codes(rel))

    def test_odoo_modules_are_clean(self):
        files = self.module_files(os.path.join(MINI, "odoo")) + self.module_files(os.path.join(MINI, "enterprise"))
        self.assertGreater(len(files), 30)
        for path in files:
            self.assert_none(os.path.relpath(path, MINI))

    def test_shape_and_order(self):
        found = self.diags(f"{self.MY}/models/sale_order.py") + self.diags(f"{self.MY}/__manifest__.py")
        for d in found:
            self.assertEqual("odoo-lens", d["source"])
            self.assertIn(d["severity"], (1, 2, 3, 4))
            self.assertNotIn("\n", d["message"])
            self.assertEqual(d["range"]["start"]["line"], d["range"]["end"]["line"])
        found = self.diags(f"{self.MY}/models/sale_order.py")
        keys = [(d["severity"], d["range"]["start"]["line"], d["range"]["start"]["character"]) for d in found]
        self.assertEqual(sorted(keys), keys)

    def test_package_entry_point_stays_callable(self):
        # a submodule import must not rebind features.diagnostics to the module
        path = os.path.join(MINI, self.MY, "models", "sale_order.py")
        for _ in range(2):
            ctx = RequestContext(self.server, self.env, self.env.registry, path)
            self.assertEqual(4, len(features.diagnostics(ctx)))

    def test_framework_and_js_files(self):
        self.assert_none("odoo/odoo/models.py")
        self.assert_none("odoo/odoo/fields.py")


def _base_extra(version: tuple[int, int]) -> str:
    groups = "group_ids" if version >= (19, 0) else "groups_id"
    access = (
        """
        class IrAccess(models.Model):
            _name = 'ir.access'
            _description = 'Access'

            name = fields.Char()
            model_id = fields.Many2one('ir.model')
            group_id = fields.Many2one('res.groups')
            operation = fields.Char()
            domain = fields.Char()
        """
        if version >= (19, 4)
        else """
        class IrModelAccess(models.Model):
            _name = 'ir.model.access'
            _description = 'Access'

            name = fields.Char()
            model_id = fields.Many2one('ir.model')
            group_id = fields.Many2one('res.groups')
            perm_read = fields.Boolean()
            perm_write = fields.Boolean()
            perm_create = fields.Boolean()
            perm_unlink = fields.Boolean()
        """
    )
    return textwrap.dedent(
        f"""
        from odoo import fields, models


        class IrModel(models.Model):
            _name = 'ir.model'
            _description = 'Models'

            name = fields.Char()
            model = fields.Char()


        class IrModelFields(models.Model):
            _name = 'ir.model.fields'
            _description = 'Fields'

            name = fields.Char()
            model_id = fields.Many2one('ir.model')


        class IrSequence(models.Model):
            _name = 'ir.sequence'
            _description = 'Sequence'

            name = fields.Char()

            def next_by_code(self, code):
                return code


        class IrUiMenu(models.Model):
            _name = 'ir.ui.menu'
            _description = 'Menu'

            name = fields.Char()
            action = fields.Char()
            parent_id = fields.Many2one('ir.ui.menu')
            {groups} = fields.Many2many('res.groups')


        class IrActionsActWindow(models.Model):
            _name = 'ir.actions.act_window'
            _description = 'Window action'

            name = fields.Char()
            res_model = fields.Char()
            view_mode = fields.Char()
            view_id = fields.Many2one('ir.ui.view')
            context = fields.Char()
            {groups} = fields.Many2many('res.groups')


        class IrUiView(models.Model):
            _inherit = 'ir.ui.view'

            type = fields.Char()
            mode = fields.Char()
            priority = fields.Integer()
            {groups} = fields.Many2many('res.groups')
        """
    ) + textwrap.dedent(access)


LAB = {
    "custom/diag_lab/__init__.py": "from . import models\n",
    "custom/diag_lab/__manifest__.py": """
        {
            'name': 'Diag Lab',
            'version': '1.0',
            'depends': ['sale', 'mail'],
            'data': [
                'security/ir.model.access.csv',
                'views/lab_views.xml',
                'data/lab_data.xml',
                'views/broken.xml',
                'views/lab_viewz.xml',
            ],
            'license': 'LGPL-3',
        }
        """,
    "custom/diag_lab/models/__init__.py": "from . import lab, lab_versioned\n",
    "custom/diag_lab/models/lab.py": """
        from odoo import api, fields, models


        class LabMixin(models.AbstractModel):
            _name = 'diag.lab.mixin'
            _description = 'Lab mixin'

            total = fields.Float(compute='_compute_total')

            @api.depends('line_ids.amount')
            def _compute_total(self):
                for rec in self:
                    rec.total = 0


        class Lab(models.Model):
            _name = 'diag.lab'
            _inherit = ['diag.lab.mixin']
            _description = 'Lab'
            _order = 'nope_order desc, id'
            _rec_name = 'nope_rec'

            name = fields.Char()
            partner_id = fields.Many2one('res.partner')
            line_ids = fields.One2many('diag.lab.line', 'lab_id')
            bad_line_ids = fields.One2many('diag.lab.line', 'nope_inverse')
            amount = fields.Monetary(currency_field='nope_currency')
            name_code = fields.Char(related='name.nope_path')
            partner_name = fields.Char(related='partner_id.name')
            computed = fields.Char(compute='_compute_nope')
            flag = fields.Boolean(default='_default_flag')
            partner2_id = fields.Many2one('res.partner', domain=[('nope_domain', '=', 1)])
            bridge_id = fields.Many2one('diag.bridge.model')
            other_id = fields.Many2one('diag.other.model')

            def _default_flag(self):
                return True

            @api.constrains('partner_id.name')
            def _check_dotted(self):
                pass

            @api.depends('name')
            @api.depends('replaced_depends')
            def _compute_name_code(self):
                pass

            def action_guarded(self):
                if 'guarded.model' in self.env:
                    self.env['guarded.model'].search([])

            def action_lab(self):
                self.env['no.such.model'].search([])
                self.env.ref('base.no_such_xmlid')
                self.env.ref('base.soft_xmlid', raise_if_not_found=False)
                self.env['account.chart.template'].ref('base.template_only')
                # self.env.ref('base.commented_out')
                self.env.ref('base.prefix_' + self.name)
                self.env['diag.bridge.model'].search([])
                self.env.user.has_group('sale_subscription.group_subscription_user')
                self.env.user.has_group('sale_subscription.group_whatever')
                return self.env.ref('base.main_company')


        class LabLine(models.Model):
            _name = 'diag.lab.line'
            _description = 'Lab line'

            lab_id = fields.Many2one('diag.lab')
            amount = fields.Float()


        class LabPlan(models.Model):
            _inherit = 'sale.subscription.plan'
        """,
    "custom/diag_lab/models/lab_versioned.py": """
        from odoo import api, fields, models


        class LabVersioned(models.Model):
            _name = 'diag.lab.versioned'
            _description = 'Versioned'
            _sql_constraints = [('name_uniq', 'unique(name)', 'Name must be unique')]

            name = fields.Char(states={'draft': [('readonly', False)]})
            qty = fields.Integer(group_operator='sum')
            partner_id = fields.Many2one('res.partner', auto_join=True)

            def name_get(self):
                return []

            @api.returns('self')
            def copy_lab(self):
                return self

            def check_groups(self):
                return self.user_has_groups('base.group_user')


        class LabPartner(models.Model):
            _inherit = ['res.partner']


        class LabDelegate(models.Model):
            _name = 'diag.lab.delegate'
            _description = 'Delegate'
            _inherits = {'res.partner': 'partner_id'}
        """,
    "custom/diag_lab/models/broken.py": "def oops(:\n    pass\n",
    "custom/diag_lab/security/ir.model.access.csv": """
        id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink,perm_nope
        access_lab,lab,model_diag_lab,base.group_user,1,1,1,1,1
        access_lab_bad,lab bad,model_no_such_model,base.group_user,1,1,1,1,1
        access_lab_typo,lab typo,model_diag_labs,base.group_user,1,1,1,1,1
        access_lab_sale,lab sale,model_sale_orders,base.group_user,1,1,1,1,1
        """,
    "custom/diag_lab/data/template/account.tax-xx.csv": """
        id,name,nope_column
        tax_1,Tax,whatever
        """,
    "custom/diag_lab/views/lab_views.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="lab_form" model="ir.ui.view">
                <field name="name">diag.lab.form</field>
                <field name="model">diag.lab</field>
                <field name="arch" type="xml">
                    <form>
                        <header>
                            <button name="action_lab" type="object" string="Go"/>
                            <button name="action_nope" type="object" string="Nope"/>
                            <button name="_private_method" type="object" string="Private"/>
                            <button name="cancel" special="cancel" type="object"/>
                        </header>
                        <field name="name" attrs="{'invisible': [('name', '=', False)]}"/>
                        <field name="no_such_lab_field"/>
                        <field name="line_ids">
                            <tree>
                                <field name="amount"/>
                                <field name="no_such_line_field"/>
                            </tree>
                        </field>
                    </form>
                </field>
            </record>
            <record id="lab_line_list" model="ir.ui.view">
                <field name="name">diag.lab.line.list</field>
                <field name="model">diag.lab.line</field>
                <field name="arch" type="xml">
                    <tree>
                        <groupby name="lab_id">
                            <field name="partner_id"/>
                        </groupby>
                        <field name="amount" states="draft"/>
                    </tree>
                </field>
            </record>
            <record id="sale_form_lab" model="ir.ui.view">
                <field name="name">sale.order.form.lab</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="sale.view_order_form"/>
                <field name="arch" type="xml">
                    <field name="product_id" position="after">
                        <field name="price_unit"/>
                    </field>
                    <field name="partner_id" position="attributes">
                        <field name="readonly">1</field>
                    </field>
                    <field name="no_such_sale_field" position="after"/>
                    <xpath expr="//field[@name='order_line']/tree" position="inside">
                        <field name="product_uom_qty"/>
                    </xpath>
                </field>
            </record>
            <record id="lab_action" model="ir.actions.act_window">
                <field name="name">Labs</field>
                <field name="res_model">diag.lab</field>
                <field name="view_mode">tree,form</field>
                <field name="context">{'tree_view_ref': 'diag_lab.lab_line_list'}</field>
            </record>
        </odoo>
        """,
    "custom/diag_lab/data/lab_data.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="lab_record" model="diag.lab">
                <field name="name">Lab</field>
                <field name="no_such_record_field">x</field>
                <field name="partner_id" ref="base.no_such_partner"/>
            </record>
            <record id="lab_unknown_model" model="no.such.data.model">
                <field name="name">x</field>
            </record>
            <record id="lab_plan" model="sale.subscription.plan">
                <field name="name">Plan</field>
            </record>
            <record id="lab_menu_record" model="ir.ui.menu">
                <field name="name">Menu</field>
                <field name="groups_id" eval="[]"/>
            </record>
            <menuitem id="lab_menu" name="Lab" action="lab_action" groups="base.group_user"/>
            <function model="res.partner" name="nope_function"/>
        </odoo>
        """,
    "custom/diag_lab/views/broken.xml": "<odoo>\n    <record id='x'>\n</odoo>\n",
    "enterprise/sale_subscription/security/groups.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="group_subscription_user" model="res.groups">
                <field name="name">Subscriptions</field>
            </record>
        </odoo>
        """,
    "enterprise/sale_subscription/__manifest__.py": """
        {
            'name': 'Subscriptions',
            'version': '1.1',
            'depends': ['sale'],
            'data': ['security/groups.xml'],
            'license': 'OEEL-1',
        }
        """,
    "custom/diag_lab/tests/broken_fixture.xml": "<odoo><unclosed></odoo>\n",
    "custom/diag_bridge/__init__.py": "from . import models\n",
    "custom/diag_bridge/__manifest__.py": "{'name': 'Bridge', 'depends': ['sale', 'mail'], 'auto_install': True, 'license': 'LGPL-3'}\n",
    "custom/diag_bridge/models.py": """
        from odoo import models


        class BridgeModel(models.Model):
            _name = 'diag.bridge.model'
            _description = 'Bridge'
        """,
    "custom/diag_other/__init__.py": "from . import models\n",
    "custom/diag_other/__manifest__.py": "{'name': 'Other', 'depends': ['sale'], 'license': 'LGPL-3'}\n",
    "custom/diag_other/models.py": """
        from odoo import models


        class OtherModel(models.Model):
            _name = 'diag.other.model'
            _description = 'Other'
        """,
}


class SyntheticCase(DiagnosticsCase):
    version: tuple[int, int] = (18, 0)
    _tmp: tempfile.TemporaryDirectory
    _cache_env: str | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="odoo-lens-diag-")
        cls._cache_env = os.environ.get("ODOO_LENS_CACHE_DIR")
        os.environ["ODOO_LENS_CACHE_DIR"] = os.path.join(cls._tmp.name, "cache")
        cls.root = os.path.join(cls._tmp.name, "mini")
        shutil.copytree(MINI, cls.root)
        major, minor = cls.version
        release = f"('saas~{major}', {minor}, 0, FINAL, 0, '')" if minor else f"({major}, 0, 0, FINAL, 0, '')"
        with open(os.path.join(cls.root, "odoo/odoo/release.py"), "w", encoding="utf-8") as fh:
            fh.write(f"RELEASE_LEVELS = [ALPHA, BETA, RELEASE_CANDIDATE, FINAL] = ['alpha', 'beta', 'candidate', 'final']\nversion_info = {release}\n")
        files = dict(LAB)
        files["odoo/odoo/addons/base/models/ir_extra.py"] = _base_extra(cls.version)
        for rel, content in files.items():
            path = os.path.join(cls.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(content).lstrip("\n"))
        with open(os.path.join(cls.root, "odoo/odoo/addons/base/models/__init__.py"), "a", encoding="utf-8") as fh:
            fh.write("from . import ir_extra\n")
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, os.path.join(cls.root, "custom/diag_lab/models/lab.py"))
        assert cls.env.version.key() == cls.version, cls.env.version

    @classmethod
    def tearDownClass(cls):
        cls.release()
        if cls._cache_env is None:
            os.environ.pop("ODOO_LENS_CACHE_DIR", None)
        else:
            os.environ["ODOO_LENS_CACHE_DIR"] = cls._cache_env
        cls._tmp.cleanup()

    LAB_PY = "custom/diag_lab/models/lab.py"
    VERSIONED = "custom/diag_lab/models/lab_versioned.py"
    VIEWS = "custom/diag_lab/views/lab_views.xml"
    DATA = "custom/diag_lab/data/lab_data.xml"
    ACCESS = "custom/diag_lab/security/ir.model.access.csv"
    MANIFEST = "custom/diag_lab/__manifest__.py"

    def codes_at(self, rel: str, needle: str) -> list[str]:
        line, col = position(os.path.join(self.root, rel), needle)
        return [d["code"] for d in self.diags(rel) if d["range"]["start"] == {"line": line, "character": col}]


class TestSynthetic18(SyntheticCase):
    version = (18, 0)

    def test_python_field_strings(self):
        rel = self.LAB_PY
        self.assertIn("'diag.lab'", self.find(rel, "unknown-field", "nope_order")["message"])
        self.find(rel, "unknown-field", "nope_rec")
        self.assertIn("'diag.lab.line'", self.find(rel, "unknown-field", "nope_inverse")["message"])
        self.find(rel, "unknown-field", "nope_currency")
        d = self.find(rel, "unknown-field", "nope_path")
        self.assertIn("'name' (Char field of model 'diag.lab', not relational)", d["message"])
        self.assertEqual(2, self.find(rel, "unknown-field", "nope_domain")["severity"])
        self.assertIn("dotted paths", self.find(rel, "unknown-field", "partner_id.name", 1)["message"])
        self.assertEqual([], self.codes_at(rel, "replaced_depends"))  # an outer @api.depends replaces it
        self.assertEqual([], self.codes_at(rel, "line_ids.amount"))

    def test_python_methods(self):
        rel = self.LAB_PY
        d = self.find(rel, "unknown-method", "_compute_nope")
        self.assertEqual(1, d["severity"])
        self.assertIn("compute= of field 'computed'", d["message"])
        self.assertIn("plain string value", self.find(rel, "invalid-default", "_default_flag")["message"])

    def test_python_models_and_dependencies(self):
        rel = self.LAB_PY
        self.assertEqual(2, self.find(rel, "unknown-model", "no.such.model")["severity"])
        self.assertEqual([], self.codes_at(rel, "guarded.model"))
        d = self.find(rel, "missing-dependency", "sale.subscription.plan")
        self.assertIn("loading 'diag_lab' fails without it", d["message"])
        self.find(rel, "missing-dependency", "diag.other.model")
        self.assertEqual([], self.codes_at(rel, "diag.bridge.model"))
        self.assertEqual([], self.codes_at(rel, "diag.bridge.model'].search"))

    def test_python_xmlids(self):
        rel = self.LAB_PY
        self.find(rel, "unknown-xmlid", "base.no_such_xmlid")
        for soft in ("base.soft_xmlid", "base.template_only", "base.commented_out", "base.prefix_", "base.main_company"):
            self.assertEqual([], self.codes_at(rel, soft), soft)
        # has_group() tolerates groups of modules that are not installed
        self.assertEqual([], self.codes_at(rel, "sale_subscription.group_subscription_user"))
        self.find(rel, "unknown-xmlid", "sale_subscription.group_whatever")

    def test_python_syntax_error(self):
        d = self.diags("custom/diag_lab/models/broken.py")
        self.assertEqual(["syntax-error"], [x["code"] for x in d])
        self.assertEqual(0, d[0]["range"]["start"]["line"])

    def test_version_checks_18(self):
        rel = self.VERSIONED
        self.assertEqual(2, self.find(rel, "deprecated", "name_get")["severity"])
        self.find(rel, "deprecated", "states=")
        d = self.find(rel, "deprecated", "group_operator")
        self.assertEqual(3, d["severity"])  # still an alias in 18.0
        self.assertIn("'aggregator'", d["message"])
        self.assertEqual(1, self.find(rel, "deprecated", "user_has_groups")["severity"])
        for absent in ("auto_join", "_sql_constraints", "api.returns", "_inherit = ['res.partner']", "'partner_id'}"):
            self.assertEqual([], self.codes_at(rel, absent), absent)

    def test_primary_view(self):
        rel = self.VIEWS
        self.assertIn("'diag.lab'", self.find(rel, "unknown-field", "no_such_lab_field")["message"])
        self.assertIn("'diag.lab.line'", self.find(rel, "unknown-field", "no_such_line_field")["message"])
        self.assertEqual(2, self.find(rel, "unknown-method", "action_nope")["severity"])
        self.assertIn("private", self.find(rel, "private-method", "_private_method")["message"])
        self.assertEqual([], self.codes_at(rel, '"cancel" special'))
        self.assertEqual([], self.codes_at(rel, '"action_lab"'))
        self.assertEqual([], self.codes_at(rel, 'name="partner_id"/>'))

    def test_inheriting_view(self):
        rel = self.VIEWS
        self.find(rel, "unknown-field", "no_such_sale_field")
        self.assertEqual([], self.codes_at(rel, '"price_unit"'))
        self.assertEqual([], self.codes_at(rel, '"product_uom_qty"'))
        # <field> children of position="attributes" are ignored by Odoo
        self.assertEqual([], self.codes_at(rel, '"readonly"'))

    def test_view_deprecations_18(self):
        rel = self.VIEWS
        self.find(rel, "deprecated-attrs", "attrs=")
        self.find(rel, "deprecated-attrs", "states=")
        self.find(rel, "deprecated-tree", "tree>", 0)
        self.find(rel, "deprecated-tree", "tree,form")
        self.find(rel, "deprecated-tree", "tree_view_ref")
        self.find(rel, "deprecated-tree", "//field[@name='order_line']/tree")

    def test_data_file(self):
        rel = self.DATA
        self.assertIn("'diag.lab'", self.find(rel, "unknown-field", "no_such_record_field")["message"])
        self.assertEqual(1, self.find(rel, "unknown-xmlid", "base.no_such_partner")["severity"])
        self.assertEqual(1, self.find(rel, "unknown-model", "no.such.data.model")["severity"])
        self.find(rel, "missing-dependency", "sale.subscription.plan")
        self.find(rel, "unknown-method", "nope_function")
        self.assertEqual([], self.codes_at(rel, '"groups_id"'))  # still groups_id in 18.0
        self.assertEqual([], self.codes_at(rel, '"lab_action"'))

    def test_xml_syntax_errors(self):
        self.assertEqual(["syntax-error"], self.codes("custom/diag_lab/views/broken.xml"))
        self.assertEqual([], self.codes("custom/diag_lab/tests/broken_fixture.xml"))

    def test_access_csv(self):
        rel = self.ACCESS
        self.assertIn("'ir.model.access'", self.find(rel, "unknown-field", "perm_nope")["message"])
        self.find(rel, "unknown-xmlid", "model_no_such_model")
        d = self.find(rel, "unknown-xmlid", "model_diag_labs")
        self.assertIn("did you mean 'diag_lab.model_diag_lab'", d["message"])
        d = self.find(rel, "unknown-xmlid", "model_sale_orders")
        self.assertIn("did you mean 'sale.model_sale_order'", d["message"])
        self.assertEqual([], self.codes_at(rel, "model_diag_lab"))
        self.assertEqual([], self.codes("custom/diag_lab/data/template/account.tax-xx.csv"))

    def test_manifest(self):
        d = self.find(self.MANIFEST, "missing-file", "views/lab_viewz.xml")
        self.assertIn("did you mean 'views/lab_views.xml'", d["message"])
        self.assertEqual(["missing-file"], self.codes(self.MANIFEST))

    def test_odoo_modules_stay_clean(self):
        for path in self.module_files(os.path.join(self.root, "odoo")) + self.module_files(os.path.join(self.root, "enterprise")):
            self.assert_none(os.path.relpath(path, self.root))

    def test_registry_changes_are_seen(self):
        reg = self.env.registry
        path = os.path.join(self.root, self.LAB_PY)
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        self.find(self.LAB_PY, "unknown-method", "_compute_nope")
        anchor = "    def _default_flag(self):\n"
        fixed = original.replace(anchor, "    def _compute_nope(self):\n        pass\n\n" + anchor)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(fixed)
            reg.update_file(path, extract_source(path, "py", fixed.encode()))
            self.assertEqual([], self.codes_at(self.LAB_PY, "_compute_nope"))
        finally:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
            reg.update_file(path, extract_source(path, "py", original.encode()))
        self.find(self.LAB_PY, "unknown-method", "_compute_nope")


class TestSynthetic16(SyntheticCase):
    version = (16, 0)

    def test_no_late_deprecations(self):
        for rel in (self.VERSIONED, self.VIEWS):
            codes = self.codes(rel)
            self.assertNotIn("deprecated", codes, rel)
            self.assertNotIn("deprecated-attrs", codes, rel)
            self.assertNotIn("deprecated-tree", codes, rel)

    def test_checks_still_run(self):
        self.find(self.VIEWS, "unknown-field", "no_such_lab_field")
        self.find(self.LAB_PY, "unknown-method", "_compute_nope")


class TestSynthetic17(SyntheticCase):
    version = (17, 0)

    def test_version_checks_17(self):
        rel = self.VERSIONED
        self.assertIn("deprecated", self.find(rel, "deprecated", "name_get")["message"])
        self.find(rel, "deprecated", "states=")
        for absent in ("group_operator", "user_has_groups", "auto_join", "_sql_constraints", "api.returns"):
            self.assertEqual([], self.codes_at(rel, absent), absent)
        self.find(self.VIEWS, "deprecated-attrs", "attrs=")
        self.assertNotIn("deprecated-tree", self.codes(self.VIEWS))


class TestSynthetic19(SyntheticCase):
    version = (19, 0)

    def test_version_checks_19(self):
        rel = self.VERSIONED
        self.assertIn("models.Constraint", self.find(rel, "deprecated", "_sql_constraints")["message"])
        self.assertIn("bypass_search_access", self.find(rel, "deprecated", "auto_join")["message"])
        self.assertEqual(1, self.find(rel, "deprecated", "@api.returns")["severity"])
        self.assertEqual(3, self.find(rel, "deprecated", "group_operator")["severity"])  # still an alias before saas-19.1
        d = self.find(rel, "deprecated", "_inherit = ['res.partner']")
        self.assertIn("new model 'lab.partner'", d["message"])
        d = self.find(rel, "unknown-field", "partner_id'}")
        self.assertIn("must be declared", d["message"])

    def test_groups_id_renamed(self):
        d = self.find(self.DATA, "unknown-field", 'groups_id"')
        self.assertIn("renamed 'group_ids' in Odoo 19.0", d["message"])


class TestSynthetic194(SyntheticCase):
    version = (19, 4)

    def test_access_csv_replaced(self):
        found = self.diags(self.ACCESS)
        self.assertEqual(["unknown-model"], [d["code"] for d in found])
        self.assertIn("use 'ir.access'", found[0]["message"])
        self.assertEqual(2, self.find(self.VERSIONED, "deprecated", "group_operator")["severity"])  # alias removed in saas-19.1


class RealTreeCase(DiagnosticsCase):
    version = ""
    clean_modules: tuple[str, ...] = ("base", "web", "mail", "account", "sale", "purchase", "stock", "mrp", "hr", "crm", "project", "website", "point_of_sale")
    known_defects: tuple[tuple[str, str], ...] = ()

    @classmethod
    def setUpClass(cls):
        if not cls.version:
            raise unittest.SkipTest("abstract")
        cls.root = os.path.join(WORKTREES, cls.version)
        cls.server = headless_server()
        cls.env, _ = load_env(cls.server, cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.release()

    def test_core_modules_are_clean(self):
        reg = self.env.registry
        checked = 0
        unexpected = []
        for name in self.clean_modules:
            info = reg.module(name)
            self.assertIsNotNone(info, name)
            for path in self.module_files(info.path):
                rel = os.path.relpath(path, self.root)
                for d in self.diags(rel):
                    if d.get("code") == "not-imported":
                        continue  # true for Odoo's own dead files (e.g. account onboarding_onboarding.py)
                    if not any(rel == f and excerpt in d["message"] for f, excerpt in self.known_defects):
                        unexpected.append((rel, d["range"]["start"]["line"] + 1, d["message"]))
                checked += 1
        self.assertEqual([], unexpected)
        self.assertGreater(checked, 1000)

    def test_latency(self):
        reg = self.env.registry
        for rel in ("odoo/addons/sale/models/sale_order.py", "odoo/addons/account/models/account_move.py", "odoo/addons/account/views/account_move_views.xml"):
            path = os.path.join(self.root, rel)
            ctx = RequestContext(self.server, self.env, reg, path)
            ctx.facts  # extraction is cached by the server for open documents
            reg._bump()
            t = time.perf_counter()
            diagnostics(ctx)
            self.assertLess(time.perf_counter() - t, 0.5, rel)


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "16.0")), "no 16.0 checkout")
class TestReal16(RealTreeCase):
    version = "16.0"
    known_defects = (("odoo/addons/account/models/chart_template.py", "Field 'deprecated' does not exist on model 'account.account.template'"),)

    def test_known_defects(self):
        d = self.find("enterprise/helpdesk/controllers/rating.py", "unknown-xmlid", "helpdesk.group_heldpesk_manager")
        self.assertIn("did you mean 'helpdesk.group_helpdesk_manager'", d["message"])
        self.assertEqual(["syntax-error"], self.codes("enterprise/knowledge/static/src/components/html_field/html_field.xml"))

    def test_chained_name_models(self):
        self.assert_none("odoo/addons/base_import/security/ir.model.access.csv")
        self.assert_none("odoo/odoo/addons/test_impex/models.py")


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "18.0")), "no 18.0 checkout")
class TestReal18(RealTreeCase):
    version = "18.0"
    known_defects = (
        ("odoo/addons/stock/models/stock_move.py", "'stock.group_stock_tracking_lot'"),
        ("odoo/addons/stock/models/stock_move.py", "'stock.act_assign_serial_numbers'"),
    )

    def test_known_defects(self):
        d = self.find("odoo/addons/stock/models/stock_move.py", "unknown-xmlid", "stock.act_assign_serial_numbers")
        self.assertEqual(2, d["severity"])
        d = self.find("odoo/addons/l10n_dk_nemhandel/wizard/nemhandel_registration.py", "unknown-method", "_inverse_phone_number")
        self.assertIn("inverse= of field 'phone_number'", d["message"])

    def test_bridges_and_guards(self):
        self.assert_none("enterprise/l10n_be_hr_payroll/models/hr_contract.py")
        # wizard depends through regular models are not resolved by Odoo
        self.assert_none("odoo/addons/account/wizard/account_payment_register.py")


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "saas-19.4")), "no saas-19.4 checkout")
class TestReal194(RealTreeCase):
    version = "saas-19.4"
    known_defects = (("odoo/addons/stock/models/stock_move.py", "'stock.group_stock_tracking_lot'"),)

    def test_known_defects(self):
        self.find("enterprise/l10n_be_hr_payroll/views/l10n_be_274_XX_views.xml", "deprecated-tree", "tree_view_ref")
        self.assert_none("odoo/addons/sale/security/ir.access.csv")


@unittest.skipUnless(os.path.isdir(os.path.join(WORKTREES, "20.0")), "no 20.0 checkout")
class TestReal20(RealTreeCase):
    version = "20.0"
    known_defects = (("odoo/addons/stock/models/stock_move.py", "'stock.group_stock_tracking_lot'"),)

    def test_known_defects(self):
        d = self.find("odoo/addons/stock/models/stock_move.py", "unknown-xmlid", "stock.group_stock_tracking_lot")
        self.assertIn("did you mean 'stock.group_tracking_lot'", d["message"])
        d = self.find("enterprise/l10n_iq_hr_payroll/data/l10n_iq_hr_payroll_demo.xml", "unknown-method", "_set_input_value")
        self.assertIn("'hr.version'", d["message"])
        # stale demo file (manifest 'demo' entry commented out): never loaded by Odoo
        self.assertEqual(self.diags("enterprise/l10n_be_hr_contract_salary/data/hr_contract_salary_benefit_demo.xml"), [])

    def test_precision_patterns(self):
        self.assert_none("enterprise/documents_spreadsheet/views/spreadsheet_template_views.xml")
        self.assert_none("enterprise/account_asset/models/account_asset_ledger_variant.py")
        self.assert_none("odoo/odoo/addons/base/views/ir_rule_views.xml")
        self.assert_none("enterprise/marketing_automation_crm/models/marketing_campaign.py")
        self.assert_none("enterprise/hr_appraisal/models/hr_appraisal.py")


if __name__ == "__main__":
    unittest.main()
