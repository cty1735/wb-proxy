# -*- coding: utf-8 -*-
"""Qoder 聊天通道（algo SSE agent_chat_generation）的请求构造与流解码。

与 wb_qoder.py 的分工：wb_qoder 负责账号侧（登录/刷新/额度/目录），本模块
负责 /v1 聊天链路对 Qoder 上游的最后一跳，全部按 pi-provider-qoder
（simonsmh/pi-provider-qoder）的 TypeScript 实现逐条移植：

* protocol/encoding.ts        -> encode_body（自定义 base64 表 + 3 字节块重排）
* protocol/tool-call-id.ts    -> wire_tool_call_id（非法 id 确定性哈希成 qtc_）
* protocol/thinking.ts        -> ThinkingTagParser / strip_thinking_tags /
                                 strip_dsml_residue / is_degenerate_dsml_turn
* protocol/transform.ts       -> 消息与工具的 OpenAI 形状清洗
* protocol/stream.ts          -> build_chat_request（请求体逐字段）与
                                 iter_openai_chunks（上游 SSE -> OpenAI 增量块），
                                 含 stableHash / stableChatRecordID
* credits.ts                  -> chargedCredits 语义（credit 字段）

与参考实现的两处刻意差异（都有注释标注）：

* 参考对单行 JSON 解析失败是跳过该行（SyntaxError -> continue），本模块按
  网关约定直接抛 QoderChatError：坏行意味着上游协议崩了，静默吞掉只会让
  客户端拿到半截回答；
* 参考经 pi-ai 的 content-block 模型输出，这里直接输出 OpenAI 流式增量块，
  并补齐参考在流末才收敛的 finish_reason 与 usage 块。

导入本模块不做任何网络 IO；网络请求由调用方通过 wb_qoder 的既有设施发起。
"""

import base64
import codecs
import hashlib
import json
import re
import threading
import time
import uuid
from http.client import IncompleteRead

import wb_qoder

# ---------------------------------------------------------------------------
# protocol/encoding.ts：Qoder 自定义 base64 字母表 + 3 字节块重排

#: Qoder 的替换字母表（下标与标准 base64 字母表一一对应；官方 CLI 同值）。
QODER_BASE64_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"

_STD_BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

#: 字符映射表：标准 base64 的每个字符 -> 自定义字母表；"=" padding 换成 "$"。
_ENCODE_TABLE = bytearray(range(256))
for _i, _ch in enumerate(_STD_BASE64_ALPHABET):
    _ENCODE_TABLE[ord(_ch)] = ord(QODER_BASE64_ALPHABET[_i])
_ENCODE_TABLE[ord("=")] = ord("$")


def encode_body(payload):
    """把请求 JSON 字节编码成 Qoder 的上线格式（与 qoderEncodeBody 等价）。

    先做标准 base64，再按 a = floor(n/3) 把字符串切成
    [n-a, n) + [a, n-a) + [0, a) 三段重排（等价于
    std.slice(n - a) + std.slice(a, n - a) + std.slice(0, a)），
    最后逐字符过替换表，"=" 变成 "$"。返回 ASCII 字节。
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    std = base64.b64encode(payload or b"").decode("ascii")
    n = len(std)
    a = n // 3
    out = bytearray(n)
    dst = 0
    for i in range(n - a, n):
        out[dst] = _ENCODE_TABLE[ord(std[i])]
        dst += 1
    for i in range(a, n - a):
        out[dst] = _ENCODE_TABLE[ord(std[i])]
        dst += 1
    for i in range(a):
        out[dst] = _ENCODE_TABLE[ord(std[i])]
        dst += 1
    return bytes(out)


# ---------------------------------------------------------------------------
# protocol/tool-call-id.ts：上游只接受 ^[a-zA-Z0-9_-]{1,64}$ 的工具调用 id

#: Qoder 线上合法的工具调用 id 形状。
_WIRE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

#: 预留给本函数生成值的前缀；已经是这个形状的输入必须再哈希，
#: 否则「透传 id」可能与「生成 id」撞车（tool-call-id.ts 的注释）。
_RESERVED_WIRE_ID_RE = re.compile(r"^qtc_[0-9a-f]{60}$")

#: 工具调用 id 哈希的域分隔前缀（v1 方案，见 tool-call-id.ts digestToolCallId）。
_WIRE_ID_HASH_PREFIX = b"qoder-tool-call-id:v1\x00"


def wire_tool_call_id(original):
    """把内部工具调用 id 变成 Qoder 线上可接受的形状。

    合法（^[a-zA-Z0-9_-]{1,64}$ 且不是 qtc_ + 60 位 hex）就原样返回；否则
    返回 "qtc_" + sha256(域分隔前缀 + id).hexdigest()[:60]，确定性且不泄漏
    原始 id（"|" 等字符上游会 400）。
    """
    if not isinstance(original, str):
        raise QoderChatError("Qoder 工具调用 id 必须是字符串")
    if _WIRE_ID_RE.match(original) and not _RESERVED_WIRE_ID_RE.match(original):
        return original
    digest = hashlib.sha256()
    digest.update(_WIRE_ID_HASH_PREFIX)
    digest.update(original.encode("utf-8"))
    return "qtc_" + digest.hexdigest()[:60]


# ---------------------------------------------------------------------------
# protocol/thinking.ts：思考标签解析与 DSML 残渣清理

#: 内容通道里可能出现的思考标签对（顺序即参考实现的匹配顺序）。
THINKING_TAG_VARIANTS = (
    ("<thinking>", "</thinking>"),
    ("<think>", "</think>"),
    ("<reasoning>", "</reasoning>"),
    ("<thought>", "</thought>"),
)

_ALL_THINKING_TAGS = [tag for pair in THINKING_TAG_VARIANTS for tag in pair]


def strip_thinking_tags(text):
    """去掉文本里所有思考标签（开/闭）——与 stripThinkingTags 等价。

    Qoder 后端有时把字面量 <thinking> 塞进 reasoning_content（配对的
    </thinking> 落在 content 通道），逐块尽力清理，标签跨块拆开由
    ThinkingTagParser 兜住。
    """
    out = text or ""
    for open_tag, close_tag in THINKING_TAG_VARIANTS:
        if open_tag in out:
            out = out.replace(open_tag, "")
        if close_tag in out:
            out = out.replace(close_tag, "")
    return out


#: 上游网关偶发漏出的 DSML 工具调用标记（全角竖线 / </invoke> 等）。
_DSML_TAG_RE = re.compile(
    r"</?[\uFF5C|]{0,2}\s*DSML\s*[\uFF5C|]{0,2}[^>]*>|"
    r"</?(?:invoke|invocation|parameter|function_call|function_calls)\b[^>]*>")

#: 三行以上的连续空行并成两行（等价 JS 的 /\n{3,}/g -> "\n\n"）。
_BLANK_RUN_RE = re.compile(r"\n{3,}")

#: reasoning 通道末尾连续两个以上的 DSML 标记 = 网关吞掉工具调用的退化回合。
_DEGENERATE_DSML_TAIL_RE = re.compile(
    r"(?:(?:</?(?:invoke|invocation|parameter|function_call|function_calls)\b[^>]*>)\s*|"
    r"(?:</?[\uFF5C|]{0,2}\s*DSML\s*[\uFF5C|]{0,2}[^>]*>)\s*){2,}$")


def strip_dsml_residue(text):
    """删掉漏出的 DSML 工具调用标记，并把它留下的空行并回两行。"""
    return _BLANK_RUN_RE.sub("\n\n", _DSML_TAG_RE.sub("", text or ""))


def is_degenerate_dsml_turn(stop_reason, has_text, raw_reasoning):
    """判断一个已结束的回合是否是「工具调用被网关吞掉」的退化输出。

    与 isDegenerateDsmlTurn 等价：stop_reason 是 toolUse 就不是；有非空
    可见文本也不是；否则看未经 strip_dsml_residue 清洗的 reasoning 尾部
    是否以连续 DSML 标记收尾。raw_reasoning 只保留尾部即可（参考只留 512）。
    """
    if stop_reason in ("tool_calls", "toolUse", "tool_use"):
        return False
    if has_text:
        return False
    return bool(_DEGENERATE_DSML_TAIL_RE.search(raw_reasoning or ""))


def _trailing_tag_prefix_length(text, tag):
    """text 末尾最多有多少个字符是 tag 的前缀（跨块标签需要扣住不输出）。"""
    max_len = min(len(text), len(tag) - 1)
    for length in range(max_len, 0, -1):
        if text.endswith(tag[:length]):
            return length
    return 0


def _max_trailing_tag_prefix_length(text, tags):
    best = 0
    for tag in tags:
        best = max(best, _trailing_tag_prefix_length(text, tag))
    return best


class ThinkingTagParser:
    """把 content 通道里的 <thinking>…</thinking> 块拆到思考通道。

    移植 thinking.ts 的 ThinkingTagParser：参考类把事件推进 pi 的
    AssistantMessageEventStream，这里改成最朴素的返回值——process_chunk /
    finalize 返回 ("text", 文本) / ("thinking", 文本) 事件列表，由
    iter_openai_chunks 组装成 OpenAI 增量块。跨块拆开的标签（如
    "<thi" + "nking>a</thinking>b"）由内部缓冲兜住。
    """

    def __init__(self):
        self._buffer = ""
        self._in_thinking = False
        self._thinking_extracted = False
        self._active_end_tag = THINKING_TAG_VARIANTS[0][1]

    def process_chunk(self, chunk):
        events = []
        self._buffer += chunk or ""
        while self._buffer:
            prev_length = len(self._buffer)
            if not self._in_thinking and not self._thinking_extracted:
                self._process_before_thinking(events)
                if not self._buffer:
                    break
            if self._in_thinking:
                self._process_inside_thinking(events)
                if not self._buffer:
                    break
            if self._thinking_extracted:
                self._process_after_thinking(events)
                break
            if len(self._buffer) >= prev_length:
                break
        return events

    def finalize(self):
        """流末把残留缓冲吐出：思考中途按 thinking，否则按 text。"""
        events = []
        if self._buffer:
            kind = "thinking" if self._in_thinking else "text"
            events.append((kind, self._buffer))
            self._buffer = ""
            self._in_thinking = False
        return events

    def _process_before_thinking(self, events):
        best_open_pos = -1
        best_open = None
        best_close_pos = -1
        best_close = None
        for variant in THINKING_TAG_VARIANTS:
            open_pos = self._buffer.find(variant[0])
            if open_pos != -1 and (best_open_pos == -1 or open_pos < best_open_pos):
                best_open_pos = open_pos
                best_open = variant
            close_pos = self._buffer.find(variant[1])
            if close_pos != -1 and (best_close_pos == -1 or close_pos < best_close_pos):
                best_close_pos = close_pos
                best_close = variant

        # 先出现开标签：真正的思考块在 content 通道里，进入思考模式。
        if best_open is not None and (best_close is None or best_open_pos < best_close_pos):
            if best_open_pos > 0:
                events.append(("text", self._buffer[:best_open_pos]))
            self._buffer = self._buffer[best_open_pos + len(best_open[0]):]
            self._active_end_tag = best_open[1]
            self._in_thinking = True
            return

        # 孤立的闭标签：开标签走的是 reasoning_content 通道，这里没有思考块
        # 可关，丢掉闭标签与紧跟的分隔换行（见 stream.ts 的注释）。
        if best_close is not None:
            if best_close_pos > 0:
                events.append(("text", self._buffer[:best_close_pos]))
            self._buffer = self._buffer[best_close_pos + len(best_close[1]):]
            if self._buffer.startswith("\n\n"):
                self._buffer = self._buffer[2:]
            elif self._buffer.startswith("\n"):
                self._buffer = self._buffer[1:]
            return

        # 还没有完整标签：扣住末尾可能是标签前缀的片段，别把它当正文吐出去。
        hold = _max_trailing_tag_prefix_length(self._buffer, _ALL_THINKING_TAGS)
        safe_len = len(self._buffer) - hold
        if safe_len > 0:
            events.append(("text", self._buffer[:safe_len]))
            self._buffer = self._buffer[safe_len:]

    def _process_inside_thinking(self, events):
        end_pos = self._buffer.find(self._active_end_tag)
        if end_pos != -1:
            if end_pos > 0:
                events.append(("thinking", self._buffer[:end_pos]))
            self._buffer = self._buffer[end_pos + len(self._active_end_tag):]
            self._in_thinking = False
            self._thinking_extracted = True
            if self._buffer.startswith("\n\n"):
                self._buffer = self._buffer[2:]
            elif self._buffer.startswith("\n"):
                self._buffer = self._buffer[1:]
            return

        hold = _trailing_tag_prefix_length(self._buffer, self._active_end_tag)
        safe_len = len(self._buffer) - hold
        if safe_len > 0:
            events.append(("thinking", self._buffer[:safe_len]))
            self._buffer = self._buffer[safe_len:]

    def _process_after_thinking(self, events):
        events.append(("text", self._buffer))
        self._buffer = ""


class QoderChatError(RuntimeError):
    """Qoder 聊天通道的协议/流错误（调用方按普通字符串展示）。"""


# ---------------------------------------------------------------------------
# protocol/stream.ts：stableHash / stableChatRecordID（会话与记录 ID 的稳定派生）

def _stable_hash(prefix, *inputs):
    """stableHash 的 Python 版：sha256(prefix + ("\\0" + input)*).hex[:16]。"""
    digest = hashlib.sha256()
    digest.update(str(prefix).encode("utf-8"))
    for item in inputs:
        digest.update(b"\0")
        digest.update(str(item).encode("utf-8"))
    return digest.hexdigest()[:16]


def _json_compact(value):
    """等价 JS JSON.stringify（无空格、非 ASCII 不转义）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _js_truthy(value):
    """JS 真值语义：None/False/""/0 假；空数组、空对象也是真（与 JS 一致）。"""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value != ""
    return True


def _stable_chat_record_id(model, messages, tools, max_tokens):
    """stableChatRecordID 的移植：同一会话内容派生同一个记录 ID。

    tools 为空时传 None 而不是 []：参考里 toolsRaw 在无工具时是 undefined，
    `if (tools)` 为假，工具段（连同 json）根本不进哈希。
    """
    digest = hashlib.sha256()
    digest.update(b"qoder-record")
    digest.update(b"\0")
    digest.update(str(model).encode("utf-8"))
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role:
            digest.update(b"\0")
            digest.update(str(role).encode("utf-8"))
        if _js_truthy(content):
            digest.update(b"\0")
            if isinstance(content, str):
                digest.update(content.encode("utf-8"))
            else:
                digest.update(_json_compact(content).encode("utf-8"))
    if tools:
        digest.update(b"\0")
        digest.update(_json_compact(tools).encode("utf-8"))
    digest.update(b"\0")
    digest.update(("mt=%d" % int(max_tokens)).encode("utf-8"))
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# protocol/transform.ts：OpenAI 形状消息 / 工具 -> Qoder 消息 / 工具

def _content_text(content):
    """把 content（字符串或部件数组）折成纯文本（getContentText 口径）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") == "thinking" and isinstance(part.get("thinking"), str):
                    parts.append(part["thinking"])
        return "".join(parts)
    return ""


def _image_url_of(part):
    """从 image_url 部件里取 url（dict 或裸字符串两种写法都认）。"""
    raw = part.get("image_url")
    if isinstance(raw, dict):
        url = raw.get("url")
    elif isinstance(raw, str):
        url = raw
    else:
        url = None
    return url if isinstance(url, str) and url else None


def _content_images(content):
    """content 数组里的 image_url 部件（url 原样保留，通常是 data: URL）。"""
    images = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = _image_url_of(part)
                if url:
                    images.append({"type": "image_url", "image_url": {"url": url}})
    return images


def _user_content(content):
    """user 消息：带图时变成 [text, image_url...] 部件数组，否则折成纯文本。

    与 transform.ts 的 user 分支一致：有图片时逐部件映射（保持原顺序），
    没有图片时 getContentText 折叠。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append({"type": "text", "text": str(part.get("text") or "")})
            elif part.get("type") == "image_url":
                url = _image_url_of(part)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
        if any(p.get("type") == "image_url" for p in parts):
            return parts
        return _content_text(content)
    return ""


def _assistant_message(msg):
    """assistant 消息：content / reasoning_content / tool_calls 按参考映射。

    * tool_calls[].id 与 function.arguments 都过清洗：id 走 wire_tool_call_id，
      arguments 保证是字符串（dict 等用紧凑 JSON）；
    * content 为空且带工具调用或思考时注入单个空格占位、否则为 null——
      transform.ts 注释：上游会丢弃 content 为 null 的 assistant，导致后面的
      tool 结果成为孤儿（dmodel/ultimate 报 "tool must follow a message with
      tool_calls"）；
    * reasoning_content 回传（transform.ts 会先过 stripDsmlResidue，因为存下来的
      思考文本可能夹带上游漏出的 DSML 标记）。
    """
    content_raw = msg.get("content")
    content = content_raw if isinstance(content_raw, str) else _content_text(content_raw)
    reasoning_raw = msg.get("reasoning_content")
    reasoning = reasoning_raw if isinstance(reasoning_raw, str) else _content_text(reasoning_raw)
    reasoning = strip_dsml_residue(reasoning)
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        arguments = fn.get("arguments")
        if not isinstance(arguments, str):
            arguments = "{}" if arguments is None else _json_compact(arguments)
        tool_calls.append({
            "id": wire_tool_call_id(str(tc.get("id") or "")),
            "type": "function",
            "function": {"name": str(fn.get("name") or ""), "arguments": arguments},
        })
    mapped = {
        "role": "assistant",
        "content": content or (" " if (tool_calls or reasoning) else None),
    }
    if reasoning:
        mapped["reasoning_content"] = reasoning
    if tool_calls:
        mapped["tool_calls"] = tool_calls
    return mapped


def _transform_tools(tools):
    """transformTools 的移植：只留 {type, function:{name, description, parameters}}。

    无工具（或全被清掉）时返回 []——参考在请求里始终发 tools（无工具时 []），
    但记录 ID 的哈希段会跳过空工具（见 _stable_chat_record_id）。
    """
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if not isinstance(fn, dict):
            fn = tool  # 兼容平铺的 pi 形状（name/description/parameters 直接挂工具上）
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        item = {"name": name}
        if fn.get("description") is not None:
            item["description"] = fn["description"]
        if fn.get("parameters") is not None:
            item["parameters"] = fn["parameters"]
        out.append({"type": "function", "function": item})
    return out


def _transform_messages(raw_messages):
    """清洗 OpenAI 消息列表，返回 (system_text, messages)。

    依据 stream.ts：系统提示从消息里抽出来，作为唯一的领先 role:system
    注入（上游忽略顶层 system 字段）；开头连续的 system/developer 合并成
    这一条（参考把整个系统提示压成一条 systemText 注入，多条之间以空行
    连接，避免指令粘连）。中途出现的 system/developer 改名为 system 后
    原地保留——参考的 pi 输入里不存在这类消息（遇到会悄悄丢弃），网关
    不静默丢指令，这里选择保留（模块级的刻意差异）。
    """
    if not isinstance(raw_messages, list):
        raw_messages = []
    messages = []
    system_texts = []
    index = 0
    count = len(raw_messages)
    while index < count:
        msg = raw_messages[index]
        if not isinstance(msg, dict):
            index += 1
            continue
        role = str(msg.get("role") or "")
        if role in ("system", "developer") and not messages:
            # 开头连续的 system/developer：合并进唯一的前导系统消息
            text = _content_text(msg.get("content"))
            if text:
                system_texts.append(text)
            index += 1
            continue
        index += 1
        if role in ("system", "developer"):
            messages.append({"role": "system",
                             "content": _content_text(msg.get("content"))})
        elif role == "user":
            messages.append({"role": "user", "content": _user_content(msg.get("content"))})
        elif role == "assistant":
            messages.append(_assistant_message(msg))
        elif role == "tool":
            # transform.ts：连续 tool 结果先整体输出（tool 消息只能带文本），
            # 图片收集起来，跑完这一段后补一条 user 消息携带它们。
            image_parts = []
            while True:
                messages.append({
                    "role": "tool",
                    "tool_call_id": wire_tool_call_id(str(msg.get("tool_call_id") or "")),
                    "content": _content_text(msg.get("content")),
                })
                image_parts.extend(_content_images(msg.get("content")))
                if index >= count or not isinstance(raw_messages[index], dict) or \
                        str(raw_messages[index].get("role") or "") != "tool":
                    break
                msg = raw_messages[index]
                index += 1
            if image_parts:
                label = "[%d image%s returned by the previous tool call]" % (
                    len(image_parts), "" if len(image_parts) == 1 else "s")
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": label}] + image_parts,
                })
        # 其余角色（function 等）：与参考一样丢弃。
    return "\n\n".join(system_texts), messages


def _last_user_text(messages):
    """最后一条 user 消息的纯文本（chat_context / business.name 用）。"""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(part.get("text") or "") for part in content
                           if isinstance(part, dict) and part.get("type") == "text")
        return ""
    return ""


# ---------------------------------------------------------------------------
# 模型解析（远端目录 300s 缓存 -> 内置兜底）

#: 远端目录的进程内缓存 TTL（秒）。
MODEL_CACHE_TTL = 300

_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE = {}  # realm -> (timestamp, 原始条目列表)


def _realm_key(realm):
    return "cn" if str(realm or "").lower() == "cn" else "intl"


def _remote_model_entries(realm_key, access_token, auth, data_dir, proxy):
    """取远端目录（TTL 内直接用缓存）；抛异常时调用方回落内置清单。"""
    now = time.time()
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(realm_key)
        if cached and now - cached[0] < MODEL_CACHE_TTL:
            return cached[1]
    entries = wb_qoder.fetch_model_list(access_token, auth or {}, realm_key,
                                        data_dir=data_dir, proxy=proxy)
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE[realm_key] = (time.time(), entries)
    return entries


def resolve_model_config(realm, model_id, access_token=None, auth=None,
                         data_dir=None, proxy=""):
    """把网关的模型 id 解析成上游条目；解析不到返回 None。

    远端优先：有 token 时拉一次目录（300s 缓存、失败不缓存），按
    display_name 派生 id 或上游 key 匹配；兜底用内置清单（合成一条
    source=system 的条目）。返回 {"entry", "key", "source", "name"}。
    """
    model_id = str(model_id or "")
    if not model_id:
        return None
    realm_key = _realm_key(realm)
    if access_token:
        try:
            entries = _remote_model_entries(realm_key, access_token, auth,
                                            data_dir, proxy)
        except Exception:
            entries = None
        for entry in entries or ():
            if not isinstance(entry, dict):
                continue
            if wb_qoder.to_model_id(entry.get("display_name")) == model_id or \
                    entry.get("key") == model_id:
                return {
                    "entry": entry,
                    "key": str(entry.get("key") or ""),
                    "source": "remote",
                    "name": str(entry.get("display_name") or ""),
                }
    for model in wb_qoder.static_models(realm_key):
        if model["id"] == model_id:
            entry = {
                "key": model["key"],
                "is_reasoning": model["reasoning"],
                "display_name": model["name"],
                "source": "system",
            }
            return {"entry": entry, "key": model["key"],
                    "source": "system", "name": model["name"]}
    return None


# ---------------------------------------------------------------------------
# parameters：max_tokens 与思考档位（对应 stream.ts 的 clamp + 映射）

#: 请求档位词 -> 实际发上游的 reasoning_effort（minimal 归到 low）。
_EFFORT_LEVELS = {"minimal": "low", "low": "low", "medium": "medium",
                  "high": "high", "xhigh": "xhigh", "max": "max"}

#: 显式关闭思考的取值。
_EFFORT_OFF = ("none", "off", "disabled", "false")

#: 开思考但不点名档位的取值（Anthropic 风格的 thinking.type=enabled）。
_EFFORT_ONLY_ON = ("enabled", "on", "true", "auto")


def _entry_is_reasoning(entry):
    """模型的推理能力开关：entry.is_reasoning 为真才算（stream.ts 的 !!is_reasoning）。

    上游目录没给该字段（或给了假）时一律视为不支持思考，enable_thinking
    会被显式关掉——与「模型 is_reasoning 假 -> enable_thinking=false」一致。
    """
    return bool((entry or {}).get("is_reasoning"))


def _entry_efforts(entry):
    """entry.thinking_config.enabled.efforts（不存在或不是 dict 时返回 None）。"""
    thinking = (entry or {}).get("thinking_config")
    if not isinstance(thinking, dict):
        return None
    enabled = thinking.get("enabled")
    if not isinstance(enabled, dict):
        return None
    efforts = enabled.get("efforts")
    return efforts if isinstance(efforts, dict) and efforts else None


def _parse_effort(chat_req):
    """解析请求里的档位，返回 (state, effort)，state ∈ ("none","on","off")。

    取值来源与参考的 options.reasoning 对应：reasoning_effort /
    reasoningEffort，其次 thinking.type（OpenAI / Anthropic 客户端两套写法）。
    """
    raw = None
    for key in ("reasoning_effort", "reasoningEffort"):
        value = chat_req.get(key)
        if value not in (None, ""):
            raw = value
            break
    if raw in (None, ""):
        thinking = chat_req.get("thinking")
        if isinstance(thinking, dict):
            raw = thinking.get("type")
    if raw in (None, ""):
        return "none", None
    level = str(raw).strip().lower()
    if level in _EFFORT_OFF:
        return "off", None
    if level in _EFFORT_LEVELS:
        return "on", _EFFORT_LEVELS[level]
    if level in _EFFORT_ONLY_ON:
        return "on", None
    # 其余未知取值：按参考 clamp 的保守语义，只开思考、不带档位。
    return "on", None


def _resolve_thinking(chat_req, entry):
    """算出 (enable_thinking, reasoning_effort)。

    * 没点名档位 / 显式关闭 / 模型 is_reasoning 假 -> (False, None)；
    * 点名且模型支持 -> enable_thinking=True；只有模型
      thinking_config.enabled.efforts 存在时才带 reasoning_effort
      （取值映射见 _EFFORT_LEVELS；未知档位只有开关）。
    """
    state, effort = _parse_effort(chat_req)
    if state != "on":
        return False, None
    if not _entry_is_reasoning(entry):
        return False, None
    if effort is not None and _entry_efforts(entry) is not None:
        return True, effort
    return True, None


# ---------------------------------------------------------------------------
# 请求构造（stream.ts reqBody 的逐字段移植）

def _request_max_tokens(chat_req):
    """max_tokens = min(MAX_OUTPUT_TOKENS, chat_req.max_tokens)（>0 才采纳）。"""
    max_tokens = int(wb_qoder.MAX_OUTPUT_TOKENS)
    requested = chat_req.get("max_tokens")
    if isinstance(requested, (int, float)) and not isinstance(requested, bool) \
            and requested > 0:
        max_tokens = min(max_tokens, int(requested))
    return max_tokens


def build_chat_request(chat_req, resolved, identity, session_id=None, data_dir=None):
    """构造一次 Qoder 聊天请求。

    返回 {"url", "headers", "body"(bytes), "effort", "model"}；body 是
    encode_body 之后的字节，COSY 签名与哈希都基于这份字节（stream.ts 同样
    先 qoderEncodeBody 再 buildAuthHeaders）。identity 至少要有
    user_id / access_token；name / email / machineId 可选；realm 取
    identity["realm"]（wb_proxy 的账号对象携带），缺省按国际版。
    """
    chat_req = chat_req if isinstance(chat_req, dict) else {}
    resolved = resolved if isinstance(resolved, dict) else {}
    identity = identity if isinstance(identity, dict) else {}
    entry = resolved.get("entry") if isinstance(resolved.get("entry"), dict) else {}
    model_key = str(resolved.get("key") or "")
    if not model_key:
        raise QoderChatError("Qoder 模型配置缺少上游 key")
    realm = identity.get("realm") or resolved.get("realm") or "intl"

    user_id = str(identity.get("user_id") or "qoder-user")
    access_token = str(identity.get("access_token") or "")
    name = str(identity.get("name") or "")
    email = str(identity.get("email") or "")
    machine = str(identity.get("machineId") or "")

    system_text, messages = _transform_messages(chat_req.get("messages"))
    tools = _transform_tools(chat_req.get("tools"))
    last_user_text = _last_user_text(messages)

    max_tokens = _request_max_tokens(chat_req)
    enable_thinking, effort = _resolve_thinking(chat_req, entry)
    parameters = {"max_tokens": max_tokens, "enable_thinking": enable_thinking}
    if effort is not None:
        parameters["reasoning_effort"] = effort

    # 会话 ID：稳定的用户+模型部分保证上游提示缓存亲和；调用方给了 pi 风格
    # 的 session_id 就拼在后面，否则随机（stream.ts 同款逻辑）。
    stable_part = _stable_hash("qoder-session", user_id, model_key)
    if session_id:
        session_value = "%s-%s" % (stable_part, session_id)
    else:
        session_value = "%s-%s" % (stable_part, uuid.uuid4())

    # 记录 ID 基于清洗后的消息与工具、在上游接受的 max_tokens 上派生；
    # 以系统提示注入之前的 messages 计算（stream.ts 也是在注入前算的）。
    record_id = _stable_chat_record_id(model_key, messages,
                                       tools if tools else None, max_tokens)

    final_messages = ([{"role": "system", "content": system_text}] + messages) \
        if system_text else messages

    request_id = str(uuid.uuid4())
    body = {
        "request_id": request_id,
        "request_set_id": record_id,
        "chat_record_id": record_id,
        "session_id": session_value,
        "stream": True,
        "chat_task": "FREE_INPUT",
        "is_reply": True,
        "is_retry": False,
        "source": 1,
        "version": "3",
        "session_type": "qodercli",
        "agent_id": "agent_common",
        "task_id": "common",
        "code_language": "",
        "chat_prompt": "",
        "image_urls": None,
        "aliyun_user_type": "",
        # 上游忽略顶层 system 字段（模型看不到），系统提示以前导 role:system
        # 消息注入——与 stream.ts 的注释同一依据。
        "system": "",
        "messages": final_messages,
        "tools": tools,
        "parameters": parameters,
        "chat_context": {
            "chatPrompt": "",
            "imageUrls": None,
            "extra": {
                "context": [],
                "modelConfig": {"key": model_key, "is_reasoning": _entry_is_reasoning(entry)},
                "originalContent": last_user_text,
            },
            "features": [],
            "text": last_user_text,
        },
        "model_config": entry,
        "business": {
            "product": "cli",
            "version": "1.0.0",
            "type": "agent",
            "stage": "start",
            "id": str(uuid.uuid4()),
            "name": last_user_text[:30],
            "begin_at": int(time.time() * 1000),
        },
    }

    body_bytes = encode_body(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    config = wb_qoder.realm_config(realm)
    url = config["base_url"] + (
        "algo/api/v2/service/pro/sse/agent_chat_generation"
        "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1")
    cosy = wb_qoder.cosy_headers(body_bytes, url, user_id=user_id,
                                 access_token=access_token, name=name,
                                 email=email, machine=machine, data_dir=data_dir)
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "Accept-Encoding": "identity",
        "X-Model-Key": model_key,
        "X-Model-Source": str(resolved.get("source") or "system"),
    }
    headers.update(cosy)
    return {"url": url, "headers": headers, "body": body_bytes,
            "effort": effort, "model": model_key}


# ---------------------------------------------------------------------------
# 上游 SSE -> OpenAI 增量块（stream.ts 消费循环的移植）

#: 内层没有 id 时的兜底 chunk id。
DEFAULT_CHUNK_ID = "chatcmpl-qoder"

#: finish_reason 映射（上游用 OpenAI 口径；pi 的 toolUse 别名也认）。
_FINISH_REASONS = {
    "stop": "stop",
    "length": "length",
    "content_filter": "content_filter",
    "tool_calls": "tool_calls",
    "tooluse": "tool_calls",
    "tool_use": "tool_calls",
    "function_call": "tool_calls",
}


def _credit_number(value):
    """credits.ts creditNumber：有限且 >= 0 的数值才算数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return value


def _as_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _short_json(value):
    """把任意值折成一小段可读文本（错误消息用）。"""
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)
    return text[:200]


def _load_json_object(text, what):
    try:
        value = json.loads(text)
    except Exception:
        raise QoderChatError("%s 不是合法 JSON" % what)
    if not isinstance(value, dict):
        raise QoderChatError("%s 不是 JSON 对象" % what)
    return value


def _qoder_status_message(envelope, model):
    """把 Qoder 的错误 envelope 折成一句可读的中文报错。

    上游的 403 常把真正的错误码一层层塞进字符串里（body 是 JSON 字符串，
    message 又是 JSON 字符串），这里逐层解开，尽量露出 subCode 与提示。
    典型形态：statusCodeValue=403，内层 subCode=RESOURCE_LIMIT（10605），
    serviceAvailable=false、retryAfterSeconds=30 —— 即模型当前排队/无容量。
    """
    status = envelope.get("statusCodeValue") or envelope.get("statusCode")
    node = envelope.get("body")
    sub = None
    message = None
    for _ in range(4):
        if isinstance(node, str):
            try:
                node = json.loads(node)
            except Exception:
                if message is None:
                    message = node
                break
        if not isinstance(node, dict):
            break
        sub = node.get("subCode") or node.get("code") or sub
        message = node.get("message") or node.get("msg") or message
        node = node.get("message")
    text = _short_json(message) if message else ""
    if ("RESOURCE_LIMIT" in str(sub) or "10605" in str(sub)
            or "RESOURCE_LIMIT" in text):
        return ("Qoder 上游拒绝该请求（状态 %s / %s）：模型「%s」当前资源受限"
                "或正在排队（serviceAvailable=false），通常是该模型在所选区域"
                "暂无可用容量；请稍后重试（上游建议 30 秒），或换用其它 Qoder "
                "模型" % (status, sub or "RESOURCE_LIMIT", model))
    return "Qoder 上游返回状态 %s: %s" % (
        status, text or _short_json(envelope.get("body")))


def _usage_to_openai(raw):
    """把上游 usage 快照塑造成 OpenAI 口径的 usage 块。

    * 标准 token 字段 + prompt_tokens_details / completion_tokens_details
      原样保留（cached_tokens、cache_write_tokens、cacheable_tokens、
      reasoning_tokens 都在里面；wb_proxy._best_cached_tokens 读的就是
      prompt_tokens_details.cached_tokens——cacheable_tokens 只是容量指标，
      按参考不映射到任何写缓存字段）；
    * credit 按 credits.ts 的 chargedCredits 语义：billable===false -> 0，
      billable===true -> credits 值，billable 不是布尔就不写这个字段。
    """
    usage = raw if isinstance(raw, dict) else {}
    out = {
        "prompt_tokens": _as_int(usage.get("prompt_tokens")),
        "completion_tokens": _as_int(usage.get("completion_tokens")),
        "total_tokens": _as_int(usage.get("total_tokens")),
    }
    prompt_details = usage.get("prompt_tokens_details")
    out["prompt_tokens_details"] = dict(prompt_details) \
        if isinstance(prompt_details, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    out["completion_tokens_details"] = dict(completion_details) \
        if isinstance(completion_details, dict) else {}
    if isinstance(usage.get("billable"), bool):
        if usage["billable"]:
            credit = _credit_number(usage.get("credits"))
        else:
            credit = 0
        if credit is not None:
            out["credit"] = credit
    return out


def _map_finish_reason(value):
    text = str(value or "").strip().lower()
    return _FINISH_REASONS.get(text) or (text or None)


def _stream_chunk(chunk_id, model, created, delta=None, finish_reason=None):
    """一个带 choices 的增量块（finish_reason 只在非空时带上）。"""
    choice = {"index": 0, "delta": dict(delta) if delta else {}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model, "choices": [choice]}


def _usage_chunk(chunk_id, model, created, usage):
    return {"id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model, "choices": [], "usage": usage}


def iter_openai_chunks(resp, model):
    """把一次 Qoder SSE 响应解码成 OpenAI chat.completion.chunk 增量块。

    与 stream.ts 消费循环对齐：read(65536) 分块、utf-8 errors="replace"
    解码、按行处理 data: 载荷；[DONE]（裸的或包在 envelope.body 里的）立刻
    停止读循环——上游发完哨兵后可能把连接挂住；envelope 的
    statusCodeValue / 内层 error 抛 QoderChatError（参考对单行 JSON 解析
    失败是静默跳过，这里按网关约定改为抛错）。

    content 恒过 ThinkingTagParser（参考按 thinkingEnabled 开关，本接口没有
    这个选项，一律解析：不含标签的正文原样透传，跨块标签前缀只是短暂扣住），
    reasoning_content 走 stripThinkingTags + stripDsmlResidue（stream.ts
    第 515 行同款）；两部分再各过一次 stripDsmlResidue。tool_calls 输出
    OpenAI 增量形状：首个块带 id/name/type，之后只带 arguments 增量。

    流末（含 [DONE]）：先把解析器残留吐出，缺失时补一个收敛的
    finish_reason 块（有工具调用 -> tool_calls，否则 stop），最后按需产出
    唯一一个 {"choices": [], "usage": {...}} 块。本函数不产出 [DONE]，
    wb_proxy 自己写哨兵。
    """
    model = "" if model is None else str(model)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    created = int(time.time())
    chunk_id = DEFAULT_CHUNK_ID
    parser = ThinkingTagParser()
    tool_states = {}   # 上游 tool_calls[].index -> {id, name, started, pending}
    tool_started = False
    finish_emitted = False
    usage_snapshot = None
    buffer = ""
    saw_done = False

    while not saw_done:
        try:
            # read1()：有就只取「已经到达」的那一块（chunked 下一个 chunk 的
            # 数据），不等凑满 64KB。read(65536) 会一直阻塞到攒够 64KB 或
            # 连接关闭 —— 上游 0.2 秒就把「排队中（10605 RESOURCE_LIMIT）」的
            # 错误信封送来了，却要等上游断开（实测 3 分钟）才看得见，客户端
            # 那边就是干等三分钟才报错。
            raw = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        except QoderChatError:
            raise
        except IncompleteRead as exc:
            # 上游用 chunked 传输时，遇到 403 / 限流这类错误会先把 SSE 信封
            # 写出来，却不发终止块就断开；http.client 在读完信封前抛
            # IncompleteRead，把真正的错误码（如 10605 RESOURCE_LIMIT）藏在
            # exc.partial 里。这里先把 partial 里的 data: 行解出来，把上游的
            # 真实报错翻出来，避免只看到 "IncompleteRead(...)"。
            partial = getattr(exc, "partial", b"") or b""
            if isinstance(partial, str):
                partial = partial.encode("utf-8", "replace")
            text = buffer + partial.decode("utf-8", "replace")
            for line in text.split("\n"):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    envelope = _load_json_object(data_str, "Qoder 流数据")
                except QoderChatError:
                    continue
                status = envelope.get("statusCodeValue")
                if status and status != 200:
                    raise QoderChatError(_qoder_status_message(envelope, model))
                inner = envelope.get("body")
                if isinstance(inner, dict) and inner.get("error"):
                    raise QoderChatError("Qoder 上游返回错误: %s"
                                         % _short_json(inner.get("error")))
            if not partial:
                raise QoderChatError(
                    "Qoder 上游在 %.0f 秒内没有返回任何数据：该模型可能资源受限"
                    "或正在排队（10605 RESOURCE_LIMIT），请稍后重试或换用其它"
                    " Qoder 模型" % max(0, time.time() - created))
            raise QoderChatError(
                "Qoder 上游返回了不完整的响应（分块传输被中断），请稍后重试")
        except TimeoutError:
            raise QoderChatError(
                "Qoder 上游在 %.0f 秒内没有返回任何数据：该模型可能资源受限"
                "或正在排队（10605 RESOURCE_LIMIT），请稍后重试或换用其它"
                " Qoder 模型" % max(0, time.time() - created))
        except Exception as exc:
            raise QoderChatError("Qoder 上游流读取失败: %s" % exc)
        if not raw:
            break
        if isinstance(raw, bytes):
            buffer += decoder.decode(raw)
        else:
            buffer += str(raw)
        while True:
            line_end = buffer.find("\n")
            if line_end == -1:
                break
            line = buffer[:line_end].strip()
            buffer = buffer[line_end + 1:]
            if not line or line.startswith(":"):
                continue          # 空行 / SSE 注释
            if not line.startswith("data:"):
                continue          # event: / id: 等，参考同样只认 data:
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                saw_done = True
                break
            envelope = _load_json_object(data_str, "Qoder 流数据")
            status = envelope.get("statusCodeValue")
            if status and status != 200:
                raise QoderChatError(_qoder_status_message(envelope, model))
            inner_raw = envelope.get("body")
            # 哨兵也会以 envelope.body == "[DONE]" 的形态出现，同样结束。
            if inner_raw == "[DONE]":
                saw_done = True
                break
            if inner_raw in (None, ""):
                continue
            if isinstance(inner_raw, str):
                inner = _load_json_object(inner_raw, "Qoder 响应体")
            elif isinstance(inner_raw, dict):
                inner = inner_raw
            else:
                raise QoderChatError("Qoder 上游返回了意外的 envelope")
            if inner.get("error"):
                raise QoderChatError("Qoder 上游返回错误: %s"
                                     % _short_json(inner.get("error")))
            if inner.get("id"):
                chunk_id = str(inner["id"])
            if isinstance(inner.get("usage"), dict):
                # usage 是快照不是增量：取最新的那一份（参考逐字段替换）。
                usage_snapshot = inner["usage"]
            choices = inner.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta")
            if isinstance(delta, dict):
                # 1) reasoning_content：参考先 stripThinkingTags 再 stripDsmlResidue
                reasoning_raw = delta.get("reasoning_content")
                if isinstance(reasoning_raw, str) and reasoning_raw:
                    text = strip_dsml_residue(strip_thinking_tags(reasoning_raw))
                    if text:
                        yield _stream_chunk(chunk_id, model, created,
                                            {"reasoning_content": text})
                # 2) content：标签块转思考通道，其余文本原样
                content_raw = delta.get("content")
                if isinstance(content_raw, str) and content_raw:
                    for kind, text in parser.process_chunk(content_raw):
                        text = strip_dsml_residue(text)
                        if not text:
                            continue
                        key = "reasoning_content" if kind == "thinking" else "content"
                        yield _stream_chunk(chunk_id, model, created, {key: text})
                # 3) tool_calls：识别到就开块（哪怕没有参数增量），
                #    与 stream.ts「id/name 一到就开块」一致，避免无参工具
                #    调用被吞成空回合。
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index")
                        if not isinstance(idx, int) or isinstance(idx, bool):
                            idx = 0
                        state = tool_states.get(idx)
                        if state is None:
                            state = {"id": "", "name": "", "started": False, "pending": ""}
                            tool_states[idx] = state
                        tc_id = tc.get("id")
                        if tc_id:
                            state["id"] = wire_tool_call_id(str(tc_id))
                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                        tc_name = fn.get("name")
                        if tc_name:
                            state["name"] = str(tc_name)
                        if not state["started"] and (state["id"] or state["name"]):
                            state["started"] = True
                            tool_started = True
                            yield _stream_chunk(chunk_id, model, created, {"tool_calls": [{
                                "index": idx, "id": state["id"], "type": "function",
                                "function": {"name": state["name"], "arguments": ""},
                            }]})
                            if state["pending"]:
                                yield _stream_chunk(chunk_id, model, created, {"tool_calls": [{
                                    "index": idx,
                                    "function": {"arguments": state["pending"]},
                                }]})
                                state["pending"] = ""
                        arguments = fn.get("arguments")
                        if arguments is not None and arguments != "":
                            if not isinstance(arguments, str):
                                arguments = _json_compact(arguments)
                            if state["started"]:
                                yield _stream_chunk(chunk_id, model, created, {"tool_calls": [{
                                    "index": idx,
                                    "function": {"arguments": arguments},
                                }]})
                            else:
                                state["pending"] += arguments
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                finish_emitted = True
                yield _stream_chunk(chunk_id, model, created, None,
                                    _map_finish_reason(finish_reason))

    # 流末：解析器残留（参考 thinkingParser.finalize() 的同款收尾）。
    for kind, text in parser.finalize():
        text = strip_dsml_residue(text)
        if not text:
            continue
        key = "reasoning_content" if kind == "thinking" else "content"
        yield _stream_chunk(chunk_id, model, created, {key: text})

    # 上游没给过 finish_reason 时收敛一个（参考流末把 stopReason 收敛为
    # toolUse / stop；工具调用块只要真的开过就算 tool_calls）。
    if not finish_emitted:
        yield _stream_chunk(chunk_id, model, created, None,
                            "tool_calls" if tool_started else "stop")

    if usage_snapshot is not None:
        yield _usage_chunk(chunk_id, model, created, _usage_to_openai(usage_snapshot))
