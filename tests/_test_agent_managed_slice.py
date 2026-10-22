"""客户端自己重排配置时，不该被当成「用户改了我们那段」。

背景：Hermes 会自己重写 config.yaml（把 `default: "x"` 写成 `default: x`）。
面板判断「我们写的那一段有没有被外部改过」以前比的是原文，指纹一变就判成
「外部修改过」→ 面板切提供商时重刷模型清单会**跳过**这个客户端，它的模型
表就一直停在上一家提供商的目录上（Hermes 的模型列表切到鸡蛋后不更新）。

现在比的是**取值**：引号、键序、空格差异都不算改过；值真的变了才算。

    python tests/_test_agent_managed_slice.py
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_agents

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


CONFIG = (
    "model:\n"
    "  default: \"beam-free\"\n"
    "  provider: \"custom:wb-proxy\"\n"
    "providers:\n"
    "  wb-proxy: {\"api\": \"http://127.0.0.1:8788/v1\", \"key_env\": "
    "\"WB_PROXY_API_KEY\", \"models\": {\"beam-free\": {\"context_length\": "
    "131072}}}\n"
)
# 客户端重排后的同一份配置：引号没了、provider 块的键序换了。
REWRITTEN = (
    "model:\n"
    "  default: beam-free\n"
    "  provider: custom:wb-proxy\n"
    "providers:\n"
    "  wb-proxy: {\"models\": {\"beam-free\": {\"context_length\": 131072}}, "
    "\"key_env\": \"WB_PROXY_API_KEY\", \"api\": "
    "\"http://127.0.0.1:8788/v1\"}\n"
)

tmp = tempfile.mkdtemp(prefix="managed-")
try:
    paths = {"config": os.path.join(tmp, "config.yaml"),
             "env": os.path.join(tmp, ".env")}
    with open(paths["env"], "w", encoding="utf-8") as fh:
        fh.write("WB_PROXY_API_KEY=wb-test\n")

    def projection(text):
        contents = {paths["config"]: text, paths["env"]: open(
            paths["env"], encoding="utf-8").read()}
        return wb_agents._managed_hermes(paths, contents)

    written = projection(CONFIG)
    rewritten = projection(REWRITTEN)
    check("引号 / 键序变了，取值不变", written == rewritten,
          "\n        " + written[:200] + "\n        " + rewritten[:200])

    # 真的改了模型表 → 取值必须不同（不能把真改动也吞掉）。
    changed = projection(REWRITTEN.replace(
        '"beam-free": {"context_length": 131072}',
        '"glm-5.3": {"context_length": 1000000}'))
    check("模型表真的变了 → 取值不同", changed != rewritten)

    # 指纹也跟着走：同一份内容，指纹稳定。
    fp_a = wb_agents._managed_fingerprint(
        wb_agents.CLIENTS["hermes"], paths,
        {paths["config"]: CONFIG, paths["env"]: "WB_PROXY_API_KEY=wb-test\n"})
    fp_b = wb_agents._managed_fingerprint(
        wb_agents.CLIENTS["hermes"], paths,
        {paths["config"]: REWRITTEN, paths["env"]: "WB_PROXY_API_KEY=wb-test\n"})
    check("指纹对重排稳定", fp_a == fp_b, "%s != %s" % (fp_a, fp_b))

    # 取值可以直接存进 state（apply 记的就是它）。
    values = json.loads(rewritten)
    check("取值是可序列化的 JSON", isinstance(values, dict) and values,
          values)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAIL:
    print("FAILED: %d" % len(FAIL))
    for name in FAIL:
        print("  - %s" % name)
    sys.exit(1)
print("ALL PASS")
