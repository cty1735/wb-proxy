"""客户端归属与看板筛选自检。

背景：数据看板需要按「哪个 AI 客户端在用」筛选。一键配置现在会写
X-WB-Agent 头（DSH / OpenCode / Hermes），老配置没有这个头时网关退回
User-Agent 识别。本测试覆盖：

1. 头 / UA 的识别规则，认不出来就返回 None（不猜）；
2. record_usage / record_error 把识别结果写进 usage 行；
3. analytics / timeseries / recent / perf 四个看板接口都按 agent 过滤，
   且「全部」口径不受影响。

用临时 usage.jsonl，不碰真实数据。
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def close(a, b, eps=1e-6):
    return abs(float(a) - float(b)) <= eps


def usage(prompt, completion):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


# ---------------------------------------------------------------------------
# 1. 识别规则
# ---------------------------------------------------------------------------
check("explicit header wins",
      P.detect_agent({"X-WB-Agent": "opencode",
                      "User-Agent": "deepseek-harness/0.1"}) == "opencode")
check("header is case-insensitive",
      P.detect_agent({"x-wb-agent": "Hermes"}) == "hermes")
check("unknown header falls back to UA",
      P.detect_agent({"X-WB-Agent": "not-a-client",
                      "User-Agent": "opencode/latest/2.1.1/cli"}) == "opencode")
check("DSH UA", P.detect_agent({"User-Agent": "deepseek-harness/1.2.3"}) == "dsh")
check("OpenCode UA",
      P.detect_agent({"User-Agent": "opencode/latest/2.1.1/cli"}) == "opencode")
check("Hermes UA", P.detect_agent({"User-Agent": "HermesAgent/0.9.0"}) == "hermes")
check("Claude Code UA",
      P.detect_agent({"User-Agent": "claude-cli/2.0.1 (external, cli)"}) == "claude-code")
check("browser UA is not an agent",
      P.detect_agent({"User-Agent": "Mozilla/5.0 (Windows NT 10.0)"}) is None)
check("no headers is no agent", P.detect_agent({}) is None)

check("row matches all", P.row_matches_agent({}, None))
check("row matches literal all", P.row_matches_agent({"agent": "dsh"}, "all"))
check("row matches same agent", P.row_matches_agent({"agent": "dsh"}, "dsh"))
check("row rejects other agent", not P.row_matches_agent({"agent": "dsh"}, "opencode"))
check("old row has no agent",
      not P.row_matches_agent({"at": 1}, "dsh"))
check("unknown filter value matches nothing",
      not P.row_matches_agent({"agent": "dsh"}, "bogus"))

# ---------------------------------------------------------------------------
# 2. record_usage / record_error 写入归属
# ---------------------------------------------------------------------------
tmp = tempfile.mkdtemp(prefix="agentfilter-")
old_dir, old_log = P.USAGE_DIR, P.USAGE_LOG
old_index_ready = P._recent_index_ready
P.USAGE_DIR = tmp
P.USAGE_LOG = os.path.join(tmp, "usage.jsonl")
try:
    P.set_request_agent({"User-Agent": "deepseek-harness/0.1"})
    P.record_usage("m-dsh", usage(100, 50), elapsed_ms=1000, ttft_ms=200,
                   gen_ms=800, outcome="completed")
    P.set_request_agent({"X-WB-Agent": "opencode"})
    P.record_usage("m-oc", usage(200, 100), elapsed_ms=2000, ttft_ms=300,
                   gen_ms=1700, outcome="completed")
    P.set_request_agent({"User-Agent": "Mozilla/5.0"})
    P.record_usage("m-old", usage(400, 200), elapsed_ms=3000, ttft_ms=400,
                   gen_ms=2600, outcome="completed")
    with open(P.USAGE_LOG, encoding="utf-8") as fh:
        written = [json.loads(line) for line in fh if line.strip()]
    check("record_usage tags DSH from UA",
          written[0].get("agent") == "dsh", written[0].get("agent"))
    check("record_usage tags OpenCode from header",
          written[1].get("agent") == "opencode", written[1].get("agent"))
    check("unidentified row keeps old shape",
          "agent" not in written[2], written[2].get("agent"))

    # record_error 单独验一条，然后把这行删掉，后面的接口口径只数 3 条成功行。
    P.set_request_agent({"x-wb-agent": "hermes"})
    P.record_error("m-hermes", 500, "boom", elapsed_ms=500, outcome="failed")
    P.set_request_agent({})
    with open(P.USAGE_LOG, encoding="utf-8") as fh:
        err_row = json.loads(fh.readlines()[-1])
    check("record_error tags Hermes from header",
          err_row.get("agent") == "hermes", err_row.get("agent"))
    with open(P.USAGE_LOG, "w", encoding="utf-8") as fh:
        for row in written:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -----------------------------------------------------------------------
    # 3. 四个接口的过滤
    # -----------------------------------------------------------------------
    now = int(time.time()) + 5
    P.POOL = None
    P.CURRENT_REALM = "intl"
    P._series_cache.clear()
    P._analytics_cache.clear()
    P._perf_cache.clear()
    P._agent_count_cache.clear()
    P._count_cache.clear()
    P._count_state.update({"offset": 0, "key": None, "tail": b"", "n": 0,
                           "n_realm": {}, "n_provider": {}, "n_pair": {}})
    old_index_ready = P._recent_index_ready
    P._recent_index_ready = lambda **kwargs: None

    for agent, requests, tokens in (("dsh", 1, 150), ("opencode", 1, 300),
                                    (None, 3, 1050)):
        a = P.compute_usage_analytics(since=0, until=now, agent=agent)
        got = a["summary"]["window"]
        check("analytics %s requests" % agent,
              got["requests"] == requests, got["requests"])
        check("analytics %s tokens" % agent,
              got["total_tokens"] == tokens, got["total_tokens"])

        t = P.usage_timeseries(since=0, until=now, bucket_seconds=3600,
                               agent=agent)
        check("timeseries %s requests" % agent,
              sum(b["requests"] for b in t["series"]) == requests)
        check("timeseries %s tokens" % agent,
              sum(b["total_tokens"] for b in t["series"]) == tokens)

        r = P.recent_usage(limit=10, realm="intl", agent=agent)
        check("recent %s total" % agent, r["total"] == requests, r["total"])
        check("recent %s rows all match" % agent,
              all(P.row_matches_agent(x, agent) for x in r["rows"]))

        p = P.perf_stats(sample=100, realm="intl", since=0, until=now,
                         agent=agent)
        check("perf %s sampled" % agent, p["sampled"] == requests, p["sampled"])

    # 分页：只筛 DSH 时也应只有 1 条，而不是把全部 4 条的路由参数带错。
    r = P.recent_usage(limit=1, realm="intl", page=1, agent="dsh")
    check("filtered recent page has one row", len(r["rows"]) == 1)
    check("filtered recent total is one", r["total"] == 1)
    check("filtered recent row is DSH",
          r["rows"][0].get("agent") == "dsh")
finally:
    P.set_request_agent({})
    P._recent_index_ready = old_index_ready
    P.USAGE_DIR, P.USAGE_LOG = old_dir, old_log
    shutil.rmtree(tmp, ignore_errors=True)

print("ALL PASS" if not FAIL else "FAILED: %s" % ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
