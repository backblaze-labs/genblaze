"""Tests for tools/batch_policy.py: the one list of paths batch agents may not change.

Both the executor guard hook and the pre-push ``publish`` check read this list,
so a gap here is a gap in both.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import pytest  # noqa: E402
from batch_policy import is_dependency_file, is_guarded_path  # noqa: E402


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/release.yml",
        ".github/workflows/new.yml",
        ".claude/settings.json",
        ".claude/hooks/batch-executor-guard.py",
        ".claude/agents/batch-executor.md",
        "tools/release_smoke.sh",
        "tools/batch_gitsteward.py",
        "tools/prepare_release.py",
        "tools/check_pin_parity.py",
        "CHANGELOG.md",
        "RELEASING.md",
        "SECURITY.md",
        "Makefile",
        ".git/hooks/pre-commit",
        "./.github/CODEOWNERS",
    ],
)
def test_guarded_paths(path):
    assert is_guarded_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "libs/core/genblaze_core/_utils.py",
        "libs/connectors/lmnt/tests/test_lmnt_provider.py",
        "docs/features/parquet-sink.md",
        "README.md",
        "tools/tests/test_connector_output_paths.py",
        "examples/pipeline.py",
    ],
)
def test_ordinary_paths_are_not_guarded(path):
    assert not is_guarded_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "libs/core/pyproject.toml",
        "pyproject.toml",
        "uv.lock",
        "libs/spec/package.json",
        "libs/spec/package-lock.json",
        "requirements-dev.txt",
    ],
)
def test_dependency_files(path):
    assert is_dependency_file(path)


def test_source_file_is_not_a_dependency_file():
    assert not is_dependency_file("libs/core/genblaze_core/pyproject_helpers.py")


@pytest.mark.parametrize(
    "path",
    [
        ".GitHub/workflows/x.yml",
        ".Claude/settings.local.json",
        "Changelog.md",
        ".git",
        "CLAUDE.md",
        "AGENTS.md",
        "libs/core/CLAUDE.md",
        ".gitmodules",
        ".gitattributes",
        "libs/core/.gitattributes",
        ".pre-commit-config.yaml",
        ".envrc",
        ".vscode/settings.json",
        "．github/workflows/x.yml",  # fullwidth full stop folds to "."
    ],
)
def test_guard_is_case_and_unicode_insensitive_and_covers_agent_files(path):
    assert is_guarded_path(path)


@pytest.mark.parametrize(
    "path",
    ["PyProject.toml", "setup.py", "libs/x/setup.cfg", "requirements/dev.in", "Pipfile.lock"],
)
def test_more_dependency_files(path):
    assert is_dependency_file(path)
