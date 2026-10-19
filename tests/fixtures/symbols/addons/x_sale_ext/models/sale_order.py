import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)
X_STATES = [('a', 'A'), ('b', 'B')]


def x_helper(value, digits=2):
    return round(value, digits)


class SaleOrder(models.Model):
    _inherit = 'sale.order'
    _order = 'name desc'

    x_reference = fields.Char(string='X Reference', required=True)
    partner_id = fields.Many2one(tracking=True)
    x_note_ids = fields.One2many('x.sale.note', 'order_id', string='Notes')
    x_total = fields.Float(compute='_compute_x_total', store=True)

    _sql_constraints = [('x_reference_uniq', 'unique(x_reference)', 'Reference must be unique')]

    @api.depends('order_line.price_subtotal')
    def _compute_x_total(self):
        for order in self:
            order.x_total = 0.0

    def action_confirm(self):
        return True

    @api.model_create_multi
    def create(self, vals_list):
        return super().create(vals_list)


class XSaleNote(models.Model):
    _name = 'x.sale.note'
    _inherit = ['mail.thread']
    _description = 'Sale Note'

    order_id = fields.Many2one('sale.order', required=True)
    body = fields.Text()


class XSaleNoteWizard(models.TransientModel):
    _name = 'x.sale.note.wizard'
    _description = 'Note Wizard'

    note = fields.Text()


class NotAModel:
    """Helper."""

    def run(self):
        return 1
