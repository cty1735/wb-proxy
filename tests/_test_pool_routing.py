"""「路由」池子与路由策略自检。

池子的公开 id 是 ``模型名 · 供应商``：
  - WorkBuddy / Qoder / Trae 用网关名 + global / cn（WorkBuddy-global、Qoder-cn）
  - 鸡蛋用供应商 / 池子名（OpenRouter、Zen 免费车道）
``split_pool_model`` 必须能从后缀反解回 (provider, realm)，且后缀优先于
``?provider=`` / ``X-Provider`` —— 点顶部哪家视图都不该改变它的去向。

路由策略（参考 magpie 的路由组）：smart / order / rotate / usage / pace。
这里只验证选路函数本身：候选非空、排序稳定、轮换会推进、状态结构完整。

    python tests/_test_pool_routing.py
"""

import os
import shutil
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_proxy as P
import wb_settings

# 策略配置写到临时目录：自检不该动操作者真实的 settings.json。
TMP_ACCOUNTS = tempfile.mkdtemp(prefix="wb-pool-routing-")
REAL_ACCOUNTS = P.ACCOUNTS_DIR
try:
    src = os.path.join(REAL_ACCOUNTS, "settings.json")
    if os.path.isfile(src):
        shutil.copy(src, os.path.join(TMP_ACCOUNTS, "settings.json"))
except Exception:
    pass
P.ACCOUNTS_DIR = TMP_ACCOUNTS

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def report(name, detail):
    print("  ..   %s %s" % (name, detail))


print("池子 id 的拼装 / 反解")
check("WorkBuddy 国际版后缀", P.pool_suffix("workbuddy", "intl") == "WorkBuddy-global")
check("Qoder 国内版后缀", P.pool_suffix("qoder", "cn") == "Qoder-cn")
check("Trae 国际版后缀", P.pool_suffix("trae", "intl") == "Trae-global")
check("鸡蛋用供应商名", P.pool_suffix("egg", None, "OpenRouter") == "OpenRouter")

public = P.pool_public_id("MiniMax-M3.1-Flash", "egg", None, "OpenRouter")
check("公开 id 形状", public == "MiniMax-M3.1-Flash · OpenRouter", public)
parsed = P.split_pool_model(public)
check("鸡蛋反解 provider", parsed and parsed["provider"] == "egg", parsed)
check("鸡蛋反解基础 id", parsed and parsed["model"] == "MiniMax-M3.1-Flash", parsed)

public = P.pool_public_id("deepseek-v4.1-flash", "workbuddy", "intl")
parsed = P.split_pool_model(public)
check("WorkBuddy 反解 provider", parsed and parsed["provider"] == "workbuddy", parsed)
check("WorkBuddy 反解 realm", parsed and parsed["realm"] == "intl", parsed)

public = P.pool_public_id("x", "trae", "cn")
parsed = P.split_pool_model(public)
check("Trae 反解 realm", parsed and parsed["realm"] == "cn", parsed)

check("普通 id 不被误判", P.split_pool_model("deepseek-v4.1-flash") is None)
# 鸡蛋的自建池名是操作者自己起的，没法预先枚举：未知后缀按鸡蛋供应商名处理，
# 基础 id 是否真的属于鸡蛋由 wb_zen.resolve 再判一次。
unknown = P.split_pool_model("foo · 不存在的网关")
check("未知后缀按鸡蛋供应商处理",
      unknown and unknown["provider"] == "egg"
      and unknown["label"] == "不存在的网关", unknown)

print("后缀优先于显式提示")
parsed = P.split_pool_model(P.pool_public_id("m", "qoder", "cn"))
check("池子 id 自带归属", P.resolve_chat_provider(parsed and
      (parsed["model"] + " · Qoder-cn"), "workbuddy") == "qoder")
check("普通 id 仍听显式提示",
      P.resolve_chat_provider("deepseek-v4.1-flash", "egg") == "egg")

print("路由控制模型")
check("route/auto", P.routing_strategy_of("route/auto") == "auto")
check("route/smart", P.routing_strategy_of("route/smart") == "smart")
check("route/pace", P.routing_strategy_of("route/pace") == "pace")
check("普通模型不是路由模型", P.routing_strategy_of("deepseek-v4.1-flash") is None)
ids = [m["id"] for m in P.routing_model_entries()]
check("控制模型齐全", ids == ["route/auto", "route/smart", "route/order",
                              "route/rotate", "route/usage", "route/pace"], ids)

print("候选与排序")
cands = P._routing_candidates()
report("候选数", len(cands))
check("候选非空", bool(cands))
check("候选都有 provider", all(c.get("provider") for c in cands))
ordered = P._routing_rank(cands, "order", advance=False)
check("order 保持池子顺序", [c["public"] for c in ordered] ==
      [c["public"] for c in cands])

with P._routing_lock:
    P._routing_rotate["n"] = 0
first = P._routing_rank(cands, "rotate")
second = P._routing_rank(cands, "rotate")
if len(cands) > 1:
    check("rotate 每轮换一个", first[0]["public"] != second[0]["public"],
          (first[0]["public"], second[0]["public"]))
else:
    report("rotate", "只有一个候选，跳过轮换断言")

for strategy in ("smart", "usage", "pace"):
    ranked = P._routing_rank(cands, strategy, advance=False)
    check("%s 不丢候选" % strategy, len(ranked) == len(cands))

print("策略配置")
original = wb_settings.routing_config(P.ACCOUNTS_DIR)
try:
    saved = wb_settings.set_routing_config(P.ACCOUNTS_DIR, {"strategy": "pace"})
    check("写入 pace", saved["strategy"] == "pace", saved)
    saved = wb_settings.set_routing_config(P.ACCOUNTS_DIR, {"strategy": "bogus"})
    check("未知策略回落 smart", saved["strategy"] == "smart", saved)
finally:
    wb_settings.set_routing_config(P.ACCOUNTS_DIR, original)

print("状态结构")
status = P.routing_status()
check("有 config", isinstance(status.get("config"), dict))
check("五种策略", [s["id"] for s in status.get("strategies") or []]
      == list(P.ROUTING_STRATEGIES))
check("有候选预览", isinstance(status.get("models"), list))
check("有决策列表", isinstance(status.get("decisions"), list))
# 明细弹窗要看完整排名：full=True 回全部候选，默认预览仍是前 40 条且顺序一致。
full = P.routing_status(full=True)
check("预览最多 40 条", len(status.get("models") or []) <= 40,
      len(status.get("models") or []))
check("full 给完整排名",
      len(full.get("models") or []) == full.get("candidates"),
      (len(full.get("models") or []), full.get("candidates")))
check("full 与预览前 40 条一致",
      [m["model"] for m in (full.get("models") or [])[:40]]
      == [m["model"] for m in (status.get("models") or [])],
      "")

print("池子按账号过滤")
real_pool = P.POOL
real_cache = P._pool_cache
try:
    raw = P._build_pool_entries()
    check("未过滤的池子有三个账号网关",
          {meta.get("pool_provider") for _mid, meta in raw}
          >= {"workbuddy", "qoder", "trae"},
          sorted({meta.get("pool_provider") for _mid, meta in raw}))
    # 用一份新鲜缓存拦掉后台刷新：这里只验证「取清单时按当前账号过滤」。
    P._pool_cache = {"at": time.time(), "data": raw}

    P.POOL = types.SimpleNamespace(accounts=[])
    providers = {meta.get("pool_provider")
                 for _mid, meta in P.fetch_pool_models()}
    check("空账号池只留鸡蛋", providers == {"egg"}, sorted(providers))

    P.POOL = types.SimpleNamespace(accounts=[
        types.SimpleNamespace(provider="workbuddy", realm="cn")])
    entries = P.fetch_pool_models()
    realms = {(meta.get("pool_provider"), meta.get("pool_realm"))
              for _mid, meta in entries
              if meta.get("pool_provider") == "workbuddy"}
    check("只有国内账号时只留国内模型", realms == {("workbuddy", "cn")},
          sorted(realms))
    check("有账号后鸡蛋仍然保留",
          any(meta.get("pool_provider") == "egg"
              for _mid, meta in entries), "")
finally:
    P.POOL = real_pool
    P._pool_cache = real_cache

route = P.resolve_routing_model("order")
check("order 能选出成员", bool(route and route.get("public")), route)
if route:
    back = P.split_pool_model(route["public"])
    check("选出的成员能反解", bool(back), route)
    P.record_routing_decision("route/auto", route)
    latest = P.routing_status().get("decisions") or []
    check("决策进入缓冲", latest and latest[0].get("requested") == "route/auto",
          latest[:1])

P.ACCOUNTS_DIR = REAL_ACCOUNTS
shutil.rmtree(TMP_ACCOUNTS, ignore_errors=True)

if FAIL:
    print("\nFAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("\nALL PASS")
