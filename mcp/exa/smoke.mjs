// E2E smoke test: spawn the built Exa MCP server over stdio, then drive a real
// MCP session (initialize, tools/list, resources, a live tool call).
// Run: EXA_API_KEY=... bun run build/index.js  (as a child) — or just `bun smoke.mjs`
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import {
  StdioClientTransport,
} from "@modelcontextprotocol/sdk/client/stdio.js";

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

// Assert the full research surface is present (63 tools): 5 core + 6 agent-mgmt
// + 5 batch + 9 search-monitors + 7 webset-monitors + 14 webset + 4 enrichment
// + 5 import + 6 webhook + 2 event.
const expected = [
  "exa_search", "exa_fetch", "exa_answer", "exa_agent", "exa_find_similar",
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

// 2. resources
const res = await client.listResources();
console.log("resources:", res.resources.map((r) => r.uri));

// 3. resource templates
const rts = await client.listResourceTemplates();
console.log("templates:", rts.resourceTemplates.map((t) => t.uriTemplate));

// 4. a live tool call
const out = await client.callTool({
  name: "exa_search",
  arguments: { query: "exa search api", numResults: 3 },
});
const text = out.content?.[0]?.text ?? "";
console.log("\n--- exa_search result (first 400 chars) ---");
console.log(text.slice(0, 400));

// 5. resource read (team)
const team = await client.readResource({ uri: "exa-team:" });
console.log("\nteam read ok, bytes:", team.contents?.[0]?.text?.length ?? 0);

await client.close();
console.log(`\nsmoke OK in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
