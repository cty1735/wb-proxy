"""活网关巡检：四个网关的每个模型都真打一次。

对每个提供商的每一个模型（清单取自活网关的 /v1/models，也就是客户端真正
看到的那份），发一条**不带 provider 提示**的流式请求——这正是 OpenCode 这类
客户端的形状——然后核对两件事：

  1. 流以 finish_reason 收尾（客户端不会判定「流被截断」而重试）
  2. 网关把这次记在了**该模型自己的**网关名下（workbuddy / qoder / trae / egg），
     没有串到别家

用法：
    python tests/_test_provider_sweep.py [--base http://127.0.0.1:8788]
                                         [--key <api key>] [--only workbuddy]
                                         [--timeout 90] [--delay 0.3]

注意：这是**真打上游**，会消耗账号额度，也会往活网关的用量日志
（usage/usage.jsonl）里写巡检行。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_proxy as P  # noqa: E402  (只用它做归属判定)

PROVIDERS = ("workbuddy", "qoder", "trae", "egg")
REALMS = ("intl", "cn")


def http_json(url, key=None, body=None, timeout=120, raw=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if data is None:
        headers = {}
    if key:
        headers["Authorization"] = "Bearer %s" % key
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            if raw:
                return resp.status, text
            try:
                return resp.status, json.loads(text)
            except Exception:
                return resp.status, {}
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        if raw:
            return exc.code, text
        try:
            return exc.code, json.loads(text)
        except Exception:
            return exc.code, {"raw": text}
    except Exception as exc:
        return 0, {"raw": str(exc)}


def panel_token(base, password="admin"):
    status, body = http_json(base + "/panel/login", body={"password": password},
                              timeout=20)
    if status != 200 or not body.get("token"):
        return None
    return body["token"]


def usage_rows(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8788")
    ap.add_argument("--key", default=os.environ.get("WB_PROXY_KEY") or
                    "wb-XrJVTDNsPNZDxS_y8wDWE0Mh-59mpKRg")
    ap.add_argument("--only", default="", help="只跑某个提供商")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--usage", default=os.path.join(ROOT, "usage", "usage.jsonl"))
    ap.add_argument("--max-errors", type=int, default=12)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    token = panel_token(base)
    print("panel token:", "ok" if token else "FAILED (面板登录不上)")

    # 清单：客户端真正看到的那份
    catalogs = {}
    for provider in PROVIDERS:
        ids = []
        realms = REALMS if provider != "egg" else ("",)
        for realm in realms:
            url = "%s/v1/models?provider=%s" % (base, provider)
            if realm:
                url += "&realm=" + realm
            status, body = http_json(url, key=args.key, timeout=60)
            if status != 200:
                print("  ! 清单拉取失败 %s/%s: %s" % (provider, realm or "-", status))
                continue
            for item in body.get("data") or []:
                mid = str(item.get("id") or "").strip()
                if mid and mid not in ids:
                    ids.append(mid)
        catalogs[provider] = ids
        print("%-10s %4d 个模型" % (provider, len(ids)))

    if args.only:
        catalogs = {args.only: catalogs.get(args.only, [])}

    # 归属：id 归谁（同名按固定顺序第一家）
    owners = {}
    for provider, ids in catalogs.items():
        for mid in ids:
            owners.setdefault(mid, set()).add(provider)
    order = ("workbuddy", "qoder", "trae", "egg")
    expected = {}
    for mid, who in owners.items():
        expected[mid] = sorted(who, key=order.index)[0]

    print()
    print("%-34s %-9s %-22s %s" % ("model", "期望网关", "实际网关", "结果"))
    print("-" * 96)

    stats = {"ok": 0, "no_finish": 0, "http_error": 0, "wrong_provider": 0,
             "unknown": 0}
    problems = []
    seen = set()
    for provider in PROVIDERS:
        if args.only and provider != args.only:
            continue
        for mid in catalogs.get(provider, []):
            if mid in seen:
                continue
            seen.add(mid)
            want = expected[mid]
            before = len(usage_rows(args.usage))
            status, raw = http_json(
                "%s/v1/chat/completions" % base, key=args.key,
                body={"model": mid, "stream": True, "max_tokens": 8,
                      "messages": [{"role": "user", "content": "ping"}]},
                timeout=args.timeout, raw=True)
            time.sleep(args.delay)

            finishes = []
            has_error = False
            err_text = ""
            if status == 200:
                for line in raw.splitlines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    if obj.get("error"):
                        has_error = True
                        err_text = str((obj.get("error") or {}).get("message") or "")[:60]
                    for ch in obj.get("choices") or []:
                        finishes.append(ch.get("finish_reason"))
            elif status:
                try:
                    err_text = json.dumps(json.loads(raw), ensure_ascii=False)[:60]
                except Exception:
                    err_text = raw[:60]

            # 网关把这次记给了谁
            got = ""
            rows = usage_rows(args.usage)
            for row in rows[before:]:
                if str(row.get("model") or "") == mid or \
                        str(row.get("model") or "").endswith(mid):
                    got = str(row.get("provider") or "")
            if not got:
                for row in reversed(rows[-5:]):
                    if str(row.get("model") or "") == mid:
                        got = str(row.get("provider") or "")
                        break

            if status == 200 and has_error:
                # 上游在流里回了错误：网关已经把它变成错误事件 + 收尾块发过来了，
                # 客户端能看到真实原因（但这次请求没成功）。
                result = "上游错误: %s" % err_text
                stats["http_error"] += 1
                problems.append((mid, want, got, result))
            elif status == 200 and any(f for f in finishes):
                result = "ok"
                stats["ok"] += 1
                if got and got != want:
                    result = "!! 记到了 %s" % got
                    stats["wrong_provider"] += 1
                    problems.append((mid, want, got, result))
            elif status == 200:
                result = "!! 流没有 finish_reason"
                stats["no_finish"] += 1
                problems.append((mid, want, got, result))
            else:
                result = "HTTP %s %s" % (status, err_text)
                stats["http_error"] += 1
                problems.append((mid, want, got, result))

            flag = "  " if result in ("ok",) or result.startswith("上游错误") \
                or result.startswith("HTTP") else "!!"
            print("%s %-31s %-9s %-22s %s"
                  % (flag, mid[:31], want, got or "-", result[:60]))

    print()
    print("合计: %s" % json.dumps(stats, ensure_ascii=False))
    if problems:
        print()
        print("有问题的 %d 个（最多列 %d 个）：" % (len(problems), args.max_errors))
        for mid, want, got, result in problems[:args.max_errors]:
            print("  - %-32s 期望 %-9s 实际 %-9s %s" % (mid[:32], want, got or "-", result))
    return 1 if (stats["no_finish"] or stats["wrong_provider"]) else 0


if __name__ == "__main__":
    sys.exit(main())
