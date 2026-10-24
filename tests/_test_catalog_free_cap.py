"""目录成员、限时免费与「刷新全部模型清单」任务状态自检。

覆盖这次修复的四件事：
  - merge_catalog 以 live 目录决定成员：上游删掉的模型刷新后不能靠内置快照复活；
  - 过期免费活动不再对外宣告 x0.00，按付费兄弟模型的倍率回落；
  - 免费额度（6004）用尽时 model_entry 带上 free_cap_* 字段供面板换徽章；
  - refresh_pool_catalog 是带任务 id 的后台状态机，七份目录逐份记录结果。

    python tests/_test_catalog_free_cap.py
"""

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_catalog
import wb_proxy as P

# 自检不读写操作者真实的 accounts / settings。
P.ACCOUNTS_DIR = tempfile.mkdtemp(prefix="wb-catalog-free-cap-")

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


print("merge_catalog：live 目录决定成员")
old_intl = wb_catalog.STATIC_INTL_MODELS
try:
    wb_catalog.STATIC_INTL_MODELS = [
        {"id": "retired-model", "credits": "x0.11"},
        {"id": "kept-model", "credits": "x0.22",
         "reasoning": {"supportedEfforts": ["low", "high"]}},
    ]
    live = [("kept-model", {"credits": "x0.33"}),
            ("brand-new", {"credits": "x0.44"})]
    merged = P.merge_catalog(live, realm="intl", extras=True)
    ids = [mid for mid, _meta in merged]
    meta = dict(merged)
    check("上游已删除的模型不复活", "retired-model" not in ids, ids)
    check("仍在 live 的模型保留", "kept-model" in ids, ids)
    check("live 新增模型进清单", "brand-new" in ids, ids)
    check("live 倍率覆盖内置快照",
          meta.get("kept-model", {}).get("credits") == "x0.33",
          meta.get("kept-model", {}).get("credits"))
    check("内置快照仍补 reasoning 字段",
          meta.get("kept-model", {}).get("reasoning", {})
          .get("supportedEfforts") == ["low", "high"],
          meta.get("kept-model", {}).get("reasoning"))
finally:
    wb_catalog.STATIC_INTL_MODELS = old_intl

print("过期免费活动：改用付费兄弟的倍率")
now = time.time()
meta = {
    "deepseek-v4.1-flash": {
        "credits": "x0.00",
        "_wb_promo": {"until": now - 3600, "id": "promo-1"},
        "_wb_rate_cap": {"paid_id": "deepseek-v4.1-flash-sg",
                         "allow_paid": True},
    },
    "deepseek-v4.1-flash-sg": {"credits": "x0.03"},
}
P._apply_expired_promotions(meta, now=now)
base = meta["deepseek-v4.1-flash"]
check("过期后不再是 x0.00", base.get("credits") == "x0.03", base.get("credits"))
check("记下回落模型", base.get("free_cap_paid_id")
      == "deepseek-v4.1-flash-sg", base.get("free_cap_paid_id"))
check("记下回落倍率", base.get("free_cap_paid_rate") == "x0.03",
      base.get("free_cap_paid_rate"))
check("credits_note 说明活动已结束",
      "活动已于" in str(base.get("credits_note") or ""),
      base.get("credits_note"))

print("curate_remote_catalog：活动期标注与付费变体")
active = {
    "deepseek-v4.1-flash": {
        "credits": "x0.00",
        "_wb_promo": {"until": now + 3600, "id": "promo-2"},
        "_wb_rate_cap": {"paid_id": "deepseek-v4.1-flash-sg",
                         "allow_paid": True},
    },
    "deepseek-v4.1-flash-sg": {"credits": "x0.03"},
}
ids = P.curate_remote_catalog(
    "intl", ["deepseek-v4.1-flash", "deepseek-v4.1-flash-sg"], active)
check("免费 base 在时隐藏付费变体",
      ids == ["deepseek-v4.1-flash"], ids)
active_base = active["deepseek-v4.1-flash"]
check("活动期标注截止时间",
      "限时免费至" in str(active_base.get("credits_note") or ""),
      active_base.get("credits_note"))
check("活动期注明额度用尽后的回落模型",
      "回落至 deepseek-v4.1-flash-sg（x0.03）"
      in str(active_base.get("credits_note") or ""),
      active_base.get("credits_note"))
check("活动期也带回落倍率",
      active_base.get("free_cap_paid_rate") == "x0.03",
      active_base.get("free_cap_paid_rate"))
ids = P.curate_remote_catalog(
    "intl", ["deepseek-v4.1-flash-sg"],
    {"deepseek-v4.1-flash-sg": {"credits": "x0.03"}})
check("base 下线后保留付费变体",
      ids == ["deepseek-v4.1-flash-sg"], ids)


class FakeAccount(object):
    realm = "intl"
    enabled = True
    access_token = "token"
    provider = "workbuddy"

    def __init__(self, reset):
        self.reset = reset

    def cap_until(self, model, now=None):
        return self.reset if self.reset > (now or time.time()) else 0.0


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = accounts


print("免费额度（6004）：运行时状态")
old_pool = P.POOL
try:
    P.POOL = FakePool([FakeAccount(now + 600), FakeAccount(now + 1200)])
    status = P.free_model_cap_status("intl", "deepseek-v4.1-flash", now=now)
    check("所有账号都在 cap 里 -> 用尽", status.get("exhausted") is True, status)
    check("恢复时间取最早的那个", int(status.get("reset") or 0) == int(now + 600),
          status.get("reset"))
    check("记录参与判断的账号数", status.get("accounts") == 2, status.get("accounts"))

    P.POOL = FakePool([FakeAccount(now + 600), FakeAccount(0)])
    check("还有一个账号可用 -> 不算用尽",
          P.free_model_cap_status("intl", "deepseek-v4.1-flash", now=now) == {})

    P.POOL = FakePool([FakeAccount(now + 600), FakeAccount(now + 1200)])
    entry = P.model_entry("deepseek-v4.1-flash", {
        "credits": "x0.00",
        "free_cap_paid_id": "deepseek-v4.1-flash-sg",
        "free_cap_paid_rate": "x0.03",
        "_wb_realm": "intl",
    })
    check("model_entry 带 free_cap_exhausted",
          entry.get("free_cap_exhausted") is True, entry.get("free_cap_exhausted"))
    check("model_entry 带恢复时间",
          int(entry.get("free_cap_reset") or 0) == int(now + 600),
          entry.get("free_cap_reset"))
    check("model_entry 带回落倍率",
          entry.get("free_cap_paid_rate") == "x0.03",
          entry.get("free_cap_paid_rate"))
finally:
    P.POOL = old_pool

print("refresh_pool_catalog：后台任务状态")
old_refresh = P.refresh_catalog
old_kick = P.kick_pool_agent_sync
calls = []


def fake_refresh(realm, provider=None):
    calls.append((provider, realm))
    time.sleep(0.02)
    return {"ok": True, "realm": realm, "count": 3, "msg": "ok"}


def wait_done(timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = P.pool_catalog_refresh_state()
        if not state.get("running"):
            return state
        time.sleep(0.02)
    return P.pool_catalog_refresh_state()


try:
    P.refresh_catalog = fake_refresh
    P.kick_pool_agent_sync = lambda *args, **kwargs: None
    first = P.refresh_pool_catalog()
    check("POST 立即返回任务 id", bool(first.get("id")), first)
    check("POST 返回 running 状态", first.get("running") is True, first)
    second = P.refresh_pool_catalog()
    check("重复点击复用同一个任务", second.get("id") == first.get("id"),
          (first.get("id"), second.get("id")))

    state = wait_done()
    check("任务结束后 running=false", state.get("running") is False, state)
    check("七份目录都有结果", len(state.get("results") or []) == 7,
          len(state.get("results") or []))
    check("七份目录全部成功", state.get("ok_count") == 7
          and state.get("failed_count") == 0, state)
    check("任务结果 success=true", state.get("success") is True, state)
    check("没有重复触发后台刷新", len(calls) == 7, calls)
    check("目录顺序：三家 × 两个出口 + 鸡蛋",
          calls[:6] == [("workbuddy", "intl"), ("workbuddy", "cn"),
                        ("qoder", "intl"), ("qoder", "cn"),
                        ("trae", "intl"), ("trae", "cn")]
          and calls[6][0] == "egg", calls)
    check("结束消息带 7/7", "7/7" in str(state.get("msg") or ""),
          state.get("msg"))

    def flaky_refresh(realm, provider=None):
        time.sleep(0.01)
        if provider == "qoder" and realm == "cn":
            return {"ok": False, "realm": realm, "msg": "上游没返回"}
        return {"ok": True, "realm": realm, "count": 1, "msg": "ok"}

    P.refresh_catalog = flaky_refresh
    P.refresh_pool_catalog()
    state = wait_done()
    check("失败份数被记下", state.get("failed_count") == 1, state)
    check("有失败时 success=false", state.get("success") is False, state)
    check("失败消息点名目录", "qoder/cn" in str(state.get("msg") or ""),
          state.get("msg"))
finally:
    P.refresh_catalog = old_refresh
    P.kick_pool_agent_sync = old_kick

if FAIL:
    print("\nFAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
print("\nALL PASS")
