<!-- last_verified: 2026-03-16 -->
# Queue Integration

Video generation takes 1–10 minutes. In web services, run pipelines from
background workers, not request handlers. Use the `on_submit` callback to
persist prediction IDs for crash recovery.

## Pattern: Checkpoint + Resume

```python
from genblaze_core import Pipeline, Modality
from genblaze_core.testing import MockVideoProvider

# Store prediction IDs for crash recovery
checkpoints: dict[str, Any] = {}

def checkpoint(step_id: str, prediction_id: Any) -> None:
    """Persist to Redis/DB so a worker restart can resume polling."""
    checkpoints[step_id] = prediction_id
    # In production: redis.set(f"genblaze:{step_id}", prediction_id)

result = (
    Pipeline("video-job")
    .step(MockVideoProvider(), model="sora-2", prompt="sunset", modality=Modality.VIDEO)
    .run(
        timeout=600,
        on_progress=lambda e: print(f"Progress: {e.status}"),
        on_submit=checkpoint,  # fires after submit(), before polling
    )
)
```

The `on_submit` callback receives `(step_id, prediction_id)` immediately after
the provider's `submit()` succeeds. If the process dies during polling, the
prediction ID is already persisted and can be used to resume.

## Pattern: Task Queue Worker

```python
# enqueue.py — web handler
import json
from your_queue import enqueue

def handle_request(prompt: str) -> str:
    job_id = str(uuid.uuid4())
    enqueue("generate_video", {"job_id": job_id, "prompt": prompt})
    return job_id

# worker.py — background worker
from genblaze_core import Pipeline, Modality
from genblaze_openai import SoraProvider

def generate_video(payload: dict) -> None:
    result = (
        Pipeline(payload["job_id"])
        .step(SoraProvider(), model="sora-2", prompt=payload["prompt"], modality=Modality.VIDEO)
        .run(
            timeout=600,
            pipeline_timeout=900,
            on_submit=lambda sid, pid: save_checkpoint(payload["job_id"], sid, pid),
            on_step_complete=lambda e: update_job_status(payload["job_id"], e),
        )
    )
    save_result(payload["job_id"], result)
```

## Inputs / Outputs

| Parameter | Type | Description |
|-----------|------|-------------|
| `on_submit` | `Callable[[str, Any], None]` | Receives `(step_id, prediction_id)` after submit |
| `on_step_complete` | `Callable[[StepCompleteEvent], None]` | Receives event after each step finishes |
| `on_progress` | `Callable[[ProgressEvent], None]` | Receives event during provider polling |
| `pipeline_timeout` | `float` | Wall-clock limit for entire pipeline |

## Verification
- `on_submit` fires in both sync (`invoke`) and async (`ainvoke`) paths
- Test: pass a tracking callback and verify it receives the prediction_id
