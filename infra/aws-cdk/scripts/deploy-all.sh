#!/bin/bash
# Full deployment: infrastructure + API + migrations + UI
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=========================================="
echo "     Stardag Full Deployment"
echo "=========================================="
echo ""

# Step 1: Deploy infrastructure
echo "Step 1/4: Deploying infrastructure..."
echo ""
"$SCRIPT_DIR/deploy-infra.sh"
echo ""

# Step 2: Build and push API
echo "Step 2/4: Building and pushing API..."
echo ""
"$SCRIPT_DIR/deploy-api.sh"
echo ""

# Step 3: Run migrations
echo "Step 3/4: Running database migrations..."
echo ""
"$SCRIPT_DIR/run-migrations.sh"
echo ""

# Step 4: Deploy UI
echo "Step 4/4: Building and deploying UI..."
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
