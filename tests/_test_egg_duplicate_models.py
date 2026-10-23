"""自检：同一个模型 id 被多个供应商池挑走时，清单与请求各走各的。

背景：一个模型 id 只能有一个去处（``?provider=egg`` 的清单按纯 id 给客户端
挑）。但「路由」页的公开 id 是 ``模型名 · 池名``，同一个 id 挂在两个供应商池
下本来就分得开：两个都要列出来，请求按后缀回到对应的池子，不能都发给先登记
的那一家。

    python tests/_test_egg_duplicate_models.py
"""

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_egg_pools as P
import wb_zen

# wb_proxy 导入时会按真实 accounts 目录 configure 一遍，测试要把它掰回临时
# 目录（只在进程内，不写真实文件）。
import wb_proxy as W

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


MODEL = "dup-model-x"
tmp = tempfile.mkdtemp(prefix="egg-dup-")
try:
    P.configure(tmp)
    W.ACCOUNTS_DIR = tmp
    pool_a = P.upsert({
        "name": "Dup A",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:18901/v1",
        "api_key": "sk-a",
        "models": [MODEL],
        "catalog": [{"id": MODEL, "name": "Dup Model X"}],
    })
    pool_b = P.upsert({
        "name": "Dup B",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:18902/v1",
        "api_key": "sk-b",
        "models": [MODEL],
        "catalog": [{"id": MODEL, "name": "Dup Model X"}],
    })

    # 1) 纯 id：先登记的池子赢（老客户端不带后缀时的行为不变）。
    first = P.find_by_model(MODEL)
    check("纯 id 命中先登记的池子", first is not None
          and first["id"] == pool_a["id"], first)
    check("纯 id 请求发给 A",
          wb_zen.build_request({"model": MODEL})[0]
          == "http://127.0.0.1:18901/v1/chat/completions")

    # 2) 带池名：以后缀为准，同名模型互不顶掉。
    picked = P.find_by_model(MODEL, "Dup B")
    check("池名命中 B", picked is not None and picked["id"] == pool_b["id"],
          picked)
    url_b, body_b, headers_b = wb_zen.build_request(
        {"model": MODEL}, pool_label="Dup B")[:3]
    check("B 后缀发给 B", url_b == "http://127.0.0.1:18902/v1/chat/completions",
          url_b)
    check("B 用的是 B 的 Key",
          headers_b.get("authorization") == "Bearer sk-b", headers_b)
    url_a = wb_zen.build_request({"model": MODEL}, pool_label="Dup A")[0]
    check("A 后缀发给 A", url_a == "http://127.0.0.1:18901/v1/chat/completions",
          url_a)
    resolved = wb_zen.resolve(MODEL, "Dup B")
    check("resolve 认 B 池", resolved is not None
          and resolved[0] == pool_b["id"], resolved)

    # 3) 两份清单：鸡蛋视图去重，路由统一清单两条都列。
    egg_entries = P.catalog_entries()
    check("鸡蛋清单只留一条", len(egg_entries) == 1, egg_entries)
    all_entries = P.catalog_entries(dedupe=False)
    check("全量清单两条", len(all_entries) == 2, len(all_entries))
    check("两条来源不同",
          len(set(e.get("source") for e in all_entries)) == 2, all_entries)
    check("wb_zen 全量清单两条", len(wb_zen.pool_models_all()) == 2,
          len(wb_zen.pool_models_all()))

    public = [pub for pub, meta in W._build_pool_entries()
              if meta.get("pool_model") == MODEL]
    check("路由清单两条公开 id", len(public) == 2, public)
    check("两条公开 id 不同", len(set(public)) == 2, public)
    check("公开 id 带各自的池名",
          any(p.endswith("· Dup A") for p in public)
          and any(p.endswith("· Dup B") for p in public), public)

    # 4) 停用一个池子后，它的后缀不再接流量（回落先登记的那家）。
    P.set_enabled(pool_b["id"], False)
    url_off = wb_zen.build_request({"model": MODEL}, pool_label="Dup B")[0]
    check("B 停用后后缀回落 A",
          url_off == "http://127.0.0.1:18901/v1/chat/completions", url_off)
    check("停用后全量清单只剩一条",
          len(P.catalog_entries(dedupe=False)) == 1,
          P.catalog_entries(dedupe=False))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
