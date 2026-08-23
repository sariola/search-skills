# Exa skill v0.7 — Round 6 next-layer power

Verified live against api.exa.ai (2026-08-07). All features below work with the
current public API.

## New content-control params

| Param | Functions | Payload field | Live-verified? |
|---|---|---|---|
| `extras_rich_image_links: int` | search, fetch, find_similar, monitor_create, deep_research | `contents.extras.richImageLinks` | ✓ (URL + alt text per hit) |
| `compliance="hipaa"` | search, run | `compliance` | ✓ (403 on non-enterprise — expected) |
| `exclude_source_domain: bool` | find_similar | `excludeSourceDomain` | ✓ (filters seed-domain hosts) |

## monitor_create() content parity

Now passes every `_contents_block` option through to search monitors:
`text_verbosity`, `highlights_max_characters`, `summary_schema`,
`extras_links`/`image_links`/`rich_links`/`rich_image_links`/`code_blocks`,
`max_age_hours`, `subpages`, `subpage_target`, `include_sections`/
`exclude_sections`, `include_html_tags`, `livecrawl`, `livecrawl_timeout`.

## New convenience surfaces

- `merge_searches(*results, dedupe=True)` — merge SearchResults groups,
  URL-deduplicated, honors query_hits ranking.
- `monitor_check(monitor_id, trigger_if_empty=True)` — peek latest run output,
  auto-trigger a fresh run when none exists.
- `SearchResults.to_markdown(with_metadata=True)` — ref-friendly rendering
  (numbered titles, linked sources, snippets, publish dates).

## OpenAPI gaps closed

| Gap | Old state | New state |
|---|---|---|
| `richImageLinks` | not exposed | `extras_rich_image_links` param |
| `compliance` (HIPAA) | not exposed | `compliance="hipaa"` param |
| `excludeSourceDomain` | not exposed | `find_similar(exclude_source_domain=True)` |
| `extras_rich_links` on fetch | missing | added |
| monitor content controls | minimal | full parity with search |
| deep_research synthesis+content | narrow | `output_schema`, `system_prompt`, `user_location`, extras, etc. |

## Version

- Module header: v0.7
- pyproject.toml: 0.7.0
