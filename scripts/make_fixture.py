# Tests reference line numbers in tests/fixtures/mini: append new content at file END only.

import os
import shutil
import textwrap

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "mini")

FILES = {
    "odoo/odoo-bin": "#!/usr/bin/env python3\nimport odoo\nodoo.cli.main()\n",
    "odoo/odoo/__init__.py": """
        from . import api
        from . import fields
        from . import models
        from . import tools
        from .tools.translate import _
        """,
    "odoo/odoo/release.py": """
        RELEASE_LEVELS = [ALPHA, BETA, RELEASE_CANDIDATE, FINAL] = ['alpha', 'beta', 'candidate', 'final']
        version_info = (18, 0, 0, FINAL, 0, '')
        version = '18.0'
        """,
    "odoo/odoo/api.py": """
        def depends(*args):
            return lambda f: f

        def depends_context(*args):
            return lambda f: f

        def constrains(*args):
            return lambda f: f

        def onchange(*args):
            return lambda f: f

        def ondelete(*, at_uninstall):
            return lambda f: f

        def model(method):
            return method

        def model_create_multi(method):
            return method


        class Environment:
            def __getitem__(self, model_name):
                return self.registry[model_name]

            def ref(self, xml_id, raise_if_not_found=True):
                pass

            @property
            def user(self):
                return self(su=True)['res.users'].browse(self.uid)

            @property
            def company(self):
                pass
        """,
    "odoo/odoo/fields.py": """
        class Field:
            type = None

            def __init__(self, string=None, **kwargs):
                self.string = string


        class Id(Field):
            type = 'integer'


        class Boolean(Field):
            type = 'boolean'


        class Integer(Field):
            type = 'integer'


        class Float(Field):
            type = 'float'


        class Monetary(Field):
            type = 'monetary'


        class Char(Field):
            type = 'char'


        class Text(Field):
            type = 'text'


        class Html(Field):
            type = 'html'


        class Date(Field):
            type = 'date'


        class Datetime(Field):
            type = 'datetime'


        class Selection(Field):
            type = 'selection'

            def __init__(self, selection=None, string=None, **kwargs):
                super().__init__(string=string, **kwargs)


        class _Relational(Field):
            pass


        class Many2one(_Relational):
            type = 'many2one'

            def __init__(self, comodel_name=None, string=None, **kwargs):
                super().__init__(string=string, **kwargs)


        class One2many(_Relational):
            type = 'one2many'

            def __init__(self, comodel_name=None, inverse_name=None, string=None, **kwargs):
                super().__init__(string=string, **kwargs)


        class Many2many(_Relational):
            type = 'many2many'

            def __init__(self, comodel_name=None, relation=None, column1=None, column2=None, string=None, **kwargs):
                super().__init__(string=string, **kwargs)


        class Command:
            @classmethod
            def link(cls, id):
                return (4, id, 0)
        """,
    "odoo/odoo/models.py": """
        from . import api, fields


        class MetaModel(type):
            pass


        class BaseModel(metaclass=MetaModel):
            \"\"\"Base class for Odoo models.\"\"\"

            _auto = False
            _register = False
            _abstract = True
            _transient = False
            _name = None
            _description = None
            _inherit = ()
            _inherits = {}
            _rec_name = None
            _order = 'id'

            id = fields.Id(automatic=True)
            display_name = fields.Char(string='Display Name', compute='_compute_display_name')
            create_uid = fields.Many2one('res.users', string='Created by', automatic=True, readonly=True)
            create_date = fields.Datetime(string='Created on', automatic=True, readonly=True)
            write_uid = fields.Many2one('res.users', string='Last Updated by', automatic=True, readonly=True)
            write_date = fields.Datetime(string='Last Updated on', automatic=True, readonly=True)

            @api.model_create_multi
            def create(self, vals_list):
                \"\"\"Create new records for the model.\"\"\"
                return self.browse()

            def write(self, vals):
                return True

            def unlink(self):
                return True

            def browse(self, ids=None):
                return self

            def search(self, domain, offset=0, limit=None, order=None):
                return self

            def search_count(self, domain, limit=None):
                return 0

            def exists(self):
                return self

            def ensure_one(self):
                return self

            def sudo(self, flag=True):
                return self

            def with_context(self, *args, **kwargs):
                return self

            def with_company(self, company):
                return self

            def filtered(self, func):
                return self

            def mapped(self, func):
                return self

            def sorted(self, key=None, reverse=False):
                return self

            def read(self, fields=None, load='_classic_read'):
                return []

            def copy(self, default=None):
                return self

            def _compute_display_name(self):
                for record in self:
                    record.display_name = record.id


        class AbstractModel(BaseModel):
            _auto = False
            _abstract = True


        class Model(AbstractModel):
            _auto = True
            _register = False
            _abstract = False
            _transient = False


        class TransientModel(Model):
            _transient = True

            def _transient_vacuum(self):
                pass
        """,
    "odoo/odoo/tools/__init__.py": """
        from .float_utils import float_compare, float_round
        from . import translate
        """,
    "odoo/odoo/tools/float_utils.py": """
        def float_round(value, precision_digits=None):
            return round(value, precision_digits or 2)


        def float_compare(value1, value2, precision_digits=None):
            \"\"\"Compare two floats at the given precision.\"\"\"
            delta = float_round(value1 - value2, precision_digits)
            return (delta > 0) - (delta < 0)
        """,
    "odoo/odoo/tools/translate.py": """
        def _(source, *args):
            return source
        """,
    "odoo/odoo/http.py": """
        class Controller:
            pass


        def route(route=None, **kw):
            return lambda f: f


        request = None
        """,
    "odoo/odoo/addons/__init__.py": "",
    "odoo/odoo/addons/base/__init__.py": "from . import models\n",
    "odoo/odoo/addons/base/__manifest__.py": """
        {
            'name': 'Base',
            'version': '1.3',
            'category': 'Hidden',
            'depends': [],
            'data': [
                'security/base_groups.xml',
                'security/ir.model.access.csv',
                'data/res_company_data.xml',
                'views/res_partner_views.xml',
            ],
            'installable': True,
            'auto_install': True,
            'license': 'LGPL-3',
        }
        """,
    "odoo/odoo/addons/base/models/__init__.py": """
        from . import base
        from . import res_partner
        from . import res_company
        from . import res_country
        from . import res_users
        from . import ir_ui_view
        """,
    "odoo/odoo/addons/base/models/base.py": """
        from odoo import models


        class Base(models.AbstractModel):
            _name = 'base'
            _description = 'Base'

            def _base_helper(self):
                return self.ids
        """,
    "odoo/odoo/addons/base/models/res_partner.py": """
        from odoo import api, fields, models


        class Partner(models.Model):
            _name = 'res.partner'
            _description = 'Contact'
            _order = 'complete_name ASC, id DESC'
            _rec_name = 'complete_name'

            name = fields.Char(index=True)
            complete_name = fields.Char(compute='_compute_complete_name', store=True, index=True)
            parent_id = fields.Many2one('res.partner', string='Related Company', index=True)
            child_ids = fields.One2many('res.partner', 'parent_id', string='Contact')
            country_id = fields.Many2one('res.country', string='Country', ondelete='restrict')
            company_id = fields.Many2one('res.company', 'Company', index=True)
            email = fields.Char()
            is_company = fields.Boolean(string='Is a Company', default=False)
            user_ids = fields.One2many('res.users', 'partner_id', string='Users')

            @api.depends('name', 'parent_id.complete_name')
            def _compute_complete_name(self):
                for partner in self:
                    partner.complete_name = partner.name

            def _get_country_code(self):
                self.ensure_one()
                return self.country_id.code
        """,
    "odoo/odoo/addons/base/models/res_company.py": """
        from odoo import fields, models


        class Company(models.Model):
            _name = 'res.company'
            _description = 'Companies'

            name = fields.Char(related='partner_id.name', string='Company Name', required=True, store=True)
            partner_id = fields.Many2one('res.partner', string='Partner', required=True)
            currency_id = fields.Many2one('res.currency', string='Currency')
        """,
    "odoo/odoo/addons/base/models/res_country.py": """
        from odoo import fields, models


        class Country(models.Model):
            _name = 'res.country'
            _description = 'Country'

            name = fields.Char(string='Country Name', required=True, translate=True)
            code = fields.Char(string='Country Code', size=2)


        class Currency(models.Model):
            _name = 'res.currency'
            _description = 'Currency'

            name = fields.Char(string='Currency', size=3, required=True)
        """,
    "odoo/odoo/addons/base/models/res_users.py": """
        from odoo import api, fields, models


        class Groups(models.Model):
            _name = 'res.groups'
            _description = 'Access Groups'

            name = fields.Char(required=True, translate=True)
            users = fields.Many2many('res.users', 'res_groups_users_rel', 'gid', 'uid')


        class Users(models.Model):
            _name = 'res.users'
            _description = 'User'
            _inherits = {'res.partner': 'partner_id'}

            partner_id = fields.Many2one('res.partner', required=True, ondelete='restrict')
            login = fields.Char(required=True)
            groups_id = fields.Many2many('res.groups', 'res_groups_users_rel', 'uid', 'gid', string='Groups')

            def has_group(self, group_ext_id):
                return group_ext_id in self.groups_id.mapped('name')
        """,
    "odoo/odoo/addons/base/models/ir_ui_view.py": """
        from odoo import fields, models


        class View(models.Model):
            _name = 'ir.ui.view'
            _description = 'View'

            name = fields.Char(string='View Name', required=True)
            model = fields.Char(index=True)
            arch = fields.Text()
            inherit_id = fields.Many2one('ir.ui.view', string='Inherited View')
        """,
    "odoo/odoo/addons/base/security/base_groups.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="group_user" model="res.groups">
                <field name="name">Internal User</field>
            </record>
            <record id="group_system" model="res.groups">
                <field name="name">Settings</field>
            </record>
        </odoo>
        """,
    "odoo/odoo/addons/base/security/ir.model.access.csv": """
        id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
        access_res_partner_user,res.partner user,model_res_partner,group_user,1,1,1,0
        access_res_company_user,res.company user,model_res_company,base.group_user,1,0,0,0
        """,
    "odoo/odoo/addons/base/data/res_company_data.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <data noupdate="1">
                <record id="main_partner" model="res.partner">
                    <field name="name">My Company</field>
                    <field name="is_company" eval="True"/>
                </record>
                <record id="main_company" model="res.company">
                    <field name="name">My Company</field>
                    <field name="partner_id" ref="main_partner"/>
                </record>
            </data>
        </odoo>
        """,
    "odoo/odoo/addons/base/views/res_partner_views.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="view_partner_form" model="ir.ui.view">
                <field name="name">res.partner.form</field>
                <field name="model">res.partner</field>
                <field name="arch" type="xml">
                    <form string="Partners">
                        <field name="name"/>
                        <field name="parent_id" invisible="is_company"/>
                        <field name="child_ids">
                            <list>
                                <field name="name"/>
                                <field name="email"/>
                            </list>
                        </field>
                        <field name="country_id" groups="base.group_user"/>
                    </form>
                </field>
            </record>
            <record id="action_partner_form" model="ir.actions.act_window">
                <field name="name">Customers</field>
                <field name="res_model">res.partner</field>
                <field name="view_id" ref="view_partner_form"/>
            </record>
            <menuitem id="menu_contacts" name="Contacts" action="action_partner_form" sequence="4"/>
        </odoo>
        """,
    "odoo/addons/mail/__init__.py": "from . import models\n",
    "odoo/addons/mail/__manifest__.py": """
        {
            'name': 'Discuss',
            'version': '1.15',
            'depends': ['base'],
            'data': [],
            'license': 'LGPL-3',
        }
        """,
    "odoo/addons/mail/models/__init__.py": "from . import mail_thread\n",
    "odoo/addons/mail/models/mail_thread.py": """
        from odoo import fields, models


        class MailThread(models.AbstractModel):
            _name = 'mail.thread'
            _description = 'Email Thread'

            message_ids = fields.One2many('res.partner', 'id', string='Messages')

            def message_post(self, body='', **kwargs):
                \"\"\"Post a message on the record.\"\"\"
                return self.env['res.partner']
        """,
    "odoo/addons/product/__init__.py": "from . import models\n",
    "odoo/addons/product/__manifest__.py": """
        {
            'name': 'Products',
            'version': '1.2',
            'depends': ['base', 'mail'],
            'data': [],
            'license': 'LGPL-3',
        }
        """,
    "odoo/addons/product/models/__init__.py": "from . import product\n",
    "odoo/addons/product/models/product.py": """
        from odoo import fields, models


        class ProductProduct(models.Model):
            _name = 'product.product'
            _inherit = ['mail.thread']
            _description = 'Product'

            name = fields.Char(required=True)
            list_price = fields.Float('Sales Price', digits=(16, 2))
            active = fields.Boolean(default=True)
        """,
    "odoo/addons/sale/__init__.py": "from . import models\n",
    "odoo/addons/sale/__manifest__.py": """
        {
            'name': 'Sales',
            'version': '1.2',
            'depends': ['product', 'mail'],
            'data': [
                'security/ir.model.access.csv',
                'views/sale_order_views.xml',
            ],
            'license': 'LGPL-3',
        }
        """,
    "odoo/addons/sale/models/__init__.py": """
        from . import sale_order
        from . import sale_order_line
        """,
    "odoo/addons/sale/models/sale_order.py": """
        from odoo import api, fields, models
        from odoo.tools import float_compare


        class SaleOrder(models.Model):
            _name = 'sale.order'
            _inherit = ['mail.thread']
            _description = 'Sales Order'
            _order = 'date_order desc, id desc'

            name = fields.Char(string='Order Reference', required=True, copy=False, default='New')
            partner_id = fields.Many2one('res.partner', string='Customer', required=True, index=True)
            date_order = fields.Datetime(string='Order Date', required=True)
            state = fields.Selection([('draft', 'Quotation'), ('sale', 'Sales Order'), ('cancel', 'Cancelled')], string='Status', default='draft')
            order_line = fields.One2many('sale.order.line', 'order_id', string='Order Lines', copy=True)
            amount_total = fields.Monetary(string='Total', compute='_compute_amounts', store=True)
            country_code = fields.Char(related='partner_id.country_id.code')
            company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)

            @api.depends('order_line.price_subtotal')
            def _compute_amounts(self):
                for order in self:
                    order.amount_total = sum(order.order_line.mapped('price_subtotal'))

            def action_confirm(self):
                \"\"\"Confirm the quotation into a sales order.\"\"\"
                for order in self:
                    if float_compare(order.amount_total, 0.0, precision_digits=2) < 0:
                        continue
                    order.partner_id.message_post(body='confirmed')
                self.write({'state': 'sale'})
                return True

            @api.model_create_multi
            def create(self, vals_list):
                orders = super().create(vals_list)
                for order in orders:
                    order.name = self.env['ir.sequence'].next_by_code('sale.order')
                return orders

            def _get_partner_country(self):
                lines = self.order_line.filtered(lambda l: l.product_id.active)
                partner = self.env['res.partner'].browse(self.partner_id.id)
                return partner.country_id, lines
        """,
    "odoo/addons/sale/models/sale_order_line.py": """
        from odoo import api, fields, models


        class SaleOrderLine(models.Model):
            _name = 'sale.order.line'
            _description = 'Sales Order Line'

            order_id = fields.Many2one('sale.order', string='Order Reference', required=True, ondelete='cascade')
            product_id = fields.Many2one('product.product', string='Product')
            price_unit = fields.Float('Unit Price')
            product_uom_qty = fields.Float('Quantity', default=1.0)
            price_subtotal = fields.Monetary(compute='_compute_amount', store=True)

            @api.depends('product_uom_qty', 'price_unit')
            def _compute_amount(self):
                for line in self:
                    line.price_subtotal = line.product_uom_qty * line.price_unit
        """,
    "odoo/addons/sale/security/ir.model.access.csv": """
        id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
        access_sale_order,sale.order,model_sale_order,base.group_user,1,1,1,1
        access_sale_order_line,sale.order.line,model_sale_order_line,base.group_user,1,1,1,1
        """,
    "odoo/addons/sale/views/sale_order_views.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="view_order_form" model="ir.ui.view">
                <field name="name">sale.order.form</field>
                <field name="model">sale.order</field>
                <field name="arch" type="xml">
                    <form string="Sales Order">
                        <header>
                            <button name="action_confirm" type="object" string="Confirm" invisible="state != 'draft'"/>
                        </header>
                        <field name="partner_id"/>
                        <field name="date_order"/>
                        <field name="order_line">
                            <list editable="bottom">
                                <field name="product_id"/>
                                <field name="price_subtotal"/>
                            </list>
                        </field>
                        <field name="amount_total"/>
                    </form>
                </field>
            </record>
            <record id="action_orders" model="ir.actions.act_window">
                <field name="name">Sales Orders</field>
                <field name="res_model">sale.order</field>
                <field name="context">{'default_company_id': ref('base.main_company')}</field>
            </record>
            <menuitem id="sale_menu_root" name="Sales" groups="base.group_user"/>
            <menuitem id="menu_sale_order" name="Orders" parent="sale_menu_root" action="action_orders"/>
        </odoo>
        """,
    "odoo/addons/sale_stock/__init__.py": "from . import models\n",
    "odoo/addons/sale_stock/__manifest__.py": """
        {
            'name': 'Sales and Warehouse Management',
            'version': '1.0',
            'depends': ['sale'],
            'data': ['views/sale_order_views.xml'],
            'auto_install': True,
            'license': 'LGPL-3',
        }
        """,
    "odoo/addons/sale_stock/models/__init__.py": "from . import sale_order\n",
    "odoo/addons/sale_stock/models/sale_order.py": """
        from odoo import fields, models


        class SaleOrder(models.Model):
            _inherit = 'sale.order'

            picking_policy = fields.Selection([('direct', 'As soon as possible'), ('one', 'When all products are ready')], default='direct')
            partner_id = fields.Many2one(tracking=True)

            def action_confirm(self):
                result = super().action_confirm()
                for order in self:
                    order._create_pickings()
                return result

            def _create_pickings(self):
                return self.order_line.mapped('product_id')
        """,
    "odoo/addons/sale_stock/views/sale_order_views.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="view_order_form_inherit_sale_stock" model="ir.ui.view">
                <field name="name">sale.order.form.sale.stock</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="sale.view_order_form"/>
                <field name="arch" type="xml">
                    <field name="date_order" position="after">
                        <field name="picking_policy"/>
                    </field>
                    <xpath expr="//field[@name='order_line']/list/field[@name='product_id']" position="after">
                        <field name="product_uom_qty"/>
                    </xpath>
                </field>
            </record>
        </odoo>
        """,
    "enterprise/sale_subscription/__init__.py": "from . import models\n",
    "enterprise/sale_subscription/__manifest__.py": """
        {
            'name': 'Subscriptions',
            'version': '1.1',
            'depends': ['sale'],
            'data': [],
            'license': 'OEEL-1',
        }
        """,
    "enterprise/sale_subscription/models/__init__.py": "from . import sale_order\n",
    "enterprise/sale_subscription/models/sale_order.py": """
        from odoo import fields, models


        class SaleSubscriptionPlan(models.Model):
            _name = 'sale.subscription.plan'
            _description = 'Subscription Plan'

            name = fields.Char(required=True)


        class SaleOrder(models.Model):
            _inherit = 'sale.order'

            is_subscription = fields.Boolean()
            plan_id = fields.Many2one('sale.subscription.plan')

            def action_confirm(self):
                return super().action_confirm()
        """,
    "custom/my_module/__init__.py": "from . import models\n",
    "custom/my_module/__manifest__.py": """
        {
            'name': 'My Module',
            'version': '18.0.1.0.0',
            'depends': ['sale', 'unknown_module'],
            'data': [
                'views/sale_order_views.xml',
                'views/missing_file.xml',
            ],
            'license': 'LGPL-3',
        }
        """,
    "custom/my_module/models/__init__.py": "from . import sale_order\n",
    "custom/my_module/models/sale_order.py": """
        from odoo import api, fields, models


        class SaleOrder(models.Model):
            _inherit = 'sale.order'

            x_note = fields.Text('Internal Note')
            x_country_code = fields.Char(related='partner_id.country_id.code')
            x_plan_id = fields.Many2one('sale.subscription.plan')
            x_bad = fields.Many2one('res.partnr')

            @api.depends('partner_id.namee')
            def _compute_x(self):
                for order in self:
                    order.x_note = order.partner_id.name

            def action_confirm(self):
                company = self.env.ref('base.main_company')
                partners = self.env['res.partner'].search([('is_company', '=', True)])
                for partner in partners:
                    partner.email = company.partner_id.email
                self.env.ref('base.no_such_record')
                return super().action_confirm()
        """,
    "custom/my_module/views/sale_order_views.xml": """
        <?xml version="1.0" encoding="utf-8"?>
        <odoo>
            <record id="view_order_form_inherit_my_module" model="ir.ui.view">
                <field name="name">sale.order.form.my.module</field>
                <field name="model">sale.order</field>
                <field name="inherit_id" ref="sale.view_order_form"/>
                <field name="arch" type="xml">
                    <field name="partner_id" position="after">
                        <field name="x_note"/>
                        <field name="no_such_field"/>
                    </field>
                </field>
            </record>
        </odoo>
        """,
}


def main() -> None:
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    for rel, content in FILES.items():
        path = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = textwrap.dedent(content).lstrip("\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    os.chmod(os.path.join(ROOT, "odoo/odoo-bin"), 0o755)
    print(f"wrote {len(FILES)} files under {ROOT}")


if __name__ == "__main__":
    main()
