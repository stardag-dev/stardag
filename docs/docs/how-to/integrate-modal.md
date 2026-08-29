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

    # Define the StardagApp. The Stardag Registry API key is injected into
    # every function automatically from the `stardag-api-key` Modal secret
    # (created below via `stardag modal stardag-api-key create`); see the
    # `stardag_api_key_secret` argument to override the name/secret or set
    # it to None if you supply the key another way.
    app = sd_modal.StardagApp(
        "stardag-poc",
        builder_settings=sd_modal.FunctionSettings(image=image),
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
orchestrator at all**. The trigger mints the build, registers the root
tasks and spawns one deployed function — `bootstrap` — with those roots
passed by value; everything else is driven by short-lived scheduler
_ticks_, spawned by the bootstrap, whenever a worker finishes a task, and
(recommended) by a periodic watchdog. Between ticks, nothing runs except
your tasks: a multi-day build with a few long-running tasks costs no
orchestrator container time, and there is no orchestrator process whose
crash could affect the build.

**Triggering is fast and does no target I/O.** Discovering a DAG means one
target existence check per task, and the trigger performs none of them:
the `bootstrap` container walks the DAG, registers it, persists the task
objects and only then arms the build and spawns the first tick. The
difference is not cosmetic for a `modalvol://` target root — inside Modal
the volume is a mounted filesystem, while from your laptop each check is a
rate-limited Volume API call. Triggering a wide DAG used to spend most of
its time in rate-limit backoff; now it returns as soon as the spawn is
acknowledged. It also means a reactive trigger needs **registry
credentials only** — no target-root access at all.

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
# Re-trigger with the same build id to wake a stalled build, to add new
# root tasks to the running build, or to change the tick configuration:
app.build_trigger(
    more_tasks, build_id=result.build_id, reactive=True,
    tick_kwargs={"linger_seconds": 60},
)
```

Requirements and current limitations: the app must be deployed with this
stardag version — **both** the Modal app and the registry server (an older
server fails reactive triggers with a "does not support reactive
scheduling" error, and an app deployed before the `bootstrap` function
existed has nothing to spawn — see `reactive_discovery` below); the
triggering process needs registry credentials (task _objects_ are
persisted to the target root as pickles for the ticks, but that write now
happens inside the `bootstrap` container — the reactive marker, owning
app, and tick config live in the registry, not on the target root, so
re-triggering works even when the target root is immutable/append-only and
a re-trigger may update `tick_kwargs`; declaring
[`task_modules`](#declaring-your-task-modules-recommended) removes the
target-root write entirely for most builds); the global
concurrency lock and build-local
`ConcurrencyConfig` limits are not applied by ticks (use the
registry-backed named limits above; Modal's per-function
`concurrency_limit` also still applies). Builds cancelled from the
registry UI are picked up by the next tick (within the watchdog period),
which cancels the running Modal function calls; on failure, tasks
transitively blocked by the failed task are marked skipped.

**Sizing the bootstrap.** `bootstrap` is a separate deployed function
rather than work folded into the first tick, because the two want
different timeouts. A tick is one frontier pass and is meant to be short
(its timeout also derives the per-pass spawn cap); the bootstrap is a
single whole-DAG walk whose cost scales with the DAG and is paid once per
trigger. One number cannot honestly cover both — shortening the tick,
normally a good idea, would start killing the bootstrap of large DAGs. It
defaults to `builder_settings` (same image, secrets and target-root
volume mounts as the builder, which does the same discovery for resident
builds); override it with `bootstrap_settings`:

```{.python notest}
app = sd_modal.StardagApp(
    "stardag-poc",
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={"default": sd_modal.FunctionSettings(image=image)},
    tick_settings=sd_modal.FunctionSettings(image=image, timeout=300),
    # Discovery of a very wide DAG gets its own budget.
    bootstrap_settings=sd_modal.FunctionSettings(image=image, timeout=1800),
    watchdog_period_minutes=5,
)
```

**Failures leave no orphan builds.** A reactive trigger mints a `RUNNING`
build and then walks away, so both sides of the spawn record a terminal
`BUILD_FAILED` before propagating: the trigger for anything that goes
wrong once it knows the build is running (a re-trigger whose `build_resume`
fails is deliberately excluded — until that lands the build may still be
terminal, and failing it would misattribute someone else's outcome), and
the bootstrap for anything that goes wrong in its container, including a
failed first-tick spawn. The bootstrap's exception also surfaces on
`result.function_call.get()`.

**Running discovery locally instead.** `StardagApp(reactive_discovery=
"local")` runs the identical bootstrap in the triggering process — the
behaviour reactive triggers had before the `bootstrap` function existed.
Reach for it when the deployed app predates that function, or when the
target root is reachable from your machine but not from the Modal app.
Note that it also puts the coverage pre-flight below on your _local_
`task_modules` rather than the deployed one, reinstating the stale-deploy
blind spot.

Two operational notes:

- **Avoid redeploying the app with changed task definitions while
  reactive builds are in flight.** Task objects are persisted as pickles
  for the scheduler; if a stored pickle becomes unloadable (e.g. after a
  redeploy), the tick falls back to **reconstructing the task from the
  registry's stored data** — which works as long as the task class is
  still importable and its fields are compatible (nested task fields
  must use `sd.TaskLoads`/`sd.SubClass` annotations). Only if both paths
  fail is the task failed by the next tick (never a silent stall).
  Declaring [`task_modules`](#declaring-your-task-modules-recommended) is
  what makes "still importable" true by construction — and lets stardag
  skip the pickle in the first place.
- **The watchdog sweep runs one quick scheduling pass per running build
  that this app owns** (it skips the linger), so its per-period cost is
  one short function invocation plus a frontier query per such build.
  The sweep asks the registry only for RUNNING builds whose reactive
  owner is this app, so unrelated builds in the environment — resident
  builds, and builds left RUNNING by an orchestrator that died without
  emitting a terminal event — cost nothing and cannot crowd out the
  sweep's per-period cap. A build owned by an app deployed _without_
  `watchdog_period_minutes` therefore has no watchdog covering it, even
  if another app in the environment has one.
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
  named `app`) fails to deserialize on the first cold container with a
  `ModuleNotFoundError` for the `app` module. (The `modal/basic` example
  is unaffected only because it passes no such callables; the
  `modal/walkthrough` example keeps its selectors in a dedicated
  `selectors.py` for exactly this reason.)
- **Every deployed function needs the registry secret** — the workers
  self-report their lifecycle (started/completed/…) and the tick/watchdog
  read and update build state, so all of them make registry calls and
  `401` without credentials. As of stardag 0.10.2 this is handled by
  `StardagApp(stardag_api_key_secret=...)`: the named secret (default
  `"stardag-api-key"`, created by `stardag modal stardag-api-key create`)
  is injected into every function, so you declare it once — or just rely
  on the default and don't declare a registry secret at all. (On stardag
  ≤ 0.10.1 you instead had to put the secret on the builder — 0.10.1 —
  or on every worker — 0.10.0.)

#### Declaring your task modules (recommended)

A scheduler tick is a fresh, short-lived process. It learns _which_ tasks
are actionable from the registry, but to spawn a worker it needs the actual
task _object_ — and it has two ways to get one:

1. unpickle it from the build task store (needs target-root access, and is
   only valid for the deployment that wrote it), or
2. rebuild it from the payload the registry already stores.

The second path is the good one, but it has a catch: rebuilding a task
resolves its class through stardag's polymorphic registry, and classes land
in that registry **as a side effect of importing the module that defines
them**. A pickle carries `module.QualName` and self-imports; the registry
payload carries no module locator at all. So the tick can only rebuild
classes whose modules its container happened to import — which, without
help, is essentially arbitrary.

`task_modules` is that help:

```{.python notest}
app = sd_modal.StardagApp(
    "stardag-poc",
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={"default": sd_modal.FunctionSettings(image=image)},
    watchdog_period_minutes=5,
    # Modules whose import registers the task classes this app may
    # schedule. Default: the root package of the module defining the app.
    # Pass [] to opt out (resident builds never need this).
    task_modules=["my_pkg.tasks.*", "my_pkg.pipelines.*"],
)
```

**Pattern grammar.** Each entry is either an exact module
(`"my_pkg.tasks.ingest"`) or a package followed by a trailing recursive
wildcard (`"my_pkg.tasks.*"`, matching `my_pkg.tasks` and everything below
it). A `*` anywhere but the final component, or a malformed path, raises
from `StardagApp(...)` — a typo must not degrade into a silent no-match.
Left unset, the default is `"<root package of the module defining the
app>.*"`; if the app lives in `__main__` or a loose script (no importable
package), inference is impossible and stardag warns and falls back to the
pickle path.

**A redeploy is required** when you add or move task classes. The patterns
are expanded to a concrete module list at deploy time and baked into the
deployed tick, so the deployed set is explicit and auditable and container
startup does no filesystem walking. `stardag modal deploy` reports it:

```text
Task modules: 37 discovered from "my_pkg.*"  ->  128 task classes registered
```

The class count requires importing the modules locally, which the CLI does
by default but **warn-only** — your deploy environment may lack extras the
image has, so a local import failure never fails the deploy. Pass
`--no-check-task-modules` to skip the check and report names only.

**What you get.** Once you declare `task_modules` explicitly, every
discovered task whose class is covered _and_ whose payload round-trips to
the same task id is persisted **without a pickle**. A build whose classes
are all covered writes nothing to the target root at all. Set
`require_pickle_free=True` to turn the fallback into a hard error that
names every task that would have needed a pickle and why — enforced in
the `bootstrap` container, where the task store is written, and loud:
it fails the build in the registry _and_ propagates on
`result.function_call.get()`.

**Skipping pickles requires the explicit declaration** — the inferred
default never elides on its own. Inference happens for every app,
including apps written before this feature existed. If inference alone
skipped pickles, upgrading stardag would silently start dropping pickles
that an app deployed by an older version has no baked-in module list to
compensate for. Requiring you to write the argument is what puts the
redeploy requirement in front of you at the moment it matters. Inference
still drives the coverage warning below, which only observes.

Some payloads stay pickle-bound by design, and always will:

- **`AliasTask`**, whose `loads_type` is pickled bytes — auto-unpickling
  registry-supplied bytes inside a scheduler tick would be a remote code
  execution vector, so rehydration refuses those payloads outright;
- **dynamically generated or otherwise non-importable classes**;
- **anything whose serialization is not losslessly round-trippable** (in
  particular, nested task fields must use `sd.TaskLoads` / `sd.SubClass`
  annotations — a plain task-typed annotation validates children into the
  abstract base class).

**The coverage check** warns — naming the class, the pattern to add, and
the redeploy requirement — for any discovered class the patterns don't
cover. It is a warning rather than an error because an uncovered class
still works via the pickle path, exactly as before this feature existed.
It runs wherever discovery runs, i.e. in the `bootstrap` container, over
the real discovered set, against the module list **the deployment baked
in**. The trigger additionally prints a labelled, roots-only advisory
before spawning, so the common "I never declared my package" case shows
up in your terminal rather than only in the bootstrap's Modal logs; it is
by construction a subset of the real check, never a substitute for it.

Two caveats worth designing around:

- **Task modules become import-hot.** They are imported in every tick
  container, on every cold start. Keep heavy runtime dependencies inside
  `run()` rather than at module scope — good practice regardless, but here
  it directly buys tick cold-start latency.
- **Redeploy whenever you change `task_modules`**, before triggering —
  which reactive mode already requires for other reasons (see the
  requirements above). The coverage check now reads the _deployed_ list,
  so adding a pattern without redeploying is visible rather than silently
  agreeable — but the elision decision is made from that same deployed
  list, so until you redeploy nothing changes. (With
  `reactive_discovery="local"` the check reads your local app definition
  instead and the old stale-deploy blind spot returns: the pre-flight goes
  quiet while the tick still can't resolve the class, with no pickle left
  as a fallback.) Upgrading stardag alone is safe: elision only follows an
  explicit declaration, so a newer SDK triggering against an app deployed
  by an older one still writes pickles. Passing `task_modules=[]` restores
  the pre-feature behaviour unconditionally.

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
task stuck RUNNING without an execution ref once its **execution claim
lapses** (see below), which would otherwise hold its slots indefinitely.
Also note that limit-key tags recorded at a
task's start persist until its next start _with_ keys — a later build
re-running the same task id without tags briefly counts under the old
keys while RUNNING.

Server requirement: concurrency-limit enforcement (like reactive mode as
a whole) needs a stardag-api version matching this SDK — an **older
server silently ignores the enforcement parameters**, so upgrade the
server before relying on limits.

**App ownership.** Each reactive build is owned by the `StardagApp`
that triggered it (`app_name` recorded in the build's reactive metadata in
the registry, read by every tick from the build frontier). With
several apps deployed in one environment, each app's watchdog sweeps only
the builds that app owns. A tick from a non-owning app can still be
triggered — typically a wake-up from a worker still running under a
previous owner — and it never drives the build with its own commit's code
and selectors (or unpickles the owner's task store, which may not match
its code). Instead it **forwards**: it spawns the owner app's tick
(best-effort) and returns `outcome="foreign_app"` — so wake-ups that land
on the wrong app are not lost (the owner-side scheduler lease collapses
duplicate forwards). Redeploying the **same** app name is the normal
upgrade path and unaffected.

To migrate a build to a different app, re-trigger it from that app
(`build_trigger(tasks, reactive=True, build_id=<existing id>)`): the
re-trigger updates the reactive metadata (owning app + tick config) in the
registry and re-persists the task objects under the new app's code. Two
handoff details: ownership takes effect for _new_
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
resident task can be force-failed once its claim lapses if it also appears
in a concurrently ticking reactive build. Resident builds do not derive a
claim TTL from an executor timeout, so such a task gets the registry's
default expiry — keep that in mind if you mix modes over tasks that run
longer than it.

**Builds that overlap.** Task state is per environment, so a task another
build owns blocks yours. A tick waits that out — whether the other build is
executing the task under a live execution claim, or has yet to schedule it
— instead of failing the build. A blocker whose claim has **lapsed**, or a
non-running blocker no _live_ build is going to run, fails the build with a
message naming the task, the build that owns it and why that owner will not
move it. Symptom worth knowing: a tick log line saying the build is _"waiting on
N upstream task(s) owned by other builds … waiting rather than failing"_
means your build is fine and waiting on a neighbour. See
[Cross-build blocking](../concepts/build-execution.md#cross-build-blocking)
for the recovery path when the blocker is abandoned — including a task left
SUSPENDED, which a retry (and therefore a re-trigger) now resets.
`stardag builds frontier <build-id>` shows this directly, naming the blocking
task and the build that owns it — see
[Reading the frontier](../configuration/cli.md#reading-the-frontier).

**Task retries: `retries=` and `max_attempts` are not the same knob.**
`FunctionSettings(retries=N)` on a worker is Modal's own retry policy: it
covers an exception raised _inside_ the container, and it is the right tool
for that. It cannot cover a spawn that failed before the container existed,
a container Modal killed (OOM, timeout), or a preempted worker — from
Modal's side there is nothing to retry, and from the build's side those
used to end a `FAIL_FAST` build outright.

Reactive ticks therefore carry `TickConfig.max_attempts` (default **2**), a
per-task budget on how many executions the _scheduler_ starts in one build
round. It applies only to failures a tick records itself — a failed spawn,
an execution Modal reports as failed, and a task whose execution claim
lapsed with no ref left to probe (the preemption/OOM shape). A task that
simply raises never reaches it: the worker self-reports the failure, which
is what `retries=` is for. Set the two together — `retries=` for flaky task
code, `max_attempts` for flaky infrastructure:

```{.python notest}
app.build_trigger(
    root_task, reactive=True, tick_kwargs={"max_attempts": 3}
)
```

`max_attempts=1` restores the previous behaviour (record the failure, never
respawn).

**A build that ran out of attempts is recovered by re-triggering it.** The
budget is scoped to a build _round_, and re-triggering an existing build id
records `BUILD_RESUMED` ahead of its discovery retries, so every task
starts the new round at zero:

```{.python notest}
# Resets the attempt budget and re-runs what failed. Optionally raise the
# budget for the new round at the same time.
app.build_trigger(
    root_task, build_id=result.build_id, reactive=True,
    tick_kwargs={"max_attempts": 4},
)
```

A **bare** retry does not do this. Clicking Retry in the UI (or running
`stardag tasks retry`) flips the task to pending without starting a new
round, so on a task already at budget the retry succeeds and the scheduler
still refuses to start it. The tick logs that case explicitly, names the
re-trigger, and fails the task again rather than leaving it pending and
inert. See
[Task retries](../concepts/build-execution.md#task-retries-the-failures-no-backend-can-retry-for-you).

### Preemption and timeouts

Two things routinely kill a Modal container without the task being wrong:
Modal **reclaims** the instance, or the execution hits the function
**timeout**. Stardag treats both as _interruptions_ — the attempt ended,
the task did not fail — but they recover by different routes, and the
difference decides what you should write in your task.

The contract below was measured against a live workspace with **modal
client 1.5.0 on 2026-08-12**, and is pinned by the regression tests in
`test_live_semantics.py`. Modal documents some of it and not the rest, so
treat the version as part of the statement.

#### What arrives in your task, and when

| event                          | your code receives                  | when                                        |
| ------------------------------ | ----------------------------------- | ------------------------------------------- |
| Modal reclaims the container   | `KeyboardInterrupt`                 | when the platform decides                   |
| The function `timeout` elapses | `modal.exception.InputCancellation` | at the declared timeout, to the millisecond |
| Someone cancels the call       | `modal.exception.InputCancellation` | when the cancel is issued                   |

Both are **`BaseException`, not `Exception`** — so a bare `except
Exception:` in your task will not catch them, which is deliberate on
Modal's part and load-bearing here.

!!! warning "`except KeyboardInterrupt:` does not catch a timeout"

    `InputCancellation` derives straight from `BaseException`; it is **not**
    a `KeyboardInterrupt`. A handler written for preemption therefore does
    nothing at all on a timeout. Catch `MODAL_INTERRUPTIONS`, which is
    exactly the two of them — see the recipe below.

After the first signal you have roughly **a minute** before the container
is killed (Modal escalates SIGUSR1 → SIGINT after ~30s → SIGKILL after
another ~30s). That is enough to write a checkpoint. It also means a
worker's `timeout` does not bound how long its container lives: budget
`timeout + ~60s`.

!!! danger "Catch the interruption types, never `BaseException`"

    `except BaseException:` looks like the way to cover both signals. It is
    not: a `NameError` is a `BaseException` too, so a blanket catch sweeps
    up ordinary bugs, and re-raising `ResumableInterruption` for one turns a
    deterministic failure into a task that resumes until its budget runs
    out. Catch `MODAL_INTERRUPTIONS` — exactly `KeyboardInterrupt` and
    `modal.exception.InputCancellation`, and nothing else.

    `except KeyboardInterrupt:` is equally wrong in the other direction: it
    misses the timeout entirely, so a training task silently never
    checkpoints.

#### The recipe

Everything you need is one `try/except` and one exception:

```{.python notest}
import stardag as sd
from stardag.integration.modal import MODAL_INTERRUPTIONS


class TrainModel(sd.TargetTask[sd.DirectoryTarget]):
    seed: int = 0

    def target(self) -> sd.DirectoryTarget:
        return sd.get_directory_target(sd.get_default_relpath(self))

    def run(self):
        directory = self.target()              # bind once, see below
        checkpoint = directory / "checkpoint.json"

        state = {"step": 0}
        if checkpoint.exists():
            with checkpoint.open("r") as f:
                state = json.load(f)

        try:
            while state["step"] < TOTAL_STEPS:
                train_one_step(state)
                state["step"] += 1
        except MODAL_INTERRUPTIONS:            # preemption OR the timeout
            with checkpoint.open("w") as f:
                json.dump(state, f)
            raise sd.ResumableInterruption("checkpointed") from None

        with (directory / "model.pkl").open("wb") as f:
            f.write(serialize(model))
        directory.mark_done()                  # only now is the task complete
```

Three things carry it:

- **`MODAL_INTERRUPTIONS`** is the exact pair the platform raises. Importing
  it keeps `modal.exception` out of your task and makes being specific the
  easy thing to write.
- **`sd.ResumableInterruption` is the whole request.** Raising it is how a
  task says "I saved my progress, run me again", and it is the only way a
  task gets resumed.
- **The checkpoint lives inside the task's own directory target**, and
  `mark_done()` is what makes the task complete. Writing a checkpoint does
  not — `DirectoryTarget.exists()` is backed by a `._DONE` flag file — so
  progress and completion cannot be confused.
- **`TargetTask`, not `Task`.** `sd.Task` picks your target from its
  serializer and types `target()` as the serializer's
  `LoadableSaveableFileSystemTarget`, so returning a bare `DirectoryTarget`
  from it does not typecheck. `sd.TargetTask[sd.DirectoryTarget]` is the
  base for a task that owns its target, with `complete()` derived from it.
- **Bind the directory once.** `target()` builds a _new_ `DirectoryTarget`
  every call, and each instance remembers only the sub-targets it handed
  out via `/`. Call it once for the checkpoint and again for
  `mark_done()`, and the instance that marks done has never seen your
  files, so it writes an empty `._SUB_KEYS` manifest beside them.
  Completion still works — that is the separate `._DONE` flag — but the
  directory's own listing of its contents comes out blank.

#### What happens if you _don't_ catch it

Nothing to configure, and this is the part worth understanding: **an
interruption you do not catch is a failure.** The execution dies, a
scheduler tick notices, and the task is retried under the ordinary
`TickConfig.max_attempts` (default 2) like any other failure.

That is deliberate. Letting an interruption propagate means the task had no
plan for one, which leaves exactly two possibilities — it hung, or the
worker's `timeout` is too small for the work — and neither is improved by
running it twenty more times.

So there is no "is this timeout expected?" setting anywhere. The task
answers that by raising `ResumableInterruption` or not, and a task that is
not built to resume simply never raises it.

A task that _does_ ask is bounded by `TickConfig.max_interruptions`
(default 20), a budget separate from `max_attempts` — a trainer designed to
be killed and resumed would otherwise exhaust a budget meant for genuine
failures and fail the build for the one reason it was built to survive.

!!! note "One path that budget does not cover"

    A resumption request raised **before** the function timeout is handled
    by Modal restarting the input, not by the scheduler — no event, no
    attempt, no `interrupt_count`, and that restart is ungated by
    `retries`. It is what makes preemption recovery fast, and preemption is
    rare. But a task that raises `ResumableInterruption` on a condition
    that is *always* true would loop at full container cost with
    `max_interruptions` never consulted. Raise it only for interruptions
    you did not choose.

#### The knobs, and how they multiply

| knob                                | covers                                                           |
| ----------------------------------- | ---------------------------------------------------------------- |
| `FunctionSettings(timeout=)`        | how long one execution attempt may run                           |
| `FunctionSettings(retries=)`        | exceptions raised inside the container, and timeouts             |
| `FunctionSettings(nonpreemptible=)` | opts out of reclamation entirely (3× CPU/memory price; no GPU)   |
| `TickConfig.max_attempts`           | failures a tick records itself — spawn failures, dead executions |
| `TickConfig.max_interruptions`      | how many times a task may ask to be resumed                      |

They **multiply**, which is easy to miss: a worker with `retries=3` running
a task allowed 20 interruptions can consume up to 80 container attempts.
Each Modal retry also gets a fresh `timeout` window.

Two things `retries=` does _not_ do, both verified rather than assumed: it
is not what recovers a **preempted or crashed** container (Modal restarts
those on the same input regardless of the setting), and it cannot rescue a
**timed-out** call once the timeout has fired — at that point the call
resolves `FunctionTimeoutError` whatever your code does next, including
catching the signal and returning normally. That is why a timeout is
reported to the registry: the event is the only path back into the
frontier.

If a task genuinely cannot be interrupted, `nonpreemptible=True` is the
honest answer — at 3× the CPU and memory price, and not available for GPU
functions.

!!! note "Registry version"

    Interruption reporting needs a Registry API that serves
    `POST /builds/{id}/tasks/{task_id}/interrupt`. Against an older server
    the SDK logs a warning and records nothing, which is exactly its
    behaviour before this existed — a version skew degrades to the old
    recovery path, never to a failed build.

**Claim expiry and your worker `timeout`.** Every start a tick records
carries a claim TTL derived from the `timeout` of the worker function the
task routes to (`FunctionSettings(timeout=...)`), plus a grace margin — so
the claim outlives the execution it guards by a small margin and no more,
and other builds can tell an abandoned claim from a live one without
probing Modal themselves. Workers that declare no `timeout` fall back to
the registry's default expiry. This is the main reason to set an explicit
`timeout` on long-running workers: it is what keeps a claim from being
taken while the execution is still alive, and equally what lets a claim
left behind by a dead scheduler heal promptly.

**Wide layers.** A tick fans out concurrently, up to
`max_concurrent_actions` spawns in flight (default 50), and caps how many
tasks one pass commits to via `max_spawns_per_tick`. Left unset, that cap
is derived from **the `tick` function's own `timeout`** — a fraction of it,
spread over the in-flight bound — because the cap exists to stop a tick
starting more work than its container can live long enough to finish. The
app reads that timeout at deploy time from `tick_settings` (or
`builder_settings`, which `tick_settings` falls back to), so a deployment
like

```{.python notest}
app = sd_modal.StardagApp(
    "stardag-poc",
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image, timeout=3600)
    },
    tick_settings=sd_modal.FunctionSettings(image=image, timeout=600),
    watchdog_period_minutes=5,
)
```

gives the cap the right number with no further configuration: the one-hour
worker `timeout` is what claim TTLs are derived from, the ten-minute tick
`timeout` is what the spawn cap is derived from. If neither the tick nor
the builder declares a `timeout`, the cap falls back to the worker timeout
as a proxy — a different quantity, and the tick's log line says it is on
that rung.

When a pass truncates at the cap it says so in the tick log and immediately
re-evaluates on a fresh frontier; the layer goes out in batches, not over
the watchdog period. Every tick also logs its cap and which input produced
it, once per tick. Both knobs are `tick_kwargs`, so they can be overridden
per build at trigger time:

```{.python notest}
app.build_trigger(
    root_task, reactive=True,
    tick_kwargs={"max_concurrent_actions": 100, "max_spawns_per_tick": 2000},
)
```

The **watchdog** sweeps every running build sequentially inside a single
container, so it hands each build a proportional share of that container's
budget instead of letting the first wide build size its fan-out as though
it owned the whole timeout. Watchdog passes therefore spawn in smaller
batches than a build's own ticks do — which is what you want from a safety
net.

**Seeing what a tick decided.** Every tick reports its summary to the registry
(`stardag builds ticks <build-id>`), so a build driven by dozens of
short-lived tick containers does not leave its reasoning scattered across as
many logs. A tick that _crashes_ is reported too, as `outcome="error"` with
the exception's type and message — usually the most informative thing a "why
did this build stall?" question can turn up. Reporting is best-effort
throughout: it can never fail a tick, change its outcome or mask its
exception, and it is tolerated by servers that predate the endpoint. Turn it
off for a whole deployment with `TickConfig(report_tick_summaries=False)`;
it is app-level configuration, not a per-trigger `tick_kwarg`.

Requirements and current limitations: the app must be deployed with this
stardag version (scheduler `tick` function + self-reporting workers); the
triggering process needs registry credentials and access to the default
target root (task _objects_ are persisted there for the ticks — the
reactive marker/owner/tick config live in the registry); the global
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

## Where to define what you pass to `StardagApp`

Every callable a `StardagApp` is handed — `container_setup`,
`worker_selector`, `limit_key_selector`, `build_function` and
`run_function` — must be defined in a **module the container can import**.
That means one of your own package's modules, added to the image with
`add_local_python_source(...)`, and _imported_ into the file you deploy.

**Not** the deploy entry point itself. This is the one placement rule you
cannot infer from your own code, so it is worth stating plainly:

```{.python notest}
# my_app/routing.py — importable, and in the image
def worker_selector(task):
    return "gpu" if task.get_name() == "TrainModel" else "default"


# my_app/app.py — the file you pass to `stardag modal deploy`
from my_app.routing import worker_selector  # ✅ imported, not defined here

app = sd_modal.StardagApp(
    "stardag-poc",
    worker_selector=worker_selector,
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image),
        "gpu": sd_modal.FunctionSettings(image=gpu_image),
    },
)
```

**Why.** `StardagApp` registers its Modal functions with `serialized=True`,
so a container receives a pickled closure rather than importing the module
your app was declared in. Cloudpickle stores a module-level callable — or
the _class_ of a callable instance, such as a `Builder` or `Runner`
subclass — as a **reference to its defining module**, and the container
resolves that reference by importing the module by name.

`stardag modal deploy path/to/app.py` loads that file under a module name
taken from the file name, so a `def` written in `app.py` pickles as
`app.<name>`. `app` exists only in the process that ran the deploy. In a
container the hydration fails before any of your code runs:

```
ModuleNotFoundError: No module named 'app'
modal.exception.DeserializationError: Deserialization failed because the
'app' module is not available in the remote environment.
```

Nothing at deploy time looks wrong — the deploy succeeds and prints the
full function list — and the damage is partial: `build` and `worker_*`
often survive, because their closures reach your package's modules anyway,
while the scheduled reactive functions do not. Stardag therefore refuses
the callable at `StardagApp(...)` with a
`SerializedCallablePlacementError` naming the callable, the module and the
fix, rather than letting it deploy.

Lambdas and closures written in the entry point are exempt, and are not
rejected: cloudpickle cannot look them up by name, so it serialises the
code object by value. They work — but a lambda that _calls_ a `def` from
the same file drags the same broken reference along with it, so importing
from a real module is the habit worth keeping.

### The same failure from the other direction: stardag's own version

Cloudpickle stores **stardag's** callables by reference too, so the image's
stardag has to be at least as new as the stardag doing the pickling. If it
is older, the app deploys cleanly and every container dies at hydration on
a stardag module — `No module named 'stardag.integration.modal._builder'`,
say — instead of one of yours.

`with_stardag_on_image` handles this for you: it ships your **local working
tree** when stardag is installed editable or is a dev build, and installs
the pinned release otherwise. Two things can still get it wrong, and both
warn:

- `STARDAG_MODAL_LOCAL_STARDAG_SOURCE=no` while you are working in a
  stardag checkout. The version it then pins comes from the install
  metadata, and an **editable install's version is frozen at install
  time** — a checkout installed at `0.17.0` reports `0.17.0` however far
  its source has moved on.
- An explicit `with_stardag_on_image(image, version=...)` older than the
  stardag you are deploying with.

If you hit this in a stardag checkout, note that a plain `uv sync` will
**not** refresh the recorded version — the editable install is already
present, so nothing rebuilds its metadata. Force it:

```bash
uv sync --reinstall-package stardag
```

## Container setup: code that runs in every container

Some setup is a property of the _container_, not of a build or a task:
materialising credentials onto disk, installing your own log formatter,
validating that the environment is what you think it is. Pass it as
`container_setup` and stardag runs it once per container, at the top of
**every** function the app registers — `build`, each `worker_*`, and the
reactive `tick`, `bootstrap` and `tick_watchdog`.

```{.python notest}
# my_app/setup.py — an importable module, not the deploy script (see above)
def container_setup() -> None:
    configure_logging()
    write_credentials()


# my_app/app.py
from my_app.setup import container_setup

app = sd_modal.StardagApp(
    "stardag-poc",
    container_setup=container_setup,
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={"default": sd_modal.FunctionSettings(image=image)},
    watchdog_period_minutes=5,
)
```

**Why this exists.** `StardagApp` registers its functions with
`serialized=True`, so a container unpickles a closure rather than importing
the module your app was declared in. Which of your modules get imported is
therefore decided by what each function's closure happens to reference:
`build` and `worker_*` close over your `build_function` / `run_function`,
so their modules are imported — but a `bootstrap` container closes over
nothing of yours at all, and `tick` / `tick_watchdog` import your code only
as a side effect of a `worker_selector` or the expanded `task_modules`.
Setup that "obviously runs everywhere" because it runs in your workers can
therefore be silently absent from the containers that drive a reactive
build. `container_setup` is the contract that replaces that accident.

### Which hook does what

`container_setup` does **not** replace a custom `Builder` or `Runner`, and
they do not replace it — the three have different scopes and are meant to
be used together:

| Hook                   | Scope         | Runs                                                            | For                                                                                                |
| ---------------------- | ------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `container_setup()`    | the container | once per container, before anything else, in all five functions | credentials, logging, environment checks — nothing build- or task-specific (it takes no arguments) |
| `Builder.setup(tasks)` | one build     | once per `build` invocation, in the `build` container only      | preparation that depends on the roots being built                                                  |
| `Runner.setup(task)`   | one task      | before every input a worker container serves                    | preparation that depends on that task                                                              |

For the reactive functions this is not a matter of taste: a `tick`,
`bootstrap` or `tick_watchdog` container contains no `Builder` and no
`Runner`, so `container_setup` is the only hook that reaches them.
Conversely, moving per-task work into `container_setup` would run it once
and then never again for the rest of that container's inputs.

### Details worth knowing

- **Define it in an importable module**, not in the file you deploy — see
  [Where to define what you pass to
  `StardagApp`](#where-to-define-what-you-pass-to-stardagapp), which
  applies identically to `worker_selector` and your build/run functions.
  Importing the hook from your own package is also what makes any
  module-level code in the hook's module run in every container of the
  app.
- **Once per container, not once per input.** A worker serves many tasks
  and a tick container may be reused; stardag holds the guard so you do
  not have to write one.
- **A hook that raises propagates, and is retried on the next input.** It
  is deliberately not remembered as done on failure — the alternative is a
  container whose remaining inputs run silently un-set-up. A hook that
  fails deterministically therefore fails every input, loudly.
- **It runs before stardag's own logging default**, which is a plain
  `logging.basicConfig(level=INFO)`. `basicConfig` no-ops once the root
  logger has handlers, so a hook that configures root logging wins, and an
  app that does not still gets the default. A hook that configures a
  _non-root_ logger will still see stardag add a root `StreamHandler`.
- **Only containers this app deploys.** `reactive_discovery="local"` runs
  discovery in the _triggering_ process, which is not a container of this
  app, so the hook does not run there — writing credentials or
  reconfiguring root logging in someone's shell would be the wrong call.
  An app that relies on the hook and also triggers with `"local"` has to
  prepare the triggering process itself.
- **It runs outside per-task `env_overrides`.** A `worker_selector`
  returning `(worker_name, env_overrides)` applies those around the task's
  `run` call only, so a hook that reads the environment sees the
  container's base environment, not the per-task overrides. Correct by
  scope — the container is set up once, the overrides vary per task — but
  worth knowing if you route credentials through both.
- **A failing hook is visible in Modal, not in the registry.** It runs
  before the worker's lifecycle reporter exists, so it does not record a
  `TASK_FAILED`; a reactive build sees the execution claim lapse and the
  next tick re-spawn. Same shape as a raising `Runner.setup()`.

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
