# Integrate with Modal

Run Stardag tasks on Modal's serverless infrastructure.

## Overview

[Modal](https://modal.com/) provides serverless cloud computing for engineers who want to build compute-intensive applications without managing infrastructure. The Stardag Modal integration enables:

- Serverless execution of tasks
- Automatic scaling
- Flexible routing of individual tasks to appropriate compute resources, including GPU access

## Prerequisites

### Modal Account

- [Sign up](https://modal.com/apps) for a [Modal](https://modal.com/) account.
- Optionally create a new dedicated [Modal environment](https://modal.com/docs/guide/environments), or stick with the default `main` environment.

### Stardag Registry Environment (Optional)

You are recommended to get set up to use the Stardag Registry. Sign up at `app.stardag.com` or follow [the setup guide](../getting-started/registry-ui.md#get-setup) for running it locally or self hosted.

You can also run Stardag on Modal, completely without the Registry.

=== "With Registry"

    **Create a new envrionment**

    WIP

=== "Without Registry"

    You're all set. Just skip using a Stardag API-key in the examples.

## Minimal Example from Scratch

We are going to create a new minimal standalone python project with the folowing structure:

```
stardag-modal/
├── stardag_modal/
|   ├── __init__.py
|   └── main.py
└── pyproject.toml
```

Create the new project (with `uv` for build system)

```sh
mkdir stardag-modal
cd stardag-modal
cat > pyproject.toml << 'EOF'
[project]
name = "stardag_modal"
version = "0.0.1"
requires-python = ">=3.10"
dependencies = ["stardag[modal]", "modal"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
EOF
mkdir stardag_modal
touch stardag_modal/__init__.py
touch stardag_modal/main.py
```

And install it:

```sh
uv sync
```

Now in `stardag_modal/main.py` let's define some minimal tasks that we can compose into a DAG:

```{.python notest}
# stardag_modal/main.py
import modal
import stardag as sd
import stardag.integration.modal as sd_modal


@sd.task(name="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))


@sd.task(name="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)

```

Then let's define the modal image we will be using:

```{.python notest}
# stardag_modal/main.py continued...

# Define the Modal image
image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_sync()
    .add_local_python_source("stardag_modal")
)

# Define the StardagApp
app = sd_modal.StardagApp(
    "stardag-poc",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            # required for communication with registry
            modal.Secret.from_name("stardag-api-key"),
        ],
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image),
    },
)
```

And finally a main section for building the DAG on modal:

```{.python notest}
# stardag_modal/main.py continued...

if __name__ == "__main__":
    dag = get_sum(integers=get_range(limit=21))
    res = app.build_spawn(dag)
    print(res)
```

## Prerequisites

=== "uv"

    ```sh
    uv add stardag[modal]
    ```

=== "pip"

    ```sh
    pip install stardag[modal]
    ```

You'll also need a [Modal](https://modal.com/) account. [Sign up](https://modal.com/apps) and authenticate you local workstation:

=== "Active venv"

    ```sh
    modal token new
    ```

=== "uv run ..."

    ```sh
    uv run modal token new
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

# Define the Modal image with Stardag installed
image = sd_modal.with_stardag_on_image(
    modal.Image.debian_slim(python_version="3.12")
).add_local_python_source("my_package")

# Define the StardagApp
app = sd_modal.StardagApp(
    "my-stardag-app",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            modal.Secret.from_name("stardag-api-key"),
        ],
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image),
    },
)
```

The `sd_modal.with_stardag_on_image()` helper installs the correct version of Stardag on your image.

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

Deploy using the Stardag CLI:

```sh
stardag modal deploy my_package/app.py
```

Run your DAG:

```python
# my_package/main.py
from my_package.app import app
from my_package.tasks import get_range, get_sum

if __name__ == "__main__":
    dag = get_sum(integers=get_range(limit=10))
    res = app.build_spawn(dag)
    print(res)
```

You'll see the `build` and `worker_default` functions invoked in the Modal UI.

## Running the Examples

The examples package includes ready-to-run Modal examples:

=== "uv"

    ```sh
    cd lib/stardag-examples
    uv sync --extra modal

    # Deploy basic example
    stardag modal deploy stardag_examples/modal/basic/app.py

    # Run
    uv run python -m stardag_examples.modal.basic.main
    ```

=== "pip"

    ```sh
    cd lib/stardag-examples
    pip install -e ".[modal]"

    # Deploy basic example
    stardag modal deploy stardag_examples/modal/basic/app.py

    # Run
    python -m stardag_examples.modal.basic.main
    ```

## With Prefect Observability

For production workloads, combine Modal with Prefect for observability.

=== "uv"

    ```sh
    cd lib/stardag-examples
    uv sync --extra modal --extra prefect --extra ml-pipeline

    # Deploy
    stardag modal deploy stardag_examples/modal/prefect/app.py

    # Run
    uv run python -m stardag_examples.modal.prefect.main
    ```

=== "pip"

    ```sh
    cd lib/stardag-examples
    pip install -e ".[modal,prefect,ml-pipeline]"

    # Deploy
    stardag modal deploy stardag_examples/modal/prefect/app.py

    # Run
    python -m stardag_examples.modal.prefect.main
    ```

### App Configuration

```python
# app.py
import modal
import stardag.integration.modal as sd_modal

# Define the Modal image with Stardag and dependencies
image = sd_modal.with_stardag_on_image(
    modal.Image.debian_slim(python_version="3.12").pip_install(
        # Helper to pull dependencies from pyproject.toml
        sd_modal.get_package_deps(__file__, optional=["prefect", "ml-pipeline"]),
    )
).add_local_python_source("stardag_examples")

app = sd_modal.StardagApp(
    "my-app-with-prefect",
    builder_type="prefect",  # Enable Prefect orchestration
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            # Contains PREFECT_API_KEY and PREFECT_API_URL
            modal.Secret.from_name("prefect-api"),
            # Contains STARDAG_API_KEY
            modal.Secret.from_name("stardag-api-key"),
        ],
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
# main.py
import stardag as sd

from stardag_examples.ml_pipeline.class_api import get_benchmark_dag
from stardag_examples.modal.prefect.app import app


def worker_selector(task: sd.BaseTask) -> str:
    if task.get_name() == "TrainedModel":
        return "large"  # Heavy computation
    return "default"


if __name__ == "__main__":
    dag = get_benchmark_dag()
    res = app.build_spawn(dag, worker_selector=worker_selector)
    print(res)
```

### View in Prefect UI

Tasks run concurrently as soon as their dependencies complete:

![Prefect UI showing concurrent task execution](https://github.com/user-attachments/assets/2f0d9db7-e9b7-4138-91c8-5973073dcd62)

## GPU Support

Configure GPU workers for ML training:

```python
gpu_image = sd_modal.with_stardag_on_image(
    modal.Image.debian_slim().pip_install("torch")
)

app = sd_modal.StardagApp(
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

### Helper Functions

| Function                                       | Description                                          |
| ---------------------------------------------- | ---------------------------------------------------- |
| `sd_modal.with_stardag_on_image(image)`        | Install Stardag on a Modal image                     |
| `sd_modal.get_package_deps(path, optional=[])` | Get dependencies from pyproject.toml for pip_install |

## See Also

- [ML Pipeline Example](ml-pipeline-example.md) - Complete ML pipeline walkthrough
- [Integrate with Prefect](integrate-prefect.md) - Prefect orchestration
- [Modal Examples](https://github.com/stardag-dev/stardag/tree/main/lib/stardag-examples/src/stardag_examples/modal) - Source code
- [Modal Documentation](https://modal.com/docs) - Modal features
