# -*- coding: utf-8 -*-
"""Trae 浏览器授权的「回调地址形状」自检。

背景（2026-10-22）：面板「添加 Trae 账号」在浏览器里打开 Trae 授权页后，
页面直接显示 “Login Failed / Something went wrong. Try to refresh the page.”，
国内版（trae.cn）和国际版（trae.ai）都这样。

根因不在网关这一侧，而是 Trae 授权页自己的一条硬性校验。trae.ai 与
trae.cn 的 /authorization 页面里都写死了同一个正则：

    /^http:\\/\\/127\\.0\\.0\\.1:(\\d+)\\/authorize$/

对不上就进 invalidUrl 分支，直接渲染失败页（连登录框都不出，失败文案就是
默认的 “Something went wrong. Try to refresh the page.”）。而面板此前把
看板的 origin 拼成回调地址（``location.origin + "/authorize"``），用户用
http://localhost:8788 打开看板时回调地址就成了
``http://localhost:8788/authorize`` —— localhost 不在正则里，于是每次都失败。

这里自检三件事：

  1. normalize_oauth_callback 把 localhost / 局域网 IP / 带 query 的地址都
     收拾成 127.0.0.1:<端口>/authorize；
  2. 签发出去的授权链接里，auth_callback_url 一定能过 Trae 页面的正则；
  3. 面板（不管用什么域名打开）走服务端 /accounts/login/start 拿到的链接，
     回调地址同样是合规的 127.0.0.1 形状。

    python tests/_test_trae_callback_url.py
"""

import json
import os
import sys
import threading
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_accounts  # noqa: E402
import wb_trae  # noqa: E402

FAIL = []

#: Trae 授权页（trae.ai / trae.cn）里写死的回调地址校验，原样抄过来。
TRAE_PAGE_CALLBACK_RE = r"^http://127\.0\.0\.1:(\d+)/authorize$"


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def report(name, detail=""):
    print("  ..   %s %s" % (name, detail))


def callback_of(url):
    """从授权链接里取出 auth_callback_url（页面那边 decode 一次）。"""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    raw = (query.get("auth_callback_url") or [""])[0]
    return urllib.parse.unquote(raw)


# ---------------------------------------------------------------------------
# 1) 回调地址归一化
# ---------------------------------------------------------------------------

def test_normalize():
    print("[1] 回调地址归一化")
    cases = [
        ("http://127.0.0.1:8788/authorize", "http://127.0.0.1:8788/authorize"),
        ("http://localhost:8788/authorize", "http://127.0.0.1:8788/authorize"),
        ("http://LOCALHOST:8788/authorize", "http://127.0.0.1:8788/authorize"),
        ("http://192.168.1.20:8788/authorize", "http://127.0.0.1:8788/authorize"),
        ("https://192.168.1.20:8788/authorize", "http://127.0.0.1:8788/authorize"),
        ("http://127.0.0.1:8788/authorize?provider=trae",
         "http://127.0.0.1:8788/authorize"),
        ("http://127.0.0.1:8788/other", "http://127.0.0.1:8788/authorize"),
        ("http://127.0.0.1:8788", "http://127.0.0.1:8788/authorize"),
    ]
    for src, want in cases:
        got = wb_trae.normalize_oauth_callback(src)
        check("归一化 %s" % src, got == want, "-> %r（想要 %r）" % (got, want))

    # 端口拿不到时退回 fallback_port（服务端按网关自己的端口填）。
    check("没有端口时用 fallback",
          wb_trae.normalize_oauth_callback("", 8788)
          == "http://127.0.0.1:8788/authorize",
          repr(wb_trae.normalize_oauth_callback("", 8788)))
    check("既没端口也没有 fallback 时给空串",
          wb_trae.normalize_oauth_callback("") == "",
          repr(wb_trae.normalize_oauth_callback("")))


# ---------------------------------------------------------------------------
# 2) 签发出去的授权链接
# ---------------------------------------------------------------------------

def test_login_url():
    print("[2] 授权链接里的回调地址")
    for realm, host in (("cn", "https://www.trae.cn/authorization?"),
                        ("intl", "https://www.trae.ai/authorization?")):
        # 面板此前会传 localhost / 局域网 IP 形状的回调地址。
        for src in ("http://localhost:8788/authorize",
                    "http://192.168.1.20:8788/authorize",
                    "http://127.0.0.1:8788/authorize"):
            started = wb_trae.oauth_login_url(realm, src)
            url = str(started.get("authUrl") or "")
            cb = callback_of(url)
            check("%s 授权页的回调地址合规（%s）" % (realm, src),
                  cb.startswith("http://127.0.0.1:") and cb.endswith("/authorize"),
                  "-> %r" % cb)
            check("%s 授权页地址正确" % realm, url.startswith(host), url)
            check("%s 回调地址过 Trae 页面正则" % realm,
                  _page_regex_ok(cb), "-> %r" % cb)
            check("%s 回调地址与 redirectUri 一致" % realm,
                  cb == (started.get("redirectUri") or ""),
                  "%r / %r" % (cb, started.get("redirectUri")))


def _page_regex_ok(callback):
    import re
    return bool(re.match(TRAE_PAGE_CALLBACK_RE, callback or ""))


# ---------------------------------------------------------------------------
# 3) 面板 HTTP 入口（服务端按网关自己的端口生成回调地址）
# ---------------------------------------------------------------------------

def test_http_route(tmpdir):
    import wb_proxy as P

    print("[3] 面板 HTTP 入口")
    old_pool = getattr(P, "POOL", None)
    P.POOL = wb_accounts.AccountPool(tmpdir)
    server = None
    try:
        server = P.GatewayServer(("127.0.0.1", 0), P.Handler)
        port = server.server_address[1]
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

        for realm in ("cn", "intl"):
            # 面板不传回调地址（现在的行为）与传 localhost 形状（老行为）
            # 都必须拿到合规的 127.0.0.1 回调地址。
            for payload in ({"provider": "trae", "realm": realm},
                            {"provider": "trae", "realm": realm,
                             "callbackUrl": "http://localhost:%d/authorize" % port},
                            {"provider": "trae", "realm": realm,
                             "callbackUrl": "http://192.168.1.20:%d/authorize" % port}):
                r = post("/accounts/login/start", payload)
                url = str(r.get("authUrl") or "")
                cb = callback_of(url)
                report("%s 面板拿到的回调地址" % realm, cb)
                check("%s 面板拿到的回调地址过 Trae 页面正则" % realm,
                      _page_regex_ok(cb), "-> %r" % cb)
                check("%s 回调地址指向网关自己的端口" % realm,
                      cb == "http://127.0.0.1:%d/authorize" % port,
                      "-> %r（网关端口 %d）" % (cb, port))
                check("%s redirectUri 与链接里的一致" % realm,
                      (r.get("redirectUri") or "") == cb,
                      repr(r.get("redirectUri")))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        P.POOL = old_pool


def main():
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        test_normalize()
        test_login_url()
        test_http_route(tmpdir)
    print()
    if FAIL:
        print("FAILED: %d" % len(FAIL))
        for name in FAIL:
            print("  - %s" % name)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
