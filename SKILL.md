---
name: sgit-merge-worktree
description: Clean temporary build artifacts in the current linked worktree, exclude .local/logs/caches, squash all business changes into the main branch, update SemVer, and cleanly remove the worktree with atomic verification.
---

# Merge & Teardown Worktree (sgit-merge-worktree)

Integrates all business changes from an ephemeral linked Git worktree into the primary target branch in a single, clean squash commit, automatically cleans up local build artifacts and caches, updates Semantic Versioning, and safely tears down the worktree. Built strictly on Python 3.10+ standard library and Git CLI with zero external dependencies.

## 1. Discovery & Preparation

1. **Inspect Repository & Worktrees**:
   Run `scripts/inspect_repo.py` to identify paths, active branches, and registered worktrees:
   ```bash
   python scripts/inspect_repo.py --repo <source-worktree> --worktrees
   ```
   Determine the target main branch (`main` or `master`) based on repository conventions or remote symbolic refs.
   **Crucial Boundary**: If the current worktree is ALREADY on the target main branch, stop immediately:
   > *"The current worktree is already on the target main branch (<branch>). Merge not required; operation stopped."*

2. **Pre-Flight Sanity Checks**:
   - Source must be a removable linked worktree (detached HEAD is permitted).
   - Target must be a clean working tree checked out on the main branch.
   - If the target has uncommitted changes or active operations (merge/rebase/cherry-pick), stop immediately.

3. **Artifact Pre-Cleanup**:
   Read [Merge Artifact Handling](references/merge-artifacts.md) and [Workspace Conventions](references/workspace.md). Stop any test, build, or preview servers running in the source worktree. Inspect `.local/` and temporary output paths.
   Run `merge_worktree.py prepare`:
   ```bash
   python scripts/merge_worktree.py prepare --source <source_path> --target-branch <main_branch> --state <task_dir>/merge.json --authorized --cleanup-ready
   ```
   This automatically verifies preconditions, safely cleans unneeded source build artifacts, stages business changes, and records source/target state.

4. **Unified Confirmation Proposal**:
   Read [Version Guidelines](references/version.md) to calculate the 3-segment SemVer increment based on the target branch's committed baseline.
   Follow [Commit Message Guidelines](references/message.md) to prepare a single Conventional Commits subject and categorized body (Features, Fixes, Performance, Refactor, etc.).
   Present a unified proposal covering:
   - Source and target worktrees
   - Cleaned artifact summaries
   - Version bump plan (source file, baseline, candidate, target value)
   - Proposed Conventional Commit message
   - Worktree removal and task cleanup plan
   Obtain explicit user confirmation before applying changes.

## 2. Squash Apply & Conflict Resolution

1. Write the confirmed commit message to `<task_dir>/message.txt` (UTF-8).
2. Execute squash apply:
   ```bash
   python scripts/merge_worktree.py apply --state <task_dir>/merge.json --message <task_dir>/message.txt --confirmed
   ```
   The script creates an identifiable safety stash of uncommitted business changes (excluding local artifacts), restores any mistakenly committed history artifacts to baseline values, and executes `git merge --squash` on the target.

3. **Conflict Resolution**:
   If conflicts occur, read [Conflict Resolution](references/conflicts.md). Resolve conflicts file by file preserving the approved business intent. Never use blanket `--ours` or `--theirs`. Excluded artifacts must not enter conflict resolution. After resolving, stage resolved files with `git add -- <paths...>`.

## 3. Version Update & Atomic Commit

1. After squash/conflict resolution, verify net diffs on the target. If actual code changes exist, update the authoritative version files per the approved plan and stage them with `git add -- <version_files...>`.
2. Finalize the commit:
   ```bash
   python scripts/merge_worktree.py commit --state <task_dir>/merge.json
   ```
   This verifies that all conflicts are resolved, creates a single clean squash commit on the target main branch, and verifies parent, tree, and commit message integrity.

## 4. Teardown & Final Delivery

1. Run cleanup pre-check:
   ```bash
   python scripts/merge_worktree.py check-cleanup --state <task_dir>/merge.json
   ```
   This confirms that target is clean on the main branch, source HEAD is unchanged, and returns the canonical source path.
2. Remove the linked worktree from the target directory:
   ```bash
   git worktree remove <source_worktree_canonical_path>
   ```
   *(Do not use `--force`; source branch and safety stashes remain preserved).*
3. Verify teardown:
   ```bash
   python scripts/merge_worktree.py verify --state <task_dir>/merge.json
   ```
4. Automatically delete the single-task directory `<task_dir>` (`.workbench/sgit/<task-id>/`).
5. Deliver the summary report (target branch, commit SHA, version, verified tests, and cleaned task directory).

## 5. Security & Safety Principles

- **Zero Data Loss**: Creates an identifiable safety stash of source modifications before any squash.
- **Artifact Isolation**: Guarantees that `.local`, caches, node_modules, and binaries never contaminate the target branch.
- **Human-in-the-Loop**: Strict unified confirmation boundary before applying merges or committing changes.
