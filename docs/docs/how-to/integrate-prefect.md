# Integrate with Prefect

Use Prefect for orchestration, observability, and workflow management.

## Overview

The Prefect integration provides (near-zero boilerplate) logic to build any Stardag DAG such that the execution gets mapped to native Prefect primitives:

- Get observability via the **Prefect UI**
- Manage concurrent task execution, retry logic and error handling via native [**Prefect Task Runners**](https://docs.prefect.io/v3/api-ref/python/prefect-task_runners#task_runners)
- _Still leverage Stardag for Makefile-style/bottom-up execution and persistent caching_

## Prerequisites

Stardag with `prefect` extra dependencies installed:

=== "uv"

    ```sh
    uv add stardag[prefect]
    ```

=== "pip"

    ```sh
    pip install stardag[prefect]
    ```

You'll also need a Prefect server or Prefect Cloud account.

## Setup

=== "Local Prefect Server"

    Start a local Prefect server:

    ```sh
    prefect server start
    ```

    Then, in a separate terminal:

    ```sh
    export PREFECT_API_URL="http://127.0.0.1:4200/api"
    ```

=== "Prefect Cloud"

    Sign up at [prefect.io](https://www.prefect.io/) then:

    ```sh
    prefect cloud login
    ```

## Basic Usage

Use the Prefect builder to execute your DAG with Prefect orchestration:

```{.python notest}
import asyncio

import stardag as sd
from prefect import flow
from stardag.integration.prefect import build_aio as prefect_build_aio


@sd.task
def fetch_data(source: str) -> list[int]:
    return [1, 2, 3, 4, 5]


@sd.task
def process(data: sd.Depends[list[int]]) -> int:
    return sum(data)


@flow
async def my_flow():
    task = process(data=fetch_data(source="api"))
    await prefect_build_aio(task)


if __name__ == "__main__":
    asyncio.run(my_flow())
```

The only thing needed to build your DAG using Prefect primitives is to replace `stardag.build_aio` with `stardag.integration.prefect.build_aio`.

You can call `stardag.integration.prefect.build_aio` anywhere in an existing Prefect flow and mix Prefect's imperative execution with Stardag's bottom-up, persistently cached execution to your liking.

A difference between `stardag.build_aio` and `stardag.integration.prefect.build_aio` is that the latter returns `dict[str, PrefectConcurrentFuture]` - a mapping from `task.id` to a Prefect future. If you set `build_aio(..., wait_for_completion=False)`, the function will return as soon as the DAG is traversed and all tasks are submitted to the native Prefect [`TaskRunner`](https://docs.prefect.io/v3/api-ref/python/prefect-task_runners#task_runners), so that you can continue submitting other Prefect tasks to the task runner conditioned on Stardag tasks having completed.

## Running the `stardag-examples` Example

The examples package includes a ready-to-run Prefect example.
Clone the repo:

=== "HTTPS"

    Clone using the web URL.

    ```sh
    git clone https://github.com/stardag-dev/stardag.git
    cd stardag/lib/stardag-examples
    ```

=== "SSH"

    Use a password-protected SSH key.

    ```sh
    git clone git@github.com:stardag-dev/stardag.git
    cd stardag/lib/stardag-examples
    ```

=== "GitHub CLI"

    Use the GitHub official CLI. [Learn more](https://cli.github.com/)

    ```sh
    gh repo clone stardag-dev/stardag
    cd stardag/lib/stardag-examples
    ```

And install the package (with `ml-pipeline` extra dependencies)

=== "uv"

    ```sh
    uv sync --extra prefect --extra ml-pipeline
    uv run python -m stardag_examples.prefect.main
    ```

=== "pip"

    ```sh
    pip install -e ".[prefect,ml-pipeline]"
    python -m stardag_examples.prefect.main
    ```

You'll see Prefect logs in your terminal. Navigate to the Prefect UI and click "latest run" to see your DAG:

![Prefect UI showing DAG execution](https://github.com/user-attachments/assets/3bc3f6af-503c-4eb0-bad9-61ad25c747c0)

Click the artifacts to get more details about the task:

![Task details](https://github.com/user-attachments/assets/3e761707-3d4f-442f-8685-70ce4e5bda63)

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
