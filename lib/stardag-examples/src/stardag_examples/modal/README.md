# Modal Integration Examples

Run Stardag tasks on Modal's serverless infrastructure with automatic scaling, GPU support, and custom environments.

Includes examples for:

- **basic/** - Minimal Modal app setup
- **ml_pipeline/** - ML pipeline on Modal
- **prefect/** - Modal + Prefect for observability

## Quick Start

```bash
cd lib/stardag-examples
uv sync --extra modal

# Deploy and run basic example
modal deploy stardag_examples/modal/basic/app.py
python stardag_examples/modal/basic/main.py
```

## Documentation

See the full guide: [Integrate with Modal](https://docs.stardag.com/how-to/integrate-modal/)
