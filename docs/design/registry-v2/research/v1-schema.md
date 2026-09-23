# v1 schema: inventory and claim check

> Point-in-time research note, 2026-09-23, against `stardag-dev/stardag` at
> commit `f96fb751` (then `main`). Written by a Claude agent for the STA-105
> design; verified against the models and migrations at that commit. Not
> maintained: read [../design.md](../design.md) for the design and
> [../plan.md](../plan.md) for current status.

**Migrations:** 24, in one linear chain from `4f69a5df4d93` (initial) to head
`a3c1f0d47b28` (`20260921_150000_add_tasks_latest_execution_id.py`). The one
before it is `690e61e0c920` (scope-keyed edges and deployments).

Paths below are relative to `app/stardag-api/src/stardag_api/models/` (M) or
`app/stardag-api/migrations/versions/` (V). Every table except
`distributed_locks` has a `created_at timestamptz NOT NULL`, from
`TimestampMixin` (`M/base.py:45`) or declared directly. There are **no CHECK
constraints anywhere**: grepping for `CheckConstraint` in `src/` and
`migrations/` finds nothing. Enum columns are either `String(32)` with no DB
check (task, build and event statuses) or native PG enums (`workspacerole`,
`invitestatus`, group b only).

## 1. Schema inventory

### (a) Core tables

#### `tasks` (`M/task.py`)

| Column                                  | Type          | Null | Notes                                                                       |
| --------------------------------------- | ------------- | ---- | --------------------------------------------------------------------------- |
| id                                      | uuid (uuid7)  | PK   | :81                                                                         |
| task_id                                 | varchar(64)   | NO   | SDK hash; index `ix_tasks_task_id` :88                                      |
| environment_id                          | uuid          | NO   | FK environments CASCADE; index :93                                          |
| task_namespace                          | varchar(255)  | NO   | default `""` :101                                                           |
| task_name                               | varchar(255)  | NO   | index `ix_tasks_task_name` :106                                             |
| task_data                               | JSONB         | NO   | :115 (JSON→JSONB in `V/20260427_212342`)                                    |
| version                                 | varchar(64)   | yes  | :121                                                                        |
| output_uri                              | varchar(2048) | yes  | :124                                                                        |
| is_phantom                              | bool          | NO   | :128 (see Surprises)                                                        |
| latest_status                           | varchar(32)   | NO   | index `ix_tasks_latest_status` :142                                         |
| latest_status_at                        | timestamptz   | yes  | :148                                                                        |
| latest_status_expires_at                | timestamptz   | yes  | claim expiry, not indexed :185                                              |
| latest_preempted_at                     | timestamptz   | yes  | never cleared :203                                                          |
| latest_status_event_id                  | uuid          | yes  | **no FK** :206                                                              |
| latest_status_build_id                  | uuid          | yes  | FK builds SET NULL (`fk_tasks_latest_status_build_id`, `V/20260427_213529`) |
| latest_status_scope_key                 | varchar(96)   | yes  | :218                                                                        |
| latest_started_at / latest_completed_at | timestamptz   | yes  | :219/:222                                                                   |
| latest_error_message                    | text          | yes  | :225                                                                        |
| latest_waiting_for_lock                 | bool          | NO   | :226                                                                        |
| latest_commit_hash                      | varchar(64)   | yes  | :229                                                                        |
| latest_execution_id                     | uuid          | yes  | claim identity, no index :261                                               |
| latest_executor                         | varchar(32)   | yes  | :268                                                                        |
| latest_executor_ref                     | varchar(255)  | yes  | :269                                                                        |
| latest_executor_metadata                | JSONB         | yes  | :275                                                                        |

- **Unique:** `uq_task_environment_taskid (environment_id, task_id)` at :46.
- **Indexes:** `ix_tasks_environment_name (env, task_name)`,
  `ix_tasks_environment_namespace`, `ix_tasks_environment_created`,
  `ix_tasks_environment_status (env, latest_status, latest_status_at)`
  (:49-77), plus the single-column indexes on task_id, environment_id,
  task_name and latest_status.

#### `builds` (`M/build.py`)

| Column                                  | Type            | Null   | Notes                            |
| --------------------------------------- | --------------- | ------ | -------------------------------- |
| id                                      | uuid            | PK     | :67                              |
| environment_id                          | uuid            | NO     | FK CASCADE, index :72            |
| user_id                                 | uuid            | yes    | FK users SET NULL, index :78     |
| name                                    | varchar(64)     | NO     | slug, index :86                  |
| description                             | text            | yes    | :89                              |
| commit_hash                             | varchar(64)     | yes    | display only, index :94          |
| scope_key                               | varchar(96)     | **NO** | default `build:<id>` :109-117    |
| build_config                            | JSONB           | yes    | :125                             |
| root_task_ids                           | JSONB list[str] | NO     | task_id **hashes**, no FK :132   |
| last_active_at                          | timestamptz     | NO     | :152                             |
| executor_metadata                       | JSONB           | yes    | :165                             |
| needs_tick_at                           | timestamptz     | yes    | dirty flag :175                  |
| tick_requested_at                       | timestamptz     | yes    | wake hand-out :187               |
| scheduler_lease_until                   | timestamptz     | yes    | :208                             |
| scheduler_lease_owner                   | varchar(64)     | yes    | :215                             |
| reactive_app_name                       | varchar(64)     | yes    | index :229                       |
| reactive_tick_kwargs                    | JSONB           | yes    | :243                             |
| latest_status                           | varchar(32)     | NO     | :265                             |
| latest_started_at / latest_completed_at | timestamptz     | yes    | :273/:278                        |
| latest_status_triggered_by_user_id      | varchar(255)    | yes    | a user `external_id`, no FK :285 |
| latest_is_resumed                       | bool            | NO     | :290                             |

- **Indexes:** `ix_builds_environment_created`,
  `ix_builds_environment_last_active`, `ix_builds_environment_status (env,
latest_status, last_active_at)` (:40-64), plus single-column indexes.
- **No unique constraints** other than the PK.

#### `task_dependencies` (`M/task_dependency.py`)

| Column             | Type        | Null    | Notes                    |
| ------------------ | ----------- | ------- | ------------------------ |
| id                 | uuid        | PK      | :65                      |
| upstream_task_id   | uuid        | NO      | FK tasks.id CASCADE :71  |
| downstream_task_id | uuid        | NO      | FK tasks.id CASCADE :76  |
| scope_key          | varchar(96) | **yes** | NULL = pre-scope row :84 |
| is_dynamic         | bool        | NO      | server_default false :95 |

- **Unique:** `uq_task_dependency_scope_edge (scope_key, upstream_task_id,
downstream_task_id)` at :51. It replaced `uq_task_dependency_edge
(upstream, downstream)` in `V/20260919…:163-176`.
- **Indexes:** `ix_task_dep_upstream`, `ix_task_dep_downstream`,
  `ix_task_dep_scope_downstream (scope_key, downstream_task_id)` (:57-62).

#### `events` (`M/event.py`)

| Column         | Type        | Null   | Notes                              |
| -------------- | ----------- | ------ | ---------------------------------- |
| id             | uuid        | PK     | :43                                |
| build_id       | uuid        | **NO** | FK builds CASCADE, index :50       |
| task_id        | uuid        | yes    | FK **tasks.id** CASCADE, index :58 |
| event_type     | varchar(32) | NO     | index :65                          |
| created_at     | timestamptz | NO     | index :72                          |
| scope_key      | varchar(96) | yes    | :90                                |
| error_message  | text        | yes    | :93                                |
| event_metadata | JSONB       | yes    | :97                                |

- **Indexes:** `ix_events_build_created`, `ix_events_task_created`,
  `ix_events_type_created`, `ix_events_build_task_type (build, task, type)`,
  `ix_events_build_scope (build, scope_key)` (:34-40), plus five
  single-column indexes.
- **Event types:** 6 build and 13 task types (`M/enums.py:59-101`), including
  `TASK_INTERRUPTED` and `TASK_PREEMPTED`.

#### `deployments` (`M/deployment.py`, created in `V/20260919…:74-105`)

| Column         | Type            | Null       |
| -------------- | --------------- | ---------- |
| id             | uuid            | PK         |
| environment_id | uuid FK CASCADE | NO (index) |
| app_name       | varchar(64)     | NO         |
| code_id        | varchar(64)     | NO (index) |
| deployed_at    | timestamptz     | NO         |
| modal_app_id   | varchar(64)     | yes        |

- **Unique:** `uq_deployment_app_code (environment_id, app_name, code_id)`.
- **Index:** `ix_deployments_environment_app_deployed (env, app_name,
deployed_at)` (:35-45).
- No other table has an FK to it.

#### `build_tick_summaries` (`M/build_tick_summary.py`)

- **Columns:** id (PK), build_id (uuid NO, FK builds CASCADE), outcome
  (varchar(32) NO), summary (JSONB NO), created_at.
- **Index:** `ix_build_tick_summaries_build_created (build_id, created_at)`
  only (:54).
- Pruned to the newest N rows per build on insert (:41).

#### `environment_concurrency_limits` (`M/concurrency_limit.py:29`)

- **Columns:** id (PK), environment_id (FK CASCADE, index), key (varchar(255)
  NO), max_concurrent (int NO).
- **Unique:** `uq_environment_concurrency_limit_key (environment_id, key)`.
- No `created_at`.

#### `task_limit_keys` (`M/concurrency_limit.py:56`)

- **Columns:** id (PK), task_pk (uuid NO, FK tasks.id CASCADE, index), key
  (varchar(255) NO).
- **Unique:** `uq_task_limit_key (task_pk, key)`.
- **Index:** `ix_task_limit_keys_key`.
- No `created_at`.
- A slot is occupied by this row together with `tasks.latest_status =
RUNNING` (:6-8, :59-61).

#### `distributed_locks` (`M/lock.py`)

| Column                   | Type                         | Null               |
| ------------------------ | ---------------------------- | ------------------ |
| **name**                 | text                         | **PK alone** (:34) |
| environment_id           | uuid FK CASCADE              | NO                 |
| owner_id                 | uuid (varchar(36) on sqlite) | NO                 |
| acquired_at / expires_at | timestamptz                  | NO                 |
| version                  | bigint                       | NO                 |

- **Indexes:** `ix_distributed_locks_environment_expires`,
  `ix_distributed_locks_expires`.

#### `task_artifacts` (`M/task_artifact.py`, renamed from `task_registry_assets` in `V/20260225`)

- **Columns:** id (PK), task_pk (FK tasks.id CASCADE), environment_id (FK
  CASCADE), artifact_type (varchar(50) NO), name (varchar(255) NO), body_json
  (JSONB NO), created_at.
- **Unique:** `uq_task_artifact_task_type_name (task_pk, artifact_type, name)`.
- **Indexes:** `ix_task_artifacts_task_pk`, `ix_task_artifacts_environment`,
  `ix_task_artifacts_environment_created`.

### (b) Tables the redesign should leave alone

| Table               | Summary                                                                                                                                                                      |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `users`             | id, external_id (unique), email (unique), display_name, password_hash, password_changed_at (`M/user.py`)                                                                     |
| `workspaces`        | id, name (non-unique since `V/20260202`), slug (unique), description, created_by_id → users SET NULL, is_personal                                                            |
| `environments`      | id, workspace_id → CASCADE, name, slug, unique (workspace_id, slug), owner_id → users, **`max_concurrent_locks int`**, which feeds the lock service (`services/lock.py:120`) |
| `workspace_members` | (workspace_id, user_id) unique, role as PG enum `workspacerole`                                                                                                              |
| `invites`           | workspace_id, email, role and status as PG enums; partial unique index (workspace_id, email) WHERE status='pending' (`V/20260129…`)                                          |
| `api_keys`          | environment_id → CASCADE, name, key_prefix (indexed), key_hash, created_by_id, last_used_at, revoked_at (indexed)                                                            |
| `target_roots`      | environment_id, name, uri_prefix(512), unique (environment_id, name)                                                                                                         |

## 2. The seven claims

| #   | Claim                                                    | Verdict                                           | Evidence                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| --- | -------------------------------------------------------- | ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | One `task` row carries identity and state                | **VERIFIED**                                      | Identity: task_id, namespace, name, task_data, version, output_uri (`M/task.py:88-124`), unique `(environment_id, task_id)` at :46. On the same row: status (`latest_status*`), claim (`latest_status_expires_at`, `latest_execution_id`, `latest_status_build_id` as owner, see `services/claims.py:68-93`) and executor columns (`latest_executor*`) at :142-278. Uniqueness is per **environment**, not global. State is environment-global: `latest_status` is "environment-global" (`services/build_cleanup.py:5-7`).                                                                                                                   |
| 2   | Edges are keyed by structure scope                       | **VERIFIED, with nuances**                        | The edge key is unique `(scope_key, upstream_task_id, downstream_task_id)` (`M/task_dependency.py:51-56`), and the upstream/downstream columns are **`tasks.id` PKs**, not task_id hashes. `scope_key` is the string `<code_id>:<config_hash>` (40-char SHA or 32-char hex, colon, 16-hex hash), or the synthetic `build:<uuid>`, or NULL for legacy rows (:17-21, :39-41). It is not a composite FK: code_id is a string prefix and nothing references `deployments`. `tasks.latest_status_scope_key` is a varchar(96) copy of the scope of the build that produced the task's current status, frozen at status time (`M/task.py:211-218`). |
| 3   | Deployments: `family--<code_id>` handles behind a record | **PARTLY**                                        | The table holds only `(environment_id, app_name, code_id, deployed_at, modal_app_id)`, unique per (env, app, code) (`M/deployment.py:35-73`). The newest row per app is "current" (:9-10). **No column stores a `family--<code_id>` handle.** No build FK points at a deployment; the link is `builds.reactive_app_name` = `deployments.app_name` (string match) plus the code_id prefix of `builds.scope_key`.                                                                                                                                                                                                                              |
| 4   | Executions as records (STA-50)                           | **CONTRADICTED as a table, VERIFIED as a column** | There is no `executions` table in the models or migrations. `docs/design/executions-as-records.md:12-22` says "The `executions` table is not planned. What shipped is one column, `tasks.latest_execution_id`". The column is minted by the caller, set by a claiming start and cleared only by TASK_RETRIED (`M/task.py:231-261`; `V/20260921…:17-29`, no backfill, no index). The history of past executions exists only as `events` rows (TASK_STARTED metadata).                                                                                                                                                                         |
| 5   | What `build` holds                                       | **VERIFIED (details)**                            | `build_config` JSONB is `{"<ns>.<Name>": {field: value}}` for `dependencies_only` **and** `execution_only` fields, immutable for the build's life (`M/build.py:119-128`; a change is 409 `scope_mismatch` per `schemas.py:109-124`). **There is no `env_overrides` column** and no deployment FK. `scope_key` is NOT NULL (:109). Status is a **denormalised column** (`latest_status` + 4 companions, :248-292), maintained in-transaction by `services.status.apply_event_to_build` (`services/status.py:1096`). Events stay the source it is folded from.                                                                                 |
| 6   | Where status history and events live                     | **VERIFIED: per build, with a global fold**       | All history is in `events`. `build_id` is NOT NULL on every event (`M/event.py:50-55`) and `task_id` is nullable for build-level events. Per-build task status is a replay over events (`services/status.py:1179 get_task_status_in_build`), as are per-build attempt and interrupt counts (:499, :633). The global status is the denormalised `tasks.latest_*`, folded from every build's events (:1334/1375 plus `_apply_event_to_task` :759). No per-(task, build) state table exists.                                                                                                                                                    |
| 7   | Where the task body lives                                | **VERIFIED**                                      | The body is in `tasks.task_data` JSONB (`M/task.py:115`). The SDK sends a **registry-mode** dump holding identity parameters only and none of the `dependencies_only`/`execution_only` fields (`lib/stardag/src/stardag/registry/_api_registry.py:3123-3128, 3161-3163`). Those values are stored only in `builds.build_config`. `task_data` is first-write-wins: inserts use `ON CONFLICT (uq_task_environment_taskid) DO UPDATE … WHERE false` (`routes/builds.py:3103-3112`), so it is never updated.                                                                                                                                     |

## 3. Items not in the redesign summary that key on task, build or scope

| Item                             | Keys on                                                      | Why it must be re-pointed                                                                                                                                                                                                                                               |
| -------------------------------- | ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `task_limit_keys`                | `tasks.id` (task_pk)                                         | Slot = row + `tasks.latest_status=RUNNING`. Tied to the global task row, not to a build or execution, and keys persist across executions (`_api_registry.py:3130-3133`). If state moves off `tasks`, the slot definition moves with it.                                 |
| `environment_concurrency_limits` | (env, key)                                                   | Enforced by locking FOR UPDATE and counting RUNNING holders in `start_task` (`M/concurrency_limit.py:10-12`). The count query depends on where RUNNING lives.                                                                                                           |
| `distributed_locks`              | `name` (usually a task_id hash)                              | Global PK, and `environments.max_concurrent_locks` caps it. The `latest_waiting_for_lock` column and the `TASK_WAITING_FOR_LOCK` event are its footprint on tasks.                                                                                                      |
| `task_artifacts`                 | `tasks.id`                                                   | Artifacts belong to the identity row and are unique per (task, type, name). A new identity table means a new FK target.                                                                                                                                                 |
| `events`                         | build_id (NOT NULL) + tasks.id + scope_key                   | Registration events (`TASK_PENDING`/`TASK_REFERENCED`) with `scope_key` **are the plan membership** of a build under a scope (`M/event.py:79-90`; index `ix_events_build_scope`). Plan membership is not a table of its own.                                            |
| Interruption and preemption      | events plus `tasks.latest_preempted_at` and the claim expiry | Counts come from TASK_INTERRUPTED rows per build (`services/status.py:633`). "Restart outstanding" is derived as `latest_preempted_at > latest_status_at` (`M/task.py:188-205`).                                                                                        |
| Refused reports                  | `events.event_metadata[REPORT_APPLIED_KEY]=false`            | A JSON flag that changes query semantics. Both attempt-stream queries must filter on it (`services/status.py:375-407`). It is a typed column in all but name.                                                                                                           |
| `build_tick_summaries`           | build_id                                                     | Observability trail with CASCADE. Cheap to keep, but it follows the build PK.                                                                                                                                                                                           |
| Wake-ups, lease, dirty flag      | `builds` columns                                             | `needs_tick_at`, `tick_requested_at`, `scheduler_lease_*`, `reactive_app_name`, `reactive_tick_kwargs`. There is no wakeups table; `services/wakeups.py` reads only these (:67-323).                                                                                    |
| Build cleanup and reaper         | builds + events                                              | `last_event_at_subquery` = max(events.created_at) per build (`services/build_cleanup.py:58`). The cascade cancel uses `BUILD_OWNED_STATUSES` (RUNNING/SUSPENDED/INTERRUPTED) on `tasks.latest_status` with owner `latest_status_build_id` (`services/claims.py:60-93`). |
| `builds.root_task_ids`           | JSON list of task_id **hashes**                              | No FK. A second way of referring to tasks, by hash rather than by PK.                                                                                                                                                                                                   |
| `tasks.latest_status_build_id`   | builds FK SET NULL                                           | The claim owner. With SET NULL, a deleted build turns "owner gone" into "revocable by anyone" (`claims.py:85-87`).                                                                                                                                                      |

## 4. Surprises

1. **`tasks.is_phantom` is dead.** Migration `690e61e0c920` deletes every
   phantom row (`V/20260919…:108`). All three writers hardcode `False`
   (`routes/builds.py:3172, 3509, 3800`), yet the column stays and is still
   exposed in `TaskResponse` (`schemas.py:322`, `routes/builds.py:5015`).
   `TaskStatus.UNREGISTERED` ("Phantom task", `M/enums.py:26`) is probably
   dead too.
2. **The `distributed_locks` PK is `name` alone** (`M/lock.py:34`), although
   the docstring says locks are environment-scoped (:21) and names are
   usually task_id hashes. The existing-lock lookup in `acquire` does not
   filter by environment (`services/lock.py:176`), and the upsert conflicts
   on name. Two environments using the same lock name collide.
3. **The edge uniqueness does not hold for legacy rows.** `scope_key` is
   nullable inside `uq_task_dependency_scope_edge`, and Postgres NULLs are
   distinct, so NULL-scope edges are no longer deduplicated. The migration
   also copies legacy edges into each running build's scope
   (`V/20260919…:177-207`), so the same logical edge now exists once per
   scope as physical rows.
4. **`tasks.latest_status_event_id` has no FK** to events (`M/task.py:206`),
   while `latest_status_build_id` does.
5. **Deployments are an island.** No FK connects builds to deployments. The
   join is `reactive_app_name` = `app_name` (string) plus the prefix of
   `scope_key`, which only the resolver may parse.
6. **Redundant single-column indexes** sit next to composites that lead with
   the same column: `ix_events_build_id`, `ix_events_task_id`,
   `ix_events_event_type`, `ix_tasks_environment_id`, and
   `ix_tasks_latest_status`, which spans every environment and whose own
   model comment calls it insufficient (`M/task.py:58-62`). Every event
   write pays for all of them.
7. **Server defaults were removed after backfill** on `tasks.latest_status`,
   `tasks.latest_waiting_for_lock`, `builds.latest_status` and
   `builds.latest_is_resumed` (`V/20260427_213529`, `V/20260807_235511`).
   Only Python-side defaults remain, so raw-SQL inserts must supply them.
8. **`builds.scope_key` default is a context lambda** that reads the row's
   own client-generated id (`M/build.py:116`). That works only because the
   id default runs first.
9. **Model vs migration naming:** the migration creates the FK as
   `fk_tasks_latest_status_build_id`, but the model leaves it unnamed
   (`M/task.py:209`). Harmless for autogenerate. No type or nullability
   mismatches were found between the models and the head migration.
10. **Events are called IMMUTABLE** (`M/event.py:22`), but correctness
    depends on a JSON marker in `event_metadata` that decides whether an
    event counts. It is written when the event is created
    (`services/status.py:823-1026`, inside the fold), so rows are not
    mutated later. It is still a schema-level concept hidden in JSON.
11. **Event FKs are `CASCADE` from both builds and tasks**, so deleting a
    build erases the task history it contributed, and with it the global
    fold's source events, while `tasks.latest_*` keeps the folded result.
