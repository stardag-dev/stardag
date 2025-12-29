# API Service

The Stardag API service provides a REST API for task tracking and coordination.

## Overview

The API enables:

- Task registration and status tracking
- Build coordination across workers
- Target root configuration
- Organization and workspace management

## Authentication

### API Keys

For programmatic access (CI/CD, scripts):

```bash
export STARDAG_API_KEY=sk_your_api_key_here
```

Generate keys from the Web UI under Organization Settings > API Keys.

### OAuth/OIDC

For interactive use (CLI, web):

```bash
stardag auth login
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
from stardag.build.api_registry import APIRegistry

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
POST /auth/exchange
```

Exchange refresh token for org-scoped access token.

### Tasks

```
POST /api/v1/tasks
GET /api/v1/tasks/{task_id}
```

Register and retrieve task definitions.

### Builds

```
POST /api/v1/builds
GET /api/v1/builds/{build_id}
PATCH /api/v1/builds/{build_id}
```

Create, retrieve, and update build records.

### Workspaces

```
GET /api/v1/workspaces
GET /api/v1/workspaces/{workspace_id}
GET /api/v1/workspaces/{workspace_id}/target-roots
```

Workspace management and configuration.

<!-- TODO: Add complete API reference with request/response schemas -->

## Rate Limits

<!-- TODO: Document rate limits -->

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
