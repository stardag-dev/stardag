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

```python
@sd.task
def hello() -> str:
    return "Hello, Stardag!"
```

The `@sd.task` decorator transforms your function into a task class. The return type annotation (`-> str`) tells Stardag how to serialize the output.

### 2. Task Instantiation

```python
task = hello()
```

This creates a task _instance_ - a declarative specification of what to compute. No computation happens yet.

### 3. Building

```python
sd.build(task)
```

`build()` executes the task and saves the output to a deterministic file path.

### 4. Loading Results

```python
task.output().load()
```

Retrieve the persisted result from storage.

## Task with Parameters

Tasks can have parameters:

```python
@sd.task
def greet(name: str) -> str:
    return f"Hello, {name}!"

task = greet(name="World")
sd.build(task)
print(task.output().load())  # "Hello, World!"
```

## Key Concepts

- **Declarative**: Task instances describe _what_ to compute, not _when_
- **Persistent**: Results are saved to disk automatically
- **Deterministic**: Output paths are determined by task parameters

## Inspecting Tasks

Tasks are Pydantic models with useful properties:

```python
task = greet(name="World")

# View the task specification
print(repr(task))
# greet(version=None, name='World')

# Check if already complete
print(task.complete())
# False (before build), True (after build)

# View output path
print(task.output().path)
# /path/to/.stardag/target-roots/default/greet/ab/cd/abcd1234....json
```

## What's Next?

Continue to [Your First DAG](first-dag.md) to learn how to create tasks with dependencies.
