"""鸡蛋模型清单必须秒回：客户端探 /v1/models 只给 5 秒。

背景：客户端（Hermes 等）在打开模型选择器时探网关的 /v1/models，超时就是
5 秒（快模式 1.5 秒）。而鸡蛋的免费源目录要拉 40 多秒（Zen + Kilo 两个
上游各 45 秒上限）。以前 /v1/models?provider=egg 会同步等这一轮刷新，
于是客户端每次探针都超时 → 面板切到鸡蛋后，客户端的模型列表一直停在上一
家提供商（面板自己看着正常，因为它走的是另一条路径）。

修法：目录过期但手上有一份可用目录时，先把手上这份交出去，刷新丢到后台；
缓存也只留 15 秒，后台刷新一落地，下一次请求就能看到新名单。

    python tests/_test_egg_models_fast.py
"""

import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_zen

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def slow_listing(*_args, **_kwargs):
    """假装上游很慢（比客户端的探针超时还长）。"""
    time.sleep(8)
    return []


tmp = tempfile.mkdtemp(prefix="eggfast-")
try:
    wb_zen.configure(tmp, log=lambda *_a, **_k: None)
    real_zen = wb_zen.fetch_zen_listing
    real_kilo = wb_zen.fetch_kilo_listing
    wb_zen.fetch_zen_listing = slow_listing
    wb_zen.fetch_kilo_listing = slow_listing

    # 1) 冷启动：没有目录 → 立刻给静态兜底清单，不等上游。
    started = time.time()
    snap = wb_zen.ensure(tmp)
    cold_elapsed = time.time() - started
    check("冷启动不等上游（< 1s）", cold_elapsed < 1.0, "%.2fs" % cold_elapsed)
    check("冷启动给的是静态兜底清单", len(snap.get("models") or []) > 0,
          len(snap.get("models") or []))
    check("冷启动的快照标成兜底", not snap.get("at"),
          snap.get("at"))

    # 2) 目录过期但手上有一份 → 立刻交出上一份，刷新在后台。
    with wb_zen._lock:
        wb_zen._state["models"] = [
            {"id": "beam-free", "name": "Beam", "source": "zen"},
        ]
        wb_zen._state["at"] = time.time() - (wb_zen.CATALOG_TTL_SECONDS + 60)
    started = time.time()
    snap = wb_zen.ensure(tmp)
    stale_elapsed = time.time() - started
    check("过期目录不等上游（< 1s）", stale_elapsed < 1.0, "%.2fs" % stale_elapsed)
    check("过期目录先给上一份",
          [m.get("id") for m in (snap.get("models") or [])] == ["beam-free"],
          snap.get("models"))
    check("过期目录的快照带着旧时间戳", snap.get("at"), snap.get("at"))

    # 3) 后台刷新真的在跑（不是把刷新丢了）。
    time.sleep(0.3)
    thread = wb_zen._warm_thread.get("thread")
    check("刷新被丢到后台线程", thread is not None and thread.is_alive(),
          thread)

    # 4) TTL 内：立刻返回，一秒都不等。
    with wb_zen._lock:
        wb_zen._state["at"] = time.time()
    started = time.time()
    wb_zen.ensure(tmp)
    fresh_elapsed = time.time() - started
    check("TTL 内立刻返回（< 0.5s）", fresh_elapsed < 0.5, "%.2fs" % fresh_elapsed)

    # 5) 网关这一层的缓存：过期目录只缓存 15 秒（后台刷新落地后立刻可见）。
    import wb_proxy as P
    key = P._catalog_cache_key("intl", "egg")
    with P._lock:
        P._models_cache.pop(key, None)
    with wb_zen._lock:
        wb_zen._state["models"] = [
            {"id": "beam-free", "name": "Beam", "source": "zen"},
        ]
        wb_zen._state["at"] = time.time() - (wb_zen.CATALOG_TTL_SECONDS + 60)
    started = time.time()
    entries = P._fetch_egg_models("intl")
    fetch_elapsed = time.time() - started
    check("网关取清单也不等上游（< 1s）", fetch_elapsed < 1.0,
          "%.2fs" % fetch_elapsed)
    check("网关拿到的是上一份目录",
          [mid for mid, _meta in entries] == ["beam-free"],
          [mid for mid, _meta in entries])
    with P._lock:
        ttl = (P._models_cache.get(key) or {}).get("ttl")
    check("过期目录的缓存只留 15 秒", ttl == 15, ttl)
finally:
    wb_zen.fetch_zen_listing = real_zen
    wb_zen.fetch_kilo_listing = real_kilo
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
