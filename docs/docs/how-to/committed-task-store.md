# Pin Tasks in a Committed Task Store

Keep a reviewed, registry-independent copy of the task instances your code
pins — a key dataset, a trained model — in the repository, and load them
eagerly as real task objects.

!!! tip "What you need to know"

    - **For pinned, long-lived instances in code, use the committed task
      store**, loaded eagerly: `TrainedModel.from_store(task_id, ...)`
      returns the real task, read from the repository with no registry,
      profile or network.
    - **For ad hoc work** (a notebook loading an asset already materialized
      in the current environment), use a registry-backed
      [`TrainedModel.ref(...)`](reference-existing-tasks.md) or
      `from_registry` instead. Nothing needs saving to the store.
    - **`stardag tasks ref <task-id> --write`** copies an instance from the
      registry into the store and prints the line to paste;
      **`sd.write_to_store(task)`** does the same from a task object, for a
      task not built yet.
    - **Also register key instances in the environment that matters**
      (usually production), so they stay searchable and browsable there with
      tags and descriptions. The registry is the record of what ran; the
      store is a reviewed, immutable copy of it.

## Why a committed copy

A [reference](reference-existing-tasks.md) is the right handle while you
work against a registry: it costs nothing to construct, and a build
hydrates it. For the handful of instances a code base is built around, two
things are missing:

- **Review.** A changed pin in a reference is two new UUIDs in a diff. A
  reviewer cannot see what the code now points at.
- **Independence.** Hydrating a reference needs the registry and
  environment the task was registered in. An isolated test suite, a fresh
  checkout or a laptop on a train has neither.

The task store is a directory in the repository holding the instance bodies
the code pins. Changing a pin changes files in it, so the diff shows the
parameters that moved; reading one back needs nothing but the repository.

## Set it up

Configure the store in the project config, committed with the code:

```toml
# <repo>/.stardag/config.toml
[project]
task_store = "stardag-tasks"
task_modules = ["my_project.tasks.*"]
```

- `task_store` is a directory, created on first write. A relative path
  resolves against the **project root** (the directory holding
  `.stardag/`), not the working directory, so the same setting works from
  any subdirectory, a notebook or a test runner.
- `task_modules` lists the modules that define the project's task classes,
  in the pattern grammar of `StardagApp(task_modules=...)`: an exact module
  (`"my_project.models"`) or a package with a trailing recursive wildcard
  (`"my_project.tasks.*"`). See [Import resolution](#import-resolution).

`STARDAG_TASK_STORE` and `STARDAG_TASK_MODULES` (comma-separated) override
both; a relative `STARDAG_TASK_STORE` resolves against the working
directory. Every function and the CLI also take an explicit store
directory. The project settings are read even with `STARDAG_NO_REGISTRY=1`.

## Write an instance into the store

From the registry, for an instance that ran (find its id in the UI or with
`stardag tasks list`):

```sh
stardag tasks ref 6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b --write
```

```{.python notest}
from my_project.models import TrainedModel
TrainedModel.from_store("6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b", instance_id="0193b0c4-...")
```

The command reads the instance body (the newest, or `--instance <id>`),
checks that it rehydrates to its task id under today's code, writes it and
every nested task's body into the store, and prints the import and the
reader line. A body that no longer rehydrates is not written, and the error
says why. `--store DIR` writes elsewhere than the configured store.

From a task object, with no registry involved, which also covers a
specification that is not built yet:

```{.python notest}
import stardag as sd

(stored,) = sd.write_to_store(TrainedModel(data=make_dataset("2026-q3")))
print(stored)
# from my_project.models import TrainedModel
# TrainedModel.from_store("6f1c...", instance_hash="7a2e...")
```

Prefer the registry path for anything that ran: the file then records the
registry instance id it was read as, and where it came from.

Commit the new files with the code that reads them.

## Read it back

```{.python notest}
from my_project.models import TrainedModel

BASELINE_MODEL = TrainedModel.from_store(
    "6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b",
    instance_id="0193b0c4-...",
)

BASELINE_MODEL.learning_rate              # a real TrainedModel
candidate = BASELINE_MODEL.model_copy(update={"learning_rate": 1e-4})
evaluation = Evaluate(model=BASELINE_MODEL)
```

`from_store` is eager: it returns the task object itself, so its fields are
readable and derivations with `model_copy` work, which a reference refuses.
It re-inlines the stored tree, imports the classes in it, validates the body
in compat mode (see [Evolve a DAG Safely](evolve-dags.md)) and checks that
the result is a `TrainedModel` with that task id.

- **Picking a body.** `instance_id=` picks the body recorded as that
  registry instance, `instance_hash=` the body with that hash. With neither,
  the store must hold exactly one body of the task; with several, the error
  lists them.
- **The function form.** Every task class has `from_store`, so a class with
  a field named `from_store` shadows it. Use
  `sd.task_from_store(TrainedModel, task_id, ...)`, which is typed the same
  (it returns a `TrainedModel`) and never reads the attribute.
- **Errors** name what is missing and how to write it: no store configured,
  no body of the task, a file a pointer names that is not there, or a file
  that was edited by hand.

## The store format

```text
stardag-tasks/
├── my_project.models.Dataset/
│   └── 9c4c6f0f-.../                 # task id
│       └── 63a7f0ec-....json         # instance hash
└── my_project.models.TrainedModel/
    └── 896bc2e7-.../
        └── 1de4ec67-....json
```

One file per distinct instance body, at
`<namespace>.<Name>/<task_id>/<instance_hash>.json` (`<Name>` alone for the
root namespace). The class directory is there for browsing; lookups go by
task id and instance hash. A file is an envelope around the body:

```json
{
  "stardag_task_store": 1,
  "task_id": "896bc2e7-c400-54c8-86e7-abea153501ab",
  "instance_hash": "1de4ec67-98c5-5a56-beba-7719d3cafd93",
  "namespace": "my_project.models",
  "name": "TrainedModel",
  "module": "my_project.models",
  "sources": [
    {
      "environment": "production",
      "instance_id": "0193b0c4-...",
      "kind": "registry",
      "registry_url": "https://api.example.com",
      "stardag_version": "0.30.0",
      "workspace": "my-team",
      "written_at": "2026-10-28T12:20:35+00:00"
    }
  ],
  "body": {
    "__name": "TrainedModel",
    "__namespace": "my_project.models",
    "data": {
      "__stardag_task": {
        "instance_hash": "63a7f0ec-b711-551f-9264-d543f3738b09",
        "task_id": "9c4c6f0f-0c9c-5a1c-841e-baca5398428b"
      }
    },
    "learning_rate": 0.0003,
    "version": ""
  }
}
```

- **Normalized.** Every nested task in the body is a pointer,
  `{"__stardag_task": {"task_id", "instance_hash"}}`, to its own file. A
  shared upstream is stored once, however many downstream tasks embed it,
  so a changed pin diffs as the files that changed and nothing else. The
  pointer names the instance hash, not only the task id: two downstream
  tasks may embed the same task with different
  [non-significant fields](../concepts/parameters.md#significant-and-non-significant-fields),
  and those are two bodies.
- **Lossless and content-addressed.** A nested task's part of an instance
  body is exactly that task's own body, so re-inlining the pointers
  reproduces the original body, byte for byte, and its instance hash. Every
  file is checked against the hash it is stored under when it is read: a
  hand-edited file is refused. Rewriting the same content is a no-op, and a
  different body at an existing address is refused.
- **Deterministic.** Sorted body keys, a fixed envelope order, two-space
  indentation and a trailing newline: the same content is always the same
  bytes.
- **`module`** is the defining module of the class that wrote the file,
  recorded as an import hint for the reader (`null` for a class defined in
  `__main__`). It is never derived from the namespace.
- **`sources`** says where the body came from: `"kind": "registry"` with
  the registry URL, workspace and environment (and, on the instance that
  was read, its `instance_id`), or `"kind": "local"` for one written from a
  task object. One body may be several registry instances, so a file lists
  every instance id it was written as.

## Import resolution

A stored body names its classes by namespace and name only, and a namespace
is an identity, not an import location. Reading a body needs every class in
it imported, which the store and the project config take care of, so code
does not import every task class by hand:

1. **Module hints.** Before validating a stored tree, `from_store` imports
   the recorded `module` of every class not yet registered. Anything written
   by `--write` or `write_to_store` loads with no configuration at all.
2. **The project's `task_modules`.** Whenever rehydration misses a class —
   `from_store` with a stale hint, `sd.task_from_body`, `from_registry`, a
   reference's hydration — the configured modules are imported, once per
   process, and the lookup is retried. If the class is still missing, the
   error names it, the configured patterns and any module that failed to
   import.

`stardag tasks ref` uses the same `task_modules` to print a class's import
without `--module`.

## Tests read the store offline

A test suite reads the store with no registry and no profile:

```{.python notest}
# conftest.py sets STARDAG_NO_REGISTRY=1; the store comes from
# .stardag/config.toml in the repository.
from my_project.models import TrainedModel
from my_project.pins import BASELINE_MODEL  # a from_store(...) constant


def test_baseline_is_the_reviewed_one():
    assert BASELINE_MODEL.learning_rate == 3e-4
```

No registry seeding, no recorded JSON fixtures: the store is the fixture.
For a test that needs an in-memory registry to serve an instance instead
(say, to test code that hydrates references from a registry), seed it with
`InMemoryRegistry.add_instance`, as in
[Reference Existing Tasks](reference-existing-tasks.md#testing-code-that-uses-references).

## References consult the store first

`ref.hydrate()` and the build-start hydration of every reference look in
the configured store before the body cache and the registry: a pinned
reference is served from a body recorded under its instance id, an unpinned
one only when the store holds exactly one body of the task.

One caveat: a reference that a task returns from `requires()` is hydrated
wherever that task runs. Inside a remote container (a Modal worker, a
reactive tick) there is no repository and so no store, unless the image
bakes one in, and the reference is read from the registry there. A
reference created in driver code and passed to `sd.build` is hydrated in
the driver, where the store is.

## Recommended procedure for key instances

1. **Build or register the instance in the environment that matters**
   (typically production) — with `sd.build`, or by registering it without
   running. Tag and describe it there, so it is found by search and
   browsable in the UI.
2. **Copy it into the store**: `stardag tasks ref <task-id> --write`
   against that environment's profile.
3. **Pin it in code** with the printed `from_store(...)` line, and commit
   the store files with it. The review shows the parameters.
4. **Leave the copy alone.** Store files are immutable: to move a pin,
   write the new instance and change the line that reads it.
