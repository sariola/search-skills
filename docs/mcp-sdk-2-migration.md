# MCP SDK 2.x migration

Status: implemented on `@modelcontextprotocol/server` 2.3.1 and `@modelcontextprotocol/client` 2.3.1. Both servers use the low-level `Server`, method-string handlers, and `serveStdio` with the default `legacy: 'serve'`.

The sections below are the design this change followed. Deviations from that design are listed immediately after the recommendation. Live smoke and probe were not run in the environment that landed the change (no API keys).

Sources used for the claims below:

- [Upgrading from v1 to v2](https://ts.sdk.modelcontextprotocol.io/v2/migration/upgrade-to-v2)
- [Supporting protocol revision 2026-07-28](https://ts.sdk.modelcontextprotocol.io/v2/migration/support-2026-07-28)
- [Protocol versions](https://ts.sdk.modelcontextprotocol.io/v2/protocol-versions)
- [Support legacy clients](https://ts.sdk.modelcontextprotocol.io/v2/serving/legacy-clients.html) and [`serveStdio`](https://ts.sdk.modelcontextprotocol.io/v2/api/%40modelcontextprotocol/server/server/serveStdio.html)
- [SDK beta announcement](https://blog.modelcontextprotocol.io/posts/sdk-betas-2026-07-28/) and the [`@modelcontextprotocol/server@2.0.0` release](https://github.com/modelcontextprotocol/typescript-sdk/releases/tag/@modelcontextprotocol/server@2.0.0)
- [preloadSchemas PR](https://github.com/modelcontextprotocol/typescript-sdk/pull/2483)

## Recommendation

v2 does not make these search servers faster. The time is the Exa and Brave HTTP calls and the in-process rate limiter, which are our code. The v2 work that matters here is wire compatibility: SDK 0.7.0 only advertises `2024-11-05` and `2024-10-07`. v2's legacy era still accepts that range and also `2025-03-26` and `2025-11-25`, and `serveStdio` serves `2026-07-28` on the same factory. Stopping at SDK 1.32.1 would not have avoided the real break (tool-result `metadata` is stripped) and never grows the protocol past `2025-11-25`.

This change targets v2 2.3.1, keeps `serveStdio`'s default `legacy: 'serve'`, and moves the per-result bag in the same change.

## What landed, and where it differs from the plan

Done in both servers:

- Imports are `Server` and `Tool` from `@modelcontextprotocol/server`, and `serveStdio` from `@modelcontextprotocol/server/stdio`. `@modelcontextprotocol/sdk` is gone.
- Handlers are method strings: `resources/read`, `resources/list`, `resources/templates/list`, `tools/list`, `prompts/list`, `prompts/get`, `tools/call`.
- Capabilities are `{ prompts: {}, resources: {}, tools: {} }`.
- Resource template listings dropped `parameters` and `examples`. Descriptions still say what the URI means.
- `prompts/list` returns `{ name, description }` only. `prompts/get` returns `{ description, messages: [{ role: "user", content: { type: "text", text: instructions } }] }`. The instruction string still lives on the in-process prompt object; it is not a list field.
- Every tool return, including the error return, goes through one `toolResult` helper per file (64 Exa sites, 12 Brave sites). The bag is `structuredContent` and `content[0]._meta`. `content[].metadata` is gone. Exa API request-body `metadata` arguments are unchanged.
- Smoke and probe import `Client` from `@modelcontextprotocol/client` and `StdioClientTransport` from `@modelcontextprotocol/client/stdio`. They read `structuredContent`, falling back to `content[0]._meta`.
- `mcp/strict_framing.mjs` spawns one child per opening: `initialize` `2024-11-05`, `initialize` `2025-11-25` (must be echoed), and `server/discover` for `2026-07-28` followed by `tools/list` and `prompts/get`. A dummy-key `tools/call` must come back with matching `structuredContent` and `content[0]._meta` and no `metadata`.
- Python packages were not edited for this migration.

Deviations:

- The missing-API-key check still runs before `serveStdio` and `process.exit(1)`. The Nix check runs `node build/index.js` with no key and requires that exit. Putting the check only inside the factory would leave the process on stdio. `ExaClient` / `BraveClient` are still constructed inside the factory, once per connection.
- `toolResult` omits `isError` when the call succeeded. The sketch used `isError: isError || undefined`, which leaves an own property whose value is `undefined`. The wire result is the same: success frames have no `isError`.
- Low-level handlers do not call `server.projectCallToolResult`. That projection wraps non-object `structuredContent` on the 2025 era and appends a text fallback. Our bag is always a plain object and every result already has a text block, and no tool advertises `outputSchema`.
- `ResourceTemplate` exported by `@modelcontextprotocol/server` is the `registerResource` class (`uriTemplate` is a `UriTemplate`, and the class has no `name`). Wire listings are plain `{ uriTemplate, name, description }` objects, so the arrays are unannotated.
- `main` is synchronous. `serveStdio` returns a handle, not a promise, so the old `main().catch` is a `try/catch` around `main()`.
- `outputSchema` was not added. Zod is not imported by application code.
- Live `smoke.mjs` / `probe.mjs` were not run here. Framing with a dummy key proves the envelope and that a failing tool call still returns JSON, not that a real search result is shaped correctly.

## What 2.x actually changes

Honest split between things these stdio servers would use and things they would not.

### Not a performance win for this repo

- Schema construction is lazy, and `preloadSchemas()` warms it. That matters on Cloudflare Workers, where the first request on a fresh isolate is billed CPU ([PR 2483](https://github.com/modelcontextprotocol/typescript-sdk/pull/2483)). Node and browser builds stay lazy on purpose. A long-lived stdio process pays the cost once at startup. It is noise next to a search request.
- There is no published number showing faster `tools/call` dispatch, less memory per call, or higher throughput. Nothing in the v2 notes claims that.
- The package split (`server`, `client`, `node`, `express`, `hono`) shrinks an install that only needs one side. `bun build` still bundles whatever the server imports. The Exa bundle today is about 0.30 MB of JavaScript with SDK 0.7.0. v2 pulls a larger stack (Zod 4 internally, Ajv). Expect the bundle to grow, not shrink, until someone measures it.
- Stateless HTTP, `Mcp-Method` header routing, cache hints (`ttlMs` / `cacheScope`), multi-round-trip `inputRequired`, and `subscriptions/listen` are real protocol features. These servers do not speak HTTP MCP. They do not ask the user a question mid-tool. They do not subscribe. Adopting v2 does not turn any of that on by itself.

### What we would actually gain

- One server factory can serve two eras. `serveStdio(factory)` pins the connection from the opening bytes. Default `legacy: 'serve'` keeps `initialize` clients. A `2026-07-28` opening is served by the same factory. `server.connect(new StdioServerTransport())` does **not** speak 2026; the docs are explicit that a hand-wired transport stays on the 2025-era wire.
- Legacy era is `2024-10-07` through `2025-11-25`, one wire codec. Modern era is `2026-07-28` only: no `initialize`, `server/discover` instead, a `_meta` envelope on every request.
- Clients that send `initialize` with `2025-11-25` and refuse to drop back to `2024-11-05` cannot finish negotiation with SDK 0.7.0. The 0.7 server answers with `2024-11-05` when the requested version is not in its list (`2024-11-05`, `2024-10-07`). A strict client then disconnects. v2 can answer with the 2025 version the client asked for.
- v1 security updates run for at least six months after the v2 release (2026-07-27), so into early 2027. 0.7.0 is outside that window. That is a maintenance reason, not a speed reason.
- Tool results are validated before they are written to stdout. A bad result becomes a JSON-RPC error instead of a frame the client rejects later. That is why `content[].metadata` cannot survive the upgrade unchanged.
- Standard Schema on the high-level `registerTool` API (Zod 4, Valibot, ArkType, or `fromJsonSchema`). These servers do not need that if they keep hand-written JSON Schema on `tools/list`. Zod 4 would be an internal dependency of the SDK, not something `index.ts` has to import.
- The codemod `npx @modelcontextprotocol/codemod v1-to-v2` rewrites import paths and `setRequestHandler(SomeSchema, ...)` to method strings. It will not rewrite our `metadata` bags or the non-spec prompt shape. Treat it as a first pass, then read the diff.

### Features to leave unused

Do not add HTTP transport, tasks, elicitation, sampling, or cache hints as part of the upgrade. The tool list, rate limiter, and error categories stay as they are.

## API changes that touch this code

Both servers use the low-level `Server`, `StdioServerTransport`, and schema constants from `@modelcontextprotocol/sdk/*/index.js` and `types.js`. Stay on that low-level style. Rewriting every tool onto `McpServer.registerTool` would replace the JSON Schema objects and the big `switch` with Zod (or `fromJsonSchema`) for no behavior change.

| Today (0.7.0) | v2 |
| --- | --- |
| `@modelcontextprotocol/sdk/server/index.js` `Server` | `@modelcontextprotocol/server` `Server` (still there; `McpServer` is the high-level wrapper) |
| `@modelcontextprotocol/sdk/server/stdio.js` `StdioServerTransport` | `@modelcontextprotocol/server` `StdioServerTransport`, but the process entry should be `serveStdio` from `@modelcontextprotocol/server/stdio` |
| `@modelcontextprotocol/sdk/types.js` schema constants | `@modelcontextprotocol/server` or `@modelcontextprotocol/core` (the server package re-exports core) |
| `setRequestHandler(CallToolRequestSchema, fn)` | `setRequestHandler('tools/call', fn)` |
| same for `resources/read`, `resources/list`, `resources/templates/list`, `tools/list`, `prompts/list`, `prompts/get` | method strings: `resources/read`, `resources/list`, `resources/templates/list`, `tools/list`, `prompts/list`, `prompts/get` |
| handler `(request, extra)` | handler `(request, ctx)`. Nothing here reads `extra`, so the body can keep using `request.params` |
| `Client` / `StdioClientTransport` in smoke and probe | `@modelcontextprotocol/client`. `callTool({ name, arguments })` no longer takes a result schema for spec methods. These scripts already omit it |
| Zod 3, transitive | Zod 4 inside the SDK. Application code keeps JSON Schema `inputSchema` objects |
| Node 18-era 0.7 package | v2 engines: Node 20+. This repo's flake pins Node 22 |

`serveStdio` replaces the tail of `main`:

```ts
import { serveStdio } from "@modelcontextprotocol/server/stdio";

serveStdio(() => buildServer(process.env.EXA_API_KEY!));
```

Construct `ExaClient` / `BraveClient` inside the factory. Stdio calls the factory once per connection, which matches today's one-process client. Putting the client outside the factory would be wrong if the same factory is later reused for per-request HTTP.

Spike check, before editing return sites: log one `tools/call` request in a handler and confirm `request.params.name` and `request.params.arguments` are unchanged. The codemod only rewrites the registration.

### Non-spec fields the current types allow and v2 will not

SDK 0.7 schemas are Zod passthrough, so these extra keys typecheck and go out on the wire. v2 object schemas for the spec use strip. They have to be removed or moved, not cast away.

- `capabilities.prompts.default` and `capabilities.resources.templates` / `read`. Spec capabilities are `prompts: { listChanged? }`, `resources: { subscribe?, listChanged? }`, `tools: { listChanged? }`. Advertise `{ prompts: {}, resources: {}, tools: {} }` so the handlers still pass the capability checks. Empty objects are enough; the 0.7 server only tests truthiness.
- Resource templates carry `parameters` and `examples`. The descriptions already say what the URI means. Drop the extra keys.
- `Prompt.instructions` is not a spec field. `prompts/get` today returns `{ prompt: serverPrompt }`, and `GetPromptResult` in v2 requires `messages`. Return `{ description, messages: [{ role: "user", content: { type: "text", text: instructions } }] }`. `prompts/list` returns `{ name, description }` only. The instruction text stays; the envelope changes. Hosts that followed the spec were not reading `instructions` off the list entry.

### Preserving category, raw payload, and apiMetrics

Every successful tool return looks like:

```ts
content: [{ type: "text", text, metadata: { ...meta, raw, /* sometimes costDollars, nextCursor, ... */ } }]
```

Errors:

```ts
return {
  isError: true,
  content: [{
    type: "text",
    text: JSON.stringify({ error: message, category }, null, 2),
    metadata: { error: true, category, ...meta },
  }],
};
```

`meta` is `apiMetrics`: `requestsInLastHour`, `remainingRequests`, `averageRequestTime`, `queueLength`.

`content[].metadata` is not in the spec. SDK 1.32 already validates `tools/call` results and returns the stripped object, so `metadata` disappears before stdout. v2's text content schema has `type`, `text`, `annotations`, and `_meta`, and the content object is strip. Assume `metadata` will be dropped. Confirm with a one-result unit spike in the implementation PR; do not depend on passthrough.

Put the same bag in both spec fields:

- `structuredContent` — the machine-readable tool result. This is what probes should read. It can hold `apiMetrics`, `raw`, `error`, `category`, and the tool-specific extras (`costDollars`, `nextCursor`, `websetId`, and so on).
- `content[0]._meta` — the same object, so a client that only walks content blocks still sees it. `_meta` is in the text-content schema.

Do not also keep `metadata`. Two private keys will drift. `text` and `isError` stay exactly as they are. The error `text` is already JSON `{ error, category }`, so a client that only reads text does not lose the category. `mcp/exa/README.md` currently says errors carry `metadata.error: true`. Update that sentence to `structuredContent.error` and `content[0]._meta.error` in the implementation PR. `mcp/brave/README.md` only says each response carries `apiMetrics`; name the new field there too.

Suggested helper, used by both servers (duplicated, not a third package; the repo has no shared MCP library):

```ts
function toolResult(
  text: string,
  bag: Record<string, unknown>,
  isError = false,
) {
  return {
    isError: isError || undefined,
    structuredContent: bag,
    content: [{ type: "text" as const, text, _meta: bag }],
  };
}
```

There are 63 Exa success sites and 11 Brave success sites matching `metadata: { ...meta`, plus one error return in each file. A helper is how those stay consistent. Do not hand-edit them into slightly different shapes.

`outputSchema` on each tool is optional. Adding a JSON Schema that describes the bag would help clients that validate structured output. It is not required to preserve today's probes. Skip it in the first PR unless a host demands it. The bag is not a stable public schema beyond `apiMetrics`, `raw`, `error`, and `category`.

Exa request-body `metadata` (webset tags, webhook tags, monitor tags) is the Exa API field. It is unrelated. Do not rename those arguments.

## Impact by file

### Servers

- `mcp/exa/index.ts`, `mcp/brave/index.ts`: imports, capability object, `serveStdio` factory, eight `setRequestHandler` calls each, prompt get/list, resource templates, every tool return via `toolResult`.
- No change to rate limiting, URL paths, beta headers, or error classification.

### Scripts

- `mcp/exa/smoke.mjs`, `mcp/brave/smoke.mjs`, `mcp/exa/probe.mjs`, `mcp/brave/probe.mjs`: import `Client` and `StdioClientTransport` from `@modelcontextprotocol/client`. Default `client.connect` still performs the 2025-era `initialize` and does not probe `2026-07-28`. Against `serveStdio` that negotiates legacy, which is what we want for the existing scripts.
- Probes read `out.content[0].metadata` for `category`, `error`, and `raw` (`mcp/exa/probe.mjs`, `mcp/brave/probe.mjs`). Brave smoke reads `llm.content[0].metadata.category` for the plan-gate. Point those at `out.structuredContent` (fall back to `out.content[0]._meta` during the PR if useful). `isError` checks stay.
- `mcp/strict_framing.mjs` speaks raw JSON-RPC and hardcodes `protocolVersion: "2024-11-05"`. Leave that request as the legacy regression. It keeps passing only if `legacy` stays `'serve'`. `legacy: 'reject'` makes this script fail on purpose.
- Add a second framing case, not a replacement: `initialize` with `2025-11-25` must be echoed, and a `server/discover` opening must get a modern response that is still one JSON object per stdout line. Do not print the API key.

### Python

No code change. `exa/` and `brave/` do not import the MCP SDK. Their `metadata` arguments are provider fields (websets, webhooks, monitors). The offline unittest suite does not spawn the MCP servers. Run it anyway after the TS change so a drive-by edit cannot go unnoticed.

### Docs

Update the two MCP READMEs' notes about `metadata.error` and `apiMetrics`. Do not edit `.analysis/`.

## Client compatibility

| Client opening | SDK 0.7.0 today | v2 `serveStdio` default | v2 `legacy: 'reject'` |
| --- | --- | --- | --- |
| `initialize` `2024-10-07` or `2024-11-05` | accepted | accepted (legacy era) | rejected |
| `initialize` `2025-03-26` or `2025-11-25` | server answers `2024-11-05`; a strict client disconnects | accepted and echoed | rejected |
| `server/discover` / `2026-07-28` | unknown method, or the server exits | served, modern era | served |
| v2 client default `connect()` | legacy `initialize` only | works | fails unless the client sets `versionNegotiation: { mode: 'auto' }` or `{ pin: '2026-07-28' }` |

`versionNegotiation` defaults to legacy. Smoke and probe should keep that default so they test the path Cursor and Claude Desktop still use. A modern-era test must opt in. On stdio, an `auto` client probes with a short-lived sibling process because some legacy servers exit on any pre-`initialize` request. Our server must not exit on `server/discover` once it is on `serveStdio`. SDK 0.7.0 might ignore or fail that message; that is another reason the framing script must stay on `initialize` until the server is actually on v2.

2026 connections do not run `initialize`. `getClientCapabilities()` and `getClientVersion()` are undefined there. These servers do not call them. Handlers that later need the client identity read `ctx.mcpReq.envelope`. Do not add that now.

`InMemoryTransport` is 2025-era only. In-process 2026 tests go through `createMcpHandler` fetch, which these servers do not need.

## Step-by-step plan

This is the plan the implementation followed. The "What landed" section is the record of the result.

1. Spike on a branch, Brave first (smaller file). Install `@modelcontextprotocol/server` and `@modelcontextprotocol/client`. Remove `@modelcontextprotocol/sdk`.
2. Rewrite imports and the eight handlers to method strings. Replace `server.connect(transport)` with `serveStdio`. Fix capabilities, resource templates, and `prompts/get`.
3. Confirm with one live or dummy `tools/call` that a `metadata` key is absent on the wire and that `_meta` and `structuredContent` survive validation.
4. Add `toolResult` and switch every return, including the error path. Delete `metadata` on content blocks.
5. Point probe and smoke reads at `structuredContent`. Keep `isError` and the plan-gate / `expectCategory` logic.
6. Repeat for Exa. The helper and the handler list are the same shape; the tool switch is larger.
7. Update both MCP READMEs.
8. Run the test plan below. Do not merge without live smoke and probe if keys are available. Framing with a dummy key is not a substitute for a real tool call.

Do not regenerate tool schemas. Do not change Python.

## Test plan

Offline, no keys:

- `bun run typecheck` and `bun run build` in both packages (also `nix flake check`, which already runs `tsc` and `bun build` against the vendored lockfile).
- `node mcp/strict_framing.mjs` for both servers with a dummy key: `bad=0`, and the initialize result's `protocolVersion` is `2024-11-05`.
- New case: initialize `2025-11-25` is echoed; stdout is still pure JSON.
- New case: a `server/discover` opening gets a JSON result and the process stays up for a following `tools/list`.
- `search-skills-check` / the Python unittest suite stays green. It should not change.

With keys:

- `node mcp/brave/smoke.mjs` and `node mcp/exa/smoke.mjs`.
- `node mcp/brave/probe.mjs` and `node mcp/exa/probe.mjs`. Plan-gated tools must still pass on `category === "plan"`. Negative guards must still pass on `bad_request`.
- Capture one success and one error frame from before the migration and diff `text`, `isError`, and the bag. Only the key (`metadata` to `structuredContent` / `_meta`) should move.

## Risks and rollback

- A return site missed by the helper silently drops `raw` and `category`. Mitigation: grep for `metadata:` on content blocks before merging, and run both probes.
- A host that parsed the non-spec `content[].metadata` or `prompts/get`'s `{ prompt }` shape breaks. The spec-shaped replacement is the compatible fix for SDK clients; custom callers need the README note. The error text still contains `category`.
- `legacy: 'reject'` would strand every current host and the framing script. Do not set it.
- `serveStdio` might call the factory more than once if both eras are probed on one process. Building the API client inside the factory keeps that safe. Do not cache a half-initialized `Server`.
- Bundle and cold-start size might grow. Measure `build/index.js` bytes and time-to-stdio-ready if that becomes a complaint. It is not a reason to block the compatibility fix, and it is not a reason to do the migration early.
- Rollback is a revert of the dependency bump and the `toolResult` edit. There is no on-disk state and no Python migration.

## Effort

The protocol wiring is small and the same in both packages: imports, factory, eight handlers, prompt result, capability object.

The large diff is mechanical: about 75 success returns plus two error returns, then four scripts and two README notes. A `toolResult` helper makes that one pattern instead of 75 hand edits. The Exa and Brave HTTP clients, tool JSON Schemas, and Python packages stay put.

The work is one focused change, reviewed as a behavior change of the tool-result envelope, and it is not done until live smoke and probe have run. It is not a good fit for a dependency-bump PR.

## After this change

Rollback is a revert of the dependency bump and the `toolResult` edit. There is no on-disk state and no Python migration. Do not set `legacy: 'reject'`. Re-run live smoke and probe when API keys are available before treating the tool-result bag as confirmed against the real APIs.
