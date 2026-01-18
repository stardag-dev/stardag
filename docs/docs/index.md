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

## What's Next?

### Getting Started

[Installation :material-download:](getting-started/installation.md){ .md-button }
[Quick Start :material-rocket-launch:](getting-started/quickstart.md){ .md-button }
[Your First DAG :material-graph:](getting-started/first-dag.md){ .md-button }

### Go Deeper

[Core Concepts :material-book-open-variant:](concepts/index.md){ .md-button }
[How-To Guides :material-tools:](how-to/index.md){ .md-button }
[Configuration :material-cog:](configuration/index.md){ .md-button }
[Platform :material-server:](platform/index.md){ .md-button }
