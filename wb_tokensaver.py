# -*- coding: utf-8 -*-
"""wb_tokensaver.py — 省 token：提示词压缩、工具输出压缩（RTK）、本地应答。

三块各管一段，但共用一个总开关（``settings.json`` 的 ``token_saver.enabled``）：

  1. **提示词压缩**（输出侧）：往 system 里追加一条写作指令。Caveman 管说话
     方式（关闭 / 轻 / 全 / 极 / 文言轻 / 文言 / 文言极），Ponytail 管代码
     体积（YAGNI，关闭 / 轻 / 全 / 极）。这是唯一能减少**输出** Token 的一
     块 —— 它改变的是模型怎么写，不是我们转发了什么。默认关闭：改说话方式
     是要人自己点头的事。

  2. **RTK 工具输出压缩**（输入侧）：只动 role=tool 的讯息，按内容自动识别
     git log / diff / status、构建日志、grep、find、tree、ls、路径清单、带
     行号的文件转储、重复日志行，最后兜底「保头留尾」。安全边界照 9Router /
     rtk-ai 的口径：报错类结果原样保留、小于 500 字节不碰、超过 10 MiB 不
     碰、过滤器抛异常回退原文、压缩结果不比原文小就回退 —— 压缩永远不会把
     正文变大或变空。

  3. **本地应答**：五类「答案就在请求体里」的探测（额度、命令前缀、会话标
     题、建议模式、文件路径提取）就地回掉，不占上游额度、不发上游请求。默
     认关闭：错杀一条真请求比省一次调用贵得多，打开后也只认窄到不能再窄的
     形状（单条 user 讯息、不带 tools、不在一段对话中间）。

统计只按字节数记本机实际发生的压缩，写在 ``accounts/token-saver-stats.json``；
不估算、不上报。每个请求可带 ``X-WB-Token-Saver: off`` 单条绕过全部省 token
逻辑（排查「是不是压坏了上下文」用）。
"""

import json
import os
import re
import threading
import time

import wb_settings

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

CONFIG_KEY = "token_saver"
STATS_FILE = "token-saver-stats.json"

CAVEMAN_LEVELS = ("off", "lite", "full", "ultra",
                  "wenyan-lite", "wenyan", "wenyan-ultra")
PONYTAIL_LEVELS = ("off", "lite", "full", "ultra")
LOCAL_KINDS = ("quota", "command_prefix", "title", "suggestion", "file_path")

# 总开关默认开、两条压缩线默认开、提示词与本地应答默认关：默认行为只在
# 「上游看到的正文更小」这一侧变化，不改变模型怎么说话，也不替客户端答题。
DEFAULT_CONFIG = {
    "enabled": True,
    "prompt": {"caveman": "off", "ponytail": "off"},
    "rtk": True,
    "local": {
        "enabled": False,
        "quota": True,
        "command_prefix": True,
        "title": True,
        "suggestion": True,
        "file_path": True,
    },
}


def _level(value, allowed):
    text = str(value or "").strip().lower()
    return text if text in allowed else "off"


def _flag(value, default):
    """Read a switch that a human may have hand-edited as a string.

    The panel always sends JSON booleans, but settings.json is also edited by
    hand, and ``"false"`` as a string is truthy in Python. Anything unreadable
    falls back to the shipped default rather than to a silent enable.
    """
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return value is True


def normalize_config(raw):
    """Coerce anything hand-written or half-saved into the full shape.

    Unknown keys are dropped on purpose: the panel only ever sends the fields
    it owns, and a typo in ``settings.json`` must not become a new switch the
    code silently starts honouring.
    """
    raw = raw if isinstance(raw, dict) else {}
    prompt = raw.get("prompt") if isinstance(raw.get("prompt"), dict) else {}
    local = raw.get("local") if isinstance(raw.get("local"), dict) else {}
    cfg = {
        "enabled": _flag(raw.get("enabled"), True),
        "prompt": {
            "caveman": _level(prompt.get("caveman"), CAVEMAN_LEVELS),
            "ponytail": _level(prompt.get("ponytail"), PONYTAIL_LEVELS),
        },
        "rtk": _flag(raw.get("rtk"), True),
        "local": {"enabled": _flag(local.get("enabled"), False)},
    }
    for kind in LOCAL_KINDS:
        cfg["local"][kind] = _flag(local.get(kind), True)
    return cfg


def config(accounts_dir):
    """Current token-saver settings, fail-open to the shipped defaults."""
    try:
        stored = wb_settings.load(accounts_dir).get(CONFIG_KEY)
    except Exception:
        stored = None
    return normalize_config(stored)


def set_config(accounts_dir, patch):
    """Merge and persist a panel patch. Returns the stored config."""
    current = config(accounts_dir)
    patch = patch if isinstance(patch, dict) else {}
    merged = {
        "enabled": patch.get("enabled", current["enabled"]),
        "rtk": patch.get("rtk", current["rtk"]),
        "prompt": dict(current["prompt"]),
        "local": dict(current["local"]),
    }
    if isinstance(patch.get("prompt"), dict):
        merged["prompt"].update(patch["prompt"])
    if isinstance(patch.get("local"), dict):
        merged["local"].update(patch["local"])
    clean = normalize_config(merged)
    with wb_settings._lock:
        data = wb_settings.load(accounts_dir)
        data[CONFIG_KEY] = clean
        wb_settings.save(accounts_dir, data)
    return clean


# ---------------------------------------------------------------------------
# 单条请求的逃生口
# ---------------------------------------------------------------------------

SAVER_HEADER = "X-WB-Token-Saver"
_ALSO_READ = ("X-Token-Saver",)
_OFF_VALUES = ("off", "0", "false", "no")
_local = threading.local()


def begin_request(headers):
    """Read the per-request escape hatch before anything is compressed.

    The handler calls this once per request; the thread-local is what lets
    ``build_upstream_body()`` (which never sees the headers) honour it.
    """
    value = ""
    try:
        for name in (SAVER_HEADER,) + _ALSO_READ:
            value = str(headers.get(name) or "").strip().lower()
            if value:
                break
    except Exception:
        value = ""
    _local.skip = value in _OFF_VALUES


def end_request():
    _local.skip = False


def disabled_for_request():
    return bool(getattr(_local, "skip", False))


# ---------------------------------------------------------------------------
# 统计（按字节，只记本机）
# ---------------------------------------------------------------------------

_stats_lock = threading.Lock()
_pending = {}          # path -> counters not yet on disk
_last_flush = {}       # path -> unix time of the last flush
FLUSH_INTERVAL = 2.0   # 热路径上每 2 秒最多写一次盘


def _empty_stats():
    stats = {"before": 0, "after": 0, "saved": 0, "blocks": 0,
             "calls": 0, "injections": 0,
             "local": dict((kind, 0) for kind in LOCAL_KINDS),
             "started_at": time.time(), "updated_at": 0}
    return stats


def _stats_path(accounts_dir):
    return os.path.join(accounts_dir or ".", STATS_FILE)


def _read_stats(accounts_dir):
    data = _empty_stats()
    try:
        with open(_stats_path(accounts_dir), encoding="utf-8") as fh:
            parsed = json.load(fh)
    except Exception:
        return data
    if not isinstance(parsed, dict):
        return data
    for key in ("before", "after", "saved", "blocks", "calls", "injections"):
        try:
            data[key] = int(parsed.get(key) or 0)
        except Exception:
            data[key] = 0
    local = parsed.get("local")
    if isinstance(local, dict):
        for kind in LOCAL_KINDS:
            try:
                data["local"][kind] = int(local.get(kind) or 0)
            except Exception:
                data["local"][kind] = 0
    for key in ("started_at", "updated_at"):
        try:
            data[key] = float(parsed.get(key) or 0) or data[key]
        except Exception:
            pass
    return data


def _write_stats(accounts_dir, stats):
    path = _stats_path(accounts_dir)
    try:
        os.makedirs(accounts_dir or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(stats, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _flush_locked(accounts_dir):
    path = _stats_path(accounts_dir)
    pending = _pending.pop(path, None)
    _last_flush[path] = time.time()
    if not pending:
        return
    stats = _read_stats(accounts_dir)
    for key in ("before", "after", "saved", "blocks", "calls", "injections"):
        stats[key] = int(stats.get(key) or 0) + int(pending.get(key) or 0)
    for kind, count in (pending.get("local") or {}).items():
        if kind in stats["local"]:
            stats["local"][kind] += int(count or 0)
    stats["updated_at"] = time.time()
    _write_stats(accounts_dir, stats)


def flush(accounts_dir):
    with _stats_lock:
        _flush_locked(accounts_dir)


def stats(accounts_dir):
    """Counters plus the derived percentage the panel prints."""
    with _stats_lock:
        _flush_locked(accounts_dir)
        data = _read_stats(accounts_dir)
    before = int(data.get("before") or 0)
    # Derive the saving instead of trusting the stored counter: a crash between
    # the before/after accumulation and the saved write would otherwise leave a
    # block counted but invisible.
    data["saved"] = max(0, before - int(data.get("after") or 0))
    data["percent"] = (data["saved"] * 100.0 / before) if before else 0.0
    return data


def reset_stats(accounts_dir):
    with _stats_lock:
        _pending.pop(_stats_path(accounts_dir), None)
        data = _empty_stats()
        data["updated_at"] = time.time()
        _write_stats(accounts_dir, data)
    return data


def _note(accounts_dir, *, calls=0, before=0, after=0, blocks=0,
          injections=0, local_kind=None):
    if not accounts_dir:
        # No accounts dir means no place to put the counters - never drop a
        # stray token-saver-stats.json into the process working directory.
        return
    path = _stats_path(accounts_dir)
    with _stats_lock:
        pending = _pending.setdefault(path, {
            "calls": 0, "before": 0, "after": 0, "saved": 0, "blocks": 0,
            "injections": 0, "local": {}})
        pending["calls"] += calls
        pending["before"] += before
        pending["after"] += after
        pending["saved"] += max(0, before - after)
        pending["blocks"] += blocks
        pending["injections"] += injections
        if local_kind:
            pending["local"][local_kind] = pending["local"].get(local_kind, 0) + 1
        due = time.time() - _last_flush.get(path, 0) >= FLUSH_INTERVAL
        if due:
            _flush_locked(accounts_dir)


# ---------------------------------------------------------------------------
# 提示词压缩
# ---------------------------------------------------------------------------

MARKER = "[token-saver]"

CAVEMAN_TEXT = {
    "lite": "回答用短句：去掉客套、问题复述与收尾语；技术内容、代码与命令保持完整。",
    "full": "电报体：只给结论与代码。去掉冠词、客套、复述、过渡句与收尾语，能一行就不写一段。",
    "ultra": "极简体：只给结论、代码与命令，可用碎片短语；不解释背景，不给未被问的建议。",
    "wenyan-lite": "尽量用浅近文言与短句作答；技术名词、代码、命令与报错照原样保留。",
    "wenyan": "以文言作答，字少意赅；代码、命令、路径与报错原样保留。",
    "wenyan-ultra": "文言极简：只留结论与代码，不解释、不客套。",
}

PONYTAIL_TEXT = {
    "lite": "最小实现：只改被要求的地方，不顺手重构，不加未被要求的注释。",
    "full": "YAGNI：只写被要求的那一件事；不加抽象、配置、依赖与兜底，能删就不加。",
    "ultra": "极简实现：先质疑需求本身，能不写就不写；只保留必要代码，不做未被要求的扩展。",
}

# 两套提示词都明确不省这几样：省 token 不能拿正确性换。
SAFETY_LINE = "输入校验、防数据丢失的错误处理、安全与无障碍代码不省。"


def prompt_instruction(cfg):
    """The block appended to the system prompt, or "" when both levels are off."""
    lines = []
    caveman = (cfg.get("prompt") or {}).get("caveman")
    ponytail = (cfg.get("prompt") or {}).get("ponytail")
    if caveman and caveman != "off" and caveman in CAVEMAN_TEXT:
        lines.append(CAVEMAN_TEXT[caveman])
    if ponytail and ponytail != "off" and ponytail in PONYTAIL_TEXT:
        lines.append(PONYTAIL_TEXT[ponytail])
    if not lines:
        return ""
    return ("\n\n%s\n%s\n%s" % (MARKER, "\n".join("- " + l for l in lines),
                                SAFETY_LINE))


def _content_text(content):
    """The text of a message content field, or None when it is not plain text.

    Only shapes we can rewrite without changing their meaning are accepted: a
    plain string, or a list made only of text blocks. An image block, a file
    reference or anything newer falls through untouched.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str) \
                    and str(block.get("type") or "text") in ("text", "input_text",
                                                             "output_text"):
                parts.append(block["text"])
            else:
                return None
        return "\n".join(parts)
    return None


def apply_prompt(messages, cfg):
    """Append the writing instruction to the first system message.

    Returns ``(messages, injected)``. Idempotent through MARKER: a caller that
    rebuilds the same body (a retry, a probe) must not stack copies.
    """
    instruction = prompt_instruction(cfg)
    if not instruction:
        return messages, 0
    for index, message in enumerate(messages or []):
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        text = _content_text(message.get("content"))
        if text is None or MARKER in text:
            return messages, 0
        out = list(messages)
        item = dict(message)
        item["content"] = text.rstrip() + instruction
        out[index] = item
        return out, 1
    return messages, 0


# ---------------------------------------------------------------------------
# 工具输出压缩（RTK）
# ---------------------------------------------------------------------------

MIN_BYTES = 500
MAX_BYTES = 10 * 1024 * 1024

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_GIT_LOG_RE = re.compile(r"(?m)^commit [0-9a-f]{7,40}\b")
_GIT_DIFF_RE = re.compile(r"(?m)^(diff --git |@@ -\d+)")
_GIT_STATUS_RE = re.compile(
    r"(?mi)^(on branch |changes to be committed:|changes not staged for commit:"
    r"|untracked files:|nothing to commit|your branch is |head detached )")
_BUILD_RE = re.compile(
    r"(?mi)^(compiling |building |collecting |downloading |installing "
    r"|unpacking |added \d+ packages?|npm (warn|error) |warning: "
    r"|error(\[|: )|make(\[\d+\])?: |\[[\d/]+\] (building|compiling)"
    r"|> task |successfully (built|installed) )")
_GREP_LINE_RE = re.compile(r"^((?:[A-Za-z]:)?[^\s:][^:\n]*):(\d+)(?::(\d+))?:(.*)$")
_TREE_RE = re.compile(r"(?m)^[\s│├└─|`+\-]*[├└]── ")
_LS_LONG_RE = re.compile(r"^[-dlbcps][rwxsStT-]{9}[.+@]?\s+\d+\s+")
_NUMBERED_RE = re.compile(r"(?m)^\s*\d+\s*[|:\u2502]\s?")
_PATH_RE = re.compile(r"^(?:\./|[A-Za-z]:[\\/]|/)?[\w.@+~\-]+"
                      r"(?:[\\/][\w.@+~\-]+)*[\\/]?$")
_ERROR_HEAD_RE = re.compile(
    r"(?is)^\s*(error\b|fatal\b|panic\b|exception\b|segmentation fault"
    r"|traceback \(most recent call last\))")
_EXIT_MARKERS = ("exit code", "command failed", "returned non-zero",
                 "non-zero exit status")


def looks_like_error(text):
    """A failing tool result is a debugging scene, not compression fodder."""
    head = (text or "")[:300]
    if _ERROR_HEAD_RE.search(head):
        return True
    low = head.lower()
    return any(marker in low for marker in _EXIT_MARKERS)


def _byte_len(text):
    return len(text.encode("utf-8", "surrogatepass"))


def classify(text):
    """Pick the RTK filter for a tool result; first match wins.

    The order is the one the 9Router / rtk pipeline uses (git-log first,
    smart-truncate last) so a git log is never mistaken for a build log.
    """
    head = text[:1024]
    low = head.lower()
    if _GIT_LOG_RE.search(head) and ("author:" in low or "date:" in low):
        return "git-log"
    if _GIT_DIFF_RE.search(head):
        return "git-diff"
    if _GIT_STATUS_RE.search(head):
        return "git-status"
    if _BUILD_RE.search(head):
        return "build-output"
    lines = [line for line in head.splitlines() if line.strip()]
    if len(lines) >= 4:
        grep_hits = sum(1 for line in lines if _GREP_LINE_RE.match(line))
        if grep_hits >= max(4, int(len(lines) * 0.8)):
            return "grep"
    if len(lines) >= 8:
        path_hits = sum(1 for line in lines if _PATH_RE.match(line.strip()))
        if path_hits >= max(8, int(len(lines) * 0.9)):
            return "find"
    if _TREE_RE.search(head):
        return "tree"
    if len(lines) >= 6 and sum(1 for line in lines if _LS_LONG_RE.match(line)) >= 6:
        return "ls"
    if len(lines) >= 4 and sum(1 for line in lines if _NUMBERED_RE.match(line)) >= 4:
        return "read-numbered"
    body = text.splitlines()
    repeats = 0
    for prev, cur in zip(body, body[1:]):
        if cur == prev and cur.strip():
            repeats += 1
    if repeats >= 5:
        return "dedup-log"
    if len(body) >= 200:
        return "smart-truncate"
    return ""


def _filter_git_log(text):
    blocks = [b for b in re.split(r"(?m)^(?=commit [0-9a-f]{7,40}\b)", text)
              if re.match(r"^commit [0-9a-f]{7,40}\b", b)]
    if len(blocks) < 3:
        return text
    out = []
    for block in blocks:
        match = re.match(r"commit ([0-9a-f]+)", block)
        if not match:
            continue
        subject = ""
        for line in block.splitlines()[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            # Author / Date / Merge headers are what makes a log long; the
            # first remaining line is the subject (git indents it by four
            # spaces), which is what an agent reads when it scans history.
            if re.match(r"(?i)^(author|date|merge|committer):", stripped):
                continue
            subject = stripped
            break
        out.append("%s %s" % (match.group(1)[:8], subject))
    return "\n".join(out)


def _filter_git_diff(text):
    out = []
    dropped = 0
    for line in text.splitlines():
        if line.startswith(("index ", "--- ", "+++ ")):
            continue
        # A single leading space is a context line (two or more is an added
        # line whose content starts with a space).
        if line.startswith(" ") and not line.startswith("  "):
            dropped += 1
            continue
        out.append(line)
    if dropped < max(8, len(out) // 2):
        return text
    return "\n".join(out)


def _filter_git_status(text):
    marks = (("changes to be committed", "M"),
             ("changes not staged for commit", "M"),
             ("untracked files", "??"),
             ("unmerged paths", "U"))
    out = []
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        low = stripped.lower()
        if low.startswith("(") and low.endswith(")"):
            continue
        if low.startswith(("no changes added", "nothing to commit",
                           'use "git ', "use 'git ")):
            continue
        heading = None
        for key, mark in marks:
            if low.startswith(key):
                heading = (key, mark)
                break
        if heading:
            section = heading[1]
            out.append(stripped.rstrip(":"))
            continue
        if line[:1] in ("\t", " "):
            out.append("%s %s" % (section or "?", stripped))
            continue
        out.append(stripped)
    return "\n".join(out)


_PROGRESS_RE = re.compile(
    r"(?i)^\s*(downloading|downloaded|fetching|resolving|installing|unpacking"
    r"|collecting|building|compiling|progress|\[=+|[-\\|/]\s*\d+\s*%|\d+%\s*\|)")


def _filter_build_output(text):
    text = _ANSI_RE.sub("", text)
    kept = []
    seen = set()
    dropped = 0
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if _PROGRESS_RE.search(line):
            dropped += 1
            continue
        key = line.strip()
        if len(key) > 20 and key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(line)
    if dropped < 8:
        return text
    return "\n".join(kept)


def _filter_grep(text):
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    groups = {}
    order = []
    for line in lines:
        match = _GREP_LINE_RE.match(line)
        if not match:
            return text
        path = match.group(1)
        if path not in groups:
            groups[path] = []
            order.append(path)
        groups[path].append((match.group(2), match.group(4)))
    out = []
    collapsed = 0
    for path in order:
        rows = groups[path]
        out.append("%s: %d match%s" % (path, len(rows),
                                       "" if len(rows) == 1 else "es"))
        for line_no, content in rows[:3]:
            out.append("  %s:%s" % (line_no, content.strip()))
        if len(rows) > 3:
            collapsed += len(rows) - 3
            out.append("  ... +%d more" % (len(rows) - 3))
    if collapsed < 5:
        return text
    return "\n".join(out)


def _filter_find(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 10:
        return text
    if sum(1 for line in lines if _PATH_RE.match(line)) < int(len(lines) * 0.9):
        return text
    groups = {}
    order = []
    for line in lines:
        norm = line[2:] if line.startswith("./") else line
        parent, sep, name = norm.rpartition("/")
        if not sep:
            parent, name = ".", norm
        if parent not in groups:
            groups[parent] = []
            order.append(parent)
        groups[parent].append(name)
    out = []
    for parent in order:
        names = groups[parent]
        out.append("%s/ (%d)" % (parent.rstrip("/") or ".", len(names)))
        cap = min(len(names), 8)
        out.extend("  " + name for name in names[:cap])
        if len(names) > cap:
            out.append("  ... +%d more" % (len(names) - cap))
    return "\n".join(out)


def _filter_tree(text):
    """Keep every directory, cap the files listed under each one.

    A tree's shape (which directories exist) is what an agent navigates by;
    the 40th file under ``node_modules/`` is what makes it 6 KB. Paths are
    rebuilt from the glyph indent so the surviving lines keep their meaning
    even though the drawing characters are gone.
    """
    lines = text.splitlines()
    if len(lines) < 60:
        return text
    entries = []
    for line in lines:
        raw = line.rstrip()
        if not raw.strip():
            continue
        index = raw.find("── ")
        if index < 0:
            entries.append((-1, raw.strip(), raw.strip().endswith("/")))
        else:
            name = raw[index + 3:].strip()
            entries.append((index // 4, name, name.endswith("/")))
    if sum(1 for entry in entries if entry[2]) < 3:
        return text
    at_level = {}
    counts = {}
    out = []
    for level, name, is_dir in entries:
        if level < 0:
            # The tree root is printed without glyphs.
            at_level = {0: name}
            out.append(name)
            continue
        depth = level + 1
        parent = at_level.get(depth - 1, "")
        if is_dir:
            at_level[depth] = ("%s/%s" % (parent, name) if parent else name)
            # A directory printed at this level replaces whatever a previous
            # sibling had put here; deeper levels must not survive it.
            for deeper in [k for k in at_level if k > depth]:
                del at_level[deeper]
            out.append("  " * depth + name)
            continue
        counts[parent] = counts.get(parent, 0) + 1
        if counts[parent] <= 6:
            out.append("  " * depth + name)
    if len(lines) - len(out) < 10:
        return text
    return "\n".join(out)


def _filter_ls(text):
    lines = [line.rstrip() for line in text.splitlines()
             if line.strip() and not line.strip().startswith("total ")]
    if len(lines) < 8:
        return text
    out = []
    for line in lines:
        parts = line.split(None, 8)
        if len(parts) < 9 or not _LS_LONG_RE.match(line):
            return text
        size = parts[4] if parts[4].isdigit() else "-"
        out.append("%s (%s)" % (parts[8], size))
    return "\n".join(out)


def _filter_read_numbered(text):
    lines = [line.rstrip() for line in text.splitlines()]
    out = []
    blank = 0
    for line in lines:
        if len(line) > 400:
            line = line[:400] + "..."
        if not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(line)
    if len(out) > 240:
        keep_head = 120
        keep_tail = 60
        omitted = len(out) - keep_head - keep_tail
        return "\n".join(out[:keep_head]
                         + ["... [%d lines omitted] ..." % omitted]
                         + out[-keep_tail:])
    return "\n".join(out)


def _filter_dedup(text):
    lines = text.splitlines()
    out = []
    prev = None
    count = 0

    def flush():
        if prev is None:
            return
        out.append(prev if count <= 1 else "%s  [x%d]" % (prev, count))

    for line in lines:
        if prev is not None and line == prev and line.strip():
            count += 1
            continue
        flush()
        prev = line
        count = 1
    flush()
    if len(lines) - len(out) < 5:
        return text
    return "\n".join(out)


def _filter_smart_truncate(text):
    lines = text.splitlines()
    if len(lines) < 200:
        return text
    head = [line.rstrip() for line in lines[:120]]
    tail = [line.rstrip() for line in lines[-60:]]
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join(head + ["... [%d lines omitted] ..." % omitted] + tail)


_FILTERS = {
    "git-log": _filter_git_log,
    "git-diff": _filter_git_diff,
    "git-status": _filter_git_status,
    "build-output": _filter_build_output,
    "grep": _filter_grep,
    "find": _filter_find,
    "tree": _filter_tree,
    "ls": _filter_ls,
    "read-numbered": _filter_read_numbered,
    "dedup-log": _filter_dedup,
    "smart-truncate": _filter_smart_truncate,
}


def compress_text(text):
    """Compress one tool result. Returns ``(new_text or None, kind)``.

    ``None`` means "send the original": below the size floor, unknown shape,
    a filter that raised, an empty result, or a result that is not strictly
    smaller. Those five fallbacks are the whole safety story.
    """
    if not isinstance(text, str):
        return None, ""
    size = _byte_len(text)
    if size < MIN_BYTES or size > MAX_BYTES:
        return None, ""
    kind = classify(text)
    if not kind:
        return None, ""
    try:
        new = _FILTERS[kind](text)
    except Exception:
        return None, kind
    if not isinstance(new, str) or not new.strip():
        return None, kind
    if _byte_len(new) >= size:
        return None, kind
    return new, kind


def compress_messages(messages):
    """Compress every tool result in a chat message list.

    Returns ``(messages, stats)``; the input list and its dicts are never
    mutated. Only ``role=tool`` / ``role=function`` messages are candidates:
    user text and assistant text are the conversation, not tool noise.
    """
    out = []
    changed = False
    before = after = blocks = 0
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") not in ("tool", "function"):
            out.append(message)
            continue
        if message.get("is_error") is True or message.get("error"):
            out.append(message)
            continue
        content = message.get("content")
        text = _content_text(content)
        if not text or looks_like_error(text):
            out.append(message)
            continue
        new, _kind = compress_text(text)
        if new is None:
            out.append(message)
            continue
        item = dict(message)
        if isinstance(content, str):
            item["content"] = new
        else:
            block_type = "text"
            if isinstance(content, list) and content and isinstance(content[0], dict):
                block_type = str(content[0].get("type") or "text")
            item["content"] = [{"type": block_type, "text": new}]
        out.append(item)
        changed = True
        before += _byte_len(text)
        after += _byte_len(new)
        blocks += 1
    if not changed:
        return messages, {"blocks": 0, "before": 0, "after": 0}
    return out, {"blocks": blocks, "before": before, "after": after,
                 "saved": before - after}


def apply(messages, accounts_dir=None):
    """One compression pass over an outbound message list.

    Called from ``build_upstream_body()`` and the Qoder / Trae / egg adapters,
    so every chat request goes through exactly one gate. Records the byte
    counters the panel's 节省统计 reads.
    """
    if disabled_for_request():
        return messages
    cfg = config(accounts_dir)
    if not cfg.get("enabled"):
        return messages
    messages, injections = apply_prompt(messages, cfg)
    blocks = before = after = 0
    if cfg.get("rtk"):
        messages, info = compress_messages(messages)
        blocks = info["blocks"]
        before = info["before"]
        after = info["after"]
    _note(accounts_dir, calls=1, before=before, after=after, blocks=blocks,
          injections=injections)
    return messages


# ---------------------------------------------------------------------------
# 本地应答
# ---------------------------------------------------------------------------

QUOTA_ANSWER = "额度正常，可以继续。"
TITLE_ANSWER = "新会话"
SUGGESTION_ANSWER = "无建议"

_QUOTA_RE = re.compile(
    r"(?i)^[/\s]*(quota|usage|balance|credits?)([\s_-]*(check|status|query))?[?？]?\s*$"
    r"|^[/\s]*(额度|剩余额度|额度检查|额度查询|查额度|查余额|余额|余额查询)[?？]?\s*$")
_CMD_PREFIX_RE = re.compile(r"(?i)command\s+prefix|命令前缀")
_FILE_PATH_RE = re.compile(r"(?i)file\s*paths?|文件路径")
_EXTRACT_RE = re.compile(r"(?i)extract|list|parse|提取|列出|解析")
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*\n(.*?)```", re.S)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_ABS_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|\.{0,2}/)?(?:[\w.@+~-]+[\\/])+[\w.@+-]+\.[A-Za-z0-9]{1,8}")


def _metadata_fields(payload):
    """Flatten Codex's client_metadata hints (plain keys or JSON strings)."""
    meta = payload.get("client_metadata")
    if not isinstance(meta, dict):
        return {}
    fields = {}
    for key, value in meta.items():
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                inner = json.loads(value)
            except Exception:
                inner = None
            if isinstance(inner, dict):
                for name in ("request_kind", "turn_trigger", "thread_source",
                             "kind", "trigger", "source"):
                    if name in inner:
                        fields[name] = inner[name]
        if key in ("request_kind", "turn_trigger", "thread_source"):
            fields[key] = value
    return fields


def _command_prefix(text):
    """The permission-system prefix of the command the prompt carries."""
    match = _FENCE_RE.search(text)
    command = match.group(1) if match else ""
    if not command:
        match = _BACKTICK_RE.search(text)
        command = match.group(1) if match else ""
    if not command:
        return None
    command = command.strip().splitlines()[0].strip()
    segment = re.split(r"\s*(?:\|\||&&|\||;|>|<)\s*", command)[0].strip()
    tokens = segment.split()
    while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
        tokens.pop(0)
    while tokens and tokens[0] in ("sudo", "env", "time", "nohup"):
        tokens.pop(0)
    if not tokens:
        return None
    keep = 1
    if len(tokens) > 1 and not tokens[1].startswith("-"):
        keep = 2
    return " ".join(tokens[:keep])


def _file_paths(text):
    found = []
    for match in _ABS_PATH_RE.findall(text or ""):
        path = match.strip().strip("\"'")
        if path and path not in found:
            found.append(path)
    return found


def _content_kind(text):
    """Which of the five local answers this request is, if any."""
    if not text:
        return ""
    stripped = text.strip()
    if _QUOTA_RE.match(stripped):
        return "quota"
    if _CMD_PREFIX_RE.search(stripped) and _command_prefix(stripped):
        return "command_prefix"
    if _FILE_PATH_RE.search(stripped) and _EXTRACT_RE.search(stripped) \
            and _file_paths(stripped):
        return "file_path"
    return ""


def _answer_for(kind, text):
    if kind == "quota":
        return QUOTA_ANSWER
    if kind == "title":
        return TITLE_ANSWER
    if kind == "suggestion":
        return SUGGESTION_ANSWER
    if kind == "command_prefix":
        return _command_prefix(text)
    if kind == "file_path":
        paths = _file_paths(text)
        if not paths:
            return None
        return json.dumps(paths[:20], ensure_ascii=False)
    return None


def local_answer(payload, accounts_dir=None):
    """Answer a probe locally. Returns ``{"kind", "text"}`` or None.

    Deliberately narrow: one user message, no tools, nothing already answered
    in the conversation. A probe is a request whose *whole* point is the answer
    we already have; anything that looks like real work goes upstream.
    """
    if disabled_for_request():
        return None
    cfg = config(accounts_dir)
    if not cfg.get("enabled") or not (cfg.get("local") or {}).get("enabled"):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("tools"):
        return None
    messages = [m for m in (payload.get("messages") or []) if isinstance(m, dict)]
    if not messages:
        return None
    if any(m.get("role") in ("assistant", "tool", "function") for m in messages):
        return None
    users = [m for m in messages if m.get("role") == "user"]
    if len(users) != 1:
        return None
    text = _content_text(users[0].get("content")) or ""
    fields = _metadata_fields(payload)
    blob = " ".join(str(v) for v in fields.values()).lower()
    kind = ""
    if "title" in blob:
        kind = "title"
    elif "suggestion" in blob or "ambient" in blob:
        kind = "suggestion"
    if not kind:
        kind = _content_kind(text)
    if not kind or not (cfg.get("local") or {}).get(kind):
        return None
    answer = _answer_for(kind, text)
    if answer is None:
        return None
    _note(accounts_dir, local_kind=kind)
    return {"kind": kind, "text": answer}
