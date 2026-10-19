from odoo import models


class Base(models.AbstractModel):
    _name = 'base'
    _description = 'Base'

    def _base_helper(self):
        return self.ids
