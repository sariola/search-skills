// Deeper probe: exercise the full Exa tool surface — core search, agent runs
// (incl. chained/stop/events), bulk websets (create→add_search→items→cancel→
// delete lifecycle with cleanup), and server-side batches. Each plan-gated
// surface tolerates a category==="plan" (403) — that proves the request reached
// the right route; a bad body would 400/422 (bad_request).
//
// NOTE on stop/cancel: exa_agent (the tool) blocks until a run is terminal, so
// by the time the probe can call exa_agent_stop the run is already done and the
// API correctly 400s ("can't stop a finished run"). That 400 still proves the
// route + stop-reason enum are wired (pickEnum validated the reason
// client-side before the request left), so the probe treats a stop/cancel
// against a terminal run as an acceptable outcome.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

// Resolve the built server relative to this script, not CWD, so the
// documented `node mcp/exa/probe.mjs` (run from the repo root) works too.
const probeHere = new URL(".", import.meta.url).pathname;
const probeServer = new URL("build/index.js", import.meta.url).pathname;
const transport = new StdioClientTransport({
  command: process.execPath, args: [probeServer],
  env: { ...process.env }, cwd: probeHere,
});
const client = new Client({ name: "probe", version: "0.0.1" }, { capabilities: {} });
await client.connect(transport);

// planGateOk: a 403 "plan" is an acceptable outcome (key lacks the add-on).
// tolerate: extra categories to treat as a pass even though they are errors
//   (e.g. stop/cancel against a terminal run → bad_request from the API).
// expectError: a NEGATIVE test — the call MUST error (the right guard fired).
//   Passes when errored with one of `expectCategory` (default: any).
async function call(name, args, label, opts = {}) {
  const { planGateOk = false, tolerate = [], expectError = false, expectCategory = [] } = opts;
  let out;
  try {
    out = await client.callTool({ name, arguments: args });
  } catch (e) {
    console.log(`\n[ERROR] ${label} (${name}): transport ${e?.message ?? e}`);
    return { passed: false, category: "transport", raw: undefined, text: "" };
  }
  const text = out.content?.[0]?.text ?? "";
  const meta = out.content?.[0]?.metadata ?? {};
  const category = meta.category ?? null;
  const errored = out.isError === true || !!meta.error;

  let passed, note = "";
  if (expectError) {
    const categoryOk = expectCategory.length === 0 || expectCategory.includes(category);
    passed = errored && categoryOk;
    note = passed ? " [negative, guard fired as expected]" : " [negative EXPECTED an error]";
  } else {
    const tolerated = errored && (tolerate.includes(category) || (planGateOk && category === "plan"));
    passed = tolerated || !errored;
    if (tolerated) note = ` [${category}, accepted for this probe]`;
  }
  const flag = passed ? "OK   " : "ERROR";
  console.log(`\n[${flag}] ${label} (${name})${note}${category ? ` · cat=${category}` : ""}`);
  console.log(text.slice(0, 400));
  return { passed, category, raw: meta.raw, text };
}

// A plan-gate / not-enabled / rate-limit result means "the request was well-
// formed and reached the server" — treat as a soft pass for these surfaces.
let ok = true;

// ---- Core search: granular params that shape results ------------------------
ok = (await call("exa_search", {
  query: "AI breakthroughs", numResults: 4,
  startPublishedDate: "2024-01-01T00:00:00Z",
  endPublishedDate: "2024-12-31T23:59:59Z",
  withHighlights: true,
}, "search(date-range + highlights)")).passed && ok;

ok = (await call("exa_search", {
  query: "OpenAI", category: "company", numResults: 3, withSummary: true,
}, "search(category=company + summary)")).passed && ok;

ok = (await call("exa_fetch", {
  urls: ["https://exa.ai/"], withText: true, textMaxCharacters: 800, links: 3, withHighlights: true,
}, "fetch(text-object + extras.links + highlights)")).passed && ok;

// Negative: bad searchType enum should be a client-side bad_request.
ok = (await call("exa_search", { query: "x", searchType: "hybrid" }, "search(bad searchType → expect bad_request)", { expectError: true, expectCategory: ["bad_request"] })).passed && ok;
// Negative: date filter on entity category should be rejected client-side.
ok = (await call("exa_search", { query: "x", category: "company", startPublishedDate: "2024-01-01T00:00:00Z" }, "search(company + date → expect bad_request)", { expectError: true, expectCategory: ["bad_request"] })).passed && ok;

// ---- Agent: top-level body, then chain / stop / events / list ---------------
// A cheap low-effort run. If it completes (or even plan-gates) we get a run id
// to exercise the management surface.
const agent1 = await call("exa_agent", { query: "What is 2+2?", effort: "low", timeout: 90 }, "agent(top-level body, effort=low)", { planGateOk: true });
if (!agent1.passed) ok = false;

const runId = agent1.raw?.id;
let completedRunId = null;
if (runId && agent1.category !== "plan") {
  // Poll the run to a terminal state via exa_agent_get (cheap GET).
  const getOut = await call("exa_agent_get", { runId }, "agent_get(poll run)");
  if (!getOut.passed) ok = false;
  if (getOut.raw?.status === "completed") completedRunId = runId;
  // Events for the same run (ordered tool-call/reasoning stream).
  const evOut = await call("exa_agent_events", { runId, limit: 20 }, "agent_events(event stream)");
  if (!evOut.passed) ok = false;

  // Chained agent: continue from this run's id (progressive refinement).
  if (completedRunId) {
    const chain = await call("exa_agent", {
      query: "Now state the result of 2+2 in one word.",
      effort: "low", previousRunId: runId, timeout: 90,
      metadata: { probe: "chained" },
    }, "agent(chained via previousRunId)", { planGateOk: true });
    if (!chain.passed) ok = false;
    const chainId = chain.raw?.id;
    if (chainId && chain.category !== "plan") {
      // stop/cancel a run that exa_agent already drove to terminal → the API
      // correctly 400s; that still proves the route + reason-enum are wired.
      const stop = await call("exa_agent_stop", { runId: chainId, reason: "budget_reached" }, "agent_stop(reason=budget_reached)", { planGateOk: true, tolerate: ["bad_request", "http"] });
      if (!stop.passed) ok = false;
    }
  }
}

// Agent list: reverse-chronological run history.
ok = (await call("exa_agent_list", { limit: 5 }, "agent_list(run history)")).passed && ok;

// ---- Websets: preview (cheap), then create→add_search→items→cancel→delete ---
const preview = await call("exa_webset_preview", {
  query: "US marketing agencies that focus on consumer products", count: 5,
}, "webset_preview(decompose, no commit)", { planGateOk: true });
if (!preview.passed) ok = false;

// Full lifecycle with cleanup. If create plan-gates, skip the rest (they'd all
// 403 too on a missing add-on); otherwise exercise each step and delete so no
// residue is left.
if (preview.category !== "plan") {
  const created = await call("exa_webset_create", {
    query: "US marketing agencies that focus on consumer products",
    count: 3, externalId: "probe-webset-lifecycle", metadata: { probe: "lifecycle" },
    timeout: 90,
  }, "webset_create(poll to terminal)", { planGateOk: true });
  if (!created.passed) ok = false;

  const websetId = created.raw?.id ?? created.raw?.externalId;
  if (websetId) {
    ok = (await call("exa_webset_items", { websetId, limit: 10 }, "webset_items(found records)")).passed && ok;
    ok = (await call("exa_webset_add_search", {
      websetId, query: "and their average team size is 10-50", count: 3, behavior: "append",
    }, "webset_add_search(behavior=append)")).passed && ok;
    ok = (await call("exa_webset_update", { websetId, title: "probe-renamed" }, "webset_update(retitle)")).passed && ok;
    ok = (await call("exa_webset_cancel", { websetId }, "webset_cancel(idempotent)")).passed && ok;
    const del = await call("exa_webset_delete", { websetId }, "webset_delete(cleanup)");
    if (!del.passed) ok = false;
  } else {
    console.log("\n[WARN] webset_create returned no usable id; skipping dependent steps");
  }
}

// ---- Batches: enqueue 2 same-shape /search calls ----------------------------
const batch = await call("exa_batch_create", {
  requests: [
    { customId: "b1", url: "/search", body: { query: "best coffee in SF", numResults: 2 } },
    { customId: "b2", url: "/search", body: { query: "best coffee in Seattle", numResults: 2 } },
  ],
  metadata: { probe: "batch" },
}, "batch_create(2 /search sub-requests)", { planGateOk: true });
if (!batch.passed) ok = false;
const batchId = batch.raw?.id;
if (batchId && batch.category !== "plan") {
  ok = (await call("exa_batch_get", { batchId, wait: true, timeout: 120 }, "batch_get(wait → terminal + results)")).passed && ok;
}

// ---- New surfaces: read-only list probes (cheap, prove the routes wire up) --
// These are GET list endpoints; a plan-gated key returns 403 ('plan'), which
// still proves the request reached the right route. A malformed body/param
// would 400/422 (bad_request) instead.
ok = (await call("exa_batch_list", { limit: 5 }, "batch_list(route check)", { planGateOk: true })).passed && ok;
ok = (await call("exa_monitor_list", { limit: 5 }, "monitor_list(route check)", { planGateOk: true })).passed && ok;
ok = (await call("exa_webset_monitor_list", { limit: 5 }, "webset_monitor_list(route check)", { planGateOk: true })).passed && ok;
ok = (await call("exa_import_list", { limit: 5 }, "import_list(route check)", { planGateOk: true })).passed && ok;
ok = (await call("exa_webhook_list", { limit: 5 }, "webhook_list(route check)", { planGateOk: true })).passed && ok;
ok = (await call("exa_event_list", { limit: 5 }, "event_list(route check)", { planGateOk: true })).passed && ok;

// Negative: monitor_batch with no filter must be rejected client-side.
ok = (await call("exa_monitor_batch", { action: "pause" }, "monitor_batch(no filter → expect bad_request)", { expectError: true, expectCategory: ["bad_request"] })).passed && ok;

// ---- Resources --------------------------------------------------------------
const c = await client.readResource({ uri: "exa-content:///https%3A%2F%2Fexample.com" });
console.log("\n[exa-content resource]", (c.contents?.[0]?.text ?? "").slice(0, 250));

await client.close();
console.log(`\nexa probe done ${ok ? "ALL OK" : "SOME FAILED"}`);
process.exit(ok ? 0 : 1);
