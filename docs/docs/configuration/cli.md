# CLI Reference

Complete reference for Stardag CLI commands.

## Installation

```bash
pip install stardag
```

## Global Commands

### Version

```bash
stardag version
```

Show SDK version.

## Authentication Commands

### Login

```bash
stardag auth login [--registry NAME]
```

Opens browser for OAuth authentication.

**Options:**

- `--registry NAME` - Target registry (default: from active profile)

### Status

```bash
stardag auth status
```

Show current authentication status.

### Refresh

```bash
stardag auth refresh
```

Refresh access token for current profile.

### Logout

```bash
stardag auth logout
```

Clear stored credentials.

## Configuration Commands

### Show Configuration

```bash
stardag config show
```

Display current configuration and context.

## Registry Management

### List Registries

```bash
stardag config registry list
```

### Add Registry

```bash
stardag config registry add NAME --url URL
```

**Example:**

```bash
stardag config registry add central --url https://api.stardag.com
```

### Remove Registry

```bash
stardag config registry remove NAME
```

## Profile Management

### List Profiles

```bash
stardag config profile list
```

### Add Profile

```bash
stardag config profile add NAME \
    --registry REGISTRY \
    --organization ORG \
    --workspace WORKSPACE \
    [--default]
```

**Options:**

- `-r, --registry` - Registry name
- `-o, --organization` - Organization slug
- `-w, --workspace` - Workspace slug
- `--default` - Set as default profile

**Example:**

```bash
stardag config profile add prod \
    -r central \
    -o my-company \
    -w production \
    --default
```

### Use Profile

```bash
stardag config profile use NAME
```

Set the default profile (also refreshes access token).

### Remove Profile

```bash
stardag config profile remove NAME
```

## Organization & Workspace Commands

### List Organizations

```bash
stardag config list organizations
```

List organizations you have access to.

### List Workspaces

```bash
stardag config list workspaces
```

List workspaces in current organization.

## Target Root Commands

### List Target Roots

```bash
stardag config target-roots list
```

Show cached target roots for current workspace.

### Sync Target Roots

```bash
stardag config target-roots sync
```

Fetch latest target roots from server.

## Environment Variables

All CLI behavior can be overridden with environment variables:

| Variable                    | Description                   |
| --------------------------- | ----------------------------- |
| `STARDAG_PROFILE`           | Active profile name           |
| `STARDAG_REGISTRY_URL`      | Registry API URL              |
| `STARDAG_API_KEY`           | API key (bypasses OAuth)      |
| `STARDAG_ORGANIZATION_ID`   | Organization UUID             |
| `STARDAG_WORKSPACE_ID`      | Workspace UUID                |
| `STARDAG_TARGET_ROOTS`      | JSON string of target roots   |
| `STARDAG_TARGET_ROOT__NAME` | Specific target root override |

## Common Workflows

### Initial Setup

```bash
# Add registry
stardag config registry add local --url http://localhost:8000

# Login
stardag auth login --registry local

# Create profile
stardag config profile add dev \
    -r local \
    -o my-org \
    -w development \
    --default

# Verify
stardag config show
```

### Switch Environments

```bash
# Create multiple profiles
stardag config profile add dev -r local -o my-org -w dev
stardag config profile add prod -r central -o my-org -w prod

# Switch between them
stardag config profile use dev
stardag config profile use prod

# Or use environment variable
export STARDAG_PROFILE=prod
```

### CI/CD Setup

```bash
# No interactive login needed - use API key
export STARDAG_REGISTRY_URL=https://api.stardag.com
export STARDAG_API_KEY=sk_...
export STARDAG_WORKSPACE_ID=...

# Run builds
python my_pipeline.py
```
