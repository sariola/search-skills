# Round-16 — typed infobox facts, thumbnail gallery, drink recipes; absent-notes

All live-verified on `api.search.brave.com` with the logged-in key
(BRAVE_API_KEY / BRAVE_SEARCH_API_KEY).

New public helpers: `infobox()`, `thumbnails()`, `drinks()` — total public
entry points now 51 (prior 48 untouched).

## `infobox(query, *, count=10, ...)` — typed knowledge-panel fact table
Brave's web `infobox` carries an `attributes` array of `[field, <span html>]`
rows whose values mix `<a>` links and `<br>` breaks. `infobox()` strips the
HTML (same `_clean_html` used across the skill) into clean `{field, value}`
facts, plus the entity `entity`/`entity_type` (label || category) and the
primary `provider` attribution.

Verified live:
- `infobox("clojure programming language")` → entity `Clojure`,
  `entity_type "programming"`, provider `Wikipedia`, **12 facts**, e.g.
  `Paradigm = agent-oriented / concurrent / functional / ...`,
  `Designed by = Rich Hickey`, `Stable release = 1.12.4 / 10 Dec 2025`.
  `<br>` becomes a line break, `<a>` becomes plain text.
- No knowledge panel (`infobox("random obscure string xyzzq")`) → `entity=None,
  facts=[]`, render `"No knowledge panel for '...'."` (no fabricated data).

## `thumbnails(url, *, video=True, image=True, count=5, ...)` — thumbnail gallery
Flattens just the artwork URLs across the video and image corpora into one
ordered `items` list (+ per-corpus `video`/`image` pools). Each item carries
title, source url, Brave's proxied `thumbnail`, and the direct source
`thumbnail_original` (video) / `image_url` (image); `duration` for video,
`width`/`height` for images.

Verified live: `thumbnails("clojure", count=3)` → 6 thumbnails
(3 video + 3 image), with un-proxied `https://i.ytimg.com/vi/.../maxresdefault.jpg`
originals for video and source `image_url` for images.

## `drinks(url, *, count=12, ...)` — drink / cocktail recipe lookup
Runs `recipes()` and narrows to the drink subset: filter on schema `category`
against `DRINK_CATEGORIES` (drinks, cocktail, beverage, margarita, ...) OR the
title/domain cocktail-signal lexicon.

Verified live: `drinks("margarita cocktail recipe", count=12)` → 10 drink
recipes from `recipes()`-scanned 12, `category` values `Drinks`, `Cocktail`,
`Cocktail,Margarita,Beverage`, `Cocktails, Drink`; a `None` -category,
`liquor.com`-domain hit is caught by the domain signal. Light title-dedupe.

## Live-verified absent notes (round-16)
- **`news_regions` / trending-geo** — `/news` response keys are only
  `query/results/type`; items carry no region bucket. Probed `region`,
  `news_region`, and `country` params → HTTP 200 but **identical results**
  (pass-through on this plan). No `news_regions` helper built.
- **`newsvine` source-category aggregation** — no per-article field on
  `/news` result items (`age/description/meta_url/page_age/profile/thumbnail/
  title/type/url` only). Use `news_cluster()`/`news_beams()` for client-side
  source grouping.
- **Discussion `joinCount`/membership + nested reply depth** — raw thread
  `data` carries only `forum_name/num_answers/question/score/title/
  top_comment` (no `joinCount`, no `top_answers`/`top_comments` arrays).
  `forums_expand()` (following nested replies via `probe()`) is **not built**:
  the dominant embedded thread source (Reddit) serves a thin accessibility
  page (`<title>Reddit</title>`, no comments) when fetched, so nested reply
  depth is not recoverable. `forums()` surfaces the single authoritative
  `top_comment`.
- **video thumbnail variant/playability** — raw `video` item: `thumbnail` has
  only `src`/`original`; `video` block only `author/creator/duration/
  publisher`. No variant/playability/embed field (round-13 `live` proxy kept).
- **image `image_cache`/`image_provider`** — raw image `properties` has only
  `url/placeholder/width/height`; `thumbnail` only `src/width/height`. No
  cache/provider field on this plan.

All prior 48 public signatures + return shapes preserved; `_get_api_key()`
untouched (env → dotenv → /login); no Serper/websearch.
