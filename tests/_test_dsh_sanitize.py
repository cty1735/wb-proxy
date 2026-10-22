# -*- coding: utf-8 -*-
"""DSH 系统提示自检：上游 11-128 的触发点是 Web GUI 段落，不是身份句。

2026-10-22 实测（system 消息，直打上游）：

  * 只发 `You are an AI agent powered by DeepSeek.`                     -> 200
  * 只发 `The runtime context names the DeepSeek Harness implementation
    checkout. ...`                                                      -> 200
  * 只发下面这段                                                       -> 400 11-128
        You are interacting with the user through the DeepSeek Harness
        Web GUI whose URL the runtime context names. When the user refers
        to "this page", "this GUI", ... they mean this GUI.
  * 把这段里的 `DeepSeek Harness Web GUI` 换成 `harness web interface`  -> 200
    （只换 `Web GUI`、只换产品名、或把整句改短也都 200 —— 上游认的是
    这段文本，大小写不敏感，不是单个单词。）

所以网关出站时只改这四个词：语义不变，DSH 不用打补丁，升级也不会丢。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


IDENTITY = "You are an AI agent powered by DeepSeek."
WEB = ('You are interacting with the user through the DeepSeek Harness Web GUI '
       'whose URL the runtime context names. When the user refers to "this page", '
       '"this GUI", or "this app" without naming another target, they mean this '
       'GUI.')
FIXED = WEB.replace("DeepSeek Harness Web GUI", "harness web interface")

check("has_fingerprint 命中 Web GUI 段", P.has_fingerprint(WEB))
check("身份句不再触发改写", not P.has_fingerprint(IDENTITY))
check("身份句原样保留",
      P.sanitize_text(IDENTITY) == IDENTITY,
      repr(P.sanitize_text(IDENTITY)))
check("Web GUI 段被改写",
      P.sanitize_text(WEB) == FIXED,
      repr(P.sanitize_text(WEB)))
check("大小写变体也改写",
      P.sanitize_text(WEB.replace("DeepSeek Harness Web GUI",
                                  "deepseek harness web gui")) == FIXED,
      repr(P.sanitize_text(WEB.replace("DeepSeek Harness Web GUI",
                                       "deepseek harness web gui"))))
check("运行时上下文行里的同一词组也改写",
      P.sanitize_text('Current DeepSeek Harness Web GUI URL: "http://x/".')
      == 'Current harness web interface URL: "http://x/".')
check("改写幂等",
      P.sanitize_text(FIXED) == FIXED,
      repr(P.sanitize_text(FIXED)))
check("普通提示词原样返回",
      P.sanitize_text("You are a helpful assistant.") == "You are a helpful assistant.")

# 消息级入口
msgs = P.sanitize_messages([{"role": "system", "content": WEB},
                            {"role": "user", "content": "hi"}])
check("sanitize_messages 覆盖 system 内容", msgs[0]["content"] == FIXED)
check("sanitize_messages 不动普通 user 内容", msgs[1]["content"] == "hi")

# 请求级入口：developer 角色会先归一成 system，再进入清洗
body = P.build_upstream_body({
    "model": "deepseek-v4.1-flash",
    "stream": True,
    "messages": [
        {"role": "developer", "content": IDENTITY + "\n\n" + WEB},
        {"role": "user", "content": "hi"},
    ],
})
check("build_upstream_body 覆盖 developer→system 的 Web GUI 段",
      body["messages"][0] == {"role": "system",
                              "content": IDENTITY + "\n\n" + FIXED},
      repr(body["messages"][0]))

print()
if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
print("ALL PASS")
