# WebSets API — bulk structured entity discovery

WebSets (api.exa.ai/websets) is Exa's web-scale **entity discovery** surface.
Whereas `/search` returns ranked *pages*, WebSets returns *records*: people,
companies, articles, research papers, or custom entities that match a
natural-language query, each with a rich structured profile. It backs lead
generation, prospect research, and enrichment workflows.

## Two API bases

- **Base** — `https://api.exa.ai` (`exa.DEFAULT_API_URL`): `/search`, `/contents`,
  `/answer`, `/findSimilar`, `/agent/runs`, `/monitors`.
- **WebSets** — `https://api.exa.ai/websets` (`exa.WEBSETS_API_URL`): websets +
  imports + webhooks + events + team + webset-monitors. All `exa.webset_*`,
  `exa.import_*`, `exa.webhook_*`, `exa.event_*`, `exa.wmonitor_*`,
  and `exa.team_info()` use this base.

## Round-5 upgrades (v0.7)

- **Recall**: `webset_create(..., recall=True)` (and `webset_add_search(..., recall=True)`)
  requests a market-size estimate in the search's `recall` field.
- **Bootstrap from existing data**: `webset_create(..., imports=[{"source":"import",
  "id": "...", "evaluate": True}])` seeds a webset from an existing import/webset;
  `imports=[...]` items may also carry `evaluate`.
- **Built-in enrichments**: `webset_create(..., enrichments=[{"description":"..."}])`
  runs extractions immediately after items are found.
- **Update support**: `enrichment_update(ws_id, en_id, format="email", ...)` and
  `import_update(import_id, title=..., metadata=...)` for in-place edits.
- **Pagination/filter improvements**: `webset_items(..., source_id=...)` filters by
  source; `webset_list(..., search=...)` filters by id/title substring.
- **Web-set monitors**: `wmonitor_create(webset_id, cron="0 9 * * 1", ...)` keeps
  websets fresh on a cron schedule (at most once/day). See "Two API bases".

## Entity types (auto-detected from the query, or override with `entity=`)

- `{"type": "company"}` — name, description, location, employees, industry,
  about, logoUrl, foundedYear, headquarters (address/city/state/postal), 
  financials (revenue/funding), webTraffic.
- `{"type": "person"}` — name, position, company/employer, location,
  workHistory[{title, location, dates, company}], pictureUrl, linkedinUrl.
- `{"type": "article"}` / `{"type": "research_paper"}` / custom via a description.

## WebSets lifecycle

1. **Preview** — `webset_preview(query)` resolves the query into (entity, criteria)
   without creating a webset. Iterate on the query first to get the desired
   decomposition before spending a full webset run.
2. **Create** — `webset_create(query, count=..., entity=..., criteria=...)` fires
   a search. It runs asynchronously: found items accumulate.
3. **Wait** — `webset_wait(id)` polls until the search phase (and, by default,
   any enrichments) reach a terminal state, then returns the webset w/ items.
4. **Read items** — `webset_items(id)` returns the structured records.
5. **Enrich** — `webset_enrich(id, description, format=..., options=...)` asks an
   agent to compute one more field per item (email/phone/URL/options/...).

## Multi-search

`webset_add_search(id, query, ...)` starts another search scoped to the same
webset, growing the set. Each run has its own id; `webset_search_status(id, sid)` /
`webset_search_cancel(id, sid)` manage individual runs.

## Imports

Seeding from a CSV of URLs/identifiers: `import_create("f.csv", title=...,
entity={"type":"company"})`. Requires a `format:"csv"`, `entity`, `size`, and
`count`; the CSV's identifier column (`identifier=`) is the entity key (URL by
default). Imports become scopes/excludes for webset searches.

## Webhooks & events

- `webhook_create(events, url, metadata=...)` returns a `secret` used to HMAC-verify
  deliveries. Events: `webset.created`, `webset.deleted`, `webset.completed`,
  `webset.search.created`/`...completed`, `webset.item.evaluations.updated`,
  `webset.item.enrichments.updated`, `import.created`/`...completed`/`...failed`.
- `webhook_attempts(id)` shows delivery status/response codes/timestamps for
  retry/debugging.
- `event_list()` is a read-side audit log; `event_get(id)` fetches one payload.

## Concurrency & limits

`team_info()` returns `limits.maxConcurrent` (e.g. 3) and `maxQueued` (0 here).
A webset counts against `active` concurrency while its search is `running`;
with `maxQueued:0` extra concurrent requests are rejected with a queue-full error
rather than waiting. Serializing webset creations on a personal plan is safest.

## Latency notes

A search across ~30-40 candidates with `count=3-5` typically resolves in 60-90s.
Rich company records are ready soon after; person discovery is usually slower.
Enrichments (email/phone) are the most latent step — they queue after searches
and can take minutes. Poll with `webset_wait` / `enrichment_get`.
## Criterion evaluations & lead qualification (v0.8)

Every `WebsetItem` carries an `evaluations[]` array — one entry per search
criterion — with `criterion`, `reasoning`, `satisfied` (`yes`|`no`|`unclear`),
and `references` (title/snippet/url) backing the decision. Three helpers put
this evidence layer to work:

- `webset_items(id, satisfied="yes")` — filter the returned `data` to only items
  meeting a given mark (`yes`/`no`/`unclear`); great for isolating qualified leads.
- `webset_eval_review(id)` — render a readable per-item markdown review (criterion
  → satisfaction + reasoning) plus `criteria`, `by_satisfaction` counts, and raw
  `items`.
- `webset_item_get(id, item_id)` — a single item's full reasoning + references.
