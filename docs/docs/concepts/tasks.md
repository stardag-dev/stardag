# Tasks

Tasks are the fundamental building blocks of Stardag. A task represents a unit of work that produces an output.

## What is a Task?

A task is:

- A **specification** of what to compute
- A **Pydantic model** with typed parameters
- **Serializable** to JSON for storage and transfer
- **Hashable** to produce a deterministic ID

## The Task Contract and Core Interface

Below is a minimal example of task:

```python
import stardag as sd

# Some external persistent state (typically *not* in memory as here)
world_state = {}

class MyTask(sd.BaseTask):
    # Declare any parameters
    parameter: str

    def run(self):
        # do some work
        result = len(self.parameter)
        # persist the result
        world_state[self.parameter] = result

    def complete(self):
        # let the outside world know if this task is complete
        return self.parameter in world_state
```

Even if contrived, it emphaises the the fundamental contract of a stardag task; At the very least, any task must implement the methods `complete` and `run`, and:

- `complete` should return `True` only if the task's desired world state is achieved
- `run` should only execute succesfully once this state is achieved

To defined how tasks depends on other tasks, each task must also implement the method:

```{.python notest}
    def requires(self) -> TaskStruct | None:
```

for which `BaseTask` default implementation simply returns `None` (no dependencies). When a task do return one or more tasks, it can - _and should_ - make the assumprion that:

- all tasks returned from `self.requires()` are complete when `self.run()` is executed.

To some extent, _that's it_.

This allows us to implement build logic that traverses the Directled Acyclic Graph (DAG) of tasks and executes `run`, in the correct order until the final desired tasks are complete.

```{.python continuation}
# instantiate an instance
my_task = MyTask(parameter="hello")

# build (or "materialize") the task and upstream
sd.build(my_task)

assert world_state == {"hello": 5}
```

In the following section we will cover the fact the most tasks uses `Target`s, and in particular `FileSystemTarget`s, to persistently store their output and for downstream tasks to retrive it as input.

<!--
## Three Ways to Define Tasks

Stardag offers three APIs for defining tasks, each with different trade-offs:

### 1. The `@task` Decorator

Best for simple tasks where dependencies are injected as parameters:

```python
import stardag as sd

@sd.task
def process_data(input_data: sd.Depends[list[int]]) -> list[int]:
    return [x * 2 for x in input_data]
```

**When to use:**

- All dependencies are injected via parameters
- You want minimal boilerplate
- Type annotations can express the target type

### 2. The `AutoTask` Class

Best when you need to compute dependencies dynamically:

```python
import stardag as sd

class ProcessData(sd.AutoTask[list[int]]):
    multiplier: int
    input_data: sd.TaskLoads[list[int]]

    def requires(self):
        return self.input_data

    def run(self):
        data = self.input_data.output().load()
        self.output().save([x * self.multiplier for x in data])
```

**When to use:**

- Dependencies need to be computed from parameters
- You need to override specific behaviors
- You want the convenience of automatic target creation

### 3. The Base `Task` Class

Best for full control over all aspects:

```python
import stardag as sd
from stardag.target import LoadableSaveableFileSystemTarget
from stardag.target.serialize import JSONSerializer, Serializable

class ProcessData(sd.Task[LoadableSaveableFileSystemTarget[list[int]]]):
    multiplier: int
    input_data: sd.TaskLoads[list[int]]

    def requires(self):
        return self.input_data

    def output(self) -> LoadableSaveableFileSystemTarget[list[int]]:
        return Serializable(
            wrapped=sd.get_target(f"process_data/{self.task_id}.json", task=self),
            serializer=JSONSerializer(list[int]),
        )

    def run(self):
        data = self.input_data.output().load()
        self.output().save([x * self.multiplier for x in data])
```

**When to use:**

- Non-filesystem targets (database rows, API calls)
- Custom serialization logic
- Complete control over output paths

## Task Lifecycle

```
┌──────────────┐
│ Instantiate  │  task = MyTask(param=value)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Inspect    │  task.complete(), task.requires(), task.output()
└──────┬───────┘
       │
       ▼
┌──────────────┐
│    Build     │  sd.build(task)
└──────┬───────┘
       │
       ▼
┌──────────────┐
│    Load      │  task.output().load()
└──────────────┘
```

## Key Methods

| Method       | Description                               |
| ------------ | ----------------------------------------- |
| `requires()` | Returns upstream task dependencies        |
| `output()`   | Returns the target where output is stored |
| `complete()` | Returns `True` if output exists           |
| `run()`      | Executes the task logic                   |
| `task_id`    | Deterministic hash of parameters          |

## Task Properties

Tasks are Pydantic models, so they support:

```python
task = process_data(input_data=source())

# Serialization
task.model_dump_json(indent=2)

# Validation
task.model_validate({"input_data": {...}})

# Schema
ProcessData.model_json_schema()
```

## Versioning

Tasks support versioning to invalidate cached outputs:

```python
@sd.task(version="1.0")
def my_task(x: int) -> int:
    return x * 2
```

Changing the version changes the task ID, causing re-execution.


## Namespacing

Organize tasks with namespaces:

```python
@sd.task(namespace="data.preprocessing")
def clean_data(raw: sd.Depends[str]) -> str:
    return raw.strip()
```

TODO:  -->
