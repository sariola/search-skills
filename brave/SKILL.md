---
name: brave
description: Search the web with Brave for exact terms, current news, documentation, images, videos, and local places. Use for Brave searches or an independent discovery path through the bundled Python client, with structured results and page retrieval. Requires BRAVE_API_KEY or BRAVE_SEARCH_API_KEY.
---

# Brave search

Use Brave's independent index to discover sources, then read the relevant pages
before making substantive claims. This skill supplies a local Python client,
not a browser and not an MCP search tool.

## Start here

Resolve this skill's directory and run its package:

```bash
uv run --with /absolute/path/to/brave python - <<'PYTHON'
import brave
result = brave.search('site:docs.python.org "TaskGroup"', count=5)
for item in result.get("results") or []:
    print(item.get("title"), item.get("url"), item.get("snippet"))
PYTHON
```

Supply `BRAVE_API_KEY` or `BRAVE_SEARCH_API_KEY` through the environment; existing
fallbacks are in [api.md](references/api.md). Do not print credentials.
Use `brave.run(query)` for readable text. Calling `brave(query)` requires an
external harness binding and is not normal Python. The awaitable wrappers still
perform blocking I/O; use a worker thread if integrating with an event loop.

## Choose a path

| Need | Operation |
|---|---|
| Web sources | `search(query, count=5)`; compact dict by default |
| Readable result list | `run(query, ...)` |
| Exact metadata or blended ranking | `search(query, view="full")` |
| Recent articles | `search(query, mode="news", freshness="pw")` |
| Several query variants | `batch([q1, q2], count=5)`; inspect each query's errors |
| More pages | `paged(query, count=10, total=30, max_pages=3)` |
| Read a known page | `probe(url)` or `summarize_page(url)` |
| Images or videos | `search(query, mode="image" or "video")` |
| Local businesses | `place_search(query, location=...)` |

For developer research, use [developer.md](references/developer.md). For news,
media, local places, rich answers, and schema blocks, use
[verticals.md](references/verticals.md). Use [api.md](references/api.md) for return
shapes, filters, retrieval, and errors.

## Search, verify, answer

1. Use exact phrases, `site:`, exclusions, or `filetype:` when appropriate.
   Include the relevant version, place, or date scope. Check query diagnostics
   for spelling changes; use `spellcheck=False` when an exact identifier matters.
2. Triage a small set of results. Refine the query for the missing evidence before
   expanding to unrelated corpora. `mode="all"` and `mosaic()` issue multiple
   searches; use them only when those media types help answer the request.
3. Fetch the strongest primary sources and confirm the passage, date, and context.
   `probe()` succeeds only when its status and readable text support that claim;
   it is an HTTP fetcher, not a JavaScript browser. `summarize_page()` extracts
   sentences mechanically and may omit the part that matters.
4. Reconcile conflicting sources. Exa can provide a second discovery path when
   available and useful; agreement on one shared source is not corroboration.
   Stop once the answer is supported, or explain the remaining evidence gap.
5. Cite supporting page URLs next to claims. Separate provider facts, extracted
   text, heuristics, and your own conclusions. Treat publication and event dates
   separately; search freshness can reflect a page update rather than a new event.

## Return-shape rules

`search()` defaults to `view="agent"`: compact fields such as `title`, `url`,
`snippet`, and `date`. Empty fields and sections can be omitted, and `query` can
be a string or a diagnostics dict. Always use guarded access.

`view="full"` returns the client's normalized detailed response, not untouched
API JSON. Use it for `mixed`, `top_results`, thumbnail metadata, distinct date
fields, and full query diagnostics. Convenience helpers may return their own
full shapes; do not assume every helper follows the compact search schema.

Generated place descriptions and summarizer links are not primary evidence.
Sentiment counts, missing video duration, and estimated distances must not be
presented as consensus, confirmed live video, or travel time.
