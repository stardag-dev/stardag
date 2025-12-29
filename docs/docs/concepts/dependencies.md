# Dependencies

Dependencies define how tasks relate to each other and form the DAG structure.

## Dependency Types

Stardag provides several ways to declare dependencies:

### `sd.Depends[T]`

Inject a task's output directly into the function:

```python
@sd.task
def downstream(data: sd.Depends[list[int]]) -> int:
    # data is already loaded - it's list[int], not a task
    return sum(data)
```

The upstream task's output is loaded automatically.

### `sd.TaskLoads[T]`

Get access to the task object (for class-based tasks):

```python
class Downstream(sd.AutoTask[int]):
    data: sd.TaskLoads[list[int]]

    def requires(self):
        return self.data  # Return the task for dependency tracking

    def run(self):
        loaded = self.data.output().load()  # Manually load
        self.output().save(sum(loaded))
```

Use when you need the task object, not just its output.

### `sd.TaskSet[T]`

Handle multiple tasks of the same type:

```python
@sd.task
def aggregate(items: sd.TaskSet[int]) -> int:
    return sum(item for item in items)

# Usage
result = aggregate(items=[task_a(), task_b(), task_c()])
```

<!-- TODO: Verify TaskSet behavior and usage patterns -->

## Composability

Stardag's key innovation is treating task instances as first-class parameters.

### The Problem with Traditional DAG Frameworks

In Luigi and similar frameworks, dependencies are often expressed through inheritance:

```python
# Luigi-style (not Stardag)
class ChildTask(luigi.Task):
    def requires(self):
        return ParentTask()  # Tightly coupled!
```

This creates tightly coupled DAGs where changing upstream tasks requires modifying downstream code.

### Stardag's Solution

Tasks receive dependencies as parameters:

```python
@sd.task
def downstream(upstream: sd.Depends[int]) -> int:
    return upstream * 2

# Compose at instantiation time
result = downstream(upstream=task_a())
# Or use a different upstream
result = downstream(upstream=task_b())
```

Benefits:

- **Loose coupling**: Downstream tasks don't know upstream implementation
- **Reusability**: Same task works with different inputs
- **Testability**: Easy to mock dependencies
- **Flexibility**: Compose DAGs dynamically

## Type Contracts

Dependencies have type expectations:

```python
@sd.task
def process(data: sd.Depends[list[int]]) -> float:
    return sum(data) / len(data)
```

The `list[int]` type annotation declares:

1. What type the upstream task must output
2. What type will be received at runtime

This creates clear contracts between tasks.

## Dynamic Dependencies

For dependencies that depend on parameters, use class-based tasks:

```python
class ProcessMultiple(sd.AutoTask[list[int]]):
    count: int

    def requires(self):
        # Generate dependencies based on parameters
        return [GenerateData(index=i) for i in range(self.count)]

    def run(self):
        results = [dep.output().load() for dep in self.requires()]
        self.output().save(results)
```

<!-- TODO: Add more examples of dynamic dependencies -->

## Inspecting Dependencies

```python
task = downstream(upstream=source(x=1))

# View immediate dependencies
print(task.requires())
# {'upstream': source(version=None, x=1)}

# Full JSON representation includes nested tasks
print(task.model_dump_json(indent=2))
```
