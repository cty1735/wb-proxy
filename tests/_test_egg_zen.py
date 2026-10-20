"""鸡蛋 + 自建供应商池的联调自检。"""

import os
import shutil
import sys
import tempfile

# 模块在仓库根目录：把根目录放进 sys.path 才能 import 到 wb_egg_pools。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_egg_pools as P
import wb_zen

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


tmp = tempfile.mkdtemp(prefix="eggzen-")
try:
    P.configure(tmp)
    pool = P.upsert({
        "name": "NVIDIA NIM",
        "protocol": "openai",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key": "nvapi-key",
        "models": ["deepseek-ai/deepseek-v4.1-flash"],
    })

    # 池子里的模型：resolve 要认出来
    resolved = wb_zen.resolve("deepseek-ai/deepseek-v4.1-flash")
    check("resolve 命中池子", resolved is not None
          and resolved[0] == pool["id"], resolved)

    # provider_hint：老客户端不带 ?provider= 也要走鸡蛋
    check("provider_hint 认池子模型",
          wb_zen.provider_hint("deepseek-ai/deepseek-v4.1-flash") == "egg")

    # 请求构造：OpenAI 兼容
    url, body, headers, rename, source, billed, sse = wb_zen.build_request({
        "model": "deepseek-ai/deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 256,
    })
    check("池子 URL", url == "https://integrate.api.nvidia.com/v1/chat/completions",
          url)
    check("池子 body model",
          body["model"] == "deepseek-ai/deepseek-v4.1-flash", body.get("model"))
    check("池子 body max_tokens", body["max_tokens"] == 256, body)
    check("池子带 Authorization",
          headers.get("authorization") == "Bearer nvapi-key", headers)
    check("池子不需要改名映射", rename == {}, rename)
    check("OpenAI 协议无 sse 转换器", sse is None)
    check("source 是池子 id", source == pool["id"], source)

    # Anthropic 协议
    P.upsert({
        "id": pool["id"], "name": "Anthropic 官方",
        "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "sk-ant-key",
        "models": ["claude-sonnet-4-5"],
    })
    url, body, headers, rename, source, billed, sse = wb_zen.build_request({
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "hi"}],
    })
    check("Anthropic URL", url == "https://api.anthropic.com/v1/messages", url)
    check("Anthropic 带 x-api-key", headers.get("x-api-key") == "sk-ant-key",
          headers)
    check("Anthropic 带版本头",
          headers.get("anthropic-version") == "2023-06-01", headers)
    check("Anthropic 有 sse 转换器", sse is not None)
    check("Anthropic body 是 messages 形状",
          isinstance(body.get("messages"), list)
          and body.get("model") == "claude-sonnet-4-5", body)

    # 清单条目：池子的模型要带池子归属
    entries = P.catalog_entries()
    check("池子清单条目", len(entries) == 1, entries)
    meta = wb_zen.model_entry_meta(entries[0])
    check("清单标出池子", meta["egg_source"] == pool["id"], meta)
    check("清单标签是池子名", meta["egg_source_label"] == "Anthropic 官方", meta)
    check("清单标出是自建池", meta["egg_source_kind"] == "pool", meta)

    # 内置源的条目不受影响
    zen_meta = wb_zen.model_entry_meta(wb_zen._zen_model("deepseek-v4.1-flash-free"))
    check("内置源标签不变",
          zen_meta["egg_source_label"] == "Zen 免费车道", zen_meta)
    check("内置源类型是 builtin", zen_meta["egg_source_kind"] == "builtin", zen_meta)
    check("内置源计费说明",
          "无需账号或密钥" in zen_meta["credits_note"], zen_meta)

    # 自建池优先：同一个 id 内置免费源也有时，清单里算自建池的
    # （请求本来就走自建池，见 resolve；否则池子那一行会被内置源吞掉，
    #  池子就永远不出现在「池子」下拉里）
    P.upsert({
        "name": "OpenRouter",
        "protocol": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-key",
        "models": ["minimax/minimax-m3.1-flash:free"],
    })
    builtin = wb_zen._static_models()
    check("内置源本来有同名模型",
          any(m["id"] == "minimax/minimax-m3.1-flash:free"
              and m.get("source") == "kilo" for m in builtin))
    merged = wb_zen.merge_models(builtin)
    same = [m for m in merged if m["id"] == "minimax/minimax-m3.1-flash:free"]
    check("同名只留一条", len(same) == 1, same)
    check("同名算自建池", same and same[0].get("sourceLabel") == "OpenRouter", same)
    check("自建池的模型进清单",
          sum(1 for m in merged if m.get("sourceLabel") == "OpenRouter") == 1)
    check("请求也走自建池",
          (wb_zen.resolve("minimax/minimax-m3.1-flash:free") or ("",))[0].startswith("pool-"),
          wb_zen.resolve("minimax/minimax-m3.1-flash:free"))

    # 免费模型：上游用 id 结尾的 :free / /free 标出来（OpenRouter 就是这个约定）
    free_entries = [e for e in P.catalog_entries()
                    if e["id"] == "minimax/minimax-m3.1-flash:free"]
    check("池子条目标出免费", free_entries and free_entries[0].get("free") is True,
          free_entries)
    paid_entries = [e for e in P.catalog_entries()
                    if e["id"] == "claude-sonnet-4-5"]
    check("付费条目不标免费", paid_entries and paid_entries[0].get("free") is False,
          paid_entries)
    free_meta = wb_zen.model_entry_meta(free_entries[0])
    check("清单元数据带免费标记", free_meta.get("egg_free") is True, free_meta)
    check("免费模型不说按供应商计费",
          "免费" in free_meta["credits_note"]
          and "费用由该供应商结算" not in free_meta["credits_note"], free_meta)
    paid_meta = wb_zen.model_entry_meta(paid_entries[0])
    check("付费模型没有免费标记", not paid_meta.get("egg_free"), paid_meta)
    check("付费模型说明按供应商计费",
          "费用由该供应商结算" in paid_meta["credits_note"], paid_meta)

    # 上游直接报 0 价也算免费（不靠 id 猜）
    zero_priced = P._clean_model("some/model", {"pricing": {"prompt": "0", "completion": "0"}})
    check("0 价算免费", zero_priced.get("free") is True, zero_priced)
    priced = P._clean_model("some/model", {"pricing": {"prompt": "0.001", "completion": "0"}})
    check("有价不算免费", priced.get("free") is False, priced)

    # 未知模型仍然报原来的错
    try:
        wb_zen.build_request({"model": "totally-unknown-model-xyz",
                              "messages": []})
        check("未知模型报错", False, "没有抛异常")
    except wb_zen.EggError as exc:
        check("未知模型报错", exc.status == 400, exc.detail)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
