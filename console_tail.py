#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy proxy - live console viewer.

独立的日志查看器：连接到本地 wb_proxy 服务，轮询 /logs 增量并打印到
*本进程自己的* 控制台窗口。关闭本窗口只会关掉这个查看器，服务进程照常运行。

由启动脚本（start-wb-proxy.bat）或面板「显示控制台」按钮拉起。仅依赖标准库。

用法:
    python console_tail.py --port 8788 [--host 127.0.0.1] [--token <panel-token>]
                             [--password <panel-password>]

    --token 由面板按钮场景传入（服务端内置 token，即使面板密码改过也有效）；
    未给 token 时用 --password / WB_PANEL_PASSWORD / 默认密码 admin 登录。
    任一种 token 失效都会自动重新登录。
"""
import os
import sys
import time
import json
import urllib.request
import urllib.error

HOST = "127.0.0.1"
PORT = 8788
TOKEN = ""
PASSWORD = os.environ.get("WB_PANEL_PASSWORD") or "admin"
REFRESH = 1.0
# 启动脚本会先拉起服务再开本查看器：服务就绪前的连接失败静默等待，
# 超过该时长仍连不上才退出。
STARTUP_GRACE = 30.0
# 连接成功过之后，连续失败达到该次数即认为服务已关闭，查看器自动退出。
FAIL_STREAK_EXIT = 3

# 本地回环请求绝不走代理：环境里若有全局 HTTP(S)_PROXY，请求会被代理转发，
# 服务退出时看到的就是代理层的 502 报错，而不是干净的「服务已关闭」。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ensure_console_output():
    """Force stdout/stderr onto this process's own console window.

    服务进程（pythonw）的 std 句柄可能被重定向（nohup/服务管理器）。Windows 的
    句柄继承机制会让 CREATE_NEW_CONSOLE 弹出的本进程继承那些重定向句柄，导致
    print 全写进文件、新控制台窗口一片空白。CONOUT$ 永远指向**本进程当前attached
    控制台**的屏幕缓冲区，显式重开 stdout 即可强制输出到新弹的窗口。
    正常在终端里直接运行（stdout 本来就是 tty）时不做任何事，保留管道调试能力。
    """
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            return  # 本来就输出到控制台
    except Exception:
        pass
    try:
        import ctypes
        if not ctypes.windll.kernel32.GetConsoleWindow():
            return  # 无 attached 控制台（重定向调试场景），保持原样
        sys.stdout = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
        sys.stderr = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        pass


def parse_args(argv):
    global HOST, PORT, TOKEN, PASSWORD
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--port" and i + 1 < len(argv):
            try:
                PORT = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif a == "--host" and i + 1 < len(argv):
            HOST = argv[i + 1]
            i += 2
        elif a == "--token" and i + 1 < len(argv):
            TOKEN = argv[i + 1]
            i += 2
        elif a == "--password" and i + 1 < len(argv):
            PASSWORD = argv[i + 1]
            i += 2
        else:
            i += 1


def _url(path):
    return "http://%s:%d%s" % (HOST, PORT, path)


def login():
    global TOKEN
    data = json.dumps({"password": PASSWORD}).encode("utf-8")
    req = urllib.request.Request(
        _url("/panel/login"),
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _OPENER.open(req, timeout=5) as r:
        body = json.loads(r.read().decode("utf-8"))
    TOKEN = body.get("token", "")
    return bool(TOKEN)


def fetch_logs(since_id):
    headers = {}
    if TOKEN:
        headers["X-Panel-Token"] = TOKEN
    req = urllib.request.Request(
        _url("/logs?since_id=%d&limit=300" % since_id), headers=headers)
    with _OPENER.open(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def fmt(entry):
    t = entry.get("time", "")
    lvl = entry.get("level", "")
    tag = entry.get("tag", "")
    msg = entry.get("msg", "")
    if tag and tag != "system":
        return "%s [%s/%s] %s" % (t, lvl, tag, msg)
    return "%s [%s] %s" % (t, lvl, msg)


def main():
    _ensure_console_output()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parse_args(sys.argv)
    print("=" * 60, flush=True)
    print("  WorkBuddy proxy - live console (port %d)" % PORT)
    print("  关闭本窗口【不会】停止服务，仅关闭日志查看。")
    print("  按 Ctrl+C 仅关闭本查看器。")
    print("=" * 60)
    if not TOKEN:
        try:
            login()
            print("[viewer] logged in")
        except Exception:
            print("[viewer] 等待面板登录（密码取自 --password / "
                  "WB_PANEL_PASSWORD，默认 admin）...")
    since = 0
    fail_streak = 0
    ever_ok = False
    started = time.time()
    while True:
        try:
            d = fetch_logs(since)
            ever_ok = True
            fail_streak = 0
            for e in d.get("logs", []):
                since = max(since, e.get("id", since))
                print(fmt(e))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # token 失效（服务重启过）或尚未登录：尝试重新登录。
                try:
                    login()
                    fail_streak = 0
                except Exception:
                    fail_streak += 1
            else:
                fail_streak += 1
        except Exception:
            # 连接被拒 / 超时等：只计数不打印，避免刷屏。
            fail_streak += 1
        if fail_streak >= FAIL_STREAK_EXIT:
            if ever_ok:
                print("[viewer] 服务已关闭，查看器退出。")
                print("[viewer] 本窗口将在 2 秒后自动关闭...")
                time.sleep(2)
                return
            if time.time() - started > STARTUP_GRACE:
                if TOKEN:
                    print("[viewer] 无法连接服务（30 秒无响应），查看器退出。")
                else:
                    print("[viewer] 面板登录失败：如果改过面板密码，请用"
                          " --password <密码> 或环境变量 WB_PANEL_PASSWORD 重开；"
                          "也可以直接从面板「运行日志」页查看。")
                time.sleep(2)
                return
            # 服务尚未就绪：静默等待，不打印错误。
        time.sleep(REFRESH)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # 崩溃保护：让错误显示在窗口里并停留，而不是一闪而过
        try:
            print("[viewer] fatal error: %s" % e, flush=True)
            print("[viewer] 本窗口将在 15 秒后关闭...", flush=True)
            time.sleep(15)
        except Exception:
            pass
