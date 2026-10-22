# -*- coding: utf-8 -*-
"""端到端自检：起一个真网关，验证「省 token」的三条对外路径。

  1. 本地应答应在打开上游之前就把答案给出去（不碰账号池，用量记 0）；
  2. 流式与非流式两种回包形状都要对得上（有的客户端只认其中一种）；
  3. 统计端点与设置保存都能从面板走到落盘，再读回来是同一个值；
  4. 面板里确实有这一块（区块 id、统计卡 id、脚本函数都在）；
  5. 单条请求带 X-WB-Token-Saver: off 时绕过本地应答 —— 没有账号池就该
     走到 upstream 失败，而不是再被本地回掉。

提示词压缩与 RTK 的字节效果是纯函数行为，见 tests/_test_token_saver.py；
这里只验证它们确实挂在请求链路上。
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
import wb_tokensaver

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http(method, url, body=None, headers=None, raw=False):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, text if raw else json.loads(text)
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(text)
        except Exception:
            return exc.code, {"raw": text}
    except Exception as exc:
        return 0, {"error": str(exc)}


accounts = tempfile.mkdtemp(prefix="tokensaver-e2e-")
with open(os.path.join(accounts, "settings.json"), "w", encoding="utf-8") as fh:
    json.dump({"token_saver": {
        "enabled": True,
        "prompt": {"caveman": "full", "ponytail": "off"},
        "rtk": True,
        "local": {"enabled": True},
    }}, fh)

port = free_port()
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
proc = subprocess.Popen(
    [sys.executable, "wb_proxy.py", "--host", "127.0.0.1", "--port", str(port),
     "--accounts-dir", accounts],
    cwd=ROOT, env=env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

base = "http://127.0.0.1:%d" % port
try:
    for _ in range(120):
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

    status, reply = http("POST", base + "/panel/login", {"password": "admin"})
    check("面板登录", status == 200 and reply.get("token"), reply)
    panel = {"X-Panel-Token": reply.get("token", "")}

    # 1. 本地应答（非流式）
    probe = {"model": "test-model", "messages": [{"role": "user", "content": "额度检查"}]}
    status, reply = http("POST", base + "/v1/chat/completions", probe)
    check("额度探测由本地应答", status == 200, (status, reply))
    choice = (reply.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    check("答复是那句固定话术", content == wb_tokensaver.QUOTA_ANSWER, content)
    check("finish_reason=stop", choice.get("finish_reason") == "stop", choice)
    check("用量记为 0（没碰上游）",
          (reply.get("usage") or {}).get("total_tokens") == 0, reply.get("usage"))

    # 2. 本地应答（流式）
    stream_probe = dict(probe, stream=True)
    status, text = http("POST", base + "/v1/chat/completions", stream_probe, raw=True)
    check("流式也是 200", status == 200, status)
    check("流式正文里带答复", wb_tokensaver.QUOTA_ANSWER in text, text[:200])
    check("流式以 [DONE] 收尾", text.strip().endswith("[DONE]"), text[-80:])
    check("流式带 finish_reason=stop", '"finish_reason": "stop"' in text
          or '"finish_reason":"stop"' in text, text[-400:])

    # 3. 统计
    status, stats = http("GET", base + "/token-saver/stats", headers=panel)
    check("GET /token-saver/stats", status == 200, (status, stats))
    check("两次探测都记进本地应答", (stats.get("local") or {}).get("quota") == 2,
          stats.get("local"))
    check("统计字段齐全",
          all(key in stats for key in ("before", "after", "saved", "blocks",
                                       "calls", "injections", "percent", "local")),
          list(stats.keys()))
    check("统计端点要面板口令",
          http("GET", base + "/token-saver/stats")[0] == 401)

    # 4. 设置保存
    status, saved = http("POST", base + "/settings/save",
                         {"token_saver": {"prompt": {"caveman": "wenyan-ultra"},
                                          "rtk": False}},
                         headers=panel)
    check("POST /settings/save token_saver", status == 200, (status, saved))
    stored = saved.get("token_saver") or {}
    check("档位保存后回包就是存下的值",
          stored.get("prompt", {}).get("caveman") == "wenyan-ultra", stored)
    check("RTK 开关保存", stored.get("rtk") is False, stored)
    check("没改的字段保留（本地应答仍开）",
          stored.get("local", {}).get("enabled") is True, stored)
    status, view = http("GET", base + "/settings", headers=panel)
    check("GET /settings 带同一份 token_saver",
          (view.get("token_saver") or {}).get("prompt", {}).get("caveman")
          == "wenyan-ultra", view.get("token_saver"))

    # 5. 面板里有这一块
    status, html = http("GET", base + "/", headers=panel, raw=True)
    for marker, label in (
            ('id="secTokenSaver"', "设置区块"),
            ('id="tsBefore"', "统计卡「压缩前」"),
            ('id="tsSaved"', "统计卡「节省字节」"),
            ('id="setTokenSaverEnabled"', "总开关"),
            ('id="setTokenSaverRtk"', "RTK 开关"),
            ('id="setTokenSaverLocal"', "本地应答总开关"),
            ("function saveTokenSaver(", "保存函数"),
            ("function tsApply(", "重放函数"),
            ("/token-saver/stats", "统计端点")):
        check("面板有" + label + "（" + marker + "）", marker in html, marker[:40])

    # 6. 单条请求绕过
    off = dict(probe)
    status, reply = http("POST", base + "/v1/chat/completions", off,
                         headers={"X-WB-Token-Saver": "off"})
    check("X-WB-Token-Saver: off 不再本地应答",
          status != 200, (status, reply))
    status, stats2 = http("GET", base + "/token-saver/stats", headers=panel)
    check("绕过的那条没有记本地应答",
          (stats2.get("local") or {}).get("quota") == 2, stats2.get("local"))
finally:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

if FAIL:
    print("\n%d FAILED: %s" % (len(FAIL), ", ".join(FAIL)))
    sys.exit(1)
print("\nall checks passed")
