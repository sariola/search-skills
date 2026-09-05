# News, media, structured data, and places

## News

Use `search(mode="news", freshness=...)`, `headlines()`, or `newsflash()` for
recent stories. `news_cluster()` groups articles by source; `news_beams()` groups
web-embedded news by outlet. These are client-side groupings, not independent
corroboration or topic clusters. Compare event dates with reported page dates.

`news_breaking()` filters flags from web-embedded news. `news_live()` filters
standalone news rows and can be empty when that endpoint omits live flags;
empty output does not establish that no live coverage exists. If needed, inspect
`search(mode="web", view="full").get("news")` and the source pages.

Detailed rows may carry `published_at`, `page_age`, and `age_meta` with relative
days or an unknown/date kind. Relative strings are approximate; null days do
not mean zero age. Compact results collapse date fields; request full view when
you need to know which field supplied the date.

## Images and video

Use `pictures()` / `clips()` or the corresponding search modes. The client caps
image requests at 200, news at 50, and video at 50; these are client settings,
not guaranteed delivered counts or a provider-wide contract.

Use full view for `thumbnail_original`, direct `image_url`, source-page URLs,
and dimensions. `thumbnails()` collects artwork from image/video searches.
A returned URL does not grant reuse rights. `property`, `search_type`, and other
image hints must not be treated as verified licensing or transparency filters.
The derived video `live` field can simply mean missing duration; it does not
confirm a live broadcast or playability.

`mosaic(query, images=False)` combines web, news, and video with optional images;
map rows come from web results. `explain()` adds an extractive page summary.
Use individual modes when only one corpus is relevant.

## Structured web blocks

`structured()` extracts recipes, products, movies, software, and discussions
from one web search. Dedicated helpers `recipes`, `products`, `movies`,
`software`, and `forums` expose focused views; `drinks` uses recipe categories
and a title/domain heuristic. Missing blocks are normal and are not evidence
that a product, recipe, or discussion does not exist.

`infobox()` converts knowledge-panel HTML attributes to field/value facts;
preserve provider attribution and verify critical facts against source pages.
`article_review()` counts positive/negative words in fetched pages. Its verdict
is a lexicon heuristic, not a model review, representative sentiment study,
or consensus judgment.

## Places

`place_search(query, location=...)` uses a place-name anchor;
`latitude`, `longitude`, and `radius` allow coordinate intent. Check `resolved`
before treating results as local. A place name can be ambiguous. `locations()`
and `near()` use web-location results; `search(mode="local")` may fall back to
web results on an unavailable endpoint. Optional category hints may not filter.

Use returned place IDs with `pois(ids)` for detailed records and
`poi_descriptions(ids)` for generated blurbs. `place_detail()` composes an anchor,
POI details, description, and nearby results. Its distances are straight-line
estimates, not routes or journey times. `open_now(row, now=...)` interprets
available hours/timezone data; check uncertainty, holidays, and current venue
information when opening status matters.

## Rich results

`rich(query)` requests a callback hint from web search and then fetches its
payload when present; `fetch=False` requests only the hint. Helpers include
`weather`, `stock_quote`, `crypto`, `currency_x`, and `definition`. Supported
queries can return no rich hint; do not fabricate structured data from that gap.
`convert_values` and `unix_time` can compute locally. `package` attempts tracking
lookup but cannot guarantee a carrier payload.

Preserve provider, timestamp, units, and market context for rich data. For an
answer requiring current values, inspect freshness rather than relying on a
helper's name or an old example.
