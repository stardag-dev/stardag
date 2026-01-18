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
- Team-level access control

## Common Patterns

### By Project

```
Workspace: my-company
├── Environment: ml-pipeline
├── Environment: data-etl
└── Environment: analytics-dashboard
```

### By Stage

```
Workspace: my-company
├── Environment: project-dev
├── Environment: project-staging
└── Environment: project-prod
```

### Personal Environments

Auto-created `personal-{username}` environment for individual work:

```
Workspace: my-company
├── Environment: team-project
├── Environment: personal-alice
└── Environment: personal-bob
```

## Managing Workspaces & Environments

### List Workspaces

```bash
stardag config list workspaces
```

### List Environments

```bash
stardag config list environments
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

### Synchronizing Target Roots

```bash
# Fetch latest from server
stardag config target-roots sync

# View current configuration
stardag config target-roots list
```

### Why Sync?

Target roots are defined centrally to ensure all team members use consistent paths. The SDK:

1. Fetches roots on login/sync
2. Caches locally for offline access
3. Validates against server on builds
4. Auto-syncs new roots
5. Requires explicit sync for modifications

## Current Context

```bash
stardag config show
```

## Best Practices

1. **Separate by concern** - Different projects get different environments
2. **Use naming conventions** - `{project}-{stage}` or similar
3. **Consistent target roots** - Define once, use everywhere
4. **Personal environments for experiments** - Keep production clean
