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
import wb_proxy


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

    def test_client_filter_keeps_the_option_list_complete(self):
        rows = [
            {"at": 1700000001.0, "rid": "a", "client": "opencode", "model": "m1",
             "gateway": False, "ok": True, "prompt_tokens": 10,
             "completion_tokens": 1, "reasoning_tokens": 0, "cached_tokens": 0,
             "total_tokens": 11, "cost_usd": 0.0, "elapsed_ms": 100,
             "session": "s", "project": "p"},
            {"at": 1700000002.0, "rid": "b", "client": "claude", "model": "m2",
             "gateway": False, "ok": True, "prompt_tokens": 20,
             "completion_tokens": 2, "reasoning_tokens": 0, "cached_tokens": 0,
             "total_tokens": 22, "cost_usd": 0.0, "elapsed_ms": 200,
             "session": "s", "project": "p"},
            {"at": 1700000003.0, "rid": "c", "client": "opencode", "model": "m1",
             "gateway": True, "ok": True, "prompt_tokens": 30,
             "completion_tokens": 3, "reasoning_tokens": 0, "cached_tokens": 0,
             "total_tokens": 33, "cost_usd": 0.0, "elapsed_ms": 300,
             "session": "s", "project": "p"},
        ]
        original = wb_local._collect
        wb_local._collect = lambda since=None: (rows, ["opencode", "claude"], 1)
        try:
            payload = wb_local.analytics(client="claude")
        finally:
            wb_local._collect = original
        # KPI 只算被选中的客户端（且仍然排除经网关的那条）。
        self.assertEqual(payload["local"]["agent"], "claude")
        self.assertEqual(payload["summary"]["window"]["requests"], 1)
        self.assertEqual([a["uid"] for a in payload["accounts"]], ["claude"])
        # 下拉的选项来自未过滤的行：筛掉一个客户端也还能切回另一个。
        ids = sorted(c["id"] for c in payload["local"]["clients"])
        self.assertEqual(ids, ["claude", "opencode"])
        self.assertEqual(wb_local.normalize_client("claude-code"), "claude")
        self.assertIsNone(wb_local.normalize_client("all"))

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

    def test_perf_rows_are_gateway_shaped(self):
        rows = [
            {"at": 1700000003.0, "rid": "c", "client": "opencode", "model": "m1",
             "gateway": False, "ok": True, "prompt_tokens": 200,
             "completion_tokens": 20, "reasoning_tokens": 0, "cached_tokens": 50,
             "total_tokens": 220, "cost_usd": 0.0, "elapsed_ms": 1500,
             "session": "s", "project": "p"},
            {"at": 1700000001.0, "rid": "a", "client": "opencode", "model": "m1",
             "gateway": False, "ok": True, "prompt_tokens": 100,
             "completion_tokens": 10, "reasoning_tokens": 0, "cached_tokens": 80,
             "total_tokens": 110, "cost_usd": 0.0, "elapsed_ms": 1000,
             "session": "s", "project": "p"},
            {"at": 1700000002.0, "rid": "b", "client": "opencode", "model": "m2",
             "gateway": False, "ok": False, "prompt_tokens": 0,
             "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
             "total_tokens": 0, "cost_usd": 0.0, "elapsed_ms": 3000,
             "session": "s", "project": "p"},
            {"at": 1700000004.0, "rid": "d", "client": "opencode", "model": "m1",
             "gateway": True, "ok": True, "prompt_tokens": 100,
             "completion_tokens": 10, "reasoning_tokens": 0, "cached_tokens": 80,
             "total_tokens": 110, "cost_usd": 0.0, "elapsed_ms": 900,
             "session": "s", "project": "p"},
        ]
        original = wb_local._collect
        wb_local._collect = lambda since=None: (rows, ["opencode"], 1)
        try:
            got = wb_local.perf_rows(since=1700000000.0, until=1700000005.0)
        finally:
            wb_local._collect = original
        # 网关行被排除，且按时间升序（合并时直接接在日志尾巴后面）。
        self.assertEqual([r["at"] for r in got], [1700000001.0, 1700000002.0, 1700000003.0])
        for row in got:
            self.assertEqual(row["realm"], "local")
            self.assertEqual(row["account"], "opencode")
            self.assertIn("outcome", row)
        # 缓存命中只对成功请求成立：失败行没有可比的 prompt。
        self.assertAlmostEqual(got[0]["cache_hit_pct"], 80.0)
        self.assertIsNone(got[1]["cache_hit_pct"])
        self.assertAlmostEqual(got[2]["cache_hit_pct"], 25.0)
        self.assertEqual(got[1]["outcome"], "failed")

    def test_perf_rows_limit_keeps_the_newest(self):
        rows = [
            {"at": float(i), "rid": str(i), "client": "opencode", "model": "m",
             "gateway": False, "ok": True, "prompt_tokens": 10,
             "completion_tokens": 1, "reasoning_tokens": 0, "cached_tokens": 0,
             "total_tokens": 11, "cost_usd": 0.0, "elapsed_ms": 100,
             "session": "s", "project": "p"}
            for i in range(1, 6)
        ]
        original = wb_local._collect
        wb_local._collect = lambda since=None: (rows, ["opencode"], 1)
        try:
            got = wb_local.perf_rows(limit=2)
        finally:
            wb_local._collect = original
        self.assertEqual([r["at"] for r in got], [4.0, 5.0])

    def test_account_all_time_is_not_zero(self):
        rows = [
            {"at": 1690000000.0, "rid": "old", "client": "opencode",
             "model": "m1", "gateway": False, "ok": True,
             "prompt_tokens": 10, "completion_tokens": 1,
             "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 11,
             "cost_usd": 0.0, "elapsed_ms": 100, "session": "s", "project": "p"},
            {"at": 1700000000.0, "rid": "new", "client": "opencode",
             "model": "m1", "gateway": False, "ok": True,
             "prompt_tokens": 20, "completion_tokens": 2,
             "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 22,
             "cost_usd": 0.0, "elapsed_ms": 200, "session": "s", "project": "p"},
        ]
        original = wb_local._collect
        wb_local._collect = lambda since=None: (rows, ["opencode"], 1)
        try:
            payload = wb_local.analytics(since=1699999000.0, until=1700001000.0)
        finally:
            wb_local._collect = original
        account = payload["accounts"][0]
        self.assertEqual(account["window"]["requests"], 1)
        # 「全部时间」要把窗口外的那条也接回来，而不是恒为 0。
        self.assertEqual(account["all_time"]["requests"], 2)
        self.assertEqual(account["all_time"]["total_tokens"], 33)

    def test_price_model_resolution(self):
        # A local client spells the model the way its provider does; the price
        # table is keyed by the bare model id.
        self.assertEqual(wb_local._price_model("deepseek-v4.1-flash"),
                         "deepseek-v4.1-flash")
        self.assertIsNone(wb_local._price_model("no-such-model-at-all"))


class LocalMergeTest(unittest.TestCase):
    """「全部提供商」= 网关载荷 + 本机非网关载荷。"""

    def _stat(self, requests, tokens, prompt=None):
        prompt = tokens if prompt is None else prompt
        stat = wb_proxy._new_analytics_stat()
        stat["requests"] = requests
        stat["prompt_tokens"] = prompt
        stat["cached_tokens"] = prompt // 2
        stat["completion_tokens"] = tokens - prompt
        stat["total_tokens"] = tokens
        stat["ttft_sum"] = 100.0 * requests
        stat["ttft_n"] = requests
        stat["speed_sum"] = 10.0 * requests
        stat["speed_n"] = requests
        stat["elapsed_sum"] = 200.0 * requests
        stat["elapsed_n"] = requests
        wb_proxy._finalize_analytics_stat(stat)
        return stat

    def test_merge_condition(self):
        self.assertTrue(wb_proxy._merge_local_requested("all", None))
        self.assertTrue(wb_proxy._merge_local_requested(None, None))
        # 指定了提供商或出口时不合并：本机会话没有这两个归属。
        self.assertFalse(wb_proxy._merge_local_requested("all", "workbuddy"))
        self.assertFalse(wb_proxy._merge_local_requested("intl", None))

    def test_analytics_merge_sums_and_does_not_mutate(self):
        gateway = {
            "summary": {"window": self._stat(2, 200), "all_time": self._stat(3, 300)},
            "accounts": [{"uid": "a", "nickname": "A", "realm": "intl",
                          "domain": "", "credits": {},
                          "window": self._stat(2, 200), "all_time": self._stat(3, 300),
                          "window_models": {"m": {"requests": 2, "tokens": 200,
                                                  "reasoning": 0, "cost_cny": 0.0}},
                          "all_models": {}}],
            "models": [{"model": "m", "window": self._stat(2, 200),
                        "all_time": self._stat(3, 300)}],
            "keys": [],
        }
        before = json.dumps(gateway, sort_keys=True)
        local = {
            "summary": {"window": self._stat(5, 500), "all_time": self._stat(7, 700)},
            "accounts": [{"uid": "opencode", "nickname": "OpenCode",
                          "window": self._stat(5, 500), "all_time": self._stat(7, 700),
                          "window_models": {"m": {"requests": 5, "tokens": 500,
                                                  "reasoning": 0, "cost_cny": 0.0}},
                          "all_models": {}}],
            "models": [{"model": "m", "window": self._stat(5, 500),
                        "all_time": self._stat(7, 700)},
                       {"model": "local-only", "window": self._stat(1, 100),
                        "all_time": self._stat(1, 100)}],
            "local": {"clients": [{"id": "opencode", "label": "OpenCode"}],
                      "scan_ms": 4},
        }
        merged = wb_proxy.merge_local_analytics(gateway, local)
        self.assertEqual(json.dumps(gateway, sort_keys=True), before)  # 缓存没被改
        self.assertEqual(merged["summary"]["window"]["requests"], 7)
        self.assertEqual(merged["summary"]["window"]["total_tokens"], 700)
        self.assertEqual(merged["summary"]["window"]["cache_hit_pct"], 50.0)
        # 同 id 的模型相加，本地独有的追加
        by_id = {m["model"]: m for m in merged["models"]}
        self.assertEqual(by_id["m"]["window"]["requests"], 7)
        self.assertEqual(by_id["local-only"]["window"]["requests"], 1)
        # 本地客户端作为一行，uid 带前缀；全历史也接上了
        local_row = [a for a in merged["accounts"] if a["uid"] == "local:opencode"][0]
        self.assertEqual(local_row["window"]["requests"], 5)
        self.assertEqual(local_row["all_time"]["requests"], 7)
        self.assertIn("本地", local_row["nickname"])
        # Key 表多一行本机会话，且与 KPI 同源
        key_row = [k for k in merged["keys"] if k["source"] == "local"][0]
        self.assertEqual(key_row["window"]["requests"], 5)
        self.assertEqual(merged["local_merged"]["requests"], 5)
        self.assertEqual(merged["local_merged"]["tokens"], 500)

    def test_timeseries_merge_aligns_buckets(self):
        gateway = {"bucket_seconds": 3600,
                   "series": [{"at": 100, "requests": 1, "errors": 0,
                               "total_tokens": 10, "prompt_tokens": 10,
                               "cached_tokens": 0, "credit": 0.5}]}
        local = {"bucket_seconds": 3600,
                 "series": [{"at": 100, "requests": 2, "errors": 0,
                             "total_tokens": 20, "prompt_tokens": 20,
                             "cached_tokens": 0, "credit": 0.0},
                            {"at": 200, "requests": 3, "errors": 1,
                             "total_tokens": 30, "prompt_tokens": 30,
                             "cached_tokens": 0, "credit": 0.0}]}
        merged = wb_proxy.merge_local_timeseries(gateway, local)
        self.assertEqual([b["at"] for b in merged["series"]], [100, 200])
        self.assertEqual(merged["series"][0]["requests"], 3)
        self.assertEqual(merged["series"][0]["total_tokens"], 30)
        self.assertEqual(merged["series"][1]["requests"], 3)
        self.assertEqual(gateway["series"][0]["requests"], 1)  # 原载荷没变

    def test_recent_merge_sorts_and_paginates(self):
        gateway = {"total": 2, "rows": [{"at": 30}, {"at": 10}], "usd_cny": 7.1}
        local = {"total": 2, "rows": [{"at": 25}, {"at": 20}]}
        page1 = wb_proxy.merge_local_recent(gateway, local, 2, 1)
        self.assertEqual(page1["total"], 4)
        self.assertEqual(page1["total_pages"], 2)
        self.assertEqual([r["at"] for r in page1["rows"]], [30, 25])
        page2 = wb_proxy.merge_local_recent(gateway, local, 2, 2)
        self.assertEqual([r["at"] for r in page2["rows"]], [20, 10])


if __name__ == "__main__":
    unittest.main()
