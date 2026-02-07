# Integrate with Modal

Run Stardag tasks on Modal's serverless infrastructure.

## Overview

[Modal](https://modal.com/) provides serverless cloud computing for engineers who want to build compute-intensive applications without managing infrastructure. The Stardag Modal integration enables:

- Serverless execution of tasks
- Automatic scaling
- GPU support
- Custom container environments
- Multiple worker types for different workloads

## Prerequisites

```bash
pip install stardag[modal]
```

You'll also need a [Modal](https://modal.com/) account:

```bash
modal token new
```

## Project Structure

We recommend organizing your Modal code in a proper package structure:

```
my_package/
  __init__.py    # (can be empty)
  app.py         # Modal app definition
  tasks.py       # Stardag task definitions
  main.py        # Entry point
```

## Basic Example

### 1. Define Your Modal App

```python
# my_package/app.py
import modal
import stardag.integration.modal as sd_modal

VOLUME_NAME = "stardag-default"
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("stardag[modal]")
    .env({"STARDAG_TARGET_ROOTS__DEFAULT": f"modalvol://{VOLUME_NAME}/root/default"})
    .add_local_python_source("my_package")
)

stardag_app = sd_modal.StardagApp(
    "my-stardag-app",
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image),
    },
)

app = stardag_app.modal_app
```

### 2. Define Your Tasks

```python
# my_package/tasks.py
import stardag as sd

@sd.task(name="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task(name="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)
```

### 3. Deploy and Run

Deploy to Modal:

```bash
modal deploy my_package/app.py
```

Run your DAG:

```python
# my_package/main.py
from my_package.app import stardag_app
from my_package.tasks import get_range, get_sum

if __name__ == "__main__":
    dag = get_sum(integers=get_range(limit=10))
    stardag_app.build_spawn(dag)
```

You'll see the `build` and `worker_default` functions invoked in the Modal UI.

### 4. Retrieve Results Locally

Configure the same target root locally:

```bash
export STARDAG_TARGET_ROOTS__DEFAULT=modalvol://stardag-default/root/default
```

Then load the result:

```python
from my_package.tasks import get_range, get_sum

dag = get_sum(integers=get_range(limit=10))
print(dag.output().load())  # 45
```

## Running the Examples

The examples package includes ready-to-run Modal examples:

```bash
cd lib/stardag-examples
uv sync --extra modal

# Basic example
modal deploy stardag_examples/modal/basic/app.py
python stardag_examples/modal/basic/main.py
```

## With Prefect Observability

For production workloads, combine Modal with Prefect for observability:

```bash
modal deploy stardag_examples/modal/prefect/app.py
python stardag_examples/modal/prefect/main.py
```

### App Configuration

```python
import modal
import stardag.integration.modal as sd_modal

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("stardag[modal,prefect]", "pandas", "scikit-learn")
    .env({
        "PREFECT_API_URL": "https://api.prefect.cloud/api/accounts/<id>/workspaces/<id>",
        "STARDAG_TARGET_ROOTS__DEFAULT": f"modalvol://{VOLUME_NAME}/root/default",
    })
    .add_local_python_source("my_package")
)

stardag_app = sd_modal.StardagApp(
    "my-app-with-prefect",
    builder_type="prefect",  # Enable Prefect orchestration
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[modal.Secret.from_name("prefect-api-key")],
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image, cpu=1),
        "large": sd_modal.FunctionSettings(image=image, cpu=2),
    },
)
```

### Worker Routing

Route tasks to different workers based on their requirements:

```python
import stardag as sd

def worker_selector(task: sd.Task) -> str:
    if task.get_name() == "TrainedModel":
        return "large"  # Heavy computation
    return "default"

dag = get_benchmark_dag()
stardag_app.build_spawn(dag, worker_selector=worker_selector)
```

### View in Prefect UI

Tasks run concurrently as soon as their dependencies complete:

![Prefect UI showing concurrent task execution](https://github.com/user-attachments/assets/2f0d9db7-e9b7-4138-91c8-5973073dcd62)

## GPU Support

Configure GPU workers for ML training:

```python
gpu_image = (
    modal.Image.debian_slim()
    .pip_install("stardag[modal]", "torch")
)

stardag_app = sd_modal.StardagApp(
    "gpu-training",
    builder_settings=sd_modal.FunctionSettings(image=gpu_image),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=gpu_image),
        "gpu": sd_modal.FunctionSettings(image=gpu_image, gpu="T4"),
    },
)
```

## Configuration Reference

### StardagApp Parameters

| Parameter          | Description                                 |
| ------------------ | ------------------------------------------- |
| `name`             | Modal app name                              |
| `builder_type`     | `"default"` or `"prefect"`                  |
| `builder_settings` | FunctionSettings for the build orchestrator |
| `worker_settings`  | Dict of worker name to FunctionSettings     |

### FunctionSettings Parameters

| Parameter | Description                                 |
| --------- | ------------------------------------------- |
| `image`   | Modal Image with dependencies               |
| `cpu`     | CPU cores (e.g., `1`, `2`, `4`)             |
| `gpu`     | GPU type (e.g., `"T4"`, `"A10G"`, `"A100"`) |
| `memory`  | Memory in MB                                |
| `secrets` | List of Modal secrets                       |

### Environment Variables

| Variable                        | Description                                           |
| ------------------------------- | ----------------------------------------------------- |
| `STARDAG_TARGET_ROOTS__DEFAULT` | Target root URI (e.g., `modalvol://volume-name/path`) |
| `PREFECT_API_URL`               | Prefect API URL (for Prefect integration)             |

## See Also

- [ML Pipeline Example](ml-pipeline-example.md) - Complete ML pipeline walkthrough
- [Integrate with Prefect](integrate-prefect.md) - Prefect orchestration
- [Modal Examples](https://github.com/stardag-dev/stardag/tree/main/lib/stardag-examples/src/stardag_examples/modal) - Source code
- [Modal Documentation](https://modal.com/docs) - Modal features
