from odoo import fields, models


class Company(models.Model):
    _name = 'res.company'
    _description = 'Companies'

    name = fields.Char(related='partner_id.name', string='Company Name', required=True, store=True)
    partner_id = fields.Many2one('res.partner', string='Partner', required=True)
    currency_id = fields.Many2one('res.currency', string='Currency')
