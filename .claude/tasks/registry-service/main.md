# Registry Service - Backend API & Frontend App

## Status

active

## Goal

Transform stardag into a full-fledged data processing/workflow tool by adding:

1. A **backend API service** that the DAG building functionality communicates with via the Registry abstraction to track and monitor task execution
2. A **frontend application** for monitoring DAGs, tasks, and execution status through a web UI

## Instructions

### Scope & Priorities

**MVP**: Tasks are registered via backend service and viewable/searchable by basic options (task family, ID, etc.)

**Initial steps** - Restructure repo into:

- `lib/stardag-sdk` - Main user SDK library (current codebase)
- `service/stardag-api` - Backend service (registry API)
- `app/stardag-ui` - Frontend application

### Backend Architecture

- **Framework**: FastAPI
- **API style**: REST (initially)
- **Database**: PostgreSQL
- **Authentication**: None for MVP (later: API keys, OAuth)
- **Deployment**: Local Docker with docker-compose for MVP

### Frontend Architecture

- **Framework**: React 18 with TypeScript
- **Styling**: Tailwind CSS + shadcn/ui
- **Real-time updates**: Not in MVP (WebSockets/SSE/polling can be added later)

### Registry Integration

- `APIRegistry` as default when service is available, with `FileSystemRegistry` as fallback
- Start with current `RegisterdTaskEnvelope` fields
- Track task status (pending, running, completed, failed) in addition to registration
- DAG-level tracking: Not part of MVP

### Key Features

**Backend (MVP):**

- Task registration and status tracking
- Execution history and logs
- DAG structure persistence: Store serialized tasks as JSONB, model dependencies in DB for querying upstream/downstream tasks

**Backend (Not MVP):**

- Run/build initiation via API (execution triggered from CLI/Python only; UI is passive)
- Metrics and statistics

**Frontend (MVP):**

- Task list with filtering/search
- Task detail view (parameters, outputs, dependencies)
- DAG visualization (graph view) - bonus, not required

**Frontend (Later):**

- Execution timeline/history
- Real-time status updates
- Log viewer

### Constraints & Preferences

- **Testing**: High-level unit and integration tests
- **Documentation**: Standard docs in code; READMEs for user overview (how to start service, run examples, etc.)
- **Performance**: Not critical for MVP, but use reasonable indices in PostgreSQL

## Context

### Current Registry System

The existing `RegistryABC` (`src/stardag/build/registry.py`) provides:

- `FileSystemRegistry`: Persists task registration to JSON files
- `NoOpRegistry`: No-op implementation for when registry is disabled
- `RegisterdTaskEnvelope`: Captures task, task_id, user, created_at, commit_hash

The registry is used by `TaskRunner` (`src/stardag/build/task_runner.py`) to register tasks after successful completion.

### Current Build System

- `build()` function in `sequential.py` traverses DAG depth-first
- Uses `TaskRunner` which invokes callbacks and registers completed tasks
- `registry_provider` pattern allows runtime registry injection

### Related Files

- `src/stardag/build/registry.py` - Registry abstraction
- `src/stardag/build/task_runner.py` - Task execution
- `src/stardag/build/sequential.py` - Sequential build orchestration
- `src/stardag/_base.py` - Core Task class

## Execution Plan

### Summary Of Preparatory Analysis

_To be filled after instructions are finalized and analysis is complete._

### Plan

_To be created after instructions are reviewed._

## Decisions

- **Repo structure**: Monorepo with `lib/`, `service/`, `app/` directories
- **Backward compatibility**: No concerns for FileSystemRegistry users
- **Deployment model**: Both local dev (docker-compose prioritized in MVP) and standalone service (later)

## Progress

- [x] Created task folder and main.md
- [x] User reviewed and completed Instructions section
- [ ] Preparatory analysis
- [ ] Architecture design
- [ ] Implementation plan breakdown into subtasks

## Notes

### Suggested Subtask Structure

This is a large initiative. Consider breaking into subtasks like:

- `repo-restructure.md` - Monorepo setup
- `backend-api.md` - API service implementation
- `database-schema.md` - Data model design
- `api-registry.md` - New APIRegistry implementation
- `frontend-app.md` - Frontend application
- `dag-visualization.md` - Graph rendering (bonus)
- `deployment.md` - Docker/docker-compose setup
