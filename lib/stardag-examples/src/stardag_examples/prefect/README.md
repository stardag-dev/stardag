# Prefect Integration Example

Build and execute Stardag DAGs with Prefect for orchestration and observability.

## Quick Start

```bash
cd lib/stardag-examples
uv sync --extra prefect --extra ml-pipeline

# Start local Prefect server (or use Prefect Cloud)
prefect server start

# In another terminal
export PREFECT_API_URL="http://127.0.0.1:4200/api"
uv run python -m stardag_examples.prefect.main
```

## Documentation

See the full guide: [Integrate with Prefect](https://docs.stardag.com/how-to/integrate-prefect/)
