"""冒烟：起一个真网关，确认面板页面里带着「供应商池」那一整块。

顺便确认 /v1/models 的清单里池子的模型带了池子归属字段。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

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


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "vendor/model-a"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


upstream = HTTPServer(("127.0.0.1", 0), FakeUpstream)
upstream_port = upstream.server_address[1]
threading.Thread(target=upstream.serve_forever, daemon=True).start()

accounts = tempfile.mkdtemp(prefix="eggsmoke-")
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

    # 面板页面：新的「供应商池」区块在
    status, page = http("GET", base + "/")
    check("面板页面可取", status == 200, status)
    for needle in ("eggPoolsBlock", "eggPresetList", "eggPoolList",
                   "eggPoolName", "eggPoolProtocol", "eggPoolBaseUrl",
                   "eggPoolKey", "eggPoolFetchBtn", "eggPoolSaveBtn",
                   "eggPoolFilter", "供应商池", "免费模型预设", "添加供应商",
                   "拉取模型列表", "挑选模型"):
        check("页面里有「%s」" % needle, needle in page)

    # 面板会话
    status, raw = http("POST", base + "/panel/login", {"password": "admin"})
    token = json.loads(raw).get("token", "")
    panel = {"X-Panel-Token": token}

    status, raw = http("GET", base + "/egg/pools", headers=panel)
    r = json.loads(raw)
    check("池子接口可用", status == 200 and "pools" in r, r)
    check("预设齐了", len(r.get("presets") or []) == 17,
          len(r.get("presets") or []))
    for host in ("sensenova.cn", "agnes-ai.cn"):
        check("预设含 %s" % host,
              any(host in (p.get("base_url") or "")
                  for p in (r.get("presets") or [])))

    # 加一个池子，确认清单里带上归属
    status, raw = http("POST", base + "/egg/pools/save", {
        "name": "冒烟上游", "protocol": "openai",
        "base_url": "http://127.0.0.1:%d/v1" % upstream_port,
        "models": ["vendor/model-a"],
        "catalog": [{"id": "vendor/model-a", "name": "Model A"}],
    }, panel)
    r = json.loads(raw)
    check("保存池子", r.get("ok") is True, r)

    status, raw = http("GET", base + "/v1/models?provider=egg")
    r = json.loads(raw)
    entry = [m for m in (r.get("data") or []) if m.get("id") == "vendor/model-a"]
    check("清单里有池子模型", bool(entry), [m.get("id") for m in (r.get("data") or [])][:12])
    check("模型带池子 id", entry and entry[0].get("egg_source", "").startswith("pool-"),
          entry)
    check("模型带池子名", entry and entry[0].get("egg_source_label") == "冒烟上游", entry)
    check("模型带池子类型", entry and entry[0].get("egg_source_kind") == "pool", entry)
    check("egg 状态带池子", len((r.get("egg") or {}).get("pools") or []) == 1,
          r.get("egg"))
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
