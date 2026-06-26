from __future__ import annotations

import importlib.util
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
SMOKE_IMPORTS = release_import_smoke.SMOKE_IMPORTS


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


def test_smoke_imports_core_and_all_extra_modules_once() -> None:
    expected = CORE_IMPORTS + tuple(module for _package, module in ALL_EXTRA_PACKAGE_IMPORTS)

    assert SMOKE_IMPORTS == expected
    assert len(SMOKE_IMPORTS) == len(set(SMOKE_IMPORTS))
