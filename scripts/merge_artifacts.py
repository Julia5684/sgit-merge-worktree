"""Exclude local outputs from integration; delete only reviewed untracked outputs."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile

from git_common import GitError, git, run

ROOT_OUTPUTS = {".local", "build", "dist", "tmp", "temp", "logs", "cache", ".cache",
                ".internal", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
NESTED_CACHES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}
TEMP_SUFFIXES = {".log", ".tmp", ".temp", ".pyc", ".pyo"}
PROTECTED = {"saves", "save", "signing", "keys", "credentials", "source-assets"}
LOCAL_BUILDS = {"build", "builds", "compiled", "bundle", "bundles", "preview", "previews"}


def excluded(path: str, extra=()) -> bool:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return bool(parts) and (
        parts[0] in ROOT_OUTPUTS or ".local" in parts or
        any(part in NESTED_CACHES for part in parts) or
        PurePosixPath(parts[-1]).suffix.lower() in TEMP_SUFFIXES or
        any(path == item or path.startswith(item + "/") for item in extra))


def changes(repo: Path, extra=()) -> list[dict]:
    from git_common import status
    return [item for item in status(repo) if not excluded(item["path"], extra)]


def paths_input(paths) -> bytes:
    return b"".join(os.fsencode(":(literal)" + path) + b"\0" for path in sorted(set(paths)))


def business_pathspecs(extra=()) -> list[str]:
    # Pathspecs for business-only history/diff summaries.
    return (["."] + [":(top,glob,exclude)" + name + "/**" for name in sorted(ROOT_OUTPUTS)] +
            [":(glob,exclude)**/" + name + "/**" for name in sorted(NESTED_CACHES | {".local"})] +
            [":(glob,icase,exclude)**/*" + suffix for suffix in sorted(TEMP_SUFFIXES)] +
            [":(top,exclude,literal)" + name for name in extra])


def stage_source(repo: Path, extra=()) -> None:
    staged = [os.fsdecode(p) for p in run(repo, "diff", "--cached", "--name-only", "-z").stdout.split(b"\0") if p]
    skipped = [p for p in staged if excluded(p, extra)]
    if skipped:
        run(repo, "restore", "--staged", "--source=HEAD", "--pathspec-from-file=-",
            "--pathspec-file-nul", input=paths_input(skipped))
    candidates = run(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout
    selected = [os.fsdecode(p) for p in candidates.split(b"\0") if p and not excluded(os.fsdecode(p), extra)]
    if selected:
        run(repo, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", input=paths_input(selected))


def require_no_staged_outputs(repo: Path, extra=()) -> None:
    paths = run(repo, "diff", "--cached", "--name-only", "-z").stdout.split(b"\0")
    if any(p and excluded(os.fsdecode(p), extra) for p in paths):
        raise GitError("目标索引包含 .local 或临时产物改动, 保留现场并停止")


def cleanup(repo: Path, extra_paths=(), *, ready=False) -> dict:
    """Every candidate is validated before the first deletion; never follow links."""
    candidates = []
    local = repo / ".local"
    if local.is_symlink() or (local.exists() and local.resolve() != local):
        raise GitError(".local 是符号链接或目录联接, 无法安全清理")
    checks = local / "checks"
    if checks.is_symlink() or (checks.exists() and checks.resolve() != checks):
        raise GitError("checks 是符号链接或目录联接, 无法安全清理")
    if checks.exists():
        candidates.extend(p for p in checks.iterdir() if p.name != "tools")
    candidates.extend(local / name for name in LOCAL_BUILDS if (local / name).exists())
    candidates.extend(repo / name for name in ("build", "dist", "tmp", "temp", "__pycache__") if (repo / name).exists())
    for raw in extra_paths:
        relative = PurePosixPath(raw.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0].endswith(":"):
            raise GitError("清理路径必须是源 worktree 内的明确相对路径")
        candidates.append(repo.joinpath(*relative.parts))
    tracked = [os.fsdecode(p) for p in run(repo, "ls-files", "-z").stdout.split(b"\0") if p]
    selected = []
    for path in sorted(set(candidates), key=lambda p: (len(p.parts), str(p))):
        if any(path.is_relative_to(parent) for parent in selected):
            continue
        if not path.exists() and not path.is_symlink():
            continue
        relative = path.relative_to(repo).as_posix()
        if path == repo or not path.resolve().is_relative_to(repo) or any(part.lower() == ".git" for part in path.parts):
            raise GitError("清理路径逃逸源 worktree: " + str(path))
        # Do not erase unique inputs just because their parent is called .local.
        if any(part.lower() in PROTECTED or part.lower().endswith("save") for part in path.relative_to(repo).parts):
            raise GitError("清理范围包含存档, 签名或制作输入: " + relative)
        if relative in (".local", ".local/checks", ".local/checks/tools") or relative.startswith(".local/checks/tools/"):
            raise GitError("不能整目录删除 .local 或共享验证工具")
        if any(p == relative or p.startswith(relative + "/") for p in tracked):
            raise GitError("清理候选包含已跟踪文件, 先人工确认用途: " + relative)
        for parent in (path, *path.parents):
            if parent == repo:
                break
            if parent.is_symlink() or parent.resolve() != parent:
                raise GitError("清理路径包含符号链接或目录联接: " + str(parent))
        for parent, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                item = Path(parent) / name
                if name.lower() == ".git" or item.is_symlink() or item.resolve() != item:
                    raise GitError("清理目录内有链接或嵌套仓库: " + str(item))
                if name.lower() in PROTECTED or name.lower().endswith("save"):
                    raise GitError("清理目录内有受保护输入: " + str(item))
        selected.append(path)
    if selected and not ready:
        raise GitError("请先停止源 worktree 的构建, 预览和验证进程, 再传 --cleanup-ready")
    removed = []
    for path in selected:
        relative = path.relative_to(repo).as_posix()
        # Recheck the resolved boundary immediately before each recursive deletion.
        if path.resolve() != path or not path.is_relative_to(repo):
            raise GitError("删除前路径检查失败: " + str(path))
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(relative)
    return {"removed": removed, "free_bytes": shutil.disk_usage(repo).free}


def filtered_input(repo: Path, source_head: str, target_head: str, tree: str,
                   original: str, directory: Path, extra=()) -> str:
    """Make a merge-only input with output paths unchanged from the merge base."""
    bases = git(repo, "merge-base", "--all", source_head, target_head).splitlines()
    if len(bases) != 1:
        raise GitError("合并基线不唯一, 不能安全过滤临时产物")
    def outputs(revision):
        result = {}
        for record in run(repo, "ls-tree", "-r", "-z", revision).stdout.split(b"\0"):
            if record:
                info, _, path = record.partition(b"\t")
                if excluded(os.fsdecode(path), extra):
                    mode, kind, sha = info.split(b" ")
                    result[path] = mode + b" " + sha + b"\t" + path + b"\0"
        return result
    incoming, baseline = outputs(tree), outputs(bases[0])
    if incoming == baseline:
        return original
    with tempfile.TemporaryDirectory(prefix=".filtered-index-", dir=directory) as temporary:
        if Path(temporary).resolve().parent != directory.resolve():
            raise GitError("临时索引目录超出本次任务范围")
        index = Path(temporary) / "index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index)
        def index_run(*args, data=None):
            result = subprocess.run(["git", "-C", str(repo), *args], input=data,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            if result.returncode:
                raise GitError(result.stderr.decode("utf-8", errors="replace"))
            return result.stdout
        index_run("read-tree", tree)
        zero = b"0" * len(source_head)
        records = b"".join(b"0 " + zero + b"\t" + path + b"\0" for path in incoming)
        records += b"".join(baseline.values())
        index_run("update-index", "-z", "--index-info", data=records)
        filtered_tree = index_run("write-tree").decode("ascii").strip()
    # An unreferenced input commit leaves the source branch/HEAD unchanged.
    return git(repo, "commit-tree", filtered_tree, "-p", source_head,
               "-m", "sgit filtered merge input")
