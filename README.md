# odoo-lens

odoo-lens helps Claude Code understand Odoo code. It has three parts:

- a language server for Odoo 16.0 to 20.0, saas-19.x included;
- a linter for Odoo modules;
- a skill that tells Claude when to use them.

It is plain Python with no dependencies.

Why bother? Odoo spreads one model over many modules. A field can be declared in one module,
extended in three others and overridden in a fourth. Grep finds all of those lines. It can't tell
you which definition wins, or in what order the `super()` calls run. odoo-lens can, because it
indexes Odoo the way Odoo loads itself.

This is not [odoo-lsp](https://github.com/Desdaemon/odoo-lsp), a language server written in Rust.
It is not [OdooLS](https://github.com/odoo/odoo-ls) either, which is Odoo's official language server.
odoo-lens was built for Claude Code, but any editor that speaks LSP can use it.

## What it knows

Reading Odoo as plain Python isn't enough. odoo-lens follows the parts that are specific to Odoo:

- how models are put together: `_name`, `_inherit`, `_inherits`, mixins and `base`
- module dependency order, and so the order overrides run in
- field definitions merged across modules, plus the magic fields
- XML ids, including the implicit `model_*` and `field_*` ones
- view archs with their x2many sub-views, QWeb and owl templates
- security CSVs: `ir.model.access.csv`, and `ir.access.csv` from saas-19.4 on
- manifests and JS `@module/...` imports

## What you can ask

Claude talks to the server through its `LSP` tool. Each request gives back:

| Request | What you get |
|---|---|
| Hover | The most useful one. For a field: its type, comodel, merged attributes and every definition. For a method: its signature, decorators and override chain, including the call order once every overriding module is installed. It also works on models, modules, records, views, actions, and the inferred type of a variable. |
| Go to definition | For a model: the class that defines it, then every extension in load order. For a field: the original declaration, then its overrides. On `super().x()`: the next implementation. It also works on XML ids, templates, modules, files and imports. |
| Go to implementation | Every override of a method, every extension of a model, every view that inherits a view. |
| Find references | Uses in Python (when the receiver's type is known), XML views and data, CSV access rules, `orm.call` and imports in JS. |
| Document symbols | An outline of the file, with models, field types and decorators. |
| Workspace symbols | Search by name: `sale.order`, `sale.order.partner_id`, `sale.order.` (every member), `action_confirm`, `sale.view_order_form`, `view:sale.order`, `/shop/cart`, and so on. |
| Call hierarchy | Who calls a method and what it calls. Callers include Python code, XML buttons, cron and server action code, and `compute=` links. |

## The linter

```bash
bin/odoo-lens check path/to/my_module
```

It prints one line per problem, as `file:line:col`, and exits with 1 if it found errors. It reports:

- unknown models, fields, methods and XML ids, with a "did you mean" when something close exists
- models used from a module that isn't in your `depends`
- manifest entries pointing to modules or files that don't exist
- Python files that are never imported, and data files whose manifest entry is commented out
- buttons calling a `_private` method, which Odoo refuses to do
- syntax your Odoo version removed, such as `attrs` and `states` in 17, `<tree>` in 18 and
  `_sql_constraints` in 19, or doesn't have yet, such as `<list>` before 18

You can give it one module, several, or a whole addons folder. Each file is checked against its own
Odoo version. It is tuned to stay quiet on correct code: a full 18.0 checkout gives about 30
diagnostics in total.

Claude also gets these diagnostics while it works, both for the files it edits and for your own modules.
Odoo's own repositories (`odoo`, `enterprise`, `design-themes`, `themes`, `industry`, `upgrade`,
`upgrade-util`) only get them for files Claude edits, not for files it merely opens. To turn
diagnostics off for a project, put `{"diagnostics": false}` in `.odoo-lens.json`.

## Requirements

You need Python 3.12 or newer, because Odoo saas-19.1 and later use 3.12 syntax.

The launcher looks for `python3.12` up to `python3.15`, and for `python3`. It searches your `PATH`
and the usual install locations. If it only finds 3.10 or 3.11, it uses that and prints a warning.

To choose the interpreter yourself, set `ODOO_LENS_PYTHON=/path/to/python`. It must be 3.10 or newer,
and the launcher won't look for another one.

No other packages are needed.

## Install

The repository is both a Claude Code plugin and a plugin marketplace:

```bash
claude plugin marketplace add ChengyanOo/odoo-lens
claude plugin install odoo-lens@odoo-lens
```

`install` takes `--scope user` (the default), `project` or `local`. To get updates, run
`claude plugin marketplace update odoo-lens`.

You can add a local clone instead: `claude plugin marketplace add /path/to/odoo-lens`. A local clone
is loaded in place, so your edits apply at the next session, or after `/reload-plugins`.

To try it for a single session without installing:

```bash
claude --plugin-dir /path/to/odoo-lens
```

To remove it:

```bash
claude plugin uninstall odoo-lens@odoo-lens
claude plugin marketplace remove odoo-lens
```

### Other language servers

Claude Code sends each file extension to the first LSP server registered for it. If you have
`pyright-lsp`, it takes the `.py` files and odoo-lens never sees them, so disable it in your Odoo
projects. `typescript-lsp` does the same with `.js` files. odoo-lens only covers the Odoo side of JS:
`@module/...` imports, owl templates and `orm.call`.

### Permissions

When you work in a ticket folder, the Odoo sources usually live somewhere else, say
`~/odev/worktrees/17.0`. Claude needs to be allowed to read them. Otherwise it asks every time it opens
a core file. Add the sources to `permissions.additionalDirectories` in your settings, or start Claude
with `--add-dir ~/odev/worktrees`.

The skill asks Claude to run `odoo-lens check`. To let it do that without asking you, add
`Bash(<plugin root>/bin/odoo-lens:*)` to `permissions.allow`. The session hint prints the exact path.
In `claude -p` runs the command is denied without that rule, and Claude falls back to the LSP tool.

### The session hint

The plugin adds a `SessionStart` hook. It takes about 50 ms. If your project holds Odoo code, it adds
a short note to Claude's context that points at the skill and at `odoo-lens check`. Odoo code here
means a checkout, a folder of checkouts, or modules up to three levels down. Otherwise the hook prints
nothing. Without the note, Claude tends to grep the Odoo sources on debugging tasks instead of asking
the index.

## Finding your Odoo sources

Most of the time there is nothing to configure.

If the file is inside an Odoo checkout, odoo-lens finds the community root, which is the folder with
`odoo-bin` and `odoo/release.py`. It also uses the folders next to it: `enterprise`,
`design-themes`, or any other folder of modules. odev worktrees
(`~/odev/worktrees/<version>/{odoo,enterprise,design-themes}`) work as they are.

If the file is in a folder of custom modules, such as a ticket folder, those modules become the custom
addons path. The Odoo version is the one most manifests agree on (`17.0.1.0.0` means 17.0).
odoo-lens then looks for that version's sources in the usual places: `~/odev/worktrees/<version>/odoo`,
`~/odoo/<version>`, `~/src/odoo/<version>` and a few others. You can add your own with
`ODOO_LENS_SOURCES="~/src/{version}/odoo:..."`.

A folder that holds several checkouts, like `~/odev/worktrees`, works too. Each file is analysed
against its own version, and each version is indexed the first time you need it.

If the guess is wrong, put a `.odoo-lens.json` in the project or in any parent folder:

```json
{"odoo": "/path/to/odoo/community", "addons": ["../enterprise", "."], "version": "17.0", "diagnostics": true}
```

Or set `ODOO_LENS_ODOO`, `ODOO_LENS_ADDONS` (paths separated by `:`) and `ODOO_LENS_VERSION`.

## Speed

The index is built in a process pool and cached per addons root. The cache is checked against file
modification times and sizes. Projects that use the same sources share it.

On Odoo 20.0 community plus enterprise (about 40,000 files and 1,600 modules), a cold index takes 4
to 5 seconds and a warm start about 1 second. Memory sits around 470 MB after indexing and grows to
about 700 MB with heavy use.

The cache lives in `$ODOO_LENS_CACHE_DIR`. If that isn't set, it goes in `$XDG_CACHE_HOME/odoo-lens`,
which defaults to `~/.cache/odoo-lens`.

Open files are checked again on every request, and all indexed files every 30 seconds. Changes made
outside the editor, by git or by a script, are picked up.

## Command line

You can run the same queries from the shell, which is handy when something looks off:

```bash
bin/odoo-lens env <path>                          # the detected environment
bin/odoo-lens index <path>                        # build or load the index, with timings
bin/odoo-lens query hover <file> <line> <col>     # any request, 1-based positions
bin/odoo-lens symbols "sale.order." --path <dir>  # workspace symbol search
bin/odoo-lens check <path>                        # the linter
```

`query` takes `definition`, `references`, `hover`, `implementation`, `typedef`, `symbols`,
`calls-in`, `calls-out` or `targets`. `targets` shows the raw cursor analysis, which is useful for
debugging. `symbols` prints the whole answer. Add `--limit N` to cut it short.

To see what the server is doing, run `claude --debug`, which shows the server's stderr. You can also
set `ODOO_LENS_LOG=/tmp/odoo-lens.log` and `ODOO_LENS_LOG_LEVEL=DEBUG`.

## Development

Start with `CLAUDE.md` and `docs/ARCHITECTURE.md`. Run the tests with:

```bash
python3.12 -m unittest discover -s tests -v
```

Tests that need real Odoo sources run only when `~/odev/worktrees/<version>` exists. Set
`ODOO_LENS_WORKTREES` if yours are somewhere else.
