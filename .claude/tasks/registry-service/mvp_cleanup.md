# MVP Cleanup

## Status

active

## Goal

Clean up efforts from [main_and_mvp.md](./main_and_mvp.md) to get to a clean state.

## Instructions

An MVP was successfully implemented. But there are quite a few things to clean up. Here's a rough task list. It will need refinement and clarification:

**Monorepo Structure**

`tox` command now fails (hence GitHub workflows as well). We should consider the Python packages:

- `stardag` (lib/stardag-sdk/)
- `stardag_examples` (examples/)
- `stardag_api` (service/stardag-api/)

"independent" in CI; they should have the same structure and their own .venv when we run pytest and type checks (which run against a given .venv).

- [ ] Move `stardag_examples` to `lib/stardag-examples` (make it a regular Python lib, use dash in dir name)
- [ ] Rename `stardag-sdk` -> `stardag` to match package name
- [ ] Move tests that are now in `stardag` but use `stardag_examples` to `stardag_examples/tests`
- [ ] Move `service/stardag-api` to `app/stardag-api` (simpler with just one split between `lib/` and `app/`)
- [ ] Update pre-commit config to cover everything
  - [ ] For pyright: add separate hooks for the different Python packages, because they should use their separate .venvs for type checks
- [ ] We _can_ still allow a root .venv (via root pyproject.toml) for dev purposes that installs all packages, but this should not be used for tests and/or pyright type checks
- [ ] Add a minimal bash script in the root for installing all packages
- [ ] Add a minimal bash script in the root for running tests in all packages
- [ ] Add minimal tests (and testing framework) for app/stardag-ui
- [ ] Update and extend tox.ini to run tests and pyright checks for all _respective_ Python packages (with the right .venv), and formatting/linting of everything (including root docs)
- [ ] _If suitable_, also use tox to install and run tests in stardag-ui
- [ ] Make sure tox passes
- [ ] Carefully review `.github/workflows/ci.yml` and make sure it is up to date (add tests for stardag-ui!) and is in sync with the rest so that CI will pass

**Docs**

- [ ] Update DEV_README.md with basic instructions for how to run the full stack service: installation, testing, running, and contribution guidelines. (Keep it concise and simple.)
- [ ] Carefully go through all READMEs (md files) and other docs and check that any (markdown hyper-) links and references are still valid.

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

- [ ] Monorepo structure cleanup
- [ ] Documentation updates

## Notes

Any additional observations, blockers, or open questions.
