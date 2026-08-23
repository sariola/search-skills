
## Round-13: cross-format fusion + convenience briefs (NEW)

All round-13 capabilities are live-verified on this key (BRAVE key through
`_get_api_key()` env>dotenv>/login; no Serper/websearch).

### `mosaic()` now also returns map/POI `locations`
`mosaic(query)` — already the single-call cross-corpus digest (web + news +
video + images + infobox + FAQ) — now **fuses in Brave's map/POI `locations`**
from the web corpus. For local-intent topics (e.g. "coffee shop san francisco")
the web `search()` already carries `locations` rows; round-13 surfaces them in
`mosaic()` too (was previously dropped). Return adds a `locations` key
(normalised `location_item` rows) rendered as "Map / POIs" in the digest, and
counts them in `n` and `searches["locations"]`.

```python
d = brave.mosaic("coffee shop san francisco", count=3)
d["locations"]            # up to ~100 POIs for local-intent topics
d["searches"]["locations"]
```

Verified live: "coffee shop san francisco" → 100 POIs; "quantum computing" → 0.

### Video items carry a derived `live` flag
`_video_item` now adds `"live": True` when a video exposes **no `duration`** in
its `video` block. Verified live that 24/7/news streams (ABC News Live, Sky
Sports Main Event, YouTube 24/7 radio labels) expose no duration, while
recorded tutorials do. Documented honestly as a **low-confidence proxy**
(some recorded videos also omit duration).

```python
v = brave.search("live news stream", mode="video", count=5)["results"]
[v.get("live") for v in v[:3]]     # True for 24/7 streams
```

### Image `property` / `search_type` params → `search()` + `pictures()`
`search(mode="image")` and `pictures()`. add `property="any|commercial|non-
commercial"` (OECD licensing) and `search_type="all|transparent"`. Live probes:
all three `property` values and both `search_type` values return **200** — but
identical results regardless of value on this plan (pass-through, forward-compat).

Verified evidence: "red sports car" returned the same 3 images for
`property=any|commercial|non-commercial`; "apple logo" returned the same 3 for
`search_type=all|transparent`.

### `place_search(..., cate=...)` — EP place category hint
`place_search` accepts `cate="cafe"` etc (verified live: `cate=pizza` on "food"
in New York returned HTTP 200). Also pass-through — results not visibly filtered
on this plan. Forward-compat.

### `newsflash(topic, count, freshness=...)` — quick top-headlines digest
Pure composition of the verified `search(mode="news")` with `freshness` (default
`pd_1w`). Returns `{"topic","count","results","headlines","n","render",
"search"}` and a readable HEADLINES block (title + age + source + url).

```python
nf = brave.newsflash("quantum", count=8)
print(nf["render"])
```

### `explain(query)` — one-call Markdown research brief
Pure client-side composition: `mosaic()` (web/news/video/images/locations) +
`summarize_page()` on the top web hit, folded into a compact Markdown brief
(overview + key points + top sources + headlines + videos/images/POIs).

```python
brief = brave.explain("quantum computing", images_count=0)
print(brief)
```

Verified: gives "# quantum computing
**Quantum computing** — study of a model
of computation
### Overview (Quantum computing - Wikipedia)
..." on this key.

### `trending_topics()` — documented-absent (not fabricated)
Probed live: `/res/v1/web/trending`, `/res/v1/news/trending`,
`/res/v1/trending/search`, `/res/v1/web/top_stories`, `/res/v1/news/discover`,
`/res/v1/web/trending_search` — all HTTP **301** redirecting to the docs HTML
(no JSON). Same OPTION_NOT_IN_PLAN family as `suggest`/`answers` (round-11).
`trending_topics()` is included as a documented-absent note; use
`newsflash(query)` / `headlines(query)` for current stories.

### Round-13 verified-absent / pass-through notes
- `image property` and `search_type`, `place_search cate`: accepted (200),
  no visible filtering on this plan (pass-through forward-compat).
- `video length` (short/medium/long): accepted (200) but identical results on
  this plan — not honored.
- `image freshness`: accepted (200) but identical results — not honored.
- `trending` / `top_stories` / `discover`: 301→HTML (absent).

---
