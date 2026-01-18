# Stardag

**Declarative and composable DAGs.**

Stardag provides a clean Python API for representing persistently stored assets, the code that produces them, and their dependencies as a declarative Directed Acyclic Graph (DAG). As such, it is a spritual - but highly modernized - descendant of [Luigi](https://github.com/spotify/luigi).

It emphasizes _ease of use_, _composability_, and _compatibility_ with other data workflow frameworks.

Stardag is built on top of, any integrates well with, Pydantic and utilizes expressive type annotations to reduce boilerplate and clarify io-contracts of tasks.

See the [Core Concepts](./concepts/index.md#core-concepts) section for further details on its architecture and [Design Philosophy](./concepts/index.md#design-philosophy).

---

## Why Stardag?

Stardag primary objectice is to boost productivity in Data Science/Machine Learning/AI workflows where the line between production" and "development/experimentation" is often blury. It gives you light-weight tools to structure and get overview of your data processing. It, cruically, provides many of the benfits of "Data-as-Code" (DaC) and help manage complexity and reduce bolilerplate.

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
sum_task = get_sum(integers=get_range(limit=4))

# Materialize all tasks' targets
sd.build(sum_task)

# Load the result
assert sum_task.output().load() == 6
# inspect intermediate results
assert sum_task.integers.output().load() == [0, 1, 2, 3]
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
