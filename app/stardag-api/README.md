# Stardag API

FastAPI backend for task tracking and monitoring.

## Development

```bash
# Install dependencies
uv sync --group dev

# Run locally (requires PostgreSQL)
uv run uvicorn stardag_api.main:app --reload

# Run tests
uv run pytest
```

## Database Migrations

This project uses [Alembic](https://alembic.sqlalchemy.org/) for database migrations.

### Running Migrations

```bash
# Apply all migrations
uv run alembic upgrade head

# Rollback one migration
uv run alembic downgrade -1

# View current revision
uv run alembic current

# View migration history
uv run alembic history
```

### Creating Migrations

```bash
# Auto-generate migration from model changes (requires running database)
uv run alembic revision --autogenerate -m "description of change"

# Create empty migration (for manual edits)
uv run alembic revision -m "description of change"
```

## Docker

```bash
# Run with docker-compose (from repo root)
docker-compose up

# The migrations service runs automatically before the API starts

# Reset database (useful during development when modifying migrations)
docker-compose down -v  # -v removes the postgres_data volume
docker-compose up
```

## Configuration

Environment variables (prefix: `STARDAG_API_`):

| Variable       | Default                                                       | Description             |
| -------------- | ------------------------------------------------------------- | ----------------------- |
| `DATABASE_URL` | `postgresql+asyncpg://stardag:stardag@localhost:5432/stardag` | Database connection URL |
| `DEBUG`        | `false`                                                       | Enable debug mode       |
