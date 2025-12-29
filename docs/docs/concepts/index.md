# Core Concepts

Understanding these concepts will help you get the most out of Stardag.

## Overview

Stardag is built around a few key abstractions:

| Concept                                       | Description                              |
| --------------------------------------------- | ---------------------------------------- |
| **[Tasks](tasks.md)**                         | Units of work that produce outputs       |
| **[Dependencies](dependencies.md)**           | How tasks depend on each other's outputs |
| **[Parameter Hashing](parameter-hashing.md)** | Deterministic task identification        |
| **[Targets](targets.md)**                     | Where and how outputs are stored         |
| **[Build & Execution](build-execution.md)**   | How DAGs are executed                    |

## The Big Picture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Task A    │────▶│   Task B    │────▶│   Task C    │
│ (upstream)  │     │ (middle)    │     │ (downstream)│
└─────────────┘     └─────────────┘     └─────────────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Target A   │     │  Target B   │     │  Target C   │
│ (persisted) │     │ (persisted) │     │ (persisted) │
└─────────────┘     └─────────────┘     └─────────────┘
```

1. **Tasks** define what to compute and how their output depends on inputs
2. **Dependencies** create the DAG structure by linking task outputs to inputs
3. **Parameter hashing** gives each unique task configuration a deterministic ID
4. **Targets** persist outputs at paths determined by the task ID
5. **Build** traverses the DAG bottom-up, executing only incomplete tasks

## Design Philosophy

### Declarative Over Imperative

Tasks are _specifications_ of what to compute, not instructions to execute. This separation enables:

- Inspection before execution
- Serialization of the full DAG
- Intelligent caching and skip logic

### Composition Over Inheritance

Tasks are composed by passing task instances as parameters, not by inheriting from parent tasks. This promotes:

- Loose coupling
- Reusability
- Testability

### Determinism

Given the same parameters, a task always:

- Has the same ID (via parameter hashing)
- Writes to the same output location
- Produces the same result (assuming pure functions)

## Mental Model

Think of Stardag like a Makefile for Python:

- Each task is like a Make target
- Dependencies define the build order
- If an output file exists, the task is considered complete
- Building starts from the requested target and works backward

The key difference: parameter hashing makes targets automatically unique based on their inputs.
