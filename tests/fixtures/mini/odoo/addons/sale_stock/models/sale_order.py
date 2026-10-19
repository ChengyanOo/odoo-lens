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
