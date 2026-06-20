"""Tests for ModerationHook pipeline integration."""

from __future__ import annotations

import asyncio

import pytest
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.models.step import Step
from genblaze_core.pipeline.moderation import ModerationHook, ModerationResult
from genblaze_core.pipeline.pipeline import Pipeline, _pre_moderation_payload
from genblaze_core.testing import MockProvider

# ---------------------------------------------------------------------------
# Test hooks
# ---------------------------------------------------------------------------


class AlwaysRejectHook(ModerationHook):
    """Rejects all prompts."""

    def check_prompt(self, prompt, params):
        return ModerationResult(
            allowed=False,
            reason="test rejection",
            flagged_categories=["test"],
        )

    def check_output(self, assets):
        return ModerationResult(allowed=True)


class AlwaysAllowHook(ModerationHook):
    """Allows everything."""

    def check_prompt(self, prompt, params):
        return ModerationResult(allowed=True)

    def check_output(self, assets):
        return ModerationResult(allowed=True)


class OutputRejectHook(ModerationHook):
    """Allows prompts but rejects outputs."""

    def check_prompt(self, prompt, params):
        return ModerationResult(allowed=True)

    def check_output(self, assets):
        return ModerationResult(
            allowed=False,
            reason="output rejected",
            flagged_categories=["nsfw"],
        )


class BrokenHook(ModerationHook):
    """Raises an exception during check."""

    def check_prompt(self, prompt, params):
        raise RuntimeError("hook crashed")

    def check_output(self, assets):
        raise RuntimeError("hook crashed")


class TrackingHook(ModerationHook):
    """Records calls for assertion."""

    def __init__(self):
        self.prompt_calls: list[tuple[str | None, dict]] = []
        self.output_calls: list[list[Asset]] = []

    def check_prompt(self, prompt, params):
        self.prompt_calls.append((prompt, params))
        return ModerationResult(allowed=True)

    def check_output(self, assets):
        self.output_calls.append(assets)
        return ModerationResult(allowed=True)


class RejectContainingHook(TrackingHook):
    """Rejects prompts or input payloads containing a target string."""

    def __init__(self, needle: str):
        super().__init__()
        self.needle = needle

    def check_prompt(self, prompt, params):
        self.prompt_calls.append((prompt, params))
        if self.needle in (prompt or ""):
            return ModerationResult(
                allowed=False,
                reason="input text rejected",
                flagged_categories=["test"],
            )
        return ModerationResult(allowed=True)


# ---------------------------------------------------------------------------
# ModerationResult tests
# ---------------------------------------------------------------------------


class TestModerationResult:
    def test_defaults(self):
        result = ModerationResult(allowed=True)
        assert result.allowed is True
        assert result.reason is None
        assert result.flagged_categories == []

    def test_rejected_with_details(self):
        result = ModerationResult(
            allowed=False,
            reason="violence",
            flagged_categories=["violence", "gore"],
        )
        assert result.allowed is False
        assert result.reason == "violence"
        assert "violence" in result.flagged_categories


# ---------------------------------------------------------------------------
# Pre-step moderation tests
# ---------------------------------------------------------------------------


class TestPreStepModeration:
    def test_prompt_rejected_skips_generation(self):
        provider = MockProvider()
        result = (
            Pipeline("test", moderation=AlwaysRejectHook())
            .step(provider, model="m", prompt="bad prompt", modality=Modality.IMAGE)
            .run()
        )
        assert provider.call_count == 0
        assert result.run.steps[0].status == StepStatus.FAILED
        assert result.run.steps[0].error.startswith("Moderation rejected prompt/input:")

    def test_prompt_allowed_proceeds(self):
        provider = MockProvider()
        result = (
            Pipeline("test", moderation=AlwaysAllowHook())
            .step(provider, model="m", prompt="good prompt", modality=Modality.IMAGE)
            .run()
        )
        assert provider.call_count == 1
        assert result.run.steps[0].status == StepStatus.SUCCEEDED

    def test_prompt_rejection_sets_metadata(self):
        result = (
            Pipeline("test", moderation=AlwaysRejectHook())
            .step(MockProvider(), model="m", prompt="bad", modality=Modality.IMAGE)
            .run()
        )
        step = result.run.steps[0]
        assert step.metadata["moderation"]["stage"] == "pre"
        assert step.metadata["moderation"]["reason"] == "test rejection"
        assert "test" in step.metadata["moderation"]["flagged_categories"]
        assert step.error_code == ProviderErrorCode.INVALID_INPUT

    def test_no_moderation_runs_normally(self):
        """Pipeline without moderation param works as before."""
        provider = MockProvider()
        result = (
            Pipeline("test")
            .step(provider, model="m", prompt="hello", modality=Modality.IMAGE)
            .run()
        )
        assert provider.call_count == 1
        assert result.run.steps[0].status == StepStatus.SUCCEEDED

    def test_null_prompt_without_text_inputs_skips_moderation(self):
        """Promptless non-text steps skip pre-moderation (e.g. compositor)."""
        hook = TrackingHook()
        provider = MockProvider()
        result = (
            Pipeline("test", moderation=hook)
            .step(provider, model="m", prompt=None, modality=Modality.VIDEO)
            .run()
        )
        assert len(hook.prompt_calls) == 0  # check_prompt was NOT called
        assert provider.call_count == 1
        assert result.run.steps[0].status == StepStatus.SUCCEEDED

    def test_promptless_external_text_input_rejected_before_generation(self):
        hook = RejectContainingHook("blocked user text")
        provider = MockProvider()
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": "blocked user text"},
        )
        result = (
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt=None,
                modality=Modality.IMAGE,
                external_inputs=[text_asset],
            )
            .run()
        )

        step = result.run.steps[0]
        assert provider.call_count == 0
        assert step.status == StepStatus.FAILED
        assert step.error_code == ProviderErrorCode.INVALID_INPUT
        assert step.error.startswith("Moderation rejected prompt/input:")
        assert len(hook.prompt_calls) == 1
        assert hook.prompt_calls[0][0] == "blocked user text"

    def test_prompt_and_input_text_combined_into_single_payload(self):
        hook = TrackingHook()
        provider = MockProvider()
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": "input side"},
        )
        result = (
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt="prompt side",
                modality=Modality.IMAGE,
                external_inputs=[text_asset],
            )
            .run()
        )

        assert provider.call_count == 1
        assert result.run.steps[0].status == StepStatus.SUCCEEDED
        assert len(hook.prompt_calls) == 1
        assert hook.prompt_calls[0][0] == "prompt side\n\ninput side"

    def test_negative_prompt_combined_into_single_payload(self):
        hook = TrackingHook()
        provider = MockProvider()
        result = (
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt="prompt side",
                modality=Modality.IMAGE,
                negative_prompt="negative side",
            )
            .run()
        )

        assert provider.call_count == 1
        assert result.run.steps[0].status == StepStatus.SUCCEEDED
        assert len(hook.prompt_calls) == 1
        assert hook.prompt_calls[0][0] == "prompt side\n\nnegative side"

    def test_negative_prompt_rejected_before_generation(self):
        hook = RejectContainingHook("blocked negative text")
        provider = MockProvider()
        result = (
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt="safe prompt",
                modality=Modality.IMAGE,
                negative_prompt="blocked negative text",
            )
            .run()
        )

        step = result.run.steps[0]
        assert provider.call_count == 0
        assert step.status == StepStatus.FAILED
        assert step.error_code == ProviderErrorCode.INVALID_INPUT
        assert len(hook.prompt_calls) == 1
        assert hook.prompt_calls[0][0] == "safe prompt\n\nblocked negative text"

    def test_bytes_input_text_decoded_for_moderation(self):
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": b"blocked user text"},
        )
        step = Step(provider="mock", model="m", prompt=None, inputs=[text_asset])

        assert _pre_moderation_payload(step) == "blocked user text"

    def test_invalid_bytes_input_text_uses_replacement_char(self):
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": b"bad\xfftext"},
        )
        step = Step(provider="mock", model="m", prompt=None, inputs=[text_asset])

        assert _pre_moderation_payload(step) == "bad\ufffdtext"

    def test_empty_and_none_input_text_skip_moderation(self):
        empty_asset = Asset(
            url="https://input.test/empty.txt",
            media_type="text/plain",
            metadata={"text": ""},
        )
        none_asset = Asset(
            url="https://input.test/none.txt",
            media_type="text/plain",
            metadata={"text": None},
        )
        step = Step(provider="mock", model="m", prompt=None, inputs=[empty_asset, none_asset])

        assert _pre_moderation_payload(step) is None

    def test_whitespace_only_input_text_is_moderated(self):
        text_asset = Asset(
            url="https://input.test/whitespace.txt",
            media_type="text/plain",
            metadata={"text": "   "},
        )
        step = Step(provider="mock", model="m", prompt=None, inputs=[text_asset])

        assert _pre_moderation_payload(step) == "   "

    def test_multiple_text_inputs_combined_deterministically(self):
        first_asset = Asset(
            url="https://input.test/first.txt",
            media_type="text/plain",
            metadata={"text": "first"},
        )
        second_asset = Asset(
            url="https://input.test/second.txt",
            media_type="text/plain",
            metadata={"text": "second"},
        )
        step = Step(
            provider="mock",
            model="m",
            prompt="p",
            inputs=[first_asset, second_asset],
        )

        assert _pre_moderation_payload(step) == "p\n\nfirst\n\nsecond"

    def test_image_input_with_prompt_moderates_prompt_only(self):
        hook = TrackingHook()
        provider = MockProvider()
        image_asset = Asset(
            url="https://input.test/image.png",
            media_type="image/png",
            metadata={},
        )
        result = (
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt="safe prompt",
                modality=Modality.IMAGE,
                external_inputs=[image_asset],
            )
            .run()
        )

        assert provider.call_count == 1
        assert result.run.steps[0].status == StepStatus.SUCCEEDED
        assert len(hook.prompt_calls) == 1
        assert hook.prompt_calls[0][0] == "safe prompt"

    def test_rejected_input_text_does_not_populate_cache(self, tmp_path):
        from genblaze_core.pipeline.cache import StepCache

        cache_dir = tmp_path / "cache"
        cache = StepCache(cache_dir=cache_dir)
        hook = RejectContainingHook("blocked user text")
        provider = MockProvider()
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": "blocked user text"},
        )

        result = (
            Pipeline("test", moderation=hook)
            .cache(cache)
            .step(
                provider,
                model="m",
                prompt=None,
                modality=Modality.IMAGE,
                external_inputs=[text_asset],
            )
            .run()
        )

        assert provider.call_count == 0
        assert result.run.steps[0].status == StepStatus.FAILED
        assert list(cache_dir.glob("*.json")) == []

    def test_structured_input_text_metadata_rejected_before_serialization(self):
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": {"message": "blocked user text"}},
        )
        step = Step(provider="mock", model="m", prompt=None, inputs=[text_asset])

        with pytest.raises(ValueError, match="structured metadata"):
            _pre_moderation_payload(step)

    def test_oversized_input_text_metadata_rejected_before_hook(self):
        hook = TrackingHook()
        provider = MockProvider()
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": "A" * (8 * 1024 + 1)},
        )

        result = (
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt=None,
                modality=Modality.IMAGE,
                external_inputs=[text_asset],
            )
            .run()
        )

        step = result.run.steps[0]
        assert provider.call_count == 0
        assert hook.prompt_calls == []
        assert step.status == StepStatus.FAILED
        assert step.error_code == ProviderErrorCode.INVALID_INPUT
        assert step.error.startswith("Moderation input error:")

    def test_too_many_text_inputs_rejected_before_hook(self):
        hook = TrackingHook()
        provider = MockProvider()
        assets = [
            Asset(
                url=f"https://input.test/user-{i}.txt",
                media_type="text/plain",
                metadata={"text": "A" * 1024},
            )
            for i in range(33)
        ]

        result = (
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt=None,
                modality=Modality.IMAGE,
                external_inputs=assets,
            )
            .run()
        )

        step = result.run.steps[0]
        assert provider.call_count == 0
        assert hook.prompt_calls == []
        assert step.status == StepStatus.FAILED
        assert step.error_code == ProviderErrorCode.INVALID_INPUT

    def test_structured_input_text_metadata_fails_closed_before_provider(self):
        hook = TrackingHook()
        provider = MockProvider()
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": {"message": "blocked user text"}},
        )

        result = (
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt=None,
                modality=Modality.IMAGE,
                external_inputs=[text_asset],
            )
            .run()
        )

        step = result.run.steps[0]
        assert provider.call_count == 0
        assert hook.prompt_calls == []
        assert step.status == StepStatus.FAILED
        assert step.error_code == ProviderErrorCode.INVALID_INPUT

    def test_promptless_input_from_text_rejected_before_generation(self):
        hook = RejectContainingHook("blocked user text")
        source = MockProvider(
            assets=[
                Asset(
                    url="https://input.test/analysis.txt",
                    media_type="text/plain",
                    metadata={"text": "blocked user text"},
                )
            ]
        )
        consumer = MockProvider()

        result = (
            Pipeline("test", moderation=hook)
            .step(source, model="source", prompt="source text", modality=Modality.TEXT)
            .step(consumer, model="consumer", prompt=None, modality=Modality.IMAGE, input_from=0)
            .run()
        )

        rejected = result.run.steps[1]
        assert source.call_count == 1
        assert consumer.call_count == 0
        assert rejected.status == StepStatus.FAILED
        assert rejected.error_code == ProviderErrorCode.INVALID_INPUT
        assert len(hook.prompt_calls) == 2
        assert hook.prompt_calls[1][0] == "blocked user text"

    def test_promptless_chain_text_rejected_before_generation(self):
        hook = RejectContainingHook("blocked user text")
        source = MockProvider(
            assets=[
                Asset(
                    url="https://input.test/analysis.txt",
                    media_type="text/plain",
                    metadata={"text": "blocked user text"},
                )
            ]
        )
        consumer = MockProvider()

        result = (
            Pipeline("test", chain=True, moderation=hook)
            .step(source, model="source", prompt="source text", modality=Modality.TEXT)
            .step(consumer, model="consumer", prompt=None, modality=Modality.IMAGE)
            .run()
        )

        rejected = result.run.steps[1]
        assert source.call_count == 1
        assert consumer.call_count == 0
        assert rejected.status == StepStatus.FAILED
        assert rejected.error_code == ProviderErrorCode.INVALID_INPUT
        assert len(hook.prompt_calls) == 2
        assert hook.prompt_calls[1][0] == "blocked user text"


# ---------------------------------------------------------------------------
# Post-step moderation tests
# ---------------------------------------------------------------------------


class TestPostStepModeration:
    def test_output_rejected_marks_failed(self):
        provider = MockProvider()
        result = (
            Pipeline("test", moderation=OutputRejectHook())
            .step(provider, model="m", prompt="hello", modality=Modality.IMAGE)
            .run()
        )
        # Provider was called (generation happened)
        assert provider.call_count == 1
        step = result.run.steps[0]
        assert step.status == StepStatus.FAILED
        assert "Moderation rejected output" in step.error
        assert step.metadata["moderation"]["stage"] == "post"
        assert "nsfw" in step.metadata["moderation"]["flagged_categories"]

    def test_output_allowed_succeeds(self):
        provider = MockProvider()
        result = (
            Pipeline("test", moderation=AlwaysAllowHook())
            .step(provider, model="m", prompt="hello", modality=Modality.IMAGE)
            .run()
        )
        assert result.run.steps[0].status == StepStatus.SUCCEEDED

    def test_output_rejection_not_cached(self, tmp_path):
        """Rejected outputs should not be written to cache."""
        from genblaze_core.pipeline.cache import StepCache

        cache = StepCache(cache_dir=str(tmp_path / "cache"))
        provider = MockProvider()
        result = (
            Pipeline("test", moderation=OutputRejectHook())
            .cache(cache)
            .step(provider, model="m", prompt="hello", modality=Modality.IMAGE)
            .run()
        )
        assert result.run.steps[0].status == StepStatus.FAILED
        # Cache dir should have no entries — rejected outputs are not cached
        cache_dir = tmp_path / "cache"
        cached_files = list(cache_dir.glob("*.json")) if cache_dir.exists() else []
        assert len(cached_files) == 0


# ---------------------------------------------------------------------------
# Async moderation tests
# ---------------------------------------------------------------------------


class TestAsyncModeration:
    def test_async_prompt_rejected(self):
        provider = MockProvider()
        result = asyncio.run(
            Pipeline("test", moderation=AlwaysRejectHook())
            .step(provider, model="m", prompt="bad", modality=Modality.IMAGE)
            .arun()
        )
        assert provider.call_count == 0
        assert result.run.steps[0].status == StepStatus.FAILED

    def test_async_output_rejected(self):
        provider = MockProvider()
        result = asyncio.run(
            Pipeline("test", moderation=OutputRejectHook())
            .step(provider, model="m", prompt="hello", modality=Modality.IMAGE)
            .arun()
        )
        assert provider.call_count == 1
        assert result.run.steps[0].status == StepStatus.FAILED
        assert "Moderation rejected output" in result.run.steps[0].error

    def test_async_promptless_external_text_input_rejected(self):
        hook = RejectContainingHook("blocked user text")
        provider = MockProvider()
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": "blocked user text"},
        )
        result = asyncio.run(
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt=None,
                modality=Modality.IMAGE,
                external_inputs=[text_asset],
            )
            .arun()
        )

        step = result.run.steps[0]
        assert provider.call_count == 0
        assert step.status == StepStatus.FAILED
        assert step.error_code == ProviderErrorCode.INVALID_INPUT
        assert len(hook.prompt_calls) == 1
        assert hook.prompt_calls[0][0] == "blocked user text"

    def test_async_oversized_external_text_input_rejected_before_hook(self):
        hook = TrackingHook()
        provider = MockProvider()
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": "A" * (8 * 1024 + 1)},
        )
        result = asyncio.run(
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt=None,
                modality=Modality.IMAGE,
                external_inputs=[text_asset],
            )
            .arun()
        )

        step = result.run.steps[0]
        assert provider.call_count == 0
        assert hook.prompt_calls == []
        assert step.status == StepStatus.FAILED
        assert step.error_code == ProviderErrorCode.INVALID_INPUT

    def test_async_prompt_and_input_text_combined_into_single_payload(self):
        hook = TrackingHook()
        provider = MockProvider()
        text_asset = Asset(
            url="https://input.test/user.txt",
            media_type="text/plain",
            metadata={"text": "input side"},
        )
        result = asyncio.run(
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt="prompt side",
                modality=Modality.IMAGE,
                external_inputs=[text_asset],
            )
            .arun()
        )

        assert provider.call_count == 1
        assert result.run.steps[0].status == StepStatus.SUCCEEDED
        assert len(hook.prompt_calls) == 1
        assert hook.prompt_calls[0][0] == "prompt side\n\ninput side"

    def test_async_negative_prompt_rejected_before_generation(self):
        hook = RejectContainingHook("blocked negative text")
        provider = MockProvider()
        result = asyncio.run(
            Pipeline("test", moderation=hook)
            .step(
                provider,
                model="m",
                prompt="safe prompt",
                modality=Modality.IMAGE,
                negative_prompt="blocked negative text",
            )
            .arun()
        )

        step = result.run.steps[0]
        assert provider.call_count == 0
        assert step.status == StepStatus.FAILED
        assert step.error_code == ProviderErrorCode.INVALID_INPUT
        assert len(hook.prompt_calls) == 1
        assert hook.prompt_calls[0][0] == "safe prompt\n\nblocked negative text"

    def test_async_promptless_input_from_text_rejected(self):
        hook = RejectContainingHook("blocked user text")
        source = MockProvider(
            assets=[
                Asset(
                    url="https://input.test/analysis.txt",
                    media_type="text/plain",
                    metadata={"text": "blocked user text"},
                )
            ]
        )
        consumer = MockProvider()

        result = asyncio.run(
            Pipeline("test", moderation=hook)
            .step(source, model="source", prompt="source text", modality=Modality.TEXT)
            .step(consumer, model="consumer", prompt=None, modality=Modality.IMAGE, input_from=0)
            .arun()
        )

        rejected = result.run.steps[1]
        assert source.call_count == 1
        assert consumer.call_count == 0
        assert rejected.status == StepStatus.FAILED
        assert rejected.error_code == ProviderErrorCode.INVALID_INPUT
        assert len(hook.prompt_calls) == 2
        assert hook.prompt_calls[1][0] == "blocked user text"

    def test_async_promptless_chain_text_rejected(self):
        hook = RejectContainingHook("blocked user text")
        source = MockProvider(
            assets=[
                Asset(
                    url="https://input.test/analysis.txt",
                    media_type="text/plain",
                    metadata={"text": "blocked user text"},
                )
            ]
        )
        consumer = MockProvider()

        result = asyncio.run(
            Pipeline("test", chain=True, moderation=hook)
            .step(source, model="source", prompt="source text", modality=Modality.TEXT)
            .step(consumer, model="consumer", prompt=None, modality=Modality.IMAGE)
            .arun()
        )

        rejected = result.run.steps[1]
        assert source.call_count == 1
        assert consumer.call_count == 0
        assert rejected.status == StepStatus.FAILED
        assert rejected.error_code == ProviderErrorCode.INVALID_INPUT
        assert len(hook.prompt_calls) == 2
        assert hook.prompt_calls[1][0] == "blocked user text"


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestModerationErrorHandling:
    def test_hook_exception_fails_step(self):
        """A buggy moderation hook should fail the step, not crash the pipeline."""
        provider = MockProvider()
        result = (
            Pipeline("test", moderation=BrokenHook())
            .step(provider, model="m", prompt="hello", modality=Modality.IMAGE)
            .run()
        )
        step = result.run.steps[0]
        assert step.status == StepStatus.FAILED
        assert "Moderation hook error" in step.error
        assert step.error_code == ProviderErrorCode.UNKNOWN
        assert provider.call_count == 0  # Prompt check failed, never reached provider


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestModerationIntegration:
    def test_chain_mode_stops_on_rejection(self):
        """Chain pipeline stops when a moderation-rejected step fails."""
        mock1 = MockProvider(name="m1")
        mock2 = MockProvider(name="m2")
        result = (
            Pipeline("test", chain=True, moderation=AlwaysRejectHook())
            .step(mock1, model="m1", prompt="step1", modality=Modality.IMAGE)
            .step(mock2, model="m2", prompt="step2", modality=Modality.IMAGE)
            .run(fail_fast=True)
        )
        # First step rejected, second never executed
        assert mock1.call_count == 0
        assert mock2.call_count == 0
        assert len(result.run.steps) == 1

    def test_fail_fast_false_continues(self):
        """With fail_fast=False, pipeline continues past moderation failure."""
        mock1 = MockProvider(name="m1")
        mock2 = MockProvider(name="m2")
        result = (
            Pipeline("test", moderation=AlwaysRejectHook())
            .step(mock1, model="m1", prompt="step1", modality=Modality.IMAGE)
            .step(mock2, model="m2", prompt="step2", modality=Modality.IMAGE)
            .run(fail_fast=False)
        )
        # Both steps attempted (both rejected by moderation)
        assert len(result.run.steps) == 2
        assert all(s.status == StepStatus.FAILED for s in result.run.steps)

    def test_moderation_called_with_correct_args(self):
        """Verify moderation hook receives the actual prompt and params."""
        hook = TrackingHook()
        provider = MockProvider()
        Pipeline("test", moderation=hook).step(
            provider,
            model="m",
            prompt="hello world",
            modality=Modality.IMAGE,
            custom_param="value",
        ).run()
        assert len(hook.prompt_calls) == 1
        prompt, params = hook.prompt_calls[0]
        assert prompt == "hello world"
        assert params.get("custom_param") == "value"
        # Output check was also called
        assert len(hook.output_calls) == 1
