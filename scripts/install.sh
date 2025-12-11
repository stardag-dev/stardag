#!/bin/bash
# Install all packages in the monorepo
set -e

echo "Installing Python packages..."
uv sync --all-extras

echo ""
echo "Installing frontend dependencies..."
cd app/stardag-ui
npm install
cd ../..

echo ""
echo "Done! All packages installed."
