<!-- last_verified: 2026-03-17 -->
# Webhooks

`WebhookNotifier` delivers fire-and-forget HTTP notifications for pipeline events via a background thread. Uses only stdlib (`urllib.request`).

## Usage

```python
from genblaze_core import WebhookNotifier, WebhookConfig, Pipeline, Modality

notifier = WebhookNotifier(WebhookConfig(
    url="https://hooks.example.com/gen",
    headers={"Authorization": "Bearer tok123"},
))

result = (
    Pipeline("webhook-demo")
    .step(provider, model="m", prompt="hello", modality=Modality.IMAGE)
    .run(
        on_progress=notifier.make_on_progress(),
        on_step_complete=notifier.make_on_step_complete(),
    )
)
notifier.notify_pipeline_completed(result)
notifier.close()
```

## Events

| Event | Trigger |
|---|---|
| `pipeline.started` | `notify_pipeline_started()` called manually |
| `step.started` | First "submitted" status in `on_progress` |
| `step.completed` | Step succeeded in `on_step_complete` |
| `step.failed` | Step failed in `on_step_complete` |
| `pipeline.completed` | `notify_pipeline_completed()` with COMPLETED status |
| `pipeline.failed` | `notify_pipeline_completed()` with FAILED status |

## JSON payload

```json
{
  "event": "step.completed",
  "step_id": "uuid",
  "provider": "openai",
  "model": "sora-2",
  "status": "succeeded",
  "elapsed_sec": 12.5,
  "timestamp": "2026-03-17T12:00:00+00:00"
}
```

## WebhookSink

For completion-only notifications without wiring callbacks:

```python
from genblaze_core import WebhookSink, WebhookConfig

sink = WebhookSink(WebhookConfig(url="https://hooks.example.com/gen"))
Pipeline("demo").step(...).run(sink=sink)
```

## Configuration

- `url` — target POST endpoint (HTTPS only)
- `headers` — extra HTTP headers (auth, etc.)
- `timeout` — HTTP request timeout (default 10s)
- `max_retries` — retries on 5xx errors (default 2). 4xx errors are not retried.
- `include_events` — optional set to filter events (e.g. `{"pipeline.completed"}`)

## Security

`WebhookConfig` validates the target URL at construction:
- Only `https://` URLs are accepted
- `localhost` is rejected

On first dispatch, the hostname is DNS-resolved and checked against private/loopback IP ranges (RFC 1918, link-local, IMDS 169.254.x.x). Requests to private IPs raise `WebhookError`. This prevents SSRF when webhook URLs come from user input.

## Canonical files

- `libs/core/genblaze_core/webhooks/notifier.py`
- `libs/core/genblaze_core/webhooks/sink.py`
