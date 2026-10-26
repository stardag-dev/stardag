#!/bin/sh
# Install the API package into the current python, with its dependencies
# pinned and hash-checked from uv.lock.
#
# Every server image installs through this script: app/server.Dockerfile
# (the published stardag-server image), app/stardag-api/Dockerfile (the
# API-only image infra/aws-cdk builds) and the `stardag self-host
# --from-source` image (lib/stardag/src/stardag/selfhost/_modal_app.py).
# Installing the package from pyproject.toml alone would resolve whatever
# is newest on PyPI at build time, so the image would not run what CI tested
# against the lock, and a lockfile bump would not reach it.
#
# --locked fails the build if uv.lock is out of date with pyproject.toml,
# rather than silently re-resolving (--frozen would skip that check).
#
# Installs into the interpreter's own site-packages (no venv): the images
# must stay plain python that Modal and other platforms can use directly.
#
# Usage: sh install-locked.sh <path-to-stardag-api>
set -eu

API_DIR="${1:?usage: install-locked.sh <path-to-stardag-api>}"
UV_VERSION=0.12.5

python -m pip install --no-cache-dir "uv==${UV_VERSION}"
requirements="$(mktemp)"
# The index variables are cleared for the export only. uv counts an index
# that differs from the one recorded in uv.lock as a stale lock, and Modal's
# image builder sets UV_INDEX_URL to its PyPI mirror. The install below still
# downloads through any configured mirror (PIP_INDEX_URL), and the hashes
# make that safe.
env -u UV_INDEX_URL -u UV_DEFAULT_INDEX -u UV_INDEX -u UV_EXTRA_INDEX_URL \
    python -m uv export --project "$API_DIR" --locked --no-dev \
    --no-emit-project --quiet --output-file "$requirements"
python -m pip install --no-cache-dir --require-hashes -r "$requirements"
python -m pip install --no-cache-dir --no-deps "$API_DIR"
python -m pip uninstall --yes uv
rm -f "$requirements"
