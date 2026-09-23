# SDK core-entities verification report

> Point-in-time research note, 2026-09-23, against `stardag-dev/stardag` at
> commit `f96fb751` (then `main`). Written by a Claude agent for the STA-105
> design; verified against the source at that commit. Not maintained: read
> [../design.md](../design.md) for the design and [../plan.md](../plan.md)
> for current status.

All paths are relative to `lib/stardag/src/stardag/` unless noted.

## 1. Identity today

**task_id computation.**

- `BaseTask.id` = `model_dump(mode="json", context={"mode": "hash"})["id"]` (`_core/base_task.py:360-367`). `_hash_mode_finalize` swaps the dump for `{"id": uuid5(ns, json(sorted, compact))}` (`_core/base_task.py:369-372`, `_core/task_id.py:31-45`). The namespace is `9ca26b27-…`, which can be overridden through `task_uuid5_namespace_provider` (`_core/task_id.py:12-19`).
- What goes into the hash: the polymorphic discriminators `__namespace`/`__name` are always added (`polymorphic.py:582-594`), plus `version` (a real field, defaulting to `__version__`, `_core/base_task.py:128-145`), plus every field that survives `_handle_hash_mode` (`base_model.py:347-396`). That function drops:
  - build-config fields (`is_build_config_field`, :367);
  - legacy `hash_exclude=True` fields, in hash mode only (:373);
  - fields whose raw value equals `compat_default`, in hash mode only (:385-390).
- A nested task is hashed as `{"id": …}`, because its own serializer finalizes it in hash mode (recursive Merkle). `AliasTask` hashes to the aliased id (`_core/alias_task.py:153-160`). Sets are sorted only in hash mode (`_core/hashable_set.py:54`).
- The target path embeds the id: `<ns>/<name>/v<version>/<id[:2]>/<id[2:4]>/<id>` (`_core/task.py:23-48, 194-201`).

**`StardagField` (`base_model.py:72-171`).** It has three fields: `compat_default`, `hash_exclude` (deprecated) and `significance: Literal["identity","dependencies_only","execution_only"]` (:53, :102).

- Invalid literal → `ValueError` (:111).
- `compat_default` combined with a non-identity significance → `ValueError` (:127).
- `hash_exclude` combined with an explicit non-identity significance → `ValueError` (:134).
- `hash_exclude` alone → `DeprecationWarning` (:119).
- Derived properties: `is_identity`, `is_legacy_hash_exclude`, `is_build_config_field` (= significance != identity), `effective_significance` (folds `hash_exclude` into `execution_only`), and `field_significance(field)` (:145-178).

**Where each level is read, i.e. every consumer of `significance`.** Only two files consume it (grep confirms; `integration/modal/_app.py:1364` and `_core/alias_task.py:82` are comments):

1. `base_model.py:206-264`, the `_check_add_compatibility_defaults` before-validator:
   - For each build-config field, under plain init: present in the input → `ValueError` (:233-242).
   - Under `mode="compat"`: present → dropped (:243).
   - Then `resolve_field_value(config_key, name)` reads the ContextVar build config, and the value is used if found; otherwise the class default applies (:244-246).
   - `_non_identity_fields` is cached per class and excludes the legacy form (:267-291).
2. `base_model.py:361-374`, serialization. Build-config fields are dropped in both hash and registry modes. Legacy `hash_exclude` is dropped in hash mode but kept in registry mode.
3. `build_config.py:99-162`, `register_build_config_class`. It indexes non-task `StardagBaseModel`s that have build-config fields, called from `__pydantic_init_subclass__` (`base_model.py:293-300`).
4. `build_config.py:312-397`, `canonical_structure_config`:
   - validates every override (unknown class → `UnknownTaskClassError`; unknown field or identity field → `BuildConfigError`);
   - hashes only `dependencies_only`, in hash mode, dropping values equal to the default;
   - validates `execution_only` but excludes it from the hash.

**`task_data` (registry-mode dump).** Built by `_get_task_data_for_registration` (`registry/_api_registry.py:3116-3181`) as `task.model_dump(mode="json", context={"mode":"registry"})`.

- It keeps compat-default-valued fields and legacy `hash_exclude` fields, and drops level 2 and 3.
- Nested tasks appear as full registry dumps, not ids, because `_hash_mode_finalize` runs only in hash mode (`base_model.py:394-396`).

**How non-significant fields are stored and rehydrated.** They are never stored per task. They are stored once per build, as `build_config` on `POST /builds` or `PUT /builds/{id}/scope`, and are re-resolved from the installed ContextVar at every validation.

- `rebind_to_build_config(task)` does a registry dump followed by a compat validate, so the task is re-created under the current config and keeps the same id (`build_config.py:400-413`).

**`model_fields_set`.** It is not used anywhere in the SDK: there are no `model_fields_set`, `exclude_unset` or `exclude_defaults` references. "Was this passed explicitly?" is decided purely by key presence in the before-validator input (`base_model.py:233`).

**Rehydration paths.**

- `task_from_registry_data` (`_core/rehydrate.py:73-159`):
  - checks the discriminators;
  - refuses `__aliased` anywhere in the payload (pickle safety);
  - checks the class is registered;
  - validates via `TypeAdapter(SubClass[BaseTask])` in compat mode;
  - optionally checks `expected_task_id`.
- `BaseTask.from_registry` (`_core/base_task.py:401-424`) is the explicit path. It calls `task_get_metadata` and then compat-validates, and it will unpickle an `AliasTask.loads_type` (`_core/base_task.py:377-399`).
- The tick uses the registry-data path only (`build/_reactive/_frontier_actions.py:203-240`).
- Pickle: the only task-object transport is Modal call arguments (cloudpickle by value). The trigger passes roots to `bootstrap` and `build`, and the tick passes the task to `worker.remote`. There is no pickle store (see section 6).

**Claim: "levels 2/3 are read only from a central build config and never passable at init". Confirmed, with caveats.**

- Plain `__init__` and `model_validate` without compat both refuse the key (`base_model.py:233-242`). Compat mode silently drops it (:243).
- "Central" is a ContextVar (`build_config.py:76-78`). The public `sd.build_config_scope` and `sd.set_build_config` (`__init__.py:55-60`) let any code install one around construction, so "the build's config" is a convention the engines enforce by installing the stored config, not an invariant.
- Validation bypasses are unguarded: `model_copy(update=…)` and `model_construct` would set a level-2 value without the check (no overrides exist; grep shows none).
- The legacy `hash_exclude=True` field is still passable at init, hash-excluded and stored in `task_data` (`base_model.py:94-101, 150-157, 373`).
- With no config installed, the class default applies silently. That is why every entry point rebinds its roots.

## 2. Discovery and registration (SDK side)

**Reactive/shared static discovery.** Implemented in `discover_and_register_aio` (`build/_reactive/_discovery.py:177-364`).

- Phase 1 is a concurrent walk (:270-293):
  - a visited set keyed by `task.id`;
  - `complete_aio()` under one semaphore (default 16, :38);
  - **it stops at complete tasks, and their `requires()` is never evaluated** (:286-287);
  - otherwise `deps_of[id] = flatten(requires())`.
- Phase 2 is a sequential post-order (:299-315): deps before parents, and complete tasks are emitted as leaves.
- Registration: `task_register_bulk_aio` is called in **sequential chunks of 50** (:317-337). The call carries `declared_dependencies={id: deps}` only for expanded tasks, plus `scope_key` and optional `limit_keys`.
  - A pruned or complete task is absent from the map, so the payload omits `dependency_task_ids` and it **declares no edges** (`registry/_api_registry.py:3170-3180`, `registry/_base.py:1998-2006`).
  - Then, if `retry_failed`: `task_retry_aio` runs per task for failed, cancelled, skipped, suspended or interrupted tasks (:338-354).
  - Then `task_complete_aio` runs per previously completed task, bounded concurrently (:356-362).
- Request count per call: ⌈N/50⌉ bulk POSTs, plus one retry POST per retryable task, plus one complete POST per pruned-complete task. Each bulk body is `{tasks:[{task_id, task_namespace, task_name, task_data, version, output_uri, dependency_task_ids?, limit_keys?}], scope_key?}` with `?id_only=true` (:815-883), gzipped above a threshold (:515).
- Callers:
  - the bootstrap and rollover, through `plan_under_scope_aio` (`integration/modal/_bootstrap.py:255-301`). It registers under the new scope, then runs the rehydration pre-flight, then `PUT /builds/{id}/scope` **last**;
  - the worker's dynamic-dependency path.

**Resident engine (`build/_concurrent.py`).**

- Its own `discover()` (:790-882) has the same prune-at-complete, post-order and declared-deps behaviour. It also has a `register_all` option that expands complete tasks too (:855).
- `flush_pending_registrations` (:884-957) bulk-registers in chunks of 50 (`_BULK_REGISTER_CHUNK_SIZE`).
- `build_start_aio(root_tasks, scope_key, build_config)` or `build_resume_aio` runs first (:755-782).
- The sequential engine mirrors this (`build/_sequential.py:210, 348, 965-987`).

**Dynamic dependencies.**

- In a reactive Modal worker, `_WorkerLifecycleReporter.suspended` (`integration/modal/_runner.py:742-755`) calls `_register_dynamic_deps` (:860-910) before `task_suspend` and then wakes the scheduler. The order inside is:
  1. `discover_and_register_aio(yielded, scope_key=worker_scope)`. This registers the children **and their static `requires()` closure**, post-order, in chunks of 50, with `retry_failed=False`.
  2. A coverage warning for task modules.
  3. `task_add_dependencies(parent, flatten(yielded), is_dynamic=True, scope_key)`, **one POST** (`registry/_api_registry.py:1084-1126`).
  4. `task_suspend`.
- So a child's own closure lands **before** the parent→child edges, and the edges land before SUSPENDED.
- Could a tick observe a child with no registered upstreams? Not from SDK ordering. Each child is registered in the same bulk item as its `dependency_task_ids`, after its deps' chunk. Whether that is atomic per item is a server question.
- Two windows exist:
  - A tick can observe the children as registered, pending members of the build before the parent→child edge exists. The parent is still RUNNING and owned, so this is benign.
  - A child that was already complete is registered without edges, because it was pruned.
- Resident engine (`build/_concurrent.py:1107-1160`): the order is **different**:
  1. `task_suspend_aio` first (unless the worker self-reports);
  2. `discover(dep)` for each new dep;
  3. `flush_pending_registrations`;
  4. `task_add_dependencies_aio`.
- Here the parent is SUSPENDED in the registry before its children exist. That is harmless resident-side, since the engine is the scheduler.
- Process pool: `_run_task_in_process` re-runs the generator and returns incomplete yields as a TaskStruct (:196-240). Async generators are driven in-loop (:402-410).

**Static-declaration conflict (the STA-41 pattern).** There is no detection on the SDK side any more. grep finds no `cancel_conflicting`, no "live build holds" and no declaration compare. The only 409 handling is:

- `scope_mismatch` → `ScopeMismatchError` / `BuildConfigMismatchError` (`registry/_api_registry.py:3092`, `exceptions.py:357-373`);
- the start-claim codes `task_already_running`, `task_already_completed` and `concurrency_limit_reached` (:2675-2688);
- `execution_superseded` / `task_cancelled` (`exceptions.py:98`).

What the SDK does contribute to arbitration is the `None` versus `[]` declaration distinction (`registry/_base.py:333-340`).

**"One instance per task id" checks.** There are none beyond dedupe by id (`visited`/`task_states`, first seen wins). The only way two distinct instances can share an id is through a legacy `hash_exclude` field. In reactive discovery, phase 1 is concurrent, so which instance's `requires()` fills `deps_of[id]` is nondeterministic (`_discovery.py:279-289`). Phase 2 may then emit a different instance for `task_data` (:301-312).

## 3. Build config and scope

**`build_config.py`.**

- `BuildConfig = Mapping["ns.Name", Mapping[field, value]]` (:49), held in a ContextVar (:76).
- Functions: `task_config_key`, `register_build_config_class` / `get_build_config_class`, `get_build_config` / `set_build_config` / `build_config_scope`, `resolve_field_value`, `jsonable_build_config` (JSON-mode coercion, :254-286), `structure_config_hash` (sha256[:16] of the canonical dependencies_only map, :289-309), `canonical_structure_config` and `rebind_to_build_config`.
- Errors: `BuildConfigError`, `UnknownTaskClassError`.

**Structure scope key.** `f"{code_id}:{structure_config_hash(config)}"` (`build/_scope.py:108-112`), with helpers `scope_code_id` and `scope_config_hash` (:115-145).

- The placeholder is `build:<uuid>`, server-assigned (:45-52, :148-166).
- Only the code-id half is compared by ticks and workers (:115-128, `build/_reactive/_tick.py:810-819`).

**Where the config enters.**

- Per `sd.build` / `build_aio` / `build_sequential`: a `build_config=` kwarg (`build/_concurrent.py:569, 2326`), installed via `@installs_build_config[_aio]` (`build/_sequential.py:100-133`).
- On resume without a config, the stored one is adopted (`build/_sequential.py:136-202`).
- Roots are rebound (`build/_concurrent.py:641-646`).
- Per trigger: `StardagApp.build_trigger(build_config=)` (`integration/modal/_app.py:1298-1310`). This does:
  - validation at the trigger, tolerating unknown classes (`integration/modal/_app.py:235-256`);
  - JSON coercion (:1416-1420);
  - adoption of the stored config on a re-trigger (:1433-1447);
  - `build_start(build_config=)` **without `scope_key`**, so the server gives the placeholder (:1452-1457);
  - forwarding to `bootstrap` (:1656-1664) or `build` (:1473).

**`env_overrides` are not build config.** They are per worker invocation (per task), from `worker_selector(task)` returning `(name, env)` (`integration/modal/_selector.py:17-43`). The deploy fixes the selector function, not the values. The executor adds framework env on every call (`integration/modal/_executor.py:320-423`):

- `STARDAG_BUILD_ID`, `STARDAG_MODAL_APP_NAME`, `STARDAG_REACTIVE`, `STARDAG_EXECUTION_ID`, the claim TTL, the timeout and the Modal coordinates;
- **`STARDAG_BUILD_CONFIG`** (compact JSON) and **`STARDAG_SCOPE_KEY`** (:412-422).

They are applied with `temp_env_vars` around `run` (`_app.py:1084-1106`). There is no per-trigger or per-`sd.build` env_overrides.

**How the config is transported.**

- Thread pool: `contextvars.copy_context().run` (`build/_concurrent.py:411-421`).
- Process pool: an explicit `build_config` argument, then `set_build_config` (:196-229, :424-434).
- Modal worker: `build_config_scope(_build_config_from_env(env_overrides))` around the reporter and run (`integration/modal/_runner.py:350-383, 1083-1087`). The received task object is not rebound; tasks it constructs resolve under the config.
- Tick: `set_build_config(build_info.build_config)` read from `GET /builds/{id}` (`integration/modal/_tick.py:397`).
- Bootstrap: `build_config_scope` plus rebinding the roots (`integration/modal/_bootstrap.py:30, 318-336`).

**Local build.** `code_id()` resolves in this order (`build/_scope.py:68-105`):

1. `STARDAG_CODE_ID` (validated: non-empty, no `:`);
2. otherwise the git HEAD SHA if the tree is clean;
3. otherwise a per-process `uuid4().hex`, with a warning.

A local build has no deployment: no `deployment_record` call, and no rollover (rollover exists only in the Modal tick).

## 4. Deployment and Modal

**Code id at deploy.**

- `StardagApp.code_id` is `_scope.code_id()` memoised on the app (`integration/modal/_app.py:758-765`).
- `finalize()` bakes it as the Modal Secret `STARDAG_CODE_ID` into every function (`integration/modal/_app.py:855-861`), along with the volume mounts, the Modal workspace and the API key.
- `stardag modal deploy` (`_cli/modal.py:~650-844`) runs `deploy_app(name=deployment_name, tag=tag or code_id)` (:822-828). It then calls `_record_deployment`, which is `POST /deployments {app_name, code_id, modal_app_id}` (:556-617, `registry/_api_registry.py:2415-2451`). A failure exits 1, and a missing route raises `RegistryTooOldError`.

**No `family--<code_id>` handle exists.** The deployment is `(app_name, code_id)`, and the Modal app name is unchanged across versions. The design doc records `<family>--<code id>` as rejected (`docs/design/scope-keyed-dependency-structure.md:280-291`). grep for `family--` in lib/ and app/ finds nothing.

**How workers know their deployment.** Only via the baked `STARDAG_CODE_ID`. There is no deployment id at runtime.

- The worker's scope is `f"{code_id()}:{scope_config_hash(forwarded)}"`. A forwarded placeholder is passed through, and another build's placeholder is refused before user code runs (`integration/modal/_runner.py:386-448, 1071-1075`).
- Workers are otherwise code-agnostic.

**Functions (`_app.py:947-1200+`).** All five run `_run_container_setup` (`integration/modal/_container_setup.py:1-22`).

- `build`: resident, imports task modules first (:1060-1075).
- `worker_*`: `_modal_run`, which publishes the task-module patterns and the limit-key selector (:1086-1106).
- `bootstrap`: `run_reactive_bootstrap` (:1170-1208). It imports task modules, rebinds roots, runs `plan_under_scope_aio`, then `build_set_reactive_meta`, then spawns `tick` (`integration/modal/_bootstrap.py:304-368`). A failure calls `_fail_build_best_effort`.
- `tick`: requires a non-None `scope_key` (otherwise `registry_too_old`), installs the config, imports task modules, constructs `ModalTaskExecutor(build_config, scope_key)` and runs `run_tick_aio` with the rollover hook (`integration/modal/_tick.py:380-501`).
  - The tick loop: `clear_notify` → `get_frontier` → rollover check → schedule (`build/_reactive/_tick.py:1040-1110`).
- `tick_watchdog`: `build_list_running(reactive_app_name=…)`, then spawns one single-pass tick per build (`integration/modal/_tick.py:656-739`).

**Rollover detection.**

- `_planned_by_other_code(frontier.scope_key)` compares the scope's code-id half with `code_id()` (`build/_reactive/_tick.py:810-819`). It runs under the lease, once per tick, and only while the build is RUNNING (:1076-1106).
- The hook `_roll_over_build_aio` (`integration/modal/_tick.py:504-653`) proceeds as follows:
  1. `deployment_list_aio(app_name)`; proceed only if the current record's `code_id` is this tick's. No record → no rollover.
  2. Compute the new scope.
  3. Rehydrate the roots from `task_get_metadata` with `expected_task_id`, and rebind them.
  4. `plan_under_scope_aio(retry_failed=False)`.
  5. Any failure → fail the build with the message "re-trigger as new build", and the outcome `rollover_failed`.
- If the scope still names other code, the tick exits as superseded.

## 5. Registry client surface

**`APIRegistry`** (`registry/_api_registry.py`, with an `_aio` twin for most). All routes are under `/api/v1`.

| Area               | Method → route                                                                                                                                                                                                                                                                                                                                        |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Build lifecycle    | build_start → POST /builds (:600-642) · build_resume → POST /builds/{id}/resume (:657) · build_complete → POST …/complete · build_fail → POST …/fail · build_cancel → POST …/cancel · build_exit_early → POST …/exit-early · build_bulk_cancel → POST /builds/bulk-cancel · build_add_roots → POST …/roots · build_skip_blocked → POST …/skip-blocked |
| Build reads        | build_get / build_get_summary → GET /builds/{id} · build_list (and build_list_running, which wraps it) → GET /builds · build_get_frontier → GET …/frontier · build_list_tick_summaries → GET …/tick-summaries · build_report_tick_summary → POST …/tick-summaries                                                                                     |
| Scope / reactive   | build_set_scope → PUT …/scope (:2337) · build_set_reactive_meta → PUT …/reactive-meta · build_notify → POST …/notify · build_get_notify → GET …/notify · build_clear_notify → DELETE …/notify · build_wake_candidates → POST /builds/wake-candidates · scheduler lease acquire/renew/release → …/scheduler-lease (`_lease_call`, :2065)               |
| Deployments        | deployment_record → POST /deployments · deployment_list → GET /deployments                                                                                                                                                                                                                                                                            |
| Task registration  | task_register → POST /builds/{id}/tasks · task_register_bulk → POST …/tasks/bulk?id_only=true · task_add_dependencies → POST …/tasks/{tid}/dependencies                                                                                                                                                                                               |
| Task events        | start / start_claim → …/start · complete · fail · interrupt · preempt · suspend · resume · skip · cancel_by_id → …/cancel · retry_by_id → …/retry · waiting_for_lock → …/waiting-for-lock · upload_artifacts → …/artifacts · execution_status → GET …/tasks/{tid}/execution-status                                                                    |
| Task reads         | task_get_metadata → GET /tasks/{id}/metadata · task_list → GET /tasks                                                                                                                                                                                                                                                                                 |
| Concurrency limits | GET /concurrency-limits · PUT and DELETE /concurrency-limits/{key} · GET …/{key}/holders · POST …/{key}/holders/{tid}/evict                                                                                                                                                                                                                           |

About 55 distinct routes in total.

Outside `APIRegistry`:

- `registry/_lock.py:229-377`: `/locks/{tid}/acquire|renew|release` and `/locks/tasks/{tid}/completion-status`.
- `/auth/config|login|exchange`, `/ui/me`, and `/ui/workspaces[/{id}[/environments[/{env}[/target-roots[/{id}]]]]]` (`registry/_auth.py`, `_cli/auth.py`, `_cli/config.py`, `_cli/environment.py`, `_cli/_selfhost_connect.py`, `_cli/modal.py:213`).

**Version gate.**

- `_require_scope_support` (:3025-3080): a build start must echo `scope_key`, a claimed scope must match, and a claimed `build_config` must be echoed. Otherwise it raises `RegistryTooOldError`, and a start is failed as orphaned (:644-655).
- The same gate applies to set_scope and resume.

**CLI commands** (`_cli/`):

| Group                | Commands                                                                                                                                                               |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---- | ------ | ---------------------- | ---------------------------- | ---- | ------- |
| Top level            | `version`                                                                                                                                                              |
| `auth`               | `login`, `logout`, `status`, `refresh`                                                                                                                                 |
| `config`             | `show`; `profile add                                                                                                                                                   | list | remove | use`; `list workspaces | environments`; `registry add | list | remove` |
| `environment`        | `list`, `create`, `delete`; `target-roots list                                                                                                                         | add  | remove | set`                   |
| `builds`             | `list`, `show`, `frontier`, `ticks`, `cancel`, `stop`, `cleanup`                                                                                                       |
| `tasks`              | `list`, `cancel`, `retry`                                                                                                                                              |
| `concurrency-limits` | `list`, `set`, `delete`, `holders`, `evict`                                                                                                                            |
| `modal`              | `deploy` (finalize, then deploy with the code-id tag, then record the deployment), `deployments` (list the records and mark the current one), `stardag-api-key create` |
| `self-host`          | `up`, `upgrade`, `connect`, `status`, `destroy`                                                                                                                        |

## 6. Pickle / task store (STA-69 context)

The task-pickle store has been **retired**:

- "pickle store was retired" (`integration/modal/_tick.py:547-549`, `integration/modal/_bootstrap.py:65`, `build/_reactive/_frontier_actions.py:209-212`);
- `StardagApp(require_pickle_free=…)` is deprecated and ignored (`_app.py:324, 589-590, 719-724`).

Remaining pickle dependencies:

1. Modal call arguments (cloudpickle by value): roots to `bootstrap` and `build` (`_app.py:1170-1175, 1476-1480`), and a task object to `worker.remote(task, env_overrides=…)` (`integration/modal/_executor.py:449-450, 515-519`). The tick's object comes from `task_data`, so what is pickled is a registry-rehydrated object.
2. `AliasTask.loads_type`, base64-pickled into its serialization (`_core/alias_task.py:164-171`). It is unpickled by `BaseTask.resolve` (`_core/base_task.py:385-397`) and refused by `task_from_registry_data`.
3. `PickleSerializer` for task outputs (`target/serialize.py:415-418`). This is an output format, not a task store.
4. Functions are registered `serialized=True`, so the closures are pickled by reference to their defining module (`integration/modal/_container_setup.py:8-21`).

## 7. Public API affected by the v2

These are the names affected by `significant: bool` replacing `significance`, by two-hash identity, and by deleting config transport.

**Top-level `sd.*`** (`__init__.py:54-60, 94-137`):

- `StardagField`, `Significance`, `StardagBaseModel`;
- `build_config_scope`, `get_build_config`, `set_build_config`, `BuildConfigError`, `UnknownTaskClassError`;
- `RegistryTooOldError`;
- `task_from_registry_data`, `TaskRehydrationError`;
- `BaseTask.id`, `TaskRef`, `AliasTask` / `AliasedMetadata`, `get_default_relpath` (id in the path);
- `build`, `build_aio`, `build_sequential`, `build_sequential_aio` (the `build_config=` kwarg).

**Module-level public:**

- `stardag.base_model.field_significance`, `CONTEXT_MODE_KEY`;
- `stardag.build_config.{BuildConfig, structure_config_hash, canonical_structure_config, jsonable_build_config, rebind_to_build_config, register_build_config_class, get_build_config_class, task_config_key, resolve_field_value}`;
- `stardag._core.task_id.task_uuid5_namespace_provider`;
- `stardag.exceptions.{ScopeMismatchError, BuildConfigMismatchError}` (not re-exported at top level);
- `stardag.build.{discover_and_register_aio, DiscoveryResult, RollOver, RollOverFailed, run_tick_aio, TickConfig, TickSummary}`;
- `stardag.registry.{DeploymentInfo, BuildInfo (build_config, scope_key), BuildFrontier (scope_key), DERIVE_DEPENDENCIES, RegistryABC.build_set_scope/deployment_*}`;
- `stardag.integration.modal.{StardagApp (code_id, build_trigger/build_remote build_config=), ModalTaskExecutor(build_config=, scope_key=), WorkerSelection, WorkerSelector, RunFunction}`, where `env_overrides` itself stays but carries `STARDAG_BUILD_CONFIG` / `STARDAG_SCOPE_KEY`;
- `STARDAG_CODE_ID` (env var).

**Docs hits** (grep for significance, hash_exclude, build_config, env_overrides, task_id, code_id, StardagField, compat_default):

| File                                              | Hits | Terms                                                  |
| ------------------------------------------------- | ---- | ------------------------------------------------------ |
| `docs/docs/concepts/parameters.md`                | 11   | StardagField, build_config, hash_exclude, significance |
| `docs/docs/how-to/evolve-dags.md`                 | 12   | same four                                              |
| `docs/docs/how-to/integrate-modal.md`             | 7    | build_config, env_overrides, significance, task_id     |
| `docs/docs/concepts/build-execution.md`           | 2    | build_config, significance                             |
| `docs/docs/concepts/index.md`                     | 1    | significance                                           |
| `docs/docs/concepts/modal-orchestration.md`       | 1    | code_id                                                |
| `docs/docs/how-to/define-tasks.md`                | 2    | task_id                                                |
| `docs/docs/configuration/cli.md`                  | 2    | task_id                                                |
| `docs/docs/platform/api.md`                       | 3    | task_id                                                |
| `docs/design/scope-keyed-dependency-structure.md` | 10   | —                                                      |
| `docs/design/README.md`                           | 1    | —                                                      |
| `docs/design/execution-claims-and-liveness.md`    | 3    | —                                                      |
| `docs/design/executions-as-records.md`            | 1    | —                                                      |

Terms like scope, rollover, dependencies_only and build_config_scope also appear in parameters.md, evolve-dags.md, integrate-modal.md, modal-orchestration.md and build-execution.md.

## 8. Contradictions and surprises

1. **`family--<code_id>` is stale in prior planning notes.** Earlier notes for build-scoped-dag-structure said "Deployments: `family--<code_id>` handles behind a registry `deployments` record" (maintainer notes). The code has no handle: `(app_name, code_id)` with in-place redeploy and rollover. The design doc itself marks the family scheme as replaced (`docs/design/scope-keyed-dependency-structure.md:280-291`).
2. **The STA-41 conflict detection is gone from the SDK.** There is no `cancel_conflicting` and no declaration-conflict error. Scopes superseded it, and only `scope_mismatch` / `BuildConfigMismatchError` remain.
3. **`reactive_discovery="local"` plans under the trigger machine's code id.** `run_reactive_bootstrap` runs in-process (`_app.py:1623-1636`), so `code_id()` is the laptop's SHA or uuid4, not the deployment's baked id. The first deployed tick then sees "other code" and must roll over. It only can if the current deployment record matches, and otherwise it exits superseded until one does.
4. **Dynamic-dependency ordering differs between engines.** The reactive worker does register, then edges, then suspend (`_runner.py:742-755, 860-910`). The resident engine does suspend, then register, then edges (`_concurrent.py:1116-1150`).
5. **Instance dedupe is nondeterministic.** First-seen-wins under a concurrent phase 1 means that if two instances share an id (only possible via legacy `hash_exclude`), which one's `requires()` and `task_data` win is race-dependent (`_discovery.py:279-312`).
6. **The legacy `hash_exclude` is a live third mode.** It is hash-excluded, passable at init and stored in `task_data`, so the registry payload is not a pure function of the id while such fields exist. This contradicts the docstring at `registry/_api_registry.py:3123-3128`.
7. **The level-2/3 refusal is validator-only.** `model_copy(update=)` and `model_construct` bypass it, and `sd.set_build_config` is public, so "central" is enforced by convention.
8. **A trigger's `build_start` sends `build_config` but no scope.** The server assigns `build:<id>`, and the bootstrap later sets the scope via PUT (`_app.py:1452-1457`, `_bootstrap.py:298-300`). A resident `sd.build` sends both at start (`_concurrent.py:780-782`).
9. **`model_fields_set` plays no role.** Explicitness is key presence in the raw input.
10. **Worker env is also in `os.environ`.** Config and scope travel as env vars inside `env_overrides`, applied with `temp_env_vars`, so user code in the worker sees `STARDAG_BUILD_CONFIG` in its environment too (`_runner.py:1083-1087`).
