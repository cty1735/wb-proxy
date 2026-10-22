"""Qoder 排队（10605 / RESOURCE_LIMIT）的识别与「等一会儿重试」。

上游在模型没容量时不拒绝请求，而是回一个 403 信封说「排队中」并附上
retryAfterSeconds（实测 30）。官方 qodercli 收到这个码是等一会儿重试
（docs.qoder.com/cli/sdk/errors），网关也该照做，而不是把「排队」当成
失败直接甩给客户端。

这里回归三件事：
  1. 403 信封能被认成「排队中」，并读出上游建议的等待秒数；
  2. 别的 403（不是排队）仍旧是普通错误，不会被误当成可重试；
  3. 适配器遇到排队会等 + 重开一条上游流接着读；预算用完才把排队报出去。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy
import wb_qoder_chat

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def _queue_envelope(wait=30, queued=True):
    """上游的排队信封：外层 statusCodeValue=403，里面裹着排队信息。

    queued=True 表示「真在排队」（serviceAvailable=true，等一会儿会轮到）；
    服务不可用（serviceAvailable=false）是另一种信封，见 _unavailable_envelope。
    """
    inner = json.dumps({
        "code": "10605",
        "message": json.dumps({
            "isQueued": queued,
            "modelKey": "dfmodel",
            "queueCount": 0,
            "queueType": "p3",
            "retryAfterSeconds": wait,
            "serviceAvailable": True,
            "subCode": "RESOURCE_LIMIT",
            "waitTime": wait,
        }),
        "subCode": "RESOURCE_LIMIT",
    })
    return {
        "headers": {"Content-Type": ["application/json"]},
        "body": inner,
        "statusCodeValue": 403,
        "statusCode": "FORBIDDEN",
    }


def _other_403_envelope():
    """不是排队的 403：没有 isQueued / serviceAvailable 这套排队字段。"""
    inner = json.dumps({
        "code": "403",
        "message": "no permission for this model",
        "subCode": "FORBIDDEN",
    })
    return {
        "headers": {"Content-Type": ["application/json"]},
        "body": inner,
        "statusCodeValue": 403,
        "statusCode": "FORBIDDEN",
    }


# 1. 排队信封：认得出来，且带得上游建议的等待秒数
err = wb_qoder_chat._qoder_status_error(_queue_envelope(wait=30), "DeepSeek-Flash")
check("排队信封认成 QoderQueuedError", isinstance(err, wb_qoder_chat.QoderQueuedError),
      type(err).__name__)
check("排队带出上游建议的等待秒数", getattr(err, "retry_after", 0) == 30,
      getattr(err, "retry_after", None))
check("排队带出队列类型", getattr(err, "queue_type", "") == "p3",
      getattr(err, "queue_type", None))
check("排队也是 QoderChatError（老代码照旧接得住）",
      isinstance(err, wb_qoder_chat.QoderChatError))
check("排队文案说清是排队",
      "排队" in str(err) and "DeepSeek-Flash" in str(err), str(err))

# waitTime 也要认（上游有时只给这个字段）
err = wb_qoder_chat._qoder_status_error(_queue_envelope(wait=45), "M")
check("waitTime 也算等待秒数", getattr(err, "retry_after", 0) == 45,
      getattr(err, "retry_after", None))

# 2. 不是排队的 403：仍是普通错误
err = wb_qoder_chat._qoder_status_error(_other_403_envelope(), "DeepSeek-Flash")
check("非排队 403 不是 QoderQueuedError",
      not isinstance(err, wb_qoder_chat.QoderQueuedError), type(err).__name__)
check("非排队 403 仍是 QoderChatError", isinstance(err, wb_qoder_chat.QoderChatError))

# serviceAvailable=true 的信封不是排队
err = wb_qoder_chat._qoder_status_error(_queue_envelope(queued=False), "M")
check("serviceAvailable=true 不算排队",
      not isinstance(err, wb_qoder_chat.QoderQueuedError), type(err).__name__)


# 3. 适配器：排队时等 + 重开一条上游流接着读
class _Resp(object):
    """假的上游响应：按脚本吐出若干块。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def read1(self, n=None):
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

    def read(self, n=None):
        return self.read1(n)

    def close(self):
        self.closed = True


def _queue_frame(wait=30):
    return ("data:" + json.dumps(_queue_envelope(wait=wait)) + "\n\n").encode("utf-8")


def _ok_frame(text="你好"):
    body = {"id": "c1", "choices": [{"index": 0, "delta": {"content": text},
                                      "finish_reason": None}]}
    return ("data:" + json.dumps({"body": body}) + "\n\n").encode("utf-8")


waits = []
wb_proxy.time.sleep = lambda s: waits.append(s)

# 第一条流排队，重开的那条正常 -> 客户端拿到的是重开后的内容
first = _Resp([_queue_frame(wait=30)])
second = _Resp([_ok_frame("排队之后才拿到")])
reopened = []


def _reopen():
    reopened.append(1)
    return second


adapter = wb_proxy.QoderUpstreamAdapter(first, "DeepSeek-Flash", reopen=_reopen,
                                        queue_wait_budget=120)
out = b""
while True:
    piece = adapter.read1(65536)
    if not piece:
        break
    out += piece
adapter.close()
check("排队后重开了一条上游流", len(reopened) == 1, len(reopened))
check("按上游建议等了 30 秒", waits == [30], waits)
check("客户端拿到的是重开后的内容", "排队之后才拿到" in out.decode("utf-8"),
      out.decode("utf-8", "replace")[:200])
check("旧的排队连接被关掉了（上游会挂 3 分钟）", first.closed)

# 预算用完：不再重开，把排队如实报出去
waits[:] = []
reopened[:] = []
streams = []


def _always_queued():
    """每次重开都还是排队 —— 模拟上游一直没容量。"""
    resp = _Resp([_queue_frame(wait=30)])
    streams.append(resp)
    return resp


adapter = wb_proxy.QoderUpstreamAdapter(_always_queued(), "M", reopen=_always_queued,
                                        queue_wait_budget=60)
raised = None
try:
    while True:
        if not adapter.read1(65536):
            break
except Exception as exc:      # noqa: BLE001 - 这里就是要看抛什么
    raised = exc
check("预算用完就抛出去", isinstance(raised, wb_qoder_chat.QoderQueuedError),
      type(raised).__name__)
check("预算用完前等了 2 轮（60 秒 / 每次 30）", waits == [30, 30], waits)
check("预算用完就不再重开", len(streams) == 3, len(streams))  # 1 条初始 + 2 次重开
check("放弃时说清已等了多久",
      "已按上游建议等待 60 秒" in str(raised), str(raised))

# 没有 reopen 时不重试：直接把排队报出去（老行为）
waits[:] = []
always = _Resp([_queue_frame(wait=30)])
adapter = wb_proxy.QoderUpstreamAdapter(always, "M")
raised = None
try:
    adapter.read1(65536)
except Exception as exc:      # noqa: BLE001
    raised = exc
check("没有 reopen 就不重试", isinstance(raised, wb_qoder_chat.QoderQueuedError),
      type(raised).__name__)
check("没有 reopen 就不等", waits == [], waits)


# 4. serviceAvailable=false：这条队列没有服务，等多久都不会轮到 ——
#    必须立刻报出去（面板测试 30 秒超时就是因为这里以前会一直等）。
def _unavailable_envelope(wait=16):
    inner = json.dumps({
        "code": "10605",
        "message": json.dumps({
            "isQueued": True, "modelKey": "auto", "queueCount": 0,
            "queueType": "p3", "retryAfterSeconds": wait,
            "serviceAvailable": False, "subCode": "RESOURCE_LIMIT",
            "waitTime": wait,
        }),
        "subCode": "RESOURCE_LIMIT",
    })
    return {
        "headers": {"Content-Type": ["application/json"]},
        "body": inner,
        "statusCodeValue": 403,
        "statusCode": "FORBIDDEN",
    }


err = wb_qoder_chat._qoder_status_error(_unavailable_envelope(), "Auto")
check("serviceAvailable=false 仍是 QoderQueuedError",
      isinstance(err, wb_qoder_chat.QoderQueuedError), type(err).__name__)
check("serviceAvailable=false 被记下来",
      getattr(err, "service_available", None) is False,
      getattr(err, "service_available", None))
check("serviceAvailable=false 的文案说清「没有服务」",
      "serviceAvailable=false" in str(err) and "不会" in str(err), str(err))

waits[:] = []
reopened[:] = []
unavailable = _Resp([("data:" + json.dumps(_unavailable_envelope()) + "\n\n").encode("utf-8")])
adapter = wb_proxy.QoderUpstreamAdapter(unavailable, "Auto", reopen=_reopen,
                                        queue_wait_budget=120)
raised = None
try:
    adapter.read1(65536)
except Exception as exc:      # noqa: BLE001
    raised = exc
check("serviceAvailable=false 立刻报错", isinstance(raised, wb_qoder_chat.QoderQueuedError),
      type(raised).__name__)
check("serviceAvailable=false 不等待", waits == [], waits)
check("serviceAvailable=false 不重开", reopened == [], reopened)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
