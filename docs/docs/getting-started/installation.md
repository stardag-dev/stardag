# Installation

## Requirements

- Python 3.10 or higher
- pip, uv, or another Python package manager

## Basic Installation

Install the core SDK:

=== "uv"

    ```sh
    uv add stardag
    ```

=== "pip"

    ```sh
    pip install stardag
    ```

## Optional Extras

Stardag provides optional extras for specific integrations:

=== "uv"

    ```sh
    uv add stardag[s3,modal]
    ```

    Enables S3 [Targets](../concepts/targets.md#targets) and [Modal execution](../how-to/integrate-modal.md).

=== "pip"

    ```sh
    pip install stardag[s3,modal]
    ```

    Enables S3 [Targets](../concepts/targets.md#targets) and [Modal](https://modal.com) execution.

## Verify Installation

```python
import stardag as sd

print(sd.__version__)
```

## What's Next?

Continue to [Quick Start](quickstart.md) to create your first task.
