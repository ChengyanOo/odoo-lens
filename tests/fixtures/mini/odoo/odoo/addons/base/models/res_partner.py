from odoo import api, fields, models


class Partner(models.Model):
    _name = 'res.partner'
    _description = 'Contact'
    _order = 'complete_name ASC, id DESC'
    _rec_name = 'complete_name'

    name = fields.Char(index=True)
    complete_name = fields.Char(compute='_compute_complete_name', store=True, index=True)
    parent_id = fields.Many2one('res.partner', string='Related Company', index=True)
    child_ids = fields.One2many('res.partner', 'parent_id', string='Contact')
    country_id = fields.Many2one('res.country', string='Country', ondelete='restrict')
    company_id = fields.Many2one('res.company', 'Company', index=True)
    email = fields.Char()
    is_company = fields.Boolean(string='Is a Company', default=False)
    user_ids = fields.One2many('res.users', 'partner_id', string='Users')

    @api.depends('name', 'parent_id.complete_name')
    def _compute_complete_name(self):
        for partner in self:
            partner.complete_name = partner.name

    def _get_country_code(self):
        self.ensure_one()
        return self.country_id.code
