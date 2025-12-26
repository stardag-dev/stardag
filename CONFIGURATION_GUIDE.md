# Stardag Configuration Guide

This guide covers how to configure Stardag for different deployment scenarios, manage multiple API backends, and organize work across organizations and workspaces.

## Quick Summary

Stardag configuration follows a hierarchical model:

```
Registry (API Backend)
  └── Organization (team/company)
        └── Workspace (project/stage separation)
              └── Target Roots (where task outputs are stored)
```

**Registry** = Which Stardag API backend you're connecting to (local dev server vs central/remote SAAS).

**Workspace** = Logical grouping within an organization for separating projects or stages (staging vs production).

**Target Roots** = Named storage locations (local paths or S3 URIs) where task outputs are persisted.

## Conceptual Model

### Registries

A **registry** represents a connection to a specific Stardag API backend. Each registry is defined by an (API) URL and the stardag CLI manages credentials for the respective registry (and organization):

**Common registry setup:**

| Registry          | API URL                   | Use Case                              |
| ----------------- | ------------------------- | ------------------------------------- |
| `local`           | `http://localhost:8000`   | Local development with docker-compose |
| `central\|remote` | `https://api.stardag.com` | Central SAAS deployment               |

For self-hosted deployments, you might have additional registries:

| Registry          | API URL                               | Use Case               |
| ----------------- | ------------------------------------- | ---------------------- |
| `local`           | `http://localhost:8000`               | Local development      |
| `company-staging` | `https://staging.stardag.company.com` | Staging environment    |
| `company-prod`    | `https://api.stardag.company.com`     | Production environment |

### Organizations

An **organization** is a team or company that shares access to workspaces and their contents. Organizations exist within a registry (API backend).

- Users can belong to multiple organizations
- Each organization has members with roles (owner, admin, member)
- Organizations contain workspaces

### Workspaces

A **workspace** is a logical grouping of tasks and builds within an organization. Use workspaces to separate:

- **Different projects** that don't share tasks (e.g., `ml-pipeline`, `data-etl`)
- **Different stages** of the same project (e.g., `myproject-staging`, `myproject-prod`)
- **Personal development** (auto-created `personal-{username}` workspace)

Each workspace has:

- Its own set of target roots (storage locations)
- Its own API keys for CI/CD access
- Isolated task and build history

### Target Roots

**Target roots** are named storage locations where task outputs are persisted. They are configured per workspace to ensure all team members use the same paths.

Example target roots for a workspace:

| Name      | URI Prefix                          | Purpose           |
| --------- | ----------------------------------- | ----------------- |
| `default` | `s3://company-bucket/stardag/prod/` | Primary storage   |
| `archive` | `s3://company-archive/stardag/`     | Long-term storage |

The target root configuration is:

- **Defined centrally** in the workspace settings (via UI or API)
- **Cached locally** for offline access
- **Validated at build time** to ensure consistency across team members

## Configuration Hierarchy

Stardag resolves configuration values in this priority order (highest to lowest):

1. **Environment variables** (`STARDAG_*`)
2. **Project config** (`.stardag/config.toml` in a repository/project root directory, ~~or a `[tool.stardag]` section in your `pyproject.toml` file.~~[NOTE this might be added LATER])
3. **User config** (`~/.stardag/config.toml`)
4. **Defaults**

This allows:

- CI/CD to override via environment variables
- Projects to set sensible defaults for all contributors
- Users to have personal preferences that apply across projects

_NOTE: Providing the environment variables `STARDAG_REGISTRY_URL`, `STARDAG_API_KEY` and `STARDAG_WORKSPACE` is sufficent to fully specify, and override, all other configuration related to registry, organization and workspace. This is what you will normally provide when building tasks in a production worker._

_NOTE: A `.stardag/config.toml` file ~~or a `[tool.stardag]` section in a `pyproject.toml` file~~ [NOTE this might be added later] in any parent directpry of current working directory when code is executed, always fully override the one specified in the user's home directory._

## Local Configuration and Active Profile

Stardag is built to facilitate explorative and data centric work, such as Data Science/ML/AI experimentation. As such, it aims to make it seamless to move between running tasks and loading outputs in "production" (any hosted/cloud based infrastructure) as well as _locally_ on your working station. (TODO complete: both switching between workspaces, including copying of tasks, and reading (+optionally writing) _from(/to) production_ on your local workstation)

For the latter use case, Stardag provides a CLI and configuration files to easilly get setup and switch between registries, organization and workspaces.

### Active Profile

Whenever code is executed which builds or loads `Task`s, or when you interact with the Registry API, there must be an _active profile_ that defines which registry, organization and worspace is in use. As of before, in production workers, providing the environment variables `STARDAG_REGISTRY_URL`, `STARDAG_API_KEY` and `STARDAG_WORKSPACE_ID` is sufficient and the recommeneded way to define the active context and handle authentication.

When building or loading tasks locally, it is recommended to authenticate and create predefined profiles via the `stardag` CLI or directly in the `.stardag/config.toml` or the CLI. This way you it is enough to set the environment variable `STARDAG_PROFILE="my-profile"` rather than `STARDAG_REGISTRY_URL`, `STARDAG_ORGANIZAION_ID` and `STARDAG_WORKSPACE_ID`.

To get started, run:

```sh
stardag registry add local --url "http://localhost:3000"
stardag auth loging [--registry=local]
stardag profile add local [-r|--registry=local] [-o|--organization="my-org-slug"] [-w|--workspace="my-workspace-slug"]
```

Or edit the config.toml directly, see an example below:

```toml
[registry.local]
url = "http://localhost:3000"

[registry.central]
url = "https://api.startdag.com"

[profile.local]
registry = "local"
organization = "default"
workspace = "default"

[profile.central-individual]
registry = "central"
organization = "my-org"
workspace = "default-username"

[profile.central-dev]
registry = "central"
organization = "my-org"
workspace = "developement"

[profile.central-prod]
registry = "central"
organization = "my-org"
workspace = "production"

[default]
profile = "local"
```

### File Structure

Configuration is stored in a directory called `.stardag` either in the user's home directory or within a specific project:

```
<USER_HOME>|<PROJECT_ROOT>/.stardag/
├── config.toml
├── credentials/                   # Per-registry refresh tokens (gitignore in PROJECT_ROOT)
│   ├── local.json                 # { refresh_token, token_endpoint, client_id }
│   └── central.json
├── access-token-cache/            # Short-lived org-scoped JWTs (gitignore in PROJECT_ROOT)
│   ├── local__default.json        # { access_token, expires_at }
│   └── central__my-org.json
├── local-target-roots/            # For local file-based targets (gitignore in PROJECT_ROOT)
│   └── default/                   # workspace
│       └── default/               # target root name
└── target-root-cache.json
```

**Credentials model:** Refresh tokens are user-scoped and stored per-registry (not per-organization). The org-scoped access JWTs are minted on demand via the `/auth/exchange` endpoint and cached briefly.

### Target Roots Cache

Target roots are defined centrally in workspace settings to ensure consistency across team members. The SDK validates local cache against central configuration. Target root cache is stored as a flat array on the format:

```json
[
  {
    "registry_url": "...",
    "organization_id": "...",
    "workspace_id": "...",
    "target_roots": {
      "name-1": "uri-prefix-1"
      // ...
    }
  }
]
```

See the [Target Roots Syncronization](#target-roots-synchronization) for further details.

## Environment Variables

All configuration can be overridden via environment variables:

| Variable                         | Description                                             |
| -------------------------------- | ------------------------------------------------------- |
| `STARDAG_REGISTRY_URL`           | Registry API backend URL                                |
| `STARDAG_REGISTRY_API_KEY`       | API key                                                 |
| `STARDAG_ORGANIZATION_ID`        | Active organization ID                                  |
| `STARDAG_WORKSPACE_ID`           | Active workspace ID                                     |
| `STARDAG_TARGET_ROOTS[__{NAME}]` | Override specific target root (json string or per name) |

## CLI Commands

### Registry Management

```bash
# List available registries
stardag registry list

# Add a new registry (provide url as arg or get prompted to provide as input)
stardag registry add central [--url https://api.stardag.com]
```

### Profile Management

```bash
# Add a new profile (provide args or get prompted to provide them as inputs/select from available)
stardag profile add local [-r|--registry=local] [-o|--organization="my-org-slug"] [-w|--workspace="my-workspace-slug"]
```

### Authentication

```bash
# Login (opens browser for OAuth, uses https://api.stardag.com by default unless provided)
stardag auth login

# Login to specific registry
stardag auth login --registry central

# Check auth status
stardag auth status

# Logout
stardag auth logout [--registry]
```

### Organization, Workspace and Target Roots

```bash
# List organizations (in current registry, defined by `STARDAG_REGISTRY_URL` or `STARDAG_PROFILE`, unless provided as arg)
stardag organizations list [--registry]

# List workspaces (in current registry and organization, defined by `STARDAG_REGISTRY_URL` and `STARDAG_ORGANIZATION_ID` or `STARDAG_PROFILE` unless provided as arg)
stardag workspaces list [--registry] [--organization]

# Sync workspace settings from server
stardag target-roots list [--registry] [--organization] [--workspace]

# Sync and verify target roots with registry
stardag target-roots sync [--registry] [--organization] [--workspace]
```

## Target Roots Synchronization

Target roots are defined centrally in workspace settings to ensure consistency across team members. The SDK validates local cache against central configuration:

**On login / `stardag target-roots sync`:**

- Fetches latest target roots from server
- Updates local cache

**On build start:**

- Compares local cache with server
- **New roots added**: Auto-syncs and logs the change
- **Roots modified or deleted**: Raises error, requires explicit sync

This protects against:

- Building with outdated storage paths
- Accidentally using a teammate's local paths
- Inconsistent task output locations

### Warning on Modifying Target Roots

Modifying or deleting existing target roots is discouraged because:

1. Previously completed tasks will appear incomplete (outputs not found at new location)
2. Historical builds reference the old paths

**Recommended approach:**

1. Create a new target root with the new path
2. Update task definitions to use the new root
3. Keep the old root for historical reference (or migrate data manually)

## Troubleshooting

### "Target root mismatch" error

Your local cache doesn't match the server. Run:

```bash
stardag stardag target-roots sync
```

### Working offline

The SDK caches target roots locally, so you can read task outputs without API connectivity. However, registering new builds requires connection to the API.
