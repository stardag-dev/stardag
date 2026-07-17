# Combined Stardag server image: Registry API + web UI in one container.
#
# This is the single image definition for server releases: CI builds and
# publishes it to ghcr.io/stardag-dev/stardag-server on `server-v*` tags
# (see .github/workflows/publish-server-image.yml), and `stardag self-host`
# consumes the published image via modal.Image.from_registry.
#
# Build from the repo root:
#   docker build -f app/server.Dockerfile -t stardag-server .
#
# Layout contract (keep in sync with the from-source build in
# lib/stardag/src/stardag/selfhost/_modal_app.py — the self-host Modal
# functions reference these paths in both modes):
#   /opt/stardag/api  - alembic.ini + migrations/ (DB migrations run with
#                       this as cwd: python -m alembic -c alembic.ini upgrade head)
#   /opt/stardag/ui   - built web UI (static files; all UI config is
#                       fetched from the API at runtime, no build-time env)
#
# No ENTRYPOINT and a plain python install: Modal (and other platforms)
# must be able to use the image's python directly.

# --- Stage 1: build the web UI ------------------------------------------------
FROM node:22-slim AS ui-build

WORKDIR /build/stardag-ui

COPY app/stardag-ui/package.json app/stardag-ui/package-lock.json ./
RUN npm ci

COPY app/stardag-ui/ ./
RUN npm run build

# --- Stage 2: runtime ---------------------------------------------------------
FROM python:3.12-slim

# Install the API package
COPY app/stardag-api /tmp/stardag-api
RUN pip install --no-cache-dir /tmp/stardag-api && rm -rf /tmp/stardag-api

# Alembic config + migrations (not part of the installed python package)
COPY app/stardag-api/alembic.ini /opt/stardag/api/alembic.ini
COPY app/stardag-api/migrations /opt/stardag/api/migrations

# Built web UI
COPY --from=ui-build /build/stardag-ui/dist /opt/stardag/ui
ENV STARDAG_UI_DIST=/opt/stardag/ui

# Server release version, injected by CI from the `server-vX.Y.Z` tag;
# surfaced at GET /api/v1/version.
ARG STARDAG_SERVER_VERSION=dev
ENV STARDAG_SERVER_VERSION=${STARDAG_SERVER_VERSION}

EXPOSE 8000

# gunicorn with multiple uvicorn workers absorbs CPU-bound bursts (auth /
# JWT verification, bcrypt cache misses) better than a single uvicorn
# process; override worker count at deploy time via GUNICORN_WORKERS.
#
# --preload imports the app module once in the master process before
# fork()-ing workers, so the (potentially ephemeral) RSA keypair created
# at import time is shared by all workers (see app/stardag-api/Dockerfile
# for the full rationale).
ENV GUNICORN_WORKERS=2
CMD ["sh", "-c", "exec gunicorn stardag_api.server:app -k uvicorn.workers.UvicornWorker -w ${GUNICORN_WORKERS} --preload --bind 0.0.0.0:8000 --access-logfile - --error-logfile -"]
