# Changelog

All notable changes to the Stardag project (SDK, Registry API, and UI).

For detailed SDK migration guides, see [RELEASE_NOTES.md](RELEASE_NOTES.md).

## [Unreleased]

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

  What this does **not** change: proving a blocker dead still does not let
  your build run it — a blocker outside your build's task set has to be
  released under the build that owns it, so the build still fails, just with
  certainty about why. And the collapse applies to RUNNING blockers only: a
  SUSPENDED or PENDING blocker holds no claim and therefore carries no
  expiry, so "will anyone move it?" is still asked of its owning build (an
  abandoned-SUSPENDED upstream remains a real wedge, recovered by retrying
  it under its owner).

- **Every start records a claim TTL derived from the executor's own
  timeout.** For Modal that is the worker function's `timeout` from its
  `FunctionSettings`, plus a grace margin; where no timeout is known the
  registry's default applies. Granting an expiry on every start is what
  makes an abandoned claim heal, but it also means a task outliving its TTL
  could have its claim taken while alive — deriving the TTL from the limit
  the backend itself enforces is what keeps that from being a real risk.
  Setting an explicit `timeout` on long-running Modal workers is therefore
  worth doing.

- **Removed: `TickConfig.stale_external_blocker_seconds`,
  `TickConfig.stale_running_no_ref_seconds` and
  `ClaimConfig.stale_running_no_ref_seconds`.** All three configured a local
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
  status, how long it has been in it, the owning build, and which of the two
  remedies applies (a blocker outside this build's task set can only be
  waited on or released; one inside it resolves when its owner finishes it).
  It also states honestly that the registry computes that list only for a
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
  counts. Common whenever DAGs overlap, and especially when the blocker is
  a dynamic dependency registered under an earlier build, so it is not in
  the new build's task set at all.

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
  and how to release it. A blocker inside this build's own task set is
  unchanged: it is already visible in the counts, and still fails the build
  when it can never run.

  Both waits are bounded by the new
  `TickConfig.stale_external_blocker_seconds` (default 6 hours), measured on
  how long the blocker has sat in its status rather than on the tick, which
  is too short-lived to bound anything: past the bound the blocker is
  presumed abandoned and the build fails with the full explanation instead
  of hanging silently. `None` waits indefinitely. `TickSummary` gains
  `external_blockers`, `external_blockers_waited` and
  `external_blockers_fatal`, and `BuildInfo` gains `status` (the build's
  derived status, `None` when a server or custom registry does not report
  it). Owner liveness is resolved only when a build actually looks stalled,
  and once per owning build per pass, so a healthy build issues no extra
  requests however often it polls. Requires a stardag-api version matching
  this SDK; against an older server the blocker list is always empty and
  terminal detection behaves exactly as before.

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

- **`TickConfig.stale_running_no_ref_seconds` takes effect for the first
  time**, now that the frontier actually populates
  `FrontierTaskRef.latest_status_at`. The field was declared and documented
  as the input to that bound but never sent by the server — it silently
  serialised as `null` on every ref, leaving the guard dead. Against a
  matching server, a task stuck RUNNING without an executor ref for longer
  than the bound (default 30 minutes) is now failed as intended. Raise the
  bound if you run long ref-less tasks — e.g. a resident build sharing
  tasks with a concurrently ticking reactive one.

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
