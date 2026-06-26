#!/usr/bin/env bash
#
# Pre-release wheel install smoke test.
#
# Builds every published package to a local wheelhouse, then installs
# every local genblaze wheel into a fresh venv while leaving PyPI enabled
# for transitive dependencies. Asserts every connector imports.
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

echo "==> Installing local genblaze wheels + PyPI transitive deps"
meta_wheels=( "${WHEELHOUSE}"/genblaze-[0-9]*-py3-none-any.whl )
if [[ ${#meta_wheels[@]} -ne 1 || ! -e "${meta_wheels[0]}" ]]; then
    echo "Expected exactly one local genblaze umbrella wheel, found ${#meta_wheels[@]}" >&2
    exit 1
fi

local_genblaze_wheels=( "${WHEELHOUSE}"/genblaze_*.whl )
if [[ ${#local_genblaze_wheels[@]} -eq 0 || ! -e "${local_genblaze_wheels[0]}" ]]; then
    echo "Expected local genblaze dependency wheels in ${WHEELHOUSE}" >&2
    exit 1
fi

meta_wheel_uri="$(
    python - "${meta_wheels[0]}" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve().as_uri())
PY
)"

# The direct file requirements force every genblaze distribution to come
# from the wheelhouse even when the same version already exists on PyPI.
# The default PyPI index remains enabled for third-party transitive deps.
pip install \
    --quiet \
    --find-links "${WHEELHOUSE}" \
    "genblaze[all] @ ${meta_wheel_uri}" \
    "${local_genblaze_wheels[@]}"

echo "==> Asserting every genblaze[all] module imports cleanly"
python "${REPO_ROOT}/tools/release_import_smoke.py"

echo "==> Release smoke test passed."
