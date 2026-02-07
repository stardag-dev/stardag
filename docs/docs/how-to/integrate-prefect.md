# Integrate with Prefect

Use Prefect for orchestration, observability, and workflow management.

## Overview

The Prefect integration provides:

- Prefect flow and task wrappers for Stardag tasks
- Observability via Prefect UI
- Concurrent task execution
- Retry logic and error handling

## Prerequisites

```bash
pip install stardag[prefect]
```

You'll also need a Prefect server or Prefect Cloud account.

## Setup

### Option 1: Local Prefect Server

Start a local Prefect server:

```bash
prefect server start
```

Then, in a separate terminal:

```bash
export PREFECT_API_URL="http://127.0.0.1:4200/api"
```

### Option 2: Prefect Cloud

Sign up at [prefect.io](https://www.prefect.io/) then:

```bash
prefect cloud login
```

## Basic Usage

Use the Prefect builder to execute your DAG with Prefect orchestration:

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

## Running the Example

The examples package includes a ready-to-run Prefect example:

```bash
cd lib/stardag-examples
uv sync --extra prefect --extra ml-pipeline
uv run python -m stardag_examples.prefect.main
```

You'll see Prefect logs in your terminal. Navigate to the Prefect UI and click "latest run" to see your DAG:

![Prefect UI showing DAG execution](https://github.com/user-attachments/assets/372c40c4-ca14-49b3-bbf6-18b758ddce5f)

## Viewing in Prefect UI

After running with `prefect_build()`:

1. Open your Prefect UI (http://localhost:4200 for local, or Prefect Cloud)
2. Navigate to Flow Runs
3. Click on the latest run to see:
   - Task dependencies as a graph
   - Task states (pending, running, completed, failed)
   - Execution timeline
   - Logs and artifacts

## Concurrent Execution

Prefect automatically executes independent tasks concurrently. Tasks run as soon as their dependencies complete:

```python
@sd.task
def step_a() -> int:
    return 1

@sd.task
def step_b() -> int:
    return 2

@sd.task
def combine(a: sd.Depends[int], b: sd.Depends[int]) -> int:
    return a + b

# step_a and step_b run concurrently
# combine waits for both to complete
result = combine(a=step_a(), b=step_b())
prefect_build(result)
```

## Example: ML Pipeline with Prefect

Combine the [ML Pipeline example](ml-pipeline-example.md) with Prefect:

```python
from stardag.integration.prefect.build import build as prefect_build
from stardag_examples.ml_pipeline.decorator_api import get_benchmark_dag

# Get the benchmark DAG (trains multiple models)
dag = get_benchmark_dag()

# Build with Prefect - models train concurrently
prefect_build(dag)
```

## Configuration

### Environment Variables

| Variable          | Description                      |
| ----------------- | -------------------------------- |
| `PREFECT_API_URL` | Prefect API URL (local or cloud) |

### Using with Modal

For running Prefect-orchestrated DAGs on Modal's serverless infrastructure, see [Integrate with Modal](integrate-modal.md#with-prefect-observability).

## See Also

- [ML Pipeline Example](ml-pipeline-example.md) - Complete ML pipeline walkthrough
- [Prefect Examples](https://github.com/stardag-dev/stardag/tree/main/lib/stardag-examples/src/stardag_examples/prefect) - Source code
- [Prefect Documentation](https://docs.prefect.io/) - Prefect features
