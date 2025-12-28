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

AWS_PROFILE="${AWS_PROFILE:-stardag}"

echo "=== Deploying Stardag Infrastructure ==="
echo "AWS Profile: $AWS_PROFILE"
echo "Region: $AWS_REGION"
echo "Account: $AWS_ACCOUNT_ID"
echo ""

# Synthesize first to catch errors
echo "=== Synthesizing CloudFormation template ==="
AWS_PROFILE=$AWS_PROFILE npx cdk synth --quiet

# Deploy
echo ""
echo "=== Deploying stack ==="
AWS_PROFILE=$AWS_PROFILE npx cdk deploy --require-approval never

echo ""
echo "=== Infrastructure deployment complete ==="
echo ""
echo "Next steps:"
echo "1. Run ./scripts/deploy-api.sh to build and push the API image"
echo "2. Run ./scripts/run-migrations.sh to initialize the database"
echo "3. Run ./scripts/deploy-ui.sh to deploy the UI"
