# Release Notes

Release notes for the **stardag SDK** (`pip install stardag`). These cover significant changes and migration guides for SDK versions published to PyPI.

For changes to the Registry API, UI, and other components, see [CHANGELOG.md](CHANGELOG.md).

---

## v0.21.0 — One scheduler tick per build, not one per finished task

Additive for anyone using the Modal integration as documented. The
behaviour change is that a finishing worker now **skips spawning a
scheduler tick when the registry says one is already live** — fewer
containers, same scheduling.

**Upgrade the registry (stardag-api) too, or you get the old behaviour.**
The "is a scheduler live?" answer comes from the server. An older registry
does not send it, the SDK reads that as "unknown", and every wake-up spawns
a tick exactly as before — correct, just not cheaper. Nothing breaks either
way, and the two can be upgraded in any order.

### What it fixes

On a build whose tasks are short relative to a tick container's startup,
every completion used to spawn a tick that did nothing. The resident
scheduler's own linger loop did all the work; each spawned tick started
after it, found the lease held or the build already terminal, and exited.

Measured end-to-end on a 7-task build, before and after, same DAG and same
registry:

|                  | before                        | after                         |
| ---------------- | ----------------------------- | ----------------------------- |
| Tick invocations | **8**                         | **1**                         |
| Doing the work   | 1 (`iterations=7, spawned=7`) | 1 (`iterations=7, spawned=7`) |
| Doing nothing    | **7**                         | **0**                         |

Both numbers are from a live run, not an estimate.

### The correctness fix underneath it

Skipping the spawn is only safe if a live scheduler cannot exit past a
wake-up it has not seen — and the old exit path could. Nothing re-read the
wake-up flag between the linger loop's final poll and the release of the
scheduler lease, so a flag set in that window was served by nobody until
the next completion or the watchdog. With the last task in flight there may
be no next completion.

A tick now re-reads the flag once **before** releasing the lease (set →
keep the lease and re-act) and once **after** (set → spawn a successor
tick). This closes the release window; it is not crash recovery — a tick
that clears the flag and then dies still leaves that wake-up to the next
completion or the watchdog, as before.

Two new `TickSummary` counters, `linger_extended` and `successor_spawned`,
report each half. Both are normally zero: the window they cover is rare by
construction.

### Migration

Nothing to change. Two notes if you have written against the internals:

- **`RegistryABC.build_notify` now returns `BuildNotifyResult`** instead of
  `None`. A custom backend that overrides it and returns `None` keeps
  working — that reads as "scheduler state unknown", which spawns.
- **A custom tick runner** (anything calling `run_tick_aio` directly rather
  than through the Modal integration) should pass
  `TickConfig.spawn_successor_tick` if its workers can skip spawning. The
  two halves belong together: the worker decides on what the registry says,
  while handing off on the way out belongs to whoever holds the lease. It
  warns once per process if the lease is taken without one.

### Also in this release

- **`with_stardag_on_image` no longer pins a Modal image to a stale PyPI
  release when stardag is installed editable.** The choice between shipping
  the working tree and installing the pinned release was inferred from the
  version string, but an editable install's recorded version is frozen at
  install time — a checkout installed at v0.17.0 keeps reporting `0.17.0`
  while its source moves on. The image was then pinned to a real release
  older than the code being serialized into it, and every container died at
  hydration with `ModuleNotFoundError` for a stardag module. It now asks the
  installer whether this is a working tree.

  If you deploy from a stardag checkout and see this, note that a plain
  `uv sync` does not refresh the recorded version;
  `uv sync --reinstall-package stardag` does.

- **The reactive scheduler no longer logs an ERROR for a task-store miss it
  recovers from.** Declaring `task_modules` _is_ the opt-in to pickle
  elision, so on the recommended configuration every lookup missed by
  design and a healthy build emitted one error per task. The miss is now
  DEBUG.

---

## v0.20.1 — A callable your containers cannot import is now refused at deploy

Nearly additive. The one behaviour change: `StardagApp(...)` raises
`SerializedCallablePlacementError` for a callable defined in the file you
deploy. Such an app deployed cleanly before, and then failed in its
containers — so nothing that worked stops working.

**If you define `container_setup`, `worker_selector`, `limit_key_selector`,
a custom `build_function` or a custom `run_function` directly in the file you
pass to `stardag modal deploy`, move it into an importable module.** That
app is already broken; this release tells you so at the point you write it
instead of minutes later, in a container, in a function you were not
watching.

```python
# my_app/routing.py — a module in your package, added to the image with
# add_local_python_source(...)
def worker_selector(task):
    return "gpu" if task.get_name() == "TrainModel" else "default"


# my_app/app.py — the file you deploy
from my_app.routing import worker_selector  # imported, not defined here

app = StardagApp(
    "my-app",
    worker_selector=worker_selector,
    builder_settings=FunctionSettings(image=image),
    worker_settings={"default": FunctionSettings(image=image)},
)
```

### What went wrong

`finalize()` registers every function with `serialized=True`, so a container
receives a pickled closure rather than importing the module your app was
declared in. Cloudpickle stores a module-level callable — or the _class_ of
a callable instance, such as a `Builder` or `Runner` subclass — as a
reference to its defining module, and the container resolves that reference
by importing the module by name.

`stardag modal deploy path/to/app.py` loads that file under a module name
taken from the file name. A `def` written in `app.py` therefore pickles as
`app.<name>`, and `app` is a name that exists only in the process that ran
the deploy:

```
ModuleNotFoundError: No module named 'app'
modal.exception.DeserializationError: Deserialization failed because the
'app' module is not available in the remote environment.
```

Three things made this hard to catch. Every deploy-time signal is green —
the deploy succeeds and prints the full function list, and the `StardagApp`
object really is correctly wired in the deploying process. The damage is
partial: `build` and `worker_*` usually survive, because their closures
reach your package's modules anyway, while the scheduled reactive functions
do not — so an app can look healthy with its scheduler dead. And the rule is
a property of the deploy CLI, not of your app: nothing in your own code says
"this file will be imported under the name `app`".

### What is not rejected

Lambdas and closures written in the entry point, and anything defined in
`__main__`, are left alone. Cloudpickle cannot look those up by name, so it
serialises the code object by value and no import is needed in the container.
They work today and continue to. A `functools.partial` or a bound method is
itself written by value but carries a reference to what it wraps, so the
check looks through both.

The check is best-effort in the same way the existing `container_setup` arity
check is: a callable it cannot introspect is let through rather than refused
on a guess.

### Docs

The placement rule was previously stated in exactly one place — the
`ContainerSetup` docstring — which read as though it were specific to that
hook, when all five callables carry it identically. It now appears on every
one of them, and the Modal how-to gains a section, [Where to define what you
pass to
`StardagApp`](https://stardag-dev.github.io/stardag/how-to/integrate-modal/#where-to-define-what-you-pass-to-stardagapp),
that states the deploy CLI's import naming explicitly.

---

## v0.20.0 — Container setup you can actually rely on, in every Modal container

Additive. Nothing to migrate: an app that does not pass `container_setup`
behaves exactly as before.

**If your Modal app has setup code that must run before anything else —
credentials written to disk, your own log formatter, an environment check —
move it to `container_setup`.** It may not be running where you think it is.

```python
# my_app/setup.py — an importable module, not the deploy script
def container_setup() -> None:
    configure_logging()
    write_credentials()


# my_app/app.py
app = StardagApp(
    "my-app",
    container_setup=container_setup,
    builder_settings=FunctionSettings(image=image),
    worker_settings={"default": FunctionSettings(image=image)},
)
```

### Why it was needed

`StardagApp.finalize()` registers every function with `serialized=True`. A
container therefore unpickles a closure rather than importing the module your
app was declared in, and which of your modules get imported is decided by what
each function's closure happens to reference.

That worked out for two of the five. `build` closes over your `build_function`
and `worker_*` over your `run_function`, so those modules are imported and
their module-level code runs — the behaviour `StardagApp.__init__` documents
and that apps have relied on. The other three were never covered. A
`bootstrap` container closes over the app name, the task-module patterns and
two booleans — nothing of yours, so nothing pulls in the module your setup
lives in. (Its root tasks arrive by value, so unpickling them does import
_their_ modules; that is not the same thing, and is no help if your setup
lives anywhere else.) A `tick` or `tick_watchdog` container imports your code
only as a side effect — of a `worker_selector` or `limit_key_selector` if you
passed one, and of whatever the expanded `task_modules` pull in.

The failure mode this produces is a quiet one. Setup that plainly works —
because you see it working in your workers — is simply absent from the
containers that drive a reactive build, and the first symptom is somewhere
else entirely. It was reported by a deployed app whose object-store
credentials are prepared by exactly such a routine: its reactive builds failed
in `bootstrap`, at the first completion check of discovery, with a storage
error that said nothing about setup.

`container_setup` turns that accident into a contract. It runs at the top of
all five functions, so where your setup runs is no longer a property of your
import graph.

### Semantics worth knowing

- **Once per container, not once per input.** A worker serves many tasks and a
  tick container may be reused. The guard lives in stardag so you do not have
  to write one.
- **A hook that raises propagates, and is retried on the next input.** It is
  deliberately not remembered as done on failure — the alternative is a
  container whose remaining inputs run silently un-set-up. A hook that fails
  deterministically fails every input, loudly.
- **It runs before stardag's own logging default**, a plain
  `logging.basicConfig(level=INFO)`. `basicConfig` no-ops once the root logger
  has handlers, so a hook that configures root logging wins and an app that
  does not still gets the default. A hook that configures a _non-root_ logger
  will still see stardag add a root `StreamHandler`.
- **Define it in an importable module.** Like `worker_selector`, it is pickled
  by reference, so its defining module must be importable inside the container
  — part of the source you add via `add_local_python_source(...)`, not a loose
  deploy script. That is also what makes module-level code in the hook's own
  module run in every container.

### How it relates to a custom `Builder` or `Runner`

It does not replace them, and they do not replace it. The three hooks have
different scopes and are meant to be used together:

| Hook                   | Scope         | Runs                                                            | For                                                                                                |
| ---------------------- | ------------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `container_setup()`    | the container | once per container, before anything else, in all five functions | credentials, logging, environment checks — nothing build- or task-specific (it takes no arguments) |
| `Builder.setup(tasks)` | one build     | once per `build` invocation, in the `build` container only      | preparation that depends on the roots being built                                                  |
| `Runner.setup(task)`   | one task      | before every input a worker container serves                    | preparation that depends on that task                                                              |

For the reactive functions there is nothing to weigh up: a `tick`, `bootstrap`
or `tick_watchdog` container contains no `Builder` and no `Runner`, so
`container_setup` is the only hook that reaches them. In the other direction,
moving per-task work into `container_setup` would run it once and then never
again for the rest of that container's inputs.

See [Integrate with Modal](https://stardag-dev.github.io/stardag/how-to/integrate-modal/)
for the full section.

### Also: worker routing is checked at deploy

An app with **no `"default"` worker and no `worker_selector`** now fails
`finalize()` instead of deploying. Every task would route to a
`worker_default` function the app does not deploy, so nothing would have
worked; previously it deployed cleanly and failed at the first task. This is
scoped to the no-selector case — an app that declares a selector may omit
`"default"` and route everything to its own tiers, which works today and
keeps working.

The softer case gets a warning instead.

`finalize()` now warns when an app declares several `worker_settings` but no
`worker_selector`. Every task then routes to `"default"`, so the other tiers
are deployed and never reached — and nothing looks wrong, because the build
succeeds, just entirely on the wrong worker. The warning names the unreachable
workers, and passing a selector explicitly — even one that always returns
`"default"` — silences it.

Per-trigger overrides (`build_spawn`/`build_trigger(worker_selector=...)`)
remain a valid way to route a _resident_ build. Reactive builds reject them,
because later ticks (worker wake-ups, watchdog sweeps) could not honour them,
which is why the app-level selector is what the warning points at.

---

## v0.19.1 — A user package named `modal` no longer breaks target resolution

No API changes, nothing to migrate.

**If you have a package or module of your own named `modal` (or `boto3`),
upgrade.** On `0.19.0` that could make `get_file_target` and
`get_directory_target` fail at import — in a service that never used Modal or
S3 — with

```
AttributeError: module 'modal' has no attribute 'exception'
```

Here is the whole shape of it, because the interesting part is how ordinary
each half is.

A deployment launched its entrypoint as a script path — `python
pkg/service/main.py` rather than `python -m pkg.service.main`. CPython puts
that script's own directory on `sys.path[0]`, and that directory happened to
contain a first-party `modal/` subpackage. From then on every `import modal`
anywhere in the process resolved to it, stardag's included. Note this does
not raise: the import _succeeds_ and hands back the wrong module, so nothing
notices until something touches an attribute.

What made it stardag's problem is the target factory. It imports the optional
`stardag.integration.*` backends to find out which URI prefixes it can serve,
and guarded those imports against the backend not being _installed_ —
`ImportError` — and nothing else. A shadowed `modal` is not an `ImportError`.
So the `AttributeError` came up through the factory and took **every** prefix
with it, plain local paths included. The service could not build a target at
all, because of a backend it never asked for. It had simply never imported
`stardag.integration.modal` before; `0.19.0` reaches it to register
`modalvol://`, which is why a long-latent name collision surfaced as a
version bump.

Both halves are fixed. The Modal integration no longer reaches
`modal.exception` through the parent package, so a shadowed or partial modal
now fails as a plain `ImportError` — accurate, and catchable by everything
that already guards these imports. And the factory's guards catch and log an
unexpected failure rather than only a missing dependency: that integration's
prefix drops out of the mapping, asking for it afterwards gives the ordinary
"unsupported prefix" error, and a warning names the cause. A genuinely absent
optional dependency stays silent, as before, so the warning means something
when you see it.

### Recognising this in your own code

The generalisable part has nothing to do with modal. When a local package
shadows an installed distribution, `import x` **succeeds** — it just returns
the wrong module. So there is no `ImportError` anywhere, nothing fails at
import time, and the first symptom is an `AttributeError` at whatever line
first touches the real package's API. That line is usually deep inside a
library, so the traceback blames the dependency, and CPython's message for a
missing module attribute is often identical to what the real package's own
`__getattr__` would have produced.

If you are staring at an `AttributeError` on a dependency that makes no
sense, check what you actually imported:

```python
import x
print(x.__file__, getattr(x, "__version__", None))
```

A path outside `site-packages` is the answer. The usual cause is running an
entrypoint as a script path (`python pkg/service/main.py`), which puts that
script's own directory on `sys.path[0]` — so any sibling module or package
of your entrypoint can shadow any installed distribution. `python -m
pkg.service.main` does not do this.

Worth saying plainly: the old attribute form worked on every modal release
stardag supports. No modal version is implicated — it was the name collision
that exposed the assumption.

## v0.19.0 — A task can survive being interrupted

Modal takes containers away. It reclaims preemptible instances, and it kills
a function that hits its timeout. Neither means the task was wrong — but
until now the SDK had no way to say so, and the way you would naturally
write the handler made things worse.

To checkpoint, you catch the interruption. Do that and re-raise anything
derived from `Exception` — a `RuntimeError`, say — and stardag recorded a
permanent failure and, under `FAIL_FAST`, killed the build. The same task
with a bare `raise` was restarted and succeeded. One keyword apart, and
nothing warned you.

**The rule is now: the task decides whether it is resumable.**

```python
import stardag as sd
from stardag.integration.modal import MODAL_INTERRUPTIONS


class TrainModel(sd.TargetTask[sd.DirectoryTarget]):
    def target(self) -> sd.DirectoryTarget:
        return sd.get_directory_target(sd.get_default_relpath(self))

    def run(self):
        directory = self.target()
        checkpoint = directory / "checkpoint.json"
        try:
            train(resume_from=checkpoint)
        except MODAL_INTERRUPTIONS:        # preemption OR the function timeout
            save_checkpoint(checkpoint)
            raise sd.ResumableInterruption("checkpointed") from None
        directory.mark_done()
```

`sd.ResumableInterruption` is the only way a task gets resumed. An
interruption you let propagate stays a failure, retried under the ordinary
`TickConfig.max_attempts` — which is the right answer for both things an
uncaught interruption can mean: the task hung, or its worker's `timeout` is
too small. So there is **no configuration** deciding whether a timeout was
"expected". The task answers that by raising, or by not raising.

Resumption is bounded by the new `TickConfig.max_interruptions` (default
20), a budget separate from `max_attempts`. It has to be separate: a trainer
designed to be killed and resumed would otherwise exhaust a budget meant for
genuine failures and fail the build for the one reason it was built to
survive.

**Catch `MODAL_INTERRUPTIONS`, never `BaseException`.** It is exactly
`KeyboardInterrupt` (preemption) and `modal.exception.InputCancellation`
(the timeout). A blanket catch sweeps up ordinary bugs — a `NameError` is a
`BaseException` too — and answering one with "resume me" runs a
deterministic failure until the budget is gone. `except KeyboardInterrupt:`
is wrong in the other direction: `InputCancellation` is not a subclass of
it, so that handler does nothing at all on a timeout.

**The checkpoint must not be the task's result.** `DirectoryTarget`
completion is a `._DONE` flag written by `mark_done()`, so a checkpoint
written inside the directory sits beside the result without being mistaken
for one.

Note the base class: `sd.TargetTask[sd.DirectoryTarget]`, not `sd.Task`.
`Task` picks your target from its serializer and types `target()`
accordingly, so returning a bare `DirectoryTarget` from it does not
typecheck; `TargetTask` is the base for a task that owns its target.

### What you will see

The task cycles RUNNING → INTERRUPTED → RUNNING and finishes COMPLETED.
`INTERRUPTED` is a new task status — non-terminal, holds no execution claim,
and rendered in the UI as its own thing rather than as a failure. It does
not spend an attempt: a task resumed three times still reports
`attempt_count == 1`.

### Scope

**Reactive builds only.** `sd.build` / `build_aio` have no resumption path —
a timed-out execution fails the task there, as it always did, because only a
scheduler tick can respawn one.

**Requires a Registry API serving `POST /builds/{id}/tasks/{id}/interrupt`.**
Against an older server the SDK logs a warning and records nothing, which is
exactly its behaviour before this existed — a version skew degrades to the
old recovery path, never to a failed build. Deploy the server before
upgrading the SDK, as usual.

### Also

`FunctionSettings` gains **`nonpreemptible`** (the direct answer to "this
task must not be preempted" — 3x CPU/memory price, no GPU support) and
**`startup_timeout`**.

An interruption is now reported by the worker inside the grace window the
platform allows, rather than being inferred later by a scheduler probing a
corpse. That releases the execution claim and its concurrency-limit slots
immediately and wakes the scheduler directly — so recovery no longer depends
on having configured the watchdog.

---

## v0.18.0 — Builds collaborate, and triggering got fast

Reactive scheduling worked until something went wrong. Then a build sat at
RUNNING forever with no way to ask why, and the usual answer — a task in
_another_ build holding an execution claim nobody would ever release — was
invisible from anywhere in the product.

This release closes that loop, and makes triggering roughly ten times
faster on the way.

**Triggering discovers the DAG inside Modal.** A reactive trigger now
spawns a `bootstrap` function that walks the DAG, registers it, persists the
task objects and only then arms the build and spawns the first tick.
Discovery means one target existence check per task; from your laptop
against a `modalvol://` root each is a rate-limited Volume API call, while
inside Modal the volume is a mounted filesystem. A 127-task trigger went
from 64s — most of it rate-limit backoff — to 6.6s. It also means a
reactive trigger needs **registry credentials only**, with no target-root
access at all.

**Execution claims expire.** `Task.latest_status == RUNNING` _is_ the claim,
and it recorded no liveness evidence a third party could evaluate — so an
orchestrator that died wedged its tasks permanently. Claims now carry an
expiry, granted at start and derived from the executor's own timeout, and
both the claim check and the concurrency-limit count honour it.

**Builds collaborate instead of owning tasks.** A build is a request for a
set of root tasks to be materialised, not an owner of the tasks that
materialise them. The execution claim is the only thing coordinating across
builds; which build completes a task is otherwise irrelevant.

Two things follow, and both were previously wrong:

_A build's plan holds every dependency that was not complete when it was
discovered._ Discovery walks `requires()`, but dependency edges also come
from tasks that yielded them dynamically in some earlier build, and those
edges outlive it. A build could therefore be gated by an upstream it never
registered — which nothing could ever run, since the only thing that would
produce it was the task being gated. Registration now closes the plan over
every recorded edge, pruning at complete tasks.

_A shared task another build cancelled is still this build's to run._ When a
fail-fast build cancels the tasks it started, it correctly releases those
claims — but the tasks were left `CANCELLED`, which is not schedulable, so
every other build sharing the dependency died with it. One build's fail-fast
is now just that: one build's. Any build with the task in its plan resets it
and runs it, bounded by the per-task retry budget.

**A build that still cannot progress says why.** The frontier names the
tasks holding it up, their status and how long they have been in it.

**Operational surface.** New `stardag builds` and `stardag tasks` CLI groups:
list builds by status/idleness/app, list the tasks holding claims, and clean
up abandoned builds in bulk with a dry run that is the same query the reaper
acts on. Scheduler ticks record what they decided, per build.

**Reactive scheduling is more concurrent.** Ticks fan out with bounded
concurrency instead of spawning serially, discovery runs concurrently, and
the per-pass spawn cap is derived from the tick container's own timeout.
Tasks get a retry policy for failures the backend cannot retry itself.

**The SDK identifies itself.** Every request carries
`X-Stardag-SDK-Version`, and a registry that requires a newer SDK answers
`426` with a message naming both versions and the upgrade command, instead
of a confusing 404 or 422.

---

### Upgrade order

**Upgrade your registry first.** This SDK uses endpoints and fields added in
the same release (claim expiry, the frontier's blocker reporting, tick
summaries, bulk cancel, attempt counts). Against an older registry those
calls fail. The registry ships four migrations, two of which backfill data —
build status, and claim expiry — see [CHANGELOG.md](CHANGELOG.md).

One thing to check before upgrading a registry with long-running work: the
claim-expiry backfill stamps existing RUNNING tasks with
`latest_status_at + <default TTL>` (7 days). A task that has been RUNNING
longer than that gets an expiry already in the past, so the next tick treats
its claim as lapsed and may recover it. That is the intended healing for a
genuinely wedged task; run this first if you want to know what it will touch:

```sql
SELECT count(*) FROM tasks
WHERE latest_status = 'running'
  AND latest_status_at < now() - interval '7 days';
```

### Migration guide

**Reactive triggers now need registry credentials in your Modal
environment**, because discovery runs there rather than on the triggering
machine. `StardagApp` injects them from the `stardag-api-key` secret by
default. To keep discovery local — for a target root only your machine can
reach, say — pass `reactive_discovery="local"`.

**Declaring `task_modules` is now what enables pickle-free ticks.**
Inference still happens, but it is observation-only: an SDK upgrade never
starts eliding pickles on its own. Nothing breaks if you do not declare —
ticks keep reading the build task store exactly as before. To opt in:

```python
app = StardagApp(
    "my-app",
    task_modules=["my_pkg.tasks.*", "my_pkg.pipelines.*"],
    ...
)
```

**A build's task set now includes upstreams it did not register.** Because
the plan is closed over every recorded dependency edge, a task another build
is running — or one it cancelled — appears in your build's own
`actionable`/`running` and in its DAG view as a primary node rather than as
greyed-out context. `upstream_depth` in the graph view therefore reveals
only _complete_ upstreams; incomplete ones are already in the build. Nothing
to change on your side, but the counts and the view will look different.

**The watchdog sweep is scoped to the app that owns each build.** It used to
page every RUNNING build and filter client-side, which meant an environment
with more stale builds than the sweep's page budget could starve the
watchdog of the builds it exists to find. The cost is that an app deployed
**without** `watchdog_period_minutes` no longer gets incidental coverage
from another app's watchdog. Set it on every app you want swept.

**`stardag builds list --older-than` implies `--status running`.** Idleness
only means anything for a build that has not finished; a completed build has
no activity by definition and always will, so including terminal builds
filled a staleness listing with history sorted oldest-first. Pairing
`--older-than` with any other status is rejected.

**Custom `RegistryABC` implementations may need updating.** The
compatibility shims for registry backends that predate recent keyword
arguments have been removed, and `build_list_running` is now expressed in
terms of `build_list` with server-side filtering. If you have your own
`RegistryABC` subclass, check `build_list_running`'s signature. If you use
the shipped `APIRegistry` — which is almost certainly the case — nothing
changes.

---

## v0.17.0 — stardag ships its type information (PEP 561)

The published distribution now includes a
[PEP 561](https://peps.python.org/pep-0561/) `py.typed` marker, so type
checkers honour stardag's inline annotations instead of discarding them.
There are no API or runtime changes.

**What this means if you use mypy.** Without the marker, mypy ignored
stardag's annotations altogether:

```
error: Skipping analyzing "stardag": module is installed, but missing
library stubs or py.typed marker  [import-untyped]
```

Every stardag symbol was `Any`, so mistakes in your own code — a wrong
argument type, a misspelled attribute on a `Task` — passed silently. After
upgrading they are checked, so **a mypy run that was green may now report
new errors**. Those errors were always there; they were just invisible.

Two workarounds you may have in place become unnecessary, and will be
reported as unused once `warn_unused_ignores` is on:

```python
from stardag import Task  # type: ignore[import-untyped]  # <- remove
```

```toml
[[tool.mypy.overrides]]           # <- remove
module = ["stardag.*"]
ignore_missing_imports = true
```

**If you use pyright**, nothing changes in practice — it already resolved
stardag's types from the installed package. Strict mode simply stops
emitting `reportMissingTypeStubs: Stub file not found for "stardag"`.

---

## v0.16.1 — Self-host: shared-workspace default + API-key secret safety

Two `stardag self-host` fixes:

- The setup now defaults to creating a **shared** Stardag workspace named
  after your Modal workspace (with you as owner), and wires the API key,
  target root, and local profile to it. Modal's token lookup can't
  distinguish a personal from a team/org workspace, so this is now an
  explicit, shared-by-default choice — use `--no-primary-workspace` for
  solo/individual use, or `--primary-workspace NAME` to name it.
- `connect`/`up` no longer silently overwrite an existing `stardag-api-key`
  Modal secret in your execution environment (which would repoint DAG
  execution there, possibly away from another registry). You're warned and
  must confirm — a typed phrase interactively, or `--overwrite-api-key-secret`
  under `--yes`.

CLI-only; no server image change. Upgrade in place with
`stardag self-host upgrade`.

---

## v0.16.0 — Self-host: no Python pin, cleaner defaults

The prebuilt `self-host` deploy no longer needs its interpreter to match
the server image — run it under any supported Python (≥ 3.10), no
`--python 3.12` pin:

```bash
uvx --from "stardag[selfhost]" stardag self-host up
```

Under the hood the Modal `web`/`migrate` functions are now defined
by-reference and imported inside the server image, so nothing is
cloudpickled by the client ([#196](https://github.com/stardag-dev/stardag/issues/196)).
The default prebuilt image is now `server-v0.1.1` (fixes the settings
footer that read "vdev"). `--from-source` is unchanged.

No breaking changes; upgrade in place with `stardag self-host upgrade`.

---

## v0.15.0 — Self-host the Stardag service on Modal

You can now run your own private Stardag service (Registry API + web UI)
on [Modal](https://modal.com) with a single command — database on
[Neon](https://neon.com), free-tier friendly, no AWS or Docker required:

```bash
uvx --python 3.12 --from "stardag[selfhost]" stardag self-host up
# → prompts for a Neon API key and an admin account, then prints your UI URL
uvx --python 3.12 --from "stardag[selfhost]" stardag self-host upgrade  # migrate + redeploy
```

(The `--python 3.12` pin matches the prebuilt server image's interpreter,
required for the default prebuilt deploy; see the guide for details.)

Alongside it, a new **local authentication mode**: the self-hosted API
manages email/password accounts directly (no external identity
provider). `stardag auth login` detects it automatically and prompts for
credentials instead of opening a browser.

No breaking changes and no migration required: OIDC remains the default
auth mode, and all existing configurations behave exactly as before. New
optional extra: `pip install "stardag[selfhost]"` (pulls in `modal` and
`cryptography`).

See the [Self-host on Modal guide](https://docs.stardag.com/how-to/self-host-modal/)
for the full quickstart, including both auth modes.

---

## v0.14.0 — Exactly-once task execution by default (execution claims)

Two concurrent builds that both see a task as PENDING could previously
both execute it. As of v0.14.0 every task start carries an atomic
**execution claim** — evaluated inside the registry's start transaction,
at no extra roundtrip — so at most one execution can win a task, across
builds, processes, retries and restarts. Run `pip install -U stardag`.

Claims are **on by default** wherever a registry with claim support is
configured and the execution is probeable (detached Modal executions, in
both resident builds and reactive ticks). A losing claimant does not
fail — it resolves:

- **Re-attaches** to the winner's live execution and awaits its result.
- **Self-heals** a completion the winner already produced (target
  existence is ground truth, with eventual-consistency retries).
- Records a **provably dead** winner (a FAILED liveness probe,
  corroborated by a second delayed probe) and re-claims.
- **Waits with backoff** for a winner that exposes no probeable ref,
  bounded by `ClaimConfig.wait_timeout_seconds` (default 300s;
  `0` means "claim, but don't wait if held"). A timeout fails only the
  waiting build — never the winner's env-global status.

Control:

```python
from stardag.build import build, ClaimConfig

build(task)                    # claim=None (default): claim probeable executions
build(task, claim=True)        # force claiming (ref-less losers wait)
build(task, claim=False)       # disable
build(task, claim_config=ClaimConfig(wait_timeout_seconds=60.0))
```

Reactive scheduler ticks claim via `TickConfig.claim` (default `True`).
Older registry servers and custom registry backends without claim
support **degrade gracefully** to the previous behavior (duplicate
executions remain safe — idempotent re-execution and sticky
completion — just wasteful). Custom arbitration backends implement
`RegistryABC.task_start_claim_aio` (returning `StartClaimResult`), which
keeps claim, status and completion arbitration in one backend.

### `GlobalLockConfig` is deprecated

The lease-based global concurrency lock predates claims and is now
deprecated (a `DeprecationWarning` is emitted when it is enabled):

```python
# Before (deprecated)
build(task, global_lock_config=GlobalLockConfig(enabled=True))

# After — nothing: claims are on by default for probeable executions
build(task)
```

The lock remains functional for the one case claims don't cover:
executions **without probeable liveness** (e.g. local executors shared
across machines), where its TTL lease recovers from a crashed holder.
For long-running tasks the engine now **renews held locks in the
background**, fixing the previous silent expiry of the 60s lease. The
`GlobalConcurrencyLockManager` protocol is unchanged (it also backs the
reactive scheduler lease and remains the registry-less escape hatch).

## v0.13.0 — Strict polymorphic fields: also enforced on load

Follow-up to v0.12.0. A _strict_ polymorphic field — a bare concrete
annotation like `dep: MyTask` (no `SubClass[...]`) — means exactly `MyTask`.
v0.12.0 rejected a subclass **instance** assigned to such a field; v0.13.0
closes the remaining gap on the **deserialize path**. Run `pip install -U stardag`.

Previously, validating serialized data (e.g. via `model_validate` /
registry rehydration) with a subclass payload at a strict field silently
coerced it into the base type, dropping the subclass's parameters. Now an
input dict whose `__namespace`/`__name` discriminator names anything other
than the exact strict type raises `StrictPolymorphicTypeError`:

```python
class Parent(sd.Task[int]):
    dep: MyTask                                    # strict: exactly MyTask

Parent.model_validate({"dep": exact_MyTask_dump})       # OK
Parent.model_validate({"dep": {"a": 1}})                # OK (plain dict → exact type)
Parent.model_validate({"dep": child_of_MyTask_dump})    # StrictPolymorphicTypeError
```

> **⚠️ May surface latent bugs as errors.** Data that a **pre-0.12.0**
> version already truncated into a strict field now fails loudly on load
> instead of loading the degraded base type. This is the correct "fail loud
> on corrupt data" behavior. If the field is meant to hold subclasses,
> annotate it polymorphically:
>
> ```python
> dep: sd.SubClass[MyTask]
> ```

Exact-type payloads, plain dicts without a discriminator, and any field
already using `sd.SubClass[...]` / `sd.TaskLoads[...]` are unaffected.

## v0.12.0 — Stricter polymorphic field validation

Task fields that hold other tasks (or any `PolymorphicRoot`) must use a
polymorphic wrapper — `sd.SubClass[...]` or `sd.TaskLoads[...]` — so that the
concrete subclass and all of its parameters round-trip through serialization.
This release turns two long-standing silent-data-loss traps around bare
task-typed annotations into explicit, up-front errors. Pure bug fix — but
because it replaces silent corruption with a raised error, code that was
already (silently) corrupting will now fail loudly. Run `pip install -U stardag`.

> **⚠️ May surface latent bugs as errors.** If your task defines a field with
> a _bare_ task/polymorphic annotation, you will now get an error instead of
> silently losing data. The fix is to wrap the annotation. Both cases below
> were already broken before this release (dropped parameters, colliding task
> ids, load failures) — the error just makes that visible.

**1. Bare _abstract_ base annotations are rejected at class-definition time.**
A field annotated with an abstract base — `dep: BaseTask`, `deps: list[Task[int]]`,
`dep: TargetTask`, etc. — now raises `NakedPolymorphicFieldError` when the class
is defined:

```python
# Before (silently dropped subclass params, crashed on load):
class MyTask(sd.Task[int]):
    dep: sd.Task[int]

# After — wrap it:
class MyTask(sd.Task[int]):
    dep: sd.SubClass[sd.Task[int]]     # keeps the concrete subclass
    # or, if you consume the dependency's loaded output:
    dep_value: sd.TaskLoads[int]
```

**2. Bare _concrete_ base annotations are now strict (exact-type).**
A bare concrete annotation (`dep: MyTask`, without `SubClass[...]`) is a
_strict_ field meaning exactly `MyTask`. Passing a **subclass instance** now
raises `StrictPolymorphicTypeError` at construction, instead of silently
dropping the subclass's extra parameters (which also collapsed distinct values
to the same task id):

```python
class Parent(sd.Task[int]):
    dep: MyTask                        # strict: exactly MyTask

Parent(dep=MyTask(...))                # OK
Parent(dep=ChildOfMyTask(...))         # StrictPolymorphicTypeError

# To accept subclasses, wrap it:
class Parent(sd.Task[int]):
    dep: sd.SubClass[MyTask]
```

Passing an exact-type instance, and any field already using `sd.SubClass[...]`
or `sd.TaskLoads[...]`, are unaffected.

## v0.11.0 — Reactive build metadata in the registry

Reactive scheduling now keeps its marker/owner/tick-config in the registry
instead of a `meta.json` on the target root. This makes re-triggering work
on immutable/append-only target roots, lets a re-trigger update
`tick_kwargs`, and gives the server a real "RUNNING reactive builds owned by
app X" query. Requires a matching registry server (an older server fails the
reactive trigger with a clear error). Run `pip install -U stardag` and
**redeploy your Modal app**.

> **⚠️ Breaking for in-flight reactive builds — re-trigger them.** The
> scheduler tick now reads the "this build is reactively scheduled" marker
> **only** from the registry. A reactive build that was already in flight
> when you upgrade has no registry marker (its marker lived in the old
> target-root `meta.json`, which is no longer read), so **its ticks will
> silently no-op and the build stalls — with no error**. This affects only
> builds spanning the upgrade.
>
> **Action:** after upgrading and redeploying, re-trigger any reactive build
> that was running across the upgrade:
>
> ```python
> app.build_trigger(root_tasks, build_id=<existing_build_id>, reactive=True)
> ```
>
> A re-trigger resumes the build, re-persists its task objects under the new
> code, and writes the registry marker — after which ticks drive it normally.
> New reactive builds triggered on this version are unaffected.

- **Re-triggering updates `tick_kwargs`; a bare re-trigger preserves them.**
  Because the config now lives in the (mutable) registry, a re-trigger that
  passes `tick_kwargs` updates the scheduling config for all subsequent
  ticks; a re-trigger with no explicit `tick_kwargs` leaves the stored
  config untouched (it is not reset to defaults).

- **Task store is pickle-only.** The per-build task store on the target root
  now holds only the pickled task _objects_ (write-once, so it is compatible
  with immutable/append-only roots); the orchestration metadata moved to the
  registry.

This release also ships two additive, non-breaking improvements:

- **Stable Modal dashboard deep links.** The executor metadata now captures
  the Modal app id (`ap-…`) and worker function id (`fu-…`), and the UI uses
  them to build function-call links in the app-id URL form — which keep
  resolving after an app version is stopped or redeployed (the old links did
  not). Builds without the ids degrade gracefully to the app page, and the
  task-detail view gained a "more details" block listing every captured
  identifier (click-to-copy). **Redeploy your Modal app** to start emitting
  the new ids.
- **`stardag concurrency-limits` CLI.** Manage named concurrency limits from
  the CLI — `list`, `set`, `delete`, `holders`, and `evict` — against the
  active profile/environment, wrapping the registry endpoints. No more
  hand-rolled scripts to create or inspect limits.

---

## v0.10.2 — Modal secret & workspace ergonomics

An SDK-only patch cleaning up Modal registry-credential handling and
fixing UI dashboard deep links. No migration; `pip install -U stardag`
and **redeploy your Modal app**.

- **`StardagApp(stardag_api_key_secret=...)`** — declare the Stardag
  Registry API-key secret once; it is injected into every deployed
  function (all of them talk to the registry). Defaults to the
  `"stardag-api-key"` secret that `stardag modal stardag-api-key create`
  produces, so it works out of the box; pass a `modal.Secret`/name to
  override, or `None` to supply the key another way. A missing by-name
  secret now fails at deploy with a clear, actionable error.

  **Behavior change from 0.10.1:** 0.10.1 propagated _every_
  builder-declared secret to the workers/tick as a stopgap. That is
  reverted — per-function `secrets` are function-local again, and only the
  api-key secret is shared. If you moved the registry secret into
  `builder_settings.secrets` for 0.10.1, you can drop it (the default
  handles it) or pass it explicitly as `stardag_api_key_secret`.

- **Modal workspace now resolves in the UI (app dashboard links).** The
  workspace was missing from executor metadata — it was resolved from the
  Modal token, which isn't available inside Modal containers, and the
  lookup also ignored the personal-workspace case (the slug is the account
  `username`). Both are fixed: the workspace is resolved at deploy time
  (falling back to `username`) and baked into every function, so the
  app-level Modal dashboard link works. (The per-function-call deep-link
  URL format is a separate UI fix landing as a follow-up.)

---

## v0.10.1 — Modal reactive scheduling bug fixes

A bug-fix release for reactive Modal scheduling (shipped in 0.10.0).
**SDK-only — no API change, no migration, no server upgrade.** Run
`pip install -U stardag` and **redeploy your Modal app** so its image
picks up the fixes.

- **Reactive ticks / watchdog no longer crash in fresh containers.** A
  resource provider captured by a serialized Modal function (e.g. the
  scheduler `tick` / `tick_watchdog`) now re-initializes correctly in the
  container instead of returning an uninitialized sentinel.
- **Registry credentials propagate from the builder to the workers and
  tick/watchdog.** Declare the `stardag-api-key` secret once on
  `builder_settings`; workers no longer `401` on their self-reported
  lifecycle events. (Add it per-worker only if you must stay on ≤ 0.10.0.)
- **Re-triggering a reactive build (add-roots / retry) no longer fails on
  a no-overwrite / immutable target root.** The per-build task store is
  write-once; build roots are tracked solely in the registry.

---

## v0.10.0 — Modal as a first-class execution layer

A large feature release that makes Modal a first-class execution layer:
builds and long-running tasks survive restarts, builds can run with no
resident orchestrator at all, concurrency limits move server-side, and
the UI links straight into the Modal dashboard. **Fully backward
compatible — no client-code changes required**: existing builds keep
working unchanged after `pip install -U stardag`. The new
server-dependent features (reactive scheduling and named
concurrency-limit _enforcement_) additionally need a matching
stardag-api version — if you self-host the registry, upgrade the server
first; see [upgrade ordering](#upgrade-ordering--server-before-sdk)
below. (On the hosted registry at stardag.com the server is already
up to date.)

The new execution model is documented in
[Build & Execution](docs/docs/concepts/build-execution.md); the packaged
Modal setup in
[Integrate with Modal](docs/docs/how-to/integrate-modal.md).

### Restart-safe triggering and detached execution

`StardagApp.build_trigger()` triggers a deployed build with the registry
build id minted at the trigger point:

```python
result = stardag_app.build_trigger(tasks)
result.build_id       # registry build id — pass back to re-attach
result.function_call  # spawned Modal FunctionCall handle
```

Any restart of the build function (a Modal retry after preemption, or
re-triggering with the returned `build_id`) **resumes the same build**
instead of creating a new one — already-completed task outputs are
detected during discovery and skipped. Requires registry credentials in
the calling process; `build_spawn` remains for Modal-credentials-only
triggering.

Task execution on Modal is now **detached by default**:
`ModalTaskExecutor` spawns worker invocations as detached Modal function
calls (instead of holding blocking `remote` calls) and records the
function call id with the task's started event. A restarted build
**re-attaches to still-running workers instead of re-executing them** —
a task's execution survives orchestrator crashes. FAIL_FAST and user
cancellation now explicitly cancel the tracked function calls
(previously, workers of a dead build kept running). Opt out with
`ModalTaskExecutor(detached=False)` / `Builder(detached=False)`.

Workers also **self-report their lifecycle** from inside the worker
container — started (with a fresh re-attachable ref), completed plus
artifact upload, suspended (dynamic deps), failed — so registry state
stays accurate even if the build orchestrator dies mid-task. Opt out
with `ModalTaskExecutor(worker_reports_lifecycle=False)` (required when
driving an app deployed with an older stardag version from a newer local
SDK) or `Runner(report_lifecycle=False)`.

### Reactive (tick-based) scheduling — experimental

A build can now run with **no resident orchestrator**:

```python
stardag_app = StardagApp(
    ...,
    watchdog_period_minutes=5,  # optional but recommended
)

result = stardag_app.build_trigger(tasks, reactive=True)
```

Discovery runs at the trigger, and short-lived, idempotent scheduler
**ticks** — spawned by the trigger, by workers finishing tasks, and by
the optional periodic watchdog — drive the build: each tick spawns ready
tasks as detached function calls, probes running refs, self-heals
completions from target existence, and handles terminal states. While
only long-running tasks are in flight, **nothing runs but your tasks** —
no orchestrator container time, and no orchestrator to crash. On a
failure terminal, tasks transitively blocked by the failure are marked
**skipped** (mirroring the resident engine), and failed builds can be
re-triggered with the same `build_id` (failed tasks reset to pending).

Marked experimental: surface and semantics may still change. Requires a
registry and an app deployed with this stardag version; the global
concurrency lock and build-local `ConcurrencyConfig` limits are not
applied by ticks (registry-backed named limits are — see below). See
[Reactive Scheduling](docs/docs/concepts/build-execution.md#reactive-scheduling-no-resident-orchestrator)
and the
[Modal how-to](docs/docs/how-to/integrate-modal.md#reactive-scheduling-no-resident-build-function-experimental).

### Registry-backed named concurrency limits

Environment-level named limits cap how many tasks tagged with a key may
run concurrently — **across builds, processes and machines**, which
build-local `ConcurrencyConfig` semaphores never could. Configure keys
via the API (`PUT /api/v1/concurrency-limits/{key}`) or the new UI admin
page, then attach tasks to keys:

- **Reactive scheduling**: configure a key selector on the deployed app
  — `StardagApp(limit_key_selector=lambda task: ...)`. A denied task
  stays in the frontier and proceeds when a slot frees.
- **Resident builds** (`build` / `build_aio`): pass the new
  `RegistryConcurrencyLimiter` to the existing `ConcurrencyLimiter`
  seam:

```python
from stardag.build import RegistryConcurrencyLimiter

build(
    root,
    concurrency_limiter=RegistryConcurrencyLimiter(
        key_selector=lambda task: ["gpu"] if needs_gpu(task) else [],
    ),
)
```

A task occupies a slot simply by being RUNNING with the key recorded at
start (no leases/TTLs); acquisition is atomic in the task-start
transaction. Both modes share the same slots. Reactive builds self-heal
slots leaked by crashes; resident mode has no automatic healer — the new
holders/evict admin API and UI page are the recovery path for slots held
by a crashed resident build process. See
[Concurrency Limits](docs/docs/concepts/build-execution.md#concurrency-limits).

### Pickle-free task rehydration

New `stardag.task_from_registry_data(task_data, expected_task_id=...)`
reconstructs a task instance from the payload stored at registration —
no pickle involved. Requirements and limits: the defining module must be
imported (task classes register at definition time); nested task fields
must use the polymorphic annotations (`TaskLoads` / `SubClass`, whose
discriminators are embedded recursively); `AliasTask` payloads are
rejected (they embed pickled bytes); the optional identity check guards
against non-round-trippable custom serializers. Reactive scheduler ticks
use it as a fallback when a task's stored pickle is missing or
unloadable, so an app redeploy with compatible task definitions no
longer breaks in-flight reactive builds.

### Executor metadata and UI surfacing

Tasks and builds executed on Modal now record descriptive executor
metadata (app name, workspace, environment, function name). The UI
surfaces it: a "⚡ Modal" badge on tasks, an **Execution** section on the
task detail panel with deep links into the Modal dashboard, a
"Modal: app-name" chip and "reactive" badge on builds, and a new
env-scoped **Concurrency Limits** admin page (manage keys, inspect
holders, evict a stuck holder). Workspace resolution is lazy and
best-effort; override with `StardagApp(modal_workspace=...)` or
`ModalTaskExecutor(modal_workspace=...)`.

### Multiple apps per environment

Reactive builds are **owned by the app that triggered them**: scheduler
ticks from another deployed app in the same environment (typically its
watchdog sweep) forward the wake-up to the owner app's tick instead of
driving the build with the wrong app's code. Move a build to a new app
by re-triggering it from that app.

### Upgrade ordering — server before SDK

All changes are additive — no breaking changes, and older SDKs keep
working against a newer server. If you self-host the registry, **deploy
the upgraded stardag-api (including its DB migrations) before relying on
the new SDK features**:

- **Named concurrency limits**: an older server silently ignores the
  enforcement parameters (no error) — limits are only actually enforced
  once the server is upgraded.
- **Reactive scheduling** requires the new frontier/notify (and
  skip-blocked, retry, roots) endpoints — a matching server version.
- **Executor metadata** and detached re-attach degrade gracefully in
  both directions.

Mixed versions on Modal: driving an app deployed with an older stardag
version from a newer local SDK requires
`ModalTaskExecutor(worker_reports_lifecycle=False)` (the older deployed
workers don't self-report their lifecycle).

---

## v0.9.0 — Build concurrency limits & per-task Modal env overrides

Two additive features, both backward compatible. **No client-code changes
required** — `pip install -U stardag` is sufficient.

### Build-level concurrency limits

`build` / `build_aio` now accept a `ConcurrencyConfig` to bound how many tasks
run at once. It supports an overall cap and named limits applied to tasks via a
callback:

```python
from stardag.build import ConcurrencyConfig

build(
    tasks,
    concurrency_config=ConcurrencyConfig(
        max_concurrent_tasks=32,
        limits={"request-to-service-x": 10},
        key_selector=lambda task: (
            ["request-to-service-x"] if needs_service_x(task) else []
        ),
    ),
)
```

A task may be subject to several named limits at once. Limits are enforced
uniformly across all executors (local, Modal, routed) by gating the executor
submit call, and compose with the global concurrency lock. A task's slot is
released while it is suspended on its own dynamic dependencies and re-acquired
on resume (unlike the global lock, which is held across suspension).
`ConcurrencyLimiter` is a protocol seam for a future global, server-configured
limiter; for now limits are local to a single build.

### Per-task environment overrides for the Modal integration

A `WorkerSelector` can now return a `(worker_name, env_overrides)` tuple in
addition to a bare worker name, where `env_overrides` is a `dict[str, str]`:

```python
def select(task):
    if is_heavy(task):
        return "gpu", {"NUM_WORKERS": "16"}
    return "default"

stardag_app = StardagApp(..., worker_selector=select)
```

The overrides are set in the worker container's environment around the task's
`run` call and restored afterwards — handy for tuning task-specific execution
knobs (worker/thread counts, batch sizes, library env vars) on a per-task
basis without deploying a separate worker. `Runner.__call__` gained an optional
`env_overrides` parameter for this. The `RunFunction` protocol's required
signature is unchanged: existing custom run functions written against the
`(task)`-only signature keep working, with overrides applied to the process
environment around the call. Separately, `ModalTaskExecutor` now caches the
per-worker `modal.Function.from_name` handle instead of recreating it on every
submitted task.

---

## v0.8.1 — Fix `stardag modal deploy` on modal >= 1.4.3

Fixes `stardag modal deploy` crashing at import time when the installed
`modal` SDK is >= 1.4.3:

```
ImportError: cannot import name 'ensure_env' from 'modal.environments'
```

modal 1.4.3 moved `ensure_env` into a private module
(`modal._environments`) without a public re-export. The CLI now inlines
the small environment-resolution logic using modal's public config API
instead, removing the dependency on modal internals. Behaviour is
identical across the full supported modal range (`modal>=1.0.0`).

If you pinned `modal==1.4.2` as a workaround, you can remove the pin
after upgrading.

**No client-code changes** — `pip install -U stardag` is sufficient.

---

## v0.8.0 — `compat_default` compares the raw Python value

Fixes `StardagField(compat_default=...)` so it works for all field types.
The hash-mode drop now compares a field's **raw Python value** against
`compat_default`, instead of the already-serialized value.

`compat_default` exists so adding a field with a backward-compatible default
keeps existing task IDs/hashes unchanged: in hash-mode serialization, a field
whose value equals its `compat_default` is dropped from the hash dump. But the
drop previously compared `compat_default` against the _serialized_ field value.
For any field whose serialized form differs from its Python value — enums
(→ `.value`), tuples (→ lists), or a field with a custom/hash-only serializer —
the comparison failed even at the default, so the field was **not** dropped and
the hash changed anyway. The feature silently no-opped for those types.

It now compares the raw value (`getattr(self, name)`), which works for every
type and is symmetric with the compat-validation path (which already injects
the raw `compat_default`).

### Breaking change

Task IDs/hashes can change for any model that uses `compat_default` on a
non-trivially-serialized field (enum, tuple, custom serializer). Fields with
serialization-invariant types (`int`, `bool`, `str`, plain `float`) are
unaffected. There are two cases:

- **You worked around the bug by passing the serialized form** (e.g.
  `compat_default=["red"]` for a `tuple[Color, ...]` field). That no longer
  matches the raw value, so the field stops being dropped — switch to the
  natural Python form to restore the old hashes (see migration below).
- **You passed the natural form and it silently did nothing.** Adding the field
  had already shifted your task IDs. With the fix the field is now correctly
  dropped, shifting them again — back to the pre-field values you originally
  intended.

If either applies and you need to keep already-materialised outputs reachable,
re-pin via `__version__` or rebuild the affected tasks.

### Migration

Supply `compat_default` in the field's **natural, validated Python form** — the
value the field holds after validation, not its serialized form:

```python
import enum
from typing import Annotated
import stardag as sd

class Color(str, enum.Enum):
    RED = "red"

class Config(sd.Task[None]):
    # before (serialized-form workaround — no longer drops the field):
    colors: Annotated[
        tuple[Color, ...], sd.StardagField(compat_default=["red"])
    ] = (Color.RED,)

    # after (natural Python form — drops the field at its default):
    colors: Annotated[
        tuple[Color, ...], sd.StardagField(compat_default=(Color.RED,))
    ] = (Color.RED,)
```

Note `compat_default` must be **idempotent under validation**: if validation
coerces it (e.g. `[1, 2]` → `(1, 2)` for a `tuple[int, ...]` field), pass the
coerced form (`(1, 2)`), or the serialize-side equality check fails and the
field is not dropped.

---

## v0.7.3 — Correct slug display in `stardag modal` CLI

Fixes a misleading display in `stardag modal deploy` and
`stardag modal stardag-api-key create` where the workspace/environment
slug printed alongside the resolved UUID could come from a different
profile than the one whose IDs were actually used. The CLI now
reverse-looks up the slug from the resolved UUID via the id-cache, or
omits it when no cache entry exists.

This typically affected setups that override `STARDAG_WORKSPACE_ID` /
`STARDAG_ENVIRONMENT_ID` via env vars or a custom `config_provider`
default factory while leaving an unrelated profile active — for example
hypothetical output like
`Environment: <env-a-uuid> (env-b-slug)`, where the UUID resolves to
environment A but the slug is read from a profile pointing at
environment B. The deployed bytes were already correct; only the
printed slug was wrong.

**No client-code changes** — `pip install -U stardag` is sufficient.

---

## v0.7.2 — Build resume status fix and SKIPPED UI polish

Fixes a UX bug where `sd.build(resume_build_id=...)` silently reused the
build id without notifying the registry — so a build that previously
terminated (`failed` / `cancelled` / `completed` / `exit_early`) kept
showing its old terminal status while the SDK actively ran tasks under
it again. The SDK now fires a `BUILD_RESUMED` event immediately after
adopting the resumed id, so the registry flips the build back to
`running` and the UI surfaces a **"running (resumed)"** badge with the
build pinned to the top of the Home list.

**No client-code changes** — `pip install -U stardag` is sufficient.

### What changed in the SDK

- `RegistryABC.build_resume` / `build_resume_aio` (default no-op)
  added. `build`, `build_aio`, `build_sequential`, and
  `build_sequential_aio` invoke it whenever `resume_build_id` is set.
- `APIRegistry.build_resume[_aio]` posts to
  `POST /api/v1/builds/{build_id}/resume`. Older registry servers that
  don't expose this route return FastAPI's default `Not Found` body;
  the SDK swallows that with a warning (same `_is_route_not_found`
  pattern used by `task_skip` and `add_dependencies`) so resumed builds
  still run to completion against an un-upgraded registry — only the
  status-flip in the UI is missing in that combination.

### Compatibility

| SDK / API combination               | Behaviour                                                                                                                                              |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| New SDK + new API (≥ 0.7.2 on both) | Resumed build flips to `running (resumed)` and rises to top of Home list.                                                                              |
| New SDK + old API                   | SDK fires `build_resume[_aio]` → 404 → swallowed with a warning. Build runs to completion; UI shows the old terminal status until the API is upgraded. |
| Old SDK + new API                   | Unchanged behaviour. The new API additions (`/resume`, `last_active_at`, `is_resumed`) are additive.                                                   |

### Drive-by UI polish

The same release also fixes `TaskStatus = "skipped"` rendering across
the UI — previously near-invisible (black-on-dark-blue) on the
build-view task table and missing from the build-view status filter
dropdown. These are UI-only changes and don't affect the SDK; see
[CHANGELOG.md](CHANGELOG.md#072--2026-05-08) for the full list.

---

## v0.7.1 — Multi-root builds and `build_kwargs` on `StardagApp`

Two additive changes to `stardag.integration.modal`, plus one renamed
parameter for consistency with `stardag.build()`.

### What changed

- **Multi-root `build_spawn` / `build_remote`.** Both methods now accept
  either a single `BaseTask` or a `Sequence[BaseTask]`, matching what
  `Builder.__call__` and `stardag.build()` already supported.
- **`build_kwargs` passthrough.** `BuildFunction`, `Builder.__call__` /
  `Builder.build`, `PrefectBuilder.build`, and
  `StardagApp.build_spawn` / `build_remote` all gained a new
  `build_kwargs: dict[str, Any] | None` argument. The default `Builder`
  splats it into `stardag.build(...)`:

  ```python
  stardag_app.build_remote(
      task,
      build_kwargs={"fail_mode": FailMode.CONTINUE},
  )
  ```

  `Builder.build` rejects reserved keys (`tasks`, `task_executor`) with
  `TypeError`.

### Breaking changes

- **`build_spawn` / `build_remote`'s first parameter is renamed
  `task` → `tasks`.** Motivated by consistency with `stardag.build()` and
  the existing `Builder` entry points. Positional callers
  (`build_spawn(my_task)`) are unaffected; only callers using the keyword
  form (`build_spawn(task=my_task)`) need to update.
- **`BuildFunction` protocol gained a 4th parameter, `build_kwargs=None`.**
  Custom build functions passed to `StardagApp(build_function=...)`, and
  `Builder` subclasses overriding `build()`, must accept it (default to
  `None`).

---

## v0.7.0 — FAIL_FAST actually fails fast; explicit SKIPPED status for blocked tasks

Two related fixes to the build loop's failure handling. No breaking
changes — `pip install -U stardag` is sufficient. The behaviour change
matters mainly if you've been running `FailMode.FAIL_FAST` builds with
long-running tasks in flight (Modal worker calls, slow async tasks):
those will now exit promptly on the first failure instead of silently
abandoning the remote calls or blocking on in-flight work to drain.

### What changed

**FAIL_FAST cancels in-flight siblings.** When a task fails, the build
loop processes the rest of the current `asyncio.wait` batch first (so
sibling completions land), then cancels every remaining
`pending_futures` entry. asyncio cancellation propagates into
`modal.Function.remote.aio` and Modal terminates the remote container.
Each cancelled task gets a `TASK_CANCELLED` event and any global lock
it held is released with `completed=False`. Previously the build
re-raised in place — the asyncio cancel never reached the executor,
Modal containers kept running and billing, and the registry left them
stuck in `RUNNING`.

**Tasks blocked by a failed dep emit `TASK_SKIPPED`.** A fixed-point
walk after the build loop emits `task_skip_aio` for any task whose
dependency failed or was cancelled, including transitive descendants.
Applies to both `FAIL_FAST` and `CONTINUE`. Previously they stayed
`PENDING` in the registry forever.

### New API surface (additive)

For users writing custom executors or registries:

- `TaskExecutorABC.cancel(task)` — optional best-effort cancel hook.
  Default no-op. Override for executor-specific termination beyond
  asyncio cooperation. `HybridConcurrentTaskExecutor`'s thread/process
  pools rely on the asyncio.cancel of the wrapping future and the
  inherited no-op (Python can't reliably terminate threads/subprocesses
  anyway, and the existing `teardown(wait=True)` will still block
  until they finish).
- `RegistryABC.task_skip` / `task_skip_aio` — default no-op. The
  `APIRegistry` impl POSTs to a new `/skip` route.
- `TaskCount.cancelled` and `TaskCount.skipped` — new outcome counters
  on `BuildSummary.task_count`, rendered by `__repr__` when non-zero.

### Rollout

The new SDK degrades gracefully against older Registry APIs that lack
the `/skip` route (the SDK swallows the FastAPI default 404 with a
warning — same pattern as `/dependencies`; blocked tasks stay
`PENDING`, matching pre-0.7.0 observable behaviour). Older SDKs are
compatible with the new API since `/skip` is additive. No coordination
required, but the natural sequence is: deploy the API first, then bump
the SDK.

---

## v0.6.1 — Modal-volume disk cache (opt-in) and reload-staleness fix

A small, Modal-focused patch release. Two changes, both isolated to the
`stardag.integration.modal` integration. No breaking changes.

### New: optional local-disk cache for `modalvol://` targets

When you read a `modalvol://<name>/<path>` target _outside_ Modal — i.e.
from your laptop or another machine where the volume isn't mounted —
`get_modal_target` already falls back to an API-based `RemoteFileTarget`
that calls Modal's volume API for every read. For workflows that
re-read the same outputs repeatedly during local development, that's
slow and rate-limit-prone.

You can now wrap that path with a local-disk cache, mirroring the S3
integration's `STARDAG_TARGET_S3_*` knobs:

```bash
export STARDAG_TARGET_MODALVOL_CACHE_ROOT=~/.stardag/cache/modalvol/<workspace>/<env>/
```

Setting this env var is the **sole toggle** — there is no separate
"use cache" flag and no default root. Once set, every API-based read
populates a local cache file under that root; subsequent reads of the
same URI skip the API entirely. On write, the file is uploaded to the
volume first and then published into the cache atomically via a
tmp-file plus `Path.replace`, so a crash during cache population can
never leave a partial entry at the final cache path. Cache refresh
(re-uploading the same URI) works on POSIX and Windows.

When the volume _is_ mounted (running on Modal, or via
`STARDAG_MODAL_VOLUME_MOUNTS` / the auto-mount path),
`get_modal_target` continues to return a
`ModalMountedVolumeFileTarget` that bypasses the RFS entirely — caching
is automatically inactive on Modal workers, no extra configuration
needed.

#### Why no default cache root?

Unlike S3 bucket names, **Modal volume names are only unique within a
`(workspace, environment)` pair**. The same `modalvol://my-vol/foo` URI
can resolve to different content depending on which Modal profile is
active. Because the cache keys entries by URI alone, a default like
`~/.stardag/cache/modalvol/` would silently mix or overwrite cache
files across workspaces and environments — returning stale or
cross-tenant data after a `modal profile activate` switch.

Forcing you to set the root makes the workspace/environment scoping a
deliberate choice. Recommended pattern: include the workspace and
environment in the path, e.g.
`STARDAG_TARGET_MODALVOL_CACHE_ROOT=~/.stardag/cache/modalvol/<workspace>/<env>/`.
For per-volume scoping, use `STARDAG_TARGET_MODALVOL_CACHE_ROOT_BY_PREFIX`
to map specific `modalvol://` prefixes to dedicated cache directories.

#### Full env-var surface

| Env var                                                  | Purpose                                                                                        |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `STARDAG_TARGET_MODALVOL_CACHE_ROOT`                     | Sole toggle; absolute path where cache files live. Caching is enabled iff this is set.         |
| `STARDAG_TARGET_MODALVOL_CACHE_ROOT_BY_PREFIX`           | JSON dict mapping `modalvol://...` prefixes to per-prefix cache roots.                         |
| `STARDAG_TARGET_MODALVOL_CACHE_ALLOW_CACHE_CHECK_EXISTS` | Whether `exists()` may answer from the cache without round-tripping to Modal (default `true`). |

### Fix: volume-reload staleness in `ModalMountedVolumeFileTarget`

When a Modal volume _is_ mounted locally,
`ModalMountedVolumeFileTarget` reads files via the local filesystem
(fast, no API calls). To pick up writes committed by other workers, it
calls `volume.reload()` lazily on read-miss. Since v0.5.x, the SDK
imposed a 5-second per-volume cooldown between consecutive reloads to
prevent thundering-herd reloads during a 1000-task discovery scan.

The cooldown had a side-effect that we'd like to apologize for: it
could also suppress a reload that was _needed_. Concretely, if worker
A committed a file at T+4 and worker B called `exists()` on it at
T+4.5, B would skip the reload (cooldown not yet expired since the
last one at T) and incorrectly report the file as missing. Worst-case
observable staleness: up to one full cooldown window (5 seconds).
Producer/consumer polling and tight inter-worker handoff patterns
were the most affected.

This release replaces the cooldown with per-volume **singleflight
coalescing**: the original thundering-herd protection is preserved
(N concurrent `exists_aio()` callers still produce only one reload),
but a caller that arrives _after_ the in-flight reload was issued is
no longer suppressed — it correctly triggers a fresh reload of its own
to cover writes that may have landed in between.

#### What this means in practice

- Async discovery scans (the canonical thundering-herd case): unchanged
  — still ~1 reload per concurrent burst.
- Producer/consumer signalling: writes are now visible on the very next
  poll instead of after up to a 5-second wait.
- Sequential bursts of `exists()` calls on missing files (rare in
  practice — `ModalMountedVolumeFileTarget` is used inside Modal where
  the build engine is async) will reload per call instead of every 5s.
  If this turns out to bite, a small cooldown can be reintroduced on
  top of the new "always re-check" caller contract without
  reintroducing the original bug.

#### Also: cross-loop safety for the async reload lock

`asyncio.Lock` instances are bound to the running event loop at
acquire-time. The lock cache is now keyed by
`(volume_name, id(running_loop))`, so a fresh `asyncio.run()` gets its
own lock instance instead of reusing one bound to a now-closed loop —
fixes a latent `RuntimeError`/deadlock for code that calls
`asyncio.run()` repeatedly.

### Migration

None — both changes are backwards-compatible. `pip install -U stardag`
is sufficient. To opt in to caching, set
`STARDAG_TARGET_MODALVOL_CACHE_ROOT` to a workspace/environment-scoped
path of your choice.

---

## v0.6.0 — Discover-time task registration & bulk register

The build engine now registers every task with the registry as it's
discovered (in post-order DFS), in a single bulk HTTP call per discover
walk. The full DAG appears in the UI immediately rather than appearing
leaves-first as tasks become runnable, and the table view stays in a
stable order across refreshes.

### What changed in your build runs

Before v0.6.0:

```
[Discovery walks the DAG locally]
[Build runs]
   leaf-1 starts → registry sees leaf-1 for the first time
   leaf-2 starts → registry sees leaf-2 for the first time
   ...
   parent starts → registry sees parent
[UI shows tasks appearing in execution order, often leaves-first]
```

In v0.6.0:

```
[Discovery walks the DAG locally — collecting tasks in post-order]
[Bulk-register: chunks of 50 tasks per POST, gzipped on the wire]
[UI immediately shows the full DAG, in stable post-order]
[Build runs — each task already exists in the registry]
```

For large fan-out DAGs — think a parent whose

```python
def requires(self):
    return [Process(chunk=c) for c in self.chunks]
```

declares 500 deps — this is the difference between 500 sequential
HTTP round-trips at task-start time (the v0.5.x behaviour) and 10
bulk POSTs during discover (the v0.6.0 behaviour, registering all 500
deps + the parent up front). The chunk size of 50 is deliberately well
under the API's 1000 hard cap — keeps DB transactions short, keeps
request bodies friendly even with fat task specs, and limits the blast
radius of a chunk-level failure in `warn` mode.

> **Note**: dynamic deps yielded _one at a time_ from `run()` /
> `run_aio()` (`for chunk in chunks: yield Process(chunk)`) are still
> sequential — each yield blocks until the yielded dep completes
> before the generator advances. Bulk register only batches what's
> already in `requires()` (or what's yielded as a single list /
> tuple in one go: `yield [Process(c) for c in chunks]`). The DAG
> shape determines the batch shape; v0.6.0 just stops paying N
> round-trips to register the deps it can already see.

Request bodies above 1KB are gzipped before sending — bulk payloads
with repeated JSON structure compress 5–10× typically — and the server
transparently decompresses via a new `GZipRequestMiddleware`. Old SDKs
keep sending plain JSON; the middleware passes them through unchanged.

### Compatibility — the short version

- **Old SDK ↔ new server**: works unchanged. The per-task
  `POST /builds/{id}/tasks` endpoint hasn't changed semantics, and the
  new `is_phantom` field on responses is silently ignored by old
  Pydantic models.
- **New SDK ↔ old server**: works. The SDK calls the new bulk endpoint
  first; on a 404 ("missing route") it falls back to per-task
  registration with a warning. You lose the per-discover single-call
  perf win until the API is upgraded, but builds keep working.
- **You can deploy in either order.** Server-first is recommended so
  early SDK upgraders immediately get the bulk-call optimisation.

### Migration: direct callers of `APIRegistry.task_start[_aio]`

> **Skip this section** if you only build via `build()`, `build_aio()`,
> `build_sequential()`, or `build_sequential_aio()`. The build engines
> are updated and shield you from the contract change.

`APIRegistry.task_start[_aio]` no longer auto-calls `task_register[_aio]`
before emitting the start event. The `/start` endpoint is now pure — it
returns 404 if the task hasn't been registered.

```python
# v0.5.9 and earlier — task_start_aio auto-registered.
await registry.task_start_aio(build_id, task)

# v0.6.0 — you must register first.
await registry.task_register_aio(build_id, task)
await registry.task_start_aio(build_id, task)
```

If you subclass `RegistryABC` directly: no migration needed — your
subclass continues to work as before. The default
`RegistryABC.task_register_bulk[_aio]` falls back to looping
`task_register[_aio]` per task, so you don't need to override it
unless your backend has an efficient batch path.

### Behaviour notes (most users won't notice)

- **Sequential build registers in post-order DFS** (leaves before
  parents) where it previously used pre-order. Visible only in the
  registry / UI ordering, not in any API.
- **`build_fail(_aio)` now emitted when discovery raises.** If a
  task's `requires()` or `complete()` throws during discovery (rare),
  the build is now properly marked failed in the registry instead of
  being left in RUNNING state forever.
- **Phantom rows are filtered out of the build task table in the UI.**
  These auto-created placeholder rows used to flash up briefly between
  parent and child registration; they no longer occur in normal
  operation (post-order ensures deps register first), but if one
  legitimately exists — e.g. a build crashed mid-discover and left
  orphan rows — the UI hides it from the table. The DAG view still
  renders it.

### Also in this release

- **New `RegistryABC.task_register_bulk[_aio]` method** with a default
  loop-`task_register` implementation. `APIRegistry` overrides it to
  POST `/tasks/bulk`. Custom registries get the loop default for free
  but can override for batched backends.
- **`is_phantom` exposed on `TaskResponse`** so other API consumers
  can distinguish placeholder rows from real registered tasks.
- **Discover-walk race fix in concurrent build**: when two siblings
  shared a dep (diamond DAG), the fast-path
  `if task.id in task_states: return` could let one sibling's parent
  append to the registration order ahead of the still-in-flight dep,
  re-introducing the phantom window. The fast-path now awaits a
  per-task `discover_done` event, ensuring deps always register
  before parents even under sibling concurrency.

---

## v0.5.9 — Dynamic deps visible in the DAG view

Dynamically-yielded dependencies now register as graph edges in the Registry,
so the DAG view can finally render the parent → yielded-dep relationship.
Before v0.5.9, a task yielded from a `run()` / `run_aio()` generator only
had its own static `requires()` chain recorded — the yielded dep appeared as
a disconnected node in the DAG view.

```python
import stardag as sdag

class Orchestrator(sdag.Task[int]):
    source_uri: str

    def requires(self):
        return GetChunksToProcess(source_uri=self.source_uri)

    def run(self):
        specs = self.requires().load()
        chunks = [TransformChunk(chunk=LoadChunk(spec=s)) for s in specs]
        yield chunks  # ← these edges now reach the registry
        self._save(sum(c.load() for c in chunks))
```

In the UI:

- **Static edges** (declared via `task.requires()`) render as solid grey
  lines — unchanged.
- **Dynamic edges** (yielded from `run()` / `run_aio()`) render as
  _dashed_ grey lines. Hover an edge to see a tooltip explaining it was
  yielded at runtime.

### Registry protocol addition

A new method `RegistryABC.task_add_dependencies(_aio)` has been added. It
accepts a task plus a list of upstream tasks and an `is_dynamic` flag.
Default implementations on in-memory registries (`NoOpRegistry` etc.) are
no-ops. `APIRegistry` POSTs to the new
`POST /builds/{build_id}/tasks/{task_id}/dependencies` endpoint. Users who
subclass `RegistryABC` get the no-op default for free.

### Backward compatibility with older Registry API

If your SDK is newer than the deployed Registry API (no
`/dependencies` endpoint yet), the SDK call swallows the specific
FastAPI "missing route" 404 (`{"detail": "Not Found"}`) and logs a
warning — your builds keep working, you just won't see dynamic edges in
the DAG view until the API is upgraded. App-level 404s (`"Build not
found"`, `"Task … not registered …"`) still propagate normally.

### Requires a Registry API update

The companion API change (`is_dynamic` column on `task_dependencies` +
the new `/dependencies` endpoint) ships as part of the same release of
the platform. To see dynamic edges in the UI, deploy the updated Registry
API **before** upgrading the SDK (the SDK tolerates the old API with a
warning; reverse ordering loses the new-edge persistence until the SDK is
bumped).

### Also in this release

- **`max_per_type_per_level` grouping now applies at depth=0**. The
  `GET /builds/{id}/graph` endpoint previously only grouped when
  `upstream_depth` or `downstream_depth` was > 0 — the default in-build
  view ignored the setting. Builds with many structurally-identical tasks
  (e.g. a 50-chunk parallel fan-out) now collapse those into batch nodes
  in the default view.
- **Group edges inherit `is_dynamic`**: when same-type tasks collapse
  into a batch node, the resulting aggregate edge is marked dynamic if
  _any_ underlying contributor is dynamic.
- **New example**:
  `stardag_examples.general.dynamic_deps_demo` — a small pipeline with
  one static dep feeding dynamic yields (`Orchestrator` reads a
  `GetChunksToProcess` result to decide what to yield), each yielded
  task having its own static require. Exercises both edge types in a
  single DAG.

---

## v0.5.8 — Dynamic deps fixes: sequential build & async generators & Modal

This release is primarily a correctness fix for dynamically-yielded tasks plus expanded support for the pattern across all executors.

### Fix: `build_sequential` resolves the `requires()` chain of yielded tasks

`build_sequential` (and `build_sequential_aio`) previously executed a
dynamically yielded task directly, skipping any static `requires()` that task
declared. The concurrent `build()` already resolved them first.

```python
import stardag as sdag

class Leaf(sdag.Task[int]):
    value: int
    def run(self):
        self._save(self.value * 10)

class Middle(sdag.Task[int]):
    dep: sdag.TaskLoads[int]
    def requires(self):
        return self.dep
    def run(self):
        self._save(self.dep.load() + 1)

class Orchestrator(sdag.BaseTask):
    def complete(self):
        return False
    def run(self):
        middle = Middle(dep=Leaf(value=5))
        yield middle
        # At this point Middle — and Leaf, its static require — must be complete.
```

Before v0.5.8: `sdag.build_sequential(Orchestrator())` would run `Middle.run()`
before `Leaf.run()`, and `Middle` would fail with `FileNotFoundError` on
`self.dep.load()`.

After v0.5.8: both sequential and concurrent executors build `Leaf` first,
then `Middle`, then resume `Orchestrator`. ([#118](https://github.com/stardag-dev/stardag/issues/118))

No code changes needed — existing tasks that relied on the concurrent
`build()` will now work with `build_sequential()` as well.

### New: async generator dynamic dependencies

You can now declare dynamic dependencies from an `async def run_aio` as an
**async generator**:

```python
import stardag as sdag

class AsyncOrchestrator(sdag.Task[int]):
    limit: int

    async def run_aio(self):  # type: ignore[override]
        range_task = make_range(limit=self.limit)
        yield range_task
        # Build system ensures range_task is complete here
        values = await range_task.load_aio()
        await self._save_aio(sum(values))
```

Both the sequential and concurrent build executors detect async generators
via `inspect.isasyncgenfunction` and drive them with `async for`. The yield
semantics are identical to sync generators: after `yield task`, the build
system has ensured `task` is complete before execution resumes.

### New: Modal integration handles dynamic deps

Generators cannot be pickled, so `ModalTaskExecutor` couldn't previously
handle tasks that yielded dynamic deps. `Runner.run()` now drives generators
(sync and async) in the worker container and returns a `TaskStruct` of
yielded deps. The build system builds those deps and re-invokes the task —
on re-execution the generator advances past the previously-yielded batch.
This mirrors the existing behavior of `_run_task_in_process` for the
subprocess executor.

Async-only tasks (`run_aio` without `run`) are now also supported in Modal:
they're executed via `asyncio.run(task.run_aio())` in the worker.

### Minor breaking change: `@task` rejects generator functions

Generator and async-generator functions are no longer accepted by the
`@task` decorator:

```python
import stardag as sdag

@sdag.task
def bad(a: int) -> int:
    yield a   # raises TypeError at decoration time
```

Dynamic dependencies were never properly supported by the decorator API —
the class-based Task API is where the full machinery lives (type
annotations for yielded tasks, `requires()`, etc.). The change surfaces the
mismatch loudly instead of silently producing a task that misbehaves.
Migrate to a `Task` subclass:

```python
import stardag as sdag

class Good(sdag.Task[int]):
    a: int
    def run(self):
        yield some_dep(self.a)
        self._save(...)
```

### Also in this release

- **Sequential build consistency.** `_run_task_sequential[_aio]` now routes
  all dep discovery through a `runtime_discover()` wrapper that registers
  previously-complete tasks surfaced at runtime (e.g. a static dep of a
  dynamically-yielded task that's already on disk). Those tasks now appear
  in the build's task list in the Registry as `task_register` +
  `task_complete` events, instead of being silently excluded.
- **Modal integration tests.** New `lib/stardag/tests/test_integration/test_modal/test_runner.py`
  unit tests for `Runner.run()` dispatch behavior (no Modal account needed),
  and `TestEndToEndDynamicDepsBuild` in
  `lib/stardag/tests/test_integration/test_modal/test__app.py` covers the
  full remote round-trip for async-only tasks and sync/async dynamic deps.

### Known limitations

- **Runtime config override for Modal.** `StardagApp.build_remote` does not
  yet forward runtime configuration (e.g. target root overrides) to remote
  build/worker containers. Test isolation on Modal therefore relies on
  distinct task parameters plus pre/post volume wipes, rather than the
  cleaner `test_harness`-style per-test subpath override used locally.
  Tracked as [#121](https://github.com/stardag-dev/stardag/issues/121).

---

## v0.5.7 — Support for user-defined generic task classes

User-defined generic tasks can now be declared and instantiated directly —
two long-standing blockers have been removed without introducing any new
wire format or hash dependency.

### Unblocker 1: generic tasks get a `__type_id__`

Previously, any class with unresolved `__parameters__` was silently skipped
during polymorphic registration — so no `__type_id__` was attached. That
meant a user-defined generic task like

```python
from typing import Generic, TypeVar
import stardag as sd

ItemT = TypeVar("ItemT")

class MyGenericTask(sd.Task[list[ItemT]], Generic[ItemT]):
    deps: list[sd.TaskLoads[ItemT]]
    def run(self):
        self._save([d.load() for d in self.deps])

MyGenericTask(deps=[...])  # AttributeError: __type_id__ (at model_dump)
```

failed at the first `model_dump()`. The registration filter now only skips
**parameterized generic aliases** (`Task[int]`, not real classes) and
classes explicitly marked `__stardag_abstract__ = True`. `Task`,
`LoadableTask`, and `TargetTask` carry the marker, so their current
unregistered status is preserved. Any user-defined generic task is
registered under its own name and works end-to-end.

### Unblocker 2: `SubClass[T]` inside generic tasks

A generic task that wants to dispatch polymorphically on its TypeVar can
now declare:

```python
from typing import Generic, TypeVar
import stardag as sd
from stardag.polymorphic import PolymorphicRoot, SubClass

class ParamsBase(PolymorphicRoot): ...
ParamT = TypeVar("ParamT", bound=ParamsBase)

class MyGenericTask(sd.Task[int], Generic[ParamT]):
    params: SubClass[ParamT]
    # ...
```

Previously, the `SubClass[T]` annotation raised `TypeError: Polymorphic()
can only be used with PolymorphicRoot subclasses` at class-body time
because the TypeVar itself isn't a `PolymorphicRoot`. The schema builder
now treats a TypeVar as its `__bound__` for the generic form; Pydantic
re-invokes it with the concrete type for each parameterized form
(`MyGenericTask[Concrete]`), which narrows validation strictly. Unbounded
TypeVars still raise a clear `TypeError` at schema-build time.

### What remains class-definition-time

A TypeVar on a generic `Task` is a **static-typing convenience**. Runtime
behavior — serializer selection, target path, validators — is baked in at
class-definition time. If you need _different_ runtime behavior for
different type parameters (e.g. a distinct serializer for `MyTask[int]`
vs `MyTask[str]`), define a concrete subclass:

```python
class MyInt(MyGeneric[int]): pass
```

Concrete subclasses get their own `__type_id__` and hash distinctly;
parameterized aliases (`MyGeneric[int]`) do not — they share the generic
class's `__type_id__` and hash identically to the bare form. This is an
intentional invariant: **different `__type_id__` ⇔ different class with
different runtime behavior**.

### What this release does NOT do

An earlier iteration of this branch also pickle-transferred resolved type
args in the serialized payload (under `__type_args`), so distinct
parameterizations like `MyGeneric[int](...)` and `MyGeneric[str](...)`
hashed distinctly and round-tripped back to the parameterized class. We
decided against that path:

- It coupled task id stability to pickle output, which is version-
  sensitive (task ids could drift across Python minor versions).
- The `Task` machinery already draws the runtime-behavior line at
  concrete subclasses (parameterized aliases don't get their own
  serializer anyway). Introducing a finer hash granularity than the
  behavior granularity would be a surprise, not an asset.
- Users who genuinely need per-parameterization ids have a clear,
  already-supported path: concrete subclass.

If this tradeoff doesn't hold for a future use case, the pickle-transfer
design is recoverable from the PR history.

---

## v0.5.6 — Softer default for generic-type-mismatch handling

`Polymorphic(on_generic_type_mismatch=...)` — the option that controls what
happens when the best-effort generic-args compatibility check inside
`SubClass[...]` annotations fires — now defaults to `"warn"` instead of
`"raise"`. The same default applies transitively to plain `SubClass[T]`
annotations and to `TaskLoads[T]`-driven dispatch, which all flow through
`Polymorphic()`.

### Why

The compatibility check is heuristic and occasionally produces false positives
on patterns that are safe in context (different origins without a mapper,
nested `Annotated[...]`, etc.). A hard `ValidationError` on every such case
was too strict for what is ultimately an informational signal. A warning
surfaces the same information without blocking otherwise-valid code.

### Env var override: `STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH`

The mode can now be controlled globally via
`STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH`. Accepted values are `"raise"`,
`"warn"`, or `"ignore"`. Any other value raises a clear `ValueError`.
Resolution order:

1. Explicit non-`None` arg on `Polymorphic(...)` — always wins.
2. Env var.
3. Fallback to `"warn"`.

Resolution happens at validation time, so toggling the env var takes effect
live (useful with `monkeypatch.setenv(...)` in tests).

### Migration

- **Tolerate the warnings** (do nothing) — new default.
- **Silence them entirely** — `export STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH=ignore`.
- **Restore the old strict behavior** — `export STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH=raise`.
- **Per-field override** — pass `Polymorphic(on_generic_type_mismatch="raise")` (or `"warn"` / `"ignore"`) explicitly; that wins over the env var.

The emitted warning now carries a suppression hint so users encountering it
know how to silence it:

```
UserWarning: Value of type LoadsIntTask is not compatible with expected type
... (suppress by setting STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH=ignore)
```

---

## v0.5.5 — Customizable Modal Builder and Runner

The Modal integration now uses subclassable `Builder` and `Runner` classes
instead of fixed functions. Override `setup()`/`teardown()` to add custom
container-level initialization without replacing the entire build/run logic.

### Breaking changes

- `builder_type` parameter removed from `StardagApp.__init__` — use
  `build_function=MyBuilder()` instead.
- `default_build`/`default_run` functions removed — use `Builder`/`Runner` classes.
- `BuildFunction` protocol signature changed to
  `(tasks, worker_selector, app_name) -> BuildSummary`.
- `build_remote`/`build_spawn` kwargs: `task=` → `tasks=`, `modal_app_name=` → `app_name=`.

### Usage

```python
from stardag.integration.modal import StardagApp, Builder, Runner, FunctionSettings

class MyBuilder(Builder):
    def setup(self, tasks):
        super().setup(tasks)
        configure_my_environment()

class MyRunner(Runner):
    def setup(self, task):
        super().setup(task)
        torch.cuda.set_device(0)

app = StardagApp(
    "my-app",
    build_function=MyBuilder(),
    run_function=MyRunner(),
    builder_settings=FunctionSettings(image=image),
    worker_settings={"default": FunctionSettings(image=image)},
)
```

Subclasses work without calling `super().__init__()` — the `finalize()`
wrapper functions handle Modal compatibility automatically.

### New: `stardag.testing.modal`

Test tasks (`make_range`, `sum_list`) and `create_test_app()` factory for
Modal integration tests, defined inside the package for container serialization.

---

## v0.5.4 — Fix modal >= 1.4 compatibility

`import stardag.integration.modal` broke on `modal >= 1.4` due to the removed
`modal.gpu` module. The `GPU_T` type is replaced with `str | list[str]`, which
is what the modal 1.x API actually accepts.

---

## v0.5.3 — Secret masking for auth credentials

`RegistryAuth.api_key` and `RegistryAuth.access_token` now use Pydantic
`SecretStr` instead of plain `str`. This means secrets are automatically masked
as `**********` in `repr()`, `str()`, `model_dump()`, and log output.

**Migration**: If you read these fields directly, call `.get_secret_value()`
to get the plain string:

```python
config = get_config()
if config.registry and config.registry.auth.api_key:
    key = config.registry.auth.api_key.get_secret_value()
```

Truthiness checks still work (`if config.registry.auth.api_key:` is fine).

### Env var rename: `STARDAG_API_URL`

`STARDAG_REGISTRY_URL` is renamed to `STARDAG_API_URL` for consistency with
`STARDAG_API_KEY` and `STARDAG_API_TIMEOUT`. The old name still works as a
deprecated alias (with a `DeprecationWarning`).

### Bug fix: token auth with env var overrides

When `STARDAG_API_URL`/`STARDAG_WORKSPACE_ID`/`STARDAG_ENVIRONMENT_ID` are
set directly (bypassing profile for connection details), the loader now still
inherits user and registry_name from the active profile. This fixes OIDC token
auth failing in setups that override the URL but rely on a profile for identity.

---

## v0.5.2 — Configuration cleanup and auth token auto-refresh

This release restructures the configuration system and adds automatic JWT token
refresh during builds. **Breaking changes are limited to the configuration/auth
layer** — the core SDK for defining tasks, building DAGs, and working with
targets is completely unaffected.

### Breaking changes

If you access `StardagConfig` fields programmatically, the following paths changed:

| Before                          | After                                                       |
| ------------------------------- | ----------------------------------------------------------- |
| `config.api.url`                | `config.registry.url` (check `config.registry is not None`) |
| `config.api.timeout`            | `config.registry.timeout`                                   |
| `config.context.workspace_id`   | `config.registry.workspace_id`                              |
| `config.context.environment_id` | `config.registry.environment_id`                            |
| `config.context.user`           | `config.registry.auth.user_email`                           |
| `config.access_token`           | `config.registry.auth.access_token`                         |
| `config.api_key`                | `config.registry.auth.api_key`                              |
| `config.context.profile`        | `config.context.profile` (unchanged)                        |
| `config.context.registry_name`  | `config.context.registry_name` (unchanged)                  |

`config.registry` is `None` when no registry is configured (offline/local mode).

Removed symbols: `APIConfig`, `ContextConfig`, `DEFAULT_API_URL`.

`RegistryConfig` repurposed: was `RegistryConfig(url: str)` (TOML entry), now
`RegistryConfig(url, workspace_id, environment_id, auth, timeout)` (runtime config).
TOML registry entries are plain `dict[str, str]` in `TomlConfig`.

The `config/__init__.py` public API is now explicit. Internal symbols must be
imported from submodules: `from stardag.config.cache import _looks_like_uuid`.

### New: auto-refresh JWT tokens

`APIRegistry` now transparently refreshes expired JWT tokens before each API
request. This fixes a bug where long-running builds with browser-login auth
would fail when the short-lived token expired mid-execution.

### New: STARDAG_NO_REGISTRY

Set `STARDAG_NO_REGISTRY=1` to force offline/local mode. The registry provider
returns `NoOpRegistry` and `config.registry` is `None`.

---

## v0.5.1 — Automatic version field default

Small quality-of-life improvement: the `version` instance field on task classes now
automatically defaults to `cls.__version__`, eliminating the boilerplate
`version: str = __version__` that previously had to be repeated in every versioned
task subclass.

**Before:**

```python
class MyTask(sd.Task[int]):
    __version__ = "1"
    version: str = __version__  # ← required boilerplate
```

**After:**

```python
class MyTask(sd.Task[int]):
    __version__ = "1"  # version field defaults automatically
```

Existing code that already declares `version: str = __version__` continues to work
without any changes. Stored/serialized tasks are also unaffected — explicit `version`
values are always preserved.

---

## v0.5.0 — LoadValidator, Test Harness, and Build System Robustness

This release introduces load-time validation, a testing utility, and significant build system improvements. **No breaking changes** — all additions are backward-compatible.

### New: `LoadValidator[T]`

Validators that run automatically when data passes through `Task._save()` and `Task.load()`. Attach them via `typing.Annotated`, following the same pattern as serializers. Validators can reject (raise) or transform (return modified value), and multiple validators chain left-to-right.

```python
import typing
import stardag as sd

class NonEmpty(sd.LoadValidator[list]):
    def validate(self, value: list) -> list:
        if not value:
            raise ValueError("List must not be empty")
        return value

class MyTask(sd.Task[typing.Annotated[list[int], NonEmpty()]]):
    def run(self):
        self._save([1, 2, 3])  # validated before saving

# Also works with @task decorator
@sd.task
def my_task() -> typing.Annotated[list[int], NonEmpty()]:
    return [1, 2, 3]
```

**Attribute-based escape hatch**: For cases where subclassing `LoadValidator` causes MRO conflicts, any class with `stardag_load_validator = True` and a `validate()` method is also discovered.

### New: `test_harness`

A context manager in `stardag.testing` that sets up an isolated test environment with temporary target root directories and a `NoOpRegistry`:

```python
from stardag.testing import test_harness

def test_my_pipeline():
    with test_harness():
        task = MyTask(param="value")
        task.complete()
        result = task.load()
        assert result == expected
```

### New: `get_default_relpath()`

Standalone public utility for constructing default task output relpaths. Previously this logic was internal to `Task._relpath`:

```python
import stardag as sd

relpath = sd.get_default_relpath(task, extension=".json")
```

### New: `BuildSummary.raise_on_failure()`

Raises a new `BuildFailed` exception (with `.summary` attribute) when the build status is `FAILURE`:

```python
from stardag import build

summary = build([my_task])
summary.raise_on_failure()  # raises BuildFailed if any task failed
```

### Build System Improvements

- **`on_registry_failure` parameter** on all build functions (`build`, `build_aio`, `build_sequential`, `build_sequential_aio`) — `"warn"` (default) or `"raise"` to control registry error handling.
- **`register_all` flag** — opt-in full DAG registration, ensuring all tasks (including already-complete dependencies) are registered in the registry for complete graph visibility.
- **FAIL_FAST fix**: Task exceptions now properly propagate to the caller in both sequential and concurrent builds.
- **Deadlock detection** in sequential builds.
- **`TaskExecutionError`**: Wraps executor exceptions with pre-formatted tracebacks for better debugging across thread/process boundaries.
- **Commit hash traceability**: All task/build lifecycle events include the git commit hash in metadata.

### Other Improvements

- All serializers are now hashable (Pydantic generic cache compatibility with `Annotated` types).
- `TaskLoads[Annotated[T, ...]]` validation fixed — `Annotated` wrappers are now stripped in type compatibility checks.
- `Task.from_registry(id)` accepts `str | UUID`.
- `artifacts()` / `artifacts_aio()` return `Sequence` instead of `list`.
- `ResourceProvider.is_initialized()` added.

---

## v0.4.0 — Breaking: Target & Serializer Type Hierarchy Restructure

The target and serializer type hierarchies have been restructured to cleanly support both file and directory targets through a unified `Task` interface.

### Rationale

The previous hierarchy had `FileSystemTarget` serving double duty — it was both the minimal base protocol and the full file-oriented target. This made it impossible to properly type directory targets within the same hierarchy. The restructure introduces a clear separation: `FileSystemTarget` (minimal base with `uri` + `exists()`) → `FileTarget` (file I/O) / `DirectoryTarget` (directory of sub-targets).

### What Changed

#### Target Renames

| Before                         | After                          | Description                                                                                           |
| ------------------------------ | ------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `FileSystemTarget` (old, full) | `FileTarget`                   | File-oriented target with `open()`, `proxy_path()`, etc.                                              |
| _(new)_                        | `FileSystemTarget`             | Minimal base protocol: `uri` + `exists()`. Both `FileTarget` and `DirectoryTarget` inherit from this. |
| `RemoteFileSystemTarget`       | `RemoteFileTarget`             | Remote file target (S3, Modal volumes, etc.)                                                          |
| `InMemoryFileSystemTarget`     | `InMemoryFileTarget`           | In-memory file target for testing                                                                     |
| `LocalTarget`                  | `LocalFileTarget`              | Local filesystem file target                                                                          |
| `ModalMountedVolumeTarget`     | `ModalMountedVolumeFileTarget` | Modal mounted volume file target                                                                      |
| `_FileSystemTargetGeneric`     | `_FileTargetGeneric`           | Internal generic base for file targets                                                                |
| `_FSTargetType`                | `_FileTargetType`              | Internal TypeVar for file target types                                                                |

#### Serializer Changes

| Before                | After                          | Description                                       |
| --------------------- | ------------------------------ | ------------------------------------------------- |
| `Serializer[LoadedT]` | `Serializer[LoadedT, TargetT]` | Now parameterized by target type                  |
| _(new)_               | `FileSerializer[LoadedT]`      | Alias for `Serializer[LoadedT, FileTarget]`       |
| _(new)_               | `DirectorySerializer[LoadedT]` | Alias for `Serializer[LoadedT, DirectoryTarget]`  |
| `Serializable`        | `FileSerializable`             | File target + serializer wrapper                  |
| _(new)_               | `DirectorySerializable`        | Directory target + serializer wrapper             |
| `SelfSerializing`     | `SelfFileSerializing`          | Protocol for file self-serializers                |
| `SelfSerializer`      | `SelfFileSerializer`           | Serializer for `SelfFileSerializing` objects      |
| _(new)_               | `SelfDirectorySerializing`     | Protocol for directory self-serializers           |
| _(new)_               | `SelfDirectorySerializer`      | Serializer for `SelfDirectorySerializing` objects |

#### Factory & Helper Renames

| Before                        | After                             | Description                             |
| ----------------------------- | --------------------------------- | --------------------------------------- |
| `get_target()` (module-level) | `get_file_target()`               | Get a file target for a relative path   |
| `TargetFactory.get_target()`  | `TargetFactory.get_file_target()` | Method on factory class                 |
| `TargetPrototype`             | `FileTargetPrototype`             | Type alias for file target constructors |

### New: Directory Serializer Support in `Task`

`Task[T]` now automatically detects directory serializers and creates the appropriate target type:

```python
from typing import Annotated
import stardag as sd
from stardag.target import DirectoryTarget

class MyDirectorySerializer:
    target_type = DirectoryTarget

    def dump(self, obj, target: DirectoryTarget) -> None:
        with (target / "data.json").open("w") as f:
            f.write(json.dumps(obj))
        target.mark_done()

    def load(self, target: DirectoryTarget):
        with (target / "data.json").open("r") as f:
            return json.loads(f.read())

# Task automatically creates a DirectoryTarget
class MyTask(sd.Task[Annotated[dict, MyDirectorySerializer()]]):
    def run(self):
        self._save({"key": "value"})
```

### Migration Guide

1. **`FileSystemTarget` → `FileTarget`** in type annotations for file-oriented targets:

   ```python
   # Before
   class MyTask(sd.TargetTask[sd.FileSystemTarget]):
       def target(self) -> sd.FileSystemTarget:
           return sd.get_target("path.txt")

   # After
   class MyTask(sd.TargetTask[sd.FileTarget]):
       def target(self) -> sd.FileTarget:
           return sd.get_file_target("path.txt")
   ```

2. **`get_target()` → `get_file_target()`**:

   ```python
   # Before
   sd.get_target("path/file.json")

   # After
   sd.get_file_target("path/file.json")
   ```

3. **`Serializable` → `FileSerializable`**:

   ```python
   # Before
   from stardag.target.serialize import Serializable
   Serializable(wrapped=target, serializer=s)

   # After
   from stardag.target.serialize import FileSerializable
   FileSerializable(wrapped=target, serializer=s)
   ```

4. **`SelfSerializing` → `SelfFileSerializing`**, **`SelfSerializer` → `SelfFileSerializer`**

5. **`RemoteFileSystemTarget` → `RemoteFileTarget`**, **`InMemoryFileSystemTarget` → `InMemoryFileTarget`**

6. **`LocalTarget` → `LocalFileTarget`**, **`ModalMountedVolumeTarget` → `ModalMountedVolumeFileTarget`**

### Quick Find-and-Replace

```
sd.FileSystemTarget  →  sd.FileTarget         (for file-oriented targets)
sd.get_target(       →  sd.get_file_target(
Serializable(        →  FileSerializable(
SelfSerializing      →  SelfFileSerializing
SelfSerializer       →  SelfFileSerializer
RemoteFileSystemTarget  →  RemoteFileTarget
InMemoryFileSystemTarget  →  InMemoryFileTarget
LocalTarget          →  LocalFileTarget
ModalMountedVolumeTarget  →  ModalMountedVolumeFileTarget
```

---

## v0.3.0 — Breaking: Task Class Hierarchy Rename + LoadableTask + TaskLoads Update

The task class hierarchy has been renamed for clarity and a new `LoadableTask` abstraction has been introduced for better composability.

### Rationale

- **`LoadableTask` / adding `load()` to the task itself**: For downstream tasks, we only care about _what type is loaded_, not what type the `Target` has beyond that. In some cases, it is convenient not to implement a Target at all.
- **`output()` renamed to `target()`**: `output()` was taken from Luigi, where it was paired with `input()` (which mapped each dependency's `output()` into a corresponding struct). In Luigi, the Target was the only first-class representation of a task's result, so the naming made sense. In Stardag, the _loaded type_ is the primary result of a task — it's what powers type-hinted composability and why we don't have a Luigi-style `input()`. Given that, `output()` is confusingly close to the concept of "the task's result", while `target()` maps 1:1 to the data type it returns.
- **`AutoTask` renamed to `Task`**: This should be the default choice for most users, and now `Task` maps naturally to the `@task` decorator.

### What Changed

| Before                                               | After                       | Description                                                 |
| ---------------------------------------------------- | --------------------------- | ----------------------------------------------------------- |
| `AutoTask`                                           | `Task`                      | Auto filesystem targets + serialization (default)           |
| `Task`                                               | `TargetTask`                | Base class introducing typed `target()` targets             |
| `BaseTask`                                           | `BaseTask`                  | Unchanged - minimal core API                                |
| _(new)_                                              | `LoadableTask`              | Abstract base: `BaseTask` + `load() -> T`                   |
| `TaskLoads[T]` = `SubClass[Task[LoadableTarget[T]]]` | `SubClass[LoadableTask[T]]` | Now requires any `LoadableTask` subclass with matching type |
| `task.output()`                                      | `task.target()`             | Renamed for clarity: the target of a task                   |
| `_FunctionTask.result()`                             | _(removed)_                 | Use inherited `load()` instead                              |

### New: `LoadableTask[T]`

`LoadableTask[T]` is a new public abstraction that extends `BaseTask` with a single abstract method `load() -> T`. It is the minimal interface required for composability via `TaskLoads[T]`.

The class hierarchy is now:

```
          BaseTask                    # complete(), run(), requires()
         /        \
LoadableTask[T]    TargetTask[TT]    # load() -> T  /  target() -> TT
         \        /
          Task[T]                    # Combines both (TT = LSFST[T])
```

`Task[T]` uses diamond inheritance to extend both `TargetTask[LoadableSaveableFileSystemTarget[T]]` and `LoadableTask[T]`, so `Task` instances satisfy both interfaces.

### Convenience Methods on `Task`

`Task` gained `load()` and `_save()`, and `TaskLoads[T]` now resolves to `SubClass[LoadableTask[T]]` so `.load()` is available on dependency fields:

```python
# Before
class MyTask(sd.AutoTask[dict]):
    dep: sd.TaskLoads[list[int]]

    def run(self):
        data = self.dep.output().load()
        self.output().save({"sum": sum(data)})

# After
class MyTask(sd.Task[dict]):
    dep: sd.TaskLoads[list[int]]

    def run(self):
        data = self.dep.load()
        self._save({"sum": sum(data)})
```

### `@task` Decorator: New `target_root_key` Parameter

The `@task` decorator gained a `target_root_key` parameter to control which target root from config is used for output:

```python
@sd.task(target_root_key="s3")
def my_task(data: sd.Depends[list[int]]) -> int:
    return sum(data)
```

### Migration Guide

1. **Rename `AutoTask` to `Task`** everywhere:

   ```python
   # Before                          # After
   class MyTask(sd.AutoTask[int]):   class MyTask(sd.Task[int]):
       ...                               ...
   ```

2. **Rename `Task` to `TargetTask`** if you subclass it directly (with custom `target()`):

   ```python
   # Before                                    # After
   class MyTask(sd.Task[MyTarget]):             class MyTask(sd.TargetTask[MyTarget]):
       def output(self) -> MyTarget: ...            def target(self) -> MyTarget: ...
   ```

3. **`TaskLoads[T]` now requires `LoadableTask[T]`** (not `TargetTask`). Both `Task` and bare `LoadableTask` subclasses work. If you have a `TargetTask` subclass that needs to be passed as a dependency, use the explicit annotation:

   ```python
   # Use TaskLoads for most cases (Task and LoadableTask subclasses)
   dep: sd.TaskLoads[MyType]

   # For TargetTask subclasses (rare), use explicit annotation
   dep: sd.SubClass[sd.TargetTask[LoadableTarget[MyType]]]
   ```

4. **Rename `output()` to `target()`** on all task classes:

   ```python
   # Before                          # After
   task.output().load()              task.target().load()
   task.output().save(data)          task.target().save(data)
   def output(self) -> Target:       def target(self) -> Target:
   ```

5. **Replace `.result()` with `.load()`** on `@task`-created instances:

   ```python
   # Before                          # After
   my_task.result()                  my_task.load()
   ```

6. **`BaseTask` is unchanged**.

### Quick Find-and-Replace

For most codebases:

```
sd.Task[    →  sd.TargetTask[     (only for custom target() subclasses)
sd.AutoTask →  sd.Task
.output()   →  .target()
.result()   →  .load()
```

Run the first replacement before the second to avoid conflicts.
