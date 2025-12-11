# [Task Name]

## Status

active

## Goal

Get a state-of-the-art scalable setup of DB use.

## Instructions

This task will happen iteratively. There are several steps. Some mostly boilerplate,
other of an arcitectural nature. Starting with an overview list below:

**Use async sqlalchemy everywhere (+tests)**

- [x] Refactore current implementation to use async sql alchemy functionality.
- [x] Extend unit tests to also cover the task endpoint (high level functionality/smoke test)

**Set up standard migration management with alembic**

- [x] Setup standard alembic migrations (using default autogenerate template)
- [x] Make sure migrations are applied in tests (via conftest fixtures).
- [x] In docker-compose, add an additional `alembic` service that runs migrations
- [x] Add migration handling to a app/stardag-api/README.md (also add other basic info here, keep it concise)

## Context

There is just a quick and dirty MVP in place.

## Execution Plan

### Summary Of Preparatory Analysis

### Plan

1. Step one
2. Step two
3. ...

## Decisions

Key decisions made and their rationale.

## Progress

- [x] Completed item
- [ ] Pending item

## Notes

Any additional observations, blockers, or open questions.
