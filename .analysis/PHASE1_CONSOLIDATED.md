# PHASE 1 — CONSOLIDATED (single source of truth for Phase 2 design)

Date: 2026-09-11. Scope: maximal (user directive). All findings below were **adversarially
verified against real source** by the `verify-defect-findings` workflow (one skeptic per
candidate, "default to refutation") plus 7 cross-cutting global lenses. Severities from the
raw rollup are INFLATED — only items here are trusted. Anything NOT listed was refuted or is
below the bar.

Ground-truth files: `exa_openapi.json` (Exa Public API v2.0.0 OpenAPI 3.1),
`phase1_verified.json` (31 verdicts), `phase1_globals_full.json` (38 lens findings),
`PHASE1_NOTES.md` (raw notes).

---

## A. CONFIRMED DEFECTS (26) — must fix in Phase 3

Key: [id] sev — module — one-line defect → fix. "EXA/BRAVE/SHARED".

### A1. Exa correctness / contract bugs
- **[5] HIGH exa** `deprecations()` gate regex `\b(deprecat|obsolete|removed|migrat|breaking|sunset)\b`
  word-boundary on stems → misses inflected forms (e.g. "removals", "migrated", "deprecation").
  Fix: strip boundaries / use substring stems `("deprecat","remov","migrat","obsolet","sunset","breaking")`.
- **[10] MED exa** unknown `mode` to `search()`/`stream_search()` raises built-in `ValueError`,
  not `ExaError`. Docstring promises module-wide `ExaError` for "invalid option combo".
  Fix: raise `ExaBadRequestError("unknown mode ...")`.
- **[13] MED exa** `Answer.__init__`: `raw.get("citations", [])` — if API returns `citations: null`,
  key present → `None`, not `[]`. Fix: `raw.get("citations") or []`.
- **[14] MED exa** `agent()`: when POST /agent/runs returns an already-terminal run, `if run.done:
  return run` short-circuits and SKIPS the `raise_on_fail` check → a run that failed immediately
  returns silently instead of raising. Fix: run the fail-check before the terminal early-return.
- **[15] MED exa** `agent()` wall-clock budget not as documented: POST create call runs BEFORE
  `deadline = time.time()+timeout`, so create time is added on top of the budget; each poll also
  ignores remaining budget. Fix: compute deadline before create; break/raise on `time.time()>deadline`.
- **[16] MED exa** `top_terms()` returns domain clusters as `{"domain","count"}` but docstring
  documents `{"domain","results"}`. Fix: emit `results` (or document `count`).
- **[17] MED exa** `entity_search(dedupe=True)` — `dedupe` accepted but never read; roster always
  deduped. Fix: honor flag or drop the param + docstring.
- **[18] MED exa** `monitor_create(timeout=45.0)` kwarg is DEAD — POST /monitors hardcodes
  `timeout=30`. Fix: pass `timeout` through.
- **[19] MED exa** `monitor_check()` documented to return `latest_run` but returns no such key in
  either branch. Fix: add `latest_run` key (the runs[0] dict) or fix docstring.
- **[20] MED exa** `monitor_check()` inspects ONLY `runs[0]`; if latest run's `output` is falsy it
  unconditionally triggers, ignoring older completed-with-output runs. Fix: scan all runs for an
  output before falling through to trigger.
- **[21] MED exa** `webset_eval_review` docstring promises keys `item_count`/`by_satisfied`;
  actual returns `count`/`by_satisfaction`. Fix: rename return keys or docstring.
- **[4] HIGH exa** `company_dossier()` default (fetch_website=True, max_age_hours=None): inner
  `fetch()` takes the backward-compat single-URL-string branch → returns a bare string
  ("--- Title ---\n<body>") instead of a structured fetch envelope. Fix: don't trigger the
  backward-compat branch for internal structured callers; keep envelope.
- **[11] MED exa** `answer()` has undocumented `stream: bool = False` kwarg (line ~1900) not in the
  Args docstring. Fix: document it or remove.
- **[12] MED exa** `fetch()` backward-compat string path fires on `len==1 & mode=="text" & no
  highlights/summary/extras` — surprising shape switch for callers. (Design: make explicit via
  opt-in flag; keep default structured.)
- **[24] MED exa** `stream_answer()` trailing meta dict: `cost_dollars` value inconsistent with the
  non-stream `Answer.cost` (verify exact key/value and align).
- **[23] LOW exa** `_get_api_key()` raises bare `RuntimeError` when no key found, but `ExaAuthError`
  docstring says it covers "key missing/invalid/revoked". Fix: raise `ExaAuthError`.

### A2. Brave correctness / contract bugs
- **[0] HIGH brave** `_request` fallback gate at line 1041
  `if fallback and resp.status_code != 200 and (retryable or not fast_fail and fallback is not None)`
  — after the retry/raise block, a non-200 that is retryable-or-not-fastfail routes to
  `_run_fallback` which can return None (see A3). Also `fallback is not None` is redundant (already
  guarded by leading `fallback and`). Fix: simplify predicate + guard None return.
- **[20-global] HIGH brave** `_run_fallback` (lines 1059-1075) returns None when: fallback GET
  raises, alt.status_code != 200, or alt.json() fails. `_request` then does `return _run_fallback(...)`
  → caller gets None → downstream `.get`/attribute access → **AttributeError**. Fix: raise
  `BraveError` on fallback failure; never return None from a request path.
- **[1] MED brave** `_summarizer` deep_link: `except Exception` branch at line 1779 builds
  `"https://search.brave.com/summarizer?key=" + _up.urlencode({"key": key})` → produces
  `...summarizer?key=key=...` (double key). The non-exception branch (line ~1776) is the correct
  form. Fix: use the same correct form in both branches.
- **[7] MED brave** `open_now(row, *, now=None)` — line ~3481 unconditionally reassigns
  `now = datetime.now(tz) if tz else datetime.now()`, clobbering a caller-supplied `now`.
  Fix: `if now is None: now = ...`.
- **[8] MED brave** `research()` returns the AI-summary deep-link under key `summary_dead_link`
  (line ~2345) but its public Returns contract documents `summary_deep_link`. Fix: emit
  `summary_deep_link` (add `summary_dead_link` alias for back-compat if needed).
- **[28] MED brave** `drinks()`: web items are `_web_item`-normalized (host field is `site`, not
  `domain`), so `r.get("domain")` is always None and the `dom` filter clause is always False.
  Fix: use `_host(r)` / `r.get("site")`.
- **[29] MED brave** `software()` returns key `registry` (singular, a `{registry_name->[names]}` map)
  and `versions` as `[{name,version,url,code}]`, but docstring documents `registries` (plural) and
  `versions` as `[version str...]`. Fix: align code to docstring (or vice versa) — pick the shape
  downstream/skill docs expect.
- **[26] MED brave** `rich()` returns two different shapes: fetch path returns
  `type/query/hint/callback_key/vertical/results/render/raw`; early-return path (no callback_key or
  fetch=False) omits BOTH `render` and `raw`. Fix: always include `render`/`raw` (None when absent).
- **[30] MED brave** `research()`: per-query `added` counter `n` only increments after cross-query
  dedup passes, so reported counts understate matches. (Design: decide semantics — count candidates
  vs unique; document.)

### A3. Exa duplicated / shadowed definitions (delete dead first copies, keep live last)
- **[22] LOW exa** five module-level functions defined twice (last def wins). Delete the first copies,
  keep the live ones:
  - `pkg_releases` 5459 (dead) / **5705 (LIVE)** — byte-identical
  - `_dep_type` 5507 (dead) / **5753 (LIVE)** — byte-identical, private
  - `deprecations` 5522 (dead, DRIFTED: has extra comment + var `start`) / **5768 (LIVE)**
  - `snippet` 5581 (dead) / **5826 (LIVE)** — byte-identical, public
  - `code_lint_tips` 5672 (dead) / **5870 (LIVE)** — byte-identical, public
  The 'Round-18' block was pasted twice (headers at 5451 and 5697).

---

## B. REFUTED — do NOT chase (5)
- **#2** brave `_request` retry/raise block IS reachable (via unparseable/`null` 200 body). Not a defect.
- **#3** brave line-1041 precedence: `and > or` parse is correct but the consequence is HARMLESS
  (first conjunct already gates it). The real issue is the None-return (#0/#20-global), not the precedence.
- **#9** exa `webset_items_all` terminates correctly (breaks on hasMore=false / no nextCursor). Not infinite.
- **#25** brave `_rich_definitions` maps upstream `pronounciation` → normalized `pronunciation`;
  downstream reads `pronunciation`. Intentional normalization, not a bug.
- **#27** brave typed helpers (weather/stock_quote/...) correctly delegate to `rich()` and return the
  full package. Not a defect.

---

## C. CROSS-CUTTING / ARCHITECTURAL FINDINGS (38, global lenses)
Only the HIGH + decision-relevant items; LOW thread-race items are noted as "acceptable, document."

### C1. Dual sync/async result-wrapper system (SHARED, both modules, ~200 lines each, BYTE-IDENTICAL)
exa `__init__.py` 133-334 ≡ brave `__init__.py` 522-723. Contains `_identity_await`,
`_AsyncDict/_AsyncStr/_AsyncList/_AsyncInt/_AsyncFloat/_AsyncBool/_AsyncScalar`,
`_wrap_result/_make_async/_apply_async_to`. **Every public function's return is wrapped.** This is
the root of several subtle bugs:
- **[11] HIGH both** `hasattr(value,"__await__"): return value` guard in `_wrap_result` is DEAD CODE —
  the isinstance checks for the subclassed `_Async*` types catch everything first.
- **[12] HIGH both** `__await__` returns inconsistent kinds: containers/primitives return a COPY
  (`return dict(self)` etc.), while `_AsyncScalar` + result classes (via `_identity_await`) return
  the LIVE object. Await-vs-sync value can differ by identity. Fix: make the contract uniform —
  awaiting any wrapper yields the plain underlying value.
- **[13] LOW both** `_AsyncBool` subclasses `int` (not `bool`), so `isinstance(x, bool)` is False;
  re-wrapping an `_AsyncBool` falls into the `int` branch.
- **[16] MED both** `None` return becomes `_AsyncScalar(None)`; sync caller gets the proxy, not None
  (`result is None` is False). Contract drift.
- **[14] LOW both** `__name__ == "_async_aware"` filter in `_apply_async_to` is dead code (wraps copy
  the original name).
- **[15] MED both** `_apply_async_to(globals())` wraps EVERY `FunctionType`, including IMPORTED
  names (e.g. `parsedate_to_datetime` from `email.utils`) — accidental public surface + surprising
  async-wrapping of foreign functions. Fix: wrap only module-defined public functions.
**DECISION (feeds Phase 2):** consolidate this block into a shared `search_skills/_common/asyncwrappers.py`
(single copy, both packages import it), and FIX items [11],[12],[13],[16] + restrict [15] to
module-local defs. Preserve the public "functions are awaitable" contract.

### C2. API-key / secrets (SHARED trio byte-identical: exa 411-488 ≡ brave 890-967)
`_seek_api_key` / `_read_key_file` / `_get_api_key`.
- **[17] HIGH both** `_API_KEY_CACHE` written once per label and NEVER invalidated → a key rotation /
  env change mid-process is silently ignored for the process lifetime.
  Fix: cache only when a stable source (env var) is set; or add a re-validate-on-miss hook.
- **[6] MED both** same root cause — first-resolved key cached under `_API_LABEL`, later calls never
  re-read env.
- **[7/9] LOW-MED both** unsynchronized check-then-act on `_API_KEY_CACHE` (benign under CPython GIL,
  but document).
- **[18] MED both** key-store `.env`-style files parsed by naive `_read_key_file`, never `python-dotenv`
  → multi-line / quoted / inline-comment values break. Fix: parse `.env` files with a proper parser.
- **[19] MED both** `_get_api_key` mutates `os.environ` via `os.environ.setdefault(n, key)` as a side
  effect of a read. Fix: don't write to environ; keep resolution local.

### C3. HTTP engine (SHARED core: exa 26-79 ≡ brave 23-80, trivial renames)
module globals `_CLIENT` (httpx.Client, keep-alive), `_pool`/`_WORKERS` (process-lifetime
ThreadPoolExecutor), `_retry_after_seconds`, `_fanout`.
- **[10] MED both** docstring claims the shared Client is "thread-safe for request()" — httpx Client
  is safe for concurrent `.request()` but NOT for concurrent `.close()`. Document + ensure close is
  guarded.
- **[5/6/8/23] LOW both** lazy-init of `_CLIENT`/`_WORKERS` is unsynchronized check-then-set (first-use
  race leaks a 2nd client/executor). Benign under GIL; add a lock or document.
- **[21] MED exa** `exa._request` does NOT catch transport-level httpx exceptions (ConnectError/
  ReadTimeout/PoolTimeout) → they propagate as raw httpx, not `ExaError`. Fix: wrap and re-raise.
- **[22] MED exa** non-idempotent stateful requests (POST /agent/runs, POST /monitors, ...) are
  blindly re-sent on 429/5xx → double-charged agent compute / duplicate monitors.
  Fix: retry ONLY idempotent GETs (or honor an `idempotent` flag); never auto-retry side-effect POSTs.
- **[24] LOW exa** success-path `resp.json()` unguarded → a 2xx with empty/non-JSON body raises raw
  `JSONDecodeError`. Fix: guard json parse, raise `ExaError`.

### C4. Domain/URL helpers — DO NOT merge (divergent, not dup)
- **[37] LOW both** exa `_domain_of` (registrable domain, strips last-two-labels) vs brave host helpers
  are conceptually overlapping but DIVERGENT. Keep separate; a refactor must not wrongly merge them.

### C5. TEST COVERAGE GAPS (all "medium/low", drive the Phase-3 offline test suite)
Existing suite: `tests/test_search_contracts.py` — 6 tests pass (brave.paged pagination, compact vs
full view, exa fetch failed-URL envelope, exa webset yes-filter).
NOT covered (add offline tests, monkeypatch `exa._request` / `brave._search_full`):
- [26] exa.search / run / SearchResults.to_agent cost + grounding
- [27] exa.answer / agent / agent_structured cost + grounding + structured-guard
- [28] exa.fetch string-vs-list return-shape switch (incl. one-element→string + rich-option override)
- [29] brave.merge URL dedupe + per-source `_query` tagging
- [30] brave.probe / summarize_page success semantics
- [31] brave.rich verticals (hint-then-fetch + per-vertical normalization)
- [32] brave.place_search resolved-anchor + coordinate-intent + validation guards
- [33] large tail of documented public helpers (SKILL.md/references surface) — add at least a
  signature + no-crash smoke pass over the whole public surface.

---

## D. API SURFACE (ground truth for feature-add + MCP tool inventory)

### Exa (api.exa.ai, auth `x-api-key`; OpenAPI "Exa Public API" v2.0.0)
Core: `POST /search`, `POST /contents`, `POST /answer`, `POST /findSimilar`.
Agent: `POST /agent/runs`, `GET /agent/runs/{id}`, `POST /agent/runs/{id}/cancel`,
`GET /agent/runs/{id}/events`, `POST /agent/runs/{id}/stop`.
Monitors: `POST /monitors`, `GET/DELETE /monitors/{id}`, runs/trigger.
Batches: `/batches` (create/get/cancel/poll) — **feature-add candidate**.
Websets: `/v0/websets`, items, eval-review, enrichment. Webhooks `/v0/webhooks`,
imports `/v0/imports`, events `/v0/events`, team `/v0/teams/me`.
Base for websets/monitors/webhooks/etc. = `https://api.exa.ai/websets` (client calls `/v0/...` under it).
**agent() feature-add candidates:** `budget`, `effort`, `previousRunId` params; `/agent/runs/{id}/stop`.

### Brave (api.search.brave.com, auth `X-Subscription-Token`)
`/res/v1/web/search`, `/res/v1/news/search`, `/res/v1/videos/search`, `/res/v1/images/search`,
`/res/v1/local/pois` (up to 20 `ids`), unit-conversion endpoint.
Supports `country`, `search_lang`, `spellcheck`, `count`, `offset`, `freshness`, `safesearch`,
`units` (metric/imperial). "fallback endpoint" pattern (local → web). Summarizer via
`search.brave.com/summarizer?key=...` (callback_key).

---

## E. SHARED-CODE REFACTOR TARGETS (feed Phase 2 layout)
1. **`_common/asyncwrappers.py`** — the byte-identical ~200-line dual sync/async set (fix [11,12,13,16,15]).
2. **`_common/keys.py`** — the byte-identical ~80-line API-key trio (fix [17,6,18,19]).
3. **`_common/httpengine.py`** — the byte-identical HTTP core: `_CLIENT`/`_pool`/`_retry_after_seconds`/
   `_fanout` (fix [21,22,24,10,5/6/8/23]).
4. **Domain/url helpers stay SEPARATE per module** (C4) — do not merge.

## F. FEATURE-ADD CANDIDATES (Phase 3, "maximal")
- Exa: `/batches` (create/get/cancel/poll); `/agent/runs/{id}/stop`; `agent()` params
  `budget`/`effort`/`previousRunId`.
- Brave: ensure `/res/v1/local/pois` (batch by ids), unit-conversion, all 4 verticals fully
  parameterized; `place_search` coordinate-intent.

## G. PHASE-2 DESIGN INPUTS (checklist)
- Preserve public API compat: every public function stays awaitable; error classes kept; return-key
  names stabilized (prefer FIXING code to docstring where skill docs already reference the code shape).
- New package layout: `exa/{errors,http,keys,models,search,contents,answer,agent,monitors,websets,
  batches,streaming,composites,dev}` + shared `_common`. `brave/` analogous.
- Decide: backward-compat fetch string-path → opt-in flag; monitor_check `latest_run` semantics;
  research `added` count semantics; rich() always-include render/raw.
- Offline test suite must cover C5 list + every confirmed defect's regression.
