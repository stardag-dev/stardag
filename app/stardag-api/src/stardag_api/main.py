from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from stardag_api.db import engine
from stardag_api.models import Base
from stardag_api.routes import (
    auth_router,
    builds_router,
    organizations_router,
    target_roots_router,
    tasks_router,
    ui_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="Stardag API",
    description="API for tracking and monitoring Stardag task execution",
    version="0.0.1",
    lifespan=lifespan,
)

# CORS for frontend (both dev server and nginx)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        "http://localhost:3000",  # Nginx (docker-compose)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth routes - included twice with different prefixes:
# - No prefix: JWKS at /.well-known/jwks.json (standard location)
# - /api/v1 prefix: Exchange at /api/v1/auth/exchange
app.include_router(auth_router)  # JWKS
app.include_router(auth_router, prefix="/api/v1")  # Exchange

# UI routes (internal JWT auth required)
app.include_router(ui_router, prefix="/api/v1")
app.include_router(organizations_router, prefix="/api/v1")

# SDK routes (API key or internal JWT auth)
app.include_router(builds_router, prefix="/api/v1")
app.include_router(tasks_router, prefix="/api/v1")
app.include_router(target_roots_router, prefix="/api/v1")


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
