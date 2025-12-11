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

This project uses [Alembic](https://alembic.sqlalchemy.org/) for database migrations with plain SQL files.

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
# Create a new migration
uv run alembic revision -m "description of change"
```

This creates two files in `migrations/versions/`:

- `<revision>_<slug>.py` - Python migration script
- `<revision>_<slug>.sql` - SQL statements (edit this file)

Write your SQL statements in the `.sql` file. The Python script automatically reads and executes the SQL.

### Migration Guidelines

- Write migrations in plain SQL for PostgreSQL
- Keep migrations atomic and reversible when possible
- Test migrations locally before committing

## Docker

```bash
# Run with docker-compose (from repo root)
docker-compose up

# The migrations service runs automatically before the API starts
```

## Configuration

Environment variables (prefix: `STARDAG_API_`):

| Variable       | Default                                                       | Description             |
| -------------- | ------------------------------------------------------------- | ----------------------- |
| `DATABASE_URL` | `postgresql+asyncpg://stardag:stardag@localhost:5432/stardag` | Database connection URL |
| `DEBUG`        | `false`                                                       | Enable debug mode       |
