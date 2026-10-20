// E2E smoke test: spawn the built Exa MCP server over stdio, then drive a real
// MCP session (initialize, tools/list, resources, a live tool call).
// Run: EXA_API_KEY=... bun run build/index.js  (as a child) — or just `bun smoke.mjs`
import { Client } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";

const t0 = Date.now();
// Resolve the built server relative to this script, not CWD, so the
// documented `node mcp/exa/smoke.mjs` (run from the repo root) works too.
const here = new URL(".", import.meta.url).pathname;
const serverScript = new URL("build/index.js", import.meta.url).pathname;
const transport = new StdioClientTransport({
  command: process.execPath,
  args: [serverScript],
  env: { ...process.env },
  cwd: here,
});

const client = new Client({ name: "smoke", version: "0.0.1" }, { capabilities: {} });
await client.connect(transport);
console.log("connected");

// 1. tools
const tools = await client.listTools();
console.log("tools:", tools.tools.map((t) => t.name));

// Assert the full research surface is present (66 tools): 8 core + 6 agent-mgmt
// + 5 batch + 9 search-monitors + 7 webset-monitors + 14 webset + 4 enrichment
// + 5 import + 6 webhook + 2 event.
const expected = [
  "exa_search", "exa_search_lite", "exa_search_near", "exa_fetch", "exa_fetch_snapshot",
  "exa_answer", "exa_agent", "exa_find_similar",
  "exa_agent_get", "exa_agent_list", "exa_agent_cancel", "exa_agent_stop",
  "exa_agent_events", "exa_agent_delete",
  "exa_batch_create", "exa_batch_get", "exa_batch_list", "exa_batch_cancel",
  "exa_batch_delete",
  "exa_monitor_create", "exa_monitor_list", "exa_monitor_get", "exa_monitor_update",
  "exa_monitor_delete", "exa_monitor_trigger", "exa_monitor_runs",
  "exa_monitor_run_get", "exa_monitor_batch",
  "exa_webset_monitor_create", "exa_webset_monitor_list", "exa_webset_monitor_get",
  "exa_webset_monitor_update", "exa_webset_monitor_delete", "exa_webset_monitor_runs",
  "exa_webset_monitor_run_get",
  "exa_webset_create", "exa_webset_preview", "exa_webset_get", "exa_webset_list",
  "exa_webset_items", "exa_webset_item_get", "exa_webset_item_delete",
  "exa_webset_add_search", "exa_webset_search_status", "exa_webset_search_cancel",
  "exa_webset_enrich", "exa_enrichment_get", "exa_enrichment_cancel",
  "exa_enrichment_update", "exa_enrichment_delete",
  "exa_webset_update", "exa_webset_cancel", "exa_webset_delete",
  "exa_import_create", "exa_import_list", "exa_import_get", "exa_import_update",
  "exa_import_delete",
  "exa_webhook_create", "exa_webhook_list", "exa_webhook_get", "exa_webhook_update",
  "exa_webhook_delete", "exa_webhook_attempts",
  "exa_event_list", "exa_event_get",
];
const got = new Set(tools.tools.map((t) => t.name));
const missing = expected.filter((n) => !got.has(n));
if (missing.length) {
  console.error(`\nMISSING tools: ${missing.join(", ")}`);
  process.exit(1);
}
// The surface is pinned to exactly this set: a new tool that isn't added to
// `expected` would otherwise pass silently. Fail on both drift directions.
if (tools.tools.length !== expected.length) {
  console.error(`\nTool count drift: server exposes ${tools.tools.length}, expected ${expected.length}. Update this list.`);
  process.exit(1);
}
console.log(`tool surface OK (${tools.tools.length} tools; ${expected.length} expected present)`);

const searchTool = tools.tools.find((t) => t.name === "exa_search");
const searchTypeDesc = searchTool?.inputSchema?.properties?.searchType?.description ?? "";
const highlightProps = searchTool?.inputSchema?.properties ?? {};
if (!searchTypeDesc.includes("lite") || !highlightProps.highlightsDynamic || !highlightProps.snapshotAsOf || !highlightProps.objective) {
  console.error("\nexa_search schema is missing an Exa 3.0 field (lite, highlightsDynamic, snapshotAsOf, or objective)");
  process.exit(1);
}
console.log("exa_search Exa 3.0 schema OK");

const byName = Object.fromEntries(tools.tools.map((t) => [t.name, t]));
const liteProps = Object.keys(byName.exa_search_lite?.inputSchema?.properties ?? {});
const snapRequired = byName.exa_fetch_snapshot?.inputSchema?.required ?? [];
const nearRequired = byName.exa_search_near?.inputSchema?.required ?? [];
if (liteProps.includes("withSummary") || liteProps.includes("maxAgeHours") || liteProps.includes("searchType")) {
  console.error("\nexa_search_lite exposes a non-lite parameter");
  process.exit(1);
}
if (!snapRequired.includes("snapshotAsOf") || !nearRequired.includes("userLocation")) {
  console.error("\nconvenience tools are missing a required field");
  process.exit(1);
}
if (!byName.exa_agent?.inputSchema?.properties?.maxDurationSeconds || !highlightProps.requestTimeout) {
  console.error("\nexa_agent.maxDurationSeconds or exa_search.requestTimeout is missing");
  process.exit(1);
}
console.log("convenience + budget + requestTimeout schema OK");

// 2. resources
const res = await client.listResources();
console.log("resources:", res.resources.map((r) => r.uri));

// 3. resource templates
const rts = await client.listResourceTemplates();
console.log("templates:", rts.resourceTemplates.map((t) => t.uriTemplate));

// 4. a live tool call
const out = await client.callTool({
  name: "exa_search",
  arguments: { query: "exa search api", numResults: 3, searchType: "lite", withHighlights: true },
});
const text = out.content?.[0]?.text ?? "";
console.log("\n--- exa_search result (first 400 chars) ---");
console.log(text.slice(0, 400));

// 5. resource read (team)
const team = await client.readResource({ uri: "exa-team:" });
console.log("\nteam read ok, bytes:", team.contents?.[0]?.text?.length ?? 0);

await client.close();
console.log(`\nsmoke OK in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
