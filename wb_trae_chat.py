# -*- coding: utf-8 -*-
"""Trae 聊天通道：请求构造 + 上游 SSE 到 OpenAI 增量块的翻译。

Trae 的 ``/api/agent/v3/llm_utils_chat`` 返回一组命名事件：

    event: output       data: {"content": "...", "reasoning": "..."}
    event: output       data: {"response": "..."}            # 旧格式
    event: output       data: {"tool_calls": [...]}
    event: done         data: {"finish_reason": "stop"}
    event: token_usage  data: {...}
    event: error        data: {"code": ..., "message": ...}

网关的下游管线只认 ``data: {...}`` 的 OpenAI chunk（见
wb_proxy.QoderUpstreamAdapter 的同款说明），所以这里把命名事件翻成
``chat.completion.chunk``，由 wb_proxy 再序列化。Trae 没有 Qoder 的 COSY
签名，也没有 ``price_factor``；请求头就是客户端公开的那一套
（Cloud-IDE-JWT / X-Cloudide-Token / x-device-*）。
"""

import json
import time
import uuid

import wb_trae


class TraeChatError(RuntimeError):
    """Trae 聊天通道错误（上游 error 事件 / 流损坏）。"""


#: 直连 llm_utils_chat 还不认的目录条目 -> 同底层模型的稳定 config_name。
#: ``deepseek-v4.1-flash`` 在客户端 solo_agent 会话里可用，但直连
#: llm_utils_chat 稳定返回 4001「param invalid」（与消息、会话、UA 无关）；
#: ``DeepSeek-V4-Flash`` 的底层 provider 模型同为 ``ali-deepseek-v4-flash``。
_UPSTREAM_CONFIG_FALLBACKS = {
    "deepseek-v4.1-flash": "DeepSeek-V4-Flash",
}

#: 上游 finish_reason -> OpenAI 口径。
_FINISH_REASONS = {
    "stop": "stop",
    "end": "stop",
    "done": "stop",
    "finish": "stop",
    "eos": "stop",
    "length": "length",
    "max_tokens": "length",
    "content_filter": "content_filter",
    "tool_calls": "tool_calls",
    "tool_call": "tool_calls",
    "tooluse": "tool_calls",
    "tool_use": "tool_calls",
    "function_call": "tool_calls",
    "error": "stop",
}


def _map_finish_reason(value):
    text = str(value or "").strip().lower()
    return _FINISH_REASONS.get(text) or (text or None)


def _image_url_of(part):
    if not isinstance(part, dict):
        return ""
    image = part.get("image_url")
    if isinstance(image, dict):
        return str(image.get("url") or "")
    if isinstance(image, str):
        return image
    if part.get("type") in ("input_image", "image"):
        return str(part.get("image_url") or part.get("url") or "")
    return ""


def _content_parts(content):
    """OpenAI content -> Trae 的 [{type:text|image_url}] 数组。"""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append({"type": "text", "text": item})
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind in ("text", "input_text"):
                parts.append({"type": "text",
                              "text": str(item.get("text") or "")})
            elif kind in ("image_url", "input_image", "image"):
                url = _image_url_of(item)
                if url:
                    parts.append({"type": "image_url",
                                  "image_url": {"url": url}})
        return parts or [{"type": "text", "text": ""}]
    if content is None:
        return [{"type": "text", "text": ""}]
    return [{"type": "text", "text": str(content)}]


def _tool_result_text(content):
    """Trae 不认 role=tool；把工具结果折成一条 user 文本。"""
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict) and item.get("type") in ("text", "input_text"):
                chunks.append(str(item.get("text") or ""))
        text = "\n".join(chunks)
    else:
        text = "" if content is None else str(content)
    return "[工具结果]\n" + text


def _normalize_tool_calls(tool_calls):
    """OpenAI assistant.tool_calls -> Trae ToolCall 形状。

    Trae 的 protobuf JSON 是 ``tool_calls[].function_call.{name,arguments}``；
    OpenAI 是 ``tool_calls[].function.{name,arguments}``。原样透传时上游读不到
    ``function_call.name``，会报 2001 "required field Name is not set"。
    ``arguments`` 同样必须是字符串。
    """
    out = []
    for call in tool_calls if isinstance(tool_calls, list) else []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        fc = call.get("function_call") if isinstance(call.get("function_call"), dict) else {}
        name = str(fn.get("name") or fc.get("name") or "")
        if not name:
            continue
        args = fn.get("arguments") if "arguments" in fn else fc.get("arguments")
        if args is None:
            args = "{}"
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        item = {"function_call": {"name": name, "arguments": args}}
        call_id = call.get("id") or fc.get("id")
        if call_id:
            item["id"] = str(call_id)
        out.append(item)
    return out


def transform_messages(messages):
    """把 OpenAI messages 转成 Trae 的 role/content 形状。"""
    out = []
    for raw in messages if isinstance(messages, list) else []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "user")
        if role == "developer":
            role = "system"
        content = raw.get("content")
        if role == "tool":
            out.append({"role": "user",
                        "content": _content_parts(_tool_result_text(content))})
            continue
        item = {"role": role, "content": _content_parts(content)}
        tool_calls = raw.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            normalized = _normalize_tool_calls(tool_calls)
            if normalized:
                item["tool_calls"] = normalized
        out.append(item)
    return out


def _request_max_tokens(chat_req):
    """max_tokens = min(上限, chat_req.max_tokens)；客户端没给就不带这个字段。

    Trae 客户端本身只在调用方显式给了 max_tokens 时才发送，未指定时留给上游
    默认值——网关照抄更安全（有些模型的输出上限小于 128k，硬塞会被上游拒绝）。
    """
    limit = int(wb_trae.MAX_OUTPUT_TOKENS)
    requested = chat_req.get("max_tokens")
    if isinstance(requested, (int, float)) and not isinstance(requested, bool) \
            and requested > 0:
        return min(limit, int(requested))
    return None


def resolve_model_config(realm, model_id, access_token=None, auth=None,
                         proxy="", data_dir=None):
    """把网关的模型 id 解析成 Trae 的上游 config_name。

    Trae 的模型目录随账号变化，但用户手填一个新的 config_name 也不该被网关
    拦下（上游会给出真正的错误）：目录里没有时合成一条 ``source=system``
    的条目。``auto`` 走 inline_chat（客户端同款语义）。
    """
    realm = wb_trae.realm_key(realm)
    model_id = str(model_id or "").strip()
    if not model_id:
        return None
    if model_id.lower() == "auto":
        return {
            "entry": {"config_name": "auto", "name": "Auto",
                      "supports_tool_call": True},
            "key": "auto",
            "source": "auto",
            "name": "Auto",
            "function": "inline_chat",
        }
    config = wb_trae.alias_for(realm, model_id)
    models, source = wb_trae.available_models(
        realm, access_token=access_token, auth=auth or {}, proxy=proxy)
    lowered = {m["id"].lower(): m for m in models}
    entry = (lowered.get(config.lower())
             or next((m for m in models if m.get("key", "").lower() == config.lower()), None)
             or next((m for m in models
                      if m["id"].lower() == model_id.lower()), None))
    if entry is None:
        entry = {
            "id": config,
            "name": config,
            "key": config,
            "context": wb_trae.DEFAULT_CONTEXT_WINDOW,
            "max_tokens": wb_trae.MAX_OUTPUT_TOKENS,
            "reasoning": True,
            "supports_effort": False,
            "efforts": [],
            "input": ["text"],
            "supports_tool_call": True,
            "credits": "",
            "source": "system",
        }
    return {
        "entry": entry,
        "key": str(entry.get("key") or config),
        "source": "remote" if entry.get("source") == "remote" else "system",
        "name": entry.get("name") or config,
        "function": "chat_v3",
    }


def _normalize_tools(tools):
    """OpenAI tools -> Trae 的 FunctionDefinition 形状。

    Trae 的 Go 结构体把 ``function.parameters`` 声明成 ``string``，直接透传
    OpenAI 的对象会 400（cannot unmarshal object ... of type string），所以
    这里把 parameters 序列化成 JSON 文本；已经是字符串的原样保留。
    """
    out = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        item = dict(tool)
        fn = item.get("function")
        if isinstance(fn, dict):
            fn = dict(fn)
            params = fn.get("parameters")
            if isinstance(params, (dict, list)):
                fn["parameters"] = json.dumps(params, ensure_ascii=False)
            elif params is None:
                fn["parameters"] = "{}"
            item["function"] = fn
        out.append(item)
    return out


def _normalize_tool_choice(choice):
    """Trae 的 ``tool_choice`` 是字符串；OpenAI 的对象形式降级成 auto。"""
    return choice if isinstance(choice, str) else "auto"


def build_chat_request(chat_req, resolved, identity, session_id=None,
                       data_dir=None):
    """构造一次 Trae 聊天请求。

    返回 ``{"url", "headers", "body"(bytes), "effort", "model"}``；body 是
    JSON 字节。identity 至少要有 realm；access_token / auth 由调用方从账号
    对象传进来。
    """
    chat_req = chat_req if isinstance(chat_req, dict) else {}
    resolved = resolved if isinstance(resolved, dict) else {}
    identity = identity if isinstance(identity, dict) else {}
    entry = resolved.get("entry") if isinstance(resolved.get("entry"), dict) else {}
    config_name = str(resolved.get("key") or entry.get("key") or "")
    # 目录里的新条目不一定被 llm_utils_chat 接受；按需换成稳定的同源
    # config_name（对客户端仍保留原模型 id）。
    upstream_config = _UPSTREAM_CONFIG_FALLBACKS.get(
        config_name.lower(), config_name)
    realm = identity.get("realm") or resolved.get("realm") or "cn"
    function = str(resolved.get("function") or "chat_v3")
    access_token = str(identity.get("access_token") or "")
    auth = identity.get("auth") if isinstance(identity.get("auth"), dict) else {}
    api_host = wb_trae.api_host_for(realm, auth,
                                    identity.get("api_host") or "")

    body = {
        "messages": transform_messages(chat_req.get("messages")),
        "function": function,
        "stream": True,
    }
    if upstream_config and function != "inline_chat":
        body["config_name"] = upstream_config
        body["model"] = upstream_config
    elif identity.get("model_name"):
        body["model"] = str(identity["model_name"])

    max_tokens = _request_max_tokens(chat_req)
    if max_tokens:
        body["max_tokens"] = max_tokens
    for key in ("temperature", "top_p", "presence_penalty",
                "frequency_penalty"):
        value = chat_req.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            body[key] = value
    seed = chat_req.get("seed")
    if isinstance(seed, (int, float)) and not isinstance(seed, bool) and seed != 0:
        body["seed"] = seed
    count = chat_req.get("n")
    if isinstance(count, int) and count > 1:
        body["n"] = count
    stop = chat_req.get("stop")
    if stop:
        body["stop"] = [str(stop)] if isinstance(stop, str) else list(stop)
    # 工具定义按 OpenAI 形状透传，但 parameters 要转成 JSON 字符串；
    # tool_choice 也只接受字符串（见 _normalize_tools / _normalize_tool_choice）。
    tools = chat_req.get("tools")
    if isinstance(tools, list) and tools:
        normalized = _normalize_tools(tools)
        if normalized:
            body["tools"] = normalized
            body["tool_choice"] = _normalize_tool_choice(
                chat_req.get("tool_choice", "auto"))

    request_id = str(uuid.uuid4())
    headers = wb_trae.build_stream_headers(access_token, auth, realm,
                                           request_id=request_id)
    url = api_host + "/api/agent/v3/llm_utils_chat"
    return {
        "url": url,
        "headers": headers,
        "body": json.dumps(body, ensure_ascii=False).encode("utf-8"),
        "effort": None,
        "model": config_name or str(chat_req.get("model") or ""),
    }


# ---------------------------------------------------------------------------
# 上游 SSE -> OpenAI 增量块

DEFAULT_CHUNK_ID = "chatcmpl-trae"


def _as_int(value):
    try:
        if isinstance(value, bool):
            return 0
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _usage_to_openai(raw):
    """把 Trae 的 token_usage 事件塑造成 OpenAI usage（兼容两套字段名）。"""
    usage = raw if isinstance(raw, dict) else {}
    inner = usage.get("data") if isinstance(usage.get("data"), dict) else usage
    prompt = _as_int(inner.get("prompt_tokens") or inner.get("input_tokens")
                     or inner.get("promptTokens") or inner.get("inputTokens"))
    completion = _as_int(
        inner.get("completion_tokens") or inner.get("output_tokens")
        or inner.get("completionTokens") or inner.get("outputTokens"))
    total = _as_int(inner.get("total_tokens") or inner.get("totalTokens"))
    if total <= 0:
        total = prompt + completion
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_tokens_details": {},
        "completion_tokens_details": {},
    }
    cached = (inner.get("cached_tokens")
              or inner.get("cache_read_input_tokens")
              or inner.get("cache_tokens"))
    if cached:
        out["prompt_tokens_details"]["cached_tokens"] = _as_int(cached)
    reasoning = inner.get("reasoning_tokens")
    if reasoning:
        out["completion_tokens_details"]["reasoning_tokens"] = _as_int(reasoning)
    return out


def _stream_chunk(chunk_id, model, created, delta=None, finish_reason=None):
    choice = {"index": 0, "delta": dict(delta) if delta else {}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model, "choices": [choice]}


def _usage_chunk(chunk_id, model, created, usage):
    return {"id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model, "choices": [], "usage": usage}


def _tool_call_deltas(raw_calls):
    """上游 tool_calls -> OpenAI 增量形状（首块带 id/name，后续只带参数）。"""
    out = []
    for index, call in enumerate(raw_calls if isinstance(raw_calls, list) else []):
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        if not fn:
            # Trae 的增量事件用 function_call（与请求侧同名字段）。
            fn = call.get("function_call") if isinstance(
                call.get("function_call"), dict) else {}
        item = {"index": call.get("index", index)}
        call_id = str(call.get("id") or fn.get("id") or "")
        name = str(fn.get("name") or call.get("name") or "")
        args = fn.get("arguments")
        if args is None:
            args = fn.get("partial_arguments")
        if args is None:
            args = call.get("arguments")
        if call_id:
            item["id"] = call_id
            item["type"] = "function"
        if name or args is not None:
            item["function"] = {}
            if name:
                item["function"]["name"] = name
            if args is not None:
                item["function"]["arguments"] = (args if isinstance(args, str)
                                                 else json.dumps(args,
                                                                 ensure_ascii=False))
        if item.get("id") or item.get("function"):
            out.append(item)
    return out


def _output_delta(data):
    """一个 output 事件里可能同时有正文 / 思考 / 工具调用。"""
    if not isinstance(data, dict):
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    text = inner.get("content")
    if text is None:
        text = inner.get("response")
    if text is None:
        text = inner.get("text")
    reasoning = inner.get("reasoning_content")
    if reasoning is None:
        reasoning = inner.get("reasoning")
    delta = {}
    if isinstance(text, str) and text:
        if text.startswith("Building prompt:") or \
                text.startswith("Completed building prompt"):
            return None
        delta["content"] = text
    if isinstance(reasoning, str) and reasoning:
        delta["reasoning_content"] = reasoning
    calls = inner.get("tool_calls") or inner.get("toolCalls")
    calls = _tool_call_deltas(calls) if calls else []
    if calls:
        delta["tool_calls"] = calls
    return delta or None


def _load_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def iter_openai_chunks(resp, model):
    """把一次 Trae SSE 响应解码成 OpenAI chat.completion.chunk 增量块。

    只认 ``event:`` / ``data:`` 两行；空行和注释跳过。``[DONE]`` 立即结束
    （上游发完哨兵后可能把连接挂住）。事件 ``error`` 抛 TraeChatError，交给
    调用方按上游断流记账。本函数不写 ``[DONE]``，wb_proxy 自己写哨兵。
    """
    model = "" if model is None else str(model)
    created = int(time.time())
    chunk_id = "%s-%s" % (DEFAULT_CHUNK_ID, uuid.uuid4().hex[:12])
    finish_emitted = False
    usage_snapshot = None
    saw_any = False
    buffer = ""
    event_name = "message"
    saw_done = False

    yield _stream_chunk(chunk_id, model, created,
                        {"role": "assistant", "content": ""})

    while not saw_done:
        try:
            raw = resp.read(65536)
        except TraeChatError:
            raise
        except Exception as exc:
            raise TraeChatError("Trae 上游流读取失败: %s" % exc)
        if not raw:
            break
        if isinstance(raw, bytes):
            buffer += raw.decode("utf-8", "replace")
        else:
            buffer += str(raw)
        while True:
            line_end = buffer.find("\n")
            if line_end == -1:
                break
            line = buffer[:line_end].strip()
            buffer = buffer[line_end + 1:]
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip() or "message"
                continue
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                saw_done = True
                break
            payload = _load_json(data_str)
            if payload is None:
                continue
            saw_any = True

            if event_name == "output":
                delta = _output_delta(payload)
                if delta:
                    yield _stream_chunk(chunk_id, model, created, delta)
                continue

            if event_name == "done":
                inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                reason = _map_finish_reason(
                    inner.get("finish_reason") or inner.get("stop_reason")
                    or inner.get("reason"))
                if not finish_emitted:
                    finish_emitted = True
                    yield _stream_chunk(chunk_id, model, created, {},
                                        reason or "stop")
                continue

            if event_name == "token_usage":
                usage_snapshot = _usage_to_openai(payload)
                continue

            if event_name == "error":
                inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                code = inner.get("code") or inner.get("error_code") or ""
                message = inner.get("message") or inner.get("msg") or "unknown error"
                if str(code) == "4011":
                    message = ("Trae 上游限流（4011）：账号或出口的请求频率/额度已超限，"
                               "稍后再试或换一个账号；原始信息: %s" % message)
                elif str(code) == "4015":
                    message = ("Trae 上游风控拦截（4015）：账号或 IP 被判定有风险；"
                               "原始信息: %s" % message)
                raise TraeChatError("Trae 上游错误 %s: %s" % (code, message))

            if event_name in ("extra_info", "metadata", "timing_cost",
                              "progress_notice", "queue_begin", "queue_end",
                              "request_wait_in_queue"):
                continue

            # 兼容没有 event: 行、只给 data: 的部署：按正文处理。
            delta = _output_delta(payload)
            if delta:
                yield _stream_chunk(chunk_id, model, created, delta)

    if not finish_emitted:
        yield _stream_chunk(chunk_id, model, created, {}, "stop")
    if usage_snapshot is not None:
        yield _usage_chunk(chunk_id, model, created, usage_snapshot)
    elif not saw_any:
        # 空流交给 wb_proxy 的「empty upstream stream」兜底，不额外造 usage。
        pass
