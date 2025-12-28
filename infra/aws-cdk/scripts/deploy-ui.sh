#!/bin/bash
# Build and deploy UI to S3/CloudFront
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$CDK_DIR/../.." && pwd)"

cd "$CDK_DIR"

# Load config
if [ -f .env.deploy ]; then
    export $(grep -v '^#' .env.deploy | xargs)
fi

AWS_PROFILE="${AWS_PROFILE:-stardag}"
AWS_REGION="${AWS_REGION:-us-east-1}"

echo "=== Building and Deploying UI ==="
echo "AWS Profile: $AWS_PROFILE"
echo "Region: $AWS_REGION"
echo ""

# Get stack exports
echo "=== Getting stack exports ==="
BUCKET_NAME=$(AWS_PROFILE=$AWS_PROFILE aws cloudformation list-exports \
    --query "Exports[?Name=='StardagFrontendBucketName'].Value" \
    --output text \
    --region $AWS_REGION)

DISTRIBUTION_ID=$(AWS_PROFILE=$AWS_PROFILE aws cloudformation list-exports \
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
COGNITO_USER_POOL_ID=$(AWS_PROFILE=$AWS_PROFILE aws cloudformation list-exports \
    --query "Exports[?Name=='StardagCognitoUserPoolId'].Value" \
    --output text \
    --region $AWS_REGION 2>/dev/null || echo "")

COGNITO_CLIENT_ID=$(AWS_PROFILE=$AWS_PROFILE aws cloudformation list-exports \
    --query "Exports[?Name=='StardagCognitoClientId'].Value" \
    --output text \
    --region $AWS_REGION 2>/dev/null || echo "")

# Build the UI
echo "=== Building UI ==="
cd "$REPO_ROOT/app/stardag-ui"

# Set environment variables for the build
export VITE_OIDC_ISSUER="https://cognito-idp.${AWS_REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}"
export VITE_OIDC_CLIENT_ID="${COGNITO_CLIENT_ID}"
export VITE_OIDC_REDIRECT_URI="https://${UI_SUBDOMAIN:-app}.${DOMAIN_NAME}/callback"
export VITE_API_URL="https://${API_SUBDOMAIN:-api}.${DOMAIN_NAME}"

echo "OIDC Issuer: $VITE_OIDC_ISSUER"
echo "OIDC Client ID: $VITE_OIDC_CLIENT_ID"
echo "OIDC Redirect URI: $VITE_OIDC_REDIRECT_URI"
echo "API URL: $VITE_API_URL"
echo ""

# Install dependencies and build
npm ci
npm run build

# Deploy to S3
echo ""
echo "=== Uploading to S3 ==="
AWS_PROFILE=$AWS_PROFILE aws s3 sync ./dist s3://$BUCKET_NAME --delete --region $AWS_REGION

# Invalidate CloudFront cache
echo ""
echo "=== Invalidating CloudFront cache ==="
INVALIDATION_ID=$(AWS_PROFILE=$AWS_PROFILE aws cloudfront create-invalidation \
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
