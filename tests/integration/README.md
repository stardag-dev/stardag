# Stardag Integration Tests

This package contains integration tests that verify the full system behavior
across API, CLI, SDK, and Frontend components.

## Prerequisites

1. Docker and docker-compose installed
2. Python 3.11+
3. uv package manager

## Running Tests

### Start services (if not already running)

```bash
# From repository root
docker-compose up -d --build
```

### Run all integration tests

```bash
# From repository root
cd tests/integration
uv sync
uv run pytest
```

### Run specific test categories

```bash
# API tests only
uv run pytest test_api_*.py

# CLI tests only
uv run pytest test_cli_*.py

# Skip browser tests
uv run pytest -m "not browser"
```

### Run with browser tests

Browser tests require Playwright:

```bash
# Install browser dependencies
uv sync --extra browser
uv run playwright install chromium

# Run all tests including browser tests
uv run pytest
```

## Test Structure

```
tests/integration/
├── conftest.py           # Shared fixtures (auth, clients, etc.)
├── docker_fixtures.py    # Docker compose management
├── test_api_auth.py      # API authentication tests
├── test_api_endpoints.py # API endpoint access tests
├── test_cli_auth.py      # CLI authentication tests
├── test_cli_config.py    # CLI configuration tests
└── test_frontend/        # Browser-based tests
    ├── conftest.py       # Playwright fixtures
    └── test_login_flow.py
```

## Environment Variables

| Variable          | Default                 | Description                |
| ----------------- | ----------------------- | -------------------------- |
| `STARDAG_API_URL` | `http://localhost:8000` | API service URL            |
| `KEYCLOAK_URL`    | `http://localhost:8080` | Keycloak OIDC provider URL |
| `STARDAG_UI_URL`  | `http://localhost:3000` | Frontend UI URL            |
| `STARDAG_DB_HOST` | `localhost`             | Database host              |
| `STARDAG_DB_PORT` | `5432`                  | Database port              |

## Test User

The tests use a pre-configured test user in Keycloak:

- **Username**: `testuser`
- **Email**: `testuser@localhost`
- **Password**: `testpassword`

## Fixtures

Key fixtures available:

- `docker_services`: Ensures docker-compose is running
- `test_user`: Test user credentials
- `oidc_token`: OIDC token for the test user
- `internal_token`: Org-scoped internal token
- `authenticated_client`: HTTP client with OIDC auth
- `internal_authenticated_client`: HTTP client with internal token auth
- `unauthenticated_client`: HTTP client without auth
- `test_organization_id`: ID of test organization
- `test_workspace_id`: ID of test workspace

## Debugging

To see docker logs when tests fail:

```bash
uv run pytest --capture=no
```

Logs are automatically printed for failed tests when using the
`docker_logs_on_failure` fixture.
