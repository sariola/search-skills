#!/usr/bin/env node

// Exa MCP Server
// ----------------------------------------------------------------------------
// A Model Context Protocol server for the Exa REST API (api.exa.ai).
//
// Quality bar: mirrors the structure of the Linear MCP reference —
//   * a RateLimiter that serializes API calls, tracks request metrics, and
//     throttles toward the plan's hourly budget;
//   * a typed client wrapping every call with the rate limiter and
//     error classification;
//   * JSON-Schema input contracts for each tool, with inline defaults and
//     allowed values so the model can make the right granular choice;
//   * addressable resources (team info, per-URL content);
//   * a server prompt that teaches the model when to use each tool;
//   * apiMetrics attached to every tool response.
//
// Auth: EXA_API_KEY (via `x-api-key` header). Loaded from the environment or a
// .env file (Bun and Node both surface process.env; Bun auto-loads .env).
//
// Endpoints used (all live-verified against the public OpenAPI):
//   POST /search            exa_search
//   POST /contents          exa_fetch      (+ resource exa-content:///{url})
//   POST /answer            exa_answer
//   POST /agent/runs        exa_agent
//   GET  /agent/runs/{id}   (polling inside exa_agent)
//   POST /findSimilar       exa_find_similar
//   GET  /v0/teams/me       (resource exa-team:)
//
// No credentials are ever printed.
// ---------------------------------------------------------------------------

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequest,
  CallToolRequestSchema,
  ListResourcesRequestSchema,
  ListToolsRequestSchema,
  ReadResourceRequestSchema,
  ListResourceTemplatesRequestSchema,
  ListPromptsRequestSchema,
  GetPromptRequestSchema,
  Tool,
  ResourceTemplate,
  Prompt,
} from "@modelcontextprotocol/sdk/types.js";

const API_BASE = "https://api.exa.ai";
const WEBSETS_BASE = "https://api.exa.ai/websets";

// Authoritative Exa enums (from the public OpenAPI).
const SEARCH_TYPES = ["instant", "fast", "auto", "deep-lite", "deep", "deep-reasoning"] as const;
const CATEGORIES = ["company", "publication", "news", "personal site", "financial report", "people"] as const;
const AGENT_EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "auto", "max"] as const;
const TEXT_VERBOSITIES = ["compact", "standard", "full"] as const;
// The company/people entity-backed categories reject date filters and
// domain exclusion (the API returns 400).
const ENTITY_CATEGORIES = new Set(["company", "people"]);
// Agent run stop reasons (OpenAPI AgentStopReason). The probe live-verifies
// which the API actually accepts on a running run.
const AGENT_STOP_REASONS = ["schema_satisfied", "budget_reached", "stopped", "error", "cancelled"] as const;
// The /agent/runs/{id}/stop endpoint requires this beta feature header; every
// other agent endpoint (create, get, list, events, cancel) works without it.
const AGENT_STOP_BETA_HEADER = "agent-max-effort-2026-07-27";
// All /batches* endpoints (create, get, list, delete, cancel) require this
// beta token; without it the API 403s even on plans that enable /batches.
const BATCHES_BETA_HEADER = "batches-2026-06-06";
// Webset search behavior: override the auto criteria vs append to them.
const WEBSET_BEHAVIORS = ["override", "append"] as const;
// Webset enrichment output formats.
const WEBSET_ENRICH_FORMATS = ["text", "date", "number", "options", "email", "phone", "url"] as const;
// Agent Connect data-source providers (Exa Connect).
const AGENT_DATA_SOURCES = [
  "fiber", "financial_datasets", "similarweb", "baselayer",
  "affiliate", "particle", "jinko", "polymarket",
] as const;
// Search-change monitor lifecycle statuses.
const MONITOR_STATUSES = ["active", "paused", "disabled"] as const;
// Monitor bulk-action verb.
const MONITOR_BATCH_ACTIONS = ["delete", "pause", "unpause"] as const;
// Webset-monitor merge behavior for new items.
const WMONITOR_BEHAVIORS = ["append", "override"] as const;
// Webset-monitor enable/disable.
const WMONITOR_STATUSES = ["enabled", "disabled"] as const;

// ---------------------------------------------------------------------------
// Rate limiter (mirrors the Linear MCP server: serial queue, hourly budget,
// request metrics).
// ---------------------------------------------------------------------------
interface RateLimiterMetrics {
  totalRequests: number;
  requestsInLastHour: number;
  averageRequestTime: number;
  queueLength: number;
  lastRequestTime: number;
}

class RateLimiter {
  public readonly requestsPerHour = 2000; // conservative Exa budget
  private queue: (() => Promise<any>)[] = [];
  private processing = false;
  private lastRequestTime = 0;
  private readonly minDelayMs = 3600000 / this.requestsPerHour;
  private requestTimes: number[] = [];
  private requestTimestamps: number[] = [];
  private lifetimeRequests = 0; // total attempts ever (not pruned by the hourly window)

  async enqueue<T>(fn: () => Promise<T>, operation?: string): Promise<T> {
    const queuePosition = this.queue.length;
    // stdout is the JSON-RPC framing channel for a stdio MCP server — log to
    // stderr (console.error) or the line reader chokes on every API call.
    console.error(`[Exa API] Enqueueing request${operation ? ` for ${operation}` : ""} (queue: ${queuePosition})`);
    return new Promise((resolve, reject) => {
      this.queue.push(async () => {
        this.lifetimeRequests++; // count every attempt, success or failure
        const startedAt = Date.now(); // flight start — excludes queue wait
        try {
          const result = await fn();
          resolve(result);
        } catch (error) {
          console.error(`[Exa API] Error in request${operation ? ` for ${operation}` : ""}: `, error);
          reject(error);
        } finally {
          // Track in finally so a failed request still counts against the
          // hourly budget gate and the average-time metric.
          this.trackRequest(startedAt, Date.now(), operation);
        }
      });
      this.processQueue();
    });
  }

  private async processQueue() {
    if (this.processing || this.queue.length === 0) return;
    this.processing = true;
    while (this.queue.length > 0) {
      const now = Date.now();
      const timeSinceLastRequest = now - this.lastRequestTime;
      const requestsInLastHour = this.requestTimestamps.filter((t) => t > now - 3600000).length;
      if (requestsInLastHour >= this.requestsPerHour * 0.9 && timeSinceLastRequest < this.minDelayMs) {
        const waitTime = this.minDelayMs - timeSinceLastRequest;
        await new Promise((resolve) => setTimeout(resolve, waitTime));
      }
      const fn = this.queue.shift();
      if (fn) {
        this.lastRequestTime = Date.now();
        await fn();
      }
    }
    this.processing = false;
  }

  private trackRequest(startTime: number, endTime: number, _operation?: string) {
    this.requestTimes.push(endTime - startTime);
    this.requestTimestamps.push(startTime);
    const oneHourAgo = Date.now() - 3600000;
    this.requestTimestamps = this.requestTimestamps.filter((t) => t > oneHourAgo);
    this.requestTimes = this.requestTimes.slice(-this.requestTimestamps.length);
  }

  getMetrics(): RateLimiterMetrics {
    const now = Date.now();
    const recent = this.requestTimestamps.filter((t) => t > now - 3600000);
    return {
      totalRequests: this.lifetimeRequests,
      requestsInLastHour: recent.length,
      averageRequestTime: this.requestTimes.length > 0
        ? this.requestTimes.reduce((a, b) => a + b, 0) / this.requestTimes.length
        : 0,
      queueLength: this.queue.length,
      lastRequestTime: this.lastRequestTime,
    };
  }
}

// ---------------------------------------------------------------------------
// Error type: classified, with an actionable hint. The MCP layer surfaces the
// message + a machine-readable `category`.
// ---------------------------------------------------------------------------
class ExaApiError extends Error {
  constructor(message: string, public readonly category: string, public readonly status?: number) {
    super(message);
    this.name = "ExaApiError";
  }
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------
class ExaClient {
  private apiKey: string;
  public readonly rateLimiter: RateLimiter;

  constructor(apiKey: string) {
    if (!apiKey) throw new Error("EXA_API_KEY environment variable is required");
    this.apiKey = apiKey;
    this.rateLimiter = new RateLimiter();
  }

  private async raw(
    method: string,
    path: string,
    opts: { json?: any; base?: string; timeoutMs?: number; idempotent?: boolean; extraHeaders?: Record<string, string> } = {},
  ): Promise<any> {
    const { json, base = API_BASE, timeoutMs = 45000, idempotent = method.toUpperCase() === "GET", extraHeaders } = opts;
    const url = `${base}${path}`;
    const resp = await fetch(url, {
      method,
      headers: {
        "x-api-key": this.apiKey,
        "Content-Type": "application/json",
        ...(extraHeaders ?? {}),
      },
      body: json === undefined ? undefined : JSON.stringify(json),
      signal: AbortSignal.timeout(timeoutMs),
    });
    let data: any = undefined;
    const text = await resp.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = undefined;
      }
    }
    if (resp.ok) {
      if (data === undefined && text === "") return {};
      return data ?? {};
    }
    // classify
    let detail = data ?? text.slice(0, 400);
    // Exa's error body is { requestId, error: string, tag }; `detail` above is
    // the whole parsed object, so pull the human-readable `error` string before
    // interpolating to avoid "Detail: [object Object]".
    const msg: string =
      data && typeof data.error === "string" ? data.error :
      typeof detail === "string" ? detail :
      (() => { try { return JSON.stringify(detail).slice(0, 200); } catch { return "(unavailable)"; } })();
    if (resp.status === 401) {
      throw new ExaApiError(
        `Exa API key rejected (401). Check EXA_API_KEY. Detail: ${msg}`, "auth", 401);
    }
    if (resp.status === 403) {
      throw new ExaApiError(
        `Exa endpoint ${path} returned 403: not enabled on the current plan ` +
        `(agent runs / monitors / websets / batches require a specific plan). Detail: ${msg}`,
        "plan", 403);
    }
    if (resp.status === 404) {
      throw new ExaApiError(
        `Exa endpoint ${path} returned 404: resource not found (bad id or path). Detail: ${msg}`,
        "not_found", 404);
    }
    if (resp.status === 429) {
      throw new ExaApiError(
        `Exa rate limit hit (429). Slow down and retry. Detail: ${msg}`, "rate_limit", 429);
    }
    if (resp.status === 400 || resp.status === 422) {
      throw new ExaApiError(
        `Exa rejected the request (${resp.status}): invalid params/payload. Detail: ${msg}`,
        "bad_request", resp.status);
    }
    if (resp.status >= 500) {
      throw new ExaApiError(
        `Exa server error (${resp.status}) on ${path}. ${idempotent ? "Retryable." : "Not retried (non-idempotent)."}`,
        "server", resp.status);
    }
    throw new ExaApiError(`Exa endpoint ${path} returned ${resp.status}. Detail: ${msg}`, "http", resp.status);
  }

  private async get(path: string, base = API_BASE, timeoutMs = 45000,
    extraHeaders?: Record<string, string>): Promise<any> {
    return this.raw("GET", path, { base, timeoutMs, extraHeaders });
  }

  private async post(path: string, json: any, opts: { base?: string; timeoutMs?: number; idempotent?: boolean; extraHeaders?: Record<string, string> } = {}): Promise<any> {
    return this.raw("POST", path, { json, ...opts });
  }

  addMetrics(response: any) {
    const m = this.rateLimiter.getMetrics();
    return {
      ...response,
      metadata: {
        ...(typeof response === "object" && response !== null && "metadata" in (response as any)
          ? (response as any).metadata
          : {}),
        apiMetrics: {
          requestsInLastHour: m.requestsInLastHour,
          remainingRequests: this.rateLimiter.requestsPerHour - m.requestsInLastHour,
          averageRequestTime: `${Math.round(m.averageRequestTime)}ms`,
          queueLength: m.queueLength,
        },
      },
    };
  }

  // ---- endpoints --------------------------------------------------------
  search(args: ExaSearchArgs) {
    const body: any = { query: args.query, numResults: args.numResults ?? 10, type: args.searchType ?? "auto" };
    if (args.category) body.category = args.category;
    if (args.startPublishedDate) body.startPublishedDate = args.startPublishedDate;
    if (args.endPublishedDate) body.endPublishedDate = args.endPublishedDate;
    if (args.includeDomains) body.includeDomains = args.includeDomains;
    if (args.excludeDomains) body.excludeDomains = args.excludeDomains;
    if (args.userLocation) body.userLocation = args.userLocation;

    // content options live under `contents` (ContentsOptions on /search).
    const contents: any = {};
    if (args.withHighlights) {
      contents.highlights = args.highlightsQuery ? { query: args.highlightsQuery } : true;
    }
    if (args.withText || args.textMaxCharacters || args.textVerbosity) {
      if (args.withText && !args.textMaxCharacters && !args.textVerbosity) {
        contents.text = true;
      } else {
        const t: any = {};
        if (args.textMaxCharacters) t.maxCharacters = args.textMaxCharacters;
        if (args.textVerbosity) t.verbosity = args.textVerbosity;
        contents.text = t;
      }
    }
    if (args.withSummary) {
      contents.summary = args.summaryQuery ? { query: args.summaryQuery } : true;
    }
    const extras: any = {};
    if (args.links) extras.links = args.links;
    if (args.imageLinks) extras.imageLinks = args.imageLinks;
    if (Object.keys(extras).length) contents.extras = extras;
    if (Object.keys(contents).length) body.contents = contents;

    if (args.outputSchema) body.outputSchema = args.outputSchema;
    return this.rateLimiter.enqueue(
      () => this.post("/search", body, { timeoutMs: 45000 }), "exa_search");
  }

  fetch(args: ExaFetchArgs) {
    const body: any = {};
    if (args.urls && args.urls.length) body.urls = args.urls;
    if (args.ids && args.ids.length) body.ids = args.ids;
    // /contents takes ContentsOptions at the top level (same options as the
    // search `contents` block).
    if (args.withText || args.textMaxCharacters || args.textVerbosity) {
      if (args.withText && !args.textMaxCharacters && !args.textVerbosity) {
        body.text = true;
      } else {
        const t: any = {};
        if (args.textMaxCharacters) t.maxCharacters = args.textMaxCharacters;
        if (args.textVerbosity) t.verbosity = args.textVerbosity;
        body.text = t;
      }
    }
    if (args.withHighlights) body.highlights = args.highlightsQuery ? { query: args.highlightsQuery } : true;
    if (args.withSummary) body.summary = args.summaryQuery ? { query: args.summaryQuery } : true;
    const extras: any = {};
    if (args.links) extras.links = args.links;
    if (args.imageLinks) extras.imageLinks = args.imageLinks;
    if (Object.keys(extras).length) body.extras = extras;
    if (args.subpages) body.subpages = args.subpages;
    if (args.subpageTarget) body.subpageTarget = args.subpageTarget;
    if (args.maxAgeHours !== undefined && args.maxAgeHours !== null) body.maxAgeHours = args.maxAgeHours;
    return this.rateLimiter.enqueue(
      () => this.post("/contents", body, { timeoutMs: 60000 }), "exa_fetch");
  }

  answer(args: ExaAnswerArgs) {
    const body: any = { query: args.query, text: args.text ?? false, stream: false };
    if (args.outputSchema) body.outputSchema = args.outputSchema;
    if (args.systemPrompt) body.systemPrompt = args.systemPrompt;
    if (args.userLocation) body.userLocation = args.userLocation;
    return this.rateLimiter.enqueue(
      () => this.post("/answer", body, { timeoutMs: 90000 }), "exa_answer");
  }

  async agent(args: ExaAgentArgs): Promise<any> {
    // CreateAgentRunRequest: query / effort / systemPrompt / outputSchema /
    // previousRunId / metadata / dataSources are TOP-LEVEL fields (NOT nested
    // under `input`). `data` / `exclusion` nest under `body.input`.
    const body: any = { query: args.query };
    if (args.effort) body.effort = args.effort;
    if (args.systemPrompt) body.systemPrompt = args.systemPrompt;
    if (args.outputSchema) body.outputSchema = args.outputSchema;
    if (args.maxCostDollars) body.budget = { maxCostDollars: args.maxCostDollars };
    if (args.previousRunId) body.previousRunId = args.previousRunId;
    if (args.metadata) body.metadata = args.metadata;
    if (args.dataSources && args.dataSources.length) {
      body.dataSources = args.dataSources.map((p) => ({ provider: p }));
    }
    const input: any = {};
    if (args.data && args.data.length) input.data = args.data;
    if (args.exclusion && args.exclusion.length) input.exclusion = args.exclusion;
    if (Object.keys(input).length) body.input = input;
    const created = await this.rateLimiter.enqueue(
      () => this.post("/agent/runs", body, { timeoutMs: 45000, idempotent: false }), "exa_agent:create");
    const runId: string = created.id;
    const deadline = Date.now() + (args.timeout ?? 120) * 1000;
    let last: any = created;
    // Terminal statuses: completed | failed | cancelled.
    // In-flight statuses: queued | running.
    while (Date.now() < deadline) {
      last = await this.rateLimiter.enqueue(
        () => this.get(`/agent/runs/${runId}`, API_BASE, 45000), "exa_agent:poll");
      if (["completed", "failed", "cancelled"].includes(last?.status)) break;
      await new Promise((r) => setTimeout(r, 2000));
    }
    if (last?.status === "completed") {
      return last;
    }
    if (last?.status === "failed") {
      throw new ExaApiError(
        `Exa agent run ${runId} failed${last?.stopReason ? ` (stopReason: ${last.stopReason})` : ""}. ` +
        `${last?.output?.text ?? ""}`.trim(),
        "agent_failed", 500);
    }
    if (last?.status === "cancelled") {
      throw new ExaApiError(`Exa agent run ${runId} was cancelled.`, "agent_cancelled", 409);
    }
    // Deadline reached while still queued/running: the remote run may still be
    // running. Surface it as a timeout, NOT a success — the model must not be
    // told the research finished when it did not.
    throw new ExaApiError(
      `Exa agent run ${runId} not finished after ${(args.timeout ?? 120)}s ` +
      `(status ${last?.status ?? "unknown"}). The run may still be running; call exa_agent again with a ` +
      `larger timeout or check back later.`,
      "agent_timeout", 408);
  }

  // ---- agent run management (inspect / control prior + running runs) ----
  agentGet(runId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/agent/runs/${encodeURIComponent(runId)}`, API_BASE, 45000), "exa_agent_get");
  }

  agentList(args: { status?: string; limit?: number; cursor?: string }) {
    const params = new URLSearchParams();
    if (args.status) params.set("status", args.status);
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/agent/runs${qs}`, API_BASE, 45000), "exa_agent_list");
  }

  agentCancel(runId: string) {
    return this.rateLimiter.enqueue(
      () => this.post(`/agent/runs/${encodeURIComponent(runId)}/cancel`, {}, { timeoutMs: 45000, idempotent: true }),
      "exa_agent_cancel");
  }

  agentStop(runId: string, reason: string) {
    return this.rateLimiter.enqueue(
      () => this.post(
        `/agent/runs/${encodeURIComponent(runId)}/stop`,
        { reason },
        { timeoutMs: 45000, idempotent: true, extraHeaders: { "Exa-Beta": AGENT_STOP_BETA_HEADER } },
      ),
      "exa_agent_stop");
  }

  agentEvents(runId: string, args: { limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/agent/runs/${encodeURIComponent(runId)}/events${qs}`, API_BASE, 45000),
      "exa_agent_events");
  }

  // ---- batches (bulk /search + /agent/runs) -----------------------------
  batchCreate(requests: any[], metadata?: any) {
    if (!Array.isArray(requests) || !requests.length) {
      throw new ExaApiError("exa_batch_create requires at least one sub-request", "bad_request");
    }
    const seen = new Set<string>();
    const norm = requests.map((r, i) => {
      const item: any = { ...r, method: "POST", customId: r?.customId ?? `req-${i}` };
      if (item.url !== "/search" && item.url !== "/agent/runs") {
        throw new ExaApiError(
          `exa_batch_create: sub-request '${item.customId}' url must be '/search' or '/agent/runs' (got ${JSON.stringify(item.url)})`,
          "bad_request");
      }
      if (!item.body || typeof item.body !== "object") {
        throw new ExaApiError(
          `exa_batch_create: sub-request '${item.customId}' needs a 'body' payload`, "bad_request");
      }
      const id = String(item.customId);
      if (id.length < 1 || id.length > 64) {
        throw new ExaApiError(`exa_batch_create: customId '${id}' must be 1-64 chars`, "bad_request");
      }
      if (seen.has(id)) {
        throw new ExaApiError(`exa_batch_create: duplicate customId '${id}'`, "bad_request");
      }
      seen.add(id);
      return item;
    });
    const body: any = { requests: norm };
    if (metadata && typeof metadata === "object") body.metadata = metadata;
    return this.rateLimiter.enqueue(
      () => this.post("/batches", body, {
        timeoutMs: 45000, idempotent: false,
        extraHeaders: { "Exa-Beta": BATCHES_BETA_HEADER },
      }), "exa_batch_create");
  }

  batchGet(batchId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/batches/${encodeURIComponent(batchId)}`, API_BASE, 45000,
        { "Exa-Beta": BATCHES_BETA_HEADER }), "exa_batch_get");
  }

  // Download a batch's short-lived presigned results URL (plain fetch, no auth
  // header) and parse the JSONL, keyed by customId. Done server-side because a
  // model cannot reliably fetch a presigned URL inside a tool call.
  async batchDownloadResults(resultsUrl: string): Promise<{ results: Record<string, any>; lines: any[] }> {
    const resp = await fetch(resultsUrl, { redirect: "follow", signal: AbortSignal.timeout(60000) });
    if (!resp.ok) {
      throw new ExaApiError(`Batch results download failed (HTTP ${resp.status}).`, "server", resp.status);
    }
    const lines: any[] = [];
    const results: Record<string, any> = {};
    const text = await resp.text();
    text.split(/\r?\n/).forEach((raw, idx) => {
      const s = raw.trim();
      if (!s) return;
      let obj: any;
      try { obj = JSON.parse(s); } catch { obj = { _raw: s, _line: idx }; }
      lines.push(obj);
      const key = obj && typeof obj === "object" && obj.customId != null ? String(obj.customId) : `line-${idx}`;
      results[key] = obj;
    });
    return { results, lines };
  }

  // Poll a batch to a terminal state (completed|cancelling|cancelled|expired),
  // then download + parse its results JSONL keyed by customId.
  async batchPoll(batchId: string, timeoutSec: number): Promise<any> {
    const deadline = Date.now() + timeoutSec * 1000;
    let snap: any = await this.batchGet(batchId);
    while (Date.now() < deadline && (snap?.status ?? "") === "in_progress") {
      await new Promise((r) => setTimeout(r, 3000));
      snap = await this.batchGet(batchId);
    }
    const status = (snap?.status ?? "").toLowerCase();
    if (status === "in_progress") {
      throw new ExaApiError(
        `Batch ${batchId} still in_progress after ${timeoutSec}s. Call exa_batch_get again with wait=true to resume, or exa_batch_get without wait for a one-shot check.`,
        "batch_timeout", 408);
    }
    if (status === "expired") {
      throw new ExaApiError(
        `Batch ${batchId} expired (requestCounts: ${JSON.stringify(snap?.requestCounts ?? {})}). Results are unavailable; create a fresh batch.`,
        "batch_expired", 410);
    }
    let results: Record<string, any> = {};
    let lines: any[] = [];
    let resultsUrl: string | undefined;
    if (snap?.resultsUrl) {
      const url: string = snap.resultsUrl;
      resultsUrl = url;
      try {
        const dl = await this.batchDownloadResults(url);
        results = dl.results;
        lines = dl.lines;
      } catch (e) {
        // Non-fatal: surface the batch + URL even if the download failed.
        console.error("Batch results download error:", e);
      }
    }
    return { batch: snap, results, results_lines: lines, results_url: resultsUrl };
  }

  // ---- websets (bulk web-scale entity discovery) ------------------------
  websetPreview(args: { query: string; entity?: any; count?: number }) {
    const search: any = { query: args.query };
    if (args.entity) search.entity = args.entity;
    if (args.count !== undefined) search.count = args.count;
    // The preview-items flag is a query param (?search=true), NOT a body field.
    return this.rateLimiter.enqueue(
      () => this.post("/v0/websets/preview?search=true", { search }, { base: WEBSETS_BASE, timeoutMs: 90000 }),
      "exa_webset_preview");
  }

  websetCreate(args: ExaWebsetCreateArgs): Promise<any> {
    const searchPayload: any = { query: args.query, count: args.count ?? 10 };
    if (args.entity) searchPayload.entity = args.entity;
    if (args.criteria && args.criteria.length) searchPayload.criteria = args.criteria;
    if (args.exclude && args.exclude.length) searchPayload.exclude = args.exclude;
    if (args.scope && args.scope.length) searchPayload.scope = args.scope;
    if (args.recall) searchPayload.recall = true;
    if (args.maxPeoplePerCompany !== undefined) searchPayload.maxPeoplePerCompany = args.maxPeoplePerCompany;
    // Set behavior=override when the caller overrides/generated criteria so the
    // auto-detection does not also apply (matches the Python client).
    if (args.criteria && args.criteria.length) searchPayload.behavior = "override";
    const body: any = { search: searchPayload };
    if (args.title) body.title = args.title;
    if (args.externalId) body.externalId = args.externalId;
    if (args.metadata) body.metadata = args.metadata;
    if (args.enrichments && args.enrichments.length) body.enrichments = args.enrichments;
    if (args.imports && args.imports.length) body.import = args.imports; // SINGULAR key
    const created = this.rateLimiter.enqueue(
      () => this.post("/v0/websets", body, { base: WEBSETS_BASE, timeoutMs: 90000, idempotent: false }),
      "exa_webset_create");
    return this.websetPollToSearchTerminal(created, args.timeout ?? 90);
  }

  // Poll a freshly-created webset until its search phase is terminal (all
  // searches completed/cancelled/failed) or the timeout elapses; throw a
  // webset_timeout so the model isn't told a still-running webset is done.
  async websetPollToSearchTerminal(created: Promise<any>, timeoutSec: number): Promise<any> {
    const id: string = (await created).id;
    const deadline = Date.now() + timeoutSec * 1000;
    let ws: any = { id };
    while (Date.now() < deadline) {
      ws = await this.websetGetRaw(id);
      const searches: any[] = ws?.searches ?? [];
      const terminal = (ws?.status ?? "") === "paused" || (ws?.status ?? "") === "cancelled"
        || (searches.length > 0 && searches.every((s) => (s?.status ?? "") === "completed"
          || (s?.status ?? "") === "cancelled" || (s?.status ?? "") === "failed"));
      if (terminal) break;
      await new Promise((r) => setTimeout(r, 2500));
    }
    const stillRunning = !((ws?.searches ?? []).length > 0
      && (ws?.searches ?? []).every((s: any) => ["completed", "cancelled", "failed"].includes(s?.status ?? "")));
    if (stillRunning) {
      throw new ExaApiError(
        `Webset ${id} search phase still running after ${timeoutSec}s. Call exa_webset_get(id, wait=true) to continue waiting, or exa_webset_items(id) to read what has arrived.`,
        "webset_timeout", 408);
    }
    return ws;
  }

  websetGetRaw(id: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/websets/${encodeURIComponent(id)}`, WEBSETS_BASE, 45000), "exa_webset_get");
  }

  async websetGet(id: string, args: { includeItems?: boolean; wait?: boolean; timeoutSec?: number } = {}): Promise<any> {
    let ws = await this.websetGetRaw(id);
    if (args.wait) {
      const deadline = Date.now() + (args.timeoutSec ?? 60) * 1000;
      while (Date.now() < deadline) {
        ws = await this.websetGetRaw(id);
        const searches: any[] = ws?.searches ?? [];
        if ((ws?.status ?? "") === "paused" || (ws?.status ?? "") === "cancelled"
          || (searches.length > 0 && searches.every((s) => ["completed", "cancelled", "failed"].includes(s?.status ?? "")))) {
          break;
        }
        await new Promise((r) => setTimeout(r, 2500));
      }
    }
    if (args.includeItems) {
      const params = new URLSearchParams();
      params.set("expand", "items");
      const withItems = await this.rateLimiter.enqueue(
        () => this.get(`/v0/websets/${encodeURIComponent(id)}?${params.toString()}`, WEBSETS_BASE, 60000),
        "exa_webset_get:items");
      return withItems;
    }
    return ws;
  }

  websetList(args: { limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/websets${qs}`, WEBSETS_BASE, 45000), "exa_webset_list");
  }

  websetItems(id: string, args: { limit?: number; cursor?: string; sourceId?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    if (args.sourceId) params.set("sourceId", args.sourceId);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/websets/${encodeURIComponent(id)}/items${qs}`, WEBSETS_BASE, 45000),
      "exa_webset_items");
  }

  websetAddSearch(id: string, args: { query: string; count?: number; entity?: any; criteria?: any[]; exclude?: any[]; scope?: any[]; recall?: boolean; maxPeoplePerCompany?: number; behavior?: string; metadata?: any }) {
    // CreateWebsetSearchParameters is sent at the TOP LEVEL (NOT nested under a
    // `search` key).
    const body: any = { query: args.query, count: args.count ?? 10 };
    if (args.behavior) body.behavior = args.behavior;
    if (args.entity) body.entity = args.entity;
    if (args.criteria && args.criteria.length) body.criteria = args.criteria;
    if (args.exclude && args.exclude.length) body.exclude = args.exclude;
    if (args.scope && args.scope.length) body.scope = args.scope;
    if (args.recall) body.recall = true;
    if (args.maxPeoplePerCompany !== undefined) body.maxPeoplePerCompany = args.maxPeoplePerCompany;
    if (args.metadata) body.metadata = args.metadata;
    return this.rateLimiter.enqueue(
      () => this.post(`/v0/websets/${encodeURIComponent(id)}/searches`, body, { base: WEBSETS_BASE, timeoutMs: 60000 }),
      "exa_webset_add_search");
  }

  websetEnrich(id: string, args: { description: string; format?: string; options?: any[]; metadata?: any }) {
    const body: any = { description: args.description };
    if (args.format) body.format = args.format;
    if (args.options && args.options.length) body.options = args.options;
    if (args.metadata) body.metadata = args.metadata;
    return this.rateLimiter.enqueue(
      () => this.post(`/v0/websets/${encodeURIComponent(id)}/enrichments`, body, { base: WEBSETS_BASE, timeoutMs: 60000 }),
      "exa_webset_enrich");
  }

  websetUpdate(id: string, args: { title?: string; metadata?: any }) {
    const body: any = {};
    if (args.title !== undefined) body.title = args.title;
    if (args.metadata !== undefined) body.metadata = args.metadata;
    if (!Object.keys(body).length) {
      throw new ExaApiError("exa_webset_update needs at least one of title or metadata", "bad_request");
    }
    return this.rateLimiter.enqueue(
      () => this.post(`/v0/websets/${encodeURIComponent(id)}`, body, { base: WEBSETS_BASE, timeoutMs: 45000 }),
      "exa_webset_update");
  }

  websetCancel(id: string) {
    return this.rateLimiter.enqueue(
      () => this.post(`/v0/websets/${encodeURIComponent(id)}/cancel`, {}, { base: WEBSETS_BASE, timeoutMs: 45000, idempotent: true }),
      "exa_webset_cancel");
  }

  websetDelete(id: string) {
    return this.rateLimiter.enqueue(
      () => this.raw("DELETE", `/v0/websets/${encodeURIComponent(id)}`, { base: WEBSETS_BASE, timeoutMs: 45000 }),
      "exa_webset_delete");
  }

  findSimilar(args: ExaFindSimilarArgs) {
    const body: any = { url: args.url, numResults: args.numResults ?? 10 };
    if (args.category) body.category = args.category;
    if (args.excludeSourceDomain) body.excludeSourceDomain = true;
    if (args.includeDomains) body.includeDomains = args.includeDomains;
    if (args.excludeDomains) body.excludeDomains = args.excludeDomains;
    if (args.startPublishedDate) body.startPublishedDate = args.startPublishedDate;
    if (args.endPublishedDate) body.endPublishedDate = args.endPublishedDate;
    return this.rateLimiter.enqueue(
      () => this.post("/findSimilar", body, { timeoutMs: 45000 }), "exa_find_similar");
  }

  team() {
    return this.rateLimiter.enqueue(
      () => this.get("/v0/teams/me", WEBSETS_BASE, 30000), "exa_team");
  }

  private async patch(path: string, json: any, opts: { base?: string; timeoutMs?: number; idempotent?: boolean; extraHeaders?: Record<string, string> } = {}): Promise<any> {
    return this.raw("PATCH", path, { json, ...opts });
  }

  // ---- batch lifecycle (list / cancel / delete) ---------------------------
  batchList(args: { limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/batches${qs}`, API_BASE, 45000, { "Exa-Beta": BATCHES_BETA_HEADER }),
      "exa_batch_list");
  }

  batchCancel(batchId: string) {
    return this.rateLimiter.enqueue(
      () => this.post(`/batches/${encodeURIComponent(batchId)}/cancel`, {},
        { timeoutMs: 45000, idempotent: true, extraHeaders: { "Exa-Beta": BATCHES_BETA_HEADER } }),
      "exa_batch_cancel");
  }

  batchDelete(batchId: string) {
    return this.rateLimiter.enqueue(
      () => this.raw("DELETE", `/batches/${encodeURIComponent(batchId)}`,
        { timeoutMs: 45000, extraHeaders: { "Exa-Beta": BATCHES_BETA_HEADER } }),
      "exa_batch_delete");
  }

  // ---- agent runs ---------------------------------------------------------
  agentDelete(runId: string) {
    return this.rateLimiter.enqueue(
      () => this.raw("DELETE", `/agent/runs/${encodeURIComponent(runId)}`, { timeoutMs: 45000 }),
      "exa_agent_delete");
  }

  // ---- search-change monitors (api.exa.ai/monitors) -----------------------
  monitorCreate(args: { query: string; webhookUrl: string; name?: string; period?: string; numResults?: number; outputSchema?: object; metadata?: any; events?: string[]; search?: any }) {
    const search: any = { query: args.query };
    if (args.numResults !== undefined) search.numResults = args.numResults;
    if (args.search && typeof args.search === "object") Object.assign(search, args.search);
    const body: any = {
      search,
      trigger: { type: "interval", period: args.period ?? "24h" },
      webhook: { url: args.webhookUrl },
    };
    if (args.name) body.name = args.name;
    if (args.outputSchema) body.outputSchema = args.outputSchema;
    if (args.metadata) body.metadata = args.metadata;
    if (args.events && args.events.length) body.webhook.events = args.events;
    return this.rateLimiter.enqueue(
      () => this.post("/monitors", body, { timeoutMs: 45000, idempotent: false }), "exa_monitor_create");
  }

  monitorList(args: { status?: string; name?: string; metadata?: Record<string, string>; limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.status) params.set("status", args.status);
    if (args.name) params.set("name", args.name);
    if (args.metadata) for (const [k, v] of Object.entries(args.metadata)) params.set(`metadata[${k}]`, String(v));
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/monitors${qs}`, API_BASE, 45000), "exa_monitor_list");
  }

  monitorGet(monitorId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/monitors/${encodeURIComponent(monitorId)}`, API_BASE, 45000), "exa_monitor_get");
  }

  monitorUpdate(monitorId: string, body: any) {
    return this.rateLimiter.enqueue(
      () => this.patch(`/monitors/${encodeURIComponent(monitorId)}`, body, { timeoutMs: 45000 }),
      "exa_monitor_update");
  }

  monitorDelete(monitorId: string) {
    return this.rateLimiter.enqueue(
      () => this.raw("DELETE", `/monitors/${encodeURIComponent(monitorId)}`, { timeoutMs: 45000 }),
      "exa_monitor_delete");
  }

  monitorTrigger(monitorId: string) {
    return this.rateLimiter.enqueue(
      () => this.post(`/monitors/${encodeURIComponent(monitorId)}/trigger`, {}, { timeoutMs: 45000, idempotent: false }),
      "exa_monitor_trigger");
  }

  monitorRuns(monitorId: string, args: { limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/monitors/${encodeURIComponent(monitorId)}/runs${qs}`, API_BASE, 45000),
      "exa_monitor_runs");
  }

  monitorRunGet(monitorId: string, runId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/monitors/${encodeURIComponent(monitorId)}/runs/${encodeURIComponent(runId)}`, API_BASE, 45000),
      "exa_monitor_run_get");
  }

  monitorBatch(args: { action: string; name?: string; status?: string; metadata?: Record<string, string>; dryRun?: boolean; limit?: number }) {
    const filter: any = {};
    if (args.name) filter.name = args.name;
    if (args.status) filter.status = args.status;
    if (args.metadata) filter.metadata = args.metadata;
    if (!Object.keys(filter).length) {
      throw new ExaApiError("exa_monitor_batch needs at least one filter (name|status|metadata)", "bad_request");
    }
    const body: any = { action: args.action, filter, dryRun: args.dryRun ?? true, limit: args.limit ?? 50 };
    return this.rateLimiter.enqueue(
      () => this.post("/monitors/batch", body, { timeoutMs: 45000, idempotent: false }), "exa_monitor_batch");
  }

  // ---- webset monitors (api.exa.ai/websets/v0/monitors) -------------------
  websetMonitorCreate(args: { websetId: string; cron: string; timezone?: string; count?: number; query?: string; criteria?: any[]; entity?: any; behavior?: string; metadata?: any }) {
    const config: any = { count: args.count ?? 10, behavior: args.behavior ?? "append" };
    if (args.query) config.query = args.query;
    if (args.criteria && args.criteria.length) config.criteria = args.criteria;
    if (args.entity) config.entity = args.entity;
    const body: any = {
      websetId: args.websetId,
      cadence: { cron: args.cron, timezone: args.timezone ?? "Etc/UTC" },
      behavior: { type: "search", config },
    };
    if (args.metadata) body.metadata = args.metadata;
    return this.rateLimiter.enqueue(
      () => this.post("/v0/monitors", body, { base: WEBSETS_BASE, timeoutMs: 30000, idempotent: false }),
      "exa_webset_monitor_create");
  }

  websetMonitorList(args: { websetId?: string; limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.websetId) params.set("websetId", args.websetId);
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/monitors${qs}`, WEBSETS_BASE, 45000), "exa_webset_monitor_list");
  }

  websetMonitorGet(monitorId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/monitors/${encodeURIComponent(monitorId)}`, WEBSETS_BASE, 45000),
      "exa_webset_monitor_get");
  }

  websetMonitorUpdate(monitorId: string, body: any) {
    return this.rateLimiter.enqueue(
      () => this.patch(`/v0/monitors/${encodeURIComponent(monitorId)}`, body, { base: WEBSETS_BASE, timeoutMs: 45000 }),
      "exa_webset_monitor_update");
  }

  websetMonitorDelete(monitorId: string) {
    return this.rateLimiter.enqueue(
      () => this.raw("DELETE", `/v0/monitors/${encodeURIComponent(monitorId)}`, { base: WEBSETS_BASE, timeoutMs: 45000 }),
      "exa_webset_monitor_delete");
  }

  websetMonitorRuns(monitorId: string, args: { limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/monitors/${encodeURIComponent(monitorId)}/runs${qs}`, WEBSETS_BASE, 45000),
      "exa_webset_monitor_runs");
  }

  websetMonitorRunGet(monitorId: string, runId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/monitors/${encodeURIComponent(monitorId)}/runs/${encodeURIComponent(runId)}`, WEBSETS_BASE, 45000),
      "exa_webset_monitor_run_get");
  }

  // ---- webset sub-resources (items / searches / enrichments) --------------
  websetItemGet(id: string, itemId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/websets/${encodeURIComponent(id)}/items/${encodeURIComponent(itemId)}`, WEBSETS_BASE, 45000),
      "exa_webset_item_get");
  }

  websetItemDelete(id: string, itemId: string) {
    return this.rateLimiter.enqueue(
      () => this.raw("DELETE", `/v0/websets/${encodeURIComponent(id)}/items/${encodeURIComponent(itemId)}`, { base: WEBSETS_BASE, timeoutMs: 45000 }),
      "exa_webset_item_delete");
  }

  websetSearchStatus(id: string, searchId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/websets/${encodeURIComponent(id)}/searches/${encodeURIComponent(searchId)}`, WEBSETS_BASE, 45000),
      "exa_webset_search_status");
  }

  websetSearchCancel(id: string, searchId: string) {
    return this.rateLimiter.enqueue(
      () => this.post(`/v0/websets/${encodeURIComponent(id)}/searches/${encodeURIComponent(searchId)}/cancel`, {},
        { base: WEBSETS_BASE, timeoutMs: 45000, idempotent: true }), "exa_webset_search_cancel");
  }

  enrichmentGet(id: string, enrichmentId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/websets/${encodeURIComponent(id)}/enrichments/${encodeURIComponent(enrichmentId)}`, WEBSETS_BASE, 45000),
      "exa_enrichment_get");
  }

  enrichmentCancel(id: string, enrichmentId: string) {
    return this.rateLimiter.enqueue(
      () => this.post(`/v0/websets/${encodeURIComponent(id)}/enrichments/${encodeURIComponent(enrichmentId)}/cancel`, {},
        { base: WEBSETS_BASE, timeoutMs: 45000, idempotent: true }), "exa_enrichment_cancel");
  }

  enrichmentUpdate(id: string, enrichmentId: string, body: any) {
    return this.rateLimiter.enqueue(
      () => this.patch(`/v0/websets/${encodeURIComponent(id)}/enrichments/${encodeURIComponent(enrichmentId)}`, body,
        { base: WEBSETS_BASE, timeoutMs: 45000 }), "exa_enrichment_update");
  }

  enrichmentDelete(id: string, enrichmentId: string) {
    return this.rateLimiter.enqueue(
      () => this.raw("DELETE", `/v0/websets/${encodeURIComponent(id)}/enrichments/${encodeURIComponent(enrichmentId)}`,
        { base: WEBSETS_BASE, timeoutMs: 45000 }), "exa_enrichment_delete");
  }

  // ---- imports (bring-your-own CSV entity lists) --------------------------
  importCreate(args: { format?: string; size: number; count: number; entity: object; title?: string; csv?: any; metadata?: any }) {
    const body: any = { format: args.format ?? "csv", size: args.size, count: args.count, entity: args.entity };
    if (args.title) body.title = args.title;
    if (args.csv) body.csv = args.csv;
    if (args.metadata) body.metadata = args.metadata;
    return this.rateLimiter.enqueue(
      () => this.post("/v0/imports", body, { base: WEBSETS_BASE, timeoutMs: 60000, idempotent: false }),
      "exa_import_create");
  }

  importList(args: { limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/imports${qs}`, WEBSETS_BASE, 45000), "exa_import_list");
  }

  importGet(importId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/imports/${encodeURIComponent(importId)}`, WEBSETS_BASE, 45000), "exa_import_get");
  }

  importUpdate(importId: string, body: any) {
    return this.rateLimiter.enqueue(
      () => this.patch(`/v0/imports/${encodeURIComponent(importId)}`, body, { base: WEBSETS_BASE, timeoutMs: 45000 }),
      "exa_import_update");
  }

  importDelete(importId: string) {
    return this.rateLimiter.enqueue(
      () => this.raw("DELETE", `/v0/imports/${encodeURIComponent(importId)}`, { base: WEBSETS_BASE, timeoutMs: 45000 }),
      "exa_import_delete");
  }

  // ---- webhooks (event delivery) ------------------------------------------
  webhookCreate(args: { events: string[]; url: string; metadata?: any }) {
    const body: any = { events: args.events, url: args.url };
    if (args.metadata) body.metadata = args.metadata;
    return this.rateLimiter.enqueue(
      () => this.post("/v0/webhooks", body, { base: WEBSETS_BASE, timeoutMs: 45000, idempotent: false }),
      "exa_webhook_create");
  }

  webhookList(args: { limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/webhooks${qs}`, WEBSETS_BASE, 45000), "exa_webhook_list");
  }

  webhookGet(webhookId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/webhooks/${encodeURIComponent(webhookId)}`, WEBSETS_BASE, 45000), "exa_webhook_get");
  }

  webhookUpdate(webhookId: string, body: any) {
    return this.rateLimiter.enqueue(
      () => this.patch(`/v0/webhooks/${encodeURIComponent(webhookId)}`, body, { base: WEBSETS_BASE, timeoutMs: 45000 }),
      "exa_webhook_update");
  }

  webhookDelete(webhookId: string) {
    return this.rateLimiter.enqueue(
      () => this.raw("DELETE", `/v0/webhooks/${encodeURIComponent(webhookId)}`, { base: WEBSETS_BASE, timeoutMs: 45000 }),
      "exa_webhook_delete");
  }

  webhookAttempts(webhookId: string, args: { limit?: number; cursor?: string; eventType?: string; successful?: boolean } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    if (args.eventType) params.set("eventType", args.eventType);
    if (args.successful !== undefined) params.set("successful", args.successful ? "true" : "false");
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/webhooks/${encodeURIComponent(webhookId)}/attempts${qs}`, WEBSETS_BASE, 45000),
      "exa_webhook_attempts");
  }

  // ---- events (audit log) -------------------------------------------------
  eventList(args: { limit?: number; cursor?: string; type?: string; types?: string[]; createdBefore?: string; createdAfter?: string } = {}) {
    const params = new URLSearchParams();
    if (args.limit !== undefined) params.set("limit", String(args.limit));
    if (args.cursor) params.set("cursor", args.cursor);
    if (args.type) params.set("type", args.type);
    const types: string[] = [];
    if (args.types) types.push(...args.types);
    if (args.type && !types.includes(args.type)) types.push(args.type);
    if (types.length) params.set("types", types.join(","));
    if (args.createdBefore) params.set("createdBefore", args.createdBefore);
    if (args.createdAfter) params.set("createdAfter", args.createdAfter);
    const qs = params.toString() ? `?${params.toString()}` : "";
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/events${qs}`, WEBSETS_BASE, 45000), "exa_event_list");
  }

  eventGet(eventId: string) {
    return this.rateLimiter.enqueue(
      () => this.get(`/v0/events/${encodeURIComponent(eventId)}`, WEBSETS_BASE, 45000), "exa_event_get");
  }
}

// ---------------------------------------------------------------------------
// Tool arg types
// ---------------------------------------------------------------------------
interface ExaSearchArgs {
  query: string;
  numResults?: number;
  category?: string;
  searchType?: string;
  startPublishedDate?: string;
  endPublishedDate?: string;
  includeDomains?: string[];
  excludeDomains?: string[];
  userLocation?: string;
  withHighlights?: boolean;
  highlightsQuery?: string;
  withText?: boolean;
  textMaxCharacters?: number;
  textVerbosity?: string;
  withSummary?: boolean;
  summaryQuery?: string;
  links?: number;
  imageLinks?: number;
  outputSchema?: object;
}
interface ExaFetchArgs {
  urls?: string[];
  ids?: string[];
  textVerbosity?: string;
  textMaxCharacters?: number;
  withText?: boolean;
  withHighlights?: boolean;
  highlightsQuery?: string;
  withSummary?: boolean;
  summaryQuery?: string;
  links?: number;
  imageLinks?: number;
  subpages?: number;
  subpageTarget?: string;
  maxAgeHours?: number;
}
interface ExaAnswerArgs {
  query: string;
  text?: boolean;
  outputSchema?: object;
  systemPrompt?: string;
  userLocation?: string;
}
interface ExaAgentArgs {
  query: string;
  effort?: string;
  systemPrompt?: string;
  outputSchema?: object;
  maxCostDollars?: number;
  timeout?: number;
  previousRunId?: string;
  metadata?: Record<string, string>;
  data?: Record<string, any>[];
  exclusion?: Record<string, any>[];
  dataSources?: string[];
}
interface ExaWebsetCreateArgs {
  query: string;
  count?: number;
  title?: string;
  externalId?: string;
  entity?: Record<string, any>;
  criteria?: { description: string; weight?: number }[];
  exclude?: Record<string, any>[];
  scope?: Record<string, any>[];
  recall?: boolean;
  maxPeoplePerCompany?: number;
  metadata?: Record<string, string>;
  enrichments?: { description: string; format?: string }[];
  imports?: Record<string, any>[];
  timeout?: number;
}
interface ExaFindSimilarArgs {
  url: string;
  numResults?: number;
  category?: string;
  excludeSourceDomain?: boolean;
  includeDomains?: string[];
  excludeDomains?: string[];
  startPublishedDate?: string;
  endPublishedDate?: string;
}

// ---------------------------------------------------------------------------
// Tool definitions (JSON-Schema input contracts, Linear-style: each field
// carries its default and allowed values inline so the model can make the
// right granular choice without extra round-trips).
// ---------------------------------------------------------------------------
const exaSearchTool: Tool = {
  name: "exa_search",
  description:
    "Searches the web via Exa using flexible criteria. Supports filtering by " +
    "any combination of: search depth (searchType: instant=fastest, fast, " +
    "auto=balanced default, deep-lite, deep, deep-reasoning=deepest), vertical " +
    "(category: company, publication, news, personal site, financial report, " +
    "people; other strings act as hints), published-date range, domain " +
    "include/exclude, location, and per-result content (highlights, text, " +
    "summary, links). Returns up to 10 results by default (configurable via " +
    "numResults).",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "Search query" },
      numResults: { type: "number", description: "Max results to return, 1-100 (default: 10)" },
      searchType: {
        type: "string",
        description:
          "Search depth (instant=minimum latency, fast=reduced latency, " +
          "auto=balanced, deep-lite=lightweight research ~4s, deep=multi-step " +
          "research with synthesis, deep-reasoning=stronger reasoning) " +
          "(default: auto)",
      },
      category: {
        type: "string",
        description:
          "Vertical to focus on (company, publication, news, personal site, " +
          "financial report, people; other strings act as hints). company and " +
          "people do not support published-date filters or excludeDomains.",
      },
      startPublishedDate: {
        type: "string",
        description: "Only results published after this ISO 8601 date (e.g. '2024-01-01T00:00:00Z')",
      },
      endPublishedDate: {
        type: "string",
        description: "Only results published before this ISO 8601 date",
      },
      includeDomains: {
        type: "array",
        items: { type: "string" },
        description: "Only these domains/paths (use instead of a site: operator in the query)",
      },
      excludeDomains: {
        type: "array",
        items: { type: "string" },
        description: "Exclude these domains/paths",
      },
      userLocation: { type: "string", description: "2-letter ISO country code (e.g. 'US')" },
      withHighlights: { type: "boolean", description: "Return query-relevant highlight snippets (default: false)" },
      highlightsQuery: { type: "string", description: "Guide which highlights are picked (with withHighlights)" },
      withText: { type: "boolean", description: "Return full page text (default: false)" },
      textMaxCharacters: { type: "number", description: "Cap page text at N characters, 1-10000 (with withText)" },
      textVerbosity: { type: "string", description: "Text rendering (compact | standard | full, default: full; with withText)" },
      withSummary: { type: "boolean", description: "Return a per-result summary (default: false)" },
      summaryQuery: { type: "string", description: "Guide the per-result summary (with withSummary)" },
      links: { type: "number", description: "Outbound links per result, 0-1000 (default: 0)" },
      imageLinks: { type: "number", description: "Image URLs per result, 0-1000 (default: 0)" },
      outputSchema: {
        type: "object",
        description:
          "JSON Schema (root 'text' or 'object'); when given, the response " +
          "includes a synthesized, citation-grounded answer in 'output' " +
          "(adds ~2s)",
      },
    },
    required: ["query"],
  },
};

const exaFetchTool: Tool = {
  name: "exa_fetch",
  description:
    "Fetches full page content for one or more URLs or Exa document ids via " +
    "/contents (title, text, highlights, summary, links, images). Use this to " +
    "deep-read specific results — e.g. the top hits from exa_search — instead " +
    "of re-searching. Provide urls or ids (ids are the `id` field on exa_search " +
    "results).",
  inputSchema: {
    type: "object",
    properties: {
      urls: {
        type: "array",
        items: { type: "string" },
        description: "URLs to fetch (provide urls or ids)",
      },
      ids: {
        type: "array",
        items: { type: "string" },
        description: "Exa document ids to fetch (from exa_search results)",
      },
      withText: { type: "boolean", description: "Return full page text (default: false)" },
      textVerbosity: { type: "string", description: "Text rendering (compact | standard | full, default: full; with withText)" },
      textMaxCharacters: { type: "number", description: "Cap page text at N characters, 1-10000 (with withText)" },
      withHighlights: { type: "boolean", description: "Return query-relevant highlights (default: false)" },
      highlightsQuery: { type: "string", description: "Focus highlights on this text (with withHighlights)" },
      withSummary: { type: "boolean", description: "Return a page summary (default: false)" },
      summaryQuery: { type: "string", description: "Focus the summary on this text (with withSummary)" },
      links: { type: "number", description: "Outbound links per page, 0-1000 (default: 0)" },
      imageLinks: { type: "number", description: "Image URLs per page, 0-1000 (default: 0)" },
      subpages: { type: "number", description: "Subpages to crawl per result, 0-100 (default: 0)" },
      subpageTarget: { type: "string", description: "Term to locate specific subpages (e.g. 'sources')" },
      maxAgeHours: { type: "number", description: "Max age of cached content in hours; 0 forces a fresh fetch, -1 uses cache" },
    },
  },
};

const exaAnswerTool: Tool = {
  name: "exa_answer",
  description:
    "Answers a question directly via /answer, with a list of cited source URLs. " +
    "Use for 'just tell me' questions that need sources; use exa_search when you " +
    "want the result links themselves. Returns the answer string, or a " +
    "structured object when outputSchema is given.",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "The question (required)" },
      text: {
        type: "boolean",
        description: "Include full page text of each cited source. (default: false)",
      },
      outputSchema: {
        type: "object",
        description: "JSON Schema; when given the answer is returned as a structured object.",
      },
      systemPrompt: {
        type: "string",
        description: "Extra guidance: source preferences, style, novelty constraints.",
      },
      userLocation: {
        type: "string",
        description: "Two-letter ISO country code for location-aware answers.",
      },
    },
    required: ["query"],
  },
};

const exaAgentTool: Tool = {
  name: "exa_agent",
  description:
    "Runs a full Exa agentic research task via /agent/runs: multi-step web " +
    "research written into a cited answer. Use for open-ended research a single " +
    "exa_search cannot cover. Slower and more expensive than exa_search; the " +
    "run is polled to completion, bounded by `timeout` seconds (default: 120). " +
    "Supports continuing a prior run (chained research via previousRunId), " +
    "processing/avoiding given records (data / exclusion), and Exa Connect data " +
    "sources (dataSources). Requires a plan that enables /agent/runs.",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "The research task / question (required)" },
      effort: {
        type: "string",
        description:
          "Reasoning effort: minimal | low | medium | high | xhigh | max " +
          "(highest-effort beta, completeness over latency/cost) | auto (let " +
          "Exa choose). (default: auto)",
      },
      systemPrompt: {
        type: "string",
        description: "Additional instructions: source preferences, style, novelty constraints.",
      },
      outputSchema: {
        type: "object",
        description: "JSON Schema for structured output.",
      },
      maxCostDollars: {
        type: "number",
        description:
          "Per-run spending cap in USD, 1-100. (default: $5 cap on auto effort)",
      },
      timeout: {
        type: "number",
        description: "Max seconds to wait for the run to finish. (default: 120)",
      },
      previousRunId: {
        type: "string",
        description:
          "Continue this prior completed run's research thread (its `id`). Use " +
          "for chained/progressive refinement without re-doing the prior work.",
      },
      data: {
        type: "array",
        items: { type: "object" },
        description: "JSON records the agent should process/enrich as part of the run.",
      },
      exclusion: {
        type: "array",
        items: { type: "object" },
        description: "JSON records/entities the agent should avoid returning.",
      },
      dataSources: {
        type: "array",
        items: {
          type: "string",
          enum: AGENT_DATA_SOURCES as unknown as string[],
        },
        description:
          "Exa Connect providers to draw from (max 5): fiber, financial_datasets, " +
          "similarweb, baselayer, affiliate, particle, jinko, polymarket.",
      },
      metadata: {
        type: "object",
        additionalProperties: { type: "string" },
        description: "Arbitrary string key-value pairs to store with the run (your own tracking).",
      },
    },
    required: ["query"],
  },
};

const exaFindSimilarTool: Tool = {
  name: "exa_find_similar",
  description:
    "Finds web pages similar to a given URL via /findSimilar. Use to broaden " +
    "one good source into related coverage. Supports filtering by published-" +
    "date range, vertical, and domain include/exclude (same rules as exa_" +
    "search). Returns up to 10 results by default (configurable via numResults).",
  inputSchema: {
    type: "object",
    properties: {
      url: { type: "string", description: "The seed URL (required)" },
      numResults: { type: "number", description: "Max similar pages, 1-100 (default: 10)" },
      category: {
        type: "string",
        description:
          "Vertical to bias similarity (company, publication, news, personal " +
          "site, financial report, people)",
      },
      excludeSourceDomain: {
        type: "boolean",
        description: "Exclude results from the seed URL's own domain. (default: false)",
      },
      includeDomains: {
        type: "array",
        items: { type: "string" },
        description: "Only these domains/paths.",
      },
      excludeDomains: {
        type: "array",
        items: { type: "string" },
        description: "Exclude these domains/paths.",
      },
      startPublishedDate: {
        type: "string",
        description: "Only similar pages published after this ISO 8601 date.",
      },
      endPublishedDate: {
        type: "string",
        description: "Only similar pages published before this ISO 8601 date.",
      },
    },
    required: ["url"],
  },
};

// ---- Agent run management -------------------------------------------------
const exaAgentGetTool: Tool = {
  name: "exa_agent_get",
  description:
    "Fetches a single Exa agent run by id (status, stopReason, cost, output, " +
    "grounding). Use to check on a run after exa_agent timed out, or to read a " +
    "prior run's result before chaining onto it via exa_agent's previousRunId.",
  inputSchema: {
    type: "object",
    properties: {
      runId: { type: "string", description: "The agent run id (agent_run_...)" },
    },
    required: ["runId"],
  },
};

const exaAgentListTool: Tool = {
  name: "exa_agent_list",
  description:
    "Lists prior Exa agent runs (reverse chronological) with status/usage/cost. " +
    "Supports filtering by status (queued|running|completed|failed|cancelled) and " +
    "paging via cursor. Use to find a run id to chain onto or inspect.",
  inputSchema: {
    type: "object",
    properties: {
      status: {
        type: "string",
        enum: ["queued", "running", "completed", "failed", "cancelled"],
        description: "Filter by run status.",
      },
      limit: { type: "number", description: "Max runs to return, 1-100 (default: 50)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
  },
};

const exaAgentCancelTool: Tool = {
  name: "exa_agent_cancel",
  description:
    "Aborts a running Exa agent run (final status becomes 'cancelled'; work in " +
    "flight is discarded). Use when the research direction is wrong and you want " +
    "to stop spending. Idempotent.",
  inputSchema: {
    type: "object",
    properties: {
      runId: { type: "string", description: "The agent run id (agent_run_...)" },
    },
    required: ["runId"],
  },
};

const exaAgentStopTool: Tool = {
  name: "exa_agent_stop",
  description:
    "Stops a running Exa agent run at the next safe checkpoint, keeping any " +
    "partial output already produced (distinct from cancel, which discards). " +
    "Use to save a useful-but-incomplete result. Graceful stop is only supported " +
    "for effort=max runs — the API 400s for lower-effort or already-terminal runs " +
    "(use cancel instead). Idempotent.",
  inputSchema: {
    type: "object",
    properties: {
      runId: { type: "string", description: "The agent run id (agent_run_...)" },
      reason: {
        type: "string",
        enum: AGENT_STOP_REASONS as unknown as string[],
        description:
          "Stop reason: schema_satisfied | budget_reached | stopped | error | " +
          "cancelled. (default: stopped)",
      },
    },
    required: ["runId"],
  },
};

const exaAgentEventsTool: Tool = {
  name: "exa_agent_events",
  description:
    "Returns the ordered event stream of an Exa agent run (tool calls, web " +
    "lookups, reasoning steps) — useful for debugging why a run behaved a way or " +
    "for streaming progress. Supports paging via cursor.",
  inputSchema: {
    type: "object",
    properties: {
      runId: { type: "string", description: "The agent run id (agent_run_...)" },
      limit: { type: "number", description: "Max events per page (default: API default)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
    required: ["runId"],
  },
};

// ---- Batches (bulk /search + /agent/runs) ---------------------------------
const exaBatchCreateTool: Tool = {
  name: "exa_batch_create",
  description:
    "Creates a batch of asynchronous Exa sub-requests, each addressed by a " +
    "caller-chosen customId and routed to /search or /agent/runs. Exa runs them " +
    "server-side and, once complete, provides a short-lived presigned results URL " +
    "that exa_batch_get (wait=true) downloads and keys by customId. Use for " +
    "50+ same-shape requests in one call. Paid (one Exa request per sub-request); " +
    "requires a plan that enables /batches.",
  inputSchema: {
    type: "object",
    properties: {
      requests: {
        type: "array",
        minItems: 1,
        items: {
          type: "object",
          properties: {
            customId: { type: "string", maxLength: 64, description: "Your 1-64 char handle keying this result (default: req-<index>)" },
            url: { type: "string", enum: ["/search", "/agent/runs"], description: "The Exa route this sub-request targets (required)" },
            body: { type: "object", description: "That route's request payload (required)" },
          },
          required: ["url", "body"],
        },
        description: "The sub-requests to run (method is auto-set to POST).",
      },
      metadata: {
        type: "object",
        additionalProperties: true,
        description: "Optional string KV to attach to the batch for tracking.",
      },
    },
    required: ["requests"],
  },
};

const exaBatchGetTool: Tool = {
  name: "exa_batch_get",
  description:
    "Fetches a batch's status, lifecycle counts, and (when complete) its " +
    "per-request results downloaded from the presigned URL and keyed by " +
    "customId. With wait=true it polls to a terminal state " +
    "(completed|cancelling|cancelled|expired) before downloading — the one-call " +
    "path for fire-and-then-collect. Requires a plan that enables /batches.",
  inputSchema: {
    type: "object",
    properties: {
      batchId: { type: "string", description: "The batch id (batch_...)" },
      wait: {
        type: "boolean",
        description: "If true, poll to a terminal state (default: false)",
      },
      timeout: {
        type: "number",
        description: "Max seconds to wait when wait=true. (default: 150)",
      },
    },
    required: ["batchId"],
  },
};

// ---- Websets (bulk web-scale entity discovery) ----------------------------
const exaWebsetCreateTool: Tool = {
  name: "exa_webset_create",
  description:
    "Creates a Webset: web-scale discovery of structured entity records " +
    "(people / companies / articles / research papers / custom) matching a " +
    "natural-language query, each with rich profiles (name, company, location, " +
    "work history, industry, headcount, ...). The query is decomposed into an " +
    "entity + criteria (overridable); results accumulate async. Polls the search " +
    "phase to terminal under `timeout` seconds (default: 90) before returning. " +
    "Paid (web-scale discovery); requires a plan that enables Websets.",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "Natural-language search (e.g. 'US marketing agencies that focus on consumer products')" },
      count: { type: "number", description: "Number of items to attempt to find, >=1 (default: 10)" },
      title: { type: "string", description: "Optional display title for the webset" },
      externalId: { type: "string", description: "Your own identifier for this webset" },
      entity: {
        type: "object",
        description: "Override the detected entity, e.g. {\"type\":\"company\"} or {\"type\":\"person\"}",
      },
      criteria: {
        type: "array",
        items: { type: "object" },
        description: "Override the auto-generated criteria, list of {\"description\": \"...\"} (<=5)",
      },
      exclude: {
        type: "array",
        items: { type: "object" },
        description: "Existing imports/websets to omit, [{\"source\":\"webset\"|\"import\",\"id\":...}]",
      },
      scope: {
        type: "array",
        items: { type: "object" },
        description: "Restrict the search to existing imports, [{\"source\":\"import\",\"id\":...}]",
      },
      recall: { type: "boolean", description: "Request an estimate of total reachable results (default: false)" },
      maxPeoplePerCompany: { type: "number", description: "Soft cap on people from the same employer" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "String KV for your own tracking" },
      enrichments: {
        type: "array",
        items: { type: "object" },
        description: "Enrichments to run on found items, [{\"description\": \"...\"}]",
      },
      imports: {
        type: "array",
        items: { type: "object" },
        description: "Seed from existing collections, [{\"source\":\"webset\"|\"import\",\"id\":...}]",
      },
      timeout: { type: "number", description: "Max seconds to wait for the search phase to finish (default: 90)" },
    },
    required: ["query"],
  },
};

const exaWebsetPreviewTool: Tool = {
  name: "exa_webset_preview",
  description:
    "Previews how a natural-language query decomposes into entity + criteria " +
    "(and a short sample of matching items) WITHOUT creating a webset. Cheap " +
    "recon before committing to a paid exa_webset_create. Returns the detected " +
    "entity, criteria, and preview items.",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "The natural-language search query to preview (required)" },
      entity: {
        type: "object",
        description: "Optional entity hint to guide decomposition, e.g. {\"type\":\"company\"}",
      },
      count: { type: "number", description: "Max preview items to return, 1-10 (default: 10)" },
    },
    required: ["query"],
  },
};

const exaWebsetGetTool: Tool = {
  name: "exa_webset_get",
  description:
    "Fetches a single webset by id (status, searches, imports, enrichments). " +
    "includeItems=true embeds the current item list in one round-trip; wait=true " +
    "polls until the search phase is terminal (bounded by `timeout` seconds, " +
    "default: 60).",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      includeItems: { type: "boolean", description: "Embed the current item list (default: false)" },
      wait: { type: "boolean", description: "Poll until the search phase is terminal (default: false)" },
      timeout: { type: "number", description: "Max seconds to wait when wait=true (default: 60)" },
    },
    required: ["websetId"],
  },
};

const exaWebsetListTool: Tool = {
  name: "exa_webset_list",
  description:
    "Lists all websets for the authenticated team (reverse chronological), with " +
    "paging via cursor. Use to discover existing websets to chain or delete.",
  inputSchema: {
    type: "object",
    properties: {
      limit: { type: "number", description: "Max websets per page, 1-100 (default: 50)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
  },
};

const exaWebsetItemsTool: Tool = {
  name: "exa_webset_items",
  description:
    "Retrieves the structured items (person/company/article records) a webset " +
    "has found so far. Each item carries an `evaluations` array (one entry per " +
    "criterion) with satisfied: yes|no|unclear — a fast way to isolate qualified " +
    "leads. Supports paging via cursor and filtering by the producing sourceId.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      limit: { type: "number", description: "Max items per page, 1-100 (default: 100)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
      sourceId: { type: "string", description: "Filter items by the source (search/import) that produced them" },
    },
    required: ["websetId"],
  },
};

const exaWebsetAddSearchTool: Tool = {
  name: "exa_webset_add_search",
  description:
    "Adds another search run to an existing webset, scoped to the webset's item " +
    "corpus. Use to refine or broaden a webset after its first pass. Parameters " +
    "are sent at top level (not nested under a `search` key). behavior: override " +
    "(replaces criteria) or append (adds to them).",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      query: { type: "string", description: "Natural-language search describing entities you want (required)" },
      count: { type: "number", description: "Number of items to attempt to find, >=1 (default: 10)" },
      entity: { type: "object", description: "Override detected entity, e.g. {\"type\":\"company\"}" },
      criteria: { type: "array", items: { type: "object" }, description: "Criteria override, [{\"description\":\"...\"}] (<=5)" },
      exclude: { type: "array", items: { type: "object" }, description: "Existing imports/websets to avoid" },
      scope: { type: "array", items: { type: "object" }, description: "Restrict search to existing imports" },
      recall: { type: "boolean", description: "Request a total-match estimate (default: false)" },
      maxPeoplePerCompany: { type: "number", description: "Soft cap on people from the same employer" },
      behavior: {
        type: "string",
        enum: WEBSET_BEHAVIORS as unknown as string[],
        description: "override (default) replaces criteria; append adds to them",
      },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "String KV tags" },
    },
    required: ["websetId", "query"],
  },
};

const exaWebsetEnrichTool: Tool = {
  name: "exa_webset_enrich",
  description:
    "Asks the enrichment agent to produce one extra field per webset item " +
    "(e.g. 'primary email address and phone number'). Results are stored back on " +
    "each item. Paid; requires a plan that enables Webset enrichments. format " +
    "auto-detected when omitted (text|date|number|options|email|phone|url).",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      description: { type: "string", description: "What to find/produce for each item, <=5000 chars (required)" },
      format: {
        type: "string",
        enum: WEBSET_ENRICH_FORMATS as unknown as string[],
        description: "Output format: text|date|number|options|email|phone|url (auto when omitted)",
      },
      options: {
        type: "array",
        items: { type: "object" },
        description: "When format=options, list of {\"label\": ...} choices (<=150)",
      },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "String KV tags" },
    },
    required: ["websetId", "description"],
  },
};

const exaWebsetUpdateTool: Tool = {
  name: "exa_webset_update",
  description:
    "Updates a webset's title and/or metadata (your own tracking fields). " +
    "Requires at least one of title or metadata.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      title: { type: "string", description: "New display title" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "New string KV metadata" },
    },
    required: ["websetId"],
  },
};

const exaWebsetCancelTool: Tool = {
  name: "exa_webset_cancel",
  description:
    "Cancels a running webset's search. Idempotent. Use to stop a discovery run " +
    "that has gone off-course; already-found items are retained.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
    },
    required: ["websetId"],
  },
};

const exaWebsetDeleteTool: Tool = {
  name: "exa_webset_delete",
  description:
    "Deletes a webset and everything under it (items, searches, enrichments). " +
    "Irreversible. Use to clean up a completed or abandoned webset.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
    },
    required: ["websetId"],
  },
};

// ---- Batch lifecycle (list / cancel / delete) ------------------------------
const exaBatchListTool: Tool = {
  name: "exa_batch_list",
  description:
    "Lists batches (reverse chronological) with status and lifecycle counts. " +
    "Use to find a batch to inspect, cancel, or delete. Paged via cursor. " +
    "Requires a plan that enables /batches.",
  inputSchema: {
    type: "object",
    properties: {
      limit: { type: "number", description: "Max batches per page, 1-100 (default: 100)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
  },
};

const exaBatchCancelTool: Tool = {
  name: "exa_batch_cancel",
  description:
    "Asks the API to stop an in-progress batch; in-flight sub-requests complete " +
    "but no further queued ones start. Idempotent. Requires a plan that " +
    "enables /batches.",
  inputSchema: {
    type: "object",
    properties: {
      batchId: { type: "string", description: "The batch id (batch_...)" },
    },
    required: ["batchId"],
  },
};

const exaBatchDeleteTool: Tool = {
  name: "exa_batch_delete",
  description:
    "Deletes a batch and its stored results (irreversible). Use to clean up a " +
    "completed or abandoned batch. Requires a plan that enables /batches.",
  inputSchema: {
    type: "object",
    properties: {
      batchId: { type: "string", description: "The batch id (batch_...)" },
    },
    required: ["batchId"],
  },
};

// ---- Agent-run extras ------------------------------------------------------
const exaAgentDeleteTool: Tool = {
  name: "exa_agent_delete",
  description:
    "Deletes a completed agent run (irreversible), freeing its stored output " +
    "and cost record. Use for cleanup; only finished runs can be deleted.",
  inputSchema: {
    type: "object",
    properties: {
      runId: { type: "string", description: "The agent run id (agent_run_...)" },
    },
    required: ["runId"],
  },
};

// ---- Search-change monitors (api.exa.ai/monitors) --------------------------
const exaMonitorCreateTool: Tool = {
  name: "exa_monitor_create",
  description:
    "Creates a recurring monitor that re-runs a search on a period and delivers " +
    "new/changed results to an HTTPS webhook. Runs are interval-scheduled " +
    "(period like \"1h\"/\"1d\", default: 24h). Requires a plan that enables " +
    "monitors and a reachable webhook_url.",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "The search query the monitor repeatedly runs (required)" },
      webhookUrl: { type: "string", description: "HTTPS endpoint receiving each run's output (required)" },
      name: { type: "string", description: "Optional display name" },
      period: { type: "string", description: "Run cadence, single-unit duration (default: 24h)" },
      numResults: { type: "number", description: "Result count per run (default: 5)" },
      outputSchema: { type: "object", description: "Optional structured-output schema" },
      events: { type: "array", items: { type: "string" }, description: "Subset of monitor events to deliver (default: all)" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "String KV echoed back on deliveries" },
      search: { type: "object", description: "Extra /search fields (contents, filters) merged into the monitored search" },
    },
    required: ["query", "webhookUrl"],
  },
};

const exaMonitorListTool: Tool = {
  name: "exa_monitor_list",
  description:
    "Lists monitors, optionally filtered by status / name / metadata. Paged " +
    "via cursor. Requires a plan that enables monitors.",
  inputSchema: {
    type: "object",
    properties: {
      status: { type: "string", enum: MONITOR_STATUSES as unknown as string[], description: "Filter by lifecycle status" },
      name: { type: "string", description: "Substring filter on the monitor name" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "Exact-match string KV filters" },
      limit: { type: "number", description: "Max monitors per page, 1-500 (default: 50)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
  },
};

const exaMonitorGetTool: Tool = {
  name: "exa_monitor_get",
  description:
    "Fetches a single monitor by id (status, trigger, webhook, last run). " +
    "Requires a plan that enables monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The monitor id" },
    },
    required: ["monitorId"],
  },
};

const exaMonitorUpdateTool: Tool = {
  name: "exa_monitor_update",
  description:
    "Patches a monitor: its search, trigger period, webhook url, metadata, or " +
    "status (active|paused|disabled). Send only the fields to change. Requires " +
    "a plan that enables monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The monitor id" },
      search: { type: "object", description: "Replace the monitored /search payload" },
      trigger: { type: "object", description: "Replace the trigger, e.g. {\"type\":\"interval\",\"period\":\"1d\"}" },
      webhook: { type: "object", description: "Replace the delivery webhook, e.g. {\"url\":\"https://...\"}" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "Replace metadata" },
      status: { type: "string", enum: MONITOR_STATUSES as unknown as string[], description: "Set lifecycle status" },
    },
    required: ["monitorId"],
  },
};

const exaMonitorDeleteTool: Tool = {
  name: "exa_monitor_delete",
  description:
    "Deletes a monitor permanently. Use to stop and remove a monitor you no " +
    "longer want. Requires a plan that enables monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The monitor id" },
    },
    required: ["monitorId"],
  },
};

const exaMonitorTriggerTool: Tool = {
  name: "exa_monitor_trigger",
  description:
    "Triggers an immediate monitor run regardless of schedule (works on active " +
    "or paused monitors). Requires a plan that enables monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The monitor id" },
    },
    required: ["monitorId"],
  },
};

const exaMonitorRunsTool: Tool = {
  name: "exa_monitor_runs",
  description:
    "Lists a monitor's runs (reverse chronological) with status and delivery " +
    "result. Paged via cursor. Requires a plan that enables monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The monitor id" },
      limit: { type: "number", description: "Max runs per page, 1-500 (default: 50)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
    required: ["monitorId"],
  },
};

const exaMonitorRunGetTool: Tool = {
  name: "exa_monitor_run_get",
  description:
    "Fetches a single monitor run, including its completed output. Requires a " +
    "plan that enables monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The monitor id" },
      runId: { type: "string", description: "The run id" },
    },
    required: ["monitorId", "runId"],
  },
};

const exaMonitorBatchTool: Tool = {
  name: "exa_monitor_batch",
  description:
    "Applies a bulk action (delete|pause|unpause) to every monitor matching a " +
    "filter. dry_run=true (default) reports which monitors would be affected " +
    "without acting. At least one filter (name|status|metadata) is required to " +
    "prevent accidental bulk ops. Requires a plan that enables monitors.",
  inputSchema: {
    type: "object",
    properties: {
      action: { type: "string", enum: MONITOR_BATCH_ACTIONS as unknown as string[], description: "The bulk action to apply" },
      name: { type: "string", description: "Substring filter on monitor name" },
      status: { type: "string", enum: MONITOR_STATUSES as unknown as string[], description: "Filter by lifecycle status" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "Exact-match string KV filters" },
      dryRun: { type: "boolean", description: "Preview affected monitors without acting (default: true)" },
      limit: { type: "number", description: "Max monitors to process in one request, <=500 (default: 50)" },
    },
    required: ["action"],
  },
};

// ---- Webset monitors (keep a webset fresh on cron) -------------------------
const exaWebsetMonitorCreateTool: Tool = {
  name: "exa_webset_monitor_create",
  description:
    "Creates a webset monitor: a cron schedule that periodically re-runs a " +
    "search and merges new matching items into a webset. Cron is at most " +
    "once/day (5 fields). Requires a plan that enables webset monitors.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId to keep fresh (required)" },
      cron: { type: "string", description: "Unix cron expression, 5 fields, at most once/day, e.g. \"0 9 * * 1\" (required)" },
      timezone: { type: "string", description: "IANA timezone (default: Etc/UTC)" },
      count: { type: "number", description: "Max items to discover per run (default: 10)" },
      query: { type: "string", description: "Natural-language search; defaults to the webset's latest search" },
      criteria: { type: "array", items: { type: "object" }, description: "Refine matching, [{\"description\":\"...\"}]" },
      entity: { type: "object", description: "Entity override, e.g. {\"type\":\"company\"}" },
      behavior: { type: "string", enum: WMONITOR_BEHAVIORS as unknown as string[], description: "How new items merge (default: append)" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "String KV for tracking" },
    },
    required: ["websetId", "cron"],
  },
};

const exaWebsetMonitorListTool: Tool = {
  name: "exa_webset_monitor_list",
  description:
    "Lists webset monitors, optionally narrowed to one webset. Paged via " +
    "cursor. Requires a plan that enables webset monitors.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "Only monitors attached to this webset" },
      limit: { type: "number", description: "Max monitors per page, 1-200 (default: 25)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
  },
};

const exaWebsetMonitorGetTool: Tool = {
  name: "exa_webset_monitor_get",
  description:
    "Fetches a single webset monitor by id. Requires a plan that enables " +
    "webset monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The webset monitor id" },
    },
    required: ["monitorId"],
  },
};

const exaWebsetMonitorUpdateTool: Tool = {
  name: "exa_webset_monitor_update",
  description:
    "Updates a webset monitor: enable/disable, change the cron cadence, or " +
    "reconfigure the search behavior. Send only the fields to change. Requires " +
    "a plan that enables webset monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The webset monitor id" },
      status: { type: "string", enum: WMONITOR_STATUSES as unknown as string[], description: "enabled | disabled" },
      cadence: { type: "object", description: "New schedule, e.g. {\"cron\":\"0 9 * * 1\",\"timezone\":\"America/New_York\"}" },
      behavior: { type: "object", description: "New behavior, e.g. {\"type\":\"search\",\"config\":{...}}" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "Replace metadata" },
    },
    required: ["monitorId"],
  },
};

const exaWebsetMonitorDeleteTool: Tool = {
  name: "exa_webset_monitor_delete",
  description:
    "Deletes a webset monitor permanently. Requires a plan that enables " +
    "webset monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The webset monitor id" },
    },
    required: ["monitorId"],
  },
};

const exaWebsetMonitorRunsTool: Tool = {
  name: "exa_webset_monitor_runs",
  description:
    "Lists a webset monitor's historical runs (reverse chronological). Paged " +
    "via cursor. Requires a plan that enables webset monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The webset monitor id" },
      limit: { type: "number", description: "Max runs per page, 1-200 (default: 25)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
    required: ["monitorId"],
  },
};

const exaWebsetMonitorRunGetTool: Tool = {
  name: "exa_webset_monitor_run_get",
  description:
    "Fetches a single webset-monitor run's details (status, output, stats). " +
    "Requires a plan that enables webset monitors.",
  inputSchema: {
    type: "object",
    properties: {
      monitorId: { type: "string", description: "The webset monitor id" },
      runId: { type: "string", description: "The run id" },
    },
    required: ["monitorId", "runId"],
  },
};

// ---- Webset sub-resources (items / searches / enrichments) -----------------
const exaWebsetItemGetTool: Tool = {
  name: "exa_webset_item_get",
  description:
    "Fetches a single webset item (full profile + its enrichment results). " +
    "Requires a plan that enables Websets.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      itemId: { type: "string", description: "The item id (from exa_webset_items)" },
    },
    required: ["websetId", "itemId"],
  },
};

const exaWebsetItemDeleteTool: Tool = {
  name: "exa_webset_item_delete",
  description:
    "Removes a single item from a webset's corpus. Use to drop a false positive " +
    "without re-running the search. Requires a plan that enables Websets.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      itemId: { type: "string", description: "The item id to remove" },
    },
    required: ["websetId", "itemId"],
  },
};

const exaWebsetSearchStatusTool: Tool = {
  name: "exa_webset_search_status",
  description:
    "Gets the status/progress of one specific search attached to a webset " +
    "(status, found count, and any recall estimate). Requires a plan that " +
    "enables Websets.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      searchId: { type: "string", description: "The search id (from the webset's searches)" },
    },
    required: ["websetId", "searchId"],
  },
};

const exaWebsetSearchCancelTool: Tool = {
  name: "exa_webset_search_cancel",
  description:
    "Cancels one specific running search on a webset (leaves the webset and " +
    "its other searches intact). Idempotent. Requires a plan that enables " +
    "Websets.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      searchId: { type: "string", description: "The search id to cancel" },
    },
    required: ["websetId", "searchId"],
  },
};

const exaEnrichmentGetTool: Tool = {
  name: "exa_enrichment_get",
  description:
    "Fetches the current state of a webset enrichment job (status, per-item " +
    "progress). Requires a plan that enables Webset enrichments.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      enrichmentId: { type: "string", description: "The enrichment id (from exa_webset_enrich)" },
    },
    required: ["websetId", "enrichmentId"],
  },
};

const exaEnrichmentCancelTool: Tool = {
  name: "exa_enrichment_cancel",
  description:
    "Cancels a running webset enrichment job. Idempotent. Requires a plan that " +
    "enables Webset enrichments.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      enrichmentId: { type: "string", description: "The enrichment id to cancel" },
    },
    required: ["websetId", "enrichmentId"],
  },
};

const exaEnrichmentUpdateTool: Tool = {
  name: "exa_enrichment_update",
  description:
    "Updates an enrichment's description / format / options / metadata. Send " +
    "only the fields to change. Requires a plan that enables Webset " +
    "enrichments.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      enrichmentId: { type: "string", description: "The enrichment id" },
      description: { type: "string", description: "New task description, 1-5000 chars" },
      format: { type: "string", enum: WEBSET_ENRICH_FORMATS as unknown as string[], description: "text|date|number|options|email|phone|url" },
      options: { type: "array", items: { type: "object" }, description: "When format=options, [{\"label\":\"...\"}] (<=150)" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "String KV tags" },
    },
    required: ["websetId", "enrichmentId"],
  },
};

const exaEnrichmentDeleteTool: Tool = {
  name: "exa_enrichment_delete",
  description:
    "Deletes a webset enrichment job (and its stored results). Use to remove " +
    "an enrichment you no longer want. Requires a plan that enables Webset " +
    "enrichments.",
  inputSchema: {
    type: "object",
    properties: {
      websetId: { type: "string", description: "The webset id or externalId (required)" },
      enrichmentId: { type: "string", description: "The enrichment id to delete" },
    },
    required: ["websetId", "enrichmentId"],
  },
};

// ---- Imports (bring-your-own CSV entity lists) ------------------------------
const exaImportCreateTool: Tool = {
  name: "exa_import_create",
  description:
    "Registers a CSV import that seeds / scopes webset searches and " +
    "enrichments with your own entity records. Sends the import metadata " +
    "(format/size/count/entity) and returns an object with a short-lived " +
    "uploadUrl to PUT the CSV bytes to before uploadValidUntil. Requires a " +
    "plan that enables imports.",
  inputSchema: {
    type: "object",
    properties: {
      size: { type: "number", description: "CSV size in bytes, up to 50 MB (required)" },
      count: { type: "number", description: "Number of records in the CSV (required)" },
      entity: { type: "object", description: "Entity type, e.g. {\"type\":\"company\"} (required)" },
      format: { type: "string", description: "File format (default: csv)" },
      title: { type: "string", description: "Display name for the import" },
      csv: { type: "object", description: "CSV column config, e.g. {\"identifier\":0} (0-based key-identifier column)" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "String KV for tracking" },
    },
    required: ["size", "count", "entity"],
  },
};

const exaImportListTool: Tool = {
  name: "exa_import_list",
  description:
    "Lists CSV imports for the team with processing status. Paged via cursor. " +
    "Requires a plan that enables imports.",
  inputSchema: {
    type: "object",
    properties: {
      limit: { type: "number", description: "Max imports per page, 1-50 (default: 50)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
  },
};

const exaImportGetTool: Tool = {
  name: "exa_import_get",
  description:
    "Fetches a single import by id (status, entity, record count). Requires a " +
    "plan that enables imports.",
  inputSchema: {
    type: "object",
    properties: {
      importId: { type: "string", description: "The import id" },
    },
    required: ["importId"],
  },
};

const exaImportUpdateTool: Tool = {
  name: "exa_import_update",
  description:
    "Updates an import's title and/or metadata. Requires a plan that enables " +
    "imports.",
  inputSchema: {
    type: "object",
    properties: {
      importId: { type: "string", description: "The import id" },
      title: { type: "string", description: "New display title" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "New string KV metadata" },
    },
    required: ["importId"],
  },
};

const exaImportDeleteTool: Tool = {
  name: "exa_import_delete",
  description:
    "Deletes an import and detaches it from websets that use it as scope. " +
    "Requires a plan that enables imports.",
  inputSchema: {
    type: "object",
    properties: {
      importId: { type: "string", description: "The import id" },
    },
    required: ["importId"],
  },
};

// ---- Webhooks (event delivery) ---------------------------------------------
const exaWebhookCreateTool: Tool = {
  name: "exa_webhook_create",
  description:
    "Registers a webhook that receives POST notifications for " +
    "webset/import/monitor events. Returns the webhook object including the " +
    "secret used to verify request signatures. Requires a plan that enables " +
    "webhooks.",
  inputSchema: {
    type: "object",
    properties: {
      events: { type: "array", items: { type: "string" }, description: "Event types to subscribe, e.g. [\"webset.search.completed\"] (required)" },
      url: { type: "string", description: "HTTPS endpoint that receives POST payloads (required)" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "String KV for routing" },
    },
    required: ["events", "url"],
  },
};

const exaWebhookListTool: Tool = {
  name: "exa_webhook_list",
  description:
    "Lists all webhooks for the team. Paged via cursor. Requires a plan that " +
    "enables webhooks.",
  inputSchema: {
    type: "object",
    properties: {
      limit: { type: "number", description: "Max webhooks per page, 1-50 (default: 50)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
    },
  },
};

const exaWebhookGetTool: Tool = {
  name: "exa_webhook_get",
  description:
    "Fetches a single webhook by id (events, url, active, secret). Requires a " +
    "plan that enables webhooks.",
  inputSchema: {
    type: "object",
    properties: {
      webhookId: { type: "string", description: "The webhook id" },
    },
    required: ["webhookId"],
  },
};

const exaWebhookUpdateTool: Tool = {
  name: "exa_webhook_update",
  description:
    "Patches a webhook's events / url / metadata. Send only the fields to " +
    "change. Requires a plan that enables webhooks.",
  inputSchema: {
    type: "object",
    properties: {
      webhookId: { type: "string", description: "The webhook id" },
      events: { type: "array", items: { type: "string" }, description: "New event-type subscription list" },
      url: { type: "string", description: "New HTTPS delivery endpoint" },
      metadata: { type: "object", additionalProperties: { type: "string" }, description: "New string KV metadata" },
    },
    required: ["webhookId"],
  },
};

const exaWebhookDeleteTool: Tool = {
  name: "exa_webhook_delete",
  description:
    "Deletes a webhook. Use to stop deliveries to an endpoint you no longer " +
    "use. Requires a plan that enables webhooks.",
  inputSchema: {
    type: "object",
    properties: {
      webhookId: { type: "string", description: "The webhook id" },
    },
    required: ["webhookId"],
  },
};

const exaWebhookAttemptsTool: Tool = {
  name: "exa_webhook_attempts",
  description:
    "Lists delivery attempts for a webhook (status code, outcome, response). " +
    "Use to debug why an event didn't arrive. Paged via cursor. Requires a " +
    "plan that enables webhooks.",
  inputSchema: {
    type: "object",
    properties: {
      webhookId: { type: "string", description: "The webhook id" },
      limit: { type: "number", description: "Max attempts per page, 1-50 (default: 50)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
      eventType: { type: "string", description: "Filter by event type, e.g. \"webset.search.completed\"" },
      successful: { type: "boolean", description: "true for delivered, false for failed attempts" },
    },
    required: ["webhookId"],
  },
};

// ---- Events (audit log / onboarding) ----------------------------------------
const exaEventListTool: Tool = {
  name: "exa_event_list",
  description:
    "Lists recent team events (webset / import / monitor activity) with rich " +
    "filtering by type and time window. Paged via cursor. Requires a plan that " +
    "enables events.",
  inputSchema: {
    type: "object",
    properties: {
      limit: { type: "number", description: "Max events per page, 1-100 (default: 50)" },
      cursor: { type: "string", description: "Pagination cursor from a previous response" },
      type: { type: "string", description: "Single event-type filter, e.g. \"webset.search.completed\"" },
      types: { type: "array", items: { type: "string" }, description: "List of event types to include (combines with type)" },
      createdBefore: { type: "string", description: "ISO-8601: only events created before this" },
      createdAfter: { type: "string", description: "ISO-8601: only events created after this" },
    },
  },
};

const exaEventGetTool: Tool = {
  name: "exa_event_get",
  description:
    "Fetches a single event by id (full payload and metadata). Requires a plan " +
    "that enables events.",
  inputSchema: {
    type: "object",
    properties: {
      eventId: { type: "string", description: "The event id" },
    },
    required: ["eventId"],
  },
};

// ---------------------------------------------------------------------------
// Resources + resource templates
// ---------------------------------------------------------------------------
const resourceTemplates: ResourceTemplate[] = [
  {
    uriTemplate: "exa-team:",
    name: "Exa Team",
    description: "The team/plan associated with the EXA_API_KEY (plan, credits, limits).",
    parameters: {},
    examples: ["exa-team:"],
  },
  {
    uriTemplate: "exa-content:///{url}",
    name: "Exa Page Content",
    description: "Fetch a single URL via Exa /contents (title, text, highlights, summary).",
    parameters: {
      url: { type: "string", description: "The URL to fetch (URL-encoded)" },
    },
    examples: ["exa-content:///https%3A%2F%2Fexa.ai%2F"],
  },
];

const serverPrompt: Prompt = {
  name: "exa-server-prompt",
  description: "Instructions for using the Exa MCP server effectively",
  instructions: `This server provides access to Exa, a web search and page-reading API (api.exa.ai), including its full research surface: agentic runs, web-scale websets, server-side batches, change monitors, CSV imports, webhooks, and the event audit log.

Escalation ladder (cheapest first — only step up when the cheaper tool can't answer):
1. exa_search -> exa_fetch the top 2-3 URLs -> synthesize (the everyday pattern)
2. exa_agent when the question needs multi-step reasoning a single search can't cover
3. websets only for entity collections you'll keep/enrich (lead-gen, not one-off lookup)
4. exa_batch only for >=50 same-shape requests in one call

Core search (5 tools):
- exa_search: primary entry; pass outputSchema in the same call for a synthesized
  answer alongside the result links
- exa_fetch: deep-read specific URLs/ids; prefer over re-searching
- exa_answer: fast, citation-grounded "just tell me" answers with sources
- exa_find_similar: more-like-this from a seed URL
- exa_agent: open-ended multi-source research; the slowest/most expensive —
  use when search+fetch fall short

Agent research, incl. chained runs (7 tools):
- exa_agent: run a research task; continue a prior run via previousRunId (its
  id) for chained/progressive refinement; feed records via data / exclusion;
  draw from Exa Connect providers via dataSources
- exa_agent_get / exa_agent_list: inspect a run's status/cost/output or find a
  run id to chain onto (use when exa_agent timed out)
- exa_agent_events: the ordered tool-call/reasoning stream (debug or stream progress)
- exa_agent_cancel: abort (discard in-flight work); exa_agent_stop: halt at the
  next checkpoint, keeping partial output; exa_agent_delete: drop a finished run

Websets — web-scale entity discovery (16 tools):
- exa_webset_preview: cheap decompose-preview (entity + criteria + sample items)
  before committing; exa_webset_create: paid, creates + polls to terminal
- exa_webset_get / exa_webset_items / exa_webset_list: read the corpus
- exa_webset_item_get / exa_webset_item_delete: inspect or drop one record
- exa_webset_add_search: refine/broaden; exa_webset_search_status /
  exa_webset_search_cancel: watch or stop one search
- exa_webset_enrich: add a per-item field; exa_enrichment_get /
  exa_enrichment_cancel / exa_enrichment_update / exa_enrichment_delete: manage it
- exa_webset_update / exa_webset_cancel / exa_webset_delete: manage/clean up

Bulk batches (5 tools):
- exa_batch_create: enqueue 50+ same-shape /search or /agent/runs calls in one
  server-side batch (one Exa request per sub-request); exa_batch_get with
  wait=true polls to terminal and returns results keyed by customId
- exa_batch_list / exa_batch_cancel / exa_batch_delete: manage batches

Search-change monitors (9 tools, api.exa.ai/monitors):
- exa_monitor_create: re-run a search on a period, deliver new/changed results to
  an HTTPS webhook; exa_monitor_trigger: force a run now
- exa_monitor_list / exa_monitor_get / exa_monitor_update / exa_monitor_delete:
  manage; exa_monitor_runs / exa_monitor_run_get: read delivered runs
- exa_monitor_batch: bulk delete/pause/unpause by filter (dry_run first)

Webset monitors (7 tools, cron-scheduled refresh of a webset):
- exa_webset_monitor_create: keep a webset fresh on a cron (at most once/day)
- exa_webset_monitor_list / _get / _update / _delete / _runs / _run_get

Imports (5 tools — bring your own CSV entity lists to seed/scope websets):
- exa_import_create: register the CSV (returns a short-lived uploadUrl to PUT to);
  exa_import_list / _get / _update / _delete: manage

Webhooks (6 tools — receive event notifications):
- exa_webhook_create: register an HTTPS endpoint for events (returns a signature
  secret); exa_webhook_list / _get / _update / _delete: manage
- exa_webhook_attempts: debug why an event didn't arrive

Events (2 tools — team audit log):
- exa_event_list (filter by type/time) / exa_event_get

Plan gating: /batches, Websets, monitors, imports, webhooks, and events are
plan-gated — a 403 (category 'plan') means the key lacks the add-on, not a bug.

Best practices:
- Recency: startPublishedDate / endPublishedDate (ISO 8601), not dates in the query
- Domain scoping: includeDomains / excludeDomains, not site: / -site: operators
- category=company|people returns entity data but rejects date filters and
  excludeDomains (400)
- Request only what you use — withText / withHighlights / withSummary each add
  cost; searchType auto for everyday, deep/deep-reasoning for hard questions
- Clean up what you create: websets, batches, monitors, imports, webhooks all
  have a _delete tool; probes use it so no residue is left behind

Resource patterns:
- exa-team: — the authenticated team/plan (check limits/credits)
- exa-content:///{url} — fetch + summarize a single URL

The server uses the authenticated API key's permissions for all operations.`,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
// Exa's `summary` field is a plain string in the public OpenAPI but the live
// API has occasionally returned `{ text: string }`; accept both.
function summaryText(s: any): string {
  if (typeof s === "string") return s;
  if (s && typeof s === "object") return s.text ?? "";
  return "";
}

// Highlights are an array of strings (OpenAPI) but have been seen as objects.
function firstHighlight(r: any): string {
  const h = r?.highlights;
  if (Array.isArray(h) && h.length) {
    const first = h[0];
    return typeof first === "string" ? first : first?.text ?? "";
  }
  return typeof h === "string" ? h : "";
}

function pick(args: any, key: string, type: "string" | "number" | "boolean" | "array" | "object"): any {
  const v = args?.[key];
  if (v === undefined || v === null || v === "") return undefined;
  switch (type) {
    case "string": return String(v);
    case "number": { const n = Number(v); return Number.isNaN(n) ? undefined : n; }
    case "boolean": return Boolean(v);
    case "array": return Array.isArray(v) ? v : [v];
    case "object": return v;
  }
}

// The SDK does not validate a tool's inputSchema on the server side, so a
// missing required field would otherwise be coerced to the literal "undefined"
// string and sent to the API. Reject required string fields explicitly.
function requiredStr(args: any, key: string, tool: string): string {
  const v = args?.[key];
  if (v === undefined || v === null || String(v).trim() === "") {
    throw new ExaApiError(`Tool ${tool} requires a non-empty '${key}' argument`, "bad_request");
  }
  return String(v);
}

// Validate an enum-like value before it goes on the wire, so the model gets a
// precise, actionable error instead of a raw API 400.
function pickEnum(args: any, key: string, allowed: readonly string[], tool: string): string | undefined {
  const v = pick(args, key, "string");
  if (v === undefined) return undefined;
  if (!(allowed as readonly string[]).includes(v)) {
    throw new ExaApiError(
      `Tool ${tool}: '${key}' must be one of: ${allowed.join(", ")} (got '${v}')`, "bad_request");
  }
  return v;
}

function pickIntInRange(args: any, key: string, min: number, max: number, tool: string): number | undefined {
  const v = pick(args, key, "number");
  if (v === undefined) return undefined;
  if (v < min || v > max || !Number.isInteger(v)) {
    throw new ExaApiError(`Tool ${tool}: '${key}' must be an integer between ${min} and ${max} (got ${v})`, "bad_request");
  }
  return v;
}

// The company/people categories reject date filters and excludeDomains.
function checkEntityCategoryCompatibility(
  category: string | undefined,
  startPublishedDate: string | undefined,
  endPublishedDate: string | undefined,
  excludeDomains: string[] | undefined,
  tool: string,
): void {
  if (category && ENTITY_CATEGORIES.has(category) && (startPublishedDate || endPublishedDate || excludeDomains)) {
    throw new ExaApiError(
      `Tool ${tool}: category '${category}' does not support startPublishedDate, ` +
      `endPublishedDate, or excludeDomains — the Exa API returns 400 for these. ` +
      `Drop the date/domain filter or use a different category.`,
      "bad_request");
  }
}

interface ApiMetricsMeta {
  apiMetrics: {
    requestsInLastHour: number;
    remainingRequests: number;
    averageRequestTime: string;
    queueLength: number;
  };
}

// Compact rendering of a /contents response: keep what the model needs, cap
// the text, and never dump the raw blob into the main channel.
function renderContents(data: any): string {
  const results: any[] = Array.isArray(data?.results) ? data.results : (data?.results ? [data.results] : [data]);
  const lines = results.map((r, i) => {
    const out: any = {};
    if (r.url) out.url = r.url;
    if (r.title) out.title = r.title;
    if (r.publishedDate) out.publishedDate = r.publishedDate;
    const s = summaryText(r.summary);
    if (s) out.summary = s;
    const h = r.highlights;
    if (Array.isArray(h) && h.length) out.highlights = h.map((x) => (typeof x === "string" ? x : x?.text ?? "")).filter(Boolean);
    if (r.extras && typeof r.extras === "object") {
      const e: any = {};
      if (r.extras.links?.length) e.links = r.extras.links;
      if (r.extras.imageLinks?.length) e.imageLinks = r.extras.imageLinks;
      if (Object.keys(e).length) out.extras = e;
    }
    const text = typeof r.text === "string" ? r.text : "";
    if (text) {
      const cap = 6000;
      out.text = text.length > cap ? `${text.slice(0, cap)} …[truncated, ${text.length - cap} more chars]` : text;
    }
    return `${i + 1}. ${JSON.stringify(out)}`;
  });
  return `Fetched ${results.length} page(s):\n${lines.join("\n")}`;
}

// Compact rendering of an agent run snapshot: status, stop reason, cost,
// output text (capped), and deduplicated source citations.
function renderAgentRun(run: any): string {
  if (!run || typeof run !== "object") return "No run data.";
  const cost = run?.costDollars?.total != null ? ` · cost $${Number(run.costDollars.total).toFixed(4)}` : "";
  const stop = run?.stopReason ? ` · stop: ${run.stopReason}` : "";
  const text = typeof run?.output?.text === "string" ? run.output.text : (run?.output?.text ? JSON.stringify(run.output.text) : "");
  const cap = 4000;
  const body = text ? (text.length > cap ? `${text.slice(0, cap)} …[truncated]` : text) : "(no output text)";
  const grounding: any[] = run?.output?.grounding ?? [];
  const cites = new Map<string, string>();
  for (const g of grounding) for (const c of g.citations ?? []) if (c?.url && !cites.has(c.url)) cites.set(c.url, c.title ?? "");
  const srcLines = [...cites.entries()].map(([u, t], i) => `  ${i + 1}. ${t} — ${u}`).join("\n");
  return `Agent run ${run.id ?? "?"} (status: ${run.status ?? "?"}${stop}${cost})\n\n${body}\nSources:\n${srcLines || "  (none)"}`;
}

// Compact rendering of a webset item list: one line per item (entity type,
// name, short description, first satisfied criterion), capped at 40 items.
function renderWebsetItems(data: any): string {
  const items: any[] = Array.isArray(data?.data) ? data.data : [];
  const cap = 40;
  const lines = items.slice(0, cap).map((it, i) => {
    const p = it?.properties ?? {};
    const type = p.type ?? "unknown";
    let name = "";
    if (type === "company") name = (p.company?.name) ?? p.url ?? "";
    else if (type === "person") {
      const pe = p.person ?? {};
      name = pe.name ?? `${pe.firstName ?? ""} ${pe.lastName ?? ""}`.trim() ?? p.url ?? "";
    } else if (type === "article") name = p.article?.title ?? p.url ?? "";
    else if (type === "research_paper") name = p.researchPaper?.title ?? p.url ?? "";
    else if (type === "custom") name = p.custom?.title ?? p.url ?? "";
    else name = p.url ?? it?.id ?? "";
    const desc = p.description ? ` — ${String(p.description).slice(0, 160)}` : "";
    const ev = (it?.evaluations ?? []).find((e: any) => e?.satisfied === "yes");
    const crit = ev?.criterion ? ` [${String(ev.criterion).slice(0, 80)}]` : "";
    return `${i + 1}. [${type}] ${name}${desc}${crit}`;
  });
  const total = data?.count ?? items.length;
  const more = items.length > cap ? `\n…${items.length - cap} more (use cursor to page)` : "";
  return `Webset items (total ${total}, showing ${Math.min(items.length, cap)}):\n${lines.join("\n")}${more}`;
}

// Compact rendering of a paged list payload: one line per record (id, status,
// plus a couple of type-specific fields) with the pagination hint.
function renderList(data: any, label: string, pick: (row: any) => string, cap = 40): string {
  const rows: any[] = Array.isArray(data?.data) ? data.data : (Array.isArray(data) ? data : []);
  const lines = rows.slice(0, cap).map((r, i) => `${i + 1}. ${pick(r)}`);
  const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor ?? "?"})` : "";
  const extra = rows.length > cap ? `\n…${rows.length - cap} more` : "";
  return `${rows.length} ${label}:\n${lines.join("\n") || "  (none)"}${extra}${more}`;
}

// Render a single-object payload (monitor run, enrichment, webhook, event, ...)
// compactly: stable key order, capped so a huge blob never floods the channel.
function renderObject(data: any, label: string): string {
  if (data === undefined || data === null) return `${label}: (empty)`;
  const json = JSON.stringify(data, null, 2);
  const cap = 4000;
  return `${label}:\n${json.length > cap ? json.slice(0, cap) + "\n…[truncated]" : json}`;
}

// Render a monitor record (search-change or webset monitor).
function renderMonitor(m: any): string {
  if (!m || typeof m !== "object") return "(no monitor data)";
  const bits = [`[status: ${m.status ?? "?"}] ${m.id ?? "?"}`];
  if (m.name) bits.push(`name: ${m.name}`);
  if (m.websetId) bits.push(`webset: ${m.websetId}`);
  const cadence = m.cadence ?? m.trigger;
  if (cadence) {
    const cron = cadence.cron ?? cadence.period;
    if (cron) bits.push(`every: ${cron}`);
  }
  if (m.query) bits.push(`query: ${String(m.query).slice(0, 80)}`);
  if (m.webhook?.url) bits.push(`webhook: ${m.webhook.url}`);
  return bits.join(" · ");
}

// Render a monitor run (status + short output preview).
function renderMonitorRun(run: any): string {
  if (!run || typeof run !== "object") return "(no run data)";
  const status = run.status ?? "?";
  let out = "";
  const o = run.output ?? run.result;
  if (o !== undefined) {
    const s = typeof o === "string" ? o : JSON.stringify(o);
    out = `\noutput: ${s.slice(0, 600)}${s.length > 600 ? " …[truncated]" : ""}`;
  }
  const at = run.createdAt ?? run.finishedAt;
  return `Run ${run.id ?? "?"} (status: ${status}${at ? `, ${at}` : ""})${out}`;
}

async function main() {
  const apiKey = process.env.EXA_API_KEY;
  if (!apiKey) {
    console.error("EXA_API_KEY environment variable is required");
    process.exit(1);
  }

  console.error("Starting Exa MCP Server...");
  const client = new ExaClient(apiKey);

  const server = new Server(
    { name: "exa-mcp-server", version: "1.2.0" },
    {
      capabilities: {
        prompts: { default: serverPrompt },
        resources: { templates: true, read: true },
        tools: {},
      },
    },
  );

  const metricsMeta = (): ApiMetricsMeta => {
    const m = client.rateLimiter.getMetrics();
    return {
      apiMetrics: {
        requestsInLastHour: m.requestsInLastHour,
        remainingRequests: client.rateLimiter.requestsPerHour - m.requestsInLastHour,
        averageRequestTime: `${Math.round(m.averageRequestTime)}ms`,
        queueLength: m.queueLength,
      },
    };
  };

  // Read resources
  server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
    const uri = request.params.uri;
    if (uri === "exa-team:") {
      const team = await client.team();
      return {
        contents: [{ uri, mimeType: "application/json", text: JSON.stringify(team, null, 2) }],
      };
    }
    if (uri.startsWith("exa-content:///")) {
      // Template is `exa-content:///{url}`; strip the full `exa-content:///` prefix.
      let url: string;
      try {
        url = decodeURIComponent(uri.slice("exa-content:///".length));
      } catch {
        throw new Error(`exa-content resource: URL was not valid percent-encoding: ${uri}`);
      }
      const data = await client.fetch({ urls: [url], withText: true, withHighlights: true, withSummary: true });
      return {
        contents: [{
          uri,
          mimeType: "application/json",
          text: renderContents(data),
        }],
      };
    }
    throw new Error(`Unsupported resource URI: ${uri}`);
  });

  server.setRequestHandler(ListResourcesRequestSchema, async () => ({
    resources: [
      { uri: "exa-team:", name: "Exa Team", mimeType: "application/json" },
    ],
  }));

  server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => ({
    resourceTemplates,
  }));

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [
      exaSearchTool, exaFetchTool, exaAnswerTool, exaAgentTool, exaFindSimilarTool,
      exaAgentGetTool, exaAgentListTool, exaAgentCancelTool, exaAgentStopTool, exaAgentEventsTool,
      exaAgentDeleteTool,
      exaBatchCreateTool, exaBatchGetTool, exaBatchListTool, exaBatchCancelTool, exaBatchDeleteTool,
      exaMonitorCreateTool, exaMonitorListTool, exaMonitorGetTool, exaMonitorUpdateTool,
      exaMonitorDeleteTool, exaMonitorTriggerTool, exaMonitorRunsTool, exaMonitorRunGetTool,
      exaMonitorBatchTool,
      exaWebsetMonitorCreateTool, exaWebsetMonitorListTool, exaWebsetMonitorGetTool,
      exaWebsetMonitorUpdateTool, exaWebsetMonitorDeleteTool, exaWebsetMonitorRunsTool,
      exaWebsetMonitorRunGetTool,
      exaWebsetCreateTool, exaWebsetPreviewTool, exaWebsetGetTool, exaWebsetListTool,
      exaWebsetItemsTool, exaWebsetItemGetTool, exaWebsetItemDeleteTool,
      exaWebsetAddSearchTool, exaWebsetSearchStatusTool, exaWebsetSearchCancelTool,
      exaWebsetEnrichTool, exaEnrichmentGetTool, exaEnrichmentCancelTool, exaEnrichmentUpdateTool,
      exaEnrichmentDeleteTool,
      exaWebsetUpdateTool, exaWebsetCancelTool, exaWebsetDeleteTool,
      exaImportCreateTool, exaImportListTool, exaImportGetTool, exaImportUpdateTool,
      exaImportDeleteTool,
      exaWebhookCreateTool, exaWebhookListTool, exaWebhookGetTool, exaWebhookUpdateTool,
      exaWebhookDeleteTool, exaWebhookAttemptsTool,
      exaEventListTool, exaEventGetTool,
    ],
  }));

  server.setRequestHandler(ListPromptsRequestSchema, async () => ({
    prompts: [serverPrompt],
  }));

  server.setRequestHandler(GetPromptRequestSchema, async (request) => {
    if (request.params.name === serverPrompt.name) return { prompt: serverPrompt };
    throw new Error(`Prompt not found: ${request.params.name}`);
  });

  server.setRequestHandler(CallToolRequestSchema, async (request: CallToolRequest) => {
    const { name, arguments: args = {} } = request.params;
    const meta = metricsMeta();
    try {
      switch (name) {
        case "exa_search": {
          const a: ExaSearchArgs = {
            query: requiredStr(args, "query", "exa_search"),
            numResults: pickIntInRange(args, "numResults", 1, 100, "exa_search"),
            category: pick(args, "category", "string"),
            searchType: pickEnum(args, "searchType", SEARCH_TYPES, "exa_search"),
            startPublishedDate: pick(args, "startPublishedDate", "string"),
            endPublishedDate: pick(args, "endPublishedDate", "string"),
            includeDomains: pick(args, "includeDomains", "array"),
            excludeDomains: pick(args, "excludeDomains", "array"),
            userLocation: pick(args, "userLocation", "string"),
            withHighlights: pick(args, "withHighlights", "boolean"),
            highlightsQuery: pick(args, "highlightsQuery", "string"),
            withText: pick(args, "withText", "boolean"),
            textMaxCharacters: pickIntInRange(args, "textMaxCharacters", 1, 10000, "exa_search"),
            textVerbosity: pickEnum(args, "textVerbosity", TEXT_VERBOSITIES, "exa_search"),
            withSummary: pick(args, "withSummary", "boolean"),
            summaryQuery: pick(args, "summaryQuery", "string"),
            links: pickIntInRange(args, "links", 0, 1000, "exa_search"),
            imageLinks: pickIntInRange(args, "imageLinks", 0, 1000, "exa_search"),
            outputSchema: pick(args, "outputSchema", "object"),
          };
          checkEntityCategoryCompatibility(a.category, a.startPublishedDate, a.endPublishedDate, a.excludeDomains, "exa_search");
          const data = await client.search(a);
          const results: any[] = data?.results ?? [];
          const lines = results.map((r, i) => {
            const bits = [`${i + 1}. ${r.title || "(untitled)"}`, `   ${r.url || ""}`];
            if (r.publishedDate) bits.push(`   published: ${r.publishedDate}`);
            const s = summaryText(r.summary);
            if (s) bits.push(`   ${s.slice(0, 240)}`);
            const h = firstHighlight(r);
            if (h && s !== h) bits.push(`   ${h.slice(0, 200)}`);
            return bits.join("\n");
          });
          let answer = "";
          if (data?.output?.content) {
            answer = `\n\n--- synthesized answer ---\n${typeof data.output.content === "string"
              ? data.output.content : JSON.stringify(data.output.content)}`;
          }
          return {
            content: [{
              type: "text",
              text: `Found ${results.length} result(s):\n${lines.join("\n")}${answer}`,
              metadata: { ...meta, raw: data, costDollars: data?.costDollars },
            }],
          };
        }
        case "exa_fetch": {
          const a: ExaFetchArgs = {
            urls: pick(args, "urls", "array"),
            ids: pick(args, "ids", "array"),
            withText: pick(args, "withText", "boolean"),
            textVerbosity: pickEnum(args, "textVerbosity", TEXT_VERBOSITIES, "exa_fetch"),
            textMaxCharacters: pickIntInRange(args, "textMaxCharacters", 1, 10000, "exa_fetch"),
            withHighlights: pick(args, "withHighlights", "boolean"),
            highlightsQuery: pick(args, "highlightsQuery", "string"),
            withSummary: pick(args, "withSummary", "boolean"),
            summaryQuery: pick(args, "summaryQuery", "string"),
            links: pickIntInRange(args, "links", 0, 1000, "exa_fetch"),
            imageLinks: pickIntInRange(args, "imageLinks", 0, 1000, "exa_fetch"),
            subpages: pickIntInRange(args, "subpages", 0, 100, "exa_fetch"),
            subpageTarget: pick(args, "subpageTarget", "string"),
            maxAgeHours: pick(args, "maxAgeHours", "number"),
          };
          if (!a.urls && !a.ids) {
            throw new ExaApiError("exa_fetch needs either urls or ids", "bad_request");
          }
          const data = await client.fetch(a);
          return {
            content: [{ type: "text", text: renderContents(data), metadata: { ...meta, raw: data, costDollars: data?.costDollars } }],
          };
        }
        case "exa_answer": {
          const a: ExaAnswerArgs = {
            query: requiredStr(args, "query", "exa_answer"),
            text: pick(args, "text", "boolean"),
            outputSchema: pick(args, "outputSchema", "object"),
            systemPrompt: pick(args, "systemPrompt", "string"),
            userLocation: pick(args, "userLocation", "string"),
          };
          const data = await client.answer(a);
          const cits: any[] = data?.citations ?? [];
          const srcLines = cits.map((c, i) => `  ${i + 1}. ${c.title ?? "(untitled)"} — ${c.url ?? ""}`).join("\n");
          return {
            content: [{
              type: "text",
              text: `${typeof data?.answer === "string" ? data.answer : JSON.stringify(data?.answer)}\n\nSources:\n${srcLines}`,
              metadata: { ...meta, raw: data, costDollars: data?.costDollars },
            }],
          };
        }
        case "exa_agent": {
          const a: ExaAgentArgs = {
            query: requiredStr(args, "query", "exa_agent"),
            effort: pickEnum(args, "effort", AGENT_EFFORTS, "exa_agent"),
            systemPrompt: pick(args, "systemPrompt", "string"),
            outputSchema: pick(args, "outputSchema", "object"),
            maxCostDollars: pickIntInRange(args, "maxCostDollars", 1, 100, "exa_agent"),
            timeout: pick(args, "timeout", "number"),
            previousRunId: pick(args, "previousRunId", "string"),
            metadata: pick(args, "metadata", "object"),
            data: pick(args, "data", "array"),
            exclusion: pick(args, "exclusion", "array"),
            dataSources: pick(args, "dataSources", "array"),
          };
          const run = await client.agent(a);
          const grounding: any[] = run?.output?.grounding ?? [];
          const cites = new Map<string, string>();
          for (const g of grounding) for (const c of g.citations ?? []) if (c.url && !cites.has(c.url)) cites.set(c.url, c.title ?? "");
          const srcLines = [...cites.entries()].map(([u, t], i) => `  ${i + 1}. ${t} — ${u}`).join("\n");
          const structured = run?.output?.structured ? `\n\nStructured:\n${JSON.stringify(run.output.structured, null, 2)}\n` : "";
          return {
            content: [{
              type: "text",
              text: `Agent run ${run?.id} (${run?.status})${run?.costDollars?.total ? ` · cost $${run.costDollars.total.toFixed(4)}` : ""}\n\n${run?.output?.text ?? ""}${structured}\nSources:\n${srcLines}`,
              metadata: { ...meta, raw: run },
            }],
          };
        }
        case "exa_find_similar": {
          const a: ExaFindSimilarArgs = {
            url: requiredStr(args, "url", "exa_find_similar"),
            numResults: pickIntInRange(args, "numResults", 1, 100, "exa_find_similar"),
            category: pick(args, "category", "string"),
            excludeSourceDomain: pick(args, "excludeSourceDomain", "boolean"),
            includeDomains: pick(args, "includeDomains", "array"),
            excludeDomains: pick(args, "excludeDomains", "array"),
            startPublishedDate: pick(args, "startPublishedDate", "string"),
            endPublishedDate: pick(args, "endPublishedDate", "string"),
          };
          const data = await client.findSimilar(a);
          const results: any[] = data?.results ?? [];
          const lines = results.map((r, i) => `${i + 1}. ${r.title ?? "(untitled)"}\n   ${r.url ?? ""}`).join("\n");
          return {
            content: [{
              type: "text",
              text: `${results.length} similar page(s):\n${lines}`,
              metadata: { ...meta, raw: data },
            }],
          };
        }
        case "exa_agent_get": {
          const runId = requiredStr(args, "runId", "exa_agent_get");
          const run = await client.agentGet(runId);
          return {
            content: [{ type: "text", text: renderAgentRun(run), metadata: { ...meta, raw: run } }],
          };
        }
        case "exa_agent_list": {
          const data = await client.agentList({
            status: pick(args, "status", "string"),
            limit: pickIntInRange(args, "limit", 1, 100, "exa_agent_list"),
            cursor: pick(args, "cursor", "string"),
          });
          const runs: any[] = data?.data ?? [];
          const lines = runs.map((r, i) =>
            `${i + 1}. [${r.status ?? "?"}] ${r.id}\n` +
            `   query: ${String(r.query ?? "").slice(0, 80)}` +
            `${r.costDollars?.total != null ? `\n   cost: $${Number(r.costDollars.total).toFixed(4)}` : ""}`).join("\n");
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${runs.length} agent run(s):\n${lines}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_agent_cancel": {
          const runId = requiredStr(args, "runId", "exa_agent_cancel");
          const run = await client.agentCancel(runId);
          return {
            content: [{
              type: "text",
              text: `Cancellation requested for ${runId}${run?.status ? ` (status: ${run.status})` : ""}.`,
              metadata: { ...meta, raw: run },
            }],
          };
        }
        case "exa_agent_stop": {
          const runId = requiredStr(args, "runId", "exa_agent_stop");
          const reason = pick(args, "reason", "string") ?? "stopped";
          const run = await client.agentStop(runId, reason);
          return {
            content: [{
              type: "text",
              text: `Stop (${reason}) applied to ${runId}${run?.status ? ` (status: ${run.status})` : ""}. Partial output retained.`,
              metadata: { ...meta, raw: run },
            }],
          };
        }
        case "exa_agent_events": {
          const runId = requiredStr(args, "runId", "exa_agent_events");
          const data = await client.agentEvents(runId, {
            limit: pickIntInRange(args, "limit", 1, 500, "exa_agent_events"),
            cursor: pick(args, "cursor", "string"),
          });
          const events: any[] = data?.data ?? [];
          const lines = events.map((e, i) => {
            const type = e?.type ?? e?.eventType ?? "event";
            const label = e?.name ?? e?.label ?? e?.title ?? "";
            const detail = e?.message ?? e?.content ?? e?.data?.message ?? "";
            const s = typeof detail === "string" ? detail : JSON.stringify(detail ?? "");
            return `${i + 1}. ${type}${label ? `: ${label}` : ""}${s ? ` — ${s.slice(0, 200)}` : ""}`;
          }).join("\n");
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${events.length} event(s) for ${runId}:\n${lines}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_batch_create": {
          const requests = pick(args, "requests", "array");
          if (!Array.isArray(requests) || !requests.length) {
            throw new ExaApiError("exa_batch_create requires a non-empty 'requests' array", "bad_request");
          }
          const metadata = pick(args, "metadata", "object");
          const batch = await client.batchCreate(requests, metadata);
          return {
            content: [{
              type: "text",
              text: `Created batch ${batch?.id} (status: ${batch?.status ?? "?"}, counts: ${JSON.stringify(batch?.requestCounts ?? {})}). ` +
                `Call exa_batch_get with wait=true to collect the results.`,
              metadata: { ...meta, raw: batch, batchId: batch?.id },
            }],
          };
        }
        case "exa_batch_get": {
          const batchId = requiredStr(args, "batchId", "exa_batch_get");
          const wait = pick(args, "wait", "boolean");
          const timeoutSec = pickIntInRange(args, "timeout", 5, 600, "exa_batch_get") ?? 150;
          let payload: any = null;
          if (wait) {
            payload = await client.batchPoll(batchId, timeoutSec);
          } else {
            const snap = await client.batchGet(batchId);
            payload = { batch: snap, results: {}, results_lines: [], results_url: snap?.resultsUrl };
          }
          const counts = (payload as any)?.batch?.requestCounts ?? {};
          const resultsMap: Record<string, any> = (payload as any)?.results ?? {};
          const resultLines = Object.entries(resultsMap).slice(0, 25).map(([cid, r]) => {
            const anyR = r as any;
            const summary = (typeof anyR?.body === "object" && anyR.body) ? "result" : "error";
            return `  - ${cid}: ${summary}`;
          }).join("\n");
          return {
            content: [{
              type: "text",
              text: `Batch ${batchId} (status: ${payload.batch?.status ?? "?"}, counts: ${JSON.stringify(counts)})` +
                `${payload.results_url ? `\nresultsUrl: ${payload.results_url}` : ""}` +
                (Object.keys(payload.results ?? {}).length ? `\nResults by customId (showing up to 25):\n${resultLines}` : ""),
              metadata: { ...meta, raw: payload, results: payload.results, resultsUrl: payload.results_url },
            }],
          };
        }
        case "exa_webset_create": {
          const a: ExaWebsetCreateArgs = {
            query: requiredStr(args, "query", "exa_webset_create"),
            count: pickIntInRange(args, "count", 1, 1000, "exa_webset_create") ?? 10,
            title: pick(args, "title", "string"),
            externalId: pick(args, "externalId", "string"),
            entity: pick(args, "entity", "object"),
            criteria: pick(args, "criteria", "array"),
            exclude: pick(args, "exclude", "array"),
            scope: pick(args, "scope", "array"),
            recall: pick(args, "recall", "boolean"),
            maxPeoplePerCompany: pickIntInRange(args, "maxPeoplePerCompany", 1, 100, "exa_webset_create"),
            metadata: pick(args, "metadata", "object"),
            enrichments: pick(args, "enrichments", "array"),
            imports: pick(args, "imports", "array"),
            timeout: pick(args, "timeout", "number"),
          };
          const ws = await client.websetCreate(a);
          const searches: any[] = ws?.searches ?? [];
          const found = (ws?.items ?? []).length;
          return {
            content: [{
              type: "text",
              text: `Webset ${ws?.id} created (status: ${ws?.status ?? "?"}, searches: ${searches.map((s) => `${s.id}[${s.status ?? "?"}]`).join(", ") || "none"})` +
                `${found ? `, ${found} item(s) so far` : ""}. Use exa_webset_items to read the corpus, exa_webset_delete to clean up.`,
              metadata: { ...meta, raw: ws, websetId: ws?.id },
            }],
          };
        }
        case "exa_webset_preview": {
          const data = await client.websetPreview({
            query: requiredStr(args, "query", "exa_webset_preview"),
            entity: pick(args, "entity", "object"),
            count: pickIntInRange(args, "count", 1, 10, "exa_webset_preview"),
          });
          const search = data?.search ?? data;
          const entity = search?.entity?.type ?? data?.entity?.type ?? "(auto)";
          const criteria = (search?.criteria ?? []).map((c: any) => c?.description ?? JSON.stringify(c)).join("; ");
          const previewItems: any[] = data?.items ?? [];
          const lines = previewItems.map((it, i) => `  ${i + 1}. ${it?.name ?? it?.properties?.company?.name ?? it?.properties?.person?.name ?? it?.url ?? JSON.stringify(it).slice(0, 120)}`).join("\n");
          return {
            content: [{
              type: "text",
              text: `Preview — entity: ${entity}; criteria: ${criteria || "(none)"}${lines ? `\nSample items (${previewItems.length}):\n${lines}` : ""}`,
              metadata: { ...meta, raw: data },
            }],
          };
        }
        case "exa_webset_get": {
          const id = requiredStr(args, "websetId", "exa_webset_get");
          const ws = await client.websetGet(id, {
            includeItems: pick(args, "includeItems", "boolean"),
            wait: pick(args, "wait", "boolean"),
            timeoutSec: pickIntInRange(args, "timeout", 5, 600, "exa_webset_get"),
          });
          const searches: any[] = ws?.searches ?? [];
          const lines = searches.map((s) => `  - ${s.id}[${s.status ?? "?"}] ${String(s.query ?? "").slice(0, 60)}`).join("\n");
          const body = ws?.items ? `\n${renderWebsetItems({ data: ws.items, count: ws.items.length })}` : "";
          return {
            content: [{
              type: "text",
              text: `Webset ${ws?.id} (status: ${ws?.status ?? "?"}, title: ${ws?.title ?? "-"})\nSearches:\n${lines || "  (none)"}${body}`,
              metadata: { ...meta, raw: ws, websetId: ws?.id },
            }],
          };
        }
        case "exa_webset_list": {
          const data = await client.websetList({
            limit: pickIntInRange(args, "limit", 1, 100, "exa_webset_list"),
            cursor: pick(args, "cursor", "string"),
          });
          const websets: any[] = data?.data ?? [];
          const lines = websets.map((w, i) =>
            `${i + 1}. [${w.status ?? "?"}] ${w.id} ${w.title ? `— ${w.title}` : ""}\n` +
            `   entity: ${(w.entity?.type ?? w.searches?.[0]?.entity?.type) ?? "?"}`).join("\n");
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${websets.length} webset(s):\n${lines || "  (none)"}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_webset_items": {
          const id = requiredStr(args, "websetId", "exa_webset_items");
          const data = await client.websetItems(id, {
            limit: pickIntInRange(args, "limit", 1, 100, "exa_webset_items") ?? 100,
            cursor: pick(args, "cursor", "string"),
            sourceId: pick(args, "sourceId", "string"),
          });
          return {
            content: [{
              type: "text",
              text: renderWebsetItems(data),
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_webset_add_search": {
          const id = requiredStr(args, "websetId", "exa_webset_add_search");
          const s = await client.websetAddSearch(id, {
            query: requiredStr(args, "query", "exa_webset_add_search"),
            count: pickIntInRange(args, "count", 1, 1000, "exa_webset_add_search") ?? 10,
            entity: pick(args, "entity", "object"),
            criteria: pick(args, "criteria", "array"),
            exclude: pick(args, "exclude", "array"),
            scope: pick(args, "scope", "array"),
            recall: pick(args, "recall", "boolean"),
            maxPeoplePerCompany: pickIntInRange(args, "maxPeoplePerCompany", 1, 100, "exa_webset_add_search"),
            behavior: pickEnum(args, "behavior", WEBSET_BEHAVIORS, "exa_webset_add_search"),
            metadata: pick(args, "metadata", "object"),
          });
          return {
            content: [{
              type: "text",
              text: `Added search ${s?.id ?? "(created)"} to webset ${id} (status: ${s?.status ?? "?"}). Poll with exa_webset_get(websetId, wait=true).`,
              metadata: { ...meta, raw: s },
            }],
          };
        }
        case "exa_webset_enrich": {
          const id = requiredStr(args, "websetId", "exa_webset_enrich");
          const e = await client.websetEnrich(id, {
            description: requiredStr(args, "description", "exa_webset_enrich"),
            format: pickEnum(args, "format", WEBSET_ENRICH_FORMATS, "exa_webset_enrich"),
            options: pick(args, "options", "array"),
            metadata: pick(args, "metadata", "object"),
          });
          return {
            content: [{
              type: "text",
              text: `Enrichment ${e?.id ?? "(created)"} queued on webset ${id} (status: ${e?.status ?? "?"}). Read results via exa_webset_items.`,
              metadata: { ...meta, raw: e },
            }],
          };
        }
        case "exa_webset_update": {
          const id = requiredStr(args, "websetId", "exa_webset_update");
          const title = pick(args, "title", "string");
          const metadata = pick(args, "metadata", "object");
          if (title === undefined && metadata === undefined) {
            throw new ExaApiError("exa_webset_update needs at least one of title or metadata", "bad_request");
          }
          const ws = await client.websetUpdate(id, { title, metadata });
          return {
            content: [{
              type: "text",
              text: `Updated webset ${id}${ws?.title ? ` (title: ${ws.title})` : ""}.`,
              metadata: { ...meta, raw: ws },
            }],
          };
        }
        case "exa_webset_cancel": {
          const id = requiredStr(args, "websetId", "exa_webset_cancel");
          const ws = await client.websetCancel(id);
          return {
            content: [{
              type: "text",
              text: `Cancellation requested for webset ${id}${ws?.status ? ` (status: ${ws.status})` : ""}.`,
              metadata: { ...meta, raw: ws },
            }],
          };
        }
        case "exa_webset_delete": {
          const id = requiredStr(args, "websetId", "exa_webset_delete");
          const res = await client.websetDelete(id);
          return {
            content: [{
              type: "text",
              text: `Deleted webset ${id}.`,
              metadata: { ...meta, raw: res },
            }],
          };
        }
        case "exa_batch_list": {
          const data = await client.batchList({
            limit: pickIntInRange(args, "limit", 1, 100, "exa_batch_list"),
            cursor: pick(args, "cursor", "string"),
          });
          const rows: any[] = data?.data ?? [];
          const lines = rows.map((r, i) =>
            `${i + 1}. [${r.status ?? "?"}] ${r.id} (counts: ${JSON.stringify(r.requestCounts ?? {})})`);
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${rows.length} batch(es):\n${lines.join("\n") || "  (none)"}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_batch_cancel": {
          const batchId = requiredStr(args, "batchId", "exa_batch_cancel");
          const b = await client.batchCancel(batchId);
          return {
            content: [{
              type: "text",
              text: `Cancellation requested for batch ${batchId} (status: ${b?.status ?? "?"}).`,
              metadata: { ...meta, raw: b },
            }],
          };
        }
        case "exa_batch_delete": {
          const batchId = requiredStr(args, "batchId", "exa_batch_delete");
          const res = await client.batchDelete(batchId);
          return {
            content: [{ type: "text", text: `Deleted batch ${batchId}.`, metadata: { ...meta, raw: res } }],
          };
        }
        case "exa_agent_delete": {
          const runId = requiredStr(args, "runId", "exa_agent_delete");
          const res = await client.agentDelete(runId);
          return {
            content: [{ type: "text", text: `Deleted agent run ${runId}.`, metadata: { ...meta, raw: res } }],
          };
        }
        case "exa_monitor_create": {
          const m = await client.monitorCreate({
            query: requiredStr(args, "query", "exa_monitor_create"),
            webhookUrl: requiredStr(args, "webhookUrl", "exa_monitor_create"),
            name: pick(args, "name", "string"),
            period: pick(args, "period", "string"),
            numResults: pickIntInRange(args, "numResults", 1, 100, "exa_monitor_create"),
            outputSchema: pick(args, "outputSchema", "object"),
            events: pick(args, "events", "array"),
            metadata: pick(args, "metadata", "object"),
            search: pick(args, "search", "object"),
          });
          return {
            content: [{
              type: "text",
              text: `Monitor ${m?.id ?? "(created)"} (status: ${m?.status ?? "?"})${m?.webhookSecret ? " · webhook secret returned" : ""}.`,
              metadata: { ...meta, raw: m, monitorId: m?.id },
            }],
          };
        }
        case "exa_monitor_list": {
          const data = await client.monitorList({
            status: pickEnum(args, "status", MONITOR_STATUSES, "exa_monitor_list"),
            name: pick(args, "name", "string"),
            metadata: pick(args, "metadata", "object"),
            limit: pickIntInRange(args, "limit", 1, 500, "exa_monitor_list"),
            cursor: pick(args, "cursor", "string"),
          });
          return {
            content: [{
              type: "text",
              text: renderList(data, "monitor", renderMonitor, 50),
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_monitor_get": {
          const m = await client.monitorGet(requiredStr(args, "monitorId", "exa_monitor_get"));
          return {
            content: [{ type: "text", text: renderMonitor(m), metadata: { ...meta, raw: m } }],
          };
        }
        case "exa_monitor_update": {
          const monitorId = requiredStr(args, "monitorId", "exa_monitor_update");
          const body: any = {};
          for (const k of ["search", "trigger", "webhook", "metadata", "status"]) {
            const v = pick(args, k, k === "status" ? "string" : "object");
            if (v !== undefined) body[k] = v;
          }
          if (!Object.keys(body).length) {
            throw new ExaApiError("exa_monitor_update needs at least one field (search|trigger|webhook|metadata|status)", "bad_request");
          }
          const m = await client.monitorUpdate(monitorId, body);
          return {
            content: [{ type: "text", text: `Updated monitor ${monitorId}: ${renderMonitor(m ?? body)}`, metadata: { ...meta, raw: m } }],
          };
        }
        case "exa_monitor_delete": {
          const monitorId = requiredStr(args, "monitorId", "exa_monitor_delete");
          const res = await client.monitorDelete(monitorId);
          return {
            content: [{ type: "text", text: `Deleted monitor ${monitorId}.`, metadata: { ...meta, raw: res } }],
          };
        }
        case "exa_monitor_trigger": {
          const monitorId = requiredStr(args, "monitorId", "exa_monitor_trigger");
          const r = await client.monitorTrigger(monitorId);
          return {
            content: [{
              type: "text",
              text: `Triggered a run on monitor ${monitorId}${r?.id ? ` (run ${r.id})` : ""}${r?.status ? ` (status: ${r.status})` : ""}.`,
              metadata: { ...meta, raw: r },
            }],
          };
        }
        case "exa_monitor_runs": {
          const monitorId = requiredStr(args, "monitorId", "exa_monitor_runs");
          const data = await client.monitorRuns(monitorId, {
            limit: pickIntInRange(args, "limit", 1, 500, "exa_monitor_runs"),
            cursor: pick(args, "cursor", "string"),
          });
          const rows: any[] = data?.data ?? [];
          const lines = rows.map((r, i) => `${i + 1}. [${r.status ?? "?"}] ${r.id}${r.createdAt ? ` (${r.createdAt})` : ""}`);
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${rows.length} run(s) for monitor ${monitorId}:\n${lines.join("\n") || "  (none)"}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_monitor_run_get": {
          const monitorId = requiredStr(args, "monitorId", "exa_monitor_run_get");
          const runId = requiredStr(args, "runId", "exa_monitor_run_get");
          const run = await client.monitorRunGet(monitorId, runId);
          return {
            content: [{ type: "text", text: renderMonitorRun(run), metadata: { ...meta, raw: run } }],
          };
        }
        case "exa_monitor_batch": {
          const data = await client.monitorBatch({
            action: pickEnum(args, "action", MONITOR_BATCH_ACTIONS, "exa_monitor_batch") ?? "delete",
            name: pick(args, "name", "string"),
            status: pickEnum(args, "status", MONITOR_STATUSES, "exa_monitor_batch"),
            metadata: pick(args, "metadata", "object"),
            dryRun: pick(args, "dryRun", "boolean") ?? true,
            limit: pickIntInRange(args, "limit", 1, 500, "exa_monitor_batch"),
          });
          return {
            content: [{ type: "text", text: renderObject(data, "Monitor batch result"), metadata: { ...meta, raw: data } }],
          };
        }
        case "exa_webset_monitor_create": {
          const m = await client.websetMonitorCreate({
            websetId: requiredStr(args, "websetId", "exa_webset_monitor_create"),
            cron: requiredStr(args, "cron", "exa_webset_monitor_create"),
            timezone: pick(args, "timezone", "string"),
            count: pickIntInRange(args, "count", 1, 1000, "exa_webset_monitor_create"),
            query: pick(args, "query", "string"),
            criteria: pick(args, "criteria", "array"),
            entity: pick(args, "entity", "object"),
            behavior: pickEnum(args, "behavior", WMONITOR_BEHAVIORS, "exa_webset_monitor_create"),
            metadata: pick(args, "metadata", "object"),
          });
          return {
            content: [{
              type: "text",
              text: `Webset monitor ${m?.id ?? "(created)"} (status: ${m?.status ?? "?"}). Use exa_webset_monitor_runs to watch it.`,
              metadata: { ...meta, raw: m, monitorId: m?.id },
            }],
          };
        }
        case "exa_webset_monitor_list": {
          const data = await client.websetMonitorList({
            websetId: pick(args, "websetId", "string"),
            limit: pickIntInRange(args, "limit", 1, 200, "exa_webset_monitor_list"),
            cursor: pick(args, "cursor", "string"),
          });
          return {
            content: [{
              type: "text",
              text: renderList(data, "webset monitor", renderMonitor, 50),
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_webset_monitor_get": {
          const m = await client.websetMonitorGet(requiredStr(args, "monitorId", "exa_webset_monitor_get"));
          return {
            content: [{ type: "text", text: renderMonitor(m), metadata: { ...meta, raw: m } }],
          };
        }
        case "exa_webset_monitor_update": {
          const monitorId = requiredStr(args, "monitorId", "exa_webset_monitor_update");
          const body: any = {};
          const status = pickEnum(args, "status", WMONITOR_STATUSES, "exa_webset_monitor_update");
          const cadence = pick(args, "cadence", "object");
          const behavior = pick(args, "behavior", "object");
          const metadata = pick(args, "metadata", "object");
          if (status !== undefined) body.status = status;
          if (cadence !== undefined) body.cadence = cadence;
          if (behavior !== undefined) body.behavior = behavior;
          if (metadata !== undefined) body.metadata = metadata;
          if (!Object.keys(body).length) {
            throw new ExaApiError("exa_webset_monitor_update needs at least one field (status|cadence|behavior|metadata)", "bad_request");
          }
          const m = await client.websetMonitorUpdate(monitorId, body);
          return {
            content: [{ type: "text", text: `Updated webset monitor ${monitorId}: ${renderMonitor(m ?? body)}`, metadata: { ...meta, raw: m } }],
          };
        }
        case "exa_webset_monitor_delete": {
          const monitorId = requiredStr(args, "monitorId", "exa_webset_monitor_delete");
          const res = await client.websetMonitorDelete(monitorId);
          return {
            content: [{ type: "text", text: `Deleted webset monitor ${monitorId}.`, metadata: { ...meta, raw: res } }],
          };
        }
        case "exa_webset_monitor_runs": {
          const monitorId = requiredStr(args, "monitorId", "exa_webset_monitor_runs");
          const data = await client.websetMonitorRuns(monitorId, {
            limit: pickIntInRange(args, "limit", 1, 200, "exa_webset_monitor_runs"),
            cursor: pick(args, "cursor", "string"),
          });
          const rows: any[] = data?.data ?? [];
          const lines = rows.map((r, i) => `${i + 1}. [${r.status ?? "?"}] ${r.id}${r.createdAt ? ` (${r.createdAt})` : ""}`);
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${rows.length} run(s) for webset monitor ${monitorId}:\n${lines.join("\n") || "  (none)"}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_webset_monitor_run_get": {
          const monitorId = requiredStr(args, "monitorId", "exa_webset_monitor_run_get");
          const runId = requiredStr(args, "runId", "exa_webset_monitor_run_get");
          const run = await client.websetMonitorRunGet(monitorId, runId);
          return {
            content: [{ type: "text", text: renderMonitorRun(run), metadata: { ...meta, raw: run } }],
          };
        }
        case "exa_webset_item_get": {
          const id = requiredStr(args, "websetId", "exa_webset_item_get");
          const itemId = requiredStr(args, "itemId", "exa_webset_item_get");
          const item = await client.websetItemGet(id, itemId);
          return {
            content: [{ type: "text", text: renderObject(item, `Webset item ${itemId}`), metadata: { ...meta, raw: item } }],
          };
        }
        case "exa_webset_item_delete": {
          const id = requiredStr(args, "websetId", "exa_webset_item_delete");
          const itemId = requiredStr(args, "itemId", "exa_webset_item_delete");
          const res = await client.websetItemDelete(id, itemId);
          return {
            content: [{ type: "text", text: `Removed item ${itemId} from webset ${id}.`, metadata: { ...meta, raw: res } }],
          };
        }
        case "exa_webset_search_status": {
          const id = requiredStr(args, "websetId", "exa_webset_search_status");
          const searchId = requiredStr(args, "searchId", "exa_webset_search_status");
          const s = await client.websetSearchStatus(id, searchId);
          const lines = [`Search ${searchId} (status: ${s?.status ?? "?"})`];
          if (s?.count !== undefined) lines.push(`items found so far: ${s.count}`);
          if (s?.recall?.expected) lines.push(`recall estimate: ${JSON.stringify(s.recall.expected)}`);
          return {
            content: [{ type: "text", text: lines.join("\n"), metadata: { ...meta, raw: s } }],
          };
        }
        case "exa_webset_search_cancel": {
          const id = requiredStr(args, "websetId", "exa_webset_search_cancel");
          const searchId = requiredStr(args, "searchId", "exa_webset_search_cancel");
          const s = await client.websetSearchCancel(id, searchId);
          return {
            content: [{
              type: "text",
              text: `Cancellation requested for search ${searchId} on webset ${id}${s?.status ? ` (status: ${s.status})` : ""}.`,
              metadata: { ...meta, raw: s },
            }],
          };
        }
        case "exa_enrichment_get": {
          const id = requiredStr(args, "websetId", "exa_enrichment_get");
          const enrichmentId = requiredStr(args, "enrichmentId", "exa_enrichment_get");
          const e = await client.enrichmentGet(id, enrichmentId);
          return {
            content: [{ type: "text", text: renderObject(e, `Enrichment ${enrichmentId}`), metadata: { ...meta, raw: e } }],
          };
        }
        case "exa_enrichment_cancel": {
          const id = requiredStr(args, "websetId", "exa_enrichment_cancel");
          const enrichmentId = requiredStr(args, "enrichmentId", "exa_enrichment_cancel");
          const e = await client.enrichmentCancel(id, enrichmentId);
          return {
            content: [{
              type: "text",
              text: `Cancellation requested for enrichment ${enrichmentId} on webset ${id}${e?.status ? ` (status: ${e.status})` : ""}.`,
              metadata: { ...meta, raw: e },
            }],
          };
        }
        case "exa_enrichment_update": {
          const id = requiredStr(args, "websetId", "exa_enrichment_update");
          const enrichmentId = requiredStr(args, "enrichmentId", "exa_enrichment_update");
          const body: any = {};
          const description = pick(args, "description", "string");
          const format = pickEnum(args, "format", WEBSET_ENRICH_FORMATS, "exa_enrichment_update");
          const options = pick(args, "options", "array");
          const metadata = pick(args, "metadata", "object");
          if (description !== undefined) body.description = description;
          if (format !== undefined) body.format = format;
          if (options !== undefined) body.options = options;
          if (metadata !== undefined) body.metadata = metadata;
          if (!Object.keys(body).length) {
            throw new ExaApiError("exa_enrichment_update needs at least one field (description|format|options|metadata)", "bad_request");
          }
          const e = await client.enrichmentUpdate(id, enrichmentId, body);
          return {
            content: [{ type: "text", text: `Updated enrichment ${enrichmentId}: ${renderObject(e ?? body, "enrichment")}`, metadata: { ...meta, raw: e } }],
          };
        }
        case "exa_enrichment_delete": {
          const id = requiredStr(args, "websetId", "exa_enrichment_delete");
          const enrichmentId = requiredStr(args, "enrichmentId", "exa_enrichment_delete");
          const res = await client.enrichmentDelete(id, enrichmentId);
          return {
            content: [{ type: "text", text: `Deleted enrichment ${enrichmentId} from webset ${id}.`, metadata: { ...meta, raw: res } }],
          };
        }
        case "exa_import_create": {
          const size = pickIntInRange(args, "size", 0, 52428800, "exa_import_create");
          const count = pickIntInRange(args, "count", 0, 1000000, "exa_import_create");
          const entity = pick(args, "entity", "object");
          if (size === undefined || count === undefined || !entity) {
            throw new ExaApiError("exa_import_create requires size, count, and entity", "bad_request");
          }
          const imp = await client.importCreate({
            size, count, entity,
            format: pick(args, "format", "string"),
            title: pick(args, "title", "string"),
            csv: pick(args, "csv", "object"),
            metadata: pick(args, "metadata", "object"),
          });
          const url = imp?.uploadUrl ?? imp?.url;
          return {
            content: [{
              type: "text",
              text: `Import ${imp?.id ?? "(created)"} (status: ${imp?.status ?? "?"})${url ? `\nUpload the CSV to: ${url}` : ""}${imp?.uploadValidUntil ? ` (until ${imp.uploadValidUntil})` : ""}.`,
              metadata: { ...meta, raw: imp, importId: imp?.id, uploadUrl: url },
            }],
          };
        }
        case "exa_import_list": {
          const data = await client.importList({
            limit: pickIntInRange(args, "limit", 1, 50, "exa_import_list"),
            cursor: pick(args, "cursor", "string"),
          });
          const rows: any[] = data?.data ?? [];
          const lines = rows.map((r, i) => `${i + 1}. [${r.status ?? "?"}] ${r.id} ${r.title ? `— ${r.title}` : ""}`);
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${rows.length} import(s):\n${lines.join("\n") || "  (none)"}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_import_get": {
          const imp = await client.importGet(requiredStr(args, "importId", "exa_import_get"));
          return {
            content: [{ type: "text", text: renderObject(imp, "Import"), metadata: { ...meta, raw: imp } }],
          };
        }
        case "exa_import_update": {
          const importId = requiredStr(args, "importId", "exa_import_update");
          const title = pick(args, "title", "string");
          const metadata = pick(args, "metadata", "object");
          if (title === undefined && metadata === undefined) {
            throw new ExaApiError("exa_import_update needs at least one of title or metadata", "bad_request");
          }
          const body: any = {};
          if (title !== undefined) body.title = title;
          if (metadata !== undefined) body.metadata = metadata;
          const imp = await client.importUpdate(importId, body);
          return {
            content: [{ type: "text", text: `Updated import ${importId}${imp?.title ? ` (title: ${imp.title})` : ""}.`, metadata: { ...meta, raw: imp } }],
          };
        }
        case "exa_import_delete": {
          const importId = requiredStr(args, "importId", "exa_import_delete");
          const res = await client.importDelete(importId);
          return {
            content: [{ type: "text", text: `Deleted import ${importId}.`, metadata: { ...meta, raw: res } }],
          };
        }
        case "exa_webhook_create": {
          const events = pick(args, "events", "array");
          const url = pick(args, "url", "string");
          if (!Array.isArray(events) || !events.length) {
            throw new ExaApiError("exa_webhook_create requires a non-empty 'events' array", "bad_request");
          }
          if (!url) {
            throw new ExaApiError("exa_webhook_create requires a non-empty 'url'", "bad_request");
          }
          const wh = await client.webhookCreate({ events, url, metadata: pick(args, "metadata", "object") });
          return {
            content: [{
              type: "text",
              text: `Webhook ${wh?.id ?? "(created)"} registered for ${events.length} event type(s)${wh?.secret ? " (secret returned — store it to verify signatures)" : ""}.`,
              metadata: { ...meta, raw: wh, webhookId: wh?.id },
            }],
          };
        }
        case "exa_webhook_list": {
          const data = await client.webhookList({
            limit: pickIntInRange(args, "limit", 1, 50, "exa_webhook_list"),
            cursor: pick(args, "cursor", "string"),
          });
          const rows: any[] = data?.data ?? [];
          const lines = rows.map((r, i) => `${i + 1}. [${r.active ? "active" : "inactive"}] ${r.id} → ${r.url} (${(r.events ?? []).join(", ")})`);
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${rows.length} webhook(s):\n${lines.join("\n") || "  (none)"}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_webhook_get": {
          const wh = await client.webhookGet(requiredStr(args, "webhookId", "exa_webhook_get"));
          return {
            content: [{ type: "text", text: renderObject(wh, "Webhook"), metadata: { ...meta, raw: wh } }],
          };
        }
        case "exa_webhook_update": {
          const webhookId = requiredStr(args, "webhookId", "exa_webhook_update");
          const events = pick(args, "events", "array");
          const url = pick(args, "url", "string");
          const metadata = pick(args, "metadata", "object");
          if (events === undefined && url === undefined && metadata === undefined) {
            throw new ExaApiError("exa_webhook_update needs at least one of events, url, or metadata", "bad_request");
          }
          const body: any = {};
          if (events !== undefined) body.events = events;
          if (url !== undefined) body.url = url;
          if (metadata !== undefined) body.metadata = metadata;
          const wh = await client.webhookUpdate(webhookId, body);
          return {
            content: [{ type: "text", text: `Updated webhook ${webhookId}.`, metadata: { ...meta, raw: wh } }],
          };
        }
        case "exa_webhook_delete": {
          const webhookId = requiredStr(args, "webhookId", "exa_webhook_delete");
          const res = await client.webhookDelete(webhookId);
          return {
            content: [{ type: "text", text: `Deleted webhook ${webhookId}.`, metadata: { ...meta, raw: res } }],
          };
        }
        case "exa_webhook_attempts": {
          const webhookId = requiredStr(args, "webhookId", "exa_webhook_attempts");
          const data = await client.webhookAttempts(webhookId, {
            limit: pickIntInRange(args, "limit", 1, 50, "exa_webhook_attempts"),
            cursor: pick(args, "cursor", "string"),
            eventType: pick(args, "eventType", "string"),
            successful: pick(args, "successful", "boolean"),
          });
          const rows: any[] = data?.data ?? [];
          const lines = rows.map((r, i) =>
            `${i + 1}. ${r.successful ? "OK" : "FAIL"} ${r.eventType ?? "?"} → HTTP ${r.responseStatusCode ?? "?"} (attempt ${r.attempt ?? 1}${r.attemptedAt ? `, ${r.attemptedAt}` : ""})`);
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${rows.length} delivery attempt(s) for webhook ${webhookId}:\n${lines.join("\n") || "  (none)"}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_event_list": {
          const data = await client.eventList({
            limit: pickIntInRange(args, "limit", 1, 100, "exa_event_list"),
            cursor: pick(args, "cursor", "string"),
            type: pick(args, "type", "string"),
            types: pick(args, "types", "array"),
            createdBefore: pick(args, "createdBefore", "string"),
            createdAfter: pick(args, "createdAfter", "string"),
          });
          const rows: any[] = data?.data ?? [];
          const lines = rows.map((r, i) =>
            `${i + 1}. ${r.type ?? r.eventType ?? "?"} ${r.id ?? ""}${r.createdAt ? ` (${r.createdAt})` : ""}${r.subject?.id ? ` on ${r.subject.id}` : ""}`);
          const more = data?.hasMore ? `\n…more (cursor: ${data.nextCursor})` : "";
          return {
            content: [{
              type: "text",
              text: `${rows.length} event(s):\n${lines.join("\n") || "  (none)"}${more}`,
              metadata: { ...meta, raw: data, nextCursor: data?.nextCursor },
            }],
          };
        }
        case "exa_event_get": {
          const ev = await client.eventGet(requiredStr(args, "eventId", "exa_event_get"));
          return {
            content: [{ type: "text", text: renderObject(ev, "Event"), metadata: { ...meta, raw: ev } }],
          };
        }
        default:
          throw new Error(`Unknown tool: ${name}`);
      }
    } catch (error) {
      const message = error instanceof ExaApiError
        ? error.message
        : (error instanceof Error ? error.message : String(error));
      const category = error instanceof ExaApiError ? error.category : "error";
      console.error("Error executing tool:", error);
      // Spec-compliant tool error: isError: true plus the machine-readable
      // category in metadata.
      return {
        isError: true,
        content: [{
          type: "text",
          text: JSON.stringify({ error: message, category }, null, 2),
          metadata: { error: true, category, ...meta },
        }],
      };
    }
  });

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("Exa MCP Server running on stdio");
}

main().catch((error: unknown) => {
  console.error("Fatal error in main():", error instanceof Error ? error.message : String(error));
  process.exit(1);
});
