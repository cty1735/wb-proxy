# -*- coding: utf-8 -*-
"""渠道校验（400 / code 11‑128）自检。

背景：上游偶发返回

    {"code":11‑128,"msg":"Illegal API invocation from an unapproved
     channel",...,"displayMsg":{"en":"Request blocked. Please send it again",...}}

（11 与 128 之间是 U+2011 不换行连字符，不是 ASCII 减号）。它不是账号问题也
不是内容审核：同一个 prompt 隔一下重发就过，所以网关应当重发有限次，而不是把
400 直接甩给客户端。这里验证两件事：

  1. 正文识别：U+2011 / ASCII 减号 / 英文与中文措辞都认；别的 400（11101 /
     11140 / 普通错误）不能误判。
  2. 重试循环：第一次 11‑128、第二次成功时客户端拿到正常响应；连续失败时
     只重发 CHANNEL_VERIFY_RETRIES 次，且不给账号记失败（账号是好的）。
"""

import io
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P

FAIL = []
NB_HYPHEN = chr(0x2011)   # 上游正文里的 U+2011 不换行连字符


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


# --- 1. 正文识别 ---------------------------------------------------------------
REAL = ('{"code":11' + NB_HYPHEN + '128,"msg":"Illegal API invocation from an '
        'unapproved channel","requestId":"01a16258e07b71679d7067e3891dcb0d",'
        '"displayMsg":{"en":"Request blocked. Please send it again","zh-hans":'
        '"请求被拦截，请重新发送"},"type":"server_error","code":400}')
ASCII = REAL.replace(NB_HYPHEN, "-")

check("识别上游真实正文（U+2011）", P.is_channel_verification_error(REAL))
check("ASCII 减号也识别", P.is_channel_verification_error(ASCII))
check("只有英文措辞也识别",
      P.is_channel_verification_error("Request blocked by channel verification"))
check("只有中文措辞也识别", P.is_channel_verification_error("本次调用未通过通道校验"))
check("11101 不误判", not P.is_channel_verification_error(
    '{"code":11101,"msg":"invalid argument"}'))
check("11140 不误判", not P.is_channel_verification_error(
    '{"code":11140,"msg":"request illegal"}'))
check("普通 400 不误判", not P.is_channel_verification_error(
    '{"code":400,"msg":"bad request"}'))
check("空正文不误判", not P.is_channel_verification_error(""))
check("正文里出现 11 与 128 但不相邻不误判",
      not P.is_channel_verification_error('{"code":11,"x":128}'))


# --- 2. 重试循环 ---------------------------------------------------------------
class FakeAffinity(object):
    def demote(self, *a, **k):
        pass


class FakePool(object):
    dir = ""

    def __init__(self, account):
        self.accounts = [account]
        self.affinity = FakeAffinity()

    def count_ready(self, realm, model=None):
        return 1

    def pick_for_session(self, realm=None, session_key=None, exclude=None,
                         model=None, page=None):
        return None if self.accounts[0].uid in (exclude or set()) else self.accounts[0]

    def get(self, uid):
        return self.accounts[0] if uid == self.accounts[0].uid else None

    def apply_remaining_weights(self, weights=None):
        pass


class FakeAccount(object):
    uid = "12345678-aaaa-bbbb-cccc-dddddddddddd"
    realm = "cn"
    enabled = True
    access_token = "token"
    product = P.wb_identity.PRODUCT_DESKTOP
    proxy = None
    fails = 0
    degrade_count = 0
    soft_streak = 0
    unscoped_streak = 0
    model_cooldowns = {}

    def __init__(self):
        self.successes = 0

    def chat_base_url(self):
        return "https://upstream.invalid"

    def headers(self, **kwargs):
        return {"Content-Type": "application/json"}

    def is_cli_identity(self):
        return False

    def note_success(self, model=None):
        self.successes += 1

    def note_failure(self, message=""):
        raise AssertionError("渠道校验不应给账号记失败: %r" % (message,))

    def note_unknown_failure(self, message=""):
        raise AssertionError("渠道校验不应给账号记失败: %r" % (message,))

    def note_error(self, message, model=None, cooldown=None, until=None, detail=""):
        raise AssertionError("渠道校验不应给账号记错误: %r" % (message,))

    def refresh(self):
        return False


class FakeResponse(object):
    def __init__(self):
        self.fp = None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeURL(object):
    """前 n 次抛 11‑128 的 400，之后返回一个假响应。"""

    def __init__(self, failures, fail_forever=False):
        self.failures = failures
        self.fail_forever = fail_forever
        self.calls = 0

    def __call__(self, req, timeout=None, proxy=None):
        self.calls += 1
        if self.fail_forever or self.calls <= self.failures:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(REAL.encode("utf-8")))
        return FakeResponse()


def run_open_upstream(urlopen):
    """在隔离的池/设置下跑一次 open_upstream，返回 (结果, 异常, 账号)。"""
    account = FakeAccount()
    saved = (P.POOL, P.CURRENT_REALM, P.wb_accounts.urlopen,
             P.apply_daily_guards, P.pinned_account,
             P.restriction_hold_remaining, P.detect_model_realm,
             P.wb_settings.egress_backoff_enabled)
    P.POOL = FakePool(account)
    P.CURRENT_REALM = "cn"
    P.wb_accounts.urlopen = urlopen
    P.apply_daily_guards = lambda *a, **k: None
    P.pinned_account = lambda: None
    P.restriction_hold_remaining = lambda realm: 0
    P.detect_model_realm = lambda model: "cn"
    P.wb_settings.egress_backoff_enabled = lambda *a, **k: False
    try:
        try:
            result = P.open_upstream({
                "model": "deepseek-v4.1-flash",
                "messages": [{"role": "user", "content": "hi"}],
            })
            return result, None, account
        except Exception as exc:  # 测试要区分并检查异常类型
            return None, exc, account
    finally:
        (P.POOL, P.CURRENT_REALM, P.wb_accounts.urlopen,
         P.apply_daily_guards, P.pinned_account,
         P.restriction_hold_remaining, P.detect_model_realm,
         P.wb_settings.egress_backoff_enabled) = saved


# 2a. 一次失败后重发成功：客户端拿到正常响应
fake = FakeURL(failures=1)
result, exc, account = run_open_upstream(fake)
check("失败一次后重发成功", exc is None and result is not None, repr(exc))
check("重发确实发了两次", fake.calls == 2, fake.calls)
check("成功时账号记的是成功", account.successes == 1, account.successes)

# 2b. 一直失败：只重发 CHANNEL_VERIFY_RETRIES 次，然后原样交给客户端
fake = FakeURL(failures=0, fail_forever=True)
result, exc, account = run_open_upstream(fake)
check("连续失败最终报错", isinstance(exc, urllib.error.HTTPError), repr(exc))
check("重发次数受上限约束",
      fake.calls == 1 + P.CHANNEL_VERIFY_RETRIES, fake.calls)
body = exc.read(600) if exc else b""
check("报错正文还能被调用方读到（没有被读空）",
      b"unapproved channel" in body, body)

print()
if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
print("ALL PASS")
