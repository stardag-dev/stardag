#!/bin/bash
# Install all packages in the monorepo
# Each Python package gets its own .venv for isolated testing
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Installing lib/stardag ==="
cd "$ROOT_DIR/lib/stardag"
uv sync --all-extras

echo ""
echo "=== Installing lib/stardag-examples ==="
cd "$ROOT_DIR/lib/stardag-examples"
uv sync --all-extras

echo ""
echo "=== Installing app/stardag-api ==="
cd "$ROOT_DIR/app/stardag-api"
uv sync --all-extras

echo ""
echo "=== Installing app/stardag-ui ==="
cd "$ROOT_DIR/app/stardag-ui"
npm install

echo ""
echo "=== Installing root workspace (for dev) ==="
cd "$ROOT_DIR"
uv sync --all-extras

echo ""
echo "Done! All packages installed."
