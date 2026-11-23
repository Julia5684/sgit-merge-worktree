#!/usr/bin/env python3
"""Submit an approved Git commit in one call; retain legacy staged snapshots."""
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys

from git_common import (GitError, branch, commit_verified, ensure_ready,
                        git, head, index_tree, load_state, repository, run,
                        save_state, status)


KIND = "sgit-commit"
VERSION = re.compile(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}\Z")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": message}), file=sys.stderr)
        self.exit(2)


def version_plan(current: str, committed: str, bump: int | None = None,
                 target: str | None = None) -> dict:
    def parse(value: str) -> tuple[int, ...]:
        if not VERSION.fullmatch(value):
            raise GitError("版本必须是三段非负整数，且不能有前导零")
        return tuple(map(int, value.split(".")))

    now, old = parse(current), parse(committed)
    effective_bump = bump if bump is not None else 3
    if target is None and now < old:
        raise GitError("当前版本低于已提交版本，需先核实权威版本来源")
    if target is not None:
        result = parse(target)
        action = "target"
    elif bump is None and now > old:
        result, action = now, "reuse"
    else:
        parts = list(old)
        parts[effective_bump - 1] += 1
        parts[effective_bump:] = [0] * (3 - effective_bump)
        result = tuple(parts)
        if result < now:
            raise GitError("所请求的版本提升会覆盖更高的当前版本，请核实并明确指定 --target")
        action = "reuse" if result == now else "bump"
    if result <= old:
        raise GitError("目标版本必须高于已提交版本")
    return {"current": current, "committed": committed,
            "target": ".".join(map(str, result)), "bump": effective_bump,
            "action": action, "reuse_current": result == now}


def artifact(repo: Path, path: str | Path) -> Path:
    """Keep workflow files out of the commit and ordinary untracked output."""
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(repo).as_posix()
    except ValueError:
        return resolved
    if relative == ".git" or relative.startswith(".git/"):
        raise GitError("工作产物不得写入 Git 元数据目录")
    tracked = run(repo, "--literal-pathspecs", "ls-files", "-z", "--", relative).stdout
    ignored = run(repo, "check-ignore", "--quiet", "--", relative, ok=(0, 1)).returncode == 0
    if tracked or not ignored:
        raise GitError("仓库内的 state/message/paths 文件必须未跟踪且被 ignore")
    return resolved


def read_message(path: Path) -> str:
    return normalize_message(path.read_text(encoding="utf-8"))


def normalize_message(message: str) -> str:
    if not isinstance(message, str):
        raise GitError("提交文案必须是文本")
    if not message.strip() or message.startswith("\ufeff") or "\0" in message:
        raise GitError("提交文案必须是非空、无 BOM 和 NUL 的 UTF-8 文本")
    return message.rstrip("\r\n") + "\n"


def selected_paths(path: Path) -> list[str]:
    return validate_paths(json.loads(path.read_text(encoding="utf-8")))


def validate_paths(values) -> list[str]:
    if not isinstance(values, list) or not values:
        raise GitError("paths-file 必须是非空 JSON 文件路径数组")
    result = []
    for value in values:
        if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
            raise GitError("所选路径须为仓库相对文件路径，使用 / 分隔")
        path_parts = value.split("/")
        if Path(value).drive or PurePosixPath(value).is_absolute() or any(p in ("", ".", "..") for p in path_parts):
            raise GitError("所选路径不得绝对定位或包含空、.、.. 路径段")
        if path_parts[0].lower() == ".git":
            raise GitError("不能选择 Git 元数据")
        result.append(value)
    if len(set(result)) != len(result):
        raise GitError("所选路径不得重复")
    return result


def staged_paths(repo: Path) -> list[str]:
    # Disabling rename detection exposes both old and new paths for range checks.
    raw = run(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", "--").stdout
    return [value.decode("utf-8", "surrogateescape") for value in raw.split(b"\0") if value]


def submit(args: argparse.Namespace) -> dict:
    if not args.confirmed:
        raise GitError("必须在用户确认具体范围、版本和文案后传入 --confirmed")
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise GitError("标准输入须为包含 message、paths 和可选 stage_paths 的 JSON 对象")
    message = normalize_message(payload["message"])
    paths = validate_paths(payload["paths"])
    # Only explicitly selected whole files are added; mixed files keep their index.
    stage_paths = payload.get("stage_paths", [])
    if stage_paths != []:
        stage_paths = validate_paths(stage_paths)
    if set(stage_paths) - set(paths):
        raise GitError("stage_paths 必须属于已确认的 paths")
    repo = repository(args.repo)
    ensure_ready(repo)
    if "expected_head" in payload and head(repo) != payload["expected_head"]:
        raise GitError("HEAD 自文案确认前的取证后已变化，请核实新提交")
    outside = sorted(set(staged_paths(repo)) - set(paths))
    if outside:
        raise GitError("索引包含范围外路径：" + ", ".join(outside))
    if stage_paths:
        if any((repo / path).is_dir() for path in stage_paths):
            raise GitError("stage_paths 须逐文件列出，不能暂存整个目录")
        run(repo, "--literal-pathspecs", "add", "--", *stage_paths)
    # Git handles empty indexes and commit failures; hooks and signing stay enabled.
    run(repo, "commit", "--cleanup=verbatim", "-F", "-", input=message.encode("utf-8"))
    try:
        new_head, actual_message = git(repo, "log", "-1", "--format=%H%x00%B").split("\0", 1)
        remaining = status(repo)
    except (GitError, OSError, ValueError) as exc:
        raise GitError("git commit 已成功，但读取结果失败；先检查实际 HEAD，不要自动重试：" + str(exc)) from exc
    return {"phase": "committed", "commit": new_head, "message": actual_message,
            "remaining_changes": remaining}


def prepare(args: argparse.Namespace) -> dict:
    repo = repository(args.repo)
    state_path, message_path, paths_path = (artifact(repo, p) for p in
                                          (args.state, args.message, args.paths_file))
    if len({state_path, message_path, paths_path}) != 3:
        raise GitError("state、message、paths-file 必须使用不同文件")
    if state_path.exists():
        raise GitError("state 已存在，请为本次确认使用新的任务文件")
    ensure_ready(repo)
    paths = selected_paths(paths_path)
    snapshot_head, snapshot_branch = head(repo), branch(repo)
    snapshot_tree = index_tree(repo)
    changed = staged_paths(repo)
    if not changed:
        raise GitError("索引无净变化，拒绝空提交")
    outside = sorted(set(changed) - set(paths))
    if outside:
        raise GitError("索引包含范围外路径：" + ", ".join(outside))
    if (index_tree(repo) != snapshot_tree or head(repo) != snapshot_head
            or branch(repo) != snapshot_branch):
        raise GitError("准备期间 HEAD、分支或索引发生变化，请重新准备")
    state = {"schema": 1, "kind": KIND, "phase": "prepared", "repo": str(repo),
             "head": snapshot_head, "branch": snapshot_branch, "index_tree": snapshot_tree,
             "message_path": str(message_path), "message": read_message(message_path),
             "paths_file": str(paths_path), "paths": paths, "staged_paths": changed}
    save_state(state_path, state)
    return {"state": str(state_path), **state}


def execute(args: argparse.Namespace) -> dict:
    if not args.confirmed:
        raise GitError("必须在用户确认具体范围、版本和文案后传入 --confirmed")
    state_path = Path(args.state).resolve()
    state = load_state(state_path, KIND)
    if state.get("phase") != "prepared":
        raise GitError("此 state 已执行或未准备好；不得重复执行提交")
    repo = repository(state["repo"])
    artifact(repo, state_path)
    message_path = artifact(repo, state["message_path"])
    artifact(repo, state["paths_file"])
    ensure_ready(repo)
    if head(repo) != state["head"] or branch(repo) != state["branch"]:
        raise GitError("HEAD 或分支自确认快照后已变化，请重新准备并确认")
    if index_tree(repo) != state["index_tree"]:
        raise GitError("索引自确认快照后已变化，请重新准备并确认")
    if read_message(message_path) != state["message"]:
        raise GitError("提交文案自确认快照后已变化，请重新准备并确认")
    if not staged_paths(repo):
        raise GitError("索引无净变化，拒绝空提交")
    run(repo, "diff", "--cached", "--check")
    state["phase"] = "executing"
    save_state(state_path, state)
    try:
        result = commit_verified(repo, state["message"], state["head"], state["index_tree"])
        if branch(repo) != state["branch"]:
            raise GitError("提交已创建，但分支发生变化；保留现场并检查 hooks 或并发操作")
    except Exception as exc:
        state.update(phase="failed", error=str(exc))
        save_state(state_path, state)
        raise
    state.update(phase="committed", result=result)
    save_state(state_path, state)
    return {"state": str(state_path), "phase": "committed", **result}


def main() -> int:
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    version = sub.add_parser("version", help="计算三段版本，不读取或修改版本文件")
    version.add_argument("--current", required=True)
    version.add_argument("--committed", required=True)
    version.add_argument("--bump", type=int, choices=(1, 2, 3),
                         help="显式按已提交版本提升指定段；未指定时默认提升第三段或复用合规预改")
    version.add_argument("--target")
    direct = sub.add_parser("submit", help="从 UTF-8 JSON 标准输入读取方案，一次暂存、提交并返回结果")
    direct.add_argument("--repo", required=True)
    direct.add_argument("--confirmed", action="store_true")
    prep = sub.add_parser("prepare", help="捕获已暂存内容供确认；不修改索引")
    for name in ("repo", "state", "message", "paths-file"):
        prep.add_argument("--" + name, required=True)
    prep.add_argument("--staged", action="store_true", help="兼容显式标记；始终只提交索引")
    commit = sub.add_parser("execute", help="复核快照并创建一次本地提交")
    commit.add_argument("--state", required=True)
    commit.add_argument("--confirmed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "version":
            result = version_plan(args.current, args.committed, args.bump, args.target)
        elif args.command == "submit":
            result = submit(args)
        else:
            result = prepare(args) if args.command == "prepare" else execute(args)
        print(json.dumps(result))
        return 0
    except (GitError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
