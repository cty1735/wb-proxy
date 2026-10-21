"""Claude Code 一键配置自检：wb_agents 的 claude-code 分支。

背景：看板的「智能体配置」里，Claude Code 卡片上的「一键配置」走的是
wb_agents._apply_claude_code——它把 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
写进 ~/.claude/settings.json 的 env 段；模型 id 跨提供商重名时还要靠
ANTHROPIC_CUSTOM_HEADERS 钉住提供商（X-Provider: qoder / trae）。

这里用临时 home + 临时 accounts 目录跑完整链路（plan → integrate → 还原），
不碰真实机器上的 ~/.claude/settings.json：

    python tests/_test_claude_code_agent.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_agents as A

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def make_env():
    """A throwaway home + accounts dir for one scenario."""
    root = tempfile.mkdtemp(prefix="wb-claude-")
    home = os.path.join(root, "home")
    accounts = os.path.join(root, "accounts")
    os.makedirs(home)
    os.makedirs(accounts)
    return root, home, accounts


def settings_path(home):
    return os.path.join(home, ".claude", "settings.json")


def read_settings(home):
    with open(settings_path(home), "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_settings(home, data):
    os.makedirs(os.path.dirname(settings_path(home)), exist_ok=True)
    with open(settings_path(home), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------------------
# 1. plan(): 一键配置到底会写出什么
# ---------------------------------------------------------------------------
print("plan() 写出的 settings.json")
root, home, accounts = make_env()
try:
    built = A.plan("claude-code", "http://127.0.0.1:8788/v1", "sk-test-key",
                   model="claude-opus-4.6", home=home, provider="workbuddy")
    check("只碰一个文件", len(built["files"]) == 1, repr(built["files"]))
    check("落在 ~/.claude/settings.json",
          built["files"][0]["path"] == settings_path(home),
          built["files"][0]["path"])
    data = json.loads(built["files"][0]["content"])
    env = data.get("env") or {}
    check("BASE_URL 去掉结尾的 /v1（Claude Code 自己会补）",
          env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8788",
          repr(env.get("ANTHROPIC_BASE_URL")))
    check("AUTH_TOKEN 写的是面板那把 Key",
          env.get("ANTHROPIC_AUTH_TOKEN") == "sk-test-key",
          repr(env.get("ANTHROPIC_AUTH_TOKEN")))
    check("选了模型就写 ANTHROPIC_MODEL",
          env.get("ANTHROPIC_MODEL") == "claude-opus-4.6",
          repr(env.get("ANTHROPIC_MODEL")))
    for key in ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        check("%s 一起钉住" % key, env.get(key) == "claude-opus-4.6",
              repr(env.get(key)))
    check("WorkBuddy 不写 X-Provider 头",
          "ANTHROPIC_CUSTOM_HEADERS" not in env,
          repr(env.get("ANTHROPIC_CUSTOM_HEADERS")))
finally:
    shutil.rmtree(root, ignore_errors=True)

print("provider 切换：模型 id 跨提供商重名时靠 X-Provider 钉住")
root, home, accounts = make_env()
try:
    built = A.plan("claude-code", "http://127.0.0.1:8788", "sk-k",
                   home=home, provider="qoder")
    env = json.loads(built["files"][0]["content"]).get("env") or {}
    check("Qoder 写 X-Provider: qoder",
          env.get("ANTHROPIC_CUSTOM_HEADERS") == "X-Provider: qoder",
          repr(env.get("ANTHROPIC_CUSTOM_HEADERS")))
    built = A.plan("claude-code", "http://127.0.0.1:8788", "sk-k",
                   home=home, provider="trae")
    env = json.loads(built["files"][0]["content"]).get("env") or {}
    check("Trae 写 X-Provider: trae",
          env.get("ANTHROPIC_CUSTOM_HEADERS") == "X-Provider: trae",
          repr(env.get("ANTHROPIC_CUSTOM_HEADERS")))
finally:
    shutil.rmtree(root, ignore_errors=True)

print("不覆盖用户自己写的东西")
root, home, accounts = make_env()
try:
    write_settings(home, {
        "env": {"MY_OWN": "1", "ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
        "permissions": {"allow": ["Bash(ls)"]},
    })
    built = A.plan("claude-code", "http://127.0.0.1:8788", "sk-k", home=home)
    data = json.loads(built["files"][0]["content"])
    env = data.get("env") or {}
    check("别人的 env 键还在", env.get("MY_OWN") == "1", repr(env))
    check("顶层字段还在", data.get("permissions") == {"allow": ["Bash(ls)"]},
          repr(data.get("permissions")))
    check("旧的 BASE_URL 被换成网关",
          env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8788",
          repr(env.get("ANTHROPIC_BASE_URL")))
finally:
    shutil.rmtree(root, ignore_errors=True)

# ---------------------------------------------------------------------------
# 2. integrate(): 真写文件 + 备份 + 状态
# ---------------------------------------------------------------------------
print("integrate()：写文件、留备份、记状态")
root, home, accounts = make_env()
try:
    original = json.dumps({"env": {"KEEP": "yes"}}, indent=2) + "\n"
    write_settings(home, {"env": {"KEEP": "yes"}})
    with open(settings_path(home), "r", encoding="utf-8") as fh:
        original = fh.read()
    result = A.integrate(accounts, "claude-code", "http://127.0.0.1:8788",
                         "sk-live", home=home, key_id="k1")
    check("文件已写入", os.path.isfile(settings_path(home)))
    env = read_settings(home).get("env") or {}
    check("文件里是网关地址", env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8788",
          repr(env.get("ANTHROPIC_BASE_URL")))
    check("别人的 env 键还在", env.get("KEEP") == "yes", repr(env))
    state = json.load(open(os.path.join(accounts, A.STATE_FILE), encoding="utf-8"))
    rec = state.get("claude-code") or {}
    check("状态里有记录", bool(rec), repr(state.keys()))
    check("记了用的 Key", rec.get("key_id") == "k1", repr(rec.get("key_id")))
    backup = (rec.get("files") or [{}])[0].get("backup")
    check("留了备份", bool(backup) and os.path.isfile(backup), repr(backup))
    if backup and os.path.isfile(backup):
        with open(backup, "r", encoding="utf-8") as fh:
            check("备份是原始字节", fh.read() == original)

    over = A.overview(accounts, home=home)["claude-code"]
    check("看板视角：已安装", over["installed"] is True)
    check("看板视角：已配置", over["configured"] is True)
    check("看板视角：配置路径正确",
          over["config_paths"] == [settings_path(home)], repr(over["config_paths"]))
    check("看板视角：没被手改", over["applied"]["external_change"] is False)

    print("外部手改之后要能被认出来")
    edited = read_settings(home)
    edited["env"]["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9999"
    write_settings(home, edited)
    over = A.overview(accounts, home=home)["claude-code"]
    check("手改后 external_change 为真", over["applied"]["external_change"] is True,
          repr(over["applied"]))

    print("还原：把原始字节放回去")
    A.restore(accounts, "claude-code", home=home)
    with open(settings_path(home), "r", encoding="utf-8") as fh:
        check("文件回到原样", fh.read() == original)
    state = json.load(open(os.path.join(accounts, A.STATE_FILE), encoding="utf-8"))
    check("状态记录已清掉", "claude-code" not in state, repr(state.keys()))
finally:
    shutil.rmtree(root, ignore_errors=True)

print("重复应用：保留的是最早那份备份")
root, home, accounts = make_env()
try:
    write_settings(home, {"env": {"KEEP": "yes"}})
    A.integrate(accounts, "claude-code", "http://127.0.0.1:8788", "sk-1", home=home)
    A.integrate(accounts, "claude-code", "http://127.0.0.1:8788", "sk-2", home=home)
    state = json.load(open(os.path.join(accounts, A.STATE_FILE), encoding="utf-8"))
    rec = state["claude-code"]
    backup = rec["files"][0]["backup"]
    with open(backup, "r", encoding="utf-8") as fh:
        backed = fh.read()
    A.restore(accounts, "claude-code", home=home)
    with open(settings_path(home), "r", encoding="utf-8") as fh:
        check("还原后仍是最初那版", fh.read() == backed and "KEEP" in backed,
              repr(backed))
    env = read_settings(home).get("env") or {}
    check("最早的备份不含网关地址",
          "ANTHROPIC_BASE_URL" not in env, repr(env))
finally:
    shutil.rmtree(root, ignore_errors=True)

print("原来没有 settings.json：还原时删掉它")
root, home, accounts = make_env()
try:
    A.integrate(accounts, "claude-code", "http://127.0.0.1:8788", "sk-1", home=home)
    check("写出了文件", os.path.isfile(settings_path(home)))
    A.restore(accounts, "claude-code", home=home)
    check("文件被删掉", not os.path.exists(settings_path(home)))
    check("空的 .claude 目录也收掉", not os.path.isdir(os.path.join(home, ".claude")))
    over = A.overview(accounts, home=home)["claude-code"]
    check("还原后不再算已安装", over["installed"] is False, repr(over))
finally:
    shutil.rmtree(root, ignore_errors=True)

print("CLAUDE_CONFIG_DIR 覆盖默认路径")
root, home, accounts = make_env()
try:
    custom = os.path.join(root, "custom-claude")
    os.makedirs(custom)
    with open(os.path.join(custom, "settings.json"), "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = custom
    try:
        built = A.plan("claude-code", "http://127.0.0.1:8788", "sk-k", home=home)
        check("写的是覆盖目录里的文件",
              built["files"][0]["path"] == os.path.join(custom, "settings.json"),
              built["files"][0]["path"])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
print("ALL PASS")
