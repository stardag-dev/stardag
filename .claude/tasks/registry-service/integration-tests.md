## Status

completed

## Goal

Get automated end-to-end testing of all critical user journeys. Including the interaction between frontend (`app/stardag-ui`), backend (`app/stardag-api`), auth/OIDC provider, and the SDK (`lib/stardag`), used as a library as well as the CLI.

Implement tests such that all relevant output/logs are captured and printed out when a test is failing. This is to facilitate AI-Agent based coding/self-verification.

## Summary

Integration tests are now fully implemented and running in CI. The test suite covers:

- **API authentication**: Token exchange, token type validation, API key auth
- **CLI functionality**: All CLI commands (auth, config, registry, profile management)
- **SDK build workflows**: Simple DAG and diamond DAG tests using APIRegistry
- **Full flow tests**: End-to-end build workflows, cross-component validation
- **Browser tests** (optional): Basic Playwright tests for UI flows

### Test Results

| Test File         | Tests | Status  | Notes                              |
| ----------------- | ----- | ------- | ---------------------------------- |
| test_smoke.py     | 11    | Passing | Service health, fixture validation |
| test_api_auth.py  | 15    | Passing | Token validation, API key auth     |
| test_full_flow.py | 12    | Passing | Build workflows, SDK integration   |
| test_cli.py       | 16    | Passing | CLI commands with docker services  |
| test_browser.py   | 10    | Skipped | Optional, separate CI job          |

### Package Structure

```
integration-tests/
├── pyproject.toml          # Package config with stardag[cli,api] dependency
├── pyrightconfig.json      # Type checking config (excludes browser tests)
├── src/
│   └── stardag_integration_tests/
│       ├── __init__.py
│       ├── conftest.py     # Re-exports fixtures for pytest
│       └── docker_fixtures.py  # Docker service management
└── tests/
    ├── conftest.py         # Test fixtures (auth, org, workspace)
    ├── test_smoke.py       # Basic health checks
    ├── test_api_auth.py    # API authentication tests
    ├── test_full_flow.py   # End-to-end workflow tests
    ├── test_cli.py         # CLI integration tests
    └── test_browser.py     # Playwright browser tests (optional)
```

### CI Integration

Two CI jobs added to `.github/workflows/ci.yml`:

1. **integration-tests**: Runs all non-browser tests via `tox -e integration`
2. **integration-tests-browser**: Runs browser tests via `tox -e integration-browser`

Both jobs:

- Build and start docker-compose services
- Wait for Keycloak (OIDC discovery endpoint) and API health
- Run tests with proper fixtures
- Show docker logs on failure

## Implementation Details

### Phase 1: Test Infrastructure (COMPLETED)

- Created separate `integration-tests/` package following project conventions
- Docker fixtures manage service lifecycle with health checks
- Auth fixtures provide OIDC tokens, internal tokens, and API keys
- Test user fixture leverages Keycloak password grant for automation
- Tox environments: `integration`, `integration-browser`, `integration-pyright`

### Phase 2: API Integration Tests (COMPLETED)

File: `tests/test_api_auth.py`

- [x] `/api/v1/auth/exchange` accepts only OIDC tokens
- [x] `/api/v1/auth/exchange` rejects internal tokens (401)
- [x] `/api/v1/auth/exchange` rejects invalid tokens (401)
- [x] `/api/v1/auth/exchange` requires org_id (422)
- [x] `/api/v1/builds` rejects OIDC tokens (401)
- [x] `/api/v1/builds` accepts internal tokens
- [x] `/api/v1/tasks` rejects OIDC tokens (401)
- [x] `/api/v1/tasks` accepts internal tokens
- [x] `/api/v1/ui/me` accepts OIDC tokens (bootstrap endpoint)
- [x] `/api/v1/ui/me` accepts internal tokens
- [x] API key creation works
- [x] API key auth works for POST /builds
- [x] API key auth works for GET /builds
- [x] API key auth works for GET /tasks
- [x] Invalid API keys rejected (401)

File: `tests/test_api_auth.py` (TestEndpointAccess class)

- [x] All protected endpoints return 401 without auth
- [x] Router prefixes verified (/health, /api/v1/auth, /api/v1/ui, etc.)
- [x] Wrong org/workspace returns 403 or 404

### Phase 3: CLI Integration Tests (COMPLETED)

File: `tests/test_cli.py`

- [x] `stardag version` works
- [x] `stardag --help` shows commands
- [x] `stardag auth --help` shows auth subcommands
- [x] `stardag config --help` shows config subcommands
- [x] `stardag config show` works without profile
- [x] `stardag auth status` works without credentials
- [x] `stardag config show` shows API key when set via env
- [x] `stardag auth status` shows API key status
- [x] Registry URL from env respected
- [x] Config with multiple env vars works
- [x] `stardag config registry list/add/remove` works
- [x] `stardag config profile list` works
- [x] `stardag config profile add` requires valid registry
- [x] `stardag auth login` with API key set shows message
- [x] `stardag auth logout` without credentials works

**Note**: Full OAuth browser flow (`stardag auth login`) not fully tested due to browser interaction requirement. Tests verify the CLI handles various auth states correctly.

### Phase 4: Frontend Browser Tests (PARTIALLY COMPLETED - OPTIONAL)

File: `tests/test_browser.py`

Browser tests exist but are optional (in separate CI job):

- [x] Login page loads
- [x] OAuth redirect to Keycloak works
- [x] After login, redirected back with user info
- [x] Organization selector displayed
- [x] Workspace switching works
- [x] Builds page loads with data
- [x] Tasks page loads
- [x] API error handling in UI

**Remaining TODOs** (nice-to-have):

- [ ] Test logout flow
- [ ] Test token refresh (session expiry)
- [ ] Test deep linking (direct URL access)

### Phase 5: Full Flow Integration Tests (COMPLETED)

File: `tests/test_full_flow.py`

- [x] Create build and verify in list
- [x] Create build with tasks via API key
- [x] Complete build workflow (create → complete → verify status)
- [x] API key workflow (full CRUD)
- [x] Organization info accessible after auth
- [x] Workspace listing works
- [x] OIDC → internal token exchange
- [x] API key created via UI works for SDK
- [x] Builds visible across auth methods

SDK Build Tests:

- [x] Simple 3-task DAG build with APIRegistry
- [x] Diamond DAG build (shared dependencies)
- [x] All tasks registered and marked completed
- [x] Graph structure verified (nodes and edges)

## Known Issues / Remaining TODOs

### Non-Critical (Nice-to-Have)

1. **CLI auth login browser flow**: The interactive OAuth flow requires browser interaction. Tests verify the CLI handles auth states but don't fully automate the browser redirect flow. Could add Playwright-based CLI login test if needed.

2. **Browser test coverage**: Basic flows covered, but could expand to cover:

   - Logout flow
   - Token refresh handling
   - Error states (network failures, etc.)

3. **Profile slug resolution tests**: The CLI supports slugs (e.g., `my-org/my-workspace`) but integration tests primarily use UUIDs. Could add tests verifying slug → UUID resolution works end-to-end.

4. **Parallel test isolation**: Currently tests share org/workspace created by first test. For true isolation, could create unique workspace per test, but this adds overhead.

### Fixed Issues

- Keycloak health check in CI (was checking wrong port)
- CLI missing `[cli,api]` extras in integration-tests
- GET /builds/{build_id} missing workspace_id parameter
- CLI tests running from wrong directory

## Decisions Made

1. **Separate integration-tests package**: Follows same structure as other packages (`src/`, `tests/`), clear separation from unit tests.

2. **Browser tests optional**: Marked as separate CI job since they're slow and require Playwright installation. Non-browser tests run in main integration job.

3. **Password grant for test automation**: Using Keycloak `stardag-test` client with password grant (direct access) for automated token acquisition. Avoids browser interaction in most tests.

4. **Real PostgreSQL**: Tests run against real PostgreSQL in docker, catches DB-specific issues.

5. **Service reuse**: Docker services left running between tests for speed. Fixtures detect and reuse existing org/workspace.

## Running Tests Locally

```bash
# Start docker services
cd docker && docker compose up -d

# Wait for services
# ... (or use scripts/e2e-test.sh for full setup)

# Run integration tests
tox -e integration

# Run with browser tests (requires playwright install)
tox -e integration-browser

# Run specific test file
cd integration-tests && uv run pytest tests/test_api_auth.py -v
```

## Files Changed

- `integration-tests/` - New package with all integration tests
- `tox.ini` - Added integration, integration-browser, integration-pyright envs
- `.github/workflows/ci.yml` - Added integration-tests and integration-tests-browser jobs
- `docker-compose.yml` - No changes needed (already had all services)
- `docker/keycloak/realm-export.json` - Added stardag-test client for password grant
