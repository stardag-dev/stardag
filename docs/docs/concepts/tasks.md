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
