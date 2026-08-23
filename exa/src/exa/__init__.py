"""Exa search API skill for Prime Agent — advanced modes (v0.19).

Native REST wrapper over the Exa Search API (https://api.exa.ai) using
EXA_API_KEY, plus the WebSets API (https://api.exa.ai/websets). Unifies several
contexts behind ergonomic entry points:
  • ``await exa(query, ...)``    -> human-readable formatted summary (=== run)
  • ``await exa.search(...)``    -> structured SearchResults for pipelines
  • ``await exa.fetch(...)``     -> full page text / highlights / summary / extras
  • ``exa.answer/stream_answer`` -> compact citation-grounded (or SSE-streamed) answer
  • ``exa.agent(...)``           -> full agentic research run -> cited answer (+structured/cost)
  • ``exa.agent_chat(...)``      -> multi-turn agent loop (previous_run_id chaining)
  • ``exa.similar_to(URL)``      -> find_similar alias (more-like-this)
  • ``exa.webset_recall(wsid)``  -> read-side total-match coverage estimate
  • ``exa.search_to_csv(...)``   -> SearchResults.to_xlsx()/search_to_csv tabular exports
  • ``exa.monitor_*``            -> recurring change-detection searches (/monitors)
  • ``exa.webset_*``             -> bulk robust entity discovery (WebSets API)
  • ``exa.import/webhook/event_*``-> WebSets imports, delivery webhooks, audit events
  • ``exa.agent_trace(run_id)`` -> typed run-step audit (tools, sources, timing)
  • ``exa.stream_answer_source`` -> typed SSE deltas (per-chunk metering + sources)
  • ``exa.webset_snapshot/_diff`` -> client-side webset corpus snapshots + URL diff
  • ``exa.explain(url)``         -> one-call readable page explanation
  • ``exa.magic(query)``         -> deep search + answer + markdown pipeline
  • ``exa.github_repo(...)``     -> client-side GitHub repo profile (owner/stars/readme) (v0.19)
  • ``exa.code_search / hf_models / hf_discussions``   dev code & HF surfaces (v0.19)
  • ``exa.dev_help / find_used_by / dev_report``        dev questions, pkg users, dev README (v0.19)

This module intentionally tracks the live OpenAPI spec (api.exa.ai/openapi.json).
Notable facts the wrapper accommodates:

  * ``type`` accepts ``auto``, ``instant``, ``fast``, ``hybrid``, ``deep-lite``,
    ``deep``, ``deep-reasoning`` plus legacy ``keyword`` / ``neural`` / ``magic``
    (``magic`` -> ``deep``). ``semantic`` is NOT valid (400).
  * Rich per-result fields (image, favicon, publishedDate, summary, highlights,
    extras, entities) are only returned when requested via the ``contents``
    block — ask explicitly with ``with_highlights`` / ``with_summary`` /
    ``extras_links`` etc.
  * ``output_schema`` (+ ``system_prompt``) converts a search into a
    citation-grounded synthesized answer, surfaced by ``SearchResults.answer``
    and ``.answer_grounding``.
  * ``num_results`` is capped at 100 by the API; we validate client-side so a
    bad value fails fast with a clear message instead of a bare 400.
  * ``company`` / ``people`` categories reject publish-date filters and
    ``exclude_domains`` with a 400 — guarded locally with a clear error.
  * ``answer()``/``stream_answer()`` accept an undocumented ``citationFormat``
    (``citation_format=``) requesting rich per-source citation fields
    (``id``/``image``/``favicon``/``author``/``publishedDate``) — forwarded
    verbatim (v0.14). ``agent_chat`` takes a per-turn ``schemas=[...]`` list
    for progressive multi-schema pipelines; ``search_to_jsonl`` exports
    JSON-native lines without pandas.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Union

import httpx

# ---------------------------------------------------------------------------
# Clean engine: one client, one worker channel, deadline polls (no fixed sleep).
# ---------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

_ENGINE_LIMITS = httpx.Limits(max_connections=16, max_keepalive_connections=8)
_CLIENT = None
_WORKERS = None


def _http() -> "httpx.Client":
    """Process-lifetime client. Keep-alive pool; thread-safe for request()."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(
            limits=_ENGINE_LIMITS,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
    return _CLIENT


def _pool(n: int = 8) -> ThreadPoolExecutor:
    global _WORKERS
    if _WORKERS is None:
        _WORKERS = ThreadPoolExecutor(max_workers=n, thread_name_prefix="exa")
    return _WORKERS


def _retry_after_seconds(resp, fallback: float) -> float:
    if resp is None:
        return fallback
    raw = (resp.headers.get("Retry-After") or resp.headers.get("retry-after") or "").strip()
    if not raw:
        return fallback
    try:
        return max(0.05, min(float(raw), 8.0))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        return max(0.05, min(dt.timestamp() - time.time(), 8.0))
    except Exception:
        return fallback


def _fanout(jobs, *, concurrency: int = 8):
    """One-way: submit all (key, thunk), collect in order. No shared dict."""
    if not jobs:
        return []
    pool = _pool(max(8, int(concurrency or 8)))
    futs = [(key, pool.submit(thunk)) for key, thunk in jobs]
    out = []
    for key, fut in futs:
        try:
            out.append((key, True, fut.result()))
        except Exception as e:
            out.append((key, False, e))
    return out


def _poll_until(check, *, deadline: float, initial: float = 0.25, cap: float = 4.0):
    """Poll ``check()`` until it returns a truthy terminal value, or raise.

    Starts at ``initial`` (jobs often finish in <1s) and grows to ``cap``.
    ``check`` should return a falsey value while in-flight, or the result
    when terminal. No fixed-interval wasteful wait.
    """
    delay = max(0.05, float(initial))
    cap = max(delay, float(cap))
    last = None
    while True:
        last = check()
        if last:
            return last
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        time.sleep(min(delay, remaining, cap))
        delay = min(delay * 1.6, cap)




# ---------------------------------------------------------------------------
# Dual sync / async support (await works on any function-backed result)
# ---------------------------------------------------------------------------
# Every public function is callable BOTH ways:
#   value = fn(...)      -> the real result (a dict / str / SearchResults / ...)
#   value = await fn(...) -> the SAME real result (async-compatible)
#
# The IPython agent kernel calls `await skill.search(...)` in its notebooks.
# Previously the skills truly returned a SearchResults / dict (not a coroutine),
# so `await` raised: `TypeError: object dict can't be used in 'await' expression`.
# To make *both* call styles work, each public function's return value is made
# either awaitable *or* directly usable:
#
#   * dicts / lists / strings / bools / ints / floats are wrapped in a tiny
#     built-in subclass that additionally defines `__await__` (so awaiting
#     yields the plain unwrapped value, while direct use still passes every
#     isinstance / dict-operation an internal caller needs);
#   * the module's own container classes (SearchResults, Answer, ...) get an
#     `__await__` that returns the instance unchanged.
#
# Internal functions keep calling each other through the *same* wrapped names,
# so their cross-calls transparently receive the same dict / generic wrappers
# and keep working unmodified. Nothing here changes the wire behaviour.
# ---------------------------------------------------------------------------
import functools as _functools
import types as _types


def _identity_await(self):
    """__await__ for result classes: awaiting yields the instance itself."""
    if False:  # pragma: no cover - makes this a generator (required by await)
        yield
    return self


class _AsyncDict(dict):
    """A dict that is also awaitable; ``await x`` yields a plain ``dict``."""
    __slots__ = ()

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return dict(self)


class _AsyncStr(str):
    """A str that is also awaitable; ``await x`` yields the plain ``str``."""
    __slots__ = ()

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return str(self)


class _AsyncList(list):
    """A list that is also awaitable; ``await x`` yields a plain ``list``."""
    __slots__ = ()

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return list(self)


class _AsyncInt(int):
    """An int that is also awaitable; ``await x`` yields a plain ``int``."""
    __slots__ = ()

    def __new__(cls, v):
        return int.__new__(cls, int(v))

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return int(self)


class _AsyncFloat(float):
    """A float that is also awaitable; ``await x`` yields a plain ``float``."""
    __slots__ = ()

    def __new__(cls, v):
        return float.__new__(cls, float(v))

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return float(self)


class _AsyncBool(int):
    """A bool that is also awaitable; ``await x`` yields a plain ``bool``."""
    __slots__ = ()

    def __new__(cls, v):
        return int.__new__(cls, bool(v))


    def __str__(self):
        return str(bool(self))

    def __repr__(self):
        return repr(bool(self))

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return bool(self)


def _wrap_result(value):
    """Return a value that works both with and without ``await``."""
    if isinstance(value, dict):
        return _AsyncDict(value)
    if isinstance(value, str):
        return _AsyncStr(value)
    if isinstance(value, list):
        return _AsyncList(value)
    if isinstance(value, bool):
        return _AsyncBool(value)
    if isinstance(value, int):
        return _AsyncInt(value)
    if isinstance(value, float):
        return _AsyncFloat(value)
    # Any object that already knows how to be awaited (our own result classes,
    # or a swallowed coroutine) is returned untouched.
    if hasattr(value, "__await__"):
        return value
    # Fallback generic proxy for anything else (incl. None).
    return _AsyncScalar(value)


class _AsyncScalar(object):
    """Awaitable + mostly-transparent wrapper for any other type (incl. None)."""

    __slots__ = ("_v",)

    def __init__(self, v):
        object.__setattr__(self, "_v", v)

    def __await__(self):
        if False:  # pragma: no cover
            yield
        return self._v

    def __getattr__(self, name):
        return getattr(self._v, name)

    def __getitem__(self, k):
        return self._v[k]

    def __setitem__(self, k, val):
        self._v[k] = val

    def __iter__(self):
        return iter(self._v)

    def __contains__(self, item):
        return item in self._v

    def __len__(self):
        return len(self._v)

    def __bool__(self):
        return bool(self._v)

    def __str__(self):
        return str(self._v)

    def __repr__(self):
        return repr(self._v)

    def __float__(self):
        return float(self._v)

    def __int__(self):
        return int(self._v)

    def __eq__(self, o):
        return self._v == o

    def __ne__(self, o):
        return self._v != o

    def __hash__(self):
        return hash(self._v)

    def __call__(self, *a, **k):
        return self._v(*a, **k)

    def __format__(self, spec):
        return format(self._v, spec)


def _make_async(fn):
    """Wrap a sync public function so calls work with or without ``await``."""
    import functools

    @functools.wraps(fn)
    def _async_aware(*args, **kwargs):
        return _wrap_result(fn(*args, **kwargs))

    return _async_aware


def _apply_async_to(module_dict):
    """Replace every public function with an async-aware twin.

    Private helpers (leading `_`), classes, and inherently asynchronous /
    streaming callables are untouched:

      * generator functions (``yield`` in the body, e.g. ``stream_answer`` /
        ``stream_search``) keep their line-by-line streaming behaviour;
      * already ``async def`` coroutines / async generators are left alone.

    ``functools.wraps`` preserves each wrapped function's name/docstring/signature.
    """
    import inspect as _inspect
    for _name, _obj in list(module_dict.items()):
        if _name.startswith("_"):
            continue
        if not isinstance(_obj, _types.FunctionType):
            continue
        if getattr(_obj, "__name__", "") == "_async_aware":
            continue
        if _inspect.isgeneratorfunction(_obj) or _inspect.isasyncgenfunction(_obj) \
           or _inspect.iscoroutinefunction(_obj):
            continue
        module_dict[_name] = _make_async(_obj)


DEFAULT_API_URL = "https://api.exa.ai"
WEBSETS_API_URL = "https://api.exa.ai/websets"   # WebSets / imports / webhooks / events / team
__version__ = "0.19.0"

# Exa categories the API actually honors (from the current OpenAPI enum):
#   company | publication | news | personal site | financial report | people
# Legacy names (github / research paper / code) are mapped onto the nearest
# current category by _category_canon.
VALID_CATEGORIES = {
    "company", "publication", "news", "personal site", "financial report", "people",
    "github", "code", "person", "linkedin", "research paper",
}

# Valid `type` values accepted by the live API (api.exa.ai/openapi.json).
# `neural` is the legacy name for the semantic/neural mode; `hybrid` mixes
# neural + keyword for broad recall. `semantic` is NOT valid (400s at the API —
# use `neural` for the semantic mode). `magic` is a legacy alias we map to `deep`.
SEARCH_TYPES = {
    "auto", "instant", "fast", "deep-lite", "deep", "deep-reasoning",
    "keyword", "neural", "hybrid", "magic",
}

MODES = {
    "auto":          {"category": None,             "search_type": "auto", "hint": "General-purpose semantic + keyword search."},
    "code":          {"category": "github",         "search_type": "auto", "hint": "Find code, repos, API syntax, docs (was exa-code-search)."},
    "github":        {"category": "github",         "search_type": "auto", "hint": "Alias for `code` (GitHub repos, Gists)."},
    "company":       {"category": "company",        "search_type": "auto", "hint": "Company & market research: funding, headcount (was exa-company-research)."},
    "financial":     {"category": "financial report", "search_type": "auto", "hint": "SEC filings, 10-K/Q, earnings, IR decks (was exa-financial-report-search)."},
    "people":        {"category": "people",          "search_type": "auto", "hint": "Professional profiles / bios (was exa-people-search)."},
    "paper":         {"category": "publication",     "search_type": "auto", "hint": "Academic papers, arXiv, literature (was exa-research-paper-search)."},
    "research":      {"category": "publication",     "search_type": "auto", "hint": "Alias for `paper` / `publication`."},
    "publication":   {"category": "publication",     "search_type": "auto", "hint": "Academic/press articles (papers, preprints, journals)."},
    "personal-site": {"category": "personal site",   "search_type": "auto", "hint": "Personal blogs, portfolios (was exa-personal-site-search)."},
    "personal":      {"category": "personal site",   "search_type": "auto", "hint": "Alias for `personal-site`."},
    "news":          {"category": "news",            "search_type": "auto", "hint": "Fresh articles and announcements."},
    "lead":          {"category": "company",         "search_type": "auto", "hint": "Lead / prospect generation (was exa-lead-generation)."},
    "lead-generation": {"category": "company",       "search_type": "auto", "hint": "Alias for `lead`."},
    "websets":       {"category": "company",         "search_type": "auto", "hint": "Entity-list / collection mode (was exa-websets)."},
}


class ExaError(RuntimeError):
    """Raised when the Exa API rejects a request or an invalid option combo is given."""


class ExaAuthError(ExaError):
    """401: the API key is missing, invalid, or revoked. Re-run /login or re-export EXA_API_KEY."""


class ExaRateLimitError(ExaError):
    """429: you hit a rate limit or monthly credit/quota cap. Slow down or top up credits."""


class ExaBadRequestError(ExaError):
    """400/422: the request itself was invalid (bad param combo, bad schema, unsupported filter)."""


class ExaPlanError(ExaError):
    """403: the endpoint/feature is not enabled on the current plan (e.g. agent runs, monitors)."""


class ExaNotFoundError(ExaError):
    """404: the resource was not found (bad run/source id, or an endpoint the key cannot reach)."""


class ExaServerError(ExaError):
    """5xx: transient Exa-side failure. Retry with backoff; if it persists, contact Exa."""



_API_KEY_NAMES = ['EXA_API_KEY']
_API_LABEL = "exa"
_API_KEY_CACHE = {}

def _seek_api_key(names=None):
    from pathlib import Path
    names = names or _API_KEY_NAMES
    for n in names:
        v = os.environ.get(n, "")
        if v and not v.startswith("${"):
            return v
    home = Path.home()
    files = []
    pe = os.environ.get("PRIME_DOTENV", "")
    if pe:
        files.append(Path(pe))
    files += [
        home / ".prime" / "agent" / ".env",
        home / ".prime" / ".env",
        home / ".config" / "prime" / "env",
        home / ".env",
    ]
    try:
        from dotenv import dotenv_values
        for p in files:
            if p.exists() and p.is_file():
                d = dotenv_values(str(p))
                for n in names:
                    if d.get(n):
                        return d[n].strip()
    except Exception:
        pass
    stem = names[0].split("_")[0].lower()
    key_files = [p for p in (
        home / ".prime" / "agent" / "keys" / f"{stem}.key",
        home / ".prime" / "agent" / "keys" / f"{names[0].lower()}.key",
        home / ".prime" / "agent" / "keys" / "secrets.env",
        home / ".prime" / "keys" / f"{stem}.env",
        home / f".{stem}.env",
    )] + files
    for p in key_files:
        v = _read_key_file(p, names)
        if v:
            return v
    return ""

def _read_key_file(path, names):
    from pathlib import Path
    try:
        if not Path(path).is_file():
            return None
        raw = Path(path).read_text(errors="ignore")
    except (OSError, TypeError):
        return None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip() in names and v.strip():
                return v.strip().strip("'\"")
        elif len(line) >= 12 and "/" not in line and line not in names:
            return line.strip().strip("'\"")
    return None

def _get_api_key():
    label = _API_LABEL
    if label in _API_KEY_CACHE:
        return _API_KEY_CACHE[label]
    key = _seek_api_key(_API_KEY_NAMES)
    if not key:
        raise RuntimeError(
            f"{label} search is not configured: no API key "
            f"({', '.join(_API_KEY_NAMES)}) was found in the environment "
            f"or any key file. Ask the user to run /login (choose {label}) "
            f"or export {_API_KEY_NAMES[0]}; the skill picks it up automatically."
        )
    for n in _API_KEY_NAMES:
        os.environ.setdefault(n, key)
    _API_KEY_CACHE[label] = key
    return key


def _request(method: str, path: str, api_key: str, *, base: str = DEFAULT_API_URL,
             params=None, json_body=None, timeout=45.0, _retries=2) -> dict:
    """POST/GET to the Exa API, classifying errors for fast, actionable failures.

    Transient failures (429 rate-limit, 5xx) are retried up to ``_retries``
    times with short exponential backoff so a single throttled call does not
    kill a fan-out. Non-transient errors raise a precise subclass:
    ``ExaAuthError`` (401), ``ExaPlanError`` (403), ``ExaBadRequestError``
    (400/422), ``ExaRateLimitError`` (429, after retries), ``ExaServerError``
    (5xx, after retries).
    """
    import time
    url = f"{base}{path}"
    last_resp = None
    detail = None
    for attempt in range(_retries + 1):
        resp = _http().request(method, url, params=params, json=json_body,
                               headers={"x-api-key": api_key}, timeout=timeout)
        if resp.status_code < 400:
            return resp.json()
        last_resp = resp
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text[:500]
        status = resp.status_code
        # transient: retry then classify as rate/server after exhausting attempts
        if status in (429, 500, 502, 503, 504) and attempt < _retries:
            time.sleep(_retry_after_seconds(resp, 2 ** attempt * 0.5))
            continue
        if status == 404:
            raise ExaNotFoundError(
                f"Exa endpoint {path} returned 404: resource not found (bad id or "
                f"unsupported path). Check the run/document id. Detail: {detail}"
            )
        if status == 401:
            raise ExaAuthError(
                "Exa API key rejected (401). Your EXA_API_KEY is invalid, expired, or "
                "revoked. Re-run /login (choose exa) or export a fresh EXA_API_KEY. "
                f"Detail: {detail}"
            )
        if status == 403:
            raise ExaPlanError(
                f"Exa endpoint {path} returned 403: not enabled on the current plan. "
                f"Some features (e.g. /agent/runs, /monitors, websets) require a "
                f"specific plan. Contact hello@exa.ai to enable. Detail: {detail}"
            )
        if status in (400, 422):
            raise ExaBadRequestError(
                f"Exa rejected the request (400/422): the payload/params are invalid. "
                f"Check param combinations and output_schema. Detail: {detail}"
            )
        if status == 429:
            raise ExaRateLimitError(
                f"Exa rate limit / quota hit (429) after {_retries} retries. Slow down "
                f"or top up credits at dashboard.exa.ai. Detail: {detail}"
            )
        raise ExaServerError(
            f"Exa API transient failure ({status}) after {_retries} retries. Retry later; "
            f"if persistent, contact hello@exa.ai. Detail: {detail}"
        )
    # unreachable
    raise ExaServerError(f"Exa API request failed without a usable response. Detail: {detail}")


# ---------------------------------------------------------------------------
# Result + SearchResults: structured, pipeline-friendly containers
# ---------------------------------------------------------------------------
class Result:
    """A single Exa search/contents result with rich, JSON-friendly fields."""

    def __init__(self, raw: dict, index: int = 0):
        self.raw = raw
        self.index = index
        self.id: str = raw.get("id") or ""
        self.title: str = raw.get("title") or ""
        self.url: str = raw.get("url") or ""
        self.published_date: Optional[str] = raw.get("publishedDate")
        self.author: Optional[str] = raw.get("author")
        self.score: Optional[float] = raw.get("score")
        self.image: Optional[str] = raw.get("image")
        self.favicon: Optional[str] = raw.get("favicon")
        self.text: Optional[str] = raw.get("text")
        self.highlights: Optional[List[str]] = raw.get("highlights")
        self.highlight_scores: Optional[List[float]] = raw.get("highlightScores")
        self.summary: Optional[str] = raw.get("summary")
        self.entities: Optional[List[dict]] = raw.get("entities")
        self.extras: Optional[dict] = raw.get("extras")
        self.subpages: Optional[List[dict]] = raw.get("subpages")

    @property
    def snippet(self) -> str:
        if self.highlights:
            return self.highlights[0]
        if self.text:
            return self.text[:500]
        return ""

    def to_agent(self) -> dict:
        """Every unique field. Date keeps time when it is not midnight."""
        date = self.published_date or None
        if isinstance(date, str) and "T" in date and date.split("T", 1)[1].startswith("00:00:00"):
            date = date[:10]
        snippet = self.snippet or None
        summary = self.summary or None
        if summary and snippet and str(summary).strip() == str(snippet).strip():
            summary = None
        d = {
            "title": self.title,
            "url": self.url,
            "id": self.id if self.id and self.id != self.url else None,
            "date": date,
            "snippet": snippet,
            "summary": summary,
            "highlights": self.highlights,
            "author": self.author,
            "score": self.score,
            "entities": self.entities,
            "extras": self.extras,
            "text": self.text,
            "subpages": self.subpage_count or None,
            "image": self.image,
        }
        return {k: v for k, v in d.items() if v not in (None, "", [], {})}

    def to_dict(self) -> dict:
        """Plain, JSON-serializable dict of this result."""
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "published_date": self.published_date,
            "author": self.author,
            "score": self.score,
            "image": self.image,
            "favicon": self.favicon,
            "text": self.text,
            "highlights": self.highlights,
            "highlight_scores": self.highlight_scores,
            "summary": self.summary,
            "entities": self.entities,
            "extras": self.extras,
            "subpages": self.subpage_count,
            "snippet": self.snippet,
        }

    def to_entity(self, index: int = 0) -> Optional[dict]:
        """First (or indexed) structured entity profile of this result, normalized."""
        ents = self.entities or []
        if not ents:
            return None
        target = ents[index] if -len(ents) <= index < len(ents) else ents[0]
        return entity_summary(target)

    def entities_by_type(self) -> Dict[str, List[dict]]:
        """Group this result's entities by type (company/person/publication)."""
        out: Dict[str, List[dict]] = {}
        for e in self.entities or []:
            t = entity_type(e)
            out.setdefault(t, []).append(entity_summary(e))
        return out

    # -- subpage helpers (v0.11) ------------------------------------------
    @property
    def subpage_count(self) -> int:
        """Number of subpages returned for this result (``subpages`` array length)."""
        return len(self.subpages or [])

    def subpage_titles(self) -> List[str]:
        """Titles of this result's subpages, in order."""
        return [s.get("title") or "" for s in (self.subpages or [])]

    def subpage_urls(self) -> List[str]:
        """URLs of this result's subpages, in order."""
        return [s.get("url") or s.get("id") or "" for s in (self.subpages or [])]

    def subpage(self, index: int = 0) -> Optional[dict]:
        """Typed dict for one subpage (title / url / id / published_date / author /
        image / favicon), or ``None`` when there are no subpages."""
        sps = self.subpages or []
        if not sps:
            return None
        s = sps[index] if -len(sps) <= index < len(sps) else sps[0]
        return {
            "title": s.get("title"),
            "url": s.get("url") or s.get("id"),
            "id": s.get("id"),
            "published_date": s.get("publishedDate"),
            "author": s.get("author"),
            "image": s.get("image"),
            "favicon": s.get("favicon"),
        }

    def to_subpages(self) -> List[dict]:
        """All subpages as normalized typed dicts via :meth:`subpage`."""
        return [self.subpage(i) for i in range(self.subpage_count)]

    @property
    def domain(self) -> str:
        """Registrable domain of this result's URL (e.g. ``arxiv.org``)."""
        return _domain_of(self.url or self.id or "")

    @property
    def hostname(self) -> str:
        """Full hostname of the result's URL (e.g. ``news.mit.edu``)."""
        u = self.url or self.id or ""
        try:
            return urllib.parse.urlparse(u if "://" in u else "https://" + u).hostname or ""
        except ValueError:
            return ""

    def __repr__(self) -> str:
        bits = [self.title, self.url]
        if self.snippet:
            bits.append(self.snippet[:80])
        return " - ".join(x for x in bits if x)


def _domain_of(url: str) -> str:
    """Return the registrable domain (2nd-level label + TLD) of a URL.

    Simple and pragmatic: strips scheme/www then keeps the last two labels
    (e.g. ``arxiv.org``, ``mit.edu``, ``co.uk`` -> ``co.uk``). Good enough for
    grouping into source sites without a full public-suffix list.
    """
    host = url.split("//", 1)[-1].split("/", 1)[0].split("?")[0].split("#")[0]
    host = host.strip().rstrip(".")
    labels = [L for L in host.lower().split(".") if L]
    if not labels:
        return ""
    if len(labels) <= 2:
        return ".".join(labels)
    # naive 2-label registrable domain
    return ".".join(labels[-2:])


def entity_type(entity: dict) -> str:
    """Discriminator of a raw Exa ``entities[]`` record (company/person/publication/"").

    Falls back to ``unknown`` when the type discriminator is absent.
    """
    t = (entity or {}).get("type")
    if isinstance(t, dict):
        t = t.get("type") or t.get("const") or "unknown"
    return str(t or "unknown")


_ENTITY_PROFILE_KEYS = {
    "company": ("name", "foundedYear", "description", "workforce",
                "headquarters", "financials", "webTraffic", "research"),
    "person": ("name", "firstName", "lastName", "location",
               "workHistory", "educationHistory", "research"),
    "publication": ("title", "year", "date", "type", "language", "citationCount",
                    "authors", "referenceCount", "abstract", "doi"),
}


def entity_summary(entity: dict) -> dict:
    """Normalize one raw Exa ``entities[]`` record into a readable structured profile.

    Exa returns structured, LLM-backed entity profiles (``company`` / ``person`` /
    ``publication``) on entity-backed result categories, each with a **stable**
    library ``id`` (e.g. ``https://exa.ai/library/organization/...``) usable to
    cluster results about the same real-world entity across searches. The nested
    ``properties`` are flattened into a dict of the most useful snake_case
    fields; the raw record is kept under ``raw``.

    Returns ``{"id", "type", "name", ...profile fields, "raw"}``.
    """
    e = entity or {}
    props = e.get("properties") if isinstance(e.get("properties"), dict) else {}
    typ = entity_type(e)
    out: Dict[str, Any] = {
        "id": e.get("id"),
        "type": typ,
        "raw": e,
    }
    for k in ("name", "title"):
        if props.get(k) is not None:
            out["name"] = props.get(k)
            break
    keys = _ENTITY_PROFILE_KEYS.get(typ, list(props.keys()))
    for k in keys:
        if props.get(k) is not None:
            out[k] = props.get(k)
    return out


def entities(*, results=None) -> List[dict]:
    """Aggregate normalized entity profiles across a list of results/records.

    ``results`` may be a ``SearchResults``/list of ``Result``-like objects with
    an ``.entities`` field, or a list of raw dicts carrying ``entities``. Entity
    profiles are de-duplicated by stable library ``id`` (dedupe on ``id`` when
    present). Convenient for turning a result set into a structured roster.
    """
    seen: Dict[str, dict] = {}
    ordered: List[dict] = []
    for r in results or []:
        if isinstance(r, dict):
            ents = r.get("entities") or []
        else:
            ents = getattr(r, "entities", None) or []
        for e in ents:
            s = entity_summary(e)
            key = s.get("id") or (s.get("name") or "") + "|" + (s.get("type") or "")
            if key and key in seen:
                continue
            seen[key] = s
            ordered.append(s)
    return ordered


# ---------------------------------------------------------------------------
# Round-16 client-side composites (live-verified): entity_search, entity_schema,
# top_terms. These run real searches and post-process client-side, so they are
# deterministic and always work -- no reliance on inert backend params. The
# literal backend params this round probed (startCrawlDate / endCrawlDate on
# /search and /contents, autoprompt:false, similarityThreshold) are accepted by
# the API (HTTP 200) but reproduce identical result sets on every observed
# query == inert-accepted, so they are intentionally NOT exposed.
# ---------------------------------------------------------------------------

_ENTITY_TYPES = ("company", "person", "publication", "entity")


def entity_search(
    query: str,
    *,
    num_results: int = 8,
    mode: str = "auto",
    category: Optional[str] = None,
    search_type: Optional[str] = None,
    include_domains=None,
    exclude_domains=None,
    dedupe: bool = True,
    timeout: float = 45.0,
) -> dict:
    """Search the web and auto-extract a deduped roster of entity profiles.

    Runs a real ``search()`` then pulls every ``entities[]`` record across the
    top results, normalizes each with ``entity_summary()``, de-duplicates on the
    stable library ``id`` (or ``name|type``) and ranks the roster by how many
    distinct result pages mentioned each entity. Best used on an entity-backed
    category (``category="company"`` / ``"person"`` / ``"research paper"`` /
    etc.); in plain ``"auto"`` the API only returns entities when the results
    happen to carry them.

    Each profile is a normal ``entity_summary`` dict plus the ``_occurrences``
    (number of source pages that surfaced it). Returns a small batch dict for
    downstream pipelines.

    Returns:
        ``{"query", "category", "entity_count", "entities": [...],
          "total_cost"}``.
    """
    sr = search(
        query, num_results=num_results, mode=mode, category=category,
        search_type=search_type, include_domains=include_domains,
        exclude_domains=exclude_domains, timeout=timeout,
    )
    roster: Dict[str, dict] = {}
    for r in sr.results:
        for raw_e in (getattr(r, "entities", None) or []):
            try:
                s = entity_summary(raw_e)
            except Exception:
                continue
            key = s.get("id") or ((s.get("name") or "") + "|" + s.get("type", ""))
            if not key:
                continue
            if key in roster:
                roster[key]["_occurrences"] = roster[key].get("_occurrences", 1) + 1
                continue
            s["_occurrences"] = 1
            roster[key] = s
    ents = list(roster.values())
    ents.sort(key=lambda e: (-e.get("_occurrences", 0), e.get("type", ""),
                              e.get("name", "") or ""))
    return {
        "query": query,
        "category": category,
        "entity_count": len(ents),
        "entities": ents,
        "total_cost": sr.total_cost(),
    }


_KIND_SCHEMA = {
    "company": ("CompanyEntity", ("name", "foundedYear", "description",
                                  "workforce", "headquarters", "financials",
                                  "webTraffic", "research")),
    "person": ("PersonEntity", ("name", "firstName", "lastName", "location",
                                "workHistory", "educationHistory", "research")),
    "publication": ("ResearchPaperEntity", ("title", "year", "date", "type",
                                            "language", "citationCount",
                                            "authors", "referenceCount",
                                            "abstract", "doi")),
}


def entity_schema(entity: Optional[dict] = None) -> dict:
    """Explore the attribute schema of an Exa entity (entity schema explorer).

    A standalone discover/review tool: given a raw ``entities[]`` record (or
    ``{}`` to explore a type's full field surface without a concrete sample),
    returns which of the entity type's known attributes are present in the
    sample (with their JSON-kind), which are absent, and any unexpected
    extra keys. Nested dict values (e.g. ``workforce``, ``headquarters``,
    ``workHistory``) are reported as ``dict`` so you know to drill in with the
    raw record. Complements ``entity_summary`` (which flattens values) and
    ``entity_type`` (which only discriminates the kind).

    Args:
        entity: A raw Exa entity record (or ``None``/``{}`` to get the type's
            field map with everything "missing").

    Returns:
        ``{"type", "schema_model", "known_attributes", "present_attributes",
    "missing_optional_attributes",
    "extra_attributes"}``.
    """
    e = entity or {}
    typ = entity_type(e)
    props = e.get("properties") if isinstance(e.get("properties"), dict) else {}
    if typ not in _KIND_SCHEMA:
        # discovery mode: no concrete sample (or an unsupported kind) -> expose
        # the full field surface for every entity kind so the user can explore.
        if typ in ("unknown", "") and not props:
            return {
                "type": "unknown",
                "schema_model": "Entity",
                "known_attributes": {
                    kind: list(fields) for kind, (_m, fields) in _KIND_SCHEMA.items()
                },
                "present_attributes": {},
                "missing_optional_attributes": [],
                "extra_attributes": {},
                "discovery": True,
            }
        typ = "company"
    schema_model, known = _KIND_SCHEMA[typ]
    present: Dict[str, str] = {}
    missing: List[str] = []
    for k in known:
        v = props.get(k)
        if v is not None and v != "" and v not in ([], {}):
            present[k] = "dict" if isinstance(v, dict) else (
                "list" if isinstance(v, list) else type(v).__name__)
        else:
            missing.append(k)
    extra = {
        k: ("dict" if isinstance(v, dict) else "list" if isinstance(v, list)
            else type(v).__name__)
        for k, v in props.items()
        if k not in known and v is not None and v != "" and v not in ([], {})
    }
    return {
        "type": typ,
        "schema_model": schema_model,
        "known_attributes": list(known),
        "present_attributes": present,
        "missing_optional_attributes": missing,
        "extra_attributes": extra,
    }


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "for", "on",
    "with", "at", "from", "by", "as", "is", "are", "was", "were", "be", "been",
    "it", "its", "that", "this", "these", "those", "which", "what", "how",
    "why", "when", "who", "whom", "not", "no", "nor", "you", "your", "yours",
    "their", "theirs", "our", "ours", "we", "they", "he", "she", "him", "her",
    "i", "me", "my", "mine", "about", "into", "via", "using", "use", "used",
    "uses", "new", "latest", "best", "top", "vs", "versus", "more", "most",
    "do", "does", "did", "get", "gets", "got", "can", "could", "will", "would",
    "should", "shall", "than", "then", "there", "here", "over", "under",
    "after", "before", "during", "between", "such", "only", "so", "all",
    "some", "any", "each", "every", "both", "few", "many", "nbsp",
}


def _tokenise(text: str) -> List[str]:
    raw = (text or "").lower().replace("_", " ").replace("-", " ").replace("/", " ")
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", raw)
            if w not in _STOPWORDS and len(w) > 2]


def top_terms(
    query: str,
    *,
    num_results: int = 10,
    mode: str = "auto",
    category: Optional[str] = None,
    exclude_domains=None,
    stopwords: Optional[set] = None,
    top_n: int = 12,
    timeout: float = 45.0,
) -> dict:
    """Extract the recurring keyword and domain clusters across a query's hits.

    Client-side TF/domain clustering: runs a ``search()`` then (a) tokenises
    every result title (stop-word stripped) and tallies term frequencies, and
    (b) groups results by their registrable ``domain``. This is a cheap, fully
    local alternative to a second LLM pass for getting the "shape" of a result
    set — e.g. discovering the dominant vocabulary and the handful of source
    sites behind a topic.

    Args:
        query: The search query whose result set is clustered.
        num_results: How many results to run the search for (1..100).
        stopwords: Extra terms to ignore (always merged over the built-in
            English stopword list).
        top_n: How many top terms to keep.

    Returns:
        ``{"query", "total_results", "top_terms": [{"term","count"}, ...],
    "domain_clusters": [{"domain","results"}...], "total_cost"}``.
    """
    sr = search(query, num_results=num_results, mode=mode, category=category,
                exclude_domains=exclude_domains, timeout=timeout)
    tf: Counter = Counter()
    dom: Counter = Counter()
    skips = set(stopwords) if stopwords else set()
    for r in sr.results:
        tf.update(w for w in _tokenise(getattr(r, "title", "") or "") if w not in skips)
        d = getattr(r, "domain", None) or ""
        if d:
            dom[d] += 1
    return {
        "query": query,
        "total_results": len(sr.results),
        "top_terms": [{"term": w, "count": c} for w, c in tf.most_common(top_n)],
        "domain_clusters": [{"domain": d, "count": c} for d, c in dom.most_common()],
        "total_cost": sr.total_cost(),
    }


class SearchResults:
    """Structured search response: query + Results + metadata + synthesis.

    Behaves like a list (iteration, indexing, len) while exposing request
    metadata (``request_id``, ``cost_dollars``), synthesis helpers (``answer``,
    ``answer_grounding``) and pipeline helpers (``to_dicts``, ``to_json``).
    """

    def __init__(self, query, results, *, raw=None, request_id="", cost_dollars=None,
                 output=None, resolved_search_type="", search_time_ms=None):
        self.query = query
        self.results = list(results)
        self.raw = raw
        self.request_id = request_id or (raw or {}).get("requestId") or ""
        self.cost_dollars = cost_dollars if cost_dollars is not None else (raw or {}).get("costDollars")
        self.output = output if output is not None else (raw or {}).get("output")
        self.resolved_search_type = resolved_search_type or (raw or {}).get("resolvedSearchType") or ""
        st = search_time_ms if search_time_ms is not None else (raw or {}).get("searchTime")
        self.search_time_ms: Optional[float] = float(st) if st is not None else None

    # -- request metadata / cost helpers (v0.11) ----------------------------
    @property
    def search_time(self) -> Optional[float]:
        """Round-trip search latency in milliseconds (``searchTime``). ``None`` when absent."""
        return self.search_time_ms

    def total_cost(self) -> Optional[float]:
        """Total estimated USD cost of this request (``costDollars.total``).

        Returns ``None`` when the API omitted cost (e.g. merged/aggregate results).
        """
        if not self.cost_dollars:
            return None
        t = self.cost_dollars.get("total")
        return float(t) if t is not None else None

    def cost_breakdown(self) -> dict:
        """Per-mode estimated cost breakdown.

        The API reports the cost either flat (``{"total": 0.012}``) or nested under
        ``costDollars.search`` (``{"neural": 0.007, "keyword": 0.005, ...}``) when the
        request touched multiple retriever modes (deep, hybrid, etc.). This returns a
        ``{"total": ..., "modes": {...}}`` dict whether flat or nested — never raises.
        """
        cd = self.cost_dollars or {}
        total = cd.get("total")
        modes = dict((cd.get("search") or {})) if isinstance(cd.get("search"), dict) else {}
        # a bare flat total that isn't broken out: expose it as an "overall" mode
        if not modes and total is not None:
            modes["overall"] = total
        return {"total": float(total) if total is not None else None, "modes": modes}

    def cost_report(self) -> str:
        """One-line human-readable cost summary, e.g.
        ``"$0.012 total (modes: neural $0.007)"`` — or ``"cost not reported"``."""
        cd = self.cost_dollars or {}
        total = cd.get("total")
        modes = (cd.get("search") or {}) if isinstance(cd.get("search"), dict) else {}
        if total is None and not modes:
            return "cost not reported"
        parts = []
        if total is not None:
            parts.append(f"${float(total):.4f} total")
        if modes:
            parts.append("modes: " + ", ".join(f"{k} ${float(v):.4f}" for k, v in modes.items()))
        return " / ".join(parts)

    def __iter__(self):
        return iter(self.results)

    def __getitem__(self, i):
        return self.results[i]

    def __len__(self):
        return len(self.results)

    def __repr__(self) -> str:
        extra = []
        if self.search_time_ms is not None:
            extra.append(f"time={self.search_time_ms:.0f}ms")
        if self.total_cost() is not None:
            extra.append(f"cost=${self.total_cost():.4f}")
        suffix = f" {{{', '.join(extra)}}}" if extra else ""
        return f"<SearchResults query={self.query!r} n={len(self.results)} request_id={self.request_id}{suffix}>"

    def to_dicts(self) -> List[dict]:
        return [r.to_dict() for r in self.results]

    def to_agent(self) -> dict:
        """Query + hits + answer/grounding/cost when present. Nothing invented, nothing dropped."""
        out = {
            "query": self.query,
            "results": [r.to_agent() for r in self.results],
        }
        if self.answer:
            out["answer"] = self.answer
        grounding = self.answer_grounding
        if grounding:
            out["grounding"] = grounding
        cost = self.total_cost()
        if cost:
            out["cost"] = cost
        if self.request_id:
            out["request_id"] = self.request_id
        return out

    def to_meta(self) -> dict:
        """Render the request-level metadata (request_id, latency, cost) as a dict."""
        return {
            "query": self.query,
            "count": len(self.results),
            "request_id": self.request_id,
            "resolved_search_type": self.resolved_search_type or "auto",
            "search_time_ms": self.search_time_ms,
            "cost_dollars": self.cost_dollars,
        }

    def to_json(self, indent: int = 2, *, include_meta: bool = False) -> str:
        """JSON of the results (``include_meta=True`` wraps them with request metadata)."""
        if include_meta:
            return json.dumps({"metadata": self.to_meta(), "results": self.to_dicts()}, indent=indent)
        return json.dumps(self.to_dicts(), indent=indent)

    def to_markdown(self, *, with_metadata: bool = True) -> str:
        """Render as reference-markdown (refl-markdown style) for docs/journals.

        Results become a numbered list with bold titles, linked URLs, and any
        snippet/summary on separate lines. ``with_metadata`` prepends the query
        and a "Sources:" heading.
        """
        parts = []
        if with_metadata:
            parts.append(f"# {self.query}")
        if self.answer:
            parts.append(str(self.answer))
        for r in self.results:
            title = r.title or r.url or r.id or "(untitled)"
            url = r.url or r.id or ""
            date = (r.published_date or "")[:10]
            head = f"{r.index}. [{title}]({url})" if url else f"{r.index}. {title}"
            if date:
                head += f" · {date}"
            parts.append(head)
            text = r.snippet or r.summary
            if text:
                parts.append(f"   {str(text)[:240]}")
        return "\n".join(parts)


    @property
    def answer(self) -> Optional[Any]:
        """Synthesized answer ``output.content`` (from ``output_schema``), if any."""
        return self.output.get("content") if self.output else None

    @property
    def answer_grounding(self) -> Optional[List[dict]]:
        """Field-level citations + confidence for ``answer``."""
        return self.output.get("grounding") if self.output else None

    def entities(self) -> List[dict]:
        """All structured entity profiles across results, deduplicated by id.

        Normalizes every result's ``entities`` into readable profiles (company /
        person / publication). See :func:`entity_summary`.
        """
        return entities(results=self.results)

    def domains(self) -> Dict[str, int]:
        """Registrable-domain -> result-count frequency map across results."""
        counts: Dict[str, int] = {}
        for r in self.results:
            d = r.domain
            if d:
                counts[d] = counts.get(d, 0) + 1
        return counts

    def top_domains(self, n: int = 10) -> List[tuple]:
        """Most frequent source domains, sorted descending: [(domain, count)].

        Handles ties by stable original order of first appearance.
        """
        counts = self.domains()
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]

    def to_dataframe(self, *, include_entities: bool = False):
        """Return a ``pandas.DataFrame`` of the results (title/url/domain/
        published_date/author/summary/snippet/score).

        ``pandas`` is imported lazily so this skill works without it until you
        call this method. Each row is one result; ``entities``/``extras`` are
        omitted by default (keep JSON blobs out of the table) unless you opt in.
        """
        import pandas as pd  # lazy import: only needed for tabular output
        rows = []
        for r in self.results:
            row = {
                "title": r.title,
                "url": r.url,
                "domain": r.domain,
                "hostname": r.hostname,
                "published_date": r.published_date,
                "author": r.author,
                "score": r.score,
                "snippet": r.snippet,
                "summary": r.summary,
                "highlights": r.highlights,
            }
            if include_entities:
                row["entities"] = r.entities
            rows.append(row)
        return pd.DataFrame(rows)

    def to_xlsx(self, path, *, include_entities: bool = False):
        """Write the results to an ``.xlsx`` workbook (one result per row).

        Requires the ``openpyxl`` writer (pandas), imported lazily. Each row is
        one {title, url, domain, hostname, published_date, author, score,
        snippet, summary, highlights}; pass ``include_entities=True`` to add a
        raw ``entities`` JSON column.

        Args:
            path: destination path (``.xlsx``).
            include_entities: add the raw ``entities`` JSON column.

        Returns:
            ``path`` (str) so it can be chained/returned easily.
        """
        self.to_dataframe(include_entities=include_entities).to_excel(path, index=False)
        return path


# ---------------------------------------------------------------------------
# Parameter building + validation
# ---------------------------------------------------------------------------
def _category_canon(category: Optional[str]) -> Optional[str]:
    """Map a user-facing category/mode token to the canonical API category."""
    if category is None:
        return None
    c = str(category).strip().lower().replace(" ", "")
    table = {
        "code": "github", "github": "github",
        "research": "publication", "researchpaper": "publication", "paper": "publication",
        "academic": "publication", "arxiv": "publication", "auteurs": "publication",
        "financial": "financial report", "financialreport": "financial report", "10k": "financial report",
        "company": "company", "companies": "company", "lead": "company",
        "people": "people", "person": "people", "cv": "people", "linkedin": "people",
        "news": "news",
        "personalsite": "personal site", "blog": "personal site", "portfolio": "personal site",
    }
    if c in table:
        return table[c]
    for valid in VALID_CATEGORIES:
        if c == valid.replace(" ", ""):
            return valid
    print(f"[exa] Category '{category}' is not one Exa supports; ignoring (none applied).")
    return None


def _resolve_search_type(search_type: Optional[str], default: str) -> str:
    if search_type is None:
        return default
    st = str(search_type).strip().lower()
    if st == "magic":
        st = "deep"  # legacy name for the deep semantic answer mode
    if st not in SEARCH_TYPES:
        raise ExaError(f"Unknown search_type '{search_type}'. Options: {', '.join(sorted(SEARCH_TYPES))}")
    return st


def _mode_args(mode: str, category: Optional[str], search_type: Optional[str]):
    preset = MODES.get(mode)
    if preset is None:
        raise ValueError(f"Unknown mode '{mode}'. Call `await exa.modes()` for the list.")
    resolved_category = category if category is not None else preset.get("category")
    resolved_search_type = _resolve_search_type(search_type, preset.get("search_type", "auto"))
    return resolved_category, resolved_search_type


def _validate_search_params(num_results, max_characters, category,
                            start_published_date, end_published_date, exclude_domains):
    if num_results < 1:
        raise ExaError(f"num_results must be >= 1 (got {num_results}).")
    if num_results > 100:
        raise ExaError(
            f"num_results must be <= 100 (got {num_results}). The current Exa API "
            "caps public searches at 100 results; to fetch hundreds/thousands "
            "contact hello@exa.ai to raise your plan. You can also page with "
            "start_published_date/end_published_date or additional_queries."
        )
    if max_characters is not None and max_characters > 10000:
        raise ExaError(f"max_characters must be <= 10000 (got {max_characters}). The API caps "
                       "per-result text at 10000 characters.")
    if category in ("company", "people"):
        if start_published_date or end_published_date:
            raise ExaError(
                f"Category '{category}' does not support published-date filters "
                "(start_published_date / end_published_date) — the API returns a "
                "400. Drop the dates or use a different category (e.g. 'news')."
            )
        if exclude_domains:
            raise ExaError(
                f"Category '{category}' does not support exclude_domains — the API "
                "returns a 400. Use include_domains to restrict results instead."
            )


def _contents_block(*, with_text=False, max_characters=None, text_verbosity=None,
                    with_highlights=False, highlights_query=None, highlights_max=None,
                    with_summary=False, summary_query=None, summary_schema=None,
                    extras_links=0, extras_image_links=0, extras_rich_links=0,
                    extras_rich_image_links=0, extras_code_blocks=0, max_age_hours=None,
                    subpages=0, subpage_target=None, livecrawl=False, livecrawl_timeout=None,
                    include_sections=None, exclude_sections=None, include_html_tags=False) -> dict:
    """Build an Exa ``contents`` block, only including keys that were set.

    Used identically for /search (nested under ``contents``) and /contents
    (keys at the top level). Only requested keys are emitted so payloads stay
    small and cost-friendly.
    """
    c: dict[str, Any] = {}

    if with_text or max_characters or text_verbosity:
        if with_text and not max_characters and not text_verbosity:
            # plain full-text request (no size cap) -> contents.text: true
            c["text"] = True
        else:
            t: dict[str, Any] = {}
            if max_characters:
                t["maxCharacters"] = max_characters
            if text_verbosity:
                t["verbosity"] = text_verbosity
            if include_html_tags:
                t["includeHtmlTags"] = True
            if include_sections:
                t["includeSections"] = include_sections
            if exclude_sections:
                t["excludeSections"] = exclude_sections
            c["text"] = t

    if with_highlights:
        if highlights_query or highlights_max:
            h: dict[str, Any] = {}
            if highlights_query:
                h["query"] = highlights_query
            if highlights_max:
                h["maxCharacters"] = highlights_max
            c["highlights"] = h
        else:
            c["highlights"] = True

    if with_summary:
        if summary_query or summary_schema:
            s: dict[str, Any] = {}
            if summary_query:
                s["query"] = summary_query
            if summary_schema:
                s["schema"] = summary_schema
            c["summary"] = s
        else:
            c["summary"] = True

    ext: dict[str, Any] = {}
    if extras_links:           ext["links"] = extras_links
    if extras_image_links:     ext["imageLinks"] = extras_image_links
    if extras_rich_links:      ext["richLinks"] = extras_rich_links
    if extras_rich_image_links: ext["richImageLinks"] = extras_rich_image_links
    if extras_code_blocks:     ext["codeBlocks"] = extras_code_blocks
    if ext:
        c["extras"] = ext

    if max_age_hours is not None:
        c["maxAgeHours"] = max_age_hours
    if subpages:
        c["subpages"] = subpages
    if subpage_target:
        c["subpageTarget"] = subpage_target
    if livecrawl:
        c["livecrawl"] = livecrawl
    if livecrawl_timeout:
        c["livecrawlTimeout"] = livecrawl_timeout
    return c


# ---------------------------------------------------------------------------
# Public entry: search()  ->  structured SearchResults
# ---------------------------------------------------------------------------
def search(
    query: str,
    *,
    mode: str = "auto",
    num_results: int = 5,
    search_type: Optional[str] = None,
    category: Optional[str] = None,
    use_autoprompt: bool = False,
    start_published_date: Optional[str] = None,
    end_published_date: Optional[str] = None,
    start_crawl_date: Optional[str] = None,
    end_crawl_date: Optional[str] = None,
    include_domains=None,
    exclude_domains=None,
    text_include=None,
    text_exclude=None,
    # rich-content opts ----------------------------------------------------
    with_text: bool = False,
    max_characters: Optional[int] = None,
    text_verbosity: Optional[str] = None,
    with_highlights: bool = False,
    highlights_query: Optional[str] = None,
    highlights_max_characters: Optional[int] = None,
    with_summary: bool = False,
    summary_query: Optional[str] = None,
    summary_schema: Optional[dict] = None,
    extras_links: int = 0,
    extras_image_links: int = 0,
    extras_rich_links: int = 0,
    extras_rich_image_links: int = 0,
    extras_code_blocks: int = 0,
    max_age_hours: Optional[int] = None,
    subpages: int = 0,
    subpage_target=None,
    # semantic section / HTML / livecrawl controls (advanced content) -----
    include_sections=None, exclude_sections=None, include_html_tags: bool = False,
    livecrawl: Optional[str] = None, livecrawl_timeout: Optional[int] = None,
    # synthesis / citation-ready answer ------------------------------------
    output_schema: Optional[dict] = None,
    system_prompt: Optional[str] = None,
    additional_queries=None,
    user_location: Optional[str] = None,
    moderation: bool = False,
    compliance: Optional[str] = None,
    context: Optional[str] = None,
    stream: bool = False,
    timeout: float = 60.0,
) -> SearchResults:
    """Advanced Exa search returning a structured ``SearchResults``.

    This is the pipeline-friendly entry point: ``await exa.search(query)``
    returns a ``SearchResults`` whose ``results`` are rich ``Result`` objects
    exposing ``to_dict()``; the response also gives ``to_dicts()`` /
    ``to_json()`` and synthesis helpers (``answer``, ``answer_grounding``).

    The ``with_*`` options pull per-result content:
      * ``with_text=True`` + ``max_characters``  -> full page text
      * ``with_highlights`` (+ ``highlights_query``) -> relevant snippets
      * ``with_summary`` (+ ``summary_query`` / ``summary_schema``) -> LLM summary
      * ``extras_links`` / ``extras_image_links`` / ``extras_code_blocks`` ->
        outbound links / images / code blocks per result
      * ``max_age_hours`` -> freshness (0 = fresh crawl, -1 = cache only)

    For a citation-grounded synthesized answer, pass ``output_schema`` (a JSON
    Schema dict or plain shape) and optionally ``system_prompt``. The result
    ``.answer`` holds the synthesized content and ``.answer_grounding`` the
    per-field source citations.

    Returns:
        A SearchResults (iterable of Result, with metadata + synthesis).
    """
    resolved_category, resolved_search_type = _mode_args(mode, category, search_type)
    cat = _category_canon(resolved_category)
    _validate_search_params(num_results, max_characters if (with_text or max_characters) else None,
                            cat, start_published_date, end_published_date, exclude_domains)
    key = _get_api_key()

    body: dict[str, Any] = {"query": query, "numResults": num_results, "type": resolved_search_type}
    if use_autoprompt:
        body["autoprompt"] = True
    if cat:
        body["category"] = cat
    if start_published_date: body["startPublishedDate"] = start_published_date
    if end_published_date:   body["endPublishedDate"] = end_published_date
    if start_crawl_date:     body["startCrawlDate"] = start_crawl_date
    if end_crawl_date:       body["endCrawlDate"] = end_crawl_date
    if include_domains:      body["includeDomains"] = include_domains
    if exclude_domains:      body["excludeDomains"] = exclude_domains

    # text filter sub-block (search, not content extraction)
    tf: dict[str, Any] = {}
    if text_include: tf["include"] = text_include
    if text_exclude: tf["exclude"] = text_exclude
    if tf:
        body["text"] = tf

    contents = _contents_block(
        with_text=with_text,
        max_characters=(max_characters if max_characters else None),
        text_verbosity=text_verbosity,
        with_highlights=with_highlights, highlights_query=highlights_query,
        highlights_max=highlights_max_characters, with_summary=with_summary,
        summary_query=summary_query, summary_schema=summary_schema,
        extras_links=extras_links, extras_image_links=extras_image_links,
        extras_rich_links=extras_rich_links, extras_rich_image_links=extras_rich_image_links,
        extras_code_blocks=extras_code_blocks,
        max_age_hours=max_age_hours,
        subpages=subpages, subpage_target=subpage_target,
        include_sections=include_sections, exclude_sections=exclude_sections,
        include_html_tags=include_html_tags, livecrawl=livecrawl,
        livecrawl_timeout=livecrawl_timeout,
    )
    if contents:
        body["contents"] = contents

    if output_schema:
        body["outputSchema"] = output_schema
    if system_prompt:
        body["systemPrompt"] = system_prompt
    if additional_queries:
        body["additionalQueries"] = additional_queries
    if user_location:
        body["userLocation"] = user_location
    if moderation:
        body["moderation"] = True
    if compliance:
        body["compliance"] = compliance
    if context:
        body["context"] = context
    if stream:
        body["stream"] = True

    data = _request("POST", "/search", key, json_body=body, timeout=timeout)
    results = [Result(r, i + 1) for i, r in enumerate(data.get("results", []))]
    return SearchResults(query=query, results=results, request_id=data.get("requestId"),
                         cost_dollars=data.get("costDollars"), output=data.get("output"),
                         resolved_search_type=data.get("resolvedSearchType"),
                         search_time_ms=data.get("searchTime"), raw=data)


# ---------------------------------------------------------------------------
# Public entry: run()  (readable)   ===  await exa(...)
# ---------------------------------------------------------------------------
def _fmt_result(r: Result) -> List[str]:
    lines = []
    lines.append(f"{r.index}. {r.title}")
    if r.url:           lines.append(f"   {r.url}")
    if r.published_date: lines.append(f"   published: {r.published_date}")
    if r.author:        lines.append(f"   author:    {r.author}")
    if r.image:         lines.append(f"   image:     {r.image}")
    if r.favicon:       lines.append(f"   favicon:   {r.favicon}")
    snip = r.snippet
    if snip:            lines.append(f"   {snip}")
    if r.summary and len(snip) < 200:
        lines.append(f"   -- summary --")
        lines.append(f"   {r.summary}")
    return lines


def _fmt(results) -> str:
    block = []
    for r in results:
        block.extend(_fmt_result(r))
    return "\n".join(block)


def brief(query: str, **kwargs) -> dict:
    """Token-budgeted search dict for a model turn (title/url/snippet/score)."""
    return search(query, **kwargs).to_agent()


def run(
    query: str,
    *,
    mode: str = "auto",
    num_results: int = 5,
    search_type: Optional[str] = None,
    category: Optional[str] = None,
    use_autoprompt: bool = False,
    start_published_date: Optional[str] = None,
    end_published_date: Optional[str] = None,
    start_crawl_date: Optional[str] = None,
    end_crawl_date: Optional[str] = None,
    include_domains=None,
    exclude_domains=None,
    text_include=None,
    text_exclude=None,
    with_text: bool = False,
    max_characters: int = 500,
    text_verbosity: Optional[str] = None,
    with_highlights: bool = False,
    highlights_query: Optional[str] = None,
    highlights_max_characters: Optional[int] = None,
    with_summary: bool = False,
    summary_query: Optional[str] = None,
    summary_schema: Optional[dict] = None,
    extras_links: int = 0,
    extras_image_links: int = 0,
    extras_rich_links: int = 0,
    extras_rich_image_links: int = 0,
    extras_code_blocks: int = 0,
    max_age_hours: Optional[int] = None,
    subpages: int = 0,
    subpage_target=None,
    include_sections=None, exclude_sections=None, include_html_tags: bool = False,
    livecrawl: Optional[str] = None, livecrawl_timeout: Optional[int] = None,
    output_schema: Optional[dict] = None,
    system_prompt: Optional[str] = None,
    additional_queries=None,
    user_location: Optional[str] = None,
    moderation: bool = False,
    compliance: Optional[str] = None,
    context: Optional[str] = None,
    timeout: float = 30.0,
) -> str:
    """Advanced Exa web search returning a human-readable numbered list.

    This is the default entry -- ``await exa(query, ...)`` is ``run()``.
    For structured pipeline-friendly output, use ``await exa.search(...)``.

    The returned string is a numbered list of title / URL / optional published
    date / author / snippet (and summary when requested) per result.

    All options behave the same as in ``search()``. Pass ``output_schema``
    (a JSON Schema dict) and/or ``system_prompt`` for a citation-grounded
    synthesized answer, appended below the list as ``--- synthesized answer ---``.

    Returns:
        A human-readable numbered list of results.
    """
    res = search(
        query, mode=mode, num_results=num_results, search_type=search_type,
        category=category, use_autoprompt=use_autoprompt,
        start_published_date=start_published_date, end_published_date=end_published_date,
        start_crawl_date=start_crawl_date, end_crawl_date=end_crawl_date,
        include_domains=include_domains, exclude_domains=exclude_domains,
        text_include=text_include, text_exclude=text_exclude,
        with_text=with_text, max_characters=(max_characters if with_text else None),
        text_verbosity=text_verbosity,
        with_highlights=with_highlights, highlights_query=highlights_query,
        highlights_max_characters=highlights_max_characters,
        with_summary=with_summary, summary_query=summary_query,
        summary_schema=summary_schema,
        extras_links=extras_links, extras_image_links=extras_image_links,
        extras_rich_links=extras_rich_links, extras_rich_image_links=extras_rich_image_links,
        extras_code_blocks=extras_code_blocks,
        max_age_hours=max_age_hours, subpages=subpages, subpage_target=subpage_target,
        include_sections=include_sections, exclude_sections=exclude_sections,
        include_html_tags=include_html_tags, livecrawl=livecrawl,
        livecrawl_timeout=livecrawl_timeout,
        output_schema=output_schema, system_prompt=system_prompt,
        additional_queries=additional_queries, user_location=user_location,
        moderation=moderation, compliance=compliance, context=context,
        timeout=timeout,
    )
    lines = _fmt(res.results)
    if res.answer:
        lines += f"\n\n--- synthesized answer ---\n{res.answer}"
    return lines


# ---------------------------------------------------------------------------
# fetch()  ->  page content / highlights / summary / extras
# ---------------------------------------------------------------------------
def fetch(
    urls=None,
    ids=None,
    *,
    mode: str = "text",
    max_characters: int = 1000,
    text_verbosity: Optional[str] = None,
    with_highlights: bool = False,
    highlights_query: Optional[str] = None,
    highlights_max_characters: Optional[int] = None,
    with_summary: bool = False,
    summary_query: Optional[str] = None,
    summary_schema: Optional[dict] = None,
    extras_links: int = 0,
    extras_image_links: int = 0,
    extras_rich_links: int = 0,
    extras_rich_image_links: int = 0,
    extras_code_blocks: int = 0,
    max_age_hours: Optional[int] = None,
    subpages: int = 0,
    subpage_target=None,
    livecrawl: Optional[str] = None,
    livecrawl_timeout: Optional[int] = None,
    include_sections=None, exclude_sections=None, include_html_tags: bool = False,
    compliance: Optional[str] = None,
    include_meta: bool = False,
    timeout: float = 45.0,
) -> Union[List[dict], str, dict]:
    """Fetch the contents of one or more URLs or document IDs.

    ``urls`` may be a URL string or list; ``ids`` are document IDs from a prior
    ``search``. Provide either ``urls`` or ``ids``, never both.

    ``mode`` selects the primary content shape returned:
      * 'text'      -> raw page text (default)
      * 'markdown'  -> best-effort lightweight text (ask for a text render)
      * 'highlights'-> relevant highlighted snippets (auto-enables highlights)
      * 'summary'   -> an LLM summary (auto-enables summary)

    Rich per-result options are available directly: ``with_highlights`` (
    + ``highlights_query`` / ``highlights_max_characters``), ``with_summary``
    (+ ``summary_query`` / ``summary_schema``), ``extras_links`` /
    ``extras_image_links`` / ``extras_code_blocks``, ``max_age_hours`` (0 = fresh, -1 = cache only, 720 =
    up to 30 days old ok), and ``subpages``.

    Returns a list of dicts: each has ``url``, ``id``, ``title``, ``text``,
    ``highlights``, ``summary``, ``extras``, plus ``status`` / ``source`` /
    ``error`` (from the API statuses, e.g. a failed crawl). For backward
    compatibility, a single-URL plain-text fetch returns the old formatted
    string (``--- Title ---\n<body>``) when no rich options are requested.
    """
    if (urls is None) == (ids is None):
        raise ExaError("fetch requires exactly one of `urls` or `ids` (pass one, not both).")
    if urls is not None and isinstance(urls, str):
        urls = [urls]
    if ids is not None and isinstance(ids, str):
        ids = [ids]

    key = _get_api_key()
    body: dict[str, Any] = {}
    if ids is not None:
        body["ids"] = list(ids)
    else:
        body["urls"] = list(urls)

    # mode shortcuts
    if mode == "highlights":
        with_highlights = True
    elif mode == "summary":
        with_summary = True
    elif mode == "markdown":
        text_verbosity = text_verbosity or "full"

    contents = _contents_block(
        with_text=(mode in ("text", "markdown")) or (max_characters and not (with_summary and not with_highlights)),
        max_characters=max_characters, text_verbosity=text_verbosity,
        with_highlights=with_highlights, highlights_query=highlights_query,
        highlights_max=highlights_max_characters,
        with_summary=with_summary, summary_query=summary_query, summary_schema=summary_schema,
        extras_links=extras_links, extras_image_links=extras_image_links,
        extras_rich_links=extras_rich_links, extras_rich_image_links=extras_rich_image_links,
        extras_code_blocks=extras_code_blocks, max_age_hours=max_age_hours,
        subpages=subpages, subpage_target=subpage_target, livecrawl=livecrawl,
        livecrawl_timeout=livecrawl_timeout, include_sections=include_sections,
        exclude_sections=exclude_sections, include_html_tags=include_html_tags,
    )
    # /contents accepts these keys at the top level
    body.update(contents)
    if compliance:
        body["compliance"] = compliance

    data = _request("POST", "/contents", key, json_body=body, timeout=timeout)
    status_list = data.get("statuses", [])
    status_map = {s.get("id"): s for s in status_list}
    results: List[dict] = []
    seen: Dict[str, bool] = {}
    for r in data.get("results", []):
        url = r.get("url")
        uid = r.get("id") or url
        seen[uid] = True
        st = status_map.get(uid) or status_map.get(url)
        results.append({
            "url": url,
            "id": r.get("id"),
            "title": r.get("title"),
            "text": (r.get("text") or "").strip(),
            "highlights": r.get("highlights"),
            "summary": r.get("summary"),
            "extras": r.get("extras"),
            "image": r.get("image"),
            "favicon": r.get("favicon"),
            "status": st.get("status") if st else "success",
            "source": st.get("source") if st else None,
            "error": st.get("error") if st else None,
        })
    # Include per-URL status entries that produced no result (failed crawls),
    # so the caller never silently loses a requested URL.
    for st in status_list:
        sid = st.get("id")
        if sid and not seen.get(sid):
            results.append({
                "url": sid,
                "id": sid,
                "title": None,
                "text": None,
                "highlights": None,
                "summary": None,
                "extras": None,
                "image": None,
                "favicon": None,
                "status": st.get("status"),
                "source": st.get("source"),
                "error": st.get("error"),
            })
    # Request metadata surfaced (v0.11): per-request latency + cost. ``searchTime``
    # is returned by /contents in ms; kept alongside request_id/costDollars.
    meta = {
        "request_id": data.get("requestId"),
        "cost_dollars": data.get("costDollars"),
        "search_time_ms": data.get("searchTime"),
    }
    # Backward-compatible single-URL string form when no rich options requested
    # and the fetch actually returned readable text (so a failed crawl with no
    # content is still surfaced as a structured error instead of an empty page).
    if (len(results) == 1 and mode == "text" and not (with_highlights or with_summary)
            and not extras_links and max_age_hours is None):
        r0 = results[0]
        t = (r0.get("text") or "").strip()
        if t or r0.get("error") is None:
            head = t[: max_characters if max_characters else len(t)]
            rendered = f"--- {r0.get('title') or r0.get('url')} ---\n{head}"
            if include_meta:
                return {"results": results, "rendered": rendered, **meta}
            return rendered
    if include_meta:
        return {"results": results, **meta}
    return results



# ---------------------------------------------------------------------------
# answer()  ->  compact citation-grounded answer (api.exa.ai/answer)
# ---------------------------------------------------------------------------
class Answer:
    """Result of ``answer()``: a synthesized answer + its supporting citations.

    ``answer`` is a plain string unless ``output_schema`` was provided, in
    which case it is a dict matching that schema. ``citations`` is a list of
    source dicts (title / url / published_date / author / image / favicon).
    """

    def __init__(self, raw: dict):
        self.raw = raw
        self.request_id: str = raw.get("requestId") or ""
        self.answer = raw.get("answer")
        self.citations: List[dict] = raw.get("citations", [])
        self.cost_dollars = raw.get("costDollars")

    @property
    def sources(self) -> List[str]:
        """Just the source URLs cited by the answer."""
        return [c.get("url") for c in self.citations if c.get("url")]

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "answer": self.answer,
            "citation_count": len(self.citations),
            "citations": self.citations,
            "sources": self.sources,
            "cost_dollars": self.cost_dollars,
        }

    def to_markdown(self) -> str:
        """Render the citation-grounded answer as Markdown (answer + numbered sources)."""
        parts = []
        parts.append("## Answer")
        parts.append("")
        parts.append(str(self.answer))
        parts.append("")
        parts.append(f"### Sources ({len(self.citations)})")
        parts.append("")
        for idx, c in enumerate(self.citations, 1):
            title = c.get("title") or "(untitled)"
            url = c.get("url") or ""
            if url:
                parts.append(f"{idx}. **{title}** — [{url}]({url})")
            else:
                parts.append(f"{idx}. **{title}**")
        return "\n".join(parts)

    def __repr__(self) -> str:
        n = len(self.citations)
        return f"<Answer n_sources={n} request_id={self.request_id}>"


def answer(
    query: str,
    *,
    output_schema: Optional[dict] = None,
    text: bool = False,
    stream: bool = False,
    citation_format: Optional[dict] = None,
    timeout: float = 90.0,
) -> Answer:
    """Get a concise, citation-grounded answer to a natural-language question.

    Uses the dedicated ``/answer`` endpoint (distinct from a ``search``
    ``output_schema`` synthesis). It reads the query, pulls relevant pages and
    writes a compact answer with explicit source citations. This is the fastest
    human-facing "just tell me" surface — ideal when you want the bottom line
    with sources rather than a ranked result list.

    Args:
        query: The question or instruction (e.g. "What is the latest valuation
            of SpaceX?").
        output_schema: A JSON Schema (root type 'text' or 'object'). When given,
            ``Answer.answer`` is a structured object instead of a string.
        text: Include full page text of each citation source (heavier).
        citation_format: Live-verified ``citationFormat`` passthrough (v0.14).
            A dict of bools selecting which citation fields to request / return,
            e.g. ``{"id": True, "favicon": True, "author": True,
            "publishedDate": True, "image": True, "text": True}``. The API
            populates each requested field when it knows it (favicon / author /
            publishedDate / image are returned conditionally per source); not
            every field is present for every citation. When omitted, the API
            returns its default rich citation shape. This param is forwarded
            verbatim to the live endpoint (field is undocumented but accepted).
        timeout: Request timeout (s). Answers can take notably longer than
            plain searches.

    Returns:
        An ``Answer`` with ``.answer`` (str or dict) and ``.citations`` list.
    """
    key = _get_api_key()
    body: dict[str, Any] = {"query": query, "text": text}
    if output_schema:
        body["outputSchema"] = output_schema
    if citation_format:
        body["citationFormat"] = citation_format
    body["stream"] = bool(stream)
    data = _request("POST", "/answer", key, json_body=body, timeout=timeout)
    return Answer(data)


# ---------------------------------------------------------------------------
# agent()  — full agentic workflow  (api.exa.ai/agent/runs)
# ---------------------------------------------------------------------------
class AgentRun:
    """Result of ``agent()``: an Exa agentic run with text + optional structured
    output, field-level grounding (citations), usage, and cost.

    Mirrors the live ``/agent/runs`` response. ``text`` is the natural-language
    answer; ``structured`` is a dict matching ``outputSchema`` (``None`` if no
    schema was given); ``grounding`` is a list of per-field citation clusters;
    ``cost_dollars`` and ``usage`` track what the run spent on compute/searches.
    """

    def __init__(self, raw: dict):
        self.raw = raw
        self.id: str = raw.get("id") or ""
        self.status: str = raw.get("status") or "queued"          # queued|running|completed|failed|cancelled
        self.stop_reason: Optional[str] = raw.get("stopReason")
        self.created_at = raw.get("createdAt")
        self.completed_at = raw.get("completedAt")
        out = raw.get("output") or {}
        self.text: Optional[str] = out.get("text")
        self.structured = out.get("structured")
        self.grounding: List[dict] = out.get("grounding") or []
        self.usage: dict = raw.get("usage") or {}
        self.cost_dollars: dict = raw.get("costDollars") or {}
        self.request = raw.get("request") or {}

    @property
    def done(self) -> bool:
        """True once the run reached a terminal state (completed/failed/cancelled)."""
        return self.status in ("completed", "failed", "cancelled")

    @property
    def citations(self) -> List[dict]:
        """Unique (url, title, field) pairs across all grounding groups."""
        seen: Dict[str, dict] = {}
        for g in self.grounding:
            for c in g.get("citations", []):
                u = c.get("url")
                if u and u not in seen:
                    seen[u] = {"url": u, "title": c.get("title"), "field": g.get("field")}
        return list(seen.values())

    @property
    def sources(self) -> List[str]:
        return [c["url"] for c in self.citations]

    @property
    def cost(self) -> float:
        return self.cost_dollars.get("total") or 0.0

    @property
    def connects(self) -> Dict[str, Dict[str, Any]]:
        """Per-provider Exa Connect breakdown: tool-call count + USD spend.

        When the run invoked any Exa Connect data providers (``data_sources``,
        e.g. fiber, similarweb, baselayer...), the run reports per-provider tool
        call counts (``usage.dataSources``) and spend (``costDollars.dataSources``).
        Returns a dict ``{"provider": {"calls": int, "cost_usd": float}}``; a
        provider appears only when the agent actually used it (zero-use providers
        are omitted by the API). Empty dict when no Connect provider fired.
        """
        calls = (self.usage or {}).get("dataSources") or {}
        cost = (self.cost_dollars or {}).get("dataSources") or {}
        out: Dict[str, Dict[str, Any]] = {}
        for prov in set(calls) | set(cost):
            out[prov] = {"calls": int(calls.get(prov) or 0),
                         "cost_usd": float(cost.get(prov) or 0.0)}
        return out

    @property
    def connect_providers(self) -> List[str]:
        """Provider names the agent actually used via Exa Connect."""
        return sorted(self.connects)

    @property
    def connect_cost_usd(self) -> float:
        """Total spend across Exa Connect providers, in USD."""
        return sum(v["cost_usd"] for v in self.connects.values())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "text": self.text,
            "structured": self.structured,
            "grounding": self.grounding,
            "citations": self.citations,
            "sources": self.sources,
            "cost_dollars": self.cost_dollars,
            "usage": self.usage,
        }

    def to_markdown(self) -> str:
        """Render the agent run as markdown: status header, grounded answer, sources."""
        parts = []
        parts.append("## Agent Run")
        parts.append("")
        parts.append(f"**Status:** `{self.status}` · **Run id:** `{self.id or '?'}`" +
                     (f" · **Cost:** ${self.cost:.4f}" if self.cost_dollars else ""))
        parts.append("")
        if self.text:
            parts.append(str(self.text))
            parts.append("")
        if self.structured:
            import json as _json
            parts.append("### Structured output")
            parts.append("")
            parts.append("```json")
            parts.append(_json.dumps(self.structured, indent=2))
            parts.append("```")
            parts.append("")
        cites = self.citations
        parts.append(f"### Sources ({len(cites)})")
        parts.append("")
        for idx, c in enumerate(cites, 1):
            title = c.get("title") or "(untitled)"
            url = c.get("url") or ""
            if url:
                parts.append(f"{idx}. **{title}** — [{url}]({url})")
            else:
                parts.append(f"{idx}. **{title}**")
        return "\n".join(parts)

    def __repr__(self) -> str:
        n = len(self.citations)
        return f"<AgentRun id={self.id or '?'} status={self.status} citations={n} cost=${self.cost:.4f}>"


class AgentTrace:
    """Typed step-trace of an Exa agent run (api.exa.ai/agent/runs/{id}/events).

    Parses the event list returned by ``agent_events()`` on an agent run into an
    ordered timeline of lifecycle, tool-call, source, and completion events, plus
    aggregated metrics (duration, per-tool source counts, usage, cost). Lets you
    audit *how* an Exa agent did a task — which tools it called (search,
    finish), how many sources each search retrieved, and when events fired.

    Attributes:
        run_id: The agent run id.
        id:     Alias of ``run_id``.
        steps:  Ordered list of ``{"event", "timestamp", "data"}`` trace entries.
        tools:  Ordered, deduplicated list of tool-call dicts with ``name``,
            ``call_id``, ``status``, ``added_at``/``done_at`` timestamps,
            ``arguments`` (API-redacted), and per-tool ``sources`` URLs.
        sources: dict of ``url -> {"title", "url", "call_id"}`` unique sources.
        usage: usage dict from the completed event (searches / compute).
        cost_dollars: cost dict (total / search / agentCompute / ...).
    """

    def __init__(self, run_id: str, events):
        self.run_id = run_id or ""
        self.id = self.run_id
        self._events = events  # list of {"event","data","createdAt","id"}
        self.steps: List[dict] = []
        self.tools: List[dict] = []
        self.sources: Dict[str, dict] = {}
        self.source_truncated = 0
        self.created_at = None
        self.completed_at = None
        self.status = None
        self.stop_reason = None
        self.output: dict = {}
        self.usage: dict = {}
        self.cost_dollars: dict = {}
        self.duration_seconds: Optional[float] = None
        self._parse()

    def _parse(self):
        tool_by_id: Dict[str, dict] = {}
        for ev in self._events:
            etype = ev.get("event") or ""
            data = ev.get("data") or {}
            ts = ev.get("createdAt") or ""
            self.steps.append({"event": etype, "timestamp": ts, "data": data})

            if etype == "agent_run.created":
                self.created_at = ts
                self.id = data.get("id") or self.id
                self.run_id = data.get("id") or self.run_id
            elif etype == "agent_run.output_item.added":
                item = data.get("item") or {}
                tid = item.get("id")
                t = tool_by_id.setdefault(tid, {
                    "id": tid, "name": item.get("name"),
                    "type": item.get("type") or "function_call",
                    "status": item.get("status"), "call_id": item.get("call_id"),
                    "added_at": ts, "done_at": None,
                    "arguments": item.get("arguments"),
                    "metadata": item.get("metadata") or {},
                    "sources": [],
                })
                self.tools.append(t)
            elif etype == "agent_run.output_item.done":
                item = data.get("item") or {}
                tid = item.get("id")
                if tid in tool_by_id:
                    tool_by_id[tid]["status"] = item.get("status") or "completed"
                    tool_by_id[tid]["done_at"] = ts
                    tool_by_id[tid]["metadata"] = item.get("metadata") or {}
            elif etype == "agent_run.function_call_arguments.done":
                tid = data.get("item_id")
                if tid in tool_by_id:
                    tool_by_id[tid]["arguments"] = data.get("arguments") or tool_by_id[tid].get("arguments")
            elif etype == "agent_run.source.added":
                src = data.get("source") or {}
                url = src.get("url")
                if url:
                    self.sources[url] = {"title": src.get("title"), "url": url,
                                         "call_id": src.get("callId")}
                    for t in tool_by_id.values():
                        if t.get("call_id") == src.get("callId") and url not in t["sources"]:
                            t["sources"].append(url)
            elif etype == "agent_run.source.truncated":
                self.source_truncated += 1
            elif etype == "agent_run.completed":
                self.status = "completed"
                self.completed_at = data.get("completedAt", ts)
                self.stop_reason = data.get("stopReason")
                self.output = data.get("output") or {}
                self.usage = data.get("usage") or {}
                self.cost_dollars = data.get("costDollars") or {}

        # Deduplicate tools (by id) preserving order.
        seen: Set[str] = set()
        unique: List[dict] = []
        for t in self.tools:
            if t["id"] not in seen:
                seen.add(t["id"])
                unique.append(t)
        self.tools = unique

        if self.created_at and self.completed_at:
            try:
                c1 = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
                c2 = datetime.fromisoformat(self.completed_at.replace("Z", "+00:00"))
                self.duration_seconds = (c2 - c1).total_seconds()
            except Exception:
                pass

    @property
    def search_count(self):
        """How many ``search`` tool calls the agent made."""
        return sum(1 for t in self.tools if t.get("name") == "search")

    @property
    def tool_names(self):
        """Ordered tool names, e.g. ['search', 'search', 'finish']."""
        return [t.get("name") for t in self.tools if t.get("name")]

    @property
    def source_count(self):
        return len(self.sources)

    @property
    def citations(self):
        return [{"url": s["url"], "title": s["title"]} for s in self.sources.values()]

    @property
    def cost(self) -> float:
        return self.cost_dollars.get("total") or 0.0

    @property
    def searches(self) -> int:
        return int((self.usage or {}).get("searches") or 0)

    @property
    def agent_compute_units(self) -> float:
        return float((self.usage or {}).get("agentComputeUnits") or 0.0)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "stop_reason": self.stop_reason,
            "usage": self.usage,
            "cost_dollars": self.cost_dollars,
            "source_truncated": self.source_truncated,
            "source_count": self.source_count,
            "search_count": self.search_count,
            "tools": self.tools,
            "tool_names": self.tool_names,
            "sources": list(self.sources.values()),
            "steps": self.steps,
        }

    def to_markdown(self) -> str:
        """Render the trace as a readable markdown audit sheet."""
        p = []
        p.append("## Agent Trace")
        p.append("")
        dur = f" · **Duration:** {self.duration_seconds:.1f}s" if self.duration_seconds is not None else ""
        cost = f" · **Cost:** ${self.cost:.4f}" if self.cost else ""
        p.append(f"**Run:** `{self.run_id or '?'}` · **Status:** `{self.status or '?'}`{dur}{cost}")
        p.append("")
        if self.usage:
            usage_str = ", ".join(f"{k}={v}" for k, v in self.usage.items() if v)
            p.append(f"**Usage:** {usage_str}")
            p.append("")
        if self.tools:
            p.append(f"**Tools called ({len(self.tools)}):**")
            p.append("")
            for t in self.tools:
                p.append(f"- `{t.get('name', '?')}` — {len(t.get('sources', []))} sources"
                         + (f" · status={t.get('status')}" if t.get("status") else ""))
            p.append("")
        if self.sources:
            p.append(f"**Sources ({len(self.sources)}):**")
            p.append("")
            for s in self.sources.values():
                p.append(f"- {s.get('title') or '(untitled)'} — {s.get('url')}")
            p.append("")
        if self.output.get("text"):
            p.append("**Answer:**")
            p.append("")
            p.append(str(self.output["text"]))
            p.append("")
        p.append(f"**Events ({len(self.steps)}):**")
        p.append("")
        for step in self.steps:
            ts = step.get("timestamp", "")
            ev = step.get("event", "")
            d = step.get("data") or {}
            if ev == "agent_run.source.added":
                src = (d.get("source") or {}).get("url", "")
                p.append(f"- `{ts}` `{ev}` {src[:80]}")
            elif ev in ("agent_run.output_item.added", "agent_run.output_item.done"):
                item = d.get("item") or {}
                p.append(f"- `{ts}` `{ev}` {item.get('name','?')} ({item.get('type','')})")
            elif ev == "agent_run.completed":
                p.append(f"- `{ts}` `{ev}` status={d.get('status')}")
            else:
                p.append(f"- `{ts}` `{ev}`")
        return "\n".join(p)

    def __repr__(self):
        dur = f"{self.duration_seconds:.1f}s" if self.duration_seconds is not None else "?"
        return (f"<AgentTrace run={self.run_id[:20] if self.run_id else '?'} "
                f"status={self.status} tools={len(self.tools)} "
                f"sources={self.source_count} duration={dur}>")


def agent_trace(run_id, *, limit=50, cursor=None):
    """Fetch and type the step-trace of an agent run (tool calls, sources, timing).

    Retrieves the agent run's entire event stream via ``agent_events()``
    (auto-paginating ``cursor`` until exhausted), parses it into a typed
    ``AgentTrace`` with an ordered ``steps`` timeline, aggregated ``tools``
    (each tool call with per-tool source URLs and timing), deduplicated
    ``sources``, and lifecycle metadata (status, duration, usage, cost).

    Args:
        run_id: The agent run's id.
        limit:  Initial page size per ``agent_events`` call (default 50).
        cursor: Optional starting cursor (for resuming pagination).

    Returns:
        An ``AgentTrace``.
    """
    parsed: List[dict] = []
    page = agent_events(run_id, limit=limit, cursor=cursor)
    while True:
        data = page.get("data") or []
        for ev in data:
            e = dict(ev)
            d = e.get("data")
            if isinstance(d, str):
                try:
                    e["data"] = json.loads(d)
                except Exception:
                    pass
            parsed.append(e)
        if not page.get("hasMore") or not page.get("nextCursor"):
            break
        page = agent_events(run_id, cursor=page["nextCursor"])
    return AgentTrace(run_id, parsed)


def agent(
    query: str,
    *,
    system_prompt: Optional[str] = None,
    output_schema: Optional[dict] = None,
    effort: str = "auto",
    previous_run_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    data: Optional[List[dict]] = None,
    exclusion: Optional[List[dict]] = None,
    data_sources: Optional[List[str]] = None,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
    raise_on_failed: bool = True,
) -> AgentRun:
    """Run a full Exa **agentic** workflow and return the completed, cited answer.

    This is Exa's most powerful research surface (``/agent/runs``): instead of
    ranking results for you, the agent reasons over the web, pulls evidence, and
    writes a grounded answer — optionally structured to ``output_schema``, with
    per-field citations (``grounding``), usage, and cost. It supports continuing
    a prior run (``previous_run_id``) and can process/avoid given records
    (``data`` / ``exclusion``).

    Args:
        query: Natural-language research question or instruction (e.g. "Find the
            Series A funding rounds announced this week in AI infra").
        system_prompt: Extra guidance (source preference, novelty/duplication
            constraints, tone).
        output_schema: JSON Schema (draft-07/2019-09/2020-12). When given, the
            completed run's ``.structured`` holds validated structured output;
            fields unsupported by evidence may be ``null``.
        effort: Cost/reasoning effort: minimal|low|medium|high|xhigh|auto (default).
        previous_run_id: Completed run ID to continue from (same team).
        metadata: Arbitrary string KV to store with the run (your own tracking).
        data: JSON records for the agent to process/enrich as part of the run.
        exclusion: JSON records/entities the agent should avoid returning.
        timeout: Max total wall-clock seconds to wait for completion (default 120).
        poll_interval: Seconds between status polls (default 2).
        raise_on_failed: If the run ends with status ``failed``, raise an
            ``ExaError`` instead of returning the (empty) run.

    Returns:
        An ``AgentRun`` with ``.text``, ``.structured``, ``.grounding``,
        ``.citations``, ``.cost``, and ``.usage``.

    Raises:
        ExaError: on API errors, on timeout (run still running), or (if
            ``raise_on_failed``) when the run fails.
    """
    import time
    key = _get_api_key()
    body: dict[str, Any] = {"query": query}
    if system_prompt:
        body["systemPrompt"] = system_prompt
    if output_schema:
        body["outputSchema"] = output_schema
    if effort and effort != "auto":
        body["effort"] = effort
    if previous_run_id:
        body["previousRunId"] = previous_run_id
    if metadata:
        body["metadata"] = metadata
    if data_sources:
        if len(data_sources) > 5:
            raise ExaError("agent(data_sources=...) supports at most 5 Exa Connect providers per run.")
        body["dataSources"] = [{"provider": p} for p in data_sources]
    inp: dict[str, Any] = {}
    if data is not None:
        inp["data"] = list(data)
    if exclusion is not None:
        inp["exclusion"] = list(exclusion)
    if inp:
        body["input"] = inp

    created = _request("POST", "/agent/runs", key, json_body=body, timeout=min(timeout, 30))
    run = AgentRun(created)
    if run.done:  # rare: already terminal on create
        return run

    deadline = time.time() + timeout
    run_id = run.id
    def _tick():
        data = _request("GET", f"/agent/runs/{run_id}", key, timeout=min(timeout, 45))
        nxt = AgentRun(data)
        return nxt if nxt.done else None
    done = _poll_until(_tick, deadline=deadline,
                       initial=min(0.25, float(poll_interval or 2.0)),
                       cap=max(0.5, float(poll_interval or 2.0)))
    if done is None:
        raise ExaError(
            f"agent() run {run.id} did not finish within {timeout}s (status={run.status}). "
            "Increase `timeout`, use effort='minimal', or poll '/agent/runs/{id}' to continue."
        )
    run = done

    if raise_on_failed and run.status == "failed":
        raise ExaError(f"Exa agent run {run.id} failed (stop_reason={run.stop_reason}). "
                       f"Try lower effort, a simpler output_schema, or rephrasing the query.")
    return run


def agent_chat(query, *, system_prompt=None, turns=1, output_schema=None,
               schemas=None, effort="auto", timeout=150.0, poll_interval=2.0):
    """Run ``turns`` agentic research turns, chaining each answer into the next.

    This is a first-class multi-turn research loop: each turn is a full
    ``agent()`` run whose completed run id is passed as ``previous_run_id`` so
    the follow-up prompts continue the same thread with full awareness of the
    prior turn's sources, grounding, and conclusion. Use it for progressive
    refinement — e.g. first "survey the options", then "narrow to the top 2
    and compare" — without manually threading run ids.

    Args:
        query: the first-turn question.
        system_prompt: extra guidance applied to every turn.
        turns: number of agentic turns to run (>=1). If >1, turns 2..N ask the
            agent to continue/refine given the prior answer.
        output_schema: optional JSON schema for structured output (every turn
            validates against it).
        schemas: live-verified per-turn output schema override (v0.14): a list
            of JSON schemas, one per turn — turn i validates against ``schemas[i]``
            when given, else falls back to ``output_schema`` (or none). Enables
            a progressive multi-schema pipeline (e.g. turn 1 extracts companies,
            turn 2 enriches each with funding). Length is capped at ``turns``.
        effort: reasoning effort level for each run.
        timeout: max wall-clock seconds per turn.

    Returns:
        ``{"run_id": last_run.id, "text": last_run.text,
           "grounding": last_run.grounding, "citations": last_run.citations,
           "turns": [AgentRun, ...]}``
    """
    turns_list: List[AgentRun] = []
    prev_run_id: Optional[str] = None
    current_query = query
    schemas_in = list(schemas or []) or [None] * max(int(turns), 1)
    for i in range(max(int(turns), 1)):
        turn_schema = schemas_in[i] if i < len(schemas_in) else output_schema
        turn_schema = turn_schema or output_schema
        run = agent(current_query, system_prompt=system_prompt,
                    output_schema=turn_schema, effort=effort,
                    previous_run_id=prev_run_id, timeout=timeout,
                    poll_interval=poll_interval)
        turns_list.append(run)
        if i == 0:
            current_query = ("Building on the previous answer exactly one step "
                             "further: refine the prior conclusion, fill any "
                             "gaps it left, and give the improved final answer.")
        prev_run_id = run.id
    last = turns_list[-1]
    return {
        "run_id": last.id,
        "text": last.text,
        "grounding": last.grounding,
        "citations": last.citations,
        "turns": turns_list,
    }


def agent_structured(query: str, output_schema: dict, **kwargs) -> AgentRun:
    """Thin convenience over ``agent()`` that requires structured output.

    Equivalent to ``agent(query, output_schema=output_schema, ...)`` but asserts
    the run produced structured data so downstream code can use ``run.structured``
    directly.
    """
    run = agent(query, output_schema=output_schema, **kwargs)
    if run.structured is None:
        raise ExaError(f"agent run {run.id} did not produce structured output for the schema.")
    return run


# ---------------------------------------------------------------------------
# find_similar()  — pages similar to a URL / id  (api.exa.ai/findSimilar)
# ---------------------------------------------------------------------------
def find_similar(
    url,
    *,
    num_results: int = 5,
    category: Optional[str] = None,
    include_domains=None,
    exclude_domains=None,
    exclude_source_domain: bool = False,
    start_published_date: Optional[str] = None,
    end_published_date: Optional[str] = None,
    with_text: bool = False,
    max_characters: Optional[int] = None,
    text_verbosity: Optional[str] = None,
    with_highlights: bool = False,
    highlights_query: Optional[str] = None,
    highlights_max_characters: Optional[int] = None,
    with_summary: bool = False,
    summary_query: Optional[str] = None,
    summary_schema: Optional[dict] = None,
    extras_links: int = 0,
    extras_image_links: int = 0,
    extras_rich_links: int = 0,
    extras_rich_image_links: int = 0,
    extras_code_blocks: int = 0,
    max_age_hours: Optional[int] = None,
    subpages: int = 0,
    subpage_target=None,
    include_sections=None, exclude_sections=None, include_html_tags: bool = False,
    livecrawl: Optional[str] = None, livecrawl_timeout: Optional[int] = None,
    timeout: float = 45.0,
) -> SearchResults:
    """Find web pages similar to a given URL (or document id).

    Useful for broadening a single good source into a cluster of related
    coverage — e.g. given one well-written article or a Clojure doc page, pull
    the peer pages / papers covering the same ground.

    Args:
        url: The URL (or document ``id`` from a previous search) to match
            against. Must be a real, crawler-known page.
        num_results: 1..100.
        category: Focus a category (company / publication / news / personal
            site / financial report / people).
        include_domains / exclude_domains: restrict result domains.
        start_published_date / end_published_date: ISO date bounds.
        with_* / extras / max_age_hours: same rich-content options as ``search``.

    Returns:
        A ``SearchResults`` of similar pages.
    """
    cat = _category_canon(category)
    _validate_search_params(num_results, max_characters if (with_text or max_characters) else None,
                            cat, start_published_date, end_published_date, exclude_domains)
    key = _get_api_key()
    body: dict[str, Any] = {"url": url, "numResults": num_results}
    if cat:
        body["category"] = cat
    if include_domains:
        body["includeDomains"] = include_domains
    if exclude_domains:
        body["excludeDomains"] = exclude_domains
    if exclude_source_domain:
        body["excludeSourceDomain"] = True
    if start_published_date:
        body["startPublishedDate"] = start_published_date
    if end_published_date:
        body["endPublishedDate"] = end_published_date
    contents = _contents_block(
        with_text=with_text, max_characters=max_characters,
        text_verbosity=text_verbosity,
        with_highlights=with_highlights, highlights_query=highlights_query,
        highlights_max=highlights_max_characters,
        with_summary=with_summary, summary_query=summary_query,
        summary_schema=summary_schema,
        extras_links=extras_links, extras_image_links=extras_image_links,
        extras_rich_links=extras_rich_links, extras_rich_image_links=extras_rich_image_links,
        extras_code_blocks=extras_code_blocks,
        max_age_hours=max_age_hours, subpages=subpages, subpage_target=subpage_target,
        include_sections=include_sections, exclude_sections=exclude_sections,
        include_html_tags=include_html_tags, livecrawl=livecrawl,
        livecrawl_timeout=livecrawl_timeout,
    )
    if contents:
        body["contents"] = contents
    data = _request("POST", "/findSimilar", key, json_body=body, timeout=timeout)
    results = [Result(r, i + 1) for i, r in enumerate(data.get("results", []))]
    return SearchResults(query=f"similar-to:{url}", results=results,
                         request_id=data.get("requestId"), cost_dollars=data.get("costDollars"),
                         search_time_ms=data.get("searchTime"), raw=data)


def similar_to(url, **kwargs) -> SearchResults:
    """Find web pages similar to a given URL (alias :func:`find_similar`).

    Provided as a friendlier-style name for the "more like this" workflow:
    given one good source page, pull a cluster of peer pages / papers
    covering the same ground. Accepts every keyword argument that
    :func:`find_similar` accepts (num_results, category, domain filters,
    rich-content options, etc.).

    Examples:
        peers = exa.similar_to("https://clojure.org/reference/transducers",
                               num_results=8, with_highlights=True)
    """
    return find_similar(url, **kwargs)


# ---------------------------------------------------------------------------
# stream_answer() / stream_search() — OpenAI-style SSE streaming
# ---------------------------------------------------------------------------
def stream_answer(query, *, output_schema=None, text=False,
                  citation_format=None, timeout=90.0, collect_meta=True):
    """SSE-stream a citation-grounded answer incrementally (yields text deltas).

    Posts to ``/answer`` with ``stream=true`` and yields each incremental content
    delta (assistant role) as it arrives, then — when ``collect_meta=True``
    (default, v0.8) — one trailing ``dict`` capturing the citations and cost that
    the API emits at the end of the stream (verified live), which the plain
    streaming path used to discard:
        {"text": "...", "citations": [...], "cost_dollars": {...}, "request_id": "..."}
    ``collect_meta=False`` preserves the plain old behaviour (only text strings
    are yielded, so ``"".join(parts)`` gives the full answer). For the final
    structured citation objects directly, call ``answer()`` (non-stream).

    Args:
        citation_format: live-verified ``citationFormat`` passthrough (v0.14)
            — a dict of citation fields to request from the API (``id``, ``url``,
            ``title``, ``image``, ``favicon``, ``author``, ``publishedDate``,
            ``text``). Forwarded verbatim to ``/answer``; omitted by default.

    Yields:
        Strings of incremental answer content, then (if ``collect_meta``) one
        final dict with ``text`` / ``citations`` / ``cost_dollars`` /
        ``request_id``. ``citations`` is the list of ``{title, url, image,
        favicon, id}`` sources (empty if the API omitted them).
    """
    key = _get_api_key()
    body = {"query": query, "text": text, "stream": True}
    if output_schema:
        body["outputSchema"] = output_schema
    if citation_format:
        body["citationFormat"] = citation_format
    parts: List[str] = []
    citations_out: List[dict] = []
    cost_dollars: Optional[float] = None
    request_id_meta = ""
    with httpx.stream("POST", f"{DEFAULT_API_URL}/answer", json=body,
                      headers={"x-api-key": key}, timeout=timeout) as resp:
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:500]
            raise ExaError(f"stream_answer: {resp.status_code} {detail}")
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if not request_id_meta and obj.get("requestId"):
                request_id_meta = obj["requestId"]
            if "citations" in obj:
                citations_out = obj.get("citations") or []
                continue
            if "costDollars" in obj:
                cost_dollars = (obj.get("costDollars") or {}).get("total")
                continue
            for c in (obj.get("choices") or []):
                delta = c.get("delta") or {}
                content = delta.get("content")
                if content:
                    parts.append(content)
                    yield content
    if collect_meta:
        yield {
            "text": "".join(parts),
            "citations": citations_out,
            "cost_dollars": cost_dollars,
            "request_id": request_id_meta,
        }


def stream_answer_source(query, *, output_schema=None, citation_format=None,
                         timeout=90.0):
    """SSE-stream an answer as a rich, typed event stream (per-chunk metering).

    Unlike ``stream_answer`` (which yields raw text strings then one trailing
    dict), this variant yields **typed event dicts** with a ``"kind"`` key so
    the caller can observe each SSE frame's role and per-chunk token/char
    metering as it streams:

      * ``{"kind": "delta", "text", "chunk_index", "chars", "cumulative_chars",
         "tokens_estimate"}``  — an incremental text chunk (content delta).
      * ``{"kind": "citations", "citations": [...], "source_count"}`` — the
        source-citation payload the API emits at the end of the stream.
      * ``{"kind": "cost", "cost_dollars"}`` — the trailing cost estimate.
      * ``{"kind": "done", "text", "citations", "cost_dollars", "request_id",
         "total_chars", "total_tokens"}`` — the final aggregate summary.

    ``tokens_estimate`` is a client-side heuristic (``chars // 4``) — it is not
    an official API token count. ``citation_format`` is forwarded verbatim to
    the API (see ``stream_answer``). ``output_schema`` passes ``outputSchema``.

    Example:
        events = list(exa.stream_answer_source("What is Reitit?"))
        delta_events = [e for e in events if e["kind"] == "delta"]  # per-chunk
        final = events[-1]  # {"kind": "done", ...} aggregate
    """
    key = _get_api_key()
    body: dict[str, Any] = {"query": query, "text": False, "stream": True}
    if output_schema:
        body["outputSchema"] = output_schema
    if citation_format:
        body["citationFormat"] = citation_format

    text_parts: List[str] = []
    chunk_index = 0
    cumulative_chars = 0
    request_id = ""
    cost_dollars = None
    citations: List[dict] = []

    def _handle_frame(obj):
        nonlocal request_id, cost_dollars, citations, chunk_index, cumulative_chars, text_parts
        if not request_id and obj.get("requestId"):
            request_id = obj["requestId"]
        if "citations" in obj:
            citations = obj.get("citations") or []
            yield {"kind": "citations", "citations": citations, "source_count": len(citations)}
            return
        if "costDollars" in obj:
            cost_dollars = (obj.get("costDollars") or {}).get("total")
            yield {"kind": "cost", "cost_dollars": cost_dollars}
            return
        for c in (obj.get("choices") or []):
            delta = c.get("delta") or {}
            content = delta.get("content")
            if content:
                chunk_index += 1
                cumulative_chars += len(content)
                text_parts.append(content)
                yield {"kind": "delta", "text": content,
                       "chunk_index": chunk_index,
                       "chars": len(content),
                       "cumulative_chars": cumulative_chars,
                       "tokens_estimate": max(1, cumulative_chars // 4)}

    with httpx.stream("POST", f"{DEFAULT_API_URL}/answer", json=body,
                      headers={"x-api-key": key}, timeout=timeout) as resp:
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:500]
            raise ExaError(f"stream_answer_source: {resp.status_code} {detail}")

        frame_buffer: List[str] = []
        for line in resp.iter_lines():
            if not line:
                # Blank line → flush the accumulated frame.
                if frame_buffer:
                    payload = "\n".join(frame_buffer).strip()
                    frame_buffer = []
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                        for ev in _handle_frame(obj):
                            yield ev
                    except Exception:
                        continue
            elif line.startswith("data:"):
                frame_buffer.append(line[len("data:"):].strip())
            # ignore other SSE fields (event:, id:, etc.)

        # Flush any trailing frame that wasn't blank-terminated before EOF.
        if frame_buffer:
            payload = "\n".join(frame_buffer).strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    for ev in _handle_frame(obj):
                        yield ev
                except Exception:
                    pass

    yield {
        "kind": "done",
        "text": "".join(text_parts),
        "citations": citations,
        "cost_dollars": cost_dollars,
        "request_id": request_id,
        "total_chars": cumulative_chars,
        "total_tokens": max(1, cumulative_chars // 4),
    }


def stream_search(query, *, num_results=5, mode="auto", search_type=None,
                  category=None, output_schema=None, system_prompt=None,
                  timeout=60.0, **kwargs):
    """SSE-stream a synthesized search answer, delivering it event-by-event.

    Search streaming (``/search`` with ``stream=true``, so ``output_schema`` is
    required) streams the retrieved ``results``, the synthesized structured
    ``output``, and — verified live — a per-field ``grounding`` event carrying
    citation clusters with confidence. The ``text-delta`` events also stream the
    synthesized content incrementally (and may carry ``citations``). Each
    incremental content delta is yielded as it arrives; the final yield is a
    complete result dict (see below).

    Yields:
        Incremental content strings from ``text-delta`` events, followed by one
        final ``dict``:
            {"results": [...], "output": {...}, "grounding": [...],
             "citations": [...], "text": "...", "request_id": "..."}
        ``output`` is the structured synthesized answer, ``grounding`` the
        field-level citation clusters (may be ``None`` if the API omitted them),
        ``citations`` the URL-deduplicated source list, and ``text`` the joined
        streamed content. Use this when you want the results + schema-synthesis
        AND their explicit citations in one pass.
    """
    if not output_schema:
        raise ExaError("stream_search requires output_schema (search SSE is the synthesis path).")
    key = _get_api_key()
    body = {"query": query, "numResults": num_results, "stream": True}
    # reuse the same body-layout helper as search() for the non-stream fields
    resolved_category, resolved_search_type = _mode_args(mode, category, search_type)
    cat = _category_canon(resolved_category)
    body["type"] = resolved_search_type
    if cat:
        body["category"] = cat
    if system_prompt:
        body["systemPrompt"] = system_prompt
    body["outputSchema"] = output_schema
    # whitelist of extra kwargs the search() body accepts (drop extras like timeout)
    for extra in ("include_domains", "exclude_domains", "start_published_date",
                   "end_published_date", "use_autoprompt", "user_location"):
        if kwargs.get(extra) is not None:
            key_camel = {"include_domains": "includeDomains", "exclude_domains": "excludeDomains",
                          "start_published_date": "startPublishedDate", "end_published_date": "endPublishedDate",
                          "use_autoprompt": "autoprompt", "user_location": "userLocation"}[extra]
            body[key_camel] = kwargs[extra]
    results_out = None
    output_out = None
    grounding_out = None      # per-field citation delta (type "grounding" event)
    delta_text: List[str] = []  # streamed content from text-delta events
    delta_citations: List[dict] = []  # citations riding along text-delta events
    request_id_meta = ""
    search_time_meta = None   # v0.11: searchTime carried by stream events (ms) when present
    with httpx.stream("POST", f"{DEFAULT_API_URL}/search", json=body,
                      headers={"x-api-key": key}, timeout=timeout) as resp:
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:500]
            raise ExaError(f"stream_search: {resp.status_code} {detail}")
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if not request_id_meta:
                request_id_meta = obj.get("requestId") or ""
            if search_time_meta is None and obj.get("searchTime") is not None:
                search_time_meta = obj.get("searchTime")
            etype = obj.get("type")
            if etype == "text-delta":
                for c in (obj.get("choices") or []):
                    delta = c.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        delta_text.append(content)
                        yield content
                    for d in (delta.get("citations") or []):
                        delta_citations.append(d)
            elif etype == "grounding":
                grounding_out = obj.get("grounding")
                for c in (obj.get("citations") or []):
                    delta_citations.append(c)
            elif etype == "results":
                results_out = obj.get("results")
            elif etype == "done":
                output_out = (obj.get("output") or {}).get("content")
    # Deduplicate citations by URL/id while preserving order.
    seen_urls: Set[str] = set()
    unique_cites = []
    for c in delta_citations:
        u = c.get("url") or c.get("id")
        if u and u not in seen_urls:
            seen_urls.add(u)
            unique_cites.append(c)
    yield {
        "results": results_out,
        "output": output_out,
        "grounding": grounding_out,
        "citations": unique_cites,
        "text": "".join(delta_text),
        "request_id": request_id_meta,
        "search_time_ms": search_time_meta,
    }




# ---------------------------------------------------------------------------
# Monitors — scheduled recurring change-detection searches  (api.exa.ai/monitors)
# ---------------------------------------------------------------------------
def monitor_create(query, *, name=None, period="24h", num_results=5, output_schema=None,
                   metadata=None, webhook_url=None, events=None, filter_empty_results=False,
                   with_text=False, max_characters=None, text_verbosity=None,
                   with_highlights=False, highlights_query=None,
                   highlights_max_characters=None,
                   with_summary=False, summary_query=None, summary_schema=None,
                   extras_links=0, extras_image_links=0, extras_rich_links=0,
                   extras_rich_image_links=0, extras_code_blocks=0,
                   max_age_hours=None, subpages=0, subpage_target=None,
                   livecrawl=None, livecrawl_timeout=None,
                   include_sections=None, exclude_sections=None, include_html_tags=False,
                   timeout=45.0):
    """Create a recurring monitor that periodically re-runs a search and reports
    what's new / changed on its watched pages.

    The monitor executes its ``search`` on the given ``period`` (>= 1h, e.g.
    ``"1h"``/``"6h"``/``"1d"``/``"7d"``). Each run emits change-detection output;
    results are delivered to ``webhook_url`` (HTTPS, optional but the OpenAPI
    layer treats webhooks as the delivery channel).

    Args:
        query: The search query the monitor repeatedly runs.
        period: Run cadence, single-unit duration ("1h","6h","1d","7d").
        name: Optional display name.
        num_results: result count per run (default API 10).
        metadata: dict of string k/v echoed back on deliveries (routing).
        webhook_url: HTTPS endpoint that receives monitor events.
        events: subset of monitor events to subscribe (default all).

    Returns:
        The created monitor dict (``id``, ``status``, ``trigger``, ``webhookSecret``).
    """
    import time
    key = _get_api_key()
    search_payload: dict[str, Any] = {"query": query}
    if num_results:
        search_payload["numResults"] = num_results
    if with_text or with_highlights or with_summary:
        search_payload["contents"] = _contents_block(
            with_text=with_text, max_characters=max_characters,
            text_verbosity=text_verbosity,
            with_highlights=with_highlights, highlights_query=highlights_query,
            highlights_max=highlights_max_characters,
            with_summary=with_summary, summary_query=summary_query,
            summary_schema=summary_schema,
            extras_links=extras_links, extras_image_links=extras_image_links,
            extras_rich_links=extras_rich_links, extras_rich_image_links=extras_rich_image_links,
            extras_code_blocks=extras_code_blocks,
            max_age_hours=max_age_hours,
            subpages=subpages, subpage_target=subpage_target,
            livecrawl=livecrawl, livecrawl_timeout=livecrawl_timeout,
            include_sections=include_sections, exclude_sections=exclude_sections,
            include_html_tags=include_html_tags,
        )
    if filter_empty_results:
        search_payload["filterEmptyResults"] = True
    if not webhook_url:
        raise ExaError(
            "monitor_create requires `webhook_url`: the API delivers each run's "
            "change-detection output to an HTTPS webhook you control. Pass e.g. "
            "webhook_url='https://your-endpoint/x'."
        )
    body: dict[str, Any] = {
        "search": search_payload,
        "trigger": {"type": "interval", "period": period},
        "webhook": {"url": webhook_url},
    }
    if name:
        body["name"] = name
    if metadata:
        body["metadata"] = metadata
    if events:
        body["webhook"]["events"] = events
    if output_schema:
        body["outputSchema"] = output_schema
    return _request("POST", "/monitors", key, json_body=body, timeout=30)


def monitor_list(*, status=None, name=None, metadata=None, limit=50, cursor=None):
    """List monitors; filter by status/name/metadata. Returns the raw list payload."""
    key = _get_api_key()
    params: dict[str, Any] = {"limit": limit}
    if status: params["status"] = status
    if name:   params["name"] = name
    if metadata: params.update({f"metadata[{k}]": v for k, v in metadata.items()})
    if cursor: params["cursor"] = cursor
    return _request("GET", "/monitors", key, params=params)


def monitor_get(monitor_id):
    """Fetch a single monitor by id."""
    return _request("GET", f"/monitors/{monitor_id}", _get_api_key())


def monitor_update(monitor_id, **fields):
    """Patch a monitor (search / trigger / webhook / metadata / status)."""
    return _request("PATCH", f"/monitors/{monitor_id}", _get_api_key(), json_body=fields)


def monitor_delete(monitor_id):
    """Delete a monitor permanently."""
    return _request("DELETE", f"/monitors/{monitor_id}", _get_api_key())


def monitor_trigger(monitor_id, timeout=45.0):
    """Trigger an immediate run regardless of schedule (active or paused)."""
    return _request("POST", f"/monitors/{monitor_id}/trigger", _get_api_key(), timeout=timeout)


def monitor_runs(monitor_id, *, limit=50, cursor=None):
    """List runs of a monitor (reverse chronological)."""
    params = {"limit": limit}
    if cursor: params["cursor"] = cursor
    return _request("GET", f"/monitors/{monitor_id}/runs", _get_api_key(), params=params)


def monitor_run_get(monitor_id, run_id):
    """Fetch a single monitor run, incl. its completed ``output``."""
    return _request("GET", f"/monitors/{monitor_id}/runs/{run_id}", _get_api_key())


def monitor_wait(monitor_id, run_id=None, *, timeout=300, poll_interval=3.0):
    """Poll a monitor run until it reaches a terminal state; return the run dict."""
    deadline = time.time() + timeout
    def _tick():
        run = monitor_run_get(monitor_id, run_id) if run_id else _current_monitor_run(monitor_id)
        if run.get("status") in ("completed", "failed", "cancelled"):
            return run
        return None
    done = _poll_until(_tick, deadline=deadline,
                       initial=min(0.25, float(poll_interval or 3.0)),
                       cap=max(0.5, float(poll_interval or 3.0)))
    if done is None:
        raise ExaError(f"monitor run {run_id} did not finish within {timeout}s.")
    return done


def _current_monitor_run(monitor_id):
    runs = monitor_runs(monitor_id, limit=1).get("data", [])
    return runs[0] if runs else {}


def monitor_batch(action, *, name=None, status=None, metadata=None, dry_run=True, limit=50):
    """Bulk action (delete/pause/unpause) on monitors matching a filter.

    Args:
        action: "delete" | "pause" | "unpause".
        name: substring filter; status: "active"|"paused"|"disabled";
        metadata: dict of exact-match string KV filters.
        dry_run: if True (default) returns which monitors would be affected w/o acting.
        limit: max monitors to process in one request (<=500).
    """
    if action not in ("delete", "pause", "unpause"):
        raise ExaError(f"monitor_batch action must be delete|pause|unpause, got '{action}'")
    filt: dict[str, Any] = {}
    if name: filt["name"] = name
    if status: filt["status"] = status
    if metadata: filt["metadata"] = metadata
    if not filt:
        raise ExaError("monitor_batch requires at least one filter field to prevent accidental bulk ops.")
    return _request("POST", "/monitors/batch", _get_api_key(),
                    json_body={"action": action, "filter": filt, "dry_run": dry_run, "limit": limit})


# ---------------------------------------------------------------------------
# Web-set monitors  (api.exa.ai/v0/monitors)  -- keep websets fresh on cron
# ---------------------------------------------------------------------------
# Distinct from the /monitors *search* monitors above: these attach to a
# webset and run scheduled search/refresh operations to keep it current.
_CADENCE = "cadence"
_BEHAVIOR = "behavior"


def monitor_check(monitor_id, *, trigger_if_empty: bool = True, timeout: float = 45.0):
    """Check a search monitor's latest run output; trigger a new run if needed.

    The search-monitor API delivers runs to a webhook and stores completed runs
    under ``/monitors/{id}/runs``. This convenience wraps the common pattern:
    * if the monitor has completed runs, return the latest one's output
      (including synthesized ``content``, ``grounding``, and ``results``).
    * if the monitor has no completed run yet and ``trigger_if_empty`` is True,
      trigger a manual run on the monitor.

    Args:
        monitor_id: The search monitor's id.
        trigger_if_empty: If True (default) and no completed run exists,
            call ``monitor_trigger`` on the monitor.
        timeout: HTTP timeout for the poll/GET.

    Returns:
        A dict with ``monitor_id``, ``latest_run``, ``run_output``, ``has_run``.
        If no run exists and ``auto_if_empty=True``, ``run_output`` is ``None``
        and ``triggered`` is True.
    """
    # List runs and get the most recent one
    runs = monitor_runs(monitor_id, limit=5)
    runs_data = runs if isinstance(runs, list) else runs.get("data", [])
    if runs_data:
        latest_run = runs_data[0]
        if latest_run and latest_run.get("output"):
            return {
                "monitor_id": monitor_id,
                "has_run": True,
                "run_id": latest_run.get("id"),
                "run_output": latest_run.get("output"),
                "triggered": False,
            }

    triggered = False
    if trigger_if_empty:
        trigger_result = monitor_trigger(monitor_id, timeout=timeout)
        triggered = True
    return {
        "monitor_id": monitor_id,
        "has_run": False,
        "run_id": None,
        "run_output": None,
        "triggered": triggered,
    }
def wmonitor_create(webset_id, *, cron, timezone="Etc/UTC", count=10,
                    query=None, criteria=None, entity=None, behavior="append",
                    metadata=None, timeout=45.0):
    """Create a **web-set monitor** that keeps a webset fresh on a cron schedule.

    Unlike the search-change ``monitor_*`` API (api.exa.ai/monitors), this
    attaches to a *webset* (api.exa.ai/v0/websets) and runs periodic
    search/refresh operations to add new matching items. The schedule is a
    cron expression (5 fields) plus a timezone; at most once per day.

    Args:
        webset_id: id or externalId of the webset to watch.
        cron: Unix cron expression, 5 fields, at most once/day (e.g. "0 9 * * 1").
        timezone: IANA timezone, default Etc/UTC.
        count: max results to discover per run (required by the API).
        query: natural-language search; defaults to webset's latest search query.
        criteria: optional list of {"description": "..."} to refine matching.
        entity: optional {"type": "company"|"person"} override.
        behavior: "append" (default) or "override" — how new items merge.
        metadata: optional string KV for your own tracking.

    Returns:
        The created monitor dict (``id``, ``status``, ``cadence``, ``behavior``...).
    """
    body: dict[str, Any] = {"websetId": webset_id}
    body["cadence"] = {"cron": cron, "timezone": timezone}
    bhv_cfg: dict[str, Any] = {"count": count, "behavior": behavior}
    if query:    bhv_cfg["query"] = query
    if criteria: bhv_cfg["criteria"] = criteria
    if entity:   bhv_cfg["entity"] = entity
    body["behavior"] = {"type": "search", "config": bhv_cfg}
    if metadata: body["metadata"] = metadata
    return _request("POST", "/v0/monitors", _get_api_key(), base=WEBSETS_API_URL,
                    json_body=body, timeout=min(timeout, 30))


def wmonitor_list(*, webset_id=None, limit=25, cursor=None):
    """List webset monitors. Optionally narrow by ``webset_id``.

    Args:
        webset_id: only return monitors attached to this webset.
        limit: 1..200 (default 25).
        cursor: pagination cursor from a previous response.
    """
    params: dict[str, Any] = {"limit": limit}
    if webset_id: params["websetId"] = webset_id
    if cursor: params["cursor"] = cursor
    return _request("GET", "/v0/monitors", _get_api_key(), base=WEBSETS_API_URL, params=params)


def wmonitor_get(monitor_id):
    """Fetch one webset monitor by id."""
    return _request("GET", f"/v0/monitors/{monitor_id}", _get_api_key(),
                    base=WEBSETS_API_URL)


def wmonitor_update(monitor_id, *, status=None, cadence=None, behavior=None,
                    metadata=None):
    """Update a webset monitor — enable/disable, change cron, or reconfigure.

    Args:
        monitor_id: id of the monitor.
        status: "enabled" | "disabled".
        cadence: dict e.g. {"cron": "0 9 * * 1", "timezone": "America/New_York"}.
        behavior: dict e.g. {"type":"search", "config": {...}} (see wmonitor_create).
        metadata: replace metadata dict.

    Returns the updated monitor.
    """
    body: dict[str, Any] = {}
    if status:   body["status"] = status
    if cadence:  body["cadence"] = cadence
    if behavior: body["behavior"] = behavior
    if metadata is not None: body["metadata"] = metadata
    return _request("PATCH", f"/v0/monitors/{monitor_id}", _get_api_key(),
                    base=WEBSETS_API_URL, json_body=body)


def wmonitor_delete(monitor_id):
    """Delete (permanently remove) a webset monitor."""
    return _request("DELETE", f"/v0/monitors/{monitor_id}", _get_api_key(),
                    base=WEBSETS_API_URL)


def wmonitor_runs(monitor_id, *, limit=25, cursor=None):
    """List the historical runs of a webset monitor."""
    params: dict[str, Any] = {"limit": limit}
    if cursor: params["cursor"] = cursor
    return _request("GET", f"/v0/monitors/{monitor_id}/runs", _get_api_key(),
                    base=WEBSETS_API_URL, params=params)


def wmonitor_run_get(monitor_id, run_id):
    """Get a single webset-monitor run's details (status, output, stats)."""
    return _request("GET", f"/v0/monitors/{monitor_id}/runs/{run_id}", _get_api_key(),
                    base=WEBSETS_API_URL)


# ---------------------------------------------------------------------------
# Agent-run extra operations  (api.exa.ai/agent/runs)
# ---------------------------------------------------------------------------
def agent_list(*, status=None, limit=50, cursor=None):
    """List prior agent runs (reverse chronological) with status/usage/cost.

    Args:
        status: filter by run status (queued|running|completed|failed|cancelled).
        limit: 1..N (default 50).
        cursor: pagination cursor from a previous response's ``nextCursor``
            (the response also carries ``hasMore``/``nextCursor`` for paging).

    Returns:
        The raw list payload: ``{"object", "data": [...], "hasMore", "nextCursor"}``.
    """
    params: dict[str, Any] = {"limit": limit}
    if status: params["status"] = status
    if cursor: params["cursor"] = cursor
    return _request("GET", "/agent/runs", _get_api_key(), params=params)


def agent_get(run_id):
    """Fetch a single agent run snapshot."""
    return _request("GET", f"/agent/runs/{run_id}", _get_api_key())


def agent_delete(run_id):
    """Delete a completed agent run (irreversible)."""
    return _request("DELETE", f"/agent/runs/{run_id}", _get_api_key())


def agent_cancel(run_id):
    """Request cancellation of a running agent run."""
    return _request("POST", f"/agent/runs/{run_id}/cancel", _get_api_key())


def agent_events(run_id, *, limit=None, cursor=None):
    """Fetch the ordered event list of an agent run, with pagination support.

    Args:
        run_id: The agent run's id.
        limit: Max events per page (defaults to the API's default if omitted).
        cursor: Pagination cursor from a previous response's ``nextCursor``.

    Returns:
        The raw list payload: ``{"object", "data": [...], "hasMore", "nextCursor"}``.
    """
    params: dict[str, Any] = {}
    if limit is not None: params["limit"] = limit
    if cursor: params["cursor"] = cursor
    return _request("GET", f"/agent/runs/{run_id}/events", _get_api_key(), params=params)




# ---------------------------------------------------------------------------
# WebSets — bulk web-scale entity discovery  (https://api.exa.ai/websets)
#
# The WebSets product finds STRUCTURED records (people / companies / articles /
# research papers / custom entities) that match a natural-language query at web
# scale, rather than returning ranked result pages like /search. It backs
# Exa's lead-generation / enrichment workflows: create a webset -> it runs one
# or more searches that discover matching entities with rich profiles (name,
# company, location, work history / employees, industry, headcount, ...) ->
# then enrich each entity with additional fields you ask for.
#
# All WebSets endpoints live under the separate base ``WEBSETS_API_URL``.
# ---------------------------------------------------------------------------
def team_info():
    """Return the authenticated team + its concurrency / limits (maxConcurrent)."""
    return _request("GET", "/v0/teams/me", _get_api_key(), base=WEBSETS_API_URL)


# --- Websets lifecycle -------------------------------------------------------
def webset_preview(query, *, search_items=False, entity=None, count=10,
                     timeout=90.0):
    """Preview how a natural-language query will be decomposed without creating
    a webset. Returns the detected ``entity`` + ``criteria`` and (optionally) a
    preview list of matching items.

    Args:
        query: The natural-language search (e.g. "US marketing agencies that
            focus on consumer products").
        search_items: if True, also returns a preview list of matching items.
        entity: optional ``{"type": "company"|"person"|"article"|...}`` hint to
            guide decomposition (auto-detected when omitted).
        count: when ``search_items=True``, max preview items to return (1..10).

    Returns:
        dict with ``search`` (entity + criteria), ``enrichments``, and (when
        ``search_items=True``) ``items`` — a preview of matching records.
    """
    body: dict[str, Any] = {"search": {"query": query}}
    if entity:   body["search"]["entity"] = entity
    if count != 10: body["search"]["count"] = count
    params = {"search": "true" if search_items else "false"}
    return _request("POST", "/v0/websets/preview", _get_api_key(),
                    base=WEBSETS_API_URL, json_body=body, params=params, timeout=timeout)


def webset_create(query, *, count=10, title=None, external_id=None, entity=None,
                  criteria=None, exclude=None, scope=None, recall=False,
                  max_people_per_company=None, metadata=None, require_criteria=True,
                  enrichments=None, imports=None, timeout=90.0):
    """Create a Webset and begin discovering matching entities at web scale.

    The query is decomposed into an entity type + criteria (you can override the
    auto-detected entity/criteria, or set ``require_criteria=False`` to create
    with an empty criteria list). Results accumulate asynchronously; poll with
    ``webset_get(id)`` / ``webset_items(id)``.

    Args:
        query: Natural-language search query describing what you are looking for.
        count: number of items the webset attempts to find (>=1).
        entity: override detected entity, e.g. {"type": "company"} / {"type": "person"}.
        criteria: override generated criteria, list of {"description": "..."} (<=5).
        exclude: list of {"source": "webset"|"import", "id": ...} to omit from all ops.
        scope: restrict search to existing imports (list of {"source":"import","id":...}).
        recall: request an estimate of total reachable results (in ``search.recall``).
        max_people_per_company: soft cap on people from the same employer.
        metadata: dict of string KV for your own tracking.
        external_id: your own identifier for this webset.
        enrichments: list of {"description": "..."} enrichments to run on found items.
        imports: list of {"source": "webset"|"import", "id": ..., "evaluate": bool} to
            seed this webset from existing collections.

    Returns:
        The created webset object (``id``, ``status``, ``searches``, ``dashboardUrl``).
    """
    search_payload: dict[str, Any] = {"query": query, "count": count}
    if entity:             search_payload["entity"] = entity
    if criteria:           search_payload["criteria"] = criteria
    if exclude:            search_payload["exclude"] = exclude
    if scope:              search_payload["scope"] = scope
    if recall:             search_payload["recall"] = True
    if max_people_per_company is not None: search_payload["maxPeoplePerCompany"] = max_people_per_company
    if not require_criteria:
        search_payload["behavior"] = "override"
    body: dict[str, Any] = {"search": search_payload}
    if title:       body["title"] = title
    if external_id: body["externalId"] = external_id
    if metadata:    body["metadata"] = metadata
    if enrichments: body["enrichments"] = enrichments
    if imports:     body["import"] = imports
    return _request("POST", "/v0/websets", _get_api_key(), base=WEBSETS_API_URL,
                    json_body=body, timeout=timeout)


def webset_list(*, limit=50, cursor=None, search=None):
    """List all websets for the authenticated team.

    Args:
        limit: 1..100 (default 50).
        cursor: pagination cursor from a previous response.
        search: substring filter by webset id, external ID, or title (2..50 chars).
    """
    params: dict[str, Any] = {"limit": limit}
    if cursor: params["cursor"] = cursor
    if search: params["search"] = search
    return _request("GET", "/v0/websets", _get_api_key(), base=WEBSETS_API_URL, params=params)


def webset_get(webset_id, *, include_items=False):
    """Fetch a single webset (incl. searches, imports, enrichments) by id.

    ``include_items=True`` asks the API to embed the current items list in the
    response using the native ``expand=items`` query parameter — a single
    round-trip, no extra ``webset_items()`` call needed. (Verified live 2026-08.)

    Returns:
        Webset dict. When ``include_items`` is set, the object includes
        ``"items"`` — the current person/company/... records found so far.
    """
    params: dict[str, Any] = {}
    if include_items:
        params["expand"] = ["items"]
    return _request("GET", f"/v0/websets/{webset_id}", _get_api_key(),
                    base=WEBSETS_API_URL, params=params)


def webset_delete(webset_id):
    """Delete a webset and everything under it (irreversible)."""
    return _request("DELETE", f"/v0/websets/{webset_id}", _get_api_key(), base=WEBSETS_API_URL)


def webset_cancel(webset_id):
    """Cancel a running webset search (idempotent)."""
    return _request("POST", f"/v0/websets/{webset_id}/cancel", _get_api_key(), base=WEBSETS_API_URL)


def webset_update(webset_id, *, title=None, metadata=None):
    """Update a webset's title / metadata."""
    body: dict[str, Any] = {}
    if title is not None: body["title"] = title
    if metadata is not None: body["metadata"] = metadata
    return _request("POST", f"/v0/websets/{webset_id}", _get_api_key(), base=WEBSETS_API_URL, json_body=body)


# --- Webset items -----------------------------------------------------------
def webset_items(webset_id, *, limit=100, cursor=None, source_id=None,
                   satisfied=None):
    """Retrieve the structured items (person/company/... records) found so far.

    Each item carries an ``evaluations`` array (one entry per search criterion)
    with ``criterion`` / ``reasoning`` / ``satisfied`` (``yes``|``no``|``unclear``)
    / ``references`` — the WebSets criterion-evaluation surface. Pass
    ``satisfied="yes"`` (or ``"no"`` / ``"unclear"``) to keep only items whose
    evaluations match that mark — a fast way to isolate qualified leads before
    reviewing the full record. Items with no evaluations are excluded when a
    filter is given.

    Args:
        webset_id: id or externalId of the webset.
        limit: 1..100 (default 100).
        cursor: pagination cursor from a prior response (``nextCursor``).
        source_id: filter items by the source (search/import) that produced them.
        satisfied: optional ``"yes"`` / ``"no"`` / ``"unclear"`` to keep only
            items meeting that criterion-satisfaction mark (post-filter, since
            the API returns every item).
    """
    params: dict[str, Any] = {"limit": limit}
    if cursor: params["cursor"] = cursor
    if source_id: params["sourceId"] = source_id
    payload = _request("GET", f"/v0/websets/{webset_id}/items", _get_api_key(),
                       base=WEBSETS_API_URL, params=params)
    if satisfied is None:
        return payload
    if satisfied not in ("yes", "no", "unclear"):
        raise ExaError(f"satisfied must be one of 'yes'|'no'|'unclear' (got {satisfied!r}).")
    items = payload.get("data") or []
    kept = [
        it for it in items
        if any((ev.get("satisfied") == satisfied) for ev in (it.get("evaluations") or []))
    ]
    out = dict(payload)
    out["data"] = kept
    out["count"] = len(kept)
    return out


def webset_item_type(item: dict) -> str:
    """Return the entity type of a webset item: ``person``|``company``|``article``|
    ``research_paper`` | ``custom``.

    Example: ``webset_item_type(items[0]) -> "company"``.
    """
    return (item.get("properties") or {}).get("type") or "unknown"


def webset_item_name(item: dict) -> str:
    """Human-friendly name/identity of a webset item, regardless of type.

    For person items: the person's name; for company items: the company name;
    for article/custom items: the title; for research_paper: the paper title.
    Falls back to the item's URL when the name field is absent.
    """
    props = item.get("properties") or {}
    ptype = props.get("type")
    if ptype == "company":
        c = props.get("company") or {}
        return c.get("name") or props.get("url") or ""
    if ptype == "person":
        p = props.get("person") or {}
        nm = p.get("name")
        if nm: return nm
        # maybe firstName+lastName split
        fn = p.get("firstName"); ln = p.get("lastName")
        if fn or ln: return f"{fn or ''} {ln or ''}".strip()
        return props.get("url") or ""
    if ptype == "article":
        a = props.get("article") or {}
        return a.get("title") or props.get("url") or ""
    if ptype == "research_paper":
        rp = props.get("researchPaper") or {}
        return rp.get("title") or props.get("url") or ""
    if ptype == "custom":
        c = props.get("custom") or {}
        return c.get("title") or props.get("url") or ""
    return props.get("url") or str(item.get("id") or "")


def webset_item_summary(item: dict) -> str:
    """One-line (or short) readable summary of a webset item.

    Uses the item's ``description`` (the relevance statement), prefixing it with
    the entity name. All types.
    """
    name = webset_item_name(item)
    ptype = webset_item_type(item)
    desc = (item.get("properties") or {}).get("description") or ""
    parts = [name]
    if ptype and ptype != "custom":
        parts.insert(0, f"[{ptype}]")
    if desc:
        parts.append(desc)
    return " — ".join(parts)


def webset_items_all(webset_id, *, page_size=100, source_id=None):
    """Fetch ALL items of a webset by paging through the API cursor.

    ``webset_items()`` returns a single page (default <=100). This helper loops
    through the ``nextCursor`` until exhausted and concatenates all pages, so
    callers get one flat list without managing pagination themselves.

    Args:
        webset_id: the webset id or externalId.
        page_size: items per page to request (1..100, default 100).
        source_id: optional filter by the search/import that produced the items.

    Returns:
        Complete list of webset item dicts (all pages concatenated).
    """
    items: List[dict] = []
    cursor: Optional[str] = None
    while True:
        page = webset_items(webset_id, limit=page_size, cursor=cursor,
                            source_id=source_id)
        data = page.get("data") or []
        items.extend(data)
        if not page.get("hasMore") or not page.get("nextCursor"):
            break
        cursor = page["nextCursor"]
    return items


def webset_snapshot(webset_id, *, page_size=100):
    """Take a client-side snapshot of a webset's item corpus (by URL).

    Reads the full item list (``webset_items_all``) and stores each row keyed
    by canonical URL, with a UTC ``taken_at`` timestamp. A snapshot is a plain
    dict and can be saved/reloaded between calls to ``webset_snapshot_diff``
    to track what the webset's stored corpus gained / lost over time.

    Returns:
        ``{"webset_id", "taken_at", "item_count", "items_by_url": {url: {...}},
            "urls": [...]}``
    """
    items = webset_items_all(webset_id, page_size=page_size) if page_size else webset_items_all(webset_id)
    snap: Dict[str, Any] = {
        "webset_id": webset_id,
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "items_by_url": {},
        "urls": [],
    }
    for item in items:
        d = item if isinstance(item, dict) else item.to_dict()
        props = d.get("properties") or {}
        url = props.get("url") or d.get("url") or d.get("id")
        if url:
            snap["items_by_url"][url] = {
                "id": d.get("id"),
                "source": d.get("source"),
                "type": props.get("type"),
                "url": url,
                "description": props.get("description"),
                "content": props.get("content"),
            }
    snap["urls"] = list(snap["items_by_url"].keys())
    return snap


def webset_snapshot_diff(snap_a, snap_b):
    """Diff two ``webset_snapshot()`` dicts by canonical URL.

    Reports which URLs were **added** (present in ``snap_b`` only), **removed**
    (present in ``snap_a`` only), **kept** unchanged, and **changed** (same URL
    but a different ``description`` between snapshots). Purely client-side —
    no additional API call.

    Returns:
        ``{"added": [...], "removed": [...], "kept": [...], "changed": [...],
           "add_count", "remove_count", "change_count", "kept_count",
           "taken_at_a", "taken_at_b"}``
    """
    urls_a = set(snap_a.get("urls") or [])
    urls_b = set(snap_b.get("urls") or [])
    added = [
        {"url": u, **(snap_b.get("items_by_url") or {}).get(u, {})}
        for u in sorted(urls_b - urls_a)
    ]
    removed = [
        {"url": u, **(snap_a.get("items_by_url") or {}).get(u, {})}
        for u in sorted(urls_a - urls_b)
    ]
    kept = [
        {"url": u, **(snap_a.get("items_by_url") or {}).get(u, {})}
        for u in sorted(urls_a & urls_b)
    ]
    changed = []
    common = sorted(urls_a & urls_b)
    a_ib = snap_a.get("items_by_url") or {}
    b_ib = snap_b.get("items_by_url") or {}
    for u in common:
        a_item = a_ib.get(u, {})
        b_item = b_ib.get(u, {})
        if a_item.get("description") != b_item.get("description"):
            changed.append({"url": u, "before": a_item, "after": b_item})
    return {
        "added": added,
        "removed": removed,
        "kept": kept,
        "changed": changed,
        "add_count": len(added),
        "remove_count": len(removed),
        "change_count": len(changed),
        "kept_count": len(kept),
        "taken_at_a": snap_a.get("taken_at"),
        "taken_at_b": snap_b.get("taken_at"),
    }


def webset_eval_review(webset_id, *, limit=100, cursor=None, timeout=45.0):
    """Review a webset's items through the lens of their criterion evaluations.

    Each webset item carries an ``evaluations`` list — one entry per search
    criterion with ``criterion`` / ``reasoning`` / ``satisfied`` (yes|no|unclear)
    / ``references``. This helper pulls the items and renders a quick
    human-/agent-friendly review: for every item, a one-line markdown row with
    the entity name, its criterion → ``satisfied`` marks, and the key reason.

    Args:
        webset_id: the webset id or externalId.
        limit: 1..100 items to pull.
        cursor: pagination cursor from a prior call.

    Returns:
        ``{"markdown": str, "criteria": [...], "item_count": int,
           "by_satisfied": {yes: int, no: int, unclear: int}, "items": [...]}``
        ``markdown`` is the readable review; ``items`` are the raw item records
        (so callers can drill into references / enrichments programmatically).
    """
    payload = webset_items(webset_id, limit=limit, cursor=cursor)
    items = payload.get("data") or []
    by_sat: dict[str, int] = {"yes": 0, "no": 0, "unclear": 0}
    criteria_order: List[str] = []
    md: List[str] = [f"# Webset evaluation review — {webset_id}", ""]
    for it in items:
        props = it.get("properties") or {}
        name = (props.get("company") or props.get("person") or props.get("article")
                or props.get("researchPaper") or props.get("custom") or {}).get("name")                 or props.get("url") or it.get("id")
        md.append(f"## {name}")
        for ev in (it.get("evaluations") or []):
            crit = ev.get("criterion")
            sat = ev.get("satisfied")
            if crit not in criteria_order:
                criteria_order.append(crit)
            if sat in by_sat:
                by_sat[sat] += 1
            md.append(f"- **{crit}** → `{sat}`")
            if ev.get("reasoning"):
                md.append(f"  {ev['reasoning'][:220]}")
        md.append("")
    return {
        "markdown": "\n".join(md),
        "criteria": criteria_order,
        "by_satisfaction": by_sat,
        "count": len(items),
        "items": items,
    }


def webset_item_get(webset_id, item_id):
    """Fetch a single webset item (with its profile + enrichments)."""
    return _request("GET", f"/v0/websets/{webset_id}/items/{item_id}", _get_api_key(),
                    base=WEBSETS_API_URL)


def webset_item_delete(webset_id, item_id):
    """Remove an item from a webset."""
    return _request("DELETE", f"/v0/websets/{webset_id}/items/{item_id}", _get_api_key(),
                    base=WEBSETS_API_URL)


# --- Multi-search on one webset ---------------------------------------------
def webset_add_search(webset_id, query, *, count=10, entity=None, criteria=None,
                      exclude=None, scope=None, recall=False,
                      max_people_per_company=None, behavior="override", metadata=None):
    """Add another search run to an existing webset (scoped to the webset's items).

    Posts the query parameters at the top level as ``CreateWebsetSearchParameters``
    expects (NOT nested under a ``search`` key).

    Args:
        webset_id: id or externalId of the target webset.
        query: natural-language search describing entities you want.
        count: number of items to attempt to find (>=1).
        entity: override detected entity, e.g. {"type": "company"}.
        criteria: list of {"description": "..."} (<=5).
        exclude / scope: existing imports/websets to avoid or search within.
        recall: request an estimate of total reachable results.
        max_people_per_company: soft cap for people searches.
        behavior: "override" (default) or "append".
        metadata: optional string KV tags.
    """
    body: dict[str, Any] = {"query": query, "count": count}
    if behavior: body["behavior"] = behavior
    if entity: body["entity"] = entity
    if criteria: body["criteria"] = criteria
    if exclude: body["exclude"] = exclude
    if scope: body["scope"] = scope
    if recall: body["recall"] = True
    if max_people_per_company is not None: body["maxPeoplePerCompany"] = max_people_per_company
    if metadata: body["metadata"] = metadata
    return _request("POST", f"/v0/websets/{webset_id}/searches", _get_api_key(),
                    base=WEBSETS_API_URL, json_body=body)


def webset_search_status(webset_id, search_id):
    """Get the status/progress of a specific webset search run."""
    return _request("GET", f"/v0/websets/{webset_id}/searches/{search_id}", _get_api_key(),
                    base=WEBSETS_API_URL)


def webset_recall(webset_id, *, search_id=None, timeout=45.0):
    """Return the estimated total-match (``recall``) analysis for a Webset search.

    When a webset search is created with ``recall=True``, the API computes a
    read-side estimate of how many total matching records could exist — a
    ``recall`` block on the completed search with the expected total, a
    confidence (high|medium|low), a min/max bounds range, and the reasoning
    that produced the estimate. This helper extracts that block into a
    compact, agent-friendly summary so you can judge coverage (how much of the
    true market you've captured) before acting.

    Args:
        webset_id: webset id or externalId.
        search_id: optional; if omitted, uses the most recent search attached
            to the webset.
        timeout: request timeout (s).

    Returns:
        ``{"summary": str, "expected_total": int|None, "confidence": str|None,
           "bounds": (min, max)|None, "reasoning": str|None,
           "search_id": str, "status": str}``.
        ``summary`` is a one-paragraph readable wrap-up; the numeric fields are
        extracted for programmatic use. If the search did not request recall
        (or hasn't produced one yet), ``expected_total`` is ``None``.
    """
    if search_id is None:
        ws = webset_get(webset_id)
        searches = ws.get("searches") or []
        if not searches:
            return {"exists": False, "expected_total": None, "confidence": None,
                    "bounds": None, "reasoning": None, "summary":
                    f"Webset {webset_id} has no searches yet.",
                    "search_id": None, "status": (ws.get("status") or "unknown")}
        # prefer the last search that produced a recall; otherwise the most recent
        recall_search = next((s for s in reversed(searches) if s.get("recall")), None)
        search_id = (recall_search or searches[-1])["id"]
    data = webset_search_status(webset_id, search_id)
    rec = data.get("recall") or {}
    expected = rec.get("expected") or {}
    total = expected.get("total")
    conf = expected.get("confidence")
    bounds = expected.get("bounds") or {}
    reason = rec.get("reasoning")
    if total is None:
        summary = (f"Search `{search_id}` (status={data.get('status')}) has no recall "
                   "estimate; create the search with recall=True to get one.")
    else:
        summary = (f"Estimated {total} total potential matches (confidence={conf}, "
                   f"range {bounds.get('min')}..{bounds.get('max')}). {reason or ''}")
    return {
        "exists": total is not None,
        "expected_total": total,
        "confidence": conf,
        "bounds": (bounds.get("min"), bounds.get("max")) if bounds else None,
        "reasoning": reason,
        "summary": summary,
        "search_id": search_id,
        "status": data.get("status"),
    }


def webset_search_cancel(webset_id, search_id):
    """Cancel a specific running search on a webset."""
    return _request("POST", f"/v0/websets/{webset_id}/searches/{search_id}/cancel",
                    _get_api_key(), base=WEBSETS_API_URL)


def webset_wait(webset_id, *, search_id=None, wait_enrichments=True,
                timeout=600, poll_interval=5.0):
    """Poll a webset's (or one search's) status until terminal.

    By default waits for all searches AND any enrichments to reach a terminal
    state, then returns the webset dict with embedded items. Set
    ``wait_enrichments=False`` to return as soon as the search phase completes
    (enrichments may still be running).
    """
    deadline = time.time() + timeout
    def _tick():
        if search_id:
            data = webset_search_status(webset_id, search_id)
            if data.get("status") in ("completed", "cancelled", "failed"):
                return webset_get(webset_id, include_items=True)
            return None
        data = webset_get(webset_id)
        searches = data.get("searches") or []
        if data.get("status") in ("paused", "cancelled") or not searches:
            return data
        searches_done = all(s.get("status") in ("completed", "cancelled", "failed") for s in searches)
        enrichs = data.get("enrichments") or []
        enrichs_done = (all((e.get("state") or e.get("status")) in ("completed", "failed", "cancelled")
                             for e in enrichs) if enrichs else True)
        if searches_done and (not wait_enrichments or enrichs_done):
            return webset_get(webset_id, include_items=True)
        return None
    done = _poll_until(_tick, deadline=deadline,
                       initial=min(0.4, float(poll_interval or 5.0)),
                       cap=max(0.8, float(poll_interval or 5.0)))
    if done is None:
        raise ExaError(f"webset {webset_id} did not finish within {timeout}s.")
    return done


# --- Enrichment -------------------------------------------------------------
def webset_enrich(webset_id, description, *, format=None, options=None, metadata=None):
    """Ask the enrichment agent to produce one extra field per webset item.

    Each enrichment matches a description (e.g. "primary email address and
    phone number") against each item and stores the result back on the item.

    Args:
        description: what to find/produce for each item (<=5000 chars).
        format: "text"|"date"|"number"|"options"|"email"|"phone"|"url" (auto if omitted).
        options: when format="options", list of {"label": ...} choices (<=150).

    Returns:
        The created enrichment object (``id``, ``status``).
    """
    body: dict[str, Any] = {"description": description}
    if format: body["format"] = format
    if options: body["options"] = options
    if metadata: body["metadata"] = metadata
    return _request("POST", f"/v0/websets/{webset_id}/enrichments", _get_api_key(),
                    base=WEBSETS_API_URL, json_body=body)


def enrichment_get(webset_id, enrichment_id):
    """Fetch the current state of an enrichment job."""
    return _request("GET", f"/v0/websets/{webset_id}/enrichments/{enrichment_id}",
                    _get_api_key(), base=WEBSETS_API_URL)


def enrichment_delete(webset_id, enrichment_id):
    """Remove an enrichment job from a webset."""
    return _request("DELETE", f"/v0/websets/{webset_id}/enrichments/{enrichment_id}",
                    _get_api_key(), base=WEBSETS_API_URL)


def enrichment_cancel(webset_id, enrichment_id):
    """Cancel a running enrichment job."""
    return _request("POST", f"/v0/websets/{webset_id}/enrichments/{enrichment_id}/cancel",
                    _get_api_key(), base=WEBSETS_API_URL)


def enrichment_update(webset_id, enrichment_id, *, description=None, format=None,
                      options=None, metadata=None):
    """Update an enrichment's description / format / options / metadata.

    Args:
        webset_id: id or externalId of the webset.
        enrichment_id: id of the enrichment job.
        description: new task description (1..5000 chars).
        format: one of text|date|number|options|email|phone|url.
        options: when format="options", list of {"label": "..."} (1..150).
        metadata: optional string KV.

    Returns the updated enrichment object.
    """
    body: dict[str, Any] = {}
    if description: body["description"] = description
    if format: body["format"] = format
    if options: body["options"] = options
    if metadata is not None: body["metadata"] = metadata
    return _request("PATCH", f"/v0/websets/{webset_id}/enrichments/{enrichment_id}",
                    _get_api_key(), base=WEBSETS_API_URL, json_body=body)


# --- Imports (bring your own CSV entity lists) ------------------------------
def import_create(csv_path, *, title, entity, identifier_column=0, metadata=None,
                  timeout=60.0):
    """Upload a CSV of entity records (URL column typical) to use as the seed /
    scope for webset searches and enrichments.

    Args:
        csv_path: local path to the CSV file (<= 50 MB).
        title: name for the import.
        entity: entity type dict e.g. {"type":"company"}.
        identifier_column: 0-based column index holding the key identifier (URL).

    Returns:
        The created import object with processing ``status``.
    """
    import os
    size = os.path.getsize(csv_path)
    with open(csv_path, "r", encoding="utf-8") as fh:
        # quick line count for the `count` field
        nrows = sum(1 for _ in fh)
    body: dict[str, Any] = {
        "format": "csv",
        "size": size,
        "count": nrows,
        "entity": entity,
        "title": title,
        "csv": {"identifier": identifier_column},
    }
    if metadata: body["metadata"] = metadata
    return _request("POST", "/v0/imports", _get_api_key(), base=WEBSETS_API_URL,
                    json_body=body, timeout=timeout)


def imports_list(*, limit=50, cursor=None):
    """List imports for the authenticated team."""
    params: dict[str, Any] = {"limit": limit}
    if cursor: params["cursor"] = cursor
    return _request("GET", "/v0/imports", _get_api_key(), base=WEBSETS_API_URL, params=params)


def import_get(import_id):
    """Fetch a single import by id."""
    return _request("GET", f"/v0/imports/{import_id}", _get_api_key(), base=WEBSETS_API_URL)


def import_delete(import_id):
    """Delete an import and detach it from websets."""
    return _request("DELETE", f"/v0/imports/{import_id}", _get_api_key(), base=WEBSETS_API_URL)


def import_update(import_id, *, title=None, metadata=None):
    """Update an import's title and/or metadata."""
    body: dict[str, Any] = {}
    if title: body["title"] = title
    if metadata is not None: body["metadata"] = metadata
    return _request("PATCH", f"/v0/imports/{import_id}", _get_api_key(), base=WEBSETS_API_URL,
                    json_body=body)


# --- Webhooks (receive event notifications) ----------------------------------
def webhook_create(events, url, *, metadata=None):
    """Register a webhook to receive events for webset/import/monitor activity.

    Args:
        events: list of event type strings (e.g. ["webset.created","webset.completed"]).
        url: HTTPS endpoint that receives POST payloads.
        metadata: dict of string KV for routing.

    Returns:
        webhook object incl. the ``secret`` used to verify request signatures.
    """
    body: dict[str, Any] = {"events": events, "url": url}
    if metadata: body["metadata"] = metadata
    return _request("POST", "/v0/webhooks", _get_api_key(), base=WEBSETS_API_URL, json_body=body)


def webhook_list(*, limit=50, cursor=None):
    """List all webhooks."""
    params: dict[str, Any] = {"limit": limit}
    if cursor: params["cursor"] = cursor
    return _request("GET", "/v0/webhooks", _get_api_key(), base=WEBSETS_API_URL, params=params)


def webhook_get(webhook_id):
    return _request("GET", f"/v0/webhooks/{webhook_id}", _get_api_key(), base=WEBSETS_API_URL)


def webhook_update(webhook_id, *, events=None, url=None, metadata=None):
    """Patch a webhook (events / url / metadata)."""
    body: dict[str, Any] = {}
    if events is not None: body["events"] = events
    if url is not None: body["url"] = url
    if metadata is not None: body["metadata"] = metadata
    return _request("PATCH", f"/v0/webhooks/{webhook_id}", _get_api_key(), base=WEBSETS_API_URL,
                    json_body=body)


def webhook_delete(webhook_id):
    return _request("DELETE", f"/v0/webhooks/{webhook_id}", _get_api_key(), base=WEBSETS_API_URL)


def webhook_attempts(webhook_id, *, limit=50, cursor=None, event_type=None,
                         successful=None):
    """List delivery attempts for a webhook, with optional filtering.

    Args:
        webhook_id: the target webhook's id.
        limit: 1..N (default 50).
        cursor: pagination cursor from a previous response's ``nextCursor``.
        event_type: filter attempts by event type (e.g. ``"webset.search.completed"``).
        successful: filter by outcome — pass ``True`` for delivered, ``False``
            for failed attempts (the API accepts a boolean).

    Returns:
        Raw payload: ``{"data": [...], "hasMore": bool, "nextCursor": ...}``.
        Each attempt carries ``eventType``, ``successful``, ``responseStatusCode``,
        ``attempt``, ``attemptedAt``, and ``responseBody``/``responseHeaders``.
    """
    params: dict[str, Any] = {"limit": limit}
    if cursor:      params["cursor"] = cursor
    if event_type:  params["eventType"] = event_type
    if successful is not None:
        params["successful"] = "true" if successful else "false"
    return _request("GET", f"/v0/webhooks/{webhook_id}/attempts", _get_api_key(),
                    base=WEBSETS_API_URL, params=params)


# --- Events (audit log / onboarding) ----------------------------------------
def event_list(*, limit=50, cursor=None, event_type=None, types=None,
                 created_before=None, created_after=None):
    """List recent WebSet/import/... events for your team with rich filtering.

    Args:
        limit: max events to return (default 50).
        cursor: pagination from a previous response.
        event_type: single event-type filter (singular ``type`` param), e.g.
            ``"webset.search.completed"``.
        types: list of event types to include (plural ``types`` API param).
            When both ``event_type`` and ``types`` are given they combine.
        created_before: ISO-8601 timestamp — only events created before this.
        created_after: ISO-8601 timestamp — only events created after this.

    Returns:
        ``{"data": [...], "hasMore": bool, "nextCursor": ...}``.
    """
    params: dict[str, Any] = {"limit": limit}
    if cursor: params["cursor"] = cursor
    if event_type: params["type"] = event_type
    t_types: List[str] = []
    if types:
        t_types.extend(types)
    if event_type and event_type not in t_types:
        t_types.append(event_type)
    if t_types:
        params["types"] = t_types
    if created_before: params["createdBefore"] = created_before
    if created_after:  params["createdAfter"] = created_after
    return _request("GET", "/v0/events", _get_api_key(), base=WEBSETS_API_URL, params=params)


def event_get(event_id):
    """Fetch a single event by id."""
    return _request("GET", f"/v0/events/{event_id}", _get_api_key(), base=WEBSETS_API_URL)




# ---------------------------------------------------------------------------
# deep_research()  — fan-out orchestration across several related questions
# ---------------------------------------------------------------------------
def deep_research(
    queries,
    *,
    num_results: int = 8,
    mode: str = "auto",
    search_type: Optional[str] = None,
    category: Optional[str] = None,
    include_domains=None,
    exclude_domains=None,
    start_published_date: Optional[str] = None,
    end_published_date: Optional[str] = None,
    with_text: bool = False,
    with_highlights: bool = True,
    highlights_query: Optional[str] = None,
    highlights_max_characters: Optional[int] = None,
    max_characters: Optional[int] = None,
    text_verbosity: Optional[str] = None,
    with_summary: bool = False,
    summary_query: Optional[str] = None,
    summary_schema: Optional[dict] = None,
    extras_links: int = 0,
    extras_image_links: int = 0,
    extras_rich_links: int = 0,
    extras_rich_image_links: int = 0,
    extras_code_blocks: int = 0,
    max_age_hours: Optional[int] = None,
    subpages: int = 0,
    output_schema: Optional[dict] = None,
    system_prompt: Optional[str] = None,
    user_location: Optional[str] = None,
    moderation: bool = False,
    timeout: float = 120.0,
    dedupe: bool = True,
) -> SearchResults:
    """Multi-query research: run several related questions and merge results.

    Pass a list of related queries (e.g. the facets of a research question)
    instead of one. Each is searched and results are merged, de-duplicated by
    URL (kept per query if ``dedupe=False``), and re-ranked so results shared by
    more queries bubble to the top. Useful for quickly assembling a broad,
    source-rich picture across several angles.

    ``search_type`` defaults to the mode's default (usually ``auto``); pass a
    deep-* variant (``deep``, ``deep-lite``, ``deep-reasoning``) to give every
    sub-query heavier synthesized retrieval, or a cheap ``fast``/``instant`` to
    fan out quickly.

    Args:
        queries: One or more related query strings. If a plain string is given
            it is wrapped in a list.
        num_results: per-query result count (default 8). Total is
            ~num_results * len(queries) before de-dup.
        dedupe: True (default) -> near-duplicates (same canonical URL, `www.`-
            insensitive) are merged and results shared by multiple queries are
            ranked first. False -> keep every per-query result as-is.

    Returns:
        A merged ``SearchResults``. With dedupe, ``result.extras["query_hits"]``
        is set to the query count when a page matched more than one query.

    Returns:
        A merged ``SearchResults``. For deduped results, the ``Result.extra``
        field is set to {"query_hits": <count>} when a page matched >1 query.
    """
    if isinstance(queries, str):
        queries = [queries]
    if not queries:
        raise ExaError("deep_research requires at least one query.")
    calls = []
    for q in queries:
        calls.append(search(
            q, mode=mode, num_results=num_results, search_type=search_type,
            category=category, include_domains=include_domains,
            exclude_domains=exclude_domains,
            start_published_date=start_published_date,
            end_published_date=end_published_date,
            with_text=with_text, with_highlights=with_highlights,
            highlights_query=highlights_query,
            highlights_max_characters=highlights_max_characters,
            max_characters=max_characters, text_verbosity=text_verbosity,
            with_summary=with_summary, summary_query=summary_query,
            summary_schema=summary_schema,
            extras_links=extras_links, extras_image_links=extras_image_links,
            extras_rich_links=extras_rich_links,
            extras_rich_image_links=extras_rich_image_links,
            extras_code_blocks=extras_code_blocks,
            max_age_hours=max_age_hours, subpages=subpages,
            output_schema=output_schema, system_prompt=system_prompt,
            user_location=user_location, moderation=moderation,
            timeout=timeout,
        ))
    if not dedupe:
        merged: List[Result] = []
        idx = 1
        for res in calls:
            for r in res.results:
                r.index = idx
                merged.append(r)
                idx += 1
        return SearchResults(query=" / ".join(queries), results=merged,
                             request_id=",".join(c.request_id for c in calls),
                             cost_dollars=None, raw=None,
                             search_time_ms=_sum_search_times_defined(calls))

    def _canon(u: str) -> str:
        # collapse trivial www. differences so variants of the same host match
        return u.replace("https://www.", "https://").replace("http://www.", "http://")

    by_url: Dict[str, Result] = {}
    hits: Dict[str, int] = {}
    order: List[str] = []
    for res in calls:
        for r in res.results:
            u = _canon(r.url or r.id or "")
            if not u:
                continue
            if u not in by_url:
                by_url[u] = r
                order.append(u)
            hits[u] = hits.get(u, 0) + 1
    merged = []
    for i, u in enumerate(order, start=1):
        r = by_url[u]
        h = hits.get(u, 0)
        if h > 1:
            r.extras = dict(r.extras or {})
            r.extras["query_hits"] = h
        r.index = i
        merged.append(r)
    merged.sort(key=lambda r: (hits.get(_canon(r.url or r.id or ""), 0), 0), reverse=True)
    for i, r in enumerate(merged, start=1):
        r.index = i
    if not merged:
        raise ExaError("deep_research returned no results for any query.")
    return SearchResults(query=" / ".join(queries), results=merged,
                          request_id=",".join(c.request_id for c in calls),
                          cost_dollars=None, raw=None,
                          search_time_ms=_sum_search_times_defined(calls))



def merge_searches(*search_results, dedupe: bool = True) -> SearchResults:
    """Merge multiple ``SearchResults`` into one, de-duplicated by URL.

    Useful after a fan-out (e.g. several ``search()`` calls with different
    modes/domains) to get a single unified ``SearchResults``. Duplicate pages
    (same canonical URL, www-insensitive) are merged; when a result appears in
    more than one search, it keeps the first occurrence's metadata and its
    ``extras["query_hits"]`` is set to the number of distinct queries that
    matched it.

    Args:
        *search_results: One or more ``SearchResults`` (or iterables of
            ``Result``/dict-like) to merge.
        dedupe: Default True — collapse identical canonical URLs; keep every
            occurrence when False (same as ``deep_research``'s behavior).

    Returns:
        A single ``SearchResults`` with the merged results. If only one
        ``SearchResults`` is passed, it is returned unchanged (fast path).
    """
    if not search_results:
        raise ExaError("merge_searches requires at least one SearchResults.")
    if len(search_results) == 1 and dedupe:
        return search_results[0] if isinstance(search_results[0], SearchResults) else search_results[0]

    def _canon(u: str) -> str:
        return u.replace("https://www.", "https://").replace("http://www.", "http://")

    by_url: Dict[str, Result] = {}
    hits: Dict[str, int] = {}
    order: List[str] = []
    extra_results = []
    request_ids = []
    idx = 1

    for sr in search_results:
        if isinstance(sr, SearchResults):
            request_ids.append(sr.request_id or "")
            res_iter = sr.results
        else:
            res_iter = sr
        for r in res_iter:
            if isinstance(r, dict):
                r = Result(r, idx)
            u = _canon(r.url or r.id or "")
            if not u:
                r.index = idx
                extra_results.append(r)
                idx += 1
                continue
            if not dedupe:
                r.index = idx
                by_url[u] = r
                order.append(u)
                idx += 1
                continue
            if u not in by_url:
                by_url[u] = r
                order.append(u)
            hits[u] = hits.get(u, 0) + 1

    merged = []
    for i, u in enumerate(order, start=1):
        r = by_url[u]
        h = hits.get(u, 0)
        if h > 1:
            r.extras = dict(r.extras or {})
            r.extras["query_hits"] = h
        r.index = i
        merged.append(r)

    # Append non-URL results (no url/id) after the URL-deduped ones
    for i, r in enumerate(extra_results, start=len(merged) + 1):
        r.index = i
        merged.append(r)

    if not merged:
        raise ExaError("merge_searches produced no results.")
    query_combined = " / ".join(getattr(s, "query", "") or "?" for s in search_results)
    return SearchResults(
        query=query_combined,
        results=merged,
        request_id=",".join(rid for rid in request_ids if rid),
        raw=None,
        search_time_ms=_sum_search_times_defined(search_results),
    )


def news_roundup(query, *, days: int = 10, num_results: int = 5,
                   highlights_cap: int = 250, timeout: float = 45.0) -> SearchResults:
    """Sweep the last ``days`` days of fresh news for ``query`` and rank by freshness.

    Runs ``days`` single-day ``search(mode="news")`` windows (from the most recent
    day backward), each restricted to ``startPublishedDate``/``endPublishedDate``
    for that day, then de-duplicates by URL across windows. Each result is tagged
    with the day offsets it appeared in (``result.extras["days_seen"]``); pages
    that persist across several of the most recent days (a strong freshness
    signal) rank first. Purely client-composed convenience over normal news-mode
    searches — no new API surface is assumed.

    Args:
        query: The news query (e.g. "AI chip funding").
        days: number of trailing day-windows to sweep (>=1, default 10).
        num_results: per-window result cap (default 5). The returned total is
            roughly ``<= days * num_results`` before de-dup.
        highlights_cap: char cap for each result's highlight snippet (<=250).
        timeout: per-window request timeout.

    Returns:
        A ``SearchResults`` of de-duplicated fresh results plus roundup metadata:
        ``.window_counts`` maps day-offset -> hits in that window, and each
        result's ``extras["days_seen"]`` lists the day offsets the URL appeared on.
    """
    if days < 1:
        raise ExaError("news_roundup requires days >= 1.")
    today = datetime.now(timezone.utc).date()
    window_meta: List[dict] = []
    url_map: Dict[str, dict] = {}
    for i in range(days):
        day_start = datetime.combine(today - timedelta(days=i + 1), datetime.min.time(), tzinfo=timezone.utc)
        day_end   = datetime.combine(today - timedelta(days=i),     datetime.min.time(), tzinfo=timezone.utc)
        window_count = 0
        try:
            res = search(
                query, mode="news", num_results=num_results,
                start_published_date=day_start.isoformat(),
                end_published_date=day_end.isoformat(),
                with_highlights=True,
                highlights_max_characters=min(max(highlights_cap, 1), 250),
                timeout=timeout,
            )
            for r in res.results:
                u = (r.url or r.id or "")
                if not u:
                    continue
                window_count += 1
                rec = url_map.setdefault(u, {"days": set(), "result": r})
                rec["days"].add(i)
        except ExaError:
            window_count = 0  # a single empty/failed day-window is not fatal
        window_meta.append({"day": i, "count": window_count})

    ranked = sorted(url_map.values(),
                    key=lambda rec: (-len(rec["days"]), rec["result"].published_date or ""))
    results: List[Result] = []
    for j, rec in enumerate(ranked, start=1):
        r = rec["result"]
        r.index = j
        r.extras = dict(r.extras or {})
        r.extras["days_seen"] = sorted(rec["days"])
        results.append(r)
    if not results:
        raise ExaError(f"news_roundup found no fresh results for {query!r} in the last {days} days.")

    sr = SearchResults(
        query=f"{query} (fresh news, last {days}d)",
        results=results,
        request_id=",".join(f"day{w['day']}" for w in window_meta),
        search_time_ms=None,
    )
    sr.window_counts = {str(w["day"]): w["count"] for w in window_meta}  # type: ignore[attr-defined]
    return sr


def company_dossier(company: str, *, num_results: int = 3, fetch_website: bool = True,
                    text_cap: int = 1800, max_age_hours: Optional[int] = None,
                    timeout: float = 60.0) -> dict:
    """Build a structured **company brief** (entity profile + summary + website copy).

    One call that assembles the pieces you would otherwise fetch by hand:

      1. ``search(mode="company")`` to find the best matching company entity
         (Exa's ``company`` category targets company pages/profiles).
      2. Normalize that entity with ``entity_summary`` -> ``foundedYear``,
         ``workforce``, ``headquarters``, ``financials`` (revenue / funding).
      3. Optionally ``fetch`` the homepage (semantic ``body`` sections only) to
         capture ``about`` copy, headlines, and brand language in ``website_text``.

    Purely composed from existing, live-verified calls — no new API assumption.

    Args:
        company: Company name or natural-language query (e.g. "Anthropic").
        num_results: how many company-mode results to inspect (default 3); the
            first structured company entity wins.
        fetch_website: True (default) -> fetch the top match's page body.
        text_cap: max characters of website body text to retain (default 1800).
        max_age_hours: optional cache freshness for the website fetch.
        timeout: request timeout for each sub-call.

    Returns:
        A dict: ``query``, ``name``, ``entity_type``, ``profile`` (normalized
        entity), ``source_url``, ``summary``, ``search_results`` (list of dict),
        ``raw_entity``, and — when ``fetch_website`` succeeds — ``website_url``,
        ``website_title``, ``website_text`` (or ``website_error`` on failure).
    """
    res = search(
        company, mode="company", num_results=num_results,
        with_summary=True, summary_query="Company overview: what the company does",
        timeout=timeout,
    )
    best: List[tuple] = []
    for r in res.results:
        for e in (r.entities or []):
            best.append((r, e))
    if not best:
        raise ExaError(f"company_dossier: no structured company entity found for {company!r}. "
                       "Try a more specific company name.")
    result_r, raw_entity = best[0]
    profile_sum = entity_summary(raw_entity)
    out: dict = {
        "query": company,
        "name": profile_sum.get("name") or result_r.title,
        "entity_type": profile_sum.get("type"),
        "profile": profile_sum,
        "source_url": result_r.url,
        "summary": result_r.summary,
        "search_results": [r.to_dict() for r in res.results],
        "raw_entity": raw_entity,
    }
    if fetch_website and result_r.url:
        try:
            body = fetch([result_r.url], max_characters=text_cap,
                         include_sections=["body"], text_verbosity="standard",
                         max_age_hours=max_age_hours, timeout=timeout)
            if body and isinstance(body, list) and body[0].get("text"):
                out["website_url"] = body[0].get("url") or result_r.url
                out["website_title"] = body[0].get("title")
                out["website_text"] = body[0]["text"][:text_cap]
        except ExaError as e:
            out["website_error"] = str(e)[:200]
    return out


def _coerce_result_list(items) -> List[Result]:
    """Normalize an iterable of ``Result`` / ``dict`` into a list of ``Result``."""
    out: List[Result] = []
    for i, r in enumerate(items, start=1):
        if isinstance(r, dict):
            r = Result(r, i)
        out.append(r)
    return out


def diff_results(a, b, *, key=None) -> dict:
    """Diff two search-result sets by canonical URL -> added / removed / changed.

    Given two result collections (``SearchResults``, lists of ``Result``, or
    plain dicts — e.g. two ``news_roundup`` sweeps, two ``deep_research`` runs,
    or two monitor/webset result snapshots), report which pages appeared in ``b``
    but not ``a`` (``added``), in ``a`` but not ``b`` (``removed``), and which
    kept the same URL but changed title/date (``changed``). Purely a local
    convenience working on already-fetched data — no new API call.

    Args:
        a: baseline result collection.
        b: newer result collection.
        key: optional identity callable (default ``(url or id)``) used to match
            results across the two sets.

    Returns:
        A dict: ``a_count``/``b_count`` (unique-key totals), ``added``/``removed``
        (lists of ``Result``, URL-deduped), ``changed`` (list of ``(before, after)``
        tuples), and ``report`` — a readable multi-line human summary.
    """
    key_fn = key if key is not None else (lambda r: (r.url or r.id or ""))
    aa = _coerce_result_list(a)
    bb = _coerce_result_list(b)
    ka = {key_fn(r): r for r in aa if key_fn(r)}
    kb = {key_fn(r): r for r in bb if key_fn(r)}
    added_all   = [kb[u] for u in kb if u not in ka]
    removed_all = [ka[u] for u in ka if u not in kb]
    changed: List[tuple] = []
    for u in set(ka) & set(kb):
        ra, rb = ka[u], kb[u]
        if ra.title != rb.title or ra.published_date != rb.published_date:
            changed.append((ra, rb))

    def _rows(lst):
        seen, out = set(), []
        for r in lst:
            u = key_fn(r)
            if u in seen:
                continue
            seen.add(u)
            out.append(r)
        return out

    added_list   = _rows(added_all)
    removed_list = _rows(removed_all)

    lines: List[str] = [f"Results diff: {len(ka)} -> {len(kb)}"]
    if added_list:
        lines.append(f"  +{len(added_list)} added:")
        for r in added_list[:8]:
            lines.append(f"    + {r.title[:60]}  {r.url}")
    if removed_list:
        lines.append(f"  -{len(removed_list)} removed:")
        for r in removed_list[:8]:
            lines.append(f"    - {r.title[:60]}  {r.url}")
    if changed:
        lines.append(f"  ~{len(changed)} changed:")
        for ra, rb in changed[:5]:
            lines.append(f"    ~ {ra.title[:35]} -> {rb.title[:35]}")
    if not (added_list or removed_list or changed):
        lines.append("  no changes.")

    return {
        "a_count": len(ka),
        "b_count": len(kb),
        "added": added_list,
        "removed": removed_list,
        "changed": changed,
        "report": "\n".join(lines),
    }

def search_to_csv(results, path, *, include_entities: bool = False,
                  encoding: Optional[str] = None) -> str:
    """Write a ``SearchResults`` (or any iterable of ``Result``) to a CSV file.

    A pipeline-friendly exporter for tabular analysis of search output. Uses
    the exact same row layout as :meth:`SearchResults.to_dataframe` (title,
    url, domain, hostname, published_date, author, score, snippet, summary,
    highlights), with an optional raw ``entities`` JSON column. ``pandas`` /
    ``csv`` are imported lazily.

    Args:
        results: a ``SearchResults``, list of ``Result``, or iterable of
            dict-like result objects.
        path: destination path (``.csv``).
        include_entities: add the raw ``entities`` JSON column.
        encoding: optional file encoding (e.g. "utf-8").

    Returns:
        ``path`` (str) for easy chaining.
    """
    import pandas as pd  # lazy import
    if not isinstance(results, SearchResults):
        sr = SearchResults({"results": list(results)})
    else:
        sr = results
    df = sr.to_dataframe(include_entities=include_entities)
    df.to_csv(path, index=False, encoding=encoding or "utf-8")
    return path


def search_to_jsonl(results, path, *, include_entities: bool = True,
                    encoding: Optional[str] = None) -> str:
    """Write a ``SearchResults`` (or iterable of ``Result``) to a JSONL file.

    A JSON-native, ``pandas``-free exporter: one compact JSON object per result
    per line (title, url, domain, hostname, published_date, author, score,
    snippet, summary, highlights, and — by default — the raw ``entities`` list).
    Ideal for streaming into downstream JSON pipelines (jq, ndjson loaders).
    Only stdlib ``json`` is used, so this works even where pandas isn't
    installed (unlike ``search_to_csv`` / ``to_xlsx``).

    Args:
        results: a ``SearchResults``, list of ``Result``, or iterable of
            dict-like result objects.
        path: destination path (``.jsonl``).
        include_entities: include the raw ``entities`` JSON column per line
            (default True — JSON-native, so no bloat concern).
        encoding: optional file encoding (e.g. "utf-8").

    Returns:
        ``path`` (str) for easy chaining.
    """
    if not isinstance(results, SearchResults):
        sr = SearchResults({"results": list(results)})
    else:
        sr = results
    enc = encoding or "utf-8"
    with open(path, "w", encoding=enc) as fh:
        for r in sr.results:
            row = {
                "title": r.title,
                "url": r.url,
                "domain": r.domain,
                "hostname": r.hostname,
                "published_date": r.published_date,
                "author": r.author,
                "score": r.score,
                "snippet": r.snippet,
                "summary": r.summary,
                "highlights": r.highlights,
            }
            if include_entities:
                row["entities"] = r.entities
            fh.write(json.dumps(row, ensure_ascii=enc == "ascii"))
            fh.write("\n")
    return path


def _sum_search_times_defined(searches) -> Optional[float]:
    """Sum the non-None ``search_time_ms`` across a collection (list/tuple/iterable)
    of ``SearchResults``, returning ``None`` if no member reported a time."""
    total = None
    for s in searches:
        st = getattr(s, "search_time_ms", None)
        if st is not None:
            total = (total or 0.0) + float(st)
    return total


def explain(url, *, highlight_query=None, max_characters=400,
           text_verbosity="compact", timeout=45.0):
    """One-call page explanation: fetch + summary + highlights → readable.

    A client-side convenience over ``fetch()``: retrieves the page's text plus
    a semantic ``summary`` and highlights in one request, then renders a
    readable dict with a ``markdown`` string you can drop into a chat / note.

    Args:
        url: The URL (or Exa document id) to explain.
        highlight_query: Optional steering query for which highlights to keep.
        max_characters:  Text length cap for the page body.
        text_verbosity:  compact|standard|full.
        timeout: fetch timeout.

    Returns:
        Dict with ``title``, ``url``, ``summary``, ``highlights``, ``text``,
        ``image``, ``favicon`` and a ready-to-read ``markdown`` render.
    """
    pages = fetch(
        url,
        mode="text",
        with_summary=True, with_highlights=True,
        highlights_query=highlight_query,
        max_characters=max_characters,
        text_verbosity=text_verbosity,
        timeout=timeout,
    )
    if isinstance(pages, list) and pages:
        page = pages[0]
    elif isinstance(pages, dict):
        page = pages
    else:
        raise ExaError(f"explain: no content for {url}")
    if page.get("error"):
        raise ExaError(f"explain: {page.get('error')}")

    title = page.get("title") or "(untitled)"
    page_url = page.get("url") or url
    summary_text = page.get("summary")
    highlights = page.get("highlights") or []
    text = page.get("text") or ""
    image = page.get("image")
    favicon = page.get("favicon")

    parts = [f"## {title}", ""]
    if page_url:
        parts.append(f"**URL:** {page_url}")
        parts.append("")
    if summary_text:
        parts.append(f"**Summary:** {summary_text}")
        parts.append("")
    if highlights:
        parts.append("**Key highlights:**")
        parts.append("")
        for h in highlights[:5]:
            parts.append(f"- {h[:200]}")
            parts.append("")
    if text and not summary_text and not highlights:
        parts.append(str(text)[:max_characters])
        parts.append("")

    return {
        "title": title,
        "url": page_url,
        "summary": summary_text,
        "highlights": highlights,
        "text": text,
        "image": image,
        "favicon": favicon,
        "markdown": "\n".join(parts),
    }


def magic(query, *, num_results=5, citation_format=None, **search_kwargs):
    """One-call research: deep search + citation-grounded answer + markdown.

    Runs ``search(query, search_type='deep', ...)`` for deep-ranked results
    (with highlights + summaries by default), then ``answer(query)`` for the
    citation-grounded synthesis. Returns a dict with both, plus a readable
    markdown render: summary → cited sources → top deep-research results.

    Args:
        query: The research question.
        num_results: Result count for the deep search.
        citation_format: Forwarded to ``answer()`` for rich citation fields.
        **search_kwargs: Extra ``search()`` kwargs (``with_highlights`` and
            ``with_summary`` default True).

    Returns:
        ``{"query", "summary", "citations", "results", "answer",
          "cost_dollars", "request_id", "markdown"}`` where ``results`` is a
          ``SearchResults`` and ``answer`` an ``Answer``.
    """
    search_kwargs.setdefault("with_highlights", True)
    search_kwargs.setdefault("with_summary", True)
    sr = search(query, search_type="deep", num_results=num_results, **search_kwargs)
    ans = answer(query, citation_format=citation_format)

    p = [f"## {query}", ""]
    p.append("### Summary")
    p.append("")
    if ans.answer:
        p.append(str(ans.answer))
        p.append("")
    if ans.citations:
        p.append(f"### {len(ans.citations)} cited sources")
        p.append("")
        for i, c in enumerate(ans.citations, 1):
            title = c.get("title") or "(untitled)"
            url = c.get("url") or ""
            p.append(f"{i}. **{title}**" + (f" — {url}" if url else ""))
        p.append("")
    p.append(f"### Top {len(sr)} deep results")
    p.append("")
    for i, r in enumerate(sr, 1):
        p.append(f"{i}. **{r.title}** — {r.url}")
        if r.summary:
            p.append(f"   {r.summary[:120]}...")
        p.append("")

    return {
        "query": query,
        "summary": ans.answer,
        "citations": ans.citations,
        "results": sr,
        "answer": ans,
        "cost_dollars": ans.cost_dollars,
        "request_id": ans.request_id,
        "markdown": "\n".join(p),
    }


def modes() -> str:
    """List all supported Exa modes, search types, and entry points."""
    lines = ["Exa modes (pass as mode=... to exa.search / await exa):"]
    for name, spec in MODES.items():
        cat = spec["category"] or "auto"
        lines.append(f"  {name:15} (category={cat:15}) {spec['hint']}")
    lines.append("\nSearch types (search_type=): " + ", ".join(sorted(SEARCH_TYPES))
                 + "  (legacy: `magic` -> deep, `semantic` is invalid)")
    lines.append("\nEntry points:")
    lines.append("  exa.search(...)      structured SearchResults (+answer synthesis)")
    lines.append("  await exa(...)       human-readable numbered list")
    lines.append("  exa.answer(...)      compact citation-grounded answer (/answer, citation_format v0.14)")
    lines.append("  exa.agent(...)       FULL agentic research run -> cited answer (+structured/cost) (/agent/runs)")
    lines.append("  exa.find_similar(URL) similar pages to a URL (/findSimilar)")
    lines.append("  exa.similar_to(URL)  alias of find_similar (more-like-this)")
    lines.append("  exa.search_to_csv(...)/search_to_jsonl(...) / to_xlsx()  exports")
    lines.append("  exa.agent_chat(...)  multi-turn agent loop (per-turn schemas v0.14)")
    lines.append("  exa.fetch(...)       full page text / highlights / summary / extras")
    lines.append("  exa.deep_research([...queries])  fan-out research, merged + de-duped")
    lines.append("  exa.news_roundup(query, days=...) fresh-news sweep over N days (ranked v0.15)")
    lines.append("  exa.company_dossier(name)         entity profile + summary + website brief (v0.15)")
    lines.append("  exa.diff_results(a, b)            URL diff of two result sets: added/removed/changed (v0.15)")
    lines.append("  exa.stream_answer(...) / exa.stream_search(...)  SSE incremental synthesis (citation_format v0.14)")
    lines.append("  exa.stream_answer_source(...)  typed per-chunk SSE deltas: char/token metering + sources (v0.16)")
    lines.append("  exa.agent_trace(run_id)        typed agent-run trace: tool calls, sources, timing (v0.16)")
    lines.append("  exa.explain(url)               one-call readable page explanation (v0.16)")
    lines.append("  exa.magic(query)               deep+answer+cited markdown, one call (v0.16)")
    lines.append("  exa.monitor_* (...)         scheduled recurring change-detection (/monitors)")
    lines.append("  exa.wmonitor_* (...)       webset monitors: keep websets fresh on cron (/v0/monitors)")
    lines.append("  exa.agent_list/agent_get/agent_delete/agent_cancel/agent_events(...)")
    lines.append("  exa.webset_* (...)          bulk structured entity discovery (WebSets)")
    lines.append("  exa.webset_recall(wsid)  read-side estimated total-match coverage")
    lines.append("  exa.webset_snapshot(wsid) / webset_snapshot_diff(a, b)   snapshots + URL diff (v0.16)")
    lines.append("  exa.import_* / exa.webhook_* / exa.event_* / exa.team_info(...)  WebSets ops")
    lines.append("  exa.github_repo(owner/repo)   client-side GitHub repo profile (v0.19)")
    lines.append("  exa.code_search / hf_models / hf_discussions   dev code & HF surfaces (v0.19)")
    lines.append("  exa.dev_help / find_used_by / dev_report       dev README + help (v0.19)")
    lines.append("  exa.modes()          this list")
    return "\n".join(lines)


# Backwards-compatible accessors from the original module.
run_search = search         # prior alias expectations
# =====================================================================
# Developer-community surfaces (v0.19) -- live-verified via api.exa.ai
# plus the raw github.com / huggingface.co developer HTTP surfaces.
# ---------------------------------------------------------------------
def _gh_owner_repo(url_or_repo):
    """Return (owner, repo) from a github.com URL or an 'owner/repo' string."""
    s = (url_or_repo or "").strip()
    if not s:
        return None
    if "/" not in s:
        return None
    m = re.match(r"^([A-Za-z0-9_.-]+)\s*/\s*([A-Za-z0-9_.-]+)$", s)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", s)
    if m:
        return m.group(1), m.group(2)
    return None


def _extract_gh_repo(url):
    """Pull 'owner/repo' out of a github.com/... URL, or the URL itself."""
    m = re.search(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", url or "")
    if m:
        return "%s/%s" % (m.group(1), m.group(2))
    return url or ""


def _gh_api_repo(owner, repo, *, timeout=30.0):
    """Raw GitHub REST repo metadata (public repos only). None on failure."""
    try:
        resp = _http().get(
            "https://api.github.com/repos/%s/%s" % (owner, repo),
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout, follow_redirects=True,
        )
    except Exception:
        return None
    if resp.status_code < 400:
        try:
            return resp.json()
        except Exception:
            return None
    return None


def github_repo(url_or_repo, *, num_results=3, with_readme=True, readme_cap=700,
                timeout=45.0):
    """Client-side composite for a **GitHub repository profile**.

    Resolves ``url_or_repo`` (a ``github.com/owner/repo`` URL or an
    ``owner/repo`` string such as ``"ollama/ollama"``). It runs a ``github``-mode
    ``search()`` through this skill to confirm the repo and capture the canonical
    URL / snippet, then pulls structured repo metadata + README head from the
    raw GitHub API / raw README (both permitted developer HTTP sources).

    Returns a clean dict: ``owner``, ``repo``, ``description``,
    ``primary_language``, ``stars``, ``forks``, ``topics``, ``license``,
    ``url``, ``default_branch``, ``homepage``, ``archived``, ``created_at``,
    ``updated_at`` and ``readme_head`` (raw markdown, first ``readme_cap``
    chars). Sub-errors surface as ``github_api_error`` / ``readme_error`` notes
    rather than raising.

    Args:
        url_or_repo: a github.com URL or an ``owner/repo`` string.
        num_results: github-mode search result cap for the confirm search.
        search_readme: fetch the raw README head (default True).
    """
    pair = _gh_owner_repo(url_or_repo)
    if pair is None:
        raise ExaError(
            "github_repo: could not parse a GitHub repository from %r. "
            "Pass a github.com/owner/repo URL or an owner/repo string." % (url_or_repo,)
        )
    owner, repo = pair
    canonical = "https://github.com/%s/%s" % (owner, repo)

    # primary path through the skill: github-mode search confirms the repo
    search_hit = None
    try:
        res = search("%s/%s" % (owner, repo), mode="github", num_results=num_results,
                     with_text=True, max_characters=300, timeout=timeout)
        for r in res.results:
            if r.url and ("github.com/%s/%s" % (owner, repo)) in r.url:
                search_hit = r
                if re.match(r"^https://github\.com/[^/]+/[^/]+/?$", r.url):
                    canonical = r.url
                break
    except Exception:
        search_hit = None

    out = {
        "owner": owner,
        "repo": repo,
        "description": "",
        "primary_language": None,
        "stars": None,
        "forks": None,
        "topics": [],
        "license": None,
        "url": canonical,
        "default_branch": "main",
        "homepage": None,
        "archived": None,
        "created_at": None,
        "updated_at": None,
        "readme_head": "",
        "search_title": search_hit.title if search_hit else None,
        "search_url": search_hit.url if search_hit else None,
    }
    meta = _gh_api_repo(owner, repo, timeout=timeout)
    if meta is None:
        out["github_api_error"] = "GitHub API returned no data for %s/%s." % (owner, repo)
    else:
        out["description"] = (meta.get("description") or "").strip()
        out["primary_language"] = meta.get("language")
        out["stars"] = meta.get("stargazers_count")
        out["forks"] = meta.get("forks_count")
        out["topics"] = list(meta.get("topics") or [])
        lic = meta.get("license")
        out["license"] = (lic or {}).get("spdx_id") if isinstance(lic, dict) else lic
        out["url"] = meta.get("html_url") or out["url"]
        out["default_branch"] = meta.get("default_branch") or out["default_branch"]
        out["homepage"] = meta.get("homepage")
        out["archived"] = meta.get("archived")
        out["created_at"] = meta.get("created_at")
        out["updated_at"] = meta.get("updated_at")
        if "/" in (meta.get("full_name") or ""):
            _own, _, _rp = (meta.get("full_name") or "").rpartition("/")
            if _own:
                out["owner"], out["repo"] = _own, _rp

    if "github_api_error" not in out:
        if search_hit and not out["description"]:
            out["description"] = (search_hit.summary or search_hit.text or "").strip()[:300]
        if with_readme:
            branch = out["default_branch"]
            readme_url = "https://raw.githubusercontent.com/%s/%s/%s/README.md" % (
                out["owner"], out["repo"], branch)
            try:
                rr = _http().get(readme_url, timeout=timeout)
                if rr.status_code < 400 and rr.text.strip():
                    out["readme_head"] = rr.text[:readme_cap]
                    out["readme_url"] = readme_url
                else:
                    out["readme_error"] = "raw README HTTP %d" % rr.status_code
            except Exception as exc:
                out["readme_error"] = str(exc)
    return out


def _fence_blocks(text):
    """Extract ```lang\ncode\n``` code blocks from a markdown/answer string."""
    if not text:
        return []
    blocks = re.findall(r"```[A-Za-z0-9_+.-]*[ \\t]*\n(.*?)\n```", text, re.S)
    if blocks:
        return [b.strip() for b in blocks]
    return re.findall(r"`([^`]+)`", text)


def code_search(query, *, language=None, num_results=6, with_text=True,
                text_cap=500, timeout=45.0):
    """Developer **code / issue / PR / commit / repo** search over GitHub-mode hits.

    Runs ``search(query, mode='github', ...)`` through this skill and surfaces
    the code files (blobs), pull requests (``#N``), commits, issues, discussion
    threads and repo roots cleanly. Optional ``language`` filters client-side by
    matching a filename extension (e.g. ``py``, ``cpp``, ``go``, ``vulkan``).

    Returns a dict: ``query``, ``language``, ``hits`` (list of per-hit dicts:
    ``kind``/``title``/``url``/``repo``/``snippet``/``state``/``author``/
    ``published_date``), grouped ``code``/``issues``/``pull_requests``/
    ``commits``/``repos`` lists, ``count`` and a readable ``markdown`` render.
    """
    res = search(
        query, mode="github", num_results=num_results,
        with_text=with_text, max_characters=text_cap,
        with_highlights=True, highlights_max_characters=min(200, text_cap),
        timeout=timeout,
    )
    kinds = {"code": [], "issues": [], "pull_requests": [],
             "commits": [], "repos": [], "other": []}
    hits = []
    for r in res.results:
        u = r.url or ""
        low = u.lower()
        kind = "other"
        if ("/blob/" in low or low.endswith(".py") or low.endswith(".cpp")
                or low.endswith(".h") or low.endswith(".rs") or low.endswith(".go")
                or low.endswith(".js") or low.endswith(".ts") or low.endswith(".md")):
            kind = "code"
        elif "/pull/" in low or "/pulls/" in low or "pull request" in (r.title or "").lower():
            kind = "pull_requests"
        elif "/commit/" in low:
            kind = "commits"
        elif "/issues/" in low or (r.title or "").startswith("Is "):
            kind = "issues"
        elif re.match(r"^https://github\\.com/[^/]+/[^/]+/?$", u):
            kind = "repos"

        if language:
            lang = language.lower()
            fn = u.split("/")[-1]
            if not (fn.lower().endswith("." + lang) or fn.lower().endswith("." + lang)):
                continue

        kinds.setdefault(kind, []).append(r)
        snippet = (r.snippet or (r.text or ""))[:text_cap]
        hits.append({
            "id": r.id,
            "kind": kind,
            "title": r.title,
            "url": u,
            "repo": _extract_gh_repo(u),
            "snippet": snippet,
            "state": r.extras.get("state") if isinstance(r.extras, dict) else None,
            "author": r.author,
            "published_date": r.published_date,
        })

    lines = ["## Code search: " + query, ""]
    for i, h in enumerate(hits, 1):
        lines.append("%d. **%s** (%s) %s" % (i, h["title"], h["kind"], h["url"]))
        if h.get("snippet"):
            lines.append("   " + h["snippet"].replace("\n", " ")[:140])
    return {
        "query": query,
        "language": language,
        "hits": hits,
        "code": [h.raw for h in kinds["code"]],
        "issues": [h.raw for h in kinds["issues"]],
        "pull_requests": [h.raw for h in kinds["pull_requests"]],
        "commits": [h.raw for h in kinds["commits"]],
        "repos": [h.raw for h in kinds["repos"]],
        "count": len(hits),
        "markdown": "\n".join(lines),
    }

def hf_models(query, *, num_results=5, with_card=True, card_cap=900,
             search_type="keyword", timeout=45.0):
    """HuggingFace **model** search returning annotated model profiles.

    1. ``search(query, include_domains=['huggingface.co'], search_type=...)``
       through this skill to surface the top model pages.
    2. For the top hit, GET the raw HF model JSON (``huggingface.co/api/models/{id}``)
       for structured metadata (downloads, likes, task, license, tags) and fetch
       the raw model-card README (``/raw/main/README.md``) for ``card_markdown``.

    Returns a dict: ``query``, ``count``, ``models`` (list), ``top`` (the first,
    fully-annotated model) and ``markdown``. Each model dict carries ``model_id``,
    ``author``, ``task``, ``library``, ``license``, ``downloads``, ``likes``,
    ``params``, ``base_model``, ``description``, ``tags``, ``url``,
    ``last_modified`` and ``card_markdown`` (when ``with_card``).
    """
    res = search(query, include_domains=["huggingface.co"],
                 num_results=num_results, with_summary=True,
                 summary_query="What model is this and what is it for?",
                 timeout=timeout)
    models = []
    for r in res.results:
        mid = (r.url or "").replace("https://huggingface.co/", "").strip("/")
        if not mid or "/" not in mid:
            continue
        models.append({"url": r.url, "model_id": mid, "summary": r.summary,
                       "snippet": r.snippet, "title": r.title})
    if not models:
        raise ExaError("hf_models: no huggingface.co model found for %r" % (query,))

    top_with_card = None
    if with_card:
        top_with_card = _hf_model_card(models[0]["model_id"], timeout=timeout,
                                       card_cap=card_cap,
                                       summary=models[0].get("summary"))
        if top_with_card:
            models[0].update(top_with_card)

    lines = ["## HF models: " + query, ""]
    for m in models:
        desc = m.get("description") or m.get("summary") or ""
        lines.append("- %s -- %s" % (m["model_id"], desc[:90]))
    lines.append("")
    if top_with_card:
        lines.append("### Top card: " + top_with_card.get("model_id", ""))
        lines.append("  task=%s  license=%s  downloads=%s  likes=%s  params=%s" % (
            top_with_card.get("task"), top_with_card.get("license"),
            top_with_card.get("downloads"), top_with_card.get("likes"),
            top_with_card.get("params")))

    return {"query": query, "count": len(models), "models": models,
            "top": top_with_card if top_with_card else models[0],
            "markdown": "\\n".join(lines)}


def _hf_model_card(model_id, *, card_cap=900, timeout=30.0, summary=None):
    """Raw HF API model metadata + model-card README for one ``model_id``."""
    try:
        resp = _http().get("https://huggingface.co/api/models/%s" % model_id,
                         timeout=timeout)
        if resp.status_code >= 400:
            return None
        meta = resp.json()
    except Exception:
        return None
    card_data = meta.get("cardData") or {}
    if not isinstance(card_data, dict):
        card_data = {}
    tags = list(meta.get("tags") or [])
    params = None
    saf = meta.get("safetensors") or {}
    if isinstance(saf, dict):
        params = saf.get("total") or (saf.get("parameters") or {}).get("total")
    lic = card_data.get("license") or meta.get("license")
    if not lic:
        for t in tags:
            if t.startswith("license:"):
                lic = t.split(":", 1)[1]
                break
    task = meta.get("pipeline_tag") or card_data.get("pipeline_tag")
    base_model = meta.get("baseModel")
    if isinstance(base_model, dict):
        base_model = base_model.get("modelId")
    desc = (card_data.get("model_name") or card_data.get("description")
            or summary or "")
    out = {
        "model_id": model_id,
        "author": meta.get("author") or model_id.split("/", 1)[0],
        "task": task,
        "library": meta.get("library_name"),
        "license": lic,
        "downloads": meta.get("downloads"),
        "likes": meta.get("likes"),
        "params": params,
        "base_model": base_model,
        "description": desc,
        "tags": [t for t in tags if not t.startswith("license:")],
        "card_markdown": "",
        "url": "https://huggingface.co/%s" % model_id,
        "last_modified": meta.get("lastModified"),
        "gated": meta.get("gated"),
    }
    if card_cap:
        try:
            rr = _http().get("https://huggingface.co/%s/raw/main/README.md" % model_id,
                          timeout=timeout)
            if rr.status_code < 400 and rr.text.strip():
                out["card_markdown"] = rr.text[:card_cap]
        except Exception:
            pass
    return out


def find_used_by(package, *, num_results=8, exclude_owner=True, timeout=45.0):
    """Find repos / notebooks / py pages that **import** a Python package.

    Runs a ``github``-mode ``search('import <package>')`` plus a python-file
    web pass, dedupes by URL and returns the caller code surfaces.
    ``exclude_owner=True`` (default) drops the package author's own repo so you
    see *users* of the package rather than the package itself.

    Returns ``{"package", "count", "used_by", "markdown"}`` where each entry is
    ``{url, repo, kind, title, snippet}`` and ``kind`` is one of ``repo`` /
    ``code`` / ``python`` / ``notebook`` / ``issue`` / ``page``.
    """
    used, seen = [], set()

    def _push(r):
        u = r.url or ""
        if not u or u in seen:
            return
        seen.add(u)
        low = u.lower()
        if ".ipynb" in low:
            kind = "notebook"
        elif ".pyt" in low or ".py" in low:
            kind = "python"
        elif "/blob/" in low:
            kind = "code"
        elif "/discussions/" in low or "/issues/" in low:
            kind = "issue"
        elif re.match(r"^https://github\\.com/[^/]+/[^/]+/?$", u):
            kind = "repo"
        else:
            kind = "page"
        used.append({
            "url": u,
            "repo": _extract_gh_repo(u),
            "kind": kind,
            "title": r.title,
            "snippet": (r.snippet or (r.text or ""))[:200],
        })

    queries = [
        "import " + package,
        '\"' + package + '\" python import',
        "\"" + package + "\" " + package + " import notebook",
    ]
    for q in queries:
        try:
            res2 = search(q, mode="github", num_results=num_results,
                          with_text=True, max_characters=240,
                          with_highlights=True, highlights_max_characters=150,
                          timeout=timeout)
            for r in res2.results:
                _push(r)
        except Exception:
            pass
    try:
        web = search('"' + package + '" python file', num_results=num_results,
                     with_highlights=True, highlights_max_characters=150,
                     timeout=timeout)
        for r in web.results:
            if r.url and (".py" in r.url.lower() or ".ipynb" in r.url.lower()
                          or "/notebooks/" in r.url.lower()):
                _push(r)
    except Exception:
        pass

    if exclude_owner:
        pkg_l = package.lower()
        owner_guess = package.split("-")[0].lower()
        kept = []
        for it in used:
            rpath = it["repo"].split("/", 1)
            rname = rpath[-1].lower() if len(rpath) > 1 else it["repo"].lower()
            rowner = rpath[0].lower() if len(rpath) > 1 else ""
            # drop the package's own canonical repo (repo basename == package,
            # or the guessed github owner) so you get *users* of the package.
            if rname == pkg_l or rowner == owner_guess:
                continue
            kept.append(it)
        used = kept
    # prefer real code surfaces over README/docs/pages
    order = {"repo": 0, "python": 1, "notebook": 2, "code": 3, "issue": 4, "page": 5}
    used.sort(key=lambda it: order.get(it["kind"], 9))

    lines = ["## Used by: " + package, ""]
    for it in used[:num_results]:
        lines.append("- **%s** [%s] -- %s" % (it["kind"], it["repo"], it["url"]))
        if it.get("snippet"):
            lines.append("  " + it["snippet"].replace("\n", " ")[:120])
    return {"package": package, "count": len(used), "used_by": used,
            "markdown": "\n".join(lines)}


def dev_report(query, *, num_results=5, days=5, repo=None, with_news=True,
               timeout=60.0, return_meta=False):
    """**One-call dev-light README**: web + github repo + cited answer + fresh news.

    Composites several live-verified surfaces into a single markdown brief:
      1. ``search(query, with_summary/with_highlights/with_text)`` for top web
         results.
      2. ``github_repo(repo)`` (when ``repo`` is given, or when the top web hit
         is a github repo) for the dev-profile block.
      3. ``answer(query)`` for the citation-grounded summary.
      4. ``news_roundup(query, days=days)`` for fresh-dev signal (opt-out via
         ``with_news=False``).

    Returns the full markdown ``str`` by default; ``return_meta=True`` wraps it
    as ``{"query", "summary", "results", "github", "citations", "news",
    "markdown"}`` so callers can inspect the pieces.
    """
    # 1) web search
    web = search(query, num_results=num_results, with_summary=True,
                 summary_query="Developer context for: " + query,
                 with_highlights=True, highlights_max_characters=200,
                 timeout=timeout)
    # 2) github repo block
    gh = None
    gh_hint = repo
    if not gh_hint:
        for r in web.results[:3]:
            m = _gh_owner_repo(r.url or "")
            if m:
                gh_hint = "/".join(m)
                break
    if gh_hint:
        try:
            gh = github_repo(gh_hint, num_results=3, with_readme=True,
                             readme_cap=400, timeout=timeout)
        except Exception:
            gh = None
    # 3) answer with citations
    try:
        ans = answer(query, timeout=timeout)
    except Exception:
        ans = None
    # 4) fresh-dev news roundup (opt-in via with_news=False)
    news = None
    if with_news:
        try:
            news = news_roundup(query, days=days, num_results=min(3, num_results),
                                timeout=timeout)
        except Exception:
            news = None

    md = ["# Dev report: " + query, ""]
    if ans is not None and str(ans.answer):
        md += ["### Summary", "", str(ans.answer), ""]
        if getattr(ans, "citations", None):
            md += ["### Cited sources", ""]
            for i, c in enumerate(ans.citations, 1):
                md.append("%d. %s -- %s" % (i, c.get("title") or "(untitled)",
                                            c.get("url") or ""))
            md += [""]
    if gh:
        md += ["### GitHub: %s/%s" % (gh["owner"], gh["repo"]), ""]
        if gh.get("description"):
            md += ["_%s_" % gh["description"], ""]
        md.append("primary=%s  stars=%s  forks=%s" % (
            gh.get("primary_language"), gh.get("stars"), gh.get("forks")))
        if gh.get("topics"):
            md.append("topics: " + ", ".join(gh["topics"][:12]))
        md += [""]
    md += ["### Top results", ""]
    for i, r in enumerate(web, 1):
        md.append("%d. **%s** -- %s" % (i, r.title, r.url))
        if r.summary:
            md.append("   " + r.summary.replace("\n", " ")[:120])
    md += [""]
    if news is not None and len(news) > 0:
        md += ["### Fresh dev news (last %d days)" % days, ""]
        for i, r in enumerate(list(news)[:4], 1):
            md.append("%d. **%s** -- %s" % (i, r.title, r.url))
        md += [""]

    markdown = "\n".join(md)
    if return_meta:
        return {
            "query": query,
            "summary": ans.answer if ans is not None else None,
            "results": web,
            "github": gh,
            "citations": ans.citations if ans is not None else [],
            "news": news,
            "markdown": markdown,
        }
    return markdown

def dev_help(query, *, citation_format=None, timeout=90.0):
    """**Developer help**: citation-grounded answer over the code corpus.

    Wraps ``answer()`` (which already draws on StackOverflow / GitHub issues /
    docs) and adds code-snippet extraction + per-citation steps. Returns a dict:
    ``query``, ``answer``, ``steps`` (enumerated how-to lines if the answer
    looks listy), ``snippets`` (code fences pulled out of the answer),
    ``sources`` (with title/url excerpt), ``markdown`` and ``request_id``.
    """
    ans = answer(query, citation_format=citation_format)
    body = str(ans.answer) if ans.answer is not None else ""
    steps = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)[\.:]\s*(.*)", line)
        if m:
            steps.append((int(m.group(1)), m.group(2)))
    snippets = _fence_blocks(body)
    srcs = []
    for c in ans.citations:
        srcs.append({
            "title": c.get("title") or "(untitled)",
            "url": c.get("url") or "",
            "author": c.get("author"),
            "published_date": c.get("publishedDate"),
            "favicon": c.get("favicon"),
        })

    lines = ["## Dev help: " + query, "", body]
    if snippets:
        lines += ["", "### Code snippets", ""] + snippets
    if srcs:
        lines += ["", "### Sources", ""]
        for i, s in enumerate(srcs, 1):
            lines.append("%d. %s -- %s" % (i, s["title"], s["url"]))
    return {
        "query": query,
        "answer": body,
        "steps": steps,
        "snippets": snippets,
        "sources": srcs,
        "markdown": "\n".join(lines),
        "request_id": ans.request_id,
    }
def hf_discussions(query, *, num_results=6, timeout=45.0):
    """HuggingFace community **discussion threads** mentions.

    Runs ``search(query, include_domains=['huggingface.co'])`` and a
    ``discuss.huggingface.co``-scoped search so both the HF model
    ``/discussions/N`` threads and the HF forum topics surface. Returns a dict:
    ``query``, ``count``, ``threads`` (list of ``{title, url, source,
    community, excerpt}``) and ``markdown``. Community is one of
    ``model-community`` (``/discussions/``), ``huggingface-forum``
    (``discuss.huggingface.co``) or ``huggingface``.
    """
    threads, seen = [], set()

    def _push(r):
        u = r.url or ""
        if not u or u in seen:
            return
        seen.add(u)
        if "/discuss.huggingface.co" in u:
            comm = "huggingface-forum"
        elif "/discussions/" in u:
            comm = "model-community"
        else:
            comm = "huggingface"
        threads.append({
            "title": r.title,
            "url": u,
            "source": u,
            "community": comm,
            "excerpt": (r.snippet or r.summary or r.text or "")[:220],
        })

    for inc in (["huggingface.co"], ["discuss.huggingface.co"]):
        try:
            res = search(query, include_domains=inc, num_results=num_results,
                         with_highlights=True, highlights_max_characters=200,
                         timeout=timeout)
            for r in res.results:
                _push(r)
        except Exception:
            pass

    lines = ["## HF discussions: " + query, ""]
    for t in threads[:num_results]:
        append_line = "- **%s** [%s] (%s)" % (t["title"], t["community"], t["url"])
        lines.append(append_line)
        if t.get("excerpt"):
            lines.append("  " + t["excerpt"].replace("\n", " "))
    return {"query": query, "count": len(threads), "threads": threads,
            "markdown": "\n".join(lines)}



# ---------------------------------------------------------------------------
# Round-18: developer workflows — `is_deprecated`, `deprecations`,
# `pkg_releases`, `snippet`, `api_reference`, `code_lint_tips`.
# Client-composed over the verified `search` / `answer` / `fetch` /
# `news_roundup` / `dev_help` primitives (no new API surface). Live-verified
# against api.exa.ai. New public entry points; no prior signature touched.
# ---------------------------------------------------------------------------


def pkg_releases(pkg, *, num_results=8, with_news=True, timeout=45.0):
    """Release / version-signal digest for a package (PyPI/npm/crates/GitHub).

    Scans GitHub-mode + registry results for visible version bumps (release
    tags, changelog pages) plus an optional freshness news sweep. This is a
    search/surface digest, not an authoritative registry index (honest).

    Args:
        pkg: package name (e.g. ``"httpx"``, ``"fastapi"``).
        with_news: also run a 7-day news sweep for the package (default True).
        num_news / timeout: forwarded to the underlying calls.

    Returns:
        {"pkg", "releases":[{title,url,source,published}], "news":[{title,url,
         domain}], "n_releases", "n_news", "markdown"}.
    """
    q = (pkg + " release notes")
    res = search(q, mode="github", num_results=num_results if num_results else 8,
                 with_text=True, max_characters=260, timeout=timeout)
    releases = []
    for r in (res.results or []):
        low = (r.url or "").lower()
        if "/releases/" in low or "/tags/" in low or "changelog" in low \
                or "release" in (r.title or "").lower():
            releases.append({"title": r.title, "url": r.url,
                             "source": (r.domain or ""), "published": r.published_date})
    news = []
    if with_news:
        try:
            nr = news_roundup(pkg, days=7, num_results=5, timeout=min(timeout, 40.0))
            for r in (nr.results or [])[:5]:
                news.append({"title": getattr(r, "title", None),
                             "url": getattr(r, "url", None),
                             "domain": getattr(r, "domain", None)})
        except Exception:
            news = []
    lines = ["## %s releases" % pkg, ""]
    for rv in releases[:num_results]:
        lines.append("- %s  %s" % (rv["title"], rv["url"]))
    if news:
        lines.append("\n### Recent news")
        for r in news:
            lines.append("- %s  [%s]" % (r["title"], r["domain"]))
    return {"pkg": pkg, "releases": releases, "news": news,
            "n_releases": len(releases), "n_news": len(news),
            "markdown": "\n".join(lines)}


def _dep_type(blob):
    bb = (blob or "").lower()
    if "deprecat" in bb or "obsolete" in bb or "end of life" in bb:
        return "deprecat"
    if "migrat" in bb:
        return "migrat"
    if "remov" in bb or "sunset" in bb:
        return "removal"
    if "breaking" in bb or "major change" in bb:
        return "breaking"
    if "release" in bb or "changelog" in bb or "what's new" in bb:
        return "release"
    return "other"


def deprecations(query: str, *, days=30, timeout=90.0):
    """Deprecation / end-of-life signals for a library or API.

    Composes `answer` over a deprecation corpus plus a freshness-filtered news
    sweep, labeling each signal (deprecat / migrat / removal / breaking /
    release). Honest: only items that actually surface the vocabulary.

    Args:
        query: library / API (e.g. ``"Node.js"``, ``"pydantic v1"``).
        days: news freshness window (default 30).
        timeout: forwarded to calls.

    Returns:
        {"query", "signals":[{url,title,type,snippet}], "counts", "n",
         "markdown"}.
    """
    import re as _re
    from datetime import datetime, timedelta
    sig = []
    ans_text = None
    try:
        ans = answer(query + " deprecated",
                     text=True, citation_format={"id": True, "title": True},
                     timeout=timeout)
        ans_text = getattr(ans, "answer", None) or ""
        for c in (getattr(ans, "citations", None) or []):
            title = (c.get("title") or "")[:140]
            blob = title + " " + str(c.get("url", ""))
            if _re.search(r"\b(deprecat|obsolete|end[- ]of[- ]life|remov|migrat|breaking)\b", blob, _re.I):
                sig.append({"url": c.get("url"), "title": title, "type": _dep_type(blob)})
    except Exception:
        pass
    if ans_text and _re.search(r"\b(deprecat|obsolete|removed|migrat|breaking)\b", ans_text, _re.I):
        # the model answer itself flags deprecation - record as a signal.
        sig.append({"url": None, "title": ans_text[:140], "type": "deprecat"})
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=max(1, days))
        sr = search(query + " deprecated", mode="news",
                    num_results=10, with_summary=True,
                    start_published_date=start.strftime("%Y-%m-%d"),
                    end_published_date=end.strftime("%Y-%m-%d"),
                    timeout=min(timeout, 40.0))
        for r in (sr.results or []):
            blob = (r.title or "") + " " + (r.summary or "")
            if _re.search(r"\b(deprecat|obsolete|removed|migrat|breaking|sunset)\b", blob, _re.I):
                sig.append({"url": r.url, "title": r.title, "type": _dep_type(blob)})
    except Exception:
        pass
    counts = {}
    for s in sig:
        counts[s["type"]] = counts.get(s["type"], 0) + 1
    out = ["## Deprecation signals for %s" % query, ""]
    for s in sig[:12]:
        out.append("- [%s] %s  %s" % (s["type"], (s.get("title") or "")[:90], s.get("url")))
    return {"query": query, "signals": sig, "counts": counts, "n": len(sig),
            "markdown": "\n".join(out)}


def snippet(query: str, *, lang=None, num_results=6, timeout=45.0):
    """Pull small code snippets relevant to a developer question.

    Uses GitHub-mode `search` and surfaces candidate snippet rows — repo,
    filename, url, a short extract. ``lang`` optionally filters by filename
    extension (``py``, ``js``, ``ts``, ``cpp``, ``go``, ``rs``, ``sh``...).
    Honest: empty list when no code matches.

    Args:
        query: what to find a code sample for.
        lang: optional filename extension to prefer (default all).
        num_results: github rows to scan (default 6).
        timeout: forwarded to search.

    Returns:
        {"query", "lang", "snippets":[{repo,file,ext,url,extract}], "n",
         "markdown"}.
    """
    import os as _os
    res = search(query, mode="github", num_results=num_results,
                 with_text=True, max_characters=500, timeout=timeout)
    exts = {".py", ".js", ".ts", ".tsx", ".cpp", ".c", ".h", ".rs", ".go",
            ".java", ".rb", ".cs", ".sh", ".md", ".mjs", ".cjs"}
    snippets = []
    for r in (res.results or []):
        u = r.url or ""
        fname = u.rstrip("/").split("/")[-1] if u else ""
        ext = _os.path.splitext(fname)[1].lower()
        if not ext or ext not in exts:
            continue
        if lang and lang != "*" and ext != (lang if lang.startswith(".") else "." + lang):
            continue
        repo = "/".join(u.split("/")[3:5]) if u.startswith("https://github.com/") else u
        snippets.append({"repo": repo, "file": fname, "ext": ext.lstrip("."),
                         "url": u, "extract": (r.text or r.snippet or "")[:200]})
    lines = ["## Snippets for " + query, ""]
    for s in snippets[:12]:
        lines.append("- `%s`  %s" % (s["file"], s["url"]))
        if s["extract"]:
            lines.append("  >>> " + s["extract"].replace("\n", " "))
    return {"query": query, "lang": lang, "snippets": snippets,
            "n": len(snippets), "markdown": "\n".join(lines)}


def api_reference(url, *, max_characters=9000, with_summary=True, timeout=45.0):
    """Fetch a docs / API page into a structured, navigable reference.

    Uses the verified `fetch` with summary + highlights; returns the page
    title, url, the readable text (capped), any code-fence count, and the
    summary. A downstream model can index/query these sections.

    Args:
        url: documentation / API page.
        max_characters: max text to keep (default 16000).
        with_summary: include the model summary (default True).
        timeout: forwarded to fetch.

    Returns:
        {"url", "title", "text": str (capped), "characters": int,
         "markdown": str}.
    """
    doc = fetch(url, mode="text", max_characters=max_characters,
                with_summary=with_summary, with_highlights=True,
                highlights_max_characters=200, timeout=timeout)
    title = None
    text = ""
    summary = None
    if isinstance(doc, list):
        # mode text + summary/highlights returns a list of one result dict.
        doc = doc[0] if doc else {}
    if isinstance(doc, str):
        text = doc  # plain-text return (no structured extras)
    elif isinstance(doc, dict):
        title = doc.get("title")
        text = doc.get("text") or ""
        summary = doc.get("summary")
        if not text:
            text = (doc.get("data") or {}).get("text", "")
        if not title:
            title = (doc.get("data") or {}).get("title")
    else:
        title = getattr(doc, "title", None)
        text = getattr(doc, "text", "")
        summary = getattr(doc, "summary", None)
    text = text or ""
    head = "# %s\n%s" % (title or url, text[:600])
    return {"url": url, "title": title, "text": text,
            "characters": len(text), "summary": summary,
            "markdown": head}


def code_lint_tips(query: str, *, timeout=90.0):
    """Dev foot-gun / lint-warning primer for a framework — no model call.

    Delegates to `dev_help` (answer over a code corpus with snippets +
    sources) and returns the answer text, the numbered snippets, and source
    list — a quick primer a dev agent can act on.

    Args:
        query: framework topic or foot-gun (e.g. "pandas chained assignment").
        timeout: forwarded to dev_help.

    Returns:
        {"query", "answer": str, "snippets": [...], "sources": [...],
         "markdown": str}.
    """
    dh = dev_help(query, timeout=timeout)
    return {"query": query,
            "answer": dh.get("answer", ""),
            "snippets": dh.get("snippets") or [],
            "sources": dh.get("sources") or [],
            "markdown": dh.get("markdown") or dh.get("answer", "")}



# ---------------------------------------------------------------------------
# Round-18: developer workflows — `is_deprecated`, `deprecations`,
# `pkg_releases`, `snippet`, `api_reference`, `code_lint_tips`.
# Client-composed over the verified `search` / `answer` / `fetch` /
# `news_roundup` / `dev_help` primitives (no new API surface). Live-verified
# against api.exa.ai. New public entry points; no prior signature touched.
# ---------------------------------------------------------------------------


def pkg_releases(pkg, *, num_results=8, with_news=True, timeout=45.0):
    """Release / version-signal digest for a package (PyPI/npm/crates/GitHub).

    Scans GitHub-mode + registry results for visible version bumps (release
    tags, changelog pages) plus an optional freshness news sweep. This is a
    search/surface digest, not an authoritative registry index (honest).

    Args:
        pkg: package name (e.g. ``"httpx"``, ``"fastapi"``).
        with_news: also run a 7-day news sweep for the package (default True).
        num_news / timeout: forwarded to the underlying calls.

    Returns:
        {"pkg", "releases":[{title,url,source,published}], "news":[{title,url,
         domain}], "n_releases", "n_news", "markdown"}.
    """
    q = (pkg + " release notes")
    res = search(q, mode="github", num_results=num_results if num_results else 8,
                 with_text=True, max_characters=260, timeout=timeout)
    releases = []
    for r in (res.results or []):
        low = (r.url or "").lower()
        if "/releases/" in low or "/tags/" in low or "changelog" in low \
                or "release" in (r.title or "").lower():
            releases.append({"title": r.title, "url": r.url,
                             "source": (r.domain or ""), "published": r.published_date})
    news = []
    if with_news:
        try:
            nr = news_roundup(pkg, days=7, num_results=5, timeout=min(timeout, 40.0))
            for r in (nr.results or [])[:5]:
                news.append({"title": getattr(r, "title", None),
                             "url": getattr(r, "url", None),
                             "domain": getattr(r, "domain", None)})
        except Exception:
            news = []
    lines = ["## %s releases" % pkg, ""]
    for rv in releases[:num_results]:
        lines.append("- %s  %s" % (rv["title"], rv["url"]))
    if news:
        lines.append("\n### Recent news")
        for r in news:
            lines.append("- %s  [%s]" % (r["title"], r["domain"]))
    return {"pkg": pkg, "releases": releases, "news": news,
            "n_releases": len(releases), "n_news": len(news),
            "markdown": "\n".join(lines)}


def _dep_type(blob):
    bb = (blob or "").lower()
    if "deprecat" in bb or "obsolete" in bb or "end of life" in bb:
        return "deprecat"
    if "migrat" in bb:
        return "migrat"
    if "remov" in bb or "sunset" in bb:
        return "removal"
    if "breaking" in bb or "major change" in bb:
        return "breaking"
    if "release" in bb or "changelog" in bb or "what's new" in bb:
        return "release"
    return "other"


def deprecations(query: str, *, days=30, timeout=90.0):
    """Deprecation / end-of-life signals for a library or API.

    Composes `answer` over a deprecation corpus plus a freshness-filtered news
    sweep, labeling each signal (deprecat / migrat / removal / breaking /
    release). Honest: only items that actually surface the vocabulary.

    Args:
        query: library / API (e.g. ``"Node.js"``, ``"pydantic v1"``).
        days: news freshness window (default 30).
        timeout: forwarded to calls.

    Returns:
        {"query", "signals":[{url,title,type,snippet}], "counts", "n",
         "markdown"}.
    """
    import re as _re
    from datetime import datetime, timedelta
    sig = []
    ans_text = None
    try:
        ans = answer(query + " deprecated",
                     text=True, citation_format={"id": True, "title": True},
                     timeout=timeout)
        ans_text = getattr(ans, "answer", None) or ""
        for c in (getattr(ans, "citations", None) or []):
            title = (c.get("title") or "")[:140]
            blob = title + " " + str(c.get("url", ""))
            if _re.search(r"\b(deprecat|obsolete|end[- ]of[- ]life|remov|migrat|breaking)\b", blob, _re.I):
                sig.append({"url": c.get("url"), "title": title, "type": _dep_type(blob)})
    except Exception:
        pass
    if ans_text and _re.search(r"\b(deprecat|obsolete|removed|migrat|breaking)\b", ans_text, _re.I):
        sig.append({"url": None, "title": ans_text[:140], "type": "deprecat"})
    try:
        end = datetime.utcnow()
        st = end - timedelta(days=max(1, days))
        sr = search(query + " deprecated", mode="news",
                    num_results=10, with_summary=True,
                    start_published_date=st.strftime("%Y-%m-%d"),
                    end_published_date=end.strftime("%Y-%m-%d"),
                    timeout=min(timeout, 40.0))
        for r in (sr.results or []):
            blob = (r.title or "") + " " + (r.summary or "")
            if _re.search(r"\b(deprecat|obsolete|removed|migrat|breaking|sunset)\b", blob, _re.I):
                sig.append({"url": r.url, "title": r.title, "type": _dep_type(blob)})
    except Exception:
        pass
    counts = {}
    for s in sig:
        counts[s["type"]] = counts.get(s["type"], 0) + 1
    out = ["## Deprecation signals for %s" % query, ""]
    for s in sig[:12]:
        out.append("- [%s] %s  %s" % (s["type"], (s.get("title") or "")[:90], s.get("url")))
    return {"query": query, "signals": sig, "counts": counts, "n": len(sig),
            "markdown": "\n".join(out)}


def snippet(query: str, *, lang=None, num_results=6, timeout=45.0):
    """Pull small code snippets relevant to a developer question.

    Uses GitHub-mode `search` and surfaces candidate snippet rows — repo,
    filename, url, a short extract. ``lang`` optionally filters by filename
    extension (``py``, ``js``, ``ts``, ``cpp``, ``go``, ``rs``, ``sh``...).
    Honest: empty list when no code matches.

    Args:
        query: what to find a code sample for.
        lang: optional filename extension to prefer (default all).
        num_results: github rows to scan (default 6).
        timeout: forwarded to search.

    Returns:
        {"query", "lang", "snippets":[{repo,file,ext,url,extract}], "n",
         "markdown"}.
    """
    import os as _os
    res = search(query, mode="github", num_results=num_results,
                 with_text=True, max_characters=500, timeout=timeout)
    exts = {".py", ".js", ".ts", ".tsx", ".cpp", ".c", ".h", ".rs", ".go",
            ".java", ".rb", ".cs", ".sh", ".md", ".mjs", ".cjs"}
    snippets = []
    for r in (res.results or []):
        u = r.url or ""
        fname = u.rstrip("/").split("/")[-1] if u else ""
        ext = _os.path.splitext(fname)[1].lower()
        if not ext or ext not in exts:
            continue
        if lang and lang != "*" and ext != (lang if lang.startswith(".") else "." + lang):
            continue
        repo = "/".join(u.split("/")[3:5]) if u.startswith("https://github.com/") else u
        snippets.append({"repo": repo, "file": fname, "ext": ext.lstrip("."),
                         "url": u, "extract": (r.text or r.snippet or "")[:200]})
    lines = ["## Snippets for " + query, ""]
    for s in snippets[:12]:
        lines.append("- `%s`  %s" % (s["file"], s["url"]))
        if s["extract"]:
            lines.append("  >>> " + s["extract"].replace("\n", " "))
    return {"query": query, "lang": lang, "snippets": snippets,
            "n": len(snippets), "markdown": "\n".join(lines)}


def code_lint_tips(query: str, *, timeout=90.0):
    """Dev foot-gun / lint-warning primer for a framework — no model call.

    Delegates to `dev_help` (answer over a code corpus with snippets +
    sources) and returns the answer text, the numbered snippets, and source
    list — a quick primer a dev agent can act on.

    Args:
        query: framework topic or foot-gun (e.g. "pandas chained assignment").
        timeout: forwarded to dev_help.

    Returns:
        {"query", "answer": str, "snippets": [...], "sources": [...],
         "markdown": str}.
    """
    dh = dev_help(query, timeout=timeout)
    return {"query": query,
            "answer": dh.get("answer", ""),
            "snippets": dh.get("snippets") or [],
            "sources": dh.get("sources") or [],
            "markdown": dh.get("markdown") or dh.get("answer", "")}



def is_deprecated(pkg, *, days=180, timeout=90.0):
    """Quick staleness / deprecation verdict for a package.

    Composes a deprecation signal sweep + a freshness activity check and returns
    a bool verdict plus evidence, so a dev agent can judge whether to adopt (or
    already rely on) a library. Honest: it is a search-surface signal, not a
    full audit; absence of signals means 'no deprecation vocabulary surfaced'.

    Args:
        pkg: package name (e.g. ``"requests"``, ``"uvicorn"``).
        days: deprecation-signal window (default 180).
        timeout: forwarded to the underlying calls.

    Returns:
        {"pkg", "deprecated": bool, "reasons": [...], "active_recent": bool,
         "markdown": str}.
    """
    verdict = False
    reasons = []
    try:
        d = deprecations(pkg, days=days, timeout=timeout)
        signals = d.get("signals") or []
        if signals:
            verdict = True
            reasons.append("deprecation signal: " + (signals[0].get("title") or "")[:110])
    except Exception:
        signals = []
    active = False
    try:
        nr = news_roundup(pkg, days=7, num_results=4, timeout=min(timeout, 40.0))
        rows = nr.results or []
        active = len(rows) > 0
    except Exception:
        active = False
    lines = ["## %s — deprecated: %s" % (pkg, verdict)]
    for r in reasons[:6]:
        lines.append("- " + r)
    lines.append("- recent news activity: %s" % active)
    return {"pkg": pkg, "deprecated": verdict, "reasons": reasons,
            "active_recent": active, "markdown": "\n".join(lines)}


__all__ = [
    "ExaError", "ExaAuthError", "ExaRateLimitError", "ExaBadRequestError",
    "ExaPlanError", "ExaNotFoundError", "ExaServerError",
    "Result", "SearchResults", "Answer", "AgentRun",
    "entity_summary", "entity_type", "entities",
    "entity_search", "entity_schema", "top_terms",
    "search", "run", "brief", "fetch", "answer", "agent", "agent_structured",
    "find_similar", "similar_to", "agent_chat", "search_to_csv",
    "search_to_jsonl", "deep_research", "merge_searches", "modes",
    "news_roundup", "company_dossier", "diff_results",
    "AgentTrace", "agent_trace",
    "stream_answer", "stream_search", "stream_answer_source",
    "webset_snapshot", "webset_snapshot_diff",
    "explain", "magic",
    "monitor_create", "monitor_list", "monitor_get", "monitor_update",
    "monitor_delete", "monitor_trigger", "monitor_runs", "monitor_run_get",
    "monitor_wait", "monitor_batch", "monitor_check",
    "wmonitor_create", "wmonitor_list", "wmonitor_get", "wmonitor_update",
    "wmonitor_delete", "wmonitor_runs", "wmonitor_run_get",
    "agent_list", "agent_get", "agent_delete", "agent_cancel", "agent_events",
    "team_info",
    "webset_preview", "webset_create", "webset_list", "webset_get",
    "webset_delete", "webset_cancel", "webset_update", "webset_items",
    "webset_item_get", "webset_item_delete",
    "webset_eval_review", "webset_add_search",
    "webset_search_status", "webset_recall", "webset_search_cancel", "webset_wait",
    "webset_item_type", "webset_item_name", "webset_item_summary",
    "webset_items_all",
    "webset_enrich", "enrichment_get", "enrichment_delete", "enrichment_cancel",
    "enrichment_update",
    "import_create", "imports_list", "import_get", "import_update", "import_delete",
    "webhook_create", "webhook_list", "webhook_get", "webhook_update",
    "webhook_delete", "webhook_attempts", "event_list", "event_get",
    # developer-community surfaces (v0.19)
    "github_repo", "code_search", "hf_models", "hf_discussions",
    "dev_help", "find_used_by", "dev_report",
    "pkg_releases", "deprecations", "snippet", "api_reference",
    "is_deprecated", "code_lint_tips",
    "VALID_CATEGORIES", "SEARCH_TYPES", "MODES", "DEFAULT_API_URL", "WEBSETS_API_URL",
]


# ---------------------------------------------------------------------------
# Make every public function work with or without ``await`` (dual sync/async).
# ---------------------------------------------------------------------------
# Attach __await__ to the module's own result classes so `await` unwraps to the
# same instance (isinstance / attribute access are unaffected), then wrap every
# public function so its return value works both with and without ``await``.
for _result_cls in (Result, SearchResults, Answer, AgentRun, AgentTrace):
    if "__await__" not in getattr(_result_cls, "__dict__", {}):
        _result_cls.__await__ = _identity_await

_apply_async_to(globals())
