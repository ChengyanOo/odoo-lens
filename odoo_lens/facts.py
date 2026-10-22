"""The *facts* contract: what extractors produce for one file and the cache stores.

Facts are built only from builtin types (dict / list / tuple / str / int / float /
bool / None) so that they can be serialized with :mod:`marshal` (5x faster to
load than pickled NamedTuples) and shipped between processes cheaply. Tuples are
accessed through the index constants defined below - never by magic numbers.

Conventions shared by every tuple:

* ``line`` / ``end_line`` are 0-based;
* ``col`` / ``end_col`` are **UTF-16 code units** (LSP default) on ``line``;
* a "token" position ``(line, col, end_col)`` always lies on a single line and,
  for string literals, covers the string *content* (without the quotes) so that
  ``'sale.order'`` -> the range of ``sale.order``.

Every extractor is a pure function ``extract(path: str, source: bytes) -> dict``
that never raises: on a syntax error it returns whatever it could extract and
sets ``ERROR``.
"""

from __future__ import annotations

# Bump whenever the layout of any tuple below changes (invalidates caches).
FACTS_VERSION = 12

# ---------------------------------------------------------------------------
# Common keys of a file-facts dict

KIND = "kind"  # one of the KIND_* values
ERROR = "error"  # None, or (message, line, col) of the first syntax/parse error

KIND_PY = "py"
KIND_XML = "xml"
KIND_CSV = "csv"
KIND_MANIFEST = "manifest"
KIND_JS = "js"

# ---------------------------------------------------------------------------
# Python facts:  {"kind": "py", "error": ..., "imports": [...], "symbols": [...],
#                 "classes": [...], "refs": [...]}

IMPORTS = "imports"
SYMBOLS = "symbols"
CLASSES = "classes"
REFS = "refs"

# Import: one tuple per imported name (``import a, b`` gives two tuples).
#   import a.b.c        -> module="a.b.c", name=None, asname=None, level=0
#   import a.b as c     -> module="a.b",   name=None, asname="c",  level=0
#   from .x import y    -> module="x",     name="y",  asname=None, level=1
#   from . import y     -> module="",      name="y",  asname=None, level=1
#   from x import *     -> module="x",     name="*",  asname=None, level=0
# The position is the one of the imported name token (the alias), i.e. ``y`` in
# ``from x import y``, ``a.b.c`` in ``import a.b.c``.
I_MODULE = 0
I_NAME = 1
I_ASNAME = 2
I_LEVEL = 3
I_LINE = 4
I_COL = 5
I_END_COL = 6

# Symbol: module-level definitions (generic Python navigation & workspace symbols).
#   kind: "class" | "def" | "var"  (var = module-level assignment target name)
S_NAME = 0
S_KIND = 1
S_LINE = 2
S_COL = 3
S_END_COL = 4
S_END_LINE = 5

# Class: every top-level class (Odoo model or not).
K_NAME = 0  # python class name
K_LINE = 1  # line of the class name token
K_COL = 2
K_END_COL = 3
K_END_LINE = 4  # last line of the class body
K_BASES = 5  # tuple of base expressions as dotted text, e.g. ("models.Model",)
K_MODEL = 6  # None for non-model classes, else a model-info dict (MI_* keys below)
K_FIELDS = 7  # list of Field tuples
K_METHODS = 8  # list of Method tuples
K_ATTRS = 9  # list of ClassAttr tuples (class-level assignments that are not fields)
K_DOC = 10  # first line of the class docstring or None

# Model-info dict keys (K_MODEL). Values are literals as written in the source.
MI_NAME = "name"  # value of _name or None
MI_INHERIT = "inherit"  # list of _inherit model names ([] if none)
MI_INHERITS = "inherits"  # dict {parent_model: m2o_field_name} ({} if none)
MI_KIND = "kind"  # "Model" | "AbstractModel" | "TransientModel" | "BaseModel" | None (unknown base)
MI_ATTRS = "attrs"  # dict of other literal model attributes, e.g. {"_description": "...", "_order": "..."}
MI_INHERIT_STR = "inherit_str"  # True when _inherit was written as a plain string (implies _name in all versions)

# Field: ``name = fields.Type(...)`` in a class body.
F_NAME = 0
F_LINE = 1  # line of the field-name token
F_COL = 2
F_END_COL = 3
F_END_LINE = 4  # last line of the assignment statement (for source snippets)
F_TYPE = 5  # field class name, e.g. "Many2one", "Char" (the last dotted segment)
F_COMODEL = 6  # comodel name for relational fields (positional or comodel_name=), else None
F_ARGS = 7  # dict of normalized keyword arguments (see "Field arguments" below)

# Field arguments (F_ARGS): positional arguments are normalized to their keyword
# name (e.g. Many2one('res.partner', 'Customer') -> comodel_name, string).
# Literal values are stored as-is (str, int, float, bool, None, lists/tuples of
# literals); any other expression is stored as the tuple ("=", source_text) with
# source_text truncated to 120 chars. Example:
#   {"comodel_name": "res.partner", "string": "Customer", "required": True,
#    "default": ("=", "lambda self: self.env.company")}

# Method: ``def name(...)`` in a class body.
M_NAME = 0
M_LINE = 1  # line of the method-name token
M_COL = 2
M_END_COL = 3
M_END_LINE = 4  # last line of the method body
M_DECORATORS = 5  # tuple of (dotted_name, args) where args is a tuple of literal args
#                   (non-literal args appear as "?"), e.g. (("api.depends", ("a.b", "c")),)
M_PARAMS = 6  # parameter list source text without the parentheses, e.g. "self, vals_list"
M_SUPER = 7  # True if the body calls super()
M_DOC = 8  # first line of the docstring or None
M_RETURNS = 9  # return annotation source text (e.g. "Self", "'res.partner'") or None

# ClassAttr: other class-level assignments (incl. models.Constraint / models.Index
# declarations and _sql_constraints).
A_NAME = 0
A_LINE = 1
A_COL = 2
A_END_COL = 3
A_KIND = 4  # "attr" | "constraint" | "index" | "sql_constraints"
A_VALUE = 5  # short source text of the value (<= 120 chars)

# ---------------------------------------------------------------------------
# References (Python, XML and CSV share the layout)

R_KIND = 0  # one of the REF_* kinds
R_VALUE = 1  # referenced text: model name, xml id (as written), field path, method name...
R_LINE = 2
R_COL = 3
R_END_COL = 4
R_CTX = 5  # role-specific context tuple; first element is always the role string

REF_MODEL = "model"  # a model name               ctx: (role,) e.g. ("env",), ("_inherit",)
REF_XMLID = "xmlid"  # an XML id (maybe w/o module) ctx: (role,) e.g. ("ref",), ("groups",)
REF_FIELD = "field"  # a field path "a.b.c" or name ctx: (role, model_or_None, ...)
REF_METHOD = "method"  # a method name              ctx: (role, model_or_None, ...)
REF_QWEB = "qweb"  # an owl/QWeb template name (t-name namespace) ctx: (role,)
REF_MODULE = "module"  # a module name (manifest depends, odoo.addons.x)  ctx: (role,)
REF_FILE = "file"  # a module-relative file path (manifest data/assets) ctx: (role, module)

# Python roles
#   REF_MODEL : "_name", "_inherit", "_inherits", "env" (env['x'] / request.env['x'] / env.get('x')?),
#               "comodel" (field comodel positional/keyword), "res_model" (res_model= kw or
#               'res_model': 'x' dict entry), "model_arg" (other well-known model args)
#   REF_XMLID : "ref" (env.ref / request.env.ref / _xmlid_to_res_id / _xmlid_lookup),
#               "groups" (groups= kw, has_group / user_has_groups / _is_... args)
#   REF_FIELD : ctx = (role, model) where model is the model the *first* path segment
#               belongs to when known statically (class context), else None:
#               "related", "depends", "depends_context"(not a field: skipped), "onchange",
#               "constrains", "currency_field", "inverse_name" (model = comodel),
#               "_rec_name", "_order", "_rec_names_search", "_parent_name",
#               "mapped", "filtered", "sorted", "grouped", "domain" (first item of a
#               literal domain tuple), "read", "fields_arg"
#   REF_METHOD: ctx = (role, model): "compute", "inverse", "search", "default"(str),
#               "selection" (str method name), "onchange_method"?, "button"
#
# XML roles
#   REF_XMLID : "ref" (ref="x"), "eval" (ref('x') in eval/context/domain attrs),
#               "inherit_id", "parent", "action", "groups", "percent" (%(x)d / %(x)s),
#               "tcall" (t-call on server QWeb), "web_icon"? (no), "res_id"
#   REF_MODEL : "record" (<record model=...>), "field_model" (<field name="model">x</field>,
#               res_model, src_model, binding_model via text), "function", "delete"
#   REF_FIELD : ctx = ("record", rec_index)               record-level <field name=...>
#               ctx = ("arch", rec_index, subpath)        <field name> inside a view arch;
#                     subpath = tuple of x2many field names walked from the arch root
#               ctx = ("label", rec_index, subpath)       <label for="...">
#               ctx = ("expr", rec_index, subpath)        identifier in invisible/readonly/
#                                                         required/column_invisible exprs (17+)
#   REF_METHOD: ctx = ("button", rec_index, subpath)      <button name="m" type="object">
#   REF_QWEB  : "tcall", "tinherit" (owl / client templates: t-call / t-inherit)
#
# CSV roles (ir.model.access.csv and other data csv)
#   REF_XMLID : "model_id:id", "group_id:id", or "<column>:id"

# ---------------------------------------------------------------------------
# XML facts: {"kind": "xml", "error": ..., "records": [...], "refs": [...],
#             "root": "odoo" | "openerp" | "templates" | other root tag}

RECORDS = "records"
ROOT = "root"

# Record: one per <record>, <template>, <menuitem>, <report>, <act_window>,
# <function>, <delete>, <asset> and owl <t t-name>; CSV rows use the same layout.
X_ID = 0  # xml id / t-name exactly as written (may lack the module prefix) or None
X_MODEL = 1  # target model ("ir.ui.view" for <template>, "ir.ui.menu" for <menuitem>...)
X_TAG = 2  # "record" | "template" | "menuitem" | "report" | "act_window" | "function"
#            | "delete" | "asset" | "qweb" (owl t-name) | "csv" (CSV row)
X_LINE = 3  # position of the id attribute value (else of the tag name)
X_COL = 4
X_END_COL = 5
X_END_LINE = 6  # last line of the element
X_INFO = 7  # dict, keys depending on the record (all optional):
#   "name"       display name (<field name="name">, menuitem name=, template name=)
#   "view_model" for ir.ui.view: value of <field name="model">
#   "inherit_id" xml id of the parent view/template (as written)
#   "type"       view type: arch root tag ("form", "list", "tree", "kanban", "search", ...)
#   "mode"       "primary" | "extension"
#   "priority"   int
#   "res_model"  for actions
#   "parent", "action", "groups", "sequence"  for menus
#   "noupdate"   True when inside <data noupdate="1">
#   "fields"     list of (field_name, line, col, end_col) record-level <field> children

# ---------------------------------------------------------------------------
# CSV facts: {"kind": "csv", "error": ..., "model": "ir.model.access" (from the
#             file name) , "records": [...], "refs": [...]}

CSV_MODEL = "model"

# ---------------------------------------------------------------------------
# Manifest facts: {"kind": "manifest", "error": ..., "values": {...literal manifest...},
#                  "depends": [...], "data": [...], "demo": [...], "assets": {...},
#                  "keys": {key: (line, col, end_col)}}

MANIFEST_VALUES = "values"  # the literal_eval'ed dict (without data/demo/assets lists)
MANIFEST_DEPENDS = "depends"  # list of (module_name, line, col, end_col)
MANIFEST_DATA = "data"  # list of (relative_path, line, col, end_col)
MANIFEST_DEMO = "demo"  # same
MANIFEST_ASSETS = "assets"  # {bundle: [(path_or_glob_or_directive, line, col, end_col)]}
MANIFEST_KEYS = "keys"  # {key: (line, col, end_col)}

# ---------------------------------------------------------------------------
# JS facts: {"kind": "js", "error": None, "imports": [(specifier, line, col, end_col)],
#            "symbols": [Symbol tuples], "refs": [...]}  (REF_QWEB "template" refs)
