"""Offline Exa 3.0 request mapping: lite, location, highlights, warnings.

Every case mocks HTTP. DeprecationWarning is asserted explicitly so
``python -W error::DeprecationWarning`` (nix flake check) stays green.
"""
import warnings
import unittest
from unittest.mock import patch

import exa


def _search(query="q", **kwargs):
    with patch.object(exa, "_get_api_key", return_value="k"), \
         patch.object(exa, "_request", return_value={"results": []}) as req:
        exa.search(query, **kwargs)
    return req.call_args


class LiteSearch(unittest.TestCase):
    def test_lite_body_and_mode(self):
        _args, kwargs = _search(
            "near me coffee", mode="lite", num_results=3,
            objective="rank cafes open now",
            user_identifier="user_7f3a",
            user_location={"country": "US", "latitude": 37.7, "longitude": -122.4},
            with_highlights=True, highlights_query="hours",
            snapshot_as_of="2026-01-15",
            moderation=True, compliance="hipaa",
            include_domains=["example.com"],
            category="news",
        )
        body = kwargs["json_body"]
        self.assertEqual(body["type"], "lite")
        self.assertEqual(body["objective"], "rank cafes open now")
        self.assertEqual(body["userIdentifier"], "user_7f3a")
        self.assertEqual(body["userLocation"]["latitude"], 37.7)
        self.assertEqual(body["userLocation"]["country"], "US")
        self.assertEqual(body["contents"]["highlights"], {"query": "hours"})
        self.assertEqual(body["contents"]["snapshotAsOf"], "2026-01-15")
        self.assertIs(body["moderation"], True)
        self.assertEqual(body["compliance"], "hipaa")
        self.assertNotIn("Exa-Beta", kwargs.get("extra_headers") or {})

    def test_lite_rejects_unsupported_parameters(self):
        for kwargs in (
            {"with_summary": True},
            {"max_age_hours": 0},
            {"additional_queries": ["q2"]},
            {"system_prompt": "prefer primary sources"},
            {"output_schema": {"type": "text"}},
            {"subpages": 1},
            {"stream": True},
        ):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                with self.assertRaises(exa.ExaBadRequestError):
                    _search("q", search_type="lite", **kwargs)

    def test_unknown_type_is_passed_through(self):
        _args, kwargs = _search("q", search_type="custom-rerank")
        self.assertEqual(kwargs["json_body"]["type"], "custom-rerank")

    def test_legacy_type_warns_and_is_still_sent(self):
        with self.assertWarns(DeprecationWarning):
            _args, kwargs = _search("q", search_type="fast")
        self.assertEqual(kwargs["json_body"]["type"], "fast")

    def test_magic_maps_to_deep(self):
        with self.assertWarns(DeprecationWarning):
            _args, kwargs = _search("q", search_type="magic")
        self.assertEqual(kwargs["json_body"]["type"], "deep")


class LocationAndIdentity(unittest.TestCase):
    def test_country_string(self):
        _args, kwargs = _search("q", user_location="us")
        self.assertEqual(kwargs["json_body"]["userLocation"], "us")

    def test_coordinates_require_a_pair_and_a_range(self):
        with self.assertRaises(exa.ExaBadRequestError):
            _search("q", user_location={"latitude": 10})
        with self.assertRaises(exa.ExaBadRequestError):
            _search("q", user_location={"latitude": 91, "longitude": 0})
        with self.assertRaises(exa.ExaBadRequestError):
            _search("q", user_location={"latitude": 0, "longitude": 0, "city": "Paris"})

    def test_user_identifier_charset(self):
        with self.assertRaises(exa.ExaBadRequestError):
            _search("q", user_identifier="ada@example.com")
        with self.assertRaises(exa.ExaBadRequestError):
            _search("q", user_identifier="x" * 65)

    def test_objective_length(self):
        with self.assertRaises(exa.ExaBadRequestError):
            _search("q", objective="x" * 4097)


class DynamicHighlights(unittest.TestCase):
    def test_dynamic_sends_beta_header(self):
        _args, kwargs = _search(
            "q", highlights_dynamic=True, highlights_verbosity="low",
        )
        self.assertEqual(
            kwargs["json_body"]["contents"]["highlights"],
            {"verbosity": "low", "dynamic": True},
        )
        self.assertEqual(
            kwargs["extra_headers"],
            {"Exa-Beta": exa.DYNAMIC_HIGHLIGHTS_BETA},
        )

    def test_dynamic_rejects_max_characters(self):
        with self.assertRaises(exa.ExaBadRequestError):
            _search("q", highlights_dynamic=True, highlights_max_characters=500)

    def test_verbosity_enum(self):
        with self.assertRaises(exa.ExaBadRequestError):
            _search("q", highlights_verbosity="huge")

    def test_fetch_snapshot_and_user(self):
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"results": [], "statuses": []}) as req:
            exa.fetch(
                urls=["https://example.com"], snapshot_as_of="2026-01-15T00:00:00Z",
                highlights_dynamic=True, user_identifier="acct_1", include_meta=True,
            )
        body = req.call_args.kwargs["json_body"]
        self.assertEqual(body["snapshotAsOf"], "2026-01-15T00:00:00Z")
        self.assertEqual(body["highlights"], {"dynamic": True})
        self.assertEqual(body["userIdentifier"], "acct_1")
        self.assertEqual(req.call_args.kwargs["extra_headers"]["Exa-Beta"], exa.DYNAMIC_HIGHLIGHTS_BETA)

    def test_bad_snapshot(self):
        with self.assertRaises(exa.ExaBadRequestError):
            _search("q", snapshot_as_of="yesterday")


class WarningsAndNotices(unittest.TestCase):
    def test_warnings_are_logged_and_notices_reach_the_model(self):
        payload = {
            "results": [{"title": "T", "url": "https://example.com", "highlights": ["h"]}],
            "warnings": [{
                "code": "IGNORED_FIELD", "path": "/startCrawlDate",
                "message": "startCrawlDate is ignored. Use startPublishedDate.",
            }],
            "notices": [{
                "code": "RESTRICTIONS_RELAXED", "path": "/query",
                "message": "No page matched site:; results omit that operator.",
                "details": {"operator": "site:"},
            }],
        }
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value=payload):
            with self.assertWarns(exa.ExaWarning):
                results = exa.search("q")
        self.assertEqual(results.warnings[0]["code"], "IGNORED_FIELD")
        agent = results.to_agent()
        self.assertEqual(agent["notices"][0]["code"], "RESTRICTIONS_RELAXED")
        self.assertNotIn("warnings", agent)
        self.assertIn("warnings", results.to_meta())
        self.assertIn("notices", results.to_meta())


class DeprecatedRequestFields(unittest.TestCase):
    def test_text_filters_use_include_text(self):
        with self.assertWarns(DeprecationWarning):
            _args, kwargs = _search("q", text_include=["alpha"], text_exclude=["beta"])
        body = kwargs["json_body"]
        self.assertEqual(body["includeText"], ["alpha"])
        self.assertEqual(body["excludeText"], ["beta"])
        self.assertNotIn("text", body)

    def test_crawl_dates_still_sent(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            _args, kwargs = _search(
                "q", start_crawl_date="2020-01-01T00:00:00Z",
                end_crawl_date="2021-01-01T00:00:00Z",
            )
        self.assertTrue(caught)
        body = kwargs["json_body"]
        self.assertEqual(body["startCrawlDate"], "2020-01-01T00:00:00Z")
        self.assertEqual(body["endCrawlDate"], "2021-01-01T00:00:00Z")


class CategoryRules(unittest.TestCase):
    def test_people_reject_exclude_domains_and_dates(self):
        with self.assertRaises(exa.ExaError):
            _search("q", category="people", exclude_domains=["linkedin.com"])
        with self.assertRaises(exa.ExaError):
            _search("q", category="company", start_published_date="2024-01-01T00:00:00Z")

    def test_company_allows_exclude_domains(self):
        _args, kwargs = _search("q", category="company", exclude_domains=["example.com"])
        self.assertEqual(kwargs["json_body"]["excludeDomains"], ["example.com"])
        self.assertEqual(kwargs["json_body"]["category"], "company")

    def test_text_character_cap_matches_the_spec(self):
        _args, kwargs = _search("q", with_text=True, max_characters=1_000_000)
        self.assertEqual(kwargs["json_body"]["contents"]["text"]["maxCharacters"], 1_000_000)
        with self.assertRaises(exa.ExaError):
            _search("q", with_text=True, max_characters=1_000_001)


class OtherEndpoints(unittest.TestCase):
    def test_answer_and_agent_user_identifier(self):
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"answer": "Paris", "citations": []}) as req:
            exa.answer("capital?", user_location={"country": "FR"}, user_identifier="u_1",
                       system_prompt="one word")
        body = req.call_args.kwargs["json_body"]
        self.assertEqual(body["userLocation"], {"country": "FR"})
        self.assertEqual(body["userIdentifier"], "u_1")
        self.assertEqual(body["systemPrompt"], "one word")

        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"id": "r", "status": "completed", "output": {}}) as req:
            exa.agent("task", user_identifier="u_2", effort="ultra")
        self.assertEqual(req.call_args.kwargs["json_body"]["userIdentifier"], "u_2")
        self.assertEqual(req.call_args.kwargs["json_body"]["effort"], "ultra")

    def test_monitor_filter_empty_results_is_inside_contents(self):
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"id": "m"}) as req:
            exa.monitor_create(
                "q", webhook_url="https://example.com/hook", filter_empty_results=True,
            )
        search = req.call_args.kwargs["json_body"]["search"]
        self.assertNotIn("filterEmptyResults", search)
        self.assertIs(search["contents"]["filterEmptyResults"], True)
        self.assertIsNone(req.call_args.kwargs.get("extra_headers"))

    def test_monitor_dynamic_highlights_send_beta_header(self):
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"id": "m"}) as req:
            exa.monitor_create(
                "q", webhook_url="https://example.com/hook",
                highlights_dynamic=True, highlights_verbosity="low",
            )
        highlights = req.call_args.kwargs["json_body"]["search"]["contents"]["highlights"]
        self.assertIs(highlights["dynamic"], True)
        self.assertEqual(highlights["verbosity"], "low")
        self.assertEqual(
            req.call_args.kwargs["extra_headers"]["Exa-Beta"],
            exa.DYNAMIC_HIGHLIGHTS_BETA,
        )

    def test_run_includes_notices(self):
        payload = {
            "results": [{"title": "T", "url": "https://example.com"}],
            "notices": [{"code": "RESTRICTIONS_RELAXED", "path": "/query", "message": "relaxed"}],
        }
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value=payload):
            text = exa.run("q")
        self.assertIn("RESTRICTIONS_RELAXED", text)
        self.assertIn("relaxed", text)


if __name__ == "__main__":
    unittest.main()
