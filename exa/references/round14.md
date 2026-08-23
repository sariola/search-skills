# Round 14 — v0.15 next-layer power (fresh-news roundup + company brief + result diff)

All three additions are **client-composed conveniences** over existing live
`api.exa.ai` calls — verified against the live API (2026-08-07). No new
undocumented API surface is assumed; everything decomposes into normal, already
wrapped endpoints (`/search`, `/contents`, `entity_summary`).

## 1. `exa.news_roundup(query, *, days=10, num_results=5, ...)` — multi-day fresh-news sweep

Runs `days` single-day `search(mode="news")` windows (most recent day backward),
each restricted to that day's `startPublishedDate`/`endPublishedDate`, then
de-duplicates by URL and ranks results by how many day-windows they appeared on
(strong persisting-freshness signal). `with_highlights` gives a short snippet per
article.

```python
rr = exa.news_roundup("AI infrastructure funding", days=3, num_results=5)
len(rr)               # ~12 unique fresh articles across the 3 windows
rr.window_counts      # {"0": 5, "1": 5, "2": 4}  -> hits per trailing day
rr.results[0].extras["days_seen"]  # [0, 1]  peak freshness = seen on multiple recent days
```

Verified live: a 3-day / 4-result sweep over "AI chip" returned 12 unique fresh
articles; the top entries repeated across back-to-back day windows (a strong
"still trending" signal). Returns a `SearchResults` (iterable / `to_dicts` /
`to_json`).

## 2. `exa.company_dossier(name, *, num_results=3, fetch_website=True, ...)` — one-call company brief

Assembles the three things you'd otherwise wire together for a company profile:
a `company`-category search (which returns the structured `entities[]`
record), normalization via `entity_summary` into `foundedYear`/`workforce`/
`headquarters`/`financials`, and an optional `fetch` of the homepage's semantic
`body` sections for the "about / what it is" copy.

```python
d = exa.company_dossier("Anthropic")
d["profile"]["financials"]    # {"revenueAnnual": 3500000000, "fundingTotal": ..., ...}
d["profile"]["workforce"]     # {"total": 3402}
d["website_text"][:120]       # "# Anthropic (Anthropic PBC) ..." (homepage body copy)
d["summary"]                  # LLM summary of "what the company does"
```

Verified live on Anthropic: the structured entity (founded 2021, SF HQ, ~3402
employees, $3.5B revenue / >$180B raised) plus homepage body text came back in
one call. If the top match is a LinkedIn page, `fetch` pulls its body rather
than a dead host.

## 3. `exa.diff_results(a, b, *, key=...)` — URL-level diff between two result sets

Given two result collections (`SearchResults`, a `Result` iterable, or a list of
raw dicts), compare by canonical URL and report which pages are new in `b`
(`added`), which disappeared from `a` (`removed`), and which kept the URL but
changed title/date (`changed`). Great for comparing two `news_roundup` sweeps,
two `deep_research` runs, or two monitor/webset snapshots.

```python
d = exa.diff_results(run_a, run_b)
d["a_count"], d["b_count"]        # unique-key totals
d["added"]                        # [Result, ...]  present in b only
d["removed"]                      # [Result, ...]  present in a only
d["changed"]                      # [(before, after), ...]
print(d["report"])                # readable +N / -N / ~N table
```

Verified live on two overlapping news searches and on `dict`-valued inputs;
the readable `report` renders a compact changelog. This is a pure local helper —
no additional API call.

## Scope / compat

All three additions pass the entire existing `search`/`answer`/`agent`/`fetch`/
webset surface and every prior keyword arg onward. No existing signature or
return type changed. `__all__` goes 97 → 100 with `news_roundup`,
`company_dossier`, and `diff_results` (plus the module's internal
`_coerce_result_list` helper). `modes()` lists the new conveniences.
