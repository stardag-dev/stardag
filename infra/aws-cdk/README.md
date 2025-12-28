# Stardag AWS Infrastructure

AWS CDK infrastructure for deploying Stardag SAAS application.

## Architecture

- **Frontend:** S3 + CloudFront (`app.stardag.com`)
- **Backend:** ECS Fargate + ALB (`api.stardag.com`)
- **Database:** Aurora Serverless v2 PostgreSQL
- **Auth:** Cognito User Pool with GitHub IdP
- **DNS:** Route 53

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

# GitHub OAuth
GITHUB_CLIENT_ID=your-client-id
GITHUB_CLIENT_SECRET=your-client-secret
```

## Commands

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

# Deploy stack
npx cdk deploy --profile stardag

# Destroy stack (careful!)
npx cdk destroy --profile stardag
```

## Project Structure

```
infra/aws-cdk/
├── bin/
│   └── stardag.ts           # CDK app entry point
├── lib/
│   ├── config.ts            # Configuration loader
│   ├── stardag-stack.ts     # Main stack
│   └── constructs/          # Reusable constructs
├── test/
│   └── stardag.test.ts      # Stack tests
├── .env.deploy              # Your config (gitignored)
├── .env.deploy.example      # Config template
├── cdk.json                 # CDK configuration
└── package.json
```
