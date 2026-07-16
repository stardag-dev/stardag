import os
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from stardag_api.auth.tokens import get_token_manager
from stardag_api.config import auth_settings, settings
from stardag_api.middleware import GZipRequestMiddleware
from stardag_api.routes import (
    auth_router,
    builds_router,
    concurrency_limits_router,
    locks_router,
    workspaces_router,
    search_router,
    target_roots_router,
    tasks_router,
    ui_router,
)


# Eagerly construct the InternalTokenManager so its (potentially ephemeral)
# RSA keypair is generated at module-import time. Combined with gunicorn's
# --preload flag this happens once in the master process and is inherited
# by every forked worker — without it, each worker would generate a fresh
# keypair and tokens signed in one worker would fail validation in another.
get_token_manager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Local auth mode: idempotently provision the bootstrap admin so a
    # fresh self-hosted deployment has a first user to log in with.
    if auth_settings.mode == "local" and auth_settings.bootstrap_admin_email:
        from stardag_api.db import async_session_maker
        from stardag_api.services.local_auth import ensure_bootstrap_admin

        async with async_session_maker() as session:
            await ensure_bootstrap_admin(session)
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
# Pass-through for non-gzipped requests so existing SDK versions and
# direct callers keep working unchanged.
app.add_middleware(GZipRequestMiddleware)

# Auth routes - included twice with different prefixes:
# - No prefix: JWKS at /.well-known/jwks.json (standard location)
# - /api/v1 prefix: Exchange at /api/v1/auth/exchange
app.include_router(auth_router)  # JWKS
app.include_router(auth_router, prefix="/api/v1")  # Exchange

# UI routes (internal JWT auth required)
app.include_router(ui_router, prefix="/api/v1")
app.include_router(workspaces_router, prefix="/api/v1")

# SDK routes (API key or internal JWT auth)
app.include_router(builds_router, prefix="/api/v1")
app.include_router(locks_router, prefix="/api/v1")
app.include_router(concurrency_limits_router, prefix="/api/v1")
# search_router must come before tasks_router because tasks_router has /{task_id}
# which would match "search" as a task_id
app.include_router(search_router, prefix="/api/v1")
app.include_router(tasks_router, prefix="/api/v1")
app.include_router(target_roots_router, prefix="/api/v1")


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
    """
    try:
        api_version = _package_version("stardag-api")
    except PackageNotFoundError:  # pragma: no cover - running from a raw checkout
        api_version = "unknown"
    return {
        "server_version": os.environ.get("STARDAG_SERVER_VERSION", "dev"),
        "api_version": api_version,
    }
