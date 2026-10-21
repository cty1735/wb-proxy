"""四个网关的隔离自检：每个模型都过一遍归属判定。

背景：WorkBuddy / Qoder / Trae / 鸡蛋（免费网关）是四个独立的上游，模型和
请求必须互不影响。以前归属判定只挡了 Qoder / Trae：

  - 鸡蛋的供应商池绕过去了。华为云 CodeArts 池里有一个 deepseek-v4.1-flash，
    和 WorkBuddy 目录同名，于是 WorkBuddy 的请求被静默送到池子上——用户看到
    的是池子的报错（benefit not found），不是 WorkBuddy 的。
  - Trae 的别名表是子串匹配的，deepseek-v4-flash-free 这类免费车道的 id 也
    被算成 Trae 的，鸡蛋的流量漏到 Trae。

判定顺序固定为 WorkBuddy -> Qoder -> Trae -> 鸡蛋，一个 id 只属于一家；
显式提示（?provider= / X-Provider）仍然最高优先，所以配了哪家的客户端照样
直走那家。

另外顺带自检「上游在 200 的流里回错误」的识别（stream_error_payload）——
那种块没有 choices，透传下去客户端只会看到「流结束了却没有 finish_reason」。

    python tests/_test_provider_isolation.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_catalog
import wb_egg_pools
import wb_proxy as P
import wb_qoder
import wb_trae
import wb_zen

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def report(name, detail):
    print("  ..   %s %s" % (name, detail))


# ---------------------------------------------------------------------------
# 每家的模型清单
# ---------------------------------------------------------------------------

def ids_of(entries):
    out = set()
    for item in entries or []:
        if isinstance(item, dict):
            for key in ("id", "key"):
                mid = str(item.get(key) or "").strip()
                if mid:
                    out.add(mid)
        elif isinstance(item, str) and item.strip():
            out.add(item.strip())
    return out


def catalogs(accounts_dir=None):
    """每个提供商的模型 id 集合（静态快照 + 磁盘上的供应商池）。"""
    cats = {
        "workbuddy": ids_of(wb_catalog.STATIC_INTL_MODELS)
        | ids_of(wb_catalog.STATIC_CN_MODELS)
        | ids_of(wb_catalog.STATIC_MODELS),
        "qoder": ids_of(wb_qoder.static_models("intl"))
        | ids_of(wb_qoder.static_models("cn")),
        "trae": ids_of(wb_trae.static_models("intl"))
        | ids_of(wb_trae.static_models("cn")),
        "egg": ids_of(wb_zen._static_models()),
    }
    if accounts_dir and os.path.isdir(accounts_dir):
        try:
            wb_egg_pools.configure(accounts_dir)
            for entry in wb_egg_pools.pools() or []:
                # 停用的池子不接流量（find_by_model 只索引启用的池子），
                # 它的模型不算「有人认领」。
                if not entry.get("enabled"):
                    continue
                cats["egg"] |= ids_of(entry.get("models") or [])
        except Exception as exc:
            report("供应商池读取失败", repr(exc))
    return cats


# ---------------------------------------------------------------------------
# 1. 归属判定：每个模型都过一遍
# ---------------------------------------------------------------------------

print("每个模型的归属判定")
cats = catalogs(os.path.join(ROOT, "accounts"))
for name, ids in cats.items():
    report(name, "%d 个模型" % len(ids))

# 一个 id 被几家认领：按固定顺序归第一家，其余仍然可以用显式提示走到。
owners = {}
for name, ids in cats.items():
    for mid in ids:
        owners.setdefault(mid, set()).add(name)

PRECEDENCE = ("workbuddy", "qoder", "trae", "egg")
shared = {mid: sorted(who, key=PRECEDENCE.index)
          for mid, who in owners.items() if len(who) > 1}
report("跨家同名", "%d 个 id 被多家认领" % len(shared))

leaked = []
misrouted = []
for mid, who in sorted(owners.items()):
    ordered = sorted(who, key=PRECEDENCE.index)
    got = P.resolve_chat_provider(mid)
    if got not in ordered:
        leaked.append((mid, ordered, got))
    elif ordered[0] != got:
        misrouted.append((mid, ordered, got))

check("没有模型被路由到不认领它的网关", not leaked,
      "\n        " + "\n        ".join(
          "%s 属于 %s，却被判给了 %s" % (m, w, g) for m, w, g in leaked[:20]))
check("同名 id 按固定顺序归第一家", not misrouted,
      "\n        " + "\n        ".join(
          "%s 属于 %s，却判给了 %s" % (m, w, g) for m, w, g in misrouted[:20]))
for mid, who in sorted(shared.items())[:12]:
    report("同名", "%s -> %s（认领方 %s）" % (mid, P.resolve_chat_provider(mid),
                                              ",".join(who)))

# ---------------------------------------------------------------------------
# 2. 具体回归：本次修掉的两个串流量
# ---------------------------------------------------------------------------

print("回归用例")
check("WorkBuddy 自己的 deepseek-v4.1-flash 不再被鸡蛋的池子抢走",
      P.resolve_chat_provider("deepseek-v4.1-flash") == "workbuddy",
      P.resolve_chat_provider("deepseek-v4.1-flash"))
check("WorkBuddy 自己的 glm-5.3 不再被鸡蛋的池子抢走",
      P.resolve_chat_provider("glm-5.3") == "workbuddy",
      P.resolve_chat_provider("glm-5.3"))
check("鸡蛋免费车道的 deepseek-v4-flash-free 不被 Trae 的别名表抢走",
      P.resolve_chat_provider("deepseek-v4-flash-free") == "egg",
      P.resolve_chat_provider("deepseek-v4-flash-free"))
check("鸡蛋免费车道的 deepseek-v4.1-flash-free 仍是鸡蛋的",
      P.resolve_chat_provider("deepseek-v4.1-flash-free") == "egg",
      P.resolve_chat_provider("deepseek-v4.1-flash-free"))
check("Trae 自己的模型仍归 Trae",
      P.resolve_chat_provider("mimo-v2.6-pro") == "trae",
      P.resolve_chat_provider("mimo-v2.6-pro"))

print("显式提示最高优先")
for provider in ("workbuddy", "qoder", "trae", "egg"):
    check("?provider=%s 直走那家" % provider,
          P.resolve_chat_provider("deepseek-v4.1-flash", provider) == provider,
          P.resolve_chat_provider("deepseek-v4.1-flash", provider))
check("显式提示能走到同名的那家（池子里的 deepseek-v4.1-flash）",
      P.resolve_chat_provider("deepseek-v4.1-flash", "egg") == "egg")

print("出口（国内 / 国际）也是各家自己的")
for provider in ("workbuddy", "qoder", "trae", "egg"):
    want = P.PROVIDER_REALMS.get(provider)
    check("%s 走自己的出口（不是全局当前出口）" % provider,
          P.provider_realm(provider) == want,
          "%s != %s" % (P.provider_realm(provider), want))
check("显式的出口提示最高优先",
      P.provider_realm("qoder", "cn") == "cn", P.provider_realm("qoder", "cn"))
check("没有提示时用的是该提供商记着的出口，不是全局当前出口",
      P.provider_realm("qoder") == P.PROVIDER_REALMS["qoder"],
      (P.provider_realm("qoder"), P.PROVIDER_REALMS["qoder"]))

# ---------------------------------------------------------------------------
# 3. 上游在 200 的流里回错误：认出来
# ---------------------------------------------------------------------------

print("带内错误识别（上游 200 + 错误对象）")
codearts = {
    "error_code": "InferHub.4004.200",
    "error_msg": "benefit not found",
    "details": [{"error_code": "InferHub.4004.200", "error_msg": "benefit not found"}],
}
err = P.stream_error_payload(codearts)
check("认得出 CodeArts 的 benefit 错误", err is not None, err)
if err:
    check("错误码是 InferHub.4004.200", err[0] == "InferHub.4004.200", err)
    check("错误文案是 benefit not found", err[1] == "benefit not found", err)

err = P.stream_error_payload({"error": {"message": "quota exhausted",
                                        "type": "insufficient_quota"}})
check("认得出 OpenAI 形状的 error", err is not None, err)
if err:
    check("OpenAI 形状的文案取自 message",
          err[1] == "quota exhausted", err)

check("正常块不算错误",
      P.stream_error_payload({"choices": [{"index": 0, "delta": {"content": "hi"}}]}) is None)
check("空 choices 也不算错误",
      P.stream_error_payload({"choices": []}) is None)
check("error 为 null 不算错误",
      P.stream_error_payload({"error": None}) is None)
check("非字典不算错误", P.stream_error_payload("boom") is None)

# ---------------------------------------------------------------------------
# 4. 供应商池里的模型仍可用（显式提示）
# ---------------------------------------------------------------------------

print("供应商池的模型（显式提示下仍可达）")
try:
    pools = wb_egg_pools.pools() or []
except Exception:
    pools = []
for pool in pools:
    mids = ids_of(pool.get("models") or [])
    if not mids:
        continue
    hint = P.resolve_chat_provider(sorted(mids)[0], "egg")
    check("池子「%s」的模型用 ?provider=egg 仍走鸡蛋" % (pool.get("name") or "?"),
          hint == "egg", hint)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
