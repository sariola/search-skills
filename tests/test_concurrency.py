"""Concurrency invariants for the shared engine + deep_research fan-out.

The two packages ship a byte-identical clean engine: a process-lifetime
``httpx.Client`` and a single process-lifetime ``ThreadPoolExecutor``, with a
``_fanout`` that submits all jobs up front and throttles execution with a
semaphore. These tests pin that behaviour *offline* (no network):

  * concurrent first-use constructs exactly ONE Client / ONE pool (a leaked
    second one would hold an unclosed socket pool or never be shut down);
  * the pool is capped at the keep-alive connection ceiling (extra workers
    can only queue on a connection, so they buy nothing);
  * ``_fanout`` honours ``concurrency`` (peak in-flight == bound), preserves
    input order, and isolates each job's exception;
  * ``deep_research`` fans out its per-query searches in parallel and keeps the
    serial first-error contract.
"""
import os
import threading
import time
import unittest
from unittest import mock

import brave
import exa

# The engine caches a Client + a pool as module globals. Tests that mock the
# constructors must not leak a MagicMock into those globals, or later tests
# that need a *real* pool would submit to a mock. These reset hooks keep the
# caches clean; each test that needs a real pool forces a fresh one in setUp.


def _reset_engines():
    for mod in (exa, brave):
        mod._CLIENT = None
        mod._WORKERS = None


def _hammer(mod, fn_name, reset_attr, n_threads=64):
    """Reset the cached object to None, call ``fn`` from ``n_threads`` threads
    at once, and return the list of returned objects."""
    results, results_lock = [], threading.Lock()
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()
        with results_lock:
            results.append(getattr(mod, fn_name)())

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    setattr(mod, reset_attr, None)  # force the first-use path
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


class EngineSingleton(unittest.TestCase):
    def tearDown(self):
        _reset_engines()  # drop any mock Client/pool created below

    def test_exa_http_client_constructed_exactly_once(self):
        """Double-checked lock: a concurrent first-use must yield ONE Client."""
        with mock.patch("httpx.Client") as spy:
            results = _hammer(exa, "_http", "_CLIENT")
        self.assertEqual(len(set(id(r) for r in results)), 1,
                         "concurrent first-use produced more than one Client")
        self.assertIs(exa._CLIENT, results[0])
        self.assertEqual(spy.call_count, 1,
                         "a leaked second Client was constructed "
                         "(unclosed socket pool)")

    def test_exa_pool_constructed_exactly_once(self):
        with mock.patch.object(exa, "ThreadPoolExecutor") as spy:
            results = _hammer(exa, "_pool", "_WORKERS")
        self.assertEqual(len(set(id(r) for r in results)), 1,
                         "concurrent first-use produced more than one pool")
        self.assertIs(exa._WORKERS, results[0])
        self.assertEqual(spy.call_count, 1,
                         "a second, never-shut-down pool was created")

    def test_brave_http_client_constructed_exactly_once(self):
        with mock.patch("httpx.Client") as spy:
            results = _hammer(brave, "_http", "_CLIENT")
        self.assertEqual(len(set(id(r) for r in results)), 1)
        self.assertIs(brave._CLIENT, results[0])
        self.assertEqual(spy.call_count, 1)

    def test_brave_pool_constructed_exactly_once(self):
        with mock.patch.object(brave, "ThreadPoolExecutor") as spy:
            results = _hammer(brave, "_pool", "_WORKERS")
        self.assertEqual(len(set(id(r) for r in results)), 1)
        self.assertIs(brave._WORKERS, results[0])
        self.assertEqual(spy.call_count, 1)

    def test_pool_capped_at_connection_ceiling(self):
        """The pool must never exceed the keep-alive pool: a worker beyond it
        would only block on a connection."""
        for mod in (exa, brave):
            _reset_engines()
            pool = mod._pool()
            self.assertEqual(pool._max_workers, mod._ENGINE_LIMITS.max_connections,
                             f"{mod.__name__} pool not capped at the connection ceiling")
            self.assertLessEqual(mod._POOL_MAX, mod._ENGINE_LIMITS.max_connections)


class Fanout(unittest.TestCase):
    def setUp(self):
        _reset_engines()  # ensure a real pool, not a leftover mock

    def _jobs(self, n, dur=0.1, raise_idx=None):
        lock = threading.Lock()
        cur, peak = [0], [0]

        def job(i):
            if i == raise_idx:
                def run():
                    raise ValueError(f"boom-{i}")
            else:
                def run():
                    with lock:
                        cur[0] += 1
                        peak[0] = max(peak[0], cur[0])
                    time.sleep(dur)
                    with lock:
                        cur[0] -= 1
                    return i
            return run

        return [(i, job(i)) for i in range(n)], lock, cur, peak

    def test_honours_concurrency_bound(self):
        jobs, _, _, peak = self._jobs(8, dur=0.1)
        t0 = time.monotonic()
        out = exa._fanout(jobs, concurrency=4)
        dt = time.monotonic() - t0
        # 8 jobs @ concurrency 4 = 2 waves; peak in-flight must equal the bound.
        self.assertEqual(peak[0], 4, f"peak in-flight {peak[0]} != bound 4")
        self.assertEqual([k for k, ok, _ in out], list(range(8)))
        self.assertTrue(all(ok for _, ok, _ in out))
        self.assertEqual([v for _, _, v in out], list(range(8)))
        # Serial would be ~0.8s; 2 waves of 0.1s ~0.2s. Prove it's parallel.
        self.assertLess(dt, 0.8)

    def test_concurrency_capped_at_pool(self):
        """A requested bound above the pool is clamped; nothing explodes and
        results are still correct + in order."""
        jobs, _, _, peak = self._jobs(20, dur=0.02)
        out = exa._fanout(jobs, concurrency=999)
        self.assertEqual([k for k, _, _ in out], list(range(20)))
        self.assertTrue(all(ok for _, ok, _ in out))
        self.assertLessEqual(peak[0], exa._POOL_MAX,
                             "in-flight exceeded the connection ceiling")

    def test_exception_isolated_and_order_preserved(self):
        jobs, _, _, _ = self._jobs(6, dur=0.01, raise_idx=2)
        out = exa._fanout(jobs, concurrency=3)
        self.assertEqual([k for k, _, _ in out], list(range(6)),
                         "input order must be preserved")
        statuses = {k: ok for k, ok, _ in out}
        self.assertEqual(statuses, {0: True, 1: True, 2: False, 3: True,
                                    4: True, 5: True})
        _, _, exc = out[2]
        self.assertIsInstance(exc, ValueError)
        self.assertEqual(str(exc), "boom-2")

    def test_empty_jobs(self):
        self.assertEqual(exa._fanout([], concurrency=4), [])

    def test_brave_fanout_matches(self):
        jobs, _, _, peak = self._jobs(8, dur=0.1)
        out = brave._fanout(jobs, concurrency=4)
        self.assertEqual(peak[0], 4)
        self.assertEqual([v for _, _, v in out], list(range(8)))

    def test_nested_fanout_does_not_starve_shared_pool(self):
        """A fan-out running on a pool worker (nested fan-out, e.g.
        ``batch(mode='all')`` -> ``search`` -> a per-endpoint fan-out) must not
        submit-and-block on the shared pool that its own outer worker is parked
        on: with the shared pool saturated by its siblings, that deadlocks
        forever.

        Without the escape-pool fix this hangs, so the scenario runs in a
        subprocess under a hard timeout — a reintroduced deadlock fails the test
        via the timeout instead of hanging the whole suite.
        """
        import subprocess
        import sys
        nested = (
            "import os, sys, threading, time\n"
            "sys.path[:0] = [sys.argv[1]]\n"
            "import __MOD__\n"
            "POOL = __MOD__._POOL_MAX\n"
            "ran = []; lk = threading.Lock()\n"
            "def inner():\n"
            "    with lk: ran.append(1)\n"
            "    time.sleep(0.05)\n"
            "def one():\n"
            "    __MOD__._fanout([(i, inner) for i in range(3)], concurrency=3)\n"
            "out = __MOD__._fanout([(i, one) for i in range(POOL)], concurrency=POOL)\n"
            "ok = sum(1 for _, o, _ in out if o)\n"
            "print(f'ok={ok} inner={len(ran)} expected={POOL * 3}')\n"
            "os._exit(0 if (ok == POOL and len(ran) == POOL * 3) else 1)\n"
        )
        pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for mod in ("exa", "brave"):
            code = nested.replace("__MOD__", mod)
            p = subprocess.run(
                [sys.executable, "-c", code, os.path.join(pkg_root, mod, "src")],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(
                p.returncode, 0,
                f"{mod}: nested fan-out starved the shared pool "
                f"(rc={p.returncode}, stdout={p.stdout!r}, "
                f"timed_out={p.returncode is None})")

    def test_top_level_fanout_uses_shared_pool(self):
        """A top-level (non-nested) fan-out must run on the shared pool, not the
        escape pool — otherwise the pool-capping invariant would be bypassed."""
        names = []
        lock = threading.Lock()

        def rec():
            with lock:
                names.append(threading.current_thread().name)

        exa._WORKERS = None
        exa._fanout([(i, rec) for i in range(4)], concurrency=2)
        self.assertTrue(all(n.startswith("exa_") for n in names),
                        f"top-level fan-out ran on a non-shared pool: {names}")


def _sr(query, url):
    return exa.SearchResults(
        query, [exa.Result({"url": url}, 0)], request_id=f"rid-{query}",
    )


class DeepResearchFanout(unittest.TestCase):
    def setUp(self):
        _reset_engines()

    def test_fans_out_in_parallel_and_merges(self):
        """Per-query searches run in parallel (not serial) and results merge,
        with request_ids joined in input order."""
        qs = ["q1", "q2", "q3", "q4", "q5"]
        lock = threading.Lock()
        active, peak = [0], [0]

        def fake_search(q, *a, **kw):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.1)
            with lock:
                active[0] -= 1
            return _sr(q, f"https://x.example/{q}")

        with mock.patch.object(exa, "search", fake_search):
            t0 = time.monotonic()
            out = exa.deep_research(qs, concurrency=3)
            dt = time.monotonic() - t0

        self.assertEqual(len(out.results), 5, "dedupe should keep all distinct URLs")
        self.assertEqual(peak[0], 3, f"deep_research not bounded to concurrency=3 "
                                      f"(peak {peak[0]})")
        # 5 queries @ concurrency 3 = 2 waves (~0.2s) vs ~0.5s serial.
        self.assertLess(dt, 0.5, "deep_research ran serially")
        self.assertEqual(out.request_id, "rid-q1,rid-q2,rid-q3,rid-q4,rid-q5")

    def test_first_error_raised(self):
        """A failing sub-query raises (serial first-error contract)."""
        def fake_search(q, *a, **kw):
            if q == "bad":
                raise exa.ExaError("subquery exploded")
            return _sr(q, f"https://x.example/{q}")

        with mock.patch.object(exa, "search", fake_search):
            with self.assertRaises(exa.ExaError):
                exa.deep_research(["good", "bad", "good2"], concurrency=3)

    def test_single_query_string_wrapped(self):
        with mock.patch.object(exa, "search",
                               side_effect=lambda q, **kw: _sr(q, f"https://x/{q}")) as m:
            out = exa.deep_research("solo", concurrency=2)
        self.assertEqual(len(out.results), 1)
        self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()
