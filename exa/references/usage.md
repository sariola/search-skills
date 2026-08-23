# Exa modes, advanced categories, and API notes

This skill maps the former Codex MCP-based Exa skills onto the native Exa REST
API. Those were `exa-code-search`, `exa-company-research`,
`exa-financial-report-search`, `exa-people-search`, `exa-research-paper-search`,
`exa-personal-site-search`, `exa-lead-generation`, and `exa-websets`, and they
invoked Codex MCP tools that do not exist here. This skill is the drop-in REST
replacement, plus advanced search parameters and a page `fetch`.

## Mode ↔ category mapping

| `mode`          | Exa category       | Replaces former Codex skill    |
|-----------------|--------------------|--------------------------------|
| `auto`          | (none)             | —                              |
| `code`          | `github`           | exa-code-search                |
| `company`       | `company`          | exa-company-research           |
| `financial`     | `financial report` | exa-financial-report-search    |
| `people`        | `people`           | exa-people-search              |
| `paper`         | `research paper`   | exa-research-paper-search      |
| `personal-site` | `personal site`    | exa-personal-site-search       |
| `news`          | `news`             | —                              |
| `lead`          | `company`          | exa-lead-generation            |
| `websets`       | `company`          | exa-websets (see note below)   |

Aliases: `research`→paper, `personal`→personal-site, `lead-generation`→lead.

## Search types

- `search_type`: `auto` (default), `instant`, `fast`, `hybrid`, `neural`,
  `keyword`, `deep-lite`, `deep`, `deep-reasoning`, and legacy `magic`.
- `magic` is mapped to `deep` (the advanced semantic/research mode) — pair it
  with a clear natural-language query for hard-to-target searches.
- **`semantic` is invalid** (the API 400s it). Use `neural` for the
  semantic/neural retrieval mode. `hybrid` mixes neural + keyword for broad
  recall.

## Advanced filters (all optional, combine freely)

- Domain: `include_domains` / `exclude_domains` — both are lists of strings.
- Dates (published): `start_published_date` / `end_published_date`,
  ISO strings such as `"2023-01-01"`.
- Dates (crawled): `start_crawl_date` / `end_crawl_date`.
- Text: `text_include` / `text_exclude` are substring lists applied to the
  returned text; `max_characters` bounds that text length.
- `use_autoprompt`: let Exa rephrase the query (better recall on broad topics,
  weaker on exact-match queries).

## Category caveats

The API **silently ignores unknown categories** (returning unfiltered results)
and 400s on a few (e.g. `tweet`). This skill only ever sends categories Exa
actually honors: `news`, `company`, `financial report`, `research paper`,
`personal site`, `github`, `people` / `person`, `linkedin`. If you pass an
unknown `category=` it prints a warning and filters nothing.

## fetch() — full page content

`fetch(urls, max_characters=1200, livecrawl="fallback")` posts to the
`/contents` endpoint and returns the page text (equivalent to the old
`web_fetch_exa`). Pass either `urls` or `ids`, never both — the API rejects the
combination. Use it to pull the full body of a result before summarizing.

## websets note

Durable Exa Websets *collections* are managed in the Exa web app;
`api.exa.ai/websets` returns 404 for REST API keys. The `websets` mode here
therefore returns a sized, scored company/entity list instead — size it with
`num_results`. For a true persistent collection, the user must create it in the
Exa UI.

## answer() — compact citation-grounded answers (/answer)

The `/answer` endpoint is purpose-built for a terse **bottom line with sources**,
distinct from a `search` `output_schema` synthesis. It takes a natural-language
question and writes a short answer, marking each claim with a citation.

```python
a = await exa.answer("What is the latest valuation of SpaceX?")
a.answer      # "As of ..., SpaceX has a market capitalization of ..." (+ [n] refs)
a.sources     # list of source URLs
a.citations   # full source dicts (title/url/published_date/author/image/favicon)
```

Pass `output_schema` (JSON Schema, root `text` or `object`) for a **structured**
answer:

```python
a = await exa.answer("What is the latest valuation of SpaceX?",
                     output_schema={"type":"object",
                                    "properties":{"valuation":{"type":"string"},
                                                  "date":{"type":"string"}},
                                    "required":["valuation","date"]})
a.answer      # -> {'date': '..., 'valuation': '$...'}
```

`text=True` additionally returns full page text for each source (heavier).
Answers take longer than plain searches; the default timeout is 90s.

## find_similar() — related pages to a URL (/findSimilar)

Given one strong URL (or a document `id` from a prior `search`), pull the pages
covering the same ground — handy to turn a single great article or doc page
into a source cluster:

```python
fs = await exa.find_similar("https://clojure.org/about/history", num_results=8)
```

Supports `category`, `include/exclude_domains`, published-date bounds, and the
same `with_text`/`with_highlights`/`with_summary`/`extras_links`/`max_age_hours`
rich-content options as `search`.

## deep_research() — fan-out research over several questions

Give `deep_research` the facets of a question as a list of related queries; it
searches each, merges results by canonical URL (`www.`-insensitive), and ranks
pages that matched more than one query to the top. Pages hit by >1 query carry
`result.extras["query_hits"] = N`.

```python
res = await exa.deep_research(["clojure concurrency primitives",
                               "clojure software transactional memory",
                               "clojure core.async"], num_results=8)
```

Set `dedupe=False` to keep every per-query result instead of merging. Pass a
`search_type` (e.g. `deep`) to make every facet a heavier synthesized search,
or fan out cheap `fast`/`instant` searches. `num_results` is per-query, so total
before de-dup is ~num_results × len(queries).

## agent() — full agentic research (`/agent/runs`)

The most powerful single surface: the agent reasons over the web, pulls
evidence, and writes a grounded answer — optionally JSON-structured. Unlike
`search`/`answer`, the agent can use multiple searches, reason about
novelty/duplication, enrich given records, and emit per-field citations with
cost accounting.

```python
run = await exa.agent(
    "Which 3 companies are the top recent AI-infrastructure Series A rounds?",
    output_schema={"type":"object","properties":{"companies":{"type":"array","items":{
        "type":"object","properties":{"name":{"type":"string"},
                                      "round":{"type":"string"}}}}},
                   "required":["companies"]},
    effort="minimal",                 # auto|low|medium|high|xhigh|minimal
)
run.text        # natural-language summary
run.structured  # validated output matching output_schema (None if no schema)
run.grounding   # [{"field": "structured.companies[i].name", "citations":[{url,title}], "confidence": "high"}]
run.citations   # flattened unique (url,title,field) list
run.cost        # total USD (agent compute + searches)
run.usage       # agentComputeUnits, searches, ...
```

Capabilities:
- `previous_run_id` continues a prior completed run (e.g. refining one answer).
- `data` / `exclusion` inlet accepts JSON records to process or records to avoid.
- `system_prompt` steers sourcing/novelty/tone.
- `dataSources` enables Exa Connect providers (max 5) — passed directly.
- Runs are async: `agent()` polls until completion (default `timeout` 120s,
  `poll_interval` 2s). On timeout it raises, keeping `run.id` so you can poll
  `/agent/runs/{id}` to resume.

## Errors

`_request` raises precise subclasses of `ExaError` for fast, actionable
handling: `ExaAuthError` (401), `ExaBadRequestError` (400/422),
`ExaPlanError` (403), `ExaNotFoundError` (404), `ExaRateLimitError` (429),
`ExaServerError` (5xx). 429/5xx are auto-retried with exponential backoff.

## Summary of which endpoint to reach for

| Task | Call |
|------|------|
| Ranked result list for a query | `exa.search(query, ...)` |
| Human-readable list            | `await exa(query, ...)` (`run`) |
| The answer with citations      | `exa.answer(question, ...)` |
| **Deep, self-contained research answer** | **`exa.agent(query, ...)`** |
| Structured research enrichment | `exa.agent_structured(query, schema)` |
| Broaden one good source        | `exa.find_similar(url, ...)` |
| Many angles at once            | `exa.deep_research([q1, q2, ...])` |
| Page body / highlights         | `exa.fetch(url, ...)` |


## Two API bases (v0.5)

- **Base** `https://api.exa.ai` — `/search`, `/contents`, `/answer`, `/findSimilar`,
  `/agent/runs`, `/monitors`.
- **WebSets** `https://api.exa.ai/websets` — websets, imports, webhooks, events,
  team. All `webset_*`, `import_*`, `webhook_*`, `event_*`, `team_info()`.

## Monitors — search (api.exa.ai/monitors) and webset (api.exa.ai/v0/monitors)

### Search monitors (`monitor_*`)
`monitor_create(query, period="6h", webhook_url=..., ...)` registers a recurring
search that reports what's new/changed on the watched pages. `period` is a
single-unit duration string (>= "1h"). Runs are change-detection summaries
delivered to your webhook; `monitor_runs(id)` / `monitor_run_get(id, rid)` read
them. `monitor_batch(action, name=..., dry_run=...)` does bulk pause/delete/unpause
(always try `dry_run=True` first).

v0.7 adds `filter_empty_results=True`, `with_text=True`, `max_characters=...`,
`with_highlights=True`, `highlights_query=...`, `with_summary=True`, and
`summary_query=...` on the monitor's per-search content extraction.

### Web-set monitors (`wmonitor`)
Different product: attach to a *webset* and run scheduled search/refresh ops.
`wmonitor_create(webset_id, cron="0 9 * * 1", timezone="Etc/UTC", count=10,
query=..., behavior="append")` — cron must be a valid 5-field Unix cron
triggering at most once per day. Use `wmonitor_update` for enable/disable,
cadence change, or behaviour reconfigure. Read run history with
`wmonitor_runs(id)` / `wmonitor_run_get(id, run_id)`. These live on
`api.exa.ai/v0/monitors`.

## WebSets notes

`webset_create(...)` runs asynchronously; poll with `webset_wait`.
`webset_enrich(id, description)` adds a computed field per item. Imports seed
websets from CSV; webhooks + events provide delivery/audit. See
`references/websets.md`.

Round-5 (v0.7) additions:
- `webset_create` — `recall=True` asks for a total-market estimate; `enrichments`
  list of `{"description": "..."}` extractions run on found items; `imports`
  list seeds the webset from existing collections (`{"source", "id", "evaluate"}`).
- `webset_add_search` — `recall=True` and `max_people_per_company=N` on the added search.
- `webset_items` — `source_id` filters items by the search/import that made them.
- `webset_list` — `search` filters by id/externalId/title substring.
- `import_update(id, title=..., metadata=...)` — PATCH import title/metadata.
- `enrichment_update(ws_id, en_id, description=..., format=..., options=..., metadata=...)` —
  PATCH an enrichment job's task/format/options.
