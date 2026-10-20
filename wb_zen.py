"""鸡蛋（Egg）网关：把公开的免费模型额度聚合成 OpenAI 兼容上游。

参考 https://github.com/LAGcomcom/zen-gate 的两条免凭据免费源：

* **OpenCode Zen 免费车道** —— ``opencode.ai/zen`` 里带 ``free`` 标记的模型
  （以及 ``alwaysFree`` 名单）。这条车道没有 API Key 体系，上游按“是否为
  官方 OpenCode 客户端”做指纹校验：UA、``x-opencode-session*`` 会话头、
  以及请求里必须带着官方 ``read`` / ``shell`` 两个工具的精确定义。这里照
  该项目的实测结论实现（见 ``FINGERPRINT_TOOLS``）。
* **Kilo 免费池** —— ``api.kilo.ai/api/gateway`` 的 ``isFree`` 模型。完全
  无需凭据，普通 OpenAI chat 请求即可；提示词可能被上游记录，不能传机密。

模块只依赖标准库 + ``wb_accounts.urlopen``（沿用项目的代理 / 连接池行为），
不保存任何账号或密钥。目录按源分别缓存：某个源拉取失败时保留它上一次的
结果，绝不因为一次网络抖动把还能用的模型从列表里删掉。
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

ZEN_BASE = "https://opencode.ai"
KILO_BASE = "https://api.kilo.ai/api/gateway"

ZEN_UA = "opencode/latest/2.0.26/cli"
KILO_UA = "zen-gate/1.4"

#: 目录缓存多久算过期。免费名单轮换很快，鸡蛋视图切回来就强制刷一次。
CATALOG_TTL_SECONDS = 180
#: 单个源的列表请求超时。两个源并发拉取，整体上限由 _refresh_locked 的
#: 共享截止时间控制（约等于这个值），不会因为先 join 一个慢源再等另一个而叠加。
#: Kilo 的 /models 返回 500KB+，实测 3~18s（TLS 握手波动大）；给到 20s 以减少
#: 偶发超时，同时避免面板切换等待过久。zen-gate 用的 15s 在这台机器上仍会超时。
LIST_TIMEOUT_SECONDS = 20

#: 目录落盘文件名（放在 accounts 目录里，跟其它运行期状态一起）。
CATALOG_FILENAME = "zen-catalog.json"

#: 上游免费车道要求的两个工具名。定义必须逐字一致，改一个字都会 403。
FINGERPRINT_TOOLS = ("read", "shell")

_READ_DESC = (
    "Read the contents of a file or directory. Supports text files, images, "
    "and PDFs. Images and PDFs are presented directly to the model. Each text "
    "line is prefixed by its 1-based line number as <line>: <content>. The "
    "prefix is for reference and is not part of the file content. Directory "
    "entries are returned one per line. Use offset and limit to read large "
    "files or directories in sections. Prefer one larger read over many small "
    "slices, and use grep to find specific content in large files."
)
_SHELL_DESC = (
    "Execute a shell command and return its output. Commands run on Linux "
    "using bash. Quote file paths containing spaces or special characters. "
    "Prefer dedicated tools over shell commands when possible. When output is "
    "large, the full result is saved to a file and a truncated preview is "
    "returned. Rely on automatic truncation unless filtering the output is "
    "more useful. Commands accept an optional timeout, background commands "
    "have no timeout by default. Background commands return immediately, and "
    "you will be notified when they complete."
)

_READ_PARAMS = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File or directory to read"},
        "offset": {"type": "integer", "minimum": 0,
                   "description": "The line or directory entry to start reading from (1-based)"},
        "limit": {"type": "integer", "minimum": 0,
                  "description": "The maximum number of lines or directory entries to read (defaults to and capped at 2000)"},
    },
    "required": ["path"],
    "additionalProperties": False,
}
_SHELL_PARAMS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command string to execute"},
        "workdir": {"type": "string",
                    "description": "Working directory to execute the command in. Defaults to the current working directory. When possible, avoid changing directories in the command and set the working directory here instead."},
        "timeout": {"type": "integer", "minimum": 0,
                    "description": "Timeout in milliseconds. Set to 0 to disable the timeout. Defaults to 120000 for foreground commands. Background commands have no timeout by default."},
        "background": {"type": "boolean",
                       "description": "Run the command in the background and return immediately (useful for dev servers and long-running builds). You do not need to use '&' at the end of the command when using this parameter. You will be notified when it completes. DO NOT poll for completion."},
    },
    "required": ["command"],
    "additionalProperties": False,
}


class EggError(Exception):
    """一次免费上游请求失败，带 HTTP 状态与可直接展示的说明。"""

    def __init__(self, status, detail, retry_after=0):
        Exception.__init__(self, detail)
        self.status = int(status or 502)
        self.detail = str(detail or "")
        self.retry_after = int(retry_after or 0)


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------

_FREE_RE = re.compile(r"(?:^|[-_])free(?:$|[-_.])")

#: 永远免费、但 id 里不带 free 的模型。
_ALWAYS_FREE = {"union-alpha": True, "space-bunny-free": True}

#: 免费车道的本地能力表（实测值，来自 zen-gate 的 catalog.go）。
_ZEN_CAPS = [
    (re.compile(r"^mimo.*v2\.6"), True, True, 1048576, 131072),
    (re.compile(r"^mimo.*v2\.5"), True, True, 1048576, 131072),
    (re.compile(r"^mimo"), True, True, 262144, 131072),
    (re.compile(r"^muse.?spark"), True, True, 1048576, 943718),
    (re.compile(r"^step"), False, True, 1000000, 64000),
    (re.compile(r"^nemotron.*super.?vl"), True, True, 1048576, 262144),
    (re.compile(r"^nemotron"), False, True, 128000, 32768),
    (re.compile(r"^ling"), False, True, 262144, 32768),
    (re.compile(r"^space.?bunny"), True, True, 1048576, 65536),
    (re.compile(r"^union"), True, False, 262144, 131072),
    (re.compile(r"^deepseek.*v4\.1"), True, True, 1048576, 262144),
    (re.compile(r"^deepseek"), False, True, 1048576, 65536),
    (re.compile(r"^ll.?alpha"), True, True, 1048576, 131072),
    (re.compile(r"^glm"), False, True, 524288, 262144),
    (re.compile(r"^longcat"), True, True, 1048576, 262144),
    (re.compile(r"^fledge"), False, True, 131072, 32768),
    (re.compile(r"^jev"), False, False, 32768, 4096),
]

_ZEN_NAMES = {
    "mimo-v2.6-flash-free": "MiMo V2.6 Flash",
    "mimo-v2.5-free": "MiMo V2.5",
    "muse-spark-1.3-contributor-free": "Muse Spark 1.3",
    "muse-spark-1.2-contributor-free": "Muse Spark 1.2",
    "nemotron-3-ultra-free": "Nemotron 3 Ultra",
    "nemotron-3.5-lightning-free": "Nemotron 3.5 Lightning",
    "nemotron-3.5-super-vl-free": "Nemotron 3.5 Super VL",
    "ling-3.0-flash-fin-free": "Ling 3.0 Flash Fin",
    "ling-3.1-flash-free": "Ling 3.1 Flash",
    "space-bunny-free": "Space Bunny",
    "union-alpha": "Union Alpha",
    "deepseek-v4-flash-free": "DeepSeek V4 Flash",
    "deepseek-v4.1-flash-free": "DeepSeek V4.1 Flash",
    "ll-alpha-free": "LL Alpha",
    "glm-5.3-flash-free": "GLM 5.3 Flash",
    "beam-free": "Beam",
    "jev-1.13-free": "Jev 1.13",
    "longcat-2.5-preview-free": "LongCat 2.5 Preview",
    "fledge-alpha-free": "Fledge Alpha",
}

_ZEN_BLURBS = {
    "mimo-v2.6-flash-free": "小米开源王牌：开源权重智能指数第一、Code Arena 前十，整体逼近 GPT-5 级主力，1M 上下文。",
    "muse-spark-1.3-contributor-free": "Meta 编程特化模型，官方对标 GPT-5.6 Sol；CN 出口实测被地区门拦，有海外出口再用。",
    "nemotron-3-ultra-free": "NVIDIA 开源旗舰，约 GPT-4.5 水平；国内直连较稳，适合当稳定备选。",
    "nemotron-3.5-lightning-free": "Nemotron 3.5 轻快版，适合快问快答与短任务，约 GPT-4.5 级。",
    "nemotron-3.5-super-vl-free": "NVIDIA 多模态加强版：实测 1M 上下文、256K 输出、接受图片。",
    "ling-3.0-flash-fin-free": "蚂蚁 Ling 3.0 Flash：256K 上下文、开源权重，轻快可靠。",
    "space-bunny-free": "匿名 1M 上下文模型，OpenCode 用量榜第一，社区口碑好。",
    "deepseek-v4-flash-free": "DeepSeek V4 Flash：首字快、输出快，1M 上下文，综合对位 GPT-5 级。",
    "deepseek-v4.1-flash-free": "DeepSeek V4.1 Flash：实测 1M+ 上下文、256K 输出上限、接受图片，免费车道里最慷慨的一档。",
    "ll-alpha-free": "来路不明的免费模型，实测 1M 上下文、128K 输出、接受图片。",
    "glm-5.3-flash-free": "智谱 GLM 5.3 Flash 轻量版，实测 256K 输出上限；上游偶发 429。",
    "beam-free": "新上架模型，CN 出口被上游地区门拦下，只有海外出口才用得上。",
    "longcat-2.5-preview-free": "美团 LongCat 2.5 预览：万亿级动态稀疏 MoE、百万上下文。",
    "fledge-alpha-free": "新上架的神秘面孔，社区评价还少；先按 GPT-4.5 级试水。",
}

#: 网络不可用时的兜底清单（只用于离线展示；请求仍会打到上游）。
ZEN_FALLBACK_IDS = [
    "deepseek-v4.1-flash-free", "mimo-v2.6-flash-free", "space-bunny-free",
    "nemotron-3.5-super-vl-free", "nemotron-3.5-lightning-free",
    "ll-alpha-free", "glm-5.3-flash-free", "longcat-2.5-preview-free",
    "beam-free", "jev-1.13-free",
]

KILO_FALLBACK_IDS = [
    "kilo-auto/free", "minimax/minimax-m3.1-flash:free",
    "nvidia/nemotron-3.5-super-vl:free", "reflection/beam-free",
    "stealth/glyph-cluster", "xingchen-agi/xing4.0-29b-a4b:free",
]

#: 免费池的短名：面板的模型清单要标出每个模型属于哪一路免费源，两者的限流、
#: 地区门与提示词记录口径都不一样（Zen 车道要过官方客户端指纹校验，Kilo
#: 池什么都不用但会记录提示词）。列宽有限，列里放短名，完整名与说明进悬停提示。
_SOURCE_SHORT = {
    "zen": "Zen 免费车道",
    "kilo": "Kilo 免费池",
}

#: 只有 chat 线的免费模型才进鸡蛋目录：responses / messages 线需要另一套
#: 请求与回程转换，硬塞进 chat 只会得到 500。
_RESPONSES_ONLY = {"muse-spark-1.2-contributor-free",
                   "muse-spark-1.3-contributor-free"}
_MESSAGES_ONLY = {"union-alpha"}
_SYSTEM_ONE_RE = re.compile(r"^jev")


def _base_model(model):
    """去掉 Zen 的 ``(light|balanced|deep)`` 思考档位后缀。"""
    text = str(model or "").strip()
    if text.endswith(")"):
        idx = text.rfind("(")
        if idx > 0:
            return text[:idx].strip()
    return text


def _display_name(model_id):
    if model_id in _ZEN_NAMES:
        return _ZEN_NAMES[model_id]
    words = re.split(r"[-_.]", model_id)
    out = []
    for word in words:
        if not word:
            continue
        out.append(word[:1].upper() + word[1:] if not word[:1].isdigit() else word)
    return " ".join(out) or model_id


def _zen_caps(model_id):
    for pattern, vision, reasoning, context, output in _ZEN_CAPS:
        if pattern.search(model_id):
            return {"vision": vision, "reasoning": reasoning,
                    "context": context, "output": output}
    return {"vision": False, "reasoning": False,
            "context": 131072, "output": 32768}


def _zen_model(model_id):
    caps = _zen_caps(model_id)
    return {
        "id": model_id,
        "name": _display_name(model_id),
        "source": "zen",
        "sourceLabel": "OpenCode Zen 免费车道",
        "context": caps["context"],
        "output": caps["output"],
        "vision": bool(caps["vision"]),
        "reasoning": bool(caps["reasoning"]),
        "tools": True,
        "blurb": _ZEN_BLURBS.get(model_id, "OpenCode Zen 免费车道模型，无需登录与计费。"),
    }


def _kilo_name(row):
    name = str(row.get("name") or "").strip() or str(row.get("id") or "")
    name = re.sub(r"^[^:]{1,40}:\s+", "", name)
    name = re.sub(r"\s*\(free\)\s*$", "", name, flags=re.I).strip()
    return name or str(row.get("id") or "")


def _kilo_model(row):
    model_id = str(row.get("id") or "").strip()
    context = 0
    try:
        context = int(row.get("context_length") or 0)
    except (TypeError, ValueError):
        context = 0
    top = row.get("top_provider") or {}
    if context <= 0 and isinstance(top, dict):
        try:
            context = int(top.get("context_length") or 0)
        except (TypeError, ValueError):
            context = 0
    output = 32768
    if isinstance(top, dict):
        try:
            n = int(top.get("max_completion_tokens") or 0)
            if n > 0:
                output = n
        except (TypeError, ValueError):
            pass
    vision = False
    arch = row.get("architecture") or {}
    if isinstance(arch, dict):
        mods = arch.get("input_modalities") or []
        if isinstance(mods, list):
            vision = any(str(m).lower() == "image" for m in mods)
    reasoning = False
    params = row.get("supported_parameters") or []
    if isinstance(params, list):
        reasoning = any(str(p).lower() in ("reasoning", "include_reasoning")
                        for p in params)
    return {
        "id": model_id,
        "name": _kilo_name(row),
        "source": "kilo",
        "sourceLabel": "Kilo 免费池",
        "context": context or 131072,
        "output": output,
        "vision": vision,
        "reasoning": reasoning,
        "tools": True,
        "blurb": "Kilo 免费池模型，无需任何凭据；免费池提示词可能被上游记录，别交机密内容。",
    }


# ---------------------------------------------------------------------------
# 会话与指纹（OpenCode 免费车道）
# ---------------------------------------------------------------------------

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SESSION_RE = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")

_sessions = {}
_sessions_lock = threading.Lock()
_session_remint_after = 6 * 24 * 3600


def _mint(prefix):
    now_ms = int(time.time() * 1000)
    value = (~(now_ms << 12 | 1)) & 0xFFFFFFFFFFFF
    raw = value.to_bytes(6, "big")
    rnd = os.urandom(14)
    return "%s_%s%s" % (prefix, raw.hex(),
                        "".join(_BASE62[b % 62] for b in rnd))


def session_for(seed):
    """为一个对话稳定地铸造一个上游会话 id（免费额度按会话计）。"""
    seed = str(seed or "").strip() or "global"
    if _SESSION_RE.match(seed):
        return seed
    now = time.time()
    with _sessions_lock:
        hit = _sessions.get(seed)
        if hit and now - hit[1] < _session_remint_after:
            return hit[0]
        if len(_sessions) > 4096:
            _sessions.clear()
        sid = _mint("ses")
        _sessions[seed] = (sid, now)
        return sid


def _zen_headers(session, stream=True):
    accept = "text/event-stream" if stream else "*/*"
    return {
        "content-type": "application/json",
        "authorization": "Bearer public",
        "user-agent": ZEN_UA,
        "x-opencode-client": "cli",
        "x-opencode-session": session,
        "x-opencode-session-id": session,
        "x-session-affinity": session,
        "x-session-id": session,
        "x-opencode-project": "global",
        "accept": accept,
    }


def _tool_name(tool):
    if not isinstance(tool, dict):
        return ""
    name = str(tool.get("name") or "").strip()
    if not name:
        fn = tool.get("function")
        if isinstance(fn, dict):
            name = str(fn.get("name") or "").strip()
    return name


def _function_of(tool):
    fn = tool.get("function") if isinstance(tool, dict) else None
    return fn if isinstance(fn, dict) else None


def _fingerprint_def(name):
    if name == "read":
        return _READ_DESC, _READ_PARAMS
    if name == "shell":
        return _SHELL_DESC, _SHELL_PARAMS
    return "", None


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    except Exception:
        return ""


def _is_fingerprint_def(tool, name):
    desc, params = _fingerprint_def(name)
    body = _function_of(tool) or tool
    if not isinstance(body, dict):
        return False
    return (str(body.get("description") or "") == desc
            and _canonical(body.get("parameters")) == _canonical(params))


def apply_fingerprint(body):
    """补齐上游要求的 read / shell 工具定义，返回发送名 → 调用方名 的映射。

    Claude Code 这类客户端自带 ``read`` / ``shell`` 同名工具但定义不同：
    直接追加会重名被拒，所以把调用方的那个改名为 ``read_client``，回程再
    还原。只影响发往 OpenCode 的请求体。
    """
    tools = body.get("tools")
    tools = list(tools) if isinstance(tools, list) else []
    taken = {}
    for tool in tools:
        name = _tool_name(tool)
        if name:
            taken[name] = True
    rename = {}
    out = []
    seen = set()
    for tool in tools:
        name = _tool_name(tool)
        if name not in FINGERPRINT_TOOLS:
            out.append(tool)
            continue
        if name in seen:
            continue
        seen.add(name)
        if _is_fingerprint_def(tool, name):
            out.append(tool)
            continue
        alias = name + "_client"
        suffix = 2
        while taken.get(alias):
            alias = "%s_client%d" % (name, suffix)
            suffix += 1
        taken[alias] = True
        rename[alias] = name
        if _function_of(tool) is not None:
            cloned = dict(tool)
            fn = dict(_function_of(tool))
            fn["name"] = alias
            cloned["function"] = fn
        else:
            cloned = dict(tool)
            cloned["name"] = alias
        out.append(cloned)
    appended = []
    for name in FINGERPRINT_TOOLS:
        present = any(_tool_name(t) == name for t in out)
        if present:
            continue
        desc, params = _fingerprint_def(name)
        out.append({"type": "function",
                    "function": {"name": name, "description": desc,
                                 "parameters": params}})
        appended.append(name)
    body["tools"] = out
    if rename:
        _retarget_tool_choice(body, rename)
        _alias_history(body, rename)
    if appended:
        _note_placeholder_tools(body, appended)
    return rename


def _retarget_tool_choice(body, rename):
    aliases = dict((orig, alias) for alias, orig in rename.items())
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return
    name = str(choice.get("name") or "").strip()
    if name and aliases.get(name):
        choice["name"] = aliases[name]
        return
    fn = choice.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "").strip()
        if name and aliases.get(name):
            fn["name"] = aliases[name]


def _alias_history(body, rename):
    aliases = dict((orig, alias) for alias, orig in rename.items())
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict) and aliases.get(str(fn.get("name") or "")):
                fn["name"] = aliases[str(fn.get("name"))]


def _note_placeholder_tools(body, names):
    quoted = " or ".join("`%s`" % n for n in names)
    note = ("Do not call the %s tools; they are placeholders that do nothing. "
            "Use the other tools you have been given instead." % quoted)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        body["messages"] = [{"role": "system", "content": note}]
        return
    first = messages[0]
    if isinstance(first, dict) and first.get("role") == "system" \
            and isinstance(first.get("content"), str):
        cloned = dict(first)
        cloned["content"] = first["content"] + "\n\n" + note
        body["messages"] = [cloned] + messages[1:]
        return
    body["messages"] = [{"role": "system", "content": note}] + messages


def restore_tool_name(name, rename):
    if rename and name in rename:
        return rename[name]
    return name


# ---------------------------------------------------------------------------
# 目录状态 / 缓存
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_state = {
    "at": 0.0,
    "models": [],
    "sources": {},
    "note": "",
}
_data_dir = ""
_log = lambda msg: None
_loaded = False
_status_cache = None


def configure(directory, log=None):
    """设置运行期目录与日志函数；重复调用安全。"""
    global _data_dir, _log, _loaded
    _data_dir = str(directory or "")
    if log is not None:
        _log = log
    if not _loaded and _data_dir:
        _loaded = True
        _load_cache()


def _catalog_path():
    if not _data_dir:
        return ""
    return os.path.join(_data_dir, CATALOG_FILENAME)


def _load_cache():
    path = _catalog_path()
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        _log("egg catalog: 读取缓存失败（%s）" % exc)
        return
    models = data.get("models")
    if not isinstance(models, list) or not models:
        return
    with _lock:
        _state["models"] = [m for m in models if isinstance(m, dict) and m.get("id")]
        _state["at"] = float(data.get("at") or 0.0)
        _state["sources"] = data.get("sources") if isinstance(data.get("sources"), dict) else {}
        _state["note"] = str(data.get("note") or "")


def _save_cache():
    path = _catalog_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"at": _state["at"], "models": _state["models"],
                       "sources": _state["sources"], "note": _state["note"]},
                      fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        _log("egg catalog: 写入缓存失败（%s）" % exc)


def _static_models():
    models = []
    for mid in ZEN_FALLBACK_IDS:
        if _SYSTEM_ONE_RE.search(mid):
            continue
        models.append(_zen_model(mid))
    for mid in KILO_FALLBACK_IDS:
        models.append({
            "id": mid,
            "name": _display_name(mid.split("/")[-1].replace(":free", "")),
            "source": "kilo",
            "sourceLabel": "Kilo 免费池",
            "context": 131072,
            "output": 32768,
            "vision": False,
            "reasoning": False,
            "tools": True,
            "blurb": "Kilo 免费池模型，无需任何凭据；免费池提示词可能被上游记录，别交机密内容。",
        })
    return models


def _http_json(url, headers, timeout):
    from wb_accounts import urlopen
    req = urllib.request.Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read(8 << 20)
    return json.loads(raw.decode("utf-8", "replace"))


def fetch_zen_listing(timeout=LIST_TIMEOUT_SECONDS):
    """拉免费车道目录，过滤出真正免费的 chat 模型。"""
    session = session_for("catalog:egg")
    payload = _http_json(ZEN_BASE + "/zen/v1/models",
                         _zen_headers(session, stream=False), timeout)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    ids = []
    for row in rows:
        if isinstance(row, str):
            mid = row.strip()
        elif isinstance(row, dict):
            mid = str(row.get("id") or "").strip()
        else:
            continue
        if not mid:
            continue
        base = _base_model(mid)
        if base in _RESPONSES_ONLY or base in _MESSAGES_ONLY:
            continue
        if _SYSTEM_ONE_RE.search(base):
            continue
        if base in _ALWAYS_FREE or _FREE_RE.search(base):
            ids.append(base)
    return ids


def fetch_kilo_listing(timeout=LIST_TIMEOUT_SECONDS):
    """拉 Kilo 免费池目录（只保留 isFree 行）。"""
    payload = _http_json(KILO_BASE + "/models",
                         {"accept": "application/json", "user-agent": KILO_UA},
                         timeout)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows
            if isinstance(row, dict) and row.get("isFree") is True
            and str(row.get("id") or "").strip()]


def _merge_models(zen_ids, kilo_rows):
    models = []
    seen = set()
    for mid in zen_ids:
        if mid in seen:
            continue
        seen.add(mid)
        models.append(_zen_model(mid))
    for row in kilo_rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append(_kilo_model(row))
    return models


def _fetch_source(name, fn):
    try:
        return name, fn(), ""
    except Exception as exc:
        return name, None, str(exc)


_refresh_lock = threading.Lock()


def refresh(force=True, timeout=LIST_TIMEOUT_SECONDS):
    """刷新两个免费源；失败的源保留上一次结果。返回目录快照。"""
    now = time.time()
    with _lock:
        if not force and _state["models"] and now - _state["at"] < CATALOG_TTL_SECONDS:
            return snapshot()
        if force and _state["at"] and now - _state["at"] < 3:
            # 刚刚刷过（切到鸡蛋时的后台预热与面板强制刷新可能撞在一起）：
            # 直接复用，别把上游放大成两倍请求。
            return snapshot()
    if not _refresh_lock.acquire(blocking=False):
        # 已经有一轮刷新在跑：等它结束，用它的结果（而不是再打一次上游）。
        with _refresh_lock:
            return snapshot()
    try:
        return _refresh_locked(force, timeout)
    finally:
        _refresh_lock.release()


def _refresh_locked(force, timeout):
    """真正取数的那一半；调用方已持有 _refresh_lock。"""
    with _lock:
        previous = {
            "zen": [m for m in _state["models"] if m.get("source") == "zen"],
            "kilo": [m for m in _state["models"] if m.get("source") == "kilo"],
        }
    results = {}
    threads = []
    for name, fn in (("zen", fetch_zen_listing), ("kilo", fetch_kilo_listing)):
        thread = threading.Thread(
            target=lambda n=name, f=fn: results.__setitem__(n, _fetch_source(n, f)),
            name="egg-catalog-%s" % name)
        thread.daemon = True
        thread.start()
        threads.append(thread)
    deadline = time.time() + timeout + 3
    for thread in threads:
        thread.join(max(0.0, deadline - time.time()))
    zen_ids = None
    kilo_rows = None
    sources = {}
    errors = []
    for name in ("zen", "kilo"):
        got = results.get(name)
        if got is None:
            sources[name] = {"ok": False, "count": len(previous[name]),
                             "error": "请求超时"}
            errors.append("%s 超时" % name)
            continue
        _, value, error = got
        if error:
            sources[name] = {"ok": False, "count": len(previous[name]),
                             "error": error[:200]}
            errors.append("%s: %s" % (name, error[:120]))
            continue
        if not value:
            sources[name] = {"ok": False, "count": len(previous[name]),
                             "error": "上游没有返回免费模型"}
            errors.append("%s 空目录" % name)
            continue
        sources[name] = {"ok": True, "count": len(value), "error": ""}
        if name == "zen":
            zen_ids = value
        else:
            kilo_rows = value
    if zen_ids is None:
        zen_models = previous["zen"]
    else:
        zen_models = [_zen_model(mid) for mid in zen_ids if mid]
    if kilo_rows is None:
        kilo_models = previous["kilo"]
    else:
        kilo_models = [_kilo_model(row) for row in kilo_rows
                       if isinstance(row, dict)]
    models = []
    seen = set()
    for model in zen_models + kilo_models:
        mid = model.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append(model)
    if not models:
        models = list(_state["models"]) or _static_models()
        sources = {k: dict(v) for k, v in (_state.get("sources") or {}).items()}
    with _lock:
        _state["at"] = time.time()
        _state["models"] = models
        _state["sources"] = sources
        _state["note"] = "；".join(errors)
        _save_cache()
    if errors:
        _log("egg catalog: %s；保留上一次可用结果" % "；".join(errors))
    else:
        _log("egg catalog: 已刷新 %d 个免费模型（Zen %d / Kilo %d）"
             % (len(models), sources.get("zen", {}).get("count", 0),
                sources.get("kilo", {}).get("count", 0)))
    return snapshot()


def ensure(directory=None, timeout=LIST_TIMEOUT_SECONDS):
    """按 TTL 取目录；冷启动或过期时自动走一次刷新。"""
    if directory is not None:
        configure(directory)
    with _lock:
        if _state["models"] and time.time() - _state["at"] < CATALOG_TTL_SECONDS:
            return snapshot()
    return refresh(force=False, timeout=timeout)


def snapshot():
    with _lock:
        return {
            "at": _state["at"],
            "models": [dict(m) for m in _state["models"]],
            "sources": json.loads(json.dumps(_state["sources"] or {})),
            "note": _state["note"],
        }


def status():
    snap = snapshot()
    models = snap["models"]
    if not models:
        models = _static_models()
    by_source = {"zen": 0, "kilo": 0}
    for model in models:
        source = model.get("source")
        if source in by_source:
            by_source[source] += 1
    return {
        "at": snap["at"],
        "count": len(models),
        "zen": by_source["zen"],
        "kilo": by_source["kilo"],
        "sources": snap["sources"],
        "note": snap["note"],
        "pools": pool_status(),
        "stale": bool(snap["at"] and time.time() - snap["at"] > CATALOG_TTL_SECONDS),
    }


def pool_status():
    """自建供应商池的状态（面板的池子卡片与概览计数用）。"""
    try:
        import wb_egg_pools
    except Exception:
        return []
    try:
        return wb_egg_pools.pool_summaries()
    except Exception:
        return []


def catalog(directory=None):
    """给 /v1/models 用的当前目录（必要时刷新）。"""
    models = ensure(directory).get("models") or _static_models()
    return merge_models(models)


def merge_models(builtin_models):
    """把自建供应商池的模型并进清单：**自建池优先**。

    同名 id 只能有一个去处，请求也是按「自建池优先」发的（见 resolve），
    清单必须跟请求走同一条路：内置免费源里有同名模型时（例如 Kilo 免费池
    也有 minimax/minimax-m3.1-flash:free），让位给自建池。

    不这样的话，面板会把这模型标成「Kilo 免费池」—— 看着像走免费池，实际
    流量去了自建池；而自建池那一行因为 id 撞了被丢掉，池子就永远不出现在
    「池子」下拉里（操作者会以为池子没生效）。
    """
    pools = pool_models()
    if not pools:
        return list(builtin_models or [])
    claimed = set()
    for model in pools:
        mid = model.get("id") if isinstance(model, dict) else None
        if mid:
            claimed.add(mid)
    merged = []
    for model in builtin_models or []:
        mid = model.get("id") if isinstance(model, dict) else None
        if mid and mid in claimed:
            continue
        merged.append(model)
    merged.extend(pools)
    return merged


def model_entry_meta(model):
    """把目录条目转成 wb_proxy.model_entry() 认识的元数据。"""
    source = model.get("source") or "zen"
    kind = model.get("sourceKind") or "builtin"
    # 免费池归属：面板的模型清单按它标出每个模型来自 Zen 免费车道、Kilo
    # 免费池，还是操作者自建的供应商池（两者限流与地区门口径不同，得一眼
    # 能分）。自建池用池子的名字当标签。
    label = (_SOURCE_SHORT.get(source)
             or model.get("sourceLabel")
             or source)
    # 免费与否：自建池里也有上游明确免费的模型（id 结尾 :free / /free，或
    # 上游报 0 价）。这类模型不消耗额度，别说成「按供应商计费」。
    free = bool(model.get("free"))
    if kind == "pool":
        if free:
            credits_note = ("自建供应商池「%s」里的免费模型（上游标 :free）："
                            "不消耗额度" % label)
        else:
            credits_note = ("自建供应商池「%s」的模型：用操作者自己的 Key，"
                            "费用由该供应商结算" % label)
    else:
        credits_note = "鸡蛋免费模型：无需账号或密钥（%s）" % (model.get("sourceLabel") or source)
    meta = {
        "name": model.get("name") or model.get("id"),
        "descriptionZh": model.get("blurb") or "",
        "ownedBy": source,
        "supportsImages": bool(model.get("vision")),
        "supportsToolCall": bool(model.get("tools", True)),
        "supportsReasoning": bool(model.get("reasoning")),
        "maxInputTokens": model.get("context"),
        "maxOutputTokens": model.get("output"),
        "credits": "0.00",
        "credits_note": credits_note,
        "egg_source": source,
        "egg_source_label": label,
        "egg_source_kind": kind,
    }
    if free:
        meta["egg_free"] = True
    return meta


def _find_model(model_id):
    base = _base_model(model_id)
    snap = snapshot()
    for model in snap["models"]:
        if model.get("id") == model_id or model.get("id") == base:
            return model
    for model in _static_models():
        if model.get("id") == model_id or model.get("id") == base:
            return model
    return None


def _pool_for_model(model_id):
    """这个模型属于哪个自建供应商池（没有就 None）。

    池子是操作者自己加的：优先级高于内置的免费源 —— 他显式挑过这个模型，
    就该走他指定的那一家，而不是被同名的免费模型截胡。
    """
    try:
        import wb_egg_pools
    except Exception:
        return None
    try:
        return wb_egg_pools.find_by_model(model_id)
    except Exception:
        return None


def pool_models():
    """自建供应商池提供的模型（与内置目录同形态）。"""
    try:
        import wb_egg_pools
    except Exception:
        return []
    try:
        return wb_egg_pools.catalog_entries()
    except Exception:
        return []


def provider_hint(model):
    """没有显式 provider 时，这个模型是不是鸡蛋的。只看本地目录，不发网络。"""
    name = str(model or "").strip()
    if not name:
        return None
    if _pool_for_model(name) is not None:
        return "egg"
    if _find_model(name) is not None:
        return "egg"
    base = _base_model(name)
    if base.endswith("-free") and "/" not in base:
        return "egg"
    return None


def resolve(model_id):
    """返回 (source, 发送用的模型 id, 基础模型 id)；未知模型返回 None。"""
    name = str(model_id or "").strip()
    if not name:
        return None
    base = _base_model(name)
    pool = _pool_for_model(name)
    if pool is not None:
        # 自建池：source 是池子 id，模型 id 原样发给上游。
        return pool["id"], name, name
    found = _find_model(base)
    if found is not None:
        return found.get("source"), name, base
    if base.endswith("-free") and "/" not in base:
        return "zen", name, base
    if "/" in base and (base.endswith(":free") or base.startswith("kilo-auto/")):
        return "kilo", name, base
    return None


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------

_ALLOWED_CHAT_FIELDS = (
    "messages", "temperature", "top_p", "top_k", "stop", "seed", "user",
    "presence_penalty", "frequency_penalty", "response_format",
    "parallel_tool_calls", "tool_choice", "tools", "max_tokens",
    "max_completion_tokens", "reasoning", "reasoning_effort",
)


def _base_body(payload):
    body = {}
    for key in _ALLOWED_CHAT_FIELDS:
        if key in payload and payload.get(key) is not None:
            body[key] = payload[key]
    if "max_tokens" not in body and body.get("max_completion_tokens"):
        body["max_tokens"] = body.pop("max_completion_tokens")
    body.pop("max_completion_tokens", None)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    return body


def build_request(payload, session_key=""):
    """构造一次免费上游请求；返回 (url, body, headers, rename, source, billed)。

    自建供应商池的模型走 wb_egg_pools：协议、Base URL 与 Key 都由池子自己
    决定，Anthropic 协议还要把事件流翻成 OpenAI 形状（多一个 sse 转换器）。
    """
    chat = dict(payload or {})
    model = str(chat.get("model") or "").strip()
    pool = _pool_for_model(model)
    if pool is not None:
        url, body, headers, sse, source, billed = _pool_request(pool, chat)
        return url, body, headers, {}, source, billed, sse
    resolved = resolve(model)
    if resolved is None:
        raise EggError(400, "模型 '%s' 不在鸡蛋免费目录里：先切到鸡蛋网关刷新白名单，"
                            "或从 /v1/models?provider=egg 里挑一个。" % model)
    source, send_model, base = resolved
    body = _base_body(chat)
    body["model"] = send_model
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    rename = {}
    if source == "kilo":
        url = KILO_BASE + "/chat/completions"
        headers["user-agent"] = KILO_UA
    else:
        url = ZEN_BASE + "/inference/v1/chat/completions"
        headers = _zen_headers(session_for(session_key or "global"), stream=True)
        rename = apply_fingerprint(body)
    return url, body, headers, rename, source, base, None


def _pool_request(pool, chat):
    """把请求交给自建供应商池（wb_egg_pools）。"""
    try:
        import wb_egg_pools
    except Exception as exc:
        raise EggError(500, "自建供应商池不可用：%s" % exc)
    try:
        return wb_egg_pools.build_request(pool, chat)
    except wb_egg_pools.PoolError as exc:
        raise EggError(exc.status, exc.detail)
    except Exception as exc:
        raise EggError(502, "自建供应商池请求失败：%s" % exc)


class EggUpstream(object):
    """包住 urllib 响应：暴露 read1()，并在回程还原被改名的工具。

    ``sse`` 是一个可选的行级转换器（Anthropic 协议用它把事件流翻成 OpenAI
    形状）。转换器吃一整行、吐一整段字节；吐空串表示这行没有对应的输出。
    """

    def __init__(self, resp, source, billed_model, rename=None, sse=None):
        self._raw = resp
        self._source = source
        self._rename = rename or {}
        self._sse = sse
        self._pending = b""
        self._done = False
        self._wb_provider = "egg"
        self._wb_billed_model = billed_model
        self.fp = getattr(resp, "fp", None)
        self.headers = getattr(resp, "headers", None)

    def _transform(self, line):
        if self._sse is not None:
            # 转换器是一个带 feed() 的对象（wb_egg_pools.AnthropicStream）：
            # 吃一整行原始 SSE，吐一段 OpenAI 形状的字节。
            try:
                feed = getattr(self._sse, "feed", None)
                out = feed(line) if feed is not None else self._sse(line)
                return out or b""
            except Exception as exc:
                # 转换器自己炸了：这一行丢掉，别把整条流带崩（下游会看到
                # 上游少了一个事件，但连接仍然是好的）。
                _log("egg sse transform failed: %s" % exc)
                return b""
        if not self._rename:
            return line
        text = line.decode("utf-8", "replace").strip()
        if not text.startswith("data:"):
            return line
        data = text[5:].strip()
        if not data or data == "[DONE]":
            return line
        try:
            obj = json.loads(data)
        except Exception:
            return line
        changed = False
        for choice in obj.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            for holder in (choice.get("delta") or {}, choice.get("message") or {}):
                if not isinstance(holder, dict):
                    continue
                calls = holder.get("tool_calls")
                if isinstance(calls, list):
                    for call in calls:
                        fn = call.get("function") if isinstance(call, dict) else None
                        if isinstance(fn, dict):
                            name = str(fn.get("name") or "")
                            restored = restore_tool_name(name, self._rename)
                            if restored != name:
                                fn["name"] = restored
                                changed = True
                fc = holder.get("function_call")
                if isinstance(fc, dict):
                    name = str(fc.get("name") or "")
                    restored = restore_tool_name(name, self._rename)
                    if restored != name:
                        fc["name"] = restored
                        changed = True
        if not changed:
            return line
        return ("data: %s\n\n" % json.dumps(obj, ensure_ascii=False)).encode("utf-8")

    def read1(self, size=-1):
        while not self._pending and not self._done:
            line = self._raw.readline()
            if not line:
                self._done = True
                # 上游没发收尾事件就断了：让转换器补一个收尾（Anthropic 少了
                # message_stop 时，客户端会一直等不到 finish_reason）。
                if self._sse is not None:
                    closer = getattr(self._sse, "close", None)
                    if closer is not None:
                        try:
                            self._pending = closer() or b""
                        except Exception:
                            self._pending = b""
                break
            self._pending = self._transform(line)
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
        try:
            self._raw.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _error_detail(status, raw, pool=None, model=""):
    text = ""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        text = str(raw)
    message = ""
    try:
        obj = json.loads(text)
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("type") or "")
        elif isinstance(err, str):
            message = err
        elif isinstance(obj, dict):
            message = str(obj.get("message") or "")
    except Exception:
        message = text[:300]
    if pool:
        # 自建供应商池：是自己 的 Key 对着 自己的上游 —— 报错要说清楚是谁拒的，
        # 别写成「免费源拒绝访问」（那是内置免费源的口径，操作者会以为要去
        # 换出口或换免费模型）。
        name = str(pool.get("name") or pool.get("id") or "自建池")
        if status == 402:
            out = "自建供应商池「%s」余额不足：%s" % (name, message or "HTTP 402")
        elif status == 429:
            out = "自建供应商池「%s」限流：%s" % (name, message or "HTTP 429")
        elif status in (401, 403):
            out = "自建供应商池「%s」拒绝访问：%s" % (
                name, message or ("HTTP %d" % status))
        else:
            out = message or ("HTTP %d" % status)
        hint = ""
        try:
            import wb_egg_pools
            # 原文要一起给：403 有几种不同含义（Key 类型不对 / 地区不开放 /
            # 只给 agentic harness 用），光看状态码分不出来。
            hint = wb_egg_pools.error_hint(pool.get("base_url"), status, model,
                                           text)
        except Exception:
            hint = ""
        return out + (("（%s）" % hint) if hint else "")
    if status == 403 and ("FreeTierError" in text or "free" in message.lower()):
        return ("免费车道拒绝了本机出口（上游判定该 IP 有滥用流量）。"
                "换一个出口网络，或改用 Kilo 免费池里的模型。")
    if status == 429:
        return ("免费源限流（%s）。稍后重试，或换一个免费模型。"
                % (message or "429"))
    if status == 401 or status == 403:
        return "免费源拒绝访问：%s" % (message or ("HTTP %d" % status))
    return message or ("HTTP %d" % status)


def open_stream(payload, session_key="", timeout=600):
    """打开一次免费上游流；返回 (EggUpstream, 元数据)。"""
    url, body, headers, rename, source, base, sse = build_request(payload,
                                                                  session_key)
    from wb_accounts import urlopen
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        resp = urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = _error_detail(exc.code, exc.read(4 << 10),
                                    _pool_of(payload),
                                    str((payload or {}).get("model") or ""))
        except Exception:
            detail = "HTTP %d" % exc.code
        raise EggError(exc.code, detail,
                       retry_after=_retry_after(exc.headers))
    except EggError:
        raise
    except Exception as exc:
        pool = _pool_of(payload)
        if pool:
            raise EggError(502, "自建供应商池「%s」不可达：%s"
                           % (pool.get("name") or pool.get("id") or "自建池", exc))
        raise EggError(502, "免费源不可达：%s" % exc)
    return (EggUpstream(resp, source, base, rename=rename, sse=sse),
            {"source": source, "model": base})


def _pool_of(payload):
    """这个请求要发给哪个自建池（不是自建池就 None）。

    报错文案要分清「内置免费源」与「自建供应商池」：后者是操作者自己的
    Key 对着自己的上游，被拒了要说的是「池子拒绝访问 + 怎么换 Key」。
    """
    model = str((payload or {}).get("model") or "")
    if not model:
        return None
    return _pool_for_model(model)


def _retry_after(headers):
    try:
        value = int((headers or {}).get("retry-after") or 0)
        return value if 0 < value < 3600 else 0
    except Exception:
        return 0
