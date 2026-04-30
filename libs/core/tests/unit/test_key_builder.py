"""Unit tests for ``genblaze_core.storage.key_builder.KeyBuilder``.

Covers four contract dimensions:

* **Normalization** — ``from_prefix`` strips leading/trailing slashes and
  collapses consecutive separators; result is hashable and frozen.
* **Seam-only dedupe** — ``build`` and ``append`` drop a single duplicate
  at the prefix↔args seam; never within the prefix or args.
* **Idempotency** — running ``from_prefix`` on already-normalized input
  returns an equal object; ``build`` is a pure function of inputs.
* **Bug #5 regression** — explicit case showing ``prefix="runs"`` +
  ``"runs"`` no longer doubles.
"""

from __future__ import annotations

import dataclasses

import pytest
from genblaze_core.storage.key_builder import KeyBuilder

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("/", ""),
        ("a", "a"),
        ("/a", "a"),
        ("a/", "a"),
        ("/a/", "a"),
        ("a/b", "a/b"),
        ("a//b", "a/b"),
        ("//a//b//", "a/b"),
        ("a/b/c", "a/b/c"),
        # Intentional duplicate within the prefix is preserved.
        ("archive/archive/2026", "archive/archive/2026"),
    ],
)
def test_from_prefix_normalizes(raw: str, expected: str) -> None:
    assert KeyBuilder.from_prefix(raw).prefix == expected


def test_normalization_is_idempotent() -> None:
    once = KeyBuilder.from_prefix("/a/b/")
    twice = KeyBuilder.from_prefix(once.prefix)
    assert once == twice


def test_frozen_dataclass_blocks_mutation() -> None:
    kb = KeyBuilder.from_prefix("a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        kb.prefix = "b"  # type: ignore[misc]


def test_hashable_for_caching() -> None:
    a = KeyBuilder.from_prefix("a/b")
    b = KeyBuilder.from_prefix("/a/b/")  # equivalent after normalization
    c = KeyBuilder.from_prefix("a/c")
    assert hash(a) == hash(b)
    assert hash(a) != hash(c)


# ---------------------------------------------------------------------------
# build() — terminal key generation
# ---------------------------------------------------------------------------


def test_build_simple() -> None:
    kb = KeyBuilder.from_prefix("genblaze")
    assert kb.build("manifests", "run-1.json") == "genblaze/manifests/run-1.json"


def test_build_strips_inner_slashes() -> None:
    kb = KeyBuilder.from_prefix("genblaze")
    assert kb.build("/manifests/", "/run-1.json/") == "genblaze/manifests/run-1.json"


def test_build_handles_empty_prefix() -> None:
    kb = KeyBuilder.from_prefix("")
    assert kb.build("a", "b") == "a/b"


def test_build_handles_no_args() -> None:
    kb = KeyBuilder.from_prefix("genblaze/runs")
    assert kb.build() == "genblaze/runs"


def test_build_drops_empty_segments() -> None:
    """Conditional inputs like ``builder.build(tenant_id or "")`` must not
    produce empty path segments."""
    kb = KeyBuilder.from_prefix("genblaze")
    assert kb.build("", "manifests", "", "run-1.json") == "genblaze/manifests/run-1.json"


# ---------------------------------------------------------------------------
# Seam-only dedupe
# ---------------------------------------------------------------------------


def test_seam_dedupe_collapses_one_duplicate() -> None:
    """Bug #5: prefix='runs' + strategy-prepended 'runs/' must collapse."""
    kb = KeyBuilder.from_prefix("runs")
    assert kb.build("runs", "tenant-x", "2026-04-29", "run-id", "manifest.json") == (
        "runs/tenant-x/2026-04-29/run-id/manifest.json"
    )


def test_seam_dedupe_only_drops_one_segment() -> None:
    """If args repeat the seam segment twice, only the seam-adjacent one drops."""
    kb = KeyBuilder.from_prefix("runs")
    # First "runs" matches seam → dropped. Second "runs" is preserved.
    assert kb.build("runs", "runs", "tail") == "runs/runs/tail"


def test_intentional_duplicate_within_prefix_preserved() -> None:
    """Caller deliberately doubled a segment inside their prefix — keep it."""
    kb = KeyBuilder.from_prefix("archive/archive/2026")
    assert kb.build("data", "x") == "archive/archive/2026/data/x"


def test_intentional_duplicate_within_args_preserved() -> None:
    """Caller deliberately doubled a segment inside the args — keep it."""
    kb = KeyBuilder.from_prefix("genblaze")
    assert kb.build("audit", "audit", "2026") == "genblaze/audit/audit/2026"


def test_no_dedupe_when_seam_segments_differ() -> None:
    kb = KeyBuilder.from_prefix("runs")
    assert kb.build("manifests", "run-1.json") == "runs/manifests/run-1.json"


def test_seam_dedupe_with_multi_segment_first_arg() -> None:
    """First arg may contain multiple slashed parts; seam check uses only its
    first sub-segment."""
    kb = KeyBuilder.from_prefix("runs")
    # First sub-segment of args = "runs" → matches seam → dropped.
    # Remaining "tenant" survives.
    assert kb.build("runs/tenant", "data") == "runs/tenant/data"


# ---------------------------------------------------------------------------
# append() — for chaining
# ---------------------------------------------------------------------------


def test_append_returns_new_instance() -> None:
    a = KeyBuilder.from_prefix("a")
    b = a.append("b")
    assert a.prefix == "a"  # unchanged
    assert b.prefix == "a/b"
    assert a is not b


def test_append_seam_dedupe() -> None:
    """Same dedupe rule as build — last-of-prefix vs first-of-args."""
    kb = KeyBuilder.from_prefix("genblaze").append("runs")
    assert kb.prefix == "genblaze/runs"
    # Now appending another "runs" hits the seam.
    assert kb.append("runs", "tail").prefix == "genblaze/runs/tail"


def test_append_then_build_equivalent_to_build_combined() -> None:
    """append+build composition matches a single build call."""
    kb = KeyBuilder.from_prefix("p")
    a = kb.append("a").build("b", "c")
    b = kb.build("a", "b", "c")
    assert a == b


# ---------------------------------------------------------------------------
# Bug #5 — explicit regression
# ---------------------------------------------------------------------------


def test_bug_5_prefix_runs_no_longer_doubles() -> None:
    """``prefix="runs"`` under HIERARCHICAL no longer produces ``runs/runs/...``.

    Direct unit-level guard against the regression that motivated this
    primitive. Higher-level tests in test_object_storage_sink.py assert
    the same behavior at the sink seam.
    """
    kb = KeyBuilder.from_prefix("runs")
    manifest_key = kb.build("runs", "tenant", "2026-04-29", "run-1", "manifest.json")
    asset_key = kb.append("runs").build("tenant", "2026-04-29", "run-1", "assets", "asset-1.png")
    assert manifest_key == "runs/tenant/2026-04-29/run-1/manifest.json"
    assert asset_key == "runs/tenant/2026-04-29/run-1/assets/asset-1.png"
    # Crucially: zero "runs/runs" anywhere.
    assert "runs/runs" not in manifest_key
    assert "runs/runs" not in asset_key
