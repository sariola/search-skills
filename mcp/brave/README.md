# Brave Search MCP Server

A [Model Context Protocol](https://github.com/modelcontextprotocol) server for
the [Brave Search](https://api-dashboard.search.brave.com/app/documentation) REST
API (`api.search.brave.com`).

Built to the same quality bar as the Linear MCP reference and the sibling Exa
server: a serialized **RateLimiter** with per-request metrics, a typed REST
client with classified errors, JSON-Schema tool contracts, addressable resources,
a server prompt, and `apiMetrics` attached to every tool response. Runs on Bun
(or Node) over stdio.

## Installation

1. Create a Brave Search API key: <https://api-dashboard.search.brave.com/app/dashboard>

2. Configure your MCP client. The server reads `BRAVE_SEARCH_API_KEY`
   (or `BRAVE_API_KEY`) from the environment (or a `.env` file next to the server).

   Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

   ```json
   {
     "mcpServers": {
       "brave-search": {
         "command": "bun",
         "args": ["/abs/path/to/search-skills/mcp/brave/build/index.js"],
         "env": { "BRAVE_SEARCH_API_KEY": "your_brave_api_key" }
       }
     }
   }
   ```

   Claude Code:

   ```bash
   claude mcp add brave-search --env BRAVE_SEARCH_API_KEY=your_brave_api_key \
     -- bun /abs/path/to/search-skills/mcp/brave/build/index.js
   ```

## Components

### Tools

Each tool's `inputSchema` documents inline defaults and allowed values so a
model can make the right granular choice.

1. **`brave_web_search`** — web search (`GET /res/v1/web/search`)
   - Required: `query`
   - Recency: `freshness` (`pd` 24h / `pw` 7d / `pm` 30d / `py` 365d, or an
     inclusive date range `YYYY-MM-DDtoYYYY-MM-DD`) — the biggest lever for
     "latest/recent" queries.
   - `count` (1–20, default 5), `country`, `searchLang`,
     `safeSearch` (off/moderate/strict, default moderate), `resultFilter`
     (comma-separated sections to keep, e.g. `web,discussions` or `web,faq`).
   - `extra` (true = up to 5 extra alternative snippets per result),
     `spellcheck` (force/disable), `offset` (0–9 page window).
   - Returns `web.results` plus Brave's `mixed` ordering, embedded videos, and
     the knowledge panel/infobox.

2. **`brave_news_search`** — news (`GET /res/v1/news/search`)
   - `query`, `count` (1–20), `freshness`, `country`, `searchLang`,
     `safeSearch`, `extra` (extra snippets per article).

3. **`brave_video_search`** — videos (`GET /res/v1/videos/search`)
   - `query`, `count` (1–20), `country`, `searchLang`, `safeSearch`.
   - Duration and creator come from each result's nested `video` object.

4. **`brave_image_search`** — images (`GET /res/v1/images/search`)
   - `query`, `count` (1–200, default 10), `country`, `searchLang`,
     `safesearch` (off/strict, default strict), `spellcheck`,
     `unit` (px/em), `property` (any/commercial/non-commercial licensing),
     `searchType` (all/transparent). Not paginated — raise `count` for more.

5. **`brave_place_search`** — places / POIs (`GET /res/v1/local/place_search`)
   - `query` (omit for explore mode) and either `latitude`+`longitude` **or**
     `location`. `count` (1–100, default 10), `radius` (meters; a soft bias
     toward the anchor, not a hard cutoff), `country`, `searchLang`,
     `units` (metric/imperial), `safeSearch`, `spellcheck`, `category`
     (e.g. cafe/hotel). Returns businesses/landmarks with address, coordinates,
     opening hours, phone, rating, and the resolved location.

6. **`brave_rich_search`** — rich data (two-step: `GET /res/v1/web/search` with
   `enable_rich_callback=1`, then `GET /res/v1/web/rich?callback_key=…`)
   - `query`. Structured answers for weather, stocks, crypto, currency,
     calculator, definitions, unit conversion, unix timestamp, sports.

### Resources

- `brave-about:` — static server info (base URL, auth header, endpoint catalog,
  rate-limit notes). No network call.
- `brave-status:` — live key/plan health check (one minimal web search).

### Server prompt

`brave-server-prompt` — teaches the model which tool to reach for (prefer
`brave_rich_search` for weather/stock/FX/unit/definition/timestamp; use
`brave_news_search` with `freshness` for "recent"; `brave_place_search` for local
businesses).

## Usage examples

1. "What are the top Clojure web frameworks?" → `brave_web_search` (add
   `extra: true` for up to 5 alternative snippets per result when you need more
   context per page).
2. "Latest AI news this week" → `brave_news_search` with `freshness=pw`.
3. "Coffee shops near 37.77, -122.41" → `brave_place_search` with the
   coordinates and a `radius` (e.g. 2000 m).
4. "Find commercial, transparent logos of a leaf" → `brave_image_search` with
   `property=commercial`, `searchType=transparent`.
5. "What's the weather in Tokyo / what's AAPL worth / how many miles in 100km?"
   → `brave_rich_search`.

## Development

```bash
cd mcp/brave
bun install
bun run typecheck    # tsc --noEmit
bun run build        # bun build index.ts --target=node --outdir build
bun smoke.mjs        # E2E: connect + list tools/resources + live web + rich
bun probe.mjs        # deeper: web extra snippets, freshness, image filters,
                     # place radius, nested video fields, validation guards
```

> `bun` auto-loads `.env`; no `dotenv` dependency. Build with `bun build` (not
> tsc/esbuild) per the project's Bun-first rule.

## Notes

- Rate limiting: the client serializes all API calls and throttles toward a
  conservative hourly budget (Brave's free plan is capped by the *month*); each
  tool response carries `apiMetrics`.
- Auth errors: a 401 or an `error.code = SUBSCRIPTION_TOKEN_INVALID` body is
  surfaced as `category: "auth"`; a 422 validation error as `"bad_request"`;
  429 as `"rate_limit"`.
- Client-side validation: enum-like args (`freshness`, `safeSearch`, image
  `property`/`searchType`/`unit`, place `units`) and range args (`count`,
  `offset`) are checked before the wire call, so a bad value returns a
  precise `bad_request` error instead of a raw API 400/422. Tool errors set
  `isError: true` per the MCP spec.
- No credentials are ever printed.

## License

MIT — see the repository `LICENSE`.
