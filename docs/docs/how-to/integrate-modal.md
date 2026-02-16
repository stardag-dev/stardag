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

We recommend setting up the Stardag Registry.

You can also run Stardag on Modal, completely without the Registry.

=== "With Registry"

    Sign up at `app.stardag.com` or follow [the setup guide](../getting-started/registry-ui.md#get-setup) for running it self-hosted.

=== "Without Registry"

    You're all set. Just skip using a Stardag API-key in the examples.

## Minimal Example from Scratch

We are going to create a new minimal Python project with the following structure:

```
stardag-modal/
├── stardag_modal/
│   ├── __init__.py
│   └── main.py
└── pyproject.toml
```

### Create and install the project

Create the new project (with `uv` as build system):

```sh
mkdir stardag-modal
cd stardag-modal
cat > pyproject.toml << 'EOF'
[project]
name = "stardag_modal"
version = "0.0.1"
requires-python = ">=3.12"
dependencies = ["stardag[modal]>=0.1.2", "modal"]

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
import sys

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

=== "With Registry"

    ```{.python notest}
    # stardag_modal/main.py continued...

    # Must match local Python version for Modal serialization compatibility
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    # Define the Modal image
    image = (
        modal.Image.debian_slim(python_version=python_version)
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

=== "Without Registry"

    ```{.python notest}
    # stardag_modal/main.py continued...

    # Must match local Python version for Modal serialization compatibility
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    # Define the Modal image
    image = (
        modal.Image.debian_slim(python_version=python_version)
        .uv_sync()
        .add_local_python_source("stardag_modal")
    )

    # Define the StardagApp
    app = sd_modal.StardagApp(
        "stardag-poc",
        builder_settings=sd_modal.FunctionSettings(image=image),
        worker_settings={
            "default": sd_modal.FunctionSettings(image=image),
        },
    )
    ```

And finally, compose the tasks and add a main section for building them on modal:

```{.python notest}
# stardag_modal/main.py continued...

root_task = get_sum(integers=get_range(limit=21))

if __name__ == "__main__":
    res = app.build_spawn(root_task)
    print(res)
```

Now that we have the code in place and the `stardag` and `modal` Python packages installed, we need to set up the environment before we can run the example.

### Set up your Modal environment

Authenticate with modal (if you haven't already):

=== "Active venv"

    ```sh
    modal token new
    ```

=== "uv run ..."

    ```sh
    uv run modal token new
    ```

If you've created and want to use a dedicated Modal environment, make sure to also set:

```sh
export MODAL_ENVIRONMENT=<my-env>
```

### Set up your Stardag environment

When running Stardag on Modal, we must use a remote filesystem for our [target roots](../concepts/targets.md#target-roots). A natural choice when running on Modal is to use Modal volumes:

=== "With Registry"

    Create a new isolated Stardag environment:

    === "Active venv"


        ```sh
        stardag environment create "Modal PoC" --target-root "default=modalvol://stardag-poc/target-roots/default"
        ```

    === "uv run ..."

        ```sh
        uv run stardag environment create "Modal PoC" --target-root "default=modalvol://stardag-poc/target-roots/default"
        ```

    Add and activate a new profile for the environment:

    === "Active venv"


        ```sh
        stardag config profile add modal-poc --env modal-poc --default
        ```

    === "uv run ..."

        ```sh
        uv run stardag config profile add modal-poc --env modal-poc --default
        ```


    We also need to give modal functions access to the Stardag Registry:

    === "Active venv"

        ```sh
        stardag modal stardag-api-key create
        ```

    === "uv run ..."

        ```sh
        uv run stardag modal stardag-api-key create
        ```

=== "Without Registry"

    Point the default target root to a Modal Volume via the environment variable:

    ```sh
    export STARDAG_TARGET_ROOTS__DEFAULT="modalvol://stardag-poc/target-roots/default"
    ```

### Deploy the app

Now let's deploy the app to Modal.

=== "Active venv"

    ```sh
    stardag modal deploy stardag_modal/main.py
    ```

=== "uv run ..."

    ```sh
    uv run stardag modal deploy stardag_modal/main.py
    ```

You should see output like:

```
Using active stardag profile
  Registry URL: https://api.stardag.com
  Workspace ID: <ws-id>
  Environment ID: <env-id>
  Target roots:
    default: modalvol://stardag-poc/target-roots/default
Modal volumes:
  default: stardag-poc
Functions:
  build
  worker_default
✓ Created objects.
├── 🔨 Created mount PythonPackage:stardag_modal
├── 🔨 Created mount PythonPackage:stardag
├── 🔨 Created function build.
└── 🔨 Created function worker_default.
✓ App deployed in 2.592s! 🎉

View Deployment: https://modal.com/apps/<modal-user>/<modal-env>/deployed/stardag-poc
```

You can also navigate to your modal apps in the relevant environment and should see:

![Deployed Stardag app in modal](https://github.com/user-attachments/assets/631cf248-8df9-4a45-9de8-50f7e9128e53)

### Run the app

Now let's execute the `main.py` module:

=== "Active venv"

    ```sh
    python stardag_modal/main.py
    ```

=== "uv run ..."

    ```sh
    uv run python stardag_modal/main.py
    ```

Then navigate to the app in the Modal UI to follow the execution progress.

### Inspect the results

The easiest way to get the results is to use an instance of the desired task and load its output.

=== "Active venv"

    ```sh
    python -c "from stardag_modal.main import root_task; \
        print(root_task.output().uri); \
        print(root_task.output().load())"
    ```

=== "uv run ..."

    ```sh
    uv run python -c "from stardag_modal.main import root_task; \
        print(root_task.output().uri); \
        print(root_task.output().load())"
    ```

Output:

```
modalvol://stardag-poc/target-roots/default/Sum/e0/e6/e0e66321-c097-534f-b2ae-a95e51ff9373.json
210
```

You can also "tab" your way through the DAG dependencies to access `root_task.integers`:

=== "Active venv"

    ```sh
    python -c "from stardag_modal.main import root_task; \
        print(root_task.integers.output().load())"
    ```

=== "uv run ..."

    ```sh
    uv run python -c "from stardag_modal.main import root_task; \
        print(root_task.integers.output().load())"
    ```

If you connected to the Stardag Registry, you can also click the latest build to inspect the DAG execution.

![modal-poc dag in the Registry UI](https://github.com/user-attachments/assets/08e2d3b1-17f5-4b3d-b6ed-1b91c8a3f968)

<!-- TODO below needs significant cleanup.
## Running the `stardag-examples` Examples


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
import sys

import modal
import stardag.integration.modal as sd_modal

# Must match local Python version for Modal serialization compatibility
python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

# Define the Modal image with Stardag and dependencies
image = sd_modal.with_stardag_on_image(
    modal.Image.debian_slim(python_version=python_version).pip_install(
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

-->

## See Also

- [Stardag Modal Examples](https://github.com/stardag-dev/stardag/tree/main/lib/stardag-examples/src/stardag_examples/modal) - Ready-to-run Modal examples in the `stardag-examples` package.
- [Modal Documentation](https://modal.com/docs) - Modal features
- [ML Pipeline Example](ml-pipeline-example.md) - Complete ML pipeline walkthrough
- [Integrate with Prefect](integrate-prefect.md) - Prefect orchestration
