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
   - `numResults` (1–100, default 10), `searchType` (open string). Recommended:
     `lite` (cached, $4/1k up to 10 results plus $1/1k above that; rejects
     freshness, summary, and synthesis), `auto` (default, $7/1k), `deep`.
     Also `deep-lite`. Legacy `instant` / `fast` (use `lite`) and
     `deep-reasoning` (use `deep`) still send. `category` (`company` /
     `publication` / `news` / `personal site` / `financial report` / `people`;
     other strings act as hints).
   - Domains: `includeDomains`, `excludeDomains` (use these instead of `site:`
     in the query).
   - `objective` (max 4096), `userIdentifier` (1–64 `[A-Za-z0-9_-]`, no PII),
     `moderation`, `compliance` (`hipaa`), `systemPrompt`, `additionalQueries`
     (1–10, deep types only).
   - Per-result content: `withHighlights` (+ `highlightsQuery`,
     `highlightsMaxCharacters` 1–1000000, `highlightsDynamic`,
     `highlightsVerbosity` `low`/`medium`/`high`), `withText`
     (+ `textMaxCharacters` 1–1000000, `textVerbosity` `compact`/`standard`/
     `full`), `withSummary` (+ `summaryQuery`), `links` / `imageLinks` (0–1000),
     `maxAgeHours` (-1–720), `snapshotAsOf` (ISO date or date-time).
   - `userLocation`: a 2-letter ISO country code, or
     `{country?, latitude, longitude}`. `outputSchema` adds a synthesized,
     citation-grounded answer (rejected by `lite`).
   - `company` and `people` reject published-date filters. `people` also
     rejects `excludeDomains`. `company` may use `excludeDomains`. The server
     rejects those combinations client-side.
   - Response `notices` are included in the tool text and the result bag.
     `warnings` are logged and returned in the bag; do not feed them to a model.

2. **`exa_fetch`** — deep-read URLs / document ids (`POST /contents`)
   - `urls` or `ids` (one required), `withText` (+ `textMaxCharacters` 1–1000000,
     `textVerbosity`), `withHighlights` (+ `highlightsQuery`,
     `highlightsMaxCharacters`, `highlightsDynamic`, `highlightsVerbosity`),
     `withSummary` (+ `summaryQuery`), `links` / `imageLinks` (0–1000),
     `subpages` (0–100), `subpageTarget`, `maxAgeHours` (-1–720; 0 = fresh,
     -1 = cache), `snapshotAsOf`, `userIdentifier`.

3. **`exa_answer`** — citation-grounded answer (`POST /answer`)
   - Required: `query`; optional `text`, `outputSchema`, `systemPrompt`,
     `userLocation` (country code or `{country?, latitude, longitude}`),
     `userIdentifier`.

4. **`exa_agent`** — full agentic research run (`POST /agent/runs`, polled)
   - Required: `query` (top-level, per `CreateAgentRunRequest`); optional
     `effort` (`minimal`/`low`/`medium`/`high`/`xhigh`/`ultra`/`max`/`auto`),
     `userIdentifier`,
     `systemPrompt`, `outputSchema`, `maxCostDollars` (1–100, default $5 cap on
     `auto`), `timeout` (default 120s). Requires a plan that enables
     `/agent/runs`.

5. **`exa_find_similar`** — more-like-this (`POST /findSimilar`)
   - Required: `url`; optional `numResults` (1–100), `category`,
     `excludeSourceDomain`, `includeDomains`, `excludeDomains`,
     `startPublishedDate` / `endPublishedDate`, `withText`, `withHighlights`
     (`highlightsDynamic`, `highlightsVerbosity`), `snapshotAsOf`, `maxAgeHours`.

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
6. "Autocomplete a product query cheaply" → `exa_search` with `searchType=lite`,
   `highlightsDynamic=true`, and a `userLocation`.

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
  (requests in last hour, remaining, average latency, queue length) on
  `structuredContent` and `content[0]._meta`.
- Errors are returned as JSON `{ error, category }` with `isError: true`.
  The same bag (`error`, `category`, `apiMetrics`) is on `structuredContent`
  and `content[0]._meta` (`auth`, `plan`, `not_found`, `rate_limit`,
  `bad_request`, `server`, `http`) so callers can
  distinguish a bad key from a plan-gated endpoint.
- Client-side validation: `effort`, `textVerbosity`, and `highlightsVerbosity`,
  plus range args (`numResults`, character caps 1–1000000, `links`, `subpages`,
  `maxAgeHours`) are checked before the wire call. `searchType` is an open
  string; `lite` plus a parameter outside its allowlist is a client-side
  `bad_request`. `company` and `people` reject date filters; `people` also
  rejects `excludeDomains`. Dynamic highlights send
  `Exa-Beta: dynamic-highlights-2026-08-28`.
- No credentials are ever printed.

## License

MIT — see the repository `LICENSE`.
