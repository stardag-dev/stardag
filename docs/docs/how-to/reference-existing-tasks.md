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
class) to have it print the import line as well. The task detail page in the
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
- **Unpinned**: the newest instance at hydration time, which always asks
  the registry.

Pin unless you want "whatever was registered last".

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
  included, must be imported; the error names the missing namespace and
  class.

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
