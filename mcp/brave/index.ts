#!/usr/bin/env node

// Brave Search MCP Server
// ----------------------------------------------------------------------------
// A Model Context Protocol server for the Brave Search REST API
// (api.search.brave.com).
//
// Quality bar: mirrors the Exa MCP server / Linear MCP reference —
//   * a RateLimiter that serializes API calls, tracks request metrics, and
//     throttles toward the plan's budget;
//   * a typed fetch-based client with error classification (auth vs.
//     validation vs. rate-limit vs. network);
//   * JSON-Schema input contracts for each tool, with inline defaults and
//     allowed values so the model can make the right granular choice;
//   * addressable resources (server status / endpoint catalog);
//   * a server prompt that teaches the model when to use each tool;
//   * apiMetrics attached to every tool response.
//
// Auth: BRAVE_SEARCH_API_KEY or BRAVE_API_KEY (via `X-Subscription-Token`
// header). Loaded from the environment or a .env file (Bun auto-loads .env).
//
// Endpoints used (all live-verified against the public API):
//   GET /res/v1/web/search         brave_web_search   (web.results + mixed + videos + infobox)
//   GET /res/v1/news/search        brave_news_search  (results[])
//   GET /res/v1/videos/search      brave_video_search (results[])
//   GET /res/v1/images/search      brave_image_search (results[])
//   GET /res/v1/local/place_search brave_place_search (results[] POI + resolved location)
//   GET /res/v1/web/search +       brave_rich_search  (weather/stock/FX/… two-step)
//       /res/v1/web/rich?callback_key=...
//   GET /res/v1/llm/context        brave_llm_context  (RAG grounding; plan-gated)
//   GET /res/v1/suggest/search     brave_suggest      (query autocomplete)
//   GET /res/v1/spellcheck/search  brave_spellcheck   (corrected query form)
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

const API_BASE = "https://api.search.brave.com";

// Authoritative enums (live-verified against the API + the public client).
const FRESHNESS_SHORTHANDS = ["pd", "pw", "pm", "py"]; // 24h / 7d / 30d / 365d
const SAFE_SEARCH = ["off", "moderate", "strict"] as const;
const IMAGE_SAFE_SEARCH = ["off", "strict"] as const;
const IMAGE_PROPERTY = ["any", "commercial", "non-commercial"] as const;
const IMAGE_SEARCH_TYPE = ["all", "transparent"] as const;
const IMAGE_UNIT = ["px", "em"] as const;
const CONTEXT_THRESHOLD_MODES = ["strict", "balanced", "lenient"] as const;

// ---------------------------------------------------------------------------
// Rate limiter (serial queue, hourly budget, request metrics).
// ---------------------------------------------------------------------------
interface RateLimiterMetrics {
  totalRequests: number;
  requestsInLastHour: number;
  averageRequestTime: number;
  queueLength: number;
  lastRequestTime: number;
}

class RateLimiter {
  // Brave's free plan is capped by the *month*; this hourly throttle is a
  // conservative anti-burst guard (not the hard cap). Raise for paid plans.
  public readonly requestsPerHour = 100;
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
    console.error(`[Brave API] Enqueueing request${operation ? ` for ${operation}` : ""} (queue: ${queuePosition})`);
    return new Promise((resolve, reject) => {
      this.queue.push(async () => {
        this.lifetimeRequests++; // count every attempt, success or failure
        const startedAt = Date.now(); // flight start — excludes queue wait
        try {
          const result = await fn();
          resolve(result);
        } catch (error) {
          console.error(`[Brave API] Error in request${operation ? ` for ${operation}` : ""}: `, error);
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
// Error type: classified with an actionable category.
// ---------------------------------------------------------------------------
class BraveApiError extends Error {
  constructor(message: string, public readonly category: string, public readonly status?: number) {
    super(message);
    this.name = "BraveApiError";
  }
}

// Strip Brave's <strong> highlight markup from a description.
function clean(s: string | undefined | null): string {
  if (s === undefined || s === null) return "";
  return String(s).replace(/<\/?strong>/g, "").replace(/&amp;/g, "&").replace(/&#x27;/g, "'").replace(/&quot;/g, '"');
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------
class BraveClient {
  private apiKey: string;
  public readonly rateLimiter: RateLimiter;

  constructor(apiKey: string) {
    if (!apiKey) throw new Error("BRAVE_SEARCH_API_KEY (or BRAVE_API_KEY) environment variable is required");
    this.apiKey = apiKey;
    this.rateLimiter = new RateLimiter();
  }

  private async get(
    path: string,
    params: Record<string, string | number | boolean | string[] | undefined> = {},
    timeoutMs = 30000,
  ): Promise<any> {
    const url = new URL(`${API_BASE}${path}`);
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      // Array values emit repeated keys (ids=a&ids=b) — the shape /local/pois
      // and /local/descriptions expect; scalars use a single key.
      if (Array.isArray(v)) { for (const item of v) url.searchParams.append(k, String(item)); }
      else url.searchParams.set(k, String(v));
    }
    const resp = await fetch(url.toString(), {
      method: "GET",
      headers: { "X-Subscription-Token": this.apiKey, "Accept": "application/json" },
      signal: AbortSignal.timeout(timeoutMs),
    });
    const text = await resp.text();
    let data: any = undefined;
    if (text) { try { data = JSON.parse(text); } catch { data = undefined; } }
    if (resp.ok) return data ?? {};
    let detail = data ?? text.slice(0, 400);
    // Brave signals auth via a top-level `error` object with a `code`. The
    // `error` field is normally an object but some upstream/CDN shapes carry it
    // as a plain string; treat a non-dict `error` as empty so classification
    // never crashes and still falls through to the status-based branches.
    const e = (data && typeof data === "object" && typeof data.error === "object" && data.error) ? data.error : {};
    const code: string = typeof e.code === "string" ? e.code : "";
    const meta: any = (e.meta && typeof e.meta === "object") ? e.meta : {};
    // Human-readable message lives at error.detail (string); `detail` above is
    // the whole parsed body object, so coerce defensively before interpolating.
    const msg: string =
      typeof e.detail === "string" ? e.detail :
      typeof detail === "string" ? detail :
      (() => { try { return JSON.stringify(detail).slice(0, 200); } catch { return "(unavailable)"; } })();
    // Plan-gated add-ons (LLM Context, GNews, suggest, spellcheck, …) return
    // 400 with code=OPTION_NOT_IN_PLAN AND meta.component="authentication".
    // The component is a red herring — the code is the discriminator. Check
    // this FIRST so it isn't swallowed by the auth branch below.
    if (code === "OPTION_NOT_IN_PLAN") {
      throw new BraveApiError(
        `Brave endpoint ${path} is not enabled on the current plan ` +
        `(OPTION_NOT_IN_PLAN). This key lacks that add-on; it is not a request bug. Detail: ${msg}`,
        "plan", 400);
    }
    if (resp.status === 401 || code === "SUBSCRIPTION_TOKEN_INVALID" || meta.component === "authentication") {
      throw new BraveApiError(
        `Brave API key rejected (${resp.status}). Check BRAVE_SEARCH_API_KEY. Detail: ${msg}`, "auth", resp.status);
    }
    if (resp.status === 422 && (code === "VALIDATION" || Array.isArray(meta.errors))) {
      throw new BraveApiError(`Brave rejected the request (422): invalid params. Detail: ${msg}`, "bad_request", 422);
    }
    if (code && resp.status >= 400) {
      throw new BraveApiError(`Brave error ${code} (${resp.status}): ${msg}`, "api", resp.status);
    }
    if (resp.status === 429) {
      throw new BraveApiError(`Brave rate limit hit (429). Slow down and retry. Detail: ${msg}`, "rate_limit", 429);
    }
    if (resp.status >= 500) {
      throw new BraveApiError(`Brave server error (${resp.status}) on ${path}.`, "server", resp.status);
    }
    throw new BraveApiError(`Brave endpoint ${path} returned ${resp.status}. Detail: ${msg}`, "http", resp.status);
  }

  addMetrics(response: any) {
    const m = this.rateLimiter.getMetrics();
    return {
      ...response,
      metadata: {
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
  web(args: BraveSearchArgs) {
    const params: any = { q: args.query, count: args.count ?? 5, safe_search: "moderate" };
    if (args.freshness) params.freshness = args.freshness;
    if (args.country) params.country = args.country;
    if (args.searchLang) params.search_lang = args.searchLang;
    if (args.safeSearch) params.safe_search = args.safeSearch;
    if (args.resultFilter) params.result_filter = args.resultFilter;
    if (args.extra) params.extra = "true"; // up to 5 extra alternative snippets per result
    if (args.spellcheck !== undefined && args.spellcheck !== null) params.spellcheck = args.spellcheck ? "true" : "false";
    if (args.offset !== undefined && args.offset !== null) params.offset = args.offset; // 0..9 page window
    return this.rateLimiter.enqueue(() => this.get("/res/v1/web/search", params, 30000), "brave_web_search");
  }

  news(args: BraveNewsArgs) {
    const params: any = { q: args.query, count: args.count ?? 5, safe_search: "moderate" };
    if (args.freshness) params.freshness = args.freshness;
    if (args.country) params.country = args.country;
    if (args.searchLang) params.search_lang = args.searchLang;
    if (args.safeSearch) params.safe_search = args.safeSearch;
    if (args.extra) params.extra = "true";
    if (args.gnews) params.gnews = "true"; // GNews news block (same /news/search endpoint)
    return this.rateLimiter.enqueue(() => this.get("/res/v1/news/search", params, 30000), "brave_news_search");
  }

  llmContext(args: BraveLlmContextArgs) {
    const params: any = {
      q: args.query,
      count: args.count ?? 20,
      maximum_number_of_tokens: args.maxTokens ?? 8192,
      maximum_number_of_urls: args.maxUrls ?? 20,
      maximum_number_of_snippets: args.maxSnippets ?? 50,
      maximum_number_of_tokens_per_url: args.maxTokensPerUrl ?? 4096,
      maximum_number_of_snippets_per_url: args.maxSnippetsPerUrl ?? 50,
    };
    if (args.country) params.country = args.country;
    if (args.searchLang) params.search_lang = args.searchLang;
    if (args.thresholdMode) params.context_threshold_mode = args.thresholdMode;
    if (args.enableLocal !== undefined) params.enable_local = args.enableLocal ? "true" : "false";
    if (args.goggles) params.goggles = args.goggles;
    return this.rateLimiter.enqueue(() => this.get("/res/v1/llm/context", params, 60000), "brave_llm_context");
  }

  suggest(args: BraveSuggestArgs) {
    const params: any = { q: args.query };
    if (args.country) params.country = args.country;
    return this.rateLimiter.enqueue(() => this.get("/res/v1/suggest/search", params, 30000), "brave_suggest");
  }

  spellcheck(args: { query: string }) {
    const params: any = { q: args.query };
    return this.rateLimiter.enqueue(() => this.get("/res/v1/spellcheck/search", params, 30000), "brave_spellcheck");
  }

  video(args: BraveVideoArgs) {
    const params: any = { q: args.query, count: args.count ?? 5 };
    if (args.country) params.country = args.country;
    if (args.searchLang) params.search_lang = args.searchLang;
    if (args.safeSearch) params.safe_search = args.safeSearch;
    return this.rateLimiter.enqueue(() => this.get("/res/v1/videos/search", params, 30000), "brave_video_search");
  }

  image(args: BraveImageArgs) {
    const params: any = { q: args.query, count: args.count ?? 10 };
    if (args.country) params.country = args.country;
    if (args.searchLang) params.search_lang = args.searchLang;
    if (args.safesearch) params.safesearch = args.safesearch;
    if (args.spellcheck !== undefined && args.spellcheck !== null) params.spellcheck = args.spellcheck ? "true" : "false";
    if (args.unit) params.unit = args.unit;
    if (args.property) params.property = args.property;
    if (args.searchType) params.search_type = args.searchType;
    return this.rateLimiter.enqueue(() => this.get("/res/v1/images/search", params, 30000), "brave_image_search");
  }

  place(args: BravePlaceArgs) {
    const params: any = {};
    if (args.query) params.q = args.query;
    if (args.latitude !== undefined && args.longitude !== undefined) {
      params.latitude = args.latitude;
      params.longitude = args.longitude;
    } else if (args.location) {
      params.location = args.location;
    } else {
      throw new BraveApiError("brave_place_search needs either latitude+longitude or a location string", "bad_request");
    }
    params.count = args.count ?? 10;
    if (args.radius !== undefined && args.radius !== null) params.radius = args.radius; // meters bias
    if (args.country) params.country = args.country;
    if (args.searchLang) params.search_lang = args.searchLang;
    if (args.units) params.units = args.units;
    if (args.safeSearch) params.safesearch = args.safeSearch;
    if (args.spellcheck !== undefined && args.spellcheck !== null) params.spellcheck = args.spellcheck ? "true" : "false";
    if (args.category) params.cate = args.category;
    return this.rateLimiter.enqueue(() => this.get("/res/v1/local/place_search", params, 45000), "brave_place_search");
  }

  pois(args: { ids: string[]; searchLang?: string; uiLang?: string; units?: string }) {
    if (!Array.isArray(args.ids) || !args.ids.length) {
      throw new BraveApiError("brave_pois requires a non-empty 'ids' array", "bad_request");
    }
    if (args.ids.length > 20) {
      throw new BraveApiError("brave_pois accepts at most 20 ids (got " + args.ids.length + ")", "bad_request");
    }
    const params: any = { ids: args.ids };
    if (args.searchLang) params.search_lang = args.searchLang;
    if (args.uiLang) params.ui_lang = args.uiLang;
    if (args.units) params.units = args.units;
    return this.rateLimiter.enqueue(() => this.get("/res/v1/local/pois", params, 45000), "brave_pois");
  }

  poiDescriptions(args: { ids: string[] }) {
    if (!Array.isArray(args.ids) || !args.ids.length) {
      throw new BraveApiError("brave_poi_descriptions requires a non-empty 'ids' array", "bad_request");
    }
    if (args.ids.length > 20) {
      throw new BraveApiError("brave_poi_descriptions accepts at most 20 ids (got " + args.ids.length + ")", "bad_request");
    }
    return this.rateLimiter.enqueue(
      () => this.get("/res/v1/local/descriptions", { ids: args.ids }, 45000), "brave_poi_descriptions");
  }

  async rich(args: BraveRichArgs): Promise<any> {
    // Step 1: web search with the rich callback hint.
    const step1 = await this.rateLimiter.enqueue(
      () => this.get("/res/v1/web/search", { q: args.query, count: 1, enable_rich_callback: "1" }, 30000),
      "brave_rich_search:hint");
    const hint = (step1?.rich?.hint ?? {});
    const callbackKey = hint.callback_key;
    if (!callbackKey) {
      return { type: "rich", query: args.query, vertical: hint.vertical ?? null, results: [], note: "No rich callback returned for this query." };
    }
    // Step 2: fetch the rich payload.
    const step2 = await this.rateLimiter.enqueue(
      () => this.get("/res/v1/web/rich", { callback_key: callbackKey }, 30000),
      "brave_rich_search:fetch");
    return {
      type: "rich",
      query: args.query,
      vertical: hint.vertical ?? (step2?.results?.[0]?.subtype ?? null),
      results: step2?.results ?? [],
    };
  }

  status(): Promise<any> {
    // A minimal 1-result web search doubles as a token/plan health check.
    return this.rateLimiter.enqueue(
      () => this.get("/res/v1/web/search", { q: "test", count: 1 }, 15000), "brave_status");
  }
}

// ---------------------------------------------------------------------------
// Tool arg types
// ---------------------------------------------------------------------------
interface BraveSearchArgs {
  query: string; count?: number; freshness?: string; country?: string;
  searchLang?: string; safeSearch?: string; resultFilter?: string;
  extra?: boolean; spellcheck?: boolean; offset?: number;
}
interface BraveNewsArgs {
  query: string; count?: number; freshness?: string; country?: string;
  searchLang?: string; safeSearch?: string; extra?: boolean; gnews?: boolean;
}
interface BraveLlmContextArgs {
  query: string; count?: number; country?: string; searchLang?: string;
  maxTokens?: number; maxUrls?: number; maxSnippets?: number;
  maxTokensPerUrl?: number; maxSnippetsPerUrl?: number;
  thresholdMode?: string; enableLocal?: boolean; goggles?: string;
}
interface BraveSuggestArgs { query: string; country?: string; }
interface BraveVideoArgs { query: string; count?: number; country?: string; searchLang?: string; safeSearch?: string; }
interface BraveImageArgs {
  query: string; count?: number; country?: string; searchLang?: string;
  safesearch?: string; spellcheck?: boolean; unit?: string;
  property?: string; searchType?: string;
}
interface BravePlaceArgs {
  query?: string; latitude?: number; longitude?: number; location?: string;
  count?: number; radius?: number; country?: string; searchLang?: string;
  units?: string; safeSearch?: string; spellcheck?: boolean; category?: string;
}
interface BraveRichArgs { query: string; }

// ---------------------------------------------------------------------------
// Tool definitions (JSON-Schema input contracts, Linear-style: each field
// carries its default and allowed values inline so the model makes the right
// granular choice without extra round-trips).
// ---------------------------------------------------------------------------
const braveWebSearchTool: Tool = {
  name: "brave_web_search",
  description:
    "Searches the web via the Brave Search API. Supports filtering by any " +
    "combination of: age (freshness: pd=last 24h, pw=7d, pm=30d, py=365d, or a " +
    "YYYY-MM-DDtoYYYY-MM-DD range), location (country), language (searchLang), " +
    "result sections (resultFilter), and extra snippets (extra=true adds up to " +
    "5 per result). Results also include embedded videos and the knowledge " +
    "panel when present. Returns up to 5 results by default (configurable via " +
    "count, max 20; page forward via offset).",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "Natural-language search query (required)" },
      count: { type: "number", description: "Results to return, 1-20 (default: 5)" },
      freshness: {
        type: "string",
        description:
          "Age filter (pd=last 24h, pw=7d, pm=30d, py=365d, or a date range " +
          "like 2025-01-01to2025-06-30); the main recency lever",
      },
      country: { type: "string", description: "Two-letter ISO country code (e.g. US, DE)" },
      searchLang: { type: "string", description: "ISO 639-1 language code (e.g. en, de)" },
      safeSearch: { type: "string", description: "Adult-content filter (off | moderate | strict, default: moderate)" },
      resultFilter: {
        type: "string",
        description: "Comma-separated sections to keep (e.g. 'web,discussions'); omit for all sections",
      },
      extra: { type: "boolean", description: "Return up to 5 extra snippets per result (default: false)" },
      spellcheck: { type: "boolean", description: "Force (true) or disable (false) spell-correction (default: API-decided)" },
      offset: { type: "number", description: "Page offset, 0-9; keep count fixed and raise offset to page forward (default: 0)" },
    },
    required: ["query"],
  },
};

const braveNewsSearchTool: Tool = {
  name: "brave_news_search",
  description:
    "Searches news via the Brave Search API. Supports filtering by age " +
    "(freshness: pd=last 24h, pw=7d, pm=30d, py=365d, or a date range), " +
    "location (country), language (searchLang), and extra snippets " +
    "(extra=true adds up to 5 per article). Returns news articles (title, url, " +
    "description, age, source). Returns up to 5 results by default " +
    "(configurable via count, max 20).",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "Search query (required)" },
      count: { type: "number", description: "Results to return, 1-20 (default: 5)" },
      freshness: {
        type: "string",
        description:
          "Age filter: pd (24h), pw (7d), pm (30d), py (365d), or a date range " +
          "YYYY-MM-DDtoYYYY-MM-DD.",
      },
      country: { type: "string", description: "Two-letter ISO country code (e.g. US)" },
      searchLang: { type: "string", description: "ISO 639-1 language code (e.g. en)" },
      safeSearch: { type: "string", description: "off | moderate | strict (default: moderate)" },
      extra: {
        type: "boolean",
        description: "Return up to 5 extra alternative snippets per article. (default: false)",
      },
      gnews: {
        type: "boolean",
        description: "Include the GNews block in the response (default: false)",
      },
    },
    required: ["query"],
  },
};

const braveVideoSearchTool: Tool = {
  name: "brave_video_search",
  description:
    "Searches videos via the Brave Search API. Supports filtering by location " +
    "(country) and language (searchLang). Returns videos (title, url, " +
    "thumbnail, duration, creator). Returns up to 5 results by default " +
    "(configurable via count, max 20).",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "Search query (required)" },
      count: { type: "number", description: "Results to return, 1-20 (default: 5)" },
      country: { type: "string", description: "Two-letter ISO country code (e.g. US)" },
      searchLang: { type: "string", description: "ISO 639-1 language code (e.g. en)" },
      safeSearch: { type: "string", description: "off | moderate | strict (default: moderate)" },
    },
    required: ["query"],
  },
};

const braveImageSearchTool: Tool = {
  name: "brave_image_search",
  description:
    "Searches images via the Brave Search API. Supports filtering by licensing " +
    "(property: any, commercial, non-commercial), transparency (searchType: " +
    "all, transparent), and location (country). Not paginated — raise count " +
    "for more results. Returns up to 10 images by default (configurable via " +
    "count, max 200).",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "Search query (required)" },
      count: { type: "number", description: "Results to return, 1-200 (default: 10)" },
      country: { type: "string", description: "Two-letter ISO country code (e.g. US)" },
      searchLang: { type: "string", description: "ISO 639-1 language code (e.g. en)" },
      safesearch: { type: "string", description: "Adult filter: off | strict (default: strict)" },
      spellcheck: {
        type: "boolean",
        description: "Force (true) or disable (false) spell-correction. (default: API-decided)",
      },
      unit: {
        type: "string",
        description: "Image sizing unit for dimension values: px | em (default: px)",
      },
      property: {
        type: "string",
        description: "Licensing filter: any | commercial | non-commercial (default: any)",
      },
      searchType: {
        type: "string",
        description: "Transparency filter: all | transparent (default: all)",
      },
    },
    required: ["query"],
  },
};

const bravePlaceSearchTool: Tool = {
  name: "brave_place_search",
  description:
    "Searches places/POIs via Brave's dedicated geographic endpoint (200M+ " +
    "places), for local business / 'near me' queries. Anchor by coordinates " +
    "(latitude+longitude) or a location name; radius (meters) is a soft bias " +
    "toward the anchor, not a hard cutoff. Omit query for explore mode " +
    "(general POIs near the anchor). Returns businesses/landmarks with " +
    "address, opening hours, phone, rating, and the resolved location. Returns " +
    "up to 10 results by default (configurable via count, max 100).",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "What to look for (e.g. 'coffee shops'); omit for explore mode" },
      latitude: { type: "number", description: "Center latitude (must pair with longitude)" },
      longitude: { type: "number", description: "Center longitude (must pair with latitude)" },
      location: { type: "string", description: "Alternative anchor, e.g. 'san francisco ca' (one of location or coordinates required)" },
      count: { type: "number", description: "Results to return, 1-100 (default: 10)" },
      radius: { type: "number", description: "Bias radius in meters around the anchor; a soft bias, not a hard cutoff" },
      country: { type: "string", description: "Two-letter ISO country code (e.g. US)" },
      searchLang: { type: "string", description: "ISO 639-1 language code (e.g. en)" },
      units: { type: "string", description: "Distance units: metric | imperial (default: metric)" },
      safeSearch: { type: "string", description: "moderate | strict (default: moderate)" },
      spellcheck: {
        type: "boolean",
        description: "Force (true) or disable (false) spell-correction. (default: API-decided)",
      },
      category: {
        type: "string",
        description: "Optional place category hint to bias results (e.g. cafe, hotel, restaurant)",
      },
    },
  },
};

const bravePoisTool: Tool = {
  name: "brave_pois",
  description:
    "Fetches deep-detail records for the location ids that " +
    "brave_place_search / brave_web_search return in `results[].locations[].id` " +
    "(each id is transient, ~8h): reviews, pictures, email, phone, distance, " +
    "full-week schedule, price range, rating, contact. Use to expand a short " +
    "place result into a full business profile. Takes up to 20 ids (one " +
    "repeated ids= query param each).",
  inputSchema: {
    type: "object",
    properties: {
      ids: {
        type: "array",
        items: { type: "string" },
        minItems: 1,
        maxItems: 20,
        description: "Location ids to expand (from a place/web search's locations)",
      },
      searchLang: { type: "string", description: "ISO 639-1 language code (default: en)" },
      uiLang: { type: "string", description: "UI language, e.g. en-US (default: en-US)" },
      units: { type: "string", description: "Distance units: metric | imperial (default: metric)" },
    },
    required: ["ids"],
  },
};

const bravePoiDescriptionsTool: Tool = {
  name: "brave_poi_descriptions",
  description:
    "Fetches a short AI-generated ownership-era blurb for each location id " +
    "(e.g. \"Taylor Street Coffee is a popular breakfast spot …\") for the " +
    "transient ids returned by brave_place_search / brave_web_search. Takes up " +
    "to 20 ids (one repeated ids= query param each).",
  inputSchema: {
    type: "object",
    properties: {
      ids: {
        type: "array",
        items: { type: "string" },
        minItems: 1,
        maxItems: 20,
        description: "Location ids to describe (from a place/web search's locations)",
      },
    },
    required: ["ids"],
  },
};

const braveRichSearchTool: Tool = {
  name: "brave_rich_search",
  description:
    "Returns structured rich-data answers for weather, stocks, cryptocurrency, " +
    "currency conversion, calculator, definitions, unit conversion, unix " +
    "timestamps, and sports (e.g. 'weather in X', 'AAPL', '100 USD to EUR'). " +
    "Use for these known verticals instead of brave_web_search; the payload is " +
    "structured, not prose.",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "Query that maps to a rich vertical (weather/stock/FX/…)" },
    },
    required: ["query"],
  },
};

const braveLlmContextTool: Tool = {
  name: "brave_llm_context",
  description:
    "Fetches RAG/grounding context for a query via Brave's LLM-Context " +
    "endpoint: a set of high-value web URLs (grounding.generic), each with the " +
    "most relevant snippets, plus location data (grounding.poi/map) when " +
    "enable_local is set, plus a per-URL sources map. Use for grounded answer " +
    "generation where you want the raw evidence (not a prose answer). " +
    "maximum_number_of_tokens is a HARD cap over all grounding content; the API " +
    "selects the highest-value URLs/snippets within it. Plan-gated on the key " +
    "(400 OPTION_NOT_IN_PLAN if the key lacks the add-on).",
  inputSchema: {
    type: "object",
    properties: {
      query: {
        type: "string",
        maxLength: 400,
        description: "The query (~50 words or less, ≤400 chars). (required)",
      },
      maximum_number_of_tokens: {
        type: "number",
        description: "Hard cap over all grounding content, 1024-32768 (default: 8192)",
      },
      maximum_number_of_urls: {
        type: "number",
        description: "Max URLs in the grounding, 1-50 (default: 20)",
      },
      maximum_number_of_snippets: {
        type: "number",
        description: "Max total snippets across all URLs, default 50 (max 256)",
      },
      maximum_number_of_tokens_per_url: {
        type: "number",
        description: "Per-URL token cap, default 4096 (max 8192)",
      },
      maximum_number_of_snippets_per_url: {
        type: "number",
        description: "Per-URL snippet cap, 1-100 (default: 50)",
      },
      context_threshold_mode: {
        type: "string",
        enum: CONTEXT_THRESHOLD_MODES as unknown as string[],
        description: "URL relevance threshold: strict (fewer, higher-value) | balanced | lenient (default: balanced)",
      },
      enable_local: {
        type: "boolean",
        description: "Include local/poi+map grounding when the query is location-relevant (default: false)",
      },
      goggles: {
        type: "string",
        description: "Brave Goggles URL or inline Goggles config to bias ranking",
      },
      count: { type: "number", description: "Web-search result count to ground from, 1-50 (default: 20)" },
      country: { type: "string", description: "Two-letter ISO country code (default: US)" },
      searchLang: { type: "string", description: "ISO 639-1 language code (default: en)" },
    },
    required: ["query"],
  },
};

const braveSuggestTool: Tool = {
  name: "brave_suggest",
  description:
    "Returns query-suggestion / autocomplete completions for a partial query " +
    "(what Brave's search box would suggest next). Cheap, no plan add-on " +
    "required on most plans. Plan-gated on some keys.",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "The partial query to get suggestions for (required)" },
      country: { type: "string", description: "Two-letter ISO country code to localize suggestions" },
    },
    required: ["query"],
  },
};

const braveSpellcheckTool: Tool = {
  name: "brave_spellcheck",
  description:
    "Returns a spell-corrected version of the query (or the original when " +
    "Brave's model sees no misspelling). Useful before running brave_web_search " +
    "to avoid a query that will under-perform. Plan-gated on some keys.",
  inputSchema: {
    type: "object",
    properties: {
      query: { type: "string", description: "The text to spell-check (required)" },
    },
    required: ["query"],
  },
};

// ---------------------------------------------------------------------------
// Resources
// ---------------------------------------------------------------------------
const ENDPOINT_CATALOG = [
  { tool: "brave_web_search", path: "/res/v1/web/search" },
  { tool: "brave_news_search", path: "/res/v1/news/search" },
  { tool: "brave_video_search", path: "/res/v1/videos/search" },
  { tool: "brave_image_search", path: "/res/v1/images/search" },
  { tool: "brave_place_search", path: "/res/v1/local/place_search" },
  { tool: "brave_rich_search", path: "/res/v1/web/search + /res/v1/web/rich (two-step)" },
  { tool: "brave_llm_context", path: "/res/v1/llm/context" },
  { tool: "brave_suggest", path: "/res/v1/suggest/search" },
  { tool: "brave_spellcheck", path: "/res/v1/spellcheck/search" },
];

const resourceTemplates: ResourceTemplate[] = [
  {
    uriTemplate: "brave-about:",
    name: "Brave Server Info",
    description:
      "Static info about this Brave Search MCP server: base URL, auth header, " +
      "available tools/endpoints, and rate-limit notes. No network call.",
    parameters: {},
    examples: ["brave-about:"],
  },
  {
    uriTemplate: "brave-status:",
    name: "Brave Key Status",
    description:
      "Live health check: validates the BRAVE_SEARCH_API_KEY with a minimal web " +
      "search and reports which verticals returned data. Costs one request.",
    parameters: {},
    examples: ["brave-status:"],
  },
];

const serverPrompt: Prompt = {
  name: "brave-server-prompt",
  description: "Instructions for using the Brave Search MCP server effectively",
  instructions: `This server provides access to the Brave Search API (api.search.brave.com), including its newer retrieval surfaces: LLM grounding context, GNews, and suggest/spellcheck.

Core search (6 tools):
- brave_web_search: primary entry; extra=true adds up to 5 snippets/result
- brave_news_search: current articles, pair with freshness; gnews=true adds the
  GNews block on the same /news/search call
- brave_video_search: video results with duration and creator
- brave_image_search: images; not paginated — raise count for more
- brave_place_search: POI search anchored to coordinates or a place name; use
  radius (meters) as a soft bias for "near me"
- brave_rich_search: structured vertical answers (weather, stocks, currency,
  calculator, definitions, units, timestamps, sports)

Grounding & query utilities (3 tools):
- brave_llm_context: RAG/grounding evidence — high-value URLs + snippets +
  location data within a hard token budget; use when you want raw evidence for
  a grounded answer, not a prose answer
- brave_suggest: query autocomplete (what Brave's search box would suggest)
- brave_spellcheck: corrected query form before running a search

Best practices:
- For recency use freshness (pd/pw/pm/py or a YYYY-MM-DDtoYYYY-MM-DD range),
  not dates in the query
- To page web/news results keep count fixed and raise offset by 1 (0-9)
- Localize with country (2-letter ISO) and searchLang (ISO 639-1)
- A known vertical (weather/stock/FX/unit/definition/timestamp) belongs in
  brave_rich_search — its payload is structured, not prose
- brave_llm_context / brave_suggest / brave_spellcheck are plan-gated: a
  category 'plan' error (OPTION_NOT_IN_PLAN) means the key lacks that add-on,
  not a bug — fall back to brave_web_search

Resource patterns:
- brave-about: — static server info (base URL, endpoints, rate notes). No cost.
- brave-status: — live key/plan health check (one request).

The server uses the authenticated API key for all operations. Note: Brave's
free plan is capped by the month; this server throttles toward an hourly budget
to avoid bursts.`,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
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
    throw new BraveApiError(`Tool ${tool} requires a non-empty '${key}' argument`, "bad_request");
  }
  return String(v);
}

const FRESHNESS_RE = /^\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2}$/;
function pickFreshness(args: any, key: string, tool: string): string | undefined {
  const v = pick(args, key, "string");
  if (v === undefined) return undefined;
  if (!(FRESHNESS_SHORTHANDS.includes(v) || FRESHNESS_RE.test(v))) {
    throw new BraveApiError(
      `Tool ${tool}: '${key}' must be one of ${FRESHNESS_SHORTHANDS.join(", ")} ` +
      `or an inclusive date range YYYY-MM-DDtoYYYY-MM-DD (got '${v}')`, "bad_request");
  }
  return v;
}

function pickEnum(args: any, key: string, allowed: readonly string[], tool: string): string | undefined {
  const v = pick(args, key, "string");
  if (v === undefined) return undefined;
  if (!(allowed as readonly string[]).includes(v)) {
    throw new BraveApiError(
      `Tool ${tool}: '${key}' must be one of: ${allowed.join(", ")} (got '${v}')`, "bad_request");
  }
  return v;
}

function pickIntInRange(args: any, key: string, min: number, max: number, tool: string): number | undefined {
  const v = pick(args, key, "number");
  if (v === undefined) return undefined;
  if (v < min || v > max || !Number.isInteger(v)) {
    throw new BraveApiError(`Tool ${tool}: '${key}' must be an integer between ${min} and ${max} (got ${v})`, "bad_request");
  }
  return v;
}

const STRIP_HTML = /<[^>]+>/g;
function plain(s: any): string {
  if (s === undefined || s === null) return "";
  return String(s)
    .replace(STRIP_HTML, "")
    .replace(/&amp;/g, "&")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .trim();
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
async function main() {
  const apiKey = process.env.BRAVE_SEARCH_API_KEY || process.env.BRAVE_API_KEY;
  if (!apiKey) {
    console.error("BRAVE_SEARCH_API_KEY (or BRAVE_API_KEY) environment variable is required");
    process.exit(1);
  }

  console.error("Starting Brave Search MCP Server...");
  const client = new BraveClient(apiKey);

  const server = new Server(
    { name: "brave-search-mcp-server", version: "1.2.0" },
    {
      capabilities: {
        prompts: { default: serverPrompt },
        resources: { templates: true, read: true },
        tools: {},
      },
    },
  );

  const metricsMeta = () => {
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
    if (uri === "brave-about:") {
      return {
        contents: [{
          uri,
          mimeType: "application/json",
          text: JSON.stringify({
            name: "brave-search-mcp-server",
            baseUrl: API_BASE,
            authHeader: "X-Subscription-Token",
            keyEnvVars: ["BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"],
            keyConfigured: true,
            endpoints: ENDPOINT_CATALOG,
            notes: "Brave free plan is capped by the month. The server throttles toward an hourly budget to avoid bursts.",
          }, null, 2),
        }],
      };
    }
    if (uri === "brave-status:") {
      const data = await client.status();
      const web = (data?.web?.results ?? []).length;
      return {
        contents: [{
          uri,
          mimeType: "application/json",
          text: JSON.stringify({
            ok: true,
            keyValid: true,
            base: API_BASE,
            webResultSample: web,
            checkedAt: new Date().toISOString(),
          }, null, 2),
        }],
      };
    }
    throw new Error(`Unsupported resource URI: ${uri}`);
  });

  server.setRequestHandler(ListResourcesRequestSchema, async () => ({
    resources: [
      { uri: "brave-about:", name: "Brave Server Info", mimeType: "application/json" },
      { uri: "brave-status:", name: "Brave Key Status", mimeType: "application/json" },
    ],
  }));

  server.setRequestHandler(ListResourceTemplatesRequestSchema, async () => ({
    resourceTemplates,
  }));

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [
      braveWebSearchTool, braveNewsSearchTool, braveVideoSearchTool,
      braveImageSearchTool, bravePlaceSearchTool, bravePoisTool, bravePoiDescriptionsTool,
      braveRichSearchTool,
      braveLlmContextTool, braveSuggestTool, braveSpellcheckTool,
    ],
  }));

  server.setRequestHandler(ListPromptsRequestSchema, async () => ({
    prompts: [serverPrompt],
  }));

  server.setRequestHandler(GetPromptRequestSchema, async (request) => {
    if (request.params.name === serverPrompt.name) return { prompt: serverPrompt };
    throw new Error(`Prompt not found: ${request.params.name}`);
  });

  // Render a web/news/media result list to readable text.
  const renderResults = (results: any[], extra: (r: any) => string[] = () => []) => {
    if (!results || !results.length) return "(no results)";
    return results.map((r, i) => {
      const bits = [`${i + 1}. ${plain(r.title) || "(untitled)"}`, `   ${plain(r.url) || ""}`];
      const desc = plain(r.description);
      if (desc) bits.push(`   ${desc.slice(0, 240)}`);
      for (const b of extra(r)) if (b) bits.push(`   ${b}`);
      // Surface extra snippets when the API returns them (extra=true).
      const es = Array.isArray(r.extra_snippets) ? r.extra_snippets : [];
      if (es.length) {
        bits.push(`   extra snippets:`);
        for (const s of es.slice(0, 5)) bits.push(`     - ${plain(s).slice(0, 200)}`);
      }
      return bits.join("\n");
    }).join("\n");
  };

  server.setRequestHandler(CallToolRequestSchema, async (request: CallToolRequest) => {
    const { name, arguments: args = {} } = request.params;
    const meta = metricsMeta();
    try {
      switch (name) {
        case "brave_web_search": {
          const a: BraveSearchArgs = {
            query: requiredStr(args, "query", "brave_web_search"),
            count: pickIntInRange(args, "count", 1, 20, "brave_web_search"),
            freshness: pickFreshness(args, "freshness", "brave_web_search"),
            country: pick(args, "country", "string"),
            searchLang: pick(args, "searchLang", "string"),
            safeSearch: pickEnum(args, "safeSearch", SAFE_SEARCH, "brave_web_search"),
            resultFilter: pick(args, "resultFilter", "string"),
            extra: pick(args, "extra", "boolean"),
            spellcheck: pick(args, "spellcheck", "boolean"),
            offset: pickIntInRange(args, "offset", 0, 9, "brave_web_search"),
          };
          const data = await client.web(a);
          const web = data?.web?.results ?? [];
          const mixed = data?.mixed ?? [];
          const videos = data?.videos?.results ?? [];
          const infobox = data?.infobox ?? data?.infoboxes?.[0];
          const parts = [`Web results (${web.length}):\n${renderResults(web, (r) => [r.age ? `age: ${r.age}` : "", r.language ? `lang: ${r.language}` : ""])}
`];
          if (mixed.length) parts.push(`\nMixed ordering (${mixed.length}):\n${renderResults(mixed.map((m: any) => m.result ?? m), (r: any) => [r.type ? `type: ${r.type}` : ""])}`);
          if (videos.length) parts.push(`\nEmbedded videos (${videos.length}):\n${renderResults(videos, (r: any) => [r.duration ? `duration: ${r.duration}s` : ""])}`);
          if (infobox) {
            const ib = infobox;
            const lines = [`${ib.title ?? "Infobox"}`];
            if (ib.snippet) lines.push(clean(ib.snippet).slice(0, 300));
            for (const a of ib.areas_of_expertise ?? []) lines.push(`  · ${a}`);
            for (const [k, v] of Object.entries(ib.infobox?.attributes ?? {})) lines.push(`  ${k}: ${v}`);
            parts.push(`\nKnowledge panel:\n${lines.join("\n")}`);
          }
          return {
            content: [{ type: "text", text: parts.join("\n"), metadata: { ...meta, raw: data } }],
          };
        }
        case "brave_news_search": {
          const a: BraveNewsArgs = {
            query: requiredStr(args, "query", "brave_news_search"),
            count: pickIntInRange(args, "count", 1, 20, "brave_news_search"),
            freshness: pickFreshness(args, "freshness", "brave_news_search"),
            country: pick(args, "country", "string"),
            searchLang: pick(args, "searchLang", "string"),
            safeSearch: pickEnum(args, "safeSearch", SAFE_SEARCH, "brave_news_search"),
            extra: pick(args, "extra", "boolean"),
            gnews: pick(args, "gnews", "boolean"),
          };
          const data = await client.news(a);
          const news = data?.results ?? [];
          const parts = [`News results (${news.length}):\n${renderResults(news, (r: any) => [r.age ? `age: ${r.age}` : "", r.meta?.source?.logo?.url ? `source: ${r.meta.source.name ?? ""}` : ""])}
`];
          // GNews: an alternative news block on the same /news/search response
          // when gnews=true.
          const gnewsBlock = data?.gnews;
          if (gnewsBlock && typeof gnewsBlock === "object") {
            const gItems = Array.isArray(gnewsBlock.results) ? gnewsBlock.results : (Array.isArray(gnewsBlock) ? gnewsBlock : []);
            if (gItems.length) {
              const lines = gItems.map((r: any, i: number) =>
                `${i + 1}. ${plain(r.title) || "(untitled)"}\n   ${plain(r.url)}${r.age ? `  (age: ${r.age})` : ""}${r.meta?.source?.name ? `  source: ${r.meta.source.name}` : ""}`).join("\n");
              parts.push(`\nGNews block (${gItems.length}):\n${lines}`);
            }
          }
          return { content: [{ type: "text", text: parts.join("\n"), metadata: { ...meta, raw: data } }] };
        }
        case "brave_video_search": {
          const a: BraveVideoArgs = {
            query: requiredStr(args, "query", "brave_video_search"),
            count: pickIntInRange(args, "count", 1, 20, "brave_video_search"),
            country: pick(args, "country", "string"),
            searchLang: pick(args, "searchLang", "string"),
            safeSearch: pickEnum(args, "safeSearch", SAFE_SEARCH, "brave_video_search"),
          };
          const data = await client.video(a);
          const vids = data?.results ?? [];
          // Video metadata is nested under each result's `video` object.
          const text = `Video results (${vids.length}):\n${renderResults(vids, (r: any) => {
            const v = r.video ?? {};
            const dur = r.duration ?? v.duration;
            const creator = r.creator?.name ?? v.creator ?? v.author;
            return [dur ? `duration: ${dur}` : "", creator ? `by ${creator}` : ""];
          })}`;
          return { content: [{ type: "text", text, metadata: { ...meta, raw: data } }] };
        }
        case "brave_image_search": {
          const a: BraveImageArgs = {
            query: requiredStr(args, "query", "brave_image_search"),
            count: pickIntInRange(args, "count", 1, 200, "brave_image_search"),
            country: pick(args, "country", "string"),
            searchLang: pick(args, "searchLang", "string"),
            safesearch: pickEnum(args, "safesearch", IMAGE_SAFE_SEARCH, "brave_image_search"),
            spellcheck: pick(args, "spellcheck", "boolean"),
            unit: pickEnum(args, "unit", IMAGE_UNIT, "brave_image_search"),
            property: pickEnum(args, "property", IMAGE_PROPERTY, "brave_image_search"),
            searchType: pickEnum(args, "searchType", IMAGE_SEARCH_TYPE, "brave_image_search"),
          };
          const data = await client.image(a);
          const imgs = data?.results ?? [];
          const dims = (im: any): string => {
            const w = im.properties?.width, h = im.properties?.height;
            return w && h ? `${w}×${h}` : "";
          };
          const text = `Image results (${imgs.length}):\n${renderResults(imgs, (im: any) => [
            im.source ? `source: ${plain(im.source)}` : "",
            dims(im),
          ] as string[])}`;
          return { content: [{ type: "text", text, metadata: { ...meta, raw: data } }] };
        }
        case "brave_place_search": {
          const a: BravePlaceArgs = {
            query: pick(args, "query", "string"),
            latitude: pick(args, "latitude", "number"),
            longitude: pick(args, "longitude", "number"),
            location: pick(args, "location", "string"),
            count: pickIntInRange(args, "count", 1, 100, "brave_place_search"),
            radius: pick(args, "radius", "number"),
            country: pick(args, "country", "string"),
            searchLang: pick(args, "searchLang", "string"),
            units: pickEnum(args, "units", ["metric", "imperial"], "brave_place_search"),
            safeSearch: pickEnum(args, "safeSearch", ["moderate", "strict"], "brave_place_search"),
            spellcheck: pick(args, "spellcheck", "boolean"),
            category: pick(args, "category", "string"),
          };
          const data = await client.place(a);
          const pois = data?.results ?? [];
          const loc = data?.location ?? {};
          const coords = Array.isArray(loc.coordinates) ? loc.coordinates.join(", ") : "";
          const addressOf = (p: any): string => {
            const pa = p.postal_address;
            if (typeof pa === "string") return pa;
            return pa?.display_address ?? pa?.displayAddress ?? "";
          };
          const phoneOf = (p: any): string => {
            const c = p.contact;
            if (typeof c === "string") return c;
            if (Array.isArray(c)) return c[0]?.telephone ?? c[0]?.phone_number ?? "";
            return c?.telephone ?? c?.phone_number ?? "";
          };
          const today = (oh: any): string => {
            const cur = oh?.current_day;
            const entry = Array.isArray(cur) ? cur[0] : (cur ?? oh?.periods?.[0]);
            if (!entry) return "";
            return `${entry.abbr_name ?? entry.full_name ?? ""} ${entry.opens ?? ""}–${entry.closes ?? ""}`.trim();
          };
          const parts = [
            `Place search: ${a.query || "(explore mode)"}${loc.name ? `  ·  resolved: ${loc.name} (${loc.country ?? ""})${coords ? `, ${coords}` : ""}` : ""}`,
            `\nPlaces (${pois.length}):`,
            pois.map((p: any, i: number) => {
              const bits = [`${i + 1}. ${plain(p.title) || "(unnamed place)"}`];
              const addr = addressOf(p);
              if (addr) bits.push(`   ${addr}`);
              const phone = phoneOf(p);
              if (phone) bits.push(`   phone: ${phone}`);
              const r = p.rating;
              if (r && (r.rating_value ?? r.ratingValue ?? r.rating)) {
                const val = r.rating_value ?? r.ratingValue ?? r.rating;
                const count = r.review_count ?? r.reviewCount ?? r.reviews;
                bits.push(`   rating: ${val}${count ? ` (${count} reviews)` : ""}`);
              }
              const hours = today(p.opening_hours);
              if (hours) bits.push(`   today: ${hours}`);
              const price = p.price_range ?? p.priceLevel;
              if (price) bits.push(`   price: ${price}`);
              const cuisine = p.serves_cuisine ?? p.cuisine;
              if (Array.isArray(cuisine) && cuisine.length) bits.push(`   cuisine: ${cuisine.join(", ")}`);
              return bits.join("\n");
            }).join("\n"),
          ];
          return { content: [{ type: "text", text: parts.join("\n"), metadata: { ...meta, raw: data } }] };
        }
        case "brave_pois": {
          const ids: string[] = pick(args, "ids", "array") ?? [];
          if (!ids.length) {
            throw new BraveApiError("brave_pois requires a non-empty 'ids' array", "bad_request");
          }
          const data = await client.pois({
            ids,
            searchLang: pick(args, "searchLang", "string"),
            uiLang: pick(args, "uiLang", "string"),
            units: pickEnum(args, "units", ["metric", "imperial"], "brave_pois"),
          });
          const results: any[] = data?.results ?? [];
          const lines = results.map((p, i) => {
            const bits = [`${i + 1}. ${plain(p.title ?? p.name) || "(unnamed)"}`];
            const pa = p.postal_address ?? p.address;
            if (typeof pa === "string" && pa) bits.push(`   ${pa}`);
            const contact = p.contact;
            const phone = typeof contact === "string" ? contact : Array.isArray(contact) ? contact[0]?.telephone ?? "" : contact?.telephone ?? "";
            if (phone) bits.push(`   phone: ${phone}`);
            const r = p.rating;
            if (r && (r.rating_value ?? r.ratingValue ?? r.rating)) {
              const val = r.rating_value ?? r.ratingValue ?? r.rating;
              const count = r.review_count ?? r.reviewCount ?? r.reviews;
              bits.push(`   rating: ${val}${count ? ` (${count} reviews)` : ""}`);
            }
            const price = p.price_range ?? p.priceLevel;
            if (price) bits.push(`   price: ${price}`);
            const pics = p.pictures ?? p.photos;
            if (Array.isArray(pics) && pics.length) bits.push(`   ${pics.length} picture(s)`);
            return bits.join("\n");
          });
          return {
            content: [{
              type: "text",
              text: `${results.length} place detail record(s):\n${lines.join("\n") || "  (none)"}`,
              metadata: { ...meta, raw: data },
            }],
          };
        }
        case "brave_poi_descriptions": {
          const ids: string[] = pick(args, "ids", "array") ?? [];
          if (!ids.length) {
            throw new BraveApiError("brave_poi_descriptions requires a non-empty 'ids' array", "bad_request");
          }
          const data = await client.poiDescriptions({ ids });
          const results: any[] = data?.results ?? [];
          const lines = results.map((r, i) =>
            `${i + 1}. ${plain(r.title ?? "") || (r.id ?? "(unnamed)")}: ${clean(r.description) || "(no description)"}`);
          return {
            content: [{
              type: "text",
              text: `${results.length} place description(s):\n${lines.join("\n") || "  (none)"}`,
              metadata: { ...meta, raw: data },
            }],
          };
        }
        case "brave_rich_search": {
          const a: BraveRichArgs = { query: requiredStr(args, "query", "brave_rich_search") };
          const data = await client.rich(a);
          const results: any[] = data?.results ?? [];
          // Emit the vertical payload in a readable + machine-usable form.
          const text = `Rich search: ${a.query}  ·  vertical: ${data.vertical ?? "unknown"}\n\n` +
            `JSON payload:\n${JSON.stringify(results, null, 2)}`;
          return { content: [{ type: "text", text, metadata: { ...meta, raw: data, vertical: data.vertical } }] };
        }
        case "brave_llm_context": {
          const query = requiredStr(args, "query", "brave_llm_context");
          if (query.length > 400) {
            throw new BraveApiError("brave_llm_context: 'query' must be <=400 characters (got " + query.length + ")", "bad_request");
          }
          const a: BraveLlmContextArgs = {
            query,
            count: pickIntInRange(args, "count", 1, 50, "brave_llm_context"),
            country: pick(args, "country", "string"),
            searchLang: pick(args, "searchLang", "string"),
            maxTokens: pickIntInRange(args, "maximum_number_of_tokens", 1024, 32768, "brave_llm_context"),
            maxUrls: pickIntInRange(args, "maximum_number_of_urls", 1, 50, "brave_llm_context"),
            maxSnippets: pickIntInRange(args, "maximum_number_of_snippets", 1, 256, "brave_llm_context"),
            maxTokensPerUrl: pickIntInRange(args, "maximum_number_of_tokens_per_url", 1, 8192, "brave_llm_context"),
            maxSnippetsPerUrl: pickIntInRange(args, "maximum_number_of_snippets_per_url", 1, 100, "brave_llm_context"),
            thresholdMode: pickEnum(args, "context_threshold_mode", CONTEXT_THRESHOLD_MODES, "brave_llm_context"),
            enableLocal: pick(args, "enable_local", "boolean"),
            goggles: pick(args, "goggles", "string"),
          };
          const data = await client.llmContext(a);
          const grounding = data?.grounding ?? {};
          const generic: any[] = grounding.generic ?? [];
          const poi: any[] = grounding.poi ?? [];
          const map: any[] = grounding.map ?? [];
          const sources: Record<string, any> = data?.sources ?? {};
          const parts: string[] = [`Grounding for "${query}" (${generic.length} URL${generic.length === 1 ? "" : "s"}) within the requested token budget:`, ""];
          generic.forEach((g, i) => {
            const bits = [`${i + 1}. ${plain(g.title) || "(untitled)"} — ${plain(g.url)}`];
            const snips: any[] = Array.isArray(g.snippets) ? g.snippets : [];
            for (const s of snips.slice(0, 4)) bits.push(`   · ${plain(s).slice(0, 240)}`);
            parts.push(bits.join("\n"));
          });
          if (poi.length) parts.push(`\nLocal POI grounding (${poi.length}):\n${poi.map((p: any, i: number) => `  ${i + 1}. ${plain(p.title) || plain(p.name)}${p.url ? ` — ${plain(p.url)}` : ""}`).join("\n")}`);
          if (map.length) parts.push(`\nLocal map grounding (${map.length}): ${JSON.stringify(map).slice(0, 600)}`);
          if (Object.keys(sources).length) {
            const srcLines = Object.entries(sources).slice(0, 10).map((entry: [string, any]) => {
              const [url, s] = entry;
              return `  - ${url}${s?.hostname ? ` (${s.hostname})` : ""}${s?.age ? ` · age: ${s.age}` : ""}${s?.title ? ` · ${s.title}` : ""}`;
            }).join("\n");
            parts.push(`\nSources (first ${Math.min(Object.keys(sources).length, 10)}):\n${srcLines}`);
          }
          return {
            content: [{ type: "text", text: parts.join("\n"), metadata: { ...meta, raw: data, sources } }],
          };
        }
        case "brave_suggest": {
          const a: BraveSuggestArgs = {
            query: requiredStr(args, "query", "brave_suggest"),
            country: pick(args, "country", "string"),
          };
          const data = await client.suggest(a);
          // Suggest responses vary by plan; surface whatever list-like field the
          // API returns.
          const suggestions: any[] =
            (Array.isArray(data?.suggestions) ? data.suggestions :
            Array.isArray(data?.results) ? data.results :
            Array.isArray(data) ? data : []);
          const lines = suggestions.map((s, i) => {
            const t = typeof s === "string" ? s : (s?.title ?? s?.suggestion ?? s?.query ?? JSON.stringify(s));
            return `${i + 1}. ${plain(t)}`;
          }).join("\n");
          return {
            content: [{ type: "text", text: `${suggestions.length} suggestion(s):\n${lines || "(none)"}`, metadata: { ...meta, raw: data } }],
          };
        }
        case "brave_spellcheck": {
          const a = { query: requiredStr(args, "query", "brave_spellcheck") };
          const data = await client.spellcheck(a);
          const corrected =
            (typeof data?.spellcheck === "string" ? data.spellcheck :
            typeof data?.result === "string" ? data.result :
            typeof data?.corrected === "string" ? data.corrected :
            typeof data === "string" ? data : null);
          const changed = corrected && corrected !== a.query;
          const verdict = changed ? "corrected" : "unchanged";
          const text = corrected
            ? `Spellcheck "${a.query}" → "${corrected}" (${verdict})`
            : "No corrected form returned; using the original query.";
          return {
            content: [{ type: "text", text, metadata: { ...meta, raw: data, corrected } }],
          };
        }
        default:
          throw new Error(`Unknown tool: ${name}`);
      }
    } catch (error) {
      const message = error instanceof BraveApiError ? error.message : (error instanceof Error ? error.message : String(error));
      const category = error instanceof BraveApiError ? error.category : "error";
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
  console.error("Brave Search MCP Server running on stdio");
}

main().catch((error: unknown) => {
  console.error("Fatal error in main():", error instanceof Error ? error.message : String(error));
  process.exit(1);
});
