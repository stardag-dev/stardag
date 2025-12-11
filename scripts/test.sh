#!/bin/bash
# Run tests for all packages in the monorepo
# Each Python package runs tests in its own .venv
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Testing lib/stardag ==="
cd "$ROOT_DIR/lib/stardag"
uv run pytest tests

echo ""
echo "=== Testing lib/stardag-examples ==="
cd "$ROOT_DIR/lib/stardag-examples"
uv run pytest tests

echo ""
echo "=== Testing app/stardag-api ==="
cd "$ROOT_DIR/app/stardag-api"
uv run pytest tests

echo ""
echo "=== Testing app/stardag-ui ==="
cd "$ROOT_DIR/app/stardag-ui"
npm test

echo ""
echo "All tests completed!"
