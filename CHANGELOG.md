# Changelog

All notable changes to the Stardag project (SDK, Registry API, and UI).

For detailed SDK migration guides, see [RELEASE_NOTES.md](RELEASE_NOTES.md).

## [Unreleased]

## [0.6.0] — Discover-time task registration + bulk register endpoint

End-to-end overhaul of how tasks reach the registry during a build,
motivated by "tasks don't appear in the UI in the order they're
discovered, sometimes only after they finish executing." See
[RELEASE_NOTES.md](RELEASE_NOTES.md#v060) for the full story and migration
notes; the bullets below are the per-component summary.
([#133](https://github.com/stardag-dev/stardag/pull/133))

### SDK

#### New behaviour

- **Discover-time registration**: every task discovered during the build
  walk is now registered with the registry _before_ any task starts
  executing. The full DAG appears in the UI immediately rather than
  leaves-first as tasks become runnable. Applies to both `build` /
  `build_aio` and `build_sequential` / `build_sequential_aio`.
- **Post-order discovery walk**: `discover()` recurses into static deps
  first and only registers the parent once every child has registered.
  Eliminates the brief phantom-row window where the UI used to flash up
  `tid[:12]`-style placeholder names between parent and child
  registration. Same trick on the dynamic-dep path: `discover(dep)`
  runs before `task_add_dependencies(_aio)` so the dep row exists
  before the edge insert.
- **Bulk register**: build engines now collapse the discovered tasks
  into a single `task_register_bulk(_aio)` call per discover walk
  (initial + each dynamic-deps yield), chunked at 1000 tasks per HTTP
  request. For large fan-out DAGs this is a dramatic reduction in HTTP
  round-trips. Per-task fallback on 404 (older API deployments).
- **`build_fail(_aio)` now emitted on discovery error**: if a task's
  `requires()` / `complete()` raises during discovery, the registry
  receives `build_fail` rather than the build being left RUNNING
  forever.

#### Breaking changes

- **`APIRegistry.task_start[_aio]` no longer auto-calls
  `task_register[_aio]`.** The contract is now "register first"
  everywhere — `/start` is a pure event endpoint. Internal callers
  (build engines, Prefect integration) are updated. External callers
  that used `task_start_aio` directly without first calling
  `task_register_aio` will now hit a 404 from `/start`. Migration:
  add the explicit `await registry.task_register_aio(build_id, task)`
  call before `task_start_aio`.
- **Sequential build registers tasks in post-order DFS** (deps before
  parents) where it previously used pre-order. Concurrent build is
  approximately post-order — siblings still interleave but every
  task's deps are guaranteed to register before it. Visible via the
  UI / registry only; no API breakage.

#### Compatibility

- **Backwards compatible with the old Registry API**: the SDK's
  `task_register_bulk(_aio)` catches the FastAPI "missing route" 404
  and falls back to per-task `task_register(_aio)`, mirroring the
  pattern from `task_add_dependencies`. Existing
  `task_register(_aio)`, `task_start(_aio)`, etc. endpoints are
  unchanged.

### API

#### New features

- **New endpoint `POST /builds/{build_id}/tasks/bulk`**: registers up
  to 1000 tasks in a single transaction, processing the array in order
  so within-batch dep references resolve to existing rows (no
  phantom-creation in `_reconcile_dependency_edges`). Deduplicates by
  `task_id`, keeping the first occurrence. Same TASK_PENDING /
  TASK_REFERENCED event semantics as the single-task endpoint.
- **`is_phantom` on `TaskResponse` / `TaskWithStatusResponse`**: the
  flag has existed on the `Task` model for a while; it's now exposed
  in the response so the UI (and other consumers) can distinguish
  placeholder rows from real registered tasks.
- **`GET /builds/{id}/tasks` orders by per-build first event**:
  joins against `events` filtered to this build and orders by
  `min(events.created_at), Task.id`. Re-referenced tasks now appear
  at the position where they were _first seen in this build_, not
  where they were first ever inserted in the environment. Previously
  unordered (DB insertion order, effectively arbitrary).

#### Behaviour change

- **Phantom-creation in `_reconcile_dependency_edges` is now a safety
  hatch**: with the SDK's post-order discover walk + the bulk
  endpoint's in-array ordering, every dep `task_id` resolves to an
  existing row in normal operation. Phantom-creation only triggers
  when a build crashes mid-discover, when an out-of-band caller
  registers an edge before its upstream task, or when an older SDK is
  used. Documented inline.

### UI

- **Phantom rows hidden from the build's task table** and the "X tasks"
  counter. They still render in the DAG view (dropping nodes there
  would leave dangling edges). Reads `is_phantom` from the API's
  `TaskResponse`.

## `app/stardag-api|ui` only — 2026-04-28

> App (API + UI) changes only — no SDK release. Deployed continuously
> via `stardag-cloud`.

### API

- **Performance**: bcrypt API-key validation moved off the event loop (in-process TTL cache + `asyncio.to_thread`), explicit DB pool config, gunicorn `--preload`/`UvicornWorker` with parameterised workers and sizing. JSONB metadata columns, `(environment_id, created_at)` indices, batched dependency reconciliation. Denormalised `Task.latest_*` status columns with `SELECT … FOR UPDATE` concurrent-write protection. ([#125](https://github.com/stardag-dev/stardag/pull/125), [#126](https://github.com/stardag-dev/stardag/pull/126), [#127](https://github.com/stardag-dev/stardag/pull/127))
- **Bug fix**: `/tasks/search` no longer 500s on `filter=build_id:=:<uuid>`; malformed UUIDs now return 400. ([#128](https://github.com/stardag-dev/stardag/pull/128))
- **Stable internal JWT signing key (optional)**: when `STARDAG_API_JWT_PRIVATE_KEY_SECRET_NAME` is set at CDK synth time, the named Secrets Manager secret (containing a PEM RSA private key under `private_key`) is mounted into the container as `JWT_PRIVATE_KEY` and used by the internal token manager instead of generating an ephemeral keypair per container. Deploys and scaleouts no longer invalidate cached internal tokens. ([#129](https://github.com/stardag-dev/stardag/pull/129))

### UI

- **401 retry + session-expired overlay**: `fetchWithAuth` retries 401 once with a force-refreshed Cognito token; on unrecoverable 401 a non-dismissible modal prompts re-login instead of leaving the user on silent empty states. ([#130](https://github.com/stardag-dev/stardag/pull/130))
- **Loading-state and nav hygiene**: BuildsList no longer flashes "No builds yet" between env-arrival and the first fetch; BuildsList + TaskExplorer reset to page 1 on env change (no extra fetch with the old page); BuildView clears filters / pagination / selected task when navigating between builds; switching env from a `/builds/:id` page redirects to `/` instead of surfacing "Failed to fetch graph". ([#131](https://github.com/stardag-dev/stardag/pull/131))

## [0.5.9] — 2026-04-20

### SDK

#### New features

- **Dynamic dependency edges now reach the Registry** so they render as upstream deps in the DAG view. Previously, a task yielded from a `run()` / `run_aio()` generator only had its own static `requires()` chain recorded — the parent → yielded-dep relationship was invisible in the UI. Build executors (`build`, `build_sequential`, and their `_aio` variants) now call a new `RegistryABC.task_add_dependencies(_aio)` method at each dynamic-deps yield, passing the upstream tasks and an `is_dynamic=True` flag. ([#123](https://github.com/stardag-dev/stardag/pull/123))
- **`RegistryABC.task_add_dependencies(_aio)`**: new registry protocol method for recording dependency edges after `task_register`. Default no-op for in-memory registries (`NoOpRegistry` etc.). `APIRegistry` POSTs to the new `POST /builds/{build_id}/tasks/{task_id}/dependencies` endpoint.

#### Compatibility

- **Graceful fallback for older Registry APIs**: the SDK's `APIRegistry.task_add_dependencies(_aio)` catches the specific FastAPI "missing route" 404 (`{"detail": "Not Found"}`) and logs a warning — builds against an older API deployment continue to work, they just don't record dynamic edges. App-level 404s (e.g. `"Build not found"`, `"Task … not registered …"`) re-raise normally so genuine errors aren't hidden.

### API

#### New features

- **`is_dynamic` column on `task_dependencies`** (nullable, `server_default='false'`) — distinguishes edges discovered at runtime from those declared via `task.requires()`. Included in `TaskEdge` / `TaskEdgeExtended` response schemas. Alembic migration `94003640952d` is additive and safely reversible.
- **New endpoint `POST /builds/{build_id}/tasks/{task_id}/dependencies`**: accepts `{upstream_task_ids, is_dynamic=True}`, creates phantom upstream tasks for unknown ids, inserts edges idempotently via `ON CONFLICT DO NOTHING`, and returns `{added, total}`.
- **Grouping applies at depth=0**: `GET /builds/{id}/graph` now always routes through the grouping traversal path, so `max_per_type_per_level` is honored uniformly regardless of `upstream_depth` / `downstream_depth`. Structurally-identical tasks within a build (e.g. many chunks) collapse into batch nodes in the default in-build view.
- **`is_dynamic` propagates through group collapse**: when the extended graph collapses same-type tasks into a batch node, the resulting aggregate edge is marked `is_dynamic=True` if _any_ underlying contributor is dynamic.

#### Schema change

- The `GET /builds/{id}/graph` response is now always the extended shape (`TaskGraphExtendedResponse` — with `groups`, `truncated`, `total_upstream_count`, `total_downstream_count`). Previously it returned the basic `TaskGraphResponse` shape when both depths were 0. The UI already handled both shapes via `isExtendedResponse`; other consumers reading the basic shape should switch to the extended one (all the same fields are present, plus the extended fields default sensibly when depths are 0).

### UI

- **Dynamic dep edges render dashed** (`strokeDasharray: "6 4"`) with the same grey stroke as static deps — subtle visual distinction that stays readable in dense DAGs.
- **Hover tooltip on dynamic edges** ("Dynamic dependency — yielded at runtime from the upstream task's run() generator.") via a new `DynamicEdge` React Flow edge type with an SVG `<title>` child and a wider transparent hit-path for easy hovering.

### Examples

- New `stardag_examples.general.dynamic_deps_demo` — an `Orchestrator` that first runs `GetChunksToProcess(source_uri)` to decide how many chunks to process, then dynamically yields one `TransformChunk` per chunk; each `TransformChunk` statically requires its own `LoadChunk`. Deterministic `sha256(source_uri)`-based heuristic yields 1–6 chunks of size 1–8 per URI. Good demo of both static + dynamic deps in one pipeline.

## [0.5.8] — 2026-04-20

### SDK

#### Bug fixes

- **`build_sequential` now resolves dynamically-yielded tasks' `requires()` chain.** Previously `build_sequential` (and `build_sequential_aio`) could execute a task yielded from a `run()` generator without first building that task's own static `requires()`, causing it to fail when it tried to `load()` a dep that was never built. The concurrent `build()` already handled this correctly; the sequential executor now matches. ([#118](https://github.com/stardag-dev/stardag/issues/118), [#119](https://github.com/stardag-dev/stardag/pull/119))

#### New features

- **Async generator dynamic dependencies.** `async def run_aio(self): yield ...` is now a supported form for declaring dynamic deps on the class-based Task API — the build system detects async generators via `inspect.isasyncgenfunction` and drives them with `async for`. Both sequential and concurrent executors support it, and the Modal integration handles it via idempotent re-execution. ([#120](https://github.com/stardag-dev/stardag/pull/120))
- **Modal integration: dynamic-deps tasks can now run remotely.** `Runner.run()` now drives sync and async generators and returns a `TaskStruct` of yielded deps for idempotent re-execution (generators cannot be pickled across the Modal boundary). Async-only tasks (`run_aio` without `run`) are executed via `asyncio.run`. ([#120](https://github.com/stardag-dev/stardag/pull/120))

#### Minor breaking change

- **`@task` decorator rejects generator functions.** Declaring `@task`-decorated functions as generators (`yield`) or async generators now raises `TypeError` at decoration time, with an error message pointing to the class-based Task API. Dynamic dependencies were never well-supported by the decorator API — the class-based API is the intended path and always has been. If you relied on this (undocumented) behavior, migrate the function to a `Task` subclass with a generator `run()` / `run_aio()` method. ([#120](https://github.com/stardag-dev/stardag/pull/120))

#### Notes

- The sequential executor's handling of previously-complete dependencies surfaced at runtime (e.g. static deps of a dynamically-yielded task that happen to already be on disk) is more consistent — a `runtime_discover()` wrapper ensures they receive `task_register` + `task_complete` events so they appear in the build's task list in the Registry. Previously they could be silently excluded.
- Known limitation: `StardagApp.build_remote` does not yet support passing runtime configuration overrides to the remote build/worker containers — target roots and other config are baked into the deployed Modal app via Secrets. Tracked as [#121](https://github.com/stardag-dev/stardag/issues/121).

## [0.5.7] — 2026-04-19

### SDK

#### New features

- **Generic task classes can now be instantiated directly.** Previously, a user-defined generic task (e.g. `class MyGeneric(Task[list[T]], Generic[T]): ...`) was silently skipped during polymorphic registration because any class with unresolved `__parameters__` was excluded. With no `__type_id__` attached, the first `model_dump()` raised `AttributeError: __type_id__`. The registration filter has been narrowed to only skip parameterized generic aliases (e.g. `Task[int]`) and classes explicitly marked `__stardag_abstract__ = True`; user-defined generic tasks now get their own `__type_id__`. The internal abstract bases `Task`, `LoadableTask`, and `TargetTask` carry the marker so their current unregistered status is preserved.
- **`SubClass[T]` field annotations now accept `TypeVar`s bound to a `PolymorphicRoot` subclass.** A generic task can declare e.g. `field: SubClass[T]` where `T = TypeVar("T", bound=MyRoot)`; the schema is built using the TypeVar's bound for the generic form and re-built strictly for each parameterized form. Unbounded `TypeVar`s still raise a clear `TypeError` at schema-build time.

#### Notes

- TypeVars on a generic `Task` remain a **static-typing convenience** — runtime behavior (serializer, target selection, etc.) is fixed at class-definition time. If different type parameters need different runtime behavior, define a concrete subclass (e.g. `class MyInt(MyGeneric[int]): pass`) — concrete subclasses get their own `__type_id__` and distinct task id.

## [0.5.6] — 2026-04-19

### SDK

#### Behavior changes

- **`Polymorphic(on_generic_type_mismatch=...)` default is now `"warn"`** (was `"raise"`). Generic-type mismatches detected at validation time — including inside `SubClass[...]` annotations — now emit a `UserWarning` by default instead of raising `ValidationError`. Set `STARDAG_POLYMORPHIC_ON_GENERIC_TYPE_MISMATCH=raise` to restore the previous behavior, or `=ignore` to suppress the warning entirely. An explicit non-`None` value passed to `Polymorphic(...)` always overrides the env var. The emitted warning now includes the env-var suppression hint.

## [0.5.5] — 2026-04-09

### SDK

#### Breaking changes (Modal integration only)

- **`builder_type` removed** from `StardagApp.__init__`. Use `build_function=` instead.
- **`default_build`/`default_run` functions removed**. Replaced by `Builder` and `Runner` classes.
- **`BuildFunction` protocol signature changed**: `(tasks: Sequence[BaseTask] | BaseTask, worker_selector, app_name) -> BuildSummary`.
- **`build_remote`/`build_spawn` kwargs renamed**: `task=` → `tasks=`, `modal_app_name=` → `app_name=`.

#### New features

- **`Builder` and `Runner` classes**: Subclassable defaults for `StardagApp.build_function` and `run_function` with overridable `setup()`/`teardown()` hooks for custom container-level initialization (logging, GPU setup, config, etc.).
- **`PrefectBuilder`**: `Builder` subclass for Prefect-based build orchestration (replaces `_prefect_build` function).
- **`BuildFunction` and `RunFunction` Protocol types**: Clear contracts for custom build/run callables.
- **`stardag.testing.modal`**: Test tasks and app factory (`create_test_app()`) for Modal integration tests.

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
