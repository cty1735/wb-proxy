# -*- coding: utf-8 -*-
"""省 Token 提示词瘦身（RTK 的姊妹功能）。

两族、各自若干档位，把「助手该怎么说话」这件事写进 system 指令：

* ``caveman``  —— 电报体：话少、不寒暄，事实/代码/命令一个不少；
* ``ponytail`` —— YAGNI 最小实现：只给能跑起来的最少代码，不过度设计。

全部档位默认 ``off``：它改变的是模型说话的方式，不是数据本身，所以必须由使用者
显式打开。注入是**幂等**的（靠一个固定标记识别自己写过的那条 system 消息），
也可以整体撤销（:func:`strip_nudge`）。

本模块不 import 项目内其它模块，只做纯文本，方便单测。
"""

#: 注入的 system 消息用它开头，重复调用只保留一条。
MARK = "[wb-proxy:tokensaver]"

CAVEMAN_LEVELS = ("off", "lite", "full", "ultra", "wenyan-lite", "wenyan",
                  "wenyan-ultra")
PONYTAIL_LEVELS = ("off", "lite", "full", "ultra")

_FAMILY_LEVELS = {
    "caveman": CAVEMAN_LEVELS,
    "ponytail": PONYTAIL_LEVELS,
}

_FAMILY_LABELS = {
    "caveman": "电报体（话少、不寒暄）",
    "ponytail": "YAGNI 最小实现（能跑就好）",
}

_LEVEL_LABELS = {
    "off": "关闭",
    "lite": "轻",
    "full": "标准",
    "ultra": "极简",
    "wenyan-lite": "文言·轻",
    "wenyan": "文言",
    "wenyan-ultra": "文言·极简",
}

_CAVEMAN = {
    "lite": ("Be terse. Drop filler, pleasantries and hedging. Keep every fact, "
             "code block, command and file path exactly as it is."),
    "full": ("Answer like a telegram: short words, no scaffolding, no "
             "pleasantries. Never omit facts, code, commands or file paths; "
             "keep all identifiers and numbers verbatim."),
    "ultra": ("Answer in the fewest words possible, telegraphic fragments. "
              "Never omit facts, code, commands or file paths."),
    "wenyan-lite": ("以简省文言作答，去虚词寒暄；事实、代码、命令与路径一字不删。"),
    "wenyan": ("以文言作答，务求简省；事实、代码、命令与路径全部保留，不得省略。"),
    "wenyan-ultra": ("文言极简作答，一如尺牍；事实、代码、命令、路径一字不省。"),
}

_PONYTAIL = {
    "lite": ("Prefer the smallest change that works. Avoid speculative "
             "abstraction and unrequested features."),
    "full": ("YAGNI: implement only what was asked, in the simplest way that "
             "works. No speculative generality, no extra options, no unasked "
             "refactors."),
    "ultra": ("This is a field repair, not a reconstruction. Ship the minimal "
              "working change; no abstraction, no config, no extras."),
}

_TEXTS = {
    "caveman": _CAVEMAN,
    "ponytail": _PONYTAIL,
}


def family_levels(family):
    return _FAMILY_LEVELS.get(str(family or "").strip().lower(), ())


def family_label(family):
    return _FAMILY_LABELS.get(str(family or "").strip().lower(), "")


def level_label(level):
    return _LEVEL_LABELS.get(str(level or "").strip().lower(), "")


def normalize_family(value):
    text = str(value or "").strip().lower()
    return text if text in _FAMILY_LEVELS else "off"


def normalize_level(family, value):
    family = normalize_family(family)
    if family == "off":
        return "off"
    text = str(value or "").strip().lower()
    return text if text in _FAMILY_LEVELS[family] else "off"


def nudge_text(family, level):
    """该族该档要注入的指令文本；``off`` 或未知组合返回空串。"""
    family = normalize_family(family)
    level = normalize_level(family, level)
    if family == "off" or level == "off":
        return ""
    return _TEXTS.get(family, {}).get(level, "")


def _content_text(message):
    """把一条消息的 content 拉平成字符串（只看文本块，忽略图片等）。"""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def strip_nudge(messages):
    """移除本模块注入过的所有 system 消息（保留顺序）。"""
    if not isinstance(messages, list):
        return messages
    return [m for m in messages
            if not (isinstance(m, dict)
                    and str(m.get("role") or "") == "system"
                    and _content_text(m).startswith(MARK))]


def apply_nudge(messages, family, level):
    """注入提示词瘦身指令，返回 ``(messages, changed)``。

    幂等：先撤掉之前注入的那条，再决定是否重新注入；``off`` 时等价于纯撤销。
    """
    text = nudge_text(family, level)
    if not isinstance(messages, list):
        return messages, False
    cleaned = strip_nudge(messages)
    if not text:
        return (cleaned, cleaned is not messages) if cleaned != messages else (messages, False)
    body = "%s %s" % (MARK, text)
    return [{"role": "system", "content": body}] + cleaned, True
