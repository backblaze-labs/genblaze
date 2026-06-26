from __future__ import annotations

import importlib.util
import os
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_TOOL = REPO_ROOT / "tools/release_import_smoke.py"

spec = importlib.util.spec_from_file_location("release_import_smoke", SMOKE_TOOL)
assert spec is not None
assert spec.loader is not None
release_import_smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_import_smoke)

ALL_EXTRA_PACKAGE_IMPORTS = release_import_smoke.ALL_EXTRA_PACKAGE_IMPORTS
CORE_IMPORTS = release_import_smoke.CORE_IMPORTS
DEFAULT_PACKAGE_IMPORTS = release_import_smoke.DEFAULT_PACKAGE_IMPORTS
REDACTION = release_import_smoke.REDACTION
SMOKE_IMPORTS = release_import_smoke.SMOKE_IMPORTS
TIMEOUT_ENV = release_import_smoke.TIMEOUT_ENV


def _dependency_name(requirement: str) -> str:
    match = re.match(r"[A-Za-z0-9_.-]+", requirement)
    assert match is not None
    return match.group(0).lower().replace("_", "-")


def test_all_extra_import_mapping_matches_umbrella_all_extra() -> None:
    meta = tomllib.loads((REPO_ROOT / "libs/meta/pyproject.toml").read_text())
    all_extra_packages = [
        _dependency_name(requirement)
        for requirement in meta["project"]["optional-dependencies"]["all"]
    ]

    assert [package for package, _module in ALL_EXTRA_PACKAGE_IMPORTS] == all_extra_packages


def test_default_import_mapping_matches_umbrella_dependencies() -> None:
    meta = tomllib.loads((REPO_ROOT / "libs/meta/pyproject.toml").read_text())
    default_packages = [
        _dependency_name(requirement) for requirement in meta["project"]["dependencies"]
    ]

    assert [package for package, _module in DEFAULT_PACKAGE_IMPORTS] == default_packages


def test_smoke_imports_unique_core_and_all_extra_modules() -> None:
    expected_modules = (
        {"genblaze", "genblaze_core.storage"}
        | {module for _package, module in DEFAULT_PACKAGE_IMPORTS}
        | {module for _package, module in ALL_EXTRA_PACKAGE_IMPORTS}
    )

    assert len(SMOKE_IMPORTS) == len(set(SMOKE_IMPORTS))
    assert set(CORE_IMPORTS).issubset(SMOKE_IMPORTS)
    assert set(SMOKE_IMPORTS) == expected_modules


def test_smoke_import_sandboxes_env_and_redacts_import_failures(
    tmp_path, monkeypatch, capsys
) -> None:
    seeded_value = "review-secret-token-123"
    module_name = "fake_leaky_import"
    (tmp_path / f"{module_name}.py").write_text(
        "import os\n"
        f"literal = {seeded_value!r}\n"
        "env_secret = os.environ.get('OPENAI_API_KEY')\n"
        "home_value = os.environ.get('HOME')\n"
        "print(f'stdout env={env_secret} literal={literal} home={home_value}')\n"
        "raise RuntimeError("
        "f'stderr env={env_secret} literal={literal} home={home_value}'"
        ")\n"
    )

    seeded_home = "/sensitive/home/with-creds"
    monkeypatch.setenv("HOME", seeded_home)
    monkeypatch.setenv("OPENAI_API_KEY", seeded_value)
    monkeypatch.setenv("GITHUB_TOKEN", seeded_value)

    original_env = dict(os.environ)
    failures = release_import_smoke.smoke_import([module_name], extra_paths=[tmp_path])
    captured = capsys.readouterr()

    output = captured.out + captured.err

    assert dict(os.environ) == original_env
    assert failures
    assert failures[0][0] == module_name
    assert seeded_value not in output
    assert "env=None" in output
    assert "Traceback (most recent call last)" in output
    assert REDACTION in output
    assert seeded_home not in output


def test_smoke_import_timeout_is_configurable(tmp_path, monkeypatch, capsys) -> None:
    module_name = "fake_slow_import"
    (tmp_path / f"{module_name}.py").write_text("import time\ntime.sleep(5)\n")

    monkeypatch.setenv(TIMEOUT_ENV, "0.01")

    failures = release_import_smoke.smoke_import([module_name], extra_paths=[tmp_path])
    output = capsys.readouterr().out

    assert failures == [(module_name, "import subprocess timed out after 0.01s")]
    assert "timed out after 0.01s" in output
