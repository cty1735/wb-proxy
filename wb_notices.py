"""新功能通知：把 docs/notices.md 解析成面板右上角通知中心的数据。

通知内容和网关代码分开维护：往 markdown 里追加一节，面板下一次拉 /notices
就能看到，不需要改 Python 或前端。每条通知按 id 记住「不再显示」，状态存在
settings.json（见 wb_settings.dismissed_notices）。

文件格式（一节 = 一条通知）：

    ## 2026-10-23 通知中心上线
    id: notices-center          # 可选；缺省由标题 + 日期哈希生成
    level: feature              # feature / improve / fix / notice
    title_en: In-app notices    # 可选，英文界面用
    title_hant: 通知中心上線    # 可选，正體中文界面用
    body_en: ...                # 可选，正文同理
    body_hant: ...

    正文第一段……

    - 列表项
    - 第二项

`## ` 之前的内容（文件标题、说明、HTML 注释）会被忽略。正文按空行分段，
连续的 `- ` / `* ` 行渲染成列表；行内支持 **粗体**、`代码`、[链接](https://...)。
显式写 id 最稳：改标题不会让「不再显示」失效。
"""

import hashlib
import os
import re
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(ROOT, "docs", "notices.md")

# 手滑写一个超大文件不该把面板拖死；正常通知文件只有几 KB。
MAX_FILE_BYTES = 1024 * 1024
MAX_BODY_CHARS = 20000
MAX_NOTICES = 200

_LEVEL_ALIASES = {
    "feature": "feature",
    "new": "feature",
    "功能": "feature",
    "新功能": "feature",
    "新增": "feature",
    "improve": "improve",
    "improvement": "improve",
    "改进": "improve",
    "优化": "improve",
    "fix": "fix",
    "bugfix": "fix",
    "bug": "fix",
    "修复": "fix",
    "notice": "notice",
    "info": "notice",
    "公告": "notice",
    "提醒": "notice",
}

# 只有这些键算元数据；正文里出现「注意: ...」不会被误当成字段。
_META_KEYS = {
    "id", "level", "date", "title",
    "title_en", "title_hant", "body_en", "body_hant",
}

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*[·:：\-—]?\s*(.*)$")

_lock = threading.RLock()
# 解析结果按 (path, mtime_ns, size) 缓存：面板每次打开通知中心都会拉一次，
# 而文件只在有人追加通知时才变。
_cache = {"key": None, "value": []}


def notices_path():
    """通知文件的绝对路径。WB_NOTICES_FILE 可覆盖（相对路径按项目根算）。"""
    override = (os.environ.get("WB_NOTICES_FILE") or "").strip()
    if not override:
        return DEFAULT_FILE
    if os.path.isabs(override):
        return override
    return os.path.join(ROOT, override)


def _clean_id(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-._")
    return text[:64]


def _auto_id(date, title):
    seed = "%s|%s" % (date, title)
    return "n-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def _split_meta(lines):
    """Return (meta, body_lines) for one section.

    Metadata is the first contiguous block of `key: value` lines right after the
    heading; a blank line or any unknown key ends it. Both `key: value` and
    `- key: value` are accepted so the file reads naturally as markdown.
    `body_en: |` (or `>`) starts an indented block scalar, which is what makes
    a bilingual notice readable in the source file.
    """
    meta = {}
    body = []
    index = 0
    in_meta = True
    while index < len(lines):
        line = lines[index]
        if in_meta:
            stripped = line.strip()
            if not stripped:
                in_meta = False
                index += 1
                continue
            candidate = stripped
            if candidate.startswith(("- ", "* ")):
                candidate = candidate[2:].strip()
            key, value = "", ""
            if ":" in candidate:
                key, _, value = candidate.partition(":")
                key = key.strip().lower()
                value = value.strip()
            if key in _META_KEYS:
                if value in ("|", ">"):
                    block = []
                    index += 1
                    while index < len(lines):
                        nxt = lines[index]
                        if nxt.strip() and not nxt.startswith((" ", "\t")):
                            break
                        block.append(nxt)
                        index += 1
                    while block and not block[0].strip():
                        block.pop(0)
                    while block and not block[-1].strip():
                        block.pop()
                    indents = [len(item) - len(item.lstrip())
                               for item in block if item.strip()]
                    cut = min(indents) if indents else 0
                    block = [item[cut:] if len(item) >= cut else ""
                             for item in block]
                    meta[key] = (" " if value == ">" else "\n").join(block)
                    # index 已指向块后的第一行，这一行还要按正文/下一个字段处理。
                    continue
                meta[key] = value
                index += 1
                continue
            in_meta = False
        body.append(line)
        index += 1
    return meta, body


def _clean_level(value):
    text = str(value or "").strip().lower()
    return _LEVEL_ALIASES.get(text, "notice")


def _clean_date(value):
    text = str(value or "").strip()
    return text if re.match(r"^\d{4}-\d{2}-\d{2}$", text) else ""


def parse_notices(text):
    """Parse the markdown file body into a list of notice dicts.

    Pure function: no file access, no cache. Used by the loader and the tests.
    """
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    sections = []
    current = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        match = _HEADING_RE.match(line)
        if match:
            current = {"heading": match.group(1).strip(), "lines": []}
            sections.append(current)
            continue
        if current is not None:
            current["lines"].append(line)

    notices = []
    seen = set()
    for order, section in enumerate(sections):
        meta, body_lines = _split_meta(section["lines"])
        heading = section["heading"]
        date = _clean_date(meta.get("date"))
        heading_title = heading
        match = _DATE_RE.match(heading)
        if match:
            date = date or match.group(1)
            heading_title = match.group(2).strip() or match.group(1)
        title = str(meta.get("title") or heading_title).strip() or "未命名通知"
        notice_id = _clean_id(meta.get("id")) or _auto_id(date, title)
        # 手写文件里重复 id 不该让两条通知互相顶掉「不再显示」的状态。
        base, suffix = notice_id, 2
        while notice_id in seen:
            notice_id = "%s-%d" % (base, suffix)
            suffix += 1
        seen.add(notice_id)
        body = "\n".join(body_lines).strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS].rstrip() + "\n…"
        notices.append({
            "id": notice_id,
            "date": date,
            "level": _clean_level(meta.get("level")),
            "title": title[:200],
            "body": body,
            "title_en": str(meta.get("title_en") or "").strip()[:200],
            "title_hant": str(meta.get("title_hant") or "").strip()[:200],
            "body_en": str(meta.get("body_en") or "").strip()[:MAX_BODY_CHARS],
            "body_hant": str(meta.get("body_hant") or "").strip()[:MAX_BODY_CHARS],
            "_order": order,
        })
    # 新通知排前面：先按日期倒序；同一天按文件里的书写顺序倒序（文件约定是
    # 往末尾追加），所以刚追加的那一节排最上面。
    notices.sort(key=lambda item: (item.get("date") or "", item["_order"]),
                 reverse=True)
    for item in notices:
        item.pop("_order", None)
    return notices[:MAX_NOTICES]


def load_notices(path=None):
    """Return the parsed notices, re-reading only when the file changed.

    Missing/unreadable file reads as an empty list: a fresh checkout without the
    markdown file should still render the panel, just with no notices.
    """
    path = path or notices_path()
    try:
        info = os.stat(path)
        key = (path, info.st_mtime_ns, info.st_size)
    except OSError:
        return []
    with _lock:
        if _cache["key"] == key:
            return [dict(item) for item in _cache["value"]]
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read(MAX_FILE_BYTES)
    except OSError:
        return []
    value = parse_notices(text)
    with _lock:
        _cache.update({"key": key, "value": value})
    return [dict(item) for item in value]


def source_label(path=None):
    """Path shown in the panel, relative to the project root when possible."""
    path = path or notices_path()
    try:
        return os.path.relpath(path, ROOT).replace(os.sep, "/")
    except ValueError:
        return path
