# Exa MCP Server

A [Model Context Protocol](https://github.com/modelcontextprotocol) server for the
[Exa](https://docs.exa.ai) search & web-reading API (`api.exa.ai`).

Built to the same quality bar as the Linear MCP reference: a serialized
**RateLimiter** with per-request metrics, a typed REST client with classified
errors, JSON-Schema tool contracts, addressable resources, a server prompt, and
`apiMetrics` attached to every tool response. Runs on Bun (or Node) over stdio.

## Installation

1. Create an Exa API key: <https://dashboard.exa.ai/api-keys>

2. Configure your MCP client (e.g. Claude Desktop, Claude Code). The server reads
   `EXA_API_KEY` from the environment (or a `.env` file next to the server).

   Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "exa": {
         "command": "bun",
         "args": ["/abs/path/to/search-skills/mcp/exa/build/index.js"],
         "env": { "EXA_API_KEY": "your_exa_api_key" }
       }
     }
   }
   ```

   Claude Code:

   ```bash
   claude mcp add exa --env EXA_API_KEY=your_exa_api_key \
     -- bun /abs/path/to/search-skills/mcp/exa/build/index.js
   ```

## Components

### Tools

Parameter values are drawn from the public Exa OpenAPI; each tool's
`inputSchema` documents inline defaults and allowed values so a model can make
the right granular choice.

1. **`exa_search`** — primary web search (`POST /search`)
   - Required: `query`
   - Recency (the biggest lever): `startPublishedDate` / `endPublishedDate`
     (ISO 8601) — only results published in that window are returned.
   - `numResults` (1–100, default 10), `searchType`
     (`instant` / `fast` / `auto` (default) / `deep-lite` / `deep` /
     `deep-reasoning`), `category` (`company` / `publication` / `news` /
     `personal site` / `financial report` / `people`; other strings act as
     hints).
   - Domains: `includeDomains`, `excludeDomains` (use these instead of `site:`
     in the query).
   - Per-result content: `withHighlights` (+ `highlightsQuery`), `withText`
     (+ `textMaxCharacters` 1–10000, `textVerbosity` `compact`/`standard`/
     `full`), `withSummary` (+ `summaryQuery`), `links` / `imageLinks` (0–1000).
   - `userLocation` (2-letter ISO), `outputSchema` (adds a synthesized,
     citation-grounded answer to the same call).
   - Note: `company`/`people` do **not** support the date filters or
     `excludeDomains` (the API returns 400); the server rejects that combo
     client-side.

2. **`exa_fetch`** — deep-read URLs / document ids (`POST /contents`)
   - `urls` or `ids` (one required), `withText` (+ `textMaxCharacters`,
     `textVerbosity`), `withHighlights` (+ `highlightsQuery`), `withSummary`
     (+ `summaryQuery`), `links` / `imageLinks` (0–1000), `subpages` (0–100),
     `subpageTarget`, `maxAgeHours` (0 = force fresh, -1 = cache).

3. **`exa_answer`** — citation-grounded answer (`POST /answer`)
   - Required: `query`; optional `text`, `outputSchema`, `systemPrompt`,
     `userLocation`.

4. **`exa_agent`** — full agentic research run (`POST /agent/runs`, polled)
   - Required: `query` (top-level, per `CreateAgentRunRequest`); optional
     `effort` (`minimal`/`low`/`medium`/`high`/`xhigh`/`max`/`auto`),
     `systemPrompt`, `outputSchema`, `maxCostDollars` (1–100, default $5 cap on
     `auto`), `timeout` (default 120s). Requires a plan that enables
     `/agent/runs`.

5. **`exa_find_similar`** — more-like-this (`POST /findSimilar`)
   - Required: `url`; optional `numResults` (1–100), `category`,
     `excludeSourceDomain`, `includeDomains`, `excludeDomains`,
     `startPublishedDate` / `endPublishedDate`.

### Resources

- `exa-team:` — the authenticated team/plan (limits, credits).
- `exa-content:///{url}` — fetch + summarize a single URL via `/contents`.

### Server prompt

`exa-server-prompt` — teaches the model which tool to reach for and how to use
each (prefer `exa_search → exa_fetch` over `exa_agent` for speed/cost).

## Usage examples

1. "What's the latest on AI models this year?" → `exa_search` with
   `category=news` and `startPublishedDate`/`endPublishedDate` bounding the
   window (recency is a filter, not a word in the query).
2. "Read this page for me" → `exa_fetch` (or `exa-content:///{url}` resource).
3. "What's SpaceX's valuation?" → `exa_answer` for a fast, sourced bottom line.
4. "Give me the top 30 companies in the space economy" → `exa_search` with
   `category=company` and `numResults=30` (structured entity data; no date
   filters).
5. "Research the state of solid-state batteries and cite sources" →
   `exa_agent` with an `outputSchema`.

## Development

```bash
cd mcp/exa
bun install          # install deps (bun build is wired to the prepare hook)
bun run typecheck    # tsc --noEmit
bun run build        # bun build index.ts --target=node --outdir build
bun smoke.mjs        # E2E: connect + list tools/resources + live exa_search
bun probe.mjs        # deeper: date-range search, category, contents extras,
                     # agent (top-level body), and enum/validation guards
```

> `bun` auto-loads `.env`; no `dotenv` dependency. Build with `bun build` (not
> tsc/esbuild) per the project's Bun-first rule.

## Notes

- Rate limiting: the client serializes all API calls and throttles toward a
  conservative 2000 req/hour budget; each tool response carries `apiMetrics`
  (requests in last hour, remaining, average latency, queue length).
- Errors are returned as JSON `{ error, category }` with `metadata.error: true`
  and `isError: true` (MCP-spec tool error) plus a category (`auth`, `plan`,
  `not_found`, `rate_limit`, `bad_request`, `server`, `http`) so callers can
  distinguish a bad key from a plan-gated endpoint.
- Client-side validation: enum-like args (`searchType`, `effort`,
  `textVerbosity`) and range args (`numResults`, `maxCharacters`, `links`,
  `subpages`) are checked before the wire call, so a bad value returns a precise
  `bad_request` error instead of a raw API 400. The server also rejects a
  `company`/`people` search that carries a date filter or `excludeDomains`
  (the API 400s for that).
- No credentials are ever printed.

## License

MIT — see the repository `LICENSE`.
