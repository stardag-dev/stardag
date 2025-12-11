# Registry Service - Backend API & Frontend App

## Status

completed (MVP)

## Goal

Transform stardag into a full-fledged data processing/workflow tool by adding:

1. A **backend API service** that the DAG building functionality communicates with via the Registry abstraction to track and monitor task execution
2. A **frontend application** for monitoring DAGs, tasks, and execution status through a web UI

**Primary objective**: Make it exist first. Do all necessary simplifications to reach a working state where we can:

1. Run `docker compose up`
2. Build a minimal DAG from local Python talking to the Docker-hosted service and DB
3. View the task details in the app in the browser

## What Was Built

### Repo Structure

```
stardag/
├── lib/stardag-sdk/     # SDK library (moved from src/)
├── service/stardag-api/ # FastAPI backend
├── app/stardag-ui/      # React frontend
├── examples/            # Example code (unchanged)
└── docker-compose.yml   # Full stack orchestration
```

### Backend (service/stardag-api)

- FastAPI REST API with PostgreSQL
- Task CRUD endpoints with filtering/pagination
- Task lifecycle: pending → running → completed/failed
- SQLAlchemy models with JSONB for task data
- Dockerfile for containerization

### Frontend (app/stardag-ui)

- React 18 + TypeScript + Vite
- Tailwind CSS v4 styling
- Task list with filtering by family/status
- Task detail panel showing parameters and dependencies
- Status badges with color coding
- Nginx production container with API proxy

### SDK Integration (lib/stardag-sdk)

- New `APIRegistry` class using httpx
- Auto-selected when `STARDAG_API_REGISTRY_URL` is set
- Falls back to FileSystemRegistry or NoOpRegistry
- TaskRunner updated to call start/complete/fail lifecycle methods

## Quick Start

```bash
# Start services
docker compose up -d

# Run demo
export STARDAG_API_REGISTRY_URL=http://localhost:8000
python -m stardag_examples.api_registry_demo

# View UI
open http://localhost:3000
```

## Progress

- [x] Restructure repo into monorepo
- [x] FastAPI backend with task CRUD
- [x] React frontend with task list/detail
- [x] APIRegistry in SDK
- [x] docker-compose.yml
- [x] End-to-end demo verified

## Future Work (Not MVP)

- [ ] DAG visualization (graph view)
- [ ] Real-time updates (WebSockets/SSE)
- [ ] Execution timeline/history
- [ ] Log viewer
- [ ] Authentication (API keys, OAuth)
- [ ] Metrics and statistics
- [ ] Run/build initiation via API
