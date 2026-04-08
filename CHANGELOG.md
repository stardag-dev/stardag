# Changelog

All notable changes to the Stardag project (SDK, Registry API, and UI).

For detailed SDK migration guides, see [RELEASE_NOTES.md](RELEASE_NOTES.md).

## [Unreleased]

## [0.5.4] — 2026-04-08

### SDK

#### Bug fixes

- **Fix `modal >= 1.4` compatibility**: Remove import of `modal.gpu.GPU_T` which was deleted in modal 1.4. `FunctionSettings.gpu` now uses `str | list[str]` directly. (#113)

## [0.5.3] — 2026-04-05

### SDK

#### Security

- **Secret masking**: `RegistryAuth.api_key` and `RegistryAuth.access_token` now use Pydantic `SecretStr`. Values are masked as `**********` in `repr()`, `str()`, `model_dump()`, and log output, preventing accidental leakage of credentials.
- **`STARDAG_API_URL` env var**: Replaces `STARDAG_REGISTRY_URL` as the canonical env var for the registry API URL. `STARDAG_REGISTRY_URL` still works as a deprecated alias with a warning. Consistent with `STARDAG_API_KEY` and `STARDAG_API_TIMEOUT`.

#### Bug fixes

- **Token auth with env var overrides**: When `STARDAG_API_URL` is set (bypassing profile for URL/workspace/environment), the loader now inherits user and registry_name from the active profile so that OIDC token auth still works.

## [0.5.2] — 2026-04-05

### SDK

#### Breaking changes (configuration only — core task/build API unchanged)

- **`StardagConfig` restructured**: `config.api` (`APIConfig`), `config.context` (`ContextConfig`), and the loose `config.access_token`/`config.api_key` fields are replaced by `config.registry: RegistryConfig | None` and `config.context: ConfigContext`. Code using `config.api.url` must use `config.registry.url` (with null check for offline mode).
- **`APIConfig` removed**: Subsumed by `RegistryConfig` (url, timeout, workspace_id, environment_id, auth).
- **`ContextConfig` removed**: Replaced by `ConfigContext` (profile, registry_name only — user/workspace_id/environment_id moved to `RegistryConfig`).
- **`RegistryConfig` repurposed**: Was `RegistryConfig(url: str)` (TOML entry). Now `RegistryConfig(url, workspace_id, environment_id, auth, timeout)` (runtime config). TOML registry entries are now plain `dict[str, str]` in `TomlConfig`.
- **`config/__init__.py` trimmed**: Only public API symbols are exported. Internal code should import from submodules (`config.paths`, `config.cache`, `config.io`, `config.models`, `config.loader`).
- **`DEFAULT_API_URL` removed**: Unused constant.

#### New features

- **Automatic JWT token refresh during builds**: `APIRegistry` now uses `httpx.Auth` subclasses (`StardagAPIKeyAuth`, `StardagTokenAuth`) that transparently refresh expired tokens before each request. Long-running builds no longer fail when JWT tokens expire mid-execution.
- **`STARDAG_NO_REGISTRY=1` env var**: Forces offline/local mode (`config.registry = None`, `NoOpRegistry`).
- **Profile-less auth**: `StardagTokenAuth` can derive credential storage keys from the registry URL when no TOML profile is configured, enabling env-var-only setups.

#### Improvements

- `config.py` split into `config/` package with focused submodules: `paths`, `io`, `cache`, `models`, `loader`.
- Token refresh logic extracted from `_cli/credentials.py` to `registry/_auth.py`, removing code duplication and the `config → _cli` circular dependency.
- `get_user_workspaces()` and `get_environments()` now propagate exceptions instead of silently returning empty lists.

## [0.5.0] — 2026-03-18

### SDK

#### New features

- **`LoadValidator[T]`** — abstract base class for validators that run automatically on `Task._save()` and `Task.load()`. Attached via `typing.Annotated`, supports chaining, transforming, and an attribute-based escape hatch for MRO conflicts. Works with both the class API and `@task` decorator.
- **`test_harness`** context manager in `stardag.testing` — sets up isolated test environments with temp target roots and `NoOpRegistry` by default.
- **`get_default_relpath()`** — standalone public utility for constructing default task relpaths (previously internal to `Task._relpath`).
- **`BuildSummary.raise_on_failure()`** — raises new `BuildFailed` exception (with `.summary`) on `FAILURE` status.
- **`TaskExecutionError`** — wraps task executor exceptions with pre-formatted tracebacks, preserving context across thread/process boundaries.
- **`on_registry_failure` parameter** on all build functions — `"warn"` (default) or `"raise"` to control registry error handling.
- **`register_all` flag** on all build functions — opt-in full DAG registration, recursing into already-complete task dependencies.
- **Commit hash in event metadata** — all task/build lifecycle events now include the git commit hash for traceability (critical for resumed builds at different commits).

#### Improvements

- All serializers are now hashable for use in `Annotated` type params (Pydantic generic cache compatibility).
- `Annotated` wrappers are stripped in `_is_type_compatible`, fixing `TaskLoads[Annotated[T, ...]]` validation.
- `artifacts()` / `artifacts_aio()` return `Sequence` instead of `list`; fixed `artifacts_aio` missing `async` keyword.
- `Task.from_registry(id)` accepts `str | UUID` (previously `UUID` only).
- `ResourceProvider.is_initialized()` added; `_target_roots_override` no longer triggers config loading prematurely.
- Registry provider used consistently in build modules (enabling test overrides).
- Removed unnecessary generic `_FileTargetType` from `DirectoryTarget`.

#### Bug fixes

- **FAIL_FAST exception surfacing**: Task exceptions now propagate to caller in FAIL_FAST mode (both sequential and concurrent builds), instead of being silently wrapped.
- **Sequential build registry communication**: Previously-completed tasks now correctly marked complete in the registry (not left PENDING). Registry errors no longer mask original task errors.
- **Deadlock detection** added to sequential builds (matching concurrent build behavior).
- **Dynamic dependency discovery** uses `discover()` function, properly incrementing `task_count.discovered` and recursing sub-dependencies.
- **Artifact errors separated from registry errors** — artifact collection is best-effort with warn semantics, not subject to `on_registry_failure`.
- **Dynamically discovered already-complete deps** now registered immediately in sequential builds.
- Deduplicated sync/async sequential build logic via shared pure helper functions.

### Registry API

- Recursive upstream/downstream traversal on `GET /builds/{build_id}/graph` via optional `upstream_depth`, `downstream_depth`, `max_per_type_per_level`, `max_total_nodes` query params
- New `POST /tasks/graph` endpoint for cross-build DAG queries (used by Task Explorer)
- Graph traversal service with BFS, depth limiting, per-type grouping, and cycle protection
- Task status aggregation across builds for graph nodes
- Edge reconciliation on every `task_register` call (fixes missing edges across builds)
- Phantom task records for unregistered upstream dependencies (upgraded on proper registration)
- `is_phantom` column on tasks table; phantom tasks get `UNREGISTERED` status in graph responses
- Commit hash stored in `event_metadata` on all task/build lifecycle events
- `commit_hash` field in `TaskWithStatusResponse` (extracted from status-determining event)

### UI

- DAG view with configurable upstream/downstream depth controls
- Batch/group nodes for collapsed same-type dependencies (with expand on click)
- Depth-based visual fading for upstream and downstream nodes
- Task Explorer: DAG view works across multiple builds (removed single-build restriction)
- Task Explorer: refactored into focused sub-components (`TaskExplorerSearch`, `TaskExplorerTable`)
- Breadcrumb navigation system in global header
- Dashed border styling for phantom/unregistered task nodes
- Task Detail: commit hash from status-determining event; Event Log: per-event commit column
- DAG dependency node click fetches full task data via API
- Layout density improvements (compact sizing across all views)

## [0.4.0] — 2026-03-06

### SDK (breaking)

Target & serializer type hierarchy restructure. Directory target support added.
See [release notes](RELEASE_NOTES.md#v040--breaking-target--serializer-type-hierarchy-restructure) for migration guide.

### Registry API

- Task artifacts support (`POST /builds/{build_id}/tasks/{task_id}/artifacts`, `GET /tasks/{task_id}/artifacts`)
- Task metadata endpoint (`GET /tasks/{task_id}/metadata`) for `AliasTask.from_registry`
- Build graph endpoint (`GET /builds/{build_id}/graph`)

### UI

- Task Explorer with search, filtering, and column management
- Build view with DAG visualization
- Task detail panel with artifacts and events

## [0.3.0] — 2026-03-03

### SDK (breaking)

Task class hierarchy rename + `LoadableTask` + `TaskLoads` update.
See [release notes](RELEASE_NOTES.md#v030--breaking-task-class-hierarchy-rename--loadabletask--taskloads-update) for migration guide.

### Registry API

- Initial task registry service (builds, tasks, events, dependencies)
- API key and JWT authentication
- Workspace and environment management

### UI

- Initial React frontend with auth, workspace selection, build list
