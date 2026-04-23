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
