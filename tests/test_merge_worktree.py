"""Integration tests: every Git mutation stays in independent system-temp repos."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import merge_worktree as merge
from commit import version_plan
from git_common import GitError, git, head, index_tree, run, status


class MergeWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sgit-merge-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.environment = patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.target = self.root / "主工作区 main"
        self.target.mkdir()
        self.command(self.target, "init", "--initial-branch=main")
        self.command(self.target, "config", "user.name", "sgit test")
        self.command(self.target, "config", "user.email", "sgit@example.invalid")
        self.command(self.target, "config", "core.autocrlf", "false")
        self.write(self.target, ".gitignore", ".workbench/\ncache/\n")
        self.write(self.target, "base.txt", "base\n")
        self.write(self.target, "delete.txt", "delete\n")
        self.write(self.target, "rename.txt", "rename\n")
        self.command(self.target, "add", "-A")
        self.command(self.target, "commit", "-m", "base")
        self.before = head(self.target)
        self.source = self.root / "源 空格 worktree"
        self.command(self.target, "worktree", "add", "-b", "feature", self.source)
        self.state_path = self.target / ".workbench" / "sgit" / "merge.json"
        self.message_path = self.root / "approved-message.txt"
        self.message_path.write_text("整合功能\n\n1. 保存完整改动\n", encoding="utf-8")

    @staticmethod
    def command(repo, *args):
        return run(repo, *args)

    @staticmethod
    def write(repo, path, content):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    def prepare(self, **changes):
        values = {"source": str(self.source), "state": str(self.state_path),
                  "target_branch": None, "authorized": True}
        values.update(changes)
        return merge.prepare(argparse.Namespace(**values))

    def apply(self, **changes):
        values = {"state": str(self.state_path), "message": str(self.message_path),
                  "confirmed": True}
        values.update(changes)
        return merge.apply(argparse.Namespace(**values))

    def invoke(self, handler):
        return handler(argparse.Namespace(state=str(self.state_path)))

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def commit_source(self, filename="committed.txt", content="committed source\n"):
        self.write(self.source, filename, content)
        self.command(self.source, "add", "-A")
        self.command(self.source, "commit", "-m", "feature contribution")
        return head(self.source)

    def test_full_cli_preserves_all_changes_stash_and_source_history(self):
        source_head = self.commit_source()
        self.write(self.source, "base.txt", "updated unstaged\n")
        self.write(self.source, "新增 中文 空格.txt", "new file\n")
        (self.source / "delete.txt").unlink()
        (self.source / "rename.txt").rename(self.source / "重命名 文件.txt")
        self.write(self.source, "cache/asset.bin", "ignored resource\n")
        command = [sys.executable, str(SCRIPTS / "merge_worktree.py")]
        prepared = subprocess.run(command + ["prepare", "--source", str(self.source),
            "--state", str(self.state_path), "--authorized"], capture_output=True, check=False)
        self.assertEqual(prepared.returncode, 0, prepared.stdout.decode("utf-8"))
        expected_tree = index_tree(self.source)
        self.assertEqual(self.state()["ignored_resources"], ["cache/"])
        applied = subprocess.run(command + ["apply", "--state", str(self.state_path),
            "--message", str(self.message_path), "--confirmed"], capture_output=True, check=False)
        self.assertEqual(applied.returncode, 0, applied.stdout.decode("utf-8"))
        self.assertEqual(head(self.source), source_head)
        self.assertFalse(status(self.source))
        snapshot = self.state()["stash"]
        self.assertEqual(git(self.target, "rev-parse", snapshot + "^{tree}"), expected_tree)
        committed = self.invoke(merge.commit)
        self.assertEqual(committed["phase"], "committed")
        self.assertEqual(git(self.target, "show", "-s", "--format=%P", "HEAD"), self.before)
        self.assertEqual(git(self.target, "rev-parse", "HEAD^{tree}"), expected_tree)
        self.assertEqual(git(self.target, "rev-list", "--count", self.before + "..HEAD"), "1")
        self.assertEqual(head(self.source), source_head)
        self.assertEqual(git(self.target, "rev-parse", "refs/heads/feature"), source_head)
        self.assertEqual(git(self.target, "rev-parse", "refs/stash"), snapshot)
        ready = self.invoke(merge.check_cleanup)
        self.assertEqual(ready["cleanup_source"], str(self.source))
        self.assertEqual(ready["cleanup_ignored_resources"], ["cache/"])
        with self.assertRaises(GitError):
            self.invoke(merge.verify)
        # A caller has reviewed and saved ignored resources before normal removal.
        (self.source / "cache/asset.bin").unlink()
        (self.source / "cache").rmdir()
        self.command(self.target, "worktree", "remove", str(self.source))
        self.assertEqual(self.invoke(merge.verify)["phase"], "verified")
        self.assertEqual(git(self.target, "rev-parse", "refs/stash"), snapshot)

    def test_committed_source_needs_no_stash(self):
        source_head = self.commit_source()
        self.prepare()
        result = self.apply()
        self.assertIsNone(result["stash"])
        self.assertEqual(result["merge_input"], source_head)
        self.invoke(merge.commit)
        self.assertEqual(head(self.source), source_head)
        self.invoke(merge.check_cleanup)

    def merge_with_version_plan(self, source_version, target_version, expected):
        # Follow the skill's caller-managed version step between apply and commit.
        self.write(self.target, "VERSION", "1.2.3\n")
        self.command(self.target, "add", "VERSION")
        self.command(self.target, "commit", "-m", "baseline version")
        self.command(self.source, "merge", "--ff-only", "main")
        self.write(self.source, "VERSION", source_version + "\n")
        source_head = self.commit_source()
        if target_version != "1.2.3":
            self.write(self.target, "VERSION", target_version + "\n")
            self.command(self.target, "add", "VERSION")
            self.command(self.target, "commit", "-m", "independent target version")
        target_head = head(self.target)
        baseline = git(self.target, "show", target_head + ":VERSION")
        candidate = max((source_version, baseline),
                        key=lambda value: tuple(map(int, value.split("."))))
        plan = version_plan(candidate, baseline)
        message = "feat(project): 新增项目能力\n\n新功能\n- 支持新的项目行为\n\n测试\n- 覆盖新增行为"
        self.message_path.write_text(message + "\n", encoding="utf-8")
        self.prepare()
        self.assertEqual(self.apply()["phase"], "merged")
        self.assertNotEqual(index_tree(self.target),
                            git(self.target, "rev-parse", target_head + "^{tree}"))
        self.write(self.target, "VERSION", plan["target"] + "\n")
        self.command(self.target, "--literal-pathspecs", "add", "--", "VERSION")
        result = self.invoke(merge.commit)
        self.assertEqual(git(self.target, "show", "HEAD:VERSION"), expected)
        self.assertEqual(git(self.target, "log", "-1", "--format=%B"), message)
        self.assertEqual(result["result"]["parents"], [target_head])
        self.assertEqual(git(self.target, "rev-list", "--count", target_head + "..HEAD"), "1")
        self.assertEqual(head(self.source), source_head)
        self.assertEqual(git(self.source, "show", "HEAD:VERSION"), source_version)
        self.assertEqual(git(self.target, "show", "HEAD:committed.txt"), "committed source")
        self.invoke(merge.check_cleanup)

    def test_merge_includes_default_patch_bump_in_single_commit(self):
        self.merge_with_version_plan("1.2.3", "1.2.3", "1.2.4")

    def test_merge_reuses_source_version_above_target(self):
        self.merge_with_version_plan("1.2.4", "1.2.3", "1.2.4")

    def test_merge_from_older_source_bumps_target_version(self):
        self.merge_with_version_plan("1.2.3", "1.2.5", "1.2.6")

    def test_no_net_change_creates_no_empty_commit(self):
        self.prepare()
        self.apply()
        result = self.invoke(merge.commit)
        self.assertIsNone(result["result"])
        self.assertEqual(head(self.target), self.before)
        self.assertEqual(self.invoke(merge.check_cleanup)["phase"], "cleanup_ready")

    def test_conflict_can_be_resolved_then_committed_once(self):
        self.commit_source("base.txt", "source\n")
        self.write(self.target, "base.txt", "target\n")
        self.command(self.target, "add", "base.txt")
        self.command(self.target, "commit", "-m", "independent main change")
        target_head = head(self.target)
        self.prepare()
        result = self.apply()
        self.assertEqual(result["phase"], "conflicts")
        with self.assertRaises(GitError):
            self.invoke(merge.commit)
        self.write(self.target, "base.txt", "source and target\n")
        self.command(self.target, "add", "base.txt")
        result = self.invoke(merge.commit)
        self.assertEqual(result["result"]["parents"], [target_head])
        self.assertEqual((self.target / "base.txt").read_text(encoding="utf-8"), "source and target\n")

    def test_main_branch_source_is_rejected_without_writes(self):
        self.write(self.target, "base.txt", "dirty main\n")
        with self.assertRaisesRegex(GitError, "已在主分支"):
            self.prepare(source=str(self.target))
        self.assertEqual(status(self.target)[0]["y"], "M")
        self.assertFalse(self.state_path.exists())

    def test_primary_source_is_rejected(self):
        self.command(self.target, "switch", "-c", "primary-feature")
        with self.assertRaisesRegex(GitError, "普通主工作区"):
            self.prepare(source=str(self.target))
        self.assertFalse(self.state_path.exists())

    def test_dirty_target_refuses_before_staging_source(self):
        self.write(self.source, "base.txt", "unstaged source\n")
        self.write(self.target, "base.txt", "dirty target\n")
        with self.assertRaisesRegex(GitError, "目标工作区有改动"):
            self.prepare()
        self.assertEqual(status(self.source)[0]["y"], "M")
        self.assertFalse(self.state_path.exists())

    def test_target_branch_switches_only_after_validation(self):
        self.command(self.target, "switch", "-c", "other")
        prepared = self.prepare()
        self.assertEqual(prepared["target_previous_branch"], "other")
        self.assertEqual(git(self.target, "branch", "--show-current"), "main")

    def test_ambiguous_fallback_and_remote_defaults_refuse(self):
        self.command(self.target, "branch", "master")
        with self.assertRaisesRegex(GitError, "无法唯一确定"):
            self.prepare()
        self.command(self.target, "update-ref", "refs/remotes/origin/main", self.before)
        self.command(self.target, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.assertEqual(merge.main_branch(self.source, None), "main")
        self.command(self.target, "update-ref", "refs/remotes/upstream/master", self.before)
        self.command(self.target, "symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/master")
        with self.assertRaisesRegex(GitError, "多个远端默认分支"):
            self.prepare()
        self.assertEqual(merge.main_branch(self.source, "main"), "main")

    def test_state_must_be_ignored_or_external_and_outside_source(self):
        self.write(self.source, "base.txt", "unstaged\n")
        for state in (self.source / ".workbench/state.json", self.target / "state.json",
                      self.target / ".git" / "unsafe.json"):
            with self.subTest(state=state), self.assertRaises(GitError):
                self.prepare(state=str(state))
        self.assertEqual(status(self.source)[0]["y"], "M")
        external = self.root / "outside.json"
        self.assertEqual(self.prepare(state=str(external))["phase"], "prepared")
        self.assertTrue(external.exists())
        self.command(self.target, "config", "core.excludesFile", os.devnull)
        self.write(self.target, ".gitignore", "cache/\n")
        self.command(self.target, "add", ".gitignore")
        self.command(self.target, "commit", "-m", "remove workbench ignore")
        with self.assertRaisesRegex(GitError, "未被目标仓库忽略"):
            self.prepare()
        self.assertEqual(status(self.source)[0]["x"], "M")

    def test_locked_source_and_ignored_nested_repo_refuse(self):
        self.command(self.target, "worktree", "lock", self.source)
        with self.assertRaisesRegex(GitError, "locked"):
            self.prepare()
        self.command(self.target, "worktree", "unlock", self.source)
        nested = self.source / "cache"
        nested.mkdir(parents=True)
        self.command(nested, "init", "--initial-branch=main")
        with self.assertRaisesRegex(GitError, "嵌套仓库"):
            self.prepare()

    def test_deep_ignored_resources_are_reported_without_recursive_scan(self):
        nested = self.source / "cache" / "nested"
        nested.mkdir(parents=True)
        self.command(nested, "init", "--initial-branch=main")
        result = self.prepare()
        self.assertEqual(result["ignored_resources"], ["cache/"])
        self.assertIn("directory-boundaries-only", result["ignored_scan"])

    def test_message_must_be_preserved_outside_source_and_not_tracked(self):
        self.write(self.source, "new.txt", "source changes\n")
        self.prepare()
        for message in (self.source / "base.txt", self.target / "base.txt", self.state_path):
            with self.subTest(message=message), self.assertRaises(GitError):
                self.apply(message=str(message))
        self.assertEqual(self.state()["phase"], "prepared")
        self.assertIsNone(self.state()["stash"])

    def test_ignore_change_after_switch_stops_preparation(self):
        self.command(self.target, "switch", "-c", "other")
        self.write(self.target, ".gitignore", ".workbench/\ncache/\nother/\n")
        self.command(self.target, "add", ".gitignore")
        self.command(self.target, "commit", "-m", "different ignore")
        # Main already ignores .workbench, so a branch-specific artifact demonstrates
        # the post-switch ignore check without modifying any caller's ignore file.
        state = self.target / "other" / "state.json"
        with self.assertRaisesRegex(GitError, "未被目标仓库忽略"):
            self.prepare(state=str(state))
        self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["phase"], "prepare_failed")

    def test_target_change_after_stash_blocks_merge_and_preserves_snapshot(self):
        self.write(self.source, "new.txt", "snapshot me\n")
        self.prepare()
        original_run = merge.run
        def concurrent_target_write(repo, *args, **kwargs):
            result = original_run(repo, *args, **kwargs)
            if args[:2] == ("stash", "push"):
                self.write(self.target, "concurrent.txt", "another session\n")
            return result
        with patch.object(merge, "run", side_effect=concurrent_target_write):
            with self.assertRaisesRegex(GitError, "目标出现改动"):
                self.apply()
        self.assertEqual(self.state()["phase"], "snapshot_created")
        self.assertIsNotNone(self.state()["stash"])
        self.assertEqual(head(self.target), self.before)

    def test_changed_source_or_target_stops_before_stash(self):
        self.write(self.source, "base.txt", "prepared\n")
        self.prepare()
        self.write(self.source, "base.txt", "new unstaged\n")
        with self.assertRaises(GitError):
            self.apply()
        self.assertEqual(self.state()["phase"], "prepared")
        self.assertEqual(git(self.source, "rev-parse", "--verify", "refs/stash", ok=(0, 128)), "")
        self.write(self.source, "base.txt", "prepared\n")
        self.write(self.target, "new-file.txt", "concurrent target\n")
        with self.assertRaises(GitError):
            self.apply()
        self.assertEqual(self.state()["phase"], "prepared")

    def test_concurrent_source_changes_block_commit_and_cleanup(self):
        self.commit_source()
        self.prepare()
        self.apply()
        self.write(self.source, "new.txt", "other session\n")
        with self.assertRaises(GitError):
            self.invoke(merge.commit)
        (self.source / "new.txt").unlink()
        self.invoke(merge.commit)
        self.write(self.source, "new.txt", "other session\n")
        with self.assertRaises(GitError):
            self.invoke(merge.check_cleanup)

    def test_repeated_apply_and_commit_are_refused(self):
        self.commit_source()
        self.prepare()
        self.apply()
        with self.assertRaisesRegex(GitError, "不可自动重放"):
            self.apply()
        self.invoke(merge.commit)
        result_head = head(self.target)
        with self.assertRaisesRegex(GitError, "不可自动重放"):
            self.invoke(merge.commit)
        self.assertEqual(head(self.target), result_head)

    def test_non_conflict_failure_records_phase_without_rollback(self):
        self.write(self.source, "new.txt", "snapshot me\n")
        self.prepare()
        original_run = merge.run
        def fail_merge(repo, *args, **kwargs):
            if args[:2] == ("merge", "--squash"):
                raise GitError("simulated non-conflict merge failure")
            return original_run(repo, *args, **kwargs)
        with patch.object(merge, "run", side_effect=fail_merge):
            with self.assertRaisesRegex(GitError, "simulated"):
                self.apply()
        state = self.state()
        self.assertEqual(state["phase"], "merge_failed")
        self.assertIsNotNone(state["stash"])
        self.assertEqual(git(self.target, "rev-parse", "refs/stash"), state["stash"])
        self.assertFalse(status(self.source))
        self.assertEqual(head(self.target), self.before)


    def test_prepare_cleans_builds_first_and_preserves_local_data_and_tools(self):
        self.write(self.source, ".local/checks/ocean-old/bundle/game.wasm", "large build")
        self.write(self.source, ".local/checks/rules-old/results.json", "old failure")
        self.write(self.source, ".local/checks/tools/luajit.exe", "shared tool")
        self.write(self.source, ".local/saves/progress", "player data")
        self.write(self.source, ".local/signing/key.pem", "signing input")
        self.write(self.source, "tmp/old.log", "temporary")
        self.write(self.source, "new-game.txt", "real change")
        result = self.prepare(cleanup_ready=True)
        self.assertFalse((self.source / ".local/checks/ocean-old").exists())
        self.assertFalse((self.source / ".local/checks/rules-old").exists())
        self.assertFalse((self.source / "tmp").exists())
        self.assertTrue((self.source / ".local/checks/tools/luajit.exe").exists())
        self.assertEqual((self.source / ".local/saves/progress").read_text(), "player data")
        self.assertEqual((self.source / ".local/signing/key.pem").read_text(), "signing input")
        self.assertIn(".local/checks/ocean-old", result["cleanup"]["removed"])
        staged = git(self.source, "diff", "--cached", "--name-only").splitlines()
        self.assertEqual(staged, ["new-game.txt"])
        self.apply()
        self.invoke(merge.commit)
        self.assertEqual(git(self.target, "ls-tree", "-r", "--name-only", "HEAD", "--", ".local", "tmp"), "")
        self.assertEqual((self.target / "new-game.txt").read_text(), "real change")

    def test_cleanup_requires_idle_source_and_does_not_touch_other_worktree(self):
        self.write(self.source, ".local/checks/old/data", "source build")
        self.write(self.target, ".local/checks/active/data", "target build")
        with self.assertRaisesRegex(GitError, "cleanup-ready"):
            self.prepare()
        self.assertFalse(self.state_path.exists())
        self.assertTrue((self.source / ".local/checks/old/data").exists())
        self.prepare(cleanup_ready=True)
        self.assertTrue((self.target / ".local/checks/active/data").exists())

    def test_cleanup_refuses_tracked_outputs_without_staging_deletions(self):
        self.commit_source("build/unique.txt", "tracked input needs review")
        before_index = index_tree(self.source)
        with self.assertRaisesRegex(GitError, "已跟踪文件"):
            self.prepare(cleanup_ready=True)
        self.assertEqual(index_tree(self.source), before_index)
        self.assertTrue((self.source / "build/unique.txt").exists())
        self.assertFalse(self.state_path.exists())

    def test_cleanup_checks_entire_plan_for_nested_repo_before_deleting(self):
        self.write(self.source, ".local/checks/a-build/data", "must remain")
        nested = self.source / ".local/checks/z-repo"
        nested.mkdir(parents=True)
        self.command(nested, "init")
        with self.assertRaisesRegex(GitError, "嵌套仓库"):
            self.prepare(cleanup_ready=True)
        self.assertTrue((self.source / ".local/checks/a-build/data").exists())

    def test_cleanup_rejects_junction_and_protected_nested_data(self):
        self.write(self.source, ".local/checks/a-build/data", "must remain")
        nested = self.source / ".local/checks/z-build/i104save"
        nested.mkdir(parents=True)
        with self.assertRaisesRegex(GitError, "受保护输入"):
            self.prepare(cleanup_ready=True)
        self.assertTrue((self.source / ".local/checks/a-build/data").exists())
        nested.rmdir()
        from merge_artifacts import cleanup
        linked = self.source / ".local/checks/z-build"
        original = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.root if path == linked else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(GitError, "链接|逃逸"):
                cleanup(self.source, ready=True)
        self.assertTrue((self.source / ".local/checks/a-build/data").exists())

    def test_extra_cleanup_paths_are_scoped_and_excluded(self):
        self.write(self.source, "diagnostics/obsolete.dat", "temporary output")
        self.write(self.source, "keep.txt", "source")
        self.prepare(cleanup_ready=True, cleanup_path=["diagnostics"])
        self.assertFalse((self.source / "diagnostics").exists())
        self.assertEqual(git(self.source, "diff", "--cached", "--name-only"), "keep.txt")

    def test_cleanup_traversal_is_rejected_before_any_deletion(self):
        self.write(self.source, ".local/checks/a-build/data", "must remain")
        with self.assertRaisesRegex(GitError, "相对路径"):
            self.prepare(cleanup_ready=True, cleanup_path=["../"])
        self.assertTrue((self.source / ".local/checks/a-build/data").exists())

    def test_staged_and_untracked_artifacts_are_not_stashed_or_merged(self):
        self.write(self.source, ".local/session.log", "local")
        self.write(self.source, "debug.log", "log")
        self.write(self.source, "real.txt", "business")
        self.command(self.source, "add", "-A")
        self.write(self.source, "__pycache__/code.pyc", "cache")
        self.prepare(cleanup_ready=True)
        self.assertEqual(git(self.source, "diff", "--cached", "--name-only"), "real.txt")
        self.apply()
        self.invoke(merge.commit)
        self.assertEqual((self.target / "real.txt").read_text(), "business")
        self.assertFalse((self.target / ".local").exists())
        self.assertFalse((self.target / "debug.log").exists())
        snapshot = self.state()["stash"]
        self.assertEqual(git(self.source, "ls-tree", "-r", "--name-only", snapshot, "--", ".local", "debug.log"), "")
        self.assertTrue((self.source / ".local/session.log").exists())

    def test_committed_artifacts_never_conflict_and_target_values_stay_intact(self):
        for path in (".local/data.json", "cache/data.json", "debug.log"):
            self.write(self.target, path, "base")
            self.command(self.target, "add", "-f", "--", path)
        self.command(self.target, "commit", "-m", "legacy tracked outputs")
        self.command(self.source, "merge", "--ff-only", "main")
        for path in (".local/data.json", "cache/data.json", "debug.log"):
            self.write(self.source, path, "source output")
            self.command(self.source, "add", "-f", "--", path)
        self.write(self.source, "business.txt", "business source")
        self.command(self.source, "add", "business.txt")
        self.command(self.source, "commit", "-m", "business with accidental artifacts")
        source_head = head(self.source)
        for path in (".local/data.json", "cache/data.json", "debug.log"):
            self.write(self.target, path, "target output")
        self.command(self.target, "add", "-u")
        self.command(self.target, "commit", "-m", "independent target output")
        before = head(self.target)
        self.prepare()
        result = self.apply()
        self.assertEqual(result["phase"], "merged")
        self.assertNotEqual(result["merge_input"], source_head)
        self.invoke(merge.commit)
        self.assertEqual(head(self.source), source_head)
        for path in (".local/data.json", "cache/data.json", "debug.log"):
            self.assertEqual((self.target / path).read_text(), "target output")
        self.assertEqual(git(self.target, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"), "business.txt")
        self.assertEqual(git(self.target, "show", "-s", "--format=%P", "HEAD"), before)


    def test_artifact_only_source_has_no_business_commit(self):
        source_head = self.commit_source(".local/history.json", "accidental output")
        self.prepare()
        self.assertEqual(self.apply()["phase"], "merged")
        result = self.invoke(merge.commit)
        self.assertIsNone(result["result"])
        self.assertEqual(head(self.target), self.before)
        self.assertEqual(head(self.source), source_head)
        self.assertFalse((self.target / ".local").exists())

    def test_target_staged_artifact_blocks_commit(self):
        self.commit_source()
        self.prepare()
        self.apply()
        self.write(self.target, "accidental.log", "do not submit")
        self.command(self.target, "add", "-f", "accidental.log")
        with self.assertRaisesRegex(GitError, "目标索引"):
            self.invoke(merge.commit)
        self.assertEqual(head(self.target), self.before)

    def test_cleanup_failure_stops_before_staging_and_merge(self):
        self.write(self.source, ".local/checks/old/file", "busy build")
        self.write(self.source, "real.txt", "business")
        before_index = index_tree(self.source)
        with patch("merge_artifacts.shutil.rmtree", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                self.prepare(cleanup_ready=True)
        self.assertEqual(index_tree(self.source), before_index)
        self.assertEqual(head(self.target), self.before)
        self.assertFalse(self.state_path.exists())


    def test_tracked_local_changes_are_not_collected_in_snapshot(self):
        self.write(self.source, "debug.log", "base output")
        self.command(self.source, "add", "-f", "debug.log")
        self.command(self.source, "commit", "-m", "legacy log")
        self.write(self.source, "debug.log", "current local output")
        self.write(self.source, "real.txt", "business")
        self.prepare()
        self.apply()
        snapshot = self.state()["stash"]
        self.assertEqual(git(self.source, "show", snapshot + ":debug.log"), "base output")
        self.assertEqual((self.source / "debug.log").read_text(), "current local output")
        self.invoke(merge.commit)
        self.assertFalse((self.target / "debug.log").exists())

    def test_cleanup_refuses_git_metadata_even_with_uppercase_spelling(self):
        with self.assertRaisesRegex(GitError, "清理路径"):
            self.prepare(cleanup_ready=True, cleanup_path=[".GIT"])
        self.assertTrue((self.source / ".git").exists())
        self.assertEqual(head(self.source), self.before)


    def test_windows_cleanup_path_also_excludes_historical_outputs(self):
        self.commit_source("diagnostics/history.txt", "historical output")
        (self.source / "diagnostics/history.txt").unlink()
        (self.source / "diagnostics").rmdir()
        self.write(self.source, "real.txt", "business")
        prepared = self.prepare(cleanup_ready=True, cleanup_path=[".\\diagnostics\\"])
        self.assertEqual(prepared["artifact_exclusions"], ["diagnostics"])
        self.apply()
        self.invoke(merge.commit)
        self.assertFalse((self.target / "diagnostics").exists())
        self.assertEqual((self.target / "real.txt").read_text(), "business")


if __name__ == "__main__":
    unittest.main()
