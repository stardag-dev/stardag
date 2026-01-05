# Stardag

Declarative and composable DAG framework for Python with persistent asset management.

## Project Overview

Stardag is a Python framework for building DAGs (Directed Acyclic Graphs) with:

- **Composability**: Task instances as first-class composable units
- **Type safety**: Pydantic-based tasks with full serialization
- **Bottom-up execution**: Build only what's needed (Makefile-style)
- **Deterministic paths**: Output locations based on parameter hashes

## Tech Stack

- Python 3.10+
- Pydantic for task models and validation
- Optional integrations: Prefect, Modal, AWS S3

## Project Structure

```
lib/
├── stardag/                    # Core SDK library
│   └── src/stardag/
│       ├── _base.py            # Core Task class
│       ├── _decorator.py       # @task decorator API
│       ├── _auto_task.py       # AutoTask with filesystem targets
│       ├── _task_parameter.py  # Depends, TaskLoads, TaskSet
│       ├── build/              # Execution/build logic
│       ├── target/             # Target abstraction (local, S3)
│       └── integration/        # Prefect, Modal, AWS integrations
└── stardag-examples/           # Example DAGs and demos

app/
├── stardag-api/                # FastAPI backend for task tracking
└── stardag-ui/                 # React frontend for monitoring
```

## Development

See [DEV_README.md](/DEV_README.md) for setup and commands.

## Testing

- **Unit tests**: Prefer unit tests for testing specific components and logic
- **E2E test**: `./scripts/e2e-test.sh` runs a full integration test (docker-compose, API, UI, demo script). Use sparingly - it's slow and should not replace unit tests. Good for verifying the full stack works together after significant changes.

## Code Style

- Use type annotations throughout
- Follow existing Pydantic patterns for task definitions

## Learnings

This section captures project-specific corrections and preferences. When the user corrects how something should be done or you discover something new about this project, add it here.

**Format for new learnings:**

- **Do**: [What should be done]
- **Don't**: [What was incorrectly assumed or should be avoided]
- **Context**: [Why, if relevant]

### Learnings Log

1. **Run pre-commit hooks after editing files**
   - **Do**: Run relevant pre-commit hooks after editing files (e.g., `prettier` for markdown, `ruff` and `pyright` for Python)
   - **Don't**: Leave files in a non-compliant state
   - **Context**: Project uses prettier for markdown formatting, ruff and pyright for Python linting/formatting and typechecks
