# Exa skill v0.9 — Round 8 next-layer power

Verified live against api.exa.ai (2026-08-07). All features below work with the
current public API.

## Structured entity extraction

Entity-backed result categories (company / people / publication) return a rich
`entities` array on each result — LLM-composed, **stable-`id`** library profiles
(e.g. `https://exa.ai/library/organization/...`,
`https://exa.ai/library/person/...`). The library id is stable across searches,
so you can join/summarize a person/company across many result sets (a light
knowledge graph).

### New surface

| Helper | Purpose | Live-verified? |
|---|---|---|
| `exa.entity_summary(record)` | normalize one raw `entities[]` record to a readable snake_case profile (`id`/`type`/`name` + typed fields) | ✓ |
| `exa.entity_type(record)` | type discriminator (company/person/publication) | ✓ |
| `Result.to_entity(index=0)` | first/selected normalized profile of a result | ✓ |
| `Result.entities_by_type()` | group a result's profiles by type | ✓ |
| `SearchResults.entities()` | all profiles across results, deduped by id | ✓ |
| `exa.entities(results=...)` | same, over a SearchResults / list of Result | ✓ |

Profiles per type:

- **company** — `name`, `foundedYear`, `description`, `workforce{total}`,
  `headquarters{city,country,address}`, `financials{revenueAnnual,fundingTotal,
  fundingLatestRound}`, `webTraffic{visitsMonthly,countryRank,..}`, `research`.
- **person** — `firstName`/`lastName`/`name`, `location`, `workHistory[{title,
  company{id,name}, dates}], `educationHistory[{degree,institution{id,name}}]`.
- **publication** — `title`, `year`/`date`, `authors`, `citationCount`,
  `referenceCount`, `abstract`, `doi`, `language`, `type`.

Work-history entries embed their own organization `id`s, so chain lookups like
"who worked with Stripe under OpenAI leadership" are one search each.

## Exa Connect cost breakdown (AgentRun.connects)

`agent(data_sources=[...])` runs can invoke Connect providers (fiber,
financial_datasets, similarweb, baselayer, affiliate, particle, jinko). When (and
only when) the agent actually calls one of those providers, the run response
carries per-provider usage (`usage.dataSources`) and spend
(`cost_dollars.dataSources`).

- `run.connects` → `{"provider": {"calls": int, "cost_usd": float}}`
- `run.connect_providers` → list of providers actually used
- `run.connect_cost_usd` → total Connect spend in USD

Providers with zero use are omitted by the API (verified live: when the agent
chose web search over a Connect tool, `connects == {}`).

## fetch/contents HIPAA parity

`fetch(..., compliance="hipaa")` wires the HIPAA flag through to `/contents`
(the same `contents` options already power `/search` and `/monitors`). On a
non-enterprise plan the API returns a clear 403 which is classified as
`ExaPlanError` ("HIPAA compliance is available for enterprise customers").
Verified live.

## Version

- Module header: v0.9
- pyproject.toml: 0.9.0
