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

**Registry** = Which Stardag API backend you're connecting to (local dev server vs central/remote SaaS).

**Organization** = A team or company that shares access to workspaces.

**Workspace** = Logical grouping within an organization for separating projects or stages.

**Target Roots** = Named storage locations (local paths or S3 URIs) where task outputs are persisted.

## Conceptual Model

### Registries

A **registry** represents a connection to a specific Stardag API backend. Each registry is defined by a URL, and the CLI manages credentials per registry.

**Common registry setup:**

| Registry  | API URL                   | Use Case                              |
| --------- | ------------------------- | ------------------------------------- |
| `local`   | `http://localhost:8000`   | Local development with docker-compose |
| `central` | `https://api.stardag.com` | Central SaaS deployment               |

For self-hosted deployments, you might have additional registries:

| Registry          | API URL                               | Use Case               |
| ----------------- | ------------------------------------- | ---------------------- |
| `local`           | `http://localhost:8000`               | Local development      |
| `company-staging` | `https://staging.stardag.company.com` | Staging environment    |
| `company-prod`    | `https://api.stardag.company.com`     | Production environment |

### Organizations

An **organization** is a team or company that shares access to workspaces. Organizations exist within a registry.

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

Target roots are:

- **Defined centrally** in the workspace settings (via UI or API)
- **Cached locally** for offline access
- **Validated at build time** to ensure consistency across team members

## Configuration Hierarchy

Stardag resolves configuration in this priority order (highest to lowest):

1. **Environment variables** (`STARDAG_*`)
2. **Project config** (`.stardag/config.toml` in repository root)
3. **User config** (`~/.stardag/config.toml`)
4. **Defaults**

This allows:

- CI/CD to override via environment variables
- Projects to set sensible defaults for all contributors
- Users to have personal preferences that apply across projects

> **Note:** Providing `STARDAG_REGISTRY_URL`, `STARDAG_API_KEY`, and `STARDAG_WORKSPACE_ID` is sufficient to fully specify the context for production workers.

> **Note:** A `.stardag/config.toml` file in any parent directory of the current working directory takes precedence over the user's home directory config.

## Local Configuration

Stardag facilitates exploratory and data-centric work (Data Science, ML, AI experimentation). It makes it seamless to switch between running tasks locally and in production environments.

### Profiles

A **profile** defines a complete context: registry, organization, and workspace. Profiles make it easy to switch between different environments.

**Setting up profiles:**

```bash
# Add a registry
stardag config registry add local --url "http://localhost:8000"

# Login (opens browser for OAuth)
stardag auth login --registry local

# Create a profile (uses slugs for readability)
stardag config profile add dev -r local -o my-org -w development --default
```

**Activating a profile:**

```bash
# Via environment variable
export STARDAG_PROFILE=dev

# Or switch the default profile
stardag config profile use prod
```

### Config File Example

Edit `~/.stardag/config.toml` directly:

```toml
[registry.local]
url = "http://localhost:8000"

[registry.central]
url = "https://api.stardag.com"

[profile.dev]
registry = "local"
organization = "my-org"      # slug (human-readable)
workspace = "development"    # slug (human-readable)

[profile.prod]
registry = "central"
organization = "my-company"
workspace = "production"

[default]
profile = "dev"
```

> **Note:** Profiles store organization and workspace by **slug** (not UUID) for readability. The IDs are resolved and cached automatically.

### File Structure

Configuration is stored in `~/.stardag/` (user) or `<project>/.stardag/` (project):

```
.stardag/
├── config.toml                # Main configuration
├── id-cache.json              # Slug-to-ID mappings (auto-populated)
├── credentials/               # Per-registry refresh tokens
│   ├── local.json
│   └── central.json
├── access-token-cache/        # Short-lived org-scoped JWTs
│   ├── local__<org-id>.json
│   └── central__<org-id>.json
├── target-root-cache.json     # Cached target roots per workspace
└── local-target-roots/        # Local file-based target storage
    └── <workspace>/
        └── <target-root>/
```

**Credentials model:**

- Refresh tokens are user-scoped and stored per-registry
- Org-scoped access tokens are minted via `/auth/exchange` and cached briefly
- ID mappings (slug → UUID) are cached in `id-cache.json`

## Environment Variables

All configuration can be overridden via environment variables:

| Variable                    | Description                           |
| --------------------------- | ------------------------------------- |
| `STARDAG_PROFILE`           | Active profile name                   |
| `STARDAG_REGISTRY_URL`      | Registry API URL (overrides profile)  |
| `STARDAG_API_KEY`           | API key (for CI/CD)                   |
| `STARDAG_ORGANIZATION_ID`   | Organization ID (overrides profile)   |
| `STARDAG_WORKSPACE_ID`      | Workspace ID (overrides profile)      |
| `STARDAG_TARGET_ROOTS`      | Set all target roots as a JSON string |
| `STARDAG_TARGET_ROOTS__{N}` | Override specific target root `N`     |

**For production workers**, set:

```bash
export STARDAG_REGISTRY_URL="https://api.stardag.com"
export STARDAG_API_KEY="sk-..."
export STARDAG_WORKSPACE_ID="..."
```

## CLI Commands

### Configuration

```bash
# Show current configuration and context
stardag config show
```

### Registry Management

```bash
# List registries
stardag config registry list

# Add a registry
stardag config registry add central --url https://api.stardag.com

# Remove a registry
stardag config registry remove central
```

### Profile Management

```bash
# List profiles
stardag config profile list

# Add a profile (slugs are resolved and cached automatically)
stardag config profile add prod -r central -o my-company -w production

# Set default profile (also refreshes access token)
stardag config profile use prod

# Remove a profile
stardag config profile remove old-profile
```

### Authentication

```bash
# Login (opens browser for OAuth)
stardag auth login

# Login to specific registry
stardag auth login --registry central

# Check auth status
stardag auth status

# Refresh access token for current profile
stardag auth refresh

# Logout
stardag auth logout
```

### Organizations and Workspaces

```bash
# List organizations you have access to
stardag config list organizations

# List workspaces in current organization
stardag config list workspaces
```

### Target Roots

```bash
# List cached target roots
stardag config target-roots list

# Sync target roots from server
stardag config target-roots sync
```

## Target Roots Synchronization

Target roots are defined centrally in workspace settings to ensure consistency across team members.

**On login / `stardag config target-roots sync`:**

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

1. Previously completed tasks will appear incomplete (outputs not found)
2. Historical builds reference the old paths

**Recommended approach:**

1. Create a new target root with the new path
2. Update task definitions to use the new root
3. Keep the old root for historical reference (or migrate data manually)

## Troubleshooting

### "Target root mismatch" error

Your local cache doesn't match the server. Run:

```bash
stardag config target-roots sync
```

### "Could not resolve organization/workspace" warning

The slug couldn't be resolved to an ID. Ensure you're logged in:

```bash
stardag auth login --registry <registry>
stardag config profile use <profile>
```

### Working offline

The SDK caches target roots locally, so you can read task outputs without API connectivity. However, registering new builds requires connection to the API.
