---
name: exa
description: >-
  Advanced Exa search/api.exa.ai skill, agent-friendly.
  search/run/fetch/answer(+citationFormat)/agent(+connects cost), SSE stream + agent_trace,
  monitors(+threshold), WebSets (CRUD/imports/webhooks/enrich/diff/recall/items_all/snapshot),
  semantic sections, content extras, to_markdown/dataframe/meta/json/csv/xlsx/jsonl, similar_to,
  agent_chat+schemas, entity_search/schema, news_roundup, company_dossier, diff_results, magic,
  explain, find_used_by. DEV utilities: github_repo, code_search, hf_models, hf_discussions,
  dev_help, dev_report, custom pkg_releases, deprecations, snippet, api_reference, is_deprecated,
  code_lint_tips. instant/deep/deep-reasoning types. hipa. Requires EXA_API_KEY.
---

# Exa Search (advanced modes) — v0.20

Semantic/neural, keyword, hybrid, deep and fast web search through the Exa
Search API (`https://api.exa.ai`) using the `EXA_API_KEY` environment variable.

Agent contract: prefer `await exa(...)` (markdown), `await exa.brief(q)`
(token-budgeted `{title,url,snippet,score}`), or `await exa.answer(q)`
(cited synthesis). `search()` stays structured; call `.to_agent()` before
stuffing results into a model turn. Engine: pooled HTTP + deadline polls
(no fixed-interval wait on agent/webset/monitor).
This module tracks the live OpenAPI spec (api.exa.ai/openapi.json): rich
per-result fields (image, favicon, summary, highlights, extras, entities),
content freshness controls, citation-grounded synthesized answers via
`output_schema`, dedicated **compact-answer** (`/answer`), **similar-page**
(`/findSimilar`), **fan-out research** (multi-query), **SSE streaming** of
answers, **scheduled monitors** (recurring change-detection), **agent-run
management**, and the **WebSets API** — bulk web-scale structured entity
discovery (people / companies / articles / research papers) with imports,
enrichments, webhooks and events.


## Everything works with and without `await` (dual sync/async)

Every public callable is now **both** synchronous and async-compatible, so you
may write either form and get the same result:

```python
res  = exa.search("clojure")      # sync: a real SearchResults
res  = await exa.search("clojure")  # async: the same SearchResults
text = await exa("clojure")          # readable markdown (run)
```

- `await fn(...)` yields the **plain** value (`dict`, `str`, `SearchResults`,
  `Answer`, `AgentRun`, ...) — nothing is left wrapped.
- `fn(...)` (no await) returns the same value usable directly; dicts/strings/
  lists stay dicts/strings/lists for `[]`, `.get()`, `isinstance`, `**`-unpack,
  iteration, etc., because they're thin built-in subclasses that are also
  awaitable.
- Internal cross-calls, `isinstance`, result methods, and the `stream_*`
  generators (`stream_answer`, `stream_search`, `stream_answer_source`) are all
  unaffected.

So an agent running inside IPython can freely `await exa.search(...)` or
`exa.search(...)` interchangeably.


## Auth

Key resolution is robust across scopes: the skill checks the process env,
then dotenv files (`~/.prime/agent/.env`, `~/.prime/.env`, `~/.config/prime/env`,
`$PRIME_DOTENV`, cwd `.env`) and finally the `/login` key store
(`~/.prime/agent/keys/exa.key`); whichever is found is exported back so the CLI
and subprocesses inherit it. It raises ONE clear, actionable message only if the
key is absent from every scope — never an unexplained "unconfigured" wall.

## Entry points

**Structured (primary, pipeline-friendly) — `exa.search(...)`**
Returns a `SearchResults` (usable like a list) of rich `Result` objects, each
with `.to_dict()`. The response also gives `to_dicts()`, `to_json()`,
`.answer` and `.answer_grounding`. Prefer this whenever you will post-process,
compare, or feed results downstream.

**Readable — `await exa(...)` (alias of `exa.run(...)`)**
Returns a formatted numbered list (title / URL / published date / author /
snippet). Good for answering the user directly. `run()` accepts the full
option set of `search()`.

**Compact answer — `exa.answer(query, output_schema=None, citation_format=None)`**
Uses the dedicated `/answer` endpoint to return a terse, citation-grounded answer
(`Answer.answer`) plus the source URLs it cited (`Answer.sources`).
Give it a natural-language question ("What is the latest valuation of
SpaceX?"), not a keyword search. Pass `output_schema` to get the answer back
as a structured object. Pass `citation_format` (v0.14, e.g. `{"id": True,
"favicon": True, "author": True, "publishedDate": True}`) to request rich
citation fields — the live `/answer` accepts the undocumented `citationFormat`,
populating `id/url/title/image/favicon/author/publishedDate` on each cited
source when the API knows them.

**Full agentic research — `exa.agent(query, ...)`** (v0.4 — the most powerful surface)
Runs Exa's `/agent/runs` workflow: the agent reasons over the web, pulls
evidence, and writes a **grounded** answer — optionally structured to
`output_schema` — with per-field citations, usage, and cost. Use it when you
want a self-contained `answer + sources` in one shot instead of a ranked list.

```python
run = exa.agent(
    "What are recent AI-infrastructure Series A rounds, with amounts?",
    output_schema={"type":"object",
                   "properties":{"companies":{"type":"array","items":{
                       "type":"object","properties":{"name":{"type":"string"},
                                                     "round":{"type":"string"}}}}},
                   "required":["companies"]},
    effort="minimal")            # auto|low|medium|high|xhigh|minimal
run.text          # natural-language summary (when no/minimal schema)
run.structured    # dict matching output_schema (None if none given)
run.citations     # unique (url,title,field) sources cited
run.grounding     # per-field citation clusters + confidence
run.cost          # total USD the run spent (compute + searches)
run.usage         # agentComputeUnits / searches / emails used
```
`await exa.agent_structured(query, schema)` is a convenience that raises if no
structured output came back. Supports `system_prompt`, `previous_run_id`
(continue a prior run), and `data`/`exclusion` records to process or avoid.
`data_sources=["fiber", "financial_datasets", ...]` (v0.6) enables **Exa Connect**
providers (max 5: fiber, financial_datasets, similarweb, baselayer, affiliate,
particle, jinko). Effort `minimal` starts the fastest; default `timeout` is 120s.


**Agent-run management** (v0.5)
Prior agent runs can be listed / inspected / cancelled / deleted:
- `exa.agent_list(status=..., limit=..)` — reverse-chronological history with usage+cost.
- `exa.agent_get(run_id)` / `exa.agent_delete(run_id)` / `exa.agent_cancel(run_id)` /
  `exa.agent_events(run_id)` (run.status.* event list).

**Streaming answers — `exa.stream_answer(query, ..., citation_format=..., collect_meta=True)`** (v0.5, v0.8, v0.14 upgrades)
SSE-streams a citation-grounded answer token-by-token (like a chat). Yields
incremental content strings, then — with `collect_meta=True` (v0.8, default) —
one trailing dict with the full `citations`, `cost_dollars`, and `request_id`.
Good for "typewriter" UX that still wants the sources. Use non-stream
`answer()` when you need an `Answer` object directly.
`exa.stream_search(query, output_schema=...)` streams `/search` events
incrementally AND (v0.8) returns a final rich dict of `{"results", "output",
"grounding", "citations", "text", "request_id"}` — it does emit progressive
content and now surfaces the field-level `grounding` citations (verified live).

**Scheduled monitors — `exa.monitor_*`** (v0.5, api.exa.ai/monitors; v0.6 adds
content + filter options)
Recurring searches that detect what changed / is new on watched pages.
```
exa.monitor_create("Cloudflare pricing 2025", period="6h",
                   webhook_url="https://...", name="cf-pricing",
                   filter_empty_results=True, with_text=True, max_characters=500)
exa.monitor_list(status="active")                  # filter by status/name/metadata
exa.monitor_trigger(id)                            # immediate run
exa.monitor_runs(id) / exa.monitor_run_get(id, run_id)  # completed output
exa.monitor_wait(id, run_id=...)                   # poll until terminal
exa.monitor_update(id, status="paused")             # pause / change search / webhook
exa.monitor_delete(id)
exa.monitor_batch("pause", name="cf-")             # bulk pause/delete/unpause by filter
```

**Webset monitors — `exa.wmonitor_*`** (v0.6, api.exa.ai/v0/monitors)
Keep a webset fresh on a **cron schedule** (distinct from the search-change
`monitor_*` API — these attach to a *webset* and run periodic search/refresh
operations). The schedule is a 5-field Unix cron + IANA timezone, at most once
per day.
```
exa.wmonitor_create("webset_id", cron="0 9 * * 1",
                    timezone="America/New_York", count=10,
                    query="new AI infra startups", behavior="append")
exa.wmonitor_list(webset_id="..")                # list monitors (optionally by webset)
exa.wmonitor_get(id) / exa.wmonitor_update(id, status="disabled")  # control
exa.wmonitor_delete(id)                          # remove permanently
exa.wmonitor_runs(id) / exa.wmonitor_run_get(id, run_id)   # run history
```

**WebSets — bulk structured entity discovery** (v0.5, api.exa.ai/websets)
The WebSets product finds *records*, not pages: people / companies / articles /
research papers matching a natural-language query, with rich structured profiles
(name, company, location, work history / employees, industry, headcount,
financials, web traffic...). This is the surface behind Exa's lead-gen /
enrichment workflows.
```
preview = exa.webset_preview("US marketing agencies that focus on consumer products")
# -> {search: {entity: {type: "company"}, criteria: [...]}}   # iterate on the query first

ws = exa.webset_create("US AI startups that raised Series A in 2024", count=10,
                     recall=True, enrichments=[{"description": "Founder email list"}])
exa.webset_wait(ws["id"])                      # poll until search+enrichments done
items = exa.webset_items(ws["id"])            # {"data": [person/company/...records]}
exa.webset_get(ws["id"]) / exa.webset_list(search="AI") / exa.webset_delete(id) / exa.webset_cancel(id)
# add another search scoped to the same webset:
exa.webset_add_search(ws["id"], "san francisco AI founders", count=20,
                      recall=True, behavior="append")
# enrich every item with a custom field:
en = exa.webset_enrich(ws["id"], "Find the primary contact email address")
exa.enrichment_get(ws["id"], en["id"])
exa.enrichment_update(ws["id"], en["id"], format="email", description="Validated contact email")
exa.import_update(import_id, title="2024 rounds", metadata={"team": "sales"})
```
Each item is a structured record: person (name/position/company/linkedin/work-
history/picture) or company (name/industry/employees/headquarters/founded-year/
financials/web-traffic). See `references/websets.md`.

- **Imports** — seed/scope websets from your own CSV of URLs:
  `exa.import_create("companies.csv", title=..., entity={"type":"company"})`;
  `exa.imports_list()` / `exa.import_get(id)` / `exa.import_delete(id)`.
- **Webhooks** — receive webset/import events on your endpoint:
  `exa.webhook_create(events, url, metadata=...)`, `exa.webhook_list()`,
  `exa.webhook_update(id, events=...)`, `exa.webhook_delete(id)`,
  `exa.webhook_attempts(id)`.
- **Events / team** — `exa.event_list(limit=..)`, `exa.event_get(id)` (audit log);
  `exa.team_info()` (team id/name + concurrency `maxConcurrent`/`maxQueued`).


**Similar pages — `exa.find_similar(url, ...)`**
Given one strong URL (or earlier result `id`), return other pages covering the
same ground. Great for broadening a single good source into a cluster of
related papers/articles.

**Fan-out research — `exa.deep_research([q1, q2, ...], num_results=..)`,**
Runs several related sub-questions, merges results by URL, and ranks pages that
matched more than one query to the top. Give it the facets of a research
question and it returns one consolidated, source-dense `SearchResults`.

**Page content — `exa.fetch(urls|ids, ...)`**
Full page text / highlights / summary / extras for one or many URLs (or ids).

## Modes (`mode=`)

`auto`, `code`/`github`, `company`, `financial`, `people`, `paper` (research),
`publication`, `personal-site`, `news`, `lead`, `research`, `personal`,
`lead-generation`, `websets`. Call `await exa.modes()` for the full list.


## v0.7 — next-layer power

- **`exclude_source_domain`** in `find_similar()` — drop results sharing the seed
  URL's source domain (big for research diversification).
- **`extras_rich_image_links`** across `search()`, `fetch()`, `find_similar()`,
  `monitor_create()`, and `deep_research()` — asks the API for rich image links
  (URL + alt-text) per result.
- **`compliance="hipaa"`** on `search()`/`run()` — enterprise-only HIPAA mode.
  Non-enterprise plans receive an `ExaPlanError` (403).
- **`monitor_create()` content parity** — all rich-content options now flow to
  search monitors: `text_verbosity`, `highlights_max_characters`, `summary_schema`,
  `extras_links`/`image_links`/`rich_links`/`rich_image_links`/`code_blocks`,
  `max_age_hours`, `subpages`, `subpage_target`, `include_sections`/
  `exclude_sections`, `include_html_tags`, `livecrawl`, `livecrawl_timeout`.
- **`merge_searches(*results)`** — merge multiple `SearchResults` into one with
  URL-deduplication and `query_hits` ranking (fast-path returns a single input
  unchanged).
- **`monitor_check(id)`** — convenient last-run peek + auto-trigger for
  search monitors.
- **`SearchResults.to_markdown()`** — render results as friendly Markdown
  (numbered titles, linked sources, snippet/summary/date annotations) — ideal
  for journal entries, doc reproduction, or agent memory.


## v0.8 — next round power (round 7)

- **`stream_answer(..., collect_meta=True)`** — the trailing `citations` and
  `cost_dollars` events the API emits at the end of an answer stream are now
  captured (they used to be silently dropped). The generator yields incremental
  text deltas, then one final dict `{"text", "citations", "cost_dollars",
  "request_id"}` with the full cited sources (title + url + image + favicon)
  and spend. `collect_meta=False` restores the old plain-string-only behavior.
- **`stream_search()` grounding parity** — the `/search` SSE `grounding` event
  (per-field citation clusters with `confidence`) is no longer discarded. It
  still yields incremental `text-delta` content, and now returns a rich final
  dict `{"results", "output", "grounding", "citations", "text", "request_id"}`
  with URL-deduplicated citations — so streamed searches expose their explicit
  sources too (verified live).
- **Webset criterion-evaluation filtering** — `webset_items(..., satisfied=
  "yes"|"no"|"unclear")` keeps only items whose per-criterion `evaluations`
  match that mark (lead qualification). Each item's `evaluations` carry
  `criterion` / `reasoning` / `satisfied` / `references` — the evidence-based
  WebSets surface.
- **`webset_eval_review(id)`** — review a webset through the lens of its
  criterion evaluations: returns a per-item markdown review (criterion →
  `satisfied` + reasoning), plus `criteria`, `by_satisfaction` counts, and the
  raw `items`. Great for a quick qualification pass before enrichment.




## v0.9 — structured entities + Exa Connect cost + contents parity (round 8)

- **Structured entity extraction** — entity-backed result categories
  (company / people / publication) return rich, **stable-`id`** library profiles
  (e.g. `https://exa.ai/library/organization/...`). Normalize them into readable
  snake_case profiles:
  ```python
  res = await exa.search("OpenAI leadership", num_results=5, category="people",
                         with_highlights=True)
  exa.entity_summary(res[0].entities[0])   # one raw entity -> readable profile
  exa.entity_type(res[0].entities[0])      # "person" | "company" | "publication"
  res[0].to_entity()                       # first entity of a single result
  res[0].entities_by_type()                 # group by type: {"person": [...]}
  exa.entities(results=res)                 # ALL profiles across results, deduped by id
  ```
  Profiles carry the entity's stable `id`, `name`, and type-specific fields:
  company → `foundedYear`/`description`/`workforce`/`headquarters`/`financials`/
  `webTraffic`; person → `firstName`/`lastName`/`location`/`workHistory`/
  `educationHistory`; publication → `year`/`authors`/`citationCount`/`doi`/
  `abstract`. Work-history entries link to their own entity `id`s — build a
  real knowledge graph from one search. (All verified live.)
- **Exa Connect cost breakdown** — when `agent(data_sources=[...])` actually
  invokes a Connect provider (fiber, similarweb, baselayer...), the run returns
  per-provider tool-call counts and spend. `AgentRun.connects` aggregates them:
  `{"fiber": {"calls": 2, "cost_usd": 0.01}}`, with `connect_providers` and
  `connect_cost_usd` helpers. Providers are only listed when the agent actually
  used them (zero-use providers are omitted by the API).
- **`fetch(..., compliance="hipaa")`** — HIPAA mode is now wired through on the
  `/contents` endpoint too, matching `/search`/monitors (verified: returns a
  clear `ExaPlanError` 403 on non-enterprise plans instead of a bare error).


## v0.10 — content-option parity + rich convenience surfaces (round 9)

Closes the last cross-endpoint content-parity gaps and adds analysis-friendly
helper surfaces on results — all verified live (2026-08).

- **`fetch()` content parity** — `fetch(..., highlights_max_characters=N)` is now
  accepted and forwarded (previously only `search()`/`find_similar()` honored a
  highlights character cap). Control how much snippet text each fetched page's
  highlights may return:
  ```python
  exa.fetch(["https://exa.ai"], mode="highlights",
            highlights_query="Exa search", highlights_max_characters=400)
  ```
- **`run()` content parity** — `run()` (the readable `await exa(query)` wrapper)
  now forwards `text_verbosity` (compact|standard|full) and
  `highlights_max_characters` through to the underlying search, matching the
  full content-option set of `search()`.
- **`Result.domain` / `Result.hostname`** — every result reports its
  registrable domain (`cloudflare.com`) and full hostname
  (`developers.cloudflare.com`). Great for source grouping without a PSL table.
- **`SearchResults.domains()` / `.top_domains(n)`** — a domain→count frequency
  map and a descending sorted `[(domain, count)]` list for source-densification
  analysis ("who do these results actually come from?").
- **`SearchResults.to_dataframe()`** — lazily-imported `pandas` DataFrame of all
  results (title/url/domain/hostname/published_date/author/score/snippet/
  summary/highlights). `include_entities=True` adds the raw entities column.
  Clean table for downstream stats, dedup, or CSV export.
- **First-class new search types** — `instant` (lowest latency, chat/autocomplete),
  `deep-lite` (consistent ~4s lightweight research), `deep-reasoning` (stronger
  reasoning for complex decision analysis) are all live-verified. `instant` is
  the recommended `search_type` for interactive/fire-and-forget experiences.


## v0.11 — request-latency & cost metadata + richer helpers (round 10)

Request-level metadata and cost surfaces that round-trip the search response
envelope — live-verified against current response fields.

- **`SearchResults.search_time_ms`** (`searchTime`, ms) — per-request latency on
  `search()`, `find_similar()`, aggregated across the fan-out of `deep_research()` /
  `merge_searches()`, captured on `fetch(include_meta=...)` and the `stream_search`
  final dict.
- **Cost helpers** — `total_cost()` (USD float, `None` when unreported),
  `cost_breakdown()` (`{"total": ..., "modes": {...}}`, handles both flat
  `{"total": 0.012}` and nested `{"total": ..., "search": {"neural": ...}}` forms),
  and `cost_report()` (one line, e.g. `"$0.0120 total / modes: neural $0.0070"`).
- **`SearchResults.to_meta()`** — the request envelope (query / count / request_id /
  resolved_search_type / search_time_ms / cost_dollars) as a dict; `to_json(include_meta=True)`
  renders results wrapped with that metadata.
- **`fetch(..., include_meta=False)`** — opt-in envelope `{"results": [...],
  "request_id": ..., "cost_dollars": ..., "search_time_ms": ...}` (plus `"rendered"`
  on the single-URL text form). Default returns (list / single string) unchanged.
- **`stream_search`** — the final dict now includes `search_time_ms` (from SSE events).
- **`Result` subpage access** — `subpage_count`, `subpage_titles()`, `subpage_urls()`,
  `subpage(index)` (typed `title/url/id/published_date/author/image/favicon`), and
  `to_subpages()`. `to_dict()` reports `subpages` (count) too.
- **`Answer.to_markdown()` / `AgentRun.to_markdown()`** — render a citation-grounded
  answer (or agent run) as numbered markdown with a Sources section (agent runs also
  show status/cost/structured output). Handy for journaling, docs, or agent memory.

## Search types (`search_type=`)

The valid `type` values the live API accepts: `auto` (default), `instant`,
`fast`, `hybrid`, `neural`, `keyword`, `deep-lite`, `deep`, `deep-reasoning`,
and legacy `magic` (mapped to `deep`). Note: **`semantic` is NOT valid** — it
returns a 400; use `neural` for the semantic/neural mode. `hybrid` mixes neural
+ keyword for broad recall.

## Advanced search params

```
search_type, category (validated), use_autoprompt,
start_published_date / end_published_date,
start_crawl_date / end_crawl_date, include_domains / exclude_domains,
text_include / text_exclude, num_results (1..100), user_location,
additional_queries (deep variants), moderation, context, timeout
```

## Rich per-result content (opt-in — returned only when you ask)

```python
# Structured call pulling highlights + an LLM summary + outbound links:
res = await exa.search("Cloudflare Workers pricing", num_results=5,
                       with_text=True, with_highlights=True,
                       with_summary=True, summary_query="What does it cost?",
                       extras_links=3)
res[0].to_dict()     # -> title,url,published_date,author,image,favicon,
                     #    text,highlights,highlight_scores,summary,extras
```

- `with_text` (+ `max_characters`, `text_verbosity` = compact|standard|full)
- `with_highlights` (+ `highlights_query`, `highlights_max_characters`)
- `with_summary` (+ `summary_query`, `summary_schema` = JSON Schema for a
  structured summary returns)
- `extras_links` / `extras_image_links` / `extras_rich_links` /
  `extras_rich_image_links` / `extras_code_blocks` (outbound links / images /
  rich links / **rich image links with alt-text** / code blocks per result)
- `max_age_hours` (0=fresh, -1=cache only, <=720) and `subpages`
- **v0.6** `include_sections` / `exclude_sections` (header|navigation|banner|
  body|sidebar|footer|metadata — best-effort semantic section filtering),
  `include_html_tags` (keep lightweight HTML not markdown), `livecrawl`
  (never|always|fallback|preferred) + `livecrawl_timeout` (ms), and `stream`
  (SSE bytes when combined with `output_schema`).

## Citation-grounded answers (deep / output_schema)

```python
res = await exa.search("best chess openings 2024", num_results=3,
                       output_schema={"type":"object",
                                      "properties":{"rec":{"type":"string"}}},
                       system_prompt="Be terse and cite sources.")
res.answer               # synthesized content (str or object per schema)
res.answer_grounding     # per-field citations: [{field, citations:[{url,title}], confidence}]
```

`output_schema` turns a plain search into a grounded synthesis where each field
carries explicit source URLs. Pair with `search_type=deep` for heavier
multi-step research or `system_prompt` for style constraints.

## Fetch page content (`exa.fetch`)

```python
# single URL, plain text (backward-compatible string form):
await exa.fetch("https://clojure.org/about/history", max_characters=1500)

# multiple URLs, structured dicts with summary + highlights + error surface:
res = await exa.fetch(["https://a.io/x", "https://b.io/y"],
                      with_summary=True, with_highlights=True,
                      max_characters=800)
# each item: url / title / text / highlights / summary / extras /
#            status(source / status / error.tag)
```

`mode=` picks the primary shape: `text` (default), `markdown`, `highlights`,
`summary`. Provide either `urls` or `ids` (never both). `max_age_hours` (0 =
fresh fetch), `livecrawl` (never|always|fallback|preferred), `livecrawl_timeout` (ms),
`include_sections`/`exclude_sections`, `include_html_tags`, and `subpages` —
all (v0.6) as in `search()`.

## Validation

Bad param combos raise a clear `ExaError` instead of silently mis-filtering or
returning a bare 400: `num_results > 100`, `max_characters > 10000`,
`company`/`people` categories combined with publish-date filters or
`exclude_domains`, and `fetch(urls=...)` with `ids=...` both set.

## Error classification (fail fast with precise guidance)

API failures raise one of these subclasses of `ExaError` so the agent knows
exactly how to react — no golden-guessing:

- `ExaAuthError`  (401) — key invalid/expired/revoked → re-run `/login` or re-export
  `EXA_API_KEY`.
- `ExaBadRequestError` (400/422) — invalid payload/schema/param combo → fix
  `output_schema` or filters and retry.
- `ExaPlanError` (403) — endpoint/feature not on current plan (e.g. `/agent/runs`,
  `/monitors`) → contact hello@exa.ai to enable.
- `ExaNotFoundError` (404) — bad run/id or unresolvable resource → check the id.
- `ExaRateLimitError` (429) — rate limit / credit cap hit after auto-retries →
  slow down or top up credits at dashboard.exa.ai.
- `ExaServerError` (5xx) — transient Exa-side failure after auto-retry → back
  off and retry; if persistent, contact Exa.

Transient failures (429, 5xx) are auto-retried with short exponential backoff
before being raised.

## CLI

```bash
!exa --query "webassembly tail calls" --mode paper --num-results 5
!exa --query "Rivian funding" --mode company --num-results 5
```

(Type-assisted CLI: first arg is `--query`.)

## Pagination / paging through large sets

The v2 API has **no `offset`/`filterResultCt`**. To page, raise `num_results`
(up to 100) or loop narrowing `start_published_date`/`end_published_date`
windows, or pass `additional_queries` for deep variants. Higher-limit plans
require contacting hello@exa.ai. See references/usage.md.

See `references/usage.md` for mode ↔ API-category mapping, accents, and
silently-ignored-category behavior.

## v0.12 — pagination/filter parity + webset item helpers (round 11)

Closes the remaining API parity gaps for pagination, event/webhook filtering,
and webset item convenience — all live-verified (2026-08).

- **`webset_get(include_items=True)`** now uses the native `expand=items` query
  parameter (single round-trip) instead of a separate `webset_items()` call.
  The server embeds the current items inline — no extra HTTP round trip.
- **`agent_list(..., cursor=...)`** — the `/agent/runs` list now accepts a
  pagination `cursor` (the response already carried `hasMore`/`nextCursor`).
- **`agent_events(run_id, limit=..., cursor=...)`** — the `/agent/runs/{id}/events`
  endpoint supports page-size + cursor pagination; the list object exposes
  `hasMore`/`nextCursor` for walking through the full event log.
- **`webhook_attempts(..., cursor=..., event_type=..., successful=...)`** — the
  attempts endpoint supports `eventType` and `successful` filters plus cursor
  pagination, matching the full API surface.
- **`event_list(..., types=..., created_before=..., created_after=...)`** — the
  audit-log event list now accepts multiple event types (plural `types`),
  an `eventType` singular alias combining with it, and `created_before` /
  `created_after` ISO-8601 timestamps. Both the singular and plural filter
  params are forwarded correctly.
- **`webset_preview(query, entity=..., count=...)`** — the preview endpoint now
  accepts an `entity` hint and a `count` when returning preview items.
- **Webset item helpers** — every webset item carries a `properties.type`
  discriminator, but callers previously had to dig into the raw dict. Now:
  - `webset_item_type(item)` → `"person" | "company" | "article" | "research_paper" | "custom"`
  - `webset_item_name(item)` — human-friendly identity (person name / company
    name / paper title / ...) regardless of item type.
  - `webset_item_summary(item)` — `[company] — Name — relevance description`.
  - `webset_items_all(webset_id, page_size=100, ...)` — iterate all items by
    looping through the API's `nextCursor` until exhausted; returns the
    complete list without callers needing to manage pagination themselves.


## v0.13 — recall coverage + rich convenience layers (round 12)

Adds genuinely new live-verified powers on top of the fully-wrapped API:

- **`webset_recall(webset_id, search_id=None)`** — extracts the read-side
  total-match **coverage** estimate the WebSets API produces for a search
  created with `recall=True`. Returns the expected total, confidence
  (high|medium|low), min/max bounds range, and the agent-authored reasoning,
  as both a one-line summary and structured fields. Live-verified: a "AI
  startups in California that raised Series A in 2024" search returned an
  expected total of ~200 matches (medium confidence, range 100-300) with
  step-by-step reasoning over PitchBook/NVCA data — so you can judge how much
  of a true market you've captured.
- **`similar_to(url, **kwargs)`** — friendly alias for `find_similar` for the
  "more like this" workflow (expand one good source into a cluster of peers).
- **`agent_chat(query, ..., turns=N)`** — first-class multi-turn agentic
  research loop. Each turn is a full `agent()` run chained via
  `previous_run_id`, so follow-up prompts continue the same thread with
  awareness of the prior turn's sources/grounding/conclusion. Returns the final
  answer + citations + every intermediate `AgentRun`. Live-verified over 2
  turns (survey → refine).
- **`search_to_csv(results, path, ...)`** and **`SearchResults.to_xlsx(path)`** —
  lazy-pandas tabular exporters (title/url/domain/.../highlights + optional raw
  entities column) for feeding search output into spreadsheets/analytics.

All four surfaces live-verified (2026-08); `__all__` is now 96 entries.


## v0.14 — answer citationFormat + per-turn agent schemas + JSONL export (round 13)

Adds three new live-verified powers; every prior entry point, signature, and
return value is unchanged.

- **`exa.answer(..., citation_format={...})`** / **`exa.stream_answer(..., citation_format={...})`** —
  forwards the live `/answer` `citationFormat` object (undocumented but
  accepted). Returns/streams rich citation dicts that populate `id`, `url`,
  `title`, `image`, `favicon`, `author`, and `publishedDate` per source when
  the API knows them (populated conditionally — e.g. news articles carry
  `author`, Wikipedia rarely does). Verified live: both the non-stream and SSE
  paths carry the requested fields (incl. `favicon`) through.
- **`exa.agent_chat(query, ..., schemas=[...])`** — per-turn output-schema
  override. Pass a list of JSON schemas, one per turn, to chain progressive
  multi-schema research (turn 1 extracts companies → turn 2 enriches with
  headquarters → turn 3 adds more). Falls back to the single `output_schema`
  (or none) for turns past the list. Verified live over a 2-turn run that
  honored distinct schemas each turn.
- **`exa.search_to_jsonl(results, path)`** — JSON-native line-delimited export
  of search results (title/url/domain/.../highlights + raw `entities` by
  default). Purely stdlib `json` — **no pandas dependency** (unlike
  `search_to_csv`/`to_xlsx`), so it works in lean envs and pipes into jq/ndjson.

`__all__` is now 97 entries (added `search_to_jsonl`).


## v0.15 — fresh-news roundup, company brief, result diff (round 14)

Adds three purely client-composed convenience surfaces (all live-verified). Every
prior entry point, signature, and return value is unchanged.

- **`exa.news_roundup(query, *, days=..., num_results=..., ...)`** — sweep the last
  N days of fresh news for a query with a per-day `startPublishedDate`/`endPublishedDate`
  window loop, then de-dupe by URL and rank by freshness. Returns a ``SearchResults``
  with ``.window_counts`` (day-offset -> hits) and each result's
  ``extras["days_seen"]`` (the day offsets the URL appeared on). Verified live over
  a 3-day/4-result sweep that returned 12 unique fresh articles.
- **`exa.company_dossier(name, ...)`** — a one-call company brief from the
  ``company category search entity profile + summary + homepage body copy.
  Returns ``{query, name, entity_type, profile, source_url, summary, search_results,
  website_url/title/text}``. Verified live on Anthropic (founded-year, workforce,
  headquarters, revenue/funding + homepage text).
- **`exa.diff_results(a, b, *, key=...)`** — URL-level diff of two result collections
  (``SearchResults`` / ``Result`` list / raw dicts — e.g. two ``news_roundup`` sweeps or
  two monitor snapshots): ``added`` / ``removed`` / ``changed`` + a readable ``report``.
  Verified live on two overlapping news searches and on dict-valued inputs.

`__all__` is now 100 entries (added ``news_roundup``, ``company_dossier``, ``diff_results``).


## v0.16 — agent trace, typed answer streaming, webset snapshot diff, explain/magic (round 15)

Adds live-verified surfaces across each layer. Every prior entry point, signature,
and return value is unchanged (`__all__` 100 → 107).

- **`exa.agent_trace(run_id)` → `AgentTrace`** — parse the live `/agent/runs/{id}/events`
  step-trace into a typed object: ordered `steps` timeline (created / started / tool
  added / arg-done / source-added / source-truncated / output-done / completed),
  deduplicated per-tool summaries (`tools`: name, call_id, per-tool sources, timing),
  unique `sources`, and post-run metrics (duration, usage.searches, agentComputeUnits,
  cost). Renders a readable `to_markdown()` audit sheet; `to_dict()` for pipelines.
  Verified live on a 6-search + finish agent run (7 tool calls, 27 unique sources,
  41.8s duration, $0.10 cost).
- **`exa.stream_answer_source(...)`** — SSE-stream an `/answer` as *typed events*
  instead of bare strings: `{"kind":"delta", text, chunk_index, chars, cumulative_chars,
  tokens_estimate}`, `{"kind":"citations", citations, source_count}`,
  `{"kind":"cost", cost_dollars}`, and a final `{"kind":"done", text, citations,
  cost_dollars, request_id, total_chars, total_tokens}`. Per-chunk token metering is
  a client-side heuristic (chars/4). Resilient multi-line SSE-frame buffering.
  Verified live on /answer streaming.
- **`exa.webset_snapshot(wsid)` / `exa.webset_snapshot_diff(a, b)`** — take a
  client-side snapshot of a webset's item corpus keyed by URL (`{url: {id, source,
  type, url, description, content}}` with a UTC `taken_at`), then diff two snapshots
  for added / removed / kept / changed (description change) URLs. Purely local —
  no extra API call. Verified live on an "AI security startups" webset.
- **`exa.explain(url)`** — one-call readable page explanation: `fetch(url, with_summary,
  with_highlights)` → a dict with title/url/summary/highlights/markdown one-pager.
  Verified live on clojure.org.
- **`exa.magic(query)`** — deep research + citation-grounded answer + markdown
  pipeline in one call: `search(search_type="deep")` + `answer(query)` → `{summary,
  citations, results, answer, markdown}`. Verified live on "What is Reitit routing?".

`__all__` is now 107 (added AgentTrace, agent_trace, stream_answer_source,
webset_snapshot, webset_snapshot_diff, explain, magic). New renderers: AgentTrace._markdown`,
`stream_answer_source` event protocol, `webset_snapshot`/`webset_snapshot_diff`,
`explain`/`magic` convenience composites.


## v0.17 — entity roster, entity schema explorer, topic term clustering (round 16)

Round 16 adds three genuinely useful *client-side* composites that run a real
search on the live API and then post-process client-side, so they are
deterministic and always behave — no reliance on backend params that only get
*accepted* but do nothing. Every prior entry point, signature, and return value
is unchanged (`__all__` 107 → 110).

- **`exa.entity_search(query, ...)`** — run ``search()``, pull every
  ``entities[]`` record across the top results, normalize each with
  ``entity_summary()``, de-dup on the stable library ``id``, and rank by how
  many distinct source pages mentioned it. Returns a batch dict
  ``{"query", "category", "entity_count", "entities": [...], "total_cost"}``.
  Each profile carries ``_occurrences`` (source-page count). Best on
  entity-backed categories: ``category="company"`` / ``"person"`` /
  ``"research paper"`` etc. Verified live.
- **`exa.entity_schema(entity=None)`** — standalone entity attribute explorer:
  given a raw entity record, reports which known attributes are present (with
  their JSON kind: dict/list/primitives), which are optional-and-missing, and
  any extra keys. With ``{}``/``None`` it enters *discovery mode* and returns
  the full field map for every entity kind (company / person / research paper).
  Complements ``entity_summary`` (flattened values). Verified live.
- **`exa.top_terms(query, ...)`** — client-side TF/domain clustering. Runs
  ``search()``, tokenises every result title (English stop-words stripped),
  tallies term frequencies, and groups results by registrable ``domain``.
  Returns ``{"top_terms": [{"term","count"}], "domain_clusters": [..],
  "total_results", "total_cost"}``. A cheap local "shape of the result set".
  Verified live.

**Absent-by-design (live-probed this round, accept 200 but inert — NOT exposed):**
`startCrawlDate`/`endCrawlDate` on `/search` **and** `/contents` (http 200, but
identical results even against 1999/2099 crawl windows), `autoprompt:false`
full-OFF toggle, and `similarityThreshold` on `/search`/`/findSimilar` (0.0 vs
0.99 returned byte-identical result sets on keyword/neural/auto). Those are
documented here as absent rather than shipped, per the round's rule.

## v0.18 — developer-community surfaces (round 17B)

Round 17B ships first-class **developer** surfaces for software engineers /
agent developers. Each is a client-side composite that runs real searches /
HTTP through the skill and returns clean, structured objects — live-verified
against api.exa.ai on the queries noted. All prior entry points, signatures,
and return values are unchanged (`__all__` 110 → 117).

- **`exa.github_repo(url_or_repo, ...)`** — client-side GitHub repo profile.
  Resolves a `github.com/...` URL or `owner/repo` shorthand, confirms it via
  `search(mode='github')`, then pulls structured repo metadata + README head
  from the raw GitHub API / raw README (permitted developer HTTP).
  Returns `{owner, repo, description, primary_language, stars, forks, topics,
  license, url, default_branch, homepage, archived, created_at, updated_at,
  readme_head}`. Live-verified on `ollama/ollama` and `ggerganov/llama.cpp`
  (the renamed redirect `ggml-org/llama.cpp` resolves via follow_redirects).
- **`exa.code_search(query, *, language=None, num_results)`** — github-mode
  search surfaced as clean `code` / `pull_requests` / `commits` / `issues` /
  `repos` hits with snippets. Optional `language` filters client-side by file
  extension. Verified on `"llama.cpp vulkan replace llama_vulkan.move"`.
- **`exa.hf_models(query, ...)`** — HuggingFace **model** search: site-scoped
  `search(include_domains=['huggingface.co'])` + raw HF API model JSON for
  downloads/likes/task/license/params + raw model-card README. Returns
  annotated `{model_id, author, task, library, license, downloads, likes,
  params, base_model, description, tags, url, card_markdown, ...}`.
  Verified on `"llama 3.1 8b"` (meta-llama/Llama-3.1-8B-Instruct, params
  8030261248, license llama3.1). Gated-model card fetch returns empty
  ``card_markdown`` (401) — metadata still fully populated.
- **`exa.hf_discussions(query, ...)`** — HuggingFace community **discussion**
  threads: HF-scoped + `discuss.huggingface.co`-scoped search, classified
  `model-community` / `huggingface-forum`, with excerpts. Verified on
  `"Llama 3.1 8B"`.
- **`exa.dev_help(query)`** — citation-grounded dev **answer** with code
  snippets + enumerated steps + source citations (title/url/author/date).
  Verified on `"how to fix UnicodeDecodeError utf-8 gbk"` (8 sources, 3 code
  fences including the `encoding="gbk"` fix).
- **`exa.find_used_by(package, ...)`** — find repos / notebooks / `.py` /
  `.pyt` pages that **import** a package; github-mode `import <pkg>` +
  notebook pass, de-duped, self-repo excluded. Verified py-level signal on
  `ollama` (pdichone/ollama-fundamentals, RamiKrispin/ollama-poc, microsoft/
  Phi-3CookBook) and `httpx` (rcmckee/webscraping-with-selectolax-and-httpx,
  jupyter/notebook, aravenel-informed-name/...).
- **`exa.dev_report(query, ...)`** — one-call **dev-light README**: web search
  + github repo profile + cited answer + fresh-news sweep, returned as a
  markdown string (`return_meta=True` wraps the pieces). Verified integrity
  on `"ollama python server usage"` (markdown with summary, cited sources,
  GitHub block, top results, fresh news).

## Round notes

- `references/round15.md` — v0.16 agent-trace / typed answer streaming / webset snapshot-diff / explain / magic, all live-verified.
- `references/round18.md` — v0.18 developer-community surfaces (github_repo,
  code_search, hf_models, hf_discussions, dev_help, find_used_by, dev_report), all live-verified.
- `references/round16.md` — v0.17 client-side entity roster extraction, entity
  schema explorer, topic term/domain clustering, all live-verified.
- `references/usage.md` — mode ↔ API-category mapping, accents, silently-ignored categories.
- `references/websets.md` — WebSets endpoint-by-endpoint guide + examples.
- `references/round10.md` — v0.11 next-layer power (latency/cost metadata, subpage accessors,
  fetch(include_meta=...), markdown renderers), all live-verified.
- `references/round11.md` — v0.12 next-layer power (webset expand=items, agent_list/events cursor,
  webhook_attempts filters, event_list types+/time filters, webset_preview entity/count,
  webset item helpers + pagination), all live-verified.
- `references/round12.md` — v0.13 next-layer power (webset recall coverage,
  similar_to alias, agent_chat multi-turn loop, search_to_csv/to_xlsx exports),
  all live-verified.
- `references/round13.md` — v0.14 next-layer power (answer/stream_answer
  citationFormat, agent_chat per-turn schemas, search_to_jsonl export),
  all live-verified.
- `references/round14.md` — v0.15 next-layer power (news_roundup fresh-news
  sweep, company_dossier brief, diff_results URL diff), all live-verified.
