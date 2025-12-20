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

- [ ] **1.1** Create new Alembic migration (replace existing `normalized_schema.py`)
- [ ] **1.2** Update `User` model:
  - Remove `organization_id` FK (users belong to many orgs now)
  - Add `external_id` (string, unique) - stores OIDC `sub` claim
  - Add `email` as unique field (for invite matching)
- [ ] **1.3** Create `OrganizationMember` junction table:
  - `user_id` FK, `organization_id` FK
  - `role` enum: `owner`, `admin`, `member`
  - `joined_at` timestamp
  - Constraint: exactly one owner per org
- [ ] **1.4** Create `Invite` model:
  - `id`, `organization_id` FK, `email`, `role`, `invited_by` (user FK)
  - `created_at`, `expires_at` (optional), `accepted_at` (nullable)
  - Unique constraint: `(organization_id, email)` where not accepted
- [ ] **1.5** Create `ApiKey` model:
  - `id`, `workspace_id` FK, `name`, `key_hash` (bcrypt), `key_prefix` (first 8 chars for display)
  - `created_by` (user FK), `created_at`, `last_used_at`, `revoked_at` (nullable)
- [ ] **1.6** Update test fixtures and seed data
- [ ] **1.7** Add unit tests for new models and constraints

**Deliverable:** Database schema supporting multi-tenant auth with all required tables.

---

#### Phase 2: Keycloak + Backend Auth Infrastructure

**Goal:** Add Keycloak to local dev and implement JWT validation in FastAPI.

**Tasks:**

- [ ] **2.1** Add Keycloak service to `docker-compose.yml`:
  - Use `quay.io/keycloak/keycloak` image
  - Configure realm import on startup
  - Create public SPA client with PKCE
- [ ] **2.2** Create Keycloak realm configuration (`keycloak/realm-export.json`):
  - Realm: `stardag`
  - Client: `stardag-ui` (public, auth code + PKCE)
  - Client: `stardag-sdk` (public, for CLI device flow)
  - Test users with emails
- [ ] **2.3** Implement JWT validation in FastAPI:
  - Create `auth/jwt.py` with JWKS fetching and caching
  - Create `auth/dependencies.py` with `get_current_user` dependency
  - Validate: signature, issuer, audience, expiration
- [ ] **2.4** Implement user auto-provisioning:
  - On valid JWT, lookup user by `external_id` (sub claim)
  - If not found, create user from token claims (email, name)
  - Return `User` model instance to endpoints
- [ ] **2.5** Add auth configuration via environment variables:
  - `OIDC_ISSUER_URL`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL` (optional, derived from issuer)
- [ ] **2.6** Reorganize API routes:
  - `/api/v1/ui/...` - UI endpoints (JWT auth)
  - `/api/v1/sdk/...` - SDK endpoints (API key OR JWT auth)
  - Keep existing endpoints functional during transition
- [ ] **2.7** Add integration tests for auth flow (using test JWTs)

**Deliverable:** Working JWT auth locally with Keycloak; users auto-created on first login.

---

#### Phase 3: Frontend Auth Integration

**Goal:** Add OIDC login/logout to React app with protected routes.

**Tasks:**

- [ ] **3.1** Install and configure `oidc-client-ts` (or `react-oidc-context`)
- [ ] **3.2** Create `AuthProvider` context:
  - Manage auth state (user, isAuthenticated, isLoading)
  - Handle silent token refresh
  - Store tokens in memory (not localStorage for security)
- [ ] **3.3** Implement login flow:
  - Redirect to Keycloak hosted login
  - Handle callback, extract tokens
  - Call API to get/create user profile
- [ ] **3.4** Implement logout flow:
  - Clear local state
  - Redirect to Keycloak logout endpoint
- [ ] **3.5** Create `ProtectedRoute` wrapper component
- [ ] **3.6** Update API client to include `Authorization: Bearer` header
- [ ] **3.7** Add auth config via environment variables:
  - `VITE_OIDC_ISSUER`, `VITE_OIDC_CLIENT_ID`, `VITE_OIDC_REDIRECT_URI`
- [ ] **3.8** Create minimal login page / landing page
- [ ] **3.9** Add loading states and error handling for auth

**Deliverable:** Users can log in via Keycloak, access protected dashboard, and log out.

---

#### Phase 4: Organization & Workspace Management (Backend)

**Goal:** Implement backend APIs for org/workspace CRUD and membership management.

**Tasks:**

- [ ] **4.1** Create `OrganizationService` with business logic:
  - Create org (creator becomes owner)
  - Get user's organizations
  - Update org details (admin+ only)
  - Delete org (owner only, with cascade considerations)
- [ ] **4.2** Create `MembershipService`:
  - Invite user by email (admin+ only)
  - List pending invites
  - Accept invite (by invited user)
  - Remove member (admin+ only, cannot remove owner)
  - Change member role (owner only)
- [ ] **4.3** Create `WorkspaceService`:
  - Create workspace in org (admin+ only)
  - List workspaces in org (all members)
  - Update workspace (admin+ only)
  - Delete workspace (admin+ only)
- [ ] **4.4** Add authorization helpers:
  - `require_org_member(org_id)` - raises 403 if not member
  - `require_org_admin(org_id)` - raises 403 if not admin+
  - `require_org_owner(org_id)` - raises 403 if not owner
- [ ] **4.5** Create REST endpoints:
  - `POST /api/v1/ui/organizations` - create org
  - `GET /api/v1/ui/organizations` - list user's orgs
  - `GET /api/v1/ui/organizations/{org_id}` - get org details
  - `PUT /api/v1/ui/organizations/{org_id}` - update org
  - `DELETE /api/v1/ui/organizations/{org_id}` - delete org
  - `GET /api/v1/ui/organizations/{org_id}/members` - list members
  - `POST /api/v1/ui/organizations/{org_id}/invites` - create invite
  - `GET /api/v1/ui/organizations/{org_id}/invites` - list pending invites
  - `DELETE /api/v1/ui/organizations/{org_id}/invites/{invite_id}` - cancel invite
  - `POST /api/v1/ui/invites/{invite_id}/accept` - accept invite
  - `DELETE /api/v1/ui/organizations/{org_id}/members/{user_id}` - remove member
  - `PUT /api/v1/ui/organizations/{org_id}/members/{user_id}` - change role
  - `POST /api/v1/ui/organizations/{org_id}/workspaces` - create workspace
  - `GET /api/v1/ui/organizations/{org_id}/workspaces` - list workspaces
  - `GET /api/v1/ui/me` - get current user profile
  - `GET /api/v1/ui/me/invites` - list user's pending invites
- [ ] **4.6** Update existing build/task endpoints to enforce org membership
- [ ] **4.7** Add comprehensive unit and integration tests

**Deliverable:** Full backend API for multi-tenant org/workspace management with authorization.

---

#### Phase 5: Organization & Workspace Management (UI)

**Goal:** Build UI for profile, org selection, workspace switching, and member management.

**Tasks:**

- [ ] **5.1** Create global state for active org/workspace:
  - Store in React context or lightweight state manager
  - Persist selection to localStorage
  - Sync with URL params (optional)
- [ ] **5.2** Create `ProfilePage` component:
  - Display user info (name, email)
  - List organizations user belongs to
  - Show pending invites with accept/decline actions
- [ ] **5.3** Create org/workspace selector in header:
  - Dropdown to switch active organization
  - Secondary dropdown for workspace within org
  - "Create new organization" option
- [ ] **5.4** Create `OrganizationSettingsPage`:
  - Org details (name, description) - editable by admin+
  - Member list with roles
  - Invite member form (email + role)
  - Remove member button (admin+ only)
  - Role change dropdown (owner only)
  - Delete org button (owner only, with confirmation)
- [ ] **5.5** Create `WorkspaceSettingsPage`:
  - Workspace details (name, description)
  - Delete workspace button (admin+ only)
- [ ] **5.6** Create `CreateOrganizationModal`:
  - Name, slug, description inputs
  - On success, switch to new org
- [ ] **5.7** Create `CreateWorkspaceModal`:
  - Name, slug, description inputs
  - On success, switch to new workspace
- [ ] **5.8** Update dashboard to filter by active workspace
- [ ] **5.9** Add navigation and routing for new pages
- [ ] **5.10** Handle edge cases:
  - User with no organizations (prompt to create or wait for invite)
  - Org/workspace deleted while viewing

**Deliverable:** Complete UI for managing organizations, workspaces, and members.

---

#### Phase 6: API Keys & SDK Authentication

**Goal:** Implement API key management and SDK authentication (including CLI browser flow).

**Tasks:**

- [ ] **6.1** Create `ApiKeyService`:
  - Generate API key (returns plaintext once, stores hash)
  - List keys for workspace (show prefix, name, created_at, last_used)
  - Revoke key
- [ ] **6.2** Add API key endpoints:
  - `POST /api/v1/ui/workspaces/{workspace_id}/api-keys` - create key
  - `GET /api/v1/ui/workspaces/{workspace_id}/api-keys` - list keys
  - `DELETE /api/v1/ui/workspaces/{workspace_id}/api-keys/{key_id}` - revoke key
- [ ] **6.3** Implement API key auth middleware:
  - Check `X-API-Key` header
  - Validate against stored hash
  - Update `last_used_at`
  - Return workspace context (not user context)
- [ ] **6.4** Update SDK endpoints to accept API key OR JWT:
  - If `X-API-Key` present, use API key auth
  - If `Authorization: Bearer` present, use JWT auth
  - Require at least one
- [ ] **6.5** Create API key management UI:
  - List existing keys (prefix, name, last used)
  - "Create new key" button with name input
  - Show generated key ONCE in modal (copy button)
  - Revoke button with confirmation
  - Only visible to admin+ users
- [ ] **6.6** Implement SDK CLI authentication (lower priority):
  - Device authorization flow or localhost callback flow
  - CLI command: `stardag auth login`
  - Opens browser to Keycloak/Cognito
  - Receives token via localhost callback or polling
  - Stores token in `~/.stardag/credentials`
  - CLI command: `stardag auth logout` - clears stored token
- [ ] **6.7** Update SDK `Registry` client to use credentials:
  - Check for API key in env (`STARDAG_API_KEY`)
  - Fall back to stored JWT from CLI login
  - Fall back to browser-based login prompt
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
- [ ] Phase 1: Database Schema & Multi-Tenancy Foundation
- [ ] Phase 2: Keycloak + Backend Auth Infrastructure
- [ ] Phase 3: Frontend Auth Integration
- [ ] Phase 4: Organization & Workspace Management (Backend)
- [ ] Phase 5: Organization & Workspace Management (UI)
- [ ] Phase 6: API Keys & SDK Authentication

## Notes

- **Testing strategy:** Each phase should include unit tests. Integration tests with Keycloak can use testcontainers or a dedicated test realm.
- **Migration path for production:** When deploying to AWS, swap Keycloak for Cognito by changing env vars only.
- **Future considerations:**
  - Workspace-level permissions (read-only members)
  - Audit logging for compliance
  - SSO with other providers (GitHub, Microsoft)
  - Organization billing/quotas
