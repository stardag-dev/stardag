# Task Parameters

Parameters define the behaviour of a tasks run method, what the task does. Since Stardag tasks _are_ pydantic `BaseModel`s, we can use all pydantic features and patterns/best practices to declare a task's parameters.

As covered in previous sections, we can also pass other (arbitrarily nested) task instances as parameters; since they are also pydantic `BaseModel`s, this nesting is natural and results in a well-defined JSON schema.

## Polymorphism and `TaskLoads[...]`

A central feature that Stardag adds on top of standard pydantic is support for generalized _polymorphism_. Consider the example below:

```{.python notest}
class TrainedModel(sd.AutoTask[MyModel]):
    config: MyModelConfig  # A regular pydantic model
    dataset: Dataset  # A specific Stardag Task

    def requires(self):
        return self.dataset

    def run(self):
        training_data = self.dataset.output().load()
        model = MyModel(config)
        model.fit(training_data)
        self.output().save(model)
        # ...
```

Here, we have declared that the `dataset` must be a specific task of type `Dataset`. This could be fine, but we typically want to be able to compare different training and test datasets from different sources with different pre-processing etc. and this is typically best reflected by differently composed tasks/DAGs.

Looking closer at the `run` method, we actually only care about the data type of `training_data` in:

```{.python notest}
training_data = self.dataset.output().load()
```

We can express this by instead using:

```{.python notest}
MyDataType = ...  # For example a pandas DataFrame with a pandera schema

class TrainedModel(sd.AutoTask[MyModel]):
    config: MyModelConfig  # A regular pydantic model
    dataset: sd.TaskLoads[MyDataType]  # *Any* task, which output().load() -> MyDataType.

```

`TaskLoads[<Type>]` is short for _any Stardag task for which the return type of `output().load()` is `<Type>`_.

## Parameter Hashing

Parameter hashing gives each task instance a unique, deterministic identifier based on its parameters.

Parameter hashing solves several problems:

1. **Deterministic IDs**: Same parameters always produce the same task ID
2. **Unique paths**: Each configuration gets its own output location
3. **Caching**: Re-running with same parameters reuses existing outputs
4. **Composition**: Upstream task IDs are included in downstream hashes

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

The task ID should typically determine the output URI, and does so automatically when using the Decorator API or `AutoTask`:

```{.python continuation}
task = add(a=1, b=2)
print(task.output().uri)
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
