_NOTE this page is WIP. It is not complete and contains inaccuracies, currently excluded from docs site._

# Use the API Registry

Track task builds with the Stardag API service for visibility and collaboration.

## Overview

The API Registry:

- Tracks task execution status
- Provides a web UI for monitoring
- Enables team collaboration
- Coordinates distributed builds

## Prerequisites

1. Access to a Stardag API service (self-hosted or SaaS)
2. Authentication credentials (API key or OAuth)

## Authentication

### Option 1: API Key (Recommended for CI/CD)

```sh
export STARDAG_API_KEY=sk_your_api_key_here
export STARDAG_API_URL=https://api.stardag.com
export STARDAG_WORKSPACE_ID=your-workspace-id
```

Generate API keys from the web UI under Organization Settings > API Keys.

### Option 2: Browser Login (Local Development)

```sh
pip install stardag
stardag auth login
```

This opens a browser for OAuth authentication.

## Basic Usage

```python
import stardag as sd
from stardag.registry import APIRegistry

# Create registry instance
registry = APIRegistry()

# Build with registry tracking
sd.build(my_task, registry=registry)
```

## Configuration

### Via Environment Variables

```sh
export STARDAG_API_URL=https://api.stardag.com
export STARDAG_WORKSPACE_ID=workspace-uuid
export STARDAG_API_KEY=sk_...
```

### Via Profile

```sh
# Set up profile (one-time)
stardag config profile add prod \
    --registry central \
    --organization my-org \
    --workspace production

# Activate profile
export STARDAG_PROFILE=prod
```

### Programmatic

```python
registry = APIRegistry(
    api_url="https://api.stardag.com",
    workspace_id="workspace-uuid",
    api_key="sk_...",
)
```

## Viewing Builds

Access the web UI at `https://app.stardag.com` (or your self-hosted URL).

Features:

- Real-time build progress
- Task dependency visualization
- Historical build records
- Task output inspection

## Target Root Management

Target roots are configured centrally per environment:

```sh
# View current target roots
stardag environment target-roots list

# Set target roots
stardag environment target-roots set default=s3://bucket/outputs

# Add a target root
stardag environment target-roots add s3 s3://my-bucket/data
```

Changes are automatically synced to the local cache. This ensures all team members use consistent storage paths.

## Example: Production Workflow

```python
import stardag as sd
from stardag.registry import APIRegistry
from stardag.config import load_config

# Load configuration from profile/environment
config = load_config()

# Create registry from config
registry = APIRegistry()

# Define your DAG
@sd.task
def fetch_data(source: str) -> dict:
    # ...
    pass

@sd.task
def process(data: sd.Depends[dict]) -> list:
    # ...
    pass

# Build with tracking
task = process(data=fetch_data(source="production"))
sd.build(task, registry=registry)
```

## Loading a task from the registry

Every task the registry has seen is stored as one or more **instances**:
the full body of the task as it was constructed, one per deployment and
build settings it ran under. `from_registry` rebuilds the task object from
one of them. (To keep a stable handle on a task in code, see
[Stable References to Tasks](../concepts/task-references.md).)

```python
import stardag as sd

from my_project.models import TrainedModel  # the class must be imported

# Unpinned: for a completed task, the instance its completing execution
# ran; otherwise the newest instance:
model = TrainedModel.from_registry("2f4c...")

# A specific instance, pinned:
model = TrainedModel.from_registry("2f4c...", instance_id="9a1e...")
```

Both rebuild the body in compat mode and check that it recomputes to the
requested task id, so a significant field the class no longer declares
fails loudly instead of returning a different task. `stardag tasks show
<task-id>` lists a task's instances and their ids.

Pin the instance whenever the exact object matters. Its non-significant
fields are then the same on every load, and since an instance never
changes, its body is cached on disk after the first read, so a pinned load
works offline:

- The cache lives under `~/.stardag/registry-cache/`, one directory per
  registry, workspace and environment, so one profile never reads another
  profile's entries. It holds instance bodies only, never status.
- A cached body is still checked against the task id on every load. An
  entry that is unreadable, or no longer matches, is discarded and read
  again from the registry.
- `stardag cache clear` empties the active profile's partition (`--all`
  for every partition). `STARDAG_REGISTRY_CACHE=0` turns the cache off.

`registry.task_instance_get(instance_id)` reads one instance directly (also
through the cache). Against a registry that predates the instance route,
pass `task_id=` as well and it reads the instance from its task instead.

### Validating a stored body

To rebuild a task from a body you hold yourself, for example one instance's
`body` from the `instances` of `stardag tasks show <task-id> --json`, or a
saved `task.instance_body()`,
use `sd.task_from_body`. It applies the same compat-mode rules and, given
`expected_task_id`, the same identity check:

```python
task = sd.task_from_body(body, expected_task_id="2f4c...")
```

Every task class in the body, nested ones included, must be imported
first; a missing one is reported by namespace and name.

## Offline Support

The SDK caches target roots locally. You can read task outputs without API connectivity, but registering new builds requires connection.

## Troubleshooting

### "Authentication failed"

Check credentials:

```sh
stardag auth status
```

Re-authenticate if needed:

```sh
stardag auth login
```

### "Workspace not found"

Verify workspace ID:

```sh
stardag config show
```

### "Target root mismatch"

Check target roots on the server:

```sh
stardag environment target-roots list
```

## See Also

- [Configuration Guide](../configuration/index.md) - Full configuration reference
- [Platform Overview](../platform/index.md) - API and UI documentation
