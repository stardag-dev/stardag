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

### Restart-safe triggering with `build_trigger`

With `build_spawn`, the registry build id is minted _inside_ the Modal
build container, so a restarted container starts a **new** build. With a
registry, prefer `build_trigger`: it mints the build first and passes the
id in, so any restart — a Modal retry, a manual re-trigger — **resumes**
the same build, and tasks whose outputs exist are skipped.

```{.python notest}
result = app.build_trigger(root_task)
print(result.build_id)       # minted at the trigger point
result.function_call.get()   # optionally block on the build function

# Re-attach to the same build later (after a failure, or a preemption):
app.build_trigger(root_task, build_id=result.build_id)
```

`builder_settings=FunctionSettings(..., retries=2)` lets Modal restart the
build function after infrastructure failures, which then auto-resumes.
`build_trigger` needs registry credentials in the calling process (the
active stardag profile) as well as Modal credentials.

### Detached execution: running tasks survive restarts

Tasks run as detached Modal function calls by default, and workers report
their own lifecycle to the registry — see [Orchestration on
Modal](../concepts/modal-orchestration.md#detached-execution-and-self-reporting-workers)
for what that buys. Two practical notes:

- Driving an app deployed with an **older** stardag from a newer SDK: pass
  `ModalTaskExecutor(worker_reports_lifecycle=False)` or redeploy, so the
  build engine does not wait for events old workers never send.
- Executor metadata (app, workspace, environment, function name) is
  recorded with starts and surfaced in the UI as Modal deep links. The
  workspace is resolved from the cached Modal token; set
  `StardagApp(modal_workspace=...)` to be explicit.

To opt out (legacy blocking `remote` calls): `StardagApp(...,
build_function=sd_modal.Builder(detached=False))`.

### Reactive scheduling: no resident build function

```{.python notest}
result = app.build_trigger(root_task, reactive=True)

# Same build id: wake a stalled build, add roots, or change tick config.
app.build_trigger(
    more_tasks, build_id=result.build_id, reactive=True,
    tick_kwargs={"linger_seconds": 60},
)
```

The model — bootstrap, ticks, wake-ups, retries, the watchdog — is on
[Orchestration on Modal](../concepts/modal-orchestration.md#reactive-scheduling).
What you configure:

**Requirements.** Both the Modal app and the registry server must run a
matching stardag version (an older server fails reactive triggers with a
clear "does not support reactive scheduling" error; an app deployed before
the `bootstrap` function existed has nothing to spawn — see
`reactive_discovery` below). The triggering process needs registry
credentials only. Builds cancelled in the UI are picked up by the next
tick in the environment, which cancels the running Modal calls.

**Function sizing.** `tick_settings` and `bootstrap_settings` default to
`builder_settings`. They want different timeouts: a tick is one frontier
pass, and its `timeout` also derives the per-pass spawn cap; the bootstrap
is one whole-DAG walk, paid once per trigger.

```{.python notest}
app = sd_modal.StardagApp(
    "stardag-poc",
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image, timeout=3600)
    },
    tick_settings=sd_modal.FunctionSettings(image=image, timeout=600),
    bootstrap_settings=sd_modal.FunctionSettings(image=image, timeout=1800),
)
```

Set an explicit worker `timeout`: the execution claim's TTL is derived
from it, which is what lets other builds tell an abandoned claim from a
live one, and what keeps a live claim from being taken early.

**Per-build knobs** (`tick_kwargs`, persisted with the build so every tick
shares them): `linger_seconds` (default 120), `poll_interval_seconds` (3),
`fail_mode`, `max_attempts` (2), `max_interruptions` (20),
`max_concurrent_actions` (50), `max_spawns_per_tick` (derived). Callables —
`worker_selector`, `limit_key_selector` — are deployed-app configuration,
never per-trigger.

**Named concurrency limits** are enforced registry-side, across builds.
Configure caps (`stardag concurrency-limits set gpu 4`) and tag tasks on
the app:

```{.python notest}
app = sd_modal.StardagApp(
    "stardag-poc",
    ...,
    limit_key_selector=lambda task: ["gpu"] if needs_gpu(task) else [],
)
```

A denied task stays pending and runs when a slot frees — whichever build
frees it. Resident builds enforce the same limits with
`RegistryConcurrencyLimiter`.

**The watchdog** (`watchdog_period_minutes`) is deployed always and
scheduled only when set. Leave it off unless a stall of a few minutes is
unacceptable; a standing sweep keeps a scale-to-zero registry database
awake. Without a period, a full sweep is one click away in the Modal UI
(`tick_watchdog`).

**Local discovery.** `StardagApp(reactive_discovery="local")` runs the
bootstrap in the triggering process — for an app deployed before the
`bootstrap` function existed, or a target root reachable from your machine
but not from Modal. It puts the task-module coverage check on your local
app definition rather than the deployed one.

**Redeploying mid-build.** Task objects are persisted as pickles for the
ticks; if a redeploy invalidates one, the tick rebuilds the task from the
registry's stored data, which works as long as the class is importable
(declare [`task_modules`](#declaring-your-task-modules-recommended)). Only
if both fail is the task failed — never silently stalled.

**Seeing what a tick decided.** `stardag builds ticks <build-id>` lists
every tick's summary — outcome, spawns, retries, neighbours woken, a
crashed tick's exception. `stardag builds frontier <build-id>` shows what
a build is waiting on and which build owns it.

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
[Retries and interruptions](../concepts/modal-orchestration.md#retries-and-interruptions).

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
