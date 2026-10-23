# -*- coding: utf-8 -*-
"""新功能通知：md 解析、id 稳定、远程/本地回退与「不再显示」的持久化。

不真联网：远程源用假的 _fetch_url 替换。wb_settings 只碰一个临时目录里的
settings.json。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_notices
import wb_settings

# 本地文件/缓存用例不该联网；远程用例自己打开。
os.environ["WB_NOTICES_REMOTE"] = "0"


SAMPLE = """# 新功能通知

<!-- 这段说明不该出现在任何一条通知里 -->

## 2026-10-23 通知中心上线
id: notices-center
level: feature
title_en: In-app notices
body_en: |
  A bell icon now collects release notes.

  - Notice content lives in `docs/notices.md`
  - Dismissals survive a browser change

右上角新增通知入口。

- 支持 `feature` / `fix`
- 「不再显示」按 id 记住

## 2026-10-22 修复登录
level: 修复

登录页不再卡住。
"""


class ParseNoticesTest(unittest.TestCase):
    def test_basic_fields(self):
        notices = wb_notices.parse_notices(SAMPLE)
        self.assertEqual(2, len(notices))
        newest = notices[0]
        self.assertEqual("notices-center", newest["id"])
        self.assertEqual("2026-10-23", newest["date"])
        self.assertEqual("feature", newest["level"])
        self.assertEqual("通知中心上线", newest["title"])
        self.assertIn("右上角新增通知入口。", newest["body"])
        self.assertIn("- 支持 `feature` / `fix`", newest["body"])
        self.assertEqual("In-app notices", newest["title_en"])
        self.assertIn("A bell icon", newest["body_en"])
        self.assertNotIn("这段说明", newest["body"])

    def test_heading_date_and_level_alias(self):
        notices = wb_notices.parse_notices(SAMPLE)
        older = notices[1]
        self.assertEqual("2026-10-22", older["date"])
        self.assertEqual("fix", older["level"])
        self.assertEqual("修复登录", older["title"])

    def test_missing_id_is_stable_hash(self):
        one = wb_notices.parse_notices("## 2026-10-23 同一条\n\n正文\n")
        two = wb_notices.parse_notices("## 2026-10-23 同一条\n\n正文改过\n")
        self.assertEqual(one[0]["id"], two[0]["id"])
        self.assertTrue(one[0]["id"].startswith("n-"))

    def test_duplicate_id_gets_suffix(self):
        text = ("## 2026-10-23 第一条\nid: same\n\nA\n\n"
                "## 2026-10-22 第二条\nid: same\n\nB\n")
        notices = wb_notices.parse_notices(text)
        ids = [item["id"] for item in notices]
        self.assertEqual(["same", "same-2"], ids)

    def test_sorted_newest_first(self):
        notices = wb_notices.parse_notices(SAMPLE)
        self.assertEqual(["2026-10-23", "2026-10-22"],
                         [item["date"] for item in notices])

    def test_same_date_appended_last_is_shown_first(self):
        text = ("## 2026-10-23 早写的\nid: early\n\nA\n\n"
                "## 2026-10-23 后写的\nid: late\n\nB\n")
        notices = wb_notices.parse_notices(text)
        self.assertEqual(["late", "early"], [item["id"] for item in notices])

    def test_empty_file(self):
        self.assertEqual([], wb_notices.parse_notices(""))
        self.assertEqual([], wb_notices.parse_notices("# 只有标题\n"))

    def test_body_is_capped(self):
        huge = "x" * (wb_notices.MAX_BODY_CHARS + 500)
        notices = wb_notices.parse_notices("## 2026-10-23 长文\n\n" + huge)
        self.assertLessEqual(len(notices[0]["body"]), wb_notices.MAX_BODY_CHARS + 2)


class LoadNoticesTest(unittest.TestCase):
    def test_missing_file_reads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], wb_notices.load_notices(os.path.join(tmp, "none.md")))

    def test_cache_refreshes_when_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notices.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("## 2026-10-23 第一条\nid: one\n\nA\n")
            first = wb_notices.load_notices(path)
            self.assertEqual(["one"], [item["id"] for item in first])
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("## 2026-10-23 第一条\nid: one\n\nA\n\n"
                         "## 2026-10-23 第二条\nid: two\n\nB\n")
            second = wb_notices.load_notices(path)
            self.assertEqual(2, len(second))

    def test_returned_list_is_a_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notices.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("## 2026-10-23 第一条\nid: one\n\nA\n")
            first = wb_notices.load_notices(path)
            first[0]["title"] = "被改过"
            second = wb_notices.load_notices(path)
            self.assertEqual("第一条", second[0]["title"])


class RemoteNoticesTest(unittest.TestCase):
    REMOTE = ("## 2026-10-23 远程更新\nid: remote-one\n\n"
              "来自 GitHub 的通知\n")
    LOCAL = "## 2026-10-23 本地副本\nid: local-one\n\n离线时的兜底\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "notices.md")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(self.LOCAL)
        self._env = {name: os.environ.get(name) for name in
                     ("WB_NOTICES_REMOTE", "WB_NOTICES_URL",
                      "WB_NOTICES_TTL", "WB_NOTICES_TIMEOUT")}
        self.addCleanup(self._restore)
        os.environ["WB_NOTICES_REMOTE"] = "1"
        os.environ["WB_NOTICES_URL"] = "https://example.test/notices.md"
        os.environ["WB_NOTICES_TTL"] = "60"
        os.environ.pop("WB_NOTICES_TIMEOUT", None)
        self._fetch = wb_notices._fetch_url
        self.addCleanup(setattr, wb_notices, "_fetch_url", self._fetch)
        wb_notices._reset_caches()

    def _restore(self):
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        wb_notices._reset_caches()

    def _serve(self, text, calls=None):
        calls = [] if calls is None else calls

        def fetch(url, timeout):
            calls.append(url)
            if isinstance(text, Exception):
                raise text
            return text

        wb_notices._fetch_url = fetch
        return calls

    def test_remote_wins_over_local(self):
        calls = self._serve(self.REMOTE)
        state = wb_notices.load_notices_with_meta(self.path, force=True)
        self.assertTrue(state["remote"])
        self.assertEqual(["remote-one"], [item["id"] for item in state["notices"]])
        self.assertIn("example.test", state["source"])
        self.assertEqual(1, len(calls))
        self.assertIn("t=", calls[0])

    def test_cache_and_force(self):
        calls = self._serve(self.REMOTE)
        first = wb_notices.load_notices_with_meta(self.path, force=True)
        second = wb_notices.load_notices_with_meta(self.path)
        self.assertEqual(["remote-one"], [item["id"] for item in first["notices"]])
        self.assertEqual(["remote-one"], [item["id"] for item in second["notices"]])
        self.assertEqual(1, len(calls), "TTL 内不该再抓一次")
        next_text = (self.REMOTE.replace("remote-one", "remote-two")
                     .replace("远程更新", "远程更新 v2"))
        self._serve(next_text, calls)
        third = wb_notices.load_notices_with_meta(self.path, force=True)
        self.assertEqual(["remote-two"], [item["id"] for item in third["notices"]])
        self.assertEqual(2, len(calls), "force 必须绕过缓存")

    def test_network_failure_falls_back_to_local(self):
        self._serve(OSError("offline"))
        state = wb_notices.load_notices_with_meta(self.path, force=True)
        self.assertFalse(state["remote"])
        self.assertEqual(["local-one"], [item["id"] for item in state["notices"]])
        self.assertNotIn("example.test", state["source"])

    def test_disabled_reads_local_only(self):
        calls = self._serve(self.REMOTE)
        os.environ["WB_NOTICES_REMOTE"] = "0"
        state = wb_notices.load_notices_with_meta(self.path, force=True)
        self.assertFalse(state["remote"])
        self.assertEqual(["local-one"], [item["id"] for item in state["notices"]])
        self.assertEqual([], calls)


class DismissedNoticesTest(unittest.TestCase):
    def test_dismiss_union(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], wb_settings.dismissed_notices(tmp))
            self.assertEqual(["a"], wb_settings.dismiss_notices(tmp, ["a"]))
            self.assertEqual(["a", "b"], wb_settings.dismiss_notices(tmp, ["b", "a"]))
            self.assertEqual(["a", "b"], wb_settings.dismissed_notices(tmp))

    def test_invalid_ids_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            stored = wb_settings.dismiss_notices(
                tmp, ["ok-1", "", "has space", "../etc", None, {"a": 1},
                      "ok-1", "OK-2"])
            self.assertEqual(["ok-1", "ok-2"], stored)

    def test_list_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            ids = ["n-%04d" % index for index in range(600)]
            stored = wb_settings.dismiss_notices(tmp, ids)
            self.assertEqual(wb_settings.MAX_DISMISSED_NOTICES, len(stored))


if __name__ == "__main__":
    unittest.main(verbosity=2)
