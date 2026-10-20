"""端到端自检（Anthropic 协议）：网关把 Anthropic 事件流翻成 OpenAI 形状。

起一个假的 Anthropic 上游（/v1/models + /v1/messages），加一个 Anthropic 协议
的池子，然后从网关发一个 OpenAI 形状的聊天请求，检查下游收到的是 OpenAI 的
chunk（而不是 Anthropic 的事件）。
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


UPSTREAM = []


class FakeAnthropic(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            body = json.dumps({"data": [
                {"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"},
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
        UPSTREAM.append({
            "path": self.path,
            "x-api-key": self.headers.get("x-api-key"),
            "version": self.headers.get("anthropic-version"),
            "body": payload,
        })
        if payload.get("tools"):
            # 带工具的请求：上游回一个 tool_use 块，网关要把它翻成
            # OpenAI 的 tool_calls（名字、参数、finish_reason=tool_calls）。
            frames = [
                ("message_start", {"type": "message_start", "message": {
                    "id": "msg_2", "role": "assistant", "model": payload.get("model"),
                    "content": [], "usage": {"input_tokens": 9, "output_tokens": 1}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "tool_use",
                                                           "id": "toolu_1",
                                                           "name": "get_weather",
                                                           "input": {}}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "input_json_delta",
                                                   "partial_json": "{\"city\":\"BJ\"}"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": "tool_use"},
                                   "usage": {"output_tokens": 6}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        else:
            frames = [
                ("message_start", {"type": "message_start", "message": {
                    "id": "msg_1", "role": "assistant", "model": payload.get("model"),
                    "content": [], "usage": {"input_tokens": 11, "output_tokens": 1}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": "pong"}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": "end_turn"},
                                   "usage": {"output_tokens": 4}}),
                ("message_stop", {"type": "message_stop"}),
            ]
        out = b""
        for event, data in frames:
            out += ("event: %s\ndata: %s\n\n"
                    % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8")
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
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


upstream = HTTPServer(("127.0.0.1", 0), FakeAnthropic)
upstream_port = upstream.server_address[1]
threading.Thread(target=upstream.serve_forever, daemon=True).start()

accounts = tempfile.mkdtemp(prefix="eggant-")
port = free_port()
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
proc = subprocess.Popen(
    [sys.executable, "wb_proxy.py", "--host", "127.0.0.1", "--port", str(port),
     "--accounts-dir", accounts],
    cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

base = "http://127.0.0.1:%d" % port
try:
    for _ in range(80):
        try:
            if http("GET", base + "/health")[0] == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)
    else:
        print("gateway did not start")
        print(proc.stdout.read(4000).decode("utf-8", "replace"))
        raise SystemExit(1)

    status, raw = http("POST", base + "/panel/login", {"password": "admin"})
    token = json.loads(raw).get("token", "")
    panel = {"X-Panel-Token": token}

    base_url = "http://127.0.0.1:%d/v1" % upstream_port
    status, raw = http("POST", base + "/egg/pools", {
        "base_url": base_url, "api_key": "sk-ant-test", "protocol": "anthropic"},
        panel)
    r = json.loads(raw)
    check("Anthropic 拉模型清单", r.get("ok") is True
          and r["models"][0]["id"] == "claude-sonnet-4-5", r)

    status, raw = http("POST", base + "/egg/pools/save", {
        "name": "Anthropic 假上游", "protocol": "anthropic",
        "base_url": base_url, "api_key": "sk-ant-test",
        "models": ["claude-sonnet-4-5"], "catalog": r.get("models") or []}, panel)
    r = json.loads(raw)
    check("保存 Anthropic 池子", r.get("ok") is True, r)
    pool = (r.get("pools") or [{}])[0]
    check("协议标签", pool.get("protocol_label") == "Anthropic 兼容", pool)

    # 流式：下游应当收到 OpenAI 的 chunk
    UPSTREAM.clear()
    status, raw = http("POST", base + "/v1/chat/completions?provider=egg", {
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": True,
    })
    check("流式请求成功", status == 200, (status, raw[:400]))
    check("上游收到请求", len(UPSTREAM) == 1, UPSTREAM)
    if UPSTREAM:
        got = UPSTREAM[0]
        check("上游路径是 /v1/messages", got["path"] == "/v1/messages", got["path"])
        check("上游带 x-api-key", got["x-api-key"] == "sk-ant-test", got)
        check("上游带版本头", got["version"] == "2023-06-01", got)
        check("上游 body 是 messages 形状",
              isinstance(got["body"].get("messages"), list), got["body"])
        check("上游 body 有 max_tokens", got["body"].get("max_tokens"), got["body"])
    check("下游是 OpenAI chunk",
          '"object": "chat.completion.chunk"' in raw, raw[:500])
    check("下游有内容", '"content": "pong"' in raw, raw[:500])
    check("下游有 finish_reason", '"finish_reason": "stop"' in raw, raw[-400:])
    check("下游有 DONE", "data: [DONE]" in raw, raw[-200:])
    check("下游不是 Anthropic 事件", "content_block_delta" not in raw, raw[:300])

    # 非流式：网关把流聚合成一个 chat.completion
    status, raw = http("POST", base + "/v1/chat/completions?provider=egg", {
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
    })
    check("非流式请求成功", status == 200, (status, raw[:400]))
    try:
        obj = json.loads(raw)
        content = ((obj.get("choices") or [{}])[0].get("message") or {}).get("content")
        check("非流式内容", content == "pong", obj)
        check("非流式用量",
              (obj.get("usage") or {}).get("total_tokens") == 15, obj.get("usage"))
    except Exception as exc:
        check("非流式内容", False, "%s %s" % (exc, raw[:300]))

    # 带工具的请求：上游回 tool_use，下游要拿到 OpenAI 的 tool_calls。
    UPSTREAM.clear()
    status, raw = http("POST", base + "/v1/chat/completions?provider=egg", {
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "天气?"}],
        "tools": [{"type": "function", "function": {
            "name": "get_weather", "description": "查天气",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}}}}}],
        "stream": False,
    })
    check("工具请求成功", status == 200, (status, raw[:400]))
    check("上游收到工具定义",
          UPSTREAM and UPSTREAM[0]["body"].get("tools")
          and UPSTREAM[0]["body"]["tools"][0]["name"] == "get_weather",
          UPSTREAM[0]["body"] if UPSTREAM else None)
    check("上游工具用 input_schema",
          UPSTREAM and "input_schema" in UPSTREAM[0]["body"]["tools"][0],
          UPSTREAM[0]["body"] if UPSTREAM else None)
    try:
        obj = json.loads(raw)
        calls = ((obj.get("choices") or [{}])[0].get("message") or {}).get("tool_calls")
        check("下游有 tool_calls", bool(calls), obj)
        if calls:
            fn = calls[0].get("function") or {}
            check("tool_calls 名字", fn.get("name") == "get_weather", calls)
            check("tool_calls 参数", '"city"' in (fn.get("arguments") or ""), calls)
        check("tool_calls 收尾",
              ((obj.get("choices") or [{}])[0].get("finish_reason") == "tool_calls"),
              obj)
    except Exception as exc:
        check("下游有 tool_calls", False, "%s %s" % (exc, raw[:400]))
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
