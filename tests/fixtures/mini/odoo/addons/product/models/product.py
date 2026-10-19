from odoo import fields, models


class ProductProduct(models.Model):
    _name = 'product.product'
    _inherit = ['mail.thread']
    _description = 'Product'

    name = fields.Char(required=True)
    list_price = fields.Float('Sales Price', digits=(16, 2))
    active = fields.Boolean(default=True)
