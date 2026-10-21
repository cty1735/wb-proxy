"""Local-session view: the dashboard's 「本地」 option.

The local view counts the requests of every agent session on this machine and
tells apart the ones that went through this project's gateway from the ones
that never touched it.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_local


class LocalScopeTest(unittest.TestCase):
    def test_scope_normalisation(self):
        self.assertEqual(wb_local.normalize_scope(None), wb_local.SCOPE_NON_GATEWAY)
        self.assertEqual(wb_local.normalize_scope(""), wb_local.SCOPE_NON_GATEWAY)
        self.assertEqual(wb_local.normalize_scope("nongw"), wb_local.SCOPE_NON_GATEWAY)
        self.assertEqual(wb_local.normalize_scope("all"), wb_local.SCOPE_ALL)

    def test_gateway_detection_by_port(self):
        wb_local.set_gateway_port(8788)
        self.assertTrue(wb_local._is_gateway_url("http://127.0.0.1:8788/v1"))
        self.assertTrue(wb_local._is_gateway_url("http://localhost:8788/v1"))
        self.assertFalse(wb_local._is_gateway_url("http://127.0.0.1:8787/v1"))
        self.assertFalse(wb_local._is_gateway_url("https://api.example.com/v1"))
        self.assertFalse(wb_local._is_gateway_url(""))

    def test_gateway_detection_by_provider_id(self):
        wb_local.set_gateway_port(8788)
        self.assertTrue(wb_local._is_gateway_provider("wb-proxy", ""))
        self.assertFalse(wb_local._is_gateway_provider("opencode", ""))
        self.assertFalse(wb_local._is_gateway_provider("", None))

    def test_row_folding_matches_the_gateway_shape(self):
        row = {"at": 1700000000.0, "client": "opencode", "model": "m",
               "gateway": False, "ok": True, "prompt_tokens": 100,
               "completion_tokens": 10, "reasoning_tokens": 0,
               "cached_tokens": 80, "total_tokens": 110, "cost_usd": 0.0,
               "elapsed_ms": 1200, "session": "s", "project": "p"}
        stat = wb_local._new_stat()
        wb_local._fold(stat, row, 0.5)
        wb_local._finalize(stat)
        self.assertEqual(stat["requests"], 1)
        self.assertEqual(stat["errors"], 0)
        self.assertEqual(stat["total_tokens"], 110)
        self.assertEqual(stat["cost_cny"], 0.5)
        self.assertEqual(stat["cache_hit_pct"], 80.0)
        self.assertEqual(stat["elapsed_ms_avg"], 1200)

    def test_failed_request_counts_as_an_error(self):
        row = {"at": 1700000000.0, "client": "opencode", "model": "m",
               "gateway": False, "ok": False, "prompt_tokens": 0,
               "completion_tokens": 0, "reasoning_tokens": 0,
               "cached_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
               "elapsed_ms": None, "session": "s", "project": "p"}
        stat = wb_local._new_stat()
        wb_local._fold(stat, row, 0.0)
        self.assertEqual(stat["requests"], 0)
        self.assertEqual(stat["errors"], 1)

    def test_scope_filter_drops_gateway_rows(self):
        rows = [
            {"at": 10.0, "gateway": True, "ok": True, "total_tokens": 5,
             "prompt_tokens": 5, "completion_tokens": 0, "reasoning_tokens": 0,
             "cached_tokens": 0, "client": "opencode", "model": "m"},
            {"at": 20.0, "gateway": False, "ok": True, "total_tokens": 7,
             "prompt_tokens": 7, "completion_tokens": 0, "reasoning_tokens": 0,
             "cached_tokens": 0, "client": "opencode", "model": "m"},
        ]
        only_local = wb_local._scope_rows(rows, wb_local.SCOPE_NON_GATEWAY)
        self.assertEqual(len(only_local), 1)
        self.assertEqual(only_local[0]["at"], 20.0)
        both = wb_local._scope_rows(rows, wb_local.SCOPE_ALL)
        self.assertEqual(len(both), 2)

    def test_window_filter(self):
        rows = [{"at": 10.0}, {"at": 20.0}, {"at": 30.0}]
        got = wb_local._scope_rows(rows, wb_local.SCOPE_ALL, 15.0, 25.0)
        self.assertEqual([r["at"] for r in got], [20.0])

    def test_payload_shape_matches_the_analytics_payload(self):
        rows = [
            {"at": 1700000000.0, "rid": "a", "client": "opencode",
             "model": "m1", "gateway": False, "ok": True,
             "prompt_tokens": 100, "completion_tokens": 10,
             "reasoning_tokens": 0, "cached_tokens": 80, "total_tokens": 110,
             "cost_usd": 0.0, "elapsed_ms": 1000, "session": "s1",
             "project": "p1"},
            {"at": 1700000100.0, "rid": "b", "client": "claude",
             "model": "m2", "gateway": True, "ok": True,
             "prompt_tokens": 50, "completion_tokens": 5, "reasoning_tokens": 0,
             "cached_tokens": 0, "total_tokens": 55, "cost_usd": 0.0,
             "elapsed_ms": None, "session": "s2", "project": "p2"},
        ]
        original = wb_local._collect
        wb_local._collect = lambda since=None: (rows, ["opencode", "claude"], 7)
        try:
            payload = wb_local.analytics(since=1699999000.0, until=1700001000.0)
        finally:
            wb_local._collect = original
        self.assertIn("summary", payload)
        self.assertIn("window", payload["summary"])
        self.assertIn("all_time", payload["summary"])
        self.assertTrue(payload["accounts"])
        self.assertTrue(payload["models"])
        account = payload["accounts"][0]
        for key in ("uid", "nickname", "window", "all_time", "window_models"):
            self.assertIn(key, account)
        model = payload["models"][0]
        for key in ("model", "window", "all_time"):
            self.assertIn(key, model)
        # The default scope counts the request that bypassed the gateway only.
        self.assertEqual(payload["summary"]["window"]["requests"], 1)
        self.assertEqual(payload["summary"]["window"]["total_tokens"], 110)
        local = payload["local"]
        self.assertEqual(local["gateway_requests"], 1)
        self.assertEqual(local["non_gateway_requests"], 1)
        self.assertEqual(local["scan_ms"], 7)

    def test_scope_all_includes_the_gateway_rows(self):
        rows = [
            {"at": 1700000000.0, "rid": "a", "client": "opencode",
             "model": "m1", "gateway": True, "ok": True,
             "prompt_tokens": 100, "completion_tokens": 10,
             "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 110,
             "cost_usd": 0.0, "elapsed_ms": None, "session": "s1",
             "project": "p1"},
        ]
        original = wb_local._collect
        wb_local._collect = lambda since=None: (rows, ["opencode"], 1)
        try:
            payload = wb_local.analytics(since=1699999000.0, until=1700001000.0,
                                         scope=wb_local.SCOPE_ALL)
        finally:
            wb_local._collect = original
        self.assertEqual(payload["summary"]["window"]["requests"], 1)

    def test_timeseries_buckets(self):
        rows = [
            {"at": 1700000000.0, "rid": "a", "client": "opencode",
             "model": "m1", "gateway": False, "ok": True,
             "prompt_tokens": 10, "completion_tokens": 1,
             "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 11,
             "cost_usd": 0.0, "elapsed_ms": None, "session": "s", "project": "p"},
            {"at": 1700000600.0, "rid": "b", "client": "opencode",
             "model": "m1", "gateway": False, "ok": False,
             "prompt_tokens": 0, "completion_tokens": 0,
             "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
             "cost_usd": 0.0, "elapsed_ms": None, "session": "s", "project": "p"},
        ]
        original = wb_local._collect
        wb_local._collect = lambda since=None: (rows, ["opencode"], 1)
        try:
            payload = wb_local.timeseries(since=1699999000.0, until=1700001200.0,
                                         bucket_seconds=3600)
        finally:
            wb_local._collect = original
        self.assertEqual(payload["bucket_seconds"], 3600)
        self.assertTrue(payload["series"])
        self.assertEqual(payload["series"][0]["requests"], 1)
        self.assertEqual(payload["series"][0]["errors"], 1)

    def test_recent_rows_carry_the_client(self):
        rows = [
            {"at": 1700000000.0, "rid": "a", "client": "opencode",
             "model": "m1", "gateway": False, "ok": True,
             "prompt_tokens": 10, "completion_tokens": 1,
             "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 11,
             "cost_usd": 0.0, "elapsed_ms": 42, "session": "s", "project": "p"},
        ]
        original = wb_local._collect
        wb_local._collect = lambda since=None: (rows, ["opencode"], 1)
        try:
            payload = wb_local.recent(limit=10)
        finally:
            wb_local._collect = original
        self.assertEqual(payload["total"], 1)
        row = payload["rows"][0]
        self.assertEqual(row["account"], "opencode")
        self.assertEqual(row["client_label"], "OpenCode")
        self.assertEqual(row["outcome"], "completed")
        self.assertFalse(row["gateway"])

    def test_perf_payload_matches_the_gateway_shape(self):
        rows = [
            {"at": 1700000001.0, "rid": "a", "client": "opencode", "model": "m1",
             "gateway": False, "ok": True, "prompt_tokens": 100,
             "completion_tokens": 10, "reasoning_tokens": 0, "cached_tokens": 80,
             "total_tokens": 110, "cost_usd": 0.0, "elapsed_ms": 1000,
             "session": "s", "project": "p"},
            {"at": 1700000002.0, "rid": "b", "client": "opencode", "model": "m1",
             "gateway": True, "ok": False, "prompt_tokens": 100,
             "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
             "total_tokens": 100, "cost_usd": 0.0, "elapsed_ms": 3000,
             "session": "s", "project": "p"},
        ]
        original = wb_local._collect
        wb_local._collect = lambda since=None: (rows, ["opencode"], 1)
        try:
            payload = wb_local.perf()
        finally:
            wb_local._collect = original
        # The panel's percentile fold reads these keys directly (same as /usage/perf).
        for key in ("sampled", "success", "errors", "success_rate_pct",
                    "wall_ms", "tokens_per_sec", "cache_hit_pct"):
            self.assertIn(key, payload)
        self.assertEqual(payload["sampled"], 1)          # gateway row excluded
        self.assertEqual(payload["success"], 1)
        self.assertEqual(payload["success_rate_pct"], 100.0)
        self.assertIsNone(payload["ttft_ms"])
        self.assertIsNone(payload["tokens_per_sec"])
        self.assertEqual(payload["wall_ms"]["samples"], 1)
        self.assertEqual(payload["wall_ms"]["p90"], 1000)
        self.assertAlmostEqual(payload["cache_hit_pct"]["avg"], 80.0)

    def test_price_model_resolution(self):
        # A local client spells the model the way its provider does; the price
        # table is keyed by the bare model id.
        self.assertEqual(wb_local._price_model("deepseek-v4.1-flash"),
                         "deepseek-v4.1-flash")
        self.assertIsNone(wb_local._price_model("no-such-model-at-all"))


if __name__ == "__main__":
    unittest.main()
