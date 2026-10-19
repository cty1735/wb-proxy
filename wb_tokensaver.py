# -*- coding: utf-8 -*-
"""省 Token 中间件：工具输出压缩（RTK）+ 提示词瘦身 + 本地应答。

对外的三个入口都很小，全部**失败开放**——任何异常都退回原始请求：

* :func:`compress_payload` —— 就地重写历史里的工具结果（三种协议形状）；
* :func:`apply_prompt`     —— 注入/撤销提示词瘦身（见 wb_tokensaver_prompts）；
* :func:`local_answer`     —— 网关进程内直接应答 5 类"不需要上游"的请求。

统计单独落 ``<usage_dir>/tokensaver.jsonl``，**不进**主用量管线（usage.jsonl
被大量扫描器逐字段解析，往那里加字段的连带面太大）。

本模块不 import wb_proxy（避免循环依赖），只依赖标准库 + 两个同级纯文本模块；
配置以普通 dict 传入，方便单测。
"""

import json
import os
import re
import threading
import time

import wb_tokensaver_prompts as prompts
import wb_tokensaver_rtk as rtk

#: 本地应答的种类（与 settings 里的开关一一对应）。
LOCAL_KINDS = ("quota", "shell", "title", "suggest", "paths")

DEFAULT_CONFIG = {
    "enabled": True,
    "rtk": True,
    "prompt_family": "off",
    "prompt_level": "off",
    "local": {kind: False for kind in LOCAL_KINDS},
}

_STATS_LOCK = threading.Lock()

#: 注入检测：命令前缀抽取时，出现这些字符就判定为命令注入，拒绝本地应答。
_INJECTION_RE = re.compile(r"[;&|`$(){}<>\n]")


def normalize_config(raw):
    """把任意来源的配置归一成一份完整、可信的 dict。"""
    raw = raw if isinstance(raw, dict) else {}
    local_raw = raw.get("local") if isinstance(raw.get("local"), dict) else {}
    return {
        "enabled": raw.get("enabled", DEFAULT_CONFIG["enabled"]) is not False,
        "rtk": raw.get("rtk", DEFAULT_CONFIG["rtk"]) is not False,
        "prompt_family": prompts.normalize_family(raw.get("prompt_family")),
        "prompt_level": prompts.normalize_level(
            raw.get("prompt_family"), raw.get("prompt_level")),
        "local": {kind: local_raw.get(kind) is True for kind in LOCAL_KINDS},
    }


# ---------------------------------------------------------------------------
# 工具输出压缩
# ---------------------------------------------------------------------------

class _Acc(object):
    """一次请求的压缩统计。"""

    __slots__ = ("before", "after", "blocks")

    def __init__(self):
        self.before = 0
        self.after = 0
        self.blocks = 0

    def add(self, original, replacement):
        self.before += len(original.encode("utf-8"))
        self.after += len(replacement.encode("utf-8"))
        self.blocks += 1


def _compress_text_field(container, key, acc, sub_key="text"):
    """压缩 ``container[key]``：字符串直接压，文本块列表逐块压。"""
    value = container.get(key)
    if isinstance(value, str):
        new = rtk.compress_tool_output(value)
        if new is not None:
            container[key] = new
            acc.add(value, new)
    elif isinstance(value, list):
        for block in value:
            if isinstance(block, dict) and isinstance(block.get(sub_key), str):
                original = block[sub_key]
                new = rtk.compress_tool_output(original)
                if new is not None:
                    block[sub_key] = new
                    acc.add(original, new)


def _compress_chat(payload, acc):
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict) and str(msg.get("role") or "") in ("tool", "function"):
            _compress_text_field(msg, "content", acc)


def _compress_anthropic(payload, acc):
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    _compress_text_field(block, "content", acc)


def _compress_responses(payload, acc):
    items = payload.get("input")
    if not isinstance(items, list):
        return
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            _compress_text_field(item, "output", acc)


def compress_payload(payload, protocol, config):
    """压缩请求里的工具结果，返回 ``(payload, stats_or_None)``。

    只改工具结果，不动普通消息；``protocol`` 取 ``chat`` / ``anthropic`` /
    ``responses``，决定去哪种形状里找工具结果。
    """
    if not isinstance(payload, dict) or not config.get("enabled") or not config.get("rtk"):
        return payload, None
    acc = _Acc()
    try:
        if protocol == "anthropic":
            _compress_anthropic(payload, acc)
        elif protocol == "responses":
            _compress_responses(payload, acc)
        else:
            _compress_chat(payload, acc)
    except Exception:
        return payload, None
    if not acc.blocks:
        return payload, None
    stats = {
        "at": time.time(),
        "kind": "rtk",
        "model": str(payload.get("model") or ""),
        "protocol": protocol,
        "before": acc.before,
        "after": acc.after,
        "saved": max(0, acc.before - acc.after),
        "blocks": acc.blocks,
    }
    return payload, stats


# ---------------------------------------------------------------------------
# 提示词瘦身
# ---------------------------------------------------------------------------

def apply_prompt(payload, protocol, config):
    """按配置注入/撤销提示词瘦身，返回 ``(payload, changed)``。

    ``off`` 不只是"不注入"，还要把之前注入过的那条撤掉——所以这里**不能**在
    ``off`` 时提前返回，必须走一遍撤销路径（幂等：没注入过就是空操作）。
    """
    if not isinstance(payload, dict) or not config.get("enabled"):
        return payload, False
    family = config.get("prompt_family") or "off"
    level = config.get("prompt_level") or "off"
    try:
        if protocol == "anthropic":
            return _apply_prompt_anthropic(payload, family, level)
        return _apply_prompt_chat(payload, family, level)
    except Exception:
        return payload, False


def _apply_prompt_chat(payload, family, level):
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload, False
    new, changed = prompts.apply_nudge(messages, family, level)
    if changed:
        payload["messages"] = new
    return payload, changed


def _apply_prompt_anthropic(payload, family, level):
    """Anthropic 的 system 可能是字符串或文本块数组，注入时要保持形状。"""
    text = prompts.nudge_text(family, level)
    system = payload.get("system")
    marker = prompts.MARK
    # 先撤销：删掉任何以标记开头的 system 文本块。
    if isinstance(system, list):
        kept = [b for b in system
                if not (isinstance(b, dict) and str(b.get("text") or "").startswith(marker))]
        changed = len(kept) != len(system)
        if not text:
            if changed:
                payload["system"] = kept
            return payload, changed
        kept.insert(0, {"type": "text", "text": "%s %s" % (marker, text)})
        payload["system"] = kept
        return payload, True
    if isinstance(system, str):
        if system.startswith(marker):
            if not text:
                payload.pop("system", None)
                return payload, True
            new_system = "%s %s" % (marker, text)
            payload["system"] = new_system
            return payload, new_system != system
        if not text:
            return payload, False
        payload["system"] = "%s %s\n%s" % (marker, text, system)
        return payload, True
    # 没有 system：插一条字符串。
    if text:
        payload["system"] = "%s %s" % (marker, text)
        return payload, True
    return payload, False


# ---------------------------------------------------------------------------
# 本地应答
# ---------------------------------------------------------------------------

def _block_text(value, sub_key="text"):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and isinstance(block.get(sub_key), str):
                parts.append(block[sub_key])
            elif isinstance(block, dict) and isinstance(block.get("content"), str):
                parts.append(block["content"])
        return "\n".join(parts)
    return ""


def _messages_view(payload, protocol):
    """把请求拉平成 ``[(role, text), ...]``，屏蔽三种协议的形状差异。"""
    out = []
    if protocol == "anthropic":
        system = payload.get("system")
        system_text = _block_text(system)
        if system_text:
            out.append(("system", system_text))
        for msg in payload.get("messages") or []:
            if isinstance(msg, dict):
                out.append((str(msg.get("role") or ""), _block_text(msg.get("content"))))
        return out
    for msg in payload.get("messages") or []:
        if isinstance(msg, dict):
            out.append((str(msg.get("role") or ""), _block_text(msg.get("content"))))
    return out


def _last_user_text(view):
    for role, text in reversed(view):
        if role == "user":
            return text
    return ""


def _system_text(view):
    return "\n".join(text for role, text in view if role == "system")


def _detect_shell_prefix(user_text):
    """``<policy_spec>`` + ``Command:`` -> 命令的首个词；疑似注入则返回 None。"""
    marker = "Command:"
    idx = user_text.rfind(marker)
    if idx < 0:
        return None
    tail = user_text[idx + len(marker):].strip()
    command = tail.splitlines()[0].strip() if tail else ""
    if not command:
        return None
    if _INJECTION_RE.search(command):
        return None
    return command.split()[0]


def local_answer(payload, protocol, config):
    """命中就返回 ``(kind, text)``，否则 ``None``。

    所有种类默认 **off**：它们是给特定客户端（Claude Code 等）的内部探测用的，
    对通用客户端形同误伤，所以必须显式打开。命中的请求**不发上游**。
    """
    if not isinstance(payload, dict) or not config.get("enabled"):
        return None
    local = config.get("local") or {}
    if not any(local.values()):
        return None
    try:
        view = _messages_view(payload, protocol)
        user = _last_user_text(view)
        system = _system_text(view)
    except Exception:
        return None
    if not user and not system:
        return None
    low_user = user.lower()
    low_sys = system.lower()

    if local.get("quota") and payload.get("max_tokens") == 1 and "quota" in low_user:
        return ("quota", "Quota check passed.")
    if local.get("shell") and "<policy_spec>" in low_sys and "command:" in low_user:
        prefix = _detect_shell_prefix(user)
        if prefix:
            return ("shell", prefix)
        return None
    if local.get("paths") and ("<filepaths>" in low_sys or "extract file path" in low_sys):
        return ("paths", "<filepaths>\n</filepaths>")
    if local.get("title") and ("conversation title" in low_sys
                               or "generate a title" in low_sys
                               or "summarize the conversation into a title" in low_sys):
        return ("title", "Conversation")
    if local.get("suggest") and ("<suggestion" in low_sys
                                 or "inline suggestion" in low_sys):
        return ("suggest", "")
    return None


def local_stats(model, protocol, kind):
    return {"at": time.time(), "kind": "local", "model": str(model or ""),
            "protocol": protocol, "answer": kind}


# ---------------------------------------------------------------------------
# 统计文件
# ---------------------------------------------------------------------------

def stats_path(usage_dir):
    return os.path.join(usage_dir or ".", "tokensaver.jsonl")


def record_stats(usage_dir, stats):
    """把一行统计追加到 tokensaver.jsonl；失败静默（统计不该影响请求）。"""
    if not stats:
        return
    try:
        os.makedirs(usage_dir, exist_ok=True)
        line = json.dumps(stats, ensure_ascii=False)
        with _STATS_LOCK:
            with open(stats_path(usage_dir), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def _read_tail(path, max_bytes=4 * 1024 * 1024):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # 丢掉可能截断的首行
            data = fh.read()
        return data.decode("utf-8", "replace")
    except Exception:
        return ""


def stats_summary(usage_dir, max_bytes=4 * 1024 * 1024, top_models=8):
    """汇总统计：总量、节省率、按天、按模型。"""
    text = _read_tail(stats_path(usage_dir), max_bytes=max_bytes)
    totals = {"calls": 0, "before": 0, "after": 0, "saved": 0,
              "blocks": 0, "local": 0}
    by_day = {}
    by_model = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("kind")
        stamp = row.get("at")
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(float(stamp)))
        except Exception:
            day = ""
        bucket = by_day.setdefault(day, {"calls": 0, "saved": 0, "before": 0, "local": 0})
        if kind == "local":
            totals["local"] += 1
            bucket["local"] += 1
            continue
        before = int(row.get("before") or 0)
        after = int(row.get("after") or 0)
        saved = int(row.get("saved") or max(0, before - after))
        totals["calls"] += 1
        totals["before"] += before
        totals["after"] += after
        totals["saved"] += saved
        totals["blocks"] += int(row.get("blocks") or 0)
        bucket["calls"] += 1
        bucket["saved"] += saved
        bucket["before"] += before
        model = str(row.get("model") or "")
        m = by_model.setdefault(model, {"calls": 0, "saved": 0, "before": 0})
        m["calls"] += 1
        m["saved"] += saved
        m["before"] += before
    before = totals["before"]
    totals["ratio"] = round(totals["saved"] / before, 4) if before else 0.0
    days = [dict(day=k, **v) for k, v in sorted(by_day.items(), reverse=True) if k]
    models = sorted(by_model.items(), key=lambda kv: kv[1]["saved"], reverse=True)
    models = [dict(model=k, **v) for k, v in models[:top_models]]
    return {"totals": totals, "days": days, "models": models,
            "file": stats_path(usage_dir)}


# ---------------------------------------------------------------------------
# 一个"假的"上游：把一段固定文本伪装成 OpenAI 流
# ---------------------------------------------------------------------------

class LocalUpstreamAdapter(object):
    """把一段文本变成下游管线认的 ``data: {...}`` 字节源。

    只给本地应答的流式路径用（Anthropic 的 ``stream_messages_events`` 需要一个
    带 ``read1()`` 的源）。它不联网、不记账，产出即结束。
    """

    def __init__(self, text, model):
        self._pending = self._build(text or "", model).encode("utf-8")

    @staticmethod
    def _chunk(model, delta, finish=None, usage=None):
        obj = {
            "id": "chatcmpl-local",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage is not None:
            obj["usage"] = usage
        return "data: %s\n\n" % json.dumps(obj, ensure_ascii=False)

    @classmethod
    def _build(cls, text, model):
        parts = [cls._chunk(model, {"role": "assistant"})]
        if text:
            parts.append(cls._chunk(model, {"content": text}))
        parts.append(cls._chunk(model, {}, finish="stop", usage={
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }))
        return "".join(parts)

    def read1(self, size=-1):
        if not self._pending:
            return b""
        if size is None or size < 0:
            data, self._pending = self._pending, b""
            return data
        data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def read(self, size=-1):
        return self.read1(size)

    def close(self):
        self._pending = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False
