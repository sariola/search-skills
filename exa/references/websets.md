# Persistent entity collections

Use Websets for a persistent collection of companies, people, articles, research
papers, or custom entities with criteria and enrichment. For a one-off page list,
use `search()` instead. `search(mode="websets")` does not create a collection.

The client uses `https://api.exa.ai/websets` plus `/v0/...` paths for Websets,
imports, enrichments, webhooks, events, team information, and webset monitors.
Availability and quotas depend on the account; inspect errors and `team_info()`
rather than assuming a personal plan's observed limits apply universally.

## Create and inspect

1. Translate the requested population into a query, entity type, and testable
   criteria. `webset_preview(query, entity=..., search_items=False)` helps inspect
   that interpretation without creating a collection; it is still an API request.
2. Create within the requested scope using `webset_create(query, count=...,
   entity=..., criteria=...)`. Save the returned ID before waiting.
3. `webset_wait(id, timeout=...)` waits for terminal searches/enrichments by default.
   Inspect each status for failure or cancellation. With `search_id`, it follows
   that search and does not guarantee all enrichments are finished.
4. `webset_items(id, limit=..., cursor=...)` retrieves a page; follow `nextCursor`
   and `hasMore`, or use `webset_items_all(id)`. `webset_get(id, include_items=True)`
   expands items but should not be treated as an unbounded complete export.
5. Review evidence for each criterion before calling the records qualified.

`webset_item_type`, `webset_item_name`, and `webset_item_summary` normalize item
labels. Each item's `evaluations` can carry a criterion, reasoning, satisfaction
mark, and supporting references. **`webset_items(satisfied="yes")` keeps items
with any yes evaluation, not necessarily all criteria satisfied.** For strict
qualification, verify every required criterion and handle missing/unclear marks.
`webset_eval_review()` is useful for a review page, not a complete paginated audit.

`recall=True` on creation/search requests a coverage estimate;
`webset_recall(id)` reads it when available. Report bounds and confidence as an
estimate, not the true market size. `exists=False` means no estimate is available.

## Extend and export

- `webset_add_search(id, query, behavior="append", ...)` starts another search.
  Preserve its ID for `webset_search_status` / `webset_search_cancel`.
- `webset_enrich(id, description, format=...)` adds a requested field. Inspect
  `enrichment_get` before relying on output; `enrichment_update`, cancel, and
  delete manage the job. Do not add contact enrichment unless the task needs it.
- `import_create(csv_path, title=..., entity=..., identifier_column=0)` uploads
  a local CSV. Send only the records needed for the requested operation.
  Use `import_get`, `imports_list`, `import_update`, or `import_delete` as needed.
- `webset_snapshot(id)` reads all items into a local URL-keyed snapshot;
  `webset_snapshot_diff(a, b)` compares membership and selected fields. It does
  not compare complete source-page contents or preserve every duplicate URL.
- `webset_update` edits metadata; `webset_cancel` stops work; `webset_delete` and
  `webset_item_delete` remove resources. Keep user-owned collections unless
  deletion is requested or part of an explicitly agreed temporary lifecycle.

On an ambiguous create/upload response or timeout, inspect existing resources
before retrying; another POST can create duplicate work and charges.

## Delivery and scheduled refresh

Use `webhook_create(events, url)` only for a requested destination. Keep any
returned secret out of output and logs; use it in the receiver's signature
verification. `webhook_attempts` accepts cursor, event-type, and success filters.
`event_list` supports types and creation-time bounds; follow pagination for an
audit. `webhook_update`, `webhook_delete`, and `webhook_get` manage that resource.

`wmonitor_create(webset_id, cron=..., timezone=..., query=..., count=...,
behavior=...)` schedules Webset search/refresh. Inspect the accepted cadence and
account support rather than assuming arbitrary frequency. Use `wmonitor_list`,
get, update, delete, runs, and run_get to manage the requested monitor. These
are distinct from the search `monitor_*` functions in [jobs.md](jobs.md).
