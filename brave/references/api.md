# Client interface and retrieval

## Setup

Use the bundled `brave` directory with `uv run --with /path/to/brave python ...`.
Python 3.10+ and `httpx` are required. Credentials are sent in the
`X-Subscription-Token` header to `https://api.search.brave.com`.

The client checks `BRAVE_API_KEY` / `BRAVE_SEARCH_API_KEY`, then dotenv locations
including `~/.prime/agent/.env`, `~/.prime/.env`, `~/.config/prime/env`,
`$PRIME_DOTENV`, cwd `.env`, and its Prime key-store fallback. Prefer explicit
environment configuration. The external Prime launcher is not bundled; import
`brave` and use `run` or `search` directly.

## Search options

`search(query, view="agent", **kwargs)` delegates to the client's detailed search
implementation. `run(query, ...)` renders readable results. They do not have
identical signatures; inspect [the module](../src/brave/__init__.py) for an
unfamiliar option.

| Option | Use |
|---|---|
| `mode` | `web`, `news`, `image`, `video`, `local`, or `all` (web/news/video) |
| `count` | Requested results per corpus; actual result counts can be lower |
| `country`, `search_lang`, `ui_lang` | Geographic/language intent |
| `freshness` | `pd`, `pw`, `pm`, `py`, or `YYYY-MM-DDtoYYYY-MM-DD`; legacy `pd_1d/1w/1m/1y` aliases are normalized |
| `safe_search` | `off`, `moderate`, `strict` |
| `spellcheck` | Control rewriting; inspect `query.altered` in full view |
| `extra` | Request additional snippets |
| `goggles` | Custom ranking/filter rules; preferred over `goggles_id` |
| `result_filter` | Endpoint section names such as `web,news,discussions`; not skill mode aliases |
| `loc` | Location dictionary forwarded as geographic hints |
| `summary` | Request summarizer metadata/deep link, not a guaranteed answer body |
| `enable_rich_callback` | Request a rich-result hint |
| `timeout` | Per-request timeout; composites may take longer overall |

Other signature options include `grep`, `discussion_count`, `video_count`,
`movie_count`, `unit`, `text_decorations`, `units`, `operators`, and
`include_fetch_metadata`. Applicability depends on the endpoint. A 200 response
does not establish that an unsupported filter changed the results.

## Pagination

For web search, `count` is 1–20 and `offset` is a **page number** from 0–9.
Keep count fixed and advance offset by one, deduplicating overlap. See the
[official web API reference](https://api-dashboard.search.brave.com/api-reference/web/search/get).
`paged(query, count=10, total=30, max_pages=3)` handles this bounded traversal.
It can stop short of `total`; do not describe it as retrieving the whole index.
Check `more_results_available` in full query diagnostics when paging manually.

## Return shapes

Compact `search()` uses `results` for web/news/image/video/local. `mode="all"`
uses `web`, `news`, and `videos`. Optional embedded sections include `faq`,
`infobox`, `locations`, and `discussions`. Empty sections may be absent.

Compact hits use `title`, `url`, `snippet`, `date`, and optional `extra`,
`author`, `publisher`, `kind`, `qa`, `sitelinks`, and schema blocks. `query` may
be a string or diagnostics dict. Compact output removes fields as well as
aliases; it is neither a strict token budget nor a lossless full-response view.

Full view includes normalized detailed fields (`description`, `page_age`,
`article_date`, `age_meta`, image/thumbnail fields), query diagnostics, and
`mixed` / `top_results` blended ordering. Use `view="full"` before accessing
those keys. Helpers such as `headlines`, `paged`, and `mosaic` return their own
schemas and often include detailed rows plus a `render` or `readable` field.

`batch()` fans out queries and reports individual payloads/errors;
`merge(*searches)` deduplicates results by URL. Inspect partial failures instead
of treating an empty failed query as no matches. Helpers using several corpora
can issue several requests even though their Python call is singular.

## Read pages

`probe(url, max_chars=..., timeout=...)` fetches and cleans HTTP content.
Check `status == 200` and nonempty `text`; there is no universal `ok` field.
`crawl(urls, ...)` fetches several pages with the same per-item contract.
`summarize_page(url, max_chars=20000, max_points=5)` chooses a lead and weighted
sentences locally. It is an extractive convenience, not an LLM synthesis.

For blocked, script-rendered, empty, or truncated pages, use another available
retrieval method or report the limitation. Preserve the source URL and check
context before quoting. `search_worker(depth=..., breadth=...)` follows search
sitelinks and page links; use small explicit bounds when that traversal is needed.

## Errors and capabilities

`BraveError.category` distinguishes `auth`, `param`, `rate_limit`, `http`,
`network`, and `timeout`; inspect `details` for structured diagnostics. An invalid
token can be HTTP 422, so HTTP status alone does not distinguish auth from
validation. The request layer retries transient failures and honors Retry-After;
avoid layering unbounded retries on top. Correct auth/parameters or state the
plan limitation when the response is definitive.

Local mode can fall back to web search; inspect `fallback`. Stub helpers
`related`, `trending_topics`, `trending_libs`, and `docker_aliases` return
limitation notes rather than discovery data. Do not use them as working indexes.
LLM Context, Answers, autosuggest, and other provider products may exist without
being wrapped here or enabled for the current key. Consult
[Brave's documentation](https://api-dashboard.search.brave.com/app/documentation/web-search)
when current entitlement or endpoint behavior matters; do not generalize one
account's historical error into permanent absence of a provider feature.
