#!/bin/bash
# End-to-end integration test
# Brings up docker-compose, runs demo, verifies API and UI
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

echo "=== E2E Integration Test ==="

# Cleanup on exit
cleanup() {
    echo ""
    echo "=== Cleaning up ==="
    docker-compose down -v 2>/dev/null || true
    if [ -n "$TARGET_ROOT" ] && [ -d "$TARGET_ROOT" ]; then
        rm -rf "$TARGET_ROOT"
    fi
}
trap cleanup EXIT

# Fresh start
echo "=== Starting services ==="
docker-compose down -v 2>/dev/null || true
docker-compose up -d --build

# Wait for API to be healthy
echo "=== Waiting for API ==="
for i in {1..30}; do
    if curl -s http://localhost:8000/health > /dev/null 2>&1; then
        echo "API is ready"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "ERROR: API failed to start"
        docker-compose logs api
        exit 1
    fi
    sleep 1
done

# Wait for UI to be ready
echo "=== Waiting for UI ==="
for i in {1..30}; do
    if curl -s http://localhost:3000 > /dev/null 2>&1; then
        echo "UI is ready"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "ERROR: UI failed to start"
        docker-compose logs ui
        exit 1
    fi
    sleep 1
done

# Wait for Keycloak (realm import can take a while)
echo "=== Waiting for Keycloak ==="
for i in {1..60}; do
    if curl -s http://localhost:8080/realms/stardag/.well-known/openid-configuration > /dev/null 2>&1; then
        echo "Keycloak is ready"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "ERROR: Keycloak failed to start"
        docker-compose logs keycloak
        exit 1
    fi
    sleep 2
done

# Bootstrap SDK credentials: test user -> workspace -> environment -> API key
# (mirrors the flow in integration-tests/src/stardag_integration_tests/conftest.py)
echo "=== Bootstrapping API credentials ==="
OIDC_TOKEN=$(curl -sf -X POST http://localhost:8080/realms/stardag/protocol/openid-connect/token \
    -d "grant_type=password" \
    -d "client_id=stardag-test" \
    -d "username=testuser" \
    -d "password=testpassword" \
    -d "scope=openid profile email" \
    | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

# First authenticated request auto-creates the test user
# and their personal workspace (with a default environment)
WORKSPACE_ID=$(curl -sf http://localhost:8000/api/v1/ui/me \
    -H "Authorization: Bearer $OIDC_TOKEN" \
    | python3 -c "import sys, json; print(json.load(sys.stdin)['workspaces'][0]['id'])")
echo "Using workspace: $WORKSPACE_ID"

INTERNAL_TOKEN=$(curl -sf -X POST http://localhost:8000/api/v1/auth/exchange \
    -H "Authorization: Bearer $OIDC_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"workspace_id\": \"$WORKSPACE_ID\"}" \
    | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

ENVIRONMENT_ID=$(curl -sf "http://localhost:8000/api/v1/ui/workspaces/$WORKSPACE_ID/environments" \
    -H "Authorization: Bearer $INTERNAL_TOKEN" \
    | python3 -c "import sys, json; print(json.load(sys.stdin)[0]['id'])")
echo "Using environment: $ENVIRONMENT_ID"

API_KEY=$(curl -sf -X POST "http://localhost:8000/api/v1/ui/workspaces/$WORKSPACE_ID/environments/$ENVIRONMENT_ID/api-keys" \
    -H "Authorization: Bearer $INTERNAL_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"name": "e2e-test"}' \
    | python3 -c "import sys, json; print(json.load(sys.stdin)['key'])")
echo "API key created"

# Run demo script
echo "=== Running demo script ==="
TARGET_ROOT=$(mktemp -d)
export STARDAG_TARGET_ROOTS__DEFAULT="$TARGET_ROOT"
export STARDAG_API_URL="http://localhost:8000"
export STARDAG_API_KEY="$API_KEY"

cd "$REPO_ROOT/lib/stardag-examples"
uv run python -m stardag_examples.general.artifacts_demo

# Verify API has builds
echo "=== Verifying API - Builds ==="
BUILDS_RESPONSE=$(curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/api/v1/builds)
BUILD_COUNT=$(echo "$BUILDS_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('total', 0))")
if [ "$BUILD_COUNT" -lt 1 ]; then
    echo "ERROR: No builds found in API"
    echo "API response: $BUILDS_RESPONSE"
    exit 1
fi
echo "Found $BUILD_COUNT build(s) in API"

# Verify API has tasks
echo "=== Verifying API - Tasks ==="
TASKS_RESPONSE=$(curl -s -H "X-API-Key: $API_KEY" http://localhost:8000/api/v1/tasks)
TASK_COUNT=$(echo "$TASKS_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('total', 0))")
if [ "$TASK_COUNT" -lt 1 ]; then
    echo "ERROR: No tasks found in API"
    echo "API response: $TASKS_RESPONSE"
    exit 1
fi
echo "Found $TASK_COUNT task(s) in API"

# Verify UI serves HTML
echo "=== Verifying UI ==="
if ! curl -s http://localhost:3000 | grep -q '<div id="root"'; then
    echo "ERROR: UI not serving expected HTML"
    exit 1
fi
echo "UI is serving HTML correctly"

echo ""
echo "=== E2E Integration Test PASSED ==="
