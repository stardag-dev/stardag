# Auth + User, Organization and Workspace Management (In preparation for Deployment)

## Status

active

## Goal

The end/bigger goal is to be able to easily deploy the stardag application to AWS, and for anyone else to do this in a reproducible way.

For this we need to first of all setup proper authentication. That is the scope of this specific task. (The actual deployment will follow in later tasks).

When the app is deployed, we want anyone to be able

- to navigate to some landing page, hit "signup"
- choose "login with google" -> a user account is created
- The user should be able to create an organization and add ("invite") new users to this org, identified by email. (_no_ email send out needs to be implemented)
- When a user is part of an org, it should be selectable as "active org" in the app, and the user should see the workspaces of that org (all initially, no access management per workspace.)

## Instructions

Add "SAAS production grade" authentication to `app/stardag-api` and `app/stardag-ui`.

Rough plan of what needs to be done.

**Prepare DB for production multi tenant setup**

- Create a DB "admin" role, with adequate permission, which will be used by alembic and maintainers of the service.
- Create a separate "service" role, with adequate (necessary CRUD) permissions to be used by the FastAPI backend.

**Modify the DB schema(s)**
Such that:

- [ ] any user can belong to (none or) many organizations.
- [ ] a user can be admin or not for an organization (only admin can create workspaces and invite/remove members)
- [ ] Add a schema for API-Keys connected to an organization (not user)

**Update UI with settings for all core (auth related) entities**

- [ ] basic profile page
- [ ] some clean way to select the active organization
- [ ] after having selected an organization, toggle between workspaces.
- [ ] A way to create a new organization -> becomes admin
- [ ] A way to create a new workspace
- [ ] A way to add members _by email_ to this org. These users don't need to exist yet ("pending invite") but once they are created, they will be added to the org.
- [ ] A way to manage (i.e. also remove) members -> access removed but any entities related to this user still remain (visible for remaining users)
- [ ] Add a view for creating and deactivating API keys (for SDK auth), only visible for admin users.

**Clearly separate API endpoints used by UI app vs SDK (task Registry)**
As a preparatory step. Auth from SDK will be handled with _API Keys_

**Add actual auth mechanisms to frontend and backend**

- [ ] Setup Keycloak for local usage/testing
- [ ] API key auth OR JWT for SDK access (via the Registry during `build`s), the idea here is that when running on production infra, API keys will be used, but when a user runs locally _against the production workspace_ the user can authenticate via CLI and get directed to the browser to authenticate and then store a local token for SDK access.
- [ ] OIDC / JWT for UI app access.

**Default initialization when running locally**
Implement minimal setup logic: Python based, running in a separate docker compose service which creates, if not exists, a default user, organization and workspace.

**Basic unit test coverage**
Preferably as we go for previous steps!

## Context

### End Goal

Single FastAPI + Postgres + React SPA app, deployable to **any AWS account**, with **real authentication in prod** and **OIDC-backed auth locally** via docker-compose. Same auth model everywhere (JWT / OIDC).

---

### Core Design

- Authentication model: OpenID Connect (OIDC) with JWTs
- Prod IdP: Amazon Cognito User Pool (Hosted UI)
  - Federated with Google (OIDC)
- Local IdP: Keycloak (OIDC)
- Backend auth rule: FastAPI validates JWTs using issuer JWKS (pluggable issuer via env vars)

---

### AWS Stack (IaC)

Use AWS CDK (TypeScript) or Terraform.

Components:

- Frontend: React → S3 + CloudFront
- Backend: FastAPI container → ECS Fargate + ALB
- Database: RDS Postgres
- Auth:
  - Cognito User Pool
  - Hosted UI enabled
  - Google IdP configured
- Secrets: AWS Secrets Manager (DB creds)
- Networking: VPC, private subnets for DB

Outputs:

- FRONTEND_URL
- API_URL
- OIDC_ISSUER (Cognito)
- OIDC_CLIENT_ID

---

### Local Development (docker-compose)

Services:

- frontend (React)
- backend (FastAPI)
- postgres
- keycloak (OIDC issuer)

Keycloak:

- Preloaded realm
- Public SPA client (Auth Code + PKCE)
- Test users
- Emits standard OIDC JWTs

---

### Backend (FastAPI)

- Auth implemented as dependency / middleware
- Validate:
  - Authorization: Bearer <JWT>
  - Signature via JWKS
  - Claims: iss, aud, exp
- Config via env vars:
  - OIDC_ISSUER_URL
  - OIDC_AUDIENCE
- No auth bypass flags; issuer swap handles local vs prod

---

### Frontend (React)

- Uses OIDC Authorization Code + PKCE
- Library: oidc-client-ts (or equivalent)
- Config via env:
  - OIDC_ISSUER
  - OIDC_CLIENT_ID
  - REDIRECT_URI
- Same code path for Cognito (prod) and Keycloak (local)

---

### Auth Flow (Both Environments)

1. User logs in via Hosted UI (Cognito or Keycloak)
2. React receives ID/access token
3. React calls API with Authorization: Bearer
4. FastAPI validates token via issuer JWKS
5. Claims drive user identity + org mapping

---

### Non-Goals

- No custom auth UI
- No password storage
- No “auth disabled” mode
- No environment-specific branching in app logic

---

### Result

- Reproducible AWS deployment
- Real auth locally
- Identical security model in dev and prod
- Minimal surface area for auth bugs

## Execution Plan

### Summary of Preparatory Analysis

**Current State:**

- No authentication implemented; all requests use hardcoded "default" user/org/workspace
- User model exists but lacks OIDC fields (no `external_id` for subject claim)
- User has single org FK - needs to become many-to-many with roles
- Organization and Workspace models exist with proper relationships
- API has 2 routers (builds, tasks) - no separation between UI and SDK endpoints
- UI has no auth context, no login flow, no protected routes
- Docker Compose has 4 services: db, migrations, api, ui (no IdP)

**Key Design Decisions:**

1. **Roles:** owner (one per org, cannot be removed), admin (can manage members/workspaces), member (read/write access)
2. **User provisioning:** Auto-create User record on first OIDC login (using `sub` claim as `external_id`)
3. **Invites:** Store pending invites by email; require explicit acceptance when user signs up
4. **API Keys:** Scoped to workspace (not org), no varying permission levels for now
5. **No backward compatibility:** Can rewrite migrations from scratch

---

### Plan

The implementation is split into 6 phases, each building on the previous.

---

#### Phase 1: Database Schema & Multi-Tenancy Foundation

**Goal:** Restructure the database for proper multi-tenant auth with roles, invites, and API keys.

**Tasks:**

- [x] **1.0** Setup separate Postgres roles (admin/service):
  - Create `docker/postgres/init.sql` with role creation and default privileges
  - Update `docker-compose.yml` to mount init script and use separate credentials
  - Migrations run as `stardag_admin`, API runs as `stardag_service`
- [x] **1.1** Create new Alembic migration (replace existing `normalized_schema.py`)
- [x] **1.2** Update `User` model:
  - Remove `organization_id` FK (users belong to many orgs now)
  - Add `external_id` (string, unique) - stores OIDC `sub` claim
  - Add `email` as unique field (for invite matching)
- [x] **1.3** Create `OrganizationMember` junction table:
  - `user_id` FK, `organization_id` FK
  - `role` enum: `owner`, `admin`, `member`
  - `joined_at` timestamp
  - Constraint: exactly one owner per org
- [x] **1.4** Create `Invite` model:
  - `id`, `organization_id` FK, `email`, `role`, `invited_by` (user FK)
  - `created_at`, `expires_at` (optional), `accepted_at` (nullable)
  - Unique constraint: `(organization_id, email)` where not accepted
- [x] **1.5** Create `ApiKey` model:
  - `id`, `workspace_id` FK, `name`, `key_hash` (bcrypt), `key_prefix` (first 8 chars for display)
  - `created_by` (user FK), `created_at`, `last_used_at`, `revoked_at` (nullable)
- [x] **1.6** Update test fixtures and seed data
- [x] **1.7** Add unit tests for new models and constraints

**Deliverable:** Database schema supporting multi-tenant auth with all required tables.

---

#### Phase 2: Keycloak + Backend Auth Infrastructure

**Goal:** Add Keycloak to local dev and implement JWT validation in FastAPI.

**Tasks:**

- [x] **2.1** Add Keycloak service to `docker-compose.yml`:
  - Use `quay.io/keycloak/keycloak` image
  - Configure realm import on startup
  - Create public SPA client with PKCE
- [x] **2.2** Create Keycloak realm configuration (`docker/keycloak/realm-export.json`):
  - Realm: `stardag`
  - Client: `stardag-ui` (public, auth code + PKCE)
  - Client: `stardag-sdk` (public, for CLI device flow)
  - Test users: `admin`/`admin`, `testuser`/`testpassword`
- [x] **2.3** Implement JWT validation in FastAPI:
  - Create `auth/jwt.py` with JWKS fetching and caching
  - Create `auth/dependencies.py` with `get_current_user` dependency
  - Validate: signature, issuer, audience, expiration
- [x] **2.4** Implement user auto-provisioning:
  - On valid JWT, lookup user by `external_id` (sub claim)
  - If not found, create user from token claims (email, name)
  - Return `User` model instance to endpoints
- [x] **2.5** Add auth configuration via environment variables:
  - `OIDC_ISSUER_URL`, `OIDC_AUDIENCE`, `OIDC_EXTERNAL_ISSUER_URL`
- [x] **2.6** Reorganize API routes:
  - `/api/v1/ui/...` - UI endpoints (JWT auth)
  - `/api/v1/sdk/...` - SDK endpoints (API key OR JWT auth) - pending
  - Keep existing endpoints functional during transition
- [x] **2.7** Add integration tests for auth flow (19 new tests)

**Deliverable:** Working JWT auth locally with Keycloak; users auto-created on first login.

---

#### Phase 3: Frontend Auth Integration

**Goal:** Add OIDC login/logout to React app with protected routes.

**Tasks:**

- [x] **3.1** Install and configure `oidc-client-ts`
- [x] **3.2** Create `AuthProvider` context:
  - Manage auth state (user, isAuthenticated, isLoading)
  - Handle silent token refresh via `automaticSilentRenew`
  - Store tokens in localStorage (via oidc-client-ts default)
- [x] **3.3** Implement login flow:
  - Redirect to Keycloak hosted login
  - Handle callback via `/callback` route
  - User auto-provisioned via backend on first API call
- [x] **3.4** Implement logout flow:
  - Clear local state
  - Redirect to Keycloak logout endpoint
- [ ] **3.5** Create `ProtectedRoute` wrapper component (deferred - not blocking)
- [x] **3.6** Update API client to include `Authorization: Bearer` header
  - Created `api/client.ts` with `fetchWithAuth()` wrapper
- [x] **3.7** Add auth config via environment variables:
  - `VITE_OIDC_ISSUER`, `VITE_OIDC_CLIENT_ID`, `VITE_OIDC_REDIRECT_URI`
  - Updated Dockerfile to pass build args
- [x] **3.8** Create UserMenu component with login/logout
- [x] **3.9** Add loading states and error handling for auth

**Deliverable:** Users can log in via Keycloak, access protected dashboard, and log out.

---

#### Phase 4: Organization & Workspace Management (Backend)

**Goal:** Implement backend APIs for org/workspace CRUD and membership management.

**Tasks:**

- [x] **4.1** Create organization management endpoints:
  - Create org (creator becomes owner, default workspace created)
  - Get org details (members only)
  - Update org details (admin+ only)
  - Delete org (owner only, cascades)
- [x] **4.2** Create membership management endpoints:
  - Invite user by email (admin+ only)
  - List pending invites (admin+ only)
  - Accept/decline invite (by invited user)
  - Remove member (admin+ only, cannot remove last owner)
  - Change member role (admin+, owner for owner changes)
- [x] **4.3** Create workspace management endpoints:
  - Create workspace in org (admin+ only)
  - List workspaces in org (all members)
  - Get/Update workspace (admin+ only)
  - Delete workspace (admin+ only, can't delete last)
- [x] **4.4** Add authorization helpers:
  - `require_org_access(org_id, min_role)` - returns 404 if not member, 403 if insufficient role
  - Role hierarchy: member < admin < owner
- [x] **4.5** Create REST endpoints in `routes/organizations.py`:
  - `POST /api/v1/ui/organizations` - create org
  - `GET /api/v1/ui/organizations/{org_id}` - get org details
  - `PATCH /api/v1/ui/organizations/{org_id}` - update org
  - `DELETE /api/v1/ui/organizations/{org_id}` - delete org
  - `GET /api/v1/ui/organizations/{org_id}/members` - list members
  - `PATCH /api/v1/ui/organizations/{org_id}/members/{id}` - change role
  - `DELETE /api/v1/ui/organizations/{org_id}/members/{id}` - remove member
  - `POST /api/v1/ui/organizations/{org_id}/invites` - create invite
  - `GET /api/v1/ui/organizations/{org_id}/invites` - list pending invites
  - `DELETE /api/v1/ui/organizations/{org_id}/invites/{id}` - cancel invite
  - `POST /api/v1/ui/organizations/invites/{id}/accept` - accept invite
  - `POST /api/v1/ui/organizations/invites/{id}/decline` - decline invite
  - `GET /api/v1/ui/organizations/{org_id}/workspaces` - list workspaces
  - `POST /api/v1/ui/organizations/{org_id}/workspaces` - create workspace
  - `GET /api/v1/ui/organizations/{org_id}/workspaces/{id}` - get workspace
  - `PATCH /api/v1/ui/organizations/{org_id}/workspaces/{id}` - update workspace
  - `DELETE /api/v1/ui/organizations/{org_id}/workspaces/{id}` - delete workspace
  - (Existing) `GET /api/v1/ui/me` - get current user profile with orgs
  - (Existing) `GET /api/v1/ui/me/invites` - list user's pending invites
- [ ] **4.6** Update existing build/task endpoints to enforce org membership (deferred to Phase 5/6)
- [x] **4.7** Add comprehensive unit and integration tests (11 new tests)

**Deliverable:** Full backend API for multi-tenant org/workspace management with authorization.

---

#### Phase 5: Organization & Workspace Management (UI)

**Goal:** Build UI for profile, org selection, workspace switching, and member management.

**Tasks:**

- [x] **5.1** Create global state for active org/workspace:
  - Created `WorkspaceContext` with `WorkspaceProvider`
  - Persist selection to localStorage
  - Fetches user orgs on login, auto-selects last active
- [ ] **5.2** Create `ProfilePage` component (deferred - not blocking):
  - Display user info (name, email)
  - List organizations user belongs to
  - Show pending invites with accept/decline actions
- [x] **5.3** Create org/workspace selector in header:
  - Created `WorkspaceSelector` dropdown component
  - Shows active org/workspace
  - Allows switching between orgs and workspaces
- [x] **5.4** Create `OrganizationSettingsPage`:
  - Created `/settings` route with `OrganizationSettings` component
  - Org details (name, description) - editable by admin+
  - Member list with roles and role change (owner only)
  - Invite member form (email + role) - admin+
  - Remove member button (owner only)
  - Delete org button (owner only, with slug confirmation)
- [x] **5.5** Create workspace management in settings:
  - Workspace list with CRUD in `OrganizationSettings`
  - Create workspace form (name, auto-slug)
  - Edit workspace name inline
  - Delete workspace button (admin+ only, can't delete last)
- [ ] **5.6** Create `CreateOrganizationModal` (deferred - can use API directly):
  - Name, slug, description inputs
  - On success, switch to new org
- [ ] **5.7** Create `CreateWorkspaceModal` (moved to settings page):
  - Name, slug, description inputs
  - On success, switch to new workspace
- [x] **5.8** Update dashboard to filter by active workspace:
  - Updated `useTasks` hook to use `activeWorkspace` from context
  - Builds fetched with `workspace_id` filter
- [x] **5.9** Add navigation and routing for new pages:
  - Added `/settings` route
  - Added settings gear icon in header
  - Simple URL-based router in App.tsx
- [x] **5.10** Handle edge cases:
  - User with no organizations shows "View pending invites" link
  - Loading states implemented
  - Stale data cleared when switching workspaces with no builds
- [x] **5.11** Additional UI improvements:
  - Created landing page for non-authenticated users
  - URL paths now include org/workspace slugs for shareable links (e.g., `/:orgSlug/:workspaceSlug`)
  - Create organization page with auto-slug generation
  - Pending invites page for new users
  - Newly created org automatically becomes active
- [x] **5.12** Add workspace authorization to builds API (read endpoints):
  - `GET /builds` - requires auth + workspace_id, verifies user has access
  - `GET /builds/{build_id}` - requires auth, verifies user can access build's workspace
  - `GET /builds/{build_id}/tasks` - requires auth, verifies workspace access
  - `GET /builds/{build_id}/events` - requires auth, verifies workspace access
  - `GET /builds/{build_id}/graph` - requires auth, verifies workspace access
  - Created `verify_workspace_access()` helper in `auth/dependencies.py`
- [x] **5.13** Fix tasks API to use authenticated fetch:
  - Updated `api/tasks.ts` to use `fetchWithAuth()` for all API calls

**Deliverable:** Complete UI for managing organizations, workspaces, and members.

---

#### Phase 6: API Keys & SDK Authentication

**Goal:** Implement API key management and SDK authentication (including CLI browser flow).

**Tasks:**

- [x] **6.1** Create `ApiKeyService` (`services/api_keys.py`):
  - Generate API key with bcrypt hashing (returns plaintext once, stores hash)
  - List keys for workspace (show prefix, name, created_at, last_used)
  - Revoke key (sets `revoked_at` timestamp)
  - Validate API key by prefix lookup + hash verification
- [x] **6.2** Add API key endpoints in `routes/organizations.py`:
  - `POST /api/v1/ui/organizations/{org_id}/workspaces/{workspace_id}/api-keys` - create key (admin+)
  - `GET /api/v1/ui/organizations/{org_id}/workspaces/{workspace_id}/api-keys` - list keys
  - `DELETE /api/v1/ui/organizations/{org_id}/workspaces/{workspace_id}/api-keys/{key_id}` - revoke key (admin+)
- [x] **6.3** Implement API key auth middleware (`auth/dependencies.py`):
  - Check `X-API-Key` header
  - Validate against stored hash via `api_key_service.validate_api_key()`
  - Update `last_used_at` on successful validation
  - Return `ApiKeyAuth` context with workspace (not user context)
  - Added `SdkAuth` dataclass for unified SDK authentication context
- [x] **6.4** Update SDK endpoints to accept API key OR JWT (`routes/builds.py`):
  - If `X-API-Key` present, use API key auth (workspace from key)
  - If `Authorization: Bearer` present, use JWT auth (requires workspace_id param)
  - All SDK write endpoints now require authentication:
    - `POST /builds` - create build (auth required)
    - `POST /builds/{id}/complete` - mark complete (auth required, verifies workspace)
    - `POST /builds/{id}/fail` - mark failed (auth required, verifies workspace)
    - `POST /builds/{id}/tasks` - register task (auth required, verifies workspace)
    - `POST /builds/{id}/tasks/{task_id}/start` - start task (auth required, verifies workspace)
    - `POST /builds/{id}/tasks/{task_id}/complete` - complete task (auth required, verifies workspace)
    - `POST /builds/{id}/tasks/{task_id}/fail` - fail task (auth required, verifies workspace)
- [x] **6.5** Create API key management UI (`components/OrganizationSettings.tsx`):
  - Added API Keys section in organization settings (admin+ only)
  - Workspace selector to view/manage keys per workspace
  - List existing keys (prefix, name, last used)
  - Create key form with name input
  - Show generated key ONCE in green highlighted modal with copy button
  - Revoke button with confirmation dialog
  - Added API functions in `api/organizations.ts` (fetchApiKeys, createApiKey, revokeApiKey)
- [x] **6.6** Implement SDK CLI authentication (`lib/stardag/src/stardag/cli/`):
  - Created Typer-based CLI with `stardag` command
  - `stardag auth login --api-key sk_xxx` - validates and stores API key
  - `stardag auth logout` - clears stored credentials
  - `stardag auth status` - shows current auth status
  - `stardag auth configure` - update API URL or workspace
  - Credentials stored in `~/.stardag/credentials.json` (0600 permissions)
  - Added `cli` optional dependency with typer and httpx
- [x] **6.7** Update SDK `Registry` client to use credentials (`build/api_registry.py`):
  - APIRegistry auto-loads credentials from CLI store
  - Priority: explicit api_key > STARDAG_API_KEY env var > CLI credentials
  - Also loads api_url and workspace_id from CLI credentials
  - Adds X-API-Key header to all requests
- [ ] **6.8** Add tests for API key auth and SDK flows

**Deliverable:** Complete API key management; SDK can authenticate via API key or user JWT.

---

### Local Development Initialization

After Phase 2, add a `setup` service to docker-compose that:

- Waits for API to be healthy
- Creates default organization "Local Dev" if not exists
- Creates default workspace "default" if not exists
- Creates test user in Keycloak realm
- Invites test user to default org as owner

This ensures `docker-compose up` provides a working local environment.

---

## Decisions

| Decision                            | Rationale                                                             |
| ----------------------------------- | --------------------------------------------------------------------- |
| Roles: owner/admin/member           | Standard SaaS pattern; owner ensures org can't be orphaned            |
| User auto-provisioning              | Reduces friction; user record created on first login from OIDC claims |
| Invites require acceptance          | Explicit consent; allows user to decline unwanted org membership      |
| API keys scoped to workspace        | Principle of least privilege; SDK typically operates in one workspace |
| API key hash storage                | Security best practice; plaintext shown once on creation              |
| No permission levels on API keys    | Simplicity for MVP; can add read-only keys later                      |
| Keycloak for local dev              | Full OIDC compatibility with Cognito; no mock/bypass needed           |
| Single migration rewrite            | No existing users; clean slate is simpler than migration path         |
| Separate UI/SDK route prefixes      | Clear separation of auth mechanisms; easier to reason about           |
| oidc-client-ts for frontend         | Well-maintained, TypeScript-native, supports PKCE                     |
| Tokens in memory (not localStorage) | XSS protection; refresh via silent renew                              |

## Progress

- [x] Initial planning and requirements gathering
- [x] Codebase analysis
- [x] Phase 1: Database Schema & Multi-Tenancy Foundation
  - [x] 1.0 Setup separate Postgres roles (admin/service)
  - [x] 1.1-1.5 Created new models (OrganizationMember, Invite, ApiKey)
  - [x] 1.6 Updated User model (external_id, email, removed org FK)
  - [x] 1.7 Created Alembic migration with all auth tables
  - [x] 1.8 Updated test fixtures and added 9 new auth model tests
  - [x] E2E smoke test verified (docker-compose up, migrations, API)
- [x] Phase 2: Keycloak + Backend Auth Infrastructure
  - [x] Added Keycloak to docker-compose with realm import
  - [x] Created realm config with stardag-ui and stardag-sdk clients
  - [x] Implemented JWT validation with JWKS caching (`auth/jwt.py`)
  - [x] Implemented user auto-provisioning (`auth/dependencies.py`)
  - [x] Added OIDC config via env vars
  - [x] Created `/api/v1/ui/me` and `/api/v1/ui/me/invites` endpoints
  - [x] Added 19 auth tests
- [x] Phase 3: Frontend Auth Integration
  - [x] Installed oidc-client-ts
  - [x] Created AuthProvider context with login/logout
  - [x] Created UserMenu component
  - [x] Added `/callback` route for OIDC redirect
  - [x] Created `fetchWithAuth` API wrapper
  - [x] Updated Dockerfile with OIDC build args
  - [x] Verified login flow works with Keycloak
- [x] Phase 4: Organization & Workspace Management (Backend)
  - [x] Created `routes/organizations.py` with full CRUD
  - [x] Implemented authorization (role hierarchy: member < admin < owner)
  - [x] Added org, workspace, member, and invite endpoints
  - [x] Added 11 new tests (57 total tests passing)
- [x] Phase 5: Organization & Workspace Management (UI)
  - [x] Created `WorkspaceContext` for global org/workspace state
  - [x] Created `WorkspaceSelector` dropdown in header
  - [x] Created `OrganizationSettings` page with:
    - Org details editing (admin+)
    - Member list with role changes (owner)
    - Invite form (admin+)
    - Workspace list with CRUD (admin+)
    - Delete org (owner with confirmation)
  - [x] Updated `useTasks` hook to filter by active workspace
  - [x] Added `/settings` route and navigation
  - [x] Created landing page for non-authenticated users
  - [x] Added URL paths with org/workspace slugs for shareable links
  - [x] Created organization creation page
  - [x] Created pending invites page
  - [x] Added workspace authorization to builds API (read endpoints)
  - [x] Fixed tasks API to use authenticated fetch
- [x] Phase 6: API Keys & SDK Authentication
  - [x] Created `ApiKeyService` in `services/api_keys.py`
  - [x] Added API key management endpoints in `routes/organizations.py`
  - [x] Implemented API key auth middleware in `auth/dependencies.py`
  - [x] Updated SDK endpoints to accept API key OR JWT in `routes/builds.py`
  - [x] Created API key management UI in `OrganizationSettings.tsx`
  - [x] Implemented SDK CLI with Typer (`stardag auth login/logout/status`)
  - [x] Updated APIRegistry to auto-load credentials from CLI store
  - [ ] Tests for API key auth (6.8) - to be added

## Notes

- **Testing strategy:** Each phase should include unit tests. Integration tests with Keycloak can use testcontainers or a dedicated test realm.
- **Migration path for production:** When deploying to AWS, swap Keycloak for Cognito by changing env vars only.
- **Legacy data migration:** Builds created before multi-tenancy have `workspace_id: 'default'` (string) instead of proper UUID. These need manual migration to associate with real workspaces.
- **Future considerations:**
  - Workspace-level permissions (read-only members)
  - Audit logging for compliance
  - SSO with other providers (GitHub, Microsoft)
  - Organization billing/quotas
