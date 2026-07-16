# Self-host Stardag on Modal

Run your own private Stardag service — the Registry API and web UI — on
[Modal](https://modal.com), with the database on [Neon](https://neon.com).
One command brings up the full stack; one command updates it. Expected cost
for a small team: **$0** (both services have free tiers that comfortably
cover a low-traffic deployment).

!!! info "What you get"

    - The Stardag **UI + Registry API** served from your own Modal workspace
      at `https://<your-workspace>--stardag-server.modal.run`
    - A **Postgres database** on Neon (scale-to-zero, free tier)
    - **Authentication** without extra services (email/password managed by
      the API) — or bring any OIDC provider
    - The same Modal account then **executes your DAGs** via Stardag's
      [Modal integration](integrate-modal.md)

## Quickstart

**Prerequisites:** ~10 minutes, a GitHub/Google account for sign-ups, and
[uv](https://docs.astral.sh/uv/) (or plain Python ≥ 3.10 + pip).

### 1. Create a Modal account and token

Sign up at [modal.com](https://modal.com) (free Starter plan), then
authenticate your machine:

```bash
uvx modal token new
```

This opens the browser and stores a token in `~/.modal.toml`.

### 2. Create a Neon account and API key

Sign up at [console.neon.tech](https://console.neon.tech) (free plan), then
create an API key at
[console.neon.tech/app/settings/api-keys](https://console.neon.tech/app/settings/api-keys)
→ **Create key**. Copy the key — you'll paste it in step 4.

You do _not_ need to create a database or project — the deploy command does
that via the API (Postgres 16, project name `stardag`).

### 3. Clone the stardag repo

```bash
git clone https://github.com/stardag-dev/stardag.git
cd stardag
```

### 4. Deploy

```bash
uvx --from "stardag[selfhost]" stardag self-host up
```

The command walks you through the rest interactively:

- **Neon API key** → provisions the database (created once, reused after)
- **Auth mode** → press Enter for `local` (email/password, no extra
  accounts; see [OIDC mode](#auth-mode-oidc-external-identity-provider)
  for the alternative)
- **Admin email + password** → your first login account

It then generates a JWT signing keypair (stored as a Modal secret), applies
database migrations, builds the container image (the UI is compiled inside
the image build — no local Node needed), deploys, and prints your URL:

```
Stardag is up!

  UI:  https://<your-workspace>--stardag-server.modal.run
  API: https://<your-workspace>--stardag-server.modal.run/api/v1
```

### 5. Sign in and connect the SDK

Open the UI and sign in with the admin account. A personal workspace with a
`Local` environment is created automatically.

Point the SDK/CLI at your registry:

```bash
stardag config registry add selfhosted --url https://<your-workspace>--stardag-server.modal.run
stardag auth login -r selfhosted   # prompts for the same email/password
```

From here, everything in [Use the API Registry](use-api-registry.md) and
[Integrate with Modal](integrate-modal.md) applies — e.g. mint an API key
for Modal-executed DAGs with `stardag modal stardag-api-key create`.

## Updating to a new version

From the repo checkout:

```bash
git pull
uvx --from "stardag[selfhost]" stardag self-host upgrade
```

This applies any new database migrations and redeploys the API + UI from
the current source. The JWT keypair secret is left untouched, so existing
sessions and SDK logins survive upgrades.

## Auth mode: `local` (default)

The API manages email/password accounts directly and mints its own tokens —
no external identity provider.

- **Self-service signup is off by default** (your deployment is on a public
  URL). To onboard a team, re-run `up` with `--enable-registration`, let
  everyone register, then re-run without it to close signup again. Members
  are added to shared workspaces from the UI's workspace settings.
- Passwords: min 8 characters, bcrypt-hashed; login is rate-limited.
- Change your password anytime from the UI (user menu → Change password),
  or via `POST /api/v1/auth/change-password`.

## Auth mode: `oidc` (external identity provider)

Any standards-compliant OIDC provider works (the hosted flow is
authorization code + PKCE, RS256 tokens). A good free option is
[WorkOS AuthKit](https://workos.com) (free up to 1M monthly active users);
[Auth0](https://auth0.com)'s free tier also works.

```bash
uvx --from "stardag[selfhost]" stardag self-host up --auth-mode oidc \
  --oidc-issuer https://<your-idp-issuer> \
  --oidc-ui-client-id <client-id>
```

Provider setup (example: WorkOS AuthKit):

1. Create a WorkOS account → enable AuthKit.
2. Create a client application; note the **issuer URL** and **client ID**.
3. Add the redirect URI printed by `self-host up`:
   `https://<your-workspace>--stardag-server.modal.run/callback`
4. If your provider's JWKS is not at
   `<issuer>/protocol/openid-connect/certs` (the Keycloak convention), pass
   `--oidc-jwks-url` explicitly (e.g. WorkOS: `<issuer>/oauth2/jwks`,
   Auth0: `<issuer>/.well-known/jwks.json`).

The UI discovers all of this at runtime from the API (`/api/v1/auth/config`)
— no UI rebuild needed when you change providers.

## Costs, limits, and knobs

| Aspect     | Default       | Notes                                                                                                           |
| ---------- | ------------- | --------------------------------------------------------------------------------------------------------------- |
| Modal cost | $0            | Starter plan includes $30/month credits; scale-to-zero when idle                                                |
| Cold start | a few seconds | First request after idle. Add `--keep-warm 1` for an always-on container (≈$5/month, still within free credits) |
| Neon       | free plan     | 0.5 GB storage, ~100 compute-hours/month, autosuspends after 5 min idle (~1s wake)                              |
| URL        | `*.modal.run` | Custom domains require a paid Modal plan                                                                        |
| Emails     | disabled      | Workspace invites by email require SES and are off in self-host mode                                            |

## Commands reference

```bash
stardag self-host up         # provision + deploy (idempotent; re-run to reconfigure)
stardag self-host upgrade    # migrate DB + redeploy from current source
stardag self-host status     # deployment status + URL
stardag self-host destroy    # stop the Modal app (never touches the database)
```

Useful flags for `up` (all prompts have flag equivalents for CI):

- `--name` — Modal app name / URL label (default `stardag-server`)
- `--neon-project` — Neon project name to find-or-create (default `stardag`)
- `--database-url` / `--database-url-direct` — bring your own Postgres
  instead of Neon (use `postgresql+asyncpg://...` URLs; pass the direct
  variant if the main URL goes through a transaction-mode pooler)
- `--keep-warm N` — always-on containers (default 0 = scale to zero)
- `--yes` — non-interactive; fails instead of prompting

## Troubleshooting

- **First request hangs a few seconds** — cold start (Modal container boot
  - Neon wake). Expected with scale-to-zero; use `--keep-warm 1` to avoid.
- **`Modal authentication not set up`** — run `uvx modal token new`.
- **`Neon API key rejected`** — create a key at
  [console.neon.tech/app/settings/api-keys](https://console.neon.tech/app/settings/api-keys).
- **Sign-in loops or 401s right after an upgrade** — stale cached tokens;
  sign out and in again. (The JWT keypair is preserved across upgrades, so
  this should be rare.)
- **Logs** — `uvx modal app logs stardag-server`.
- **Migration failures** — `self-host up`/`upgrade` print the Alembic
  output; migrations always run against Neon's direct (non-pooled)
  endpoint. Re-running the command is safe (migrations are idempotent).
