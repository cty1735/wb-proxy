"""端到端自检：起一个真网关，打「本地」口径的四个接口。

「本地」看板选项读的是本机各客户端自己的会话记录（没走本网关的请求网关根本
看不到），所以这几个接口的载荷结构必须和网关侧一致，��板才不用另写一份渲染。
这里验证：
  1. GET /usage/analytics?provider=local  —— 载荷结构与网关侧同构
  2. GET /usage/timeseries?provider=local —— 桶结构与网关侧同构
  3. GET /usage/recent?provider=local      —— 行结构带客户端与「是否走本网关」
  4. GET /usage/perf?provider=local        —— 没有首字延迟样本就写 null
  5. provider=workbuddy 等原口径不受影响（没被本地视图劫持）
  6. local_scope=all 把经本网关的那部分也算进来
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method, url, body=None, headers=None):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}
    except Exception as exc:
        return 0, {"error": str(exc)}


accounts = tempfile.mkdtemp(prefix="localacct-")
port = free_port()
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
proc = subprocess.Popen(
    [sys.executable, "wb_proxy.py", "--host", "127.0.0.1", "--port", str(port),
     "--accounts-dir", accounts],
    cwd=ROOT, env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

base = "http://127.0.0.1:%d" % port
panel = {}
try:
    for _ in range(80):
        try:
            status, _ = http("GET", base + "/health")
            if status == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)
    else:
        print("gateway did not start")
        print(proc.stdout.read(4000).decode("utf-8", "replace"))
        raise SystemExit(1)

    status, r = http("POST", base + "/panel/login", {"password": "admin"})
    check("面板登录", status == 200 and r.get("token"), r)
    panel = {"X-Panel-Token": r.get("token", "")}

    # 1. analytics
    status, r = http("GET", base + "/usage/analytics?provider=local&range=today",
                     headers=panel)
    check("GET /usage/analytics?provider=local", status == 200, (status, r))
    check("载荷有 summary/window/all_time",
          bool(r.get("summary", {}).get("window")) and bool(r.get("summary", {}).get("all_time")),
          r.get("summary"))
    check("载荷带 accounts / models 列表",
          isinstance(r.get("accounts"), list) and isinstance(r.get("models"), list),
          type(r.get("accounts")).__name__)
    check("载荷带 local 段（口径说明用）", isinstance(r.get("local"), dict), r.get("local"))
    if r.get("accounts"):
        acc = r["accounts"][0]
        check("账号行结构与网关侧同构",
              all(k in acc for k in ("uid", "nickname", "window", "all_time", "window_models")),
              list(acc.keys()))
    if r.get("models"):
        model = r["models"][0]
        check("模型行结构与网关侧同构",
              all(k in model for k in ("model", "window", "all_time")),
              list(model.keys()))

    # 2. timeseries
    status, r = http("GET", base + "/usage/timeseries?provider=local&range=today",
                     headers=panel)
    check("GET /usage/timeseries?provider=local", status == 200, (status, r))
    check("桶结构与网关侧同构",
          bool(r.get("bucket_seconds")) and isinstance(r.get("series"), list),
          (r.get("bucket_seconds"), type(r.get("series")).__name__))
    if r.get("series"):
        bucket = r["series"][0]
        check("桶字段齐全",
              all(k in bucket for k in ("at", "requests", "errors", "total_tokens",
                                        "prompt_tokens", "cached_tokens", "credit")),
              list(bucket.keys()))

    # 3. recent
    status, r = http("GET", base + "/usage/recent?provider=local&limit=5", headers=panel)
    check("GET /usage/recent?provider=local", status == 200, (status, r))
    check("行里带客户端与网关标记",
          all(("client" in row and "gateway" in row) for row in (r.get("rows") or [])),
          (r.get("rows") or [])[:1])

    # 3b. 本地视图的客户端筛选：agent 参数解释成本机客户端（opencode / claude / codex）。
    status, r = http("GET", base + "/usage/recent?provider=local&limit=50&agent=opencode",
                     headers=panel)
    check("GET /usage/recent?provider=local&agent=opencode", status == 200, (status, r))
    check("客户端筛选只返回该客户端的行",
          all(row.get("client") == "opencode" for row in (r.get("rows") or [])),
          (r.get("rows") or [])[:3])
    status, r = http("GET", base + "/usage/analytics?provider=local&range=all&agent=opencode",
                     headers=panel)
    check("GET /usage/analytics?provider=local&agent=opencode", status == 200, (status, r))
    check("本地载荷回显筛选的客户端", (r.get("local") or {}).get("agent") == "opencode",
          (r.get("local") or {}).get("agent"))
    check("筛掉一个客户端后仍列出全部客户端（下拉能切回去）",
          len((r.get("local") or {}).get("clients") or []) >= 1,
          (r.get("local") or {}).get("clients"))
    check("筛选后的明细只剩该客户端",
          all(a.get("uid") == "opencode" for a in (r.get("accounts") or [])),
          [a.get("uid") for a in (r.get("accounts") or [])])
    status, r = http("GET", base + "/usage/timeseries?provider=local&range=all&agent=opencode",
                     headers=panel)
    check("GET /usage/timeseries?provider=local&agent=opencode", status == 200, (status, r))

    # 4. perf
    status, r = http("GET", base + "/usage/perf?provider=local", headers=panel)
    check("GET /usage/perf?provider=local", status == 200, (status, r))
    check("首字延迟没有样本就写 null", r.get("ttft_ms") is None, r.get("ttft_ms"))
    check("性能载荷与网关同构（分位块 + 成功率）",
          "sampled" in (r or {}) and "success_rate_pct" in (r or {})
          and isinstance((r or {}).get("wall_ms"), (dict, type(None))), list((r or {}).keys()))

    # 5. 其它口径没被劫持
    status, r = http("GET", base + "/usage/analytics?provider=workbuddy&range=today",
                     headers=panel)
    check("provider=workbuddy 仍走网关口径", status == 200 and "local" not in (r or {}),
          (status, list((r or {}).keys()))[:6])
    status, r = http("GET", base + "/usage/analytics?range=today", headers=panel)
    check("不带 provider 仍是全部提供商", status == 200 and "local" not in (r or {}),
          (status, list((r or {}).keys()))[:6])

    # 6. local_scope=all 把经本网关的那部分也算进来
    status, narrow = http("GET", base + "/usage/analytics?provider=local&range=all",
                          headers=panel)
    status2, wide = http("GET",
                          base + "/usage/analytics?provider=local&range=all&local_scope=all",
                          headers=panel)
    check("两种口径都能取到", status == 200 and status2 == 200, (status, status2))
    n_narrow = ((narrow.get("summary") or {}).get("all_time") or {}).get("requests") or 0
    n_wide = ((wide.get("summary") or {}).get("all_time") or {}).get("requests") or 0
    check("含网关口径不少于仅非网关口径", n_wide >= n_narrow, (n_narrow, n_wide))
finally:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

print()
if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    raise SystemExit(1)
print("all checks passed")
