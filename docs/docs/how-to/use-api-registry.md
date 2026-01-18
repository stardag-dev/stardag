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

```bash
export STARDAG_API_KEY=sk_your_api_key_here
export STARDAG_REGISTRY_URL=https://api.stardag.com
export STARDAG_WORKSPACE_ID=your-workspace-id
```

Generate API keys from the web UI under Organization Settings > API Keys.

### Option 2: Browser Login (Local Development)

```bash
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

```bash
export STARDAG_REGISTRY_URL=https://api.stardag.com
export STARDAG_WORKSPACE_ID=workspace-uuid
export STARDAG_API_KEY=sk_...
```

### Via Profile

```bash
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

## Target Root Synchronization

Target roots are configured centrally per workspace:

```bash
# Sync target roots from server
stardag config target-roots sync

# View current configuration
stardag config target-roots list
```

This ensures all team members use consistent storage paths.

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

## Offline Support

The SDK caches target roots locally. You can read task outputs without API connectivity, but registering new builds requires connection.

## Troubleshooting

### "Authentication failed"

Check credentials:

```bash
stardag auth status
```

Re-authenticate if needed:

```bash
stardag auth login
```

### "Workspace not found"

Verify workspace ID:

```bash
stardag config show
```

### "Target root mismatch"

Sync from server:

```bash
stardag config target-roots sync
```

## See Also

- [Configuration Guide](../configuration/index.md) - Full configuration reference
- [Platform Overview](../platform/index.md) - API and UI documentation
