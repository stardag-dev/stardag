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

# Run demo script
echo "=== Running demo script ==="
TARGET_ROOT=$(mktemp -d)
export STARDAG_TARGET_ROOT__DEFAULT="$TARGET_ROOT"
export STARDAG_API_REGISTRY_URL="http://localhost:8000"

cd "$REPO_ROOT/lib/stardag-examples"
uv run python src/stardag_examples/api_registry_demo.py

# Verify API has runs
echo "=== Verifying API - Runs ==="
RUNS_RESPONSE=$(curl -s http://localhost:8000/api/v1/runs)
RUN_COUNT=$(echo "$RUNS_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('total', 0))")
if [ "$RUN_COUNT" -lt 1 ]; then
    echo "ERROR: No runs found in API"
    echo "API response: $RUNS_RESPONSE"
    exit 1
fi
echo "Found $RUN_COUNT run(s) in API"

# Verify API has tasks
echo "=== Verifying API - Tasks ==="
TASKS_RESPONSE=$(curl -s http://localhost:8000/api/v1/tasks)
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
