# Conflict Resolution & Safety Boundaries

When `merge_worktree.py apply` reports conflicts, preserve the source worktree, target working copy, and safety snapshot stash intact.

## 1. Locating and Resolving Conflicts

1. Run `git ls-files -u` to locate unmerged paths.
2. Inspect the common ancestor (`merge-base`), the target main branch version, the incoming tree, callers, and tests to integrate the valid business intent of both branches file by file.
3. Excluded paths (`.local`, build outputs, logs, caches) MUST NOT enter conflict resolution. If an excluded path appears in conflict, stop and re-check artifact filtering; do not copy or recreate it for commit.
4. The script cannot divine business semantics; NEVER use blanket `--ours` or `--theirs` overrides across files.

## 2. Staging and Completion

1. After resolving each file, stage only the verified path with `git add -- <path>`.
2. Once all conflicts are resolved, verify that `git ls-files -u` is empty and that no lingering conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) remain in the diff.
3. Verify impacted functionality and affected callers.
4. Note: `merge --squash` does not generate `MERGE_HEAD`; do NOT run `git merge --continue`. Proceed directly to updating version files and running `merge_worktree.py commit`.

## 3. Exception Handling

If crucial semantics cannot be determined from available project files or if changes exceed the confirmed scope, pause and ask the user. For unexpected failures, hook modifications, or interrupted states:
- Preserve the exact environment.
- Inspect the state JSON, actual HEAD, index, and the stash SHA before deciding on recovery.
- Do NOT delete the state file to blindly retry, and do NOT automatically run destructive operations like `git reset --hard` or `git checkout -f`.
