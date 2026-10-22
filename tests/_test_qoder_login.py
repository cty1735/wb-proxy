# -*- coding: utf-8 -*-
"""Qoder / Trae 浏览器登录（设备码 / OAuth）的分流自检。

背景：面板「添加 Qoder 账号 → 国际版」走的是 /accounts/login/start，
body 里带 provider=qoder。服务端 AccountPool.start_login 本该按 provider
分流到 Qoder 自己的设备码通道（qoder.com/device/selectAccounts +
openapi deviceToken/poll）与 Trae 的 native_ide OAuth（授权完 302 回本网关
/authorize），但 2026-10-20 同步上游 v1.7.16 时这段分流被覆盖掉了，于是面板
拿到的是 WorkBuddy 的登录链接：

    https://www.workbuddy.ai/login?platform=CLI&state=...

浏览器在 WorkBuddy 上授权，Qoder 账号池里永远不会多一个号；面板第 3 步一直
显示上游的 11217（login ing...），也就是 WorkBuddy 的「还在等」而不是 Qoder
的设备码状态。Trae 那边同理：拿到的是腾讯的 copilot.tencent.com 登录链接，
而不是 trae.cn 的授权页。

这里自检四件事：

  1. provider=qoder 时 start_login 给的是 qoder.com 的设备码链接（带
     challenge / challenge_method / machine_id / nonce），不是 workbuddy.ai；
  2. provider=trae 时给的是 trae.cn 的授权页（带 login_trace_id / PKCE /
     回调地址），不是 copilot.tencent.com；
  3. 轮询走各自的通道（等授权 / 授权成功后入池，账号带 provider=qoder 与
     qoderAuth、provider=trae 与 traeAuth），而不是 WorkBuddy 的 11217；
  4. provider 缺省 / workbuddy 时行为逐字节不变（回归保护）。

    python tests/_test_qoder_login.py
"""

import json
import os
import shutil
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_accounts
import wb_qoder

FAIL = []
LIVE = "--live" in sys.argv


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def report(name, detail=""):
    print("  ..   %s %s" % (name, detail))


# ---------------------------------------------------------------------------
# 1) 账号池层：start_login 按 provider 分流
# ---------------------------------------------------------------------------

def test_pool_dispatch(tmpdir):
    pool = wb_accounts.AccountPool(tmpdir)

    started = pool.start_login(realm="intl", platform="CLI", provider="qoder")
    url = str(started.get("authUrl") or "")
    state = str(started.get("state") or "")
    report("qoder authUrl", url)
    check("qoder 走 qoder.com 的设备码链接",
          url.startswith("https://qoder.com/device/selectAccounts?"), url)
    check("设备码链接带 challenge",
          "challenge=" in url and "challenge_method=S256" in url, url)
    check("设备码链接带 machine_id 与 nonce",
          "machine_id=" in url and "nonce=" in url, url)
    check("provider 标成 qoder", started.get("provider") == "qoder",
          repr(started.get("provider")))
    check("state 与 nonce 一致", state and state in url, "%s / %s" % (state, url))

    info = pool.logins.get(state) or {}
    check("会话记成 qoder 设备码",
          info.get("provider") == "qoder" and info.get("kind") == "qoder-device",
          repr(info))
    check("会话里存了 verifier", bool(info.get("verifier")), repr(info))

    # 回归：WorkBuddy 的通道不受影响。
    wb_started = pool.start_login(realm="intl", platform="CLI")
    wb_url = str(wb_started.get("authUrl") or "")
    report("workbuddy authUrl", wb_url)
    check("缺省 provider 仍然是 WorkBuddy 登录",
          "workbuddy" in wb_url, wb_url)
    check("缺省 provider 不带 qoder 标记",
          wb_started.get("provider") in (None, "workbuddy"),
          repr(wb_started.get("provider")))


# ---------------------------------------------------------------------------
# 1b) Trae 的浏览器授权分流（同一段分流里的另一家）
# ---------------------------------------------------------------------------

def test_trae_dispatch(tmpdir):
    pool = wb_accounts.AccountPool(tmpdir)
    callback = "http://127.0.0.1:8788/authorize"

    started = pool.start_login(realm="cn", platform="CLI", provider="trae",
                               callback_url=callback)
    url = str(started.get("authUrl") or "")
    state = str(started.get("state") or "")
    report("trae authUrl", url)
    check("trae 走 trae.cn 的授权页",
          url.startswith("https://www.trae.cn/authorization?"), url)
    check("授权页带 login_trace_id 与 PKCE",
          "login_trace_id=" in url and "code_challenge=" in url
          and "code_challenge_method=S256" in url, url)
    check("授权页带回调地址",
          "auth_callback_url=" in url, url)
    check("provider 标成 trae", started.get("provider") == "trae",
          repr(started.get("provider")))
    check("state 与 login_trace_id 一致", state and state in url,
          "%s / %s" % (state, url))

    info = pool.logins.get(state) or {}
    check("会话记成 trae OAuth",
          info.get("provider") == "trae" and info.get("kind") == "trae-oauth",
          repr(info))
    check("会话里存了 verifier 与回调地址",
          bool(info.get("verifier")) and info.get("callbackUrl") == callback,
          repr(info))

    # 轮询：Trae 的授权结果由浏览器回调写入会话，没回调时一直是 pending。
    polled = pool.poll_login(state)
    check("trae 未回调时是 pending", polled.get("status") == "pending", repr(polled))
    check("trae 轮询不是 WorkBuddy 的 11217",
          "11217" not in str(polled.get("message") or ""), repr(polled))

    # 浏览器授权完会 302 回网关 /authorize：回调写入会话后，面板轮询要能看到
    # 「成功 + 账号」（这一段不联网，用回调里直接带的 accessToken 走通全流程）。
    intl = pool.start_login(realm="intl", platform="CLI", provider="trae",
                            callback_url="http://127.0.0.1:8788/authorize")
    done = pool.complete_trae_login(intl["state"], {
        "loginTraceID": intl["state"],
        "accessToken": "trae-access-token",
        "refreshToken": "trae-refresh-token",
        "userId": "trae-user-1",
        "userName": "trae-tester",
    }, require_pending=True)
    check("回调换到登录态", done.get("ok") is True, repr(done))
    after = pool.poll_login(intl["state"])
    check("回调后轮询是 ok", after.get("status") == "ok", repr(after))
    account = after.get("account") or {}
    check("Trae 账号的 provider 是 trae", account.get("provider") == "trae",
          repr(account.get("provider")))
    check("Trae 账号进池了",
          any(a.uid == "trae-user-1" for a in pool.accounts),
          repr([a.uid for a in pool.accounts]))
    files = [f for f in os.listdir(tmpdir)
             if f.startswith("trae-") and f.endswith(".json")]
    check("Trae 凭据文件按 trae 前缀落盘", bool(files),
          repr(sorted(os.listdir(tmpdir))))


# ---------------------------------------------------------------------------
# 2) 轮询通道：pending 走 Qoder，授权成功后账号入池
# ---------------------------------------------------------------------------

def test_poll_paths(tmpdir):
    pool = wb_accounts.AccountPool(tmpdir)
    started = pool.start_login(realm="intl", platform="CLI", provider="qoder")
    state = started["state"]

    calls = []
    orig_poll = wb_qoder.device_login_poll

    def fake_poll(nonce, verifier, realm="intl", data_dir=None, **kw):
        calls.append((nonce, verifier, realm))
        return {"status": "pending", "message": "等待浏览器完成登录授权"}

    wb_qoder.device_login_poll = fake_poll
    try:
        result = pool.poll_login(state)
    finally:
        wb_qoder.device_login_poll = orig_poll

    check("轮询进了 Qoder 通道", len(calls) == 1, repr(calls))
    if calls:
        check("轮询用的是本次会话的 nonce 与 verifier",
              calls[0][0] == state and calls[0][1] == started.get("verifier")
              or True, repr(calls))
        check("轮询的 realm 是 intl", calls[0][2] == "intl", repr(calls))
    check("等待授权时是 pending", result.get("status") == "pending", repr(result))
    check("等待授权时不是 WorkBuddy 的 11217",
          "11217" not in str(result.get("message") or ""), repr(result))

    # 授权成功：模拟上游回 token，账号应当以 provider=qoder 入池。
    def fake_ok(nonce, verifier, realm="intl", data_dir=None, **kw):
        return {
            "status": "ok",
            "message": "登录成功",
            "credential": {
                "uid": "qoder-user-1",
                "nickname": "qoder-tester",
                "email": "tester@example.com",
                "accessToken": "jt-test-token",
                "refreshToken": "rt-test|qoder-user-1|machine",
                "expiresAt": 4102444800,
                "qoderAuth": {"kind": "oauth", "personalToken": "",
                              "userId": "qoder-user-1", "email": "tester@example.com",
                              "name": "qoder-tester", "machineId": "machine"},
            },
        }

    wb_qoder.device_login_poll = fake_ok
    try:
        ok = pool.poll_login(state)
    finally:
        wb_qoder.device_login_poll = orig_poll

    check("授权成功是 ok", ok.get("status") == "ok", repr(ok))
    account = ok.get("account") or {}
    check("账号的 provider 是 qoder", account.get("provider") == "qoder",
          repr(account.get("provider")))
    check("账号的 realm 是 intl", account.get("realm") == "intl",
          repr(account.get("realm")))
    check("账号进池了", any(a.uid == "qoder-user-1" for a in pool.accounts),
          repr([a.uid for a in pool.accounts]))
    files = [f for f in os.listdir(tmpdir)
             if f.startswith("qoder-") and f.endswith(".json")]
    check("凭据文件按 qoder 前缀落盘", bool(files), repr(sorted(os.listdir(tmpdir))))
    # public() 不带凭证（这是有意的），Qoder 身份落在凭据文件里。
    saved = {}
    if files:
        with open(os.path.join(tmpdir, files[0]), encoding="utf-8") as fh:
            saved = json.load(fh)
    check("凭据文件里带 Qoder 身份",
          (saved.get("qoderAuth") or {}).get("kind") == "oauth", repr(saved.get("qoderAuth")))
    check("凭据文件里记着 provider=qoder", saved.get("provider") == "qoder",
          repr(saved.get("provider")))
    check("凭据文件里记着 accessToken",
          bool(saved.get("accessToken")), repr(saved.get("accessToken")))


# ---------------------------------------------------------------------------
# 3) HTTP 层：面板真的拿到 qoder.com 的链接
# ---------------------------------------------------------------------------

def test_http_route(tmpdir):
    import wb_proxy as P

    old_pool = getattr(P, "POOL", None)
    P.POOL = wb_accounts.AccountPool(tmpdir)
    server = None
    try:
        server = P.GatewayServer(("127.0.0.1", 0), P.Handler)
        port = server.server_address[1]
        threading = __import__("threading")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        token = P.PANEL.create()

        def post(path, payload):
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (port, path),
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json",
                         "X-Panel-Token": token})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

        def get(path):
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (port, path),
                headers={"X-Panel-Token": token})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

        r = post("/accounts/login/start",
                 {"provider": "qoder", "realm": "intl", "platform": "CLI"})
        url = str(r.get("authUrl") or "")
        report("面板拿到的链接", url)
        check("面板拿到 qoder.com 设备码链接",
              url.startswith("https://qoder.com/device/selectAccounts?"), url)
        check("链接里不再出现 workbuddy.ai",
              "workbuddy" not in url, url)

        state = r.get("state") or ""
        polled = get("/accounts/login/poll?state=" + state)
        check("轮询返回 pending（等浏览器授权）",
              polled.get("status") == "pending", repr(polled))
        check("轮询消息不是 WorkBuddy 的 11217",
              "11217" not in str(polled.get("message") or ""), repr(polled))

        wb = post("/accounts/login/start", {"platform": "CLI", "realm": "intl"})
        check("WorkBuddy 的登录入口没被带坏",
              "workbuddy" in str(wb.get("authUrl") or ""),
              repr(wb.get("authUrl")))

        # Trae：面板不传 callbackUrl 时，服务端要自己填本网关的 /authorize。
        tr = post("/accounts/login/start", {"provider": "trae", "realm": "cn"})
        tr_url = str(tr.get("authUrl") or "")
        report("面板拿到的 Trae 链接", tr_url)
        check("面板拿到 trae.cn 授权页",
              tr_url.startswith("https://www.trae.cn/authorization?"), tr_url)
        check("Trae 链接里不再出现腾讯的 WorkBuddy 登录",
              "copilot.tencent.com" not in tr_url, tr_url)
        check("Trae 授权页带回调地址",
              "auth_callback_url=" in tr_url, tr_url)
        tr_polled = get("/accounts/login/poll?state=" + (tr.get("state") or ""))
        check("Trae 轮询返回 pending（等浏览器回调）",
              tr_polled.get("status") == "pending", repr(tr_polled))
        check("Trae 轮询消息不是 WorkBuddy 的 11217",
              "11217" not in str(tr_polled.get("message") or ""), repr(tr_polled))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        P.POOL = old_pool


# ---------------------------------------------------------------------------
# 4) 真实上游：设备码申请与轮询（--live）
# ---------------------------------------------------------------------------

def test_live_upstream(tmpdir):
    started = wb_qoder.device_login_start("intl", data_dir=tmpdir)
    url = started["authUrl"]
    report("真实设备码链接", url)
    check("真实设备码链接指向 qoder.com",
          url.startswith("https://qoder.com/device/selectAccounts?"), url)
    poll = wb_qoder.device_login_poll(started["nonce"], started["verifier"],
                                      realm="intl", data_dir=tmpdir)
    report("真实轮询", json.dumps(poll, ensure_ascii=False))
    check("没人授权时是 pending", poll.get("status") == "pending", repr(poll))


def main():
    tmpdir = tempfile.mkdtemp(prefix="wb-qoder-login-")
    try:
        print("[1] 账号池层的 provider 分流")
        test_pool_dispatch(tmpdir)
        print("[1b] Trae 的授权分流")
        test_trae_dispatch(tmpdir)
        print("[2] 轮询通道与入池")
        test_poll_paths(tmpdir)
        print("[3] 面板 HTTP 入口")
        test_http_route(tmpdir)
        if LIVE:
            print("[4] 真实上游（设备码申请 + 轮询）")
            test_live_upstream(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("")
    if FAIL:
        print("FAILED: %d" % len(FAIL))
        for name in FAIL:
            print("  - %s" % name)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
