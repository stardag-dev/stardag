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

## Basic Usage

```python
import modal
import stardag as sd
from stardag.integration.modal import modal_task

app = modal.App("stardag-example")

@modal_task(app)
def process_data(data: sd.Depends[list[int]]) -> int:
    return sum(data)
```

<!-- TODO: Verify modal_task decorator syntax -->

## Configuration

### Modal Setup

```bash
# Login to Modal
modal token new
```

### Environment Configuration

```python
image = modal.Image.debian_slim().pip_install("stardag")

@modal_task(app, image=image)
def my_task(...):
    pass
```

## Example: GPU Processing

```python
import modal
import stardag as sd
from stardag.integration.modal import modal_task

app = modal.App("gpu-example")

gpu_image = (
    modal.Image.debian_slim()
    .pip_install("stardag", "torch")
)

@modal_task(app, image=gpu_image, gpu="T4")
def train_model(data: sd.Depends[dict]) -> dict:
    import torch
    # GPU training logic
    pass
```

## Combining with Prefect

Use Modal for execution with Prefect for orchestration:

```python
from stardag.integration.prefect import build as prefect_build
from stardag.integration.modal import modal_task

# Modal handles execution
@modal_task(app)
def heavy_computation(data: sd.Depends[list]) -> dict:
    # Runs on Modal infrastructure
    pass

# Prefect handles orchestration
prefect_build(heavy_computation(data=source_task()))
```

## See Also

- [Modal Examples](https://github.com/andhus/stardag/tree/main/lib/stardag-examples/src/stardag_examples/modal) - Example implementations
- [Modal Documentation](https://modal.com/docs) - Modal features

<!-- TODO: Add more detailed Modal integration documentation -->
