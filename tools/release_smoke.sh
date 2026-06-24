#!/usr/bin/env bash
#
# Pre-release wheel install smoke test.
#
# Builds every published package to a local wheelhouse, then installs
# ``genblaze[all]`` into a fresh venv from that wheelhouse only
# (``--no-index --find-links``). Asserts every connector imports.
#
# Why this exists:
#   ``make install-dev`` and the CI matrix both use ``pip install -e``
#   which bypasses version constraints entirely. There is otherwise no
#   gate that proves a freshly built ``genblaze-core 0.3.0`` wheel
#   installs against ``genblaze-openai 0.3.0`` from PyPI metadata
#   constraints. This script catches incompatible pyproject pins
#   BEFORE tagging — the exact failure mode that broke 15+ files
#   before 0.3.0.
#
# Run this:
#   * locally before tagging a release
#   * as a CI job pre-tag (post-build, pre-publish)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WHEELHOUSE="${REPO_ROOT}/dist-smoke"
VENV="${REPO_ROOT}/.venv-smoke"

cleanup() {
    rm -rf "${WHEELHOUSE}" "${VENV}"
}
# Trap cleanup so a failure mid-script doesn't leave artifacts around.
# Comment out the trap to inspect wheelhouse/venv after a failure.
trap cleanup EXIT

echo "==> Cleaning previous smoke artifacts"
cleanup
mkdir -p "${WHEELHOUSE}"

# Every package to build, in publish order: core, then connectors,
# then meta (which references connector versions in extras).
PACKAGES=(
    "libs/core"
    "libs/connectors/replicate"
    "libs/connectors/s3"
    "libs/connectors/openai"
    "libs/connectors/google"
    "libs/connectors/runway"
    "libs/connectors/luma"
    "libs/connectors/decart"
    "libs/connectors/elevenlabs"
    "libs/connectors/stability-audio"
    "libs/connectors/lmnt"
    "libs/connectors/hume"
    "libs/connectors/gmicloud"
    "libs/connectors/langsmith"
    "libs/connectors/nvidia"
    "libs/connectors/assemblyai"
    "cli"
    "libs/meta"
)

echo "==> Building wheels for ${#PACKAGES[@]} packages → ${WHEELHOUSE}"
for pkg in "${PACKAGES[@]}"; do
    echo "  -> ${pkg}"
    (
        cd "${REPO_ROOT}/${pkg}"
        rm -rf dist
        python -m build --wheel --sdist --outdir "${WHEELHOUSE}" >/dev/null 2>&1
    )
done

echo "==> Running twine check on every wheel"
python -m twine check "${WHEELHOUSE}"/*

echo "==> Creating fresh venv"
python -m venv "${VENV}"
# shellcheck disable=SC1091
source "${VENV}/bin/activate"
pip install --quiet --upgrade pip

echo "==> Installing 'genblaze[all]' from wheelhouse + PyPI for transitive deps"
# ``--find-links`` makes pip prefer local wheels for genblaze-* packages;
# the default PyPI index supplies transitive deps (pillow, httpx, etc.)
# the same way it will for end users post-publish. ``--no-index`` would
# block transitives — wrong for this smoke test; we're verifying genblaze
# pyproject pins, not vendoring the entire dep tree.
pip install --quiet --find-links "${WHEELHOUSE}" "genblaze[all]"

echo "==> Asserting every connector imports cleanly"
python - <<'PY'
import importlib
import sys

connectors = [
    "genblaze_core",
    # Submodule with a hard import-at-load dependency (urllib3, via the
    # AssetTransfer PoolManager). `import genblaze_core` uses lazy __getattr__
    # dispatch and never executes this, so it must be imported explicitly to
    # prove the clean-install contract — the regression class behind #37/#106.
    # (genblaze_core.testing is NOT checked here: it needs pytest, which
    # genblaze[all] does not install — that extra is gated in the follow-up.)
    "genblaze_core.storage",
    "genblaze_s3",
    "genblaze_openai",
    "genblaze_google",
    "genblaze_runway",
    "genblaze_luma",
    "genblaze_decart",
    "genblaze_replicate",
    "genblaze_elevenlabs",
    "genblaze_stability_audio",
    "genblaze_lmnt",
    "genblaze_hume",
    "genblaze_gmicloud",
    "genblaze_langsmith",
    "genblaze_nvidia",
    "genblaze_assemblyai",
    "genblaze",
]
failures = []
for mod in connectors:
    try:
        importlib.import_module(mod)
        print(f"  ok: {mod}")
    except Exception as exc:  # noqa: BLE001 — surface all import errors
        failures.append((mod, exc))
        print(f"  FAIL: {mod}: {exc}")

if failures:
    sys.exit(f"{len(failures)} import failure(s)")
PY

echo "==> Release smoke test passed."
