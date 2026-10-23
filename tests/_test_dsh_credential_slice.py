# -*- coding: utf-8 -*-
"""DSH `.credentials.yaml` 的 managed 投影自检。

    python tests/_test_dsh_credential_slice.py

网关把自己的 Key 写进 `~/.dsh/.credentials.yaml` 的 `refs:` 块，判断
「用户有没有手改过我们那段」时也要读回来。旧实现用 dotenv 读取器
(`KEY=VALUE`) 去读 YAML 的 `KEY: value`，投影里 credential 恒为 null：

  - 任何真实 Key 都不会被记进 state，等于没在管这一项
  - 修好读取后，旧记录会与新投影不同，所有已接入的 DSH 会被一次性读成
    external_change，池子的自动同步就不再碰它们

这里验证读取（含引号形态）与旧记录的迁移比对。全部在临时目录跑。
"""

import io
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
KEY = "wb-real-key"
BASE = "http://127.0.0.1:8788"
MODELS = [{"id": "m1", "context_window": 256000}]


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        FAIL.append(name)


def read(path):
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


class Case(object):
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="wb-dsh-cred-")
        self.home = os.path.join(self.root, "home")
        self.state = os.path.join(self.root, "state")
        dsh = os.path.join(self.home, ".dsh")
        os.makedirs(dsh)
        self.settings = os.path.join(dsh, "settings.yaml")
        self.creds = os.path.join(dsh, ".credentials.yaml")
        write(self.settings, "version: 1\n")
        write(self.creds, "version: 1\nrefs: {}\n")
        self.apply()

    def apply(self):
        wb_agents.integrate(self.state, "dsh", BASE, KEY, model="m1",
                            models=MODELS, home=self.home)

    def record(self):
        path = os.path.join(self.state, wb_agents.STATE_FILE)
        with io.open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def patch_record(self, mutate):
        state = self.record()
        mutate(state["dsh"])
        write(os.path.join(self.state, wb_agents.STATE_FILE),
              json.dumps(state, ensure_ascii=False))

    def external(self):
        return wb_agents.overview(self.state, home=self.home)["dsh"][
            "applied"]["external_change"]

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def main():
    print("DSH credential slice")

    case = Case()
    try:
        values = json.loads(case.record()["dsh"]["managed_values"])
        check("credential recorded", values["credential"] == KEY,
              repr(values.get("credential")))
        check("fresh apply is not an external change",
              case.external() is False)

        # 客户端自己给值加引号：值没变，不该算手改。
        write(case.creds, 'version: 1\nrefs:\n  %s: "%s"\n'
              % (wb_agents.CREDENTIAL_REF, KEY))
        check("quoted value still matches", case.external() is False)

        # 用户换了 Key：我们那一段确实被改了。
        write(case.creds, "version: 1\nrefs:\n  %s: hand-edited\n"
              % wb_agents.CREDENTIAL_REF)
        check("hand-edited key is an external change",
              case.external() is True)

        # 旧记录（修复前写下的）：credential 恒为 null。
        case.apply()
        case.patch_record(lambda record: record.update({
            "managed_values": json.dumps(
                dict(json.loads(record["managed_values"]), credential=None),
                sort_keys=True, ensure_ascii=False)}))
        check("legacy null credential is not an external change",
              case.external() is False)

        # 但旧记录下别的字段被改，仍然要读出来。
        case.patch_record(lambda record: record.update({
            "managed_values": json.dumps(
                dict(json.loads(record["managed_values"]),
                     credential=None, patches=[]),
                sort_keys=True, ensure_ascii=False)}))
        check("legacy record still catches other edits",
              case.external() is True)

        # settings.yaml 里的 provider 表被手改。
        case.apply()
        text = read(case.settings).replace(BASE + "/v1", "http://evil/v1")
        write(case.settings, text)
        check("hand-edited settings is an external change",
              case.external() is True)

        # 用户在 DSH 里删掉了 credentials 文件。
        os.remove(case.creds)
        check("missing credentials file gives null credential",
              wb_agents._dsh_credential("") is None)
        check("missing credentials file is an external change",
              case.external() is True)

        # 重新 apply 会重建 refs 块，记录恢复一致。
        case.apply()
        values = json.loads(case.record()["dsh"]["managed_values"])
        check("re-apply records the key again", values["credential"] == KEY,
              repr(values.get("credential")))
        check("re-apply is clean", case.external() is False)
    finally:
        case.close()

    print("")
    if FAIL:
        print("FAILED: %s" % ", ".join(FAIL))
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    sys.exit(main())
