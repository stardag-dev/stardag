#!/bin/bash
# Build, push, and deploy API to ECS
# Usage: deploy-api.sh [--skip-update] [--image-uri <uri>]
#   --skip-update: Only build and push, don't update the ECS service
#   --image-uri <uri>: Skip the local docker build+push entirely and deploy
#       the given (typically prebuilt public) image, e.g.
#       ghcr.io/stardag-dev/stardag-server:X.Y.Z
#       This runs `cdk deploy StardagApi -c apiImageUri=<uri>` so the ECS
#       task definition points at the image directly (no ECR involved).
#       Recommended for production: mirror the image through an ECR
#       pull-through cache first — see infra/aws-cdk/README.md.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$CDK_DIR/../.." && pwd)"

# Parse arguments
SKIP_UPDATE=false
IMAGE_URI=""
while [ $# -gt 0 ]; do
    case $1 in
        --skip-update)
            SKIP_UPDATE=true
            shift
            ;;
        --image-uri)
            IMAGE_URI="$2"
            if [ -z "$IMAGE_URI" ]; then
                echo "ERROR: --image-uri requires a value"
                exit 1
            fi
            shift 2
            ;;
        *)
            echo "ERROR: Unknown argument: $1"
            echo "Usage: deploy-api.sh [--skip-update] [--image-uri <uri>]"
            exit 1
            ;;
    esac
done

if [ -n "$IMAGE_URI" ] && [ "$SKIP_UPDATE" = true ]; then
    echo "ERROR: --skip-update only applies to the local build+push flow;"
    echo "it cannot be combined with --image-uri."
    exit 1
fi

cd "$CDK_DIR"

# Load config
if [ -f .env.deploy ]; then
    export $(grep -v '^#' .env.deploy | xargs)
fi

AWS_REGION="${AWS_REGION:-us-east-1}"

# Only use AWS_PROFILE if credentials aren't already set (CI uses OIDC env vars)
if [ -z "$AWS_ACCESS_KEY_ID" ]; then
    AWS_PROFILE="${AWS_PROFILE:-stardag}"
    export AWS_PROFILE
    AWS_CMD="aws --profile $AWS_PROFILE"
    echo "=== Deploying API ==="
    echo "AWS Profile: $AWS_PROFILE"
else
    # Unset AWS_PROFILE to ensure env credentials are used (also by cdk)
    unset AWS_PROFILE
    AWS_CMD="aws"
    echo "=== Deploying API ==="
    echo "Using environment credentials (CI mode)"
fi
echo "Region: $AWS_REGION"

# =============================================================
# Prebuilt image mode: no local docker build, no ECR push.
# The image URI is baked into the ECS task definition via CDK.
# =============================================================
if [ -n "$IMAGE_URI" ]; then
    echo "Mode: Prebuilt image (no local build)"
    echo "Image: $IMAGE_URI"
    echo ""
    echo "=== Deploying StardagApi stack with explicit image ==="
    npx cdk deploy StardagApi \
        --require-approval never \
        -c "apiImageUri=$IMAGE_URI"

    echo ""
    echo "=== API deployment initiated ==="
    echo ""
    echo "The ECS service now rolls out a task definition pointing at:"
    echo "  $IMAGE_URI"
    echo ""
    echo "NOTE: subsequent 'cdk deploy StardagApi' runs without"
    echo "-c apiImageUri=... will revert the service to the ECR :latest"
    echo "image. Pin the value in .env.deploy to make it stick:"
    echo "  STARDAG_API_IMAGE_URI=$IMAGE_URI"
    exit 0
fi

echo "Mode: Local docker build + push to ECR"
if [ "$SKIP_UPDATE" = true ]; then
    echo "Mode: Build and push only (--skip-update)"
fi
echo ""

# Get ECR repository URI from CloudFormation exports
echo "=== Getting ECR repository URI ==="
ECR_URI=$($AWS_CMD cloudformation list-exports \
    --query "Exports[?Name=='StardagEcrRepositoryUri'].Value" \
    --output text \
    --region $AWS_REGION)

if [ -z "$ECR_URI" ] || [ "$ECR_URI" == "None" ]; then
    echo "ERROR: Could not find ECR repository URI. Have you deployed the infrastructure?"
    exit 1
fi

echo "ECR Repository: $ECR_URI"

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

# Login to ECR
echo "=== Logging in to ECR ==="
$AWS_CMD ecr get-login-password --region $AWS_REGION | \
    docker login --username AWS --password-stdin "${ECR_URI%%/*}"

# Resolve the server version to stamp into the image (surfaced at
# GET /api/v1/version; otherwise the deployment reports "dev").
#
# Honor an already-exported $STARDAG_SERVER_VERSION; otherwise derive it from
# scripts/server-version.sh (git-describe). Never fail the deploy over
# versioning: if the script is missing or errors, fall back to "dev".
#
# CAVEAT: server-version.sh needs the `server-v*` tags present in the checkout
# to produce a clean X.Y.Z (or X.Y.Z+N.g<sha>); a shallow/tagless CI checkout
# yields 0.0.0+g<sha> or "dev". CI should check out with full history and tags
# (e.g. actions/checkout with fetch-depth: 0, including for submodules) for a
# clean version. It degrades gracefully otherwise. See infra/aws-cdk/README.md.
if [ -z "$STARDAG_SERVER_VERSION" ]; then
    STARDAG_SERVER_VERSION="$(bash "$REPO_ROOT/scripts/server-version.sh" 2>/dev/null || echo dev)"
fi
echo "Server version (STARDAG_SERVER_VERSION): $STARDAG_SERVER_VERSION"

# Build the API image
echo ""
echo "=== Building API Docker image ==="
cd "$REPO_ROOT/app/stardag-api"

IMAGE_TAG="${IMAGE_TAG:-latest}"
docker build \
    --build-arg STARDAG_SERVER_VERSION="$STARDAG_SERVER_VERSION" \
    -t stardag-api:$IMAGE_TAG .

# Tag and push to ECR
echo ""
echo "=== Pushing to ECR ==="
docker tag stardag-api:$IMAGE_TAG $ECR_URI:$IMAGE_TAG
docker push $ECR_URI:$IMAGE_TAG

# Update ECS service to use new image (unless --skip-update was specified)
if [ "$SKIP_UPDATE" = true ]; then
    echo ""
    echo "=== Skipping ECS service update (--skip-update) ==="
    echo ""
    echo "Image has been pushed to ECR. To update the service, run:"
    echo "  $AWS_CMD ecs update-service --cluster $CLUSTER_NAME --service $SERVICE_NAME --force-new-deployment --region $AWS_REGION"
else
    echo ""
    echo "=== Updating ECS service ==="
    $AWS_CMD ecs update-service \
        --cluster $CLUSTER_NAME \
        --service $SERVICE_NAME \
        --force-new-deployment \
        --region $AWS_REGION

    echo ""
    echo "=== API deployment initiated ==="
    echo ""
    echo "The ECS service is now updating. You can monitor progress with:"
    echo "  $AWS_CMD ecs describe-services --cluster $CLUSTER_NAME --services $SERVICE_NAME --region $AWS_REGION"
    echo ""
    echo "Or check the AWS Console: https://$AWS_REGION.console.aws.amazon.com/ecs/v2/clusters/$CLUSTER_NAME/services/$SERVICE_NAME"
fi
