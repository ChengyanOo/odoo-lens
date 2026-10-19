from odoo import api, fields, models


class Groups(models.Model):
    _name = 'res.groups'
    _description = 'Access Groups'

    name = fields.Char(required=True, translate=True)
    users = fields.Many2many('res.users', 'res_groups_users_rel', 'gid', 'uid')


class Users(models.Model):
    _name = 'res.users'
    _description = 'User'
    _inherits = {'res.partner': 'partner_id'}

    partner_id = fields.Many2one('res.partner', required=True, ondelete='restrict')
    login = fields.Char(required=True)
    groups_id = fields.Many2many('res.groups', 'res_groups_users_rel', 'uid', 'gid', string='Groups')

    def has_group(self, group_ext_id):
        return group_ext_id in self.groups_id.mapped('name')
