# sgit-merge-worktree: Safe Worktree Squash & Teardown for AI Agents

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)
[![Conventional Commits: 1.0.0](https://img.shields.io/badge/Conventional%20Commits-1.0.0-yellow.svg)](https://www.conventionalcommits.org/en/v1.0.0/)
[![Tests: 61 Passed](https://img.shields.io/badge/Tests-61%20Passed-success.svg)](tests/)

A deterministic, artifact-excluding Git worktree squash, SemVer synchronization, and teardown skill designed for modern AI coding agents (Claude Code, Cursor, Codex, Windsurf, Antigravity, OpenClaw, Roo Code).

---

## ⚡ The Problem with AI Worktrees

Multi-agent and branch workflows frequently spin up separate Git worktrees. However, integrating a worktree back into the main branch often results in serious problems:
* **Artifact Pollution**: Worktrees accumulate local build outputs, compilation artifacts (`.local/`, `build/`, `dist/`, `tmp/`), caches, and debug logs. Standard `git merge --squash` pulls these dirty files directly into the main repository.
* **Worktree Leaks & Drift**: Agents abandon unremoved worktree folders across disk, leaving locked repositories or stale tracking branches.
* **Accidental Data Loss**: Force-deleting worktrees without creating a safety snapshot of uncommitted experimentation.
* **Version Drift**: The version on the main branch is not incremented, or requires multiple corrective commits after the merge.

---

## ✨ The Solution: How `sgit-merge-worktree` Works

`sgit-merge-worktree` provides an atomic, multi-stage pipeline powered by robust Python standard-library scripts:

1. **Pre-Merge Artifact Cleanup (`merge_artifacts.py`)**:
   Safely terminates idle processes and purges unneeded temporary build directories (`.local/`, `build/`, `dist/`, `tmp/`, `__pycache__`) while protecting game saves, signing keys, credentials, and source assets.
2. **Strict Artifact Exclusion**:
   Guarantees that temporary files and caches NEVER enter the Git index, safety stash, squash diff, conflict resolution, or final commit. If artifacts were mistakenly committed in branch history, an isolated temporary index restores them to baseline before squashing.
3. **Safety Snapshot Stash**:
   Captures an identifiable safety stash of uncommitted work before performing any mutations, ensuring zero data loss.
4. **Conventional Commits & Automated SemVer Bump**:
   Calculates a 3-segment Semantic Versioning patch bump (or specified major/minor bump) against the target main branch and bundles the update into the single squash commit.
5. **Clean Worktree Removal & Teardown**:
   Performs pre-removal sanity checks (`check-cleanup`), removes the linked worktree (`git worktree remove`) without dangerous `--force` flags, verifies the target branch is clean (`verify`), and cleans up task state directories.
6. **Zero External Dependencies**:
   100% built on Python 3.10+ standard library and Git CLI. No external packages required.

---

## 📦 Compatibility & Supported Platforms

| Platform | Location / Setup | Calling Convention |
| --- | --- | --- |
| **Agensi.io (MCP)** | Install via Agensi Marketplace | Natural language / Tool invocation |
| **Claude Code** | `~/.claude/skills/sgit-merge-worktree/` | `/sgit-merge-worktree` |
| **Cursor / Windsurf** | `~/.agents/skills/sgit-merge-worktree/` | `$sgit-merge-worktree` or prompt |
| **Codex CLI** | `~/.agents/skills/sgit-merge-worktree/` | `$sgit-merge-worktree` |
| **Antigravity** | `~/.gemini/config/skills/sgit-merge-worktree/` | `/sgit-merge-worktree` |

---

## 🚀 Quick Start & Usage

Simply invoke the skill in your agent prompt from within a linked worktree:

```text
# Standard integration: Clean artifacts, squash all changes to main branch, bump version, and remove worktree
/sgit-merge-worktree

# Preview only: Inspect worktree status, artifact cleanup plan, and proposed commit without applying
/sgit-merge-worktree preview only, do not merge

# Target specific branch: Specify target branch if ambiguous
/sgit-merge-worktree merge current worktree into main
```

### Typical Output Preview

```text
---
Source Worktree: D:/Workspace/project/.worktrees/feature-auth (branch: feature-auth)
Target Branch: main (HEAD: 8f9b2c1)
Artifact Cleanup: 14 temporary build files removed from .local/build (freed 42 MB)
Version Plan: 1.2.0 -> 1.2.1 (patch increment on main)
Proposed Squash Commit:
feat(auth): complete OAuth2 implementation and token refresh

Features
- Add OAuth2 provider support
- Implement token refresh rotation

Bug Fixes
- Fix session expiration race condition
---
Confirm this proposal to execute squash merge, commit to main, and remove source worktree.
```

---

## 📁 Repository Structure

```text
sgit-merge-worktree/
├── SKILL.md                 # Primary skill instructions for AI agents
├── README.md                # Documentation & marketplace overview
├── LICENSE                  # MIT License
├── agents/
│   └── openai.yaml          # Agent platform interface metadata
├── references/              # Detailed specification playbooks
│   ├── conflicts.md         # Conflict resolution & safety rules
│   ├── merge-artifacts.md   # Artifact exclusion & cleanup rules
│   ├── message.md           # Conventional Commits format & categories
│   ├── version.md           # 3-segment SemVer increment rules
│   └── workspace.md         # Task state directory conventions
├── scripts/                 # Self-contained Python standard-library scripts
│   ├── __init__.py
│   ├── commit.py            # SemVer calculator & commit helpers
│   ├── git_common.py        # Shared Git primitives & worktree handlers
│   ├── inspect_repo.py      # Repository & worktree discovery
│   ├── merge_artifacts.py   # Artifact filtering & exclusion logic
│   └── merge_worktree.py    # Multi-stage worktree squash engine
└── tests/                   # Automated unit and integration test suite
    ├── test_commit.py       # Commit & version tests
    ├── test_inspect.py      # Repository inspection tests
    └── test_merge_worktree.py # Complete 61-case worktree test suite
```

---

## 🧪 Testing

Run the included test suite against any local Python 3.10+ installation:

```bash
python -m unittest discover -s tests -v
```

All 61 integration tests execute in disposable system-temporary repositories without touching your local work.

---

## 🔒 Security & Privacy

* **100% Local Execution**: Operates purely within your local file system and Git binary.
* **Zero Telemetry / No Network**: Never transmits code or diffs externally.
* **Strict Human Confirmation**: Requires explicit confirmation before squashing, writing to main, or deleting worktrees.

---

## 📄 License

Distributed under the [MIT License](LICENSE). Copyright (c) 2026 SpxZhu.
