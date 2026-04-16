# Iteration & Lineage

Every generation should know what came before it. Genblaze tracks iteration history through `parent_run_id` — a field on every run that points to the previous attempt, creating a linked chain of manifests.

## Core patterns

### Prompt refinement

The most common iteration loop: same pipeline, adjusted prompt.

```python
from genblaze_core import Pipeline, Modality
from genblaze_openai import DalleProvider

dalle = DalleProvider(output_dir="output/")

# First attempt
v1 = Pipeline("hero-shot").step(
    dalle, model="dall-e-3",
    prompt="product on white background",
    modality=Modality.IMAGE,
).run()

# Refine the prompt, linked to the previous attempt
v2 = Pipeline("hero-shot").from_result(v1).step(
    dalle, model="dall-e-3",
    prompt="product on white background, soft shadows, studio lighting",
    modality=Modality.IMAGE,
).run()

# v2.run.parent_run_id == v1.run.run_id
```

### Forking (image-to-video, add voiceover)

Take a successful run's output and start a new pipeline from it:

```python
from genblaze_openai import SoraProvider

# Fork v1's image into a video pipeline
v3 = Pipeline("hero-video", chain=True).from_result(v1).step(
    SoraProvider(), model="sora-2",
    prompt="slow zoom in",
    modality=Modality.VIDEO,
).run(timeout=300)

# v3.run.parent_run_id == v1.run.run_id
```

### Prompt exploration with batch_run

Compare multiple prompts in parallel, all sharing the same pipeline template:

```python
results = Pipeline("prompt-test").step(
    dalle, model="dall-e-3", prompt="{prompt}", modality=Modality.IMAGE,
).batch_run(
    prompts=[
        "a cat on a beach",
        "a cat on a beach at sunset",
        "a cat on a beach, cinematic",
    ],
)
```

## How it works

- `Pipeline.from_result(previous)` stores `previous.run.run_id` as the parent
- When `run()` or `arun()` finalizes, `parent_run_id` is set on the new `Run`
- `parent_run_id` is **excluded from the canonical hash** — linking doesn't change provenance integrity
- `parent_run_id` **is included** in the full manifest JSON, so it roundtrips through serialization and storage

## Manual builds

Use `RunBuilder.parent()` for non-pipeline workflows:

```python
from genblaze_core import RunBuilder, StepBuilder, Modality, Manifest

step = StepBuilder("replicate", "flux-schnell").prompt("v2 prompt").modality(Modality.IMAGE).build()
run = RunBuilder("v2").parent("previous-run-id").add_step(step).build()
manifest = Manifest.from_run(run)
```

## Querying lineage from Parquet

The `ParquetSink` writes `parent_run_id` to the runs table. Query iteration chains with standard Parquet/SQL tools:

```python
import pyarrow.parquet as pq

runs = pq.read_table("data/runs/").to_pandas()

# All iterations spawned from a specific run
children = runs[runs["parent_run_id"] == "target-run-id"]

# Full lineage chain (walk parent pointers)
def lineage(run_id, df):
    chain = []
    while run_id:
        row = df[df["run_id"] == run_id]
        if row.empty:
            break
        chain.append(row.iloc[0])
        run_id = row.iloc[0]["parent_run_id"]
    return chain
```

## Tagging iterations

Use the existing `metadata` dict to mark favorites:

```python
result = Pipeline("hero-shot").from_result(v1).step(...).run()
result.run.metadata["selected"] = True
result.run.metadata["iteration_tag"] = "prompt-refinement"
```
