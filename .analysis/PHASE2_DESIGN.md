# PHASE 2 — DESIGN (architecture, decisions, build order)

Date: 2026-09-11. Inputs: PHASE1_CONSOLIDATED.md (verified defects + API surface),
phase1_signatures.json, SKILL.md (exa/brave), pyproject.toml (exa/brave),
tests/test_search_contracts.py (baseline 6/6).

## 0. Hard constraints (non-negotiable)
1. **Public top-level names unchanged.** `import exa` and `import brave` must expose the
   identical `__all__` (exa 124, brave 78) with identical call signatures
   (phase1_signatures.json). Every public function stays awaitable.
2. **Self-contained skill install.** SKILL.md runs `uv run --with /abs/path/to/exa` (and .../brave)
   as a SINGLE path. Each package is a standalone wheel (`packages = ["src/<name>"]`). ⇒ the shared
   plumbing CANNOT become a third `search_skills_common` distribution (it would be an unresolvable
   dependency and break the one-line install). Shared code is therefore **mirrored** into each
   package as private submodules, and a **hash-sync test** guards drift.
3. **Credentials never printed** (existing skill rule) — preserved.
4. **Return-key names:** when code and its docstring/skill-doc disagree, prefer aligning the CODE to
   the shape the SKILL.md/references already document (callers trust the docs). Note exceptions below.

## 1. Package layout (after refactor)

Both packages share the same skeleton; only the endpoint domain differs.

```
exa/src/exa/
  __init__.py        # thin facade: re-export all public names from submodules; run async-wrap
  errors.py          # ExaError + 6 subclasses  (exa-only)
  _http.py           # engine: _CLIENT/_pool/_retry_after_seconds/_fanout + _request (shared-mirror)
  _keys.py           # _seek_api_key/_read_key_file/_get_api_key + cache  (shared-mirror)
  _async.py          # dual sync/async wrapper set + _apply_async_to  (shared-mirror)
  _domain.py         # exa _domain_of etc. (KEEP SEPARATE from brave — divergent, C4)
  models.py          # Result, SearchResults, Answer, AgentRun, AgentTrace
  search.py          # search, run, brief, modes, find_similar, similar_to, entity_* , top_terms
  contents.py        # fetch, _contents_block
  answer.py          # answer, stream_answer, stream_answer_source
  agent.py           # agent, agent_structured, agent_chat, agent_trace, agent_list/get/delete/cancel/events
  monitors.py        # monitor_*, wmonitor_*
  websets.py         # webset_*, enrichment_*, import_*, webhook_*, event_*
  batches.py         # NEW: batch_*, poll_batch
  composites.py      # deep_research, news_roundup, company_dossier, merge_searches, diff_results, explain, magic
  dev.py             # github_repo, code_search, hf_*, dev_*, pkg_releases, deprecations, snippet, api_reference, is_deprecated, code_lint_tips, find_used_by, search_to_csv/jsonl

brave/src/brave/
  __init__.py        # thin facade
  errors.py          # BraveError (category) + _classify_response
  _http.py           # engine + _request + _fallback_ok + _run_fallback   (shared-mirror, with fallback)
  _keys.py           # (shared-mirror)
  _async.py          # (shared-mirror)
  _view.py           # _slim_item/_agent_view + item normalizers (_web_item/_news_item/...)
  _domain.py         # brave host helpers (KEEP SEPARATE)
  search.py          # _search_full, search, paged, merge, research, batch, run, brief, probe, summarize_page, mosaic, structured, explain, modes
  verticals.py       # software, near, locations, place_search, place_detail, pois, news_*, movies, recipes, products, reddit, forums, github_*, cve_*, pkg_*, weather, stock_quote, crypto, definition, currency_x, convert_values, unix_time, ...
  rich.py            # _rich_* + rich
```

`__init__.py` facade:
```python
from exa.errors import *            # noqa
from exa.models import *            # noqa
from exa.search import *            # noqa
... (all domain modules)
from exa import _async
_async.apply_async_to_all(globals()) # wrap the module's public callables, once
```
The async-wrap MUST only wrap this module's re-exported public callables (fix finding C1-[15]:
today it wraps imported names too). Implementation: wrap a known `__all__` list, not all FunctionTypes.

**Mirror-sync test** (`tests/test_shared_mirrors.py`): for each of `_http`, `_keys`, `_async`,
assert `sha256(exa.<mod>) == sha256(brave.<mod>)` after stripping the module-specific
`_request`/fallback differences (or compare the common core lines). This is the "shared _common"
goal achieved while preserving self-containment.

## 2. Defect fixes (map to consolidated ids) — surgical
All 26 confirmed (A1/A2/A3) + shared-block fixes (C1/C2/C3) are applied in Phase 3. Each edit is
verified against its finding + covered by a regression test where the behavior is unit-testable.

Return-key decision table (where code vs docstring diverged):
- exa top_terms `{"domain","results"}`: **emit `results`** (docstring wins; add `count` alias).
- exa monitor_check: **add `latest_run`** key (documented; keep existing keys).
- exa webset_eval_review: **rename return keys to `item_count`/`by_satisfied`** (docstring wins) — but
  keep old keys too for back-compat.
- exa monitor_create: **pass `timeout` through** (dead kwarg).
- exa agent(): run fail-check BEFORE terminal early-return; honor wall-clock budget; document/keep
  `stream` param on answer().
- exa company_dossier: do NOT trigger fetch backward-compat string path for internal callers.
- exa deprecations(): substring stems, no `\b` on stems.
- exa _get_api_key(): raise ExaAuthError (not RuntimeError) when no key.
- exa _request(): catch transport exceptions→ExaError; guard 2xx json; retry ONLY idempotent GETs.
- exa answer(): `raw.get("citations") or []`.
- brave research(): emit `summary_deep_link` (add `summary_dead_link` alias).
- brave _summarizer(): fix double-`key=` deep_link in except branch.
- brave open_now(): `if now is None:` guard.
- brave drinks(): use host field `site` not `domain`.
- brave software(): align code→docstring shape (document exact keys).
- brave rich(): always include `render`/`raw` (None when absent).
- brave _request/_run_fallback(): never return None; raise BraveError on fallback failure;
  simplify the line-1041 predicate.
- SHARED _async: kill dead `__await__` guard; make `__await__` uniformly return the plain value;
  `_AsyncBool` stays int-compatible but documented; None → return None (not _AsyncScalar(None));
  wrap only module-defined public fns.
- SHARED _keys: invalidate cache on env-var change; don't os.environ.setdefault side-effect; parse
  .env files properly.
- SHARED _http: guard Client.close; lock lazy-init.

## 3. Feature-adds (Phase 3, maximal)
- Exa `batches.py`: `batch_create`, `batch_get`, `batch_cancel`, `poll_batch` against `/batches`.
- Exa agent `stop` (`POST /agent/runs/{id}/stop`) + params `budget`/`effort`/`previousRunId` on agent().
- Brave: full `/res/v1/local/pois` (batch by ids), unit-conversion helper, ensure all 4 verticals
  parameterized; place_search coordinate-intent.
All new public functions go into `__all__` + are async-wrapped.

## 4. Test suite (Phase 3) — offline, no network
- Keep the 6 existing tests (they must keep passing).
- `tests/test_defect_regressions.py`: one test per confirmed defect that is unit-testable (monkeypatch
  `exa._request` / `brave._search_full`).
- `tests/test_coverage_gaps.py`: the C5 list (exa.search/run/to_agent, exa.answer/agent/agent_structured,
  exa.fetch shape switch, brave.merge, brave.probe/summarize_page, brave.rich verticals, brave.place_search).
- `tests/test_async_wrappers.py`: await returns plain value; None stays None; re-wrap safe; only public
  fns wrapped.
- `tests/test_shared_mirrors.py`: hash-sync of _http/_keys/_async between exa and brave.
- `tests/test_surface.py`: smoke — every `__all__` name is callable/importable + signature matches
  phase1_signatures.json.
Run: `PYTHONPATH=exa/src:brave/src python -m unittest discover -s tests -v` (baseline command).

## 5. MCP servers (Phase 5) — `mcp/exa`, `mcp/brave` (TypeScript, Bun)
Mirror Linear MCP quality (`/home/ks/repos/flow-test-generator/mcp/linear/`):
RateLimiter (enqueue/queue/trace/batch/trackRequest/getMetrics) + `addMetricsToResponse` +
typed tool const objects (rich `description` + `inputSchema`) + `resourceTemplates` +
`serverPrompt` (name/description/instructions) + `MCPMetricsResponse` + `main()` wiring
`Server` (capabilities: prompts/resources/tools) + `StdioServerTransport` + request handlers for
ListTools/CallTool/ListResources/ReadResource/ListPrompts/GetPrompt/ListResourceTemplates.
- Talk DIRECTLY to REST: Exa `https://api.exa.ai` (`x-api-key`), Brave `https://api.search.brave.com`
  (`X-Subscription-Token`). Use `Bun` (per global CLAUDE.md): `bun test`, no express; plain fetch.
- Tool coverage = "All of it": expose the broadest useful subset of the Python client (search/
  fetch/answer/agent/monitors/websets/batches for exa; web/news/video/image/local/pois/rich/
  unit-conversion for brave).
- Deps: `@modelcontextprotocol/sdk` (align to the version linear used, 0.6.0), `dotenv`; Bun runtime.
- package.json `type: module`; scripts build/watch/inspector; README with install + usage + dev.

## 6. Build order
1. Phase 3a — shared plumbing submodules (_async/_keys/_http) + shared defects + facade + mirror test.
2. Phase 3b — in-place defect fixes + regression tests.
3. Phase 3c — domain submodule split + surface test.
4. Phase 3d — feature-adds + coverage-gap tests. (run full suite after each)
5. Phase 4 — maximal live verification against real Exa + Brave APIs; fix contract drift; record baselines.
6. Phase 5 — MCP servers (exa, brave) + bun test smoke.
7. Phase 6 — adversarial review; update SKILL.md/references/README; finalize.
