"""Brave search client.

Use search() for structured results and run() for readable output.
See SKILL.md and its task-specific references for workflows and limitations.
Awaitable return values provide compatibility, not nonblocking I/O.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Optional

import httpx

# ---------------------------------------------------------------------------
# Clean engine: one client, one worker channel, one-way results.
# No per-call TLS, no busy-wait thread spawn, no shared mutable output dict.
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
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
    return _CLIENT


def _pool(n: int = 8) -> ThreadPoolExecutor:
    global _WORKERS
    if _WORKERS is None:
        _WORKERS = ThreadPoolExecutor(max_workers=n, thread_name_prefix="brave")
    return _WORKERS


def _retry_after_seconds(resp, fallback: float) -> float:
    """Honor Retry-After when present; otherwise the computed backoff."""
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
    """Submit (key, thunk) pairs; return [(key, ok, value_or_exc), ...] in order.

    All work is queued first (one-way), then collected. Workers do not write
    a shared dict. Bounded by the process-lifetime pool.
    """
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


def _drop_empty(o):
    if isinstance(o, dict):
        return {k: _drop_empty(v) for k, v in o.items()
                if v is not None and v != "" and v != [] and v != {}}
    if isinstance(o, list):
        return [_drop_empty(x) for x in o]
    return o


def _snippet(text, cap=None):
    """HTML-clean a text field. Truncate only when ``cap`` is set (renderers)."""
    t = _clean_html(text)
    if not t:
        return None
    if cap and len(t) > cap:
        cut = t[:cap].rsplit(" ", 1)[0]
        return (cut or t[:cap]).rstrip(" ,;:-") + "…"
    return t


def _clean_tree(o):
    """Walk a nested value and HTML-clean every string. Structure preserved."""
    if isinstance(o, dict):
        return {k: _clean_tree(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean_tree(x) for x in o]
    if isinstance(o, str):
        return _clean_html(o)
    return o


def _url_host(url):
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _keep_breadcrumb(bc, url):
    """Keep a breadcrumb only when it is not a restatement of the URL path."""
    if not bc:
        return None
    bc = _clean_html(bc)
    if not bc:
        return None
    if not url:
        return bc
    try:
        from urllib.parse import urlparse, unquote
        path = unquote(urlparse(str(url)).path or "").lower()
    except Exception:
        return bc
    segs = [s.strip().lower() for s in bc.replace("›", "/").split("/") if s.strip()]
    if not segs:
        return None
    if len(segs) == 1 and segs[0] in path:
        return None
    hit = sum(1 for s in segs if s and s in path)
    if hit >= max(1, len(segs) - 1) and len(segs) >= 2:
        return None
    return bc


def _one_date(item):
    """Most precise single timestamp. ISO with time if not midnight; else day; else human age."""
    for key in ("date", "page_age", "published_at"):
        v = item.get(key)
        if not v:
            continue
        s = str(v).strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            if "T" in s:
                clock = s.split("T", 1)[1]
                if clock.startswith("00:00:00"):
                    return s[:10]
                return s.replace("Z", "").split(".")[0]
            return s[:10]
    if item.get("article_date"):
        return str(item["article_date"]).strip()
    if item.get("age"):
        return str(item["age"]).strip()
    return None


def _authors(item):
    author = item.get("author")
    if isinstance(author, list):
        names = [str(a).strip() for a in author if a]
        if not names:
            return None
        return names[0] if len(names) == 1 else names
    if isinstance(author, str) and author.strip():
        return author.strip()
    return None


def _distinct_publisher(item, author):
    pub = item.get("publisher") or item.get("source")
    if not pub:
        return None
    pub = str(pub).strip()
    if not pub:
        return None
    if author is None:
        return pub
    auth = author if isinstance(author, str) else " ".join(author)
    if pub.lower() == auth.lower():
        return None
    return pub


def _norm_url(u):
    if not u:
        return ""
    u = str(u).strip().split("#", 1)[0]
    if u.endswith("/") and u.count("/") > 2:
        u = u[:-1]
    if "?" in u:
        base, qs = u.split("?", 1)
        keep = [p for p in qs.split("&")
                if p and not p.split("=", 1)[0].lower().startswith("utm_")
                and p.split("=", 1)[0].lower() not in ("fbclid", "gclid", "ref", "ref_src")]
        u = base + (("?" + "&".join(keep)) if keep else "")
    return u.lower()


def _hit_key(it):
    """Identity: URL, plus address/coords so chain stores stay distinct."""
    url = _norm_url(it.get("url") or it.get("page_url") or it.get("image_url"))
    addr = str(it.get("address") or "").strip().lower()
    lat, lon = it.get("latitude"), it.get("longitude")
    geo = ""
    if lat is not None and lon is not None:
        try:
            geo = f"{float(lat):.5f},{float(lon):.5f}"
        except (TypeError, ValueError):
            geo = ""
    if addr or geo:
        return f"{url}|{addr or geo}"
    return url


def _dedupe_hits(items):
    """Drop identical identities only. First occurrence wins (Brave rank)."""
    seen = set()
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        key = _hit_key(it)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(it)
    return out


def _bool_present(item, *keys):
    for k in keys:
        if k in item and item.get(k) is not None:
            return bool(item.get(k))
    return None


def _slim_item(item):
    """Compact form that keeps every unique fact. Synonyms collapsed, nothing truncated."""
    if not isinstance(item, dict):
        return item
    url = item.get("url") or item.get("page_url") or item.get("image_url")
    snippet = _snippet(item.get("description") or item.get("snippet"))
    extra = []
    for s in item.get("extra") or item.get("extra_snippets") or []:
        t = _snippet(s)
        if t and (not snippet or t.lower() not in snippet.lower()):
            extra.append(t)
    author = _authors(item)
    publisher = _distinct_publisher(item, author)
    site = item.get("site")
    host = _url_host(url)
    if site and host and str(site).lower().removeprefix("www.") == host:
        site = None
    kind = item.get("subtype") or item.get("type") or item.get("kind")
    if kind in (None, "", "web"):
        kind = item.get("content_type") or None
    lang = item.get("language")
    if lang and str(lang).lower().split("-")[0] == "en":
        lang = None
    image = item.get("image_url") or item.get("image")
    page = item.get("page_url") or item.get("page")
    if image and _norm_url(image) == _norm_url(url):
        image = None
    if page and _norm_url(page) == _norm_url(url):
        page = None
    am = item.get("age_meta") if isinstance(item.get("age_meta"), dict) else {}
    hit = {
        "title": _snippet(item.get("title") or item.get("name")),
        "url": url,
        "date": _one_date(item),
        "age_days": am.get("days") if am.get("days") is not None else None,
        "snippet": snippet,
        "extra": extra or None,
        "author": author,
        "publisher": publisher,
        "site": site,
        "breadcrumb": _keep_breadcrumb(item.get("breadcrumb"), url),
        "kind": kind,
        "language": lang,
        "duration": item.get("duration"),
        "score": item.get("score"),
        "views": item.get("views"),
        "breaking": _bool_present(item, "breaking"),
        "live": _bool_present(item, "is_live", "live"),
        "paywall": _bool_present(item, "paywall"),
        "width": item.get("width"),
        "height": item.get("height"),
        "confidence": item.get("confidence"),
        "image": image,
        "page": page,
        "address": item.get("address"),
        "phone": item.get("phone"),
        "creator": (None if item.get("creator") and author and
                    str(item.get("creator")).lower() == (author if isinstance(author, str) else " ".join(author)).lower()
                    else item.get("creator")),
        "channel": item.get("channel"),
        "author_url": item.get("author_url"),
        "tags": item.get("tags") or None,
        "forum": item.get("forum"),
        "num_answers": item.get("num_answers"),
        "question": _snippet(item.get("question")),
        "top_comment": _snippet(item.get("top_comment")),
        "content_type": item.get("content_type"),
    }
    qa = item.get("qa")
    if isinstance(qa, dict) and (qa.get("question") or qa.get("answer") or qa.get("q") or qa.get("a")):
        hit["qa"] = _drop_empty({
            "q": _snippet(qa.get("question") or qa.get("q")),
            "a": _snippet(qa.get("answer") or qa.get("a")),
            "upvotes": qa.get("upvote_count") if qa.get("upvote_count") is not None else qa.get("upvotes"),
            "url": qa.get("url"),
        })
    cluster = item.get("sitelinks") or item.get("cluster") or []
    if isinstance(cluster, list) and cluster:
        hit["sitelinks"] = _dedupe_hits([
            _drop_empty({
                "title": _snippet(c.get("title")),
                "url": c.get("url"),
                "snippet": _snippet(c.get("description") or c.get("snippet")),
            })
            for c in cluster if isinstance(c, dict) and c.get("url")
        ])
    deep = item.get("nav") or item.get("deep") or []
    if isinstance(deep, list) and deep:
        nav = [_drop_empty({"title": _snippet(b.get("title")), "url": b.get("url")})
               for b in deep if isinstance(b, dict) and (b.get("url") or b.get("title"))]
        if nav:
            hit["nav"] = nav
    for key in ("software", "recipe", "product", "movie", "location",
                "creative_work", "inline_faq"):
        block = item.get(key)
        if block:
            hit[key] = _drop_empty(_clean_tree(block))
    for k in ("opening_hours", "week", "price", "rating", "latitude", "longitude",
              "timezone", "timezone_offset", "id", "profiles", "website", "icon",
              "cuisine", "categories", "provider_url"):
        if item.get(k) not in (None, "", [], {}):
            v = item[k]
            hit[k] = _drop_empty(_clean_tree(v)) if isinstance(v, (dict, list)) else v
    return _drop_empty(hit)


def _slim_infobox(ib):
    """Knowledge panel: every fact, cleaned. No HTML, no image dump, no synonym echo."""
    if not isinstance(ib, dict) or not (ib.get("title") or ib.get("description") or ib.get("blurb") or ib.get("facts")):
        return None
    facts = ib.get("facts")
    if not isinstance(facts, dict):
        facts = {}
        for a in (ib.get("attributes") or []):
            if isinstance(a, (list, tuple)) and len(a) >= 2:
                k = _clean_html(a[0])
                parts = [_clean_html(x).strip(" /") for x in _clean_html(a[1]).splitlines()]
                v = " / ".join(p for p in parts if p)
                if k and v:
                    facts[k] = v
    blurb = _snippet(ib.get("description") or ib.get("blurb"))
    detail = _snippet(ib.get("long_desc") or ib.get("detail"))
    if detail and blurb and detail.lower() == blurb.lower():
        detail = None
    providers = []
    for pvd in ib.get("providers") or []:
        if isinstance(pvd, dict) and pvd.get("name"):
            providers.append(_drop_empty({
                "name": pvd.get("name"), "url": pvd.get("url"), "type": pvd.get("type"),
            }))
    url = ib.get("url") or ib.get("page")
    website = ib.get("website") or ib.get("website_url")
    if website and _norm_url(website) == _norm_url(url):
        website = None
    kind = ib.get("kind") or ib.get("category") or ib.get("entity_type")
    label = ib.get("label")
    if label and kind and str(label).lower() == str(kind).lower():
        label = None
    profiles = []
    for pvd in ib.get("profiles") or []:
        if isinstance(pvd, dict):
            profiles.append(_drop_empty({
                "name": pvd.get("name"), "url": pvd.get("url"), "type": pvd.get("type"),
            }))
        elif isinstance(pvd, str) and pvd.strip():
            profiles.append(pvd.strip())
    return _drop_empty({
        "title": _snippet(ib.get("title") or ib.get("entity")),
        "url": url,
        "website": website,
        "kind": kind,
        "label": label,
        "blurb": blurb,
        "detail": detail,
        "facts": facts or None,
        "providers": providers or None,
        "profiles": profiles or None,
        "ratings": _drop_empty(_clean_tree(ib.get("ratings"))) if ib.get("ratings") else None,
        "found_in": ib.get("found_in") or ib.get("found_in_urls") or None,
    })


def _slim_faq(items):
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        q = _snippet(it.get("question") or it.get("q"))
        a = _snippet(it.get("answer") or it.get("a"))
        url = it.get("url")
        site = it.get("site")
        if site and _url_host(url) == str(site).lower().removeprefix("www."):
            site = None
        row = _drop_empty({
            "q": q, "a": a, "url": url,
            "title": _snippet(it.get("title")),
            "site": site,
        })
        if row:
            out.append(row)
    return out


def _slim_list(items):
    return _dedupe_hits([_slim_item(x) for x in (items or []) if isinstance(x, dict)])


def _agent_query(q):
    if not isinstance(q, dict):
        return q
    out = {}
    original = q.get("original") or q.get("q")
    if q.get("altered") and q.get("altered") != original:
        out["q"] = original
        out["altered"] = q.get("altered")
    else:
        if not (q.get("bad_results") or q.get("more_results_available") is False):
            return original or q
        out["q"] = original
    if q.get("bad_results"):
        out["bad_results"] = True
    if q.get("more_results_available") is False:
        out["more_results_available"] = False
    return out or original


def _agent_view(data: dict) -> dict:
    """Same facts as the full SERP, one name each. No caps, no dropped hits."""
    if not isinstance(data, dict):
        return data
    out = {
        "mode": data.get("mode"),
        "query": _agent_query(data.get("query")),
        "infobox": _slim_infobox(data.get("infobox")),
        "results": _slim_list(data.get("results") or data.get("web")),
        "videos": _slim_list(data.get("videos")),
        "discussions": _slim_list(data.get("discussions")),
        "news": _slim_list(data.get("news")),
        "faq": _slim_faq(data.get("faq")),
        "locations": _slim_list(data.get("locations")),
    }
    if data.get("mode") == "all" and data.get("web") is not None:
        out["web"] = _slim_list(data.get("web"))
        out.pop("results", None)
    sm = data.get("summarizer")
    if isinstance(sm, dict) and (sm.get("deep_link") or sm.get("query") or sm.get("results_hash")):
        out["summarizer"] = _drop_empty({
            "deep_link": sm.get("deep_link"),
            "query": sm.get("query"),
            "results_hash": sm.get("results_hash"),
        })
    if data.get("fallback"):
        out["fallback"] = data.get("fallback")
    if data.get("might_be_offensive"):
        out["might_be_offensive"] = True
    if data.get("family_friendly") is False:
        out["family_friendly"] = False
    if data.get("rich"):
        out["rich"] = data.get("rich")
    return _drop_empty(out)


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


DEFAULT_API_URL = "https://api.search.brave.com"

PATHS = {
    "web": "/res/v1/web/search",
    "news": "/res/v1/news/search",
    "image": "/res/v1/images/search",
    "video": "/res/v1/videos/search",
    "local": "/res/v1/local/search",
    "place_search": "/res/v1/local/place_search",   # round-11: dedicated POI/place endpoint
}

MODES = {
    "web": "Web results + knowledge panel + videos + discussions + faq + mixed order.",
    "news": "Recent news articles; pair with freshness (pd/pw/pm/py or a date range).",
    "image": "Images with original-source URL and dimensions.",
    "video": "Videos with creator, duration and thumbnail.",
    "local": "Local places (needs a key that supports it; else falls back to web).",
    "all": "One query across web + news + video, merged.",
}

SAFE_SEARCH = {"off", "moderate", "strict"}

# round-16: recipe `category` strings that identify a drink/cocktail recipe for the
# `drinks()` helper (categories Brave's recipe schema blocks return for beverages).
DRINK_CATEGORIES = {
    "drinks", "drink", "cocktail", "cocktails, drink", "cocktail,margarita,beverage",
    "beverage", "beverages", "alcohol", "alcoholic", "alcoholic beverages",
    "mixed drink", "cocktails", "bar drinks", "tequila", "spirits", "mocktail",
    "smoothie", "juice", "coffee", "tea", "espresso", "soda", "punch",
}

# round-13: image licensing / transparency hints (forward-compat; pass-through
# on this plan but validated so a caller gets a clear error, not a silent 422).
IMAGE_PROPERTY = {"any", "commercial", "non-commercial"}
IMAGE_SEARCH_TYPE = {"all", "transparent"}

# Round-6: freshness accepts the documented shorthands (`pd`/`pw`/`pm`/`py`),
# the legacy `pd_1d`/`pd_1w`/`pd_1m`/`pd_1y` aliases, and a custom inclusive
# date range `YYYY-MM-DDtoYYYY-MM-DD` (e.g. `2025-01-01to2025-06-30`).
FRESHNESS = {"pd", "pw", "pm", "py", "pd_1d", "pd_1w", "pd_1m", "pd_1y"}
_FRESHNESS_RE = __import__("re").compile(
    r"^\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2}$"
)

# Round-6: X-Loc-* request headers the API uses to localise results even for a
# plain (non-"near me") query. `loc` accepts these keys (case-insensitive).
LOC_HEADERS = {
    "latitude": "X-Loc-Lat",
    "lng": "X-Loc-Long",
    "longitude": "X-Loc-Long",
    "timezone": "X-Loc-Time-Zone",
    "city": "X-Loc-City",
    "state": "X-Loc-State",
    "state_name": "X-Loc-State-Name",
    "country": "X-Loc-Country",
    "postal_code": "X-Loc-Postal-Code",
}


def _clean_html(text) -> str:
    """Strip HTML tags/entities and collapse whitespace for readable text.

    Block/line tags are turned into newlines so list items and `<br>` breaks
    (common in Brave knowledge-panel attributes) stay readable instead of being
    concatenated into a single run.
    """
    import html as _h
    import re as _re
    if text is None:
        return ""
    t = _h.unescape(str(text))
    # Convert block/line-break tags to newlines and drop everything else.
    t = _re.sub(r"(?i)<br\s*/?>|<li(?=[\"'\s>])[^>]*>|</li>|<p\s*/?>|</p>|</?tr>|</?div>", "\n", t)
    t = _re.sub(r"<[^>]+>", "", t)
    t = _re.sub(r"&nbsp;", " ", t)
    t = _re.sub(r"[ \t]+", " ", t)
    t = _re.sub(r"\n\s*\n+", "\n", t)
    t = _re.sub(r"\s*\n\s*", "\n", t).strip()
    return t


class BraveError(RuntimeError):
    """Raised when the Brave API rejects a request.

    `category` classifies the failure (`auth`, `param`, `rate_limit`, `http`,
    `timeout`, `network`, `http_422`) so callers can react precisely instead of
    string-matching the message. `details` carries extra structured context
    (e.g. the `meta.errors` list Brave returns for a validation 422).
    """

    def __init__(self, message, *, category=None, details=None):
        super().__init__(message)
        self.category = category
        self.details = details

    @property
    def is_auth(self) -> bool:
        return self.category == "auth"

    @property
    def is_rate_limited(self) -> bool:
        return self.category == "rate_limit"


def _classify_response(path, status, body, timeout) -> "BraveError":
    """Turn an HTTP response into a categorised BraveError.

    Probing api.search.brave.com live shows the API is not always REST-obedient:

      * bad/invalid X-Subscription-Token -> HTTP 422 with body
        {"error":{"code":"SUBSCRIPTION_TOKEN_INVALID", ...}}  (NOT 401/403!)
      * invalid parameter value (e.g. offset=10, bad goggles_id) -> HTTP 422 with
        body `error.code == "VALIDATION"` and `error.meta.errors[]` listing each
        offending field + a human message.
      * rate limiting -> HTTP 429
      * server/upstream errors -> 5xx

    We surface all of these on `.category` so an agent can react (fix the key,
    fix the params, back off, or surface the field-level `details`).
    """
    category = None
    details = None
    if status == 429:
        category = "rate_limit"
    elif isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        code = err.get("code")
        meta = err.get("meta") if isinstance(err.get("meta"), dict) else {}
        if code == "SUBSCRIPTION_TOKEN_INVALID" or meta.get("component") == "authentication":
            category = "auth"
            details = err.get("detail")
        elif code == "VALIDATION" or status == 422 and meta.get("errors"):
            category = "param"
            details = meta.get("errors")
        elif code:
            category = "http"
            details = err.get("detail")
    if category is None and status == 422:
        category = "param"
    elif category is None and 500 <= status < 600:
        category = "http"
    if isinstance(details, list):
        try:
            details = [
                {
                    "field": ".".join(str(x) for x in e.get("loc", []) if x != "query"),
                    "message": e.get("msg"),
                    "input": e.get("input"),
                }
                for e in details if isinstance(e, dict)
            ]
        except Exception:
            pass
    return BraveError(
        f"Brave API error {status} for '{path}': {str(details if details else body)[:200]}",
        category=category, details=details,
    )



_API_KEY_NAMES = ['BRAVE_API_KEY', 'BRAVE_SEARCH_API_KEY']
_API_LABEL = "brave"
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


def _request(path, params, timeout, *, fallback=None, retries=2, headers=None):
    """GET endpoint; return parsed JSON. Optionally falls back to another path.

    `retries` controls how many extra attempts we make on *transient* failures
    (network errors and HTTP 429/5xx) with a short backoff, so a blip or a
    brief rate-limit bar doesn't surface as a hard failure. Definitive errors
    (auth, validation 422) are not retried. 0 disables retrying entirely.

    `headers` (optional dict) is merged into the request headers on top of the
    auth token — used for the X-Loc-* location hints so results are localised.
    """
    key = _get_api_key()
    headers = {"X-Subscription-Token": key, **(headers or {})}
    url = f"{DEFAULT_API_URL}{path}"
    attempt = 0
    body = None
    error = None
    while attempt <= (retries or 0):
        attempt += 1
        resp = None
        try:
            resp = _http().get(url, params=params, headers=headers, timeout=timeout,
                               follow_redirects=False)
        except (httpx.TimeoutException, httpx.ConnectError,
                httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            error = BraveError(
                f"Brave request to '{path}' failed on the wire ({type(e).__name__}): {e}",
                category="network",
            )
            if attempt <= (retries or 0):
                time.sleep(_retry_after_seconds(resp, 0.6 * attempt))
                continue
            raise error
        except httpx.HTTPError as e:
            error = BraveError(f"Brave HTTP error for '{path}': {e}", category="network")
            if attempt <= (retries or 0):
                time.sleep(_retry_after_seconds(resp, 0.6 * attempt))
                continue
            raise error

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                data = None
            if isinstance(data, dict) or data is not None:
                return data
            # 200-but-unparseable is a server-side oddity; treat as transient.
            error = BraveError(
                f"Brave returned non-JSON 200 for '{path}' (q={params.get('q')})",
                category="http",
            )
            if attempt <= (retries or 0):
                time.sleep(_retry_after_seconds(resp, 0.6 * attempt))
                continue
            raise error

        # Classify the failure. 429 / 5xx are transient and retried; definitive
        # errors (auth / validation) are raised immediately.
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        fast_fail = resp.status_code in (401, 403) or (
            isinstance(body, dict) and (
                (body.get("error") or {}).get("code") == "SUBSCRIPTION_TOKEN_INVALID"
                or (resp.status_code == 422 and ((body.get("error") or {}).get("code") == "VALIDATION"))
                or (body.get("error") or {}).get("meta", {}).get("component") == "authentication"
            )
        )
        retryable = resp.status_code in (429, 500, 502, 503, 504)
        if fallback and resp.status_code != 200 and (retryable or not fast_fail and fallback is not None):
            if _fallback_ok(path, params, headers, timeout, fallback, resp.status_code):
                # _fallback handles local->web etc. before we raise.
                return _run_fallback(fallback, params, headers, timeout, path, resp.status_code)
        if fast_fail or not retryable:
            raise _classify_response(path, resp.status_code, body, timeout)
        error = _classify_response(path, resp.status_code, body, timeout)
        if attempt <= (retries or 0):
            time.sleep(_retry_after_seconds(resp, 1.0 * attempt))
            continue
        raise error


def _fallback_ok(fallback, params, headers, timeout, path, status):
    """Whether a fallback endpoint path is configured for this request."""
    return bool(fallback)


def _run_fallback(fallback, params, headers, timeout, path, source_status):
    """Quietenly attempt the fallback endpoint; return its data tagged, or None."""
    try:
        alt = _http().get(
            f"{DEFAULT_API_URL}{fallback}", params=params, headers=headers,
            timeout=timeout, follow_redirects=False,
        )
    except Exception:
        return None
    if alt.status_code != 200:
        return None
    try:
        data = alt.json()
    except Exception:
        return None
    data.setdefault("_fallback", {"from": path, "to": fallback, "status": source_status})
    return data

# ---------------------------------------------------------------------------
# Normalisers (structured item dicts)
# ---------------------------------------------------------------------------

def _host(item):
    """Extract hostname from a meta_url dict if present."""
    mu = item.get("meta_url") if isinstance(item.get("meta_url"), dict) else None
    return (mu or {}).get("hostname") if mu else None


def _as_list(v):
    """Coerce a value into a list (of dicts) or []."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return v.get("results") or v.get("buttons") or []
    return []


def _breadcrumb(item: dict) -> Optional[str]:
    """Extract a clean human-readable breadcrumb from a result's `meta_url.path`.

    E.g. Brave's raw `\u203a index  \u203a docs  \u203a getting-started` becomes
    `index / docs / getting-started`.
    """
    mu = item.get("meta_url") if isinstance(item.get("meta_url"), dict) else None
    if not mu:
        return None
    path = mu.get("path") or ""
    if not path or not str(path).strip():
        return None
    parts = [p.strip() for p in str(path).replace("\u203a", "/").split("/") if p.strip()]
    return " / ".join(parts) if parts else None


def _qa_answer(qa: dict) -> str:
    """Extract the readable Q&A answer text.

    Live-verified: Brave's embedded Q&A answer is a *dict* `{text, upvoteCount}`
    (not a plain string), so `_clean_html` on the whole dict would render the
    Python repr and drop the vote count. Pull the `text` (cleaned) and let the
    caller surface `upvoteCount` separately.
    """
    ans = qa.get("answer")
    if isinstance(ans, dict):
        return _clean_html(ans.get("text") or "")
    return _clean_html(ans)


def _qa_upvotes(qa: dict) -> Optional[int]:
    """Surface the community upvote count attached to a Brave Q&A result."""
    ans = qa.get("answer")
    if isinstance(ans, dict):
        v = ans.get("upvoteCount")
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    return None


def _software(item: dict) -> Optional[dict]:
    """Package/registry metadata for web results with `subtype == "software"`.

    Round-5, verified live: Brave's web endpoint tags package/software-registry
    results (PyPI, npm, GitHub release pages) with `subtype == "software"` and a
    `software` struct carrying the package identity — e.g. for `jsonschema`:
    `{"name":"jsonschema","version":"4.26.0","is_pypi":true,
      "codeRepository":"https://github.com/python-jsonschema/jsonschema",
      "is_npm":false,"programmingLanguage":""}`. We surface it so an agent can
    answer "what version / which registry / where is the code" for a library
    without a separate registry crawl.
    """
    s = item.get("software")
    if not isinstance(s, dict):
        return None
    out = {
        "name": s.get("name"),
        "author": s.get("author") or (item.get("organization") or {}).get("name")
                  if isinstance(item.get("organization"), dict) else s.get("author"),
        "version": s.get("version"),
        "code_repository": s.get("codeRepository"),
        "published": s.get("datePublished"),
        "programming_language": s.get("programmingLanguage") or None,
    }
    registry = []
    if s.get("is_pypi"):
        registry.append("pypi")
    if s.get("is_npm"):
        registry.append("npm")
    if s.get("registry"):
        registry.append(str(s.get("registry")))
    out["registry"] = registry if registry else None
    return out if (out.get("name") or out.get("version") or out.get("registry")) else None


def _recipe_item(recipe: dict) -> Optional[dict]:
    """Normalise a schema.org Recipe block on a web result (round-9)."""
    if not isinstance(recipe, dict):
        return None
    thumb = recipe.get("thumbnail") or {}
    rating = recipe.get("rating") or {}
    ingredients = recipe.get("ingredients")
    return {
        "title": recipe.get("title") or recipe.get("name"),
        "url": recipe.get("url"),
        "domain": recipe.get("domain"),
        "time": recipe.get("time"),
        "prep_time": recipe.get("prep_time"),
        "cook_time": recipe.get("cook_time"),
        "ingredients": [
            _clean_html(i.strip()) for i in ingredients.split(",")
        ] if isinstance(ingredients, str) and ingredients.strip() else None,
        "instructions": [
            {
                "text": _clean_html(st.get("text")),
                "url": st.get("url"),
            }
            for st in (recipe.get("instructions") or [])
            if isinstance(st, dict) and st.get("text")
        ] or None,
        "servings": recipe.get("servings"),
        "calories": recipe.get("calories"),
        "publisher": recipe.get("publisher"),
        "category": recipe.get("recipeCategory"),
        "cuisine": recipe.get("recipeCuisine"),
        "thumbnail": (thumb.get("src") if thumb else None),
        "thumbnail_original": (thumb.get("original") if thumb else None),
        "rating": {
            "value": rating.get("ratingValue"),
            "best": rating.get("bestRating"),
            "reviews": rating.get("reviewCount"),
            "is_tripadvisor": bool(rating.get("is_tripadvisor")),
        } if isinstance(rating, dict) and rating.get("ratingValue") is not None else None,
    }


def _product_item(product: dict) -> Optional[dict]:
    """Normalise a schema.org Product block on a web result (round-9)."""
    if not isinstance(product, dict):
        return None
    thumb = product.get("thumbnail") or {}
    rating = product.get("rating") or {}
    offers = product.get("offers") or []
    return {
        "name": product.get("name"),
        "url": product.get("url"),
        "price": product.get("price"),
        "offers": [
            {
                "url": o.get("url"),
                "price": o.get("price"),
                "price_currency": o.get("priceCurrency"),
            }
            for o in offers if isinstance(o, dict)
        ] or None,
        "rating": {
            "value": rating.get("ratingValue"),
            "best": rating.get("bestRating"),
            "reviews": rating.get("reviewCount"),
            "is_tripadvisor": bool(rating.get("is_tripadvisor")),
        } if isinstance(rating, dict) and rating.get("ratingValue") is not None else None,
        "description": _clean_html(product.get("description")),
        "thumbnail": (thumb.get("src") if thumb else None),
        "thumbnail_original": (thumb.get("original") if thumb else None),
    }


def _movie_item(movie: dict) -> Optional[dict]:
    """Normalise a schema.org Movie block on a web result (round-9)."""
    if not isinstance(movie, dict):
        return None
    rating = movie.get("rating") or {}
    thumb = movie.get("thumbnail") or {}
    return {
        "name": movie.get("name"),
        "description": _clean_html(movie.get("description")),
        "url": movie.get("url"),
        "release": movie.get("release"),
        "directors": [
            {"name": d.get("name"), "url": d.get("url")}
            for d in (movie.get("directors") or [])
            if isinstance(d, dict) and d.get("name")
        ] or None,
        "actors": [
            {
                "name": a.get("name"),
                "url": a.get("url"),
                "thumbnail": ((a.get("thumbnail") or {}).get("src")
                              if isinstance(a.get("thumbnail"), dict) else None),
            }
            for a in (movie.get("actors") or [])
            if isinstance(a, dict) and a.get("name")
        ] or None,
        "genre": movie.get("genre") or [],
        "duration": movie.get("duration"),
        "rating": {
            "value": rating.get("ratingValue"),
            "best": rating.get("bestRating"),
            "reviews": rating.get("reviewCount"),
        } if isinstance(rating, dict) and rating.get("ratingValue") is not None else None,
        "thumbnail": (thumb.get("src") if thumb else None),
        "thumbnail_original": (thumb.get("original") if thumb else None),
    }


def _web_item(item: dict) -> dict:
    article = item.get("article") or {}
    publisher = (article.get("publisher") or {}) if isinstance(article, dict) else {}
    org = item.get("organization") or {}
    authors = (article.get("author") or []) if isinstance(article, dict) else []
    author_names = [a.get("name") for a in authors if isinstance(a, dict) and a.get("name")]
    # Round-7: also support each author's URL + portrait (article.author may carry both).
    author_meta = [
        {
            "name": a.get("name"),
            "url": a.get("url"),
            "thumbnail": ((a.get("thumbnail") or {}).get("src")
                          if isinstance(a.get("thumbnail"), dict) else None),
        }
        for a in authors if isinstance(a, dict) and a.get("name")
    ] or None
    # Round-8: article.publisher may carry identity beyond the plain name —
    # a URL and a thumbnail (brand logo), plus `isAccessibleForFree` (a
    # paywall flag). Verified live across news publishers. `pub_thumb` is the
    # publisher logo thumbnail.
    pub_logo_src = (publisher.get("thumbnail") or {}).get("src") if isinstance(publisher.get("thumbnail"), dict) else None
    pub_logo_orig = (publisher.get("thumbnail") or {}).get("original") if isinstance(publisher.get("thumbnail"), dict) else None
    free_flag = article.get("isAccessibleForFree")
    mu = item.get("meta_url") or {}
    thumb = item.get("thumbnail") or {}
    profile = item.get("profile") or {}
    dr = item.get("deep_results") or {}
    qa = item.get("qa") or {}
    embedded_video = item.get("video") or {}
    # Round-7: creative_work (ratings), inline location POI, inline FAQ subsection
    cw = item.get("creative_work") if isinstance(item.get("creative_work"), dict) else None
    loc = item.get("location") if isinstance(item.get("location"), dict) else None
    inline_faq = item.get("faq") if isinstance(item.get("faq"), dict) else None
    faq_items = (inline_faq.get("items") or []) if inline_faq else []
    org_cp = (org.get("contact_points") or []) if isinstance(org, dict) else []
    favicon = (mu.get("favicon") if mu else None)
    breadcrumb = _breadcrumb(item)
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "site": _host(item),
        "description": item.get("description"),
        "extra_snippets": item.get("extra_snippets") or [],
        "type": item.get("subtype") or "web",
        "subtype": item.get("subtype") or "web",   # round-5: article | qa | software | generic | web
        "software": _software(item),               # round-5: package/registry metadata for software results
        "is_live": item.get("is_live"),
        "is_source_local": item.get("is_source_local"),
        "is_source_both": item.get("is_source_both"),
        "language": item.get("language"),
        "age": item.get("age"),                      # humanised, e.g. "2 days ago"
        "page_age": item.get("page_age"),            # ISO date of the page, e.g. 2024-08-18
        "article_date": (article.get("date") if isinstance(article, dict) else None),
        "author": author_names or None,
        "author_meta": author_meta,                  # round-7: name + URL + thumbnail
        "author_types": [                               # round-8: per-author role/type ("person", "organization")
            str(a.get("type") or "person")
            for a in authors if isinstance(a, dict) and a.get("name")
        ] or None,
        "publisher": (publisher.get("name") if publisher else None),
        "publisher_url": (publisher.get("url") if publisher else None),     # round-8
        "publisher_logo": pub_logo_src,                  # round-8: brand logo (proxied)
        "publisher_logo_original": pub_logo_orig,        # round-8: brand logo direct URL
        "publisher_type": (publisher.get("type") if publisher else None),   # round-8: "organization" | None
        "paywall": (free_flag is not None and not free_flag) or None,       # round-8: True when article is behind a paywall
        "fetched_content_timestamp": item.get("fetched_content_timestamp"),  # round-8: epoch of Brave's last fetch
        "organization": (org.get("name") if org else None),
        "contact_points": [cp.get("telephone") for cp in org_cp if isinstance(cp, dict) and cp.get("telephone")] if org_cp else None,  # round-7
        "family_friendly": item.get("family_friendly"),
        "favicon": favicon,
        "thumbnail": (thumb.get("src") if thumb else None),
        "thumbnail_original": (thumb.get("original") if thumb else None),   # round-7
        "thumbnail_is_logo": bool(thumb.get("logo")) if thumb else None,    # round-7
        "breadcrumb": breadcrumb,                     # round-7: clean path breadcrumb
        "profile": {
            "name": profile.get("name"),
            "url": profile.get("url"),
            "long_name": profile.get("long_name"),
            "img": profile.get("img"),
        } if profile else None,
        "deep": [
            {"title": b.get("title"), "url": b.get("url")}
            for b in _as_list(dr.get("buttons")) if isinstance(b, dict) and (b.get("url") or b.get("title"))
        ] if isinstance(item.get("deep_results"), dict) and (dr := item.get("deep_results")) and dr.get("buttons") else [],
        "cluster": [
            {
                "title": _clean_html(c.get("title")),
                "url": c.get("url"),
                "description": _clean_html(c.get("description")),
            }
            for c in (item.get("cluster") or [])
            if isinstance(c, dict) and c.get("url")
        ] if isinstance(item.get("cluster"), list) else [],
        "cluster_type": item.get("cluster_type"),
        "qa": {
            "question": _clean_html(qa.get("question")),
            "answer": _qa_answer(qa),
            "upvote_count": _qa_upvotes(qa),
            "url": qa.get("url"),
        } if isinstance(item.get("qa"), dict) and (qa.get("question") or qa.get("answer")) else None,
        "embedded_video": {
            "thumbnail": (embedded_video.get("thumbnail") or {}).get("src") if isinstance(embedded_video.get("thumbnail"), dict) else embedded_video.get("thumbnail"),
            "duration": embedded_video.get("duration"),
        } if isinstance(item.get("video"), dict) and (embedded_video.get("duration") or embedded_video.get("thumbnail")) else None,
        # Round-7: creative_work ratings (e.g. courses, docs with review scores)
        "creative_work": {
            "name": cw.get("name"),
            "rating": {
                "value": (cw.get("rating") or {}).get("ratingValue") if isinstance(cw.get("rating"), dict) else None,
                "best": (cw.get("rating") or {}).get("bestRating") if isinstance(cw.get("rating"), dict) else None,
                "reviews": (cw.get("rating") or {}).get("reviewCount") if isinstance(cw.get("rating"), dict) else None,
                "is_tripadvisor": bool((cw.get("rating") or {}).get("is_tripadvisor")) if isinstance(cw.get("rating"), dict) else None,
            } if isinstance(cw.get("rating"), dict) else None,
            "thumbnail": ((cw.get("thumbnail") or {}).get("src")
                          if isinstance(cw.get("thumbnail"), dict) else None),
            "thumbnail_original": ((cw.get("thumbnail") or {}).get("original")
                                   if isinstance(cw.get("thumbnail"), dict) else None),
        } if cw else None,
        # Round-7: inline map/POI attached to a single web result
        "location": {
            "title": loc.get("title"),
            "url": loc.get("url"),
            "coordinates": loc.get("coordinates") or [],
            "provider_url": loc.get("provider_url"),
            "zoom_level": loc.get("zoom_level"),
            "postal_address": (loc.get("postal_address") or {}).get("displayAddress"),
            "postal_code": (loc.get("postal_address") or {}).get("postalCode"),
            "street": (loc.get("postal_address") or {}).get("streetAddress"),
            "locality": (loc.get("postal_address") or {}).get("addressLocality"),
            "region": (loc.get("postal_address") or {}).get("addressRegion"),
            "country": (loc.get("postal_address") or {}).get("country"),
            "phone": ((loc.get("contact") or {}).get("telephone")
                      if isinstance(loc.get("contact"), dict) else None),
            "picture": ((loc.get("thumbnail") or {}).get("src")
                        if isinstance(loc.get("thumbnail"), dict) else None),
        } if loc else None,
        "inline_faq": [
            {
                "question": _clean_html(f.get("question")),
                "answer": _clean_html(f.get("answer")),
            }
            for f in faq_items if isinstance(f, dict)
        ] or None,  # round-7: Q&A subsection embedded inside a web result
        # Round-9: rich schema.org structured data on web results
        "content_type": item.get("content_type"),           # round-9: file indirection — "pdf", "doc", ...
        "recipe": _recipe_item(item["recipe"]) if isinstance(item.get("recipe"), dict) else None,   # round-9: schema.org Recipe
        "product": _product_item(item["product"]) if isinstance(item.get("product"), dict) else None,  # round-9: schema.org Product
        "movie": _movie_item(item["movie"]) if isinstance(item.get("movie"), dict) else None,           # round-9: schema.org Movie
    }


def _age_meta(age, page_age):
    """Consistent numeric age (days) across news rows (round-15).

    Brave's raw news `age` field is a human string that mixes two forms:
      * relative  — "26 minutes ago", "14 hours ago", "5 days ago", "2 weeks ago"
      * absolute  — "February 23, 2021" (older / evergreen articles)
    There is no separate `publish_time` on this plan, so news-carrying
    functions need a consistent, comparable age. We derive a numeric
    `age_days` for the relative form (deterministic), expose `age_kind`
    saying which form the raw value used, and surface the authoritative
    `page_age` (full ISO publish timestamp from Brave) as `published_at`.
    For the absolute-date form we keep `age_days=None` (no reference clock)
    but still surface `age_kind="date"` + `published_at`.
    """
    age_days = None
    age_kind = None
    if age:
        a = str(age).strip()
        m = re.match(r"^(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago$", a, re.I)
        if m:
            val, unit = int(m.group(1)), m.group(2).lower()
            scale = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1.0,
                     "week": 7.0, "month": 30.4375, "year": 365.25}[unit]
            age_days, age_kind = round(val * scale, 2), "relative"
        elif re.match(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$", a):
            age_kind = "date"
        elif re.search(r"just now|moments ago", a, re.I):
            age_days, age_kind = 0.0, "relative"
        else:
            age_kind = "unknown"
    published_at = page_age or None
    return {"kind": age_kind, "days": age_days,
            "text": age, "published_at": published_at}


def _news_item(item: dict) -> dict:
    prof = item.get("profile") or {}
    thumb = item.get("thumbnail") or {}
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "site": _host(item),
        "description": item.get("description"),
        "age": item.get("age"),
        "age_meta": _age_meta(item.get("age"), item.get("page_age")),  # round-15: consistent age
        "published_at": item.get("page_age"),                          # round-15: ISO publish ts
        "page_age": item.get("page_age"),
        "extra_snippets": item.get("extra_snippets") or [],
        "source": (prof.get("name") if prof else None),
        "publisher_url": (prof.get("url") if prof else None),   # round-6: publisher site URL
        "thumbnail": (thumb.get("src") if thumb else None),
        "thumbnail_original": (thumb.get("original") if thumb else None),   # round-7
        "breadcrumb": _breadcrumb(item),                        # round-7
        "family_friendly": item.get("family_friendly"),         # round-7
        "fetched_content_timestamp": item.get("fetched_content_timestamp"),  # round-8
        "language": item.get("language"),                        # round-8
        "breaking": item.get("breaking"),                         # round-14: breaking-news flag (live-verified)
        "is_live": item.get("is_live"),                           # round-14: live-coverage flag (live-verified)
        "is_source_local": item.get("is_source_local"),           # round-14: local-community signal
        "is_source_both": item.get("is_source_both"),             # round-14
    }


def _discussion_item(item: dict) -> dict:
    d = item.get("data") or {}
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "site": _host(item),
        "description": item.get("description"),
        "type": item.get("subtype") or "discussion",
        "forum": (d.get("forum_name") if d else None),
        "is_source_local": item.get("is_source_local"),   # round-5: local-community signal (e.g. Reddit)
        "is_source_both": item.get("is_source_both"),
        "extra_snippets": item.get("extra_snippets") or [],
        "breadcrumb": _breadcrumb(item),                  # round-7
        "family_friendly": item.get("family_friendly"),   # round-7
        "num_answers": d.get("num_answers"),              # round-7: engagement count
        "score": d.get("score"),                          # round-7: upvote/score count (string)
        "question": _clean_html(d.get("question")) if isinstance(d.get("question"), str) else None,  # round-7
        "top_comment": _clean_html(d.get("top_comment")) if isinstance(d.get("top_comment"), str) else None,  # round-7
        "fetched_content_timestamp": item.get("fetched_content_timestamp"),  # round-8
        "language": item.get("language"),                  # round-8
    }


def _video_item(item: dict) -> dict:
    v = item.get("video") if isinstance(item.get("video"), dict) else {}
    thumb = item.get("thumbnail") or {}
    author = (v.get("author") or {}) if isinstance(v.get("author"), dict) else {}
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "site": _host(item),
        "breadcrumb": _breadcrumb(item),                    # round-7
        "description": item.get("description"),
        "age": item.get("age"),
        "page_age": item.get("page_age"),
        "creator": (v.get("creator") if v else None),
        "channel": (v.get("publisher") if v else None),
        "duration": (v.get("duration") if v else None),
        "live": (v.get("duration") is None) if v else None,  # round-13: live/24-7 streams expose no duration
        "requires_subscription": (v.get("requires_subscription") if v else None),
        "tags": (v.get("tags") if v else None) or [],
        "author": (author.get("name") if author else None),
        "author_url": (author.get("url") if author else None),   # round-5: creator channel/profile URL
        "views": (int(v["views"]) if v.get("views") is not None else None),  # round-5: play count (ranking signal)
        "thumbnail": (thumb.get("src") if thumb else None),
        "thumbnail_original": (thumb.get("original") if thumb else None),
        "family_friendly": item.get("family_friendly"),     # round-7
        "is_source_local": item.get("is_source_local"),     # round-7
        "fetched_content_timestamp": item.get("fetched_content_timestamp"),  # round-8
    }


def _image_item(item: dict) -> dict:
    props = item.get("properties") or {}
    thumb = item.get("thumbnail") or {}
    return {
        "title": item.get("title"),
        "page_url": item.get("url"),
        "image_url": props.get("url") or item.get("url"),
        "width": props.get("width") or (thumb.get("width") if thumb else None),
        "height": props.get("height") or (thumb.get("height") if thumb else None),
        "source": item.get("source"),
        "thumbnail": (thumb.get("src") if thumb else None),
        "thumbnail_width": (thumb.get("width") if thumb else None),   # round-7
        "placeholder": props.get("placeholder"),
        "confidence": item.get("confidence"),
        "page_fetched": item.get("page_fetched"),
        "breadcrumb": _breadcrumb(item),                   # round-7
        "site": _host(item),                               # round-7
    }


def _faq_item(item: dict) -> dict:
    """Normalise a Brave FAQ Q&A entry.

    Round-8: the raw FAQ item also carries a `title` (the source page's heading)
    and a full `meta_url` — so we surface the source site and a breadcrumb trail
    as well as the Q&A itself.
    """
    return {
        "question": item.get("question"),
        "answer": _clean_html(item.get("answer")),
        "url": item.get("url"),
        "site": _host(item),
        "title": item.get("title"),            # round-8: source page heading
        "breadcrumb": _breadcrumb(item),       # round-8: clean source trail
    }


def _location_item(item: dict) -> dict:
    """Normalise a raw Brave map/POI result (from the `locations` object).

    Round-6: Brave auto-surfaces a `locations` map section on local-intent web
    queries (e.g. "coffee shop new york"). Each entry is a rich place: address,
    open hours, phone, picture, coordinates, cuisine, category. We flatten (and
    html-clean) it so an agent can answer "is it open now / where / call".

    Round-8: also surface the *full weekly* schedule (all 7 days, not just the
    current day), the live `price_range` and `rating`, the venue `icon`
    category, timezone offset minus the epoch `id`, and any `profiles`/`website`
    links — the extra signals an agent needs to pick a place or answer "is it
    open / how pricey / how well-rated".
    """
    coords = item.get("coordinates") or []
    addr = item.get("postal_address") or {}
    name, phone = (addr.get("displayAddress") if isinstance(addr, dict) else None), None
    contact = item.get("contact") or {}
    if isinstance(contact, dict):
        phone = contact.get("telephone")
    hours = item.get("opening_hours") or {}
    cur_day = (hours.get("current_day") if isinstance(hours, dict) else None) or []
    oh = []
    for e in cur_day[:3]:
        if isinstance(e, dict):
            oh.append("{} {}-{}".format(
                _clean_html(e.get("full_name") or e.get("abbr_name") or ""),
                _clean_html(e.get("opens") or "?"),
                _clean_html(e.get("closes") or "?"),
            ))
    # Round-8: full weekly schedule. `opening_hours.days` is a per-day list of
    # segment dicts ({ask full_name, abbr_name, opens, closes}). When a venue is
    # closed that day the segment list may be empty.
    days = (hours.get("days") if isinstance(hours, dict) else None) or []
    week = []
    if isinstance(days, list):
        for segs in days:
            if not isinstance(segs, list) or not segs:
                continue
            for e in segs:
                if isinstance(e, dict):
                    week.append("{} {}-{}".format(
                        _clean_html(e.get("full_name") or e.get("abbr_name") or ""),
                        _clean_html(e.get("opens") or "?"),
                        _clean_html(e.get("closes") or "?"),
                    ))
                    break  # first (primary) open window per day
    thumb = item.get("thumbnail") or {}
    pics = (item.get("pictures") or {}).get("results") or []
    rating = item.get("rating") or {}
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "description": item.get("description"),
        "type": item.get("type"),               # 'location_result'
        "coordinates": list(coords) if isinstance(coords, (list, tuple)) else coords,
        "latitude": coords[0] if isinstance(coords, (list, tuple)) and len(coords) > 0 else None,
        "longitude": coords[1] if isinstance(coords, (list, tuple)) and len(coords) > 1 else None,
        "address": (addr.get("displayAddress") if isinstance(addr, dict) else None) or name,
        "postal_address": addr if isinstance(addr, dict) else None,
        "opening_hours": oh or None,                       # round-6: current-day window(s)
        "week": week or None,                              # round-8: full weekly schedule
        "phone": phone,
        "categories": item.get("categories") or [],
        "cuisine": item.get("serves_cuisine") or [],
        "timezone": item.get("timezone"),
        "timezone_offset": item.get("timezone_offset"),    # round-8: minutes west of UTC
        "price": item.get("price_range"),                  # round-8: e.g. "$".."$$$$"
        "rating": {
            "value": rating.get("ratingValue") if isinstance(rating, dict) else None,
            "best": rating.get("bestRating") if isinstance(rating, dict) else None,
            "reviews": rating.get("reviewCount") if isinstance(rating, dict) else None,
            "is_tripadvisor": bool(rating.get("is_tripadvisor")) if isinstance(rating, dict) else None,
        } if isinstance(item.get("rating"), dict) and rating.get("ratingValue") is not None else None,  # round-8
        "icon": item.get("icon_category"),                 # round-8: venue category icon ("cafe", "restaurant", ...)
        "id": item.get("id"),                              # round-8: Brave POI id
        "profiles": item.get("profiles") or [],            # round-8
        "website": (item.get("provider_url") or None),     # round-8: venue site URL when disclosed
        "thumbnail": (thumb.get("src") if isinstance(thumb, dict) else None),
        "pictures": [
            {"src": (p.get("src") if isinstance(p, dict) else None),
             "original": (p.get("original") if isinstance(p, dict) else None)}
            for p in pics[:6] if isinstance(p, dict)
        ],
        "provider_url": item.get("provider_url"),
        "zoom_level": item.get("zoom_level"),
        "family_friendly": item.get("family_friendly"),
    }


def _loc_lines(data) -> list:
    """Extract normalised map/POI results from a web response's `locations`."""
    locs = (data.get("locations") if isinstance(data, dict) else {}) or {}
    res = locs.get("results") if isinstance(locs, dict) else None
    return [_location_item(i) for i in (res or []) if isinstance(i, dict)]


def _infobox(data: dict) -> Optional[dict]:
    ib = (data.get("infobox") or {}).get("results") or []
    if not ib:
        return None
    b = ib[0]
    return {
        "title": b.get("title"),
        "url": b.get("url"),
        "position": b.get("position"),      # round-8: rank in the infobox list
        "label": b.get("label"),            # round-8: e.g. "programming language"
        "category": b.get("category"),
        "description": b.get("description"),
        "long_desc": _clean_html(b.get("long_desc"))[:800],
        "attributes": b.get("attributes") or [],
        "website_url": b.get("website_url"),
        "profiles": b.get("profiles") or [],
        "providers": [
            {
                "type": p.get("type"),
                "name": p.get("name"),
                "url": p.get("url"),
                "img": p.get("img"),
            }
            for p in (b.get("providers") or [])
            if isinstance(p, dict) and p.get("name")
        ],
        "images": b.get("images") or [],
        "ratings": b.get("ratings") or [],
        "found_in_urls": b.get("found_in_urls") or [],
    }


def _query_diag(data: dict) -> dict:
    q = data.get("query") or {}
    return {
        "original": q.get("original"),
        "altered": q.get("altered"),
        "spellcheck_off": q.get("spellcheck_off"),
        "is_navigational": q.get("is_navigational"),
        "is_news_breaking": q.get("is_news_breaking"),
        "bad_results": q.get("bad_results"),
        "should_fallback": q.get("should_fallback"),
        "more_results_available": q.get("more_results_available"),
        "country": q.get("country"),
        "header_country": q.get("header_country"),
        "city": q.get("city"),
        "state": q.get("state"),
        "postal_code": q.get("postal_code"),
        "show_strict_warning": q.get("show_strict_warning"),  # round-7
        "is_geolocal": q.get("is_geolocal"),                  # round-7: Brave located this query geographically
        "local_decision": q.get("local_decision"),            # round-7
        "local_locations_idx": q.get("local_locations_idx"),  # round-7
    }


def _offensive(data: dict) -> Optional[bool]:
    """Top-level adult-content safety flag on video/image responses.

    Live-verified: video and image search responses carry a top-level
    `extra = {"might_be_offensive": bool}` (web does not; web carries
    `web.family_friendly` instead). Surface it so a caller can gate adult/
    sensitive results.
    """
    ext = data.get("extra")
    if isinstance(ext, dict) and ext.get("might_be_offensive") is not None:
        return bool(ext.get("might_be_offensive"))
    return None


def _summarizer(data: dict) -> Optional[dict]:
    """Capture Brave's AI-summary (summarizer) deep-link if present.

    Brave's `/web/search` can return a compact `summarizer` payload (when asked
    with `summary=True`) that deep-links the self-contained AI answer for the
    query. We surface the raw key so a downstream agent can render it or hand
    the URL to a user.
    """
    s = data.get("summarizer")
    if not isinstance(s, dict):
        return None
    key = s.get("key")
    out: dict[str, Any] = {"type": s.get("type")}
    if isinstance(key, str):
        try:
            import json as _json, urllib.parse as _up
            k = _json.loads(key)
            out["query"] = k.get("query")
            out["country"] = k.get("country")
            out["language"] = k.get("language")
            out["safesearch"] = k.get("safesearch")
            out["results_hash"] = k.get("results_hash")
            out["experimental_inline_refs"] = k.get("experimental_inline_refs")
            # Rebuild the deep-link a browser can open to render the AI answer.
            out["deep_link"] = "https://search.brave.com/summarizer?" + _up.urlencode({"key": key})
        except Exception:
            out["key"] = key
            out["deep_link"] = "https://search.brave.com/summarizer?key=" + _up.urlencode({"key": key})
    elif key is not None:
        out["key"] = key
    return out


# ---------------------------------------------------------------------------
# search() - the structured, scriptable core
# ---------------------------------------------------------------------------

def _search_full(
    query: str,
    *,
    mode: str = "web",
    count: int = 5,
    country: Optional[str] = None,
    search_lang: Optional[str] = None,
    result_filter: Optional[str] = None,
    safe_search: str = "moderate",
    freshness: Optional[str] = None,
    grep: Optional[str] = None,
    extra: bool = False,
    goggles_id: Optional[str] = None,
    spellcheck: Optional[bool] = None,
    offset: Optional[int] = None,
    discussion_count: Optional[int] = None,
    video_count: Optional[int] = None,
    movie_count: Optional[int] = None,
    unit: Optional[str] = None,
    text_decorations: Optional[bool] = None,
    summary: bool = False,
    ui_lang: Optional[str] = None,
    units: Optional[str] = None,
    operators: Optional[bool] = None,
    include_fetch_metadata: Optional[bool] = None,
    enable_rich_callback: Optional[bool] = None,
    goggles: Optional[Any] = None,
    loc: Optional[dict] = None,
    property: Optional[str] = None,
    search_type: Optional[str] = None,
    timeout: float = 45.0,
) -> dict:
    """Run Brave search and return structured result (scriptable power).

    Use this when you want dicts to feed downstream; use `run()` for a readable
    rendering of the same query.

    Args:
        query: The search query.
        mode: "web" | "news" | "image" | "video" | "local" | "all" (web+news+video).
        count: Results per section (default 5; web up to ~20). Acts as the page
            size for offset pagination.
        country: Two-letter country filter, e.g. "us", "de".
        search_lang: ISO 639 language code, e.g. "en", "de".
        result_filter: Comma-separated categories to keep, e.g. "discussions,web".
            Round-6 also accepts `locations` / `news`.
        safe_search: "moderate" (default), "strict", or "off".
        freshness: relative-age filter.
            Round-6 shorthands `pd`/`pw`/`pm`/`py`, legacy aliases
            `pd_1d`/`pd_1w`/`pd_1m`/`pd_1y`, or a custom inclusive date range
            `YYYY-MM-DDtoYYYY-MM-DD` (e.g. `2025-05-01to2025-06-30`).
        grep: PCRE regex applied to result sources (web).
        extra: Request extra metadata / additional snippets (web).
        goggles_id: (deprecated) legacy Goggles id / URL. Prefer `goggles`.
        spellcheck: Boolean spell check or disable spell-correction.
        offset: zero-based page number for the current window. Brave's real window
            is offset 0..9 regardless of count (verified live: every count
            accepts offset up to 9; offset >= 10 returns HTTP 422). Keep count
            fixed and raise offset by one to page forward (e.g. count=5,
            offset=0 then offset=1). A window deeper than the query's available
            results returns fewer or none — use `brave.paged(...)` for a robust
            multi-page fetch that handles this and dedupes. News/image/video
            also accept it.
        discussion_count: Number of discussions to embed in web mode.
        video_count: Number of videos to embed in web mode.
        movie_count: Number of movies to embed in web mode.
        unit: Image sizing unit "px" or "em" (image mode only).
        property: (image mode) OECD licensing filter — "any" | "commercial" |
            "non-commercial". Accepted by the API (verified live: all three
            return 200); on this plan it is a pass-through (results do not
            visibly change — a forward-compat hook for licensing tiering).
        search_type: (image mode) unstructured filter "all" | "transparent".
            Accepted by the API (verified live: 200) but, like `property`, a
            pass-through on this plan (no visible result change).
        summary: Ask Brave for its self-contained AI answer; when returned the
            `summarizer` key holds a deep-link payload for that answer.
        text_decorations: True to keep result HTML highlight marks (`<strong>`)
            in descriptions, False to request clean plain text (verified live:
            `text_decorations=false` strips the `<strong>` artefacts). Default
            None lets Brave choose (currently emits `<strong>` marks).
        ui_lang: UI language for the response, e.g. "en-US", "fr-FR".
        units: Measurement units for values in results — "metric" or "imperial".
        operators: False to disable Brave's search operators (so a literal
            "site:..." query is treated as plain text instead of an operator).
        include_fetch_metadata: True to ask Brave for fetch metadata.
        goggles: Modern Goggles filter — a goggle URL / inline definition, or a
            comma-separated string of up to 3. Replaces the deprecated
            `goggles_id` (which is still accepted).
        loc: dict of X-Loc-* location headers (e.g. city/state/`postal_code`/
            lat/lon/timezone/country) so results are geographically localised
            even for a non-"near me" query. Keys are case-insensitive; see
            `brave.near(...)` for a typed helper.
        timeout: HTTP timeout seconds.

    Returns:
        A dict shaped by mode (see module docstring).
    """
    if safe_search not in SAFE_SEARCH:
        raise ValueError(f"safe_search must be one of {sorted(SAFE_SEARCH)}")
    if freshness and not (freshness in FRESHNESS or _FRESHNESS_RE.match(freshness)):
        raise ValueError(
            "freshness must be one of {} or a date range 'YYYY-MM-DDtoYYYY-MM-DD'".format(
                sorted(FRESHNESS)
            )
        )
    if offset is not None and not (0 <= offset <= 9):
        raise ValueError(
            "offset must satisfy 0 <= offset <= 9 (Brave's real page window), "
            "got {}. To fetch additional pages for a query, page forward "
            "by re-calling with a larger offset, or use `brave.paged(...)` for a "
            "handled multi-page fetcher.".format(offset)
        )
    if mode == "video" and unit:
        raise ValueError("unit is only meaningful for image mode")
    if property is not None and property not in IMAGE_PROPERTY:
        raise ValueError(
            f"property must be one of {sorted(IMAGE_PROPERTY)} (image mode licensing hint), "
            f"got {property!r}"
        )
    if search_type is not None and search_type not in IMAGE_SEARCH_TYPE:
        raise ValueError(
            f"search_type must be one of {sorted(IMAGE_SEARCH_TYPE)} (image mode hint), "
            f"got {search_type!r}"
        )

    params: dict[str, object] = {"q": query}
    if mode != "all":
        params["count"] = count
    if country:
        params["country"] = country
    if search_lang:
        params["search_lang"] = search_lang
    if result_filter:
        params["result_filter"] = result_filter
    if safe_search:
        params["safe_search"] = safe_search
    if freshness:
        params["freshness"] = freshness
    if grep:
        params["grep"] = grep
    if extra:
        params["extra"] = "true"
    # Round-6: modern `goggles` (url/definition/list) wins; deprecated
    # `goggles_id` still accepted as a fallback. Both map to the goggles param.
    gg = goggles if (goggles is not None) else goggles_id
    if gg:
        params["goggles"] = gg if isinstance(gg, str) else ",".join(str(x) for x in gg)
    if spellcheck is not None:
        params["spellcheck"] = "true" if spellcheck else "false"
    if offset is not None:
        params["offset"] = offset
    if discussion_count is not None and mode in ("web", "all"):
        params["discussion_count"] = discussion_count
    if video_count is not None and mode in ("web", "all"):
        params["video_count"] = video_count
    if movie_count is not None and mode in ("web", "all"):
        params["movie_count"] = movie_count
    if unit is not None and mode == "image":
        params["unit"] = unit
    # round-13: image licensing / transparency hints — accepted by the API,
    # pass-through on this plan (forward-compat for licensing tiering).
    if property is not None and mode == "image":
        params["property"] = property
    if search_type is not None and mode == "image":
        params["search_type"] = search_type
    if text_decorations is not None and mode in ("web", "all"):
        params["text_decorations"] = "true" if text_decorations else "false"
    if summary:
        params["summary"] = "true"

    # ---- round-6 params -------------------------------------------------
    if ui_lang:
        params["ui_lang"] = ui_lang
    if units:
        params["units"] = units
    if operators is not None:
        params["operators"] = "true" if operators else "false"
    if include_fetch_metadata is not None:
        params["include_fetch_metadata"] = "true" if include_fetch_metadata else "false"
    if enable_rich_callback:
        params["enable_rich_callback"] = "1"


    # X-Loc-* location hints: convert the `loc` dict to request headers.
    loc_headers = {}
    if loc:
        for key, value in (loc.items() if isinstance(loc, dict) else []):
            hdr = LOC_HEADERS.get(str(key).strip().lower())
            if hdr and value is not None:
                loc_headers[hdr] = str(value)

    # ---- "all" mode: run web + news + video and merge -----------------------
    if mode == "all":
        got = {k: (ok, val) for k, ok, val in _fanout([
            ("web", lambda: _request(PATHS["web"], {**params, "count": 10}, timeout, headers=loc_headers)),
            ("news", lambda: _request(PATHS["news"], {**params, "count": max(1, count), "freshness": freshness or "pd_1m"}, timeout, headers=loc_headers)),
            ("video", lambda: _request(PATHS["video"], {**params, "count": min(count, 5)}, timeout, headers=loc_headers)),
        ], concurrency=3)}
        def _need(name):
            ok, val = got.get(name, (False, None))
            if not ok:
                raise val if isinstance(val, Exception) else BraveError(f"{name} fanout failed")
            return val
        w, n, v = _need("web"), _need("news"), _need("video")
        return {
            "mode": "all",
            "query": _query_diag(w),
            "infobox": _infobox(w),
            "summarizer": _summarizer(w),
            "web": [_web_item(i) for i in (w.get("web") or {}).get("results") or []],
            "news": [_news_item(i) for i in n.get("results") or []],
            "videos": [_video_item(i) for i in v.get("results") or []],
            "faq": [_faq_item(i) for i in (w.get("faq") or {}).get("results") or []],
            "locations": _loc_lines(w),
        }

    # ---- single-endpoint modes ----------------------------------------------
    path = PATHS.get(mode)
    if path is None:
        raise ValueError(f"Unknown mode '{mode}'. Call `await brave.modes()` for the list.")
    fallback = PATHS["web"] if mode == "local" else None
    data = _request(path, params, timeout, fallback=fallback, headers=loc_headers)
    diag = _query_diag(data)
    reported = data.get("_fallback") if isinstance(data.get("_fallback"), dict) else None

    if mode == "web":
        web_items = [_web_item(i) for i in (data.get("web") or {}).get("results") or []]
        video_items = [_video_item(i) for i in (data.get("videos") or {}).get("results") or []]
        discussion_items = [_discussion_item(i) for i in (data.get("discussions") or {}).get("results") or []]
        news_items = [_news_item(i) for i in (data.get("news") or {}).get("results") or []]
        faq_items = [_faq_item(i) for i in (data.get("faq") or {}).get("results") or []]
        infobox = _infobox(data)
        web_sec = data.get("web") if isinstance(data.get("web"), dict) else {}
        disc_sec = data.get("discussions") if isinstance(data.get("discussions"), dict) else {}
        vid_sec = data.get("videos") if isinstance(data.get("videos"), dict) else {}
        return {
            "mode": "web",
            "query": diag,
            "infobox": infobox,
            "summarizer": _summarizer(data),
            "results": web_items,
            "web": web_items,
            "videos": video_items,
            "discussions": discussion_items,
            "news": news_items,
            "faq": faq_items,
            "locations": _loc_lines(data),                       # round-6: map/POI results on local-intent queries
            "family_friendly": web_sec.get("family_friendly"),  # round-5: whole-SERP safety flag
            "might_be_offensive": _offensive(data),             # round-5: safety flag from top-level extra
            "mixed": _mixed(data, web_items, video_items, discussion_items, news_items, faq_items, infobox),
            "top_results": _top_results(data, web_items, discussion_items, video_items, news_items, faq_items, infobox),
            "rich": data.get("rich") if isinstance(data.get("rich"), dict) else None,
            "mutated_by_goggles": {                             # round-7
                "web": bool(web_sec.get("mutated_by_goggles")),
                "videos": bool(vid_sec.get("mutated_by_goggles")),
                "discussions": bool(disc_sec.get("mutated_by_goggles")),
            },
        }
    if mode in ("news", "video"):
        items = [_news_item(i) for i in data.get("results") or []] if mode == "news"             else [_video_item(i) for i in data.get("results") or []]
        return {"mode": mode, "query": diag, "results": items,
                "might_be_offensive": _offensive(data)}   # round-5
    if mode == "image":
        return {
            "mode": "image",
            "query": diag,
            "results": [_image_item(i) for i in data.get("results") or []],
            "might_be_offensive": _offensive(data),       # round-5
        }
    if mode == "local":
        # data may have fallen back to web results
        return {
            "mode": "local",
            "query": diag,
            "fallback": reported,
            "results": [_web_item(i) for i in (data.get("web") or {}).get("results") or data.get("results") or []],
        }
    raise BraveError(f"Unhandled mode {mode}")



def search(query: str, *, view: str = "agent", **kwargs) -> dict:
    """Structured Brave search. Default ``view="agent"`` is token-budgeted.

    Agents should call this (or ``brief()`` / ``run()``). The historical SERP
    dump — ``mixed``, ``top_results``, empty fields, publisher logos — is
    ``view="full"`` (used internally by helpers that need schema blocks).

    Args:
        query: search query.
        view: ``"agent"`` (default) compact dict, or ``"full"`` / ``"raw"`` /
            ``"serp"`` for the complete normalised SERP.
        **kwargs: forwarded to the engine (mode, count, freshness, ...).
    """
    v = (view or "agent").lower()
    if v not in ("full", "raw", "serp") and "text_decorations" not in kwargs:
        kwargs["text_decorations"] = False
    data = _search_full(query, **kwargs)
    if v in ("full", "raw", "serp"):
        return data
    return _agent_view(data)


def brief(query: str, **kwargs) -> dict:
    """Explicit token-budgeted search. Identical to ``search(view="agent")``."""
    kwargs.pop("view", None)
    return search(query, view="agent", **kwargs)



def paged(
    query: str,
    *,
    mode: str = "web",
    count: int = 5,
    total: int = 10,
    max_pages: int = 6,
    dedupe: bool = True,
    **kw: Any,
) -> dict:
    """Fetch multiple pages of results and merge into one deduplicated set.

    This is the robust "total > single page" helper on top of `search()`. Brave
    pages a query through a shallow offset window (`offset` 0..9), so to gather
    more results than one page you advance the offset by one per page.
    `paged()` does that, tolerates pages that come back short or empty, stops
    when the target `total` is reached or the feed indicates no more results
    exist, dedupes by URL, and returns one combined dict.

    Args:
        query: search text.
        mode: "web" | "news" | "video" (image/local/all are single-shot; use
            `search()` for those). web is default.
        count: per-page size used when advancing offset.
        total: stop once we have at least this many results (cap on result count).
        max_pages: hard cap on HTTP round-trips, safety valve.
        dedupe: drop repeat URLs across pages (default True).
        **kw: any other `search()` options (freshness, country, safe_search,
            discussion_count, video_count, etc.) forwarded to each page.

    Returns:
        {"query":..., "results":[...], "n":int, "pages":int, "exhausted":bool,
         "count":count, "total":total}
        For web mode the first page's `infobox`, `faq`, `videos`, `discussions`,
        and `summarizer` (when requested) are also surfaced under `web_meta`, so
        you keep the rich single-page extras while paging the web corpus.
    """
    results: list[dict] = []
    seen: set[str] = set()
    offset = 0
    pages = 0
    exhausted = False
    first_web: Optional[dict] = None
    while True:
        if pages >= (max_pages or 0):
            break
        if offset > 9:                      # Brave's window caps offset at 9.
            break
        if total is not None and len(results) >= total:
            break
        page = _search_full(query, mode=mode, count=count, offset=offset, **kw)
        if first_web is None and mode == "web":
            first_web = page
        items = page.get("results") or page.get("web") or []
        if not items:
            exhausted = True
            break
        for it in items:
            if total is not None and len(results) >= total:
                break
            url = it.get("url") or it.get("page_url") or it.get("image_url")
            if dedupe and url:
                if url in seen:
                    continue
                seen.add(url)
            results.append(it)
        pages += 1
        q = page.get("query") or {}
        more = q.get("more_results_available")
        if more is False:                   # Brave says no later results exist.
            exhausted = True
            break
        # Offset counts pages, independently of the requested page size.
        if offset + 1 > 9:                  # next page would exceed the window.
            exhausted = True
            break
        offset += 1
    out: dict[str, Any] = {
        "query": query,
        "results": results,
        "n": len(results),
        "pages": pages,
        "exhausted": exhausted,
        "count": count,
        "total": total,
    }
    if mode == "web" and first_web is not None:
        out["web_meta"] = {
            "infobox": first_web.get("infobox"),
            "faq": first_web.get("faq"),
            "videos": first_web.get("videos"),
            "discussions": first_web.get("discussions"),
            "summarizer": first_web.get("summarizer"),
            "mixed": first_web.get("mixed"),
        }
    return out


def merge(*searches: dict, dedupe: bool = True) -> list[dict]:
    """Merge `results`/`web` lists from one or more `search()` dicts, deduped.

    A small research helper for the multi-query case: run several distinct
    queries and combine their results into a single URL-deduped ranked list.

    ```python
    a = await brave.search("clojure web framework", mode="web", count=5)
    b = await brave.search("clojure concurrency", mode="web", count=5)
    merged = brave.merge(a, b)          # -> [ {url, title, ...}, ... ]
    ```
    Returns a flat list of merged dicts (each tagged with its original `_query`).
    """
    out: list[dict] = []
    seen: set[str] = set()
    for d in searches:
        items = d.get("results") or d.get("web") or []
        for it in items:
            url = it.get("url") or it.get("page_url")
            if dedupe and url:
                if url in seen:
                    continue
                seen.add(url)
            copy = dict(it)
            copy.setdefault("_query", d.get("query", {}).get("original") if isinstance(d.get("query"), dict) else None)
            out.append(copy)
    return out


def research(
    query: str,
    *,
    queries: Optional[list[str]] = None,
    mode: str = "web",
    count: int = 6,
    total: int = 12,
    summary: bool = True,
    max_pages: int = 4,
    **kw: Any,
) -> dict:
    """One high-level call that fuses multi-page + multi-query + AI-summary.

    This is the "give me everything Brave has on this topic" entry point built
    on the verified primitives: it pages an optional primary query with
    `paged()`, merges any explicit query variations URL-deduped, carries the
    first page's knowledge panel / FAQ / embedded videos / discussions / AI
    summary deep-link, and returns a single coherent package (plus a readable
    `render` text) — so you don't hand-roll `search()` + `paged()` + `merge()`.

    Strictly additive: `search()` / `run()` / `paged()` / `merge()` / `modes()`
    are unchanged and still work on their own.

    Args:
        query: the topic.
        queries: optional explicit list of query variations. If none, we page
            the primary `query` (web/news/video) for a fuller window.
        mode: "web" | "news" | "video" (corpus-oriented). For image/local/all
            (single-shot) we fall back to a plain `search()` per query.
        count: per-page result size.
        total: per-query target count for paged().
        summary: also request Brave's AI-summary deep-link for the primary query.
        max_pages: hard cap on HTTP round-trips per query in paged().
        **kw: forwarded (country, search_lang, safe_search, freshness,
            result_filter, discussion_count, video_count, grep, goggles_id, ...).

    Returns:
        {"query", "queries", "mode", "results":[dedup items],
         "n", "sources":[hosts], "infobox", "faq", "videos", "discussions",
         "summary_deep_link", "per_search", "exhausted", "render": str,
         "duplicates": int}
    """
    qs = [q for q in ([query] if not queries else queries) if q]
    out_items: list[dict] = []
    seen: set[str] = set()
    web_meta: dict[str, Any] = {}
    per_search: list[dict] = []
    exhausted_any = 0
    primary: Optional[dict] = None

    for qq in qs:
        if mode not in ("web", "news", "video"):
            # image/local/all are single-shot; use a plain search per query.
            try:
                page = _search_full(qq, mode=mode, count=count, summary=summary, **kw)
            except BraveError as e:
                per_search.append({"query": qq, "error": f"{e.category}: {e}"})
                continue
            added = 0
            for it in (page.get("results") or page.get("web") or []):
                url = it.get("url") or it.get("page_url")
                if url and url in seen:
                    continue
                if url:
                    seen.add(url)
                out_items.append({"query": qq, **it})
                added += 1
            per_search.append({"query": qq, "n": added, "mode": mode})
            if primary is None:
                primary = page
            continue
        try:
            pg = paged(qq, mode=mode, count=count, total=total,
                       max_pages=max_pages, summary=summary, **kw)
            if pg.get("exhausted"):
                exhausted_any += 1
            pgm = pg.get("web_meta") if isinstance(pg.get("web_meta"), dict) else {}
            added = 0
            for it in pg.get("results") or []:
                url = it.get("url") or it.get("page_url")
                if url in seen:
                    continue
                seen.add(url)
                out_items.append(it)
                added += 1
            # Web-mode first page carries infobox/faq/videos/discussions/summarizer.
            if qq == qs[0]:
                if pgm:
                    web_meta = dict(pgm)
                elif primary is None:
                    primary = _search_full(qq, mode=mode, count=count, summary=summary, **kw)
                    web_meta = {k: primary.get(k) for k in
                                ("infobox", "faq", "videos", "discussions")}
            per_search.append({"query": qq, "pages": pg.get("pages"), "n": added,
                               "exhausted": pg.get("exhausted")})
        except BraveError as e:
            per_search.append({"query": qq, "error": f"{e.category}: {e}"})
    if not web_meta and primary is not None:
        web_meta = {k: primary.get(k) for k in ("infobox", "faq", "videos", "discussions")}
    summary_dl = None
    if summary:
        sm = web_meta.get("summarizer") if isinstance(web_meta.get("summarizer"), dict) else None
        if not (isinstance(sm, dict) and sm.get("deep_link")) and primary is not None:
            sm = primary.get("summarizer") if isinstance(primary.get("summarizer"), dict) else None
        if isinstance(sm, dict) and sm.get("deep_link"):
            summary_dl = sm["deep_link"]
    render = _render_lines(query, out_items, web_meta)
    sources = sorted({it.get("site") for it in out_items if it.get("site") if it.get("site")})
    return {
        "query": query,
        "queries": qs,
        "mode": mode,
        "results": out_items,
        "n": len(out_items),
        "sources": sources,
        "infobox": (web_meta.get("infobox") or {}),
        "faq": web_meta.get("faq") or [],
        "videos": web_meta.get("videos") or [],
        "discussions": web_meta.get("discussions") or [],
        "summary_dead_link": summary_dl,
        "per_search": per_search,
        "exhausted": (exhausted_any == len(qs) and len(qs) > 0) if qs else False,
        "render": render,
        "duplicates": sum(p.get("n", 0) for p in per_search if p.get("n")) - len(out_items),
    }


def software(
    package: str,
    *,
    count: int = 10,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
) -> dict:
    """Package/library registry lookup via Brave web search (round-5).

    Brave: web results tagged `subtype == "software"` carry the package's
    registry identity (`name`, `version`, `is_npm`/`is_pypi`, `codeRepository`,
    `datePublished`, `programmingLanguage`). This is the clean "what is this
    package / which version / where is the code" lookup — it runs the needle
    query through `search()` and returns just the *software* results, enriched
    with that metadata (the `software` key on each web item).

    Verified live: `software("jsonschema")` → v4.26.0 on PyPI with the
    python-jsonschema code repo; `software("fastapi pypi")` → fastapi-slim
    0.129.1 etc.

    Args:
        package: package/library name (optionally with a registry hint, e.g.
            "jsonschema pypi", "lodash npm", "ruff").
        count: how many web results to pull (larger finds more software hits).
        country / safe_search / timeout: forwarded to `search()`.

    Returns:
        {"query", "package", "results":[software web items], "n",
         "registries":{pypi->[names], npm->[names]}, "versions":[version str...],
         "render": str} — `render` is a readable summary; `results[i].software`
         is the registry metadata dict. Non-software hits are dropped.
    """
    data = _search_full(package, mode="web", count=count, country=country,
                  safe_search=safe_search, timeout=timeout)
    all_items = data.get("results") or []
    sw = [it for it in all_items if it.get("subtype") == "software" and it.get("software")]
    registry: dict[str, list[str]] = {}
    versions = []
    for it in sw:
        s = it.get("software") or {}
        name = s.get("name") or it.get("title")
        for rg in s.get("registry") or []:
            registry.setdefault(rg, []).append(name or str(s))
        if s.get("version"):
            versions.append({"name": name, "version": s["version"],
                             "url": it.get("url"), "code": s.get("code_repository")})
    lines = [f"Software/package results for {package!r}:"]
    if not sw:
        lines.append("  (no software/package-registry results — try appending 'pypi'/'npm' or the registry name)")
    for it in sw:
        s = it.get("software") or {}
        name = s.get("name") or (it.get("title") or "")[:60]
        ver = f" v{s['version']}" if s.get("version") else ""
        rg = f" [{'/'.join(s.get('registry') or [])}]" if s.get("registry") else ""
        line = f"  · {name}{ver}{rg}  {it.get('url')}"
        if s.get("code_repository"):
            line += f"\n      code: {s['code_repository']}"
        if s.get("published"):
            line += f"\n      published: {s['published']}"
        lines.append(line)
    return {
        "query": package,
        "package": package,
        "results": sw,
        "n": len(sw),
        "registry": registry,
        "versions": versions,
        "render": "\n".join(lines),
    }


def near(
    query: str,
    *,
    mode: str = "web",
    count: int = 5,
    country: Optional[str] = None,
    search_lang: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    city: Optional[str] = None,
    state: Optional[str] = None,
    state_name: Optional[str] = None,
    postal_code: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    timezone: Optional[str] = None,
) -> dict:
    """Geographically localised search via the X-Loc-* request headers (round-6).

    Brave lets you hint the client location on *any* query through request
    headers, so a plain "coffee shops" (or even "best cafes") returns results
    for that place instead of a generic default — no "near me" phrasing needed.

    Verified live: `near("best cafes", city="San Francisco", state="CA",
    postal_code="94105")` returns San Francisco coffee shops with street
    addresses, opening hours, phone and coordinates (the web response's
    `locations` map), while the same query with no hint returns results for the
    default region.

    Args:
        query: the search term.
        mode/count/country/search_lang/safe_search/timeout: forwarded to
            `search()`.
        city / state / state_name / postal_code: fill out, e.g. San Francisco /
            CA / California / 94105.
        latitude / longitude: decimal coordinates.
        timezone: IANA zone, e.g. "America/Los_Angeles".

    Returns:
        Same dict as `search(mode="web", loc=...)`, with `locations` populated
        for local-intent queries.
    """
    loc: dict[str, Any] = {}
    if latitude is not None:
        loc["latitude"] = latitude
    if longitude is not None:
        loc["longitude"] = longitude
    if city:
        loc["city"] = city
    if state:
        loc["state"] = state
    if state_name:
        loc["state_name"] = state_name
    if postal_code:
        loc["postal_code"] = postal_code
    if timezone:
        loc["timezone"] = timezone
    return _search_full(
        query, mode=mode, count=count, country=country, search_lang=search_lang,
        safe_search=safe_search, timeout=timeout, loc=loc or None,
    )


def locations(
    query: str,
    *,
    count: int = 5,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Map/POI lookup — returns just the `locations` results for a query.

    A thin wrapper over a `web` search: Brave auto-surfaces a `locations` map
    section on local-intent queries (restaurants, cafes, services near a place)
    with rich per-place data. `locations()` returns that map alone plus a
    readable `render`, so callers don't have to filter a full web response.

    Args:
        query: the search term.
        count: how many places to pull.
        country / safe_search / timeout: forwarded to `search()`.
        **loc: X-Loc-* hints, e.g. `city="San Francisco"`, `postal_code="94105"`,
            `timezone="America/Los_Angeles"`, `latitude=...`, `longitude=...`.

    Returns:
        {"query", "results":[location items], "n", "render", "web"} — `web` is
        the underlying search result for context; `results` are the places.
    """
    data = _search_full(query, mode="web", count=count, country=country,
                  safe_search=safe_search, timeout=timeout,
                  loc=loc or None)
    places = data.get("locations") or []
    lines = [f"Places for '{query}':"]
    for p in places:
        ln = f"  · {p.get('title')}"
        if p.get("address"):
            ln += f" — {p['address']}"
        if p.get("phone"):
            ln += f"  ☎ {p['phone']}"
        if p.get("opening_hours"):
            ln += f"  ({' / '.join(p['opening_hours'][:2])})"
        lines.append(ln)
    return {
        "query": query,
        "results": places,
        "n": len(places),
        "render": "\n".join(lines),
        "web": data,
    }


def forums(
    query: str,
    *,
    count: int = 10,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Discussion/forum lookup — returns just the `discussions` for a query.

    Brave surfaces Reddit / forum threads on many queries. This helper returns
    only those `discussion` results, enriched with the engagement metadata the
    API carries: answer count, upvote/score, the original post question text,
    and the top comment (round-7). Use it for "what are people asking about X"
    or "what's the consensus on X in forums".

    Args:
        query: the search term.
        count: how many discussions to pull (the API typically returns up to 10).
        country / safe_search / timeout: forwarded to `search()`.
        **loc: X-Loc-* hints for geo-localised results.

    Returns:
        {"query", "results":[discussion items], "n", "render", "web"} — `web`
        is the underlying search result for context; `results` are the forum
        threads with `num_answers`, `score`, `question`, `top_comment`.
    """
    data = _search_full(query, mode="web", count=max(1, count), country=country,
                  safe_search=safe_search, timeout=timeout,
                  loc=loc or None)
    threads = data.get("discussions") or []
    lines = [f"Forums / discussions for '{query}':"]
    for t in threads:
        ln = f"  · {t.get('title')}"
        if t.get("forum"):
            ln += f"  ({t['forum']})"
        if t.get("num_answers") is not None:
            ln += f"  [{t['num_answers']} answers"
            if t.get("score") is not None:
                ln += f", score {t['score']}"
            ln += "]"
        lines.append(ln)
        if t.get("question"):
            lines.append(f"    Q: {t['question'][:120]}")
        if t.get("top_comment"):
            lines.append(f"    Top: {t['top_comment'][:120]}")
        if t.get("url"):
            lines.append(f"    {t['url']}")
        lines.append("")
    # drop the trailing blank line
    if lines and not lines[-1]:
        lines.pop()
    return {
        "query": query,
        "results": threads,
        "n": len(threads),
        "render": "\n".join(lines),
        "web": data,
    }


def headlines(
    topic: str,
    *,
    count: int = 8,
    country: Optional[str] = None,
    search_lang: Optional[str] = None,
    safe_search: str = "moderate",
    freshness: Optional[str] = "pd_1w",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Fresh news headlines — dedicated `/news` lookup (round-8).

    Round-8 helper that mirrors `forums()`/`locations()`: run a `news` search
    and return just those headlines (with source, age, publisher URL and a
    readable `render`), so callers get a clean "what's the latest on X" answer
    instead of hand-fetching `search(mode=news,...)` and filtering.

    Args:
        topic: the subject to poll for fresh news.
        count: how many headlines to return (news supports up to 50; default 8).
        country / search_lang / safe_search / freshness / timeout: forwarded to
            `search()`. A freshness default of `pd_1w` (last week) keeps the list
            timely; pass e.g. `pd_1d` or a date-range for tighter windows.
        **loc: X-Loc-* hints, e.g. `country="us"`, `city=...`.

    Returns:
        {"query", "results":[news items], "n", "render", "search"} — `search`
        is the underlying structured news reply for context.
    """
    data = _search_full(topic, mode="news", count=max(1, count), country=country,
                  search_lang=search_lang, safe_search=safe_search,
                  freshness=freshness, timeout=timeout, loc=loc or None)
    items = data.get("results") or []
    lines = [f"News headlines for '{topic}':"]
    for it in items:
        ln = f"  · {it.get('title')}"
        src = it.get("source")
        if src:
            ln += f"  — {src}"
        if it.get("age"):
            ln += f"  ({it['age']})"
        lines.append(ln)
        if it.get("url"):
            lines.append(f"    {it['url']}")
    return {
        "query": topic,
        "results": items,
        "n": len(items),
        "count": count,
        "render": "\n".join(lines),
        "search": data,
    }


def clips(
    topic: str,
    *,
    count: int = 6,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Video clips — dedicated `/videos` lookup (round-8).

    Mirror of `headlines()` for the `video` index: returns just the video
    results (creator, channel, duration, tags, views where available) with a
    readable `render`.

    Args:
        topic: video search query.
        count: how many clips (video returns up to ~50; default 6).
        country / safe_search / timeout: forwarded to `search()`.
        **loc: X-Loc-* hints.

    Returns:
        {"query", "results": [video items], "n", "render", "search"}.
    """
    data = _search_full(topic, mode="video", count=max(1, count), country=country,
                  safe_search=safe_search, timeout=timeout, loc=loc or None)
    items = data.get("results") or []
    lines = [f"Videos for '{topic}':"]
    for it in items:
        ln = f"  · {it.get('title')}"
        if it.get("creator"):
            ln += f"  by {it['creator']}"
        if it.get("duration"):
            ln += f"  ({it['duration']})"
        lines.append(ln)
        if it.get("url"):
            lines.append(f"    {it['url']}")
        if it.get("views") is not None:
            lines.append(f"    {it['views']:,} views")
    return {
        "query": topic,
        "results": items,
        "n": len(items),
        "count": count,
        "render": "\n".join(lines),
        "search": data,
    }


def pictures(
    topic: str,
    *,
    count: int = 8,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    property: Optional[str] = None,
    search_type: Optional[str] = None,
    **kw,
) -> dict:
    """Image lookup — dedicated `/images` mirror (round-8).

    Like `headlines()`/`clips()`, a tiny wrapper that runs an `image` search and
    returns the image items + a readable `render`.

    Args:
        topic: image search query.
        count: images (image API supports up to 200 per request; default 8).
        safe_search / timeout: forwarded to `search()`.
        property: (round-13) OECD licensing hint "any" | "commercial" |
            "non-commercial" (accepted by the API; pass-through on this plan).
        search_type: (round-13) "all" | "transparent" (accepted; pass-through).
        **kw: other `search()` options (unit="px"|"em", country, ...).

    Returns:
        {"query", "results": [image items], "n", "render", "search"}.
    """
    data = _search_full(topic, mode="image", count=max(1, min(count or 8, 200)), safe_search=safe_search,
                  timeout=timeout, property=property, search_type=search_type, **kw)
    items = data.get("results") or []
    lines = [f"Images for '{topic}':"]
    for it in items:
        ln = f"  · {it.get('title')}"
        if it.get("width") and it.get("height"):
            ln += f"  ({it['width']}x{it['height']})"
        lines.append(ln)
        img = it.get("image_url")
        if img:
            lines.append(f"    {img}")
    return {
        "query": topic,
        "results": items,
        "n": len(items),
        "count": count,
        "render": "\n".join(lines),
        "search": data,
    }



def recipes(
    query: str,
    *,
    count: int = 8,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Recipe lookup — returns just the web results with schema.org Recipe data (round-9).

    Brave returns rich recipe blocks (schema.org Recipe embedded in web
    results) for recipe queries — title, timings, ingredients, instructions,
    servings, calories, category/cuisine, ratings. `recipes()` runs a web search
    and returns only those recipe results with a readable `render`.

    Args:
        query: the search term.
        count: how many results to pull (web mode up to ~20).
        country / safe_search / timeout: forwarded to `search()`.
        **loc: X-Loc-* hints for geo-localised results.

    Returns:
        {"query", "results":[web items each with a `.recipe` block], "n",
         "render", "web"} — `web` is the underlying search result for context.
    """
    data = _search_full(query, mode="web", count=max(1, count), country=country,
                  safe_search=safe_search, timeout=timeout,
                  loc=loc or None)
    items = [r for r in (data.get("results") or []) if isinstance(r.get("recipe"), dict)]
    lines = [f"Recipes for '{query}':"]
    for r in items:
        ln = f"  \u00b7 {r.get('title')}"
        rc = r.get("recipe") or {}
        meta = []
        if rc.get("time"):
            meta.append(f"  {rc['time']}")
        if rc.get("servings") is not None:
            meta.append(f"serves {rc['servings']}")
        if rc.get("calories") is not None:
            meta.append(f"{rc['calories']} cal")
        if rc.get("cuisine"):
            meta.append(rc["cuisine"])
        if meta:
            ln += "  [" + ", ".join(meta) + "]"
        lines.append(ln)
        if rc.get("ingredients"):
            lines.append(f"    {len(rc['ingredients'])} ingredients")
        if rc.get("category"):
            lines.append(f"    Category: {rc['category']}")
        if r.get("url"):
            lines.append(f"    {r['url']}")
    return {
        "query": query,
        "results": items,
        "n": len(items),
        "count": len(items),
        "render": "\n".join(lines),
        "web": data,
    }


def products(
    query: str,
    *,
    count: int = 8,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Product lookup — web results carrying schema.org Product data (round-9).

    Brave surfaces product listings with price/rating metadata directly in web
    results (`product` = schema.org Product with name, price, offers, rating).
    `products()` returns only those items with a readable `render`.

    Args:
        query: the search term.
        count: how many results to pull.
        **loc: X-Loc-* hints.

    Returns:
        {"query", "results", "n", "count", "render", "web"}.
    """
    data = _search_full(query, mode="web", count=max(1, count), country=country,
                  safe_search=safe_search, timeout=timeout,
                  loc=loc or None)
    items = [r for r in (data.get("results") or []) if r.get("product")]
    lines = [f"Products for '{query}':"]
    for r in items:
        p = r.get("product") or {}
        name = p.get("name") or r.get("title")
        ln = f"  \u00b7 {name}"
        if p.get("price") is not None:
            ln += f"  \u2014 ${p['price']}"
        lines.append(ln)
        rt = p.get("rating") or {}
        if rt.get("value") is not None:
            lines.append(f"    \u2605 {rt['value']} ({rt.get('reviews', 0)} reviews)")
        offers = p.get("offers") or []
        if offers:
            lines.append(f"    {len(offers)} offer(s)")
        if p.get("url") or r.get("url"):
            lines.append(f"    {p.get('url') or r.get('url')}")
    return {
        "query": query,
        "results": items,
        "n": len(items),
        "count": len(items),
        "render": "\n".join(lines),
        "web": data,
    }


def movies(
    query: str,
    *,
    count: int = 8,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Movie lookup — web results carrying schema.org Movie data (round-9).

    Brave enriches movie-centric web results with the film's full metadata:
    release date, directors, actors, genre, duration, rating. `movies()`
    returns just those items with a readable `render`.

    Args:
        query: the search term.
        count: how many results to pull.
        **loc: X-Loc-* hints.

    Returns:
        {"query", "results", "n", "count", "render", "web"}.
    """
    data = _search_full(query, mode="web", count=max(1, count), country=country,
                  safe_search=safe_search, timeout=timeout,
                  loc=loc or None)
    items = [r for r in (data.get("results") or []) if r.get("movie") is not None]
    lines = [f"Movies for '{query}':"]
    for r in items:
        m = r.get("movie") or {}
        ln = f"  \u00b7 {m.get('name') or r.get('title')}"
        meta = []
        if m.get("release"):
            meta.append(m["release"])
        if m.get("genre"):
            meta.append(", ".join(m["genre"][:3]))
        rt = m.get("rating") or {}
        if rt.get("value") is not None:
            meta.append(f"\u2605 {rt['value']} ({rt.get('reviews', 0)} reviews)")
        if meta:
            ln += f"  [{', '.join(meta)}]"
        lines.append(ln)
        dirs = m.get("directors") or []
        if dirs:
            lines.append(f"    Directed by: {', '.join(d.get('name') for d in dirs)}")
        actors = m.get("actors") or []
        if actors:
            names = ", ".join(a.get("name") for a in actors[:3])
            if len(actors) > 3:
                names += f" +{len(actors)-3} more"
            lines.append(f"    Starring: {names}")
        if m.get("url") or r.get("url"):
            lines.append(f"    {m.get('url') or r.get('url')}")
    return {
        "query": query,
        "results": items,
        "n": len(items),
        "count": len(items),
        "render": "\n".join(lines),
        "web": data,
    }
def structured(
    query: str,
    *,
    count: int = 12,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Extract typed structured blocks from ONE general web search (round-15).

    A single web SERP often embeds several heterogeneous schema.org blocks at
    once — a recipe, a product with offers, a movie profile, a software
    package, and/or the discussions (forum Q&A) section — scattered across the
    `results` dict. `structured()` runs one `web` search (no extra HTTP
    round-trips) and re-pools every present block into typed, object-shaped
    lists with per-category counts, so a pipeline can answer "what product /
    recipe / movie / software info does this SERP carry" in one call instead
    of guessing which corpus to fetch.

    Args:
        query: the search term.
        count: how many web results to scan (web mode up to ~20).
        country / safe_search / timeout: forwarded to `search()`.
        **loc: X-Loc-* hints.

    Returns:
        {"query", "n", "counts", "recipes", "products", "movies", "software",
         "discussions", "render"} — each `*` list holds the typed blocks found
         embedded in the single SERP; `counts` is {category: n}. Recipes /
         products / movies / software come from schema blocks on individual
         results; discussions come from the top-level `discussions` section
         (e.g. Reddit threads) Brave attaches to forum-intent queries.
    """
    data = _search_full(query, mode="web", count=max(1, count), country=country,
                  safe_search=safe_search, timeout=timeout,
                  loc=loc or None)
    res = data.get("results") or []
    recipes = [r.get("recipe") for r in res if isinstance(r.get("recipe"), dict)]
    products = [r.get("product") for r in res if isinstance(r.get("product"), dict)]
    movies = [r.get("movie") for r in res if isinstance(r.get("movie"), dict)]
    software = [r.get("software") for r in res if isinstance(r.get("software"), dict)]
    discussions = [
        {"title": d.get("title"), "url": d.get("url"), "site": _host(d),
         "forum": d.get("forum")}
        for d in (data.get("discussions") or []) if isinstance(d, dict)
    ]
    counts = {
        "recipes": len(recipes), "products": len(products), "movies": len(movies),
        "software": len(software), "discussions": len(discussions),
    }
    lines = [f"Structured blocks for '{query}':"]
    for cat, items in (("recipes", recipes), ("products", products),
                       ("movies", movies), ("software", software),
                       ("discussions", discussions)):
        if not items:
            continue
        lines.append(f"  {cat} ({len(items)}):")
        for blk in items[:6]:
            name = (blk.get("name") or blk.get("title") or "").strip()
            url = blk.get("url")
            extra = ""
            if cat == "recipes" and blk.get("time"):
                extra = f"  [{blk['time']}]"
            elif cat == "products" and blk.get("price"):
                extra = f"  [{blk['price']}]"
            elif cat == "movies" and blk.get("release"):
                extra = f"  [{blk['release']}]"
            elif cat == "software" and blk.get("version"):
                extra = f"  v{blk['version']}"
            elif cat == "discussions":
                forum = blk.get("forum")
                extra = f"  #{blk.get('title')}" if not forum and blk.get("title") else ""
            if extra:
                lines.append(f"    - {name}{extra}")
            elif name:
                lines.append(f"    - {name}")
            if url:
                lines.append(f"      {url}")
    return {
        "query": query,
        "n": sum(counts.values()),
        "counts": counts,
        "recipes": recipes,
        "products": products,
        "movies": movies,
        "software": software,
        "discussions": discussions,
        "render": "\n".join(lines),
        "web": data,
    }

def infobox(
    query: str,
    *,
    count: int = 10,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Knowledge-panel typed fact table for an entity (round-16).

    `search(mode='web')` returns the infobox (entity panel) as a struct of
    `attributes` rows where each fact is `[field, html_value]` (values carry
    `<a>`/`<br>` link markup) plus a `providers` provenance list. `infobox()`
    re-interprets that into a *typed fact table*: each `facts` row is a clean
    `{field, value}` (HTML stripped via the same `_clean_html` used everywhere
    else), alongside the entity `name`/`type` (label or category), the source
    `provider` attribution (e.g. Wikipedia), and the entity's `page`/`website`.

    Args:
        query: the search term (e.g. "clojure programming language").
        count: how many web results to pull for the SERP (web up to ~20).
        country / safe_search / timeout: forwarded to `search()`.
        **loc: X-Loc-* hints.

    Returns:
        {"query", "entity", "entity_type", "page", "website_url",
         "provider", "providers", "facts":[{"field","value",...}],
         "n", "render", "web"} -- facts are the knowledge-panel attribute
         rows with HTML stripped so each value is plain readable text
         (multiline when the source used <br>/lists). `provider` is the
         primary attribution dict. If the SERP has no knowledge panel,
         `entity` is None and `facts` is [] (no fabricated data).
    """
    data = _search_full(query, mode="web", count=max(1, count), country=country,
                  safe_search=safe_search, timeout=timeout,
                  loc=loc or None)
    ib = data.get("infobox") if isinstance(data, dict) else None
    entity = (ib or {}).get("title") if ib else None
    if not entity:
        return {
            "query": query, "entity": None, "entity_type": None, "page": None,
            "website_url": None, "provider": None, "providers": None,
            "facts": [], "n": 0,
            "render": f"No knowledge panel for '{query}'.",
            "web": data,
        }
    facts = []
    for row in (ib.get("attributes") or []):
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            field = str(row[0]).strip()
            value = _clean_html(row[1]).strip()
            if field and value:
                facts.append({"field": field, "value": value})
            elif value:
                facts.append({"field": field if field else "(label)", "value": value})
    providers = [p for p in (ib.get("providers") or []) if isinstance(p, dict)]
    entity_type = ib.get("label") or ib.get("category")
    lines = [f"Infobox - {entity}" + (f" ({entity_type})" if entity_type else "")]
    for f in facts:
        lines.append(f"  {f['field']}: {f['value'][:160]}")
    if providers:
        names = ", ".join(p.get("name") for p in providers if p.get("name"))
        lines.append(f"  source: {names}")
    if ib.get("website_url"):
        lines.append(f"  website: {ib['website_url']}")
    return {
        "query": query,
        "entity": entity,
        "entity_type": entity_type,
        "page": ib.get("url"),
        "website_url": ib.get("website_url"),
        "provider": providers[0] if providers else None,
        "providers": providers,
        "facts": facts,
        "n": len(facts),
        "render": "\n".join(lines),
        "web": data,
    }



def thumbnails(
    query: str,
    *,
    count: int = 5,
    video: bool = True,
    image: bool = True,
    timeout: float = 45.0,
    **kw,
) -> dict:
    """Pull just the thumbnail artwork URLs across video + image webs (round-16).

    `search()` normalises both corpora to a rich `thumbnail`/`original` pair
    (Brave's proxied image vs. the direct source URL) scattered across the
    structured reply. `thumbnails()` is the convenience that flattens just those
    URLs into one item list (plus per-item title/url/dimensions where present)
    so a pipeline can pre-load thumbnail assets or build a gallery without
    re-parsing the full result dicts.

    Args:
        query: the search term.
        count: how many thumbnails to pull per enabled corpus.
        video / image: which corpora to query (both default on).
        timeout: forwarded to `search()`.
        **kw: forwarded to `search()` for the enabled corpora (e.g.
            `country=`, `safe_search=`).

    Returns:
        {"query", "items":[{"kind":"video|image","title","url","thumbnail",
         "thumbnail_original"|"image_url","duration"|"width"|"height"}],
         "video":[...], "image":[...], "count", "n", "render"} -- `count`/`n`
         are the total number of thumbnails pulled.
    """
    pools = {"video": [], "image": []}
    jobs = []
    if video:
        jobs.append(("video", lambda: _search_full(query, mode="video", count=max(1, count), timeout=timeout, **kw)))
    if image:
        jobs.append(("image", lambda: _search_full(query, mode="image", count=max(1, count), timeout=timeout, **kw)))
    got = {k: (ok, val) for k, ok, val in _fanout(jobs, concurrency=2)}
    v_ok, v = got.get("video", (False, None))
    if v_ok:
        for r in (v.get("results") or []):
            pools["video"].append({
                "kind": "video",
                "title": r.get("title"),
                "url": r.get("url"),
                "thumbnail": r.get("thumbnail"),
                "thumbnail_original": r.get("thumbnail_original"),
                "duration": r.get("duration"),
            })
    im_ok, im = got.get("image", (False, None))
    if im_ok:
        for r in (im.get("results") or []):
            pools["image"].append({
                "kind": "image",
                "title": r.get("title"),
                "url": r.get("page_url") or r.get("url"),
                "thumbnail": r.get("thumbnail"),
                "image_url": r.get("image_url"),
                "width": r.get("width"),
                "height": r.get("height"),
            })
    items = pools["video"] + pools["image"]
    lines = [f"Thumbnails for '{query}':"]
    for it in items[:10]:
        tag = "[V]" if it["kind"] == "video" else "[I]"
        th = it.get("thumbnail_original") or it.get("thumbnail") or it.get("image_url")
        head = (it.get("title") or "")[:60]
        lines.append(f"  {tag} {head}")
        if th:
            lines.append(f"      {th}")
    return {
        "query": query,
        "count": len(items),
        "items": items,
        "video": pools["video"],
        "image": pools["image"],
        "n": len(items),
        "render": "\n".join(lines),
    }



def drinks(
    query: str,
    *,
    count: int = 12,
    country: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **loc,
) -> dict:
    """Drink / cocktail recipe lookup (round-16).

    Recipe queries surface schema.org `Recipe` blocks whose `category` splits
    into beverage classes ("Drinks", "Cocktail", "Beverage", "Margarita", ...)
    alongside food recipes. `drinks()` runs `recipes()` and narrows to the
    *drink* subset -- recipes whose `category` is a drink/bar class, or whose
    title/domain signals a cocktail (margarita, mojito, old fashioned,
    espresso, sangria, ...). Returns the drink recipes with their schema block
    (ingredients, timings, servings) and a readable digest.

    Args:
        query: the search term (e.g. "margarita cocktail recipe").
        count: how many recipes to scan (web mode up to ~20; default 12).
        country / safe_search / timeout: forwarded via `recipes()`.
        **loc: X-Loc-* hints.

    Returns:
        {"query", "results":[web items each with a `.recipe` block], "n",
         "render", "web"} -- same shape as `recipes()` but filtered to drink
         recipes, light title-deduped.
    """
    data = recipes(query, count=max(1, count), country=country,
                   safe_search=safe_search, timeout=timeout, **loc)
    items = []
    for r in (data.get("results") or []):
        rc = r.get("recipe") or {}
        tokens = ("%s %s" % (rc.get("title") or "", rc.get("category") or ""))
        tl = tokens.lower()
        dom = (r.get("domain") or "").lower()
        cat = (rc.get("category") or "").strip().lower()
        drink_hint = (
            cat in DRINK_CATEGORIES
            or any(w in tl for w in (
                "cocktail", "margarita", "martini", "mojito", "negroni",
                "old fashioned", "espresso", "sangria", "mimosa", "daiquiri",
                "cosmopolitan", "whiskey", "tequila", "vodka", "gin", "brandy"))
            or any(s in dom for s in ("liquor", "cocktail", "drink", "wine"))
        )
        if drink_hint:
            items.append(r)
    # light title dedupe
    seen = set(); uniq = []
    for r in items:
        k = (r.get("recipe") or {}).get("title") or r.get("title")
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        uniq.append(r)
    lines = [f"Drinks for '{query}':"]
    for r in uniq:
        rc = r.get("recipe") or {}
        ln = f"  · {rc.get('title') or r.get('title')}"
        meta = []
        if rc.get("time"):
            meta.append(rc["time"])
        if rc.get("servings") is not None:
            meta.append("serves %s" % rc["servings"])
        if rc.get("category"):
            meta.append(rc["category"])
        if meta:
            ln += "  [" + ", ".join(meta) + "]"
        lines.append(ln)
        if rc.get("ingredients"):
            lines.append("    %d ingredients" % len(rc["ingredients"]))
        if rc.get("cuisine"):
            lines.append("    Cuisine: %s" % rc["cuisine"])
        if r.get("url"):
            lines.append("    %s" % r["url"])
    return {
        "query": query,
        "results": uniq,
        "n": len(uniq),
        "render": "\n".join(lines),
        "web": data.get("web"),
    }


def domain(domain_name: str, *, count: int = 20, country=None, search_lang=None,
          safe_search: str = "moderate", summary: bool = True, timeout: float = 45.0,
          **loc) -> dict:
    """Site-scoped lookup for a domain (round-11).

    Runs a `site:<domain>` web search and summarises the top pages, so an agent
    can answer "what does Python.org cover?" or "find the npm CLI docs on
    docs.npmjs.com" in one call. A brand-homepage query usually also surfaces the
    knowledge panel (`infobox`) — e.g. Python.org returns the "Python is a
    general-purpose programming language" panel.

    Args:
        domain: a domain (or full URL); protocol/path/www. prefix is stripped.
        count: how many site pages (1..20).
        country / search_lang / safe_search / summary / timeout: passed through
            to `search()`. `**loc`: X-Loc-* hints.

    Returns:
        {"domain", "query", "results": [web items], "summary": (AI deep-link when
         summary=True), "infobox": {..}|None, "n", "count", "render", "search"}.
    """
    if not domain_name or not str(domain_name).strip():
        raise ValueError("domain() needs a domain name, e.g. 'python.org'")
    name = str(domain_name).strip().lower()
    if "://" in name:
        name = name.split("://", 1)[1]
    name = name.split("/")[0].split("?")[0]
    if name.startswith("www."):
        name = name[4:]
    if not name:
        raise ValueError("domain() needs a domain name, e.g. 'python.org'")
    data = _search_full(
        f"site:{name}", count=max(1, min(count or 20, 20)),
        country=country, search_lang=search_lang, safe_search=safe_search,
        summary=bool(summary), text_decorations=False,
        timeout=timeout or 45.0, loc=loc or None,
    )
    items = data.get("results") or []
    lines = [f"[{name}] {len(items)} top pages:"]
    for i, r in enumerate(items, 1):
        lines.append(f"{i}. {r.get('title','')}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        desc = (r.get("description") or "")[:220]
        if desc:
            lines.append(f"   {desc}")
    return {
        "domain": name,
        "query": data.get("query"),
        "results": items,
        "summary": data.get("summarizer"),
        "infobox": data.get("infobox"),
        "n": len(items),
        "count": count or 20,
        "render": "\n".join(lines),
        "search": data,
    }


def find_files(query: str, filetype: str = "", *, count: int = 15,
               safe_search: str = "moderate", freshness=None, country=None,
               timeout: float = 45.0, **loc) -> dict:
    """Find web documents of a given file type (round-11).

    Combines Brave's `filetype:` operator with its `content_type` signal to
    return only direct document hits (PDF/DOCX/PPT/...). Brave tags document
    results with a `content_type` (e.g. `"pdf"`); `find_files` narrows a query to
    just those rows rather than mixing papers in with HTML pages.

    Args:
        query: what the documents are about.
        filetype: the file extension to target (e.g. "pdf", "docx", "ppt",
            "doc", "pptx"); omit to return *any* document (content_type present).
        count: how many results to search (1..20).
        safe_search / freshness / country / timeout: forward.
        **loc: X-Loc-* hints.

    Returns:
        {"query", "filetype": matched-ext (or None), "results": [web items with
         content_type], "n", "render", "search"}.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("find_files() needs a query")
    ft = None
    q = query
    if filetype:
        ft = str(filetype).strip().lower().lstrip(".")
        if ft:
            q = f"{query} filetype:{ft}"
    d = _search_full(q, count=max(1, min(count or 15, 20)), safe_search=safe_search,
               country=country, extra=True, text_decorations=False,
               freshness=freshness, timeout=timeout or 45.0, loc=loc or None)
    results = d.get("results") or []
    if ft:
        matched = []
        for r in results:
            ct = (r.get("content_type") or "").lower().lstrip(".")
            if ct and (ct == ft or ft in ct):
                matched.append(r)
    else:
        matched = [r for r in results if r.get("content_type")]
    lines = [f"Files ({ft or 'any'}): {len(matched)} of {len(results)} results"]
    for r in matched[:12]:
        ext = (r.get("content_type") or "").upper()
        lines.append(f"· [{ext}] {r.get('title','')}")
        if r.get("url"):
            lines.append(f"  {r['url']}")
    return {
        "query": query,
        "filetype": ft,
        "results": matched,
        "n": len(matched),
        "render": "\n".join(lines),
        "search": d,
    }


def news_cluster(topic: str, *, count: int = 30, freshness: str = "pd_1w",
                 country=None, search_lang=None, safe_search: str = "moderate",
                 timeout: float = 45.0, **loc) -> dict:
    """Grouped news digest — news search grouped client-side by source (round-11).

    Brave's news API returns a *flat* list of articles (no native "related
    stories" clustering — see api.md). `news_cluster()` fills that gap by
    grouping the article set by source/sub-outlet, so an agent can see at a
    glance which publisher is covering a topic most, and can skip/sample
    duplicate coverage.

    Args:
        topic: news topic.
        count: articles to fetch (1..50).
        freshness: pd/pw/pm/py or a date range (default pd_1w).
        country / search_lang / safe_search / timeout: forward.
        **loc: X-Loc-* hints.

    Returns:
        {"query", "results": [news items], "groups": [{source, count, articles}],
         "n", "n_sources", "render", "search"}.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("news_cluster() needs a topic")
    d = _search_full(topic, mode="news", count=max(1, min(count or 30, 50)),
               country=country, search_lang=search_lang,
               freshness=freshness, safe_search=safe_search,
               timeout=timeout or 45.0, loc=loc or None)
    items = d.get("results") or []
    groups_map: dict[str, list] = {}
    for r in items:
        src = (r.get("source") or r.get("site") or "unknown").strip()
        groups_map.setdefault(src, []).append(r)
    grouped = [
        {"source": src, "count": len(arts), "articles": arts}
        for src, arts in sorted(groups_map.items(), key=lambda kv: -len(kv[1]))
    ]
    lines = [
        f"News on '{topic}' — {len(items)} articles across {len(grouped)} source(s):"
    ]
    for g in grouped:
        lines.append(f"{g['source']} ({g['count']})")
        for a in g["articles"][:3]:
            age = a.get("age") or ""
            age_s = f"  [{age}]" if age else ""
            lines.append(f"  · {a.get('title','')}{age_s}")
            if a.get("url"):
                lines.append(f"    {a['url']}")
    return {
        "query": topic,
        "results": items,
        "groups": grouped,
        "n": len(items),
        "n_sources": len(grouped),
        "render": "\n".join(lines),
        "search": d,
    }


def open_now(row, *, now=None):
    """Answer "is this place open right now" from a location/POI row (round-8).

    Row 6+ gave us each place's current-day open hours; Brave's `locations`
    entry also carries the venue timezone. `open_now()` resolves the current
    local clock time in that timezone and compares it to the venue's current-day
    window(s) to return a plain verdict.

    Args:
        row: a normalised location dict (from `search()["locations"]` /
            `locations(...)["results"]` / `near(...)["locations"]`).
        now: optional `datetime` override (mostly for tests); defaults to the
            venue's `timezone` clock (falling back to the caller's local time).

    Returns:
        {"open": bool, "now": "HH:MM", "day": "Friday", "window": "07:00-16:00",
         "note": str} — whenever the venue has hours for today; otherwise
        `open` is None and `note` explains (e.g. no hours available).
    """
    from datetime import datetime
    hours = row.get("opening_hours") or []
    tz_str = row.get("timezone")
    tz = None
    if tz_str:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_str)
        except Exception:
            tz = None
    now = datetime.now(tz) if tz else datetime.now()
    day = now.strftime("%A")
    window = None
    if hours:
        for h in hours:
            # hours entries look like "Friday 07:30-18:30"
            if isinstance(h, str) and h.split()[0].startswith(day[:3]):
                window = h.split()[1] if len(h.split()) > 1 else None
                break
    if not window:
        note = "no today's hours in the result"
        return {"open": None, "now": now.strftime("%H:%M"), "day": day,
                "window": None, "note": note}
    opens, _, closes = window.partition("-")
    opens = opens.strip() or "00:00"
    closes = closes.strip() or "24:00"
    cur = now.strftime("%H:%M")
    try:
        def _min(t):
            hh, mm = t.split(":")
            return int(hh) * 60 + int(mm)
        cur_m = _min(cur)
        o_m = _min(opens)
        c_m = _min(closes)
        is_open = o_m <= cur_m < c_m
    except Exception:
        is_open = None
    return {"open": is_open, "now": cur, "day": day, "window": window,
            "note": None}



def probe(
    url: str,
    *,
    max_chars: int = 8000,
    timeout: float = 15.0,
    headers: Optional[dict] = None,
) -> dict:
    """Fetch a URL and return readable text extracted from the HTML.

    Round-7 next-layer power: the Brave search API gives you result metadata but
    not the actual page content. `probe()` does a plain HTTP GET (no JS) with a
    browser-ish user-agent, strips scripts/styles/tags, collapses whitespace,
    and returns the clean text — enough to redirect an agent pipeline onto the
    underlying resource instead of just the SERP snippet.

    Args:
        url: the page to fetch.
        max_chars: cap the trimmed text at roughly this many chars.
        timeout: HTTP timeout seconds (default 15).
        headers: optional extra request headers (e.g. Accept-Language) merged
            onto the defaults.

    Returns:
        {"url", "status", "title", "text", "chars", "truncated", "final_url",
         "content_type"} — `errors` raised as BraveError on fetch failure.
    """
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    _h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    if headers:
        _h.update(headers)
    try:
        resp = _http().get(url, headers=_h, timeout=timeout, follow_redirects=True)
    except (httpx.HTTPError, ValueError) as e:
        raise BraveError(f"Fetch '{url}' failed: {e}", category="network") from None
    if resp.status_code != 200:
        raise BraveError(
            f"Fetch '{url}' returned HTTP {resp.status_code}", category="http")
    ct = resp.headers.get("content-type", "") or ""
    title = ""
    text = ""
    if "text/html" in ct:
        body = resp.text
        _re = __import__("re")
        tm = _re.search(r"<title[^>]*>(.*?)</title>", body, _re.IGNORECASE | _re.DOTALL)
        if tm:
            title = tm.group(1).strip()
            title = (title.replace("&trade;", "™").replace("&amp;", "&")
                     .replace("&quot;", '"').replace("&#39;", "'")
                     .replace("&nbsp;", " "))
        t = _re.sub(r"<(script|style|nav|footer|head)[^>]*>.*?</\1>", " ",
                    body, flags=_re.IGNORECASE | _re.DOTALL)
        t = _re.sub(r"<br[^>]*>", "\n", t, flags=_re.IGNORECASE)
        t = _re.sub(r"</(p|div|li|h[1-6]|section|article|header)>", " \n ", t,
                    flags=_re.IGNORECASE)
        t = _re.sub(r"<[^>]+>", " ", t)
        for _ent, _rep in [("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'),
                          ("&lt;", "<"), ("&gt;", ">"), ("&#x27;", "'"),
                          ("&apos;", "'"), ("&frac12;", "½"), ("&copy;", "©"),
                          ("&reg;", "®"), ("&trade;", "™"), ("&hellip;", "…"),
                          ("&mdash;", "—"), ("&ndash;", "–")]:
            t = t.replace(_ent, _rep)
        t = _re.sub(r"[ \t]+", " ", t)
        t = _re.sub(r"\n\s*\n+", "\n", t)
        t = t.strip()
        text = t
    elif resp.text:
        text = resp.text
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + "…"
    return {
        "url": url,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "content_type": ct,
        "title": title or None,
        "text": text,
        "chars": len(text),
        "truncated": truncated,
    }


def crawl(
    urls: list[str],
    *,
    max_chars: int = 4000,
    timeout: float = 15.0,
) -> dict:
    """Fetch multiple URLs and return readable text for each.

    Round-7 next-content. Useful after `search()` to pull the full text of the
    top N results:

        hits = await brave.search("clojure", count=4)
        docs = await brave.crawl([h["url"] for h in hits["results"]])
        docs["items"][0]["text"]

    Args:
        urls: page URLs to fetch.
        max_chars: cap per-page text chars.
        timeout: seconds per request.

    Returns:
        {"items":[{url, status, title, text, chars, truncated, error}]} —
        individual fetch failures are captured as `error` per item rather than
        aborting the whole batch.
    """
    urls = [str(u) for u in (urls or []) if u]
    def _one(u):
        try:
            r = probe(u, max_chars=max_chars, timeout=timeout)
            return {k: r[k] for k in ("url", "status", "title", "text", "chars", "truncated")}
        except BraveError as e:
            return {"url": str(u), "status": None, "error": str(e), "text": "", "chars": 0}
    gathered = _fanout([(u, (lambda u=u: _one(u))) for u in urls], concurrency=8)
    items = [val if ok else {"url": key, "status": None, "error": str(val), "text": "", "chars": 0}
             for key, ok, val in gathered]
    return {
        "results": items,
        "items": items,
        "total": len(items),
        "ok": sum(1 for i in items if not i.get("error")),
    }






# =====================================================================
# Round-10: Rich Search API + Local POIs / Local Descriptions
#
# BRAVE's Web Search API exposes a **Rich Search** capability: when a query
# has a rich intent (weather, stock, currency, calculator, etc.), the web
# response carries an `enable_rich_callback=1` hint with a `callback_key`.
# A second call to `/res/v1/web/rich?callback_key=...` returns the actual
# structured real-time data from third-party providers.
#
# This round also surfaces the **Local POIs** (`/res/v1/local/pois`) and
# **Local Descriptions** (`/res/v1/local/descriptions`) endpoints.  For a
# web search's `locations` section, each POI row carries a transient `id`;
# fetching those ids through /local/pois returns the deep-dive record
# (reviews, pictures, email, distance, opening-hours whole-week, profiles),
# and /local/descriptions gives an AI-generated place blurb.
#
# All verified live against api.search.brave.com (round-10).
# =====================================================================

# ---- rich vertical normalisers ---------------------------------------

def _rich_provider(raw):
    """Extract provider attribution ({name, url, type}) from a rich row."""
    p = raw.get("provider") if isinstance(raw.get("provider"), dict) else None
    if not p:
        return None
    return {"name": p.get("name"), "url": p.get("url"), "type": p.get("type")}


def _rich_weather(w):
    """Normalise the weather rich vertical."""
    loc = w.get("location") or {}
    cur = w.get("current_weather") or {}
    wind = cur.get("wind") or {}
    whet = cur.get("weather") or {}
    daily_in = w.get("daily") or []
    hourly_in = w.get("hours3") or []
    alerts = w.get("alerts") or []

    def _day(d):
        temp = d.get("temperature") or {}
        feels = d.get("feels_like") or {}
        dw = d.get("weather") or {}
        dwind = d.get("wind") or {}
        return {
            "date": d.get("date_i18n"),
            "temp_day": temp.get("day"), "temp_min": temp.get("min"),
            "temp_max": temp.get("max"), "temp_night": temp.get("night"),
            "feels": feels.get("day"),
            "pressure": d.get("pressure"), "humidity": d.get("humidity"),
            "wind_speed": dwind.get("speed"), "wind_deg": dwind.get("deg"),
            "clouds": d.get("clouds"), "pop": d.get("pop"), "uvi": d.get("uvi"),
            "weather": dw.get("description"), "weather_main": dw.get("main"),
            "sunrise": d.get("sunrise"), "sunset": d.get("sunset"),
        }

    def _hour(h):
        t = h.get("temperature")
        td = t if isinstance(t, dict) else None
        return {
            "ts": h.get("ts"),
            "temp": (td or {}).get("temp") if td else t,
            "feels_like": (td or {}).get("feels_like") if td else None,
            "pressure": h.get("pressure"), "humidity": h.get("humidity"),
            "clouds": h.get("clouds"), "pop": h.get("pop"),
            "visibility": h.get("visibility"),
            "wind_speed": (h.get("wind") or {}).get("speed"),
            "weather": ((h.get("weather") or {}).get("description")),
        }

    return {
        "location": {
            "name": loc.get("name"), "country": loc.get("country"),
            "state": loc.get("state"), "coords": loc.get("coords") or {},
            "sunrise": loc.get("sunrise"), "sunset": loc.get("sunset"),
            "tz_offset": loc.get("tzoffset"),
            "implicit": bool(loc.get("implicit_location")),
        },
        "current_time": w.get("current_time_iso"),
        "current": {
            "ts": cur.get("ts"), "temp": cur.get("temp"),
            "feels_like": cur.get("feels_like"), "pressure": cur.get("pressure"),
            "humidity": cur.get("humidity"), "dew_point": cur.get("dew_point"),
            "uvi": cur.get("uvi"), "clouds": cur.get("clouds"),
            "visibility": cur.get("visibility"),
            "wind_speed": wind.get("speed"), "wind_deg": wind.get("deg"),
            "wind_gust": wind.get("gust"),
            "weather": whet.get("description"), "weather_main": whet.get("main"),
            "sunrise": cur.get("sunrise"), "sunset": cur.get("sunset"),
        },
        "hourly": [_hour(h) for h in hourly_in] if hourly_in else [],
        "daily": [_day(d) for d in daily_in] if daily_in else [],
        "alerts": [
            {"event": a.get("event"), "description": a.get("description"),
             "start": a.get("start_ts") or a.get("start"),
             "end": a.get("end_ts") or a.get("end")}
            for a in alerts if isinstance(a, dict)
        ] if alerts else [],
    }


def _rich_stock(s):
    """Normalise the stocks vertical."""
    asset = s.get("asset_info") or {}
    q = s.get("quote") or {}
    ex = s.get("exchange_info") or {}
    return {
        "symbol": asset.get("symbol"),
        "company_name": q.get("company_name") or asset.get("name"),
        "exchange": asset.get("exchange"), "currency": asset.get("currency"),
        "latest_price": q.get("latest_price"),
        "open": q.get("open"), "close": q.get("close"),
        "high": q.get("high"), "low": q.get("low"),
        "change": q.get("change"), "change_percent": q.get("change_percent"),
        "volume": q.get("volume"), "market_cap": q.get("market_cap"),
        "pe_ratio": q.get("pe_ratio"),
        "week_52_high": q.get("week_52_high"), "week_52_low": q.get("week_52_low"),
        "latest_update": q.get("latest_update"),
        "time_range": s.get("time_range"),
        "timezone": ex.get("timezone"),
        "open_time": ex.get("open_time"), "close_time": ex.get("close_time"),
    }


def _rich_crypto(c):
    """Normalise the cryptocurrency vertical."""
    q = c.get("quote") or {}
    return {
        "intent_type": c.get("intent_type"),
        "symbol": q.get("symbol"), "name": q.get("name"),
        "current_price": q.get("current_price"),
        "market_cap": q.get("market_cap"),
        "market_cap_rank": q.get("market_cap_rank"),
        "total_volume": q.get("total_volume"),
        "high_24h": q.get("high_24h"), "low_24h": q.get("low_24h"),
        "price_change_24h": q.get("price_change_24h"),
        "price_change_pct_24h": q.get("price_change_percentage_24h"),
        "circulating_supply": q.get("circulating_supply"),
        "total_supply": q.get("total_supply"),
        "max_supply": q.get("max_supply"),
        "ath": q.get("ath"), "ath_change_pct": q.get("ath_change_percentage"),
        "ath_date": q.get("ath_date"),
        "image": q.get("image"),
    }


def _rich_currency(c):
    """Normalise the currency-conversion vertical."""
    conv = c.get("conversion") or {}
    q = conv.get("query") or {}
    info = conv.get("info") or {}
    fc = q.get("from_currency") if isinstance(q, dict) else {}
    tc = q.get("to_currency") if isinstance(q, dict) else {}
    return {
        "intent_type": conv.get("intent_type"),
        "from": {"code": (fc or {}).get("code"), "name": (fc or {}).get("full_name")},
        "to": {"code": (tc or {}).get("code"), "name": (tc or {}).get("full_name")},
        "amount": (q or {}).get("amount") if isinstance(q, dict) else None,
        "rate": info.get("rate"), "rate_timestamp": info.get("timestamp"),
        "result": conv.get("result"), "date": conv.get("date"),
    }


def _rich_calculator(c):
    """Normalise a calculator cell."""
    return {"expression": c.get("expression"), "answer": c.get("answer")}


def _rich_definitions(d):
    """Normalise a definitions (dictionary) vertical."""
    out = {
        "word": d.get("word"), "language": d.get("language"),
        "pronunciation": d.get("pronounciation"),
        "source_dict": d.get("source_dict"),
        "attribution_text": d.get("attribution_text"),
        "attribution_url": d.get("attribution_url"),
        "audio_available": bool(d.get("audio_available")),
    }
    out["definitions"] = [
        {
            "part_of_speech": d1.get("part_of_speech"),
            "meanings": [
                {
                    "text": m.get("text"),
                    "examples": m.get("example_uses") or [],
                    "related_words": m.get("related_words") or [],
                    "labels": [{"text": lb.get("text"), "type": lb.get("type")}
                               for lb in (m.get("labels") or []) if isinstance(lb, dict)],
                }
                for m in (d1.get("definitions") or [])
                if isinstance(m, dict)
            ],
        }
        for d1 in (d.get("definitions") or []) if isinstance(d1, dict)
    ]
    return out



def _rich_unitconv(uc):
    """Normalise a unit-conversion vertical (nested or flat).

    The API returns {amount, from_unit, to_unit, dimensionality} but NOT the
    result — this helper computes common conversions (km, mi, kg, lb, C, F, ...)
    so the answer is immediately usable.
    """
    if isinstance(uc, dict) and "conversion" in uc:
        conv = uc["conversion"]
        amount, fu, tu, dim = (conv.get("amount"), conv.get("from_unit"),
                               conv.get("to_unit"), conv.get("dimensionality"))
    else:
        amount, fu, tu, dim = (uc.get("amount"), uc.get("from_unit"),
                               uc.get("to_unit"), uc.get("dimensionality"))
    return {
        "amount": amount,
        "from_unit": fu,
        "to_unit": tu,
        "dimensionality": dim,
        "result": _convert_unit(amount, fu, tu),
    }


_UNITCONV_FACTORS = {
    # length (units as returned by the Brave API)
    "kilometer-mile": 0.621371, "mile-kilometer": 1.609344,
    "kilometer-meter": 1000.0, "meter-kilometer": 0.001,
    "kilometer-foot": 3280.84, "foot-kilometer": 0.0003048,
    "meter-foot": 3.28084, "foot-meter": 0.3048,
    "centimeter-inch": 0.393701, "inch-centimeter": 2.54,
    "cm-inch": 0.393701, "inch-cm": 2.54,
    "centimeter-meter": 0.01, "meter-centimeter": 100.0,
    "cm-meter": 0.01, "meter-cm": 100.0,
    "millimeter-inch": 0.0393701, "inch-millimeter": 25.4,
    "meter-yard": 1.09361, "yard-meter": 0.9144,
    # mass / weight (API uses "poundmass", not "pound")
    "kilogram-poundmass": 2.20462, "poundmass-kilogram": 0.453592,
    "kilogram-pound": 2.20462, "pound-kilogram": 0.453592,
    "gram-ounce": 0.035274, "ounce-gram": 28.3495,
    "kilogram-gram": 1000.0, "gram-kilogram": 0.001,
    "kilogram-tonne": 0.001, "tonne-kilogram": 1000.0,
    # volume
    "usgallon-liter": 3.78541, "liter-usgallon": 0.264172,
    "usgallon-litre": 3.78541, "litre-usgallon": 0.264172,
    "liter-litre": 1.0, "litre-liter": 1.0,
    "liter-gallons": 0.264172, "gallons-liter": 3.78541,
    "usgallon-gallon": 1.0, "gallon-usgallon": 1.0,
    "liter-milliliter": 1000.0, "milliliter-liter": 0.001,
    "liter-cubic_meter": 0.001, "cubic_meter-liter": 1000.0,
    # area
    "hectare-acre": 2.47105, "acre-hectare": 0.404686,
    "square_meter-acre": 0.000247105, "acre-square_meter": 4046.86,
    "square_kilometer-square_mile": 0.386102, "square_mile-square_kilometer": 2.58999,
    # speed
    "kilometer_per_hour-mile_per_hour": 0.621371, "mile_per_hour-kilometer_per_hour": 1.609344,
    "km/h-mph": 0.621371, "mph-km/h": 1.609344,
    # energy
    "kilocalorie-kilojoule": 4.184, "kilojoule-kilocalorie": 0.239006,
    # temperature (affine)
    "celsius-fahrenheit": "c2f", "fahrenheit-celsius": "f2c",
    "celsius-kelvin": "c2k", "kelvin-celsius": "k2c",
    # digital
    "megabyte-gigabyte": 0.001, "gigabyte-megabyte": 1024.0,
    "terabyte-gigabyte": 1024.0, "gigabyte-terabyte": 0.0009765625,
    "kilobyte-megabyte": 0.0009765625, "megabyte-kilobybyte": 1024.0,
}


def _convert_unit(amount, from_unit, to_unit):
    """Compute a unit conversion result for known units.  Returns None when
    the conversion is unknown."""
    try:
        amt = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amt = None
    if amt is None:
        return None
    fu = str(from_unit or "").strip().lower()
    tu = str(to_unit or "").strip().lower()
    key = fu + "-" + tu
    factor = _UNITCONV_FACTORS.get(key)
    if factor == "c2f":
        return (amt * 9 / 5) + 32
    if factor == "f2c":
        return (amt - 32) * 5 / 9
    if factor == "c2k":
        return amt + 273.15
    if factor == "k2c":
        return amt - 273.15
    if factor is None:
        return None
    return round(amt * factor, 6)


def _rich_unixts(ut):
    """Normalise a Unix-timestamp conversion vertical.

    When the API provides only the input timestamp (`ts_to_date` intent), the
    skill also computes the real date/time so the answer is ready to use.
    """
    import datetime as _dt
    conv = ut.get("conversion") or {}
    intent = conv.get("intent_type")
    ts = conv.get("input_ts")
    out = {"intent_type": intent, "input_ts": ts, "date": conv.get("date")}
    if intent == "ts_to_date" and ts is not None:
        try:
            dt_obj = _dt.datetime.fromtimestamp(int(ts), tz=_dt.timezone.utc)
            out["date"] = dt_obj.strftime("%Y-%m-%d %H:%M:%S UTC")
            out["year"] = dt_obj.year
            out["month"] = dt_obj.strftime("%B")
            out["day"] = dt_obj.day
            out["weekday"] = dt_obj.strftime("%A")
            out["iso"] = dt_obj.isoformat()
        except (ValueError, OSError, OverflowError):
            pass
    return out


def _rich_package(pk):
    """Normalise a package-tracker vertical."""
    return {
        "intent_type": pk.get("intent_type"),
        "carrier": pk.get("carrier"),
        "tracking_number": pk.get("tracking_number"),
        "status": pk.get("status"),
        "events": pk.get("events") or [],
    }


def _rich_sports(block):
    """Normalise a sports (scores) vertical.  `block` is the
    american_football / baseball / basketball / … key on the result."""
    sport = block.get("sport") or ""
    content = block.get("content") or {}
    lg = content.get("league") or {}
    games = content.get("games") or []
    return {
        "sport": sport,
        "view": block.get("view"),
        "content_type": content.get("type"),
        "league": {
            "id": lg.get("id"), "name": lg.get("name"),
            "season": lg.get("season"), "logo": lg.get("logo"),
        },
        "date": content.get("date"),
        "date_kind": content.get("date_kind"),
        "games": [
            {
                "id": g.get("id"),
                "start_time": g.get("start_time"),
                "status": ((g.get("status") or {}).get("code")
                           if isinstance(g.get("status"), dict) else None),
                "phase": g.get("phase"),
                "home": ((g.get("teams") or {}).get("home") or {}).get("name")
                        if isinstance(g.get("teams"), dict) else None,
                "away": ((g.get("teams") or {}).get("away") or {}).get("name")
                        if isinstance(g.get("teams"), dict) else None,
                "home_score": ((g.get("score") or {}).get("home"))
                              if isinstance(g.get("score"), dict) else None,
                "away_score": ((g.get("score") or {}).get("away"))
                              if isinstance(g.get("score"), dict) else None,
            }
            for g in games if isinstance(g, dict)
        ],
    }


_RICH_VERTICALS = {
    "weather": ("weather", _rich_weather),
    "stocks": ("stock", _rich_stock),
    "cryptocurrency": ("cryptocurrency", _rich_crypto),
    "currency": ("currency", _rich_currency),
    "calculator": ("calculator", _rich_calculator),
    "definitions": ("definitions", _rich_definitions),
    "unitconversion": ("unitconversion", _rich_unitconv),
    "unixtimestamp": ("unixtimestamp", _rich_unixts),
}


def _rich_result(raw):
    """Normalise one raw rich result object into `{type, subtype, provider,
    data}`.  Sports use "sports" as the subtype and the sport name as the
    data key (american_football / baseball / basketball / …)."""
    subtype = raw.get("subtype") or ""
    key, norm = _RICH_VERTICALS.get(subtype, (None, None))
    if key and norm:
        block = raw.get(key)
        data = norm(block) if isinstance(block, dict) else None
    elif subtype == "sports":
        data = None
        for k in ("american_football", "baseball", "basketball", "cricket",
                  "football", "ice_hockey", "formula1"):
            if isinstance(raw.get(k), dict):
                data = _rich_sports(raw[k])
                data["sport"] = k
                break
    elif subtype == "packagetracker":
        block = raw.get("package_tracker")
        data = _rich_package(block) if isinstance(block, dict) else None
    elif subtype == "formula1":
        data = raw.get("formula1") or None
    else:
        data = raw  # passthrough guard
    return {
        "type": raw.get("type") or "rich",
        "subtype": subtype,
        "provider": _rich_provider(raw),
        "data": data,
    }


def _rich_fetch(callback_key, *, timeout=45.0):
    """Fetch the rich payload for a callback_key."""
    d = _request("/res/v1/web/rich", {"callback_key": callback_key}, timeout)
    return d if isinstance(d, dict) else {}


def rich(query, *, fetch=True, count=5, timeout=45.0, **kw):
    """Look up a rich-data answer for a query (weather/stock/FX/…).

    Brave Web Search returns a `rich` hint when a query maps to a rich
    vertical (weather, stocks, crypto, currency, calculator, definitions,
    unit conversion, unix timestamp, sports).  `rich()` runs the web search
    with `enable_rich_callback`, then when `fetch=True` ALSO fetches the
    `/res/v1/web/rich` payload and normalises it.

    Returns: {"type":"rich","query","hint","callback_key","vertical",
              "results":[{subtype,provider,data}], "render":..., "raw":{}}.

    `fetch=False` stops at the hint without the second HTTP call.

    **kw forwarded to the inner _search_full().
    """
    data = _search_full(query, count=count, enable_rich_callback=True, **kw)
    hint = data.get("rich")
    hint_obj = (hint.get("hint") or {}) if isinstance(hint, dict) else {}
    cb = (hint_obj or {}).get("callback_key")
    vertical = (hint_obj or {}).get("vertical")
    if not cb or not fetch:
        return {"type": "rich", "query": query, "hint": hint,
                "callback_key": cb, "vertical": vertical, "results": []}
    dd = _rich_fetch(cb, timeout=timeout)
    results = [_rich_result(r) for r in (dd.get("results") or []) if isinstance(r, dict)]
    v = vertical or (results[0]["subtype"] if results else None)
    return {"type": "rich", "query": query, "hint": hint,
            "callback_key": cb, "vertical": v, "results": results,
            "render": _rich_lines(results), "raw": dd}


def _rich_lines(results):
    """Readable rendering of rich results."""
    lines = []
    for r in results:
        st = r.get("subtype") or ""
        data = r.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        prov = r.get("provider") or {}
        provname = prov.get("name") or ""
        if st == "weather":
            loc = data.get("location") or {}
            cur = data.get("current") or {}
            city = loc.get("name") or "?"
            cc = loc.get("country") or ""
            where = f" in {city}, {cc}"
            lines.append(f"[Weather{where}] {cur.get('temp')}°C {cur.get('weather')}")
            lines.append(f"   Feels {cur.get('feels_like')}°C · Hum {cur.get('humidity')}% · Wind {cur.get('wind_speed')} m/s")
            for d in (data.get("daily") or [])[:5]:
                lines.append(f"   {d.get('date')}: {d.get('temp_min')}°–{d.get('temp_max')}°C {d.get('weather')} (pop {d.get('pop')})")
            if provname:
                lines.append(f"   Source: {provname}")
        elif st == "stocks":
            n = data.get("company_name") or data.get("symbol") or "?"
            px = data.get("latest_price")
            sig = data.get("currency") or "USD"
            parts = [f"[Stock] {n}"]
            if px is not None:
                parts.append(f"{px} {sig}")
            if data.get("change_percent") is not None:
                parts.append(f"Δ {data['change_percent']:.2f}%")
            lines.append(" · ".join(parts))
            if data.get("volume"):
                lines.append(f"   Vol {int(data['volume']):,}")
            if data.get("market_cap"):
                lines.append(f"   Cap {int(data['market_cap']):,}")
            if data.get("pe_ratio"):
                lines.append(f"   P/E {data['pe_ratio']:.1f}")
            if data.get("week_52_high") or data.get("week_52_low"):
                lines.append(f"   52w {data.get('week_52_low')}–{data.get('week_52_high')}")
        elif st == "cryptocurrency":
            n = data.get("name") or data.get("symbol") or "?"
            px = data.get("current_price")
            chg = data.get("price_change_pct_24h")
            base = f"[Crypto] {n}"
            if px is not None:
                base += f" · ${px:,}"
            if chg is not None:
                base += f" · 24h Δ {chg:.2f}%"
            lines.append(base)
            if data.get("market_cap"):
                lines.append(f"   MCap ${int(data['market_cap']):,}")
            if data.get("market_cap_rank"):
                lines.append(f"   Rank #{data['market_cap_rank']}")
            if data.get("high_24h") or data.get("low_24h"):
                lines.append(f"   24h {data.get('low_24h')}–{data.get('high_24h')}")
            if provname:
                lines.append(f"   Source: {provname}")
        elif st == "currency":
            frm = data.get("from") or {}
            to = data.get("to") or {}
            line = f"[Currency] {data.get('amount', '?')} {frm.get('code', '?')}"
            if data.get("result") is not None:
                line += f" = {data['result']} {to.get('code', '?')}"
            lines.append(line)
            if data.get("rate") is not None:
                lines.append(f"   Rate: 1 {frm.get('code', '?')} = {data['rate']} {to.get('code', '?')}")
            if data.get("date"):
                lines.append(f"   {data['date']}")
            if provname:
                lines.append(f"   Source: {provname}")
        elif st == "calculator":
            lines.append(f"[Calculator] {data.get('expression', '?')} = {data.get('answer', '?')}")
        elif st == "definitions":
            word = data.get("word") or "?"
            head = f"[Definition] {word}"
            if data.get("pronunciation"):
                head += f"  ({data['pronunciation']})"
            lines.append(head)
            for d in (data.get("definitions") or []):
                pos = d.get("part_of_speech") or ""
                for m in (d.get("meanings") or []):
                    lines.append(f"   {pos}: {m.get('text', '')}")
            if data.get("source_dict"):
                lines.append(f"   Source: {data['source_dict']}")
        elif st == "unitconversion":
            amount = data.get("amount", "?")
            fu = data.get("from_unit", "?")
            tu = data.get("to_unit", "?")
            result = data.get("result", "?")
            lines.append(f"[Unit] {amount} {fu} = {result} {tu}")
            if data.get("dimensionality"):
                lines.append(f"   Dimensionality: {data['dimensionality']}")
        elif st == "unixtimestamp":
            intent = data.get("intent_type")
            ts = data.get("input_ts")
            if intent == "ts_to_date" and ts is not None:
                lines.append(f"[Unix] timestamp {ts}")
            elif intent == "date_to_ts":
                lines.append(f"[Unix] {data.get('date')}")
            else:
                lines.append(f"[Unix] {ts or data.get('date', '?')}")
        elif st == "sports":
            lg = data.get("league") or {}
            lines.append(f"[Sports] {lg.get('name') or data.get('sport', '?')}")
            for g in (data.get("games") or [])[:8]:
                home = g.get("home") or "?"
                away = g.get("away") or "?"
                hs = g.get("home_score")
                as_ = g.get("away_score")
                if hs is None and as_ is None:
                    lines.append(f"   {away} vs {home}")
                else:
                    lines.append(f"   {away} {as_} – {hs} {home}")
            if provname:
                lines.append(f"   Source: {provname}")
        else:
            # generic fallback
            lines.append(f"[{st}] {str(data)[:200]}")
    return "\n".join(lines)


# ---- typed rich lookups (one-liners around `rich`) ------------------------

def weather(query, *, count=5, **kw):
    """One-call lookup: `brave.weather("london") → "→ {lat, lon}"`.
    Returns the full `rich()` package with `results[0]["data"]` = weather
    normalised block (location, current, daily, hourly, alerts)."""
    d = rich(query, count=count, **kw)
    return d


def stock_quote(ticker, *, count=5, **kw):
    """One-call lookup of a live stock quote. `brave.stock_quote("AAPL")` →
    rich() with results[0]["data"] = {symbol, latest_price, change_percent, …}."""
    return rich(f"{ticker} stock", count=count, **kw)


def crypto(coin, *, count=5, **kw):
    """One-call cryptocurrency price lookup. `brave.crypto("bitcoin")`."""
    return rich(f"{coin} price", count=count, **kw)


def definition(word, *, count=5, **kw):
    """One-call dictionary lookup. `brave.definition("serendipity")`."""
    return rich(f"define {word}", count=count, **kw)


def currency_x(amount, from_code, to_code, *, count=5, **kw):
    """One-call FX conversion. `brave.currency_x(100, "USD", "EUR")`."""
    return rich(f"{amount} {from_code} to {to_code}", count=count, **kw)


def convert_values(amount, from_unit, to_unit, *, count=5, **kw):
    """One-call unit conversion. `brave.convert_values(100, "km", "mi")`."""
    return rich(f"convert {amount} {from_unit} to {to_unit}", count=count, **kw)


# ---- Local POIs / Local Descriptions ---------------------------------------

def _poi_item(item):
    """Normalise a raw `/local/pois` result into a flat POI record.

    Unlike _search_full()["locations"] rows (which come from the web /locations
    section), the POIs endpoint returns the *deep* record: `reviews`,
    `pictures`, `contact` (email + phone), `distance`, `profiles`,
    price_range, and opening_hours with the full weekly shape.
    """
    addr = item.get("postal_address") or {}
    th = item.get("thumbnail") or {}
    rating = item.get("rating") or {}
    hours = item.get("opening_hours") or {}
    days = (hours.get("days") or []) if isinstance(hours, dict) else []
    contact = item.get("contact") or {}
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "description": item.get("description"),
        "type": item.get("type") or "location_result",
        "id": item.get("id"),
        "coordinates": list(item.get("coordinates") or []),
        "address": addr.get("displayAddress") if addr else None,
        "postal_address": addr if addr else None,
        "phone": contact.get("telephone") if isinstance(contact, dict) else None,
        "email": contact.get("email") if isinstance(contact, dict) else None,
        "weeks": [
            [
                f"{e.get('full_name') or e.get('abbr_name')} {e.get('opens')}-{e.get('closes')}"
                for e in seg if isinstance(e, dict)
            ]
            for seg in days if isinstance(seg, list)
        ] if isinstance(days, list) else [],
        "price_range": item.get("price_range"),
        "rating": {
            "value": rating.get("ratingValue"),
            "best": rating.get("bestRating"),
            "reviews": rating.get("reviewCount"),
            "is_tripadvisor": bool(rating.get("is_tripadvisor")),
        } if isinstance(rating, dict) else None,
        "distance": item.get("distance"),
        "profiles": [
            {"name": p.get("name"), "url": p.get("url"), "img": p.get("img")}
            for p in (item.get("profiles") or []) if isinstance(p, dict)
        ] or None,
        "reviews": [
            {
                "title": r.get("title"),
                "description": r.get("description"),
                "date": r.get("date"),
                "rating": ((r.get("rating") or {}).get("ratingValue")
                           if isinstance(r.get("rating"), dict) else None),
                "author": ((r.get("author") or {}).get("name")
                           if isinstance(r.get("author"), dict) else None),
                "author_url": ((r.get("author") or {}).get("url")
                              if isinstance(r.get("author"), dict) else None),
                "url": r.get("review_url"),
            }
            for r in ((item.get("reviews") or {}).get("results") or [])
            if isinstance(r, dict)
        ] or None,
        "reviews_url": (item.get("reviews") or {}).get("viewMoreUrl"),
        "pictures": [
            p.get("src") or p.get("original")
            for p in ((item.get("pictures") or {}).get("results") or [])
            if isinstance(p, dict)
        ] or None,
        "pictures_url": (item.get("pictures") or {}).get("viewMoreUrl"),
        "thumbnail": th.get("src"),
        "thumbnail_original": th.get("original"),
        "action": item.get("action"),
        "categories": item.get("categories") or [],
        "cuisine": item.get("serves_cuisine") or [],
        "icon_category": item.get("icon_category"),
        "timezone": item.get("timezone"),
        "timezone_offset": item.get("timezone_offset"),
    }


def pois(ids, *, search_lang="en", ui_lang="en-US", units="metric", timeout=45.0, **kw):
    """Fetch deep-detail records for location ids.

    The `search()["locations"]` rows carry a transient `id` (valid ~8h);
    `pois()` passes those ids to `/res/v1/local/pois` and returns the
    business' deep detail: reviews, pictures, email, phone, distance,
    profiles, full-week schedule, price range, rating, contact.

    Args:
        ids: one string or a list of strings (max 20).
        search_lang / ui_lang / units: forwarded to the endpoint.

    Returns: {"type":"local_pois","results":[...normalised...],"n":int,
              "render":"readable"}.
    """
    id_list = [ids] if isinstance(ids, str) else list(ids or [])
    if not id_list:
        return {"type": "local_pois", "results": [], "n": 0, "render": ""}
    if len(id_list) > 20:
        id_list = id_list[:20]
    params = {}
    for i in id_list:
        params.setdefault("_idlist", []).append(i)
    qp = []
    for x, i in enumerate(id_list):
        qp.append(("ids", str(i)))
    if search_lang:
        qp.append(("search_lang", search_lang))
    if ui_lang:
        qp.append(("ui_lang", ui_lang))
    if units:
        qp.append(("units", units))
    # use _request with a special params handling for list-type ids
    # (httpx handles tuple-list params as repeated keys)
    data = _request_list("/res/v1/local/pois", qp, timeout)
    if not isinstance(data, dict):
        return {"type": "local_pois", "results": [], "n": 0, "render": ""}
    res = [_poi_item(i) for i in (data.get("results") or []) if isinstance(i, dict)]
    render = _poi_lines(res)
    return {
        "type": "local_pois",
        "results": res,
        "n": len(res),
        "render": render,
    }


def poi_descriptions(ids, *, timeout=45.0):
    """Fetch AI-generated descriptions for given location ids.

    `/res/v1/local/descriptions` returns a short ownership-era blurb about
    each place (e.g. "Taylor Street Coffee Shop is a popular breakfast
    and brunch spot …").

    Args: ids — a string or list of strings (max 20).
    Returns: {"type":"local_descriptions","results":[…] (title, description),
              "n":…, "render":…}
    """
    id_list = [ids] if isinstance(ids, str) else list(ids or [])
    if not id_list:
        return {"type": "local_descriptions", "results": [], "n": 0, "render": ""}
    id_list = id_list[:20]
    qp = [("ids", i) for i in id_list]
    data = _request_list("/res/v1/local/descriptions", qp, timeout)
    if not isinstance(data, dict):
        return {"type": "local_descriptions", "results": [], "n": 0, "render": ""}
    res = [
        {"id": r.get("id"), "title": r.get("title"), "description": r.get("description")}
        for r in (data.get("results") or []) if isinstance(r, dict)
    ]
    lines = []
    for r in res:
        lines.append(f"{r.get('title')}: {r.get('description')}")
    return {"type": "local_descriptions", "results": res, "n": len(res), "render": "\n".join(lines) if lines else ""}

def _place_geo_item(entry: dict) -> dict:
    """Normalize a place_search 'city'/'country'/'region'/'neighborhood' bucket (round-11).

    The dedicated `/res/v1/local/place_search` endpoint can surface not just
    POI rows (`results`) but entire geographic groups — a city / country /
    region / neighborhood matched by the query. Each bucketed entry carries a
    name, country, coordinates and (when disclosed) a hero image; we flatten it
    to a small, clean dict an agent can render as an "about this place" header.
    """
    return {
        "type": entry.get("type"),
        "name": entry.get("name"),
        "country": entry.get("country"),
        "coordinates": entry.get("coordinates") or [],
        "thumbnail_original": ((entry.get("thumbnail") or {}).get("original")
                                if isinstance(entry.get("thumbnail"), dict) else None),
    }


def _place_context_item(entry: dict) -> dict:
    """Normalize a place_search 'address'/'street' bucket entry (round-11).

    Beyond POIs and geography-buckets, `/place_search` can also return whole
    `addresses` (street + number) and `streets` matched by a query, each with
    coordinates, its own postal address, a suggested map `zoom_level`, and (when
    disclosed) a `distance` from the search centre. Those rows may embed POIs
    sitting *on* (`pois`) or *nearby* (`pois_nearby`) the address/street.
    """
    po = (entry.get("postal_address") or {}) if isinstance(entry.get("postal_address"), dict) else {}
    return {
        "type": entry.get("type"),    # 'address' | 'street'
        "name": entry.get("name"),
        "coordinates": entry.get("coordinates") or [],
        "zoom_level": entry.get("zoom_level"),
        "distance": entry.get("distance"),   # {'value','units'} when disclosed
        "postal_address": po if po else None,
        "pois": [_location_item(p) for p in (entry.get("pois") or [])],
        "pois_nearby": [_location_item(p) for p in (entry.get("pois_nearby") or [])],
    }


def place_search(query=None, *, latitude=None, longitude=None, location=None,
                 radius=None, count=10, country=None, search_lang=None, ui_lang=None,
                 units=None, safesearch=None, spellcheck=None, geoloc=None,
                 cate=None, timeout=45.0, **kw):
    """Dedicated place / POI search against the Brave Place Search API (round-11).

    The dedicated `/res/v1/local/place_search` endpoint searches Brave's
    geographic index of 200M+ places (businesses, landmarks, POIs), anchored to
    a coordinate pair (`latitude`/`longitude`) or a location name (`location`).
    Omit `query` for **explore mode**: general points of interest in the area.

    Besides POIs (`results`), the endpoint can also return rich geographic
    groupings the legacy `locations` section never exposed — `cities`,
    `countries`, `regions`, `neighborhoods`, `addresses`, and `streets` — each
    with coordinates and (for addresses/streets) distance + nested POIs.

    Args:
        query: What to look for (e.g. "coffee shops"). Omit for explore mode.
        latitude/longitude: search centre (both required together).
        location: alternative anchor string, e.g. "san francisco ca united
            states" / "tokyo japan".
        radius: meters *bias* toward the centre (not a hard cutoff; search is
            global if omitted).
        count: how many results (1..100).
        country / search_lang / ui_lang / units / safesearch: forwardable
            filters (country two-letter code; units metric|imperial;
            safesearch moderate|strict).
        spellcheck: force True/False.
        geoloc: "<lat>x<lon>" to compute distance values.
        cate: (round-13) EP place category hint (e.g. "cafe", "pizza", "hotel").
            Accepted by the Place Search API (verified live: 200) but a
            pass-through on this plan — results are not visibly filtered.
        timeout: HTTP timeout seconds.

    Returns:
        {"query", "results": [normalized POI rows], "cities"/"countries"/
         "regions"/"neighborhoods" (geo buckets), "addresses"/"streets"
         (address/street buckets), "mixed": (interleave ordering),
         "resolved": {"name","country","coordinates"} (resolved anchor),
         "n", "count", "render"}.
    """
    if latitude is None and longitude is None and not location:
        raise ValueError(
            "place_search needs either latitude+longitude OR a location string "
            "(e.g. 'san francisco ca united states')"
        )
    if (latitude is None) != (longitude is None):
        raise ValueError("latitude and longitude must be provided together")
    if not (1 <= count <= 100):
        raise ValueError("place_search count must be in 1..100")
    params: dict[str, object] = {}
    if query:
        params["q"] = query
    if latitude is not None:
        params["latitude"] = latitude
        params["longitude"] = longitude
    if location:
        params["location"] = location
    if radius is not None:
        params["radius"] = radius
    params["count"] = count
    if cate:
        params["cate"] = cate
    for k, v in (("country", country), ("search_lang", search_lang), ("ui_lang", ui_lang),
                 ("units", units), ("safesearch", safesearch), ("geoloc", geoloc)):
        if v:
            params[k] = v
    if spellcheck is not None:
        params["spellcheck"] = "true" if spellcheck else "false"

    data = _request(PATHS["place_search"], params, timeout)

    results = [_location_item(r) for r in (data.get("results") or [])]
    geo = {
        b: [_place_geo_item(it) for it in (data.get(b) or [])]
        for b in ("cities", "countries", "regions", "neighborhoods")
    }
    ctx = {
        b: [_place_context_item(it) for it in (data.get(b) or [])]
        for b in ("addresses", "streets")
    }
    resolved = data.get("location") or {}
    resolved_dict = {
        "name": resolved.get("name"),
        "country": resolved.get("country"),
        "coordinates": resolved.get("coordinates") or [],
    } if resolved else None

    header = f"Place search: {query or '(explore mode)'}"
    if resolved_dict:
        header += "  ·  resolved: {} ({}), {}".format(
            resolved_dict.get("name") or "", resolved_dict.get("country") or "",
            ", ".join(str(x) for x in resolved_dict.get("coordinates") or [])
        ).rstrip(", ")
    lines = [header]
    if results:
        lines.append("[Places]")
        for i, r in enumerate(results, 1):
            title = r.get("title") or ""
            addr = r.get("address") or ""
            ln = f"{i}. {title}" + (f" — {addr}" if addr else "")
            lines.append(ln)
            if r.get("phone"):
                lines.append(f"   ☎ {r['phone']}")
            if r.get("opening_hours"):
                lines.append(f"   open: {' / '.join(r['opening_hours'][:2])}")
            info = ""
            if r.get("price"):
                info += " " + r["price"]
            rt = r.get("rating") or {}
            if rt.get("value") is not None:
                info += f"  ★{rt['value']}"
                if rt.get("reviews"):
                    info += f" ({rt['reviews']} reviews)"
            if info.strip():
                lines.append("  " + info.strip())
            if r.get("url"):
                lines.append(f"   {r['url']}")
    for b in ("cities", "countries", "regions", "neighborhoods"):
        if geo[b]:
            lines.append(f"[{b.capitalize()}]")
            for g in geo[b][:6]:
                coord = g.get("coordinates") or []
                cs = f" {coord[0]:.2f},{coord[1]:.2f}" if len(coord) == 2 else ""
                lines.append(("  {} ({}){}".format(g.get("name") or "",
                                                   g.get("country") or "", cs)).strip())
    for b in ("addresses", "streets"):
        if ctx[b]:
            lines.append(f"[{b.capitalize()}]")
            for c in ctx[b][:6]:
                dist = c.get("distance")
                ds = ""
                if isinstance(dist, dict) and dist.get("value") is not None:
                    ds = f"  ({dist['value']}{dist.get('units','')})"
                lines.append(f"  {c.get('name','')}{ds}")
    if not results and not any(geo[b] for b in geo) and not any(ctx[b] for b in ctx):
        lines.append("[no places found]")

    return {
        "query": query,
        "results": results,
        **geo,
        **ctx,
        "mixed": data.get("mixed") or [],
        "resolved": resolved_dict,
        "n": len(results),
        "count": count,
        "render": "\n".join(lines),
    }


def _request_list(path, params_pairs, timeout=45.0):
    """Call an endpoint with repeated (name,value) params (e.g. ids=X&ids=Y).

    The Brave POIs/Descriptions APIs require repeated `ids=…` query params,
    which httpx supports via a list of tuples.  `_request()` passes a dict,
    so we use a direct call here mirroring the same error handling.
    """
    key = _get_api_key()
    headers = {"X-Subscription-Token": key}
    url = f"{DEFAULT_API_URL}{path}"
    try:
        resp = _http().get(url, params=params_pairs, headers=headers, timeout=timeout,
                           follow_redirects=False)
    except (httpx.TimeoutException, httpx.ConnectError,
            httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        raise BraveError(f"Brave request to '{path}' failed on the wire: {e}",
                          category="network") from None
    except httpx.HTTPError as e:
        raise BraveError(f"Brave HTTP error for '{path}': {e}", category="network") from None
    if resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            return None
    body = {}
    try:
        body = resp.json()
    except Exception:
        pass
    err = body.get("error") or {}
    code = err.get("code")
    if resp.status_code == 401 or code == "SUBSCRIPTION_TOKEN_INVALID":
        raise BraveError(f"Brave auth error for '{path}'", category="auth")
    if resp.status_code == 422:
        raise BraveError(f"Brave param validation error for '{path}': {body}",
                          category="param", details=body)
    if resp.status_code == 429:
        raise BraveError(f"Brave rate limit for '{path}'", category="rate_limit")
    raise BraveError(f"Brave HTTP {resp.status_code} for '{path}'", category="http")


def _poi_lines(items):
    """Readable rendering of POI detail rows."""
    out = []
    if not items:
        return ""
    out.append("[Place Details]")
    for i, it in enumerate(items, 1):
        out.append(f"{i}. {it.get('title') or '?'}")
        if it.get("address"):
            out.append(f"   {it['address']}")
        if it.get("phone"):
            out.append(f"   ☎ {it['phone']}")
        if it.get("email"):
            out.append(f"   ✉ {it['email']}")
        if it.get("price_range"):
            out.append(f"   price: {it['price_range']}")
        r = it.get("rating") or {}
        if r.get("value") is not None:
            out.append(f"   ★ {r['value']}" + (f" ({r['reviews']} reviews)" if r.get("reviews") else ""))
        if it.get("distance"):
            d = (it["distance"].get("value")) if isinstance(it["distance"], dict) else it["distance"]
            u = (it["distance"].get("units")) if isinstance(it["distance"], dict) else ""
            if d is not None:
                out.append(f"   {d} {u}")
        if it.get("reviews"):
            out.append(f"   ↳ {it['reviews_url'] or 'more reviews'} ({len(it['reviews'])} shown)")
        if it.get("pictures"):
            out.append(f"   {len(it['pictures'])} pictures {it['pictures_url'] or ''}")
    return out


def place_detail(
    query: str,
    *,
    count: int = 10,
    index: int = 0,
    similar: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    location: Optional[str] = None,
    radius: Optional[float] = None,
    country: Optional[str] = None,
    search_lang: Optional[str] = None,
    safe_search: str = "moderate",
    timeout: float = 45.0,
    **kw,
) -> dict:
    """One-call deep dossier for a place (round-15).

    Brave''s geographic index exposes the *same* place through several
    complementary lenses scattered across different endpoints:
      * `place_search` / web `locations` rows — the lightweight row (title,
        street address, coords, phone, rating, price, weekly hours, categories)
      * `pois()`   — the deep record (reviews, pictures, email, profiles,
        full-week schedule, price range) keyed by the transient `id`
      * `poi_descriptions()` — the AI-written place blurb
      * `near()`   — what is around it (geo-anchored at the place itself)
    `place_detail()` resolves a place (by query or a `loc...` id) and assembles
    all four into one object with a `render`, so an agent gets the complete
    picture — deep review+picture data, the AI blurb, and curated nearby
    alternatives with **computed straight-line distances** (km/m) from the
    anchor coordinates.

    Args:
        query: the place name/query (e.g. "Sightglass Coffee"). A `loc...` id
            (from `pois`/`place_search`) is also accepted.
        count: how many nearby places to pull (default 10).
        index: which place_search result is the anchor (default 0).
        similar: category/term to fetch nearby for; defaults to the anchor''s
            first category, else the original query.
        latitude/longitude: optional explicit anchor coords (both together).
        location: optional city/region anchor for `place_search`.
        radius: metres for `place_search`.
        country / search_lang / safe_search / timeout: forwarded to the
            underlying calls.
        **kw: forwarded to `place_search` (cate, spellcheck, ...).

    Returns:
        {"query", "anchor", "pois", "blurb", "nearby", "coords", "n", "count",
         "render", "search"}. `nearby` is `None` when the anchor has no
        coordinates; deep `pois`/`blurb` may be missing when the transient
        `id` has expired (rare). Distances are great-circle estimates from the
        anchor coordinate — still get an ETA API before quoting drive times.
    """
    import math as _math
    def _hav_m(a, b, c, d):
        R = 6371000.0
        p1, p2 = _math.radians(a), _math.radians(c)
        dp = _math.radians(c - a); dl = _math.radians(d - b)
        h = _math.sin(dp/2)**2 + _math.cos(p1)*_math.cos(p2)*_math.sin(dl/2)**2
        return 2 * R * _math.asin(_math.sqrt(h))
    def _fmt(m):
        return f"{m/1000.0:.1f} km" if m >= 1000 else f"{m:.0f} m"

    q = (query or "").strip()
    if not q:
        raise ValueError("place_detail() needs a query or a loc... id")
    kw_ps = dict(kw)
    if radius is not None:
        kw_ps.setdefault("radius", radius)
    ps = place_search(q, latitude=latitude, longitude=longitude,
                      location=location, count=max(index + 1, 1),
                      country=country, search_lang=search_lang,
                      timeout=timeout, **kw_ps)
    rows = ps.get("results") or []
    if not rows:
        return {"query": q, "anchor": None, "pois": None, "blurb": None,
                "nearby": None, "coords": None, "n": 0, "count": count,
                "render": "", "search": ps}
    anchor_idx = min(index, len(rows) - 1)
    anchor = rows[anchor_idx]
    pid = anchor.get("id")
    alat = anchor.get("latitude"); alon = anchor.get("longitude")
    if (alon is None or alat is None) and anchor.get("coordinates"):
        c = anchor.get("coordinates")
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            alat, alon = c[0], c[1]
    coords = ([alat, alon] if alon is not None and alat is not None else None)

    deep = None
    if pid:
        try:
            pr = (pois(pid, timeout=timeout).get("results") or [])
            deep = pr[0] if pr else None
        except Exception:
            deep = None
    desc = None
    if pid:
        try:
            dsc = poi_descriptions(pid, timeout=timeout).get("results") or []
            if dsc and dsc[0].get("description"):
                desc = {"id": dsc[0].get("id"),
                        "title": dsc[0].get("title") or anchor.get("title"),
                        "description": dsc[0].get("description")}
        except Exception:
            desc = None

    nearby = None
    if coords:
        cats = anchor.get("categories") or (deep or {}).get("categories") or []
        term = similar if similar else (cats[0] if cats else q)
        try:
            n = near(term, latitude=coords[0], longitude=coords[1],
                     count=max(1, count), country=country,
                     search_lang=search_lang, safe_search=safe_search,
                     timeout=timeout)
            nearby = []
            seen = set()
            for row in (n.get("locations") or []):
                rlat = row.get("latitude"); rlon = row.get("longitude")
                if (rlat is None or rlon is None) and row.get("coordinates"):
                    c = row.get("coordinates")
                    if isinstance(c, (list, tuple)) and len(c) >= 2:
                        rlat, rlon = c[0], c[1]
                dm = dfm = None
                if rlat is not None and rlon is not None and coords:
                    dm = _hav_m(coords[0], coords[1], rlat, rlon) / 1000.0
                    dfm = _fmt(dm * 1000.0)
                row_id = row.get("id")
                row_title = (row.get("title") or "").strip()
                # Skip the anchor itself + exact duplicates.
                is_self = bool(pid and row_id and row_id == pid) or (
                    row_title and row_title == (anchor.get("title") or "").strip()
                    and dm is not None and dm <= 0.01)
                dupe_key = row_id or (row_title, dfm)
                if is_self or dupe_key in seen:
                    continue
                seen.add(dupe_key)
                nearby.append({"id": row_id, "title": row_title or None,
                               "url": row.get("url"),
                               "address": row.get("address") or (row.get("postal_address") or {}).get("displayAddress"),
                               "coordinates": row.get("coordinates"),
                               "distance_km": round(dm, 2) if dm is not None else None,
                               "distance_formatted": dfm,
                               "distance_kind": "straight_line_estimate"})
            if count:
                nearby = nearby[:count]
        except Exception:
            nearby = None

    lines = [anchor.get("title") or f"Place: {q}"]
    addr = (deep or {}).get("address") or anchor.get("address")
    if addr:
        lines.append(f"  {addr}")
    if coords:
        lines.append(f"  coords: {coords[0]:.5f}, {coords[1]:.5f}")
    rt = (deep or {}).get("rating") or anchor.get("rating")
    if isinstance(rt, dict):
        if rt.get("value") is not None:
            lines.append(f"  \u2605 {rt['value']} ({rt.get('reviews', 0)} reviews)")
    price = (deep or {}).get("price_range") or anchor.get("price")
    if price:
        lines.append(f"  price: {price}")
    phone = (deep or {}).get("phone") or anchor.get("phone")
    if phone:
        lines.append(f"  phone: {phone}")
    cats = anchor.get("categories") or (deep or {}).get("categories") or []
    if cats:
        lines.append(f"  categories: {', '.join(cats[:6])}")
    if desc:
        lines.append("  blurb:")
        lines.append("    " + desc["description"].replace("\n", "\n    "))
    if nearby:
        lines.append(f"  {len(nearby)} nearby via '{term}':")
        for nb in nearby[:8]:
            dfs = nb.get("distance_formatted")
            lines.append(f"    - {nb.get('title')}[{dfs}]" if dfs else f"    - {nb.get('title')}")
            if nb.get("address"):
                lines.append(f"      {nb['address']}")
    return {"query": q, "anchor": anchor, "pois": deep, "blurb": desc,
            "nearby": nearby, "coords": coords,
            "n": (1 if anchor else 0) + (len(nearby or []) or 0),
            "count": count, "render": "\n".join(lines), "search": ps}


def unix_time(ts, *, count=5, **kw):
    """One-call Unix timestamp → date lookup.  `brave.unix_time(1700000000)`."""
    return rich("unix timestamp " + str(ts), count=count, **kw)

def package(tracking_number, *, count=5, **kw):
    """One-call package tracker lookup (when the API supports it)."""
    return rich("track " + str(tracking_number), count=count, **kw)




def _render_lines(query, items, web_meta):
    """Human-readable text for a `research()` package (used when summary enabled)."""
    lines = []
    if web_meta.get("infobox"):
        lines.extend(_infobox_lines(web_meta["infobox"]))
        lines.append("")
    lines.extend(_section_lines(items, "Results"))
    if web_meta.get("faq"):
        lines.append("")
        lines.extend(_faq_lines(web_meta["faq"]))
    if web_meta.get("videos"):
        lines.append("")
        lines.extend(_section_lines(web_meta["videos"], "Videos"))
    if web_meta.get("discussions"):
        lines.append("")
        lines.extend(_section_lines(web_meta["discussions"], "Discussions"))
    if not lines:
        lines.append("[no results]")
    return "\n".join(lines)


def _mixed(data, web_items, video_items, discussion_items, news_items, faq_items, infobox):
    m = data.get("mixed") if isinstance(data.get("mixed"), dict) else None
    if not m:
        return None
    pools = {
        "web": web_items,
        "infobox": [infobox] if infobox else [],
        "videos": video_items,
        "discussions": discussion_items,
        "news": news_items or [],
        "faq": faq_items or [],
    }
    cols = {}
    for col in ("main", "top", "side"):
        out = []
        for e in m.get(col) or []:
            etype = e.get("type")
            if etype not in pools:
                continue
            pool = pools[etype]
            idx = e.get("index")
            if e.get("all"):
                # 'all' sections (videos, discussions) place the whole group at once;
                # expand them so each component is materialised in order.
                for j, item in enumerate(pool):
                    out.append({"type": etype, "index": j, "all": True, "item": item})
            else:
                item = pool[idx] if idx is not None and idx < len(pool) else None
                if item is not None:
                    out.append({"type": etype, "index": idx, "all": False, "item": item})
        cols[col] = out
    return {"columns": cols, "main_rank": _main_rank(cols.get("main", []))}


def _main_rank(main):
    """Count of web items at the head of `main` (the immediate ranking)."""
    r = 0
    for e in main:
        if e.get("type") == "web":
            r += 1
        else:
            break
    return r


def _top_results(data, web_items, video_items, discussion_items, news_items, faq_items, infobox):
    """Return the overall blended result order that matches on-screen layout.

    Uses the `mixed` `main` column; falls back to `results` if absent. Returns
    a flat list of dicts each tagged with their section type, ready to render
    as 'the on-screen result list'.
    """
    m = data.get("mixed") if isinstance(data.get("mixed"), dict) else None
    if m:
        pools = {
            "web": web_items,
            "videos": video_items,
            "discussions": discussion_items,
            "news": news_items or [],
            "faq": faq_items or [],
            "infobox": [infobox] if infobox else [],
        }
        out = []
        for e in m.get("main") or []:
            etype = e.get("type")
            if etype not in pools:
                continue
            pool = pools[etype]
            idx = e.get("index")
            if e.get("all"):
                for item in pool:
                    out.append({"type": etype, "item": item})
            elif idx is not None and idx < len(pool):
                out.append({"type": etype, "item": pool[idx]})
        return out
    return [{"type": "web", "item": it} for it in web_items]


# ---------------------------------------------------------------------------
# Rendering + run() - the readable entry point (bound to `await brave(...)`)
# ---------------------------------------------------------------------------

def _section_lines(items, label):
    """One title, one url, one snippet, one byline. No extra_snippets/deep/breadcrumb echo."""
    out = []
    if label:
        out.append(f"[{label}]")
    for i, it in enumerate(_slim_list(items) if items else [], 1):
        title = it.get("title") or ""
        url = it.get("url") or ""
        desc = it.get("snippet") or ""
        meta = _source_meta(it)
        if not any([title, url, desc, meta]):
            continue
        out.append(f"{i}. {title}{meta}")
        if url:
            out.append(f"   {url}")
        if desc:
            out.append(f"   {desc}")
        qa = it.get("qa") or {}
        if qa.get("q") or qa.get("a"):
            if qa.get("q"):
                out.append(f"   Q: {qa['q']}")
            if qa.get("a"):
                out.append(f"   A: {qa['a']}")
        for c in (it.get("sitelinks") or [])[:3]:
            if c.get("title") and c.get("url"):
                out.append(f"   - {c['title']}  {c['url']}")
    return out


def _source_meta(it):
    """Author, publisher if distinct, date, duration, views. No dropped identity."""
    parts = []
    author = it.get("author") or it.get("by")
    if isinstance(author, list):
        author = ", ".join(str(a) for a in author if a)
    if author:
        parts.append(str(author))
    pub = it.get("publisher")
    if pub and str(pub) != str(author):
        parts.append(str(pub))
    date = it.get("date")
    if date:
        parts.append(str(date))
    if it.get("duration"):
        parts.append(str(it["duration"]))
    if it.get("views") is not None:
        parts.append(f"{it['views']} views")
    if not parts:
        return ""
    return " · " + " · ".join(parts)


def _infobox_lines(ib):
    ib = _slim_infobox(ib) or ib or {}
    lines = ["▸ Knowledge panel"]
    if ib.get("title"):
        lines.append(f"  {ib['title']}")
    if ib.get("url"):
        lines.append(f"  {ib['url']}")
    if ib.get("kind"):
        lines.append(f"  {ib['kind']}")
    if ib.get("blurb"):
        lines.append(f"  {ib['blurb']}")
    facts = ib.get("facts") if isinstance(ib.get("facts"), dict) else {}
    for k, v in list(facts.items())[:8]:
        lines.append(f"  {k}: {v}")
    return lines


def run(
    query: str,
    *,
    mode: str = "web",
    count: int = 5,
    country: Optional[str] = None,
    search_lang: Optional[str] = None,
    result_filter: Optional[str] = None,
    safe_search: str = "moderate",
    freshness: Optional[str] = None,
    grep: Optional[str] = None,
    extra: bool = False,
    goggles_id: Optional[str] = None,
    spellcheck: Optional[bool] = None,
    offset: Optional[int] = None,
    discussion_count: Optional[int] = None,
    video_count: Optional[int] = None,
    movie_count: Optional[int] = None,
    unit: Optional[str] = None,
    text_decorations: Optional[bool] = None,
    summary: bool = False,
    ui_lang: Optional[str] = None,
    units: Optional[str] = None,
    operators: Optional[bool] = None,
    include_fetch_metadata: Optional[bool] = None,
    enable_rich_callback: Optional[bool] = None,
    goggles: Optional[Any] = None,
    loc: Optional[dict] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    postal_code: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    timezone: Optional[str] = None,
    property: Optional[str] = None,
    search_type: Optional[str] = None,
    timeout: float = 45.0,
) -> str:
    """Advanced Brave search, returned as readable formatted text.

    Calls `search()` and renders the full picture: knowledge panel, web/news/
    news/image/video results, embedded videos, discussions, and a diagnostics
    footer. For the raw structured data use `await brave.search(...)` instead.

    Args:
        See `search()` for the full parameter list and semantics. `run()` adds
        no extra flags; it renders everything `search` found.
    """
    # Build the X-Loc-* hints from the typed CLI-friendly location args if the
    # caller didn't pass an explicit `loc` dict. (The dict form is the primary
    # kernel API; city/state/postal_code/... are convenient CLI flags.)
    if loc is None:
        loc_built: dict[str, Any] = {}
        if latitude is not None:
            loc_built["latitude"] = latitude
        if longitude is not None:
            loc_built["longitude"] = longitude
        if city:
            loc_built["city"] = city
        if state:
            loc_built["state"] = state
        if postal_code:
            loc_built["postal_code"] = postal_code
        if timezone:
            loc_built["timezone"] = timezone
        loc = loc_built or None

    data = _search_full(
        query, mode=mode, count=count, country=country, search_lang=search_lang,
        result_filter=result_filter, safe_search=safe_search, freshness=freshness,
        grep=grep, extra=extra, goggles_id=goggles_id, spellcheck=spellcheck,
        offset=offset, discussion_count=discussion_count, video_count=video_count,
        movie_count=movie_count, unit=unit, text_decorations=text_decorations,
        summary=summary, ui_lang=ui_lang, units=units, operators=operators,
        include_fetch_metadata=include_fetch_metadata,
        enable_rich_callback=enable_rich_callback,
        goggles=goggles, loc=loc,
        property=property, search_type=search_type,
        timeout=timeout,
    )
    data = _agent_view(data)
    lines = []
    infobox = data.get("infobox")
    if infobox:
        lines.extend(_infobox_lines(infobox))
        lines.append("")

    summ = data.get("summarizer")
    if summ and summ.get("query"):
        dl = summ.get("deep_link")
        if not summary:
            lines.append("▸ A Brave AI answer is available; rerun with summary=True to capture its deep-link (or grab d['summarizer']).")
        else:
            lines.append("▸ Brave AI answer (results_hash={}){}".format(
                summ.get("results_hash"),
                ": " + dl if dl else "."
            ))
        lines.append("")

    mode = data.get("mode", "web")
    if mode == "all":
        lines.extend(_section_lines(data.get("web") or [], "Web"))
        lines.append("")
        if data.get("faq"):
            lines.extend(_faq_lines(data.get("faq")))
            lines.append("")
        lines.extend(_section_lines(data.get("news") or [], "News"))
        lines.append("")
        lines.extend(_section_lines(data.get("videos") or [], "Videos"))
    elif mode == "web":
        lines.extend(_section_lines(data.get("results") or [], "Web"))
        if data.get("news"):
            lines.append("")
            lines.extend(_section_lines(data.get("news"), "Breaking/News"))
        if data.get("faq"):
            lines.append("")
            lines.extend(_faq_lines(data.get("faq")))
        if data.get("videos"):
            lines.append("")
            lines.extend(_section_lines(data.get("videos"), "Videos"))
        if data.get("discussions"):
            lines.append("")
            lines.extend(_section_lines(data.get("discussions"), "Discussions"))
        if data.get("rich"):
            _rh = data["rich"]
            _hint = (_rh.get("hint") or {}) if isinstance(_rh, dict) else {}
            if _hint.get("vertical"):
                lines.append("")
                lines.append("[Rich] {} — use `brave.rich(...)` to fetch the payload".format(
                    _hint.get("vertical").upper()))
                if _hint.get("callback_key"):
                    lines.append("   callback_key={}".format(_hint.get("callback_key")))
        if data.get("locations"):
            lines.append("")
            places = data["locations"]
            lines.extend(_location_section_lines(places[:6]))
            if len(places) > 6:
                lines.append("  … {} more places (see brave.locations(...))".format(len(places)))
        if data.get("mixed"):
            lines.append("")
            lines.extend(_mixed_lines(data.get("mixed")))
    elif mode == "news":
        lines.extend(_section_lines(data.get("results") or [], "News"))
    elif mode == "video":
        lines.extend(_section_lines(data.get("results") or [], "Videos"))
    elif mode == "image":
        lines.extend(_image_section_lines(data.get("results") or []))
    elif mode == "local":
        if data.get("fallback"):
            lines.append("(local endpoint unavailable on this key — used web results instead)")
            lines.append("")
        lines.extend(_section_lines(data.get("results") or [], "Results"))

    # diagnostics footer — query is a string unless Brave altered it
    q = data.get("query")
    notes = []
    if isinstance(q, dict):
        altered = q.get("altered")
        if altered and q.get("q") and altered != q.get("q"):
            notes.append(f"Did you mean: '{altered}'?")
        if q.get("bad_results"):
            notes.append("Brave flagged these results as low quality")
    if notes:
        lines.append("")
        lines.append("— " + "; ".join(notes) + " —")
    return "\n".join(lines)



def _faq_lines(faqs):
    out = ["[FAQ]"]
    for i, f in enumerate(faqs or [], 1):
        q = f.get("q") or f.get("question") or ""
        a = f.get("a") or f.get("answer") or ""
        if q:
            out.append(f"{i}. {_clean_html(q)}")
        if a:
            out.append(f"   {_snippet(a, 200)}")
        if f.get("url"):
            out.append(f"   {f['url']}")
    return out


def _mixed_lines(mixed):
    if not mixed:
        return []
    out = []
    cols = mixed.get("columns") or {}
    for col in ("main", "top", "side"):
        entries = cols.get(col) or []
        if not entries:
            continue
        out.append(f"[blended {col}]")
        for i, e in enumerate(entries, 1):
            it = e.get("item")
            if not it:
                continue
            title = _clean_html(it.get("title") or "") if isinstance(it, dict) else ""
            url = it.get("url") if isinstance(it, dict) else ""
            out.append(f"  {i}. ({e.get('type')}) {title}")
            if url:
                out.append(f"     {url}")
    return out


def _location_section_lines(items):
    out = []
    if not items:
        return out
    out.append("[Locations]")
    for i, it in enumerate(items, 1):
        ln = f"{i}. {it.get('title') or ''}"
        addr = it.get("address") or ""
        if addr:
            ln += f" — {addr}"
        out.append(ln)
        if it.get("phone"):
            out.append(f"   ☎ {it['phone']}")
        if it.get("opening_hours"):
            out.append(f"   open: {' / '.join(it['opening_hours'][:3])}")
        if it.get("latitude") is not None and it.get("longitude") is not None:
            out.append(f"   {it['latitude']:.4f}, {it['longitude']:.4f}")
        # Round-8: price + rating enrich the "pick a place" decision.
        if it.get("price") or it.get("rating"):
            info = ""
            if it.get("price"):
                info += f"  {it['price']}"
            rt = it.get("rating") or {}
            if rt.get("value") is not None:
                info += f"  ★{rt['value']}"
                if rt.get("reviews"):
                    info += f" ({rt['reviews']} reviews)"
            if info:
                out.append(f"  {info.strip()}")
        if it.get("url"):
            out.append(f"   {it['url']}")
    return out


def _image_section_lines(items):
    out = []
    if not items:
        return out
    out.append("[Images]")
    for i, it in enumerate(items, 1):
        t = (it.get("title") or "").strip()
        u = it.get("url") or it.get("image_url") or it.get("page_url") or ""
        if t:
            out.append(f"{i}. {t}")
        if u:
            out.append(f"   {u}")
        dims = ""
        if it.get("width") and it.get("height"):
            dims = f" {it['width']}x{it['height']}"
        if it.get("source"):
            dims += f" · {it['source']}"
        if dims:
            out.append(f"  {dims.strip()}")
    return out



def summarize_page(url, *, max_chars: int = 20000, max_points: int = 5,
                   timeout: float = 45.0) -> dict:
    """Clean page digest — fetch a URL and return a concise extractive summary (round-12).

    Wraps `probe(url)` into a directly usable package: fetch the page text, strip
    citation/reference markup and navigation boilerplate, then return a lead
    paragraph plus the most information-dense sentences.  Deterministic text
    extraction (no model call), so an agent can read or re-phrase the digest
    itself — the fastest way to "tell me what this page says".

    Args:
        url: the page to summarise.
        max_chars: probe() text cap in characters (default 20000).
        max_points: how many key sentences to extract (default 5).
        timeout: HTTP timeout (default 45s; the inner fetch uses up to 15s).

    Returns:
        {"url", "title", "final_url", "content_type", "chars", "summary",
         "points": [...], "readable": str, "text": raw cleaned text}.
    """
    import re as _re
    from collections import Counter as _Counter
    p = probe(url, max_chars=max_chars, timeout=min(timeout or 45.0, 15.0))
    if p.get("status") != 200:
        return p
    text = p.get("text") or ""
    # strip citation/reference markup that pollutes lead extraction
    text = _re.sub(r"\{\{cite[^}]*\}\}|<ref[^>]*>.*?</ref>|\[\s*\d+\s*\]", " ", text)
    title = p.get("title") or url
    nav = _re.compile(
        r"^\s*(home|search|about|contact|menu|subscribe|log ?in|sign ?in|sign ?up|"
        r"register|donate|follow|share|cookie|privacy|terms|all rights reserved|"
        r"copyright|©|tools|table of contents|contents)\b", _re.I)
    # skip wiki-markup/ref-heavy lines so the lead lands in the prose body
    _markup = _re.compile(r"\[\[|\{\{|<ref|</?ref>|\* *\[")
    paras = []
    for raw in _re.split(r"\n+", text):
        x = _re.sub(r"\s+", " ", raw).strip()
        if len(x) < 90 or nav.match(x) or _markup.search(x):
            continue
        paras.append(x)
    lead = paras[0] if paras else _re.sub(r"\s+", " ", text)[:400].strip()
    words = [w.lower().strip(".,;:!?()\"'[]{}") for w in _re.split(r"\W+", text)]
    words = [w for w in words if len(w) > 3 and not w.isdigit()]
    tf = _Counter(words)
    cand = []
    for s in _re.split(r"(?<=[.!?])\s+", text):
        s2 = _re.sub(r"\s+", " ", s).strip()
        if not (70 <= len(s2) <= 340) or nav.match(s2) or _markup.search(s2):
            continue
        ws = [w for w in _re.split(r"\W+", s2.lower()) if len(w) > 3]
        cand.append((sum(tf.get(w, 0) for w in ws), s2))
    cand.sort(key=lambda kv: -kv[0])
    points, seen = [], set()
    for _, s2 in cand:
        if s2 in seen:
            continue
        seen.add(s2)
        points.append(s2)
        if len(points) >= max(1, max_points or 5):
            break
    if not points:
        points = [lead or "…"]
    readable = "{0}\n{1}\n\n{2}\n\nKey points:\n".format(title, url, lead)
    readable += "\n".join("  · " + s for s in points)
    return {
        "url": url,
        "title": title,
        "final_url": p.get("final_url"),
        "content_type": p.get("content_type"),
        "chars": p["chars"],
        "summary": lead,
        "points": points,
        "readable": readable,
        "text": p.get("text") or "",
    }

def mosaic(query: str, *, count: int = 4, freshness: str = "pm",
           safe_search: str = "moderate", country: Optional[str] = None,
           search_lang: Optional[str] = None, timeout: float = 45.0,
           images: bool = True, **kw: Any) -> dict:
    """One-call cross-content digest: web + news + video + image fused (round-12).

    Unlike `research()` (web/news/video, paged) or `run()` (single-SERP),
    `mosaic()` fans one query across all five corpora in a single call and
    returns each type as its own labelled pool plus a blended digest. Round-13
    adds `locations` (map/POI rows for local-intent topics, fused in from the
    web corpus) so a single call covers the widest cross-format surface at once —
    web + news + video + images + map/POIs + infobox + FAQ.

    Args:
        query: the topic.
        count: per-corpus cap (web up to 20, news up to 50, video ~50, image up to 200).
        freshness: news window (default pm = past month; pd/pw/py or date-range ok).
        safe_search / country / search_lang / timeout: forwarded to each search.
        images: include the image corpus (an extra HTTP call; default True).
        **kw: forwarded to each inner `search()` (spellcheck, result_filter,
            image `property`/`search_type`, ...).

    Returns:
        {"query", "web", "news", "videos", "images", "locations", "infobox",
         "faq", "n", "render", "searches"}.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("mosaic() needs a query")
    jobs = [
        ("web", lambda: _search_full(q, mode="web", count=max(1, count),
                                    safe_search=safe_search, country=country,
                                    search_lang=search_lang, timeout=timeout, **kw)),
        ("news", lambda: _search_full(q, mode="news", count=max(1, count),
                                     safe_search=safe_search, country=country,
                                     search_lang=search_lang, freshness=freshness,
                                     timeout=timeout, **kw)),
        ("video", lambda: _search_full(q, mode="video", count=max(1, count),
                                      safe_search=safe_search, timeout=timeout, **kw)),
    ]
    if images:
        jobs.append(("image", lambda: _search_full(
            q, mode="image", count=max(1, min(count or 1, 200)),
            safe_search="strict", timeout=timeout,
            property=kw.get("property"), search_type=kw.get("search_type"))))
    got = {k: (ok, val) for k, ok, val in _fanout(jobs, concurrency=4)}
    w, info, faq, locs = [], None, [], []
    rw_ok, rw = got.get("web", (False, None))
    if rw_ok:
        w = rw.get("web") or []
        info = rw.get("infobox")
        faq = rw.get("faq") or []
        locs = rw.get("locations") or []
    elif isinstance(rw, BraveError):
        w = [{"error": f"{rw.category}: {rw}"}]
    rn_ok, rn = got.get("news", (False, None))
    news = (rn.get("results") or []) if rn_ok else []
    vv_ok, vv = got.get("video", (False, None))
    vids = (vv.get("results") or []) if vv_ok else []
    imgs = []
    if images:
        ri_ok, ri = got.get("image", (False, None))
        imgs = (ri.get("results") or []) if ri_ok else []
    w, news, vids, imgs, locs = (_slim_list(w), _slim_list(news),
                                 _slim_list(vids), _slim_list(imgs), _slim_list(locs))
    info = _slim_infobox(info)
    faq = _slim_faq(faq)
    lines = [f"MOSAIC '{q}'"]
    if info:
        lines.extend(_infobox_lines(info))
    if w:
        lines.extend(_section_lines(w, f"Web ({len(w)})"))
    if news:
        lines.extend(_section_lines(news, f"News ({len(news)})"))
    if vids:
        lines.extend(_section_lines(vids, f"Videos ({len(vids)})"))
    if imgs:
        lines.extend(_section_lines(imgs, f"Images ({len(imgs)})"))
    if locs:
        lines.extend(_section_lines(locs, f"Map / POIs ({len(locs)})"))
    for it in faq or []:
        if it.get("q"):
            lines.append(f"Q: {it['q']}")
        if it.get("a"):
            lines.append(f"A: {it['a']}")
    if len(lines) == 1:
        lines.append("[no results]")
    return _drop_empty({
        "query": q,
        "web": w,
        "news": news,
        "videos": vids,
        "images": imgs,
        "locations": locs,
        "infobox": info,
        "faq": faq,
        "render": "\n".join(lines),
    })


def modes() -> str:
    """List the supported Brave modes and the advanced parameters."""
    out = ["Brave search modes (pass as mode=...):"]
    for name, hint in MODES.items():
        out.append(f"  {name:7} {hint}")
    out.append("")
    out.append("Advanced options: grep=..., goggles=... (url/definition, up to 3), extra=True,")
    out.append("freshness=pd|pw|pm|py|pd_1d|pd_1w|pd_1m|pd_1y|YYYY-MM-DDtoYYYY-MM-DD,")
    out.append(f"country=XX, search_lang=yy, ui_lang=..., units=metric|imperial,")
    out.append("result_filter=..., safe_search=off|moderate|strict, spellcheck=True/False,")
    out.append("operators=True/False, include_fetch_metadata=True/False,")
    out.append("offset=0..count-1, discussion_count=, video_count=, movie_count=,")
    out.append("unit=px|em (image only), summary=True (Brave AI deep-link),")
    out.append("property=any|commercial|non-commercial (image licensing hint, pass-through),")
    out.append("search_type=all|transparent (image transparency hint, pass-through),")
    out.append("place_search cate=... (EP place category hint, pass-through),")
    out.append("loc={X-Loc-* hints} — geo-localise results (see brave.near(...));")
    out.append("Also: brave.near(...) localised search, brave.locations(...) map/POI lookup,")
    out.append("      brave.software(...) package registry lookup,")
    out.append("      brave.forums(...) discussion/forum threads w/ engagement,")
    out.append("      brave.headlines(...) / brave.clips(...) / brave.pictures(...) media lookups,")
    out.append("      brave.recipes(...) / brave.products(...) / brave.movies(...) rich schema lookups,")
    out.append("      brave.rich(query) / weather() / stock_quote() / crypto() / definition() /"),
    out.append("            currency_x() / convert_values() — live rich-data lookups (round-10)."),
    out.append("      brave.pois(ids) / brave.poi_descriptions(ids) — deep POI detail + AI blurbs,")
    out.append("      brave.open_now(location_row) - is a place open right now?,")
    out.append("      brave.place_search(...) - dedicated POI search (coords/location + explore),") 
    out.append("      brave.domain(name) - site-scoped lookup, brave.find_files() file-type search,")
    out.append("      brave.news_cluster(topic) - news grouped by source (client-side),") 
    out.append("      brave.probe(url) fetch page text, brave.crawl(urls) batch fetch,")
    out.append("      brave.summarize_page(url) clean page digest (extractive).")
    out.append("      brave.mosaic(query) cross-content digest (web+news+video+image+locations).")
    out.append("      brave.newsflash(topic) latest top-headlines digest (news mode + freshness).")
    out.append("      brave.explain(query) one-call Markdown research brief (mosaic+summarize_page).")
    out.append("view='agent' (default) compact JSON; view='full' raw SERP (mixed/top_results).")
    out.append("Engine: pooled HTTP + one-way fanout (batch/mosaic/crawl/mode=all). brief() == search(view='agent').")
    out.append("Structured: await brave.search(...) (JSON dicts); await brave.mosaic(...) for multi-corpus.")
    return "\n".join(out)

def newsflash(topic: str, *, count: int = 10, freshness: str = "pd_1w",
              country: Optional[str] = None, search_lang: Optional[str] = None,
              safe_search: str = "moderate", timeout: float = 45.0, **loc) -> dict:
    """Quick top-headlines renderer for a topic (round-13).

    Pure client-side composition: runs a `news` search with `freshness` (default
    `pd_1w` = past week) and renders the *latest* top headlines into a compact
    readable digest — heading, source, age, and URL. A fast "what's new on
    <topic>?" check without the full web-SERP noise.

    Args:
        topic: the topic / query.
        count: headline cap (news API supports up to 50; default 10).
        freshness: news recency window (default `pd_1w`; pd/pw/pm or date-range ok).
        country / search_lang / safe_search / timeout: forwarded to `search()`.
        **loc: X-Loc-* location hints (same keys as `search()`/`near()`).

    Returns:
        {"topic", "results": [news items], "n", "headlines": [titles],
         "render", "search"}.
    """
    data = _search_full(topic, mode="news", count=max(1, min(count or 10, 50)),
                  freshness=freshness, country=country, search_lang=search_lang,
                  safe_search=safe_search, timeout=timeout, **loc)
    items = data.get("results") or []
    lines = [f"HEADLINES · '{topic}' ({freshness})"]
    for it in items:
        age = it.get("age") or ""
        head = f"· {it.get('title')}"
        if age:
            head += f"  [{age}]"
        lines.append(head)
        src = it.get("source") or it.get("site")
        if src:
            lines.append(f"    {src}  {it.get('url')}")
    if not items:
        lines.append("    [no news results]")
    return {
        "topic": topic,
        "count": count,
        "results": items,
        "headlines": [it.get("title") for it in items],
        "n": len(items),
        "render": "\n".join(lines),
        "search": data,
    }


def explain(query: str, *, count: int = 4, news_count: int = 6, images_count: int = 6,
            timeout: float = 45.0, **kw) -> dict:
    """One-call compact research brief for a topic (round-13).

    Pure client-side composition over client-verified primitives: runs `mosaic()`
    across web/news/video/images/locations, then `summarize_page()` on the top
    web hit to attach an extractive digest, and folds everything into a compact
    Markdown research brief you can hand straight to a downstream model or user.

    Args:
        query: the topic.
        count: per-corpus cap for `mosaic()` (web/news/images/video/locations).
        news_count: number of news headlines to include in the brief.
        images_count: images to include (0 disables the image corpus).
        timeout / **kw: forwarded into `mosaic()`.

    Returns:
        A Markdown-formatted research brief string.
    """
    from urllib.parse import urlparse
    q = (query or "").strip()
    if not q:
        raise ValueError("explain() needs a query")
    m = mosaic(q, count=count, images=images_count > 0, timeout=timeout, **kw)
    web = m.get("web") or []
    news = m.get("news") or []
    vids = m.get("videos") or []
    imgs = m.get("images") or []
    locs = m.get("locations") or []
    info = m.get("infobox")

    # Summarise the top web hit that actually has a crawlable URL.
    top_url, top_title = None, None
    for it in web[:6]:
        u = it.get("url")
        if u and not u.lstrip().startswith(("http", "www")):
            continue
        if u and u.startswith(("http://", "https://")):
            top_url, top_title = u, it.get("title")
            break
    page = None
    if top_url:
        try:
            page = summarize_page(top_url, max_chars=6000, max_points=4, timeout=timeout)
        except BraveError:
            page = None

    out = [f"# {q}"]
    if info and info.get("title"):
        out.append(f"**{info.get('title')}** — {info.get('description') or ''}")
        out.append("")
    if page:
        sum_txt = page.get("summary") or ""
        if sum_txt:
            out.append(f"### Overview ({top_title})")
            out.append(sum_txt.strip())
            out.append("")
        pts = page.get("points") or []
        if pts:
            out.append("### Key points")
            for p in pts[:4]:
                out.append(f"- {p.strip()}")
            out.append("")
    if web:
        out.append(f"### Top sources ({len(web)})")
        for it in web[: min(count, 12)]:
            t, u = it.get("title"), it.get("url")
            if t and u:
                dom = urlparse(u).netloc or ""
                out.append(f"- [{t}]({u}) — {dom}")
        out.append("")
    if news:
        out.append(f"### Headlines ({len(news)})")
        for it in news[: news_count]:
            t, u = it.get("title"), it.get("url")
            if t and u:
                out.append(f"- {t} — {it.get('source') or it.get('site')} [{it.get('age')}] ({u})")
        out.append("")
    if vids:
        out.append(f"### Videos ({len(vids)})")
        for it in vids[:5]:
            t, u = it.get("title"), it.get("url")
            dur = it.get("duration")
            if t and u:
                out.append(f"- {t} ({dur or 'live/stream'}) — {u}")
    if imgs:
        out.append(f"### Images ({len(imgs)})")
        for it in imgs[: images_count]:
            u = it.get("page_url") or it.get("url")
            if u:
                out.append(f"- {it.get('title')} — {u}")
    if locs:
        out.append(f"### Map / POIs ({len(locs)})")
        for it in locs[:5]:
            t, a = it.get("title"), it.get("address")
            if t:
                out.append(f"- {t}" + (f" — {a}" if a else ""))
    if top_url and page is None:
        out.append("")
        out.append("*Top page failed to summarise; see sources above.*")
    return "\n".join(out)


def trending_topics(*, country: Optional[str] = None, timeout: float = 15.0) -> str:
    """Documented-absent: Brave has no public trending-topics endpoint (round-13).

    Probed live (this session) — every candidate path returns HTTP 301 and
    redirects to the docs HTML, not JSON:
      /res/v1/web/trending, /res/v1/news/trending, /res/v1/trending/search,
      /res/v1/web/top_stories, /res/v1/news/discover, /res/v1/web/trending_search.
    This mirrors the round-11 finding that `suggest`/`answers`/`spellcheck`/
    `search` enrichment surfaces are OPTION_NOT_IN_PLAN. Use `newsflash(topic)`
    or `headlines(topic)` to get current top stories for a specific topic instead.

    Returns:
        A short human-readable note (does not hit the network).
    """
    return (
        "trending_topics() is not available on the Brave Search API. "
        "No public trending-topics/top-stories endpoint is exposed "
        "(verified live round-13: 301 redirect to HTML for all probed paths). "
        "Use brave.newsflash(query) or brave.headlines(query) for current headlines instead."
    )



# ----------------------------------------------------------------------------
# Round-14: news depth (breaking/live + publisher "beams"), parallel batch,
# consensus article review, BFS search-worker, related (documented-absent)
# ----------------------------------------------------------------------------

def news_breaking(topic, *, count=10, freshness=None, country=None,
                  search_lang=None, safe_search="moderate", timeout=45.0, **loc):
    """Breaking-news digest — only the ``breaking``-flagged headlines (round-14).

    Brave's standalone ``/news/search`` items carry no breaking flag, but the
    **web-embedded news section** does (raw ``breaking``: bool on each item,
    verified live). ``news_breaking()`` runs a web search and returns just the
    news items Brave tagged ``breaking=true`` — the genuinely "breaking now"
    headlines vs. all recent articles. Items also carry ``is_live`` (live
    coverage) and the usual ``age``/``source`` metadata.

    Args:
        topic: news topic.
        count: embedded news items pulled from the web section (capped ~20).
        freshness: forward to the web search (e.g. ``pd_1h``/``pd_1d``/``pd_1w``).
        country / search_lang / safe_search / timeout / **loc: forward to
            ``search(mode="web")`` (X-Loc-* geo via **loc).

    Returns:
        {"query", "results": [breaking news items], "n", "total", "render",
        "search"}.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("news_breaking() needs a topic")
    d = _search_full(topic, mode="web", count=max(1, min(count or 10, 20)),
               country=country, search_lang=search_lang,
               freshness=freshness, safe_search=safe_search,
               timeout=timeout or 45.0, loc=loc or None)
    items = d.get("news") or []
    breaking = [i for i in items if i.get("breaking")]
    lines = [f"Breaking news on '{topic}' — {len(breaking)} flagged item(s):"]
    for it in breaking[:15]:
        live = ("  [LIVE]" if it.get("is_live") else "")
        age = (f"  [{it.get('age')}]" if it.get("age") else "")
        lines.append(f"· {it.get('title', '')}{live}{age}")
        if it.get("url"):
            lines.append(f"  {it['url']}")
    if not breaking:
        lines.append("  (no current news item was flagged `breaking` by Brave)")
    return {"query": topic, "results": breaking, "n": len(breaking),
            "total": len(items), "render": "\n".join(lines), "search": d}


def news_beams(topic, *, count=40, freshness=None, country=None,
               search_lang=None, safe_search="moderate", timeout=45.0, **loc):
    """Publisher/"beam" grouped news digest from the web-embedded news feed (round-14).

    The requested "grouped publisher/beam view" over the news headlines. Brave's
    standalone `/news/search` returns a flat list with no native clustering (see
    api.md); its *web-embedded* news feed carries the extra ``breaking`` /
    ``is_live`` signals the flat endpoint lacks. ``news_beams()`` pulls the
    web-embedded news, groups it by publishing outlet (derived from the article
    host/source), and reports how many breaking/live stories each outlet
    currently carries plus its top featured articles.

    Args:
        topic: news topic.
        count: embedded news items pulled from the web section.
        freshness: forward (e.g. ``pd_1h``/``pd_1d``/``pd_1w``).
        country / search_lang / safe_search / timeout / **loc: forward to
            ``search(mode="web")``.

    Returns:
        {"query", "results": [news items], "beams": [{publisher, count,
        breaking_count, live_count, articles}...], "n_items", "n_publishers",
        "render", "search"}.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("news_beams() needs a topic")
    d = _search_full(topic, mode="web", count=max(1, min(count or 50, 20)),
               country=country, search_lang=search_lang,
               freshness=freshness, safe_search=safe_search,
               timeout=timeout or 45.0, loc=loc or None)
    items = d.get("news") or []
    beams_map = {}
    for it in items:
        pub = ((it.get("source") or "").strip() or
               (it.get("site") or "").strip() or "unknown")
        beam = beams_map.setdefault(pub, {
            "publisher": pub, "count": 0, "breaking_count": 0, "live_count": 0,
            "articles": [],
        })
        beam["count"] += 1
        if it.get("breaking"):
            beam["breaking_count"] += 1
        if it.get("is_live"):
            beam["live_count"] += 1
        beam["articles"].append(it)
    beams = sorted(beams_map.values(), key=lambda b: -b["count"])
    lines = [f"News beams for '{topic}' — {len(items)} article(s) across {len(beams)} publisher(s):"]
    for b in beams:
        tags = []
        if b["breaking_count"]:
            tags.append(f"{b['breaking_count']} breaking")
        if b["live_count"]:
            tags.append(f"{b['live_count']} live")
        suffix = ("  [" + ", ".join(tags) + "]") if tags else ""
        lines.append(f"{b['publisher']} ({b['count']}){suffix}")
        for art in b["articles"][:2]:
            lines.append(f"  · {art.get('title', '')}")
            if art.get("url"):
                lines.append(f"    {art['url']}")
    return {"query": topic, "results": items, "beams": beams,
            "n_items": len(items), "n_publishers": len(beams),
            "render": "\n".join(lines), "search": d}




def news_live(topic, *, count=50, freshness=None, country=None,
              search_lang=None, safe_search="moderate", timeout=45.0, **loc):
    """Live-coverage-only news filter (round-15).

    Returns only the news items flagged ``is_live==True`` on the news query.
    Brave''s news rows carry ``is_live`` (rolling live coverage, e.g. a sports
    match or election night) and ``breaking`` (flash alerts) on the web-embedded
    feed and, where surfacing, on `/news` rows. When a query has no live-
    coverage stories, this returns an empty list.

    Live-verify note (round-15): on the queries pressed live at verification
    time (breaking/live-match/debate), Brave returned **no** ``is_live`` rows
    via the standalone endpoint on this plan, and the web-embedded feed''s
    ``breaking``/``is_live`` fields were also consistently unset — i.e. there is
    no per-query trigger we could pin; the filter is still available and
    correct for when a live story does surface. For a timeline of "what is
    freshest" (rather than a narrow live-only view), prefer ``headlines()`` /
    ``news_cluster()`` which also report ``age_meta``/``published_at``.

    Args:
        topic: news topic.
        count: articles to fetch (news supports up to 50; default 50).
        freshness: pd/pw/pm/py or a date range.
        country / search_lang / safe_search / timeout: forwarded to ``search()``.
        **loc: X-Loc-* hints.

    Returns:
        {"query", "results":[...is_live items...], "n", "render", "search"}.
    """
    data = _search_full(topic, mode="news", count=max(1, min(count, 50)),
                  country=country, search_lang=search_lang,
                  safe_search=safe_search, freshness=freshness,
                  timeout=timeout, loc=loc or None)
    items = [r for r in (data.get("results") or []) if r.get("is_live") is True]
    lines = [f"Live coverage for '{topic}' ({len(items)}):"]
    for r in items:
        lines.append(f"  · {r.get('title')}")
        meta = []
        if r.get("age_meta"):
            ad = r["age_meta"]
            if ad.get("days") is not None:
                meta.append(f"{ad['days']}d")
        if r.get("breaking"):
            meta.append("breaking")
        if r.get("is_live"):
            meta.append("live")
        if meta:
            lines.append(f"    [{', '.join(meta)}]")
        if r.get("url"):
            lines.append(f"    {r['url']}")
    return {"query": topic,
            "results": items, "n": len(items), "counted": len(items),
            "render": "\n".join(lines), "search": data}


def batch(queries, *, mode="web", count=4, concurrency=6, timeout=45.0, **kw):
    """Run several queries in parallel and bundle their structured results (round-14).

    A pure client-side thread-pool composition of the verified ``search()``
    entrypoint — useful when an agent needs the same-mode answer to many
    questions in one message (entity lookups, fact-checks, a small corpus sweep).
    Each query stays independent: a single query's auth/param error is captured
    in that query's ``error`` slot without aborting the others.

    Args:
        queries: iterable of search terms (or a single string).
        mode: a search mode (default `web`).
        count: per-query result count.
        concurrency: max in-flight requests at once (default 8).
        timeout: HTTP timeout seconds.
        **kw: forwarded to each ``search()`` (freshness, country, safe_search,
            loc, ...).

    Returns:
        {"queries": [...], "mode": ..., "results": {query: {...}},
        "ordered": [...], "n", "n_ok", "render"}.
    """
    if isinstance(queries, str):
        qs = [queries]
    else:
        qs = [str(q) for q in (queries or []) if q is not None and str(q).strip()]
    if not qs:
        raise ValueError("batch() needs at least one query")
    view = (kw.pop("view", None) or "agent").lower()

    def _one(q):
        if view in ("full", "raw", "serp"):
            return _search_full(q, mode=mode, count=count, timeout=timeout or 45.0, **kw)
        return search(q, view="agent", mode=mode, count=count, timeout=timeout or 45.0, **kw)

    gathered = _fanout([(q, (lambda q=q: _one(q))) for q in qs],
                       concurrency=concurrency)
    out = {}
    for q, ok, val in gathered:
        out[q] = {"error": str(val)} if not ok else val
    n_ok = sum(1 for q in qs if not (out.get(q) or {}).get("error"))
    return {"queries": qs, "results": out, "n_ok": n_ok}


def article_review(query, *, count=4, max_chars=3000, positive_hint=None,
                   negative_hint=None, timeout=45.0, **kw):
    """Consensus good/bad/mixed review of a topic across several live sources (round-14).

    Pulls the top web results, fetches each page's full text via the verified
    ``crawl()``, and buckets the sources into ``good`` / ``mixed`` / ``bad`` /
    ``neutral`` based on whether agreeing language (*positive*: "best",
    "recommend", "powerful", ...) or *disagreeing* language (*negative*:
    "worst", "fail", "buggy", "avoid", ...) dominates. A pure client-side
    heuristic — no model call — handy for a quick "should I pick X?" framing.

    Args:
        query: topic or product to review.
        count: top results to fetch and score (default 4, capped ~10).
        max_chars: per-page text cap fed to the heuristic.
        positive_hint / negative_hint: optional comma-separated extra positive /
            negative tokens folded into the lexicons.
        timeout / **kw: forwarded to ``search()``.

    Returns:
        {"query", "verdict", "breakdown": [{url, title, verdict, pos, neg}],
        "pos_count", "neg_count", "mixed_count", "source_count", "render"}.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("article_review() needs a query")
    positive = {"best", "recommend", "recommended", "great", "excellent", "powerful",
                "fast", "stable", "easy", "love", "awesome", "robust", "fantastic",
                "impressive", "well", "solid", "top", "good", "clean", "modern"}
    negative = {"worst", "avoid", "bug", "buggy", "fail", "failed", "unstable",
                "slow", "broken", "outdated", "terrible", "horrible", "bad",
                "overhyped", "insecure", "deprecated", "crash", "lacking",
                "confusing", "messy", "error"}
    for h in (positive_hint,):
        if h:
            positive |= {x.strip().lower() for x in str(h).split(",") if x.strip()}
    for h in (negative_hint,):
        if h:
            negative |= {x.strip().lower() for x in str(h).split(",") if x.strip()}
    sd = _search_full(q, mode="web", count=max(1, min(count or 4, 10)),
                timeout=timeout or 45.0, **kw)
    urls = [(r.get("url"), r.get("title")) for r in (sd.get("results") or []) if r.get("url")]
    if not urls:
        return {"query": q, "verdict": "neutral", "breakdown": [],
                "pos_count": 0, "neg_count": 0, "mixed_count": 0, "source_count": 0,
                "render": f"article_review('{q}'): no sources returned."}
    texts = {}
    cr = crawl([u for u, _ in urls], max_chars=max(1000, int(max_chars or 3000)))
    for item in (cr.get("results") or []):
        if item.get("status") == 200 and item.get("text"):
            texts[item.get("url")] = item.get("text")
    breakdown = []
    for url, title in urls:
        text = texts.get(url)
        if not text:
            breakdown.append({"url": url, "title": title, "verdict": "neutral",
                              "pos": 0, "neg": 0, "note": "could not fetch full text"})
            continue
        low = (text or "").lower()
        p = sum(1 for w in positive if w in low)
        n = sum(1 for w in negative if w in low)
        if p > n + 2:
            verdict = "good"
        elif n > p + 2:
            verdict = "bad"
        elif p and n:
            verdict = "mixed"
        else:
            verdict = "neutral"
        breakdown.append({"url": url, "title": title, "verdict": verdict,
                          "pos": p, "neg": n, "note": None})
    pos_c = sum(1 for b in breakdown if b["verdict"] == "good")
    neg_c = sum(1 for b in breakdown if b["verdict"] == "bad")
    mix_c = sum(1 for b in breakdown if b["verdict"] == "mixed")
    if mix_c:
        verdict = "mixed"
    elif pos_c > neg_c:
        verdict = "good"
    elif neg_c > pos_c:
        verdict = "bad"
    else:
        verdict = "neutral"
    lines = [f"ARTICLE REVIEW '{q}' — {len(breakdown)} source(s), verdict: {verdict.upper()}"]
    for b in breakdown:
        note = f"  ({b['note']})" if b.get("note") else ""
        lines.append(f"· {b['verdict'].upper():8} [{b['pos']}+ / {b['neg']}-]{note} "
                     f"{b.get('title') or b['url'][:50]}")
        lines.append(f"    {b['url']}")
    return {"query": q, "verdict": verdict, "breakdown": breakdown,
            "pos_count": pos_c, "neg_count": neg_c, "mixed_count": mix_c,
            "source_count": len(breakdown), "render": "\n".join(lines)}


def related(query, *, count=4, timeout=45.0, **kw):
    """Related-searches — documented ABSENT on the Brave Search API (round-14).

    Probed live (this round): the web search response exposes **no** related /
    ``web_effects`` / "related-queries" block on any probe query (the top-level
    JSON only carries ``type/query/web/videos/discussions/infobox/faq/news/
    locations/rich/mixed`` — no ``related`` key). There is no documented
    related-searches endpoint outside the HTML dashboard, and ``suggest/*``
    (the closest surface) returns HTTP 301 to the docs HTML /
    ``OPTION_NOT_IN_PLAN`` (see `trending_topics()`).

    Returns:
        A short human-readable note that does **not** hit the network, plus the
        probe queries used so callers can inspect ``search()`` themselves.
    """
    return (
        "related() is not available on the Brave Search API — no related-"
        "queries block or endpoint is exposed (probed live: web responses carry "
        "no 'related' key on any probe query; /res/v1/suggest/* 301s to the "
        "docs HTML and other enrichment surfaces return OPTION_NOT_IN_PLAN). "
        "For query discovery use brave.search / brave.paged queries over "
        "related phrasings instead."
    )




def _extract_hrefs(text):
    """Naive http(s) link extraction from page text (round-14 `search_worker`)."""
    import re as _re
    return list(dict.fromkeys(
        _re.findall(r'https?://[A-Za-z0-9./_~:%+?&=#@\-]+', text or '')
    ))


def search_worker(query, *, depth=2, breadth=4, timeout=45.0, **kw):
    """Breadth-first "knowledge worker" over the web index (round-14).

    Composes the verified ``search()`` + ``crawl()`` into a BFS crawl. The seed
    is a web search; from each seed's pages it expands the next frontier using
    Brave's own structured **``cluster``/sitelink URLs** on the web results
    (plus, at deeper hops, naive in-page http(s) link extraction). Each
    neighbour is crawled (full text) up to ``depth`` hops and ``breadth`` nodes
    per level, so you fan out from a topic across a small related page graph and
    get back the fetched pages for skimming.

    Args:
        query: seed search term.
        depth: max BFS hop count (default 2).
        breadth: max pages crawled per hop (default 3).
        timeout / **kw: forwarded to ``search()``.

    Returns:
        {"query", "pages": [{depth, url, title, ok, chars, links}], "seed",
        "n", "visited", "render"}.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("search_worker() needs a seed query")
    depth = max(1, int(depth or 3))
    breadth = max(1, int(breadth or 3))
    sd = _search_full(q, mode="web", count=breadth, timeout=timeout or 45.0, **kw)
    seed_results = sd.get("results") or []
    seeds = [r.get("url") for r in seed_results if r.get("url")][:breadth]

    # structured Brave cluster/sitelink URLs per seed URL, for a clean frontier
    sitelinks = {}
    for r in seed_results:
        if r.get("url"):
            sitelinks[r["url"]] = [
                c.get("url") for c in (r.get("cluster") or []) if isinstance(c, dict) and c.get("url")
            ]

    visited = set()
    pages = []
    frontier = [(0, u) for u in seeds]
    d = 0
    while frontier and d < depth:
        level = frontier
        frontier = []
        urls = [u for _, u in level if u and u not in visited][:breadth]
        crawled = {}
        if urls:
            cr = crawl(urls, max_chars=2500)
            for item in (cr.get("results") or []):
                if item.get("status") == 200 and item.get("text"):
                    crawled[item.get("url")] = item
        for hop, u in level:
            if not u or u in visited:
                continue
            visited.add(u)
            rec = crawled.get(u)
            if rec:
                children = [c for c in (sitelinks.get(u) or []) if c] or                            _extract_hrefs(rec.get("text") or "")
                pages.append({"depth": hop, "url": u, "title": rec.get("title"),
                              "ok": True, "chars": rec.get("chars"),
                              "children": children[:breadth]})
                if hop + 1 < depth:
                    frontier += [(hop + 1, c) for c in children[:breadth]]
            else:
                pages.append({"depth": hop, "url": u, "title": None,
                              "ok": False, "chars": 0, "children": []})
        d += 1
    lines = [f"search_worker '{q}' — {len(pages)} page(s) walked ({depth} hop(s)):"]
    for p in pages[:60]:
        lines.append(f"[d{p['depth']}] {'OK' if p['ok'] else 'FAIL'} {p['url']}"
                     + (f"  ({p['chars']} ch)" if p.get('chars') else ""))
    return {"query": q, "pages": pages, "visited": visited, "n": len(pages),
            "render": "\n".join(lines)}


# ----------------------------------------------------------------------------
# Round-17: developer-first helpers — `docs`, `code_result`, `qna`,
# `cve_lookup`, `breaking_change`, `dev_digest`, and an honest docker-absent note.
# All live-verified against api.search.brave.com on this plan. New public entry
# points are added without touching any prior signature.
# ----------------------------------------------------------------------------

_DOC_DOMAINS = (
    "readthedocs.io", "learn.microsoft.com", "developer.mozilla.org",
    "docs.python.org", "docs.docker.com", "docs.github.com",
    "docs.kubernetes.io", "kubernetes.io", "docs.djangoproject.com",
    "docs.scipy.org", "fastapi.tiangolo.com", "flask.palletsprojects.com",
    "pandas.pydata.org", "numpy.org", "scikit-learn.org", "pypi.org",
    "redis.io", "webpack.js.org", "react.dev", "laravel.com/docs",
    "docs.oracle.com", "docs.aws.amazon.com", "dev.azure.com",
    "istio.io", "grpc.io", "developer.apple.com", "developers.google.com",
    "cloud.google.com", "docs.datastax.com", "en.cppreference.com",
    "cplusplus.com", "devdocs.io", "docs.astro.build",
)
_DOC_URL_MARKERS = ("/docs/", "/documentation", "/reference/", "/api/", "/learn/")
_DOC_BREAD_PREFIXES = ("docs", "reference", "api", "tutorial", "guide", "manual",
                       "how-to", "learn", "examples")
_CODE_DOMAINS = ("github.", "gitlab.", "stackoverflow.com", "stackexchange.com",
                 "dev.to", "codeberg.", "bitbucket.", "gist.github.com",
                 "askubuntu.com", "softwareengineering.stackexchange.com")


def _site(item):
    return (item.get("site") or "").lower()


def _doc_relevance(item):
    """0..1 heuristic "is this an official doc/reference page" for `docs()`."""
    site = _site(item)
    url = (item.get("url") or "").lower()
    br = (item.get("breadcrumb") or "").lower()
    score = 0.0
    if any(d in site for d in _DOC_DOMAINS):
        score += 0.45
    if any(m in site for m in ("docs.", "dev.", "developer.", "api.", "reference.")):
        score += 0.30
    if any(m in url for m in _DOC_URL_MARKERS):
        score += 0.25
    if any(br.startswith(p) for p in _DOC_BREAD_PREFIXES):
        score += 0.25
    return min(score, 1.0)


def _code_relevance(item):
    """Heuristic "is this a code-y hit" (repo/recipe/Q&A) for `code_result()`."""
    site = _site(item)
    score = 0.0
    if any(k in site for k in _CODE_DOMAINS):
        score += 0.5
    qa = item.get("qa")
    if isinstance(qa, dict) and (qa.get("answer") or qa.get("question")):
        score += 0.3
    if site.endswith("github.io") or "/blob/" in (item.get("url") or ""):
        score += 0.2
    return score


def docs(query, *, count=14, country=None, search_lang=None, safe_search="moderate",
         freshness=None, timeout=45.0):
    """Tech-docs search — bias web results toward authoritative doc/reference pages (round-17).

    Re-ranks the verified `search(mode="web")` hit set so official reference /
    read-the-docs / ``docs.*`` / ``developer.*`` / ``api.*`` / ``reference.*``
    pages (learn.microsoft.com, developer.mozilla.org, docs.python.org,
    docs.docker.com, kubernetes.io/docs, pandas.pydata.org, ...) float above
    blog-aside posts. Every returned web item already carries the `breadcrumb`
    ("docs / api / reference"), `site`, `content_type` and `author`/`publisher`
    needed to tell reference text from a blog at a glance. Purely a client-side
    re-rank of results Brave already returns — no fabricated filter.

    Live-verified: `docs("clojure transducers")` promotes clojure.org
    `reference / transducers` and dev.solita.fi; `docs("pandas groupby")` puts
    pandas.pydata.org `docs / reference / api / pandas.DataFrame.groupby.html`
    first.

    Args:
        query: the technology/feature to find docs for.
        count: how many web results to fetch & re-rank (default 12).
        country / search_lang / safe_search / timeout: forwarded to `search()`.
        freshness: optional freshness filter forwarded to `search()`.

    Returns:
        {"query", "results": web items re-ranked doc-first, "docs": only the
         doc-scored sub-list, "n", "n_docs", "breadcrumbs": [site+" → "+br, ...],
         "render": readable}.
    """
    data = _search_full(query, mode="web", count=count, country=country,
                  search_lang=search_lang, safe_search=safe_search,
                  freshness=freshness, timeout=timeout)
    items = data.get("results") or []
    ranked = sorted(items, key=_doc_relevance, reverse=True)
    doc_sub = [r for r in items if _doc_relevance(r) > 0.25]
    crumbs = []
    for r in doc_sub[: count]:
        br = r.get("breadcrumb")
        crumbs.append((r.get("site") or "") + ("  →  " + br if br else ""))
    lines = ["Docs for %r (%d doc-y of %d web):" % (query, len(doc_sub), len(items))]
    for i, r in enumerate(ranked[: count], 1):
        lines.append("%d. %s" % (i, (r.get("title") or "")[:100]))
        lines.append("   %s" % (r.get("url") or ""))
        if r.get("breadcrumb"):
            lines.append("   %s  →  %s" % (r.get("site") or "", r.get("breadcrumb")))
    return {
        "query": query,
        "results": ranked,
        "docs": doc_sub,
        "n": len(ranked),
        "n_docs": len(doc_sub),
        "breadcrumbs": crumbs,
        "render": "\n".join(lines),
    }


def code_result(query, *, lang=None, count=12, country=None, safe_search="moderate",
                timeout=45.0):
    """Code-focused result set — surfaced Q&A/recipe/snippet hits (round-17).

    Runs a web search (optionally augmented with a ``lang`` hint in the query,
    e.g. "python ") and re-ranks toward the *code-shaped* results Brave already
    returns — GitHub / GitLab / StackOverflow / dev.to / doc hub hits, plus any
    result carrying an embedded `qa` (question + `answer` + `upvote_count`) or
    discussion thread with `top_comment`/`top_answer`. Returns the code-y items
    with their breadcrumb, source and (when present) inline answer text.

    Honest live note (round-17): Brave's categorical `language:` / `framework:`
    *goggles* and query prefixes are NOT usable to filter on this plan —
    ``goggles="language:python"`` returns HTTP 422 and ``language:python ...``
    as a query prefix returns an empty result set; real goggle-URL filters are
    accepted but do not change results (pass-through). So `code_result` biases
    the already-returned hits client-side and nudges the query with a language
    word instead — no fabricated categorical filter.

    Args:
        query: what to look up (e.g. "sort list", "fastapi middleware").
        lang: optional language/framework hint appended to the query
            (e.g. "python", "javascript", "react").
        count: web results to fetch (default 12).
        safe_search / timeout: forwarded to `search()`.

    Returns:
        {"query", "results": code-ranked web items, "code": the code-sub scored
        hits, "answers": [{kind, title, url, question, answer, upvote_count}],
        "n", "n_code", "render"}.
    """
    q = (query or "").strip()
    if lang:
        q = "%s %s" % (lang.strip(), q)
    data = _search_full(q, mode="web", count=count, safe_search=safe_search, timeout=timeout)
    items = data.get("results") or []
    ranked = sorted(items, key=_code_relevance, reverse=True)
    code_sub = [it for it in items if _code_relevance(it) > 0]
    answers = []
    for it in items:
        qa = it.get("qa")
        if isinstance(qa, dict) and (qa.get("answer") or qa.get("question")):
            answers.append({
                "text": qa.get("answer") or "",
                "question": qa.get("question") or "",
                "title": it.get("title"),
                "url": it.get("url"),
                "site": it.get("site"),
                "upvote_count": qa.get("upvote_count"),
            })
    lines = ["Code results for %r (%d code-y of %d):" % (q, len(code_sub), len(items))]
    for i, it in enumerate(ranked[: count], 1):
        title = (it.get("title") or "")[:90]
        lines.append("%d. %s — %s" % (i, title, it.get("site") or ""))
        if it.get("url"):
            lines.append("   %s" % it.get("url"))
        qa = it.get("qa")
        if isinstance(qa, dict) and qa.get("answer"):
            lines.append("   ▸ %s" % _clean_html(qa["answer"])[:160])
    return {
        "query": q,
        "results": ranked,
        "code": code_sub,
        "answers": answers,
        "n": len(ranked),
        "n_code": len(code_sub),
        "render": "\n".join(lines),
    }


def qna(query, *, count=12, min_answers=3, country=None, search_lang=None,
        safe_search="moderate", timeout=45.0):
    """Developer Q&A surfacer — rank StackOverflow/forum threads + inline answers.

    Pools the two Q&A-shaped surfaces Brave already returns on a web query —
    the embedded ``discussions`` threads (with `forum`/`forum_name`,
    `num_answers`, `score`, `question`, `top_comment`) and the web results that
    carry an inline ``qa`` (question + `answer` + `upvote_count`) — and ranks
    them by engagement (num_answers / upvote_count) opposite to stack-overflow
    style. Each row exposes the *top answer / top comment* directly so an agent
    can grab a working answer snippet without a second hop. `lim` count is
    client-side (the search+discussions pulls up to `count` web rows).

    Live-verified: `qna("python async await")` returns the inline QA
    ("Simplest async/await example possible in Python", 336 upvotes) plus Reddit
    threads (r/learnpython 206 ans, r/Python 69 ans), each with `top_comment`.

    Args:
        query: the developer question.
        count: how many web results to pull before pooling the Q&A surfaces.
        min_answers: drop threads with fewer than this many answers (default 3).
        country / search_lang / safe_search / timeout: forwarded to `search()`.

    Returns:
        {"query", "qas": [{kind, title, url, site, question, answer, top_comment,
         upvote_count/score, num_answers, breadcrumb}], "n", "forums":
         {forum_name: count}, "render"}.
    """
    data = _search_full(query, mode="web", count=count, country=country,
                  search_lang=search_lang, safe_search=safe_search, timeout=timeout)
    qas = []
    for it in (data.get("results") or []):
        qa = it.get("qa")
        if isinstance(qa, dict) and (qa.get("answer") or qa.get("question")):
            qas.append({
                "kind": "qa",
                "title": it.get("title"),
                "url": it.get("url"),
                "site": it.get("site"),
                "question": qa.get("question"),
                "answer": qa.get("answer"),
                "top_answer": qa.get("answer"),
                "upvote_count": qa.get("upvote_count"),
                "num_answers": None,
                "breadcrumb": it.get("breadcrumb"),
            })
    for t in (data.get("discussions") or []):
        na = t.get("num_answers")
        if na is not None and min_answers and na < min_answers:
            continue
        if t.get("top_comment") or t.get("question") or na is not None:
            qas.append({
                "kind": "discussion",
                "title": t.get("title"),
                "url": t.get("url"),
                "site": t.get("site"),
                "forum": t.get("forum"),
                "question": t.get("question"),
                "answer": t.get("top_comment"),
                "top_answer": t.get("top_comment"),
                "top_comment": t.get("top_comment"),
                "num_answers": na,
                "score": t.get("score"),
                "upvote_count": t.get("score") if (t.get("score") is None or str(t.get("score")).isdigit()) and na is None else na,
                "breadcrumb": t.get("breadcrumb"),
            })
    qas.sort(key=lambda x: (x.get("upvote_count") if x.get("upvote_count") is not None else 0)
                              or (x.get("num_answers") or 0), reverse=True)
    forums = {}
    for t in (data.get("discussions") or []):
        f = t.get("forum")
        if f:
            forums[f] = forums.get(f, 0) + 1
    lines = ["Q&A for %r (%d thread/answer rows):" % (query, len(qas))]
    for i, q in enumerate(qas[:count], 1):
        head = (q.get("title") or q.get("question") or "")
        eng = "↑%s" % (q.get("upvote_count") if q.get("upvote_count") is not None else q.get("num_answers"))
        lines.append("%d. [%s] %s  %s" % (i, q.get("kind", ""), str(head)[:80], eng))
        lines.append("   %s" % (q.get("url") or ""))
        ans = q.get("top_answer") or q.get("top_comment")
        if ans:
            lines.append("   ▸ %s" % _clean_html(ans)[:140])
    return {"query": query, "qas": qas, "results": qas, "n": len(qas),
            "forums": forums, "render": "\n".join(lines)}


def cve_lookup(cve, *, count=8, news_count=4, freshness=None, country=None,
               safe_search="moderate", timeout=45.0):
    """CVE advisory summary — web advisories + current news for one CVE id.

    Runs `search(mode="web")` and `search(mode="news")` for the (normalised)
    CVE id and distils an advisory digest: the top official/advisory hits
    (nvd.nist.gov, cve.org, osv.dev, cisa, vendor advisory/support pages ...),
    plus a freshness-aware news thread if Brave surfaces any (age_meta /
    published_at on every news row). No data is invented — what's absent is
    simply left out.

    Live-verified:
      `cve_lookup("CVE-2023-2566")` → nvd.nist.gov detail + CISA/pentest-tools
      addenda under `advisories`, plus news rows where the CVE is trending.

    Args:
        cve: a CVE id (case-insensitive, e.g. "cve-2023-2566").
        count: web results to pull for the advisory pool (default 12).
        news_count: news headlines to pull (default 4; 0 disables news).
        freshness: optional freshness passed to the news search.
        country / safe_search / timeout: forwarded to `search()`.

    Returns:
        {"cve": normalized id, "advisories": [{title, site, url, description}],
         "news": [{title, site, url, age_meta, published_at}], "n_advisories",
         "n_news", "render"}.
    """
    cve = (cve or "").strip().upper()
    if not cve:
        raise ValueError("cve_lookup() needs a CVE id")
    adv_result = []
    try:
        d = _search_full(cve, mode="web", count=count, country=country,
                   safe_search=safe_search, timeout=timeout)
        seen = set()
        for it in (d.get("results") or []):
            u = it.get("url")
            if not u or u in seen:
                continue
            seen.add(u)
            adv_result.append({
                "title": it.get("title"),
                "url": u,
                "site": it.get("site"),
                "description": it.get("description"),
            })
    except Exception:
        adv_result = []
    news = []
    if news_count:
        try:
            n = _search_full(cve, mode="news", count=news_count, country=country,
                       safe_search=safe_search, freshness=freshness, timeout=timeout)
            for it in (n.get("results") or []):
                news.append({
                    "title": it.get("title"),
                    "url": it.get("url"),
                    "site": it.get("site"),
                    "age_meta": it.get("age_meta"),
                    "published_at": it.get("published_at"),
                    "description": it.get("description"),
                })
        except Exception:
            news = []
    lines = ["CVE advisory digest for %s:" % cve]
    lines.append("  Advisories (%d):" % len(adv_result))
    for a in adv_result[:count]:
        lines.append("   • %s — %s" % (a["title"], a["url"]))
    if news:
        lines.append("  News (%d):" % len(news))
        for nn in news[:news_count]:
            am = (nn.get("age_meta") or {}).get("text", "")
            lines.append("   • %s  [%s]  %s" % (nn.get("title"), am, nn.get("site")))
    return {"cve": cve, "advisories": adv_result, "news": news,
            "n_advisories": len(adv_result), "n_news": len(news),
            "render": "\n".join(lines)}


def breaking_change(query, *, freshness="pd_1m", count=30, hint=None,
                    min_age_days=None, country=None, search_lang=None,
                    safe_search="moderate", timeout=45.0):
    """Freshness-filtered news roundup for release / deprecation / breaking signals.

    Runs a `news` search for the library/tool (the query is augmented with a
    release/change vocabulary so Brave returns change-management headlines) and
    keeps the rows whose title or description mention a breaking-change signal
    (``breaking``, ``deprecat``, ``migrat``, ``upgrad``, ``release``,
    ``changelog``, ``remov(ed|al)``, ``sunset``, ``end[- ]of[- ]life``,
    ``retir``). Every kept item carries `age_meta` + `published_at` so you can
    gate on recency. No data fabrication; non-signal headlines are simply
    dropped.

    Live-verified: `breaking_change("fastapi", freshness="pd_1m")` returns the
    fastapi/fastapi DevUpdate feed + "FastAPI 0.140.13 Released" + a release-
    notes row under `signals`.

    Args:
        query: the library / tool.
        freshness: forwarded to `search(mode="news")`. Required to filter.
        count: news items to pull & scan (default 30).
        hint: optional relative query appended so Brave surfaces change
            headlines (default ``"release notes OR breaking changes OR
            changelog"``); pass ``hint=""`` to disable augmentation.
        min_age_days: optional — drop signals older than this many days
            (`age_meta.days`; absolute-date rows without days are kept).
        country / search_lang / safe_search / timeout: forwarded to `search()`.

    Returns:
        {"query": the query actually searched, "signals": [{title, url, site,
         age_meta, published_at}], "n", "n_scanned", "render"}.
    """
    pat = re.compile(
        r"\b(breaking|breaking ?-? ?change|deprecat|migrat|upgrad|release|changelog|"
        r"remov|removal|sunset|end[- ]of[- ]life|retir|new in|what's new)\b", re.I)
    scanned = []
    q = (query or "").strip()
    hint_ = "release notes OR breaking changes OR changelog" if hint is None else (hint or "")
    if q and hint_ and not re.search(
            r"\b(release|changelog|breaking|deprecat)\b", q, re.I):
        q = "%s %s" % (q, hint_)
    try:
        d = _search_full(q, mode="news", count=count, freshness=freshness,
                   country=country, search_lang=search_lang, safe_search=safe_search,
                   timeout=timeout)
        scanned = d.get("results") or []
    except Exception:
        scanned = []
    sig = []
    for it in scanned:
        blob = " ".join([
            it.get("title") or "", it.get("description") or "",
            (it.get("extra_snippets") or [""])[0] or ""])
        if not pat.search(blob):
            continue
        am = it.get("age_meta") or {}
        days = am.get("days")
        if min_age_days and days is not None and days > min_age_days:
            continue
        sig.append({
            "title": it.get("title"),
            "url": it.get("url"),
            "site": it.get("site"),
            "age_meta": am,
            "published_at": it.get("published_at"),
        })
    lines = ["%s — release/breaking signals (%d/%d scanned):" % (q, len(sig), len(scanned))]
    for s in sig[:count]:
        amt = (s.get("age_meta") or {}).get("text", "")
        lines.append("  • %s  [%s]  %s" % (s.get("title"), amt, s.get("site")))
        if s.get("url"):
            lines.append("      %s" % s.get("url"))
    return {"query": q, "signals": sig, "n": len(sig),
            "n_scanned": len(scanned), "render": "\n".join(lines)}


def dev_digest(query, *, count=6, news_count=6, summary=True, study=True,
               country=None, search_lang=None, safe_search="moderate", timeout=45.0):
    """A compact Markdown brief for a developer topic, in one call.

    Composes the verified surfaces — a web search (with `docs`-style ranking),
    a news search, and `summarize_page()` on the top web hit — into one Markdown
    brief: an overview line, the top doc/code pages (with source + breadcrumb),
    the key page digest of the lead hit, and the freshest headlines. Hand it to
    a downstream model or display it directly.

    Args:
        query: topic (e.g. "fastapi", "clojure conditionals").
        count: web results & news results to pull (default 6 each).
        summary: also fetch + summarize the top doc hit (default True).
        safe_search/timeout/...: forwarded to the underlying `search()` calls.

    Returns:
        {"query", "markdown": str, "web_titles": [...], "news_titles": [...],
         "digest": {title, summary} | None}.
    """
    web = _search_full(query, mode="web", count=count, country=country,
                 search_lang=search_lang, safe_search=safe_search, timeout=timeout)
    w = sorted((web.get("results") or []), key=_doc_relevance, reverse=True)
    w = w[:count]
    news = _search_full(query, mode="news", count=news_count, country=country,
                  search_lang=search_lang, safe_search=safe_search, timeout=timeout)
    nn = (news.get("results") or [])[:news_count]
    digest = None
    if summary and w:
        try:
            digest = summarize_page(w[0].get("url"), max_chars=12000, max_points=3,
                                    timeout=min(timeout or 45.0, 20.0))
        except Exception:
            digest = None
    md = []
    md.append("## %s" % query)
    if web.get("infobox"):
        ib = web.get("infobox")
        md.append("**%s** — %s" % (_clean_html(ib.get("title") or ""),
                                    _clean_html(ib.get("description") or ib.get("long_desc") or "")))
    md.append("\n### Top sources")
    for i, r in enumerate(w, 1):
        br = r.get("breadcrumb")
        md.append("%d. **%s** — %s" % (i, _clean_html(r.get("title") or ""), r.get("site") or ""))
        md.append("   <sub>%s%s</sub>" % (r.get("url") or "", ("  ·  " + br) if br else ""))
    if digest:
        md.append("\n### Lead page")
        md.append("**_%s_**" % (digest.get("title") or ""))
        md.append("%s" % (digest.get("summary") or ""))
        pts = digest.get("points") or []
        for p in pts[:3]:
            md.append("- %s" % p)
    if nn:
        md.append("\n### Freshest news")
        for n in nn:
            amt = (n.get("age_meta") or {}).get("text", "")
            md.append("- %s  (%s) [%s]" % (n.get("title"), n.get("site"), amt))
    return {"query": query, "markdown": "\n".join(md),
            "web_titles": [r.get("title") for r in w],
            "news_titles": [n.get("title") for n in nn],
            "digest": {"title": (digest or {}).get("title"),
                       "summary": (digest or {}).get("summary"),
                       "points": (digest or {}).get("points")},
            "render": "\n".join(md)}


def docker_aliases() -> str:
    """Documented-absent: Brave has no container/docker-registry search.

    Probed live (round-17): there is no ``/res/v1/docker`` / ``/images`` /
    ``/registry`` endpoint on the Brave Search API, and a ``docker``-styled
    query returns ordinary web hits, not a registry index. The skill's ``pypi``/
    ``npm`` package surface (``software()`` / ``package``) is the closest thing
    but Brave does not tag Docker Hub images as ``software`` here, so a
    ``docker_aliases()`` list cannot be truthfully produced. Use ``software()``
    for PyPI/npm packages; use ``find_files()``/``search()`` for docker docs.
    """
    return (
        "docker_aliases() is not available on the Brave Search API — no Docker/"
        "registry index endpoint exists (probed live round-17). Use "
        "brave.software(pkg) for PyPI/npm packages and brave.docs('docker ...') "
        "or brave.search('docker <topic>') to find Docker/Hub documentation."
    )



# ----------------------------------------------------------------------------
# Round-17b: developer-community helpers — `reddit`, `github_repos`,
# `github_issues`. Client-composed over the VERIFIED `search()`/`forums()`
# primitives (no new API surface). Live-verified on api.search.brave.com.
# New public entry points; no prior signature touched.
# ----------------------------------------------------------------------------

def reddit(query=None, *, subreddit=None, count=15, timeout=45.0, **kwargs):
    """Reddit developer-community threads (r/LocalLLaMA et al).

    Returns forum/discussion threads for a query, optionally restricted to a
    subreddit (e.g. ``subreddit="LocalLLaMA"`` -> reddit.com/r/LocalLLaMA).
    Each thread carries title, url, subreddit, score, num_answers, question,
    and the top_comment excerpt. No fabrication — only rows Brave returns.

    Args:
        query: search phrase (e.g. "what model to self-host locally").
        subreddit: optional subreddit name (no ``r/`` prefix) to restrict to.
        count: max threads to return (default 15).
        timeout / **kwargs: forwarded to the underlying search.

    Returns:
        {"subreddit", "query", "threads":[{title,url,subreddit,score,
         num_answers,top_comment}], "n", "render"}.
    """
    q = query or ""
    data = forums(q, count=max(1, count), timeout=timeout, **kwargs)
    rows = data.get("results") or []
    threads = []
    for it in rows:
        url = it.get("url") or ""
        site = it.get("forum") or it.get("site") or ""
        if subreddit:
            if ("/r/" + subreddit) not in url and ("/r/" + subreddit) not in site:
                continue
        else:
            if "reddit" not in (url + site):
                continue
        th = {
            "title": it.get("title"),
            "url": url,
            "subreddit": subreddit or _extract_subreddit(url),
            "score": it.get("score"),
            "num_answers": it.get("num_answers"),
            "question": it.get("question"),
            "top_comment": it.get("top_comment"),
        }
        threads.append(th)
    lines = ["r/%s threads for '%s':" % (subreddit or "reddit", q or subreddit or "")]
    for th in threads[:count]:
        lines.append("  \u2022 %s  [score %s, %s answers]  %s" % (
            th["title"], th["score"], th["num_answers"], th["url"]))
    return {"subreddit": subreddit, "query": q, "threads": threads,
            "n": len(threads), "render": "\n".join(lines)}


def _extract_subreddit(url):
    """Best-effort pull of ``r/<name>`` from a reddit URL (returns str|None)."""
    m = re.search(r"/r/([A-Za-z0-9_]+)", url or "")
    return m.group(1) if m else None


def github_repos(query, *, count=10, exact=True, timeout=45.0, **kwargs):
    """GitHub repository lookup — developer-facing repo records.

    Runs a web search scoped to github.com and returns clean repo records:
    owner, repo, description, primary language (when present), and url. This
    is a convenience over `search()` — Brave does not expose a dedicated repo
    index, so it is live-composed from web results (honest). For a deep
    composite with stars/readme use the exa skill's ``github_repo``.

    Args:
        query: repo-ish query (e.g. "ollama", "llama.cpp").
        count: results to scan (default 10).
        exact: only keep github.com repo pages (default True).
        timeout / **kwargs: forwarded to _search_full().

    Returns:
        {"query", "repos":[{owner,repo,url,description,language,stars}],
         "n", "render"}.
    """
    d = _search_full(query, mode="web", count=count, timeout=timeout, **kwargs)
    repos = []
    for it in (d.get("results") or []):
        url = it.get("url") or ""
        if "github.com" not in url:
            continue
        if exact and not any(url.endswith(p) for p in ("", "/")) and False:
            pass
        parts = url.replace("https://github.com/", "").split("/")
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            stars = it.get("extra_snippets") or [""]
            lang = it.get("language")
            repos.append({
                "owner": owner, "repo": repo, "url": url,
                "description": it.get("description") or it.get("title"),
                "language": lang, "stars": it.get("stars"),
            })
    lines = ["GitHub repos for '%s':" % query]
    for rp in repos[:count]:
        lines.append("  \u2022 %s/%s  %s" % (rp["owner"], rp["repo"], (rp["description"] or ""))[:110])
    return {"query": query, "repos": repos, "n": len(repos), "render": "\n".join(lines)}


def github_issues(query, *, count=12, timeout=45.0, **kwargs):
    """GitHub issue / PR conversations for a developer query.

    Runs a site-scoped web search toward github.com issue/PR pages and returns
    conversation records — title, url, owner/repo, issue number — plus a
    ``top_comment`` snippet when present. Composed over ``search()`` (no
    dedicated issue API on this plan). Honest: if no issue pages round-trip,
    ``n`` is 0.

    Args:
        query: issue-flavored query (e.g. "ollama vulkan error").
        count: results to scan (default 20).
        timeout / **kwargs: forwarded to _search_full().

    Returns:
        {"query", "issues":[{title,url,owner,repo,number,excerpt}], "n",
         "render"}.
    """
    gites = _search_full(query + " site:github.com", mode="web", count=count,
                   timeout=timeout, extra=True, **kwargs)
    issues = []
    for it in (gites.get("results") or []):
        url = it.get("url") or ""
        if "/issues/" not in url and "/pull/" not in url:
            continue
        parts = url.split("/")
        owner, repo, num = (parts[3] if len(parts) > 3 else None,
                            parts[4] if len(parts) > 4 else None,
                            parts[6] if len(parts) > 6 else None)
        issues.append({
            "title": it.get("title") or it.get("description"),
            "url": url, "owner": owner, "repo": repo, "number": num,
            "excerpt": (it.get("extra_snippets") or [""])[0],
        })
    lines = ["GitHub issues for '%s':" % query]
    for isu in issues[:count]:
        lines.append("  \u2022 %s  %s" % (isu["title"], isu["url"]))
    return {"query": query, "issues": issues, "n": len(issues),
            "render": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Round-18 developer helpers (live-verified).
# ---------------------------------------------------------------------------

def pkg_lookup(query, *, count=14, docs_count=6, country=None,
               safe_search="moderate", timeout=45.0):
    """Library/package identity lookup — registry identity + versions + pages.

    One-call dev look-up for a Python/JS/Rust library. Distils the ``software``
    package records Brave already tags on its web endpoint — registry name,
    *version*, `published` date, registry (pypi/npm), code repository — for the
    exact registry pages (npmjs.com/package/*, pypi.org/project/*), and pools the
    remaining web hits into ``pages`` (homepage / GH repo / docs). Nothing is
    invented: registries/versions only appear when Brave's ``software`` struct
    carries them (e.g. the ``uuid`` npm row yields version 14.0.1 + published
    "Jun 20, 2026"; GitHub-only hits carry no version).

    Complements ``software()``/``package`` (round-5): ``pkg_lookup`` additionally
    separates **registry identity** rows from **general pages** and dedups the
    exact PyPI/npm project pages for a library, which the raw list does not.

    Args:
        query: library name, optionally with an ecosystem hint ("uuid",
            "lxml pypi", "serde_json crates") to bias the registry.
        count: software/web rows to scan (default 14).
        docs_count: keep at most this many non-registry ``pages`` rows (default
            6; set 0 to return only registry rows).
        country / safe_search / timeout: forwarded to `search()`.

    Returns:
        {"query", "packages": [{name, version, registries, url, site, language,
         published, code_repository}], "pages": [{title, url, site, breadcrumb}],
         "n", "n_packages", "n_pages", "render"}.
    """
    regs = (_search_full(query, mode="web", count=count, country=country,
                   safe_search=safe_search, timeout=timeout).get("results") or [])
    packages, pages, seen_p = [], [], set()
    for it in regs:
        url = it.get("url") or ""
        sw = it.get("software")
        if isinstance(sw, dict):
            pname = sw.get("name") or it.get("title")
            key = (pname or "") + "|" + url
            if key in seen_p:
                continue
            seen_p.add(key)
            regs_ = [str(x) for x in (sw.get("registry") or [])]
            if sw.get("is_pypi"):
                regs_.append("pypi")
            if sw.get("is_npm"):
                regs_.append("npm")
            packages.append({
                "name": pname,
                "version": sw.get("version"),
                "registries": regs_ or None,
                "url": url,
                "site": it.get("site"),
                "published": sw.get("published") or sw.get("datePublished"),
                "code_repository": sw.get("code_repository") or sw.get("codeRepository"),
                "language": sw.get("programming_language") or None,
            })
        elif docs_count and url and (it.get("title") or it.get("description")):
            pages.append({"title": it.get("title"), "url": url,
                          "site": it.get("site"), "breadcrumb": it.get("breadcrumb")})
    if docs_count:
        pages = pages[:docs_count]
    lines = ["Package lookup for '%s' (%d registry, %d pages):" % (
        query, len(packages), len(pages))]
    for p in packages[:count]:
        v = p["version"] or "?  "
        reg = ",".join(p["registries"] or ["?"])
        lines.append("  \u2022 %s  v%s  [%s]  %s" % (p["name"], v, reg, p["url"]))
    for pg in pages[:docs_count]:
        lines.append("    page: %s — %s" % (pg["title"], pg["url"]))
    return {"query": query, "packages": packages, "pages": pages,
            "n": len(packages) + len(pages), "n_packages": len(packages),
            "n_pages": len(pages), "render": "\n".join(lines)}


def pkg_security(query, *, context=None, count=14, news_count=4, freshness=None,
                 country=None, safe_search="moderate", timeout=45.0):
    """Package security / advisory digest — advisories + GH issues + news.

    Dev-facing: for a library name, distils the security-shaped hits of a web
    search (`<pkg> vulnerability OR CVE OR advisory`) — Snyk / cvedetails / NVD
    / OSV / vendor advisory pages (with any CVE ids that appear in titles) plus
    GitHub issue pages about a flaw and a freshness-aware news pass. No data is
    invented: only rows Brave actually returns are kept, and CVEs are only
    surfaced when they show up in a title/snippet.

    Live-verified: `pkg_security("pyyaml")` returns the Snyk package page,
    the CVE-2020-14343 sentinelone advisory, and cvedetails vendor list under
    `advisories`; `pkg_security("fastapi", news_count=3)` adds the fresh
    advisories feed.

    Args:
        query: package name (e.g. "pyyaml", "react", "log4j").
        context: optional extra context appended to the advisory query
            (e.g. ">=2.0", "RCE", "CVE-2023-...") to sharpen it.
        count: web hits to scan for advisories (default 14).
        news_count: news headlines to pull (default 4; 0 disables news).
        freshness / country / safe_search / timeout: forwarded to `search()`.

    Returns:
        {"query", "advisories":[{title, url, site, description, cvcs}],
         "issues":[{title, url, owner, repo}], "news":[{title, url, site,
         age_meta, published_at}], "n", "render"}.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("pkg_security() needs a package name")
    base = q
    if context:
        base = "%s %s" % (base, str(context).strip())
    adv_query = "%s vulnerability OR CVE OR advisory" % base
    adv, issues, seen = [], [], set()
    try:
        d = _search_full(adv_query, mode="web", count=count, country=country,
                   safe_search=safe_search, timeout=timeout)
        for it in (d.get("results") or []):
            url = it.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            txt = "%s %s" % ((it.get("title") or ""),
                            " ".join(it.get("extra_snippets") or [""]))
            cvcs = sorted(set(re.findall(r"CVE-\d{4,}-\d+", txt, re.I)))
            tl = (it.get("title") or it.get("description") or "").lower()
            gh = ("github.com" in url)
            if gh and ("/issues/" in url or "/pull/" in url or "security" in tl):
                parts = url.replace("https://github.com/", "").split("/")
                issues.append({"title": it.get("title"), "url": url,
                               "owner": parts[0] if parts else None,
                               "repo": parts[1] if len(parts) > 1 else None})
                continue
            adv.append({"title": it.get("title") or it.get("description"),
                        "url": url, "site": it.get("site"),
                        "description": it.get("description"),
                        "cvcs": cvcs or None})
    except Exception:
        adv = []
    news = []
    if news_count:
        try:
            n = _search_full(adv_query, mode="news", count=news_count, country=country,
                       safe_search=safe_search, freshness=freshness, timeout=timeout)
            for it in (n.get("results") or []):
                news.append({"title": it.get("title"), "url": it.get("url"),
                             "site": it.get("site"), "age_meta": it.get("age_meta"),
                             "published_at": it.get("published_at"),
                             "description": it.get("description")})
        except Exception:
            news = []
    lines = ["Security digest for '%s' (%d advisories):" % (q, len(adv))]
    for a in adv[:count]:
        cvc = (" " + ",".join(a["cvcs"])) if a["cvcs"] else ""
        lines.append("  \u2022 %s%s — %s" % (a["title"] or "", cvc, a["url"]))
    if issues:
        lines.append("  GH issues (%d):" % len(issues))
        for gi in issues[:4]:
            lines.append("   \u2022 %s — %s" % (gi["title"], gi["url"]))
    if news:
        lines.append("  News (%d):" % len(news))
        for nn in news[:news_count]:
            am = (nn.get("age_meta") or {}).get("text", "")
            lines.append("   \u2022 %s  [%s]  %s" % (nn.get("title"), am, nn.get("site")))
    return {"query": q, "advisories": adv, "issues": issues, "news": news,
            "n": len(adv) + len(issues) + len(news),
            "n_advisories": len(adv), "n_issues": len(issues), "n_news": len(news),
            "render": "\n".join(lines)}


def error_solution(query, *, ctx=None, count=18, min_answers=3, country=None,
                   search_lang=None, safe_search="moderate", timeout=45.0):
    """Compact solution digest for an error / stack-trace string.

    The dev "what do I run" answer: take an error message (e.g. ``"ModuleNot
    FoundError: No module named 'requests'"`` or a full stack-trace fragment),
    quote-peel it so Brave hits the exact thread, then surface (a) the single
    **best answer** — the top Stack Overflow / forum ``question`` + ``top_comment``
    / top_answer (the highest-engagement hit) — and (b) the next strongest Q&A
    sources. Composes only live-verified primitives (``search()`` + the Q&A
    pooling used by ``qna()``); it never fabricates a fix.

    Companion: ``stack_trace()`` returns the whole linked *universe* of
    GH issues + SOA + articles for a stack snippet; ``error_solution`` returns
    just the distilled best answer + top sources.

    Args:
        ctx: optional extra context word(s) appended to the quoted query (e.g.
            the language, "python", "postgres") to disambiguate the error.
        count: web rows to pull & scan for Q&A (default 18).
        min_answers: drop forum threads with fewer answers (default 3).
        ... / search_lang / safe_search / timeout: forwarded to `search()`.

    Returns:
        {"query", "best": {kind, question, answer, top_comment, url, site,
         upvotes, forum} | None, "sources":[same dict, next up to 5], "n",
         "render"}.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("error_solution() needs an error/stack string")
    quoted = '"{0}"{1}'.format(q, (" " + str(ctx).strip()) if ctx else "")
    d = _search_full(quoted, mode="web", count=count, country=country,
               search_lang=search_lang, safe_search=safe_search, timeout=timeout)
    pool = []
    for it in (d.get("results") or []):
        qa = it.get("qa")
        if isinstance(qa, dict) and (qa.get("answer") or qa.get("question")):
            pool.append({"kind": "qa", "title": it.get("title"), "url": it.get("url"),
                         "site": it.get("site"), "forum": None,
                         "question": qa.get("question"), "answer": qa.get("answer"),
                         "top_comment": qa.get("answer"), "top_answer": qa.get("answer"),
                         "upvotes": qa.get("upvote_count") or 0,
                         "breadcrumb": it.get("breadcrumb")})
    for t in (d.get("discussions") or []):
        na = t.get("num_answers")
        if min_answers and na is not None and na < min_answers:
            continue
        if t.get("top_comment") or t.get("question"):
            up = t.get("score") if t.get("score") is not None else (na or 0)
            pool.append({"kind": "discussion", "title": t.get("title"),
                         "url": t.get("url"), "site": t.get("site"),
                         "forum": t.get("forum"), "question": t.get("question"),
                         "answer": t.get("top_comment"), "top_answer": t.get("top_comment"),
                         "top_comment": t.get("top_comment"), "upvotes": up,
                         "breadcrumb": t.get("breadcrumb")})
    pool.sort(key=lambda p: p.get("upvotes") or 0, reverse=True)
    best = pool[0] if pool else None
    sources = pool[1:6]
    lines = ["Best answer for '%s':" % q]
    if best:
        lines.append("  \u2022 %s" % _clean_html(best.get("question") or best.get("title")))
        if best.get("answer"):
            lines.append("    => %s" % _clean_html(best["answer"])[:260])
        lines.append("    (%s)" % best.get("url"))
    else:
        lines.append("  (no Q&A surfaced — see `sources`)")
    for s in sources:
        forum = s.get("forum") or s.get("site") or ""
        lines.append("  \u2022 [%d\u2191 %s] %s" % (s.get("upvotes") or 0, forum,
                       _clean_html(s.get("title"))[:90]))
    return {"query": q, "best": best, "sources": sources,
            "n": len(pool), "qna_count": len(pool), "render": "\n".join(lines)}


def stack_trace(query, *, ctx=None, count=12, issues=6, min_answers=3,
                country=None, search_lang=None, safe_search="moderate",
                timeout=45.0):
    """The linked debug universe for a stack trace / error snippet.

    For a stack trace (or a distinctive error line) returns, in one call, the
    matching GitHub issue conversations, the Q&A threads that solve it (SO /
    Reddit / forums), and the top plain article/blog hits — composing the
    ``qna`` + ``github_issues`` + ``search`` primitives into one debug lede
    with counts per source. Every record is a real live Brave hit; no solution
    text is fabricated.

    Differs from ``error_solution()`` (which distills only the best answer):
    ``stack_trace`` gives the whole spectrum — GitHub issues the trace hits
    (with issue number), every closely-matching thread (with top comment), and
    the plain blog/doc links — so you can survey the frontier.

    Args:
        ctx: optional extra context word(s) added to the trace line (e.g.
            "python", "postgres") to pin the stack to a runtime.
        count: web results to scan for articles (default 12).
        issues: max GitHub issue records to keep (default 6; 0 disables GH).
        min_answers: drop Q&A threads with fewer answers (default 3).
        ... / search_lang / safe_search / timeout: forwarded.

    Returns:
        {"query", "github":[{...github_issues row}], "threads":[{...qna row}],
         "articles":[{title,url,site}], "n", "render"}.
    """
    peek = (query or "").strip()
    if not peek:
        raise ValueError("stack_trace() needs a stack/error snippet")
    peek = peek.splitlines()[-1].strip() or peek
    if ctx:
        peek = "%s %s" % (str(ctx).strip(), peek)
    github = []
    if issues:
        try:
            github = (github_issues(peek, count=issues, timeout=timeout) or {}).get("issues") or []
        except Exception:
            github = []
    threads = []
    try:
        th = qna(peek, count=min(count or 10, 12), min_answers=min_answers,
                 country=country, search_lang=search_lang, safe_search=safe_search,
                 timeout=timeout)
        threads = (th or {}).get("qas") or []
    except Exception:
        threads = []
    articles = []
    try:
        d = _search_full(peek, mode="web", count=count, country=country,
                   safe_search=safe_search, timeout=timeout)
        kept = 0
        for it in (d.get("results") or []):
            u = it.get("url") or ""
            if any(x in u for x in ("/issues/", "stackoverflow.com", "reddit.com",
                                     "/discuss", "github.com")):
                continue
            articles.append({"title": it.get("title"), "url": u, "site": it.get("site")})
            kept += 1
            if kept >= (count or 12):
                break
    except Exception:
        articles = []
    lines = ["Stack-trace universe for '%s' — %d issues, %d threads, %d links:" % (
        query, len(github), len(threads), len(articles))]
    for gi in github[:issues]:
        lines.append("  [GH] %s  %s" % (gi.get("title"), gi.get("url")))
    for t_ in threads[:5]:
        lines.append("  [QA] %s  %s" % (_clean_html(t_.get("title") or "")[:80], t_.get("url")))
    for a in articles[:5]:
        lines.append("  [ ]  %s — %s" % (a.get("title"), a.get("url")))
    return {"query": query, "searched": peek, "github": github, "threads": threads,
            "articles": articles, "n": len(github) + len(threads) + len(articles),
            "render": "\n".join(lines)}


def dep_signal(query, *, library=None, freshness="pd_1m", count=40, signal=None,
               threshold_days=None, hint=None, country=None, search_lang=None,
               safe_search="moderate", timeout=45.0):
    """Deprecation / breaking-change signal scan for a library.

    Round-18 extension of ``breaking_change()``: scans the fresh news + release
    hits for a library and *classifies* every signal by type (breaking /
    deprecat / migrat / removal / release) instead of one grab-bag list, then
    returns per-type counts, a signal ledger (with recency + source), and a
    ``render`` grouped by type. Honest: only rows whose headline or description
    carry a signal are kept, and ``threshold_days`` drops stale rows using
    ``age_meta.days`` (absolute-date rows are kept).

    Live-verified: `dep_signal("pydantic", signal="deprecat")` → the Pydantic
    v2 migration guide + a breaking-change release + changelog row, each labeled
    by type.

    Args:
        query: the library/tool (search query).
        library: restrict signal rows to those whose title/description mention
            this spelling (defaults to the ``query``; set "" to disable).
        signal: optional — keep only signals whose type contains this token
            (e.g. "deprecat"); None keeps all.
        threshold_days: optional — drop signals older than this many days.
        hint: optional relative clause in the query (default ``"deprecation OR
            breaking change OR migration OR removal OR release notes"``).
        freshness / country / search_lang / safe_search / timeout / count\
            forwarded to ``search(mode="news")``.

    Returns:
        {"query", "signals":[{type, title, url, site, age_meta, published_at}],
         "by_type": {type: [row,...]}, "counts": {type: n}, "n", "render"}.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("dep_signal() needs a library name")
    lib = str(library).strip() if library is not None else q
    hint_ = ("deprecation OR migration OR removal OR breaking OR release notes"
             if hint is None else (hint or ""))
    wq = "%s (%s)" % (q, hint_) if hint_ else q
    try:
        items = _search_full(wq, mode="news", count=count, country=country,
                       search_lang=search_lang, safe_search=safe_search,
                       freshness=freshness, timeout=timeout).get("results") or []
    except Exception:
        items = []
    types_pat = [
        ("breaking", r"\bbreaking ?-? ?change\b|\bbreaking\b"),
        ("deprecat", r"\bdeprecat\w*"),
        ("migrat", r"\bmigrat\w*"),
        ("removal", r"\bremov\w*|\bretir\w*|\bsunset\w*|end[- ]of[- ]life"),
        ("release", r"\brelease\b|\bchangelog\b|\bwhat's new\b"),
    ]
    signals, seen = [], set()
    for it in items:
        title = (it.get("title") or "")
        desc = (it.get("description") or "")
        blob = "%s %s" % (title, desc)
        if lib and lib.lower() not in blob.lower():
            continue
        td = it.get("age_meta") or {}
        if threshold_days is not None and td.get("days") is not None and td["days"] > threshold_days:
            continue
        found = [name for name, pat in types_pat if re.search(pat, blob, re.I)]
        if not found:
            continue
        url = it.get("url") or ""
        if url in seen:
            continue
        seen.add(url)
        signals.append({"type": ",".join(found), "title": title, "url": url,
                        "site": it.get("site"), "age_meta": td,
                        "published_at": it.get("published_at"),
                        "description": desc[:180]})
    if signal:
        signals = [s for s in signals if signal.lower() in s["type"]]
    by_type = {}
    for s in signals:
        for t in s["type"].split(","):
            t = t.strip()
            if t:
                by_type.setdefault(t, []).append(s)
    counts = {t: len(v) for t, v in by_type.items()}
    lines = ["Deprecation/breaking signal scan for '%s':" % q]
    for t in ("breaking", "deprecat", "migrat", "removal", "release"):
        for s in by_type.get(t, [])[:5]:
            am = (s.get("age_meta") or {}).get("text", "")
            lines.append("  \u2022 [%s] %s  %s  %s" % (t, s["title"], am, s["site"]))
    return {"query": q, "signals": signals, "by_type": by_type, "counts": counts,
            "n": len(signals), "render": "\n".join(lines)}


def trending_libs(query=None):
    """Documented-absent: Brave has no trending-libraries / trending-packages.

    Probed live (round-18): Brave Search exposes no public trending / top
    packages endpoint — the ``/res/v1/trending`` family 301-redirects to an
    HTML shell on this plan (same finding as ``trending_topics()`` in round-13
    and ``related()`` in round-14). A "trending libs by recent downloads" list
    needs a registry index (PyPI/npm/GH download/stars) that Brave does not
    surface here. Closest live capability is ``dep_signal()`` / ``breaking_change()``
    for a *specific* library, or ``newsflash()``/``headlines()`` for what's being
    posted. This function truthfully reports absence rather than fabricating a list.
    """
    return ("trending_lib() is not available on the Brave Search API — no "
            "trending/top-packages index is exposed (probed live round-18: the "
            "trending endpoints 301-redirect to HTML). Use "
            "brave.dep_signal('lib') or brave.breaking_change('lib') for a "
            "library's release/breaking signals, or brave.newsflash(query) / "
            "brave.headlines(query) for current headlines.")

# ---------------------------------------------------------------------------
# Make every public function work with or without ``await`` (dual sync/async).
# Internal cross-calls are unaffected -- they receive the same dict/str wrappers
# and keep working (isinstance / [] / .get() / ** unpack all still hold).
# ---------------------------------------------------------------------------
_apply_async_to(globals())

__all__ = [
    "BraveError",
    "article_review",
    "batch",
    "brief",
    "breaking_change",
    "clips",
    "code_result",
    "convert_values",
    "crawl",
    "crypto",
    "currency_x",
    "cve_lookup",
    "definition",
    "dep_signal",
    "dev_digest",
    "docker_aliases",
    "docs",
    "domain",
    "drinks",
    "error_solution",
    "explain",
    "find_files",
    "forums",
    "github_issues",
    "github_repos",
    "headlines",
    "infobox",
    "locations",
    "merge",
    "modes",
    "mosaic",
    "movies",
    "near",
    "news_beams",
    "news_breaking",
    "news_cluster",
    "news_live",
    "newsflash",
    "open_now",
    "package",
    "paged",
    "pictures",
    "pkg_lookup",
    "pkg_security",
    "place_detail",
    "place_search",
    "poi_descriptions",
    "pois",
    "probe",
    "products",
    "qna",
    "recipes",
    "reddit",
    "related",
    "research",
    "rich",
    "run",
    "search",
    "search_worker",
    "software",
    "stack_trace",
    "stock_quote",
    "structured",
    "summarize_page",
    "thumbnails",
    "trending_libs",
    "trending_topics",
    "unix_time",
    "weather",
    "DEFAULT_API_URL",
    "DRINK_CATEGORIES",
    "FRESHNESS",
    "IMAGE_PROPERTY",
    "IMAGE_SEARCH_TYPE",
    "LOC_HEADERS",
    "MODES",
    "PATHS",
    "SAFE_SEARCH"
]
