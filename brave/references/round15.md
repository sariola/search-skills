# Round-15 — typed structured blocks, deep place dossiers, live-news filter, consistent news age

All live-verified on `api.search.brave.com` with the logged-in key
(BRAVE_API_KEY / BRAVE_SEARCH_API_KEY).

## `structured(query, *, count=12, ...)` — typed blocks from one web SERP
One `web` search embeds several heterogeneous `schema.org` blocks at once.
`structured()` runs a single `web` search (no extra round-trips) and re-pools
every present block into typed object lists with per-category `counts`:
`recipes`, `products`, `movies`, `software` (from per-result schema blocks,
same normalised shape as `recipes()`/`products()`/`movies()`/`software()`) and
`discussions` (from the top-level `discussions` section, e.g. Reddit threads).
`n == sum(counts.values())`.

Verified live:
- `chocolate chip cookie recipe` → `recipes: 7`
- `macbook pro 14 2023 price`   → `products: 3, discussions: 10`
- `inception 2010 movie`        → `movies: 3, discussions: 4`
- `fastapi python`              → `discussions: 11` (no software block on SEER)

## `place_detail(query, *, count=10, similar=None, location=..., index=0, ...)`
One-call deep dossier assembling Brave's complementary views of a place:
- `anchor` — `place_search` row (title, street address, coords, phone, rating,
  price, categories)
- `pois`   — the deep record (`pois(id)`): reviews, photos, email, profiles,
  full-week hours, price range
- `blurb`  — the AI-written place description (`poi_descriptions(id)`)
- `nearby` — geo-anchored `near(similar-or-first-category, lat, lon)` rows, each
  with a **computed great-circle `distance_km` / `distance_formatted`
  ("848 m", "1.6 km") / `distance_kind = "straight_line_estimate"`**; the
  anchor itself and score-duplicates are skipped, deduped by id / (title,dist).

Verified live (SF):
- `place_detail("Sightglass Coffee", location="San Francisco, CA",
  similar="coffee shop", count=8)` → anchor coords, deep `pois`,
  `blurb` (AI text), and `nearby` list: Taylor Street (998 m), Delah (848 m),
  Telescope (339 m), Philz (879 m), Four Barrel (1.6 km), Peet's (1.0 km).
- No coordinate → `coords=None`, `nearby=None`.

Distances are great-circle estimates from the anchor coordinate; an ETA API is
still needed for drive times.

## `news_live(topic, *, count=50, ...)` — live-coverage-only news filter
Dry filter returning the news rows where `is_live is True`. Live-verify note:
on the queries pressed (breaking/live-match/debate) **no** `is_live` rows were
returned via the standalone `/news` endpoint on this plan, so `news_live` now
returns `[]` for those topics — documented as a plan-gate observation, not a
fabricated hit. The web-embedded feed's `breaking` flag does fire (round-14:
20/20 on "breaking today") but `is_live` does not; prefer `headlines()` /
`news_cluster()` for a fresh-timeline.

## Consistent news age — `age_meta` + `published_at`
Every news item (`news`, `headlines()`, `news_cluster()`, `news_breaking()`,
`news_live()`) now reports `published_at` (the ISO `page_age` timestamp) and
`age_meta = {kind, days, text, published_at}`:
- `kind` = `"relative"` (parsed from "N min/hr/day/week/month/year ago"),
  `"date"` (absolute "Month D, YYYY"), or `"unknown"`.
- `days` = numeric age for the relative form ("5 hours ago" → 0.21,
  "2 weeks ago" → 14.0), else `None`.
Basis: Brave returns only a human `age` string plus ISO `page_age`; there is
**no** separate `publish_time`/epoch field on this plan.

Verified live: `tech news` → "5 hours ago" with `days=0.21`; absolute-date
"January 7, 2025" → `kind="date", days=None`; both render consistently.

## Backward compatibility
All prior 45 public entry points keep their exact signatures and return
shapes; `_get_api_key()` untouched (env → dotenv → /login); no Serper/websearch.
