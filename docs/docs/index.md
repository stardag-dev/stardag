# Stardag

**Declarative and composable DAG framework for Python with persistent asset management.**

Stardag provides a clean Python API for representing persistently stored assets - the code that produces them and their dependencies - as a declarative Directed Acyclic Graph (DAG). It emphasizes ease of use, composability, and type safety.

---

## Why Stardag?

<div class="grid cards" markdown>

- :material-puzzle: **Composable**

  ***

  Task instances as first-class parameters. Build complex DAGs by composing simple, reusable tasks.

- :material-check-decagram: **Type Safe**

  ***

  Built on Pydantic with full serialization support. Expressive type annotations reduce boilerplate and clarify IO contracts.

- :material-arrow-up-bold: **Bottom-Up Execution**

  ***

  Makefile-style "build only what's needed". Skip completed tasks automatically based on deterministic output paths.

- :material-file-tree: **Deterministic Paths**

  ***

  Output locations determined by parameter hashes before execution. Reproducible builds across environments.

</div>

## Quick Example

```python
import stardag as sd

@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)

# Declarative DAG specification - no computation yet
task = get_sum(integers=get_range(limit=10))

# Materialize task targets
sd.build(task)

# Load the result
print(task.output().load())  # 45
```

## The Stardag Offering

Stardag consists of three components:

| Component    | Description                                                      |
| ------------ | ---------------------------------------------------------------- |
| **SDK**      | Python library for defining and building DAGs                    |
| **CLI**      | Command-line tools for authentication and configuration          |
| **Platform** | Optional API service and Web UI for monitoring and collaboration |

## Getting Started

<div class="grid cards" markdown>

- :material-download: **Installation**

  ***

  Install Stardag and get your environment ready.

  [:octicons-arrow-right-24: Install](getting-started/installation.md)

- :material-rocket-launch: **Quick Start**

  ***

  Build your first task in 5 minutes.

  [:octicons-arrow-right-24: Quick Start](getting-started/quickstart.md)

- :material-graph: **Your First DAG**

  ***

  Create a complete pipeline with dependencies.

  [:octicons-arrow-right-24: Build a DAG](getting-started/first-dag.md)

</div>

## Why Not Just Use...?

### Luigi

Luigi is powerful in its simplicity but outdated. Stardag addresses Luigi's lack of composability - promoting tightly coupled DAGs and "parameter explosion". See [Composability](concepts/dependencies.md#composability) for details.

### Modern Frameworks (Prefect, Dagster)

Modern frameworks prioritize flexibility ("just annotate any Python function as a task") but often lack clean declarative DAG abstractions. Stardag fills this gap while integrating with these tools for orchestration.

<!-- TODO: Add more detailed comparison with concrete examples -->

---

<div class="grid" markdown>

[:material-book-open-variant: **Concepts**](concepts/index.md){ .md-button }
Learn core concepts

[:material-tools: **How-To Guides**](how-to/index.md){ .md-button }
Solve specific problems

[:material-cog: **Configuration**](configuration/index.md){ .md-button }
Configure your environment

[:material-server: **Platform**](platform/index.md){ .md-button }
API & Web UI

</div>
