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

Target roots are managed under `stardag environment target-roots`. Changes are automatically synced to the local cache.

=== "Active venv"

    ```sh
    stardag environment target-roots list
    stardag environment target-roots add <name> <uri>
    stardag environment target-roots remove <name>
    stardag environment target-roots set <name=uri ...>
    ```

=== "uv run ..."

    ```sh
    uv run stardag environment target-roots list
    uv run stardag environment target-roots add <name> <uri>
    uv run stardag environment target-roots remove <name>
    uv run stardag environment target-roots set <name=uri ...>
    ```

See `stardag environment target-roots --help` for full options (e.g. `--env` to target a specific environment).

## Build Commands

`stardag builds` answers "what does the registry actually think the state
is?" — one build's status and roots, its active plan as a scheduler tick
would see it, and the reasoning behind its recent reactive ticks.

=== "Active venv"

    ```sh
    stardag builds show <build-id> [--json]
    stardag builds frontier <build-id> [--json]
    stardag builds ticks <build-id> [--limit N] [--json]
    stardag builds cancel <build-id> [--yes]
    ```

=== "uv run ..."

    ```sh
    uv run stardag builds show <build-id> [--json]
    uv run stardag builds frontier <build-id> [--json]
    uv run stardag builds ticks <build-id> [--limit N] [--json]
    uv run stardag builds cancel <build-id> [--yes]
    ```

All commands accept `-p/--stardag-profile` and `-e/--stardag-env` to target a
profile / environment other than the active one.

- `builds show` — one build: status, roots, reactive metadata.
- `builds frontier` — the build's active plan as a scheduler tick sees it,
  after the registry's closure step: members to expand (discovery jobs),
  members to claim (runnable), members under a live claim (running) — see
  [Reading the frontier](#reading-the-frontier).
- `builds ticks` — the scheduler's own account of its recent ticks, crashed
  ones included. Reactive builds are driven by many short-lived ticks, each in
  its own container; this is where their reasoning is kept.
- `builds cancel` — release the claims held by every one of the build's
  plans and stop there. **Nothing is stopped**: the released tasks go to
  `CANCELLED` (actionable, so any other build holding them runs them), and
  a worker still running exits at its next cooperative checkpoint, or runs
  to completion if its `run()` has none — its report is recorded as late.

`--json` writes exactly one JSON document to stdout on the read-only
commands; every hint and prompt goes to stderr, so piping is safe:

```sh
stardag builds frontier <build-id> --json | jq '.runnable | length'
```

The document is the SDK's model of the API payload — the same field names
and nesting as the REST response, minus any field this SDK version does
not model.

### Reading the frontier

`builds frontier` is the command for a build that is not progressing. It
shows the active plan's `deployment`, `settings` hash, whether it is
`sealed`, whether the registry considers it `plan_complete`, and three
partitions of its members:

- **Discovery jobs** — members whose instance has never been expanded
  (`requires()` not yet evaluated under this scope). A tick rehydrates
  each one and registers what it finds.
- **Runnable** — members whose upstreams are all `COMPLETED` and whose
  status is actionable (`PENDING`, `SUSPENDED`, `INTERRUPTED`,
  `CANCELLED`, `SKIPPED`, or `RUNNING` with a lapsed claim).
- **Running** — members currently `RUNNING` under a live claim, whoever
  holds it. Task state is global to the environment, so this can be a
  claim another build took out on a shared task; this build's next tick
  waits for it to move rather than treating it as a problem (see [Build &
  Execution](../concepts/build-execution.md#shared-tasks-across-builds)).

An empty frontier with the build still `running` and no discovery jobs
either means the plan is not yet sealed, or every member is settled —
`plan_complete` distinguishes the two.

## Environment Variables

All CLI behavior can be overridden with environment variables:

| Variable                     | Description                   |
| ---------------------------- | ----------------------------- |
| `STARDAG_PROFILE`            | Active profile name           |
| `STARDAG_API_URL`            | Registry API URL              |
| `STARDAG_API_KEY`            | API key (bypasses OAuth)      |
| `STARDAG_WORKSPACE_ID`       | Workspace UUID                |
| `STARDAG_ENVIRONMENT_ID`     | Environment UUID              |
| `STARDAG_TARGET_ROOTS`       | JSON string of target roots   |
| `STARDAG_TARGET_ROOTS__NAME` | Specific target root override |

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
export STARDAG_API_URL=https://api.stardag.com
export STARDAG_API_KEY=sk_...
export STARDAG_WORKSPACE_ID=...
export STARDAG_ENVIRONMENT_ID=...

# Run builds
python my_pipeline.py
```
