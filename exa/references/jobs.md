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
and `agent_trace(id)` help inspect runs. Cancel or delete only runs within the
user's requested scope; do not delete completed results merely as cleanup.

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
