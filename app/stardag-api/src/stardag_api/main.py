import os
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from stardag_api.auth.tokens import get_token_manager
from stardag_api.config import auth_settings, settings
from stardag_api.middleware import GZipRequestMiddleware
from stardag_api.routes import (
    auth_router,
    registry_v2_router,
    target_roots_router,
    ui_router,
    workspaces_router,
)
from stardag_api.services.errors import RegistryError


# Eagerly construct the InternalTokenManager so its (potentially ephemeral)
# RSA keypair is generated at module-import time. Combined with gunicorn's
# --preload flag this happens once in the master process and is inherited
# by every forked worker — without it, each worker would generate a fresh
# keypair and tokens signed in one worker would fail validation in another.
get_token_manager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Local auth mode: idempotently provision the bootstrap admin so a
    # fresh self-hosted deployment has a first user to log in with, then
    # the primary workspace/environment (AUTH_PRIMARY_WORKSPACE_*).
    if auth_settings.mode == "local" and auth_settings.bootstrap_admin_email:
        from stardag_api.db import async_session_maker
        from stardag_api.services.local_auth import (
            ensure_bootstrap_admin,
            ensure_primary_workspace,
        )

        async with async_session_maker() as session:
            await ensure_bootstrap_admin(session)
            await ensure_primary_workspace(session)

    yield


app = FastAPI(
    title="Stardag API",
    description="API for tracking and monitoring Stardag task execution",
    version="0.0.1",
    lifespan=lifespan,
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Decompress incoming gzipped request bodies (the SDK's bulk-register path
# gzips bodies above ~1KB to keep large batches manageable on the wire).
# Pass-through for non-gzipped requests.
app.add_middleware(GZipRequestMiddleware)

# Auth routes - included twice with different prefixes:
# - No prefix: JWKS at /.well-known/jwks.json (standard location)
# - /api/v1 prefix: Exchange at /api/v1/auth/exchange
app.include_router(auth_router)  # JWKS
app.include_router(auth_router, prefix="/api/v1")  # Exchange

# UI routes (internal JWT auth required)
app.include_router(ui_router, prefix="/api/v1")
app.include_router(workspaces_router, prefix="/api/v1")

# SDK routes (API key or internal JWT auth). The v1 core routes (builds,
# tasks, locks, deployments, search, ...) are deleted; their v2
# replacements live under /api/v2.
app.include_router(target_roots_router, prefix="/api/v1")
app.include_router(registry_v2_router, prefix="/api/v2")


@app.exception_handler(RegistryError)
async def registry_error_handler(_: Request, exc: RegistryError) -> JSONResponse:
    """A v2 service refusal: its status code, and its code and detail."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.to_dict()})


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/api/v1/version")
async def version():
    """Server + API package versions.

    ``server_version`` is the release version of the combined server
    (API + UI) image, injected via the STARDAG_SERVER_VERSION environment
    variable at image build time ("dev" when unset, e.g. running from
    source). ``api_version`` is the installed stardag-api package version.

    Expected ``server_version`` forms (see scripts/server-version.sh and
    DEV_README.md "Releasing the Server"):

    - ``X.Y.Z`` - a release build (from a ``server-vX.Y.Z`` tag)
    - ``X.Y.Z+N.g<sha>`` - a non-release build, N commits past the
      nearest release tag (semver build metadata)
    - ``0.0.0+g<sha>`` - a build with no release tag reachable
    - ``dev`` - the env var was not set
    """
    try:
        api_version = _package_version("stardag-api")
    except PackageNotFoundError:  # pragma: no cover - running from a raw checkout
        api_version = "unknown"
    return {
        "server_version": os.environ.get("STARDAG_SERVER_VERSION", "dev"),
        "api_version": api_version,
    }
