# Round 13 — v0.14 next-layer power (answer citationFormat + multi-schema agent_chat + JSONL export)

All surfaces below are live-verified against api.exa.ai (2026-08-07).

## 1. `/answer` `citationFormat` passthrough — `answer(..., citation_format=...)` + `stream_answer(..., citation_format=...)`

The live `/answer` endpoint accepts an undocumented **`citationFormat`** object
that lets you request/suppress specific citation fields per source. The skill
previously never forwarded it (and the returned citations carried whatever the
default was). Verified live: supplying `citationFormat` yields rich citation
dicts that populate `id`, `url`, `title`, `image`, `favicon`, `author`, and
`publishedDate` fields when the API knows them (fields are populated
conditionally per source — e.g. a Wikipedia citation rarely has `author`, but
a news article does).

```python
ans = exa.answer("Who is the CEO of Anthropic?",
                 citation_format={"id": True, "url": True, "title": True,
                                  "image": True, "favicon": True,
                                  "author": True, "publishedDate": True})
ans.citations[0]  # {"id": ".../Dario_Amodei", "title": "Dario Amodei",
                   #  "url": "...", "publishedDate": "2026-08-03T...",
                   #  "image": "...", "favicon": None, "author": None}

# SSE streaming variant — same citationFormat forwarded
parts = list(exa.stream_answer("What is the capital of Australia?",
                               citation_format={"id": True, "title": True, "favicon": True}))
meta  = parts[-1]
meta["citations"]   # rich dict incl. "favicon": "https://www.britannica.com/favicon.png"
```

Verified live: `answer()` returns `requestId`, `answer`, `citations`, and
`costDollars`; `stream_answer` with `citation_format` emits the requested
fields (including `favicon`) in the trailing citations dict. The field is
forwarded verbatim and defaults to API behavior when omitted (full backward
compat).

## 2. `agent_chat(..., schemas=[...])` — per-turn output_schema override

`agent_chat` now accepts a **list of JSON schemas** (`schemas`), one per turn,
letting you chain a progressive multi-schema pipeline: turn 0 extracts a base
record, turn 1 enriches it with additional fields, turn 2 adds yet more — each
turn's completed run validates against its own schema. Falls back to the single
`output_schema` (or none) for turns beyond the provided list. `schemas` length
is capped at `turns`.

```python
exa.agent_chat(
    "What is the parent company of Instagram?",
    turns=2,
    schemas=[
        {"type":"object","properties":{"company":{"type":"string"}},"required":["company"]},
        {"type":"object","properties":{"company":{"type":"string"},
                                       "headquarters":{"type":"string"}},
                              "required":["company"]},
    ],
    effort="minimal",
)
out["turns"][0].structured  # {"company": "Meta Platforms, Inc."}
out["turns"][1].structured  # {"company": "Meta Platforms, Inc.", "headquarters": "..."}
```

Live-verified over a 2-turn run: turn 0 produced `{company}`, turn 1 refined
to `{company, headquarters}` — distinct schemas honoured per turn.

## 3. `search_to_jsonl(results, path)` — JSON-native line-delimited export

`search_to_jsonl` writes one compact JSON object per result per line (title,
url, domain, hostname, published_date, author, score, snippet, summary,
highlights, and by default the raw `entities` list). Purely stdlib `json` —
**no pandas dependency** (unlike `search_to_csv` / `to_xlsx`), so it works in
lean environments and pipes cleanly into `jq`/ndjson loaders.

```python
exa.search_to_jsonl(exa.search("AI funding news", num_results=3), "/tmp/ai.jsonl")
# {"title": "DeepSeek made AI cheap...", "url": "...", "domain": "thenextweb.com", ...}
```

Verified live on a 3-result search.

## Scope / compat

All three additions preserve **full backward compat**: no existing public
entry point, signature, or return value changed; every prior parameter still
behaves identically when the new opt-in arguments are omitted. `__all__` goes
96 → 97 with `search_to_jsonl` (plus the new keyword args on `answer`,
`stream_answer`, and `agent_chat`).
