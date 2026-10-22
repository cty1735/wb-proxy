"""Qoder 流式读取：上游早早就把错误信封送来了，不能等攒满缓冲区。

回归的是这么一件事：读上游用 read(65536) 会一直阻塞到攒够 64KB 或连接
关闭。Qoder 排队（10605 RESOURCE_LIMIT）时，0.2 秒就把 403 的 SSE 信封送
来了，却要等上游断开（实测 3 分钟）才看得见 —— 客户端那边就是干等三分钟
才报错。改用 read1()（只取已经到达的那一块）后，0.3 秒就能把错误报出去。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_qoder_chat

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


class _Resp(object):
    """假的上游响应。

    read() 模拟老行为：一次要等到「全部数据到齐」（= 攒满缓冲区才返回）；
    read1() 模拟新行为：一次只给已经到达的那一块。
    """

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.read_calls = 0
        self.read1_calls = 0

    def read(self, n=None):
        self.read_calls += 1
        data = b"".join(self.chunks)
        self.chunks = []
        return data

    def read1(self, n=None):
        self.read1_calls += 1
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


def _error_envelope():
    """Qoder 排队时的错误信封（statusCodeValue=403 + RESOURCE_LIMIT）。"""
    inner = json.dumps({
        "code": "10605",
        "message": json.dumps({
            "isQueued": True, "queueCount": 0, "queueType": "p3",
            "retryAfterSeconds": 30, "serviceAvailable": True,
            "subCode": "RESOURCE_LIMIT", "waitTime": 30,
        }),
        "subCode": "RESOURCE_LIMIT",
    })
    return {
        "headers": {"Content-Type": ["application/json"]},
        "body": inner,
        "statusCodeValue": 403,
        "statusCode": "FORBIDDEN",
    }


# 1. 错误信封：必须立刻报出来，而不是等连接关闭
resp = _Resp([("data:" + json.dumps(_error_envelope()) + "\n\n").encode("utf-8")])
try:
    list(wb_qoder_chat.iter_openai_chunks(resp, "DeepSeek-Flash"))
    check("错误信封要报错", False, "没有抛异常")
except wb_qoder_chat.QoderChatError as exc:
    check("错误信封要报错", "RESOURCE_LIMIT" in str(exc), str(exc))
    check("报错说清是资源受限", "资源受限" in str(exc), str(exc))
check("用 read1 而不是 read", resp.read1_calls >= 1 and resp.read_calls == 0,
      (resp.read1_calls, resp.read_calls))

# 2. 正常流：第一个 chunk 到了就能吐字，不用等攒满 64KB
def _ok_chunk(text):
    body = {"id": "c1", "choices": [{"index": 0, "delta": {"content": text},
                                     "finish_reason": None}]}
    return ("data:" + json.dumps({"body": body}) + "\n\n").encode("utf-8")


resp = _Resp([_ok_chunk("你好")])
chunks = list(wb_qoder_chat.iter_openai_chunks(resp, "DeepSeek-Flash"))
texts = []
for c in chunks:
    for ch in (c.get("choices") or []):
        delta = ch.get("delta") or {}
        if delta.get("content"):
            texts.append(delta["content"])
check("正常流能取到内容", "你好" in "".join(texts), texts)
check("正常流也用 read1", resp.read1_calls >= 1 and resp.read_calls == 0,
      (resp.read1_calls, resp.read_calls))

# 3. 没有 read1 的老响应对象：回退到 read()，仍能工作
class _OldResp(_Resp):
    read1 = None

    def __getattribute__(self, name):
        if name == "read1":
            raise AttributeError("read1")
        return object.__getattribute__(self, name)


resp = _OldResp([_ok_chunk("老接口")])
chunks = list(wb_qoder_chat.iter_openai_chunks(resp, "DeepSeek-Flash"))
texts = []
for c in chunks:
    for ch in (c.get("choices") or []):
        delta = ch.get("delta") or {}
        if delta.get("content"):
            texts.append(delta["content"])
check("没有 read1 时回退 read()", "老接口" in "".join(texts), texts)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
