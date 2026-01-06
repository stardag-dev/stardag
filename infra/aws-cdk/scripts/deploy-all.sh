#!/bin/bash
# Full deployment: infrastructure + API + migrations + UI
# Order ensures:
#   1. ECR exists before pushing image
#   2. Image exists before ECS service is created
#   3. Migrations run before API service starts
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "     Stardag Full Deployment"
echo "=========================================="
echo ""

# Step 1: Deploy Foundation (VPC, Database, ECR, Cognito, DNS)
# This must come first to create ECR repository
echo "Step 1/6: Deploying Foundation infrastructure..."
echo ""
"$SCRIPT_DIR/deploy-infra.sh" --foundation-only
echo ""

# Step 2: Build and push API image (now ECR exists)
echo "Step 2/6: Building and pushing API image..."
echo ""
"$SCRIPT_DIR/deploy-api.sh" --skip-update
echo ""

# Step 3: Deploy API and Frontend stacks (now image exists)
echo "Step 3/6: Deploying API and Frontend stacks..."
echo ""
"$SCRIPT_DIR/deploy-infra.sh" --all
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
echo "Step 6/6: Building and deploying UI..."
echo ""
"$SCRIPT_DIR/deploy-ui.sh"
echo ""

echo "=========================================="
echo "     Deployment Complete!"
echo "=========================================="
echo ""
echo "Your Stardag instance is now running at:"
echo "  API: https://${API_SUBDOMAIN:-api}.${DOMAIN_NAME}"
echo "  UI:  https://${UI_SUBDOMAIN:-app}.${DOMAIN_NAME}"
echo ""
