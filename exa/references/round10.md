# Exa skill v0.11 — Round 10 next-layer power

Verified live against api.exa.ai (2026-08). Surfaces request-level latency + per-mode
cost metadata (previously dropped), typed subpage access, a fetch metadata envelope,
and markdown renderers for answers/agent runs.

## searchTime latency metadata

The live `/search`, `/findSimilar`, and `/contents` responses carry a `searchTime`
field (request latency in milliseconds) that the skill previously discarded. Now
surfaced everywhere:

| Surface | Field / value |
|---|---|
| `SearchResults` | `search_time_ms` (and `.search_time`) — live: `search("Clojure agents")` → `~920.6 ms` |
| `search()`, `find_similar()` | forwarded from `searchTime` |
| `deep_research()` / `merge_searches()` | aggregated sum of member `search_time_ms` |
| `fetch(..., include_meta=True)` | `search_time_ms` in the returned envelope |
| `stream_search` | `search_time_ms` in the final (already-rich) dict, captured from SSE events |

The `deep` search type returned `search_time_ms=4075.2` (its slower multi-stage
pipeline), confirming the latency measurement is meaningful per request type.

## Cost helpers (flat + nested breakdown)

`costDollars` is emitted either flat (`{"total": 0.012}` for `/deep`) or nested
(`{"total": 0.007, "search": {"neural": 0.007}}` for `/search`). The new helpers
never raise on either shape:

- `SearchResults.total_cost()` → USD float (or `None` when unreported).
- `SearchResults.cost_breakdown()` → `{"total": ..., "modes": {mode: cost}}`
  (flat totals are exposed as an `overall` mode).
- `SearchResults.cost_report()` → one-line string like
  `"$0.0120 total / modes: neural $0.0070"`.
- `SearchResults.to_meta()` → request envelope dict;
  `to_json(include_meta=True)` wraps the results with it.

Live-verified: `search("Clojure concurrency")` → `cost_report()` =
`"$0.0070 total / modes: neural $0.0070"`; `search(..., type="deep")` →
`{"total": 0.012, "modes": {"overall": 0.012}}`.

## fetch(include_meta=True)

Opt-in envelope so request metadata isn't lost on the fetch path while the
default returns (list of dicts / single-text string) stay byte-compatible:

```python
exa.fetch(["https://clojure.org/about/history"], with_summary=True,
          include_meta=True)
# -> {"results": [...], "request_id": "...",
#     "cost_dollars": {"total": 0.002, "contents": {...}},
#     "search_time_ms": 1964.1}
```
Single-URL text form additionally returns `{"rendered": "...", "results": [...], ...}`.

## stream_search final-dict latency

The streaming `/search` SSE events carry a `searchTime`; the final yielded dict
now includes it (`search_time_ms`) alongside results/output/grounding/citations/
text/request_id. Live-verified at `921.8 ms` on a streamed schema-synthesis.

## Result subpage accessors

`subpages` rarely populate for ordinary queries (live probes returned `[]` for
several documents — the API gates subpage extraction), but the accessors are
correct against the documented item shape (`title/url/publishedDate/author/id/
image/favicon`) and degrade gracefully to empty/`None`:

- `Result.subpage_count`, `Result.subpage_titles()`, `Result.subpage_urls()`
- `Result.subpage(index)` → typed dict (or `None`)
- `Result.to_subpages()` → list of typed dicts
- `Result.to_dict()` now emits `subpages` (count)

## Answer / AgentRun markdown renderers

```python
exa.answer("What is Clojure?").to_markdown()
# ## Answer / Clojure is a dynamic, functional ... [1][2][3] / ### Sources / ...

exa.agent("...", effort="minimal").to_markdown()
# ## Agent Run / **Status:** `completed` · **Run id:** ... · **Cost:** $0.0120
# ground-truth answer text / ### Sources / 1. **Clojure** — [https://clojure.org]
```

Live-verified both against real endpooint responses; the `agent()` test run was
deleted afterwards (cleanup), and no websets were created this round.
