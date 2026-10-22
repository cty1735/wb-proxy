"""跑「鸡蛋」这部分的全部自检，外加 Qoder 流式读取、排队重试与登录分流那三组。

    python tests/run_egg.py

包含七组 Python 套件（池子存取 / 与 wb_zen 的联动 / OpenAI 协议端到端 /
Anthropic 协议端到端 / Qoder 流式读取 / Qoder 排队重试 / Qoder 登录分流）
与一组 Node 套件（面板的池子渲染与筛选）。

端到端那几套会真的起一个网关进程与一个假上游，所以比纯单元测试慢一点
（每个约十几秒）。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

PY_SUITES = [
    "_test_egg_pools.py",
    "_test_egg_platforms.py",
    "_test_egg_zen.py",
    "_test_egg_e2e.py",
    "_test_egg_anthropic.py",
    "_test_egg_smoke.py",
    "_test_agent_source_tags.py",
    "_test_qoder_stream.py",
    "_test_qoder_queue.py",
    "_test_qoder_login.py",
]
JS_SUITES = [
    "_check_dashboard_egg.js",
]


def run(argv, cwd):
    print("$ %s" % " ".join(argv))
    proc = subprocess.run(argv, cwd=cwd)
    print("")
    return proc.returncode


def main():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    failed = []
    for name in PY_SUITES:
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            continue
        proc = subprocess.run([sys.executable, path], cwd=ROOT, env=env)
        if proc.returncode:
            failed.append(name)
    for name in JS_SUITES:
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            continue
        if not _have_node():
            print("skip %s（PATH 上没有 node）" % name)
            continue
        proc = subprocess.run(["node", path], cwd=ROOT)
        if proc.returncode:
            failed.append(name)
    print("")
    if failed:
        print("FAILED: %s" % ", ".join(failed))
        return 1
    print("ALL PASS")
    return 0


def _have_node():
    try:
        subprocess.run(["node", "--version"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
