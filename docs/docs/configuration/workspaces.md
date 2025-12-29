# Workspaces

Workspaces organize tasks and builds within an organization.

## What is a Workspace?

A workspace is a logical grouping that provides:

- Isolated task and build history
- Separate target root configurations
- Dedicated API keys for CI/CD
- Team-level access control

## Common Patterns

### By Project

```
Organization: my-company
├── Workspace: ml-pipeline
├── Workspace: data-etl
└── Workspace: analytics-dashboard
```

### By Stage

```
Organization: my-company
├── Workspace: project-dev
├── Workspace: project-staging
└── Workspace: project-prod
```

### Personal Workspaces

Auto-created `personal-{username}` workspace for individual work:

```
Organization: my-company
├── Workspace: team-project
├── Workspace: personal-alice
└── Workspace: personal-bob
```

## Managing Workspaces

### List Workspaces

```bash
stardag config list workspaces
```

### Create Workspace

Workspaces are created via the web UI or API.

<!-- TODO: Document workspace creation -->

## Target Roots

Each workspace has its own target root configuration:

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

## Accessing Workspace Data

### List Organizations

```bash
stardag config list organizations
```

### Current Context

```bash
stardag config show
```

## Best Practices

1. **Separate by concern** - Different projects get different workspaces
2. **Use naming conventions** - `{project}-{stage}` or similar
3. **Consistent target roots** - Define once, use everywhere
4. **Personal workspaces for experiments** - Keep production clean
