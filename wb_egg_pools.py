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

#: 池子的两种来源：
#: * ``api``      —— 操作者自己填 Base URL + API Key（本文件原来的形态）；
#: * ``platform`` —— 平台账号池：浏览器 / 设备码登录，账号与凭据由
#:                   ``wb_platforms`` 管，请求也由它构造。
KIND_API = "api"
KIND_PLATFORM = "platform"
KINDS = (KIND_API, KIND_PLATFORM)

#: 一个平台池最多挂多少个账号。
MAX_PLATFORM_ACCOUNTS = 20

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
    {"name": "商汤 SenseNova", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://token.sensenova.cn/v1",
     "host": "token.sensenova.cn"},
    {"name": "Agnes AI", "protocol": PROTOCOL_OPENAI,
     "base_url": "https://api.agnes-ai.cn/v1",
     "host": "api.agnes-ai.cn"},
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

#: 免费模型被限流时的提示（免费模型都有每日额度）。实测（2026-10-20，
#: 同一把 Key 同一天）：15 个免费模型里有 14 个报「今日额度用完」，另一个照常
#: 能用 —— 每日 50 次是**每个模型各算各的**，不是整个账号共用一份。
#: 额度在 UTC 0 点重置（东八区是早上 8 点）。
_FREE_QUOTA_HINTS = {
    "openrouter.ai": (
        "OpenRouter 免费档每个免费模型每天各 50 次（UTC 0 点重置），"
        "这个模型的今日额度已用完；等隔天恢复，或在池子里取消勾选它、"
        "改走内置的免费源（往账号充 $10 可把额度提到 1000 次/天）"),
}

#: 403 还有两种跟 Key 无关的含义，得看原文才分得开：
#: 一是模型在出口所在地区不开放，二是模型只开放给 agentic harness。
_REGION_MARKERS = ("your region",)
_HARNESS_MARKERS = ("agentic harness",)


def error_hint(base_url, status, model="", raw=""):
    """这个上游报错时该怎么排查；没有提示就返回空串。

    ``raw`` 是上游回的原文 —— 403 有好几种含义（Key 类型不对 / 地区不开放 /
    只给 agentic harness 用），光看状态码分不出来。
    """
    host = str(base_url or "").strip().lower()
    text = str(model or "").strip().lower()
    body = str(raw or "").strip().lower()
    if status in (401, 403):
        if any(mark in body for mark in _REGION_MARKERS):
            return ("这个模型在出口所在的地区不开放"
                    "（上游按出口 IP 判地区）：换一个模型，"
                    "或把出口换到开放地区")
        if any(mark in body for mark in _HARNESS_MARKERS):
            return ("这个模型只开放给 agentic harness（编程助手一类）用，"
                    "普通聊天接口调不动；换一个普通聊天模型")
        for suffix, hint in _AUTH_HINTS.items():
            if suffix in host:
                return hint
        return ""
    if status == 429 and (text.endswith(":free") or text.endswith("/free")
                          or "free-models-per-day" in body):
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
        try:
            entry = _normalize(row)
        except Exception:
            # 一条坏记录不该把整个列表拖下水：跳过它，剩下的照常加载。
            continue
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


def _clean_kind(value, platform=""):
    text = str(value or "").strip().lower()
    if text == KIND_PLATFORM or (not text and platform):
        return KIND_PLATFORM
    return KIND_API


def _platform_defaults(platform):
    try:
        import wb_platforms
        return wb_platforms.defaults(platform)
    except Exception:
        return {}


def _clean_accounts(value):
    """平台账号列表：只留认得出的账号，凭据字段原样保留。"""
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("id") or "").strip()
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        row = dict(item)
        row["id"] = account_id
        row["enabled"] = item.get("enabled", True) is not False
        row["label"] = str(item.get("label") or "").strip()
        row["status"] = str(item.get("status") or "ok").strip() or "ok"
        row["last_error"] = str(item.get("last_error") or "").strip()
        for key in ("created_at", "last_used", "expires_at"):
            try:
                row[key] = float(item.get(key) or 0.0)
            except (TypeError, ValueError):
                row[key] = 0.0
        out.append(row)
        if len(out) >= MAX_PLATFORM_ACCOUNTS:
            break
    return out


def _clean_meta(value):
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, item in value.items():
        if isinstance(key, str) and len(out) < 40:
            out[key] = item
    return out


def _normalize(entry):
    """把一条落盘的记录收拾成干净形态；没法用的返回 None。"""
    if not isinstance(entry, dict):
        return None
    platform = str(entry.get("platform") or "").strip().lower()
    kind = _clean_kind(entry.get("kind"), platform)
    name = str(entry.get("name") or "").strip()
    base_url = _clean_base_url(entry.get("base_url"))
    if kind == KIND_PLATFORM:
        if not platform:
            return None
        try:
            info = _platform_defaults(platform)
        except Exception:
            # 这个平台已经不支持了（比如功能撤掉），落盘里的旧池子直接丢掉：
            # 留着的话加载整个列表都会失败，其它池子也跟着看不见。
            return None
        if not name:
            name = str(info.get("name") or platform)
        if not base_url:
            base_url = _clean_base_url(info.get("base_url"))
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
        "kind": kind,
        "platform": platform if kind == KIND_PLATFORM else "",
        "name": name,
        "protocol": _clean_protocol(entry.get("protocol")),
        "base_url": base_url,
        "api_key": str(entry.get("api_key") or "").strip(),
        "enabled": entry.get("enabled", True) is not False,
        "models": models,
        "catalog": catalog,
        "catalog_at": catalog_at,
        "accounts": _clean_accounts(entry.get("accounts")),
        "meta": _clean_meta(entry.get("meta")),
    }


def _is_free_id(model_id):
    """这个模型 id 是不是「免费」标记。

    上游把免费标在 id 结尾：OpenRouter 用 ``:free``（minimax/…:free），
    也有用 ``/free`` 的（kilo-auto/free、openrouter/free）。
    """
    text = str(model_id or "").strip().lower()
    return text.endswith(":free") or text.endswith("/free") or text == "free"


def _is_free_namespace(model_id):
    """免费车道常用**命名空间前缀**：``cline-free/xxx``、``vendor-free/xxx``。

    这类 id 结尾不是 ``:free``，光看后缀认不出来 —— 平台适配器一般会自己
    标 free，但已落盘的旧目录要在这里补认。
    """
    head = str(model_id or "").strip().lower().split("/", 1)[0]
    return head.endswith("-free") or head.endswith("_free")


def _clean_model(model_id, row=None):
    """把上游的模型行收拾成清单条目（字段缺失就留空，不猜）。"""
    row = row if isinstance(row, dict) else {}
    name = str(row.get("name") or row.get("display_name") or "").strip()
    if not name:
        name = model_id
    context = _int(row.get("context_length") or row.get("context_window")
                   or row.get("context") or 0)
    output = _int(row.get("max_output_tokens") or row.get("max_completion_tokens")
                  or row.get("output") or 0)
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
    free = _is_free_id(model_id) or _is_free_namespace(model_id) or row.get("free") is True
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
    row_out = {
        "id": model_id,
        "name": name,
        "context": context,
        "output": output,
        "vision": vision,
        "reasoning": reasoning,
        "tools": True,
        "free": free,
    }
    # 平台池的模型要记住「走哪条通道」（CodeArts 的 benefit / credit），
    # 构造请求时按它决定是否带 maas_type 头。
    channel = str(row.get("channel") or "").strip()
    if channel:
        row_out["channel"] = channel
    platform = str(row.get("platform") or "").strip()
    if platform:
        row_out["platform"] = platform
    # 平台池的模型对外 id 带平台前缀，upstream_id 是发给上游的原始 id。
    upstream_id = str(row.get("upstream_id") or "").strip()
    if upstream_id:
        row_out["upstream_id"] = upstream_id
    return row_out


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def pools():
    """全部池子（含明文 Key / 账号凭据与上游目录，给服务端自己用）。"""
    with _lock:
        return [_full_copy(p) for p in _state["pools"]]


def _full_copy(pool):
    out = dict(pool)
    out["catalog"] = [dict(m) for m in pool.get("catalog") or []]
    out["models"] = list(pool.get("models") or [])
    out["accounts"] = [dict(a) for a in pool.get("accounts") or []]
    out["meta"] = dict(pool.get("meta") or {})
    return out


def _record(pool):
    """池子的浅副本：不含上游目录。

    请求热路径上要读池子（Base URL / Key / 协议），而上游目录可能有上千行，
    每个请求都拷一遍纯属白烧 CPU —— 目录只有面板的「挑选模型」与刷新要用。
    平台池要把账号带上（请求要挑账号、续期），账号数量很少，拷贝不心疼。
    """
    return {
        "id": pool["id"],
        "kind": pool.get("kind") or KIND_API,
        "platform": pool.get("platform") or "",
        "name": pool["name"],
        "protocol": pool["protocol"],
        "base_url": pool["base_url"],
        "api_key": pool["api_key"],
        "enabled": pool["enabled"],
        "models": list(pool["models"]),
        "catalog": [],
        "catalog_at": pool["catalog_at"],
        "accounts": [dict(a) for a in pool.get("accounts") or []],
        "meta": dict(pool.get("meta") or {}),
    }


def get(pool_id):
    pid = str(pool_id or "").strip()
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] == pid:
                return _full_copy(pool)
    return None


def find_by_model(model_id, pool_label=None):
    """这个模型属于哪个池子（没有就 None）。

    只认**显式挑过**的模型：池子的 catalog 是「上游有什么」，models 才是
    「这个池子对外提供什么」。没挑过的模型不接流量，也不进清单。

    ``pool_label`` 是公开 id 里「·」后面那段（客户端明确挑了哪个池子）。
    同一个模型 id 可能被多个池子挑走（例如两家都提供 ``gpt-4o-mini``），
    只按 id 查只能命中先登记的那个；带了池名就以池名为准，否则选「· Dup B」
    的请求会打到 Dup A 去。
    """
    mid = str(model_id or "").strip()
    if not mid:
        return None
    label = str(pool_label or "").strip()
    if label:
        with _lock:
            for pool in _state["pools"]:
                if not pool["enabled"]:
                    continue
                if pool["name"] == label and mid in pool["models"]:
                    return _record(pool)
    pool_id = _index["map"].get(mid)
    if not pool_id:
        return None
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] == pool_id:
                return _record(pool)
    return None


def platform_model_info(pool_id, model_id):
    """平台池里某个对外模型 id 的信息：{upstream_id, channel}。

    请求热路径拿到的池子是「浅副本」（不带上游目录），但平台请求要知道
    走哪条通道（CodeArts 的 benefit / credit）、发给上游的原始 id 是
    什么 —— 这里按 id 查一次就行，不用把整份目录拷一遍。
    """
    pid = str(pool_id or "").strip()
    mid = str(model_id or "").strip()
    if not pid or not mid:
        return None
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] != pid:
                continue
            for row in pool.get("catalog") or []:
                if isinstance(row, dict) and row.get("id") == mid:
                    return {"upstream_id": row.get("upstream_id") or mid,
                            "channel": row.get("channel") or ""}
    return None


def enabled_pools():
    with _lock:
        return [dict(p) for p in _state["pools"] if p["enabled"]]


def _builtin_free_ids():
    """内置免费源（Zen / Kilo）里的模型 id。

    池子选中的 id 若与内置免费源撞了，请求就改走本池（一个 id 只能有一个
    去处，见 wb_zen.merge_models）—— 内置那条等于被顶掉了。面板要能提醒
    操作者「选它就顶掉了 Kilo 的那条」，免得把还能用的免费模型弄没了。
    """
    try:
        import wb_zen
    except Exception:
        return set()
    try:
        snap = wb_zen.snapshot()
    except Exception:
        return set()
    return set(m.get("id") for m in (snap.get("models") or [])
               if isinstance(m, dict) and m.get("source") in ("zen", "kilo"))


def shadowed_ids(pool):
    """这个池子顶掉了内置免费源的哪些模型 id。"""
    builtin = _builtin_free_ids()
    if not builtin:
        return []
    return sorted(mid for mid in (pool.get("models") or []) if mid in builtin)


def pool_summaries():
    """池子的概要（不含 catalog 副本）：概览卡片与状态接口用。

    pools() 会把每条池子的上游目录整个拷一遍；概览只要名字、地址与条数，
    不值得为它拷那些模型行。
    """
    with _lock:
        return [{
            "id": p["id"],
            "kind": p.get("kind") or KIND_API,
            "platform": p.get("platform") or "",
            "name": p["name"],
            "protocol": p["protocol"],
            "base_url": p["base_url"],
            "enabled": p["enabled"],
            "count": len(p["models"]),
            "catalog_count": len(p["catalog"]),
            "catalog_at": p["catalog_at"],
            "account_count": len(p.get("accounts") or []),
            "shadowed": shadowed_ids(p),
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
    """给面板的视图：Key / 账号凭据打码，catalog 原样带上。"""
    out = []
    for pool in pools():
        pool["api_key_masked"] = mask_key(pool.get("api_key"))
        pool["protocol_label"] = PROTOCOL_LABELS.get(pool["protocol"], pool["protocol"])
        pool["model_count"] = len(pool.get("models") or [])
        pool["catalog_count"] = len(pool.get("catalog") or [])
        # 顶掉了内置免费源的模型 id（面板要提醒：选它就改走本池）
        pool["shadowed"] = shadowed_ids(pool)
        if pool.get("kind") == KIND_PLATFORM:
            accounts = [a for a in pool.get("accounts") or []
                        if isinstance(a, dict)]
            pool["accounts"] = [_public_account(a) for a in accounts]
            pool["account_count"] = len(accounts)
            pool["enabled_account_count"] = sum(
                1 for a in accounts if a.get("enabled", True) is not False)
            info = {}
            try:
                import wb_platforms
                info = wb_platforms.platform_def(pool.get("platform")) or {}
            except Exception:
                info = {}
            pool["platform_label"] = info.get("name") or pool.get("platform")
            pool["platform_short"] = info.get("short") or pool.get("platform")
            pool["platform_home"] = info.get("home") or ""
            pool["login_mode"] = info.get("login") or ""
            pool["login_label"] = info.get("login_label") or "登录"
            pool["can_balance"] = bool(info.get("balance"))
            pool["can_claim"] = bool(info.get("claim"))
            meta = pool.get("meta") or {}
            pool["balance"] = meta.get("balance") or {}
            pool["claim"] = meta.get("claim") or {}
        out.append(pool)
    return out


def _public_account(account):
    """账号的面板视图（不带任何凭据）。"""
    try:
        import wb_platforms
        return wb_platforms.account_public(account)
    except Exception:
        return {
            "id": account.get("id") or "",
            "label": account.get("label") or "",
            "email": account.get("email") or "",
            "enabled": account.get("enabled", True) is not False,
            "status": account.get("status") or "ok",
            "last_error": account.get("last_error") or "",
            "expires_at": account.get("expires_at") or 0,
        }


def catalog_entries(dedupe=True):
    """池子里对外提供的模型（清单条目形态，和 wb_zen 的目录一致）。

    同一个模型 id 被两个池子挑走时，默认先登记的池子赢 —— 鸡蛋视图按纯 id
    挑模型，一个 id 只能出现一次，否则客户端会随机命中。``dedupe=False`` 时
    每个池子各留一条：路由页的公开 id 带「· 池名」，本来就分得开。
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
            if dedupe and mid in seen:
                continue
            seen.add(mid)
            meta = by_id.get(mid) or {}
            platform = pool.get("kind") == KIND_PLATFORM
            if platform:
                blurb = ("平台账号池「%s」的模型：登录态由网关保管，"
                         "额度按该平台账号结算" % pool["name"])
            else:
                blurb = ("自建供应商「%s」的模型（%s）：%s"
                         % (pool["name"],
                            PROTOCOL_LABELS.get(pool["protocol"],
                                                pool["protocol"]),
                            pool["base_url"]))
            entries.append({
                "id": mid,
                "name": meta.get("name") or mid,
                "source": pool["id"],
                "sourceKind": "platform" if platform else "pool",
                "sourceLabel": pool["name"],
                "platform": pool.get("platform") or "",
                "context": meta.get("context") or 0,
                "output": meta.get("output") or 0,
                "vision": bool(meta.get("vision")),
                "reasoning": bool(meta.get("reasoning")),
                "tools": True,
                # 免费与否：目录里存的是上次拉的结果（可能没有 free 字段），
                # 兜底看 id 结尾的 :free / /free 与免费命名空间前缀。
                "free": bool(meta.get("free")) or _is_free_id(mid)
                or _is_free_namespace(mid),
                "blurb": blurb,
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
    platform = str(payload.get("platform") or "").strip().lower()
    kind = _clean_kind(payload.get("kind"), platform)
    with _lock:
        existing = None
        for pool in _state["pools"]:
            if pool["id"] == pool_id:
                existing = pool
                break
        if existing is not None:
            platform = platform or str(existing.get("platform") or "")
            if "kind" not in payload and "platform" not in payload:
                kind = existing.get("kind") or KIND_API
            name = name or existing["name"]
            base_url = base_url or existing["base_url"]
        if kind == KIND_PLATFORM:
            info = _platform_defaults(platform)
            if not info:
                raise PoolError(400, "不支持的平台：%s" % (platform or "（空）"))
            name = name or str(info.get("name") or platform)
            base_url = base_url or _clean_base_url(info.get("base_url"))
        if not name:
            raise PoolError(400, "供应商名称不能为空")
        if not base_url:
            raise PoolError(400, "Base URL 不能为空")
        if not base_url.lower().startswith(("http://", "https://")):
            raise PoolError(400, "Base URL 必须以 http:// 或 https:// 开头")
        if existing is None:
            if len(_state["pools"]) >= MAX_POOLS:
                raise PoolError(400, "供应商数量已达上限（%d 个）" % MAX_POOLS)
            pool_id = _mint_id()
        protocol = _clean_protocol(payload.get("protocol")
                                   or (existing or {}).get("protocol"))
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
        accounts = existing.get("accounts") if existing else []
        if isinstance(payload.get("accounts"), list):
            accounts = _clean_accounts(payload["accounts"])
        meta = existing.get("meta") if existing else {}
        if isinstance(payload.get("meta"), dict):
            meta = _clean_meta(payload["meta"])
        entry = _normalize({
            "id": pool_id, "kind": kind, "platform": platform,
            "name": name, "protocol": protocol,
            "base_url": base_url, "api_key": api_key, "enabled": enabled,
            "models": models, "catalog": catalog, "catalog_at": catalog_at,
            "accounts": accounts, "meta": meta,
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
                return _full_copy(pool)
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
                return _full_copy(pool)
    raise PoolError(404, "供应商不存在")


# ---------------------------------------------------------------------------
# 平台账号池
# ---------------------------------------------------------------------------

def platform_pool(platform):
    """按平台找池子（同一平台只维护一个池子）。"""
    pid = str(platform or "").strip().lower()
    if not pid:
        return None
    with _lock:
        for pool in _state["pools"]:
            if (pool.get("kind") == KIND_PLATFORM
                    and pool.get("platform") == pid):
                return _full_copy(pool)
    return None


def ensure_platform_pool(platform):
    """取这个平台的池子；没有就按默认值建一个。"""
    pool = platform_pool(platform)
    if pool is not None:
        return pool
    try:
        import wb_platforms
        info = wb_platforms.defaults(platform)
    except Exception as exc:
        raise PoolError(400, "不支持的平台：%s（%s）" % (platform, exc))
    entry = upsert({
        "kind": KIND_PLATFORM,
        "platform": platform,
        "name": info["name"],
        "protocol": info["protocol"],
        "base_url": info["base_url"],
        "enabled": True,
    })
    _log("egg pools: 新建平台池 %s" % entry["name"])
    return entry


def add_platform_account(pool_id, account):
    """把一个登录成功的账号放进池子（同 id 覆盖，不重复添加）。"""
    if not isinstance(account, dict) or not str(account.get("id") or "").strip():
        raise PoolError(400, "账号数据不完整")
    pid = str(pool_id or "").strip()
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] != pid:
                continue
            accounts = list(pool.get("accounts") or [])
            row = dict(account)
            for index, item in enumerate(accounts):
                if item.get("id") == row.get("id"):
                    accounts[index] = row
                    break
            else:
                if len(accounts) >= MAX_PLATFORM_ACCOUNTS:
                    raise PoolError(400, "账号数量已达上限（%d 个）"
                                    % MAX_PLATFORM_ACCOUNTS)
                accounts.append(row)
            pool["accounts"] = _clean_accounts(accounts)
            _save()
            return _full_copy(pool)
    raise PoolError(404, "平台池不存在")


def update_platform_account(pool_id, account_id, account):
    """按 id 更新一个账号（续期 / 状态变化时用）。"""
    pid = str(pool_id or "").strip()
    aid = str(account_id or "").strip()
    if not pid or not aid:
        return None
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] != pid:
                continue
            accounts = list(pool.get("accounts") or [])
            for index, item in enumerate(accounts):
                if item.get("id") == aid:
                    accounts[index] = dict(account or {}, id=aid)
                    pool["accounts"] = _clean_accounts(accounts)
                    _save()
                    return _full_copy(pool)
    return None


def remove_platform_account(pool_id, account_id):
    pid = str(pool_id or "").strip()
    aid = str(account_id or "").strip()
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] != pid:
                continue
            before = len(pool.get("accounts") or [])
            pool["accounts"] = [a for a in pool.get("accounts") or []
                                if a.get("id") != aid]
            if len(pool["accounts"]) == before:
                return False
            _save()
            return True
    return False


def set_platform_account_enabled(pool_id, account_id, enabled):
    pid = str(pool_id or "").strip()
    aid = str(account_id or "").strip()
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] != pid:
                continue
            for item in pool.get("accounts") or []:
                if item.get("id") == aid:
                    item["enabled"] = bool(enabled)
                    _save()
                    return _full_copy(pool)
    raise PoolError(404, "账号不存在")


def set_platform_meta(pool_id, key, value):
    """写一条平台快照（余额 / 领取记录）。"""
    pid = str(pool_id or "").strip()
    key = str(key or "").strip()
    if not pid or not key:
        return None
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] == pid:
                meta = dict(pool.get("meta") or {})
                meta[key] = value
                pool["meta"] = _clean_meta(meta)
                _save()
                return _full_copy(pool)
    return None


def platform_model_id(platform, upstream_id):
    """平台池的对外模型 id：带平台前缀。

    平台给的模型 id 很通用（CodeArts 的 ``GLM-5.2``、Cline 的
    ``anthropic/claude-sonnet-5-5``），和别的池子/别的提供商会撞名 ——
    撞了就有一个被清单丢掉，请求还可能被发去别家。对外统一加前缀
    （``codearts/GLM-5.2``），向上游发的时候再把前缀摘掉。
    """
    pid = str(platform or "").strip().lower()
    raw = str(upstream_id or "").strip()
    if not pid or not raw:
        return raw
    if raw.lower().startswith(pid + "/"):
        return raw
    return pid + "/" + raw


def apply_platform_catalog(pool_id, models, select_all=True):
    """平台拉回清单后写入池子；新模型默认也选中（操作者再取消）。"""
    pid = str(pool_id or "").strip()
    platform = ""
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] == pid:
                platform = str(pool.get("platform") or "")
                break
    cleaned = []
    for item in models or []:
        if not isinstance(item, dict):
            continue
        upstream = str(item.get("id") or "").strip()
        if not upstream:
            continue
        public = platform_model_id(platform, upstream)
        row = _clean_model(public, item)
        # 记住上游原始 id：构造请求时用它，别把前缀发给上游。
        row["upstream_id"] = upstream
        cleaned.append(row)
        if len(cleaned) >= MAX_CATALOG_MODELS:
            break
    with _lock:
        for pool in _state["pools"]:
            if pool["id"] != pid:
                continue
            pool["catalog"] = cleaned
            pool["catalog_at"] = time.time()
            # 已选清单跟着新目录走：老 id（没前缀的）映射到新 id，
            # 没选过的按 select_all 决定要不要加上。
            previous = set(pool.get("models") or [])
            selected = []
            for model in cleaned:
                public = model["id"]
                raw = model.get("upstream_id") or public
                if public in previous or raw in previous:
                    selected.append(public)
                elif select_all:
                    selected.append(public)
                if len(selected) >= MAX_SELECTED_MODELS:
                    break
            pool["models"] = selected
            _save()
            return _full_copy(pool)
    raise PoolError(404, "供应商不存在")


# ---------------------------------------------------------------------------
# 拉模型清单
# ---------------------------------------------------------------------------

def _proxy_url():
    """供应商池的出口代理地址（空串 = 直连）。

    自建池用的多半是境外上游（OpenRouter 之类），直连会被按地区挡掉；出口
    统一走设置里指定的代理槽（见 wb_settings.egg_proxy_url）。每次现取，
    改完设置立刻生效。
    """
    if not _data_dir:
        return ""
    try:
        import wb_settings
        return wb_settings.egg_proxy_url(_data_dir)
    except Exception:
        return ""


def _http_json(url, headers, timeout):
    from wb_accounts import urlopen_with_fallback
    req = urllib.request.Request(url, headers=headers)
    with urlopen_with_fallback(req, timeout=timeout, proxy=_proxy_url(),
                               log=_log) as resp:
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


def refresh_pool(pool_id, timeout=LIST_TIMEOUT_SECONDS, adopt_new=True):
    """重拉一个已存池子的模型清单。"""
    pool = get(pool_id)
    if pool is None:
        raise PoolError(404, "供应商不存在")
    if pool.get("kind") == KIND_PLATFORM:
        try:
            import wb_platforms
        except Exception as exc:
            raise PoolError(500, "平台适配模块不可用：%s" % exc)
        models, error = wb_platforms.fetch_models(pool)
        if error:
            raise PoolError(502, error)
        apply_platform_catalog(pool["id"], models, select_all=True)
        updated = get(pool["id"])
        # 顺手刷一次余额：面板的「刷新」按钮就是想要最新状态。
        try:
            balance = wb_platforms.fetch_balance(updated)
            if balance:
                set_platform_meta(pool["id"], "balance", balance)
        except Exception as exc:
            _log("egg pools: 刷新平台余额失败：%s" % exc)
        return get(pool["id"]) or updated
    # 自建供应商（OpenAI / Anthropic 兼容）：真打一次上游的 /models。
    models, error = fetch_models(pool["base_url"], pool["api_key"],
                                 pool["protocol"], timeout)
    if error:
        raise PoolError(502, error)
    with _lock:
        for item in _state["pools"]:
            if item["id"] == pool["id"]:
                # 上游下线（免费期结束 / 模型退役）的 id 要从「对外提供」里摘掉：
                # 只摘「上一次目录里有过」的那些，手填的自定义 id 不动。
                previous = set(m.get("id") for m in item.get("catalog") or []
                               if isinstance(m, dict))
                fresh = set(m["id"] for m in models)
                expired = [mid for mid in item["models"]
                           if mid in previous and mid not in fresh]
                item["catalog"] = models
                item["catalog_at"] = time.time()
                # 拉回来的清单要能被挑：还没挑过就全给（操作者可以再删）。
                # adopt_new=False（「刷新免费模型」的连带刷新）只更新目录，
                # 不把上游新模型全塞进对外清单 —— 那会一下子放出几百个。
                if adopt_new:
                    known = set(item["models"])
                    for model in models:
                        if model["id"] not in known:
                            item["models"].append(model["id"])
                if expired:
                    gone = set(expired)
                    item["models"] = [mid for mid in item["models"]
                                      if mid not in gone]
                _save()
                return dict(item)
    raise PoolError(404, "供应商不存在")


def refresh_all_pools(timeout=LIST_TIMEOUT_SECONDS, join_timeout=90):
    """重拉所有启用池子的上游清单（面板「刷新免费模型」用）。

    每个池子一条线程：自建上游大多在公网，串行拉会让按钮等上一分钟。返回
    ``{"ok", "failed", "removed", "pools"}``，``removed`` 是这轮被上游下线、
    从「对外提供」里摘掉的模型数。

    只碰自建供应商池（kind=api）：平台账号池的刷新会按 select_all 把新模型
    全部选上，那是它自己「刷新模型」按钮的语义，不该被「刷新免费模型」顺手
    触发。同样地，这里用 adopt_new=False：只更新上游目录、摘掉已下线的 id，
    不把上游新模型全塞进「对外提供」。
    """
    targets = [p for p in pools()
               if p.get("enabled") and p.get("kind") != KIND_PLATFORM]
    if not targets:
        return {"ok": 0, "failed": 0, "removed": 0, "pools": []}
    results = {}
    lock = threading.Lock()

    def work(pool):
        before = set(pool.get("models") or [])
        try:
            updated = refresh_pool(pool["id"], timeout=timeout, adopt_new=False)
            after = set(updated.get("models") or [])
            row = {"id": pool["id"], "name": pool["name"], "ok": True,
                   "count": len(after),
                   "catalog": len(updated.get("catalog") or []),
                   "removed": len(before - after), "error": ""}
        except Exception as exc:
            row = {"id": pool["id"], "name": pool["name"], "ok": False,
                   "count": len(before), "catalog": 0, "removed": 0,
                   "error": str(exc)[:200]}
        with lock:
            results[pool["id"]] = row

    threads = []
    for pool in targets:
        thread = threading.Thread(target=work, args=(pool,),
                                  name="egg-pool-refresh-%s" % pool["id"])
        thread.daemon = True
        thread.start()
        threads.append(thread)
    deadline = time.time() + join_timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.time()))
    rows = []
    for pool in targets:
        row = results.get(pool["id"])
        if row is None:
            row = {"id": pool["id"], "name": pool["name"], "ok": False,
                   "count": len(pool.get("models") or []), "catalog": 0,
                   "removed": 0, "error": "刷新超时"}
        rows.append(row)
    return {
        "ok": sum(1 for row in rows if row["ok"]),
        "failed": sum(1 for row in rows if not row["ok"]),
        "removed": sum(row.get("removed") or 0 for row in rows),
        "pools": rows,
    }


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
    from wb_accounts import urlopen_with_fallback
    try:
        resp = urlopen_with_fallback(req, timeout=timeout, proxy=_proxy_url(),
                                     log=_log)
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
    if pool.get("kind") == KIND_PLATFORM:
        # 平台账号池的请求要带登录态 / 签名，交给 wb_platforms 构造。
        try:
            import wb_platforms
        except Exception as exc:
            raise PoolError(500, "平台适配模块不可用：%s" % exc)
        try:
            return wb_platforms.build_request(pool, chat)
        except Exception as exc:
            if isinstance(exc, PoolError):
                raise
            raise PoolError(getattr(exc, "status", 502),
                            getattr(exc, "detail", str(exc)))
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
