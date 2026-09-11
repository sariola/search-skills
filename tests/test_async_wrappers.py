"""Dual sync/async wrapper tests (Phase-1 cross-cutting finding C1).

Every public function in ``exa`` / ``brave`` is wrapped at module load so its
return value works with OR without ``await``. These tests pin that contract:
the awaitable wrappers must behave exactly like their base types synchronously
(dict access, str methods, list iteration, numeric ops) AND yield plain base
values when awaited.
"""
import asyncio
import inspect
import unittest

import brave
import exa


def _run(awaitable):
    """Drive an awaitable to completion on a fresh, closed event loop."""
    async def driver():
        return await awaitable
    return asyncio.run(driver())


class AsyncWrapperSemantics(unittest.TestCase):
    # _AsyncDict: sync dict behaviour + await -> plain dict
    def test_async_dict(self):
        d = exa._AsyncDict({"a": 1, "b": 2})
        # sync: behaves like a dict
        self.assertEqual(d["a"], 1)
        self.assertIn("b", d)
        self.assertEqual(len(d), 2)
        self.assertEqual(dict(d), {"a": 1, "b": 2})
        # sync .get still works (internal call path relies on this)
        self.assertEqual(d.get("a"), 1)
        self.assertIsNone(d.get("zzz"))
        # await: yields a plain dict, not the wrapper
        out = _run(d)
        self.assertIsInstance(out, dict)
        self.assertNotIsInstance(out, exa._AsyncDict)
        self.assertEqual(out, {"a": 1, "b": 2})

    def test_async_str(self):
        s = exa._AsyncStr("hello")
        self.assertEqual(s.upper(), "HELLO")          # sync str method
        self.assertEqual(len(s), 5)
        out = _run(s)
        self.assertIsInstance(out, str)
        self.assertNotIsInstance(out, exa._AsyncStr)
        self.assertEqual(out, "hello")

    def test_async_list(self):
        l = exa._AsyncList([1, 2, 3])
        self.assertEqual(l[0], 1)                     # sync index
        self.assertEqual(list(l), [1, 2, 3])          # sync iteration
        self.assertIn(2, l)
        out = _run(l)
        self.assertIsInstance(out, list)
        self.assertNotIsInstance(out, exa._AsyncList)
        self.assertEqual(out, [1, 2, 3])

    def test_async_int_float(self):
        i = exa._AsyncInt(7)
        self.assertEqual(i + 3, 10)                   # sync numeric
        self.assertIsInstance(_run(i), int)
        f = exa._AsyncFloat(1.5)
        self.assertEqual(f * 2, 3.0)                  # sync numeric
        self.assertIsInstance(_run(f), float)

    def test_async_bool(self):
        b = exa._AsyncBool(True)
        self.assertTrue(b)                           # sync truthiness
        self.assertIsInstance(_run(b), bool)
        self.assertIs(_run(b), True)

    def test_async_scalar_none(self):
        sc = exa._AsyncScalar(None)
        self.assertEqual(_run(sc), None)

    def test_async_scalar_passthrough(self):
        sc = exa._AsyncScalar({"k": 42})
        # transparent access to the wrapped object
        self.assertEqual(sc["k"], 42)
        self.assertEqual(sc, {"k": 42})
        self.assertTrue(sc)
        self.assertEqual(len(sc), 1)
        self.assertIn("k", sc)


class WrapResult(unittest.TestCase):
    def test_wrap_result_dispatch(self):
        self.assertIsInstance(exa._wrap_result({}), exa._AsyncDict)
        self.assertIsInstance(exa._wrap_result(""), exa._AsyncStr)
        self.assertIsInstance(exa._wrap_result([]), exa._AsyncList)
        self.assertIsInstance(exa._wrap_result(True), exa._AsyncBool)
        self.assertIsInstance(exa._wrap_result(0), exa._AsyncInt)
        self.assertIsInstance(exa._wrap_result(0.0), exa._AsyncFloat)
        self.assertIsInstance(exa._wrap_result(None), exa._AsyncScalar)

    def test_wrap_result_passes_awaitable_through(self):
        """A value that already knows how to be awaited (our result classes)
        is returned untouched — that is the mechanism that lets
        ``await exa.search(...)`` unwrap to the same SearchResults."""
        sr = exa.SearchResults("q", [])
        self.assertIs(exa._wrap_result(sr), sr)


class ResultClassAwaitable(unittest.TestCase):
    """exa's result classes gain ``__await__`` so ``await`` yields the instance
    itself (attribute access / isinstance are unaffected)."""

    def _self_await(self, obj):
        async def g():
            return await obj
        return _run(g())

    def test_result_classes_await_to_self(self):
        r = exa.Result({"url": "https://a", "title": "A"}, 0)
        sr = exa.SearchResults("q", [r])
        a = exa.Answer({"answer": "x", "citations": []})
        ar = exa.AgentRun({"id": "r", "status": "completed"})
        for obj in (r, sr, a, ar):
            out = self._self_await(obj)
            self.assertIs(out, obj, f"{type(obj).__name__} did not await to self")

    def test_result_classes_have_await_attribute(self):
        for name in ("Result", "SearchResults", "Answer", "AgentRun",
                     "AgentTrace"):
            cls = getattr(exa, name)
            self.assertIn("__await__", cls.__dict__,
                          f"{name} missing __await__")


class PublicFunctionsAwaitable(unittest.TestCase):
    """The wrapping is what makes ``await exa.search(...)`` legal. Every
    non-underscore, non-generator, non-async public function must be wrapped so
    that its *call* returns an awaitable; generators/async fns are exempt
    (they are the true streaming paths)."""

    def _is_exempt(self, fn):
        return (inspect.isgeneratorfunction(fn)
                or inspect.isasyncgenfunction(fn)
                or inspect.iscoroutinefunction(fn))

    def test_exa_public_functions_are_wrapped(self):
        for name in exa.__all__:
            fn = getattr(exa, name)
            if not inspect.isfunction(fn) or name.startswith("_"):
                continue
            if self._is_exempt(fn):
                continue
            # the wrapper is detectable: functools.wraps sets __wrapped__
            self.assertTrue(
                hasattr(fn, "__wrapped__") or
                getattr(fn, "__name__", "") != name,
                f"exa.{name} does not look async-wrapped")

    def test_brave_public_functions_are_wrapped(self):
        for name in brave.__all__:
            fn = getattr(brave, name)
            if not inspect.isfunction(fn) or name.startswith("_"):
                continue
            if self._is_exempt(fn):
                continue
            self.assertTrue(
                hasattr(fn, "__wrapped__") or
                getattr(fn, "__name__", "") != name,
                f"brave.{name} does not look async-wrapped")

    def test_wrapped_signature_is_preserved(self):
        """functools.wraps must keep the original signature intact — a public
        API signature change would be detected here even by the diff test."""
        sig = inspect.signature(exa.search)
        self.assertIn("query", sig.parameters)
        sig_b = inspect.signature(brave.search)
        self.assertIn("query", sig_b.parameters)


if __name__ == "__main__":
    unittest.main()
