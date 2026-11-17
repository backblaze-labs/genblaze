#!/usr/bin/env python3
"""
What batch-maintainer agents may not change, in one place.

Batch executors work from issue text written by anyone on the internet, so the
files that control releases, CI, agent behavior and dependencies are off-limits
to them. Two independent layers read this list and must never disagree:

* ``.claude/hooks/batch-executor-guard.py`` refuses Edit/Write calls to these
  paths while the executor runs.
* ``batch_gitsteward.publish`` refuses to push a branch whose diff touches them
  (catching anything changed through Bash instead of the edit tools).

A fix that genuinely needs one of these files stays local for a human to finish.
"""

from __future__ import annotations

import fnmatch
import re
import unicodedata
from pathlib import PurePosixPath

# Matched against a casefolded, NFKC-normalized repo-relative path: macOS
# filesystems are case- and normalization-insensitive, so ".GitHub/x" is
# ".github/x" once checked out. fnmatch's "*" also matches "/", so "dir/*"
# covers everything below dir.
GUARDED_PATTERNS: tuple[str, ...] = (
    ".github",
    ".github/*",
    ".claude",
    ".claude/*",
    ".git",
    ".git/*",
    ".vscode/*",
    ".idea/*",
    "tools/release*",
    "tools/batch_*",
    "tools/prepare_release.py",
    "tools/check_pin_parity.py",
    "changelog.md",
    "releasing.md",
    "security.md",
    "makefile",
)

# Matched on the file name anywhere in the tree: agent instructions and git or
# tooling config that changes how code runs on a maintainer's machine.
GUARDED_NAMES: tuple[str, ...] = (
    "claude.md",
    "agents.md",
    ".gitmodules",
    ".gitattributes",
    ".pre-commit-config.yaml",
    ".envrc",
    ".env",
    ".env.*",
)

# Matched on the file name anywhere in the tree. Dependency and version changes
# belong to a human-reviewed PR or the release step, never to a batch agent.
DEPENDENCY_FILE_PATTERNS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "uv.lock",
    "poetry.lock",
    "pipfile",
    "pipfile.lock",
    "requirements*.txt",
    "requirements*.in",
    "constraints*.txt",
    "package.json",
    "package-lock.json",
)

# Any file inside a requirements/ directory (requirements/dev.in, ...).
DEPENDENCY_DIR_PATTERNS: tuple[str, ...] = ("requirements/*", "*/requirements/*")

# Credential shapes that must never appear in a pushed diff. The executor
# sandbox blocks reading credential stores; this is the last check before push.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"),
    re.compile(r"\bpypi-[A-Za-z0-9_-]{50,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
)


def _normalize(path: str) -> str:
    folded = unicodedata.normalize("NFKC", path).casefold().replace("\\", "/")
    return str(PurePosixPath(folded)).removeprefix("./")


def _match(value: str, patterns: tuple[str, ...]) -> bool:
    # fnmatchcase: patterns and values are already casefolded, and plain
    # fnmatch would apply the host's case rules inconsistently.
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def is_guarded_path(path: str) -> bool:
    """True if ``path`` (repo-relative) is a release, CI, git or agent-control file."""
    rel = _normalize(path)
    return _match(rel, GUARDED_PATTERNS) or _match(PurePosixPath(rel).name, GUARDED_NAMES)


def is_dependency_file(path: str) -> bool:
    """True if ``path`` declares or locks dependencies or package versions."""
    rel = _normalize(path)
    return _match(PurePosixPath(rel).name, DEPENDENCY_FILE_PATTERNS) or _match(
        rel, DEPENDENCY_DIR_PATTERNS
    )


def find_secrets(text: str) -> list[str]:
    """Return the credential-shaped strings in ``text`` (redacted to a prefix)."""
    return [m.group(0)[:8] + "…" for p in SECRET_PATTERNS for m in p.finditer(text)]
