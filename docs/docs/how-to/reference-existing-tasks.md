# Reference Existing Tasks

Point code at a task the registry already holds — a trained model, a
curated dataset — by a stable, typed handle, instead of re-composing it
from code that keeps evolving.

!!! tip "What you need to know"

    - **`MyTask.ref(task_id, instance_id=...)`** is a reference to a
      registered task. Its id is that task id, so anything built on it keys
      exactly as it would on the real task.
    - **`stardag tasks ref <task-id>`** prints the line to paste, pinned to
      an instance.
    - **Builds hydrate references first.** `sd.build` reads each reference
      from the registry and swaps in the real task before anything is
      registered or run; `.hydrate()` does it explicitly.
    - **A reference needs a registry** — the one the task was registered
      in. A pinned one is served from the local body cache after its first
      read, so it also works offline.
    - **For pinned, long-lived instances in code** (a key dataset, a
      trained model), prefer the
      [committed task store](committed-task-store.md):
      `TrainedModel.from_store(...)` loads a reviewed copy kept in the
      repository, eagerly, with no registry. A reference is the handle for
      ad hoc work against the current environment.

## The problem: constants that re-key

A project that composes tasks from factory functions and defaults tends to
keep the tasks it cares about as module-level constants:

```{.python notest}
BASELINE_MODEL = TrainedModel(
    data=make_dataset("2026-q3"),
    learning_rate=3e-4,
)
```

That constant serves two purposes at once: a recipe for a new run, and a
pointer to the run that already happened. The two drift apart the moment
anything it is composed from changes. Give `make_dataset` or `TrainedModel`
a new default — even backward-compatibly, with
`StardagField(compat_default=...)` keeping the ids of tasks already stored —
and the constant, which picks up the new default, is a different task with
a different id. Building it trains a new model rather than loading the old
one, and everything downstream re-keys with it.

## The fix: reference the task by id

Find the task's id (in the registry UI, or `stardag tasks list`), and let the
CLI print the reference:

```sh
stardag tasks ref 6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b
```

```{.python notest}
TrainedModel.ref("6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b", instance_id="0193b0c4-...")
```

The task's namespace is an identity, not an import path, so the CLI does not
guess the import from it. Pass `-m my_project.models` (the module defining the
class), or list the package's task modules once under `[tool.stardag]
task_modules` in its `pyproject.toml` (see
[Import resolution](committed-task-store.md#import-resolution)), to have it
print the import line as well. Add `--write` to copy the instance into the
[committed task store](committed-task-store.md) and print a `from_store(...)`
line instead. The task detail page in the
registry UI has the same `ref(...)` line behind a copy button.

Use it wherever the task would go:

```{.python notest}
import stardag as sd
from my_project.models import TrainedModel
from my_project.evaluation import Evaluate

BASELINE_MODEL = TrainedModel.ref(
    "6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b",
    instance_id="0193b0c4-...",
)

evaluation = Evaluate(model=BASELINE_MODEL)  # same id as with the real task
sd.build(evaluation)                         # hydrates BASELINE_MODEL first
```

Nothing in `my_project` can move the reference: it names the task by id,
and the registry's stored body is the record of how it was constructed.

## What a reference is

- **Typed.** `TrainedModel.ref(...)` is a `TaskRef[TrainedModel, Model]`,
  a `Task[Model]`. A type checker accepts it wherever an
  `sd.TaskLoads[Model]` is expected and rejects it for any other loaded
  type; `.hydrate()` returns a `TrainedModel`. Every task class has
  `.ref(...)`: a `LoadableTask[T]` subclass (custom target or `load()`)
  gets a `TaskRef[MyTask, T]` too, and a plain `BaseTask` subclass, which
  declares no loaded type, gets a `TaskRef[MyTask, Any]` that fits any
  polymorphic task field (`sd.SubClass[sd.BaseTask]`).
- **Free to construct.** It makes no registry call. Its `id` is the task id
  you gave it, and in a parent's hash it contributes `{"id": task_id}` —
  exactly what the real task contributes. So `Evaluate(model=ref)` has the
  same id as `Evaluate(model=real_task)`, at any depth of nesting.
- **Hydrated on demand.** `ref.hydrate()` reads the task from the registry
  (rehydrated in compat mode, see
  [Evolve a DAG Safely](evolve-dags.md)) and checks it is a
  `TrainedModel` with the referenced id. `complete()`, `target()` and
  `load()` hydrate and delegate, so `ref.load()` loads the model.
- **A placeholder, not a proxy.** To read the referenced task's parameters,
  call `.hydrate()` and read them off the result. Class-level identity
  (`get_name()`, `get_namespace()`) answers for the referenced class without
  hydrating. To derive a new task from it, derive from the real one:
  `ref.hydrate().model_copy(update=...)`; `ref.model_copy(update=...)` is
  refused, since a reference has no parameters to update.

## Where a reference fits

A reference is a `Task[LoadedT]`, not an instance of the referenced class.
So it goes into fields that accept any task loading the right type:

- `sd.TaskLoads[Model]` fields, and lists, tuples and dicts of them
  (`list[sd.TaskLoads[Model]]`, `dict[str, sd.TaskLoads[Model]]`);
- `sd.Depends[Model]` parameters of `@sd.task` functions;
- a build's roots.

A field annotated with the concrete class, `model: TrainedModel`, rejects a
reference, and the validation error says so. Either annotate the field
`sd.TaskLoads[Model]` (usually what was meant: the task only loads the
model), or pass `ref.hydrate()`, the real `TrainedModel`.

## Pin the instance

A task id may have several instances: constructions of it that agree on
every significant field but may differ in non-significant ones (see
[Parameters](../concepts/parameters.md#significant-and-non-significant-fields)).
`instance_id` picks one:

- **Pinned** (`instance_id="..."`, what `stardag tasks ref` prints): the
  hydrated task is always that construction, and it is served from the
  local body cache (`~/.stardag/registry-cache/`) after its first read, so
  hydration works offline.
- **Unpinned**: chosen at hydration time, which always asks the registry.
  For a completed task, it is the instance its completing execution ran
  (an execution that reported completion and whose report completed the
  task, at the task's current completion time: a rejected late report does
  not count, nor an execution that predates a later completion observed at
  discovery after an invalidation);
  otherwise, or when no completed execution is recorded (a completion
  observed at discovery, say), the newest instance.

Why not simply the newest: every later build that registers a task under a
new deployment adds an instance, whose non-significant fields reflect a
deployment that never ran it. A completed task can hold dozens of these,
and the instance that produced its output may not be among the newest the
registry lists. The execution ledger records which instance ran, so the
default follows it, and `from_registry(task_id)` does the same.

`stardag tasks ref <task-id>` pins by the same rule, and says which instance
it chose and why (on stderr). Pass `--instance newest` for the newest
registered instance instead, or `--instance <id>` for a specific one.
`stardag tasks show <task-id>` names the instance the completing execution
ran, even when it is not among the instances it lists.

Pin unless you want the choice made again at every hydration.

### Get a reference by registering

A task that is not registered yet has no instance to pin. `sd.register`
records it — and its static dependencies — without running anything, and
returns the pinned reference, printing the line to paste:

```python
(model_ref,) = sd.register(TrainedModel(data=Dataset(size=3)), tags=["baseline"])
# Registered build 0193... (nothing was run):
#   from my_project.models import TrainedModel
#   TrainedModel.ref("<task_id>", instance_id="<instance_id>")
```

See [Registering without
running](../concepts/build-execution.md#registering-without-running-plan-only-builds).

## What a build does with references

At build start, every reference anywhere in the DAG — a root, a nested
field, an element of a list or dict of tasks — is replaced by its hydrated
task. Nothing downstream ever sees a reference:

- the registry stores the real task's body, never a reference;
- workers, Modal payloads and `run()` receive real tasks;
- a reference given directly as a root builds the referenced task (usually
  a no-op: it is complete) and is then loadable.

This applies to `sd.build`, `sd.build_aio`, the sequential builds, the
Prefect build, and the Modal triggers. `build_trigger` hydrates from the
registry it creates the build in. `build_spawn` and `build_remote` need no
local registry, so they never fall back to the local default one: given
references, pass `registry=` (the registry the deployed app builds against),
or they refuse. To do it yourself — say, to inspect the DAG a build will see
— call `sd.resolve_refs(tasks)`.

A reference has no body, only its task id, so it serializes into nothing but
its parent's id. An instance body, a Modal payload, or a plain `model_dump()`
of a task holding an unhydrated reference raises
`sd.TaskRefNotHydratedError` rather than producing a dump that could not be
loaded back.

## When hydration fails

`sd.TaskRefHydrationError` names the reference and the reason:

- **No registry configured, or the wrong one.** A reference hydrates from
  the registry and environment the build (or the active profile) uses.
  Instance reads are scoped to one environment, so a reference to a task
  registered in another environment of the same workspace does not hydrate:
  it fails with `TaskRefHydrationError` saying the registry holds no such
  task (or instance). Use the profile, or pass the registry, of the
  environment the task was registered in.
- **An old pinned instance on an older registry.** Against a registry that
  predates `GET /instances/{id}`, a pinned instance is found through the
  task's instance list, which serves the newest 500 instances only. An
  instance older than that is reported as not found; upgrading the registry
  fixes it.
- **The registry cannot be reached.** An unpinned reference always asks the
  registry: pin it. A pinned one needs one online read before it is served
  from the local body cache (and every time, if the cache is turned off
  with `STARDAG_REGISTRY_CACHE=0`).
- **Another class.** The stored body is not a `TrainedModel` (or a subclass
  of it): the id belongs to a different task.
- **The id no longer matches.** The stored body, validated under today's
  code, recomputes to another task id — a significant field was removed or
  renamed without a `compat_default`.
- **A class is not imported.** Every task class in the body, nested ones
  included, must be imported. On the first miss in a process, the project's
  `[tool.stardag] task_modules` are imported and the lookup retried (see
  [Import resolution](committed-task-store.md#import-resolution)); the
  error names the missing namespace and class, and what was imported.

A reference is served from the
[committed task store](committed-task-store.md#references-consult-the-store-first)
before the registry when the project configures one and it holds the body.

## A class with a field named `ref`

Every task class has the `ref` method, so a task that declares a field
named `ref` (a git ref, say) shadows it: pydantic warns when the class is
defined, a type checker reports the field as an incompatible override, and
`MyTask.ref(...)` is the field, not the method. Reference such a class with
the function form, which does the same and is typed the same:

```{.python notest}
import stardag as sd

checkout = sd.task_ref(Checkout, "6f1c...", instance_id="0193...")
```

`sd.task_ref` works for any task class, so code that references classes it
does not control can use it throughout. `stardag tasks ref` prints it for a
class whose `ref` is shadowed.

## Testing code that uses references

The simplest way to test code that pins instances is to pin them in the
[committed task store](committed-task-store.md#tests-read-the-store-offline):
`from_store` reads it with no registry, and references are served from it
too.

To test code against a registry instead, seed an in-memory one: a pinned
reference names one instance of one task, so a test can serve that exact
instance without a real registry. Record the instance body next to the code
that pins it (from `stardag tasks show <task-id> --json`, which lists the
task's instances with their ids and bodies), then seed it into
`stardag.testing.InMemoryRegistry` under the same ids:

```{.python notest}
from uuid import UUID

from stardag.testing import InMemoryRegistry, test_harness

reg = InMemoryRegistry()
for rec in recorded_bodies:  # {task_id, instance_id, body}
    reg.add_instance(body=rec.body, task_id=rec.task_id, instance_id=rec.instance_id)

with test_harness(registry=reg):
    assert TrainedModel.ref(task_id, instance_id=instance_id).hydrate().id == UUID(task_id)
```

`add_instance` takes the ids as given and stores the body as is: it needs no
build, no deployment and no import of the task's classes, and it does not
check the body. Hydration does, exactly as against a real registry, so a
body that no longer rebuilds into its task id fails the test the same way it
would fail a build. An unpinned reference reads the most recently seeded
instance of its task. Seeding an instance again is a no-op with the same
body and an error with a different one.

## Migrating from `AliasTask`

`AliasTask` is deprecated. It needed the loaded type pickled into its body,
which registry rehydration refuses, and it was not typed by the referenced
class. Replace

```{.python notest}
sd.AliasTask[Model].from_registry(task_id)
```

with

```{.python notest}
TrainedModel.ref(task_id, instance_id=...)
```
