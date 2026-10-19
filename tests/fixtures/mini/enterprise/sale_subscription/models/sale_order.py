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
