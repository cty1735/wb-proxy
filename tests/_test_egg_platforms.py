"""wb_platforms 的自检：DPoP / CodeArts 签名 / 平台池归一化 / 登录会话。

不联网：只测纯计算与本地存取。
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_ed25519 as E
import wb_egg_pools as P
import wb_platforms as W
import wb_zen

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def b64d(text):
    return W._b64url_decode(text)


def verify_es256(public_jwk, signing_input, signature):
    """用公钥验一次裸 r||s 的 ECDSA 签名（P-256）。"""
    q = (int.from_bytes(b64d(public_jwk["x"]), "big"),
         int.from_bytes(b64d(public_jwk["y"]), "big"))
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    z = int.from_bytes(hashlib.sha256(signing_input.encode("ascii")).digest(),
                       "big")
    w = pow(s, -1, W._N)
    point = W._pt_add(W._pt_mul(W._G, (z * w) % W._N),
                      W._pt_mul(q, (r * w) % W._N))
    return point is not None and point[0] % W._N == r


# --- 平台注册表 -----------------------------------------------------------
check("平台列表含 CodeArts / Cline / Freebuff",
      set(W.PLATFORM_IDS) == {"codearts", "cline", "freebuff"}, W.PLATFORM_IDS)
check("CodeArts 默认池字段", W.defaults("codearts")["base_url"].startswith("https://"))
check("Cline 默认池字段", W.defaults("cline")["platform"] == "cline")
check("Freebuff 默认池字段",
      W.defaults("freebuff")["base_url"] == "https://www.codebuff.com",
      W.defaults("freebuff"))
try:
    W.defaults("nope")
    check("未知平台报错", False)
except W.PlatformError as exc:
    check("未知平台报错", exc.status == 400, exc.detail)

# --- P-256 / DPoP ---------------------------------------------------------
private, public = W.new_dpop_key()
check("私钥带 d", bool(private.get("d")))
check("公钥不带 d", "d" not in public)
check("公钥坐标是 32 字节",
      len(b64d(public["x"])) == 32 and len(b64d(public["y"])) == 32)

proof = W.dpop_proof(private, public, "post", "https://example.com/token")
parts = proof.split(".")
check("DPoP 三段式", len(parts) == 3, proof)
header = json.loads(b64d(parts[0]))
payload = json.loads(b64d(parts[1]))
check("DPoP header alg", header.get("alg") == "ES256", header)
check("DPoP header typ", header.get("typ") == "dpop+jwt", header)
check("DPoP 带公钥 JWK", header.get("jwk") == public, header)
check("DPoP htm 大写", payload.get("htm") == "POST", payload)
check("DPoP htu", payload.get("htu") == "https://example.com/token", payload)
check("DPoP iat 是当前秒", abs(int(payload.get("iat") or 0) - time.time()) < 30)
check("DPoP jti 足够长", len(str(payload.get("jti") or "")) >= 32)
check("DPoP 签名可验证",
      verify_es256(public, parts[0] + "." + parts[1], b64d(parts[2])))

thumb = W.jwk_thumbprint(public)
check("JWK thumbprint 稳定", thumb == W.jwk_thumbprint(dict(public)))
check("JWK thumbprint 长度", len(b64d(thumb)) == 32)

# --- Ed25519（Freebuff 设备签名）----------------------------------------
# RFC 8032 第一条测试向量：空消息、固定 seed。
ed_seed = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
ed_pub = E.public_key(ed_seed)
ed_sig = E.sign(ed_seed, b"")
check("Ed25519 公钥 RFC 向量",
      ed_pub.hex() ==
      "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
      ed_pub.hex())
check("Ed25519 签名 RFC 向量",
      ed_sig.hex() ==
      "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
      "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
      ed_sig.hex())

# --- CodeArts SDK-HMAC-SHA256 签名 ---------------------------------------
account = {
    "access_key": "AKIDEXAMPLE",
    "secret_key": "SECRETEXAMPLE",
    "security_token": "TOKENEXAMPLE",
}
url = "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions"
body = b'{"model":"x","stream":true}'
headers = {"content-type": "application/json",
           "x-sdk-date": "20260101T000000Z",
           "host": "ignored.example.com"}
signed = W.sign_headers(account, "POST", url, body, headers)
check("签名带 x-sdk-date", signed.get("x-sdk-date") == "20260101T000000Z")
check("签名带 host", signed.get("host") ==
      "snap-access.cn-north-4.myhuaweicloud.com", signed.get("host"))
check("签名带 security token",
      signed.get("x-security-token") == "TOKENEXAMPLE")
auth = signed.get("Authorization") or ""
check("Authorization 前缀",
      auth.startswith("SDK-HMAC-SHA256 Access=AKIDEXAMPLE, SignedHeaders="),
      auth)
check("Authorization 含 Signature",
      ", Signature=" in auth and len(auth.rsplit("=", 1)[-1]) == 64, auth)
check("SignedHeaders 排序",
      auth.split("SignedHeaders=")[1].split(",")[0].split(";")
      == sorted(auth.split("SignedHeaders=")[1].split(",")[0].split(";")))

# GET 不签 body：换 body 不应改变签名（x-sdk-date 固定）
get_a = W.sign_headers(account, "GET", url, b"aaa", headers)["Authorization"]
get_b = W.sign_headers(account, "GET", url, b"bbb", headers)["Authorization"]
check("GET 签名忽略 body", get_a == get_b)
post_a = W.sign_headers(account, "POST", url, b"aaa", headers)["Authorization"]
post_b = W.sign_headers(account, "POST", url, b"bbb", headers)["Authorization"]
check("POST 签名包含 body", post_a != post_b)

check("CanonicalURI 补尾斜杠", W._canonical_uri("/a/b") == "/a/b/")
check("CanonicalURI 根路径", W._canonical_uri("/") == "/")
check("CanonicalQuery 排序",
      W._canonical_query("b=2&a=1") == "a=1&b=2")

# --- 账号解析 -------------------------------------------------------------
creds = {"access_key_id": "AK", "secret_access_key": "SK",
         "security_token": "ST", "expiration": "2099-01-01T00:00:00Z"}
ca = W._codearts_account(creds, "refresh", private, public, "verifier",
                         label="主账号")
check("CodeArts 账号 access_key", ca["access_key"] == "AK")
check("CodeArts 账号 refresh_token", ca["refresh_token"] == "refresh")
check("CodeArts 账号 platform", ca["platform"] == "codearts")
check("CodeArts 账号稳定 id", ca["id"] == W._codearts_account(
    creds, "refresh", private, public, "verifier", label="主账号")["id"])
check("CodeArts 账号过期时间在将来", ca["expires_at"] > time.time())

cline_payload = {"success": True, "data": {
    "accessToken": "access-1", "refreshToken": "refresh-1",
    "tokenType": "Bearer", "expiresAt": "2099-01-01T00:00:00Z",
    "userInfo": {"email": "a@example.com", "name": "A",
                 "clineUserId": "user-1"}}}
cl = W._cline_token_account(cline_payload)
check("Cline 账号 access_token", cl["access_token"] == "access-1")
check("Cline 账号 refresh_token", cl["refresh_token"] == "refresh-1")
check("Cline 账号 email", cl["email"] == "a@example.com")
check("Cline 账号 platform", cl["platform"] == "cline")

# Freebuff：浏览器授权返回的 user（公开字段；不读本机凭据）
fb_user = {"id": "fb-user-1", "email": "fb@example.com", "name": "FB",
           "authToken": "fb-token-1"}
fb = W._freebuff_account(fb_user, fingerprint_id="fp-1",
                         fingerprint_hash="fh-1")
fb_seed, fb_pub = E.generate()
fb["freebuff_device_seed"] = W._b64url(fb_seed)
fb["freebuff_device_public"] = W._b64url(fb_pub)
fb["freebuff_device_key_id"] = "key-chat"
fb["freebuff_catalog_fetch_id"] = "fetch-chat"
fb["freebuff_catalog_handles"] = {"z-ai/glm-5.3-flash": "fbm1.glm"}
# 已有目录快照：build_request 不再为了拿 handle 去联网拉目录。
fb["freebuff_catalog_at"] = time.time()
fb["freebuff_catalog_refresh_at"] = time.time() + 3600
# 预置一个活跃会话，build_request 直接复用，不联网走准入。
fb["freebuff_session"] = {
    "status": "active", "instance_id": "cli:test", "attempt_id": "test",
    "model_key": "z-ai/glm-5.3-flash", "model_handle": "fbm1.glm",
    "fetch_id": "fetch-chat", "expires_at": time.time() + 3600,
}
check("Freebuff 账号 access_token", fb["access_token"] == "fb-token-1")
check("Freebuff 账号 email", fb["email"] == "fb@example.com")
check("Freebuff 账号 platform", fb["platform"] == "freebuff")
check("Freebuff 账号稳定 id",
      fb["id"] == W._freebuff_account(dict(fb_user))["id"])
check("Freebuff 账号长期有效（不需要 refresh）",
      not W._needs_refresh(fb), fb.get("expires_at"))
fb_exp = W._freebuff_account(dict(fb_user, expiresAt="2000-01-01T00:00:00Z"))
check("Freebuff 不把登录链接过期时间当令牌过期",
      fb_exp["expires_at"] == 0 and fb_exp["long_lived"] is True, fb_exp)
fb_public = W.account_public(fb)
check("Freebuff 账号视图不泄露 token",
      "access_token" not in fb_public and "auth_token" not in fb_public,
      fb_public)
check("Freebuff 账号视图长期有效", fb_public.get("long_lived") is True, fb_public)
fb_models = W._freebuff_models()
check("Freebuff 模型目录非空", len(fb_models) >= 5, len(fb_models))
check("Freebuff 模型 id 唯一",
      len({m["id"] for m in fb_models}) == len(fb_models),
      [m["id"] for m in fb_models])
check("Freebuff 模型目录用普通 id（不是 catalog handle）",
      all(not m["id"].startswith("fbm1.") for m in fb_models))
check("Freebuff 兜底目录是当前的免费模型",
      {"mimo/mimo-v2.5", "z-ai/glm-5.3-flash", "deepseek/deepseek-v4-flash",
       "upstage/solar-pro4", "upstage/solar-mini4"}.issubset(
           {m["id"] for m in fb_models}),
      [m["id"] for m in fb_models])
check("Freebuff 兜底目录只留免费模型",
      all(m.get("free") is True for m in fb_models),
      [m["id"] for m in fb_models])

# 设备签名：原文与 CLI 的 RRA 一致，Ed25519 签名可复现。
dev_seed, dev_pub = E.generate()
dev_account = {
    "id": "acct-dev", "access_token": "fb-token-1",
    "freebuff_device_seed": W._b64url(dev_seed),
    "freebuff_device_public": W._b64url(dev_pub),
    "freebuff_device_key_id": "key-1",
}
dev_url = "https://www.codebuff.com/api/v1/chat/completions"
dev_body = b'{"model":"deepseek/deepseek-v4-flash"}'
dev_headers = W._freebuff_device_headers(
    dev_account, "POST", dev_url, dev_body, fetch_id="fetch-1")
check("Freebuff 设备头带 keyId",
      dev_headers.get("x-freebuff-device-key") == "key-1", dev_headers)
check("Freebuff 设备头带目录快照",
      dev_headers.get("x-freebuff-catalog-fetch") == "fetch-1", dev_headers)
try:
    dev_ts = int(dev_headers.get("x-freebuff-device-ts") or 0)
except (TypeError, ValueError):
    dev_ts = 0
check("Freebuff 设备头时间戳是毫秒", abs(dev_ts - time.time() * 1000) < 30000,
      dev_headers.get("x-freebuff-device-ts"))
dev_signing = "\n".join([
    "freebuff-device-v1",
    "POST",
    "/api/v1/chat/completions",
    str(dev_ts),
    hashlib.sha256(dev_body).hexdigest(),
    "fetch-1",
])
dev_expected = W._b64url(E.sign(dev_seed, dev_signing.encode("utf-8"),
                                public=dev_pub))
check("Freebuff 设备签名原文/结果一致",
      dev_headers.get("x-freebuff-device-sig") == dev_expected,
      (dev_headers.get("x-freebuff-device-sig"), dev_expected))
no_fetch = W._freebuff_device_headers(
    dev_account, "GET", dev_url, b"", fetch_id="")
check("Freebuff 无目录快照时不发 fetch 头",
      "x-freebuff-catalog-fetch" not in no_fetch, no_fetch)

# 设备公钥注册：POST /device-keys，body 是 base64url 原始公钥。
reg_seed, reg_pub = E.generate()
reg_account = {
    "id": "acct-reg", "access_token": "fb-token-1",
    "freebuff_device_seed": W._b64url(reg_seed),
    "freebuff_device_public": W._b64url(reg_pub),
}
reg_call = {}


def fake_device_register(url, method="GET", headers=None, body=None,
                         timeout=None):
    reg_call.update({"url": url, "method": method, "headers": headers,
                     "body": body, "timeout": timeout})
    return {"keyId": "key-reg-1"}


_old_json_request = W._json_request
W._json_request = fake_device_register
try:
    reg_device = W._freebuff_device_key(None, reg_account)
finally:
    W._json_request = _old_json_request
check("Freebuff 设备注册 URL",
      reg_call.get("url") ==
      "https://www.codebuff.com/api/v1/freebuff/device-keys", reg_call)
check("Freebuff 设备注册方法", reg_call.get("method") == "POST", reg_call)
reg_body = json.loads((reg_call.get("body") or b"{}").decode("utf-8"))
check("Freebuff 设备注册公钥", reg_body.get("publicKey") == W._b64url(reg_pub),
      reg_body)
check("Freebuff 设备注册 client", reg_body.get("client") == "cli", reg_body)
check("Freebuff 设备注册带 Bearer",
      (reg_call.get("headers") or {}).get("authorization")
      == "Bearer fb-token-1", reg_call.get("headers"))
check("Freebuff 设备注册保存 keyId",
      reg_account.get("freebuff_device_key_id") == "key-reg-1"
      and reg_device and reg_device.get("key_id") == "key-reg-1",
      reg_account)

# 目录接口：key 是对外模型 id，handle 只用于会话准入。
_old_json_request = W._json_request
W._json_request = lambda *args, **kwargs: {"protocol": 1,
                                           "fetchId": "fetch-cat",
                                           "rows": [
    {"key": "deepseek/deepseek-v4-flash", "handle": "fbm1.deepseek",
     "displayName": "DeepSeek V4.1 Flash 10/17", "premium": False,
     "multimodal": True, "contextWindow": 1048576,
     "efforts": ["low", "high"], "defaultEffort": "high"},
    {"key": "openai/gpt-6.1-sol", "handle": "fbm1.sol",
     "displayName": "GPT-6.1 Sol", "premium": True,
     "multimodal": True, "contextWindow": 1000000,
     "efforts": ["low", "medium", "high"], "defaultEffort": "high"},
    # locked 行只用于付费墙展示，网关不能拿它开会话，目录里直接丢掉。
    {"key": "locked/only", "handle": "fbm1.locked",
     "displayName": "Locked Only", "premium": False, "access": "locked",
     "multimodal": False, "contextWindow": 100000},
]}
cat_account = {"access_token": "fb-token-1"}
try:
    catalog = W._freebuff_catalog(cat_account)
finally:
    W._json_request = _old_json_request
check("Freebuff 目录解析 key 为对外 id",
      [m["id"] for m in catalog]
      == ["deepseek/deepseek-v4-flash", "openai/gpt-6.1-sol"],
      catalog)
check("Freebuff 目录保留 handle",
      catalog[0]["handle"] == "fbm1.deepseek"
      and catalog[1]["handle"] == "fbm1.sol", catalog)
check("Freebuff 目录聊天上游用 key",
      catalog[0]["upstream_id"] == "deepseek/deepseek-v4-flash", catalog[0])
check("Freebuff 目录免费标记来自 premium",
      catalog[0]["free"] is True and catalog[1]["free"] is False, catalog)
check("Freebuff 目录过滤 locked 行",
      all(m["id"] != "locked/only" for m in catalog), catalog)
check("Freebuff 目录把 fetchId 写回账号",
      cat_account.get("freebuff_catalog_fetch_id") == "fetch-cat",
      cat_account)
check("Freebuff 目录缓存 fetchId",
      W._freebuff_catalog_meta(cat_account).get("fetch_id") == "fetch-cat",
      W._freebuff_catalog_meta(cat_account))
check("Freebuff 目录按账号保存 handle",
      cat_account.get("freebuff_catalog_handles") == {
          "deepseek/deepseek-v4-flash": "fbm1.deepseek",
          "openai/gpt-6.1-sol": "fbm1.sol",
      },
      cat_account.get("freebuff_catalog_handles"))

# 会话准入：聊天前必须 POST /session/admission，body 为空、模型走 handle 头。
admit_account = {
    "id": "acct-admit", "access_token": "fb-token-1",
    "freebuff_device_seed": W._b64url(dev_seed),
    "freebuff_device_public": W._b64url(dev_pub),
    "freebuff_device_key_id": "key-1",
    "freebuff_catalog_fetch_id": "fetch-admit",
}
admit_calls = []


def fake_admission(url, method="GET", headers=None, body=None, timeout=None):
    admit_calls.append({"url": url, "method": method, "headers": headers,
                        "body": body, "timeout": timeout})
    return {"status": "active", "instanceId": "cli:abc",
            "model": "z-ai/glm-5.3-flash",
            "expiresAt": "2099-01-01T00:00:00Z", "remainingMs": 3600000}


_old_json_request = W._json_request
W._json_request = fake_admission
try:
    admitted = W._freebuff_ensure_session(
        None, admit_account,
        {"upstream_id": "z-ai/glm-5.3-flash", "handle": "fbm1.glm"},
        fetch_id="fetch-admit", catalog_mode=True)
    admitted_again = W._freebuff_ensure_session(
        None, admit_account,
        {"upstream_id": "z-ai/glm-5.3-flash", "handle": "fbm1.glm"},
        fetch_id="fetch-admit", catalog_mode=True)
finally:
    W._json_request = _old_json_request
check("Freebuff 会话准入 URL",
      admit_calls and admit_calls[0]["url"] ==
      "https://www.codebuff.com/api/v1/freebuff/session/admission",
      admit_calls)
check("Freebuff 会话准入方法/空 body",
      admit_calls and admit_calls[0]["method"] == "POST"
      and admit_calls[0]["body"] is None, admit_calls)
admit_headers = (admit_calls[0]["headers"] if admit_calls else {})
check("Freebuff 会话准入发 handle 头",
      admit_headers.get("x-freebuff-model") == "fbm1.glm", admit_headers)
check("Freebuff 会话准入带目录快照",
      admit_headers.get("x-freebuff-catalog-fetch") == "fetch-admit",
      admit_headers)
check("Freebuff 会话准入多会话头",
      admit_headers.get("x-freebuff-multi-session") == "1"
      and admit_headers.get("x-freebuff-purchase-continuity") == "1",
      admit_headers)
check("Freebuff 会话准入额度头",
      admit_headers.get("x-freebuff-wallet-spend-limit") == "session",
      admit_headers)
check("Freebuff 会话准入 instance/attempt",
      str(admit_headers.get("x-freebuff-instance-id") or "").startswith("cli:")
      and bool(admit_headers.get("x-freebuff-desktop-attempt-id")),
      admit_headers)
check("Freebuff 会话准入设备头",
      admit_headers.get("x-freebuff-device-key") == "key-1", admit_headers)
check("Freebuff 会话准入返回 handle",
      admitted.get("handle") == "fbm1.glm", admitted)
check("Freebuff 会话准入落账号",
      admit_account.get("freebuff_session", {}).get("status") == "active"
      and admit_account["freebuff_session"].get("instance_id") == "cli:abc"
      and admit_account["freebuff_session"].get("model_key")
      == "z-ai/glm-5.3-flash",
      admit_account.get("freebuff_session"))
check("Freebuff 同模型会话复用",
      len(admit_calls) == 1 and admitted_again.get("handle") == "fbm1.glm",
      (len(admit_calls), admitted_again))

# 旧协议（静态兜底目录）没有 catalog/device 头，模型走 key。
legacy_account = {"id": "acct-legacy", "access_token": "fb-token-1"}
legacy_calls = []


def fake_legacy_admission(url, method="GET", headers=None, body=None,
                          timeout=None):
    legacy_calls.append({"url": url, "method": method, "headers": headers,
                         "body": body})
    return {"status": "active", "instanceId": "cli:legacy",
            "model": "deepseek/deepseek-v4-flash",
            "expiresAt": "2099-01-01T00:00:00Z"}


_old_json_request = W._json_request
W._json_request = fake_legacy_admission
try:
    legacy = W._freebuff_ensure_session(
        None, legacy_account,
        {"upstream_id": "deepseek/deepseek-v4-flash",
         "handle": "deepseek/deepseek-v4-flash"},
        fetch_id="", catalog_mode=False)
finally:
    W._json_request = _old_json_request
legacy_headers = (legacy_calls[0]["headers"] if legacy_calls else {})
check("Freebuff 旧协议准入发 key",
      legacy_headers.get("x-freebuff-model") == "deepseek/deepseek-v4-flash",
      legacy_headers)
check("Freebuff 旧协议不带目录/设备头",
      "x-freebuff-catalog-protocol" not in legacy_headers
      and "x-freebuff-catalog-fetch" not in legacy_headers
      and "x-freebuff-device-key" not in legacy_headers,
      legacy_headers)
check("Freebuff 旧协议准入返回 key",
      legacy.get("handle") == "deepseek/deepseek-v4-flash", legacy)
check("Freebuff 会话锁提示",
      "其他模型" in W._freebuff_session_message(
          409, {"status": "model_locked", "currentModel": "x"}),
      W._freebuff_session_message(409, {"status": "model_locked"}))
check("Freebuff 会话限流提示",
      "额度" in W._freebuff_session_message(
          429, {"status": "rate_limited"}),
      W._freebuff_session_message(429, {"status": "rate_limited"}))

# --- 登录会话：成功账号只交付一次 ----------------------------------------
session = W._new_session(W.PLATFORM_CLINE)
session["status"] = "success"
session["account"] = {"id": "acct-1", "platform": "cline",
                      "label": "A", "access_token": "x",
                      "refresh_token": "y", "expires_at": time.time() + 600}
check("poll 成功", W.poll_login(session["state"])["status"] == "success")
first = W.take_account(session["state"])
check("take_account 取到账号", first and first["access_token"] == "x")
check("take_account 只取一次", W.take_account(session["state"]) is None)
W.cancel_login(session["state"])
check("取消后状态保留成功", W.poll_login(session["state"])["status"] == "success")

# --- 平台池存取 / 目录 / 请求构造 ----------------------------------------
tmp = tempfile.mkdtemp(prefix="eggplatform-")
try:
    P.configure(tmp)
    pool = P.ensure_platform_pool("codearts")
    check("平台池已建", pool["kind"] == "platform", pool)
    check("平台池默认名", pool["name"] == "华为云 CodeArts", pool["name"])
    check("同一平台只有一个池",
          P.ensure_platform_pool("codearts")["id"] == pool["id"])

    ca["enabled"] = True
    ca["expires_at"] = time.time() + 3600
    pool = P.add_platform_account(pool["id"], ca)
    check("账号入池", len(pool["accounts"]) == 1, pool["accounts"])
    again = P.add_platform_account(pool["id"], ca)
    check("同 id 账号不重复", len(again["accounts"]) == 1, again["accounts"])

    view = P.public_pools()[0]
    check("面板视图有账号数", view["account_count"] == 1, view)
    check("面板视图有平台标签", view["platform_label"] == "华为云 CodeArts", view)
    check("面板视图不泄露凭据",
          all(k not in view["accounts"][0]
              for k in ("access_key", "secret_key", "security_token",
                        "dpop_private_jwk", "refresh_token")),
          view["accounts"][0])

    P.apply_platform_catalog(pool["id"], [
        {"id": "model-a", "name": "A", "channel": "benefit", "free": True},
        {"id": "model-b", "name": "B", "channel": "credit"},
    ])
    pool = P.get(pool["id"])
    check("平台目录落盘", len(pool["catalog"]) == 2, pool["catalog"])
    check("平台目录保留 channel",
          pool["catalog"][0]["channel"] == "benefit", pool["catalog"][0])
    # 对外 id 带平台前缀（免得和别的池子撞名），记住上游原始 id。
    check("平台目录带平台前缀",
          pool["catalog"][0]["id"] == "codearts/model-a", pool["catalog"][0])
    check("平台目录记住上游 id",
          pool["catalog"][0]["upstream_id"] == "model-a", pool["catalog"][0])
    check("新模型默认选中",
          set(pool["models"]) == {"codearts/model-a", "codearts/model-b"},
          pool["models"])
    check("按模型路由到平台池",
          (P.find_by_model("codearts/model-a") or {}).get("kind") == "platform")

    entries = P.catalog_entries()
    check("平台清单 sourceKind", entries[0]["sourceKind"] == "platform",
          entries[0])
    check("平台清单 platform 字段", entries[0]["platform"] == "codearts",
          entries[0])
    meta = wb_zen.model_entry_meta(entries[0])
    check("平台模型元数据", meta["egg_source_kind"] == "platform", meta)
    check("平台模型说明", "平台账号池" in meta["credits_note"],
          meta["credits_note"])

    # 请求构造：CodeArts 走签名 + 选通道
    chat = {"model": "codearts/model-a",
            "messages": [{"role": "user", "content": "hi"}]}
    url, body, headers, sse, source, billed = P.build_request(pool, chat)
    check("平台请求 URL", url.endswith("/api/v2/chat/completions"), url)
    check("平台请求带签名",
          str(headers.get("Authorization") or "").startswith(
              "SDK-HMAC-SHA256"), headers)
    check("benefit 通道带 maas_type", headers.get("maas_type") == "benefit",
          headers)
    check("平台请求模型（发上游的是原始 id）",
          body.get("model") == "model-a", body)
    check("平台请求 header 模型（原始 id）",
          headers.get("model-id") == "model-a", headers.get("model-id"))
    check("平台请求 source", source == pool["id"]
          and billed == "codearts/model-a")

    chat_b = {"model": "codearts/model-b",
              "messages": [{"role": "user", "content": "hi"}]}
    _u, _b, headers_b, _s, _src, _bill = P.build_request(pool, chat_b)
    check("credit 通道不带 maas_type", "maas_type" not in headers_b, headers_b)

    # Cline：Bearer 登录态
    cl["expires_at"] = time.time() + 3600
    cline_pool = P.ensure_platform_pool("cline")
    cline_pool = P.add_platform_account(cline_pool["id"], cl)
    P.apply_platform_catalog(cline_pool["id"], [
        {"id": "cline-model", "name": "Cline", "channel": "cline"}])
    cline_pool = P.get(cline_pool["id"])
    cu, cb, ch, cs, _csrc, _cbill = P.build_request(
        cline_pool, {"model": "cline/cline-model",
                     "messages": [{"role": "user", "content": "hi"}]})
    check("Cline 请求 URL", cu.endswith("/api/v1/chat/completions"), cu)
    # 令牌要带 workos: 前缀，否则 Cline 网关一律 401（看着像版本问题）。
    check("Cline 请求带 Bearer",
          ch.get("authorization") == "Bearer workos:access-1", ch)
    check("Cline 请求 body（发上游的是原始 id）",
          cb.get("model") == "cline-model", cb)
    # 已经带前缀的令牌不能再套一层。
    cl2 = dict(cl)
    cl2["access_token"] = "workos:abc"
    check("Cline 令牌前缀不重复",
          W._cline_bearer(cl2) == "workos:abc", W._cline_bearer(cl2))
    check("Cline 令牌补前缀",
          W._cline_bearer(cl) == "workos:access-1", W._cline_bearer(cl))

    # Freebuff：模型 id 带 freebuff/ 前缀，和已有池子的同名原始 id 不冲突。
    existing = P.upsert({
        "name": "Existing Pool",
        "base_url": "https://example.com/v1",
        "api_key": "sk-test",
        "models": ["z-ai/glm-5.3-flash"],
        "catalog": [{"id": "z-ai/glm-5.3-flash", "name": "Existing GLM"}],
    })
    fb_pool = P.ensure_platform_pool("freebuff")
    check("Freebuff 平台池默认名", fb_pool["name"] == "Freebuff", fb_pool)
    fb_pool = P.add_platform_account(fb_pool["id"], fb)
    P.apply_platform_catalog(fb_pool["id"], [
        {"id": "z-ai/glm-5.3-flash", "name": "GLM 5.3 Flash",
         "handle": "fbm1.glm", "free": True},
        # 同一个上游 id 重复出现时只留一行（目录/清单都不能出现重复 id）。
        {"id": "z-ai/glm-5.3-flash", "name": "GLM 5.3 Flash duplicate"},
        {"id": "deepseek/deepseek-v4-flash",
         "name": "DeepSeek V4.1 Flash 10/17", "handle": "fbm1.ds",
         "free": True},
    ])
    fb_pool = P.get(fb_pool["id"])
    check("Freebuff 目录带平台前缀",
          [m["id"] for m in fb_pool["catalog"]]
          == ["freebuff/z-ai/glm-5.3-flash",
              "freebuff/deepseek/deepseek-v4-flash"],
          [m["id"] for m in fb_pool["catalog"]])
    check("Freebuff 重复模型 id 去重", len(fb_pool["catalog"]) == 2,
          fb_pool["catalog"])
    check("Freebuff 目录记住上游 id",
          fb_pool["catalog"][0]["upstream_id"] == "z-ai/glm-5.3-flash",
          fb_pool["catalog"][0])
    check("Freebuff 目录记住 handle",
          fb_pool["catalog"][0]["handle"] == "fbm1.glm",
          fb_pool["catalog"][0])
    check("Freebuff 与已有池子的同名模型不冲突",
          (P.find_by_model("z-ai/glm-5.3-flash") or {}).get("id")
          == existing["id"]
          and (P.find_by_model("freebuff/z-ai/glm-5.3-flash") or {}).get("id")
          == fb_pool["id"],
          (P.find_by_model("z-ai/glm-5.3-flash"),
           P.find_by_model("freebuff/z-ai/glm-5.3-flash")))
    check("Freebuff 清单条目独立",
          len([e for e in P.catalog_entries()
               if e.get("platform") == "freebuff"]) == 2,
          P.catalog_entries())
    check("Freebuff 429 提示额度/会话",
          "免费额度" in P.error_hint(
              "https://www.codebuff.com", 429,
              "freebuff/z-ai/glm-5.3-flash", ""),
          P.error_hint("https://www.codebuff.com", 429,
                       "freebuff/z-ai/glm-5.3-flash", ""))
    check("Freebuff 401 提示重新登录",
          "浏览器授权" in P.error_hint("https://freebuff.com", 401,
                                       "freebuff/z-ai/glm-5.3-flash", "")
          and "浏览器授权" in P.error_hint(
              "https://www.codebuff.com", 401,
              "freebuff/z-ai/glm-5.3-flash", ""),
          P.error_hint("https://www.codebuff.com", 401,
                       "freebuff/z-ai/glm-5.3-flash", ""))
    check("Freebuff 409 句柄过期提示",
          "刷新模型" in P.error_hint(
              "https://www.codebuff.com", 409,
              "freebuff/z-ai/glm-5.3-flash",
              '{"error":"freebuff_catalog_stale"}'),
          P.error_hint("https://www.codebuff.com", 409,
                       "freebuff/z-ai/glm-5.3-flash",
                       '{"error":"freebuff_catalog_stale"}'))
    check("Freebuff 403 地区提示",
          "地区" in P.error_hint(
              "https://www.codebuff.com", 403,
              "freebuff/z-ai/glm-5.3-flash",
              '{"status":"country_blocked","countryCode":"CN"}'),
          P.error_hint("https://www.codebuff.com", 403,
                       "freebuff/z-ai/glm-5.3-flash",
                       '{"status":"country_blocked","countryCode":"CN"}'))
    fu, fb_body, fh, _fsse, _fsrc, _fbill = P.build_request(
        fb_pool, {"model": "freebuff/z-ai/glm-5.3-flash",
                  "messages": [{"role": "user", "content": "hi"}]})
    check("Freebuff 请求 URL",
          fu == "https://www.codebuff.com/api/v1/chat/completions", fu)
    check("Freebuff 请求带 Bearer",
          fh.get("authorization") == "Bearer fb-token-1", fh)
    check("Freebuff 请求 body 用目录 handle",
          fb_body.get("model") == "fbm1.glm", fb_body)
    check("Freebuff 聊天请求不塞 handle 头",
          "x-freebuff-model" not in fh, fh)
    check("Freebuff 请求带 client 头",
          fh.get("x-freebuff-client") == "cli", fh)
    check("Freebuff 请求带 acting user 头",
          fh.get("x-freebuff-acting-user-id") == "fb-user-1", fh)
    check("Freebuff 请求带 UA", fh.get("user-agent") == "wb-proxy/egg-platforms", fh)
    check("Freebuff 请求带目录快照头",
          fh.get("x-freebuff-catalog-fetch") == "fetch-chat", fh)
    check("Freebuff 请求带设备密钥头",
          fh.get("x-freebuff-device-key") == "key-chat", fh)
    try:
        chat_ts = int(fh.get("x-freebuff-device-ts") or 0)
    except (TypeError, ValueError):
        chat_ts = 0
    chat_body = W._json_bytes(fb_body)
    chat_signing = "\n".join([
        "freebuff-device-v1",
        "POST",
        "/api/v1/chat/completions",
        str(chat_ts),
        hashlib.sha256(chat_body).hexdigest(),
        "fetch-chat",
    ])
    chat_expected = W._b64url(E.sign(fb_seed, chat_signing.encode("utf-8"),
                                     public=fb_pub))
    check("Freebuff 请求设备签名可复现",
          fh.get("x-freebuff-device-sig") == chat_expected,
          (fh.get("x-freebuff-device-sig"), chat_expected))

    # handle 过期（目录刷新后 handle 变了）：旧会话不能复用，要重新开会话。
    stale_acct = {
        "id": "acct-stale", "access_token": "fb-token-1",
        "freebuff_device_seed": W._b64url(fb_seed),
        "freebuff_device_public": W._b64url(fb_pub),
        "freebuff_device_key_id": "key-1",
        "freebuff_catalog_fetch_id": "fetch-stale",
        "freebuff_session": {
            "status": "active", "instance_id": "cli:old",
            "attempt_id": "att-old", "model_key": "z-ai/glm-5.3-flash",
            "model_handle": "fbm1.old", "fetch_id": "fetch-stale",
            "expires_at": time.time() + 3600},
    }
    stale_calls = []

    def fake_stale(url, method="GET", headers=None, body=None, timeout=None):
        stale_calls.append({"url": url, "method": method})
        if method == "DELETE":
            return {}
        return {"status": "active", "instanceId": "cli:new",
                "model": "z-ai/glm-5.3-flash",
                "expiresAt": "2099-01-01T00:00:00Z"}

    _old_json_request = W._json_request
    W._json_request = fake_stale
    try:
        stale = W._freebuff_ensure_session(
            None, stale_acct,
            {"upstream_id": "z-ai/glm-5.3-flash", "handle": "fbm1.new"},
            fetch_id="fetch-stale", catalog_mode=True)
    finally:
        W._json_request = _old_json_request
    stale_methods = [c["method"] for c in stale_calls]
    check("Freebuff handle 变了要重开会话",
          stale_methods == ["DELETE", "POST"]
          and stale.get("handle") == "fbm1.new", (stale_methods, stale))
    check("Freebuff 重开会话后记住新 handle",
          stale_acct.get("freebuff_session", {}).get("model_handle")
          == "fbm1.new", stale_acct.get("freebuff_session"))

    # 目录刷新：强制重拉目录，并丢掉绑在旧 handle 上的会话。
    cat_pool = {"id": fb_pool["id"], "accounts": [stale_acct]}
    cat_calls = []

    def fake_catalog(url, method="GET", headers=None, body=None, timeout=None):
        cat_calls.append({"url": url, "method": method})
        if method == "DELETE":
            return {}
        return {"protocol": 1, "fetchId": "fetch-new",
                "issuedAt": 1790000000000,
                "rows": [{"key": "z-ai/glm-5.3-flash", "handle": "fbm1.new",
                          "displayName": "GLM 5.3 Flash", "premium": False,
                          "multimodal": True, "contextWindow": 200000}]}

    _old_json_request = W._json_request
    W._json_request = fake_catalog
    try:
        refreshed = W.refresh_catalog(cat_pool)
    finally:
        W._json_request = _old_json_request
    check("Freebuff 刷新目录成功", refreshed is True, refreshed)
    check("Freebuff 刷新目录真的重拉目录",
          any(c["url"].endswith("/api/v1/freebuff/models") for c in cat_calls),
          cat_calls)
    check("Freebuff 刷新目录后清掉旧会话",
          not stale_acct.get("freebuff_session"),
          stale_acct.get("freebuff_session"))
    check("Freebuff 刷新目录后拿到新 handle",
          stale_acct.get("freebuff_catalog_handles", {}).get(
              "z-ai/glm-5.3-flash") == "fbm1.new",
          stale_acct.get("freebuff_catalog_handles"))

    # 服务端不认设备密钥时，清掉 keyId 让下一次请求重新注册。
    key_pool = {"id": fb_pool["id"], "accounts": [
        {"id": "acct-key", "access_token": "fb-token-1",
         "freebuff_device_key_id": "key-dead"}]}
    forgot = W.forget_device_key(key_pool)
    check("Freebuff 设备密钥可重置",
          forgot is True
          and key_pool["accounts"][0].get("freebuff_device_key_id") == "",
          key_pool["accounts"][0])
    check("Freebuff 设备密钥重置后不重复重置",
          W.forget_device_key(key_pool) is False, key_pool["accounts"][0])

    # 网关层：识别「目录过期」与「设备密钥不认识」的响应。
    check("网关认得目录过期",
          wb_zen._is_catalog_stale(
              b'{"error":{"code":"freebuff_catalog_stale"}}')
          and not wb_zen._is_catalog_stale(b'{"error":"nope"}'),
          wb_zen._is_catalog_stale(
              b'{"error":{"code":"freebuff_catalog_stale"}}'))
    check("网关认得设备密钥不认识",
          wb_zen._is_device_key_unknown(
              b'{"error":{"code":"device_key_unknown"}}')
          and not wb_zen._is_device_key_unknown(b'{"error":"nope"}'),
          wb_zen._is_device_key_unknown(
              b'{"error":{"code":"device_key_unknown"}}'))

    # 端到端：刚登录、还没有目录快照的账号，第一次聊天也要按目录协议走
    # （拉目录 → 注册设备密钥 → 开会话 → 聊天 body 发 handle）。
    fb2 = W._freebuff_account(fb_user, fingerprint_id="fp-2",
                              fingerprint_hash="fh-2")
    fb2["id"] = "acct-fresh"
    fb_pool = P.add_platform_account(fb_pool["id"], fb2)
    P.set_platform_account_enabled(fb_pool["id"], fb["id"], False)
    e2e_calls = []

    def fake_e2e(url, method="GET", headers=None, body=None, timeout=None):
        e2e_calls.append({"url": url, "method": method, "headers": headers,
                          "body": body})
        if url.endswith("/api/v1/freebuff/models"):
            return {"protocol": 1, "fetchId": "fetch-e2e",
                    "issuedAt": 1790000000000,
                    "rows": [{"key": "z-ai/glm-5.3-flash",
                              "handle": "fbm1.e2e",
                              "displayName": "GLM 5.3 Flash", "premium": False,
                              "multimodal": True, "contextWindow": 200000}]}
        if url.endswith("/api/v1/freebuff/device-keys"):
            return {"keyId": "key-e2e"}
        if url.endswith("/api/v1/freebuff/session/admission"):
            return {"status": "active", "instanceId": "cli:e2e",
                    "model": "z-ai/glm-5.3-flash",
                    "expiresAt": "2099-01-01T00:00:00Z"}
        return {}

    _old_json_request = W._json_request
    W._json_request = fake_e2e
    try:
        e2e_pool = P.get(fb_pool["id"])
        eu, eb, eh, _es, _esrc, _ebill = P.build_request(
            e2e_pool, {"model": "freebuff/z-ai/glm-5.3-flash",
                       "messages": [{"role": "user", "content": "hi"}]})
    finally:
        W._json_request = _old_json_request
    e2e_urls = [c["url"] for c in e2e_calls]
    check("Freebuff 首次聊天先拉目录",
          "https://www.codebuff.com/api/v1/freebuff/models" in e2e_urls,
          e2e_urls)
    check("Freebuff 首次聊天注册设备密钥",
          "https://www.codebuff.com/api/v1/freebuff/device-keys" in e2e_urls,
          e2e_urls)
    check("Freebuff 首次聊天先开会话",
          "https://www.codebuff.com/api/v1/freebuff/session/admission"
          in e2e_urls, e2e_urls)
    check("Freebuff 首次聊天 body 用 handle",
          eb.get("model") == "fbm1.e2e", eb)
    check("Freebuff 首次聊天带目录快照头",
          eh.get("x-freebuff-catalog-fetch") == "fetch-e2e", eh)
    check("Freebuff 首次聊天带设备密钥头",
          eh.get("x-freebuff-device-key") == "key-e2e", eh)
    admit_call = next((c for c in e2e_calls
                       if c["url"].endswith("/session/admission")), None)
    check("Freebuff 首次聊天会话头用 handle",
          admit_call
          and (admit_call["headers"] or {}).get("x-freebuff-model")
          == "fbm1.e2e",
          (admit_call or {}).get("headers"))

    # 停用账号后不再被选中
    P.set_platform_account_enabled(pool["id"], ca["id"], False)
    check("停用账号后池子里没有可用账号",
          all(not a.get("enabled", True)
              for a in P.get(pool["id"])["accounts"]),
          P.get(pool["id"])["accounts"])
    try:
        W.ensure_account(P.get(pool["id"]))
        check("无可用账号时报错", False)
    except W.PlatformError as exc:
        check("无可用账号时报错", exc.status == 503, exc.detail)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if FAIL:
    print("\nFAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
print("\nALL PASS")
