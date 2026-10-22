# odoo-lens

Odoo code intelligence for **Claude Code**: a fast, dependency-free language server for
**Odoo 16.0 → 20.0** (including saas-19.x) driven by Claude Code's `LSP` tool (and usable from any LSP
editor), plus a module linter (`odoo-lens check`), a skill and a session-start hint that make Claude use
them. Not to be confused with [odoo-lsp](https://github.com/Desdaemon/odoo-lsp) (a Rust language
server) or [OdooLS](https://github.com/odoo/odoo-ls) (Odoo's official language server).

It understands Odoo rather than just Python: model composition (`_name` / `_inherit` / `_inherits` /
mixins / `base`), module dependency order, merged field definitions, override chains, magic fields,
XML ids (including implicit `model_*` / `field_*`), view archs and x2many sub-views, QWeb and owl
templates, security CSVs (`ir.model.access.csv` and saas-19.4+ `ir.access.csv`), manifests and JS
`@module/...` imports.

| Operation | What you get |
|---|---|
| goToDefinition | model → defining class then extensions (load order); field → original declaration then overrides; `super().x()` → next implementation; XML ids, templates, modules, files, imports |
| hover | field type/comodel/merged attributes/all definitions with source; method signature, decorators, override chain (and the call order with every overriding module installed); model summary; module load order; record/view/action details; inferred type of a variable |
| goToImplementation | every override of a method / extension of a model / view inheriting a view, across all modules |
| findReferences | usages across Python (typed receivers), XML views and data, CSV access rules, JS `orm.call` and imports |
| documentSymbol | outline with Odoo details (model, field types, decorators) |
| workspaceSymbol | `sale.order`, `sale.order.partner_id`, `sale.order.`, `action_confirm`, `sale.view_order_form`, `view:sale.order`, `/shop/cart`, `model:`/`field:`/`xmlid:`… |
| call hierarchy | callers / callees of a method (Python calls, XML buttons, cron / server action code, compute links) |
| diagnostics | unknown model/field/method/xml id, missing module dependency, missing data file, buttons calling `_private` methods, syntax removed in the target version (`attrs`/`states` 17+, `<tree>` 18+, `_sql_constraints` 19+…) or not yet in it (`<list>` before 18.0) |

## Requirements

- Python ≥ 3.12 (`python3.12` … `python3.15` or `python3` on `PATH` or in well-known install locations).
  Odoo saas-19.1+ sources use Python 3.12 syntax, so 3.12+ is needed to index them; without one the
  launcher falls back to Python ≥ 3.10 with a warning. `ODOO_LENS_PYTHON=/path/to/python` (≥ 3.10, same warning
  below 3.12) is used as is, without probing; the launcher stops with an error when it is missing or older than 3.10.
- No third-party packages.

## Install in Claude Code

The repository is a Claude Code plugin and a local plugin marketplace:

```bash
claude plugin marketplace add ChengyanOo/odoo-lens   # or a local clone: claude plugin marketplace add /path/to/odoo-lens
claude plugin install odoo-lens@odoo-lens             # --scope user (default) | project | local
```

Installed from a local clone, the plugin loads in place: edits apply at the next Claude Code session or
after `/reload-plugins` (from GitHub: `claude plugin marketplace update odoo-lens`). For a one-off
session: `claude --plugin-dir /path/to/odoo-lens`.
Uninstall with `claude plugin uninstall odoo-lens@odoo-lens` (and `claude plugin marketplace remove odoo-lens`).

Besides the LSP server and the `odoo-lens` skill, the plugin has a `SessionStart` hook
(`hooks/hooks.json` → `bin/odoo-lens session-hint`, ~50 ms): when the project holds Odoo code (a checkout,
a folder of checkouts, or modules up to 3 levels deep) it adds one short paragraph to Claude's context
pointing at the skill and at `odoo-lens check`; elsewhere it prints nothing. Without it, Claude tends to
grep Odoo sources on debugging tasks instead of asking the index.

Claude Code routes a file extension to the **first** registered LSP server. If you also install
`pyright-lsp` (or Claude Code offers it), disable it in Odoo projects, otherwise it takes `.py` files;
`typescript-lsp` would likewise take `.js` (odoo-lens only resolves Odoo JS: `@module/...` imports,
owl templates, `orm.call` models/methods).

LSP calls on files outside the session's working directories (e.g. Odoo sources in
`~/odev/worktrees/<version>` while working in a ticket folder) go through Claude Code's read
permissions: add the sources to `permissions.additionalDirectories` in your settings (or start Claude
with `--add-dir ~/odev/worktrees`) to avoid a prompt for every core file Claude navigates into.
The skill and the session hint ask Claude to run the linter (`bin/odoo-lens check <module>`) on
debugging tasks; to let it do so without a prompt, allow `Bash(<plugin root>/bin/odoo-lens:*)` (the
session hint prints the exact path) in `permissions.allow` (in `claude -p` runs the command is otherwise denied and Claude falls back to
the LSP tool).

Diagnostics are pushed for files Claude edits and for customer modules (any addons root that is not
one of Odoo's own repositories: `odoo`, `enterprise`, `design-themes`, `themes`, `industry`, `upgrade`,
`upgrade-util`), never for core files merely opened by
navigation. Disable them per project with `{"diagnostics": false}` in `.odoo-lens.json`.

## How Odoo sources are found

- A file inside an Odoo checkout: the community root (`odoo-bin` + `odoo/release.py`) and its siblings
  (`enterprise`, `design-themes`, any directory of modules next to it) — e.g. odev worktrees
  `~/odev/worktrees/<version>/{odoo,enterprise,design-themes}`.
- A file in custom modules (e.g. a ticket folder): the modules around it form the custom addons path;
  the version is the majority of manifest versions (`17.0.1.0.0` → 17.0) and the sources are looked
  up in `~/odev/worktrees/<version>/odoo`, `~/odoo/<version>`, `~/src/odoo/<version>`… (extra
  templates: `ODOO_LENS_SOURCES="~/src/{version}/odoo:..."`).
- Explicit configuration: `.odoo-lens.json` in the project (or any parent directory):

  ```json
  {"odoo": "/path/to/odoo/community", "addons": ["../enterprise", "."], "version": "17.0", "diagnostics": true}
  ```

  or environment variables `ODOO_LENS_ODOO`, `ODOO_LENS_ADDONS` (path list), `ODOO_LENS_VERSION`.

A directory containing several checkouts (e.g. `~/odev/worktrees`) is fine: each file is analysed
against its own version, indexed on first use.

## Performance

The index is built in a process pool and cached per addons root in `$ODOO_LENS_CACHE_DIR`, else
`$XDG_CACHE_HOME/odoo-lens` (default `~/.cache/odoo-lens`), validated by file mtime/size and shared between projects using the same
sources. Odoo 20.0 community + enterprise (≈40k files, 1.6k modules): cold index ≈ 4–5 s,
warm start ≈ 1 s, ≈ 470 MB RSS after indexing (≈ 700 MB after heavy use). Open files are re-checked on every request and all indexed files
every 30 s, so edits made outside the editor (git, scripts) are picked up.

## Command line

```bash
bin/odoo-lens env <path>                        # detected environment
bin/odoo-lens index <path>                      # build/load the index, timings
bin/odoo-lens query hover <file> <line> <col>   # definition|references|hover|implementation|typedef|symbols|calls-in|calls-out|targets (raw cursor analysis, for debugging)
bin/odoo-lens symbols "sale.order." --path <dir> # the whole server-capped answer; --limit N cuts it
bin/odoo-lens check <module_dir>                # diagnostics as a linter (exit 1 on errors); files, modules or a whole
                                                # addons folder, each file checked against its own Odoo env
```

Logs: `claude --debug` shows the server's stderr; `ODOO_LENS_LOG=/tmp/odoo-lens.log`,
`ODOO_LENS_LOG_LEVEL=DEBUG`.

## Development

See `CLAUDE.md` and `docs/ARCHITECTURE.md`. Tests: `python3.12 -m unittest discover -s tests -v`
(real-tree tests run when `~/odev/worktrees/<version>` exists; set `ODOO_LENS_WORKTREES` for another
location).
