from . import api, fields


class MetaModel(type):
    pass


class BaseModel(metaclass=MetaModel):
    """Base class for Odoo models."""

    _auto = False
    _register = False
    _abstract = True
    _transient = False
    _name = None
    _description = None
    _inherit = ()
    _inherits = {}
    _rec_name = None
    _order = 'id'

    id = fields.Id(automatic=True)
    display_name = fields.Char(string='Display Name', compute='_compute_display_name')
    create_uid = fields.Many2one('res.users', string='Created by', automatic=True, readonly=True)
    create_date = fields.Datetime(string='Created on', automatic=True, readonly=True)
    write_uid = fields.Many2one('res.users', string='Last Updated by', automatic=True, readonly=True)
    write_date = fields.Datetime(string='Last Updated on', automatic=True, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        """Create new records for the model."""
        return self.browse()

    def write(self, vals):
        return True

    def unlink(self):
        return True

    def browse(self, ids=None):
        return self

    def search(self, domain, offset=0, limit=None, order=None):
        return self

    def search_count(self, domain, limit=None):
        return 0

    def exists(self):
        return self

    def ensure_one(self):
        return self

    def sudo(self, flag=True):
        return self

    def with_context(self, *args, **kwargs):
        return self

    def with_company(self, company):
        return self

    def filtered(self, func):
        return self

    def mapped(self, func):
        return self

    def sorted(self, key=None, reverse=False):
        return self

    def read(self, fields=None, load='_classic_read'):
        return []

    def copy(self, default=None):
        return self

    def _compute_display_name(self):
        for record in self:
            record.display_name = record.id


class AbstractModel(BaseModel):
    _auto = False
    _abstract = True


class Model(AbstractModel):
    _auto = True
    _register = False
    _abstract = False
    _transient = False


class TransientModel(Model):
    _transient = True

    def _transient_vacuum(self):
        pass
