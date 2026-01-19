# Using the Class-API for Defining Tasks

In the previous section, we used the `@sd.task` _Function Decorator_-API to define tasks. This is suitable if you want the least boilerplate possible to turn basic python functions into stardag tasks and DAGs.

For more control, and to some extent clarity, you can define tasks by subclassing [`sd.BaseTask`](../reference/api.md#stardag.BaseTask), [`sd.Task`](../reference/api.md#stardag.Task), [`sd.AutoTask`](../reference/api.md#stardag.AutoTask).

!!! info "Stardag Tasks are Pydantic Models"

    Note that all of these base classes (and all stardag tasks!) _are_ pydantic [`BaseModel`](https://docs.pydantic.dev/latest/api/base_model/)s, and hence pretty much anything that you can do with a pydantic model, you can also do with tasks, including serialization, validation, field annotations and etc.

For most scenarios, _the Class-API is the recommended way to define tasks_.

## Subclassing [`sd.AutoTask`](../reference/api.md#stardag.AutoTask)

In the previous section we defined the following DAG with two tasks:

```python
@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)

# Compose the DAG
root_task = get_sum(integers=get_range(limit=10))
```

The corresponding way to use the class API but still get as much out of the box as possible is to subclass [`sd.AutoTask`](../reference/api.md#stardag.AutoTask). The following produces a functionally equivalent DAG:

```python
class Range(sd.AutoTask[list[int]]):
    limit: int

    def run(self):
        self.output().save(list(range(self.limit)))


class Sum(sd.AutoTask[int]):
    integers: sd.TaskLoads[list[int]]

    def requires(self):
        return self.integers

    def run(self):
        self.output().save(sum(self.integers.output().load()))

# Compose the DAG
root_task = Sum(integers=Range(limit=10))

# Build
sd.build(root_task)

# Load
print(root_task.output().load())  # 45
```

### Specifying the `output().load()` type

The decorator-API parses the function return type annotation to understand how the task result should be serialized (`-> int`).

```{.python notest}
@sd.task
def get_sum(integers: sd.Depends[list[int]]) -> int:
```

With [`sd.AutoTask`](../reference/api.md#stardag.AutoTask) subclassing, the result type annotation goes as a parameter to the `AutoTask`:

```{.python notest}
class Sum(sd.AutoTask[int]):
```

This let's `AutoTask` automatically implement the `output()` with the appropriate Serializer and target URI. Under the hood, this is roughly the corresponding implementaton of `output()`:

```{.python notest}
    # ...
    def output(self) -> LoadableSaveableFileSystemTarget[int]:
        return Serializable(
            wrapped=sd.get_target(default_relpath(self)),
            serializer=JSONSerializer(int),
        )
```

Which implements the `load()` and `save()` methods which take care of de/serialization and persistance.

### Specifying dependencies

The decorator-API parses the function argument type annotation (`sd.Depends[list[int]]`) to understand its dependencies.

```{.python notest}
@sd.task
def get_sum(integers: sd.Depends[list[int]]) -> int:
```

With the class-API (irrespective of which base class used), we can specify other tasks as parameters to a task, either by specifying the exact type expected

```{.python notest}
class Sum(sd.AutoTask[int]):
    integers: Range
```

or by only specifying the required "`TaskLoads`" type of the input task:

```{.python notest}
class Sum(sd.AutoTask[int]):
    integers: sd.TaskLoads[list[int]]
```

In the former case `integers: Range`, only instances of `Range` task is accepted, validated by standard pydantic validation logic (remember all tasks are pydantic `BaseModel`s). But this is an unnecessarilly specific and narrow constrain since the implementation of `Sum` only depdens on that `integers.output().load()` returns `list[int]`. This syntax `TaskLoads[<type>]` is what allows for smooth composability of tasks into DAGs, while still being declarative.

Note that so far we have only specified expectations on the `integers` input argument of the `Sum` task. To properly declare that this is an upstream dependency of `Sum`, we also need to return it from the `requires()` method:

```{.python notest}
class Sum(sd.AutoTask[int]):
    integers: sd.TaskLoads[list[int]]

    def requires(self):
        return self.integers
```

??? info "Why do you need to implement `requires()` when you already declare `sd.TaskLoads`?"

    With the decorator-API, input arguments with type annotation `sd.Depends[<type>]` were autoamtically returned from the generated task's `requires()` method. But in the more capable class-API case, it is better to be explicit because:

    1. Tasks can be passed as inputs in nested data structures (`parameter: dict[str, list[TaskLoads[<type>]]]`) which makes it more complex to parse arguments to extract the tasks.
    2. We might not want to depend on the input task directly, but include it as input to other dependencies e.g.:

    ```python

    class PerformanceComparison(sd.AutoTask[dict]):
        models: list[Subclass[MLModelBase]]
        train_dataset: TaskLoads[MyDatasetType]
        test_dataset: TaskLoads[MyDatasetType]

        def requires(self):
            # reqiure a "Train->Predict->Metrics" DAG for each model specification.
            return [
                Metrics(
                    predictions=Predictions(
                        trained_model=TrainedModel(
                            model=model,
                            dataset=self.train_dataset,
                        ),
                        dataset=self.test_dataset,
                    ),
                )
                for model in self.models
            ]
            # NOTE that `train_dataset` and `test_dataset`

        def run(self):
            metrics_dicts = [metrics.load() for metrics in self.requires()]
            # compare and summarize...
    ```

### Implementing the task's run logic

The `run` (or `run_aio` in the case of async tasks) method implements the actual task logic, it generally involves the following steps:

1. Load input data from the task's dependencies
2. Transform the data (apply the main run logic)
3. Save the data to the task target

So in with the class-API we are responsible for loading output from dependencies and storing the results to the target, but the automaticaly implemented `output()` (returning a `LoadableSaveableFileSystemTarget`) makes this straight forward:

```{.python notest}
    # ...
    def run(self):
        # Load input data
        input_values = self.integers.output().load()
        # Execute operation(s)
        result = sum(input_values)
        # Save result
        self.output().save(result)

```

## What's Next?

- Learn about [Tasks](../concepts/tasks.md) in depth
- Understand [Parameter Hashing](../concepts/parameter-hashing.md)
<!-- - Explore [How to Define Tasks](../how-to/define-tasks.md) using different APIs -->
