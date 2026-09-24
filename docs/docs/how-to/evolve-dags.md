# Evolve a DAG Safely

Change dependencies, tune a run, and deploy new code — without version
bumps, without disturbing builds already running, and without two builds
disagreeing about what a task is.

!!! tip "What you need to know"

    - **Only fields that change the output need to be significant** (the
      default). A field that changes only _how the work is done_ or
      _which upstreams are required or yielded_ is
      `sd.StardagField(significant=False)` — still an ordinary constructor
      argument.
    - **`settings`** is a flat `dict[str, str]` of environment variables
      for build-wide knobs you would rather not turn into task parameters
      — passed to `sd.build(...)` or `app.build_trigger(...)`, applied in
      every process of the build.
    - **Changing `requires()` or a fan-out needs no version bump.** Deploy;
      new builds plan under the new deployment, and builds already
      running **roll over** to it at their next scheduler pass.
    - **One live deployment per app**, as on Modal. Want a branch to run
      beside production? Give it its own app name.
    - **A build has one scope (deployment + settings) per plan.**
      Re-triggering it with a root that would construct differently under
      that scope is refused: **start a new build.**

The concepts behind this page:
[significant and non-significant fields](../concepts/parameters.md#significant-and-non-significant-fields),
[the plan](../concepts/build-execution.md#the-plan-roots-discovery-closure),
and [deployments and code
versions](../concepts/modal-orchestration.md#deployments-and-code-versions).

## 1. Say what each field is for

```{.python notest}
from typing import Annotated
import stardag as sd

class Aggregate(sd.Task[Summary]):
    __namespace__ = "reports"
    period: str                                                          # significant
    partition_size: Annotated[int, sd.StardagField(significant=False)] = 100
    num_threads: Annotated[int, sd.StardagField(significant=False)] = 4

    def requires(self):
        return ListExportFiles(period=self.period)

    def run(self):
        files = self.requires().load()      # the period's export: a list of file names
        chunks = [
            ChunkStats(files=files[i : i + self.partition_size])
            for i in range(0, len(files), self.partition_size)
        ]
        yield chunks                        # the slicing depends on partition_size, the summary does not
        summary = merge((c.load() for c in chunks), threads=self.num_threads)
        self._save(summary)
```

`period` is part of the task id: two periods are two outputs. The file
list comes from a static upstream, so it is known once that upstream has
run; the partition size decides how that list is sliced into `ChunkStats`
tasks, each summarising one slice. Slicing 1,000 files by 100 or by 500
yields different chunk tasks but the same merged summary, so
`partition_size` is non-significant even though it changes structure.
`num_threads` changes only execution — also non-significant. Both are
ordinary constructor arguments; only their effect on `task.id` differs
from `period`'s.

The rule of thumb: **if two values of the field would give a different
result at the target, it is significant.** Otherwise it is not, whether it
changes which upstreams are used or only how the work runs — the model
does not need you to say which.

## 2. Give build-wide values through `settings`

A field you set per instance (as in step 1) is the normal case. `settings`
exists for the other one: a knob you would rather not thread through every
task's constructor at all — a global thread count, a feature flag —
applied as environment variables in every process of the build:

```{.python notest}
# Locally — also sd.build_aio and sd.build_sequential:
sd.build(Aggregate(period="2026-01"), settings={"NUM_THREADS": "8"})

# On Modal, the same argument on the trigger:
app.build_trigger(
    Aggregate(period="2026-01"), reactive=True, settings={"NUM_THREADS": "8"},
)
```

Read it back with the [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
pattern, at run time rather than at import time — a warm container imports
before it knows which build it is serving.

A misspelled or reserved key (`STARDAG_*`, `MODAL_*`) is refused at the
trigger, before a build exists. A re-trigger of an existing build
(`build_trigger(build_id=...)`) with `settings` omitted reuses the
build's stored settings; passing different ones starts a new plan in the
same build — see [The deterministic
scope](../concepts/build-execution.md#the-plan-roots-discovery-closure).

!!! note "Environment variables outside `settings`"

    They may affect how a task executes — a thread count read directly
    from the process environment is fine, same as one read from
    `settings`. They must not affect a task's output or which dependencies
    it requires or yields *unless* they arrive through `settings` or the
    deployment itself — reading an arbitrary, unscoped environment
    variable from `requires()` breaks the contract a scope's shared
    structure relies on. The registry has no way to catch this at write
    time; it shows up as a `TASK_STRUCTURE_DIVERGED` event when the same
    instance's declared edges grow between two registrations in the same
    scope.

## 3. Change dependencies

Edit `requires()`, reshape a yield, or change a non-significant default.
Then:

```bash
stardag modal deploy app.py
```

```{.python notest}
app.build_trigger(root, reactive=True)   # a new build, planned by the new deployment
```

Nothing else. The new build's dependency edges are recorded on its
instances under the new deployment's scope and evaluated over that scope
only, so upstreams the old code needed do not gate it, and an abandoned
fan-out from an old build is not inherited. Completed tasks stay
completed: the task id still promises the output, and the new build
reuses every target that exists.

Two plans sharing a scope (the same deployment and settings) share what
they discovered. If one has already run a fan-out parent to its yield, the
other's frontier closure step admits those edges directly, and it waits on
or runs the children instead of re-running the parent's pre-yield section.

## 4. Deploy new code

There is one live deployment per app, on Modal and in the registry. Each
`stardag modal deploy` **records a deployment row before the deploy** (so
the registry assigns its `generation` before any code changes) and
**activates it after** the deploy succeeds. After it, containers already
running finish on the old code, and every new spawn lands on the new one.

```bash
stardag modal deploy app.py        # records, deploys, activates
stardag modal deployments          # deployments recorded, newest first — the newest activated one is current
```

A running build's next scheduler tick runs on the new deployment, notices
the build's active plan names an older `deployment_id`, and **re-plans
it**: it rehydrates the plan's root instances under the new code, checks
their task ids are unchanged, then runs the static phase and seals a plan
for `(new deployment, same settings)` — reusing one if a scope-mate
already created and sealed it. You will see `rolled_over` in that tick's
summary. Nothing to do on your side.

What happens to work in flight:

- Containers started under the old code finish and report as usual.
- A dynamic dependency an old container yields after the redeploy is
  accepted into the superseded plan — a true fact about that scope, useful
  to any scope-mate — but the rolled-over build's own instance for that
  parent has no dynamic edges yet, so its next tick restarts the parent
  under the new code. The children the old container already ran stay
  completed and are reused; only the pre-yield section repeats.
- An execution the new plan no longer needs finishes on its own. Its
  output is content-addressed, so it harms nothing.
- A tick still lingering on the old deployment exits with `superseded`.

One precondition, checked by the tick and again at its `/seal`: **the
deployment must be the registry's current one for the app.** `stardag
modal deploy` records and activates each deploy; if either step fails
(the registry was unreachable), the command exits non-zero and says so,
and no build rolls over to that code until you re-run it — both steps are
idempotent (same client-minted deployment id).

What makes a rollover code-safe at all is that a task object has no
representation outside a running process other than the registry's stored
**instance body** — the full construction under its scope. Rebuilt in the
new deployment, a task is exactly what that deployment would construct;
nothing carries over from the code that planned the build.

Two things cannot roll over: a root whose _task id_ changed under the new
code, and a task the new deployment cannot rebuild — its class is gone, or
no longer covered by the deployment's `task_modules`. Both fail the build;
re-trigger as a new build.

**Branches.** A branch that should run beside production is another app
with its own name and its own single live deployment. A convention, not a
feature:

```{.python notest}
import os

app = sd_modal.StardagApp(f"reports-{os.environ.get('BRANCH', 'main')}", ...)
```

Two things to know about the code id baked into a deployment:

- For a Modal deployment it comes from `stardag modal deploy`, which pins
  the deployment id itself (`STARDAG_DEPLOYMENT_ID`) — the code id is
  recorded alongside it, from a **clean** checkout's git SHA. A dirty tree
  gets a one-off id with a warning: every deploy of it is a new scope that
  shares nothing. Commit before you deploy.
- For a **local** build there is no deploy step: the deployment is looked
  up or created from `(environment, kind="local", code_id)`, where
  `code_id` is `STARDAG_CODE_ID` if you set it, else the clean git SHA,
  else a one-off id. Set `STARDAG_CODE_ID` yourself where there is no git
  checkout to read — a CI image, a container built from an archive.

## What to expect in the UI

- A build's page shows its active plan's DAG, over the edges its
  instances recorded — a hop between deployments is visible as the
  active plan changing scope after a redeploy.
- Task instances are listed under the scope (deployment + settings) that
  constructed them, so two constructions of one task id in two scopes
  show up as two rows, not one.
- The deployments page lists every recorded deployment, newest first,
  with the current one for each app marked.
