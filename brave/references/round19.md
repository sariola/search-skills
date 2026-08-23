# Round-19 — clean engine (agent contract + one-way I/O)

Previous rounds added surface area. This round fixes the transport and the
agent-facing result contract.

Measured before (live): `search("core.async Rich Hickey", count=5)` returned
61,596 JSON chars. `mixed` + `top_results` alone were ~30k of duplicated SERP.
An agent-slim view of the same query is 2.3k chars (~26x).

## Engine

- Process-lifetime `httpx.Client` (keep-alive pool). No per-call TLS.
- `_fanout`: submit all jobs, collect in order. Workers do not mutate a shared
  dict. No `threading.Thread` + `sleep(0.05)` busy-wait (removed from `batch`).
- Retries honor `Retry-After` when present.

## Agent contract

- `search(view="agent")` is the default. Drops `mixed`/`top_results`, empty
  fields, and unused result keys. `view="full"` is the historical SERP.
- `brief()` is an explicit alias of the agent view.
- Internals call `_search_full` so helpers that need schema blocks stay intact.

## Fan-out

`batch`, `mosaic`, `crawl`, `mode="all"`, `thumbnails` now run independent
HTTP on the worker channel. `batch` returns the actual `search` payload per
query (not just titles).
