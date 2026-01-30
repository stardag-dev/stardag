# Stardag Applications

This directory contains the backend API service and web UI for monitoring Stardag task execution.

## Quick Start

### 1. Start the services

From the repository root:

```bash
docker compose up -d
```

This starts:

- PostgreSQL database on port 5432
- API service on port 8000
- Web UI on port 3000

### 2. Run a DAG with API registry

Set the API URL environment variable:

```bash
export STARDAG_API_REGISTRY_URL=http://localhost:8000
```

Then run any stardag build. Tasks will be automatically registered with the API.

Example:

```bash
python -m stardag_examples.api_registry_demo
```

### 3. View tasks in the UI

Open http://localhost:3000 in your browser to see registered tasks.

## Development

### Run services locally (without Docker)

**Database:**

```bash
docker compose up -d db
```

**API:**

```bash
cd app/stardag-api
STARDAG_API_DATABASE_URL=postgresql://stardag:stardag@localhost:5432/stardag \
  uvicorn stardag_api.main:app --reload
```

**UI:**

```bash
cd app/stardag-ui
npm install
npm run dev
```

The UI dev server runs on http://localhost:5173 with API proxy to localhost:8000.

## API Endpoints

- `GET /health` - Health check
- `POST /api/v1/tasks` - Create a task
- `GET /api/v1/tasks` - List tasks (with filtering)
- `GET /api/v1/tasks/{task_id}` - Get a task
- `PATCH /api/v1/tasks/{task_id}` - Update a task
- `POST /api/v1/tasks/{task_id}/start` - Mark task as started
- `POST /api/v1/tasks/{task_id}/complete` - Mark task as completed
- `POST /api/v1/tasks/{task_id}/fail` - Mark task as failed

API documentation available at http://localhost:8000/docs when running.
