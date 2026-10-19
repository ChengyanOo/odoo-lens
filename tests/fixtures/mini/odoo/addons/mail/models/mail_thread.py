from odoo import fields, models


class MailThread(models.AbstractModel):
    _name = 'mail.thread'
    _description = 'Email Thread'

    message_ids = fields.One2many('res.partner', 'id', string='Messages')

    def message_post(self, body='', **kwargs):
        """Post a message on the record."""
        return self.env['res.partner']
