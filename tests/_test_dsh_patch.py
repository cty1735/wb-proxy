# -*- coding: utf-8 -*-
"""DSH profile patch（cordis.patch.yml）写入自检。

    python tests/_test_dsh_patch.py

新版 DSH（带 Settings 服务的版本）只读 profile 的 cordis.patch.yml，而且
`- id: llm-pi-ai` 是**整段替换**插件 config：网关以前只写 settings.yaml，
DSH 界面里就一直停着旧模型表（重新应用 + 重启也没用）。这里验证 patch 写入：

  - 块式 patch：换掉 wb-proxy 的模型表，同一条目里的其它 provider 原样保留
  - flow 式 patch（`[{id: llm-pi-ai, ...}]`）：替换 / 插入 wb-proxy，zen-gate 不动
  - 默认模型条目只在本网关条目上更新，不是本网关的一律不碰
  - 重复写入幂等（指纹没变不重排字节）
  - 投影只看 id 顺序与默认模型：DSH 自己重排格式不会误判成「用户改过」
  - _dsh_patch_paths 只挑已经声明 llm-pi-ai + providers 的 patch

全部在临时目录跑，不碰真实的 ~/.dsh。
"""

import io
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_agents

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


BLOCK_PATCH = """\
# patch layer
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      wb-proxy:
        displayName: WB Proxy
        baseURL: http://127.0.0.1:8788/v1
        models:
          - id: hy4-preview
            contextWindow: 960000
      zen-gate:
        displayName: Zen Gate
        baseURL: http://127.0.0.1:8787/v1
        models:
          - id: flint-40b-free
- id: ui-chat
  name: "@deepseek-ai/dsh-client-ui-chat"
  config:
    transcriptView: standard
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: wb-proxy
    model: hy4-preview
"""

FLOW_PATCH = """\
# patch layer
[{id: llm-pi-ai, name: '@deepseek-ai/dsh-llm-pi-ai', config: {providers: {zen-gate: {displayName: Zen Gate, baseURL: 'http://127.0.0.1:8787/v1', models: [{id: flint-40b-free}]}}}}]
"""

PROVIDER = {
    "displayName": "WB Proxy",
    "apiKeyEnv": "WB_PROXY_API_KEY",
    "api": "openai-completions",
    "baseURL": "http://127.0.0.1:8788/v1",
    "headers": {"X-WB-Agent": "dsh"},
    "models": [
        {"id": "route/auto", "contextWindow": 200000},
        {"id": "deepseek-v4-pro · WorkBuddy-cn", "contextWindow": 1000000,
         "reasoningEfforts": {"high": "high"}, "supportsVision": True},
    ],
}


def test_block():
    print("块式 patch")
    out = wb_agents.dsh_patch_apply(BLOCK_PATCH, PROVIDER, "route/auto")
    check("旧模型表换掉了", "hy4-preview" not in out.split("zen-gate")[0])
    check("新模型表写进去了", "deepseek-v4-pro · WorkBuddy-cn" in out)
    check("zen-gate 原样保留", "flint-40b-free" in out
          and "Zen Gate" in out)
    check("条目结构没被拆", out.count("- id: llm-pi-ai") == 1)
    check("它自己的条目还在", "transcriptView: standard" in out)
    check("默认模型跟着换",
          wb_agents.dsh_patch_projection(out)["default"]
          == "wb-proxy/route/auto")
    again = wb_agents.dsh_patch_apply(out, PROVIDER, "route/auto")
    check("重复写入幂等", again == out)
    return out


def test_flow():
    print("flow 式 patch")
    out = wb_agents.dsh_patch_apply(FLOW_PATCH, PROVIDER, "route/auto")
    check("wb-proxy 插进去了", "wb-proxy:" in out)
    check("zen-gate 原样保留", "flint-40b-free" in out)
    check("只有一条 llm-pi-ai 条目", out.count("id: llm-pi-ai") == 1)
    check("括号仍然配平",
          out.count("{") == out.count("}") and out.count("[") == out.count("]"))
    proj = wb_agents.dsh_patch_projection(out)
    check("投影拿到 id 顺序",
          proj["ids"] == ["route/auto", "deepseek-v4-pro · WorkBuddy-cn"], proj)
    again = wb_agents.dsh_patch_apply(out, PROVIDER, "route/auto")
    check("重复写入幂等", again == out)
    return out


def test_default_entry_untouched():
    print("默认模型条目只认自己的")
    text = BLOCK_PATCH.replace("provider: wb-proxy", "provider: zen-gate")
    out = wb_agents.dsh_patch_apply(text, PROVIDER, "route/auto")
    check("别家的默认模型不动", "provider: zen-gate" in out
          and "model: hy4-preview" in out)
    check("投影里没有默认模型",
          wb_agents.dsh_patch_projection(out)["default"] is None)


def test_projection_formatting():
    print("投影不看格式")
    out = wb_agents.dsh_patch_apply(BLOCK_PATCH, PROVIDER, "route/auto")
    # DSH 自己重写 patch 时会缩进 / 键序变化：投影必须一样。
    reindented = "\n".join(
        line.replace("      wb-proxy:", "      wb-proxy:") + "  "
        if line.strip().startswith("contextWindow") else line
        for line in out.split("\n"))
    check("缩进变化不影响投影",
          wb_agents.dsh_patch_projection(reindented)
          == wb_agents.dsh_patch_projection(out))
    # DSH 的 YAML 写出器会去掉不必要的引号（`id: "a · B"` → `id: a · B`）。
    # 投影必须拿到全名，不能在第一个空白处截断 —— 截断后和记录对不上，这个
    # 客户端会被一直判成「用户手改过」，池子的同步再也不碰它。
    unquoted = re.sub(r'id: "([^"]*)"', r"id: \1", out)
    check("去引号不影响投影",
          wb_agents.dsh_patch_projection(unquoted)
          == wb_agents.dsh_patch_projection(out),
          wb_agents.dsh_patch_projection(unquoted)["ids"][:3])


def test_reasoning_levels():
    print("思考档位只写 DSH 认得的键")
    levels = wb_agents.dsh_reasoning_levels(["light", "high", "extra_high"])
    check("light / extra_high 换成 low / xhigh",
          levels == {"low": "light", "high": "high", "xhigh": "extra_high"},
          levels)
    check("全是不认识的档位就不写",
          wb_agents.dsh_reasoning_levels(["ultra"]) is None)
    check("白名单原样保留",
          wb_agents.dsh_reasoning_levels({"off": "off", "max": "max"})
          == {"off": "off", "max": "max"})


def test_paths():
    print("patch 发现")
    home = tempfile.mkdtemp(prefix="wb-dsh-")
    profiles = os.path.join(home, ".dsh", "profiles")
    for name, text in (("desktop", BLOCK_PATCH), ("tui", FLOW_PATCH),
                       ("plain", "- id: ui-chat\n  config:\n    a: 1\n")):
        directory = os.path.join(profiles, name)
        os.makedirs(directory)
        with io.open(os.path.join(directory, "cordis.patch.yml"), "w",
                     encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    found = wb_agents._dsh_patch_paths(home)
    names = sorted(os.path.basename(os.path.dirname(p)) for p in found)
    check("只挑声明了 llm-pi-ai 的 patch", names == ["desktop", "tui"], names)
    files = wb_agents.CLIENTS["dsh"]["files"](home)
    check("文件表带上 patch",
          sorted(k for k in files if k.startswith("patch:"))
          == ["patch:desktop", "patch:tui"], sorted(files))
    plan = wb_agents.plan("dsh", "http://127.0.0.1:8788", "wb-test",
                          model="route/auto", models=PROVIDER["models"],
                          home=home)
    touched = {os.path.basename(item["path"]) for item in plan["files"]}
    check("dry run 带上 patch", "cordis.patch.yml" in touched, touched)


test_block()
test_flow()
test_default_entry_untouched()
test_projection_formatting()
test_reasoning_levels()
test_paths()

print()
if FAIL:
    print("FAILED: %d 项" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("全部通过")
