# -*- coding: utf-8 -*-
"""聚合渠道的**线协议**层：把上游的响应翻成下游管线认的 ``data: {...}`` 字节流。

网关的聊天管线（`_chat_stream_response` / `aggregate_stream` / `_messages_*` /
`_responses_*`）只认一件事：一个带 ``read1()`` 的字节源，按行吐 `data: <json>`，
`[DONE]` 由下游自己写。所以这里只需要把上游那侧翻译成 **OpenAI chat chunk 序列**。

目前支持 `wire: "chat"`（绝大多数聚合渠道——OpenRouter / Groq / DeepSeek /
Mistral / Together / NVIDIA / SiliconFlow / Ollama… 都是 OpenAI 兼容）。

三种上游形状都认，而且**不要**为了省事只做第一种：

1. SSE（`data: {...}` 分行）—— 标准形态，直接把每个 chunk 归一化后透传；
2. 一整份 JSON 正文 —— 有些实现在 `stream: true` 下仍然回 JSON（或代理把 SSE
   拍平了），按 `chunks_from_completion()` 合成等价 chunk 序列；
3. 流里塞 `{"error": {...}}` —— 抛 :class:`UpstreamStreamError`，交给下游的
   「流中断」路径记账并回一个 SSE 错误帧。

`wire: "responses"` / `"messages"` 的渠道目前**列得出来但不可驱动**
（:data:`SUPPORTED_WIRES`），与 oauth / adc 的处理一致：不假装支持。

只依赖标准库。
"""

import json
import time

#: 线协议取值域（与 data/providers.json 的 `wire` 字段一致）。
WIRE_CHAT = "chat"
WIRE_RESPONSES = "responses"
WIRE_MESSAGES = "messages"

#: 真正实现过翻译器的线协议。其余照常列出，但网关拒绝驱动。
SUPPORTED_WIRES = (WIRE_CHAT,)

#: 默认每次从上游读多少字节。
READ_BLOCK = 65536

#: 上游 chat 请求体里允许透传的字段（白名单，不是黑名单）。
#:
#: 白名单的两个理由：一是网关自己的扩展字段（`thinking` / `reasoning` /
#: `product` / `enable_thinking` …）到了别家只会换来 400；二是**缺省即安全**——
#: 将来上游新增字段不会被网关悄悄转发到第三方。
PASSTHROUGH_FIELDS = (
    "messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens",
    "max_completion_tokens", "stop", "presence_penalty", "frequency_penalty",
    "seed", "response_format", "logprobs", "top_logprobs", "n", "user",
    "parallel_tool_calls", "reasoning_effort", "modalities", "service_tier",
    "prediction", "metadata", "store", "logit_bias",
)

#: 字段名里带这个标记的 400，判定为「上游不认 stream_options」，去掉重试一次。
_STREAM_OPTIONS_HINT = "stream_options"


class UpstreamStreamError(Exception):
    """上游在流中途报了错（或回了非流形状的 error）。

    故意不做成上游 HTTP 错误：状态码已经发出去了，下游只能按「流中断」处理。
    """


def wire_supported(wire):
    return str(wire or "").strip().lower() in SUPPORTED_WIRES


def chat_body(upstream_model, payload, include_usage=True):
    """构造发给聚合渠道的 chat 请求体。

    一律 ``stream: true``：下游的流式与非流式管线都建立在「上游给 chunk 序列」
    之上，非流式客户端由 `aggregate_stream` 自己聚合，网关因此只需维护一条路径。
    ``stream_options.include_usage`` 是拿 token 账本的前提——多数 OpenAI 兼容
    上游只在开启它时才在末尾发 usage；不认这个字段的由调用方去掉重试。
    """
    src = payload if isinstance(payload, dict) else {}
    body = {}
    for field in PASSTHROUGH_FIELDS:
        value = src.get(field)
        if value is not None:
            body[field] = value
    body["model"] = upstream_model
    body["stream"] = True
    if include_usage:
        body["stream_options"] = {"include_usage": True}
    if not body.get("messages"):
        body["messages"] = []
    return body


def is_stream_options_rejection(status, detail):
    """这个上游错误是不是「不认识 stream_options」。"""
    if status != 400:
        return False
    return _STREAM_OPTIONS_HINT in str(detail or "").lower()


def raw_lines(resp, block=READ_BLOCK):
    """按行读取一个带 ``read1()``/``read()`` 的响应体。"""
    read = getattr(resp, "read1", None) or getattr(resp, "read", None)
    if read is None:
        return
    buf = b""
    while True:
        try:
            chunk = read(block)
        except TypeError:
            chunk = read()
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", "replace")
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line
    if buf:
        yield buf


def _normalize_chunk(obj, model, chunk_id=None):
    """把一个上游 chunk 归一到下游认的形状；认不出返回 None。"""
    if not isinstance(obj, dict):
        return None
    err = obj.get("error")
    if err and not obj.get("choices"):
        message = err.get("message") if isinstance(err, dict) else str(err)
        raise UpstreamStreamError(message or "upstream reported an error mid-stream")
    choices = obj.get("choices")
    has_usage = isinstance(obj.get("usage"), dict)
    if not isinstance(choices, list) and not has_usage:
        return None
    if model:
        obj["model"] = model
    if chunk_id and not obj.get("id"):
        obj["id"] = chunk_id
    obj.setdefault("object", "chat.completion.chunk")
    if not isinstance(obj.get("choices"), list):
        obj["choices"] = []
    return obj


def chunks_from_completion(obj, model, chunk_id=None):
    """把一整份 chat.completion 摊成等价的 chunk 序列。

    用于「上游在 stream: true 下仍然回 JSON」的实现，以及为不支持流式的小渠道
    兜底。语义与真流一致：role → （thinking）→ content → tool_calls → finish。
    """
    obj = obj if isinstance(obj, dict) else {}
    cid = chunk_id or obj.get("id") or "chatcmpl-channel"
    created = obj.get("created") or int(time.time())
    choices = obj.get("choices") if isinstance(obj.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}

    def chunk(delta, finish=None):
        return {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    yield chunk({"role": "assistant"})
    reasoning = message.get("reasoning_content") or message.get("reasoning")
    if reasoning:
        yield chunk({"reasoning_content": reasoning})
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text") or "" for part in content
                          if isinstance(part, dict))
    if content:
        yield chunk({"content": content})
    for index, call in enumerate(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        yield chunk({"tool_calls": [{
            "index": index,
            "id": call.get("id") or "call_%d" % index,
            "type": "function",
            "function": {"name": fn.get("name") or "",
                         "arguments": fn.get("arguments") or ""},
        }]})
    finish = choice.get("finish_reason") or ("tool_calls" if message.get("tool_calls") else "stop")
    final = chunk({}, finish)
    if isinstance(obj.get("usage"), dict):
        final["usage"] = obj["usage"]
    yield final


def iter_chunks(resp, model, chunk_id=None):
    """上游响应体 → 归一化后的 OpenAI chunk 字典序列。

    会抛 :class:`UpstreamStreamError`（上游流内报错）以及底层 IO 异常
    （断流 / 超时）——两者都由下游的「流中断」路径接住。
    """
    streamed = False
    json_buf = None
    for raw in raw_lines(resp):
        line = raw.decode("utf-8", "replace").rstrip("\r")
        if not line.strip() or line.startswith(":"):
            continue
        if line.startswith("data:"):
            streamed = True
            data = line[5:].strip()
            if data == "[DONE]":
                return
            if not data:
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            normalized = _normalize_chunk(obj, model, chunk_id)
            if normalized is not None:
                yield normalized
            continue
        # 事件名 / id / retry 之类的前缀行直接跳过。
        if line.startswith(("event:", "id:", "retry:")):
            continue
        if not streamed:
            # 还没有见过任何 `data:`：先当整份 JSON 正文攒着。
            json_buf = (json_buf or "") + line + "\n"
    if json_buf is not None and not streamed:
        try:
            obj = json.loads(json_buf)
        except Exception:
            return
        if isinstance(obj, dict) and obj.get("error") and not obj.get("choices"):
            err = obj["error"]
            raise UpstreamStreamError(
                (err.get("message") if isinstance(err, dict) else str(err))
                or "upstream returned an error document")
        for item in chunks_from_completion(obj, model, chunk_id):
            yield item
