# AWS Deployment

## Status

**completed** (initial deployment)

## Goal

**Primary:** Deploy Stardag API and UI (including DB and OIDC provider) to AWS - Make the API available at `api.stardag.com` and the UI at `app.stardag.com` (SAAS offering).

**Secondary:** Make it easy for anyone to reproduce the deployment to their own AWS account (Self-hosted).

## Current State (2025-12-28)

### Deployed Infrastructure

| Component | Status        | URL/Resource                    |
| --------- | ------------- | ------------------------------- |
| API       | ✅ Live       | https://api.stardag.com         |
| UI        | ✅ Live       | https://app.stardag.com         |
| Database  | ✅ Running    | Aurora Serverless v2 PostgreSQL |
| Auth      | ✅ Configured | Cognito with GitHub IdP         |
| DNS/SSL   | ✅ Active     | Route 53 + ACM certificates     |

### AWS Resources

**CloudFormation Stacks:**

- `StardagFoundation` - VPC, Database, Cognito, ECR, DNS/Certificates
- `StardagApi` - ECS Fargate cluster, ALB, auto-scaling
- `StardagFrontend` - S3 bucket, CloudFront CDN

**Key Resource IDs:**

- VPC: `vpc-0bbcaec48b665d773`
- ECS Cluster: `stardag`
- ECS Service: `stardag-api`
- ECR Repository: `763997220528.dkr.ecr.us-east-1.amazonaws.com/stardag-api`
- S3 Bucket: `stardag-ui-763997220528`
- CloudFront Distribution: `E215AUJ1JU6H4S`
- Cognito User Pool: `us-east-1_3BInI6b9g`
- Cognito Client ID: `7i4eji14kj5oikpup78fqkt8s2`
- Route 53 Hosted Zone: `Z0664658365XWOS9MG5WV`

### Bastion/Jump Host

**Not currently deployed.** Can be created on-demand for debugging/local development against RDS. See `infra/PRIVATE_README.md` for setup commands.

## Remaining TODOs

### High Priority

- [ ] **Database migrations**: Currently using admin credentials. Should:
  - Create `stardag_service` user via migration
  - Switch ECS to use service credentials (already configured in CDK, just needs the user)
- [ ] **CI/CD Pipeline**: Automate deployments
  - API: Build image → Push to ECR → Update ECS service
  - UI: Build with env vars → Sync to S3 → Invalidate CloudFront

### Medium Priority

- [ ] **Monitoring & Alerting**:
  - CloudWatch alarms for ECS health, database connections
  - SNS notifications for deployment failures
- [ ] **Logging improvements**:
  - Structured JSON logging in API
  - Log retention policies
- [ ] **Cost optimization**:
  - Consider NAT instance vs NAT Gateway ($35/month savings)
  - Review Aurora ACU scaling settings

### Low Priority

- [ ] **Google IdP**: Add as second identity provider in Cognito
- [ ] **Staging environment**: Separate stack with `staging-*` subdomains
- [ ] **Custom auth domain**: `auth.stardag.com` instead of Cognito managed domain
- [ ] **Move ECS Cluster to FoundationStack**: For faster API stack iterations

## Issues Encountered & Solutions

### 1. ECS Container Health Check Failure

**Problem:** Container health check used `curl` which isn't installed in `python:3.11-slim`.

**Solution:** Changed health check to use Python:

```typescript
healthCheck: {
  command: [
    "CMD-SHELL",
    "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\" || exit 1",
  ],
}
```

### 2. Database Credentials

**Problem:** CDK creates `stardag_service` secret, but Aurora only creates admin user automatically.

**Solution:** Temporarily using admin credentials. TODO: Create service user via migration.

### 3. UI API Configuration

**Problem:** UI used relative `/api/v1` URLs, but API is on different subdomain.

**Solution:** Added `VITE_API_BASE_URL` environment variable and `src/api/config.ts` to configure API base URL at build time.

### 4. CloudFormation Signature Expiration

**Problem:** Long-running deployments caused AWS signature expiration errors.

**Solution:** Delete stuck stack and redeploy. ECS service stabilization can take 5+ minutes.

### 5. Cognito Logout Missing client_id

**Problem:** Clicking logout redirected to Cognito `/error` page with "Required String parameter 'client_id' is not present".

**Root Cause:** Amazon Cognito's logout endpoint doesn't follow the standard OIDC logout flow. It requires:

- `client_id` parameter (not in standard OIDC)
- `logout_uri` instead of `post_logout_redirect_uri`

The `oidc-client-ts` library sends standard OIDC parameters which Cognito rejects.

**Solution:** Added Cognito-specific logout handling in the UI:

1. Added `VITE_COGNITO_DOMAIN` environment variable (e.g., `stardag.auth.us-east-1.amazoncognito.com`)
2. Detect Cognito vs Keycloak by checking if issuer contains "cognito-idp"
3. For Cognito: use `manager.removeUser()` then redirect to `https://{domain}/logout?client_id={id}&logout_uri={uri}`
4. For Keycloak: use standard `manager.signoutRedirect()`

Files changed:

- `app/stardag-ui/src/auth/config.ts` - added `COGNITO_DOMAIN`, `isCognitoIssuer()`, `getCognitoLogoutUrl()`
- `app/stardag-ui/src/context/AuthContext.tsx` - updated `logout()` to handle Cognito specially
- `infra/aws-cdk/scripts/deploy-ui.sh` - fetch and pass `VITE_COGNITO_DOMAIN`

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Internet                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
          ▼                   ▼                   ▼
   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
   │ CloudFront  │    │     ALB     │    │  Cognito    │
   │ app.stardag │    │ api.stardag │    │  (auth)     │
   └─────────────┘    └─────────────┘    └─────────────┘
          │                   │
          ▼                   ▼
   ┌─────────────┐    ┌─────────────┐
   │  S3 Bucket  │    │ ECS Fargate │
   │  (UI files) │    │  (API)      │
   └─────────────┘    └─────────────┘
                              │
                              ▼
                      ┌─────────────┐
                      │   Aurora    │
                      │ Serverless  │
                      │    (RDS)    │
                      └─────────────┘
```

## Files & Configuration

### CDK Project Structure

```
infra/aws-cdk/
├── bin/stardag.ts              # Entry point
├── lib/
│   ├── config.ts               # Configuration loader
│   ├── foundation-stack.ts     # VPC, DB, Cognito, ECR, DNS
│   ├── api-stack.ts            # ECS Fargate + ALB
│   └── frontend-stack.ts       # S3 + CloudFront
├── .env.deploy                 # Deployment secrets (gitignored)
├── .env.deploy.example         # Template
└── cdk.context.json            # CDK context (gitignored)
```

### Environment Variables

**API (ECS Task):**

- `STARDAG_API_DATABASE_HOST` - RDS endpoint
- `STARDAG_API_DATABASE_PORT` - 5432
- `STARDAG_API_DATABASE_NAME` - stardag
- `STARDAG_API_DATABASE_USER` - From Secrets Manager
- `STARDAG_API_DATABASE_PASSWORD` - From Secrets Manager
- `OIDC_ISSUER_URL` - Cognito issuer
- `OIDC_AUDIENCE` - Cognito client ID
- `STARDAG_API_CORS_ORIGINS` - Allowed origins

**UI (Build-time):**

- `VITE_API_BASE_URL` - https://api.stardag.com
- `VITE_OIDC_ISSUER` - Cognito issuer URL
- `VITE_OIDC_CLIENT_ID` - Cognito client ID
- `VITE_COGNITO_DOMAIN` - Cognito hosted UI domain (required for logout, e.g., `stardag.auth.us-east-1.amazoncognito.com`)

## Progress (Completed)

- [x] Phase 0: Prerequisites
  - [x] 0.1 AWS account setup
  - [x] 0.2 Route 53 hosted zone & nameserver update
  - [x] 0.3 GitHub OAuth app setup
  - [x] 0.4 Deployment config file
- [x] Phase 1: CDK project setup
  - [x] 1.1 Initialize project structure
  - [x] 1.2 Config loader & base stack
- [x] Phase 2: Networking & Database
  - [x] 2.1 VPC construct
  - [x] 2.2 Aurora Serverless construct
  - [ ] 2.3 DB initialization script (using admin creds for now)
- [x] Phase 3: Authentication (Cognito)
  - [x] 3.1 User Pool
  - [x] 3.2 App Client
  - [x] 3.3 Domain setup (managed domain)
  - [x] 3.4 GitHub IdP
- [x] Phase 4: API (ECS Fargate)
  - [x] 4.1 ECR repository
  - [x] 4.2 ECS cluster & service
  - [x] 4.3 ALB setup
  - [ ] 4.4 Migration task (manual for now)
- [x] Phase 5: Frontend (S3 + CloudFront)
  - [x] 5.1 S3 bucket
  - [x] 5.2 CloudFront distribution
- [x] Phase 6: DNS & SSL
  - [x] 6.1 ACM certificates
  - [x] 6.2 Route 53 records
- [ ] Phase 7: Scripts & Documentation
  - [ ] 7.1 Deployment scripts
  - [x] 7.2 README documentation (PRIVATE_README.md)

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

### Deployment Commands Quick Reference

```bash
# Deploy all stacks
cd infra/aws-cdk
AWS_PROFILE=stardag npx cdk deploy --all

# Deploy specific stack
AWS_PROFILE=stardag npx cdk deploy StardagApi

# Build and push API image
cd app/stardag-api
AWS_PROFILE=stardag aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 763997220528.dkr.ecr.us-east-1.amazonaws.com
docker build -t stardag-api .
docker tag stardag-api:latest 763997220528.dkr.ecr.us-east-1.amazonaws.com/stardag-api:latest
docker push 763997220528.dkr.ecr.us-east-1.amazonaws.com/stardag-api:latest

# Force ECS to pull new image
AWS_PROFILE=stardag aws ecs update-service --cluster stardag --service stardag-api --force-new-deployment

# Build and deploy UI
cd app/stardag-ui
VITE_API_BASE_URL=https://api.stardag.com \
VITE_OIDC_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_3BInI6b9g \
VITE_OIDC_CLIENT_ID=7i4eji14kj5oikpup78fqkt8s2 \
VITE_COGNITO_DOMAIN=stardag.auth.us-east-1.amazoncognito.com \
npm run build
AWS_PROFILE=stardag aws s3 sync ./dist s3://stardag-ui-763997220528 --delete
AWS_PROFILE=stardag aws cloudfront create-invalidation --distribution-id E215AUJ1JU6H4S --paths "/*"
```

See `infra/PRIVATE_README.md` for complete command reference.
