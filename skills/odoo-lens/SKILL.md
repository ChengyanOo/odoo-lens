---
name: odoo-lens
description: Odoo 16.0-20.0 code intelligence (odoo-lens) for any task on Odoo code — debugging a custom or customer module, migration/upgrade breakage, finding where a model / field / method / xml id / view is defined, extended or overridden, override and super() order, what references something. Answers come from an index of the Odoo sources through the LSP tool (hover, goToDefinition, findReferences, goToImplementation, workspaceSymbol) and a module linter (`odoo-lens check <module_dir>`: unknown fields/models/methods/xml ids, missing dependencies, files never imported, syntax removed in the target version). Use it before grepping or reading Odoo sources.
---

# Odoo navigation with the LSP tool

`LSP` is a deferred tool: load it first with ToolSearch `select:LSP`. The odoo-lens server answers for `.py`, `.xml`, `.csv` and `.js` files of Odoo checkouts and custom modules (Odoo 16.0 → 20.0 incl. saas-19.x) and understands Odoo semantics: `_inherit`/`_inherits`/mixins, module dependency order, magic fields, XML ids (also implicit `model_*`/`field_*`), view archs and sub-views, QWeb/owl templates.

Every operation takes `filePath`, `line`, `character` (1-based). Aim at the token itself — a model name string, a field or method name, `self.x`, `record.field`, `env['model']`, `env.ref('mod.id')`, `ref="..."`, `<field name="...">`, `t-call`, a manifest `depends` entry. A column a few characters off is tolerated.

## Debugging a module (customer bug, migration, "it does nothing", install/upgrade error)

Run the linter on the module first — one command, exact file:line answers, no false alarms on Odoo's own code:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/odoo-lens check <module_dir>     # or the whole custom addons folder (each file uses its own Odoo version)
```

It reports unknown models / fields / methods / xml ids for the module's Odoo version (with renames and "did you mean"),
missing `depends`, files never imported (commented-out imports included), buttons calling `_private` methods, and syntax
removed in that version (`attrs`/`states` 17+, `<tree>` 18+, `_sql_constraints` 19+...) or not yet in it (`<list>` before 18.0).
Then use the LSP operations below to understand the code involved.

## Which operation

| Need | Operation |
|---|---|
| Where is model / field / method / xml id defined? | `workspaceSymbol` with a query (below). It searches the Odoo environment of the file given as `filePath` (line 1, character 1): pass any file of the right project/version. |
| What is this? Type, comodel, merged attributes, every definition and override, docstring | `hover` — the richest answer, often enough on its own |
| Jump to definition(s) | `goToDefinition` — original declaration first, then overrides in module load order; on `super().x()` the next implementation |
| All overrides of a method, all extensions of a model, views inheriting a view | `goToImplementation` (the model's own classes in load order, then inheriting models; for a method, then the inherited implementations most-derived first, i.e. super() order) |
| In which order do overrides run / does `super()` chain? | `hover` on the method: the chain seen from this module, plus the call order with every overriding module installed (Odoo's real load order: dependency depth, then name) — no need to compute module depths yourself |
| Every usage (Python, XML views and data, CSV access rules, JS `orm.call` and imports) | `findReferences` |
| Outline of a big file with Odoo details (model, field types, decorators) | `documentSymbol` |
| Who calls a method (Python, buttons, cron / server action code, `compute=`...) / what it calls | `incomingCalls` / `outgoingCalls` |

## workspaceSymbol queries

- `sale.order` (or `sale order`) → defining class, then every module extending it
- `sale.order.partner_id` (or `sale.order:partner_id`) → field definitions; `sale.order.action_confirm` → method and its overrides; `sale.order.` → members (customer ones first; a long listing ends with a count of what is not shown — narrow it with `sale.order.part`)
- `sale.view_order_form` → XML record; `view:sale.order` → views of a model; `action:sale.order` → its window/server/report actions, crons and Action/Print menu entries
- `action_confirm` → members with that name on every model; a typo or abbreviation still finds close names (flagged)
- `/shop/cart` → the controller route (a `/fr/...` language prefix is ignored)
- code is accepted as typed: `def _compute_amount`, `class SaleOrder(...)`, `self.partner_id`, `env['sale.order']`, `env.ref('sale.view_order_form')`, `@api.depends(...)`, `SaleOrder.action_confirm`, dotted Python paths like `odoo.fields.Many2one`
- prefixes: `model:` `field:` `method:` `xmlid:` `view:` `action:` `menu:` `template:` `module:` `class:` `def:` `route:`

## Good to know

- Results are scoped to what the current file's module can see (its `depends` closure). Hover warns when a member only exists in modules outside the dependencies — a classic support bug ("missing dependency").
- After edits, diagnostics report unknown models/fields/methods/xml ids, missing dependencies and syntax removed in the target version (`attrs`/`states` since 17.0, `<tree>` since 18.0, `_sql_constraints` since 19.0, ...). They are high-precision: fix them.
- The Odoo version and sources are detected automatically: Odoo checkouts, odev worktrees (`~/odev/worktrees/<version>/{odoo,enterprise}`), or custom modules matched to sources through their manifest version. Override with a `.odoo-lens.json` next to the modules: `{"odoo": "/path/to/community", "addons": ["../enterprise"], "version": "17.0"}`.
- Same engine from the shell (useful for batch checks): `${CLAUDE_PLUGIN_ROOT}/bin/odoo-lens query hover <file> <line> <col>`, `... symbols "sale.order." --path <dir>`, `... check <module_dir>`.
