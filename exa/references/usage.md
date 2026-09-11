# Search, content, and result handling

## Setup and interface

Install the bundled directory with `uv run --with /path/to/exa python ...` or
install it into a virtual environment. Import `exa`; do not install a similarly
named registry package as a substitute. Python 3.10+ and `httpx` are required.

Key lookup checks `EXA_API_KEY`, then dotenv locations including
`~/.prime/agent/.env`, `~/.prime/.env`, `~/.config/prime/env`, `$PRIME_DOTENV`, and
cwd `.env`, then `~/.prime/agent/keys/exa.key`. A found key is exported into the
process environment. Prefer explicit environment configuration; do not dump files
or call private key helpers merely to check availability.

## Modes and filters

| Mode | Category |
|---|---|
| `auto` | Unrestricted |
| `code`, `github` | `github` |
| `company`, `lead`, `lead-generation`, `websets` | `company` |
| `financial` | `financial report` |
| `people` | `people` |
| `paper`, `research` | `publication` |
| `personal-site`, `personal` | `personal site` |
| `publication` | `publication` |
| `news` | `news` |

Check `modes()` or the module's mappings for aliases. Company/people searches
are locally rejected with publication-date or `exclude_domains` filters.
Unknown categories can fall back to unfiltered search with a warning; do not
interpret the output as filtered. The client validates `num_results` from 1–100.

`search_type` accepts `auto`, `instant`, `fast`, `hybrid`, `neural`, `keyword`,
`deep-lite`, `deep`, and `deep-reasoning`; legacy `magic` maps to `deep`.
These are client-accepted values, not a promise of current server support.
Use `auto` initially, fast variants for latency, and deep variants for a concrete
research need. `semantic` is not a supported client search type.

- `include_domains` / `exclude_domains`: lists of hosts.
- `start_published_date` / `end_published_date`: ISO date bounds.
- `text_include` / `text_exclude`: lists forwarded as text filters.
- `start_crawl_date` / `end_crawl_date` are forwarded, but their effect must be
  checked; do not confuse crawl time with publication time.
- `use_autoprompt=True` sends an autoprompt request; `False` omits the field,
  so it does not explicitly disable server-side rewriting.

## Content retrieval

Request just enough content for the next decision:

```python
results = exa.search("incremental view maintenance benchmark", num_results=5,
                     with_highlights=True, highlights_max_characters=600)
urls = [r.url for r in results][:3]
pages = exa.fetch(urls=urls, include_meta=True, max_characters=6000)
for page in pages["results"]:
    print(page.get("url"), page.get("status"), page.get("error"))
    print(page.get("text"))
```

Use exactly one of `urls` or `ids`. `include_meta=True` guarantees an envelope
with `results`, `request_id`, `cost_dollars`, and `search_time_ms`; metadata can
be null. Without it, `fetch` returns a list or a formatted string for a single
plain-text result—even when the input was a one-element list.

`mode="text"` retrieves text; `markdown` requests fuller text rendering rather
than guaranteeing faithful Markdown; `highlights` and `summary` request excerpts
and generated summaries. Use `with_highlights`, `highlights_query`,
`with_summary`, `summary_query`, and `summary_schema` for targeted extraction.
`max_characters` and highlight caps can truncate relevant evidence.

Advanced options include `text_verbosity`, `include_sections`, `exclude_sections`,
`include_html_tags`, `subpages`, `subpage_target`, `extras_links`,
`extras_image_links`, `extras_rich_links`, `extras_rich_image_links`, and
`extras_code_blocks`. Sections and subpages may be absent. Inspect the relevant
function signature: not every operation accepts every content option.

`max_age_hours`, `livecrawl`, and `livecrawl_timeout` request content freshness.
The client documents `max_age_hours=0` as fresh and `-1` as cache-only; inspect
returned status/source rather than assuming a crawl succeeded. `compliance`
is a server feature request, not a certification of the caller's workflow.

## Results, entities, and exports

`SearchResults` supports iteration, indexing, and `len`. Each `Result` has
`title`, `url`, `published_date`, `author`, `score`, optional content, and `raw`.
Use `to_agent()` for compact output, `to_dicts()` or `to_json()` for processing,
and `to_markdown()` for a readable list. Compact output is not a strict token
budget or a lossless substitute for `raw`.

`entities()` deduplicates profiles; `entity_search()` searches and builds a roster.
`entity_summary()` normalizes one record, `entity_type()` reads its kind, and
`entity_schema()` inspects available fields. Entity arrays are optional even
with a category. IDs help join records; verify identity and important financial
or employment claims against source pages. `company_dossier()` composes a company
search and optional page fetch; its first match need not be the official website.

`domains()` / `top_domains()` reveal concentration; domain extraction uses a
heuristic rather than a public suffix list. `top_terms()` counts title terms.
`merge_searches()` and `deep_research()` deduplicate URLs and use query-hit counts;
repetition is relevance evidence, not independent confirmation. `news_roundup()`
runs one query per day-window; repeated hits are not proof of a trend.
`diff_results()` compares URL membership and selected metadata, not whole pages.

`search_to_jsonl()` uses the standard library. `to_dataframe()` and
`search_to_csv()` need optional `pandas`; `to_xlsx()` also needs an Excel writer
such as `openpyxl`. Request those dependencies only for the export being used.
`to_meta()`, `total_cost()`, and `cost_breakdown()` preserve reported usage;
missing costs are unknown, not zero. Aggregated search times are summed request
latencies, not elapsed wall time.

## Errors and API drift

Catch `ExaAuthError`, `ExaBadRequestError`, `ExaPlanError`, `ExaNotFoundError`,
`ExaRateLimitError`, and `ExaServerError` as appropriate (all derive from
`ExaError`). The request layer retries transient HTTP failures. Do not repeatedly
retry auth, validation, or entitlement errors. Correct parameters or use an
available path while stating the limitation. For uncertain writes, inspect the
existing resource before retrying creation.

Error detail is read defensively: when a non-2xx response's body is not valid
JSON (an HTML error page, a CDN banner, or a truncated body), the raw text is
included in the raised message rather than crashing the parse. Only idempotent
requests (GET, or explicitly marked) are retried, so state-creating POST/PATCH/DELETE
calls are never double-sent.

Use the [official Exa documentation](https://exa.ai/docs) to verify changing
endpoint support; the bundled [module](../src/exa/__init__.py) determines Python
signatures. A wrapper's existence does not guarantee a plan permits its endpoint.
