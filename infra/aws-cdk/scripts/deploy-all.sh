#!/bin/bash
# Full deployment: infrastructure + API + migrations + UI
# Usage: deploy-all.sh [--image-uri <uri>] [--release server-vX.Y.Z]
#   --image-uri <uri>: Use an explicit (typically prebuilt public) API
#       container image, e.g. ghcr.io/stardag-dev/stardag-server:X.Y.Z,
#       instead of building and pushing the API image locally.
#   --release server-vX.Y.Z: Deploy the prebuilt UI dist attached to that
#       GitHub release instead of building the UI locally with npm.
#       Requires the CloudFront same-origin API proxy
#       (STARDAG_UI_API_PROXY=true) — see deploy-ui.sh / README.md.
#
# Fully prebuilt deployment (no local docker/npm builds):
#   STARDAG_UI_API_PROXY=true ./scripts/deploy-all.sh \
#       --image-uri ghcr.io/stardag-dev/stardag-server:X.Y.Z \
#       --release server-vX.Y.Z
#
# Order ensures:
#   1. ECR exists before pushing image (local build mode)
#   2. Image exists before ECS service is created
#   3. Migrations run before API service starts
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse arguments
IMAGE_URI=""
RELEASE=""
while [ $# -gt 0 ]; do
    case $1 in
        --image-uri)
            IMAGE_URI="$2"
            if [ -z "$IMAGE_URI" ]; then
                echo "ERROR: --image-uri requires a value"
                exit 1
            fi
            shift 2
            ;;
        --release)
            RELEASE="$2"
            if [ -z "$RELEASE" ]; then
                echo "ERROR: --release requires a value (e.g. server-v0.1.0)"
                exit 1
            fi
            shift 2
            ;;
        *)
            echo "ERROR: Unknown argument: $1"
            echo "Usage: deploy-all.sh [--image-uri <uri>] [--release server-vX.Y.Z]"
            exit 1
            ;;
    esac
done

echo "=========================================="
echo "     Stardag Full Deployment"
echo "=========================================="
if [ -n "$IMAGE_URI" ]; then
    echo "API image: $IMAGE_URI (prebuilt, no local docker build)"
fi
if [ -n "$RELEASE" ]; then
    echo "UI dist:   from GitHub release $RELEASE (no local npm build)"
fi
echo ""

# Step 1: Deploy Foundation (VPC, Database, ECR, Cognito, DNS)
# This must come first to create ECR repository
echo "Step 1/6: Deploying Foundation infrastructure..."
echo ""
"$SCRIPT_DIR/deploy-infra.sh" --foundation-only
echo ""

# Step 2: Build and push API image (now ECR exists)
# Skipped when an explicit prebuilt image is used.
if [ -n "$IMAGE_URI" ]; then
    echo "Step 2/6: Skipping local API image build (--image-uri given)"
    echo ""
else
    echo "Step 2/6: Building and pushing API image..."
    echo ""
    "$SCRIPT_DIR/deploy-api.sh" --skip-update
    echo ""
fi

# Step 3: Deploy API and Frontend stacks (now image exists)
echo "Step 3/6: Deploying API and Frontend stacks..."
echo ""
if [ -n "$IMAGE_URI" ]; then
    "$SCRIPT_DIR/deploy-infra.sh" --all --image-uri "$IMAGE_URI"
else
    "$SCRIPT_DIR/deploy-infra.sh" --all
fi
echo ""

# Step 4: Run migrations (before API service uses new code)
echo "Step 4/6: Running database migrations..."
echo ""
"$SCRIPT_DIR/run-migrations.sh"
echo ""

# Step 5: Update API service (now safe to start new code)
echo "Step 5/6: Updating API service..."
echo ""
"$SCRIPT_DIR/update-api-service.sh"
echo ""

# Step 6: Deploy UI assets
echo "Step 6/6: Deploying UI..."
echo ""
if [ -n "$RELEASE" ]; then
    "$SCRIPT_DIR/deploy-ui.sh" --release "$RELEASE"
else
    "$SCRIPT_DIR/deploy-ui.sh"
fi
echo ""

echo "=========================================="
echo "     Deployment Complete!"
echo "=========================================="
echo ""
echo "Your Stardag instance is now running at:"
echo "  API: https://${API_SUBDOMAIN:-api}.${DOMAIN_NAME}"
echo "  UI:  https://${UI_SUBDOMAIN:-app}.${DOMAIN_NAME}"
echo ""
