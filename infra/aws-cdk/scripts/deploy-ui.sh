#!/bin/bash
# Build and deploy UI to S3/CloudFront
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$CDK_DIR/../.." && pwd)"

cd "$CDK_DIR"

# Load config (required)
if [ ! -f .env.deploy ]; then
    echo "ERROR: .env.deploy file not found in $CDK_DIR"
    echo "This file is required and should contain at minimum:"
    echo "  DOMAIN_NAME=your-domain.com"
    echo ""
    echo "See .env.deploy.example for a template."
    exit 1
fi
export $(grep -v '^#' .env.deploy | xargs)

AWS_REGION="${AWS_REGION:-us-east-1}"

# Only use AWS_PROFILE if credentials aren't already set (CI uses OIDC env vars)
if [ -z "$AWS_ACCESS_KEY_ID" ]; then
    AWS_PROFILE="${AWS_PROFILE:-stardag}"
    AWS_CMD="aws --profile $AWS_PROFILE"
else
    AWS_CMD="aws"
fi

# Validate required variables
if [ -z "$DOMAIN_NAME" ]; then
    echo "ERROR: DOMAIN_NAME is not set in .env.deploy"
    exit 1
fi

echo "=== Building and Deploying UI ==="
if [ -z "$AWS_ACCESS_KEY_ID" ]; then
    echo "AWS Profile: $AWS_PROFILE"
else
    echo "Using environment credentials (CI mode)"
fi
echo "Region: $AWS_REGION"
echo ""

# Get stack exports
echo "=== Getting stack exports ==="
BUCKET_NAME=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagFrontendBucketName'].Value" \
    --output text \
    --region $AWS_REGION)

DISTRIBUTION_ID=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagFrontendDistributionId'].Value" \
    --output text \
    --region $AWS_REGION)

if [ -z "$BUCKET_NAME" ] || [ "$BUCKET_NAME" == "None" ]; then
    echo "ERROR: Could not find S3 bucket. Have you deployed the infrastructure?"
    exit 1
fi

echo "S3 Bucket: $BUCKET_NAME"
echo "CloudFront Distribution: $DISTRIBUTION_ID"
echo ""

# Get Cognito config for UI build
COGNITO_USER_POOL_ID=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagCognitoUserPoolId'].Value" \
    --output text \
    --region $AWS_REGION 2>/dev/null || echo "")

COGNITO_CLIENT_ID=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagCognitoClientId'].Value" \
    --output text \
    --region $AWS_REGION 2>/dev/null || echo "")

COGNITO_DOMAIN=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagCognitoDomain'].Value" \
    --output text \
    --region $AWS_REGION 2>/dev/null || echo "")

# Build the UI
echo "=== Building UI ==="
cd "$REPO_ROOT/app/stardag-ui"

# Set environment variables for the build
export VITE_OIDC_ISSUER="https://cognito-idp.${AWS_REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}"
export VITE_OIDC_CLIENT_ID="${COGNITO_CLIENT_ID}"
export VITE_OIDC_REDIRECT_URI="https://${UI_SUBDOMAIN:-app}.${DOMAIN_NAME}/callback"
export VITE_API_BASE_URL="https://${API_SUBDOMAIN:-api}.${DOMAIN_NAME}"
# Cognito domain for logout (Cognito uses non-standard logout endpoint)
export VITE_COGNITO_DOMAIN="${COGNITO_DOMAIN}"

echo "OIDC Issuer: $VITE_OIDC_ISSUER"
echo "OIDC Client ID: $VITE_OIDC_CLIENT_ID"
echo "OIDC Redirect URI: $VITE_OIDC_REDIRECT_URI"
echo "API Base URL: $VITE_API_BASE_URL"
echo "Cognito Domain: $VITE_COGNITO_DOMAIN"
echo ""

# Install dependencies and build
npm ci
npm run build

# Deploy to S3
echo ""
echo "=== Uploading to S3 ==="
$AWS_CMD s3 sync ./dist s3://$BUCKET_NAME --delete --region $AWS_REGION

# Invalidate CloudFront cache
echo ""
echo "=== Invalidating CloudFront cache ==="
INVALIDATION_ID=$($AWS_CMD cloudfront create-invalidation \
    --distribution-id $DISTRIBUTION_ID \
    --paths "/*" \
    --query "Invalidation.Id" \
    --output text)

echo "Invalidation ID: $INVALIDATION_ID"

echo ""
echo "=== UI deployment complete ==="
echo ""
echo "Your UI is now available at:"
echo "  https://${UI_SUBDOMAIN:-app}.${DOMAIN_NAME}"
echo ""
echo "CloudFront invalidation is in progress. It may take a few minutes for changes to propagate."
