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

    Sign up at [app.stardag.com](https://app.stardag.com) or follow [the setup guide](../getting-started/registry-ui.md#get-setup) for running it self-hosted.

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
        stardag config profile add modal-poc -e modal-poc --default
        ```

    === "uv run ..."

        ```sh
        uv run stardag config profile add modal-poc -e modal-poc --default
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
        print(root_task.target().uri); \
        print(root_task.load())"
    ```

=== "uv run ..."

    ```sh
    uv run python -c "from stardag_modal.main import root_task; \
        print(root_task.target().uri); \
        print(root_task.load())"
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
        print(root_task.integers.load())"
    ```

=== "uv run ..."

    ```sh
    uv run python -c "from stardag_modal.main import root_task; \
        print(root_task.integers.load())"
    ```

If you connected to the Stardag Registry, you can also click the latest build to inspect the DAG execution.

![modal-poc dag in the Registry UI](https://github.com/user-attachments/assets/08e2d3b1-17f5-4b3d-b6ed-1b91c8a3f968)

### Restart-safe triggering with `build_trigger` (recommended with Registry)

With `build_spawn`, the registry build id is created _inside_ the Modal build
container — if that container is restarted (preemption, timeout, a Modal-level
retry), the new invocation starts a **new** build in the registry.

When you use the Stardag Registry, prefer `build_trigger`: it creates the
build in the registry from the calling process first, then passes the build id
to the build function as `resume_build_id`. Any restart of the build function
then _resumes_ the same build — tasks whose outputs already exist are detected
during discovery and skipped:

```{.python notest}
result = app.build_trigger(root_task)
print(result.build_id)       # registry build id, minted at the trigger point
result.function_call.get()   # optionally block on the Modal build function
```

Re-triggering with the same build id re-attaches to the build (e.g. after a
failure, or to bring a preempted build back up):

```{.python notest}
app.build_trigger(root_task, build_id=result.build_id)
```

To let Modal restart the build function automatically after infrastructure
failures (and thereby auto-resume the build), configure retries on the
builder:

```{.python notest}
app = sd_modal.StardagApp(
    "stardag-poc",
    builder_settings=sd_modal.FunctionSettings(image=image, retries=2),
    worker_settings={"default": sd_modal.FunctionSettings(image=image)},
)
```

Note that `build_trigger` requires registry credentials in the calling process
(the active stardag profile), in addition to Modal credentials — unlike
`build_spawn`, which only needs Modal credentials locally.

### Detached execution: running tasks survive restarts

Tasks are executed as _detached_ Modal function calls by default: the worker
invocation is spawned (not held open by a blocking call), and its function
call id is recorded in the registry with the task's started event. When a
build is resumed (via `resume_build_id` / `build_trigger`), tasks that are
still running in live workers are **re-attached instead of re-executed** — a
preempted or restarted build function does not restart your long-running
tasks. This also applies across builds: if another build is already running
the same task, the new build attaches to that execution rather than
duplicating it.

Detached mode also makes cancellation real: when a build fails fast or is
cancelled, the tracked function calls are explicitly cancelled on Modal
(with the legacy blocking mode, workers of a dead build kept running to
completion).

Workers additionally report their own lifecycle events (started, completed
with artifacts, suspended, failed) directly to the registry when the app has
registry credentials — so a task's registry state stays accurate even if the
build function dies while the task is running. If you drive a deployed app
built with an older stardag version from a newer local SDK, pass
`ModalTaskExecutor(worker_reports_lifecycle=False)` (or redeploy the app) so
the build engine doesn't skip events the old workers won't send.

Modal executions also record descriptive **executor metadata** with task
starts and triggered builds — the Modal app name, workspace, environment,
and function name — which the Stardag UI surfaces (e.g. as deep links to
the Modal dashboard). Resolution is automatic and best-effort (the
workspace comes from a cached Modal token lookup); pass
`StardagApp(modal_workspace=...)` to set the workspace name explicitly.

### Reactive scheduling: no resident build function (experimental)

With `build_trigger(..., reactive=True)` the build runs with **no resident
orchestrator at all**: task discovery happens at the trigger, and the build
is driven by short-lived scheduler _ticks_ — spawned when the build is
triggered, whenever a worker finishes a task, and (recommended) by a
periodic watchdog. Between ticks, nothing runs except your tasks: a
multi-day build with a few long-running tasks costs no orchestrator
container time, and there is no orchestrator process whose crash could
affect the build.

```{.python notest}
app = sd_modal.StardagApp(
    "stardag-poc",
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={"default": sd_modal.FunctionSettings(image=image)},
    # Recommended with reactive mode: periodically re-check running builds
    # (covers lost wake-ups and builds cancelled from the UI).
    watchdog_period_minutes=5,
)

# After deploy:
result = app.build_trigger(root_task, reactive=True)
# Re-trigger with the same build id to wake a stalled build — or to add
# new root tasks to the running build:
app.build_trigger(more_tasks, build_id=result.build_id, reactive=True)
```

Requirements and current limitations: the app must be deployed with this
stardag version — **both** the Modal app and the registry server (an older
server fails reactive triggers with a "does not support reactive
scheduling" error); the triggering process needs registry credentials and
access to the default target root (task objects are persisted there for
the ticks); the global concurrency lock and build-local
`ConcurrencyConfig` limits are not applied by ticks (use the
registry-backed named limits above; Modal's per-function
`concurrency_limit` also still applies). Builds cancelled from the
registry UI are picked up by the next tick (within the watchdog period),
which cancels the running Modal function calls; on failure, tasks
transitively blocked by the failed task are marked skipped.

Two operational notes:

- **Avoid redeploying the app with changed task definitions while
  reactive builds are in flight.** Task objects are persisted as pickles
  for the scheduler; if a stored pickle becomes unloadable (e.g. after a
  redeploy), the tick falls back to **reconstructing the task from the
  registry's stored data** — which works as long as the task class is
  still importable and its fields are compatible (nested task fields
  must use `sd.TaskLoads`/`sd.SubClass` annotations). Only if both paths
  fail is the task failed by the next tick (never a silent stall).
- **The watchdog sweep runs one quick scheduling pass per running build**
  (it skips the linger), so its per-period cost is one short function
  invocation plus a frontier query per running build.
- **Define the callables you pass to `StardagApp` in an importable
  module.** `worker_selector`, `limit_key_selector`, and any custom
  build/run functions are captured by the serialized Modal functions
  (build, workers, and the reactive `tick` / `tick_watchdog`), which Modal
  deserializes in fresh containers — including the scheduled watchdog,
  which always runs cold. A plain module-level function is pickled _by
  reference_, so its defining module must be importable in the container:
  put these callables in a module that is part of the source you add via
  `add_local_python_source(...)`, **not** in a loose deploy script. A
  selector defined directly in a script deployed by path
  (`stardag modal deploy app.py`, which Modal loads as a top-level module
  named `app`) deserializes to `ModuleNotFoundError: No module named
'app'` on the first cold container. (The `modal/basic` example is
  unaffected only because it passes no such callables; the
  `modal/walkthrough` example keeps its selectors in a dedicated
  `selectors.py` for exactly this reason.)

Named concurrency limits are enforced registry-side in reactive mode —
across builds, not just within one. Configure caps per environment
(`PUT /api/v1/concurrency-limits/{key}` with `{"max_concurrent": N}`) and
tag tasks with keys on the app (deployed configuration, applied
consistently by every scheduler tick):

```{.python notest}
app = sd_modal.StardagApp(
    "stardag-poc",
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={"default": sd_modal.FunctionSettings(image=image)},
    watchdog_period_minutes=5,
    limit_key_selector=lambda task: ["gpu"] if needs_gpu(task) else [],
)
```

A task denied by a limit stays pending and is retried when a slot frees
(immediately for same-build releases; within the watchdog period for
releases in other builds).

When limits are enforced, **the watchdog is strongly recommended**
(`watchdog_period_minutes=5`): a slot is freed by the holder reaching a
terminal status, and the watchdog is the safety net that keeps statuses
honest when wake-ups are lost — including the escape hatch that fails a
task stuck RUNNING without an execution ref (default after 30 minutes,
`TickConfig.stale_running_no_ref_seconds`), which would otherwise hold
its slots indefinitely. Also note that limit-key tags recorded at a
task's start persist until its next start _with_ keys — a later build
re-running the same task id without tags briefly counts under the old
keys while RUNNING.

Server requirement: concurrency-limit enforcement (like reactive mode as
a whole) needs a stardag-api version matching this SDK — an **older
server silently ignores the enforcement parameters**, so upgrade the
server before relying on limits.

**App ownership.** Each reactive build is owned by the `StardagApp`
that triggered it (`app_name` recorded in the build's store meta). With
several apps deployed in one environment, every watchdog sweeps all
running reactive builds — but a tick from a non-owning app never drives
the build with its own commit's code and selectors (or unpickles the
owner's task store, which may not match its code). Instead it
**forwards**: it spawns the owner app's tick (best-effort) and returns
`outcome="foreign_app"` — so wake-ups that land on the wrong app are
not lost, and every app's watchdog doubles as cross-app coverage (the
owner-side scheduler lease collapses duplicate forwards). Redeploying
the **same** app name is the normal upgrade path and unaffected.

To migrate a build to a different app, re-trigger it from that app
(`build_trigger(tasks, reactive=True, build_id=<existing id>)`): the
re-trigger rewrites the meta and re-persists the task objects under the
new app's code. Two handoff details: ownership takes effect for _new_
ticks — a tick of the previous owner that is mid-linger keeps driving
the build until its linger deadline passes (bounded by its
`linger_seconds`); and wake-ups from the previous owner's still-running
workers reach the new owner via the forwarding above. Symptom worth
knowing: a build not progressing while tick logs show `foreign_app`
with failed forwards means the owning app was **deleted** — the build
is orphaned; re-trigger it from a live app to adopt it.

The same named limits can be enforced from resident (non-reactive)
builds via `stardag.build.RegistryConcurrencyLimiter` — both modes share
the slots. Two caveats when mixing modes: a crashed _resident_ build has
no automatic healer (its RUNNING task holds the slot until explicitly
failed/cancelled via the API/UI — the worker-reporting/tick self-healing
story above is reactive-only), and a legitimately long-running ref-less
resident task that also appears in a concurrently ticking reactive build
can be force-failed by that build's `stale_running_no_ref_seconds`
escape hatch — raise the bound if you mix modes over the same long
tasks.

Requirements and current limitations: the app must be deployed with this
stardag version (scheduler `tick` function + self-reporting workers); the
triggering process needs registry credentials and access to the default
target root (task objects are persisted there for the ticks); the global
concurrency lock and build-local `ConcurrencyConfig` limits are not applied
by ticks (use the registry-backed named limits above; Modal's per-function
`concurrency_limit` also still applies). Builds cancelled from the registry UI are picked up by
the next tick (within the watchdog period), which cancels the running
Modal function calls.

To opt out (legacy blocking `remote` calls), pass `detached=False`:

```{.python notest}
app = sd_modal.StardagApp(
    "stardag-poc",
    build_function=sd_modal.Builder(detached=False),
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={"default": sd_modal.FunctionSettings(image=image)},
)
```

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
