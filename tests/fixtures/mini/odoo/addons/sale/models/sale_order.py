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
        """Confirm the quotation into a sales order."""
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
