# Workspaces & Environments

Workspaces and environments organize tasks and builds hierarchically.

## Conceptual Model

```
Workspace (team/company)
  └── Environment (project/stage)
        └── Target Roots (storage locations)
```

### Workspaces

A **workspace** represents a team or company that shares access to environments:

- Users can belong to multiple workspaces
- Each workspace has members with roles (owner, admin, member)
- Workspaces contain environments

### Environments

An **environment** is a logical grouping within a workspace that provides:

- Isolated task and build history
- Separate target root configurations
- Dedicated API keys for CI/CD

## Common Patterns

### By Stage

```
Workspace: my-company
├── Environment: main (or prod)
├── Environment: dev
└── Environment: staging
```

### By Project

!!! tip "Avoid too granular environments"

    Only split environments by projects if your projects are truly independent. If you think you might reference tasks in one project from another project, they should be in the *same* environment.

```
Workspace: my-company
├── Environment: ml-pipeline_main
├── Environment: ml-pipeline_dev
├── Environment: analytics-dashboard_main
└── Environment: analytics-dashboard_dev
```

### Personal Environments

Auto-created `personal-{username}` environment for individual work:

```
Workspace: my-company
├── Environment: main
├── Environment: dev
├── Environment: personal-alice
└── Environment: personal-bob
```

## Managing Workspaces & Environments

### List Workspaces

=== "Active venv"

    ```sh
    stardag config list workspaces
    ```

=== "uv run ..."

    ```sh
    uv run stardag config list workspaces
    ```

### List Environments

=== "Active venv"

    ```sh
    stardag config list environments
    ```

=== "uv run ..."

    ```sh
    uv run stardag config list environments
    ```

Lists environments in the active workspace.

### Create Environment

Environments are created via the web UI or API.

## Target Roots

Each environment has its own target root configuration:

| Name      | URI Prefix                   | Purpose           |
| --------- | ---------------------------- | ----------------- |
| `default` | `s3://company/stardag/prod/` | Primary storage   |
| `archive` | `s3://company/archive/`      | Long-term storage |

!!! info "Local paths with ~/"

    Target root paths starting with `~/` are automatically expanded to the user's home directory. They mostly make sense for personal/local environments.

### Managing Target Roots

Target roots are managed via the `stardag environment target-roots` commands. All mutations automatically sync the local cache.

=== "Active venv"

    ```sh
    # View current target roots
    stardag environment target-roots list

    # Add, remove, or set target roots
    stardag environment target-roots add <name> <uri>
    stardag environment target-roots remove <name>
    stardag environment target-roots set default=s3://bucket/outputs
    ```

=== "uv run ..."

    ```sh
    uv run stardag environment target-roots list
    uv run stardag environment target-roots add <name> <uri>
    uv run stardag environment target-roots remove <name>
    uv run stardag environment target-roots set default=s3://bucket/outputs
    ```

### Why Central Target Roots?

Target roots are defined centrally to ensure all team members use consistent paths. The SDK:

1. Caches roots locally for offline access
2. Auto-syncs the cache on every target root change
3. Validates against server on builds

## Current Context

=== "Active venv"

    ```sh
    stardag config show
    ```

=== "uv run ..."

    ```sh
    uv run stardag config show
    ```

## Best Practices

1. **Separate by concern** - Different projects get different environments
2. **Use naming conventions** - `{project}-{stage}` or similar
3. **Consistent target roots** - Define once, use everywhere
4. **Personal environments for experiments** - Keep production clean
