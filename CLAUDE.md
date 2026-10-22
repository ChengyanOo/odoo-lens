# odoo-lens — development notes

Dependency-free (stdlib only) Python language server for Odoo 16.0 → 20.0, packaged as a
Claude Code plugin (this repo root is the plugin root: `.claude-plugin/`, `.lsp.json`, `skills/`,
`hooks/` (SessionStart → `bin/odoo-lens session-hint`)).
Design: `docs/ARCHITECTURE.md`. Claude Code's LSP client contract (reverse-engineered, decisive for
output formats): see "How Claude Code consumes results" below.

## Layout

- `odoo_lens/protocol.py` JSON-RPC framing + dispatcher (reader thread, single dispatcher thread)
- `odoo_lens/server.py` lifecycle, doc sync, env/index management, diagnostics publishing, routing
- `odoo_lens/envs.py` Odoo env discovery (checkouts, odev worktrees, custom modules, `.odoo-lens.json`)
- `odoo_lens/indexer.py` + `cache.py` scan → per-root marshal cache → spawn process pool extraction
- `odoo_lens/extract/*` pure extractors → **facts** (`odoo_lens/facts.py` is the contract)
- `odoo_lens/registry.py` semantic index (modules, MRO, fields/methods, xml ids, inverted refs)
- `odoo_lens/analysis/*` per-file cursor analysis → `Target`s (`analysis/targets.py` contract)
- `odoo_lens/features/*` targets → LSP results (navigation, references, hover, symbols, calls, diagnostics)
- `bin/odoo-lens` launcher (picks python3.12+ first, else ≥ 3.10 with a warning; `ODOO_LENS_PYTHON` (≥ 3.10)
  is used exclusively when set; runs with `-I`), `odoo_lens_main.py` entry
- `tests/` unittest suites; `tests/fixtures/mini` synthetic Odoo (regenerate: `python3 scripts/make_fixture.py`)

## Commands

```bash
python3.12 -m unittest discover -s tests -v          # all tests (real-tree tests skip if ~/odev/worktrees absent)
bin/odoo-lens index <path>                           # build/load index, print timings + RSS
bin/odoo-lens query hover <file> <line> <col>        # 1-based, like Claude (definition|references|implementation|typedef|symbols|calls-in|calls-out|targets)
bin/odoo-lens symbols "sale.order." --path <dir>     # whole server-capped answer; --limit N cuts it
bin/odoo-lens check <module_dir>                     # diagnostics as a linter (any dirs/files, each against its own env)
python3.12 scripts/sweep_diagnostics.py ~/odev/worktrees/18.0   # precision sweep: must stay ~0 on Odoo's own code
python3.12 scripts/coverage_python.py ~/odev/worktrees/18.0     # receiver-inference coverage
python3.12 scripts/bench.py --versions 16.0,18.0,20.0           # stdio latency + RSS, like Claude Code drives it
```

Usefulness check that unit tests miss: ask an open question with the installed plugin, e.g.
`cd ~/odev/worktrees/18.0 && claude -p "…" --output-format stream-json --verbose`, then read which tools
Claude used and where it fell back to grep/scripts (each fallback is a missing answer in hover/symbols).

Set `ODOO_LENS_CACHE_DIR` to a scratch dir during development; `ODOO_LENS_NO_CACHE=1` disables the cache;
`ODOO_LENS_LOG=/tmp/odoo-lens.log` + `ODOO_LENS_LOG_LEVEL=DEBUG` for server logs (stderr also lands in `claude --debug`).

## Invariants

- Standard library only; Python 3.10+ syntax; run on 3.12+ (Odoo saas-19.1+ sources need the 3.12 parser).
- Never write to stdout in the server (protocol only; `sys.stdout` is redirected to stderr).
- Positions: 0-based lines, **UTF-16** columns everywhere (ast columns are UTF-8 bytes → `extract/common.SrcIndex`
  in extractors and registry, `documents.bytecol_to_utf16` for a single line).
- Changing a tuple layout in `facts.py` or the extracted contents (new refs, roles) ⇒ bump `FACTS_VERSION` (invalidates caches).
- Facts are builtin types only (marshal); access tuples via the index constants, never magic numbers.
- The dispatcher thread owns published registries; background threads only build new ones (`env.registry` swap + `env.ready`);
  the stat sweep works on `file_stats` / `module_dirs` copies taken on the dispatcher.
- Index build runs with the cyclic GC disabled and `gc.freeze()`s the result (GC was the #1 build cost).
- Precision over recall, especially for diagnostics: Odoo's own sources must stay (near) diagnostic-free.
- No guessing presented as fact: an unknown receiver (Python) or view model (XML arch ref) is never pinned to a
  model for BaseModel/magic members, and single-candidate guesses are flagged (`extra["guess"]`, shown by hover
  and prepareCallHierarchy). findReferences/incomingCalls count a use on an unknown receiver only when exactly
  one model defines the name (`navigation.MAX_UNKNOWN_RECEIVER_MODELS`).
- Python buffers with syntax errors are parsed tolerantly (`pyfacts.parse_tolerant`): line numbers are
  preserved, only the offending lines are neutralised.
- Comments: only one-line notes for non-obvious contracts, invariants and Odoo/client quirks; no docstrings
  (except scripts/sweep_diagnostics.py, whose argparse reads __doc__).

## How Claude Code consumes results (2.1.x)

- Requests: definition/references/hover/documentSymbol/workspace/symbol/implementation/call hierarchy;
  1-based positions from Claude (often a few columns off → analyses snap to the nearest token).
- Sync: `didOpen` before the first request on a file; full-text `didChange` + `didSave` after Edit/Write;
  never `didClose` (except LRU >50) nor `didChangeWatchedFiles` → the server stats files itself.
- Rendering: plain text; a single Location prints "Defined in …" and bypasses the `git check-ignore`
  filter applied to arrays; no client-side caps (>50k chars spills to a file) → server ranks and caps.
- Hover markdown is passed verbatim (richest channel). documentSymbol shows `name (Kind) detail - Line N`
  using `range.start`; workspaceSymbol shows `containerName`.
- Diagnostics: push only, ≤10/file and ≤30/batch, basename only; published here only for edited files
  and custom (non-Odoo-source) modules, never for core files opened by navigation.
- `initialize` must answer in ms (no default startup timeout); requests time out after 60 s.
