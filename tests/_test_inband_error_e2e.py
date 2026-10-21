"""端到端自检：上游在 200 的流里回错误时，客户端看到的是错误，不是「没有 finish_reason」。

背景：平台池（华为云 CodeArts 等）拒单时回的是 HTTP 200 + 一个错误对象：

    {"error_code": "InferHub.4004.200", "error_msg": "benefit not found"}

这种块没有 choices，也没有 finish_reason。网关以前把它当正文透传，然后只写
一个 data: [DONE] 就收尾——客户端（@ai-sdk/openai-compatible 这类只认
finish_reason 的）于是判定「流被截断」，报
"OpenAI Chat stream ended without finish_reason" 并对同一个死上游反复重试；
网关自己还把这次记成成功。

这里用一个假的上游（本地 http.server）当供应商池，验证：
  1. 流式请求拿到的是错误事件（带真实原因），不是半截正文
  2. 流式请求仍然带一个 finish_reason 收尾块，客户端不会判定流被截断
  3. 非流式请求直接 502，错误文案是上游给的
  4. 用量记录里这次是失败（failed），不是成功
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


# --- 假的上游：模型名带 "boom" 的回带内错误，其它模型正常答 ---------------
UPSTREAM_REQUESTS = []
INBAND = {
    "error_code": "InferHub.4004.200",
    "error_msg": "benefit not found",
    "details": [{"error_code": "InferHub.4004.200",
                 "error_msg": "benefit not found"}],
}


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": "vendor/boom-model",
                                         "name": "Boom"}]}).encode()
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
        UPSTREAM_REQUESTS.append(payload)
        out = b""
        if "boom" in str(payload.get("model") or ""):
            # 上游拒单：HTTP 200 + 一个错误对象，然后直接结束。
            out += ("data: %s\n\n" % json.dumps(INBAND)).encode()
            out += b"data: [DONE]\n\n"
        else:
            chunks = [
                {"id": "c1", "object": "chat.completion.chunk",
                 "model": payload.get("model"),
                 "choices": [{"index": 0, "delta": {"role": "assistant"},
                              "finish_reason": None}]},
                {"id": "c1", "object": "chat.completion.chunk",
                 "model": payload.get("model"),
                 "choices": [{"index": 0, "delta": {"content": "pong"},
                              "finish_reason": None}]},
                {"id": "c1", "object": "chat.completion.chunk",
                 "model": payload.get("model"),
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            for c in chunks:
                out += ("data: %s\n\n" % json.dumps(c)).encode()
            out += b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


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
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.dumps(json.loads(raw.decode("utf-8")),
                                        ensure_ascii=False)
        except Exception:
            return exc.code, raw.decode("utf-8", "replace")


upstream = HTTPServer(("127.0.0.1", 0), FakeUpstream)
upstream_port = upstream.server_address[1]
threading.Thread(target=upstream.serve_forever, daemon=True).start()

accounts = tempfile.mkdtemp(prefix="inband-")
usage_dir = tempfile.mkdtemp(prefix="inband-usage-")
port = free_port()
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
proc = subprocess.Popen(
    [sys.executable, "wb_proxy.py", "--host", "127.0.0.1", "--port", str(port),
     "--accounts-dir", accounts, "--usage-dir", usage_dir],
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

    status, raw = http("POST", base + "/panel/login", {"password": "admin"})
    r = json.loads(raw)
    check("面板登录", status == 200 and r.get("token"), r)
    panel = {"X-Panel-Token": r.get("token", "")}

    # 建一个池子，模型叫 vendor/boom-model（上游对它回带内错误）
    status, raw = http("POST", base + "/egg/pools/save", {
        "name": "假上游", "protocol": "openai",
        "base_url": "http://127.0.0.1:%d/v1" % upstream_port,
        "api_key": "sk-test", "models": ["vendor/boom-model"],
        "catalog": [{"id": "vendor/boom-model", "name": "Boom"}],
    }, panel)
    check("池子已保存", status == 200, raw[:300])

    # 1+2. 流式：错误事件 + finish_reason 收尾
    status, raw = http("POST", base + "/v1/chat/completions?provider=egg", {
        "model": "vendor/boom-model", "stream": True,
        "messages": [{"role": "user", "content": "ping"}],
    })
    check("流式请求返回 200", status == 200, (status, raw[:300]))
    events = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data and data != "[DONE]":
                try:
                    events.append(json.loads(data))
                except Exception:
                    events.append({"raw": data})
    check("上游的错误事件到了客户端",
          any(e.get("error") for e in events), events)
    err = next((e.get("error") for e in events if e.get("error")), None)
    check("错误文案是上游给的 benefit not found",
          err and "benefit not found" in (err.get("message") or ""), err)
    finishes = [c.get("finish_reason")
                for e in events for c in (e.get("choices") or [])]
    check("流里带一个 finish_reason 收尾块",
          any(f for f in finishes), finishes)
    check("带内错误对象没被当正文透传",
          not any(e.get("error_code") for e in events), events)
    check("没有把错误内容拼进 delta",
          not any((c.get("delta") or {}).get("content")
                  for e in events for c in (e.get("choices") or [])),
          events)

    # 3. 非流式：直接 502，错误文案是上游给的
    status, raw = http("POST", base + "/v1/chat/completions?provider=egg", {
        "model": "vendor/boom-model", "stream": False,
        "messages": [{"role": "user", "content": "ping"}],
    })
    check("非流式请求报 502", status == 502, (status, raw[:300]))
    check("非流式的错误文案是上游给的", "benefit not found" in raw, raw[:300])

    # 4. 用量记录里这次是失败
    usage = os.path.join(usage_dir, "usage.jsonl")
    rows = []
    if os.path.isfile(usage):
        with open(usage, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    failed = [r for r in rows if r.get("outcome") == "failed"]
    check("用量里记的是失败而不是成功", failed, rows[-3:])
    if failed:
        check("失败原因写的是上游的错",
              "benefit not found" in json.dumps(failed[-1], ensure_ascii=False),
              failed[-1])
    done = [r for r in rows if r.get("outcome") == "completed"]
    check("没有把这次记成成功", not done, done[-2:])
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
    shutil.rmtree(usage_dir, ignore_errors=True)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
