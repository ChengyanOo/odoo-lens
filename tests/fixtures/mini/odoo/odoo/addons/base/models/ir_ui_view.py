from odoo import fields, models


class View(models.Model):
    _name = 'ir.ui.view'
    _description = 'View'

    name = fields.Char(string='View Name', required=True)
    model = fields.Char(index=True)
    arch = fields.Text()
    inherit_id = fields.Many2one('ir.ui.view', string='Inherited View')
