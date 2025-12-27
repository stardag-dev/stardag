# AWS Deployment

## Status

active

## Goal

**Primary:** Deploy Stardag API and UI (including DB and OIDC provider) to AWS - Make the API available at `api.stardag.com` and the UI at `app.stardag.com` (SAAS offering).

**Secondary:** Make it easy for anyone to reproduce the deployment to their own AWS account (Self-hosted).

## Instructions

Implement a reproducible way (infra as code) to deploy the full stardag application to AWS as a "SAAS" product.

Considerations/Guidelines:

- Needs to happen interactively against a real account - provide guidance for all manual steps required to get set up.
- IaC/CDK to the extent possible
- Decouple deployment specific config (specific target account/domain etc.) in a gitignore:d file/as env vars.

### AWS Stack (IaC)

Use AWS CDK (TypeScript)

Components:

- Frontend: React → S3 + CloudFront
- Backend: FastAPI container → ECS Fargate + ALB
- Database: RDS Postgres → Aurora Serverless v2
- Auth:
  - Cognito User Pool
  - Hosted UI enabled
  - GitHub IdP configured (start here, add Google later)
- Secrets: AWS Secrets Manager (DB creds)
- Networking: VPC, private subnets for DB

Outputs:

- FRONTEND_URL
- API_URL
- OIDC_ISSUER (Cognito)
- OIDC_CLIENT_ID

## Context

The [auth.md](auth.md) task established the authentication foundation with OIDC/JWT support. The application is designed to swap Keycloak (local) for Cognito (AWS) via environment variables only - no code changes needed.

### Current Application Architecture

| Component    | Technology                      | Port | Notes                                         |
| ------------ | ------------------------------- | ---- | --------------------------------------------- |
| API          | FastAPI + uvicorn               | 8000 | Container-ready, Dockerfile exists            |
| UI           | React + Vite → nginx            | 80   | Static build, OIDC config at build-time       |
| Database     | PostgreSQL 16                   | 5432 | Dual roles (admin/service), Alembic migration |
| Auth (local) | Keycloak                        | 8080 | Swappable to Cognito via env vars             |
| Auth (AWS)   | Cognito User Pool               | -    | Target for this deployment                    |
| Migrations   | Alembic (runs in API container) | -    | Separate run before API starts                |

### Key Configuration Variables

**API (runtime env vars):**

- `STARDAG_API_DATABASE_URL` - PostgreSQL connection string
- `OIDC_ISSUER_URL` - Cognito issuer URL (internal, for JWKS fetch)
- `OIDC_EXTERNAL_ISSUER_URL` - Cognito issuer URL (browser-facing)
- `OIDC_AUDIENCE` - Allowed audiences (comma-separated)

**UI (build-time args):**

- `VITE_OIDC_ISSUER` - Cognito issuer URL
- `VITE_OIDC_CLIENT_ID` - Cognito app client ID
- `VITE_OIDC_REDIRECT_URI` - `https://app.stardag.com/callback`

## Execution Plan

### Summary of Preparatory Analysis

**Application readiness:** ✅ Ready

- Dockerfiles exist for API and UI
- OIDC configuration is environment-driven
- Database roles (admin/service) match AWS best practices
- Health endpoints exist (`/health` on both services)

**Infrastructure requirements:**

1. **Domain:** stardag.com owned (name.com), needs Route 53 hosted zone setup
2. **SSL:** ACM certificates for api.stardag.com and app.stardag.com
3. **Auth:** Cognito with GitHub IdP (Google can be added later)
4. **Database:** Aurora Serverless v2 PostgreSQL (cost-effective, auto-scaling)
5. **Compute:** ECS Fargate for API (no server management)
6. **CDN:** CloudFront for UI (static assets)
7. **Secrets:** Secrets Manager for DB credentials

**CDK project location:** `infra/aws-cdk/`

---

### Phase 0: Prerequisites (Manual Setup)

These steps must be completed before running CDK deployment.

#### 0.1 AWS Account Setup

- [ ] Create/identify AWS account for deployment
- [ ] Configure AWS CLI with credentials (`aws configure`)
- [ ] Note down: Account ID, preferred region (e.g., `us-east-1`)

#### 0.2 Domain & DNS Setup

Since domain is owned at name.com:

1. Create Route 53 hosted zone for `stardag.com`
2. Note the NS records from Route 53
3. Update name.com nameservers to point to Route 53 NS records
4. Wait for DNS propagation (up to 48h, usually faster)

**Why Route 53?** Required for ACM DNS validation and simpler CDK integration.

#### 0.3 GitHub OAuth App Setup

Create OAuth App in GitHub:

1. Go to GitHub → Settings → Developer settings → OAuth Apps → New OAuth App
2. Configure:
   - **Application name:** Stardag
   - **Homepage URL:** `https://app.stardag.com`
   - **Authorization callback URL:** `https://<cognito-domain>.auth.<region>.amazoncognito.com/oauth2/idpresponse`
     (Will be known after Cognito deployment - can update later)
3. Note: Client ID and Client Secret

#### 0.4 Deployment Configuration File

Create `infra/aws-cdk/.env.deploy` (gitignored):

```bash
# AWS Configuration
AWS_ACCOUNT_ID=123456789012
AWS_REGION=us-east-1

# Domain Configuration
DOMAIN_NAME=stardag.com
API_SUBDOMAIN=api
UI_SUBDOMAIN=app

# GitHub OAuth (from step 0.3)
GITHUB_CLIENT_ID=Iv1.xxxxxxxxxxxx
GITHUB_CLIENT_SECRET=xxxxxxxxxxxxxxxx

# Optional: Google OAuth (add later)
# GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
# GOOGLE_CLIENT_SECRET=xxx
```

---

### Phase 1: CDK Project Setup

**Goal:** Create CDK project structure with shared configuration.

#### 1.1 Initialize CDK Project

```
infra/aws-cdk/
├── bin/
│   └── stardag.ts           # CDK app entry point
├── lib/
│   ├── config.ts            # Load deployment config
│   ├── stardag-stack.ts     # Main stack (or split into nested stacks)
│   ├── constructs/
│   │   ├── vpc.ts           # VPC construct
│   │   ├── database.ts      # Aurora construct
│   │   ├── cognito.ts       # Cognito construct
│   │   ├── api.ts           # ECS Fargate + ALB construct
│   │   └── frontend.ts      # S3 + CloudFront construct
│   └── ...
├── .env.deploy              # Deployment config (gitignored)
├── .env.deploy.example      # Template for deployment config
├── cdk.json
├── package.json
├── tsconfig.json
└── README.md                # Deployment instructions
```

#### 1.2 Core Dependencies

```json
{
  "dependencies": {
    "aws-cdk-lib": "^2.170.0",
    "constructs": "^10.0.0",
    "dotenv": "^16.0.0"
  }
}
```

---

### Phase 2: Networking & Database

**Goal:** VPC, subnets, security groups, Aurora Serverless.

#### 2.1 VPC Construct

- VPC with 2 AZs (cost optimization)
- Public subnets (ALB, NAT Gateway)
- Private subnets (ECS tasks, Aurora)
- Single NAT Gateway (cost optimization for MVP)

#### 2.2 Aurora Serverless v2 Construct

- Engine: PostgreSQL 16.x compatible
- Serverless v2 scaling: 0.5-4 ACU (auto-scales, cost-effective)
- Private subnet placement
- Security group: Allow from ECS tasks only
- Secrets Manager integration for credentials
- Initial database: `stardag`

#### 2.3 Database Initialization

Post-deployment script to create roles (similar to `docker/postgres/init.sql`):

- `stardag_admin` role (for migrations)
- `stardag_service` role (for API runtime)
- Grant appropriate permissions

---

### Phase 3: Authentication (Cognito)

**Goal:** Cognito User Pool with GitHub IdP.

#### 3.1 Cognito User Pool

- Password policy: Standard complexity
- Self-registration: Enabled
- Email verification: Optional (using federated login)
- MFA: Optional (user preference)

#### 3.2 User Pool Client

- Client name: `stardag-ui`
- Auth flows: Authorization code grant (with PKCE)
- Callback URLs: `https://app.stardag.com/callback`
- Logout URLs: `https://app.stardag.com`
- OAuth scopes: openid, email, profile
- Token validity: Access 24h, ID 24h, Refresh 30d

#### 3.3 Cognito Domain

- Custom domain: `auth.stardag.com` (requires ACM cert)
- Or managed domain: `stardag.auth.<region>.amazoncognito.com`

#### 3.4 GitHub Identity Provider

- Map GitHub attributes to Cognito:
  - `sub` → GitHub user ID
  - `email` → email
  - `name` → name (or login as fallback)

---

### Phase 4: API Deployment (ECS Fargate)

**Goal:** Containerized API with ALB, auto-scaling.

#### 4.1 ECR Repository

- Repository: `stardag-api`
- Lifecycle policy: Keep last 10 images

#### 4.2 ECS Cluster & Service

- Cluster: `stardag`
- Service: `stardag-api`
- Task definition:
  - CPU: 256 (0.25 vCPU)
  - Memory: 512 MB
  - Container: stardag-api from ECR
  - Port: 8000
  - Health check: `/health`
  - Environment from Secrets Manager
- Desired count: 1 (MVP), auto-scaling: 1-4

#### 4.3 Application Load Balancer

- Public-facing ALB
- HTTPS listener (ACM cert for api.stardag.com)
- HTTP → HTTPS redirect
- Target group: ECS service
- Health check: `/health`

#### 4.4 Database Migration Task

- One-off ECS task (runs before/alongside service update)
- Command: `alembic upgrade head`
- Uses `stardag_admin` credentials
- Can be triggered via CDK custom resource or manual CLI

---

### Phase 5: Frontend Deployment (S3 + CloudFront)

**Goal:** Static UI hosting with CDN.

#### 5.1 S3 Bucket

- Bucket: `stardag-ui-<account-id>`
- Block public access (CloudFront OAC)
- Versioning: Enabled

#### 5.2 CloudFront Distribution

- Origin: S3 bucket (OAC)
- Custom domain: `app.stardag.com`
- ACM certificate (us-east-1 for CloudFront)
- Default root object: `index.html`
- Error pages: 403/404 → `/index.html` (SPA routing)
- Cache behavior: Static assets cached, index.html no-cache

#### 5.3 API Proxy (Alternative Approach)

Option A: CloudFront origin for `/api/*` → ALB (single domain)
Option B: Separate domains (api.stardag.com, app.stardag.com) ✅ **Recommended**

Using separate domains is simpler and matches the current architecture.

---

### Phase 6: DNS & SSL Certificates

**Goal:** Route 53 records and ACM certificates.

#### 6.1 ACM Certificates

- `api.stardag.com` - For ALB (in deployment region)
- `app.stardag.com` - For CloudFront (must be us-east-1)
- `auth.stardag.com` - For Cognito custom domain (optional)

All using DNS validation via Route 53.

#### 6.2 Route 53 Records

- `api.stardag.com` → ALB alias
- `app.stardag.com` → CloudFront alias
- `auth.stardag.com` → Cognito domain (if custom domain)

---

### Phase 7: Deployment Scripts & Documentation

**Goal:** Make deployment reproducible.

#### 7.1 Deployment Scripts

```bash
# infra/aws-cdk/scripts/
├── deploy.sh              # Full deployment
├── build-and-push-api.sh  # Build & push API image to ECR
├── build-and-deploy-ui.sh # Build UI & sync to S3
├── run-migrations.sh      # Run Alembic migrations
└── init-db-roles.sh       # Initialize DB roles (one-time)
```

#### 7.2 Deployment README

Comprehensive guide covering:

1. Prerequisites checklist
2. Initial setup (one-time)
3. Infrastructure deployment (CDK)
4. Application deployment (containers/static files)
5. Post-deployment verification
6. Updating the deployment
7. Troubleshooting

---

### Implementation Order

| Step | Phase     | Description                      | Dependencies    |
| ---- | --------- | -------------------------------- | --------------- |
| 1    | Phase 0   | Manual prerequisites             | -               |
| 2    | Phase 1   | CDK project setup                | Phase 0         |
| 3    | Phase 6.1 | ACM certificates                 | Phase 0.2 (DNS) |
| 4    | Phase 2   | VPC & Database                   | Phase 1         |
| 5    | Phase 3   | Cognito                          | Phase 1, 0.3    |
| 6    | Phase 4   | API (ECS Fargate)                | Phase 2, 3      |
| 7    | Phase 5   | Frontend (S3 + CloudFront)       | Phase 3, 6.1    |
| 8    | Phase 6.2 | DNS records                      | Phase 4, 5      |
| 9    | Phase 7   | Scripts & docs                   | All             |
| 10   | -         | Update GitHub OAuth callback URL | Phase 3         |

---

## Decisions

| Decision                       | Rationale                                                                                 |
| ------------------------------ | ----------------------------------------------------------------------------------------- |
| Aurora Serverless v2           | Auto-scaling, pay-per-use, no instance management. Cost-effective for variable workloads. |
| Single NAT Gateway             | Cost optimization for MVP. Can add multi-AZ NAT later for HA.                             |
| GitHub IdP first               | Simpler setup than Google OAuth. Target audience (developers) likely has GitHub accounts. |
| Separate domains (api/app)     | Matches current architecture. Simpler than CloudFront multi-origin routing.               |
| Route 53 for DNS               | Required for ACM DNS validation. Simpler CDK integration.                                 |
| ECS Fargate over Lambda        | Better fit for long-running FastAPI app. Consistent with local Docker development.        |
| Manual CDK deployment          | Simpler initial setup. CI/CD can be added later.                                          |
| Production only                | Faster to production. Staging can be added as separate stack later.                       |
| Cognito custom domain optional | Managed domain works fine. Custom domain is nice-to-have.                                 |

## Progress

- [ ] Phase 0: Prerequisites
  - [ ] 0.1 AWS account setup
  - [ ] 0.2 Route 53 hosted zone & nameserver update
  - [ ] 0.3 GitHub OAuth app setup
  - [ ] 0.4 Deployment config file
- [ ] Phase 1: CDK project setup
  - [ ] 1.1 Initialize project structure
  - [ ] 1.2 Config loader & base stack
- [ ] Phase 2: Networking & Database
  - [ ] 2.1 VPC construct
  - [ ] 2.2 Aurora Serverless construct
  - [ ] 2.3 DB initialization script
- [ ] Phase 3: Authentication (Cognito)
  - [ ] 3.1 User Pool
  - [ ] 3.2 App Client
  - [ ] 3.3 Domain setup
  - [ ] 3.4 GitHub IdP
- [ ] Phase 4: API (ECS Fargate)
  - [ ] 4.1 ECR repository
  - [ ] 4.2 ECS cluster & service
  - [ ] 4.3 ALB setup
  - [ ] 4.4 Migration task
- [ ] Phase 5: Frontend (S3 + CloudFront)
  - [ ] 5.1 S3 bucket
  - [ ] 5.2 CloudFront distribution
- [ ] Phase 6: DNS & SSL
  - [ ] 6.1 ACM certificates
  - [ ] 6.2 Route 53 records
- [ ] Phase 7: Scripts & Documentation
  - [ ] 7.1 Deployment scripts
  - [ ] 7.2 README documentation

## Notes

### Cost Estimates (Monthly, MVP traffic)

| Service              | Estimate     | Notes                              |
| -------------------- | ------------ | ---------------------------------- |
| Aurora Serverless v2 | $15-50       | 0.5 ACU minimum, scales with usage |
| ECS Fargate          | $10-20       | 0.25 vCPU, 512MB, 1 task           |
| ALB                  | $20          | Fixed cost + LCU                   |
| CloudFront           | $1-5         | Depends on traffic                 |
| S3                   | <$1          | Static assets only                 |
| NAT Gateway          | $35          | Fixed + data transfer              |
| Route 53             | $0.50/zone   | Per hosted zone                    |
| Secrets Manager      | $0.40/secret | Per secret per month               |
| **Total**            | **~$80-130** | Low traffic MVP                    |

**Cost optimization options:**

- Use NAT instance instead of NAT Gateway (~$5 vs $35)
- Use RDS instead of Aurora for predictable workloads
- Reserved capacity for Fargate if usage is consistent

### Adding Google IdP Later

1. Create Google OAuth app in Google Cloud Console
2. Add `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` to `.env.deploy`
3. Update Cognito construct to include Google identity provider
4. Redeploy: `cdk deploy`

### Staging Environment

To add staging:

1. Create separate `.env.deploy.staging` with different subdomains
2. Deploy with: `DEPLOY_ENV=staging cdk deploy`
3. Uses `staging-api.stardag.com` and `staging-app.stardag.com`

### Rollback Procedure

1. **API:** ECS deployment automatically rolls back on health check failure
2. **UI:** CloudFront invalidation + S3 versioning allows rollback
3. **Database:** Aurora point-in-time recovery (automated backups)
4. **Infrastructure:** `cdk deploy` with previous commit
