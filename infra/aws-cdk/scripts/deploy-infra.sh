#!/bin/bash
# Deploy CDK infrastructure to AWS
# Usage: deploy-infra.sh [--foundation-only | --all] [--image-uri <uri>]
#   --foundation-only: Deploy only Foundation stack (for first-time setup)
#   --all: Deploy all stacks (default for subsequent deployments)
#   --image-uri <uri>: Use an explicit (typically prebuilt public) API
#       container image, e.g. ghcr.io/stardag-dev/stardag-server:X.Y.Z.
#       Passed through to CDK as -c apiImageUri=<uri>. Alternatively set
#       STARDAG_API_IMAGE_URI in .env.deploy to pin it persistently.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$CDK_DIR"

# Parse arguments
# Default: deploy main stacks (Foundation, Api, Frontend) but NOT Bastion
STACKS="StardagFoundation StardagApi StardagFrontend"
IMAGE_URI=""
while [ $# -gt 0 ]; do
    case $1 in
        --foundation-only)
            STACKS="StardagFoundation"
            shift
            ;;
        --all)
            # Main stacks only, Bastion is deployed separately
            STACKS="StardagFoundation StardagApi StardagFrontend"
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
            echo "Usage: deploy-infra.sh [--foundation-only | --all] [--image-uri <uri>]"
            exit 1
            ;;
    esac
done

# Extra CDK context args (applies to synth and deploy)
CDK_CONTEXT_ARGS=()
if [ -n "$IMAGE_URI" ]; then
    CDK_CONTEXT_ARGS+=(-c "apiImageUri=$IMAGE_URI")
fi

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
echo "Stacks: $STACKS"
if [ -n "$IMAGE_URI" ]; then
    echo "API Image: $IMAGE_URI (explicit, no ECR build)"
fi
echo ""

# Synthesize first to catch errors
echo "=== Synthesizing CloudFormation template ==="
npx cdk synth --quiet "${CDK_CONTEXT_ARGS[@]}"

# Deploy
echo ""
echo "=== Deploying stack ==="
npx cdk deploy $STACKS --require-approval never "${CDK_CONTEXT_ARGS[@]}"

echo ""
echo "=== Infrastructure deployment complete ==="
echo ""
if [ "$STACKS" = "StardagFoundation" ]; then
    echo "Foundation stack deployed. Next steps:"
    echo "1. Run ./scripts/deploy-api.sh --skip-update to build and push the API image"
    echo "2. Run ./scripts/deploy-infra.sh --all to deploy remaining stacks"
    echo "3. Run ./scripts/run-migrations.sh to initialize the database"
    echo "4. Run ./scripts/update-api-service.sh to start the API"
else
    echo "All infrastructure deployed."
fi
