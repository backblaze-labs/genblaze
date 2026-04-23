#!/usr/bin/env bash
# Generate libs/spec/ts/genblaze.d.ts from the JSON Schemas.
#
# Deterministic output: rerunning with no schema changes must produce
# a byte-identical file. CI uses `make ts-types-check` (see Makefile) to
# fail PRs that change schemas without regenerating.
#
# Phase 1a: no package.json in the repo; npx fetches a pinned version.
set -euo pipefail

JSTT_VERSION="15.0.4"
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SCHEMA_DIR="$REPO_ROOT/libs/spec/schemas/manifest/v1"
OUT_FILE="$REPO_ROOT/libs/spec/ts/genblaze.d.ts"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$(dirname "$OUT_FILE")"

# Shared CLI flags: --declareExternallyReferenced walks $refs so a single
# input file (manifest) emits the full Run/Step/Asset tree. --unknownAny
# prefers `unknown` over `any` for safer consumer code.
JSTT_FLAGS=(
  --cwd="$SCHEMA_DIR"
  --declareExternallyReferenced
  --unknownAny
)

# Manifest pulls Run → Step → Asset via $ref. Emits the full tree.
npx --yes "json-schema-to-typescript@$JSTT_VERSION" \
  -i "$SCHEMA_DIR/manifest.schema.json" \
  "${JSTT_FLAGS[@]}" \
  > "$TMP_DIR/manifest.ts"

# EmbedPolicy is standalone (not referenced by Manifest).
npx --yes "json-schema-to-typescript@$JSTT_VERSION" \
  -i "$SCHEMA_DIR/policy.schema.json" \
  "${JSTT_FLAGS[@]}" \
  > "$TMP_DIR/policy.ts"

# Strip the per-file JSTT banner so the combined output has one unified
# banner. The banner JSTT emits is always:
#   /* eslint-disable */
#   /**
#    * This file was automatically generated ...
#    */
# followed by a blank line. Skip through the first ` */` that follows `/**`,
# then skip any leading blank lines.
strip_banner() {
  awk '
    /^\/\* eslint-disable \*\/$/ { skipping=1; next }
    skipping && /^\/\*\*$/ { in_banner=1; next }
    in_banner { if ($0 ~ /^ \*\/$/) { in_banner=0; skipping=0; past_banner=1 }; next }
    past_banner { print; next }
    { print }
  ' "$1" | sed '/./,$!d'
}

{
  cat <<'EOF'
/* eslint-disable */
/**
 * genblaze TypeScript type definitions — manifest/v1
 *
 * AUTO-GENERATED from libs/spec/schemas/manifest/v1/*.schema.json.
 * DO NOT EDIT BY HAND. Regenerate with `make ts-types`.
 *
 * Source of truth: the JSON Schemas, which are enforced against the
 * Pydantic models by tests/unit/test_spec_conformance.py.
 */

EOF
  strip_banner "$TMP_DIR/manifest.ts"
  echo ""
  strip_banner "$TMP_DIR/policy.ts"
} > "$OUT_FILE"

echo "Generated $OUT_FILE"
