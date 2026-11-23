"""Integration tests use disposable repositories, never the market checkout."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "commit.py"


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sgit-commit-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        self.git("init", "-q")
        self.git("config", "user.name", "SGit Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.repo / ".git" / "info" / "exclude").write_text(".workbench/\n", encoding="utf-8")
        self.task = self.repo / ".workbench" / "sgit" / "task"
        self.task.mkdir(parents=True)
        self.state = self.task / "state.json"
        self.message = self.task / "message.txt"
        self.paths = self.task / "paths.json"
        self.message.write_text("fix(message): 保留中文与 shell 字符 $() ` & ;\n\n修复\n- 保留 UTF-8 文案\n\n测试\n- 覆盖特殊字符传递\n", encoding="utf-8")
        self.paths.write_text(json.dumps(["selected.txt"]), encoding="utf-8")

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.repo), *args],
                                env=self.env, capture_output=True)
        if result.returncode:
            self.fail(result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8").rstrip("\r\n")

    def cli(self, *args, success=True, input=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                cwd=self.repo, env=self.env, capture_output=True, input=input)
        output = result.stdout if success else result.stderr
        self.assertEqual(result.returncode, 0 if success else 2,
                         result.stderr.decode("utf-8", "replace"))
        return json.loads(output.decode("utf-8"))

    def baseline(self):
        (self.repo / "selected.txt").write_text("old\n", encoding="utf-8", newline="\n")
        (self.repo / "other.txt").write_text("old\n", encoding="utf-8", newline="\n")
        self.git("add", "--", "selected.txt", "other.txt")
        self.git("commit", "-qm", "baseline")

    def stage(self):
        (self.repo / "selected.txt").write_text("staged\n", encoding="utf-8", newline="\n")
        self.git("add", "--", "selected.txt")

    def prepare(self, success=True):
        return self.cli("prepare", "--repo", self.repo, "--state", self.state,
                        "--message", self.message, "--paths-file", self.paths,
                        success=success)

    def execute(self, success=True):
        return self.cli("execute", "--state", self.state, "--confirmed", success=success)

    def submit(self, *, success=True, **changes):
        payload = {"message": self.message.read_text(encoding="utf-8"),
                   "paths": ["selected.txt"], "stage_paths": ["selected.txt"]}
        changes_for_cli = changes.pop("confirmed", True)
        payload.update(changes)
        flags = ["--confirmed"] if changes_for_cli else []
        return self.cli("submit", "--repo", self.repo, *flags, success=success,
                        input=json.dumps(payload).encode("utf-8"))

    def test_direct_submit_stages_selected_files_and_leaves_other_work(self):
        self.baseline()
        before = self.git("rev-parse", "HEAD")
        (self.repo / "selected.txt").write_text("updated\n", encoding="utf-8", newline="\n")
        (self.repo / "other.txt").write_text("other task\n", encoding="utf-8")
        extra = "新增 空格 $() `.txt"
        (self.repo / extra).write_text("new\n", encoding="utf-8", newline="\n")
        result = self.submit(paths=["selected.txt", extra],
                             stage_paths=["selected.txt", extra], expected_head=before)
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(result["message"], self.message.read_text(encoding="utf-8").rstrip("\n"))
        self.assertEqual(self.git("show", "HEAD:selected.txt"), "updated")
        self.assertEqual(self.git("show", "HEAD:" + extra), "new")
        self.assertEqual(self.git("show", "HEAD:other.txt"), "old")
        self.assertEqual([item["path"] for item in result["remaining_changes"]], ["other.txt"])
        self.assertFalse(self.state.exists())

    def test_direct_submit_preserves_unstaged_part_of_selected_file(self):
        self.baseline()
        self.stage()
        (self.repo / "selected.txt").write_text("unselected edits\n", encoding="utf-8")
        self.submit(stage_paths=[])
        self.assertEqual(self.git("show", "HEAD:selected.txt"), "staged")
        self.assertEqual((self.repo / "selected.txt").read_text(), "unselected edits\n")

    def test_direct_submit_rejects_outside_index_before_staging(self):
        self.baseline()
        (self.repo / "other.txt").write_text("other task\n", encoding="utf-8", newline="\n")
        self.git("add", "--", "other.txt")
        before = self.git("write-tree")
        (self.repo / "selected.txt").write_text("not yet staged\n", encoding="utf-8")
        result = self.submit(success=False)
        self.assertIn("范围外", result["error"])
        self.assertEqual(self.git("write-tree"), before)

    def test_direct_submit_rejects_changed_head_and_unconfirmed_request(self):
        self.baseline()
        before = self.git("rev-parse", "HEAD")
        self.stage()
        self.git("commit", "-qm", "another commit")
        self.assertIn("HEAD", self.submit(expected_head=before, success=False)["error"])
        self.assertIn("确认", self.submit(confirmed=False, success=False)["error"])

    def test_direct_submit_retains_failing_hook_and_index(self):
        self.baseline()
        self.stage()
        before = self.git("rev-parse", "HEAD")
        tree = self.git("write-tree")
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        self.submit(success=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("write-tree"), tree)
        self.assertFalse(self.state.exists())

    def test_direct_initial_commit_then_empty_submit_fails(self):
        (self.repo / "selected.txt").write_text("initial\n", encoding="utf-8", newline="\n")
        result = self.submit(expected_head=None)
        self.assertEqual(result["remaining_changes"], [])
        self.assertEqual(self.git("show", "HEAD:selected.txt"), "initial")
        self.submit(success=False)
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "1")

    def test_commit_preserves_unicode_and_shell_characters(self):
        self.baseline()
        self.stage()
        self.prepare()
        result = self.execute()
        self.assertEqual(result["phase"], "committed")
        self.assertEqual(self.git("log", "-1", "--format=%B"),
                         self.message.read_text(encoding="utf-8").rstrip("\n"))
        self.assertEqual(len(result["parents"]), 1)
        self.execute(success=False)

    def test_staged_snapshot_preserves_unstaged_and_unselected_changes(self):
        self.baseline()
        self.stage()
        (self.repo / "selected.txt").write_text("unstaged\n", encoding="utf-8")
        (self.repo / "other.txt").write_text("other task\n", encoding="utf-8")
        self.prepare()
        # Further worktree edits are safe because execution commits only the index.
        (self.repo / "other.txt").write_text("newer other task\n", encoding="utf-8")
        self.execute()
        self.assertEqual(self.git("show", "HEAD:selected.txt"), "staged")
        self.assertEqual(self.git("show", "HEAD:other.txt"), "old")
        self.assertEqual((self.repo / "selected.txt").read_text(), "unstaged\n")
        self.assertEqual((self.repo / "other.txt").read_text(), "newer other task\n")

    def test_out_of_scope_index_is_rejected_without_changes(self):
        self.baseline()
        self.stage()
        (self.repo / "other.txt").write_text("unrelated\n", encoding="utf-8")
        self.git("add", "--", "other.txt")
        before = self.git("write-tree")
        self.prepare(success=False)
        self.assertEqual(self.git("write-tree"), before)
        self.assertFalse(self.state.exists())

    def test_head_change_rejected(self):
        self.baseline()
        self.stage()
        self.prepare()
        self.git("commit", "-qm", "another commit")
        self.assertIn("HEAD", self.execute(success=False)["error"])

    def test_index_change_rejected(self):
        self.baseline()
        self.stage()
        self.prepare()
        (self.repo / "selected.txt").write_text("new staged content\n", encoding="utf-8", newline="\n")
        self.git("add", "--", "selected.txt")
        self.assertIn("索引", self.execute(success=False)["error"])

    def test_message_change_rejected(self):
        self.stage()
        self.prepare()
        self.message.write_text("changed\n", encoding="utf-8")
        self.assertIn("文案", self.execute(success=False)["error"])

    def test_failing_hook_keeps_index_and_marks_attempt_failed(self):
        self.baseline()
        self.stage()
        self.prepare()
        old_head, old_tree = self.git("rev-parse", "HEAD"), self.git("write-tree")
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        self.execute(success=False)
        self.assertEqual(self.git("rev-parse", "HEAD"), old_head)
        self.assertEqual(self.git("write-tree"), old_tree)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["phase"], "failed")
        hook.unlink()
        self.execute(success=False)

    def test_initial_commit(self):
        self.stage()
        self.prepare()
        result = self.execute()
        self.assertEqual(result["parents"], [])
        self.assertEqual(self.git("show", "HEAD:selected.txt"), "staged")

    def test_empty_index_rejected(self):
        self.baseline()
        self.prepare(success=False)

    def test_tracked_artifact_rejected(self):
        self.stage()
        self.git("add", "-f", "--", str(self.message))
        self.prepare(success=False)

    def test_version_rules(self):
        cases = [("1.2.3", "1.2.3", [], "1.2.4"),
                 ("0.0.0", "0.0.0", [], "0.0.1"),
                 ("1.2.3", "1.2.3", ["--bump", "1"], "2.0.0"),
                 ("1.2.3", "1.2.3", ["--bump", "2"], "1.3.0"),
                 ("1.2.3", "1.2.3", ["--bump", "3"], "1.2.4"),
                 ("1.2.8", "1.2.3", [], "1.2.8"),
                 ("1.2.3", "1.2.3", ["--target", "2.0.0"], "2.0.0")]
        for current, committed, options, expected in cases:
            with self.subTest(current=current, options=options):
                result = self.cli("version", "--current", current, "--committed", committed, *options)
                self.assertEqual(result["target"], expected)
                if not options:
                    self.assertEqual(result["bump"], 3)
        for current, committed in [("1.2.3", "1.2.3.4"),
                                   ("1.2.3.4", "1.2.3"),
                                   ("1.2", "1.2.3"),
                                   ("01.2.3", "1.2.3"),
                                   ("1.02.3", "1.2.3"),
                                   ("1.2.03", "1.2.3"),
                                   ("1.2.-1", "1.2.3"),
                                   ("1.2.3-beta", "1.2.3"),
                                   ("1.2.2", "1.2.3")]:
            self.cli("version", "--current", current, "--committed", committed, success=False)
        for target in ("1.2.3", "1.2.2", "1.2.3.4"):
            self.cli("version", "--current", "1.2.3", "--committed", "1.2.3",
                     "--target", target, success=False)
        for bump in ("0", "4"):
            self.cli("version", "--current", "1.2.3", "--committed", "1.2.3",
                     "--bump", bump, success=False)

    def test_explicit_bump_honors_requested_segment_after_existing_edit(self):
        result = self.cli("version", "--current", "1.2.4", "--committed", "1.2.3",
                          "--bump", "2")
        self.assertEqual(result["target"], "1.3.0")
        self.assertFalse(result["reuse_current"])

    def test_explicit_bump_reuses_exact_requested_version(self):
        result = self.cli("version", "--current", "1.3.0", "--committed", "1.2.3",
                          "--bump", "2")
        self.assertEqual(result["target"], "1.3.0")
        self.assertEqual(result["action"], "reuse")

    def test_explicit_bump_preserves_higher_current_version_until_target_given(self):
        result = self.cli("version", "--current", "1.3.1", "--committed", "1.2.3",
                          "--bump", "2", success=False)
        self.assertIn("--target", result["error"])
        result = self.cli("version", "--current", "1.3.1", "--committed", "1.2.3",
                          "--target", "1.3.0")
        self.assertEqual(result["target"], "1.3.0")


if __name__ == "__main__":
    unittest.main()
