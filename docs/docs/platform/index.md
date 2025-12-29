# Platform

The Stardag Platform provides optional services for team collaboration and monitoring.

## Components

| Component       | Description                | URL               |
| --------------- | -------------------------- | ----------------- |
| **API Service** | REST API for task tracking | `api.stardag.com` |
| **Web UI**      | Dashboard for monitoring   | `app.stardag.com` |

## Do I Need the Platform?

The Stardag SDK works standalone - you don't need the platform for:

- Local development
- Single-user workflows
- Simple automation scripts

The platform adds value for:

- **Team collaboration** - Shared task visibility
- **Build monitoring** - Track long-running pipelines
- **Centralized configuration** - Consistent target roots across team
- **Audit trails** - Build history and status
- **Coordination** - Prevent duplicate builds

## Getting Started

### SaaS (Hosted)

1. Visit [app.stardag.com](https://app.stardag.com)
2. Sign up with GitHub
3. Create an organization and workspace
4. Configure your SDK (see [Quick Start](../getting-started/quickstart.md))

### Self-Hosted

See [Self-Hosting Guide](self-hosting.md).

## In This Section

- **[API Service](api.md)** - REST API documentation
- **[Web UI](ui.md)** - Dashboard features
- **[Self-Hosting](self-hosting.md)** - Deploy your own instance
