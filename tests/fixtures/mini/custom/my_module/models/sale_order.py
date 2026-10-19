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
