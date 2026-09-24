# Task Parameters

Parameters define the behaviour of a tasks run method, what the task does. Since Stardag tasks _are_ pydantic `BaseModel`s, we can use all pydantic features and patterns/best practices to declare a task's parameters.

As covered in previous sections, we can also pass other (arbitrarily nested) task instances as parameters; since they are also pydantic `BaseModel`s, this nesting is natural and results in a well-defined JSON schema.

## Polymorphism and `TaskLoads[...]`

A central feature that Stardag adds on top of standard pydantic is support for generalized _polymorphism_. Consider the example below:

```{.python notest}
class TrainedModel(sd.Task[MyModel]):
    config: MyModelConfig  # A regular pydantic model
    dataset: Dataset  # A specific Stardag Task

    def requires(self):
        return self.dataset

    def run(self):
        training_data = self.dataset.load()
        model = MyModel(config)
        model.fit(training_data)
        self._save(model)
        # ...
```

Here, we have declared that the `dataset` must be a specific task of type `Dataset`. This could be fine, but we typically want to be able to compare different training and test datasets from different sources with different pre-processing etc. and this is typically best reflected by differently composed tasks/DAGs.

Looking closer at the `run` method, we actually only care about the data type of `training_data` in:

```{.python notest}
training_data = self.dataset.load()
```

We can express this by instead using:

```{.python notest}
MyDataType = ...  # For example a pandas DataFrame with a pandera schema

class TrainedModel(sd.Task[MyModel]):
    config: MyModelConfig  # A regular pydantic model
    dataset: sd.TaskLoads[MyDataType]  # *Any* task, which .load() -> MyDataType.

```

`TaskLoads[<Type>]` is short for _any Stardag task for which the return type of `.load()` is `<Type>`_.

## Two identities

Every task has two identities, and it matters which one a given piece of
code needs.

| Identity             | Hashes                                                            | Answers                                              |
| -------------------- | ----------------------------------------------------------------- | ---------------------------------------------------- |
| `task.id`            | namespace, name, `version`, every **significant** field           | "Is this output done?" — global, across every build  |
| `task.instance_hash` | the same, plus every **non-significant** field — i.e. all of them | "How exactly was this task constructed?" — per scope |

`task.id` (the **task id**) is a promise about **output**. It is what
completion and the execution claim are keyed on, it is what names the
target, and it is global: any build, anywhere, that constructs a task with
the same task id is asking for the same thing.

`task.instance_hash` is the hash of the task's full **instance body** —
every field, defaults included, nested tasks embedded as their own full
bodies. The registry stores a construction of a task under a deterministic
scope (a deployment and its [settings](build-execution.md)) as an
**instance**: an `(deployment, settings, instance_hash)` row holding that
body. Two vocabulary words worth keeping straight, because the docs and
the UI use them precisely:

- An **instance** is that registry row.
- The Python object you construct is a **task object**.

One task object planned under two scopes is two instances; one instance
rehydrates into any number of task objects. `instance_hash` on its own is
never how you address an instance — it is only meaningful together with
the scope it was registered under, and the CLI/UI/API always address an
instance by its row id or by the full scope triple.

Two task objects can share a task id while differing in their
non-significant fields — two ways of asking for one completion. Globally
the registry stores as many of those as show up; **within one plan there
may be only one**, so two builds that construct the same task id
differently, in the same scope, at the same time, is a conflict raised at
the point of discovery.

## Significant and non-significant fields

!!! tip "In short"

    Only fields that change the **output** should be significant — the
    default. A field that changes only how the work is done, or which
    upstreams are required or yielded, but never the output, is
    `sd.StardagField(significant=False)`. Both kinds are ordinary
    constructor arguments. The how-to:
    [Evolve a DAG Safely](../how-to/evolve-dags.md).

Every field is one of two kinds, declared with
`sd.StardagField(significant: bool = True)`:

| Kind                  | `significant` | Affects                                                            |
| --------------------- | ------------- | ------------------------------------------------------------------ |
| Significant (default) | `True`        | the output — part of `task.id`                                     |
| Non-significant       | `False`       | how the work is done, or which upstreams it has — never the output |

```{.python notest}
from typing import Annotated

class Aggregate(sd.Task[Summary]):
    __namespace__ = "reports"
    period: str                                                     # significant
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

The file list is loaded from a static upstream; `partition_size` only
decides how it is sliced into `ChunkStats` tasks, and `num_threads` only
decides how fast the merge runs. Slicing 1,000 files by 100 or by 500
yields different chunk tasks but the same merged summary, so
`partition_size` and `num_threads` are both non-significant, even though
one affects structure and the other only execution — the model needs only
this one distinction; the API does not ask which of the two a
non-significant field is for.

Both kinds of field are **ordinary parameters**: passed at init like any
other, stored on the instance body, and rehydrated from it. There is no
"never at init, only from a build-wide config" restriction any more — a
non-significant field's value simply does not affect `task.id`, so two
task objects that differ only in a non-significant field share one
completion.

Only significant output matters for reuse across builds: if two builds
construct the task with the same significant fields, they are asking for
the same output, whatever their non-significant fields say. If they
construct it with the same significant fields but _different_
non-significant fields **in the same scope**, that is the one-instance-
per-plan conflict above.

**The three rules on what may affect what:**

- **Output** is a function of the significant fields and nothing else —
  not the code version, not an environment variable, not
  [settings](build-execution.md). That is the task-id promise, and
  keeping it is the user's job (bump `__version__` or add a significant
  field when it changes).
- **Structure** — which upstreams a task requires or yields — may depend
  on the code and its deployment, and on settings, but on nothing else
  from the environment. Within one scope (deployment + settings) it is
  therefore deterministic: reading an arbitrary environment variable from
  `requires()` is a contract breach, and the failure mode is over-gating
  (a stricter frontier than intended), never wrong output.
- **Execution** — how the work is done — may depend on anything: settings,
  the environment, wall-clock time, whatever you like.

## `compat_default`: adding a field without re-keying every downstream

```{.python notest}
new_field: Annotated[int, sd.StardagField(compat_default=0)] = 0
```

`compat_default` is valid only on a significant field (a non-significant
one is not in `task.id`, so there is nothing to keep stable). A
significant field whose value equals its `compat_default` is dropped from
the `task.id` hash, so adding the field to an existing task class does not
change the id of every instance that would otherwise get the default —
only instances that set a non-default value get a new id. It also lets a
missing field on rehydration take the `compat_default` rather than
failing.

Supply it in the field's validated Python form, not its serialized form —
both the hashing and the rehydration side compare against the raw value.

## Serialization stability

The instance body has no user-facing hash mode: it is simply the ordinary
pydantic serialization of every field, canonicalised (sorted keys, sorted
sets — including a set nested inside a list, dict or `Any` field —
compact separators, UTF-8). What the framework asks of your fields is one
thing: **stability**, a fixed point under its own round trip —
`dump(validate(dump(x))) == dump(x)`. At registration, the driver runs
that round trip once per distinct instance and raises
`UnstableSerializationError`, naming the field that moved, rather than
letting two processes disagree about one construction later.

Instabilities worth knowing about, because they are the ones the check
exists for:

- **Sets** iterate in a randomised, per-process order — sorted in every
  dump, so this is handled for you, but a custom serializer that bypasses
  the ordinary dump path can reintroduce it.
- **Floats** are stable when the value is (`repr` is the shortest
  round-trip form), but `-0.0` vs `0.0`, `NaN`, infinities, and numpy
  scalar types are not, and neither is a float computed
  non-deterministically in `__init__`.
- **Datetimes**: naive vs aware, or a custom serializer that drops
  precision, fail the round trip.
- **Defaults**: the instance body includes every field, set at init or
  not, so a changed class default under a new deployment produces a new
  `instance_hash` — correct, since it is a new scope anyway, but worth
  remembering when you compare instance hashes across a redeploy.

## The Task ID

Every task has an `id` property:

```python
from uuid import UUID

@sd.task
def add(a: int, b: int) -> int:
    return a + b

task = add(a=1, b=2)
assert task.id == UUID("fa9b74b1-1cde-5676-8650-dbcf755a2699")  # UUID-5
```

The task ID is derived from:

- Task name (class name or function name, unless overridden)
- Task namespace
- Task version
- Every **significant** parameter value (recursively hashed; a nested
  task appears by its own full body, so the outer id covers the nested
  task's parameters in full)

This recursive hashing ensures that:

- Changes to an upstream's significant parameters change downstream ids
- The full DAG lineage, at the significant level, is captured in the hash

**"I want to fix its output" means a new task id.** Changing what a task
produces is a change of promise: bump `__version__` or add/change a
significant parameter. That flows into every downstream id on its own,
because a downstream that takes the task as a parameter hashes its own id
from it — there is no other way to change a task's output for one build
without affecting every build that shares the completion.

## Output URIs

The task ID should typically determine the output URI, and does so automatically when using the Decorator API or `Task`:

```{.python continuation}
task = add(a=1, b=2)
print(task.target().uri)
# /path/to/.stardag/local-target-roots/default/add/fa/9b/fa9b74b1-1cde-5676-8650-dbcf755a2699.json
```

The default path structure is:

```
<target_root>/[<namespace>/]<name>/<id[0:2]>/<id[2:4]>/<id>.json
```

The `id[0:2]/id[2:4]` directory structure prevents having too many files in a single directory (facilitate file browsing in some filesystems).

!!! info "🚧 **Work in progress** 🚧"

    This documentation is still taking shape. It should soon cover:

    - `task.instance_hash` in more detail, and how a scope reuses it
    - Customizing hash behaviour
    - Task versioning
    - Best practices (examples for experimental ML and model hyperparameters)
