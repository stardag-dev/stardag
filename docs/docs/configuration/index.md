# Configuration

Stardag configuration follows a hierarchical model for managing different environments, teams, and storage locations.

## Conceptual Model

```
Registry (API Backend)
  └── Workspace (team/company)
        └── Environment (project/stage)
              └── Target Roots (storage locations)
```

| Concept          | Description                                        |
| ---------------- | -------------------------------------------------- |
| **Registry**     | A Stardag API backend (local dev server or SaaS)   |
| **Workspace**    | A team or company sharing access to environments   |
| **Environment**  | Logical grouping for separating projects or stages |
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

=== "uv"

    ```bash
    uv add stardag
    ```

=== "pip"

    ```bash
    pip install stardag
    ```

Then configure:

=== "Activated venv"

    ```bash
    # Add registry and login
    stardag config registry add local --url http://localhost:8000
    stardag auth login --registry local

    # Create and use profile
    stardag config profile add dev \
        -r local \
        -u me@example.com \
        -w my-workspace \
        -e development
    stardag config profile use dev
    ```

=== "uv run ..."

    ```bash
    # Add registry and login
    uv run stardag config registry add local --url http://localhost:8000
    uv run stardag auth login --registry local

    # Create and use profile
    uv run stardag config profile add dev \
        -r local \
        -u me@example.com \
        -w my-workspace \
        -e development
    uv run stardag config profile use dev
    ```

### Production (CI/CD)

```bash
export STARDAG_REGISTRY_URL=https://api.stardag.com
export STARDAG_API_KEY=sk_...
export STARDAG_WORKSPACE_ID=...
export STARDAG_ENVIRONMENT_ID=...
```

## In This Section

- **[Profiles](profiles.md)** - Switch between environments
- **[Workspaces & Environments](environments.md)** - Organize teams and projects
- **[CLI Reference](cli.md)** - All CLI commands
