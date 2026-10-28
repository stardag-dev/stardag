# Pin Tasks in a Committed Task Store

Keep a reviewed, registry-independent copy of the task instances your code
pins — a key dataset, a trained model — inside the package that pins them,
and load them eagerly as real task objects.

!!! tip "What you need to know"

    - **For pinned, long-lived instances in code, use the committed task
      store**, loaded eagerly: `TrainedModel.from_store(task_id, ...)`
      returns the real task, read from the package with no registry,
      profile or network.
    - **For ad hoc work** (a notebook loading an asset already materialized
      in the current environment), use a registry-backed
      [`TrainedModel.ref(...)`](reference-existing-tasks.md) or
      `from_registry` instead. Nothing needs saving to the store.
    - **Each package has its own store**, configured in its
      `pyproject.toml`. A pin reads its own package's store wherever it is
      imported from.
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

The task store is a directory in the package holding the instance bodies
the code pins. Changing a pin changes files in it, so the diff shows the
parameters that moved; reading one back needs nothing but the package.

## Set it up

Configure the store in the `[tool.stardag]` table of the package's
`pyproject.toml`. Put the store **inside the importable package**, so it
ships with the wheel and with any image the package is copied into:

```toml
# my-package/pyproject.toml
[tool.stardag]
task_store = "src/my_package/stardag_tasks"
task_modules = ["my_package.tasks.*", "!my_package.tasks.deploy"]
```

- `task_store` is a directory, created on first write. A relative path
  resolves against the directory of the `pyproject.toml` declaring it. The
  form `"pkg:my_package/stardag_tasks"` names a directory inside an
  importable package instead (resolved with `importlib.resources`), which is
  the same place whether the package runs from a checkout, an installed
  wheel or a container image.
- `task_modules` lists the modules that define the package's task classes,
  in the pattern grammar of `StardagApp(task_modules=...)`: an exact module
  (`"my_package.models"`), a package with a trailing recursive wildcard
  (`"my_package.tasks.*"`), and either prefixed with `!` to exclude it (a
  deploy entry point that needs deploy-time environment at import, say).
  See [Import resolution](#import-resolution).
- Ship the store as package data: hatchling includes every file under the
  package by default; with setuptools, add it to `package-data` (for example
  `"my_package" = ["stardag_tasks/**/*"]`).

This table is read only for the store and the task modules, never for
registry profiles, and is read even with `STARDAG_NO_REGISTRY=1`. Registry
configuration stays in `.stardag/config.toml` and `~/.stardag/config.toml`.

## Which store a call reads

Every call decides its own store, so a repository of several packages
works: a constant pinned in one package reads that package's store, even
when another package imports it, a test runner starts in another directory,
or the package is installed. For a relative or unset store, in order:

1. an explicit `store=` (relative: against the package root found below);
2. `STARDAG_TASK_STORE` (relative: against the working directory);
3. walking up from the **calling module's file** to the nearest
   `pyproject.toml` that has a `[tool.stardag]` table (one without the
   table, a monorepo root's, is passed). Frames inside the `stardag`
   package itself are never the caller;
4. the same walk from the **task class's defining module** (for a
   reference, the referenced class): what decides for a caller with no
   file of its own (a REPL, a notebook cell, `python -c`) or one whose walk
   finds nothing (a script loaded by file path from a `scripts/` directory);
5. the same walk from the **working directory**.

An **installed package** (under `site-packages`) has no `pyproject.toml`:
there, the store is the directory inside the package holding a
`stardag_task_store.toml` marker, which every store writer creates. The
walk never leaves an installed package, so a virtualenv inside another
repository does not pick up that repository's store.

`task_modules` follow the same order (without the explicit argument).

The **CLI** resolves from the working directory: run
`stardag tasks ref --write` in the package you write to.

To pin a store explicitly, relative to the module itself:

```{.python notest}
from pathlib import Path

BASELINE = TrainedModel.from_store(
    "6f1c...", instance_hash="7a2e...", store=Path(__file__).parent / "stardag_tasks"
)
```

## Write an instance into the store

From the registry, for an instance that ran (find its id in the UI or with
`stardag tasks list`), from the package's directory:

```sh
stardag tasks ref 6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b --write
```

```{.python notest}
from my_package.models import TrainedModel
TrainedModel.from_store("6f1c2c62-...", instance_id="0193b0c4-...", instance_hash="7a2e...")
```

The command reads the instance body (the newest, or `--instance <id>`),
checks that it rehydrates to its task id under today's code, writes it and
every nested task's body into the store, and prints the import and the
reader line. A body that no longer rehydrates is not written, and the error
says why. `--store DIR` writes elsewhere; it is printed as given, and a
relative one resolves against the package root, in the CLI as in the
reader. The configured store is never printed.

From a task object, with no registry involved, which also covers a
specification that is not built yet:

```{.python notest}
import stardag as sd

(stored,) = sd.write_to_store(TrainedModel(data=make_dataset("2026-q3")))
print(stored)
# from my_package.models import TrainedModel
# TrainedModel.from_store("6f1c...", instance_hash="7a2e...")
```

Prefer the registry path for anything that ran: the file then records the
registry instance id it was read as, and where it came from.

Commit the new files with the code that reads them.

## Read it back

```{.python notest}
from my_package.models import TrainedModel

BASELINE_MODEL = TrainedModel.from_store(
    "6f1c2c62-1f5c-5a0e-9b8e-3a4f0e1d2c3b",
    instance_id="0193b0c4-...",
    instance_hash="7a2e...",
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

- **Picking a body.** `instance_hash=` picks the body with that hash, which
  is verified (it is the file's address). `instance_id=` picks the body
  recorded as that registry instance, which is matched against the file's
  `sources`, unverified metadata. Given both, as the printed lines do, the
  body must match both, so an edited `sources` cannot select another body.
  With neither, the store must hold exactly one body of the task; with
  several, the error lists them.
- **The function form.** Every task class has `from_store`, so a class with
  a field named `from_store` shadows it. Use
  `sd.task_from_store(TrainedModel, task_id, ...)`, which is typed the same
  (it returns a `TrainedModel`) and never reads the attribute.
- **Errors** name what is missing and how to write it: no store configured,
  no body of the task, a file a pointer names that is not there, or a file
  that was edited by hand.
- **Nested drift.** When the stored body no longer recomputes to its task
  id, the error names the deepest nested task whose id moved — its field
  path and class — and the likely cause: a significant field added without
  `compat_default`, or one removed or renamed.

## The store format

```text
src/my_package/stardag_tasks/
├── stardag_task_store.toml           # marker: this directory is a store
├── my_package.models.Dataset/
│   └── 9c4c6f0f-.../                 # task id
│       └── 63a7f0ec-....json         # instance hash
└── my_package.models.TrainedModel/
    └── 896bc2e7-.../
        └── 1de4ec67-....json
```

One file per distinct instance body, at
`<task>/<task_id>/<instance_hash>.json`, where `<task>` is
`<namespace>.<Name>` (just `<Name>` for the root namespace), escaped to be a
safe directory name everywhere: characters outside `[A-Za-z0-9_.-]` become
`_`, and a leading dot or a name Windows reserves (`CON`, `NUL`, `COM1`, …)
gets a leading `_`. A file is an envelope around the body:

```json
{
  "stardag_task_store": 1,
  "task_id": "896bc2e7-c400-54c8-86e7-abea153501ab",
  "instance_hash": "1de4ec67-98c5-5a56-beba-7719d3cafd93",
  "namespace": "my_package.models",
  "name": "TrainedModel",
  "module": "my_package.models",
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
    "__namespace": "my_package.models",
    "data": {
      "__stardag_task": {
        "instance_hash": "63a7f0ec-b711-551f-9264-d543f3738b09",
        "task": "my_package.models.Dataset",
        "task_id": "9c4c6f0f-0c9c-5a1c-841e-baca5398428b"
      }
    },
    "learning_rate": 0.0003,
    "version": ""
  }
}
```

- **Normalized.** Every nested task in the body is a pointer to its own
  file. A shared upstream is stored once, however many downstream tasks
  embed it. The pointer names the instance hash, not only the task id: two
  downstream tasks may embed the same task with different
  [non-significant fields](../concepts/parameters.md#significant-and-non-significant-fields),
  and those are two bodies.
- **Following a pointer.** The pointed-at file is
  `<task>/<task_id>/<instance_hash>.json`, from the pointer's three fields.
  `task` is there to read and browse by; the task id and instance hash are
  the address. On read, `task` is checked against the target file's
  namespace and name, and a mismatch is refused.
- **Lossless and content-addressed.** A nested task's part of an instance
  body is exactly that task's own body, so re-inlining the pointers
  reproduces the original body, byte for byte, and its instance hash.
  Every file is checked against the hash it is stored under when it is
  read: a hand-edited file is refused. Rewriting the same content is a
  no-op, and a different body at an existing address is refused.
- **What is verified.** Only `body`, through the file's address. `task_id`,
  `namespace`, `name`, `module` and `sources` are metadata: `sources` is
  only trusted when you select by `instance_id` alone.
- **What looks like a task is stored as one.** A dict carrying the
  `__namespace` / `__name` keys of a registered task class is split into
  its own file, even in a plain `dict` field. Lossless all the same.
- **Deterministic and diff-friendly.** Two-space indentation with every
  object property and array element on its own line, sorted keys inside
  `body` (the envelope keeps its fixed order), non-ASCII text kept as is
  (UTF-8), and a trailing newline: the same content is always the same
  bytes. The formatting is independent of the instance hash, which is
  computed from the canonical compact body. Files are written atomically with an ordinary file mode, so a
  store copied into a container image is readable by its user.
- **`module`** is the defining module of the class that wrote the file,
  recorded as an import hint for the reader (`null` for a class defined in
  `__main__`; a known hint is never replaced by `null`). It is never
  derived from the namespace.
- **`sources`** says where the body came from: `"kind": "registry"` with
  the registry URL, workspace and environment the client resolved (and, on
  the instance that was read, its `instance_id`), or `"kind": "local"` for
  one written from a task object. One body may be several registry
  instances, so a file lists every instance id it was written as.

## Import resolution

A stored body names its classes by namespace and name only, and a namespace
is an identity, not an import location. Reading a body needs every class in
it imported, which the store and the package settings take care of, so code
does not import every task class by hand:

1. **Module hints.** Before validating a stored tree, `from_store` imports
   the recorded `module` of every class not yet registered. Anything written
   by `--write` or `write_to_store` loads with no configuration at all.
2. **The package's `task_modules`.** Whenever rehydration misses a class —
   `from_store` with a stale hint, `sd.task_from_body`, `from_registry`, a
   reference's hydration — the applicable `task_modules` are imported, once
   per process, and the lookup is retried. If the class is still missing,
   the error names it, the patterns and any module that failed to import.
   No lock is held while the modules import, so a module whose own import
   rehydrates a body cannot deadlock it.

`stardag tasks ref` uses the same `task_modules` to print a class's import
without `--module`.

## Trust

A store is code. Reading one imports the module each file names as its
class's hint, so point `store=` (or `task_store`) only at a store whose code
you would run: a reviewed one in your repository, or one shipped in a
package you installed.

## Tests read the store offline

A test suite reads the store with no registry and no profile:

```{.python notest}
# conftest.py sets STARDAG_NO_REGISTRY=1; the store is the package's own.
from my_package.pins import BASELINE_MODEL  # a from_store(...) constant


def test_baseline_is_the_reviewed_one():
    assert BASELINE_MODEL.learning_rate == 3e-4
```

No registry seeding, no recorded JSON fixtures: the store is the fixture.
For a test that needs an in-memory registry to serve an instance instead
(say, to test code that hydrates references from a registry), seed it with
`InMemoryRegistry.add_instance`, as in
[Reference Existing Tasks](reference-existing-tasks.md#testing-code-that-uses-references).

## A repository of several packages

```text
repo/
├── pyproject.toml                    # no [tool.stardag]: passed by the walk
├── features/
│   ├── pyproject.toml                # task_store = "src/features/stardag_tasks"
│   └── src/features/
│       ├── pins.py                   # DATASET = Dataset.from_store(...)
│       └── stardag_tasks/
└── training/
    ├── pyproject.toml                # task_store = "src/training/stardag_tasks"
    └── src/training/
        ├── pins.py                   # BASELINE = TrainedModel.from_store(...)
        └── stardag_tasks/
```

`training` may import `features.pins.DATASET`: that constant was loaded by
`features/src/features/pins.py`, so it reads `features`' store, whatever the
working directory. Each package's `stardag tasks ref --write` runs in its
own directory.

## References consult the store first

`ref.hydrate()` and the build-start hydration of every reference look in a
store before the body cache and the registry: the store of the package
whose code **created** the reference (it remembers that file, outside its
fields: not dumped, hashed, compared or pickled), else the referenced
class's package. A pinned reference is served from a body recorded under
its instance id, an unpinned one only when the store holds exactly one body
of the task. An explicit `ref.hydrate(registry=...)` asks that registry and
skips the store. A failed hydration says which store was checked, or that
none is configured.

## Remote workers and deployed apps

A process that runs your tasks remotely (a Modal worker, a reactive tick)
reads a store only if the image has it:

- **Ship the store with the package.** A store inside the package
  (`src/my_package/stardag_tasks`, or a `pkg:` spec) is copied into the
  image with the package source and found there by the same rules.
  Otherwise, set `STARDAG_TASK_STORE` in the image.
- **A module-level `from_store(...)` constant is evaluated at import.** If
  its module is matched by a deployed app's `task_modules`, it is imported
  in every tick container, so its store must be in the image, or every tick
  fails at import.
- A reference that a task returns from `requires()` is hydrated where that
  task runs; with no store in the image, it reads the registry there. A
  reference created in driver code and passed to `sd.build` is hydrated in
  the driver, where the store is.

## Recommended procedure for key instances

1. **Build or register the instance in the environment that matters**
   (typically production) — with `sd.build`, or by registering it without
   running. Tag and describe it there, so it is found by search and
   browsable in the UI.
2. **Copy it into the package's store**: run
   `stardag tasks ref <task-id> --write` from the package directory,
   against that environment's profile.
3. **Pin it in code** with the printed `from_store(...)` line, and commit
   the store files with it. The review shows the parameters.
4. **Leave the copy alone.** Store files are immutable: to move a pin,
   write the new instance and change the line that reads it.
