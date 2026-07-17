# Self-host Stardag on Modal

Run your own private Stardag service — the Registry API and web UI — on
[Modal](https://modal.com), with the database on [Neon](https://neon.com).
One command brings up the full stack; one command updates it. Expected cost
for a small team: **$0** (both services have free tiers that comfortably
cover a low-traffic deployment).

!!! info "What you get"

    - The Stardag **UI + Registry API** served from your own Modal workspace
      at `https://<your-workspace>-stardag-host--server.modal.run`
    - A **Postgres database** on Neon (scale-to-zero, free tier)
    - **Authentication** without extra services (email/password managed by
      the API) — or bring any OIDC provider
    - A **ready-to-use setup**: workspace + `main` environment mirroring
      your Modal workspace, an API key wired into Modal for DAG execution,
      and a local SDK profile — created automatically by `self-host up`
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
→ **Create key**. Copy the key — you'll paste it in step 3.

You do _not_ need to create a database or project — the deploy command does
that via the API (Postgres 16, project name `stardag`).

### 3. Deploy

```bash
uvx --python 3.12 --from "stardag[selfhost]" stardag self-host up
```

(`--python 3.12` matches the CLI's interpreter to the prebuilt server
image's Python — required because the deploy serializes functions with your
local interpreter. The CLI checks this and tells you the remedy if it
doesn't match.)

The command walks you through the rest interactively:

- **Neon API key** → provisions the database (created once, reused after)
- **Auth mode** → press Enter for `local` (email/password, no extra
  accounts; see [OIDC mode](#auth-mode-oidc-external-identity-provider)
  for the alternative)
- **Admin email + password** → your first login account
- **Primary workspace** → if your Modal workspace is shared (a team
  workspace), a Stardag workspace with the same name is created; personal
  Modal accounts use your personal Stardag workspace

It then generates a JWT signing keypair (stored as a Modal secret), applies
database migrations, deploys the prebuilt server image (Registry API + web
UI, published to the public GitHub Container Registry — no repo checkout,
Node, or Docker needed locally), and **completes the setup** — after which
everything is wired up:

```
Stardag is up!

  UI:  https://<your-workspace>-stardag-host--server.modal.run
  API: https://<your-workspace>-stardag-host--server.modal.run/api/v1
```

### 4. Done — what the defaults create

`up` finishes with a summary panel of everything that now exists:

| What                                         | Default                                                                  | Purpose                                                                        |
| -------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| Modal env `stardag-host` + app `server`      | server app + config/JWT secrets                                          | Isolates the server from the Modal environments where your DAG apps run        |
| Stardag **workspace**                        | named after your shared Modal workspace, or your personal workspace      | Mirrors your Modal account's structure                                         |
| Stardag **environment**                      | `main`                                                                   | Mirrors Modal's default environment; where deployed-DAG runs are tracked       |
| **API key** → Modal secret `stardag-api-key` | in Modal env `main` (your default)                                       | Lets Modal-executed DAGs authenticate against your registry                    |
| **Target root** `default`                    | `modalvol://stardag-targets-<workspace-slug>-<environment-slug>/default` | Where task outputs land (a dedicated Modal volume per workspace + environment) |
| Local **registry + profile** `selfhosted`    | in `~/.stardag/config.toml`                                              | Points your SDK/CLI at the deployment                                          |

Open the UI, sign in with the admin account, and deploy a DAG app with
[`stardag modal deploy`](integrate-modal.md) — the `stardag-api-key` secret
and target root are already in place. Everything in
[Use the API Registry](use-api-registry.md) applies from here.

Re-run the setup phase anytime (it's idempotent — existing workspaces,
environments, and keys are matched, not duplicated):

```bash
stardag self-host connect
```

## Updating to a new version

```bash
uvx --python 3.12 --from "stardag[selfhost]" stardag self-host upgrade
```

This applies any new database migrations and redeploys the API + UI. Each
SDK release pins the server version it was tested against, so `uvx` picking
up a newer SDK also rolls the server forward; pass
`--server-version X.Y.Z` (or `latest`) to deploy a specific
[server release](https://github.com/stardag-dev/stardag/releases). The JWT
keypair secret is left untouched, so existing sessions and SDK logins
survive upgrades.

If you deployed with a non-default `--name` or `--server-modal-env`, pass
the **same** values to `upgrade` (and `status`/`destroy`) — otherwise the
command looks in the wrong place and reports nothing deployed. In
particular, a deployment created before these defaults changed (app
`stardag-server` in Modal's default environment) is upgraded with
`--name stardag-server --server-modal-env ''`.

## Deploying from source (development)

The prebuilt image `ghcr.io/stardag-dev/stardag-server` is the default and
needs no repo checkout. To deploy your own modifications instead, clone the
repo and pass `--from-source`:

```bash
git clone https://github.com/stardag-dev/stardag.git
cd stardag
uvx --from "stardag[selfhost]" stardag self-host up --from-source
```

The image is then built from the checkout: the UI is compiled with npm
inside the Modal image build (no local Node needed) and the API package is
installed from source. The image's Python is matched to the interpreter
running the CLI (any Python ≥ 3.10 works — no `--python 3.12` needed in
this mode). `upgrade --from-source` redeploys after local changes or a
`git pull`.

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
uvx --python 3.12 --from "stardag[selfhost]" stardag self-host up --auth-mode oidc \
  --oidc-issuer https://<your-idp-issuer> \
  --oidc-ui-client-id <client-id>
```

In OIDC mode the post-deploy setup can't run server-side (there is no known
admin until someone signs in), so complete it afterwards — this opens the
browser for the OIDC login, then creates the workspace, `main` environment,
API key, and local profile exactly like the local-mode flow:

```bash
uvx --python 3.12 --from "stardag[selfhost]" stardag self-host connect
```

Provider setup (example: WorkOS AuthKit):

1. Create a WorkOS account → enable AuthKit.
2. Create a client application; note the **issuer URL** and **client ID**.
3. Add the redirect URI printed by `self-host up`:
   `https://<your-workspace>-stardag-host--server.modal.run/callback`
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
stardag self-host up         # provision + deploy + connect (idempotent; re-run to reconfigure)
stardag self-host connect    # (re)run the post-deploy setup only
stardag self-host upgrade    # migrate DB + redeploy
stardag self-host status     # deployment status + URL
stardag self-host destroy    # stop the Modal app (never touches the database)
```

Useful flags for `up` (all prompts have flag equivalents for CI):

- `--server-version X.Y.Z` (or `latest`) — prebuilt server image version to
  deploy (default: the version this SDK release is tested against)
- `--from-source` — build the image from a local repo checkout instead of
  the prebuilt image (see
  [Deploying from source](#deploying-from-source-development))
- `--name` — Modal app name / URL label (default `server`)
- `--neon-project` — Neon project name to find-or-create (default `stardag`)
- `--database-url` / `--database-url-direct` — bring your own Postgres
  instead of Neon (use `postgresql+asyncpg://...` URLs; pass the direct
  variant if the main URL goes through a transaction-mode pooler)
- `--keep-warm N` — always-on containers (initially 0 = scale to zero). The
  value is persisted: `up`/`upgrade` without the flag keep the last
  explicitly set value.
- `--server-modal-env NAME` — Modal environment for the server app + its
  secrets (default `stardag-host`, created if missing). Keeps the server
  isolated from the environments your DAG apps run in. Also accepted by
  `connect`/`upgrade`/`status`/`destroy`; pass `''` for Modal's default
  environment (deployments made with older SDK versions live there).
- `--yes` — non-interactive; takes the defaults and fails on required prompts

Setup flags shared by `up` and `connect` (each prompt/default has a flag
equivalent):

- `--primary-workspace NAME` — name of the shared Stardag workspace to
  create (default: your shared Modal workspace's name); or
  `--no-primary-workspace` to use only your personal workspace
- `--execution-modal-env NAME` — Modal environment your DAG apps run in;
  the `stardag-api-key` secret is pushed there (default: your Modal
  account's default environment). This is deliberately _not_ the server's
  environment.
- `--target-root name=uri` — default target root for the `main` environment
  (default
  `default=modalvol://stardag-targets-<workspace-slug>-<environment-slug>/default`);
  or `--no-target-root` to skip
- `--registry-name` / `--profile-name` — names for the local SDK config
  entries (both default `selfhosted`)
- `--skip-connect` (`up` only) — deploy without the setup phase; run
  `stardag self-host connect` later

## Troubleshooting

- **First request hangs a few seconds** — cold start (Modal container boot
  - Neon wake). Expected with scale-to-zero; use `--keep-warm 1` to avoid.
- **`InvalidError: The 'migrate' Function (using serialized=True) was defined with Python 3.X, but its Image has 3.12`**
  — the CLI's interpreter must match the server image's Python. For the
  prebuilt image, run under 3.12 (`uvx --python 3.12 ...` as in the
  quickstart — recent CLI versions catch this before deploying); with
  `--from-source` the image is built to match your interpreter
  automatically.
- **`Modal authentication not set up`** — run `uvx modal token new`.
- **`Neon API key rejected`** — create a key at
  [console.neon.tech/app/settings/api-keys](https://console.neon.tech/app/settings/api-keys).
- **Sign-in loops or 401s right after an upgrade** — stale cached tokens;
  sign out and in again. (The JWT keypair is preserved across upgrades, so
  this should be rare.)
- **Logs** — `uvx modal app logs server --env stardag-host` (the server
  lives in its own Modal environment; use `--env ''`/the app name you chose
  for deployments made with older SDK versions or a custom `--name`).
- **Migration failures** — `self-host up`/`upgrade` print the Alembic
  output; migrations always run against Neon's direct (non-pooled)
  endpoint. Re-running the command is safe (migrations are idempotent).
