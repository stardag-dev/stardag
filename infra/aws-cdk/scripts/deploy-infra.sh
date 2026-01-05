#!/bin/bash
# Deploy CDK infrastructure to AWS
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CDK_DIR"

# Load config
if [ -f .env.deploy ]; then
    export $(grep -v '^#' .env.deploy | xargs)
fi

# Only use AWS_PROFILE if credentials aren't already set (CI uses OIDC env vars)
echo "=== Deploying Stardag Infrastructure ==="
if [ -z "$AWS_ACCESS_KEY_ID" ]; then
    AWS_PROFILE="${AWS_PROFILE:-stardag}"
    export AWS_PROFILE
    echo "AWS Profile: $AWS_PROFILE"
else
    # Unset AWS_PROFILE to ensure env credentials are used
    unset AWS_PROFILE
    echo "Using environment credentials (CI mode)"
fi
echo "Region: $AWS_REGION"
echo "Account: $AWS_ACCOUNT_ID"
echo ""

# Synthesize first to catch errors
echo "=== Synthesizing CloudFormation template ==="
npx cdk synth --quiet

# Deploy
echo ""
echo "=== Deploying stack ==="
npx cdk deploy --all --require-approval never

echo ""
echo "=== Infrastructure deployment complete ==="
echo ""
echo "Next steps:"
echo "1. Run ./scripts/deploy-api.sh to build and push the API image"
echo "2. Run ./scripts/run-migrations.sh to initialize the database"
echo "3. Run ./scripts/deploy-ui.sh to deploy the UI"
