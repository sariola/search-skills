
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


## CLI
