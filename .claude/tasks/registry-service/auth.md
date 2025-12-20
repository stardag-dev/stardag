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

### Summary Of Preparatory Analysis

### Plan

TODO

1. Step one
2. Step two
3. ...

## Decisions

Key decisions made and their rationale.

## Progress

- [x] Completed item
- [ ] Pending item

## Notes

Any additional observations, blockers, or open questions.
