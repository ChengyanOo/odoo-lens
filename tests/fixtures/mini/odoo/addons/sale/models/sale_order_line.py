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
