# Config and auth cleanup - TAKE TWO

## Status

completed

## Goal

Simplify and clarify configuration and auth handling.

## Instructions

Update auth and configuraton handling to match the current update description in the [CONFIGURATION_GUIDE.md](../../../CONFIGURATION_GUIDE.md).

Key things to handle:

- [ ] Authentication "once" but refetch credentials for each specific org., _See last section on [Target model (authoritative)](#target-model-authoritative)_.
  - [ ] Solve this in CLI auth flow and storing of credentials
  - [ ] Soved this in the Web Auth flow.
    - [ ] In the UI: Make the distinction/selection of organization more _fundamental_, there should be single "icon/text" in the upper left corner indicating the organization, and this is the only place to switch and created new orgs. Everything else in the UI (including settings) should only concern the active organization. This is basically like it works and looks in _Slack_.
- [ ] Simplify and update configuration and active profile
  - [ ] The "priamary key" for the active state is `(registry_url, organization_id, workspace_id)`.
  - [ ] There is _no longer any state stored_ by the CLI; for simplicity either these entities need to be specified as environment variables, or a single envvironment variable pointing to the active profile via `STARDAG_PROFILE`.
  - [ ] So, the config.toml (new format) is just a _flat_ list of registries and profiles (for now).
  - [ ] The CLI can help out to create/maintain this configuration
  - [ ] Question: should it be `stardag config registry ...` and `stardag config profile ...` or just skip the `config` level in the CLI?
  - [ ] Keep all config, credentials and target roots caching in
- [ ] API key auth (recommended alternative in production workers): Make sure this plays well with CLI for local auth and configuration.
- [ ] Review all endpoints wrt. the updated auth.
  - [ ] Oranization baked into ACCESS*TOKEN/API_KEY: backen \_look up* organization from this
  - [ ] Worspace provided explcitily as path(/query parameter), backend alwayas _verifies_ this.

_GENERAL NOTE: strive for simplicitly and principle of least surprise - question any part that deviates from best practices or where a simpler options is available._

## Context

In the previous tasks:

- [auth.md](./auth.md)
- [config-and-auth-cleanup.md](./config-and-auth-cleanup.md)
  We introduced auth and configuration logic. This was not properly thought through. It is over complicated and has several glitches.

See the diff in commit "simplify intended setup in CONFIGURATION_GUIDE.md":

```bash
git show 6fbc2ca44a967f17b49736fcedd6e0615620ced3
```

NOTE: _See also last section on [Target model (authoritative)](#target-model-authoritative)_.

## Execution Plan

### Summary Of Preparatory Analysis

#### Current State vs Target Model

**Current Implementation:**

| Aspect             | Current                                             | Target                                                 |
| ------------------ | --------------------------------------------------- | ------------------------------------------------------ |
| **Config format**  | JSON files                                          | TOML files                                             |
| **File structure** | Deep nested: `~/.stardag/registries/{registry}/...` | Flat: `~/.stardag/config.toml` + `credentials/`        |
| **CLI state**      | `active_registry`, `active_workspace` files         | **No state files** - use env vars or `STARDAG_PROFILE` |
| **Token model**    | Single access token (user-scoped from Keycloak)     | User-scoped refresh token + org-scoped access JWTs     |
| **Org in auth**    | Not in token, looked up via membership              | `org_id` claim in access JWT                           |
| **Workspace**      | In context config, sometimes in request             | Always from path/query params, never in JWT            |
| **Env vars**       | `STARDAG_API_URL`, `STARDAG_REGISTRY`               | `STARDAG_REGISTRY_URL`, `STARDAG_PROFILE`              |

**Key Files to Change (SDK/CLI):**

- `lib/stardag/src/stardag/config.py` - Complete rewrite for TOML, flat structure, profile lookup
- `lib/stardag/src/stardag/cli/auth.py` - Token exchange flow, per-registry refresh tokens
- `lib/stardag/src/stardag/cli/credentials.py` - New storage model
- `lib/stardag/src/stardag/cli/config.py` - Simplify commands, remove state management
- `lib/stardag/src/stardag/cli/registry.py` - Simplify to just registry URL management

**Key Files to Change (API):**

- `app/stardag-api/src/stardag_api/auth/jwt.py` - Add org_id to TokenPayload
- `app/stardag-api/src/stardag_api/auth/dependencies.py` - Read org from JWT, not request
- `app/stardag-api/src/stardag_api/routes/` - New `/auth/exchange` endpoint, update all routes

**Key Files to Change (UI):**

- Organization selector → top-left, fundamental (like Slack)
- Token exchange on org switch
- Separate user session from org context

#### Analysis: Does the New Setup Make Sense?

**YES - Significantly simpler and more robust:**

1. **No CLI state** → Deterministic behavior from environment. Same env = same result.
2. **Flat TOML** → More readable, standard Python config format.
3. **Clear separation**: Registry = URL, Profile = (registry, org, workspace) tuple.
4. **Org-scoped tokens** → Proper security model, org can't be tampered with.
5. **Workspace from path** → Explicit, auditable, no hidden state.

**Potential Further Simplifications:**

1. **Credentials storage clarification**: The TODO in CONFIGURATION_GUIDE.md asks "how to store credentials per each (registry, organization)?".

   - **Answer**: Refresh tokens are **user-scoped, per-registry only** (not per-org). The org-scoped access JWT is minted on demand via `/auth/exchange`. Store refresh tokens in `credentials/{registry_name}.json`, cache access JWTs in memory or a separate cache file.

2. **Default profile**: The `[default]` section in CONFIGURATION_GUIDE.md example only has `registry = "local"`. Should be `profile = "local"` to indicate which named profile to use when `STARDAG_PROFILE` is not set.

3. **API key scope alignment**: Current API keys are workspace-scoped. Target says "org-scoped (optionally workspace-restricted)". **No change needed** - workspace-scoped is compatible (more restrictive).

4. **Environment variable naming**: Consider aligning names more closely:
   - `STARDAG_REGISTRY_URL` (new) replaces `STARDAG_API_URL` (current)
   - `STARDAG_REGISTRY_API_KEY` (new) vs `STARDAG_API_KEY` (current) - minor rename

### Plan

#### Phase 0: Documentation & Design Decisions (this analysis)

- [x] Analyze current vs target state
- [x] Identify all changes needed
- [ ] Finalize open design questions (see Decisions section)

#### Phase 1: Backend - Org-Scoped Token Model

**1.1 Internal JWT Infrastructure**

- Generate RSA keypair for signing (or load from env/secrets)
- Implement `/.well-known/jwks.json` endpoint serving public key
- Create `tokens.py` module:
  - `create_internal_token(user_id, org_id, roles?) -> str` - mint RS256-signed JWT
  - `InternalTokenPayload` dataclass with `sub`, `org_id`, `iss="stardag-api"`, `exp`, etc.

**1.2 Token Exchange Endpoint**

- Add `POST /auth/exchange` endpoint:
  - Input: `{ org_id: string }`
  - Auth: **Keycloak JWT only** (this is the only endpoint accepting external JWTs)
  - Flow:
    1. Validate Keycloak JWT (existing `JWTValidator`)
    2. Get/create user from token claims
    3. Verify user is member of requested org
    4. Mint internal JWT with `org_id` claim
    5. Return `{ access_token, token_type, expires_in }`

**1.3 Separate Validators**

- `KeycloakJWTValidator` - validates external Keycloak tokens (used only by `/auth/exchange`)
- `InternalJWTValidator` - validates internal org-scoped tokens (used by all other endpoints)
- Update `get_token()` dependency to use `InternalJWTValidator` only

**1.4 Update TokenPayload**

- `InternalTokenPayload` - for internal JWTs, requires `org_id: str`
- `KeycloakTokenPayload` - for Keycloak JWTs, no `org_id` (user identity only)
- All endpoints except `/auth/exchange` use `InternalTokenPayload`

**1.5 Update Auth Dependencies**

- `get_internal_token()` - validate internal JWT, return `InternalTokenPayload`
- `get_current_user()` - lookup user by `token.sub`
- `get_org_id()` - extract `org_id` from internal token (always present)
- `verify_workspace_access()` - validate `workspace.organization_id == token.org_id`
- `SdkAuth` - include `org_id` from token

**1.6 Update API Routes**

- All routes (except `/auth/exchange`): require internal JWT via `get_internal_token()`
- If `org_id` in path: validate it matches `token.org_id` (or return 403)
- Workspace always from path/query, validated against token's org

#### Phase 2: SDK/CLI - Config Format & Structure

**2.1 New Config File Format (TOML)**

```toml
# ~/.stardag/config.toml
[registry.local]
url = "http://localhost:8000"

[registry.central]
url = "https://api.stardag.com"

[profile.local]
registry = "local"
organization = "default"
workspace = "default"

[profile.prod]
registry = "central"
organization = "my-org"
workspace = "production"

[default]
profile = "local"
```

**2.2 New Directory Structure**

```
~/.stardag/
├── config.toml                    # All config in one file
├── credentials/                   # Per-registry refresh tokens
│   ├── local.json                 # { refresh_token, token_endpoint, client_id }
│   └── central.json
├── access-token-cache/            # Short-lived, per (registry, org)
│   ├── local__default.json        # { access_token, expires_at }
│   └── central__my-org.json
├── target-root-cache.json         # Flat array per (registry_url, org_id, workspace_id)
└── local-target-roots/            # For local file-based targets
    └── default/
        └── default/
```

**2.3 Rewrite `config.py`**

- Load config.toml using `tomllib` (Python 3.11+) or `tomli`
- Profile resolution: `STARDAG_PROFILE` env var → `[default].profile` → error
- Explicit env vars override everything:
  - `STARDAG_REGISTRY_URL` + `STARDAG_ORGANIZATION_ID` + `STARDAG_WORKSPACE_ID`
  - `STARDAG_API_KEY` (for production workers)
- Remove all state-loading functions (`load_active_registry`, `load_active_workspace`)
- Keep target root cache loading/validation

**2.4 Update CLI Commands**

- `stardag registry add <name> --url <url>` - Add registry to config.toml
- `stardag registry list` - List registries from config.toml
- `stardag auth login [--registry]` - OAuth flow, store refresh token
- `stardag auth status [--registry]` - Show auth status
- `stardag profile add <name> -r <registry> -o <org> -w <workspace>` - Add profile
- `stardag profile list` - List profiles
- **REMOVE**: `stardag registry use`, `stardag config set organization/workspace` (no more state)
- **KEEP**: `stardag target-roots sync` - Sync target roots from API

#### Phase 3: CLI - Token Exchange Flow

**3.1 Login Flow**

1. `stardag auth login --registry central`
2. OAuth PKCE flow → get tokens from Keycloak
3. Store **refresh token only** in `credentials/central.json`
4. Prompt user to select org (or auto-select if only one)
5. Call `/auth/exchange { org_id }` to get org-scoped access JWT
6. Cache access JWT in `access-token-cache/central__my-org.json`
7. Prompt user to create a profile for this (registry, org, workspace) combo

**3.2 Token Refresh**

- Before API calls, check if cached access JWT is expired
- If expired, use refresh token to mint new org-scoped access JWT
- If refresh token invalid, prompt interactive login

**3.3 Profile-Based Access**

- `STARDAG_PROFILE=prod python my_script.py`
- SDK looks up profile → gets registry, org, workspace
- Loads refresh token for registry
- Mints/caches access JWT for that org
- Provides workspace to API calls via path/query

#### Phase 4: UI - Organization Selector

**4.1 Top-Level Organization Selector**

- Single org selector in top-left corner (like Slack)
- Shows current org name/logo
- Dropdown to switch orgs or create new org
- Everything else in UI assumes active org

**4.2 Token Exchange on Org Switch**

- When user selects new org:
  1. Call `POST /auth/exchange { org_id }`
  2. Store new access JWT in memory/localStorage
  3. Redirect to org's default workspace
- No re-authentication with IdP

**4.3 Session Management**

- User session (cookie) = user identity only
- Access JWT = org-scoped, stored separately
- On page load: check session valid, then check/refresh access JWT for current org

#### Phase 5: API Key Path (Production Workers)

**5.1 Verify Current Implementation**

- API keys are workspace-scoped (includes org implicitly)
- `X-API-Key` header → lookup → get (workspace_id, org_id)
- No changes needed, this already aligns with target model

**5.2 Environment Variable Setup**

- Production workers use:
  ```
  STARDAG_REGISTRY_URL=https://api.stardag.com
  STARDAG_API_KEY=sk_...
  STARDAG_WORKSPACE_ID=ws_123
  ```
- SDK detects API key → skips OAuth flow → uses API key directly

#### Phase 6: Migration & Cleanup

**6.1 Backwards Compatibility (Temporary)**

- Support both old JSON config and new TOML config
- Log deprecation warnings for old format
- Auto-migrate on first run (optional)

**6.2 Cleanup Old Code**

- Remove `active_registry` / `active_workspace` file handling
- Remove nested `registries/{name}/...` directory structure support
- Remove JSON config loading (after migration period)

### Open Design Questions

**Q1: Token Exchange Implementation**

- Option A: Mint our own JWTs signed with a secret key
- Option B: Use Keycloak's token exchange feature
- **Recommendation**: Option A - simpler, full control, no Keycloak dependency for exchange

**Q2: Access Token Cache Location**

- Option A: Memory only (re-mint on each CLI invocation)
- Option B: File cache with expiry
- **Recommendation**: Option B - better UX, avoid repeated exchange calls

**Q3: Profile Name in API Calls**

- Current: workspace_id in query param
- Target: workspace_id in path (e.g., `/workspaces/{workspace_id}/...`)
- **Recommendation**: Path for resource-specific endpoints, query for list endpoints

**Q4: Rename CLI Commands**

- Current: `stardag config list organizations`, `stardag config set workspace`
- Target: `stardag organizations list`, `stardag workspaces list` (no set commands)
- **Recommendation**: Cleaner, matches the "no state" philosophy

## Decisions

### Confirmed Decisions

**D1: Remove CLI state files**

- No `active_registry`, `active_workspace` files
- Everything via environment variables or explicit config

**D2: TOML config format**

- Flat structure with `[registry.name]` and `[profile.name]` sections
- More readable than nested JSON

**D3: Org-scoped access tokens**

- `org_id` claim in JWT
- Minted via `/auth/exchange` endpoint
- Short TTL (5-15 min)

**D4: Workspace from path/query only**

- Never in JWT
- Always explicit in API requests
- Server validates workspace belongs to JWT's org

**D5: Credentials per registry (not per org)**

- Refresh token is user-scoped
- Access JWTs cached per (registry, org)

### Pending Decisions

(None - all resolved)

### Resolved Decisions

**P1: JWT signing for token exchange** → **Option A with refinements**

Decision:

- **Only `/auth/exchange` accepts Keycloak JWTs** - single entry point for token conversion
- **All other endpoints accept only internal org-scoped JWTs** - clean separation
- **Sign internal JWTs with asymmetric keys (RS256) + serve JWKS** - standard, rotatable
- **Short TTLs** - minimize risk from token theft

Rationale:

- Clear separation: Keycloak handles AuthN (user identity), Stardag API handles AuthZ (org-scoping)
- Single validation path for most endpoints (simpler, faster)
- JWKS allows key rotation without client changes
- IdP-agnostic: can swap Keycloak for Auth0/Okta without changing internal token model

Implementation notes:

```
                    ┌─────────────┐
                    │  Keycloak   │
                    │   (AuthN)   │
                    └──────┬──────┘
                           │ Keycloak JWT (user identity)
                           ▼
┌──────────────────────────────────────────────────────┐
│                    Stardag API                        │
│  ┌────────────────┐                                  │
│  │ /auth/exchange │ ◄── Only endpoint accepting      │
│  │                │     Keycloak JWTs                │
│  └───────┬────────┘                                  │
│          │ Mints internal JWT with org_id            │
│          ▼                                           │
│  ┌────────────────┐                                  │
│  │ Internal JWT   │ RS256-signed, short TTL          │
│  │ {sub, org_id}  │ Served via /.well-known/jwks.json│
│  └───────┬────────┘                                  │
│          │                                           │
│          ▼                                           │
│  ┌────────────────┐                                  │
│  │ All other      │ ◄── Only accept internal JWTs    │
│  │ endpoints      │                                  │
│  └────────────────┘                                  │
└──────────────────────────────────────────────────────┘
```

**P2: Access token TTL** → **10 minutes**

- Start with 10 min, easy to adjust via config
- Short enough to limit damage from token theft
- Long enough to avoid excessive exchange calls

**P3: Migration strategy** → **Clean slate**

- No backwards compatibility needed
- Can rewrite migrations, DB schema, config format
- Recreate any test data as needed
- Goal: cleanest possible implementation

## Progress

- [x] Initial analysis of current vs target state
- [x] Identified all necessary changes
- [x] Created detailed execution plan
- [x] Finalize pending decisions
- [x] Phase 1: Backend token model
  - [x] Phase 1.1: Created `tokens.py` with `InternalTokenManager` (RS256, JWKS)
  - [x] Phase 1.2: Added `/auth/exchange` endpoint
  - [x] Phase 1.3: Created separate validators (Keycloak vs Internal)
  - [x] Phase 1.4: Updated token payload models (`InternalTokenPayload`)
  - [x] Phase 1.5: Updated auth dependencies (`get_org_id_from_token`, `verify_workspace_access`)
  - [x] Phase 1.6: Updated all API routes to use internal tokens
  - All 68 tests passing
- [x] Phase 2: SDK/CLI config rewrite
  - [x] TOML config format (`~/.stardag/config.toml`)
  - [x] Profile-based configuration (registry, org, workspace tuple)
  - [x] New env vars: `STARDAG_PROFILE`, `STARDAG_REGISTRY_URL`
  - [x] New directory structure:
    - `~/.stardag/credentials/{registry}.json` (refresh tokens)
    - `~/.stardag/access-token-cache/{registry}__{org}.json`
    - `~/.stardag/target-root-cache.json`
  - [x] Removed CLI state files (active_registry, active_workspace)
  - [x] Updated CLI commands (registry add/list/remove, config profile add/list/use/remove)
  - [x] Deprecated old functions with warnings
  - [x] Updated tests for new config system
- [x] Phase 3: CLI token exchange flow (implemented in Phase 2)
  - [x] Login flow calls `/auth/exchange` after Keycloak auth
  - [x] `stardag auth refresh` command for token refresh
  - [x] Access tokens cached per (registry, org) combo
- [x] Phase 4: UI organization selector
  - [x] Added `api/auth.ts` with `exchangeToken()` function
  - [x] Updated `AuthContext` with org-scoped token handling
  - [x] Updated `api/client.ts` to use org-scoped tokens
  - [x] Updated `WorkspaceContext` to trigger token exchange on org switch
  - [x] Created `OrganizationSelector` component (Slack-like design)
  - [x] Token caching in localStorage with expiry tracking
- [x] Phase 5: Verify API key path
  - [x] `SdkAuth` supports both API key and JWT authentication
  - [x] API keys workspace-scoped, org inferred from workspace
  - [x] No changes needed to API key flow
- [x] Phase 6: Migration & cleanup (clean slate - no migration needed)

## Notes

### Key Insight: Simplicity Through Statelessness

The core improvement in the new model is **removing CLI state**. Instead of:

```
# Old: State scattered across files
~/.stardag/active_registry → "central"
~/.stardag/registries/central/active_workspace → "ws_123"
~/.stardag/registries/central/config.json → { organization_id: "..." }
```

We have:

```
# New: Everything explicit
STARDAG_PROFILE=prod  # or explicit: STARDAG_REGISTRY_URL + STARDAG_ORGANIZATION_ID + STARDAG_WORKSPACE_ID
```

This makes behavior **deterministic** and **debuggable**. Same environment = same result.

### Risk Areas

1. **Token exchange implementation** - Need to design JWT signing carefully
2. **UI session management** - Complex state management for org switching
3. **Migration path** - Existing users have old directory structure
4. **Testing** - Need comprehensive tests for new auth flow

### Dependencies Between Phases

```
Phase 1 (Backend tokens) → Phase 3 (CLI token exchange)
                        → Phase 4 (UI org selector)
Phase 2 (Config format) → Phase 3 (CLI token exchange)
Phase 5 (API keys) - Independent, verify existing implementation
Phase 6 (Migration) - After all other phases complete
```

# Target model (authoritative)

**AuthN once (user-scoped), AuthZ per org (token-scoped).**

- Authenticate the user once via OAuth/OIDC.
- Store a **user-scoped refresh token / session** (not tied to any org).
- **Mint short-lived JWT access tokens per organization**.
- Workspace is **NOT in the JWT** → handled via explicit API path/query params and validated server-side.

---

## Token model

### Refresh token / session

- User identity only
- Long-lived
- Stored securely (cookie for web, keychain for CLI)
- Used only to mint access tokens

### Access JWT (org-scoped)

Must include:

- `sub` = user_id
- `org_id` = active organization (required)
- `aud`, `iss`, `iat`, `exp` (short TTL, e.g. 5–15 min)
- roles / entitlements for that org (or role ids)
- optional: `jti`, `membership_version`

Must NOT include:

- workspace_id

---

## Web UI flow

1. **Login**

   - OAuth/OIDC → establish user session / refresh token
   - Mint initial access JWT for default org

2. **Switch organization**

   - UI calls `POST /auth/exchange { org_id }`
   - Server verifies membership
   - Server returns new access JWT with new org_id
   - No reauthentication with IdP

3. **API calls**
   - `Authorization: Bearer <access_jwt>`
   - API infers org strictly from JWT

---

## CLI flow

1. **Login**

   - OAuth (PKCE or device code)
   - Store refresh token securely
   - Call `/auth/exchange` to mint org-scoped access JWT

2. **Switch organization**

   - `mycli org use <org>`
   - Call `/auth/exchange { org_id }`
   - Cache new access JWT

3. **Token refresh**
   - On access token expiry → re-exchange using refresh token
   - Interactive login only if refresh token expired/revoked

---

## API authorization rules

- Organization is inferred from JWT only
- Workspace comes from path/query param
- Server must validate:
  - token is valid
  - user is member of `token.org_id`
  - workspace belongs to that org
  - user has permission in that workspace

❌ Never accept org_id from query/body  
✅ If org_id appears in path, it must match token.org_id

---

## Required endpoints

- `POST /auth/exchange`

  - input: `{ org_id }`
  - auth: refresh token or session
  - output: org-scoped access JWT

- `GET /me/orgs`
- `GET /orgs/{org_id}/workspaces`

---

## API keys (machine / SDK clients)

- Use **API key on every request** (no JWT exchange).
- API key identifies a non-human principal (service account).
- Key is **org-scoped** (optionally workspace- and permission-restricted).
- Server authenticates key, resolves `{org_id, permissions, key_id}`, and enforces authZ.
- Store only hashed API keys; support rotation, revocation, rate limits, and audit logs.

---

## Security checklist (for implementation review)

### Authentication

- [ ] OAuth/OIDC login establishes user identity only
- [ ] Refresh token is NOT org-scoped
- [ ] Refresh token stored securely (cookie / keychain)

### Access tokens

- [ ] Access JWT is org-scoped
- [ ] Short TTL (≤15 min)
- [ ] New JWT minted on org switch
- [ ] No workspace in JWT

### Authorization

- [ ] API never trusts org from request params
- [ ] org_id always comes from JWT
- [ ] workspace validated against org + membership
- [ ] permission checks use (user, org, workspace)

### API keys

- [ ] Keys are org-scoped service accounts
- [ ] Keys stored hashed; log key_id only
- [ ] Rotation + revocation supported
- [ ] Rate limiting and auditing per key

### Edge cases

- [ ] Org removal takes effect within token TTL
- [ ] Token exchange verifies membership every time
- [ ] CLI auto-refreshes tokens
- [ ] Org mismatch (path vs token) → 403/404

---

## Mental model

**Login once as a user → mint short-lived JWTs per organization → workspace is explicit and validated; machine clients use org-scoped API keys on every call.**
