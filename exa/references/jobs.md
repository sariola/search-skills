# Hosted research, streams, and monitors

## Agent research

Use `agent()` when the task needs a hosted multi-step synthesis. It creates a
remote run and can cost more than search. Set effort and scope proportionally;
`agent_chat(turns=N)` creates a run per turn, not a local chat loop.

```python
run = exa.agent("Compare the documented snapshot restore approaches of Firecracker and QEMU",
                system_prompt="Use official documentation and cite each comparison.",
                effort="minimal", timeout=120)
print(run.id, run.status)
print(run.text)
print(run.citations)
```

Inspect `status` and output, not merely return from the function. `output_schema`
requests structured output in `run.structured`; inspect missing/null fields and
validate against your downstream contract. `agent_structured(query, schema)`
checks that structured output exists; it is not a local JSON Schema validator.
`run.grounding` and `run.citations` link fields to sources. Confidence is provider
assessment, not a guarantee. `run.cost`, `usage`, and `connects` report available
spend; provider omission does not prove a zero-cost overall run.

`previous_run_id` continues prior research; `data` / `exclusion` pass records.
`data_sources` requests Connect providers (client limit: five); use only when
those providers are needed and available. `agent_chat(..., schemas=[...])`
can vary the requested schema by turn; inspect each turn's actual output.

On timeout the remote run may still be running. Preserve the ID in the error
and inspect it with `agent_get(id)` instead of resubmitting the same job.
`agent_list(limit=..., cursor=...)`, `agent_events(id, limit=..., cursor=...)`,
and `agent_trace(id)` help inspect runs.

Stopping a run is a distinct choice from cancelling it: `agent_stop(run_id,
reason=...)` tells the run to stop cleanly at the next safe checkpoint so
partial work can be kept, whereas `agent_cancel(run_id)` aborts it (final status
`cancelled`). Use `agent_stop` when the requested output is already satisfied or
the budget is exhausted; use `agent_cancel` to abandon the run. Stop reasons are
`schema_satisfied`, `budget_reached`, `stopped`, `error`, or `cancelled`
(default `budget_reached`). Cancel or delete only runs within the user's
requested scope; do not delete completed results merely as cleanup.

Graceful stop is supported **only for effort=max runs** and requires the
`Exa-Beta: agent-max-effort-2026-07-27` header (sent automatically by the client
and by `exa_agent_stop`). For a lower-effort run the API returns 400
(`ExaBadRequestError`) — use `agent_cancel` instead. If the run has already
reached a terminal status, the existing run is returned unchanged.

## Batches

Batches run many sub-requests asynchronously against `/search` or
`/agent/runs` and return results in one pass, which is cheaper and steadier than
firing N synchronous calls. The lifecycle:

- `batch_create(requests)` creates the batch. Each sub-request dict must carry
  `url` (`"/search"` or `"/agent/runs"`) and `body` (that route's payload);
  `customId` (your own 1–64-char handle that keys the result) and `method` are
  auto-filled when absent (`customId` defaults to `req-<index>`). Returns the
  batch object with `id` (`batch_...`), `status`, `requestCounts`, and
  `resultsUrl` (null until complete).
- `batch_get(batch_id)` / `batch_list(limit=..., cursor=...)` inspect status
  and enumerate batches.
- `batch_cancel(batch_id)` stops an in-progress batch; `batch_delete(batch_id)`
  deletes it and its results (irreversible) — use only within the user's scope.
- `poll_batch(batch_id, timeout=150.0, poll_interval=3.0, fetch_results=True)`
  waits for a terminal state (`completed` / `cancelling` / `cancelled` /
  `expired`) and, on completion, downloads the presigned JSONL `resultsUrl`,
  returning `{"batch", "results": {customId: {...}}, "results_lines",
  "results_url"}`. It raises `ExaError` on a failed terminal state or when the
  wall-clock budget is exceeded.

Batch endpoints are plan-gated: on a plan without batch access they raise
`ExaPlanError` (403). The batch object's `expiresAt` is when uncollected
results are dropped, so poll promptly. `timeout` is a waiting limit, not a
guaranteed cancellation or a strict spend cap.

## Answers and streaming

`answer(question)` returns an `Answer` with `answer`, `citations`, and `sources`.
`output_schema` requests structured content. `text=True` includes heavier source
content. `citation_format` is a pass-through option; fields remain optional.

`stream_answer()` and `stream_search()` are synchronous generators. Iterate
normally and distinguish text/event chunks from their final metadata dict.
`stream_answer(..., collect_meta=True)` retains citations and usage;
`stream_answer_source()` produces typed events including `delta`, `citations`,
`cost`, and `done`. Its token estimates are character-based approximations.
If a stream ends without terminal metadata, do not report it as fully completed.
Use `answer()` when incremental display is unnecessary. `magic()` composes a
deep search and an answer; one helper call still incurs both requests.

## Recurring search monitors

Create recurring resources only when monitoring is requested. Respect an existing
automation chosen by the user or host; do not silently create a second scheduler.
Select the query, cadence, destination, and relevant result fields from the task.

- `monitor_create(query, period=..., webhook_url=..., ...)` creates a search monitor.
- `monitor_get(id)`, `monitor_list(...)`, `monitor_runs(id)`, and
  `monitor_run_get(id, run_id)` inspect existing state.
- `monitor_trigger(id)` starts a run. `monitor_wait(...)` waits with a deadline;
  a terminal failed/cancelled state is not success.
- `monitor_check(id, trigger_if_empty=False)` is the read-only choice; its
  default `True` can start a run when no prior output exists.
- `monitor_update(id, ...)` changes configuration; `monitor_delete(id)` deletes.
- `monitor_batch(action, ..., dry_run=True)` previews matching resources before
  a bulk mutation. Check IDs and scope before executing with `dry_run=False`.

Search monitors use `/monitors` on `api.exa.ai`. Webset monitors are a distinct
resource under the Websets base; see [websets.md](websets.md). Poll helpers use
backoff, but timeout is a waiting limit, not guaranteed cancellation or a strict
end-to-end spending cap.
