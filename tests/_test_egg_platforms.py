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
check("平台列表含 CodeArts / Cline / ZCode",
      set(W.PLATFORM_IDS) == {"codearts", "cline", "zcode"}, W.PLATFORM_IDS)
check("CodeArts 默认池字段", W.defaults("codearts")["base_url"].startswith("https://"))
check("Cline 默认池字段", W.defaults("cline")["platform"] == "cline")
check("ZCode 默认池字段",
      W.defaults("zcode")["base_url"] == "https://api.z.ai/api/anthropic"
      and W.defaults("zcode")["protocol"] == "anthropic", W.defaults("zcode"))
check("ZCode 站点归一化",
      W._zcode_site("bigmodel") == "bigmodel"
      and W._zcode_site("zhipu") == "bigmodel"
      and W._zcode_site("zai") == "zai"
      and W._zcode_site("") == "zai")
check("ZCode 账号设备号稳定",
      W._zcode_key_device("abc.def") == W._zcode_key_device("abc.def")
      and len(W._zcode_key_device("abc.def")) == 36)
check("ZCode 赠送额度解析",
      W._zcode_bucket_models({"capabilities": ["model:glm-5.3", "tool:search"]})
      == ["glm-5.3"]
      and W._zcode_bucket_spent({"remaining_units": 0})
      and not W._zcode_bucket_spent({"remaining_units": 1}))
check("ZCode 模型名归一化",
      W._zcode_model_name("glm-5.3-flash") == "GLM-5.3-Flash"
      and W._zcode_model_name("Glm-5.3") == "GLM-5.3"
      and W._zcode_model_name("glm-x") == "glm-x")

# --- ZCode：Start Plan 解析（不联网，用桩替掉 _zcode_call） ---------------
_saved_zcode_call = W._zcode_call
_now = time.time()
W._zcode_call = lambda method, url, **kw: {
    "server_time": _now,
    "plans": [
        {"user_plan_id": "p1", "plan_id": "start-1024", "status": "active",
         "name": "ZCode 1024 Dev Day", "ends_at": _now + 3600},
    ],
    "balances": [
        {"user_plan_id": "p1", "plan_id": "start-1024",
         "capabilities": ["model:glm-5.3-flash"],
         "total_units": 300, "used_units": 100, "remaining_units": 200,
         "expires_at": _now + 1800},
        {"user_plan_id": "p1", "plan_id": "start-1024",
         "capabilities": ["model:glm-5.3"],
         "total_units": 1, "used_units": 1, "remaining_units": 0,
         "expires_at": _now + 1800},
    ],
}
start = W._zcode_start_plan("jwt.jwt.jwt", "dev-1")
W._zcode_call = _saved_zcode_call
check("Start Plan 解析出活模型",
      start and start["models"] == ["GLM-5.3-Flash"], start)
check("Start Plan 把用完的桶排除",
      start and start["all_models"] == ["GLM-5.3-Flash", "GLM-5.3"]
      and start["total"] == 300 and start["remaining"] == 200, start)
check("Start Plan 带用量桶",
      start and len(start.get("buckets") or []) == 2
      and start["buckets"][0]["used"] == 100
      and start["buckets"][0]["name"] == "GLM-5.3-Flash"
      and start["buckets"][1]["spent"] is True, start and start.get("buckets"))

# --- ZCode：Coding Plan 账号的清单 / 请求（不联网） -----------------------
coding = W._zcode_account("zai", "coding", W.ZCODE_ZAI_BASE, key="key-1234567890.abcdef",
                          plan="GLM Coding Plan", source="key",
                          models=[m["id"] for m in W.ZCODE_MODELS])
check("ZCode Key 账号不泄露凭据",
      "api_key" not in W.account_public(coding)
      and "jwt" not in W.account_public(coding)
      and "auth_token" not in W.account_public(coding), W.account_public(coding))
# 拉清单要读官方配置：测试里桩成「拿不到」，应回落内置兜底表。
_saved_zcode_config = W._zcode_config
W._zcode_config = lambda force=False: None
try:
    models, err = W.fetch_models({"platform": "zcode", "accounts": [coding]})
finally:
    W._zcode_config = _saved_zcode_config
check("ZCode Coding 账号清单",
      [m["id"] for m in models] == [m["id"] for m in W.ZCODE_MODELS]
      and not err, (models, err))
check("ZCode Coding 模型不标免费",
      all(m.get("free") is not True for m in models), models)

# --- ZCode：官方 provider 配置的解析 -------------------------------------
official_cfg = {"config": {
    "providerConfigRules": {"providerRules": [
        {"providerId": "account:zai-individual-coding-plan",
         "builtinModelIds": ["GLM-5.3-Flash", "GLM-5.2"]},
    ]},
    "modelConfigRules": {
        "builtinProviderModelRules": [],
        "modelRules": [
            {"modelMatch": "GLM-5.*", "config": {
                "properties": {"contextWindow": 1000000,
                               "inputFormat": {"supportsImage": False}},
                "optionSpecs": {"maxOutputTokens": {"max": 64000},
                                "reasoningLevel": {"values": ["low", "high", "max"]}},
            }},
        ],
    },
}}
official_rows = W._zcode_models_of(official_cfg, "account:zai-individual-coding-plan")
check("官方配置解析出模型",
      [m["id"] for m in official_rows] == ["GLM-5.3-Flash", "GLM-5.2"],
      official_rows)
check("官方配置带上下文 / 输出 / 推理档位",
      official_rows and official_rows[0]["context"] == 1000000
      and official_rows[0]["output"] == 64000
      and official_rows[0]["efforts"] == ["low", "high", "max"],
      official_rows)
check("官方配置 plan id 分流",
      W._zcode_plan_id(W.ZCODE_ZAI_BASE) == "account:zai-individual-coding-plan"
      and W._zcode_plan_id(W.ZCODE_BIGMODEL_BASE)
      == "account:bigmodel-individual-coding-plan"
      and W._zcode_plan_id(W.ZCODE_START_BASE) == "account:zai-start-plan")
check("兜底表齐全",
      [m["id"] for m in W._zcode_fallback_models()]
      == [m["id"] for m in W.ZCODE_MODELS])
_saved_zcode_config = W._zcode_config
W._zcode_config = lambda force=False: None
try:
    fallback = W._zcode_official_models(W.ZCODE_ZAI_BASE, force=True)
finally:
    W._zcode_config = _saved_zcode_config
check("官方配置拿不到时回落兜底表",
      [m["id"] for m in fallback] == [m["id"] for m in W.ZCODE_MODELS],
      fallback)
url, body, headers, conv, source, billed = W.build_request(
    {"id": "pool-z", "platform": "zcode", "accounts": [coding]},
    {"model": "zcode/GLM-5.3", "messages": [{"role": "user", "content": "hi"}],
     "max_tokens": 8})
check("ZCode Coding 请求 URL",
      url == "https://api.z.ai/api/anthropic/v1/messages", url)
check("ZCode Coding 请求带头",
      headers.get("x-api-key") == "key-1234567890.abcdef"
      and headers.get("authorization") == "Bearer key-1234567890.abcdef", headers)
check("ZCode Coding 请求模型是原始 id", body.get("model") == "GLM-5.3", body)

# --- ZCode：Start Plan 请求的打扮（不联网） -------------------------------
dress_body = {
    "model": "GLM-5.3-Flash",
    "max_tokens": 16,
    "stream": True,
    "messages": [{"role": "user",
                  "content": [{"type": "text", "text": "hi"}]}],
    "system": "agent's own system prompt",
    "tools": [{"name": "read", "input_schema": {"type": "object"}}],
}
dressed = W._zcode_dress(dict(dress_body), "bigmodel", "dev-2")
check("Start Plan 三段 system",
      isinstance(dressed.get("system"), list) and len(dressed["system"]) == 3
      and dressed["system"][0].get("cache_control", {}).get("type") == "ephemeral",
      dressed.get("system"))
check("Start Plan 并进自己的 system 提示词",
      dressed["system"][2]["text"].endswith("agent's own system prompt"),
      dressed["system"][2]["text"][-60:])
check("Start Plan 加环境前缀",
      isinstance(dressed.get("messages"), list) and len(dressed["messages"]) == 2
      and "<system-reminder>" in dressed["messages"][0]["content"][0]["text"])
check("Start Plan 带设备号",
      "device_id" in (dressed.get("metadata") or {}).get("user_id", ""),
      dressed.get("metadata"))
start_headers = W._zcode_start_headers("tok")
check("Start Plan 请求头",
      start_headers.get("authorization") == "Bearer tok"
      and start_headers.get("HTTP-Referer") == "https://zcode.z.ai"
      and start_headers.get("X-ZCode-Agent") == "glm"
      and start_headers.get("anthropic-version") == "2023-06-01", start_headers)

# --- ZCode：登录入口的入参校验（不联网） ---------------------------------
try:
    W.login_start("zcode", {"mode": "key", "key": "short"})
    check("ZCode 短 Key 报错", False)
except W.PlatformError as exc:
    check("ZCode 短 Key 报错", exc.status == 400, exc.detail)
try:
    W.login_start("zcode", {"mode": "nope"})
    check("ZCode 未知登录方式报错", False)
except W.PlatformError as exc:
    check("ZCode 未知登录方式报错", exc.status == 400, exc.detail)
check("ZCode 账号 id 不随登录态变",
      W._zcode_account("bigmodel", "start", W.ZCODE_BIGMODEL_BASE,
                       jwt="j1", user={"id": "u1"})["id"]
      == W._zcode_account("bigmodel", "start", W.ZCODE_BIGMODEL_BASE,
                          jwt="j2", user={"id": "u1"})["id"])
usage_account = W._zcode_account(
    "bigmodel", "start", W.ZCODE_BIGMODEL_BASE, jwt="j1", user={"id": "u1"},
    usage=[{"plan": "ZCode 1024 Dev Day", "name": "GLM-5.3-Flash",
            "models": ["GLM-5.3-Flash"], "total": 300, "used": 100,
            "remaining": 200, "spent": False}])
usage_public = W.account_public(usage_account).get("usage") or []
check("账号视图带用量",
      len(usage_public) == 1
      and usage_public[0]["plan"] == "ZCode 1024 Dev Day"
      and usage_public[0]["used"] == 100
      and usage_public[0]["remaining"] == 200
      and abs(usage_public[0]["percent"] - 33.33) < 0.01, usage_public)
check("用量视图不带凭据",
      "jwt" not in usage_public and "device" not in usage_public, usage_public)
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
