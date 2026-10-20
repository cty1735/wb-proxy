"""wb_egg_pools 的自检：池子的存取、模型清单挑选、Anthropic 协议转换。"""

import json
import os
import shutil
import sys
import tempfile

# 模块在仓库根目录：把根目录放进 sys.path 才能 import 到 wb_egg_pools。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_egg_pools as P

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


tmp = tempfile.mkdtemp(prefix="eggpool-")
try:
    P.configure(tmp)
    check("初始为空", P.pools() == [])

    pool = P.upsert({
        "name": "NVIDIA NIM",
        "protocol": "openai",
        "base_url": "https://integrate.api.nvidia.com/v1/",
        "api_key": "nvapi-abcdef123456",
        "models": ["deepseek-ai/deepseek-v4.1-flash"],
        "catalog": [{"id": "deepseek-ai/deepseek-v4.1-flash", "name": "DeepSeek"}],
    })
    check("池子已建", pool["id"].startswith("pool-"), pool["id"])
    check("Base URL 去掉尾斜杠",
          pool["base_url"] == "https://integrate.api.nvidia.com/v1",
          pool["base_url"])
    check("Key 掩码", P.mask_key("nvapi-abcdef123456") == "nvapi****3456",
          P.mask_key("nvapi-abcdef123456"))
    check("按模型找池子",
          (P.find_by_model("deepseek-ai/deepseek-v4.1-flash") or {}).get("id")
          == pool["id"])
    check("未挑的模型不接流量",
          P.find_by_model("some/other-model") is None)

    entries = P.catalog_entries()
    check("清单条目", len(entries) == 1 and entries[0]["sourceKind"] == "pool",
          entries)
    check("清单标签是池子名", entries[0]["sourceLabel"] == "NVIDIA NIM")

    # 落盘 / 读回
    P.configure(tmp)
    P._loaded = False
    P.configure(tmp)
    check("重启后读回", len(P.pools()) == 1, P.pools())

    # 编辑时留空 Key = 沿用旧的
    P.upsert({"id": pool["id"], "name": "NVIDIA NIM 2",
              "base_url": "https://integrate.api.nvidia.com/v1"})
    again = P.get(pool["id"])
    check("留空 Key 沿用旧的", again["api_key"] == "nvapi-abcdef123456",
          again["api_key"])
    check("改名生效", again["name"] == "NVIDIA NIM 2")

    # 只改挑选的模型
    P.set_models(pool["id"], ["a/one", "b/two"])
    check("挑选模型", P.get(pool["id"])["models"] == ["a/one", "b/two"])

    # 停用后不再接流量
    P.set_enabled(pool["id"], False)
    check("停用后不接流量",
          P.find_by_model("a/one") is None)
    P.set_enabled(pool["id"], True)
    check("启用后接流量", P.find_by_model("a/one") is not None)

    # 删除
    check("删除返回 True", P.delete(pool["id"]) is True)
    check("删除后为空", P.pools() == [])
    check("重复删除返回 False", P.delete(pool["id"]) is False)

    # --- Anthropic 请求体转换 ---
    chat = {
        "model": "claude-sonnet-4",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "get_weather",
                                          "arguments": "{\"city\":\"BJ\"}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "晴"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "get_weather", "description": "查天气",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}}}}}],
        "max_tokens": 1024,
        "temperature": 0.3,
    }
    body = P._anthropic_body(chat, "claude-sonnet-4")
    check("Anthropic body model", body["model"] == "claude-sonnet-4")
    check("Anthropic body system", body["system"] == "你是助手", body.get("system"))
    check("Anthropic body max_tokens", body["max_tokens"] == 1024)
    check("Anthropic body stream", body["stream"] is True)
    check("Anthropic 工具转 input_schema",
          body["tools"][0]["input_schema"]["properties"]["city"]["type"]
          == "string", body["tools"])
    check("Anthropic 工具结果块",
          body["messages"][-1]["content"][0]["type"] == "tool_result",
          body["messages"])
    check("Anthropic 助手 tool_use",
          body["messages"][1]["content"][0]["type"] == "tool_use",
          body["messages"])

    # --- Anthropic SSE → OpenAI SSE ---
    def sse(obj):
        return ('data: %s\n' % json.dumps(obj, ensure_ascii=False)).encode("utf-8")

    conv = P.AnthropicStream("claude-sonnet-4")
    out = conv.feed(sse({"type": "message_start",
                          "message": {"usage": {"input_tokens": 10,
                                                "output_tokens": 1}}}))
    check("message_start 产出 role", b'"role":"assistant"' in out, out)
    out = conv.feed(sse({"type": "content_block_delta",
                          "delta": {"type": "text_delta", "text": "你好"}}))
    check("text_delta 产出 content", "你好".encode("utf-8") in out, out)
    out = conv.feed(sse({"type": "content_block_start",
                          "content_block": {"type": "tool_use", "id": "toolu_1",
                                            "name": "get_weather"}}))
    check("tool_use 产出 tool_calls", b'"tool_calls"' in out, out)
    out = conv.feed(sse({"type": "content_block_delta",
                          "delta": {"type": "input_json_delta",
                                    "partial_json": '{"a":1}'}}))
    check("input_json_delta 产出 arguments", b'"arguments"' in out, out)
    out = conv.feed(sse({"type": "message_delta",
                          "delta": {"stop_reason": "tool_use"},
                          "usage": {"output_tokens": 5}}))
    check("tool_use 收尾是 tool_calls", b'"finish_reason":"tool_calls"' in out, out)
    check("收尾带 usage", b'"total_tokens":15' in out, out)
    check("收尾带 DONE", b"data: [DONE]" in out, out)
    check("close 不再产出", conv.close() == b"")

    # 少了 message_stop：close 要补一个收尾
    conv2 = P.AnthropicStream("m")
    conv2.feed(sse({"type": "content_block_delta",
                     "delta": {"type": "text_delta", "text": "x"}}))
    tail = conv2.close()
    check("缺 message_stop 时 close 补收尾",
          b"data: [DONE]" in tail and b'"finish_reason"' in tail, tail)

    # --- OpenAI 兼容请求体 ---
    body = P._openai_body({"model": "m", "messages": [], "max_completion_tokens": 77},
                          "m")
    check("OpenAI body max_tokens", body["max_tokens"] == 77, body)
    check("OpenAI body 强制流式", body["stream"] is True)
    check("OpenAI body 带 usage", body["stream_options"]["include_usage"] is True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
