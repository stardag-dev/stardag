# Integrate with Modal

Run Stardag tasks on Modal's serverless infrastructure.

## Overview

The Modal integration enables:

- Serverless execution of tasks
- Automatic scaling
- GPU support
- Custom container environments

## Prerequisites

```bash
pip install stardag[modal]
```

You'll also need a [Modal](https://modal.com/) account.

---

!!! info "🚧 **Work in progress** 🚧"

    This documentation is still taking shape. It should soon cover how to execute your Stardag DAGs on Modal.

    For now see: [Modal Examples](https://github.com/stardag-dev/stardag/tree/main/lib/stardag-examples/src/stardag_examples/modal)

<!--
## Basic Usage

Create a `StardagApp` to run your DAGs on Modal:

```python
import modal
from stardag.integration.modal import StardagApp, FunctionSettings

# Define base image with stardag installed
base_image = modal.Image.debian_slim().pip_install("stardag")

# Create app with worker configuration
app = StardagApp(
    "stardag-example",
    builder_settings=FunctionSettings(image=base_image),
    worker_settings={
        "default": FunctionSettings(image=base_image),
    },
)

# Your tasks are defined normally
import stardag as sd

@sd.task
def process_data(data: sd.Depends[list[int]]) -> int:
    return sum(data)

# Run remotely via Modal
if __name__ == "__main__":
    task = process_data(data=some_upstream_task())
    app.build_remote(task)
```

## Configuration

### Modal Setup

```bash
# Login to Modal
modal token new
```

### Worker Configuration

Configure different workers for different task types:

```python
from stardag.integration.modal import StardagApp, FunctionSettings, WorkerSelectorByName

gpu_image = (
    modal.Image.debian_slim()
    .pip_install("stardag", "torch")
)

cpu_image = modal.Image.debian_slim().pip_install("stardag")

app = StardagApp(
    "gpu-example",
    builder_settings=FunctionSettings(image=cpu_image),
    worker_settings={
        "default": FunctionSettings(image=cpu_image),
        "gpu": FunctionSettings(image=gpu_image, gpu="T4"),
    },
    # Route tasks to workers by name
    worker_selector=WorkerSelectorByName({"TrainModel": "gpu"}),
)
```

## Example: GPU Processing

```python
import modal
import stardag as sd
from stardag.integration.modal import StardagApp, FunctionSettings

gpu_image = (
    modal.Image.debian_slim()
    .pip_install("stardag", "torch")
)

app = StardagApp(
    "training-app",
    builder_settings=FunctionSettings(image=gpu_image),
    worker_settings={
        "default": FunctionSettings(image=gpu_image, gpu="T4"),
    },
)

@sd.task
def train_model(data: sd.Depends[dict]) -> dict:
    import torch
    # GPU training logic
    return {"model": "trained"}

# Run on Modal with GPU
if __name__ == "__main__":
    task = train_model(data=prepare_data())
    app.build_remote(task)
```

## Using ModalTaskExecutor

For more control, use `ModalTaskExecutor` with the standard build:

```python
from stardag import build
from stardag.build import HybridConcurrentTaskExecutor, RoutedTaskExecutor
from stardag.integration.modal import ModalTaskExecutor

modal_executor = ModalTaskExecutor(
    modal_app_name="my-app",
    worker_selector=lambda task: "gpu" if needs_gpu(task) else "default",
)
local_executor = HybridConcurrentTaskExecutor()

routed = RoutedTaskExecutor(
    executors={"modal": modal_executor, "local": local_executor},
    router=lambda task: "modal" if should_run_on_modal(task) else "local",
)

build([task], task_executor=routed)
```

## See Also

- [Modal Examples](https://github.com/stardag-dev/stardag/tree/main/lib/stardag-examples/src/stardag_examples/modal) - Example implementations
- [Modal Documentation](https://modal.com/docs) - Modal features
 -->
