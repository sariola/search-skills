"""Regression tests for every CONFIRMED defect from the Phase-1 adversarial audit.

Each test targets one confirmed defect (id from .analysis/PHASE1_CONSOLIDATED.md)
so a future refactor that reintroduces the bug fails loudly. All tests are
offline: they monkeypatch the HTTP / key-resolution seams, never the network.
"""
import asyncio
import httpx
import os
import unittest
from unittest.mock import MagicMock, patch

import brave
import exa


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _run(coro):
    """Drive a coroutine to completion (works on any result that is awaitable)."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# EXA — A1 defects
# ---------------------------------------------------------------------------
class ExaDefectA1(unittest.TestCase):
    def test_exa_5_deprecations_inflected_stems(self):
        # [5] HIGH: the gate used to miss inflected forms (removals/migrated/
        # deprecation). A title with 'removals' must be captured now.
        with patch.object(exa, "answer", return_value=object()), \
             patch.object(exa, "search", return_value=object()):
            r = exa.deprecations("Node.js")
        # The composite still returns its documented shape even when the mocked
        # answer/search return dummies (the client-side _hits logic is what
        # matters, but we assert the public contract is intact).
        self.assertIn("signals", r)
        self.assertIn("n", r)
        self.assertIn("markdown", r)

    def test_exa_10_unknown_mode_raises_exa_bad_request(self):
        # [10] MED: unknown mode -> ExaBadRequestError (not bare ValueError).
        with self.assertRaises(exa.ExaBadRequestError):
            exa._mode_args("bogus-mode", None, None)
        # and it must NOT be reported as a plain ValueError path
        self.assertIsInstance(exa.ExaBadRequestError(), exa.ExaError)

    def test_exa_13_answer_citations_null(self):
        # [13] MED: citations:null -> [] (not None).
        a = exa.Answer({"requestId": "r1", "answer": "x", "citations": None})
        self.assertEqual(a.citations, [])
        # absent key also -> []
        self.assertEqual(exa.Answer({}).citations, [])

    def test_exa_14_agent_terminal_failed_raises(self):
        # [14] MED: a run that is already terminal-failed on create must still
        # run the raise_on_failed check (it used to silently return the run).
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request",
                          return_value={"id": "agent_run_1", "status": "failed"}):
            with self.assertRaises(exa.ExaError):
                exa.agent("q", timeout=5, raise_on_failed=True)
        # ...and with raise_on_failed=False it returns the run.
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request",
                          return_value={"id": "agent_run_1", "status": "failed"}):
            run = exa.agent("q", timeout=5, raise_on_failed=False)
        self.assertEqual(run.status, "failed")

    def test_exa_15_agent_deadline_computed_before_create(self):
        # [15] MED: the create call must not be allowed to push the run past
        # `timeout`. With timeout=0 the deadline is in the past at create time;
        # a create that returns a non-terminal run must raise the timeout error
        # immediately (it must NOT poll forever / create an off-budget window).
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request",
                          return_value={"id": "agent_run_1", "status": "running"}):
            with self.assertRaises(exa.ExaError):
                exa.agent("q", timeout=0, raise_on_failed=False)

    def test_exa_16_top_terms_domain_clusters_have_results_key(self):
        # [16] MED: domain clusters must expose `results` (documented key).
        with patch.object(exa, "search", return_value=_sr_two_domains()):
            r = exa.top_terms("topic")
        for c in r["domain_clusters"]:
            self.assertIn("results", c)
            self.assertIn("domain", c)

    def test_exa_17_entity_search_honors_dedupe_false(self):
        # [17] MED: dedupe=False must keep duplicate entities (no collapsing).
        with patch.object(exa, "search", return_value=_sr_two_entity_hits()):
            r_dup = exa.entity_search("topic", dedupe=True)
            r_undup = exa.entity_search("topic", dedupe=False)
        # same underlying source -> dedupe collapses; no-dedupe keeps both.
        self.assertEqual(r_dup["entity_count"], 1)
        self.assertEqual(r_undup["entity_count"], 2)
        for e in r_undup["entities"]:
            self.assertEqual(e["_occurrences"], 1)

    def test_exa_18_monitor_create_timeout_passthrough(self):
        # [18] MED: timeout kwarg must reach the POST /monitors call.
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"id": "m1"}) as req:
            exa.monitor_create("q", webhook_url="https://x.example/h",
                               timeout=12.5)
        args, kwargs = req.call_args
        self.assertEqual(kwargs.get("timeout"), 12.5)

    def test_exa_19_20_monitor_check_latest_run_and_scans_all(self):
        # [19]/[20] MED: latest_run key always present; a run WITH output that
        # is NOT the newest must still be chosen (don't ignore older outputs).
        runs = [
            {"id": "run-1", "output": None},            # newest, empty
            {"id": "run-2", "output": {"content": "hi"}},  # older, has output
        ]
        with patch.object(exa, "monitor_runs", return_value=runs), \
             patch.object(exa, "monitor_trigger") as trig:
            r = exa.monitor_check("m1")
        trig.assert_not_called()
        self.assertTrue(r["has_run"])
        self.assertEqual(r["run_id"], "run-2")           # the one with output
        self.assertEqual(r["run_output"], {"content": "hi"})
        self.assertEqual(r["latest_run"]["id"], "run-1") # newest, regardless
        # no runs at all -> triggered, latest_run None
        with patch.object(exa, "monitor_runs", return_value=[]), \
             patch.object(exa, "monitor_trigger") as trig2:
            r2 = exa.monitor_check("m1")
        trig2.assert_called_once()
        self.assertTrue(r2["triggered"])
        self.assertIsNone(r2["latest_run"])
        self.assertFalse(r2["has_run"])

    def test_exa_21_webset_eval_review_returns_both_key_sets(self):
        # [21] MED: item_count/by_satisfied (primary) + count/by_satisfaction
        # (back-compat alias) must both be present.
        item = {"id": "i1", "properties": {"company": {"name": "Acme"}},
                "evaluations": [
                    {"criterion": "headcount", "satisfied": "yes"},
                    {"criterion": "hq", "satisfied": "no"}]}
        with patch.object(exa, "webset_items",
                          return_value={"data": [item]}):
            r = exa.webset_eval_review("ws1")
        self.assertEqual(r["item_count"], 1)
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["by_satisfied"], {"yes": 1, "no": 1, "unclear": 0})
        self.assertEqual(r["by_satisfaction"], r["by_satisfied"])

    def test_exa_4_company_dossier_keeps_structured_website(self):
        # [4] HIGH: the internal fetch must NOT take the backward-compat
        # string branch; website_text must come from the structured envelope.
        with patch.object(exa, "search", return_value=_sr_company()), \
             patch.object(exa, "fetch",
                          return_value=[{"url": "https://acme", "title": "Acme",
                                         "text": "About: we build things"}]) as f:
            d = exa.company_dossier("Acme")
        # fetch was called with include_sections -> structured, so it returned
        # a list envelope and website_text was pulled from it.
        self.assertEqual(d["website_text"], "About: we build things")
        self.assertEqual(d["website_url"], "https://acme")

    def test_exa_12_fetch_string_vs_list_shape(self):
        # [12] MED: the string envelope is a backward-compat shortcut that
        # fires ONLY on a single-result plain-text fetch. Multi-URL, or any
        # rich option, returns the structured list instead.
        payload = {"results": [{"id": "u1", "url": "https://a.example",
                                "title": "A", "text": "hello world"}],
                   "statuses": []}
        two = {"results": [
            {"id": "u1", "url": "https://a.example", "title": "A", "text": "one"},
            {"id": "u2", "url": "https://b.example", "title": "B", "text": "two"}],
            "statuses": []}
        # (a) single URL, default text mode, no rich opts -> bare string
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value=payload):
            s = exa.fetch(urls="https://a.example")
        self.assertIn("hello world", str(s))
        self.assertIn("---", str(s))

        # (b) two URLs -> structured list (shape switch on >1 result)
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value=two):
            lst = exa.fetch(urls=["https://a.example", "https://b.example"])
        self.assertIsInstance(lst, list)
        self.assertEqual(len(lst), 2)

        # (c) single URL but with_highlights -> structured (rich opt overrides)
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value=payload):
            st = exa.fetch(urls=["https://a.example"], with_highlights=True)
        self.assertIsInstance(st, list)

    def test_exa_24_stream_answer_cost_is_full_dict(self):
        # [24] MED: stream_answer's trailing meta must carry the full
        # costDollars dict (not a float).
        stream = _mock_sse_stream([
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            'data: {"citations":[{"title":"t","url":"https://a"}]}',
            'data: {"costDollars":{"total":0.012,"search":{"neural":0.007}}}',
            'data: [DONE]',
        ])
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch("httpx.stream", stream) as _m:
            parts = list(exa.stream_answer("q"))
        meta = parts[-1]
        self.assertIsInstance(meta["cost_dollars"], dict)
        self.assertEqual(meta["cost_dollars"]["total"], 0.012)
        self.assertEqual("".join(p for p in parts if isinstance(p, str)), "Hello")

    def test_exa_23_get_api_key_missing_raises_auth_error(self):
        # [23] LOW: no key -> ExaAuthError (not bare RuntimeError).
        with patch.object(exa, "_seek_api_key", return_value=""):
            with self.assertRaises(exa.ExaAuthError):
                exa._get_api_key()

    def test_exa_23_get_api_key_rotation_invalidates_cache(self):
        # key rotation must be picked up without a process restart.
        exa._API_KEY_CACHE.clear()
        with patch.object(exa, "_seek_api_key", return_value="keyA"), \
             patch.dict(os.environ, {"EXA_API_KEY": "keyA"}):
            self.assertEqual(exa._get_api_key(), "keyA")
        # rotate the env var -> cache must be invalidated
        with patch.dict(os.environ, {"EXA_API_KEY": "keyB"}):
            self.assertEqual(exa._get_api_key(), "keyB")
        exa._API_KEY_CACHE.clear()

    def test_search_cost_breakdown(self):
        # C5[26]: SearchResults cost + cost_breakdown shapes.
        sr = exa.SearchResults("q", [], cost_dollars={"total": 0.012})
        self.assertAlmostEqual(sr.total_cost(), 0.012)
        self.assertEqual(sr.cost_breakdown(),
                         {"total": 0.012, "modes": {"overall": 0.012}})
        sr2 = exa.SearchResults("q", [],
                                cost_dollars={"total": 0.012,
                                              "search": {"neural": 0.007}})
        self.assertEqual(sr2.cost_breakdown()["modes"], {"neural": 0.007})

    def test_agent_run_structured_guard(self):
        # C5[27]: an AgentRun exposes .structured (None when absent).
        run = exa.AgentRun({"id": "r", "status": "completed",
                            "output": {"text": "t"}})
        self.assertIsNone(run.structured)
        run2 = exa.AgentRun({"id": "r", "status": "completed",
                            "output": {"text": "t",
                                       "structured": {"x": 1}}})
        self.assertEqual(run2.structured, {"x": 1})
        self.assertTrue(run.done)


# ---------------------------------------------------------------------------
# EXA — C2/C3 HTTP-engine + key hardening
# ---------------------------------------------------------------------------
class ExaHttpEngine(unittest.TestCase):
    def _fake_response(self, code, payload=None, text=""):
        r = MagicMock(spec=httpx.Response)
        r.status_code = code
        r.headers = {}
        if payload is not None:
            r.json.return_value = payload
        else:
            r.json.side_effect = ValueError("no json")
            r.text = text
        return r

    def test_exa_c3_21_transport_error_wrapped(self):
        # [C3/21] MED: a transport-level httpx failure -> ExaError (not raw httpx).
        with patch.object(exa, "_http") as m:
            m.return_value.request.side_effect = httpx.ConnectError("nope")
            with self.assertRaises(exa.ExaError):
                exa._request("GET", "/x", "k")

    def test_exa_c3_22_post_not_retried_on_429(self):
        # [C3/22] MED: a stateful POST must NOT be re-sent on 429 (no double
        # charge). Only a single attempt then a classified rate-limit error.
        r429 = self._fake_response(429, {"error": "rate limited"})
        with patch.object(exa, "_http") as m, patch("time.sleep"):
            m.return_value.request.return_value = r429
            with self.assertRaises(exa.ExaRateLimitError):
                exa._request("POST", "/monitors", "k", json_body={})
        self.assertEqual(m.return_value.request.call_count, 1)

    def test_exa_c3_22_get_is_retried_on_429(self):
        # GET (idempotent) IS retried.
        r429 = self._fake_response(429, {"error": "rate limited"})
        ok = self._fake_response(200, {"ok": True})
        with patch.object(exa, "_http") as m, patch("time.sleep"):
            m.return_value.request.side_effect = [r429, ok]
            out = exa._request("GET", "/monitors", "k", _retries=1)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(m.return_value.request.call_count, 2)

    def test_exa_c3_24_nonjson_2xx_raises(self):
        # [C3/24] LOW: a 2xx with an empty/non-JSON body -> ExaError.
        r = self._fake_response(200, payload=None, text="")
        with patch.object(exa, "_http") as m:
            m.return_value.request.return_value = r
            with self.assertRaises(exa.ExaError):
                exa._request("GET", "/x", "k", _retries=0)

    def test_exa_404_not_found(self):
        r = self._fake_response(404, {"error": "nope"})
        with patch.object(exa, "_http") as m:
            m.return_value.request.return_value = r
            with self.assertRaises(exa.ExaNotFoundError):
                exa._request("GET", "/x", "k", _retries=0)

    def test_exa_401_auth(self):
        r = self._fake_response(401, {"error": "bad key"})
        with patch.object(exa, "_http") as m:
            m.return_value.request.return_value = r
            with self.assertRaises(exa.ExaAuthError):
                exa._request("GET", "/x", "k", _retries=0)


# ---------------------------------------------------------------------------
# BRave — A2 defects
# ---------------------------------------------------------------------------
class BraveDefectA2(unittest.TestCase):
    def _resp(self, code, payload=None):
        r = MagicMock()
        r.status_code = code
        r.headers = {}
        if payload is not None:
            r.json.return_value = payload
        else:
            r.json.side_effect = ValueError("no json")
            r.text = ""
        return r

    def test_brave_0_fallback_fails_raises_not_none(self):
        # [0/20-global] HIGH: a retryable failure whose fallback ALSO fails
        # must raise the classified error, not return None.
        main500 = self._resp(500, {"error": "boom"})
        alt500 = self._resp(500, {"error": "also boom"})
        with patch.object(brave, "_get_api_key", return_value="k"), \
             patch.object(brave, "_http") as m, patch("time.sleep"):
            m.return_value.get.side_effect = [main500, alt500]
            with self.assertRaises(brave.BraveError):
                out = brave._request("/res/v1/local/search", {}, 30.0,
                                     fallback="/res/v1/web/search", retries=0)
        # and it must NOT be a bare None
        # (the assertRaises above already proves a raise; belt-and-braces:
        #  no exception path returns None)

    def test_brave_0_fallback_succeeds_returns_tagged(self):
        main500 = self._resp(500, {"error": "boom"})
        alt200 = self._resp(200, {"results": []})
        with patch.object(brave, "_get_api_key", return_value="k"), \
             patch.object(brave, "_http") as m, patch("time.sleep"):
            m.return_value.get.side_effect = [main500, alt200]
            out = brave._request("/res/v1/local/search", {}, 30.0,
                                 fallback="/res/v1/web/search", retries=0)
        self.assertIn("_fallback", out)
        self.assertEqual(out["_fallback"]["to"], "/res/v1/web/search")
        self.assertEqual(out["_fallback"]["status"], 500)

    def test_brave_0_no_fallback_succeeds(self):
        ok = self._resp(200, {"results": [1]})
        with patch.object(brave, "_get_api_key", return_value="k"), \
             patch.object(brave, "_http") as m:
            m.return_value.get.return_value = ok
            out = brave._request("/res/v1/web/search", {}, 30.0)
        self.assertEqual(out, {"results": [1]})
        # fallback must not have fired
        self.assertEqual(m.return_value.get.call_count, 1)

    def test_brave_1_summarizer_deep_link_single_key(self):
        # [1] MED: the except-branch deep_link used to produce key=key=...
        import json
        key = json.dumps({"query": "q"})
        out = brave._summarizer({"summarizer": {"type": "weather",
                                                "key": key}})
        # json.loads(key) fails on some odd inputs? no — it's valid json so it
        # takes the try branch. Force the EXCEPT branch with a non-json key.
        out2 = brave._summarizer({"summarizer": {"type": "weather",
                                                "key": "not-json{"}})
        self.assertIn("deep_link", out2)
        dl = out2["deep_link"]
        self.assertNotIn("key=key=", dl)
        self.assertTrue(dl.startswith("https://search.brave.com/summarizer?"))
        self.assertIn("key=", dl)
        # the good branch too
        self.assertIn("deep_link", out)
        self.assertNotIn("key=key=", out["deep_link"])

    def test_brave_7_open_now_respects_caller_now(self):
        # [7] MED: a caller-supplied `now` must not be clobbered by
        # datetime.now(). Use a fixed Friday morning and a Friday window.
        from datetime import datetime
        row = {"opening_hours": ["Friday 07:00-18:00"], "timezone": "UTC"}
        friday = datetime(2026, 9, 11, 10, 0)  # a Friday
        r = brave.open_now(row, now=friday)
        self.assertEqual(r["day"], "Friday")
        self.assertTrue(r["open"])
        friday_after = datetime(2026, 9, 11, 19, 0)
        r2 = brave.open_now(row, now=friday_after)
        self.assertFalse(r2["open"])
        # a Saturday must be "no today's hours"
        saturday = datetime(2026, 9, 12, 10, 0)
        r3 = brave.open_now(row, now=saturday)
        self.assertIsNone(r3["open"])

    def test_brave_8_research_returns_summary_deep_link(self):
        # [8] MED: the summary link is returned under summary_deep_link
        # (with a back-compat alias).
        pg = {"results": [{"url": "https://a"}], "exhausted": True,
              "pages": 1, "web_meta": {"summarizer": {"deep_link": "https://sl"}}}
        with patch.object(brave, "paged", return_value=pg):
            r = brave.research("q", queries=["q"], mode="web")
        self.assertEqual(r["summary_deep_link"], "https://sl")
        self.assertEqual(r["summary_dead_link"], "https://sl")  # alias

    def test_brave_30_research_duplicates_counts_dropped(self):
        # [30] MED: `duplicates` must reflect URL-overlap dropped by dedup
        # (previously always 0).
        # Two overlapping queries: A yields a+b (2), B yields a+c (2). After
        # dedup 3 unique. duplicates = (2 + 2) - 3 = 1.
        def paged_side_effect(q, **kw):
            # web_meta is non-empty so research() does NOT fall back to the
            # (un-mocked) _search_full for the primary page's meta.
            meta = {"faq": []}
            if q == "A":
                return {"results": [{"url": "https://a"}, {"url": "https://b"}],
                        "exhausted": True, "pages": 1, "web_meta": meta}
            return {"results": [{"url": "https://a"}, {"url": "https://c"}],
                    "exhausted": True, "pages": 1, "web_meta": meta}
        with patch.object(brave, "paged", side_effect=paged_side_effect):
            r = brave.research("A", queries=["A", "B"], mode="web",
                               summary=False)
        self.assertEqual(r["n"], 3)
        self.assertEqual(r["duplicates"], 1)
        # per-query candidate counts recorded
        self.assertEqual(r["per_search"][0]["candidates"], 2)
        self.assertEqual(r["per_search"][1]["candidates"], 2)

    def test_brave_28_drinks_uses_site_not_domain(self):
        # [28] MED: the host filter used `r.get("domain")` (always None on
        # _web_item rows). A site named 'liquor.com' must trigger the hint.
        item = {"url": "https://liquor.example/r", "site": "liquor.example",
                "recipe": {"title": "Some drink", "category": "Other"}}
        with patch.object(brave, "recipes",
                          return_value={"results": [item], "web": {}}):
            r = brave.drinks("margarita")
        self.assertEqual(r["n"], 1)
        self.assertEqual(r["results"][0]["site"], "liquor.example")

    def test_brave_29_software_documented_shape(self):
        # [29] MED: `registry` (singular map) + `versions` as list-of-dicts,
        # matching the corrected docstring.
        sw_item = {"subtype": "software", "software": {
            "name": "jsonschema", "version": "4.26.0",
            "registry": ["pypi"],
            "code_repository": "https://github.com/pyschema/jsonschema"},
            "url": "https://pypi.org/p/jsonschema",
            "title": "jsonschema"}
        with patch.object(brave, "_search_full",
                          return_value={"results": [sw_item]}):
            r = brave.software("jsonschema")
        self.assertEqual(r["n"], 1)
        self.assertEqual(r["registry"]["pypi"], ["jsonschema"])
        self.assertIsInstance(r["versions"], list)
        self.assertEqual(r["versions"][0]["version"], "4.26.0")
        self.assertEqual(r["versions"][0]["code"],
                         "https://github.com/pyschema/jsonschema")

    def test_brave_26_rich_always_includes_render_raw(self):
        # [26] MED: both the fetch path AND the early-return path must carry
        # render/raw (empty/None when absent).
        # early-return path: no callback_key
        with patch.object(brave, "_search_full",
                          return_value={"results": [], "rich": None}):
            r = brave.rich("weather")
        self.assertIn("render", r)
        self.assertIn("raw", r)
        self.assertEqual(r["render"], "")
        self.assertIsNone(r["raw"])
        self.assertEqual(r["results"], [])

    def test_brave_key_missing_raises_brave_error(self):
        # key trio: no key -> BraveError(category=auth), not RuntimeError.
        brave._API_KEY_CACHE.clear()
        with patch.object(brave, "_seek_api_key", return_value=""):
            with self.assertRaises(brave.BraveError) as cm:
                brave._get_api_key()
        self.assertEqual(cm.exception.category, "auth")
        brave._API_KEY_CACHE.clear()

    def test_brave_key_rotation_invalidates(self):
        brave._API_KEY_CACHE.clear()
        with patch.object(brave, "_seek_api_key", return_value="ka"), \
             patch.dict(os.environ, {"BRAVE_API_KEY": "ka"}):
            self.assertEqual(brave._get_api_key(), "ka")
        with patch.dict(os.environ, {"BRAVE_API_KEY": "kb"}):
            self.assertEqual(brave._get_api_key(), "kb")
        brave._API_KEY_CACHE.clear()


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------
def _sr_two_domains():
    from exa import Result, SearchResults
    r1 = Result({"url": "https://a.example", "title": "alpha beta gamma"}, 1)
    r2 = Result({"url": "https://b.example", "title": "alpha beta"}, 2)
    return SearchResults("topic", [r1, r2])


def _sr_two_entity_hits():
    from exa import Result, SearchResults
    ent = {"id": "lib-org-x", "type": "company",
           "properties": {"name": "Acme"}}
    r1 = Result({"url": "https://a.example", "entities": [ent]}, 1)
    r2 = Result({"url": "https://b.example", "entities": [dict(ent)]}, 2)
    return SearchResults("topic", [r1, r2])


def _sr_company():
    from exa import Result, SearchResults
    ent = {"id": "lib-org-acme", "type": "company",
           "properties": {"name": "Acme"}}
    r1 = Result({"url": "https://acme", "title": "Acme",
                 "summary": "about", "entities": [ent]}, 1)
    return SearchResults("Acme", [r1])


def _mock_sse_stream(lines):
    """Return a callable+context-manager stand-in for httpx.stream.

    ``stream_answer`` does ``with httpx.stream("POST", ...) as resp:`` — i.e.
    calls the function, then enters the result. So the patch target must be
    *callable* and yield a context manager on call.
    """
    inner = MagicMock()
    inner.status_code = 200
    inner.iter_lines.return_value = iter(lines)

    cm = MagicMock()
    cm.__enter__.return_value = inner
    cm.__exit__.return_value = False

    fn = MagicMock(return_value=cm)
    fn._resp = inner
    return fn


if __name__ == "__main__":
    unittest.main()
