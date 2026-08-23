# Round-11: Dedicated Place Search + domain / file / news conveniences

Round 11 adds four live, agent-useful surfaces to the Brave skill and honestly
documents four endpoint gaps. All additions are **verified live** against
`https://api.search.brave.com` (2025).

---

## `place_search(...)` — the dedicated Place Search endpoint (NEW)

Brave ships a **dedicated** geographic-search endpoint distinct from the web
`locations` section:

```
GET /res/v1/local/place_search
```

It searches a 200M+ index of physical places (businesses, landmarks, POIs),
anchored to either a **coordinate pair** or a **location-name string**, and
optionally biased to a **radius** (meters). Omit `q` for **explore mode**
(general places around a point).

Live-verified shapes:

```python
d = await brave.place_search("coffee shops", location="san francisco ca united states", count=5)
d["results"]   # POI rows (address, phone, hours, rating, price, coords)
d["resolved"]  # {"name":"San Francisco","country":"US","coordinates":[...]}
d["render"]    # readable digest

# Coordinates + radius
d = await brave.place_search("pizza", latitude=37.7749, longitude=-122.4194,
                             radius=1000, count=5)

# Street / address query surfaces `streets` / `addresses` buckets
d = await brave.place_search("el camino real palo alto",
                             location="palo alto california united states")
d["streets"]   # [{type:"street", name:"El Camino Real", coordinates:...,
               #   distance:{value:1.5,units:"km"}, postal_address:...,
               #   pois:[...], pois_nearby:[...]}]

# City-level query surfaces the `cities` bucket
d = await brave.place_search("san francisco", location="san francisco ca united states")
d["cities"]    # [{type:"city", name:"San Francisco", country:"us",
                 #   coordinates:[37.77493,-122.41942], thumbnail_original:...}]
```

POI rows reuse the *same* `_location_item` normaliser as `locations()` and the
Web Search `locations` section, so `open_now()`, `pois(ids)`, and
`poi_descriptions(ids)` all work on `place_search` output directly.

Verified: coffee-shop searches around SF return rich places (phone, weekly
hours, `★` rating, price range, coordinates); street queries (`el camino real
palo alto`) return the `streets` bucket with `distance`; city queries return
the `cities` bucket with a hero image; explore mode (no q) surfaces landmarks.

## `domain(name)` — site-scoped lookup

Runs `site:<domain>` web search and summarises top pages:

```python
d = await brave.domain("python.org", count=8)
d["results"]   # → Welcome to Python.org / Download Python / About Python ...
d["infobox"]   # the "Python" knowledge panel
```

`domain()` strips `www.`/protocol/path, asks for `text_decorations=False`
(clean descriptions) and surfaces the AI-summarizer deep-link when `summary`
is on. Verified: `domain("python.org") → {n:20, infobox:{title:"Python"}}`.

---

## `find_files(query, filetype)` — file-type search

Combines the `filetype:` operator with Brave's `content_type` filter:

```python
d = await brave.find_files("data science", "pdf", count=20)
d["results"]   # web items whose content_type == 'pdf' (verified: 6/8 results)
```

Verified `filetype:` coverage: `pdf` → 9/10 document hits; `docx`/`ppt`/
`pptx` → a few hits; `doc`/`xls`/`xlsx`/`csv`/`zip` → **no** `content_type` on
results (Brave's file-extractors are selective — documented gap).

---

## `news_cluster(topic)` — source-grouped news digest

Brave's news API returns a **flat** article list (no `related`/`cluster`
field — verified). `news_cluster()` groups the set client-side:

```python
d = await brave.news_cluster("artificial intelligence", count=30, freshness="pd_1w")
d["groups"]  # [{"source":"NYTimes","count":5,"articles":[...]}, ...] by count
d["render"]  # "News on '...' — 30 articles across 16 source(s):"
d["n_sources"]
```

---

## Honestly absent / plan-gated

- **`week_trending`** — `/res/v1/trends*` all HTTP-301 to the HTML dashboard
  → **no** trends API. Documented absent.
- **`local_directions`** — `/res/v1/local/directions` returns an HTML dashboard
  body, not JSON → **no** directions/routing API. Documented absent.
- **`news_cluster` native clustering** — news results are flat; grouping in
  this skill is client-side.
- **Plan-gated** (presence docs only): `llm/context` (LLM Context plan),
  `answers` (`/res/v1/chat/completions`, Answers plan), `suggest/search`
  (Autosuggest plan), `spellcheck/search` (separate option) → all return
  `error.code == "OPTION_NOT_IN_PLAN"` 400 on the standard Search key.

---

## Backward compatibility

- `search()`/`run()` signatures unchanged.
- All 30 prior public entry points preserved (verified by import).
- `modes()` now lists the four round-11 helpers.
- Old callers unaffected; the new functions are additive.
