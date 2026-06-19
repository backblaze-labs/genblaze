<!-- last_verified: 2026-06-17 -->
# Issue 65: Moderate Promptless Text Inputs

## Issue

- GitHub: https://github.com/backblaze-labs/genblaze/issues/65
- Title: ModerationHook.check_prompt skips user text carried in Step.inputs when prompt is None

## Plan

1. Add regression coverage for promptless text carried through `external_inputs=` and `input_from=` in sync and async pipeline runs.
2. Build the pre-step moderation payload from the explicit prompt plus recognized textual input assets.
3. Keep promptless steps without textual inputs runnable for compositor and transform workflows.
4. Update moderation docs to describe the covered prompt/input fields.

## Acceptance

- Promptless steps with textual `Step.inputs` are moderated before provider invocation.
- Rejected input-carried text fails the step with `ProviderErrorCode.INVALID_INPUT`.
- Promptless non-text steps still run without fake prompt strings.
- Documentation defines the moderation coverage for prompts and input assets.
