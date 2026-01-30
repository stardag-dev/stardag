#!/bin/bash
# Trigger a new deployment of the API service (uses latest image from ECR)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CDK_DIR"

# Load config
if [ -f .env.deploy ]; then
    export $(grep -v '^#' .env.deploy | xargs)
fi

AWS_REGION="${AWS_REGION:-us-east-1}"

# Only use AWS_PROFILE if credentials aren't already set (CI uses OIDC env vars)
if [ -z "$AWS_ACCESS_KEY_ID" ]; then
    AWS_PROFILE="${AWS_PROFILE:-stardag}"
    AWS_CMD="aws --profile $AWS_PROFILE"
    echo "=== Updating API Service ==="
    echo "AWS Profile: $AWS_PROFILE"
else
    AWS_CMD="aws"
    echo "=== Updating API Service ==="
    echo "Using environment credentials (CI mode)"
fi
echo "Region: $AWS_REGION"
echo ""

# Get ECS cluster and service names
CLUSTER_NAME=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagApiClusterName'].Value" \
    --output text \
    --region $AWS_REGION)

SERVICE_NAME=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagApiServiceName'].Value" \
    --output text \
    --region $AWS_REGION)

echo "ECS Cluster: $CLUSTER_NAME"
echo "ECS Service: $SERVICE_NAME"
echo ""

# Update ECS service to trigger new deployment
echo "=== Triggering new deployment ==="
$AWS_CMD ecs update-service \
    --cluster $CLUSTER_NAME \
    --service $SERVICE_NAME \
    --force-new-deployment \
    --region $AWS_REGION

echo ""
echo "=== API service update initiated ==="
echo ""
echo "The ECS service is now updating. You can monitor progress with:"
echo "  $AWS_CMD ecs describe-services --cluster $CLUSTER_NAME --services $SERVICE_NAME --region $AWS_REGION"
echo ""
echo "Or check the AWS Console: https://$AWS_REGION.console.aws.amazon.com/ecs/v2/clusters/$CLUSTER_NAME/services/$SERVICE_NAME"
