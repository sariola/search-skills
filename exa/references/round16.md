# Round 16 — v0.17 next-layer power (entity roster + entity schema explorer + topic term clustering)

All three surfaces below are **client-side composites**: they run a real
`search()` against the live api.exa.ai and then post-process the returned
results locally. They are deterministic, fully backward-compatible, and
**live-verified** (2026-08-07). This round deliberately does NOT ship backend
params that the API *accepts* but that provably do nothing on every observed
query (see the absent-by-design note at the bottom).

## 1. `exa.entity_search(query, ...)` — auto-extract a deduped entity roster

Runs `search()`, then walks every `entities[]` array across the top results,
normalizes each raw record with `entity_summary()`, de-duplicates on the stable
library `id` (falling back to `name|type`), and ranks the roster by
`_occurrences` — how many distinct source pages surfaced each entity.

Best on entity-backed categories. All normal `search()` kwargs pass through.

```python
await exa.entity_search("major AI research companies",
                        num_results=5, category="company")
# -> {"query": ..., "category": "company", "entity_count": 5,
#     "entities": [{id, type, name, ..., "_occurrences": N}, ...],
#     "total_cost": {total: ...}}
```

In plain `"auto"` mode the API only returns `entities[]` when the result set
happens to carry them (so `entity_count` can be 0 — that is honest, not a a
bug; pass an entity-backed `category` to guarantee profiles).

## 2. `exa.entity_schema(entity=None)` — standalone entity attribute explorer

Evaluates which of an entity kind's known attributes are present in a sample
record (also the JSON kind: dict / list / primitives), which optional ones are
missing, and any unexpected extra keys. With no sample (`{}`/`None`) it enters
discovery mode and returns the complete field map for every entity kind:

```python
sch = await exa.entity_schema(raw_entity)   # company / person / publication
sch["present_attributes"]                     # {name: "str", workforce: "dict", ...}
await exa.entity_schema({})                    # discovery: known_attributes per kind
```

Complements `entity_summary` (flattened values) and `entity_type`
(kind discriminator).

## 3. `exa.top_terms(query, ...)` — TF/domain clustering of a result set

Runs `search()`, tokenises every result title (English stop-words stripped),
tallies term frequencies, and groups results by their registrable domain. A
cheap local "shape of the result set":

```python
tt = await exa.top_terms("machine learning transformers", num_results=10)
tt["top_terms"]         # [{"term": "transformers", "count": 4}, ...]
tt["domain_clusters"]   # [{"domain": "arxiv.org", "count": 2}, ...]
```

## 4. Backend params probed but intentionally NOT exposed (absent-by-design)

The round's rule: only ship what truly verifies live; anything that 400s,
301s, HTMLs, or is *inert-accepted* (API returns 200 but the field has no
observable effect) is documented as absent. All four probes below were
accepted with HTTP 200 but produced byte-identical result sets regardless of
their values, on every mode/query tried:

- `startCrawlDate` / `endCrawlDate` on `/search` **and** `/contents` — a
  1999-01-01..1999-01-02 or 2099 crawl window returned the same cached page as
  no window at all.
- `autoprompt: false` (full-OFF toggle) on `/search` — accepted, but results
  identical to `autoprompt: true` on the probed queries.
- `similarityThreshold` on `/search` and `/findSimilar` — 0.0 vs 0.99 returned
  identical result sets across keyword / neural / auto.

These remain reachable only by hand-building a raw request; they are not new
skill APIs.
