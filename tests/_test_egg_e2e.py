"""端到端自检：起一个真网关，打新的 /egg/pools 接口。

用一个假的 OpenAI 兼容上游（本地 http.server）当供应商，验证：
  1. GET  /egg/pools          —— 池子清单 + 预设
  2. POST /egg/pools          —— 拉取模型清单（不落盘）
  3. POST /egg/pools/save     —— 保存池子
  4. GET  /v1/models?provider=egg —— 清单里出现池子的模型
  5. POST /v1/chat/completions  —— 请求真的发到那个池子
  6. POST /egg/pools/toggle   —— 停用后不再接流量
  7. POST /egg/pools/models   —— 只改挑选的模型
  8. POST /egg/pools/delete   —— 删除
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# 模块在仓库根目录：把根目录放进 sys.path 才能 import 到 wb_egg_pools。
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


# --- 假的供应商上游 -------------------------------------------------------
UPSTREAM_REQUESTS = []


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"data": [
                {"id": "vendor/model-a", "name": "Model A",
                 "context_length": 128000},
                {"id": "vendor/model-b", "name": "Model B"},
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
        UPSTREAM_REQUESTS.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": payload,
        })
        chunks = [
            {"id": "c1", "object": "chat.completion.chunk", "model": payload.get("model"),
             "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
            {"id": "c1", "object": "chat.completion.chunk", "model": payload.get("model"),
             "choices": [{"index": 0, "delta": {"content": "pong"}, "finish_reason": None}]},
            {"id": "c1", "object": "chat.completion.chunk", "model": payload.get("model"),
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
        ]
        out = b""
        for c in chunks:
            out += ("data: %s\n\n" % json.dumps(c)).encode()
        out += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


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
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return exc.code, {"raw": raw.decode("utf-8", "replace")}


upstream = HTTPServer(("127.0.0.1", 0), FakeUpstream)
upstream_port = upstream.server_address[1]
threading.Thread(target=upstream.serve_forever, daemon=True).start()

accounts = tempfile.mkdtemp(prefix="eggacct-")
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
    # 等网关起来
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

    # 登录面板
    status, r = http("POST", base + "/panel/login", {"password": "admin"})
    check("面板登录", status == 200 and r.get("token"), r)
    panel = {"X-Panel-Token": r.get("token", "")}

    # 1. GET /egg/pools
    status, r = http("GET", base + "/egg/pools", headers=panel)
    check("GET /egg/pools", status == 200 and "pools" in r and "presets" in r, r)
    check("预设非空", len(r.get("presets") or []) >= 10, len(r.get("presets") or []))
    check("协议列表", len(r.get("protocols") or []) == 2, r.get("protocols"))

    # 未带面板会话应当被拒
    status, r = http("GET", base + "/egg/pools")
    check("未带面板会话被拒", status == 401, (status, r))

    base_url = "http://127.0.0.1:%d/v1" % upstream_port

    # 2. POST /egg/pools —— 拉模型清单
    status, r = http("POST", base + "/egg/pools",
                     {"base_url": base_url, "api_key": "sk-test", "protocol": "openai"},
                     panel)
    check("拉取模型清单", status == 200 and r.get("ok") is True, r)
    check("拉到 2 个模型", len(r.get("models") or []) == 2, r.get("models"))
    check("模型 id 对", (r.get("models") or [{}])[0].get("id") == "vendor/model-a",
          r.get("models"))
    check("拉取不落盘", not http("GET", base + "/egg/pools", headers=panel)[1]
          .get("pools"))

    # 3. 保存
    status, r = http("POST", base + "/egg/pools/save", {
        "name": "本地假上游", "protocol": "openai", "base_url": base_url,
        "api_key": "sk-test", "models": ["vendor/model-a", "vendor/model-b"],
        "catalog": r.get("models") or [],
    }, panel)
    check("保存池子", status == 200 and r.get("ok") is True, r)
    pools = r.get("pools") or []
    check("池子已落盘", len(pools) == 1, pools)
    pool = pools[0]
    check("Key 已掩码", pool.get("api_key_masked") == "sk***",
          pool.get("api_key_masked"))
    check("协议标签", pool.get("protocol_label") == "OpenAI 兼容",
          pool.get("protocol_label"))
    check("模型数", pool.get("model_count") == 2, pool)

    # 4. /v1/models 里出现池子的模型
    status, r = http("GET", base + "/v1/models?provider=egg")
    check("/v1/models 可用", status == 200, (status, r))
    ids = [m.get("id") for m in (r.get("data") or [])]
    check("清单含池子模型", "vendor/model-a" in ids, ids[:10])
    entry = [m for m in (r.get("data") or []) if m.get("id") == "vendor/model-a"]
    check("池子模型带池子标签",
          entry and entry[0].get("egg_source") == pool.get("id"), entry)
    check("池子模型标签是池子名",
          entry and entry[0].get("egg_source_label") == "本地假上游", entry)
    check("池子模型标出是自建池",
          entry and entry[0].get("egg_source_kind") == "pool", entry)
    check("egg 状态带 pools",
          isinstance((r.get("egg") or {}).get("pools"), list), r.get("egg"))

    # 5. 聊天请求真的发到池子
    UPSTREAM_REQUESTS.clear()
    status, r = http("POST", base + "/v1/chat/completions?provider=egg", {
        "model": "vendor/model-a",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
    })
    check("聊天成功", status == 200, (status, r))
    check("回答内容", "pong" in json.dumps(r), r)
    check("上游收到请求", len(UPSTREAM_REQUESTS) == 1, UPSTREAM_REQUESTS)
    if UPSTREAM_REQUESTS:
        got = UPSTREAM_REQUESTS[0]
        check("上游路径", got["path"] == "/v1/chat/completions", got["path"])
        check("上游带 Key", got["auth"] == "Bearer sk-test", got.get("auth"))
        check("上游模型名", got["body"].get("model") == "vendor/model-a", got["body"])

    # 6. 停用后不再接流量
    status, r = http("POST", base + "/egg/pools/toggle",
                     {"id": pool["id"], "enabled": False}, panel)
    check("停用池子", status == 200 and r.get("ok") is True, r)
    UPSTREAM_REQUESTS.clear()
    status, r = http("POST", base + "/v1/chat/completions?provider=egg", {
        "model": "vendor/model-a",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
    })
    check("停用后不再转发", len(UPSTREAM_REQUESTS) == 0, UPSTREAM_REQUESTS)
    status, r = http("GET", base + "/v1/models?provider=egg")
    ids = [m.get("id") for m in (r.get("data") or [])]
    check("停用后清单里没有池子模型", "vendor/model-a" not in ids, ids[:10])
    http("POST", base + "/egg/pools/toggle",
         {"id": pool["id"], "enabled": True}, panel)

    # 7. 只改挑选的模型
    status, r = http("POST", base + "/egg/pools/models",
                     {"id": pool["id"], "models": ["vendor/model-b"]}, panel)
    check("改模型选择", status == 200 and r.get("ok") is True, r)
    status, r = http("GET", base + "/v1/models?provider=egg")
    ids = [m.get("id") for m in (r.get("data") or [])]
    check("只有挑中的模型在清单里",
          "vendor/model-b" in ids and "vendor/model-a" not in ids, ids[:10])

    # 8. 删除
    status, r = http("POST", base + "/egg/pools/delete", {"id": pool["id"]}, panel)
    check("删除池子", status == 200 and r.get("ok") is True, r)
    status, r = http("GET", base + "/v1/models?provider=egg")
    ids = [m.get("id") for m in (r.get("data") or [])]
    check("删除后清单里没有池子模型", "vendor/model-b" not in ids, ids[:10])
finally:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    upstream.shutdown()
    shutil.rmtree(accounts, ignore_errors=True)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
