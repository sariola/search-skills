"""Agent budget, typed timeouts, cancellation, and Exa 3.0 convenience helpers."""
import threading
import unittest
from unittest.mock import MagicMock, patch

import httpx

import exa


class AgentBudget(unittest.TestCase):
    def _create(self, **kwargs):
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"id": "r", "status": "completed", "output": {}}) as req:
            exa.agent("task", **kwargs)
        return req.call_args.kwargs["json_body"]

    def test_ultra_sends_duration_and_cost(self):
        body = self._create(effort="ultra", max_cost_dollars=20, max_duration_seconds=300)
        self.assertEqual(body["budget"], {"maxCostDollars": 20.0, "maxDurationSeconds": 300})
        self.assertEqual(body["effort"], "ultra")

    def test_auto_sends_cost_and_rejects_duration(self):
        body = self._create(max_cost_dollars=5)
        self.assertEqual(body["budget"], {"maxCostDollars": 5.0})
        with self.assertRaises(exa.ExaBadRequestError):
            self._create(max_duration_seconds=300)

    def test_duration_bounds_and_effort(self):
        with self.assertRaises(exa.ExaBadRequestError):
            self._create(effort="ultra", max_duration_seconds=299)
        with self.assertRaises(exa.ExaBadRequestError):
            self._create(effort="ultra", max_duration_seconds=10801)
        with self.assertRaises(exa.ExaBadRequestError):
            self._create(effort="minimal", max_duration_seconds=600)
        with self.assertRaises(exa.ExaBadRequestError):
            self._create(effort="ultra", max_cost_dollars=0.5)
        with self.assertRaises(exa.ExaBadRequestError):
            self._create(effort="max", max_cost_dollars=5)


class TypedTimeouts(unittest.TestCase):
    def test_phases(self):
        cases = [
            (httpx.ConnectTimeout("c"), "connect"),
            (httpx.ReadTimeout("r"), "read"),
            (httpx.WriteTimeout("w"), "write"),
            (httpx.PoolTimeout("p"), "pool"),
            (httpx.TimeoutException("t"), "total"),
        ]
        for exc, phase in cases:
            client = MagicMock()
            client.request.side_effect = exc
            with patch.object(exa, "_http", return_value=client):
                with self.assertRaises(exa.ExaTimeoutError) as caught:
                    exa._request("GET", "/x", "k", timeout=1, _retries=0)
            self.assertEqual(caught.exception.phase, phase)
            self.assertIsInstance(caught.exception, exa.ExaError)

    def test_request_timeout_object_reaches_httpx(self):
        client = MagicMock()
        client.request.return_value = MagicMock(status_code=200, json=lambda: {"ok": True})
        spec = exa.RequestTimeout(10, connect=1.5)
        with patch.object(exa, "_http", return_value=client):
            exa._request("GET", "/x", "k", timeout=spec, _retries=0)
        sent = client.request.call_args.kwargs["timeout"]
        self.assertIsInstance(sent, httpx.Timeout)
        self.assertEqual(sent.connect, 1.5)
        self.assertEqual(sent.read, 10.0)

    def test_negative_timeout_rejected(self):
        with self.assertRaises(exa.ExaBadRequestError):
            exa._coerce_timeout(-1)


class Cancellation(unittest.TestCase):
    def test_cancel_before_request(self):
        token = exa.CancelToken()
        token.cancel("stop")
        client = MagicMock()
        with patch.object(exa, "_one_shot_client", return_value=client):
            with self.assertRaises(exa.ExaCancelledError) as caught:
                exa._request("GET", "/x", "k", cancel=token, _retries=0)
        self.assertIn("stop", str(caught.exception))
        client.request.assert_not_called()

    def test_scope_cancel_before_request(self):
        token = exa.CancelToken()
        token.cancel()
        with exa.RequestScope(cancel=token):
            with self.assertRaises(exa.ExaCancelledError):
                exa._request("GET", "/x", "k", _retries=0)

    def test_inflight_close_cancels(self):
        class Blocking:
            def __init__(self, *args, **kwargs):
                self._closed = threading.Event()

            def request(self, *args, **kwargs):
                if not self._closed.wait(2):
                    raise AssertionError("client was not closed")
                raise httpx.ReadError("closed")

            def close(self):
                self._closed.set()

        token = exa.CancelToken()

        def later():
            token.cancel("abort")

        threading.Timer(0.05, later).start()
        with patch.object(exa, "_one_shot_client", return_value=Blocking()):
            with self.assertRaises(exa.ExaCancelledError) as caught:
                exa._request("GET", "/x", "k", cancel=token, timeout=5, _retries=0)
        self.assertIn("abort", str(caught.exception))


class ConvenienceHelpers(unittest.TestCase):
    def test_search_lite_forces_type_and_rejects_other_types(self):
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"results": []}) as req:
            exa.search_lite("q", num_results=3, objective="hours")
        body = req.call_args.kwargs["json_body"]
        self.assertEqual(body["type"], "lite")
        self.assertEqual(body["objective"], "hours")
        with self.assertRaises(exa.ExaBadRequestError):
            exa.search_lite("q", search_type="auto")

    def test_fetch_snapshot_requires_date(self):
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"results": []}) as req:
            exa.fetch_snapshot(["https://example.com"], snapshot_as_of="2026-01-15")
        body = req.call_args.kwargs["json_body"]
        self.assertEqual(body["urls"], ["https://example.com"])
        self.assertEqual(body["snapshotAsOf"], "2026-01-15")
        with self.assertRaises(exa.ExaBadRequestError):
            exa.fetch_snapshot(["https://example.com"], snapshot_as_of="")

    def test_search_near_sends_location(self):
        with patch.object(exa, "_get_api_key", return_value="k"), \
             patch.object(exa, "_request", return_value={"results": []}) as req:
            exa.search_near("coffee", {"latitude": 37.7, "longitude": -122.4})
        self.assertEqual(
            req.call_args.kwargs["json_body"]["userLocation"],
            {"latitude": 37.7, "longitude": -122.4},
        )
        with self.assertRaises(exa.ExaBadRequestError):
            exa.search_near("coffee", "")


if __name__ == "__main__":
    unittest.main()
