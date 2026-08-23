# Round-14 — news depth, parallel batch, consensus review, BFS worker

All live-verified on `api.search.brave.com` with the logged-in key.

## News rows expose `breaking` / `is_live` (web-embedded news)
The **web-embedded news section** (`/res/v1/web/search` → `news.results[]`)
carries per-item `breaking` (bool) and `is_live` (bool) fields; the flat
`/news/search` endpoint does **not** (its items lack them). `_news_item`
now surfaces `breaking`, `is_live`, `is_source_local`, `is_source_both`.

Verified:
- `breaking news today` → 20/20 items `breaking=true`
- `openai news` → `breaking=false` across the board (a legitimate "not breaking
  right now" signal, not a bug)

## `news_breaking(topic, ...)`
Web search + filter to `breaking=true`. `{"query","results","n","total",
"render","search"}`.

## `news_beams(topic, ...)`
Web search, group the embedded news by publishing outlet (site host), report
`breaking_count`/`live_count` per outlet + top articles. The requested
"publisher/beam" view.

## `batch(queries, mode="web", count=4, concurrency=8, ...)`
concurrent thread pool over `search()`. Added `import threading`.

## `article_review(query, count=4, max_chars=3000, positive_hint,
negative_hint)`
`search` → `crawl` → positive/negative lexicon bucket → `good`/`mixed`/`bad`/
`neutral` verdict. No model call.

## `search_worker(query, depth=2, breadth=3)`
BFS over cron: seed = `search(count=breadth)`; next frontier = Brave's own
`cluster` (sitelink) URLs on the web results (+ naive in-page http(s) href
fallback); `crawl()` full text per node. Verified 2-hop (`clojure` → api/news/
rationale).

## `related(...)` — documented ABSENT
Probed: web JSON has **no** `related` key / related-queries block on any mode
or probe query; `/res/v1/suggest/*` 301→HTML / OPTION_NOT_IN_PLAN. Documented,
not fabricated. Use `batch`/`merge` for query discovery.

---
Notes:
- crawl() result items report success via `status == 200` **and** non-empty
  `text` (there is NO per-item `ok` key). `article_review`/`search_worker` use
  that contract.
- `run()` already renders `[Locations]` for local-intent web queries (round-6)
  and `mosaic()` already fuses `locations` (round-13); neither needed changes.
- No test data left behind; /tmp scratch and __pycache__ cleaned.
