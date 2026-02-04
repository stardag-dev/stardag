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
```

### Deployment Order

For first-time deployments, the correct order is:

1. Foundation stack (creates ECR, VPC, Database)
2. Push API image to ECR
3. Api and Frontend stacks (now image exists)
4. Run migrations
5. Update API service

The `deploy-all.sh` script handles this automatically.

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

| Feature     | Env Var       | Default | Description                                      |
| ----------- | ------------- | ------- | ------------------------------------------------ |
| SES (Email) | `SES_ENABLED` | `false` | AWS SES for transactional emails (invites, etc.) |

**SES (Email):** When enabled, creates an SES email identity for your domain with automatic DKIM DNS records. Requires the domain to be configured in Route 53. After deployment, you'll need to request SES production access in the AWS Console (sandbox mode only allows sending to verified emails).

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
