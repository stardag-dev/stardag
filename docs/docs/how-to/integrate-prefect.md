# Integrate with Prefect

Use Prefect for orchestration, observability, and workflow management.

## Overview

The Prefect integration provides:

- Prefect flow and task wrappers for Stardag tasks
- Observability via Prefect UI
- Concurrent task execution
- Retry logic and error handling

## Prerequisites

=== "uv"

    ```bash
    uv add stardag[prefect]
    ```

=== "pip"

    ```bash
    pip install stardag[prefect]
    ```

You'll also need a Prefect server or Prefect Cloud account.

## Setup

=== "Local Prefect Server"

    Start a local Prefect server:

    ```bash
    prefect server start
    ```

    Then, in a separate terminal:

    ```bash
    export PREFECT_API_URL="http://127.0.0.1:4200/api"
    ```

=== "Prefect Cloud"

    Sign up at [prefect.io](https://www.prefect.io/) then:

    ```bash
    prefect cloud login
    ```

## Basic Usage

Use the Prefect builder to execute your DAG with Prefect orchestration:

```python
import asyncio

import stardag as sd
from prefect import flow
from stardag.integration.prefect.build import build as prefect_build


@sd.task
def fetch_data(source: str) -> list[int]:
    return [1, 2, 3, 4, 5]


@sd.task
def process(data: sd.Depends[list[int]]) -> int:
    return sum(data)


@flow
async def my_flow():
    task = process(data=fetch_data(source="api"))
    await prefect_build(task)


if __name__ == "__main__":
    asyncio.run(my_flow())
```

## Running the Example

The examples package includes a ready-to-run Prefect example:

=== "uv"

    ```bash
    cd lib/stardag-examples
    uv sync --extra prefect --extra ml-pipeline
    uv run python -m stardag_examples.prefect.main
    ```

=== "pip"

    ```bash
    cd lib/stardag-examples
    pip install -e ".[prefect,ml-pipeline]"
    python -m stardag_examples.prefect.main
    ```

You'll see Prefect logs in your terminal. Navigate to the Prefect UI and click "latest run" to see your DAG:

![Prefect UI showing DAG execution](https://github.com/user-attachments/assets/372c40c4-ca14-49b3-bbf6-18b758ddce5f)

## Example: ML Pipeline with Prefect

The actual example from the source code:

```python
import asyncio

import stardag as sd
from prefect import flow
from stardag.integration.prefect.build import build as prefect_build
from stardag.integration.prefect.build import create_markdown

from stardag_examples.ml_pipeline.class_api import get_metrics_dag


async def custom_callback(task):
    """Upload artifacts to Prefect Cloud for tasks that implement the special method."""
    if hasattr(task, "prefect_on_complete_artifacts"):
        for artifact in task.prefect_on_complete_artifacts():
            await artifact.create()


@flow
async def build_dag(task: sd.Task):
    """A flow that builds any stardag Task."""
    await prefect_build(
        task,
        before_run_callback=create_markdown,
        on_complete_callback=custom_callback,
    )


if __name__ == "__main__":
    metrics = get_metrics_dag()
    asyncio.run(build_dag(metrics))
    print(metrics.output().load())
```

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

Prefect automatically executes independent tasks concurrently. Tasks run as soon as their dependencies complete.

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
