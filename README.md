# search-skills

Reusable agent skills and Python clients for Exa and Brave Search, plus
Model Context Protocol (MCP) servers for both. Each skill starts with task
selection and evidence handling; focused references cover advanced operations.
The clients can be used independently or as complementary search paths.

## Install and use

Python 3.10+ and `uv`:

```bash
uv venv
uv pip install --python .venv/bin/python -e ./exa -e ./brave
```

Configure `EXA_API_KEY` and `BRAVE_API_KEY` (or `BRAVE_SEARCH_API_KEY`) in your
environment. The clients also support existing Prime dotenv/key-store fallbacks.

```python
import exa
import brave

papers = exa.search("incremental view maintenance", mode="paper",
                    num_results=5, with_highlights=True)
print(papers.to_agent())

pages = brave.search('site:docs.python.org "TaskGroup"', count=5)
for page in pages.get("results") or []:
    print(page.get("title"), page.get("url"), page.get("snippet"))
```

Use `exa.run(...)` / `brave.run(...)` for readable text. No external harness or
CLI launcher is required. The functions accept awaitable results for harness
compatibility but still perform blocking I/O; use threads for event-loop use.

To install as agent skills, copy or link each complete skill directory into your
agent's global skills directory. Keep `src`, `pyproject.toml`, and `references`
with `SKILL.md`; names alone are not dependencies or installed packages.

## Skill guides

- [Exa](exa/SKILL.md): semantic discovery, content, papers, code, and entities.
  References cover [search](exa/references/usage.md),
  [developer research](exa/references/developer.md),
  [hosted jobs](exa/references/jobs.md), and [Websets](exa/references/websets.md).
- [Brave](brave/SKILL.md): web, news, media, local, and exact-term discovery.
  References cover [the client](brave/references/api.md),
  [developer research](brave/references/developer.md), and
  [verticals](brave/references/verticals.md).

These are custom clients, not the vendors' official SDKs. Python signatures come
from the bundled source; current endpoint support and entitlement come from the
provider. Convenience helpers can make multiple requests and return heuristics
or generated summaries. Verify substantive claims against original sources.

## MCP servers

Each provider also ships a standalone Model Context Protocol server under
`mcp/`, built to the same structure as a reference-quality Linear MCP: a
`RateLimiter` that serializes API calls and tracks request metrics, a typed
client with per-endpoint error classification, JSON-Schema tool contracts,
addressable resources + resource templates, a server prompt, and API metrics
attached to every tool response. Both run over stdio.

- [Exa MCP server](mcp/exa/README.md) — `exa_search`, `exa_fetch`, `exa_answer`,
  `exa_agent`, `exa_find_similar`; resources `exa-team:` and
  `exa-content:///{url}`. Requires `EXA_API_KEY`.
- [Brave Search MCP server](mcp/brave/README.md) — `brave_web_search`,
  `brave_news_search`, `brave_video_search`, `brave_image_search`,
  `brave_place_search`, `brave_rich_search`; resources `brave-about:` and
  `brave-status:`. Requires `BRAVE_API_KEY` / `BRAVE_SEARCH_API_KEY`.

Each server is a self-contained package: `bun install && bun run build`, then
point your MCP host at `build/index.js` (see each README for the Claude Desktop
and Claude Code config). No credentials are ever printed.

Both servers follow the Linear MCP decision-making style: the tool set stays
small, but the high-impact knobs that actually shape results are exposed with
inline defaults and allowed values so a model picks them without guessing —
e.g. Exa published-date ranges, search depth, vertical `category`, and agent
effort; Brave `freshness` (incl. date ranges), per-result `extra` snippets,
image licensing/transparency filters, and place `radius`. Every tool errors
with `isError: true` plus a machine-readable `category`, and enum/range args
are validated client-side before the wire call.

## Validation

```bash
uv run --with ./exa --with ./brave python -m unittest discover -s tests
```

The offline suite covers defect regressions, search contracts, return-shape
stability (pinned against the Phase 1 signature baseline), the dual sync/async
wrapper semantics, byte-identical shared mirrors, and public-surface smoke.
The tests use local fixtures and do not need keys or make paid API calls.

`python -W error::ResourceWarning -m unittest discover` should also report no
resource leaks. `.analysis/live_verify.py` exercises the full documented surface
against the live APIs with bounded counts and writes a pass/fail baseline
(requires valid keys).

MCP servers (each requires its key + a built `build/index.js`):

```bash
cd mcp/exa    && bun run typecheck && bun run build && bun smoke.mjs && bun probe.mjs
cd mcp/brave  && bun run typecheck && bun run build && bun smoke.mjs && bun probe.mjs
```

`tsc --noEmit` must be clean; `smoke.mjs` connects over stdio and drives a live
call; `probe.mjs` exercises the granular parameters (date ranges, category,
`extra` snippets, image filters, place `radius`) plus client-side validation
guards. A strict JSON-RPC framing check asserts every stdout line is valid JSON
(the regression that `console.log` on stdout would cause).

## License

[MIT](LICENSE) — Copyright (c) 2026 karolus.
