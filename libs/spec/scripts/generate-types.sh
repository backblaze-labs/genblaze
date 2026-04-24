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
MANIFEST_SCHEMA_DIR="$REPO_ROOT/libs/spec/schemas/manifest/v1"
EVENT_SCHEMA_DIR="$REPO_ROOT/libs/spec/schemas/events/v1"
OUT_FILE="$REPO_ROOT/libs/spec/ts/genblaze.d.ts"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$(dirname "$OUT_FILE")"

# Shared CLI flags: --declareExternallyReferenced walks $refs so a single
# input file emits the full connected tree. --unknownAny prefers `unknown`
# over `any` for safer consumer code.
MANIFEST_FLAGS=(
  --cwd="$MANIFEST_SCHEMA_DIR"
  --declareExternallyReferenced
  --unknownAny
)

EVENT_FLAGS=(
  --cwd="$EVENT_SCHEMA_DIR"
  --declareExternallyReferenced
  --unknownAny
)

# Manifest pulls Run → Step → Asset via $ref. Emits the full tree.
npx --yes "json-schema-to-typescript@$JSTT_VERSION" \
  -i "$MANIFEST_SCHEMA_DIR/manifest.schema.json" \
  "${MANIFEST_FLAGS[@]}" \
  > "$TMP_DIR/manifest.ts"

# EmbedPolicy is standalone (not referenced by Manifest).
npx --yes "json-schema-to-typescript@$JSTT_VERSION" \
  -i "$MANIFEST_SCHEMA_DIR/policy.schema.json" \
  "${MANIFEST_FLAGS[@]}" \
  > "$TMP_DIR/policy.ts"

# StreamEvent is the discriminated-union root — pulls every variant via
# oneOf/$ref. One invocation emits the full event tree.
npx --yes "json-schema-to-typescript@$JSTT_VERSION" \
  -i "$EVENT_SCHEMA_DIR/stream-event.schema.json" \
  "${EVENT_FLAGS[@]}" \
  > "$TMP_DIR/events.ts"

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
 * genblaze TypeScript type definitions — manifest/v1 + events/v1
 *
 * AUTO-GENERATED from libs/spec/schemas/{manifest,events}/v1/*.schema.json.
 * DO NOT EDIT BY HAND. Regenerate with `make ts-types`.
 *
 * Source of truth: the JSON Schemas, which are enforced against the
 * Pydantic models by tests/unit/test_spec_conformance.py.
 */

EOF
  strip_banner "$TMP_DIR/manifest.ts"
  echo ""
  strip_banner "$TMP_DIR/policy.ts"
  echo ""
  strip_banner "$TMP_DIR/events.ts"
} > "$OUT_FILE"

echo "Generated $OUT_FILE"
