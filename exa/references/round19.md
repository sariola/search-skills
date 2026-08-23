# Round-19 — clean engine (pooled HTTP + deadline polls)

## Engine

- Process-lifetime `httpx.Client` for api.exa.ai and ancillary GETs
  (GitHub, HuggingFace). Keep-alive; no per-call handshake.
- `_fanout` one-way work channel (submit all, collect in order).
- `_poll_until`: exponential backoff from 250ms, capped at the caller
  `poll_interval`. Replaces fixed `sleep(2)` / `sleep(3)` / `sleep(5)`
  on `agent()`, `monitor_wait()`, `webset_wait()`. Jobs that finish in
  <1s no longer wait a full poll interval.

## Agent contract

- `Result.to_agent()` / `SearchResults.to_agent()` / `brief()` return
  `{title, url, snippet, score, ...}` with nulls stripped.
- Prefer `run()` / `brief()` / `answer()` in a model turn.
