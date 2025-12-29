# Your First DAG

Build a complete pipeline with task dependencies.

## Adding Dependencies

Use `sd.Depends` to declare that a task depends on another task's output:

```python
import stardag as sd

@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)

# Create the DAG
task = get_sum(integers=get_range(limit=10))

# Build executes both tasks in the correct order
sd.build(task)

print(task.output().load())  # 45
```

## How Dependencies Work

### Declaration

```python
def get_sum(integers: sd.Depends[list[int]]) -> int:
```

`sd.Depends[list[int]]` tells Stardag:

1. This parameter expects a task that outputs `list[int]`
2. The input task's output will be loaded and passed to the function

### Composition

```python
task = get_sum(integers=get_range(limit=10))
```

You pass a _task instance_ as the parameter, not a value. Stardag handles:

- Determining the execution order
- Building the upstream task first
- Loading the output and injecting it

### Inspection

```python
# View dependencies
print(task.requires())
# {'integers': get_range(version=None, limit=10)}

# View the full DAG as JSON
print(task.model_dump_json(indent=2))
```

## Building Multiple Branches

DAGs can have multiple branches:

```python
@sd.task
def add(a: float, b: float) -> float:
    return a + b

@sd.task
def multiply(a: float, b: float) -> float:
    return a * b

@sd.task
def combine(
    sum_result: sd.Depends[float],
    product_result: sd.Depends[float]
) -> float:
    return sum_result + product_result

# Diamond-shaped DAG
result = combine(
    sum_result=add(a=1, b=2),
    product_result=multiply(a=3, b=4)
)

sd.build(result)
print(result.output().load())  # 15.0 (3 + 12)
```

## Reusing Task Results

Because outputs are persisted with deterministic paths, running the same task twice skips execution:

```python
task = get_sum(integers=get_range(limit=10))

# First build - executes both tasks
sd.build(task)

# Second build - both tasks already complete, nothing runs
sd.build(task)
```

This is the "Makefile-style" bottom-up execution model.

## Calling Functions Directly

Sometimes you want to call the underlying function without persistence:

```python
# Using .call() bypasses targets and returns the raw result
result = get_sum.call(get_range.call(10))
print(result)  # 45
```

## What's Next?

- Learn about [Tasks](../concepts/tasks.md) in depth
- Understand [Parameter Hashing](../concepts/parameter-hashing.md)
- Explore [How to Define Tasks](../how-to/define-tasks.md) using different APIs
