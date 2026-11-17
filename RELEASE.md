# Release process

The canonical maintainer guide is [RELEASING.md](RELEASING.md). This fleet
entry point preserves existing links and the detailed operating procedures.

- Add package-scoped Changie fragments with `make changelog-new`.
- Prepare independent package versions, then batch an explicitly selected wave
  with `make changelog-batch WAVE=X.Y.Z`; tag mapping stays `vX.Y.Z`.
- Close the wave milestone with no open issues or PRs, and sign the annotated
  tag with a reviewed key from `.github/release-signers` on protected main.
- Run `make pre-release`. Production retains all four pre-publish gates,
  PyPI/npm OIDC and provenance/attestations.
- Use `dry_run=true, validation_only=true` for a workflow validation without
  registry uploads. TestPyPI uploads require explicit `validation_only=false`.

The trigger decision remains open for human review: this change preserves
`release: published` and recommends a later, separately approved migration to
signed tag → publish → GitHub Release. See the guide for both options and the
conditions for a documented exception.
