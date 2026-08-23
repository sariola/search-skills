# Round 11 — v0.12 next-layer power (pagination/filter parity + webset item helpers)

All surfaces below are live-verified against api.exa.ai / api.exa.ai/websets (2026-08-07).

## 1. `webset_get(..., include_items=True)` → native `expand=items`

**Before:** two round-trips — `GET /websets/{id}` then `GET /websets/{id}/items`.
**After:** `webset_get(ws, include_items=True)` sends `GET /websets/{id}?expand=items`
and the server embeds items in the response's `"items"` key. One round trip.

```python
data = exa.webset_get("webset_01kzdy...", include_items=True)
data["items"]  # [WebsetItem, ...] already embedded server-side
```

(Verified live: `expand=items` returns the same items as a separate
`webset_items()` call — embedded, no per-item latency.)

## 2. `agent_list(..., cursor=...)`

The `/agent/runs` list now accepts a `cursor` to walk the response's
`hasMore`/`nextCursor`. Previously the cursor was returned in the response but
ignored.

```python
page1 = exa.agent_list(limit=5)            # {"data":[...], "hasMore": True, "nextCursor": "..."}
page2 = exa.agent_list(limit=5, cursor=page1["nextCursor"])
```

## 3. `agent_events(run_id, limit=..., cursor=...)`

The `/agent/runs/{id}/events` endpoint paginates. Previously only `run_id`
was forwarded.

```python
events1 = exa.agent_events(run_id, limit=2)                # {"data":[...], "hasMore", "nextCursor"}
events2 = exa.agent_events(run_id, limit=2, cursor=events1["nextCursor"])
```

## 4. `webhook_attempts(..., cursor=..., event_type=..., successful=...)`

The `/v0/webhooks/{id}/attempts` endpoint accepts three extra params:
`cursor`, `eventType`, and `successful` (boolean filter on whether the delivery
succeeded). The wrapper now forwards them all.

```python
ok    = exa.webhook_attempts(wh_id, successful=True, limit=5)
by_event = exa.webhook_attempts(wh_id, event_type="webset.search.completed")
page2 = exa.webhook_attempts(wh_id, cursor=ok["nextCursor"])
```

## 5. `event_list(..., types=..., created_before=..., created_after=...)`

The WebSets audit-log `/v0/events` endpoint accepts multiple filters that were
previously dropped:
- `types` — plural, list of event type strings (e.g. `["webset.created", "import.completed"]`)
- `createdBefore` / `createdAfter` — ISO-8601 timestamps.

`event_type` (singular) remains supported as an alias; when both `event_type`
and `types` are given they combine (without mutating the caller's list).

```python
exa.event_list(types=["webset.created", "webset.search.completed"], limit=10)
exa.event_list(created_after="2026-08-01T00:00:00Z")
exa.event_list(event_type="import.completed", created_before="2026-08-05T00:00:00Z")
```

## 6. `webset_preview(query, entity=..., count=...)`

The `/v0/websets/preview` endpoint accepts an `entity` hint and a `count`
(when returning preview items). The wrapper now forwards both.

```python
preview = exa.webset_preview(
    "SF cybersecurity startups",
    search_items=True,
    entity={"type": "company"},
    count=3,
)
preview["items"]  # [WebSetItemCompanyProperties, ...]
```

## 7. WebSet item helpers

WebSet items have a `properties.type` discriminator that drives their
structure (person/company/article/research_paper/custom). Three ergonomic
helpers now exist:

```python
exa.webset_item_type(item)      # -> "company" | "person" | "article" | "research_paper" | "custom"
exa.webset_item_name(item)      # -> human-friendly name (company name, person name, ...)
exa.webset_item_summary(item)   # -> "[company] — Acme Inc. — relevance description"
```

And a pagination convenience:

```python
all_items = exa.webset_items_all("webset_01kzyd...")   # all records, cursor-paged
```

## API parity notes

| Surface        | Before                     | After                                 |
|----------------|----------------------------|----------------------------------------|
| webset_get     | 2 calls for items          | 1 call (`expand=items`)                |
| agent_list     | no cursor                  | cursor pagination                       |
| agent_events   | no limit/cursor            | limit + cursor                         |
| webhook_attempts| limit only                | cursor + eventType + successful         |
| event_list     | singular type only         | plural types + created_before/after     |
| webset_preview | query (search_items) only   | + entity hint, count                    |
| webset items   | raw dict access            | type/name/summary helpers + all-pager   |

## Version bump

`exa` skill now v0.12 (module docstring, pyproject 0.12.0, SKILL.md).
All surfaces verified live; module compiles and imports cleanly.
Test webset `webset_01kzdy...` created & cleaned up.
