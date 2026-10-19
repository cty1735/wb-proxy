# -*- coding: utf-8 -*-
"""聚合渠道：把「有 Key / 免 Key 就能用的 OpenAI 兼容上游」接进网关。

为什么要有一个**独立于账号**的渠道概念：渠道没有账号。它有的是 base_url + Key，
没有 uid、没有账号池、没有额度护栏、没有指纹缓存。硬塞进 POOL 会把「渠道 ≠ 账号」
这条边界糊掉——选号、告警、额度、指纹会开始谎报，而谎报的对象是使用者的账单。

因此这里的约定是三层：

* ``data/providers.json``（随发布走的**数据**）描述「有哪些渠道」；
* ``accounts/settings.json`` 的 ``providers`` 段记住「这个安装怎么用它们」
  （Key / base_url 覆盖 / enabled / 已发现的模型）；
* 渠道**不进 POOL**。用量与错误行按 ``provider="channel"``、``realm="channel"``、
  ``account="channel:<id>"`` 记；wb_proxy 侧有一个账号替身（:func:`account_stub`）
  认领这三行的归属——不认领它们就会掉进 WorkBuddy 缺省，把第三方流量记成官方流量。

对外的视图一律走 :func:`view()`：**永不回显 Key 明文**，只给 ``hasKey`` 与末 4 位
指纹。写入语义见 wb_settings.set_provider_entry()。

只依赖标准库。
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

import wb_accounts
import wb_channels_wire
import wb_settings

#: 注册表位置。**随代码发布**的数据，不是运行期产物（运行期的选择在 settings.json）。
REGISTRY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "providers.json")

#: 用量行的“账号”前缀：`channel:<id>`。渠道 id 从它后面取。
CHANNEL_ACCOUNT_PREFIX = "channel:"

#: 渠道行在 realm 维度上的归属。渠道不属于任何出口（intl / cn），单独一档才不会
#: 把第三方流量混进官方出口的统计里；它是短字符串，recent-index 照常收下。
CHANNEL_REALM = "channel"

#: 探测模型清单的超时（秒）。刻意短：这是面板上一个「点了要等」的动作。
DISCOVER_TIMEOUT = 15.0

#: 「测试」按钮的超时（秒）与前几个 chunk 就收工的预算。测试要证明的是「发得
#: 出去、流回得来、解得开」，不是等模型把话说完。
TEST_TIMEOUT = 30.0
TEST_CHUNK_BUDGET = 8

#: 单次响应体的大小上限（模型清单）：防一个坏掉/恶意的上游把内存吃光。
MAX_MODELS_BYTES = 4 * 1024 * 1024

#: 未知 wire / 认证方式的默认值。
DEFAULT_WIRE = wb_channels_wire.WIRE_CHAT
DEFAULT_AUTH = "bearer"

_registry_lock = threading.Lock()
_registry_cache = {"key": None, "value": {}}


class ChannelError(Exception):
    """渠道不可驱动 / 上游拒绝。``status`` 是给客户端的 HTTP 状态码。

    刻意把「配置问题」（没 Key、被停用、wire 不支持）与「上游问题」（401/429/
    5xx/网络）收在同一个类型里，让 wb_proxy 用一段 except 就能把两边都映射成
    正确的响应：前者 400，后者按上游状态码。

    ``local`` 标记「这是我们自己查出来的配置问题」：调用方据此决定**不记账**
    ——网关没配好不是上游失败，记进错误率会把成功率的含义搞浑。
    """

    def __init__(self, message, status=502, detail="", local=False):
        Exception.__init__(self, message)
        self.status = int(status or 502)
        self.detail = str(detail or "")
        self.local = bool(local)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

def _load_registry():
    """读注册表，按 (mtime, size) 缓存。文件缺失或坏了都读作空表（fail-safe）。"""
    try:
        info = os.stat(REGISTRY_FILE)
        key = (info.st_mtime, info.st_size)
    except OSError:
        key = None
    with _registry_lock:
        if key is not None and _registry_cache["key"] == key:
            return _registry_cache["value"]
    table = {}
    try:
        with open(REGISTRY_FILE, encoding="utf-8") as fh:
            parsed = json.load(fh)
        raw = parsed.get("channels") if isinstance(parsed, dict) else None
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                cid = wb_settings.channel_id(item.get("id"))
                if not cid or cid in table:
                    continue
                entry = dict(item)
                entry["id"] = cid
                table[cid] = entry
    except FileNotFoundError:
        pass
    except Exception:
        pass
    with _registry_lock:
        _registry_cache.update({"key": key, "value": table})
    return table


def registry_channels():
    """注册表里的全部渠道，按 id 排序。返回的是副本，调用方不要改。"""
    table = _load_registry()
    return [dict(table[cid]) for cid in sorted(table)]


def registry_channel(channel):
    """按 id 取注册表条目；查不到返回 None。"""
    cid = wb_settings.channel_id(channel)
    if not cid:
        return None
    found = _load_registry().get(cid)
    return dict(found) if found else None


# ---------------------------------------------------------------------------
# 条目解析（注册表 + 本地设置 → 可用的渠道）
# ---------------------------------------------------------------------------

def _join(base, path):
    base = str(base or "").strip().rstrip("/")
    path = str(path or "").strip()
    if not path:
        return base
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base + "/" + path.lstrip("/")


def base_url(channel, entry=None):
    """生效的 base_url：设置里的覆盖优先，其次注册表缺省。"""
    entry = entry if isinstance(entry, dict) else {}
    override = entry.get("base_url")
    if isinstance(override, str) and override.strip():
        return override.strip().rstrip("/")
    return str(channel.get("base_url") or "").strip().rstrip("/")


def chat_url(channel, entry=None):
    return _join(base_url(channel, entry), "/chat/completions")


def needs_key(channel):
    """这个渠道是不是必须有 Key（本地推理服务不需要）。"""
    if str(channel.get("category") or "").strip().lower() == "local":
        return False
    return str(channel.get("auth") or DEFAULT_AUTH).strip().lower() != "none"


def is_enabled(entry):
    """渠道的启用状态。缺省**关**：不主动把流量发给第三方是安全的默认。"""
    return isinstance(entry, dict) and entry.get("enabled") is True


def can_drive(channel):
    """这个渠道的线协议/认证方式网关是否真的实现过。

    oauth / adc 之类照常列出但标注不可驱动——不假装支持。
    """
    auth = str(channel.get("auth") or DEFAULT_AUTH).strip().lower()
    if auth not in ("bearer", "none"):
        return False
    return wb_channels_wire.wire_supported(channel.get("wire") or DEFAULT_WIRE)


def resolve_key(accounts_dir, channel, entry=None):
    """``(key, source)``。source ∈ {"settings", "env", ""}。

    设置里的 Key 优先，其次注册表声明的环境变量（``key_env``）——前者是面板上
    显式填的，后者是本机部署时更常见的做法（与各家 SDK 的约定一致）。两者都
    只在这里读，绝不出现在 :func:`view()` 之外的任何响应里。
    """
    entry = entry if isinstance(entry, dict) else wb_settings.provider_entry(
        accounts_dir, channel.get("id"))
    stored = entry.get("key")
    if isinstance(stored, str) and stored.strip():
        return stored.strip(), "settings"
    env_name = channel.get("key_env")
    if isinstance(env_name, str) and env_name.strip():
        value = os.environ.get(env_name.strip())
        if isinstance(value, str) and value.strip():
            return value.strip(), "env"
    return "", ""


def effective_models(accounts_dir, channel, entry=None):
    """这个渠道对外暴露的**上游模型 id**（不带渠道前缀）。

    已发现/已配置的优先，其次注册表给的 models_override（给没有模型清单端点的
    渠道兜底，例如 Perplexity）。都没有就是空表——宁可什么都不报，也不猜。
    """
    entry = entry if isinstance(entry, dict) else wb_settings.provider_entry(
        accounts_dir, channel.get("id"))
    configured = wb_settings.clean_channel_models(entry.get("models"))
    if configured:
        return configured
    return wb_settings.clean_channel_models(channel.get("models_override"))


def composite_id(channel_id, upstream_id):
    """渠道里一个模型对外的 id：``<渠道>/<上游模型>``。

    上游模型 id 本身可能带斜杠（OpenRouter 的 ``anthropic/claude-...``），所以
    解析时只切**第一个**斜杠（见 :func:`resolve`）。
    """
    return "%s/%s" % (channel_id, upstream_id)


def channel_models(accounts_dir, channel, entry=None):
    """``[(composite_id, upstream_id)]``，只算启用中的渠道。"""
    cid = channel.get("id")
    entry = entry if isinstance(entry, dict) else wb_settings.provider_entry(accounts_dir, cid)
    if not is_enabled(entry):
        return []
    return [(composite_id(cid, mid), mid)
            for mid in effective_models(accounts_dir, channel, entry)]


def enabled_channels(accounts_dir):
    """启用中的渠道（注册表条目 + 已存设置）。"""
    out = []
    for channel in registry_channels():
        entry = wb_settings.provider_entry(accounts_dir, channel.get("id"))
        if is_enabled(entry):
            out.append((channel, entry))
    return out


def view(accounts_dir, channel, entry=None):
    """对外的渠道视图。**永不包含 Key 明文**——只有 hasKey 与末 4 位指纹。"""
    cid = channel.get("id")
    entry = entry if isinstance(entry, dict) else wb_settings.provider_entry(accounts_dir, cid)
    key, source = resolve_key(accounts_dir, channel, entry)
    registry_models = wb_settings.clean_channel_models(channel.get("models_override"))
    configured = wb_settings.clean_channel_models(entry.get("models"))
    if configured:
        models_source = "configured"
    elif registry_models:
        models_source = "registry"
    else:
        models_source = "none"
    override = entry.get("base_url")
    return {
        "id": cid,
        "name": channel.get("name") or cid,
        "category": str(channel.get("category") or "apikey"),
        "wire": str(channel.get("wire") or DEFAULT_WIRE),
        "wireSupported": wb_channels_wire.wire_supported(
            channel.get("wire") or DEFAULT_WIRE),
        "auth": str(channel.get("auth") or DEFAULT_AUTH),
        "needsKey": needs_key(channel),
        "canDrive": can_drive(channel),
        "baseUrl": base_url(channel, entry),
        "baseUrlDefault": str(channel.get("base_url") or ""),
        "baseUrlOverride": override.strip() if isinstance(override, str) else "",
        "docs": str(channel.get("docs") or ""),
        "keyEnv": str(channel.get("key_env") or ""),
        "enabled": is_enabled(entry),
        "hasKey": bool(key),
        "keyHint": wb_settings.key_fingerprint(key),
        "keySource": source,
        "models": [composite_id(cid, mid)
                   for mid in effective_models(accounts_dir, channel, entry)],
        "modelsSource": models_source,
    }


def channels_view(accounts_dir):
    """全部注册渠道的对外视图（面板 / GET /channels 用）。"""
    return [view(accounts_dir, channel) for channel in registry_channels()]


def resolve(hint, model):
    """这个请求打的是哪个渠道：``(注册表条目, 上游模型 id)`` 或 ``(None, None)``。

    两条来源，顺序即优先级：

    1. 显式提示 ``?provider=<渠道 id>`` / ``X-Provider``；模型 id 原样交给上游。
    2. 模型 id 的 ``<渠道>/`` 前缀；前缀之后（含斜杠）是上游模型 id。

    前缀必须命中**注册表里真实存在的渠道 id**才认，否则 ``foo/bar`` 这种普通
    模型名会被误当成渠道——注册表里的 id 都是各家厂商名，不会和模型名撞车。
    """
    text = str(hint or "").strip().lower()
    if text and text != "all":
        found = registry_channel(text)
        if found is not None:
            return found, str(model or "")
    name = str(model or "")
    if "/" in name:
        prefix, rest = name.split("/", 1)
        found = registry_channel(prefix.strip().lower())
        if found is not None and rest.strip():
            return found, rest.strip()
    return None, ""


# ---------------------------------------------------------------------------
# 归属替身：让渠道用量行不落进 WorkBuddy
# ---------------------------------------------------------------------------

class _ChannelAccount:
    """渠道用量行的账号**替身**。只承载归属字段，不进任何选号路径。"""

    def __init__(self, channel_id):
        self.uid = CHANNEL_ACCOUNT_PREFIX + str(channel_id)
        self.provider = wb_accounts.CHANNEL_PROVIDER
        self.realm = CHANNEL_REALM
        self.nickname = str(channel_id)
        self.channel = str(channel_id)


def is_channel_account(uid):
    return isinstance(uid, str) and uid.startswith(CHANNEL_ACCOUNT_PREFIX)


def channel_of_account(uid):
    return uid[len(CHANNEL_ACCOUNT_PREFIX):] if is_channel_account(uid) else ""


def account_stub(uid):
    """账号 uid → 渠道替身；不是渠道 uid 返回 None。

    调用方（record_usage / record_error）按账号对象取 provider / realm；渠道不在
    POOL 里，不给替身这两行就会掉进 WorkBuddy 缺省。
    """
    if not is_channel_account(uid):
        return None
    return _ChannelAccount(channel_of_account(uid))


def account_uid(channel):
    """一个渠道的用量行账号字段：``channel:<id>``。"""
    cid = channel.get("id") if isinstance(channel, dict) else channel
    return CHANNEL_ACCOUNT_PREFIX + str(cid or "").strip().lower()


# ---------------------------------------------------------------------------
# 上游调用
# ---------------------------------------------------------------------------

class ChannelUpstream:
    """渠道上游的响应，包装成下游管线认的「带 read1() 的字节源」。

    与 QoderUpstreamAdapter / TraeUpstreamAdapter 同一契约：``read1(size)`` 每次
    至多产出若干 ``data: {...}\\n\\n`` 帧，异常（断流 / 超时 / 流内错误）从
    read1() 原样冒出来，由调用方的「上游断流」路径按同一套规则记账。

    另外实现了 ``__iter__``：Responses 的流式事件生成器（见
    responses_stream_events 里那句 ``for raw in upstream``）直接迭代字节源，而
    不是用 read1()。Qoder / Trae 的适配器没有这个方法，那条路今天只有 HTTPResponse
    走得通——渠道这边补上，免得同样的形状在渠道上必炸。
    """

    def __init__(self, resp, model, chunk_id=None, url=""):
        self._resp = resp
        self._chunks = wb_channels_wire.iter_chunks(resp, model, chunk_id)
        self._pending = b""
        self._done = False
        self.url = url
        self.model = model

    @staticmethod
    def _frame(chunk):
        return ("data: %s\n\n" % json.dumps(chunk, ensure_ascii=False)).encode("utf-8")

    def _next_frame(self):
        """取下一帧；没有下一帧返回 b""。异常原样冒出。"""
        while True:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                self._done = True
                return b""
            return self._frame(chunk)

    def read1(self, size=-1):
        while not self._pending and not self._done:
            self._pending = self._next_frame()
        if not self._pending:
            return b""
        if size is None or size < 0:
            data, self._pending = self._pending, b""
            return data
        data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def chunks(self):
        """归一化后的 chunk 字典序列——read1() 的原料，给需要在上面做检查的
        调用方用（见 test_channel）。与 read1() **不可混用**：两边共用同一个
        迭代器。"""
        return self._chunks

    # iter_sse_lines 优先用 read1()；read() 只给按文件对象用的调用方兜底，
    # 所以它要守住文件对象的语义：size < 0 = 一直读到流尽头。
    def read(self, size=-1):
        if size is not None and size >= 0:
            return self.read1(size)
        out = [self._pending]
        self._pending = b""
        while not self._done:
            piece = self._next_frame()
            if not piece:
                break
            out.append(piece)
        return b"".join(out)

    def __iter__(self):
        """按行迭代，行尾带换行——与 http.client.HTTPResponse 的形状一致。"""
        buf = b""
        while True:
            piece = self.read1(wb_channels_wire.READ_BLOCK)
            if not piece:
                break
            buf += piece
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line + b"\n"
        if buf:
            yield buf

    def close(self):
        try:
            if hasattr(self._resp, "close"):
                self._resp.close()
        except Exception:
            pass
        finally:
            self._chunks = iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _read_detail(exc, limit=300):
    try:
        raw = exc.read(limit * 4)
    except Exception:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return " ".join(str(raw or "").split())[:limit]


def _auth_headers(channel, key):
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "wb-proxy/channel",
    }
    auth = str(channel.get("auth") or DEFAULT_AUTH).strip().lower()
    if key and auth == "bearer":
        headers["Authorization"] = "Bearer " + key
    extra = channel.get("extra_headers")
    if isinstance(extra, dict):
        for name, value in extra.items():
            if isinstance(name, str) and isinstance(value, str):
                headers[name] = value
    return headers


def _post_chat(url, body, headers, timeout, on_open=None):
    """发一次 chat 请求，返回响应对象。所有失败都翻成 ChannelError。

    ``stream_options`` 是唯一一个「上游可能不认」的字段：不认它的实现在 400 里
    会点名它。这里据此去掉重试一次——拿得到 token 账本还是尽量要拿到。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers, method="POST"),
            timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = _read_detail(exc)
        if (body.get("stream_options")
                and wb_channels_wire.is_stream_options_rejection(exc.code, detail)):
            retry = dict(body)
            retry.pop("stream_options", None)
            return _post_chat(url, retry, headers, timeout, on_open=on_open)
        raise ChannelError(
            "upstream rejected the request: HTTP %d %s" % (exc.code, detail or exc.reason),
            exc.code, detail)
    except urllib.error.URLError as exc:
        raise ChannelError("channel unreachable: %s" % (exc.reason or exc), 502)
    except Exception as exc:
        raise ChannelError("channel request failed: %s" % exc, 502)
    if on_open is not None:
        try:
            on_open(resp)
        except Exception:
            pass
    return resp


def open_channel_upstream(accounts_dir, channel, upstream_model, payload,
                          timeout=None, on_open=None, allow_disabled=False):
    """渠道版的 ``open_upstream``：返回 ``(ChannelUpstream, 账号替身, None)``。

    与三条内置上游的契约一致（见 wb_proxy._dispatch_chat_post 的调用点）：

    * 配置不齐 / 不可驱动 → :class:`ChannelError`，状态码 400；
    * 上游 401 / 403 / 429 / 5xx → :class:`ChannelError`，状态码照抄上游；
    * 网络失败 → :class:`ChannelError`，502。

    第三个返回值是 ``effort``，渠道不参与思考档位协商，恒为 None。

    ``allow_disabled`` 只给面板的「测试」用：验证一把还没启用的 Key 是正当需求，
    而那扇门（enabled）挡的是**真实流量**。真实请求永远走默认的 False。
    """
    cid = channel.get("id")
    entry = wb_settings.provider_entry(accounts_dir, cid)
    if not allow_disabled and not is_enabled(entry):
        raise ChannelError("channel '%s' is not enabled" % cid, 400, local=True)
    if not can_drive(channel):
        raise ChannelError(
            "channel '%s' cannot be driven by this gateway yet "
            "(wire=%s auth=%s)" % (cid, channel.get("wire"), channel.get("auth")),
            400, local=True)
    key, _source = resolve_key(accounts_dir, channel, entry)
    if needs_key(channel) and not key:
        raise ChannelError("channel '%s' has no API key configured" % cid,
                           400, local=True)
    url = chat_url(channel, entry)
    if not url:
        raise ChannelError("channel '%s' has no base URL configured" % cid,
                           400, local=True)
    body = wb_channels_wire.chat_body(upstream_model, payload, include_usage=True)
    if not body.get("messages"):
        raise ChannelError("no messages to send to channel '%s'" % cid,
                           400, local=True)
    if timeout is None:
        timeout = 120.0
    resp = _post_chat(url, body, _auth_headers(channel, key), timeout, on_open=on_open)
    # 回显客户端**请求的那个** model 串：客户端发的是 `openrouter/xxx` 就回
    # `openrouter/xxx`，发 `?provider=openrouter` + `xxx` 就回 `xxx`。
    echo = str((payload or {}).get("model") or upstream_model)
    chunk_id = "chatcmpl-%s-%d" % (cid[:8], int(time.time() * 1000) % 10 ** 12)
    return ChannelUpstream(resp, echo, chunk_id, url), account_stub(account_uid(cid)), None


def test_channel(accounts_dir, channel, model, timeout=TEST_TIMEOUT):
    """向上游发一个**最小**的 chat 请求，返回 ``{model, latency_ms, chunks, text}``。

    这是面板上「测试」按钮的全部内容：证明「Base URL 对、Key 认、模型名对、
    线协议翻得动」四件事同时成立。它比模型清单探测更有说服力（清单端点可能
    免鉴权，测试请求一定带 Key），代价是上游那边的一次极小请求：提示词固定
    是 ``ping``、``max_tokens=8``，而且只读前几个 chunk 就收工——即使模型话痨
    也只花一个可忽略的量。所以它只应该由使用者点按钮触发，绝不自作主张跑。

    失败语义与 :func:`open_channel_upstream` 完全一致（同一个 ChannelError）。
    """
    cid = channel.get("id")
    model = str(model or "").strip()
    if not model:
        raise ChannelError("channel '%s' has no model to test" % cid, 400, local=True)
    started = time.time()
    upstream, _account, _effort = open_channel_upstream(
        accounts_dir, channel, model,
        {"model": model, "max_tokens": 8,
         "messages": [{"role": "user", "content": "ping"}]},
        timeout=timeout, allow_disabled=True)
    text = []
    chunks = 0
    try:
        for chunk in upstream.chunks():
            chunks += 1
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str):
                    text.append(delta["content"])
            if chunks >= TEST_CHUNK_BUDGET:
                break
    finally:
        upstream.close()
    return {
        "model": model,
        "latency_ms": int((time.time() - started) * 1000),
        "chunks": chunks,
        "text": "".join(text)[:200],
    }


# ---------------------------------------------------------------------------
# 模型清单探测
# ---------------------------------------------------------------------------

def parse_model_ids(raw):
    """从各家 ``GET /models`` 的正文里抠出模型 id。

    认三种形状：OpenAI 的 ``{"data": [{"id": ...}]}``、Ollama 原生
    ``{"models": [{"name": ...}]}``、以及直接一个字符串数组。
    """
    try:
        parsed = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    except Exception:
        return []
    items = None
    if isinstance(parsed, dict):
        items = parsed.get("data")
        if not isinstance(items, list):
            items = parsed.get("models")
    elif isinstance(parsed, list):
        items = parsed
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, str):
            mid = item
        elif isinstance(item, dict):
            mid = item.get("id") or item.get("name") or item.get("model")
        else:
            continue
        if isinstance(mid, str) and mid.strip():
            out.append(mid.strip())
    return wb_settings.clean_channel_models(out)


def discover_models(accounts_dir, channel, timeout=DISCOVER_TIMEOUT):
    """拉一次上游的模型清单，返回上游模型 id 列表（不写盘）。"""
    cid = channel.get("id")
    path = channel.get("list_models")
    entry = wb_settings.provider_entry(accounts_dir, cid)
    if not path:
        raise ChannelError("channel '%s' does not declare a model list endpoint" % cid,
                           400, local=True)
    if not can_drive(channel):
        raise ChannelError("channel '%s' cannot be driven by this gateway yet" % cid,
                           400, local=True)
    key, _source = resolve_key(accounts_dir, channel, entry)
    if needs_key(channel) and not key:
        raise ChannelError("channel '%s' has no API key configured" % cid,
                           400, local=True)
    url = _join(base_url(channel, entry), path)
    headers = _auth_headers(channel, key)
    headers["Accept"] = "application/json"
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers, method="GET"),
                timeout=timeout) as resp:
            raw = resp.read(MAX_MODELS_BYTES)
    except urllib.error.HTTPError as exc:
        detail = _read_detail(exc)
        raise ChannelError("channel '%s' model list failed: HTTP %d %s"
                           % (cid, exc.code, detail or exc.reason), exc.code, detail)
    except ChannelError:
        raise
    except Exception as exc:
        raise ChannelError("channel '%s' model list failed: %s" % (cid, exc), 502)
    return parse_model_ids(raw)
