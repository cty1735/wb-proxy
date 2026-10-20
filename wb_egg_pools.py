"""鸡蛋网关的「供应商池」：面板里自建的多个上游（OpenAI / Anthropic 兼容）。

内置的两个免费源（OpenCode Zen 免费车道 / Kilo 免费池）是写死的，模型清单由
上游自己给（见 wb_zen）。这份文件管的是**操作者自己加的供应商**：每个供应商
是一个池子，各有各的 Base URL、API Key、协议与一份自己挑过的模型清单。面板的
模型清单按池子筛选，看清单时一眼就知道这条模型走的是哪一家。

存在 ``accounts/egg-pools.json``（跟 zen-catalog.json 放在一起）：

* 写入用 ``os.replace`` 原子替换，写坏一次不会留下半个文件；
* API Key 明文保存 —— 与 ``settings.json`` 里的 API Key 同一口径（面板本来
  就要能回显密钥，这台机器上不藏）。

只依赖标准库 + ``wb_accounts.urlopen``（沿用项目的代理 / 连接池行为）。
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

#: 池子清单落盘文件名（放在 accounts 目录里，跟其它运行期状态一起）。
POOLS_FILENAME = "egg-pools.json"

#: 拉模型清单的超时。自建供应商大多在公网上，给得比免费源宽一点。
LIST_TIMEOUT_SECONDS = 20

#: 「测试这个池子能不能用」的超时：只发一个 token 的最小请求。
TEST_TIMEOUT_SECONDS = 30

PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOLS = (PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC)

#: 协议下拉里的中文名（面板用）。
PROTOCOL_LABELS = {
    PROTOCOL_OPENAI: "OpenAI 兼容",
    PROTOCOL_ANTHROPIC: "Anthropic 兼容",
}

#: Anthropic Messages API 的版本头。
ANTHROPIC_VERSION = "2023-06-01"

#: 面板「免费模型预设」里的快捷项：点一下就把名称 / Base URL 填进表单。
#: 这些都是各自官方文档里给的 OpenAI 兼容入口，Key 仍要操作者自己填。
PRESETS = [
    {"name": "NVIDIA NIM", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://integrate.api.nvidia.com/v1",
     "host": "integrate.api.nvidia.com"},
    {"name": "Google AI Studio (Gemini)", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "host": "generativelanguage.googleapis.com"},
    {"name": "GitHub Models", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://models.github.ai/inference",
     "host": "models.github.ai"},
    {"name": "Groq", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://api.groq.com/openai/v1",
     "host": "api.groq.com"},
    {"name": "Cerebras", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://api.cerebras.ai/v1",
     "host": "api.cerebras.ai"},
    {"name": "Mistral", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://api.mistral.ai/v1",
     "host": "api.mistral.ai"},
    {"name": "OpenRouter", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://openrouter.ai/api/v1",
     "host": "openrouter.ai"},
    {"name": "HuggingFace Router", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://router.huggingface.co/v1",
     "host": "router.huggingface.co"},
    {"name": "硅基流动 SiliconFlow", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://api.siliconflow.cn/v1",
     "host": "api.siliconflow.cn"},
    {"name": "魔搭 ModelScope", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://api-inference.modelscope.cn/v1",
     "host": "api-inference.modelscope.cn"},
    {"name": "智谱 BigModel", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "host": "open.bigmodel.cn"},
    {"name": "LongCat（美国）", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://api.longcat.chat/openai/v1",
     "host": "api.longcat.chat"},
    {"name": "LongCat（Anthropic 协议）", "protocol": PROTOCOL_ANTHROPIC,
     "base_url": "https://api.longcat.chat/anthropic",
     "host": "api.longcat.chat"},
    {"name": "Anthropic 官方", "protocol": PROTOCOL_ANTHROPIC,
     "base_url": "https://api.anthropic.com/v1",
     "host": "api.anthropic.com"},
    {"name": "OpenCode Go", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://opencode.ai/zen/v1",
     "host": "opencode.ai"},
]

#: 池子 id 的形态（面板自己生成的短 id，不给用户看）。
_POOL_ID_RE = re.compile(r"^pool-[0-9a-f]{8,32}$")

#: 上游报错时给一句「能照着做」的提示。原文往往没头没尾 —— OpenRouter 对
#: 拿错类型的 Key 直接回 "User not found"，光看这句没人知道该换哪把 Key。
_AUTH_HINTS = {
    "openrouter.ai": (
        "OpenRouter 推理要用「API Keys」页里的普通 Key；"
        "管理（Management）/置备（Provisioning）Key 只能用来管 Key，"
        "拿去做推理上游就会回 User not found"),
}

#: 免费模型被限流时的提示（免费模型都有每日额度）。
_FREE_QUOTA_HINTS = {
    "openrouter.ai": "OpenRouter 免费档的免费模型每天 50 次，用完要等隔天重置",
}


def error_hint(base_url, status, model=""):
    """这个上游报错时该怎么排查；没有提示就返回空串。"""
    host = str(base_url or "").strip().lower()
    text = str(model or "").strip().lower()
    if status in (401, 403):
        for suffix, hint in _AUTH_HINTS.items():
            if suffix in host:
                return hint
        return ""
    if status == 429 and (text.endswith(":free") or text.endswith("/free")):
        for suffix, hint in _FREE_QUOTA_HINTS.items():
            if suffix in host:
                return hint
    return ""

#: 一个池子最多记多少个模型（拉回来的清单可能几百条，全记下来文件会很大）。
MAX_CATALOG_MODELS = 2000
#: 一个池子最多对外暴露多少个模型。
MAX_SELECTED_MODELS = 500
#: 池子数量上限：再多就是误操作了。
MAX_POOLS = 64


class PoolError(Exception):
    """一次池子操作失败，带 HTTP 状态与一句能直接展示的说明。"""

    def __init__(self, status, detail):
        Exception.__init__(self, detail)
        self.status = int(status or 400)
        self.detail = str(detail or "")


_lock = threading.RLock()
_state = {"pools": []}
#: 模型 id → 池子 id 的索引。聊天请求的每一条都要查「这个模型属于哪个池子」，
#: 逐池逐模型地扫是 O(池数×模型数)，请求多了就白烧 CPU。索引在每次写入后重建
#: （_save 只在增删改时调），读的时候不加锁也不拷贝。
_index = {"map": {}}
_data_dir = ""
_log = lambda msg: None
_loaded = False


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def configure(directory, log=None):
    """设置运行期目录与日志函数；重复调用安全。"""
    global _data_dir, _log, _loaded
    _data_dir = str(directory or "")
    if log is not None:
        _log = log
    if not _loaded and _data_dir:
        _loaded = True
        _load()


def _pools_path():
    if not _data_dir:
        return ""
    return os.path.join(_data_dir, POOLS_FILENAME)


def _load():
    path = _pools_path()
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        _log("egg pools: 读取失败（%s）" % exc)
        return
    rows = data.get("pools") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return
    pools = []
    for row in rows:
        entry = _normalize(row)
        if entry is not None:
            pools.append(entry)
    with _lock:
        _state["pools"] = pools
    _rebuild_index()


def _rebuild_index():
    """重建「模型 id → 池子 id」索引（每次写入后调一次）。"""
    mapping = {}
    for pool in _state["pools"]:
        if not pool["enabled"]:
            continue
        for mid in pool["models"]:
            # 同一个 id 被两个池子挑走时，先登记的赢：清单里一个 id 只能有一个
            # 去处，客户端按 id 挑模型不能随机命中。
            if mid not in mapping:
                mapping[mid] = pool["id"]
    _index["map"] = mapping


def _save():
    path = _pools_path()
    _rebuild_index()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pools": _state["pools"]}, fh,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        _log("egg pools: 写入失败（%s）" % exc)


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------

def _clean_base_url(url):
    """去掉末尾斜杠：Base URL 后面要接 /models 与 /chat/completions。"""
    text = str(url or "").strip()
    while text.endswith("/"):
        text = text[:-1]
    return text


def _clean_protocol(value):
    text = str(value or "").strip().lower()
    if text in ("anthropic", "anthropic-compatible", "claude"):
        return PROTOCOL_ANTHROPIC
    return PROTOCOL_OPENAI


def _mint_id():
    return "pool-%s" % os.urandom(8).hex()


def _normalize(entry):
    """把一条落盘的记录收拾成干净形态；没法用的返回 None。"""
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()
    base_url = _clean_base_url(entry.get("base_url"))
    if not name or not base_url:
        return None
    pool_id = str(entry.get("id") or "").strip()
    if not _POOL_ID_RE.match(pool_id):
        pool_id = _mint_id()
    models = []
    for item in entry.get("models") or []:
        mid = str(item or "").strip()
        if mid and mid not in models:
            models.append(mid)
        if len(models) >= MAX_SELECTED_MODELS:
            break
    catalog = []
    seen = set()
    for item in entry.get("catalog") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        catalog.append(_clean_model(mid, item))
        if len(catalog) >= MAX_CATALOG_MODELS:
            break
    try:
        catalog_at = float(entry.get("catalog_at") or 0.0)
    except (TypeError, ValueError):
        catalog_at = 0.0
    return {
        "id": pool_id,
        "name": name,
        "protocol": _clean_protocol(entry.get("protocol")),
        "base_url": base_url,
        "api_key": str(entry.get("api_key") or "").strip(),
        "enabled": entry.get("enabled", True) is not False,
        "models": models,
        "catalog": catalog,
        "catalog_at": catalog_at,
    }


def _is_free_id(model_id):
    """这个模型 id 是不是「免费」标记。

    上游把免费标在 id 结尾：OpenRouter 用 ``:free``（minimax/…:free），
    也有用 ``/free`` 的（kilo-auto/free、openrouter/free）。
    """
    text = str(model_id or "").strip().lower()
    return text.endswith(":free") or text.endswith("/free") or text == "free"


def _clean_model(model_id, row=None):
    """把上游的模型行收拾成清单条目（字段缺失就留空，不猜）。"""
    row = row if isinstance(row, dict) else {}
    name = str(row.get("name") or row.get("display_name") or "").strip()
    if not name:
        name = model_id
    context = _int(row.get("context_length") or row.get("context_window") or 0)
    output = _int(row.get("max_output_tokens") or row.get("max_completion_tokens") or 0)
    top = row.get("top_provider")
    if isinstance(top, dict):
        if not context:
            context = _int(top.get("context_length") or 0)
        if not output:
            output = _int(top.get("max_completion_tokens") or 0)
    vision = False
    arch = row.get("architecture")
    if isinstance(arch, dict):
        mods = arch.get("input_modalities")
        if isinstance(mods, list):
            vision = any(str(m).lower() == "image" for m in mods)
    if row.get("vision") is True:
        vision = True
    reasoning = False
    params = row.get("supported_parameters")
    if isinstance(params, list):
        reasoning = any(str(p).lower() in ("reasoning", "include_reasoning")
                        for p in params)
    if row.get("reasoning") is True:
        reasoning = True
    # 免费与否：id 结尾的 :free / /free 是一手标记；上游直接报 0 价也算
    # （OpenRouter 的免费模型就是这样标的）。标错了会让面板把免费模型
    # 说成「按供应商计费」，或者反之。
    free = _is_free_id(model_id)
    pricing = row.get("pricing")
    if isinstance(pricing, dict) and pricing:
        pin = pricing.get("prompt")
        pout = pricing.get("completion")
        if pin is not None or pout is not None:
            try:
                if float(pin or 0) == 0 and float(pout or 0) == 0:
                    free = True
            except (TypeError, ValueError):
                pass
    return {
        "id": model_id,
        "name": name,
        "context": context,
        "output": output,
        "vision": vision,
        "reasoning": reasoning,
        "tools": True,
        "free": free,
    }


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def pools():
    """全部池子（含明文 Key 与上游目录，给服务端自己用）。"""
    with _lock:
        return [dict(p, catalog=[dict(m) for m in p["catalog"]])
                for p in _state["pools"]]


def _record(pool):
    """池子的浅副本：不含上游目录。

    请求热路径上要读池子（Base URL / Key / 协议），而上游目录可能有上千行，
    每个请求都拷一遍纯属白烧 CPU —— 目录只有面板的「挑选模型」与刷新要用。
    """
    return {
        "id": pool["id"],
        "name": pool["name"],
        "protocol": pool["protocol"],
        "base_url": pool["base_url"],
        "api_key": pool["api_key"],
        "enabled": pool["enabled"],
        "models": list(pool["models"]),
        "catalog": [],
        "catalog_at": pool["catalog_at"],
    }


def get(pool_id):
    pid = str(pool_id or "").strip()
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] == pid:
                return dict(pool, catalog=[dict(m) for m in pool["catalog"]])
    return None


def find_by_model(model_id):
    """这个模型属于哪个池子（没有就 None）。

    只认**显式挑过**的模型：池子的 catalog 是「上游有什么」，models 才是
    「这个池子对外提供什么」。没挑过的模型不接流量，也不进清单。
    """
    mid = str(model_id or "").strip()
    if not mid:
        return None
    pool_id = _index["map"].get(mid)
    if not pool_id:
        return None
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] == pool_id:
                return _record(pool)
    return None


def enabled_pools():
    with _lock:
        return [dict(p) for p in _state["pools"] if p["enabled"]]


def pool_summaries():
    """池子的概要（不含 catalog 副本）：概览卡片与状态接口用。

    pools() 会把每条池子的上游目录整个拷一遍；概览只要名字、地址与条数，
    不值得为它拷那些模型行。
    """
    with _lock:
        return [{
            "id": p["id"],
            "name": p["name"],
            "protocol": p["protocol"],
            "base_url": p["base_url"],
            "enabled": p["enabled"],
            "count": len(p["models"]),
            "catalog_count": len(p["catalog"]),
            "catalog_at": p["catalog_at"],
        } for p in _state["pools"]]


def mask_key(key):
    """面板上显示的 Key：只留头尾，中间打点。"""
    text = str(key or "")
    if not text:
        return ""
    if len(text) <= 10:
        return text[:2] + "***"
    return "%s****%s" % (text[:5], text[-4:])


def public_pools():
    """给面板的视图：Key 打了码，catalog 原样带上（面板要显示模型清单）。"""
    out = []
    for pool in pools():
        pool["api_key_masked"] = mask_key(pool.get("api_key"))
        pool["protocol_label"] = PROTOCOL_LABELS.get(pool["protocol"], pool["protocol"])
        pool["model_count"] = len(pool.get("models") or [])
        pool["catalog_count"] = len(pool.get("catalog") or [])
        out.append(pool)
    return out


def catalog_entries():
    """池子里对外提供的模型（清单条目形态，和 wb_zen 的目录一致）。

    同一个模型 id 被两个池子挑走时，先登记的池子赢 —— 清单里一个 id 只能出现
    一次，否则客户端按 id 挑模型会随机命中。
    """
    entries = []
    seen = set()
    with _lock:
        pools_snapshot = [dict(p) for p in _state["pools"]]
    for pool in pools_snapshot:
        if not pool["enabled"]:
            continue
        by_id = dict((m.get("id"), m) for m in pool.get("catalog") or [])
        for mid in pool.get("models") or []:
            if mid in seen:
                continue
            seen.add(mid)
            meta = by_id.get(mid) or {}
            entries.append({
                "id": mid,
                "name": meta.get("name") or mid,
                "source": pool["id"],
                "sourceKind": "pool",
                "sourceLabel": pool["name"],
                "context": meta.get("context") or 0,
                "output": meta.get("output") or 0,
                "vision": bool(meta.get("vision")),
                "reasoning": bool(meta.get("reasoning")),
                "tools": True,
                # 免费与否：目录里存的是上次拉的结果（可能没有 free 字段），
                # 兜底看 id 结尾的 :free / /free。
                "free": bool(meta.get("free")) or _is_free_id(mid),
                "blurb": "自建供应商「%s」的模型（%s）：%s"
                         % (pool["name"], PROTOCOL_LABELS.get(pool["protocol"],
                                                            pool["protocol"]),
                            pool["base_url"]),
            })
    return entries


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def upsert(payload):
    """新建或更新一个池子。返回落盘后的池子。"""
    if not isinstance(payload, dict):
        raise PoolError(400, "池子配置必须是一个对象")
    pool_id = str(payload.get("id") or "").strip()
    name = str(payload.get("name") or "").strip()
    base_url = _clean_base_url(payload.get("base_url"))
    if not name:
        raise PoolError(400, "供应商名称不能为空")
    if not base_url:
        raise PoolError(400, "Base URL 不能为空")
    if not base_url.lower().startswith(("http://", "https://")):
        raise PoolError(400, "Base URL 必须以 http:// 或 https:// 开头")
    protocol = _clean_protocol(payload.get("protocol"))
    with _lock:
        existing = None
        for pool in _state["pools"]:
            if pool["id"] == pool_id:
                existing = pool
                break
        if existing is None:
            if len(_state["pools"]) >= MAX_POOLS:
                raise PoolError(400, "供应商数量已达上限（%d 个）" % MAX_POOLS)
            pool_id = _mint_id()
        else:
            name = name or existing["name"]
            base_url = base_url or existing["base_url"]
        # Key：留空表示「沿用已存的」—— 编辑时面板不回显明文，空着不改它。
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key and existing is not None:
            api_key = existing.get("api_key") or ""
        # 启用状态：payload 给了才算数；没给就沿用已存的（新建则默认开）。
        if "enabled" in payload:
            enabled = payload.get("enabled", True) is not False
        else:
            enabled = existing["enabled"] if existing else True
        catalog = existing["catalog"] if existing else []
        catalog_at = existing["catalog_at"] if existing else 0.0
        if isinstance(payload.get("catalog"), list):
            catalog = []
            for item in payload["catalog"]:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or "").strip()
                if not mid:
                    continue
                catalog.append(_clean_model(mid, item))
                if len(catalog) >= MAX_CATALOG_MODELS:
                    break
            catalog_at = time.time()
        models = existing["models"] if existing else []
        if "models" in payload:
            raw = payload.get("models")
            if not isinstance(raw, list):
                raise PoolError(400, "models 必须是模型 id 的数组")
            models = []
            for item in raw:
                mid = str(item or "").strip()
                if mid and mid not in models:
                    models.append(mid)
                if len(models) >= MAX_SELECTED_MODELS:
                    break
        entry = _normalize({
            "id": pool_id, "name": name, "protocol": protocol,
            "base_url": base_url, "api_key": api_key, "enabled": enabled,
            "models": models, "catalog": catalog, "catalog_at": catalog_at,
        })
        if entry is None:
            raise PoolError(400, "供应商配置不完整")
        if existing is None:
            _state["pools"].append(entry)
        else:
            index = _state["pools"].index(existing)
            _state["pools"][index] = entry
        _save()
    _log("egg pools: 保存供应商 %s（%s）" % (entry["name"], entry["base_url"]))
    return entry


def delete(pool_id):
    pid = str(pool_id or "").strip()
    with _lock:
        before = len(_state["pools"])
        _state["pools"] = [p for p in _state["pools"] if p["id"] != pid]
        if len(_state["pools"]) == before:
            return False
        _save()
    _log("egg pools: 删除供应商 %s" % pid)
    return True


def set_enabled(pool_id, enabled):
    pid = str(pool_id or "").strip()
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] == pid:
                pool["enabled"] = bool(enabled)
                _save()
                return dict(pool)
    return None


def set_models(pool_id, models):
    """只改「对外提供哪些模型」。"""
    pid = str(pool_id or "").strip()
    if not isinstance(models, list):
        raise PoolError(400, "models 必须是模型 id 的数组")
    cleaned = []
    for item in models:
        mid = str(item or "").strip()
        if mid and mid not in cleaned:
            cleaned.append(mid)
        if len(cleaned) >= MAX_SELECTED_MODELS:
            break
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] == pid:
                pool["models"] = cleaned
                _save()
                return dict(pool)
    raise PoolError(404, "供应商不存在")


# ---------------------------------------------------------------------------
# 拉模型清单
# ---------------------------------------------------------------------------

def _http_json(url, headers, timeout):
    from wb_accounts import urlopen
    req = urllib.request.Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read(8 << 20)
    return json.loads(raw.decode("utf-8", "replace"))


def _list_headers(protocol, api_key):
    headers = {"accept": "application/json",
               "user-agent": "wb-proxy/egg-pools"}
    key = str(api_key or "").strip()
    if protocol == PROTOCOL_ANTHROPIC:
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    elif key:
        headers["authorization"] = "Bearer " + key
    return headers


def _rows_of(payload):
    """各家 /models 的返回形状不统一，能认出来的都认出来。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "models", "result", "items"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return rows
    return []


def fetch_models(base_url, api_key, protocol, timeout=LIST_TIMEOUT_SECONDS):
    """拉一个供应商的模型清单。返回 (模型条目列表, 错误说明)。

    错误说明非空时列表可能为空：调用方要把它原样展示给操作者。
    """
    base = _clean_base_url(base_url)
    if not base:
        return [], "Base URL 不能为空"
    if not base.lower().startswith(("http://", "https://")):
        return [], "Base URL 必须以 http:// 或 https:// 开头"
    proto = _clean_protocol(protocol)
    url = base + "/models"
    try:
        payload = _http_json(url, _list_headers(proto, api_key), timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(4 << 10).decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        if exc.code in (401, 403):
            return [], "上游拒绝访问（HTTP %d）：API Key 可能不对%s" % (
                exc.code, ("：" + detail) if detail else "")
        if exc.code == 404:
            return [], ("上游没有 /models 接口（HTTP 404）：确认 Base URL "
                       "是否写到了版本号那一层")
        return [], "拉取失败（HTTP %d）%s" % (exc.code, detail)
    except Exception as exc:
        return [], "拉取失败：%s" % exc
    rows = _rows_of(payload)
    models = []
    seen = set()
    for row in rows:
        if isinstance(row, str):
            mid = row.strip()
            meta = {}
        elif isinstance(row, dict):
            mid = str(row.get("id") or row.get("name") or "").strip()
            meta = row
        else:
            continue
        if not mid or mid in seen:
            continue
        seen.add(mid)
        models.append(_clean_model(mid, meta))
        if len(models) >= MAX_CATALOG_MODELS:
            break
    if not models:
        return [], "上游没有返回模型清单"
    return models, ""


def refresh_pool(pool_id, timeout=LIST_TIMEOUT_SECONDS):
    """重拉一个已存池子的模型清单。"""
    pool = get(pool_id)
    if pool is None:
        raise PoolError(404, "供应商不存在")
    models, error = fetch_models(pool["base_url"], pool["api_key"],
                                 pool["protocol"], timeout)
    if error:
        raise PoolError(502, error)
    with _lock:
        for item in _state["pools"]:
            if item["id"] == pool["id"]:
                item["catalog"] = models
                item["catalog_at"] = time.time()
                # 拉回来的清单要能被挑：还没挑过就全给（操作者可以再删）。
                known = set(item["models"])
                for model in models:
                    if model["id"] not in known:
                        item["models"].append(model["id"])
                _save()
                return dict(item)
    raise PoolError(404, "供应商不存在")


def _pool_error_detail(status, raw, pool, model=""):
    """上游报错时那句话（复用 wb_zen 的口径：说清是谁拒的、该怎么改）。"""
    try:
        import wb_zen
        return wb_zen._error_detail(status, raw, pool, model)
    except Exception:
        return "HTTP %d" % status


def test_pool(pool_id, model=None, timeout=TEST_TIMEOUT_SECONDS):
    """拿池子的 Key 打一次最小请求，确认「这个池子真能推理」。

    拉模型清单（GET /models）证明不了 Key 能不能推理：OpenRouter 的模型
    目录是公开的，垃圾 Key 也能拉到 470 个模型 —— 面板上「刷新」一片绿，
    第一次对话却被拒（User not found）。所以这里真的发一次请求（1 个 token）。

    返回 (ok, status, message)。
    """
    pool = get(pool_id)
    if pool is None:
        raise PoolError(404, "供应商不存在")
    mid = str(model or "").strip()
    if not mid:
        models = pool.get("models") or []
        if not models:
            raise PoolError(400, "这个池子还没挑模型：先挑一个再测试")
        mid = models[0]
    chat = {"model": mid, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1}
    url, body, headers, _sse, _source, _billed = build_request(pool, chat)
    data = json.dumps(body, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    from wb_accounts import urlopen
    try:
        resp = urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read(4 << 10)
        except Exception:
            pass
        return False, exc.code, _pool_error_detail(exc.code, raw, pool, mid)
    except Exception as exc:
        return False, 502, "连不上上游：%s" % exc
    try:
        resp.read(1)   # 读一点，确认连接真的通了
    except Exception:
        pass
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return True, 200, "%s 回了一个正常响应" % mid


# ---------------------------------------------------------------------------
# 请求：OpenAI 兼容
# ---------------------------------------------------------------------------

_CHAT_FIELDS = (
    "messages", "temperature", "top_p", "top_k", "stop", "seed", "user",
    "presence_penalty", "frequency_penalty", "response_format",
    "parallel_tool_calls", "tool_choice", "tools", "max_tokens",
    "max_completion_tokens", "reasoning", "reasoning_effort", "stream",
)


def _openai_body(chat, model):
    body = {}
    for key in _CHAT_FIELDS:
        if key in chat and chat.get(key) is not None:
            body[key] = chat[key]
    if "max_tokens" not in body and body.get("max_completion_tokens"):
        body["max_tokens"] = body.pop("max_completion_tokens")
    body.pop("max_completion_tokens", None)
    body["model"] = model
    # 上游一律走流式：下游要非流式时由网关自己聚合（见 aggregate_stream），
    # 这样两条路只维护一套解析。
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    return body


# ---------------------------------------------------------------------------
# 请求：Anthropic 兼容
# ---------------------------------------------------------------------------

def _anthropic_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _anthropic_content(value):
    """OpenAI 的一段内容 → Anthropic 的 content 块数组。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if isinstance(value, list):
        blocks = []
        for item in value:
            if isinstance(item, str):
                if item:
                    blocks.append({"type": "text", "text": item})
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "text")
            if kind == "text":
                text = _anthropic_text(item.get("text"))
                if text:
                    blocks.append({"type": "text", "text": text})
            elif kind == "image_url":
                url = ""
                detail = item.get("image_url")
                if isinstance(detail, dict):
                    url = str(detail.get("url") or "")
                elif isinstance(detail, str):
                    url = detail
                if url.startswith("data:"):
                    head, _, data = url.partition(",")
                    media = head[5:] or "image/png"
                    blocks.append({"type": "image",
                                   "source": {"type": "base64", "media_type": media,
                                              "data": data}})
                elif url:
                    blocks.append({"type": "image",
                                   "source": {"type": "url", "url": url}})
        return blocks
    if isinstance(value, dict):
        return [{"type": "text", "text": _anthropic_text(value.get("text"))}]
    return []


def _anthropic_tool(tool):
    """OpenAI 的工具定义 → Anthropic 的 input_schema。"""
    fn = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(fn, dict):
        fn = tool if isinstance(tool, dict) else {}
    name = str(fn.get("name") or "").strip()
    if not name:
        return None
    params = fn.get("parameters")
    if not isinstance(params, dict):
        params = {"type": "object", "properties": {}}
    return {
        "name": name,
        "description": _anthropic_text(fn.get("description")),
        "input_schema": params,
    }


def _anthropic_body(chat, model):
    """OpenAI chat 请求 → Anthropic Messages 请求。"""
    system_parts = []
    messages = []
    for message in chat.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system" or role == "developer":
            for block in _anthropic_content(content):
                if block.get("type") == "text":
                    system_parts.append(block["text"])
            continue
        if role == "tool":
            # Anthropic 的工具结果走 user 消息里的 tool_result 块。
            call_id = str(message.get("tool_call_id") or "")
            messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": call_id,
                             "content": _anthropic_text(content)}],
            })
            continue
        if role == "assistant":
            blocks = _anthropic_content(content)
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    continue
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) and raw else (
                        raw if isinstance(raw, dict) else {})
                except Exception:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(call.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": args,
                })
            if blocks:
                messages.append({"role": "assistant", "content": blocks})
            continue
        blocks = _anthropic_content(content)
        if blocks:
            messages.append({"role": "user", "content": blocks})
    tools = []
    for tool in chat.get("tools") or []:
        converted = _anthropic_tool(tool)
        if converted is not None:
            tools.append(converted)
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
    }
    if system_parts:
        body["system"] = "\n\n".join(p for p in system_parts if p)
    if tools:
        body["tools"] = tools
    limit = chat.get("max_tokens")
    if not limit:
        limit = chat.get("max_completion_tokens")
    body["max_tokens"] = int(limit or 4096)
    temperature = chat.get("temperature")
    if temperature is not None:
        body["temperature"] = temperature
    top_p = chat.get("top_p")
    if top_p is not None:
        body["top_p"] = top_p
    if chat.get("stop"):
        stop = chat.get("stop")
        body["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    return body


# ---------------------------------------------------------------------------
# Anthropic SSE → OpenAI SSE
# ---------------------------------------------------------------------------

def _sse(data):
    return ("data: %s\n\n" % json.dumps(data, ensure_ascii=False,
                                        separators=(",", ":"))).encode("utf-8")


class AnthropicStream(object):
    """把 Anthropic 的事件流翻译成 OpenAI 的 chunk 流。

    下游（网关的响应管线、客户端）只认 OpenAI 形状，所以翻译放在最上游这一层：
    翻译完就是一条普通的 OpenAI SSE，后面的代码一行都不用改。
    """

    def __init__(self, model):
        self.model = model
        self.created = int(time.time())
        self.call_id = "call_%s" % os.urandom(8).hex()
        self.tool_index = 0
        self.started = False
        self.finished = False
        self.usage = None
        self.reasoning = []

    def _chunk(self, delta, finish=None):
        return {
            "id": "chatcmpl-%s" % self.call_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _start(self):
        if not self.started:
            self.started = True
            return _sse(self._chunk({"role": "assistant", "content": ""}))
        return b""

    def feed(self, line):
        """吃一行 Anthropic SSE，返回若干行 OpenAI SSE。"""
        text = line.decode("utf-8", "replace").strip()
        if not text or text.startswith(":"):
            return b""
        if text.startswith("event:"):
            return b""
        if not text.startswith("data:"):
            return b""
        payload = text[5:].strip()
        if not payload or payload == "[DONE]":
            return b""
        try:
            obj = json.loads(payload)
        except Exception:
            return b""
        if not isinstance(obj, dict):
            return b""
        kind = str(obj.get("type") or "")
        if kind == "message_start":
            message = obj.get("message") or {}
            usage = message.get("usage")
            if isinstance(usage, dict):
                self.usage = usage
            return self._start()
        if kind == "content_block_start":
            block = obj.get("content_block") or {}
            if str(block.get("type") or "") == "tool_use":
                delta = {"tool_calls": [{
                    "index": self.tool_index,
                    "id": str(block.get("id") or "toolu_%s" % self.call_id),
                    "type": "function",
                    "function": {"name": str(block.get("name") or ""),
                                 "arguments": ""},
                }]}
                self.tool_index += 1
                return self._start() + _sse(self._chunk(delta))
            return self._start()
        if kind == "content_block_delta":
            delta = obj.get("delta") or {}
            dtype = str(delta.get("type") or "")
            if dtype == "text_delta":
                return _sse(self._chunk({"content": delta.get("text") or ""}))
            if dtype == "thinking_delta":
                text_value = delta.get("thinking") or ""
                self.reasoning.append(text_value)
                return _sse(self._chunk({"reasoning_content": text_value}))
            if dtype == "input_json_delta":
                return _sse(self._chunk({"tool_calls": [{
                    "index": max(0, self.tool_index - 1),
                    "function": {"arguments": delta.get("partial_json") or ""},
                }]}))
            return b""
        if kind == "message_delta":
            usage = obj.get("usage")
            if isinstance(usage, dict):
                merged = dict(self.usage or {})
                merged.update(usage)
                self.usage = merged
            delta = obj.get("delta") or {}
            stop = delta.get("stop_reason")
            if stop:
                return self._finish(stop)
            return b""
        if kind == "message_stop":
            return self._finish("stop")
        if kind == "error":
            message = ""
            err = obj.get("error")
            if isinstance(err, dict):
                message = str(err.get("message") or "")
            elif err:
                message = str(err)
            return _sse({"error": {"message": message or "上游报错",
                                   "type": "server_error"}})
        return b""

    def _finish(self, stop_reason):
        if self.finished:
            return b""
        self.finished = True
        finish = "tool_calls" if stop_reason == "tool_use" else (
            "length" if stop_reason == "max_tokens" else "stop")
        out = _sse(self._chunk({}, finish))
        usage = self.usage or {}
        if usage:
            prompt = _int(usage.get("input_tokens"))
            completion = _int(usage.get("output_tokens"))
            out += _sse({
                "id": "chatcmpl-%s" % self.call_id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [],
                "usage": {"prompt_tokens": prompt,
                          "completion_tokens": completion,
                          "total_tokens": prompt + completion},
            })
        out += b"data: [DONE]\n\n"
        return out

    def close(self):
        """上游没发 message_stop 就断了：补一个收尾，别让客户端干等。"""
        if self.finished:
            return b""
        return self._finish("stop")


# ---------------------------------------------------------------------------
# 构造请求
# ---------------------------------------------------------------------------

def build_request(pool, chat):
    """构造一次池子请求。

    返回 (url, body, headers, 转换器, source, 计费模型 id)。转换器是可选的
    行级过滤器：Anthropic 协议要把它的事件流翻成 OpenAI 形状，OpenAI 协议
    没有（None 表示原样透传）。
    """
    base = _clean_base_url(pool.get("base_url"))
    model = str(chat.get("model") or "").strip()
    protocol = _clean_protocol(pool.get("protocol"))
    key = str(pool.get("api_key") or "").strip()
    if protocol == PROTOCOL_ANTHROPIC:
        url = base + "/messages"
        body = _anthropic_body(chat, model)
        headers = {"content-type": "application/json",
                   "accept": "text/event-stream",
                   "anthropic-version": ANTHROPIC_VERSION,
                   "user-agent": "wb-proxy/egg-pools"}
        if key:
            headers["x-api-key"] = key
        return url, body, headers, AnthropicStream(model), pool["id"], model
    url = base + "/chat/completions"
    body = _openai_body(chat, model)
    headers = {"content-type": "application/json",
               "accept": "text/event-stream",
               "user-agent": "wb-proxy/egg-pools"}
    if key:
        headers["authorization"] = "Bearer " + key
    return url, body, headers, None, pool["id"], model
