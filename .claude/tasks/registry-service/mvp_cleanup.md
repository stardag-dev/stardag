# MVP Cleanup

## Status

active

## Goal

Clean up efforts from [main_and_mvp.md](./main_and_mvp.md) to get to a clean state.

## Instructions

An MVP was successfully implemented. But there are quite a few things to clean up. Here's a rough task list. It will need refinement and clarification:

### 1. Monorepo Structure

`tox` command now fails (hence GitHub workflows as well). The Python packages should be treated as independent in CI, each with its own structure and .venv for pytest and type checks.

**Current packages:**

- `stardag` (lib/stardag-sdk/)
- `stardag_examples` (examples/)
- `stardag_api` (service/stardag-api/)

#### 1.1 Directory Reorganization

- [ ] Rename `lib/stardag-sdk/` → `lib/stardag/` to match package name
- [ ] Move `examples/` → `lib/stardag-examples/` (make it a regular Python lib, use dash in dir name)
- [ ] Move `service/stardag-api/` → `app/stardag-api/` (simpler with just `lib/` and `app/` split)
- [ ] Move tests in `stardag` that use `stardag_examples` → `lib/stardag-examples/tests/`

#### 1.2 Dev Scripts

- [ ] Add a minimal bash script in root for installing all packages
- [ ] Add a minimal bash script in root for running tests in all packages
- [ ] Note: Root .venv (via root pyproject.toml) _can_ exist for dev purposes, but should not be used for tests or pyright

#### 1.3 Pre-commit & Tox

- [ ] Update pre-commit config to cover everything
  - [ ] Add separate pyright hooks per Python package (each uses its own .venv)
- [ ] Update tox.ini:
  - [ ] Tests and pyright for each Python package (with respective .venv)
  - [ ] Formatting/linting for everything (including root docs)
  - [ ] _If suitable_, also run stardag-ui tests via tox
- [ ] Make sure tox passes

#### 1.4 Frontend Testing

- [ ] Add minimal tests and testing framework for `app/stardag-ui`

#### 1.5 CI

- [ ] Review `.github/workflows/ci.yml` and ensure it:
  - [ ] Is up to date with new structure
  - [ ] Includes tests for stardag-ui
  - [ ] Is in sync with tox so CI will pass

### 2. Docs

- [ ] Update DEV_README.md with basic full-stack instructions: installation, testing, running, and contribution guidelines (keep concise)
- [ ] Audit all markdown files for broken links and outdated references

## Context

Background information, related files, prior discussions.

## Execution Plan

### Summary of Preparatory Analysis

### Plan

1. Step one
2. Step two
3. ...

## Decisions

Key decisions made and their rationale.

## Progress

- [ ] 1.1 Directory reorganization
- [ ] 1.2 Dev scripts
- [ ] 1.3 Pre-commit & tox
- [ ] 1.4 Frontend testing
- [ ] 1.5 CI
- [ ] 2. Docs

## Notes

Any additional observations, blockers, or open questions.
