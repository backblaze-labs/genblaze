"""genblaze-core — orchestration framework for media generation."""

from genblaze_core._version import __version__

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # pipeline (primary entry point)
    "Pipeline": ("genblaze_core.pipeline.pipeline", "Pipeline"),
    "PipelineResult": ("genblaze_core.pipeline.result", "PipelineResult"),
    "StepCompleteEvent": ("genblaze_core.pipeline.result", "StepCompleteEvent"),
    # pipeline cache
    "StepCache": ("genblaze_core.pipeline.cache", "StepCache"),
    # pipeline templates
    "PipelineTemplate": ("genblaze_core.pipeline.template", "PipelineTemplate"),
    "StepTemplate": ("genblaze_core.pipeline.template", "StepTemplate"),
    # moderation
    "ModerationHook": ("genblaze_core.pipeline.moderation", "ModerationHook"),
    "ModerationResult": ("genblaze_core.pipeline.moderation", "ModerationResult"),
    # builders
    "RunBuilder": ("genblaze_core.builders.run_builder", "RunBuilder"),
    "StepBuilder": ("genblaze_core.builders.step_builder", "StepBuilder"),
    # providers
    "BaseProvider": ("genblaze_core.providers.base", "BaseProvider"),
    "ProviderCapabilities": ("genblaze_core.providers.base", "ProviderCapabilities"),
    "SubmitResult": ("genblaze_core.providers.base", "SubmitResult"),
    "SyncProvider": ("genblaze_core.providers.base", "SyncProvider"),
    "validate_asset_url": ("genblaze_core.providers.base", "validate_asset_url"),
    "validate_chain_input_url": ("genblaze_core.providers.base", "validate_chain_input_url"),
    "FFmpegCompositor": ("genblaze_core.providers.compositor", "FFmpegCompositor"),
    "FFmpegTransform": ("genblaze_core.providers.transform", "FFmpegTransform"),
    "ProgressEvent": ("genblaze_core.providers.progress", "ProgressEvent"),
    # observability
    "StreamEvent": ("genblaze_core.observability.events", "StreamEvent"),
    "Tracer": ("genblaze_core.observability.tracer", "Tracer"),
    "NoOpTracer": ("genblaze_core.observability.tracer", "NoOpTracer"),
    "LoggingTracer": ("genblaze_core.observability.tracer", "LoggingTracer"),
    "OTelTracer": ("genblaze_core.observability.tracer", "OTelTracer"),
    "CompositeTracer": ("genblaze_core.observability.tracer", "CompositeTracer"),
    "StructuredLogger": ("genblaze_core.observability.logger", "StructuredLogger"),
    # agents
    "AgentLoop": ("genblaze_core.agents.loop", "AgentLoop"),
    "AgentContext": ("genblaze_core.agents.loop", "AgentContext"),
    "AgentIteration": ("genblaze_core.agents.loop", "AgentIteration"),
    "AgentResult": ("genblaze_core.agents.loop", "AgentResult"),
    "Evaluator": ("genblaze_core.agents.evaluator", "Evaluator"),
    "EvaluationResult": ("genblaze_core.agents.evaluator", "EvaluationResult"),
    "CallableEvaluator": ("genblaze_core.agents.evaluator", "CallableEvaluator"),
    "ThresholdEvaluator": ("genblaze_core.agents.evaluator", "ThresholdEvaluator"),
    # models
    "Manifest": ("genblaze_core.models.manifest", "Manifest"),
    "Run": ("genblaze_core.models.run", "Run"),
    "Step": ("genblaze_core.models.step", "Step"),
    "Asset": ("genblaze_core.models.asset", "Asset"),
    "AudioMetadata": ("genblaze_core.models.asset", "AudioMetadata"),
    "Track": ("genblaze_core.models.asset", "Track"),
    "VideoMetadata": ("genblaze_core.models.asset", "VideoMetadata"),
    "WordTiming": ("genblaze_core.models.asset", "WordTiming"),
    # enums
    "Modality": ("genblaze_core.models.enums", "Modality"),
    "StepType": ("genblaze_core.models.enums", "StepType"),
    "RunStatus": ("genblaze_core.models.enums", "RunStatus"),
    "StepStatus": ("genblaze_core.models.enums", "StepStatus"),
    "PromptVisibility": ("genblaze_core.models.enums", "PromptVisibility"),
    "ProviderErrorCode": ("genblaze_core.models.enums", "ProviderErrorCode"),
    "EmbedPolicy": ("genblaze_core.models.policy", "EmbedPolicy"),
    "PromptTemplate": ("genblaze_core.models.prompt_template", "PromptTemplate"),
    # provider discovery
    "discover_providers": ("genblaze_core.providers.registry", "discover_providers"),
    # sinks
    "BaseSink": ("genblaze_core.sinks.base", "BaseSink"),
    "ParquetSink": ("genblaze_core.sinks.parquet", "ParquetSink"),
    # storage
    "StorageBackend": ("genblaze_core.storage.base", "StorageBackend"),
    "ObjectStorageSink": ("genblaze_core.storage.sink", "ObjectStorageSink"),
    "AssetTransfer": ("genblaze_core.storage.transfer", "AssetTransfer"),
    "KeyStrategy": ("genblaze_core.storage.base", "KeyStrategy"),
    # testing
    "MockProvider": ("genblaze_core.testing", "MockProvider"),
    "MockVideoProvider": ("genblaze_core.testing", "MockVideoProvider"),
    "MockAudioProvider": ("genblaze_core.testing", "MockAudioProvider"),
    "ProviderComplianceTests": ("genblaze_core.testing", "ProviderComplianceTests"),
    # exceptions
    "GenblazeError": ("genblaze_core.exceptions", "GenblazeError"),
    "PipelineTimeoutError": ("genblaze_core.exceptions", "PipelineTimeoutError"),
    "EmbeddingError": ("genblaze_core.exceptions", "EmbeddingError"),
    "StorageError": ("genblaze_core.exceptions", "StorageError"),
    "ProviderError": ("genblaze_core.exceptions", "ProviderError"),
    "ManifestError": ("genblaze_core.exceptions", "ManifestError"),
    "SinkError": ("genblaze_core.exceptions", "SinkError"),
    "WebhookError": ("genblaze_core.exceptions", "WebhookError"),
    # webhooks
    "WebhookNotifier": ("genblaze_core.webhooks.notifier", "WebhookNotifier"),
    "WebhookConfig": ("genblaze_core.webhooks.notifier", "WebhookConfig"),
    "WebhookEvent": ("genblaze_core.webhooks.notifier", "WebhookEvent"),
    "WebhookSink": ("genblaze_core.webhooks.sink", "WebhookSink"),
}

__all__ = [*_LAZY_IMPORTS.keys(), "__version__"]


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr = _LAZY_IMPORTS[name]
        import importlib

        mod = importlib.import_module(module_path)
        val = getattr(mod, attr)
        globals()[name] = val
        return val
    raise AttributeError(f"module 'genblaze_core' has no attribute {name!r}")
