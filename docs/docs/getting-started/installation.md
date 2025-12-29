# Installation

## Requirements

- Python 3.10 or higher
- pip, uv, or another Python package manager

## Basic Installation

Install the core SDK:

=== "pip"

    ```bash
    pip install stardag
    ```

=== "uv"

    ```bash
    uv add stardag
    ```

## Optional Extras

Stardag provides optional extras for specific integrations:

=== "CLI tools"

    ```bash
    pip install stardag[cli]
    ```

    Includes authentication and configuration CLI commands.

=== "Prefect integration"

    ```bash
    pip install stardag[prefect]
    ```

    Enables Prefect-based orchestration.

=== "AWS S3 targets"

    ```bash
    pip install stardag[s3]
    ```

    Enables S3 storage for task outputs.

=== "All extras"

    ```bash
    pip install stardag[cli,prefect,s3]
    ```

## Verify Installation

```python
import stardag as sd

print(sd.__version__)
```

## What's Next?

Continue to [Quick Start](quickstart.md) to create your first task.
