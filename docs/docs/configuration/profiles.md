# Profiles

Profiles define a complete context for Stardag operations: registry, user, workspace, and environment.

## Creating Profiles

### Via CLI

=== "Activated venv"

    ```bash
    # Add a registry first
    stardag config registry add central --url https://api.stardag.com

    # Login to the registry
    stardag auth login --registry central

    # Create a profile
    stardag config profile add my-company_prod \
        --registry central \
        --user me@company.com \
        --workspace my-company \
        --environment production
    ```

=== "uv run ..."

    ```bash
    # Add a registry first
    uv run stardag config registry add central --url https://api.stardag.com

    # Login to the registry
    uv run stardag auth login --registry central

    # Create a profile
    uv run stardag config profile add my-company_prod \
        --registry central \
        --user me@company.com \
        --workspace my-company \
        --environment production
    ```

### Via Config File

Edit `~/.stardag/config.toml`:

```toml
[registry.local]
url = "http://localhost:8000"

[registry.central]
url = "https://api.stardag.com"

[profile.local_dev]
registry = "local"
user = "me@example.com"
workspace = "my-workspace"
environment = "development"

[profile.my-company_prod]
registry = "central"
user = "me@company.com"
workspace = "my-company"
environment = "production"

[default]
profile = "local_dev"
```

## Profile Fields

| Field         | Description                               |
| ------------- | ----------------------------------------- |
| `registry`    | Name of the registry (API backend) to use |
| `user`        | User email for credential lookup          |
| `workspace`   | Workspace slug (team/company)             |
| `environment` | Environment slug (project/stage)          |

## Using Profiles

### Set Default Profile

=== "Activated venv"

    ```bash
    stardag config profile use prod
    ```

=== "uv run ..."

    ```bash
    uv run stardag config profile use prod
    ```

### Environment Variable

```bash
export STARDAG_PROFILE=prod
```

### Temporary Override

```bash
# Run single command with different profile
STARDAG_PROFILE=dev python my_script.py
```

## Profile Management

### List Profiles

=== "Activated venv"

    ```bash
    stardag config profile list
    ```

=== "uv run ..."

    ```bash
    uv run stardag config profile list
    ```

### Show Current Configuration

=== "Activated venv"

    ```bash
    stardag config show
    ```

=== "uv run ..."

    ```bash
    uv run stardag config show
    ```

### Remove Profile

=== "Activated venv"

    ```bash
    stardag config profile remove old-profile
    ```

=== "uv run ..."

    ```bash
    uv run stardag config profile remove old-profile
    ```

## File Structure

```
~/.stardag/
├── config.toml                # Main configuration
├── id-cache.json              # Slug-to-ID mappings (auto-populated)
├── credentials/               # Per-registry, per-user refresh tokens
│   ├── local__me@example.com.json
│   └── central__me@company.com.json
├── access-token-cache/        # Short-lived workspace-scoped JWTs
│   └── ...
├── target-root-cache.json     # Cached target roots per environment
└── local-target-roots/        # Local file storage
```

## Project-Level Profiles

You can place a `.stardag/config.toml` in your repository root. This will be merged with user config in `~/.stardag/config.toml`. Project config takes precedence over user config. Since the profiles contain user details, a project level config should typically be git-ignored for multi-user projects (but keeping project level configs can still be helpful to set defaults per project/repo).

## Environment Variable Overrides

Environment variables override profile settings:

| Variable                 | Description                        |
| ------------------------ | ---------------------------------- |
| `STARDAG_PROFILE`        | Active profile name                |
| `STARDAG_REGISTRY_URL`   | Registry URL (overrides profile)   |
| `STARDAG_API_KEY`        | API key for authentication         |
| `STARDAG_WORKSPACE_ID`   | Workspace ID (overrides profile)   |
| `STARDAG_ENVIRONMENT_ID` | Environment ID (overrides profile) |
