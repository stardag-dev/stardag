# Core Concepts

Understanding these concepts will help you get the most out of Stardag.

!!! info "🚧 **Work in progress** 🚧"

    This section is still taking shape. Questions, feedback, and suggestions are very welcome — feel free to [email us](mailto:hello@stardag.dev?subject=Stardag%20docs%20feedback) or [open an issue on GitHub](https://github.com/stardag-dev/stardag/issues/new) if anything is unclear.

## Overview

Stardag is built around a few key abstractions:

| Concept                                     | Description                                                 |
| ------------------------------------------- | ----------------------------------------------------------- |
| **[Tasks](tasks.md)**                       | Units of work that produce outputs and declare dependencies |
| **[Targets](targets.md)**                   | Where and how outputs are stored                            |
| **[Build & Execution](build-execution.md)** | How DAGs are executed                                       |

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

Tasks are _specifications_ of what to compute, not (only) instructions to execute. This separation enables:

- Inspection before execution
- Serialization of the full DAG
- Efficinet caching and skip logic
- Data as Code (DaC)

Moreover, especially in experimental Machine Learning workflows, it can be extremely valuable with a human readable and searchable specification of any asset produced. Each task is a self-contained specification of the compelete provenance of its persistently stored target. Done right this allows inspection of the "diff" between the specification of, say, two different instances of ML-model performance metrics; Why is one better than the other? Which hyper-parameters have changed? Is the same training dataset and filtering used?

### Composition (Over Inheritance and/or Static DAG Topology)

Tasks are composed by passing task instances as parameters. This promotes:

- Loose coupling
- Reusability
- Testability

### Determinism

Given the same parameters, a task always:

- Has the same ID (via parameter hashing)
- Writes to the same output location
- Produces the same result (assuming pure functions)

### The Right Tool for the Job

Stardag happily aknowledges that the declarative DAG abstraction is _not_ suitable for all data processing/workflows. That's why its ambition is to be interoperable with other modern data workflow frameworks, such as [Prefect](https://www.prefect.io/), that lacks the declarative DAG abstraction (both as an SDK and at the execution layer).

## Mental Model

Think of Stardag like a Makefile for Python:

- Each task is like a Make target
- Dependencies define the build order
- If an output file exists, the task is considered complete
- Building starts from the requested target and works backward

The key difference: parameter hashing makes targets automatically unique based on their inputs.
