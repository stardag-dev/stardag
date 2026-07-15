# Registry API, UI, CLI & Configuration

## Overview

The Stardag platform consists of:

- **Registry API** (`stardag-api`): FastAPI backend for task tracking, builds, locks
- **Registry UI** (`stardag-ui`): React frontend for monitoring and exploration
- **CLI** (`stardag` command): Authentication, configuration, environment management

The SDK integrates with the API optionally — tasks work standalone without it.

## Registry API

### Key Endpoints

| Endpoint                                                     | Method          | Purpose                              |
| ------------------------------------------------------------ | --------------- | ------------------------------------ |
| `/api/v1/builds`                                             | POST            | Create a new build                   |
| `/api/v1/builds`                                             | GET             | List builds with pagination          |
| `/api/v1/builds/{id}`                                        | GET             | Get build details                    |
| `/api/v1/builds/{id}/tasks`                                  | POST            | Register task in build               |
| `/api/v1/tasks`                                              | GET             | List tasks (workspace-scoped)        |
| `/api/v1/tasks/{id}`                                         | GET             | Get task details                     |
| `/api/v1/tasks/graph`                                        | POST            | Get DAG graph (upstream/downstream)  |
| `/api/v1/tasks/{id}/artifacts`                               | GET             | List task artifacts                  |
| `/api/v1/tasks/{id}/events`                                  | GET             | Task lifecycle events                |
| `/api/v1/tasks/{id}/metadata`                                | GET             | Task metadata (for AliasTask)        |
| `/api/v1/tasks/search`                                       | POST            | Full-text search with filters        |
| `/api/v1/locks`                                              | POST/PUT/DELETE | Distributed lock management          |
| `/api/v1/builds/{id}/frontier`                               | GET             | Scheduling frontier (reactive ticks) |
| `/api/v1/builds/{id}/notify`                                 | POST/DELETE     | Scheduler wake-up flag (reactive)    |
| `/api/v1/concurrency-limits`                                 | GET/PUT/DELETE  | Named env concurrency limits         |
| `/api/v1/concurrency-limits/{key}/holders[/{task_id}/evict]` | GET/POST        | Limit slot holders + admin evict     |
| `/api/v1/auth/exchange`                                      | POST            | OIDC token exchange                  |

### Authentication Methods

1. **API Key** (SDK): Set `STARDAG_API_KEY=sk_...` or `X-API-Key` header
2. **Browser Login** (CLI): `stardag auth login` → stores JWT in credentials
3. **OIDC** (UI): Keycloak-based login → token exchange → internal JWT

### SDK Integration

The SDK communicates with the API via `APIRegistry`:

```python
import stardag as sd

# Option 1: Environment variables
# STARDAG_API_URL=https://api.stardag.com
# STARDAG_API_KEY=sk_...
# STARDAG_WORKSPACE_ID=...

# Option 2: CLI login + config
# $ stardag auth login
# $ stardag config registry add prod --url https://api.stardag.com

# Then build normally — registry tracking is automatic
sd.build(task)
```

When a registry is configured, `sd.build()` automatically:

1. Creates a build record (`POST /builds`)
2. Registers each task (`POST /builds/{id}/tasks`) — edges are reconciled on every registration, and phantom task records are created for unregistered upstream dependencies
3. Reports task start/complete/fail events — each event includes the git commit hash in metadata for traceability
4. Marks build as complete/failed

### NoOpRegistry (Default)

Without configuration, the SDK uses `NoOpRegistry` — all registry calls are silently skipped.
Tasks still execute and persist to local targets normally.

### Registry Abstract Base

```python
from stardag.registry import RegistryABC

class RegistryABC:
    async def build_start_aio(root_tasks, description) -> UUID
    async def build_complete_aio(build_id) -> None
    async def build_fail_aio(build_id, error_msg) -> None
    async def task_register_aio(build_id, task) -> None
    async def task_start_aio(build_id, task) -> None
    async def task_complete_aio(build_id, task) -> None
    async def task_fail_aio(build_id, task, error) -> None
    async def task_get_metadata_aio(task_id) -> TaskMetadata
```

## Registry UI

The React frontend at `app.stardag.com` provides:

- **Builds List**: Paginated view of all builds with status (running/completed/failed)
- **Build View**: Detailed view with interactive DAG visualization (Dagre + XYFlow)
- **Task Explorer**: Search and browse tasks with advanced filtering (refactored into `TaskExplorerSearch` + `TaskExplorerTable` sub-components)
- **DAG Graph**: Interactive visualization with configurable upstream/downstream depth, batch/group nodes for collapsed same-type dependencies, depth-based opacity fading, and breadcrumb navigation
- **Task Detail**: Shows commit hash from status-determining event; Event Log table shows per-event commit hash
- **Phantom Nodes**: Dashed border styling for unregistered/phantom task nodes in the DAG
- **Artifact Viewer**: Display task artifacts (markdown with syntax highlighting, JSON)
- **Workspace Management**: Create/manage workspaces, invite members, manage API keys
- **Search**: Full-text search with filter syntax (`key:op:value`)

### Filter Syntax

The search UI supports operators: `=`, `!=`, `>`, `<`, `>=`, `<=`, `~` (contains)

```
namespace:=:my_app.pipeline
name:~:metrics
status:=:completed
```

## CLI Commands

### Authentication

```bash
stardag auth login              # Browser-based OIDC login
stardag auth login --api-url URL  # Login to specific API
stardag auth logout             # Remove stored credentials
stardag auth status             # Show current auth state
stardag auth refresh            # Refresh expired tokens
```

### Configuration

```bash
stardag config show                          # Display current config
stardag config registry add NAME --url URL   # Add API backend
stardag config registry list                 # List registries
stardag config registry remove NAME          # Remove registry
stardag config profile add/list/use/remove   # Manage profiles
stardag config list workspaces               # List available workspaces
stardag config list environments             # List environments
```

### Environment Management

```bash
stardag environment list                     # List environments
stardag environment create NAME              # Create environment
stardag environment target-roots add KEY URI # Add target root
stardag environment target-roots list        # List target roots
```

### Concurrency Limits

```bash
stardag concurrency-limits list              # List named limits (--holders adds counts)
stardag concurrency-limits set KEY N         # Upsert a limit (max_concurrent = N)
stardag concurrency-limits delete KEY        # Remove a limit (--yes to skip confirm)
stardag concurrency-limits holders KEY       # List RUNNING slot holders
stardag concurrency-limits evict KEY TASK_ID # Free a leaked slot (--yes to skip confirm)
```

All accept `-p/--stardag-profile` and `-e/--stardag-env` to target a
non-active profile / environment.

### Modal Integration

```bash
stardag modal deploy APP_REF                # Deploy to Modal.com
stardag modal stardag-api-key create        # Create API key for Modal
```

## Configuration System

### Config Sources (Priority Order)

1. **Environment variables** (`STARDAG_*`)
2. **Project config** (`.stardag/config.toml` in cwd or parents)
3. **User config** (`~/.stardag/config.toml`)
4. **Defaults**

### Key Environment Variables

| Variable                 | Purpose                   |
| ------------------------ | ------------------------- |
| `STARDAG_PROFILE`        | Active profile name       |
| `STARDAG_API_URL`        | Registry API URL          |
| `STARDAG_WORKSPACE_ID`   | Workspace UUID            |
| `STARDAG_ENVIRONMENT_ID` | Environment UUID          |
| `STARDAG_API_KEY`        | API key (`sk_...`)        |
| `STARDAG_TARGET_ROOTS`   | JSON dict of target roots |

### Config File Example

`~/.stardag/config.toml`:

```toml
[profiles.default]
registry = "production"
workspace_id = "abc123..."
environment_id = "def456..."

[registries.production]
url = "https://api.stardag.com"

[profiles.default.target_roots]
default = "~/.stardag/local-target-roots/default/default"
```

### File Locations

| Path                             | Purpose                      |
| -------------------------------- | ---------------------------- |
| `~/.stardag/config.toml`         | User configuration           |
| `~/.stardag/credentials/`        | Stored auth credentials      |
| `~/.stardag/access-token-cache/` | Token cache                  |
| `~/.stardag/local-target-roots/` | Default local target storage |
| `.stardag/config.toml`           | Project-level configuration  |

## Local Development Setup

### Docker Compose

The full platform runs locally via docker-compose:

```bash
cd stardag/
docker-compose up -d --build
```

Services:

- **db**: PostgreSQL 16 (port 5432)
- **keycloak**: OIDC identity provider (port 8080)
- **migrations**: Alembic migration runner
- **seed**: Database seeder
- **api**: FastAPI backend (port 8000)
- **ui**: React frontend (port 3000)

### Database Roles

- `stardag_admin` / `stardag_admin`: Migration role (DDL permissions)
- `stardag_service` / `stardag_service`: Application role (DML only)
- `stardag` / `stardag`: Superuser (Docker init)

### Test Credentials

- Keycloak admin: `admin:admin`
- Test user: `testuser@localhost` / `testpassword`

## Distributed Locks

For coordinating concurrent builds across machines:

```python
from stardag.build import GlobalLockConfig

# Via build parameter
sd.build(task, global_lock_config=GlobalLockConfig(enabled=True))
```

Lock lifecycle via API:

- `POST /api/v1/locks` → Acquire lock
- `PUT /api/v1/locks/{id}` → Renew (heartbeat)
- `DELETE /api/v1/locks/{id}` → Release

Prevents duplicate task execution when multiple workers run the same DAG.

## API Technology Stack

- **Framework**: FastAPI 0.115+
- **ORM**: SQLAlchemy 2.0+ (async)
- **Database**: PostgreSQL + asyncpg
- **Migrations**: Alembic (always use `--autogenerate`)
- **Validation**: Pydantic
- **Auth**: JWT + OIDC (Keycloak)
- **HTTP Client**: httpx

## Further Reading

For the latest platform documentation, visit [docs.stardag.com/platform](https://docs.stardag.com/platform/).
