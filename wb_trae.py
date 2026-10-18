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
import json
import os
import re
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
        "auth_host": "https://trae-api-cn.mchost.guru",
        "domain": "trae.cn",
        "ide_version": "3.3.67",
    },
    "intl": {
        "realm": "intl",
        "name": "国际版",
        "site_url": "https://www.trae.ai",
        "api_host": "https://coresg-normal.trae.ai",
        "auth_host": "https://coresg-normal.trae.ai",
        "domain": "trae.ai",
        "ide_version": "3.5.51",
    },
}

#: 国际版 US 出口；账号 userRegion=US 时改用它。
US_API_HOST = "https://coreva-normal.trae.ai"

#: 桌面端 OAuth client id（公开的客户端标识，不是用户密钥）。
OAUTH_CLIENT_ID = "ono9krqynydwx5"

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
    out = {
        "uid": uid,
        "nickname": nickname,
        "realm": realm,
        "domain": urllib.parse.urlsplit(realm_config(realm)["site_url"]).netloc,
        "accessToken": token,
        "refreshToken": str(auth.get("refreshToken") or ""),
        "expiresAt": expires,
        "traeAuth": {
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
        },
    }
    return out


def credential_from_token(token, realm="cn", api_host=""):
    """面板手动粘贴 JWT 的入口（不要求本机装 Trae）。"""
    token = str(token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise TraeError("请填写 Trae 的 JWT（Cloud-IDE-JWT）")
    realm = realm_key(realm)
    auth = {
        "token": token,
        "refreshToken": "",
        "expiredAt": int(_jwt_claims(token).get("exp") or 0),
        "apiHost": str(api_host or "").strip().rstrip("/"),
        "_realm": realm,
        "_source": "manual",
    }
    return credential_from_auth(auth, realm)


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


def _device_info(auth):
    auth = auth or {}
    machine_id = str(auth.get("machineId") or "")
    return {
        "cpu": os.environ.get("TRAE_CPU") or "Intel",
        "device_id": hash_device_id(machine_id)
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
    status, raw = _request(url, method="POST", body=body, timeout=30,
                           proxy=proxy,
                           headers={"Content-Type": "application/json"})
    if status < 200 or status >= 300:
        detail = raw[:300].decode("utf-8", "replace")
        raise TraeError("Trae 刷新失败 (%d): %s" % (status, detail))
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise TraeError("Trae 刷新返回的不是 JSON")
    token = str(data.get("token") or data.get("accessToken") or "")
    if not token:
        raise TraeError("Trae 刷新返回里没有 token")
    merged = dict(auth)
    merged.update({
        "token": token,
        "refreshToken": str(data.get("refreshToken") or refresh),
        "expiredAt": _as_epoch(data.get("expiredAt")) or _as_epoch(
            _jwt_claims(token).get("exp")),
        "refreshExpiredAt": _as_epoch(data.get("refreshExpiredAt")),
        "tokenReleaseAt": _as_epoch(data.get("tokenReleaseAt")),
    })
    return credential_from_auth(merged, realm)


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
