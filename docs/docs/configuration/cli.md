# CLI Reference

Complete reference for Stardag CLI commands.

## Installation

=== "uv"

    ```bash
    uv add stardag
    ```

=== "pip"

    ```bash
    pip install stardag
    ```

## Global Commands

### Version

=== "Activated venv"

    ```bash
    stardag version
    ```

=== "uv run ..."

    ```bash
    uv run stardag version
    ```

Show SDK version.

## Authentication Commands

### Login

=== "Activated venv"

    ```bash
    stardag auth login [--registry NAME]
    ```

=== "uv run ..."

    ```bash
    uv run stardag auth login [--registry NAME]
    ```

Opens browser for OAuth authentication.

**Options:**

- `--registry NAME` - Target registry (default: from active profile)

### Status

=== "Activated venv"

    ```bash
    stardag auth status
    ```

=== "uv run ..."

    ```bash
    uv run stardag auth status
    ```

Show current authentication status.

### Refresh

=== "Activated venv"

    ```bash
    stardag auth refresh
    ```

=== "uv run ..."

    ```bash
    uv run stardag auth refresh
    ```

Refresh access token for current profile.

### Logout

=== "Activated venv"

    ```bash
    stardag auth logout
    ```

=== "uv run ..."

    ```bash
    uv run stardag auth logout
    ```

Clear stored credentials.

## Configuration Commands

### Show Configuration

=== "Activated venv"

    ```bash
    stardag config show
    ```

=== "uv run ..."

    ```bash
    uv run stardag config show
    ```

Display current configuration and context.

## Registry Management

### List Registries

=== "Activated venv"

    ```bash
    stardag config registry list
    ```

=== "uv run ..."

    ```bash
    uv run stardag config registry list
    ```

### Add Registry

=== "Activated venv"

    ```bash
    stardag config registry add NAME --url URL
    ```

=== "uv run ..."

    ```bash
    uv run stardag config registry add NAME --url URL
    ```

**Example:**

=== "Activated venv"

    ```bash
    stardag config registry add central --url https://api.stardag.com
    ```

=== "uv run ..."

    ```bash
    uv run stardag config registry add central --url https://api.stardag.com
    ```

### Remove Registry

=== "Activated venv"

    ```bash
    stardag config registry remove NAME
    ```

=== "uv run ..."

    ```bash
    uv run stardag config registry remove NAME
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

### Add Profile

=== "Activated venv"

    ```bash
    stardag config profile add NAME \
        --registry REGISTRY \
        --user USER \
        --workspace WORKSPACE \
        --environment ENVIRONMENT \
        [--default]
    ```

=== "uv run ..."

    ```bash
    uv run stardag config profile add NAME \
        --registry REGISTRY \
        --user USER \
        --workspace WORKSPACE \
        --environment ENVIRONMENT \
        [--default]
    ```

**Options:**

- `-r, --registry` - Registry name
- `-u, --user` - User email
- `-w, --workspace` - Workspace slug (team/company)
- `-e, --environment` - Environment slug (project/stage)
- `-d, --default` - Set as default profile

**Example:**

=== "Activated venv"

    ```bash
    stardag config profile add prod \
        -r central \
        -u me@company.com \
        -w my-company \
        -e production \
        --default
    ```

=== "uv run ..."

    ```bash
    uv run stardag config profile add prod \
        -r central \
        -u me@company.com \
        -w my-company \
        -e production \
        --default
    ```

### Use Profile

=== "Activated venv"

    ```bash
    stardag config profile use NAME
    ```

=== "uv run ..."

    ```bash
    uv run stardag config profile use NAME
    ```

Set the default profile (also refreshes access token).

### Remove Profile

=== "Activated venv"

    ```bash
    stardag config profile remove NAME
    ```

=== "uv run ..."

    ```bash
    uv run stardag config profile remove NAME
    ```

## Workspace & Environment Commands

### List Workspaces

=== "Activated venv"

    ```bash
    stardag config list workspaces
    ```

=== "uv run ..."

    ```bash
    uv run stardag config list workspaces
    ```

List workspaces you have access to.

### List Environments

=== "Activated venv"

    ```bash
    stardag config list environments
    ```

=== "uv run ..."

    ```bash
    uv run stardag config list environments
    ```

List environments in the active workspace.

## Target Root Commands

### List Target Roots

=== "Activated venv"

    ```bash
    stardag config target-roots list
    ```

=== "uv run ..."

    ```bash
    uv run stardag config target-roots list
    ```

Show cached target roots for current environment.

### Sync Target Roots

=== "Activated venv"

    ```bash
    stardag config target-roots sync
    ```

=== "uv run ..."

    ```bash
    uv run stardag config target-roots sync
    ```

Fetch latest target roots from server.

## Environment Variables

All CLI behavior can be overridden with environment variables:

| Variable                    | Description                   |
| --------------------------- | ----------------------------- |
| `STARDAG_PROFILE`           | Active profile name           |
| `STARDAG_REGISTRY_URL`      | Registry API URL              |
| `STARDAG_API_KEY`           | API key (bypasses OAuth)      |
| `STARDAG_WORKSPACE_ID`      | Workspace UUID                |
| `STARDAG_ENVIRONMENT_ID`    | Environment UUID              |
| `STARDAG_TARGET_ROOTS`      | JSON string of target roots   |
| `STARDAG_TARGET_ROOT__NAME` | Specific target root override |

## Common Workflows

### Initial Setup

=== "Activated venv"

    ```bash
    # Add registry
    stardag config registry add local --url http://localhost:8000

    # Login
    stardag auth login --registry local

    # Create profile
    stardag config profile add dev \
        -r local \
        -u me@example.com \
        -w my-workspace \
        -e development \
        --default

    # Verify
    stardag config show
    ```

=== "uv run ..."

    ```bash
    # Add registry
    uv run stardag config registry add local --url http://localhost:8000

    # Login
    uv run stardag auth login --registry local

    # Create profile
    uv run stardag config profile add dev \
        -r local \
        -u me@example.com \
        -w my-workspace \
        -e development \
        --default

    # Verify
    uv run stardag config show
    ```

### Switch Environments

=== "Activated venv"

    ```bash
    # Create multiple profiles
    stardag config profile add dev -r local -u me@example.com -w my-workspace -e dev
    stardag config profile add prod -r central -u me@company.com -w my-company -e prod

    # Switch between them
    stardag config profile use dev
    stardag config profile use prod

    # Or use environment variable
    export STARDAG_PROFILE=prod
    ```

=== "uv run ..."

    ```bash
    # Create multiple profiles
    uv run stardag config profile add dev -r local -u me@example.com -w my-workspace -e dev
    uv run stardag config profile add prod -r central -u me@company.com -w my-company -e prod

    # Switch between them
    uv run stardag config profile use dev
    uv run stardag config profile use prod

    # Or use environment variable
    export STARDAG_PROFILE=prod
    ```

### CI/CD Setup

```bash
# No interactive login needed - use API key
export STARDAG_REGISTRY_URL=https://api.stardag.com
export STARDAG_API_KEY=sk_...
export STARDAG_WORKSPACE_ID=...
export STARDAG_ENVIRONMENT_ID=...

# Run builds
python my_pipeline.py
```
