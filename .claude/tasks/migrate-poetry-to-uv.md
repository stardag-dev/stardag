# Migrate from Poetry to uv

## Status

completed

## Goal

Replace Poetry with uv as the package manager for stardag.

## Instructions

Migrate the project to UV.

First carefully review everything that affected such as tox, github/workflows, DEV_README.md, etc. and make a plan and TODO list for what to fix. Also clarify how we can test/knows that we are done.

## Context

- Current setup uses Poetry for dependency management
- uv is a fast Python package installer and resolver written in Rust
- Migration involves converting pyproject.toml configuration and updating development workflows

## Execution Plan

### Summary Of Preparatory Analysis

**Files affected by Poetry:**

1. **pyproject.toml** (root) - Uses `[tool.poetry]` section for package metadata, dependencies, extras, and dev dependencies
2. **examples/pyproject.toml** - Same structure for the examples sub-package with local path dependency to main package
3. **poetry.lock** (root) - Lock file to be replaced with uv.lock
4. **examples/poetry.lock** - Lock file for examples package
5. **tox.ini** - Uses `poetry install` and `poetry run pytest` commands
6. **DEV_README.md** - Documents Poetry commands for setup and running tests
7. **.github/workflows/ci.yml** - Uses `snok/install-poetry@v1` action and relies on tox which uses poetry
8. **.github/workflows/publish.yml** - Uses `python -m build` (should work with standard pyproject.toml)
9. **.pre-commit-config.yaml** - References `.tox/py312/bin/python` for pyright (indirect dependency)

**Key considerations:**

- uv uses standard PEP 621 format (`[project]` instead of `[tool.poetry]`)
- uv supports workspaces for monorepo-style setups (main + examples)
- Need to convert optional dependencies (extras) to uv format
- Dev dependencies become a dev dependency group or separate extra
- Local path dependencies need workspace configuration

### Plan

1. Convert root `pyproject.toml` from Poetry to uv/PEP 621 format
2. Convert `examples/pyproject.toml` to uv format and set up workspace
3. Update `tox.ini` to use `uv` commands instead of `poetry`
4. Update `.github/workflows/ci.yml` to install uv instead of poetry
5. Update `DEV_README.md` with uv commands
6. Update `.pre-commit-config.yaml` pyright path (if needed after tox changes)
7. Delete `poetry.lock` files and generate `uv.lock`
8. Test the migration locally

### Verification (How we know we're done)

1. `uv sync --all-extras` successfully installs all dependencies
2. `uv run pytest` runs tests successfully
3. `tox` runs all environments successfully (pre-commit, py311/312/313, pyright)
4. `python -m build` can build the package
5. No references to poetry remain in config files

## Decisions

- Use uv workspaces to manage main package + examples sub-package relationship
- Keep extras structure (prefect, s3, modal) as optional dependencies

## Progress

- [x] Analyze current Poetry configuration
- [x] Create migration plan
- [x] Convert root pyproject.toml
- [x] Convert examples/pyproject.toml and set up workspace
- [x] Update tox.ini
- [x] Update .github/workflows/ci.yml
- [x] Update DEV_README.md
- [x] Update .pre-commit-config.yaml (removed tox path from pyright args)
- [x] Delete poetry.lock files and generate uv.lock
- [x] Test migration

## Verification Results

All verification criteria passed:

1. ✅ `uv sync --all-extras` - Successfully installed all dependencies
2. ✅ `uv run pytest` - All 54 tests passed
3. ✅ `uv run pyright` - 0 errors, 0 warnings
4. ✅ `python -m build` - Successfully built stardag-0.0.3.tar.gz and stardag-0.0.3-py3-none-any.whl

## Notes

- Changed build backend from poetry-core to hatchling (standard PEP 517 backend)
- Used uv workspaces to link main package and examples sub-package
- Added pyright to dev dependencies so it can be run via `uv run pyright`
- Fixed pytest config (consolidated [tool.pytest] and [tool.pytest.ini_options])
- Updated .claude/settings.json to allow uv commands instead of poetry
