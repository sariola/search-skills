# Round 15 — v0.16 next-layer power (agent trace + typed answer streaming + webset snapshot + explain/magic)

All surfaces below are **live-verified** against api.exa.ai / api.exa.ai/websets
(2026-08-07). The additions span three layers of the skill: agent-run deep
trace access, typed SSE streaming with per-chunk metering, and client-side
composite conveniences (webset corpus snapshots, `explain`, `magic`).

## 1. `exa.agent_trace(run_id)` → `AgentTrace` — typed run-step audit

The `/agent/runs/{id}/events` endpoint streams per-step events while a run
executes (or after completion). Previously only surfaced through the raw
`agent_events()` cursor-paged list, these events carry a rich trace: lifecycle
(`agent_run.created` / `started` / `completed`), tool-call openings
(`output_item.added` with `{name: 'search', type: 'function_call', call_id,
args}`), argument finalization (`function_call_arguments.done`), per-source
attachments (`source.added` with `url`/`title`/`callId`), source overflows
(`source.truncated`), and tool completions (`output_item.done` with
`metadata.sourceCount`).

`agent_trace(run_id)` fetches the entire trace (auto-paginating the cursor),
and returns an `AgentTrace`:
```python
tr = exa.agent_trace(run_id)
tr.status                # "completed"
tr.tools                 # [ {name:'search', call_id, sources:[urls], added_at, done_at, ...} x6,
                         #   {name:'finish', ...} ]
tr.tool_names            # ['search','search','search','search','search','search','finish']
tr.search_count          # 6
tr.sources               # {url: {title, url, call_id}, ...} unique sources (27)
tr.source_count          # 27
tr.searches              # 12  (usage.searches from completed event)
tr.agent_compute_units   # 0.4
tr.duration_seconds      # 41.8
tr.cost                  # 0.10
tr.to_markdown()         # readable audit sheet (run header, tools, sources, answer, events)
tr.to_dict()             # full structured dict
```
Verified live on a 6-search + finish run (75 events → tools, 27 unique sources,
source truncations tracked).

## 2. `exa.stream_answer_source(query, ...)` — typed SSE answer events

Unlike `stream_answer` (bare strings), this yields typed dict events so you can
observe each SSE frame with per-chunk metering:
```python
events = list(exa.stream_answer_source("What is the capital of Japan?",
                                       citation_format={"id": True, "url": True}))
# [{kind:'delta',  text:'Tokyo is', chunk_index:1, chars:8, cumulative_chars:8,
#   tokens_estimate:2},
#  {kind:'delta', ...},
#  {kind:'citations',  citations:[{...}], source_count:8},
#  {kind:'cost',      cost_dollars:0.005},
#  {kind:'done',      text:'...', citations:[...], cost_dollars:0.005,
#   total_chars:320, total_tokens:80, request_id:'...'}]
```
`tokens_estimate` is a client-side heuristic (`chars // 4`) — not an official
token counter. Resilient multi-line SSE-frame buffering (handles frames split
across network lines); preserves `citation_format` passthrough and
`output_schema` (as `outputSchema`). Verified live on /answer streaming.

## 3. `webset_snapshot(wsid)` + `webset_snapshot_diff(a, b)` — client-side corpus diff

```python
snap_a = exa.webset_snapshot("webset_01kze1c23brjh24bchwdadbr25")
# {"webset_id", "taken_at", "item_count", "items_by_url": {url: {...}}, "urls": [...]}

snap_b = exa.webset_snapshot("webset_01kze1c23brjh24bchwdadbr25")   # later
diff = exa.webset_snapshot_diff(snap_a, snap_b)
# {"added":[...], "removed":[...], "kept":[...], "changed":[...],
#  "add_count","remove_count","change_count","kept_count","taken_at_a","taken_at_b"}
```
Takes a local URL-keyed snapshot of every `webset_items_all` row; diff reports
which URLs entered / left the corpus over time, and which kept URLs changed
their description between snapshots. Purely client-side — no extra API call.
Verified live on an "AI security startups" webset (5 companies, 1 synthetic add
correctly reported).

## 4. `exa.explain(url)` — one-call readable page explainer

```python
x = exa.explain("https://clojure.org/")
x["title"]      # "Clojure"
x["summary"]    # "Clojure is a dynamic ..."
x["highlights"] # [...]
x["markdown"]   # read: title / URL / summary / highlights
```
Calls `fetch(url, with_summary=True, with_highlights=True)` in one request, then
renders a readable one-pager. Verified live on clojure.org.

## 5. `exa.magic(query)` — one-call research pipeline

```python
m = exa.magic("What is Reitit routing?", num_results=3)
m["summary"]      # "Reitit is a fast, data-driven routing library..."
m["citations"]    # [citation dicts]
m["results"]      # SearchResults (deep)
m["markdown"]     # header + summary + citations + top-deep-results
```
Runs `search(search_type="deep")` + `answer()` in one call, and returns the
summary, the cited sources, the deep results, and a combined markdown digest.
Verified live on "What is Reitit routing?".

## Scope / compat

All additions preserve **full backward compat** — no existing signature or
return type changed. `__all__` goes 100 → 107 with `AgentTrace`, `agent_trace`,
`stream_answer_source`, `webset_snapshot`, `webset_snapshot_diff`, `explain`,
and `magic`. No new undocumented API assumption: everything decomposes into
endpoints already wrapped by the skill (`/search`, `/answer`, `/agent/runs`,
`/agent/runs/{id}/events`, `/websets/{id}/items`, `/contents`).
