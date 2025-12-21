from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from stardag_api.db import engine
from stardag_api.models import Base
from stardag_api.routes import (
    builds_router,
    organizations_router,
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

# UI routes (JWT auth required)
app.include_router(ui_router, prefix="/api/v1")
app.include_router(organizations_router, prefix="/api/v1")

# Existing routes (no auth during transition, will be moved to /api/v1/sdk later)
app.include_router(builds_router, prefix="/api/v1")
app.include_router(tasks_router, prefix="/api/v1")


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
