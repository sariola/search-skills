# Phase 1 — Analysis notes (2026-09-11)

Consolidated from: 30-agent defect-audit workflow (wf_b75cbf45-235 + wf_f5b90e34-047),
manual OpenAPI ground-truth extraction (api.exa.ai/openapi.json v2.0.0, saved at
`.analysis/exa_openapi.json`), and live smoke probes with the CURRENT clients.

## A. Defect inventory (raw, needs verification)

- `.analysis/phase1_all.json` — 109 candidate defects from 14 section audits
  (1 critical, 8 high, 38 medium, 62 low by raw count).
- `.analysis/phase1_to_verify.json` — the 31 candidates selected for adversarial
  verification (all critical/high + structural + contract-shape medium).
- Global cross-cutting lenses (duplicates / shared-state / async-wrappers /
  auth-secrets / http-retry / test-gaps / shared-code) ran in wf_f5b90e34-047.
- CAVEAT: the first workflow's rollup (511 defects, 43 critical) was NOT
  reproducible on disk and contained at least one FALSE POSITIVE
  ("_AsyncDict.__await__ mutates the original dict" — actually `return dict(self)`,
  a copy, exa __init__.py:144-147). Severities are inflated; every finding must be
  verified against source before it drives a fix.

## B. OpenAPI contract drift (verified against live spec)

Spec: OpenAPI 3.1.0, "Exa Public API" v2.0.0. Full copy: `.analysis/exa_openapi.json`.

1. `/search` `type` enum: `instant, fast, auto, deep-lite, deep, deep-reasoning`.
   Client `SEARCH_TYPES` (exa __init__.py:354-357) additionally claims
   `keyword, neural, hybrid, magic`. `magic` is mapped to `deep` client-side, but
   `keyword/neural/hybrid` are sent verbatim and would 400 per current spec.
   (Spec may still accept them — verify live in Phase 4.)
2. `/search` `category` enum: `company, publication, news, personal site,
   financial report, people`. Spec says "other strings are accepted and used as
   category hints" — so client's extra tokens (github/code/research paper/
   linkedin/person) are probably still tolerated, but the doc comments at
   exa __init__.py:341-348 claim a closed set.
3. `autoprompt` is NOT a SearchRequest property in the current spec;
   `search(use_autoprompt=True)` sends `body["autoprompt"]=True`. The comment at
   :349-350 says "live-verified" — re-verify in Phase 4 (accepted-but-inert vs 400).
4. `numResults`: 1..100, default 10 (client validates 1..100 — correct).
5. `contents.text.verbosity` enum: `compact, standard, full` (client passes through
   — fine). `contents.highlights.verbosity`: `low, medium, high`.
   `contents.summary` takes only `query`/`schema` (client matches).
   `ContentsOptions` properties: context, extras, highlights, livecrawl,
   livecrawlTimeout, maxAgeHours, subpageTarget, subpages, summary, text.
   NOTE: spec has no `includeSections`/`excludeSections`/`includeHtmlTags` at the
   text-object level beyond `includeHtmlTags` — verify sections params live.
6. `SearchResponse` now marks `resolvedSearchType` and `context` as DEPRECATED
   ("current production responses may return an empty string; clients should not
   branch on this value"). Live probe CONFIRMED: resolved_search_type == "" and
   `context` absent. Client still surfaces both in to_meta() (harmless but docs
   should say deprecated).
7. `AnswerResponse` props: answer, citations, costDollars, requestId (client
   matches). `/answer` request accepts `model`, `outputSchema`, `query`, `stream`,
   `systemPrompt`, `text`, `userLocation` — client's `citationFormat` param is NOT
   in the spec (undocumented passthrough; comment at :1918 admits it).
   Live probe: answer() works, returns 8 citations.
8. Endpoints the client does NOT wrap (spec has them):
   - `POST /batches`, `GET /batches/{id}`, `POST /batches/{id}/cancel` —
     bulk batched search/contents runs (CreateBatchRequest{metadata, requests};
     BatchRequestItem{method,url,body,customId}). NEW FEATURE CANDIDATE.
   - `POST /agent/runs/{id}/stop` — complete a running agent run early
     (requires Exa-Beta header + max effort). NEW FEATURE CANDIDATE.
   - `GET /v0/websets/{webset}/searches/{id}` (cancel exists; status via
     webset_search_status — check it hits /searches/{id} not /searches).
9. AgentRunRequest props: budget, dataSources, effort, input, metadata,
   outputSchema, previousRunId, query, systemPrompt — client's agent() covers
   query/system_prompt/output_schema/data_sources; `budget`, `effort`,
   `previousRunId` are unwrapped (NEW FEATURE CANDIDATES).

## C. Live baseline (current clients, keys work)

- exa.search "python asyncio TaskGroup" → 3 results, cost $0.007, resolved="".
- exa.search mode=paper + with_highlights → works, highlights present.
- exa.fetch(urls=[...], include_meta=True) → dict envelope {results, rendered,
  request_id, cost_dollars, search_time_ms}; result status success, text 499 chars.
- exa.answer("capital of France?") → "Paris [1]", 8 citations.
- brave.search agent view → keys {infobox, mode, query, results, videos};
  result keys {extra, kind, live, snippet, title, url}.
- brave.search mode=news → keys {age_days, date, extra, publisher, snippet,
  title, url}.
- brave.probe(url, max_chars=500) → keys {chars, content_type, final_url, status,
  text, title, truncated, url}; NOTE: NO 'ok' key in success shape (SKILL.md
  references probe success semantics — check docstring/contract).

## D. Confirmed real defects (verified against source by hand)

1. BRAVE _summarizer: malformed deep link at :1779 — `"https://search.brave.com/
   summarizer?key=" + urlencode({"key": key})` → `...summarizer?key=key=...`.
   (:1776 above it has the CORRECT form; :1779 is the buggy branch.)
2. EXA: `pkg_releases`, `_dep_type`, `deprecations`, `snippet`, `code_lint_tips`
   each defined TWICE in exa __init__.py (~5459-5705 and again ~5705-5894).
   Last def wins; first defs are dead code. Must confirm bodies are identical
   before deleting; if divergent, keep the live (last) one.
3. EXA version drift: `__version__ = "0.19.0"` (code) vs pyproject `0.20.0`.
4. EXA `_request` retry loop (lines 506-553): the fallthrough after the for-loop
   (line 553 "unreachable") is actually reachable — if the last attempt is
   transient (429/5xx) it hits `continue`? No: `if status in (...) and attempt <
   _retries: ... continue`; on the LAST attempt with a transient status it falls
   to the status-429/5xx classification branches, which ARE reachable. Verify:
   429 after retries → ExaRateLimitError (correct); 5xx after retries → the
   `raise ExaServerError` catch-all (line 548) is reached before the loop-end
   "unreachable" raise. The :553 raise is genuinely unreachable ONLY if every
   non-<400 path either returns or raises inside the loop — TRUE for 400/401/403/
   404/422/429, and for 5xx it falls through the retry check to... line 548
   `raise ExaServerError` — wait, that raise sits AFTER the 429 block unconditionally
   for any remaining status. So loop-end is unreachable. Low-priority; leave.
5. EXA `fetch()` legacy string-mode guard (line 1823-1824) checks only
   `not extras_links and max_age_hours is None` — ignores other rich options
   (extras_image_links, subpages, with_text+max_characters interplay). Contract
   drift, medium.
6. EXA `to_agent()` uses `if cost:` so cost==0.0 is dropped (line ~1129).
7. EXA `_read_key_file` bare-token heuristic: any line >=12 chars with no '/'
   is returned as the key even if the file contains unrelated long text
   (both modules). Medium.
8. EXA `entity_search(dedupe=...)` param accepted but never read (:826).
9. EXA `top_terms` docstring promises `{"domain","results"}` but returns
   `{"domain","count"}` (:1024).
10. BRAVE `_request` operator-precedence: `retryable or not fast_fail and
    fallback is not None` (:1041) → `retryable or ((not fast_fail) and (fallback
    is not None))`. Probably intended, but the `fallback and ... and fallback is
    not None` is redundant/odd. Verify intent before touching.
11. BRAVE `_request`: `if isinstance(data, dict) or data is not None:` (:1015)
    is a tautology; intended logic was likely "return dict, else treat 200 as
    transient". As written: any non-None JSON (incl. a list) returns; only
    JSON-decode failure hits the transient path. Medium.
12. EXA `answer()` forwards `stream` into the body but routes through
    `_request` (non-streaming) — `stream=True` from answer() is silently a
    lie (medium; the streaming variants are stream_answer*).
13. EXA `agent()` early-return `if run.done: return run` skips
    `raise_on_failed` check when the run is already terminal (medium).

## E. Feature-add candidates (from spec diff)

- exa: `batches` (POST /batches + get + cancel + poll), `agent_stop`
  (POST /agent/runs/{id}/stop), agent() `budget`/`effort`/`previous_run_id`
  params, possibly `context` param on answer/search.
- brave: TBD — no public OpenAPI spec bundled; use the API reference docs
  (developer.brave.com/search/web) to check for endpoints the client misses
  (e.g. /res/local/place context endpoints, /v1/goggles). Verify live.

## F. Design inputs for Phase 2

- Shared internal module `_common` candidates (from shared-code lens, pending
  wf_f5b90e34-047 confirmation): httpx engine (client/pool/retry-after/fanout),
  dual sync/async wrapper set, api-key lookup trio, domain helpers.
- Public API must stay backward-compatible: same top-level function names and
  signatures in `exa` and `brave` namespaces (skills + tests depend on them).
- Tests: keep `tests/test_search_contracts.py` passing; add focused offline
  tests for the contracts enumerated in the test-gaps lens.
- MCP servers: new dirs `mcp/exa` and `mcp/brave`, TS, mirror
  `~/repos/flow-test-generator/mcp/linear` structure: RateLimiter class,
  metrics-in-response metadata, typed tool definitions with rich descriptions,
  resources + templates, a server prompt with tool-usage + best practices,
  stdio transport, dotenv, tsconfig + package.json + README. Exa talks to
  api.exa.ai REST; brave to api.search.brave.com REST.
