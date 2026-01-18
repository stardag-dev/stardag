# Quick Start

Create and run your first Stardag task in 5 minutes.

## Hello World

The simplest Stardag task is a function decorated with `@sd.task`:

```python
import stardag as sd

@sd.task
def hello() -> str:
    return "Hello, Stardag!"

# Create a task instance
task = hello()

# Build (execute) the task
sd.build(task)

# Load the result
print(task.output().load())  # "Hello, Stardag!"
```

## Understanding the Flow

Let's break down what happened:

### 1. Task Definition

```{.python}
import stardag as sd

@sd.task
def hello() -> str:
    return "Hello, Stardag!"
```

The `@sd.task` decorator transforms your function into a task class. The return type annotation (`-> str`) tells Stardag how to serialize the output.

### 2. Task Instantiation

```{.python continuation}
task = hello()
```

This creates a task _instance_ - a declarative specification of what to compute. No computation happens yet.

### 3. Building

```{.python continuation}
sd.build(task)
```

`build()` executes the task and saves the output to a deterministic file path.

### 4. Loading Results

```{.python continuation}
task.output().load()
```

Retrieve the persisted result from storage.

## Task with Parameters

Tasks can have parameters:

```{.python continuation}
@sd.task
def greet(name: str) -> str:
    return f"Hello, {name}!"

task = greet(name="World")
sd.build(task)
print(task.output().load())  # "Hello, World!"
```

## Key Concepts

- **Declarative**: Task instances describe what to compute, not when.
- **Persistent**: Results are saved to remote or local filesystem automatically (by default).
- **Deterministic**: Output URIs are determined by task parameters; task instances are pointers to _assets_ which we can reason about (know the location of) before they are materialized.

## Inspecting Tasks

Tasks are Pydantic models with useful properties:

```{.python continuation}
task = greet(name="World")

# View the task specification
print(repr(task))
# greet(version=None, name='World')

# Check if already complete
print(task.complete())
# False (before build), True (after build)

# View output path
print(task.output().uri)
# /path/to/.stardag/target-roots/default/greet/ab/cd/abcd1234....json
```

## What's Next?

Continue to [Your First DAG](first-dag.md) to learn how to create tasks with dependencies.
