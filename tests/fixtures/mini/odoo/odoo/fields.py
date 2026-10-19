class Field:
    type = None

    def __init__(self, string=None, **kwargs):
        self.string = string


class Id(Field):
    type = 'integer'


class Boolean(Field):
    type = 'boolean'


class Integer(Field):
    type = 'integer'


class Float(Field):
    type = 'float'


class Monetary(Field):
    type = 'monetary'


class Char(Field):
    type = 'char'


class Text(Field):
    type = 'text'


class Html(Field):
    type = 'html'


class Date(Field):
    type = 'date'


class Datetime(Field):
    type = 'datetime'


class Selection(Field):
    type = 'selection'

    def __init__(self, selection=None, string=None, **kwargs):
        super().__init__(string=string, **kwargs)


class _Relational(Field):
    pass


class Many2one(_Relational):
    type = 'many2one'

    def __init__(self, comodel_name=None, string=None, **kwargs):
        super().__init__(string=string, **kwargs)


class One2many(_Relational):
    type = 'one2many'

    def __init__(self, comodel_name=None, inverse_name=None, string=None, **kwargs):
        super().__init__(string=string, **kwargs)


class Many2many(_Relational):
    type = 'many2many'

    def __init__(self, comodel_name=None, relation=None, column1=None, column2=None, string=None, **kwargs):
        super().__init__(string=string, **kwargs)


class Command:
    @classmethod
    def link(cls, id):
        return (4, id, 0)
