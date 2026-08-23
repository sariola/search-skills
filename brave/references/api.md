# Brave skill — API details, modes, shapes

Base: `https://api.search.brave.com`. Auth: header `X-Subscription-Token: <key>`.

## Two call surfaces

| Function       | Returns | Use when                                   |
|----------------|---------|--------------------------------------------|
| `run(query,…)`  | str (formatted) | showing results to a user / final answer |
| `search(query,…)`| dict (structured) | scripting a pipeline, ranking, persisting |

`run()` is the kernel-bound entry (`await brave(...)` calls `run()`); `search()`
is available on the module for structured scripting (`await brave.search(...)`).

## Modes and endpoints

| `mode` | Path                     | Highlights                          |
|--------|--------------------------|-------------------------------------|
| web    | `/res/v1/web/search`     | + knowledge panel (`infobox`), embedded `videos` + `discussions`, `faq` Q&A, `mixed` blended order, query diagnostics |
| news   | `/res/v1/news/search`    | recent news; pair with `freshness`  |
| image  | `/res/v1/images/search`  | original image src + dimensions (+ `unit`) |
| video  | `/res/v1/videos/search`  | creator, channel, duration, thumbnail, tags |
| local  | `/res/v1/local/search`   | local places (falls back to web when the key lacks local support) |
| all    | (web+news+video merged)  | one query across the indexes       |

## Structured `search()` return shapes

`web`:
```json
{"mode":"web","query":{...},"infobox":{...},"summarizer":{...}|null,
 "results":[web...],"web":[web...],"videos":[video...],"discussions":[discussion...],
 "faq":[faq...],"family_friendly":bool|None,"might_be_offensive":bool|None,
 "mixed":{...},"top_results":[{type,item}...]}
```
News/video/image replies carry a `might_be_offensive` bool too (round-5).
`news` / `video` / `image`:
```json
{"mode":"...","query":{...},"results":[...]}
```
`all`: `{"mode":"all","query":{...},"infobox":{...},"web":[...],"news":[...],"videos":[...],"faq":[...]}`
`local`: `{"mode":"local","query":{...},"fallback":{...}|null,"results":[...]}`

Normalised item shapes:

- web: `title, url, site, description, extra_snippets[], type, subtype,
    software, is_live, is_source_local, is_source_both, language, age,
    page_age, article_date, author, author_meta[{name,url,thumbnail}],
    publisher, organization, contact_points, family_friendly,
    favicon, thumbnail, thumbnail_original, thumbnail_is_logo, breadcrumb,
    profile{name,url,long_name,img}, deep[{title,url}],
    cluster[{title,url,description}], cluster_type, qa{question,answer,url,
    upvote_count}, embedded_video{duration,thumbnail},
    creative_work{name,rating{value,best,reviews}},
    location{title,url,coordinates,address,phone},
    inline_faq[{question,answer}]`
    (round-7: `breadcrumb` clean trailing-break from `meta_url.path`;
    `thumbnail_original` unproxied URL; `thumbnail_is_logo` flag; `author_meta`
    extra author identity; `contact_points` org phone list; `creative_work`
    course/review ratings; `location` map/POI on place-intent results;
    `inline_faq` Q&A embedded inside a single result.)`
    (round-5: `subtype` is the raw category `article|qa|software|generic|web`;
    `software` is the package/registry dict on software-subtype hits: `{name,
    author, version, code_repository, published, programming_language,
    registry:[pypi|npm,...]}`.)
    (round-3: `published` renamed `article_date` (article.date ISO), plus
    `is_source_local/is_source_both`, `deep` nav buttons, inline `qa`, embedded
    `video`, `profile` flattened.  round-4: `cluster` sitelinks + `cluster_type`
    surfaced, and the `qa.answer` dict `{text,upvoteCount}` is decoded into
    clean `answer` text plus a separate `upvote_count`.)
- news: `title, url, site, description, age, page_age, extra_snippets[], source, thumbnail`
- discussion: `title, url, site, description, type, forum, is_source_local,
    is_source_both, extra_snippets[], breadcrumb, family_friendly,
    num_answers, score, question, top_comment`
    (round-5: local-community signals; round-7: `num_answers` (reply count),
    `score` (upvote score string), `question` (post question text cleaned),
    `top_comment` (best answer snippet cleaned), and `breadcrumb`.)
- video: `title, url, description, age, page_age, creator, channel, duration,
    requires_subscription, tags[], author, author_url, views, thumbnail,
    thumbnail_original, site, breadcrumb, family_friendly, is_source_local` — round-5 adds `views` (play count, a ranking signal)
    and `author_url` (creator channel/profile URL). (`thumbnail_original` is the
    direct un-proxied source URL; `thumbnail` is Brave's proxied image URL.)
- image: `title, page_url, image_url, width, height, source, thumbnail,
    thumbnail_width, thumbnail_height, placeholder, breadcrumb, site,
    confidence, page_fetched`
    (+ round-3 `confidence` / `page_fetched`; round-7 `breadcrumb` / `site`) — `placeholder` is a
    low-res blur-up preview for lazy loading.
- faq:    `question, answer, url, site` + (round-8: `title`, `breadcrumb`)

Web mode `search()` reply additionally carries `news`: the list of current
news stories Brave embeds in a web SERVER (below `results`/`web`). `_query_diag`
now also exposes `state` and `postal_code` (empty when unchanged).

`infobox` (knowledge panel): `title, url, category, description, long_desc,
attributes, website_url, profiles[], images[], ratings[], found_in_urls[],
providers[]`.  `providers` carries the provenance of the facts as
`[{type, name, url, img}]` (e.g. Wikipedia) — round-4 addition.

`query` (diagnostics): `original, altered, spellcheck_off, is_navigational,
is_news_breaking, bad_results, should_fallback, more_results_available,
country, header_country, city`.  `altered` is Brave's corrected spelling when it
fixes a typo (differs from `original`); `run()` renders "Did you mean: '...'?" .
Round-7 `query` additionally carries `show_strict_warning`, `is_geolocal`,
`local_decision`, and `local_locations_idx`.

`summarizer` (only when `summary=True`): `type, query, country, language,
safesearch, results_hash, experimental_inline_refs, deep_link` — a deep-link
payload for Brave's self-contained AI answer.

Web-mode `mixed`/`top_results` pools now also include `news` and `faq`
sections, so the blended/on-screen order materialises them (round-4, verified
with live `news` entries in the `main` column).

## `mixed` — Brave's authoritative blended order

Brave returns a `mixed` struct with `main`, `top`, `side` columns, each an
ordered list of entries:

```json
[{"type":"web","index":0,"all":false},
 {"type":"infobox","index":0,"all":false},
 {"type":"discussions","all":true},
 {"type":"videos","all":true}, ...]
```

The skill materialises these into `mixed.columns.<col>` (each entry tagged
`{type, index, all, item}`) and emits `mixed.main_rank` (count of web results at
the head of `main`). `top_results` is a convenience flattening of `main` keyed by
type — use it as "the on-screen result order".

## Advanced params by mode

- **web only**: `grep` (PCRE on sources), `goggles_id`, `extra=True`,
  `discussion_count`, `video_count`, `movie_count`, `summary=True`, `offset`.
- **web + news + video**: `freshness`, `count`, `country`, `search_lang`,
  `safe_search`, `spellcheck`, `timeout`.
- **image**: `unit` (`px`|`em`).
- **all**: `count` (per-section), `freshness` (applied to news).

## Round-6: `locations` map + geo-localisation + full freshness

### `locations` (map / POI)

On local-intent web queries (restaurants, cafes, services…) Brave auto-appends
a `locations` section. `search()`/`run()`/`all` now surface it as a
normalised list (was previously dropped):

```json
{"title","url","description","type":"location_result",
 "latitude","longitude","coordinates":[...],
 "address" (street display), "postal_address":{...},
 "opening_hours":["Friday 07:00-18:30", ...] (current day, cleaned),
 "phone","categories":[...], "cuisine":[...], "timezone",
 "thumbnail","pictures":[{"src","original"}...],
 "provider_url","zoom_level","family_friendly"}
    (round-8: `week` full schedule, `price`, `rating`, `icon`, `id`,
    `timezone_offset`, `website`, `profiles`)
```

`run()` renders a capped `[Locations]` block (first 6 + "… N more");
`search()` returns all of them. `locations(query, **kw)` returns just the
places + a readable `render`. Verified live: "coffee shop new york" → 100 POIs
each with coordinates + phone + Friday hours.

### X-Loc-* geo-localisation

Pass any of `city`, `state`, `state_name`, `postal_code`, `country`,
`timezone`, `latitude`, `longitude` either as a `loc` dict on `search`/`run`,
or as the typed `city/state/postal_code/...` args on `run`, or via
`near(query, **loc)`. They become X-Loc-* request headers so results localise
even for a non-"near me" query. Verified live: `near("best cafes", city,
state="San Francisco"/CA)` returns SF cafes+map with addresses/phones/hours
while the same bare query returns the default region.

### Expanded `freshness`

`pd` / `pw` / `pm` / `py` (24h / 7d / 31d / 365d), legacy `pd_1d`/`pd_1w`/
`pd_1m`/`pd_1y`, and a custom inclusive date range `YYYY-MM-DDtoYYYY-MM-DD`
(e.g. `2025-01-01to2025-03-01`). Validation accepts all of these.

### Other new params (all verified live)

`ui_lang` (e.g. fr-FR), `units` (metric|imperial), `operators` (False turns
`site:`/`inurl:` into literal text), `include_fetch_metadata` (bool), and the
modern `goggles` (a goggle `.goggle` URL / inline definition or a comma-joined
list of up to 3; `goggles_id` deprecated still accepted). `result_filter` now
also accepts `locations` / `news`.

## Gotchas

- Unknown `mode` and invalid `safe_search`/`freshness` raise a clear `ValueError`
  with the valid set.
- `offset` window is `0 <= offset <= 9` (Brave 422s at `offset >= 10`). Verified live: every count accepts offsets 0..9.
- For more than a single page, call `paged()` (advances offset, dedupes by URL).
- `research(query, queries=None, mode=web|news|video, count=6, total=12,
  summary=True, max_pages=4, **kw)` → one fused package:
  `{query, queries, results[], n, sources[], infobox, faq[], videos[],
  discussions[], summary_deep_link, per_search[], exhausted, render, duplicates}`.
  It pages the primary query, merges any `queries` variations URL-deduped, and
  carries the first page's knowledge extras + AI-summary deep-link
  (`summary=True`). `image`/`local`/`all` are single-shot → falls back to
  `search()` per query.
- `software(package, count=10, country=..., safe_search=..., timeout=...)`
  (round-5) runs web search and returns only the `subtype == "software"`
  registry hits: `{query, results (each with `.software`), n, registry, versions,
  render}`. `registry` = `{"pypi":[names],"npm":[names]}`, `versions` =
  `[{name, version, url, code}]` for exact-version hits. Verified live:
  `software("jsonschema pypi")` → v4.26.0 [pypi] + code repo, plus siblings.
- `paged(query, mode=web|news|video, count, total, **kw)` returns
  `{results, n, pages, exhausted, web_meta}`; `merge(*searches)` returns a
  URL-deduped flat list tagged `_query`.
- Unsupported `local` → transparently falls back to `web` and flags `fallback`.
- News often needs `freshness` for timely/meaningful results.
- Result `count` caps: web up to ~20, image/video ~10, news ~10.
- `faq`, `mixed`, `summarizer`, `videos`, `discussions`, `news` are optional —
  guard with `.get(...) or []`.

Errors:

- `BraveError` (a `RuntimeError`) carries `.category` ∈ `auth | param |
  rate_limit | http | network | timeout` and `.details` (field-level list for
  validation errors), plus `.is_auth` / `.is_rate_limited` helpers.
- Verified live against `api.search.brave.com`:
  - invalid `X-Subscription-Token` → **HTTP 422** with
    `error.code = "SUBSCRIPTION_TOKEN_INVALID"` (NOT 401/403) → `auth`.
  - invalid parameter (e.g. `offset=10`, bad `goggles_id`) → HTTP 422 with
    `error.code = "VALIDATION"` and `error.meta.errors[]` (each `{field,
    message, input}`) → `param`.
  - rate limiting → HTTP 429 → `rate_limit`.
  - unexpected params (e.g. `foo_bar=1`) and invalid `freshness` are silently
    ignored by the API (return 200) — validated locally instead.
- Transient failures (network blips, 429, 5xx) are retried once with a short
  backoff; definitive auth/validation errors are raised immediately. `_request`
  takes an optional `retries` (default 1 extra attempt).

## Round-7: breadcrumbs, discussion engagement, page text

### Result enrichment

Every web/news/video/discussion/image result now carries:

- `breadcrumb` — the `meta_url.path` decoded into a clean trail (`docs / api / x`).
- web: `thumbnail_original` (unproxied URL) + `thumbnail_is_logo`; `author_meta`
  (name, url, thumbnail per author); `contact_points` (org phone list);
  `creative_work` (name + rating `value`/`best`/`reviews`); inline `location`
  (map/POI attached to a single web result); `inline_faq` (Q&A sub-items).
- video: `site` + `breadcrumb` + `family_friendly` + `is_source_local`.
- discussion: `breadcrumb` + `family_friendly` + engagement from Brave's `data`
  (`num_answers`, `score`, `question`, `top_comment`).
- image: `breadcrumb` + `site` + `thumbnail_width`/`thumbnail_height`.

### Query diagnostics (round-7)

`d["query"]` now includes `show_strict_warning` (bool), `is_geolocal` (bool),
`local_decision`, and `local_locations_idx`.

### `mutated_by_goggles`

In web-mode `search()`, the `mutated_by_goggles` key is a dict
(`{"web": bool, "videos": bool, "discussions": bool}`) reporting whether a
Goggles filter changed each section's result set.

### `forums(query, ...)` — discussion threads with engagement

Runs a web search and returns *only* the `discussions` section (with
`num_answers`, `score`, `question`, `top_comment`, `breadcrumb`, `forum`),
plus a readable `render`. Verified: "new york pizza" → 11 threads with
answers/score/question/top.

### `probe(url, max_chars=8000, timeout=15)` — readable page text

```python
{url, status, title, text, chars, truncated, final_url, content_type}
```
Raised as `BraveError` (category `network`/`http`) on failure.

### `crawl(urls, max_chars=4000, timeout=15)` — batch page fetch

```python
{results: [{url,status,title,text,chars,truncated,error}], total, ok}
```
Per-item `error` is captured rather than raising.
## Round-8: full-context answers + media lookups (verified live)

Round-enrichment on existing items:

- **web**: `publisher_url`, `publisher_logo` (+`_original`), `publisher_type`,
  `author_types` (per-author role), `paywall` (True when `isAccessibleForFree` is
  false), `fetched_content_timestamp`.
- **news/video/discussion**: `fetched_content_timestamp`; discussions also
  carry `language`.
- **faq**: `title` + `breadcrumb` (source page identity).
- **infobox**: `position` + `label`.
- **locations**: `week` (full 7-day schedule), `price` ("$".."$$$$"), `rating`
  `{value,best,reviews,is_tripadvisor}`, `icon` (venue category icon),
  `id` (POI id), `timezone_offset`, `website` (provider_url non-empty),
  `profiles`. `opening_hours` (current-day) unchanged.

### `open_now(place, now=None)`

`{"open": bool|None, "now": "HH:MM", "day": "Friday", "window": "07:00-16:00",
 "note": str|None}` — resolves the venue `timezone` clock (via stdlib
`zoneinfo`, falling back to caller local on load/parse failures) and compares it
against `opening_hours`. `open` is `None` when today's hours are absent.

### `headlines(topic, count, freshness="pd_1w", **kw)` / `clips(topic,...)` /
`pictures(topic, ...)`

Media mirrors of `forums()`/`locations()`:
`{query, results, n, count, render, search}`. Headlines returns `/news` items
(source, age, thumbnail, publisher_url); clips returns `/videos` items (creator,
channel, duration, tags, views when available); pictures returns `/images` items
(image_url, dimensions, source). Each keeps the underlying structured reply
under `search`.


## Round-9: schema.org/schema.org structured data + subtype-aware lookups (verified)

### Web enrichment

Round-9 adds raw schema.org structured data on web results:

- **`recipe`** — `{title, url, domain, time, prep_time, cook_time, ingredients,
  instructions[], servings, category, cuisine, thumbnail, thumbnail_original,
  rating: {value, best, reviews, is_tripadvisor}}`
- **`product`** — `{name, url, price, offers: [{url, price, price_currency}],
  rating: {value, best, reviews, is_tripadvisor}, description, thumbnail,
  thumbnail_original}`
- **`movie`** — `{name, description, url, release, directors: [{name,url}],
  actors: [{name,url,thumbnail}], genre[], duration, rating, thumbnail,
  thumbnail_original}`
- **`content_type`** — file-type tag ("pdf", etc.)

All three schema blocks appear on the normalised web item next to the existing
`software`/`qa`/`cluster` keys. Verified live.

### Subtype-aware lookups

- `recipes(query, count=8, ...)` → `{"query", "results", "n", "count", "render", "web"}`
  — filters web results to items with a `recipe` block
- `products(query, count=8, ...)` → same shape, items with `product`
- `movies(query, count=8, ...)` → same shape, items with `movie`

Each runs `search(mode="web")`, filters, and builds a readable `render` with
timings/servings, prices/ratings, directors/actors as available.

### `run()` rendering

`_section_lines` renders the rich schema inline on web results:

- Recipe: `🍳 Recipe: time 45:00, prep 15:00, cook 30:00, serves 24, 124 cal, American`
  plus `Σ 16 ingredients: <preview>`
- Product: `🛒 Product: $368.0 ★4.5 (1852 reviews)`
- Movie: `🎬 Movie: Jul 16, 2010, Action, Adventure, ★ 8.8, 02:28:00` + `Directed:` + `Starring:` lines
- `content_type`: `[pdf]` tag line

## Round-10: Rich Search + Local POIs / Descriptions

### Rich Search (`/res/v1/web/rich`)

Enable with `enable_rich_callback=1` on web search.  When the query maps to a
rich intent, the web `search()` result carries:
```json
{"type": "rich", "hint": {"vertical": "weather|stocks|...", "callback_key": "..."}}
```
Fetch the payload with `GET /res/v1/web/rich?callback_key=...`.

The rich payload `results[]` array carries one dict per vertical; each has
`{type:"rich", subtype:"...", provider:{...}}` plus a subtype-specific
nested block (`weather`, `stock`, `cryptocurrency`, `currency`,
`calculator`, `definitions`, `unitconversion`, `unixtimestamp`,
`american_football`/`baseball`/..., `formula1`).

`_rich_result()` normalises each to `{subtype, provider, data}` where `data`
is the flattened, typed object.

### Local POIs (`/res/v1/local/pois`) and Local Descriptions

After a web search returns `locations`, each has `id` (valid ~8h):
```
GET /res/v1/local/pois?ids=X&ids=Y
   → { type: "local_pois", results: [...] }
GET /res/v1/local/descriptions?ids=X&ids=Y
   → { type: "local_descriptions", results: [...] }
```

The POI deep record adds: `reviews` (full user reviews w/ rating + author),
`pictures`, `contact.email`, `distance.equals`, `profiles`, and the full-week
`opening_hours.days` schedule. `Local Descriptions` returns an AI-generated
place blurb with markdown headings.


---

## Round-11: Dedicated Place Search + domain/file/news conveniences

### Key endpoints
| Function | Endpoint | Status |
|----------|----------|--------|
| `place_search()` | `/res/v1/local/place_search` | **LIVE** (verified) |
| `domain(name)` | `site:<name>` on `/res/v1/web/search` | LIVE (client-side `site:` wrapper) |
| `find_files(q, type)` | `/res/v1/web/search` + `filetype:` op + `content_type` | LIVE |
| `news_cluster(topic)` | `/res/v1/news/search` (grouped client-side) | LIVE |

### Place Search (`/res/v1/local/place_search`)
```
GET /res/v1/local/place_search?q=coffee+shops&latitude=37.7749&longitude=-122.4194
       &radius=2000&count=10
```
Versus `location=` name string instead of coordinates:
```
GET ...&q=coffee+shops&location=san+francisco+ca+united+states&count=6
```
Query params: `q` (optional → explore mode), `latitude`+`longitude` (paired),
`location`, `radius` (meters *bias*, not a hard cutoff), `count` (1..100),
`country`, `search_lang`, `ui_lang`, `units` (metric/imperial),
`safesearch` (moderate|strict, default strict), `spellcheck`, `geoloc`
(`<lat>x<lon>` for distance values).

Response `{type:"locations", results, cities, countries, regions,
neighborhoods, addresses, streets, mixed, location}`.

- `results` — `LocationResult` rows (name, url, provider_url, description,
  coordinates, postal_address, opening_hours, contact, rating, price_range,
  distance[present on address/street/geoloc contexts], categories,
  serves_cuisine, thumbnail, pictures, profiles, timezone, icon_category, id).
- `cities`/`countries`/`regions`/`neighborhoods` — geographic group buckets
  (`type`, `name`, `country`, `coordinates`, `thumbnail`).
- `addresses`/`streets` — `{type: "address"|"street", name, coordinates,
  zoom_level, distance, postal_address, pois, pois_nearby}`.
- `mixed` — ordered list of `{type, index, all}` refs describing how to
  interleave `results`/`cities`/... on a SERP.
- `location` — resolved anchor `{coordinates, name, country}`.

**Skill shape** — `place_search()` returns
`{"query", "results":[normalized POI rows], "cities":[...], "countries":[...],
"regions":[...], "neighborhoods":[...], "addresses":[...], "streets":[...],
"mixed":[...], "resolved":{name,country,coordinates}, "n", "count", "render"}`.
POI rows reuse the `_location_item` normalizer (same fields as `locations()`
web-section output), so `open_now()`, `pois()`, `poi_descriptions()` work on
them too.

### `domain(name)`, `find_files`, `news_cluster` (client-side conveniences)

`domain(name)` runs `search(f"site:{name}")` with `text_decorations=False` and
returns `{domain, query, results, summary, infobox, n, count, render, search}`.

`find_files(query, filetype="")` runs web search with the `filetype:` operator
(if given), then filters results whose `content_type` matches. Brave's file
type index covers PDF/DOCX/PPT (verified live) but e.g. `xlsx`/`csv`/`zip`
return few or no document hits (`content_type` may be absent even on direct
file URLs).

`news_cluster(topic)` runs news search and groups the flat article list by
`source`/`site` client-side, returning `{groups:[{source,count,articles}],
n, n_sources, render, search}`. It is *not* a native server-side cluster.

### Documented-as-absent endpoints (honest gaps)

Brave's web-search **API** exposes **no** trending or routing endpoint:

| Requested | Probe result | Verdict |
|-----------|--------------|---------|
| `week_trending()` / any `/res/v1/trends*` path | HTTP 301 → HTML dashboard (no JSON) | **Absent** — no trends API |
| `local_directions()` / `/res/v1/local/directions` | HTTP 200 but HTML dashboard body | **Absent** — no routing/directions API |
| `news_cluster()` server-side clustering | News results are a flat list (no `related`/`cluster` field) | **Absent** — done client-side |

### Plan-gated endpoints (present in docs, `OPTION_NOT_IN_PLAN` on this key)

- `llm/context` (POST/GET `/res/v1/llm/context`) — pre-extracted LLM-ready
  context with token budgets. Returns `error.code == "OPTION_NOT_IN_PLAN"`
  (`400`) on the standard Search plan.
- `answers` (`/res/v1/chat/completions`, OpenAI-compatible) — Needs `Answers`
  plan.
- `suggest/search` (`/res/v1/suggest/search`) — Needs `Autosuggest` plan.
- `spellcheck/search` (`/res/v1/spellcheck/search`) — Needs a separate option.

These are intentionally **not** surfaced as live functions in the skill (they
can't be verified with real data on the current key); they are documented here
so a deployment with the extra plans knows the endpoint paths.


---

## Round-12 addendum

### New convenience functions (client-side, no new endpoint surface)

- `summarize_page(url, *, max_chars=20000, max_points=5, timeout=45)` — fetch a
  page with `probe()` and return a deterministic extractive digest: title, lead
  paragraph, top `max_points` info-dense sentences, raw cleaned text, and a
  `readable` render. No model call. On non-200 fetch it returns the `probe()`
  error dict.
- `mosaic(query, *, count=4, freshness="pm", safe_search, country, search_lang,
  timeout, images=True, **kw)` — fans one query across web + news + video +
  image in a single call, returns each corpus as its own labelled pool plus a
  blended `render` and aggregate `n`.

### Image batch cap (corrected)

`/res/v1/images/search` supports `count` up to **200** per request (default 50).
`search(mode="image", count=...)` and `pictures(count=...)` pass the full range
(capped at 200). Verified: count=200 → 200 results; pictures(count=150) → 150.

### Verified endpoint constraints

- `/res/v1/suggest` — **301** redirect to the HTML dashboard (no JSON).
- `/res/v1/suggest/search` — `400 OPTION_NOT_IN_PLAN` (Autosuggest plan).
- `offset >= 10` on web/news — **HTTP 422** (Brave's hard 0..9 window).
- `aspect_ratio` / `min_width` / `min_height` on images — accepted but **ignored**
  (identical results); not documented params, so not surfaced.
- `estimated` recap fields — absent from web responses on this plan.


## Round-13: cross-format fusion, image/video params, convenience briefs

### `mosaic()` return shape (round-13)
`mosaic(query)` return dict adds a `locations` key (normalised map/POI
`location_item` rows from the web corpus, for local-intent topics). Structure:
```json
{"query", "web":[...], "news":[...], "videos":[...], "images":[...],
 "locations":[location_item...],
 "infobox":{...}|null, "faq":[...],
 "n": count incl. locations, "render": "MOSAIC 'q'...",
 "searches": {"news":n,"video":n,"image":n,"locations":n}}
```

### Video item `live` flag (round-13)
`_video_item` adds `"live": true` when the `video` block has no `duration`
(24/7 & live streams expose none). Low-confidence proxy; some recorded videos
also omit `duration`. Backward-compatible (new key; absent → None for no
`video` block).

### Image endpoint params (round-13)
`search(mode="image")` and `pictures()` accept:
- `property` = `any` | `commercial` | `non-commercial` (OECD licensing)
- `search_type` = `all` | `transparent`

Verified live: all values return HTTP 200; on this plan they are pass-through
(results identical regardless of value) — forward-compat for licence tiering.

### place_search `cate` (round-13)
`place_search(..., cate="cafe")` passes `cate` to the Place Search endpoint.
Verified live: 200, pass-through (results not visibly filtered).

### New funcs
- `newsflash(topic, count, freshness="pd_1w", ...)` → news-headline digest.
- `explain(query, ...)` → Markdown research brief (mosaic + summarize_page).
- `trending_topics(...)` → documented-absent note (no endpoint; all 301).

### Absent / pass-through findings (round-13)
- `/res/v1/{web,news}/trending`, `/trending/search`, `/web/top_stories`,
  `/news/discover`, `/web/trending_search` — **301** to docs HTML.
- `video length` (short/medium/long) — accepted (200), identical results (not honored on this plan).
- `image freshness` — accepted (200), identical results (not honored on this plan).


### Round-14: news depth, parallel batch, review, BFS worker
- `_news_item` now surfaces raw `breaking` / `is_live` / `is_source_local` /
  `is_source_both` flags from the **web-embedded news section** (probed live:
  `/res/v1/web/search` returns `news.results[].breaking` (bool) and
  `.is_live`; the flat `/news/search` items do NOT carry these — they only lack
  the breaking/live keys).
- `news_breaking(topic, count, freshness, ...)` → web search, filter to
  `breaking=true` items. `{"query","results","n","total","render","search"}`.
- `news_beams(topic, count, freshness, ...)` → web search, group embedded news
  by publishing outlet (source or page host), report breaking/live counts +
  top articles. `{"query","results","beams","n_items","n_publishers"}`.
- `batch(queries, mode, count, concurrency, timeout, **kw)` → thread-pool over
  `search()`; each query's error captured per-slot. `{"results","ordered"}`.
- `article_review(query, count, max_chars, positive_hint, negative_hint)` →
  search→crawl→heuristic positive/negative lexicon bucket → verdict.
- `search_worker(query, depth, breadth)` → BFS over `search()` seed + Brave
  `cluster` sitelinks (and in-page http(s) href fallback) + `crawl()` full
  text. `{"pages", "visited", ...}`.
- `related(...)` → **documented-absent** (no related-queries block in the web
  JSON; `/res/v1/suggest/*` 301 → HTML / OPTION_NOT_IN_PLAN).

### New requirements
- `import threading` (for `batch`).
## Round-16 addendum
New public helpers (all live-verified; each is a client-side composition over
the verified `search()`/`recipes()` surfaces — no new endpoint):
- `infobox(query, *, count=10, country, safe_search, timeout, **loc)` ->
  `{query, entity, entity_type(label||category), page, website_url, provider,
  providers, facts:[{field,value}], n, render, web}` — re-interprets the web
  `infobox.attributes` `[field, html]` rows into clean `{field,value}` facts;
  `entity=None/facts=[]` when the SERP has no knowledge panel.
- `thumbnails(query, *, count=5, video=True, image=True, timeout, **kw)` ->
  `{query, count, n, items:[{kind,title,url,thumbnail,thumbnail_original|
  image_url, duration|width|height}], video:[], image:[], render}` — flattens
  just the thumbnail URLs across the video + image corpora.
- `drinks(query, *, count=12, country, safe_search, timeout, **loc)` ->
  `{query, results:[drink recipe items with .recipe], n, render, web}` — a
  `recipes()` subset filtered by `DRINK_CATEGORIES` (schema category) or a
  title/domain cocktail-signal lexicon; light title-dedupe.

Honest absent / pass-through findings this round (live-probed):
- `/news` has no `news_regions`/trending-geo (reply keys: query/results/type);
  `region`/`news_region`/`country` params -> 200 but identical results (pass-through).
- `/news` items carry no `newsvine` source-category field.
- discussion `data` has no `joinCount` and no `top_answers`/`top_comments`
  arrays (only forum_name/num_answers/question/score/title/top_comment);
  `forums_expand()` won't work because Reddit (the dominant thread source)
  serves a thin accessibility page to scrapers.
- video `thumbnail` has only `src`/`original`; video block only
  author/creator/duration/publisher (no playability/variant field); image
  `properties` only url/placeholder/width/height (no `image_cache`/
  `image_provider`).

