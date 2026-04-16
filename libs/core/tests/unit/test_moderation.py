"""Tests for ModerationHook pipeline integration."""

from __future__ import annotations

import asyncio

from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import Modality, ProviderErrorCode, StepStatus
from genblaze_core.pipeline.moderation import ModerationHook, ModerationResult
from genblaze_core.pipeline.pipeline import Pipeline
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
        assert "Moderation rejected prompt" in result.run.steps[0].error

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

    def test_null_prompt_skips_moderation(self):
        """Steps with prompt=None skip pre-moderation (e.g. compositor)."""
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
