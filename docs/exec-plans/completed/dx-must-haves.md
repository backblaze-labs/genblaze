<!-- completed: 2026-03-17 -->
# DX Must-Have Features

## Summary
Five features to close developer experience gaps: prompt templates, asset transforms, pipeline templates, moderation hooks, and webhook notifications.

## Features Implemented
1. **Prompt Templates** — `PromptTemplate` with `{variable}` placeholders; integrates with `batch_run()` for parameterized batch execution
2. **Asset Transforms** — `FFmpegTransform` SyncProvider with resize, crop, overlay_text, audio_normalize, and convert_format operations; shared ffmpeg utils with FFmpegCompositor
3. **Pipeline Templates** — `PipelineTemplate` / `StepTemplate` for serializable pipeline definitions (JSON); `Pipeline.to_template()` for export, `instantiate()` for reconstruction
4. **Moderation Hooks** — `ModerationHook` ABC with `check_prompt()` / `check_output()` pre/post-step content screening; audit trail in `step.metadata["moderation"]`
5. **Webhook Notifications** — `WebhookNotifier` with fire-and-forget background thread, retry on 5xx, event filtering, SSRF protection; `WebhookSink` for pipeline integration

## New Files
- `models/prompt_template.py`, `providers/_ffmpeg_utils.py`, `providers/transform.py`
- `pipeline/template.py`, `pipeline/moderation.py`
- `webhooks/notifier.py`, `webhooks/sink.py`
