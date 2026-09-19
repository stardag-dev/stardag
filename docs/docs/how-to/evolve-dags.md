# Evolve a DAG Safely

Change dependencies, tune a run, and deploy new code — without version
bumps, without disturbing builds already running, and without two builds
disagreeing about what a task is.

!!! tip "What you need to know"

    - **Only identity parameters go in the constructor.** A knob that changes
      _what is required or yielded_ is `significance="dependencies_only"`; one
      that changes _how the work is done_ is `"execution_only"`. Both are read
      from the **build config**, never passed at init.
    - **One `build_config` per build**, keyed `"<namespace>.<Name>"`, passed
      to `sd.build(...)` or `app.build_trigger(...)`. It is stored with the
      build and every worker installs it before constructing a task.
    - **Changing `requires()` or a fan-out needs no version bump.** Deploy,
      start a new build. Builds already running keep their own structure.
    - **A build carries one code version for its life.** Re-triggering it
      after a code change is refused; start a new build.
    - **To keep old builds running through a redeploy on Modal**, turn on
      `versioned_deployments=True`. Otherwise a redeploy under the same app
      name stalls running builds until they are re-triggered.

The concepts behind this page: [three levels of
significance](../concepts/parameters.md#three-levels-of-significance),
the [structure scope](../concepts/build-execution.md#structure-scope), and
[deployments and code
versions](../concepts/modal-orchestration.md#deployments-and-code-versions).

## 1. Say what each parameter is for

```{.python notest}
from typing import Annotated
import stardag as sd

class Aggregate(sd.Task[Summary]):
    __namespace__ = "reports"
    period: str                                                                      # identity
    partition_size: Annotated[int, sd.StardagField(significance="dependencies_only")] = 100
    num_threads: Annotated[int, sd.StardagField(significance="execution_only")] = 4

    def run(self):
        chunks = [Chunk(period=self.period, index=i) for i in range(self.partition_size)]
        yield chunks
        self._save(merge(c.load() for c in chunks), threads=self.num_threads)
```

`period` is part of the task id: two periods are two outputs. The
partition size changes which chunk tasks are yielded but not the merged
result, so it is _dependencies only_. The thread count changes neither, so
it is _execution only_. Passing either at init raises — that is what keeps
one task id to one structure within a build.

The rule of thumb: **if two values of the parameter would give a different
result at the target, it is identity.** If they give the same result
through a different set of upstream tasks, it is `dependencies_only`.
Anything else is `execution_only`.

## 2. Give values per build

```{.python notest}
config = {"reports.Aggregate": {"partition_size": 500, "num_threads": 8}}

# Locally — also sd.build_aio and sd.build_sequential:
sd.build(Aggregate(period="2026-01"), build_config=config)

# On Modal, the same argument on the trigger:
app.build_trigger(Aggregate(period="2026-01"), reactive=True, build_config=config)

# In tests, or anywhere no build is running:
with sd.build_config_scope({"reports.Aggregate": {"num_threads": 2}}):
    assert Aggregate(period="2026-01").num_threads == 2
```

A misspelled class or field, an identity field, or a value of the wrong
type is refused at the trigger, before a build exists. A re-trigger of an
existing build (`build_trigger(build_id=...)`) reuses the build's stored
config; passing a different one is refused, because a build has one config
for its life.

!!! note "Environment variables"

    They may affect how a task executes — a thread count read from the
    environment is fine. They must not affect a task's output or which
    dependencies it requires or yields; that is a parameter or a
    `dependencies_only` value. The registry warns when a task yields a
    different set than the one recorded for the same code and config.

## 3. Change dependencies

Edit `requires()`, reshape a yield, or change a `dependencies_only`
default. Then:

```bash
stardag modal deploy app.py
```

```{.python notest}
app.build_trigger(root, reactive=True)   # a new build, planned by the new code
```

Nothing else. The new build's dependency edges are recorded under the new
code's _structure scope_ and evaluated over that scope only, so upstreams
the old code needed do not gate it, and an abandoned fan-out from an old
build is not inherited. Completed tasks stay completed: the task id still
promises the output, and the new build reuses every target that exists.

Two builds under the **same** code and config share what they discovered.
If one has already run a fan-out parent to its yield, the other trusts
those edges and waits on or runs the children instead of re-running the
parent's pre-yield section.

## 4. Deploy new code beside running builds (Modal)

Modal has one live deployment per app name. After a redeploy under the same
name, in-flight containers finish on the old code but every _new_ spawn
lands on the new one — and a reactive build progresses by new spawns. Its
next tick sees code that did not plan the build and refuses to drive it
(`outcome='scope_mismatch'` in the tick summary). Safe, and it means
"redeploy, then re-trigger running builds as new builds".

To keep them running instead:

```{.python notest}
app = sd_modal.StardagApp("reports", versioned_deployments=True, ...)
```

```bash
stardag modal deploy app.py                  # deploys reports--<code id> and records it
stardag modal deployments --family reports   # live code ids, and how many builds run on each
stardag modal gc reports --keep 1            # retire and stop the ones no running build needs
```

```{.python notest}
app.build_trigger(root, reactive=True)                          # newest recorded deployment
app.build_trigger(root, reactive=True, deployment="local")      # this checkout's own code id
app.build_trigger(root, reactive=True, deployment="3f9c1a2b")   # a recorded code id, or a prefix of one
```

The name you wrote is the **family**; the app that runs is the **handle**
`<family>--<code id>`; the registry record ties them together, and nothing
else parses a handle. A running build keeps ticking on its own handle
while newer code deploys beside it. `gc` refuses to retire a deployment a
running build still names.

Two things to know about the code id:

- It is the git SHA of a **clean** checkout. A dirty tree gets a one-off id
  with a warning: every deploy of it is a new app that shares nothing, and
  `gc` is what cleans it up. Commit before you deploy.
- Where there is no git checkout — a CI image, a container built from an
  archive — set `STARDAG_CODE_ID` to name the code yourself.

## 5. Migrate from `hash_exclude`

`sd.StardagField(hash_exclude=True)` dropped a field from the hash but let
you pass the value at init, which is exactly what the build config exists
to prevent. It keeps working for one release with a `DeprecationWarning`.

1. Change the annotation to `significance="execution_only"`, or
   `"dependencies_only"` if the value changes what the task requires or
   yields.
2. Delete the argument from every constructor call.
3. Pass the value in `build_config`, keyed `"<namespace>.<Name>"`, where you
   call `sd.build` or `build_trigger`.

`AliasTask` needs no change.

## What to expect in the UI

- The Task Explorer's graph follows each task's _provenance_: the edges
  from the build that produced its current status. A hop between code
  versions is marked.
- A build's page shows its structure scope. `build:<id>` means the build
  never fixed one — an older SDK, or a build with no structure to share.
- Placeholder ("phantom") nodes are gone; an upstream that was never
  registered is a registration error, not a grey node.
