"""Shared import smoke check for release verification."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterable

# The umbrella installs core + s3 by default; keep the storage submodule here
# because it has a hard import-time urllib3 dependency hidden by core's lazy
# top-level exports.
CORE_IMPORTS = (
    "genblaze",
    "genblaze_core",
    "genblaze_core.storage",
    "genblaze_s3",
)

# Keep this list aligned with libs/meta/pyproject.toml's genblaze[all] extra.
# tools/tests/test_release_import_smoke.py fails when a new all-extra package
# is added without a matching import smoke target.
ALL_EXTRA_PACKAGE_IMPORTS = (
    ("genblaze-gmicloud", "genblaze_gmicloud"),
    ("genblaze-openai", "genblaze_openai"),
    ("genblaze-google", "genblaze_google"),
    ("genblaze-replicate", "genblaze_replicate"),
    ("genblaze-runway", "genblaze_runway"),
    ("genblaze-luma", "genblaze_luma"),
    ("genblaze-decart", "genblaze_decart"),
    ("genblaze-elevenlabs", "genblaze_elevenlabs"),
    ("genblaze-stability-audio", "genblaze_stability_audio"),
    ("genblaze-lmnt", "genblaze_lmnt"),
    ("genblaze-hume", "genblaze_hume"),
    ("genblaze-assemblyai", "genblaze_assemblyai"),
    ("genblaze-langsmith", "genblaze_langsmith"),
    ("genblaze-nvidia", "genblaze_nvidia"),
)

CONNECTOR_IMPORTS = tuple(module for _package, module in ALL_EXTRA_PACKAGE_IMPORTS)
SMOKE_IMPORTS = CORE_IMPORTS + CONNECTOR_IMPORTS


def smoke_import(modules: Iterable[str] = SMOKE_IMPORTS) -> list[tuple[str, BaseException]]:
    failures: list[tuple[str, BaseException]] = []
    for module_name in modules:
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            failures.append((module_name, exc))
            print(f"  FAIL: {module_name}: {exc}")
        else:
            print(f"  ok: {module_name}")
    return failures


def main() -> int:
    failures = smoke_import()
    if failures:
        print(f"{len(failures)} import failure(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
