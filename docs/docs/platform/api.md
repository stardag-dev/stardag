# API Service

The Stardag API service provides a REST API for task tracking and coordination.

## Overview

The API enables:

- Task registration and status tracking
- Build coordination across workers
- Target root configuration
- Workspace and environment management

## Authentication

### API Keys

For programmatic access (CI/CD, scripts):

```bash
export STARDAG_API_KEY=sk_your_api_key_here
```

Generate keys from the Web UI under Workspace Settings > API Keys.

### OAuth/OIDC

For interactive use (CLI, web):

=== "Activated venv"

    ```bash
    stardag auth login
    ```

=== "uv run ..."

    ```bash
    uv run stardag auth login
    ```

Uses browser-based OAuth flow.

## Base URL

| Environment | URL                       |
| ----------- | ------------------------- |
| SaaS        | `https://api.stardag.com` |
| Local dev   | `http://localhost:8000`   |
| Self-hosted | Your configured domain    |

## SDK Integration

The SDK handles API communication automatically:

```python
from stardag.registry import APIRegistry

registry = APIRegistry()
sd.build(task, registry=registry)
```

## API Endpoints

### Health Check

```
GET /health
```

Returns API status.

### Authentication

```
GET  /.well-known/jwks.json     # JWKS for token verification
GET  /api/v1/auth/config        # Auth configuration
POST /api/v1/auth/exchange      # Exchange refresh token for workspace-scoped access token
```

### User

```
GET /api/v1/me                  # Current user profile with workspaces
GET /api/v1/me/invites          # Pending workspace invites
```

### Workspaces

```
POST   /api/v1/workspaces                           # Create workspace
GET    /api/v1/workspaces/{workspace_id}            # Get workspace details
PATCH  /api/v1/workspaces/{workspace_id}            # Update workspace
DELETE /api/v1/workspaces/{workspace_id}            # Delete workspace
GET    /api/v1/workspaces/{workspace_id}/members    # List members
```

### Environments

```
GET    /api/v1/workspaces/{workspace_id}/environments                    # List environments
POST   /api/v1/workspaces/{workspace_id}/environments                    # Create environment
GET    /api/v1/workspaces/{workspace_id}/environments/{environment_id}   # Get environment
PATCH  /api/v1/workspaces/{workspace_id}/environments/{environment_id}   # Update environment
DELETE /api/v1/workspaces/{workspace_id}/environments/{environment_id}   # Delete environment
```

### Builds

```
POST  /api/v1/builds                          # Create build
GET   /api/v1/builds                          # List builds
GET   /api/v1/builds/{build_id}               # Get build
POST  /api/v1/builds/{build_id}/complete      # Mark build complete
POST  /api/v1/builds/{build_id}/fail          # Mark build failed
POST  /api/v1/builds/{build_id}/cancel        # Cancel build
GET   /api/v1/builds/{build_id}/tasks         # List tasks in build
GET   /api/v1/builds/{build_id}/graph         # Get task dependency graph
POST  /api/v1/builds/{build_id}/tasks         # Register task in build
```

### Tasks

```
GET /api/v1/tasks                    # List/search tasks
GET /api/v1/tasks/{task_id}          # Get task details
GET /api/v1/tasks/{task_id}/assets   # Get task assets
GET /api/v1/tasks/{task_id}/events   # Get task events
```

### Task Search

```
GET /api/v1/tasks/search             # Search tasks with filters
GET /api/v1/tasks/search/keys        # Get available search keys
GET /api/v1/tasks/search/values      # Get values for a search key
GET /api/v1/tasks/search/columns     # Get available columns
```

### Target Roots

```
GET /api/v1/target-roots             # Get target roots for current environment
```

### Locks

```
POST /api/v1/locks/{lock_name}/acquire   # Acquire lock
POST /api/v1/locks/{lock_name}/renew     # Renew lock
POST /api/v1/locks/{lock_name}/release   # Release lock
GET  /api/v1/locks                       # List locks
GET  /api/v1/locks/{lock_name}           # Get lock status
```

## Error Handling

API errors return JSON with:

```json
{
  "detail": "Error description"
}
```

Common status codes:

| Code | Meaning                           |
| ---- | --------------------------------- |
| 401  | Invalid or expired authentication |
| 403  | Insufficient permissions          |
| 404  | Resource not found                |
| 422  | Validation error                  |

## See Also

- [Using the API Registry](../how-to/use-api-registry.md) - SDK integration guide
- [Self-Hosting](self-hosting.md) - Run your own API server
