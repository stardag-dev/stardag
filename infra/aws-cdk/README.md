# Stardag AWS Infrastructure

AWS CDK infrastructure for deploying Stardag SAAS application.

## Architecture

- **Frontend:** S3 + CloudFront (`app.stardag.com`)
- **Backend:** ECS Fargate + ALB (`api.stardag.com`)
- **Database:** Aurora Serverless v2 PostgreSQL
- **Auth:** Cognito User Pool with Google IdP
- **DNS:** Route 53
- **Email:** AWS SES (optional, for transactional emails)

## Stacks

| Stack             | Description                            | Deployed by default   |
| ----------------- | -------------------------------------- | --------------------- |
| StardagFoundation | VPC, Database, Cognito, ECR, DNS/Certs | Yes                   |
| StardagApi        | ECS Fargate service + ALB              | Yes                   |
| StardagFrontend   | S3 + CloudFront for static UI          | Yes                   |
| StardagBastion    | EC2 bastion for database access        | No (deploy on-demand) |

## Deployment

### Full Deployment (Recommended)

Use the deployment scripts in `scripts/`:

```bash
# Full deployment (handles correct order)
./scripts/deploy-all.sh

# Individual components
./scripts/deploy-infra.sh          # Deploy all main stacks
./scripts/deploy-infra.sh --foundation-only  # Foundation only (first-time setup)
./scripts/deploy-api.sh            # Build, push, and deploy API
./scripts/deploy-ui.sh             # Build and deploy UI
./scripts/run-migrations.sh        # Run database migrations

# Prebuilt public release images (no local docker/npm builds)
# See "Use Prebuilt Public Images" below
./scripts/deploy-api.sh --image-uri ghcr.io/stardag-dev/stardag-server:X.Y.Z
./scripts/deploy-ui.sh --release server-vX.Y.Z
```

### Deployment Order

For first-time deployments, the correct order is:

1. Foundation stack (creates ECR, VPC, Database)
2. Push API image to ECR
3. Api and Frontend stacks (now image exists)
4. Run migrations
5. Update API service

The `deploy-all.sh` script handles this automatically.

## Server version stamping

`deploy-api.sh` stamps a server version into the locally-built API image via
the `STARDAG_SERVER_VERSION` build arg, so the deployment reports a truthful
version at `GET /api/v1/version` instead of the `"dev"` default.

The version is resolved in this order:

1. An already-exported `$STARDAG_SERVER_VERSION` (set it explicitly to override).
2. Otherwise `scripts/server-version.sh` (git-describe against `server-v*` tags):
   - exactly at a `server-vX.Y.Z` tag → `X.Y.Z`
   - `N` commits past the nearest tag → `X.Y.Z+N.g<sha>`
   - no `server-v*` tag reachable → `0.0.0+g<sha>`
   - not a git checkout → `dev`
3. If the script is missing or errors, it falls back to `"dev"` — versioning
   never fails the deploy.

**CI caveat — check out tags.** A clean tagged version requires the `server-v*`
tags to be present in the checkout. A shallow or tagless CI checkout yields
`0.0.0+g<sha>` or `"dev"`. In CI, check out with full history and tags — e.g.
`actions/checkout` with `fetch-depth: 0` (and the same for any submodule
checkout that contains this repo) — so `server-version.sh` can find the tags.
Deployments degrade gracefully to a sha-based or `dev` version otherwise; no
consumer code change is needed beyond ensuring tags are fetched.

Prebuilt public images (`--image-uri …`) are already stamped at publish time
by the release workflow, so this only concerns the local docker build path.

## Use Prebuilt Public Images (no local builds)

Each server release (git tag `server-vX.Y.Z`) publishes:

- A combined server container image: `ghcr.io/stardag-dev/stardag-server:X.Y.Z`
  (Registry API + built web UI served same-origin + Alembic migrations).
- The built UI dist as a GitHub Release asset: `stardag-ui-dist-X.Y.Z.tar.gz`.

These let you deploy without Docker or Node installed locally.

### API from a prebuilt image

```bash
# One-off deploy of a specific release image
./scripts/deploy-api.sh --image-uri ghcr.io/stardag-dev/stardag-server:0.1.0

# Or pass through the infra deploy (used by deploy-all.sh)
./scripts/deploy-infra.sh --all --image-uri ghcr.io/stardag-dev/stardag-server:0.1.0
```

Under the hood this is a CDK context value (`-c apiImageUri=<uri>`), so it
also works with plain CDK commands:

```bash
npx cdk deploy StardagApi -c apiImageUri=ghcr.io/stardag-dev/stardag-server:0.1.0
```

To make the choice persistent (so later deploys don't silently fall back to
the ECR `:latest` image), pin it in `.env.deploy`:

```bash
STARDAG_API_IMAGE_URI=ghcr.io/stardag-dev/stardag-server:0.1.0
```

Notes:

- **Always pin an explicit version** (`:0.1.0`), never a moving tag. ECS
  resolves the tag at task start, so a moving tag would make scale-out
  events and task replacements pull a different build than the running
  tasks. Upgrades are then an explicit `.env.deploy` edit + `cdk deploy`.
- The combined `stardag-server` image also contains the built web UI
  (the API serves it same-origin when no external UI is configured). That
  is harmless here: this CDK setup keeps serving the UI via S3 +
  CloudFront, and the image is a strict superset of the API-only image —
  including `alembic.ini` + `migrations/` at the same working directory,
  so `run-migrations.sh` works unchanged.
- `run-migrations.sh` overrides the container command with
  `alembic upgrade head` (configurable via `MIGRATION_COMMAND` for images
  with a different layout).

### Recommended for production: ECR pull-through cache

Pulling from `ghcr.io` puts an external registry on your ECS scale-up /
recovery path (rate limits, availability). The recommended production
pattern is an ECR pull-through cache: ECS pulls from your own ECR, which
transparently mirrors GHCR on first pull.

```bash
# 1. Store GitHub credentials for GHCR upstream (a PAT with read:packages;
#    required by ECR for ghcr.io upstreams). The secret name must start
#    with "ecr-pullthroughcache/".
aws secretsmanager create-secret \
    --name ecr-pullthroughcache/ghcr \
    --secret-string '{"username":"<github-username>","accessToken":"<github-pat>"}'

# 2. Create the pull-through cache rule
aws ecr create-pull-through-cache-rule \
    --ecr-repository-prefix ghcr \
    --upstream-registry-url ghcr.io \
    --credential-arn arn:aws:secretsmanager:<region>:<account-id>:secret:ecr-pullthroughcache/ghcr-xxxxxx

# 3. Prime the cache (first pull creates the repo and mirrors the image)
aws ecr get-login-password --region <region> | \
    docker login --username AWS --password-stdin <account-id>.dkr.ecr.<region>.amazonaws.com
docker pull <account-id>.dkr.ecr.<region>.amazonaws.com/ghcr/stardag-dev/stardag-server:0.1.0

# 4. Use the cached URI as the API image
./scripts/deploy-api.sh \
    --image-uri <account-id>.dkr.ecr.<region>.amazonaws.com/ghcr/stardag-dev/stardag-server:0.1.0
```

The ECS task execution role created by CDK already has ECR pull
permissions via the managed `AmazonECSTaskExecutionRolePolicy`; add
`ecr:BatchImportUpstreamImage` on the cache repositories if you want ECS
itself (rather than a manual `docker pull`) to trigger first-time imports.

### UI from a prebuilt release dist

```bash
./scripts/deploy-ui.sh --release server-v0.1.0
```

This downloads `stardag-ui-dist-0.1.0.tar.gz` from the GitHub release
(via `gh` if installed, else `curl`) and syncs it to S3 — no `npm` needed.

**Important caveat — same-origin requirement:** the prebuilt dist contains
no baked `VITE_*` configuration. It resolves auth/API configuration at
runtime from `GET /api/v1/auth/config` on its own origin
(`window.location.origin`). In this CDK setup the UI (CloudFront) and API
(ALB) are different origins by default, so the prebuilt dist only works if
CloudFront routes `/api/*` (plus `/health` and `/.well-known/*`) to the
API. Enable the same-origin proxy and redeploy the Frontend stack first:

```bash
# via .env.deploy: STARDAG_UI_API_PROXY=true, then ./scripts/deploy-infra.sh
# or one-off:
npx cdk deploy StardagFrontend -c uiApiProxy=true
```

`deploy-ui.sh --release` verifies the deployed distribution has the
`/api/*` behavior and refuses to deploy without it
(`--skip-same-origin-check` overrides). The proxy requires DNS to be
configured (the CloudFront origin points at the API custom domain).

When the proxy is enabled, SPA routing switches from distribution-wide
403/404 → `index.html` error responses to a CloudFront viewer-request
function on the S3 behavior (otherwise legitimate API 403/404 responses
would be rewritten to the app shell). The function serves `/assets/*` and
root-level files with a known static extension as-is and rewrites every
other path to `/index.html`, so client routes may safely contain dots; if
the UI dist ever ships static files outside `/assets/` with a new
extension, add it to the allowlist in `lib/frontend-stack.ts`.

The locally-built flow (plain `./scripts/deploy-ui.sh`) is unaffected and
does not need the proxy: it bakes `VITE_API_BASE_URL` etc. at build time.

### Fully prebuilt deployment

```bash
STARDAG_UI_API_PROXY=true ./scripts/deploy-all.sh \
    --image-uri ghcr.io/stardag-dev/stardag-server:0.1.0 \
    --release server-v0.1.0
```

Keep the API image version and the UI `--release` tag in lockstep (same
`X.Y.Z`) unless a release note says otherwise.

## Operations

### View Logs

```bash
# API logs (last 30 minutes)
AWS_PROFILE=stardag aws logs tail /stardag/api --since 30m --region us-east-1

# Follow logs in real-time
AWS_PROFILE=stardag aws logs tail /stardag/api --follow --region us-east-1
```

### Run Migrations

```bash
./scripts/run-migrations.sh
```

### Database Access (Bastion Host)

For direct database access, deploy the optional bastion stack:

```bash
# Deploy bastion
npx cdk deploy StardagBastion --profile stardag

# The stack outputs commands - run them in order:
# 1. Allow bastion to access DB (run the Step1_AllowDBAccess output)
aws ec2 authorize-security-group-ingress --group-id <db-sg-id> --protocol tcp --port 5432 --source-group <bastion-sg-id> --profile stardag

# 2. Connect via SSM (run the Step2_Connect output)
aws ssm start-session --target <instance-id> --profile stardag

# 3. Once connected, access the database (run the Step3_AccessDB output):
psql -h <db-endpoint> -U stardag_admin -d stardag

# 4. When done, clean up and destroy:
# First revoke DB access (run the Cleanup_RevokeDBAccess output)
aws ec2 revoke-security-group-ingress --group-id <db-sg-id> --protocol tcp --port 5432 --source-group <bastion-sg-id> --profile stardag

# Then destroy the stack
npx cdk destroy StardagBastion --profile stardag
```

The bastion host:

- Uses SSM Session Manager (no inbound ports, no SSH keys)
- Has PostgreSQL 16 client pre-installed
- Runs t3.micro (minimal cost)
- Requires manual security group rule (outputs the commands)
- Should be destroyed when not in use

### Run Ad-hoc Database Commands

For one-off database operations without deploying bastion:

```bash
# Example: Drop all tables
aws ecs run-task \
    --cluster stardag \
    --task-definition <task-def-arn> \
    --launch-type FARGATE \
    --network-configuration "..." \
    --overrides '{
        "containerOverrides": [{
            "name": "Api",
            "command": ["python", "-c", "..."]
        }]
    }'
```

## Prerequisites

1. AWS CLI configured with profile `stardag`:

   ```bash
   aws configure --profile stardag
   ```

2. CDK bootstrapped in your account:

   ```bash
   npx cdk bootstrap --profile stardag
   ```

3. Create `.env.deploy` from template:
   ```bash
   cp .env.deploy.example .env.deploy
   # Edit .env.deploy with your values
   ```

## Configuration

Create `.env.deploy` with your deployment configuration:

```bash
# AWS Configuration
AWS_ACCOUNT_ID=123456789012
AWS_REGION=us-east-1
AWS_PROFILE=stardag

# Domain Configuration
DOMAIN_NAME=stardag.com
API_SUBDOMAIN=api
UI_SUBDOMAIN=app

# Google OAuth
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret

# Optional Features
SES_ENABLED=true  # Enable AWS SES for transactional emails
```

### Optional Features

| Feature             | Env Var                     | Default | Description                                       |
| ------------------- | --------------------------- | ------- | ------------------------------------------------- |
| SES (Email)         | `SES_ENABLED`               | `false` | AWS SES for transactional emails (invites, etc.)  |
| Self-service signup | `COGNITO_ALLOW_SELF_SIGNUP` | `false` | Allow anyone to register in the Cognito user pool |

**SES (Email):** When enabled, creates an SES email identity for your domain with automatic DKIM DNS records. Requires the domain to be configured in Route 53. After deployment, you'll need to request SES production access in the AWS Console (sandbox mode only allows sending to verified emails).

### Restricting who can sign up

This deployment is reachable from the public internet, so **who can obtain an
account is a security decision**. The Registry API auto-provisions an internal
user and a personal workspace on first login for any principal Cognito
authenticates, so account creation in the pool is effectively account creation
in Stardag.

**Default is closed.** `COGNITO_ALLOW_SELF_SIGNUP` defaults to `false`, which
sets the user pool's `selfSignUpEnabled: false` — the public `SignUp` API is
disabled and native (email/password) users can only be created by an admin
(`aws cognito-idp admin-create-user`, or the Cognito console). Set
`COGNITO_ALLOW_SELF_SIGNUP=true` only for a deployment that deliberately offers
open registration (e.g. a hosted trial).

> **Important — federated (Google) sign-up is a separate door.** Disabling
> self-signup blocks the _native_ `SignUp` API only. When a Google (or other
> federated) IdP is configured, Cognito **still auto-provisions a pool user on
> first federated login**, regardless of `selfSignUpEnabled`. So with the
> default Google IdP, `COGNITO_ALLOW_SELF_SIGNUP=false` alone does **not** stop
> anyone with a Google account from signing in and getting an account.
>
> To actually restrict federated sign-up, do one (or both) of:
>
> - **Add a pre-sign-up Lambda trigger** on the user pool that rejects sign-ups
>   whose email is outside an allowlist/domain. The trigger fires for federated
>   sign-ups too (`triggerSource == "PreSignUp_ExternalProvider"`); `throw` to
>   deny. This is the robust, IdP-independent control.
> - **Restrict at the IdP** — e.g. set the Google OAuth consent screen to
>   _Internal_ for a Workspace org, or limit it to explicit test users, so only
>   your org's Google accounts can complete the flow.
>
> If neither is in place, treat the deployment as open-registration regardless
> of the `COGNITO_ALLOW_SELF_SIGNUP` value.

## CDK Commands

```bash
# Install dependencies
npm install

# Build TypeScript
npm run build

# Run tests
npm test

# Synthesize CloudFormation template
npx cdk synth --profile stardag

# Compare deployed stack with current state
npx cdk diff --profile stardag

# Deploy specific stack
npx cdk deploy StardagFoundation --profile stardag

# Deploy all main stacks
npx cdk deploy StardagFoundation StardagApi StardagFrontend --profile stardag

# Destroy stack (careful!)
npx cdk destroy StardagBastion --profile stardag
```

## Project Structure

```
infra/aws-cdk/
├── bin/
│   └── stardag.ts           # CDK app entry point
├── lib/
│   ├── config.ts            # Configuration loader
│   ├── foundation-stack.ts  # VPC, Database, Auth, ECR
│   ├── api-stack.ts         # ECS Fargate + ALB
│   ├── frontend-stack.ts    # S3 + CloudFront
│   ├── bastion-stack.ts     # Optional EC2 bastion
│   └── constructs/          # Reusable constructs
├── scripts/
│   ├── deploy-all.sh        # Full deployment
│   ├── deploy-infra.sh      # CDK stacks
│   ├── deploy-api.sh        # Build and push API
│   ├── deploy-ui.sh         # Build and deploy UI
│   └── run-migrations.sh    # Database migrations
├── test/
│   └── stardag.test.ts      # Stack tests
├── .env.deploy              # Your config (gitignored)
├── .env.deploy.example      # Config template
├── cdk.json                 # CDK configuration
└── package.json
```
