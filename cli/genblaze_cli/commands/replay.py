"""Replay command — re-execute a pipeline from a manifest file."""

import json
from pathlib import Path

import click
from genblaze_core.models.enums import PromptVisibility


def _load_provider(provider_name: str, allowed: tuple[str, ...] | None):
    """Load a provider by name using entry point discovery.

    If allowed is set, reject providers not in the allowlist.
    """
    if allowed and provider_name not in allowed:
        raise click.ClickException(
            f"Provider '{provider_name}' not in allowlist: {', '.join(allowed)}"
        )
    from genblaze_core.providers.registry import discover_providers

    registry = discover_providers()
    cls = registry.get(provider_name)
    if cls is None:
        available = ", ".join(sorted(registry.keys())) or "(none installed)"
        raise click.ClickException(
            f"Unknown provider '{provider_name}'. Installed providers: {available}"
        )
    click.echo(f"  Loading provider: {provider_name}", err=True)
    return cls()


def _detect_chain_mode(run) -> bool:
    """Detect if the original pipeline used chain mode by checking step inputs.

    If any step has _input_from metadata, this is a fan-in pipeline (not chain).
    Otherwise, if every non-first step has inputs, it was likely chain mode.
    """
    has_input_from = any(step.metadata.get("_input_from") is not None for step in run.steps)
    if has_input_from:
        return False
    non_first = run.steps[1:]
    return bool(non_first) and all(len(step.inputs) > 0 for step in non_first)


def _print_summary(run, *, show_prompts: bool) -> None:
    """Print manifest run summary.

    Prompts are only shown when ``show_prompts`` is True AND the step's
    prompt_visibility is PUBLIC. Non-public prompts are redacted to avoid
    leaking private content to terminals/CI logs when replaying manifests
    pulled from shared storage.
    """
    click.echo(f"Run:    {run.run_id}")
    click.echo(f"Name:   {run.name or '(unnamed)'}")
    click.echo(f"Steps:  {len(run.steps)}")
    click.echo()

    for i, step in enumerate(run.steps, 1):
        click.echo(f"  Step {i}: {step.provider}/{step.model}")
        click.echo(f"    Type:     {step.step_type}")
        if show_prompts and step.prompt_visibility == PromptVisibility.PUBLIC:
            click.echo(f"    Prompt:   {step.prompt or '(none)'}")
        else:
            click.echo(
                f"    Prompt:   [redacted — visibility={step.prompt_visibility};"
                " pass --show-prompts to reveal public prompts]"
            )
        click.echo(f"    Modality: {step.modality}")
        if step.params:
            click.echo(f"    Params:   {step.params}")


# Fields that cannot be faithfully replayed (logged as warnings)
_UNREPLAYABLE_FIELDS = [
    ("prompt_visibility", lambda s: s.prompt_visibility != "public"),
]


@click.command()
@click.argument("manifest_file", type=click.Path(exists=True, path_type=Path))
@click.option("--dry-run/--no-dry-run", default=True, help="Preview without executing.")
@click.option(
    "--allow-provider",
    multiple=True,
    help="Only allow these providers (can be specified multiple times).",
)
@click.option(
    "--show-prompts",
    is_flag=True,
    help="Show public-visibility prompts in the summary. Non-public prompts are always redacted.",
)
@click.option("--force", is_flag=True, help="Skip manifest hash verification.")
def replay(
    manifest_file: Path,
    dry_run: bool,
    allow_provider: tuple[str, ...],
    show_prompts: bool,
    force: bool,
) -> None:
    """Re-execute a pipeline from a manifest JSON file."""
    from genblaze_core.models.manifest import parse_manifest
    from genblaze_core.pipeline import Pipeline

    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest = parse_manifest(data)
    except Exception as exc:
        raise click.ClickException(f"Failed to load manifest: {exc}") from exc

    # Verify manifest hash integrity
    if not force and not manifest.verify():
        click.echo(
            "WARNING: Manifest hash does not match content. The file may have been modified.",
            err=True,
        )
        if not click.confirm("Continue anyway?"):
            raise click.Abort()

    run = manifest.run
    _print_summary(run, show_prompts=show_prompts)

    if dry_run:
        click.echo()
        click.echo("Dry run — no steps executed. Use --no-dry-run to execute.")
        return

    # Build and execute pipeline from manifest steps
    click.echo()
    click.echo("Replaying pipeline...")

    allowed: tuple[str, ...] | None = allow_provider if allow_provider else None

    # Safety: when no allowlist is provided, confirm each unique provider.
    # A hostile manifest could reference any installed provider and execute
    # paid API calls on the user's credentials without consent.
    if allowed is None:
        unique_providers = sorted({step.provider for step in run.steps})
        click.echo(
            "No --allow-provider allowlist set. Confirm each provider before execution:",
            err=True,
        )
        confirmed: list[str] = []
        for name in unique_providers:
            if click.confirm(f"  Execute with provider '{name}'?", default=False):
                confirmed.append(name)
            else:
                raise click.Abort()
        allowed = tuple(confirmed)

    # Detect chain mode from step inputs in the manifest
    chain = _detect_chain_mode(run)
    if chain:
        click.echo("  Detected chain mode from manifest step inputs.", err=True)

    # Cache providers by name to avoid re-instantiating
    providers: dict[str, object] = {}
    pipe = Pipeline(
        run.name,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        chain=chain,
    )

    # Reserved kwargs that Pipeline.step() accepts explicitly
    _reserved = {"model", "prompt", "modality", "step_type", "seed", "negative_prompt"}

    for step_idx, step in enumerate(run.steps):
        if step.provider not in providers:
            providers[step.provider] = _load_provider(step.provider, allowed)

        # Warn about fields that can't be faithfully replayed
        for field_name, has_value in _UNREPLAYABLE_FIELDS:
            if has_value(step):
                click.echo(
                    f"  WARNING: Step {step_idx + 1} has {field_name}={getattr(step, field_name)}"
                    f" which may not replay identically.",
                    err=True,
                )

        # Strip reserved keys from params to avoid TypeError on collision
        extra_params = {k: v for k, v in step.params.items() if k not in _reserved}

        # Restore seed and negative_prompt if present
        if step.seed is not None:
            extra_params["seed"] = step.seed
        if step.negative_prompt is not None:
            extra_params["negative_prompt"] = step.negative_prompt

        # Restore fallback_models and input_from from step metadata
        fallback_models = step.metadata.get("_fallback_models")
        input_from = step.metadata.get("_input_from")

        pipe.step(
            providers[step.provider],
            model=step.model,
            prompt=step.prompt,
            modality=step.modality,
            step_type=step.step_type,
            fallback_models=fallback_models,
            input_from=input_from,
            **extra_params,
        )

    try:
        result = pipe.run()
        click.echo()
        click.echo(f"Replay complete. New run ID: {result.run.run_id}")
        click.echo(f"Hash: {result.manifest.canonical_hash}")
        click.echo(f"Status: {result.run.status}")
    except Exception as exc:
        raise click.ClickException(f"Replay failed: {exc}") from exc
