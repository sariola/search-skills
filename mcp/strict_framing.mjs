// Strict JSON-RPC framing check: spawn the built server, drive a few requests,
// and assert that EVERY line on stdout parses as JSON. The MCP SDK silently
// drops bad lines, so this catches stdout corruption that naive E2E misses.
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
// Resolve <dir>/build/index.js relative to this script's own location, and
// run the child with the server's package dir as CWD (its node_modules, .env,
// and any relative paths resolve there).
const serverDir = fileURLToPath(new URL(`${dir}/`, import.meta.url));
const server = fileURLToPath(new URL(`${dir}/build/index.js`, import.meta.url));
const child = spawn(process.execPath, [server], {
  cwd: serverDir, env, stdio: ["pipe", "pipe", "pipe"],
});
let bad = 0, total = 0;
const rl = createInterface({ input: child.stdout });
rl.on("line", (line) => {
  total++;
  let ok = false;
  try { JSON.parse(line); ok = true; } catch { ok = false; }
  if (!ok) { bad++; if (bad <= 5) console.error("BAD STDOUT LINE:", line.slice(0, 200)); }
});
function rpc(id, method, params = {}) { child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n"); }
child.stderr.on("data", () => {}); // swallow server logs

await new Promise((r) => setTimeout(r, 500));
rpc(1, "initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "f", version: "0" } });
await new Promise((r) => setTimeout(r, 800));
rpc(2, "tools/list", {});
await new Promise((r) => setTimeout(r, 500));
rpc(3, "resources/list", {});
await new Promise((r) => setTimeout(r, 500));
// a tool call that exercises the granular params
if (dir === "exa") rpc(4, "tools/call", { name: "exa_search", arguments: { query: "x", startPublishedDate: "2024-01-01T00:00:00Z" } });
else rpc(4, "tools/call", { name: "brave_web_search", arguments: { query: "x", extra: true, count: 2 } });
await new Promise((r) => setTimeout(r, 4000));
child.kill();
console.error(`${dir}: stdout lines=${total} bad=${bad} ${bad === 0 ? "FRAMING OK" : "FRAMING CORRUPTED"}`);
process.exit(bad === 0 ? 0 : 1);
