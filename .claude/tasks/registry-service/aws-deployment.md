# [Task Name]

## Status

active

## Goal

Primary: Deploy Stardag API and UI (including DB and OIDC provider) to AWS - Make the API avaialable at `api.stardag.com` and the UI at `app.stardag.com` (SAAS offering).

Secondary: Make it easy for anyone to reproduce the deployment to their own AWS accont (Self hosted).

## Instructions

Implement a reproducible way (infra as code) to deploy the full stardag application to AWS as a "SAAS" product.

Considerations/Guidelines:

- Needs to happen interactively against a real account - provide guidance for all manual steps required to get set up.
- IaC/CDK to the extent possible
- Decouple deplyment specific config (specifc target account/domain etc.) in a gitignore:d file/as env vars.

### AWS Stack (IaC)

Use AWS CDK (TypeScript)

Components:

- Frontend: React → S3 + CloudFront
- Backend: FastAPI container → ECS Fargate + ALB
- Database: RDS Postgres -> Aurora
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

## Context

The [task auth.md](auth.md) covers AWS deployment considerations.

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
