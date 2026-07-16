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

#### Live Modal tests

Modal integration tests come in two tiers. The unit tier (default) uses fakes
and needs no credentials. Modules marked `modal_live` hit a real Modal
workspace (deploy test apps, create volumes, run containers):

```bash
cd lib/stardag

# Unit tier only
uv run pytest tests/test_integration/test_modal -m "not modal_live"

# Live tier — requires Modal credentials; use a personal/dev profile!
STARDAG_MODAL_TEST_PROFILE=<your-dev-profile> \
  uv run pytest tests/test_integration/test_modal -m modal_live
```

Gating (see `stardag.testing.modal.live_modal_guard`):

- `STARDAG_MODAL_LIVE_TESTS`: `auto` (default: run if authenticated, else
  skip), `1` (require: fail instead of skip — for CI), `0` (always skip).
- `STARDAG_MODAL_TEST_PROFILE`: if set, live tests are skipped unless the
  active Modal profile matches. Recommended to always set this locally, so
  live tests can never run against a shared/production workspace by accident.

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

## Releasing the Server

The server (Registry API + web UI) is released as one image with its own
semver, independent of the SDK. API and UI share a single joint version.
The image definition is `app/server.Dockerfile` (build context = repo root):

```bash
docker build -f app/server.Dockerfile -t stardag-server .
```

To release, push a `server-vX.Y.Z` tag on `main`:

```bash
git tag server-vX.Y.Z
git push origin server-vX.Y.Z
```

CI (`.github/workflows/publish-server-image.yml`) then:

1. Builds the image and pushes it to
   `ghcr.io/stardag-dev/stardag-server:X.Y.Z` and `:latest`, with
   `STARDAG_SERVER_VERSION=X.Y.Z` baked in (surfaced at
   `GET /api/v1/version`).
2. Creates a GitHub Release for the tag with the built web UI attached as
   `stardag-ui-dist-X.Y.Z.tar.gz` (for deployments that serve the UI
   separately, e.g. from S3/CDN).

`stardag self-host` deploys the prebuilt image by default; each SDK release
pins the server version it was tested against
(`DEFAULT_SERVER_VERSION` in `lib/stardag/src/stardag/selfhost/_modal_app.py`
— bump it when a new server version becomes the tested pairing).

### Version convention for non-release builds

Release builds get a clean `X.Y.Z` from the tag (CI passes it as the
`STARDAG_SERVER_VERSION` build arg). Any _other_ build of
`app/server.Dockerfile` (e.g. a deployment pipeline building from an
arbitrary commit) should derive the version with `scripts/server-version.sh`,
which normalizes `git describe --tags --match "server-v*"` to semver
build-metadata form — so deployments truthfully report their deviation from
the nearest release:

| State                          | Version          |
| ------------------------------ | ---------------- |
| Exactly at `server-vX.Y.Z`     | `X.Y.Z`          |
| N commits past the nearest tag | `X.Y.Z+N.g<sha>` |
| No `server-v*` tag reachable   | `0.0.0+g<sha>`   |

```bash
docker build -f app/server.Dockerfile \
  --build-arg STARDAG_SERVER_VERSION="$(scripts/server-version.sh)" .
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on submitting changes.
