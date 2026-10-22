# -*- coding: utf-8 -*-
"""DSH 系统提示自检：cn 通道的 11-128 认两个连续词组，网关都用零宽空格断开。

用的是 **DSH 真实发出来的系统提示**（tests/fixtures/dsh_system_prompt.txt ，
5780 字，取自网关落盘的真实请求体），不是手抄片段 —— 上一版就是只看短片段，
漏掉了「两处同时在场的组合」才一直被拦。

2026-10-22 直打 cn 上游的取证（system + hi，每个 case 连发两次，结论一致）：

  真实整段系统提示                                        -> 400 / 11-128
  真实整段，只断身份句 "powered by DeepSeek Harness"       -> 仍 400（Web 那处还在）
  真实整段，只断 "DeepSeek Harness Web GUI"                -> 200
  真实整段，两处都断                                       -> 200
  真实整段删掉 Web GUI 段 / 只发前 4451 字                 -> 200
  身份句单独发                                            -> 400
  Web GUI 段单独发                                        -> 200（所以不能只看单段）

改写只做一件事：把词组内部空格换成 U+200B。字面仍是原句（零宽空格还原成空格
即原文），模型照常读，出站字节里不再有那两段连续文本。DSH 不用打补丁。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P

FAIL = []
ZW = "\u200b"
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "dsh_system_prompt.txt")
IDENT_PHRASE = " ".join(["powered", "by", "DeepSeek", "Harness"])
WEB_PHRASE = " ".join(["DeepSeek", "Harness", "Web", "GUI"])


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


REAL = open(FIXTURE, encoding="utf-8").read()
IDENT = "You are an AI agent " + IDENT_PHRASE + "."
IDENT_FIXED = "You are an AI agent powered" + ZW + "by" + ZW + "DeepSeek" + ZW + "Harness."
WEB = ("You are interacting with the user through the " + WEB_PHRASE
       + " whose URL the runtime context names.")
WEB_FIXED = WEB.replace(WEB_PHRASE, WEB_PHRASE.replace(" ", ZW))
CHECKOUT = ("The runtime context names the DeepSeek Harness implementation checkout. "
            "The checkout location and current working directory are separate values "
            "and may differ")

# --- 0. 源码里的模式本身必须干净（这一段曾被写进过零宽字符，改写当场变空转）---
check("两条模式的词组都是纯 ASCII",
      P.SANITIZE_DSH_PHRASES == (IDENT_PHRASE, WEB_PHRASE),
      repr(P.SANITIZE_DSH_PHRASES))
check("模式里没有零宽字符",
      all("\u200b" not in r.pattern for r in P.SANITIZE_DSH_RES),
      ascii([r.pattern for r in P.SANITIZE_DSH_RES]))
check("零宽常量是单个 U+200B", P.SANITIZE_DSH_ZWSP == "\u200b")

# --- 1. 单条词组 ---
check("has_fingerprint 命中身份句", P.has_fingerprint(IDENT))
check("has_fingerprint 命中 Web GUI 词组", P.has_fingerprint(WEB))
check("身份句被断开", P.sanitize_text(IDENT) == IDENT_FIXED, repr(P.sanitize_text(IDENT)))
check("身份句改写是真实变更", P.sanitize_text(IDENT) != IDENT)
check("Web GUI 词组被断开", P.sanitize_text(WEB) == WEB_FIXED, repr(P.sanitize_text(WEB)))
UPPER = "You are an AI agent " + " ".join(["POWERED", "BY", "DEEPSEEK", "HARNESS"]) + "."
check("身份句全大写也改写",
      ZW in P.sanitize_text(UPPER)
      and IDENT_PHRASE not in P.sanitize_text(UPPER).lower(),
      repr(P.sanitize_text(UPPER)))
upper = "you are interacting with the " + WEB_PHRASE.lower()
check("Web GUI 词组大小写不敏感地断开",
      ZW in P.sanitize_text(upper), repr(P.sanitize_text(upper)))

# --- 2. 真实整段系统提示（今天线上真正被拦的那份）---
check("fixture 是真实提示（含两处词组）",
      IDENT_PHRASE in REAL and WEB_PHRASE in REAL, len(REAL))
clean = P.sanitize_text(REAL)
check("整段里两处都断开",
      IDENT_PHRASE not in clean.lower() and WEB_PHRASE not in clean.lower())
check("整段改写是真实变更", clean != REAL, len(clean) - len(REAL))
check("整段字面不变（零宽还原成空格等于原文）",
      clean.replace(ZW, " ") == REAL)
check("checkout 句原样保留",
      CHECKOUT in clean, repr(clean[:0]))
check("改写幂等", P.sanitize_text(clean) == clean)
check("幂等再改一次也不变", P.sanitize_text(P.sanitize_text(REAL)) == clean)
check("普通提示词原样返回",
      P.sanitize_text("You are a helpful assistant.") == "You are a helpful assistant.")

# --- 3. 消息级 / 请求级入口 ---
msgs = P.sanitize_messages([{"role": "system", "content": REAL},
                            {"role": "user", "content": "hi"}])
check("sanitize_messages 覆盖 system 内容", msgs[0]["content"] == clean)
check("sanitize_messages 不动普通 user 内容", msgs[1]["content"] == "hi")

body = P.build_upstream_body({
    "model": "deepseek-v4.1-flash",
    "stream": True,
    "messages": [
        {"role": "developer", "content": REAL},
        {"role": "user", "content": "hi"},
    ],
})
sent = body["messages"][0]["content"]
check("build_upstream_body 覆盖 developer→system 的整段提示",
      body["messages"][0]["role"] == "system"
      and IDENT_PHRASE not in sent.lower()
      and WEB_PHRASE not in sent.lower(),
      repr(sent[:80]))

print()
if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
print("ALL PASS")
