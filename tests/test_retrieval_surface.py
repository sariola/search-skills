"""Offline contract tests for Brave's newer retrieval endpoints.

These pin the *wire shape* of ``brave.llm_context`` / ``brave.gnews`` /
``brave.suggest`` / ``brave.spellcheck`` (paths + query params) without
touching the network, mirroring the style of ``test_search_contracts.py``.
"""
from unittest.mock import patch

import unittest

import brave


def _args_of(mock):
    """(path, params) for the single recorded _request call."""
    c = mock.call_args_list[0]
    # _request(path, params, timeout) — all positional.
    return c.args[0], c.args[1]


class LlmContextWire(unittest.TestCase):
    def test_defaults(self):
        with patch.object(brave, "_request", return_value={}) as req, \
             patch.object(brave, "_get_api_key", return_value="k"):
            out = brave.llm_context("What is the capital of France?")
        path, params = _args_of(req)
        self.assertEqual(path, "/res/v1/llm/context")
        self.assertEqual(params["q"], "What is the capital of France?")
        self.assertEqual(params["maximum_number_of_tokens"], 8192)
        self.assertEqual(params["maximum_number_of_urls"], 20)
        self.assertEqual(params["maximum_number_of_snippets"], 50)
        self.assertEqual(params["maximum_number_of_tokens_per_url"], 4096)
        self.assertEqual(params["maximum_number_of_snippets_per_url"], 50)
        self.assertEqual(params["context_threshold_mode"], "balanced")
        self.assertEqual(params["count"], 20)
        # No enable_local / goggles / country / search_lang in defaults.
        self.assertNotIn("enable_local", params)
        self.assertNotIn("goggles", params)
        self.assertNotIn("country", params)
        self.assertNotIn("search_lang", params)
        self.assertEqual(out["query"], "What is the capital of France?")
        self.assertEqual(out["n_urls"], 0)

    def test_explicit_params(self):
        with patch.object(brave, "_request", return_value={}) as req, \
             patch.object(brave, "_get_api_key", return_value="k"):
            brave.llm_context(
                "Q", count=5, max_tokens=1024, max_urls=1,
                max_snippets=10, max_tokens_per_url=2048,
                max_snippets_per_url=2, threshold_mode="strict",
                enable_local=True, goggles="https://goggles.brave.com/x",
                country="DE", search_lang="de",
            )
        _, params = _args_of(req)
        self.assertEqual(params["count"], 5)
        self.assertEqual(params["maximum_number_of_tokens"], 1024)
        self.assertEqual(params["maximum_number_of_urls"], 1)
        self.assertEqual(params["maximum_number_of_snippets"], 10)
        self.assertEqual(params["maximum_number_of_tokens_per_url"], 2048)
        self.assertEqual(params["maximum_number_of_snippets_per_url"], 2)
        self.assertEqual(params["context_threshold_mode"], "strict")
        self.assertEqual(params["enable_local"], "true")
        self.assertEqual(params["goggles"], "https://goggles.brave.com/x")
        self.assertEqual(params["country"], "DE")
        self.assertEqual(params["search_lang"], "de")

    def test_query_too_long(self):
        with patch.object(brave, "_request", return_value={}), \
             patch.object(brave, "_get_api_key", return_value="k"):
            with self.assertRaises(brave.BraveError) as ctx:
                brave.llm_context("x" * 401)
        self.assertEqual(ctx.exception.category, "bad_request")

    def test_bad_threshold_mode(self):
        with patch.object(brave, "_request", return_value={}), \
             patch.object(brave, "_get_api_key", return_value="k"):
            with self.assertRaises(brave.BraveError) as ctx:
                brave.llm_context("Q", threshold_mode="weird")
        self.assertEqual(ctx.exception.category, "bad_request")

    def test_clamping(self):
        with patch.object(brave, "_request", return_value={}) as req, \
             patch.object(brave, "_get_api_key", return_value="k"):
            brave.llm_context("Q", max_tokens=999999, max_urls=999,
                              max_snippets=9999, max_tokens_per_url=999999)
        _, params = _args_of(req)
        self.assertEqual(params["maximum_number_of_tokens"], 32768)
        self.assertEqual(params["maximum_number_of_urls"], 50)
        self.assertEqual(params["maximum_number_of_snippets"], 256)
        self.assertEqual(params["maximum_number_of_tokens_per_url"], 8192)


class GNewsWire(unittest.TestCase):
    def test_gnews_flag_present(self):
        with patch.object(brave, "_request", return_value={}) as req, \
             patch.object(brave, "_get_api_key", return_value="k"):
            brave.gnews("AI", count=3, freshness="pd_1d", country="US")
        path, params = _args_of(req)
        self.assertEqual(path, "/res/v1/news/search")
        self.assertEqual(params["gnews"], "true")
        self.assertEqual(params["q"], "AI")
        self.assertEqual(params["count"], 3)
        self.assertEqual(params["freshness"], "pd_1d")
        self.assertEqual(params["country"], "US")

    def test_gnews_block_shape(self):
        payload = {
            "results": [{"title": "std", "url": "https://a"}],
            "gnews": {"results": [{"title": "g1", "url": "https://b", "age": "1h"}]},
        }
        with patch.object(brave, "_request", return_value=payload), \
             patch.object(brave, "_get_api_key", return_value="k"):
            out = brave.gnews("AI")
        self.assertEqual(out["n"], 1)
        self.assertEqual(out["gnews_n"], 1)
        self.assertEqual(out["results"][0]["title"], "std")
        self.assertEqual(out["gnews"]["results"][0]["title"], "g1")
        self.assertIn("GNEWS", out["render"])


class SuggestSpellcheckWire(unittest.TestCase):
    def test_suggest_path_and_suggestions(self):
        with patch.object(brave, "_request", return_value={"suggestions": ["a", "b", "c"]}) as req, \
             patch.object(brave, "_get_api_key", return_value="k"):
            out = brave.suggest("ai")
        path, _ = _args_of(req)
        self.assertEqual(path, "/res/v1/suggest/search")
        self.assertEqual(out["n"], 3)
        self.assertEqual(out["suggestions"], ["a", "b", "c"])

    def test_suggest_list_response(self):
        with patch.object(brave, "_request", return_value=["x", "y"]), \
             patch.object(brave, "_get_api_key", return_value="k"):
            out = brave.suggest("ai")
        self.assertEqual(out["n"], 2)
        self.assertEqual(out["suggestions"], ["x", "y"])

    def test_spellcheck_corrected(self):
        with patch.object(brave, "_request", return_value={"corrected": "py"}), \
             patch.object(brave, "_get_api_key", return_value="k"):
            out = brave.spellcheck("p y")
        self.assertEqual(out["corrected"], "py")
        self.assertTrue(out["changed"])

    def test_spellcheck_unchanged(self):
        with patch.object(brave, "_request", return_value={"corrected": "ok"}), \
             patch.object(brave, "_get_api_key", return_value="k"):
            out = brave.spellcheck("ok")
        self.assertFalse(out["changed"])


class PlanGateClassification(unittest.TestCase):
    """A 400 OPTION_NOT_IN_PLAN response must classify as category 'plan'."""

    def test_option_not_in_plan_classifies_as_plan(self):
        # Live shape: code=OPTION_NOT_IN_PLAN carries meta.component=
        # "authentication" too — the discriminator must be the CODE, not the
        # component, or this silently re-classifies to "auth".
        body = {"error": {"code": "OPTION_NOT_IN_PLAN",
                          "detail": "The option is not included in the plan.",
                          "meta": {"component": "authentication"}}}
        err = brave._classify_response("/res/v1/llm/context", 400, body, 30.0)
        self.assertEqual(err.category, "plan")

    def test_invalid_token_still_classifies_as_auth(self):
        # Guard against the plan-gate branch over-capturing real auth failures.
        body = {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID",
                          "meta": {"component": "authentication"}}}
        err = brave._classify_response("/res/v1/web/search", 401, body, 30.0)
        self.assertEqual(err.category, "auth")


if __name__ == "__main__":
    unittest.main()
