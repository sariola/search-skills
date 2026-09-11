"""Coverage-gap tests (Phase-1 C5 [26]-[33]).

Fills the offline test coverage for the documented public helpers the existing
suite did not exercise: cost/grounding/cost-report, agent structured output +
grounding, fetch shape switching, brave merge dedupe/tagging, probe /
summarize_page, rich verticals, place_search guards, and a whole-public-surface
no-crash smoke pass.
"""
import inspect
import httpx
import unittest
from unittest.mock import MagicMock, patch

import brave
import exa


# ---------------------------------------------------------------------------
# EXA
# ---------------------------------------------------------------------------
class ExaCostGrounding(unittest.TestCase):
    # C5[26] SearchResults.to_agent cost + grounding
    def test_search_to_agent_cost_and_grounding(self):
        r = exa.Result({"url": "https://a", "title": "A", "text": "t"}, 1)
        sr = exa.SearchResults(
            "q", [r],
            request_id="req_1",
            cost_dollars={"total": 0.01},
            output={"content": "synth",
                    "grounding": [{"field": "f", "citations": []}]},
        )
        agent = sr.to_agent()
        self.assertEqual(agent["query"], "q")
        self.assertEqual(agent["answer"], "synth")
        self.assertEqual(agent["cost"], 0.01)
        self.assertEqual(agent["request_id"], "req_1")
        self.assertEqual(agent["grounding"],
                         [{"field": "f", "citations": []}])
        # to_meta carries the raw cost dict
        self.assertEqual(sr.to_meta()["cost_dollars"]["total"], 0.01)
        # cost_breakdown exposes a flat total as an "overall" mode
        self.assertEqual(sr.cost_breakdown()["total"], 0.01)
        self.assertIn("overall", sr.cost_breakdown()["modes"])

    def test_run_returns_readable_string(self):
        r = exa.Result({"url": "https://a", "title": "A", "text": "body"}, 1)
        sr = exa.SearchResults("q", [r])
        with patch.object(exa, "search", return_value=sr):
            s = exa.run("q")
        self.assertIn("1. A", s)
        self.assertIn("https://a", s)

    # C5[27] answer / agent_structured cost + grounding + structured-guard
    def test_answer_returns_answer_obj(self):
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request",
                          return_value={"requestId": "r", "answer": "A",
                                        "citations": [{"url": "https://x"}],
                                        "costDollars": {"total": 0.02}}):
            a = exa.answer("q")
        self.assertEqual(a.answer, "A")
        self.assertEqual(a.sources, ["https://x"])
        self.assertEqual(a.cost_dollars["total"], 0.02)

    def test_agent_structured_returns_agent_run(self):
        run = exa.AgentRun(
            {"id": "agent_run_1", "status": "completed",
             "output": {"text": "x", "structured": {"k": 1}},
             "costDollars": {"total": 0.03}})
        with patch.object(exa, "agent", return_value=run):
            got = exa.agent_structured("q", {"type": "object"})
        self.assertEqual(got.structured, {"k": 1})
        self.assertTrue(got.done)
        self.assertEqual(got.cost, 0.03)

    def test_agent_structured_raises_when_no_structured(self):
        run = exa.AgentRun(
            {"id": "agent_run_1", "status": "completed",
             "output": {"text": "x"}})
        with patch.object(exa, "agent", return_value=run):
            with self.assertRaises(exa.ExaError):
                exa.agent_structured("q", {"type": "object"})

    def test_agent_run_grounding_citations_and_connects(self):
        run = exa.AgentRun(
            {"id": "r", "status": "completed",
             "output": {"grounding": [
                 {"field": "f", "citations": [
                     {"url": "https://u1", "title": "T1"}]},
                 {"field": "g", "citations": [
                     {"url": "https://u1", "title": "T1"}]}]},
             "usage": {"dataSources": {"fiber": 2}},
             "costDollars": {"total": 1.0, "dataSources": {"fiber": 0.5}}})
        self.assertEqual(run.sources, ["https://u1"])
        self.assertEqual(len(run.citations), 1)
        # Connect breakdown: provider present only when it was used.
        self.assertEqual(run.connects, {"fiber": {"calls": 2, "cost_usd": 0.5}})
        self.assertEqual(run.connect_providers, ["fiber"])
        self.assertEqual(run.connect_cost_usd, 0.5)


# ---------------------------------------------------------------------------
# BRAVE
# ---------------------------------------------------------------------------
class BraveMerge(unittest.TestCase):
    # C5[29] merge URL dedupe + per-source _query tagging
    def test_merge_dedupes_and_tags_query(self):
        a = {"query": {"original": "alpha"},
             "results": [{"url": "https://x", "title": "X"},
                         {"url": "https://y", "title": "Y"}]}
        b = {"query": {"original": "beta"},
             "results": [{"url": "https://x", "title": "X2"},
                         {"url": "https://z", "title": "Z"}]}
        merged = brave.merge(a, b)
        # x deduped (first wins)
        self.assertEqual(len(merged), 3)
        urls = [m["url"] for m in merged]
        self.assertEqual(urls.count("https://x"), 1)
        # each item tagged with its source query
        self.assertEqual(merged[0]["_query"], "alpha")
        self.assertEqual(merged[2]["_query"], "beta")
        # dedupe off -> keep both x
        merged_all = brave.merge(a, b, dedupe=False)
        self.assertEqual(len(merged_all), 4)


class BraveProbe(unittest.TestCase):
    # C5[30] probe / summarize_page success semantics
    def _resp(self, code=200, text="", ctype="text/html", url="https://a"):
        r = MagicMock()
        r.status_code = code
        r.url = url
        r.headers = {"content-type": ctype}
        r.text = text
        return r

    def test_probe_success_extracts_text_and_title(self):
        html = ("<html><head><title>T &amp; Co</title></head>"
                "<body><p>hello world</p></body></html>")
        with patch.object(brave, "_http") as m:
            m.return_value.get.return_value = self._resp(text=html)
            r = brave.probe("https://a")
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["title"], "T & Co")
        self.assertIn("hello world", r["text"])
        self.assertFalse(r["truncated"])

    def test_probe_non_200_raises(self):
        with patch.object(brave, "_http") as m:
            m.return_value.get.return_value = self._resp(code=404, text="nf")
            with self.assertRaises(brave.BraveError):
                brave.probe("https://a")

    def test_probe_network_error_wrapped(self):
        with patch.object(brave, "_http") as m:
            m.return_value.get.side_effect = httpx.ConnectError("x")
            with self.assertRaises(brave.BraveError):
                brave.probe("https://a")

    def test_summarize_page(self):
        # A body of three dense paragraphs so lead + points extraction has work.
        para = ("The subject of this document is a detailed, multi-paragraph "
                "explanation that goes on for a fair length so that the text "
                "extraction path has meaningful material to summarise here.")
        html = ("<html><head><title>P</title></head><body>"
                f"<p>{para}</p><p>{para}</p><p>{para}</p></body></html>")
        with patch.object(brave, "_http") as m:
            m.return_value.get.return_value = self._resp(text=html)
            r = brave.summarize_page("https://a", max_points=3)
        self.assertEqual(r["url"], "https://a")
        self.assertEqual(r["title"], "P")
        self.assertTrue(r["points"])
        self.assertIn("text", r)
        self.assertIsInstance(r["summary"], str)


class BraveRich(unittest.TestCase):
    # C5[31] rich verticals: hint-then-fetch + per-vertical normalization
    _WEATHER_BLOCK = {
        "location": {"name": "San Francisco", "country": "US"},
        "current_time_iso": "2026-09-11T00:00:00Z",
        "current_weather": {"ts": "1", "temp": 20, "feels_like": 19,
                            "wind": {"speed": 10, "deg": 90},
                            "weather": {"description": "clear", "main": "clear"}},
    }

    def test_rich_hint_then_fetch(self):
        hint = {"hint": {"callback_key": "ck", "vertical": "weather"}}
        payload = {"results": [
            {"subtype": "weather", "weather": self._WEATHER_BLOCK,
             "provider": {"name": "OpenWeather"}}]}
        with patch.object(brave, "_search_full",
                          return_value={"results": [], "rich": hint}), \
             patch.object(brave, "_rich_fetch", return_value=payload):
            r = brave.rich("weather in san francisco", fetch=True)
        self.assertEqual(r["callback_key"], "ck")
        self.assertEqual(r["vertical"], "weather")
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["subtype"], "weather")
        # per-vertical normalization ran: the weather block was flattened
        self.assertEqual(r["results"][0]["data"]["location"]["name"],
                         "San Francisco")
        self.assertIsInstance(r["render"], str)

    def test_rich_fetch_false_stops_at_hint(self):
        hint = {"hint": {"callback_key": "ck", "vertical": "weather"}}
        with patch.object(brave, "_search_full",
                          return_value={"results": [], "rich": hint}), \
             patch.object(brave, "_rich_fetch") as rf:
            r = brave.rich("weather", fetch=False)
        rf.assert_not_called()
        self.assertEqual(r["callback_key"], "ck")
        self.assertEqual(r["results"], [])

    def test_rich_vertical_normalization(self):
        # _rich_result normalizes a raw vertical record; stocks block key is
        # "stock" for subtype "stocks".
        out = brave._rich_result(
            {"subtype": "stocks",
             "stock": {"asset_info": {"symbol": "AAPL"},
                       "quote": {"company_name": "Apple Inc"}}})
        self.assertEqual(out["subtype"], "stocks")
        self.assertEqual(out["data"]["symbol"], "AAPL")
        self.assertEqual(out["data"]["company_name"], "Apple Inc")


class BravePlaceSearch(unittest.TestCase):
    # C5[32] place_search resolved-anchor + coordinate-intent + validation guards
    def test_place_search_no_anchor_raises(self):
        with self.assertRaises(ValueError):
            brave.place_search("coffee")  # no lat/lon, no location

    def test_place_search_mismatched_coords_raises(self):
        with self.assertRaises(ValueError):
            brave.place_search("coffee", latitude=37.0)  # lon missing

    def test_place_search_count_out_of_range(self):
        with self.assertRaises(ValueError):
            brave.place_search(location="san francisco", count=0)

    def test_place_search_resolved_anchor(self):
        payload = {"results": [{"title": "Cafe", "url": "https://c"}],
                   "location": {"name": "San Francisco", "country": "US",
                                "coordinates": [37.77, -122.41]}}
        with patch.object(brave, "_get_api_key", return_value="k"), \
             patch.object(brave, "_request", return_value=payload) as req:
            r = brave.place_search("cafe", latitude=37.77, longitude=-122.41)
        self.assertEqual(r["resolved"]["name"], "San Francisco")
        self.assertEqual(r["resolved"]["coordinates"], [37.77, -122.41])
        self.assertEqual(r["n"], 1)
        # latitude/longitude were forwarded as the params dict (2nd arg)
        args, kwargs = req.call_args
        params = args[1] if len(args) > 1 else kwargs.get("params")
        self.assertEqual(params.get("latitude"), 37.77)
        self.assertEqual(params.get("longitude"), -122.41)


# ---------------------------------------------------------------------------
# C5[33] whole-public-surface smoke pass
# ---------------------------------------------------------------------------
def _zero_arg_functions(mod):
    """Public functions whose signature has no required positional or keyword
    argument — i.e. callable with just their defaults."""
    out = []
    for n in mod.__all__:
        o = getattr(mod, n)
        if not inspect.isfunction(o) or n.startswith("_"):
            continue
        sig = inspect.signature(o)
        if any(p.default is inspect.Parameter.empty
               for p in sig.parameters.values()
               if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD,
                             p.KEYWORD_ONLY)):
            continue
        out.append(o)
    return out


def _patch_seams(mod):
    """Patch the HTTP + key seams so a default call is a no-op. Returns a list
    of started mock contexts (stop them in reverse)."""
    ctxs = [patch.object(mod, "_get_api_key", return_value="k"),
            patch.object(mod, "_request",
                         return_value={"results": [], "data": []})]
    if hasattr(mod, "_request_list"):
        ctxs.append(patch.object(mod, "_request_list",
                                 return_value={"results": []}))
    ctxs.append(patch.object(mod, "_http"))
    for c in ctxs:
        c.start()
    resp = MagicMock(status_code=200, json=lambda: {}, text="",
                     headers={"content-type": "text/html"},
                     url="https://a")
    mod._http.return_value.get.return_value = resp
    mod._http.return_value.request.return_value = resp
    return ctxs


class PublicSurfaceSmoke(unittest.TestCase):
    def test_every_exported_name_resolves(self):
        """Every name in __all__ is a real, importable module attribute — the
        invariant that catches a typo'd or dangling export."""
        for mod in (exa, brave):
            for name in mod.__all__:
                self.assertTrue(hasattr(mod, name),
                                f"{mod.__name__}.{name} missing from module")

    def test_every_exported_function_is_documented(self):
        """Every public function exported by __all__ has a docstring — the MCP
        tool surface is generated from these, so undocumented tools are a
        quality gap."""
        for mod in (exa, brave):
            undocumented = []
            for name in mod.__all__:
                o = getattr(mod, name)
                if inspect.isfunction(o) and not (o.__doc__ or "").strip():
                    undocumented.append(name)
            self.assertEqual(undocumented, [],
                             f"{mod.__name__}: undocumented: {undocumented}")

    def test_default_call_surface_reaches_clean_seam(self):
        """Every public function that is callable with only its defaults must
        reach its network/key seam without a signature/attribute crash. A typed
        ExaError/BraveError/ValueError from that default path is *intended*
        validation (e.g. fetch requires urls), not a crash — so it passes."""
        for mod in (exa, brave):
            called, clean_errors = 0, 0
            for fn in _zero_arg_functions(mod):
                ctxs = _patch_seams(mod)
                try:
                    fn()
                    called += 1
                except (exa.ExaError, brave.BraveError, ValueError) as e:
                    # intended validation on a default call: pass
                    clean_errors += 1
                    _ = e
                except (TypeError, AttributeError):
                    raise
                except Exception as e:
                    raise AssertionError(
                        f"{mod.__name__}.{fn.__name__} crashed on a "
                        f"default call: {type(e).__name__}: {e}")
                finally:
                    for c in reversed(ctxs):
                        c.stop()
            self.assertGreater(called, 0,
                               f"{mod.__name__}: no default-callable surface")


if __name__ == "__main__":
    unittest.main()
