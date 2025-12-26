# Config and auth cleanup - TAKE TWO

## Status

active

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

### Plan

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
