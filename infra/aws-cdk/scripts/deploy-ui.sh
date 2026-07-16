#!/bin/bash
# Build (or download) and deploy UI to S3/CloudFront
# Usage: deploy-ui.sh [--release server-vX.Y.Z] [--skip-same-origin-check]
#   (no args): Build the UI locally with npm, baking VITE_* config
#       (OIDC issuer, API base URL, ...) from the deployed stacks' outputs.
#   --release server-vX.Y.Z: Skip the local npm build and deploy the
#       prebuilt UI dist attached to the GitHub release with that tag
#       (asset: stardag-ui-dist-X.Y.Z.tar.gz).
#
#       IMPORTANT: the prebuilt dist contains NO baked VITE_* config. It
#       resolves auth/API configuration at runtime from
#       GET <window.location.origin>/api/v1/auth/config — which only
#       reaches the API if CloudFront forwards /api/* to it (the UI and
#       API are different origins in this setup otherwise). Deploy the
#       Frontend stack with the same-origin API proxy enabled first:
#           STARDAG_UI_API_PROXY=true ./scripts/deploy-infra.sh
#       (or: npx cdk deploy StardagFrontend -c uiApiProxy=true)
#       This script verifies the deployed distribution has the /api/*
#       behavior and refuses to deploy a prebuilt dist without it.
#   --skip-same-origin-check: Skip that verification (use only if you
#       know the distribution routes /api/* to the API).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$CDK_DIR/../.." && pwd)"

GITHUB_REPO="${STARDAG_GITHUB_REPO:-stardag-dev/stardag}"

# Parse arguments
RELEASE=""
SKIP_SAME_ORIGIN_CHECK=false
while [ $# -gt 0 ]; do
    case $1 in
        --release)
            RELEASE="$2"
            if [ -z "$RELEASE" ]; then
                echo "ERROR: --release requires a value (e.g. server-v0.1.0)"
                exit 1
            fi
            shift 2
            ;;
        --skip-same-origin-check)
            SKIP_SAME_ORIGIN_CHECK=true
            shift
            ;;
        *)
            echo "ERROR: Unknown argument: $1"
            echo "Usage: deploy-ui.sh [--release server-vX.Y.Z] [--skip-same-origin-check]"
            exit 1
            ;;
    esac
done

if [ -n "$RELEASE" ]; then
    case "$RELEASE" in
        server-v*) ;;
        *)
            echo "ERROR: --release expects a server release tag (server-vX.Y.Z), got: $RELEASE"
            exit 1
            ;;
    esac
fi

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

echo "=== Deploying UI ==="
if [ -z "$AWS_ACCESS_KEY_ID" ]; then
    echo "AWS Profile: $AWS_PROFILE"
else
    echo "Using environment credentials (CI mode)"
fi
echo "Region: $AWS_REGION"
if [ -n "$RELEASE" ]; then
    echo "Mode: Prebuilt dist from GitHub release $RELEASE"
else
    echo "Mode: Local npm build"
fi
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

if [ -n "$RELEASE" ]; then
    # =========================================================
    # Prebuilt dist mode
    # =========================================================
    VERSION="${RELEASE#server-v}"
    ASSET="stardag-ui-dist-${VERSION}.tar.gz"

    if [ "$SKIP_SAME_ORIGIN_CHECK" = true ]; then
        echo "=== Skipping same-origin check (--skip-same-origin-check) ==="
    else
        # The prebuilt dist has no baked VITE_* config: it fetches its
        # runtime config from /api/v1/auth/config on its own origin, so
        # CloudFront must route /api/* to the API.
        echo "=== Verifying CloudFront routes /api/* to the API ==="
        API_BEHAVIOR_COUNT=$($AWS_CMD cloudfront get-distribution-config \
            --id "$DISTRIBUTION_ID" \
            --query "length(DistributionConfig.CacheBehaviors.Items[?PathPattern=='/api/*'] || \`[]\`)" \
            --output text)
        if [ "$API_BEHAVIOR_COUNT" == "0" ] || [ -z "$API_BEHAVIOR_COUNT" ] || [ "$API_BEHAVIOR_COUNT" == "None" ]; then
            echo ""
            echo "ERROR: The CloudFront distribution has no /api/* behavior, so a"
            echo "prebuilt UI dist cannot reach the API (it has no baked VITE_*"
            echo "config and relies on same-origin runtime config)."
            echo ""
            echo "Enable the same-origin API proxy and redeploy the Frontend stack:"
            echo "  STARDAG_UI_API_PROXY=true ./scripts/deploy-infra.sh"
            echo "or:"
            echo "  npx cdk deploy StardagFrontend -c uiApiProxy=true"
            echo ""
            echo "(Or pass --skip-same-origin-check to override.)"
            exit 1
        fi
        echo "OK: /api/* behavior present"
        echo ""
    fi

    echo "=== Downloading $ASSET from release $RELEASE ==="
    DOWNLOAD_DIR="$(mktemp -d)"
    trap 'rm -rf "$DOWNLOAD_DIR"' EXIT

    if command -v gh > /dev/null 2>&1; then
        gh release download "$RELEASE" \
            --repo "$GITHUB_REPO" \
            --pattern "$ASSET" \
            --dir "$DOWNLOAD_DIR"
    else
        curl -fL --retry 3 \
            -o "$DOWNLOAD_DIR/$ASSET" \
            "https://github.com/$GITHUB_REPO/releases/download/$RELEASE/$ASSET"
    fi

    if [ ! -f "$DOWNLOAD_DIR/$ASSET" ]; then
        echo "ERROR: Failed to download $ASSET from release $RELEASE"
        exit 1
    fi

    echo ""
    echo "=== Extracting UI dist ==="
    EXTRACT_DIR="$DOWNLOAD_DIR/extract"
    mkdir -p "$EXTRACT_DIR"
    tar -xzf "$DOWNLOAD_DIR/$ASSET" -C "$EXTRACT_DIR"

    # Locate the dist root (index.html) — tolerate both a flat tarball
    # and one wrapped in a single top-level directory (e.g. dist/).
    if [ -f "$EXTRACT_DIR/index.html" ]; then
        DIST_DIR="$EXTRACT_DIR"
    elif [ -f "$EXTRACT_DIR/dist/index.html" ]; then
        DIST_DIR="$EXTRACT_DIR/dist"
    else
        DIST_DIR="$(dirname "$(find "$EXTRACT_DIR" -maxdepth 2 -name index.html | head -n 1)")"
        if [ ! -f "$DIST_DIR/index.html" ]; then
            echo "ERROR: Could not find index.html in the extracted UI dist"
            exit 1
        fi
    fi
    echo "Dist directory: $DIST_DIR"
else
    # =========================================================
    # Local build mode
    # =========================================================

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

    DIST_DIR="$REPO_ROOT/app/stardag-ui/dist"
fi

# Deploy to S3
echo ""
echo "=== Uploading to S3 ==="
$AWS_CMD s3 sync "$DIST_DIR" s3://$BUCKET_NAME --delete --region $AWS_REGION

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
