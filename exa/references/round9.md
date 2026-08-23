# Exa skill v0.10 — Round 9 next-layer power

Verified live against api.exa.ai (2026-08). Closes the last cross-endpoint
content-parity gaps and adds results-analysis conveniences.

## fetch() / run() content-option parity

Previously only `search()` / `find_similar()` honored a highlights character
cap; `instant`/`deep-*` weren't surfaced anywhere, and `fetch()`/`run()` silently
dropped two rich-content knobs. Now all four main entry points accept the full
content-option set.

| Entry point | Newly added (round 9) |
|---|---|
| `fetch()` | `highlights_max_characters` forwarded to `contents.highlights.maxCharacters` |
| `run()` | `text_verbosity` (compact/standard/full) + `highlights_max_characters` forwarded to the underlying search |

Verified live: `fetch(["https://exa.ai"], mode="highlights",
highlights_query="Exa search", highlights_max_characters=400)` returns
highlights whose per-item length honors the cap, and `run(...,
text_verbosity="compact", highlights_max_characters=300)` streams the same
options through to `/search`. Inputs that used to be silently dropped now take
effect.

## Result.domain / Result.hostname

Every `Result` now exposes:

- `Result.domain` — registrable domain (e.g. `cloudflare.com`), computed with a
  pragmatic 2-label heuristic (no public-suffix list dependency).
- `Result.hostname` — full hostname (e.g. `developers.cloudflare.com`).

Verified live: results from `developers.cloudflare.com/...` and
`www.cloudflare.com/...` both group under `cloudflare.com`.

## SearchResults.domains() / top_domains(n)

- `domains()` → `{domain: count}` frequency map across all results.
- `top_domains(n=10)` → descending `[(domain, count)]` list (ties by name).

Source-densification analysis: "these 15 results come overwhelmingly from
`cloudflare.com` (13) plus `github.com` (2)".

## SearchResults.to_dataframe()

Lazy-imports `pandas` (no hard dependency). Each row = one result with columns
title/url/domain/hostname/published_date/author/score/snippet/summary/
highlights. `include_entities=True` adds the raw `entities` column. Clean input
for dedup, stats, or CSV export.

## First-class new search types (live-verified)

- `search_type="instant"` — lowest latency for chat/autocomplete/voice; verified
  returns results fast.
- `search_type="deep-lite"` — consistent ~4s lightweight research with
  synthesis.
- `search_type="deep-reasoning"` — stronger reasoning for complex analysis /
  decision tasks; verified against a use case.
- `search_type="fast"` — high-quality reduced-latency.

All are already accepted by `SEARCH_TYPES` (with `auto` default); round 9 merely
confirms them live and documents the recommended use.

## Version

- Module header: v0.10
- pyproject.toml: 0.10.0
- SKILL.md header: v0.10
