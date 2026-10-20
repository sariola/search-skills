// Strict JSON-RPC framing check: spawn the built server, drive a few requests,
// and assert that EVERY line on stdout parses as JSON. The MCP SDK silently
// drops bad lines, so this catches stdout corruption that naive E2E misses.
//
// One child process per protocol opening. initialize pins the connection, so
// 2024-11-05, 2025-11-25, and 2026-07-28 cannot share a process.
//
// Usage (from anywhere; the server path resolves relative to this script):
//   EXA_API_KEY=...     node mcp/strict_framing.mjs exa     EXA_API_KEY
//   BRAVE_..._KEY=...   node mcp/strict_framing.mjs brave   BRAVE_SEARCH_API_KEY
//
// The key is passed as argv so the caller's shell (not this script) is what
// holds the credential; the script never prints it.
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

const [, , dir, keyName] = process.argv;
if (!dir || !keyName) {
  console.error("usage: node mcp/strict_framing.mjs <exa|brave> <ENV_KEY_NAME>");
  process.exit(2);
}
const env = { ...process.env, [keyName]: process.env[keyName] };
if (!env[keyName]) {
  console.error(`framing: ${keyName} is not set in the environment`);
  process.exit(2);
}
const serverDir = fileURLToPath(new URL(`${dir}/`, import.meta.url));
const server = fileURLToPath(new URL(`${dir}/build/index.js`, import.meta.url));

const MODERN = "2026-07-28";

function modernParams(extra = {}) {
  return {
    ...extra,
    _meta: {
      "io.modelcontextprotocol/protocolVersion": MODERN,
      "io.modelcontextprotocol/clientCapabilities": {},
    },
  };
}

function toolCall() {
  if (dir === "exa") {
    return { name: "exa_search", arguments: { query: "x", startPublishedDate: "2024-01-01T00:00:00Z" } };
  }
  return { name: "brave_web_search", arguments: { query: "x", extra: true, count: 2 } };
}

// Each case opens its own server. `steps` are [delayMs, (rpc) => void].
// `check` sees the parsed stdout messages for that child.
const cases = [
  {
    name: "2024-11-05",
    steps: [
      [500, (rpc) => rpc(1, "initialize", {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "f", version: "0" },
      })],
      [200, (rpc) => rpc.notify("notifications/initialized")],
      [600, (rpc) => rpc(2, "tools/list", {})],
      [400, (rpc) => rpc(3, "resources/list", {})],
      [400, (rpc) => rpc(4, "tools/call", toolCall())],
    ],
    check(messages) {
      const init = messages.find((m) => m.id === 1);
      const version = init?.result?.protocolVersion;
      if (version !== "2024-11-05") {
        return `initialize echoed ${version ?? "nothing"}, want 2024-11-05`;
      }
      return assertToolBag(messages);
    },
  },
  {
    name: "2025-11-25",
    steps: [
      [500, (rpc) => rpc(1, "initialize", {
        protocolVersion: "2025-11-25",
        capabilities: {},
        clientInfo: { name: "f", version: "0" },
      })],
      [200, (rpc) => rpc.notify("notifications/initialized")],
      [600, (rpc) => rpc(2, "tools/list", {})],
      [400, (rpc) => rpc(3, "prompts/list", {})],
      [400, (rpc) => rpc(4, "tools/call", toolCall())],
    ],
    check(messages) {
      const init = messages.find((m) => m.id === 1);
      const version = init?.result?.protocolVersion;
      if (version !== "2025-11-25") {
        return `initialize echoed ${version ?? "nothing"}, want 2025-11-25`;
      }
      const prompts = messages.find((m) => m.id === 3);
      const listed = prompts?.result?.prompts?.[0];
      if (!listed?.name || "instructions" in (listed ?? {})) {
        return "prompts/list entry must be name/description only";
      }
      return assertToolBag(messages);
    },
  },
  {
    name: "2026-07-28",
    steps: [
      [500, (rpc) => rpc(1, "server/discover", modernParams())],
      [800, (rpc) => rpc(2, "tools/list", modernParams())],
      [400, (rpc) => rpc(3, "prompts/get", modernParams({ name: dir === "exa" ? "exa-server-prompt" : "brave-server-prompt" }))],
      [400, (rpc) => rpc(4, "tools/call", modernParams(toolCall()))],
    ],
    check(messages) {
      const discover = messages.find((m) => m.id === 1);
      const versions = discover?.result?.supportedVersions;
      if (!Array.isArray(versions) || !versions.includes(MODERN)) {
        return `server/discover supportedVersions missing ${MODERN}`;
      }
      const tools = messages.find((m) => m.id === 2);
      if (!Array.isArray(tools?.result?.tools) || tools.result.tools.length === 0) {
        return "tools/list after server/discover returned no tools";
      }
      const prompt = messages.find((m) => m.id === 3);
      const text = prompt?.result?.messages?.[0]?.content?.text;
      if (typeof text !== "string" || text.length < 20) {
        return "prompts/get did not return a user message";
      }
      return assertToolBag(messages);
    },
  },
];

function assertToolBag(messages) {
  const call = messages.find((m) => m.id === 4);
  const result = call?.result;
  if (!result) return `tools/call has no result (${JSON.stringify(call?.error ?? call)?.slice(0, 180)})`;
  const block = result.content?.[0];
  if (block && "metadata" in block) return "tools/call still has content[0].metadata";
  if (!result.structuredContent || typeof result.structuredContent !== "object") {
    return "tools/call missing structuredContent";
  }
  if (!block?._meta || typeof block._meta !== "object") return "tools/call missing content[0]._meta";
  if (JSON.stringify(result.structuredContent) !== JSON.stringify(block._meta)) {
    return "structuredContent and content[0]._meta differ";
  }
  return null;
}

function runCase(spec) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [server], {
      cwd: serverDir, env, stdio: ["pipe", "pipe", "pipe"],
    });
    let bad = 0;
    let total = 0;
    const messages = [];
    const rl = createInterface({ input: child.stdout });
    rl.on("line", (line) => {
      total++;
      try {
        messages.push(JSON.parse(line));
      } catch {
        bad++;
        if (bad <= 5) console.error("BAD STDOUT LINE:", line.slice(0, 200));
      }
    });
    child.stderr.on("data", () => {});
    const rpc = (id, method, params = {}) => {
      child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    };
    rpc.notify = (method, params = {}) => {
      child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
    };
    let elapsed = 0;
    for (const [delay, send] of spec.steps) {
      elapsed += delay;
      setTimeout(() => send(rpc), elapsed);
    }
    const tail = 4000;
    setTimeout(() => {
      child.kill();
      const problem = bad === 0 ? spec.check(messages) : `${bad} non-JSON stdout line(s)`;
      resolve({ name: spec.name, total, bad, problem });
    }, elapsed + tail);
  });
}

if (dir === "exa") {
  for (const spec of cases) {
    const prior = spec.check;
    const params = { name: "exa_search", arguments: { query: "x", searchType: "lite", withSummary: true } };
    spec.steps.push([300, (rpc) => rpc(5, "tools/call", spec.name === "2026-07-28" ? modernParams(params) : params)]);
    spec.check = (messages) => {
      const base = prior(messages);
      if (base) return base;
      const call = messages.find((m) => m.id === 5);
      const result = call?.result;
      if (!result?.isError) return "lite+summary did not return isError";
      if (result.structuredContent?.category !== "bad_request") {
        return `lite+summary category ${result.structuredContent?.category ?? "missing"}`;
      }
      const block = result.content?.[0];
      if (JSON.stringify(result.structuredContent) !== JSON.stringify(block?._meta)) {
        return "lite+summary structuredContent and content[0]._meta differ";
      }
      return null;
    };
  }
}

let failed = 0;
for (const spec of cases) {
  const result = await runCase(spec);
  const ok = result.bad === 0 && !result.problem;
  if (!ok) failed++;
  const detail = result.problem ? ` ${result.problem}` : "";
  console.error(
    `${dir} ${result.name}: stdout lines=${result.total} bad=${result.bad}${detail} ${ok ? "FRAMING OK" : "FRAMING CORRUPTED"}`,
  );
}
process.exit(failed === 0 ? 0 : 1);
