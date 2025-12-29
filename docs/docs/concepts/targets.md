# Targets

Targets represent where and how task outputs are stored.

## What is a Target?

A target is:

- A **storage location** for task output
- An abstraction over **different backends** (local, S3, in-memory)
- A wrapper providing **serialization** (JSON, pickle, etc.)

## Target Types

### FileSystemTarget

The most common target type - files on a filesystem:

```python
from stardag.target import FileSystemTarget

target = sd.get_target("path/to/output.json", task=self)
```

### LoadableSaveableTarget

Targets with serialization support:

```python
from stardag.target import LoadableSaveableFileSystemTarget

# Automatically handles JSON serialization
target: LoadableSaveableFileSystemTarget[list[int]]
target.save([1, 2, 3])
data = target.load()  # [1, 2, 3]
```

### RemoteFilesystemTarget

For cloud storage like S3:

```python
# Configured via target roots
# s3://bucket/path/to/output.json
```

### InMemoryTarget

For testing:

```python
from stardag.target import InMemoryTarget

target = InMemoryTarget()
target.save(data)
```

## Target Roots

Target roots define the base location for task outputs.

### Configuration

Via environment variables:

```bash
# Single root
export STARDAG_TARGET_ROOT__DEFAULT=/path/to/outputs

# Multiple roots
export STARDAG_TARGET_ROOT__DEFAULT=/local/path
export STARDAG_TARGET_ROOT__S3=s3://bucket/prefix/
```

Or via JSON:

```bash
export STARDAG_TARGET_ROOTS='{"default": "/local/path", "s3": "s3://bucket/"}'
```

### Using Target Roots

Tasks specify relative paths; target roots provide the base:

```python
# Task outputs to: <target_root>/my_task/ab/cd/abcd1234.json
sd.get_target("my_task/ab/cd/abcd1234.json", task=self)
```

### Switching Environments

A common pattern is different roots per environment:

```bash
# Development (local)
export STARDAG_TARGET_ROOT__DEFAULT=~/.stardag/outputs

# Testing (temporary)
export STARDAG_TARGET_ROOT__DEFAULT=/tmp/stardag-test

# Production (S3)
export STARDAG_TARGET_ROOT__DEFAULT=s3://my-bucket/stardag/
```

## Serialization

Targets support different serialization strategies:

### JSON (default)

```python
@sd.task
def get_data() -> list[int]:  # JSON-serializable type
    return [1, 2, 3]
```

### Pickle

For complex Python objects:

```python
from stardag.target.serialize import PickleSerializer

# Used automatically for non-JSON types
@sd.task
def get_model() -> MyModel:
    return trained_model
```

### Custom Serializers

<!-- TODO: Document custom serializer implementation -->

## Target Operations

### Checking Existence

```python
if task.output().exists():
    print("Output already exists")
```

### Complete Check

```python
if task.complete():  # Calls output().exists()
    print("Task is complete")
```

### Loading and Saving

```python
# Save
task.output().save(result)

# Load
data = task.output().load()
```

### Path Access

```python
path = task.output().path
# /home/user/.stardag/outputs/my_task/ab/cd/abcd1234.json
```

## Advanced: TargetFactory

For programmatic control over target creation:

```python
from stardag.target import TargetFactory, target_factory_provider

# Create custom factory
factory = TargetFactory(
    target_roots={
        "default": "/path/to/default",
        "archive": "s3://archive-bucket/",
    }
)

# Set as global provider
target_factory_provider.set(factory)
```

<!-- TODO: Document more TargetFactory options and patterns -->
