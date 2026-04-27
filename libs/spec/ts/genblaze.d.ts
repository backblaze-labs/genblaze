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

/**
 * A canonical, hash-verified generation manifest.
 */
export interface Manifest {
  /**
   * Schema version identifier.
   */
  schema_version: "1.0" | "1.1" | "1.2" | "1.3" | "1.4" | "1.5";
  run: Run;
  /**
   * SHA-256 hash of the canonical JSON representation.
   */
  canonical_hash: string;
  /**
   * URI pointing to the full manifest when using pointer mode.
   */
  manifest_uri?: string | null;
  /**
   * Encryption scheme used for manifest content.
   */
  encryption_scheme?: string | null;
  /**
   * Cryptographic signature of the manifest.
   */
  signature?: string | null;
  /**
   * Asset IDs that failed to transfer to storage. Transport-layer diagnostic, not part of the canonical hash.
   */
  transfer_failures?: string[];
}
/**
 * A collection of generation steps forming a single pipeline execution.
 */
export interface Run {
  run_id: string;
  /**
   * Optional tenant/org identifier for multi-tenant deployments.
   */
  tenant_id?: string | null;
  /**
   * Optional project identifier.
   */
  project_id?: string | null;
  /**
   * Human-readable name for this run.
   */
  name?: string | null;
  /**
   * Overall run status.
   */
  status?: "pending" | "running" | "completed" | "failed" | "cancelled";
  steps: Step[];
  /**
   * Parent run ID for replay/fork lineage.
   */
  parent_run_id?: string | null;
  /**
   * Client-provided key for deduplication.
   */
  idempotency_key?: string | null;
  created_at: string;
  /**
   * Execution start timestamp.
   */
  started_at?: string | null;
  /**
   * Execution completion timestamp.
   */
  completed_at?: string | null;
  metadata?: {
    [k: string]: unknown;
  };
}
/**
 * A single generation step within a run.
 */
export interface Step {
  step_id: string;
  run_id?: string | null;
  /**
   * Provider name (e.g. replicate, runway, elevenlabs).
   */
  provider: string;
  /**
   * Model identifier (e.g. black-forest-labs/flux-schnell).
   */
  model: string;
  /**
   * Type of operation performed in this step.
   */
  step_type?: "generate" | "upscale" | "transcode" | "mix" | "edit" | "custom";
  /**
   * Specific model version identifier.
   */
  model_version?: string | null;
  /**
   * Hash of the model weights used.
   */
  model_hash?: string | null;
  /**
   * Output modality.
   */
  modality?: "image" | "video" | "audio" | "text";
  /**
   * Generation prompt.
   */
  prompt?: string | null;
  negative_prompt?: string | null;
  prompt_visibility?: "public" | "private" | "redacted" | "encrypted";
  /**
   * Random seed used for generation.
   */
  seed?: number | null;
  /**
   * Provider-specific generation parameters.
   */
  params?: {
    [k: string]: unknown;
  };
  status: "pending" | "submitted" | "processing" | "succeeded" | "failed" | "cancelled";
  /**
   * Input assets for this step.
   */
  inputs?: Asset[];
  assets?: Asset[];
  /**
   * Raw provider response data, namespaced per provider.
   */
  provider_payload?: {
    [k: string]: unknown;
  };
  /**
   * Number of retries attempted.
   */
  retries?: number;
  /**
   * Estimated cost in USD for this step.
   */
  cost_usd?: number | null;
  error?: string | null;
  /**
   * Normalized error code.
   */
  error_code?:
    | "timeout"
    | "rate_limit"
    | "auth_failure"
    | "invalid_input"
    | "model_error"
    | "server_error"
    | "content_policy"
    | "unknown"
    | null;
  started_at?: string | null;
  completed_at?: string | null;
  /**
   * Position of this step within the run (0-based).
   */
  step_index?: number | null;
  metadata?: {
    [k: string]: unknown;
  };
}
/**
 * A generated media asset within a step.
 */
export interface Asset {
  /**
   * Unique identifier for this asset.
   */
  asset_id: string;
  /**
   * Durable, credential-free URL of the asset. After storage-sink upload, this is the backend's durable URL — never a presigned URL. There is no separate storage-key field; parse the key from this URL if needed.
   */
  url: string;
  /**
   * MIME type of the asset (e.g. image/png, video/mp4).
   */
  media_type: string;
  /**
   * SHA-256 hash of the asset content.
   */
  sha256?: string | null;
  /**
   * File size in bytes.
   */
  size_bytes?: number | null;
  /**
   * Width in pixels (for image/video assets).
   */
  width?: number | null;
  /**
   * Height in pixels (for image/video assets).
   */
  height?: number | null;
  /**
   * Duration in seconds (for audio/video assets).
   */
  duration?: number | null;
  video?: null | {
    frame_rate?: number | null;
    codec?: string | null;
    bitrate?: number | null;
    color_space?: string | null;
    has_audio?: boolean | null;
    resolution?: string | null;
  };
  audio?: null | {
    sample_rate?: number | null;
    channels?: number | null;
    codec?: string | null;
    bitrate?: number | null;
    word_timings?:
      | null
      | {
          /**
           * The spoken word or token.
           */
          word: string;
          /**
           * Start time in seconds.
           */
          start: number;
          /**
           * End time in seconds.
           */
          end: number;
          /**
           * Recognition confidence 0-1.
           */
          confidence?: number | null;
        }[];
  };
  /**
   * Media tracks in this container asset.
   */
  tracks?:
    | {
        /**
         * Track type: 'video', 'audio', 'subtitle'.
         */
        kind: string;
        /**
         * Track codec (e.g. 'h264', 'aac').
         */
        codec?: string | null;
        /**
         * Human-readable label (e.g. 'generated-audio').
         */
        label?: string | null;
      }[]
    | null;
  /**
   * Arbitrary key-value metadata.
   */
  metadata?: {
    [k: string]: unknown;
  };
}

/**
 * Policy controlling how manifest data is embedded into media files.
 */
export interface EmbedPolicy {
  /**
   * Controls whether prompts are included in embedded manifests. Mirrors PromptVisibility.
   */
  prompt_visibility?: "public" | "private" | "redacted" | "encrypted";
  /**
   * full=embed entire manifest, pointer=embed only URI+hash, none=no embedding.
   */
  embed_mode?: "full" | "pointer" | "none";
  /**
   * Whether to include generation parameters in embedded manifest.
   */
  include_params?: boolean;
  /**
   * Whether to include the random seed in embedded manifest.
   */
  include_seed?: boolean;
}

/**
 * A discriminated union of every pipeline or agent-loop streaming event. The `type` field selects the variant. In-process Python objects (step, result) are not part of the wire contract — derived fields (step_status, manifest_hash, run_status) carry the equivalent information.
 */
export type StreamEvent =
  | PipelineStartedEvent
  | PipelineCompletedEvent
  | PipelineFailedEvent
  | StepQueuedEvent
  | StepStartedEvent
  | StepProgressEvent
  | StepRetriedEvent
  | StepCompletedEvent
  | StepFailedEvent
  | AgentIterationStartedEvent
  | AgentIterationEvaluatedEvent
  | AgentCompletedEvent;

/**
 * Emitted once at the start of a pipeline run.
 */
export interface PipelineStartedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "pipeline.started";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Run identifier this event belongs to.
   */
  run_id: string;
  /**
   * Total number of steps in the pipeline.
   */
  total_steps: number;
  /**
   * Pipeline name, if named.
   */
  message?: string | null;
}
/**
 * Emitted once at the end of a successful pipeline run.
 */
export interface PipelineCompletedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "pipeline.completed";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Run identifier this event belongs to.
   */
  run_id: string;
  /**
   * Terminal run status (e.g. `completed`).
   */
  run_status?: string | null;
  /**
   * Canonical SHA-256 hash of the run's manifest.
   */
  manifest_hash?: string | null;
}
/**
 * Emitted once when a pipeline run terminates in failure.
 */
export interface PipelineFailedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "pipeline.failed";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Run identifier this event belongs to.
   */
  run_id: string;
  /**
   * Short human-readable failure summary.
   */
  message?: string | null;
  /**
   * Terminal run status (`failed` or similar).
   */
  run_status?: string | null;
  /**
   * Canonical hash of the partial manifest, if one was produced.
   */
  manifest_hash?: string | null;
}
/**
 * Emitted when a step is waiting on capacity (serial pipeline or concurrency-limit), not yet running. Additive — fires alongside the existing step.started flow rather than replacing it.
 */
export interface StepQueuedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "step.queued";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Run identifier this event belongs to.
   */
  run_id: string;
  /**
   * Step identifier (UUID).
   */
  step_id: string;
  /**
   * 0-based step position in the pipeline.
   */
  step_index: number;
  /**
   * Total number of steps in the pipeline.
   */
  total_steps: number;
  /**
   * Provider name.
   */
  provider: string;
  /**
   * Model slug.
   */
  model: string;
  /**
   * Why the step is queued.
   */
  reason: "serial" | "concurrency_limit";
}
/**
 * Emitted when a step transitions from queued to running.
 */
export interface StepStartedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "step.started";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Run identifier this event belongs to.
   */
  run_id: string;
  /**
   * Step identifier (UUID).
   */
  step_id: string;
  /**
   * 0-based step position in the pipeline.
   */
  step_index: number;
  /**
   * Total number of steps in the pipeline.
   */
  total_steps: number;
  /**
   * Provider name (e.g. `gmicloud`).
   */
  provider: string;
  /**
   * Model slug passed to the provider.
   */
  model: string;
  /**
   * Caller-supplied ETA hint (seconds) for this step, set via Pipeline.step(expected_duration_sec=...). Informational only; the SDK does not synthesize this.
   */
  expected_duration_sec?: number | null;
}
/**
 * Emitted on provider progress ticks (may fire many times per step).
 */
export interface StepProgressEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "step.progress";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Run identifier if available.
   */
  run_id?: string | null;
  /**
   * Step identifier (UUID).
   */
  step_id: string;
  /**
   * Provider name.
   */
  provider: string;
  /**
   * Model slug.
   */
  model: string;
  /**
   * Upstream provider's prediction/job id, populated as soon as submit() returns.
   */
  request_id?: string | null;
  /**
   * Progress ratio in [0.0, 1.0], if the provider reports one.
   */
  progress_pct?: number | null;
  /**
   * Wall-clock seconds since step submission.
   */
  elapsed_sec?: number | null;
  /**
   * Ephemeral preview URL, if the provider emits one.
   */
  preview_url?: string | null;
  /**
   * Optional provider-supplied note.
   */
  message?: string | null;
  /**
   * True for keepalive ticks emitted between long-poll intervals; tracers and dashboards may safely filter these out.
   */
  is_heartbeat?: boolean;
  /**
   * Provider-specific extra fields (e.g. polling status).
   */
  data?: {
    [k: string]: unknown;
  };
}
/**
 * Emitted when a transient phase failure triggers a retry. Fires once per retry attempt (not per final failure).
 */
export interface StepRetriedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "step.retried";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Run identifier if available.
   */
  run_id?: string | null;
  /**
   * Step identifier (UUID).
   */
  step_id: string;
  /**
   * Provider name.
   */
  provider: string;
  /**
   * Model slug.
   */
  model: string;
  /**
   * Which lifecycle phase is being retried.
   */
  phase: "submit" | "poll" | "fetch";
  /**
   * 1-based attempt counter that just failed.
   */
  attempt: number;
  /**
   * Total attempts permitted for this phase.
   */
  max_attempts: number;
  /**
   * Seconds the retry helper will sleep before the next attempt.
   */
  delay_sec: number;
  /**
   * Normalized ProviderErrorCode that triggered the retry.
   */
  error_code?: string | null;
  /**
   * Sanitized failure message, if available.
   */
  error?: string | null;
}
/**
 * Emitted when a step finishes successfully.
 */
export interface StepCompletedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "step.completed";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Run identifier if available.
   */
  run_id?: string | null;
  /**
   * Step identifier (UUID).
   */
  step_id: string;
  /**
   * 0-based step position in the pipeline.
   */
  step_index: number;
  /**
   * Total number of steps in the pipeline.
   */
  total_steps: number;
  /**
   * Provider name.
   */
  provider: string;
  /**
   * Model slug.
   */
  model: string;
  /**
   * Upstream provider's prediction/job id, mirrored from step.metadata['upstream_id'].
   */
  request_id?: string | null;
  /**
   * Wall-clock seconds from step start to completion.
   */
  elapsed_sec: number;
  /**
   * Terminal status string (usually `succeeded`).
   */
  step_status?: string | null;
}
/**
 * Emitted when a step terminates in failure.
 */
export interface StepFailedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "step.failed";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Run identifier if available.
   */
  run_id?: string | null;
  /**
   * Step identifier (UUID).
   */
  step_id: string;
  /**
   * 0-based step position in the pipeline.
   */
  step_index: number;
  /**
   * Total number of steps in the pipeline.
   */
  total_steps: number;
  /**
   * Provider name.
   */
  provider: string;
  /**
   * Model slug.
   */
  model: string;
  /**
   * Upstream provider's prediction/job id if submit completed before failure.
   */
  request_id?: string | null;
  /**
   * Wall-clock seconds from step start to failure.
   */
  elapsed_sec: number;
  /**
   * Failure reason surfaced from the step.
   */
  error?: string | null;
  /**
   * Terminal status string (usually `failed`).
   */
  step_status?: string | null;
}
/**
 * Emitted at the start of each agent-loop iteration.
 */
export interface AgentIterationStartedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "agent.iteration.started";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * 0-based iteration counter.
   */
  iteration: number;
  /**
   * Maximum iterations configured for this loop.
   */
  total: number;
  /**
   * Feedback from the previous iteration, if any.
   */
  message?: string | null;
}
/**
 * Emitted after an iteration's result has been evaluated.
 */
export interface AgentIterationEvaluatedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "agent.iteration.evaluated";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * 0-based iteration counter this evaluation covers.
   */
  iteration: number;
  /**
   * Whether the evaluation's pass criterion was met.
   */
  passed: boolean;
  /**
   * Evaluator score (usually in [0, 1] — evaluator-defined). May be absent when the evaluator only returns pass/fail.
   */
  score?: number | null;
  /**
   * Free-text evaluator feedback.
   */
  feedback?: string | null;
}
/**
 * Emitted once when the agent loop terminates (pass, fail, or max-iters).
 */
export interface AgentCompletedEvent {
  /**
   * Discriminator tag identifying the event variant.
   */
  type: "agent.completed";
  /**
   * When this event was created (UTC).
   */
  timestamp: string;
  /**
   * Whether the final iteration's evaluation passed.
   */
  passed: boolean;
  /**
   * Total number of iterations executed.
   */
  iterations: number;
  /**
   * Summed cost across all iterations, if tracked.
   */
  total_cost_usd?: number | null;
}
