"""Connectors must name output files through ``get_output_path``.

Hand-rolled ``output_dir / f"{step.step_id}..."`` joins skipped the
single-path-component check, so a ``Step`` whose id was set after validation,
or an ``output_format`` param such as ``"../x"`` used as the suffix, could
write outside ``output_dir`` (same class as the Veo fix, #284). This guards
the whole connector tree, including connectors added later.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONNECTORS = REPO_ROOT / "libs" / "connectors"

# `<anything>_dir / f"{step.step_id...`
_STEP_ID_JOIN = re.compile(r"_dir\s*/\s*f[\"']\{step\.step_id")


def _connector_sources() -> list[Path]:
    return [p for p in CONNECTORS.glob("*/genblaze_*/**/*.py") if "tests" not in p.parts]


def test_connector_sources_are_found():
    assert len(_connector_sources()) > 20


def test_no_hand_rolled_step_id_output_paths():
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{lineno}"
        for path in _connector_sources()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _STEP_ID_JOIN.search(line)
    ]
    assert not offenders, (
        "Build output file paths with genblaze_core.providers.get_output_path "
        "(validates step_id and ext, creates output_dir, temp-file fallback): "
        + ", ".join(offenders)
    )
