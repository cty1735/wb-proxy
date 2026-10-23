# -*- coding: utf-8 -*-
"""新功能通知：md 解析、id 稳定、文件缓存与「不再显示」的持久化。

不联网、不起服务：wb_notices 是纯函数 + 文件缓存，wb_settings 只碰一个
临时目录里的 settings.json。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_notices
import wb_settings


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
