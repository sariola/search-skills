# search-skills

Exa + Brave search clients and MCP servers. Two Python packages, two
TypeScript MCP servers, one offline test suite.

## Layout

- `exa/`, `brave/` — Python packages. Public API in `src/<name>/__init__.py`;
  `src/<name>/_async.py` is the shared async-compat layer (byte-identical across
  both packages — never edit one without the other; verify with `cmp`).
- `mcp/exa/`, `mcp/brave/` — TypeScript MCP servers. Source `index.ts`,
  built to `build/index.js`. `smoke.mjs` (surface + a few live calls),
  `probe.mjs` (full-surface live exercise, self-cleaning), and
  `strict_framing.mjs` (in-repo, CWD-independent JSON-RPC framing check).
- `tests/` — offline `unittest` suite (wire-shape, classification, and
  concurrency tests; no network).
- `.analysis/` — audit artifacts. Read-only; do not edit.

## Commands

```sh
# MCP typecheck + build (target=node; output must run on plain node, not just bun)
cd mcp/exa && bun run typecheck && bun run build     # same in mcp/brave

# Live verification (API keys from env; NEVER print key values).
# smoke/probe resolve their server path from the script, so run them from the root:
node mcp/exa/smoke.mjs     # node mcp/brave/smoke.mjs
node mcp/exa/probe.mjs     # node mcp/brave/probe.mjs   # longer; creates+deletes a webset

# JSON-RPC framing check (in-repo, CWD-independent; stdout must stay pure JSON)
node mcp/strict_framing.mjs exa     EXA_API_KEY          # expect: bad=0
node mcp/strict_framing.mjs brave   BRAVE_SEARCH_API_KEY # expect: bad=0

# Offline suite (wheels are not installed — the PYTHONPATH is required)
PYTHONPATH="exa/src:brave/src" python3 -m unittest discover -s tests
```

## Invariants

- **MCP stdout is the JSON-RPC channel.** All server logging goes to
  `console.error` (stderr). A stray `console.log` corrupts framing — the SDK
  silently drops the bad lines. Verify with `strict_framing.mjs` (expect `bad=0`).
- **Error classification order matters.** Brave marks plan-gated errors
  (`code=OPTION_NOT_IN_PLAN`) with `meta.component="authentication"`; check the
  `code` before the component or plan-gate gets misclassified as `auth`.
- **Exa beta headers:** `POST /agent/runs/{id}/stop` requires
  `Exa-Beta: agent-max-effort-2026-07-27` (and only works on effort=max runs);
  every `/batches*` endpoint requires `Exa-Beta: batches-2026-06-06`. Both are
  sent automatically by the clients.
- **Doc density: Linear style.** One sentence + inline `(default: X)`; no
  multi-paragraph enum tours in tool descriptions, docstrings, or SKILL.md.
- **Python "async" is compatibility, not concurrency.** Public functions are
  sync; their returns are awaitable shims applied by `_apply_async_to(globals(),
  public=__all__)` — scoped to `__all__` so imported helpers (e.g.
  `parsedate_to_datetime`) keep their original, non-wrapped identity.
- **The engine is a process-lifetime singleton, pinned by `tests/test_concurrency.py`.**
  One `httpx.Client` and one `ThreadPoolExecutor`, each built under a
  double-checked lock so concurrent first-use constructs exactly one of each.
  The pool is capped at the keep-alive connection ceiling (`_POOL_MAX` =
  `max_connections`); a worker past it only queues on a connection. Per-call
  parallelism comes from `_fanout`'s semaphore (capped at the pool) — never
  grow the pool per call.
  **A nested fan-out runs inline, not on the pool.** `_fanout` called from
  inside a pool worker (e.g. `batch(mode='all')` → `search` → the per-endpoint
  fan-out) must NOT submit to the shared pool it is parked on: with the pool
  saturated by its siblings it deadlocks forever (0 of N outer jobs complete).
  So it runs its jobs in the current thread. The parent fan-out's workers
  already provide the real parallelism; the inline jobs just serialize within
  one worker, which respects the connection ceiling. If you "simplify" this
  back to `pool.submit(...)` you reintroduce the deadlock —
  `tests/test_concurrency.py::test_nested_fanout_does_not_starve_shared_pool`
  hangs (subprocess-timeout) instead of failing fast without the fix.
- **`await` on an event loop still blocks the loop** — use a worker thread.
- Paid surfaces (exa agent runs, websets, batches; brave llm_context/suggest/
  spellcheck) are plan-gated: a `plan` category error means the key lacks the
  add-on, not a bug. Probes treat it as pass; probes clean up anything they
  create (webset delete, batch delete).
