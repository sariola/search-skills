"""Public-surface invariants (Phase-1: keep the public API backward-compatible).

These tests pin the documented surface so a refactor cannot silently drop a
function, rename a class, or change a signature. Counts are asserted explicitly
so a regression that *adds or removes* an export fails loudly; the signature
check is anchored to a checked-in baseline taken from the Phase-1 audit.
"""
import inspect
import json
import os
import unittest

import brave
import exa

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASELINE = os.path.join(_ROOT, ".analysis", "phase1_signatures.json")

# Intentional feature-adds since the Phase-1 baseline (documented additions,
# not drift): agent_stop + the /batches surface. Anything else that appears
# here is a regression the diff below will catch.
_EXA_INTENDED_NEW = {
    "agent_stop", "batch_create", "batch_get", "batch_list",
    "batch_cancel", "batch_delete", "poll_batch",
}

# Intentional signature drifts (documented behaviour changes, not silent
# churn): deep_research gained a `concurrency` kwarg so its per-query searches
# fan out in parallel instead of running serially.
_EXA_INTENDED_DRIFT = {
    "deep_research",
}

# Intentional feature-adds to the brave surface: Brave's newer retrieval
# endpoints (LLM Context, GNews, suggest, spellcheck) — MCP + Python parity.
# Anything else that appears here is a regression the diff below will catch.
_BRAVE_INTENDED_NEW = {
    "llm_context", "gnews", "suggest", "spellcheck",
}

# No intentional brave signature drifts. Any drift that surfaces is a
# regression the diff below will catch (previously batch's concurrency
# default was "fixed" 6 -> 8 and its return shape was expanded; both reverted
# to the committed behaviour — the docstring was corrected to match instead).
_BRAVE_INTENDED_DRIFT = set()

# Pinned surface counts. exa grew its public surface with the agent-stop +
# batches additions after the Phase-1 baseline; brave grew with the four
# newer retrieval endpoints.
_EXA_COUNT = 131
_BRAVE_COUNT = 82


class SurfaceCounts(unittest.TestCase):
    def test_exa_all_count(self):
        self.assertEqual(len(exa.__all__), _EXA_COUNT,
                         f"exa.__all__ changed from {_EXA_COUNT}")

    def test_brave_all_count(self):
        self.assertEqual(len(brave.__all__), _BRAVE_COUNT,
                         f"brave.__all__ changed from {_BRAVE_COUNT}")

    def test_exa_core_functions_present(self):
        """The load-bearing documented functions must survive any refactor."""
        for name in ("search", "run", "fetch", "answer", "agent",
                     "agent_structured", "agent_stop", "find_similar",
                     "batch_create", "batch_get", "batch_list", "poll_batch",
                     "monitor_create", "monitor_check", "team_info",
                     "webset_preview", "entity_search", "top_terms"):
            self.assertIn(name, exa.__all__, f"exa.{name} missing")

    def test_brave_core_functions_present(self):
        for name in ("search", "run", "paged", "merge", "research", "rich",
                     "probe", "summarize_page", "place_search", "stock_quote",
                     "weather", "structured", "convert_values"):
            self.assertIn(name, brave.__all__, f"brave.{name} missing")


class ErrorHierarchy(unittest.TestCase):
    EXA_ERRORS = ["ExaAuthError", "ExaRateLimitError", "ExaBadRequestError",
                  "ExaPlanError", "ExaNotFoundError", "ExaServerError"]

    def test_exa_error_is_runtime_error(self):
        self.assertTrue(issubclass(exa.ExaError, RuntimeError))

    def test_exa_error_subclasses(self):
        for name in self.EXA_ERRORS:
            cls = getattr(exa, name)
            self.assertTrue(issubclass(cls, exa.ExaError),
                            f"{name} is not a subclass of ExaError")
            self.assertIn(name, exa.__all__, f"{name} not exported")

    def test_brave_error_is_runtime_error(self):
        self.assertTrue(issubclass(brave.BraveError, RuntimeError))
        self.assertIn("BraveError", brave.__all__)


class SignatureStability(unittest.TestCase):
    """No public signature may drift from the Phase-1 baseline except the
    documented feature-adds (which had no baseline entry to differ from)."""

    def _current(self, mod):
        out = {}
        for n in mod.__all__:
            o = getattr(mod, n)
            if inspect.isfunction(o):
                out[n] = str(inspect.signature(o))
        return out

    @staticmethod
    def _load_baseline():
        with open(_BASELINE) as f:
            return json.load(f)

    def test_exa_signatures_unchanged(self):
        base = self._load_baseline().get("exa", {})
        cur = self._current(exa)
        diffs = [k for k in cur if k in base and cur[k] != base[k]]
        # Documented behaviour changes are allowed; anything else is drift.
        self.assertEqual(set(diffs), _EXA_INTENDED_DRIFT,
                         f"exa signature drift: {diffs}")
        missing = [k for k in base if k not in cur]
        self.assertEqual(missing, [], f"exa signatures lost: {missing}")
        new = [k for k in cur if k not in base]
        self.assertEqual(set(new), _EXA_INTENDED_NEW,
                         f"unexpected new exa functions: {new}")

    def test_brave_signatures_unchanged(self):
        base = self._load_baseline().get("brave", {})
        cur = self._current(brave)
        diffs = [k for k in cur if k in base and cur[k] != base[k]]
        # Documented behaviour changes are allowed; anything else is drift.
        self.assertEqual(set(diffs), _BRAVE_INTENDED_DRIFT,
                         f"brave signature drift: {diffs}")
        missing = [k for k in base if k not in cur]
        self.assertEqual(missing, [], f"brave signatures lost: {missing}")
        new = [k for k in cur if k not in base]
        self.assertEqual(set(new), _BRAVE_INTENDED_NEW,
                         f"unexpected new brave functions: {new}")


class DocDefaultConsistency(unittest.TestCase):
    """A documented default must match the actual signature default.

    Catches the drift class where a signature's default value disagrees with
    the docstring's claim. Matches the project's line-based arg style:
    ``pname: <description> (default N)``. If a parameter line states a numeric
    default, it must equal the signature's default for that parameter."""
    import re as _re

    # "<param>: ... (default <N>)" on the parameter's own doc line.
    _PARAM_DEFAULT_RE = _re.compile(
        r"^\s*([A-Za-z0-9_]+):\s*[^()\n]*\(default\s+(\d+)\)")

    def test_kwarg_defaults_match_docstrings(self):
        for mod in (exa, brave):
            for name in list(mod.__all__):
                fn = getattr(mod, name)
                if not inspect.isfunction(fn):
                    continue
                sig = inspect.signature(fn)
                doc = fn.__doc__ or ""
                for line in doc.splitlines():
                    m = self._PARAM_DEFAULT_RE.match(line)
                    if not m:
                        continue
                    pname, claimed = m.group(1), int(m.group(2))
                    if pname not in sig.parameters:
                        continue  # not a parameter line (e.g. nested note)
                    default = sig.parameters[pname].default
                    self.assertEqual(
                        default, claimed,
                        f"{mod.__name__}.{name}({pname}): doc says "
                        f"default {claimed}, signature says {default}")


class ReturnTypes(unittest.TestCase):
    """Pin the documented return types of the primary surfaces."""

    def test_exa_result_classes(self):
        for name, ann in (("Result", exa.Result), ("SearchResults",
                                                   exa.SearchResults),
                          ("Answer", exa.Answer), ("AgentRun", exa.AgentRun),
                          ("AgentTrace", exa.AgentTrace)):
            self.assertTrue(inspect.isclass(ann), f"exa.{name} is not a class")
            self.assertIn(name, exa.__all__)

    def test_searchresults_is_sequence_like(self):
        sr = exa.SearchResults("q", [exa.Result({"url": "u"}, 0)])
        self.assertEqual(len(sr), 1)
        self.assertEqual(sr[0].url, "u")
        self.assertEqual(list(sr)[0].url, "u")
        self.assertIsInstance(sr.to_dicts(), list)


if __name__ == "__main__":
    unittest.main()
