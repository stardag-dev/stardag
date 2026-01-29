# Integrate with Prefect

Use Prefect for orchestration, observability, and workflow management.

---

!!! info "🚧 **Work in progress** 🚧"

    This documentation is still taking shape. It should soon cover how to get the most out of Stardag with Prefect for observability and orchestration.

    For now see: [ML Pipeline Example](https://github.com/andhus/stardag/tree/main/lib/stardag-examples/src/stardag_examples/ml_pipeline)

<!--
## Overview

The Prefect integration provides:

- Prefect flow and task wrappers for Stardag tasks
- Observability via Prefect UI
- Retry logic and error handling
- Scheduling and triggers

## Prerequisites

```bash
pip install stardag[prefect]
```

You'll also need a Prefect server or Prefect Cloud account.

## Basic Usage

```python
from stardag.integration.prefect.build import build as prefect_build
import stardag as sd

@sd.task
def fetch_data(source: str) -> list[int]:
    return [1, 2, 3, 4, 5]

@sd.task
def process(data: sd.Depends[list[int]]) -> int:
    return sum(data)

# Build using Prefect orchestration
task = process(data=fetch_data(source="api"))
prefect_build(task)
```

## Viewing in Prefect UI

After running with `prefect_build()`, view your DAG execution in the Prefect UI:

1. Start Prefect server: `prefect server start`
2. Open http://localhost:4200
3. View flow runs and task states

## Configuration

### Prefect Server

```bash
# Local server
prefect server start

# Or use Prefect Cloud
prefect cloud login
```

### Environment Variables

```bash
export PREFECT_API_URL=http://localhost:4200/api
```

## Example: ML Pipeline

```python
from stardag.integration.prefect.build import build as prefect_build
import stardag as sd

@sd.task
def load_dataset(path: str) -> dict:
    """Load training data."""
    # ...
    pass

@sd.task
def train_model(data: sd.Depends[dict]) -> dict:
    """Train ML model."""
    # ...
    pass

@sd.task
def evaluate(model: sd.Depends[dict], data: sd.Depends[dict]) -> dict:
    """Evaluate model performance."""
    # ...
    pass

# Build the pipeline
dataset = load_dataset(path="data/train.csv")
model = train_model(data=dataset)
evaluation = evaluate(model=model, data=dataset)

prefect_build(evaluation)
```

View in Prefect UI:

![Prefect UI showing ML pipeline](https://github.com/user-attachments/assets/372c40c4-ca14-49b3-bbf6-18b758ddce5f)


## See Also

- [ML Pipeline Example](https://github.com/andhus/stardag/tree/main/lib/stardag-examples/src/stardag_examples/ml_pipeline) - Complete example
- [Prefect Documentation](https://docs.prefect.io/) - Prefect features -->
