# odoo-lens architecture

A dependency-free (Python ≥ 3.10 standard library only) language server that
understands Odoo 16.0 → 20.0 (incl. saas-19.x) codebases. Primary consumer:
Claude Code's `LSP` tool (goToDefinition, findReferences, hover, documentSymbol,
workspaceSymbol, goToImplementation, call hierarchy) and its diagnostics feed;
it also works in any LSP editor.

Design priorities, in order: **correct Odoo semantics → precision (no noisy
results/diagnostics) → speed → breadth**.

## 1. Data flow

```
file path ──► EnvManager (envs.py) ──► Env {version, community root, addons roots}
                                           │
                     scan (indexer.py) ◄───┘   pruned scandir walk + stat
                           │
             cache shards (cache.py) ── hit ──► facts (facts.py contract)
                           │ miss
             extract (extract/*.py, process pool) ──► facts
                           │
                     Registry (registry.py)  models / fields / methods / xml ids /
                           │                 inverted references / module graph
                           ▼
 request ─► RequestContext (server.py) ─► analysis.targets_at() ─► [Target]
                                                                   │
                         features/* (navigation, references, hover, symbols,
                                     calls, diagnostics) ──► LSP JSON
```

* **Facts** (`odoo_lens/facts.py`) are what extractors produce per file: builtin
  types only (marshal-able), tuples accessed via index constants. They are
  cached per addons root in `*.marshal` shards under `$ODOO_LENS_CACHE_DIR`,
  else `$XDG_CACHE_HOME/odoo-lens` (default `~/.cache/odoo-lens`), and
  validated with `(mtime_ns, size)`.
* **Registry** is built from all facts of an env and updated per file
  (`update_file`/`remove_file`) when documents change or the periodic stat sweep
  finds modified files. It is the single source of semantic truth.
* **Analysis** (`odoo_lens/analysis/`) works on ONE file on demand (the file of
  the request): Python AST + Odoo-aware inference, XML context. It turns a
  cursor position into `Target`s (`analysis/targets.py`).
* **Features** (`odoo_lens/features/`) turn targets into LSP results using the
  registry.

## 2. Threads & lifecycle (server.py, protocol.py)

* Reader thread parses stdin; `$/cancelRequest`, `shutdown` and `exit` are
  handled right there (a busy dispatcher must never delay them: Claude Code does
  not always await `shutdown` and would orphan the process).
* Dispatcher thread runs every handler in order and owns every published
  registry (no locks needed). Handlers check `ctx.token.check()` in long loops.
* Index build runs in a background thread (+ spawn process pool for
  extraction, workers' fd 1 redirected to stderr so they can never write into the
  protocol stream); it publishes `env.registry` then sets `env.ready`. Requests
  wait on `env.ready` (≤ 45 s, under Claude Code's 60 s request timeout).
  Open-document overlays are applied lazily by `server._registry()`, and only
  when the document text differs from the indexed disk content.
* Claude Code sends no didChangeWatchedFiles, and didClose only when it evicts
  documents beyond 50 open files (LRU): open documents are re-checked against
  disk on every request, and a 30 s background stat sweep re-extracts changed
  files and applies updates on the dispatcher. The sweep compares the disk
  against copies of `file_stats` / `module_dirs` taken on the dispatcher
  (`server._start_refresh`), so the refresher thread never reads a published
  registry.
* A Python file with a syntax error is parsed tolerantly
  (`extract/pyfacts.parse_tolerant`): the offending lines are replaced by
  `pass` / `if 1:` or blanked (line numbers untouched) until the rest parses,
  so the buffer keeps its facts and its analysis. The index takes these facts
  only when they still declare every class / field / method of the last good
  ones; otherwise it keeps the last good facts. The error is still reported.
* At most 3 envs keep a registry in memory (LRU).
* All columns are UTF-16 (`positionEncoding: utf-16`). UTF-8 byte columns are
  converted by `extract/common.SrcIndex` (extractors, registry) and
  `documents.bytecol_to_utf16` (a single line).

## 3. Environments (envs.py)

* File inside an Odoo checkout (or side-by-side `odoo/ enterprise/
  design-themes/` container): community root from `odoo/release.py`
  (+`odoo-bin`); addons roots = `odoo/odoo/addons`, `odoo/addons`, sibling
  addons roots (enterprise first).
* File in a custom module: custom addons roots = parents of modules found from
  the workspace folder / config dir / module parent; version = majority vote of
  manifest versions (`17.0.x.y.z`); sources located through
  `ODOO_LENS_SOURCES` templates and defaults (`~/odev/worktrees/{version}/odoo`…).
* Overrides: `.odoo-lens.json` (`odoo`, `addons`, `version`, `diagnostics`)
  and `ODOO_LENS_ODOO / ODOO_LENS_ADDONS / ODOO_LENS_VERSION`.
* `env.custom_roots` = addons roots that are customer code: everything except
  the community addons and Odoo's own repositories found next to it
  (`enterprise`, `design-themes`, `industry`, ...). Diagnostics are published as
  soon as a customer file is opened, and customer code ranks first in results.
* A container of several checkouts (e.g. `~/odev/worktrees`) is not
  pre-indexed; each file's env is discovered and indexed lazily.

## 4. Registry semantics (registry.py)

* **Modules**: first occurrence of a module name in addons-root order wins.
  `depends` from the manifest (`base` when it declares none, except for `base`
  itself). `depth(base)=0`, `depth(m)=1+max(depth(deps))`; **load rank** = sort
  by `(depth, name)`; from 19.0 a `test_*` module loads right after its last
  loaded dependency (same depth, ordered after it). Missing dependencies are
  ignored for depth.
* **Scope** of a file = `closure(module)` (module + transitive deps): what is
  guaranteed loaded when that code runs. Lookups are scoped first; when a scoped
  lookup finds nothing, features may fall back to the unscoped (whole env)
  lookup and flag the result ("defined in X, which is not a dependency").
* **Loaded files**: only Python files reachable through imports from a module's
  `__init__.py` are registered (Odoo never imports the others: forgotten
  imports, scripts); the post-order of that import walk is the class definition
  order inside a module. Classes under `tests/`, `migrations/`, `upgrades/`,
  `upgrade/`, `static/` and `_register = False` classes are never registered
  either.
* **Model composition** (Odoo `_build_model` / `add_to_registry`):
  * `_name = X`, `X ∉ _inherit` → defines X; other `_inherit` entries are parents.
  * no `_name` with a str `_inherit = X` → extends X; with a list `_inherit`:
    16–18 take the single parent (the class name verbatim with several), 19+
    derive `_name` from the class name (`HrEmployee` → `hr.employee`);
    `_name = X ∈ _inherit` → extends X.
  * every model except `base` gets `base` as implicit last parent.
  * `_inherits = {P: f}` (and `Many2one(..., delegate=True)`) delegates P's fields
    (not methods) through m2o `f`; 16–18 create `f` automatically.
  * contributions sorted by `(module rank, import order, line)`.
* **MRO(X)**: the bases of X's registry class are rebuilt exactly like Odoo:
  for each contributing class in load order, `LastOrderedSet([cls])` extended,
  for each `_inherit` entry as written (+ `base`), with the previous bases when
  the entry is X itself, else with the parent's registry class. Expanding the
  parents recursively and keeping each class's last occurrence approximates
  Python's C3; the framework classes (Model/AbstractModel/TransientModel/
  CachedModel → BaseModel) close it.
* **Fields** are merged like Odoo does: the effective field type is the most
  derived definition's; effective arguments = merge of all definitions'
  arguments from base-most to most-derived; comodels follow `related=` paths.
  Magic fields (`id`, `display_name`, log-access fields, 16's `__last_update`)
  are synthesized where Odoo's metaclass adds them (`_log_access`/`_auto` read
  from the model's own classes only). Exception: abstract models keep the
  log-access fields so mixin code navigates; diagnostics apply Odoo's default.
* Implicit xml ids: `<module>.model_<model with . → _>` for every module that
  defines **or extends** the model, `<module>.field_<model_>__<field>` –
  resolvable by `records()`.

## 5. Targets & features

| Target | definition | references | hover | implementation |
|---|---|---|---|---|
| model `X` | classes defining X (first), then extensions in scope | every model-name ref (py/xml/csv incl. `model_x` xml ids) | model card | all classes of X in all modules + models inheriting X as parent |
| field `X.f` | definitions in scope (base-most first, overrides after) | definitions + string paths (related/depends/domains/…) + XML `<field name>` + Python attribute accesses inferred on X (or children) | field card (type, comodel, merged args, definition list, source snippet) | all definitions in all modules |
| method `X.m` | definitions in scope (for `super().m` only the ones below the current class) | definitions + calls/attribute accesses inferred on X + XML buttons + `compute=`… strings | method card (signature, decorators, docstring, override chain) | every override in all modules and child models |
| xmlid `m.x` | record definition(s) | every ref (`ref=`, `eval ref()`, `%(x)d`, `inherit_id`, `parent`, `action`, `groups`, `t-call`, `env.ref`, csv `:id` columns) | record card | records/views inheriting it (`inherit_id`) |
| template (owl t-name) | `t-name` element | `t-call` / `t-inherit` / JS `template = "..."` | template card | inheriting templates |
| module | `__manifest__.py` | manifest `depends` entries naming it + absolute `odoo.addons.<m>` imports | module card | modules depending on it |
| file | the file | Python imports of the module, JS imports of the file, manifest entries | file card (module, record counts) | — |
| pysym (Python or JS symbol) | the definition | name-based references in its file and in importing files (JS: named / renamed / default / namespace imports and `export … from` re-exports, under each importer's local name) | signature + docstring (JS: declaration, JSDoc, `@module/path`) | Python subclasses / overrides |

Result ordering is meaningful (Claude reads the first entries first): the
"most informative" location comes first. Results are capped (definitions 50,
implementations 100, references 300 or 100 for Python / JS symbols and files
(time box 2.2 s), workspace symbols 150; outlines and workspace answers stop at
~40k rendered characters, at most 2000 XML/CSV records built). Workspace symbol
answers and outlines end with a final marker saying what was left out;
location lists are cut silently. A single result is returned as a lone
`Location` (Claude Code's gitignore filter only applies to arrays).

findReferences and incomingCalls share two rules. A use on a receiver whose
model is not inferred counts only when the name is not a framework (BaseModel)
member and exactly one model defines it (`navigation.MAX_UNKNOWN_RECEIVER_MODELS`).
Python files Odoo never loads (`migrations/`, `upgrades/`, `upgrade/`,
`static/`) are scanned but ranked last. incomingCalls also lists cron / server
action `code` callers (under the record's xml id, detail `cron code` /
`server action code`) and field arguments naming the method (`compute=`,
`compute_sql=`...); prepareCallHierarchy marks an item built on an uninferred
receiver `receiver not inferred (candidates: ...)`.

### Cursor leniency
Claude's columns are approximate. When the exact column is not on a meaningful
token, analysis picks the closest meaningful token on the same line (prefer
model names > field/method names > xml ids > other identifiers). Never out of
a string literal, a keyword-argument name or a builtin: a cursor there that
has no Odoo meaning gets no result rather than a neighbour's (`super` in
`super().x` resolves like `.x`).

## 6. Python analysis & inference (analysis/python.py)

Per request: parse the file (32-entry LRU keyed by
`(path, len(source), hash(source))`), find the node at the cursor, classify,
infer receiver types. Types: `RS(model)` recordset, `ENV`, `PYMOD(dotted)`,
`PYCLS(path, name)`, `PYINST(path, name)`, unknown.

* `self` in a model method → `RS(current model)`; `cls` in classmethods.
* `self.env` / `request.env` / `x.env` → `ENV`; `ENV['x']` → `RS('x')`;
  `ENV.user` → `RS('res.users')`; `ENV.company(ies)` → `RS('res.company')`;
  `ENV.ref('m.x')` → `RS(model of the record)`.
* Recordset-preserving methods: `search browse create copy sudo with_context
  with_user with_company with_env with_prefetch exists filtered filtered_domain
  sorted ensure_one new union concat _origin browse search_fetch`, binary
  `| & -`, `super().create/copy/…`.
* `RS(M).f` → `RS(comodel)` for relational fields; `mapped('a.b')` walks the
  path; lambdas in `filtered/sorted/mapped/grouped` get `RS(M)`.
* Locals: assignments in the function with a light reaching-definitions
  narrowing (`x = OrderedSet(); x = recs.browse(x)` types as the recordset); loop
  targets and comprehension targets over `RS(M)` are `RS(M)`.
* Calls to model methods are typed from their `-> Self` / model annotations,
  else from the `return` statements of the called method.
* Parameter annotations naming a model class or a model string → `RS`.

Measured on 15 core modules: 90–91 % of record-attribute accesses get a
recordset type and 99.8–99.99 % of those resolve on the inferred model
(16.0, 18.0, 20.0; `scripts/coverage_python.py`).
* Unknown receiver: candidates = models (in scope) defining the member,
  narrowed by the other members used on the same variable (duck typing); one
  candidate → use it, flagged as a guess (hover says so); ≤ 8 → ambiguous
  target (`models=[...]`), ranked by relatedness to the file (models referenced
  in the file, comodels of the current model); more → nothing. Never guessed:
  members of BaseModel and magic fields (they exist on every model), and code
  outside modules (framework, scripts).
* Containers: dict keys typed from `d[rec.company_id] = ...` stores; values
  dicts followed through comprehensions, `list()`, `dict(k=...)`, subscript
  stores and variables to their `create` / `write` / `Command.create`
  consumer; `with Form(rec) as f` / `f.line_ids.new()` / `.edit(i)` typed as
  the record (tests).
* Strings: domain leaves need a real operator (or a variable one in a field
  `search=` method, whose returned domain applies to its own model); `domain=`
  keywords only of ORM domain consumers; `getattr(rec, 'x')` names any member;
  read_group specs `alias:agg(field)` / `field:agg` and `order='f:agg desc'`
  target the field; a role-less literal naming a known model
  (`_get_model_defaults('sale.order')`) targets the model.
* Generic Python: names resolve local → enclosing → class body → module
  globals/imports; imports resolve through `Registry.py_module_file`
  (`odoo.addons.<m>` → module dirs, `odoo.*` → framework, relative imports),
  following re-exports (`from .x import y`, `*`) with a depth limit.

## 7. XML analysis (analysis/xml.py)

Facts already carry every reference with its range and role; the cursor is
matched against refs/records of the current file's facts. View field refs carry
`(record index, subpath)`: the model is `view_model(record)` (follows
`inherit_id`) walked through the x2many fields of `subpath`. For inherited views
using `xpath`, the subpath is derived from `field[@name='x']` steps of the expr.
When the view model stays unknown, an arch ref falls back to the models defining
the name, like an unknown Python receiver: none for BaseModel members and magic
fields; one → used, flagged `extra["guess"]`; ≤ 8 → ambiguous target; more →
nothing.

## 8. Diagnostics policy (features/diagnostics.py)

Precision first: Odoo's own source must produce (almost) **zero** diagnostics;
tests enforce it on a fixed set of core modules per version
(`tests/test_diagnostics.py`, `RealTreeCase.clean_modules`), and
`scripts/sweep_diagnostics.py` measures it on whole checkouts. Report only
when the env is complete enough to be sure (the referenced module is part of
the env).

Codes: `syntax-error`, `unknown-model`, `unknown-field`, `unknown-method`,
`private-method` (warning: a button calling a `_`-prefixed method, which Odoo
refuses to call from buttons), `unknown-xmlid`, `missing-dependency` (defined
only in modules outside the dependency closure), `unknown-module` and
`missing-file` (manifest), `deprecated-attrs` (17.0+ `attrs`/`states` in views),
`deprecated-tree` (18.0+ `<tree>`), `unknown-view-type` (`<list>` view or
`list` xpath before 18.0), `deprecated` (Python API removed/ignored in the
target version: `name_get`, `_sql_constraints`, field `states=`, `group_operator=`,
`user_has_groups`...), `invalid-default` (`default='_method'` string),
`not-imported` (a Python file defining models that no import from the module's
`__init__.py` reaches; says when the import is commented out), `not-loaded` (a
data file whose manifest entry is commented out). XML/CSV files the manifest
does not load are otherwise not checked (Odoo never reads them), and an unknown
field / method declared in a never-imported file says so.
Messages are one self-explanatory line (Claude only sees the file basename) and
suggest close names ("did you mean ...").

Publication policy (server.py): only for files Claude edited and for files of
customer addons roots; never for Odoo sources opened by navigation.

Sweeps of whole checkouts (community + enterprise, 17–33k files each) yield
15–37 diagnostics per version, all verified as genuine defects in Odoo itself
(`scripts/sweep_diagnostics.py`).

## 9. Workspace symbols query language (features/symbols.py)

Answers are SymbolInformation (Claude Code prints `name (Kind) - Line N in
containerName`, grouped by file), best first, capped at 150 and at ~40k
rendered chars (Claude Code moves answers over 50k chars to a file); a truncated
answer ends with a `(... more ... not listed)` marker item. Queries are
case-insensitive (model names are lowercase whatever the query's case); quotes
are ignored and code as Claude writes it is understood: `def x` (= `def:x`),
`class X(Base):` (= `class:X`), `x(` / `x(self)` / `self.x`, `@api.depends(...)`,
`@route('/x')` (= `/x`), `env['sale.order']` / `request.env["sale.order"].search`,
`env.ref('base.group_user')`, `fields.Many2one` / `models.Model` / `api.` /
`http.` / `tools.` (= `odoo.<that>`), `SaleOrder.action_confirm` (the member
through the model of the classes named `SaleOrder`; for other classes the method
of the class or of its Python bases).

| query | results |
|---|---|
| `sale.order` | every class of the model: definition first, then extensions (customer modules first, then load order) - container `sale (defines[, abstract\|transient])` / `sale_stock (extends)`; then the defining class of models starting with the query (`sale.order.line`...) |
| `sale.order.partner_id`, `sale.order/partner_id`, `sale.order:partner_id`, `sale.order partner_id` | definitions of the member (field, else method, else class attribute) visible on the model: the model's own classes (definition first), then inherited ones (mixins, `base`, framework), then `_inherits`-delegated fields - container `sale.order (sale) Many2one→res.partner` / `sale.order (sale_stock) method` / `res.partner (base) Char, via partner_id` |
| `sale.order.partner_id.country_id` | field paths are walked through relational fields |
| `sale.order.` / `sale.order:` | the members of the model, one per name: customer ones first, then own fields, methods, attributes, then inherited ones; when they do not fit, each (own / inherited, kind) group gets a share and a final marker counts the fields / methods / attributes left out |
| `sale.order.amo` | members of the model by prefix > substring, else close spellings (see ranking) |
| `sale order`, `Sale Order Line` | a model spelled with spaces: its classes like `sale.order`, then menus / actions whose label has the words |
| `sale.view_order_form`, `sale.model_sale_order` | records with that xml id, the record creating it first (module load order, then the manifest's data / demo order), then the others (customer first, load order); CSV rows and implicit `model_*` / `field_*__*` ids included - container `ir.ui.view (sale) form view of sale.order, inherits sale.view_x`; then xml ids / models / owl templates by prefix > substring |
| `odoo.addons.sale.models.sale_order[.SaleOrder]`, `odoo.fields.Many2one` | a Python module file, or a symbol of it; names a module re-exports are followed to their definition (19.0+ `odoo.fields` / `models` / `api` re-export `odoo.orm.*`, `odoo.tools` star-imports) - container `odoo.orm.fields_relational, re-exported by odoo.fields` |
| bare name: `action_confirm`, `view_order_form`, `SaleOrder`, `FormView`, `model_sale_order` | exact names in every category - models, record ids (also their local part; bare implicit ids of the module defining the model / field first), owl t-names (also after the module prefix), modules, fields, methods, class attributes, Python classes / functions / methods of non-model classes, JS classes / functions / variables - then prefix, substring; the definitions of one member on one model stay together (definition first) |
| several words: `Sales Orders` | menus and actions by display name; a menuitem without `name` is labelled by its action (`menu: Invoices (from action)`), like Odoo does |
| `/shop/cart`, `https://host/my/orders/42?x=1`, `/fr/shop/cart` | controller methods whose `@route` paths match: exact path, else `<converter>` pattern of a concrete URL, then prefix, substring; a leading language segment (`/fr`, `/fr_BE`, `/es_419`, `/sr@latin`) is stripped when nothing matches exactly, like http_routing does; an override decorated with `@route()` inherits its parent's paths - container `route /shop/cart (website_sale) type=http auth=public, overrides WebsiteSale.cart (website_sale)` |

Prefixes restrict the search: `model:`, `field:`, `method:`, `def:` (methods,
Python and JS functions), `class:` (Python and JS classes), `xmlid:`,
`view:<model>` (views of the model, primary ones first; else views by id),
`action:<model>` (actions on the model: `res_model`, or the `model_id` of server
actions and crons - container `server action → sale.order "Mark as sent"`; else
by id or name), `menu:<id or name>`, `template:` (QWeb `<template>` and owl
`t-name`; an owl `t-inherit-mode="extension"` patches its parent in place and
says so), `module:`, `route:`.

Ranking: exact > prefix > substring; only when none of them found anything
(last resort), names one typo away (insertion, deletion, substitution, swapped
neighbors) or else subsequence matches whose every run of characters starts a
word (`actconf` → `action_confirm`, `useState` → `useDropdownState`) or that skip
few characters; ranked by match quality, at most 20 (5 per name), flagged
`[fuzzy match]`; scattered characters (`name_get` on 18.0) give nothing. Within
a tier models, records, templates, modules, then members, Python, JS; customer
modules (under `env.custom_roots`) first, then load order, path, line -
deterministic. Files under `tests/`, `migrations/`... are not indexed. A module
file no import chain from its `__init__.py` reaches is never run by Odoo: its
model classes are left out, its other code is flagged `not imported by its
module`, its routes `controller file not imported by its module: route never
registered`; outlines mark its classes `not imported: never loaded`. ACL rows
(`ir.model.access`, saas-19.4+ `ir.access`) show model, group (`restriction (all
users)` for an `ir.access` row without group), permissions / operation and
domain.

Index: per category, entries grouped by lowercase name, searched by bisect
(exact / prefix), `str.find` / regex over a newline-joined name haystack
(substring / subsequence) and set probes of the one-typo variants of the query.
Categories are fed per file in families (model classes from the registry,
XML/CSV records, Python symbols, JS symbols) built on first use, then updated
only for the files whose facts changed when `registry.generation` moves (every
registry update bumps it: an open document whose text differs from the indexed
disk content, a stat-sweep change, a new module; a didOpen of an indexed file
whose text matches the disk bumps nothing); a full registry rebuild (a file
becoming (un)reachable from its module's `__init__.py`) or a manifest change
(data load order) starts over. Routes are parsed from the decorator source
(facts keep literal paths only, not `type=` / `auth=`) once per file content.
Stored in `registry.__dict__`, referencing the registry weakly so evicted
registries are freed.

## 10. Performance budgets

| operation | budget | measured (Odoo 20 community+enterprise, 40k files, M-series 10-core) |
|---|---|---|
| initialize response | < 100 ms | 43–50 ms |
| cold index (no cache) | < 6 s | 4.2–4.5 s |
| warm index (cache) | < 1.5 s | 0.9 s (scan 0.45, cache 0.17, registry 0.3) |
| definition / hover | < 50 ms | 0.1–5 ms typical |
| references | < 3 s | 0.5–2.4 s (time-boxed) |
| workspace symbols | < 150 ms | 2–130 ms after a one-time ~0.4 s index build |
| diagnostics of one file | < 150 ms | 2–12 ms |
| server RSS | | ~470 MB after load, ~680 MB with all lazy indexes |

Techniques: process-pool extraction with marshal batches; per-root marshal
shards (`marshal.loads` of the whole blob, not `marshal.load(file)`); the
cyclic GC disabled while building and `gc.freeze()` afterwards (GC traversals
were the #1 build cost); "index mode" extraction that omits high-volume facts
only needed for open files; tuples with index constants; interned names; lazy
per-(model, scope) caches keyed by `registry.generation`; byte-level prefilters
(`bytes.find`) before any parsing in reference searches; ASCII fast paths for
UTF-16 conversion.

## 11. Testing

* `tests/fixtures/mini/` – a tiny synthetic Odoo (framework + base + a few
  addons) for fast deterministic unit tests.
* Real-tree tests (skipped when `<worktrees>/<ver>` is absent; `<worktrees>` =
  `$ODOO_LENS_WORKTREES`, default `~/odev/worktrees`): golden definitions per
  version; a fixed set of core modules must stay diagnostic-free.
* Manual sweeps (scripts, not run by the tests): *coverage*
  (`scripts/coverage_python.py`: share of record-attribute accesses that
  resolve); *precision* (`scripts/sweep_diagnostics.py`: diagnostics on whole
  checkouts must stay ~0).
* End-to-end: `tests/lsp_client.py` drives `bin/odoo-lens` over stdio with
  Claude Code's exact initialize capabilities and sync behaviour
  (`tests/test_e2e.py`); `scripts/bench.py` measures per-operation latency.
* Live: `claude -p --plugin-dir <repo> ...` in an Odoo checkout exercises the
  real Claude Code `LSP` tool against the server.
