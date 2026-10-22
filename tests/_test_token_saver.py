# -*- coding: utf-8 -*-
"""省 token：提示词压缩、RTK 工具输出压缩、本地应答、节省统计。

不联网、不起服务：直接测 wb_tokensaver 的纯函数，再经 wb_proxy 真正的
build_upstream_body() 走一遍出站路径，确认这三块确实挂在请求链路上。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wb_settings
import wb_tokensaver
import wb_proxy


def _git_log(commits=3):
    out = []
    for index in range(commits):
        out += [
            "commit %040x" % (index + 1),
            "Author: Someone <someone@example.com>",
            "Date:   Mon Oct 20 10:0%d:00 2026 +0800" % index,
            "",
            "    fix(scope): commit subject %d" % index,
            "    " + "long commit body explaining the change in detail " * 6,
            "",
        ]
    return "\n".join(out)


def _git_diff(pairs=12):
    lines = ["diff --git a/wb_proxy.py b/wb_proxy.py",
             "index 1111111..2222222 100644",
             "--- a/wb_proxy.py",
             "+++ b/wb_proxy.py",
             "@@ -1,12 +1,12 @@"]
    for index in range(pairs):
        lines.append(" context line %d of a big file nobody needs verbatim" % index)
        lines.append("+added line %d" % index)
    return "\n".join(lines) + "\n"


def _grep(matches=8):
    lines = []
    for name in ("src/a.js", "src/b.js"):
        for index in range(matches):
            lines.append("%s:%d:const value%d = handler(%d);"
                         % (name, 10 + index, index, index))
    return "\n".join(lines) + "\n"


def _find():
    lines = []
    for folder in ("src/components", "src/pages"):
        for index in range(12):
            lines.append("./%s/file%d.tsx" % (folder, index))
    return "\n".join(lines) + "\n"


def _numbered(lines=260):
    return "\n".join("%4d|%s" % (index, ("def f%d(): return %d" % (index, index)) * 3)
                     for index in range(lines)) + "\n"


class RtkFilterTest(unittest.TestCase):
    def test_git_log_folds_to_hash_and_subject(self):
        text = _git_log()
        new, kind = wb_tokensaver.compress_text(text)
        self.assertEqual(kind, "git-log")
        self.assertIsNotNone(new)
        self.assertIn("fix(scope): commit subject 0", new)
        self.assertNotIn("Author:", new)
        self.assertLess(len(new.encode()), len(text.encode()))

    def test_git_diff_drops_context_keeps_changes(self):
        text = _git_diff()
        new, kind = wb_tokensaver.compress_text(text)
        self.assertEqual(kind, "git-diff")
        self.assertIsNotNone(new)
        self.assertIn("+added line 0", new)
        self.assertIn("@@ -1,12 +1,12 @@", new)
        self.assertNotIn("context line 0", new)

    def test_git_status_keeps_sections_and_marks(self):
        text = "\n".join([
            "On branch main",
            "Your branch is up to date with 'origin/main'.",
            "",
            "Changes not staged for commit:",
            '  (use "git add <file>..." to update what will be committed)',
            "",
            "\tmodified:   wb_proxy.py",
            "",
            "Untracked files:",
            '  (use "git add <file>..." to include in what will be committed)',
            "",
            "\tnotes.txt",
            "",
            'no changes added to commit (use "git add" and/or "git commit -a")',
        ]) + "\n"
        self.assertEqual(wb_tokensaver.classify(text), "git-status")
        new = wb_tokensaver._filter_git_status(text)
        self.assertIn("Changes not staged for commit", new)
        self.assertIn("M modified:   wb_proxy.py", new)
        self.assertIn("?? notes.txt", new)
        self.assertNotIn('use "git add', new)

    def test_build_output_drops_progress_and_repeats(self):
        text = ["added 231 packages in 12s"]
        text += ["Downloading https://registry.npmjs.org/pkg/pkg-1.0.tgz (42%%)"] * 20
        text += ["npm warn deprecated inflight@1.0.6: not supported"] * 10
        text += ["> task :compile%d" % index for index in range(10)]
        blob = "\n".join(text) + "\n"
        new, kind = wb_tokensaver.compress_text(blob)
        self.assertEqual(kind, "build-output")
        self.assertIsNotNone(new)
        self.assertNotIn("Downloading", new)
        self.assertEqual(new.count("npm warn deprecated"), 1)

    def test_grep_folds_per_file(self):
        text = _grep()
        new, kind = wb_tokensaver.compress_text(text)
        self.assertEqual(kind, "grep")
        self.assertIsNotNone(new)
        self.assertIn("src/a.js: 8 matches", new)
        self.assertIn("+5 more", new)

    def test_find_groups_by_directory(self):
        text = _find()
        new, kind = wb_tokensaver.compress_text(text)
        self.assertEqual(kind, "find")
        self.assertIsNotNone(new)
        self.assertIn("src/components/ (12)", new)
        self.assertIn("+4 more", new)

    def test_numbered_dump_trims_head_and_tail(self):
        text = _numbered()
        new, kind = wb_tokensaver.compress_text(text)
        self.assertEqual(kind, "read-numbered")
        self.assertIsNotNone(new)
        self.assertIn("lines omitted", new)
        self.assertIn("def f0()", new)
        self.assertIn("def f259()", new)

    def test_repeated_log_lines_collapse(self):
        text = "\n".join(["INFO start"] + ["waiting for lock"] * 30 + ["INFO done"]) + "\n"
        new, kind = wb_tokensaver.compress_text(text)
        self.assertEqual(kind, "dedup-log")
        self.assertIsNotNone(new)
        self.assertIn("[x30]", new)

    def test_safety_floors(self):
        # 小于 500 字节不碰。
        self.assertEqual(wb_tokensaver.compress_text("x" * 120), (None, ""))
        # 不认识的形状不碰：一行长文本，没有可识别的结构。
        self.assertEqual(wb_tokensaver.compress_text("A" * 800), (None, ""))
        # 报错类结果不碰。
        error = "Error: something failed\n" + "stack line\n" * 300
        self.assertTrue(wb_tokensaver.looks_like_error(error))
        messages = [{"role": "tool", "content": error}]
        out, info = wb_tokensaver.compress_messages(messages)
        self.assertIs(out, messages)
        self.assertEqual(info["blocks"], 0)
        # 过滤器返回不更小的结果 -> compress_text 回退原文。
        tiny = "commit abc\n\ntiny"
        self.assertEqual(wb_tokensaver._filter_git_log(tiny), tiny)
        self.assertIsNone(wb_tokensaver.compress_text(tiny)[0])

    def test_never_larger_never_empty(self):
        samples = [_git_log(), _git_diff(), _grep(), _find(), _numbered(),
                   "\n".join(["a"] * 400)]
        for text in samples:
            new, _kind = wb_tokensaver.compress_text(text)
            if new is None:
                continue
            self.assertTrue(new.strip())
            self.assertLess(len(new.encode()), len(text.encode()))


class MessagePassTest(unittest.TestCase):
    def test_only_tool_messages_change(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": _git_log()},
            {"role": "assistant", "content": "ok"},
            {"role": "tool", "content": _git_log()},
        ]
        out, info = wb_tokensaver.compress_messages(messages)
        self.assertIsInstance(out, list)
        self.assertEqual(out[1]["content"], messages[1]["content"])
        self.assertNotEqual(out[3]["content"], messages[3]["content"])
        self.assertEqual(info["blocks"], 1)
        # 原列表与原始 dict 都没被改。
        self.assertEqual(messages[3]["content"], _git_log())

    def test_list_content_is_rewritten_as_text_block(self):
        messages = [{"role": "tool", "content": [
            {"type": "text", "text": _git_log()}]}]
        out, info = wb_tokensaver.compress_messages(messages)
        self.assertEqual(info["blocks"], 1)
        self.assertEqual(typeof(out[0]["content"]), "list")
        self.assertIsInstance(out[0]["content"][0]["text"], str)

    def test_prompt_injection_is_idempotent(self):
        cfg = wb_tokensaver.normalize_config(
            {"prompt": {"caveman": "ultra", "ponytail": "full"}})
        messages = [{"role": "system", "content": "You are helpful."},
                    {"role": "user", "content": "hi"}]
        out, injected = wb_tokensaver.apply_prompt(messages, cfg)
        self.assertEqual(injected, 1)
        self.assertIn(wb_tokensaver.MARKER, out[0]["content"])
        self.assertIn(wb_tokensaver.SAFETY_LINE, out[0]["content"])
        again, injected_again = wb_tokensaver.apply_prompt(out, cfg)
        self.assertEqual(injected_again, 0)
        self.assertEqual(again[0]["content"], out[0]["content"])

    def test_no_system_message_means_no_injection(self):
        cfg = wb_tokensaver.normalize_config({"prompt": {"caveman": "full"}})
        messages = [{"role": "user", "content": "hi"}]
        out, injected = wb_tokensaver.apply_prompt(messages, cfg)
        self.assertEqual(injected, 0)
        self.assertIs(out, messages)


class ConfigAndStatsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tokensaver-")

    def test_defaults(self):
        cfg = wb_tokensaver.config(self.dir)
        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["rtk"])
        self.assertEqual(cfg["prompt"]["caveman"], "off")
        self.assertEqual(cfg["prompt"]["ponytail"], "off")
        self.assertFalse(cfg["local"]["enabled"])
        for kind in wb_tokensaver.LOCAL_KINDS:
            self.assertTrue(cfg["local"][kind])

    def test_patch_roundtrip_and_normalisation(self):
        cfg = wb_tokensaver.set_config(self.dir, {
            "prompt": {"caveman": "wenyan-ultra", "ponytail": "ultra"},
            "local": {"enabled": True},
        })
        self.assertEqual(cfg["prompt"]["caveman"], "wenyan-ultra")
        self.assertTrue(cfg["local"]["enabled"])
        with open(os.path.join(self.dir, "settings.json"), encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["token_saver"]["prompt"]["ponytail"], "ultra")
        # 手改坏的档位落回 off，未知键被丢掉。
        cfg = wb_tokensaver.set_config(self.dir, {"prompt": {"caveman": "nonsense",
                                                            "bogus": 1}})
        self.assertEqual(cfg["prompt"]["caveman"], "off")
        self.assertNotIn("bogus", cfg["prompt"])

    def test_string_flags_are_read_not_trusted(self):
        # 手改 settings.json 写成字符串时，"false" 不能被当成真值。
        self.assertFalse(wb_tokensaver.normalize_config({"enabled": "false"})["enabled"])
        self.assertTrue(wb_tokensaver.normalize_config({"enabled": "on"})["enabled"])
        self.assertFalse(wb_tokensaver.normalize_config({"rtk": 0})["rtk"])
        self.assertTrue(wb_tokensaver.normalize_config({"local": {"enabled": "yes"}})
                        ["local"]["enabled"])
        self.assertFalse(wb_tokensaver.normalize_config({"local": {"enabled": "no"}})
                         ["local"]["enabled"])
        # 认不出来的值按默认（总开关默认开），不静默打开本地应答。
        self.assertTrue(wb_tokensaver.normalize_config({"enabled": "maybe"})["enabled"])
        self.assertFalse(wb_tokensaver.normalize_config({"local": {"enabled": "maybe"}})
                         ["local"]["enabled"])

    def test_stats_accumulate_and_derive(self):
        wb_tokensaver._note(self.dir, calls=2, before=1000, after=400, blocks=2,
                            injections=1)
        wb_tokensaver._note(self.dir, calls=1, before=500, after=250, blocks=1)
        wb_tokensaver._note(self.dir, local_kind="quota")
        stats = wb_tokensaver.stats(self.dir)
        self.assertEqual(stats["calls"], 3)
        self.assertEqual(stats["before"], 1500)
        self.assertEqual(stats["after"], 650)
        self.assertEqual(stats["saved"], 850)
        self.assertEqual(stats["blocks"], 3)
        self.assertEqual(stats["injections"], 1)
        self.assertEqual(stats["local"]["quota"], 1)
        self.assertAlmostEqual(stats["percent"], 850 * 100.0 / 1500, places=4)

    def test_reset(self):
        wb_tokensaver._note(self.dir, calls=1, before=10, after=5, blocks=1)
        wb_tokensaver.reset_stats(self.dir)
        stats = wb_tokensaver.stats(self.dir)
        self.assertEqual(stats["calls"], 0)
        self.assertEqual(stats["saved"], 0)


class LocalAnswerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tokensaver-local-")
        wb_tokensaver.set_config(self.dir, {"local": {"enabled": True}})
        wb_tokensaver.begin_request({})

    def tearDown(self):
        wb_tokensaver.end_request()

    def answer(self, payload):
        return wb_tokensaver.local_answer(payload, self.dir)

    def test_quota_probe(self):
        for text in ("额度检查", "查额度", "quota", "/quota", "quota check"):
            got = self.answer({"messages": [{"role": "user", "content": text}]})
            self.assertEqual(got["kind"], "quota", text)
            self.assertEqual(got["text"], wb_tokensaver.QUOTA_ANSWER)

    def test_command_prefix_from_fence(self):
        payload = {"messages": [{"role": "user", "content":
                   "What command prefix does this need?\n```sh\nnpm test -- --watch\n```"}]}
        got = self.answer(payload)
        self.assertEqual(got, {"kind": "command_prefix", "text": "npm test"})

    def test_session_title_from_metadata(self):
        payload = {"client_metadata": {"request_kind": "title"},
                   "messages": [{"role": "user", "content": "Name this chat"}]}
        self.assertEqual(self.answer(payload)["text"], wb_tokensaver.TITLE_ANSWER)

    def test_suggestion_from_metadata(self):
        payload = {"client_metadata": {"turn_trigger": "suggestion"},
                   "messages": [{"role": "user", "content": "next?"}]}
        self.assertEqual(self.answer(payload)["kind"], "suggestion")

    def test_file_paths_are_structured(self):
        payload = {"messages": [{"role": "user", "content":
                   "extract file paths from: cat src/app/main.py and ./tests/_test_a.py"}]}
        got = self.answer(payload)
        self.assertEqual(got["kind"], "file_path")
        self.assertEqual(json.loads(got["text"]), ["src/app/main.py", "./tests/_test_a.py"])

    def test_refuses_real_conversations(self):
        cases = [
            # 对话已经进行到一半。
            {"messages": [{"role": "user", "content": "quota"},
                          {"role": "assistant", "content": "ok"},
                          {"role": "user", "content": "quota"}]},
            # 带工具：这一轮要干活。
            {"tools": [{"type": "function", "function": {"name": "x"}}],
             "messages": [{"role": "user", "content": "quota"}]},
            # 一条真问题。
            {"messages": [{"role": "user", "content": "quota 是什么？顺便看下 quota.py"}]},
        ]
        for payload in cases:
            self.assertIsNone(self.answer(payload), payload)

    def test_master_switch_and_per_kind(self):
        wb_tokensaver.set_config(self.dir, {"local": {"enabled": False}})
        self.assertIsNone(self.answer({"messages": [{"role": "user", "content": "额度"}]}))
        wb_tokensaver.set_config(self.dir, {"local": {"enabled": True, "quota": False}})
        self.assertIsNone(self.answer({"messages": [{"role": "user", "content": "额度"}]}))

    def test_header_escape_hatch(self):
        wb_tokensaver.begin_request({"X-WB-Token-Saver": "off"})
        self.assertIsNone(self.answer({"messages": [{"role": "user", "content": "额度"}]}))
        wb_tokensaver.begin_request({})


class UpstreamBodyWiringTest(unittest.TestCase):
    """The module is only useful if build_upstream_body() actually calls it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tokensaver-body-")
        self._saved_dir = wb_proxy.ACCOUNTS_DIR
        wb_proxy.ACCOUNTS_DIR = self.dir
        wb_settings.save(self.dir, {"token_saver": {
            "enabled": True,
            "prompt": {"caveman": "full"},
            "rtk": True,
            "local": {"enabled": False},
        }})
        wb_tokensaver.begin_request({})

    def tearDown(self):
        wb_tokensaver.end_request()
        wb_proxy.ACCOUNTS_DIR = self._saved_dir

    def _payload(self):
        return {
            "model": "gpt-test-model",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "tool", "content": _git_log()},
                {"role": "user", "content": "summarise the log"},
            ],
        }

    def test_compression_and_injection_reach_the_upstream_body(self):
        body = wb_proxy.build_upstream_body(self._payload())
        system = body["messages"][0]
        self.assertEqual(system["role"], "system")
        self.assertIn(wb_tokensaver.MARKER, system["content"])
        tool = [m for m in body["messages"] if m["role"] == "tool"][0]
        self.assertLess(len(tool["content"]), len(_git_log()))
        stats = wb_tokensaver.stats(self.dir)
        self.assertGreaterEqual(stats["blocks"], 1)
        self.assertGreater(stats["saved"], 0)
        self.assertEqual(stats["injections"], 1)

    def test_master_switch_stops_everything(self):
        with wb_settings._lock:
            data = wb_settings.load(self.dir)
            data["token_saver"]["enabled"] = False
            wb_settings.save(self.dir, data)
        body = wb_proxy.build_upstream_body(self._payload())
        self.assertNotIn(wb_tokensaver.MARKER, body["messages"][0]["content"])
        tool = [m for m in body["messages"] if m["role"] == "tool"][0]
        self.assertEqual(tool["content"], _git_log())

    def test_header_off_skips_the_pass(self):
        wb_tokensaver.begin_request({"X-WB-Token-Saver": "off"})
        body = wb_proxy.build_upstream_body(self._payload())
        self.assertNotIn(wb_tokensaver.MARKER, body["messages"][0]["content"])
        tool = [m for m in body["messages"] if m["role"] == "tool"][0]
        self.assertEqual(tool["content"], _git_log())
        self.assertEqual(wb_tokensaver.stats(self.dir)["calls"], 0)


def typeof(value):
    return type(value).__name__


if __name__ == "__main__":
    unittest.main()
