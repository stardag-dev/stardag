# Development Guide

## Setup

Install dependencies with uv:

```bash
uv sync --all-extras
```

## Running Tests

```bash
# Run tests directly
uv run pytest

# Run tests via tox (all Python versions)
tox

# Run tests for specific Python version
tox -e py311
```

## Linting & Type Checking

```bash
# Run pre-commit hooks (linting, formatting)
tox -e pre-commit

# Run type checking with pyright
tox -e pyright
```

## Full CI Check

```bash
# Run all checks (pre-commit, tests on all Python versions, pyright)
tox
```
