"""Changie is the source of wave notes, without controlling package versions."""

from __future__ import annotations

import shutil
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


def test_historical_pep440_post_release_survives_changie_merge():
    text = (ROOT / "CHANGELOG.md").read_text()
    assert "## [0.2.5.post1] - 2026-04-24" in text
    assert text.index("## [0.2.6]") < text.index("## [0.2.5.post1]") < text.index("## [0.2.5]")


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
