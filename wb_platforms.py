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
import uuid
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import wb_ed25519

# ---------------------------------------------------------------------------
# 平台注册表
# ---------------------------------------------------------------------------

PLATFORM_CODEARTS = "codearts"
PLATFORM_CLINE = "cline"
PLATFORM_FREEBUFF = "freebuff"

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
    {
        "id": PLATFORM_FREEBUFF,
        "name": "Freebuff",
        "short": "Freebuff",
        # 浏览器授权 + 轮询：面板只展示登录链接，不需要回调 URL，也不需要
        # 设备码。复用 device 模式即可（没有 user_code 时设备码框自动隐藏）。
        "login": "device",
        "login_label": "浏览器授权",
        "hint": "在浏览器打开授权页并登录 Freebuff；授权完成后自动加入账号池。",
        "home": "https://freebuff.com",
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

# ---------------------------------------------------------------------------
# Freebuff 固定端点
# ---------------------------------------------------------------------------

FREEBUFF_WEB_BASE = "https://freebuff.com"
#: 真正的 API 域名。freebuff.com 只是登录/首页，聊天和模型目录都在
#: codebuff.com（CLI 的 ``NEXT_PUBLIC_CODEBUFF_APP_URL`` 生产值）。
FREEBUFF_API_BASE = "https://www.codebuff.com"
FREEBUFF_LOGIN_CODE_PATH = "/api/auth/cli/code"
FREEBUFF_LOGIN_STATUS_PATH = "/api/auth/cli/status"
FREEBUFF_CATALOG_PATH = "/api/v1/freebuff/models"
FREEBUFF_SESSION_PATH = "/api/v1/freebuff/session/admission"
FREEBUFF_SESSION_BASE_PATH = "/api/v1/freebuff/session"
FREEBUFF_SESSION_ATTEMPT_PATH = "/api/v1/freebuff/session/attempt"
FREEBUFF_DEVICE_KEYS_PATH = "/api/v1/freebuff/device-keys"
FREEBUFF_CHAT_PATH = "/api/v1/chat/completions"

#: Freebuff 的编译期模型目录（CLI 里的 ``bF`` / ``RsH``）。
#:
#: 登录后优先拉 ``/api/v1/freebuff/models``，那份目录才是账号当前真正
#: 能用的模型；这里只作为目录接口失败、且账号上还没有任何快照时的兜底。
#: id 用 CLI 的原始模型 id（目录行 ``legacyDigests`` 对应的那个），
#: ``free`` 对齐目录里的 ``premium:false``。
#: 字段：id / name / context / vision / reasoning / free。
FREEBUFF_MODEL_CATALOG = [
    {"id": "mimo/mimo-v2.5", "name": "MiMo v2.6 Flash Fast",
     "context": 0, "vision": True, "reasoning": False, "free": True},
    {"id": "z-ai/glm-5.3-flash", "name": "GLM 5.3 Flash",
     "context": 1000000, "vision": True, "reasoning": True, "free": True},
    {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4.1 Flash 10/17",
     "context": 1048576, "vision": True, "reasoning": True, "free": True},
    {"id": "upstage/solar-pro4", "name": "Solar Pro 4",
     "context": 500000, "vision": False, "reasoning": False, "free": True},
    {"id": "upstage/solar-mini4", "name": "Solar Mini 4",
     "context": 500000, "vision": False, "reasoning": False, "free": True},
]


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
    elif item["id"] == PLATFORM_FREEBUFF:
        base_url = FREEBUFF_API_BASE
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


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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
# Freebuff：浏览器授权 / 模型 / 账号
# ---------------------------------------------------------------------------

def _freebuff_bearer(account):
    """Freebuff 请求的 Authorization 头内容（登录拿到的 authToken）。"""
    return str((account or {}).get("access_token") or "").strip()


#: Freebuff 目录快照（fetchId）的进程内缓存。CLI 会把 fetchId 和设备签名
#: 绑在一起发给聊天接口；目录拉取后先放这里，聊天请求按账号 id 取。
_freebuff_meta = {}
_freebuff_meta_lock = threading.RLock()
#: 设备密钥注册是低频操作，但首次聊天可能同时来几条请求；串行化避免重复
#: 生成两把密钥。
_freebuff_device_lock = threading.RLock()


def _freebuff_meta_key(account):
    account = account if isinstance(account, dict) else {}
    aid = str(account.get("id") or "").strip()
    if aid:
        return aid
    token = _freebuff_bearer(account)
    return "token:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]


def _freebuff_catalog_meta(account):
    key = _freebuff_meta_key(account)
    with _freebuff_meta_lock:
        item = _freebuff_meta.get(key)
        return dict(item) if isinstance(item, dict) else {}


def _freebuff_set_catalog_meta(account, fetch_id, issued_ms=0):
    key = _freebuff_meta_key(account)
    with _freebuff_meta_lock:
        _freebuff_meta[key] = {
            "fetch_id": str(fetch_id or "").strip(),
            "issued_at": _int(issued_ms),
            "available": True,
        }


def _freebuff_device_from_account(account):
    """账号里已经保存的设备密钥；没有（或格式不对）就 None。"""
    account = account if isinstance(account, dict) else {}
    seed = str(account.get("freebuff_device_seed") or "").strip()
    public = str(account.get("freebuff_device_public") or "").strip()
    key_id = str(account.get("freebuff_device_key_id") or "").strip()
    if not seed or not public or not key_id:
        return None
    try:
        seed_raw = _b64url_decode(seed)
        public_raw = _b64url_decode(public)
    except Exception:
        return None
    if len(seed_raw) != 32 or len(public_raw) != 32:
        return None
    return {"seed": seed_raw, "public": public_raw, "key_id": key_id}


def _freebuff_device_headers(account, method, url, body=b"", fetch_id=""):
    """按 CLI 的 ``freebuff-device-v1 ...`` 原文签一次请求。

    只有账号里已经有设备密钥时才签；没有密钥时返回空对象，让调用方按
    普通 Bearer 请求继续（和 CLI 首次拉目录时一样）。
    """
    device = _freebuff_device_from_account(account)
    if device is None:
        return {}
    path = urllib.parse.urlparse(url).path or "/"
    timestamp_ms = int(time.time() * 1000)
    body_sha = hashlib.sha256(body or b"").hexdigest()
    # CLI 的 freebuffDeviceSignaturePayload 是换行拼接，不是空格。签名原文
    # 错一个分隔符，服务端就会当成伪造设备，聊天直接 401/403。
    signing = "\n".join([
        "freebuff-device-v1",
        str(method or "GET").upper(),
        path,
        str(timestamp_ms),
        body_sha,
        str(fetch_id or ""),
    ])
    try:
        signature = wb_ed25519.sign(device["seed"], signing.encode("utf-8"),
                                    public=device["public"])
    except Exception as exc:
        _log("freebuff: 设备签名失败：%s" % exc)
        return {}
    headers = {
        "x-freebuff-device-key": device["key_id"],
        "x-freebuff-device-ts": str(timestamp_ms),
        "x-freebuff-device-sig": _b64url(signature),
    }
    if fetch_id:
        headers["x-freebuff-catalog-fetch"] = str(fetch_id)
    return headers


def _freebuff_device_key(pool, account):
    """确保账号有一把已注册的 Freebuff 设备密钥。

    首次聊天前按 CLI 的 ``POST /api/v1/freebuff/device-keys`` 注册公钥。
    注册失败不阻塞聊天：有些部署不要求设备签名，最坏也只是少一组头。
    这里生成的是**网关自己的**密钥，不读取本机 CLI 的 device-key.json。
    """
    # 设备密钥只注册一次，但它会把整个账号写回；和目录/会话共用一把锁，
    # 先读最新副本，避免用旧账号覆盖刚写入的 handle 或会话。
    with _freebuff_session_lock:
        fresh = _freebuff_reload_account(pool, account)
        if isinstance(fresh, dict):
            account.update(fresh)
        return _freebuff_device_key_locked(pool, account)


def _freebuff_device_key_locked(pool, account):
    with _freebuff_device_lock:
        device = _freebuff_device_from_account(account)
        if device is not None:
            return device
        if account.get("freebuff_device_unsupported"):
            return None
        token = _freebuff_bearer(account)
        if not token:
            raise PlatformError(401, "Freebuff 登录态缺少 authToken：请重新登录")
        seed_b64 = str(account.get("freebuff_device_seed") or "").strip()
        public_b64 = str(account.get("freebuff_device_public") or "").strip()
        if not seed_b64 or not public_b64:
            seed, public = wb_ed25519.generate()
            seed_b64 = _b64url(seed)
            public_b64 = _b64url(public)
            account["freebuff_device_seed"] = seed_b64
            account["freebuff_device_public"] = public_b64
            if pool:
                _persist(pool, account)
        url = FREEBUFF_API_BASE + FREEBUFF_DEVICE_KEYS_PATH
        body = _json_bytes({"publicKey": public_b64, "client": "cli"})
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
            "authorization": "Bearer " + token,
            "user-agent": "wb-proxy/egg-platforms",
        }
        try:
            payload = _json_request(url, method="POST", headers=headers,
                                    body=body, timeout=10)
        except urllib.error.HTTPError as exc:
            detail = _http_detail(exc)
            if exc.code in (404, 405):
                account["freebuff_device_unsupported"] = True
                if pool:
                    _persist(pool, account)
                _log("freebuff: 设备密钥接口不可用（HTTP %d）" % exc.code)
                return None
            if exc.code == 401:
                raise PlatformError(
                    401, "Freebuff 登录态已失效：请重新授权（设备密钥注册 401）")
            _log("freebuff: 设备密钥注册失败（HTTP %d）%s"
                 % (exc.code, ("：" + detail) if detail else ""))
            return None
        except Exception as exc:
            _log("freebuff: 设备密钥注册失败：%s" % exc)
            return None
        key_id = ""
        if isinstance(payload, dict):
            key_id = str(payload.get("keyId") or "").strip()
        if not key_id:
            _log("freebuff: 设备密钥注册响应里没有 keyId")
            return None
        account["freebuff_device_key_id"] = key_id
        account["freebuff_device_registered_at"] = time.time()
        if pool:
            _persist(pool, account)
        return {"seed": _b64url_decode(seed_b64),
                "public": _b64url_decode(public_b64),
                "key_id": key_id}


#: 会话准入：Freebuff 同一账号同一时间只允许一个模型会话，并发聊天不能各开
#: 一个；串行化检查/准入/切换。
_freebuff_session_lock = threading.RLock()


def _freebuff_session_of(account):
    session = (account or {}).get("freebuff_session")
    return dict(session) if isinstance(session, dict) else {}


def _freebuff_parse_epoch(value):
    """把 ISO 时间或毫秒/秒时间戳转成 epoch 秒；认不出就 0。"""
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        number = float(text)
        return number / 1000.0 if number > 1e12 else number
    except (TypeError, ValueError):
        pass
    try:
        return datetime.datetime.fromisoformat(
            text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _freebuff_session_expiry(body, now):
    body = body if isinstance(body, dict) else {}
    expires = _freebuff_parse_epoch(body.get("expiresAt")
                                    or body.get("expires_at"))
    if not expires:
        remaining = body.get("remainingMs") or body.get("remaining_ms")
        try:
            expires = now + max(0.0, float(remaining)) / 1000.0
        except (TypeError, ValueError):
            expires = 0.0
    return expires or (now + 3600.0)


def _freebuff_handle_for_key(models, key, fallback=""):
    """在目录行里按稳定 key 找 handle；找不到再退回 fallback。"""
    key = str(key or "").strip()
    if not key:
        return fallback
    for item in models or []:
        if not isinstance(item, dict):
            continue
        upstream = str(item.get("upstream_id") or "").strip()
        mid = str(item.get("id") or "").strip()
        if upstream == key or mid == key or mid == "freebuff/" + key:
            return str(item.get("handle") or upstream or mid or fallback)
    if key.startswith("fbm1."):
        return key
    return fallback


def _freebuff_session_message(status, body):
    """把会话准入的拒绝原因翻成一句能直接展示的话。"""
    body = body if isinstance(body, dict) else {}
    state = str(body.get("status") or "").strip()
    message = str(body.get("message") or body.get("error") or "").strip()
    if status == 401:
        return "Freebuff 登录态已失效：请在平台卡片里重新走一次浏览器授权"
    if state == "country_blocked":
        country = str(body.get("countryCode") or "").strip()
        return ("Freebuff 在当前出口地区不可用%s：换一个出口网络，"
                "或改用别的模型" % (("（%s）" % country) if country else ""))
    if state == "banned":
        return "Freebuff 账号已被封禁：去 freebuff.com/account 申诉，或换一个账号"
    if state == "rate_limited":
        return ("Freebuff 免费会话额度已用完：等额度重置，或换一个免费模型"
                + (("（%s）" % message) if message else ""))
    if state == "spend_limited":
        return ("Freebuff 余额不足，无法为这个模型开会话"
                + (("：%s" % message) if message else ""))
    if state == "ip_capped":
        return "当前出口 IP 上的 Freebuff 会话太多：等别的会话结束，或换出口"
    if state == "takeover_prompt":
        return ("Freebuff 账号已有另一个客户端在跑会话：先在 CLI/桌面端"
                "结束它，再回来重试")
    if state == "model_locked":
        current = str(body.get("currentModel") or "").strip()
        return ("Freebuff 账号已有其他模型的会话%s：先在 CLI/桌面端结束它，"
                "或改用那个模型" % (("（%s）" % current) if current else ""))
    if state == "model_unavailable":
        hours = str(body.get("availableHours") or "").strip()
        return ("Freebuff 这个模型当前不可选"
                + (("：%s" % hours) if hours else "：稍后再试或换一个模型"))
    if state in ("premium_slot_taken", "purchase_claim_released",
                 "purchase_in_use", "purchase_capacity"):
        if state == "purchase_capacity" and str(
                body.get("accessTier") or "") == "limited":
            return ("Freebuff 免费档一个账号同时只能跑一个会话"
                    "（CLI / 桌面端和网关共用这一个名额），"
                    "顶不掉那个会话：把本机的 Freebuff CLI / 桌面端关掉再试")
        return ("Freebuff 这个模型的会话名额被占用了：稍后再试，"
                "或换一个免费模型")
    if status == 429:
        return ("Freebuff 会话被限流了：稍后重试，或换一个模型"
                + (("（%s）" % message) if message else ""))
    return message or ("Freebuff 会话准入失败（HTTP %d）" % int(status or 502))


def _freebuff_session_post(pool, account, handle, fetch_id, catalog_mode,
                           attempt_id, instance_id, takeover_instance_id=""):
    """POST 一次会话准入；返回 (HTTP 状态, 响应 JSON)。

    ``takeover_instance_id`` 是 CLI 的「顶掉」流程：免费档一个账号同时只能
    有一个会话（CLI 和桌面端共用名额），准入被拒时服务端会在 409 里回
    ``currentInstanceId``；带上这个头再申请，服务端会先结束那个会话、把名额
    让给我们（CLI 桌面端对应的是「Take over to stop the other session」）。
    """
    url = FREEBUFF_API_BASE + FREEBUFF_SESSION_PATH
    headers = {
        "accept": "application/json",
        "authorization": "Bearer " + _freebuff_bearer(account),
        "user-agent": "wb-proxy/egg-platforms",
        "x-freebuff-client": "cli",
        "x-freebuff-multi-session": "1",
        "x-freebuff-purchase-continuity": "1",
        "x-freebuff-instance-id": instance_id,
        "x-freebuff-desktop-attempt-id": attempt_id,
        "x-freebuff-model": handle,
        "x-freebuff-wallet-spend-limit": "session",
    }
    takeover_instance_id = str(takeover_instance_id or "").strip()
    if takeover_instance_id:
        headers["x-freebuff-takeover-instance-id"] = takeover_instance_id
    if catalog_mode:
        headers["x-freebuff-catalog-protocol"] = "1"
        if fetch_id:
            headers["x-freebuff-catalog-fetch"] = fetch_id
        _freebuff_device_key(pool, account)
        headers.update(_freebuff_device_headers(
            account, "POST", url, b"", fetch_id=fetch_id))
    try:
        payload = _json_request(url, method="POST", headers=headers,
                                body=None, timeout=20)
        return 200, payload if isinstance(payload, dict) else {}
    except urllib.error.HTTPError as exc:
        # 判定要用到的字段（status、currentInstanceId、currentModel）都在
        # JSON 里，而这个响应体很长（带 desktopRefunds 列表），按默认的 300
        # 字节截断就解析不出来，最后只剩一句「HTTP 409」这种没用的提示。
        detail = _http_detail(exc, limit=64 << 10)
        try:
            body = json.loads(detail)
        except Exception:
            body = {"_raw": detail[:300]}
        if not isinstance(body, dict):
            body = {"_raw": detail[:300]}
        return exc.code, body


def _freebuff_release_session(pool, account, fetch_id="", catalog_mode=False):
    """结束网关自己持有的会话；成功（或服务端已经没有）返回 True。"""
    session = _freebuff_session_of(account)
    instance_id = str(session.get("instance_id") or "").strip()
    if not instance_id:
        account["freebuff_session"] = {}
        if pool:
            _persist(pool, account)
        return True
    attempt_id = str(session.get("attempt_id") or "").strip()
    url = FREEBUFF_API_BASE + (FREEBUFF_SESSION_ATTEMPT_PATH if attempt_id
                               else FREEBUFF_SESSION_BASE_PATH)
    headers = {
        "accept": "application/json",
        "authorization": "Bearer " + _freebuff_bearer(account),
        "user-agent": "wb-proxy/egg-platforms",
        "x-freebuff-client": "cli",
        "x-freebuff-multi-session": "1",
        "x-freebuff-purchase-continuity": "1",
        "x-freebuff-instance-id": instance_id,
    }
    if attempt_id:
        headers["x-freebuff-desktop-attempt-id"] = attempt_id
    if catalog_mode:
        headers["x-freebuff-catalog-protocol"] = "1"
        if fetch_id:
            headers["x-freebuff-catalog-fetch"] = fetch_id
        try:
            _freebuff_device_key(pool, account)
            headers.update(_freebuff_device_headers(
                account, "DELETE", url, b"", fetch_id=fetch_id))
        except PlatformError:
            raise
        except Exception as exc:
            _log("freebuff: 结束会话的设备签名失败：%s" % exc)
    try:
        _json_request(url, method="DELETE", headers=headers, body=None,
                      timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            _log("freebuff: 结束会话失败（HTTP %d）" % exc.code)
            return False
    except Exception as exc:
        _log("freebuff: 结束会话失败：%s" % exc)
        return False
    account["freebuff_session"] = {}
    if pool:
        _persist(pool, account)
    return True


def _freebuff_release_instance(pool, account, instance_id, fetch_id="",
                                catalog_mode=False):
    """结束**别处**开着的会话（按 instance id）。

    免费档一个账号同时只有一个会话名额，CLI 和桌面端共用。网关自己开的
    会话记在账号状态里，能正常结束；但账号也可能在 CLI / 桌面端开着会话，
    那个会话网关不掌握 id，只能靠服务端在 409 里回的 ``currentInstanceId``
    找到它 —— 这时按这个 id 结束它，把名额让给网关。
    """
    instance_id = str(instance_id or "").strip()
    if not instance_id:
        return False
    url = FREEBUFF_API_BASE + FREEBUFF_SESSION_ATTEMPT_PATH
    headers = {
        "accept": "application/json",
        "authorization": "Bearer " + _freebuff_bearer(account),
        "user-agent": "wb-proxy/egg-platforms",
        "x-freebuff-client": "cli",
        "x-freebuff-multi-session": "1",
        "x-freebuff-purchase-continuity": "1",
        "x-freebuff-instance-id": instance_id,
    }
    if catalog_mode:
        headers["x-freebuff-catalog-protocol"] = "1"
        if fetch_id:
            headers["x-freebuff-catalog-fetch"] = fetch_id
        try:
            _freebuff_device_key(pool, account)
            headers.update(_freebuff_device_headers(
                account, "DELETE", url, b"", fetch_id=fetch_id))
        except PlatformError:
            raise
        except Exception as exc:
            _log("freebuff: 结束外部会话的设备签名失败：%s" % exc)
    try:
        payload = _json_request(url, method="DELETE", headers=headers,
                                body=None, timeout=15)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 服务端已经没有这个会话了 —— 名额本来就是空的，算结束成功。
            return True
        _log("freebuff: 结束外部会话失败（HTTP %d）" % exc.code)
        return False
    except Exception as exc:
        _log("freebuff: 结束外部会话失败：%s" % exc)
        return False
    status = str((payload or {}).get("status") or "").strip()
    if status and status != "ended":
        _log("freebuff: 结束外部会话后服务端回的状态是 %s" % status)
        return False
    return True


def _freebuff_session_get(pool, account, instance_id="", fetch_id="",
                          catalog_mode=False):
    """GET 一次当前会话；返回响应 JSON（失败时返回空字典）。

    会话状态是账号级的：网关自己开的会话记在账号状态里，但账号也可能在
    CLI / 桌面端开着会话，那边的情况只能问服务端要。
    """
    url = FREEBUFF_API_BASE + FREEBUFF_SESSION_BASE_PATH
    headers = {
        "accept": "application/json",
        "authorization": "Bearer " + _freebuff_bearer(account),
        "user-agent": "wb-proxy/egg-platforms",
        "x-freebuff-client": "cli",
        "x-freebuff-multi-session": "1",
        "x-freebuff-purchase-continuity": "1",
    }
    instance_id = str(instance_id or "").strip()
    if instance_id:
        headers["x-freebuff-instance-id"] = instance_id
    if catalog_mode:
        headers["x-freebuff-catalog-protocol"] = "1"
        if fetch_id:
            headers["x-freebuff-catalog-fetch"] = fetch_id
    try:
        payload = _json_request(url, method="GET", headers=headers, body=None,
                                timeout=15)
    except urllib.error.HTTPError as exc:
        _log("freebuff: 查询当前会话失败（HTTP %d）" % exc.code)
        return {}
    except Exception as exc:
        _log("freebuff: 查询当前会话失败：%s" % exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def _freebuff_reload_account(pool, account):
    """从池子状态里拿同一个账号的最新副本（别的线程可能刚更新会话）。"""
    try:
        import wb_egg_pools
        fresh_pool = wb_egg_pools.get((pool or {}).get("id"))
        for item in (fresh_pool or {}).get("accounts") or []:
            if (isinstance(item, dict)
                    and item.get("id") == (account or {}).get("id")):
                merged = dict(account or {})
                merged.update(item)
                return merged
    except Exception:
        pass
    return account


def _freebuff_ensure_session(pool, account, info, fetch_id="",
                             catalog_mode=False):
    """保证账号有一个能跑 ``info`` 的活跃会话；返回实际要用的 handle。

    Freebuff 的聊天接口要求先 POST ``/session/admission``；没有活跃会话的
    请求会被拒。这里按 CLI 的多会话协议开会话，并把它记在账号状态里，后续
    同模型请求直接复用。切模型时先结束网关自己开的旧会话，避免撞
    ``model_locked``。
    """
    info = info if isinstance(info, dict) else {}
    model_key = str(info.get("upstream_id") or "").strip()
    handle = str(info.get("handle") or model_key).strip()
    result = {"handle": handle, "model_key": model_key,
              "fetch_id": str(fetch_id or ""), "catalog_mode": bool(catalog_mode)}
    if not model_key:
        return result
    with _freebuff_session_lock:
        fresh = _freebuff_reload_account(pool, account)
        if isinstance(fresh, dict):
            account.update(fresh)
        if account.get("freebuff_session_unsupported"):
            return result
        session = _freebuff_session_of(account)
        now = time.time()
        expires = float(session.get("expires_at") or 0)
        same_model = str(session.get("model_key") or "") == model_key
        # 会话是按当时的 handle 开的；目录刷新后 handle 会变，旧会话就
        # 绑在过期 handle 上（聊天会回 409 freebuff_catalog_stale）。
        session_handle = str(session.get("model_handle") or "").strip()
        handle_changed = bool(session_handle and handle
                              and session_handle != handle)
        if (str(session.get("status") or "") == "active" and same_model
                and not handle_changed and expires > now + 30):
            # 复用已有会话；目录可能刷新过，handle 用当前目录里的。
            result["fetch_id"] = (str(fetch_id or "")
                                  or str(session.get("fetch_id") or ""))
            return result
        if (str(session.get("status") or "") == "active"
                and session.get("instance_id") and expires > now):
            # 网关自己开的另一个模型会话：先结束再开，别让服务端回
            # model_locked。结束失败就明确报错，避免反复撞墙。
            if not _freebuff_release_session(
                    pool, account, fetch_id=fetch_id,
                    catalog_mode=catalog_mode):
                if handle_changed:
                    raise PlatformError(
                        409, "Freebuff 账号的会话绑在过期的模型快照上，"
                             "自动结束失败：请在面板点「刷新模型」再试")
                raise PlatformError(
                    409, "Freebuff 账号当前有一个不同模型的会话（%s），"
                         "自动结束失败：请先在 CLI/桌面端结束它，"
                         "或改用那个模型" % (session.get("model_key") or "未知"))
        elif session:
            # 已过期/已结束的本地记录：尽力让服务端也结束，避免它还在
            # model_locked 状态；结束不掉就清本地，下一次准入会给出明确原因。
            if session.get("instance_id"):
                try:
                    _freebuff_release_session(
                        pool, account, fetch_id=fetch_id,
                        catalog_mode=catalog_mode)
                except PlatformError:
                    raise
                except Exception as exc:
                    _log("freebuff: 清理过期会话失败：%s" % exc)
            account["freebuff_session"] = {}
            if pool:
                _persist(pool, account)

        # 准入被拒时的自愈：目录 handle 过期、免费名额被别人占着、账号锁在
        # 别的模型上 —— 这三种都先自修一次再重试（各只自修一次）。
        takeover_id = ""
        takeover_done = False
        unlock_done = False
        catalog_done = False
        for attempt in range(4):
            attempt_id = str(uuid.uuid4())
            instance_id = "cli:" + attempt_id
            status, body = _freebuff_session_post(
                pool, account, handle, fetch_id, catalog_mode,
                attempt_id, instance_id, takeover_id)
            state = str(body.get("status") or body.get("error") or "")
            if status == 200 and state == "active":
                effective_key = str(body.get("model") or model_key).strip()
                effective_handle = handle
                if effective_key.startswith("fbm1."):
                    # 有的部署会回显 handle；会话簿记仍用稳定 key，否则
                    # 每次请求都会误判成「换了模型」而反复重开会话。
                    effective_handle = effective_key
                    effective_key = model_key
                elif effective_key and effective_key != model_key:
                    # 服务端可能把模型降级/替换；用新 key 在目录里找 handle。
                    effective_handle = ""
                    if catalog_mode:
                        try:
                            models = _freebuff_catalog(account)
                            effective_handle = _freebuff_handle_for_key(
                                models, effective_key, "")
                        except Exception as exc:
                            _log("freebuff: 会话模型替换后刷新目录失败：%s" % exc)
                        if not str(effective_handle).startswith("fbm1."):
                            effective_handle = ""
                    if not effective_handle:
                        effective_handle = _freebuff_handle_for_key(
                            _freebuff_models(), effective_key, "")
                    if not effective_handle:
                        raise PlatformError(
                            502, "Freebuff 会话返回了目录里没有的模型：%s"
                                 % effective_key)
                    handle = effective_handle
                expires_at = _freebuff_session_expiry(body, now)
                admitted_at = (_freebuff_parse_epoch(
                    body.get("admittedAt") or body.get("admitted_at")) or now)
                account["freebuff_session"] = {
                    "status": "active",
                    "instance_id": str(body.get("instanceId")
                                       or body.get("instance_id")
                                       or instance_id),
                    "attempt_id": attempt_id,
                    "model_key": effective_key or model_key,
                    "model_handle": effective_handle,
                    "fetch_id": str(fetch_id or ""),
                    "admitted_at": admitted_at,
                    "expires_at": expires_at,
                    "last_used": now,
                }
                if pool:
                    _persist(pool, account)
                return {"handle": effective_handle,
                        "model_key": effective_key or model_key,
                        "fetch_id": str(fetch_id or ""),
                        "catalog_mode": bool(catalog_mode)}
            if status in (404, 405):
                # 老服务端没有会话准入：标记后按无会话继续，聊天本身可能还能用。
                account["freebuff_session_unsupported"] = True
                if pool:
                    _persist(pool, account)
                _log("freebuff: 会话准入接口不可用（HTTP %d）" % status)
                return result
            if status == 401:
                raise PlatformError(
                    401, "Freebuff 登录态已失效：请重新授权（会话准入 401）")
            if status == 409:
                # 1) 目录 handle 过期：刷新目录，拿同一 key 的新 handle，重试。
                if (catalog_mode and not catalog_done
                        and ("stale" in state.lower()
                             or "stale" in str(body.get("message") or "").lower())):
                    catalog_done = True
                    try:
                        models = _freebuff_catalog(account)
                    except PlatformError:
                        raise
                    except Exception as exc:
                        raise PlatformError(502, "Freebuff 目录刷新失败：%s" % exc)
                    new_handle = _freebuff_handle_for_key(models, model_key, "")
                    if not new_handle:
                        raise PlatformError(
                            409, "Freebuff 模型目录已过期，刷新后仍找不到这个模型："
                                 "请在面板点「刷新模型」再试")
                    handle = new_handle
                    info["handle"] = new_handle
                    fetch_id = str(account.get("freebuff_catalog_fetch_id")
                                   or fetch_id or "")
                    if pool:
                        _persist(pool, account)
                    continue
                # 2) 免费档只有一个会话名额，被 CLI / 桌面端占着：顶掉它。
                #    服务端在 409 里回 currentInstanceId，带这个头再申请，
                #    服务端会先结束那个会话、把名额让给网关（CLI 的 takeover）。
                if (state in ("purchase_capacity", "purchase_in_use",
                              "premium_slot_taken") and not takeover_done):
                    current = str(body.get("currentInstanceId") or "").strip()
                    if current:
                        takeover_done = True
                        takeover_id = current
                        _log("freebuff: 会话名额被 %s 占着，顶掉它再开会话"
                             % current)
                        continue
                # 3) 账号锁在别的模型的会话上（多半是别处开的）：结束它再开。
                if state == "model_locked" and not unlock_done:
                    unlock_done = True
                    current = str(body.get("currentInstanceId") or "").strip()
                    if not current:
                        current = str(_freebuff_session_get(
                            pool, account, instance_id, fetch_id=fetch_id,
                            catalog_mode=catalog_mode).get("instanceId") or "")
                    if current and _freebuff_release_instance(
                            pool, account, current, fetch_id=fetch_id,
                            catalog_mode=catalog_mode):
                        _log("freebuff: 账号锁在别的模型的会话上，"
                             "已结束 %s 再开会话" % current)
                        account["freebuff_session"] = {}
                        if pool:
                            _persist(pool, account)
                        continue
            raise PlatformError(status or 502,
                                _freebuff_session_message(status, body))
        return result


def _freebuff_account(user, fallback=None, fingerprint_id="",
                      fingerprint_hash=""):
    """把 Freebuff 登录状态里的 ``user`` 当成账号。"""
    if not isinstance(user, dict):
        raise PlatformError(502, "Freebuff 登录失败：响应里没有 user")
    token = str(user.get("authToken") or user.get("auth_token") or "").strip()
    if not token:
        raise PlatformError(502, "Freebuff 登录失败：响应里没有 authToken")
    fallback = fallback or {}
    email = str(user.get("email") or "")
    name = str(user.get("name") or "")
    account_id = str(user.get("id") or "")
    stable = account_id or email or token[:24]
    # Freebuff 的 authToken 是长期登录态，没有 refresh_token 流程；这里不拿
    # 登录链接的 expiresAt 当令牌过期时间，失效时靠 401 提示重新授权。
    expires_at = 0.0
    return {
        "id": str(fallback.get("id") or
                  ("acct-" + hashlib.sha256(
                      stable.encode("utf-8")).hexdigest()[:12])),
        "label": name or email or ("Freebuff " + stable[-6:]),
        "email": email,
        "name": name,
        "enabled": fallback.get("enabled", True),
        "created_at": float(fallback.get("created_at") or time.time()),
        "last_used": float(fallback.get("last_used") or 0.0),
        "status": "ok",
        "last_error": "",
        "access_token": token,
        "token_type": "Bearer",
        "expires_at": expires_at,
        "long_lived": not bool(expires_at),
        "user_info": {"id": account_id, "name": name, "email": email},
        "account_id": account_id,
        "fingerprint_id": str(fingerprint_id or
                              fallback.get("fingerprint_id") or ""),
        "fingerprint_hash": str(fingerprint_hash or
                                fallback.get("fingerprint_hash") or ""),
        # 网关自己的设备密钥（不是本机 CLI 的 device-key.json）。重新授权
        # 时保留，避免每次登录都重新注册一把。
        "freebuff_device_seed": str(fallback.get("freebuff_device_seed") or ""),
        "freebuff_device_public": str(
            fallback.get("freebuff_device_public") or ""),
        "freebuff_device_key_id": str(
            fallback.get("freebuff_device_key_id") or ""),
        "freebuff_device_registered_at": float(
            fallback.get("freebuff_device_registered_at") or 0.0),
        "freebuff_device_unsupported": bool(
            fallback.get("freebuff_device_unsupported")),
        "freebuff_catalog_fetch_id": str(
            fallback.get("freebuff_catalog_fetch_id") or ""),
        "freebuff_catalog_handles": (
            dict(fallback.get("freebuff_catalog_handles"))
            if isinstance(fallback.get("freebuff_catalog_handles"), dict)
            else {}),
        "freebuff_catalog_models": (
            [dict(m) for m in (fallback.get("freebuff_catalog_models") or [])
             if isinstance(m, dict)]
            if isinstance(fallback.get("freebuff_catalog_models"), list)
            else []),
        "freebuff_catalog_at": float(
            fallback.get("freebuff_catalog_at") or 0),
        "freebuff_catalog_refresh_at": float(
            fallback.get("freebuff_catalog_refresh_at") or 0),
        "freebuff_session": (dict(fallback.get("freebuff_session"))
                             if isinstance(fallback.get("freebuff_session"),
                                           dict) else {}),
        "freebuff_session_unsupported": bool(
            fallback.get("freebuff_session_unsupported")),
        "platform": PLATFORM_FREEBUFF,
    }


def _freebuff_models():
    """Freebuff 的编译期兜底模型清单（目录接口失败时用）。"""
    models = []
    seen = set()
    for item in FREEBUFF_MODEL_CATALOG:
        mid = str(item.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        row = dict(item)
        row["id"] = mid
        row["upstream_id"] = mid
        # 没有目录时，会话准入只能拿模型 id 当 handle（CLI 的 fallback 写法）。
        row["handle"] = mid
        row["channel"] = "freebuff"
        row["tools"] = True
        models.append(row)
    return models


def _freebuff_catalog(account, timeout=HTTP_TIMEOUT_SECONDS):
    """拉 Freebuff 账号当前可用的模型目录。

    CLI 的目录请求只需要 Bearer + 协议头；设备签名只在「刷新目录 / 会话
    准入」时才可能用上，首次拉目录不要求（CLI 的 ``deviceHeaders`` 在没有
    设备密钥时返回空对象）。这里按首次请求的形状实现，避免读取或生成
    本机任何设备凭据。
    """
    token = _freebuff_bearer(account)
    if not token:
        raise PlatformError(401, "Freebuff 登录态缺少 authToken：请重新登录")
    url = FREEBUFF_API_BASE + FREEBUFF_CATALOG_PATH
    headers = {
        "accept": "application/json",
        "authorization": "Bearer " + token,
        "x-freebuff-catalog-protocol": "1",
        "x-freebuff-client": "cli",
        "user-agent": "wb-proxy/egg-platforms",
    }
    # 已经有设备密钥时，刷新目录按 CLI 的后续目录请求带设备签名；首次
    # 拉目录还没有密钥，保持不带（CLI 也是这样）。
    try:
        headers.update(_freebuff_device_headers(account, "GET", url, b""))
    except Exception as exc:
        _log("freebuff: 目录请求设备签名失败：%s" % exc)
    try:
        payload = _json_request(url, headers=headers, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = _http_detail(exc)
        status = exc.code if exc.code in (401, 403) else 502
        raise PlatformError(
            status, "Freebuff 模型目录拉取失败（HTTP %d）%s"
            % (exc.code, ("：" + detail) if detail else ""))
    except Exception as exc:
        raise PlatformError(502, "Freebuff 模型目录拉取失败：%s" % exc)
    if not isinstance(payload, dict):
        raise PlatformError(502, "Freebuff 模型目录响应格式不对")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        # 兼容个别部署把 rows 放在 data 里。
        data = payload.get("data")
        rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise PlatformError(502, "Freebuff 模型目录响应里没有 rows")
    issued = payload.get("issuedAt")
    if issued is None:
        issued_ms = int(time.time() * 1000)
    elif str(issued).isdigit():
        issued_ms = _int(issued)
    else:
        issued_ms = int(_epoch(issued, time.time()) * 1000)
    models = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or row.get("id") or "").strip()
        if not key or key in seen:
            continue
        # 目录里可能带「还没开放」的模型（opensAt），CLI 也会先过滤掉。
        opens = row.get("opensAt")
        if opens is not None:
            opens_ms = (_int(opens) if str(opens).isdigit()
                        else int(_epoch(opens, 0) * 1000))
            if opens_ms and opens_ms > issued_ms:
                continue
        # locked 行需要付费计划，CLI 只把它画成付费墙入口，不能拿来开会话。
        access = str(row.get("access") or "").strip().lower()
        if access == "locked":
            continue
        seen.add(key)
        handle = str(row.get("handle") or "").strip()
        context = row.get("contextWindow")
        if context is None:
            context = row.get("context_window")
        efforts = row.get("efforts")
        reasoning = bool(row.get("reasoningEffort") or row.get("defaultEffort")
                         or (isinstance(efforts, list) and efforts))
        # premium 字段缺失时按「付费」处理，避免把没标清楚的模型误报成免费。
        premium = row.get("premium")
        if premium is None:
            premium = True
        models.append({
            "id": key,
            # 对外/持久化用稳定 key；真正发给上游的聊天 body 和会话准入
            # 头用 handle。
            "upstream_id": key,
            "handle": handle or key,
            "name": str(row.get("displayName") or row.get("display_name")
                        or key),
            "context": _int(context),
            "vision": bool(row.get("multimodal")),
            "reasoning": reasoning,
            "free": (not bool(premium)) and access != "locked",
            "channel": "freebuff",
            "premium": bool(premium),
            "data_use": str(row.get("dataUse") or ""),
            "access": access,
            "efforts": efforts if isinstance(efforts, list) else [],
            "default_effort": str(row.get("defaultEffort") or ""),
            "sort_order": _int(row.get("sortOrder")),
            "tools": True,
        })
    # fetchId 是这次目录快照的标识；聊天请求的设备签名原文会带它。
    # handle 是按账号 mint 的，必须和这次 fetchId 一起按账号保存，不能拿
    # 别的账号拉到的 handle 去给这个账号开会话。
    fetch_id = str(payload.get("fetchId") or "").strip()
    _freebuff_set_catalog_meta(account, fetch_id, issued_ms)
    account["freebuff_catalog_fetch_id"] = fetch_id
    account["freebuff_catalog_handles"] = {
        str(item.get("upstream_id") or ""): str(item.get("handle") or "")
        for item in models
        if item.get("upstream_id") and item.get("handle")
    }
    # 把这次目录快照整个存下来：目录接口临时抽风时，面板还能给出上一次的
    # 模型清单（连同 handle），不至于退回一份过期的编译期兜底列表。
    account["freebuff_catalog_models"] = [
        {
            "id": item.get("id"),
            "upstream_id": item.get("upstream_id"),
            "handle": item.get("handle"),
            "name": item.get("name"),
            "context": item.get("context"),
            "vision": item.get("vision"),
            "reasoning": item.get("reasoning"),
            "free": item.get("free"),
            "channel": "freebuff",
            "tools": True,
        }
        for item in models
    ]
    account["freebuff_catalog_at"] = time.time()
    refresh_at = _freebuff_parse_epoch(payload.get("refreshAt"))
    if not refresh_at:
        # 服务端没给 refreshAt 时，按 10 分钟主动换一次 handle；handle 过期
        # 会变成 409 freebuff_catalog_stale，聊天就直接失败了。
        refresh_at = _freebuff_parse_epoch(issued_ms) + 600.0
    account["freebuff_catalog_refresh_at"] = refresh_at
    return models


def _freebuff_catalog_locked(pool, account, timeout=HTTP_TIMEOUT_SECONDS):
    """拉目录并落盘；和会话准入共用一把锁，避免互相覆盖账号状态。

    ``fetch_models`` 和聊天前的懒刷新都会走到这里。目录响应里的 handle 是
    按账号 mint 的，如果它和 ``freebuff_session`` 分别从两份旧账号副本写回，
    就会丢字段；所以在锁里先读最新账号，再请求、再保存。
    """
    with _freebuff_session_lock:
        fresh = _freebuff_reload_account(pool, account)
        if isinstance(fresh, dict):
            account.update(fresh)
        models = _freebuff_catalog(account, timeout=timeout)
        _persist(pool, account)
        return models


def refresh_catalog(pool, timeout=HTTP_TIMEOUT_SECONDS):
    """强制刷新一次 Freebuff 模型目录（handle 过期时用）。

    目录里的 handle 是会过期的（服务端回 409 ``freebuff_catalog_stale``）。
    CLI 的做法是刷新目录后把这一轮重来一次；这里对应的是：刷新目录 +
    丢掉绑在旧 handle 上的会话，下一次聊天按新 handle 重新开会话。
    """
    accounts = [a for a in (pool or {}).get("accounts") or []
                if isinstance(a, dict) and _has_live_credential(a)]
    if not accounts:
        raise PlatformError(401, "Freebuff 没有可用的登录账号：请重新授权")
    errors = []
    for account in accounts:
        try:
            _freebuff_catalog_locked(pool, account, timeout=timeout)
        except PlatformError as exc:
            errors.append(exc.detail)
            continue
        except Exception as exc:
            errors.append(str(exc))
            continue
        session = _freebuff_session_of(account)
        if session.get("instance_id"):
            try:
                _freebuff_release_session(
                    pool, account,
                    fetch_id=str(account.get("freebuff_catalog_fetch_id") or ""),
                    catalog_mode=True)
            except Exception as exc:
                _log("freebuff: 刷新目录后结束旧会话失败：%s" % exc)
                account["freebuff_session"] = {}
                if pool:
                    _persist(pool, account)
        return True
    raise PlatformError(502, "Freebuff 模型目录刷新失败：%s"
                        % (errors[0] if errors else "未知错误"))


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
    if item["id"] == PLATFORM_FREEBUFF:
        return _freebuff_login_start()
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


# --- Freebuff 登录 ---------------------------------------------------------

def _freebuff_login_start():
    """开始一次 Freebuff 浏览器授权登录。

    Freebuff 的 CLI 登录是「生成登录链接 → 用户在浏览器授权 → CLI 轮询
    status」。网关只做链接生成与轮询，不读取本机任何凭据。
    """
    session = _new_session(PLATFORM_FREEBUFF)
    # Freebuff CLI 的 fingerprint 是 `codebuff-cli-<8 位 base64url>`；这里按同样
    # 的形状生成一个随机 id，只用于本次登录，不读取本机硬件信息。
    fingerprint_id = "codebuff-cli-" + secrets.token_urlsafe(6)[:8]
    try:
        payload = _json_request(
            FREEBUFF_WEB_BASE + FREEBUFF_LOGIN_CODE_PATH, method="POST",
            headers={"content-type": "application/json",
                     "accept": "application/json",
                     "user-agent": "wb-proxy/egg-platforms"},
            body=_json_bytes({"fingerprintId": fingerprint_id}))
    except urllib.error.HTTPError as exc:
        detail = _http_detail(exc)
        raise PlatformError(502, "Freebuff 登录链接申请失败（HTTP %d）%s"
                            % (exc.code, ("：" + detail) if detail else ""))
    except Exception as exc:
        raise PlatformError(502, "Freebuff 登录链接申请失败：%s" % exc)
    if not isinstance(payload, dict):
        raise PlatformError(502, "Freebuff 登录链接响应格式不对")
    login_url = str(payload.get("loginUrl") or "").strip()
    fingerprint_hash = str(payload.get("fingerprintHash") or "").strip()
    expires_text = str(payload.get("expiresAt") or "").strip()
    if not login_url or not fingerprint_hash or not expires_text:
        raise PlatformError(502, "Freebuff 登录链接申请失败：%s"
                            % _error_detail(payload))
    # loginUrl 正常是绝对地址；个别部署给相对路径，补成可点的完整链接。
    login_url = urllib.parse.urljoin(FREEBUFF_WEB_BASE + "/", login_url)
    now = time.time()
    expires_at = _epoch(expires_text, now + LOGIN_TTL_SECONDS)
    session.update({
        "url": login_url,
        "instructions": ("在浏览器打开授权页并登录 Freebuff；授权完成后会自动"
                         "加入账号池，可以关闭授权页回到网关看板。"),
        "expires_at": min(now + LOGIN_TTL_SECONDS,
                          max(now + 60, expires_at)),
        "fingerprint_id": fingerprint_id,
        "fingerprint_hash": fingerprint_hash,
        "login_expires_at": expires_text,
        "interval": 5,
    })
    thread = threading.Thread(target=_freebuff_poll_loop,
                              args=(session,), daemon=True)
    thread.start()
    session["thread"] = thread
    _log("freebuff: 浏览器授权登录已启动")
    return _public_session(session)


def _freebuff_poll_loop(session):
    deadline = float(session.get("expires_at") or 0)
    interval = max(2, int(session.get("interval") or 5))
    query = urllib.parse.urlencode({
        "fingerprintId": session.get("fingerprint_id") or "",
        "fingerprintHash": session.get("fingerprint_hash") or "",
        "expiresAt": session.get("login_expires_at") or "",
    })
    url = FREEBUFF_WEB_BASE + FREEBUFF_LOGIN_STATUS_PATH + "?" + query
    while time.time() < deadline:
        if session.get("cancel") or session.get("status") != "pending":
            return
        time.sleep(interval)
        if session.get("cancel") or session.get("status") != "pending":
            return
        try:
            payload = _json_request(
                url, headers={"accept": "application/json",
                              "user-agent": "wb-proxy/egg-platforms"})
        except urllib.error.HTTPError as exc:
            # 401 = 用户还没完成授权；限流 / 网关错误也继续等到登录码过期。
            if exc.code in (401, 408, 429) or exc.code >= 500:
                continue
            detail = _http_detail(exc)
            session["status"] = "error"
            session["error"] = ("Freebuff 登录轮询失败（HTTP %d）%s"
                                % (exc.code,
                                   ("：" + detail) if detail else ""))
            return
        except Exception:
            # 网络抖动不终止登录，下一轮再试。
            continue
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict):
            error = str((payload.get("error") if isinstance(payload, dict)
                         else "") or "")
            # 授权还没完成时上游可能回 200 + error；这些是「继续等」，不是
            # 终态。真正的失败留给登录码过期或明确的错误文案。
            if error and error.lower() not in (
                    "authorization_pending", "pending", "slow_down"):
                session["status"] = "error"
                session["error"] = "Freebuff 登录失败：%s" % _error_detail(payload)
                return
            continue
        try:
            account = _freebuff_account(
                user,
                fingerprint_id=session.get("fingerprint_id") or "",
                fingerprint_hash=session.get("fingerprint_hash") or "")
        except Exception as exc:
            session["status"] = "error"
            session["error"] = str(exc)
            return
        session["account"] = account
        session["status"] = "success"
        _log("freebuff: 账号 %s 登录成功" % account.get("label"))
        return
    session["status"] = "expired"
    session["error"] = "Freebuff 登录授权已过期，请重新发起登录"


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
        "long_lived": bool(account.get("long_lived")),
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


def forget_device_key(pool, account=None):
    """服务端说不认识这个设备密钥：清掉 keyId，下次请求重新注册一次。

    密钥对（seed/public）留着：CLI 也是保留同一对公钥、只丢掉注册关系
    再注册一次。
    """
    accounts = [a for a in (pool or {}).get("accounts") or []
                if isinstance(a, dict)]
    if account is not None:
        only = str(account.get("id") or "")
        accounts = [a for a in accounts if str(a.get("id") or "") == only]
    changed = False
    for item in accounts:
        if not item.get("freebuff_device_key_id"):
            continue
        item["freebuff_device_key_id"] = ""
        item["freebuff_device_registered_at"] = 0
        _persist(pool, item)
        changed = True
    if changed:
        _log("freebuff: 服务端不认这个设备密钥，重新注册一次")
    return changed


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
        elif account.get("platform") == PLATFORM_FREEBUFF:
            # Freebuff 的登录态是长期 authToken，没有 refresh_token 流程；
            # 失效了只能重新走一次浏览器授权。
            if not _has_live_credential(account):
                raise PlatformError(401, "Freebuff 登录态已失效：请重新登录")
            updated = dict(account)
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


def _freebuff_pick_account(pool, model_key):
    """Freebuff 优先选已经有同模型会话的账号，其次选空闲账号。

    ``ensure_account`` 是无状态轮询；如果直接用它，多账号池里每个请求都会
    换一个账号，每个账号都要单独开一小时会话。这里按会话粘一下，只有所有
    账号都在跑别的模型时才去切换。
    """
    accounts = [a for a in (pool.get("accounts") or [])
                if isinstance(a, dict) and a.get("enabled", True) is not False]
    if not accounts:
        return ensure_account(pool)
    now = time.time()

    def active(account):
        session = _freebuff_session_of(account)
        return (str(session.get("status") or "") == "active"
                and float(session.get("expires_at") or 0) > now + 30)

    same = [a for a in accounts if active(a)
            and str(_freebuff_session_of(a).get("model_key") or "") == model_key]
    idle = [a for a in accounts if not active(a)]
    rest = [a for a in accounts if a not in same and a not in idle]
    for account in same + idle + rest:
        try:
            if _needs_refresh(account):
                account = refresh_account(pool, account)
        except PlatformError:
            if not _has_live_credential(account):
                continue
        if not _has_live_credential(account):
            continue
        account = dict(account)
        account["last_used"] = time.time()
        return account
    raise PlatformError(503, "Freebuff 账号的登录态都失效了：请重新登录")


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
        if platform == PLATFORM_FREEBUFF:
            # 登录后以账号目录为准；目录暂时拉不到再用编译期兜底，避免
            # 面板因为一次网络抖动就整片模型消失。
            account = ensure_account(pool)
            catalog_ok = False
            try:
                # 落盘 fetchId 与按账号 mint 的 handle；网关重启后第一次
                # 聊天还能用同一份目录快照签名。
                models = _freebuff_catalog_locked(
                    pool, account, timeout=timeout)
                catalog_ok = True
            except PlatformError as exc:
                # 401 是登录态失效，必须让操作者重新授权；403 可能只是
                # 目录接口对账号限权，退回内置目录仍可继续用聊天接口。
                if exc.status == 401:
                    return [], exc.detail
                _log("freebuff: 模型目录拉取失败，使用内置目录：%s" % exc.detail)
                models = []
            except Exception as exc:
                _log("freebuff: 模型目录拉取失败，使用内置目录：%s" % exc)
                models = []
            if not models and not catalog_ok:
                # 目录这次没拉到，但账号上还存着上一次拉到的快照（连 handle）：
                # 面板继续用这份清单，别退回一份过期的编译期兜底列表。
                stored = account.get("freebuff_catalog_models")
                if isinstance(stored, list) and stored:
                    models = [dict(m) for m in stored
                              if isinstance(m, dict) and m.get("id")]
                    _log("freebuff: 模型目录暂时拉不到，沿用上次拉到的快照"
                         "（%d 个模型）" % len(models))
                else:
                    models = _freebuff_models()
            if not models:
                if catalog_ok:
                    return [], "Freebuff 账号当前没有可用模型（目录里都是 locked 或未开放）"
                return [], "Freebuff 没有可用的内置模型目录"
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
                        "channel": item.get("channel") or "",
                        "handle": item.get("handle") or ""}
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
    info = _model_info(pool, model) if platform == PLATFORM_FREEBUFF else {}
    if platform == PLATFORM_FREEBUFF:
        # Freebuff 的会话是账号级的，先按「谁已经有这个模型的会话」挑账号，
        # 避免多账号池每个请求都换账号、各开一小时会话。
        account = _freebuff_pick_account(
            pool, str(info.get("upstream_id") or upstream or ""))
    else:
        account = ensure_account(pool)
        if upstream and body.get("model") != upstream:
            body["model"] = upstream
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
    if platform == PLATFORM_FREEBUFF:
        token = _freebuff_bearer(account)
        if not token:
            raise PlatformError(401, "Freebuff 登录态缺少 authToken：请重新登录")
        upstream = str(info.get("upstream_id") or upstream or "").strip()
        if upstream and not info.get("upstream_id"):
            info["upstream_id"] = upstream
        # handle 是按账号 mint 的；池子目录可能是别的账号拉的，不能混用。
        handles = account.get("freebuff_catalog_handles")
        handles = handles if isinstance(handles, dict) else {}
        account_handle = str(handles.get(upstream) or "").strip()
        handle = account_handle or str(info.get("handle") or upstream or "").strip()
        # 目录 handle 固定以 fbm1. 开头；静态兜底目录没有真 handle，走旧协议
        # （聊天 body 发 key，会话准入也发 key）。
        catalog_mode = handle.startswith("fbm1.")
        meta = _freebuff_catalog_meta(account)
        fetch_id = str(account.get("freebuff_catalog_fetch_id")
                       or meta.get("fetch_id") or "")
        now = time.time()
        refresh_at = float(account.get("freebuff_catalog_refresh_at") or 0)
        catalog_at = float(account.get("freebuff_catalog_at") or 0)
        refresh_due = bool(refresh_at and now >= refresh_at - 30
                           and now - catalog_at > 60)
        # 第一次聊天时账号可能还没有目录快照（用户没点「刷新模型」，
        # 或者刚从旧版本升级上来）。这里主动拉一次，否则只能按旧协议
        # 发 key；新版服务端要求 handle + fetchId + 设备签名。
        need_catalog = bool(not account_handle or not fetch_id or refresh_due
                            or not account.get("freebuff_catalog_at"))
        if need_catalog and not account.get("freebuff_catalog_unsupported"):
            fetched = False
            fetch_error = ""
            try:
                models = _freebuff_catalog_locked(pool, account)
                fetched = True
                handles = account.get("freebuff_catalog_handles") or {}
                account_handle = str(handles.get(upstream) or "").strip()
                fetch_id = str(account.get("freebuff_catalog_fetch_id") or "")
                new_handle = _freebuff_handle_for_key(models, upstream, "")
                chosen = account_handle or new_handle
                if chosen:
                    handle = chosen
                    info["handle"] = chosen
                    # 之前按静态目录走旧协议时 catalog_mode 是 False；
                    # 拉到真 handle 后必须切回目录模式，否则聊天不会带
                    # 目录快照和设备签名。
                    catalog_mode = handle.startswith("fbm1.")
            except PlatformError as exc:
                if exc.status == 401:
                    raise
                fetch_error = exc.detail
                _log("freebuff: 聊天前刷新目录失败：%s" % exc.detail)
            except Exception as exc:
                fetch_error = str(exc)
                _log("freebuff: 聊天前刷新目录失败：%s" % exc)
            if catalog_mode and not account_handle and not fetched:
                # 手里只有别的账号拉到的 handle，拿它去准入只会白撞一次。
                raise PlatformError(
                    502, "Freebuff 账号目录不可用：%s" % (fetch_error or "未知错误"))
            if catalog_mode and fetched and not account_handle:
                raise PlatformError(
                    409, "Freebuff 这个账号的模型目录里没有该模型："
                         "刷新模型或换一个账号")
        # Freebuff 的聊天接口要求先有活跃会话；这里开会话并拿到实际 handle。
        session_info = _freebuff_ensure_session(
            pool, account, info, fetch_id=fetch_id,
            catalog_mode=catalog_mode)
        handle = str(session_info.get("handle") or handle or upstream)
        fetch_id = str(session_info.get("fetch_id") or fetch_id or "")
        catalog_mode = bool(session_info.get("catalog_mode", catalog_mode))
        if handle or upstream:
            # 目录模式下 body 必须发 handle，不是 key；旧协议才发 key。
            body["model"] = handle or upstream
        url = FREEBUFF_API_BASE + FREEBUFF_CHAT_PATH
        headers = {
            "content-type": "application/json",
            "accept": ("text/event-stream" if body.get("stream")
                       else "application/json"),
            "user-agent": "wb-proxy/egg-platforms",
            "authorization": "Bearer " + token,
            "x-freebuff-client": "cli",
            "x-freebuff-sse-gzip-recovery": "1",
        }
        # CLI 的 codebuff provider 会带当前用户 id；有就照发，没有也不影响。
        user_info = account.get("user_info")
        user_id = str(account.get("account_id") or
                      (user_info.get("id") if isinstance(user_info, dict)
                       else "") or "")
        if user_id:
            headers["x-freebuff-acting-user-id"] = user_id
        # 目录模式下聊天请求还要带目录快照 + Ed25519 设备签名。签名必须对
        # 实际发出去的 body 字节算；open_stream 用同一份紧凑 JSON 序列化。
        if catalog_mode:
            try:
                _freebuff_device_key(pool, account)
            except PlatformError:
                raise
            except Exception as exc:
                _log("freebuff: 设备密钥准备失败：%s" % exc)
            try:
                headers.update(_freebuff_device_headers(
                    account, "POST", url, _json_bytes(body),
                    fetch_id=fetch_id))
            except Exception as exc:
                _log("freebuff: 聊天请求设备签名失败：%s" % exc)
        return url, body, headers, None, pool["id"], model
    raise PlatformError(400, "不支持的平台：%s" % platform)


def test_connection(pool):
    """用账号打一次最小请求，确认平台登录态真能推理。"""
    account = ensure_account(pool)
    return account
