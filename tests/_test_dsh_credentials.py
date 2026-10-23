# -*- coding: utf-8 -*-
"""DSH .credentials.yaml 的 refs 写入自检。

    python tests/_test_dsh_credentials.py

新装的 DSH 会生成 `refs: {}`（flow 空 mapping），旧实现直接抛
`cannot upsert into refs: {}`，导致「一键配置」对全新用户必失败。
这里验证：

  - `refs: {}` 被展开成块式再插入（注释保留）
  - 没有 refs 时在文件末尾追加一个块
  - 已有块式 refs 时原地改值
  - `refs: {OTHER: x}` 展开时保留已有条目
  - 幂等：重复写入不改变文本
  - 非 flow 的 inline 值仍然报错，不静默改坏文件
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_agents

FAIL = []
KEY = wb_agents.CREDENTIAL_REF
VALUE = "wb-test-key"


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def main():
    print("DSH credentials refs")

    out = wb_agents.yaml_refs_upsert("version: 1\nrefs: {}\n", KEY, VALUE)
    check("empty flow mapping expands",
          "refs:\n  %s: %s\n" % (KEY, VALUE) in out and "refs: {}" not in out,
          repr(out))

    out = wb_agents.yaml_refs_upsert("version: 1\nrefs: {}  # keep\n", KEY, VALUE)
    check("trailing comment kept",
          out.split("\n")[1] == "refs:  # keep", repr(out))

    out = wb_agents.yaml_refs_upsert("version: 1\n", KEY, VALUE)
    check("missing refs block appended",
          out.rstrip("\n").endswith("refs:\n  %s: %s" % (KEY, VALUE)), repr(out))

    out = wb_agents.yaml_refs_upsert(
        "refs:\n  %s: old\n  OTHER: keep\n" % KEY, KEY, VALUE)
    check("existing block value replaced",
          "  %s: %s" % (KEY, VALUE) in out and "  OTHER: keep" in out
          and "old" not in out, repr(out))

    out = wb_agents.yaml_refs_upsert("refs: {OTHER: keep}\n", KEY, VALUE)
    check("non-empty flow mapping expands",
          "  OTHER: keep" in out and "  %s: %s" % (KEY, VALUE) in out, repr(out))

    once = wb_agents.yaml_refs_upsert("refs: {}\n", KEY, VALUE)
    twice = wb_agents.yaml_refs_upsert(once, KEY, VALUE)
    check("idempotent", once == twice, repr((once, twice)))

    try:
        wb_agents.yaml_refs_upsert("refs: null\n", KEY, VALUE)
        check("non-flow inline value raises", False)
    except wb_agents.AgentConfigError as exc:
        check("non-flow inline value raises", "refs" in str(exc), str(exc))

    print("")
    if FAIL:
        print("FAILED: %s" % ", ".join(FAIL))
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    sys.exit(main())
