"""DSH 身份指纹自检：WorkBuddy 上游的 11128 只认系统消息开头这句。

背景：DeepSeek Harness（DSH）把
    You are an AI agent powered by DeepSeek Harness.
放在系统提示的第一段。上游 channel 校验只有在这句话位于系统消息开头时
才返回 code 11128（前面加空格或任何前缀就放行，后面继续跟内容仍会拦）。
网关把结尾的 " Harness" 去掉，既保留「powered by DeepSeek」的身份，也能
正常出字；正文里单独出现的 "DeepSeek Harness" 上游不拦，不能一起改。
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


IDENTITY = "You are an AI agent powered by DeepSeek Harness."
REWRITTEN = "You are an AI agent powered by DeepSeek."

check("DSH 身份句被改写",
      P.sanitize_text(IDENTITY) == REWRITTEN,
      repr(P.sanitize_text(IDENTITY)))
check("身份句后面还有系统提示也改写",
      P.sanitize_text(IDENTITY + "\n\nYou are a helpful assistant.")
      == REWRITTEN + "\n\nYou are a helpful assistant.")
check("大小写变体同样处理",
      P.sanitize_text("you are an AI agent powered by deepseek harness.")
      == REWRITTEN)
check("开场带空格也处理",
      P.sanitize_text(" " + IDENTITY) == REWRITTEN,
      repr(P.sanitize_text(" " + IDENTITY)))
check("正文里的 DeepSeek Harness 保持原样",
      P.sanitize_text("DeepSeek Harness is the product name.")
      == "DeepSeek Harness is the product name.")
check("普通系统提示原样返回",
      P.sanitize_text("You are a helpful assistant.")
      == "You are a helpful assistant.")
check("has_fingerprint 能识别身份句", P.has_fingerprint(IDENTITY))
check("has_fingerprint 不误伤普通提及",
      not P.has_fingerprint("DeepSeek Harness is the product name."))
check("改写可重复执行（幂等）",
      P.sanitize_text(REWRITTEN) == REWRITTEN)

# 消息级入口：DSH 发的是 system 字符串，走 sanitize_messages 后必须干净。
msgs = P.sanitize_messages([{"role": "system", "content": IDENTITY}])
check("sanitize_messages 覆盖 system 内容",
      msgs[0]["content"] == REWRITTEN,
      repr(msgs[0]["content"]))

# 请求级入口：developer 角色会先归一成 system，再进入清洗，顺序不能反。
body = P.build_upstream_body({
    "model": "deepseek-v4.1-flash",
    "stream": True,
    "messages": [
        {"role": "developer", "content": IDENTITY},
        {"role": "user", "content": "hi"},
    ],
})
check("build_upstream_body 覆盖 developer→system 身份句",
      body["messages"][0] == {"role": "system", "content": REWRITTEN},
      repr(body["messages"][0]))

print()
if FAIL:
    print("FAILED: %s" % ", ".join(FAIL))
    sys.exit(1)
print("ALL PASS")
