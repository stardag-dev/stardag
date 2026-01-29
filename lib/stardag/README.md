# Stardag

[![PyPI version](https://img.shields.io/pypi/v/stardag.svg)](https://pypi.org/project/stardag/)
[![Python versions](https://img.shields.io/pypi/pyversions/stardag.svg)](https://pypi.org/project/stardag/)
[![Documentation](https://img.shields.io/badge/docs-stardag--dev.github.io-blue)](https://stardag-dev.github.io/stardag/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/stardag-dev/stardag/blob/main/lib/LICENSE)

**Declarative and composable DAGs for Python.**

Stardag provides a clean Python API for representing persistently stored assets, the code that produces them, and their dependencies as a declarative Directed Acyclic Graph (DAG). It is a spiritual—but highly modernized—descendant of [Luigi](https://github.com/spotify/luigi), designed for iterative data and ML workflows.

Built on [Pydantic](https://docs.pydantic.dev/), Stardag uses expressive type annotations to reduce boilerplate and make task I/O contracts explicit—enabling composable tasks and pipelines while maintaining a fully declarative specification of every produced asset.

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

# Load results
assert sum_task.output().load() == 6
assert sum_task.integers.output().load() == [0, 1, 2, 3]
```

## Installation

```bash
pip install stardag
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add stardag
```

**Optional extras:**

```bash
pip install stardag[s3]      # S3 storage support
pip install stardag[prefect] # Prefect integration
pip install stardag[modal]   # Modal integration
```

## Documentation

**[Read the docs](https://stardag-dev.github.io/stardag/)** for tutorials, guides, and API reference.

- [Getting Started](https://stardag-dev.github.io/stardag/getting-started/) — Installation and first steps
- [Core Concepts](https://stardag-dev.github.io/stardag/concepts/) — Tasks, targets, dependencies
- [How-To Guides](https://stardag-dev.github.io/stardag/how-to/) — Integrations with Prefect, Modal
- [Configuration](https://stardag-dev.github.io/stardag/configuration/) — Profiles, CLI reference

## Stardag Cloud

[Stardag Cloud](https://app.stardag.com) provides optional services for team collaboration and monitoring:

- **Web UI** — Dashboard for build monitoring and task inspection
- **API Service** — Task tracking and coordination across distributed builds

The SDK works fully standalone—the platform adds value for teams needing shared visibility and coordination.

## Why Stardag?

- **Composability** — Task instances as first-class parameters enable loose coupling and reusability
- **Declarative** — Full DAG specification before execution; inspect, serialize, and reason about pipelines
- **Deterministic** — Parameter hashing gives each task a unique, reproducible ID and output path
- **Pydantic-native** — Tasks are Pydantic models with full validation and serialization support
- **Framework-agnostic** — Integrate with Prefect, Modal, or run standalone

## Links

- [Documentation](https://stardag-dev.github.io/stardag/)
- [GitHub](https://github.com/stardag-dev/stardag)
- [Stardag Cloud](https://app.stardag.com)
- [Contributing](https://github.com/stardag-dev/stardag/blob/main/CONTRIBUTING.md)
