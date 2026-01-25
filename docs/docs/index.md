# Stardag

!!! info "🚧 **Work in progress** 🚧"

    This documentation is still taking shape. Questions, feedback, and suggestions are very welcome — feel free to [email us](mailto:hello@stardag.dev?subject=Stardag%20docs%20feedback) or [open an issue on GitHub](https://github.com/stardag-dev/stardag/issues/new) if anything is unclear or missing.

## Declarative and composable DAGs

Stardag provides a clean Python API for representing persistently stored assets, the code that produces them, and their dependencies as a declarative Directed Acyclic Graph (DAG). It is a spiritual—but highly modernized—descendant of [Luigi](https://github.com/spotify/luigi), designed for iterative data and ML workflows.

It emphasizes _ease of use_, _composability_, and _compatibility_ with existing data workflow frameworks, rather than locking you into a closed ecosystem.

Stardag is built on top of, and integrates seamlessly with, Pydantic. It uses expressive type annotations to reduce boilerplate and make task I/O contracts explicit. This enables composable tasks and pipelines, while still maintaining a fully declarative specification of every produced asset.

See the [Core Concepts](./concepts/index.md#core-concepts) section for a deeper dive into the architecture, and [Design Philosophy](./concepts/index.md#design-philosophy) for the guiding principles behind the project.

---

## Why Use Stardag?

Stardag’s primary objective is to boost productivity in Data Science, Machine Learning, and AI workflows, where the line between production and development or experimentation is often blurry.

It provides lightweight tools to structure data processing, make dependencies explicit, and maintain a clear overview of how assets are produced. Crucially, it brings many of the benefits of _Data-as-Code_ (DaC) to everyday workflows: managing complexity, improving reproducibility, and reducing boilerplate, without sacrificing flexibility or developer ergonomics.

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
