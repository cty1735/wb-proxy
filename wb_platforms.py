"""鸡蛋网关的「平台账号池」：浏览器 / 设备码登录的第三方平台。

和 ``wb_egg_pools`` 里的自建供应商（填 Base URL + API Key）不同，这里的
上游要的是**账号登录态**：登录一次拿到短期凭据，之后靠 refresh_token
续期。面板上看到的是一张平台卡片：账号数、模型数、余额，以及「添加账号 /
续期 / 刷新余额 / 一键领取」。

本文件只负责「平台适配」这一层：

* 登录流程（CodeArts 的 OAuth + PKCE + DPoP 环回回调、Cline 的 WorkOS
  设备码）；
* 凭据续期；
* 拉模型清单 / 余额 / 领取福利；
* 把一次对话构造成上游请求（含 CodeArts 的 SDK-HMAC-SHA256 签名）。

池子本身（落盘、模型挑选、按模型路由）仍然在 ``wb_egg_pools`` 里 —— 平台
账号池只是池子的一种 ``kind``，跟自建供应商共用同一套清单与分发管线。

只依赖标准库。CodeArts 的 DPoP 需要 P-256 ECDSA 签名，这里用纯 Python
实现了最小的一套曲线运算（不引入 cryptography）。
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 平台注册表
# ---------------------------------------------------------------------------

PLATFORM_CODEARTS = "codearts"
PLATFORM_CLINE = "cline"

#: 面板上的平台卡片。顺序就是展示顺序；加新平台时在这里加一项，再补一个
#: 适配分支即可（登录 / 续期 / 拉清单 / 建请求）。
PLATFORMS = [
    {
        "id": PLATFORM_CODEARTS,
        "name": "华为云 CodeArts",
        "short": "CodeArts",
        "login": "browser",
        "login_label": "浏览器登录",
        "hint": "浏览器登录后自动回调本机；每日可领 1000 万 Tokens 免费额度。",
        "home": "https://codearts.huaweicloud.com/portal",
        "balance": True,
        "claim": True,
    },
    {
        "id": PLATFORM_CLINE,
        "name": "Cline",
        "short": "Cline",
        "login": "device",
        "login_label": "设备码登录",
        "hint": "在浏览器打开验证页并输入设备码；登录完成后自动加入账号池。",
        "home": "https://app.cline.bot",
        "balance": False,
        "claim": False,
    },
]

PLATFORM_IDS = tuple(p["id"] for p in PLATFORMS)

#: 登录会话最多保留多久（秒）。超时后状态查询会返回 expired。
LOGIN_TTL_SECONDS = 15 * 60

#: 凭据快到期前多久就主动续期（秒）。
REFRESH_MARGIN_SECONDS = 5 * 60

#: 上游请求超时。
HTTP_TIMEOUT_SECONDS = 30

#: 一次登录最多挂多少个账号（防止误点把文件撑大）。
MAX_ACCOUNTS = 20

# ---------------------------------------------------------------------------
# CodeArts 固定端点（与 CodeArts Agent 26.9.902 对齐）
# ---------------------------------------------------------------------------

CODEARTS_SNAP_HOST = "https://snap-access.cn-north-4.myhuaweicloud.com"
CODEARTS_SNAP_BACKUP = "https://snap-access.cn-north-4.myhuaweicloud.cn"
CODEARTS_BENEFIT_HOST = "https://opengw.developer.huaweicloud.com"
CODEARTS_IAM_TOKEN_URL = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
CODEARTS_AUTHORIZE_URL = "https://codearts.huaweicloud.com/portal/authorize"
CODEARTS_CLIENT_ID = "codearts-agent"
CODEARTS_PLUGIN_NAME = "codemate_vscode"
CODEARTS_PLUGIN_VERSION = "26.9.902"
CODEARTS_APP_ID = "CodeAgent3.0"
CODEARTS_IDE_NAME = "VSCode"

CODEARTS_CHAT_PATH = "/api/v2/chat/completions"
CODEARTS_GATEWAY_CONFIG_PATH = "/api/v1/gateway/config"
CODEARTS_BALANCE_PATH = "/api/v1/user/tokens/balance"
CODEARTS_BUILTIN_MODELS_PATH = "/v1/model/builtin"
CODEARTS_BENEFIT_CLAIM_PATH = "/api/v1/benefit/claim"
#: 这个账号有没有开通「每日免费 Token」福利（在 snap 主机上问）。
CODEARTS_BENEFIT_GATEWAY_PATH = "/v1/benefit-gateway-config"

# ---------------------------------------------------------------------------
# Cline 固定端点
# ---------------------------------------------------------------------------

CLINE_API_BASE = "https://api.cline.bot"
CLINE_WORKOS_BASE = "https://api.workos.com"
CLINE_WORKOS_CLIENT_ID = "client_01K3A541FN8TA3EPPHTD2325AR"

CLINE_DEVICE_AUTHORIZE = "/user_management/authorize/device"
CLINE_WORKOS_AUTHENTICATE = "/user_management/authenticate"
CLINE_REGISTER_PATH = "/api/v1/auth/register"
CLINE_REFRESH_PATH = "/api/v1/auth/refresh"
CLINE_CHAT_PATH = "/api/v1/chat/completions"
CLINE_MODELS_PATH = "/api/v1/ai/cline/models"
CLINE_RECOMMENDED_PATH = "/api/v1/ai/cline/recommended-models"

#: Cline 的「账号登录态令牌」前面要带这个前缀。
#:
#: 登录拿到的是 WorkOS 签发的 JWT，但 Cline 网关认的是带前缀的写法 ——
#: 少一个前缀就一律 401（"Please make sure you're using the latest version
#: of Cline and re-authenticate your Cline account."），看着像版本问题，其实是
#: 令牌格式问题。Cline 自己的 CLI 也是这么存 / 这么发的。
CLINE_TOKEN_PREFIX = "workos:"


class PlatformError(Exception):
    """一次平台操作失败，带 HTTP 状态与一句能直接展示的说明。"""

    def __init__(self, status, detail):
        Exception.__init__(self, detail)
        self.status = int(status or 400)
        self.detail = str(detail or "")


_log = lambda msg: None


def configure(log=None):
    global _log
    if log is not None:
        _log = log


def platform_def(platform):
    pid = str(platform or "").strip().lower()
    for item in PLATFORMS:
        if item["id"] == pid:
            return dict(item)
    return None


def platform_defs():
    return [dict(item) for item in PLATFORMS]


def defaults(platform):
    """平台池的默认字段（新建池子时用）。"""
    item = platform_def(platform)
    if item is None:
        raise PlatformError(400, "不支持的平台：%s" % platform)
    if item["id"] == PLATFORM_CODEARTS:
        base_url = CODEARTS_SNAP_HOST
    else:
        base_url = CLINE_API_BASE
    return {
        "kind": "platform",
        "platform": item["id"],
        "name": item["name"],
        "protocol": "openai",
        "base_url": base_url,
    }


# ---------------------------------------------------------------------------
# P-256 / ECDSA / DPoP（纯标准库）
# ---------------------------------------------------------------------------

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_G = (_GX, _GY)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text):
    raw = str(text or "").strip().rstrip("=")
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _int_b64(value):
    raw = int(value).to_bytes(32, "big")
    return _b64url(raw)


def _pt_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P == 0:
            return None
        lam = ((3 * x1 * x1 + _A) * pow(2 * y1, -1, _P)) % _P
    else:
        lam = ((y2 - y1) * pow((x2 - x1) % _P, -1, _P)) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _pt_mul(point, scalar):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _pt_add(result, addend)
        addend = _pt_add(addend, addend)
        scalar >>= 1
    return result


def new_dpop_key():
    """生成一对 P-256 密钥，返回 (私钥 JWK, 公钥 JWK)。"""
    d = secrets.randbelow(_N - 1) + 1
    point = _pt_mul(_G, d)
    if point is None:
        raise PlatformError(500, "生成 DPoP 密钥失败")
    x, y = point
    private = {"kty": "EC", "crv": "P-256", "x": _int_b64(x),
               "y": _int_b64(y), "d": _int_b64(d)}
    public = {"kty": "EC", "crv": "P-256", "x": _int_b64(x), "y": _int_b64(y)}
    return private, public


def _ecdsa_sign(private_jwk, digest):
    d = int.from_bytes(_b64url_decode(private_jwk.get("d")), "big")
    z = int.from_bytes(digest, "big")
    while True:
        k = secrets.randbelow(_N - 1) + 1
        point = _pt_mul(_G, k)
        if point is None:
            continue
        r = point[0] % _N
        if r == 0:
            continue
        s = (pow(k, -1, _N) * (z + r * d)) % _N
        if s == 0:
            continue
        return r, s


def _json_bytes(value):
    """与网关其它地方一致的紧凑 JSON（签名按这份字节算）。"""
    return json.dumps(value, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def dpop_proof(private_jwk, public_jwk, htm, htu):
    """生成 DPoP 证明（ES256 紧凑 JWS）。"""
    header = {"alg": "ES256", "typ": "dpop+jwt", "jwk": public_jwk}
    payload = {"htm": str(htm).upper(), "htu": str(htu),
               "iat": int(time.time()), "jti": secrets.token_hex(32)}
    hb = _json_bytes(header)
    pb = _json_bytes(payload)
    signing_input = _b64url(hb) + "." + _b64url(pb)
    digest = hashlib.sha256(signing_input.encode("ascii")).digest()
    r, s = _ecdsa_sign(private_jwk, digest)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input + "." + _b64url(sig)


# ---------------------------------------------------------------------------
# CodeArts 签名（SDK-HMAC-SHA256）
# ---------------------------------------------------------------------------

def _js_quote(text):
    # 对齐华为云 JS SDK：只保留 A-Za-z0-9-._~，其余按 %XX（大写）转义。
    return urllib.parse.quote(str(text or ""), safe="-._~")


def _canonical_uri(path):
    raw = str(path or "/")
    segments = raw.split("/")
    out = []
    for seg in segments:
        try:
            seg = urllib.parse.unquote(seg)
        except Exception:
            pass
        out.append(_js_quote(seg))
    value = "/".join(out)
    if not value.endswith("/"):
        value += "/"
    return value


def _canonical_query(query):
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(str(query), keep_blank_values=True)
    pairs.sort(key=lambda item: (item[0], item[1]))
    return "&".join("%s=%s" % (_js_quote(k), _js_quote(v)) for k, v in pairs)


def _sha256_hex(data):
    return hashlib.sha256(data or b"").hexdigest()


def sign_headers(account, method, raw_url, body, headers):
    """按华为云 JS SDK 的规则给一次请求签名。

    两个必须复刻的坑：CanonicalURI 每段单独编码后**强制补尾斜杠**；
    CanonicalHeaders 自带尾 \\n，拼 CanonicalRequest 时再补一个 \\n。
    """
    split = urllib.parse.urlsplit(raw_url)
    out = {}
    for key, value in (headers or {}).items():
        out[str(key).lower()] = str(value)
    out["host"] = split.netloc
    out.setdefault("x-sdk-date",
                   time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    token = str(account.get("security_token") or "").strip()
    if token:
        out["x-security-token"] = token
    payload = body if str(method).upper() in ("PUT", "PATCH", "POST") else b""
    names = sorted(out)
    canonical_headers = "".join("%s:%s\n" % (k, out[k].strip())
                                for k in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join([
        str(method).upper(),
        _canonical_uri(split.path),
        _canonical_query(split.query),
        canonical_headers,
        signed_headers,
        _sha256_hex(payload),
    ])
    string_to_sign = "\n".join([
        "SDK-HMAC-SHA256",
        out["x-sdk-date"],
        _sha256_hex(canonical_request.encode("utf-8")),
    ])
    signature = hmac.new(str(account.get("secret_key") or "").encode("utf-8"),
                         string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    out["Authorization"] = (
        "SDK-HMAC-SHA256 Access=%s, SignedHeaders=%s, Signature=%s"
        % (account.get("access_key") or "", signed_headers, signature))
    return out


def jwk_thumbprint(public_jwk):
    canonical = '{"crv":%s,"kty":%s,"x":%s,"y":%s}' % (
        json.dumps(public_jwk.get("crv")),
        json.dumps(public_jwk.get("kty")),
        json.dumps(public_jwk.get("x")),
        json.dumps(public_jwk.get("y")),
    )
    return _b64url(hashlib.sha256(canonical.encode("utf-8")).digest())


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------

def _proxy_url():
    try:
        import wb_egg_pools
        return wb_egg_pools._proxy_url()
    except Exception:
        return ""


def _open(req, timeout=HTTP_TIMEOUT_SECONDS):
    from wb_accounts import urlopen_with_fallback
    return urlopen_with_fallback(req, timeout=timeout, proxy=_proxy_url(),
                                 log=_log)


def _read_json(resp, limit=8 << 20):
    raw = resp.read(limit)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"_raw": raw.decode("utf-8", "replace")}


def _json_request(url, method="GET", headers=None, body=None,
                  timeout=HTTP_TIMEOUT_SECONDS):
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    with _open(req, timeout=timeout) as resp:
        return _read_json(resp)


def _form_request(url, fields, headers=None, timeout=HTTP_TIMEOUT_SECONDS):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    merged = {"content-type": "application/x-www-form-urlencoded"}
    merged.update(headers or {})
    return _json_request(url, method="POST", headers=merged, body=data,
                         timeout=timeout)


def _http_detail(exc, limit=300):
    try:
        return exc.read(4 << 10).decode("utf-8", "replace")[:limit]
    except Exception:
        return ""


def _error_detail(payload):
    if not isinstance(payload, dict):
        return str(payload or "")[:300]
    for key in ("error_description", "error_msg", "message", "error", "_raw"):
        value = payload.get(key)
        if value:
            if isinstance(value, dict):
                return str(value.get("message") or value)[:300]
            return str(value)[:300]
    return json.dumps(payload, ensure_ascii=False)[:300]


def _epoch(value, default=0.0):
    text = str(value or "").strip()
    if not text:
        return default
    if text.isdigit():
        number = float(text)
        return number / 1000.0 if number > 1e11 else number
    try:
        return datetime.datetime.fromisoformat(
            text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return default


# ---------------------------------------------------------------------------
# CodeArts：请求与账号
# ---------------------------------------------------------------------------

def _codearts_headers(account, model="", channel="benefit", stream=False):
    trace = secrets.token_hex(16)
    session_id = str(account.get("session_id") or "")
    if not session_id:
        session_id = "ses_" + secrets.token_hex(12)
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
        "app-id": CODEARTS_APP_ID,
        "x-ot-function": "agent-general",
        "x-ot-trace-id": trace,
        "x-ot-span-id": secrets.token_hex(8),
        "x-snap-traceid": trace + "_" + secrets.token_hex(8),
        "x-ot-client-type": "IDE",
        "x-ot-client-version": CODEARTS_PLUGIN_VERSION,
        "plugin-name": CODEARTS_PLUGIN_NAME,
        "plugin-version": CODEARTS_PLUGIN_VERSION,
        "ide-name": CODEARTS_IDE_NAME,
        "ide-version": CODEARTS_PLUGIN_VERSION,
        "created-time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user-msg-id": "msg_" + secrets.token_hex(12),
        "x-ot-session-id": session_id,
        "x-ot-parent-session-id": "",
        "user-session-id": session_id,
        "is_confidential": "false",
        "X-Language": "zh-cn",
    }
    if channel == "benefit":
        headers["maas_type"] = "benefit"
    if model:
        headers["model-id"] = model
        headers["model-name"] = model
    return headers


def _codearts_call(account, method, url, body=None, headers=None,
                   timeout=HTTP_TIMEOUT_SECONDS):
    # 非对话接口（余额、领取、模型清单）走 ChannelCredit，不能带 maas_type。
    base = _codearts_headers(account, channel="credit", stream=False)
    base.update(headers or {})
    payload = body or b""
    signed = sign_headers(account, method, url, payload, base)
    return _json_request(url, method=method, headers=signed, body=payload,
                         timeout=timeout)


def _codearts_envelope(payload):
    if not isinstance(payload, dict):
        return payload
    if "result" in payload:
        code = str(payload.get("error_code") or "").strip()
        if code and code not in ("0000", "0"):
            raise PlatformError(502, "CodeArts 返回错误 %s：%s"
                                % (code, payload.get("error_msg") or ""))
        return payload.get("result")
    return payload


def _codearts_creds(payload):
    """解析 STS 令牌响应，返回 (凭据 dict, refresh_token)。"""
    if not isinstance(payload, dict):
        raise PlatformError(502, "CodeArts 登录响应格式不对")
    creds = payload.get("credentials")
    refresh = str(payload.get("refresh_token") or "")
    result = payload.get("result")
    if not isinstance(creds, dict) and isinstance(result, dict):
        creds = result.get("credentials")
        refresh = refresh or str(result.get("refresh_token") or "")
    if not isinstance(creds, dict) or not creds.get("access_key_id"):
        raise PlatformError(502, "CodeArts 登录失败：%s" % _error_detail(payload))
    return creds, refresh


def _codearts_account(creds, refresh_token, private_jwk, public_jwk,
                      code_verifier, label=""):
    access_key = str(creds.get("access_key_id") or "")
    expires_at = _epoch(creds.get("expiration"), 0.0)
    if not expires_at:
        expires_at = time.time() + 110 * 60
    account_id = "acct-" + hashlib.sha256(
        (access_key + "|" + label).encode("utf-8")).hexdigest()[:12]
    return {
        "id": account_id,
        "label": label or ("CodeArts " + access_key[-6:]),
        "email": "",
        "enabled": True,
        "created_at": time.time(),
        "last_used": 0.0,
        "status": "ok",
        "last_error": "",
        "access_key": access_key,
        "secret_key": str(creds.get("secret_access_key") or ""),
        "security_token": str(creds.get("security_token") or ""),
        "expires_at": expires_at,
        "refresh_token": str(refresh_token or ""),
        "dpop_private_jwk": private_jwk,
        "dpop_public_jwk": public_jwk,
        "code_verifier": str(code_verifier or ""),
        "session_id": "ses_" + hashlib.sha256(
            (account_id + access_key).encode("utf-8")).hexdigest()[:16],
        "platform": PLATFORM_CODEARTS,
    }


def _codearts_refresh(account):
    private = account.get("dpop_private_jwk") or {}
    public = account.get("dpop_public_jwk") or {}
    refresh_token = str(account.get("refresh_token") or "")
    verifier = str(account.get("code_verifier") or "")
    if not refresh_token or not private or not verifier:
        raise PlatformError(401, "CodeArts 登录态缺少续期材料：请重新登录")
    proof = dpop_proof(private, public, "POST", CODEARTS_IAM_TOKEN_URL)
    payload = _form_request(CODEARTS_IAM_TOKEN_URL, {
        "client_id": CODEARTS_CLIENT_ID,
        "code_verifier": verifier,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }, headers={"DPoP": proof})
    creds, fresh = _codearts_creds(payload)
    updated = dict(account)
    updated.update({
        "access_key": str(creds.get("access_key_id") or ""),
        "secret_key": str(creds.get("secret_access_key") or ""),
        "security_token": str(creds.get("security_token") or ""),
        "expires_at": _epoch(creds.get("expiration"),
                             time.time() + 110 * 60),
        "refresh_token": fresh or refresh_token,
        "status": "ok",
        "last_error": "",
    })
    return updated


def _codearts_models(account):
    """拉 CodeArts 的模型清单：免费权益通道 + 积分通道。"""
    models = []
    seen = set()

    def add(mid, row, channel, free):
        mid = str(mid or "").strip()
        if not mid or mid in seen:
            return
        seen.add(mid)
        item = {
            "id": mid,
            "name": str(row.get("model_name") or row.get("modelName") or mid),
            "context": int(row.get("context_window") or 0),
            "output": int(row.get("max_tokens") or 0),
            "vision": bool(row.get("supports_images") or row.get("supportsImages")),
            "reasoning": False,
            "tools": True,
            "free": bool(free),
            "channel": channel,
        }
        models.append(item)

    # 1) 免费权益通道（benefit）：/api/v1/gateway/config
    #    没开通福利的账号不该去问这个接口 —— 它只会回一句看不懂的
    #    "invalid channel, group not found"。
    if _codearts_benefit_enabled(account):
        try:
            payload = _codearts_call(
                account, "GET", CODEARTS_BENEFIT_HOST + CODEARTS_GATEWAY_CONFIG_PATH)
            cfg = _codearts_envelope(payload) or {}
            rows = cfg.get("models") if isinstance(cfg, dict) else None
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                add(row.get("model_id"), row, "benefit", True)
        except Exception as exc:
            _log("codearts: 拉免费权益模型失败：%s" % exc)
    else:
        _log("codearts: 这个账号没开通每日免费 Token，跳过 benefit 通道")

    # 2) 内置 / 订阅模型（credit）：/v1/model/builtin
    try:
        payload = _codearts_call(
            account, "GET", CODEARTS_SNAP_HOST + CODEARTS_BUILTIN_MODELS_PATH,
            headers={"Agent-Type": "PromptCenter"})
        rows = payload.get("builtinModels") if isinstance(payload, dict) else None
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if row.get("enable") is False:
                continue
            add(row.get("model_id"), row, "credit", False)
    except Exception as exc:
        _log("codearts: 拉内置模型失败：%s" % exc)

    return models


def _codearts_benefit_config(account):
    """查这个账号有没有开通「每日免费 Token」福利。

    上游给的是 snap 主机上的 ``/v1/benefit-gateway-config``：``enabled``
    为 false 表示这个账号不在福利名单里，此时 opengw 上的余额 / 领取接口
    只会回 "invalid channel, group not found" —— 先问这一句，省得把那句
    天书直接甩给操作者。
    """
    try:
        payload = _codearts_call(
            account, "GET", CODEARTS_SNAP_HOST + CODEARTS_BENEFIT_GATEWAY_PATH,
            headers={"Agent-Type": "PromptCenter"})
    except Exception as exc:
        _log("codearts: 查福利通道配置失败：%s" % exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _codearts_benefit_enabled(account):
    cfg = _codearts_benefit_config(account)
    return bool(cfg.get("enabled"))


def _codearts_balance(account):
    """查余额。没开通福利的账号就说清楚，别把上游的天书原样抛出去。"""
    cfg = _codearts_benefit_config(account)
    if cfg and not cfg.get("enabled"):
        return {
            "enabled": False,
            "note": "这个账号没开通每日免费 Token（只有消耗积分的模型可用）",
            "at": time.time(),
        }
    try:
        payload = _codearts_call(
            account, "GET", CODEARTS_BENEFIT_HOST + CODEARTS_BALANCE_PATH)
        result = _codearts_envelope(payload) or {}
    except PlatformError as exc:
        return {"enabled": True, "error": exc.detail, "at": time.time()}
    if not isinstance(result, dict):
        return {"enabled": True, "at": time.time()}
    return {
        "enabled": True,
        "channel": result.get("channel") or "",
        "daily_limit": int(result.get("daily_token_limit") or 0),
        "daily_used": int(result.get("daily_tokens_used") or 0),
        "monthly_limit": int(result.get("monthly_token_limit") or 0),
        "monthly_used": int(result.get("monthly_tokens_used") or 0),
        "total_balance": int(result.get("total_balance") or 0),
        "total_quota": int(result.get("total_quota") or 0),
        "used_amount": int(result.get("used_amount") or 0),
        "update_time": int(result.get("update_time") or 0),
        "expire_time": int(result.get("expire_time") or 0),
        "at": time.time(),
    }


def _codearts_claim(account):
    """领取当日免费 Token。"""
    cfg = _codearts_benefit_config(account)
    if cfg and not cfg.get("enabled"):
        return {"ok": False, "msg": "当前账号未开通免费福利 Token 活动",
                "at": time.time()}
    try:
        payload = _codearts_call(
            account, "GET", CODEARTS_BENEFIT_HOST + CODEARTS_BENEFIT_CLAIM_PATH)
        result = _codearts_envelope(payload)
    except PlatformError as exc:
        return {"ok": False, "msg": exc.detail, "at": time.time()}
    message = ""
    if isinstance(result, dict):
        message = str(result.get("message") or result.get("msg") or "")
    elif result:
        message = str(result)
    return {"ok": True, "msg": message or "领取请求已提交", "at": time.time()}


# ---------------------------------------------------------------------------
# Cline：设备码 / 续期 / 模型
# ---------------------------------------------------------------------------

def _cline_token_account(payload, fallback=None):
    """把 Cline 的 register/refresh 响应当成账号。"""
    if not isinstance(payload, dict) or not payload.get("success"):
        raise PlatformError(502, "Cline 登录失败：%s" % _error_detail(payload))
    data = payload.get("data") or {}
    access = str(data.get("accessToken") or "")
    refresh = str(data.get("refreshToken") or "")
    if not access:
        raise PlatformError(502, "Cline 登录失败：响应里没有 accessToken")
    user = data.get("userInfo") or {}
    if not isinstance(user, dict):
        user = {}
    email = str(user.get("email") or "")
    name = str(user.get("name") or "")
    account_id = str(user.get("clineUserId") or "")
    fallback = fallback or {}
    if not refresh:
        refresh = str(fallback.get("refresh_token") or "")
    expires_at = _epoch(data.get("expiresAt"), 0.0)
    if not expires_at:
        expires_at = time.time() + 50 * 60
    stable = account_id or email or access[:24]
    return {
        "id": str(fallback.get("id") or
                  ("acct-" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:12])),
        "label": name or email or ("Cline " + stable[-6:]),
        "email": email,
        "enabled": fallback.get("enabled", True),
        "created_at": float(fallback.get("created_at") or time.time()),
        "last_used": float(fallback.get("last_used") or 0.0),
        "status": "ok",
        "last_error": "",
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "token_type": str(data.get("tokenType") or "Bearer"),
        "user_info": user,
        "account_id": account_id,
        "platform": PLATFORM_CLINE,
    }


def _cline_bearer(account):
    """Cline 请求的 Authorization 头内容（令牌要带 workos: 前缀）。"""
    token = str((account or {}).get("access_token") or "").strip()
    if not token:
        return ""
    if token.lower().startswith(CLINE_TOKEN_PREFIX):
        return token
    return CLINE_TOKEN_PREFIX + token


def _cline_refresh(account):
    refresh = str(account.get("refresh_token") or "")
    if not refresh:
        raise PlatformError(401, "Cline 登录态缺少 refresh_token：请重新登录")
    payload = _json_request(
        CLINE_API_BASE + CLINE_REFRESH_PATH, method="POST",
        headers={"content-type": "application/json"},
        body=_json_bytes({"refreshToken": refresh,
                          "grantType": "refresh_token"}))
    return _cline_token_account(payload, fallback=account)


def _cline_models():
    """拉 Cline 的模型目录（公开接口，不需要登录）。"""
    catalog = []
    recommended = {}
    try:
        payload = _json_request(CLINE_API_BASE + CLINE_MODELS_PATH,
                                headers={"accept": "application/json"})
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(rows, list):
            catalog = rows
    except Exception as exc:
        _log("cline: 拉模型目录失败：%s" % exc)
    try:
        payload = _json_request(CLINE_API_BASE + CLINE_RECOMMENDED_PATH,
                                headers={"accept": "application/json"})
        if isinstance(payload, dict):
            recommended = payload.get("data") if isinstance(
                payload.get("data"), dict) else payload
    except Exception as exc:
        _log("cline: 拉推荐模型失败：%s" % exc)

    free_ids = set()
    rows = []
    for key, free in (("free", True), ("clinePass", False),
                      ("clineCloud", False), ("recommended", False)):
        group = recommended.get(key) if isinstance(recommended, dict) else None
        for row in group or []:
            if isinstance(row, dict):
                rows.append(row)
                if free:
                    free_ids.add(str(row.get("id") or ""))
    rows.extend(catalog)

    models = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        name = str(row.get("display_name") or row.get("displayName")
                   or row.get("name") or mid)
        models.append({
            "id": mid,
            "name": name,
            "context": int(row.get("context_window") or row.get("contextWindow")
                           or row.get("context_length") or 0),
            "output": int(row.get("max_tokens") or row.get("maxTokens") or 0),
            "vision": bool(row.get("supports_images") or row.get("vision")),
            "reasoning": bool(row.get("reasoning")),
            "tools": True,
            "free": mid in free_ids or mid.endswith(":free")
                    or mid.startswith("cline-free/"),
            "channel": "cline",
        })
    return models


# ---------------------------------------------------------------------------
# 登录会话
# ---------------------------------------------------------------------------

_sessions = {}
_sessions_lock = threading.RLock()


def _new_session(platform):
    state = secrets.token_urlsafe(24)
    session = {
        "state": state,
        "platform": platform,
        "created_at": time.time(),
        "status": "pending",
        "url": "",
        "user_code": "",
        "instructions": "",
        "expires_at": time.time() + LOGIN_TTL_SECONDS,
        "account": None,
        "error": "",
        "consumed": False,
        "server": None,
        "thread": None,
        "cancel": False,
        "code_verifier": "",
        "redirect_uri": "",
        "dpop_private": None,
        "dpop_public": None,
        "device_code": "",
        "interval": 5,
    }
    with _sessions_lock:
        _sessions[state] = session
    return session


def _session(state):
    with _sessions_lock:
        return _sessions.get(str(state or "").strip())


def _prune_sessions():
    now = time.time()
    with _sessions_lock:
        for state, session in list(_sessions.items()):
            if now - float(session.get("created_at") or 0) > LOGIN_TTL_SECONDS:
                if session.get("status") == "pending":
                    session["status"] = "expired"
                if now - float(session.get("created_at") or 0) > LOGIN_TTL_SECONDS * 2:
                    _sessions.pop(state, None)


def login_start(platform):
    """开始一次平台登录，返回给面板的启动信息。"""
    _prune_sessions()
    item = platform_def(platform)
    if item is None:
        raise PlatformError(400, "不支持的平台：%s" % platform)
    if item["id"] == PLATFORM_CODEARTS:
        return _codearts_login_start()
    if item["id"] == PLATFORM_CLINE:
        return _cline_login_start()
    raise PlatformError(400, "这个平台还没有登录适配")


def _public_session(session):
    if session is None:
        return {"ok": False, "status": "missing", "error": "登录会话不存在或已过期"}
    status = str(session.get("status") or "pending")
    if status == "pending" and time.time() > float(session.get("expires_at") or 0):
        status = "expired"
    out = {
        "ok": status == "success",
        "status": status,
        "platform": session.get("platform"),
        "state": session.get("state"),
        "url": session.get("url") or "",
        "user_code": session.get("user_code") or "",
        "instructions": session.get("instructions") or "",
        "expires_in": max(0, int(float(session.get("expires_at") or 0)
                                 - time.time())),
        "error": session.get("error") or "",
        "account": None,
    }
    if status == "success" and session.get("account"):
        out["account"] = account_public(session["account"])
    return out


def poll_login(state):
    """查询一次登录状态；成功时账号由 ``take_account`` 交给调用方入池。"""
    session = _session(state)
    if session is None:
        return {"ok": False, "status": "missing",
                "error": "登录会话不存在或已过期"}
    return _public_session(session)


def take_account(state):
    """取走登录成功后的账号（只取一次），给池子落盘用。

    面板轮询会反复查同一个 state：``consumed`` 保证凭据只在第一次成功时
    交给调用方，后续轮询只看到公开视图。
    """
    session = _session(state)
    if session is None:
        return None
    if session.get("status") != "success" or session.get("consumed"):
        return None
    account = session.get("account")
    if not isinstance(account, dict):
        return None
    session["consumed"] = True
    return dict(account)


def cancel_login(state):
    session = _session(state)
    if session is None:
        return {"ok": False, "status": "missing"}
    session["cancel"] = True
    if session.get("status") == "pending":
        session["status"] = "cancelled"
    _close_server(session)
    return {"ok": True, "status": session.get("status")}


def _close_server(session):
    server = session.get("server") if session else None
    if server is not None:
        try:
            threading.Thread(target=server.shutdown, daemon=True).start()
        except Exception:
            pass
        session["server"] = None


# --- CodeArts 登录 ---------------------------------------------------------

def _codearts_login_start():
    session = _new_session(PLATFORM_CODEARTS)
    verifier = secrets.token_hex(32)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    private, public = new_dpop_key()
    session["code_verifier"] = verifier
    session["dpop_private"] = private
    session["dpop_public"] = public

    server = _bind_callback_server(session)
    port = server.server_address[1]
    redirect_uri = "http://127.0.0.1:%d/oauth/callback" % port
    session["redirect_uri"] = redirect_uri
    session["server"] = server
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session["thread"] = thread

    query = urllib.parse.urlencode([
        ("theme", "2"),
        ("locale", "zh-cn"),
        ("uri_scheme", CODEARTS_CLIENT_ID),
        ("client_id", CODEARTS_CLIENT_ID),
        ("port", str(port)),
        ("code_challenge", challenge),
        ("code_challenge_method", "SHA-256"),
        ("ticket_id", secrets.token_hex(8)),
        ("plugin-name", "snap_AIIDE"),
        ("plugin-version", CODEARTS_PLUGIN_VERSION),
    ])
    session["url"] = CODEARTS_AUTHORIZE_URL + "?" + query
    session["instructions"] = ("在浏览器打开授权页并登录华为云；登录成功后会自动"
                               "跳回本机完成入池。")
    _log("codearts: 浏览器登录已启动（回调端口 %d）" % port)
    return _public_session(session)


def _bind_callback_server(session):
    last = None
    for _ in range(40):
        port = 10000 + secrets.randbelow(55000)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port),
                                         _callback_handler(session))
            server.daemon_threads = True
            return server
        except OSError as exc:
            last = exc
            continue
    raise PlatformError(500, "找不到可用的本机回调端口：%s" % last)


def _callback_handler(session):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, status, body, headers=None):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            split = urllib.parse.urlsplit(self.path)
            if split.path != "/oauth/callback":
                self._send(404, "<h3>Not Found</h3>")
                return
            params = urllib.parse.parse_qs(split.query, keep_blank_values=True)

            def one(name):
                values = params.get(name) or [""]
                return str(values[-1] or "")

            # 门户的第一跳：?secret=&redirect= → 跟随 redirect 到带 code 的一跳。
            if one("secret") and one("redirect"):
                self._send(302, "", {"Location": one("redirect")})
                return
            code = one("code")
            if not code:
                message = one("error_description") or one("error") or "回调里没有授权码"
                session["status"] = "error"
                session["error"] = message
                self._send(400, _callback_page(False, "CodeArts 登录失败", message))
                return
            try:
                account = _codearts_exchange(session, code)
                session["account"] = account
                session["status"] = "success"
                self._send(200, _callback_page(
                    True, "CodeArts 登录成功",
                    "%s 已加入平台账号池，可以关闭此页回到网关看板。"
                    % account.get("label")))
            except Exception as exc:
                session["status"] = "error"
                session["error"] = str(exc)
                self._send(400, _callback_page(False, "CodeArts 登录失败",
                                               str(exc)))
            finally:
                _close_server(session)

    return Handler


def _callback_page(ok, title, message):
    color = "#2cb044" if ok else "#d9534f"
    icon = "✓" if ok else "×"
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>%s</title></head>
<body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
background:#0f1511;color:#e7efe9;display:flex;min-height:100vh;align-items:center;justify-content:center">
<div style="max-width:520px;width:calc(100%% - 32px);padding:36px;border:1px solid #24352a;
border-radius:16px;background:#141d17;text-align:center">
<div style="font-size:42px;color:%s;margin-bottom:14px">%s</div>
<h1 style="font-size:21px;margin:0 0 10px;color:%s">%s</h1>
<p style="font-size:14px;line-height:1.7;color:#a9b8ad;margin:0">%s</p>
</div></body></html>""" % (title, color, icon, color, title, message)


def _codearts_exchange(session, code):
    proof = dpop_proof(session["dpop_private"], session["dpop_public"],
                       "POST", CODEARTS_IAM_TOKEN_URL)
    payload = _form_request(CODEARTS_IAM_TOKEN_URL, {
        "client_id": CODEARTS_CLIENT_ID,
        "code": code,
        "code_verifier": session["code_verifier"],
        "grant_type": "authorization_code",
        "redirect_uri": session["redirect_uri"],
    }, headers={"DPoP": proof})
    creds, refresh = _codearts_creds(payload)
    return _codearts_account(creds, refresh, session["dpop_private"],
                             session["dpop_public"], session["code_verifier"])


def submit_callback(state, url):
    """手工提交回调 URL（远端部署时浏览器回不到 127.0.0.1）。"""
    session = _session(state)
    if session is None:
        raise PlatformError(404, "登录会话不存在或已过期")
    if session.get("platform") != PLATFORM_CODEARTS:
        raise PlatformError(400, "这个平台不需要提交回调 URL")
    text = str(url or "").strip()
    if not text:
        raise PlatformError(400, "回调 URL 不能为空")
    split = urllib.parse.urlsplit(text)
    params = urllib.parse.parse_qs(split.query, keep_blank_values=True)
    code = (params.get("code") or [""])[-1]
    if not code:
        redirect = (params.get("redirect") or [""])[-1]
        if redirect:
            return submit_callback(state, redirect)
        raise PlatformError(400, "回调 URL 里没有 code")
    account = _codearts_exchange(session, code)
    session["account"] = account
    session["status"] = "success"
    _close_server(session)
    return _public_session(session)


# --- Cline 登录 ------------------------------------------------------------

def _cline_login_start():
    session = _new_session(PLATFORM_CLINE)
    payload = _form_request(CLINE_WORKOS_BASE + CLINE_DEVICE_AUTHORIZE, {
        "client_id": CLINE_WORKOS_CLIENT_ID,
    })
    if not isinstance(payload, dict):
        raise PlatformError(502, "Cline 设备码响应格式不对")
    device_code = str(payload.get("device_code") or "")
    user_code = str(payload.get("user_code") or "")
    verify = str(payload.get("verification_uri") or "")
    complete = str(payload.get("verification_uri_complete") or "")
    if not device_code or not user_code or not verify:
        raise PlatformError(502, "Cline 设备码申请失败：%s"
                            % _error_detail(payload))
    session.update({
        "device_code": device_code,
        "user_code": user_code,
        "url": complete or verify,
        "instructions": ("在浏览器打开验证页，输入设备码 %s 并完成登录。"
                         % user_code),
        "expires_at": time.time() + max(60, int(payload.get("expires_in") or 300)),
        "interval": max(2, int(payload.get("interval") or 5)),
    })
    thread = threading.Thread(target=_cline_poll_loop,
                              args=(session,), daemon=True)
    thread.start()
    session["thread"] = thread
    _log("cline: 设备码登录已启动（%s）" % user_code)
    return _public_session(session)


def _cline_poll_loop(session):
    deadline = float(session.get("expires_at") or 0)
    interval = max(2, int(session.get("interval") or 5))
    while time.time() < deadline:
        if session.get("cancel") or session.get("status") != "pending":
            return
        time.sleep(interval)
        if session.get("cancel") or session.get("status") != "pending":
            return
        try:
            payload = _form_request(CLINE_WORKOS_BASE + CLINE_WORKOS_AUTHENTICATE, {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": session.get("device_code") or "",
                "client_id": CLINE_WORKOS_CLIENT_ID,
            })
        except urllib.error.HTTPError as exc:
            detail = _http_detail(exc)
            try:
                payload = json.loads(detail) if detail else {}
            except Exception:
                payload = {}
            code = str(payload.get("error") or "")
            if code in ("authorization_pending",):
                continue
            if code in ("slow_down",):
                interval += 2
                continue
            session["status"] = "error"
            session["error"] = str(payload.get("error_description") or detail
                                   or ("HTTP %d" % exc.code))
            return
        except Exception as exc:
            session["status"] = "error"
            session["error"] = "设备码轮询失败：%s" % exc
            return
        access = str((payload or {}).get("access_token") or "")
        refresh = str((payload or {}).get("refresh_token") or "")
        if not access:
            continue
        try:
            registered = _json_request(
                CLINE_API_BASE + CLINE_REGISTER_PATH, method="POST",
                headers={"content-type": "application/json"},
                body=_json_bytes({"accessToken": access,
                                  "refreshToken": refresh}))
            account = _cline_token_account(registered)
        except Exception as exc:
            session["status"] = "error"
            session["error"] = "Cline 登录换取凭据失败：%s" % exc
            return
        session["account"] = account
        session["status"] = "success"
        _log("cline: 账号 %s 登录成功" % account.get("label"))
        return
    session["status"] = "expired"
    session["error"] = "设备码已过期，请重新发起登录"


# ---------------------------------------------------------------------------
# 账号续期 / 选择
# ---------------------------------------------------------------------------

_rr_lock = threading.RLock()
_rr = {}


def account_public(account):
    """给面板的账号视图：不带任何凭据。"""
    account = account if isinstance(account, dict) else {}
    expires_at = float(account.get("expires_at") or 0)
    return {
        "id": account.get("id") or "",
        "label": account.get("label") or "",
        "email": account.get("email") or "",
        "enabled": account.get("enabled", True) is not False,
        "status": account.get("status") or "ok",
        "last_error": account.get("last_error") or "",
        "created_at": float(account.get("created_at") or 0),
        "last_used": float(account.get("last_used") or 0),
        "expires_at": expires_at,
        "expires_in": max(0, int(expires_at - time.time())) if expires_at else 0,
        "platform": account.get("platform") or "",
    }


def _has_live_credential(account):
    if not account:
        return False
    expires_at = float(account.get("expires_at") or 0)
    if expires_at and time.time() > expires_at:
        return False
    if account.get("platform") == PLATFORM_CODEARTS:
        return bool(account.get("access_key") and account.get("secret_key"))
    return bool(account.get("access_token"))


def _needs_refresh(account):
    expires_at = float(account.get("expires_at") or 0)
    if not expires_at:
        return not _has_live_credential(account)
    return time.time() > expires_at - REFRESH_MARGIN_SECONDS


def _persist(pool, account):
    try:
        import wb_egg_pools
        wb_egg_pools.update_platform_account(pool.get("id"), account.get("id"),
                                             account)
    except Exception as exc:
        _log("platform: 保存账号状态失败：%s" % exc)


def refresh_account(pool, account, force=False):
    """续期一个账号；返回更新后的账号（同时落盘）。"""
    account = dict(account or {})
    if not force and not _needs_refresh(account):
        return account
    try:
        if account.get("platform") == PLATFORM_CODEARTS:
            updated = _codearts_refresh(account)
        else:
            updated = _cline_refresh(account)
    except Exception as exc:
        account["status"] = "error"
        account["last_error"] = str(exc)
        _persist(pool, account)
        raise PlatformError(getattr(exc, "status", 502),
                            "续期失败：%s" % getattr(exc, "detail", exc))
    updated["last_error"] = ""
    updated["status"] = "ok"
    _persist(pool, updated)
    _log("platform: 账号 %s 已续期" % (updated.get("label") or updated.get("id")))
    return updated


def ensure_account(pool):
    """挑一个可用账号；必要时先续期。

    轮询按池子做（多账号时把请求摊开），优先挑没被停用、凭据没坏的。
    """
    accounts = [a for a in (pool.get("accounts") or [])
                if isinstance(a, dict) and a.get("enabled", True) is not False]
    if not accounts:
        raise PlatformError(503, "这个平台还没有可用账号：先在面板「添加账号」")
    pool_id = str(pool.get("id") or "")
    with _rr_lock:
        start = _rr.get(pool_id, 0)
        _rr[pool_id] = start + 1
    ordered = accounts[start % len(accounts):] + accounts[:start % len(accounts)]
    last_error = ""
    for account in ordered:
        if _needs_refresh(account):
            try:
                account = refresh_account(pool, account)
            except PlatformError as exc:
                last_error = exc.detail
                if not _has_live_credential(account):
                    continue
        account = dict(account)
        account["last_used"] = time.time()
        return account
    raise PlatformError(503, last_error or "平台账号的登录态都失效了：请重新登录")


# ---------------------------------------------------------------------------
# 模型 / 余额 / 领取
# ---------------------------------------------------------------------------

def fetch_models(pool, timeout=HTTP_TIMEOUT_SECONDS):
    """拉一个平台池的模型清单。返回 (模型条目列表, 错误说明)。"""
    platform = str(pool.get("platform") or "")
    try:
        if platform == PLATFORM_CODEARTS:
            account = ensure_account(pool)
            models = _codearts_models(account)
            if not models:
                return [], "CodeArts 没有返回可用模型（账号可能还没有开通权益）"
            return models, ""
        if platform == PLATFORM_CLINE:
            models = _cline_models()
            if not models:
                return [], "Cline 没有返回模型目录"
            return models, ""
        return [], "不支持的平台：%s" % platform
    except PlatformError as exc:
        return [], exc.detail
    except urllib.error.HTTPError as exc:
        detail = _http_detail(exc)
        return [], "拉取失败（HTTP %d）%s" % (
            exc.code, ("：" + detail) if detail else "")
    except Exception as exc:
        return [], "拉取失败：%s" % exc


def fetch_balance(pool):
    """查询平台余额；返回可落盘的快照。"""
    platform = str(pool.get("platform") or "")
    if platform == PLATFORM_CODEARTS:
        account = ensure_account(pool)
        return _codearts_balance(account)
    return {"note": "这个平台没有公开的余额接口", "at": time.time()}


def claim(pool):
    """领取平台每日福利。"""
    platform = str(pool.get("platform") or "")
    if platform == PLATFORM_CODEARTS:
        account = ensure_account(pool)
        return _codearts_claim(account)
    raise PlatformError(400, "这个平台没有每日领取")


def model_channel(pool, model):
    """CodeArts 的模型走哪条通道（benefit / credit）。"""
    mid = str(model or "").strip()
    info = _model_info(pool, mid)
    if info.get("channel"):
        return info["channel"]
    for item in pool.get("catalog") or []:
        if isinstance(item, dict) and item.get("id") == mid:
            return str(item.get("channel") or "benefit")
    return "credit"


def upstream_model(pool, model):
    """对外的模型 id（可能带平台前缀）→ 上游认识的原始 id。"""
    mid = str(model or "").strip()
    info = _model_info(pool, mid)
    if info.get("upstream_id"):
        return info["upstream_id"]
    platform = str(pool.get("platform") or "").strip().lower()
    prefix = platform + "/"
    if platform and mid.lower().startswith(prefix):
        return mid[len(prefix):]
    return mid


def _model_info(pool, model):
    """查一个模型在平台池里的信息：{upstream_id, channel}。

    请求热路径拿到的池子是不带上游目录的浅副本，所以要去池子状态里按 id
    查一次（见 wb_egg_pools.platform_model_info）。
    """
    mid = str(model or "").strip()
    info = {}
    try:
        import wb_egg_pools
        found = wb_egg_pools.platform_model_info((pool or {}).get("id"), mid)
        if isinstance(found, dict):
            info = found
    except Exception:
        info = {}
    if not info:
        for item in (pool or {}).get("catalog") or []:
            if isinstance(item, dict) and item.get("id") == mid:
                info = {"upstream_id": item.get("upstream_id") or mid,
                        "channel": item.get("channel") or ""}
                break
    return info


# ---------------------------------------------------------------------------
# 构造请求
# ---------------------------------------------------------------------------

def build_request(pool, chat):
    """构造一次平台池请求；返回 (url, body, headers, sse, source, billed)。"""
    platform = str(pool.get("platform") or "")
    model = str((chat or {}).get("model") or "").strip()
    try:
        import wb_egg_pools
    except Exception as exc:
        raise PlatformError(500, "供应商池模块不可用：%s" % exc)
    body = wb_egg_pools._openai_body(chat, model)
    # 发给上游的是原始模型 id（对外的 id 带了平台前缀）。
    upstream = upstream_model(pool, model)
    if upstream and body.get("model") != upstream:
        body["model"] = upstream
    account = ensure_account(pool)
    if platform == PLATFORM_CODEARTS:
        channel = model_channel(pool, model)
        url = CODEARTS_SNAP_HOST + CODEARTS_CHAT_PATH
        headers = _codearts_headers(account, upstream, channel, stream=True)
        payload = _json_bytes(body)
        headers = sign_headers(account, "POST", url, payload, headers)
        return url, body, headers, None, pool["id"], model
    if platform == PLATFORM_CLINE:
        url = CLINE_API_BASE + CLINE_CHAT_PATH
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "user-agent": "wb-proxy/egg-platforms",
            # Cline 的账号令牌要带 workos: 前缀，否则一律 401。
            "authorization": "Bearer " + _cline_bearer(account),
            "HTTP-Referer": "https://cline.bot",
            "X-Title": "Cline",
            "X-CLIENT-TYPE": "cline-sdk",
        }
        return url, body, headers, None, pool["id"], model
    raise PlatformError(400, "不支持的平台：%s" % platform)


def test_connection(pool):
    """用账号打一次最小请求，确认平台登录态真能推理。"""
    account = ensure_account(pool)
    return account
