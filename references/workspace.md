# Workspace State and Artifact Conventions

Standard commits via direct `submit` read proposals from standard input without creating intermediate state files. Whenever a task involves multi-phase worktree merging, state persistence, or recovery, follow these conventions.

## Task Directory Allocation

1. Always locate the user's primary repository. By default, write state, messages, path lists, and temporary verification evidence to `.workbench/sgit/<task-id>/` located at the target repository root. Never write task files into plugin installation directories.
2. Workspace state artifacts must be untracked and covered by Git ignore rules. Verify using `git check-ignore`. If the path is not ignored, fall back to an isolated temporary directory in the system temp directory rather than modifying `.gitignore`.
3. Merge task state files must be placed in the target repository root or an external temp location; NEVER place them inside the linked source worktree that is scheduled for deletion.
4. The state JSON records file paths, HEAD SHAs, tree hashes, and lifecycle phases. It prevents duplicate execution and tracks mutations.

## Automatic Post-Merge Cleanup

Each merge task uses a directory unique to its `<task-id>`. After all required tests pass, the source worktree is safely removed, and `verify` succeeds:
1. Deliver the final summary in session (source/target branches, commit hash, SemVer bump, verification result).
2. Validate the absolute canonical path of the task directory (it must match `.workbench/sgit/<task-id>/`).
3. Recursively remove only the designated task directory:
   - On Windows: `Remove-Item -LiteralPath <task_dir> -Recurse -Force`
   - Preserve `.workbench/sgit/` and any other sibling task directories.
