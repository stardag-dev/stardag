# Registry API, UI, CLI & Configuration

## Overview

The Stardag platform consists of:

- **Registry API** (`stardag-api`, server `0.6`): FastAPI backend — the ledger of tasks,
  task instances, plans, deployments, settings and executions
- **Registry UI** (`stardag-ui`): React frontend for monitoring and exploration
- **CLI** (`stardag` command): builds, inspection, auth, configuration, Modal, self-hosting

The SDK integrates with the API optionally — tasks work standalone without it. The SDK and the
server upgrade **together**: there is no compatibility between v1 and v2 in either direction.

## The Registry as a Ledger

| Entity            | What it is                                                                                        |
| ----------------- | ------------------------------------------------------------------------------------------------- |
| **task**          | One row per task id: global status, the live claim, the current execution. No parameters.         |
| **task instance** | A construction of a task under a scope `(deployment, settings)`: full parameter body + its edges. |
| **plan**          | One build's request under one scope: its members (instances) and the edges between them.          |
| **deployment**    | One `stardag modal deploy`, or one local code id. With **settings**, the scope.                   |
| **execution**     | The ledger of attempts: one row per claim granted, with how it started and ended.                 |

What a user of the SDK needs from this:

- **Task state is global to the environment.** The same task id in two builds is one row with
  one status, so a build never re-runs what another completed.
- **A build is a request, not an owner.** The execution claim (RUNNING + a finite expiry) is
  the only coordination: at most one execution per task at a time, a lapsed claim is taken
  over, and cancelling build B releases only B's claims.
- **Dependency edges belong to the instance, under its scope** — not to the task id. Within
  a scope edges only grow; a build acts on everything in its plan.
- **A plan** is created holding its roots, filled by discovery in chunks, then **sealed**.
  Exactly one plan per build is active; a rollover (new deployment) or a re-trigger under
  new settings creates a new plan and supersedes the old one, whose running executions finish
  on their own (see `builds stop --not-in-current-plan`).
- **The only way out of COMPLETED** is a build observing the target missing. To re-run a
  task, delete its target and build; there is no "mark incomplete" route.
- **Excluding** a member (`stardag tasks exclude`) gives up on it and its downstream within
  one plan; an excluded root fails the build.

## Registry API

Registry routes live under **`/api/v2`**; auth, UI, workspace and target-root routes stay under
`/api/v1`. The full list is `docs/docs/platform/api.md`.

| Endpoint                                                     | Method         | Purpose                                                              |
| ------------------------------------------------------------ | -------------- | -------------------------------------------------------------------- |
| `/api/v2/version`                                            | GET            | Server and API version (no auth)                                     |
| `/api/v2/deployments`, `/deployments/{id}/activate`          | GET/POST       | Record, activate and list deployments                                |
| `/api/v2/settings/{settings_hash}`                           | GET            | A stored settings body                                               |
| `/api/v2/builds`, `/builds/{id}`                             | GET/POST       | Create, list, read builds                                            |
| `/api/v2/builds/{id}/{complete,fail,cancel,resume}`          | POST           | Build lifecycle (cancel releases every plan's claims)                |
| `/api/v2/builds/{id}/frontier`                               | GET            | Runnable / discovery-job / running members                           |
| `/api/v2/builds/{id}/executions`                             | GET            | The build's unended (or all) executions                              |
| `/api/v2/builds/{id}/plans`, `/plans/{id}[/graph]`           | GET            | Plans, their members and instance edges                              |
| `/api/v2/plans/{id}/members[/{task_id}/...]`                 | POST           | Register, seal, start (claim), complete, fail, yield, retry, exclude |
| `/api/v2/tasks`, `/tasks/{id}[/executions,events,artifacts]` | GET            | Tasks and their history                                              |
| `/api/v2/executions/{id}/stopped`                            | POST           | Record a stop (`stopped` / `lost`)                                   |
| `/api/v2/concurrency-limits[/{key}]`                         | GET/PUT/DELETE | Named environment-wide limits                                        |
| `/api/v1/auth/exchange`                                      | POST           | OIDC token exchange                                                  |

Refusals carry a code (`instance_conflict`, `plan_superseded`, `reserved_settings_key`,
`concurrency_limit_reached`, ...). A v2 SDK against a v1 server fails on its first call with a
404 (`NotFoundError`): the routes do not exist there.

### Authentication Methods

The server runs in one of two auth modes (`AUTH_MODE=oidc|local`, served to
clients at runtime via `GET /api/v1/auth/config`):

1. **API Key** (SDK): Set `STARDAG_API_KEY=sk_...` or `X-API-Key` header (both modes)
2. **Browser Login** (CLI): `stardag auth login` → OIDC PKCE flow (oidc mode)
   or email/password prompt (local mode) → stores credentials
3. **OIDC** (UI, oidc mode): external IdP login → token exchange → internal JWT
4. **Local** (UI, local mode): email/password → session token → exchange for workspace
   tokens. Bootstrap admin via `AUTH_BOOTSTRAP_ADMIN_EMAIL/_PASSWORD` env vars.

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

When a registry is configured, `sd.build()`:

1. Resolves its deployment (local: keyed on `STARDAG_CODE_ID` / the clean git commit)
2. Creates the build and a plan under `(deployment, settings)` holding the roots
3. Registers the walk in chunks (instances with their edges), then seals the plan
4. Claims each task before running it, reports start/complete/fail per execution
5. Completes or fails the build (releasing its claims)

### NoOpRegistry (Default)

Without configuration, the SDK uses `NoOpRegistry` — all registry calls are silently skipped:
no plan, no claims. Tasks still execute and persist to local targets normally.

### Custom Registries

`stardag.registry.RegistryABC` is organised around plans and members: `build_create`,
`plan_create`, `plan_register_members`, `plan_seal`, `member_start` (the claiming start),
`member_complete` / `member_fail` / `member_yield`, `build_complete` / `build_fail`,
deployments, settings and executions (each with an `_aio` twin). A custom registry is written
against it; `stardag.testing.InMemoryRegistry` is the reference implementation that follows
the server's seams, and the one to use in tests:

```python
from stardag.testing import InMemoryRegistry

registry = InMemoryRegistry()
sd.build(task, registry=registry)
```

## Registry UI

The React frontend provides builds (list and detail, with the active plan's graph), a task
explorer and task detail (status, claim holder, instances, executions, events, artifacts),
artifact viewing (markdown, JSON) and workspace management (members, invites, API keys). Only
its registry calls (builds, plans, tasks, executions, deployments) moved to `/api/v2`; auth,
UI, workspace and target-root calls stay on `/api/v1`.

## CLI Commands

All registry-backed commands accept `-p/--stardag-profile` and `-e/--stardag-env` (except
`stardag build`, which takes only `-p`) and `--json` (one JSON document on stdout).

### Building

```bash
stardag build mypkg.dags:root                        # sd.build here (task, list, callable or class)
stardag build mypkg.dags:Train --param epochs=5      # a task class, constructed from --param
stardag build mypkg.dags:root --settings K=V         # build settings (repeatable)
stardag build mypkg.dags:root --app mypkg.app:app --reactive   # build_trigger on a deployed app
stardag build mypkg.dags:root --resume <build-id>    # resume (stored settings reused)
stardag build mypkg.dags:root --dry-run              # print the plan; write nothing
```

### Builds

```bash
stardag builds list [--status S] [--app NAME]        # most recently active first, paged
stardag builds show <build-id>                       # status, failure reason, active plan
stardag builds frontier <build-id>                   # what a tick sees: discovery jobs, runnable, running
stardag builds ticks <build-id>                      # the reactive scheduler's recent ticks
stardag builds cancel <build-id>                     # release claims; stops nothing
stardag builds stop <build-id> [--dry-run]           # stop unended executions, then cancel
stardag builds stop <build-id> --not-in-current-plan # only orphans of a rollover (implies --no-cancel)
stardag builds stop <build-id> --mark-lost           # also end executions it cannot stop (outcome lost)
stardag builds complete <build-id> [--force]
stardag builds fail <build-id> [--message TEXT]
```

`builds cancel` releases the build's claims and reaches no container: a worker notices at its
next checkpoint. `builds stop` is the hard stop: it lists the build's unended executions from
the ledger, cancels each Modal call it can identify, reports it stopped, and cancels the build
unless `--no-cancel`. Filters: `--executor`, `--worker`, `--namespace`, `--older-than 2h`,
`--task-id`. Use `--mark-lost` only when the container is known gone: a later report from a
lost execution is refused.

### Plans, deployments, executions, tasks

```bash
stardag plans show <plan-id>                          # lifecycle, scope, member counts
stardag plans list --build <build-id>                 # rollover / re-trigger history
stardag deployments list [--app NAME] [--current]     # newest first, current marked
stardag deployments show <deployment-id>
stardag executions list --build <build-id> [--include-ended] [--not-in-current-plan]
stardag executions list --task <task-id>
stardag tasks list [--status running]                 # Claim column names the holding build
stardag tasks show <task-id> [--events N]             # status, claim holder, instances, executions
stardag tasks check <task-id> --module mypkg.tasks    # run complete() locally; writes nothing
stardag tasks retry <task-id> [--build <build-id>]    # FAILED -> PENDING (refused on COMPLETED)
stardag tasks cancel <task-id> [--build <build-id>]   # release one claim; stops nothing
stardag tasks exclude <plan-id> <task-id> --reason TEXT
```

### Concurrency Limits

```bash
stardag concurrency-limits list [--holders]          # limits, in_use, and holders in one call
stardag concurrency-limits set <key> <max_concurrent> # upsert (0 blocks the key)
stardag concurrency-limits delete <key>
stardag concurrency-limits holders <key>
```

There is no `evict`: a slot is released by ending its execution (`builds stop --mark-lost`
for a holder whose worker is gone).

### Authentication

```bash
stardag auth login              # Browser-based OIDC login (or local email/password)
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

### Modal Integration

```bash
stardag modal deploy my_pkg/app.py          # record a deployment, deploy, activate it
stardag modal deployments                   # alias of `stardag deployments list`
stardag modal stardag-api-key create        # Create API key for Modal
```

### Self-Hosting (API + UI on Modal)

Requires the `selfhost` extra (`pip install "stardag[selfhost]"`). Deploys
the prebuilt server image by default (`--from-source` builds from a repo
checkout). See docs/how-to/self-host-modal.md.

```bash
stardag self-host up        # Provision Neon DB, migrate, deploy API+UI, complete setup
stardag self-host connect   # (Re)run the post-deploy setup only (idempotent)
stardag self-host upgrade   # Apply migrations + redeploy (--accept-data-loss for the v2 migration)
stardag self-host status    # Deployment status + URL
stardag self-host destroy   # Stop the Modal app (DB untouched)
```

The server app (default name `server`) and its secrets live in a dedicated Modal environment
(default `stardag-host`, `--server-modal-env`), isolated from the environments where DAG apps
run. `up`/`connect` also create a primary workspace, a `main` environment, an API key pushed as
Modal secret `stardag-api-key` into the DAG-execution environment (`--execution-modal-env`), a
default target root on a Modal volume, and a local SDK registry + profile named `selfhosted`.

## Upgrading from v1

- **The registry starts empty.** The v2 migration drops every v1 build, task, event,
  deployment, artifact and concurrency-limit record (users, workspaces, environments, API keys
  and target roots are kept). On a database holding v1 rows it refuses unless
  `STARDAG_ACCEPT_V2_DATA_LOSS=1` is set for that migration run
  (`stardag self-host upgrade --accept-data-loss` sets it); take a `pg_dump` first if the
  history matters. Set concurrency limits again afterwards.
- **Targets are untouched**, so the first v2 build of an existing DAG finds its tasks complete
  and re-runs nothing; they appear as complete leaves until something rebuilds them.
- **Upgrade SDK and server together**; finish or cancel running v1 builds first. Redeploy every
  Modal app with the v2 `stardag modal deploy`.
- **Code**: `significance=` / `hash_exclude=` → `StardagField(significant=False)` (no task id
  moves); `build_config=` / `sd.build_config_scope` / `sd.get_build_config` /
  `sd.set_build_config` → `settings`, read through a settings class; `GlobalLockConfig` and
  the lock routes → gone (the claim is the only mutual exclusion); `stardag builds cleanup` →
  gone; `stardag tasks list` filters by status only. Python 3.11+.
- Removed errors: `RegistryTooOldError`, `SDKVersionUnsupportedError`, `ScopeMismatchError`,
  `BuildConfigMismatchError`.

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

## API Technology Stack

- **Framework**: FastAPI 0.115+
- **ORM**: SQLAlchemy 2.0+ (async)
- **Database**: PostgreSQL 15+ + asyncpg (SQLite is not supported)
- **Migrations**: Alembic (always use `--autogenerate`)
- **Validation**: Pydantic
- **Auth**: JWT + OIDC (Keycloak)
- **HTTP Client**: httpx

## Further Reading

For the latest platform documentation, visit [docs.stardag.com/platform](https://docs.stardag.com/platform/).
