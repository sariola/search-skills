// E2E smoke test: spawn the built Brave Search MCP server over stdio, then
// drive a real MCP session (initialize, tools/list, resources, live tool calls).
// Run:  bun smoke.mjs   (BRAVE_SEARCH_API_KEY / BRAVE_API_KEY in env or .env)
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const t0 = Date.now();
// Resolve the built server relative to this script, not CWD, so the
// documented `node mcp/brave/smoke.mjs` (run from the repo root) works too.
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

// Assert the full retrieval surface is present (11 tools: 7 core incl. the two
// local-POI detail endpoints + llm_context + suggest + spellcheck).
const expected = [
  "brave_web_search", "brave_news_search", "brave_video_search",
  "brave_image_search", "brave_place_search", "brave_pois",
  "brave_poi_descriptions", "brave_rich_search",
  "brave_llm_context", "brave_suggest", "brave_spellcheck",
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

// 4. a live tool call: web search
const web = await client.callTool({
  name: "brave_web_search",
  arguments: { query: "clojure web framework", count: 3 },
});
console.log("\n--- brave_web_search (first 500 chars) ---");
console.log((web.content?.[0]?.text ?? "").slice(0, 500));

// 5. a live tool call: rich (weather)
const rich = await client.callTool({
  name: "brave_rich_search",
  arguments: { query: "weather in san francisco" },
});
console.log("\n--- brave_rich_search (first 300 chars) ---");
console.log((rich.content?.[0]?.text ?? "").slice(0, 300));

// 6. a live tool call: news with the GNews block (gnews=true)
const gnews = await client.callTool({
  name: "brave_news_search",
  arguments: { query: "artificial intelligence", count: 3, gnews: true },
});
console.log("\n--- brave_news_search(gnews=true) (first 300 chars) ---");
console.log((gnews.content?.[0]?.text ?? "").slice(0, 300));

// 7. LLM Context: plan-gated on most keys. A "plan"-category error is the
// expected, correct outcome (the request reached /llm/context correctly).
const llm = await client.callTool({
  name: "brave_llm_context",
  arguments: { query: "capital of France" },
});
const llmCat = llm.content?.[0]?.metadata?.category;
const llmOk = !llm.isError || llmCat === "plan";
console.log(`\nbrave_llm_context -> ${llm.isError ? `category=${llmCat} (${llmCat === "plan" ? "plan-gated, expected" : "unexpected error"})` : "live"}`);
if (!llmOk) {
  console.error("\nLLM context returned an unexpected (non-plan) error.");
  process.exit(1);
}

// 8. resource read (about — no network)
const about = await client.readResource({ uri: "brave-about:" });
console.log("\nabout read ok, bytes:", about.contents?.[0]?.text?.length ?? 0);

await client.close();
console.log(`\nsmoke OK in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
