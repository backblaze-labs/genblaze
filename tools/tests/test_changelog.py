"""Changie is the source of wave notes, without controlling package versions."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from changelog import ChangelogError, batch_notes, check_changelog, read_wave, require_batched

ROOT = Path(__file__).resolve().parents[2]


def batch_fixture(root):
    """A fixed starting wave, independent of this checkout's release state."""
    pending = root / ".changes/unreleased"
    pending.mkdir(parents=True)
    shutil.copy(ROOT / ".changie.yaml", root)
    (root / ".changes/header.tpl.md").write_text("# Changelog\n")
    (root / ".changes/v0.8.2.md").write_text("## [0.8.2] - 2026-11-16\n")
    (pending / "change.yaml").write_text(
        "component: Internal\nkind: Changed\nbody: Release tooling\ntime: 2026-11-17T00:00:00Z\n"
    )


def test_every_historical_release_section_preserves_original_bytes():
    import hashlib
    import json
    import re

    snapshot = json.loads(
        (ROOT / "tools/tests/fixtures/historical_changelog_pre_migration.json").read_text()
    )["sections_sha256"]
    text = (ROOT / "CHANGELOG.md").read_bytes()
    headings = list(re.finditer(rb"^## \[([0-9][^\]]*)\][^\n]*(?:\n|$)", text, re.M))
    historical = []
    for index, heading in enumerate(headings):
        version = heading[1].decode()
        if version not in snapshot:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        assert hashlib.sha256(text[heading.start() : end]).hexdigest() == snapshot[version], (
            version
        )
        historical.append(version)
    assert historical == list(snapshot)


def test_pre_release_rejects_unbatched_notes_before_other_checks(tmp_path):
    shutil.copytree(ROOT / ".changes", tmp_path / ".changes")
    for name in [".changie.yaml", "CHANGELOG.md", "Makefile"]:
        shutil.copy(ROOT / name, tmp_path)
    (tmp_path / "tools").mkdir()
    shutil.copy(ROOT / "tools/changelog.py", tmp_path / "tools/changelog.py")
    (tmp_path / ".changes/unreleased/_regression.yaml").write_text(
        "component: Internal\nkind: Fixed\nbody: Pending regression fixture\n"
        "time: 2026-11-18T00:00:00Z\n"
    )
    subprocess.run([sys.executable, "tools/changelog.py", "merge"], cwd=tmp_path, check=True)
    # Isolate the real note gate from unrelated SDK and wheel checks.
    (tmp_path / "other-checks.mk").write_text(
        "lint deptry typecheck docs-lint ts-types ts-types-check "
        "pypi-metadata-check pypi-pin-parity "
        "test e2e-smoke release-smoke pypi-new-projects:\n\t@touch other-checks-ran\n"
    )
    result = subprocess.run(
        ["make", "-f", "Makefile", "-f", "other-checks.mk", "pre-release"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0, result.stdout
    assert "unreleased fragments must be batched" in result.stderr
    assert not (tmp_path / "other-checks-ran").exists()


def test_latest_wave_uses_semver_and_checks_heading(tmp_path):
    changes = tmp_path / ".changes"
    changes.mkdir()
    (changes / "v0.9.0.md").write_text("## [0.9.0] - 2026-11-17\n")
    (changes / "v0.10.0.md").write_text("## [0.10.0] - 2026-11-17\n")
    assert read_wave(tmp_path) == "0.10.0"
    (changes / "v0.10.0.md").write_text("## [0.9.0] - 2026-11-17\n")
    with pytest.raises(ChangelogError):
        read_wave(tmp_path)


def test_pending_fragment_blocks_production(tmp_path):
    pending = tmp_path / ".changes/unreleased"
    pending.mkdir(parents=True)
    (pending / ".gitkeep").touch()
    require_batched(tmp_path)
    (pending / "fix.yaml").write_text("kind: Fixed\nbody: A fix\n")
    with pytest.raises(ChangelogError, match="unreleased"):
        require_batched(tmp_path)


def test_generated_changelog_cannot_drift(tmp_path):
    shutil.copytree(ROOT / ".changes", tmp_path / ".changes")
    shutil.copy(ROOT / ".changie.yaml", tmp_path)
    shutil.copy(ROOT / "CHANGELOG.md", tmp_path)
    check_changelog(tmp_path)
    with (tmp_path / "CHANGELOG.md").open("a") as stream:
        stream.write("\nUnreviewed hand edit\n")
    with pytest.raises(ChangelogError, match="generated"):
        check_changelog(tmp_path)


def test_batch_preserves_independent_versions_and_records_inventory(tmp_path):
    batch_fixture(tmp_path)
    paths = {
        "libs/core/pyproject.toml": '[project]\nname="genblaze-core"\nversion="0.3.10"\n',
        "cli/pyproject.toml": '[project]\nname="genblaze-cli"\nversion="0.3.8"\n',
        "libs/spec/package.json": '{"name":"@genblaze/spec","version":"0.4.1"}',
    }
    for name, content in paths.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    batch_notes(tmp_path, "1.2.3")
    require_batched(tmp_path)
    notes = (tmp_path / ".changes/v1.2.3.md").read_text()
    assert "`genblaze-core` **0.3.10**" in notes
    assert "`genblaze-cli` **0.3.8**" in notes
    assert "`@genblaze/spec` **0.4.1**" in notes
    for name, content in paths.items():
        assert (tmp_path / name).read_text() == content
    check_changelog(tmp_path)


@pytest.mark.parametrize("version", ["../../outside", "0.8.2", "v01.2.3", "v1.2.3;touch bad"])
def test_batch_rejects_invalid_or_existing_wave_without_consuming_notes(tmp_path, version):
    batch_fixture(tmp_path)
    before = sorted(p.name for p in (tmp_path / ".changes/unreleased").glob("*.yaml"))
    with pytest.raises(ChangelogError):
        batch_notes(tmp_path, version)
    assert sorted(p.name for p in (tmp_path / ".changes/unreleased").glob("*.yaml")) == before


@pytest.mark.parametrize("version", ["0.8.1", "v0.7.9"])
def test_batch_rejects_older_wave(tmp_path, version):
    batch_fixture(tmp_path)
    with pytest.raises(ChangelogError, match="newer"):
        batch_notes(tmp_path, version)
