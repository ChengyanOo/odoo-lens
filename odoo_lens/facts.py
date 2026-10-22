# Per-file extractor output, cached and sent between processes with marshal (5x faster than pickle):
# builtin types only (dict/list/tuple/str/int/float/bool/None).
# Index tuples via the constants below; ctx tuples and manifest entries are unpacked.
# Lines 0-based; cols UTF-16 code units; a token (line, col, end_col) lies on one line and covers
# a string literal's content without the quotes.
# extract(path, source[, index]) -> dict; never raises: on a parse error, partial facts + ERROR.

from __future__ import annotations

# Bump when a tuple layout or the extracted contents (refs, roles, keys) change: invalidates caches.
FACTS_VERSION = 12

KIND = "kind"
ERROR = "error"  # None | (message, line, col)

KIND_PY = "py"
KIND_XML = "xml"
KIND_CSV = "csv"
KIND_MANIFEST = "manifest"
KIND_JS = "js"

IMPORTS = "imports"
SYMBOLS = "symbols"
CLASSES = "classes"
REFS = "refs"

# Import: one per imported name, pos = the whole alias ("a.b as c").
# "from .x import y" -> ("x", "y", None, 1); "import a.b": name None; "from x import *": name "*".
I_MODULE = 0
I_NAME = 1
I_ASNAME = 2
I_LEVEL = 3
I_LINE = 4
I_COL = 5
I_END_COL = 6

# Symbol: module-level definition; S_KIND "class" | "def" | "var" (assignment target).
S_NAME = 0
S_KIND = 1
S_LINE = 2
S_COL = 3
S_END_COL = 4
S_END_LINE = 5

K_NAME = 0
K_LINE = 1
K_COL = 2
K_END_COL = 3
K_END_LINE = 4
K_BASES = 5  # dotted texts ("?" if not dotted), e.g. ("models.Model",)
K_MODEL = 6  # None for non-model classes, else an MI_* dict
K_FIELDS = 7
K_METHODS = 8
K_ATTRS = 9
K_DOC = 10

MI_NAME = "name"
MI_INHERIT = "inherit"  # list, [] if none
MI_INHERITS = "inherits"  # {parent_model: m2o_field}
MI_KIND = "kind"  # "Model" | "AbstractModel" | "TransientModel" | "BaseModel" | None (unknown base)
#   | "CachedModel" (saas-19.1+) | "framework" (ORM bases in odoo/models.py, odoo/orm/models*.py)
MI_ATTRS = "attrs"  # other small literal _attributes, e.g. _description, _order
MI_INHERIT_STR = "inherit_str"  # _inherit was a str: implies _name (a list does not on 19+)

F_NAME = 0
F_LINE = 1
F_COL = 2
F_END_COL = 3
F_END_LINE = 4
F_TYPE = 5  # last dotted segment, e.g. "Many2one"
F_COMODEL = 6
F_ARGS = 7  # {kwarg: value}; positional args renamed to their keyword
# F_ARGS values: small literals as-is, anything else ("=", source_text[:120]).

M_NAME = 0
M_LINE = 1
M_COL = 2
M_END_COL = 3
M_END_LINE = 4
M_DECORATORS = 5  # ((dotted_name, args), ...); args: str literals of positional and route=/routes=
#                   args (str lists flattened), "?" for anything else
M_PARAMS = 6  # source text inside the parentheses, e.g. "self, vals_list"
M_SUPER = 7  # body calls super().<this method> (super().other() does not count)
M_DOC = 8
M_RETURNS = 9  # return annotation source text or None

# ClassAttr: other class-level assignments.
A_NAME = 0
A_LINE = 1
A_COL = 2
A_END_COL = 3
A_KIND = 4  # "attr" | "constraint" | "index" (models.Constraint/[Unique]Index) | "sql_constraints"
A_VALUE = 5  # source text, <= 120 chars

# Ref: shared by every facts kind.
R_KIND = 0
R_VALUE = 1  # as written (an xml id may lack its module)
R_LINE = 2
R_COL = 3
R_END_COL = 4
R_CTX = 5  # (role, ...): layout depends on source and role, see below

REF_MODEL = "model"
REF_XMLID = "xmlid"
REF_FIELD = "field"  # a field name or path "a.b.c"
REF_METHOD = "method"
REF_QWEB = "qweb"  # owl/client template name
REF_MODULE = "module"  # manifest depends: ("depends",); odoo.addons.x imports live in IMPORTS
REF_FILE = "file"  # manifest file path: (manifest key, module)

# Python ctx (model = class model, or the comodel for domain/inverse_name; may be None):
#   MODEL (role,): _name _inherit _inherits comodel env res_model
#   XMLID (role,): ref template groups
#   FIELD (role, model): related depends onchange constrains currency_field inverse_name domain
#     _inherits _order _rec_name _parent_name _fold_name _active_name _date_name _rec_names_search
#   METHOD (role, model): compute inverse search group_expand compute_sql selection default
#   Field paths in method bodies are not indexed (query-time analysis finds them).
# XML ctx (rec = index in this file's RECORDS, sub = tuple of x2many fields from the arch root):
#   XMLID (role,): ref eval inherit_id parent action groups percent tcall view_ref report delete
#   MODEL (role,): record field_model function delete
#   FIELD: ("record", rec) ("record_domain", rec) ("arch" | "label" | "expr", rec, sub)
#          ("arch_domain", rec, sub, on_field)  on_field: field of a <field domain>, else None
#   METHOD: ("button", rec, sub) ("function", model) ("code", rec)
#   QWEB (role,): tcall tinherit
# CSV ctx: XMLID (column,) for "*:id" / "*/id" columns; MODEL ("csv",);
#   FIELD ("csv_header", file_model) ("domain", row_model or None)
# JS ctx: QWEB ("template",); MODEL ("js",); METHOD ("js", model)

RECORDS = "records"
ROOT = "root"

# Record: <record> <template> <menuitem> <report> <act_window>, other data tags with an id
# (<asset>), owl t-name ("qweb"), CSV rows ("csv"); registry-made "implicit" (ir.model[.fields]).
# <function>/<delete> only produce refs.
X_ID = 0  # as written (may lack the module) or None
X_MODEL = 1  # None for owl templates and unknown data tags
X_TAG = 2
X_LINE = 3  # the id (or t-name) value, else the tag name
X_COL = 4
X_END_COL = 5
X_END_LINE = 6
X_INFO = 7  # dict, or None for big-CSV rows in index mode (use `or {}`)
#   "type": arch root tag; "view_type": <field name="type"> text, "qweb" for <template>;
#   also name view_model inherit_id mode priority res_model parent action groups noupdate key...

CSV_MODEL = "model"  # from the file name

MANIFEST_VALUES = "values"  # literal values; file lists (data, demo, *_xml, qweb, assets) left out
MANIFEST_DEPENDS = "depends"  # [(module, line, col, end_col)]
MANIFEST_DATA = "data"  # [(path, line, col, end_col)], also init_xml/update_xml/qweb
MANIFEST_DEMO = "demo"  # same, also demo_xml
MANIFEST_ASSETS = "assets"  # {bundle: [(path_or_glob_or_directive, line, col, end_col)]}
MANIFEST_KEYS = "keys"  # {key: (line, col, end_col)}

# JS facts: IMPORTS = [(specifier, line, col, end_col)] (not Import tuples), SYMBOLS, REFS.
