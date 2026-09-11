// Deeper probe: exercise the reworked Brave tools — web extra snippets,
// freshness date-range, image filters, place radius, and the enum/validation
// guards.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

// Resolve the built server relative to this script, not CWD, so the
// documented `node mcp/brave/probe.mjs` (run from the repo root) works too.
const probeHere = new URL(".", import.meta.url).pathname;
const probeServer = new URL("build/index.js", import.meta.url).pathname;
const transport = new StdioClientTransport({
  command: process.execPath, args: [probeServer],
  env: { ...process.env }, cwd: probeHere,
});
const client = new Client({ name: "probe", version: "0.0.1" }, { capabilities: {} });
await client.connect(transport);

// planGateOk: a category==="plan" (400 OPTION_NOT_IN_PLAN) is an acceptable
// outcome for add-on-gated endpoints (llm_context, suggest, spellcheck) — it
// proves the request reached the right route with a well-formed body; a bad
// body would 422 (bad_request).
async function call(name, args, label, opts = {}) {
  const { expectError = false, planGateOk = false } = opts;
  const out = await client.callTool({ name, arguments: args });
  const text = out.content?.[0]?.text ?? "";
  const meta = out.content?.[0]?.metadata ?? {};
  const category = meta.category ?? null;
  const errored = out.isError === true || !!meta.error;
  let passed, note = "";
  if (expectError) {
    passed = errored;
    note = " [negative]";
  } else {
    const tolerated = errored && planGateOk && category === "plan";
    passed = tolerated || !errored;
    if (tolerated) note = " [plan-gated, expected]";
  }
  const flag = passed ? "OK   " : "ERROR";
  console.log(`\n[${flag}] ${label} (${name})${note}${category ? ` · cat=${category}` : ""}`);
  console.log(text.slice(0, 450));
  return passed;
}

let ok = true;
ok = (await call("brave_web_search", { query: "machine learning", count: 2, extra: true }, "web(extra snippets)")) && ok;
ok = (await call("brave_web_search", { query: "AI news", count: 2, freshness: "pw" }, "web(freshness=pw)")) && ok;
ok = (await call("brave_news_search", { query: "renewable energy", count: 2, freshness: "pm", extra: true }, "news(freshness + extra)")) && ok;
ok = (await call("brave_news_search", { query: "AI", count: 3, gnews: true }, "news(gnews=true → GNews add-on)")) && ok;
ok = (await call("brave_video_search", { query: "clojure", count: 2 }, "video(nested duration/creator)")) && ok;
ok = (await call("brave_image_search", { query: "mount fuji", count: 2, property: "commercial", searchType: "all" }, "image(property + searchType)")) && ok;
const placeOut = await (async () => {
  const out = await client.callTool({
    name: "brave_place_search",
    arguments: { query: "coffee shops", latitude: 37.77, longitude: -122.41, count: 3, radius: 2000 },
  });
  const passed = out.isError !== true && !out.content?.[0]?.metadata?.error;
  console.log(`\n[${passed ? "OK   " : "ERROR"}] place(radius) (brave_place_search)`);
  console.log((out.content?.[0]?.text ?? "").slice(0, 300));
  return { passed, ids: (out.content?.[0]?.metadata?.raw?.results ?? [])
    .map((r) => r?.id).filter((v) => typeof v === "string" && v.length > 0).slice(0, 5) };
})();
ok = placeOut.passed && ok;

// POI detail endpoints: expand the transient ids from the place search above.
// /local/pois + /local/descriptions are add-on-gated on some plans → tolerate plan.
if (placeOut.ids.length) {
  ok = (await call("brave_pois", { ids: placeOut.ids, units: "metric" }, `pois(${placeOut.ids.length} ids → deep detail)`, { planGateOk: true })) && ok;
  ok = (await call("brave_poi_descriptions", { ids: placeOut.ids }, `poi_descriptions(${placeOut.ids.length} ids → blurb)`, { planGateOk: true })) && ok;
} else {
  console.log("\n[WARN] place_search returned no usable location ids; skipping pois/poi_descriptions");
}
// Negative: >20 ids must be rejected client-side.
ok = (await call("brave_pois", { ids: Array.from({ length: 21 }, (_, i) => "id" + i) }, "pois(21 ids → expect bad_request)", { expectError: true })) && ok;

// New retrieval endpoints (add-on-gated on most plans → tolerate category=plan).
ok = (await call("brave_llm_context", { query: "What is a transformer model?", maximum_number_of_urls: 3, context_threshold_mode: "balanced" }, "llm_context(grounded chunks)", { planGateOk: true })) && ok;
ok = (await call("brave_suggest", { query: "cla" }, "suggest(query autocomplete)", { planGateOk: true })) && ok;
ok = (await call("brave_spellcheck", { query: "recieve" }, "spellcheck(spelling fix)", { planGateOk: true })) && ok;

// Negative: bad freshness should be a client-side bad_request.
ok = (await call("brave_web_search", { query: "x", freshness: "lastweek" }, "web(bad freshness → expect bad_request)", { expectError: true })) && ok;
// Negative: place_search with no anchor.
ok = (await call("brave_place_search", { query: "coffee", count: 2 }, "place(no-anchor → expect bad_request)", { expectError: true })) && ok;

// resource: status (live health check)
const st = await client.readResource({ uri: "brave-status:" });
console.log("\n[brave-status] ", st.contents?.[0]?.text?.slice(0, 200));

await client.close();
console.log(`\nbrave probe done ${ok ? "ALL OK" : "SOME FAILED"}`);
process.exit(ok ? 0 : 1);
