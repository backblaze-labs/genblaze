<!-- last_verified: 2026-06-23 -->
# Webhooks

`WebhookNotifier` delivers fire-and-forget HTTP notifications for pipeline events via a background thread. Uses only stdlib (`http.client`, `ssl`, `threading`).

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

On every dispatch, `resolve_ssrf` resolves the hostname once, validates all returned IPs against private/loopback ranges (RFC 1918, link-local, IMDS 169.254.x.x), and returns the pinned IP. The connection is opened directly to that IP — the HTTP client never performs a second DNS resolution. This closes the DNS rebinding / TOCTOU window. Requests to private IPs raise `WebhookError`.

DNS pinning: the TLS connection uses the original hostname for SNI and certificate verification (`server_hostname`) while the TCP socket connects to the pinned IP, so both security properties are maintained.

HTTP redirects cannot occur: `http.client.HTTPSConnection` has no redirect handler, so a 3xx response is treated as a delivery failure.

**Egress proxy note:** DNS pinning requires a direct TCP connection to the validated IP. `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` env vars are ignored by design — routing through a proxy would re-introduce the DNS rebinding window. If your deployment mandates an egress proxy, configure the proxy's allowlist to pass through the webhook target instead.

## Canonical files

- `libs/core/genblaze_core/webhooks/notifier.py`
- `libs/core/genblaze_core/webhooks/sink.py`
