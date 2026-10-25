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
import getpass
import hashlib
import hmac
import json
import locale
import os
import platform as _platform
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 平台注册表
# ---------------------------------------------------------------------------

PLATFORM_CODEARTS = "codearts"
PLATFORM_CLINE = "cline"
PLATFORM_ZCODE = "zcode"

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
        "id": PLATFORM_ZCODE,
        "name": "ZCode",
        "short": "ZCode",
        "protocol": "anthropic",
        "login": "zcode",
        "login_label": "登录 / API Key",
        "hint": ("Z.ai 或智谱 BigModel 的 ZCode（GLM Coding Plan）账号：浏览器登录、"
                 "导入本机 ZCode 登录态，或粘贴 Coding Plan API Key。"),
        "home": "https://zcode.z.ai",
        "balance": True,
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
# ZCode 固定端点（与 ZCode 3.16.3 / opencode-zcode-auth 对齐）
# ---------------------------------------------------------------------------

#: ZCode 自己的站（登录流程、Start Plan 请求、设备号都在这边）。
ZCODE_BASE = "https://zcode.z.ai"
#: 两个站的业务 API（账号信息 / 项目 Key / 套餐）。
ZCODE_ZAI_API = "https://api.z.ai"
ZCODE_BIGMODEL_API = "https://bigmodel.cn"
#: 两个站的 GLM Coding Plan 推理端点（Anthropic Messages 协议）。
ZCODE_ZAI_BASE = "https://api.z.ai/api/anthropic"
ZCODE_BIGMODEL_BASE = "https://open.bigmodel.cn/api/anthropic"
#: ZCode 的赠送额度（Start Plan）由 zcode.z.ai 自己服务。
ZCODE_START_BASE = ZCODE_BASE + "/api/v1/zcode-plan/anthropic"

ZCODE_APP_VERSION = "3.16.3"
ZCODE_ANTHROPIC_VERSION = "2023-06-01"

ZCODE_SITES = {
    "zai": {
        "name": "Z.ai",
        "api": ZCODE_ZAI_API,
        "base": ZCODE_ZAI_BASE,
        "subscribe": "https://z.ai/subscribe",
    },
    "bigmodel": {
        "name": "智谱 BigModel",
        "api": ZCODE_BIGMODEL_API,
        "base": ZCODE_BIGMODEL_BASE,
        "subscribe": "https://bigmodel.cn/glm-coding",
    },
}

#: ZCode 的 Coding Plan 模型（插件里的兜底表；ZCode 的远端配置拿不到时用它）。
ZCODE_MODELS = [
    {"id": "GLM-5.3", "context": 1000000, "output": 128000,
     "reasoning": True, "vision": False},
    {"id": "GLM-5.3-Flash", "context": 1000000, "output": 128000,
     "reasoning": True, "vision": False},
    {"id": "GLM-5.2", "context": 1000000, "output": 128000,
     "reasoning": True, "vision": False},
    {"id": "GLM-5-Turbo", "context": 200000, "output": 64000,
     "reasoning": True, "vision": False},
]

#: 登录方式：browser（ZCode 的 OAuth CLI 流程）、import（本机 ZCode 登录态）、
#: key（GLM Coding Plan API Key）。
ZCODE_LOGIN_MODES = ("browser", "import", "key")


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
    elif item["id"] == PLATFORM_ZCODE:
        base_url = ZCODE_ZAI_BASE
    else:
        base_url = CLINE_API_BASE
    return {
        "kind": "platform",
        "platform": item["id"],
        "name": item["name"],
        "protocol": item.get("protocol") or "openai",
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
# ZCode：登录 / 续期 / 模型 / 请求
# ---------------------------------------------------------------------------
#
# ZCode（zcode.z.ai）是智谱 GLM Coding Plan 的客户端。账号可能落在两个站点：
# Z.ai（api.z.ai）或智谱 BigModel（bigmodel.cn）。它有三种可用的登录态：
#
# * 浏览器登录：ZCode 的 OAuth CLI 流程（zcode.z.ai 开一个授权页，后台轮询）；
# * 本机 ZCode 登录态：~/.zcode/v2/credentials.json（加密，AES-256-GCM）；
# * GLM Coding Plan API Key（形如 <id>.<secret>）。
#
# 有 Coding Plan 的账号用 ``api.z.ai/api/anthropic`` 或
# ``open.bigmodel.cn/api/anthropic``；只有 ZCode 赠送额度（Start Plan /
# ZCode 1024 Dev Day）的账号走 ``zcode.z.ai/api/v1/zcode-plan/anthropic``，
# 请求必须带 ZCode 自己的系统提示词与请求头，否则一律 405/3012。

_ZCODE_DEVICE_LOCK = threading.RLock()
_ZCODE_DEVICE = {"id": "", "path": ""}


def _zcode_site(value):
    text = str(value or "").strip().lower()
    if text in ("bigmodel", "big-model", "bigmodel.cn", "zhipu", "cn",
                "智谱", "智谱 bigmodel", "智谱bigmodel"):
        return "bigmodel"
    return "zai"


def _zcode_site_info(site):
    return dict(ZCODE_SITES[_zcode_site(site)])


def _zcode_root(base):
    try:
        parts = urllib.parse.urlsplit(str(base or ""))
        if parts.scheme and parts.netloc:
            return "%s://%s" % (parts.scheme, parts.netloc)
    except Exception:
        pass
    return ZCODE_ZAI_API


def _zcode_num(value):
    """ZCode 的金额 / 时间有时是数字，有时是字符串。"""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value or "").strip())
    except (TypeError, ValueError):
        return 0.0


def _zcode_model_name(name):
    """把赠送额度里的 ``glm-5.3-flash`` 归一成目录里的 ``GLM-5.3-Flash``。"""
    text = str(name or "").strip()
    for model in ZCODE_MODELS:
        if model["id"].lower() == text.lower():
            return model["id"]
    return text


# ---------------------------------------------------------------------------
# ZCode 官方模型清单（provider 配置）
# ---------------------------------------------------------------------------
#
# ZCode 自己的模型目录（哪些模型、上下文、输出上限、推理档位）不在推理
# 端点里，而在它的客户端配置里：zcode.z.ai 指名的 builtin_provider_config
# 或者本机安装的 ZCode 自带的 zcode-builtin.json（取 revision 最新的）。
# 「刷新模型」时强制重拉远程那份，拿不到就用本机 / 上次的 / 内置兜底表。

_ZCODE_CONFIG_CACHE = {"cfg": None, "at": 0.0}
_ZCODE_CONFIG_LOCK = threading.RLock()
ZCODE_CONFIG_TTL_SECONDS = 10 * 60


def _zcode_local_config():
    """本机 ZCode 客户端带的官方 provider 配置（revision 最大的那份）。"""
    try:
        paths = _zcode_local_paths()
        root = os.path.join(paths["data_dir"], ".zcode", "v2", "runtime",
                            "provider")
        best = None
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep) if dirpath.startswith(root) else 99
            if depth > 3:
                dirnames[:] = []
                continue
            if depth == 3 and "zcode-builtin.json" in filenames:
                try:
                    with open(os.path.join(dirpath, "zcode-builtin.json"),
                              "r", encoding="utf-8") as fh:
                        cfg = json.load(fh)
                    if isinstance(cfg, dict) and (
                            best is None
                            or (cfg.get("revision") or 0) > (best.get("revision") or 0)):
                        best = cfg
                except Exception:
                    pass
        return best
    except Exception:
        return None


def _zcode_remote_config():
    """zcode.z.ai 指名的官方 provider 配置。"""
    data = _zcode_call(
        "GET", ZCODE_BASE + "/api/v1/client/configs?app_version="
        + urllib.parse.quote(ZCODE_APP_VERSION, safe="")
        + "&platform=" + urllib.parse.quote(_zcode_platform_tag(), safe=""))
    configs = (data or {}).get("configs") if isinstance(data, dict) else {}
    url = ""
    if isinstance(configs, dict):
        url = str(configs.get("builtin_provider_config_json") or "").strip()
    if not url.startswith("https://"):
        raise PlatformError(502, "ZCode 的客户端配置里没有 provider 配置地址")
    req = urllib.request.Request(url, headers={
        "user-agent": "ZCode/" + ZCODE_APP_VERSION,
        "accept": "application/json",
    })
    with _open(req, timeout=8) as resp:
        raw = resp.read(8 << 20)
    cfg = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(cfg, dict):
        raise PlatformError(502, "ZCode 的 provider 配置不是 JSON 对象")
    return cfg


def _zcode_config(force=False):
    """官方 provider 配置：远程 + 本机取最新的；拿不到就用上次的。"""
    with _ZCODE_CONFIG_LOCK:
        now = time.time()
        cached = _ZCODE_CONFIG_CACHE["cfg"]
        if not force and cached is not None \
                and now - _ZCODE_CONFIG_CACHE["at"] < ZCODE_CONFIG_TTL_SECONDS:
            return cached
        cfg = None
        if force or cached is None:
            try:
                cfg = _zcode_remote_config()
            except Exception as exc:
                _log("zcode: 拉官方 provider 配置失败：%s" % exc)
        local = _zcode_local_config()
        if local is not None and (cfg is None
                                  or (local.get("revision") or 0)
                                  > (cfg.get("revision") or 0)):
            cfg = local
        if cfg is not None:
            _ZCODE_CONFIG_CACHE["cfg"] = cfg
            _ZCODE_CONFIG_CACHE["at"] = now
            return cfg
        return cached


def _zcode_plan_id(base):
    text = str(base or "")
    if "/zcode-plan/" in text:
        return "account:zai-start-plan"
    if "bigmodel.cn" in text:
        return "account:bigmodel-individual-coding-plan"
    return "account:zai-individual-coding-plan"


def _zcode_models_of(cfg, plan):
    """从官方配置里解出 plan 的模型：id / 上下文 / 输出 / 图像 / 推理档位。"""
    if not isinstance(cfg, dict):
        return []
    config = cfg.get("config") if isinstance(cfg.get("config"), dict) else {}
    provider_rules = (config.get("providerConfigRules") or {}) \
        .get("providerRules") if isinstance(config.get("providerConfigRules"), dict) else []
    model_config = config.get("modelConfigRules") \
        if isinstance(config.get("modelConfigRules"), dict) else {}
    ids = []
    seen = set()
    for rule in provider_rules or []:
        if not isinstance(rule, dict) or rule.get("providerId") != plan:
            continue
        for mid in rule.get("builtinModelIds") or []:
            mid = str(mid or "").strip()
            if mid and mid.lower() not in seen:
                seen.add(mid.lower())
                ids.append(mid)
    off = set()
    for rule in model_config.get("builtinProviderModelRules") or []:
        if not isinstance(rule, dict) or rule.get("providerId") != plan:
            continue
        mid = str(rule.get("modelId") or "").strip()
        if not mid:
            continue
        rule_cfg = rule.get("config") if isinstance(rule.get("config"), dict) else {}
        if rule_cfg.get("enabled") is False:
            off.add(mid.lower())
        elif mid.lower() not in seen:
            seen.add(mid.lower())
            ids.append(mid)
    model_rules = model_config.get("modelRules") or []
    rows = []
    for mid in ids:
        if mid.lower() in off:
            continue
        row = {"id": mid, "context": 0, "output": 0, "image": False,
               "efforts": []}
        for rule in model_rules or []:
            if not isinstance(rule, dict):
                continue
            pattern = str(rule.get("modelMatch") or "").strip()
            if not pattern:
                continue
            try:
                matched = re.match("^(?:%s)$" % pattern, mid, re.IGNORECASE)
            except Exception:
                continue
            if not matched:
                continue
            rule_cfg = rule.get("config") if isinstance(rule.get("config"), dict) else {}
            props = rule_cfg.get("properties") \
                if isinstance(rule_cfg.get("properties"), dict) else {}
            specs = rule_cfg.get("optionSpecs") \
                if isinstance(rule_cfg.get("optionSpecs"), dict) else {}
            try:
                context = int(props.get("contextWindow") or 0)
                if context > 0:
                    row["context"] = context
            except (TypeError, ValueError):
                pass
            max_tokens = specs.get("maxOutputTokens") or {}
            if isinstance(max_tokens, dict):
                try:
                    output = int(max_tokens.get("max") or 0)
                    if output > 0:
                        row["output"] = output
                except (TypeError, ValueError):
                    pass
            input_format = props.get("inputFormat") or {}
            if isinstance(input_format, dict) \
                    and isinstance(input_format.get("supportsImage"), bool):
                row["image"] = input_format["supportsImage"]
            levels = specs.get("reasoningLevel") or {}
            if isinstance(levels, dict):
                values = levels.get("values") or []
                efforts = []
                for value in values:
                    if not isinstance(value, str):
                        continue
                    if value == "disabled":
                        efforts.append("none")
                    elif value == "enabled":
                        efforts.append("high")
                    else:
                        efforts.append(value)
                if efforts:
                    row["efforts"] = efforts
        rows.append(row)
    return rows


def _zcode_fallback_models():
    return [
        {"id": m["id"], "context": m["context"], "output": m["output"],
         "image": bool(m.get("vision")),
         "efforts": (["none", "high", "max"] if m.get("reasoning") else [])}
        for m in ZCODE_MODELS
    ]


def _zcode_official_models(base, force=False):
    """一个 Coding Plan 的官方模型列表；拿不到就回落内置兜底表。"""
    cfg = _zcode_config(force=force)
    rows = _zcode_models_of(cfg, _zcode_plan_id(base))
    if not rows:
        rows = _zcode_fallback_models()
    return rows


def _zcode_arch():
    machine = str(_platform.machine() or "").strip().lower()
    return {
        "amd64": "x64", "x86_64": "x64", "x64": "x64",
        "arm64": "arm64", "aarch64": "arm64",
        "x86": "ia32", "i386": "ia32", "i686": "ia32",
    }.get(machine, machine or "x64")


def _zcode_platform_tag():
    name = "win32" if sys.platform == "win32" else (
        "darwin" if sys.platform == "darwin" else "linux")
    return name + "-" + _zcode_arch()


def _zcode_os_release():
    # Node 的 os.release()：Windows 上是 10.0.x（Python 的 release() 只给 11）。
    if sys.platform == "win32":
        return str(_platform.version() or "").strip() or "10.0.0"
    return str(_platform.release() or "").strip()


def _zcode_os_version_line():
    name = "win32" if sys.platform == "win32" else (
        "darwin" if sys.platform == "darwin" else "linux")
    return "%s %s %s" % (name, _zcode_os_release(), _zcode_arch())


def _zcode_os_category():
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _zcode_language():
    try:
        value = locale.getdefaultlocale()[0] or ""
    except Exception:
        value = ""
    value = str(value or "").replace("_", "-")
    return value or "en-US"


def _zcode_timezone():
    value = str(os.environ.get("TZ") or "").strip()
    if value:
        return value
    try:
        names = " ".join(str(x or "") for x in time.tzname).lower()
    except Exception:
        names = ""
    if "china" in names or "中国" in names or "台北" in names or "香港" in names:
        return "Asia/Shanghai"
    if "tokyo" in names or "日本" in names:
        return "Asia/Tokyo"
    if "korea" in names or "韩国" in names:
        return "Asia/Seoul"
    if "utc" in names or "gmt" in names:
        return "UTC"
    return "Asia/Shanghai"


def _zcode_device_mid():
    """发给 zcode.z.ai 的 X-Device-Mid（本机固定一个 UUID）。"""
    with _ZCODE_DEVICE_LOCK:
        if _ZCODE_DEVICE.get("id"):
            return _ZCODE_DEVICE["id"]
        path = ""
        try:
            import wb_egg_pools
            path = os.path.join(os.path.dirname(wb_egg_pools._pools_path()),
                                "zcode-device-mid")
        except Exception:
            path = ""
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read().strip()
                uuid.UUID(text)
                _ZCODE_DEVICE.update({"id": text.lower(), "path": path})
                return _ZCODE_DEVICE["id"]
            except Exception:
                pass
        value = str(uuid.uuid4())
        if path:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(value + "\n")
            except Exception:
                pass
        _ZCODE_DEVICE.update({"id": value, "path": path})
        return value


def _zcode_key_device(key):
    """只有 API Key 的账号：设备号由 Key 派生（每个进程都一样）。"""
    digest = hashlib.sha256(("zcode-device\0" + str(key or "")).encode("utf-8")).hexdigest()
    variant = format((int(digest[16], 16) & 3) | 8, "x")
    return "%s-%s-4%s-%s%s-%s" % (
        digest[0:8], digest[8:12], digest[13:16], variant, digest[17:20],
        digest[20:32])


def _zcode_call(method, url, auth="", body=None, headers=None, device="",
                timeout=HTTP_TIMEOUT_SECONDS):
    """问一个 ZCode / Z.ai / BigModel 的 JSON 接口。

    它们把结果包在 ``{code, msg, data}`` 里（0 / 200 算成功）；zcode.z.ai
    还要 X-Device-Mid。
    """
    request_headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": "ZCode/" + ZCODE_APP_VERSION,
    }
    if auth:
        request_headers["authorization"] = auth
    if str(url).startswith(ZCODE_BASE + "/"):
        request_headers["x-device-mid"] = device or _zcode_device_mid()
    for key, value in (headers or {}).items():
        if value is not None and value != "":
            request_headers[str(key)] = str(value)
    data = _json_bytes(body) if body is not None else None
    req = urllib.request.Request(str(url), data=data, method=str(method).upper(),
                                 headers=request_headers)
    try:
        with _open(req, timeout=timeout) as resp:
            raw = resp.read(8 << 20)
    except urllib.error.HTTPError as exc:
        detail = _http_detail(exc)
        message = detail
        code = ""
        try:
            parsed = json.loads(detail) if detail else {}
            if isinstance(parsed, dict):
                code = str(parsed.get("code") or "")
                error = parsed.get("error")
                if isinstance(error, dict):
                    code = code or str(error.get("code") or error.get("type") or "")
                    message = str(error.get("message") or parsed.get("msg")
                                  or parsed.get("message") or detail)
                else:
                    message = str(parsed.get("msg") or parsed.get("message")
                                  or error or detail)
        except Exception:
            pass
        message = str(message or "").strip() or ("HTTP %d" % exc.code)
        if code and code not in ("0", "200", "null"):
            message = "%s（HTTP %d，code %s）" % (message, exc.code, code)
        else:
            message = "%s（HTTP %d）" % (message, exc.code)
        raise PlatformError(exc.code, "ZCode：%s" % message)
    except PlatformError:
        raise
    except Exception as exc:
        raise PlatformError(502, "连不上 ZCode：%s" % exc)
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise PlatformError(502, "ZCode 返回的不是 JSON")
    if not isinstance(payload, dict):
        return payload
    code = payload.get("code")
    if code not in (None, "", 0, "0", 200, "200", "null"):
        raise PlatformError(502, "ZCode：%s（code %s）"
                            % (payload.get("msg") or payload.get("message") or "请求失败",
                               code))
    return payload.get("data")


def _zcode_jwt_claims(jwt):
    parts = str(jwt or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        raw = _b64url_decode(parts[1])
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _zcode_jwt_expiry(jwt):
    value = _zcode_num(_zcode_jwt_claims(jwt).get("exp"))
    return value if value > 0 else 0.0


def _zcode_jwt_expired(jwt):
    expiry = _zcode_jwt_expiry(jwt)
    return bool(expiry and time.time() > expiry)


def _zcode_bucket_models(bucket):
    names = []
    for item in bucket.get("capabilities") or []:
        text = str(item or "").strip()
        if not text:
            continue
        if ":" in text and not text.lower().startswith("model:"):
            continue
        name = text.split(":", 1)[1].strip() if ":" in text else text
        if name and name.lower() not in [x.lower() for x in names]:
            names.append(name)
    return names


def _zcode_bucket_spent(bucket):
    remaining = bucket.get("remaining_units")
    if remaining is not None:
        return _zcode_num(remaining) <= 0
    total = bucket.get("total_units")
    used = bucket.get("used_units")
    if total is not None and used is not None:
        return _zcode_num(used) >= _zcode_num(total)
    return False


def _zcode_start_plan(jwt, device=""):
    """读 ZCode 的赠送额度（Start Plan / 1024 Dev Day）。

    返回 ``{name, models, all_models, ends_at, total, remaining, plans}``；
    没有生效中的赠送计划时返回 None。
    """
    if not str(jwt or "").strip():
        return None
    if _zcode_jwt_expired(jwt):
        raise PlatformError(401, "ZCode 的登录态已过期：重新登录一次")
    data = _zcode_call(
        "GET", ZCODE_BASE + "/api/v1/zcode-plan/billing/balance?app_version="
        + urllib.parse.quote(ZCODE_APP_VERSION, safe=""),
        auth="Bearer " + str(jwt or "").strip(), device=device)
    if not isinstance(data, dict):
        return None
    now = _zcode_num(data.get("server_time")) or time.time()
    plans = [x for x in (data.get("plans") or []) if isinstance(x, dict)]
    balances = [x for x in (data.get("balances") or []) if isinstance(x, dict)]

    def same(bucket, plan):
        if bucket.get("user_plan_id") and plan.get("user_plan_id"):
            return bucket.get("user_plan_id") == plan.get("user_plan_id")
        return str(bucket.get("plan_id") or "") == str(plan.get("plan_id") or "")

    live_models = []
    all_models = []
    live_buckets = []
    buckets = []
    plan_names = []
    ends_at = 0.0
    for plan in plans:
        if str(plan.get("status") or "").strip().lower() != "active":
            continue
        end = _zcode_num(plan.get("ends_at"))
        if end > 0 and end <= now:
            continue
        name = str(plan.get("name") or "").strip()
        if name and name not in plan_names:
            plan_names.append(name)
        if end > 0:
            ends_at = end if not ends_at else min(ends_at, end)
        for bucket in balances:
            if not same(bucket, plan):
                continue
            expiry = _zcode_num(bucket.get("expires_at"))
            if expiry > 0 and expiry <= now:
                continue
            if expiry > 0:
                ends_at = expiry if not ends_at else min(ends_at, expiry)
            bucket_models = [_zcode_model_name(x)
                             for x in _zcode_bucket_models(bucket)]
            spent = _zcode_bucket_spent(bucket)
            row = {
                "plan": name or "ZCode Start Plan",
                "name": str(bucket.get("show_name")
                            or bucket.get("entitlement_id")
                            or (bucket_models[0] if bucket_models else "")
                            or "额度").strip(),
                "models": bucket_models,
                "total": _zcode_num(bucket.get("total_units")),
                "used": _zcode_num(bucket.get("used_units")),
                "remaining": _zcode_num(bucket.get("remaining_units")),
                "period": str(bucket.get("period") or "").strip(),
                "spent": spent,
            }
            buckets.append(row)
            for model in bucket_models:
                if model.lower() not in [x.lower() for x in all_models]:
                    all_models.append(model)
                if not spent and model.lower() not in [
                        x.lower() for x in live_models]:
                    live_models.append(model)
            if not spent:
                live_buckets.append({
                    "total": row["total"],
                    "remaining": row["remaining"],
                })
    if not plan_names and not live_models:
        return None
    total = sum(x["total"] for x in live_buckets)
    remaining = sum(x["remaining"] for x in live_buckets)
    return {
        "name": plan_names[0] if plan_names else "ZCode Start Plan",
        "plans": plan_names,
        "models": live_models,
        "all_models": all_models,
        "ends_at": ends_at,
        "total": total,
        "remaining": remaining,
        "buckets": buckets,
    }


def _zcode_business_auth(site, token):
    """把站点登录 token 换成业务 API 的 Authorization。"""
    token = str(token or "").strip()
    if not token:
        raise PlatformError(401, "ZCode 登录响应里没有 access_token")
    if _zcode_site(site) == "bigmodel":
        return token
    data = _zcode_call("POST", ZCODE_ZAI_API + "/api/auth/z/login",
                       body={"token": token})
    access = str((data or {}).get("access_token") or "").strip() \
        if isinstance(data, dict) else ""
    if not access:
        raise PlatformError(502, "Z.ai 没有返回业务登录 token")
    return "Bearer " + access


def _zcode_customer_info(site, auth):
    root = _zcode_site_info(site)["api"]
    data = _zcode_call("GET", root + "/api/biz/customer/getCustomerInfo",
                       auth=auth)
    return data if isinstance(data, dict) else {}


def _zcode_keys_url(site, org, project):
    api = _zcode_site_info(site)["api"]
    return "%s/api/biz/v1/organization/%s/projects/%s/api_keys" % (
        api, urllib.parse.quote(str(org or ""), safe=""),
        urllib.parse.quote(str(project or ""), safe=""))


def _zcode_project_key(site, org, project, auth, name, key_type=None,
                       extra_headers=None):
    """找项目里叫 name 的 Key（没有就建一个），返回 ``<id>.<secret>``。"""
    url = _zcode_keys_url(site, org, project)
    headers = dict(extra_headers or {})
    try:
        rows = _zcode_call("GET", url, auth=auth, headers=headers)
    except PlatformError as exc:
        raise PlatformError(exc.status, "%s API Key：%s"
                            % (_zcode_site_info(site)["name"], exc.detail))
    if not isinstance(rows, list):
        rows = []
    key_id = ""
    for row in rows:
        if not isinstance(row, dict) or str(row.get("name") or "") != str(name):
            continue
        if key_type is not None and str(row.get("keyType") or "") != str(key_type):
            continue
        key_id = str(row.get("apiKey") or "").strip()
        if key_id:
            break
    if not key_id:
        body = {"name": str(name)}
        if key_type is not None:
            body["keyType"] = key_type
        try:
            created = _zcode_call("POST", url, auth=auth, body=body,
                                  headers=headers)
        except PlatformError as exc:
            raise PlatformError(exc.status, "%s API Key：%s"
                                % (_zcode_site_info(site)["name"], exc.detail))
        key_id = str((created or {}).get("apiKey") or "").strip() \
            if isinstance(created, dict) else ""
    if not key_id:
        raise PlatformError(502, "%s 没有给出 API Key"
                            % _zcode_site_info(site)["name"])
    try:
        copied = _zcode_call("GET", url + "/copy/" + urllib.parse.quote(key_id, safe=""),
                             auth=auth, headers=headers)
    except PlatformError as exc:
        raise PlatformError(exc.status, "%s API Key：%s"
                            % (_zcode_site_info(site)["name"], exc.detail))
    secret = str((copied or {}).get("secretKey") or "").strip() \
        if isinstance(copied, dict) else ""
    if secret:
        return key_id + "." + secret
    # 团队 Key 有时不回 secret，直接给 id
    if key_type is not None:
        return key_id
    raise PlatformError(502, "%s 没有给出 API Key 的 secret"
                        % _zcode_site_info(site)["name"])


def _zcode_subscriptions(site, key):
    root = _zcode_site_info(site)["api"]
    data = _zcode_call("GET", root + "/api/biz/subscription/list", auth=key)
    return data if isinstance(data, list) else []


def _zcode_plan_name(site, key):
    plan = ""
    for row in _zcode_subscriptions(site, key):
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").upper() != "VALID":
            continue
        plan = str(row.get("productName") or "").strip()
        if plan:
            break
    return plan


def _zcode_team_seat(site, info, auth):
    """账号在团队套餐里的座位（组织 / 项目 / key）。"""
    root = _zcode_site_info(site)["api"]
    projects = []
    for org in info.get("organizations") or []:
        if not isinstance(org, dict):
            continue
        for project in org.get("projects") or []:
            if not isinstance(project, dict):
                continue
            if str(project.get("projectType") or "") != "2":
                continue
            if not org.get("organizationId") or not project.get("projectId"):
                continue
            projects.append((str(org["organizationId"]),
                             str(project["projectId"])))
    last_error = ""
    for org_id, project_id in projects:
        headers = {
            "Bigmodel-Organization": org_id,
            "Bigmodel-Project": project_id,
            "Set-Language": "zh" if _zcode_site(site) == "bigmodel" else "en",
            "Accept-Language": "en-US,en",
        }
        try:
            detail = _zcode_call(
                "GET", root + "/api/biz/team/subscribe/product/querySubscribeDetail",
                auth=auth, headers=headers)
        except PlatformError as exc:
            last_error = exc.detail
            continue
        if not isinstance(detail, dict):
            continue
        status = str(detail.get("status") or "").upper()
        grant = str(detail.get("memberGrantStatus") or "").upper()
        if detail.get("hasSubscription") is False or status != "EFFECTIVE" \
                or grant != "VALID":
            if status == "EFFECTIVE" and grant == "UNASSIGNED":
                last_error = "这个账号在团队里，但还没有分到座位"
            elif status == "EXPIRED":
                last_error = "这个团队套餐已经到期"
            continue
        key = _zcode_project_key(site, org_id, project_id, auth,
                                 "zcode-team-api-key", key_type=2,
                                 extra_headers=headers)
        return {
            "key": key,
            "org": org_id,
            "project": project_id,
            "plan": str(detail.get("productName") or "GLM Coding Team"),
        }
    return {"error": last_error}


def _zcode_default_project(info):
    chosen_org = None
    chosen_project = None
    for org in info.get("organizations") or []:
        if not isinstance(org, dict) or not org.get("organizationId"):
            continue
        projects = [x for x in (org.get("projects") or [])
                    if isinstance(x, dict) and x.get("projectId")
                    and str(x.get("projectType") or "") != "2"]
        if not projects:
            continue
        project = next((x for x in projects
                        if "默认项目" in str(x.get("projectName") or "")), projects[0])
        named = "默认机构" in str(org.get("organizationName") or "")
        if chosen_org is None or named:
            chosen_org = str(org["organizationId"])
            chosen_project = str(project["projectId"])
            if named:
                break
    return chosen_org or "", chosen_project or ""


def _zcode_who(info, fallback="ZCode"):
    if not isinstance(info, dict):
        return fallback
    email = str(info.get("email") or "").strip()
    if email.endswith("@phone.local"):
        email = email[:-len("@phone.local")]
    name = str(info.get("displayName") or info.get("name")
               or info.get("nickName") or info.get("username") or "").strip()
    uid = str(info.get("id") or info.get("user_id")
              or info.get("customerNumber") or "").strip()
    return email or name or uid or fallback


def _zcode_resolve_signed_in(site, token, jwt, device, on_team=False):
    """用一次站点登录态找出这个账号真正能用的额度。

    返回 route（coding / start）、key、base、plan、models、user 等。
    """
    site = _zcode_site(site)
    token = str(token or "").strip()
    jwt = str(jwt or "").strip()
    device = str(device or "").strip() or _zcode_device_mid()
    auth = _zcode_business_auth(site, token)
    info = _zcode_customer_info(site, auth)
    user = {
        "email": str(info.get("email") or "").strip(),
        "name": str(info.get("nickName") or info.get("customerName") or "").strip(),
        "id": str(info.get("customerNumber") or info.get("id") or "").strip(),
    }
    site_info = _zcode_site_info(site)
    if on_team:
        seat = _zcode_team_seat(site, info, auth)
        if not seat.get("key"):
            raise PlatformError(400, seat.get("error")
                                or "这个账号没有团队 GLM Coding Plan 的座位")
        return {
            "route": "coding", "site": site, "base": site_info["base"],
            "key": seat["key"], "jwt": jwt, "device": device,
            "plan": seat.get("plan") or "GLM Coding Team",
            "org": seat.get("org") or "", "project": seat.get("project") or "",
            "team": True, "models": [m["id"] for m in ZCODE_MODELS],
            "expires_at": 0.0, "user": user, "auth_token": auth,
        }
    own_error = ""
    own_key = ""
    org_id, project_id = _zcode_default_project(info)
    if org_id and project_id:
        try:
            own_key = _zcode_project_key(site, org_id, project_id, auth,
                                         "zcode-api-key")
        except PlatformError as exc:
            own_error = exc.detail
    if own_key:
        try:
            plan = _zcode_plan_name(site, own_key)
        except PlatformError as exc:
            own_error = exc.detail
            plan = ""
        if plan:
            return {
                "route": "coding", "site": site, "base": site_info["base"],
                "key": own_key, "jwt": jwt, "device": device, "plan": plan,
                "org": org_id, "project": project_id, "team": False,
                "models": [m["id"] for m in ZCODE_MODELS],
                "expires_at": 0.0, "user": user, "auth_token": auth,
            }
    team = _zcode_team_seat(site, info, auth)
    if team.get("key"):
        return {
            "route": "coding", "site": site, "base": site_info["base"],
            "key": team["key"], "jwt": jwt, "device": device,
            "plan": team.get("plan") or "GLM Coding Team",
            "org": team.get("org") or "", "project": team.get("project") or "",
            "team": True, "models": [m["id"] for m in ZCODE_MODELS],
            "expires_at": 0.0, "user": user, "auth_token": auth,
        }
    if jwt:
        start = _zcode_start_plan(jwt, device)
        if start and start.get("models"):
            return {
                "route": "start", "site": site,
                "base": site_info["base"], "key": "", "jwt": jwt,
                "device": device, "plan": start.get("name") or "ZCode Start Plan",
                "org": "", "project": "", "team": False,
                "models": list(start.get("models") or []),
                "expires_at": float(start.get("ends_at") or 0),
                "user": user, "auth_token": auth,
                "gift": start,
            }
    detail = own_error or team.get("error") or ""
    if detail:
        raise PlatformError(400, "这个 ZCode 账号没有可用的 GLM Coding Plan 或赠送额度：%s"
                            % detail)
    subscribe = site_info.get("subscribe") or ""
    raise PlatformError(
        400, "这个 ZCode 账号没有 GLM Coding Plan，赠送额度也已用完；"
             "请先在 %s 订阅，或在 ZCode 里领取赠送额度后再登录。" % subscribe)


def _zcode_account(site, route, base, key="", jwt="", device="", plan="",
                   models=None, user=None, source="browser", org="",
                   project="", team=False, expires_at=0.0, auth_token="",
                   base_account=None, usage=None):
    """把一次登录结果收拾成平台账号。"""
    user = user if isinstance(user, dict) else {}
    site = _zcode_site(site)
    base_account = base_account if isinstance(base_account, dict) else {}
    uid = str(user.get("id") or "").strip()
    email = str(user.get("email") or "").strip()
    label = _zcode_who(user, "ZCode")
    if source == "key":
        label = str(plan or "GLM Coding Plan").strip() + " · " + str(key or "")[-6:]
    identity = uid or key or jwt
    seed = "%s\0%s" % (site, identity)
    stable = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    account_id = str(base_account.get("id") or ("acct-" + stable[:12]))
    return {
        "id": account_id,
        "label": label,
        "email": email,
        "enabled": base_account.get("enabled", True) is not False,
        "created_at": float(base_account.get("created_at") or time.time()),
        "last_used": float(base_account.get("last_used") or 0.0),
        "status": "ok",
        "last_error": "",
        "platform": PLATFORM_ZCODE,
        "site": site,
        "route": route,
        "base_url": base,
        "api_key": str(key or ""),
        "jwt": str(jwt or ""),
        "device": str(device or ""),
        "plan": str(plan or ""),
        "models": list(models or []),
        "uid": uid,
        "org": str(org or ""),
        "project": str(project or ""),
        "team": bool(team),
        "source": source,
        "auth_token": str(auth_token or ""),
        "expires_at": float(expires_at or 0.0),
        # 赠送额度的用量快照（每个余额桶一行：套餐 / 模型 / 用了多少）。
        "usage": [dict(x) for x in (usage or []) if isinstance(x, dict)],
    }


def _zcode_account_from_login(site, token, jwt, device, source="browser",
                              on_team=False, user_hint=None):
    resolved = _zcode_resolve_signed_in(site, token, jwt, device,
                                        on_team=on_team)
    user = dict(resolved.get("user") or {})
    if isinstance(user_hint, dict):
        for key in ("email", "name", "id"):
            if not user.get(key) and user_hint.get(key):
                user[key] = user_hint[key]
    gift = resolved.get("gift") if isinstance(resolved.get("gift"), dict) else {}
    return _zcode_account(
        site, resolved["route"], resolved["base"],
        key=resolved.get("key") or "", jwt=resolved.get("jwt") or "",
        device=resolved.get("device") or device,
        plan=resolved.get("plan") or "", models=resolved.get("models") or [],
        user=user, source=source, org=resolved.get("org") or "",
        project=resolved.get("project") or "", team=resolved.get("team") or False,
        expires_at=resolved.get("expires_at") or 0.0,
        auth_token=resolved.get("auth_token") or "",
        usage=gift.get("buckets") or [])


def _zcode_secret():
    seed = str(os.environ.get("ZCODE_CREDENTIAL_SECRET") or "").strip()
    if not seed:
        home = os.path.expanduser("~")
        try:
            name = getpass.getuser()
        except Exception:
            name = ""
        if "\\" in name:
            name = name.rsplit("\\", 1)[-1]
        platform_name = "win32" if sys.platform == "win32" else (
            "darwin" if sys.platform == "darwin" else sys.platform)
        seed = "zcode-credential-fallback:%s:%s:%s" % (platform_name, home, name)
    return hashlib.sha256(seed.encode("utf-8")).digest()


def _zcode_decrypt(secret, value):
    text = str(value or "")
    if not text.startswith("enc:v1:"):
        return ""
    parts = text[7:].split(".")
    if len(parts) != 3:
        return ""
    try:
        iv, tag, data = [
            base64.urlsafe_b64decode(item + "=" * (-len(item) % 4))
            for item in parts
        ]
        import wb_atrest
        plain = wb_atrest._gcm_open(secret, iv, b"", data, tag)
        return plain.decode("utf-8", "replace")
    except Exception:
        return ""


def _zcode_local_paths():
    home = os.path.expanduser("~")
    settings = os.path.join(home, ".zcode", "v2", "setting.json")
    data_dir = home
    try:
        with open(settings, "r", encoding="utf-8") as fh:
            setting = json.load(fh)
        moved = str((setting or {}).get("dataBaseDir") or "").strip()
        if moved and os.path.isabs(moved):
            data_dir = moved
    except Exception:
        setting = {}
    return {
        "home": home,
        "setting": setting if isinstance(setting, dict) else {},
        "settings_path": settings,
        "data_dir": data_dir,
        "credentials": os.path.join(data_dir, ".zcode", "v2", "credentials.json"),
    }


def _zcode_local_login():
    """读本机 ZCode 的登录态；没有就返回 None。"""
    paths = _zcode_local_paths()
    try:
        with open(paths["credentials"], "r", encoding="utf-8") as fh:
            store = json.load(fh)
    except Exception:
        return None
    if not isinstance(store, dict):
        return None
    secret = _zcode_secret()
    active = _zcode_decrypt(secret, store.get("oauth:active_provider", "")).strip().lower()
    site = active if active in ("zai", "bigmodel") else ""
    if not site:
        site = "bigmodel" if store.get("oauth:bigmodel:access_token") \
            and not store.get("oauth:zai:access_token") else "zai"
    token = _zcode_decrypt(secret, store.get("oauth:%s:access_token" % site)).strip()
    jwt = _zcode_decrypt(secret, store.get("zcodejwttoken", "")).strip()
    jwt = jwt[7:].strip() if jwt.lower().startswith("bearer ") else jwt
    if not token:
        other = "zai" if site == "bigmodel" else "bigmodel"
        token = _zcode_decrypt(secret, store.get("oauth:%s:access_token" % other)).strip()
        if token:
            site = other
    if not token and not jwt:
        return None
    user = {}
    raw = _zcode_decrypt(secret, store.get("oauth:%s:user_info" % site, ""))
    if raw:
        try:
            info = json.loads(raw)
            if isinstance(info, dict):
                profile = info.get("rawProfile") if isinstance(
                    info.get("rawProfile"), dict) else {}
                user = {
                    "email": str(info.get("email") or profile.get("email") or ""),
                    "name": str(info.get("displayName") or info.get("name")
                                or info.get("username") or ""),
                    "id": str(info.get("id") or info.get("user_id") or ""),
                }
        except Exception:
            pass
    return {
        "site": site,
        "token": token,
        "jwt": jwt,
        "user": user,
        "device": _zcode_device_mid(),
        "path": paths["credentials"],
    }


def _zcode_import_login(options=None):
    local = _zcode_local_login()
    if local is None:
        raise PlatformError(
            400, "本机没有找到 ZCode 的登录态：先打开 ZCode 客户端登录，"
                 "或改用浏览器登录 / GLM Coding Plan API Key。")
    try:
        account = _zcode_account_from_login(
            local["site"], local["token"], local["jwt"], local["device"],
            source="zcode-app", user_hint=local.get("user"))
    except PlatformError as exc:
        raise PlatformError(exc.status, "本机 ZCode 登录态不可用：%s" % exc.detail)
    _log("zcode: 已读取本机 ZCode 登录态（%s）" % account.get("label"))
    return account


def _zcode_key_login(options=None):
    options = options or {}
    key = str(options.get("key") or options.get("api_key") or "").strip()
    if not key:
        raise PlatformError(400, "先粘贴 GLM Coding Plan API Key")
    if len(key) < 16 or " " in key or "." not in key:
        raise PlatformError(400, "GLM Coding Plan API Key 形如 <id>.<secret>，请检查")
    site = _zcode_site(options.get("site"))
    plan = _zcode_plan_name(site, key)
    if not plan:
        raise PlatformError(
            400, "这把 Key 背后没有生效的 GLM Coding Plan。"
                 "ZCode 的赠送额度只能通过 ZCode 登录态使用，不能只用 Key。")
    return _zcode_account(
        site, "coding", _zcode_site_info(site)["base"], key=key,
        device=_zcode_key_device(key), plan=plan,
        models=[m["id"] for m in ZCODE_MODELS], user={}, source="key")


def _zcode_login_success(platform, account):
    session = _new_session(platform)
    session["account"] = account
    session["status"] = "success"
    session["instructions"] = ""
    return _public_session(session)


def _zcode_browser_login_start(site):
    site = _zcode_site(site)
    session = _new_session(PLATFORM_ZCODE)
    poll_auth = "Bearer " + secrets.token_hex(32)
    device = str(uuid.uuid4())
    init_body = {"provider": site}
    flow = _zcode_call("POST", ZCODE_BASE + "/api/v1/oauth/cli/init",
                       auth=poll_auth, body=init_body, device=device)
    flow = flow if isinstance(flow, dict) else {}
    flow_id = str(flow.get("flow_id") or "").strip()
    authorize = str(flow.get("authorize_url") or "").strip()
    if not flow_id or not authorize.startswith("https://"):
        raise PlatformError(502, "ZCode 没有给出登录页")
    split = urllib.parse.urlsplit(authorize)
    query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
    back = ZCODE_BASE + "/app/oauth/login?" + urllib.parse.urlencode({
        "redirect": "zcode://oauth/callback",
        "app_version": ZCODE_APP_VERSION,
    })
    query["redirect" if site == "bigmodel" else "redirect_uri"] = [back]
    authorize = urllib.parse.urlunsplit((
        split.scheme, split.netloc, split.path,
        urllib.parse.urlencode(query, doseq=True), split.fragment))
    interval = max(1.0, _zcode_num(flow.get("poll_interval_sec")) or 2.0)
    deadline = _zcode_num(flow.get("expires_at")) or (time.time() + 5 * 60)
    session.update({
        "site": site,
        "zcode_flow_id": flow_id,
        "zcode_poll_auth": poll_auth,
        "zcode_device": device,
        "zcode_interval": interval,
        "url": authorize,
        "instructions": ("在浏览器打开授权页并登录 %s；登录完成后会自动加入账号池。"
                         % _zcode_site_info(site)["name"]),
        "expires_at": min(time.time() + LOGIN_TTL_SECONDS,
                          max(time.time() + 60, deadline)),
    })
    thread = threading.Thread(target=_zcode_poll_loop, args=(session,),
                              daemon=True)
    thread.start()
    session["thread"] = thread
    _log("zcode: %s 浏览器登录已启动" % _zcode_site_info(site)["name"])
    return _public_session(session)


def _zcode_poll_loop(session):
    site = session.get("site") or "zai"
    flow_id = session.get("zcode_flow_id") or ""
    auth = session.get("zcode_poll_auth") or ""
    device = session.get("zcode_device") or ""
    interval = float(session.get("zcode_interval") or 2.0)
    deadline = float(session.get("expires_at") or 0)
    while time.time() < deadline:
        if session.get("cancel") or session.get("status") != "pending":
            return
        time.sleep(interval)
        if session.get("cancel") or session.get("status") != "pending":
            return
        try:
            got = _zcode_call(
                "GET", ZCODE_BASE + "/api/v1/oauth/cli/poll/"
                + urllib.parse.quote(flow_id, safe=""),
                auth=auth, device=device)
        except PlatformError as exc:
            if exc.status in (408, 429) or exc.status >= 500:
                continue
            session["status"] = "error"
            session["error"] = "ZCode 登录：%s" % exc.detail
            return
        except Exception as exc:
            session["status"] = "error"
            session["error"] = "ZCode 登录轮询失败：%s" % exc
            return
        got = got if isinstance(got, dict) else {}
        status = str(got.get("status") or "").strip().lower()
        if status in ("", "pending"):
            continue
        if status == "failed":
            session["status"] = "error"
            session["error"] = "在 %s 上拒绝了这次登录" % _zcode_site_info(site)["name"]
            return
        if status != "ready":
            session["status"] = "error"
            session["error"] = "ZCode 登录返回了意外的状态：%s" % status
            return
        block = got.get(site) if isinstance(got.get(site), dict) else {}
        token = str(block.get("access_token") or block.get("accessToken") or "").strip()
        jwt = str(got.get("token") or "").strip()
        if not token:
            session["status"] = "error"
            session["error"] = "ZCode 登录成功但没有返回 access_token"
            return
        user = got.get("user") if isinstance(got.get("user"), dict) else None
        try:
            account = _zcode_account_from_login(
                site, token, jwt, device, source="browser", user_hint=user)
        except PlatformError as exc:
            session["status"] = "error"
            session["error"] = exc.detail
            return
        except Exception as exc:
            session["status"] = "error"
            session["error"] = "ZCode 登录换取凭据失败：%s" % exc
            return
        session["account"] = account
        session["status"] = "success"
        _log("zcode: 账号 %s 登录成功" % account.get("label"))
        return
    session["status"] = "expired"
    session["error"] = "ZCode 登录页已过期，请重新发起登录"


def _zcode_refresh_account(pool, account):
    """续期 / 重新读取一个 ZCode 账号。Key 不会过期，但套餐与额度会变。"""
    account = dict(account or {})
    site = _zcode_site(account.get("site"))
    source = str(account.get("source") or "")
    route = str(account.get("route") or "")
    key = str(account.get("api_key") or "")
    jwt = str(account.get("jwt") or "")
    device = str(account.get("device") or "") or _zcode_device_mid()
    if source == "zcode-app":
        local = _zcode_local_login()
        if local is not None:
            old_uid = str(account.get("uid") or "")
            new_uid = str((local.get("user") or {}).get("id") or "")
            if not old_uid or not new_uid or old_uid == new_uid:
                try:
                    updated = _zcode_account_from_login(
                        local["site"], local["token"], local["jwt"],
                        local["device"], source="zcode-app",
                        user_hint=local.get("user"))
                except PlatformError as exc:
                    raise PlatformError(exc.status, "本机 ZCode 登录态不可用：%s"
                                        % exc.detail)
                updated["id"] = account.get("id") or updated["id"]
                updated["created_at"] = account.get("created_at") or updated["created_at"]
                updated["enabled"] = account.get("enabled", True) is not False
                updated["last_used"] = account.get("last_used") or 0.0
                return updated
    if bool(account.get("team")):
        # 团队 Key 不会过期；有业务登录态时顺手复核一次座位。
        auth = str(account.get("auth_token") or "")
        if auth:
            try:
                info = _zcode_customer_info(site, auth)
                seat = _zcode_team_seat(site, info, auth)
                if seat.get("key"):
                    account["api_key"] = seat["key"]
                    account["plan"] = seat.get("plan") or account.get("plan") \
                        or "GLM Coding Team"
                else:
                    raise PlatformError(401, seat.get("error")
                                        or "团队座位已失效：重新登录一次")
            except PlatformError:
                raise
            except Exception:
                pass
        account["status"] = "ok"
        account["last_error"] = ""
        return account
    if route == "start" or (not key and jwt):
        start = _zcode_start_plan(jwt, device)
        if not start or not start.get("models"):
            raise PlatformError(401, "ZCode 的赠送额度已用完或已过期：重新登录一次")
        account.update({
            "route": "start",
            "plan": start.get("name") or "ZCode Start Plan",
            "models": list(start.get("models") or []),
            "expires_at": float(start.get("ends_at") or 0),
            "usage": [dict(x) for x in (start.get("buckets") or [])
                      if isinstance(x, dict)],
            "status": "ok",
            "last_error": "",
        })
        return account
    if key:
        plan = _zcode_plan_name(site, key)
        if not plan:
            raise PlatformError(401, "这把 Key 背后没有生效的 GLM Coding Plan")
        account.update({
            "plan": plan,
            "models": [m["id"] for m in ZCODE_MODELS],
            "expires_at": 0.0,
            "status": "ok",
            "last_error": "",
        })
        if str(account.get("route") or "") not in ("coding", "start"):
            account["route"] = "coding"
        return account
    raise PlatformError(401, "这个 ZCode 账号的登录态不可用：重新登录一次")


def _zcode_prompt():
    import wb_zcode_prompt
    return wb_zcode_prompt.PROMPT


def _zcode_cached(text):
    return {"type": "text", "text": str(text or ""),
            "cache_control": {"type": "ephemeral"}}


def _zcode_context_prefix():
    prompt = _zcode_prompt()
    context = prompt["context"]
    today = datetime.date.today().isoformat()
    body = "\n".join([
        context["intro"],
        context["currentDateHeading"] + "\n"
        + context["currentDateLine"].replace("{date}", today),
        "",
        context["outro"],
    ])
    return {"role": "user",
            "content": [{"type": "text",
                         "text": "<system-reminder>" + body + "</system-reminder>"}]}


def _zcode_environment(site, model):
    prompt = _zcode_prompt()
    env = prompt["environment"]
    provider = "bigmodel-api" if _zcode_site(site) == "bigmodel" else "zai-api"
    lines = [env["heading"], env["invokedLine"],
             "- %s: %s" % (env["platformLabel"],
                           "win32" if sys.platform == "win32" else sys.platform),
             "- %s: %s" % (env["osVersionLabel"], _zcode_os_version_line())]
    if model:
        lines.append(env["poweredByLine"].replace("{provider}", provider)
                     .replace("{model}", str(model)))
    return "\n".join(lines)


def _zcode_dress(body, site, device):
    """把 Anthropic Messages 请求体打扮成 ZCode 客户端的样子。

    Start Plan 只认 ZCode 自己的三段 system 提示词；系统的额外提示词并进
    第三段（第四段会被 405/3012 拒掉）。
    """
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return body
    prompt = _zcode_prompt()
    system = body.get("system")
    own = []
    if isinstance(system, str):
        if system.strip():
            own.append(system)
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text" \
                    and str(block.get("text") or "").strip():
                own.append(str(block["text"]))
    dynamic = [prompt["beforeEnvironment"],
               _zcode_environment(site, body.get("model")),
               prompt["afterEnvironment"]] + own
    body["system"] = [
        _zcode_cached(prompt["cliPrefix"]),
        _zcode_cached("\n\n".join(prompt["stableSections"])),
        _zcode_cached("\n\n" + "\n\n".join(dynamic)),
    ]
    body["messages"] = [_zcode_context_prefix()] + list(body["messages"])
    last = None
    for message in body["messages"]:
        if not isinstance(message, dict) or message.get("role") == "system":
            continue
        last = message
        if isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    if isinstance(last, dict):
        content = last.get("content")
        if isinstance(content, str):
            last["content"] = [_zcode_cached(content)]
        elif isinstance(content, list) and content:
            block = content[-1]
            if isinstance(block, dict) and not block.get("cache_control"):
                block["cache_control"] = {"type": "ephemeral"}
    if isinstance(body.get("tools"), list):
        for tool in body["tools"]:
            if isinstance(tool, dict):
                tool.pop("cache_control", None)
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    metadata = dict(metadata)
    metadata["user_id"] = json.dumps({
        "device_id": str(device or _zcode_device_mid()),
        "account_uuid": "",
        "session_id": "",
    }, separators=(",", ":"))
    body["metadata"] = metadata
    return body


def _zcode_mark(body, device):
    """Coding Plan 请求也带 ZCode 的设备号（缓存按它固定在一台机器上）。"""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return body
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    metadata = dict(metadata)
    if not str(metadata.get("user_id") or "").strip():
        metadata["user_id"] = json.dumps({
            "device_id": str(device or _zcode_device_mid()),
            "account_uuid": "",
            "session_id": "",
        }, separators=(",", ":"))
    body["metadata"] = metadata
    return body


def _zcode_start_headers(jwt):
    return {
        "content-type": "application/json",
        "accept": "*/*",
        "accept-language": "*",
        "authorization": "Bearer " + str(jwt or "").strip(),
        "anthropic-version": ZCODE_ANTHROPIC_VERSION,
        "HTTP-Referer": ZCODE_BASE,
        "user-agent": "ZCode/%s ai-sdk/anthropic/3.0.81" % ZCODE_APP_VERSION,
        "X-ZCode-App-Version": ZCODE_APP_VERSION,
        "X-Title": "Z Code@cli",
        "X-Release-Channel": "production",
        "X-Client-Language": _zcode_language(),
        "X-Client-Timezone": _zcode_timezone(),
        "X-ZCode-Agent": "glm",
        "X-Platform": _zcode_platform_tag(),
        "X-Os-Category": _zcode_os_category(),
        "X-Os-Version": _zcode_os_release(),
        "x-request-id": str(uuid.uuid4()),
        "x-zcode-session-type": "main",
        "x-zcode-trace-id": str(uuid.uuid4()),
        "x-zcode-query-source": "main_turn",
    }


def _zcode_coding_headers(key):
    key = str(key or "").strip()
    return {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "anthropic-version": ZCODE_ANTHROPIC_VERSION,
        "user-agent": "ZCode/" + ZCODE_APP_VERSION,
        "x-api-key": key,
        "authorization": "Bearer " + key,
    }


def _zcode_account_models(account):
    names = [str(x or "").strip() for x in (account.get("models") or [])
             if str(x or "").strip()]
    if not names and str(account.get("route") or "") != "start":
        names = [m["id"] for m in ZCODE_MODELS]
    return names


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


def login_start(platform, options=None):
    """开始一次平台登录，返回给面板的启动信息。

    ``options`` 只给需要额外输入的登录方式用（ZCode 的站点 / API Key /
    导入本机登录态）；其它平台忽略它，行为与以前一致。
    """
    _prune_sessions()
    options = options if isinstance(options, dict) else {}
    item = platform_def(platform)
    if item is None:
        raise PlatformError(400, "不支持的平台：%s" % platform)
    if item["id"] == PLATFORM_CODEARTS:
        return _codearts_login_start()
    if item["id"] == PLATFORM_CLINE:
        return _cline_login_start()
    if item["id"] == PLATFORM_ZCODE:
        mode = str(options.get("mode") or "browser").strip().lower()
        if mode not in ZCODE_LOGIN_MODES:
            raise PlatformError(400, "不支持的 ZCode 登录方式：%s" % mode)
        if mode == "import":
            return _zcode_login_success(
                PLATFORM_ZCODE, _zcode_import_login(options))
        if mode == "key":
            return _zcode_login_success(
                PLATFORM_ZCODE, _zcode_key_login(options))
        return _zcode_browser_login_start(options.get("site"))
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
        # ZCode 的账号卡片要多说两句：站点、套餐、额度类型（Coding Plan /
        # Start Plan）。这些都不是凭据，可以给面板看。
        "site": account.get("site") or "",
        "site_label": (_zcode_site_info(account.get("site"))["name"]
                       if account.get("platform") == PLATFORM_ZCODE else ""),
        "plan": account.get("plan") or "",
        "route": account.get("route") or "",
        "source": account.get("source") or "",
        "team": bool(account.get("team")),
        "models": [str(x) for x in (account.get("models") or [])][:40],
        # 赠送额度的用量快照（每个余额桶一行）。只含数字与名字，没有凭据。
        "usage": [
            {
                "plan": str(x.get("plan") or ""),
                "name": str(x.get("name") or ""),
                "models": [str(m) for m in (x.get("models") or [])][:8],
                "total": int(_zcode_num(x.get("total"))),
                "used": int(_zcode_num(x.get("used"))),
                "remaining": int(_zcode_num(x.get("remaining"))),
                "percent": round(_zcode_num(x.get("used"))
                                 / _zcode_num(x.get("total")) * 100, 2)
                if _zcode_num(x.get("total")) > 0 else 0.0,
                "period": str(x.get("period") or ""),
                "spent": bool(x.get("spent")),
            }
            for x in (account.get("usage") or []) if isinstance(x, dict)
        ][:20],
    }


def _has_live_credential(account):
    if not account:
        return False
    expires_at = float(account.get("expires_at") or 0)
    if expires_at and time.time() > expires_at:
        return False
    if account.get("platform") == PLATFORM_CODEARTS:
        return bool(account.get("access_key") and account.get("secret_key"))
    if account.get("platform") == PLATFORM_ZCODE:
        if account.get("api_key"):
            return True
        return bool(account.get("jwt")) and not _zcode_jwt_expired(account.get("jwt"))
    return bool(account.get("access_token"))


def _needs_refresh(account):
    if account.get("platform") == PLATFORM_ZCODE:
        # Coding Plan 的 Key 不会过期；赠送额度靠 expires_at（计划结束时间）
        # 判断，Key 账号不需要后台续期。
        if account.get("api_key"):
            return False
        expires_at = float(account.get("expires_at") or 0)
        if not expires_at:
            return not _has_live_credential(account)
        return time.time() > expires_at - REFRESH_MARGIN_SECONDS
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
        elif account.get("platform") == PLATFORM_ZCODE:
            updated = _zcode_refresh_account(pool, account)
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


def _zcode_model_served(account, model):
    wanted = str(model or "").strip().lower()
    if not wanted:
        return True
    names = [str(x or "").strip().lower()
             for x in (account.get("models") or [])]
    if not names:
        return str(account.get("route") or "") != "start"
    return wanted in names


def ensure_account(pool, model=None):
    """挑一个可用账号；必要时先续期。

    轮询按池子做（多账号时把请求摊开），优先挑没被停用、凭据没坏的。
    ZCode 多账号时按模型挑：赠送额度的账号排在前面（先用免费额度）。
    """
    accounts = [a for a in (pool.get("accounts") or [])
                if isinstance(a, dict) and a.get("enabled", True) is not False]
    if not accounts:
        raise PlatformError(503, "这个平台还没有可用账号：先在面板「添加账号」")
    if str(pool.get("platform") or "") == PLATFORM_ZCODE:
        served = [a for a in accounts if _zcode_model_served(a, model)]
        if not served:
            raise PlatformError(400, "这个 ZCode 账号池里没有提供「%s」的账号"
                                % str(model or ""))
        served.sort(key=lambda a: 0 if str(a.get("route") or "") == "start" else 1)
        accounts = served
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

def _zcode_fetch_models(pool):
    """ZCode 的模型清单：官方 provider 配置 + 赠送额度实际能用的模型。

    「刷新模型」时强制重拉一次官方最新配置（远程拿不到就用本机客户端 /
    上次的 / 内置兜底表），所以官方上新模型后刷新就能看到。
    """
    accounts = [a for a in (pool.get("accounts") or [])
                if isinstance(a, dict) and a.get("enabled", True) is not False]
    if not accounts:
        return [], "这个平台还没有可用账号：先在面板「添加账号」"
    official = {}

    def official_rows(base):
        base = str(base or "")
        if base not in official:
            official[base] = _zcode_official_models(base, force=True)
        return official[base]

    def detail(raw):
        wanted = str(raw or "").lower()
        for rows in official.values():
            for row in rows:
                if str(row.get("id") or "").lower() == wanted:
                    return row
        for row in _zcode_fallback_models():
            if row["id"].lower() == wanted:
                return row
        return {"id": raw, "context": 0, "output": 0, "image": False,
                "efforts": []}

    def reasoning_of(raw, info):
        efforts = info.get("efforts") or []
        if efforts:
            return any(str(x) != "none" for x in efforts)
        for row in ZCODE_MODELS:
            if row["id"].lower() == str(raw or "").lower():
                return bool(row.get("reasoning"))
        return False

    rows = []
    seen = set()
    start_models = set()
    errors = []
    for account in accounts:
        route = str(account.get("route") or "")
        names = []
        if route == "start" or (not account.get("api_key") and account.get("jwt")):
            # 预热官方配置（上下文 / 输出 / 图像标记），拿不到也不影响桶模型。
            base = str(account.get("base_url") or "").strip() \
                or _zcode_site_info(account.get("site"))["base"]
            official_rows(base)
            try:
                start = _zcode_start_plan(
                    account.get("jwt"), account.get("device") or "")
            except PlatformError as exc:
                errors.append("%s：%s" % (account.get("label") or "账号", exc.detail))
                start = None
            if start:
                names = [_zcode_model_name(x) for x in (start.get("models") or [])]
                start_models.update(x.lower() for x in names)
                usage_rows = [dict(x) for x in (start.get("buckets") or [])
                              if isinstance(x, dict)]
                changed = (account.get("models") != names
                           or account.get("usage") != usage_rows
                           or float(account.get("expires_at") or 0)
                           != float(start.get("ends_at") or 0))
                account["models"] = names
                account["usage"] = usage_rows
                account["plan"] = start.get("name") or account.get("plan") or ""
                account["expires_at"] = float(start.get("ends_at") or 0)
                if changed:
                    _persist(pool, account)
            elif not names:
                if not errors:
                    errors.append("%s：赠送额度已用完或已过期"
                                  % (account.get("label") or "账号"))
        else:
            base = str(account.get("base_url") or "").strip() \
                or _zcode_site_info(account.get("site"))["base"]
            names = [str(m.get("id") or "") for m in official_rows(base)]
            names = [x for x in names if x]
            if account.get("models") != names:
                account["models"] = names
                _persist(pool, account)
        for raw in names:
            key = str(raw or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            info = detail(raw)
            rows.append({
                "id": raw,
                "name": raw,
                "context": int(info.get("context") or 0),
                "output": int(info.get("output") or 0),
                "vision": bool(info.get("image")),
                "reasoning": reasoning_of(raw, info),
                "tools": True,
                "free": key in start_models,
                "channel": "start" if key in start_models else "coding",
            })
    if rows:
        return rows, ""
    return [], errors[0] if errors else "ZCode 没有返回可用模型"


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
        if platform == PLATFORM_ZCODE:
            return _zcode_fetch_models(pool)
        return [], "不支持的平台：%s" % platform
    except PlatformError as exc:
        return [], exc.detail
    except urllib.error.HTTPError as exc:
        detail = _http_detail(exc)
        return [], "拉取失败（HTTP %d）%s" % (
            exc.code, ("：" + detail) if detail else "")
    except Exception as exc:
        return [], "拉取失败：%s" % exc


def _zcode_balance(pool):
    accounts = [a for a in (pool.get("accounts") or [])
                if isinstance(a, dict) and a.get("enabled", True) is not False]
    if not accounts:
        return {"note": "还没有账号", "at": time.time()}
    total = 0.0
    remaining = 0.0
    names = []
    errors = []
    for account in accounts:
        label = account.get("label") or "ZCode"
        if account.get("plan") and account.get("plan") not in names:
            names.append(str(account.get("plan")))
        if str(account.get("route") or "") == "start" or (
                not account.get("api_key") and account.get("jwt")):
            try:
                start = _zcode_start_plan(
                    account.get("jwt"), account.get("device") or "")
            except PlatformError as exc:
                errors.append("%s：%s" % (label, exc.detail))
                continue
            if start:
                total += float(start.get("total") or 0)
                remaining += float(start.get("remaining") or 0)
                for name in start.get("plans") or []:
                    if name and name not in names:
                        names.append(name)
        else:
            key = str(account.get("api_key") or "")
            if key:
                try:
                    plan = _zcode_plan_name(account.get("site"), key)
                    if plan and plan not in names:
                        names.append(plan)
                except PlatformError as exc:
                    errors.append("%s：%s" % (label, exc.detail))
    note = "、".join(names) if names else "ZCode 账号已就绪"
    out = {"plan": note, "at": time.time()}
    if total > 0:
        out["total_quota"] = int(total)
        out["total_balance"] = int(remaining)
        out["total_used"] = int(max(0, total - remaining))
        out["note"] = note
    elif errors:
        out["error"] = errors[0]
    else:
        out["note"] = note
    return out


def fetch_balance(pool):
    """查询平台余额；返回可落盘的快照。"""
    platform = str(pool.get("platform") or "")
    if platform == PLATFORM_CODEARTS:
        account = ensure_account(pool)
        return _codearts_balance(account)
    if platform == PLATFORM_ZCODE:
        return _zcode_balance(pool)
    return {"note": "这个平台没有公开的余额接口", "at": time.time()}


def fetch_usage(pool):
    """实时拉平台账号的用量（不是登录时的快照）并落盘。

    ZCode：逐账号问一次最新余额（套餐名 / 每个余额桶的用了多少），顺便把
    池子余额也刷新一遍。返回 ``{usage_ok, errors, balance}``。
    """
    platform = str(pool.get("platform") or "")
    if platform != PLATFORM_ZCODE:
        return {"usage_ok": True, "errors": []}
    errors = []
    for account in [a for a in (pool.get("accounts") or [])
                    if isinstance(a, dict)]:
        if account.get("enabled", True) is False:
            continue
        route = str(account.get("route") or "")
        key = str(account.get("api_key") or "")
        jwt = str(account.get("jwt") or "")
        device = str(account.get("device") or "") or _zcode_device_mid()
        label = account.get("label") or "ZCode"
        try:
            if route == "start" or (not key and jwt):
                start = _zcode_start_plan(jwt, device)
                if not start or not start.get("models"):
                    raise PlatformError(401, "ZCode 的赠送额度已用完或已过期：重新登录一次")
                account["usage"] = [dict(x) for x in (start.get("buckets") or [])
                                    if isinstance(x, dict)]
                account["models"] = list(start.get("models") or [])
                account["plan"] = start.get("name") or account.get("plan") or ""
                account["expires_at"] = float(start.get("ends_at") or 0)
            elif key:
                plan = _zcode_plan_name(_zcode_site(account.get("site")), key)
                if not plan:
                    raise PlatformError(401, "这把 Key 背后没有生效的 GLM Coding Plan")
                account["plan"] = plan
            else:
                raise PlatformError(401, "这个 ZCode 账号的登录态不可用：重新登录一次")
            account["status"] = "ok"
            account["last_error"] = ""
        except PlatformError as exc:
            errors.append("%s：%s" % (label, exc.detail))
            account["status"] = "error"
            account["last_error"] = exc.detail
        _persist(pool, account)
    balance = _zcode_balance(pool)
    try:
        import wb_egg_pools
        wb_egg_pools.set_platform_meta(pool.get("id"), "balance", balance)
    except Exception as exc:
        _log("zcode: 落盘余额失败：%s" % exc)
    return {"usage_ok": not errors, "errors": errors[:3], "balance": balance}


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
    # 发给上游的是原始模型 id（对外的 id 带了平台前缀）。
    upstream = upstream_model(pool, model)
    if platform == PLATFORM_ZCODE:
        account = ensure_account(pool, upstream)
        site = _zcode_site(account.get("site"))
        key = str(account.get("api_key") or "").strip()
        jwt = str(account.get("jwt") or "").strip()
        device = str(account.get("device") or "").strip() or _zcode_device_mid()
        route = str(account.get("route") or "")
        body = wb_egg_pools._anthropic_body(chat, upstream)
        converter = wb_egg_pools.AnthropicStream(model)
        if route == "start" or (not key and jwt):
            if not jwt:
                raise PlatformError(401, "这个 ZCode 账号没有可用的登录态：重新登录一次")
            if _zcode_jwt_expired(jwt):
                raise PlatformError(401, "ZCode 的登录态已过期：重新登录一次")
            body = _zcode_dress(body, site, device)
            url = ZCODE_START_BASE + "/v1/messages"
            headers = _zcode_start_headers(jwt)
            return url, body, headers, converter, pool["id"], model
        if not key:
            raise PlatformError(401, "这个 ZCode 账号没有 GLM Coding Plan Key：重新登录一次")
        body = _zcode_mark(body, device)
        base = str(account.get("base_url") or "").strip() \
            or _zcode_site_info(site)["base"]
        url = base.rstrip("/") + "/v1/messages"
        headers = _zcode_coding_headers(key)
        return url, body, headers, converter, pool["id"], model
    body = wb_egg_pools._openai_body(chat, model)
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
