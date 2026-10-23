# -*- coding: utf-8 -*-
"""总路由池子 → 已接入 Agent 的自动同步自检。

    python tests/_test_pool_agent_sync.py

覆盖：
  - 清单指纹稳定；清单没变时一个字节都不写
  - 清单变了（多了一条模型）时自动把新表写进 OpenCode 配置
  - 池子写进客户端配置时不带 X-Provider（靠 id 后缀路由）
  - 被手工改过（external_change）的客户端跳过，不覆盖用户改动
  - 只钉了某一家（provider=workbuddy）的客户端跳过

全部在临时目录里跑，不碰本机真实的 accounts / 客户端配置。
"""

import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_agents
import wb_proxy as P

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def read_json(path):
    with io.open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def models_a():
    return [
        {"id": "deepseek-v4.1-flash · WorkBuddy-global",
         "name": "deepseek-v4.1-flash", "source": "workbuddy",
         "source_label": "WorkBuddy-global", "pool_label": "WorkBuddy-global"},
        {"id": "glm-5.2 · Qoder-cn", "name": "glm-5.2", "source": "qoder",
         "source_label": "Qoder-cn", "pool_label": "Qoder-cn"},
    ]


def models_b():
    out = models_a()
    out.append({"id": "kimi-k2.7 · OpenRouter", "name": "kimi-k2.7",
                "source": "egg", "source_label": "OpenRouter",
                "pool_label": "OpenRouter"})
    return out


TMP = tempfile.mkdtemp(prefix="wb-pool-agent-")
HOME = os.path.join(TMP, "home")
os.makedirs(HOME)
_real_home = wb_agents._resolve_home
wb_agents._resolve_home = lambda h=None: os.path.abspath(h or HOME)
_real_accounts = P.ACCOUNTS_DIR
_real_key = P.API_KEY
P.API_KEY = "wb-test-key"


def fresh_scope(name):
    accounts = os.path.join(TMP, name)
    os.makedirs(accounts, exist_ok=True)
    P.ACCOUNTS_DIR = accounts
    P._pool_agent_sig = None
    return accounts


CONFIG = os.path.join(HOME, ".config", "opencode", "opencode.json")
BASE = "http://127.0.0.1:8788"

try:
    print("池子清单构建（agent_models / 面板入口同源）")
    _real_fetch_pool = P.fetch_pool_models
    P.fetch_pool_models = lambda realm=None: [
        (m["id"], dict(m)) for m in models_a()]
    try:
        built = P.agent_models(pool=True)
        check("池子清单带路由控制模型",
              bool(built) and built[0].get("id") == "route/auto",
              [b.get("id") for b in built][:3])
        check("面板入口走同一个实现",
              P.Handler._agents_models(None, pool=True) == built)
        check("每条都带 source_label",
              all(b.get("source_label") for b in built), built[:2])
    finally:
        P.fetch_pool_models = _real_fetch_pool

    print("清单指纹")
    check("同一份清单指纹稳定",
          P.pool_agent_signature(models_a()) == P.pool_agent_signature(models_a()))
    check("多一条模型指纹就变",
          P.pool_agent_signature(models_a()) != P.pool_agent_signature(models_b()))

    print("同步：清单没变不写盘")
    accounts = fresh_scope("accounts1")
    wb_agents.integrate(accounts, "opencode", BASE, "wb-test-key",
                        models=models_a(), provider=None)
    cfg = read_json(CONFIG)
    node = cfg["provider"]["wb-proxy"]
    check("apply 写入池子 id", set(node.get("models") or {}) ==
          {m["id"] for m in models_a()}, list(node.get("models") or {}))
    check("池子不写 X-Provider",
          "X-Provider" not in ((node.get("options") or {}).get("headers") or {}),
          node.get("options"))

    out = P.sync_pool_agents(models=models_a(), reason="test")
    check("首次同步认下指纹", out.get("changed") is True and
          "opencode" in (out.get("synced") or []), out)
    out = P.sync_pool_agents(models=models_a(), reason="test")
    check("指纹没变不写盘", out.get("changed") is False and
          not out.get("synced"), out)

    print("同步：清单变化 → 客户端跟着换")
    out = P.sync_pool_agents(models=models_b(), reason="test")
    cfg = read_json(CONFIG)
    kept = set((cfg["provider"]["wb-proxy"].get("models") or {}))
    check("新模型写进去", "kimi-k2.7 · OpenRouter" in kept, sorted(kept))
    check("旧模型没被丢", "glm-5.2 · Qoder-cn" in kept, sorted(kept))
    check("同步报告客户端", "opencode" in (out.get("synced") or []), out)

    print("同步：手改过的客户端跳过")
    cfg = read_json(CONFIG)
    cfg["provider"]["wb-proxy"]["models"] = {"hand-edited": {"name": "hand edited"}}
    write_json(CONFIG, cfg)
    out = P.sync_pool_agents(models=models_a(), reason="test")
    cfg = read_json(CONFIG)
    check("跳过并记录", "opencode" in (out.get("skipped") or []), out)
    check("没有覆盖用户改动",
          set(cfg["provider"]["wb-proxy"].get("models") or {}) == {"hand-edited"},
          list(cfg["provider"]["wb-proxy"].get("models") or {}))

    print("同步：只钉某一家（provider=qoder）的客户端不动")
    accounts = fresh_scope("accounts2")
    wb_agents.integrate(accounts, "opencode", BASE, "wb-test-key",
                        models=[{"id": "deepseek-v4.1-flash"}],
                        provider="qoder")
    cfg = read_json(CONFIG)
    check("apply 钉了 X-Provider",
          (cfg["provider"]["wb-proxy"].get("options") or {})
          .get("headers", {}).get("X-Provider") == "qoder",
          (cfg["provider"]["wb-proxy"].get("options") or {}).get("headers"))
    out = P.sync_pool_agents(models=models_b(), reason="test")
    cfg = read_json(CONFIG)
    check("provider 记录可见",
          wb_agents.overview(accounts)["opencode"]["applied"]["provider"] == "qoder")
    check("固定某一家时跳过", "opencode" in (out.get("skipped") or []), out)
    check("配置没被池子改写",
          set(cfg["provider"]["wb-proxy"].get("models") or {}) ==
          {"deepseek-v4.1-flash"},
          list(cfg["provider"]["wb-proxy"].get("models") or {}))
finally:
    wb_agents._resolve_home = _real_home
    P.ACCOUNTS_DIR = _real_accounts
    P.API_KEY = _real_key

print()
if FAIL:
    print("FAILED: %d 项" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("全部通过")
