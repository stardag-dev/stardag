# Stardag Configuration Guide

This guide covers how to configure Stardag for different deployment scenarios, manage multiple API backends, and organize work across organizations and workspaces.

## Quick Summary

Stardag configuration follows a hierarchical model:

```
Profile (API Backend)
  └── Organization (team/company)
        └── Workspace (project/stage separation)
              └── Target Roots (where task outputs are stored)
```

**Profile** = Which Stardag API backend you're connecting to (local dev server vs central/remote SAAS).

**Workspace** = Logical grouping within an organization for separating projects or stages (staging vs production).

**Target Roots** = Named storage locations (local paths or S3 URIs) where task outputs are persisted.

## Conceptual Model

### Profiles

A **profile** represents a connection to a specific Stardag API backend. Each profile contains:

- API URL (where the backend is running)
- Credentials (OAuth tokens or API key)
- Cached workspace settings

Profiles are stored in `~/.stardag/profiles/{profile_name}/`.

**Common profile setup:**

| Profile   | API URL                  | Use Case                              |
| --------- | ------------------------ | ------------------------------------- |
| `local`   | `http://localhost:8000`  | Local development with docker-compose |
| `central` | `https://api.stardag.io` | Central SAAS deployment               |

For self-hosted deployments, you might have additional profiles:

| Profile           | API URL                               | Use Case               |
| ----------------- | ------------------------------------- | ---------------------- |
| `local`           | `http://localhost:8000`               | Local development      |
| `company-staging` | `https://staging.stardag.company.com` | Staging environment    |
| `company-prod`    | `https://api.stardag.company.com`     | Production environment |

### Organizations

An **organization** is a team or company that shares access to workspaces and their contents. Organizations exist within a profile (API backend).

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

Stardag resolves configuration values in this priority order (highest to lowest):

1. **Environment variables** (`STARDAG_*`)
2. **Project config** (`.stardag/config.json` in repository root)
3. **Profile config** (`~/.stardag/profiles/{profile}/config.json`)
4. **Defaults**

This allows:

- CI/CD to override via environment variables
- Projects to set sensible defaults for all contributors
- Users to have personal preferences that apply across projects

## File Structure

### User Configuration

```
~/.stardag/
├── active_profile              # Contains active profile name (e.g., "central")
└── profiles/
    ├── local/
    │   ├── active_workspace    # Contains the active workspace in this profile
    │   ├── config.json         # API URL, other *shared* settings (can be overriden in project config)
    │   ├── credentials.json    # OAuth tokens
    │   └── workspaces/
    |       └── README.md      # Explaining content optional
    │       └── {workspace_id}/
    │           └── target_roots.json
    └── central/
    │   ├── active_workspace
        ├── config.json
        ├── credentials.json
        └── workspaces/
            └── {workspace_id}/
                └── target_roots.json
```

### Project Configuration

```
your-project/
├── .stardag/
│   └── config.json             # Project-specific settings
├── src/
└── ...
```

## Configuration Files

### Profile Config (`~/.stardag/profiles/{profile}/config.json`)

```json
{
  "api_url": "https://api.stardag.io",
  "timeout": 30.0,
  "organization_id": "org_abc123",
  "workspace_id": "ws_def456"
}
```

### Profile Credentials (`~/.stardag/profiles/{profile}/credentials.json`)

```json
{
  "access_token": "eyJ...",
  "refresh_token": "eyJ...",
  "token_endpoint": "https://auth.stardag.io/token",
  "client_id": "stardag-cli"
}
```

### Project Config (`.stardag/config.json`)

```json
{
  "profile": "central",
  "organization_id": "org_abc123",
  "workspace_id": "ws_def456",
  "allowed_organizations": ["org_abc123", "my-org-slug"]
}
```

**Fields:**

| Field                   | Description                                             |
| ----------------------- | ------------------------------------------------------- |
| `profile`               | Which profile to use by default for this project        |
| `organization_id`       | Override the active organization                        |
| `workspace_id`          | Override the active workspace                           |
| `allowed_organizations` | Restrict which organizations can be used (safety check) |

### Workspace Target Roots (`~/.stardag/profiles/{profile}/workspaces/{workspace_id}/target_roots.json`)

```json
{
  "default": "s3://company-bucket/stardag/prod/",
  "archive": "s3://company-archive/stardag/"
}
```

This file is synced from the central API when you set a workspace. It can also be committed to a project's `.stardag/workspaces/{workspace_id}/target_roots.json` for team consistency.

## Environment Variables

All configuration can be overridden via environment variables:

| Variable                      | Description                         |
| ----------------------------- | ----------------------------------- |
| `STARDAG_PROFILE`             | Active profile name                 |
| `STARDAG_API_URL`             | API backend URL                     |
| `STARDAG_API_KEY`             | API key (for CI/CD, bypasses OAuth) |
| `STARDAG_ORGANIZATION_ID`     | Active organization ID              |
| `STARDAG_WORKSPACE_ID`        | Active workspace ID                 |
| `STARDAG_TARGET_ROOT__{NAME}` | Override specific target root       |

## CLI Commands

### Profile Management

```bash
# List available profiles
stardag profile list

# Add a new profile
stardag profile add central --api-url https://api.stardag.io

# Switch active profile
stardag profile use central

# Show current profile
stardag profile current
```

### Authentication

```bash
# Login (opens browser for OAuth)
stardag auth login

# Login to specific profile
stardag auth login --profile central

# Check auth status
stardag auth status

# Logout
stardag auth logout
```

### Organization & Workspace

```bash
# List organizations (in current profile)
stardag config list organizations

# Set active organization (by ID or slug)
stardag config set organization my-org-slug

# List workspaces (in active organization)
stardag config list workspaces

# Set active workspace (by ID or slug)
stardag config set workspace myproject-prod

# Sync workspace settings from server
stardag config sync
```

### Show Current Configuration

```bash
# Show resolved configuration
stardag config show
```

## Common Patterns

### Pattern 1: Local Development + Central Production

Most common setup for individual developers or small teams:

```bash
# Setup local profile (docker-compose)
stardag profile add local --api-url http://localhost:8000

# Setup central profile (SAAS)
stardag profile add central --api-url https://api.stardag.io
stardag profile use central
stardag auth login

# In your project, set defaults
# .stardag/config.json:
{
  "profile": "central",
  "workspace_id": "ws_your_workspace"
}
```

**Workflow:**

- Daily work: Uses `central` profile automatically (from project config)
- Testing stardag itself: `stardag profile use local`

### Pattern 2: Multi-Stage Deployment

For projects with staging and production environments:

```bash
# Create workspaces in UI:
# - myproject-staging
# - myproject-prod

# For staging deployment (CI/CD):
export STARDAG_WORKSPACE_ID=ws_staging_id
export STARDAG_API_KEY=key_for_staging

# For production deployment (CI/CD):
export STARDAG_WORKSPACE_ID=ws_prod_id
export STARDAG_API_KEY=key_for_prod
```

### Pattern 3: Multi-Organization Safety

For consultants or developers working across multiple client organizations:

```json
// Project A: .stardag/config.json
{
  "profile": "central",
  "organization_id": "org_client_a",
  "allowed_organizations": ["org_client_a"]
}

// Project B: .stardag/config.json
{
  "profile": "central",
  "organization_id": "org_client_b",
  "allowed_organizations": ["org_client_b"]
}
```

This prevents accidentally running builds against the wrong organization.

### Pattern 4: Personal Development Workspace

Each user automatically gets a personal workspace (`personal-{username}`) in every organization they join. Use this for:

- Experimenting with task definitions
- Testing changes before merging
- Running ad-hoc analysis

```bash
# Switch to personal workspace
stardag config set workspace personal-anders

# Run experimental build
python my_experiment.py
```

## Target Root Synchronization

Target roots are defined centrally in workspace settings to ensure consistency across team members. The SDK validates local cache against central configuration:

**On login / `stardag config sync`:**

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
stardag config sync
```

### "Organization not allowed" error

The project's `.stardag/config.json` restricts allowed organizations. Either:

1. Switch to an allowed organization
2. Update the project config (if you have permission)

### "No active workspace" error

Set a workspace:

```bash
stardag config list workspaces
stardag config set workspace <workspace-slug>
```

### Working offline

The SDK caches target roots locally, so you can read task outputs without API connectivity. However, registering new builds requires connection to the API.
