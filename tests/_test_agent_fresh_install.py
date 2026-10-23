# -*- coding: utf-8 -*-
"""七个客户端「一键配置」在全新 / 已有配置上的写入自检。

    python tests/_test_agent_fresh_install.py

覆盖每个客户端最容易踩的两种输入：

  - 全新安装：目标文件不存在，或只有一行 `{}` / `refs: {}` / 空文件
  - 已有配置：用户或客户端自己把条目写成**块式**（缩进子键），再次
    apply 时必须整段替换，不能留下 `key: {...}` 下面还挂着旧子行的坏结构

每个 case 都会真的写进临时目录，然后：plan 幂等（第二遍与落盘内容一致）、
JSON 可解析、YAML 没有「inline 值 + 更深缩进」的非法结构、restore 能还原
到最初的字节。全部在临时目录跑，不碰真实 home。
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_agents

FAIL = []
BASE = "http://127.0.0.1:8788"
KEY = "wb-test-key"
MODELS = [{"id": "m1", "context_window": 256000},
          {"id": "m2", "context_window": 128000}]


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def write(path, text):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read(path):
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def yaml_problems(text):
    """invalid `key: value` lines followed by deeper-indented content."""
    problems = []
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        m = re.match(r"^(\s*)([^\s#][^:]*):\s*(\S.*)$", line)
        if not m:
            continue
        value = m.group(3).strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            continue
        if not (value.startswith("{") or value.startswith("[")):
            continue
        if value.count("{") != value.count("}") \
                or value.count("[") != value.count("]"):
            problems.append("unbalanced flow: %r" % line)
            continue
        for follow in lines[idx + 1:]:
            if not follow.strip() or follow.strip().startswith("#"):
                continue
            if len(follow) - len(follow.lstrip(" ")) > len(m.group(1)):
                problems.append("orphan block under %r: %r" % (line, follow))
            break
    return problems


def run_case(client_id, files, model="m1", models=MODELS):
    label = wb_agents.CLIENTS[client_id]["label"]
    print("%s (%s)" % (label, client_id))
    root = tempfile.mkdtemp(prefix="wb-agents-")
    home = os.path.join(root, "home")
    state = os.path.join(root, "state")
    os.makedirs(home, exist_ok=True)
    try:
        paths = wb_agents.CLIENTS[client_id]["files"](home)
        for key, text in files.items():
            write(paths[key], text)

        built = wb_agents.plan(client_id, BASE, KEY, model=model,
                               models=models, home=home)
        check("plan returns files", bool(built["files"]), repr(built))
        if not built["files"]:
            return

        wb_agents.integrate(state, client_id, BASE, KEY, model=model,
                            models=models, home=home)
        for entry in built["files"]:
            path = entry["path"]
            now = read(path)
            check("%s written" % os.path.basename(path), now == entry["content"],
                  "")

        again = wb_agents.plan(client_id, BASE, KEY, model=model,
                               models=models, home=home)
        for entry in again["files"]:
            check("%s idempotent" % os.path.basename(entry["path"]),
                  entry["content"] == read(entry["path"]), "")

        for entry in built["files"]:
            path = entry["path"]
            text = read(path)
            if path.endswith(".json"):
                try:
                    json.loads(text)
                    check("%s parses as JSON" % os.path.basename(path), True)
                except ValueError as exc:
                    check("%s parses as JSON" % os.path.basename(path), False,
                          str(exc))
            elif path.endswith(".toml"):
                check("%s has a provider table" % os.path.basename(path),
                      "[model_providers." in text, "")
            else:
                problems = yaml_problems(text)
                check("%s has valid YAML shape" % os.path.basename(path),
                      not problems, "; ".join(problems))

        for key, path in paths.items():
            if not any(entry["path"] == path for entry in built["files"]):
                continue
            text = read(path)
            check("%s points at gateway" % os.path.basename(path),
                  BASE in text or KEY in text, "")

        before = {paths[key]: files.get(key) for key in paths}
        wb_agents.restore(state, client_id, home=home)
        for path, prior in before.items():
            if prior is None:
                check("%s removed on restore" % os.path.basename(path),
                      not os.path.exists(path), "")
            else:
                check("%s restored byte for byte" % os.path.basename(path),
                      read(path) == prior, "")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    saved_hermes = os.environ.get("HERMES_HOME")
    root = tempfile.mkdtemp(prefix="wb-hermes-")
    os.environ["HERMES_HOME"] = os.path.join(root, "hermes")
    try:
        print("=== 全新安装 ===")
        run_case("claude-code", {})
        run_case("claude-code", {"settings": ""})
        run_case("codex", {})
        run_case("opencode", {
            "config": '{\n  "$schema": "https://opencode.ai/config.json"\n}\n'})
        run_case("dsh", {
            "settings": "version: 1\n",
            "credentials": "version: 1\nrefs: {}\n"})
        run_case("crush", {"config": "{}\n"})
        run_case("zcode", {"config": ""})
        run_case("hermes", {
            "config": ("model:\n  provider: anthropic\n"
                       "  default: claude-sonnet-4-5\nproviders: {}\n")})

        print("")
        print("=== 已有块式条目（再次 apply 必须整段替换）===")
        run_case("hermes", {
            "config": ("providers:\n"
                       "  wb-proxy:\n"
                       "    api: http://old/v1\n"
                       "    key_env: OLD_KEY\n"
                       "    transport: chat_completions\n"
                       "  other:\n"
                       "    api: http://other/v1\n"
                       "model:\n"
                       "  provider: custom:wb-proxy\n"
                       "  default: old-model\n"),
            "env": "OLD_KEY=old\n"})
        run_case("dsh", {
            "settings": ("version: 1\n"
                         "llm-pi-ai:\n"
                         "  providers:\n"
                         "    wb-proxy:\n"
                         "      displayName: Old\n"
                         "      baseURL: http://old/v1\n"
                         "      models:\n"
                         "        - id: old-model\n"),
            "credentials": "version: 1\nrefs: {}\n"})
        print("")
        print("=== 已有 flow 条目（展开时必须保留其它键）===")
        run_case("hermes", {
            "config": ("model: {provider: anthropic, default: claude-sonnet-4-5}\n"
                       "providers: {other: {api: http://other/v1}}\n"),
            "env": ""})
        run_case("dsh", {
            "settings": ('version: 1\nllm-pi-ai: {providers: {zen-gate: '
                         '{baseURL: http://z/v1}}}\n'),
            "credentials": "version: 1\nrefs: {OTHER: keep}\n"})
    finally:
        os.environ.pop("HERMES_HOME", None)
        if saved_hermes is not None:
            os.environ["HERMES_HOME"] = saved_hermes
        shutil.rmtree(root, ignore_errors=True)

    print("")
    if FAIL:
        print("FAILED: %s" % ", ".join(FAIL))
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    sys.exit(main())
