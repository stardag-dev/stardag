#!/bin/bash
# Build, push, and deploy API to ECS
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

echo "=== Building and Deploying API ==="
echo "AWS Profile: $AWS_PROFILE"
echo "Region: $AWS_REGION"
echo ""

# Get ECR repository URI from CloudFormation exports
echo "=== Getting ECR repository URI ==="
ECR_URI=$(AWS_PROFILE=$AWS_PROFILE aws cloudformation list-exports \
    --query "Exports[?Name=='StardagEcrRepositoryUri'].Value" \
    --output text \
    --region $AWS_REGION)

if [ -z "$ECR_URI" ] || [ "$ECR_URI" == "None" ]; then
    echo "ERROR: Could not find ECR repository URI. Have you deployed the infrastructure?"
    exit 1
fi

echo "ECR Repository: $ECR_URI"

# Get ECS cluster and service names
CLUSTER_NAME=$(AWS_PROFILE=$AWS_PROFILE aws cloudformation list-exports \
    --query "Exports[?Name=='StardagApiClusterName'].Value" \
    --output text \
    --region $AWS_REGION)

SERVICE_NAME=$(AWS_PROFILE=$AWS_PROFILE aws cloudformation list-exports \
    --query "Exports[?Name=='StardagApiServiceName'].Value" \
    --output text \
    --region $AWS_REGION)

echo "ECS Cluster: $CLUSTER_NAME"
echo "ECS Service: $SERVICE_NAME"
echo ""

# Login to ECR
echo "=== Logging in to ECR ==="
AWS_PROFILE=$AWS_PROFILE aws ecr get-login-password --region $AWS_REGION | \
    docker login --username AWS --password-stdin "${ECR_URI%%/*}"

# Build the API image
echo ""
echo "=== Building API Docker image ==="
cd "$REPO_ROOT/app/stardag-api"

IMAGE_TAG="${IMAGE_TAG:-latest}"
docker build -t stardag-api:$IMAGE_TAG .

# Tag and push to ECR
echo ""
echo "=== Pushing to ECR ==="
docker tag stardag-api:$IMAGE_TAG $ECR_URI:$IMAGE_TAG
docker push $ECR_URI:$IMAGE_TAG

# Update ECS service to use new image
echo ""
echo "=== Updating ECS service ==="
AWS_PROFILE=$AWS_PROFILE aws ecs update-service \
    --cluster $CLUSTER_NAME \
    --service $SERVICE_NAME \
    --force-new-deployment \
    --region $AWS_REGION

echo ""
echo "=== API deployment initiated ==="
echo ""
echo "The ECS service is now updating. You can monitor progress with:"
echo "  AWS_PROFILE=$AWS_PROFILE aws ecs describe-services --cluster $CLUSTER_NAME --services $SERVICE_NAME --region $AWS_REGION"
echo ""
echo "Or check the AWS Console: https://$AWS_REGION.console.aws.amazon.com/ecs/v2/clusters/$CLUSTER_NAME/services/$SERVICE_NAME"
