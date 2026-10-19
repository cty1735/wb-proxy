# -*- coding: utf-8 -*-
"""工具输出压缩（RTK）的过滤规则。

在请求发出**之前**就地重写历史里的 ``tool_result`` / ``role:"tool"`` 内容，把
对模型判断没有信息量、却占掉大半字节的部分去掉。目标不是"压缩文件"，而是
"让同一份工具输出用更少的 token 说清楚"。

硬约束（任何一条不满足就放弃这一块、原样放行）：

* **报错的工具结果永不触碰**——调用方之所以回灌错误，就是要模型读到它；
* 小于尺寸下限的内容不碰——压不出东西，只增加风险；
* **替换结果永不大于原文**——:func:`compress_tool_output` 只在更短时返回内容；
* 过滤器抛错一律原样放行（fail-open）；
* 整条管线**幂等**——对已压缩过的内容再跑一次，结果不变。

本模块只做纯文本，不 import 项目内其它模块，方便单测。
"""

import re

#: 小于这个字节数的工具输出不碰。
MIN_BYTES = 512
#: 通用行数上限：超过就掐头去尾、中间省略。
MAX_LINES = 240
#: 单行长度上限：超长的一行（编号转储、压缩后的 bundle 等）截断。
MAX_LINE = 4000
#: git diff 每个 hunk 里连续保留的上下文行数。
DIFF_CONTEXT = 2

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_DIFF_HEAD_RE = re.compile(r"^(diff --git |index |--- |\+\+\+ |@@ )")
_HUNK_RE = re.compile(r"^@@")
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
_FATAL_RE = re.compile(r"(?m)^\s*(fatal|panic|segmentation fault)\b", re.IGNORECASE)
#: 构建 / 包管理器 / 下载器的进度噪音行。
_NOISE_RE = re.compile(
    r"(?im)^\s*[\[\(]?\s*\d{1,3}(\.\d+)?\s*%"
    r"|^\s*\d+\s*/\s*\d+\s*(\[|\()"
    r"|Downloading|Downloaded|Installing|Resolving|Fetching|Building wheel|"
    r"creating build|adding |copying |gcc -c |clang -c "
)


def looks_like_error(text):
    """这个工具结果是不是一次失败输出。

    只有**明确**的错误信号才判失败（traceback / fatal / panic）。构建日志里
    零散的 "error:" 行不算——那类日志本来就该被压缩；宁可漏判，不可把模型
    真正要读的报错压掉。
    """
    if _TRACEBACK_RE.search(text):
        return True
    if _FATAL_RE.search(text):
        return True
    return False


def _strip_ansi(text):
    return _ANSI_RE.sub("", text)


def _drop_carriage_returns(text):
    """把 ``进度条\\r真实输出`` 这种回车覆盖压成最终可见的那一行。"""
    if "\r" not in text:
        return text
    out = []
    for line in text.split("\n"):
        if "\r" in line:
            pieces = [p for p in line.split("\r") if p != ""]
            if pieces:
                out.append(pieces[-1])
        else:
            out.append(line)
    return "\n".join(out)


def _looks_like_diff(text):
    return "@@ " in text and (
        "diff --git " in text or "--- a/" in text or "+++ b/" in text
    )


def _trim_git_diff(text):
    """git diff：保留所有 +/- 行与文件头，只裁掉连续的上下文行。"""
    out = []
    dropped = 0
    run = 0

    def flush():
        nonlocal dropped
        if dropped:
            out.append(" ... %d context line(s) elided ..." % dropped)
            dropped = 0

    for line in text.split("\n"):
        if (_HUNK_RE.match(line) or line[:1] in ("+", "-", "\\")
                or _DIFF_HEAD_RE.match(line)):
            flush()
            run = 0
            out.append(line)
            continue
        if line[:1] == " ":
            run += 1
            if run > DIFF_CONTEXT:
                dropped += 1
                continue
            flush()
            out.append(line)
            continue
        # 空行或其它行：按普通内容保留。
        flush()
        run = 0
        out.append(line)
    flush()
    return "\n".join(out)


def _collapse_noise(text):
    """把连续的进度/噪音行压成一行标记（保留"发生过"的信息，而不是删光）。"""
    out = []
    run = 0
    changed = False

    def flush():
        nonlocal run, changed
        if run:
            out.append(" ... (%d progress/noise line(s)) ..." % run)
            changed = True
            run = 0

    for line in text.split("\n"):
        if line.strip() and _NOISE_RE.match(line):
            run += 1
            continue
        flush()
        out.append(line)
    flush()
    return "\n".join(out) if changed else text


def _collapse_repeats(text, min_run=4, keep_head=1, keep_tail=1):
    """连续的相同行压成 ``<head> … (×N) … <tail>``。"""
    lines = text.split("\n")
    out = []
    i = 0
    changed = False
    n = len(lines)
    while i < n:
        j = i + 1
        while j < n and lines[j] == lines[i]:
            j += 1
        run = j - i
        if run >= min_run and lines[i].strip():
            changed = True
            head = lines[i:i + keep_head]
            tail = lines[j - keep_tail:j] if keep_tail else []
            out.extend(head)
            out.append(" ... (%d identical lines) ..." % run)
            out.extend(tail)
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out) if changed else text


def _cap_long_lines(text):
    lines = text.split("\n")
    changed = False
    out = []
    for line in lines:
        if len(line) > MAX_LINE:
            changed = True
            out.append(line[:MAX_LINE] + " ...(%d more chars)..." % (len(line) - MAX_LINE))
        else:
            out.append(line)
    return "\n".join(out) if changed else text


def _elide_middle(text, max_lines=MAX_LINES):
    """行数超限时掐头去尾，中间用一行省略号代表。"""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    head = max(1, int(max_lines * 0.6))
    tail = max(1, max_lines - head - 1)
    elided = len(lines) - head - tail
    elided_bytes = sum(len(x) + 1 for x in lines[head:len(lines) - tail])
    return "\n".join(
        lines[:head]
        + [" ...(%d line(s), %d byte(s) elided)..." % (elided, elided_bytes)]
        + (lines[-tail:] if tail else [])
    )


def compress_tool_output(text):
    """压缩一段工具输出；无法安全压缩时返回 ``None``。

    返回的内容**一定短于**输入，且语义上仍是同一份工具输出。
    """
    if not isinstance(text, str) or len(text) < MIN_BYTES:
        return None
    if looks_like_error(text):
        return None
    try:
        out = text
        out = _strip_ansi(out)
        out = _drop_carriage_returns(out)
        if _looks_like_diff(out):
            out = _trim_git_diff(out)
        else:
            out = _collapse_noise(out)
        out = _cap_long_lines(out)
        out = _collapse_repeats(out)
        out = _elide_middle(out)
    except Exception:
        return None
    if not out or len(out) >= len(text):
        return None
    return out
