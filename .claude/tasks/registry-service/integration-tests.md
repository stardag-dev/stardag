## Status

ready

## Goal

Get automated end-to-end testing of all critical user journeys. Including the interaction between frontend (`app/stardag-ui`), backend (`app/stardag-api`), auth/OIDC provider, and the SDK (`lib/stardag`), used as a library as well as the CLI.

Implement tests such that all relevant output/logs are captured and printed out when a test is failing. This is to facilitate AI-Agent based coding/self-verification.

## Instructions

Make a detailed plan for how to get close to complete test coverage. Identify the biggest gaps, categorize them and turn them into an implementation plan.

Some guidelines:

- Priority to add coverage to each components _independent unit tests_, but end-to-end integration tests are a necessary complement.
- For integration testing we should rely on a/the docker-compose setup primarily.
- Integration tests should be kept in a separate _python package_, and to the extent possible we should use _Python_ and standard pytest conventions to script the integration tests. We can for example write fixtures to bring up/down docker compose and seed the DB etc.
- We will need to to do _browser based testing_ to really verify frontend functionality. For this we should likely use [playwright-python](https://github.com/microsoft/playwright-python) for ease of use in python scripting.
- The tests need to run in GitHub workflows (eventually), headless browser dependencies _might_ (but not necessarily) need to be dockerized as well.

## Context

### Bugs Encountered in Recent Auth Development Session

The following bugs were discovered during manual testing of the auth flow. Each represents a gap in automated test coverage:

1. **401 on `/builds` endpoint before org selection**

   - UI made API calls before user had completed org selection
   - Token was OIDC (user-scoped) but endpoint expected internal (org-scoped) token
   - _Test needed_: Verify UI doesn't call protected endpoints before org selection

2. **404 on `/api/v1/auth/exchange`**

   - Router was mounted without the `/api/v1` prefix
   - _Test needed_: Verify all routers are mounted with correct prefixes

3. **"Unable to find key with ID" error**

   - API tried to validate OIDC token against internal JWKS
   - Endpoint expected internal token but received OIDC token
   - _Test needed_: Each endpoint should reject wrong token type with clear error

4. **ImportError: tomli-w in CLI**

   - CLI dependency not included in package
   - _Test needed_: CLI commands should be importable/runnable without import errors

5. **400 "Offline tokens not allowed" on OIDC exchange**

   - OIDC provider configuration issue (Keycloak `offline_access` scope)
   - _Test needed_: Full OAuth PKCE flow with OIDC provider

6. **CLI command structure mismatch with docs**

   - Docs showed `stardag registry` but CLI was `stardag config registry`
   - _Test needed_: CLI command structure matches documentation

7. **403 when profile stored slugs instead of UUIDs**
   - Profile config stored slugs but API expected UUIDs
   - Fixed by adding ID cache that resolves slugs to IDs
   - _Test needed_: Profile with slugs works correctly after slug-to-ID resolution

### Current Test Coverage

| Component           | Unit Tests | Integration Tests | Notes                                 |
| ------------------- | ---------- | ----------------- | ------------------------------------- |
| API - Auth          | ✅ Good    | ❌ None           | Token validation, JWKS caching tested |
| API - Builds/Tasks  | ✅ Basic   | ❌ None           | Mocked auth only                      |
| API - Organizations | ✅ Basic   | ❌ None           | Mocked auth only                      |
| SDK - Config        | ✅ Good    | ❌ None           | Profile/registry parsing tested       |
| SDK - CLI Registry  | ✅ Basic   | ❌ None           | CRUD operations tested                |
| SDK - CLI Auth      | ❌ None    | ❌ None           | **Critical gap**                      |
| SDK - CLI Profiles  | ❌ None    | ❌ None           | **Critical gap**                      |
| Frontend            | ❌ None    | ❌ None           | **Critical gap**                      |
| Full Auth Flow      | ❌ None    | ❌ None           | **Critical gap**                      |

### Existing Test Infrastructure

- **API tests** (`app/stardag-api/tests/`)

  - Uses in-memory SQLite
  - Mocks auth dependencies (bypasses real token validation)
  - PostgreSQL fixture available but skipped without env var

- **SDK tests** (`lib/stardag/tests/`)

  - Unit tests with mocked file system
  - No integration with real API

- **E2E script** (`scripts/e2e-test.sh`)
  - Shell script, brings up docker-compose
  - Basic curl checks (health, builds count)
  - No browser testing, no auth flow

## Execution Plan

### Summary Of Preparatory Analysis

The biggest coverage gaps are:

1. **Full authentication flow**: OIDC → token exchange → internal token → API access
2. **CLI integration**: Commands running against real API
3. **Frontend**: No tests at all (React app)
4. **Token type validation**: Endpoints accepting wrong token types
5. **Profile/context resolution**: Slug → ID resolution, profile switching

### Plan

#### Phase 1: Test Infrastructure Setup

1. **Create integration test package** (`tests/integration/`)

   ```
   tests/
   └── integration/
       ├── __init__.py
       ├── conftest.py           # Shared fixtures
       ├── docker_fixtures.py    # Docker compose management
       ├── test_api_auth.py      # API auth flow tests
       ├── test_cli_auth.py      # CLI auth flow tests
       ├── test_cli_config.py    # CLI config/profile tests
       └── test_frontend/        # Playwright browser tests
           ├── __init__.py
           ├── conftest.py
           └── test_login_flow.py
   ```

2. **Docker compose fixture** (`docker_fixtures.py`)

   - Bring up API, DB, Keycloak (OIDC provider)
   - Wait for health endpoints
   - Provide cleanup on teardown
   - Support parallel test isolation (unique workspace per test)

3. **Test user fixture**
   - Create test user in Keycloak
   - Provide credentials for automated login

#### Phase 2: API Integration Tests

4. **Token type validation tests** (`test_api_auth.py`)

   - [ ] `/api/v1/auth/exchange` accepts only OIDC tokens
   - [ ] `/api/v1/builds` rejects OIDC tokens with 401
   - [ ] `/api/v1/builds` accepts internal tokens
   - [ ] `/api/v1/ui/me` accepts both token types
   - [ ] API key auth works for SDK endpoints

5. **API endpoint access tests**
   - [ ] Verify all routers mounted with correct prefixes
   - [ ] Protected endpoints return 401 without auth
   - [ ] Protected endpoints return 403 for wrong org

#### Phase 3: CLI Integration Tests

6. **CLI auth tests** (`test_cli_auth.py`)

   - [ ] `stardag auth login` opens browser, receives callback
   - [ ] `stardag auth status` shows logged in state
   - [ ] `stardag auth refresh` refreshes tokens
   - [ ] `stardag auth logout` clears credentials

7. **CLI config tests** (`test_cli_config.py`)

   - [ ] `stardag config registry add/list/remove` works
   - [ ] `stardag config profile add` with slugs resolves to IDs
   - [ ] `stardag config profile use` switches context and refreshes token
   - [ ] `stardag config show` displays current context

8. **CLI importability tests**
   - [ ] All CLI modules import without error
   - [ ] All dependencies available

#### Phase 4: Frontend Browser Tests (Playwright)

9. **Login flow tests** (`test_frontend/test_login_flow.py`)

   - [ ] User can navigate to login
   - [ ] OAuth redirect to OIDC provider works
   - [ ] After login, redirected back to app
   - [ ] User info displayed after login
   - [ ] Organization selector works
   - [ ] Protected pages accessible after org selection
   - [ ] Protected pages inaccessible before org selection

10. **Build/Task UI tests**
    - [ ] Builds list loads after auth
    - [ ] Tasks list loads after auth
    - [ ] Workspace switching updates data

#### Phase 5: Full Flow Integration Tests

11. **SDK → API → UI round trip**

    - [ ] SDK registers task/build
    - [ ] UI displays the registered build
    - [ ] Status updates propagate

12. **Profile-based workflow**
    - [ ] Create profile with slugs via CLI
    - [ ] SDK uses resolved config to register build
    - [ ] API receives correct workspace_id

### Implementation Order

1. **Infra first**: Docker fixtures, conftest setup (1-3)
2. **API auth**: Critical for unblocking other tests (4-5)
3. **CLI auth**: Tests the full OAuth flow (6-8)
4. **Frontend**: Validates user-facing auth experience (9-10)
5. **Full flow**: End-to-end validation (11-12)

## Decisions

1. **Use pytest-playwright** for browser tests

   - Pro: Python ecosystem, shared fixtures with API tests
   - Con: Slightly less mature than JS playwright

2. **Keep integration tests in separate package**

   - Pro: Clear separation, different dependencies
   - Con: Some code duplication with unit test fixtures

3. **Run Keycloak in docker-compose for tests**

   - Pro: Tests real OIDC flow
   - Con: Slower startup, may need pre-configured realm export

4. **Test with real PostgreSQL** (not SQLite)
   - Pro: Catches DB-specific issues
   - Con: Slower than in-memory SQLite

## Progress

- [x] Analysis of current test coverage
- [x] Identification of bugs/gaps from auth session
- [x] Detailed execution plan created
- [ ] Test infrastructure setup
- [ ] API integration tests
- [ ] CLI integration tests
- [ ] Frontend browser tests
- [ ] Full flow tests

## Notes

### Key Files to Reference

- `app/stardag-api/tests/conftest.py` - Existing API test fixtures
- `lib/stardag/tests/test_config.py` - Config unit tests
- `scripts/e2e-test.sh` - Current E2E approach
- `docker-compose.yml` - Service definitions

### Dependencies to Add

```toml
# pyproject.toml for integration tests
[project.optional-dependencies]
integration = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "playwright>=1.40",
    "pytest-playwright>=0.4",
    "httpx>=0.26",
    "python-dotenv>=1.0",
]
```

### Playwright Setup

```bash
# Install browsers
playwright install chromium

# Or in CI
playwright install --with-deps chromium
```

### Environment Variables for Tests

```bash
# Point to test docker-compose services
STARDAG_API_URL=http://localhost:8000
KEYCLOAK_URL=http://localhost:8080
TEST_USER_EMAIL=test@example.com
TEST_USER_PASSWORD=testpass
```
