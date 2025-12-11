# [Task Name]

## Status

active

## Goal

Get a state-of-the-art scalable setup of DB use.

## Instructions

This task will happen iteratively. There are several steps. Some mostly boilerplate,
other of an arcitectural nature. Starting with an overview list below:

**Normalized, more complete schemas and corresponding endpoints**

Split it up, suggestion:

- `Organization` to be prepared for multi tenancy, autopopulated with a "default"
- `Workspace` like "project" for isolated environments
- `User` default to "default" but prepare for auth later.
- `Run` The execution `sd.build([...])` of a DAG/set of tasks. Metadata like
  - user (who triggered)
  - name (given randomly, memorable slug)
  - docs (optionally user provided/editable)
  - No events like start/end time, handleded by `Events`
- `Task` (main properties, independent of execution/Run,)
  - namespace
  - family
  - parameters
- `Event` All events that happens _IMMUTABLE appedn only_
  - always associated with a `run_id`
  - mostly assiciated with a `task_id` (started, completed, failed) but can also for entire run started and completed, failed etc.
  - type (see examples above)
  - So a `Task` or `Run` status will be inferred from the latest event with type like `TASK_STATUS_CHANGED`.
- `Dependency` (better name?) for keeping track for tasks' up vs downstream dependencies:
  - upstream_task_id
  - downstream_task_id
  - Should support efficent graph traversal queries

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
