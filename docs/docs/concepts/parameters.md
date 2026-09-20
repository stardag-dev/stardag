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

## Parameter Hashing

Parameter hashing gives each task instance a unique, deterministic identifier based on its parameters.

Parameter hashing solves several problems:

1. **Deterministic IDs**: Same parameters always produce the same task ID
2. **Unique paths**: Each configuration gets its own output location
3. **Caching**: Re-running with same parameters reuses existing outputs
4. **Composition**: Upstream task IDs are included in downstream hashes

## Three levels of significance

!!! tip "In short"

    Only parameters that change the **output** belong in the constructor.
    A knob that changes which upstreams are required or yielded is
    `significance="dependencies_only"`; one that changes only how the work
    is done is `"execution_only"`. Both are read from one **build config**
    per build, never passed at init. The how-to:
    [Evolve a DAG Safely](../how-to/evolve-dags.md).

Not every parameter is part of what a task _promises_. Stardag
distinguishes three levels, declared per field with
`sd.StardagField(significance=...)`:

| Level          | `significance`         | Affects                                                      | Comes from                                  |
| -------------- | ---------------------- | ------------------------------------------------------------ | ------------------------------------------- |
| 1 Identity     | `"identity"` (default) | the output — what the task promises; part of the task ID     | the constructor, like any parameter         |
| 2 Dependencies | `"dependencies_only"`  | which upstream tasks are required or yielded, not the output | the **build config**, never the constructor |
| 3 Execution    | `"execution_only"`     | neither output nor structure — only how the work is done     | the **build config**, never the constructor |

```{.python notest}
from typing import Annotated

class Aggregate(sd.Task[Summary]):
    __namespace__ = "reports"
    period: str                                                             # identity
    partition_size: Annotated[int, sd.StardagField(significance="dependencies_only")] = 100
    num_threads: Annotated[int, sd.StardagField(significance="execution_only")] = 4

    def run(self):
        # partition_size decides how many chunk tasks are yielded; the
        # output is the same however it is chunked.
        chunks = [Chunk(period=self.period, index=i) for i in range(self.partition_size)]
        yield chunks
        summary = merge((c.load() for c in chunks), threads=self.num_threads)
        self._save(summary)
```

A level 2 or 3 field is **never passed at init** — `Aggregate(period="2026-01",
num_threads=2)` raises. Give it a default: the build config overrides the
default, and a level 2 or 3 field without one would make every constructor
call demand a value that only the config may supply. It is read from the
**build config**, one mapping per build keyed by `namespace.Name`, or by the
bare `Name` for a task without a `__namespace__`:

```{.python notest}
sd.build(root, build_config={"reports.Aggregate": {"partition_size": 500, "num_threads": 8}})

# On Modal, the same argument on the trigger:
app.build_trigger(root, reactive=True, build_config={...})

# In tests, or anywhere no build is running:
with sd.build_config_scope({"reports.Aggregate": {"num_threads": 2}}):
    task = Aggregate(period="2026-01")  # num_threads == 2
```

Why one mechanism and not two: if one downstream could pass a partition
size to its upstream while another let the upstream read the config, there
would be two versions of one upstream in one build with one task ID. The
registry stores only identity parameters with a task; the build config is
stored with the build, and every worker installs it before it constructs or
rebuilds a task, so the two always agree.

The registry keys a build's dependency edges by a _structure scope_ — the
code version plus the `dependencies_only` config — which is what lets a
changed partition size or a changed `requires()` run without a version bump
and without disturbing builds already running. See
[Build & Execution](build-execution.md#structure-scope).

**Environment variables must not affect a task's output or its
dependency structure.** They may affect execution (a thread count read
from the environment is fine). Anything that changes what a task yields or
writes is a parameter or a `dependencies_only` config value; reading it from
the environment breaks the contract the shared structure relies on, and the
registry warns when it notices.

!!! note "`hash_exclude` is deprecated"

    `sd.StardagField(hash_exclude=True)` did what `significance="execution_only"`
    does — dropped the field from the hash — but allowed the value at init,
    which is exactly what the build config exists to prevent. It keeps working
    for one release with a `DeprecationWarning`; move the value to the build
    config and change the annotation.

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
- All parameter values (recursively hashed)

This recursive hashing ensures that:

- Changes to upstream parameters change downstream IDs
- The full DAG lineage is captured in the hash

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

    - How (and when) to exclude parameters from hashing -> task ID
    - How Task ID is obtained in more detail
    - Customizing hash behaviour
    - Compatibility mode validation
    - Task versioning
    - Best practices (examples for experimental ML and model hyperparameters)
