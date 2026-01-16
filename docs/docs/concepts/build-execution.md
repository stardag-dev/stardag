# Build & Execution

Understanding how Stardag executes DAGs.

## The Build Function

The primary way to execute tasks is `sd.build()`:

```python
import stardag as sd

task = my_downstream_task(upstream=my_upstream_task())
sd.build(task)
```

## Execution Model

### Bottom-Up Execution

Stardag uses "Makefile-style" bottom-up execution:

1. Start at the requested task
2. Check if it's complete (output exists)
3. If not, recursively ensure dependencies are complete
4. Execute incomplete tasks in dependency order
5. Persist outputs

```
Requested: Task C (depends on B, which depends on A)

Step 1: Is C complete? No
Step 2: Is B complete? No
Step 3: Is A complete? No
Step 4: Execute A, save output
Step 5: Execute B, save output
Step 6: Execute C, save output
```

### Skip Logic

If a task's output already exists, it's skipped:

```python
# First run - all tasks execute
sd.build(task)

# Second run - nothing executes (all complete)
sd.build(task)
```

This is why deterministic parameter hashing is crucial.

## Build Methods

### Sequential Build

For debugging, you can use sequential execution (one task at a time):

```python
from stardag.build import build_sequential

build_sequential(task)  # or sd.build_sequential(task)
```

The default `sd.build()` uses concurrent execution for better performance.

### With Registry

Track builds in the Stardag API:

```python
from stardag.registry import APIRegistry

registry = APIRegistry(api_url="https://api.stardag.com")
sd.build(task, registry=registry)
```

See [Using the API Registry](../how-to/use-api-registry.md).

### With Prefect

For orchestration and observability:

```python
from stardag.integration.prefect.build import build as prefect_build

prefect_build(task)
```

See [Integrate with Prefect](../how-to/integrate-prefect.md).

## Task Execution

### The `run()` Method

When a task executes, its `run()` method is called:

```python
@sd.task
def my_task(x: int) -> int:
    # This is the run() implementation
    return x * 2

# Or explicitly:
class MyTask(sd.AutoTask[int]):
    x: int

    def run(self):
        self.output().save(self.x * 2)
```

### Dependency Injection

For `@task` decorated functions with `sd.Depends`:

1. Upstream tasks are built first
2. Their outputs are loaded
3. Values are injected into the function call

```python
@sd.task
def process(data: sd.Depends[list[int]]) -> int:
    # 'data' is already a list[int], not a task
    return sum(data)
```

## Error Handling

<!-- TODO: Document error handling behavior -->

### Failed Tasks

When a task fails:

- The exception propagates
- No output is saved
- Downstream tasks are not executed

### Retry Logic

<!-- TODO: Document retry configuration if available -->

## Parallelization

<!-- TODO: Document parallel execution options -->

### Current Limitations

The default sequential builder executes one task at a time. For parallel execution:

- Use Prefect integration for flow-level parallelism
- Use Modal integration for serverless scaling

## Monitoring

### With API Registry

```python
registry = APIRegistry(api_url="https://api.stardag.com")
sd.build(task, registry=registry)
# View progress at app.stardag.com
```

### With Prefect

```python
# View in Prefect UI
prefect_build(task)
```

## Calling Without Persistence

To run task logic without saving outputs:

```python
# Direct function call
result = my_task.call(x=5)

# Chain calls
result = downstream.call(upstream.call(10))
```

Useful for:

- Testing
- Quick experimentation
- When you don't need persistence
