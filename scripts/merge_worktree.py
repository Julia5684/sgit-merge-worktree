#!/usr/bin/env python3
"""Durable, local-only preparation and squash integration of a linked worktree."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import sys
import uuid
from merge_artifacts import (
    business_pathspecs, changes, cleanup, excluded, filtered_input,
    require_no_staged_outputs, stage_source,
)

from git_common import (
    GitError, branch, commit_verified, ensure_ready, git, head, index_tree,
    load_state, repository, run, save_state, status,
)

KIND = "merge-worktree"


def worktrees(repo: Path) -> list[dict]:
    """Parse Git's NUL format without breaking spaces, newlines or non-ASCII paths."""
    records: list[dict] = []
    record: dict = {}
    for token in run(repo, "worktree", "list", "--porcelain", "-z").stdout.split(b"\0"):
        if not token:
            if record:
                records.append(record)
                record = {}
            continue
        key, _, value = os.fsdecode(token).partition(" ")
        if key == "worktree":
            record = {"path": str(Path(value).resolve()), "head": None,
                      "branch": None, "bare": False, "locked": False,
                      "prunable": False}
        elif key == "HEAD":
            record["head"] = value
        elif key == "branch":
            record["branch"] = value.removeprefix("refs/heads/")
        elif key in ("bare", "locked", "prunable"):
            record[key] = True
            if value:
                record[key + "_reason"] = value
    if record:
        records.append(record)
    return records


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GitError(message)


def phase(state_path: Path, state: dict, name: str, **values) -> None:
    state.update(values)
    state["phase"] = name
    save_state(state_path, state)


def local_branch(repo: Path, name: str) -> bool:
    return run(repo, "show-ref", "--verify", "--quiet", "refs/heads/" + name,
               ok=(0, 1)).returncode == 0


def main_branch(repo: Path, explicit: str | None) -> str:
    if explicit:
        require(local_branch(repo, explicit), "目标必须是已存在的本地分支")
        return explicit
    defaults = set()
    refs = git(repo, "for-each-ref", "--format=%(refname)%09%(symref)", "refs/remotes/")
    for line in refs.splitlines():
        ref, _, symbolic = line.partition("\t")
        if ref.endswith("/HEAD") and symbolic:
            prefix = ref.removesuffix("HEAD")
            require(symbolic.startswith(prefix), "远端默认分支引用不明确")
            defaults.add(symbolic[len(prefix):])
    require(len(defaults) <= 1, "多个远端默认分支相互矛盾，请明确 --target-branch")
    if defaults:
        chosen = defaults.pop()
        require(local_branch(repo, chosen), "远端默认分支没有对应本地分支")
        return chosen
    candidates = [name for name in ("main", "master") if local_branch(repo, name)]
    require(len(candidates) == 1, "无法唯一确定 main/master，请明确 --target-branch")
    return candidates[0]


def reject_embedded(repo: Path, *, untracked: bool = True) -> None:
    entries = run(repo, "ls-files", "--stage", "-z").stdout.split(b"\0")
    require(not any(entry.startswith(b"160000 ") for entry in entries),
            "存在子模块，保留 worktree 并人工处理")
    candidates = set()
    for entry in entries:
        if entry:
            relative = Path(os.fsdecode(entry.partition(b"\t")[2]))
            candidates.update(relative.parents)
    if untracked:
        # Git reports directory boundaries; never walk node_modules or ignored caches.
        paths = run(repo, "ls-files", "--others", "--exclude-standard", "--directory", "-z").stdout
        paths += run(repo, "ls-files", "--others", "--ignored", "--exclude-standard",
                     "--directory", "-z").stdout
        for item in paths.split(b"\0"):
            if item:
                relative = Path(os.fsdecode(item).rstrip("/"))
                candidates.add(relative)
                candidates.update(relative.parents)
    for relative in candidates:
        if relative != Path(".") and (repo / relative / ".git").exists():
            raise GitError("存在嵌套仓库：" + str(repo / relative))


def ignored(repo: Path) -> list[str]:
    return [os.fsdecode(path) for path in
            run(repo, "ls-files", "--others", "--ignored", "--exclude-standard",
                "--directory", "-z").stdout.split(b"\0") if path]


def check_artifact_location(path: Path, source: Path, target: Path) -> None:
    require(not path.is_relative_to(source), "工作产物不能放在待移除的源 worktree 内")
    require(".git" not in path.parts, "工作产物不能写入 Git 元数据目录")
    common = Path(git(target, "rev-parse", "--git-common-dir"))
    common = common.resolve() if common.is_absolute() else (target / common).resolve()
    require(not path.is_relative_to(common), "工作产物不能写入 Git 元数据目录")
    if path.is_relative_to(target):
        relative = path.relative_to(target).as_posix()
        require(run(target, "ls-files", "--error-unmatch", "--", ":(literal)" + relative,
                    ok=(0, 1)).returncode != 0, "工作产物不能覆盖已跟踪路径（包括已删除文件）")
        require(run(target, "check-ignore", "--quiet", "--no-index", "--", relative,
                    ok=(0, 1)).returncode == 0,
                "工作产物未被目标仓库忽略；请使用 ignored .workbench/ 或两工作区外的显式路径，脚本不会修改 ignore")


def check_state_location(state_path: Path, source: Path, target: Path, *, new: bool = True) -> None:
    if new:
        require(not state_path.exists(), "状态文件已存在，请恢复原任务或使用新的状态文件")
    check_artifact_location(state_path, source, target)


def read_state(state_path: Path) -> dict:
    state = load_state(state_path, KIND)
    check_state_location(state_path, Path(state["source"]), Path(state["target"]), new=False)
    return state


def prepare(args: argparse.Namespace) -> dict:
    require(args.authorized, "prepare 需要 --authorized，调用方须先确认已有执行授权")
    source = repository(Path(args.source))
    state_path = Path(args.state).resolve()
    entries = worktrees(source)
    require(bool(entries), "无法定位登记的 worktree")
    source_entry = next((item for item in entries if Path(item["path"]) == source), None)
    require(source_entry is not None, "源不是已登记的 worktree")
    chosen = main_branch(source, args.target_branch)
    require(branch(source) != chosen,
            f"当前 worktree 已在主分支 {chosen}，无需合并，任务已停止。")
    require(source != Path(entries[0]["path"]), "源是普通主工作区，不能按附加 worktree 收尾")
    require(not any(source_entry[key] for key in ("bare", "locked", "prunable")),
            "源 worktree 为 bare、locked 或 prunable，任务已停止")
    require(head(source) is not None, "源 worktree 尚无提交")
    matches = [item for item in entries if item["branch"] == chosen]
    require(len(matches) <= 1, "主分支被多个 worktree 检出，无法确定目标")
    target_entry = matches[0] if matches else entries[0]
    require(not any(target_entry[key] for key in ("bare", "locked", "prunable")),
            "目标 worktree 为 bare、locked 或 prunable")
    target = repository(Path(target_entry["path"]))
    require(target != source, "源和目标 worktree 不可相同")
    ensure_ready(source)
    ensure_ready(target)
    require(not changes(target), "目标工作区有改动，不能暂存源或切换目标")
    require_no_staged_outputs(target)
    reject_embedded(source)
    reject_embedded(target, untracked=False)
    check_state_location(state_path, source, target)
    if state_path.is_relative_to(target):
        relative = state_path.relative_to(target).as_posix()
        require(not git(target, "ls-tree", "-r", "--name-only", chosen, "--", ":(literal)" + relative),
                "目标分支跟踪状态文件路径，请使用两工作区外的状态文件")
    source_head = head(source)
    source_branch = branch(source)
    cleanup_result = cleanup(source, getattr(args, "cleanup_path", ()),
                             ready=getattr(args, "cleanup_ready", False))
    exclusions = [PurePosixPath(path.replace("\\", "/")).as_posix()
                  for path in getattr(args, "cleanup_path", ())]
    state = {"schema": 1, "kind": KIND, "phase": "preparing",
             "source": str(source), "source_head": source_head,
             "source_branch": source_branch, "target": str(target),
             "target_branch": chosen, "target_previous_branch": branch(target),
             "target_previous_head": head(target), "stash": None,
             "cleanup": cleanup_result, "artifact_exclusions": exclusions}
    phase(state_path, state, "staging_source")
    try:
        stage_source(source, exclusions)
        require(head(source) == source_head and branch(source) == source_branch,
                "暂存期间源 HEAD 或分支发生变化，已保留现场")
        source_status = changes(source, exclusions)
        require(all(item["y"] == " " and item["x"] != "?" for item in source_status),
                "暂存后仍有未暂存或未跟踪改动，已保留现场")
        reject_embedded(source)
        tree = index_tree(source)
        if branch(target) != chosen:
            phase(state_path, state, "switching_target", source_tree=tree,
                  source_status=source_status)
            run(target, "switch", "--", chosen)
        check_state_location(state_path, source, target, new=False)
        ensure_ready(target)
        require(branch(target) == chosen and not changes(target, exclusions),
                "切换后目标分支不符或出现改动，已保留现场")
        phase(state_path, state, "prepared", source_tree=tree,
              source_status=source_status, target_head=head(target),
              ignored_resources=ignored(source), ignored_scan="directory-boundaries-only; caller must inspect resources before cleanup")
    except (GitError, OSError) as error:
        phase(state_path, state, "prepare_failed", error=str(error))
        raise
    return {**state, "state": str(state_path),
            "commits": git(source, "log", "--format=%h %s", f"{state['target_head']}..{source_head}", "--", *business_pathspecs(exclusions)),
            "committed_diff_stat": git(source, "diff", "--stat", f"{state['target_head']}...{source_head}", "--", *business_pathspecs(exclusions)),
            "staged_diff_stat": git(source, "diff", "--cached", "--stat", "--", *business_pathspecs(exclusions)),
            "next": "审查目标规则、完整差异与忽略资源，生成唯一文案并取得统一确认后 apply"}


def source_unchanged(state: dict, *, clean: bool) -> Path:
    source = repository(Path(state["source"]))
    ensure_ready(source)
    require(head(source) == state["source_head"] and branch(source) == state["source_branch"],
            "源 HEAD 或分支已变化，保留 worktree 和快照")
    if clean:
        require(not changes(source, state.get("artifact_exclusions", ())), "源出现新改动，保留 worktree 和快照")
    else:
        require(index_tree(source) == state["source_tree"] and
                changes(source, state.get("artifact_exclusions", ())) == state["source_status"],
                "源索引或工作区已变化，需要重新审查和确认")
        unstaged = run(source, "diff", "--name-only", "-z").stdout.split(b"\0")
        require(not any(p and not excluded(os.fsdecode(p), state.get("artifact_exclusions", ())) for p in unstaged),
                "源工作区与索引不一致，需要重新审查和确认")
    return source


def target_unchanged(state: dict, *, clean: bool, result: bool = False) -> Path:
    target = repository(Path(state["target"]))
    ensure_ready(target)
    expected = state["result_head"] if result else state["target_head"]
    require(head(target) == expected and branch(target) == state["target_branch"],
            "目标 HEAD 或分支已变化，保留现场")
    if clean:
        require(not changes(target, state.get("artifact_exclusions", ())), "目标出现改动，保留现场")
        require_no_staged_outputs(target, state.get("artifact_exclusions", ()))
    return target


def apply(args: argparse.Namespace) -> dict:
    require(args.confirmed, "apply 需要 --confirmed，调用方须先取得具体方案和文案确认")
    state_path = Path(args.state).resolve()
    state = read_state(state_path)
    require(state["phase"] == "prepared", "apply 仅接受 prepared；中断或失败的写操作不可自动重放")
    source = source_unchanged(state, clean=False)
    target = target_unchanged(state, clean=True)
    message_path = Path(args.message).resolve()
    require(message_path != state_path, "文案文件不能与状态文件相同")
    check_artifact_location(message_path, source, target)
    message = message_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").rstrip("\n")
    require(bool(message.strip()), "提交文案不能为空")
    require("\x00" not in message, "提交文案不能包含 NUL")
    phase(state_path, state, "approved", message=message)
    merge_input = state["source_head"]
    if state["source_status"]:
        identifier = "sgit-merge-" + uuid.uuid4().hex
        phase(state_path, state, "stashing", stash_identifier=identifier)
        try:
            # prepare staged every business file, including formerly untracked files.
            # --staged avoids touching excluded local working files or ignored caches.
            run(source, "stash", "push", "--staged", "--message", identifier)
            candidates = []
            for line in git(source, "reflog", "show", "--format=%H%x09%gs", "refs/stash").splitlines():
                sha, _, subject = line.partition("\t")
                if subject.endswith(": " + identifier):
                    candidates.append(sha)
            require(len(candidates) == 1, "无法唯一定位本次 stash，禁止使用默认 stash 条目")
            merge_input = candidates[0]
            phase(state_path, state, "snapshot_created", stash=merge_input)
            require(git(source, "rev-parse", merge_input + "^{tree}") == state["source_tree"],
                    "stash 树与已确认源索引不符，保留现场")
            require(git(source, "rev-parse", merge_input + "^1") == state["source_head"],
                    "stash 父提交与源 HEAD 不符，保留现场")
            source_unchanged(state, clean=True)
        except (GitError, OSError) as error:
            phase(state_path, state, "stash_failed", error=str(error))
            raise
    target_unchanged(state, clean=True)
    merge_input = filtered_input(source, state["source_head"], state["target_head"],
                                 state["source_tree"], merge_input, state_path.parent,
                                 state.get("artifact_exclusions", ()))
    phase(state_path, state, "merging", merge_input=merge_input)
    try:
        result = run(target, "merge", "--squash", merge_input, ok=(0, 1))
        conflicts = git(target, "ls-files", "--unmerged")
        if conflicts:
            phase(state_path, state, "conflicts", conflicts=conflicts)
            return {**state, "state": str(state_path), "next": "分析并解决冲突、审查完整净差异和验证后运行 commit"}
        require(result.returncode == 0,
                "squash 合并非冲突失败：" + result.stderr.decode("utf-8", errors="replace").strip())
        phase(state_path, state, "merged")
    except (GitError, OSError) as error:
        phase(state_path, state, "merge_failed", error=str(error))
        raise
    return {**state, "state": str(state_path),
            "diff_stat": git(target, "diff", "--cached", "--stat"),
            "next": "审查完整净差异并完成必要验证后运行 commit"}


def commit(args: argparse.Namespace) -> dict:
    state_path = Path(args.state).resolve()
    state = read_state(state_path)
    require(state["phase"] in ("merged", "conflicts"),
            "commit 仅接受 merged/conflicts；失败或不确定的提交不可自动重放")
    source_unchanged(state, clean=True)
    target = target_unchanged(state, clean=False)
    require(not git(target, "ls-files", "--unmerged"), "仍存在未解决冲突")
    require_no_staged_outputs(target, state.get("artifact_exclusions", ()))
    target_status = changes(target, state.get("artifact_exclusions", ()))
    require(all(item["y"] == " " and item["x"] != "?" for item in target_status),
            "目标有未暂存或未跟踪改动，先审查并暂存明确的整合结果")
    run(target, "diff", "--cached", "--check")
    tree = index_tree(target)
    phase(state_path, state, "committing", reviewed_tree=tree)
    try:
        if tree == git(target, "rev-parse", state["target_head"] + "^{tree}"):
            result = None
            result_head = state["target_head"]
        else:
            result = commit_verified(target, state["message"], state["target_head"], tree)
            result_head = result["commit"]
        phase(state_path, state, "committed", result=result, result_head=result_head)
    except (GitError, OSError) as error:
        phase(state_path, state, "commit_failed", error=str(error))
        raise
    return {**state, "state": str(state_path), "next": "确认必要验证通过和资源已保存后运行 check-cleanup"}


def verify_result(state: dict) -> Path:
    target = target_unchanged(state, clean=True, result=True)
    require(git(target, "rev-parse", state["result_head"] + "^{tree}") == state["reviewed_tree"],
            "目标提交树与审查树不符")
    if state["result"] is not None:
        parents = git(target, "show", "-s", "--format=%P", state["result_head"]).split()
        require(parents == [state["target_head"]], "汇总提交不是操作前 SHA 的唯一直接子提交")
        require(git(target, "show", "-s", "--format=%B", state["result_head"]) == state["message"],
                "提交文案与批准文案不符")
    else:
        require(state["result_head"] == state["target_head"], "无净变化时目标 HEAD 应保持不变")
    if state["stash"]:
        require(git(target, "rev-parse", state["stash"] + "^{tree}") == state["source_tree"],
                "恢复快照缺失或内容不符")
    return target


def check_cleanup(args: argparse.Namespace) -> dict:
    state_path = Path(args.state).resolve()
    state = read_state(state_path)
    require(state["phase"] in ("committed", "cleanup_ready"), "尚未完成汇总提交验证")
    target = verify_result(state)
    source = source_unchanged(state, clean=True)
    reject_embedded(source)
    entries = worktrees(target)
    entry = next((item for item in entries if Path(item["path"]) == source), None)
    require(entry is not None and source != Path(entries[0]["path"]) and source != target,
            "源不再是可移除的附加 worktree")
    require(not any(entry[key] for key in ("bare", "locked", "prunable")),
            "源 worktree 现在为 bare、locked 或 prunable")
    resources = ignored(source)
    phase(state_path, state, "cleanup_ready", cleanup_ignored_resources=resources)
    return {**state, "state": str(state_path), "cleanup_source": str(source),
            "cleanup_cwd": str(target), "next": "调用方确认验证通过及资源保存后，通过准确 managed identity 归档或普通 git worktree remove；禁止 --force"}


def verify(args: argparse.Namespace) -> dict:
    state_path = Path(args.state).resolve()
    state = read_state(state_path)
    require(state["phase"] in ("committed", "cleanup_ready", "verified"), "尚未完成汇总提交")
    target = verify_result(state)
    require(all(Path(item["path"]) != Path(state["source"]) for item in worktrees(target)),
            "源 worktree 仍登记，收尾尚未完成")
    phase(state_path, state, "verified")
    return {**state, "state": str(state_path), "next": "完成；保留源分支和准确 stash SHA"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--source", required=True)
    command.add_argument("--state", required=True)
    command.add_argument("--target-branch")
    command.add_argument("--authorized", action="store_true")
    command.add_argument("--cleanup-ready", action="store_true",
                         help="Caller verified no source build/preview/validation process uses cleanup outputs")
    command.add_argument("--cleanup-path", action="append", default=[],
                         help="Additional reviewed, unused source-relative temporary output to delete and exclude")
    command.set_defaults(handler=prepare)
    command = commands.add_parser("apply")
    command.add_argument("--state", required=True)
    command.add_argument("--message", required=True)
    command.add_argument("--confirmed", action="store_true")
    command.set_defaults(handler=apply)
    for name, handler in (("commit", commit), ("check-cleanup", check_cleanup), ("verify", verify)):
        command = commands.add_parser(name)
        command.add_argument("--state", required=True)
        command.set_defaults(handler=handler)
    args = parser.parse_args()
    try:
        result = args.handler(args)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 3 if result["phase"] == "conflicts" else 0
    except (GitError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
