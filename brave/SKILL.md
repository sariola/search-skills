---
name: brave
description: >-
  Advanced Brave Search, agent-friendly. `await brave` readable, `await brave.search`
  structured. Web/news/image/video/local modes; `infobox()` fact table, FAQ, mixed order,
  cluster sitelinks, qa+upvote, breadcrumbs, author_meta, publisher/paywall; locations
  POIs + X-Loc-* geo via near()/locations();
  forums()/probe()/crawl()/headlines()/clips()/pictures()/software();
  recipes/products/movies schema; rich()/pois()/poi_descriptions();
  paged()/merge()/research(); pl
  ace_search()/domain()/find_files()/news_cluster()/summarize_page()/mosaic()/newsflash()/explain(
  ). DEV (round-17):
  docs()/code_result()/qna()/cve_lookup()/breaking_change()/dev_digest(); community
  reddit()/github_repos()/github_issues(); docker_aliases() honest-absent. round-14:
  news_breaking()/news_beams()/batch()/article_review()/search_worker(). round-15:
  structured()/place_detail()/news_live()/age_meta. round-16: thumbnails()/drinks().
  BRAVE_API_KEY. round-18:
  pkg_lookup()/pkg_security()/error_solution()/stack_trace()/dep_signal().
---

# Brave Search (advanced, agent-friendly)

Independent web search through the Brave Search API
(`https://api.search.brave.com`) using `BRAVE_API_KEY` / `BRAVE_SEARCH_API_KEY`.

Designed to be powerful *and* easy to drive from an agent:

- `await brave(...)` / `await brave.run(...)` → readable markdown (preferred
  when the next step is a model).
- `await brave.search(...)` / `await brave.brief(...)` → **token-budgeted JSON**
  (`view="agent"` default): title/url/snippet/date plus any non-empty schema
  blocks. No duplicated `mixed`/`top_results` dump. Pass `view="full"` only
  when you need the raw SERP shape.
- `await brave.batch([...])` → one-way fan-out; each item includes `.search`.
- `await brave.modes()` → list every mode and advanced option.

Agent contract: one name per fact, nothing dropped. A hit is
`{title, url, date, snippet, extra?, author, publisher, kind, …}` plus
schema/sitelinks/qa when present. Dates keep time; lists are not capped.
`view="full"` is only the raw SERP aliases (`mixed`/`top_results`/empty keys).


## Everything works with and without `await` (dual sync/async)

Every public callable is **both** synchronous and async-compatible:

```python
d   = brave.search("clojure")      # sync: a real dict
d   = await brave.search("clojure")  # async: the same plain dict
txt = await brave("clojure")           # readable markdown (run)
```

- `await fn(...)` yields the **plain** value (`dict` / `str` / list) — nothing
  stays wrapped.
- `fn(...)` without `await` returns the same value usable directly: dicts stay
  dicts for `[]`, `.get()`, `isinstance`, `**`-unpack and iteration (they are
  thin dict subclasses that are also awaitable).
- Internal cross-calls and every normaliser are unaffected.

So an agent in IPython can freely `await brave.search(...)` or `brave.search(...)`.


## Auth

Key resolution is automatic across scopes: process env → dotenv files
(`~/.prime/agent/.env`, `~/.prime/.env`, `~/.config/prime/env`, `$PRIME_DOTENV`,
cwd `.env`) → the `/login` key store; the found key is cached and exported so
CLI/subprocesses inherit it. Raises one clear, actionable message only if absent
everywhere — never an unexplained "unconfigured" wall.

## Call directly from the kernel

Readable text (the common path):

```python
await brave("Rust programming language")                  # web: infobox + results + videos + faq
await brave("OpenAI news", mode="news", freshness="pd_1w", count=10)
await brave("mountain lake wallpaper", mode="image", count=8, unit="px")
await brave("rust video tutorial", mode="video")
await brave("pizza in NYC", mode="local")                 # falls back to web on unsupported keys
await brave("physical DBs + news + talks", mode="all")    # merges web+news+video
```

Structured JSON for scripting:

```python
d = await brave.search("clojure", mode="web", count=8)   # view="agent" default
d["results"]        # slim web dicts (title/url/description/date/...)
d["infobox"]        # knowledge panel if present
d["faq"] / d["videos"] / d["news"] / d["discussions"]   # only when non-empty
# historical SERP dump (mixed/top_results/empty fields):
full = await brave.search("clojure", view="full")
```

`brave.search(...)` web results are enriched with the raw signals an agent needs
for cross-checking: `page_age` (ISO), `article_date`, `author` (list),
`publisher`, `organization`, `family_friendly`, `favicon`, `thumbnail`,
plus the round-3 (and round-4) extras surfaced from the raw API:

- `cluster` / `cluster_type` — sitelinks: sibling sub-pages of a result
  (common on navigational SERPs like a brand homepage). Each cluster entry is
  `{title, url, description}`; `cluster_type` labels the group (`"generic"`).
- `is_source_local` / `is_source_both` — Brave's local/biographical signals.
- `deep` — SERP navigation buttons (e.g. a Wikipedia result's TOC links) as
  `[{title, url}, ...]`.
- `qa` — an embedded question/answer pair attached to a result (`question`,
  `answer`, `url`, **plus `upvote_count`** — the raw `qa.answer` is a dict
  `{text, upvoteCount}`; the skill extracts the clean text and the vote count).
- `embedded_video` — an in-line video (`duration`, `thumbnail`) on a web result.
- `profile` — `{name, url, long_name, img}` for the underlying site.
- **video** results carry `thumbnail_original` (the direct, un-proxied source
  URL, useful for embeds that reject Brave's image proxy).
- **image** results carry `placeholder` (a low-res blur-up preview for lazy
  loading) plus `width`/`height`, `confidence`, and `page_fetched`.
- **infobox** now also carries `providers` — the provenance of the knowledge
  panel facts (`[{type, name, url, img}]`, e.g. Wikipedia).
- web-mode `mixed`/`top_results` now pool the `news` and `faq` sections (part
  of Brave's real on-screen blended order), not just web/videos/discussions/
  infobox.

Web mode (`await brave.search(..., mode="web")`) additionally surfaces a
top-level `news` list when Brave embeds breaking/current news in the SERP
(also rendered as a `[Breaking/News]` block by `run()`).

Brave silently corrects misspellings; the corrected text is available as
`d["query"]["altered"]`:

```python
d = await brave.search("clojoure functional")       # typo
d["query"]["altered"]   # 'clojure functional' — the spelling Brave actually searched
```
`run()` renders any correction as a **"Did you mean: ...?"** line automatically.

Get the **on-screen** result order for a query:

```python
d = await brave.search("clojure", mode="web")
blended = d["top_results"]           # already in Brave's displayed order
```

Want more (or fewer) embedded videos/discussions on web queries?

```python
await brave.search("clojure", mode="web", video_count=3, discussion_count=5)
```

Want the self-contained Brave AI answer link for a query?

```python
d = await brave.search("clojure", mode="web", summary=True)
d["summarizer"]   # {'query':..., 'results_hash':..., 'deep_link':...} AI-answer payload
```

### Fetch the whole corpus for a query — `brave.paged(...)`

Brave's real page window is `offset` 0..9 (offset >= 10 → HTTP 422), so a single
`search()` tops out around 10 results per query. `paged()` advances the offset
for you, merges the pages, dedupes by URL, and stops when the target `total` is
reached or Brave signals it has no more results:

```python
d = await brave.paged("go programming language", mode="web", count=5, total=15)
d["results"]       # up to ~10 distinct results (full window)
d["n"], d["pages"] # counts you fetched / how many HTTP calls
d["exhausted"]     # True when Brave ran out or the window was fully walked
d["web_meta"]      # web mode: first page's infobox/faq/videos/discussions/summarizer
```

`paged()` works for `web`, `news`, and `video`. Handles empty/short pages and
the offset window gracefully.

### Mix multiple queries — `brave.merge(...)`

```python
a = await brave.search("clojure web framework", count=5)
b = await brave.search("clojure concurrency", count=5)
seen = brave.merge(a, b)          # flat, URL-deduped, each item tagged `_query`
```

### Fuse everything into one call — `brave.research(...)`

`research()` is the "give me everything on this topic" entry point: it pages the
query (and any explicit `queries` variations) with `paged()`, merges them
URL-deduped, and carries the first page's knowledge panel / FAQ / embedded
videos/discussions / **AI-summary deep-link** into a single coherent package
(`search()`+`paged()`+`merge()`+`run()` combined by hand, minus the plumbing).

```python
d = await brave.research("clojure concurrency", summary=True)
d["results"]              # deduped results across the page window
d["infobox"] / ["faq"] / ["videos"] / ["discussions"]   # first-page extras
d["summary_deep_link"]    # Brave AI answer deep-link (summary=True)
d["per_search"]           # per-query metrics {query,pages,n,exhausted,error?}
d["render"]               # fully formatted readable text
```

Works for `web`/`news`/`video` (paginated). `image`/`local`/`all` are
single-shot and fall back to a plain `search()` per query. Pass `queries=[...]`
to research several phrasings at once and get one merged output.

### Package/library registry lookup — `brave.software(...)`  (round-5)

When a query's top hits are software packages, Brave tags them
`subtype == "software"` and carries registry identity (`name`, `version`,
`is_npm`/`is_pypi`, `codeRepository`, `datePublished`, `programmingLanguage`).
`software()` runs web search and returns just those *software* results,
enriched with the metadata. Use it for "what is this package / which version /
which registry / where is the code":

```python
d = await brave.software("jsonschema pypi", count=12)
d["results"]      # software-subtype web results, each with a `.software` dict
d["registry"]     # {"pypi": [names...], "npm": [names...]}
d["versions"]     # [{name, version, url, code}, ...] for exact-version hits
d["render"]       # readable output (name, version, [registry], code, url)
```

Verified: `software("jsonschema pypi")` → v4.26.0 on PyPI with the
python-jsonschema code, plus sibling packages (jsonschema-specifications
2025.9.1, jsonschema-rs, ...); `software("fastapi pypi")` → fastapi-slim
0.129.1. Appending a registry hint (`"pypi"`, `"npm"`, registry name) makes the
needle land on software results; a package with no software-registry hit
returns `results: []` with a friendly hint.

### Round-5 enrichment on results & safety flags

Every web/video/discussion result now carries extra live signals:

- **web items** — a distinct `subtype` (`article`/`qa`/`software`/`generic`/
  `web`) separate from `type`, and a `software` dict on registry hits.
- **video items** — `views` (play count, e.g. `14869488`) and `author_url`
  (creator channel/profile link). `run()` renders both (`10,460 views`,
  `http://www.youtube.com/@LoFiAlpaca`).
- **discussion items** — `is_source_local` / `is_source_both` (local-community
  / biographical signal, e.g. Reddit threads).
- **Safety flags** — `family_friendly` (bool) on web replies rates the whole
  SERP; `might_be_offensive` (bool) on `video`/`image` replies flags adult or
  sensitive media (from Brave's `extra`). Use them to gate content in
  child-facing / safe modes.

## Round-6: map `locations` + geo-localisation (NEW)

Brave auto-surfaces a **`locations`** map/POI section on web queries with a
place intent (restaurants, cafes, services, …). Each place is rich: street
address, phone, today's open hours, coordinates, cuisine, category, picture,
timezone. The skill flattens them into the `locations` key on `web`/`all`
search replies:

```python
d = await brave.search("coffee shop new york", mode="web")
d["locations"]   # [ {title, url, address, phone, opening_hours, latitude,
                 #    longitude, cuisine, categories, timezone, thumbnail,
                 #    pictures, provider_url, zoom_level, ...}, ... ]
```

For local intents you also get **geo-localised results on any query** via the
X-Loc-* request headers — no "near me" phrasing needed (verified live:
`near("best cafes", city="San Francisco", state="CA")` returns SF coffee shops
with addresses/phones/hours even though the query has no location). Three ways
to ask:

```python
# 1) `loc` dict on search/run
d = await brave.search("tacos", loc={"city": "Austin", "state": "TX", "postal_code": "78701"})

# 2) typed flags (also, the CLI surface)
r = brave.run("coffee shops", count=3, city="Miami", state="FL")

# 3) dedicated helpers
near = await brave.near("pizza", city="Brooklyn", state="NY")   # same shape as search
places = await brave.locations("pizza brooklyn", city="Brooklyn") # {"query","results","n","render"}
near["locations"]   # map/POIs for the query
```

`loc`/`near` accept: `city`, `state`, `postal_code`, `timezone` (IANA),
`latitude`, `longitude`, `country`, `state_name`. `run()` renders a capped
`[Locations]` block (first 6 + "… N more"); `locations()` renders all of them.
All verified live against `api.search.brave.com`.

### Round-6 search params (all verified live)

- **`freshness`** now accepts the full set: the shorthands `pd`/`pw`/`pm`/`py`,
  the legacy aliases `pd_1d`/`pd_1w`/`pd_1m`/`pd_1y`, and a **custom date range**
  `YYYY-MM-DDtoYYYY-MM-DD` (e.g. `2025-01-01to2025-03-01`).
- **`ui_lang`** — UI language code (e.g. `fr-FR`, `en-US`).
- **`units`** — measurement units `metric` | `imperial`.
- **`operators`** — False treats `site:`/`inurl:` as plain text (verified: it
  changes the result set).
- **`include_fetch_metadata`** — ask Brave for fetch metadata.
- **`goggles`** — the modern Goggles param (a goggle URL / inline definition or
  a comma-separated list of up to 3). `goggles_id` (deprecated) still accepted.
- `result_filter` also takes `locations` / `news`.
- `run()` (→ CLI) exposes typed location flags: `--city`, `--state`,
  `--postal-code`, `--latitude`, `--longitude`, `--timezone`.

## Round-7: breadcrumbs, discussion engagement, page fetch (NEW)

### Enriched results — breadcrumb, thumbnails, ratings

Every result type (web, news, video, discussion, image) now carries extra
live signals:

- **`breadcrumb`** — the `meta_url.path` decoded into a clean human-readable
  trail, e.g. `docs / api / reference` (verified live on many queries).
- **`thumbnail_original`** on web results (unproxied source URL — already
  present for videos/image).
- **`thumbnail_is_logo`** — True when Brave flags the thumbnail as a brand logo.
- **web items** — an **`author_meta`** list (author name, URL, portrait src),
  **`contact_points`** (phone numbers when disclosed), plus inline
  **`creative_work`** (name + `rating.value` / `best` / `reviews` on course and
  review sites), **`location`** (a POI map entry when a web result is a place),
  and **`inline_faq`** (Q&A attached to a single web result, distinct from the
  SERP-wide `faq`).

### Discussion engagement — `forums()` (round-7)

Brave's embedded discussion threads carry rich engagement metadata that the
former skill dropped. **Round-7 surfaces it**:

```python
d = await brave.search("new york pizza", mode="web", count=8)
for t in d["discussions"]:
    t["forum"]        # "r/FoodNYC"
    t["num_answers"]  # 270
    t["score"]        # "272"  (API returns a string)
    t["question"]     # original post text (cleaned)
    t["top_comment"]  # best answer snippet (cleaned)
```

And a dedicated helper:

```python
d = await brave.forums("best programming language", count=10)
d["results"]  # [ {title, url, forum, num_answers, score, question, top_comment, ...}, ... ]
d["render"]   # readable output ($answer count, Q&A, top comment, URL)
```

`forums()` runs a web search and returns **only** the `discussions` section —
the same idea as `locations()` for the `locations` section. Verified live:
"new york pizza" → 11 threads each with answers/score/question/top comment.

### Fetch the page — `probe()` / `crawl()` (round-7)

The search API returns SERP snippets, not page content. When you need the
actual page text:

```python
p = await brave.probe("https://docs.python.org/3/library/json.html", max_chars=6000)
p["title"]     # document <title>
p["text"]      # clean readable page text (scripts/styles stripped)
p["status"]    # 200
p["final_url"] # final URL after redirects

docs = await brave.crawl(["https://a.example/", "https://b.example/"])
docs["results"]  # [{url,status,title,text,chars,truncated,error}] — per-item errors
docs["ok"]       # count of successful fetches
```

`probe()` does a simple HTTP GET with a browser-ish user-agent, redirects,
strips `<script>`/`<style>`/`<nav>` blocks, and collapses whitespace. `crawl()`
batches several URLs and captures per-URL failures so a bad link doesn't abort
the whole batch. These let you turn "here's the snippet" into "here's the page".

### Section flags & geo diagnostics

- **`mutated_by_goggles`** — on `web` search replies, a dict
  `{"web": bool, "videos": bool, "discussions": bool}` telling you whether
  Brave applied a Goggles filter to each section (verified with default Goggles
  → all False; custom Goggles can flip sections to True).
- **Query geo-diagnostics** — `d["query"]` now also carries
  `is_geolocal` (Brave detected a geographic intent), `local_decision`,
  `local_locations_idx`, and `show_strict_warning` (advisory that the result
  set may show under `strict` safe-search). Verified live: a pizza-in-NY query
  sets `is_geolocal=True`.

## Round-8: full-context answers + media lookups (NEW)

Round 8 enriches nearly every result type with the raw signals an agent needs to
act, and adds dedicated media/POI helpers that mirror `forums()`/`locations()`.

### Article/publisher identity + paywall gating (web)

News-styled web results now carry the publisher beyond its name, and a hard
gating flag:

```python
d = await brave.search("nytimes technology", mode="web", count=8)
r = d["results"][0]
r["publisher"]            # "The New York Times"
r["publisher_url"]        # publisher site URL when disclosed
r["publisher_logo"]       # publisher brand logo (proxied); r["publisher_logo_original"] = direct URL
r["publisher_type"]       # "organization"
r["author_types"]         # ["person", "person", ...] roles per author
r["paywall"]              # True when isAccessibleForFree is false (paywalled)
r["fetched_content_timestamp"]   # epoch of Brave's last content fetch
```

`paywall` is answered from Brave's `isAccessibleForFree` — a genuinely useful
"can I read this for free?" signal for news-oriented pipelines (verified live:
NYT articles report `paywall=True`).

### Full weekly hours + price/rating/icon on `locations`

`locations(...)` / `search()["locations"]` rows now carry the venue's **whole
week**, not just the current day's window, plus the signals needed to pick a
place:

```python
p = d["locations"][0]
p["week"]         # ["Monday 07:00-16:00", ..., "Sunday 07:00-16:00"]  (all days)
p["opening_hours"]  # current-day window (unchanged, backward-compatible)
p["price"]           # "$" .. "$$$$"
p["rating"]          # {"value":3.8,"best":5.0,"reviews":203,"is_tripadvisor":True}
p["icon"]            # "cafe" / "restaurant" / ...
p["id"]              # Brave POI id
p["timezone_offset"] # minutes west of UTC
p["website"]         # venue site URL when disclosed
p["profiles"]        # public profiles
```

### **`open_now(place)`** — is this place open right now?

```python
d = await brave.search("coffee shop near me", mode="web")
row = d["locations"][0]
brave.open_now(row)
# {"open": True, "now": "09:42", "day": "Friday", "window": "07:00-17:00", "note": None}
```

It resolves the current clock in the venue's own `timezone` and compares it
against the venue's current-day hours. Returns `open=None` with a `note` when
no today's hours are disclosed. Verified live against New York coffee shops.

### FAQ & infobox enrichment

- **faq items** now also carry `title` (source page heading) and `breadcrumb`
  (clean source trail), e.g. `topic / python` (previously only the Q&A text).
- **infobox** now surfaces `position` (rank) and `label` (e.g.
  `"programming language"`) alongside the existing fields.

### Media / POI lookups — `headlines()`, `clips()`, `pictures()`

Three dedicated mirrors of `forums()`/`locations()` for the other indexes:

```python
d = await brave.headlines("openai", count=6, freshness="pd_1w")  # fresh news
d["results"]   # [{title, url, source, age, thumbnail, publisher_url, ...}]
d["render"]    # readable "News headlines for '...'" block

d = await brave.clips("clojure tutorial", count=4)     # videos
d = await brave.pictures("mountain lake", count=4)     # images
```

Each returns `{query, results, n, render, search}` (the underlying structured
reply under `search`). All verified live against `api.search.brave.com`.

## Round-9: rich schema.org structured data + subtype-aware lookups (NEW)

Brave web results can carry **schema.org structured data** embedded inside the
result object itself — full recipe, product, or movie metadata that earlier
rounds dropped. **Round-9 surfaces all of it.**

### `recipe`, `product`, `movie` — structured blocks on web results

On a recipe query, each result now carries its rich structured data under
`.recipe`; on a shopping query, `.product`; on a movie query, `.movie`. Plus a
**`content_type`** (`pdf`/`doc`, ...) for document files:

```python
d = await brave.search("chocolate cake recipe", mode="web")
r = d["results"][0]
r["recipe"]   # {title, url, domain, time, prep_time, cook_time,
              #  ingredients: [...], instructions: [{text, url}...],
              #  servings, calories, publisher, category, cuisine,
              #  thumbnail, thumbnail_original,
              #  rating:{value, best, reviews}}

p = await brave.search("iphone 15", mode="web", count=5)
p["results"][0]["product"]  # {name, url, price,
                            #  offers: [{url, price, price_currency}],
                            #  rating: {value, best, reviews},
                            #  description, thumbnail, thumbnail_original}

m = await brave.search("inception movie", mode="web", count=5)
m["results"][1]["movie"]    # {name, description, url, release,
                            #  directors: [{name, url}],
                            #  actors: [{name, url, thumbnail}],
                            #  genre: [...], duration,
                            #  rating: {value, best, reviews},
                            #  thumbnail, thumbnail_original}

d = await brave.search("pdf documentation", mode="web", count=5)
d["results"][0]["content_type"]   # "pdf" — the file type of the document
```

All verified live against `api.search.brave.com`.

### Dedicated lookups — `recipes()`, `products()`, `movies()`

Three mirrors of `forums()`/`locations()` that return only results carrying the
given schema type:

```python
d = await brave.recipes("chocolate cake", count=5)
d["results"]  # web items each with a rich `.recipe` block
d["render"]   # "Recipes for '...': title [time, serves N, N cal, cuisine] ..."

p = await brave.products("iphone 15", count=5)
p["render"]   # "Products for '...': Name — $387.0 ★4.5 (1852 reviews) ..."

m = await brave.movies("inception", count=5)
m["render"]   # "Movies for '...': Title [release, genre..., ★8.8] ..."
```

Each returns `{"query", "results", "n", "count", "render", "web"}`.

### `run()` rich rendering (verified live)

`run()` now renders recipe/product/movie metadata inline, e.g.:

```
1. The Best Chocolate Cake Recipe
   ...
   🎂 Recipe: time 45:00, prep 15:00, cook 30:00, serves 24, 124 cal, American
   Σ 16 ingredients: 2 cups all-purpose flour ((spoon + level, ...
```

```
2. Inception (2010) ⭐ 8.8
   ...
   🎬 Movie: Jul 16, 2010, Action, Adventure, ★ 8.8, 02:28:00
   Directed: Christopher Nolan
   Starring: Leonardo DiCaprio, Joseph Gordon-Levitt, Elliot Page
```

And `[pdf]` is rendered as a content_type tag on document results.

### `content_type` — file-type detection

Some web results are direct documents (`.pdf`, `.doc`, ...). Brave
carries a `content_type` value when known; `search()` surfaces it as
`"content_type"` on the normalised item and `run()` renders it as `[pdf]`
etc. so an agent can skip/block non-HTML sources instantly.

---

## Round-10: Rich Search + Local POI detail (NEW)

### `rich(query)` — live rich-data Q&A

Call the web search with `enable_rich_callback=1`, then fetch the payload.

```python
d = await brave.rich("weather in london")   # {vertical, results, render}
d["results"][0]["data"]["current"]["temp"]  # 23.11

await brave.stock_quote("AAPL")             # live US stock quote
await brave.crypto("bitcoin")               # live crypto price
await brave.definition("serendipity")       # dictionary
await brave.currency_x(100, "USD", "EUR")   # FX conversion
await brave.convert_values(100, "km", "mi") # unit conversion (computed)
await brave.unix_time(1700000000)           # ts → date (computed)

# sports:
d = await brave.rich("nfl scores today")
d["results"][0]["data"]["games"]           # team name + score pairs
```

### `pois(ids)` / `poi_descriptions(ids)` — deep POI + AI blurbs

After a search with `locations`, fetch the deep record:

```python
d = await brave.search("coffee shop SF", count=3)
ids = [l["id"] for l in d["locations"]]
p = await brave.pois(ids)                  # reviews, pictures, email, ...
b = await brave.poi_descriptions(ids)      # AI blurb per place
```

## Round-11: Dedicated Place Search + domain / file / news conveniences (NEW)

### `place_search(...)` — the dedicated Place Search endpoint (round-11)

Beyond the `locations` section on web results, Brave ships a **dedicated
`/res/v1/local/place_search`** endpoint that searches the geographic index
(200M+ places) anchored to **coordinates** or a **location name**, and even
supports *explore mode* (no query → POIs in an area). It can also return rich
geographic groupings the legacy endpoint never exposed: `cities`, `countries`,
`regions`, `neighborhoods`, `addresses`, and `streets` — each with coordinates
and (for addresses/streets) distance + POIs sitting on/near them.

```python
# Anchored to coordinates (both required together)
d = await brave.place_search("coffee shops", latitude=37.7749, longitude=-122.4194,
                             radius=2000, count=10)
d["results"]    # normalized POIs (address, phone, hours, rating, price, coords)
d["resolved"]   # {"name","country","coordinates"} — resolved search centre
d["render"]     # readable digest

# Anchored to a location name
d = await brave.place_search("coffee shops", location="san francisco ca united states")

# Street / address query returns the `streets` / `addresses` buckets
d = await brave.place_search("el camino real palo alto",
                             location="palo alto california united states")
d["streets"]     # [{type, name, coordinates, distance, postal_address, pois,
                 #   pois_nearby}]

# Explore mode: just the places around a point, no query
d = await brave.place_search(None, latitude=40.7128, longitude=-74.006, count=5)

# City/country/region buckets ("about this place")
d = await brave.place_search("san francisco", location="san francisco ca united states")
d["cities"]       # [{"type":"city","name":..,"country":..,"coordinates":..}]
```

All verified live against `api.search.brave.com` (POI results via the same
normalizer as `locations()`; `streets`/`addresses` buckets carry `distance` and
nested `pois`/`pois_nearby`).

### `domain(name)` — site-scoped lookup (Round-11)

Runs `site:<domain>` and summarises a domain's top pages (a brand-homepage
query also usually surfaces the knowledge panel):

```python
d = await brave.domain("python.org", count=8)
d["results"]     # web items, each {title,url,description}
d["infobox"]     # e.g. the "Python" knowledge panel
d["render"]      # readable "n top pages" list
```

### `find_files(query, filetype)` — file-type search (Round-11)

Combines the `filetype:` operator with Brave's `content_type` filter to return
only direct documents (PDF/DOCX/PPT/...), not HTML pages:

```python
d = await brave.find_files("data science", "pdf", count=10)
d["results"]     # web items whose content_type == 'pdf'
d["render"]      # "[PDF] title / url ..."
```

### `news_cluster(topic)` — source-grouped news digest (Round-11)

Brave's news API returns a *flat* article list (no native clustering — see
api.md). `news_cluster()` fills that gap by grouping articles by source:

```python
d = await brave.news_cluster("artificial intelligence", count=30, freshness="pd_1w")
d["groups"]   # [{"source":"NYTimes","count":5,"articles":[...]}, ...] (by count)
d["render"]   # "News on '...' — N articles across M source(s):"
```

> **Honest gaps (api.md):** Brave exposes **no** `week_trending` (/res/v1/trends)
> and **no** `local_directions` (/res/v1/local/directions) endpoint — both 301
> the request to the HTML dashboard. Those are documented as *absent* rather
> than invented. The `llm/context`, `answers`, `suggest/search`, and
> `spellcheck/search` endpoints exist in Brave's docs but return
> `OPTION_NOT_IN_PLAN` on the standard Search plan key.

---



## Round-12: page-digest + cross-corpus `mosaic`, image batch 200 (NEW)

Good clarity wins for agents that want to *skim* instead of *list*.  All round-12
capabilities are live-verified on this key.

### `brave.summarize_page(url)` — clean extractive page digest
Fetch a URL and return a concise digest without a model call: a lead paragraph plus
the most information-dense sentences (TF-weighted, navigation/citation boilerplate
stripped), plus the raw cleaned text.

```python
d = brave.summarize_page("https://clojure.org/about/rationale", max_points=4)
d["title"]     # "Clojure - Rationale"
d["summary"]   # lead paragraph (first substantive prose block)
d["points"]    # up to 4 most info-dense sentences
d["text"]      # full cleaned page text (chars capped by max_chars, default 20000)
d["readable"]  # "title\nurl\n\nlead\n\nKey points:\n · ..."
```

### `brave.mosaic(query)` — cross-corpus digest in one call (web+news+video+image)

Composes `search()` across all four corpora, labels each pool, and returns a single
structured package plus a readable digest — faster than four hand calls.

```python
d = brave.mosaic("openai", count=3)          # images=True default (extra call)
d["web"] ... d["news"] ... d["videos"] ... d["images"]   # per-corpus pools
d["infobox"]  # knowledge panel when present
d["n"]        # total items across corpora
d["render"]   # "MOSAIC 'openai'\n[Web (3)]...\n[News (3)]..."
```

### Image batch — up to 200 images per request (documented caps corrected)

Brave's Image Search supports **up to 200 images per request** (default 50) — far
above the other corpora. `search(mode="image", count=...)` and `pictures(count=...)`
now pass the full range (capped at 200 for you). The old "caps ~10" docstrings for
`pictures()`/`clips()` were wrong and are corrected:
**web max 20 / news max 50 / video ~50 / images max 200**.

```python
d = brave.pictures("mountain landscape", count=150)   # 150 image results, one call
```

### Verified-absent and confirmed constraints (round-12)

- `brave.suggest(q)` — **do not build**: `/res/v1/suggest/*` returns a 301 redirect
  to the HTML dashboard and `/res/v1/suggest/search` returns `OPTION_NOT_IN_PLAN`
  on this key. Documented as absent (not faked).
- `offset` beyond 9 — the API returns **HTTP 422** for `offset >= 10`; 9 is the hard
  window (the skill already raises a clear `ValueError` and `paged()` advances within it).
- `aspect_ratio` / `min_width` / `min_height` for images are **not** documented params
  (they are accepted but ignored — identical results regardless of value); the
  docs list only `q/count/country/search_lang/safesearch/spellcheck`.
- There are **no `estimated` recap fields** in web responses on this plan.
- "package tracking" is a Rich vertical in the docs but does **not** trigger a rich
  hint on this key.

---


## Round-13: cross-format fusion + convenience briefs (NEW)

### `mosaic()` now also returns map/POI `locations`

`mosaic(query)` was already the single-call cross-corpus digest
(web + news + video + images + infobox + FAQ). Round-13 makes it the **widest
cross-format shape in one call** by fusing in Brave's map/POI `locations` from
the web corpus (for local-intent topics — e.g. "coffee shop san francisco"
returns ~100 POIs). The return dict now carries a `locations` key (normalised
`location_item` rows: title, address, phone, opening_hours, rating, price,
icon, coordinates) and that pool is rendered under "Map / POIs" in the digest;
`n` and `searches["locations"]` count it too.

```python
d = await brave.mosaic("coffee shop san francisco", count=3)
d["locations"]    # [ {title, address, phone, rating, ...}, ... ] up to 100
```

For topics with no local results `locations` is `[]` (no harm).

### Video items now carry a derived `live` flag
`_video_item` adds `"live": True` when a video exposes **no `duration`** in its
`video` block. Verified live: 24/7 streams and live news feeds (ABC News Live,
Sky Sports Main Event, YouTube live radio) expose no duration, while near
recorded videos do. Treat it as a low-confidence proxy (some recorded videos
also omit duration), documented honestly — not a plan gate.

```python
v = (await brave.search("live news stream", mode="video", count=5))["results"]
[v.get("live") for v in v[:3]]   # True for 24/7 streams, usually False/None
```

### Image `property` / `search_type` params → `search()` and `pictures()`
Both now accept `property="any|commercial|non-commercial"` (OECD licensing)
and `search_type="all|transparent"` and forward them to the image endpoint.
Verified live: all three `property` values and both `search_type` values return
HTTP 200 — but on this plan they are **pass-through** (results do not visibly
change). A forward-compat hook for licence tiering; documented honestly.

```python
await brave.search("logo", mode="image", property="commercial", search_type="transparent")
await brave.pictures("car", property="any", search_type="all")
```

### `place_search(..., cate=...)` — EP place category hint
`place_search` accepts `cate="cafe"` etc. Verified live: HTTP 200; also a
pass-through on this plan (results not visibly filtered). Forward-compat.

### `newsflash(topic)` — quick top-headlines digest
Pure composition of the verified `search(mode="news")` with `freshness`
(default `pd_1w`). Returns `{"topic","count","results","headlines",
"n","render","search"}` and a readable HEADLINES block (title + age + source).

```python
nf = await brave.newsflash("quantum", count=8)   # freshness=pd_1w
print(nf["render"])
```

### `explain(query)` — one-call Markdown research brief
Pure client-side composition: runs `mosaic()` across web/news/video/images/
locations, `summarize_page()` on the top web hit, and folds everything into a
compact **Markdown brief** (overview + key points + top sources + headlines).
Hand it straight to a downstream model or user.

```python
brief = await brave.explain("quantum computing", count=4, images_count=0)
print(brief)   # markdown
```

### `trending_topics()` — documented-absent (not fabricated)
There is **no** public trending-topics / top-stories endpoint on the Brave
Search API. Probed live (round-13): `/res/v1/web/trending`, `/res/v1/news/
trending`, `/res/v1/trending/search`, `/res/v1/web/top_stories`, `/res/v1/news/
discover`, `/res/v1/web/trending_search` all return HTTP 301 and redirect to
the docs HTML — same OPTION_NOT_IN_PLAN family as `suggest`/`answers`.
`trending_topics()` is included as a documented-absent note (no fake data); use
`newsflash(query)` / `headlines(query)` for current stories instead.

### Round-13 live-verified absence notes
- `image property` / `search_type` / `place_search cate` — accepted (200) but
  no visible filtering on this plan (pass-through).
- `video length`, `image freshness` params returned 200 but did **not** change
  results on this plan.
- `trending`/`top_stories` endpoints — 301→HTML (absent).

## Round-14: news depth (breaking/live + publisher beams), parallel batch, consensus review, BFS worker (NEW)

### News rows now surface `breaking` / `is_live` (web-embedded news)

Brave's **web-embedded news section** carries a per-item `breaking` flag (the
genuinely "breaking now" headline tag) plus `is_live` (live-coverage) that the
flat `/news/search` endpoint lacks. `search(mode="web")["news"]` rows (and
`mosaic()`'s news pool) now expose them, alongside `is_source_local` /
`is_source_both`:

```python
d = await brave.search("breaking news today", mode="web", count=20)
n[0]["breaking"]   # True for breaking headlines, False otherwise
n[0]["is_live"]    # True for live-coverage feeds
```

### `news_breaking(topic)` — only the breaking-flagged headlines

Runs a web search and returns just the items Brave tagged `breaking=true` —
the "breaking now" stories, not all recent articles:

```python
d = await brave.news_breaking("breaking news today", count=20)
d["results"]   # [ {title, url, age, breaking, is_live, ...}, ... ]
d["render"]     # readable "Breaking news on '...' — N flagged item(s):"
```

### `news_beams(topic)` — grouped publisher/"beam" view (round-14)

Brave's standalone news endpoint is flat (no native clustering), but its
*web-embedded* feed carries the extra breaking/live signals. `news_beams()`
pulls that feed and groups it by publishing outlet, reporting each outlet's
breaking/live story count plus its top featured articles:

```python
d = await brave.news_beams("openai", count=40, freshness="pd_1w")
d["beams"]      # [ {publisher, count, breaking_count, live_count, articles}, ... ]
d["render"]     # "www.bbc.co.uk (2)  [2 breaking]  · title ..."
```

### `batch(queries, *, mode="web", ...)` — parallel multi-query

Runs several queries concurrently (thread pool over the verified `search()`)
and bundles structured results; a per-query error is captured without aborting
the rest:

```python
d = await brave.batch(["clojure","rust","python","go"], mode="web", count=3)
d["results"]["clojure"]["count"] / ["titles"] / ["error"]   # per-query bundle
d["n"], d["n_ok"], d["render"]
```

### `article_review(query)` — consensus good/bad/mixed from live pages

Heuristically bucketing full page text (via `crawl()`) by positive vs negative
language into `good`/`mixed`/`bad`/`neutral` verdicts — no model call:

```python
d = await brave.article_review("clojure", count=4,
                               positive_hint="fun", negative_hint="verbosity")
d["verdict"]     # "good" | "mixed" | "bad" | "neutral"
d["breakdown"]   # [{url, title, verdict, pos, neg, note}]
d["render"]
```

### `search_worker(query, *, depth=2, breadth=3)` — BFS crawl

Fans out from a web search through Brave's own `cluster`/sitelink URLs (plus
in-page link fallback) and crawls each neighbour's full text up to `depth`
hops, returning the fetched page graph:

```python
d = await brave.search_worker("clojure", breadth=3, depth=2)
d["pages"]   # [{depth, url, title, ok, chars, children:[...]}, ...]
d["render"]
```

### `related(query)` — documented ABSENT
The web search response exposes **no** related-queries block (probed live on
every mode / many queries — no `related` key, no dedicated endpoint; the
closest `suggest/*` surface 301s to the docs HTML / `OPTION_NOT_IN_PLAN`).
`related()` is a documented-absent note (not fabricated); use `batch()` /
`merge()` for query discovery instead.

Live-verified this round on `api.search.brave.com` with the logged-in key:
news `breaking`/`is_live` surfacing, `news_breaking`, `news_beams`, `batch`
(4 concurrent queries), `article_review` (fetches + scores live pages),
`search_worker` (2-hop BFS over cluster sitelinks), `mosaic`/`run()` locations
fusion, and all prior 45 entry points still function.

## Round-15: typed structured blocks, deep place dossiers, live-news filter, consistent news age (NEW)

Three new public helper functions plus a consistent, comparable age model shared
by every news-carrying function — live-verified against `api.search.brave.com`.

### `structured(query, *, count=12, ...)` — typed blocks from one web SERP

A single `web` SERP can embed several heterogeneous `schema.org` blocks at once
(a recipe, a product with `offers`, a movie profile, a software/package entry)
plus the forum `discussions` section. `structured()` runs **one** `web` search
(no extra round-trips) and re-pools every present block into typed, object-shaped
lists with per-category counts:

```python
st = structured("chocolate chip cookie recipe")
st["counts"]     # {"recipes": 7, "products": 0, "movies": 0, "software": 0, "discussions": 0}
st["recipes"]    # list of normalised schema.org Recipe blocks
st["discussions"]  # forum threads from the top-level discussions section
st["render"]     # readable digest
```

Live-verified with recipe ("chocolate chip cookie recipe" → 7 recipes), product
("macbook pro 14 2023 price" → 3 products + 10 discussions), and movie
("inception 2010 movie" → 3 movies) queries.

### `place_detail(query, *, count=10, similar=None, location=..., ...)` — one-call deep place dossier

Assembles Brave's complementary views of the same place into a single object:

- `anchor` — the lightweight `place_search` row (title, address, coords, phone,
  rating, price);
- `pois` — the deep record (`pois(id)`: reviews, photos, email, profiles,
  full-week schedule, price range);
- `blurb` — the AI-written place description (`poi_descriptions(id)`);
- `nearby` — places geo-anchored at the anchor's own coordinates
  (`near(term, lat, lon)`, `similar=` term or the place's first category), each
  with a **computed straight-line distance** (`distance_km`,
  `distance_formatted` like "848 m" / "1.6 km", `distance_kind =
  "straight_line_estimate"`). The anchor itself and duplicates are excluded.

```python
p = place_detail("Sightglass Coffee", location="San Francisco, CA",
                 similar="coffee shop", count=8)
p["pois"]["reviews"]   # list of review records
p["blurb"]["description"]
p["nearby"]            # [{title, address, distance_km, distance_formatted}, ...]
```

Live-verified for a San Francisco café: deep POI + AI blurb + nearby list with
computed distances (848 m / 998 m / 1.6 km ...) all populate. Distances are
great-circle estimates from the anchor coordinate — still ask an ETA API before
quoting drive times.

### `news_live(topic, *, count=50, ...)` — live-coverage-only news filter

Returns only the news rows flagged `is_live == True`. Live-verify note
(round-15): on the queries pressed (breaking/live-match/debate) Brave returned
**no** `is_live` rows via the standalone endpoint on this plan — the filter is
available and correct when a live story surfaces, but an empty list is expected
for most topics; use `headlines()` / `news_cluster()` for a fresh-timeline view.

### Consistent news age — `age_meta` + `published_at` on every news item

Every news-carrying function (`news`, `headlines()`, `news_cluster()`,
`news_breaking()`, `news_live()`) now reports, alongside the raw `page_age`:

- `published_at` — the ISO publish timestamp (Brave's authoritative `page_age`);
- `age_meta` = `{"kind": "relative"|"date"|"unknown", "days": float|None,
  "text": <raw>, "published_at": ...}` — `kind` states which form the raw `age`
  used; `days` is a deterministic numeric age for relative strings ("5 hours ago"
  → `0.21`, "2 weeks ago" → `14.0`) and `None` for absolute-date strings (e.g.
  "February 23, 2021"), where no reliable reference clock exists.

Live-verified: both the relative (`5 hours ago` → `0.21` days) and absolute-date
(`January 7, 2025` → `kind "date"`) cases surface correctly.

Backward compatible: all prior public signatures are untouched; `_get_api_key()`
is unchanged (`env` → dotenv → `/login`); no Serper/websearch.

## Round-16: typed infobox facts, thumbnail gallery, drink recipes (NEW)

Three new public helpers plus live-verified absent-notes, all checked against
`api.search.brave.com` with the logged-in key.

### `infobox(query, ...)` — typed knowledge-panel fact table
Brave's infobox (knowledge panel) carries an `attributes` array — `[field,
<span html>]` rows whose values mix `<a>` links and `<br>` line breaks.
`infobox()` re-interprets that into a typed fact table: each `facts` row is a
clean `{field, value}` (HTML stripped via the same `_clean_html` used on the
whole skill) plus the entity `entity`/`entity_type` (label or category), the
`provider` attribution (e.g. Wikipedia), and the entity `page`/`website_url`.

```python
d = await brave.infobox("clojure programming language")
d["entity"]    # "Clojure"
d["entity_type"]  # "programming"
d["provider"]  # {"type":"external","name":"Wikipedia","url":"..."}
d["facts"]     # [{"field":"Paradigm","value":"agent-oriented
concurrent
functional..."}, ...]
d["render"]
```
Verified live: `<a>`/`<br>` markup stripped to readable multiline values;
SERPs without a knowledge panel return `entity=None, facts=[]` (no fabrication).

### `thumbnails(url, *, video=True, image=True, count=5, ...)` — thumbnail gallery
Flattens just the thumbnail artwork URLs across the video + image corpora into
one ordered list (and per-corpus pools), so a pipeline can pre-load assets or
build a gallery without re-parsing full result dicts. Each item carries the
title, source url, Brave's proxied `thumbnail`, and the direct
`thumbnail_original` (video) / `image_url` (image), plus `duration` for
videos and `width`/`height` for images.

```python
d = await thumbnails("clojure", count=5)
d["count"] / d["n"]             # total thumbnails pulled
d["items"]/["video"]/["image"]  # [{kind,title,url,thumbnail,original|image_url,...}]
d["render"]
```
Verified live: direct `i.ytimg.com/.../maxresdefault.jpg` originals come back
un-proxied for video; image rows carry their source `image_url`.

### `drinks(url, *, count=12, ...)` — drink / cocktail recipe lookup
Runs `recipes()` and narrows to the *drink* subset — recipes whose schema
`category` is a beverage/bar class (`Drinks`, `Cocktail`, `Beverage`,
`Margarita`, ...) or whose title/domain signals a cocktail (margarita, mojito,
old fashioned, espresso, ...). Returns the drink recipes with their `.recipe`
block and a readable digest.

```python
d = await drinks("margarita cocktail recipe")
d["n"]/d["results"]     # drink recipes (with .recipe ingredients/timings/servings)
d["render"]
```
Verified live: 10 drink recipes out of an 12-recipe scan, `category` values
include `Drinks`, `Cocktail`, `Cocktail,Margarita,Beverage`, and a `None`
-category wine/liquor-domain hit is caught by the domain signal.

### Live-verified absent notes (round-16)
- **`news_regions` / trending-geo** — the `/news` endpoint has no regional
  trending field (raw reply keys are only `query/results/type`; items carry no
  region bucket). Probed `region`/`news_region`/`country` params → HTTP 200 but
  **identical results** (pass-through on this plan). No `news_regions` helper.
- **`newsvine` source-category aggregation** — no per-article
  source-category/`newsvine` field exists on `/news/result` items (the skill's
  `news_cluster()` / `news_beams()` already group by source client-side).
- **Discussions: `joinCount`/membership + nested reply depth** — the raw
  `discussion` item's `data` carries only `forum_name/num_answers/question/
  score/title/top_comment` (no `joinCount`, no `top_answers`/`top_comments`
  arrays). `forums_expand()` (following nested replies via `probe()`) is
  **not built**: Reddit — the dominant embedded thread source — returns a thin
  accessibility page (`<title>Reddit</title>`) when fetched, so nested
  reply depth can't be recovered reliably. `forums()` already surfaces the
  single authoritative `top_comment`.
- **video thumbnail-variant/playability & image `image_cache`/`image_provider`**
  — the raw `video` item `thumbnail` carries only `src`/`original` and its
  `video` block only `author/creator/duration/publisher` (all already
  surfaced, incl. the round-13 `live` proxy); image `properties` has only
  `url/placeholder/width/height`. No additional variant/playability/cache
  field exists on this plan.

Backward compatible: all prior 48 public signatures are untouched
(`infobox`/`thumbnails`/`drinks` were added, total public entry points = 51);
`_get_api_key()` unchanged (`env` → dotenv → `/login`); no Serper/websearch.


## Round-18: developer powers — package registry, security, error debugging (dev)

New live-verified dev helpers composed over `search()` / `software()` /
`qna()` / `github_issues()` (no new plan-gated endpoints):

- **`pkg_lookup(query, *, count=14, docs_count=6, ...)`** — package/registry
  identity for a Python/JS/Rust lib: Brave's `software` rows carry `name`,
  `version`, `published`, registry (`pypi`/`npm`) and code repo for the exact
  package pages (e.g. `pkg_lookup("uuid")` -> `uuid` **v14.0.1** [npm]). Non-
  registry hits (homepage/GH/docs) fall to `pages`. Live-verified.
- **`pkg_security(query, *, context=None, count, news_count, ...)`** — package
  security digest: advisories (Snyk / cvedetails / NVD / GitHub Advisory DB /
  OSV / vendor), any `CVE-YYYY-NNNN` ids seen in titles, GH-issue pages, and a
  freshness-aware news pass. Live-verified on `pyyaml` (CVE-2020-14343 etc.)
  and on `fastapi`.
- **`error_solution(query, *, ctx=None, count, min_answers, ...)`** — the best
  solve for an error / stack string: quote-peels the message, returns the top
  Stack Overflow / forum `question` + `top_comment`/`top_answer` and the next
  Q&A sources. Live-verified on a real `TypeError` trace.
- **`stack_trace(query, *, ctx=None, count, issues, min_answers, ...)`** — the
  whole linked debug universe for a stack snippet: matching GitHub issues
  (with issue numbers), Q&A threads (SO/Reddit) and plain articles, in one
  call. Live-verified on `KeyError` / `IndexError` snippets.
- **`dep_signal(query, *, library=None, freshness="pd_1m", signal=None,
  threshold_days=None, ...)`** — a *classified* deprecation/breaking scan that
  extends `breaking_change()`: labels every fresh item by type (breaking /
  deprecat / migrat / removal / release), returns `by_type` + `counts`, and
  filters stale rows via `age_meta.days` (`threshold_days`). Live-verified on
  `pydantic` (migration guide, breaking release, changelog).
- **`trending_libs(query=None)`** — **documented-absent** (round-18): Brave has
  no trending/top-packages index; the end-points 301-redirect (like
  `trending_topics()` round-13 / `related()` round-14). Use `dep_signal()` /
  `breaking_change()` per lib, or `newsflash()`/`headlines()`.

Backward compatible: all 61 prior public entry points are untouched (added
`pkg_lookup`/`pkg_security`/`error_solution`/`stack_trace`/`dep_signal`/
`trending_libs` → **67** public functions); `_get_api_key()` unchanged
(`env` → dotenv → `/login`); no Serper/websearch; one clear error.

## CLI


```bash
!brave --query "Clojure type papers" --mode web --count 8
!brave --query "openai announcements" --mode news --freshness pd_1w --count 6
!brave --query "rust advantages" --mode web --summary
```

(Type-assisted CLI: first arg is `--query`.)

## Modes

`web` (default), `news`, `image`, `video`, `local`, `all` (web+news+video).

## Advanced options (all optional)

`count`, `country`, `search_lang`, `result_filter`, `safe_search`
(`off|moderate|strict`), `freshness` (`pd`/`pw`/`pm`/`py`, legacy
`pd_1d|pd_1w|pd_1m|pd_1y`, or date-range `YYYY-MM-DDtoYYYY-MM-DD`), `grep`
(regex on sources, web), `extra=True` (more snippets), `goggles` (custom
filter: url/definition/list — replaces deprecated `goggles_id`), `ui_lang`,
`units` (`metric`/`imperial`), `operators` (toggle search operators),
`include_fetch_metadata`, `spellcheck` (force/disable). Localisation:
`loc={...}` dict or the `city`/`state`/`postal_code`/`latitude`/`longitude`/
`timezone` flags.

Section sizing & paging & AI:

- `offset` — page-start index (real window is `0 <= offset <= 9`; `offset >= 10` → HTTP 422).
- `discussion_count`, `video_count`, `movie_count` — control how many
  videos/discussions get embedded in the web answer (web mode).
- `unit` — `px` or `em` for image result dimensions (image mode).
- `text_decorations` — True keeps Brave's `<strong>` highlight marks in
  description text (default), False requests clean text without them
  (verified live: `text_decorations=false` strips the marks for plain text).
- `summary=True` — request Brave's AI summary; populated in `d["summarizer"]`.

Plus a `timeout`.

Verified: `result_filter` accepts `web, news, videos, discussions, faq` (comma
separated); values like `images`/`local`/`video` alone are rejected with an
HTTP 422 (verified live).

See `references/api.md` for endpoint/shape details.

## Gotchas

- `offset` is capped at `9` (Brave's real window); a too-large offset raises a `ValueError` instead of a silent 422.
- `faq`, `mixed`, `videos`, `discussions`, `news`, `summarizer` are **optional**
  — each may be empty/absent for a given query, so always guard with
  `.get(...) or []`.
- `top_results` is a convenience; if you need the raw columns, use `mixed`.
- **Errors are categorised.** `BraveError.category` is one of `auth`, `param`,
  `rate_limit`, `http`, `network`, `timeout`, so you can react rather than
  string-match. Verified live: an invalid key returns HTTP **422** with
  `error.code == "SUBSCRIPTION_TOKEN_INVALID"` (not 401!) → `auth`; a bad
  parameter (e.g. `offset=10`, bad `goggles_id`) returns 422 with
  `error.code == "VALIDATION"` → `param` (field details in `.details`);
  rate limiting is 429 → `rate_limit`. `.is_auth` / `.is_rate_limited` helpers
  included.
- Transient failures (network blips and HTTP 429/5xx) are retried once with a
  short backoff automatically; definitive auth/validation errors are never
  retried. Pass `timeout=...` to tune the HTTP timeout (default 45s).
  
