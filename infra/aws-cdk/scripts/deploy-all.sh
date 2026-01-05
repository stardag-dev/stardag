#!/bin/bash
# Full deployment: infrastructure + API + migrations + UI
# Order ensures migrations run BEFORE the API service starts with new code
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "     Stardag Full Deployment"
echo "=========================================="
echo ""

# Step 1: Deploy infrastructure
echo "Step 1/5: Deploying infrastructure..."
echo ""
"$SCRIPT_DIR/deploy-infra.sh"
echo ""

# Step 2: Build and push API image (don't update service yet)
echo "Step 2/5: Building and pushing API image..."
echo ""
"$SCRIPT_DIR/deploy-api.sh" --skip-update
echo ""

# Step 3: Run migrations (before API service uses new code)
echo "Step 3/5: Running database migrations..."
echo ""
"$SCRIPT_DIR/run-migrations.sh"
echo ""

# Step 4: Update API service (now safe to start new code)
echo "Step 4/5: Updating API service..."
echo ""
"$SCRIPT_DIR/update-api-service.sh"
echo ""

# Step 5: Deploy UI
echo "Step 5/5: Building and deploying UI..."
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
