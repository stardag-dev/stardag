# Stable References to Tasks

How code points at a task that already exists — a key dataset, a trained
model — so that the pointer does not move when the code around it does.
This page is the overview: what the problem is, which handle to use, and
the whole workflow end to end. The how-to guides it links to are the
detailed reference.

!!! tip "What you need to know"

    - **Pinned, long-lived instances in code** (key datasets, trained
      models): use the [committed task store](../how-to/committed-task-store.md),
      loaded eagerly with `TrainedModel.from_store(...)`. It returns the
      real task, so its fields are readable and derivation with
      `model_copy` works.
    - **Ad hoc work**, such as a notebook loading an asset already built in
      the current environment: use the registry, with
      [`TrainedModel.ref(...)`](../how-to/reference-existing-tasks.md) or
      `TrainedModel.from_registry(...)`. No store is needed.
    - **Register key instances in the production environment** (or
      whichever environment matters), so they stay searchable there with
      tags and descriptions. The registry is the record of what ran; the
      store is a reviewed, immutable copy of it.

## The problem: task constants that re-key

A task's id is a hash of its namespace, name, version and significant
fields, nested tasks included (see
[Parameters](parameters.md#significant-and-non-significant-fields)). Code
that composes its tasks from factory functions and defaults tends to keep
the ones it cares about as module-level constants:

```{.python notest}
BASELINE_MODEL = TrainedModel(
    data=make_dataset("2026-q3"),
    learning_rate=3e-4,
)
```

That constant is two things at once: a recipe for a new run, and a pointer
to the run that already happened. The two drift apart as soon as anything
it is composed from changes. Give `make_dataset` a new step, or
`TrainedModel` a new default, and the constant is a different task with a
different id. Building it trains a new model instead of loading the old
one, and everything downstream re-keys with it. Even a backward-compatible
change (a new field with `StardagField(compat_default=...)`, which keeps the
ids of stored tasks) does not help a constant that is re-evaluated under
the new code: it picks up the new default.

The durable record is not the recipe but the **stored body**: the full,
serialized task as it was constructed, which the registry keeps for every
instance it has seen. Read back in compat mode (see
[Evolve a DAG Safely](../how-to/evolve-dags.md)), a stored body rebuilds
the same task, with the same id, under today's code — or fails loudly,
naming the field that moved. Every handle on this page is a way to get at
a stored body instead of re-running the recipe.

## Which handle to use

|                     | `TrainedModel.from_store(...)`                                                                              | `TrainedModel.ref(...)` / `from_registry(...)`                             |
| ------------------- | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| **For**             | Pinned, long-lived instances in code                                                                        | Ad hoc work against the current environment                                |
| **Reads from**      | The package's [committed task store](../how-to/committed-task-store.md), a directory of reviewed JSON files | The registry of the active profile (and a local body cache)                |
| **Needs**           | Nothing but the package: no registry, profile or network                                                    | The registry and environment the task was registered in, at least once     |
| **Returns**         | The real task, eagerly: fields readable, `model_copy` works                                                 | `ref`: a lazy placeholder a build hydrates; `from_registry`: the real task |
| **Review**          | A changed pin changes files in the store, so the diff shows the parameters that moved                       | A changed pin is two new UUIDs in the diff                                 |
| **Detailed how-to** | [Pin Tasks in a Committed Store](../how-to/committed-task-store.md)                                         | [Reference Existing Tasks](../how-to/reference-existing-tasks.md)          |

The two are complementary, not alternatives for the same job. The registry
is where tasks are found, annotated and browsed, and it is the record of
what ran. The store is the copy a code base is built around. For a key
instance, use both: register and annotate it in the environment that
matters, then copy it into the store and pin it there.

## The workflow, end to end

### 1. Build or register the task

A task gets into the registry by being built (`sd.build`), or registered
without running anything:

```{.python notest}
import stardag as sd

(model_ref,) = sd.register(TrainedModel(data=Dataset(size=3)), tags=["baseline"])
# Registered build 0193... (nothing was run):
#   from my_project.models import TrainedModel
#   TrainedModel.ref("<task_id>", instance_id="<instance_id>")
```

`sd.register` records the task and its whole static closure as a
**plan-only build**, which ends with the build status `registered`, and
returns one pinned reference per root. `sd.build(tasks, plan_only=True)`
does the same and returns a `BuildSummary`. See
[Registering without running](build-execution.md#registering-without-running-plan-only-builds).

### 2. Annotate it

Tags and a description belong to the task id, not to an instance: they are
mutable, sit outside every hash, and never re-key anything. Set them when
registering (`sd.register(..., tags=[...], task_description="...")`), from
the CLI, or in place on the task page of the registry UI:

```sh
stardag tasks annotate <task-id> --tag baseline -d "Tuned baseline, 2026-q3 data"
```

`--tag` adds, `--remove-tag` removes, `--set-tags` replaces and
`--clear-tags` empties. See the [CLI reference](../configuration/cli.md).

### 3. Find it again

Tags are for discovery, ids are for code. The registry UI's **Tasks** page
searches by free text, task-id prefix, name, namespace, tag (`#baseline`)
and status, and the search lives in the URL, so it can be shared. From the
command line:

```sh
stardag tasks list --tag baseline --namespace my_project -s "2026-q3"
stardag tasks show <task-id>    # status, tags, instances, executions
```

### 4. Pin it

Print the line to paste, pinned to an instance:

```sh
stardag tasks ref <task-id>            # a TrainedModel.ref(...) line
stardag tasks ref <task-id> --write    # copy it into the store; a from_store(...) line
```

The default instance is, for a completed task, the instance its completing
execution ran; otherwise the newest. `--instance newest` and
`--instance <id>` override it, and the choice and its reason are printed on
stderr. The task page in the registry UI has the `ref(...)` line behind a
**Reference in code** copy button.

For a task object you hold, built or not, `sd.write_to_store(task)` writes
it into the store with no registry involved and prints its `from_store(...)`
line. Prefer `stardag tasks ref --write` for anything that ran: the file
then records the registry instance it was read as.

### 5. Use it

```{.python notest}
from my_project.models import TrainedModel

# Pinned in the committed store: eager, the real task.
BASELINE_MODEL = TrainedModel.from_store("6f1c...", instance_hash="7a2e...")
candidate = BASELINE_MODEL.model_copy(update={"learning_rate": 1e-4})

# Ad hoc, from the registry: a reference a build hydrates.
model = TrainedModel.ref("6f1c...", instance_id="0193...")
sd.build(Evaluate(model=model))  # same task id as with the real model
```

Lines printed by `--write` and `write_to_store` pin by `instance_hash`
alone: it is the stored file's verified address. A reference is a
`Task[Model]` placeholder, so it fits `sd.TaskLoads[Model]` fields and
`sd.Depends[Model]` parameters, but not a field annotated with the concrete
class; `ref.hydrate()` returns the real task. See
[Where a reference fits](../how-to/reference-existing-tasks.md#where-a-reference-fits).

Without `instance_id` / `instance_hash`, the choice is made at load time:
`ref` and `from_registry` take, for a completed task, the instance its
completing execution ran, else the newest; `from_store` needs the store to
hold exactly one body of the task. Pin unless you want the choice made
again on every load.

### 6. Test offline

A test suite needs no registry for either handle:

- **Store pins** are read from the package itself: run the tests with
  `STARDAG_NO_REGISTRY=1` and no profile, and the store is the fixture.
  References are served from the store too, when it holds their body. See
  [Tests read the store offline](../how-to/committed-task-store.md#tests-read-the-store-offline).
- **Code that hydrates from a registry** can be tested against
  `stardag.testing.InMemoryRegistry`, seeded with
  `add_instance(body=..., task_id=..., instance_id=...)` from a body
  recorded next to the test. See
  [Testing code that uses references](../how-to/reference-existing-tasks.md#testing-code-that-uses-references).

### 7. Compare

Before moving a pin, or to see why two runs differ, compare the two
instances in the registry UI. **Compare**, in a task's Task Parameters
section, opens `/task-diff` with that instance on the left; the right side
is picked with the task search, pre-filtered to the same task type
(removable), or is another instance of the same task. The diff is
structural:

- added, removed and changed values, old and new both shown, with nested
  tasks labelled by their `namespace.Name`;
- identical subtrees collapsed, differing ones open, and a `+a −r ~c`
  summary (added, removed, changed) on every collapsed node;
- **only differences** hides everything that matches, and the fuzzy search
  of the parameter tree works across both sides.

Both sides live in the URL, so a comparison can be shared, for instance in
the review of a changed pin.

## Import resolution

A stored body names its task classes by namespace and name, and every
class in it, nested ones included, must be imported before the body can be
rebuilt. Stardag resolves this without code importing every task class by
hand:

- **Module hints.** Every file in the store records the module defining its
  class, and `from_store` imports it first. A body written by
  `stardag tasks ref --write` or `sd.write_to_store` loads with no
  configuration.
- **The package's `task_modules`.** Whenever rebuilding a body misses a
  class (`from_store` with a stale hint, `ref.hydrate()`, `from_registry`,
  `sd.task_from_body`), the `task_modules` of the applicable package are
  imported once per process and the lookup retried. They are configured
  next to the store, in the package's `pyproject.toml`:

  ```toml
  [tool.stardag]
  task_store = "src/my_package/stardag_task_store"
  task_modules = ["my_package.tasks.*", "!my_package.tasks.deploy"]
  ```

  `!` excludes a module the other patterns match — a deploy entry point
  that needs deploy-time environment at import, say. `stardag tasks ref`
  uses the same list to print the import line without `--module`.

Package settings live in `[tool.stardag]`, apart from registry
configuration (`.stardag/config.toml` and `~/.stardag/config.toml`), and
are read even with `STARDAG_NO_REGISTRY=1`. Each package of a repository has
its own; which one applies is found per call, from the calling module's
file. See
[Import resolution](../how-to/committed-task-store.md#import-resolution)
and [Which store a call reads](../how-to/committed-task-store.md#which-store-a-call-reads).

!!! warning "A namespace is an identity, not an import path"

    A task's namespace (`my_project.models`) often looks like a module path,
    and is often derived from one by `sd.auto_namespace`, but nothing ties
    the two together: a class can be moved, or its namespace set
    explicitly. So Stardag never imports a module because a namespace looks
    like one, and neither should code built on it. The registry UI's
    "Reference in code" box prints no import line for this reason. Imports
    come from the store's recorded module hints, the package's
    `task_modules`, or an explicit `--module`.

## Environment scope

- **The registry is scoped by workspace and environment.** A reference
  hydrates, and `from_registry` loads, from the environment of the active
  profile (or of the build's registry). A task registered in another
  environment of the same workspace is not found there: use that
  environment's profile, or pass its registry. This is the main reason to
  register key instances in the environment that matters.
- **The local body cache is partitioned the same way.** A pinned instance
  is cached under `~/.stardag/registry-cache/`, one partition per registry
  and environment, after its first read, so a pinned reference hydrates
  offline afterwards. `stardag cache clear [--all]` empties it, and
  `STARDAG_REGISTRY_CACHE=0` turns it off. See
  [Loading a task from the registry](../how-to/use-api-registry.md#loading-a-task-from-the-registry).
- **The store has no environment.** It is a copy kept in the package, and
  each file's `sources` records the registry, workspace and environment it
  was read from, as provenance only. Reading it needs no profile.
- **Remote workers** read a store only if their image has it: keep the
  store inside the importable package. See
  [Remote workers and deployed apps](../how-to/committed-task-store.md#remote-workers-and-deployed-apps).

## Bodies you keep yourself

`sd.task_from_body(body, expected_task_id=...)` rebuilds a task from an
instance body you hold — the output of `stardag tasks show <task-id> --json`,
or a saved `task.instance_body()` — with the same compat-mode rules and
identity check as every handle above.

## Migrating

- **`AliasTask` is deprecated.** Replace
  `sd.AliasTask[Model].from_registry(task_id)` with
  `TrainedModel.ref(task_id, instance_id=...)`, or pin it in the store with
  `stardag tasks ref <task-id> --write`. See
  [Migrating from `AliasTask`](../how-to/reference-existing-tasks.md#migrating-from-aliastask).
- **`sd.TaskRef` is now the typed reference.** The former dataclass of a
  task's name, version, id and slug is `sd.TaskSlug`. `sd.TaskRef.from_task`
  still returns one, with a `DeprecationWarning`; constructing
  `sd.TaskRef(name=..., ...)` or checking `isinstance(x, sd.TaskRef)` for
  the old class must switch to `sd.TaskSlug`.
- **A task field named `ref` or `from_store`** shadows the method every task
  class now has: pydantic warns at class definition and a type checker
  reports an incompatible override. The field still works. Reference such
  a class with the function forms, `sd.task_ref(MyTask, task_id, ...)` and
  `sd.task_from_store(MyTask, task_id, ...)`, which `stardag tasks ref`
  prints for it, or rename the field.
