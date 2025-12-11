#!/bin/bash
# Run tests for all packages in the monorepo
set -e

echo "Running Python tests..."
uv run pytest lib/stardag/tests

echo ""
echo "Running frontend tests..."
cd app/stardag-ui
if [ -f "package.json" ] && grep -q '"test"' package.json 2>/dev/null; then
    npm test
else
    echo "No frontend tests configured yet"
fi
cd ../..

echo ""
echo "All tests completed!"
