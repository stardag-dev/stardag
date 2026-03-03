# Stardag

Declarative and composable DAG framework for Python with persistent asset management.

## Project Overview

Stardag is a Python framework for building DAGs (Directed Acyclic Graphs) with:

- **Composability**: Task instances as first-class composable units
- **Type safety**: Pydantic-based tasks with full serialization
- **Bottom-up execution**: Build only what's needed (Makefile-style)
- **Deterministic paths**: Output locations based on parameter hashes

## Tech Stack

- Python 3.10+
- Pydantic for task models and validation
- Optional integrations: Prefect, Modal, AWS S3

## Project Structure

```
lib/
├── stardag/                          # Core SDK library (PyPI: stardag)
│   ├── src/stardag/
│   │   ├── __init__.py               # Public API exports
│   │   ├── _core/                    # Core task framework
│   │   │   ├── base_task.py          # BaseTask, LoadableTask, TargetTask
│   │   │   ├── task.py               # Task (auto filesystem targets, diamond)
│   │   │   ├── decorator.py          # @task decorator, Depends
│   │   │   ├── task_loads.py         # TaskLoads type alias
│   │   │   ├── alias_task.py         # AliasTask for referencing remote outputs
│   │   │   ├── hashable_set.py       # HashableSet for set-valued parameters
│   │   │   ├── target_base.py        # Target/TargetType base protocol
│   │   │   └── task_id.py            # Deterministic UUID-5 task ID logic
│   │   ├── build/                    # Build/execution engine
│   │   │   ├── _base.py              # BuildSummary, TaskExecutorABC
│   │   │   ├── _concurrent.py        # Concurrent executor (asyncio)
│   │   │   └── _sequential.py        # Sequential executor
│   │   ├── target/                   # Target abstraction (persistence layer)
│   │   │   ├── _base.py              # FileSystemTarget, LoadableTarget, etc.
│   │   │   ├── _factory.py           # Target factory + provider
│   │   │   ├── _in_memory.py         # InMemoryTarget (for testing)
│   │   │   └── serialize.py          # Serializers (JSON, pickle, pandas, etc.)
│   │   ├── registry/                 # Stardag API registry client
│   │   │   ├── _api_registry.py      # APIRegistry (HTTP client)
│   │   │   ├── _base.py              # RegistryABC, NoOpRegistry
│   │   │   ├── _http_client.py       # Authenticated HTTP client
│   │   │   └── _lock.py              # Distributed lock via registry
│   │   ├── integration/              # Optional third-party integrations
│   │   │   ├── aws/s3.py             # S3 target factory
│   │   │   ├── modal/                # Modal.com executor
│   │   │   └── prefect/              # Prefect orchestrator
│   │   ├── _cli/                     # CLI commands (stardag auth, config, etc.)
│   │   ├── polymorphic.py            # Polymorphic validation (SubClass, Polymorphic)
│   │   ├── base_model.py             # StardagBaseModel, StardagField
│   │   ├── config.py                 # Configuration (profiles, target roots)
│   │   ├── artifact.py               # Artifact types (Markdown, JSON)
│   │   └── exceptions.py             # StardagError hierarchy
│   └── tests/
│       ├── test__core/               # Core framework tests
│       ├── test_build/               # Build engine tests
│       ├── test_target/              # Target/serialization tests
│       └── test_integration/         # Integration tests (Modal, Prefect)
│
└── stardag-examples/                 # Example DAGs and demos
    └── src/stardag_examples/
        ├── general/                  # General examples (three API levels, artifacts)
        ├── ml_pipeline/              # ML pipeline (class API + decorator API)
        ├── modal/                    # Modal.com examples
        └── prefect/                  # Prefect orchestrator example

app/
├── stardag-api/                      # FastAPI backend (task registry service)
│   ├── src/stardag_api/
│   │   ├── main.py                   # FastAPI app entrypoint
│   │   ├── routes/                   # API endpoints (tasks, builds, locks, auth)
│   │   ├── models/                   # SQLAlchemy models
│   │   ├── services/                 # Business logic (locks, API keys, email)
│   │   ├── auth/                     # JWT/token auth (Keycloak OIDC)
│   │   ├── schemas.py                # Pydantic request/response schemas
│   │   └── config.py                 # API configuration
│   ├── migrations/                   # Alembic DB migrations
│   └── tests/
│
└── stardag-ui/                       # React frontend (task explorer/monitoring)
    └── src/
        ├── components/               # UI components (TaskExplorer, DagGraph, etc.)
        ├── api/                      # API client (tasks, auth, workspaces)
        ├── context/                  # React contexts (Auth, Theme, Environment)
        └── hooks/                    # Custom hooks (useTasks)

docs/                                 # MkDocs documentation site
    └── docs/
        ├── getting-started/          # Installation, quickstart, tutorials
        ├── concepts/                 # Tasks, dependencies, parameters, build
        ├── how-to/                   # Guides (storage, integrations, ML pipeline)
        ├── platform/                 # Registry API & UI docs
        └── reference/                # Auto-generated API reference

infra/aws-cdk/                        # Generic AWS CDK infrastructure templates
integration-tests/                    # End-to-end tests (API + UI + docker-compose)
```

## Development

See [DEV_README.md](/DEV_README.md) for setup and commands.

## Testing

- **Unit tests**: Prefer unit tests for testing specific components and logic
- **E2E test**: `./scripts/e2e-test.sh` runs a full integration test (docker-compose, API, UI, demo script). Use sparingly - it's slow and should not replace unit tests. Good for verifying the full stack works together after significant changes.

## Code Style

- Use type annotations throughout
- Follow existing Pydantic patterns for task definitions

## Database Migrations (stardag-api)

**IMPORTANT**: Always use Alembic's autogenerate command - never manually create migration files.

### Creating a New Migration

1. **Update the SQLAlchemy models** in `app/stardag-api/src/stardag_api/models/`

2. **Generate the migration** using Alembic autogenerate:

   ```bash
   cd app/stardag-api
   uv run alembic revision --autogenerate -m "description of changes"
   ```

   This creates a file like `abc123def456_description_of_changes.py` with a proper hash-based revision ID.

3. **Review the generated migration** - Alembic's autogenerate is not perfect:

   - Check that all intended changes are captured
   - Verify `upgrade()` and `downgrade()` are correct inverses
   - Remove any unintended changes

4. **Test the migration** locally:
   ```bash
   uv run alembic upgrade head    # Apply
   uv run alembic downgrade -1    # Rollback
   uv run alembic upgrade head    # Re-apply
   ```

### What NOT to Do

- **Don't** create migration files manually or copy/paste from other migrations
- **Don't** use custom revision IDs (like `001_foo`, `002_bar`, timestamps, etc.)
- **Don't** modify the `revision` or `down_revision` values in migration files
- **Don't** rename migration files after creation

### Why This Matters

Alembic tracks migrations by revision ID in the `alembic_version` table. Hand-crafted IDs can cause:

- Chain breaks when IDs don't follow the proper DAG
- "Relation already exists" errors on fresh deployments
- Confusion about which migrations have been applied

## Learnings

This section captures project-specific corrections and preferences. When the user corrects how something should be done or you discover something new about this project, add it here.

**Format for new learnings:**

- **Do**: [What should be done]
- **Don't**: [What was incorrectly assumed or should be avoided]
- **Context**: [Why, if relevant]

### Learnings Log

1. **Run pre-commit hooks after editing files**

   - **Do**: Run relevant pre-commit hooks after editing files (e.g., `prettier` for markdown, `ruff` and `pyright` for Python)
   - **Don't**: Leave files in a non-compliant state
   - **Context**: Project uses prettier for markdown formatting, ruff and pyright for Python linting/formatting and typechecks

2. **Use Alembic autogenerate for database migrations**
   - **Do**: Always run `alembic revision --autogenerate -m "description"` to create migrations
   - **Don't**: Manually create migration files or use custom revision IDs
   - **Context**: Hand-crafted migrations caused deployment failures due to inconsistent revision IDs breaking the migration chain
