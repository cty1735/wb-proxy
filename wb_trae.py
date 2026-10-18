# -*- coding: utf-8 -*-
"""Trae 上游客户端（国际 coresg/coreva / 国内 trae-api-cn）。

网关对 Trae 做四件事：把账号登进来（本机客户端登录态 / 手动 JWT）、把凭
证养住（ExchangeToken）、把账号状态换成看板认识的形状（模型清单）、把聊天
请求转发出去（llm_utils_chat 的 SSE 由 wb_trae_chat 翻成 OpenAI）。

协议对齐公开的 Trae 客户端实现（ZedeX/trae-local-api）：

* 登录态：桌面端把登录结果写在
  ``%APPDATA%\\Trae CN\\User\\globalStorage\\storage.json`` 的
  ``iCubeAuthInfo://icube.cloudide``；国内版是 ``tc`` 信封（AES-128-CBC +
  SHA-512 校验），国际版多为明文 JSON；
* 刷新：``POST {auth_host}/cloudide/api/v3/trae/oauth/ExchangeToken``；
* 模型清单：``POST {api_host}/api/ide/v1/get_detail_param``；
* 聊天：``POST {api_host}/api/agent/v3/llm_utils_chat``，SSE 事件为
  ``output / done / token_usage / error``。

Trae 没有 Qoder 那种 ``price_factor``：模型清单里不写倍率，看板按「按额度
计费」展示，实际扣减以客户端账单为准。

网络层与账号池共用 wb_upstream_pool 的 keep-alive 池；代理出口通过 proxy
参数传进来，与 WorkBuddy / Qoder 账号同一套行为。
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import wb_upstream_pool


class TraeError(RuntimeError):
    """Trae 协议层错误（凭证 / 请求 / 解密）。"""


#: 国内版 = trae-api-cn；国际版按账号 userRegion 落 SG / US。
REALM_CONFIGS = {
    "cn": {
        "realm": "cn",
        "name": "国内版",
        "site_url": "https://www.trae.cn",
        "api_host": "https://trae-api-cn.mchost.guru",
        # OAuth 换发与刷新都走官方 api.trae.com.cn；trae-api-cn.mchost.guru
        # 只用于聊天/模型目录（两套域名的路径并不完全互通）。
        "auth_host": "https://api.trae.com.cn",
        "oauth_host": "https://www.trae.cn",
        "oauth_exchange_host": "https://api.trae.com.cn",
        "checkin_host": "https://api.trae.cn",
        "domain": "trae.cn",
        "ide_version": "3.3.67",
    },
    "intl": {
        "realm": "intl",
        "name": "国际版",
        "site_url": "https://www.trae.ai",
        "api_host": "https://coresg-normal.trae.ai",
        "auth_host": "https://coresg-normal.trae.ai",
        "oauth_host": "https://www.trae.ai",
        "oauth_exchange_host": "https://api.trae.ai",
        "checkin_host": "https://api.trae.ai",
        "domain": "trae.ai",
        "ide_version": "3.5.51",
    },
}

#: 国际版 US 出口；账号 userRegion=US 时改用它。
US_API_HOST = "https://coreva-normal.trae.ai"

#: 国际版积分 / 权益查询域（网页端 v1 接口；CN 用 api.trae.cn 的 v2）。
INTL_CREDITS_HOST = "https://ug-normal.trae.ai"
INTL_CREDITS_HOST_US = "https://ug-normal.us.trae.ai"

#: 桌面端 OAuth client id（公开的客户端标识，不是用户密钥）。
OAUTH_CLIENT_ID = "ono9krqynydwx5"

#: 浏览器授权页快照（2026-10-18 对齐 Trae CN 2.3.33255）。
#: native_ide 流程：授权页前端调用 GetPCAuthCode 绑定 PKCE challenge，
#: 然后 302 回 auth_callback_url，回调参数是 authCodeInfo（JSON）。
OAUTH_PAGE_PLUGIN_VERSION = "2.3.83560"
OAUTH_PAGE_APP_VERSION = "3.3.100"
OAUTH_PAGE_PLATFORM_CODE = "IDE_PC"

#: AuthCode 交换允许的官方 API 域（回调里的 host 不可信，防止令牌外发）。
EXCHANGE_HOST_ALLOWLIST = (
    "api.trae.com.cn", "api.trae.cn", "api.trae.com", "api.trae.ai",
)

#: 签到接口（国内版；国际版先按同路径探测，失败不伪造成功）。
CHECKIN_REQ_BODY = {"req_source": 2}

DEFAULT_CONTEXT_WINDOW = 128000
MAX_OUTPUT_TOKENS = 128000

#: 聊天 / 模型清单共用的客户端标识与版本号。
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
IDE_VERSION_CODE = "20260401"

#: 模型目录缓存（网关内跨账号共享；按 realm 分开）。
MODEL_CACHE_TTL = 300
_MODEL_CACHE = {}
_MODEL_CACHE_LOCK = threading.Lock()

#: 每账号一次刷新的互斥由 Account 自己的锁负责；这里只管 HTTP 客户端。
_OPENER_CACHE = {}
_OPENER_LOCK = threading.Lock()


def realm_key(realm):
    return "cn" if str(realm or "").lower() == "cn" else "intl"


def realm_config(realm):
    """该区域的端点与站点信息（未知 realm 按国际版处理）。"""
    return dict(REALM_CONFIGS[realm_key(realm)])


def site_domain(realm):
    return realm_config(realm)["domain"]


# ---------------------------------------------------------------------------
# 本机桌面端登录态

def _appdata():
    return os.environ.get("APPDATA") or os.path.expanduser("~")


def storage_candidates(realm):
    """该区域桌面端 storage.json 的候选路径（环境变量优先）。"""
    key = realm_key(realm)
    out = []
    env_dir = os.environ.get("TRAE_DATA_DIR")
    if env_dir:
        out.append(os.path.join(env_dir, "User", "globalStorage", "storage.json"))
    names = (("Trae CN",) if key == "cn" else ("Trae", "Trae CN"))
    for name in names:
        out.append(os.path.join(_appdata(), name, "User", "globalStorage",
                                "storage.json"))
    # macOS / Linux 的官方目录，便于把网关搬到别的机器上也能扫描。
    home = os.path.expanduser("~")
    if key == "cn":
        out.append(os.path.join(home, ".trae-cn", "User", "globalStorage",
                                "storage.json"))
    else:
        out.append(os.path.join(home, ".trae", "User", "globalStorage",
                                "storage.json"))
    seen = []
    for path in out:
        if path and path not in seen:
            seen.append(path)
    return seen


def read_storage(realm):
    """读该区域桌面端的 storage.json；找不到返回 {}。"""
    for path in storage_candidates(realm):
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def storage_path(realm):
    for path in storage_candidates(realm):
        if os.path.isfile(path):
            return path
    return ""


# ---------------------------------------------------------------------------
# 国内版 tc 信封的解密（AES-128-CBC，纯 Python，避免第三方依赖）

#: trae-decrypt.js 里的两组固定盐（公开客户端的硬编码值）。
_SALT_A = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129,
                 243, 215, 251, 124, 227, 57, 130, 155, 47, 255, 135, 52,
                 142, 67, 68, 196, 222, 233, 203, 84, 123, 148, 50, 166, 194,
                 35, 61, 238, 76, 149, 11, 66, 250, 195, 78, 8, 46, 161, 102,
                 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
_SALT_B = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39,
                 128, 236, 95, 96, 81, 127, 169, 25, 181, 74, 13, 45, 229,
                 122, 159, 147, 201, 156, 239, 160, 224, 59, 77, 174, 42, 245,
                 176, 200, 235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126,
                 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])
_SALT_C = bytes([191, 192, 216, 250, 122, 246, 220, 97, 31, 254, 98, 27, 8,
                 72, 71, 176, 135, 99, 96, 18, 127, 101, 203, 104, 211, 102,
                 191, 125, 37, 72, 150, 156, 51, 229, 121, 35, 17, 153, 141,
                 177, 110, 131, 150, 128, 172, 255, 254, 6, 18, 140, 55, 62,
                 236, 249, 135, 64, 135, 12, 117, 4, 89, 149, 168, 209])
_SALT_D = bytes([246, 204, 26, 232, 232, 70, 129, 109, 223, 146, 169, 242,
                 23, 241, 105, 145, 50, 196, 165, 42, 254, 120, 3, 54, 244,
                 207, 209, 85, 53, 6, 138, 106, 175, 148, 31, 204, 186, 186,
                 165, 182, 87, 142, 49, 10, 39, 110, 26, 154, 86, 56, 173,
                 125, 18, 64, 198, 225, 99, 99, 83, 82, 191, 134, 76, 170])

_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b,
    0xfe, 0xd7, 0xab, 0x76, 0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0,
    0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0, 0xb7, 0xfd, 0x93, 0x26,
    0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2,
    0xeb, 0x27, 0xb2, 0x75, 0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0,
    0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84, 0x53, 0xd1, 0x00, 0xed,
    0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f,
    0x50, 0x3c, 0x9f, 0xa8, 0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5,
    0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2, 0xcd, 0x0c, 0x13, 0xec,
    0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14,
    0xde, 0x5e, 0x0b, 0xdb, 0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c,
    0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79, 0xe7, 0xc8, 0x37, 0x6d,
    0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f,
    0x4b, 0xbd, 0x8b, 0x8a, 0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e,
    0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e, 0xe1, 0xf8, 0x98, 0x11,
    0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f,
    0xb0, 0x54, 0xbb, 0x16,
]
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i


def _gmul(a, b):
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return result


def _mix_columns(state):
    out = []
    for col in range(4):
        i = col * 4
        a0, a1, a2, a3 = state[i:i + 4]
        out.extend([
            _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3,
            a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3,
            a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3),
            _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2),
        ])
    return out


def _inv_mix_columns(state):
    out = []
    for col in range(4):
        i = col * 4
        a0, a1, a2, a3 = state[i:i + 4]
        out.extend([
            _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9),
            _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13),
            _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11),
            _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14),
        ])
    return out


class _AES128(object):
    """AES-128 分组的加/解密（Trae 的 tc 信封只需要解密）。"""

    __slots__ = ("_w",)

    def __init__(self, key, decrypt=False):
        if len(key) != 16:
            raise TraeError("AES-128 需要 16 字节密钥，得到 %d" % len(key))
        # 解密用逆序轮密钥；按 4 个 word 一组整体倒序（不是把 44 个 word
        # 逐个倒过来——那会把每轮内部的列顺序也倒掉）。
        words = self._expand(key)
        if decrypt:
            rounds = [words[4 * r:4 * r + 4] for r in range(11)]
            words = [w for rk in reversed(rounds) for w in rk]
        self._w = words

    @staticmethod
    def _expand(key):
        words = [list(key[4 * i:4 * i + 4]) for i in range(4)]
        rcon = 1
        for i in range(4, 44):
            t = list(words[i - 1])
            if i % 4 == 0:
                t = t[1:] + t[:1]
                t = [_SBOX[b] for b in t]
                t[0] ^= rcon
                rcon = _gmul(rcon, 2)
            words.append([words[i - 4][j] ^ t[j] for j in range(4)])
        return words

    def encrypt_block(self, block):
        w = self._w
        s = [block[i] ^ w[i // 4][i % 4] for i in range(16)]
        for rnd in range(1, 10):
            s = [_SBOX[b] for b in s]
            s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]      # ShiftRows
            s = _mix_columns(s)
            base = rnd * 4
            s = [s[i] ^ w[base + i // 4][i % 4] for i in range(16)]
        s = [_SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]
        s = [s[i] ^ w[40 + i // 4][i % 4] for i in range(16)]
        return bytes(s)

    def decrypt_block(self, block):
        w = self._w
        s = [block[i] ^ w[i // 4][i % 4] for i in range(16)]
        for rnd in range(1, 10):
            s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]      # InvShiftRows
            s = [_INV_SBOX[b] for b in s]
            base = rnd * 4
            s = [s[i] ^ w[base + i // 4][i % 4] for i in range(16)]
            s = _inv_mix_columns(s)
        s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]
        s = [_INV_SBOX[b] for b in s]
        s = [s[i] ^ w[40 + i // 4][i % 4] for i in range(16)]
        return bytes(s)


def _aes_cbc_decrypt(key, iv, data):
    """AES-128-CBC 解密 + PKCS#7 去填充；失败返回 None。"""
    if not data or len(data) % 16:
        return None
    cipher = _AES128(key, decrypt=True)
    out = bytearray()
    prev = bytes(iv)
    for offset in range(0, len(data), 16):
        block = bytes(data[offset:offset + 16])
        plain = cipher.decrypt_block(block)
        out.extend(a ^ b for a, b in zip(plain, prev))
        prev = block
    if not out:
        return None
    pad = out[-1]
    if pad < 1 or pad > 16 or out[-pad:] != bytes([pad]) * pad:
        return None
    return bytes(out[:-pad])


def decrypt_tc(blob):
    """解一个国内版 ``tc`` 信封，返回明文 JSON 字符串。

    布局：6 字节头 + 32 字节随机数 + 密文；密钥/IV 由
    ``sha512(sha512(random) + salt)`` 派生；明文前 64 字节是 SHA-512 校验。
    """
    raw = bytes(blob or b"")
    if len(raw) < 6 + 32 + 64 + 16 or raw[:2] != b"tc":
        raise TraeError("不是 Trae tc 信封（长度或魔数不符）")
    header = raw[:6]
    if header == b"tc\x05\x10\x00\x00":
        salt = bytes(a ^ b for a, b in zip(_SALT_A, _SALT_B))
    elif header[:6] == bytes([18, 57, 32, 32, 2, 3]):
        salt = bytes(a ^ b for a, b in zip(_SALT_C, _SALT_D))
    else:
        raise TraeError("未知的 Trae 加密类型: %s" % header.hex())
    random_bytes = raw[6:6 + 32]
    encrypted = raw[6 + 32:]
    digest = hashlib.sha512(random_bytes).digest()
    final = hashlib.sha512(digest + salt).digest()
    plain = _aes_cbc_decrypt(final[:16], final[16:32], encrypted)
    if plain is None:
        raise TraeError("AES 解密失败（密钥或数据损坏）")
    if len(plain) < 64:
        raise TraeError("解密结果过短")
    stored, payload = plain[:64], plain[64:]
    if hashlib.sha512(payload).digest() != stored:
        raise TraeError("解密校验失败（数据损坏）")
    return payload.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 凭证读取 / 规范化

def _decode_auth_value(value):
    """把 storage.json 里的一格登录态还原成 dict；认不出返回 None。"""
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            return None
    try:
        raw = base64.b64decode(text)
    except Exception:
        return None
    if raw[:2] == b"tc":
        try:
            raw = decrypt_tc(raw).encode("utf-8")
        except TraeError:
            return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _as_epoch(value):
    """上游时间字段（ISO 字符串 / 毫秒 / 秒）统一成秒。"""
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e11:
            number /= 1000.0
        return int(number)
    text = str(value).strip()
    if not text:
        return 0
    try:
        number = float(text)
    except ValueError:
        number = 0.0
    if number > 0:
        if number > 1e11:
            number /= 1000.0
        return int(number)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(text, fmt)))
        except ValueError:
            continue
    return 0


def _jwt_claims(token):
    try:
        segment = str(token or "").split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment).decode("utf-8"))
    except Exception:
        return {}


def _user_region(auth):
    region = (auth or {}).get("userRegion")
    if isinstance(region, dict):
        region = region.get("region") or region.get("name") or ""
    return str(region or "").strip().upper()


def api_host_for(realm, auth=None, override=""):
    """该账号实际要打的聊天 / 模型端点。"""
    override = str(override or "").strip().rstrip("/")
    if override:
        return override
    auth = auth or {}
    host = str(auth.get("apiHost") or auth.get("host") or "").strip().rstrip("/")
    if host.startswith("http://") or host.startswith("https://"):
        return host
    if realm_key(realm) == "cn":
        return realm_config("cn")["api_host"]
    if _user_region(auth) == "US":
        return US_API_HOST
    return realm_config("intl")["api_host"]


def credits_host_for(realm, auth=None):
    """积分 / 权益查询域：CN 走 api.trae.cn，国际版走 ug-normal。"""
    if realm_key(realm) == "cn":
        return str(realm_config("cn").get("checkin_host")
                   or "https://api.trae.cn").rstrip("/")
    return (INTL_CREDITS_HOST_US if _user_region(auth) == "US"
            else INTL_CREDITS_HOST)


def auth_from_storage(realm, storage=None):
    """从桌面端登录态里取 auth dict；没有就返回 None。"""
    storage = storage if isinstance(storage, dict) else read_storage(realm)
    if not storage:
        return None
    raw = storage.get("iCubeAuthInfo://icube.cloudide")
    auth = _decode_auth_value(raw)
    if not auth:
        return None
    token = str(auth.get("token") or auth.get("accessToken") or "")
    if not token:
        return None
    auth = dict(auth)
    auth["token"] = token
    auth.setdefault("refreshToken", "")
    auth["machineId"] = str(storage.get("telemetry.machineId") or "")
    auth["sqmId"] = str(storage.get("telemetry.sqmId") or "")
    auth["devDeviceId"] = str(storage.get("telemetry.devDeviceId") or "")
    auth["_realm"] = realm_key(realm)
    auth["_source"] = "desktop"
    return auth


def local_auths():
    """扫描本机两种版本，返回可用的 auth 列表（只读，不写账号池）。"""
    out = []
    seen = set()
    for realm in ("intl", "cn"):
        try:
            auth = auth_from_storage(realm)
        except Exception:
            auth = None
        if auth and auth["token"] not in seen:
            seen.add(auth["token"])
            out.append(auth)
    return out


def credential_from_auth(auth, realm=None):
    """把上游 auth dict 塑造成账号池认识的凭证。"""
    auth = dict(auth or {})
    token = str(auth.get("token") or auth.get("accessToken") or "")
    if not token:
        raise TraeError("Trae 登录态里没有 token")
    realm = realm_key(realm or auth.get("_realm") or "cn")
    claims = _jwt_claims(token)
    claim_data = claims.get("data") if isinstance(claims.get("data"), dict) else {}
    uid = str(auth.get("userId") or claim_data.get("id")
              or claims.get("sub") or claims.get("uid") or "")
    if not uid:
        uid = hashlib.sha1(token.encode("utf-8")).hexdigest()[:16]
    account = auth.get("account")
    nickname = ""
    if isinstance(account, str):
        nickname = account
    elif isinstance(account, dict):
        nickname = str(account.get("username") or account.get("name")
                       or account.get("email") or "")
    if not nickname:
        nickname = str(claim_data.get("username") or claim_data.get("name")
                       or claims.get("name") or "")
    expires = _as_epoch(auth.get("expiredAt")) or int(claims.get("exp") or 0)
    host = api_host_for(realm, auth)
    # 把协议态里后续要用的字段原样带过去（刷新时会用旧 auth 重建凭证）。
    # 只复制已知字段，避免把 token / 临时对象写进账号档。
    kept = {}
    for key in (
        "deviceId", "devicePublicKey", "appVersion", "marketUserId",
        "sessionId", "lastCheckin", "checkinAt", "checkinMessage",
        "checkinReward", "checkinCampaigns", "credits", "creditsAt",
        "userRegion", "ideVersion", "refreshExpiredAt", "tokenReleaseAt",
        "sqmId", "devDeviceId",
    ):
        if key in auth:
            kept[key] = auth.get(key)
    trae_auth = {
        "userId": uid,
        "name": nickname,
        "account": account if isinstance(account, (str, dict)) else "",
        "machineId": str(auth.get("machineId") or ""),
        "sqmId": str(auth.get("sqmId") or ""),
        "devDeviceId": str(auth.get("devDeviceId") or ""),
        "apiHost": host,
        "userRegion": auth.get("userRegion") or "",
        "refreshExpiredAt": _as_epoch(auth.get("refreshExpiredAt")),
        "tokenReleaseAt": _as_epoch(auth.get("tokenReleaseAt")),
        "source": str(auth.get("_source") or "desktop"),
    }
    trae_auth.update(kept)
    trae_auth.setdefault("deviceId", "")
    trae_auth.setdefault("devicePublicKey", "")
    trae_auth.setdefault("appVersion", "")
    trae_auth.setdefault("marketUserId", uid)
    out = {
        "uid": uid,
        "nickname": nickname,
        "realm": realm,
        "domain": urllib.parse.urlsplit(realm_config(realm)["site_url"]).netloc,
        "accessToken": token,
        "refreshToken": str(auth.get("refreshToken") or ""),
        "expiresAt": expires,
        "traeAuth": trae_auth,
    }
    return out


def credential_from_token(token, realm="cn", api_host=""):
    """面板手动粘贴 JWT 的入口（不要求本机装 Trae）。

    手动 JWT 没有随附设备身份，但 Trae 的聊天/签到会校验设备指纹。这里优先
    复用本机 icube-dc 设备（同机登录的 token 能对上）；跨机器粘贴的 token 可能
    过不了设备校验，签到失败会如实回给面板，不伪造成功。
    """
    token = str(token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise TraeError("请填写 Trae 的 JWT（Cloud-IDE-JWT）")
    realm = realm_key(realm)
    device = oauth_device(realm)
    auth = {
        "token": token,
        "refreshToken": "",
        "expiredAt": int(_jwt_claims(token).get("exp") or 0),
        "apiHost": str(api_host or "").strip().rstrip("/"),
        "deviceId": device.get("deviceId") or "",
        "machineId": device.get("machineId") or "",
        "devicePublicKey": device.get("publicKeyPEM") or "",
        "appVersion": device.get("appVersion") or "",
        "_realm": realm,
        "_source": "manual",
    }
    return credential_from_auth(auth, realm)


# ---------------------------------------------------------------------------
# OAuth（浏览器授权添加账号）
#
# 与 WorkBuddy 的「浏览器里点一下、面板自动入池」一致：网关先签发一个
# native_ide 授权 URL（PKCE + login_trace_id），浏览器完成登录后 Trae 会
# 302 回本机 /authorize；网关用回调里的 AuthCode 换 token，再写入账号池。
# 本机 icube-dc 设备凭证（P-256 公钥）随 DeviceInfo 一起提交，保证登录、
# 刷新、签到三条链路使用同一台设备指纹。

def _credential_storage(realm):
    return read_storage(realm)


def local_device_credentials(realm):
    """读取本机 Trae 客户端的 icube-dc 设备凭证（公钥/私钥 PEM）。

    只读本机自有凭证；私钥仅用于可选的 DeviceProof（当前登录主路径只传
    公钥），调用方按需取用，不写日志。
    """
    storage = _credential_storage(realm)
    if not storage:
        return []
    machine_id = str(storage.get("telemetry.machineId") or "")
    out = []
    seen = set()
    for key, value in storage.items():
        if not str(key).startswith("iCubeAuthInfo://icube-dc:"):
            continue
        device_id = str(key).rsplit(":", 1)[-1].strip()
        if not device_id or device_id in seen:
            continue
        data = _decode_auth_value(value)
        if not isinstance(data, dict):
            continue
        public_key = str(data.get("publicKeyPEM") or "")
        private_key = str(data.get("privateKeyPEM") or "")
        if not public_key and not private_key:
            continue
        seen.add(device_id)
        out.append({
            "deviceId": device_id,
            "machineId": machine_id,
            "publicKeyPEM": public_key,
            "privateKeyPEM": private_key,
            "appVersion": str(data.get("appVersion") or ""),
        })
    return out


def oauth_device(realm, used=None):
    """本次 OAuth 登录使用的设备身份（优先本机真实 icube 凭证）。

    ``used`` 是 {deviceId: 已绑定账号数}。Trae 服务端按设备限制可绑定账号
    数，多账号时优先选绑定最少的本机设备，避免所有账号挤在同一台设备上。
    """
    realm = realm_key(realm)
    creds = local_device_credentials(realm)
    if creds:
        used = used or {}
        creds.sort(key=lambda item: (int(used.get(item.get("deviceId"), 0)),
                                     str(item.get("deviceId") or "")))
        return dict(creds[0])
    # 没有本机客户端凭证时仍可尝试浏览器授权：用稳定的 machine_id 派生
    # device_id，ExchangeToken 的旧形态/无公钥变体会兜底。
    machine_id = str(_credential_storage(realm).get("telemetry.machineId") or "")
    if not machine_id:
        machine_id = secrets.token_hex(16)
    device_id = synthetic_device_id(machine_id) or "".join(
        str(secrets.randbelow(10)) for _ in range(15))
    return {
        "deviceId": device_id,
        "machineId": machine_id,
        "publicKeyPEM": "",
        "privateKeyPEM": "",
        "appVersion": "",
    }


def _pkce_pair():
    """RFC 7636：43-128 字符 verifier + BASE64URL(SHA256) challenge。"""
    verifier = secrets.token_hex(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def oauth_login_url(realm, callback_url, device=None):
    """生成 Trae 浏览器授权 URL 与会话状态。"""
    realm = realm_key(realm)
    cfg = realm_config(realm)
    device = dict(device or oauth_device(realm))
    trace_id = secrets.token_hex(32)
    verifier, challenge = _pkce_pair()
    host = str(cfg.get("oauth_host") or cfg["site_url"]).rstrip("/")
    hostname = socket.gethostname() or "Windows-PC"
    params = [
        ("login_version", "1"),
        ("auth_from", "trae"),
        ("login_channel", "native_ide"),
        ("plugin_version", OAUTH_PAGE_PLUGIN_VERSION),
        ("auth_type", "local"),
        ("client_id", OAUTH_CLIENT_ID),
        ("redirect", "0"),
        ("login_trace_id", trace_id),
        ("auth_callback_url", str(callback_url or "")),
        ("machine_id", device.get("machineId") or ""),
        ("device_id", device.get("deviceId") or ""),
        ("x_device_id", device.get("deviceId") or ""),
        ("x_machine_id", device.get("machineId") or ""),
        ("x_device_brand", hostname),
        ("x_device_type", "windows"),
        ("x_os_version", "Windows"),
        ("x_env", ""),
        ("x_app_version", OAUTH_PAGE_APP_VERSION),
        ("x_app_type", "stable"),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("channel_name", "common"),
    ]
    auth_url = host + "/authorization?" + urllib.parse.urlencode(params)
    return {
        "authUrl": auth_url,
        "state": trace_id,
        "verifier": verifier,
        "deviceId": device.get("deviceId") or "",
        "machineId": device.get("machineId") or "",
        "publicKeyPEM": device.get("publicKeyPEM") or "",
        # DeviceProof 需要私钥；只在网关进程内保存，不写登录日志/不返回前端。
        "privateKeyPEM": device.get("privateKeyPEM") or "",
        "realm": realm,
        "redirectUri": str(callback_url or ""),
        "appVersion": device.get("appVersion") or OAUTH_PAGE_APP_VERSION,
    }


def _param(params, *names):
    for name in names:
        if name in params and params.get(name) not in (None, ""):
            return params.get(name)
    lowered = {str(k).lower(): v for k, v in (params or {}).items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return ""


def _json_param(params, *names):
    raw = _param(params, *names)
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def parse_oauth_callback(value):
    """解析浏览器回调 URL（或直接粘贴的 query string / 参数字典）。"""
    if isinstance(value, dict):
        params = {}
        for key, item in value.items():
            if isinstance(item, list) and item:
                item = item[-1]
            params[str(key)] = item
    else:
        text = str(value or "").strip()
        if not text:
            raise TraeError("回调 URL 为空")
        if "?" in text:
            query_text = urllib.parse.urlsplit(text).query
        else:
            query_text = text
        query = urllib.parse.parse_qs(query_text, keep_blank_values=True)
        params = {str(k): (v[-1] if isinstance(v, list) and v else v)
                  for k, v in query.items()}

    auth_code = ""
    info = _json_param(params, "authCodeInfo", "auth_code_info")
    if info:
        auth_code = str(info.get("AuthCode") or info.get("auth_code") or "")
    if not auth_code:
        auth_code = str(_param(params, "AuthCode", "authCode", "code") or "")

    user_info = _json_param(params, "userInfo", "user_info")
    user_jwt = _json_param(params, "userJwt", "user_jwt")
    user_id = str(_param(params, "UserID", "userId", "user_id")
                  or user_info.get("UserID") or user_info.get("UserId") or "")
    user_name = str(_param(params, "userName", "user_name", "nickname")
                    or user_info.get("ScreenName") or user_info.get("NickName")
                    or user_info.get("Name") or "")
    avatar = str(_param(params, "avatar", "AvatarUrl")
                 or user_info.get("AvatarUrl") or "")
    access_token = str(_param(params, "accessToken", "access_token")
                       or user_jwt.get("Token") or user_jwt.get("token")
                       or user_jwt.get("AccessToken") or "")

    return {
        "authCode": auth_code,
        "refreshToken": str(_param(params, "refreshToken", "refresh_token") or ""),
        "accessToken": access_token,
        "userId": user_id,
        "userName": user_name,
        "avatar": avatar,
        "host": str(_param(params, "host") or ""),
        "userRegion": _param(params, "userRegion", "user_region")
        or user_info.get("UserRegion") or "",
        "loginTraceID": str(_param(params, "loginTraceID", "login_trace_id") or ""),
        "raw": params,
    }


def _validated_exchange_host(host, realm):
    """回调里的 host 只允许官方 API 域，防止令牌被转发到第三方。"""
    candidate = str(host or "").strip()
    if candidate:
        bare = candidate.split("://", 1)[-1].strip("/").split("/", 1)[0]
        bare = bare.rsplit("@", 1)[-1].split(":", 1)[0].lower()
        if bare in EXCHANGE_HOST_ALLOWLIST:
            # 只取主机名，丢弃回调里可能夹带的路径 / query / 用户信息，
            # 也强制 https，避免把 AuthCode 发到奇怪的位置。
            return "https://" + bare
    return str(realm_config(realm).get("oauth_exchange_host")
               or realm_config(realm)["auth_host"]).rstrip("/")


# ---------------------------------------------------------------------------
# DeviceProof：P-256 ECDSA（纯 Python，避免给网关加 cryptography 依赖）
#
# Trae 的 /cloudide/api/v3/trae/oauth/ExchangeToken 在部分账号上要求
# DeviceProof（实测 20405 "Device proof required"）。签名原文与客户端一致：
#   POST\n<path>\n<ClientID>\n<AuthCode>\n<Timestamp>\n<Nonce>
# 签名用本机 icube 设备凭证里的 P-256 私钥，输出 P1363（r||s 64B）或 DER。
# ---------------------------------------------------------------------------

_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_A = _P256_P - 3
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5


def _der_tlv(data, offset):
    """读一个 DER TLV，返回 (tag, value, next_offset)。"""
    if offset >= len(data):
        raise TraeError("DER 提前结束")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise TraeError("DER 长度缺失")
    length = data[offset]
    offset += 1
    if length & 0x80:
        count = length & 0x7F
        if count <= 0 or offset + count > len(data):
            raise TraeError("DER 长度非法")
        length = int.from_bytes(data[offset:offset + count], "big")
        offset += count
    if offset + length > len(data):
        raise TraeError("DER 内容越界")
    return tag, data[offset:offset + length], offset + length


def _pem_der(pem):
    text = str(pem or "").strip()
    if "-----BEGIN" not in text:
        raise TraeError("设备私钥不是 PEM")
    payload = "".join(line.strip() for line in text.splitlines()
                      if line.strip() and not line.strip().startswith("-----"))
    try:
        return base64.b64decode(payload)
    except Exception as exc:
        raise TraeError("设备私钥 base64 解码失败: %s" % exc)


def _der_children(sequence):
    out = []
    offset = 0
    while offset < len(sequence):
        tag, value, offset = _der_tlv(sequence, offset)
        out.append((tag, value))
    return out


def parse_p256_private_key(pem):
    """从 PKCS#8 / SEC1 PEM 里取出 P-256 私钥整数。"""
    der = _pem_der(pem)
    tag, sequence, _ = _der_tlv(der, 0)
    if tag != 0x30:
        raise TraeError("设备私钥不是 ASN.1 SEQUENCE")
    children = _der_children(sequence)
    # PKCS#8: INTEGER version + SEQUENCE algorithm + OCTET STRING ECPrivateKey
    if (len(children) >= 3 and children[0][0] == 0x02
            and children[1][0] == 0x30 and children[2][0] == 0x04):
        inner_tag, inner, _ = _der_tlv(children[2][1], 0)
        if inner_tag == 0x30:
            for child_tag, child_value in _der_children(inner):
                if child_tag == 0x04 and 0 < len(child_value) <= 32:
                    return int.from_bytes(child_value, "big")
    # SEC1: 直接是 ECPrivateKey
    for child_tag, child_value in children:
        if child_tag == 0x04 and 0 < len(child_value) <= 32:
            return int.from_bytes(child_value, "big")
    raise TraeError("无法从 PEM 里解析 P-256 私钥")


def parse_p256_public_key(pem):
    """从 SubjectPublicKeyInfo PEM 里取出 P-256 公钥点 (x, y)。"""
    der = _pem_der(pem)
    tag, sequence, _ = _der_tlv(der, 0)
    if tag != 0x30:
        raise TraeError("设备公钥不是 ASN.1 SEQUENCE")
    for child_tag, child_value in _der_children(sequence):
        if child_tag == 0x03 and child_value and child_value[0] == 0:
            point = child_value[1:]
            if len(point) == 65 and point[0] == 0x04:
                return (int.from_bytes(point[1:33], "big"),
                        int.from_bytes(point[33:65], "big"))
    raise TraeError("无法从 PEM 里解析 P-256 公钥")


def _p256_add(p1, p2):
    """椭圆曲线点加（仿射坐标，None 表示无穷远点）。"""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P256_P == 0:
        return None
    if p1 == p2:
        if y1 == 0:
            return None
        slope = (3 * x1 * x1 + _P256_A) * pow(2 * y1, _P256_P - 2, _P256_P)
    else:
        slope = (y2 - y1) * pow(x2 - x1, _P256_P - 2, _P256_P)
    slope %= _P256_P
    x3 = (slope * slope - x1 - x2) % _P256_P
    y3 = (slope * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _p256_mul(k, point=None):
    point = point or (_P256_GX, _P256_GY)
    result = None
    addend = point
    while k:
        if k & 1:
            result = _p256_add(result, addend)
        addend = _p256_add(addend, addend)
        k >>= 1
    return result


def _p256_rfc6979_k(private, digest):
    """RFC 6979 的确定性 k（与 p256 crate 的默认签名一致）。"""
    # bits2octets(H(m))：P-256 的 n 与 SHA-256 同为 256 bit，先按 n 约减。
    z = int.from_bytes(digest, "big")
    if z >= _P256_N:
        z -= _P256_N
    h = z.to_bytes(32, "big")
    x = private.to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < _P256_N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def p256_sign(message, private, low_s=True):
    """用 P-256 私钥签 SHA-256 消息，返回 (r, s)。"""
    digest = hashlib.sha256(message).digest()
    e = int.from_bytes(digest, "big")
    k = _p256_rfc6979_k(private, digest)
    point = _p256_mul(k)
    if point is None:
        raise TraeError("P-256 签名得到无穷远点")
    r = point[0] % _P256_N
    if r == 0:
        raise TraeError("P-256 签名 r=0")
    s = (pow(k, _P256_N - 2, _P256_N) * (e + r * private)) % _P256_N
    if s == 0:
        raise TraeError("P-256 签名 s=0")
    if low_s and s > _P256_N // 2:
        s = _P256_N - s
    return r, s


def p256_verify(message, signature, public):
    """验签（只用于自检，不参与线上请求）。"""
    r, s = signature
    if not (1 <= r < _P256_N and 1 <= s < _P256_N):
        return False
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = pow(s, _P256_N - 2, _P256_N)
    point = _p256_add(_p256_mul((e * w) % _P256_N),
                      _p256_mul((r * w) % _P256_N, public))
    return point is not None and point[0] % _P256_N == r


def _p256_signature_bytes(r, s, fmt):
    if fmt == "der":
        def integer(value):
            raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
            if raw[0] & 0x80:
                raw = b"\x00" + raw
            return b"\x02" + bytes([len(raw)]) + raw
        body = integer(r) + integer(s)
        return b"\x30" + bytes([len(body)]) + body
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def device_proof(private_key_pem, method, path, client_id, auth_code,
                 timestamp=None, nonce=None, low_s=True, fmt="p1363"):
    """生成 DeviceProof（默认 P1363；返回 (proof, raw_signature)）。"""
    private = parse_p256_private_key(private_key_pem)
    ts = int(timestamp if timestamp is not None else time.time())
    nonce = str(nonce or secrets.token_hex(32))
    msg = ("%s\n%s\n%s\n%s\n%s\n%s"
           % (method, path, client_id, auth_code, ts, nonce)).encode("utf-8")
    r, s = p256_sign(msg, private, low_s=low_s)
    proof = {
        "Signature": base64.b64encode(
            _p256_signature_bytes(r, s, fmt)).decode("ascii"),
        "Timestamp": ts,
        "Nonce": nonce,
    }
    return proof, {"r": r, "s": s, "message": msg}


def _exchange_tokens(status, raw):
    """从 ExchangeToken 响应里取 AccessToken / RefreshToken。"""
    detail = raw[:300].decode("utf-8", "replace")
    if status < 200 or status >= 300:
        raise TraeError("HTTP %s: %s" % (status, detail))
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise TraeError("响应不是 JSON: %s" % detail)
    if not isinstance(data, dict):
        raise TraeError("响应形状异常: %s" % detail)
    err = ((data.get("ResponseMetadata") or {}).get("Error") or {})
    if err and str(err.get("Code") or "0") not in ("0", ""):
        raise TraeError("code=%s/%s: %s" % (
            err.get("Code"), err.get("StandardCode") or "",
            err.get("Message") or "未知错误"))
    code = data.get("code")
    if code not in (None, 0, "0"):
        raise TraeError("code=%s: %s" % (code, data.get("message") or "未知错误"))
    container = data.get("Result") or data.get("data") or data.get("Data") or data
    token = _find_token(container, ("Token", "AccessToken", "accessToken",
                                   "token", "Jwt", "JWT"))
    refresh = _find_token(container, ("RefreshToken", "refreshToken"))
    if not token:
        raise TraeError("响应里没有 Token: %s" % detail)
    return {
        "accessToken": token,
        "refreshToken": refresh,
        "expiresAt": _as_epoch(container.get("TokenExpireAt")
                               or container.get("ExpiredAt")
                               or container.get("expiredAt")),
        "refreshExpiredAt": _as_epoch(
            container.get("RefreshTokenExpireAt")
            or container.get("refreshExpiredAt")),
        "tokenReleaseAt": _as_epoch(
            container.get("TokenReleaseAt") or container.get("tokenReleaseAt")),
        "raw": data,
    }


def exchange_auth_code(code, verifier, host, device, realm="cn", proxy=""):
    """用授权页回传的 AuthCode 换 token。

    主形态对齐真实客户端：``{host}/trae/api/v3/oauth/ExchangeToken`` +
    DeviceInfo（DeviceID + DevicePublicKey）。部分账号的 ``/cloudide/...``
    会要求 DeviceProof（20405），因此本机有 icube 私钥时优先补上
    DeviceProof 变体（P1363 / DER）；旧形态（AuthCode/Code）作最后兜底。
    全部失败才抛错，错误里保留每个变体的 URL / 状态 / 响应体。
    """
    code = str(code or "").strip()
    if not code:
        raise TraeError("回调里没有 AuthCode")
    realm = realm_key(realm)
    device = dict(device or {})
    device_id = str(device.get("deviceId") or "").strip()
    machine_id = str(device.get("machineId") or "").strip()
    public_key = str(device.get("publicKeyPEM") or "")
    app_version = str(device.get("appVersion") or OAUTH_PAGE_APP_VERSION)
    host = _validated_exchange_host(host, realm)
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "x-device-id": device_id,
        "x-app-id": APP_ID,
        "x-platform-code": OAUTH_PAGE_PLATFORM_CODE,
    }
    errors = []

    variants = []
    if public_key:
        variants.append((
            "DeviceInfo",
            host + "/trae/api/v3/oauth/ExchangeToken",
            {
                "ClientID": OAUTH_CLIENT_ID,
                "AuthCode": code,
                "CodeVerifier": str(verifier or ""),
                "DeviceInfo": {
                    "DeviceID": device_id,
                    "MachineID": machine_id,
                    "PlatformCode": OAUTH_PAGE_PLATFORM_CODE,
                    "DeviceType": "PC",
                    "DeviceName": "",
                    "DeviceModel": "",
                    "ClientVersion": app_version,
                    "DevicePublicKey": public_key,
                    "DeviceBrand": "",
                    "DeviceCPU": "",
                    "OSInfo": "",
                    "OSVersion": "",
                },
                "IDEVersion": app_version,
            },
        ))
    auth_host = str(realm_config(realm).get("auth_host") or "").rstrip("/")
    legacy = {
        "ClientID": OAUTH_CLIENT_ID,
        "AuthCode": code,
        "CodeVerifier": str(verifier or ""),
        "DeviceID": device_id,
        "PlatformCode": OAUTH_PAGE_PLATFORM_CODE,
    }

    # DeviceProof 变体：部分账号的 /cloudide/... 会直接 20405
    # "Device proof required"，必须用本机 icube 私钥签
    # POST\n<path>\n<ClientID>\n<AuthCode>\n<Timestamp>\n<Nonce>。
    # P1363 是真实客户端（WebCrypto）的首选，DER 保留作兼容探测。
    private_key = str(device.get("privateKeyPEM") or "")
    if private_key:
        proof_targets = [
            ("Proof", host + "/cloudide/api/v3/trae/oauth/ExchangeToken"),
        ]
        if auth_host and auth_host != host:
            proof_targets.append(
                ("Proof-auth",
                 auth_host + "/cloudide/api/v3/trae/oauth/ExchangeToken"))
        for base_tag, url in proof_targets:
            sign_path = urllib.parse.urlsplit(url).path
            for fmt in ("p1363", "der"):
                try:
                    proof, _raw = device_proof(
                        private_key, "POST", sign_path, OAUTH_CLIENT_ID,
                        code, fmt=fmt)
                except Exception as exc:
                    errors.append("%s/%s 生成失败: %s" % (base_tag, fmt, exc))
                    continue
                variants.append((base_tag + "-" + fmt, url, {
                    "ClientID": OAUTH_CLIENT_ID,
                    "AuthCode": code,
                    "CodeVerifier": str(verifier or ""),
                    "DeviceID": device_id,
                    "PlatformCode": OAUTH_PAGE_PLATFORM_CODE,
                    "DeviceProof": proof,
                }))

    variants.append(("AuthCode-cloudide",
                      host + "/cloudide/api/v3/trae/oauth/ExchangeToken",
                      dict(legacy)))
    variants.append(("Code-cloudide",
                      host + "/cloudide/api/v3/trae/oauth/ExchangeToken",
                      dict(legacy, Code=code)))
    # 官方刷新域也试一次：部分环境下回调 host 与真实交换域不同。
    if auth_host and auth_host != host:
        variants.append(("AuthCode-auth-host",
                          auth_host + "/cloudide/api/v3/trae/oauth/ExchangeToken",
                          dict(legacy)))
        variants.append(("Code-auth-host",
                          auth_host + "/cloudide/api/v3/trae/oauth/ExchangeToken",
                          dict(legacy, Code=code)))

    for tag, url, body in variants:
        req_headers = dict(headers)
        if tag == "DeviceInfo" or tag.startswith("Proof"):
            # 真实客户端 m("") 头形态：DeviceInfo / DeviceProof 变体必须
            # 存在但为空串（缺失会 20403）；旧形态不带这个头。
            req_headers["x-cloudide-token"] = ""
        try:
            status, raw = _request(url, method="POST", headers=req_headers,
                                   body=body, timeout=40, proxy=proxy)
            result = _exchange_tokens(status, raw)
            result["variant"] = tag
            return result
        except Exception as exc:
            errors.append("%s: %s" % (tag, exc))
    raise TraeError("AuthCode 交换失败 → " + " | ".join(errors))


# ---------------------------------------------------------------------------
# 签到 / 积分

def _checkin_headers(access_token, auth, realm):
    token = str(access_token or "").strip()
    if token.lower().startswith("cloud-ide-jwt "):
        authorization = token
    else:
        authorization = "Cloud-IDE-JWT " + token
    auth = auth or {}
    device = _device_info(auth)
    uid = str(auth.get("userId") or "")
    market_uid = str(auth.get("marketUserId") or uid)
    session_id = str(auth.get("sessionId") or "") or uuid.uuid4().hex
    cn = realm_key(realm) == "cn"
    return {
        "accept": "*/*",
        "accept-language": "zh-CN" if cn else "en-US",
        "authorization": authorization,
        "content-type": "application/json",
        "user-agent": ("VSCode 1.107.1 (TRAE SOLO CN)" if cn
                       else "VSCode 1.107.1 (TRAE)"),
        "x-market-client-id": "VSCode 1.107.1",
        "x-market-user-id": market_uid,
        "x-user-region": (_user_region(auth) or ("CN" if cn else "US")),
        "x-device-id": device["device_id"],
        "x-lgw-req-sdk-type": "3",
        "package-type": "stable_cn" if cn else "stable",
        "x-request-id": secrets.token_hex(16),
        "x-lscbd-aid": "787976",
        "x-lscbd-platform": "windows",
        "app-version": "0.1.45",
        "x-tt-trace-id": "00-%s-01" % secrets.token_hex(8),
        "vscode-sessionid": session_id,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "none",
    }


def _unwrap_payloads(data):
    """按 data/result/resp/response/info 逐层展开（外层优先）。"""
    scopes = []
    if isinstance(data, dict):
        scopes.append(data)
        for key in ("data", "result", "resp", "response", "info"):
            child = data.get(key)
            if isinstance(child, dict):
                scopes.extend(_unwrap_payloads(child))
    return scopes


def _payload_field(data, *keys):
    for scope in _unwrap_payloads(data):
        for key in keys:
            if key in scope:
                value = scope.get(key)
                if value is not None:
                    return value
        lowered = {str(k).lower(): v for k, v in scope.items()}
        for key in keys:
            if key.lower() in lowered and lowered[key.lower()] is not None:
                return lowered[key.lower()]
    return None


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        try:
            number = float(text)
            return int(number) if number.is_integer() else None
        except ValueError:
            return None
    return None


def _checkin_result(status, raw, action):
    detail = raw[:300].decode("utf-8", "replace")
    if status < 200 or status >= 300:
        return {"ok": False, "http": status, "error": "HTTP %s: %s" % (status, detail)}
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"ok": False, "http": status, "error": "非 JSON 响应: %s" % detail}
    if not isinstance(data, dict):
        return {"ok": False, "http": status, "error": "响应形状异常: %s" % detail}
    code = data.get("code")
    try:
        code_int = int(code) if code is not None else 0
    except (TypeError, ValueError):
        code_int = None
    message = str(data.get("message") or data.get("msg") or "")
    checked = _payload_field(data, "checked_in", "checkedIn")
    if isinstance(checked, (int, float)) and not isinstance(checked, bool):
        checked = bool(checked)
    enable = _payload_field(data, "enable", "enabled")
    credits = _as_int(_payload_field(data, "credits", "balance"))
    reward = _as_int(_payload_field(
        data, "points", "reward", "reward_credits", "claim_credits",
        "checkin_credits", "delta", "increase", "obtain", "gained", "amount"))
    ok = code_int in (0, None)
    return {
        "ok": bool(ok),
        "http": status,
        "code": code_int,
        "message": message,
        "checked_in": checked if isinstance(checked, bool) else None,
        "enable": enable if isinstance(enable, bool) else None,
        "credits": credits,
        "reward": reward,
        "raw": data,
        "action": action,
    }


def checkin_status(access_token, auth, realm, proxy="", timeout=30):
    """查询今日签到状态与（可能的）积分余额。"""
    realm = realm_key(realm)
    host = str(realm_config(realm).get("checkin_host")
               or realm_config(realm)["site_url"]).rstrip("/")
    url = host + "/trae/api/v2/ug/checkin_credits/status"
    status, raw = _request(url, method="POST",
                           headers=_checkin_headers(access_token, auth, realm),
                           body=CHECKIN_REQ_BODY, timeout=timeout, proxy=proxy)
    return _checkin_result(status, raw, "status")


def checkin_claim(access_token, auth, realm, proxy="", timeout=30):
    """领取今日签到积分。"""
    realm = realm_key(realm)
    host = str(realm_config(realm).get("checkin_host")
               or realm_config(realm)["site_url"]).rstrip("/")
    url = host + "/trae/api/v2/ug/checkin_credits/claim"
    status, raw = _request(url, method="POST",
                           headers=_checkin_headers(access_token, auth, realm),
                           body=CHECKIN_REQ_BODY, timeout=timeout, proxy=proxy)
    return _checkin_result(status, raw, "claim")


def _usage_points(data):
    """从用量/权益响应里算剩余积分（各字段名做宽容匹配）。"""
    for scope in _unwrap_payloads(data):
        packs = (scope.get("user_entitlement_pack_list")
                 or scope.get("userEntitlementPackList"))
        if isinstance(packs, list) and packs:
            total = 0.0
            found = False
            for pack in packs:
                if not isinstance(pack, dict):
                    continue
                base = pack.get("entitlement_base_info") or pack
                limit = base.get("credits_limit")
                if limit is None:
                    limit = base.get("creditsLimit")
                if limit is None:
                    # 免费套餐/功能包没有 credits_limit（只有 enable_solo_*），
                    # 不能当成 0 积分；跳过它，继续累加真正按积分计的包。
                    continue
                usage = pack.get("usage") or {}
                used = usage.get("credits_amount")
                if used is None:
                    used = usage.get("creditsAmount")
                try:
                    remain = float(limit or 0) - float(used or 0)
                except (TypeError, ValueError):
                    continue
                if remain < 0:
                    remain = 0
                total += remain
                found = True
            if found:
                return int(total)
    for scope in _unwrap_payloads(data):
        summary = scope.get("usage_summary") or scope.get("usageSummary")
        if isinstance(summary, dict):
            total = _as_int(summary.get("total_amount")
                            if summary.get("total_amount") is not None
                            else summary.get("totalAmount"))
            if total is not None:
                consumed = _as_int(summary.get("consumed_amount")
                                   if summary.get("consumed_amount") is not None
                                   else summary.get("consumedAmount")) or 0
                return max(total - consumed, 0)
            for key in ("credits_remain", "remaining_credits", "creditsRemaining",
                        "credits_balance", "balance", "credits", "total_credits"):
                value = _as_int(summary.get(key))
                if value is not None:
                    return value
    for scope in _unwrap_payloads(data):
        for key in ("credits_remain", "remaining_credits", "credits_balance",
                    "balance", "credits", "total_credits"):
            value = _as_int(scope.get(key))
            if value is not None:
                return value
    return None


def fetch_credits(access_token, auth, realm, proxy="", timeout=30):
    """查询 Trae 积分余额；拿不到返回 ok=False，不编 0。

    CN 走 api.trae.cn 的 v2 路径；国际版走 ug-normal 的 v1 路径（网页端
    GetIdeUserEntUsage / GetUserCurrentEntitlementList 的接口契约）。
    """
    realm = realm_key(realm)
    headers = _checkin_headers(access_token, auth, realm)
    if realm == "cn":
        host = credits_host_for(realm, auth)
        candidates = [
            (host + "/trae/api/v2/pay/ide_user_ent_usage", {}),
            (host + "/trae/api/v2/pay/user_current_entitlement_list",
             {"require_usage": True}),
        ]
    else:
        host = credits_host_for(realm, auth)
        hosts = [host]
        other = (INTL_CREDITS_HOST if host == INTL_CREDITS_HOST_US
                 else INTL_CREDITS_HOST_US)
        if other and other not in hosts:
            hosts.append(other)
        candidates = []
        for item in hosts:
            candidates.extend([
                (item + "/trae/api/v1/pay/ide_user_ent_usage",
                 {"require_usage": True, "req_source": 1}),
                (item + "/trae/api/v1/pay/user_current_entitlement_list",
                 {"require_usage": True, "req_source": 1}),
            ])
    errors = []
    for url, body in candidates:
        try:
            status, raw = _request(url, method="POST", headers=headers,
                                   body=body, timeout=timeout, proxy=proxy)
            detail = raw[:200].decode("utf-8", "replace")
            if status < 200 or status >= 300:
                errors.append("%s HTTP %s: %s" % (url, status, detail))
                continue
            data = json.loads(raw.decode("utf-8", "replace"))
            err = ((data.get("ResponseMetadata") or {}).get("Error") or {})
            if err and str(err.get("Code") or "0") not in ("0", ""):
                errors.append("%s: %s: %s" % (url, err.get("Code"),
                                              err.get("Message") or ""))
                continue
            points = _usage_points(data)
            if points is not None:
                return {"ok": True, "points": points, "raw": data,
                        "host": str(url).split("/trae/", 1)[0]}
            errors.append("%s 响应里没有积分字段: %s" % (url, detail))
        except Exception as exc:
            errors.append("%s: %s" % (url, exc))
    return {"ok": False, "error": " | ".join(errors) or "没有可用的积分接口"}


# ---------------------------------------------------------------------------
# 网络层

def _opener_for_proxy(proxy):
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    with _OPENER_LOCK:
        opener = _OPENER_CACHE.get(proxy)
        if opener is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            _OPENER_CACHE[proxy] = opener
        return opener


def _urlopen(req, timeout=30, proxy=""):
    if wb_upstream_pool.enabled():
        try:
            return wb_upstream_pool.urlopen(req, timeout=timeout, proxy=proxy)
        except wb_upstream_pool.PoolBypass:
            pass
    opener = _opener_for_proxy(proxy)
    if opener is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return opener.open(req, timeout=timeout)


def _request(url, method="GET", headers=None, body=None, timeout=30, proxy=""):
    """一次请求，返回 (status, bytes)；非 2xx 不抛异常。"""
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=dict(headers or {}))
    try:
        resp = _urlopen(req, timeout=timeout, proxy=proxy)
        try:
            return int(getattr(resp, "status", 200)), resp.read()
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except Exception:
            data = b""
        return int(exc.code), data
    except Exception as exc:
        raise TraeError("Trae 网络请求失败: %s" % exc)


# ---------------------------------------------------------------------------
# 设备指纹 / 请求头

def hash_device_id(machine_id):
    """Java 风格 32 位哈希，补零到 19 位（客户端同款）。"""
    if not machine_id:
        return ""
    value = 0
    for ch in str(machine_id):
        value = ((value << 5) - value + ord(ch)) & 0xFFFFFFFF
        if value >= 0x80000000:
            value -= 0x100000000
    return str(abs(value)).rjust(19, "0")


def synthetic_device_id(machine_id):
    """按本机 machineId 派生 15 位数字设备号（真实 Trae deviceId 的形态）。

    真实客户端首次启动会生成 15 位数字 deviceId；``hash_device_id`` 是 19 位
    的旧派生值，只适合做请求头占位。没有本机 icube 凭证时用这个 15 位值，
    避免登录/签到被设备指纹校验直接判形态不合法。
    """
    digits = hash_device_id(machine_id)
    if not digits:
        return ""
    return digits[-15:].rjust(15, "0")


def _device_info(auth):
    auth = auth or {}
    machine_id = str(auth.get("machineId") or "")
    return {
        "cpu": os.environ.get("TRAE_CPU") or "Intel",
        # OAuth 账号有真实 icube 设备 id（与授权页/签到同源），优先用它；
        # 旧账号没有这个字段时仍按 machineId 派生，行为不变。
        "device_id": str(auth.get("deviceId") or "").strip()
        or synthetic_device_id(machine_id)
        or os.environ.get("TRAE_DEVICE_ID") or "",
        "machine_id": machine_id or os.environ.get("TRAE_MACHINE_ID") or "",
        "device_model": os.environ.get("TRAE_DEVICE_MODEL") or "82RF",
        "os_name": os.environ.get("TRAE_OS_NAME") or "windows",
        "os_version": os.environ.get("TRAE_OS_VERSION") or "Windows 10",
    }


def ide_version(realm, auth=None):
    override = os.environ.get("TRAE_IDE_VERSION")
    if override:
        return override
    stored = str((auth or {}).get("ideVersion") or "")
    if stored:
        return stored
    return realm_config(realm)["ide_version"]


def build_common_headers(access_token, auth, realm):
    """聊天 / 目录共用的客户端头。"""
    auth = auth or {}
    uid = str(auth.get("userId") or "")
    device = _device_info(auth)
    trace = uuid.uuid4().hex
    version = ide_version(realm, auth)
    return {
        "Content-Type": "application/json",
        "Authorization": "Cloud-IDE-JWT %s" % access_token,
        "X-Cloudide-Token": str(access_token),
        "x-app-id": os.environ.get("TRAE_APP_ID") or APP_ID,
        "x-app-version": "default",
        "x-ide-version-code": os.environ.get("TRAE_IDE_VERSION_CODE")
        or IDE_VERSION_CODE,
        "x-app-version-code": os.environ.get("TRAE_IDE_VERSION_CODE")
        or IDE_VERSION_CODE,
        "x-custom-trace-id": trace,
        "x-device-brand": device["device_model"],
        "x-device-cpu": device["cpu"],
        "x-device-id": device["device_id"],
        "x-machine-id": device["machine_id"],
        "x-os-version": device["os_version"],
        "x-device-type": device["os_name"],
        "x-ide-version": version,
        "x-ide-version-type": "stable",
        "request-traffic-type": "prod",
        "x-uid": uid,
    }


def build_stream_headers(access_token, auth, realm, request_id=None):
    headers = build_common_headers(access_token, auth, realm)
    request_id = request_id or str(uuid.uuid4())
    headers["Accept"] = "text/event-stream"
    headers["Cache-Control"] = "no-cache"
    headers["X-Request-ID"] = request_id
    headers["X-Trae-Request-ID"] = request_id
    return headers


# ---------------------------------------------------------------------------
# 刷新 / 账号状态

def refresh_credential(realm, auth, refresh_token=None, proxy="", api_host=""):
    """用 refreshToken 换一套新 token；失败抛 TraeError。"""
    auth = dict(auth or {})
    refresh = str(refresh_token or auth.get("refreshToken") or "")
    if not refresh:
        raise TraeError("没有 refreshToken，请重新登录 Trae")
    host = str(os.environ.get("TRAE_AUTH_HOST") or api_host or "").strip().rstrip("/") \
        or realm_config(realm)["auth_host"]
    url = host + "/cloudide/api/v3/trae/oauth/ExchangeToken"
    body = {
        "ClientID": os.environ.get("TRAE_OAUTH_CLIENT_ID") or OAUTH_CLIENT_ID,
        "RefreshToken": refresh,
        "ClientSecret": os.environ.get("TRAE_OAUTH_CLIENT_SECRET") or "-",
        "UserID": "",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    device_id = str((auth or {}).get("deviceId") or "").strip()
    if device_id:
        # 真实客户端的刷新请求也带设备头；缺了不致命，带上更贴近原生链路。
        headers["x-device-id"] = device_id
        headers["x-app-id"] = APP_ID
        headers["x-platform-code"] = OAUTH_PAGE_PLATFORM_CODE
        headers["x-cloudide-token"] = ""
    status, raw = _request(url, method="POST", body=body, timeout=30,
                           proxy=proxy, headers=headers)
    detail = raw[:300].decode("utf-8", "replace")
    if status < 200 or status >= 300:
        raise TraeError("Trae 刷新失败 (%d): %s" % (status, detail))
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise TraeError("Trae 刷新返回的不是 JSON")
    if isinstance(data, dict):
        err = ((data.get("ResponseMetadata") or {}).get("Error") or {})
        if err and str(err.get("Code") or "0") not in ("0", ""):
            raise TraeError("Trae 刷新被拒绝: %s %s" % (
                err.get("Code"), err.get("Message") or ""))
        code = data.get("code")
        if code not in (None, 0, "0"):
            raise TraeError("Trae 刷新被拒绝: code=%s %s" % (
                code, data.get("message") or ""))
        container = data.get("Result") or data.get("data") or data
    else:
        container = {}
    token = _find_token(container, ("Token", "AccessToken", "accessToken",
                                   "token", "Jwt", "JWT"))
    if not token:
        raise TraeError("Trae 刷新返回里没有 token: %s" % detail)
    new_refresh = _find_token(container, ("RefreshToken", "refreshToken")) or refresh
    merged = dict(auth)
    merged.update({
        "token": token,
        "refreshToken": new_refresh,
        "expiredAt": _as_epoch(container.get("TokenExpireAt")
                                or container.get("ExpiredAt")
                                or container.get("expiredAt"))
        or _as_epoch(_jwt_claims(token).get("exp")),
        "refreshExpiredAt": _as_epoch(
            container.get("RefreshTokenExpireAt")
            or container.get("refreshExpiredAt")),
        "tokenReleaseAt": _as_epoch(
            container.get("TokenReleaseAt") or container.get("tokenReleaseAt")),
    })
    return credential_from_auth(merged, realm)


def _find_token(data, keys):
    """在响应容器里按候选键找第一个非空字符串（大小写不敏感）。"""
    if isinstance(data, str):
        return data.strip() or ""
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def fetch_account_status(access_token, auth, realm, proxy="", timeout=15):
    """尽力取一次账号状态（chat_mode）；拿不到返回 {}，不影响账号可用。"""
    host = api_host_for(realm, auth)
    url = host + "/api/v1/commercial/chat_mode"
    try:
        status, raw = _request(
            url, method="POST", body={}, timeout=timeout, proxy=proxy,
            headers=build_common_headers(access_token, auth, realm))
        if 200 <= status < 300:
            data = json.loads(raw.decode("utf-8", "replace"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# 模型目录

def _first_int(entry, *names):
    for name in names:
        value = entry.get(name)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def _entry_tags(entry):
    blob = " ".join(str(entry.get(k) or "").lower() for k in
                    ("name", "display_name", "config_name", "description",
                     "category", "type"))
    return blob


def normalise_model_entry(entry):
    """把 get_detail_param 的一条记录塑造成网关的模型元数据。"""
    if not isinstance(entry, dict):
        return None
    config = (entry.get("config_name") or entry.get("configName")
              or entry.get("model") or entry.get("id") or "")
    config = str(config).strip()
    if not config:
        return None
    if entry.get("enable") is False or entry.get("enabled") is False:
        return None
    name = str(entry.get("display_name") or entry.get("displayName")
               or entry.get("name") or config).strip() or config
    context = _first_int(entry, "context_window", "contextWindow",
                         "max_input_tokens", "maxInputTokens",
                         "token_limit", "max_context_tokens")
    max_out = _first_int(entry, "max_output_tokens", "maxOutputTokens",
                         "max_tokens", "maxTokens")
    tags = _entry_tags(entry)
    reasoning = bool(entry.get("is_reasoning") or entry.get("reasoning")
                     or entry.get("supports_reasoning")
                     or entry.get("thinking") or "reason" in tags
                     or "思考" in tags)
    vision = bool(entry.get("is_vl") or entry.get("multimodal")
                  or entry.get("supports_vision")
                  or "vision" in tags or "vl" in tags or "image" in tags)
    tools = entry.get("supports_tool_call")
    if tools is None:
        tools = entry.get("toolcall_compatible", True)
    efforts = []
    thinking = entry.get("thinking_config") or entry.get("thinkingConfig")
    if isinstance(thinking, dict):
        enabled = thinking.get("enabled")
        if isinstance(enabled, dict) and isinstance(enabled.get("efforts"), dict):
            efforts = sorted(str(k) for k in enabled["efforts"].keys())
    model = {
        "id": config,
        "name": name,
        "key": config,
        "context": context or DEFAULT_CONTEXT_WINDOW,
        "max_tokens": max_out or MAX_OUTPUT_TOKENS,
        "reasoning": reasoning,
        "supports_effort": bool(efforts),
        "efforts": efforts,
        "input": ["text", "image"] if vision else ["text"],
        "supports_tool_call": bool(tools),
        # Trae 没有 per-model price_factor：留空，看板显示「按额度计费」。
        "credits": "",
        "source": "remote",
    }
    return model


def _extract_entries(data):
    """在 get_detail_param 的各种包装里找到模型数组。"""
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("config_infos", "configInfos", "models", "model_list",
                "modelList", "configs"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            inner = _extract_entries(value)
            if inner:
                return inner
    inner = data.get("data") or data.get("result") or data.get("body")
    if isinstance(inner, (list, dict)):
        return _extract_entries(inner)
    return []


def fetch_model_list(access_token, auth, realm, proxy="", timeout=25):
    """拉一次上游模型目录，返回规范化后的条目列表。"""
    host = api_host_for(realm, auth)
    url = host + "/api/ide/v1/get_detail_param"
    body = {
        "function": "chat_v3",
        "config_names": None,
        "need_prompt": False,
        "current_config_info": None,
        "poly_prompt": True,
        "mode_type": None,
        "agent_type": None,
    }
    headers = build_common_headers(access_token, auth, realm)
    status, raw = _request(url, method="POST", body=body, timeout=timeout,
                           proxy=proxy, headers=headers)
    if status < 200 or status >= 300:
        detail = raw[:300].decode("utf-8", "replace")
        raise TraeError("Trae 模型目录失败 (%d): %s" % (status, detail))
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise TraeError("Trae 模型目录返回的不是 JSON")
    entries = _extract_entries(data)
    models = []
    seen = set()
    for entry in entries:
        model = normalise_model_entry(entry)
        if not model or model["id"] in seen:
            continue
        seen.add(model["id"])
        models.append(model)
    return models


#: 静态兜底清单：只用于「还没有 Trae 账号」时的面板展示。真正的模型 id
#: 以账号目录（get_detail_param）为准；这里给的是国内版常见的 config_name。
_STATIC_MODELS = {
    "cn": [
        ("glm-5.2", "GLM-5.2", True, False, 200000),
        ("glm-5.1", "GLM-5.1", True, False, 200000),
        ("glm-5", "GLM-5", True, False, 128000),
        ("glm-4.7", "GLM-4.7", True, False, 128000),
        ("glm-4.6", "GLM-4.6", True, False, 128000),
        ("DeepSeek-V4-Pro", "DeepSeek-V4-Pro", True, False, 128000),
        ("DeepSeek-V4-Flash", "DeepSeek-V4-Flash", False, False, 128000),
        ("deepseek-V3.1", "DeepSeek-V3.1", True, False, 128000),
        ("Doubao-Seed-2.0-Code", "Doubao-Seed-2.0-Code", True, True, 256000),
        ("Doubao-Seed-2.1-Pro", "Doubao-Seed-2.1-Pro", True, True, 256000),
        ("Doubao-Seed-2.1-Turbo", "Doubao-Seed-2.1-Turbo", True, True, 256000),
        ("kimi-k2.7-code", "Kimi-K2.7-Code", True, False, 256000),
        ("kimi-k2.6", "Kimi-K2.6", True, False, 256000),
        ("kimi-k2.5", "Kimi-K2.5", True, False, 128000),
        ("minimax-m3", "MiniMax-M3", True, False, 200000),
        ("minimax-m2.7", "MiniMax-M2.7", True, False, 200000),
        ("qwen-3.7-plus", "Qwen3.7-Plus", True, False, 128000),
        ("qwen-3.6-plus", "Qwen3.6-Plus", True, False, 128000),
        ("qwen-3.5", "Qwen3.5", True, False, 128000),
        ("qwen3-coder", "Qwen3-Coder", True, False, 128000),
        ("glm-5v-turbo", "GLM-5V-Turbo", True, True, 128000),
    ],
    "intl": [
        ("claude-opus-4-7", "Claude Opus 4.7", True, True, 200000),
        ("claude-opus-4-6", "Claude Opus 4.6", True, True, 200000),
        ("claude-opus-4-5", "Claude Opus 4.5", True, True, 200000),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6", True, True, 200000),
        ("claude-sonnet-4-5", "Claude Sonnet 4.5", True, True, 200000),
        ("claude-haiku-4-5", "Claude Haiku 4.5", True, True, 200000),
        ("gpt-5.2", "GPT-5.2", True, True, 400000),
        ("gpt-5.1", "GPT-5.1", True, True, 400000),
        ("gemini-3-pro", "Gemini 3 Pro", True, True, 1000000),
        ("deepseek-v4-pro", "DeepSeek-V4-Pro", True, False, 128000),
        ("kimi-k2.7-code", "Kimi-K2.7-Code", True, False, 256000),
        ("qwen3.7-max", "Qwen3.7-Max", True, False, 128000),
    ],
}

#: 国内版客户端的模型别名 -> 上游 config_name（参考公开客户端的映射表）。
_CN_ALIASES = {
    "claude-opus-4-7": "glm-5.2",
    "claude-opus-4-6": "glm-5.2",
    "claude-opus-4-5": "glm-5.2",
    "claude-opus-4-5-20251101": "glm-5.2",
    "claude-sonnet-4-6": "glm-5.2",
    "claude-sonnet-4-5": "glm-5.2",
    "claude-sonnet-4-5-20250929": "glm-5.2",
    "claude-haiku-4-5": "glm-5.1",
    "claude-haiku-4-5-20251001": "glm-5.1",
    "gpt-4o": "custom_model_gpt-5",
    "gpt-4o-mini": "custom_model_gpt-5",
    "gpt-5": "custom_model_gpt-5",
    "gemini-2.0-flash": "custom_model_gemini",
    "gemini-2.5-pro": "custom_model_vercel_gemini",
    "deepseek-v3": "DeepSeek-V4-Pro",
    "deepseek-v4-pro": "DeepSeek-V4-Pro",
    "deepseek-v4-flash": "DeepSeek-V4-Flash",
    "deepseek-v3-1": "deepseek-V3.1",
    "kimi-k2-7-code": "kimi-k2.7-code",
    "kimi-k2-6": "kimi-k2.6",
    "kimi-k2-5": "kimi-k2.5",
    "qwen3.7-plus": "qwen-3.7-plus",
    "qwen3.6-plus": "qwen-3.6-plus",
    "qwen-3-5": "qwen-3.5",
    "doubao-seed-2-1-pro": "Doubao-Seed-2.1-Pro",
    "doubao-seed-2-1-turbo": "Doubao-Seed-2.1-Turbo",
    "doubao-seed-code": "Doubao_1_6",
    "doubao-1-6": "Doubao_1_6",
    "doubao-1.8": "doubao_1_8",
}


def alias_for(realm, model_id):
    """把客户端常见别名换成上游 config_name（只国内版需要）。"""
    name = str(model_id or "").strip()
    if realm_key(realm) != "cn":
        return name
    lowered = name.lower()
    if lowered in _CN_ALIASES:
        return _CN_ALIASES[lowered]
    for alias, config in _CN_ALIASES.items():
        if alias in lowered:
            return config
    return name


def static_models(realm):
    """内置兜底清单（形状与 normalise_model_entry 的输出一致）。"""
    key = realm_key(realm)
    models = []
    for mid, name, reasoning, vision, context in _STATIC_MODELS.get(key, ()):
        models.append({
            "id": mid,
            "name": name,
            "key": mid,
            "context": context,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "reasoning": reasoning,
            "supports_effort": False,
            "efforts": [],
            "input": ["text", "image"] if vision else ["text"],
            "supports_tool_call": True,
            "credits": "",
            "source": "system",
        })
    return models


def available_models(realm, access_token=None, auth=None, proxy="", force=False):
    """账号可用就拉远端目录，否则回内置清单；返回 (models, source)。"""
    key = realm_key(realm)
    now = time.time()
    if not force:
        with _MODEL_CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
            if cached and now - cached[0] < MODEL_CACHE_TTL:
                return cached[1]
    if access_token:
        try:
            models = fetch_model_list(access_token, auth or {}, key, proxy=proxy)
            if models:
                with _MODEL_CACHE_LOCK:
                    _MODEL_CACHE[key] = (time.time(), models)
                return models, "remote"
        except Exception:
            pass
    models = static_models(key)
    return models, "system"


def cached_models(realm):
    """只读缓存（不发上游）；给「按模型归属猜 provider」用。"""
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(realm_key(realm))
    return list(cached[1]) if cached else []


def model_entry_meta(model):
    """把一份模型元数据翻译成 wb_proxy.model_entry 认识的 meta 字典。"""
    model = model or {}
    meta = {
        "name": model.get("name"),
        "supportsImages": "image" in (model.get("input") or []),
        "supportsReasoning": bool(model.get("reasoning")),
        "supportsToolCall": model.get("supports_tool_call", True),
        "maxInputTokens": model.get("context"),
        "maxOutputTokens": model.get("max_tokens"),
        "ownedBy": "trae",
        "vendor": "Trae",
    }
    if model.get("efforts"):
        meta["reasoning"] = {"supportedEfforts": list(model["efforts"])}
    # 没有 price_factor：显式带上说明，面板显示「按额度计费」而不是「-」。
    meta["credits_note"] = "Trae 按账号额度计费，没有 per-model 倍率"
    return meta
