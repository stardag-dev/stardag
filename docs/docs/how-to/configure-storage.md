_NOTE this page is WIP. It is not complete and contains inaccuracies, currently excluded from docs site._

# Configure Storage

Set up target roots for different environments and storage backends.

## Basic Configuration

### Environment Variables

Set target roots via environment variables:

```bash
# Default target root (required)
export STARDAG_TARGET_ROOT__DEFAULT=/path/to/outputs

# Additional named roots
export STARDAG_TARGET_ROOT__ARCHIVE=/path/to/archive
export STARDAG_TARGET_ROOT__TEMP=/tmp/stardag
```

### JSON Format

For complex configurations:

```bash
export STARDAG_TARGET_ROOTS='{
    "default": "/local/outputs",
    "archive": "s3://my-bucket/archive/",
    "temp": "/tmp/stardag"
}'
```

## Environment-Specific Setup

### Local Development

```bash
# ~/.bashrc or ~/.zshrc
export STARDAG_TARGET_ROOT__DEFAULT=~/.stardag/outputs
```

### Testing

Use temporary directories:

```python
# conftest.py
import pytest
import tempfile
import os

@pytest.fixture(autouse=True)
def isolated_targets(tmp_path):
    """Use isolated target root for each test."""
    os.environ["STARDAG_TARGET_ROOT__DEFAULT"] = str(tmp_path)
    yield
```

### Production (AWS S3)

```bash
export STARDAG_TARGET_ROOT__DEFAULT=s3://my-bucket/stardag/prod/
```

Ensure AWS credentials are configured:

```bash
# Via environment
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

# Or via AWS profile
export AWS_PROFILE=production
```

## Using Multiple Roots

Define multiple roots for different purposes:

```bash
export STARDAG_TARGET_ROOT__DEFAULT=s3://bucket/current/
export STARDAG_TARGET_ROOT__ARCHIVE=s3://bucket/archive/
export STARDAG_TARGET_ROOT__LOCAL=/local/cache/
```

Select a root in your task:

```python
from stardag.utils.testing import target_roots_override

with target_roots_override(
    {"default": "/default", "archive": "/archive"}
):
    # Default root
    sd.get_target("path/file.json")

    # Specific root
    sd.get_target("path/file.json", target_root_key="archive")
```

<!-- TODO: Verify root selection syntax -->

## Programmatic Configuration

### Using TargetFactory

```python
from stardag.target import TargetFactory, target_factory_provider

factory = TargetFactory(
    target_roots={
        "default": "/path/to/default",
        "s3": "s3://bucket/prefix/",
    }
)

target_factory_provider.set(factory)
```

### Context Managers

For temporary configuration:

```{.python notest}
from stardag.target import target_factory_provider, TargetFactory

with target_factory_provider.override(
    TargetFactory(target_roots={"default": "/tmp/test"})
):
    # Tasks use temporary root
    sd.build(task)
```

<!-- TODO: Verify context manager syntax -->

## S3 Configuration

### Requirements

Install S3 support:

```bash
pip install stardag[s3]
```

### Configuration

```bash
export STARDAG_TARGET_ROOT__DEFAULT=s3://my-bucket/stardag/

# AWS credentials (one of):
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

# Or use AWS profile
export AWS_PROFILE=my-profile

# Or use IAM role (on EC2/ECS)
```

### S3 Path Format

```
s3://<bucket>/<prefix>/
```

Task outputs become:

```
s3://my-bucket/stardag/my_task/ab/cd/abcd1234.json
```

## Best Practices

1. **Separate environments** - Different roots for dev/test/prod
2. **Use S3 for production** - Shared, persistent storage
3. **Use local for development** - Fast iteration
4. **Use temp for testing** - Isolation between tests
5. **Document your setup** - Include in project README

## Troubleshooting

### "Target root not configured"

Ensure `STARDAG_TARGET_ROOT__DEFAULT` is set:

```bash
echo $STARDAG_TARGET_ROOT__DEFAULT
```

### "Permission denied"

Check directory permissions or S3 bucket policy.

### Outputs in wrong location

Verify the active configuration:

```python
from stardag.target import target_factory_provider

factory = target_factory_provider.get()
print(factory.target_roots)
```
