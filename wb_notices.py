"""更新通知：把 docs/notices.md 解析成面板右上角通知中心的数据。

正文默认从 GitHub 上读取（服务端缓存 20 秒），直连失败时依次尝试
gh-proxy 镜像、Gitee 镜像，最后回退到本仓库里的副本。所以维护者只改
GitHub 上的文件，所有网关实例下一次回源就能看到，不需要重新发版。

    WB_NOTICES_URL       换一个通知地址；留空用内置的 GitHub 地址
    WB_NOTICES_REMOTE=0  强制只读本地文件（离线 / 自测）
    WB_NOTICES_TTL       远程缓存秒数，默认 20
    WB_NOTICES_TIMEOUT   单次抓取超时秒数，默认 3

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
显式写 id 最稳：改标题不会让「已读」记录失效。
"""

import hashlib
import os
import re
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(ROOT, "docs", "notices.md")

# GitHub 上的正文是唯一需要维护的副本；后面两个只是网络不通时的镜像。
GITHUB_URL = "https://raw.githubusercontent.com/cty1735/wb-proxy/main/docs/notices.md"
GH_PROXY_URL = "https://gh-proxy.com/" + GITHUB_URL
GITEE_URL = "https://gitee.com/cty1735/wb-proxy/raw/main/docs/notices.md"
DEFAULT_URLS = (GITHUB_URL, GH_PROXY_URL, GITEE_URL)

# 手滑写一个超大文件不该把面板拖死；正常通知文件只有几 KB。
MAX_FILE_BYTES = 1024 * 1024
MAX_BODY_CHARS = 20000
MAX_NOTICES = 200

REMOTE_TTL_SECONDS = 20.0
REMOTE_TIMEOUT_SECONDS = 3.0

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
# 解析结果按 (来源, 版本, 大小/内容哈希) 缓存：面板每次打开通知中心都会拉
# 一次，而内容只在有人追加/修改通知时才变。
_cache = {"key": None, "value": []}

# 远程正文：text=None 表示「还没成功拿到过」。获取失败时保留上一份好数据，
# 网络恢复前继续给用户看，不要把通知中心整个清空。
_remote_lock = threading.Lock()
_remote_cache = {"text": None, "url": "", "fetched_at": 0.0, "updated_at": 0}


def _env_float(name, default):
    try:
        value = float((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def remote_enabled():
    value = (os.environ.get("WB_NOTICES_REMOTE") or "").strip().lower()
    return value not in ("0", "off", "false", "no", "local")


def remote_urls():
    override = (os.environ.get("WB_NOTICES_URL") or "").strip()
    return (override,) if override else DEFAULT_URLS


def _fresh_url(url):
    # 镜像 / CDN 会缓存几十秒；加个时间戳让每次回源都拿到最新提交。
    sep = "&" if "?" in url else "?"
    return "%s%st=%d" % (url, sep, int(time.time()))


def _fetch_url(url, timeout):
    request = urllib.request.Request(url, headers={
        "User-Agent": "wb-proxy-notices/1.0 (+https://github.com/cty1735/wb-proxy)",
        "Accept": "text/markdown, text/plain, */*",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("notices file is too large")
    return raw.decode("utf-8-sig", "replace")


def _remote_state(force=False):
    """Return the cached remote snapshot, fetching it when it is stale.

    Returns None when no remote copy has ever been fetched. A failed refresh
    keeps the previous good snapshot so a temporary network problem does not
    empty the notice center.
    """
    if not remote_enabled():
        return None
    ttl = _env_float("WB_NOTICES_TTL", REMOTE_TTL_SECONDS)
    now = time.time()
    with _remote_lock:
        has_text = _remote_cache["text"] is not None
        age = now - _remote_cache["fetched_at"]
        if has_text and not force and age < ttl:
            return dict(_remote_cache)
        # 从没成功过时也别每个请求都重试：失败后按最多 10 秒退避。
        if not has_text and not force and age < min(ttl, 10.0):
            return None
    timeout = _env_float("WB_NOTICES_TIMEOUT", REMOTE_TIMEOUT_SECONDS)
    for base in remote_urls():
        try:
            text = _fetch_url(_fresh_url(base), timeout)
        except Exception:
            continue
        with _remote_lock:
            _remote_cache.update({
                "text": text,
                "url": base,
                "fetched_at": time.time(),
                "updated_at": int(time.time()),
            })
            return dict(_remote_cache)
    with _remote_lock:
        _remote_cache["fetched_at"] = time.time()
        if _remote_cache["text"] is not None:
            return dict(_remote_cache)
    return None


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
        # 手写文件里重复 id 不该让两条通知互相顶掉「已读」的状态。
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


def source_label(path=None):
    """Path shown in the panel, relative to the project root when possible."""
    path = path or notices_path()
    try:
        return os.path.relpath(path, ROOT).replace(os.sep, "/")
    except ValueError:
        return path


def _remote_source_label(url):
    if url == GITHUB_URL:
        return "GitHub · cty1735/wb-proxy/docs/notices.md"
    if url == GH_PROXY_URL:
        return "GitHub 镜像 · cty1735/wb-proxy/docs/notices.md"
    if url == GITEE_URL:
        return "Gitee · cty1735/wb-proxy/docs/notices.md"
    return url


def _parse_cached(key, text):
    with _lock:
        if _cache["key"] == key:
            return [dict(item) for item in _cache["value"]]
    value = parse_notices(text)
    with _lock:
        _cache.update({"key": key, "value": value})
    return [dict(item) for item in value]


def _local_state(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read(MAX_FILE_BYTES)
    except OSError:
        return None
    key = ("local", path, info.st_mtime_ns, info.st_size)
    return {
        "notices": _parse_cached(key, text),
        "source": source_label(path),
        "updated_at": int(info.st_mtime),
        "remote": False,
    }


def load_notices_with_meta(path=None, force=False):
    """Return notices plus where they came from.

    Remote first, local copy last: the markdown in the repository is the source
    of truth, while a checkout without network still shows its bundled copy.
    ``force`` skips the short cache; the panel uses it when the bell is opened.
    """
    remote = _remote_state(force=force)
    if remote is not None:
        text = remote["text"]
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
        return {
            "notices": _parse_cached(("remote", remote["url"], digest), text),
            "source": _remote_source_label(remote["url"]),
            "updated_at": int(remote.get("updated_at") or 0),
            "remote": True,
        }
    local = _local_state(path or notices_path())
    if local is not None:
        return local
    return {
        "notices": [],
        "source": source_label(path or notices_path()),
        "updated_at": 0,
        "remote": False,
    }


def load_notices(path=None, force=False):
    """Return the parsed notices, remote when available and local as fallback."""
    return load_notices_with_meta(path, force)["notices"]


def _reset_caches():
    """Drop both caches (used by tests and manual reloads)."""
    with _lock:
        _cache.update({"key": None, "value": []})
    with _remote_lock:
        _remote_cache.update({"text": None, "url": "", "fetched_at": 0.0,
                              "updated_at": 0})
