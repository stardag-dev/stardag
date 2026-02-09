# CLI Reference

Complete reference for Stardag CLI commands.

## Installation

=== "uv"

    ```sh
    uv add stardag
    ```

=== "pip"

    ```sh
    pip install stardag
    ```

## Global Commands

### Version

=== "Active venv"

    ```sh
    stardag version
    ```

=== "uv run ..."

    ```sh
    uv run stardag version
    ```

Show SDK version.

## Authentication Commands

### Login

=== "Active venv"

    ```sh
    stardag auth login [--registry NAME]
    ```

=== "uv run ..."

    ```sh
    uv run stardag auth login [--registry NAME]
    ```

Opens browser for OAuth authentication.

**Options:**

- `--registry NAME` - Target registry (default: from active profile)

### Status

=== "Active venv"

    ```sh
    stardag auth status
    ```

=== "uv run ..."

    ```sh
    uv run stardag auth status
    ```

Show current authentication status.

### Refresh

=== "Active venv"

    ```sh
    stardag auth refresh
    ```

=== "uv run ..."

    ```sh
    uv run stardag auth refresh
    ```

Refresh access token for current profile.

### Logout

=== "Active venv"

    ```sh
    stardag auth logout
    ```

=== "uv run ..."

    ```sh
    uv run stardag auth logout
    ```

Clear stored credentials.

## Configuration Commands

### Show Configuration

=== "Active venv"

    ```sh
    stardag config show
    ```

=== "uv run ..."

    ```sh
    uv run stardag config show
    ```

Display current configuration and context.

## Registry Management

### List Registries

=== "Active venv"

    ```sh
    stardag config registry list
    ```

=== "uv run ..."

    ```sh
    uv run stardag config registry list
    ```

### Add Registry

=== "Active venv"

    ```sh
    stardag config registry add NAME --url URL
    ```

=== "uv run ..."

    ```sh
    uv run stardag config registry add NAME --url URL
    ```

**Example:**

=== "Active venv"

    ```sh
    stardag config registry add central --url https://api.stardag.com
    ```

=== "uv run ..."

    ```sh
    uv run stardag config registry add central --url https://api.stardag.com
    ```

### Remove Registry

=== "Active venv"

    ```sh
    stardag config registry remove NAME
    ```

=== "uv run ..."

    ```sh
    uv run stardag config registry remove NAME
    ```

## Profile Management

### List Profiles

=== "Active venv"

    ```sh
    stardag config profile list
    ```

=== "uv run ..."

    ```sh
    uv run stardag config profile list
    ```

### Add Profile

=== "Active venv"

    ```sh
    stardag config profile add NAME \
        --registry REGISTRY \
        --user USER \
        --workspace WORKSPACE \
        --environment ENVIRONMENT \
        [--default]
    ```

=== "uv run ..."

    ```sh
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

=== "Active venv"

    ```sh
    stardag config profile add prod \
        -r central \
        -u me@company.com \
        -w my-company \
        -e production \
        --default
    ```

=== "uv run ..."

    ```sh
    uv run stardag config profile add prod \
        -r central \
        -u me@company.com \
        -w my-company \
        -e production \
        --default
    ```

### Use Profile

=== "Active venv"

    ```sh
    stardag config profile use NAME
    ```

=== "uv run ..."

    ```sh
    uv run stardag config profile use NAME
    ```

Set the default profile (also refreshes access token).

### Remove Profile

=== "Active venv"

    ```sh
    stardag config profile remove NAME
    ```

=== "uv run ..."

    ```sh
    uv run stardag config profile remove NAME
    ```

## Workspace & Environment Commands

### List Workspaces

=== "Active venv"

    ```sh
    stardag config list workspaces
    ```

=== "uv run ..."

    ```sh
    uv run stardag config list workspaces
    ```

List workspaces you have access to.

### List Environments

=== "Active venv"

    ```sh
    stardag config list environments
    ```

=== "uv run ..."

    ```sh
    uv run stardag config list environments
    ```

List environments in the active workspace.

## Target Root Commands

### List Target Roots

=== "Active venv"

    ```sh
    stardag config target-roots list
    ```

=== "uv run ..."

    ```sh
    uv run stardag config target-roots list
    ```

Show cached target roots for current environment.

### Sync Target Roots

=== "Active venv"

    ```sh
    stardag config target-roots sync
    ```

=== "uv run ..."

    ```sh
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

=== "Active venv"

    ```sh
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

    ```sh
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

=== "Active venv"

    ```sh
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

    ```sh
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

```sh
# No interactive login needed - use API key
export STARDAG_REGISTRY_URL=https://api.stardag.com
export STARDAG_API_KEY=sk_...
export STARDAG_WORKSPACE_ID=...
export STARDAG_ENVIRONMENT_ID=...

# Run builds
python my_pipeline.py
```
