"""wb_agents 的来源标签自检：模型名后面挂的短标签按当前提供商给。

背景：OpenCode 的模型选择器只搜模型名，所以网关写进客户端配置的每个模型名
都要挂一个「来源」标签。以前一律挂 wb，Qoder / Trae / 鸡蛋的模型在选择器里
也顶着 wb 的名字；现在按当前提供商给：wb / qoder / trae，选中鸡蛋时再按免费
源细分（kilo / opencode-zen）或用自建供应商池的名字。
"""

import json
import os
import sys
import tempfile

# 模块在仓库根目录：把根目录放进 sys.path 才能 import 到 wb_agents。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_agents as A

FAIL = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def opencode_names(models, provider, home):
    """跑一遍 OpenCode 的 apply，返回写进配置的「模型 id → 名字」。"""
    paths = {"config": os.path.join(home, ".config", "opencode", "opencode.json")}
    ctx = {
        "base_url": "http://127.0.0.1:8788",
        "base_url_v1": "http://127.0.0.1:8788/v1",
        "provider": provider,
        "api_key": "test-key",
        "model": None,
        "models": models,
    }
    out = A._apply_opencode(paths, ctx)
    data = json.loads(out[paths["config"]])
    node = data["provider"][A.PROVIDER_ID]
    return dict((mid, row.get("name")) for mid, row in node["models"].items())


home = tempfile.mkdtemp(prefix="agenttags-")
try:
    check("WorkBuddy 挂 wb",
          A.model_source_tag({"id": "hy3"}, "workbuddy") == "wb")
    check("Qoder 挂 qoder",
          A.model_source_tag({"id": "Auto"}, "qoder") == "qoder")
    check("Trae 挂 trae",
          A.model_source_tag({"id": "auto"}, "trae") == "trae")
    check("鸡蛋的 Kilo 免费池挂 kilo",
          A.model_source_tag({"source": "kilo", "source_label": "Kilo 免费池"},
                             "egg") == "kilo")
    check("鸡蛋的 Zen 车道挂 opencode-zen",
          A.model_source_tag({"source": "zen",
                              "source_label": "OpenCode Zen 免费车道"},
                             "egg") == "opencode-zen")
    check("自建池用池子名当标签",
          A.model_source_tag({"source": "pool-abc123",
                              "source_label": "NVIDIA NIM"},
                             "egg") == "nvidia-nim")
    check("鸡蛋但来源不明时挂 egg",
          A.model_source_tag({"id": "x"}, "egg") == "egg")
    check("没有提供商信息时退回 wb",
          A.model_source_tag({"id": "x"}) == "wb")

    names = opencode_names(
        [
            {"id": "hy3", "name": "Hy3"},
            {"id": "auto", "name": "Auto"},
            {"id": "kilo-auto/free", "name": "kilo-auto/free",
             "source": "kilo", "source_label": "Kilo 免费池"},
            {"id": "minimax/minimax-m3.1-flash:free",
             "name": "minimax/minimax-m3.1-flash:free",
             "source": "kilo", "source_label": "Kilo 免费池"},
            {"id": "gpt-5.6", "name": "GPT 5.6",
             "source": "zen", "source_label": "OpenCode Zen 免费车道"},
        ], "egg", home)
    check("鸡蛋的 Kilo 模型挂 kilo",
          names["minimax/minimax-m3.1-flash:free"].endswith(" kilo"),
          names["minimax/minimax-m3.1-flash:free"])
    check("鸡蛋的 Zen 模型挂 opencode-zen",
          names["gpt-5.6"].endswith(" opencode-zen"), names["gpt-5.6"])
    check("鸡蛋里来源不明的模型挂 egg",
          names["hy3"].endswith(" egg"), names["hy3"])

    names = opencode_names([{"id": "hy3", "name": "Hy3"}], "workbuddy", home)
    check("WorkBuddy 的模型挂 wb", names["hy3"] == "Hy3 wb", names["hy3"])

    names = opencode_names([{"id": "auto", "name": "Auto"}], "qoder", home)
    check("Qoder 的模型挂 qoder", names["auto"] == "Auto qoder", names["auto"])

    names = opencode_names([{"id": "auto", "name": "Auto"}], "trae", home)
    check("Trae 的模型挂 trae", names["auto"] == "Auto trae", names["auto"])
finally:
    import shutil
    shutil.rmtree(home, ignore_errors=True)

print("ALL PASS" if not FAIL else "FAILED: %s" % ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
