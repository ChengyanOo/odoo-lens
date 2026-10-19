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
