# Development Guide

## Project Structure

```
lib/
├── stardag/           # Core SDK library
└── stardag-examples/  # Example DAGs and demos

app/
├── stardag-api/       # FastAPI backend for task tracking
└── stardag-ui/        # React frontend for monitoring
```

## Quick Start

### Install all packages

```bash
./scripts/install.sh
```

Or manually:

```bash
# Install each Python package (creates separate .venv per package)
cd lib/stardag && uv sync --all-extras && cd ../..
cd lib/stardag-examples && uv sync --all-extras && cd ../..
cd app/stardag-api && uv sync --all-extras && cd ../..

# Install frontend
cd app/stardag-ui && npm install && cd ../..

# Install root workspace (for dev)
uv sync --all-extras
```

### Run all tests

```bash
./scripts/test.sh
```

Or via tox:

```bash
tox -e stardag-py311,stardag-examples-py311,stardag-api-py311,stardag-ui
```

## Running the Full Stack

```bash
docker compose up -d
```

This starts:

- PostgreSQL database on port 5432
- API service on port 8000
- Web UI on port 3000

Then run a DAG with API registry:

```bash
export STARDAG_API_REGISTRY_URL=http://localhost:8000
python -m stardag_examples.api_registry_demo
```

View tasks at http://localhost:3000

## Development Commands

### Testing

```bash
# Test specific package
tox -e stardag-py311
tox -e stardag-examples-py311
tox -e stardag-api-py311
tox -e stardag-ui

# Run all Python tests
tox -e stardag-py311,stardag-examples-py311,stardag-api-py311
```

### Linting & Formatting

```bash
tox -e pre-commit
```

### Type Checking (pyright)

```bash
# Type check specific package
tox -e stardag-pyright
tox -e stardag-examples-pyright
tox -e stardag-api-pyright
```

Note: pyright currently has pre-existing errors and is excluded from CI.

### Full CI Check

```bash
tox
```

## Frontend Development

```bash
cd app/stardag-ui
npm run dev      # Start dev server (port 5173)
npm test         # Run tests
npm run build    # Production build
```

The dev server proxies `/api` to `http://localhost:8000`.

## Authentication for Local Development

When developing locally against the docker compose stack, you need to authenticate the SDK with the API service.

### Setup

1. Start the full stack (includes Keycloak identity provider):

```bash
docker compose up -d
```

2. Access the web UI at http://localhost:3000 and create an account or log in.

3. Install the CLI:

```bash
cd lib/stardag
uv sync --extra cli
```

### Authentication Methods

**Method 1: Browser Login (recommended for interactive development)**

```bash
uv run stardag auth login
```

This opens your browser to Keycloak (http://localhost:8080). After login, tokens are stored in `~/.stardag/credentials.json`.

Check your auth status:

```bash
uv run stardag auth status
```

**Method 2: API Key (for scripts/automation)**

1. Log in to the web UI at http://localhost:3000
2. Go to Organization Settings > API Keys
3. Create a new API key for your workspace
4. Set the environment variable:

```bash
export STARDAG_API_KEY=sk_your_key_here
```

### Sanity Check

After authentication, verify the setup works:

```bash
# Check auth status
uv run stardag auth status

# Run the demo script to test API registry integration
cd lib/stardag-examples
export STARDAG_API_URL=http://localhost:8000
uv run python -m stardag_examples.api_registry_demo
```

You should see tasks appearing in the web UI at http://localhost:3000.

### Logout

```bash
uv run stardag auth logout
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on submitting changes.
