# ML Pipeline Example

A canonical ML pipeline demonstrating Stardag's composable task system for supervised machine learning workflows.

This example shows three API styles (plain Python, class-based, and decorator-based) for building train/predict/evaluate pipelines with full dependency tracking and caching.

## Quick Start

```bash
cd lib/stardag-examples
uv sync --extra ml-pipeline
uv run python -m stardag_examples.ml_pipeline.decorator_api
```

## Documentation

See the full guide: [ML Pipeline Example](https://docs.stardag.com/how-to/ml-pipeline-example/)
