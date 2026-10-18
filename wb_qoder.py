# -*- coding: utf-8 -*-
"""Qoder 上游客户端（国际 qoder.sh / 国内 qoder.com.cn）。

网关对 Qoder 只做三件事：把账号登进来（浏览器设备码 / PAT）、把凭证养住
（refresh）、把账号状态换成看板认识的形状（额度、模型清单）。Qoder 的聊天
通道（algo SSE）尚未接入，聊天请求在路由层直接拒绝，所以这个模块里没有
任何聊天代码。

协议对齐 pi-provider-qoder（simonsmh/pi-provider-qoder）：

* 设备码登录（仅国际）：打开 qoder.com/device/selectAccounts?challenge=..&
  challenge_method=S256&machine_id=..&nonce=..，再轮询 openapi
  /api/v1/deviceToken/poll；202/404 = 等待中；
* PAT 兑换：POST openapi /api/v1/jobToken/exchange {personal_token: "pt-..."}，
  返回短命的 job token（jt-...）与 job refresh token；
* 刷新：POST center /algo/api/v3/user/refresh_token {refreshToken}；
* 身份：GET openapi /api/v1/userinfo；
* 额度：GET openapi /api/v2/quota/usage；
* 模型清单：GET {base}algo/api/v2/model/list?Encode=1，带 COSY 签名头。

COSY 签名需要 AES-128-CBC 与 RSA PKCS#1 v1.5 公钥运算。本项目（含绿色包）
不带任何第三方依赖，这里用纯 Python 实现；AES 的 S 盒/列混合与
wb_atrest._AES256 共用同一条公式（都是为了避开 pip 依赖）。

网络层与账号池共用 wb_upstream_pool 的 keep-alive 池；代理出口（面板上的
「出口」）通过 proxy 参数传进来，与 WorkBuddy 账号同一套行为。
"""

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import wb_upstream_pool
from wb_atrest import _SBOX, _MUL2, _MUL3, _mix_columns

# ---------------------------------------------------------------------------
# 常量与区域配置

#: 国际 / 国内两个区域的上游端点。字段与 pi-provider-qoder 的 QoderRegionConfig 对齐。
REGIONS = {
    "intl": {
        "name": "国际版 (Global)",
        "provider_id": "qoder",
        "base_url": "https://api3.qoder.sh/",
        "openapi_url": "https://openapi.qoder.sh",
        "center_url": "https://center.qoder.sh",
        "site_url": "https://qoder.com",
        "pat_url": "https://qoder.com/account/integrations",
        "device_login_url": "https://qoder.com/device/selectAccounts",
        "supports_browser_login": True,
    },
    "cn": {
        "name": "国内版 (China)",
        "provider_id": "qoder-cn",
        "base_url": "https://gateway.qoder.com.cn/",
        "openapi_url": "https://openapi.qoder.com.cn",
        "center_url": "https://gateway.qoder.com.cn",
        "site_url": "https://qoder.com.cn",
        "pat_url": "https://qoder.com.cn/account/integrations",
        "device_login_url": "",
        "supports_browser_login": False,
    },
}

#: 网关自身在 Qoder 上游的名字（User-Agent），只做标识，不参与签名。
USER_AGENT = "qoder-gateway"

#: PAT（个人访问令牌）前缀；兑换前不能直接用来认证。
PAT_PREFIX = "pt-"

#: 模型清单不给每模型输出上限，pi 侧按上游文档取 131072 作为全局默认。
MAX_OUTPUT_TOKENS = 131072

#: 上游目录不给 context_config 时的上下文兜底（pi 同值）。
DEFAULT_CONTEXT_WINDOW = 1000000

#: 随包内置的 COSY 客户端版本；有效值由 refresh_cosy_version() 动态跟进
#: （网关按 COSY 版本裁剪模型清单，落后会拿到缩减版目录）。
BUNDLED_GATEWAY_COSY_VERSION = "1.1.72"

#: openapi 侧（PAT 兑换 / userinfo）用的 COSY 版本，与 CLI 版本无关。
OPENAPI_COSY_VERSION = "1.0.1"

#: COSY 客户端类型魔术值（官方 CLI 同值）。
COSY_CLIENT_TYPE = "5"

#: COSY 版本缓存文件名（放在账号目录，随网关数据一起搬）。
COSY_VERSION_CACHE_FILE = "qoder-cosy-version.json"

#: COSY 版本检查节流窗口（24h）与请求超时。
COSY_VERSION_INTERVAL = 24 * 3600
COSY_VERSION_TIMEOUT = 8

#: 官方 CLI 的版本发布源（qoder.com/changelog?type=cli 背后的接口）。
CLI_RELEASES_URL = ("https://repo.qoder.com/repos/QoderAI/changelog-CLI/"
                    "releases?per_page=5&page=1")

#: 设备码轮询参数：2s 一次，最多 90 次（3 分钟）；与 pi 相同。
DEVICE_POLL_INTERVAL = 2.0
DEVICE_POLL_ATTEMPTS = 90

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class QoderError(RuntimeError):
    """Qoder 协议/网络错误（调用方按普通字符串展示）。"""


def realm_config(realm):
    """区域配置；未知 realm 一律回落到国际版。"""
    return REGIONS.get(str(realm or "").lower()) or REGIONS["intl"]


def supports_browser_login(realm):
    return bool(realm_config(realm).get("supports_browser_login"))


# ---------------------------------------------------------------------------
# 纯 Python AES-128-CBC（COSY 载荷加密）与 RSA PKCS#1 v1.5 公钥加密

class _AES128(object):
    """AES-128 分组加密（COSY 只需要正向加密）。状态按列主序排成 16 字节。

    与 wb_atrest._AES256 同一套公式，只是 Nk=4 / Nr=10。
    """

    __slots__ = ("_w",)

    def __init__(self, key):
        if len(key) != 16:
            raise QoderError("AES-128 需要 16 字节密钥，得到 %d" % len(key))
        words = [list(key[4 * i:4 * i + 4]) for i in range(4)]
        rcon = 1
        for i in range(4, 44):
            t = list(words[i - 1])
            if i % 4 == 0:
                t = t[1:] + t[:1]
                t = [_SBOX[b] for b in t]
                t[0] ^= rcon
                rcon = _MUL2[rcon]
            words.append([words[i - 4][j] ^ t[j] for j in range(4)])
        self._w = words

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


def _aes_cbc_encrypt(key, plaintext):
    """AES-128-CBC（PKCS#7 填充）。COSY 的 IV 就是密钥本身（与官方实现一致）。"""
    if not key:
        raise QoderError("COSY aes key 为空")
    cipher = _AES128(key)
    pad = 16 - (len(plaintext) % 16)
    data = plaintext + bytes([pad]) * pad
    out = bytearray()
    prev = bytes(key)
    for offset in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[offset:offset + 16], prev))
        prev = cipher.encrypt_block(block)
        out.extend(prev)
    return bytes(out)


# COSY 公钥（1024 位）。只做公钥加密，私钥在上游。
_QODER_RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


def _der_read(data, pos):
    """极简 DER 读取：返回 (tag, value_bytes, next_pos)。只覆盖公钥用的几种结构。"""
    if pos + 2 > len(data):
        raise QoderError("RSA 公钥 DER 截断")
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or pos + count > len(data):
            raise QoderError("RSA 公钥 DER 长度非法")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if pos + length > len(data):
        raise QoderError("RSA 公钥 DER 越界")
    return tag, data[pos:pos + length], pos + length


def _rsa_public_numbers():
    b64 = "".join(line for line in _QODER_RSA_PUBLIC_KEY_PEM.splitlines()
                  if line and not line.startswith("-----"))
    der = base64.b64decode(b64)
    _tag, outer, _pos = _der_read(der, 0)              # SubjectPublicKeyInfo
    _tag, _alg, pos = _der_read(outer, 0)              # AlgorithmIdentifier
    _tag, bitstring, _pos = _der_read(outer, pos)      # BIT STRING
    _tag, rsa_seq, _pos = _der_read(bitstring[1:], 0)  # 去掉 unused-bits 字节
    _tag, modulus, pos = _der_read(rsa_seq, 0)
    _tag, exponent, _pos = _der_read(rsa_seq, pos)
    return int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big")


_RSA_NUMBERS = None
_RSA_LOCK = threading.Lock()


def _rsa_encrypt_b64(message):
    """RSA PKCS#1 v1.5 公钥加密，返回 base64。"""
    global _RSA_NUMBERS
    with _RSA_LOCK:
        if _RSA_NUMBERS is None:
            _RSA_NUMBERS = _rsa_public_numbers()
    modulus, exponent = _RSA_NUMBERS
    size = (modulus.bit_length() + 7) // 8
    if len(message) > size - 11:
        raise QoderError("RSA 明文过长")
    # PKCS#1 v1.5: 00 02 | PS(>=8 个非零随机字节) | 00 | message
    pad_len = size - 3 - len(message)
    padding = bytes(b or 1 for b in secrets.token_bytes(pad_len))
    block = b"\x00\x02" + padding + b"\x00" + message
    value = pow(int.from_bytes(block, "big"), exponent, modulus)
    return base64.b64encode(value.to_bytes(size, "big")).decode("ascii")


# ---------------------------------------------------------------------------
# 网络层（keep-alive 池 + 每账号代理出口）

_OPENER_CACHE = {}
_OPENER_LOCK = threading.Lock()


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
    """一次请求，返回 (status, body_bytes)。

    非 2xx 不抛异常而是把状态码交回调用方：设备码轮询把 202/404 当作
    「还在等」，需要按状态分支。传输层错误才抛 QoderError。
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _urlopen(req, timeout=timeout, proxy=proxy) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        except Exception:
            return exc.code, b""
    except Exception as exc:
        raise QoderError("%s %s 请求失败: %s" % (method, url, exc))


def _json_request(url, method="GET", headers=None, body=None, timeout=30, proxy="",
                  what="qoder"):
    """请求 + JSON 解码；非 2xx 或 JSON 解析失败时抛 QoderError。

    本模块用到的端点顶层都是对象（token/quota/userinfo/目录），顶层不是
    对象一律按协议异常处理。
    """
    status, raw = _request(url, method=method, headers=headers, body=body,
                           timeout=timeout, proxy=proxy)
    text = raw.decode("utf-8", "replace")
    if status < 200 or status >= 300:
        raise QoderError("%s 返回 HTTP %d: %s" % (what, status, text[:200]))
    try:
        data = json.loads(text)
    except Exception:
        raise QoderError("%s 返回非 JSON: %s" % (what, text[:120]))
    if not isinstance(data, dict):
        raise QoderError("%s 返回了意外的 JSON 形状" % what)
    return data


# ---------------------------------------------------------------------------
# machine_id：与官方 CLI 同一个来源顺序，最好复用本机已登录设备的识别码

_MACHINE_ID_CACHE = ""
_MACHINE_ID_LOCK = threading.Lock()
MACHINE_ID_FILE = "qoder-machine-id"


def machine_id(data_dir=None):
    """本机 Qoder 设备识别码。

    依次读官方 CLI 的 `~/.qoder/.auth/machine_id`、pi 的
    `~/.pi/agent/qoder-machine-id`；都没有时生成一个并持久化到网关自己的
    数据目录（不往官方目录里写东西）。设置 QODER_MACHINE_ID 环境变量可覆盖。
    """
    global _MACHINE_ID_CACHE
    with _MACHINE_ID_LOCK:
        if _MACHINE_ID_CACHE:
            return _MACHINE_ID_CACHE
        env = str(os.environ.get("QODER_MACHINE_ID") or "").strip()
        if env:
            _MACHINE_ID_CACHE = env
            return env
        home = os.path.expanduser("~")
        for path in (os.path.join(home, ".qoder", ".auth", "machine_id"),
                     os.path.join(home, ".pi", "agent", "qoder-machine-id")):
            try:
                with open(path, encoding="utf-8") as fh:
                    value = fh.read().strip()
                if value:
                    _MACHINE_ID_CACHE = value
                    return value
            except OSError:
                continue
        own_path = os.path.join(data_dir, MACHINE_ID_FILE) if data_dir else ""
        if own_path:
            try:
                with open(own_path, encoding="utf-8") as fh:
                    value = fh.read().strip()
                if value:
                    _MACHINE_ID_CACHE = value
                    return value
            except OSError:
                pass
        value = str(uuid.uuid4())
        if own_path:
            try:
                os.makedirs(os.path.dirname(own_path), exist_ok=True)
                with open(own_path, "w", encoding="utf-8") as fh:
                    fh.write(value)
            except OSError:
                pass
        _MACHINE_ID_CACHE = value
        return value


# ---------------------------------------------------------------------------
# COSY 头：网关按 COSY 客户端版本裁剪目录，落后会拿到缩减的模型清单

_cosy_version_lock = threading.Lock()


def _version_tuple(value):
    try:
        return tuple(int(part) for part in str(value).split("."))
    except (TypeError, ValueError):
        return (0,)


def is_cli_version(value):
    return bool(isinstance(value, str) and _VERSION_RE.match(value))


def _cosy_cache_path(data_dir):
    return os.path.join(data_dir, COSY_VERSION_CACHE_FILE) if data_dir else ""


def _read_cosy_cache(data_dir):
    path = _cosy_cache_path(data_dir)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cosy_cache(data_dir, data):
    path = _cosy_cache_path(data_dir)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        # 只读目录不该让请求失败：内存里的值仍然服务本进程。
        pass


def gateway_cosy_version(data_dir=None):
    """当前签名用的 COSY 版本：缓存里的新版本优先，否则内置兜底。"""
    cached = _read_cosy_cache(data_dir).get("version")
    if is_cli_version(cached) and _version_tuple(cached) >= _version_tuple(
            BUNDLED_GATEWAY_COSY_VERSION):
        return cached
    return BUNDLED_GATEWAY_COSY_VERSION


def refresh_cosy_version(data_dir=None, force=False, log=None):
    """慢慢跟官方 CLI 最新版本（24h 节流、只升不降、失败全容忍）。

    上游网关按 COSY 版本裁剪模型清单：版本落后会拿到缩减版目录，而且会被
    缓存住，所以签名前先尽量把版本跟上。任何失败都静默留在当前版本。
    """
    now = time.time()
    cached = _read_cosy_cache(data_dir)
    checked_at = cached.get("checked_at")
    if not force and isinstance(checked_at, (int, float)) and \
            now - checked_at < COSY_VERSION_INTERVAL:
        return gateway_cosy_version(data_dir)
    with _cosy_version_lock:
        # 进锁后重读：别的线程可能刚检查过。
        cached = _read_cosy_cache(data_dir)
        checked_at = cached.get("checked_at")
        if not force and isinstance(checked_at, (int, float)) and \
                now - checked_at < COSY_VERSION_INTERVAL:
            return gateway_cosy_version(data_dir)
        latest = None
        try:
            status, raw = _request(
                CLI_RELEASES_URL, timeout=COSY_VERSION_TIMEOUT,
                headers={"Accept": "application/vnd.github.v3+json"})
            if status == 200:
                releases = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(releases, list):
                    for release in releases:
                        if not isinstance(release, dict):
                            continue
                        if release.get("draft") or release.get("prerelease"):
                            continue
                        tag = release.get("tag_name")
                        if is_cli_version(tag):
                            latest = tag
                            break
        except Exception as exc:
            if log:
                log("qoder cosy version: 检查失败（保留 %s）: %s"
                    % (gateway_cosy_version(data_dir), exc))
        current = gateway_cosy_version(data_dir)
        if latest and _version_tuple(latest) > _version_tuple(current):
            _write_cosy_cache(data_dir, {"version": latest, "checked_at": time.time()})
            if log:
                log("qoder cosy version: %s -> %s" % (current, latest))
            return latest
        _write_cosy_cache(data_dir, {"version": cached.get("version"), "checked_at": time.time()})
        return current


def cosy_headers(body, url, user_id, access_token, name="", email="", machine="",
                 data_dir=None):
    """构造一个 COSY 签名头集合（模型清单等 algo 端点用）。

    与官方实现逐字节对齐：aesKey = uuid4 去横线取前 16 个字符，同时充当
    AES-128-CBC 的密钥与 IV；info 加密后放进 payload；cosyKey = RSA(aesKey)；
    签名 = md5(payloadB64 + "\\n" + cosyKey + "\\n" + 秒级时间戳 + "\\n" +
    bodyBytes + "\\n" + sigPath)，sigPath 是 pathname 去掉 "/algo" 前缀。
    """
    if not user_id:
        raise QoderError("COSY 需要 user id")
    if not access_token:
        raise QoderError("COSY 需要 access token")
    if isinstance(body, str):
        body = body.encode("utf-8")
    body = body or b""
    aes_key = uuid.uuid4().hex[:16]
    info = {
        "uid": user_id,
        "security_oauth_token": access_token,
        "name": name or "",
        "aid": "",
        "email": email or "",
    }
    info_b64 = base64.b64encode(
        _aes_cbc_encrypt(aes_key.encode("ascii"),
                         json.dumps(info, ensure_ascii=False).encode("utf-8"))).decode("ascii")
    cosy_key = _rsa_encrypt_b64(aes_key.encode("ascii"))
    timestamp = str(int(time.time()))
    request_id = str(uuid.uuid4())
    cosy_version = gateway_cosy_version(data_dir)
    payload = json.dumps({
        "version": "v1",
        "requestId": request_id,
        "info": info_b64,
        "cosyVersion": cosy_version,
        "ideVersion": "",
    }, ensure_ascii=False).encode("utf-8")
    payload_b64 = base64.b64encode(payload).decode("ascii")
    sig_path = urllib.parse.urlsplit(url).path or ""
    if sig_path.startswith("/algo"):
        sig_path = sig_path[len("/algo"):]
    digest = hashlib.md5()
    digest.update(payload_b64.encode("ascii"))
    digest.update(b"\n")
    digest.update(cosy_key.encode("ascii"))
    digest.update(b"\n")
    digest.update(timestamp.encode("ascii"))
    digest.update(b"\n")
    digest.update(body)
    digest.update(b"\n")
    digest.update(sig_path.encode("utf-8"))
    signature = digest.hexdigest()
    machine = machine or machine_id(data_dir)
    body_hash = hashlib.md5(body).hexdigest()
    return {
        "Authorization": "Bearer COSY.%s.%s" % (payload_b64, signature),
        "Cosy-Key": cosy_key,
        "Cosy-User": user_id,
        "Cosy-Date": timestamp,
        "Cosy-Version": cosy_version,
        "Cosy-Machineid": machine,
        "Cosy-Machinetoken": machine,
        "Cosy-Machinetype": COSY_CLIENT_TYPE,
        "Cosy-Machineos": "x86_64_windows",
        "Cosy-Clienttype": COSY_CLIENT_TYPE,
        "Cosy-Clientip": "127.0.0.1",
        "Cosy-Bodyhash": body_hash,
        "Cosy-Bodylength": str(len(body)),
        "Cosy-Sigpath": sig_path,
        "Cosy-Data-Policy": "disagree",
        "Cosy-Organization-Id": "",
        "Cosy-Organization-Tags": "",
        "Login-Version": "v2",
        "X-Request-Id": request_id,
    }


# ---------------------------------------------------------------------------
# 时间解析：上游的 expires_at / expires_in 单位在不同端点并不一致（见 pi 实现）

def _parse_time(value):
    """把 expires_at 的 JSON 值解析成 epoch 秒；解析不了返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # 协议按 Unix 毫秒给出；保险起见，秒级数字也认。
        return number / 1000.0 if number > 1e11 else number
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        number = float(text)
        return number / 1000.0 if number > 1e11 else number
    try:
        from datetime import datetime
        clean = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(clean)
        if parsed.tzinfo is None:
            return parsed.timestamp()
        return parsed.timestamp()
    except Exception:
        return None


def _resolve_expires(payload, default_seconds, milliseconds, buffer_seconds=300):
    """算出 (epoch_seconds, 缓冲已扣)。payload 里 expires_at 优先，其次 expires_in。"""
    now = time.time()
    end = _parse_time(payload.get("expires_at"))
    if end is None:
        expires_in = payload.get("expires_in")
        try:
            expires_in = float(expires_in) if expires_in not in (None, "") else 0
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in > 0:
            end = now + (expires_in / 1000.0 if milliseconds else expires_in)
    if end is None:
        end = now + default_seconds
    return end - buffer_seconds


# ---------------------------------------------------------------------------
# 登录：PAT 兑换

def exchange_pat(pat, realm, timeout=30, proxy=""):
    """PAT -> job token。返回 {"token", "refresh_token", "expires_at"(epoch 秒)}。"""
    pat = str(pat or "").strip()
    if not pat:
        raise QoderError("PAT 为空")
    cfg = realm_config(realm)
    url = cfg["openapi_url"] + "/api/v1/jobToken/exchange"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Cosy-Version": OPENAPI_COSY_VERSION,
        "Cosy-ClientType": COSY_CLIENT_TYPE,
    }
    payload = _json_request(url, method="POST", headers=headers,
                            body=json.dumps({"personal_token": pat}).encode("utf-8"),
                            timeout=timeout, proxy=proxy, what="Qoder PAT 兑换")
    token = payload.get("token") if isinstance(payload, dict) else None
    if not token:
        raise QoderError("Qoder PAT 兑换没有返回 job token")
    # 兑换端点的 expires_in 按毫秒给出（与设备码/刷新端点不同）。
    expires_at = _resolve_expires(payload, 24 * 3600, milliseconds=True)
    return {
        "token": str(token),
        "refresh_token": str(payload.get("refresh_token") or ""),
        "expires_at": expires_at,
    }


def fetch_userinfo(access_token, realm, timeout=15, proxy=""):
    """GET /api/v1/userinfo（best effort，失败返回空字段）。"""
    cfg = realm_config(realm)
    url = cfg["openapi_url"] + "/api/v1/userinfo"
    headers = {
        "Authorization": "Bearer " + str(access_token or ""),
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Cosy-Version": OPENAPI_COSY_VERSION,
        "Cosy-ClientType": COSY_CLIENT_TYPE,
    }
    try:
        payload = _json_request(url, headers=headers, timeout=timeout, proxy=proxy,
                                what="Qoder userinfo")
    except Exception:
        return {"uid": "", "email": "", "name": ""}
    if not isinstance(payload, dict):
        return {"uid": "", "email": "", "name": ""}
    return {
        "uid": str(payload.get("id") or ""),
        "email": str(payload.get("email") or ""),
        "name": str(payload.get("name") or payload.get("username") or ""),
    }


def credential_from_pat(pat, realm, data_dir=None, timeout=30, proxy=""):
    """PAT 全流程：兑换 + 身份解析。返回一份凭据 dict（见 credential_fields）。"""
    cfg = realm_config(realm)
    exchanged = exchange_pat(pat, realm, timeout=timeout, proxy=proxy)
    token = exchanged["token"]
    info = fetch_userinfo(token, realm, timeout=timeout, proxy=proxy)
    uid = info["uid"] or "qoder-user"
    machine = machine_id(data_dir)
    auth = {
        "kind": "pat",
        "personalToken": str(pat),
        "userId": uid,
        "email": info["email"],
        "name": info["name"],
        "machineId": machine,
    }
    return {
        "uid": uid,
        "nickname": info["name"] or info["email"] or uid[:8],
        "email": info["email"],
        "accessToken": token,
        "refreshToken": encode_pat_refresh(pat, exchanged["refresh_token"], uid, machine),
        "expiresAt": exchanged["expires_at"],
        "qoderAuth": auth,
        "realm": str(realm or "intl"),
        "domain": urllib.parse.urlsplit(cfg["site_url"]).netloc,
    }


def encode_pat_refresh(pat, job_refresh_token, user_id, machine):
    """PAT 凭据的 refresh 字段：`pat|<PAT>|<jobRefreshToken>|<userID>|<machineID>`。"""
    return "|".join(["pat", str(pat or ""), str(job_refresh_token or ""),
                     str(user_id or ""), str(machine or "")])


def decode_pat_refresh(value):
    parts = str(value or "").split("|")
    return {
        "pat": parts[1] if len(parts) > 1 else "",
        "job_refresh_token": parts[2] if len(parts) > 2 else "",
        "user_id": parts[3] if len(parts) > 3 else "",
        "machine": parts[4] if len(parts) > 4 else "",
    }


def is_pat_refresh(value):
    return str(value or "").startswith("pat|")


# ---------------------------------------------------------------------------
# 登录：浏览器设备码（仅国际版）

def generate_pkce():
    """verifier = base64url(32 随机字节)；challenge = base64url(sha256(verifier))。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def device_login_start(realm="intl", data_dir=None):
    """发起一次设备码登录。返回 {authUrl, nonce, verifier, machineId, realm}。"""
    cfg = realm_config(realm)
    if not cfg.get("device_login_url"):
        raise QoderError("Qoder 国内版不支持浏览器登录，请使用 PAT 登录")
    verifier, challenge = generate_pkce()
    nonce = str(uuid.uuid4())
    machine = machine_id(data_dir)
    url = "%s?challenge=%s&challenge_method=S256&machine_id=%s&nonce=%s" % (
        cfg["device_login_url"], urllib.parse.quote(challenge),
        urllib.parse.quote(machine), urllib.parse.quote(nonce))
    return {"authUrl": url, "nonce": nonce, "verifier": verifier,
            "machineId": machine, "realm": "intl"}


def device_login_poll(nonce, verifier, realm="intl", data_dir=None, timeout=20,
                      proxy=""):
    """轮询一次设备码状态。

    返回 {"status": "pending"|"ok"|"error", "message"?, "credential"?}。
    202/404 是「还在等」，200 才带 token。
    """
    cfg = realm_config(realm)
    url = "%s/api/v1/deviceToken/poll?nonce=%s&verifier=%s&challenge_method=S256" % (
        cfg["openapi_url"], urllib.parse.quote(str(nonce)), urllib.parse.quote(str(verifier)))
    status, raw = _request(url, headers={"Accept": "application/json",
                                         "User-Agent": USER_AGENT},
                           timeout=timeout, proxy=proxy)
    if status in (202, 404):
        return {"status": "pending", "message": "等待浏览器完成登录授权"}
    if status < 200 or status >= 300:
        text = raw.decode("utf-8", "replace")
        return {"status": "error", "message": "设备码轮询失败: HTTP %d %s"
                % (status, text[:120])}
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"status": "error", "message": "设备码轮询返回非 JSON"}
    if not isinstance(payload, dict):
        return {"status": "error", "message": "设备码轮询返回了意外的 JSON 形状"}
    token = payload.get("token")
    if not token:
        return {"status": "pending", "message": "等待浏览器完成登录授权"}
    user_id = str(payload.get("user_id") or "")
    machine = machine_id(data_dir)
    info = fetch_userinfo(token, "intl", timeout=15, proxy=proxy)
    if info["uid"]:
        user_id = info["uid"]
    credential = {
        "uid": user_id or "qoder-user",
        "nickname": info["name"] or info["email"] or (user_id[:8] if user_id else "Qoder"),
        "email": info["email"],
        "accessToken": str(token),
        "refreshToken": "|".join([
            str(payload.get("refresh_token") or ""), user_id, machine]),
        "expiresAt": _resolve_expires(payload, 30 * 24 * 3600, milliseconds=False),
        "qoderAuth": {
            "kind": "oauth",
            "personalToken": "",
            "userId": user_id,
            "email": info["email"],
            "name": info["name"],
            "machineId": machine,
        },
        "realm": "intl",
        "domain": urllib.parse.urlsplit(cfg["site_url"]).netloc,
    }
    return {"status": "ok", "message": "登录成功", "credential": credential}


# ---------------------------------------------------------------------------
# 刷新

def refresh_credential(realm, auth, refresh_field, access_token="", timeout=30,
                       proxy="", data_dir=None):
    """把一份 qoder 凭据刷到最新。

    返回 {"accessToken", "refreshToken", "expiresAt", "qoderAuth"}；失败抛
    QoderError。PAT 凭据重新兑换 PAT；设备码凭据走 center 的 refresh_token
    端点（需要当前 access token 做 Bearer）。上游把 refresh token 当一次性
    用，所以每次成功刷新都整体替换。
    """
    auth = dict(auth or {})
    pat = str(auth.get("personalToken") or "").strip()
    if not pat and is_pat_refresh(refresh_field):
        pat = decode_pat_refresh(refresh_field)["pat"]
    if pat or auth.get("kind") == "pat":
        if not pat:
            raise QoderError("PAT 凭据缺少 personal token，请重新登录")
        fresh = credential_from_pat(pat, realm, data_dir=data_dir,
                                    timeout=timeout, proxy=proxy)
        merged = dict(auth)
        merged.update(fresh["qoderAuth"])
        return {
            "accessToken": fresh["accessToken"],
            "refreshToken": fresh["refreshToken"],
            "expiresAt": fresh["expiresAt"],
            "qoderAuth": merged,
        }
    return _refresh_device_token(realm, auth, refresh_field,
                                 access_token=access_token, timeout=timeout,
                                 proxy=proxy, data_dir=data_dir)


def _refresh_device_token(realm, auth, refresh_field, access_token="", timeout=30,
                          proxy="", data_dir=None):
    parts = str(refresh_field or "").split("|")
    refresh_token = parts[0] if parts else ""
    user_id = parts[1] if len(parts) > 1 else str(auth.get("userId") or "")
    machine = (parts[2] if len(parts) > 2 else "") or str(auth.get("machineId") or "") \
        or machine_id(data_dir)
    if not refresh_token:
        raise QoderError("没有 refresh token，请重新登录")
    cfg = realm_config(realm)
    url = cfg["center_url"] + "/algo/api/v3/user/refresh_token"
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + str(access_token or ""),
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    payload = _json_request(url, method="POST", headers=headers,
                            body=json.dumps({"refreshToken": refresh_token}).encode("utf-8"),
                            timeout=timeout, proxy=proxy, what="Qoder token 刷新")
    token = payload.get("token")
    if not token:
        raise QoderError("Qoder 刷新没有返回新 token")
    new_refresh = str(payload.get("refresh_token") or refresh_token)
    # 设备码/刷新端点的 expires_in 按秒给出（与 PAT 兑换的毫秒不同）。
    expires_at = _resolve_expires(payload, 30 * 24 * 3600, milliseconds=False)
    merged = dict(auth)
    merged.update({
        "kind": merged.get("kind") or "oauth",
        "userId": user_id,
        "machineId": machine,
    })
    return {
        "accessToken": str(token),
        "refreshToken": "|".join([new_refresh, user_id, machine]),
        "expiresAt": expires_at,
        "qoderAuth": merged,
    }


# ---------------------------------------------------------------------------
# 额度（看板的「积分」列）

def fetch_quota(access_token, realm, timeout=15, proxy=""):
    """GET /api/v2/quota/usage 原始 JSON。"""
    cfg = realm_config(realm)
    url = cfg["openapi_url"] + "/api/v2/quota/usage"
    headers = {
        "Authorization": "Bearer " + str(access_token or ""),
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    payload = _json_request(url, headers=headers, timeout=timeout, proxy=proxy,
                            what="Qoder 额度查询")
    if not isinstance(payload, dict):
        raise QoderError("Qoder 额度返回了意外的 JSON 形状")
    return payload


def _quota_bucket(raw, total_key="total"):
    """一个额度桶；缺值保持缺值，不推断。返回 None 表示整桶不可用。"""
    if not isinstance(raw, dict):
        return None
    def _number(key):
        value = raw.get(key)
        if isinstance(value, bool) or value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None
    total = _number(total_key)
    used = _number("used")
    remaining = _number("remaining")
    if total is None and used is None and remaining is None:
        return None
    unit = raw.get("unit")
    return {
        "total": total,
        "used": used,
        "remaining": remaining,
        "unit": unit.strip() if isinstance(unit, str) and unit.strip() else "credits",
    }


def credits_from_quota(raw):
    """把 Qoder 的额度快照塑造成看板认识的 credits 字典（WorkBuddy 同款）。

    三个来源合成 packages：userQuota（个人套餐）、addOnQuota（加油包）、
    orgResourcePackage（企业资源包，总额字段是 cap）。缺值保持缺值：桶里
    一个数字都没有就不产生条目；一个条目都没有时返回 None（调用方按
    「查询失败」处理，而不是画成 0/0）。
    """
    if not isinstance(raw, dict):
        return None
    buckets = [
        ("user-quota", "个人套餐", _quota_bucket(raw.get("userQuota"))),
        ("add-on-quota", "加油包", _quota_bucket(raw.get("addOnQuota"))),
        ("org-resource-package", "企业资源包",
         _quota_bucket(raw.get("orgResourcePackage"),
                       total_key="cap" if isinstance(raw.get("orgResourcePackage"), dict)
                       and "cap" in raw["orgResourcePackage"] else "total")),
    ]
    expires_at = raw.get("expiresAt")
    end_epoch = _parse_time(expires_at)
    end_text = ""
    days_left = None
    if end_epoch:
        # Windows 的 localtime 对 3000 年以后/异常大的 epoch 会抛 OSError 22。
        # Qoder 对「不过期」的套餐会回 253402214400000（9999-12-31 毫秒），
        # 这里不能让它把整个额度刷新打成失败：超出可表示范围按不过期处理。
        if end_epoch > 253402300799:
            end_epoch = None
        else:
            try:
                end_text = time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(end_epoch))
                days_left = round((end_epoch - time.time()) / 86400.0, 1)
            except (OSError, OverflowError, ValueError):
                end_epoch = None
                end_text = ""
                days_left = None

    packages = []
    for bucket_id, label, bucket in buckets:
        if not bucket:
            continue
        remain = bucket["remaining"]
        used = bucket["used"]
        size = bucket["total"]
        # 只做「三缺二补一」的确定性回填（总数/已用/剩余三者知道两个），
        # 不跨桶推断，也不编造单价。
        if size is not None and remain is not None and used is None:
            used = max(0.0, size - remain)
        if size is not None and used is not None and remain is None:
            remain = max(0.0, size - used)
        packages.append({
            "name": label,
            "package_code": bucket_id,
            "product_name": "Qoder",
            "sub_product_name": "",
            "grant_reason": "",
            "resource_id": bucket_id,
            "deal_name": "",
            "create_time": "",
            "remain": round(remain, 2) if remain is not None else 0.0,
            "used": round(used, 2) if used is not None else 0.0,
            "size": round(size, 2) if size is not None else 0.0,
            "unit": bucket["unit"],
            "in_usage": True,
            "auto_renew": False,
            "cycle_start_time": "",
            "cycle_end_time": end_text,
            "expire_time": end_text,
            "no_expiry": end_epoch is None,
            "days_left": days_left,
            "is_expired": bool(days_left is not None and days_left < 0),
            "status": 0,
        })
    if not packages:
        return None
    total_remain = round(sum(p["remain"] for p in packages), 2)
    total_used = round(sum(p["used"] for p in packages), 2)
    total_size = round(sum(p["size"] for p in packages), 2)
    earliest_expiring = None
    if end_epoch:
        earliest_expiring = {
            "name": "Qoder 套餐",
            "package_code": "qoder-plan",
            "remain": total_remain,
            "cycle_end_time": end_text,
            "days_left": days_left,
        }
    return {
        "remain": total_remain,
        "used": total_used,
        "size": total_size,
        "used_percent": f"{(total_used / total_size * 100):.1f}%" if total_size > 0 else "0.0%",
        "remain_percent": f"{(total_remain / total_size * 100):.1f}%" if total_size > 0 else "100.0%",
        "is_paid_user": bool(buckets[0][2]),
        "checkin": None,
        "earliest_expiring": earliest_expiring,
        "packages": packages,
        "updated_at": time.time(),
        "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 模型清单

def to_model_id(display_name):
    """模型 id 由 display_name 去掉空白派生（pi 两区一致的口径）。"""
    return re.sub(r"\s+", "", str(display_name or "QoderModel")) or "QoderModel"


def _context_window(entry):
    config = entry.get("context_config")
    if isinstance(config, dict):
        advertised = 0
        for value in config.values():
            if isinstance(value, dict) and isinstance(value.get("token_count"), (int, float)):
                if value["token_count"] > advertised:
                    advertised = int(value["token_count"])
        if advertised > 0:
            return advertised
    return DEFAULT_CONTEXT_WINDOW


def normalise_model_entry(entry):
    """把 /model/list 的一条记录塑造成网关的模型元数据；不可用时返回 None。"""
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    display = entry.get("display_name")
    if not key or not display or not entry.get("enable"):
        return None
    thinking = entry.get("thinking_config")
    efforts = []
    if isinstance(thinking, dict):
        enabled = thinking.get("enabled")
        if isinstance(enabled, dict) and isinstance(enabled.get("efforts"), dict):
            efforts = sorted(str(name) for name in enabled["efforts"].keys())
    # 官方模型选择器里的 Credit 倍率就是这个字段（见 docs 的说明）。
    factor = entry.get("price_factor")
    if factor is None:
        factor = entry.get("priceFactor")
    credits = format_price_factor(factor)
    model = {
        "id": to_model_id(display),
        "name": str(display),
        "key": str(key),
        "context": _context_window(entry),
        "max_tokens": MAX_OUTPUT_TOKENS,
        "reasoning": bool(entry.get("is_reasoning")) or bool(thinking),
        "supports_effort": bool(efforts),
        "efforts": efforts,
        "input": ["text", "image"] if entry.get("is_vl") else ["text"],
        "credits": credits,
    }
    if credits:
        model["credits_note"] = _CREDITS_NOTE_REMOTE
    return model


#: 官网文档里的 Credit 消耗参考系数（按上游 key）。只在拿不到上游目录
#: （没有 Qoder 账号）时兜底展示；有账号时一律用 /model/list 的
#: price_factor——文档自己也说「倍率来自服务端模型列表的 price_factor」。
#: 值取自 docs.qoder.com 的模型选择器 / CLI 文档与 docs.qoder.cn 的同名页。
_DOC_PRICE_FACTORS = {
    "intl": {
        "auto": 0.5,          # 智能路由
        "ultimate": 2.0,
        "performance": 1.1,
        "efficient": 0.3,     # 官网对付费用户有 0.0 限时免费，Service Account 仍 0.3；
                              # 实际以账号目录的 price_factor 为准，这里只做无账号兜底。
        "qmodel": 0.1,        # Qwen3.7 Plus
        "qmodel_preview": 0.5,   # Qwen3.8-Max
        "qmodel_latest": 0.5,    # Qwen3.7-Max
        "dmodel": 0.5,        # DeepSeek-V4-Pro（峰谷计价，闲时减半）
        "dfmodel": 0.3,       # DeepSeek-V4-Flash
        "gm51model": 0.6,     # GLM-5.2
        "kmodel": 0.3,        # Kimi-K2.7-Code
        "kmodel_latest": 1.4,    # Kimi-K3
        "mmodel": 0.2,        # MiniMax-M3
    },
    "cn": {
        "auto": 0.5,
        "qmodel_latest": 0.5,
        "qmodel": 0.1,
        "dmodel": 0.5,
        "dfmodel": 0.1,
        "mmodel": 0.2,
    },
}

#: 静态清单里没有文档值时的说明：宁可显示「-」也不编一个数字。
_CREDITS_NOTE_DOCS = "官网文档参考值；实际倍率以账号模型清单（price_factor）为准"
_CREDITS_NOTE_REMOTE = "来自账号模型清单的 price_factor；活动折扣可能让实际倍率不同"


def format_price_factor(value):
    """把上游 price_factor 格式化成看板用的倍率字符串（0.5 -> "0.5x"）。

    认不出/负数一律返回空串，让前端显示「-」，不要假装是 0。
    """
    if value is None or value == "" or isinstance(value, bool):
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num < 0:
        return ""
    if num == 0:
        return "0x"
    text = "%.2f" % num
    if text.endswith("0"):
        # 2.00 -> 2.0 / 0.50 -> 0.5，保留一位小数；0.06 这种不动。
        text = text[:-1]
    return text + "x"


#: 静态兜底清单（拿不到上游目录时用；与 pi 内置表一致）。
_STATIC_MODELS = {
    "intl": [
        ("auto", "Auto", True, False, True, None),
        ("ultimate", "Ultimate", True, True, True, None),
        ("performance", "Performance", True, True, True, None),
        ("efficient", "Efficient", False, False, True, None),
        ("lite", "Lite", False, False, False, None),
        ("qmodel", "Qwen3.7 Plus", False, False, True, None),
        ("cmodel", "Cantus", True, True, True, None),
        ("qmodel_preview", "Qwen3.8-Max", True, True, True, None),
        ("qmodel_latest", "Qwen3.7-Max", False, False, True, None),
        ("dmodel", "DeepSeek-V4-Pro", True, True, True, None),
        ("dfmodel", "DeepSeek-V4-Flash", True, True, True, None),
        ("gm51model", "GLM-5.2", True, True, True, None),
        ("kmodel", "Kimi-K2.7-Code", False, False, True, 256000),
        ("kmodel_latest", "Kimi-K3", False, False, True, None),
        ("mmodel", "MiniMax-M3", False, False, True, None),
    ],
    "cn": [
        ("auto", "Auto", True, False, True, 200000),
        ("qmodel_latest", "Qwen3.7-Max", True, False, True, None),
        ("qmodel", "Qwen3.7-Plus", True, False, False, None),
        ("q36fmodel", "Qwen3.6-Flash", True, False, False, None),
        ("dmodel", "DeepSeek-V4-Pro", True, False, False, None),
        ("dfmodel", "DeepSeek-V4-Flash", False, False, False, None),
        ("gm51model", "GLM-5.2", True, False, True, 200000),
        ("kmodel", "Kimi-K2.7-Code", True, False, True, 256000),
        ("mmodel", "MiniMax-M2.7", False, False, False, 200000),
    ],
}


def static_models(realm):
    """内置兜底清单（形状与 normalise_model_entry 的输出一致）。"""
    rows = _STATIC_MODELS.get("cn" if str(realm).lower() == "cn" else "intl")
    factors = _DOC_PRICE_FACTORS.get("cn" if str(realm).lower() == "cn" else "intl", {})
    models = []
    for key, name, reasoning, supports_effort, vision, context in rows:
        credits = format_price_factor(factors.get(key))
        model = {
            "id": to_model_id(name),
            "name": name,
            "key": key,
            "context": context or DEFAULT_CONTEXT_WINDOW,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "reasoning": reasoning,
            "supports_effort": supports_effort,
            "efforts": [],
            "input": ["text", "image"] if vision else ["text"],
            "credits": credits,
        }
        if credits:
            model["credits_note"] = _CREDITS_NOTE_DOCS
        models.append(model)
    return models


def model_entry_meta(model):
    """把一份模型元数据翻译成 wb_proxy.model_entry 认识的 meta 字典。

    与 WorkBuddy 目录的字段名保持一致（supportsImages / supportsReasoning /
    maxInputTokens / maxOutputTokens），限额查找优先用这里的值。
    """
    model = model or {}
    meta = {
        "name": model.get("name"),
        "supportsImages": "image" in (model.get("input") or []),
        "supportsReasoning": bool(model.get("reasoning")),
        "maxInputTokens": model.get("context"),
        "maxOutputTokens": model.get("max_tokens"),
        "ownedBy": "qoder",
        "vendor": "Qoder",
    }
    if model.get("efforts"):
        meta["reasoning"] = {"supportedEfforts": list(model["efforts"])}
    if model.get("credits"):
        meta["credits"] = model["credits"]
    if model.get("credits_note"):
        meta["credits_note"] = model["credits_note"]
    return meta


def fetch_model_list(access_token, auth, realm, data_dir=None, timeout=25, proxy=""):
    """拉一次上游模型目录（COSY 签名），返回原始 chat 列表。"""
    cfg = realm_config(realm)
    url = cfg["base_url"] + "algo/api/v2/model/list?Encode=1"
    refresh_cosy_version(data_dir)
    headers = cosy_headers(None, url,
                           user_id=str((auth or {}).get("userId") or ""),
                           access_token=access_token,
                           name=str((auth or {}).get("name") or ""),
                           email=str((auth or {}).get("email") or ""),
                           machine=str((auth or {}).get("machineId") or ""),
                           data_dir=data_dir)
    headers["Accept"] = "application/json"
    payload = _json_request(url, headers=headers, timeout=timeout, proxy=proxy,
                            what="Qoder 模型目录")
    chat = payload.get("chat") if isinstance(payload, dict) else None
    return chat if isinstance(chat, list) else []


def available_models(realm, access_token=None, auth=None, data_dir=None, timeout=25,
                     proxy="", force=False, log=None):
    """模型清单：能拿到上游目录就用它，否则回落到内置表。

    返回 (models, source)；source ∈ ("remote", "bundled")。任何失败都不抛：
    目录只是展示数据，账号与额度才是主链路。
    """
    if force:
        refresh_cosy_version(data_dir, force=True, log=log)
    if access_token and (auth or {}).get("userId"):
        try:
            raw = fetch_model_list(access_token, auth, realm, data_dir=data_dir,
                                   timeout=timeout, proxy=proxy)
            models = []
            seen = set()
            for entry in raw:
                model = normalise_model_entry(entry)
                if model and model["id"] not in seen:
                    seen.add(model["id"])
                    models.append(model)
            if models:
                return models, "remote"
        except Exception as exc:
            if log:
                log("qoder catalog: %s 上游目录不可用（%s），使用内置清单" % (realm, exc))
    return static_models(realm), "bundled"
