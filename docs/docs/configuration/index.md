# Configuration

Stardag configuration follows a hierarchical model for managing different environments, teams, and storage locations.

## Conceptual Model

```
Registry (API Backend)
  └── Organization (team/company)
        └── Workspace (project/stage)
              └── Target Roots (storage locations)
```

| Concept          | Description                                        |
| ---------------- | -------------------------------------------------- |
| **Registry**     | A Stardag API backend (local dev server or SaaS)   |
| **Organization** | A team or company sharing access to workspaces     |
| **Workspace**    | Logical grouping for separating projects or stages |
| **Target Roots** | Named storage locations for task outputs           |

## Configuration Hierarchy

Settings are resolved in priority order (highest to lowest):

1. **Environment variables** (`STARDAG_*`)
2. **Project config** (`.stardag/config.toml` in repository)
3. **User config** (`~/.stardag/config.toml`)
4. **Defaults**

## Quick Start

### Local Development

```bash
# Set target root for local outputs
export STARDAG_TARGET_ROOT__DEFAULT=~/.stardag/outputs
```

### With API Service

```bash
# Install CLI
pip install stardag

# Add registry and login
stardag config registry add local --url http://localhost:8000
stardag auth login --registry local

# Create and use profile
stardag config profile add dev -r local -o my-org -w development
stardag config profile use dev
```

### Production (CI/CD)

```bash
export STARDAG_REGISTRY_URL=https://api.stardag.com
export STARDAG_API_KEY=sk_...
export STARDAG_WORKSPACE_ID=...
export STARDAG_TARGET_ROOT__DEFAULT=s3://bucket/stardag/
```

## In This Section

- **[Profiles](profiles.md)** - Switch between environments
- **[Workspaces](workspaces.md)** - Organize projects and teams
- **[CLI Reference](cli.md)** - All CLI commands
