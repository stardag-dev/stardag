# Web UI

The Stardag Web UI provides a dashboard for monitoring builds and managing your workspace.

## Accessing the UI

| Environment | URL                                        |
| ----------- | ------------------------------------------ |
| SaaS        | [app.stardag.com](https://app.stardag.com) |
| Local dev   | [localhost:5173](http://localhost:5173)    |
| Self-hosted | Your configured domain                     |

## Features

### Build Monitoring

- Real-time build status
- Task execution timeline
- Dependency visualization

### DAG Visualization

Interactive graph showing:

- Task dependencies
- Execution status (pending, running, completed, failed)
- Click tasks for details

### Task Details

- Task parameters
- Input/output paths
- Execution history
- Error logs (for failed tasks)

### Organization Management

- Member management
- Role assignments
- API key generation

### Workspace Settings

- Target root configuration
- Workspace-level API keys
- Access control

## Authentication

The UI uses OAuth for authentication:

1. Click "Sign In"
2. Authenticate with GitHub (or other configured IdP)
3. Select organization and workspace

## Quick Actions

### View Recent Builds

Navigate to Builds to see recent executions.

### Inspect Task

1. Find task in build view or DAG
2. Click to expand details
3. View parameters, output path, status

### Generate API Key

1. Go to Organization Settings
2. Navigate to API Keys
3. Click "Create API Key"
4. Copy and store securely

## Screenshots

<!-- TODO: Add screenshots of key UI features -->

## See Also

- [Platform Overview](index.md)
- [API Service](api.md)
- [Self-Hosting](self-hosting.md)
