"""ZCode 平台账号池的网关级自检：面板入口 / 登录方式 / 入池不泄露凭据。

起一个真网关（临时账号目录），验证：
  1. GET  /egg/platforms            —— 平台清单里有 ZCode，login 是 zcode；
  2. POST /egg/platforms/login/start —— 短 Key 报 400（不联网）；
  3. POST /egg/platforms/login/start —— mode=import：本机有 ZCode 登录态时
     走 poll 入池，池子视图不带凭据；没有登录态 / 离线时只要求给一句能看
     懂的错误，不算失败。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
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
        return 0, {"raw": str(exc)}


accounts = tempfile.mkdtemp(prefix="eggzcode-")
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

    # 1. 平台清单里有 ZCode
    status, r = http("GET", base + "/egg/platforms", headers=panel)
    check("GET /egg/platforms", status == 200 and "platforms" in r, (status, r))
    platforms = {p.get("id"): p for p in (r.get("platforms") or [])}
    check("平台清单含 ZCode", "zcode" in platforms, sorted(platforms))
    zcode_def = platforms.get("zcode") or {}
    check("ZCode 登录方式是 zcode", zcode_def.get("login") == "zcode", zcode_def)
    check("ZCode 登录标签", bool(zcode_def.get("login_label")), zcode_def)
    check("ZCode 可以查余额", zcode_def.get("balance") is True, zcode_def)
    check("ZCode 没有每日领取", zcode_def.get("claim") is not True, zcode_def)

    # 2. 短 Key：不联网直接报 400
    status, r = http("POST", base + "/egg/platforms/login/start",
                     {"platform": "zcode", "mode": "key", "key": "short"},
                     panel)
    check("短 Key 报 400", status == 400 and bool(r.get("error")), (status, r))

    # 2b. 未知登录方式报 400
    status, r = http("POST", base + "/egg/platforms/login/start",
                     {"platform": "zcode", "mode": "nope"}, panel)
    check("未知登录方式报 400", status == 400, (status, r))

    # 3. 导入本机登录态：有就入池（视图不带凭据），没有 / 离线就给一句人话。
    status, r = http("POST", base + "/egg/platforms/login/start",
                     {"platform": "zcode", "mode": "import"}, panel)
    check("导入登录态有明确答复",
          status == 200 or (status >= 400 and bool(r.get("error"))), (status, r))
    if status == 200 and r.get("status") == "success":
        state = r.get("state") or ""
        status, r2 = http("GET", base + "/egg/platforms/login/poll?state="
                          + urllib.request.quote(state), headers=panel)
        check("登录 poll 成功", status == 200 and r2.get("status") == "success",
              (status, r2))
        pools = r2.get("pools") or []
        zpool = [p for p in pools if p.get("platform") == "zcode"]
        check("ZCode 池子已出现", len(zpool) == 1, [p.get("platform") for p in pools])
        if zpool:
            blob = json.dumps(zpool[0], ensure_ascii=False)
            check("池子视图不带凭据",
                  "access_token" not in blob and "jwt" not in blob
                  and "auth_token" not in blob and "device" not in blob, blob[:400])
            accts = zpool[0].get("accounts") or []
            check("账号视图带套餐", bool(accts) and bool(accts[0].get("plan")),
                  accts)
            check("账号视图带站点", bool(accts) and bool(accts[0].get("site_label")),
                  accts)
            check("账号视图带用量",
                  bool(accts) and isinstance(accts[0].get("usage"), list)
                  and len(accts[0]["usage"]) >= 1, accts)
            # 用量接口：实时再拉一次，返回的用量行数不减少、不泄露凭据。
            status, r3 = http("POST", base + "/egg/platforms/usage",
                              {"platform": "zcode"}, panel)
            check("用量接口可用", status == 200 and r3.get("ok") is True, (status, r3))
            if status == 200:
                zpool2 = [p for p in (r3.get("pools") or [])
                          if p.get("platform") == "zcode"]
                blob2 = json.dumps(zpool2, ensure_ascii=False)
                check("用量接口不带凭据",
                      "access_token" not in blob2 and "jwt" not in blob2
                      and "auth_token" not in blob2 and "device" not in blob2,
                      blob2[:400])
                accts2 = (zpool2[0].get("accounts") or []) if zpool2 else []
                check("用量接口返回用量行",
                      bool(accts2) and len(accts2[0].get("usage") or []) >= 1,
                      accts2)
    else:
        print("  skip 本机没有可用 ZCode 登录态：%s" % (r.get("error") or status))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    raise SystemExit(1)
print("ALL PASS")
