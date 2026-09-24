# Changelog

All notable changes to the Stardag project (SDK, Registry API, and UI).

For detailed SDK migration guides, see [RELEASE_NOTES.md](RELEASE_NOTES.md).

## [Unreleased — v2 line]

> **TODO(Anders):** the version numbers (SDK and server image) and the
> release date replace this heading's placeholder. The `[Unreleased]` entry
> below is v1-line work; whether it ships as a last v1 release first or
> folds into this line is also yours to decide. #387 (CLI), #389 (live
> scenarios) and #390 (server reads, `lost`, artifact quota) were open when
> this entry was drafted: re-check their items against what merged.

A new release line of the SDK, the registry server, the CLI and the UI
together, on new registry entities: **task** (a completion and its claim),
**task instance** (a task as constructed under a scope, with its parameter
body and its dependency edges), **plan** (one build's request under one
scope), **deployment** and the **execution** ledger. Fully breaking: no data
migration (an existing registry starts empty), no compatibility with v1
SDKs or servers in either direction. The design is
`docs/design/registry-v2/design.md`; the principles it rests on are
`docs/design/principles.md`. See [RELEASE_NOTES.md](RELEASE_NOTES.md) for
the migration and the operator notes.

### SDK

- **Breaking: one flag replaces the significance levels.**
  `StardagField(significant: bool = True)`. A `significant=False` field is
  an ordinary parameter, passed at init and stored on the instance body; it
  is out of the task id and in the instance hash. `StardagField(
significance=...)` and `StardagField(hash_exclude=...)` are removed and
  raise `TypeError` naming the replacement; `Significance` is removed from
  the public API. `compat_default` stays, on significant fields only, and so
  do custom `"hash"`-mode serializers: user control over hashing is on the
  task id only.
- **New: `BaseTask.instance_hash`** (and `BaseTask.instance_body()`): uuid5
  over the canonical JSON of all fields, defaults included, nested tasks as
  their full body. It is the hash of the body the registry stores. Sets are
  sorted in the registry-mode dump as well as in hash mode.
- **New: `sd.check_serialization_stability(task)` and
  `sd.UnstableSerializationError`.** At registration, each distinct instance
  is round-tripped once (`dump(validate(dump(x))) == dump(x)`, task id
  unchanged); a field that moves fails the build at the trigger, naming it.
- **New: `sd.InstanceConflictError`.** Two different constructions of one
  task id in one discovery pass fail the build at the trigger, naming the
  differing fields and both construction paths. One plan holds one instance
  per task id.
- **Breaking: `settings` replaces the build config.** `sd.build(settings=...)`
  and `build_trigger(settings=...)` take a flat `dict[str, str]` applied as
  environment variables in every process of the build, and part of the
  build's scope. `STARDAG_*` and `MODAL_*` keys are refused. Removed:
  `build_config`, `sd.build_config_scope`, `sd.get_build_config`,
  `sd.set_build_config`, the ContextVar transport and the
  `STARDAG_BUILD_CONFIG` / `STARDAG_SCOPE_KEY` variables.
- **New: `stardag.build.SettingsError`.** A process applying settings serves
  one build at a time; a second build entering while another build's
  settings are installed raises it. Deployed ticks, workers and the
  bootstrap run one input per container, and a `max_concurrent_inputs`
  above one on them is refused at deploy.
- **Breaking: the registry client speaks `/api/v2` only.** `APIRegistry` and
  `RegistryABC` are rewritten around plans: plan create, members registered
  in post-order chunks, seal, and one `/yield` per dynamic-dependency batch
  with a client-minted `batch_id`. Build, plan, execution, deployment and
  batch ids are client-minted uuid7s. Refusals surface as `APIError` with
  `.code`; 429 `rate_limited` is retried per `Retry-After`. Removed:
  `RegistryTooOldError`, `SDKVersionUnsupportedError`, `ScopeMismatchError`,
  `BuildConfigMismatchError`, the version gate and the `/locks` client.
- **New: deployments as scope.** `stardag modal deploy` mints
  `STARDAG_DEPLOYMENT_ID` and bakes it into the app's secret, replacing the
  code id in that role. A local build plans under a `local` deployment
  looked up by code id (`STARDAG_CODE_ID`, else a clean git SHA, else a
  fresh id). A hybrid `sd.build()` whose tasks run on a Modal app, and
  `reactive_discovery="local"`, plan under the app's current deployment.
- **Changed: every execution claims.** In-process (thread and process pool)
  executions hold a claim with a TTL that the resident driver renews; the
  distributed lock is gone. The resident engine yields without suspending.
- **Changed: rollover by deployment id.** A tick whose deployment differs
  from the active plan's re-plans the build if its deployment is the app's
  current one, and exits `superseded` otherwise.
- **Changed: rehydration.** Strict for significant fields (the task id must
  match), lenient for the rest: an unknown non-significant key is dropped
  with a warning, a missing one takes the class default.
- **Fixed: a mounted Modal Volume could serve a deleted target as
  present** in a warm container, so discovery saw it complete and never
  invalidated it. Each discovery walk begins an observation fence, and a
  mounted-volume hit older than it reloads the volume once per walk.
- **Restored: `TickConfig.max_interruptions` (default 20).** An
  INTERRUPTED member is actionable, so a task that asks to be resumed on
  every run was restarted forever. The frontier's per-member
  `interruptions` (counted from the execution ledger over the build's plans)
  is now read: at the cap the tick claims the member without spawning it
  and records `TASK_FAILED` naming the count, and the build's fail mode
  applies. `FrontierMember` gains `attempts` and `interruptions`. A FAILED
  member is still never retried by the tick (the fail mode decides);
  `max_attempts` still covers only a failed spawn. Also accepted in
  `tick_kwargs`.
- **New: client reads on routes the server already served.**
  `RegistryABC.plan_get` (`GET /plans/{id}`), `build_list_plans`,
  `build_list_page` (cursor paging with `total` and `next_cursor`),
  `task_list` (`GET /tasks`, status filter and paging),
  `task_list_executions` (`include_ended`), `task_events` and
  `deployment_get`; `BuildInfo.error_message`, `TaskInfo.claim_plan_id` /
  `claim_build_id` and `ExecutionInfo.build_id`. The in-memory registry
  serves them with the server's shapes and orderings.
- **Fixed: a watchdog sweep landing on a lingering tick was dropped.** A
  tick refused the scheduler lease now flags the build before exiting
  `lease_held`, so the holder acts on it (or a successor is spawned if the
  holder had already left); a lapsed claim no longer waits a full watchdog
  period for its takeover.
- **Changed: a driver whose build the registry stopped stops cleanly.**
  When a claiming start is refused `build_not_running` (an operator
  cancelled the build), `build`/`build_aio` and the sequential engines stop
  — in-flight work cancelled, no member skipped, no `build_failed` written —
  and return a new `BuildExitStatus.STOPPED` summary carrying a
  `BuildStopped` error; a lifecycle report refused `build_terminal` does the
  same. A refused claim renewal now says whether the build released the
  claim or another execution took it over. A reactive tick counts such a
  claim as denied and treats a refused `/complete` or `/fail` as the
  status that stands, instead of ending in `error`.
- **Changed: a failed build's reason names the failed task.** The message
  written on `/fail` reads
  `Task <name> (<id>) failed: <error>; N downstream member(s) blocked`,
  N being what the registry skipped, instead of a `Deadlock: …` line with a
  wrong count (continue mode now skips transitively blocked tasks before
  checking for a deadlock). `BuildSummary.failed_task` names the task.
- **New: `raise_on_failure=False`** on the four build functions returns the
  `FAILURE` summary in fail-fast mode instead of raising the task's
  exception.
- **New: in-process executions record their executor.** The claiming start
  of a thread-, process-, async- or sequential-mode execution sets
  `executor` (the mode), `executor_ref` (`hostname:pid`) and
  `executor_metadata` (host, pid, Python version) on the execution row, as
  Modal executions record their call (`TaskExecutorABC.get_executor_details`).

### Server

- **Breaking: new schema, no data migration.** One migration drops the v1
  core tables (including `distributed_locks`) and creates `task`,
  `deployment`, `settings`, `task_instance`, `task_instance_dependency`,
  `plan`, `plan_member`, `execution`, `build_wake` and the re-pointed
  `event`, `task_artifact` and `task_limit_key`. Concurrency limits are
  among the dropped tables and must be set again. Downgrade raises.
  **PostgreSQL 15 or newer** is required (SQLite is not supported), and the
  API suite runs on Postgres.
- **Breaking: the registry routes move to `/api/v2`.** Every `/api/v1`
  registry route (builds, tasks, search, deployments, locks, concurrency
  limits, tick summaries) is removed, and so is the SDK version gate. Authentication, workspaces, environments and target
  roots stay under `/api/v1`; the version route moves to `GET
/api/v2/version` (same fields, unauthenticated), and `/api/v1/version`
  is removed. The `/locks` routes are
  gone: the claim is the only mutual exclusion, and in-process claims renew
  through `POST …/tasks/{task_id}/claim/renew`.
- **New: plans.** `POST /builds/{id}/plans`, chunked `POST
/plans/{id}/members`, `/seal`, and `POST
/plans/{id}/members/{task_id}/yield`; the frontier (runnable, discovery
  jobs, running, with attempt and interruption counts from the ledger) over
  the active plan. Registration is insert-if-absent throughout; conflicts
  are 409s with a code (`instance_conflict`, `instance_body_conflict`,
  `task_identity_conflict`, `root_instance_conflict`,
  `deployment_mismatch`, `not_claim_holder`, `upstream_incomplete`, …).
- **New: one `transition_task()` for every task event**, with one authority
  rule: a report is applied while it names the task's current execution,
  lapsed or not, and is late once that execution's claim was released. At
  most one terminal report per execution. A claiming start re-checks
  upstream completion under the task lock.
- **New: the execution ledger.** One row per claim granted, with the server's
  end (`claim_released_at`, `claim_outcome`) and the execution's own end
  (`ended_at`, `outcome`). `GET /builds/{id}/executions
[?not_in_current_plan=true]` lists unended and orphaned executions; `POST
/executions/{id}/stopped` ends one (outcome `stopped`, or `lost` for one
  that could not be stopped) and releases a claim it still holds.
- **New: deployments.** `POST /deployments` (client-minted id,
  server-assigned generation) before the deploy, `POST
/deployments/{id}/activate` after it; `GET /deployments` marks the current
  Modal deployment per app. Local deployments are created activated and are
  never current.
- **New: settings.** Stored by content hash, validated at the server
  (`reserved_settings_key`), read at `GET /settings/{hash}`.
- **Changed: invalidation follows the world only.** The one path out of
  COMPLETED is discovery observing the target missing (`TASK_INVALIDATED`,
  with the `observed_at` guard); there is no operator route.
- **Changed: a build's terminal transition releases its claims** (complete,
  fail and cancel alike, tasks set CANCELLED, which is actionable for other
  builds); `exit-early` releases nothing. Exclusion cascades downstream
  within the plan, and an excluded root fails the build.
- **Changed: a terminal build status is sticky.** A `complete`, `fail`,
  `cancel` or `exit-early` against a COMPLETED, FAILED or CANCELLED build is
  409 `build_terminal` and is recorded as its build event with
  `report_applied = false`; `resume` is the way out. A cancelled build no
  longer ends FAILED because its still-running driver reported a failure.
- **Changed: `skip-blocked` is a no-op on a CANCELLED or COMPLETED build**,
  so a driver racing an operator's cancel cannot skip the downstream of the
  tasks the cancel released.
- **Changed: the exclude response describes that call.** It gains
  `roots_excluded` (the roots this exclusion cascaded to), and
  `build_failed` now means this call failed the build.
- **Changed: a refused claim renewal says how the claim ended**
  (`claim_outcome`: `released` when the build stopped, `taken_over`, or
  `null` while merely lapsed).
- **Changed: deployment listings and `GET /deployments/{id}` drop `created`**, which only means
  something on the create and activate responses.
- **Changed: wake-up flags move to `build_wake`**, so flagging never locks
  the build row a claim holds.
- **New: read routes.** `GET /builds` (status and app filters, cursor
  paging), `GET /plans/{id}`, `GET /builds/{id}/plans`, `GET
/plans/{id}/graph`, `GET /plans/{id}/roots`, `GET /tasks` and `GET
/tasks/{id}` (with its instances, claim plan and build), `GET
/tasks/{id}/executions`, `GET /tasks/{id}/events`, `GET
/builds/{id}/events`, `GET /deployments/{id}`, artifacts through the
  member, `PUT/GET/DELETE /concurrency-limits/{key}`.
- **Changed: guardrails.** The rate limit applies to every v2 write route.
  The 24-hour creation quotas are per environment and charged only for rows
  a request inserted: `LIMITS_MAX_TASK_INSTANCES_PER_ENVIRONMENT_24H` (429
  `creation_quota_exceeded`) and `LIMITS_MAX_ARTIFACTS_PER_ENVIRONMENT_24H`
  (429 `artifact_creation_limit`). The four per-workspace settings
  (`LIMITS_MAX_{BUILDS,TASKS,EVENTS,ARTIFACTS}_PER_WORKSPACE_24H`) are not
  read by the v2 routes.

### CLI

- **New: `stardag build <module:attr ...>`** — roots from a task object, a
  list, a zero-argument callable or a task class with `--param KEY=VALUE`;
  `--settings KEY=VALUE` (repeatable), `--app module:attr` (with
  `--reactive`), `--resume`, `--dry-run`.
- **Changed: `stardag builds`** — `list`, `show`, `frontier`, `ticks`,
  `cancel`, and new `complete [--force]` and `fail`. **`builds stop`** lists
  the build's unended executions from the ledger, stops the Modal calls it
  can identify, reports each one stopped, and cancels the build unless
  `--no-cancel`; `--not-in-current-plan` selects orphans (and implies
  `--no-cancel`), `--mark-lost` ends executions it cannot stop as `lost`.
- **New: `stardag executions list`, `stardag plans show`, `stardag
deployments list`** (`stardag modal deployments` stays as an alias).
- **Changed: `stardag tasks`** — `list`, `show`, new `check` (runs
  `complete()` locally and prints the observation; reports nothing),
  `retry`, `cancel`, new `exclude`.
- **Changed: `stardag tasks retry` and `tasks cancel` ask for confirmation**
  again (v1's prompt), skipped with `--yes`; `--json` without `--yes` is
  refused rather than prompting. `--build` is now optional: it defaults to
  the build holding the task's claim (`claim_build_id` from `GET
/tasks/{id}`) and stays an override; a task holding no claim (a FAILED
  one) still needs it.
- **New: the CLI on the v2 reads.** `tasks list` (`--status`, `--limit`,
  `--cursor`; v1's `--older-than`, `--name` and `--namespace` need server
  support and are not offered); `tasks show` names the claim's holder (plan
  and build), lists the task's executions (`--include-ended`) and its last
  events (`--events N`), calling out every `TASK_STRUCTURE_DIVERGED`;
  `builds show` shows the failure reason and `last_active_at`; `builds
list` pages (`--cursor`, prints `total` and the next cursor), shows
  `last_active_at`, and takes `--reactive-app` as an alias of `--app`;
  `builds frontier` shows "Needs tick" (the wake-up flag, read without
  clearing it), member counts by status, roots completed out of total, and
  attempts/interruptions per runnable and running member; `plans show`
  works on a superseded plan (lifecycle, deployment, counts) and new `plans
list --build`; new `deployments show`; `executions list --task` and
  `--include-ended`; `builds stop --namespace` (v1's prefix filter, read per
  listed task). `builds cancel` and `concurrency-limits set/delete` gain
  `--json`, so every registry-backed command takes it. The usage block in
  `stardag --help`'s module docstring lists every command and flag.
- **Removed: `stardag builds cleanup`.**
- **Restored: `stardag concurrency-limits`** (`list [--holders]`, `set`,
  `delete`, `holders`) — dropped by omission between two v2 work packages
  (the server routes and client methods already existed); no `evict`, since
  a v2 slot is released by ending its execution (`stardag builds stop
--mark-lost` is the recovery path for a holder whose worker is gone).
  `list`/`holders` carry `in_use` and, with `--holders`, holder detail from
  one call (`GET /concurrency-limits?include_holders=true`), not one extra
  request per key as in v1.
- **Docs: `configuration/cli.md` matches the CLI** — `plans show` reads
  any plan, `tasks check --report` is described as what it is (accepted,
  then refused with exit 1), the `--json` claim now holds and names the
  commands outside it, the "Durations" grammar for `--older-than` is back,
  and the new commands and flags are documented. `platform/api.md` lists
  the served reads it missed (`GET /plans/{id}`, `/plans/{id}/graph`,
  `/builds/{id}/plans`, `GET /tasks`, `/tasks/{id}/executions`,
  `/deployments/{id}`). `DEV_README.md`, `reference/exceptions.md` and the
  self-hosting troubleshooting no longer describe v1's minimum-SDK gate
  (`SDKVersionUnsupportedError`, `426`): v2 has no version check in either
  direction, and a mismatched SDK and server fail on the first missing
  route (`NotFoundError`, `is_missing_route_error`).
- `stardag modal deploy` records the deployment before the deploy and
  activates it after; a failed create or activation exits non-zero.
- **Changed: a failed `stardag build` prints its summary.** Build id,
  status, the failed task and its error, in text and in `--json`, with exit
  code 1 — not the task's raw traceback (also for a `stopped` build).
- **Changed: `stardag tasks exclude` reports what that call did** — which
  roots it reached and whether it failed the build — instead of repeating
  "a root was excluded" once any root had been.

### UI

- **Changed: every call is on `/api/v2`.** Scope keys, `build_config`,
  phantom tasks, external blockers and lock views are gone.
- **Changed: the build view follows the active plan** — plan header
  (deployment, generation, settings, sealed), members, the DAG over instance
  edges (dynamic edges dashed, excluded members muted), the frontier with
  attempt counts, settings and deployment in the build info, and the stop
  list over the ledger with orphans marked.
- **New: the task page** (`/tasks/:task_id`) — status, the claim (live or
  lapsed, current execution), remedies through the viewed build's plan, the
  task's instances under their scopes with the parameters they differ in,
  executions and artifacts.
- **New: the deployments page** — every generation per app, current marked.
- **Removed:** the task explorer and search, claim triage, bulk cancel and
  the concurrency-limits admin page.

## [Unreleased]

### SDK

- **Cooperative cancellation: a worker asks whether it is still wanted.**
  Nothing reaches into a running container. A cancel marks the build (or
  the task) and releases its claims; the container finds out by asking,
  at checkpoints where stopping is safe, and exits cleanly — no output
  written, no completion reported.

  Two checkpoints are automatic: the **start of each attempt**, before
  `run()`, which catches a cancel that landed while the container was
  still queued; and **each dynamic-dependency yield**, so a stopped build
  does not pay for another layer of the DAG. For a long `run()` body,
  `stardag.cancellation_requested()` is the opt-in for a point only the
  task's author can place, and `stardag.ExecutionCancelled` is what to
  raise there. Both are new public API.

  A background poll raising into arbitrary user code was considered and
  rejected: it can interrupt a write halfway, which is the one thing
  content-addressed targets exist to prevent.

  **A worker exits only on positive evidence that it is no longer
  wanted.** A transport failure, an unreachable registry, a server
  predating the endpoint and a registry with no opinion all keep it
  running. The polarity is derived rather than defaulted: stopping a
  healthy worker destroys work, while letting a superseded one finish
  writes a content-addressed output nobody reads.

  Throttled, 30s by default, `STARDAG_CANCELLATION_CHECK_INTERVAL_SECONDS`.
  The cost is **one registry read per task attempt** plus one per
  dynamic-dependency yield, against a task attempt that already makes a
  start report, an artifact upload, a completion and a wake. The start
  report answers half the question for free — a start naming a superseded
  execution is refused — but a successful one says nothing about whether
  the build is still running, so the checkpoint asks.

  **Side-effecting tasks are the one real loss, and were never inside the
  promise.** A task that has already written to somebody else's database
  has done so; cancellation ends the execution, not what it did outside
  its target.

- **The worker carries its execution identity.** The id minted at the
  claim now reaches the container (`STARDAG_EXECUTION_ID`) and is echoed
  on the worker's own start and on its interruption and preemption
  reports, so the registry can tell them from a superseded execution's.

  **Breaking for custom executors and registries.**
  `TaskExecutorABC.submit_detached` gains a keyword-only `execution_id`,
  and `RegistryABC`'s `task_start(_aio)`, `task_interrupt(_aio)` and
  `task_preempt(_aio)` gain an optional one (as `task_start_claim(_aio)`
  did earlier in this batch). The defaults make these safe for _callers_,
  not for _overrides_: Python dispatches to the override and the engines
  pass the keyword unconditionally, so an implementation still declaring
  the old signature raises `TypeError`. Add the parameter.

  Not softened with a signature check that drops the keyword for an
  override that cannot take it — that would hand such an implementation a
  worker unable to name its own execution, with the protections and
  cooperative cancellation silently absent. A `TypeError` at the seam is
  the better answer.

  **The non-detached `submit` path is explicitly opted out**, not
  overlooked. Adding the parameter to `submit` — the one method nearly
  every custom executor overrides — would break all of them, for a path
  that already matches its reports on the executor reference and so
  degrades to nothing worse than the behaviour that predates identities.
  The refusal below needs an identity on _both_ sides, so that path is
  never wrongly refused either. It is not excluded from cancellation: a
  worker with no identity can still be told its build or its task has
  stopped.

- **Known limitation (STA-99): a build that takes over a lapsed claim and
  re-attaches to the still-live container may see that container stop
  itself.** Taking over the claim records this build's execution identity
  on the task, so the re-attached container is no longer the execution
  the task names, and its own checkpoint stops it.

  Four conditions must coincide: the task is RUNNING elsewhere with a
  recorded executor reference when this build registers, that holder's
  claim has lapsed, this build's claim is granted, and the old container
  is still alive. And the container only stops at its **next** checkpoint
  — a `run()` with no dynamic dependencies and no
  `cancellation_requested()` call has none left, so it completes normally
  and the re-attach still works.

  Symptom when it does happen: **one retried attempt and one wasted
  container**, under the ordinary attempt budget. Correctness is
  unaffected — output is content-addressed. The fix is a decision about
  whether re-attach or supersession wins on a granted claim, which is
  being designed with STA-94 rather than patched.

- **Only an execution this submission spawned is ever stopped.** A handle
  can be borrowed rather than created — the claim loser re-attaches to the
  winner's execution, and a resumed build adopts a reference an earlier run
  recorded. Cancelling one of those when the registry refuses a start would
  kill a live execution belonging to somebody else, which is the damage the
  refusal exists to prevent, done in its name.

- **Losing a task is never reported as that task's failure.** Two paths
  reach the same damage and both are closed. When the resident engine's
  post-spawn start is refused, the refusal used to travel the generic
  error path and post `TASK_FAILED` — against a task another build is now
  running, or one that was just cancelled — and a failure report writes
  through, so losing a race would have ended with this build marking
  somebody else's live execution failed. And a worker that stopped itself
  at a cooperative checkpoint raises out of its container, which a
  detached executor reports as a task failure, reaching the same place.

  Both are now counted as a **local** failure on the path the claim-loser
  timeout already uses: `fail_mode` is honoured and the build still fails,
  but nothing is said to the registry about a task that is not its own.

- The reactive tick handles the post-spawn start's `execution_superseded`
  409 instead of letting it escape. That error arrives inside a
  `TaskGroup`, where it would cancel every sibling spawn in the pass and
  kill the tick, leaving those siblings claimed and never spawned. The
  container it belongs to is stopped there, while the handle is still
  held: its reference was never recorded, so nothing else could find it.

- **The scheduler no longer stops containers.** The reactive tick's cancel
  drain is gone: a terminal build's tick no longer lists the executions the
  build started and cancels them at their backend. Cancellation is
  cooperative — a worker asks at its own checkpoints whether it is still
  wanted and exits cleanly when it is not — and a hard stop is
  `stardag builds stop`, which lists the build's executions while its claims
  are still held, ends those calls, and cancels the build last.

  This is the removal half of STA-78: all three production incidents in this
  area came from a short-lived scheduler reasoning about containers other
  processes had started, and each fix opened a hole beside it.

  **Breaking for custom registries.** `RegistryABC.build_get_executions` and
  `build_get_executions_aio` are removed, with the `BuildExecution` and
  `BuildExecutions` models exported from `stardag.registry`; an
  implementation that overrode them can delete the override. `task_cancel_aio`
  no longer accepts `if_executor` / `if_executor_ref` — an override still
  declaring them keeps working, since the engines no longer pass them, but
  the narrowing they applied is gone. See
  [RELEASE_NOTES.md](RELEASE_NOTES.md).

- `TickSummary.cancelled_refs` **stays, and now means something narrower.**
  It counted executions the drain stopped from a recorded reference; it now
  counts only the containers a tick spawned _itself_ and stopped while it
  still held the handle, because the task stopped being this build's while
  the spawn was in flight. That is the one stop a scheduler can make
  honestly, and the only one left.

### Registry API

- **A non-claiming start naming a superseded execution is refused** (409,
  `execution_superseded`). A worker's own start used to be folded in
  unconditionally — new status, new owner, new executor fields, a fresh
  claim — with no check on who held the claim, so a restart arriving
  after its claim had lapsed and been taken over could evict the live
  holder. Two executions of one task, which is the one outcome claims
  exist to prevent.

  Three conditions, all needed: a **live claim** (a task past its expiry
  is up for grabs and taking it over is the ordinary self-heal path),
  an identity on **both sides** (absence is no opinion, so a rolling
  deploy is unaffected), and the two **differing** — a Modal preemption
  restarts under the same call id and re-sends the same identity, so a
  legitimate restart is accepted. The transaction rolls back, so nothing
  is recorded and no attempt is spent.

  Build ownership is deliberately **not** part of the test. On an id
  match the recorded owner cannot separate an impostor from the genuine
  holder, so it would refuse both — and only the genuine case is
  reachable through the SDK.

- **A non-claiming start that would revive a cancelled task is refused**
  (409, `task_cancelled`). Cancelling a task releases its claim — which
  is the point, it is what lets the next build have it — but it also
  leaves no live claim for the supersession rule to protect, and the row
  still names the cancelled execution. So a container that was queued
  when the cancel landed would start, be accepted, and the fold would
  turn CANCELLED back into RUNNING under the very execution that was
  cancelled; its own checkpoint would then read a task running under
  itself and let it carry on.

  Reviving such a task is a _claim's_ job, after a reset, never a
  report's. **Scoped to starts that carry an execution identity**, and
  only those, which is the line every other rule here draws: absence of
  an identity is no opinion. That leaves the concurrency limiter's
  slot-occupying start untouched, and with it the sequential engine, the
  Prefect integration and a `claim=False` build — all of which post a
  start carrying no identity and must keep working. The limit it accepts
  is that such a start can still revive a cancelled task, which is how
  every release before this behaved.

- **`GET /builds/{build_id}/tasks/{task_id}/execution-status`**: read-only,
  two denormalised columns, no lock and no event. Answers a running
  worker's one question — still current, or `build_not_running`,
  `task_cancelled` or `superseded`. `execution_id` is optional; without
  one the build and task halves are still evaluated, which is what keeps
  a worker that was never given an identity covered for the cases a human
  causes.

- The two per-build status replays mirror the row fold's
  **claim-redelivery** guard: a re-delivered claim carries the id the task
  already holds and no executor of its own, and must not erase the
  reference the spawn recorded. A replay that cleared it where the row
  preserves it would apply a later report naming a stale reference that
  the row refuses — one task INTERRUPTED in the UI and RUNNING in the
  frontier.

- The interruption and preemption reports accept an `execution_id`,
  preferred over `executor_ref` where both are sent because the identity
  covers the whole life of an execution where the reference only covers
  the part after the spawn. The two per-build status replays compare it
  alongside the row's fold, so all three readers give one answer.

- `latest_execution_id` is surfaced on the task read models
  (`GET /tasks`, `GET /tasks/{task_id}`).

- **A build going terminal releases the claims it holds — cancel and fail
  alike.** `POST /builds/{id}/fail` now releases them, and
  `POST /builds/{id}/cancel` releases them unconditionally rather than only
  when asked. One implementation serves both, and the reaper; what is
  released, what is written and what is never touched are stated once, at
  `services.build_cleanup.cascade_cancel_build_tasks`.

  Neither route reached it on its own before. A failure wrote a single
  BUILD_FAILED event; a cancel released only when passed `cascade=true`.
  What made the rule true in practice, for reactive builds, was the SDK's
  cancel drain writing TASK_CANCELLED per execution as a side effect of
  stopping containers. Deleting the drain took the release with it — the
  consumer nobody had listed — and exposed a behaviour nobody had chosen:
  a terminal build holding its tasks' claims, and their concurrency-limit
  slots, until they expired.

  The cancel route's `cascade` parameter is therefore **redundant, and
  accepted as a no-op** so existing callers keep working; a later cleanup
  removes it.

  **`POST /builds/bulk-cancel` still honours its own `cascade`**, which
  defaults to true. So the word now means two things: on the single-build
  route it is ignored and the claims always go, while on the bulk route
  `cascade: false` still means "record the events and release nothing".
  The reaper has the same switch, `ReaperSettings.cascade`
  (`STARDAG_API_REAPER_CASCADE`, default true). Both are left that way
  deliberately — they are operator-facing controls, and changing them is a
  separate decision from this one — so do not carry "cascade is a no-op"
  from the single-build route to either. **Removing both, so a terminal
  build always releases, is tracked as STA-103.**

  **The window this opens, stated honestly.** A release lets the next
  build take the task over within seconds, while the old container is
  still writing. Both write the same bytes, since output is
  content-addressed; the old worker exits at its next cooperative
  checkpoint on `build_not_running`; and one whose `run()` has no
  checkpoint runs to completion harmlessly. This is still not the way to
  stop a live build — `stardag builds cancel` keeps its warning, and
  `stardag builds stop` remains the command for one that is still running
  something.

  **Known limitation (STA-100): a stale claiming start can revive a task
  this cancel just released.** Cancelling releases the claim, so the
  `task_already_running` refusal no longer applies, and a claiming start
  — a scheduler tick of the cancelled build that was already in flight,
  or an idempotent retry of the claim that was cancelled — is granted and
  folds the task back to RUNNING.

  Pre-existing, and reachable before this release through
  `cascade=true`; making a plain cancel release widens it to the default
  path. Two conditions must coincide: a claiming start arriving after the
  cancel, from a build that is already terminal.

  Symptom when it does: **one wasted container, and the claim held until
  that container's next checkpoint.** The revived worker asks before
  `run()`, is told `build_not_running` — the endpoint reads build status
  before task status — and exits without writing output or reporting a
  completion. Never a wrong result. The fix needs a third 409 code on the
  claiming start and an audit of every caller that reads one, which is
  why it is its own issue.

- **`GET /builds/{id}/executions` is removed**, with the event-log
  reconstruction behind it — two window functions, the keyset cursor, and
  the "which execution did this build start" lookup. Nothing needs to
  reconstruct that: a worker knows its own identity, and `builds stop` reads
  the task row while the claims make it exact.

- **The per-task cancel refuses `if_executor` / `if_executor_ref`** with
  400 `conditional_cancel_removed`. Their only caller was the drain, and
  the release is server-first, so an SDK old enough to still run one will
  meet this server: it gets a 404 from the deleted executions route, falls
  back to the frontier, and sends these conditions with a cancel it
  believes is narrowed. Ignoring them would silently widen it, and the
  case they excluded is real — a successor that reset the task to PENDING
  in the window is cancellable by anybody, so the old drain would stamp
  its freshly scheduled work. Failing costs that caller nothing: a
  terminal build's claims are released by the transition itself now, so
  its cancel had nothing left to do.

- **A failed build completes the blocked closure itself**, in the same
  transaction that releases its claims, so the descendants of what it just
  released are SKIPPED rather than dangling PENDING. The scheduler still
  asks, and that call is now a no-op.

  **`POST /builds/{id}/fail` therefore reports what it skipped**, in a new
  `skipped_task_ids` field on its response, and `RegistryABC.build_fail`
  returns it (`BuildFailResult | None`, the same optional-return
  convention `build_cancel` uses — an override returning `None` is
  unaffected). Without it the count had nowhere to come from: the
  follow-up `skip-blocked` call correctly answers empty, so a scheduler
  counting only that answer reported zero skips on the tick that skipped
  everything, which is the number its trail and the UI show.

  It has to be here rather than left to the caller, because _when_ the
  caller asks differs by version: every SDK up to v0.25.0 skips before it
  fails, since its cancel drain used to cancel the running branch first
  and make it a seed of the closure. Against this server that drain is
  refused, so such a tick would compute the closure while the branch is
  still RUNNING — which blocks nothing — and no later tick would retry it,
  the build being terminal already.

  A cancel deliberately does **not** do this: it is a revocation, not a
  verdict, and a neighbour may reset the task and run it.

- **A cancelled build is no longer flagged for a scheduler tick.** That
  flag existed to run the drain again; a terminal build's tick would now
  read a terminal frontier and return. The builds a cancel genuinely wakes
  are the neighbours whose gating upstreams it released, and each released
  task flags them on its own transition.

### UI

- The tick-summary trail's "executions cancelled" counter stays, and its
  help text follows the counter's narrower meaning: executions this tick
  spawned and then stopped, because the task stopped being this build's
  while the spawn was in flight. It no longer counts drained revocations,
  because there are none.

## [0.25.0] — 2026-09-22

**SDK-only release.** The `### Registry API` and `### UI` entries below are
merged but not yet deployed — they ship with the next server image, and the
hosted service and self-hosters get them then. Nothing in the SDK waits on
that: the claim identity rides as a query parameter an older server ignores,
so `0.25.0` runs unchanged against `server-v0.4.0`, and the idempotent claim
starts working for existing installs the moment the registry is upgraded,
with no SDK action.

### SDK

- **The pickle-based build task store is retired.** A task is rebuilt from
  the registry's identity-level `task_data` and the deployment's importable
  code, always. A pickle carried the `dependencies_only` /
  `execution_only` values the _writing_ code resolved, which is state no
  rollover could refresh — the reason 0.24.0 had to refuse a rollover for
  any deployment that might have stored one.

  Consequences: `task_modules` is required for reactive builds (the
  inferred default now counts as the declaration);
  `build_trigger(reactive=True)` on an app with none raises before a build
  is minted; the reactive bootstrap **refuses** a build whose incomplete
  tasks it could not rebuild, naming each one; rollover's only remaining
  precondition is the recorded deployment; and no reactive build needs
  target-root write access at plan time.

  API: `BuildTaskStore` removed from `stardag.build`, `run_tick_aio` no
  longer takes `task_store`, `plan_pickle_elision` / `PickleElisionPlan`
  renamed to `plan_rehydration` / `RehydrationPlan`, and
  `StardagApp(require_pickle_free=...)` is a deprecated no-op. See
  [RELEASE_NOTES.md](RELEASE_NOTES.md) for the in-flight-build upgrade
  rule.

- Fixed: the pre-flight's round-trip dry run used `model_dump(mode="json")`
  while registration has stored the **registry-mode** dump since 0.24.0, so
  it checked a payload the registry does not hold. Harmless while it only
  drove a warning; not harmless now that it decides whether a build is
  armed.

- **The worker classifies its own execution; a tick's probe waits for it.**
  A probe answers one question — is this execution still running on the
  backend? — and "no" is not a classification. The platform ending an input
  is an _interruption_ when the task caught it and checkpointed (resumed on
  `max_interruptions`, no attempt spent) and a _failure_ when it did not
  (retried on `max_attempts`). Only the dying worker knows which, and it
  reports from the grace window the platform gives it — which is exactly
  the window a probe can land inside.

  So whoever looked first decided, and under load that was the tick: a
  cancelled input was recorded as a failure, spent an attempt the task
  never asked to spend, and the worker's own report was then refused as a
  statement about an execution the task no longer held. The build still
  completed, by the retry route rather than the resumption, so only the
  accounting said what had happened.

  A probe that finds an execution gone now opens a **report window** of
  `TickConfig.worker_report_grace_seconds` (default 30) instead of acting,
  and records the failure only if the window closes with nothing reported —
  the silent-death case the probe exists for. The window closes early the
  moment the task stops being `RUNNING` under that ref, which is what the
  worker's report does, and is skipped only where no report can be coming:
  a `RUNNING` task with no executor ref, whose whole claim has already been
  waited out. Every tick honours it, including the one-pass tick a watchdog
  sweep spawns — the window lives in the tick's memory, so a tick that
  exits instead of waiting is a tick that classifies synchronously.

  The wait never outlives its own container: it is trimmed at startup to
  what `tick_timeout_seconds` can honour, less a reserve for the tick's
  exit, so a window always closes inside the tick that opened it. Two new
  `TickSummary` counters make it legible — `executions_awaiting_report`,
  and `report_window_expired` for the windows that closed unanswered.

- **`stardag builds stop`: stop a build's executions, then cancel it.** A
  cancel releases the claims the build's tasks hold, and from that instant
  another build may take a task over — so the task row names a successor's
  execution while the old container is still running, and every query about
  the present gives the wrong answer. Doing it the other way round is what
  the new command is: it lists the executions the build holds _while the
  claims still make that list exact_ (straight off the task row — no event
  walk, no ranking), cancels the selected Modal calls, and cancels the
  build last.

  ```sh
  stardag builds stop <build-id> --dry-run     # the list, and nothing else
  stardag builds stop <build-id> --worker gpu  # stop one worker's calls
  ```

  Filters — `--worker`, `--executor`, `--namespace` (a prefix),
  `--older-than`, repeatable `--task-id` — narrow what is stopped;
  `--dry-run` prints and exits, and `--json` emits the selection. An
  execution a filter excludes keeps running after the build is cancelled,
  and the prompt says how many. A hard kill is the Modal dashboard's,
  which the registry UI deep-links to.

  Nothing the build holds is dropped from the list, and there are three
  reasons a listed execution may not be stoppable. An execution on a
  non-Modal executor — permanent; stardag reaches Modal and nothing else,
  and the registry reaches no backend at all. A task claimed but whose
  spawn has not reported a call id yet — momentary, since RUNNING _is_ the
  claim and the claim is recorded first, so re-running once the container
  is up will catch it. And a row naming no executor at all, which is
  genuinely ambiguous and says so: a non-detached execution writes it, and
  so does a Modal claim whose best-effort executor metadata came back
  empty, so the reason names both and points at the one action that tells
  them apart rather than guessing.

  All three carry their reason into the table, the closing summary and
  `not_stoppable_reason` in `--json`. The list is exact about which
  executions are the build's, not about which of them can be stopped.

- **Breaking: `stardag builds cancel --cascade` is removed.** It released
  the build's claims and left its containers running, which is the ordering
  above, inverted. The flag now exits with the `builds stop` command line
  for the same build rather than silently changing meaning. Plain
  `builds cancel` is unchanged and still records an event and nothing else
  — the command for a build already believed dead.

- **Breaking: `TickConfig` and `TickSummary` are keyword-only.** Both are
  now `@dataclass(kw_only=True)`. Their fields are grouped by meaning — the
  two budgets together, the fan-out throttles together — so a new knob is
  inserted beside its relatives, and that is only safe if position carries
  no meaning: inserting a field ahead of existing ones would otherwise
  re-bind a positional caller's arguments silently, turning a grace period
  into a concurrency bound. A positional call is now a `TypeError` instead.
  See [RELEASE_NOTES.md](RELEASE_NOTES.md) for the migration.

- **A `significance=` field on a nested config model now survives a
  build.** A `StardagBaseModel` that is not a task could already declare
  `dependencies_only` / `execution_only` fields: init refused them, the
  error named a build-config key, and that key resolved at validation. But
  hashing the build's structure scope resolved every key through the
  **task** registry, so the same key raised `UnknownTaskClassError` — the
  feature worked under `build_config_scope` in a test and failed in
  `sd.build` and on every deployed path. Guard-rail caps and worker counts
  live on a nested config object as often as on the task holding it, and
  those fields could not migrate off the deprecated `hash_exclude=True`.

  A model that declares such a field is now indexed as it is defined, and
  the scope hash falls back to that index when a key is not a task class.
  Its key is the same shape as a task's: `__namespace__` and class name, or
  the bare class name when it has none. Everything downstream is unchanged
  — an `execution_only` override is validated and excluded from the hash, a
  `dependencies_only` one is hashed under the model's key.

  Two models resolving to one key is now an error where the second is
  defined, naming both: a config entry could not have said which it meant.
  Set `__namespace__` on one of them. Only models that declare a
  build-config field are indexed, so a class no config can name cannot
  collide; a legacy `hash_exclude=True` field does not count, since it is
  still passable at init.

- **A claim can say which attempt it is.** Both engines claim a task
  _before_ spawning it — the claim and any concurrency-limit slots are
  acquired in one transaction, so a denied task never occupies a worker
  — which means there is no executor reference at claim time and never
  was. Without one, a retried claiming start could not be told from a
  genuine second attempt of the same build.

  That mattered because the registry client retries a POST whose
  response was lost. The repeat was refused by the state its own first
  attempt created, and a refusal is a correct reason for a worker to
  stand down — so it did, while itself holding the claim, and the task
  then sat claimed and not running until the claim expired.

  `task_start_claim_aio` takes an optional `execution_id`: mint one
  before claiming and re-send the same value if the request is retried.
  **Both engines do this for you.** Sending none is fully supported and
  behaves exactly as before.

  The resident engine is the more affected of the two, not the lesser:
  its claim sits inside a wait-and-retry loop that polls until another
  build's claim frees up, so a lost response is followed by another
  attempt _by construction_, where the reactive engine claims once per
  tick pass and needs the HTTP client's own retry to reach the same
  failure. Its identity is minted once per `acquire_claim` call and
  re-sent on every iteration — one logical attempt, one identity.

### Registry API

- **`tasks.latest_execution_id`**: the identity of the claim a task is
  held under, as minted by the caller. One nullable column, no index,
  no backfill.

  A claiming start repeating the id the task already holds is the same
  attempt asking again and is granted; a different id from the same
  build while the claim is live is denied, as any second attempt is.
  With no id sent, the `(executor, executor_ref)` pair decides and a
  request naming neither is denied — unchanged.

  The identity is set by a start that names one and **left alone** by
  one that does not. That is deliberate rather than tidy: the tick
  records a second, ref-bearing start as soon as the spawn returns and
  that start names no identity, so clearing on it would drop the id
  moments after the claim recorded it and a slightly late retry would
  be refused — the failure this closes. `TASK_RETRIED` is the reset.

  Absence is never a mismatch, so both directions of a rolling deploy
  are safe and no `minimum_version` bump is needed. `execution_id` is
  echoed on every task-event response and named on an
  `already_running` denial. See `docs/design/executions-as-records.md`,
  which also records the `executions` table this replaces and why it is
  not being built.

### UI

- **A "Stop running tasks" panel on the build page**, showing the same
  list `stardag builds stop` acts on — with the same filters, and the
  exact command to copy, carrying whatever the filters were set to. Rows
  can be ticked individually, which the command carries as `--task-id`. It
  never stops anything itself: the server cannot reach the execution
  backend and deliberately never will, so the credentials that can stop a
  container are the operator's. Each call links straight to its Modal
  dashboard page for a hard kill. The panel is absent unless the build
  holds live executions — except where the claim-holder scan gave up
  early, which it reports rather than passing off as "nothing running".

  The panel's selection rules mirror the command's exactly and moved with
  them: a row with no call id is listed, its Call cell reads "not recorded
  yet", and the panel gives the same reason the command would. Executions
  on another executor are called out separately from those with no call id
  on their row — and that second group carries the same hedge the CLI
  does, because an unattributed row may be a non-detached execution or a
  claim whose spawn has not reported yet, so the guidance is to refresh and
  see rather than to wait or to give up. The UI ships in the server image
  rather than the SDK tag, so this half arrives with the next server
  release.

- The build page's **"Cancel & Release Claims"** action is gone, for the
  reason the `--cascade` flag is: it released the claims first and stopped
  nothing. Plain **Cancel** is unchanged.

## [0.24.0] — 2026-09-20

### SDK

- **An interruption is classified by the exception it was raised from, not
  by a stopwatch.** A detached task hit its 86400s function timeout,
  measured 86392.0s elapsed, and `elapsed >= timeout - 5.0` called it a
  preemption — so the worker reported nothing and re-raised, expecting the
  backend to restart the input. Modal does not restart a timed-out input,
  and the task sat `RUNNING` in the registry for over a day.

  The clock cannot answer that question: `time.monotonic()` starts inside
  the runner, after container boot, image load and input deserialisation —
  all inside the backend's window and outside ours — so elapsed
  systematically under-reads, by however long the container took to start.
  No fixed tolerance bounds that.

  But the question never needed a clock. What the worker needs to know is
  whether the backend will restart this input, and only a preemption will —
  which is the one case with a distinct exception type. A function timeout
  and an explicit `FunctionCall.cancel()` are indistinguishable from each
  other and both mean "nothing is coming", so both report. That harder
  distinction was the only thing the timing was there for, and it turns out
  never to have been needed: whether a report _applies_ is the registry's
  to decide, since it issued any cancel.

  The signal is read off `__cause__`/`__context__` of whatever the task
  raises, so the documented recipe keeps working unchanged. Raising
  `from None` clears `__cause__` and suppresses the traceback preamble; it
  does not clear the context. The elapsed-time rule remains as a fallback for a task that
  raises `ResumableInterruption` on its own initiative, where there is
  nothing on the chain to read.

- **A preemption is now recorded** (`TASK_PREEMPTED`), without changing the
  task's status and without releasing its claim — the backend is restarting
  the same call id and will need it. Previously a preempted worker reported
  nothing at all, on the reasoning that the restart makes the report
  unnecessary; that holds right up until the restart does not come, at
  which point the task is indistinguishable from one running happily.
  Recording it costs neither an attempt nor an interruption budget, and
  shortens the claim's expiry to a restart-sized grace, so an unfulfilled
  restart becomes an ordinary lapsed claim in minutes rather than after the
  worker's whole declared timeout.
- Docs: the watchdog section now says plainly that a deployment running
  long detached tasks wants `watchdog_period_minutes` set. Every other
  wake-up rides on a write; a claim expiring is not one, and Modal has no
  way to schedule a single wake-up for the moment it does (a schedule is
  `Cron` or `Period`, fixed at deploy time, and `spawn()` takes no start
  time).

- **Dependency edges are scoped to the code and config that evaluated
  them, not to the task id.** A build's readiness is evaluated over the
  edges in its own _structure scope_ — the deployment's (or local
  process's) code id plus a hash of its `dependencies_only` config — so a
  changed `requires()` or a changed fan-out needs no version bump. A build
  has one `build_config` for its life: a resume or re-trigger with a
  different `dependencies_only` config is refused
  (`BuildConfigMismatchError`); start a new build. Builds under the same
  code and config share what they discovered, so a fan-out's pre-yield
  section runs once per scope. Design record:
  `docs/design/scope-keyed-dependency-structure.md`.
- **A running build follows the live deployment.** After a redeploy, the
  first tick on the new code re-plans the build — discovery again under
  its own code with the stored config, edges under its own scope, the
  build's scope moved — and reports `rolled_over` in its summary; a tick
  still lingering on the old code exits `superseded`. Workers register the
  dynamic dependencies they yield under their own code's scope, so an old
  container's late yield never reaches a re-planned build. A rollover moves
  forward only (the tick re-plans only if the registry's current deployment
  of its app is its own code) and requires a pickle-free task store
  (`task_modules` or `require_pickle_free=True`). A root whose identity
  parameters changed, or a deployment that may store pickles, cannot be
  re-planned: the build fails with `rollover_failed` — re-trigger it as a
  new build. `stardag modal deploy` exits non-zero when it cannot record
  the deployment, since no build rolls over to unrecorded code.
- **Three levels of parameter significance.**
  `sd.StardagField(significance="identity" | "dependencies_only" |
"execution_only")`. Levels 2 and 3 are read only from the **build
  config** (`build_config=` on `sd.build`, `sd.build_aio`,
  `sd.build_sequential` and `StardagApp.build_trigger`;
  `sd.build_config_scope(...)` for tests) and **can no longer be passed at
  init** — that is what keeps one task id to one structure within a build.
  The registry stores only identity-level `task_data`, so a task rebuilt
  from registry data and one unpickled from the build store agree by
  construction (closes the "frozen at first registration" inconsistency).
- **`hash_exclude=True` is deprecated** in favour of
  `significance="execution_only"`. It keeps working for one release with a
  `DeprecationWarning`; the difference is that the old option allowed the
  value at init and the new levels do not.
- **A cancelled or skipped task in a build's plan is reset and run** (within
  the attempt budget) by the ordinary frontier pass, as soon as every
  upstream in the build's scope is complete. It used to be reached only
  through the external-blocker diagnostic, once the build had already
  stalled. `FAILED` is unchanged: a result, owned by `fail_mode`.
- **Deployments are recorded.** `stardag modal deploy` records
  `(app_name, code_id)` in the registry (best-effort), and
  `stardag modal deployments [--app NAME]` lists an environment's
  deployments newest first — one live deployment per app, exactly as on
  Modal. A branch that should run beside production is another app name.
- Registration sends the dependency sets discovery actually computed, and
  declares nothing for a task it pruned at, instead of re-evaluating
  `requires()` for every task in the payload.
- **Breaking for `RegistryABC` implementers outside this repo:**
  `task_register`, `task_register_aio`, `task_register_bulk` and
  `task_register_bulk_aio` take a keyword-only `declared_dependencies`;
  `build_start(_aio)` and `build_resume(_aio)` take keyword-only
  `scope_key` / `build_config`, and the registration methods a keyword-only
  `scope_key`; new `build_set_scope(_aio)`, `deployment_record`,
  `deployment_list` (no-op defaults). `BuildInfo` gains `scope_key` and
  `build_config`.
  The frontier's `blocked_by_external` is no longer read.
- **The SDK refuses a Registry API that predates structure scopes** with
  `RegistryTooOldError`: a missing `PUT /builds/{id}/scope`, or a
  `POST /builds` / `POST /builds/{id}/resume` that does not echo the
  `scope_key` it was sent. Upgrade the server first; a newer server with an
  older SDK is supported, the reverse is not.
- `build_trigger(build_config=...)` validates the config locally before a
  build is minted (`BuildConfigError` for a misspelled field, an identity
  field or an invalid value); a class the trigger process has not imported
  (`UnknownTaskClassError`) is left to the bootstrap to judge.
- The resident Modal builder forwards the build's config and structure
  scope to the workers it spawns, as the reactive path already did.

- Default prebuilt server image bumped to `0.4.0`
  (`DEFAULT_SERVER_VERSION = "0.4.0"`, from the `server-v0.4.0` release) —
  the server version this SDK release is tested against, and the one that
  carries every Registry API change below. A newer SDK against an older
  self-hosted API is refused (`RegistryTooOldError`) rather than degraded;
  self-hosters upgrade the server first (`stardag self-host upgrade`), then
  the SDK.

- **Breaking, for anyone implementing `RegistryABC` outside this repo:**
  `task_cancel_aio` takes keyword-only `if_executor` and `if_executor_ref`, and
  `build_get_executions` / `build_get_executions_aio` take a keyword-only
  `cursor`. Subclasses that override the old signatures raise `TypeError`
  when the reactive tick calls them, and **the two degrade differently**:
  a stale `task_cancel_aio` is caught per task, so the symptom is a cancel
  that is never recorded rather than a crash, while a stale
  `build_get_executions` fails the tick — that call site catches only a
  missing route, on purpose, because a cascaded build's frontier shows
  nothing to stop and degrading quietly would report "nothing to do" and
  leave the containers running with no second chance. **Update an external
  execution-list implementation before running a tick against it.**

- **A cancelled or failing build no longer stops other builds' executions.**
  The tick's cancel pass read the frontier's `running` list, which is every
  RUNNING task in the build's _plan_ — and after plan closure that includes
  tasks another build has claimed and is executing. A cancelled build
  cancelled them: killing live containers, releasing claims it never held,
  and doing it again on every tick a neighbour's status write earned it. It
  now asks the registry which executions it started and has not seen end
  (`GET /builds/{id}/executions`, `RegistryABC.build_get_executions`) and
  stops only those. Against a server predating the route it falls back to
  the frontier filtered on the new `latest_status_build_id`; against one
  predating that field too, it behaves exactly as before.
- **`stardag builds cancel --cascade` now actually stops the executions it
  releases.** The cascade writes TASK_CANCELLED for the claims the build
  held — which is what lets the next build take those tasks over — but a
  cascaded task is CANCELLED and therefore in neither `running` nor
  `actionable`, so the one caller of `cancel_detached` could not see it.
  The claim was released and the container kept running, and the next
  claimant started a second execution of the same task. The executions
  route reports them, so the one tick a cancel asks for now stops them.
- **A tick that resets a blocked upstream now runs it, instead of lingering
  for a wake-up it never sent.** Resetting a cancelled blocker is the one
  thing terminal handling does that changes the frontier, and the pass
  treated it as no action at all: it fell through to the linger poll, which
  waits on the registry's wake-up flag — and that flag deliberately skips
  the build whose own event caused the change, since it is the one that
  already knows. So the tick waited for news it had already heard, exited on
  its deadline, and left the build with nothing running, nothing scheduled
  and no flag to be handed out on, until the watchdog. Reachable whenever a
  shared task is genuinely left cancelled, which is exactly what the cancel
  fixes above make the common outcome.
- **A worker no longer spawns a tick for a build that cannot use one.** A
  cancelled build's workers keep running until a tick stops them, and each
  one re-flagged the build on its way out, so every drain in the
  environment handed it out again. `POST /builds/{id}/notify` now answers
  `needs_tick` truthfully and the worker skips the spawn when it is false.
  The one tick a cancel wants is unaffected: the cancel sets that flag
  itself, and it survives until a tick drains it.

### Registry API

- **Dependency edges carry a `scope_key`** (`task_dependencies.scope_key`,
  `builds.scope_key`, `builds.build_config`), unique on
  `(scope_key, upstream, downstream)`; gating, plan closure and skip-blocked
  read the build's current scope only. New `PUT /builds/{id}/scope` sets
  or **moves** the build's scope (a re-plan under new code); a
  `build_config` that differs from the stored one is a 409
  `scope_mismatch`. `POST /builds/{id}/resume` and `POST /builds` accept
  `scope_key` / `build_config` under the same rules. Registration endpoints
  accept an optional `scope_key` so a worker's yields are recorded under the
  code that evaluated them, defaulting to the build's current scope.
  A build that never sets a scope runs under a synthetic per-build one, so
  older SDKs keep working with per-build edges.
- **Phantom placeholder rows are gone.** Every declared upstream must be
  registered; an unknown id is a 400 `unknown_upstream_task_ids` on both
  registration endpoints and on `/dependencies`. The one tolerance: unknown
  upstreams of a task whose recorded status is COMPLETED are dropped, for
  SDKs that re-derive `requires()` for pruned tasks. The migration deletes
  existing phantom rows and their edges.
- `TaskCreate.dependency_task_ids` is `list[str] | None`: a list declares,
  `null` declares nothing.
- **Plan closure re-runs when a build stalls**, so an edge a scope-mate's
  worker wrote after registration is picked up. `blocked_by_external` is
  therefore always empty (kept on the wire for one release).
- **CANCELLED and SKIPPED tasks are actionable** when gated open.
- **The graph reads by provenance.** The environment-wide graph shows each
  node's edges from the scope of the build that produced its current
  status; a build's graph adds its own scope. Edges carry `scope_key` and
  `is_cross_scope`; nodes carry `scope_key`.
- New `deployments` table and routes: `POST /deployments`
  (`{app_name, code_id}`, idempotent), `GET /deployments?app_name=` newest
  first — one row per deployed code version of an app.
- **Migration** `690e61e0c920`: adds the columns and table, backfills every
  build's synthetic scope, deletes phantoms, and **copies legacy edges into
  the scope of every RUNNING build** so a reactive build in flight across
  the deploy keeps its gates.
- **`TASK_INTERRUPTED` applies only while the task is `RUNNING` under the
  reporting build.** A worker cannot tell a deliberate cancel from a
  function timeout, so it reports either way and the registry decides — it
  issued any cancel, and it knows whose claim the task is under. Without
  this, an interruption reported after a cancel would flip the task back to
  `INTERRUPTED`, which the frontier lists as actionable, and a tick would
  start a task the build had just cancelled. It also stops a worker whose
  claim lapsed and was re-taken from evicting the live holder. The event
  row is written either way; only the status transition is refused.
- **`POST /builds/{id}/tasks/{id}/preempt`** and `tasks.latest_preempted_at`
  (nullable, no backfill) for the SDK change above. "A restart is
  outstanding" is derived rather than stored: RUNNING, with
  `latest_preempted_at` later than `latest_status_at`. So the restarted
  execution's own start falsifies it, with nothing to clear.
  `ClaimSettings.preempt_restart_grace_seconds` (default 900, matching the
  SDK's own claim-TTL grace) sizes the shortened claim — long enough that a
  restart the backend has merely queued cannot be mistaken for one that is
  never coming.
- `GET /tasks` and `GET /tasks/{id}` now carry `latest_status_expires_at`
  and `latest_preempted_at` alongside the other claim fields.

- `POST /builds/{build}/tasks/{task}/cancel` refuses with 409
  `not_claim_holder` when the task is RUNNING, SUSPENDED or INTERRUPTED
  under a different build. Authority to revoke is build-scoped — the
  cascade already enforced it, and `stardag tasks cancel` has documented it
  since it shipped ("pass the build from `latest_status_build_id`"). PENDING
  and terminal statuses stay cancellable by any build; they hold no claim.
- `GET /builds/{build_id}/executions`: the detached executions this build
  started and has not seen end, with the backend and ref to stop them by.
  Paged with a keyset `cursor` over the task — stopping an execution records
  nothing, so the answer does not shrink as a caller works through it, and a
  bare cap would hand back the same page forever. Keyed on the task rather
  than the start time because a task restarted between two page requests
  would otherwise be re-ranked past the cursor and skipped.
  Answered from the event log rather than from the task rows, because the
  question is about the past: releasing a claim is what lets the next build
  take the task over, so by the time a cancelled build ticks, the row may
  already name somebody else's execution. A ref is not a claim — cancelling
  the one this build recorded cannot reach another build's container. It can
  only report executions whose reference reached the registry: a resident
  build resuming a task from its dynamic dependencies, with workers that do
  not self-report lifecycle, records only `TASK_RESUMED` and so never sends
  the resumed handle's reference at all.
- `FrontierTaskRef.latest_status_build_id`: who holds each task in the
  frontier, so a scheduler can tell its own executions from a neighbour's.
- `POST /builds/{id}/notify` flags only a RUNNING build, and reports
  `needs_tick` accordingly.
- `POST /builds/{b}/tasks/{t}/cancel?if_executor=…&if_executor_ref=…` records nothing
  unless this build still holds the task in RUNNING or INTERRUPTED **under
  that execution** — evaluated on the locked row, so an engine cleaning up
  after itself from a listing it read a moment ago can neither stamp a task
  another build has since reset and is about to run, nor revoke the claim of
  an execution it started since and nobody stopped. A no-op rather than an
  error: losing that race is a normal outcome, not a fault.

### UI

- Claim triage marks a held claim "restart expected" when the platform said
  it was restarting that execution and the restart has not reported back —
  the one place `RUNNING` alone cannot distinguish a container that is
  working from one that was taken away and never replaced. `task_preempted`
  appears in a task's event timeline.

### Deployment

- **Server image `0.4.0`**, carrying every Registry API change above:
  scope-keyed dependency edges, plan membership and provenance by scope,
  the `deployments` table and `POST`/`GET /deployments`, phantom rows
  removed (migration `690e61e0c920`, which copies a running build's edges
  into its own scope); `POST /builds/{b}/tasks/{t}/preempt` and
  `tasks.latest_preempted_at` (migration for `TASK_PREEMPTED`, additive);
  build-scoped cancel authority and `GET /builds/{b}/executions`;
  idempotent concurrent registration and one insert order for the
  registration paths; the lease holder's re-acquire granted. Minor rather
  than patch for the usual reason: the HTTP surface grew and there are
  schema migrations. Self-hosters upgrade with `stardag self-host upgrade`;
  the hosted service builds from this commit.
- **Upgrade order is server first, then SDK.** An SDK at 0.24.0 refuses a
  Registry API older than 0.4.0 with `RegistryTooOldError`; an older SDK
  against the new server keeps working on a per-build scope the server
  assigns.

## [0.23.0] — 2026-09-01

### SDK

- **Scheduler ticks share a container.** The deployed Modal `tick` is now an
  `async def` and is registered with `@modal.concurrent(max_inputs=10)`, so up
  to ten concurrent reactive builds are served by one container instead of one
  each — stardag's default for this function, overridable through
  `FunctionSettings(max_concurrent_inputs=...)`.
  A tick is almost entirely I/O wait — read the frontier, spawn, then poll on
  a sleep until the linger deadline — so this is close to free, and it is what
  makes `linger_seconds` cheap: a resident tick driving level after level no
  longer costs a container of its own. Async is load-bearing rather than
  stylistic: Modal serves concurrent inputs to a sync function on _threads_,
  each running its own `asyncio.run`, and `APIRegistry`'s async client is
  cached per event loop and closed when the loop changes — so threaded ticks
  would tear down each other's in-flight HTTP client. Workers are deliberately
  not packed (they run user code and may be CPU- or GPU-bound), nor is
  `tick_watchdog` (its own function, one input per period), which shares
  `tick_settings` with the tick and so is held out explicitly rather than by
  having no default. Supporting
  changes: the async client's connection pool is sized for a shared process
  rather than for one caller, and the tick's two remaining blocking calls —
  the foreign-app forward and the successor hand-off's spawn — moved off the
  event loop, where they would have stalled every co-resident tick. The
  tick's completion log line now carries `MODAL_TASK_ID`, since packing is
  otherwise unobservable: `modal app logs` has no per-container attribution
  and `modal container list` names the app but not the function.
- **`FunctionSettings` speaks Modal's current vocabulary, and translates the
  old one.** `max_containers`, `min_containers`, `buffer_containers`,
  `scaledown_window`, `max_concurrent_inputs` and `target_concurrent_inputs`
  are now accepted; the last two are applied via `@modal.concurrent` rather
  than passed to `App.function()`, and validated in stardag's own vocabulary
  rather than left to fail at registration with a `TypeError` naming a
  parameter nobody wrote. The four legacy names Modal renamed in 2025
  — `concurrency_limit`, `keep_warm`, `container_idle_timeout` and
  `allow_concurrent_inputs` — are translated with a warning instead of being
  forwarded. This is a **bug fix, not a deprecation**: Modal's client raises
  `DeprecationError` on all four, and `FunctionSettings` passed them straight
  through, so any app that set one failed to deploy.

- **`require_pickle_free=True` now binds the scheduler tick, not just the
  trigger.** The flag was a trigger-time gate: the bootstrap refused to start
  a build whose tasks would need pickles, and nothing carried the intent any
  further. But a tick is a writer of the same store — it wrote back every task
  it rehydrated from registry data — so on a writable target root a build that
  declared it writes no pickles quietly left them there anyway, one per task,
  the first time each was rehydrated. Where the class could not be pickled the
  write merely failed, warning once per rehydration per tick. `BuildTaskStore`
  now takes `pickle_free`, which makes every write a no-op, and the deployed
  tick builds its store from the app's flag. Nothing is lost by skipping: the
  write-back is a cache over an object the caller already holds, and on such a
  build every task is rehydratable by construction. Unchanged: the trigger-time
  gate, and the documented carve-out for an uncovered _dynamic_ dependency
  registered from inside a worker, which still gets its pickle rather than
  failing the bookkeeping of a task that has already run.

- **`BuildTaskStore` gains async variants** — `save_task_aio` /
  `save_tasks_aio` / `load_task_aio`, built on the targets' existing
  `exists_aio` / `open_aio`. The reactive tick's `_load_task` and the async
  trigger path called the blocking methods straight from the event loop, which
  stalled every other coroutine in the tick for a network round-trip and drew
  Modal's `AsyncUsageWarning` on volume-backed roots. The sync methods stay for
  the sync callers. Also: a corrupt or truncated store pickle now reads as a
  **miss**, so the tick falls back to registry rehydration instead of raising.

- **Compatible with modal 1.5.5**, which moved two symbols in a patch release:
  `modal.volume.FileEntryType` → `modal.types.FileEntryType` (still re-exported
  at runtime, but dropped from the stub, so it broke type checking rather than
  execution) and `_utils.function_utils.FunctionInfo` → `FunctionSourceInfo`.
  Both are handled with an import fallback rather than a version floor bump,
  since the declared `modal>=1.0.0` spans both spellings — verified against
  1.5.0 and 1.5.5 alike.

- **`--server-version latest` is resolved at deploy time, not deployed as a
  tag.** `stardag self-host up/upgrade --server-version latest` now asks the
  GitHub Releases API for the newest `server-vX.Y.Z`, prints what it resolved
  to, and uses that concrete version for the image reference, the recorded
  deployment meta and `self-host status`. Previously the literal string
  reached `modal.Image.from_registry`, where a mutable tag is cached like any
  other image definition — so an upgrade could silently redeploy the image
  already built for `:latest`, and nothing afterwards could say which release
  was running (`status` echoed `latest`). Releases rather than git tags are
  the source of truth because the release job runs `needs: publish-image`, so
  a release exists only if the image push succeeded. A deployment recorded as
  `latest` by an older SDK resolves once on its next upgrade and converts to a
  pin. GHCR's `:latest` tag is unchanged and still usable directly.
- **A plain `stardag self-host upgrade` rolls the server version forward
  again.** `_resolve_upgrade_server_version` returned the recorded deployed
  version unconditionally, so once a deployment had recorded a version only an
  explicit `--server-version` could ever move it — a newer SDK's pin was
  ignored, contrary to what the self-host docs said. It now deploys the newer
  of the recorded version and `DEFAULT_SERVER_VERSION`, which keeps the
  anti-downgrade property that motivated the original behaviour (a deployment
  ahead of this SDK's pin stays where it is) while letting the pin move a
  deployment that is behind.

- **One arbitrated start method on `RegistryABC`.**
  `task_start_with_limits_aio` is removed; `task_start_claim_aio` gains a
  `claim: bool = True` parameter, so the two orthogonal flags the registry's
  single `/start` endpoint carries — `claim` and `enforce_limits` — are
  reached through one method. The resident build's
  `RegistryConcurrencyLimiter` acquires its slots with `claim=False` (the
  engine has already claimed the task before entering the slot, so a
  claiming acquire would be denied `already_running` by its own build) and
  now logs the keys the server actually held back on. No wire change and nothing to do for a
  build driven through the engines. **Breaking for a custom `RegistryABC`
  implementation in two ways**, both loud: any direct caller of
  `task_start_with_limits_aio` (a subclass that implemented it, or code
  calling it on `APIRegistry`, where it is also removed) migrates to
  `task_start_claim_aio(..., claim=False)`; and an existing
  `task_start_claim_aio` **override must accept `claim`** or it raises
  `TypeError` the first time the limiter calls it. A backend that
  implemented only `task_start_with_limits_aio` and relied on its no-op
  default now raises `NotImplementedError` from
  `RegistryConcurrencyLimiter` instead of silently skipping enforcement.
- The Modal worker's wake-up reads `BuildNotifyResult.scheduler_live` off
  the result directly instead of through `getattr`. Unknown still spawns —
  an older registry leaves the field `None`, and a notify that raised
  returns nothing at all — but the tolerance for a third-party backend
  answering with some other shape is gone, matching the v0.18.0 decision
  that custom-backend compatibility was never real.
- **The watchdog sweep spawns instead of running ticks inline.**
  `tick_watchdog` now lists the running reactive builds the app owns, spawns
  one `tick` per build and returns, rather than running every build's tick
  body sequentially inside the sweep's own container. Three things stop being
  a function of how many builds the environment happens to be running: each
  build's spawn cap (that one container's timeout was divided across the
  sweep), whether the sweep finished at all, and — to within a spawn RPC
  rather than a whole tick — how long the last build in the list waits. Each
  build now gets a container of its own, its full timeout and its normal
  linger; a duplicate spawn still starts a container, but that tick finds the
  scheduler lease held and exits without acting. The `linger_seconds=0` and
  share-of-timeout overrides the inline form needed are gone with it.

  A swept build's tick still does **one pass and exits**: `spawn_tick` grew
  an optional `tick_kwargs`, and the sweep uses it to ask for
  `linger_seconds=0`. The inline version forced that to survive sharing one
  container; it is kept for a better reason. A wake-up's tick lingers
  because something just happened and more is likely to, whereas a sweep
  looks at builds where nothing is known to have happened. Lingering there
  would hold container time every period on exactly the builds least likely
  to have anything to do — and since a container lives as long as its
  longest live input, a couple of stale `RUNNING` builds would be enough to
  keep the tick function warm, however few they are.

- Default prebuilt server image bumped to `0.3.0`
  (`DEFAULT_SERVER_VERSION = "0.3.0"`, from the `server-v0.3.0` release) —
  the server version this SDK release is tested against, and the one that
  carries the new server surface this release's reactive changes call:
  `GET /builds/{build_id}/notify` and the scheduler-lease routes. The
  image tag
  and `--server-version` take the bare `X.Y.Z`; `server-v` prefixes the git
  tag only.

- **The linger poll no longer reads the frontier.**
  `RegistryABC.build_get_notify[_aio]` reads the wake-up flag, and the tick's
  linger poll, its pre-release re-check and its exit hand-off all use it. The
  frontier is fetched only when there is something to act on. Against a
  server without the route the poll falls back to the frontier read it did
  before, and an unparseable answer reads as _unset_ rather than set —
  fabricating a change would spin the tick without releasing its lease.
  Latched once per process, since re-probing on every poll is the cost the
  endpoint removes. The default implementation on `RegistryABC`
  delegates to the frontier, so a custom backend needs no changes to keep
  working, and can override for the cheap read.

- **The tick no longer uses the global concurrency lock at all.**
  `run_tick_aio` **drops its `lock_manager` parameter** — the lease was its
  only use — and takes the lease through the registry instead, renewing it
  in the background while it lingers and stopping
  (`TickSummary.outcome == "lease_lost"`) if a renewal reports it was taken
  over. `RegistryABC` gains
  `build_acquire_scheduler_lease_aio` / `build_renew_scheduler_lease_aio` /
  `build_release_scheduler_lease_aio`, defaulting to granting, and the
  duplicated `SCHEDULER_LOCK_PREFIX` is gone from both sides.

  Against a server without the routes the tick runs unleased and says so
  once: duplicate ticks become possible (idempotent, and task starts stay
  arbitrated by the execution claim). That server also reports
  `scheduler_live=False` to every worker, because it reads a lock table this
  SDK no longer writes — so wake-ups spawn unconditionally too, which is the
  pre-lease behaviour end to end rather than a half-broken one.

  **The other direction is the one a real deployment takes**, since the API
  upgrades before the Modal apps that bake in their SDK: a new server does
  not read the legacy lock an old tick takes, so it reports
  `scheduler_live=False` for every completion and every worker spawns a tick
  that immediately no-ops. Correct, but it costs containers until each app
  is redeployed — worth doing promptly rather than leaving.

  The lease TTL is 60 s, unchanged from the lock-table lease it replaces,
  and it is the dead-tick recovery window: until it lapses, the build is
  hidden from drainers _and_ workers skip their spawn. Renewal moved from
  half the TTL to a third, so two consecutive renewal failures are
  survivable — and a third is too, because a refused renewal re-acquires
  rather than abandoning a build nothing else is driving.

- _Internal:_ `stardag/build/_reactive.py` (3,412 lines) is now the
  `_reactive/` package — `_discovery`, `_tick`, `_frontier_actions`,
  `_terminal`, `_budgets` — split along the section banners the module already
  had. No behaviour change and no import change: `stardag.build`'s re-exports
  are untouched and the package `__init__` re-exports what the module did, with
  one deliberate exception. The **mutable module globals** (the lease timing
  knobs, the tick-summary route latch, the successor-spawner warning latch) are
  _not_ re-exported: rebinding an alias leaves the reading module untouched, so
  a monkeypatch against the package would go green while patching nothing.
  Reach into the owning submodule instead.

### Registry API

- **`GET /builds/{build_id}/notify`** reads a build's scheduler wake-up flag
  from its own row, with nothing derived. The reactive tick's linger poll
  asks one question every few seconds per lingering build — "has anything
  changed?" — and used to ask it by fetching the whole frontier: seven
  statements, one of them a window-function aggregate over the event log,
  of which it read a single boolean.

- **The reactive scheduler's lease lives on the build row.** New
  `POST`/`PUT`/`DELETE /builds/{build_id}/scheduler-lease` acquire, renew and
  release a build's single-flight lease, recorded as
  `builds.scheduler_lease_until` / `scheduler_lease_owner` (migration, no
  backfill — a lease is transient). It used to ride on the deprecated global
  concurrency lock, so both readers had to assemble a lock name from a build
  id and query `distributed_locks`: `select_wake_candidates` is now one query
  instead of two, and "is a scheduler live?" is a column comparison. Renew
  and release are owner-checked, so a tick whose lease lapsed and was taken
  over cannot extend or clear its successor's. The global lock is untouched
  for its remaining use (executions without probeable liveness).

### Deployment

- **Server image `0.3.0`**, carrying the two Registry API changes above:
  `GET /builds/{build_id}/notify` (the linger poll's one-row read) and the
  scheduler lease on the build row —
  `POST`/`PUT`/`DELETE /builds/{build_id}/scheduler-lease` plus the
  `builds.scheduler_lease_until` / `builds.scheduler_lease_owner` migration
  (`b41c7d9e2f08`, no backfill). Minor rather than patch for the same
  reason as `0.2.0`: the HTTP surface grew and there is a schema migration.
  Self-hosters upgrade with `stardag self-host upgrade`; redeploy Modal
  apps promptly after — until each app is redeployed, its old ticks take a
  legacy lock this server no longer reads, so every completion reports no
  live scheduler and spawns a tick that immediately no-ops.

- **Server image `0.2.0`**, the first server release since `0.1.2`
  (2026-08-12). It carries the Registry API and UI changes recorded under
  0.19.0 — apart from the SQL-injection fix, which shipped in `0.1.2` itself
  — plus those under 0.21.0 and 0.22.0: `INTERRUPTED` as a first-class task
  status and `POST /builds/{id}/tasks/{task_id}/interrupt`,
  `POST /builds/{id}/notify` reporting `scheduler_live`,
  `POST /builds/wake-candidates`, and the `builds.tick_requested_at`
  migration (`97ce4e3cbf32`). It also carries the `python-jose` → `PyJWT`
  swap for token auth and raised security floors on the API's transitive
  dependencies and the UI's build tooling. Minor rather than patch: the HTTP
  surface grew and there is a schema migration. Self-hosters upgrade with
  `stardag self-host upgrade`.

  A self-hosted `0.1.2` predates `wake-candidates`, so an SDK on the v0.22.0
  line talking to it degrades to the previous behaviour — cross-build
  wake-ups arrive only via the watchdog — rather than failing.

## [0.22.0] — 2026-08-30

### SDK

- **A status change reaches every build it concerns.** A reactive build
  used to be woken only by its own Modal workers; a shared task finished,
  failed, cancelled or retried by another build's worker or tick, by a
  resident build, or by an operator in the UI or CLI — and a concurrency
  slot freed by another build — reached it only through the watchdog,
  which is off by default. The registry now flags every live reactive
  build holding a task whenever that task's status changes (and the
  builds queued on a key when a slot frees, and a build itself when it is
  cancelled), and every scheduler pass — a tick after acting and on exit,
  a resident build with Modal workers after each result — asks the
  registry for the flagged builds nobody is serving and spawns one tick
  each. The registry hands each build out once per window, so N schedulers
  asking at once produce one tick per flagged build, not N. `TickSummary`
  gains `neighbour_ticks_spawned`. Both halves degrade to the previous
  behaviour across version skew: an older registry answers nothing, an
  older SDK never asks.
- **Concurrency-limit keys are registered at plan time.**
  `discover_and_register_aio(limit_key_selector=...)` sends each task's
  keys with its registration (the bootstrap passes the app's selector; the
  worker wrapper publishes it for dynamically yielded dependencies), so the
  registry knows which pending tasks want a key and can wake the builds
  queued on it when a slot frees. Plan-time rows never count towards
  occupancy — only a `RUNNING` task under a live claim does.
- **`tick_watchdog` is deployed on every app**, scheduled only when
  `watchdog_period_minutes` is set, so a full sweep is one click away on an
  app that runs no cron. `build_trigger(reactive=True)` no longer warns
  when no period is configured. `TaskExecutorABC` gains
  `can_spawn_scheduler_ticks` / `spawn_scheduler_tick`; the Modal executor
  implements them and a `RoutedTaskExecutor` delegates.
- **`TickConfig.spawn_successor_tick(build_id)` is now
  `TickConfig.spawn_tick(build_id, app_name)`.** One callable serves the
  exit hand-off and the cross-build drain. Only callers driving
  `run_tick_aio` by hand are affected; the Modal integration supplies it.
- **Docs:** `concepts/build-execution.md` is rewritten top-down and
  executor-agnostic; the Modal-specific model (detached execution,
  resident vs reactive, wake-ups, the watchdog) moves to a new
  `concepts/modal-orchestration.md`, and the Modal how-to's reactive
  section is consolidated around configuration.

### Registry API

- `POST /builds/wake-candidates` — hands out RUNNING reactive builds that
  are flagged, hold no live scheduler lease and were not handed out within
  the last window, stamping `builds.tick_requested_at` (new column,
  migration `97ce4e3cbf32`) in the same transaction. `POST
/builds/{id}/notify` stamps it too when it reports no live scheduler.
- Every path that changes a task's status — the event routes,
  `skip-blocked`, `cancel?cascade=true`, bulk cancel and the reaper, the
  lock release-with-completion — flags the other live reactive builds
  holding the task, transition-gated. A build cancel flags the build.
- `POST /builds/{id}/tasks/bulk` accepts an optional `limit_keys` per task
  and records them; `null` leaves recorded keys alone, and a `RUNNING`
  task keeps the keys it was started under.
- `PUT /builds/{id}/reactive-meta` rejects an empty `app_name`.
- The reaper's idleness signal no longer counts `needs_tick_at`: the flag
  is written by other builds' transitions now, and was redundant with the
  event stream before.

## [0.21.0] — 2026-08-29

### SDK

- **The reactive scheduler no longer logs an ERROR for a task-store miss it
  recovers from.** `BuildTaskStore.load_task` logged
  `... not found in the build task store — cannot (re)schedule it` on every
  miss, but its only caller rehydrates the task from registry data and
  succeeds. Declaring `task_modules` _is_ the opt-in to pickle elision, so on
  the recommended configuration every lookup misses by design: a healthy
  seven-task build emitted seven errors claiming its tasks could not be
  scheduled, immediately after which all seven were. The miss is now DEBUG.
  A store entry that is not a `BaseTask` remains an ERROR, and `_load_task`
  still logs at ERROR when _both_ stages fail — with the failed-import
  annotation that makes it actionable.

- **`with_stardag_on_image` no longer pins a Modal image to a stale PyPI
  release when stardag is installed editable.** The choice between "ship
  the local working tree" and "install the pinned release" was inferred
  from `stardag.__version__` — but that is
  `importlib.metadata.version("stardag")`, and an **editable install's
  metadata version is a snapshot taken when the install ran**. hatch-vcs
  computes it from the git tag reachable at that moment and nothing
  recomputes it as the working tree moves on, so a checkout installed at
  v0.17.0 keeps reporting `0.17.0` while its source is v0.20.x. That string
  carries no `dev` and no `+`, so it read as a plain released version and
  the image was pinned to a real PyPI release **older than the code being
  serialized into it**.

  The result is the failure shape v0.20.1's placement guardrail was built
  for, from a different cause: the app deploys cleanly and then every
  container dies at hydration with `ModuleNotFoundError` for a stardag
  module the deploying process could see and the container cannot —
  observed as `No module named 'stardag.integration.modal._builder'`,
  before any of the app's own code runs.

  `local_stardag_source="auto"` now asks the installer whether this is a
  working tree (an editable install, a bare `sys.path` entry, or a dev
  version) instead of guessing from the version string. The two routes that
  can still pin a version older than the running source — an explicit
  `local_stardag_source="no"` from a working tree, and an explicit
  `version=` older than the running one — warn rather than refuse, since
  both are something the caller asked for.

  Note that a plain `uv sync` does not refresh a stale editable version:
  the install is already present, so nothing rebuilds its metadata.
  `uv sync --reinstall-package stardag` does.

- **A worker no longer spawns a scheduler tick when one is already
  running.** Every task completion used to spawn a tick unconditionally. On
  a build whose tasks are short relative to a tick container's startup,
  none of those ticks scheduled anything — the resident scheduler's own
  linger loop did all the work, and each spawned tick started only after it
  had finished, took the lease or found the build terminal, and exited. A
  measured seven-task build paid seven cold starts for zero scheduling.
  `build_notify` now reports whether a scheduler holds the build's lease
  (`BuildNotifyResult.scheduler_live`) and the worker skips the spawn when
  it does — one working tick instead of N+1. An older registry does not
  report it, and every wake-up then spawns a tick exactly as before.

- **The scheduler tick's exit path no longer has a lost-wakeup window.**
  Nothing re-read the wake-up flag between the linger loop's final poll and
  the release of the scheduler lease, so a flag set in that window was
  served by nobody — it stayed set until the next completion or the
  watchdog (off by default), and with the last task in flight there may be
  no next completion. Harmless while every wake-up spawned its own tick;
  the load-bearing prerequisite for skipping that spawn. The tick now
  re-reads the flag once **before** releasing the lease (set → keep the
  lease and re-act) and once **after** (set → spawn a successor tick), and
  reports both on its `TickSummary` as `linger_extended` and
  `successor_spawned`. This closes the release window; it is not crash
  recovery — a tick that clears the flag and then dies still leaves that
  wake-up to the next completion or the watchdog, as before.

  `RegistryABC.build_notify` returns a `BuildNotifyResult` rather than
  `None`. A custom registry backend that overrides it and returns `None` is
  read as "scheduler state unknown", which keeps today's behaviour.

### Registry API

- `POST /builds/{id}/notify` reports `scheduler_live` — whether a reactive
  scheduler held the build's lease when the response was produced. The read
  happens after the flag is committed rather than atomically with it, and
  that ordering is the whole guarantee: a `true` means the lease was still
  held once the flag was already durable, so its holder cannot exit without
  seeing it. That is what makes skipping the tick spawn safe.

## [0.20.1] — 2026-08-28

### SDK

- **`StardagApp(...)` now refuses a callable no container could unpickle.**
  Everything an app passes — `container_setup`, `worker_selector`,
  `limit_key_selector`, `build_function`, `run_function` — is cloudpickled
  into the `serialized=True` functions `finalize()` registers, and
  cloudpickle stores a module-level callable (or the class of a callable
  instance) as a _reference_ to its defining module.
  `stardag modal deploy path/to/app.py` loads the entry point under a module
  name taken from the file name, so a `def` written in `app.py` pickles as
  `app.<name>` — a module that exists only in the deploying process. The app
  deployed cleanly and then every function carrying that callable died at
  hydration with `ModuleNotFoundError: No module named 'app'`, before
  reaching any of the app's own code. The damage was partial and delayed:
  `build` and `worker_*` often survived, because their closures reach the
  app's package modules anyway, while the scheduled reactive functions did
  not — so an app could look healthy with its scheduler dead.

  This is now a `SerializedCallablePlacementError` at `StardagApp(...)`,
  naming the callable, the module and the fix. It raises rather than warns:
  unlike the `task_modules` coverage warning there is no degraded-but-working
  path on the other side of it.

  Lambdas, closures and anything defined in `__main__` are **not** rejected —
  cloudpickle writes those out by value, so they need no import in the
  container and work today. A `functools.partial` or a bound method is
  itself by value but carries a reference to what it wraps, so the check
  looks through both.

  Docs: the placement rule was stated only on `ContainerSetup`, which read as
  though it were specific to the hook. It now covers all five parameters, and
  the Modal how-to gained a section stating the deploy CLI's import naming
  explicitly — the part no app author can infer from their own code.

## [0.20.0] — 2026-08-14

### SDK

- **`StardagApp(container_setup=...)` — one declared place for setup that
  every Modal container of an app needs.** A zero-argument callable, run once
  per container at the top of **all five** registered functions: `build`, each
  `worker_*`, and the reactive `tick`, `bootstrap` and `tick_watchdog`.

  It closes a real gap rather than adding sugar. `finalize()` registers every
  function with `serialized=True`, so a container unpickles a closure instead
  of importing the module the app was declared in — and which of the app's
  modules _do_ get imported was decided by what each closure happened to
  reference. `build` and `worker_*` close over the app's `build_function` /
  `run_function`, so their modules are imported and their module-level setup
  runs; that is the behaviour `StardagApp.__init__` has always documented. But
  a `bootstrap` container closes over nothing of the app's at all (just the app
  name, the task-module patterns and two flags), and `tick` / `tick_watchdog`
  import app code only as a side effect of a supplied `worker_selector` /
  `limit_key_selector` or of the expanded `task_modules`. Setup that appears to
  "run everywhere" because it runs in the workers could therefore be silently
  absent from exactly the containers that drive a reactive build — reported
  from a deployed app whose storage credentials were prepared by such a
  routine, whose reactive builds then failed in `bootstrap` at the first
  completion check.

  Semantics:

  - **Once per container, not once per input** — a worker serves many tasks and
    a tick container may be reused; the guard lives in stardag so apps do not
    each write one.
  - **A hook that raises propagates and is retried on the next input.** It is
    deliberately not remembered as done on failure, so a container's remaining
    inputs cannot run silently un-set-up; a deterministic failure fails every
    input, loudly.
  - **Runs before stardag's own `logging.basicConfig` default.** `basicConfig`
    no-ops once the root logger has handlers, so an app that configures root
    logging in the hook owns log formatting in these containers, and an app
    that does not still gets the default.
  - Pickled by reference like `worker_selector`, so **define it in a module
    importable inside the container** — which is also what makes module-level
    code in the hook's own module run in every container.

  It complements, and does not replace, `Builder.setup(tasks)` (per build,
  `build` container only) and `Runner.setup(task)` (per task). For the reactive
  functions there is no choice to make: a `tick` / `bootstrap` /
  `tick_watchdog` container holds neither a `Builder` nor a `Runner`, so this
  is the only hook that reaches them.

  Additive — an app that passes nothing behaves exactly as before. The new
  `ContainerSetup` type alias is exported from `stardag.integration.modal`.

- **`finalize()` now fails an app with no `"default"` worker and no
  `worker_selector`.** Every task would route to a `worker_default` function
  the app does not deploy, so the deployment is dead on arrival — previously
  it deployed cleanly and failed at the first task. Scoped to the
  no-selector case: an app that declares a selector may omit `"default"` and
  route everything to its own tiers, which works today and keeps working.

- **`finalize()` now warns about workers nothing can route to.** An app that
  declares several `worker_settings` but no `worker_selector` sends every task
  to `"default"`, so its other tiers are deployed and never reached — and the
  symptom is indistinguishable from a healthy deployment, because the build
  succeeds, just entirely on the wrong worker. The warning names the
  unreachable workers and fires at deploy, not on the app object the
  triggering process constructs. Passing a selector explicitly — even one that
  always returns `"default"` — silences it. Per-trigger overrides
  (`build_spawn`/`build_trigger(worker_selector=...)`) remain a valid way to
  route a **resident** build; reactive builds reject them, since later ticks
  could not honour them, which is why the app-level selector is what the
  warning points at.

## [0.19.1] — 2026-08-14

### Fixed

- **A user package named `modal` no longer breaks target resolution.**
  Reported from a deployment running `0.19.0`, where `get_directory_target()`
  failed at import with

  ```
  AttributeError: module 'modal' has no attribute 'exception'
  ```

  in a service that did not use Modal at all. Two independent defects had to
  line up.

  First, the trigger. The service's entrypoint was launched as a script path
  (`python pkg/service/main.py`, not `python -m`), which puts the script's own
  directory on `sys.path[0]`. That directory contained a first-party
  `modal/` subpackage, so **every** `import modal` in the process — stardag's
  included — resolved to it instead of the installed distribution. `import
modal` therefore _succeeded_ and returned the wrong module; the failure
  surfaced only on first attribute access. Nothing on that service's code
  path had ever imported `stardag.integration.modal` before, which is why the
  shadowing had been latent and `0.19.0` appeared to cause it.

  Second, why it escalated. `get_default_prefix_to_target_prototype()` imports
  the optional `stardag.integration.*` backends to register the `s3://` and
  `modalvol://` prefixes, and guarded those imports against `ImportError` —
  the "not installed" case — and nothing else. A shadowed `modal` does not
  raise `ImportError`, so the `AttributeError` propagated out of the factory
  and killed target resolution for **every** prefix, local paths included.

  Both are fixed. `_runner.py` and `_app.py` no longer resolve
  `modal.exception` off the parent package — `exception` is not in modal's
  `__all__`, and the attribute is bound only as a side effect of modal's own
  `__init__` — so a shadowed or partial `modal` now fails as a plain
  `ImportError`, which is both accurate and catchable. And the factory's
  guards now also catch and log an unexpected failure: the affected prefix
  drops out of the mapping, using it gives the ordinary unsupported-prefix
  error, and a warning names the cause. A genuinely absent optional dependency
  stays silent, as before.

  Note the attribute form worked on every modal release stardag supports
  (`>=1.0.0`); it was the shadowing, not a modal version, that exposed it.

## [0.19.0] — 2026-08-13

### Registry API

- **Security (server image `0.1.2`): fixed a SQL injection in the task search
  endpoint.** `GET /api/v1/tasks/search` builds JSONB accessor chains as SQL
  text (Postgres has no bind-parameter form for a `->'key'` step). The path
  segments of a `filter` key, and the artifact name in a `sort` field, were
  interpolated without validation, so a crafted key or sort value could inject
  SQL. The endpoint requires authentication, and the injection lands inside the
  environment-scoped `WHERE`; a malformed value was read-only (no statement
  stacking on the parameterised path) but could read across environments. The
  exposure is limited to **registry metadata** — task identities, parameters,
  build history, registry-stored artifacts, and user/credential records (keys
  and any local passwords are bcrypt hashes). Task `target()` output is not
  stored by the service — it lives in the user's own filesystem/S3/Modal
  storage — and is not exposed by this issue. All released server images before
  `0.1.2` are affected. Fixed by validating every path segment against an
  identifier character class and binding the sort artifact name as a parameter;
  filter/sort inputs are now length-bounded. Self-hosters should upgrade to
  server image `0.1.2`. See advisory
  [GHSA-47m3-4ppr-cfh4](https://github.com/stardag-dev/stardag/security/advisories/GHSA-47m3-4ppr-cfh4)
  (CVSS 7.7 High; read-only, authenticated).

- **`TASK_INTERRUPTED` / `TaskStatus.INTERRUPTED`**, and
  `POST /builds/{id}/tasks/{task_id}/interrupt`. Modelled on `SUSPENDED`:
  non-terminal, non-running, holds no execution claim, listed as
  actionable by the frontier, and reset by a re-trigger. **No migration** —
  both columns are already `String(32)`.

  Deliberately a status rather than a `retryable` flag on `/fail`: a
  worker-recorded _failure_ sits in the next frontier snapshot and, under
  `FAIL_FAST`, kills the build before anything can retry it. A tick avoids
  that only because it records and retries inside one pass.

- `FrontierTaskRef.interrupt_count`, counted over the same build round as
  `attempt_count`. An interruption between two starts does not open a new
  attempt.

- Old SDKs are unaffected (nothing emits the new event). A **new SDK
  against an older server** logs a warning and records nothing, which is
  its pre-existing behaviour — a version skew degrades to the old recovery
  path, never to a failed build.

### UI

- **A build whose work is done no longer says it needs intervention.** A
  build that failed waiting on a shared task, which another build later
  completed, kept showing "Nothing runnable, and no wake-up pending — needs
  intervention" alongside a status count taken when it failed. The panel now
  recognises that every root is complete.

- `INTERRUPTED` renders as its own status (orange, beside skipped's amber
  rather than failed's red), is filterable, and offers Retry but not
  Release — an interrupted task holds no claim to release.

### SDK

- **A `StopAsyncIteration` raised by an async task's loop body is no longer
  swallowed.** `_drive_async_generator` wrapped its `async for` in
  `except StopAsyncIteration: pass`, mirroring the sync driver — but `async
for` consumes the generator's own exhaustion, so the handler could only
  ever catch one raised by the _loop body_ (a `complete()` check, say).
  Swallowing that returned `None`, which the caller reads as "task
  completed" and reports as such. It now propagates.

- **A task can survive preemption and function timeouts** (closes
  [#245](https://github.com/stardag-dev/stardag/issues/245)). The two ways
  a container routinely dies without the task being broken had no
  representation in the SDK, and a task that caught the interrupt to
  checkpoint — which is what you must do — and re-raised anything derived
  from `Exception` recorded a permanent `TASK_FAILED`, killing a
  `FAIL_FAST` build.

  New: **`sd.ResumableInterruption`**, and
  **`stardag.integration.modal.MODAL_INTERRUPTIONS`** — the exact pair the
  platform raises (`KeyboardInterrupt` for preemption,
  `modal.exception.InputCancellation` for a timeout, which is _not_ a
  `KeyboardInterrupt`).

  ```python
  try:
      train(resume_from=checkpoint)
  except MODAL_INTERRUPTIONS:
      save_checkpoint(checkpoint)
      raise sd.ResumableInterruption("checkpointed") from None
  ```

  **The task decides, not configuration.** Raising `ResumableInterruption`
  is the only way a task gets resumed, bounded by the new
  `TickConfig.max_interruptions` (default 20) — a budget separate from
  `max_attempts`, or a trainer designed to be killed and resumed would
  exhaust one meant for genuine failures. An interruption a task does
  **not** catch stays a failure under `max_attempts`: it means the task had
  no plan for one, so either it hung or the worker's `timeout` is too
  small, and neither is improved by resuming it.

  Catch `MODAL_INTERRUPTIONS`, never `BaseException` — a `NameError` is a
  `BaseException` too, and resuming one would run a deterministic failure
  until the budget is gone.

  The Modal runner reports the interruption inside the grace window the
  platform allows, which releases the execution claim and its
  concurrency-limit slots immediately and wakes the scheduler directly — so
  recovery does not depend on the (opt-in) watchdog. It reports only when
  nothing else will recover the execution: before the function timeout
  fires an escaping `BaseException` gets the input restarted by Modal on
  the same call id, which is faster and keeps the claim.

  **Reactive builds only.** `sd.build`/`build_aio` have no resumption path;
  a timed-out execution fails the task there as it always did.

- **`FunctionSettings` gains `nonpreemptible` and `startup_timeout`**, the
  two Modal knobs this topic needs that were not previously expressible
  through `StardagApp`. `nonpreemptible=True` is the direct answer to
  "this task must not be preempted" (3× CPU/memory price, not supported
  for GPU functions).

### Deployment

- **Cognito self-signup now defaults to off** (AWS CDK templates). The user
  pool was created with `selfSignUpEnabled: true`, so a deployment reachable
  from the public internet let anyone register — and the Registry API
  auto-provisions an internal user and personal workspace on first login.
  It is now a config flag, `COGNITO_ALLOW_SELF_SIGNUP`, defaulting to
  `false`. **Self-hosters who rely on open registration must set it
  explicitly.** Note this governs _native_ registration only: with a
  federated IdP, Cognito still auto-provisions on first login regardless —
  the README covers the pre-sign-up-Lambda and IdP-side options.

- **Dependency security floors raised** across the API's transitive
  dependencies, the UI's dev/build tooling, and `aws-cdk-lib`
  (2.215 → 2.264, picking up a critical handlebars advisory and CDK
  tooling CVEs). Dev lockfiles relocked and Dependabot configured.

- **`aws-cdk` CLI 2.1030.0 → 2.1136.0**, to match the library above. 2.264
  emits cloud assembly schema 54.0.0 and the old CLI reads at most 48.x.x,
  so `cdk synth` refused the manifest and every deployment from
  `infra/aws-cdk` failed at step one. It fails closed — nothing is
  deployed, no stack is left half-updated — but **self-hosters deploying
  the CDK templates need this bump**, not just the library one. Bootstrap
  stack version 6 is still all the templates require; no re-bootstrap.

## [0.18.0] — 2026-08-11

### Registry API

- **The API now knows which SDK is calling it, and can say so when that
  stops being good enough.** The SDK reports its own version in a dedicated
  header, `X-Stardag-SDK-Version`; the server parses it, records it (one log
  line per distinct version per process, so "which SDK versions are actually
  calling us?" is answerable), and exposes it on `request.state.sdk_version`.
  A descriptive `User-Agent` rides along for logs, and the server
  deliberately never parses it — a policy decision must not depend on a
  free-form string that proxies rewrite.

  Alongside it, a compatibility floor: `STARDAG_API_SDK_MINIMUM_VERSION`.
  **It is unset by default, and unset means no request is ever rejected** —
  which is the state this release ships in, because the API remains wire
  compatible with every SDK released so far (changes have been additive
  fields, parameters and endpoints; no response has changed shape or
  meaning). Nothing about any client's behaviour changes here. When it _is_
  set, an SDK below it gets `426 Upgrade Required` with a body naming the
  version it is on, the version required, and the `pip install --upgrade`
  command to get there.

  The rules that make the switch safe to flip later: a **missing** header is
  never fatal (every SDK released before the header existed sends nothing,
  and must keep working), a **malformed** value is treated identically to a
  missing one and never 500s, and comparison is real PEP 440 — with
  pre-release, dev and local builds counted as their base release, so a
  `0.18.0.dev1` local build is not told to upgrade to `0.18.0`. `/health`
  and `GET /api/v1/version` are never gated, so a refused client can always
  fetch the policy that refused it.

  `GET /api/v1/version` gains `minimum_sdk_version` (`null` by default), so
  the SDK, the docs and support read one number from one place.

  **Process rule:** an API change that raises `minimum_sdk_version` must
  state it in `CHANGELOG.md`, in `RELEASE_NOTES.md`, and in the error the
  server returns. See "Releasing the Server" in `DEV_README.md`.

- **Task refs now carry `attempt_count`, so a scheduler can run a retry
  policy** ([#208](https://github.com/stardag-dev/stardag/issues/208)).
  Reactive scheduling records a failed execution and never respawns it —
  retries are the execution backend's job — but a backend's function-level
  retries only cover exceptions raised _inside_ a container that started.
  A spawn that failed before the container existed, an OOM kill, a
  preemption, or a worker that died after writing partial output are
  invisible to them, so under fail-fast a single transient failure killed
  the whole build. Deciding otherwise needs a _durable_ attempt count: a
  tick is short-lived and cannot remember what it already tried.

  `attempt_count` is how many times execution has been started for a task
  **since its build's most recent `BUILD_RESUMED` event** — since the
  build began, if it never was. Exposed on `FrontierTaskRef` (all of
  `actionable`, `running` and `roots`), on `GET /builds/{id}/tasks`, and on
  every task lifecycle event response so a caller that has just recorded a
  failure or won a claiming start can apply its budget without a second
  round-trip. Always populated wherever it is declared.

  Three things it is easy to assume and get wrong:

  - **It counts attempts, not `TASK_STARTED` events.** Engines emit
    several starts per execution — an acquiring start for the claim /
    limit slot before the spawn (no executor ref), a second carrying the
    ref, plus the worker's own start when the executor self-reports
    lifecycle. A run of consecutive starts collapses to the one execution
    it describes, which makes the number the same whichever engine and
    executor ran the task.
  - **The scope is the build, not the environment** — unlike the
    `latest_*` fields it travels with. A task that spent two attempts in
    an earlier build arrives in a fresh trigger with a full budget.
  - **A resume resets it; a retry does not.** `build_trigger(...,
build_id=<existing>, reactive=True)` is the recommended way to pick a
    failed reactive build back up, and it does _not_ mint a new build: it
    resumes this one and retries the failed tasks in it. Counting over a
    build's whole history would therefore leave the budget spent the
    moment the user asked for another go, and resuming would mean also
    raising `max_attempts`. `BUILD_RESUMED` is the durable marker of
    "another round was asked for" — and the server already skips it for a
    build with no activity beyond `BUILD_STARTED`, so a first trigger is
    unaffected. `TASK_RETRIED` on its own does not reset, because a
    scheduler retries _through_ that endpoint: a counter cleared by it
    would be cleared by every enforcement of the budget it defines. So a
    bare retry against a spent budget is a real "this round is out of
    attempts", and resuming is the answer to it.

  Derived from the event log rather than denormalised — attempts are
  per-build, per-round, and there is no per-(build, task) row to hang a
  column on — as one grouped query bounded to the tasks being reported,
  with the round cutoff riding along as a correlated subquery. No
  migration; the frontier costs exactly one extra query, and none at all
  when it has no tasks to report. Purely additive: every existing field
  keeps its exact semantics.

- **Reactive scheduler tick summaries are now persisted per build**
  (`POST`/`GET /builds/{build_id}/tick-summaries`). A reactive build is
  driven by many short-lived scheduler ticks, each in its own container,
  and each tick's `TickSummary` — the scheduler's own account of what it
  did and why it did nothing — previously reached nothing but that
  container's log. Reconstructing why a build stalled meant correlating
  logs across dozens of containers; it is now a single request. The
  summary is stored verbatim in a JSONB blob (with `outcome` promoted to
  an indexed column), so the SDK can add fields without a server release
  or a migration. Retention is bounded per build (newest 50 by default,
  `STARDAG_API_MAX_TICK_SUMMARIES_PER_BUILD`), pruned on insert.
  Additive: nothing writes to these endpoints yet.
- **A failed build now reports _why_, on the build itself.**
  `BuildResponse.latest_error_message` carries the reason recorded on the
  build's newest `BUILD_FAILED` event, so "why did this fail" no longer costs a
  `GET /builds/{id}/events` per build — which is why no listing ever showed it.
  Reported while the build is `failed` and not afterwards: a build resumed after
  failing is running again, and a current status paired with a previous round's
  reason misleads worse than no reason. A blank reason is normalised to `null`,
  so "none recorded" has one representation. Derived from the event rather than
  denormalised onto the row (unlike `Task.latest_error_message`), batched into
  one grouped query per page on the listing path.

- **`GET /builds/{id}/frontier` now reports why a build is waiting on work
  it does not own** ([#208](https://github.com/stardag-dev/stardag/issues/208)).
  Dependency gating is environment-global (task rows and edges are shared
  across builds) while `running`/`status_counts` cover only the tasks a
  build has events for, so an upstream left non-COMPLETED by another build
  could gate a build's tasks while contributing nothing it could see — a
  scheduler then read "nothing actionable, nothing running" as "cannot
  progress" and failed the build. The new `blocked_by_external` list pairs
  each such blocked task with its blocker (identity, status, status
  timestamp, the owning build id, the claim's expiry where the blocker holds
  one, the attempts the blocker has spent in this build's round, and whether
  the blocker is in this build's task set), capped with an explicit
  `blocked_by_external_truncated` flag. Purely additive: every existing field
  keeps its exact semantics.

  `blocking_in_build` is reported for diagnostics and is not the field a
  scheduler should branch on: what happens next follows from the blocker's
  status. Plan closure (below) makes `true` the normal case, but `false` stays
  reachable — closure runs once, at registration, so an edge written afterwards
  is outside the plan, which is what happens whenever a concurrent build's
  worker yields dynamic dependencies into its own plan.
  `blocking_attempt_count` is `null` for a blocker outside the plan, which is
  also what keeps a scheduler from resetting one: it has no budget to spend
  there.

- **Registration closes a build's plan over every recorded dependency edge**
  ([#208](https://github.com/stardag-dev/stardag/issues/208)). A build's plan
  is every dependency of its roots that was not complete at discovery time,
  pruned at complete tasks. Discovery enforces that by walking
  `requires()` — **static** edges. But gating consults every recorded edge,
  and _dynamic_ edges are written by whichever build first ran the task and
  then outlive it: environment-global and permanent. So a later build that
  statically discovers the same task inherited the dependency without
  inheriting the task, and was gated on an upstream **no build containing it
  could schedule** — the only thing that would produce it being the very task
  being gated. Permanent deadlock.

  Both registration endpoints now admit incomplete upstreams into the plan,
  transitively, stopping at complete tasks. Admission is a status-neutral
  `TASK_REFERENCED`: the upstream's own state is untouched, it simply becomes
  part of this build's plan, which is what makes it schedulable here.
  Over-approximating is safe and under-approximating is not — a stale edge
  costs one unnecessary upstream, a missing one deadlocks — so no attempt is
  made to judge whether a recorded edge is current.

  RUNNING upstreams are admitted too. Closure runs **once**, at registration,
  while RUNNING is transient: excluding them would leave a permanent hole in
  the plan the moment the task stopped running, and the likeliest way for it
  to stop is an operator releasing a stale claim — the documented remedy
  stranding every build that inherited the dependency. Safety comes from the
  claim, not from which build started the task.

  **Visible consequence, and it is the intended one:** an incomplete upstream
  now belongs to the build that depends on it, so `GET /builds/{id}/graph`
  reports it as a **primary** node rather than as greyed-out context, and
  `upstream_depth` reveals only _complete_ upstreams. It also joins the
  build's own `actionable`/`running` and its status counts. Nothing to change
  on your side, but the counts and the graph will look different.

- **Frontier task refs now actually carry `latest_status_at`.** The field
  was declared and documented as the input to scheduler staleness bounds
  but never populated, so it always serialised as `null` and those guards
  silently did nothing.
- **`POST /builds/{id}/tasks/{task_id}/retry` accepts `suspended`.** A task
  suspended for dynamic dependencies and then abandoned (orchestrator died,
  build cancelled) was permanently unschedulable — the only escape was an
  undocumented cancel-then-retry. `running` remains non-retryable on
  purpose: it holds a live execution claim, and releasing that is
  cancellation, not retry.
- **`GET /tasks` can enumerate claim holders**
  ([#208](https://github.com/stardag-dev/stardag/issues/208)). New
  `status` filter (repeatable, so `?status=running&status=suspended`
  works) and `status_older_than` (an absolute ISO-8601 cutoff) answer
  "which tasks in this environment are holding an execution claim, and for
  how long?". `latest_status` is environment-global, so a task left RUNNING
  by a build whose orchestrator died denies the claim to every future build
  that needs it. When either filter is applied the list is ordered oldest
  claim first; unfiltered ordering is unchanged. Backed by a new
  `(environment_id, latest_status, latest_status_at)` index.
- **`TaskResponse` now carries `latest_status`, `latest_status_at` and
  `latest_status_build_id`.** The last is the claim holder — "running under
  build Y since T" — and was previously unavailable outside the build
  frontier. Purely additive.
- **`POST /builds/{id}/cancel` accepts `cascade=true`.** Cancelling a build
  wrote a single build-level event and nothing else, so the claims and
  concurrency-limit slots its tasks held survived it indefinitely. With
  `cascade` the build's RUNNING/SUSPENDED tasks are cancelled in the same
  transaction. Scoped to tasks whose current status _this_ build produced,
  so it can never declare another build's live execution dead; PENDING tasks
  are left alone for the same reason. Default off — it is a behaviour change,
  and the SDK's fail-fast path cancels its own running tasks.
  The response gains `cascaded_task_ids` / `cascaded_task_count`.
- **New `POST /builds/bulk-cancel`: bulk cleanup and a stale-build reaper.**
  Nothing terminated abandoned builds: build status is derived from events,
  so a build whose orchestrator died without emitting a terminal one stays
  RUNNING forever, and interrupted local runs, crashed CI jobs and failed
  triggers accumulate permanently. One endpoint serves both shapes —
  `build_ids` for an explicit set, `idle_for_seconds` for staleness — with
  `dry_run`, `cascade` (on by default here), and reactive builds excluded
  unless asked for. Only builds whose derived status is RUNNING are ever
  touched, so it is idempotent.

  **Idleness is measured on activity, not on `last_active_at`.** That column
  is bumped by build-level lifecycle transitions only — task events skip it
  so worker traffic doesn't contend on the build row — so a build running
  tasks for three days still shows its BUILD_STARTED timestamp there, and
  reaping on it would cancel live work. The signal used is the newest of the
  build's entire event stream, its `last_active_at`, and any pending
  scheduler wake-up (`needs_tick_at`).

- **`BuildResponse` exposes `last_active_at` and `last_activity_at`** — the
  ordering column and the reaper's idleness signal respectively — so a UI can
  show operators the same number the reaper acts on.
- **`GET /builds` accepts `idle_for_seconds`**, using the same idleness
  definition and the same 60s floor as `bulk-cancel` (one shared SQL
  predicate, not a second implementation) — so a client can list what the
  reaper would cancel before cancelling it. Because it is a real SQL
  predicate, `total` is an exact `COUNT(*)` and pagination is server-side
  and unbounded. Ordering flips to stalest-first when it is given. It
  combines with any `status` value — see the denormalisation entry below,
  which made that true for the non-`running` ones too.
- **Optional unattended sweep** (`STARDAG_API_REAPER_ENABLED`, off by
  default) runs the same operation on a timer inside the API process. Note
  that every replica runs its own timer with no leader election; cancellation
  is idempotent, so concurrent sweeps are wasteful rather than wrong.
- **The execution claim now has an expiry, so an abandoned claim heals
  itself** ([#208](https://github.com/stardag-dev/stardag/issues/208)).
  `Task.latest_status == RUNNING` _is_ the claim, and it recorded no
  liveness evidence a third party could evaluate: a holder that vanished
  denied the task to every future build indefinitely and leaked its
  concurrency-limit slots with it. New nullable column
  `tasks.latest_status_expires_at`, written once when a start grants the
  claim — **not** a lease, nothing heartbeats it. A claim past its expiry
  simply is not a claim: the next claiming start takes the task over,
  replacing the dead holder's build, executor fields and expiry together.
  No reaper, no release call, no new status a user has to understand.
  - `POST /builds/{id}/tasks/{task_id}/start` accepts
    `claim_ttl_seconds` (60 s … 30 days, 422 outside that). Set it from the
    executor's own timeout plus a small grace — the caller is the only
    party that knows how long the execution may legitimately take. Omitted,
    it falls back to `STARDAG_API_CLAIM_DEFAULT_TTL_SECONDS` (default
    7 days). That fallback is deliberately generous because it is what a
    caller that does _not_ derive a TTL gets — every SDK predating this
    change, and any newer one whose executor declares no timeout — so it
    must be a bound no realistic task reaches. Expiring late only delays a
    heal that today never happens; expiring early hands a live task to a
    second claimant. It is a backstop, not the cleanup path: a claim held
    by an abandoned _build_ is released within a day by the reaper's
    cascade, and an operator can release any claim immediately.
  - **The concurrency-limit count uses the same predicate**, so an expired
    claim releases its slots — otherwise the leak would survive in the one
    place nobody reads. `GET /concurrency-limits/{key}/holders` matches
    (eviction deliberately still reaches an expired holder, whose task is
    RUNNING to every status reader until an event says otherwise).
  - Surfaced as `latest_status_expires_at` on frontier task refs and on
    `ConcurrencyLimitHolder`, as `blocking_status_expires_at` on
    `blocked_by_external` entries, and in the `task_already_running` 409
    detail — so a scheduler can act on evidence instead of inferring death
    from elapsed time.
  - **The migration backfills the claims that are already RUNNING**, as
    `latest_status_at + <default TTL>`. Those rows are the population this
    feature exists to heal — a task RUNNING since three months ago is an
    abandoned claim, not one that "never lapses" — and leaving them null
    would have shipped the fix while excluding every case that motivated
    it. The value is correct in both directions without guessing: a claim
    abandoned long ago backfills to a timestamp already past, so it is
    immediately re-claimable; one genuinely running across the deploy
    backfills to a future timestamp and is untouched, and its next
    `TASK_STARTED` re-stamps it from the caller's TTL anyway. Rows with no
    `latest_status_at` to measure from are left null.
  - Purely additive otherwise: `NULL` means "no expiry known" and is
    treated as a claim that never lapses, exactly as before this column.
    After the backfill that is a much narrower population than it sounds —
    a claim stamped by a server predating the column and not re-started
    since — and those still need an operator (cancel, retry, evict) to
    release. Executor probing is unaffected and remains the better evidence
    where it is available.
- **Build status is now a column, so `GET /builds?status=` is exact for
  every status** ([#208](https://github.com/stardag-dev/stardag/issues/208)).
  It was derived by replaying the build's build-level events on every read,
  which had three consequences, all now gone: every `BuildResponse` cost an
  event scan; the `status` filter could not run in SQL, so it scanned the 500
  most-recently-active candidates, filtered in Python, and returned a `total`
  that was **the matches within that window** while looking like an exact
  count; and anything needing an unbounded "is this build RUNNING?" — the
  stale-build reaper — had to carry a second SQL encoding of the same rule,
  which disagreed with the replay when a terminal event shared a timestamp
  with a start/resume.

  Five denormalised columns on `builds` (`latest_status`,
  `latest_started_at`, `latest_completed_at`,
  `latest_status_triggered_by_user_id`, `latest_is_resumed`) now hold exactly
  what the replay returned, folded in-transaction by every build lifecycle
  path — the same pattern already used for `tasks.latest_*`. Backed by a new
  `(environment_id, latest_status, last_active_at)` index. Migration
  backfills existing builds by replaying their events, so no build changes
  status across the upgrade.

  - `status` filtering is a plain column predicate: exact `COUNT(*)`,
    server-side pagination, no window and no cap, for every status.
  - `status` combined with `idle_for_seconds` **no longer 422s** for
    non-`running` statuses. That restriction existed only because `running`
    was the sole value with a SQL predicate; "failed, and idle for a week" is
    now a real query.
  - **Tie-break:** two build-level events sharing a `created_at` resolve in
    _arrival_ order — the order the server committed them — because the fold
    shares a transaction with the event insert. Equal timestamps mean the
    timestamp lost information the commit order still has, and both previous
    implementations were guessing (the replay arbitrarily, the reaper by
    always reading a tie as "not running"). The reaper is unaffected in
    practice: it still only touches builds that are RUNNING _and_ have been
    silent for at least `idle_for_seconds`.
  - No response field changed shape or meaning.

### UI

- **A Builds view, replacing "Home".** Builds are listed with status,
  duration, last activity and the reactive app that owns them, and can be
  filtered by status, by owning app, and by how long they have been idle.
  The idle filter means _abandoned_, so it implies Running: a finished
  build has no activity by definition, and including terminal builds would
  fill a staleness listing with history sorted oldest-first.

- **Bulk cleanup of abandoned builds**, admin-gated to match the API. Rows
  are selectable per page, and "Clean up idle builds…" sweeps the whole
  environment on the server rather than the visible page. Both paths open a
  confirmation showing a **dry run of the real query** — the same selection
  `POST /builds/bulk-cancel` will act on, including the per-build reasons
  anything was skipped — because a preview that disagrees with the action
  is worse than no preview.

- **A failed build says why it failed.** The scheduling panel explains a build
  while it is _stalled_, and goes quiet the moment it fails — failing skips the
  blocked tasks, and terminal tasks leave the frontier's blocker list. The
  reason the scheduler recorded on its way out (which task, its status and age,
  the owning build, why nothing will move it, and the remedy) is now shown on
  the failed build, in the place the panel's explanation occupied. Untruncated,
  because the remedy is at the end of it.

- **A build that is not progressing now says why.** When nothing is
  actionable and nothing is running, the build view names each blocking
  upstream, its status, how long it has been held, and which build owns it.
  Each carries what happens next, which follows from the blocker's
  **status**: `running` resolves when the claim finishes or expires;
  `cancelled` is a revocation, so this build's next tick resets it and runs
  it; `suspended` resolves as the owning build works through the dynamic
  dependencies it yielded; `failed` and `skipped` are results left to this
  build's `fail_mode`, so re-triggering is the way to reset them. Behind a
  disclosure: the scheduler's own tick trail, with runs of identical ticks
  collapsed ("lease held ×20") and counters that stayed at zero dropped, so
  a stalled build's repetition reads as a diagnosis instead of a log.

- **Claim triage in the task explorer.** Lists the tasks holding an
  execution claim, longest-held first, with the build that owns each one,
  and releases them in bulk. Only tasks actually holding a claim are
  selectable, and every release reads the resulting status back — a task
  that finished between listing and acting is reported as such rather than
  counted as released.

- **Fixed:** unchecked checkboxes rendered as white boxes in dark mode
  (`color-scheme` was never set, so every native control used the light
  theme); durations past a day read as `371h 41m` instead of `15d 11h 41m`;
  dates rendered in the browser's locale order (`8/9/2026` or `9/8/2026`
  depending on the reader) rather than `YYYY-MM-DD`.

### SDK

- **Reactive builds now discover the DAG inside Modal, not on the machine
  that triggers them.** `build_trigger(..., reactive=True)` used to walk
  the whole DAG locally, which meant one target existence check per task
  from the triggering process. For a `modalvol://` target root each of
  those is a Volume API call from outside Modal, and they are rate
  limited: triggering a 127-task DAG from a laptop spent ~64 s almost
  entirely in backoff, and before that was hardened it failed outright
  with `VolumeListFiles rate limit exceeded`.

  The trigger now mints (or resumes) the build, registers the root tasks —
  neither of which touches a target — and spawns a new deployed
  **`bootstrap`** function with the roots passed by value. The bootstrap
  does the walk, the registration, the task-module coverage check and the
  task-store writes _in the container_, where the same volume is a mounted
  filesystem, then arms the build and spawns the first tick. Triggering is
  fast and needs registry credentials only; it performs no target I/O at
  all.

  `bootstrap` is its own function rather than work folded into the first
  tick because the two need different timeouts: a tick is one frontier
  pass (and its timeout derives the per-pass spawn cap), while discovery
  is a single whole-DAG walk paid once per trigger. It defaults to
  `builder_settings` — the same image, secrets and volume mounts as the
  builder, which runs the same discovery for resident builds — and is
  configurable with the new `StardagApp(bootstrap_settings=...)`.

  The ordering that makes ticks safe is preserved and now stated in code:
  the reactive marker (`reactive_app_name`, without which a tick no-ops)
  is written **last**, after discovery and persistence, so no tick can
  ever observe a partially-registered DAG. Failure handling is preserved
  too, on both sides of the spawn — anything that fails once a trigger
  knows the build is `RUNNING` records a terminal `BUILD_FAILED` before
  propagating, including failures inside the bootstrap container and a
  failed first-tick spawn. A re-trigger whose `build_resume` fails is
  deliberately excluded: until that lands the build may still be terminal.

  `BuildTriggerResult.function_call` now carries the bootstrap call for
  reactive triggers (previously the first tick's), which is the honest
  handle: it is what the trigger spawned, and its failure is what means
  the build never started.

  `require_pickle_free=True` is still enforced and still fails loudly —
  now from the bootstrap, where the task store is written: it records a
  terminal `BUILD_FAILED` and re-raises on the bootstrap's Modal call. A
  side benefit of the move: the coverage check now compares your DAG
  against the **deployed** `task_modules` list rather than your local one,
  closing the stale-deploy blind spot it used to carry. The trigger also
  prints a labelled, roots-only advisory before spawning, so the common
  "I never declared my package" case still shows up in your terminal.

  Resident (non-reactive) builds are completely unaffected. Set
  `StardagApp(reactive_discovery="local")` to run the identical bootstrap
  in the triggering process — the previous behaviour — for apps deployed
  before the `bootstrap` function existed, or when the target root is
  reachable from the trigger but not from the Modal app. **Redeploy your
  app** to get the `bootstrap` function.

- **The SDK now identifies its version to the registry.** Every registry
  request carries `X-Stardag-SDK-Version` (plus a descriptive `User-Agent`
  for logs; the server keys on the header, never on the agent string). This
  ships ahead of anything that reads it, because the check only works
  forwards: a server can tell an SDK "you are too old" only if that SDK was
  already announcing itself when it was released, and no later server change
  fixes a silent release retroactively.

  When a registry is configured with a minimum SDK version and this SDK is
  below it, the `426 Upgrade Required` response now raises
  `SDKVersionUnsupportedError` (exported from `stardag`), carrying the
  server's own message — which names both versions and the exact
  `pip install --upgrade` line — plus `sdk_version` and
  `minimum_sdk_version`. CLI commands print that message as written rather
  than a repr. No minimum is configured by default, so nothing changes for
  an up-to-date pair.

- **`stardag builds cleanup` and `stardag builds ticks` say when the
  registry is too old**, instead of reporting the missing endpoint as
  "resource not found" — which read as a bad build id and sent people
  looking for a build that was fine. Both now name the command, the missing
  endpoint and the upgrade; `cleanup` also points at
  `stardag builds cancel <build-id>` as the one-at-a-time fallback. A
  genuine resource-level 404 is unaffected.

- **Reactive builds now have a task-level retry policy.** A reactive tick
  recorded a failed execution and never respawned it, on the reasoning that
  retries are the execution backend's job. They partly are — a backend's
  function-level retries (Modal's `retries=`) cover exceptions raised
  _inside_ the container — but they cannot cover a spawn that failed before
  any container existed, an execution the backend killed (OOM, timeout), a
  preempted worker, or one that died after writing partial output. Under
  `FAIL_FAST`, any one of those ended the whole build.

  `TickConfig.max_attempts` (default **2**, also accepted as a Modal
  `tick_kwarg`) is a budget, per task per build _round_, on how many
  executions the scheduler starts. A failure the tick records is reset to
  pending and picked up on the next pass while the budget allows. The
  budget covers exactly the failures no backend can retry: a failed spawn,
  an execution the backend reports failed, and a task whose execution claim
  lapsed with nothing left to probe. It deliberately does **not** cover a
  task whose object cannot be rehydrated — the same absence on the second
  reading — and it never sees an exception inside a task at all, since the
  worker self-reports that and the task leaves the frontier. Set
  `max_attempts=1` for the previous behaviour.

  A **round** runs from the build's most recent `BUILD_RESUMED` event, which
  makes the recovery path the one you already reach for: **re-triggering the
  build** (`build_trigger(..., build_id=<this build>, reactive=True)`)
  records `BUILD_RESUMED` ahead of its discovery retries, so every task
  starts the new round at zero — optionally with a raised budget via
  `tick_kwargs={"max_attempts": N}`. A **bare** retry (the UI's Retry,
  `stardag tasks retry`, the retry route) does not start a round and does
  not reset anything.

  Exhaustion is loud in both directions. A tick that declines to respawn
  names the task, the attempts spent, the budget and the re-trigger. And on
  a task already at budget, a bare retry succeeds server-side while the
  scheduler still refuses to start it — previously a silent no-op; now the
  tick says exactly that, distinguishes it from a re-trigger, fails the task
  again rather than leaving it pending and inert, and spells out the
  re-trigger that would work. New `TickSummary` counters `retried`,
  `retry_exhausted` and `budget_denied` carry the same facts into the
  persisted per-build summary.

  Resuming a **suspended** task is never budget-gated: a dynamic-dependency
  yield records a fresh start, so gating resumption would cap dynamic
  dependencies rather than retries. Server support is required
  (`attempt_count` on the frontier); against a registry that does not report
  it, no budget can bound a retry loop, so retries stay off and the tick
  says why.

- **Reactive ticks fan out concurrently.** Acting on a frontier was a plain
  `for` loop with awaits inside it: per actionable task, a task-store read,
  an execution-claim acquisition, an executor spawn and a start recording
  the ref — 2–3 registry round-trips plus a spawn, strictly serialised. A
  layer thousands of tasks wide was therefore thousands of sequential HTTP
  calls in one short-lived container, racing a function timeout nothing
  related it to. Each pass now runs those actions with bounded concurrency
  (`TickConfig.max_concurrent_actions`, default 50 — the bound the resident
  engine has always used). Ordering _within_ a task is unchanged: the
  acquiring start still precedes the spawn (a denied task never occupies a
  worker) and the ref-recording start still follows it.

- **A per-tick spawn cap, derived from the tick container's own timeout.**
  The old cap was "however many tasks are actionable", which is unrelated
  to how long the container may live. `TickConfig.max_spawns_per_tick`
  bounds one pass; left unset it is derived as a duration budget — a
  fraction of the tick's wall-clock limit, spread over the in-flight bound.
  The limit is resolved down a ladder: the explicit cap, then
  `TickConfig.tick_timeout_seconds` (which the Modal integration fills in
  automatically from the `timeout` the deployed `tick` function carries,
  falling back to `builder_settings` exactly as function registration
  does), then the executor's `execution_timeout_seconds` as a fallback
  proxy, then a conservative default. Every tick logs its cap and which
  rung produced it. Truncation is logged, never silent, and never a stall:
  the pass acted, so the tick re-evaluates on a fresh frontier immediately
  and takes the next batch. `max_spawns_per_tick` and
  `max_concurrent_actions` are accepted as Modal `tick_kwargs`;
  `tick_timeout_seconds` deliberately is not — it is a deploy-time fact
  about the container, not per-build state.

  The watchdog sweeps every running build sequentially inside one
  container, so it now hands each build a proportional share of that
  container's budget rather than letting the first wide build size its
  fan-out as though it owned the whole timeout.

- **Concurrent DAG discovery in reactive mode.**
  `discover_and_register_aio` walked the DAG with a recursive `await
task.complete_aio()` and no concurrency, while the resident engine did
  the same work 50 at a time. That cost was not paid once per build: this
  walk runs at every reactive trigger _and_ in every worker registering
  dynamically yielded dependencies, i.e. on the hot path of every dynamic
  dependency. It is now bounded-concurrent on the same default
  (`max_concurrent_discover=50`). The completion checks overlap; the
  ordering does not — post-order registration (dependencies before the
  tasks that need them, so the bulk endpoint never creates phantom rows),
  diamond deduplication, `retry_failed` behaviour and all three
  `DiscoveryResult` collections come out identical to the serial walk's,
  element for element.

- **Blocker liveness is now read from the execution claim's expiry, not
  inferred.** A reactive tick decided whether to wait on a RUNNING upstream
  owned by another build from a table over `(in-build, status, owning-build
liveness)` plus a staleness bound on how long the blocker had sat in its
  status — an educated guess, since no build can probe another build's
  executor. The registry now stamps every execution claim with an expiry, so
  the question is answered by a read: a RUNNING blocker whose claim is live
  is waited on; one whose claim has **lapsed** fails the build, with the
  message saying the claim is provably abandoned rather than presumed so.

  What this does **not** change: proving a blocker dead does not make it
  schedulable. A RUNNING task is not runnable whoever holds the lapsed claim,
  so the build still fails — just with certainty about why, and pointing at
  the cancel that releases it. And the collapse applies to RUNNING blockers
  only: a SUSPENDED or PENDING blocker holds no claim and therefore carries
  no expiry, so "will anyone move it?" is still asked of its owning build (an
  abandoned-SUSPENDED upstream remains a real wedge, recovered by
  re-triggering the build that is waiting on it).

- **Every start records a claim TTL derived from the executor's own
  timeout.** For Modal that is the worker function's `timeout` from its
  `FunctionSettings`, plus a grace margin; where no timeout is known the
  registry's default applies. Granting an expiry on every start is what
  makes an abandoned claim heal, but it also means a task outliving its TTL
  could have its claim taken while alive — deriving the TTL from the limit
  the backend itself enforces is what keeps that from being a real risk.
  Setting an explicit `timeout` on long-running Modal workers is therefore
  worth doing.

- **Removed: `TickConfig.stale_running_no_ref_seconds` and
  `ClaimConfig.stale_running_no_ref_seconds`.** Both configured a local
  guess at how long "too long" is, which the claim's own expiry now answers.
  A task RUNNING without an executor ref (a scheduler that died between the
  claiming start and the spawn) is failed when its claim lapses instead of
  after a fixed bound, and a competing claimant recovers a ref-less winner
  on the same evidence. Against a registry that does not report expiry,
  every one of these paths waits rather than failing — a missing expiry
  means "never lapses", not "dead".

- `FrontierTaskRef.latest_status_expires_at` and
  `FrontierExternalBlocker.blocking_status_expires_at` model the new server
  field; `task_start`/`task_start_aio`/`task_start_claim_aio` accept
  `claim_ttl_seconds`; `TaskExecutorABC.execution_timeout_seconds` is the
  new (optional, default `None`) hook an executor implements to expose its
  wall-clock limit. `StartClaimResult.latest_status_at` is replaced by
  `latest_status_expires_at`.

- **`stardag builds show` prints a failed build's reason.**
  `BuildSummary.latest_error_message` models the new server field, and the
  command renders it as **Failure reason** — the most useful row on a failed
  build, since the reactive scheduler's reasons name the blocking task, the
  build that owns it and what to run. Absent on servers predating the field.

- **New `stardag builds` and `stardag tasks` CLI groups.** There was no way
  to list builds, inspect a build's scheduling frontier, cancel a build or
  task, or clean up abandoned state without writing a script against the
  registry API — which is what made a wedged or spuriously-failed build hard
  to diagnose: the failure was visible in logs and in the UI, but "what does
  the scheduler actually think the state is?" required hand-rolled calls.

  ```
  stardag builds list [--status running] [--reactive-app NAME] [--older-than 24h]
  stardag builds show <build-id>
  stardag builds frontier <build-id>
  stardag builds ticks <build-id> [--limit N]
  stardag builds cancel <build-id> [--cascade] [--yes]
  stardag builds cleanup [--older-than 24h] [--build-id ID ...] [--apply] [--yes]
  stardag tasks list [--status running] [--older-than 1h]
  stardag tasks cancel <build-id> <task-id> [--yes]
  stardag tasks retry <build-id> <task-id> [--yes]
  ```

  `builds frontier` is the diagnostic one: besides the actionable/running
  partitions it renders the build's **external blockers** — tasks of this
  build held back by an upstream whose current status _another_ build
  produced — naming the blocking task's namespace/name (not just an id), its
  status, how long it has been in it, the owning build, and what happens next,
  which follows from the status rather than from which build produced it
  (`running` waits on the claim, `cancelled` is reset by the next tick,
  `suspended` waits on the owning build, `failed`/`skipped` need a
  re-trigger). It also states honestly that the registry computes that list
  only for a
  build with nothing actionable and nothing running, so an empty list never
  reads as "no blockers" for a build that is merely progressing.

  `builds cleanup` is the recovery for builds abandoned by a process that
  died: build status is derived from build-level events, so such a build
  stays `RUNNING` forever while holding every execution claim and
  concurrency-limit slot its tasks had. It **defaults to a dry run** — the
  server's own selection, so what you review is what you get — printing the
  builds, the claims that would be released and any per-build skip reasons.
  **`--apply` is the only thing that makes it act**; `-y/--yes` only skips
  the confirmation prompt, so `cleanup -y` on its own is still a dry run.
  Cascade is on by default here, and reactive builds are excluded unless
  asked for.

  `--older-than` accepts `24h` / `90m` / `3d` (one number, one optional unit
  of `s`/`m`/`h`/`d`/`w`; bare numbers are seconds) and converts at the
  boundary to whatever the endpoint takes — a duration for builds, an
  absolute cutoff for tasks. It is applied server-side by the same predicate
  the reaper uses, so `builds list --older-than 24h` and
  `builds cleanup --older-than 24h` agree on what is stale; on `builds list`
  it **implies** `--status running` and may not be combined with any other
  status (idleness only means anything for a build that has not finished —
  a completed one has no activity by definition, and always will). A registry older than the
  CLI silently ignores the filter, so the command detects that and warns on
  stderr that the results are unfiltered rather than quietly filtering the
  page itself (which would under-report exactly the oldest builds).

  The read-only commands — and `cleanup`'s dry run — take **`--json`**, a new
  convention for the CLI: stdout carries exactly one JSON document (the SDK's
  model of the API payload) and every hint, warning and prompt goes to
  stderr, so piping to `jq` is safe.

- **Reactive scheduler ticks now report their `TickSummary` to the
  registry** (`stardag builds ticks <build-id>`). A reactive build is driven
  by many short-lived ticks, each in its own container, so the summary — the
  scheduler's own account of what it did and why — used to reach nobody but
  that container's log, and reconstructing why a build stalled meant reading
  logs across dozens of them. Reporting is strictly best-effort: it sits at
  the end of every tick and can never fail one, change its outcome or mask
  its exception; it tolerates a registry that predates the endpoint (and
  stops retrying a route that 404s); and every outcome except `not_reactive`
  is recorded. A tick that **crashes** is recorded too, under a new
  `"error"` outcome carrying `TickSummary.error_type` and a length-bounded
  `error_message` — the most informative thing a "why did this build stall?"
  query can find — after which the original exception is re-raised
  untouched. Turn reporting off for a deployment with
  `TickConfig(report_tick_summaries=False)` — app-level configuration, like
  the other staleness knobs, not a per-trigger `tick_kwarg`. The summary is
  stored verbatim server-side, so future `TickSummary` fields need no server
  release.

- **The reactive watchdog's build sweep now filters server-side.**
  `build_list_running` passed no `status` and matched on the derived status
  in Python, so an environment holding more non-running builds than the
  sweep's page budget could starve it of the running builds it exists to
  find — silently disabling the safety net exactly when a backlog makes it
  necessary. Same ordering, page budget and truncation warning as before.

- **New registry-client methods** for the operational surface, all on
  `RegistryABC` (with safe defaults) and `APIRegistry`: `build_list`,
  `build_get_summary`, `build_bulk_cancel`, `build_report_tick_summary[_aio]`,
  `build_list_tick_summaries`, `task_list`, and id-addressed
  `task_cancel_by_id[_aio]` / `task_retry_by_id[_aio]` (operator tooling only
  ever has the id, and rehydrating a task object to cancel it would fail for
  exactly the abandoned tasks that most need cancelling). `build_cancel` gains
  a `cascade` keyword and now returns the cancelled build plus the claims the
  cascade released, or `None` for backends that don't report it — the same
  optional-return convention as `task_register_bulk`. New response models
  `BuildSummary`, `BuildListPage`, `BuildCancelResult`, `BulkCancelResult`,
  `BulkCancelBuildRef`, `TaskSummary`, `TaskListPage` and `TickSummaryRecord`
  are exported from `stardag.registry` and ignore unknown response fields.
  `build_list` takes the server's `status`, `reactive_app_name` and
  `idle_for_seconds` filters.

- **`StardagApp(task_modules=[...])`: declare the modules whose import
  registers your task classes, so reactive scheduler ticks can rebuild
  tasks from registry data instead of pickles.** A tick reconstructs task
  objects from the registry's stored payload, which resolves a class
  through the polymorphic registry — populated only as a side effect of
  importing the defining module. Without a declaration, whatever a tick
  container happens to import is arbitrary, so the build task store's
  pickles were load-bearing (and needed target-root write access at
  trigger time, and were invalidated by every redeploy).

  Patterns are exact modules (`"my_pkg.tasks.ingest"`) or trailing
  recursive wildcards (`"my_pkg.tasks.*"`); the default infers the root
  package of the module defining the app, and `[]` opts out. They are
  expanded to a concrete module list at deploy time (without importing
  submodules) and baked into the deployed tick, so **adding or moving task
  classes requires a redeploy**; `stardag modal deploy` reports the
  expansion (`--no-check-task-modules` skips the warn-only local import
  check). Task modules are imported in every tick container, so keep heavy
  runtime dependencies inside `run()` rather than at module scope.

  With the declaration in place, a reactive trigger writes **no pickle**
  for any task whose class is covered and whose payload round-trips to the
  same task id — a fully covered build needs no target-root write access
  at all. Everything else keeps its pickle exactly as before, including
  `AliasTask` payloads (pickled `loads_type`, never auto-unpickled from
  registry data by design) and non-importable classes. The trigger warns
  about classes the patterns don't cover, naming the pattern to add;
  `require_pickle_free=True` turns that fallback into a hard error.

  Skipping pickles requires declaring `task_modules` explicitly; the
  inferred default only drives the coverage warning. Upgrading stardag
  therefore changes nothing on its own — a newer SDK triggering against an
  app deployed by an older one still writes pickles, because eliding them
  would depend on a baked-in module list that deployment does not have.
  Resident (non-reactive) builds are unaffected either way, and
  `task_modules=[]` behaves exactly as before. **Redeploy the app whenever
  you change `task_modules`**, before triggering.

- **Fixed: a reactive build no longer fails because another build is
  running one of its upstreams.** Task state is per environment, so an
  upstream some other build left RUNNING gates this build's tasks — while
  contributing nothing to the `running` count and status counts a tick
  sees, which are scoped to this build. That shape read as "nothing
  runnable, nothing running, so this build is dead", and the build was
  failed within seconds of triggering with an error naming only status
  counts. Common whenever DAGs overlap, and worst when the blocker was a
  dynamic dependency registered under an earlier build — which plan closure
  (see the Registry API section) now pulls into the new build's plan instead
  of leaving it gating a build that could never schedule it.

  A tick now reads the frontier's blocking upstreams and asks, for each,
  whether anyone is going to move it. A blocker **another build is
  executing** is waited out (like a busy concurrency-limit slot — its
  completion wakes this scheduler), and so is one **a still-live build has
  yet to schedule**: that build is going to run it, and failing here would
  only trade one spurious failure for another. A blocker **no live build is
  going to run** — its owning build has gone terminal, no build owns its
  status, or that status could not be resolved — fails the build
  immediately, naming the task, its namespace/name, its status, how long it
  has been in it, the build that owns it, why that owner will not move it,
  and the one remedy there is: **re-trigger this build**, which resets the
  blocker (it is in this build's plan) and runs it here.

  **Which of those questions gets asked is decided by the blocker's status,
  not by which build owns it.** A build's plan is closed under the dependency
  relation, so a gating upstream is this build's own task; deferring to
  whichever build last touched it is what turned one build's fail-fast into
  every overlapping build's failure. A `CANCELLED` blocker is a revocation of
  permission to run, not a verdict on the task, and permission is not
  build-scoped — the tick **resets it and runs it**, bounded by
  `max_attempts`. `FAILED` and `SKIPPED` are _results_: `fail_mode` owns them
  (FAIL_FAST has already failed the build on the same count; CONTINUE means
  "finish what you can, then fail"), so a tick names them in the failure and
  changes nothing. A `SUSPENDED` blocker is waited on while its owning build
  lives, because resetting it would redo all of the task's pre-yield work
  while that build is legitimately progressing the children it yielded. At
  **trigger** time the whole retryable set is reset, `RUNNING` excepted — the
  asymmetry is deliberate: at trigger you asked, mid-flight nobody did.

  Waits are bounded by evidence rather than by a timer. For a RUNNING blocker
  it is the claim's expiry; for a SUSPENDED or PENDING one — which holds no
  claim, so has no expiry to read — it is the owning build going terminal,
  and a build gone silent without transitioning is reaped server-side.
  `TickSummary` gains `external_blockers`, `external_blockers_waited`,
  `external_blockers_fatal` and `in_build_blockers_reset`, and `BuildInfo`
  gains `status` (the build's derived status, `None` when a server or custom
  registry does not report it). Owner liveness is resolved only when a build
  actually looks stalled, only for the blockers whose status needs it, and
  once per owning build per pass, so a healthy build issues no extra requests
  however often it polls. Requires a stardag-api version matching this SDK;
  against an older server the blocker list is always empty and terminal
  detection behaves exactly as before.

- **Fixed: re-triggering a reactive build now recovers tasks left
  `SUSPENDED`.** A task that suspended for dynamic dependencies and was
  then abandoned (its orchestrator died, or the build was cancelled) was
  permanently unschedulable: the re-trigger's retry pass skipped it, and
  the only escape was to cancel it purely to reach a status that _was_
  retryable and then retry. `suspended` joins failed/cancelled/skipped in
  the set a re-trigger resets to pending. Safe because a suspended task has
  no live execution — the suspension means the execution yielded and
  returned — so nothing can be orphaned. Workers registering dynamically
  yielded dependencies do not retry at all and are unaffected. `running`
  remains deliberately non-retryable: it holds a live execution claim, and
  releasing that claim is cancellation, not retry.

- **Fixed: a task stuck RUNNING with no executor ref is now actually
  recovered.** A scheduler that died between the claiming start and the spawn
  leaves a task RUNNING that no worker will ever report on.
  `stale_running_no_ref_seconds` was supposed to bound that, but it was
  measured against `FrontierTaskRef.latest_status_at`, which the server
  declared and never populated — so it serialised as `null` on every ref and
  the guard was dead code for its whole life. The frontier now populates the
  field, and the bound it feeds has been replaced outright by the claim's
  expiry (see the removal above), which is evidence rather than a guess and
  needs no tuning for long ref-less tasks.

### Fixed

- **The reactive watchdog now asks the registry only for the RUNNING builds
  its own app owns** (`GET /builds?status=running&reactive_app_name=...`,
  filters the API has supported since the reactive-metadata release but the
  SDK never used). Previously each sweep paged the whole build listing,
  filtered the derived status client-side, and then spent a full `tick`
  invocation on every RUNNING build in the environment just to discover
  most were not reactive. Worse, the sweep's per-period cap was consumed by
  those irrelevant builds: an environment holding more stale RUNNING builds
  than the cap could stop reaching genuine reactive builds entirely, and
  silently — disabling the safety net exactly when it was needed. The
  truncation warning now says how many _reactive_ builds owned by _which
  app_ it truncated, and what to do about it. `build_list_running` gained an
  optional `reactive_app_name` argument; the client-side status re-check is
  retained so a server predating the filters degrades to a wider listing
  rather than to ticking terminal builds. Note that scoping removes the
  incidental cross-app coverage a sweep used to provide: a build owned by
  an app deployed without a watchdog is no longer swept by another app's
  watchdog. ([#208](https://github.com/stardag-dev/stardag/issues/208) A3)
- **A reactive trigger that fails part-way no longer leaves a build stuck
  in RUNNING forever.** `build_trigger(reactive=True)` mints the build
  before running discovery and persisting the task store, and a build's
  status is derived from its events — so a failure in between (most often a
  target-root permission or storage error on the task-store write) left a
  RUNNING build that nothing would ever terminate and that carried no
  reactive owner, so it was invisible to its own app's sweep and pure
  overhead for every other one. All post-mint trigger work is now wrapped:
  any failure emits a terminal `BUILD_FAILED` naming the stage that failed
  before the original exception propagates (a failure to record that event
  is logged, never allowed to mask the root cause).
  ([#208](https://github.com/stardag-dev/stardag/issues/208) A4)

## [0.17.0] — 2026-08-06

### SDK

- **The published distribution now ships a [PEP 561](https://peps.python.org/pep-0561/)
  `py.typed` marker** (plus the `Typing :: Typed` classifier), so type
  checkers use stardag's inline annotations instead of discarding them. No
  API change — the minor bump reflects the downstream effect below.

  **Downstream:** mypy previously skipped the package entirely —
  `module is installed, but missing library stubs or py.typed marker` —
  and treated every stardag symbol as `Any`, so genuine mismatches in
  consumer code went unreported. They are now flagged, which means a
  previously green mypy run can surface new — real — errors after
  upgrading. Two workarounds also go stale and should be removed:
  `# type: ignore[import-untyped]` comments on stardag imports, and any
  `ignore_missing_imports` override for `stardag.*` (`warn_unused_ignores`
  will report them as unused). Pyright already resolved stardag's types
  from the installed package; the only change there is that strict mode
  no longer emits `reportMissingTypeStubs`.

## [0.16.1] — 2026-07-17

### Fixed

- **`stardag self-host` now creates a shared workspace named after your
  Modal workspace by default.** Modal's token lookup returns an empty
  `workspace_name` for _both_ personal and team/org workspaces (only
  `username` differs), so the CLI could no longer tell them apart and
  misclassified org workspaces as "personal": it never set
  `AUTH_PRIMARY_WORKSPACE_NAME`, and the server bootstrapped `main` in the
  admin's personal workspace instead of a shared one. The primary workspace
  is now an explicit, well-defaulted choice keyed off the Modal `username`
  (always present): `up`/`connect` default to creating a **shared** Stardag
  workspace named after the Modal workspace (with the admin as owner) and
  wire the target root, API key, and local profile to _that_ workspace's
  `main` environment. Use `--no-primary-workspace` for solo/individual use
  (personal workspace), or `--primary-workspace NAME` for an explicit name.
- **`stardag self-host connect`/`up` no longer overwrite an existing
  `stardag-api-key` Modal secret without confirmation.** Pushing the secret
  into an execution Modal environment that already had one could silently
  repoint all DAG execution in that environment (e.g. an existing
  cloud/app.stardag.com setup) to the self-hosted registry. When the secret
  already exists the CLI now warns and requires a typed confirmation phrase
  interactively, or the explicit `--overwrite-api-key-secret` flag under
  `--yes`; otherwise it leaves the secret untouched and completes the rest
  of the setup. (The standalone `stardag modal stardag-api-key create`,
  whose purpose is to (re)push the secret, now prints a warning when it
  replaces an existing one.)

## [0.16.0] — 2026-07-17

### SDK

- Default prebuilt server image bumped to `server-v0.1.1` (rebuilt from
  the v0.15.0+ line, so a prebuilt `self-host up` now serves the UI with
  the corrected version footer). `DEFAULT_SERVER_VERSION = "0.1.1"`.
- **`stardag self-host` prebuilt deploys no longer require a matching
  client Python
  ([#196](https://github.com/stardag-dev/stardag/issues/196)).** The
  prebuilt-image path now defines the Modal `web`/`migrate` functions by
  reference (`serialized=False`) against a module-level entry point that
  Modal imports inside the server image, instead of cloudpickling closures
  with the client interpreter. Because nothing is serialized, the CLI runs
  under any supported Python (≥ 3.10) — the previous `uvx --python 3.12 …`
  requirement (and its fail-fast check) is gone. `--from-source` is
  unchanged (still serialized closures with a client-matched image Python).

## [0.15.0] — 2026-07-17

### SDK

- **Self-host the Stardag service on Modal with one command
  ([#187](https://github.com/stardag-dev/stardag/issues/187)).** New
  `stardag self-host` CLI (`up`/`upgrade`/`status`/`destroy`, extra:
  `stardag[selfhost]`): provisions a Postgres database on Neon from an
  API key (or bring-your-own via `--database-url`), applies migrations,
  and deploys the Registry API + web UI as a single Modal web endpoint.
  Deploys a prebuilt public server image by default
  (`--server-version`); `--from-source` builds from a repo checkout
  (the UI compiles inside the Modal image build — no local Node/Docker
  required). See the "Self-host on Modal" guide.
- **`stardag self-host up` completes the whole setup** (new `connect`
  subcommand re-runs it idempotently): the server app (`server`) + its
  secrets are isolated in a dedicated Modal environment (`stardag-host`,
  flag `--server-modal-env`); a primary Stardag workspace is created
  mirroring a shared Modal workspace's name (`--primary-workspace` /
  `--no-primary-workspace`; personal Modal accounts use the personal
  workspace) with a `main` environment; an API key is minted and pushed
  as the Modal secret `stardag-api-key` into the DAG-execution Modal
  environment (`--execution-modal-env`); a default target root
  `modalvol://stardag-targets-<workspace-slug>-<environment-slug>/default`
  (a dedicated Modal volume per workspace + environment) is registered
  (`--target-root`/`--no-target-root`); and a local SDK registry +
  profile (`selfhosted`) are written. In OIDC auth mode, `connect` runs
  the browser login first and provisions via the API.
- **`stardag auth login` supports local-auth registries**: when the
  registry reports `auth_mode=local` it prompts for email/password and
  stores the session token; the existing token-refresh chain uses it
  transparently.

### Registry API

- **Local authentication mode** (`AUTH_MODE=local`): email/password
  accounts managed by the API itself — no external identity provider.
  New endpoints `POST /auth/login`, `POST /auth/register` (disabled by
  default), `POST /auth/change-password`; user-scoped session tokens
  (`token_use=session`) accepted by `/auth/exchange` and bootstrap
  endpoints; bcrypt hashing with timing-uniform verification; login
  rate limiting; idempotent bootstrap-admin provisioning at startup
  (`AUTH_BOOTSTRAP_ADMIN_EMAIL`/`_PASSWORD`). OIDC mode (default) is
  unchanged.
- **Primary workspace bootstrap** (local auth mode): startup idempotently
  ensures a shared workspace named `AUTH_PRIMARY_WORKSPACE_NAME` (bootstrap
  admin as owner) and an `AUTH_PRIMARY_WORKSPACE_ENVIRONMENT` environment
  (default `main`; empty disables) — in the named workspace, or in the
  bootstrap admin's personal workspace when no name is set.
- `GET /auth/config` now serves the full client auth configuration
  (auth mode, issuer, UI client id, Cognito domain, registration flag)
  so UIs and CLIs can be configured at runtime.
- `GET /api/v1/version` reports the server version
  (`STARDAG_SERVER_VERSION`, stamped by the release pipeline) and the
  installed API package version.
- Serverless/pooler support: `STARDAG_API_DATABASE_URL_DIRECT`
  (migrations bypass transaction-mode poolers) and
  `STARDAG_API_DATABASE_POOLER_COMPAT` (asyncpg prepared-statement
  settings for PgBouncer-style poolers).

### UI

- Auth/API configuration resolves at runtime from the API — a prebuilt
  UI bundle works against any IdP or auth mode without rebuilding
  (build-time `VITE_*` values still take precedence when set).
- Local-auth mode: sign-in/registration page and a "Change password"
  action in the user menu.
- The Settings page footer shows the server version.

### Deployment

- **Server release pipeline**: git tags `server-vX.Y.Z` publish the
  combined server image `ghcr.io/stardag-dev/stardag-server:X.Y.Z`
  (Registry API + web UI + migrations, one joint version) and attach
  the built UI dist to the GitHub release. `app/server.Dockerfile` is
  the single image definition; `scripts/server-version.sh` derives
  truthful versions for non-release builds (`X.Y.Z+N.g<sha>`).
- AWS CDK: optional `apiImageUri` to run the public server image
  instead of building to ECR; `deploy-ui.sh --release` deploys the
  prebuilt UI dist; opt-in CloudFront same-origin `/api/*` proxy
  (`uiApiProxy=true`); ECR pull-through-cache recipe documented.

## [0.14.0] — 2026-07-16

### SDK

- **Exactly-once task execution by default (execution claims,
  [#185](https://github.com/stardag-dev/stardag/issues/185)).** Task
  starts now carry an atomic per-task _claim_ wherever a registry with
  claim support is configured and the execution is probeable (detached
  Modal executions — resident and reactive): a start racing an
  already-RUNNING task is denied with the running execution's ref echoed,
  and the loser re-attaches to the winner instead of spawning a duplicate
  (or self-heals an existing completion, records a provably dead winner
  and re-claims, or waits for a ref-less winner with backoff —
  `ClaimConfig`). This closes the cross-build both-see-PENDING race that
  previously could run one task in two workers. Control via
  `build(..., claim=None|True|False)` and `TickConfig.claim`; older
  servers/custom registries degrade gracefully. Custom arbitration
  backends implement `RegistryABC.task_start_claim_aio`
  (`StartClaimResult`).
- **`GlobalLockConfig` is deprecated** in favor of claims (a
  `DeprecationWarning` is emitted when enabled). The lock remains
  functional for executions without probeable liveness; the engine now
  **renews held locks in the background**, fixing the 60s-TTL expiry
  under long-running tasks. The `GlobalConcurrencyLockManager` protocol
  itself is unchanged (it backs the reactive scheduler lease and remains
  the registry-less escape hatch).

### Registry API

- `POST .../tasks/{id}/start` accepts `claim=true`: atomically deny with
  409 `task_already_running` (echoing `executor`/`executor_ref`) or
  `task_already_completed` inside the FOR-UPDATE start transaction. A
  denied claim records nothing — including no concurrency-limit slots
  (all-or-nothing with `enforce_limits`).

## [0.13.0] — 2026-07-16

### SDK

- **Strict (bare concrete) polymorphic fields now also reject subclass data on
  the deserialize path.** v0.12.0 rejected a subclass _instance_ assigned to a
  strict field; it now also rejects _serialized data_ — an input dict whose
  `__namespace`/`__name` discriminator resolves to a subclass of the declared
  strict type — instead of silently coercing it into the base type (dropping the
  subclass's parameters). Plain dicts without a discriminator are unaffected
  (validated as the exact strict type). **Note:** loading data that was already
  lossily truncated into a strict field by a pre-0.12.0 version now raises
  `StrictPolymorphicTypeError` rather than loading the degraded base type — the
  correct "fail loud on corrupt data" behavior; switch the field to
  `SubClass[...]` if it should accept subclasses.

## UI Only — 2026-07-15

### UI

- **Task detail "Execution" section: minimal Modal call-ref line.** Shows the
  function-call id (`fc-…`) as a link to the Modal call page, with a button
  that copies the id itself; the per-level app/function/environment deep
  links now live in the collapsible "More details" table (each value linked,
  with a copy-the-value button).

## [0.12.0] — 2026-07-15

### SDK

- **Bare abstract task-typed fields are now rejected at class-definition time.**
  A field annotated directly with an abstract polymorphic base (e.g.
  `child: BaseTask`, `deps: list[Task[int]]`) rather than wrapping it in
  `SubClass[...]` / `TaskLoads[...]` used to serialize by silently dropping
  every subclass-specific parameter and then crash on load (the payload tried
  to instantiate the abstract base directly). Such annotations now raise
  `NakedPolymorphicFieldError` as soon as the class is defined, with a message
  pointing at the correct polymorphic form.

- **Bare _concrete_ task-typed fields are now strict (exact-type) at
  validation time.** A field like `child: MyTask` (a concrete base, without
  `SubClass[...]`) means exactly `MyTask`. Passing a _subclass_ instance
  (`child=ChildOfMyTask(...)`) previously succeeded but silently dropped the
  subclass's extra parameters on serialization — and, because task identity is
  derived from the serialized form, distinct subclass values collapsed to the
  same task id. This now raises `StrictPolymorphicTypeError` at construction,
  directing you to `SubClass[MyTask]` if you intend to accept subclasses.
  Passing an exact-type instance is unaffected.

## [0.11.0] — 2026-07-15

### SDK

- **Reactive build metadata moved from the target root to the registry.**
  The reactive marker, owning app name, and `tick_kwargs` used to be stored
  in a `meta.json` on the default target root; they now live in the
  registry (the build's `reactive_app_name` + `reactive_tick_kwargs`,
  surfaced on the build frontier the tick already fetches, and on the
  lighter `GET /builds/{id}` the pre-lease gate now uses). The per-build
  task store is now pickle-only (task _objects_ still live on the target
  root). Because the registry is mutable — unlike a possibly-immutable
  target root — **a re-trigger may now update `tick_kwargs`** (previously
  fixed at first trigger in 0.10.1); a _bare_ re-trigger (no explicit
  `tick_kwargs`) preserves the stored config. Reactive scheduling now also
  requires a registry server new enough to support the reactive-meta
  endpoint; an older server fails the reactive trigger clearly (matching the
  existing frontier/notify version contract) rather than degrading silently.

  **⚠️ Upgrade note:** reactive builds already in flight when you upgrade
  across this release are **not** migrated — the tick now reads the marker
  from the registry only, and pre-upgrade builds have no registry marker, so
  their ticks no-op silently. **Re-trigger any in-flight reactive build**
  (`build_trigger(..., build_id=<id>, reactive=True)`) after upgrading. See
  RELEASE_NOTES.md.

- **Modal executor metadata now records the app id (`app_id`, `ap-…`) and
  worker function id (`function_id`, `fu-…`).** These ride the existing
  executor-metadata channel (base metadata + the worker env-override
  propagation, read back by the worker lifecycle reporter) so a worker's
  self-reported start carries them too. They let the UI build stable
  dashboard deep links in the app-id URL form, which keeps resolving after
  an app version is stopped or redeployed (the deployed-app-name form does
  not). Best-effort throughout: on any error the key is simply omitted rather
  than raised, so resolution never fails a task start. The two lookups sit on
  the critical path before `spawn`, so they can add latency — but each is
  bounded by a short (3 s) timeout, so a slow or hung Modal API cannot stall
  a start beyond that cap. Resolved values (including a resolved-but-missing
  id) are cached per process, so a failing lookup is not re-paid on every
  start.
- **CLI: `stardag concurrency-limits` command group for managing named
  concurrency limits.** Wraps the registry's concurrency-limit endpoints for
  the active profile / environment (override with `-p/--stardag-profile` and
  `-e/--stardag-env`). Subcommands: `list` (with optional `--holders` counts);
  `set` to upsert a limit (`stardag concurrency-limits set <key> <max_concurrent>`);
  `delete <key>` (`--yes` to skip confirmation); `holders <key>` (RUNNING slot
  holders, oldest first); and `evict` to free leaked slots
  (`stardag concurrency-limits evict <key> <task_id>`). Replaces the need for an
  ad-hoc script to `PUT /api/v1/concurrency-limits/{key}`. Backed by new
  `APIRegistry` `concurrency_limit_{list,set,delete,holders,evict}` methods.

### Registry API

- **Reactive-scheduling metadata on builds.** Two new nullable columns:
  `builds.reactive_app_name` (indexed `String` — the owning app + marker;
  NULL = not reactively scheduled, so presence
  (`reactive_app_name IS NOT NULL`) is the marker) and
  `builds.reactive_tick_kwargs` (JSONB — the SDK-owned `TickConfig` kwargs).
  Set via a `PUT /api/v1/builds/{id}/reactive-meta` upsert endpoint
  (env-scoped, rate-limited); `tick_kwargs` is only updated when provided,
  so a bare re-trigger preserves it. Both fields are exposed on the build
  response and the build frontier so a reactive scheduler tick reads
  marker/owner/config in the call it already makes. `GET /api/v1/builds`
  gains `reactive_app_name` and `status` filters (e.g.
  `?reactive_app_name=<app>&status=running`) so "RUNNING reactive builds
  owned by app X" — the watchdog's real question — is a server-side query.
  Additive/nullable migration (instant).

### UI

- **Stable, stop/redeploy-proof Modal function-call deep links.** The task
  detail and concurrency-holder "View on Modal" links previously used a
  query-param form that didn't resolve. They now build the app-id URL
  (`.../apps/{workspace}/{env}/{app_id}?activeTab=functions&functionId=…&functionSection=calls&fcId=…`)
  when the newly captured `app_id`/`function_id` metadata is present,
  falling back to the deployed-app-name form and then the plain app-page
  link. The app-page fallback itself prefers the stable app-id form
  (`.../apps/{workspace}/{env}/{app_id}`) whenever `app_id` is available —
  so metadata with an `app_id` but no `function_id` still degrades to a
  stop/redeploy-proof link rather than the deployed-name page. Reads the
  new metadata defensively, so older data without the ids still gets a
  working, non-dead link. Pairs with the SDK change that records
  `app_id`/`function_id` in the executor metadata.
- **Task detail: "more details" block for Modal identifiers.** The
  Execution section now has a collapsible list of every captured Modal
  identifier (kind, app/function names, workspace, environment, app id,
  function id, and the function-call ref), each click-to-copy, so a
  reference can be reconstructed by hand if the dashboard URL format
  drifts. Only present fields render, and the block is gated to Modal
  executions — it never surfaces its Modal-labeled fields for an
  explicitly non-modal executor kind.
- **Sidebar: shortened the "Concurrency Limits" nav item to "Concurrency"
  and made every nav label left-aligned and single-line (truncating with
  an ellipsis instead of wrapping and centering).** Each item also carries
  a `title` tooltip with its full label, so a truncated label stays
  readable on hover.

## [0.10.2] — 2026-07-14

### SDK

- **`StardagApp(stardag_api_key_secret=...)`: cleaner registry-credential
  handling.** A single, explicitly named secret (default
  `"stardag-api-key"`, the name `stardag modal stardag-api-key create`
  uses) is injected into every function (build, workers, tick, watchdog) —
  all of which talk to the registry. Accepts a `modal.Secret`, a name
  (`str`, resolved lazily), or `None` to disable. A by-name secret that
  doesn't exist raises a clear error at `finalize()`. **This replaces the
  0.10.1 behavior of propagating _all_ builder-declared secrets to the
  workers/tick** — per-function `secrets` are now function-local again;
  only the api-key secret is shared. Declare the registry key via this
  argument (or rely on the default) rather than putting it in
  `builder_settings.secrets`.
- **Fix: Modal workspace now resolves and populates executor metadata /
  the app-level UI dashboard link.** The workspace was resolved from the
  Modal token, which only exists in the local triggering/deploy process —
  inside a Modal container (where task-level metadata is produced) the
  lookup returned nothing, so the UI showed a blank workspace. Two fixes:
  the token lookup now falls back to the account `username` (the
  `WorkspaceNameLookupResponse.workspace_name` is empty for personal
  workspaces), and `finalize()` resolves the workspace at deploy time and
  bakes it into every function's env (`STARDAG_MODAL_WORKSPACE`), which the
  in-container resolver reads first. (The per-function-call deep-link URL
  format is a separate UI fix, tracked as a follow-up.)
- Completed the `StardagApp.__init__` docstring (previously several
  arguments were only described in inline comments).

## [0.10.1] — 2026-07-14

### SDK

- **Fix: reactive Modal scheduling crashed in fresh containers.** A
  `resource_provider` (e.g. `registry_provider`) captured by a
  `serialized=True` Modal function is cloudpickled by value; its unset
  sentinel is a bare `object()` whose identity does not survive pickling,
  so a deserialized provider returned that bare `object()` from `get()`.
  In a deployed app this crashed the reactive scheduler `tick` and the
  scheduled `tick_watchdog` (which runs cold, with no build) with
  `AttributeError: 'object' object has no attribute 'build_list_running'`.
  Providers now serialize without their live resource and re-initialize
  lazily in the new process (`ResourceProvider.__getstate__`/`__setstate__`).
- **`StardagApp` propagates the builder's secrets to workers and the
  tick/watchdog.** Since worker-side lifecycle reporting, every deployed
  function talks to the registry, so all of them need registry
  credentials — but the secret is naturally declared only on the builder.
  `finalize()` now applies the builder's declared secrets to the worker
  functions and the tick/watchdog, de-duplicated by name (a function that
  also declares the same secret still gets it once). Previously workers
  `401`ed on their self-reported lifecycle events unless the secret was
  repeated on every worker.
- **Fix: re-triggering a reactive build crashed on an immutable/no-overwrite
  target root.** The per-build task store rewrote its `meta.json` on every
  re-trigger (add-roots / retry), but a target root may refuse overwrites
  (Modal volumes raise; an immutable/object-locked S3 root would too), so
  the re-trigger crashed with `FileExistsError`. The store is now
  write-once: the reactive marker is written only at the first trigger, and
  task pickles are skipped if already present. Build roots are tracked
  solely in the registry (the scheduler reads them from the frontier), so a
  re-trigger no longer mutates the store. Note: `tick_kwargs` are fixed at
  first trigger until reactive build metadata moves to the registry.

## [0.10.0] — 2026-07-13

Modal as a first-class execution layer. A large, fully backward-compatible
feature release: restart-safe build triggering, detached task execution
with re-attach, worker-side lifecycle reporting, reactive (tick-based)
scheduling (experimental), registry-backed named concurrency limits,
pickle-free task rehydration, and executor metadata surfaced as Modal
dashboard deep links in the UI, plus a concurrency-limits admin surface.
See
[RELEASE_NOTES.md](RELEASE_NOTES.md#v0100--modal-as-a-first-class-execution-layer)
for the SDK-user overview and upgrade notes.

### SDK

- **`stardag/integration/modal`**: New `StardagApp.build_trigger()` — triggers
  a build with the registry build id minted at the trigger point and passed to
  the Modal build function as `resume_build_id`. Restarts of the build
  function (Modal retries after preemption, or re-triggering with the returned
  `build_id`) resume the same build instead of creating a new one:
  already-completed task outputs are detected during discovery and skipped.
  Returns a `BuildTriggerResult(build_id, function_call)`. Requires registry
  credentials in the calling process; `build_spawn` remains available for
  Modal-credentials-only triggering.
  ([#154](https://github.com/stardag-dev/stardag/pull/154))
- **`stardag/build` + `stardag/integration/modal`: detached task execution
  (restart-safe long-running tasks).** `ModalTaskExecutor` now spawns worker
  invocations as detached Modal function calls by default instead of holding
  blocking `remote` calls: the function call id is recorded with the
  TASK_STARTED event, and a build that is restarted (Modal retry after
  preemption, or a `build_trigger` re-trigger with the same build id)
  **re-attaches to still-running workers instead of re-executing them**. A
  task's execution now survives orchestrator crashes. FAIL_FAST and user
  cancellation explicitly cancel the tracked function calls (previously,
  workers of a dead build kept running). Opt out with
  `ModalTaskExecutor(detached=False)` / `Builder(detached=False)`.
  Generic executor surface: `TaskExecutorABC.supports_detached()` /
  `submit_detached()` / `reattach()` + `DetachedHandle`, so other execution
  backends can implement the same semantics; `RoutedTaskExecutor` forwards.
  ([#155](https://github.com/stardag-dev/stardag/pull/155))
- **`stardag/registry`**: `task_start[_aio]` accepts optional
  `executor`/`executor_ref`; `task_register_bulk[_aio]` returns per-task
  `RegisteredTaskInfo` (current global status + executor ref) used by the
  build engine for re-attach. Custom `RegistryABC` implementations with the
  old signatures keep working (refs are dropped gracefully).
  ([#155](https://github.com/stardag-dev/stardag/pull/155))
- **`stardag/integration/modal`: worker-side lifecycle reporting.** The
  default `Runner` now reports the task's lifecycle from inside the worker —
  TASK_STARTED (carrying the worker's own function call id as executor ref),
  TASK_COMPLETED + artifact upload, TASK_SUSPENDED (dynamic deps), and
  TASK_FAILED — whenever the executor forwarded a build id (via the
  `STARDAG_BUILD_ID` env override; no worker signature change, older
  deployed workers are unaffected) and the container has registry
  credentials. Events therefore land even if the build orchestrator dies
  mid-task, and each re-invocation records a fresh re-attachable ref. The
  build engine suppresses its own completed/suspended/resumed reporting for
  such tasks (`TaskExecutorABC.reports_lifecycle` seam), keeping started
  (immediate detached-spawn re-attachability) and failed (fallback for
  workers that die before reporting) — duplicate events are tolerated by
  the event-sourced status derivation. Opt out with
  `ModalTaskExecutor(worker_reports_lifecycle=False)` (required when driving
  an app deployed with an older stardag from a newer local SDK) or
  `Runner(report_lifecycle=False)`. New `stardag.build.get_current_build_id()`
  exposes the ambient build id inside `build[_aio]()`.
  ([#161](https://github.com/stardag-dev/stardag/pull/161))
- **Reactive (tick-based) build scheduling for Modal — experimental.** A
  build can now run with **no resident orchestrator**:
  `StardagApp.build_trigger(tasks, reactive=True)` runs discovery at the
  trigger, persists the task objects to a per-build task store under the
  default target root, and short-lived, idempotent scheduler **ticks**
  (spawned by the trigger, by workers finishing tasks, and by an optional
  periodic watchdog) drive the build: each tick fetches the build's
  scheduling frontier from the registry, spawns ready tasks as detached
  Modal function calls, probes running refs (leaving live ones alone,
  self-healing completions from target existence, recording failures),
  handles terminal states (completed / failed / externally cancelled —
  cancelling running function calls), then lingers briefly on the build's
  wake-up flag and exits when quiet. Long-running builds therefore cost no
  orchestrator container time while tasks execute, and there is no
  orchestrator to crash. Ticks are single-flighted per build via a
  scheduler lease on the existing distributed-lock service. New public
  surface: `stardag.build.run_tick_aio` / `TickConfig` / `TickSummary` /
  `BuildTaskStore` / `discover_and_register_aio`,
  `TaskExecutorABC.detached_status()` / `cancel_detached()` /
  `DetachedExecutionStatus`,
  `StardagApp(watchdog_period_minutes=..., tick_settings=...)`. Current
  limitations (documented in
  `stardag/build/_reactive.py`): requires a registry; the global
  concurrency lock and build-local `ConcurrencyConfig` limits are not
  applied by ticks — registry-backed named limits are (see the entries
  below). ([#157](https://github.com/stardag-dev/stardag/pull/157))
- **Registry-backed named concurrency limits (reactive scheduling).**
  Environment-level named limits (`PUT /api/v1/concurrency-limits/{key}`) cap how
  many tasks tagged with a key may run concurrently — **across builds**, which
  build-local `ConcurrencyConfig` semaphores never could. A task
  occupies a slot simply by being RUNNING with the key recorded at start
  (no leases/TTLs: status liveness is already maintained by worker
  reporting and tick self-healing). Acquisition is atomic in the
  task-start transaction (`/start?limit_key=...&enforce_limits=true`,
  409 when at capacity, all-or-nothing across keys). Reactive ticks
  acquire before spawning via a key selector configured on the deployed
  app (`StardagApp(limit_key_selector=...)`, engine-level surface:
  `TickConfig.limit_key_selector`) — a denied
  task stays in the frontier and proceeds when a slot frees (same-build
  releases wake the scheduler directly; cross-build releases are covered
  by the watchdog). New `RegistryABC.task_start_with_limits_aio` (default:
  no enforcement, for custom backends). Resident-mode (`build_aio`)
  integration ships in this release too, via `RegistryConcurrencyLimiter`
  (see below).
  **Requires a matching stardag-api version**: an older server ignores
  the enforcement parameters (no error), so deploy the server before
  relying on limits. A staleness escape hatch
  (`TickConfig.stale_running_no_ref_seconds`, default 30 min) fails
  tasks stuck RUNNING without an execution ref — e.g. a scheduler crash
  between slot acquisition and spawn — so leaked slots always free; the
  watchdog is strongly recommended when limits are enforced.
  ([#158](https://github.com/stardag-dev/stardag/pull/158))
- Reactive scheduling: on a failure terminal, ticks now mark tasks
  transitively blocked by the failure as **skipped** (server-computed via
  `POST /api/v1/builds/{id}/skip-blocked`) — mirroring the resident engine, so
  blocked tasks no longer dangle pending in the UI while the build shows
  failed. Together with the retry/roots endpoints below, reactive
  re-triggers now have a complete recovery story: failed builds can be
  re-triggered (failed tasks reset to pending) and roots added mid-build
  are covered by completion detection.
  ([#160](https://github.com/stardag-dev/stardag/pull/160))
- **Pickle-free task rehydration from registry data.** New
  `stardag.task_from_registry_data(task_data, expected_task_id=...)`
  reconstructs a task instance from the payload stored at registration
  (`TaskMetadata.body`) via the polymorphic validator — the payload is
  self-describing (embedded namespace/name discriminators, recursively
  for nested `TaskLoads`/`SubClass` fields). Requirements/limits: the
  defining module must be imported; nested task fields must use the
  polymorphic annotations; `AliasTask` payloads are rejected (they embed
  pickled bytes); the optional identity check guards against
  non-round-trippable custom serializers. Reactive scheduler ticks now
  use it as a fallback when a task's stored pickle is missing or
  unloadable (healing the store on success) — an app redeploy with
  compatible task definitions no longer breaks in-flight reactive
  builds. This is also the foundation for UI-triggered task retries.
  ([#162](https://github.com/stardag-dev/stardag/pull/162))
- **Registry-backed concurrency limits for resident builds.** New
  `stardag.build.RegistryConcurrencyLimiter` implements the
  `ConcurrencyLimiter` seam on top of the registry's named environment
  limits: pass a `RegistryConcurrencyLimiter(key_selector=...)` as
  `concurrency_limiter` to `build`/`build_aio` and the named caps are
  enforced server-side — shared with reactive builds and other resident
  builds, across processes and machines (the "future global,
  server-driven limiter" the seam was reserved for). Acquisition is an
  enforced task start (slot = RUNNING status; freed on completion,
  failure, cancellation, or dynamic-deps suspension — parity with
  `LocalConcurrencyLimiter`); denials block-and-retry at a configurable
  poll interval (with jitter), transient registry errors retry with
  exponential backoff, and an optional timeout fails the task. Requires
  a matching stardag-api version. Note: unlike reactive scheduling,
  resident mode has no automatic healer for slots held by a crashed
  build process — see the
  [concurrency-limits docs](docs/docs/concepts/build-execution.md#concurrency-limits)
  and the new holders/evict admin API below.
  ([#163](https://github.com/stardag-dev/stardag/pull/163))
- **Reactive builds are owned by their triggering app.** With multiple
  `StardagApp`s deployed in one environment, a scheduler tick from an
  app that doesn't own the build (per the `app_name` recorded at trigger
  time) now forwards the wake-up to the owner app's tick (best-effort)
  and returns `outcome="foreign_app"` instead of driving the build with
  the wrong app's code — previously whichever app's watchdog won the
  scheduler lease would tick every reactive build in the environment.
  Forwarding means wake-ups landing on the wrong app (e.g. a previous
  owner's still-running worker finishing after a takeover) are not
  dropped, and every app's watchdog doubles as cross-app coverage.
  Same-name redeploys are unaffected; move a build to a new app by
  re-triggering it from that app (rewrites ownership and re-persists the
  task objects — new ticks only: a mid-linger tick of the old owner
  drains first). Builds triggered by older SDK versions (no recorded
  owner) are ticked by any app, as before.
  ([#164](https://github.com/stardag-dev/stardag/pull/164))
- **Executor metadata for Modal executions.** Task starts and triggered
  builds now record a descriptive `executor_metadata` dict — for Modal
  the kind, app name, workspace, environment, and function name (plus a
  `reactive` flag at the build level) — surfaced by the UI as Modal
  dashboard deep links. Resolution is lazy, cached, and best-effort
  (workspace via a Modal token lookup; a failure never fails or delays a
  start); override with `StardagApp(modal_workspace=...)` or
  `ModalTaskExecutor(modal_workspace=...)`. Worker self-reported starts
  carry the same dict (forwarded via `STARDAG_MODAL_*` env overrides).
  Registry surface: optional `executor_metadata` on `task_start[_aio]`,
  `task_start_with_limits_aio`, `build_start[_aio]` and
  `build_resume[_aio]`, plus `DetachedHandle.executor_metadata`; custom
  `RegistryABC` implementations with the old signatures keep working
  (the metadata is dropped gracefully).
  ([#165](https://github.com/stardag-dev/stardag/pull/165))
- **`stardag/testing/modal`**: New `live_modal_guard()` centralizes gating of
  live-Modal tests, controlled by `STARDAG_MODAL_LIVE_TESTS`
  (`auto`/`1`/`0`) and an optional `STARDAG_MODAL_TEST_PROFILE` safety guard
  (skip unless the active Modal profile matches — protects shared/production
  workspaces from accidental test runs). Live test modules are now marked
  `modal_live`, so `pytest -m "not modal_live"` runs the pure unit tier. A new
  live-semantics test module pins the Modal platform behaviors stardag relies
  on (detached spawned calls, `FunctionCall.from_id` re-attach, call-id
  stability across retries, cancellation).
  ([#154](https://github.com/stardag-dev/stardag/pull/154))

### Registry API

- `POST /api/v1/builds/{id}/resume` no longer records a `BUILD_RESUMED` event for a
  "fresh" build (no activity beyond `BUILD_STARTED`), so attaching to a
  trigger-minted build id on the first run doesn't display the build as
  resumed. Real resumes (any task activity or terminal state) are recorded as
  before. ([#154](https://github.com/stardag-dev/stardag/pull/154))
- New reactive-scheduling endpoints: `POST`/`DELETE /api/v1/builds/{id}/notify`
  (scheduler wake-up flag, new `builds.needs_tick_at` column) and
  `GET /api/v1/builds/{id}/frontier` — the build's actionable tasks (global status
  pending/suspended/running with all upstream dependencies completed,
  including executor refs for liveness probing), per-status counts, root
  statuses, and build status, for scheduler ticks.
  ([#157](https://github.com/stardag-dev/stardag/pull/157))
- Named environment concurrency limits:
  `GET`/`PUT`/`DELETE /api/v1/concurrency-limits[/{key}]` (new
  `environment_concurrency_limits` +
  `task_limit_keys` tables) with atomic enforcement on task start (409
  `concurrency_limit_reached`; the environment's limit rows are locked
  while active RUNNING holders are counted, serializing concurrent
  acquires; re-starting a RUNNING task never self-blocks).
  ([#158](https://github.com/stardag-dev/stardag/pull/158))
- New `POST /api/v1/builds/{id}/skip-blocked`: emits `TASK_SKIPPED` for
  pending/suspended tasks transitively downstream of a
  failed/cancelled/skipped task (recursive dependency-edge closure, one
  transaction). ([#160](https://github.com/stardag-dev/stardag/pull/160))
- New `TASK_RETRIED` event + `POST /api/v1/builds/{id}/tasks/{task_id}/retry`:
  resets a failed/cancelled/skipped task to pending (never downgrades
  completed/running) — the retry path for reactive builds.
  ([#160](https://github.com/stardag-dev/stardag/pull/160))
- New `POST /api/v1/builds/{id}/roots`: append root task ids to a build
  (deduplicated), so completion detection covers roots added to an
  active build. ([#160](https://github.com/stardag-dev/stardag/pull/160))
- Executor metadata: task starts accept a JSON `executor_metadata` query
  param (recorded in the `TASK_STARTED` event metadata, denormalised to
  the new nullable `tasks.latest_executor_metadata` column with the same
  set/clear-on-every-start semantics as `latest_executor_ref`); build
  creation accepts an `executor_metadata` body field and
  `POST /api/v1/builds/{id}/resume` a JSON query param (new nullable
  `builds.executor_metadata` column — kept on resumes that don't carry
  metadata). Exposed as `latest_executor` / `latest_executor_ref` /
  `latest_executor_metadata` on task responses (detail, list rows,
  search results, build task rows, frontier refs, bulk-register refs)
  and `executor_metadata` on build responses. All additive/nullable —
  older SDKs and servers are unaffected. The metadata dict is capped at
  2 KB (compact JSON, 422 above) on all ingest paths.
  ([#165](https://github.com/stardag-dev/stardag/pull/165))
- Concurrency-limits admin: new `GET /api/v1/concurrency-limits/{key}/holders`
  (the RUNNING tasks currently counted against a key — task identity,
  running-since, executor fields; paginated via `limit`, oldest first)
  and `POST /api/v1/concurrency-limits/{key}/holders/{task_id}/evict` (records
  `TASK_FAILED` for a task that is currently RUNNING **and** holds the
  key — 404 otherwise, deliberately not a generic kill endpoint — freeing
  all its slots via the normal status transition; the evicting identity
  is recorded in the event). Closes the resident-mode slot-leak recovery
  gap: reactive builds self-heal leaked slots via scheduler ticks,
  resident builds now have an admin path. Eviction also sets the owning
  build's scheduler wake-up flag so reactive builds observe it promptly.
  ([#165](https://github.com/stardag-dev/stardag/pull/165))
- Concurrency-limit **writes are admin-gated on the user auth path**:
  `PUT`/`DELETE /api/v1/concurrency-limits/{key}` and the evict endpoint
  require the workspace ADMIN role (or higher) when authenticated as a
  user (JWT); API-key auth (machine credentials) keeps full access, and
  reads (limit list, holders) stay member-level.
  ([#165](https://github.com/stardag-dev/stardag/pull/165))
- Fix: `GET /api/v1/tasks`, `GET /api/v1/tasks/{task_id}` and the task registration
  responses now populate `is_phantom` (previously always the schema
  default `false`, so placeholder rows were indistinguishable from real
  tasks on these endpoints).
  ([#165](https://github.com/stardag-dev/stardag/pull/165))

### UI

- **Modal execution surfacing.** Tasks executed on Modal now show a
  "⚡ Modal" badge (tooltip shows the function call ref; click to copy)
  in the build task table, the Task Explorer, and DAG node hover. The
  task detail panel gains an **Execution** section — executor kind, app
  name, function name, call ref, and workspace/environment — with deep
  links into the Modal dashboard (app page and function call). The
  build view shows a "Modal: app-name" chip linking to the app page
  plus a "reactive" badge for tick-scheduled builds. All Modal URL
  patterns are centralized in `src/utils/modalLinks.ts`; links render
  only when the recorded metadata has the required fields (older
  servers / missing metadata degrade to plain text, never dead links).
  ([#166](https://github.com/stardag-dev/stardag/pull/166))
- **Concurrency limits admin view.** New env-scoped "Concurrency
  Limits" sidebar page: list the environment's named limits with
  current holder counts, create/edit/delete keys, and drill into a
  key's holders (task detail link, running-since, executor badge,
  Modal deep link) with an **Evict** action that fails a stuck RUNNING
  holder to free its slots — the recovery path for slots leaked by a
  crashed resident build process.
  ([#166](https://github.com/stardag-dev/stardag/pull/166))

### Docs

- Expanded the
  [Build & Execution concepts page](docs/docs/concepts/build-execution.md)
  with the new execution model: detached execution and re-attach,
  worker-side lifecycle reporting, reactive scheduling, and the
  concurrency-limit mechanisms (build-local, registry-backed named
  limits, global lock). The bundled `stardag` agent skill is updated to
  match. ([#159](https://github.com/stardag-dev/stardag/pull/159))

## [0.9.0] — 2026-06-16

### SDK

- **`stardag/build`**: Add build-level concurrency limits for task execution
  via a new `ConcurrencyConfig` accepted by `build` / `build_aio`. Supports an
  overall cap (`max_concurrent_tasks`) and named limits mapped to tasks through
  a callback (`limits={"request-to-service-x": 10}` with a `key_selector`); a
  task may be subject to multiple named limits at once. Enforced uniformly
  across all executors (local, Modal, routed) by gating the executor submit
  call. The slot is released while a task is suspended on its own dynamic deps
  and re-acquired on resume (unlike the global lock, which is held across
  suspension), and composes with the global concurrency lock.
  `ConcurrencyLimiter` is a protocol seam for a future global, server-configured
  limiter. Local to a single build for now.
  ([#151](https://github.com/stardag-dev/stardag/pull/151))
- **`stardag/integration/modal`**: A `WorkerSelector` may now return a
  `(worker_name, env_overrides)` tuple in addition to a bare worker name (new
  `WorkerSelection` type). When provided, `env_overrides` is a
  `dict[str, str]` of environment variables set temporarily around the task's
  `run` call inside the Modal worker and restored afterwards — e.g. to tune
  task-specific execution knobs (worker/thread counts, batch sizes, library
  env vars). `Runner.__call__` gained an optional `env_overrides`
  parameter; the `RunFunction` protocol's required signature is unchanged, so
  existing `(task)`-only run functions keep working (overrides are applied to
  the environment around the call for them). Also caches the per-worker
  `modal.Function.from_name` lookup in `ModalTaskExecutor` instead of
  recreating the handle on every submit. Backward compatible.
  ([#152](https://github.com/stardag-dev/stardag/pull/152))

## [0.8.1] — 2026-06-12

Compatibility fix for `stardag modal deploy` on modal >= 1.4.3. No
client-code changes required.

### SDK

- **`stardag/_cli/modal.py`**: Fix `stardag modal deploy` crashing with
  `ImportError: cannot import name 'ensure_env' from 'modal.environments'` on
  modal >= 1.4.3, where `ensure_env` moved to a private module. The small
  environment-resolution logic is now inlined using modal's public config API,
  avoiding any dependency on modal-internal modules.
  ([#148](https://github.com/stardag-dev/stardag/issues/148),
  [#150](https://github.com/stardag-dev/stardag/pull/150))
- Dev lockfile: bump modal 1.3.3 → 1.5.0 so CI continuously tests against a
  modal version affected by the import breakage. The supported range is
  unchanged (`modal>=1.0.0`).

## [0.8.0] — 2026-06-11

Behaviour fix to `StardagField(compat_default=...)` that can change task
IDs/hashes for fields with non-trivially-serialized types. See
[RELEASE_NOTES.md](RELEASE_NOTES.md#v080--compat_default-compares-the-raw-python-value)
for the migration guide.

### SDK

- **`stardag/base_model.py`**: `StardagField(compat_default=...)`'s hash-mode
  drop now compares the field's **raw Python value** against `compat_default`
  instead of the already-serialized value. Previously the feature silently
  no-opped for any field whose serialized form differs from its Python value —
  enums (→ `.value`), tuples (→ lists), or fields with a custom/hash-only
  serializer — so adding such a field with a compat default still changed
  existing task IDs/hashes. The comparison is now symmetric with the
  compat-validation path (which also uses the raw value): `compat_default` is
  supplied in its natural validated Python form rather than the serialized
  form. **Breaking** for the affected types — see the migration guide.
  ([#146](https://github.com/stardag-dev/stardag/issues/146),
  [#147](https://github.com/stardag-dev/stardag/pull/147))
- Documented `StardagField.compat_default` / `hash_exclude` (previously a
  `TODO` docstring).

## [0.7.3] — 2026-05-08

`stardag modal deploy` and `stardag modal stardag-api-key create` now
display the slug that actually corresponds to the resolved
workspace/environment UUID. Previously the slug was read from the active
CLI profile's TOML, which could be unrelated to the resolved UUID when
env vars or a custom `config_provider` override the IDs — producing
misleading lines pairing the resolved UUID with a slug from an unrelated
profile. No client-code changes — `pip install -U stardag` is
sufficient. See
[RELEASE_NOTES.md](RELEASE_NOTES.md#v073--correct-slug-display-in-stardag-modal-cli)
for details.

### SDK

- **`stardag/_cli/modal.py`**: replaced `_get_profile_slugs` with
  `_resolve_display_slugs`, which reverse-looks up the slug from the
  resolved UUID via the id-cache. Slug is omitted when no cache hit
  rather than guessing from the active profile.
- **`stardag/config/cache.py`**: added `get_cached_workspace_slug` and
  `get_cached_environment_slug` (UUID → slug reverse lookups).

## [0.7.2] — 2026-05-08

`sd.build(resume_build_id=...)` now fires a `BUILD_RESUMED` event so
resumed builds flip back to **running (resumed)** in the UI and jump to
the top of the Home list, instead of silently keeping their previous
terminal status. No client-code changes — `pip install -U stardag` is
sufficient. See
[RELEASE_NOTES.md](RELEASE_NOTES.md#v072--build-resume-status-fix-and-skipped-ui-polish)
for details.
([#141](https://github.com/stardag-dev/stardag/pull/141))

### SDK

- **`RegistryABC.build_resume` / `build_resume_aio`** added (default
  no-op for older registry backends). `build`, `build_aio`,
  `build_sequential`, `build_sequential_aio` call it whenever
  `resume_build_id` is set, immediately after adopting the existing
  build id. `APIRegistry` swallows the missing-route 404 from older
  servers via the existing `_is_route_not_found` pattern (warning
  logged, build runs to completion locally).

### Registry API

- **New `EventType.BUILD_RESUMED`** + **`POST
/api/v1/builds/{build_id}/resume`** endpoint, mirroring the existing
  `/complete` / `/fail` / `/cancel` shape. Status replay treats
  `BUILD_RESUMED` like `BUILD_STARTED` (flips status to `RUNNING`,
  clears `completed_at`) and exposes a derived `is_resumed: bool` flag
  on `BuildResponse` — true while the latest build-level event is
  `BUILD_RESUMED`, cleared by any subsequent terminal or
  `BUILD_STARTED` event.
- **New `Build.last_active_at` column** (Alembic migration backfills
  from `created_at`). Touched only on build-level lifecycle events
  (`BUILD_RESUMED` / `BUILD_COMPLETED` / `BUILD_FAILED` /
  `BUILD_CANCELLED` / `BUILD_EXIT_EARLY`) — task events deliberately
  skip this write to avoid row-lock contention against the build row
  under high task concurrency. `GET /builds` now sorts by
  `(last_active_at desc, id desc)` so resumed builds rise to the top
  while `Build.id` (UUID7) keeps pagination stable across timestamp
  ties.
- **`/tasks/search/values?key=status`** autocomplete returns the full
  filterable status set (was hardcoded to `pending`/`running`/
  `completed`/`failed`; now includes `suspended`/`skipped`/`cancelled`).
  `unregistered` is still excluded — it's an internal phantom-row
  marker, not a status users filter on.

### UI

- **"running (resumed)" badge** in `BuildStatusBadge` (Home list and
  build-view breadcrumb) when the API reports `is_resumed`.
- **`skipped` task status** added to `TaskStatus` (was previously
  unhandled). Renders in **amber** across `StatusBadge`, the DAG node
  border (`TaskNode`), and the Task Explorer table — was effectively
  near-invisible black-on-dark-blue before. The build-view status
  filter dropdown also gained the missing **Skipped** and
  **Cancelled** options.

### Compatibility

- **New SDK against an older Registry API** (no `/resume` route):
  degrades gracefully via `_is_route_not_found`. The build still runs
  to completion locally; the registry-side status flip is the only
  thing missing until the API is upgraded.
- **Older SDK against the new API**: unaffected. The new SDK call is
  additive, and `last_active_at` is initialised on insert by the column
  default plus bumped by the new build-level handlers, so list
  ordering is correct without SDK cooperation.

## [0.7.1] — 2026-05-05

Modal: `StardagApp.build_spawn` / `build_remote` now accept multiple root
tasks (`Sequence[BaseTask] | BaseTask`) and a new `build_kwargs` dict
forwarded to `stardag.build(...)`. **Breaking**: first parameter renamed
`task` → `tasks` for consistency with `stardag.build()` and
`Builder.__call__`; the `BuildFunction` protocol gains a 4th
`build_kwargs=None` parameter (custom implementations must accept it).
See
[RELEASE_NOTES.md](RELEASE_NOTES.md#v071--multi-root-builds-and-build_kwargs-on-stardagapp)
for the migration.
([#140](https://github.com/stardag-dev/stardag/pull/140))

## [0.7.0] — 2026-05-05

`FailMode.FAIL_FAST` now actually fails fast: in-flight sibling tasks
are cancelled (rather than silently abandoned) and tasks blocked by a
failed dependency emit `TASK_SKIPPED` rather than staying `PENDING`
forever. No client-code changes — `pip install -U stardag` is
sufficient. See
[RELEASE_NOTES.md](RELEASE_NOTES.md#v070--fail_fast-actually-fails-fast-explicit-skipped-status-for-blocked-tasks)
for details.
([#139](https://github.com/stardag-dev/stardag/pull/139))

### SDK

#### New behaviour

- **FAIL_FAST cancels in-flight siblings.** Asyncio cancel propagates
  into `modal.Function.remote.aio` and terminates the remote
  container; each cancelled task fires `TASK_CANCELLED` and releases
  any global lock it held. Previously the build re-raised in place,
  abandoning Modal calls (containers kept running and billing; registry
  left them stuck in `RUNNING`).
- **Tasks blocked by failed deps emit `TASK_SKIPPED`** (both
  `FAIL_FAST` and `CONTINUE`). A fixed-point walk after the loop emits
  per-task skip events for transitively blocked downstream work.
- **Sibling completions in the same `asyncio.wait` `done` batch as a
  failure are no longer lost** — `process_result` defers FAIL_FAST
  escalation until the batch finishes, so sibling
  `task_complete_aio`/`task_fail_aio` events still land.

#### New public API (additive; default no-op for existing implementations)

- `TaskExecutorABC.cancel(task)` — optional best-effort cancel hook.
  `RoutedTaskExecutor.cancel` routes to the matching child.
- `RegistryABC.task_skip` / `task_skip_aio`.
- `TaskCount.cancelled` and `TaskCount.skipped`; rendered by
  `BuildSummary.__repr__` when non-zero.

### Registry API

- **New `POST /api/v1/builds/{build_id}/tasks/{task_id}/skip`** —
  emits `TASK_SKIPPED`, mirroring the existing `/cancel` route.

### Compatibility

New SDK against an older Registry API (no `/skip` route): degrades
gracefully via the existing `_is_route_not_found` pattern (warning
logged, blocked tasks stay `PENDING` — pre-0.7.0 observable
behaviour). Older SDK against the new API: unaffected (additive
endpoint only).

## [0.6.1] — 2026-05-05

Patch release covering the Modal-volume integration. No breaking changes;
`pip install -U stardag` is sufficient. See
[RELEASE_NOTES.md](RELEASE_NOTES.md#v061--modal-volume-disk-cache-opt-in-and-reload-staleness-fix)
for details and the cache-config recipe.

### SDK

#### New features

- **Optional local-disk cache for `modalvol://` targets**: when a Modal
  volume is _not_ mounted locally (i.e. running outside Modal),
  `RemoteFileTarget`-backed reads and writes can now be transparently
  cached on local disk via a `CachedRemoteFileSystem` wrapper, mirroring
  the existing S3 integration. **Opt-in via
  `STARDAG_TARGET_MODALVOL_CACHE_ROOT`** — there is intentionally no
  default cache root, because Modal volume names are only unique within
  a `(workspace, environment)` pair (unlike S3's globally-unique bucket
  names) and a default would silently collide across profiles. When the
  volume _is_ mounted (running on Modal, or via
  `STARDAG_MODAL_VOLUME_MOUNTS` / the auto-mount path),
  `get_modal_target` continues to return a `ModalMountedVolumeFileTarget`
  that bypasses the RFS entirely — caching is automatically inactive on
  Modal workers.
  ([#135](https://github.com/stardag-dev/stardag/pull/135))

#### Bug fixes

- **Fix volume-reload staleness in `ModalMountedVolumeFileTarget`**:
  the previous lazy-reload path imposed a 5-second cooldown between
  reloads of the same volume to prevent thundering-herd reloads during
  discovery. As a side-effect, that cooldown could also suppress a
  reload that was _needed_ — e.g. a write committed at T+4 was
  invisible to an `exists()` check at T+4.5 if the last reload happened
  at T. Worst-case observable staleness: up to 5 seconds. The cooldown
  is replaced with per-volume singleflight coalescing (`threading.Lock`
  for sync, `asyncio.Lock` for async), and bookkeeping records the
  reload's _issue_ time (not its completion time) so a caller that
  started during another's in-flight reload correctly triggers a fresh
  reload of its own. The original thundering-herd protection during
  concurrent async discovery is preserved by the lock alone.
  ([#136](https://github.com/stardag-dev/stardag/pull/136))
- **Cross-loop safety for the async reload lock**: `asyncio.Lock`
  instances are bound to the running event loop at acquire-time. The
  cache is now keyed by `(volume_name, id(running_loop))`, so a fresh
  `asyncio.run()` gets its own lock instance instead of reusing one
  bound to a now-closed loop. ([#136](https://github.com/stardag-dev/stardag/pull/136))
- **Crash-atomic `CachedRemoteFileSystem.upload(_aio)`**: cache-write
  paths now publish via tmp-then-`replace` (mirroring the existing
  download paths), so a crash mid-`shutil.copy` can never leave a
  partial file at the final cache path. Uses `Path.replace` /
  `aiofiles.os.replace` for the atomic publish, which also lets cache
  refresh (re-uploading the same URI) work cross-platform — plain
  `rename` would fail to overwrite on Windows.
  ([#138](https://github.com/stardag-dev/stardag/pull/138))

## [0.6.0] — 2026-04-30

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
  (initial + each dynamic-deps yield), chunked at 50 tasks per HTTP
  request (well under the API's 1000 hard cap so DB transactions stay
  short and request bodies stay friendly even with fat task specs).
  For large fan-out DAGs this is a dramatic reduction in HTTP
  round-trips — a 5000-task DAG goes from 5000 individual POSTs to
  100 bulk POSTs. Per-task fallback on 404 (older API deployments).
- **Gzipped request bodies on the wire**: JSON request bodies above 1KB
  are gzipped client-side before sending; bulk-register payloads with
  repeated structure compress 5–10× typically. The server's new
  `GZipRequestMiddleware` decompresses transparently — old SDKs and
  non-gzipped requests pass through unchanged.
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
  Optional `?id_only=true` query param returns only `{id, task_id}`
  per task instead of the full `TaskResponse` (~10× smaller
  response — the SDK passes this since it doesn't read the response).
- **`GZipRequestMiddleware`**: ASGI middleware that decompresses
  incoming `Content-Encoding: gzip` request bodies before route
  handlers parse them. Pass-through for non-gzipped requests so old
  SDK versions, direct `curl` callers, and non-bulk endpoints keep
  working unchanged. Returns 400 on malformed gzip so clients see a
  clear error rather than a downstream parse failure.
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
