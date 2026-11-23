# Merge Artifact Handling

This playbook applies strictly to `sgit-merge-worktree`. The target of cleanup is the linked source worktree that is scheduled to be merged and removed; it does not clean build outputs on the target main branch or in other worktrees. Read-only previews never trigger deletions or merges.

## 1. Pre-Merge Cleanup

1. Identify the source worktree and its absolute canonical path. Confirm that any build, test, and preview processes running inside it have terminated and their outputs are no longer needed. Only terminate processes launched by the current task; if other tasks are still actively using the worktree, abort the merge.
2. Built-in cleanup targets checked by `merge_worktree.py prepare`:
   - Outdated verification artifacts under `.local/checks/` (excluding tools)
   - Conventional build directories under `.local/` (e.g. `build`, `compiled`, `bundle`, `preview`)
   - Root-level build directories: `build`, `dist`, `tmp`, `temp`, `__pycache__`
   - Additional custom project build folders or caches must be explicitly passed via repeated `--cleanup-path <rel_path>`. Never use dangerous blanket commands like `git clean -fdx`.
3. Candidate verification: All paths to be deleted must reside strictly within the source worktree, must not contain symlinks, junctions, or nested git repositories, and MUST NOT contain tracked files. If deletion fails, stop immediately without proceeding to staging or merging.
4. Protected data: Always preserve game saves, signing keys, unique creative inputs, production deliverable packages, and anything of lasting value. Keep `.local/checks/tools`. Do not delete `.local` as a whole, do not copy entire output trees for the merge, and do not create redundant backup copies of unneeded build outputs.

## 2. Artifacts Excluded from Merging

The following paths are permanently excluded from integration:
- All `.local/` paths
- Root-level build/temp/log/cache/dependency folders (`build`, `dist`, `tmp`, `temp`, `logs`, `cache`, `.cache`, `node_modules`, etc.)
- Python cache folders (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`)
- Files ending in `.log`, `.tmp`, `.temp`, `.pyc`, `.pyo`
- Any paths specified via `--cleanup-path`

Exclusion applies across the entire pipeline: source staging, status checks, safety stashing, commit diffs, conflict resolution, and the final squash commit.
- Staged excluded items are automatically unstaged, keeping their working tree content intact.
- Source snapshots do not collect local modifications to excluded paths.
- If historical source commits already mistakenly contain artifacts, an isolated temporary index restores those paths to the merge-base values, generating an anonymous squash input without rewriting history or advancing the source branch.
- Business changes merge cleanly, and target main branch values for excluded paths remain untouched.
- If the target index already has staged excluded items, the operation halts immediately to prevent committing external artifacts.

## 3. Teardown

After squash integration, proceed with the required tests, single commit creation, and worktree cleanup. Any temporary outputs produced by verification in the target project are discarded and never committed.
